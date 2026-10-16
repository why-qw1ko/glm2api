"""独立管理员认证；Cookie 会话与 CSRF，不接受普通反代密钥。"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import threading
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

COOKIE = "glm_panel_session"


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def same_origin(request: Request):
    origin = request.headers.get("origin")
    expected = urlsplit(str(request.base_url))
    if request.headers.get("sec-fetch-site") == "cross-site" or (
        origin and origin.rstrip("/") != f"{expected.scheme}://{expected.netloc}"
    ):
        raise HTTPException(403, "请求来源不匹配")


class AdminAuth:
    def __init__(self, db, password, data_dir: Path, secure=False):
        self.db = db
        self.secure = secure
        self.sessions = {}
        self.attempts = deque(maxlen=20)
        self.login_lock = threading.Lock()
        stored = db.get("admin_password")
        self.environment_password = bool(password)
        if password:
            if len(password) < 12:
                raise RuntimeError("PANEL_PASSWORD 至少需要 12 个字符")
            self.hashed = password_hash(password)
        elif stored:
            self.hashed = stored
        else:
            target = data_dir / "panel-bootstrap.txt"
            if target.is_file():
                # 首次初始化在写文件后意外终止时，可安全继续，不覆盖已生成密码。
                bootstrap = target.read_text(encoding="utf-8").strip()
                if len(bootstrap) < 12:
                    raise RuntimeError("首次密码文件无效，请设置 PANEL_PASSWORD 后重新启动")
            else:
                bootstrap = secrets.token_urlsafe(24)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(bootstrap + "\n")
            try:
                target.chmod(0o600)
            except OSError:
                pass
            self.hashed = password_hash(bootstrap)
            db.set("admin_password", self.hashed)

    def login(self, password):
        with self.login_lock:
            return self._login(password)

    def _login(self, password):
        now = time.time()
        while self.attempts and self.attempts[0] < now - 300:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            raise HTTPException(429, "登录失败次数过多，请 5 分钟后重试")
        self.attempts.append(now)
        if not hmac.compare_digest(password_hash(password, self.hashed.split(":")[0]), self.hashed):
            raise HTTPException(401, "管理员密码错误")
        self.attempts.clear()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        if len(self.sessions) >= 32:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        session = {"csrf": secrets.token_urlsafe(24), "expires": now + 8 * 3600}
        self.sessions[token] = session
        return token, session

    def require(self, request: Request):
        session = self.sessions.get(request.cookies.get(COOKIE, ""))
        if not session or session["expires"] < time.time():
            raise HTTPException(401, "请先登录管理面板")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            same_origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session["csrf"].encode()):
                raise HTTPException(403, "会话校验失败，请刷新页面")
        return session


def require_admin(request: Request):
    return request.app.state.panel.auth.require(request)
