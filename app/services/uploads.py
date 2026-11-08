"""上传材料：暂存（只落盘）→ 发送时解析（纯文本）→ 进共享检索。

S11 两段式：选文件走 `stage_upload`（不解析、不建消息、不进 FTS/raw）；用户点发送时
chat 路由才调 `finalize_upload`（解析/视觉/转换）+ `ingest_raw`。`save_upload` 保留成
"stage + finalize"的组合（旧调用与测试仍可用）。

三条承重口径：
1) 白名单外的扩展名也照样存（parse_status='none'），只是不进检索——收材料不设门槛；
2) 白名单内解析失败 → parse_status='failed'，原件仍在盘上（不提供下载），绝不连累上传；
3) 上限 50 MB，超限当场删掉半截文件并抛 FileTooLargeError（路由翻 413）。

S5b 起解析矩阵扩成 Hermes 式多来源（PDF/EPUB/Word/PPT/Excel/图片/老 Office）：
- `.xlsx` 走 openpyxl → 每个 sheet 一张 markdown 表；
- `.epub` 走 zip+OPF spine 按阅读顺序抽 XHTML 文本（无第三方依赖）；
- 图片（png/jpg/jpeg/webp/gif/bmp）走 `app.services.vision` 的 DeepSeek 视觉；
- **文档内嵌图片**（PPT 图片形状、PDF 扫描页/含图页、Word 图片）也走视觉：
  由 `app.services.media` 收集/去重/并行描述，拼回正文（2026-10-30 补齐）；
- 老格式 `.doc/.ppt/.xls` 有 `soffice`（libreoffice）就转成新格式再解析；没有就落
  parse_status='none'（"这类文件不解析"的现成文案，不是 failed——环境缺组件不是文件坏了）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import posixpath
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
import xml.etree.ElementTree as ET

from fastapi import UploadFile

from ..config import settings
from ..db import utcnow

logger = logging.getLogger(__name__)

MAX_SIZE = 50 * 1024 * 1024
CHUNK = 1024 * 1024
_SAFE_EXT = re.compile(r"[^A-Za-z0-9._-]")

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

# 直接能解析的格式（图片走 vision 服务，但同样算"可解析"：视觉失败记 failed 而不是 none）。
# 老 Office 格式也在列：有 soffice 时它们会被转成新格式走正常解析，转换/解析失败记
# failed；只有"系统没装 libreoffice"这一种情况由 extract 返回 unsupported → none。
PARSEABLE = {
    ".pdf", ".docx", ".txt", ".md", ".pptx", ".xlsx", ".epub",
    ".doc", ".ppt", ".xls",
} | set(IMAGE_SUFFIXES)
# 老 Office 格式 → 转换目标（soffice --convert-to 的后缀）。
CONVERTIBLE = {".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx"}
LEGACY_NOTE = "需要服务器安装 libreoffice（soffice）才能转换 .doc/.ppt/.xls"

# xlsx 转 markdown 的上限：只为护住上下文与解析耗时，超出部分不解析（不报错）。
SHEET_MAX = 20
SHEET_ROWS = 200
SHEET_COLS = 20

# 大文档的解析上限：超了不解析（parse_status='none'），别把"书太大"记成"文件坏了"。
PDF_MAX_PAGES = 2000
PPTX_MAX_SLIDES = 1000
EPUB_MAX_DOCS = 2000


def _extension(filename: str) -> str:
    suffix = Path(filename or "").suffix
    return _SAFE_EXT.sub("", suffix)[:16].lower()


def store_path_for(user_id: int, filename: str) -> Path:
    directory = settings.uploads_dir / str(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid.uuid4().hex}{_extension(filename)}"


def _store_file(user_id: int, upload: UploadFile) -> tuple[Path, int, str]:
    """把上传字节落到 data/uploads/{user_id}/，返回 (路径, 字节数, sha256)。

    50MB 上限在写的过程中数：超了当场删掉半截文件再抛 FileTooLargeError（路由翻 413）。
    """
    destination = store_path_for(user_id, upload.filename or "")
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("wb") as handle:
            while chunk := upload.file.read(CHUNK):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise FileTooLargeError()
                digest.update(chunk)
                handle.write(chunk)
    except FileTooLargeError:
        destination.unlink(missing_ok=True)
        raise
    return destination, size, digest.hexdigest()


def stage_upload(
    conn: sqlite3.Connection, user_id: int, upload: UploadFile
) -> dict:
    """**暂存**一份附件：只落盘 + 插一行，不解析、不建消息、不进 FTS/raw。

    S11 的两段式：选文件时只到这里（输入框里显示胶囊），点发送时 chat 路由才
    finalize（解析/视觉）并把 message_id 挂上。原先"选中即解析、即触发智能体"的
    行为按用户裁定去掉——大文件/图片的视觉调用不该在选文件时就烧掉。
    """
    destination, size, digest = _store_file(user_id, upload)
    cur = conn.execute(
        "INSERT INTO upload_files (message_id, user_id, orig_filename, stored_path, size,"
        " mime, sha256, parse_status, parsed_text_path, parse_report, created_at)"
        " VALUES (NULL, ?, ?, ?, ?, ?, ?, 'none', NULL, NULL, ?)",
        (
            user_id,
            upload.filename or "未命名文件",
            str(destination.relative_to(settings.data_dir)),
            size,
            upload.content_type,
            digest,
            utcnow(),
        ),
    )
    return {
        "upload_id": int(cur.lastrowid),
        "orig_filename": upload.filename or "未命名文件",
        "size": size,
        "mime": upload.content_type,
        "parse_status": "none",
    }


def finalize_upload(conn: sqlite3.Connection, upload_id: int) -> dict:
    """**发送时**才解析一份暂存附件：写解析文本 + 更新 parse_status + 进 FTS。

    幂等：已经 done 的直接返回（不重复解析/不重复索引）。原件不在盘上给失败人话。
    返回值给 chat 路由拼响应摘要：{ok, upload_id, parse_status, parse_report, chars}。
    raw 归档不在这一步（需要 actor；发送路由对成功项再调 ingest_raw）。
    """
    row = conn.execute(
        "SELECT * FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        return {
            "ok": False, "upload_id": upload_id, "parse_status": None,
            "parse_report": None, "chars": 0, "note": "没有这份材料",
        }
    if row["parse_status"] == "done":
        return {
            "ok": True, "upload_id": upload_id, "parse_status": "done",
            "parse_report": json.loads(row["parse_report"]) if row["parse_report"] else None,
            "chars": _parsed_chars(row["parsed_text_path"]),
            "already": True,
        }
    path = settings.data_dir / row["stored_path"]
    if not path.exists():
        return {
            "ok": False, "upload_id": upload_id, "parse_status": row["parse_status"],
            "parse_report": None, "chars": 0, "note": "原件不在了，解析不了",
        }

    text, report = extract(path)
    parsed_text_path = _write_parsed_text(path, text) if text else None
    suffix = path.suffix.lower()
    if parsed_text_path:
        parse_status = "done"
    elif report and report.get("unsupported"):
        # 环境缺组件（老 Office 没有 libreoffice）：这是"这类不解析"，不是文件坏了。
        parse_status = "none"
    elif suffix in PARSEABLE:
        parse_status = "failed"
    else:
        parse_status = "none"
    conn.execute(
        "UPDATE upload_files SET parse_status = ?, parsed_text_path = ?, parse_report = ?"
        " WHERE id = ?",
        (
            parse_status,
            parsed_text_path,
            json.dumps(report, ensure_ascii=False) if report is not None else None,
            upload_id,
        ),
    )
    if parsed_text_path:
        from . import search

        search.index_upload(conn, upload_id)
    return {
        "ok": True, "upload_id": upload_id, "parse_status": parse_status,
        "parse_report": report, "chars": len(text or ""),
        "orig_filename": row["orig_filename"],
    }


def save_upload(
    conn: sqlite3.Connection, message_id: int | None, user_id: int, upload: UploadFile
) -> dict:
    """旧入口 = stage + finalize 的组合（测试/旧调用可用），行为与旧版一致。

    注意：旧版也不负责 raw 归档（那是调用方的事）；这里同样不 ingest_raw。
    """
    staged = stage_upload(conn, user_id, upload)
    upload_id = staged["upload_id"]
    if message_id is not None:
        conn.execute(
            "UPDATE upload_files SET message_id = ? WHERE id = ?", (message_id, upload_id)
        )
    fin = finalize_upload(conn, upload_id)
    return {
        "upload_id": upload_id,
        "parse_status": fin["parse_status"],
        "parse_report": fin["parse_report"],
    }


def discard_staged_upload(
    conn: sqlite3.Connection, upload_id: int, user_id: int
) -> tuple[bool, str]:
    """撤掉一份**未发送**的暂存附件：删盘 + 删行 + 清 FTS 残留。

    返回 (ok, note)：note 为中文原因（路由按 404/400 翻）。只允许本人的件；
    已经挂上消息的（message_id 非空）不能再撤——内容已进聊天记录与智能体上下文。
    """
    row = conn.execute(
        "SELECT id, user_id, message_id, stored_path, parsed_text_path"
        " FROM upload_files WHERE id = ?",
        (upload_id,),
    ).fetchone()
    if row is None or int(row["user_id"]) != int(user_id):
        return False, "没有这份材料"
    if row["message_id"] is not None:
        return False, "附件已经发送，不能撤"
    for rel in (row["stored_path"], row["parsed_text_path"]):
        if rel:
            (settings.data_dir / rel).unlink(missing_ok=True)
    conn.execute("DELETE FROM upload_files WHERE id = ?", (upload_id,))
    from . import search

    search.index_upload(conn, upload_id)  # 行已删：只清 FTS 旧行（幂等）
    return True, ""


class FileTooLargeError(Exception):
    pass


def extract(path: Path) -> tuple[str | None, dict | None]:
    """把一个文件转成纯文本 + 一份页数报告，供检索、引注校验与前端提示用。

    只解析不落盘（落盘是 save_upload 的事）。返回值口径（S5b 扩展后）：
    - `(text, report)`：解析成功（report 仍是 pages_total/with_text/image_only 形状）；
    - `(None, {"unsupported": True, "note": ...})`：这类文件目前解析不了（老 Office
      没装 libreoffice）——save_upload 据此记 parse_status='none'，不是 failed；
    - `(None, None)`：白名单外，或解析真的炸了（save_upload 记 failed / none）。
    """
    suffix = path.suffix.lower()
    try:
        if suffix in CONVERTIBLE:
            return _extract_legacy(path, suffix)
        if suffix in IMAGE_SUFFIXES:
            return _extract_image(path)
        if suffix not in PARSEABLE:
            return None, None
        return _extract_modern(path, suffix)
    except Exception as exc:  # noqa: BLE001 - 解析失败绝不连累上传（裁定 R59 留痕）
        # 没有这一行，"部署里缺 python-pptx"（import 就炸）与"用户传了个假 .pptx"
        # （zip 都打不开）在库里长得一模一样，运维分不清该修环境还是该找用户。
        logger.warning("解析失败 %s（%s）：%r", path.name, suffix, exc)
        return None, None


def _pdf_pages_with_images(
    path: Path, pages: list[str]
) -> tuple[list[str], dict, str | None]:
    """PDF 的每个文字页原样保留；文字很少（含扫描页）或有内嵌图片的页整页渲染后送视觉。

    用 PyMuPDF 渲染而不是只抽 `get_images`：扫描页是一整张图、图表是矢量绘制，
    抽内嵌图会漏；整页渲染一次也顺带把版式与图表标签带进去。每页最多一次视觉调用。
    """
    from .media import IMAGE_DESC_MAX, ImageDescriptions

    desc = ImageDescriptions()
    try:
        import pymupdf  # PyMuPDF：只用于渲染/图片，文字仍走 pypdf（口径不变）
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF %s 没做图片解析（PyMuPDF 不可用）：%r", path.name, exc)
        return pages, {}, None
    labels: list[tuple[int, str]] = []
    try:
        with pymupdf.open(str(path)) as doc:
            for index, page in enumerate(doc):
                if desc.total >= IMAGE_DESC_MAX:
                    desc.truncated = True
                    break
                text = pages[index] if index < len(pages) else ""
                try:
                    has_images = bool(page.get_images(full=True))
                except Exception:  # noqa: BLE001
                    has_images = False
                if text.strip() and not has_images:
                    continue
                pixmap = page.get_pixmap(dpi=144, alpha=False)
                label = f"第 {index + 1} 页图片"
                desc.add(label, pixmap.tobytes("png"), ".png")
                labels.append((index, label))
    except Exception as exc:  # noqa: BLE001 —— 渲染失败只影响图片，不连累文字
        logger.warning("PDF %s 图片渲染失败：%r", path.name, exc)
    described = dict(desc.describe())
    merged = list(pages)
    for index, label in labels:
        text = described.get(label)
        if not text:
            continue
        prefix = merged[index].strip()
        merged[index] = (prefix + "\n\n" if prefix else "") + f"【{label}】\n{text}"
    return merged, desc.report(), desc.note()


def _iter_picture_shapes(shapes):
    """递归取 PPT 形状树里的图片（组合形状里的图片也要算）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        try:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from _iter_picture_shapes(shape.shapes)
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            image = shape.image  # 只有图片形状有；非图片抛 AttributeError
        except Exception:  # noqa: BLE001
            continue
        yield image


