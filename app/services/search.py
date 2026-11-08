"""共享语料的全文检索。

三样进得来：会话里的人话（全员可见，没有私聊隔离）、上传材料的解析文本、wiki 生效正文。
写入点：chat 服务落 human 消息、uploads.save_upload、wiki store 生效一页，
外加 cli 的 reindex 兜底重建。
"""

from __future__ import annotations

import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

MAX_BODY = 20000
MAX_PIECE = 400_000   # 每片 FTS 行的正文上限：整本书切着进，尾巴也要搜得到
MAX_QUERY = 200       # 用户查询上限：搜索框里的话不会有 200 字，再长也不截断去扫全表


def _match(text: str) -> str | None:
    """trigram 只认 3 个字符以上的子串；更短的查询走 LIKE 兜底。

    用户原文要原样塞进 FTS5 的**字符串字面量**（`"..."`），所以里面的 `"` 必须按
    FTS5 的规则转义（`"` → `""`，与 SQL 字符串字面量同一个手法）。不转义的话
    `q='a"b'` 会把这个字面量提前收尾，FTS5 抛 `unterminated string`，整条搜索顶成 500。
    转义只是让引号变回普通字符，不改变"逐字短语"这个语义。
    """
    cleaned = "".join(ch for ch in text if ch.strip())
    if len(cleaned) < 3:
        return None
    return '"' + cleaned.replace('"', '""') + '"'


