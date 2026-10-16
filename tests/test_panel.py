"""Panel 权限、配置、持久化及流式统计的离线回归测试。"""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from glmweb2api.config import get_settings
from glmweb2api.errors import GatewayError
from glmweb2api.main import app
from glmweb2api.panel.metrics import current, MetricsMiddleware
from glmweb2api.upstream.tokens import TokenManager

PASSWORD = "test-admin-password-only"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("GLM_REFRESH_TOKEN", "GLM_TOKEN_FILE", "GLM_USE_GUEST", "SERVER_API_KEYS", "PANEL_PASSWORD",
                 "PANEL_DATA_DIR", "PANEL_COOKIE_SECURE", "GLM_MAX_CONCURRENCY", "PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def login(client):
    response = client.post("/panel/api/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return {"x-csrf-token": response.json()["csrf"]}


def test_panel_is_served_and_admin_is_separate(client):
    assert client.get("/panel", follow_redirects=False).status_code == 307
    response = client.get("/panel/")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for path in ("/panel/api/overview", "/panel/api/keys", "/admin/status"):
        assert client.get(path).status_code == 401
    assert client.post("/admin/refresh-token", json={"refresh_token": "private-token"}).status_code == 401


def test_csrf_and_origin_and_validation_do_not_echo_secrets(client):
    headers = login(client)
    assert client.post("/panel/api/keys", json={"name": "test"}).status_code == 403
    assert client.post("/panel/api/keys", headers={**headers, "origin": "https://evil.example"}, json={"name": "test"}).status_code == 403
    secret = "secret-never-returned"
    response = client.post("/panel/api/accounts", headers=headers, json={"refresh_token": {"secret": secret}})
    assert response.status_code == 422
    assert secret not in response.text
    assert client.post("/panel/api/login", headers={"origin": "https://evil.example"}, json={"password": PASSWORD}).status_code == 403


def test_login_throttle_and_logout(client):
    headers = login(client)
    client.post("/panel/api/logout", headers=headers)
    assert client.get("/panel/api/session").status_code == 401
    for _ in range(10):
        assert client.post("/panel/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/panel/api/login", json={"password": PASSWORD}).status_code == 429


def test_api_key_create_revoke_and_admin_denied(client):
    headers = login(client)
    created = client.post("/panel/api/keys", headers=headers, json={"name": "Editor"}).json()
    key = created["key"]
    assert key not in client.get("/panel/api/keys").text
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 200
    assert client.get("/v1/models", headers={"x-api-key": key}).status_code == 200
    assert client.get("/v1/models", headers={"x-goog-api-key": key}).status_code == 200
    assert client.patch(f"/panel/api/keys/{created['id']}", headers=headers, json={"enabled": False}).status_code == 200
    assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 401
    client.cookies.clear()
    assert client.get("/panel/api/keys", headers={"Authorization": "Bearer " + key}).status_code == 401


def test_import_immediately_persists_and_restart_loads(client):
    headers = login(client)
    token = "test-refresh-long-token"
    response = client.post("/panel/api/accounts", headers=headers, json={"refresh_token": token})
    assert response.json()["added"]
    assert all(not a["guest"] for a in app.state.tokens.snapshot())
    assert token not in client.get("/panel/api/accounts").text
    assert get_settings().refresh_token_values == [token]
    assert not client.post("/panel/api/accounts", headers=headers, json={"refresh_token": token}).json()["added"]
    assert not TokenManager(get_settings()).snapshot()[0]["guest"]
    account_id = app.state.tokens.snapshot()[0]["id"]
    client.delete(f"/panel/api/accounts/{account_id}", headers=headers)
    assert get_settings().refresh_token_values == []
    assert Path("data/refresh_tokens.json").exists()


def test_config_validation_persistence_and_redaction(client):
    headers = login(client)
    assert client.patch("/panel/api/config", headers=headers, json={"glm_max_concurrency": 0}).status_code == 422
    assert client.patch("/panel/api/config", headers=headers, json={"host": "0.0.0.0"}).status_code == 422
    assert client.patch("/panel/api/config", headers=headers, json={"glm_base_url": "http://example.com"}).status_code == 422
    proxy = "http://username:private-proxy-password@localhost:9999"
    response = client.patch("/panel/api/config", headers=headers, json={"glm_max_concurrency": 5, "proxy": proxy})
    assert response.status_code == 200
    assert app.state.client._settings.glm_max_concurrency == 5
    assert app.state.panel.db.get("runtime_config")["glm_max_concurrency"] == 5
    assert "private-proxy-password" not in client.get("/panel/api/config").text
    assert "private-proxy-password" not in client.get("/panel/api/audit").text
    app.state.panel.active = 1
    assert client.patch("/panel/api/config", headers=headers, json={"glm_timeout": 20}).status_code == 409
    app.state.panel.active = 0


def test_config_environment_locked(client):
    app.state.panel.locked.append("glm_timeout")
    assert client.patch("/panel/api/config", headers=login(client), json={"glm_timeout": 10}).status_code == 409


def test_stream_failure_is_not_counted_as_success(client, monkeypatch):
    from glmweb2api.adapters.internal import DeltaEvent

    async def broken(canonical):
        record = current.get()
        record.update(model=canonical.model, input_tokens=7, output_tokens=3)
        yield DeltaEvent(kind="text", text="partial")
        raise GatewayError(502, "upstream_failed", "上游请求失败")

    monkeypatch.setattr(app.state.client, "stream", broken)
    headers = login(client)
    key = client.post("/panel/api/keys", headers=headers, json={"name": "Metrics"}).json()
    body = {"model": "glm-4", "stream": True, "messages": [{"role": "user", "content": "sensitive-prompt"}]}
    response = client.post("/v1/chat/completions", headers={"authorization": "Bearer " + key["key"]}, json=body)
    assert response.status_code == 200
    assert "upstream_failed" in response.text
    data = client.get("/panel/api/usage").json()
    assert data["summary"]["failed"] == 1
    assert data["summary"]["success"] == 0
    assert data["rows"][0]["key_id"] == key["id"]
    assert data["rows"][0]["status"] == 200
    assert "sensitive-prompt" not in json.dumps(data)


def test_actual_upstream_metrics_and_stream_usage(client, monkeypatch):
    from glmweb2api.adapters.internal import DeltaEvent
    from glmweb2api.adapters.openai_chat import build_canonical, sse_stream

    class FakeClient:
        async def stream(self, request):
            yield DeltaEvent(kind="text", text="abcd")
            yield DeltaEvent(kind="reasoning", reasoning="思考")
            yield DeltaEvent(kind="finish")

    async def collect():
        request = build_canonical({"model": "glm-4", "messages": [{"role": "user", "content": "你好"}]})
        chunks = [chunk async for chunk in sse_stream(FakeClient(), request, True)]
        data = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
        usage = next(c["usage"] for c in data if "usage" in c)
        assert usage["completion_tokens"] == 3
        assert usage["prompt_tokens"] == 2

    asyncio.run(collect())


def test_cancelled_request_recorded(client):
    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    async def run():
        async def noop(*args):
            pass
        with pytest.raises(asyncio.CancelledError):
            await MetricsMiddleware(cancelled)({"type": "http", "path": "/v1/chat/completions", "method": "POST", "app": app}, noop, noop)

    asyncio.run(run())
    assert app.state.panel.db.usage(0, time.time())["summary"]["cancelled"] == 1
    assert app.state.panel.active == 0


def test_browser_session_isolation(client, monkeypatch):
    from glmweb2api.panel.login import LoginSession
    first = login(client)
    session = LoginSession("private", first["x-csrf-token"], "remote")
    app.state.panel.logins.sessions[session.id] = session
    assert client.get("/panel/api/browser-login/private").status_code == 200
    login(client)
    assert client.get("/panel/api/browser-login/private").status_code == 404


def test_token_rotation_and_import_do_not_overwrite_each_other(client, monkeypatch):
    headers = login(client)
    client.post("/panel/api/accounts", headers=headers, json={"refresh_token": "token-first"})
    manager = app.state.tokens

    async def refresh(account):
        await asyncio.sleep(0.01)
        account.refresh_token = "token-rotated"
        manager._persist_rotated()
        return "access-value"

    monkeypatch.setattr(manager, "_refresh", refresh)

    async def race():
        await asyncio.gather(manager.get_access_token(), manager.add("token-new"))

    asyncio.run(race())
    assert set(app.state.store.load()) == {"token-rotated", "token-new"}


def test_usage_time_filters_and_paging(client):
    login(client)
    assert client.get("/panel/api/usage?since=100&until=1").status_code == 422
    assert client.get("/panel/api/usage?page=0").status_code == 422
    assert client.get("/panel/api/usage?since=1&until=999999999").status_code == 422
    assert client.get("/panel/api/usage").json()["official_quota"] is None


@pytest.mark.parametrize("stream", [False, True])
def test_real_upstream_path_collects_incremental_usage(client, monkeypatch, stream):
    original = httpx.AsyncClient

    def transport(request):
        if request.url.path.endswith("/guest/access"):
            return httpx.Response(200, json={"code": 0, "result": {"access_token": "test-access"}})
        events = [
            {"parts": [{"logic_id": "1", "content": [{"type": "text", "text": "ab"}]}]},
            {"parts": [{"logic_id": "1", "content": [{"type": "text", "text": "abcd"}]},
                       {"logic_id": "2", "content": [{"type": "think", "think": "思考"}]}], "status": "finish"},
        ]
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(e) for e in events) + "\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(transport)))
    login(client)
    response = client.post("/v1/chat/completions", json={"model": "glm-4", "stream": stream,
        "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": "你好"}]})
    assert response.status_code == 200
    row = client.get("/panel/api/usage").json()["rows"][0]
    assert row["outcome"] == "success"
    assert row["input_tokens"] == 2
    assert row["output_tokens"] == 3
    assert row["first_ms"] is not None
    assert row["account"] == "slot-1"


