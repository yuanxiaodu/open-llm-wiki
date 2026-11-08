"""必须在 import app.* 之前设好环境变量：app/config.py 在 import 时就读 env。"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import time
from types import SimpleNamespace

_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="openllmwiki-test-"))
os.environ["DATA_DIR"] = str(_ROOT)
os.environ["WIKI_DIR"] = str(_ROOT / "wiki")
os.environ["SESSION_SECRET"] = "test-secret-not-for-production"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["LLM_API_KEY"] = "test-key"
# wiki 只读默认开（裁定 R-S4-20）：测试要用 /api/wiki 的写路由造数据，所以测试进程
# 显式关掉；只读本身在 test_api_wiki.py 里用 monkeypatch 单独钉。
os.environ["WIKI_READONLY"] = "0"
os.environ.pop("ADMIN_PHONE", None)
os.environ.pop("ADMIN_PASSWORD", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect, init_db, utcnow  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.api import chat as chat_router  # noqa: E402
from app.services import auth  # noqa: E402

TABLES = (
    "ingest_candidates",
    "ingest_jobs",
    "wiki_sync_runs",
    "wiki_sync_state",
    "site_settings",
    "citation_checks",
    "fts_shared",
    "upload_files",
    "agent_events",
    "messages",
    "chat_sessions",
    "invites",
    "users",
)

ADMIN_PHONE = "13900000001"
ADMIN_PASSWORD = "admin-pass-1"

# 真的那个触发函数（S2 Task 5：POST messages 之后 asyncio.create_task(runner.run_turn)）。
# 在 import 时抓一份引用：下面两条 fixture 用 monkeypatch 换的是模块属性，换不动这个对象。
_REAL_START_TURN = chat_router._start_turn


def _wipe(conn) -> None:
    """清空业务表。只清存在的表：迁移前后表集不同，测试两条链上都要能跑。"""
    have = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    # users.invite_id 与 invites.created_by 互相指着，清空时先关掉外键
    conn.execute("PRAGMA foreign_keys=OFF")
    for table in TABLES:
        if table in have:
            conn.execute(f"DELETE FROM {table}")
    conn.execute("PRAGMA foreign_keys=ON")
    if "site_settings" in have:
        # 迁移会给新库插一行 chat_visibility=all；清表不该把"迁移后的默认状态"也删掉，
        # 否则依赖默认值的用例要各自补写入，且 test_migration_shape 看到的和真迁移不一致。
        conn.execute(
            "INSERT INTO site_settings(key, value, updated_at, updated_by)"
            " VALUES ('chat_visibility', 'all', ?, NULL)",
            (utcnow(),),
        )
    conn.commit()


@pytest.fixture
def client():
    init_db()
    conn = connect()
    try:
        _wipe(conn)
    finally:
        conn.close()
    auth.throttle.reset()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def conn(client):
    """测试自己用的一条连接。

    依赖 client，保证库已迁移、已清空。autocommit：fixture 里裸 SQL 写的数据要
    立刻对应用请求用的另一条连接可见，不然登录一类走 HTTP 的步骤会看不到人。
    """
    connection = connect()
    connection.isolation_level = None
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def browser(client):
    """造一个全新的独立浏览器（独立 cookie）。client 留给管理员用，别混。"""

    def make() -> TestClient:
        return TestClient(app)

    return make


def make_admin(phone: str = ADMIN_PHONE, password: str = ADMIN_PASSWORD) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, phone, password_hash, role, created_at, last_login_at)"
            " VALUES ('管理员', ?, ?, 'admin', ?, ?)",
            (phone, auth.hash_password(password), utcnow(), utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


@pytest.fixture
def admin(client):
    make_admin()
    response = client.post(
        "/api/login", json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text[:400]
    return {"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD}


def login(who: TestClient, phone: str, password: str):
    response = who.post("/api/login", json={"phone": phone, "password": password})
    assert response.status_code == 200, (phone, response.status_code, response.text[:400])
    return response


def new_invite(client, name: str) -> str:
    """走管理员的成员接口生成一条邀请链接，返回 token。"""
    response = client.post("/api/members/invites", data={"name": name})
    assert response.status_code == 200, response.text[:400]
    return response.json()["token"]


def register(client, token: str, phone: str, password: str = "member-pass-1"):
    """POST /api/register，返回响应本身（成不成由调用方断言）。"""
    return client.post(
        "/api/register",
        json={"token": token, "phone": phone, "password": password, "confirm": password},
    )


def make_member(browser, admin_client, name: str, phone: str, password: str = "member-pass-1"):
    """发链接 → 用独立浏览器注册 → 返回 (浏览器, 身份)。"""
    token = new_invite(admin_client, name)
    who = browser()
    response = register(who, token, phone, password)
    assert response.status_code == 200, response.text[:400]
    return who, {"name": name, "phone": phone, "password": password}


def fetch_one(sql: str, params: tuple = ()):
    conn = connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple = ()):
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def invite_id_for(token: str) -> int:
    return fetch_one("SELECT id FROM invites WHERE token = ?", (token,))["id"]


def make_session(conn, user_id: int, title: str) -> int:
    """裸 SQL 建一场会话（app/services/chat.py 到 Task 8 才有）。"""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO chat_sessions (user_id, title, status, started_at, last_message_at)"
        " VALUES (?, ?, 'active', ?, ?)",
        (user_id, title, now, now),
    )
    return int(cur.lastrowid)


def make_human_message(conn, session_id: int, user_id: int, content: str) -> int:
    """裸 SQL 插一条 human 消息，seq 接在这条会话最后。"""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, user_id, content, created_at)"
        " VALUES (?, ?, 'human', ?, ?, ?)",
        (session_id, seq, user_id, content, utcnow()),
    )
    return int(cur.lastrowid)


@pytest.fixture
def tmp_wiki():
    """一个全新的、空的 wiki 仓库（S5 起是 Hermes vault：只有 .git，没有页面/特殊文件）。

    只建 git 仓库、不建 SCHEMA/index/log：需要特殊文件或 lint 的用例显式用 `vault`
    fixture（它调 store.ensure_vault），写作路径的用例也可以直接从零开始写页——
    store 的 `_ensure_dirs` 会把 entities/ 一类目录建出来。
    """
    from app.config import settings
    from app.wiki import repo

    if settings.wiki_dir.exists():
        shutil.rmtree(settings.wiki_dir)
    repo.ensure_repo()
    yield settings.wiki_dir


@pytest.fixture
def vault(tmp_wiki):
    """tmp_wiki + store.ensure_vault()：SCHEMA.md/index.md/log.md 与五个页面目录齐备。"""
    from app.wiki import store

    store.ensure_vault()
    return tmp_wiki


@pytest.fixture
def member(client, conn):
    """一个已建好的用户 + 会话 + 一条 human 消息；quote 是消息全文的前 6 字。"""
    from app.services import auth

    uid = auth.create_member_direct(conn, "张三", "13800000001", "member-local-1")
    sid = make_session(conn, uid, "张三的对话")
    content = "漏单降到 1 单，客服投诉也少了"
    mid = make_human_message(conn, sid, uid, content)
    return SimpleNamespace(
        user_id=uid, session_id=sid, message_id=mid, content=content,
        quote=content[:6], name="张三",
    )


def api_login(who: TestClient, phone: str, password: str) -> dict:
    """走 /api/login，返回这一身份的 cookie 快照（dict）。

    快照的用法是 `另一个浏览器.cookies.update(快照)`（见 tests/test_sessions.py 的
    _li_client）：把身份灌进一个独立 jar，client 自己的登录态不动。裁定 R69 之后
    不再走 per-request `cookies=`——starlette/httpx 已给它挂 DeprecationWarning，
    测试输出会顶一行 warnings summary。
    """
    response = who.post("/api/login", json={"phone": phone, "password": password})
    assert response.status_code == 200, response.text[:400]
    return dict(who.cookies)


@pytest.fixture
def member_login(client, conn, member):
    """张三登录在 client 本尊上：client 从此就是"张三本人"，后续调用不用带任何 cookie。"""
    api_login(client, "13800000001", "member-local-1")
    return SimpleNamespace(session_id=member.session_id, name="张三", user_id=member.user_id)


@pytest.fixture
def other_login(client, conn, browser):
    """第二个成员（李婷）：登录走独立 TestClient，绝不动 client 的 cookie jar。

    张三那条 fixture 在 client 上登录，client 从此就是"张三本人"；这里若也在 client 上
    登录，张三的 cookie 会被李婷的顶掉，Task 8 那些不带 cookies 的调用（= 本人操作）
    就全变成李婷在操作，"归属者才能改名/归档"这类断言反着也能过——测不出东西。
    所以另开一个 jar，把 cookie 以快照交出去，用的时候灌进另一个浏览器的
    client.cookies（裁定 R69：不走 per-request cookies=）。
    """
    from app.services import auth

    uid = auth.create_member_direct(conn, "李婷", "13700000002", "member-local-2")
    sid = make_session(conn, uid, "李婷的对话")
    cookies = api_login(browser(), "13700000002", "member-local-2")
    return SimpleNamespace(session_id=sid, name="李婷", user_id=uid, cookies=cookies)


# --- S2 Task 5：POST messages 会触发一轮真的智能体跑 ------------------------------


@pytest.fixture(autouse=True)
def _no_agent_run_by_default(monkeypatch):
    """默认把"POST messages 触发一轮跑"这一步换成空转（chat._start_turn）。

    S2 Task 5 起，POST /api/chat/{id}/messages 落完人话就 asyncio.create_task(runner.run_turn(…))。
    而测试环境里 DEEPSEEK_API_KEY 是假的、base_url 是**真的**（本文件顶上那几行 env），
    不拦住的话：十几条只测会话/检索/身份的用例，每条都会往真上游发一次请求（慢、脏、
    还可能真扣余额），还会往会话里塞一条 agent 行——"详情里消息条数"一类断言当场红；
    那些后台任务也可能在 TestClient 收摊时还挂着（事件循环一停就是"任务被销毁"的噪音）。

    换掉的只有起任务那一步：404/400/409 三道闸、append_human 落库、提交，全走真的。
    要真跑一轮的用例带下面那条 real_agent。

    替身收 *args/**kwargs：真 _start_turn 的签名怎么改，替身都收得下——两边不存在
    "第二份签名"可以漂移（T5 遗留的那条账），也不会哪天替身先炸一条与用例本意
    无关的 TypeError。
    """
    monkeypatch.setattr(chat_router, "_start_turn", lambda *args, **kwargs: None)


@pytest.fixture
def real_agent(monkeypatch):
    """把触发那一步换回真的（盖在 _no_agent_run_by_default 之上）。

    收尾要等在飞的那一轮落地再走：_wipe 清表之后 session_id 会重用，RUNNING 里留着
    一个 id，下一条用例的 POST 就莫名 409 了。等不到（超时）就硬清，别让一条用例的
    残局把后面的连坐——真出了这种事，那条用例自己已经红了。
    """
    monkeypatch.setattr(chat_router, "_start_turn", _REAL_START_TURN)
    yield
    deadline = time.monotonic() + 60
    while chat_router.RUNNING and time.monotonic() < deadline:
        time.sleep(0.02)
    chat_router.RUNNING.clear()
