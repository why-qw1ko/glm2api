"""OpenAI Responses API 适配器（/v1/responses）。

与 chat/completions 的主要差异：
- 输入用 input 字段（字符串或条目数组），instructions 单独前置
- 输出是 output[] 条目，而非 choices[].message
- 支持 previous_response_id 延续（内存态）
"""

from __future__ import annotations

import time
import uuid
from typing import AsyncIterator

from ..errors import GatewayError
from ..tools.parser import ToolCall, ToolStreamSplitter
from ..tools.protocol import serialize_tools_to_prompt
from ..upstream.client import UpstreamClient
from .internal import CanonicalRequest, CompletionResult
from .prompt import estimate_tokens, flatten_openai_messages, resolve_thinking_level


def _resp_id() -> str:
    return "resp_" + uuid.uuid4().hex


def _response_states() -> dict:
    """响应状态表（内存态）。key 为 previous_response_id，value 为延续用的 prompt 片段。

    简单实现：只保存上一轮的 output 文本，供下一轮作为上下文前缀。
    TTL 与上限清理保持轻量，避免无限增长。
    """
    return _RESPONSE_STATES


_RESPONSE_STATES: dict[str, tuple[str, float]] = {}


def _normalize_input(input_obj: object, instructions: str | None) -> str:
    """把 Responses 的 input 字段压平为 prompt 文本。"""
    messages: list[dict] = []
    if isinstance(input_obj, str):
        messages = [{"role": "user", "content": input_obj}]
    elif isinstance(input_obj, list):
        for item in input_obj:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            content = item.get("content")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        kind = block.get("type")
                        if kind in ("input_text", "output_text", "text"):
                            text = block.get("text")
                            if isinstance(text, str):
                                texts.append(text)
                messages.append({"role": role, "content": "\n".join(texts)})
    else:
        raise GatewayError(400, "invalid_input", "input 必须是字符串或条目数组")
    if not messages:
        raise GatewayError(400, "missing_input", "缺少 input 字段")
    return flatten_openai_messages(messages, instructions=instructions)


def _normalize_tools(tools: list[dict]) -> list[dict]:
    """Responses 工具格式归一化。支持 function 与 custom 两种客户端工具。"""
    normalized: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind not in ("function", "custom"):
            # hosted 工具（web_search/file_search/code_interpreter 等）接受但丢弃
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        normalized.append(
            {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}) or tool.get("input_schema", {}),
            }
        )
    return normalized


def build_canonical(body: dict) -> CanonicalRequest:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise GatewayError(400, "missing_model", "缺少 model 字段")
    from ..catalog import THINKING_LEVELS, resolve_model

    profile = resolve_model(model)
    instructions = body.get("instructions") if isinstance(body.get("instructions"), str) else None

    previous_id = body.get("previous_response_id")
    prompt = _normalize_input(body.get("input"), instructions)
    if isinstance(previous_id, str) and previous_id:
        state = _response_states().get(previous_id)
        if state is None:
            raise GatewayError(400, "previous_response_not_found", f"找不到 previous_response_id: {previous_id}")
        prompt = state[0] + "\n\n" + prompt

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
        tool_choice=body.get("tool_choice", "auto"),
        parallel_tool_calls=bool(body.get("parallel_tool_calls", True)),
    )


def _usage(prompt: str, text: str, reasoning: str) -> dict:
    return {
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": max(1, estimate_tokens(text + reasoning)),
        "total_tokens": estimate_tokens(prompt) + max(1, estimate_tokens(text + reasoning)),
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": estimate_tokens(reasoning)},
    }


def render_non_stream(result: CompletionResult, canonical: CanonicalRequest) -> dict:
    resp_id = _resp_id()
    output: list[dict] = []
    if result.reasoning:
        output.append(
            {
                "id": "rs_" + uuid.uuid4().hex,
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": result.reasoning}],
            }
        )
    if result.tool_calls:
        for call in result.tool_calls:
            output.append(
                {
                    "id": "fc_" + uuid.uuid4().hex,
                    "type": "function_call",
                    "call_id": "call_" + uuid.uuid4().hex,
                    "name": call.name,
                    "arguments": _json_compact(call.arguments),
                }
            )
    if result.text:
        output.append(
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": result.text, "annotations": []}],
            }
        )

    _response_states()[resp_id] = (result.text, time.time())
    _prune_states()

    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "model": canonical.model,
        "output": output,
        "parallel_tool_calls": canonical.parallel_tool_calls,
        "previous_response_id": None,
        "reasoning": {"effort": "medium" if canonical.chat_mode == "zero" else None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": _usage(canonical.prompt, result.text, result.reasoning),
        "metadata": {},
    }