def _like_pattern(text: str) -> str:
    """2 字以下走 LIKE 时的模式：`%`、`_`、`\\` 都得转义（SQL 里配 `ESCAPE '\\'`）。

    不转义时 `q=%` 等于 `LIKE '%%'`——把整张索引捞出来；`q=_` 会匹配任意单字。
    反斜杠先转（它自己是 ESCAPE 字符），再转两个通配符。
    """
    escaped = (
        text.strip()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _drop(conn: sqlite3.Connection, kind: str, ref_id: str) -> None:
    conn.execute("DELETE FROM fts_shared WHERE kind = ? AND ref_id = ?", (kind, ref_id))


def index_message(conn: sqlite3.Connection, message_id: int) -> bool:
    """一条消息进索引；返 True 仅当真插了一行（裁定 R81，reindex 的计数靠它）。"""
    row = conn.execute(
        "SELECT m.content, m.role, u.name AS who FROM messages m"
        " LEFT JOIN users u ON u.id = m.user_id WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    _drop(conn, "message", str(message_id))
    if row is None or row["role"] != "human" or not row["content"].strip():
        return False
    conn.execute(
        "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
        " VALUES ('message', ?, 0, ?, ?)",
        (str(message_id), row["who"] or "", row["content"][:MAX_BODY]),
    )
    return True


def index_upload(conn: sqlite3.Connection, upload_id: int) -> bool:
    """上传件按 MAX_PIECE 切片入索引；返 True 仅当真插了至少一片（裁定 R81）。

    过去整份文本截到 50 万字符写一行：592k 的 PDF 尾巴整段搜不到。现在每 40 万
    字符一片（piece 从 0 起），ref_id 一律 str(upload_id)——与 message/page 的
    ref_id 口径对齐（那两样早就是 str），_drop 靠 (kind, ref_id) 一次清掉全部旧片。
    解析文本文件不在（被删、恢复出来的库指着旧盘）返 False：那一份就是没进索引，
    reindex 的计数不许把它算成"已重建"。
    """
    from ..config import settings

    row = conn.execute("SELECT * FROM upload_files WHERE id = ?", (upload_id,)).fetchone()
    _drop(conn, "upload", str(upload_id))
    if row is None or not row["parsed_text_path"]:
        return False
    path = settings.data_dir / row["parsed_text_path"]
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    inserted = False
    for piece, start in enumerate(range(0, max(len(text), 1), MAX_PIECE)):
        chunk = text[start:start + MAX_PIECE]
        if not chunk.strip():
            continue
        conn.execute(
            "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
            " VALUES ('upload', ?, ?, ?, ?)",
            (str(upload_id), piece, row["orig_filename"], chunk),
        )
        inserted = True
    return inserted


def settings_wiki_dir():
    """wiki 目录。函数而不是模块级常量：settings 在 import 时读 env，晚绑一步好测。"""
    from ..config import settings

    return settings.wiki_dir


def index_page_path(conn: sqlite3.Connection, rel: str) -> bool:
    """按相对 path 读磁盘上的页面 md 建索引，返回有没有真写进去一行。

    S5 起 wiki 是物理 vault（页面 path = `entities/foo.md` 一类），所以检索的 ref_id
    也用 path：搜索命中直接就是前端/工具要的地址，不必再猜目录。wiki 的事实源是文件
    （迁移 0004 已删 wiki_pages 表），这里读文件而不查库。
    文件不在（页被删）就只清掉旧行；文件读不回来（形状坏、字节不是 utf-8）也只清掉
    旧行、返 False —— 检索重建不许因为一个坏文件出裸 traceback（同裁定 R21 的口径，
    所以捕 ValueError 这个父类，PageFormatError 与 UnicodeDecodeError 都在里面）。
    """
    from ..wiki import serialize

    _drop(conn, "page", rel)
    path = settings_wiki_dir() / rel
    if not path.exists():
        return False
    try:
        page = serialize.parse_md(_page_slug(rel), path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    if page.extra.get("status") == "draft":
        return False
    for piece, start in enumerate(range(0, max(len(page.body_md), 1), MAX_PIECE)):
        conn.execute(
            "INSERT INTO fts_shared (kind, ref_id, piece, heading, body)"
            " VALUES ('page', ?, ?, ?, ?)",
            (rel, piece, page.title, page.body_md[start:start + MAX_PIECE]),
        )
    return True


def _page_slug(rel: str) -> str:
    """`entities/foo.md` → `foo`（parse_md 要 slug；目录层只影响 path，不影响页内形状）。"""
    name = rel.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def reindex_all(conn: sqlite3.Connection) -> dict:
    # 懒导入：store 在模块级 import 本模块，反过来在模块级导入会成环。
    from ..wiki.store import valid_page_path

    conn.execute("DELETE FROM fts_shared")
    counts = {"message": 0, "upload": 0, "page": 0}
    # 语料没有私聊隔离（迁移 0004 删了 chat_sessions.room_type）：所有 human 消息都进。
    # 裁定 R81：三类计数都只数 index_* 真插了行的（返 True），跳过的（空白消息、
    # 解析文本文件没了、坏 md）不冒充"已重建"——恢复后对账时计数必须等于真行数。
    for row in conn.execute("SELECT id FROM messages WHERE role = 'human'"):
        if index_message(conn, row["id"]):
            counts["message"] += 1
    for row in conn.execute("SELECT id FROM upload_files WHERE parsed_text_path IS NOT NULL"):
        if index_upload(conn, row["id"]):
            counts["upload"] += 1
    wiki_dir = settings_wiki_dir()
    # 递归扫新目录（计划 §8）：只有 {dir}/{slug}.md 形状的页进索引；SCHEMA/index/log
    # 在根上、raw 在 raw/ 下、形状不对的手工文件，valid_page_path 一律 False。
    if wiki_dir.exists():
        for path in sorted(wiki_dir.rglob("*.md")):
            rel = path.relative_to(wiki_dir).as_posix()
            if not valid_page_path(rel):
                # 裁定 R48：与 store.list_pages/get_page 用同一个 path 形状判据——
                # 进不了目录也点不开的文件绝不能进 FTS，否则搜索结果里躺着必 404 的命中。
                continue
            # 只数真进了索引的页：坏文件被跳过时不该在 cli 的输出里冒充"已重建"。
            if index_page_path(conn, rel):
                counts["page"] += 1
    return counts


LIKE_SNIPPET = 200    # LIKE 兜底分支的正文截断长度（裁定 R64）


def _message_owner_sql(kinds: tuple[str, ...], message_owner_id: int | None) -> tuple[str, tuple]:
    """消息命中的可见范围过滤（2026-10-30）：返回 (附加 SQL 片段, 绑定参数)。

    `own` 模式下成员只该搜到自己会话里的消息；`all` 模式与管理员传 None，什么都不加。
    只过滤 kind='message' 的行：上传材料与 wiki 页是共享知识库，不受对话可见范围影响。

    过滤条件写成"ref_id 落在属于这个人的消息里"：fts_shared.ref_id 是**消息 id 的
    文本**（索引口径见 index_message），子查询里 CAST 成 TEXT 再比，避免 SQLite 的
    亲和性把它当数字比（数字与文本混比时 `'1' IN (1)` 不成立）。
    两个分支共用这一段，别一边写一遍——少写一边就是"LIKE 能搜到别人的话"。
    """
    if message_owner_id is None or "message" not in kinds:
        return "", ()
    return (
        " AND (kind != 'message' OR ref_id IN ("
        "SELECT CAST(m.id AS TEXT) FROM messages m"
        " JOIN chat_sessions s ON s.id = m.session_id WHERE s.user_id = ?))",
        (int(message_owner_id),),
    )


def search(
    conn: sqlite3.Connection,
    text: str,
    *,
    kinds: tuple[str, ...] | None = None,
    limit: int = 6,
    message_owner_id: int | None = None,
) -> list[dict]:
    """在全站语料里搜；message_owner_id 给了就只留这个 user 会话里的消息命中。

    message_owner_id（2026-10-30，对话可见范围）：None = 不过滤（`all` 模式与管理员）。
    只影响 kind='message' 的那一组，见 _message_owner_sql。
    """
    kinds = kinds or ("message", "upload", "page")
    question = (text or "").strip()
    if not question:
        # 裁定 R82：空/纯空白 q 是"什么都没搜"，不是"搜一切"。过去的 LIKE '%%'
        # 会把任意 30 行端出来，前端一打开搜索页就躺着一堆与查询无关的命中。
        return []
    if len(question) > MAX_QUERY:
        # 超长 q 不截断、不扫全表：它不会是用户在搜索框里说的话，直接当"没搜到"。
        return []
    placeholders = ",".join("?" for _ in kinds)
    match = _match(text)
    owner_sql, owner_params = _message_owner_sql(kinds, message_owner_id)
    # 裁定 R64：先超取 limit*4，去重之后再裁到 limit。一份大材料切成几十片时，
    # 只取 limit 行会让它的切片占满窗口，去重后剩一条命中，别的语料整组饿死。
    fetch = limit * 4
    try:
        if match is None:
            like = _like_pattern(text)
            # 裁定 R64：LIKE 分支没有 snippet() 可用，正文在 SQL 里就截到 200 字符——
            # 一个 2 字查询过去能把每片 40 万字符原样端出去。
            # ESCAPE：让 `\` 被转义过的 %/_/\ 认回字面量（否则 q=% 就是搜全部）。
            rows = conn.execute(
                f"SELECT kind, ref_id, heading, substr(body, 1, {LIKE_SNIPPET}) AS body"
                f" FROM fts_shared"
                f" WHERE kind IN ({placeholders}){owner_sql}"
                f" AND (heading LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
                f" ORDER BY ref_id DESC LIMIT ?",
                (*kinds, *owner_params, like, like, fetch),
            ).fetchall()
        else:
            # FTS snippet() enumerates occurrence windows and can take minutes on repetitive documents.
            # Keep ranking, but locate the first literal match and return a bounded excerpt.
            rows = conn.execute(
                f"SELECT kind, ref_id, heading,"
                f" substr(body, max(1,instr(lower(body),lower(?))-50), 180) AS body"
                f" FROM fts_shared WHERE fts_shared MATCH ? AND kind IN ({placeholders})"
                f"{owner_sql}"
                f" ORDER BY rank LIMIT ?",
                ("".join(question.split()), match, *kinds, *owner_params, fetch),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        # 兜底：FTS/LIKE 的语法或索引故障不该把整条搜索顶成 500（搜索页一打开就红）。
        # 记一条 warning（带查询原文，便于修查询构造），对调用方按"没搜到"处理。
        logger.warning("搜索查询没跑通（q=%r）：%r", question[:50], exc)
        return []

    # 一份材料切成多片后，同一条查询可能命中它的几片；结果按 rank 排好序，
    # 同 (kind, ref_id) 只留最先出现（= rank 最好）的一片，去重成一条命中。
    # 裁定 R62：page_types 过滤分支删掉——wiki_pages 表已不存在（迁移 0004），
    # 页面类型要过滤由调用方对 slug 读 front matter，S1 不需要。
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["kind"], str(row["ref_id"]))
        if key not in best:
            entry = dict(row)
            if match is not None:
                needle = "".join(question.split())
                entry["body"] = re.sub(re.escape(needle), lambda m: "【" + m.group(0) + "】", entry["body"], count=1, flags=re.IGNORECASE)
            best[key] = entry
    return list(best.values())[:limit]


def upload_text(conn: sqlite3.Connection, upload_id: int) -> str | None:
    from ..config import settings

    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None or not row["parsed_text_path"]:
        return None
    path = settings.data_dir / row["parsed_text_path"]
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None


def upload_window(conn: sqlite3.Connection, upload_id: int, query: str,
                  before: int = 300, after: int = 1200) -> str:
    """给模型一段干净原文窗口（不带任何标记），让它能逐字抄引注。"""
    text = upload_text(conn, upload_id) or ""
    pos = text.find(query.strip())
    if pos < 0:
        return text[: before + after]
    start = max(0, pos - before)
    return text[start:pos + after]
