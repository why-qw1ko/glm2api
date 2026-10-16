"""Gemini generateContent 协议适配器。

把 Google Gemini 风格的 :generateContent 与 :streamGenerateContent 翻译到 canonical 层。

注意：Gemini 模型名（如 gemini-2.0-flash）不会命中 assistant_id，
会回退到默认助手 —— 这是可接受的：客户端把本服务当作 Gemini 后端时，
model 名只是路由标记，真正的模型由 assistant_id 决定。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from ..catalog import THINKING_LEVELS, resolve_model
from ..errors import GatewayError
from ..tools.parser import ToolCall, ToolStreamSplitter
from ..tools.protocol import serialize_tools_to_prompt
from ..upstream.client import UpstreamClient
from .internal import CanonicalRequest, CompletionResult
from .prompt import estimate_tokens, resolve_thinking_level


def _system_text(instruction: object) -> str:
    if instruction is None:
        return ""
    parts = instruction.get("parts") if isinstance(instruction, dict) else None
    if isinstance(parts, list):
        return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return ""


def _contents_to_prompt(contents: list[dict], instructions: str) -> str:
    """把 Gemini contents 压平为纯文本。role 用 user/model 两态。"""
    parts: list[str] = []
    if instructions:
        parts.append(f"[system]\n{instructions}")
    for item in contents or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "user")
        title = "assistant" if role == "model" else "user"
        texts = []
        for part in item.get("parts", []) or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if texts:
            parts.append(f"[{title}]\n" + "\n".join(texts))
    if not parts:
        raise GatewayError(400, "missing_contents", "缺少 contents 字段")
    parts.append("[end conversation history]\nGenerate only the next assistant turn.")
    return "\n\n".join(parts)


def _normalize_tools(tools: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        decls = tool.get("functionDeclarations") or []
        for decl in decls:
            if isinstance(decl, dict) and isinstance(decl.get("name"), str):
                normalized.append(
                    {
                        "name": decl["name"],
                        "description": decl.get("description", ""),
                        "parameters": decl.get("parameters", {}),
                    }
                )
    return normalized


def build_canonical(model: str, body: dict) -> CanonicalRequest:
    profile = resolve_model(model)
    instructions = _system_text(body.get("systemInstruction"))
    prompt = _contents_to_prompt(body.get("contents") or [], instructions)

    tools = _normalize_tools(body.get("tools") or [])
    if tools:
        prompt = prompt + "\n\n" + serialize_tools_to_prompt(tools)

    level = resolve_thinking_level(model, body) or ("deep" if tools else None)
    if level:
        chat_mode, reasoning_effort = THINKING_LEVELS[level]
    else:
        chat_mode, reasoning_effort = profile.chat_mode, profile.reasoning_effort

    return CanonicalRequest(
        prompt=prompt,
        assistant_id=profile.assistant_id,
        chat_mode=chat_mode,
        reasoning_effort=reasoning_effort,
        selected_model=profile.selected_model,
        model=model,
        tools=tools if tools else None,
        tool_choice="auto",
        parallel_tool_calls=True,
    )


def _finish_reason_mapping(reason: str) -> str:
    mapping = {
        "content_filter": "SAFETY",
        "length": "MAX_TOKENS",
        "max_tokens": "MAX_TOKENS",
    }
    return mapping.get(reason, "STOP")


def render_non_stream(result: CompletionResult, canonical: CanonicalRequest) -> dict:
    parts: list[dict] = []
    if result.reasoning:
        # Gemini 无独立思考通道，思考内容并入 text（前面加标记便于识别）
        parts.append({"text": result.reasoning})
    if result.text:
        parts.append({"text": result.text})
    for call in result.tool_calls or []:
        parts.append(
            {
                "functionCall": {
                    "name": call.name,
                    "args": call.arguments,
                }
            }
        )
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": _finish_reason_mapping(result.finish_reason),
            }
        ],
        "usageMetadata": {
            "promptTokenCount": estimate_tokens(canonical.prompt),
            "candidatesTokenCount": max(1, estimate_tokens(result.text + result.reasoning)),
            "totalTokenCount": estimate_tokens(canonical.prompt) + max(1, estimate_tokens(result.text + result.reasoning)),
        },
    }


async def sse_stream(
    client: UpstreamClient,
    canonical: CanonicalRequest,
) -> AsyncIterator[str]:
    """Gemini streamGenerateContent：产出换行分隔的 JSON 对象流。

    注意 Gemini 流不是 SSE（无 data: 前缀），而是裸 JSON 对象逐行返回。
    """
    splitter = ToolStreamSplitter(known_names=[t["name"] for t in (canonical.tools or [])])
    tool_calls: list[ToolCall] = []
    reasoning_buf: list[str] = []
    text_buf: list[str] = []

    def chunk(parts: list[dict], finish_reason: str | None = None) -> str:
        candidate = {"content": {"role": "model", "parts": parts}}
        if finish_reason is not None:
            candidate["finishReason"] = finish_reason
        return json.dumps({"candidates": [candidate]}, ensure_ascii=False) + "\n"

    async for event in client.stream(canonical):
        if event.kind == "reasoning" and event.reasoning:
            reasoning_buf.append(event.reasoning)
            yield chunk([{"text": event.reasoning}])
        elif event.kind == "text" and event.text:
            for kind, payload in splitter.feed(event.text):
                if kind == "text":
                    text_buf.append(payload)
                    yield chunk([{"text": payload}])
                else:
                    tool_calls.append(payload)
                    yield chunk([{"functionCall": {"name": payload.name, "args": payload.arguments}}])
        elif event.kind == "finish":
            pass

    for kind, payload in splitter.flush():
        if kind == "text":
            text_buf.append(payload)
            yield chunk([{"text": payload}])
        else:
            tool_calls.append(payload)
            yield chunk([{"functionCall": {"name": payload.name, "args": payload.arguments}}])

    yield chunk([], finish_reason=_finish_reason_mapping("stop"))
