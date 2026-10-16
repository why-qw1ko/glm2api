"""prompt 构建与思考开关测试。"""

import pytest

from glmweb2api.adapters.prompt import (
    content_to_text,
    detect_reasoning,
    estimate_tokens,
    flatten_openai_messages,
)
from glmweb2api.errors import GatewayError


def test_content_to_text_string_and_array():
    assert content_to_text("hi") == "hi"
    assert content_to_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"


def test_content_to_text_rejects_image():
    with pytest.raises(GatewayError) as exc:
        content_to_text([{"type": "image_url", "image_url": {"url": "x"}}])
    assert exc.value.code == "unsupported_multimodal_input"


def test_flatten_single_user_shortcut():
    assert flatten_openai_messages([{"role": "user", "content": "你好"}]) == "你好"


def test_flatten_multiple_with_roles():
    out = flatten_openai_messages(
        [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答案"}]
    )
    assert "[user]" in out and "[assistant]" in out
    assert "next assistant turn" in out


def test_detect_reasoning():
    assert detect_reasoning("glm-4", {"thinking": True})
    assert detect_reasoning("reasoner-x", {})
    assert detect_reasoning("glm-4", {"deep_think": True})
    assert not detect_reasoning("glm-4", {})


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # 4 个 ASCII = 1
    assert estimate_tokens("你好") == 2  # 每个非 ASCII = 1
