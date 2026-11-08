"""会话：多开、全员可见可发言、归属者才能改名归档。

五条测试各钉一件事（简报逐字给）：
1. 列表是全站会话，带 owner_name——没有私聊隔离（裁定 5）；
2. 谁都能往别人的会话里发言，消息记的是发言人的名字；
3. 标题由首条消息的前 20 字自动生成（仅当标题为空）；
4. 改名/归档只认归属者，别人来是 403；
5. 归档之后语料还在检索里（也还能被引注，见 verify_quote 不查 status）。

跨成员那几条靠 conftest 的 other_login：它在独立 TestClient 上登录，把李婷的 cookie
以快照交出来。用的时候经 _li_client 把快照灌进另一个浏览器的 client.cookies（裁定 R69：
不再走 per-request `cookies=`——starlette/httpx 已给它挂 DeprecationWarning，测试输出
不该有噪声）；`client` 始终是张三本人，两个 jar 互不干扰。
文件末尾另有五条补充测试钉住实现对简报的三处偏离与没覆盖的路径，再往下是 fix round 1
为 R67（归档只读）、R68（并发发言的 seq 原子性）补的测试，最后是 S3 T6a 为 R-S3-8
（详情里每条 agent 消息附它这一轮的 agent_events）补的四条。
"""

from __future__ import annotations


def _li_client(browser, other_login):
    """李婷登录的独立 TestClient：cookie 设在 client 对象上，不再 per-request（裁定 R69）。

    服务端只认签名过的 session cookie、不认客户端实例，所以把 other_login 交出来的
    cookie 快照灌进一个新 browser 的 jar，与旧的 `cookies=快照` 写法完全等效——只是
    httpx 不再对每次请求挂 "cookie persistence is ambiguous" 的 DeprecationWarning。
    张三那条 `client` 的 jar 一点不动，两个身份各用各的客户端，断言不会互相串。
    """
    who = browser()
    who.cookies.update(other_login.cookies)
    return who


def test_create_and_list_shows_everyone(client, conn, member_login, other_login, browser):
    li = _li_client(browser, other_login)
    mine = client.post("/api/chat/sessions").json()["id"]
    theirs = li.post("/api/chat/sessions").json()["id"]
    body = client.get("/api/chat/sessions").json()
    ids = {s["id"] for s in body}
    assert {mine, theirs} <= ids
    by_id = {s["id"]: s for s in body}
    assert by_id[theirs]["owner_name"] == other_login.name


def test_anyone_can_post_into_any_session(client, conn, member_login, other_login, browser):
    li = _li_client(browser, other_login)
    sid = client.post("/api/chat/sessions").json()["id"]
    resp = li.post(f"/api/chat/{sid}/messages", data={"content": "我来补一句"})
    assert resp.status_code == 200
    body = client.get(f"/api/chat/{sid}").json()
    assert body["messages"][-1]["content"] == "我来补一句"
    assert body["messages"][-1]["user_name"] == other_login.name


def test_first_message_sets_title(client, conn, member_login):
    sid = client.post("/api/chat/sessions").json()["id"]
    client.post(f"/api/chat/{sid}/messages", data={"content": "职教创业公司实习复盘很长的一段话"})
    body = client.get("/api/chat/sessions").json()
    assert next(s for s in body if s["id"] == sid)["title"] == "职教创业公司实习复盘很长的一段话"[:20]


def test_rename_and_archive_owner_only(client, conn, member_login, other_login, browser):
    li = _li_client(browser, other_login)
    sid = client.post("/api/chat/sessions").json()["id"]
    assert li.patch(f"/api/chat/{sid}", json={"title": "新名"}).status_code == 403
    assert client.patch(f"/api/chat/{sid}", json={"title": "新名"}).status_code == 200
    assert li.post(f"/api/chat/{sid}/archive").status_code == 403
    assert client.post(f"/api/chat/{sid}/archive").status_code == 200
    body = client.get("/api/chat/sessions").json()
    assert next(s for s in body if s["id"] == sid)["status"] == "archived"


