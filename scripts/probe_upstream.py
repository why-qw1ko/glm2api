#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上游协议探测脚本（Phase 0）—— 决定整个项目是否可行的 go/no-go 测试。

本脚本**复用包里的真实签名/请求体逻辑**（glmweb2api.upstream），而不是另写一份副本。
原因：签名与请求头是协议的关键，副本容易和主代码漂移（历史上就发生过），
导致探测结果误导。探测要测的就是主代码将来真正会发的请求。

它回答四个问题：
  0a 游客模式能否拿到 token 并成功对话（免账号 -> 零凭据验证）
  0b 真实 refresh_token 能否刷新出 access_token 并成功对话
  0c 回传 conversation_id 能否续接同一会话（决定能否做"真多轮"）
  0d 换 assistant_id 能否生图

用法
----
  # 0a 游客模式冒烟（不需要任何凭据）
  python scripts/probe_upstream.py guest

  # 0b 真实账号（token 从浏览器 F12 -> Application -> Local Storage -> chatglm_refresh_token）
  python scripts/probe_upstream.py account --refresh-token "<你的 refresh_token>"

  # 直接给一个现成 access_token（最短路径，跳过刷新）
  python scripts/probe_upstream.py account --access-token "<access_token>"

  # 0c 会话续接
  python scripts/probe_upstream.py continue --refresh-token "<...>"

  # 0d 生图
  python scripts/probe_upstream.py image --refresh-token "<...>" --prompt "一只在键盘上睡觉的橘猫"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本能直接 import 项目包（不依赖安装）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from glmweb2api.config import (  # noqa: E402
    DEFAULT_CHAT_ASSISTANT_ID,
    DEFAULT_IMAGE_ASSISTANT_ID,
)
from glmweb2api.upstream.auth import (  # noqa: E402
    build_guest_headers,
    build_headers,
    build_refresh_headers,
)
from glmweb2api.upstream.protocol import build_chat_body  # noqa: E402

DEFAULT_BASE = "https://chatglm.cn/chatglm"


# ---------------------------------------------------------------------------
# 凭据：刷新 / 游客
# ---------------------------------------------------------------------------

def _extract_token_pair(payload: dict) -> tuple[str | None, str | None]:
    """从刷新/游客响应里宽容地取出 (access_token, refresh_token)。

    成功响应里 token 在 result 下；做递归查找以兼容包裹层级差异。
    """
    access = refresh = None

    def walk(node):
        nonlocal access, refresh
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "access_token" and isinstance(v, str):
                    access = v
                elif k == "refresh_token" and isinstance(v, str):
                    refresh = v
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return access, refresh


def refresh_access_token(client: httpx.Client, base: str, refresh_token: str) -> str:
    """用长期 refresh_token 换短期 access_token（走真实的刷新请求头）。"""
    url = f"{base}/user-api/user/refresh"
    headers = build_refresh_headers(refresh_token)
    print(f"\n[刷新] POST {url}")
    resp = client.post(url, headers=headers, content=b"{}")
    print(f"[刷新] HTTP {resp.status_code}")
    body = _safe_json(resp)
    if body is None:
        print(f"[刷新] 非 JSON 响应：{resp.text[:500]}")
        return ""
    code = body.get("code", body.get("status"))
    print(f"[刷新] 业务码 code/status={code}")
    access, new_refresh = _extract_token_pair(body)
    if new_refresh and new_refresh != refresh_token:
        print("[刷新] ⚠ refresh_token 已轮换，请把下面这行写回你的配置：")
        print(f"[刷新]   {new_refresh}")
    if not access:
        print(f"[刷新] 未取到 access_token，原始响应：{json.dumps(body, ensure_ascii=False)[:800]}")
    return access or ""


