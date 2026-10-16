"""签名与请求头测试。

用固定时间戳独立重算期望值（不调用被测函数推导），避免"自己验证自己"。
"""

import hashlib

from glmweb2api.config import get_settings
from glmweb2api.upstream.auth import build_headers, build_sign


def _expected_timestamp(now_ms: int) -> str:
    """独立实现时间戳的校验位替换逻辑，作为对照。"""
    s = str(now_ms)
    digits = [int(ch) for ch in s]
    checksum = (sum(digits) - digits[-2]) % 10
    return s[: len(s) - 2] + str(checksum) + s[-1]


def test_build_sign_matches_checksum_rule():
    secret = get_settings().glm_sign_secret
    for now_ms in [1792116502978, 1700000000000, 1234567890123, 1999999999999]:
        sign, timestamp, nonce = build_sign(now_ms)
        assert timestamp == _expected_timestamp(now_ms)
        assert len(nonce) == 32
        assert sign == hashlib.md5(f"{timestamp}-{nonce}-{secret}".encode()).hexdigest()


def test_build_headers_contains_required_keys():
    headers = build_headers("tok_abc")
    for key in (
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Sign",
        "X-Timestamp",
        "X-Nonce",
        "X-Request-Id",
        "X-Device-Id",
        "X-App-Fr",
    ):
        assert key in headers
    assert headers["Authorization"] == "Bearer tok_abc"
    assert headers["X-App-Fr"] == "browser_extension"


def test_build_headers_cookie_optional():
    headers = build_headers("tok", cookie="a=1")
    assert headers["Cookie"] == "a=1"
    assert "Cookie" not in build_headers("tok")


def test_build_headers_origin_and_referer_override():
    """agent 模式把 Origin/Referer 切到 agentmore；默认仍回 chatglm.cn。"""
    headers = build_headers(
        "tok",
        origin="https://agentmore.chatglm.cn",
        referer="https://agentmore.chatglm.cn/",
    )
    assert headers["Origin"] == "https://agentmore.chatglm.cn"
    assert headers["Referer"] == "https://agentmore.chatglm.cn/"

    default = build_headers("tok")
    assert default["Origin"] == "https://chatglm.cn"
    assert "Referer" not in default
