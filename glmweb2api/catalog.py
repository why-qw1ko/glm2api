"""模型目录与模型名解析。

客户端提交的 model 字段可能是一句用户自定义名称，也可能是 "assistant_id:suffix" 形式。
这里统一解析成 (assistant_id, chat_mode)，供上游请求体使用。

关键映射：模型后缀决定思考模式（chat_mode）——
  thinking       -> "zero"     深度思考
  fast / 空      -> ""         快速
  deep_research  -> "deep_research"
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import get_settings

# assistant_id 形如 24 位十六进制；后缀可选，形如 :thinking / :fast。
_ASSISTANT_ID_RE = re.compile(r"^([0-9a-f]{24})(?::([a-z0-9_]+))?$")

# 已知后缀 -> chat_mode 的映射。
_SUFFIX_TO_CHAT_MODE = {
    "": "",
    "fast": "",
    "thinking": "thinking",
    "zero": "zero",
    "deep_research": "deep_research",
}

# 友好模型名 -> selected_model（meta_data.selected_model，与官方前端抓包一致）。
# 只有 GLM-5.3（旗舰）与 GLM-Flash（免费，5.3-Flash）两个模型。
FRIENDLY_MODEL_CATALOG: dict[str, str] = {
    "glm-5.3": "glm-5.3",
    "glm-5.3-flash": "glm-5.3-flash",
}

# 思考强度三档 -> (chat_mode, reasoning_effort)。与官方前端一致：
#   快速（默认）: chat_mode ""        + reasoning_effort "low"
#   深度:          chat_mode "thinking" + reasoning_effort "high"
#   极致:          chat_mode "deep_thinking" + reasoning_effort "max"
# 档位来源：请求体 reasoning_effort / thinking 参数，或模型名后缀 -fast/-deep/-max。
THINKING_LEVELS: dict[str, tuple[str, str]] = {
    "fast": ("", "low"),
    "deep": ("thinking", "high"),
    "max": ("deep_thinking", "max"),
}

# 模型名后缀形式的思考档位，如 glm-5.3-max / glm-5.3-flash-deep。
_THINKING_SUFFIX_RE = re.compile(r"^(glm-5\.3(?:-flash)?)-(fast|deep|max)$")


@dataclass(frozen=True)
class ModelProfile:
    """一个客户端可见模型名与其上游身份的对应关系。"""

    name: str          # 客户端提交的 model 名（原样回显）
    assistant_id: str  # 上游 assistant_id
    chat_mode: str = ""  # 上游 meta_data.chat_mode（"" 快速 / thinking 深度 / deep_thinking 极致）
    reasoning_effort: str = "low"  # 上游 meta_data.reasoning_effort（low/high/max）
    selected_model: str = "glm-5.3"  # 上游 meta_data.selected_model（glm-5.3 / glm-5.3-flash）


def suffix_to_chat_mode(suffix: str) -> str:
    """把模型后缀映射为上游 chat_mode。

    未命中已知映射时原样透传 —— 上游可能新增了未知模式，透传比强行归到快速模式更安全。
    """
    return _SUFFIX_TO_CHAT_MODE.get(suffix, suffix)


def _looks_like_assistant_id(model: str) -> bool:
    """判断字符串是否形如 assistant_id[:suffix]。"""
    return bool(_ASSISTANT_ID_RE.match(model))


def resolve_model(model: str | None) -> ModelProfile:
    """把客户端模型名解析为上游身份。

    解析优先级：
      1. 友好模型名（glm-5.3 / glm-5.3-flash）-> 默认助手 + 对应 selected_model
      2. 友好模型名 + 思考档位后缀（-fast/-deep/-max）-> 同时选定思考强度
      3. "assistant_id:suffix" 形式 -> 直接拆解
      4. 纯 assistant_id（24 位十六进制）-> 用默认后缀（快速）
      5. 其他任意名称 -> 回退到默认助手 + 快速模式（不校验模型名，交由上游判断）
    """
    settings = get_settings()
    if not model or not model.strip():
        return ModelProfile(name=model or "", assistant_id=settings.chat_assistant_id)

    name = model.strip()
    if name in FRIENDLY_MODEL_CATALOG:
        return ModelProfile(
            name=name,
            assistant_id=settings.chat_assistant_id,
            selected_model=FRIENDLY_MODEL_CATALOG[name],
        )
    level_match = _THINKING_SUFFIX_RE.match(name)
    if level_match:
        chat_mode, reasoning_effort = THINKING_LEVELS[level_match.group(2)]
        return ModelProfile(
            name=name,
            assistant_id=settings.chat_assistant_id,
            chat_mode=chat_mode,
            reasoning_effort=reasoning_effort,
            selected_model=FRIENDLY_MODEL_CATALOG[level_match.group(1)],
        )
    match = _ASSISTANT_ID_RE.match(name)
    if match:
        assistant_id = match.group(1)
        suffix = match.group(2) or "fast"
        return ModelProfile(
            name=name,
            assistant_id=assistant_id,
            chat_mode=suffix_to_chat_mode(suffix),
        )
    # 未知模型名：沿用默认助手与快速模式。上游对未识别助手会自行报错，这里不提前拒绝。
    return ModelProfile(name=name, assistant_id=settings.chat_assistant_id, chat_mode="")


def list_default_models() -> list[str]:
    """返回对外展示的默认模型名（友好名形式）。

    用于 /v1/models 的返回。assistant_id 形式的名字依然可被 resolve_model 解析，
    但展示层用友好名，避免客户端看到上游内部 ID。
    """
    return list(FRIENDLY_MODEL_CATALOG)