def fetch_guest_token(client: httpx.Client, base: str) -> str:
    """游客模式：免登录拿 access_token（走真实的游客请求头）。"""
    url = f"{base}/user-api/guest/access"
    headers = build_guest_headers()
    print(f"\n[游客] POST {url}")
    resp = client.post(url, headers=headers, content=b"")
    print(f"[游客] HTTP {resp.status_code}")
    body = _safe_json(resp)
    if body is None:
        print(f"[游客] 非 JSON 响应：{resp.text[:500]}")
        return ""
    code = body.get("code", body.get("status"))
    print(f"[游客] 业务码 code/status={code}")
    access, new_refresh = _extract_token_pair(body)
    if new_refresh:
        print(f"[游客] 拿到 refresh_token（可复用）：{new_refresh[:24]}...")
    if not access:
        print(f"[游客] 未取到 access_token，原始响应：{json.dumps(body, ensure_ascii=False)[:800]}")
    return access or ""


# ---------------------------------------------------------------------------
# 上游对话
# ---------------------------------------------------------------------------

def stream_once(
    client: httpx.Client,
    base: str,
    access_token: str,
    body: dict,
    label: str,
    max_lines: int = 40,
) -> tuple[int, list[str], str]:
    """发一次流式请求，返回 (状态码, 原始行列表, conversation_id)。"""
    url = f"{base}/backend-api/assistant/stream"
    headers = build_headers(access_token)
    print(f"\n[{label}] POST {url}")
    print(f"[{label}] body: {json.dumps(body, ensure_ascii=False)[:300]}")

    raw_lines: list[str] = []
    conversation_id = ""
    try:
        with client.stream("POST", url, headers=headers, json=body) as resp:
            print(f"[{label}] HTTP {resp.status_code}")
            if resp.status_code != 200:
                text = resp.read().decode("utf-8", "replace")
                print(f"[{label}] 响应体：{text[:1000]}")
                return resp.status_code, [], ""
            for line in resp.iter_lines():
                if not line:
                    continue
                raw_lines.append(line)
                if not conversation_id:
                    conversation_id = sniff_conversation_id(line)
                if len(raw_lines) >= max_lines:
                    print(f"[{label}] （已读到 {max_lines} 行，主动断开）")
                    break
    except httpx.HTTPError as exc:
        print(f"[{label}] 请求异常：{type(exc).__name__}: {exc}")
        return -1, raw_lines, conversation_id

    print(f"[{label}] 共读到 {len(raw_lines)} 行，前 {min(8, len(raw_lines))} 行：")
    for line in raw_lines[:8]:
        print(f"[{label}]   {line[:220]}")
    if conversation_id:
        print(f"[{label}] conversation_id = {conversation_id}")
    else:
        print(f"[{label}] ⚠ 未能在前 {len(raw_lines)} 行中识别到 conversation_id")
    return 200, raw_lines, conversation_id


