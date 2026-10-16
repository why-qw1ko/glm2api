"""模型目录解析测试。"""

from glmweb2api.catalog import list_default_models, resolve_model, suffix_to_chat_mode


def test_suffix_to_chat_mode():
    assert suffix_to_chat_mode("thinking") == "thinking"
    assert suffix_to_chat_mode("fast") == ""
    assert suffix_to_chat_mode("") == ""
    assert suffix_to_chat_mode("deep_research") == "deep_research"
    assert suffix_to_chat_mode("zero") == "zero"
    # 未知后缀透传
    assert suffix_to_chat_mode("mystery") == "mystery"


def test_resolve_assistant_id_with_suffix():
    p = resolve_model("65940acff94777010aa6b796:thinking")
    assert p.assistant_id == "65940acff94777010aa6b796"
    assert p.chat_mode == "thinking"


def test_resolve_bare_assistant_id_defaults_fast():
    p = resolve_model("65940acff94777010aa6b796")
    assert p.assistant_id == "65940acff94777010aa6b796"
    assert p.chat_mode == ""


def test_resolve_unknown_name_falls_back():
    from glmweb2api.config import DEFAULT_CHAT_ASSISTANT_ID

    p = resolve_model("glm-4-plus")
    assert p.assistant_id == DEFAULT_CHAT_ASSISTANT_ID
    assert p.chat_mode == ""


def test_resolve_friendly_names():
    from glmweb2api.config import DEFAULT_CHAT_ASSISTANT_ID

    p = resolve_model("glm-5.3")
    assert p.assistant_id == DEFAULT_CHAT_ASSISTANT_ID
    assert p.chat_mode == ""  # 快速（默认）
    assert p.reasoning_effort == "low"
    assert p.selected_model == "glm-5.3"
    assert p.name == "glm-5.3"

    p = resolve_model("glm-5.3-flash")
    assert p.selected_model == "glm-5.3-flash"
    assert p.chat_mode == ""


def test_resolve_thinking_level_suffixes():
    """模型名后缀 -fast/-deep/-max 选择思考强度。"""
    p = resolve_model("glm-5.3-deep")
    assert p.chat_mode == "thinking"  # 深度
    assert p.reasoning_effort == "high"
    assert p.selected_model == "glm-5.3"

    p = resolve_model("glm-5.3-max")
    assert p.chat_mode == "deep_thinking"  # 极致
    assert p.reasoning_effort == "max"

    p = resolve_model("glm-5.3-flash-max")
    assert p.chat_mode == "deep_thinking"
    assert p.reasoning_effort == "max"
    assert p.selected_model == "glm-5.3-flash"


def test_list_default_models_returns_friendly_names():
    models = list_default_models()
    assert models == ["glm-5.3", "glm-5.3-flash"]
    # 展示名不再是裸 assistant_id
    from glmweb2api.config import DEFAULT_CHAT_ASSISTANT_ID

    assert f"{DEFAULT_CHAT_ASSISTANT_ID}:thinking" not in models


def test_agent_stream_url_default():
    from glmweb2api.config import Settings

    # 显式禁用 .env，测试的是字段默认值本身，不依赖项目根目录的 .env 是否开启 agent 模式。
    s = Settings(_env_file=None)
    assert s.glm_agent_mode is False
    assert s.agent_stream_url == "https://agentmore.chatglm.cn/chatglm/agent-api/chat/stream"


def test_build_chat_body_agent_meta():
    """agent 模式请求体：chat_mode=chat_agent、联网打开，思考强度与模型不变。"""
    from glmweb2api.upstream.protocol import build_chat_body

    body = build_chat_body(
        assistant_id="65940acff94777010aa6b796",
        prompt="hi",
        chat_mode="chat_agent",
        is_networking=True,
        reasoning_effort="max",
        selected_model="glm-5.3",
    )
    meta = body["meta_data"]
    assert meta["chat_mode"] == "chat_agent"
    assert meta["is_networking"] is True
    assert meta["reasoning_effort"] == "max"
    assert meta["selected_model"] == "glm-5.3"
