"""上传：pptx 真摄入 + parse_report + 材料列表。

前几条是简报逐字给的；后几条钉住本次要杀的缺陷本身——解析炸了不许连累上传、
解析出来的文本要真进共享检索。文件末尾几条是 fix round 2（裁定 R58-R61）：
超限要撤干净、解析失败要留痕、进消息正文的文件名要净化截断。

2026-10-30 起**不再提供原件下载**：旧入口 /api/uploads/{id}/download 一律 404，
这里留一条回归用例钉住它；其余断言改成直接看盘上的原件还在不在。
"""

from __future__ import annotations

import io
import json
import zipfile
import logging
import re
from pathlib import Path
from types import SimpleNamespace

from pptx import Presentation
from pptx.util import Inches

from app.config import settings
from app.services import uploads as uploads_svc
from tests.conftest import fetch_one

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _files_on_disk() -> set[Path]:
    """uploads_dir 下现有的文件集合（DATA_DIR 是整轮测试共用的，别的测试会留件）。"""
    root = settings.uploads_dir
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _upload(client, member_login, filename: str, payload: bytes, mime: str, note: str = ""):
    """两段式辅助：先 POST /api/uploads 暂存，再带 upload_ids 发消息（解析在发送时）。

    返回体保持旧测试熟悉的形状（upload_id/parse_status/raw_path/parse_report）；
    暂存本身就失败（413 等）时原样返回响应对象。note 参数仅为兼容旧签名，不再使用。
    """
    staged = client.post("/api/uploads", files={"file": (filename, payload, mime)})
    if staged.status_code != 200:
        return staged
    upload_id = staged.json()["upload_id"]
    sent = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "", "upload_ids": [str(upload_id)]},
    )
    assert sent.status_code == 200, sent.text
    merged = dict(sent.json()["attachments"][0])
    row = fetch_one(
        "SELECT parse_report FROM upload_files WHERE id = ?", (upload_id,)
    )
    merged["parse_report"] = json.loads(row["parse_report"]) if row["parse_report"] else None
    return SimpleNamespace(status_code=200, json=lambda: merged, text="")


def _pptx_bytes() -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "上线首月"
    body = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(2))
    body.text_frame.text = "漏单降到 1 单"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    # 偏离简报 verbatim 一处，探针证实必要：python-pptx 1.0.2 的 add_picture 只认
    # 路径或文件对象（pptx/parts/image.py 直接 getattr(image_file, "seek")），
    # 喂裸 bytes 会 AttributeError: 'bytes' object has no attribute 'seek'。
    slide2.shapes.add_picture(io.BytesIO(_png_bytes()), Inches(1), Inches(1))
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    import struct
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00\xff\xff\xff")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def test_pptx_parsed_with_report(client, conn, member_login):
    resp = _upload(client, member_login, "周记.pptx", _pptx_bytes(), PPTX_MIME)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "done"
    assert body["parse_report"]["pages_total"] == 2
    assert body["parse_report"]["pages_with_text"] == 1
    assert body["parse_report"]["pages_image_only"] == 1
    row = conn.execute("SELECT parsed_text_path FROM upload_files WHERE id=?", (body["upload_id"],)).fetchone()
    from app.config import settings
    assert "漏单降到 1 单" in (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")


def test_original_download_route_is_gone(client, conn, member_login):
    """2026-10-30 裁定：不提供原件下载——旧入口对谁都 404，登录也不放行。"""
    resp = client.post(
        f"/api/chat/{member_login.session_id}/upload",
        files={"file": ("a.txt", b"hello", "text/plain")}, data={"note": ""},
    )
    up_id = resp.json()["upload_id"]
    assert client.get(f"/api/uploads/{up_id}/download").status_code == 404
    assert client.get("/api/uploads/1/download").status_code == 404


def test_broken_pptx_still_uploads_but_is_not_downloadable(client, conn, member_login):
    """解析失败不许连累上传：原件还在盘上，parse_status 说清是 failed；但不再有下载入口。"""
    payload = b"PK\x03\x04\xe8\xbf\x99\xe4\xb8\x8d\xe6\x98\xaf\xe7\x9c\x9f pptx"
    resp = _upload(client, member_login, "坏件.pptx", payload, PPTX_MIME)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "failed"
    assert body["parse_report"] is None
    row = conn.execute(
        "SELECT parsed_text_path, parse_report FROM upload_files WHERE id = ?",
        (body["upload_id"],),
    ).fetchone()
    assert row["parsed_text_path"] is None
    assert row["parse_report"] is None
    stored = settings.data_dir / conn.execute(
        "SELECT stored_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()["stored_path"]
    assert stored.read_bytes() == payload


def test_parsed_upload_enters_shared_search(client, conn, member_login):
    """缺陷的正脸：材料必须真进知识库（fts_shared 里有一行能搜到），不是存了就完事。"""
    resp = _upload(client, member_login, "周记.pptx", _pptx_bytes(), PPTX_MIME)
    assert resp.status_code == 200, resp.text
    row = conn.execute(
        "SELECT heading, body FROM fts_shared WHERE kind = 'upload' AND heading = '周记.pptx'"
    ).fetchone()
    assert row is not None
    assert "漏单降到 1 单" in row["body"]


# ---------------------------------------------------------------- fix round 2
# 裁定 R58-R61。这四条住在 test_uploads.py（不在 Task 8 要接管的会话测试里），
# T8 重写 app/routers/api/chat.py 的 post_upload 时它们必须继续绿。


def test_over_limit_leaves_no_message_no_fts_no_file(client, conn, member_login, monkeypatch):
    """裁定 R58：超限 413 之后，播报消息、它的 FTS 行、半截存盘件都不许留下。

    钉住 post_upload 里 `except uploads.FileTooLargeError` 那一支（DELETE messages +
    search.index_message 抹掉孤儿命中 + save_upload 当场 unlink 半截文件）。
    少写任何一步，检索里就会躺着一个指向已删消息的命中，或盘上留一坨没人认领的字节。
    """
    monkeypatch.setattr(uploads_svc, "MAX_SIZE", 10)  # 上限调到 10 字节，11 字节当场超
    sid = member_login.session_id
    messages_before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    fts_before = conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind = 'message'"
    ).fetchone()["n"]
    files_before = _files_on_disk()

    resp = _upload(client, member_login, "big.txt", b"x" * 11, "text/plain", note="超限件")

    assert resp.status_code == 413, resp.text
    body = resp.json()
    # /api 只有一种错误信封；不写死文案，T8 改措辞不该弄红这条
    assert list(body) == ["error"]
    assert isinstance(body["error"], str) and body["error"]

    # 消息行撤干净：条数没变，也没有任何一条正文提到这个文件名
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == messages_before
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND content LIKE '%big.txt%'",
        (sid,),
    ).fetchone()["n"] == 0
    # FTS 里没多出 message 行，也没有指向已删消息的孤儿命中
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind = 'message'"
    ).fetchone()["n"] == fts_before
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind = 'message'"
        " AND ref_id NOT IN (SELECT CAST(id AS TEXT) FROM messages)"
    ).fetchone()["n"] == 0
    # 盘上没留半截文件（DATA_DIR 整轮共用，所以比前后集合，不数绝对个数）
    assert _files_on_disk() == files_before


