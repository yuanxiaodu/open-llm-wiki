"""可观测性最小集：X-Request-ID、structlog（JSON/幂等）、Sentry（可选、零网络）。

全部不联网：测试进程的 SETTINGS 里 SENTRY_DSN 为空，init_sentry 的 no-op 分支被钉住；
"有 DSN"那条用 monkeypatch 换掉 sentry_sdk.init，只验参数与幂等。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re

import sentry_sdk
import structlog

from app import observability
from app.config import settings
from app.observability import RequestIDMiddleware, clean_request_id, new_request_id

_SAFE = re.compile(r"^[A-Za-z0-9._:@+/-]{1,128}$")


@contextlib.contextmanager
def _settings(**overrides):
    """临时改 frozen settings 的字段（退出还原）。"""
    previous = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        object.__setattr__(settings, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            object.__setattr__(settings, name, value)


@contextlib.contextmanager
def _capture_logs():
    """挂一个用真实 ProcessorFormatter 的临时 handler（StringIO），测试里读结构化输出。

    不用 pytest 的 capsys/capfd：root handler 是进程 import 时就绑到当时那个 stdout 上的，
    capture fixture 换掉 sys.stdout 之后它写到的并不是同一层缓冲。临时 handler 验的还是
    同一份 formatter（生产 handler 用的就是它）。
    """
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(observability._build_formatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)


def _json_lines(output: str) -> list[dict]:
    entries = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


# --- X-Request-ID ------------------------------------------------------------


def test_request_id_header_is_generated_and_safe(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    request_id = resp.headers.get("x-request-id")
    assert request_id and _SAFE.fullmatch(request_id), request_id


def test_client_request_id_is_reused_when_safe(client):
    resp = client.get("/healthz", headers={"X-Request-ID": "client.req:123_ok"})
    assert resp.headers["x-request-id"] == "client.req:123_ok"


def test_unsafe_or_too_long_request_id_is_replaced(client):
    for bad in ("bad id!!", "x" * 200, "semi;colon"):
        resp = client.get("/healthz", headers={"X-Request-ID": bad})
        got = resp.headers["x-request-id"]
        assert got != bad
        assert _SAFE.fullmatch(got), got


def test_clean_request_id_helper():
    assert clean_request_id("abc-123_X") == "abc-123_X"
    assert clean_request_id("x" * 128) == "x" * 128
    assert len(clean_request_id("x" * 129)) == 32
    assert _SAFE.fullmatch(clean_request_id(None))   # 没给就新生成一个安全的
    assert new_request_id() != new_request_id()


# --- structlog ---------------------------------------------------------------


def test_configure_logging_is_idempotent(monkeypatch):
    """重复调用不报错、也不叠加 handler（本模块装的 handler 始终只有一份）。"""
    monkeypatch.setattr(observability, "_LOGGING_CONFIGURED", False)
    observability.configure_logging()
    first = [h for h in logging.getLogger().handlers
             if getattr(h, "_openllmwiki_observability", False)]
    observability.configure_logging()
    observability.configure_logging()
    second = [h for h in logging.getLogger().handlers
              if getattr(h, "_openllmwiki_observability", False)]
    assert len(first) == 1 and len(second) == 1


def test_access_log_is_json_with_request_fields(client):
    """非 development 环境：一行一个 JSON，含 method/path/status/duration_ms/request_id。"""
    with _capture_logs() as stream:
        resp = client.get("/healthz", headers={"X-Request-ID": "obs-access-1"})
        assert resp.headers["x-request-id"] == "obs-access-1"
        out = stream.getvalue()
    entries = [
        entry for entry in _json_lines(out)
        if entry.get("event") == "http_request"
        and entry.get("request_id") == "obs-access-1"
    ]
    assert entries, out
    entry = entries[0]
    assert entry["method"] == "GET" and entry["path"] == "/healthz"
    assert entry["status"] == 200
    assert isinstance(entry["duration_ms"], (int, float))
    assert entry["level"] == "info" and entry["timestamp"]
    # 不记 query/正文：这里没有 query，但键集必须恰好是这几样（外加 logger/timestamp）
    assert not ({"query", "body", "headers", "cookies"} & set(entry)), entry


def test_request_context_is_cleared_after_the_request(client):
    client.get("/healthz", headers={"X-Request-ID": "ctx-clear-1"})
    assert structlog.contextvars.get_contextvars() == {}


def test_static_assets_are_skipped_in_access_log(client):
    with _capture_logs() as stream:
        client.get("/assets/does-not-exist.js", headers={"X-Request-ID": "static-skip-1"})
        out = stream.getvalue()
    entries = [
        entry for entry in _json_lines(out)
        if entry.get("event") == "http_request"
        and entry.get("path", "").startswith("/assets/")
    ]
    assert entries == [], entries


def test_stdlib_logs_inherit_request_id():
    """现有 `logging.getLogger("openllmwiki.*")` 的日志也走 JSON 且带当前 request_id。"""
    observability.configure_logging()

    async def tiny_app(scope, receive, send):
        logging.getLogger("openllmwiki.test").warning("记录一条 stdlib 日志")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    from fastapi.testclient import TestClient

    client = TestClient(RequestIDMiddleware(tiny_app))
    with _capture_logs() as stream:
        client.get("/probe", headers={"X-Request-ID": "ctx-stdlib-1"})
        out = stream.getvalue()
    entries = [
        entry for entry in _json_lines(out)
        if entry.get("logger") == "openllmwiki.test"
    ]
    assert entries, "stdlib 日志没有走结构化出口"
    entry = entries[0]
    assert entry["event"] == "记录一条 stdlib 日志"
    assert entry["level"] == "warning"
    assert entry["request_id"] == "ctx-stdlib-1"


# --- Sentry ------------------------------------------------------------------


def test_init_sentry_without_dsn_never_calls_sdk(monkeypatch):
    """DSN 为空 = 零网络：sentry_sdk.init 一次都不会被调。"""
    calls: list = []
    monkeypatch.setattr(observability, "_SENTRY_INITIALIZED", False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    with _settings(sentry_dsn=""):
        assert observability.init_sentry() is False
        assert observability.init_sentry() is False
    assert calls == []


def test_init_sentry_with_dsn_calls_once_with_safe_options(monkeypatch):
    calls: list = []
    monkeypatch.setattr(observability, "_SENTRY_INITIALIZED", False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    with _settings(
        sentry_dsn="https://public@example.invalid/1",
        environment="development",
        release="abc1234",
    ):
        assert observability.init_sentry() is True
        assert observability.init_sentry() is False       # 幂等：不再 init

    assert len(calls) == 1, calls
    kwargs = calls[0]
    assert kwargs["dsn"] == "https://public@example.invalid/1"
    assert kwargs["environment"] == "development"
    assert kwargs["release"] == "abc1234"
    assert kwargs["traces_sample_rate"] == 0.0
    assert kwargs["send_default_pii"] is False
    names = {type(integration).__name__ for integration in kwargs["integrations"]}
    assert names == {"FastApiIntegration", "StarletteIntegration"}


def test_init_sentry_without_release_omits_the_key(monkeypatch):
    calls: list = []
    monkeypatch.setattr(observability, "_SENTRY_INITIALIZED", False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    with _settings(sentry_dsn="https://public@example.invalid/1", release=""):
        assert observability.init_sentry() is True
    assert "release" not in calls[0]