def sniff_conversation_id(line: str) -> str:
    """从一行 SSE 数据里尝试识别 conversation_id。"""
    if not line.startswith("data:"):
        return ""
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    for key in ("conversation_id", "conv_id", "id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    meta = obj.get("meta")
    if isinstance(meta, dict):
        for key in ("conversation_id", "conv_id"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_guest(client: httpx.Client, args) -> int:
    token = fetch_guest_token(client, args.base)
    if not token:
        print("\n结论：游客模式拿不到 token。")
        return 1
    print(f"[游客] access_token 前缀：{token[:20]}...")
    body = build_chat_body(args.assistant_id, "你好，请回复一句话。")
    status, lines, conv = stream_once(client, args.base, token, body, "游客对话")
    return 0 if status == 200 and lines else 1


def cmd_account(client: httpx.Client, args) -> int:
    if args.access_token:
        token = args.access_token
        print(f"[账号] 直接使用提供的 access_token：{token[:20]}...")
    elif args.refresh_token:
        token = refresh_access_token(client, args.base, args.refresh_token)
    else:
        print("需要 --refresh-token 或 --access-token")
        return 2
    if not token:
        return 1
    body = build_chat_body(args.assistant_id, "你好，请回复一句话。")
    status, lines, conv = stream_once(client, args.base, token, body, "账号对话")
    return 0 if status == 200 and lines else 1


def cmd_continue(client: httpx.Client, args) -> int:
    token = args.access_token or (
        refresh_access_token(client, args.base, args.refresh_token) if args.refresh_token else ""
    )
    if not token:
        print("需要 --refresh-token 或 --access-token")
        return 2

    first = build_chat_body(args.assistant_id, "我叫小明。请只回复「好的」。")
    status, lines, conv = stream_once(client, args.base, token, first, "第 1 轮")
    if not conv:
        print("\n结论：第 1 轮没拿到 conversation_id，无法测试续接。")
        return 1

    print(f"\n现在把 conversation_id={conv} 回传给第 2 轮，看模型是否记得「小明」。")
    second = build_chat_body(args.assistant_id, "我叫什么名字？", conversation_id=conv)
    status, lines, conv2 = stream_once(client, args.base, token, second, "第 2 轮（带 conv_id）")

    remembered = any("小明" in line for line in lines)
    print(f"\n结论：第 2 轮返回 conversation_id={'同' if conv2 == conv else '不同或为空'}")
    print(f"      模型是否记得「小明」：{'是 -> 会话续接有效' if remembered else '否 -> 上游未沿用该会话'}")
    return 0


def cmd_image(client: httpx.Client, args) -> int:
    token = args.access_token or (
        refresh_access_token(client, args.base, args.refresh_token) if args.refresh_token else ""
    )
    if not token:
        print("需要 --refresh-token 或 --access-token")
        return 2
    body = build_chat_body(args.image_assistant_id, args.prompt)
    status, lines, conv = stream_once(client, args.base, token, body, "生图", max_lines=200)
    if status != 200:
        print("\n结论：生图请求被拒。")
        return 1
    hit = [line for line in lines if "image" in line.lower() or "finish" in line]
    print(f"\n结论：{'疑似拿到图片数据' if hit else '未发现图片字段'}")
    for line in hit[:5]:
        print(f"  {line[:300]}")
    return 0


# ---------------------------------------------------------------------------
# 工具函数与入口
# ---------------------------------------------------------------------------

def _safe_json(resp) -> dict | None:
    try:
        data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def print_verdict() -> None:
    print(
        "\n"
        "=================== 判读标准 ===================\n"
        " 200 且有 data: 行     -> 纯 HTTP 反代成立\n"
        " 401 / 403            -> 凭据问题\n"
        " 429                  -> 触发限流\n"
        " 400 且带 JSON 错误体  -> 协议已变更（先看业务码）\n"
        " 5xx                  -> 上游故障或风控\n"
        "===============================================\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 chatglm.cn 上游协议")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--base", default=DEFAULT_BASE)
        p.add_argument("--refresh-token", default="")
        p.add_argument("--access-token", default="")
        p.add_argument("--proxy", default="")
        p.add_argument("--timeout", type=float, default=60.0)

    p_guest = sub.add_parser("guest", help="0a 游客模式冒烟（免凭据）")
    add_common(p_guest)
    p_guest.add_argument("--assistant-id", default=DEFAULT_CHAT_ASSISTANT_ID)

    p_account = sub.add_parser("account", help="0b 真实账号对话")
    add_common(p_account)
    p_account.add_argument("--assistant-id", default=DEFAULT_CHAT_ASSISTANT_ID)

    p_cont = sub.add_parser("continue", help="0c 会话续接")
    add_common(p_cont)
    p_cont.add_argument("--assistant-id", default=DEFAULT_CHAT_ASSISTANT_ID)

    p_image = sub.add_parser("image", help="0d 生图")
    add_common(p_image)
    p_image.add_argument("--image-assistant-id", default=DEFAULT_IMAGE_ASSISTANT_ID)
    p_image.add_argument("--prompt", default="一只在键盘上睡觉的橘猫，写实风格")

    args = parser.parse_args()
    proxy = args.proxy or None
    print(f"上游基址：{args.base}")
    print(f"代理：{proxy or '（不使用）'}")
    with httpx.Client(proxy=proxy, timeout=args.timeout, follow_redirects=True) as client:
        handler = {"guest": cmd_guest, "account": cmd_account, "continue": cmd_continue, "image": cmd_image}[args.command]
        code = handler(client, args)

    print_verdict()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
