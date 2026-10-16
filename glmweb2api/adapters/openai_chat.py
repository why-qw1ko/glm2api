"""OpenAI Chat Completions 协议适配器。

把 POST /v1/chat/completions 与 GET /v1/models 翻译到 canonical 层。

注意几点（源自对上游行为的归纳）：
- 上游不接受 temperature/top_p/stop 等采样参数，只能静默忽略
- max_tokens 不传上游（上游请求体没有该字段），上游长度不可控
- 图片输入明确报 400（本服务当前只支持文本）
"""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator

from ..catalog import THINKING_LEVELS, list_default_models, resolve_model
from ..errors import GatewayError
from ..tools.parser import ToolCall, ToolStreamSplitter
from ..tools.protocol import append_output_format, serialize_tools_to_prompt
from ..upstream.client import UpstreamClient
from .internal import CanonicalRequest, CompletionResult
from .prompt import estimate_tokens, flatten_openai_messages, resolve_thinking_level


def _chat_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def normalize_tools(tools: list[dict]) -> list[dict]:
    """把 OpenAI 格式的 tools 归一化为 [{name, description, parameters}]。

    只接受 type=function 的工具；其他类型明确报错。
    """
    normalized: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind != "function":
            raise GatewayError(400, "unsupported_tool_type", f"不支持的工具类型: {kind}")
        fn = tool.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise GatewayError(400, "invalid_tool", "工具缺少 name")
        normalized.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        )
    return normalized


def resolve_tool_choice(tool_choice: object, known_names: list[str]) -> str:
    """把 tool_choice 归一化为 auto/required/none/forced。"""
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto"
        if tool_choice == "required":
            return "required"
        if tool_choice == "none":
            return "none"
        raise GatewayError(400, "invalid_tool_choice", f"无效的 tool_choice: {tool_choice}")
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name")
        if name not in known_names:
            raise GatewayError(400, "invalid_tool_choice", f"tool_choice 指定的工具不存在: {name}")
        return "forced"
    raise GatewayError(400, "invalid_tool_choice", "tool_choice 必须是字符串或对象")


def build_canonical(body: dict) -> CanonicalRequest:
    """把 chat/completions 请求体翻译为 CanonicalRequest。"""
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise GatewayError(400, "missing_model", "缺少 model 字段")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "missing_messages", "缺少 messages 字段")

    # n 只支持 1（上游无多候选能力）
    n = body.get("n", 1)
    if n != 1:
        raise GatewayError(400, "unsupported_n", "仅支持 n=1")

    profile = resolve_model(model)
    tools = normalize_tools(body.get("tools") or [])
    known_names = [t["name"] for t in tools]
    tool_choice = resolve_tool_choice(body.get("tool_choice"), known_names)
    parallel = bool(body.get("parallel_tool_calls", True))

    instructions = None
    system = body.get("system")
    if isinstance(system, str):
        instructions = system

    prompt = flatten_openai_messages(messages, instructions=instructions)
    if tools:
        prompt = prompt + "\n\n" + serialize_tools_to_prompt(tools)
    prompt = append_output_format(prompt, body.get("response_format"))

    # 思考强度：客户端显式选择优先；未选择时工具调用强制深度（满足 Claude Code 的 planning UI）
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


# ---------------------------------------------------------------------------
# 输出渲染
# ---------------------------------------------------------------------------

def _usage(prompt: str, content: str, reasoning: str) -> dict:
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(content + reasoning)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": estimate_tokens(reasoning)},
    }


def render_non_stream(result: CompletionResult, canonical: CanonicalRequest) -> dict:
    """构造非流式 chat/completions 响应体。"""
    tool_calls = _render_tool_calls(result.tool_calls) if result.tool_calls else None
    content = result.text if not tool_calls else None
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if result.reasoning:
        message["reasoning_content"] = result.reasoning
    return {
        "id": _chat_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": canonical.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": _usage(canonical.prompt, result.text, result.reasoning),
    }


