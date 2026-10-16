"""FastAPI 应用装配：路由、鉴权、异常处理。

所有路由都做同一件事：解析请求体 -> adapter 翻译成 canonical -> 上游 -> adapter 渲染输出。
本文件只负责 HTTP 层的装配，协议逻辑都在 adapters/ 里。
"""

from __future__ import annotations

import json
import asyncio
import logging
import hmac
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from fastapi.exceptions import RequestValidationError
from .panel.auth import require_admin
from .panel.metrics import MetricsMiddleware, mark_error, current
from .panel.runtime import PanelRuntime
from .panel.login import LoginManager
from .panel.routes import router as panel_router, public as panel_public
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import anthropic, gemini, images, openai_chat, openai_responses
from .config import get_settings
from .credentials.store import TokenStore, mask_token
from .errors import GatewayError, anthropic_error_body, openai_error_body
from .upstream.client import UpstreamClient
from .upstream.tokens import TokenManager

logger = logging.getLogger("glmweb2api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    panel = PanelRuntime(settings)
    app.state.panel = panel
    store = TokenStore(settings.glm_token_file or None)
    tokens = TokenManager(settings, store=store)
    client = UpstreamClient(settings.model_copy(), tokens)
    app.state.settings = settings
    app.state.store = store
    app.state.tokens = tokens
    app.state.client = client
    logging.basicConfig(level=settings.log_level.upper())
    mode = f"{tokens.account_count} 个账号" if tokens.account_count else "等待官方账号登录"
    if not tokens.account_count:
        logger.info("尚未接入官方账号，请打开 /panel/ 登录；反代调用暂不可用")
    logger.info("GLMWEB2API 启动，上游=%s，凭据=%s", settings.glm_base_url, mode)
    panel.logins = LoginManager(app)
    panel.maintenance_task = asyncio.create_task(panel.maintain())
    logger.info("管理面板: /panel/；首次密码见数据目录 panel-bootstrap.txt，或设置 PANEL_PASSWORD")
    try:
        yield
    finally:
        await panel.close()


app = FastAPI(title="GLMWEB2API", lifespan=lifespan)
app.add_middleware(MetricsMiddleware)
app.include_router(panel_public)
app.include_router(panel_router)
app.mount("/panel/assets", StaticFiles(directory=Path(__file__).parent / "panel" / "static"), name="panel-assets")


@app.get("/panel", include_in_schema=False)
async def panel_redirect():
    return RedirectResponse("/panel/")


@app.get("/panel/", include_in_schema=False)
async def panel_index():
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).parent / "panel" / "static" / "index.html")


@app.middleware("http")
async def panel_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/panel", "/admin")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc):
    # 不把密码、Token 或非法配置中的敏感输入回显给调用者。
    return JSONResponse(status_code=422, content={"detail": "输入格式或范围不正确，请检查字段"})



def _check_server_key(request: Request) -> None:
    """可选的本服务访问控制：配置了 server_api_keys 时校验 Bearer。"""
    keys = get_settings().server_api_keys
    db = request.app.state.panel.db
    managed = db.keys()
    if not keys and not managed:
        request.app.state.tokens.require_account()
        return
    auth = request.headers.get("Authorization", "")
    provided = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not provided:
        provided = request.headers.get("x-api-key", "") or request.headers.get("x-goog-api-key", "")
    key_id = db.authenticate_key(provided) if provided else None
    if key_id is None:
        key_id = next((f"environment-{i + 1}" for i, key in enumerate(keys)
                       if hmac.compare_digest(provided.encode(), key.encode())), None)
    if not key_id:
        raise GatewayError(401, "invalid_api_key", "本服务的 API key 无效")
    request.app.state.tokens.require_account()
    record = current.get()
    if record is not None:
        record["key_id"] = key_id



async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise GatewayError(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise GatewayError(400, "invalid_json", "请求体必须是 JSON 对象")
    return body


def _is_anthropic_path(request: Request) -> bool:
    return request.url.path.startswith("/v1/messages")


def _error_frame(protocol: str, exc: GatewayError) -> str:
    """把异常渲染成对应协议的流式错误帧。"""
    if protocol == "anthropic":
        return f"event: error\ndata: {json.dumps(anthropic_error_body(exc), ensure_ascii=False)}\n\n"
    if protocol == "gemini":
        return json.dumps({"error": {"message": exc.message, "code": exc.code}}, ensure_ascii=False) + "\n"
    # openai / responses 用 data: {error} 后跟 [DONE]
    return "data: " + json.dumps(openai_error_body(exc), ensure_ascii=False) + "\n\n" + "data: [DONE]\n\n"


async def _guard(gen: AsyncIterator[str], protocol: str) -> AsyncIterator[str]:
    """包装流式生成器：中途抛 GatewayError 时改发协议错误帧，而不是让连接静默中断。"""
    try:
        async for chunk in gen:
            yield chunk
    except GatewayError as exc:
        mark_error(exc.code)
        logger.warning("流式请求失败: %s", exc.code)
        yield _error_frame(protocol, exc)


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError):
    mark_error(exc.code)
    body = anthropic_error_body(exc) if _is_anthropic_path(request) else openai_error_body(exc)
    return JSONResponse(status_code=exc.status, content=body)


