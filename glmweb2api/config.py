"""应用配置。

集中管理所有可调参数：监听地址、上游基址、签名密钥、凭据、并发、模型目录等。
设计原则：凡是上游可能轮换、或因部署环境而异的取值，一律走配置，而不是写死在逻辑里。
这样上游接口或签名密钥变化时，只需改配置，无需改代码。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 上游基址默认值。末尾统一去掉斜杠，避免拼接出双斜杠。
DEFAULT_GLM_BASE_URL = "https://chatglm.cn/chatglm"

# 签名密钥。上游未公开该值，属于"协议指纹"而非真正的安全措施；
# 上游若轮换此值，对话会开始被拒，届时通过 GLM_SIGN_SECRET 覆盖即可。
DEFAULT_SIGN_SECRET = "8a1317a7468aa3ad86e997d08f3f31cb"

# 对话用 assistant_id：社区里"默认对话助手"对应的 id。
DEFAULT_CHAT_ASSISTANT_ID = "65940acff94777010aa6b796"
# 生图用 assistant_id：走同一个 stream 端点，只是换成生图助手。
DEFAULT_IMAGE_ASSISTANT_ID = "65a232c082ff90a2ad2f15e2"
DEFAULT_IMAGE_MODEL_NAME = "glm-image-1"

# agent 模式（agentmore.chatglm.cn 的 Agent 后端）默认基址与 Origin/Referer。
DEFAULT_AGENT_BASE_URL = "https://agentmore.chatglm.cn/chatglm"
DEFAULT_AGENT_ORIGIN = "https://agentmore.chatglm.cn"


def _env_file() -> str | None:
    """返回 .env 文件路径（若存在）。

    返回 None 表示不加载 .env，避免 pydantic-settings 在缺省时抛错。
    """
    path = Path(".env")
    return str(path) if path.is_file() else None


class Settings(BaseSettings):
    """运行时配置。所有字段均可通过环境变量或 .env 覆盖。

    字段名会自动映射为大写环境变量（例如 host -> HOST）。
    """

    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 监听 ----
    host: str = "127.0.0.1"
    port: int = 8788
    api_prefix: str = "/v1"

    # ---- 上游 ----
    glm_base_url: str = DEFAULT_GLM_BASE_URL
    glm_sign_secret: str = DEFAULT_SIGN_SECRET
    glm_timeout: float = 120.0

    # ---- agent 模式（可选，走 agentmore.chatglm.cn 的 Agent 后端）----
    # 开启后 chat_mode 固定为 chat_agent、联网开关打开，stream 端点与 Origin/Referer
    # 切到 agentmore。思考强度（reasoning_effort）与模型选择（selected_model）不变。
    glm_agent_mode: bool = False
    glm_agent_base_url: str = DEFAULT_AGENT_BASE_URL
    glm_agent_origin: str = DEFAULT_AGENT_ORIGIN

    # ---- 模型目录 ----
    chat_assistant_id: str = DEFAULT_CHAT_ASSISTANT_ID
    image_assistant_id: str = DEFAULT_IMAGE_ASSISTANT_ID
    image_model_name: str = DEFAULT_IMAGE_MODEL_NAME

    # ---- 凭据 ----
    # 单个 refresh_token（简单场景）。多账号时用 token 文件。
    glm_refresh_token: str = ""
    # 每行一个 refresh_token 的账号文件，可选。
    glm_token_file: str = "data/refresh_tokens.json"
    # ---- 并发与行为 ----
    # 允许同时占用上游的请求数。上游对并发敏感，超限会触发限流，故保守默认 3。
    glm_max_concurrency: int = 3
    # 请求结束后是否删除上游会话（保持无状态、避免服务端堆积）。
    glm_delete_conversation: bool = True

    # ---- 本服务访问控制 ----
    # 若填写，则调用本服务的客户端必须携带匹配的 Bearer token。
    # 留空表示本机调试时不鉴权（仅监听 127.0.0.1）。
    server_api_keys: list[str] = []

    # ---- 网络 ----
    # 发往上游时使用的代理（可选），如 http://127.0.0.1:7897。
    proxy: str = ""

    # ---- 管理面板 ----
    panel_open_browser: bool = True
    panel_data_dir: str = "data"
    panel_password: str = ""
    panel_cookie_secure: bool = False
    panel_browser_channel: str = ""
    panel_retention_days: int = 90

    # ---- 日志 ----
    log_level: str = "INFO"

    # ---- 派生属性 ----

    @property
    def refresh_token_values(self) -> list[str]:
        """所有可用的 refresh_token（合并 .env 单值与 token 文件）。

        返回空列表表示没有账号凭据；面板可用，反代调用返回登录提示。
        """
        from .credentials.store import TokenStore

        path = Path(self.glm_token_file or "data/refresh_tokens.json")
        if path.is_file():
            return TokenStore(path).load()
        values: list[str] = []
        if self.glm_refresh_token.strip():
            values.append(self.glm_refresh_token.strip())
        if self.glm_token_file.strip():
            path = Path(self.glm_token_file)
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    token = line.strip()
                    if token and not token.startswith("#") and token not in values:
                        values.append(token)
        return values

    @property
    def refresh_url(self) -> str:
        return f"{self.glm_base_url}/user-api/user/refresh"

    @property
    def chat_stream_url(self) -> str:
        return f"{self.glm_base_url}/backend-api/assistant/stream"

    @property
    def agent_stream_url(self) -> str:
        return f"{self.glm_agent_base_url}/agent-api/chat/stream"

    @property
    def delete_conversation_url(self) -> str:
        return f"{self.glm_base_url}/backend-api/assistant/conversation/delete"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载并缓存配置。缓存是为了避免每次请求都重新解析 .env。"""
    return Settings()
