"""可选的真实浏览器验收；使用隔离数据和随机密码，不访问上游账号。"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "data" / "ui-check"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="panel-ui-", dir=output) as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        password = secrets.token_urlsafe(24)
        env = os.environ.copy()
        for name in ("GLM_REFRESH_TOKEN", "GLM_USE_GUEST", "SERVER_API_KEYS", "PROXY"):
            env.pop(name, None)
        env.update(PANEL_PASSWORD=password, PANEL_DATA_DIR=str(Path(directory) / "data"),
                   GLM_TOKEN_FILE=str(Path(directory) / "tokens.txt"), PANEL_COOKIE_SECURE="false")
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "glmweb2api.main:app", "--host", "127.0.0.1",
                                    "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("UI 验证服务未能启动")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **({"channel": "msedge"} if os.name == "nt" else {}))
                page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.goto(base + "/panel/")
                page.locator("#login-form input").fill(password)
                page.locator("#login-form button").click()
                page.locator("#app").wait_for(state="visible")
                page.locator("#overview-metrics .metric").first.wait_for()
                page.screenshot(path=str(output / "panel-desktop.png"), full_page=True)
                for section in ("usage", "accounts", "keys", "config", "test", "connect", "audit"):
                    page.locator(f'[data-page="{section}"]').click()
                    page.locator(f"#page-{section}[aria-busy=false]").wait_for(state="visible")
                    assert not page.locator("#page-error").is_visible(), section
                page.locator('[data-page="keys"]').click()
                page.locator("#page-keys[aria-busy=false]").wait_for()
                page.locator('#key-form input').fill("浏览器验收密钥")
                page.locator('#key-form button').click()
                page.locator('#new-key-box').wait_for(state="visible")
                key = page.locator('#new-key').inner_text()
                assert key.startswith("glm_")
                # 使用合法 Key 提交无效请求，产生真实失败统计；不向上游发送内容。
                rejected = page.request.post(base + "/v1/chat/completions",
                    headers={"Authorization": "Bearer " + key}, data={"model": "glm-4", "messages": []})
                assert rejected.status == 400
                page.locator('#dismiss-key').click()
                assert not page.locator('#new-key').inner_text()
                page.locator('[data-page="config"]').click()
                page.locator("#page-config[aria-busy=false]").wait_for()
                page.locator('[name="glm_timeout"]').fill("45")
                page.locator('#config-form button[type="submit"]').click()
                try:
                    page.locator('#confirm-dialog').wait_for(state="visible", timeout=5000)
                except Exception:
                    print("Browser errors:", errors)
                    print("Panel message:", page.locator('#page-error').inner_text())
                    print("Config timeout:", page.locator('[name="glm_timeout"]').input_value())
                    page.screenshot(path=str(output / "panel-config-failure.png"), full_page=True)
                    raise
                page.locator('#confirm-ok').click()
                page.wait_for_function("document.querySelector('[name=glm_timeout]').value === '45'")
                page.locator('[data-page="overview"]').click()
                page.locator("#page-overview[aria-busy=false]").wait_for()
                assert page.locator("#overview-chart svg .bar").count() > 0
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(output / "panel-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "窄屏出现横向溢出"
                page.locator("#logout-mobile").click()
                page.locator("#login-view").wait_for(state="visible")
                assert not errors, errors
                browser.close()
            print("Panel UI passed: navigation, key creation, config save, mobile overflow, JavaScript errors.")
            print("Screenshots: data/ui-check/panel-desktop.png and panel-mobile.png")
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