def _pptx_tables_markdown(shapes) -> list[str]:
    """PPT 表格 → markdown（之前只取文本框，表格里的字全丢）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    out: list[str] = []
    for shape in shapes:
        try:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                out += _pptx_tables_markdown(shape.shapes)
                continue
            if not shape.has_table:
                continue
            rows = [
                [cell.text.replace("|", "\\|").replace("\n", " ").strip() for cell in row.cells]
                for row in shape.table.rows
            ]
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |",
                 "| " + " | ".join("---" for _ in range(width)) + " |"]
        lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
        out.append("\n".join(lines))
    return out


def _pptx_pages_with_images(slides) -> tuple[list[str], dict, str | None]:
    """每页：文本框 + 表格；图片按页收集，最后并行送视觉并拼回该页。"""
    from .media import ImageDescriptions

    desc = ImageDescriptions()
    pages: list[str] = []
    slide_labels: list[tuple[int, str]] = []
    for index, slide in enumerate(slides):
        parts = [
            para.text
            for shape in slide.shapes
            if shape.has_text_frame
            for para in shape.text_frame.paragraphs
        ]
        parts += _pptx_tables_markdown(slide.shapes)
        pages.append("\n".join(t for t in parts if t.strip()))
        for number, image in enumerate(_iter_picture_shapes(slide.shapes), start=1):
            label = f"第 {index + 1} 页图片{number}"
            try:
                desc.add(label, image.blob, image.ext)
            except Exception as exc:  # noqa: BLE001
                logger.warning("PPT 第 %s 页图片读不出来：%r", index + 1, exc)
                continue
            slide_labels.append((index, label))
    described = dict(desc.describe())
    for index, label in slide_labels:
        text = described.get(label)
        if not text:
            continue
        prefix = pages[index].strip()
        pages[index] = (prefix + "\n\n" if prefix else "") + f"【{label}】\n{text}"
    return pages, desc.report(), desc.note()


def _docx_tables_markdown(document) -> list[str]:
    """Word 表格 → markdown（之前只取段落，表格里的字全丢）。"""
    out: list[str] = []
    for table in document.tables:
        rows = [
            [cell.text.replace("|", "\\|").replace("\n", " ").strip() for cell in row.cells]
            for row in table.rows
        ]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |",
                 "| " + " | ".join("---" for _ in range(width)) + " |"]
        lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
        out.append("\n".join(lines))
    return out


def _docx_with_images(document, text: str) -> tuple[str, dict, str | None]:
    """正文尾部追加文档内图片描述（Word 图片与文字的位置关系太散，不做逐段对齐）。"""
    from .media import ImageDescriptions

    desc = ImageDescriptions()
    seen_parts: set[str] = set()
    for part in document.part.related_parts.values():
        content_type = getattr(part, "content_type", "")
        if not content_type.startswith("image/"):
            continue
        name = str(getattr(part, "partname", ""))
        if name in seen_parts:
            continue
        seen_parts.add(name)
        try:
            desc.add(f"文档图片 {len(seen_parts)}", part.blob, Path(name).suffix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DOCX 图片读不出来（%s）：%r", name, exc)
    described = desc.describe()
    if described:
        text = text + "\n\n" + "\n\n".join(f"【{label}】\n{value}" for label, value in described)
    return text, desc.report(), desc.note()


def _extract_modern(path: Path, suffix: str) -> tuple[str | None, dict | None]:
    """直接解析的格式：pdf / docx / pptx / xlsx / txt / md（不含图片与老 Office）。"""
    report = None
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        total = len(reader.pages)
        if total > PDF_MAX_PAGES:
            return None, {
                "unsupported": True,
                "note": f"这份 PDF 有 {total} 页，超过 {PDF_MAX_PAGES} 页上限，没有解析",
            }
        pages = [page.extract_text() or "" for page in reader.pages]
        # 内嵌图片/扫描页：文字很少或页内有图片时整页渲染成 PNG 送视觉
        # （2026-10-30 补齐；之前只取文字层，扫描版 PDF 整份 failed）。
        pages, image_report, image_note = _pdf_pages_with_images(path, pages)
        report = _report(pages)
        report.update(image_report)
        if image_note:
            report["note"] = image_note
        text = "\n".join(pages)
    elif suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        blocks = [p.text for p in document.paragraphs if p.text.strip()]
        blocks += _docx_tables_markdown(document)
        text, image_report, image_note = _docx_with_images(document, "\n\n".join(blocks))
        report = _report([text])
        report.update(image_report)
        if image_note:
            report["note"] = image_note
    elif suffix == ".pptx":
        from pptx import Presentation

        slides = list(Presentation(str(path)).slides)
        if len(slides) > PPTX_MAX_SLIDES:
            return None, {
                "unsupported": True,
                "note": f"这份 PPT 有 {len(slides)} 页幻灯片，超过 {PPTX_MAX_SLIDES} 页上限，没有解析",
            }
        # 每页：文本框 + 表格 + 图片（图片送视觉，2026-10-30 补齐；之前图片形状被静默忽略）
        pages, image_report, image_note = _pptx_pages_with_images(slides)
        report = _report(pages)
        report.update(image_report)
        if image_note:
            report["note"] = image_note
        text = "\n\n".join(pages)
    elif suffix == ".xlsx":
        text = _xlsx_to_markdown(path)
        report = _report([text])          # xlsx 视为 1 份材料（有表就算有文字）
    elif suffix == ".epub":
        return _extract_epub(path)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        report = _report([text])
    text = (text or "").strip()
    return (text or None), report


class _EpubHTMLParser(HTMLParser):
    """EPUB 里的 XHTML → 可读文本；保留标题/列表/段落，丢掉脚本样式。"""

    _SKIP = {"script", "style", "noscript", "svg", "head"}
    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    _BLOCKS = {"p", "div", "section", "article", "blockquote", "br", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._heading = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in self._HEADINGS:
            self._heading = self._HEADINGS[tag]
            self.parts.append("\n\n" + "#" * self._heading + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCKS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in self._HEADINGS or tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _extract_epub(path: Path) -> tuple[str | None, dict | None]:
    """EPUB（zip + OPF spine）→ 按阅读顺序拼正文；不依赖第三方库。

    只读 zip 内的 container.xml / OPF / XHTML；不落盘、不执行脚本。
    DRM、损坏包、没有可读正文都返回 (None, report) → parse_status='failed'。
    """
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "META-INF/container.xml" not in names:
            raise ValueError("EPUB 缺 META-INF/container.xml")
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = next(
            (el for el in container.iter() if _local_name(el.tag) == "rootfile"),
            None,
        )
        if rootfile is None:
            raise ValueError("EPUB container 没有 rootfile")
        opf_path = rootfile.get("full-path", "")
        if not opf_path or opf_path not in names:
            raise ValueError("EPUB 的 OPF 不存在")
        opf = ET.fromstring(zf.read(opf_path))

        title = ""
        manifest: dict[str, str] = {}
        spine: list[str] = []
        for el in opf.iter():
            name = _local_name(el.tag)
            if name == "title" and not title:
                title = " ".join((el.text or "").split())
            elif name == "item":
                item_id = el.get("id", "")
                href = el.get("href", "")
                if item_id and href:
                    manifest[item_id] = href
            elif name == "itemref":
                idref = el.get("idref", "")
                if idref:
                    spine.append(idref)

        if len(spine) > EPUB_MAX_DOCS:
            return None, {
                "unsupported": True,
                "note": f"这本 EPUB 有 {len(spine)} 个文档，超过 {EPUB_MAX_DOCS} 个上限，没有解析",
            }

        base = PurePosixPath(opf_path).parent
        docs: list[str] = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            # 允许 OPF 里写 ../ 或 ./；只读 zip 内路径，绝不落盘
            normalized = posixpath.normpath(posixpath.join(str(base), unquote(href)))
            if normalized not in names:
                continue
            try:
                raw = zf.read(normalized).decode("utf-8", errors="replace")
            except KeyError:
                continue
            parser = _EpubHTMLParser()
            parser.feed(raw)
            parser.close()
            body = parser.text()
            if body:
                docs.append(body)

        if not docs:
            return None, _report([])
        text = "\n\n".join(docs).strip()
        if title:
            text = f"# {title}\n\n{text}"
        return text, _report(docs)


def _extract_image(path: Path) -> tuple[str | None, dict | None]:
    """图片走 DeepSeek 视觉：>10 MB 直接失败并记 note；视觉没回文字也记 note。"""
    from . import vision

    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("读不到图片 %s：%r", path.name, exc)
        return None, None
    if size > vision.VISION_MAX_BYTES:
        return None, {
            **_report([""]),
            "note": f"图片超过 {vision.VISION_MAX_BYTES // (1024 * 1024)} MB，没送视觉模型",
        }
    text = vision.describe_image(path)
    report = _report([text or ""])
    if not text:
        report["note"] = "视觉模型没返回文字（可能没配 LLM_API_KEY（旧名 DEEPSEEK_API_KEY） 或调用失败）"
    return text, report


def _run_soffice(argv: list[str]) -> subprocess.CompletedProcess:
    """跑一次 libreoffice 转换（超时 120s）。单独提出来是为了测试能整段打桩。

    不把 `subprocess.run` 这个模块属性换掉：app/wiki/repo.py 用的是同一个模块对象，
    patch 它会连 git 子进程一起换成假的（测试里真踩到过）。
    """
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _extract_legacy(path: Path, suffix: str) -> tuple[str | None, dict | None]:
    """老 Office（.doc/.ppt/.xls）：有 soffice 就转成新格式再解析，没有就报"不支持"。"""
    soffice = shutil.which("soffice")
    if soffice is None:
        return None, {"unsupported": True, "note": LEGACY_NOTE}
    target_suffix = CONVERTIBLE[suffix]
    with tempfile.TemporaryDirectory(prefix="openllmwiki-convert-") as tmp:
        argv = [
            soffice, "--headless", "--convert-to", target_suffix.lstrip("."),
            "--outdir", tmp, str(path),
        ]
        proc = _run_soffice(argv)
        converted = Path(tmp) / f"{path.stem}{target_suffix}"
        if proc.returncode != 0 or not converted.exists():
            logger.warning(
                "libreoffice 转换失败 %s（exit=%s）：%s",
                path.name, proc.returncode, (proc.stderr or "").strip()[:300],
            )
            return None, None
        return _extract_modern(converted, target_suffix)


def _xlsx_to_markdown(path: Path) -> str:
    """一个工作簿 → 每个非空 sheet 一张 markdown 表（`## Sheet: 名字` + 表头/行）。

    上限：SHEET_MAX 个 sheet、每 sheet SHEET_ROWS 行 × SHEET_COLS 列——整本台账塞进
    上下文没有意义，护住窗口与解析耗时。空 sheet/空行跳过；单元格里的 `|` 转义、
    换行压成空格（否则一张表会被劈成两半）。
    """
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for name in list(workbook.sheetnames)[:SHEET_MAX]:
            sheet = workbook[name]
            rows: list[list[str]] = []
            for values in sheet.iter_rows(
                max_row=SHEET_ROWS, max_col=SHEET_COLS, values_only=True
            ):
                cells = ["" if value is None else _md_cell(value) for value in values]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    rows.append(cells)
            if not rows:
                continue
            width = max(len(row) for row in rows)
            rows = [row + [""] * (width - len(row)) for row in rows]
            lines = [
                f"## Sheet: {name}",
                "",
                "| " + " | ".join(rows[0]) + " |",
                "| " + " | ".join("---" for _ in range(width)) + " |",
            ]
            lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
    finally:
        workbook.close()


def _md_cell(value) -> str:
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")


def _report(pages: list[str]) -> dict:
    with_text = sum(1 for p in pages if p.strip())
    return {
        "pages_total": len(pages),
        "pages_with_text": with_text,
        "pages_image_only": len(pages) - with_text,
    }


def _write_parsed_text(original: Path, text: str) -> str:
    """解析出的纯文本落到 data/extracted_text，返回库里存的相对路径。

    文件名用存盘后的 uuid 主名而不是用户给的原名：同名材料传两次不会互相覆盖，
    且这个名字里没有用户可控字符。
    """
    settings.extracted_text_dir.mkdir(parents=True, exist_ok=True)
    target = settings.extracted_text_dir / f"{original.stem}.txt"
    target.write_text(text, encoding="utf-8")
    return str(target.relative_to(settings.data_dir))


def _parsed_chars(parsed_text_path: str | None) -> int:
    """解析文本的字符数（读不到给 0）——只用于响应摘要，不参与业务判断。"""
    if not parsed_text_path:
        return 0
    try:
        return len((settings.data_dir / parsed_text_path).read_text(
            encoding="utf-8", errors="replace"
        ))
    except OSError:
        return 0


def ingest_raw(conn: sqlite3.Connection, upload_id: int, filename: str, actor: str) -> str | None:
    """把一份**解析成功**的上传材料归进 wiki 的 raw 层，返回 raw 相对 path（没成就 None）。

    S5 计划 §3：raw/uploads/{id}-{safe}.md（frontmatter：source_file/upload_id/sha256/
    ingested，body=解析文本）+ 追加 log + 一次 git commit（kind=raw, action=ingest，
    见 store.ingest_raw_upload）。raw 是**来源层**，不是上传成功的前提：解析失败/这类
    不解析（parse_status != 'done'）就没有正文可归；摄入过程里任何异常都只记 warning，
    绝不让上传本身失败——原件与解析文本这时都已经落盘了。

    解析文本以 data/extracted_text 为唯一事实源（与引注逐字校验同一个口径），不重跑
    解析：save_upload 刚写完那份文件，这里只是把它抄进 raw 的不可变归档。
    """
    from ..wiki import store  # 懒导入：wiki 侧反过来要 services.search，模块级会成环

    from . import search

    row = conn.execute(
        "SELECT parse_status FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None or row["parse_status"] != "done":
        return None
    try:
        text = search.upload_text(conn, upload_id)
        result = store.ingest_raw_upload(conn, upload_id, filename, text or "", actor)
    except Exception as exc:  # noqa: BLE001 - raw 是旁路：失败只记日志，不影响上传成功
        logging.getLogger(__name__).warning(
            "raw 摄入没做成（upload_id=%s）：%r", upload_id, exc, exc_info=exc
        )
        return None
    if not result.get("ok"):
        note = (result.get("failed") or [{}])[0].get("note") or "原因不明"
        logging.getLogger(__name__).warning("raw 摄入没做成（upload_id=%s）：%s", upload_id, note)
        return None
    return result.get("path")