def test_unarchive_owner_only_idempotent_and_returns_to_active(
    client, conn, member_login, other_login, browser
):
    """归档可逆（2026-10-30）：归属者能取消归档，回到 active；别人 403；重复调幂等。"""
    li = _li_client(browser, other_login)
    sid = client.post("/api/chat/sessions").json()["id"]
    assert client.post(f"/api/chat/{sid}/archive").status_code == 200

    # 非归属者不能取消归档
    assert li.post(f"/api/chat/{sid}/unarchive").status_code == 403
    # 归属者取消归档
    assert client.post(f"/api/chat/{sid}/unarchive").status_code == 200
    body = client.get("/api/chat/sessions").json()
    assert next(s for s in body if s["id"] == sid)["status"] == "active"
    # 幂等：再调一次还是 200、还是 active
    assert client.post(f"/api/chat/{sid}/unarchive").status_code == 200
    assert conn.execute(
        "SELECT status FROM chat_sessions WHERE id = ?", (sid,)
    ).fetchone()["status"] == "active"


def test_unarchive_missing_session_is_404(client, member_login):
    assert client.post("/api/chat/987654/unarchive").status_code == 404


def test_archived_session_still_searchable_and_citable(client, conn, member_login):
    sid = client.post("/api/chat/sessions").json()["id"]
    client.post(f"/api/chat/{sid}/messages", data={"content": "漏单降到 1 单"})
    client.post(f"/api/chat/{sid}/archive")
    assert client.get("/api/search?q=漏单降到").json()["sessions"]


# ------------------------------------------- 简报五条之外：钉住偏离与没覆盖到的路径
# 上面五条是简报逐字给的。下面这几条钉的是本次实现对简报的三处偏离，以及简报没
# 覆盖的两条路径（附件元数据、归档后可引注）。偏离不写测试，下一次重构就会悄悄退回去。


def test_new_session_lists_first(client, conn, member_login):
    """偏离①：「＋新会话」建出来的那条要排在列表最前，不是掉到最底。

    create_session 只写 started_at，last_message_at 仍是 NULL（还没人说话，这一列
    不该撒谎），而 SQLite 的 DESC 把 NULL 排在最后。list_sessions 因此按
    COALESCE(last_message_at, started_at) 倒序（简报是裸 last_message_at）。
    member_login 那场会话建得更早，所以这里不比时间也不该输。
    """
    sid = client.post("/api/chat/sessions").json()["id"]
    ids = [s["id"] for s in client.get("/api/chat/sessions").json()]
    assert ids[0] == sid


def test_unknown_session_is_404_not_403(client, conn, member_login):
    """偏离②：不存在的会话一律 404；403 只留给"会话在、人不对"。

    简报的 _require_owner 把"查无此会话"也并进了 NotOwner（→403），于是改一个不存在
    的 id 会答"只有开这个会话的人能改名"——一句关于不存在的东西的谎话，前端也没法
    据此提示"这条会话没了"。路由先判存在，与详情/发消息/上传三条同口径。
    """
    responses = (
        client.get("/api/chat/987654"),
        client.patch("/api/chat/987654", json={"title": "新名"}),
        client.post("/api/chat/987654/archive"),
        client.post("/api/chat/987654/messages", data={"content": "有人吗"}),
    )
    for resp in responses:
        assert resp.status_code == 404, (resp.status_code, resp.text)
        assert list(resp.json()) == ["error"]  # /api 只有一种错误信封


def test_blank_title_is_400(client, conn, member_login):
    """偏离③：改名不接受空标题。

    空标题会把 append_human 里那半条 `CASE WHEN title = ''` 重新武装起来：人明明改过名，
    下一条消息又把标题改成它的前 20 字。60 字仍是截断而不是报错（多打的字丢掉无伤）。
    """
    sid = client.post("/api/chat/sessions").json()["id"]
    client.post(f"/api/chat/{sid}/messages", data={"content": "先说一句话"})
    resp = client.patch(f"/api/chat/{sid}", json={"title": "   "})
    assert resp.status_code == 400, resp.text
    assert list(resp.json()) == ["error"]
    title = conn.execute(
        "SELECT title FROM chat_sessions WHERE id = ?", (sid,)
    ).fetchone()["title"]
    assert title == "先说一句话", "空改名不许把已有标题抹掉"

