"""迁移：0004（wiki 出数据库、会话多开全员可见、去点头流与私聊隔离）、
0006（agent_events 加 message_id）、0007（upload_files 加来源溯源两列）——
都要能在"已经灌了数据的旧库"上跑通。"""

from __future__ import annotations

import subprocess

import pytest


def test_migration_shape(client):
    import sqlite3

    from app.config import settings

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not ({"wiki_pages", "wiki_versions", "citations", "changelog"} & names)
    assert "citation_checks" not in names  # 2026-10-30 迁移 0012 已删
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
    assert "room_type" not in cols
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    assert "kind" not in cols
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(upload_files)")}
    assert {"parse_report", "source_kind", "source_url"} <= cols
    # 0010：成员建设权限 + 站点设置表。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    assert "can_build_wiki" in cols
    assert "site_settings" in names
    row = conn.execute(
        "SELECT value FROM site_settings WHERE key = 'chat_visibility'"
    ).fetchone()
    assert row is not None and row["value"] == "all"
    conn.close()


def test_migration_carries_a_populated_legacy_db(tmp_path, monkeypatch):
    """0004 必须能在"已经灌了数据的旧库"上跑通。

    上面那条 test_migration_shape 用的是空库，空库跑不出旧库的三个坑：公共房间那行
    user_id 是 NULL（新表要 NOT NULL）、DROP 父表时子表底下还压着行（runner 的连接是
    foreign_keys=ON）、公共房间里也有人话（只按 kind 过滤会留下孤儿消息）。

    这里手搓一个旧形状的库，然后两次都走**真 runner** app.db.init_db：第一次只让它看见
    0001–0003（把旧 schema 建出来），灌完数据第二次让它看见全部，于是只补跑 0004。
    不把 SQL 抄一份到测试里，免得测试和迁移各跑各的。
    """
    import shutil

    from app import db as db_module
    from app.db import connect, init_db, utcnow

    legacy_names = ("0001_init.sql", "0002_fts.sql", "0003_citations_upload.sql")
    legacy_dir = tmp_path / "legacy_migrations"
    legacy_dir.mkdir()
    for name in legacy_names:
        shutil.copy(db_module.MIGRATIONS_DIR / name, legacy_dir / name)

    db_path = tmp_path / "legacy.sqlite3"
    real_dir = db_module.MIGRATIONS_DIR
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", legacy_dir)
    try:
        init_db(db_path)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_dir)

    # 手搓旧形状的数据：一个公共房间（user_id NULL）+ 一场私有会话，
    # 公共房间里既有播报也有真人的人话，外加 wiki 三张表和一条上传材料。
    now = utcnow()
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, name, phone, password_hash, role, created_at)"
            " VALUES (1, '管理员', '13900000001', 'scrypt$x', 'admin', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO users (id, name, phone, password_hash, role, created_at)"
            " VALUES (2, '张三', '13800000001', 'scrypt$y', 'member', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO chat_sessions (id, room_type, user_id, title, status, started_at)"
            " VALUES (1, 'public', NULL, '公共房间', 'active', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO chat_sessions (id, room_type, user_id, title, status, started_at)"
            " VALUES (2, 'private', 2, '张三 的对话', 'active', ?)", (now,)
        )
        rows = [
            # (id, session_id, seq, role, kind, user_id, content)
            (1, 1, 1, "human", "reply", 2, "公共房间里张三说的人话"),
            (2, 1, 2, "agent", "broadcast", None, "文档刚改过一版，全员都看一眼"),
            (3, 2, 1, "human", "reply", 2, "私聊里说过的一句人话"),
            (4, 2, 2, "agent", "reply", None, "智能体在私聊里的回话"),
        ]
        for mid, sid, seq, role, kind, uid, content in rows:
            conn.execute(
                "INSERT INTO messages (id, session_id, seq, role, kind, user_id, content,"
                " created_at, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')",
                (mid, sid, seq, role, kind, uid, content, now),
            )
        conn.execute(
            "INSERT INTO upload_files (id, message_id, user_id, orig_filename, stored_path,"
            " size, sha256, parse_status, created_at)"
            " VALUES (1, 3, 2, '材料.pptx', 'u/1.pptx', 10, 'abc', 'none', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO wiki_pages (id, slug, title, page_type, created_at, updated_at)"
            " VALUES (1, 'topic-a', '一个话题', 'topic', ?, ?)", (now, now)
        )
        conn.execute(
            "INSERT INTO wiki_versions (id, page_id, version_no, body_md, generated_by,"
            " status, source_message_ids, verify_summary, created_at)"
            " VALUES (1, 1, 1, '# 正文', 'agent', 'live', '[3]', '{}', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO citations (id, version_id, cite_key, message_id, upload_id,"
            " char_start, char_end, quote_text, quote_sha256, verified)"
            " VALUES (1, 1, 'c1', 3, NULL, 0, 6, '私聊里说', 'sha', 1)"
        )
        conn.execute(
            "INSERT INTO changelog (id, kind, page_id, version_id, actor_user_id, action,"
            " summary, created_at) VALUES (1, 'wiki', 1, 1, 1, 'create', '建了一页', ?)", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    # 真 runner 补跑 0004：抛任何异常这条测试就红。
    init_db(db_path)

    conn = connect(db_path)
    try:
        applied = [r["name"] for r in conn.execute("SELECT name FROM _migrations")]
        assert "0004_rewrite.sql" in applied, applied

        # 公共房间那场会话没了，私有的那场原样留着（连 id 都不变）。
        sessions = {
            r["id"]: r["user_id"] for r in conn.execute("SELECT id, user_id FROM chat_sessions")
        }
        assert sessions == {2: 2}, sessions

        # 0010 的存量用户默认能建设 wiki（旧行为），站点设置默认 all。
        builders = {
            r["id"]: r["can_build_wiki"]
            for r in conn.execute("SELECT id, can_build_wiki FROM users")
        }
        assert builders == {1: 1, 2: 1}, builders
        visibility = conn.execute(
            "SELECT value FROM site_settings WHERE key = 'chat_visibility'"
        ).fetchone()
        assert visibility["value"] == "all"

        # 播报没了；公共房间里那句真人的人话也一起没了（不然它的 session_id 会变孤儿）。
        survived = {r["id"] for r in conn.execute("SELECT id FROM messages")}
        assert survived == {3, 4}, sorted(survived)

        # 私聊里那句人话活着，内容一字不动，上传材料对它的引用也没断。
        kept = conn.execute("SELECT * FROM messages WHERE id = 3").fetchone()
        assert kept["content"] == "私聊里说过的一句人话"
        assert kept["session_id"] == 2 and kept["role"] == "human" and kept["user_id"] == 2
        upload = conn.execute(
            "SELECT message_id, parse_report FROM upload_files WHERE id = 1"
        ).fetchone()
        assert upload["message_id"] == 3 and upload["parse_report"] is None

        # wiki 四张表都出去了，且没有留下任何违反外键的行。
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not ({"wiki_pages", "wiki_versions", "citations", "changelog"} & names), names
        orphans = [dict(r) for r in conn.execute("PRAGMA foreign_key_check")]
        assert orphans == [], orphans

        # 迁完的库还得能被应用正常写：外键是开着的。
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_0006_upgrades_a_db_that_already_has_events(tmp_path, monkeypatch):
    """0006 必须能在"已经灌了 agent_events 行的旧库"上跑通（裁定 R-S2-26）。

    0006 只有一句 ADD COLUMN + 一句 CREATE INDEX，看着不会错；但升级路径有它自己的坑，
    空库跑不出来：老行没有归属可补（message_id 只能是 NULL，所以那一列必须可空）、
    ADD COLUMN 不许把 UNIQUE(session_id, seq) 与 type 的 CHECK 弄丢（丢了的话 seq 会撞、
    尾读的划轮判据也就没了）、以及最要紧的一条——**尾读还得把那些 NULL 老行发出去**
    （chat._pending_events 对 NULL 行退回 created_at 口径）。所以这里手搓一个跑到 0005
    为止的库、灌上真形状的事件行，再让真 runner app.db.init_db 补跑 0006。

    与上面那条 0004 的测试同一个手法：第一次只让它看见 0001–0005，灌完数据第二次让它
    看见全部，于是只补跑 0006；不把 SQL 抄一份到测试里。
    """
    import asyncio
    import json
    import shutil
    import sqlite3

    from app import db as db_module
    from app.db import connect, init_db
    from app.routers.api import chat as chat_router
    from tests.test_stream_api import _parse_frame

    legacy_names = ("0001_init.sql", "0002_fts.sql", "0003_citations_upload.sql",
                    "0004_rewrite.sql", "0005_agent_events.sql")
    legacy_dir = tmp_path / "legacy_migrations"
    legacy_dir.mkdir()
    for name in legacy_names:
        shutil.copy(db_module.MIGRATIONS_DIR / name, legacy_dir / name)

    db_path = tmp_path / "upgraded.sqlite3"
    real_dir = db_module.MIGRATIONS_DIR
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", legacy_dir)
    try:
        init_db(db_path)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_dir)

    # 灌旧形状的真数据：一个成员、一场会话、一句人话、一条**已封口**的 agent 行
    # （usage_json='{}'，尾读的 done 判据），外加两条工具事件。agent_events 此刻
    # 还没有 message_id 这一列，所以 INSERT 也不写它——这就是 0006 之前的写侧形状。
    ts = "2026-10-22T09:00:00+00:00"
    agent_text = "我去查一下，查过了。"
    call = {"id": "c1", "name": "search", "args": "{}"}
    result = {"id": "c1", "name": "search", "ok": True, "summary": "搜到 1 条"}
    conn = connect(db_path)
    try:
        assert "message_id" not in {
            r["name"] for r in conn.execute("PRAGMA table_info(agent_events)")
        }, "0005 为止的库里不该有 message_id 这一列，这条测试的前提没了"
        conn.execute(
            "INSERT INTO users (id, name, phone, password_hash, role, created_at)"
            " VALUES (1, '张三', '13800000001', 'scrypt$x', 'member', ?)", (ts,)
        )
        conn.execute(
            "INSERT INTO chat_sessions (id, user_id, title, status, started_at, last_message_at)"
            " VALUES (1, 1, '张三的对话', 'active', ?, ?)", (ts, ts)
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, seq, role, user_id, content, created_at,"
            " truncated, usage_json) VALUES (1, 1, 1, 'human', 1, '漏单降到 1 单', ?, 0, NULL)",
            (ts,)
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, seq, role, content, created_at, truncated,"
            " usage_json) VALUES (2, 1, 2, 'agent', ?, ?, 0, '{}')", (agent_text, ts)
        )
        for seq, event_type, payload in ((1, "tool-call", call), (2, "tool-result", result)):
            conn.execute(
                "INSERT INTO agent_events (session_id, seq, type, payload_json, created_at)"
                " VALUES (1, ?, ?, ?, ?)",
                (seq, event_type, json.dumps(payload, ensure_ascii=False), ts),
            )
        conn.commit()
    finally:
        conn.close()

    # 真 runner 补跑 0006：抛任何异常这条测试就红。
    init_db(db_path)

    conn = connect(db_path)
    try:
        applied = [r["name"] for r in conn.execute("SELECT name FROM _migrations")]
        assert "0006_agent_events_message_id.sql" in applied, applied

        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(agent_events)")}
        assert "message_id" in cols, sorted(cols)
        assert cols["message_id"]["notnull"] == 0, "老行没有归属可补，这一列必须可空"
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(agent_events)")}
        assert "idx_agent_events_message_id" in indexes, indexes

        # 老行原样留着，message_id 全是 NULL
        rows = [dict(r) for r in conn.execute(
            "SELECT seq, type, payload_json, message_id FROM agent_events ORDER BY seq")]
        assert [r["seq"] for r in rows] == [1, 2], rows
        assert [r["message_id"] for r in rows] == [None, None], rows
        assert json.loads(rows[0]["payload_json"]) == call

        # ADD COLUMN 不重建表，所以原有的两道约束都得还在（丢了 seq 就会撞号）
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_events (session_id, seq, type, payload_json, created_at,"
                " message_id) VALUES (1, 1, 'system', '{}', ?, 2)", (ts,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_events (session_id, seq, type, payload_json, created_at,"
                " message_id) VALUES (1, 3, '不是这个类型', '{}', ?, 2)", (ts,)
            )
        # 迁移之后写侧的形状：新行带得上 message_id
        conn.execute(
            "INSERT INTO agent_events (session_id, seq, type, payload_json, created_at,"
            " message_id) VALUES (1, 3, 'system', '{}', ?, 2)", (ts,)
        )
        assert conn.execute(
            "SELECT message_id FROM agent_events WHERE seq = 3"
        ).fetchone()["message_id"] == 2
        conn.execute("DELETE FROM agent_events WHERE seq = 3")
        conn.commit()
    finally:
        conn.close()

    # 尾读仍要把那些 NULL 老行投递出去：走 created_at 回落（chat._pending_events 的
    # legacy 分支）。把 chat 那个 connect 指到这个升级过来的库上，其余全走真的。
    monkeypatch.setattr(chat_router, "connect", lambda: connect(db_path))

    async def collect() -> list[str]:
        return [frame async for frame in chat_router._tail(1, 0)]

    frames = [_parse_frame(raw) for raw in asyncio.run(collect())]
    assert [f["event"] for f in frames] == [
        "text-delta", "tool-call", "tool-result", "done",
    ], frames
    assert frames[0]["data"] == {"text": agent_text}
    assert frames[1]["data"] == call
    assert frames[2]["data"] == result
    assert frames[3]["data"] == {"message_id": 2, "usage": None}
def test_search_indexes_every_human_message(client, conn, member):
    """不再有 room_type 过滤：所有 human 消息都进共享检索。"""
    from app.services import search

    search.index_message(conn, member.message_id)
    row = conn.execute(
        "SELECT kind, ref_id, piece, heading, body FROM fts_shared WHERE kind = 'message'"
    ).fetchone()
    assert row is not None
    assert row["ref_id"] == str(member.message_id)
    assert str(row["piece"]) == "0"
    assert row["heading"] == "张三"
    assert row["body"] == "漏单降到 1 单，客服投诉也少了"

    hits = search.search(conn, "客服投诉")
    assert [(h["kind"], h["ref_id"]) for h in hits] == [("message", str(member.message_id))]
    assert "客服投诉" in hits[0]["body"], hits[0]["body"]


def test_search_skips_agent_messages(client, conn, member):
    from app.services import search

    agent_id = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at)"
        " VALUES (?, 2, 'agent', '智能体说的话', '2026-10-21T00:00:00+00:00')",
        (member.session_id,),
    ).lastrowid
    search.index_message(conn, member.message_id)
    search.index_message(conn, int(agent_id))
    assert conn.execute("SELECT COUNT(*) AS n FROM fts_shared").fetchone()["n"] == 1


