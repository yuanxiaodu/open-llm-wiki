from __future__ import annotations

import logging
import os
import re
import traceback
from contextlib import asynccontextmanager
from html import escape as html_escape

from agents import tracing
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import DEFAULT_SITE_NAME, PROJECT_ROOT, settings
from .db import connect, init_db, seal_orphan_agent_messages
from .observability import RequestIDMiddleware, configure_logging, init_sentry
from .plugins import get_plugins
from .routers import api
from .routers.api.auth import healthz as _healthz
from .services import document_ingest
from .wiki import repo, store

SESSION_MAX_AGE = 30 * 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET 没设。复制 .env.example 成 .env 再填一个长随机串")
    # OpenAI Agents SDK 默认把每一轮的 trace 发去 OpenAI 自己的后端（api.openai.com）。
    # 我们用的是 DeepSeek 的兼容端点，那些请求既没用也不该发——计划的验收项之一就是"抓包或
    # 日志确认进程启动后无发往 api.openai.com 的请求"。这是个进程级全局开关，启动时调
    # 一次就够；runner._run_config 每一轮还自带一道 tracing_disabled=True（测试里不起
    # lifespan 也干净），两处不冲突。放在 init_db 之前：连库都建不出来的实例也不该发 trace。
    tracing.set_tracing_disabled(True)
    init_db()
    plugins = get_plugins()
    # 裁定 R73：data/wiki 这个事实源仓库也在启动时幂等建出来，与 init_db 并列。
    # 只跑过部署、没跑过 `python -m app.cli seed` 的实例上，data/wiki 压根不存在，
    # 读侧的 git 子进程连 cwd 都进不去（/api/log 直接 500 中文信封），而这台机器
    # 本该是"能开、目录是空的"。ensure_repo 自己幂等（目录 mkdir exist_ok、
    # .git 在就跳过 init、身份 config 写同值），所以每次启动都调不会动历史。
    # 建库失败（git 不在、data 只读）就让它抛，与 init_db 同口径：事实源建不出来
    # 的实例是坏的，启动时响亮地倒下比起来后每个 wiki 请求都 500 好查。
    # 这**只**建仓库：管理员账号仍然归 cli seed。
    repo.ensure_repo()
    conn = connect()
    try:
        # S5（计划 §1/§7）：把 data/wiki 幂等地准备成 Hermes 物理 vault —— 建目录、
        # 补 SCHEMA.md/index.md/log.md、迁移旧平文件布局（一次 commit）。读接口的
        # 契约（index/schema/log/lint、path 目录树）全都以这个布局为前提，所以它和
        # ensure_repo 一样是启动必备的一步；失败就让它抛（StoreError，启动时倒下）。
        store.ensure_vault(conn)
        for plugin in plugins:
            plugin.startup(conn)
        sealed = seal_orphan_agent_messages(conn)
        conn.commit()
    finally:
        conn.close()
    if sealed:
        print(f"启动时封口了 {sealed} 条上次没写完的回复", flush=True)
    # S6c 整本书摄入：worker 只活在本进程的线程里，进程重启后不可能还有人在写那些 job。
    # 启动时把上次遗留的 running 改 interrupted（pending 保持，等用户重新发起/取消），
    # 否则前端会永远看到一个卡在 37% 的进度条。放在 init_db 之后（表已建好）。
    interrupted = document_ingest.mark_interrupted()
    if interrupted:
        print(f"启动时把 {interrupted} 个长文摄入任务标记为中断", flush=True)
    document_ingest.resume_pending()
    try:
        yield
    finally:
        for plugin in plugins:
            plugin.shutdown()


# 可观测性最小集（S12）：建 app 之前把日志与（可选的）Sentry 装好。
# 没配 SENTRY_DSN 时 init_sentry 是零网络 no-op；configure_logging 幂等。
configure_logging()
init_sentry()

# 站点名可配置（SITE_NAME）之后，文档标题不能机械地拼「· 知识库」：默认站名本来就是
# 「知识库」，拼起来会变成「知识库 · 知识库」。只有配置了真实站名（如「新传专业产品
# 经理转型指南」）时才补上"· 知识库"这个品类后缀；/docs 的标题也就与信息架构一致。
_docs_title = (
    settings.site_name
    if settings.site_name == DEFAULT_SITE_NAME
    else f"{settings.site_name} · {DEFAULT_SITE_NAME}"
)

app = FastAPI(title=_docs_title, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "unset",
    session_cookie="openllmwiki",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)

# 请求 id 中间件放在**最外层**（最后 add）：异常响应/静态文件也要带回 X-Request-ID，
# 且 access 日志要能看见所有请求。纯 ASGI、不缓冲 body，SSE 不受影响。
app.add_middleware(RequestIDMiddleware)