def test_detail_embeds_attachment_metadata(client, conn, member_login):
    """详情里的附件元数据（简报五条一次都没碰过这条路径）。

    只给前端要显示的五列：id / orig_filename / size / parse_status / parse_report。
    parse_report 与上传响应同形（对象，不是库里的 JSON 字符串）——同一个字段两种形状，
    S3 就得记着在哪一处 JSON.parse。stored_path 与 sha256 不给：存盘名是 uuid，
    泄出去只是让人能猜目录结构；原件也不提供下载（2026-10-30 裁定）。
    """
    sid = client.post("/api/chat/sessions").json()["id"]
    staged = client.post(
        "/api/uploads",
        files={"file": ("周记.txt", "上线首月漏单降到 1 单".encode("utf-8"), "text/plain")},
    )
    assert staged.status_code == 200, staged.text
    upload_id = staged.json()["upload_id"]
    sent = client.post(
        f"/api/chat/{sid}/messages",
        data={"content": "", "upload_ids": [str(upload_id)]},
    )
    assert sent.status_code == 200, sent.text

    body = client.get(f"/api/chat/{sid}").json()
    (att,) = body["messages"][-1]["attachments"]
    assert att["id"] == upload_id
    assert att["orig_filename"] == "周记.txt"
    assert att["parse_status"] == "done"
    assert att["size"] > 0
    report = att["parse_report"]
    assert report, "txt 也该有页数报告，否则这条测试测不到解码"
    assert "stored_path" not in att and "sha256" not in att


# ------------------------------------------------- fix round 1：R67 归档只读、R68 并发 seq
# 上面 test_archived_session_message_still_citable 已钉住"归档后仍可被引注"（verify_quote
# 是服务层调用，不经路由的归档闸，所以只读拦写不拦引注）。下面补的是 R67 的两条写路由
# 各返 400、以及 R68 的并发 seq 原子性。


