"""浏览器登录：用 Playwright 打开 chatglm.cn，登录后抽取 refresh_token。

这部分是**可选**的：只有当你希望"自动获取/刷新凭据"时才需要（需安装 playwright 及 Chromium）。
也可手动从官网 Cookie 中复制 chatglm_refresh_token；兼容旧版 Local Storage。

Playwright 是延迟导入：不安装它时，本模块的其他部分（如 TokenStore）照常可用。
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit, unquote

LOGIN_URL = "https://chatglm.cn"
TOKEN_KEY = "chatglm_refresh_token"

# 当前官网把 Token 写入 Cookie，账号类型保存在 vuex.Home.userInfo。
# 必须等到非游客身份明确后再导入，避免把官网自动创建的游客凭据当作登录成功。
LOGIN_STATE_SCRIPT = """() => {
    let info = null;
    try { info = JSON.parse(localStorage.getItem('vuex') || '{}')?.Home?.userInfo; } catch {}
    return {
        authenticated: info?.is_guest === false,
        legacyToken: localStorage.getItem('chatglm_refresh_token') || ''
    };
}"""


def extract_refresh_token(snapshot, cookies) -> str:
    if not isinstance(snapshot, dict) or snapshot.get("authenticated") is not True:
        return ""
    value = next((item.get("value", "") for item in cookies
                  if item.get("name") == TOKEN_KEY
                  and item.get("domain", "").lstrip(".") == "chatglm.cn"), "")
    value = unquote(value) if value else snapshot.get("legacyToken", "")
    if not isinstance(value, str):
        return ""
    if value.startswith("Bearer "):
        value = value[7:]
    return value if value and len(value) <= 16384 and not any(c.isspace() for c in value) else ""



def login_and_extract_refresh_token(
    headless: bool = False,
    proxy: str | None = None,
    timeout_seconds: int = 600,
    poll_interval: float = 2.0,
) -> str:
    """打开浏览器让用户登录，登录完成后返回 refresh_token。

    流程：启动 Chromium → 打开 chatglm.cn → 轮询 Cookie 和旧版 Local Storage，
    直到出现 chatglm_refresh_token 或超时。用户需要在弹出的浏览器里完成登录。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "需要安装 Playwright：pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": headless}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)

        print(f"请在打开的浏览器中登录 {LOGIN_URL}，登录后会自动检测并返回。")
        deadline = time.time() + timeout_seconds
        token = ""
        while time.time() < deadline:
            token = _read_token(page)
            if token:
                break
            time.sleep(poll_interval)
        browser.close()

    if not token:
        raise TimeoutError(f"在 {timeout_seconds} 秒内未检测到登录态（尚未检测到非游客账号及 {TOKEN_KEY}）")
    return token


def _read_token(page) -> str:
    """读取当前官方页面的非游客凭据，不读取日常浏览器配置文件。"""
    if urlsplit(page.url).hostname != "chatglm.cn":
        return ""
    try:
        snapshot = page.evaluate(LOGIN_STATE_SCRIPT)
        return extract_refresh_token(snapshot, page.context.cookies(LOGIN_URL))
    except Exception:
        # 扫码回跳可能短暂销毁执行上下文，下次轮询继续。
        return ""
