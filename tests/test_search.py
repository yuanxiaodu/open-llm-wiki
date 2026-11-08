"""搜索 API：三组结果、上传切片、按 (kind, ref_id) 去重。

切片是承重点：592k 字符的 PDF 过去在 500_000 处被一刀截断，尾巴整段搜不到；
现在按 MAX_PIECE=400_000 切，每一片单独进 fts_shared，命中后在 search() 里
去重成一条（同 (kind, ref_id) 只留 rank 最好的一片）。

S4 T1 起 sessions 组每条另带一个 session_id：ref_id 是**消息** id（S1 终审 I8），
搜索页要跳的是**会话**。末尾三条例子把它的口径钉住——归属一次批量查完（不 N+1）、
空组一条 SQL 也不发、消息在库里没了就给 null 而不是一个必然 404 的 id。
"""

from __future__ import annotations

import sqlite3

from app.services import search
from tests.conftest import make_session


def _stage_and_send(client, session_id: int, filename: str, payload: bytes,
                    mime: str = "text/plain") -> dict:
    """两段式附件：先暂存，再随一条空正文消息发出去（解析发生在发送时）。"""
    staged = client.post("/api/uploads", files={"file": (filename, payload, mime)})
    assert staged.status_code == 200, staged.text
    upload_id = staged.json()["upload_id"]
    sent = client.post(
        f"/api/chat/{session_id}/messages",
        data={"content": "", "upload_ids": [str(upload_id)]},
    )
    assert sent.status_code == 200, sent.text
    return {"upload_id": upload_id, **sent.json()}


def test_upload_piece_slicing_and_search(client, conn, member_login):
    big = ("甲段内容 " * 100 + "\n") * 4000  # 远超 40 万字符
    tail = "尾巴上的关键句漏单异常分支"
    _stage_and_send(client, member_login.session_id, "big.txt",
                    (big + tail).encode("utf-8"))
    rows = conn.execute("SELECT piece FROM fts_shared WHERE kind='upload' ORDER BY piece").fetchall()
    assert len(rows) >= 2
    resp = client.get("/api/search?q=漏单异常分支")
    assert resp.status_code == 200
    assert resp.json()["uploads"], "尾部内容必须搜得到"


def test_search_groups(client, conn, member_login, tmp_wiki):
    from app.wiki import store

    client.post(f"/api/chat/{member_login.session_id}/messages", data={"content": "需求评审怎么开"})
    store.write_page_sync(
        conn, path=None, title="需求评审", page_type="entity", body_md="评审要拉齐口径",
        citations=[], actor="张三", action="create",
    )
    body = client.get("/api/search?q=需求评审").json()
    assert body["sessions"] and body["pages"]
    assert body["uploads"] == []


def test_search_dedupes_pieces(client, conn, member_login):
    big = ("需求评审 " * 50 + "\n") * 9000
    _stage_and_send(client, member_login.session_id, "b.txt", big.encode("utf-8"))
    body = client.get("/api/search?q=需求评审").json()
    assert len(body["uploads"]) == 1


def test_upload_ref_id_is_str_and_reindex_drops_all_pieces(client, conn, member_login):
    """裁定：ref_id 一律是 str（message/page 早就是，upload 这次改齐）；
    重建索引时 _drop 必须把这份上传的所有切片一次清干净，不留旧尾巴。"""
    big = ("切片重建 " * 100 + "\n") * 4000  # 两片以上
    upload_id = _stage_and_send(
        client, member_login.session_id, "re.txt", big.encode("utf-8")
    )["upload_id"]

    rows = conn.execute("SELECT piece, ref_id FROM fts_shared WHERE kind='upload'").fetchall()
    assert len(rows) >= 2, "超过 40 万字符必须切成多片"
    for row in rows:
        assert isinstance(row["ref_id"], str)
        assert row["ref_id"] == str(upload_id)
    assert [r["piece"] for r in rows] == list(range(len(rows))), "piece 从 0 起连续编号"

    # 重建：_drop 先按 (kind, ref_id) 清掉所有旧片，再按当前文本重切。
    # 旧片若清不干净，这里会多出重复 piece 或残留行数。
    search.index_upload(conn, upload_id)
    again = conn.execute("SELECT piece FROM fts_shared WHERE kind='upload' ORDER BY piece").fetchall()
    assert [r["piece"] for r in again] == list(range(len(again)))
    assert len(again) == len(rows)