def test_archived_session_rejects_new_message(client, conn, member_login):
    """裁定 R67：归档的会话只读——POST messages 返 400，且新消息一个字都不落库。

    spec 对"归档后还能不能发言"沉默，R67 按"删除=软归档"的直觉裁成只读。闸放在路由
    （_require_writable），不放进 append_human——S2 的智能体回复也走 append_human，
    在服务层拦会连"给归档会话补一条智能体回复"一起堵死。
    """
    sid = client.post("/api/chat/sessions").json()["id"]
    client.post(f"/api/chat/{sid}/messages", data={"content": "归档前的一句话"})
    assert client.post(f"/api/chat/{sid}/archive").status_code == 200

    resp = client.post(f"/api/chat/{sid}/messages", data={"content": "还想再说一句"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "这个会话已归档，不能再发言"}

    body = client.get(f"/api/chat/{sid}").json()
    assert len(body["messages"]) == 1, "被拒的消息不许落库"
    assert body["messages"][0]["content"] == "归档前的一句话"


def test_archived_session_rejects_upload(client, conn, member_login):
    """裁定 R67：归档的会话只读——旧的 POST upload 兼容壳同样返 400，不留附件。

    S11 起这条壳只暂存，闸仍然挡在最前面；归档会话的附件发送由 POST messages
    的 _require_writable 拦（同一条 400 人话）。
    """
    sid = client.post("/api/chat/sessions").json()["id"]
    client.post(f"/api/chat/{sid}/messages", data={"content": "归档前的一句话"})
    assert client.post(f"/api/chat/{sid}/archive").status_code == 200

    resp = client.post(
        f"/api/chat/{sid}/upload",
        files={"file": ("周记.txt", "上线首月漏单降到 1 单".encode("utf-8"), "text/plain")},
        data={"note": ""},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "这个会话已归档，不能再发言"}

    body = client.get(f"/api/chat/{sid}").json()
    assert len(body["messages"]) == 1, "被拒的上传不许追加播报消息"
    assert body["messages"][0]["content"] == "归档前的一句话"
    assert body["messages"][0]["attachments"] == [], "也不许留下附件"


def test_concurrent_appends_get_distinct_seq(conn, member):
    """裁定 R68：8 条线程同时往同一场会话发言，seq 互异、不撞 UNIQUE、不出 500。

    旧的两步（先 SELECT MAX(seq) 再 INSERT）两语句间没有锁；"全员都能往任何会话发言"
    变成一等公民之后，多人挤一场会话就会读到同一个 seq，第二个 INSERT 撞
    UNIQUE(session_id, seq)。改成单条 INSERT ... SELECT COALESCE(MAX(seq),0)+1
    （读 MAX 与写 INSERT 在同一条写语句、同一把写锁内完成）即消此竞态。这里用
    threading.Barrier 让 8 条独立连接同时起跑，每条各自 connect()、各自 commit()，
    模拟线程池里"一次请求一条连接"的真实并发。
    """
    import threading

    from app.db import connect
    from app.services import chat as svc

    sid = svc.create_session(conn, member.user_id)
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        c = None
        try:
            barrier.wait(timeout=10)
            c = connect()
            svc.append_human(c, sid, member.user_id, f"并发第 {i} 句")
            c.commit()
        except BaseException as exc:  # noqa: BLE001 —— 要把并发里的任何异常收出来断言
            errors.append(exc)
        finally:
            if c is not None:
                c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发发言出错（旧两步会在这里撞 UNIQUE）：{errors!r}"
    seqs = [
        r["seq"]
        for r in conn.execute(
            "SELECT seq FROM messages WHERE session_id = ? ORDER BY seq", (sid,)
        ).fetchall()
    ]
    assert seqs == list(range(1, n + 1)), f"8 条 seq 应是 1..8 且互异：{seqs}"


def test_message_over_content_cap_is_400(client, conn, member_login):
    """裁定 R86：正文超 search.MAX_BODY（20000 字）是 400 人话，不截断入库。

    FTS 一行只索引前 20000 字：存进去的比索引到的多，引注校验（读 messages 全文）
    与检索（读截断正文）就各看各的一半。掐着上限的那一条照旧 200。
    """
    from app.services import search

    sid = member_login.session_id
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,)
    ).fetchone()["n"]

    resp = client.post(
        f"/api/chat/{sid}/messages", data={"content": "长" * (search.MAX_BODY + 1)}
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error and not error.isascii() and str(search.MAX_BODY) in error
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,)
    ).fetchone()["n"] == before  # 一个字都没入库

    ok = client.post(
        f"/api/chat/{sid}/messages", data={"content": "长" * search.MAX_BODY}
    )
    assert ok.status_code == 200, ok.text[:200]


# ------------------------------------------------- S3 T6a：详情附 agent_events（R-S3-8）
# 这四条钉的是 GET /api/chat/{id} 的 events 那一半：SSE 流上的 tool-call/tool-result/
# system/error 帧原来只活在流里，刷新页面之后工具卡片就没了。落库的行本来就在
# agent_events 里，详情按 message_id 把它们交出去，前端用同一套帧语义渲染历史回合。
#
# 事件行一律**裸写**（或直接调 hooks.append_event），不跑真模型：这一半测的是接口形状，
# 而"谁在什么时候写事件"由 tests/test_agent_hooks.py 与 tests/test_stream_api.py 钉着。


def _agent_row(conn, session_id: int, content: str = "好，已经并进《报名流程线上化》。") -> int:
    """裸 SQL 补一条已封口的 agent 行（usage_json='{}' 就是封口记号，见 runner.DONE_USAGE）。"""
    from app.db import utcnow

    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
        " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', ?, ?, 0, '{}'"
        " FROM messages WHERE session_id = ?",
        (session_id, content, utcnow(), session_id),
    )
    return int(cur.lastrowid)


