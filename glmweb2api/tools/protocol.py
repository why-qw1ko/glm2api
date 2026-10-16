"""工具调用桥 —— 请求侧：把结构化 tools[] 翻译成注入 prompt 的文本契约。

上游没有结构化的工具调用通道，只接受一段纯文本。因此要把客户端的工具定义
序列化成一段约定格式的提示词，让模型"用文本描述它想调用的工具"，
再由响应侧（parser.py）把这段文本解析回结构化调用。

约定格式（自洽、无外部依赖）：
  <tool>{"name": "<工具名>", "arguments": { ... }}</tool>
"""

from __future__ import annotations

import json

# 提示词里用到的标记，parser.py 据此解析。两端必须一致。
TOOL_BLOCK_OPEN = "<tool>"
TOOL_BLOCK_CLOSE = "</tool>"


def _json_compact(value: object) -> str:
    """把工具入参 schema 序列化成紧凑 JSON 字符串。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def serialize_tools_to_prompt(tools: list[dict]) -> str:
    """把工具定义列表序列化成注入 prompt 的指令段。

    参数 tools 的元素形如 {"type": "function", "function": {"name", "description", "parameters"}}。
    也接受直接展开为 {"name", "description", "parameters"} 的形式（Anthropic 侧转换后即为此形态）。
    """
    if not tools:
        return ""

    normalized: list[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and "name" in fn:
            normalized.append(fn)
        elif isinstance(tool, dict) and "name" in tool:
            normalized.append(tool)

    if not normalized:
        return ""

    lines = [
        "[TOOL USE INSTRUCTIONS]",
        "你可以调用工具。要调用工具时，只回复一行包含 <tool> 块的 JSON，格式如下：",
        "",
        f"{TOOL_BLOCK_OPEN}{{\"name\": \"<工具名>\", \"arguments\": {{ ... }}}}{TOOL_BLOCK_CLOSE}",
        "",
        "只有当你确实要调用工具时才输出 <tool> 块；否则正常作答。",
        "",
        "可用工具：",
    ]
    for fn in normalized:
        name = fn.get("name", "")
        description = fn.get("description", "")
        parameters = fn.get("parameters") or {}
        lines.append(f"- 名称: {name}")
        if description:
            lines.append(f"  说明: {description}")
        lines.append(f"  入参 Schema: {_json_compact(parameters)}")
        lines.append("")
    lines.append("[/TOOL USE INSTRUCTIONS]")
    return "\n".join(lines)


def append_output_format(prompt: str, output_format: dict | None) -> str:
    """把输出格式要求（response_format）追加到 prompt 尾部。

    上游不支持结构化输出参数，只能靠提示词约束模型输出 JSON。
    只处理 json_object / json_schema 两种，其余忽略。
    """
    if not output_format:
        return prompt
    kind = output_format.get("type")
    suffix = ""
    if kind == "json_object":
        suffix = "[OUTPUT FORMAT]\n请只输出一个合法的 JSON 对象，不要包含任何额外说明文字或代码块标记。"
    elif kind == "json_schema":
        schema = output_format.get("json_schema", {}).get("schema", output_format)
        suffix = (
            "[OUTPUT FORMAT]\n请严格按以下 JSON Schema 输出一个合法 JSON 对象，"
            f"不要包含任何额外说明或代码块标记：\n{_json_compact(schema)}"
        )
    if not suffix:
        return prompt
    return f"{prompt}\n\n{suffix}"