def test_extract_failure_is_logged(caplog, tmp_path):
    """裁定 R59：解析失败要留一条 warning，带上文件名、后缀与异常本身。

    没有这一行，"部署里缺 python-pptx"（import 就炸）与"用户传了个假 .pptx"（zip 打不开）
    在库里长得一模一样（parse_status='failed'、parse_report 为 NULL），运维无从分辨。
    行为不变：仍然返 (None, None)——上传本身不许被解析连累（见上一条 broken pptx 测试）。
    """
    bad = tmp_path / "坏件.pptx"
    bad.write_bytes(b"PK\x03\x04\xe8\xbf\x99\xe4\xb8\x8d\xe6\x98\xaf\xe7\x9c\x9f pptx")

    with caplog.at_level(logging.WARNING, logger="app.services.uploads"):
        assert uploads_svc.extract(bad) == (None, None)

    ours = [r for r in caplog.records if r.name == "app.services.uploads"]
    assert len(ours) == 1, [r.getMessage() for r in caplog.records]
    record = ours[0]
    assert record.levelno == logging.WARNING
    # 三个入参逐条对得上裁定：path.name / suffix / 异常（%r 渲染）
    assert record.args[0] == "坏件.pptx"
    assert record.args[1] == ".pptx"
    assert isinstance(record.args[2], Exception)
    message = record.getMessage()
    assert "坏件.pptx" in message and ".pptx" in message
    assert repr(record.args[2]) in message


def test_message_embeds_clean_capped_filename(client, conn, member_login):
    """裁定 R61：进 messages.content（连带进 fts_shared 正文）的文件名先清控制字符再截 120 字。

    文件名完全由用户给：300 字的、带回车的都能传上来。不截不清，它就原样躺进语料库，
    把真正的话挤掉，还能把前端一行播报撕成多行。
    """
    raw = "报告\x7f" + "甲" * 300 + ".txt"
    assert len(raw) > 300
    resp = _upload(client, member_login, raw, b"hello", "text/plain", note="超长名")
    assert resp.status_code == 200, resp.text

    mid = conn.execute(
        "SELECT message_id FROM upload_files WHERE id = ?", (resp.json()["upload_id"],)
    ).fetchone()["message_id"]
    content = conn.execute("SELECT content FROM messages WHERE id = ?", (mid,)).fetchone()["content"]
    embedded = re.search(r"《(.*)》", content, re.DOTALL).group(1)

    assert len(embedded) <= 120
    assert len(embedded) == 120  # 截断真的发生了，不是碰巧短
    assert not re.search(r"[\x00-\x1f\x7f]", embedded)
    assert not re.search(r"[\x00-\x1f\x7f]", content)
    assert embedded.startswith("报告")  # 控制字符被摘掉，可读的部分留着
    assert "甲" * 121 not in content
    # 索引正文就是消息正文，跟着干净
    fts = conn.execute(
        "SELECT body FROM fts_shared WHERE kind = 'message' AND ref_id = ?", (str(mid),)
    ).fetchone()
    assert fts is not None and fts["body"] == content