def _event_row(conn, session_id: int, seq: int, event_type: str, payload_json: str,
               message_id: int | None) -> None:
    """裸 SQL 写一条事件，seq 由调用方指定（顺序那条测试要故意把 rowid 与 seq 拧开）。"""
    from app.db import utcnow

    conn.execute(
        "INSERT INTO agent_events (session_id, seq, type, payload_json, created_at, message_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, seq, event_type, payload_json, utcnow(), message_id),
    )


def test_detail_attaches_the_turns_events_under_the_agent_message(
    client, conn, member_login
):
    """一轮 tool-call + tool-result：详情里它们挂在那条 agent 消息上，human 行是空列表。

    payload 是**解码后的对象**（与 parse_report 同一条纪律）：库里是 JSON 文本，
    接口交对象，前端不必记着在哪一处 JSON.parse。事件用 hooks.append_event 写，
    顺带钉住"接口交出去的键就是 hooks 落库那两个键"（type / payload）。
    """
    from app.agent import hooks

    sid = member_login.session_id
    mid = _agent_row(conn, sid)
    hooks.append_event(
        conn, sid, "tool-call",
        {"id": "call_9a1", "name": "search", "args": '{"q": "试听 预约 漏单"}'},
        message_id=mid,
    )
    hooks.append_event(
        conn, sid, "tool-result",
        {"id": "call_9a1", "name": "search", "ok": True, "summary": "命中 4 条"},
        message_id=mid,
    )

    resp = client.get(f"/api/chat/{sid}")
    assert resp.status_code == 200, resp.text[:300]
    msgs = resp.json()["messages"]
    human = next(m for m in msgs if m["role"] == "human")
    agent = next(m for m in msgs if m["role"] == "agent")

    assert human["events"] == [], "human 行不该挂事件，也不该缺这个键（形状恒定）"
    assert [e["type"] for e in agent["events"]] == ["tool-call", "tool-result"]
    assert agent["events"][0]["payload"] == {
        "id": "call_9a1", "name": "search", "args": '{"q": "试听 预约 漏单"}'
    }
    assert agent["events"][1]["payload"] == {
        "id": "call_9a1", "name": "search", "ok": True, "summary": "命中 4 条"
    }


def test_detail_events_are_ordered_by_seq_not_by_rowid(client, conn, member_login):
    """events 按 **seq** 升序，不按插入顺序/rowid。

    seq 是流上当时的到达顺序，前端就是按这个顺序折工具卡片与横幅的；按 rowid 排在
    "补写/回补"之后就会与当时看到的不一致。这里故意先写 seq=2 再写 seq=1，
    两者 rowid 与 seq 拧开，ORDER BY seq 拿掉这条就红。
    """
    import json

    sid = member_login.session_id
    mid = _agent_row(conn, sid)
    _event_row(conn, sid, 2, "tool-result",
               json.dumps({"id": "c1", "name": "search", "ok": True, "summary": "后到的结果"}),
               mid)
    _event_row(conn, sid, 1, "tool-call", json.dumps({"id": "c1", "name": "search"}), mid)

    msgs = client.get(f"/api/chat/{sid}").json()["messages"]
    agent = next(m for m in msgs if m["role"] == "agent")
    assert [e["type"] for e in agent["events"]] == ["tool-call", "tool-result"]