def _json_compact(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _prune_states() -> None:
    now = time.time()
    for key in list(_RESPONSE_STATES.keys()):
        value, ts = _RESPONSE_STATES[key]
        if now - ts > 6 * 3600:
            del _RESPONSE_STATES[key]
    while len(_RESPONSE_STATES) > 128:
        oldest = min(_RESPONSE_STATES, key=lambda k: _RESPONSE_STATES[k][1])
        del _RESPONSE_STATES[oldest]


async def sse_stream(
    client: UpstreamClient,
    canonical: CanonicalRequest,
    model: str,
) -> AsyncIterator[str]:
    """Responses API 流式事件序列。"""
    resp_id = _resp_id()

    def emit(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {_json_compact(data)}\n\n"

    base = {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
    }
    yield emit("response.created", {"type": "response.created", "response": base})
    yield emit("response.in_progress", {"type": "response.in_progress", "response": base})

    msg_item_id = "msg_" + uuid.uuid4().hex
    output_index = 0
    yield emit(
        "response.output_item.added",
        {"type": "response.output_item.added", "output_index": output_index, "item": {"id": msg_item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}},
    )
    yield emit(
        "response.content_part.added",
        {"type": "response.content_part.added", "item_id": msg_item_id, "output_index": output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
    )

    splitter = ToolStreamSplitter(known_names=[t["name"] for t in (canonical.tools or [])])
    tool_calls: list[ToolCall] = []
    text_buf: list[str] = []
    reasoning_buf: list[str] = []
    reasoning_item = False

    async for event in client.stream(canonical):
        if event.kind == "reasoning" and event.reasoning:
            reasoning_buf.append(event.reasoning)
            if not reasoning_item:
                reasoning_item = True
                reasoning_id = "rs_" + uuid.uuid4().hex
                yield emit(
                    "response.output_item.added",
                    {"type": "response.output_item.added", "output_index": 1, "item": {"id": reasoning_id, "type": "reasoning", "summary": []}},
                )
                yield emit(
                    "response.reasoning_summary_part.added",
                    {"type": "response.reasoning_summary_part.added", "item_id": reasoning_id, "output_index": 1, "summary_index": 0, "part": {"type": "summary_text", "text": ""}},
                )
            yield emit(
                "response.reasoning_summary_text.delta",
                {"type": "response.reasoning_summary_text.delta", "item_id": reasoning_id, "output_index": 1, "summary_index": 0, "delta": event.reasoning},
            )
        elif event.kind == "text" and event.text:
            for kind, payload in splitter.feed(event.text):
                if kind == "text":
                    text_buf.append(payload)
                    yield emit(
                        "response.output_text.delta",
                        {"type": "response.output_text.delta", "item_id": msg_item_id, "output_index": output_index, "content_index": 0, "delta": payload},
                    )
                else:
                    tool_calls.append(payload)
                    for _chunk in _emit_function_call(emit, payload, len(tool_calls) - 1):
                        yield _chunk
        elif event.kind == "finish":
            pass

    for kind, payload in splitter.flush():
        if kind == "text":
            text_buf.append(payload)
            yield emit(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "item_id": msg_item_id, "output_index": output_index, "content_index": 0, "delta": payload},
            )
        else:
            tool_calls.append(payload)
            for _chunk in _emit_function_call(emit, payload, len(tool_calls) - 1):
                yield _chunk

    if reasoning_item:
        yield emit(
            "response.reasoning_summary_text.done",
            {"type": "response.reasoning_summary_text.done", "item_id": reasoning_id, "output_index": 1, "summary_index": 0, "text": "".join(reasoning_buf)},
        )
        yield emit("response.reasoning_summary_part.done", {"type": "response.reasoning_summary_part.done", "item_id": reasoning_id, "output_index": 1, "summary_index": 0, "part": {"type": "summary_text", "text": "".join(reasoning_buf)}})
        yield emit("response.output_item.done", {"type": "response.output_item.done", "output_index": 1, "item": {"id": reasoning_id, "type": "reasoning", "summary": [{"type": "summary_text", "text": "".join(reasoning_buf)}]}})

    yield emit(
        "response.output_text.done",
        {"type": "response.output_text.done", "item_id": msg_item_id, "output_index": output_index, "content_index": 0, "text": "".join(text_buf)},
    )
    yield emit("response.content_part.done", {"type": "response.content_part.done", "item_id": msg_item_id, "output_index": output_index, "content_index": 0, "part": {"type": "output_text", "text": "".join(text_buf), "annotations": []}})
    yield emit("response.output_item.done", {"type": "response.output_item.done", "output_index": output_index, "item": {"id": msg_item_id, "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "".join(text_buf), "annotations": []}]}})

    _response_states()[resp_id] = ("".join(text_buf), time.time())
    _prune_states()

    completed = dict(base)
    completed["status"] = "completed"
    completed["output"] = []
    yield emit("response.completed", {"type": "response.completed", "response": completed})


def _emit_function_call(emit, call: ToolCall, index: int) -> list[str]:
    item_id = "fc_" + uuid.uuid4().hex
    call_id = "call_" + uuid.uuid4().hex
    args = _json_compact(call.arguments)
    out = [
        emit(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": index + 2, "item": {"id": item_id, "type": "function_call", "status": "in_progress", "call_id": call_id, "name": call.name, "arguments": ""}},
        )
    ]
    step = 64
    for i in range(0, len(args), step):
        out.append(
            emit(
                "response.function_call_arguments.delta",
                {"type": "response.function_call_arguments.delta", "item_id": item_id, "output_index": index + 2, "delta": args[i : i + step]},
            )
        )
    out.append(
        emit(
            "response.function_call_arguments.done",
            {"type": "response.function_call_arguments.done", "item_id": item_id, "output_index": index + 2, "arguments": args},
        )
    )
    out.append(
        emit(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": index + 2, "item": {"id": item_id, "type": "function_call", "status": "completed", "call_id": call_id, "name": call.name, "arguments": args}},
        )
    )
    return out
