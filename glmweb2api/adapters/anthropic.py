"""Anthropic Messages 协议适配器。

把 POST /v1/messages 与 POST /v1/messages/count_tokens 翻译到 canonical 层。

Anthropic 的流式事件序列与 OpenAI 完全不同，这里按 Anthropic 的
SSE 帧格式（event:/data:）与 content_block 生命周期逐段构造。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import AsyncIterator

from ..errors import GatewayError
from ..tools.parser import ToolCall, ToolStreamSplitter
from ..tools.protocol import serialize_tools_to_prompt
from ..upstream.client import UpstreamClient
from .internal import CanonicalRequest, CompletionResult
from .prompt import estimate_tokens, resolve_thinking_level


def _message_id() -> str:
    return "msg_" + uuid.uuid4().hex


def _tool_use_id() -> str:
    return "toolu_" + uuid.uuid4().hex


def _reasoning_signature(reasoning: str) -> str:
    """为思考块生成一个确定性的"签名"。

    注意：这不是 Anthropic 真实的 Ed25519 链式签名，也无法被客户端校验；
    它只是对思考内容做的一次确定性哈希，加本地前缀标记，满足协议字段的占位要求。
    客户端（如 Claude Code）对本地代理的签名不校验。
    """
    digest = hashlib.sha256(reasoning.encode("utf-8")).hexdigest()
    return "glmweb2api-" + digest


def _system_text(system: object) -> str:
    """把 system 字段（字符串或 block 数组）折叠为文本。"""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _anthropic_to_prompt(messages: list[dict], instructions: str) -> str:
    """把 Anthropic messages 压平为带角色前缀的纯文本。

    - text 块累积为正文
    - tool_use 块渲染为"调用工具 ... 入参: ..."
    - tool_result 块渲染为"工具结果 ..."（is_error 时加 Error 前缀）
    - thinking/redacted_thinking 块丢弃（思考是传输上下文，本地不重放）
    - image/document/search_result 块报 400（本服务只支持文本）
    """
    parts: list[str] = []
    if instructions:
        parts.append(f"[system]\n{instructions}")

    for msg in messages:
        role = msg.get("role")
        # 官方 Anthropic API 的 system 走独立字段，但 Claude Code 等客户端经
        # 自定义 base_url 接入时常把 role=system 塞进 messages，这里宽容接受。
        if role not in ("system", "user", "assistant"):
            raise GatewayError(400, "invalid_role", f"不支持的消息角色: {role}")
        content = msg.get("content")
        rendered: list[str] = []

        if isinstance(content, str) or content is None:
            text = content or ""
            if text:
                rendered.append(text)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        rendered.append(text)
                elif block_type == "thinking" or block_type == "redacted_thinking":
                    # 思考块是传输上下文，本地不重放也不校验
                    continue
                elif block_type == "tool_use":
                    name = block.get("name")
                    if not isinstance(name, str):
                        raise GatewayError(400, "invalid_tool_use", "tool_use 块缺少 name")
                    rendered.append(
                        f"调用工具 {name} (tool_use_id={block.get('id', '')}) "
                        f"入参: {json.dumps(block.get('input', {}), ensure_ascii=False)}"
                    )
                elif block_type == "tool_result":
                    tool_id = block.get("tool_use_id")
                    if not isinstance(tool_id, str):
                        raise GatewayError(400, "invalid_tool_result", "tool_result 块缺少 tool_use_id")
                    prefix = "Error: " if block.get("is_error") else ""
                    content_text = _block_content_to_text(block.get("content"))
                    rendered.append(f"工具结果 (tool_use_id={tool_id}): {prefix}{content_text}")
                elif block_type in ("image", "document", "search_result"):
                    raise GatewayError(
                        400,
                        "unsupported_multimodal_input",
                        "该服务当前只支持文本输入，不支持图片或文档。",
                    )
                else:
                    # 未知块保真为文本
                    rendered.append(f"Anthropic content block (type={block_type}): {json.dumps(block, ensure_ascii=False)}")
        if not rendered:
            continue
        parts.append(f"[{role}]\n" + "\n".join(rendered))

    parts.append("[end conversation history]\nGenerate only the next assistant turn.")
    return "\n\n".join(parts)


def _block_content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)
    return ""


def _normalize_tools(tools: list[dict]) -> list[dict]:
    """把 Anthropic 工具格式归一化为 [{name, description, parameters}]。"""
    normalized: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            raise GatewayError(400, "invalid_tool", "工具缺少 name")
        normalized.append(
            {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            }
        )
    return normalized


def _resolve_tool_choice(tool_choice: object, known_names: list[str]) -> str:
    if tool_choice is None:
        return "auto"
    if not isinstance(tool_choice, dict):
        raise GatewayError(400, "invalid_tool_choice", "tool_choice 必须是对象")
    kind = tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = tool_choice.get("name")
        if name not in known_names:
            raise GatewayError(400, "invalid_tool_choice", f"tool_choice 指定的工具不存在: {name}")
        return "forced"
    raise GatewayError(400, "invalid_tool_choice", f"无效的 tool_choice type: {kind}")


def build_canonical(body: dict) -> CanonicalRequest:
    """把 /v1/messages 请求体翻译为 CanonicalRequest。"""
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise GatewayError(400, "missing_model", "缺少 model 字段")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "missing_messages", "缺少 messages 字段")

    from ..catalog import THINKING_LEVELS, resolve_model

    profile = resolve_model(model)
    instructions = _system_text(body.get("system"))
    tools = _normalize_tools(body.get("tools") or [])
    known_names = [t["name"] for t in tools]
    tool_choice = _resolve_tool_choice(body.get("tool_choice"), known_names)
    parallel = not bool(body.get("disable_parallel_tool_use", False))

    prompt = _anthropic_to_prompt(messages, instructions)
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
        tool_choice=tool_choice,
        parallel_tool_calls=parallel,
    )


def _anthropic_stop_reason(finish_reason: str) -> str:
    mapping = {
        "tool_use": "tool_use",
        "length": "max_tokens",
        "max_tokens": "max_tokens",
        "stop_sequence": "stop_sequence",
        "refusal": "refusal",
    }
    return mapping.get(finish_reason, "end_turn")


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def render_non_stream(result: CompletionResult, canonical: CanonicalRequest, model: str) -> dict:
    """构造非流式 /v1/messages 响应体。"""
    content: list[dict] = []
    if result.reasoning:
        content.append(
            {"type": "thinking", "thinking": result.reasoning, "signature": _reasoning_signature(result.reasoning)}
        )
    if result.tool_calls:
        for call in result.tool_calls:
            content.append(
                {"type": "tool_use", "id": _tool_use_id(), "name": call.name, "input": call.arguments}
            )
    if result.text or not result.tool_calls:
        content.append({"type": "text", "text": result.text})

    stop_reason = _anthropic_stop_reason(result.finish_reason)
    if result.tool_calls:
        stop_reason = "tool_use"

    return {
        "id": _message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _anthropic_usage(canonical.prompt, result.text, result.reasoning),
    }


def _anthropic_usage(prompt: str, text: str, reasoning: str) -> dict:
    return {
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": max(1, estimate_tokens(text + reasoning)),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


async def sse_stream(
    client: UpstreamClient,
    canonical: CanonicalRequest,
    model: str,
) -> AsyncIterator[str]:
    """把上游 DeltaEvent 流渲染成 Anthropic 的 SSE 文本流。

    内容块索引单调递增，切换块类型前先收口上一个块。这样工具调用
    （tool_use 块）不会再复用 thinking/text 的旧索引，避免 Claude Code
    因索引回跳或块未闭合而报 "response stream was malformed"。
    """
    message_id = _message_id()
    splitter = ToolStreamSplitter(known_names=[t["name"] for t in (canonical.tools or [])])
    tool_calls: list[ToolCall] = []
    reasoning_buf: list[str] = []
    text_buf: list[str] = []

    # message_start
    yield _event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": _anthropic_usage(canonical.prompt, "", ""),
            },
        },
    )

    content_index = 0               # 下一个待使用的块索引
    block_type: str | None = None   # 当前打开的块类型；None 表示没有块打开

    def close_block() -> str:
        """收口当前打开的块并推进索引。"""
        nonlocal content_index, block_type
        if block_type is None:
            return ""
        out = ""
        if block_type == "thinking":
            reasoning = "".join(reasoning_buf)
            out += _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": content_index,
                    "delta": {"type": "signature_delta", "signature": _reasoning_signature(reasoning)},
                },
            )
        out += _event("content_block_stop", {"type": "content_block_stop", "index": content_index})
        content_index += 1
        block_type = None
        return out

    def open_block(new_type: str, start_payload: dict) -> str:
        """切换到 new_type 块；若已是该类型则复用，否则先收口再新开。"""
        nonlocal block_type
        if block_type == new_type:
            return ""
        out = close_block()
        out += _event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": content_index,
                "content_block": start_payload,
            },
        )
        block_type = new_type
        return out

    def emit_tool(call: ToolCall) -> str:
        """把单个工具调用渲染为独立的 tool_use 块；先收口上一个块。"""
        nonlocal block_type
        args = json.dumps(call.arguments, ensure_ascii=False)
        out = close_block()
        out += _event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": content_index,
                "content_block": {
                    "type": "tool_use",
                    "id": _tool_use_id(),
                    "name": call.name,
                    "input": {},
                },
            },
        )
        block_type = "tool_use"
        step = 64
        for i in range(0, len(args), step):
            out += _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": content_index,
                    "delta": {"type": "input_json_delta", "partial_json": args[i : i + step]},
                },
            )
        return out

    async for event in client.stream(canonical):
        if event.kind == "reasoning" and event.reasoning:
            yield open_block("thinking", {"type": "thinking", "thinking": "", "signature": ""})
            reasoning_buf.append(event.reasoning)
            yield _event(
                "content_block_delta",
                {"type": "content_block_delta", "index": content_index, "delta": {"type": "thinking_delta", "thinking": event.reasoning}},
            )
        elif event.kind == "text" and event.text:
            for kind, payload in splitter.feed(event.text):
                if kind == "text":
                    yield open_block("text", {"type": "text", "text": ""})
                    text_buf.append(payload)
                    yield _event(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": content_index, "delta": {"type": "text_delta", "text": payload}},
                    )
                else:
                    tool_calls.append(payload)
                    yield emit_tool(payload)
        elif event.kind == "finish":
            pass  # 结束在循环后统一处理

    # 收尾：flush 残余工具块 / 正文
    for kind, payload in splitter.flush():
        if kind == "text":
            yield open_block("text", {"type": "text", "text": ""})
            text_buf.append(payload)
            yield _event(
                "content_block_delta",
                {"type": "content_block_delta", "index": content_index, "delta": {"type": "text_delta", "text": payload}},
            )
        else:
            tool_calls.append(payload)
            yield emit_tool(payload)

    yield close_block()

    stop_reason = _anthropic_stop_reason("stop")
    if tool_calls:
        stop_reason = "tool_use"

    yield _event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": max(1, estimate_tokens("".join(text_buf) + "".join(reasoning_buf)))},
        },
    )
    yield _event("message_stop", {"type": "message_stop"})
