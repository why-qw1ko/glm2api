"""工具桥测试：契约序列化与响应解析。"""

from glmweb2api.tools.parser import ToolStreamSplitter, parse_tool_calls
from glmweb2api.tools.protocol import serialize_tools_to_prompt

TOOLS = [
    {"name": "get_weather", "description": "查询天气", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}
]


def test_serialize_tools_contains_contract():
    text = serialize_tools_to_prompt(TOOLS)
    assert "<tool>" in text
    assert "get_weather" in text
    assert "TOOL USE INSTRUCTIONS" in text


def test_parse_single_tool_call():
    calls, rest = parse_tool_calls(
        '好的，我来查一下。\n<tool>{"name": "get_weather", "arguments": {"city": "北京"}}</tool>',
        ["get_weather"],
    )
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "北京"}
    assert "好的" in rest


def test_parse_fuzzy_name_match():
    calls, _ = parse_tool_calls(
        '<tool>{"name": "get_Weather", "arguments": {}}</tool>',
        ["get_weather"],
    )
    assert len(calls) == 1
    assert calls[0].name == "get_weather"


def test_parse_loose_json_repair():
    # 末尾多余逗号 + 单引号，应能修复解析
    calls, _ = parse_tool_calls(
        "<tool>{'name': 'get_weather', 'arguments': {'city': '北京',},}</tool>",
        ["get_weather"],
    )
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "北京"}


def test_stream_splitter_incremental():
    splitter = ToolStreamSplitter(known_names=["get_weather"])
    # 工具块跨片到达
    items = splitter.feed("前面正文 <tool>{")
    assert [k for k, _ in items] == ["text"]  # 只 flush 正文
    items = splitter.feed('"name": "get_weather", "arguments": {"city": "北京"}}</tool> 后面')
    kinds = [k for k, _ in items]
    assert "tool" in kinds
    tail = splitter.flush()
    assert any(k == "text" and "后面" in v for k, v in tail)