def test_bootstrap_password_restart_and_recovery(tmp_path):
    from glmweb2api.panel.auth import AdminAuth
    from glmweb2api.panel.database import Database
    db = Database(tmp_path / "panel.sqlite3")
    auth = AdminAuth(db, "", tmp_path)
    password = (tmp_path / "panel-bootstrap.txt").read_text().strip()
    assert len(password) >= 12
    assert password not in db.get("admin_password")
    assert AdminAuth(db, "", tmp_path).login(password)
    db.set("admin_password", None)
    assert AdminAuth(db, "", tmp_path).login(password)


@pytest.mark.parametrize("failure", ["eof", "timeout"])
def test_upstream_disconnect_or_timeout_is_a_failed_stream(client, monkeypatch, failure):
    original = httpx.AsyncClient

    def transport(request):
        if request.url.path.endswith("/guest/access"):
            return httpx.Response(200, json={"result": {"access_token": "test-access"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream secret diagnostic must not leak")
        return httpx.Response(200, text='data: {"parts": []}\n\n')

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(transport)))
    login(client)
    response = client.post("/v1/chat/completions", json={"model": "glm-4", "stream": True,
                                                       "messages": [{"role": "user", "content": "hello"}]})
    expected = "incomplete_stream" if failure == "eof" else "upstream_timeout"
    assert expected in response.text
    assert "secret diagnostic" not in response.text
    row = client.get("/panel/api/usage").json()["rows"][0]
    assert row["outcome"] == "failed" and row["error"] == expected


def test_token_read_browser_passes_explicit_key():
    from glmweb2api.credentials.browser import _read_token

    class Page:
        def evaluate(self, expression, key):
            assert "arguments" not in expression
            assert key == "chatglm_refresh_token"
            return "test-local-storage-value"

    assert _read_token(Page()) == "test-local-storage-value"


def test_browser_login_completion_imports_and_cleans_up(client, monkeypatch):
    import sys
    from types import SimpleNamespace
    from glmweb2api.panel.login import LoginSession

    class FakeBrowserError(Exception):
        pass

    class FakePage:
        url = "https://chatglm.cn/"

        async def goto(self, *args, **kwargs):
            pass

        def get_by_text(self, *args, **kwargs):
            raise FakeBrowserError("No optional login entry")

        async def evaluate(self, expression, key):
            return "test-browser-import-token"

    class FakeContext:
        pages = [FakePage()]

        async def new_page(self):
            return self.pages[0]

    class FakeBrowser:
        closed = False

        async def new_context(self, **kwargs):
            return FakeContext()

        async def close(self):
            self.closed = True

    browser = FakeBrowser()

    class FakePlaywright:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def chromium(self):
            return self

        async def launch(self, **kwargs):
            return browser

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(
        async_playwright=FakePlaywright, Error=FakeBrowserError))
    session = LoginSession("test-session", "test-owner", "remote")
    asyncio.run(app.state.panel.logins._run(session))
    assert session.status == "completed"
    assert app.state.store.load() == ["test-browser-import-token"]
    assert session.page is None and session.screenshot is None and session.qr_image is None
    assert browser.closed
