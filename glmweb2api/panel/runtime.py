"""面板配置白名单、持久化和运行状态。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import AdminAuth
from .database import Database


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    glm_base_url: str = "https://chatglm.cn/chatglm"
    glm_timeout: float = Field(120, ge=5, le=600)
    glm_max_concurrency: int = Field(3, ge=1, le=32)
    glm_delete_conversation: bool = True
    proxy: str = ""
    chat_assistant_id: str = Field("65940acff94777010aa6b796", pattern=r"^[0-9a-f]{24}$")
    image_assistant_id: str = Field("65a232c082ff90a2ad2f15e2", pattern=r"^[0-9a-f]{24}$")
    image_model_name: str = Field("glm-image-1", min_length=1, max_length=80)
    panel_retention_days: int = Field(90, ge=1, le=3650)

    @field_validator("glm_base_url")
    @classmethod
    def upstream_url(cls, value):
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("上游必须是 HTTPS 基址，不能包含账号、密码或查询参数")
        return value.rstrip("/")

    @field_validator("proxy")
    @classmethod
    def proxy_url(cls, value):
        if not value:
            return value
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment:
            raise ValueError("出站代理必须是 HTTP/HTTPS 地址")
        try:
            url.port
        except ValueError:
            raise ValueError("代理端口无效")
        return value


class PanelRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.started = time.time()
        self.active = 0
        self.metrics_error = False
        self.lock = asyncio.Lock()
        self.data_dir = Path(settings.panel_data_dir)
        self.db = Database(self.data_dir / "panel.sqlite3")
        # 环境和 .env 明确指定的部署配置优先，界面展示锁定原因。
        from dotenv import dotenv_values
        declared = {key.upper() for key in dotenv_values(".env")} | {key.upper() for key in os.environ}
        self.locked = [name for name in RuntimeConfig.model_fields if name.upper() in declared]
        stored = self.db.get("runtime_config", {})
        effective = {name: getattr(settings, name) for name in RuntimeConfig.model_fields}
        effective.update({k: v for k, v in stored.items() if k in effective and k not in self.locked})
        validated = RuntimeConfig(**effective)
        for name, value in validated.model_dump().items():
            setattr(settings, name, value)
        self.auth = AdminAuth(self.db, settings.panel_password, self.data_dir, settings.panel_cookie_secure)
        self.db.prune(settings.panel_retention_days)
        self.logins = None
        self.maintenance_task = None

    async def maintain(self):
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(self.db.prune, self.settings.panel_retention_days)
            except Exception:
                self.metrics_error = True
                logging.getLogger("glmweb2api").error("统计保留期清理失败，请检查数据目录")

    async def close(self):
        if self.maintenance_task:
            self.maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.maintenance_task
        await self.logins.close()

    def config_view(self):
        values = {name: getattr(self.settings, name) for name in RuntimeConfig.model_fields}
        configured_proxy = bool(values["proxy"])
        values["proxy"] = ""
        return {"values": values, "locked": self.locked, "proxy_configured": configured_proxy,
                "restart_fields": ["HOST", "PORT", "PANEL_DATA_DIR", "PANEL_COOKIE_SECURE", "PANEL_PASSWORD"],
                "host": self.settings.host, "port": self.settings.port}

    async def update_config(self, patch, app):
        async with self.lock:
            if self.active or (self.logins and self.logins.running):
                raise HTTPException(409, "有请求或登录正在进行，请完成后再修改配置")
            forbidden = set(patch) - set(RuntimeConfig.model_fields)
            if forbidden:
                raise HTTPException(422, "存在不允许修改的配置项")
            if set(patch) & set(self.locked):
                raise HTTPException(409, "配置被环境变量或 .env 锁定，请从部署配置修改")
            values = {name: getattr(self.settings, name) for name in RuntimeConfig.model_fields}
            values.update(patch)
            try:
                validated = RuntimeConfig(**values)
            except ValueError:
                raise HTTPException(422, "配置校验失败，请检查 URL、助手 ID、并发数和超时时间")
            changed = [k for k, v in validated.model_dump().items() if v != getattr(self.settings, k)]
            stored = self.db.get("runtime_config", {})
            stored.update({k: getattr(validated, k) for k in patch})
            self.db.save_config(stored, changed)
            for name, value in validated.model_dump().items():
                setattr(self.settings, name, value)
            from ..upstream.client import UpstreamClient
            app.state.client = UpstreamClient(self.settings.model_copy(), app.state.tokens)
            self.db.prune(self.settings.panel_retention_days)
            return {"ok": True, "changed": changed, "effective": "immediate"}
