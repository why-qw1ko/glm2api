"""协议 adapter 测试：canonical 构建与输出渲染（不联网）。"""

from glmweb2api.adapters import anthropic, openai_chat
from glmweb2api.adapters.internal import CompletionResult
from glmweb2api.errors import GatewayError
import pytest


def test_chat_build_canonical_basic():
    canonical = openai_chat.build_canonical(
        {"model": "glm-4", "messages": [{"role": "user", "content": "你好"}]}
    )
    assert canonical.prompt == "你好"
    assert canonical.chat_mode == ""  # 无思考要求


def test_chat_build_canonical_rejects_image():
    with pytest.raises(GatewayError) as exc:
        openai_chat.build_canonical(
            {
                "model": "glm-4",
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
                ],
            }
        )
    assert exc.value.code == "unsupported_multimodal_input"


def test_chat_build_canonical_tools_force_thinking():
    canonical = openai_chat.build_canonical(
        {
            "model": "glm-4",
            "messages": [{"role": "user", "content": "查天气"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        }
    )
    assert canonical.chat_mode == "thinking"  # 工具激活强制深度思考
    assert canonical.tools == [{"name": "get_weather", "description": "", "parameters": {}}]


def test_chat_reasoning_effort_selects_thinking_level():
    """请求体 reasoning_effort 显式选择思考强度，且显式选择优先于工具默认。"""
    canonical = openai_chat.build_canonical(
        {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "max"}
    )
    assert canonical.chat_mode == "deep_thinking"
    assert canonical.reasoning_effort == "max"
    assert canonical.selected_model == "glm-5.3"

    # 显式要求快速时，工具存在也不强制思考
    canonical = openai_chat.build_canonical(
        {
            "model": "glm-5.3-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "low",
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        }
    )
    assert canonical.chat_mode == ""
    assert canonical.reasoning_effort == "low"
    assert canonical.selected_model == "glm-5.3-flash"


def test_chat_render_non_stream_with_reasoning_and_tool():
    from glmweb2api.tools.parser import ToolCall

    canonical = openai_chat.build_canonical(
        {"model": "glm-4", "messages": [{"role": "user", "content": "hi"}]}
    )
    result = CompletionResult(
        text="",
        reasoning="想想",
        finish_reason="tool_calls",
        tool_calls=[ToolCall(name="get_weather", arguments={"city": "北京"})],
    )
    body = openai_chat.render_non_stream(result, canonical)
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert choice["message"]["reasoning_content"] == "想想"


def test_anthropic_build_canonical_and_render():
    canonical = anthropic.build_canonical(
        {
            "model": "glm-4",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
    )
    assert "hi" in canonical.prompt

    result = CompletionResult(text="你好", finish_reason="stop")
    body = anthropic.render_non_stream(result, canonical, "glm-4")
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"][-1]["type"] == "text"
    assert body["content"][-1]["text"] == "你好"
    assert body["stop_reason"] == "end_turn"


def test_anthropic_accepts_system_role_in_messages():
    """Claude Code 经自定义 base_url 接入时会把 role=system 塞进 messages，应折叠而非报 400。"""
    canonical = anthropic.build_canonical(
        {
            "model": "glm-4",
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": "你是编程助手"},
                {"role": "user", "content": "hi"},
            ],
        }
    )
    assert "[system]" in canonical.prompt
    assert "你是编程助手" in canonical.prompt
    assert "hi" in canonical.prompt


def test_anthropic_rejects_image_block():
    with pytest.raises(GatewayError) as exc:
        anthropic.build_canonical(
            {
                "model": "glm-4",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "x"}}]}
                ],
            }
        )
    assert exc.value.code == "unsupported_multimodal_input"