def _render_tool_calls(calls: list[ToolCall]) -> list[dict]:
    rendered: list[dict] = []
    for call in calls:
        rendered.append(
            {
                "id": "call_" + uuid.uuid4().hex,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
        )
    return rendered


def _chunk(chat_id: str, model: str, delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


async def sse_stream(
    client: UpstreamClient,
    canonical: CanonicalRequest,
    include_usage: bool,
) -> AsyncIterator[str]:
    """把上游 DeltaEvent 流渲染成 OpenAI chat/completions 的 SSE 文本流。

    产出 "data: {...}\\n\\n" 字符串，由 FastAPI 直接透传。
    """
    chat_id = _chat_id()
    model = canonical.model

    def emit(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    # 首块：只带 role，宣告助手开始
    yield emit(_chunk(chat_id, model, {"role": "assistant"}))

    splitter = ToolStreamSplitter(known_names=[t["name"] for t in (canonical.tools or [])])
    tool_calls: list[ToolCall] = []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_open = False
    reasoning_closed = False
    finish_reason = "stop"

    async for event in client.stream(canonical):
        if event.kind == "reasoning" and event.reasoning:
            reasoning_parts.append(event.reasoning)
            if not reasoning_open:
                # 首段思考链前加 <think> 标记
                yield emit(_chunk(chat_id, model, {"reasoning_content": "<think>\n"}))
                reasoning_open = True
            yield emit(_chunk(chat_id, model, {"reasoning_content": event.reasoning}))
        elif event.kind == "text" and event.text:
            text_parts.append(event.text)
            if reasoning_open and not reasoning_closed:
                yield emit(_chunk(chat_id, model, {"reasoning_content": "\n</think>"}))
                reasoning_closed = True
            for kind, payload in splitter.feed(event.text):
                if kind == "text":
                    yield emit(_chunk(chat_id, model, {"content": payload}))
                else:
                    tool_calls.append(payload)
                    for chunk in _tool_call_chunks(chat_id, model, payload, len(tool_calls) - 1):
                        yield emit(chunk)
        elif event.kind == "finish":
            finish_reason = event.finish_reason

    # 收尾：处理缓冲区内残余内容
    for kind, payload in splitter.flush():
        if kind == "text":
            yield emit(_chunk(chat_id, model, {"content": payload}))
        else:
            tool_calls.append(payload)
            for chunk in _tool_call_chunks(chat_id, model, payload, len(tool_calls) - 1):
                yield emit(chunk)

    if tool_calls:
        finish_reason = "tool_calls"

    yield emit(_chunk(chat_id, model, {}, finish_reason=finish_reason))
    if include_usage:
        # usage 块放在空 choices 里（OpenAI 约定）
        usage_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": _usage(canonical.prompt, "".join(text_parts), "".join(reasoning_parts)),
        }
        yield emit(usage_chunk)
    yield "data: [DONE]\n\n"


def _tool_call_chunks(chat_id: str, model: str, call: ToolCall, index: int) -> list[dict]:
    """把一次工具调用拆成 OpenAI 流式 tool_calls 块序列。"""
    arguments = json.dumps(call.arguments, ensure_ascii=False)
    first = _chunk(
        chat_id,
        model,
        {
            "tool_calls": [
                {
                    "index": index,
                    "id": "call_" + uuid.uuid4().hex,
                    "type": "function",
                    "function": {"name": call.name, "arguments": ""},
                }
            ]
        },
    )
    chunks = [first]
    # 参数按 96 字符切片，与正文切片粒度一致
    step = 96
    for i in range(0, len(arguments), step):
        chunks.append(
            _chunk(
                chat_id,
                model,
                {"tool_calls": [{"index": index, "function": {"arguments": arguments[i : i + step]}}]},
            )
        )
    return chunks


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------

def models_response() -> dict:
    """GET /v1/models 响应体。

    v1 返回内置默认模型列表（assistant_id:后缀 形式）。
    自定义模型目录后续可扩展为从配置文件读取。
    """
    models = list_default_models()
    data = [
        {"id": name, "object": "model", "created": 0, "owned_by": "glm"}
        for name in models
    ]
    return {"object": "list", "data": data}
