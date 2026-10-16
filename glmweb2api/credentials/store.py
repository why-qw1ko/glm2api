"""refresh_token 的本地持久化。

存储位置默认在 data/refresh_tokens.json（每行一个 token），可通过环境变量
GLM_TOKEN_FILE 覆盖（该文件与 config 里的 token 文件是同一个，便于统一管理）。

安全说明：token 以明文存储，但写入时收紧文件权限（0600）。日志与接口回显一律脱敏。
加密落盘需要一套密钥管理方案（密钥从哪来、如何轮换），在没有可靠密钥来源前，
用假加密只会给人虚假安全感，故此处明确不加密，交由部署者自行保护该文件。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

DEFAULT_STORE_PATH = Path("data") / "refresh_tokens.json"


class TokenStore:
    """refresh_token 的持久化读写。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_STORE_PATH

    def load(self) -> list[str]:
        """读取所有已保存的 token（非空、去重、忽略 # 注释行）。"""
        if not self._path.is_file():
            return []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        tokens: list[str] = []
        for line in text.splitlines():
            token = line.strip()
            if token and not token.startswith("#") and token not in tokens:
                tokens.append(token)
        return tokens

    def save(self, tokens: list[str]) -> None:
        """覆盖写入 token 列表，并收紧文件权限。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tokens-", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(tokens) + ("\n" if tokens else ""))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            # 部分平台（如某些 Windows 挂载）不支持 chmod，忽略
            pass

    def add(self, token: str) -> None:
        """追加一个 token（已存在则忽略）。"""
        tokens = self.load()
        token = token.strip()
        if token and token not in tokens:
            tokens.append(token)
            self.save(tokens)


def mask_token(token: str) -> str:
    """把 token 脱敏成可安全打印的形式。"""
    if not token:
        return "<empty>"
    if len(token) <= 8:
        return token[:2] + "***"
    return token[:6] + "***" + token[-4:]
