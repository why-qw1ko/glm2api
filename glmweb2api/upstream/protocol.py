"""上游请求体构造。

把 canonical 请求翻译成 chatglm.cn 的对话请求体。
绝大多数字段是协议要求的固定占位，只有 assistant_id、conversation_id、
messages、meta_data.chat_mode 会随请求变化。
"""

from __future__ import annotations


def build_chat_body(
    assistant_id: str,
    prompt: str,
    conversation_id: str = "",
    chat_mode: str = "",
    is_networking: bool = False,
    reasoning_effort: str = "low",
    selected_model: str = "glm-5.3",
) -> dict:
    """构造上游对话/生图请求体。

    参数：
      assistant_id: 助手 id（对话与生图走不同 id）
      prompt: 已压平成纯文本的完整对话内容
      conversation_id: 传空表示新建会话；传值表示续接（v1 默认空）
      chat_mode: 思考强度，"" 为快速，"thinking" 为深度，"deep_thinking" 为极致
      reasoning_effort: 与思考档位对应的强度值（low/high/max）
      selected_model: 模型选择（glm-5.3 旗舰 / glm-5.3-flash 免费 GLM-Flash）

    说明：上游的 messages 只接受单条 user 消息，content 是 [{type,text}] 数组而非纯字符串。
    多轮历史由调用方提前压平进 prompt（见 adapters 的 prompt 构建逻辑）。
    """
    return {
        "assistant_id": assistant_id,
        "conversation_id": conversation_id,
        "project_id": "",
        "chat_type": "user_chat",
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        "meta_data": {
            "channel": "",
            "chat_mode": chat_mode,
            "draft_id": "",
            "reasoning_effort": reasoning_effort,
            "selected_model": selected_model,
            "input_question_type": "xxxx",
            "is_networking": is_networking,
            "is_test": False,
            "platform": "pc",
            "quote_log_id": "",
            "cogview": {"rm_label_watermark": False},
        },
    }
