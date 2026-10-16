"""上游 HTTP 客户端：并发闸门、流式请求、会话清理。

负责与 chatglm.cn 的实际网络交互，向上暴露 DeltaEvent 流。
并发用 asyncio.Semaphore 限流（上游对并发敏感，超限会触发限流），超限排队而非报错。
"""

from __future__ import annotations

import asyncio
import time
from ..panel.metrics import current
from ..adapters.prompt import estimate_tokens
from typing import AsyncIterator

import httpx

from ..config import Settings, get_settings
from ..errors import GatewayError
from .auth import build_headers
from .parser import StreamParser, status_finish_reason
from .protocol import build_chat_body
from .tokens import TokenManager
from ..adapters.internal import CanonicalRequest, CompletionResult, DeltaEvent


def _map_http_error(status: int, body: str) -> GatewayError:
    """把上游 HTTP 错误映射为内部错误。

    不解析响应体里的业务码（上游未稳定暴露其结构），只按状态码归类。
    """
    snippet = body[:300] if body else ""
    if status in (401, 403):
        return GatewayError(status, "invalid_api_key", f"上游鉴权失败（HTTP {status}）: {snippet}")
    if status == 429:
        return GatewayError(429, "rate_limit_error", f"上游限流（HTTP 429）: {snippet}")
    if 400 <= status < 500:
        return GatewayError(400, "invalid_request", f"上游拒绝请求（HTTP {status}）: {snippet}")
    return GatewayError(502, "upstream_error", f"上游错误（HTTP {status}）: {snippet}")


class UpstreamClient:
    """与上游对话接口交互的客户端。"""

    def __init__(self, settings: Settings | None = None, tokens: TokenManager | None = None) -> None:
        self._settings = settings or get_settings()
        self._tokens = tokens or TokenManager(self._settings)
        self._semaphore = asyncio.Semaphore(self._settings.glm_max_concurrency)

    async def stream(self, request: CanonicalRequest) -> AsyncIterator[DeltaEvent]:
        """执行一次上游对话，产出 DeltaEvent 流。

        生成器结束前会 yield 一个 kind="finish" 的事件；异常则向上抛出 GatewayError。
        """
        record = current.get()
        ascii_chars = non_ascii_chars = 0
        if record is not None:
            record["model"] = str(request.model)[:160]
            record["input_tokens"] = estimate_tokens(request.prompt)
        async with self._semaphore:
            access_token, _account_idx = await self._tokens.get_access_token()
            if record is not None:
                record["account"] = f"slot-{_account_idx + 1}"

            # agent 模式：chat_mode 固定 chat_agent、联网打开，端点与 Origin/Referer 切到 agentmore。
            # 每请求可覆盖；未覆盖时跟随全局配置 glm_agent_mode。
            agent_mode = request.agent_mode if request.agent_mode is not None else self._settings.glm_agent_mode
            if agent_mode:
                chat_mode = "chat_agent"
                is_networking = True
                stream_url = self._settings.agent_stream_url
                origin = self._settings.glm_agent_origin
                referer = origin.rstrip("/") + "/"
            else:
                chat_mode = request.chat_mode
                is_networking = False
                stream_url = self._settings.chat_stream_url
                origin = None
                referer = None

            body = build_chat_body(
                assistant_id=request.assistant_id,
                prompt=request.prompt,
                conversation_id="",
                chat_mode=chat_mode,
                is_networking=is_networking,
                reasoning_effort=request.reasoning_effort,
                selected_model=request.selected_model,
            )
            parser = StreamParser()
            conversation_id: str | None = None
            finish_reason = "stop"
            finished = False
            images: list[str] = []

            try:
                async with httpx.AsyncClient(
                    proxy=self._settings.proxy or None,
                    timeout=self._settings.glm_timeout,
                    follow_redirects=True,
                ) as client:
                    headers = build_headers(access_token, origin=origin, referer=referer)
                    async with client.stream(
                        "POST",
                        stream_url,
                        headers=headers,
                        json=body,
                    ) as resp:
                        if resp.status_code != 200:
                            text = await resp.aread()
                            raise _map_http_error(resp.status_code, text.decode("utf-8", "replace"))

                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            if line.strip() == "data: [DONE]":
                                finished = True
                            event = parser.parse_line(line)
                            if event is None:
                                continue
                            if event.conversation_id:
                                conversation_id = event.conversation_id
                            if event.images:
                                images.extend(url for url in event.images if url not in images)
                            ended, reason = status_finish_reason(event.status)
                            if ended:
                                finished = True
                                finish_reason = reason
                                # 结束状态不 break：上游可能在其后还有数据，继续读到流结束
                            if record is not None:
                                content = event.text + event.reasoning
                                if content and record["first_ms"] is None:
                                    record["first_ms"] = (time.time() - record["created"]) * 1000
                                ascii_chars += sum(ord(ch) < 128 for ch in content)
                                non_ascii_chars += sum(ord(ch) >= 128 for ch in content)
                                record["output_tokens"] = (ascii_chars + 3) // 4 + non_ascii_chars
                                record["images"] = len(images)
                            if event.text:
                                yield DeltaEvent(kind="text", text=event.text)
                            if event.reasoning:
                                yield DeltaEvent(kind="reasoning", reasoning=event.reasoning)
                if not finished:
                    raise GatewayError(502, "incomplete_stream", "上游连接结束但未收到完成标记")
            except httpx.TimeoutException:
                raise GatewayError(504, "upstream_timeout", "上游响应超时") from None
            except httpx.HTTPError:
                raise GatewayError(502, "upstream_network_error", "上游网络连接失败") from None
            finally:
                # 请求结束（无论成败）都尽力清理会话，保持无状态、避免服务端堆积。
                if conversation_id and self._settings.glm_delete_conversation:
                    asyncio.ensure_future(self._delete_conversation(conversation_id, request.assistant_id, access_token))

            yield DeltaEvent(kind="finish", finish_reason=finish_reason, images=images)

    async def complete(self, request: CanonicalRequest) -> CompletionResult:
        """聚合流式结果为完整结果（非流式路径使用）。"""
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = "stop"
        images: list[str] = []
        conversation_id: str | None = None
        async for event in self.stream(request):
            if event.kind == "text":
                text_parts.append(event.text)
            elif event.kind == "reasoning":
                reasoning_parts.append(event.reasoning)
            elif event.kind == "finish":
                finish_reason = event.finish_reason
                images = event.images
        return CompletionResult(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            finish_reason=finish_reason,
            images=images,
            conversation_id=conversation_id,
        )

    async def _delete_conversation(self, conversation_id: str, assistant_id: str, access_token: str) -> None:
        """删除上游会话。先试 DELETE，失败回退 POST。失败只记日志，不影响主流程。"""
        settings = self._settings
        try:
            async with httpx.AsyncClient(
                proxy=settings.proxy or None,
                timeout=settings.glm_timeout,
                follow_redirects=True,
            ) as client:
                headers = build_headers(access_token)

                # 端点 1：DELETE /backend-api/assistant/conversation/{id}
                url = f"{settings.glm_base_url}/backend-api/assistant/conversation/{conversation_id}"
                resp = await client.delete(url, headers=headers)
                if 200 <= resp.status_code < 300:
                    return

                # 端点 2（回退）：POST /backend-api/assistant/conversation/delete
                payload = {"conversation_id": conversation_id, "assistant_id": assistant_id}
                await client.post(settings.delete_conversation_url, headers=headers, json=payload)
        except Exception:
            # 会话清理是尽力而为，任何失败都不应影响主流程。
            pass