def test_detail_events_stay_inside_their_own_message(client, conn, member_login):
    """两条边界：别的事件不许挂进来。

    1. 归**另一条**消息的行（上一轮/下一轮）不出现在这一条上——挂错就是把别人的
       工具卡片钉进这一轮的记录里；
    2. `message_id IS NULL` 的老行（迁移 0006 之前落的）谁的都不挂。SSE 尾读对 NULL 行
       有 created_at 的兜底口径，那是为了直播不断片；历史这边拿时间去猜归属，
       猜错的代价比不显示大。
    """
    import json

    sid = member_login.session_id
    first = _agent_row(conn, sid, "第一轮。")
    second = _agent_row(conn, sid, "第二轮。")
    _event_row(conn, sid, 1, "tool-call", json.dumps({"id": "c1", "name": "search"}), first)
    _event_row(conn, sid, 2, "tool-call", json.dumps({"id": "c0", "name": "read_page"}), None)

    msgs = client.get(f"/api/chat/{sid}").json()["messages"]
    agents = [m for m in msgs if m["role"] == "agent"]
    assert len(agents) == 2, agents
    assert [e["payload"]["id"] for e in agents[0]["events"]] == ["c1"]
    assert agents[1]["events"] == [], "没有归它的事件就该是空列表"


def test_detail_survives_an_unreadable_event_payload(client, conn, member_login):
    """payload_json 坏了（不是 JSON、或不是对象）：详情照样 200，那一行的 payload 是 null。

    一条详情接口不该因为一列脏数据整页 500（与 _parse_report 同口径）。null 也不是把
    这行悄悄抹掉：前端按 sse.ts 那套形状校验会把它认成坏帧、摆一条「服务端帧坏了」，
    与流上收到坏 payload 时同一句话。
    """
    sid = member_login.session_id
    mid = _agent_row(conn, sid)
    _event_row(conn, sid, 1, "tool-call", "{这不是 JSON", mid)
    _event_row(conn, sid, 2, "system", "[1, 2]", mid)          # 是 JSON，但不是对象
    _event_row(conn, sid, 3, "system", '{"text": "额度暂时用完"}', mid)

    resp = client.get(f"/api/chat/{sid}")
    assert resp.status_code == 200, resp.text[:300]
    agent = next(m for m in resp.json()["messages"] if m["role"] == "agent")
    assert [e["payload"] for e in agent["events"]] == [None, None, {"text": "额度暂时用完"}]
    assert [e["type"] for e in agent["events"]] == ["tool-call", "system", "system"]


def test_list_sessions_no_longer_carries_the_last_outcome(client, conn, member_login):
    """对话列表不再暴露操作计数（2026-10-30 裁定）：last_outcome 从列表里退场。

    委托人的原话是"不要在对话列表显示 3 页 · 11 引注这样的提示，这样的内容应该放到
    日志中"。计数本身没删（runner 照旧写 system{kind:"outcome"} 事件），只是不再随
    GET /api/chat/sessions 下发；管理员日志页走 GET /api/log/outcomes（见 test_api_wiki）。
    这里同时钉两半：列表项里没有这个键（不是 None），服务层仍读得出这份计数。
    """
    from app.agent.hooks import append_event
    from app.services import chat as chat_service

    sid = client.post("/api/chat/sessions").json()["id"]
    append_event(
        conn, sid, "system",
        {"kind": "outcome",
         "pages": [{"path": f"concepts/p{i}.md", "action": "create"} for i in range(3)],
         "citations": 18, "lint_errors": 0},
        message_id=0,
    )
    body = client.get("/api/chat/sessions").json()
    item = next(s for s in body if s["id"] == sid)
    assert "last_outcome" not in item, "计数属于管理员日志页，不该再随对话列表下发"

    # 事件没删：管理员日志页要的正是这份计数
    outcomes = {row["id"]: row["outcome"] for row in chat_service.list_session_outcomes(conn)}
    assert outcomes[sid] == {"pages": 3, "citations": 18, "lint_errors": 0}


# ---------------------------------- 2026-10-30：对话可见范围（site_settings.chat_visibility）
# own 模式下成员只看得到自己的会话：列表过滤、七条入口一律 404（不泄漏"它存在"）、
# 搜索的消息命中按 owner 过滤；管理员始终看全部。all 模式（默认）与迁移前完全一致，
# 上面那些老用例就是它的回归网。
#
# site_settings 不在 conftest._wipe 的清表清单里，所以这里每次都要还原——不还的话
# 同进程后面的用例全变成"只看自己"，而且报错会离真正的原因很远。


import contextlib  # noqa: E402 —— 放在这一节开头，让它与下面的可见范围用例挨着