def test_seed_initializes_llm_wiki_vault(client):
    """seed 建 data/wiki 这个 git 仓库，并把它初始化成 Hermes LLM-Wiki vault。

    旧 outline 类型已移除：seed 只保证 SCHEMA.md / index.md / log.md 三份特殊文件在位、
    被 git 跟踪，且第一次初始化有提交。
    """
    from argparse import Namespace

    from app.cli import seed
    from app.config import settings
    from tests.conftest import fetch_one

    seed(Namespace(admin_phone="13900000009", admin_password="admin-pass-9",
                   admin_name="管理员"))

    for name in ("SCHEMA.md", "index.md", "log.md"):
        assert (settings.wiki_dir / name).exists(), name
    assert not (settings.wiki_dir / "_outline_.md").exists(), "旧 outline 类型已移除，不再建大纲页"

    listed = subprocess.run(
        ("git", "ls-files"), cwd=settings.wiki_dir,
        capture_output=True, text=True, timeout=60,
    )
    assert listed.returncode == 0, listed.stderr
    tracked = set(listed.stdout.splitlines())
    assert {"SCHEMA.md", "index.md", "log.md"} <= tracked, tracked

    assert fetch_one("SELECT COUNT(*) AS n FROM users")["n"] == 1


def test_seed_is_idempotent(client):
    """再 seed 一次：三份特殊文件逐字不变、提交数不增加（ensure_vault 幂等）。"""
    from argparse import Namespace

    from app.cli import seed
    from app.config import settings

    args = Namespace(admin_phone="13900000009", admin_password="admin-pass-9",
                     admin_name="管理员")
    seed(args)
    before = {name: (settings.wiki_dir / name).read_text(encoding="utf-8")
              for name in ("SCHEMA.md", "index.md", "log.md")}
    count_before = subprocess.run(
        ("git", "rev-list", "--count", "HEAD"), cwd=settings.wiki_dir,
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    seed(args)
    after = {name: (settings.wiki_dir / name).read_text(encoding="utf-8")
             for name in ("SCHEMA.md", "index.md", "log.md")}
    count_after = subprocess.run(
        ("git", "rev-list", "--count", "HEAD"), cwd=settings.wiki_dir,
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert after == before
    assert count_after == count_before, (count_before, count_after)



def test_migration_0007_adds_source_origin_to_populated_old_db(tmp_path, monkeypatch):
    """0007：旧库补两列后旧行是 source_kind='file'/source_url=NULL，新插入可省略两列。

    与 0004 那条同一个手法：先只让 runner 看见 0001–0006 把旧 schema 建出来、灌一条
    **旧形状**的上传材料（没有两列），再让 runner 看见全部跑 0007。ADD COLUMN 带默认值
    会直接把旧行补成 'file'；新插入不写 source_kind 时也靠这个默认值兜底。
    """
    import shutil

    from app import db as db_module
    from app.db import connect, init_db, utcnow

    legacy_dir = tmp_path / "legacy_migrations"
    legacy_dir.mkdir()
    legacy_names = sorted(
        path.name for path in db_module.MIGRATIONS_DIR.glob("*.sql")
        if path.name < "0007"
    )
    assert "0006_agent_events_message_id.sql" in legacy_names, legacy_names
    for name in legacy_names:
        shutil.copy(db_module.MIGRATIONS_DIR / name, legacy_dir / name)

    db_path = tmp_path / "legacy.sqlite3"
    real_dir = db_module.MIGRATIONS_DIR
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", legacy_dir)
    try:
        init_db(db_path)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_dir)

    now = utcnow()
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, name, phone, password_hash, role, created_at)"
            " VALUES (1, '管理员', '13900000001', 'scrypt$x', 'admin', ?)", (now,)
        )
        # 旧形状：不写 source_kind/source_url 两列。
        conn.execute(
            "INSERT INTO upload_files (id, message_id, user_id, orig_filename, stored_path,"
            " size, sha256, parse_status, created_at)"
            " VALUES (1, NULL, 1, '旧材料.pptx', 'u/1.pptx', 10, 'abc', 'none', ?)", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    init_db(db_path)  # 真 runner 补跑 0007

    conn = connect(db_path)
    try:
        applied = [row["name"] for row in conn.execute("SELECT name FROM _migrations")]
        assert "0007_source_origin.sql" in applied, applied
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(upload_files)")}
        assert {"source_kind", "source_url"} <= cols

        old = conn.execute(
            "SELECT source_kind, source_url FROM upload_files WHERE id = 1"
        ).fetchone()
        assert (old["source_kind"], old["source_url"]) == ("file", None)

        # 新插入省略两列：默认值 'file'、URL NULL。
        conn.execute(
            "INSERT INTO upload_files (message_id, user_id, orig_filename, stored_path,"
            " size, sha256, parse_status, created_at)"
            " VALUES (NULL, 1, '新材料.pdf', 'u/2.pdf', 10, 'def', 'none', ?)", (now,)
        )
        conn.commit()
        new = conn.execute(
            "SELECT source_kind, source_url FROM upload_files ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (new["source_kind"], new["source_url"]) == ("file", None)
    finally:
        conn.close()


def test_migration_0008_adds_user_id_to_invites(tmp_path, monkeypatch):
    """0008：旧库给 invites 加 user_id；旧行 NULL（读侧退回姓名匹配），新 reset 行写死 user_id。"""
    import shutil

    from app import db as db_module
    from app.db import connect, init_db, utcnow
    from app.services import auth

    legacy_dir = tmp_path / "legacy_migrations"
    legacy_dir.mkdir()
    legacy_names = sorted(
        path.name for path in db_module.MIGRATIONS_DIR.glob("*.sql")
        if path.name < "0008"
    )
    assert "0007_source_origin.sql" in legacy_names, legacy_names
    for name in legacy_names:
        shutil.copy(db_module.MIGRATIONS_DIR / name, legacy_dir / name)

    db_path = tmp_path / "legacy.sqlite3"
    real_dir = db_module.MIGRATIONS_DIR
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", legacy_dir)
    try:
        init_db(db_path)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_dir)

    now = utcnow()
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, name, phone, password_hash, role, created_at)"
            " VALUES (1, '管理员', '13900000001', 'scrypt$x', 'admin', ?)", (now,)
        )
        # 旧形状的 reset 邀请：没有 user_id 列
        conn.execute(
            "INSERT INTO invites (id, token, name, purpose, created_by, used_count, created_at)"
            " VALUES (1, 'old-token', '张三', 'reset', 1, 0, ?)", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    init_db(db_path)  # 真 runner 补跑 0008

    conn = connect(db_path)
    try:
        applied = [row["name"] for row in conn.execute("SELECT name FROM _migrations")]
        assert "0008_invites_user_id.sql" in applied, applied
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(invites)")}
        assert "user_id" in cols

        old = conn.execute("SELECT user_id FROM invites WHERE token = 'old-token'").fetchone()
        assert old["user_id"] is None, "旧行没有绑定，读侧退回姓名匹配"

        # 新的 reset 邀请把 user_id 写死；注册邀请仍是 NULL
        reset_token = auth.create_reset_invite(conn, 1, 1)
        assert conn.execute(
            "SELECT user_id FROM invites WHERE token = ?", (reset_token,)
        ).fetchone()["user_id"] == 1
        register_token = auth.create_invite(conn, "李婷", 1)
        assert conn.execute(
            "SELECT user_id FROM invites WHERE token = ?", (register_token,)
        ).fetchone()["user_id"] is None
        conn.commit()
    finally:
        conn.close()


def test_migration_0009_adds_ingest_jobs_to_populated_old_db(tmp_path, monkeypatch):
    """0009：旧库补出长文摄入任务表，字段/默认值/CHECK/索引都在，且外键指向 upload_files。

    与 0004/0007 同一手法：先只让 runner 看见 0009 之前的迁移，把旧 schema 建出来并灌
    一行**旧形状**上传材料，再让 runner 看见全部补跑 0009。任务表是新建的，所以这里
    这里要钉三件事：列与默认值齐全、status 的 CHECK 白名单真生效、FK 真指向材料。
    """
    import shutil

    from app import db as db_module
    from app.db import connect, init_db, utcnow

    legacy_dir = tmp_path / "legacy_migrations"
    legacy_dir.mkdir()
    legacy_names = sorted(
        path.name for path in db_module.MIGRATIONS_DIR.glob("*.sql")
        if path.name < "0009"
    )
    assert "0007_source_origin.sql" in legacy_names, legacy_names
    for name in legacy_names:
        shutil.copy(db_module.MIGRATIONS_DIR / name, legacy_dir / name)

    db_path = tmp_path / "legacy.sqlite3"
    real_dir = db_module.MIGRATIONS_DIR
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", legacy_dir)
    try:
        init_db(db_path)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_dir)

    now = utcnow()
    conn = connect(db_path)
    try:
        assert "ingest_jobs" not in {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.execute(
            "INSERT INTO users (id, name, phone, password_hash, role, created_at)"
            " VALUES (1, '管理员', '13900000001', 'scrypt$x', 'admin', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO upload_files (id, message_id, user_id, orig_filename, stored_path,"
            " size, sha256, parse_status, created_at)"
            " VALUES (1, NULL, 1, '一份长文档.pdf', 'u/1.pdf', 10, 'abc', 'done', ?)", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    init_db(db_path)  # 真 runner 补跑 0009

    import sqlite3

    conn = connect(db_path)
    try:
        applied = [row["name"] for row in conn.execute("SELECT name FROM _migrations")]
        assert "0009_document_ingest_jobs.sql" in applied, applied

        cols = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(ingest_jobs)")
        }
        assert {
            "id", "upload_id", "actor_id", "title", "status", "total_chars",
            "chunk_size", "cursor", "chunks_total", "chunks_done", "pages_created",
            "pages_skipped", "error", "created_at", "updated_at", "finished_at",
        } <= set(cols)
        assert cols["status"]["notnull"] == 1
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(ingest_jobs)")}
        assert "idx_ingest_jobs_status" in indexes, indexes

        # 默认值：只交必填项就能插一行 pending
        root = conn.execute(
            "INSERT INTO ingest_jobs (upload_id, status, created_at, updated_at)"
            " VALUES (1, 'pending', ?, ?)", (now, now),
        )
        job_id = int(root.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT status, total_chars, chunk_size, cursor, chunks_total,"
            " chunks_done, pages_created, pages_skipped, error, actor_id, title"
            " FROM ingest_jobs WHERE id = ?", (job_id,),
        ).fetchone()
        assert (row["status"], row["total_chars"], row["chunk_size"], row["cursor"]) == (
            "pending", 0, 8000, 0,
        )
        assert (row["chunks_total"], row["chunks_done"], row["pages_created"],
                row["pages_skipped"]) == (0, 0, 0, 0)
        assert (row["error"], row["actor_id"], row["title"]) == ("", None, "")

        # status 白名单：没约定的值直接失败
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ingest_jobs (upload_id, status, created_at, updated_at)"
                " VALUES (1, 'error', ?, ?)", (now, now),
            )
        # 外键：材料必须在
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ingest_jobs (upload_id, status, created_at, updated_at)"
                " VALUES (999, 'pending', ?, ?)", (now, now),
            )
        conn.rollback()
    finally:
        conn.close()
