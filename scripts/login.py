"""Playwright 登录脚手架：打开浏览器登录 chatglm.cn，抽取 refresh_token 存入本地。

用法（需先安装 Playwright 与 Chromium）：
    pip install playwright
    playwright install chromium
    python scripts/login.py [--headless] [--proxy http://127.0.0.1:7897]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能直接 import 项目包（不依赖安装）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glmweb2api.credentials.browser import login_and_extract_refresh_token  # noqa: E402
from glmweb2api.credentials.store import TokenStore, mask_token  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="登录 chatglm.cn 并保存 refresh_token")
    parser.add_argument("--headless", action="store_true", help="无头模式（可能触发风控，慎用）")
    parser.add_argument("--proxy", default="", help="如 http://127.0.0.1:7897")
    args = parser.parse_args()

    try:
        token = login_and_extract_refresh_token(headless=args.headless, proxy=args.proxy or None)
    except Exception as exc:
        print(f"登录失败：{exc}")
        return 1

    store = TokenStore()
    store.add(token)
    print(f"已保存 refresh_token：{mask_token(token)}")
    print("现在启动服务即可使用：python -m glmweb2api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
