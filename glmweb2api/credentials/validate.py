"""账号凭据校验：调用 chatglm.cn 的 /api/v0/users/current 验证 access_token 是否有效。"""

from __future__ import annotations

import httpx


async def validate_access_token(
    access_token: str,
    base_url: str = "https://chatglm.cn",
    proxy: str | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """校验一个 access_token，返回账号信息；无效时返回 None。

    判定有效 = HTTP 200 且外层 code=0 且 data.biz_code=0。
    """
    url = f"{base_url}/api/v0/users/current"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Origin": "https://chatglm.cn",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
        ),
        "App-Name": "chatglm",
        "X-App-Fr": "browser_extension",
        "X-App-Platform": "pc",
        "X-Lang": "zh",
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("code") not in (0, None):
        return None
    inner = data.get("data")
    if isinstance(inner, dict) and inner.get("biz_code") not in (0, None):
        return None
    return data
