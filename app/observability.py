"""可观测性最小集：structlog 日志 + 可选 Sentry + 每请求 X-Request-ID。

三件事都刻意做小、做可关：

1. **日志**：`configure_logging()` 把 structlog 接到 stdlib root 上（ProcessorFormatter），
   所以应用里既有的 `logging.getLogger("openllmwiki.*")` 与新的 structlog 调用走同一条出口：
   `ENVIRONMENT=development` 时控制台可读，其它环境一行一个 JSON（timestamp/level/
   event/logger/request_id…），输出到 stdout。幂等：重复调用不叠加 handler。
2. **Sentry**：`init_sentry()` 只在 `SENTRY_DSN` 非空时初始化一次；空 DSN 是**零网络**
   no-op（测试/本地默认就是这条）。`traces_sample_rate=0.0`（只要错误，不做性能采样）、
   `send_default_pii=False`；5xx 由 SDK 的 FastAPI/Starlette 集成自动捕获，我们不再
   重复 capture。DSN 不写日志。
3. **请求 id**：纯 ASGI 中间件 `RequestIDMiddleware` 给每个 HTTP 请求生成/复用
   `X-Request-ID`（只接受安全字符、≤128 长度，否则重新生成），绑进 structlog
   contextvars（同请求内的日志都带 request_id），响应头回写；请求结束清上下文，
   记一条 access 日志（method/path/status/duration_ms/request_id，不记 query/正文）。
   纯 ASGI 一层透传，不缓冲 body —— SSE 流式不受影响。
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid

import structlog

from .config import settings

logger = logging.getLogger("openllmwiki.observability")

# 幂等标记：configure_logging / init_sentry 各自只生效一次。
_LOGGING_CONFIGURED = False
_SENTRY_INITIALIZED = False

# 允许客户端带过来的 request id 形状：只收常见安全字符，避免把换行/控制字符写进日志
# 或响应头。长度上限 128（任务口径）。
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:@+/-]{1,128}$")
_REQUEST_ID_HEADER = "x-request-id"

# access 日志跳过的静态前缀（内容分发不是业务访问，量大且无排查价值）。
_SKIP_PATH_PREFIXES = ("/assets/", "/static/")


def new_request_id() -> str:
    """生成一个安全的 request id（32 位十六进制）。"""
    return uuid.uuid4().hex


def clean_request_id(raw: str | None) -> str:
    """复用客户端给的 id（合法时），否则新生成。只接受安全字符且 ≤128。"""
    value = (raw or "").strip()
    return value if _REQUEST_ID_RE.fullmatch(value) else new_request_id()


def bind_request_context(request_id: str) -> None:
    """把 request_id 绑进 structlog contextvars：同一请求里所有日志自动带上它。"""
    structlog.contextvars.bind_contextvars(request_id=request_id)


def _renderer():
    """development 用可读的控制台行，其它环境用一行一个的 JSON。"""
    if settings.environment == "development":
        return structlog.dev.ConsoleRenderer(colors=False)
    return structlog.processors.JSONRenderer(ensure_ascii=False)


def _build_formatter() -> structlog.stdlib.ProcessorFormatter:
    """控制台/JSON 的那个 ProcessorFormatter（测试也用它挂临时 handler 验格式）。"""
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(),
        ],
    )


def configure_logging() -> None:
    """配置 structlog + stdlib root handler（stdout）。幂等：第二次调用直接返回。

    stdlib 记录经 `ProcessorFormatter` 走同一套 processors（含 contextvars），所以现有
    `logging.getLogger("openllmwiki.*")` 的日志也会是 JSON、也带 request_id——不必把全项目
    的 logger 换成 structlog。pytest 的 caplog 仍能按 logger 名抓到记录（它挂在自己
    的 handler 上，与这里的 root handler 并存）。
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = _build_formatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    # 本模块装的 handler 打个标记：幂等重入时只留一份（进程内理论上只会成功装一次，
    # 标记让"配置被别处重置"的情况下也能干净地补装而不叠加）。
    handler._openllmwiki_observability = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_openllmwiki_observability", False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    _LOGGING_CONFIGURED = True


def init_sentry() -> bool:
    """按需初始化 Sentry；返回是否真的初始化了。

    - DSN 为空：直接 no-op（返回 False），进程**不发任何网络请求**；
    - 已初始化过：再调直接返回 False（幂等，测试里可以放心重复调）；
    - DSN 非空：`sentry_sdk.init(...)` 一次；DSN 不进日志。
    """
    global _SENTRY_INITIALIZED
    if _SENTRY_INITIALIZED:
        return False
    dsn = settings.sentry_dsn.strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        kwargs: dict = {
            "environment": settings.environment,
            "traces_sample_rate": 0.0,      # 只要错误，不采性能
            "send_default_pii": False,      # 不把手机号/请求体一类的 PII 发出去
            "integrations": [FastApiIntegration(), StarletteIntegration()],
        }
        if settings.release:
            kwargs["release"] = settings.release
        sentry_sdk.init(dsn=dsn, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 观测性组件坏了不许拖垮站点启动
        logger.warning("Sentry 初始化失败，站点继续运行（不会重试）：%r", exc)
        return False
    _SENTRY_INITIALIZED = True
    return True


class RequestIDMiddleware:
    """纯 ASGI：生成/复用 X-Request-ID、绑定日志上下文、回写响应头、记 access 日志。

    用纯 ASGI 而不是 BaseHTTPMiddleware 是为了**不缓冲/不改写 body**：SSE 的
    `http.response.body` 帧原样透传，请求 id 只改 `http.response.start` 的 headers。
    请求结束（正常/异常都算）清 structlog 上下文，避免串到下一条请求。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(_REQUEST_ID_HEADER.encode())
        request_id = clean_request_id(raw.decode("latin-1") if raw else None)
        bind_request_context(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        start = time.perf_counter()
        status = 500

        async def send_with_request_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                raw_headers = [
                    (name, value) for name, value in message.get("headers", [])
                    if name.lower() != _REQUEST_ID_HEADER.encode()
                ]
                raw_headers.append((_REQUEST_ID_HEADER.encode(), request_id.encode("latin-1")))
                message = {**message, "headers": raw_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path = scope.get("path") or ""
            if not path.startswith(_SKIP_PATH_PREFIXES):
                structlog.get_logger("openllmwiki.access").info(
                    "http_request",
                    method=scope.get("method", ""),
                    path=path,
                    status=status,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                    request_id=request_id,
                )
            structlog.contextvars.clear_contextvars()