# Task 9 之后这是全站唯一的一组路由（auth/members/chat/wiki/search/uploads 都在
# app/routers/api/__init__.py 里聚合），旧 HTML 面已删除。
#
# S3 T6a 起 `/static` 挂载也退了（app/static 整个删掉：旧面的 style.css/app.js 与
# mock/ 三张样张都是 S1 遗留，新前端一律从 web/dist 走 /assets）。于是 /static/* 现在
# 落进下面那条 SPA 回退，拿到的是 index.html 而不是 404——裁定 R-S3-1 认这个形状：
# 那些资产已经不存在，回一个空壳页比回一个 404 更不突兀（前端路由自己会判）。
app.include_router(api.router)

# S3 的前端构建产物（`cd web && corepack pnpm build`，DEPLOY.md §2 的 rsync 清单
# 把 web/dist 一起送上机）。目录在就挂 /assets（Vite 出的静态资源），不在就什么都不挂。
# 挂载只能在 import 时做一次（StaticFiles 要目录当下就在）；下面两条路由则是每次请求
# 重新读 WEB_DIST——测试靠 monkeypatch 这个模块级名字指向临时 dist，"构建产物刚被删"
# 也不至于让进程抱着一个死路径继续 200。
WEB_DIST = PROJECT_ROOT / "web" / "dist"
if (WEB_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


# 入口页里那个静态 <title> 的坑位。DOTALL：构建产物通常是一行，但压缩器或手改都可能换行。
_TITLE_TAG = re.compile(r"<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)


def _index_page() -> HTMLResponse | None:
    """SPA 的入口页（已把 <title> 换成站点名）；前端还没构建（dist 不在）就是 None。

    每次请求重新读文件，与原来 FileResponse 的口径一致：测试靠 monkeypatch 模块级
    WEB_DIST 指到临时 dist，「构建产物刚被删／刚换过」也不该让进程抱着旧内容继续 200。
    标题在**响应时**注入而不是构建时写死：前端构建产物是各部署共享的，SITE_NAME 却是
    每台机器自己的 .env——构建期根本不知道它。html.escape 是必须的：SITE_NAME 是运维
    可填的自由文本，不转义就能被写成 `</title><script>…`，把整页结构打开一个口子。
    """
    index = WEB_DIST / "index.html"
    if not index.is_file():
        return None
    page = index.read_text(encoding="utf-8")
    # 只换第一处：入口页的 <title> 只有这一个，index.html 里的内联脚本/注释若也提到
    # "<title>" 字样，不该被顺手改动（count=1 让替换范围可数）。
    title = f"<title>{html_escape(settings.site_name)}</title>"
    return HTMLResponse(_TITLE_TAG.sub(lambda _match: title, page, count=1))


@app.get("/")
def root():
    page = _index_page()
    if page is None:
        # 只跑后端、前端还没构建的实例：/ 说一句人话，别让人对着空白页猜是服务坏了
        # 还是前端没上（S1/S2 期间的老口径，原样保留）。
        return {"note": "前端在 S3 接入，当前只有 /api/*"}
    return page


# include_in_schema=False（裁定 R-S3-3）：catch-all 是托管细节不是接口，进了 schema
# 会让 /docs 广告一条 "GET /{full_path}" 把真正的 /api/* 全淹掉；由
# tests/test_api_wiring.py 的 openapi 装配自查钉住"paths 里只有 /api/* 与基础设施"。
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    """SPA 回退：没人认领的路径一律回 index.html，由前端路由自己判。

    这是全站**最后**注册的一条路由（后面只有异常 handler，不加路由），Starlette 按
    注册顺序匹配，所以它只接得住上面所有路由都没认领的路径；新增路由必须加在它之前，
    否则会被它吃掉。

    两类路径绝不回退（守卫先按裁定 R-S3-2 归一化：p = full_path.rstrip("/")。
    尾斜杠不改变路径的身份——/api、/api/、/healthz/ 与不带斜杠的是同一个路径；
    不 rstrip 的话 "/api/"（p 之前是 "api/"，两条老守卫都不命中）会被吞成 HTML）：
    - `api`：p == "api" 或 p.startswith("api/")。/api 的契约是单一 JSON 信封
      {"error": 人话}（裁定 R11）。回一坨 HTML 只会让前端报"JSON 解析失败"，
      把"没这个接口"这句能说的话说丢；
    - `healthz`：探活打的就是它（deploy/openllmwiki-smoke），回 HTML 会让探活假绿。
      精确的 /healthz 由它自己的路由先接住、根本到不了这里；能落到这一支的只有
      /healthz/ 一类尾斜杠变体，直接返回 handler 本身的 JSON——"handler 赢"对
      变体也成立，而不是只靠注册顺序。

    api 那一支 raise 404，交给下面那条 StarletteHTTPException handler 翻成 JSON 信封
    （_wants_json 同步接受裸 "/api"，裁定 R-S3-2；非 /api 路径是 starlette 默认形状）。
    dist 不在时同样 404：没有入口页可回，比起回点别的让人以为服务正常，不如老实说没有。
    """
    p = full_path.rstrip("/")
    if p == "healthz":
        return _healthz()
    if p == "api" or p.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    page = _index_page()
    if page is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return page


def _wants_json(request: Request) -> bool:
    """/api 契约的总开关（裁定 R11 + R-S3-2）。

    前端 fetch 跟着 302 走只会拿到一个 HTML 登录页，所以 JSON 接口一律返 JSON；
    错误也只有一种信封 {"error": 人话}，不再有 detail 那一套。裸 "/api"（不带
    尾斜杠）也算 JSON 面：catch-all 对它同样 raise 404，信封键名不能漂回 detail。
    """
    path = request.url.path
    return path == "/api" or path.startswith("/api/")


def _api_error(message: str, status_code: int, headers: dict | None = None) -> JSONResponse:
    """/api 错误信封的唯一出处（裁定 R11 + R13）。

    401/403（依赖链抛的 NeedsLogin/NeedsAdmin，Task 9 起是 HTTPException 子类，
    与 400/404 一起按 MRO 命中下面那条 handler）、422（请求校验）都从这里出，
    键名不可能各写一份各自漂。
    """
    return JSONResponse({"error": message}, status_code=status_code, headers=headers)


# 信封里不许留英文（裁定 R-S3-17）：这张表翻的是**框架自己抛**的那几种状态码。
#
# 路由与服务层 raise 的 detail 早就是中文人话（"没有这个会话"、"这个会话已归档，不能再发言"…），
# 会漏出英文的只有 starlette 自己抛的那些，其中真会撞上的是 405：路径对上了、方法对不上时它抛
# HTTPException(405, "Method Not Allowed")，于是 POST /api/wiki（只有 GET）、DELETE /api/login
# （只有 POST）、乃至 POST /api/<打错的路径>（被最后那条 GET catch-all 半接住）都会把这句英文
# 原样交到前端；而前端的契约是"后端给的就是人话，不翻译"（web/src/api.ts 第 1 条），
# 于是横幅上就摆出一句 Method Not Allowed。
#
# 只翻这一档，不做"全状态码翻译表"：404 那句 "Not Found" 是 spa_fallback 自己 raise 的
# （tests/test_api_wiring.py 钉着它的信封形状，改它等于改一条已裁定的口径），其余状态码
# 全站都走中文 detail。哪天要加一档，加在这张表里，别在 handler 里散着 if。
_API_STATUS_TEXT = {405: "这个方法不行"}


# pydantic 的 loc 第一段是错误来源（body/query/path…）而不是字段名，读出来是噪音
_LOC_SOURCES = ("body", "query", "path", "header", "cookie")

# "请求体压根解不出来"的两种形状（计划 Task 5 Step 4b / M9）：
# - json_invalid：Content-Type 说是 JSON，字节却不是合法 JSON（loc 的第二段是出错的
#   **字节位置**，不是字段名，拼进人话里只是噪音）；
# - model_attributes_type：解出来的不是对象（老客户端拿表单编码打 JSON 接口就是这个，
#   loc 只剩 ("body",) 一段）——对人说来是同一件事："这接口只收 JSON"。
#   这一种只在 where 为空时翻：挂在某个字段上（loc=("body","citations")）时它的意思是
#   "这个字段该是个对象"，翻成"请求体不是 JSON"就把话说错了，那种留给老口径。
_BODY_UNPARSEABLE_TEXT = "请求体不是合法的 JSON"


def _plain_validation_error(exc: RequestValidationError) -> str:
    """把第一条校验错误拼成一句人话（裁定 R13 的信封 + 计划 Task 5 的中文兜底）。

    只取第一条：一屏 8 条 pydantic 术语对填表的人没用，先说清最要紧的那一处。
    loc 里可能出现整数（列表下标、JSON 的出错位置），一律 str 化后用点号连起来，
    开头那段"错误来源"（body/query…）去掉。

    最常见的四种形状翻成人话，其余保持"字段名: pydantic 那句话"的老口径：
    缺字段 →「缺了 <field>」；类型不对 →「<field> 的类型不对」；
    形状不对 →「<field> 的格式不对」；请求体解不出来 →「请求体不是合法的 JSON」。
    为什么只翻这几种：它们是前端真会撞上的（漏传一个键、传了 null、老客户端发表单、
    路径上写错一个 id），而 pydantic 剩下的错（too_short、greater_than、pattern 一类）
    各有各的语义，一句"类型不对"盖不住，硬翻反而把话说错——那些留着原文，字段名在前，
    人至少知道是哪个框出的问题。
    """
    errors = exc.errors()
    if not errors:
        return "请求没通过校验"
    first = errors[0]
    kind = str(first.get("type") or "")
    loc = [str(part) for part in first.get("loc", ())]
    if loc and loc[0] in _LOC_SOURCES:
        loc = loc[1:]
    where = ".".join(loc)
    if kind == "json_invalid" or (kind == "model_attributes_type" and not where):
        return _BODY_UNPARSEABLE_TEXT
    if kind == "missing":
        # loc 只剩 ("body",) 时 where 是空的：那是整个请求体没交上来，不是漏了某个字段
        return f"缺了 {where}" if where else "缺了请求体"
    if where and kind.endswith("_type"):
        # string_type / int_type / bool_type / dict_type / list_type …（pydantic 的类型错
        # 一律以 _type 结尾）。model_attributes_type 也 ending 在这儿，所以它上面先判掉了
        # （where 为空的那一支）；挂在字段上的那种落到这里，"xxx 的类型不对"也算说对了。
        return f"{where} 的类型不对"
    if where and kind.endswith("_parsing"):
        # int_parsing / float_parsing / bool_parsing：值是交上来了（一个字符串），但解不成
        # 那个类型——GET /api/chat/abc/stream 的 session_id 就是这个形状，也是整个 API 面
        # 上最容易撞到的 422（打错一个 id、前端忘了转数字）。它与"类型不对"分开说：
        # 那种是交了个别的类型的值（password: null），这种是形状不对（"abc" 不是数字），
        # 对人来说该改的地方也不一样（裁定 R-S2-24）。
        return f"{where} 的格式不对"
    message = str(first.get("msg") or kind or "请求没通过校验")
    return f"{where}: {message}" if where else message


@app.exception_handler(StarletteHTTPException)
async def http_exception(request: Request, exc: StarletteHTTPException):
    """裁定 R11：/api/* 的错误只有一种信封 —— {"error": 人话}。

    注册在 starlette 的 HTTPException 上，fastapi.HTTPException 是它的子类，
    查表按 MRO 命中同一条，所以路由里 raise 的 400/404 与依赖链的 401/403
    （deps.NeedsLogin/NeedsAdmin）共用一套键名。Task 9 之后全站只有 /api 一个面，
    非 /api 分支只剩静态挂载一类基础设施会走到，原样交回 FastAPI 默认处理。

    detail 在进信封之前过一道 _API_STATUS_TEXT：框架自己抛的那几种（今天是 405 的
    "Method Not Allowed"）翻成人话，其余原样用——路由里 raise 的本来就是中文（裁定 R-S3-17）。
    """
    if _wants_json(request):
        message = _API_STATUS_TEXT.get(exc.status_code) or str(exc.detail)
        return _api_error(message, exc.status_code, headers=getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    """裁定 R13：/api/* 的 422 也归一，与上面那条 HTTPException 同一个信封。

    注册在 fastapi 的 RequestValidationError 上——FastAPI 把默认 handler
    setdefault 在同一个键上（fastapi/applications.py:1001），所以这条直接覆盖掉
    默认的 {"detail": [一条条 pydantic 原文]}。非 /api 分支保留默认形状，
    只可能被静态挂载一类基础设施走到。
    那句话怎么写归 _plain_validation_error（计划 Task 5 Step 4b：缺字段/类型不对/
    格式不对（裁定 R-S2-24）/请求体不是 JSON 这几种翻成人话，其余保持
    "字段名: pydantic 原文"）。
    """
    if _wants_json(request):
        return _api_error(_plain_validation_error(exc), 422)
    return await request_validation_exception_handler(request, exc)


logger = logging.getLogger("openllmwiki")


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """兜底的 500（终审 I2）：任何没被路由接住的异常都在这里落地。

    /api/* 一律 {"error": "服务器出错了，请稍后再试"}——裸 traceback 不进响应体，
    只进服务端日志（uvicorn 的 error log）。非 /api 路径（静态挂载一类基础设施）
    保持 starlette 默认形状：同样的 500 纯文本 "Internal Server Error"；这条 handler
    返回后 ServerErrorMiddleware 照样会把异常 re-raise 给服务器记日志，与不注册时一致。
    """
    logger.error(
        "未处理异常 %s %s\n%s",
        request.method, request.url.path, "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    )
    if _wants_json(request):
        return _api_error("服务器出错了，请稍后再试", 500)
    return PlainTextResponse("Internal Server Error", status_code=500)
