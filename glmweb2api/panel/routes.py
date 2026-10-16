"""Panel JSON API。所有管理操作均要求独立管理员会话。"""
from __future__ import annotations

import asyncio
import hmac
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import COOKIE, password_hash, require_admin, same_origin
from .metrics import current

public = APIRouter(prefix="/panel/api")
router = APIRouter(prefix="/panel/api", dependencies=[Depends(require_admin)])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Password(Input):
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(Input):
    current_password: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=12, max_length=256)


class TokenInput(Input):
    refresh_token: str = Field(min_length=1, max_length=16384)


class KeyInput(Input):
    name: str = Field(min_length=1, max_length=80)


class KeyState(Input):
    enabled: bool


class LoginInput(Input):
    mode: Literal["local", "remote"]


class ClickInput(Input):
    x: float = Field(ge=0, le=999)
    y: float = Field(ge=0, le=739)


class TestInput(Input):
    model: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=16000)


class ChatMessage(Input):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=0, max_length=16000)


class ChatInput(Input):
    model: str = Field(min_length=1, max_length=160)
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    mode: Literal["chat", "agent"] | None = None


@public.post("/login")
async def login(body: Password, request: Request):
    same_origin(request)
    auth = request.app.state.panel.auth
    token, session = await asyncio.to_thread(auth.login, body.password)
    response = JSONResponse({"csrf": session["csrf"], "expires": session["expires"]})
    response.set_cookie(COOKIE, token, max_age=8 * 3600, httponly=True, secure=auth.secure or request.url.scheme == "https",
                        samesite="strict", path="/")
    request.app.state.panel.db.audit("admin.login", {})
    return response


@router.get("/session")
async def session(request: Request):
    auth = request.app.state.panel.auth
    return {**auth.require(request), "password_managed_by_env": auth.environment_password}


@router.post("/logout")
async def logout(request: Request):
    panel = request.app.state.panel
    owner = panel.auth.require(request)["csrf"]
    for item in list(panel.logins.sessions.values()):
        if item.owner == owner:
            await panel.logins.cancel(item)
    panel.auth.sessions.pop(request.cookies.get(COOKIE, ""), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.post("/password")
async def change_password(body: PasswordChange, request: Request):
    auth = request.app.state.panel.auth
    if auth.environment_password:
        raise HTTPException(409, "请从部署环境修改 PANEL_PASSWORD")
    hashed = await asyncio.to_thread(password_hash, body.current_password, auth.hashed.split(":")[0])
    if not hmac.compare_digest(hashed, auth.hashed):
        raise HTTPException(401, "当前密码错误")
    hashed = await asyncio.to_thread(password_hash, body.password)
    await request.app.state.panel.logins.close()
    auth.db.set("admin_password", hashed)
    auth.hashed = hashed
    auth.sessions.clear()
    auth.db.audit("admin.password", {})
    return {"ok": True}


@router.get("/overview")
async def overview(request: Request):
    panel = request.app.state.panel
    tokens = request.app.state.tokens
    today = time.time() - ((time.time() + 8 * 3600) % 86400)
    total = (await asyncio.to_thread(panel.db.usage, 0, time.time()))["summary"]
    day = await asyncio.to_thread(panel.db.usage, today, time.time())
    return {"today": day, "total": total, "uptime": time.time() - panel.started,
            "active": panel.active, "concurrency": panel.settings.glm_max_concurrency,
            "accounts": tokens.snapshot(), "metrics_error": panel.metrics_error,
            "persistence_error": tokens.persistence_error, "login_required": not tokens.account_count,
            "api_auth_enabled": bool(panel.settings.server_api_keys or panel.db.keys()),
            "agent_mode": panel.settings.glm_agent_mode,
            "official_quota": None, "timezone": "Asia/Shanghai", "retention_days": panel.settings.panel_retention_days}


@router.get("/usage")
async def usage(request: Request, since: float = Query(0, ge=0), until: float | None = Query(None, ge=0),
                model: str = Query("", max_length=160), protocol: str = Query("", max_length=30),
                key_id: str = Query("", max_length=80), outcome: str = Query("", max_length=20),
                page: int = Query(1, ge=1, le=100000)):
    until = until if until is not None else time.time()
    since = since or until - 7 * 86400
    if until < since or until - since > 366 * 86400:
        raise HTTPException(422, "查询时间范围须为 0 至 366 天")
    return await asyncio.to_thread(request.app.state.panel.db.usage, since, until, model, protocol, key_id, outcome, page)


@router.get("/accounts")
async def accounts(request: Request):
    return {"accounts": request.app.state.tokens.snapshot(), "official_quota": None}


@router.post("/accounts")
async def add_account(body: TokenInput, request: Request):
    added = await request.app.state.tokens.add(body.refresh_token.strip())
    request.app.state.panel.db.audit("account.import", {"added": added})
    return {"ok": True, "added": added}


@router.post("/accounts/{account_id}/check")
async def check_account(account_id: str, request: Request):
    account = await request.app.state.tokens.check(account_id)
    if account is None:
        raise HTTPException(404, "账号不存在")
    request.app.state.panel.db.audit("account.check", {"id": account_id, "health": account["health"]})
    return account


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str, request: Request):
    if request.app.state.panel.active:
        raise HTTPException(409, "有请求正在使用账号，请稍后移除")
    if not await request.app.state.tokens.remove(account_id):
        raise HTTPException(404, "账号不存在")
    request.app.state.panel.db.audit("account.remove", {"id": account_id})
    return {"ok": True}


