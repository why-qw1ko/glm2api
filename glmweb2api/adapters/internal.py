"""canonical 中间层：四套客户端协议与上游之间的统一接口。

所有 adapter 把客户端请求翻译成 CanonicalRequest，交给上游客户端；
上游客户端把响应解析成 DeltaEvent 流（或聚合的 CompletionResult）交还给 adapter。
adapter 再按各自协议渲染输出。

这样做的好处：新增一种客户端协议，只加一个 adapter，不动上游；反之亦然。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CanonicalRequest:
    """一次已经翻译好的上游请求。

    字段：
      prompt: 压平后的纯文本对话内容（含工具契约注入、输出格式要求）
      assistant_id: 上游助手 id
      chat_mode: 思考强度（"" 快速 / "thinking" 深度 / "deep_thinking" 极致）
      reasoning_effort: 思考强度档位（low/high/max），与 chat_mode 一一对应
      selected_model: 模型选择（glm-5.3 旗舰 / glm-5.3-flash 免费）
      model: 客户端提交的模型名（仅用于回显，不影响上游）
      tools: 客户端声明的工具定义列表（已转成上游无结构化通道时的文本契约）
      tool_choice: 工具选择策略（auto/required/none/forced）
      parallel_tool_calls: 是否允许并行工具调用
    """

    prompt: str
    assistant_id: str
    chat_mode: str = ""
    reasoning_effort: str = "low"
    selected_model: str = "glm-5.3"
    model: str = ""
    tools: list[dict] | None = None
    tool_choice: str = "auto"
    parallel_tool_calls: bool = True
    # agent 模式覆盖：None 表示跟随全局配置 glm_agent_mode；True/False 按请求覆盖。
    agent_mode: bool | None = None

    @property
    def tools_active(self) -> bool:
        return bool(self.tools)


@dataclass
class DeltaEvent:
    """上游返回的一个增量事件。

    kind 取值：
      "text"       -> text 字段是新增正文
      "reasoning"  -> reasoning 字段是新增思考链
      "finish"     -> finish_reason 生效，可能携带 images
    """

    kind: str
    text: str = ""
    reasoning: str = ""
    finish_reason: str = "stop"
    images: list[str] = field(default_factory=list)


@dataclass
class CompletionResult:
    """聚合后的完整结果（非流式路径使用）。"""

    text: str = ""
    reasoning: str = ""
    finish_reason: str = "stop"
    tool_calls: list[dict] | None = None
    images: list[str] = field(default_factory=list)
    conversation_id: str | None = None
