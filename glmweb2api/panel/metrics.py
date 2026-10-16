"""ASGI 层记录请求结果，上游层填入用量；流式错误不依赖 HTTP 200 判断。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextvars import ContextVar

current: ContextVar[dict | None] = ContextVar("request_metrics", default=None)


def mark_error(code):
    record = current.get()
    if record is not None:
        record["outcome"] = "failed"
        record["error"] = code[:80]


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        measured = scope.get("method") == "POST" and (
            path in ("/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/images/generations", "/panel/api/test", "/panel/api/chat")
            or path.startswith("/v1beta/models/")
        )
        panel = getattr(getattr(scope.get("app"), "state", None), "panel", None)
        if scope["type"] != "http" or not measured or panel is None:
            return await self.app(scope, receive, send)
        protocol = "openai"
        for part, label in (("responses", "responses"), ("messages", "anthropic"), ("/v1beta/", "gemini"),
                            ("images", "images"), ("/panel/api/chat", "panel-chat"), ("/panel/", "panel-test")):
            if part in path:
                protocol = label
        started = time.perf_counter()
        record = dict(id=uuid.uuid4().hex, created=time.time(), protocol=protocol, model="", key_id="anonymous",
                      account="", status=500, outcome="success", error="", duration_ms=0, first_ms=None,
                      input_tokens=0, output_tokens=0, images=0)
        context = current.set(record)
        panel.active += 1
        ended = False

        async def tracked_send(message):
            nonlocal ended
            if message["type"] == "http.response.start":
                record["status"] = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", record["id"].encode())]
                if message["status"] >= 400:
                    record["outcome"] = "failed"
                    record["error"] = record["error"] or f"http_{message['status']}"
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                ended = True

        try:
            await self.app(scope, receive, tracked_send)
        except asyncio.CancelledError:
            record["outcome"] = "cancelled"
            raise
        except Exception:
            mark_error("internal_error")
            raise
        finally:
            if not ended and record["outcome"] == "success":
                record["outcome"] = "cancelled"
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            panel.active -= 1
            current.reset(context)
            try:
                # 单条落盘在请求结束时执行，正文从未进入存储。
                panel.db.record(record)
            except Exception:
                panel.metrics_error = True
                logging.getLogger("glmweb2api").error("请求统计写入失败，请检查数据目录和磁盘空间")
