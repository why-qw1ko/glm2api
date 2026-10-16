"""共享的 prompt 构建工具：多轮消息压平、内容提取、思考开关判断。

上游只接受单条 user 消息（纯文本），所以各 adapter 都要把客户端的多轮消息
压平成一段带角色前缀的文本。这里的函数被 OpenAI/Responses/Anthropic adapter 共用。
"""

from __future__ import annotations

from ..errors import GatewayError


def content_to_text(content: object) -> str:
    """把一条消息的 content（字符串或 part 数组）提取为纯文本。

    遇到图片等多媒体 part 时抛出 unsupported_multimodal_input —— 本服务当前
    只支持文本，明确报错比静默丢图更诚实。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind in ("text", "input_text", "output_text", "summary_text"):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif kind in ("image_url", "input_image", "input_file", "image", "document"):
                    raise GatewayError(
                        400,
                        "unsupported_multimodal_input",
                        "该服务当前只支持文本输入，不支持图片或文件。",
                    )
                # 未知 type 跳过
        return "\n".join(parts)
    return ""


def flatten_openai_messages(messages: list[dict], instructions: str | None = None) -> str:
    """把 OpenAI 风格的 messages 压平为一段纯文本。

    单条 user 消息且无 instructions 时直接返回其内容（不包装），
    其余情况用 [role] 前缀逐条拼接，并在末尾追加明确的续写提示。
    """
    # 规范化角色：developer 与 system 视为同类
    def role_title(role: str) -> str:
        return "developer" if role == "system" else role

    # 单条 user 消息的快速路径
    if not instructions and len(messages) == 1:
        msg = messages[0]
        if msg.get("role") == "user":
            return content_to_text(msg.get("content"))

    parts: list[str] = []
    if instructions:
        parts.append(f"[developer instructions]\n{instructions}")

    for msg in messages:
        role = msg.get("role", "user")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise GatewayError(400, "invalid_role", f"不支持的消息角色: {role}")
        content = content_to_text(msg.get("content"))
        # tool 调用与结果的处理：assistant 带 tool_calls 时序列化为可读文本
        extra = ""
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            rendered = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                rendered.append(
                    f"调用工具 {fn.get('name')} (call_id={call.get('id', '')}) "
                    f"入参: {fn.get('arguments', '')}"
                )
            if rendered:
                extra = "\n" + "\n".join(rendered)
        if not content and not extra:
            continue
        parts.append(f"[{role_title(role)}]\n{content}{extra}")

    parts.append("[end conversation history]\nGenerate only the next assistant turn.")
    return "\n\n".join(parts)


def detect_reasoning(model: str, body: dict) -> bool:
    """判断客户端是否显式要求思考链（reasoning）。

    上游并不真正由这些参数控制思考 —— 思考模式由模型后缀（chat_mode）决定。
    这里仅判断"客户端是否有思考意图"，用于决定是否把 chat_mode 设为深度思考，
    以及工具调用场景下强制开启思考。
    """
    if any(k in model.lower() for k in ("reasoner", "reasoning")):
        return True
    if body.get("deep_think") or body.get("thinking_enabled") or body.get("enable_thinking"):
        return True
    thinking = body.get("thinking")
    if isinstance(thinking, bool):
        return thinking
    if isinstance(thinking, dict):
        if thinking.get("enabled") is True:
            return True
        if thinking.get("type") in ("enabled", "on", "adaptive"):
            return True
    effort = body.get("reasoning_effort")
    if isinstance(effort, str) and effort.lower() not in ("none", ""):
        return True
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("enabled"):
        return True
    return False


def resolve_thinking_level(model: str, body: dict) -> str | None:
    """解析客户端显式选择的思考强度档位，返回 "fast"/"deep"/"max"，未指定返回 None。

    优先级：
      1. reasoning_effort（OpenAI 风格）：low/minimal -> fast；medium/high -> deep；max -> 极致
      2. 其他思考意图（thinking、enable_thinking 等参数）-> deep
    显式选择优先于工具调用的默认强制，客户端明确要快就不再强行思考。
    """
    effort = body.get("reasoning_effort")
    if isinstance(effort, str):
        value = effort.strip().lower()
        if value in ("low", "minimal"):
            return "fast"
        if value in ("medium", "high"):
            return "deep"
        if value == "max":
            return "max"
    if detect_reasoning(model, body):
        return "deep"
    return None


def estimate_tokens(text: str) -> int:
    """本地估算 token 数（上游不返回真实 usage）。

    近似规则：每 4 个 ASCII 字符算 1 token，每个非 ASCII 字符算 1 token，
    至少返回 1。仅用于回填 usage 字段与 count_tokens 接口，不追求精确。
    """
    if not text:
        return 0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_count
    return max(1, (ascii_count + 3) // 4 + non_ascii)
