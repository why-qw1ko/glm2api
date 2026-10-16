"""可取消的官方浏览器登录会话；远程模式投送官方页面截图而非伪造二维码。"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
import os
from pathlib import Path
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import HTTPException

from ..credentials.browser import LOGIN_URL, LOGIN_STATE_SCRIPT, extract_refresh_token


@dataclass
class LoginSession:
    id: str
    owner: str
    mode: str
    expires: float = field(default_factory=lambda: time.time() + 300)
    status: str = "starting"
    message: str = "正在启动官方登录页面"
    screenshot: bytes | None = None
    qr_image: bytes | None = None
    task: asyncio.Task | None = None
    page: object = None
    page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LoginManager:
    def __init__(self, app):
        self.app = app
        self.sessions: dict[str, LoginSession] = {}

    @property
    def running(self):
        return any(s.status in ("starting", "waiting") for s in self.sessions.values())

    def owned(self, session_id, owner):
        session = self.sessions.get(session_id)
        if session is None or session.owner != owner:
            raise HTTPException(404, "登录会话不存在")
        return session

    def start(self, owner, mode):
        if self.running:
            raise HTTPException(409, "已有登录会话，请先完成或取消")
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > time.time()}
        session = LoginSession(uuid.uuid4().hex, owner, mode)
        self.sessions[session.id] = session
        session.task = asyncio.create_task(self._run(session))
        return self.view(session)

    @staticmethod
    def view(session):
        return {"id": session.id, "status": session.status, "message": session.message,
                "expires": session.expires, "has_image": session.screenshot is not None,
                "has_qr": session.qr_image is not None, "mode": session.mode}

    async def cancel(self, session):
        if session.task and not session.task.done():
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.task

    async def close(self):
        for session in list(self.sessions.values()):
            await self.cancel(session)

    async def click(self, session, x, y):
        if session.status != "waiting" or session.page is None:
            raise HTTPException(409, "登录页面尚未就绪")
        async with session.page_lock:
            await session.page.mouse.click(x, y)

    async def _run(self, session):
        browser = None
        try:
            from playwright.async_api import async_playwright, Error as BrowserError
            async with async_playwright() as playwright:
                settings = self.app.state.settings
                options = {"headless": session.mode == "remote"}
                if settings.panel_browser_channel:
                    options["channel"] = settings.panel_browser_channel
                elif os.name == "nt":
                    edge = Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe"
                    chrome = Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe"
                    if edge.is_file():
                        options["channel"] = "msedge"
                    elif chrome.is_file():
                        options["channel"] = "chrome"
                if settings.proxy:
                    url = urlsplit(settings.proxy)
                    from urllib.parse import unquote
                    options["proxy"] = {"server": f"{url.scheme}://{url.hostname}:{url.port or 80}"}
                    if url.username:
                        options["proxy"].update(username=unquote(url.username), password=unquote(url.password or ""))
                browser = await playwright.chromium.launch(**options)
                try:
                    context = await browser.new_context(viewport={"width": 1000, "height": 740}, locale="zh-CN")
                    page = await context.new_page()
                    session.page = page
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                    # SPA 加载与活动弹窗可能延迟出现；入口尝试失败不应销毁整个登录会话。
                    with contextlib.suppress(BrowserError):
                        await page.get_by_text("登录", exact=True).first.wait_for(timeout=15000)
                        notice = page.get_by_text("我知道了", exact=True)
                        if await notice.count() == 1 and await notice.is_visible():
                            await notice.click(timeout=3000)
                        for label in ("登录", "登录/注册", "立即登录"):
                            locator = page.get_by_text(label, exact=True)
                            if await locator.count() == 1 and await locator.is_visible():
                                await locator.click(timeout=3000)
                                break
                    session.status = "waiting"
                    session.message = "请在官方页面登录并完成确认，面板会自动读取登录结果"
                    while time.time() < session.expires:
                        async with session.page_lock:
                            for candidate in context.pages:
                                if urlsplit(candidate.url).hostname == "chatglm.cn":
                                    token = None
                                    # 扫码回跳时执行上下文可能短暂销毁，下次轮询继续读取。
                                    with contextlib.suppress(BrowserError):
                                        snapshot = await candidate.evaluate(LOGIN_STATE_SCRIPT)
                                        token = extract_refresh_token(snapshot, await context.cookies(LOGIN_URL))
                                    if isinstance(token, str) and token:
                                        await self.app.state.tokens.add(token)
                                        self.app.state.panel.db.audit("account.login", {"mode": session.mode})
                                        session.status = "completed"
                                        session.message = "登录完成，账号已保存并接入反代"
                                        return
                            if page.is_closed():
                                session.status = "cancelled"
                                session.message = "官方浏览器已关闭"
                                return
                            if session.mode == "local":
                                session.screenshot = None
                                session.qr_image = None
                            else:
                                # 截图只是辅助显示，短暂导航或截图失败不能中断账号检测。
                                with contextlib.suppress(BrowserError):
                                    session.screenshot = await page.screenshot(type="png", timeout=3000)
                            session.qr_image = None
                            # 只捕获官网创建的微信二维码 iframe，不创建或重写登录 URL。
                            frames = page.locator('iframe[src^="https://open.weixin.qq.com/connect/qrconnect"]')
                            if session.mode == "remote" and await frames.count() == 1 and await frames.is_visible():
                                with contextlib.suppress(BrowserError):
                                    session.qr_image = await frames.screenshot(type="png", timeout=3000)
                        await asyncio.sleep(2)
                    session.status = "expired"
                    session.message = "登录会话已过期，请重新发起；官方二维码可能更早失效"
                finally:
                    await browser.close()
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.message = "登录已取消"
            raise
        except ImportError:
            session.status = "failed"
            session.message = "未安装浏览器组件，请按 README 安装 browser 依赖和 Chromium"
        except Exception:
            session.status = "failed"
            session.message = "官方登录页面无法打开或登录未完成，请检查浏览器安装、网络和数据目录"
        finally:
            session.screenshot = None
            session.qr_image = None
            session.page = None