# ---------------------------------------------------------------------------
# 健康检查与模型列表
# ---------------------------------------------------------------------------

@app.get("/healthz")
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request):
    _check_server_key(request)
    return openai_chat.models_response()


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = openai_chat.build_canonical(body)
    client: UpstreamClient = request.app.state.client

    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        gen = openai_chat.sse_stream(client, canonical, include_usage)
        return StreamingResponse(_guard(gen, "openai"), media_type="text/event-stream")

    result = await client.complete(canonical)
    result.tool_calls = _resolve_tool_calls(canonical, result)
    return openai_chat.render_non_stream(result, canonical)


def _resolve_tool_calls(canonical, result):
    """非流式路径：从完整文本中解析工具调用。"""
    if not canonical.tools:
        return None
    from .tools.parser import parse_tool_calls

    calls, cleaned = parse_tool_calls(result.text, [t["name"] for t in canonical.tools])
    if calls:
        result.text = cleaned
        return calls
    return None


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def responses(request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = openai_responses.build_canonical(body)
    client: UpstreamClient = request.app.state.client

    if body.get("stream"):
        gen = openai_responses.sse_stream(client, canonical, canonical.model)
        return StreamingResponse(_guard(gen, "openai"), media_type="text/event-stream")

    result = await client.complete(canonical)
    result.tool_calls = _resolve_tool_calls(canonical, result)
    return openai_responses.render_non_stream(result, canonical)


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def messages(request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = anthropic.build_canonical(body)
    client: UpstreamClient = request.app.state.client

    if body.get("stream"):
        gen = anthropic.sse_stream(client, canonical, canonical.model)
        return StreamingResponse(_guard(gen, "anthropic"), media_type="text/event-stream")

    result = await client.complete(canonical)
    result.tool_calls = _resolve_tool_calls(canonical, result)
    return anthropic.render_non_stream(result, canonical, canonical.model)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = anthropic.build_canonical(body)
    from .adapters.prompt import estimate_tokens

    return {"input_tokens": estimate_tokens(canonical.prompt)}


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

@app.post("/v1beta/models/{model}:generateContent")
async def generate_content(model: str, request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = gemini.build_canonical(model, body)
    client: UpstreamClient = request.app.state.client
    result = await client.complete(canonical)
    result.tool_calls = _resolve_tool_calls(canonical, result)
    return gemini.render_non_stream(result, canonical)


@app.post("/v1beta/models/{model}:streamGenerateContent")
async def stream_generate_content(model: str, request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = gemini.build_canonical(model, body)
    client: UpstreamClient = request.app.state.client
    gen = gemini.sse_stream(client, canonical)
    return StreamingResponse(_guard(gen, "gemini"), media_type="application/json")


# ---------------------------------------------------------------------------
# 图片生成
# ---------------------------------------------------------------------------

@app.post("/v1/images/generations")
async def image_generations(request: Request):
    _check_server_key(request)
    body = await _read_json(request)
    canonical = images.build_canonical(body)
    client: UpstreamClient = request.app.state.client
    return await images.generate(client, canonical)


# ---------------------------------------------------------------------------
# 管理：refresh_token 导入
# ---------------------------------------------------------------------------

@app.post("/admin/refresh-token")
async def add_refresh_token(request: Request):
    """手动导入一个 refresh_token（写入本地存储，重启后仍有效）。"""
    require_admin(request)
    body = await _read_json(request)
    token = body.get("refresh_token")
    if not isinstance(token, str) or not token.strip():
        raise GatewayError(400, "missing_refresh_token", "缺少 refresh_token 字段")
    await request.app.state.tokens.add(token.strip())
    return {"ok": True, "saved": mask_token(token.strip())}


@app.get("/admin/status")
async def admin_status(request: Request):
    require_admin(request)
    settings = get_settings()
    tokens = request.app.state.tokens
    return {
        "upstream": settings.glm_base_url,
        "mode": "账号模式" if tokens.account_count else "等待登录",
        "accounts": tokens.account_count,
        "concurrency": settings.glm_max_concurrency,
    }
