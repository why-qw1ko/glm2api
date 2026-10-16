"""上游 SSE 响应解析。

上游对话接口始终返回 SSE 流（即使客户端要求非流式）。本模块把原始的
"data: {json}" 行解析成结构化的增量事件，供各 adapter 消费。

解析要点（均来自对上游行为的实测归纳，见项目说明文档）：
- 只处理 data: 前缀的行；[DONE] 结束；空 data 跳过
- 每行 JSON 的 parts[] 含多个内容块，按 logic_id 归组
- meta_data.if_increase_push 标志决定内容语义：
    true  -> 本帧 text/think 是"增量"，追加到已累积文本之后
    false -> 本帧 text/think 是"全量快照"，整体替换已累积文本
    （实测两种帧都会出现：流式过程中是增量帧，结束前会再推 1-2 帧全量快照）
- 增量 = 当前全量状态 - 已发送长度（按字符长度切），快照帧若与已发内容一致则不重复发出
- type 决定内容去向：text/execution_output/code -> 正文；think -> 思考链；image -> 丢弃
- status 字段标记结束：finish/stop 正常，intervene 表示被内容过滤
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class StreamEvent:
    """一次上游事件（解析后的单行 data）。

    字段：
      conversation_id: 本行携带的会话 id（可能为 None）
      status:         事件状态，空串表示尚未结束
      text:           本次新增的正文增量
      reasoning:      本次新增的思考链增量
      images:         本行出现的图片 URL 列表（生图场景）
      raw:            原始 JSON 对象（供生图等特殊逻辑按需取用）
    """

    conversation_id: str | None = None
    status: str = ""
    text: str = ""
    reasoning: str = ""
    images: list[str] = field(default_factory=list)
    raw: dict | None = None


def _find_conversation_id(obj: dict) -> str | None:
    """按候选字段顺序识别会话 id。"""
    for key in ("conversation_id", "conv_id", "id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    meta = obj.get("meta")
    if isinstance(meta, dict):
        for key in ("conversation_id", "conv_id"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_images(obj: dict) -> list[str]:
    """从结束事件里收集图片 URL。生图场景图片出现在 status=finish 的事件里。

    上游把图片信息放在多层嵌套结构中，字段名在不同版本间不完全稳定，
    因此这里用递归扫描兜底，凡是值是 http(s) URL 且看起来像图片的字符串都收集进来。
    """
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and key.lower() in {"url", "image", "image_url"}:
                    if value.startswith(("http://", "https://")):
                        if value not in found:
                            found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


class StreamParser:
    """把 SSE 行流解析为 StreamEvent 流的有状态解析器。

    状态（每 logic_id 的全量文本 + 已发送长度）跨事件保持，用于把上游
    "增量帧 + 快照帧"的混合推送转成干净的增量流。
    """

    def __init__(self) -> None:
        # logic_id -> 当前全量正文 / 思考链（已按帧语义合并）
        self._text_state: dict[str, str] = {}
        self._reasoning_state: dict[str, str] = {}
        # logic_id -> 已发出的正文 / 思考链长度（用于差分出增量）
        self._text_sent: dict[str, int] = {}
        self._reasoning_sent: dict[str, int] = {}
        # 保持 logic_id 首次出现顺序，保证增量按稳定顺序发出
        self._order: list[str] = []

    def parse_line(self, line: str) -> StreamEvent | None:
        """解析一行 SSE 数据，返回 StreamEvent；空行/[DONE]/注释行返回 None。"""
        line = line.strip()
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        return self._parse_event(obj)

    def _parse_event(self, obj: dict) -> StreamEvent:
        event = StreamEvent(
            conversation_id=_find_conversation_id(obj),
            status=str(obj.get("status", "") or ""),
            raw=obj,
        )
        event.images = _extract_images(obj)

        parts = obj.get("parts")
        if not isinstance(parts, list):
            return event

        # if_increase_push=True 表示本帧内容是增量（追加），False/缺省表示全量快照（替换）
        meta = obj.get("meta_data")
        increase_push = isinstance(meta, dict) and bool(meta.get("if_increase_push"))

        # 先把本帧的各内容块按 logic_id 归组（帧内多块先拼接）
        frame_text: dict[str, str] = {}
        frame_reasoning: dict[str, str] = {}
        for part in parts:
            if not isinstance(part, dict):
                continue
            logic_id = part.get("logic_id")
            if not isinstance(logic_id, str) or not logic_id:
                continue
            content = part.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    self._accumulate(frame_text, logic_id, block.get("text"))
                elif block_type == "think":
                    self._accumulate(frame_reasoning, logic_id, block.get("think"))
                elif block_type == "execution_output":
                    self._accumulate(frame_text, logic_id, block.get("content"))
                elif block_type == "code":
                    code = block.get("code")
                    if isinstance(code, str) and code:
                        self._accumulate(frame_text, logic_id, f"```python\n{code}\n```")
                # 其余类型（含 image）静默丢弃：本服务的文本链路不消费它们

        # 帧内容并入全量状态：增量帧追加，快照帧替换
        for logic_id, value in frame_text.items():
            self._text_state[logic_id] = self._text_state.get(logic_id, "") + value if increase_push else value
        for logic_id, value in frame_reasoning.items():
            self._reasoning_state[logic_id] = (
                self._reasoning_state.get(logic_id, "") + value if increase_push else value
            )

        # 顺序以 logic_id 首现为准；新 id 追加到既有顺序之后。
        for logic_id in list(frame_text.keys()) + list(frame_reasoning.keys()):
            if logic_id not in self._order:
                self._order.append(logic_id)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        for logic_id in self._order:
            text = self._diff(self._text_sent, self._text_state, logic_id)
            reasoning = self._diff(self._reasoning_sent, self._reasoning_state, logic_id)
            if text:
                text_parts.append(text)
            if reasoning:
                reasoning_parts.append(reasoning)

        event.text = "".join(text_parts)
        event.reasoning = "".join(reasoning_parts)
        return event

    @staticmethod
    def _accumulate(target: dict[str, str], logic_id: str, value: object) -> None:
        if isinstance(value, str) and value:
            target[logic_id] = target.get(logic_id, "") + value

    @staticmethod
    def _diff(sent: dict[str, int], state: dict[str, str], logic_id: str) -> str:
        """从全量状态中切出尚未发送的尾部；快照与已发内容一致时返回空。"""
        full = state.get(logic_id, "")
        prev = sent.get(logic_id, 0)
        if len(full) <= prev:
            return ""
        sent[logic_id] = len(full)
        return full[prev:]


def status_finish_reason(status: str) -> tuple[bool, str]:
    """根据 status 判断是否结束及对应的 finish_reason。

    返回 (是否结束, finish_reason)。
    finish_reason 取值：intervene -> content_filter；其余结束 -> stop。
    """
    if status in ("finish", "stop"):
        return True, "stop"
    if status == "intervene":
        return True, "content_filter"
    return False, ""
