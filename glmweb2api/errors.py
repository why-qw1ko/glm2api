"""统一异常与按客户端协议渲染错误体。

上游错误、参数校验失败、内部异常，统一收敛为 GatewayError，
再由各 adapter 按客户端协议渲染成对应形状的错误 JSON。
这样"业务逻辑怎么报错"和"客户端看到什么"解耦：新增协议只需新增一个渲染函数。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GatewayError(Exception):
    """带 HTTP 状态码与机器可读错误码的异常。

    status: HTTP 状态码，最终写进响应。
    code:   机器可读错误码（如 invalid_api_key / upstream_error），供客户端程序判断。
    message: 人类可读的错误说明。
    param:   （可选）出错的请求字段名，OpenAI 风格错误体会用到。
    """

    status: int = 500
    code: str = "internal_error"
    message: str = "internal error"
    param: str | None = None

    def __init__(self, status: int, code: str, message: str, param: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param


def openai_error_body(exc: GatewayError) -> dict:
    """OpenAI 风格错误体。"""
    return {
        "error": {
            "message": exc.message,
            "type": exc.code,
            "param": exc.param,
            "code": exc.code,
        }
    }


def anthropic_error_type(status: int) -> str:
    """把 HTTP 状态码映射为 Anthropic 错误 type 字段。"""
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 413:
        return "request_too_large"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


def anthropic_error_body(exc: GatewayError) -> dict:
    """Anthropic 风格错误体。"""
    return {
        "type": "error",
        "error": {"type": anthropic_error_type(exc.status), "message": exc.message},
    }
