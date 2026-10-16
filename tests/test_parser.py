"""上游 SSE 解析器测试。

覆盖：正文/思考链的累积全量 -> 增量 diff、conversation_id 提取、status 结束判定。
"""

import json

from glmweb2api.upstream.parser import StreamParser, status_finish_reason


def _line(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False)


def _event(parts: list, status: str = "", conv: str | None = None) -> dict:
    obj: dict = {"parts": parts, "status": status}
    if conv:
        obj["conversation_id"] = conv
    return obj


def test_parse_text_and_reasoning_delta():
    parser = StreamParser()
    # 第一个事件：logic_id=1 累积 "你好"，logic_id=2 累积 "思考"
    e1 = parser.parse_line(
        _line(
            _event(
                [
                    {"logic_id": "1", "content": [{"type": "text", "text": "你好"}]},
                    {"logic_id": "2", "content": [{"type": "think", "think": "思考"}]},
                ]
            )
        )
    )
    assert e1.text == "你好"
    assert e1.reasoning == "思考"

    # 第二个事件：logic_id=1 累积 "你好世界"（重复前文），只应产出增量 "世界"
    e2 = parser.parse_line(
        _line(
            _event(
                [
                    {"logic_id": "1", "content": [{"type": "text", "text": "你好世界"}]},
                ]
            )
        )
    )
    assert e2.text == "世界"
    assert e2.reasoning == ""


def test_mixed_delta_and_snapshot_frames():
    """实测帧序：流式过程为增量帧（if_increase_push=true），结束前推全量快照帧。

    快照帧不应导致文本重复，也不应破坏增量拼接。
    """
    parser = StreamParser()

    def push(text: str, increase: bool):
        return parser.parse_line(
            _line(
                {
                    "status": "",
                    "meta_data": {"if_increase_push": increase},
                    "parts": [{"logic_id": "L1", "content": [{"type": "text", "text": text}]}],
                }
            )
        )

    chunks = []
    for piece in ("<tool>{\"name", "\": \"B", "ash\"}}", " done</tool>"):
        e = push(piece, True)
        if e.text:
            chunks.append(e.text)
    # 快照帧重复推送全量文本，不应产生任何增量
    assert push("<tool>{\"name\": \"Bash\"}} done</tool>", False).text == ""
    assert push("<tool>{\"name\": \"Bash\"}} done</tool>", False).text == ""
    assert "".join(chunks) == "<tool>{\"name\": \"Bash\"}} done</tool>"


def test_snapshot_frame_corrects_state():
    """快照帧内容与已累积文本不一致时，以快照为准继续差分。"""
    parser = StreamParser()

    def frame(text: str, increase: bool):
        return parser.parse_line(
            _line(
                {
                    "status": "",
                    "meta_data": {"if_increase_push": increase},
                    "parts": [{"logic_id": "L1", "content": [{"type": "text", "text": text}]}],
                }
            )
        )

    assert frame("ABC", True).text == "ABC"
    # 上游快照纠正为 "ABCD"（长度变大），应只补出差量 "D"
    assert frame("ABCD", False).text == "D"
    # 再推增量 "E" 应正常追加
    assert frame("E", True).text == "E"


def test_code_block_wrapped_in_fence():
    parser = StreamParser()
    e = parser.parse_line(
        _line(_event([{"logic_id": "1", "content": [{"type": "code", "code": "print(1)"}]}]))
    )
    assert e.text == "```python\nprint(1)\n```"


def test_conversation_id_candidates():
    parser = StreamParser()
    e = parser.parse_line(_line({"status": "", "id": "conv_123"}))
    assert e.conversation_id == "conv_123"
    e2 = parser.parse_line(_line({"meta": {"conversation_id": "conv_456"}}))
    assert e2.conversation_id == "conv_456"


def test_status_finish_reason():
    assert status_finish_reason("finish") == (True, "stop")
    assert status_finish_reason("stop") == (True, "stop")
    assert status_finish_reason("intervene") == (True, "content_filter")
    assert status_finish_reason("") == (False, "")


def test_ignores_non_data_lines():
    parser = StreamParser()
    assert parser.parse_line("event: message") is None
    assert parser.parse_line("data: [DONE]") is None
    assert parser.parse_line("") is None
