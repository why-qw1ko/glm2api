"""上游请求签名与请求头构造。

这些是"浏览器插件客户端"协议里的关键部分：上游据此区分客户端形态。
核心是 X-Sign 签名 —— 一段基于时间戳、随机数、固定密钥的 MD5，
全部可用纯 Python 复现，没有任何设备指纹或原生依赖。

要点：
- 对话/生图接口与令牌接口（刷新、游客）**都**要求签名头
- 游客接口用"默认浏览器"形态（app_fr=default、Accept 为 JSON、空 body + Referer）
- 对话/刷新接口用"浏览器插件"形态（app_fr=browser_extension、Accept 为 text/event-stream）
"""

from __future__ import annotations

import hashlib
import random
import time
import uuid

from ..config import get_settings

# 伪装成 PC 端 Edge 浏览器插件。User-Agent 与 Sec-* 一起构成完整的浏览器指纹。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

# 客户端形态：default = 普通浏览器，browser_extension = 浏览器插件。
_APP_FR_DEFAULT = "default"
_APP_FR_EXTENSION = "browser_extension"

# 普通对话上游的 Origin。agent 模式会用 agentmore 域名覆盖。
_DEFAULT_ORIGIN = "https://chatglm.cn"


def build_sign(now_ms: int | None = None) -> tuple[str, str, str]:
    """生成 (X-Sign, X-Timestamp, X-Nonce)。

    时间戳处理是这套协议里唯一不直观的细节：取毫秒时间戳字符串，
    把**倒数第二位数字替换成一个校验位**，校验位 = (各位数字之和 - 原倒数第二位) % 10。

    校验位可以由时间戳完全推导出来，所以它不提供安全性，
    只是服务端识别"客户端是否实现了该协议"的一道指纹。
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    digits = [int(ch) for ch in str(now_ms)]
    checksum = (sum(digits) - digits[-2]) % 10
    timestamp = "".join(str(d) for d in digits[:-2]) + str(checksum) + str(digits[-1])
    nonce = uuid.uuid4().hex
    sign = hashlib.md5(f"{timestamp}-{nonce}-{get_settings().glm_sign_secret}".encode("utf-8")).hexdigest()
    return sign, timestamp, nonce


def random_x_forwarded_for() -> str:
    """生成一个随机的公网 IPv4 地址，用于 X-Forwarded-For 头。

    上游会用它做来源粗判。刻意避开保留段（10/127/169.254/172.16-31/192.168），
    让它看起来像真实的公网客户端。这属于浏览器指纹的一部分，不是欺骗网关。
    """
    while True:
        first = random.randint(1, 223)
        if first in (10, 127, 169, 172, 192):
            continue
        return ".".join(str(octet) for octet in (first, random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))


def _sign_headers() -> dict[str, str]:
    sign, timestamp, nonce = build_sign()
    return {
        "X-Device-Id": uuid.uuid4().hex,
        "X-Nonce": nonce,
        "X-Request-Id": uuid.uuid4().hex,
        "X-Sign": sign,
        "X-Timestamp": timestamp,
    }


def _browser_headers(app_fr: str, origin: str = _DEFAULT_ORIGIN, referer: str | None = None) -> dict[str, str]:
    """构造浏览器形态的基础请求头。

    app_fr 决定 Accept 与 Accept-Encoding：
    - default：普通网页请求，Accept 为 JSON，响应可 gzip
    - browser_extension：插件请求，Accept 为 event-stream（流式）

    origin/referer 仅在上游域名不同时覆盖（如 agent 模式切到 agentmore）。
    """
    is_default = app_fr == _APP_FR_DEFAULT
    headers = {
        "Accept": "application/json, text/plain, */*" if is_default else "text/event-stream",
        "Accept-Encoding": "gzip, deflate" if is_default else "identity",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "App-Name": "chatglm",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Origin": origin or _DEFAULT_ORIGIN,
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": _BROWSER_UA,
        "X-App-Fr": app_fr,
        "X-App-Platform": "pc",
        "X-App-Version": "0.0.1",
        "X-Device-Brand": "",
        "X-Device-Model": "",
        "X-Lang": "zh",
        "X-Forwarded-For": random_x_forwarded_for(),
    }
    if referer:
        headers["Referer"] = referer
    return headers


def build_headers(
    access_token: str,
    cookie: str | None = None,
    origin: str | None = None,
    referer: str | None = None,
) -> dict[str, str]:
    """构造一次上游对话/生图请求的完整请求头（插件形态）。"""
    headers = _browser_headers(_APP_FR_EXTENSION, origin or _DEFAULT_ORIGIN, referer)
    headers.update(_sign_headers())
    headers["Authorization"] = f"Bearer {access_token}"
    if cookie:
        headers["Cookie"] = cookie
    return headers


def build_refresh_headers(refresh_token: str) -> dict[str, str]:
    """构造访问令牌刷新接口的请求头（插件形态 + 签名）。"""
    headers = _browser_headers(_APP_FR_EXTENSION)
    headers.update(_sign_headers())
    headers["Authorization"] = f"Bearer {refresh_token}"
    return headers


def build_guest_headers() -> dict[str, str]:
    """构造游客令牌接口的请求头（默认浏览器形态 + 签名 + 空 body 标记）。

    与刷新接口的区别：游客接口要求空请求体（Content-Length: 0）、带 Referer、
    且用普通浏览器形态（X-App-Fr: default）。
    """
    headers = _browser_headers(_APP_FR_DEFAULT)
    headers.update(_sign_headers())
    headers["Referer"] = "https://chatglm.cn/"
    return headers