@router.get("/keys")
async def keys(request: Request):
    return {"keys": request.app.state.panel.db.keys(),
            "environment_key_count": len(request.app.state.settings.server_api_keys)}


@router.post("/keys")
async def create_key(body: KeyInput, request: Request):
    return request.app.state.panel.db.create_key(body.name)


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, body: KeyState, request: Request):
    if not request.app.state.panel.db.toggle_key(key_id, body.enabled):
        raise HTTPException(404, "密钥不存在")
    return {"ok": True}


@router.get("/config")
async def config(request: Request):
    return request.app.state.panel.config_view()


@router.patch("/config")
async def update_config(body: dict, request: Request):
    return await request.app.state.panel.update_config(body, request.app)


@router.get("/audit")
async def audit(request: Request, page: int = Query(1, ge=1, le=100000)):
    return {"rows": request.app.state.panel.db.audits(page), "page": page}


@router.get("/models")
async def models():
    from ..adapters.openai_chat import models_response
    return models_response()


@router.post("/test")
async def test_request(body: TestInput, request: Request):
    from ..adapters.openai_chat import build_canonical, sse_stream
    from ..main import _guard
    request.app.state.tokens.require_account()
    record = current.get()
    if record is not None:
        record["key_id"] = "panel-test"
    canonical = build_canonical({"model": body.model, "messages": [{"role": "user", "content": body.prompt}]})
    return StreamingResponse(_guard(sse_stream(request.app.state.client, canonical, True), "openai"),
                             media_type="text/event-stream")


@router.post("/chat")
async def chat(body: ChatInput, request: Request):
    from ..adapters.openai_chat import build_canonical, sse_stream
    from ..main import _guard
    request.app.state.tokens.require_account()
    record = current.get()
    if record is not None:
        record["key_id"] = "panel-chat"
    canonical = build_canonical({
        "model": body.model,
        "messages": [m.model_dump() for m in body.messages],
    })
    # mode 为空时跟随全局 glm_agent_mode 默认；指定则按请求覆盖。
    if body.mode is not None:
        canonical.agent_mode = body.mode == "agent"
    return StreamingResponse(_guard(sse_stream(request.app.state.client, canonical, True), "openai"),
                             media_type="text/event-stream")


@router.post("/browser-login")
async def start_login(body: LoginInput, request: Request):
    panel = request.app.state.panel
    return panel.logins.start(panel.auth.require(request)["csrf"], body.mode)


def owned_login(request, login_id):
    panel = request.app.state.panel
    return panel.logins.owned(login_id, panel.auth.require(request)["csrf"])


@router.get("/browser-login/{login_id}")
async def login_status(login_id: str, request: Request):
    return request.app.state.panel.logins.view(owned_login(request, login_id))


@router.get("/browser-login/{login_id}/image")
async def login_image(login_id: str, request: Request):
    session = owned_login(request, login_id)
    if not session.screenshot:
        raise HTTPException(404, "官方页面尚未就绪")
    return Response(session.screenshot, media_type="image/png")


@router.post("/browser-login/{login_id}/click")
async def click_login(login_id: str, body: ClickInput, request: Request):
    await request.app.state.panel.logins.click(owned_login(request, login_id), body.x, body.y)
    return {"ok": True}


@router.get("/browser-login/{login_id}/qr")
async def login_qr(login_id: str, request: Request):
    session = owned_login(request, login_id)
    if not session.qr_image:
        raise HTTPException(404, "官方二维码尚未就绪")
    return Response(session.qr_image, media_type="image/png")


@router.delete("/browser-login/{login_id}")
async def cancel_login(login_id: str, request: Request):
    await request.app.state.panel.logins.cancel(owned_login(request, login_id))
    return {"ok": True}