# --- 裁定 R64：LIKE 截断、超取再收窄、不认识的 kind 跳过 -----------------------


def test_like_fallback_truncates_body(client, conn, member_login):
    """2 字查询走 LIKE 兜底：正文必须在 SQL 里就截到 200 字符。

    没有这条闸时，一片 40 万字符的上传会原样躺进响应的 snippet 里。
    """
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '999', 0, '大材料', ?)",
        ("甲乙" + "填充文字" * 100_000,),  # 40 万+ 字符
    )
    body = client.get("/api/search?q=甲乙").json()
    assert body["uploads"], "LIKE 兜底得能搜到"
    for hit in body["uploads"]:
        assert len(hit["snippet"]) <= 200, len(hit["snippet"])
    assert body["uploads"][0]["snippet"].startswith("甲乙")


def test_overfetch_then_trim_keeps_other_corpora(client, conn, member_login):
    """超取 limit*4 → 去重 → 裁到 limit：一份大材料不许饿死别的语料。

    LIKE 分支 ORDER BY ref_id DESC 是确定性的：ref_id='9' 的 10 片排在
    ref_id='1' 的消息前面。旧写法只取 limit=6 行，窗口被同一份上传的切片占满，
    去重后剩一条 upload，message 整组消失；超取到 24 行后两组都在。
    """
    for piece in range(10):
        conn.execute(
            "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
            " VALUES ('upload', '9', ?, '大材料', '甲乙丙丁戊')",
            (piece,),
        )
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('message', '1', 0, '张三', '甲乙藏在消息里')"
    )
    hits = search.search(conn, "甲乙", limit=6)
    assert {h["kind"] for h in hits} == {"upload", "message"}
    assert len(hits) <= 6


def test_route_skips_unknown_kinds(client, member_login, monkeypatch):
    """裁定 R64：路由的分组映射用 target.get(kind)，不认识的 kind 跳过而不是 KeyError。"""
    import app.routers.api.search as route_mod

    ghost = [{"kind": "ghost", "ref_id": "1", "heading": "h", "body": "b"}]
    monkeypatch.setattr(route_mod.search, "search", lambda *a, **k: ghost)
    resp = client.get("/api/search?q=随便")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "uploads": [], "pages": []}


# --- final fix wave：裁定 R82（空白 q）与 R81（reindex 计数只数真插入） ----------


def test_blank_query_returns_three_empty_groups(client, conn, member_login):
    """裁定 R82：空/纯空白 q 返 200 + 三空组，不再 LIKE '%%' 把任意 30 行端出来。"""
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('message', '1', 0, '张三', '本来会被 %% 捞出来的话')"
    )
    for q in ("", "   ", "\t"):
        resp = client.get("/api/search", params={"q": q})
        assert resp.status_code == 200, q
        assert resp.json() == {"sessions": [], "uploads": [], "pages": []}, q
    # 服务层同一个口径
    assert search.search(conn, "  ") == []


