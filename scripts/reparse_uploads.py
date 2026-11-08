#!/usr/bin/env python3
"""补录已摄入材料的内嵌图片（2026-10-30 新解析能力的回填脚本）。

2026-10-30 起 PPT/PDF/DOCX 里的内嵌图片会走视觉描述；这个脚本把**之前已经摄入过**的
同类材料重新解析一遍，把图片描述补进 extracted_text、FTS 与 wiki raw 层。

用法（在仓库根跑，DATA_DIR 默认读 .env）：

    .venv/bin/python scripts/reparse_uploads.py --dry-run
    .venv/bin/python scripts/reparse_uploads.py --ids 10 20 22 23
    .venv/bin/python scripts/reparse_uploads.py --all        # 所有候选
    .venv/bin/python scripts/reparse_uploads.py --all --no-raw

行为：
- 只挑 `.pdf/.pptx/.docx` 且 parse_status ∈ {done, failed} 的记录（扫描版 PDF 之前是 failed，
  补录后应变成 done）；
- `extract()` 重新解析（会真实调用视觉，产生费用），成功才覆盖 extracted_text；
  解析失败保留旧文本，只记失败；
- 更新 parse_report（加 `backfilled` 标记，重复跑默认跳过）、重灌 FTS；
- 默认同时刷新 `raw/uploads/{id}-…md` 的正文（保留 frontmatter 与 sha256，提交
  `raw|update|…`），这样图片描述才能被引注；`--no-raw` 可关掉；
- 每份材料打印一行进度；结束打印 JSON 汇总。失败不中断其余材料。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.services import search  # noqa: E402
from app.services import uploads as uploads_svc  # noqa: E402
from app.wiki import store  # noqa: E402

IMAGE_SUFFIXES = {".pdf", ".pptx", ".docx"}
BACKFILLED = "2026-10-30"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="reparse_uploads")
    parser.add_argument("--ids", nargs="*", type=int, default=None, help="只处理这些上传 id")
    parser.add_argument("--all", action="store_true", help="处理全部候选（默认只 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只列候选，不解析、不写库")
    parser.add_argument("--force", action="store_true", help="已有 backfilled 标记也重跑")
    parser.add_argument("--no-raw", action="store_true", help="不刷新 wiki raw 层")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几份（0=不限）")
    parser.add_argument("--actor", default="reparse", help="raw 更新提交里的 actor 字段")
    return parser.parse_args()


def _report(row) -> dict:
    raw = row["parse_report"]
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def candidates(conn, args) -> list:
    sql = (
        "SELECT id, orig_filename, stored_path, parse_status, parse_report"
        " FROM upload_files WHERE parse_status IN ('done', 'failed') ORDER BY id"
    )
    rows = list(conn.execute(sql))
    out = []
    for row in rows:
        suffix = Path(str(row["stored_path"])).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            continue
        if args.ids and int(row["id"]) not in args.ids:
            continue
        if not args.force and _report(row).get("backfilled") == BACKFILLED:
            continue
        out.append(row)
        if args.limit and len(out) >= args.limit:
            break
    return out


def main() -> int:
    args = parse_args()
    conn = connect()
    try:
        rows = candidates(conn, args)
        if not rows:
            print("没有需要补录的材料。")
            return 0
        print(f"候选 {len(rows)} 份：")
        for row in rows:
            print(f"  #{row['id']} {row['orig_filename']}（{row['parse_status']}）")
        if args.dry_run or not args.all:
            print("（dry-run；加 --all 才真正解析）")
            return 0

        summary = {"ok": 0, "failed": 0, "skipped": 0, "images": 0, "described": 0}
        for row in rows:
            upload_id = int(row["id"])
            path = settings.data_dir / str(row["stored_path"])
            started = time.monotonic()
            if not path.exists():
                print(f"  #{upload_id} 跳过：原件不在 {path}")
                summary["skipped"] += 1
                continue
            try:
                text, report = uploads_svc.extract(path)
            except Exception as exc:  # noqa: BLE001
                print(f"  #{upload_id} 失败：{exc}")
                traceback.print_exc(limit=2)
                summary["failed"] += 1
                continue
            if not text:
                print(f"  #{upload_id} 失败：解析没有产出文本（保留旧文本）")
                summary["failed"] += 1
                continue

            report = dict(report or {})
            report["backfilled"] = BACKFILLED
            parsed_text_path = uploads_svc._write_parsed_text(path, text)
            conn.execute(
                "UPDATE upload_files SET parse_status = 'done', parsed_text_path = ?,"
                " parse_report = ? WHERE id = ?",
                (parsed_text_path, json.dumps(report, ensure_ascii=False), upload_id),
            )
            reindexed = search.index_upload(conn, upload_id)
            conn.commit()

            raw_note = ""
            if not args.no_raw:
                try:
                    raw = store.refresh_raw_upload(
                        conn, upload_id, str(row["orig_filename"]), text, args.actor
                    )
                    raw_note = "raw=ok" if raw.get("ok") else f"raw={raw.get('failed')}"
                except Exception as exc:  # noqa: BLE001
                    raw_note = f"raw=失败 {exc}"

            summary["ok"] += 1
            summary["images"] += int(report.get("images_total") or 0)
            summary["described"] += int(report.get("images_described") or 0)
            elapsed = time.monotonic() - started
            print(
                f"  #{upload_id} ok {elapsed:.1f}s chars={len(text)}"
                f" images={report.get('images_total', 0)}"
                f" described={report.get('images_described', 0)}"
                f" fts={'新' if reindexed else '未变'} {raw_note}"
            )
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if summary["failed"] == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