def test_display_name_strips_every_control_char():
    """上一条走 HTTP，只能带 \x7f 进来：httpx 自己会把 \x00-\x1f 转义成 "%00" 一类
    （_multipart.py 的 _HTML5_FORM_ENCODING_REPLACEMENTS），真控制字符到不了应用。
    所以整段控制字符范围在这里直接对着净化函数验。"""
    from app.routers.api import chat as chat_router

    assert chat_router._display_name("a\x00b\rc\nd\te\x1ff\x7fg") == "abcdefg"
    assert chat_router._display_name("x" * 300) == "x" * 120
    assert chat_router._display_name("\x00\t\r\n") == "未命名文件"
    assert chat_router._display_name(None) == "未命名文件"
    assert chat_router._display_name("  周记.pptx  ") == "周记.pptx"


# ---------------------------------------------------------------- final fix wave


def test_ppt_without_soffice_is_parse_status_none(client, conn, member_login, monkeypatch):
    """S5b：老 Office 格式要 libreoffice 才能转。**系统没有 soffice 时**落 'none'
    （"这类文件不解析"的现成文案），不是 failed——环境缺组件不是文件坏了；
    原件照旧在盘上（只是不提供下载）。真跑 soffice 的分支见下一条（用假 converter，不烧 CPU）。"""
    monkeypatch.setattr(uploads_svc.shutil, "which", lambda name: None)
    payload = b"\xd0\xcf\x11\xe0" + "假装是 OLE2 的老幻灯片".encode("utf-8")
    resp = _upload(
        client, member_login, "旧课件.ppt", payload, "application/vnd.ms-powerpoint"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "none"
    assert body["parse_report"]["unsupported"] is True
    assert "libreoffice" in body["parse_report"]["note"]
    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    assert row["parsed_text_path"] is None
    stored = settings.data_dir / conn.execute(
        "SELECT stored_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()["stored_path"]
    assert stored.read_bytes() == payload
    # 老格式都进了"能尝试转换"的名单；.pptx 仍是直接解析的格式。
    assert {".doc", ".ppt", ".xls"} <= set(uploads_svc.CONVERTIBLE)
    assert ".pptx" in uploads_svc.PARSEABLE


# ------------------------------------------------- S5 计划 §3：raw 摄入（after commit）


def _raw_files() -> list[Path]:
    base = settings.wiki_dir / "raw" / "uploads"
    return sorted(base.glob("*.md")) if base.is_dir() else []


def test_parsed_upload_is_ingested_into_raw(client, conn, member_login, tmp_wiki):
    """解析成功 → 响应带 raw_path、raw md 落盘、log 追加、git 里有一条 kind=raw 的提交。"""
    from app.wiki import store

    resp = _upload(client, member_login, "周记.pptx", _pptx_bytes(), PPTX_MIME)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    raw_path = body["raw_path"]
    assert raw_path == f"raw/papers/{body['upload_id']}-pptx.md"

    target = settings.wiki_dir / raw_path
    assert target.is_file(), f"raw 没落盘：{raw_path}"
    raw_text = target.read_text(encoding="utf-8")
    assert "source_file:" not in raw_text
    assert "sha256: " in raw_text
    assert "sha256:" in raw_text and "ingested:" in raw_text
    assert "漏单降到 1 单" in raw_text

    # log.md 追加了一条 ingest（与 raw 同一次 commit）
    log = (settings.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "ingest" in log and raw_path in log
    # 同一次 commit：git 主题 kind=raw / action=ingest（repo.log 按 path 读得回来）
    from app.wiki import repo

    entries = repo.log(raw_path, limit=1)
    assert entries and entries[0].kind == "raw" and entries[0].action == "ingest"

    # raw 的 sha256 与上传记录一致：lint 不许报 raw_drift
    lint = client.get("/api/wiki/lint").json()
    assert not [f for f in lint["findings"] if f["code"] == "raw_drift"], lint


def test_upload_without_parsed_text_has_no_raw_path(client, conn, member_login, tmp_wiki,
                                                     monkeypatch):
    """解析失败/这种不解析（parse_status != done）不生成 raw；响应里 raw_path 是 null。"""
    monkeypatch.setattr(uploads_svc.shutil, "which", lambda name: None)  # .ppt 按"没装 soffice"
    broken = _upload(client, member_login, "坏件.pptx", b"PK\x03\x04not-a-pptx", PPTX_MIME)
    assert broken.status_code == 200, broken.text
    assert broken.json()["parse_status"] == "failed"
    assert broken.json()["raw_path"] is None

    old = _upload(
        client, member_login, "旧课件.ppt", b"\xd0\xcf\x11\xe0xx",
        "application/vnd.ms-powerpoint",
    )
    assert old.status_code == 200, old.text
    assert old.json()["parse_status"] == "none"
    assert old.json()["raw_path"] is None
    assert _raw_files() == []


def test_raw_ingest_failure_does_not_fail_the_upload(client, conn, member_login, tmp_wiki,
                                                     monkeypatch):
    """raw 是旁路：摄入炸了/失败了，上传照样 200、解析文本与原件都还在，raw_path=null。"""
    from app.wiki import store

    def boom(*args, **kwargs):
        raise store.StoreError("版本库没跑通（假装 git 挂了）")

    monkeypatch.setattr(store, "ingest_raw_upload", boom)
    resp = _upload(client, member_login, "周记.pptx", _pptx_bytes(), PPTX_MIME)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "done"
    assert body["raw_path"] is None
    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    assert row is not None and row["parsed_text_path"]
    assert _raw_files() == []

    # 返回 failed 而不抛异常的那条路同样只是 raw_path=None
    monkeypatch.setattr(store, "ingest_raw_upload", lambda *a, **k: {
        "ok": False, "path": None, "sha": None, "noop": False,
        "failed": [{"key": "", "quote": "", "note": "写不进去"}],
    })
    resp = _upload(client, member_login, "周记2.pptx", _pptx_bytes(), PPTX_MIME)
    assert resp.status_code == 200, resp.text
    assert resp.json()["raw_path"] is None
    assert _raw_files() == []


# ------------------------------------------- S5b：多来源解析（xlsx / 图片 / 老 Office）


def _xlsx_bytes() -> bytes:
    """两个正经 sheet + 一个空 sheet + 一个 205 行的长 sheet（验行上限）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    first = workbook.active
    first.title = "报名表"
    first.append(["姓名", "状态", "备注"])
    first.append(["张三", "已报名", "含|竖线"])
    workbook.create_sheet("空表")                       # 空 sheet 应当跳过
    stats = workbook.create_sheet("统计")
    stats.append(["指标", "数值"])
    stats.append(["漏单", 1])
    long_sheet = workbook.create_sheet("长表")
    for i in range(1, 206):                              # 201–205 行应被上限截掉
        long_sheet.append([f"第{i}行", i])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_xlsx_parsed_into_markdown_tables(client, conn, member_login, tmp_wiki):
    """xlsx → 每个非空 sheet 一张 markdown 表；空 sheet 跳过；200 行 / 20 列上限生效。"""
    resp = _upload(
        client, member_login, "报名统计.xlsx", _xlsx_bytes(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "done"
    assert body["parse_report"] == {
        "pages_total": 1, "pages_with_text": 1, "pages_image_only": 0,
    }
    raw_path = body["raw_path"]
    assert raw_path == f"raw/articles/{body['upload_id']}-xlsx.md"
    assert (tmp_wiki / raw_path).is_file()

    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "## Sheet: 报名表" in text and "## Sheet: 统计" in text
    assert "## Sheet: 空表" not in text                # 空 sheet 跳过
    assert "| 姓名 | 状态 | 备注 |" in text
    assert "| --- | --- | --- |" in text
    assert "| 张三 | 已报名 | 含\\|竖线 |" in text     # 单元格里的 | 转义
    assert "| 漏单 | 1 |" in text
    # 每 sheet 200 行上限
    assert "第200行" in text and "第201行" not in text

    # 解析文本进了共享检索（一份材料 = upload 组一条命中）
    hits = conn.execute(
        "SELECT kind, ref_id FROM fts_shared WHERE kind='upload' AND ref_id=?",
        (str(body["upload_id"]),),
    ).fetchall()
    assert hits


def _image_bytes() -> bytes:
    """最小的合法 PNG（1×1 白点）——视觉调用被 monkeypatch，不会真解码。"""
    return _png_bytes()


def test_image_upload_goes_through_vision(client, conn, member_login, tmp_wiki, monkeypatch):
    """图片走 DeepSeek 视觉（这里 monkeypatch，不花真额度）：文本入库/进检索/进 raw。"""
    from app.services import vision

    seen_paths = []

    def fake_describe(path):
        seen_paths.append(path)
        return "图中文字：漏单降到 1 单，流程图有三个节点"

    monkeypatch.setattr(vision, "describe_image", fake_describe)
    resp = _upload(client, member_login, "截图.png", _image_bytes(), "image/png")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "done"
    assert body["parse_report"] == {
        "pages_total": 1, "pages_with_text": 1, "pages_image_only": 0,
    }
    raw_path = body["raw_path"]
    assert raw_path.startswith(f"raw/articles/{body['upload_id']}-")
    assert "漏单降到 1 单" in (tmp_wiki / raw_path).read_text(encoding="utf-8")
    assert len(seen_paths) == 1 and seen_paths[0].suffix == ".png"

    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "图中文字：漏单降到 1 单" in text
    hit = conn.execute(
        "SELECT ref_id FROM fts_shared WHERE kind='upload' AND ref_id=?",
        (str(body["upload_id"]),),
    ).fetchone()
    assert hit is not None


def test_image_vision_failure_is_parse_failed(client, conn, member_login, monkeypatch):
    """视觉没回文字（没配 key/上游失败）：parse_status=failed + 一句 note，原件仍在盘上。"""
    from app.services import vision

    monkeypatch.setattr(vision, "describe_image", lambda path: None)
    resp = _upload(client, member_login, "截图.png", _image_bytes(), "image/png")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "failed"
    assert body["raw_path"] is None
    assert "视觉模型没返回文字" in body["parse_report"]["note"]
    stored = settings.data_dir / conn.execute(
        "SELECT stored_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()["stored_path"]
    assert stored.read_bytes() == _image_bytes()


def test_image_over_limit_fails_with_note(client, conn, member_login, monkeypatch):
    """图片 >10 MB 直接失败并记 note（把上限 monkeypatch 到 8 字节，不造 10MB 文件）。"""
    from app.services import vision

    monkeypatch.setattr(vision, "VISION_MAX_BYTES", 8)
    resp = _upload(client, member_login, "大图.png", _image_bytes(), "image/png")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "failed"
    assert body["raw_path"] is None
    assert "没送视觉模型" in body["parse_report"]["note"]
    assert body["parse_report"]["pages_with_text"] == 0


def _fake_convert(created: list, *, returncode: int = 0):
    """假 soffice：在 --outdir 下造一个转换结果 docx，记录 argv。"""
    from types import SimpleNamespace

    import docx as docx_module

    def fake_run(argv):
        created.append(list(argv))
        outdir = Path(argv[argv.index("--outdir") + 1])
        target_ext = "." + argv[argv.index("--convert-to") + 1]
        source = Path(argv[-1])
        if returncode != 0:
            return SimpleNamespace(returncode=returncode, stderr="boom", stdout="")
        document = docx_module.Document()
        document.add_paragraph("从老文档转换来的正文。")
        document.save(outdir / f"{source.stem}{target_ext}")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    return fake_run


def test_legacy_office_converts_when_soffice_exists(client, conn, member_login, tmp_wiki,
                                                    monkeypatch):
    """有 soffice 时老格式先转成新格式再解析（假 converter，不真跑 libreoffice）。"""
    created: list = []
    monkeypatch.setattr(uploads_svc.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(uploads_svc, "_run_soffice", _fake_convert(created))

    resp = _upload(
        client, member_login, "旧文档.doc", b"\xd0\xcf\x11\xe0old-word",
        "application/msword",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "done"
    assert body["raw_path"]

    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "从老文档转换来的正文。" in text
    assert len(created) == 1
    argv = created[0]
    assert "--headless" in argv and argv[0] == "/usr/bin/soffice"
    assert argv[argv.index("--convert-to") + 1] == "docx"


def test_legacy_office_conversion_failure_is_parse_failed(client, conn, member_login,
                                                          monkeypatch):
    """有 soffice 但转换失败（真坏文件/转换器炸了）→ failed，不是 none。"""
    monkeypatch.setattr(uploads_svc.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(uploads_svc, "_run_soffice", _fake_convert([], returncode=1))
    resp = _upload(client, member_login, "坏文档.doc", b"not-a-real-doc", "application/msword")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "failed"
    assert body["raw_path"] is None


# ------------------------------------------------ S11：暂存不解析（两段式的一半）


def test_staging_does_not_parse_or_create_message(client, conn, member_login, tmp_wiki,
                                                  monkeypatch):
    """POST /api/uploads 只暂存：不调用 extract、不建消息、不进 FTS、不写 raw/解析文本。"""
    called: list = []

    def spy_extract(path):
        called.append(path)
        raise AssertionError("选文件时不该解析")

    monkeypatch.setattr(uploads_svc, "extract", spy_extract)
    rows_before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    extracted_before = (
        set(settings.extracted_text_dir.glob("*"))
        if settings.extracted_text_dir.exists() else set()
    )

    resp = client.post(
        "/api/uploads",
        files={"file": ("大材料.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"upload_id", "orig_filename", "size", "mime", "parse_status"}
    assert body["parse_status"] == "none"
    assert called == [], "暂存阶段不许调 extract"

    row = conn.execute("SELECT * FROM upload_files WHERE id = ?", (body["upload_id"],)).fetchone()
    assert row["message_id"] is None
    assert row["parse_status"] == "none" and row["parsed_text_path"] is None
    assert row["parse_report"] is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages"
    ).fetchone()["n"] == rows_before, "暂存不许建消息"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind = 'upload' AND ref_id = ?",
        (str(body["upload_id"]),),
    ).fetchone()["n"] == 0
    assert _raw_files() == []
    extracted_after = (
        set(settings.extracted_text_dir.glob("*"))
        if settings.extracted_text_dir.exists() else set()
    )
    assert extracted_after == extracted_before, "暂存不许写解析文本"


def test_delete_staged_upload_removes_row_and_file(client, conn, member_login):
    """DELETE /api/uploads/{id}：撤自己的未发送暂存件（删行+删盘）；别人的/已发送的不行。"""
    resp = client.post("/api/uploads", files={"file": ("草稿.txt", b"hello", "text/plain")})
    upload_id = resp.json()["upload_id"]
    disk = settings.data_dir / conn.execute(
        "SELECT stored_path FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()["stored_path"]
    assert disk.exists()

    deleted = client.delete(f"/api/uploads/{upload_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "upload_id": upload_id}
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()["n"] == 0
    assert not disk.exists()
    # 再删一次 = 没有这份材料（404）
    assert client.delete(f"/api/uploads/{upload_id}").status_code == 404


def test_delete_sent_upload_is_400(client, conn, member_login):
    """已经随消息发出去的附件不能撤（内容已进聊天记录与智能体上下文）。"""
    staged = client.post(
        "/api/uploads", files={"file": ("已发.txt", b"sent", "text/plain")}
    ).json()
    client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "", "upload_ids": [str(staged["upload_id"])]},
    )
    resp = client.delete(f"/api/uploads/{staged['upload_id']}")
    assert resp.status_code == 400
    assert resp.json() == {"error": "附件已经发送，不能撤"}


def test_delete_other_users_staged_upload_is_404(client, conn, member_login):
    """别人的暂存件不能撤（按"没有这份材料"处理，不泄露它存在）。"""
    from app.services import auth as auth_service

    other_id = auth_service.create_member_direct(conn, "李婷", "13700000002", "member-local-2")
    cur = conn.execute(
        "INSERT INTO upload_files (message_id, user_id, orig_filename, stored_path, size,"
        " mime, sha256, parse_status, created_at)"
        " VALUES (NULL, ?, '别人的.txt', 'uploads/other/x.txt', 1, 'text/plain', 'x',"
        " 'none', '2026-10-21T00:00:00')",
        (other_id,),
    )
    foreign_id = int(cur.lastrowid)
    assert client.delete(f"/api/uploads/{foreign_id}").status_code == 404
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM upload_files WHERE id = ?", (foreign_id,)
    ).fetchone()["n"] == 1


def test_pdf_over_page_limit_is_none_not_failed(client, conn, member_login, monkeypatch):
    """PDF > 2000 页不解析：parse_status='none'（大书不是坏文件），note 说清原因。"""
    import pypdf

    from app.services import uploads as uploads_svc

    assert uploads_svc.PDF_MAX_PAGES == 2000

    class FakeReader:
        def __init__(self, _path):
            self.pages = [None] * (uploads_svc.PDF_MAX_PAGES + 1)

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    resp = _upload(
        client, member_login, "整本书.pdf", b"%PDF-1.4 fake", "application/pdf"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "none"
    assert body["raw_path"] is None
    assert body["parse_report"]["unsupported"] is True
    assert "页上限" in body["parse_report"]["note"]


def test_pptx_over_slide_limit_is_none_not_failed(client, conn, member_login, monkeypatch):
    """PPT > 1000 页幻灯片不解析：同样是 'none' + note，不是 failed。"""
    import pptx

    from app.services import uploads as uploads_svc

    assert uploads_svc.PPTX_MAX_SLIDES == 1000

    class FakePresentation:
        def __init__(self, _path):
            self.slides = [object()] * (uploads_svc.PPTX_MAX_SLIDES + 1)

    monkeypatch.setattr(pptx, "Presentation", FakePresentation)
    resp = _upload(
        client, member_login, "超长课件.pptx", b"PK\x03\x04fake",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "none"
    assert body["raw_path"] is None
    assert body["parse_report"]["unsupported"] is True
    assert "页上限" in body["parse_report"]["note"]


# ------------------------------- S11 两段式：发送时带附件 → 解析 + 入库 + 起一轮


def _stage(client, filename, payload, mime):
    resp = client.post("/api/uploads", files={"file": (filename, payload, mime)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_send_with_attachment_finalizes_and_starts_a_turn(client, conn, member_login,
                                                          tmp_wiki, monkeypatch):
    """发送时带 upload_ids：挂 message_id → 解析（done）→ FTS/raw → 起一轮。"""
    from app.routers.api import chat as chat_router

    calls: list = []
    monkeypatch.setattr(
        chat_router, "_start_turn", lambda sid, actor: calls.append((sid, actor))
    )
    staged = _stage(client, "材料.txt", "发送时才解析的正文，够长了。".encode(), "text/plain")

    resp = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "把这份材料整理进 wiki", "upload_ids": [str(staged["upload_id"])]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] and len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["upload_id"] == staged["upload_id"]
    assert att["parse_status"] == "done" and att["chars"] > 0
    assert att["raw_path"].startswith(f"raw/articles/{staged['upload_id']}-")

    row = conn.execute(
        "SELECT message_id, parse_status, parsed_text_path FROM upload_files WHERE id = ?",
        (staged["upload_id"],),
    ).fetchone()
    assert row["message_id"] == body["id"] and row["parse_status"] == "done"
    assert row["parsed_text_path"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind='upload' AND ref_id = ?",
        (str(staged["upload_id"]),),
    ).fetchone()["n"] >= 1
    content = conn.execute(
        "SELECT content FROM messages WHERE id = ?", (body["id"],)
    ).fetchone()["content"]
    assert content == "把这份材料整理进 wiki"
    assert calls == [(
        member_login.session_id,
        {
            "id": member_login.user_id, "name": "张三",
            "can_build_wiki": True, "message_owner_id": None,
        },
    )]


def test_send_with_empty_content_and_attachments_autofills_text(client, conn, member_login,
                                                                 tmp_wiki):
    """空文字 + 只有附件也允许：正文自动写成"上传了《a》、《b》"。"""
    one = _stage(client, "书.pdf", b"%PDF-1.4 fake", "application/pdf")
    two = _stage(client, "图.png", _png_bytes(), "image/png")
    resp = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "", "upload_ids": [str(one["upload_id"]), str(two["upload_id"])]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    content = conn.execute(
        "SELECT content FROM messages WHERE id = ?", (body["id"],)
    ).fetchone()["content"]
    assert content == "上传了《书.pdf》、《图.png》"
    assert [a["upload_id"] for a in body["attachments"]] == [
        one["upload_id"], two["upload_id"],
    ]


def test_send_rejects_invalid_or_already_sent_attachments(client, conn, member_login, tmp_wiki):
    """附件校验：无效 id / 别人的 / 已发送过的，都 400，消息不落库。"""
    from app.services import auth as auth_service

    before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]

    # 无效 id
    resp = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "x", "upload_ids": ["999999"]},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "有些附件已失效，重新上传"}

    # 别人的暂存件
    other_id = auth_service.create_member_direct(conn, "李婷", "13700000002", "member-local-2")
    cur = conn.execute(
        "INSERT INTO upload_files (message_id, user_id, orig_filename, stored_path, size,"
        " mime, sha256, parse_status, created_at)"
        " VALUES (NULL, ?, '别人的.txt', 'uploads/other/y.txt', 1, 'text/plain', 'y',"
        " 'none', '2026-10-21T00:00:00')",
        (other_id,),
    )
    foreign_id = int(cur.lastrowid)
    resp = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "x", "upload_ids": [str(foreign_id)]},
    )
    assert resp.status_code == 400

    # 已经发出去过的
    staged = _stage(client, "已发.txt", b"already sent", "text/plain")
    sent = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "", "upload_ids": [str(staged["upload_id"])]},
    )
    assert sent.status_code == 200
    resp = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "再次", "upload_ids": [str(staged["upload_id"])]},
    )
    assert resp.status_code == 400

    # 被拒的这三条没有额外落库（只有那条成功的附件消息）
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == before + 1


def test_old_chat_upload_route_only_stages(client, conn, member_login, tmp_wiki,
                                           monkeypatch):
    """旧 chat upload 兼容壳：只暂存，不建消息、不解析、不起轮。"""
    from app.routers.api import chat as chat_router

    calls: list = []
    monkeypatch.setattr(chat_router, "_start_turn", lambda *a, **k: calls.append(a))
    rows_before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    resp = client.post(
        f"/api/chat/{member_login.session_id}/upload",
        files={"file": ("旧.txt", "旧路由只暂存".encode(), "text/plain")},
        data={"note": "说明"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "none"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages"
    ).fetchone()["n"] == rows_before
    row = conn.execute(
        "SELECT message_id, parsed_text_path FROM upload_files WHERE id = ?",
        (body["upload_id"],),
    ).fetchone()
    assert row["message_id"] is None and row["parsed_text_path"] is None
    assert calls == []


def test_history_shows_attachment_line_for_the_agent(client, conn, member_login, tmp_wiki):
    """runner._history 给人话追加 [附件] upload/{id}《文件名》（状态），模型据此摄入。"""
    from app.agent import runner as runner_module

    staged = _stage(client, "文档.txt", "文档的正文内容够长一些。".encode(), "text/plain")
    sent = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "整理这份", "upload_ids": [str(staged["upload_id"])]},
    )
    assert sent.status_code == 200, sent.text
    history = runner_module._history(conn, member_login.session_id)
    human = [m["content"] for m in history if m["role"] == "user"][-1]
    assert human.startswith(f"[消息{sent.json()['id']}] 整理这份")
    assert f"[附件] upload/{staged['upload_id']}《文档.txt》（已解析）" in human


def _epub_bytes() -> bytes:
    """最小可读 EPUB：container → OPF → 两个 XHTML 章节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>测试电子书</dc:title></metadata>'
            '<manifest>'
            '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest>'
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine>'
            '</package>',
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>不该出现</title>'
            '<style>p{color:red}</style></head><body><h1>第一章 起头</h1>'
            '<p>第一段<strong>正文</strong>。</p><script>alert(1)</script></body></html>',
        )
        zf.writestr(
            "OEBPS/ch2.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第二章 继续</h1>'
            '<ul><li>条目一</li><li>条目二</li></ul></body></html>',
        )
    return buf.getvalue()


def test_epub_extract_text_and_report(tmp_path):
    path = tmp_path / "电子书.epub"
    path.write_bytes(_epub_bytes())
    text, report = uploads_svc.extract(path)
    assert text is not None
    assert "# 测试电子书" in text
    assert "# 第一章 起头" in text
    assert "第一段正文。" in text
    assert "# 第二章 继续" in text
    assert "- 条目一" in text
    assert "alert(1)" not in text and "不该出现" not in text
    assert report == {"pages_total": 2, "pages_with_text": 2, "pages_image_only": 0}


def test_epub_broken_returns_failed_parse(tmp_path):
    path = tmp_path / "坏书.epub"
    path.write_bytes(b"not a zip")
    assert uploads_svc.extract(path) == (None, None)


def test_epub_upload_end_to_end(client, conn, member_login):
    resp = _upload(
        client, member_login, "电子书.epub", _epub_bytes(), "application/epub+zip"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "done"
    assert body["parse_report"] == {
        "pages_total": 2, "pages_with_text": 2, "pages_image_only": 0,
    }
    assert body["raw_path"]


# --------------------------------------- 2026-10-30：文档内嵌图片（PPT/PDF/DOCX）

def _big_png_bytes() -> bytes:
    """一张 >1KB 的 PNG：视觉描述测试用（1x1 的 _png_bytes 会被当图标跳过）。"""
    import os
    from PIL import Image

    buffer = io.BytesIO()
    # 随机像素：纯色 PNG 压缩后只有几百字节，会被 1KB 的图标阈值跳过
    pixels = os.urandom(120 * 120 * 3)
    Image.frombytes("RGB", (120, 120), pixels).save(buffer, format="PNG")
    return buffer.getvalue()


def test_pptx_pictures_are_described(client, conn, member_login, monkeypatch):
    """PPT 里的图片形状不再是静默忽略：送视觉、拼回该页、报告里计数。"""
    from io import BytesIO
    from pptx import Presentation

    from app.services import vision

    calls: list[str] = []

    def fake_describe(path) -> str:
        calls.append(str(path))
        return "图中是一张渠道漏斗截图，转化率 12%"

    monkeypatch.setattr(vision, "describe_image", fake_describe)

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "有图的页"
    slide.shapes.add_picture(BytesIO(_big_png_bytes()), Inches(1), Inches(1), Inches(3), Inches(2))
    buf = BytesIO()
    prs.save(buf)

    body = _upload(client, member_login, "带图页面.pptx", buf.getvalue(), PPTX_MIME).json()
    assert body["parse_status"] == "done"
    assert body["parse_report"]["images_total"] == 1
    assert body["parse_report"]["images_described"] == 1
    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "【第 1 页图片1】" in text
    assert "转化率 12%" in text
    assert calls, "图片没有被送视觉"


def test_pdf_image_page_is_rendered_and_described(client, conn, member_login, monkeypatch):
    """扫描/含图 PDF：文字层为空时整页渲染送视觉，parse_status 从 failed 变成 done。"""
    import pymupdf
    from io import BytesIO

    from app.services import vision

    monkeypatch.setattr(vision, "describe_image", lambda path: "图中是一页扫描表格：岗位、城市、薪资")

    png = _big_png_bytes()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(40, 40, 500, 320), stream=png)
    buf = BytesIO()
    doc.save(buf)
    doc.close()

    body = _upload(client, member_login, "扫描页.pdf", buf.getvalue(), "application/pdf").json()
    assert body["parse_status"] == "done", body
    assert body["parse_report"]["images_total"] == 1
    assert body["parse_report"]["images_described"] == 1
    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "【第 1 页图片】" in text
    assert "扫描表格" in text


def test_docx_tables_and_images(client, conn, member_login, monkeypatch):
    """Word 表格不再是空白；内嵌图片也送视觉并拼到正文尾部。"""
    import docx
    from io import BytesIO

    from app.services import vision

    monkeypatch.setattr(vision, "describe_image", lambda path: "图中是一张组织架构图")

    document = docx.Document()
    document.add_paragraph("先看表格")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "岗位"
    table.cell(0, 1).text = "要求"
    table.cell(1, 0).text = "产品实习"
    table.cell(1, 1).text = "能写清需求"
    document.add_picture(BytesIO(_big_png_bytes()))
    buf = BytesIO()
    document.save(buf)

    body = _upload(
        client, member_login, "带表带图.docx", buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ).json()
    assert body["parse_status"] == "done", body
    assert body["parse_report"]["images_total"] == 1
    assert body["parse_report"]["images_described"] == 1
    row = conn.execute(
        "SELECT parsed_text_path FROM upload_files WHERE id = ?", (body["upload_id"],)
    ).fetchone()
    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "| 岗位 | 要求 |" in text
    assert "能写清需求" in text
    assert "组织架构图" in text


def test_message_while_attachment_is_parsing_is_409(client, conn, member_login):
    """解析中的附件不许放进第二条消息（2026-10-30 测试账号的假失败）。

    PDF/视觉解析可能几分钟。解析发生在起轮之前，RUNNING 还没置位；如果这时放第二条
    消息进来，它会立刻起一轮，智能体看到 parse_status='none' 的附件就把"还在解析"
    误报成"这类不解析"，摄入看起来失败了。PARSING 是解析期间的独立闸。
    """
    from app.routers.api import chat as chat_router

    sid = member_login.session_id
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,)
    ).fetchone()["n"]
    chat_router.PARSING.add(sid)
    try:
        resp = client.post(
            f"/api/chat/{sid}/messages", data={"content": "等解析完再发"}
        )
        assert resp.status_code == 409, resp.text
        assert "附件还在解析" in resp.json()["error"]
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()["n"]
        assert after == before, "被 409 的消息不许先落库"
    finally:
        chat_router.PARSING.discard(sid)

    ok = client.post(f"/api/chat/{sid}/messages", data={"content": "解析完了"})
    assert ok.status_code == 200, ok.text