def test_reindex_counts_match_real_rows_after_parsed_file_deleted(conn, member):
    """裁定 R81：index_message/index_upload 返 bool，reindex 的计数只数真插入的行。

    恢复/回滚之后 db 与磁盘可能对不上（parsed_text_path 指着的文件没了）：计数
    若还按"遍历过几行"报，cli 的输出就是一张假绿的对账单。
    """
    from app.config import settings

    # 一份解析文本还在的上传件：该进索引
    real = settings.extracted_text_dir / "real.txt"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("能搜到的解析正文", encoding="utf-8")
    real_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, parsed_text_path, created_at)"
        " VALUES (?, '真件.pdf', 'uploads/1/a.pdf', 10, 'a', 'done', ?, '2026-10-21T00:00:00')",
        (member.user_id, str(real.relative_to(settings.data_dir))),
    ).lastrowid)
    # 一份解析文件已被删掉的上传件：一行都进不去
    gone = settings.extracted_text_dir / "gone.txt"
    gone.write_text("要消失的解析正文", encoding="utf-8")
    dead_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, parsed_text_path, created_at)"
        " VALUES (?, '丢件.pdf', 'uploads/1/b.pdf', 10, 'b', 'done', ?, '2026-10-21T00:00:00')",
        (member.user_id, str(gone.relative_to(settings.data_dir))),
    ).lastrowid)
    gone.unlink()
    # 一条空白 human 消息：也一行都进不去
    blank_id = int(conn.execute(
        "INSERT INTO messages (session_id, seq, role, user_id, content, created_at)"
        " VALUES (?, 98, 'human', ?, '   ', '2026-10-21T00:00:00')",
        (member.session_id, member.user_id),
    ).lastrowid)

    # bool 口径本身
    assert search.index_upload(conn, real_id) is True
    assert search.index_upload(conn, dead_id) is False
    assert search.index_message(conn, blank_id) is False
    assert search.index_message(conn, member.message_id) is True

    counts = search.reindex_all(conn)
    real_rows = {
        kind: conn.execute(
            "SELECT COUNT(*) AS n FROM fts_shared WHERE kind = ?", (kind,)
        ).fetchone()["n"]
        for kind in ("message", "upload", "page")
    }
    assert counts == real_rows, (counts, real_rows)
    assert counts["message"] == 1   # member 那条真话；空白的不算
    assert counts["upload"] == 1    # 真件；文件没了的不算


# --- S4 T1：sessions 组带 session_id（搜索页要直跳 /chat/{session_id}） -------------

# 那条批量查归属的 SELECT 长这样；trace 回调给的是参数已展开的语句，所以按
# "FROM messages WHERE id IN" 认它就够，还能顺手看清 IN 里到底带了几个 id。
_BATCH_SQL = "FROM messages WHERE id IN"


def _traced_sql(monkeypatch) -> list[str]:
    """记下应用连接执行过的每条 SQL（sqlite3 的 trace 回调，参数已展开）。

    用它钉两件事：一批会话命中只发一条批量 SELECT（不是每条一次 = N+1），
    以及 sessions 组为空时那条 SELECT 一次也不发。
    """
    import app.db as db_mod

    seen: list[str] = []
    real_connect = db_mod.connect

    def traced(db_path=None):
        conn = real_connect(db_path)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(db_mod, "connect", traced)
    return seen


def test_session_hits_carry_the_session_the_message_lives_in(
    client, conn, member, member_login, monkeypatch
):
    """ref_id 是**消息** id（S1 终审 I8），搜索页要跳的是**会话**：另给一个 session_id。

    两条闸：一是这个 session_id 拿去 /api/chat/{id} 真能读到那条 ref_id 的消息
    （两边对得上，链接才不是猜的）；二是三条命中只发一条 IN 批量查，不许 N+1。
    """
    seen = _traced_sql(monkeypatch)
    other_sid = make_session(conn, member.user_id, "另一场")
    for sid, text in (
        (member.session_id, "验收口径怎么定"),
        (member.session_id, "验收口径要写进合同"),
        (other_sid, "验收口径在另一场里"),
    ):
        resp = client.post(f"/api/chat/{sid}/messages", data={"content": text})
        assert resp.status_code == 200, resp.text

    hits = client.get("/api/search", params={"q": "验收口径"}).json()["sessions"]
    assert len(hits) == 3, hits
    for hit in hits:
        assert hit["session_id"] in (member.session_id, other_sid), hit
        detail = client.get(f"/api/chat/{hit['session_id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["id"] == hit["session_id"]
        assert int(hit["ref_id"]) in [m["id"] for m in body["messages"]], "ref_id 仍是消息 id"

    batch = [s for s in seen if _BATCH_SQL in s]
    assert len(batch) == 1, batch
    assert f"({','.join(h['ref_id'] for h in hits)})" in batch[0], batch[0]


def test_no_message_lookup_when_no_session_hits(client, conn, member_login, monkeypatch):
    """空 sessions 组连那条批量查询都不发：没东西要查，白跑一趟也是白跑。"""
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('page', 'p1', 0, '验收口径', '文档里写着验收口径')"
    )
    seen = _traced_sql(monkeypatch)

    body = client.get("/api/search", params={"q": "验收口径"}).json()
    assert body["sessions"] == []
    assert len(body["pages"]) == 1
    assert [s for s in seen if _BATCH_SQL in s] == []


