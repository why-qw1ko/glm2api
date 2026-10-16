"""工具调用桥 —— 响应侧：把模型输出文本解析回结构化工具调用。

模型按 prompt 契约输出 `<tool>{json}</tool>` 块；本模块负责：
1. 从文本中识别并剥离这些块
2. 把块内 JSON 宽松地解析成 {"name", "arguments"}
3. 把工具名与客户端声明的工具做匹配（精确优先，其次模糊）

同时提供一个流式拆分器，让流式路径能边收边区分"正文"与"工具调用"。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

# 两种可能出现的调用块：规范形式 <tool>{json}</tool>，
# 以及部分模型自行发挥的 <tool_call name="...">{json}</tool_call>。
_TOOL_BLOCK_RE = re.compile(r"<tool>\s*(.*?)\s*</tool>", re.DOTALL)
_TOOL_CALL_TAG_RE = re.compile(
    r"<tool_call(?:\s+[^>]*)?>\s*(.*?)\s*</tool_call>", re.DOTALL
)
# 判断文本中是否还存在（可能未闭合的）工具块开头，用于流式安全边界。
_OPEN_TAG_RE = re.compile(r"<tool(?:_call)?(?:\s|>)")


@dataclass
class ToolCall:
    """解析出的一个工具调用。"""

    name: str
    arguments: dict


@dataclass
class ToolStreamSplitter:
    """增量地把文本流拆成"正文"与"工具调用"。

    流式场景下文本是分片到达的，工具块可能跨片。这里维护一个缓冲区：
    - 已完整的 <tool>...</tool> 块被立即解析为工具调用
    - 块开始前的内容按正文 flush
    - 尚未闭合的块开头保留在缓冲区，等闭合后再处理
    """

    known_names: list[str]
    _buffer: str = field(default="", init=False)

    def feed(self, text: str) -> list[tuple[str, object]]:
        """喂入一段文本，返回 [(kind, payload)]，kind 为 "text" 或 "tool"。"""
        self._buffer += text
        return self._drain(flush_rest=False)

    def flush(self) -> list[tuple[str, object]]:
        """流结束时调用，把残余缓冲区全部处理。"""
        return self._drain(flush_rest=True)

    def _drain(self, flush_rest: bool) -> list[tuple[str, object]]:
        output: list[tuple[str, object]] = []
        # 反复提取最靠前的完整工具块
        while True:
            match = _TOOL_BLOCK_RE.search(self._buffer) or _TOOL_CALL_TAG_RE.search(self._buffer)
            if not match:
                break
            before = self._buffer[: match.start()]
            if before:
                output.append(("text", before))
            tool = self._parse_block(match.group(1))
            if tool is not None:
                output.append(("tool", tool))
            self._buffer = self._buffer[match.end():]

        if flush_rest:
            if self._buffer:
                output.append(("text", self._buffer))
                self._buffer = ""
        else:
            # 找到最早的（可能未闭合的）工具块开头，之前的内容可以安全 flush。
            open_match = _OPEN_TAG_RE.search(self._buffer)
            if open_match:
                if open_match.start() > 0:
                    output.append(("text", self._buffer[: open_match.start()]))
                    self._buffer = self._buffer[open_match.start():]
            # 没有块开头时，整段都是正文，但保留少量尾部以应对块被截断在中间的情况？
            # 不保留：没有 "<tool" 开头就不可能是块，可放心 flush。
            elif self._buffer:
                # 保守起见保留尾部可能拼出 "<tool" 的部分（最长 6 字符 "<tool"）
                keep = min(len(self._buffer), 6)
                if keep < len(self._buffer):
                    output.append(("text", self._buffer[: len(self._buffer) - keep]))
                    self._buffer = self._buffer[len(self._buffer) - keep:]
        return output

    def _parse_block(self, body: str) -> ToolCall | None:
        body = body.strip()
        if not body:
            return None
        # 优先尝试直接作为 {"name":..., "arguments":...} 解析
        data = _loose_json_parse(body)
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            arguments = data.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            name = self._match_name(data["name"])
            if name:
                return ToolCall(name=name, arguments=arguments)
        # 整个 body 可能本身就是一个 arguments 对象（配合 name 属性时）
        if data is not None:
            return None
        return None

    def _match_name(self, requested: str) -> str | None:
        if requested in self.known_names:
            return requested
        lower = requested.lower()
        for name in self.known_names:
            if name.lower() == lower:
                return name
        # 模糊匹配：名字拼写有轻微出入时仍尽量匹配
        for name in self.known_names:
            if difflib.SequenceMatcher(None, name.lower(), lower).ratio() >= 0.72:
                return name
        return None


def _loose_json_parse(text: str) -> object | None:
    """宽松 JSON 解析：先标准解析，失败后做常见修复再试一次。

    模型输出常带多余逗号、单引号、缺少外层花括号等小瑕疵，
    这里做有限度的修复，而不是无脑吞异常。
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    fixed = _repair_json(text)
    if fixed is None:
        return None
    try:
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        return None


def _repair_json(text: str) -> str | None:
    """对无法直接解析的 JSON 文本做少量修复。改不动则返回 None。"""
    s = text.strip()
    if not s:
        return None
    # 1) 去掉末尾多余逗号（对象/数组末尾的 " ," / ",\n}" 等）
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # 2) 若被单引号包裹，尝试整体替换为双引号（仅当内容里没有已存在的双引号）
    if '"' not in s:
        s = s.replace("'", '"')
    # 3) 若首尾不是对象/数组，尝试补上对象括号
    if not (s.startswith("{") and s.endswith("}")):
        if not (s.startswith("[") and s.endswith("]")):
            s = "{" + s + "}"
    return s


def parse_tool_calls(text: str, known_names: list[str]) -> tuple[list[ToolCall], str]:
    """一次性解析整段文本，返回 (工具调用列表, 剥离工具块后的正文)。

    供非流式路径使用：拿到完整输出后调用一次即可。
    """
    splitter = ToolStreamSplitter(known_names=known_names)
    items = splitter.feed(text)
    items += splitter.flush()
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    for kind, payload in items:
        if kind == "tool":
            calls.append(payload)
        else:
            text_parts.append(payload)
    return calls, "".join(text_parts)
