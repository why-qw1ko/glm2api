"""命令行入口：启动服务并在本机打开管理面板。"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    server = uvicorn.Server(uvicorn.Config(
        "glmweb2api.main:app", host=settings.host, port=settings.port,
        log_level=settings.log_level.lower(),
    ))
    stopped = threading.Event()
    host = settings.host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    url = f"http://{host}:{settings.port}/panel/"

    def open_panel():
        # 等待本进程真正监听成功，避免打开未启动服务或同端口其他服务。
        while not stopped.wait(0.1):
            if not server.started:
                continue
            try:
                if not webbrowser.open(url, new=2):
                    logging.getLogger("glmweb2api").info("请在浏览器打开 %s", url)
            except Exception:
                logging.getLogger("glmweb2api").info("浏览器未能打开，请访问 %s", url)
            return

    desktop = os.name == "nt" or os.sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if settings.panel_open_browser and desktop:
        threading.Thread(target=open_panel, name="panel-browser", daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        # Windows 下 uvicorn 优雅关闭后会重新抛出 SIGINT，这里吞掉以免打印 traceback。
        pass
    finally:
        stopped.set()


if __name__ == "__main__":
    main()
