"""上游访问令牌管理：刷新、缓存、多账号轮转；无账号时禁止调用。

GLMKit（安卓模块）最大的短板是完全没有令牌刷新——它只抓一次短命的 access_token，
过期就失效。这里补上完整的令牌生命周期：

  refresh_token（长期，浏览器 Local Storage 里的 chatglm_refresh_token）
      │  POST /user-api/user/refresh   (Authorization: Bearer <refresh_token>, body {})
      ▼
  { access_token, refresh_token }   ← refresh_token 会轮换，必须写回
      │  Authorization: Bearer <access_token>
      ▼
  /backend-api/assistant/stream

未登录时保持空账号池，等待管理员在面板接入官方账号。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from ..config import Settings, get_settings


@dataclass
class _Account:
    """一个已导入凭据的账号槽位。"""

    refresh_token: str
    access_token: str | None = None
    expires_at: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    health: str = "unknown"
    checked_at: float | None = None


def _deep_get(obj: object, key: str) -> object | None:
    """在上游响应 JSON 里递归查找给定 key（响应包裹层级在不同接口间不完全一致）。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _deep_get(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_get(item, key)
            if found is not None:
                return found
    return None


class TokenManager:
    """多账号访问令牌管理器。

    get_access_token() 按轮转顺序返回一个可用令牌；过期时自动刷新。
    刷新失败会尝试下一个账号（触发一次账号切换），避免单个账号失效拖垮整体。
    """

    def __init__(self, settings: Settings | None = None, store=None) -> None:
        self._settings = settings or get_settings()
        self._lock = asyncio.Lock()
        self._store = store  # 可选：刷新后的 token 写回存储
        self._accounts: list[_Account] = self._build_accounts()
        self._index = 0
        self.persistence_error = False

    def _build_accounts(self) -> list[_Account]:
        return [_Account(refresh_token=t) for t in self._settings.refresh_token_values]

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    def require_account(self) -> None:
        if not self._accounts:
            from ..errors import GatewayError

            raise GatewayError(503, "account_login_required", "请先在 /panel/ 登录并接入官方账号")

    def _next_index(self) -> int:
        idx = self._index
        self._index = (self._index + 1) % max(1, len(self._accounts))
        return idx

    async def get_access_token(self) -> tuple[str, int]:
        """返回 (access_token, 账号下标)。下标供日志与失败切号用。"""
        async with self._lock:
            self.require_account()
            for _ in range(len(self._accounts)):
                idx = self._next_index()
                account = self._accounts[idx]
                if account.access_token and account.expires_at > time.time() + 10:
                    return account.access_token, idx
                try:
                    token = await self._refresh(account)
                    account.health = "healthy"
                    account.checked_at = time.time()
                    account.access_token = token
                    account.expires_at = time.time() + self._token_ttl()
                    return token, idx
                except Exception:
                    account.health = "refresh_failed"
                    account.checked_at = time.time()
                    # 单个账号失败：继续尝试下一个，而不是直接抛错。
                    continue
        # 所有账号都失败
        from ..errors import GatewayError

        raise GatewayError(502, "token_refresh_failed", "所有账号的访问令牌刷新均失败")

    async def _refresh(self, account: _Account) -> str:
        from .auth import build_refresh_headers
        import httpx

        settings = self._settings
        async with httpx.AsyncClient(
            proxy=settings.proxy or None,
            timeout=settings.glm_timeout,
            follow_redirects=False,
        ) as client:
            resp = await client.post(settings.refresh_url,
                                     headers=build_refresh_headers(account.refresh_token), content=b"{}")
        if resp.status_code != 200:
            raise RuntimeError(f"Token 刷新 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("Token 刷新返回非 JSON") from None
        if not isinstance(data, dict):
            raise RuntimeError("Token 刷新返回不是 JSON 对象")
        if data.get("code", data.get("status")) not in (0, None):
            raise RuntimeError("Token 刷新业务失败")
        result = data.get("result")
        result = result if isinstance(result, dict) else data
        access = _deep_get(result, "access_token")
        if not isinstance(access, str) or not access:
            raise RuntimeError("Token 刷新响应缺少 access_token")
        new_refresh = _deep_get(result, "refresh_token")
        if isinstance(new_refresh, str) and new_refresh and new_refresh != account.refresh_token:
            account.refresh_token = new_refresh
            self._persist_rotated()
        return access

    def _persist_rotated(self) -> None:
        """把当前所有账号的 refresh_token 写回存储（若配置了 store）。"""
        if self._store is None:
            return
        tokens = [a.refresh_token for a in self._accounts if a.refresh_token]
        try:
            self._store.save(tokens)
            self.persistence_error = False
        except Exception:
            self.persistence_error = True

    def snapshot(self) -> list[dict]:
        from ..credentials.store import mask_token
        return [{"id": a.id, "label": f"账号 {i + 1}", "guest": False,
                 "token": mask_token(a.refresh_token), "health": a.health,
                 "checked_at": a.checked_at, "cache_until": a.expires_at or None}
                for i, a in enumerate(self._accounts)]

    async def add(self, token: str):
        from ..errors import GatewayError
        if not token or len(token) > 16384 or any(c.isspace() for c in token):
            raise GatewayError(400, "invalid_token", "Token 不能为空或包含空白字符")
        async with self._lock:
            existing = list(self._accounts)
            if any(a.refresh_token == token for a in existing):
                return False
            if self._store is None:
                raise GatewayError(500, "token_store_missing", "凭据存储不可用")
            try:
                self._store.save([a.refresh_token for a in existing] + [token])
            except OSError:
                raise GatewayError(500, "token_store_failed", "凭据保存失败，请检查数据目录")
            self._accounts = existing + [_Account(refresh_token=token)]
            self._index = 0
            return True

    async def remove(self, account_id):
        async with self._lock:
            accounts = [a for a in self._accounts if a.id != account_id]
            if not any(a.id == account_id for a in self._accounts):
                return False
            self._store.save([a.refresh_token for a in accounts])
            self._accounts = accounts
            self._index = 0
            return True

    async def check(self, account_id):
        async with self._lock:
            account = next((a for a in self._accounts if a.id == account_id), None)
            if account is None:
                return None
            try:
                account.access_token = await self._refresh(account)
                account.expires_at = time.time() + self._token_ttl()
                account.health = "healthy"
            except Exception:
                account.health = "refresh_failed"
            account.checked_at = time.time()
            return next(a for a in self.snapshot() if a["id"] == account_id)

    @staticmethod
    def _token_ttl() -> float:
        """访问令牌的有效期。上游未稳定返回 expires_in，这里用保守默认值。

        保守一点意味着多刷新几次，但能避免"缓存了已过期的令牌"导致偶发 401。
        """
        return 300.0