@contextlib.contextmanager
def _own_only(conn):
    """把对话可见范围临时切成 own；退出时还原成进入前的值（不是写死 all）。"""
    from app.services import site_settings

    previous = site_settings.get_chat_visibility(conn)
    site_settings.set_chat_visibility(conn, "own", None)
    try:
        yield
    finally:
        site_settings.set_chat_visibility(conn, previous, None)


def _other_client(browser, other_login):
    """李婷的独立 TestClient（cookie 灌进新 jar，client 仍是张三本人）。"""
    who = browser()
    who.cookies.update(other_login.cookies)
    return who


def test_own_visibility_lists_only_my_sessions(client, conn, member_login, other_login, browser):
    """own 模式：列表只剩自己的会话；李婷那边同样只看得到自己的。"""
    mine = client.post("/api/chat/sessions").json()["id"]
    li = _other_client(browser, other_login)
    theirs = li.post("/api/chat/sessions").json()["id"]

    with _own_only(conn):
        ids = {s["id"] for s in client.get("/api/chat/sessions").json()}
        assert mine in ids and theirs not in ids
        ids_li = {s["id"] for s in li.get("/api/chat/sessions").json()}
        assert theirs in ids_li and mine not in ids_li

    # 还原成 all：两边又都看得到（证明 finally 真的还原了）
    ids = {s["id"] for s in client.get("/api/chat/sessions").json()}
    assert {mine, theirs} <= ids


def test_own_visibility_404s_every_session_entry_point(
    client, conn, member_login, other_login, browser
):
    """own 模式：别人的会话在**七条入口**上都是 404「没有这个会话」。

    列表挡住了、详情 404、SSE 还能看，是这类隔离最常见的漏法。所以七条一起钉：
    详情 / 流 / 发言 / 上传 / 改名 / 归档 / 取消归档。看不见与不存在给同一个 404——
    403 会泄漏"这个 id 存在，只是不给你看"。
    """
    mine = client.post("/api/chat/sessions").json()["id"]
    li = _other_client(browser, other_login)
    theirs = li.post("/api/chat/sessions").json()["id"]

    with _own_only(conn):
        responses = (
            client.get(f"/api/chat/{theirs}"),
            client.get(f"/api/chat/{theirs}/stream"),
            client.post(f"/api/chat/{theirs}/messages", data={"content": "你好"}),
            client.post(
                f"/api/chat/{theirs}/upload",
                files={"file": ("周记.txt", "一句话".encode("utf-8"), "text/plain")},
            ),
            client.patch(f"/api/chat/{theirs}", json={"title": "改个名"}),
            client.post(f"/api/chat/{theirs}/archive"),
            client.post(f"/api/chat/{theirs}/unarchive"),
        )
        for resp in responses:
            assert resp.status_code == 404, (resp.request.url, resp.status_code, resp.text)
            assert resp.json() == {"error": "没有这个会话"}

        # 自己的会话一切照常：读、写都行（写会起一轮，conftest 把它换成了空转）
        assert client.get(f"/api/chat/{mine}").status_code == 200
        sent = client.post(f"/api/chat/{mine}/messages", data={"content": "自己的话"})
        assert sent.status_code == 200, sent.text


def test_own_visibility_does_not_restrict_admin(
    client, conn, admin, member, other_login
):
    """管理员始终看全部：own 模式下列表与详情都不受限制。"""
    with _own_only(conn):
        ids = {s["id"] for s in client.get("/api/chat/sessions").json()}
        assert {member.session_id, other_login.session_id} <= ids
        for sid in (member.session_id, other_login.session_id):
            assert client.get(f"/api/chat/{sid}").status_code == 200