def test_session_id_is_null_when_the_message_is_gone(client, conn, member_login):
    """索引里躺着的消息 id 库里已经没了（恢复出来的库、索引没重建）：session_id 给 null。

    前端拿到 null 就不渲染链接——比给一个必然 404 的会话 id 诚实。
    """
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('message', '424242', 0, '张三', '验收口径的幽灵命中')"
    )
    hits = client.get("/api/search", params={"q": "验收口径"}).json()["sessions"]
    assert [h["ref_id"] for h in hits] == ["424242"]
    assert hits[0]["session_id"] is None


# --- S5 fix wave：搜索加固（引号 FTS 注入 / LIKE 通配符 / 超长 q） -------------------

def test_double_quote_in_query_is_200_not_500(client, conn, member_login):
    """q 里的 `"` 必须按 FTS5 字符串字面量规则转义（`"`→`""`）。

    不转义时 `q='a"b'` 会让 `_match` 拼出的字面量提前收尾，FTS5 抛
    `unterminated string` → 整条搜索 500。转义之后它只是一个普通字符：
    既不能把查询拆成"语法"，也不能改变"逐字短语"的语义。
    """
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '77', 0, '带引号的材料', '他说 a\"b 是重点')"
    )
    for q in ('a"b', '"', 'a" or "b', 'a"b"c'):
        resp = client.get("/api/search", params={"q": q})
        assert resp.status_code == 200, (q, resp.text)
        assert set(resp.json()) == {"sessions", "uploads", "pages"}, q
    # 转义不等于把引号剔掉：字面短语照样搜得到那一行
    hits = client.get("/api/search", params={"q": 'a"b'}).json()["uploads"]
    assert [hit["ref_id"] for hit in hits] == ["77"]
    # 服务层同一个口径（路由只是透传）
    assert [hit["ref_id"] for hit in search.search(conn, 'a"b')] == ["77"]


def test_like_branch_escapes_wildcards(client, conn, member_login):
    r"""2 字以下走 LIKE：`%`/`_`/`\` 是字面量，不是通配符。

    不转义时 `q=%` 等于 `LIKE '%%'`，把整张索引端出来；`q=_` 匹配任意单字。
    """
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('message', '1', 0, '张三', '普通的一句话')"
    )
    assert client.get("/api/search", params={"q": "%"}).json() == {
        "sessions": [], "uploads": [], "pages": [],
    }
    assert client.get("/api/search", params={"q": "_"}).json() == {
        "sessions": [], "uploads": [], "pages": [],
    }
    assert client.get("/api/search", params={"q": "\\"}).json() == {
        "sessions": [], "uploads": [], "pages": [],
    }

    # 反过来：真的含这些字符的行仍然搜得到（转义不是把它们删掉）
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '7', 0, '百分号', '完成度 100%')"
    )
    hits = client.get("/api/search", params={"q": "%"}).json()["uploads"]
    assert [hit["ref_id"] for hit in hits] == ["7"]

    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '8', 0, '下划线', '字段_a 的说明')"
    )
    hits = client.get("/api/search", params={"q": "_"}).json()["uploads"]
    assert [hit["ref_id"] for hit in hits] == ["8"]

    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '9', 0, '路径', '路径 C:\\tmp 下')"
    )
    hits = client.get("/api/search", params={"q": "\\"}).json()["uploads"]
    assert [hit["ref_id"] for hit in hits] == ["9"]


