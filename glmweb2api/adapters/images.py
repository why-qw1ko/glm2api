"""图片生成适配器（POST /v1/images/generations）。

上游生图走同一个 stream 端点，只是换成生图 assistant_id。
图片 URL 出现在 status="finish" 事件里（parser 已负责提取）。
"""

from __future__ import annotations

import time

from ..errors import GatewayError
from ..upstream.client import UpstreamClient
from .internal import CanonicalRequest


def build_canonical(body: dict) -> CanonicalRequest:
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise GatewayError(400, "missing_prompt", "缺少 prompt 字段")
    from ..config import get_settings

    settings = get_settings()
    return CanonicalRequest(
        prompt=prompt.strip(),
        assistant_id=settings.image_assistant_id,
        chat_mode="",
        model=body.get("model") or settings.image_model_name,
    )


async def generate(client: UpstreamClient, canonical: CanonicalRequest) -> dict:
    """执行生图并构造响应体。"""
    result = await client.complete(canonical)
    if not result.images:
        raise GatewayError(502, "no_image", "上游未返回图片（可能账号无生图权限，或 assistant_id 已变更）")
    data = [{"url": url} for url in result.images]
    return {"created": int(time.time()), "data": data}