def test_own_visibility_filters_search_message_hits(
    client, conn, member_login, other_login, browser
):
    """own 模式：/api/search 的会话组只出现自己的消息；上传材料与 wiki 页不受影响。

    消息那一组按 owner 过滤（access.message_owner_filter），另两组是共享知识库，
    照旧全员可搜——口径与引注脱敏一致：限制的是会话，不是知识库。
    """
    li = _other_client(browser, other_login)
    mine_mid = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "验收口径在张三这场"},
    ).json()["id"]
    li.post(
        f"/api/chat/{other_login.session_id}/messages",
        data={"content": "验收口径在李婷那场"},
    )
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('upload', '4242', 0, '共享材料', '验收口径在共享材料里')"
    )

    with _own_only(conn):
        mine = client.get("/api/search", params={"q": "验收口径"}).json()
        assert [hit["ref_id"] for hit in mine["sessions"]] == [str(mine_mid)]
        assert "张三这场" in mine["sessions"][0]["snippet"]
        assert [hit["ref_id"] for hit in mine["uploads"]] == ["4242"], "材料不受可见范围影响"

        theirs = li.get("/api/search", params={"q": "验收口径"}).json()
        assert len(theirs["sessions"]) == 1
        assert "李婷那场" in theirs["sessions"][0]["snippet"]

    # all 模式：两组消息都搜得到
    everything = client.get("/api/search", params={"q": "验收口径"}).json()["sessions"]
    assert len(everything) == 2


def test_post_message_actor_carries_permission_snapshot(client, conn, member_login,
                                                        monkeypatch):
    """POST messages 交给 runner 的 actor dict 带上两个快照（2026-10-30）。

    runner 把它们塞进 AgentContext：tools 据此拒绝越权写、search 据此过滤消息命中。
    默认 all 模式 + 有建设权限 → (True, None)；切 own + 关掉建设权限后两个都变。
    """
    import app.routers.api.chat as chat_route

    captured: list[dict] = []
    monkeypatch.setattr(chat_route, "_start_turn", lambda sid, actor: captured.append(actor))

    client.post(f"/api/chat/{member_login.session_id}/messages", data={"content": "看看快照"})
    (actor,) = captured
    assert actor["name"] == "张三"
    assert actor["can_build_wiki"] is True
    assert actor["message_owner_id"] is None, "all 模式不过滤消息"

    conn.execute(
        "UPDATE users SET can_build_wiki = 0 WHERE id = ?", (member_login.user_id,)
    )
    with _own_only(conn):
        client.post(
            f"/api/chat/{member_login.session_id}/messages", data={"content": "再看一次"}
        )
    actor = captured[-1]
    assert actor["can_build_wiki"] is False
    assert actor["message_owner_id"] == member_login.user_id, "own 模式只搜自己的会话"


def test_attachments_need_build_permission(client, conn, member_login):
    """带附件发消息 / 旧上传路由：非管理员且无建设权限 → 403，且一个字都没落库。

    委托口径（2026-10-30）：只有使用权限的成员能查，但不能摄入材料。纯文字提问
    不受影响——查资料不需要建设权限。
    """
    denied = {"error": "当前账号只能使用 wiki，不能摄入材料或修改知识库"}
    sid = member_login.session_id
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,)
    ).fetchone()["n"]
    conn.execute(
        "UPDATE users SET can_build_wiki = 0 WHERE id = ?", (member_login.user_id,)
    )

    # 旧兼容路由：先暂存也不许（闸在 stage_upload 之前）
    legacy = client.post(
        f"/api/chat/{sid}/upload",
        files={"file": ("周记.txt", "一段话".encode("utf-8"), "text/plain")},
    )
    assert legacy.status_code == 403, legacy.text
    assert legacy.json() == denied

    # 带 upload_ids 的消息：403，且没有先落一条人话
    sent = client.post(
        f"/api/chat/{sid}/messages",
        data={"content": "带个附件", "upload_ids": ["1"]},
    )
    assert sent.status_code == 403, sent.text
    assert sent.json() == denied
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,)
    ).fetchone()["n"]
    assert after == before, "被拒的附件消息不许先落库"

    # 纯文字照常
    ok = client.post(f"/api/chat/{sid}/messages", data={"content": "只是想问一句"})
    assert ok.status_code == 200, ok.text