def test_overlong_query_returns_empty_without_scanning(client, conn, member_login,
                                                       monkeypatch):
    """q 超长（> MAX_QUERY）直接空结果：不 500、也不把长串交给 FTS/LIKE 去扫全表。"""
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('message', '1', 0, '张三', '报名流程线上化')"
    )
    # 对照：短查询是命中的
    assert client.get("/api/search", params={"q": "报名流程"}).json()["sessions"]

    long_q = "报名流程" * 60          # 240 字
    assert len(long_q) > search.MAX_QUERY
    seen = _traced_sql(monkeypatch)
    resp = client.get("/api/search", params={"q": long_q})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessions": [], "uploads": [], "pages": []}
    # 服务层就掉头：一条查语料的 SQL 都不发（不截断、不整表扫）
    assert [sql for sql in seen if "fts_shared" in sql] == []
    assert search.search(conn, long_q) == []


def test_operational_error_while_querying_is_swallowed_as_empty():
    """最后一层兜底：查询本身抛 sqlite3.OperationalError 也按"没搜到"返回，不出 500。"""

    class Boom:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("fts5: syntax error near")

    assert search.search(Boom(), "报名流程") == []
    assert search.search(Boom(), "甲乙") == []      # LIKE 分支同样兜住


# --- 2026-10-30：对话可见范围（message_owner_id）只过滤消息命中 ----------------------


def _indexed_human(conn, session_id: int, user_id: int, content: str) -> int:
    from tests.conftest import make_human_message

    mid = make_human_message(conn, session_id, user_id, content)
    assert search.index_message(conn, mid) is True
    return mid


def test_message_owner_filter_applies_to_both_query_branches(conn, member):
    """own 模式下消息命中按 owner 过滤，FTS（3 字以上）与 LIKE（2 字）两个分支都要挡。

    「只挡一边」是这类过滤最典型的 bug：FTS 查得到、短查询走 LIKE 就漏出别人的话。
    上传材料与 wiki 页是共享知识库，不受这个参数影响。
    """
    from app.services import auth

    other_uid = auth.create_member_direct(conn, "李婷", "13700000002", "member-local-2")
    other_sid = make_session(conn, other_uid, "李婷的对话")
    mine = _indexed_human(conn, member.session_id, member.user_id, "验收口径在张三这里")
    theirs = _indexed_human(conn, other_sid, other_uid, "验收口径在李婷那里")
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '55', 0, '共享材料', '验收口径在共享材料里')"
    )

    # FTS 分支：各看各的
    assert [
        h["ref_id"] for h in search.search(
            conn, "验收口径", kinds=("message",), message_owner_id=member.user_id,
        )
    ] == [str(mine)]
    assert [
        h["ref_id"] for h in search.search(
            conn, "验收口径", kinds=("message",), message_owner_id=other_uid,
        )
    ] == [str(theirs)]

    # LIKE 分支（2 字）：同样的过滤，一个都不能漏
    assert [
        h["ref_id"] for h in search.search(
            conn, "验收", kinds=("message",), message_owner_id=member.user_id,
        )
    ] == [str(mine)]

    # 不传 owner（all 模式/管理员）：两条都在
    assert {
        h["ref_id"] for h in search.search(conn, "验收口径", kinds=("message",))
    } == {str(mine), str(theirs)}

    # 上传材料不吃这个参数
    assert [
        h["ref_id"] for h in search.search(
            conn, "验收口径", kinds=("upload",), message_owner_id=member.user_id,
        )
    ] == ["55"]


def test_message_owner_filter_short_circuits_when_messages_not_requested(conn, member):
    """kinds 里没有 message 时不拼那段子查询（纯 wiki 检索不多查一次库）。

    判据看 SQL 文本：trace 回调里不该出现 `ref_id IN` 那一段。
    """
    from app.db import connect

    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('page', 'entities/x.md', 0, '验收口径', '验收口径写在页里')"
    )
    traced = connect()
    seen: list[str] = []
    traced.set_trace_callback(seen.append)
    try:
        hits = search.search(
            traced, "验收口径", kinds=("page",), message_owner_id=member.user_id,
        )
    finally:
        traced.close()
    assert [h["ref_id"] for h in hits] == ["entities/x.md"]
    assert [sql for sql in seen if "ref_id IN" in sql] == [], seen
