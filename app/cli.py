from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

from .config import settings
from .db import connect, init_db, utcnow
from .services import git_sync
from .services.auth import (
    create_reset_invite,
    hash_password,
    normalize_phone,
)
from .wiki import repo, store


def _ensure_wiki(conn: sqlite3.Connection, actor: str) -> None:
    """建 data/wiki 这个 git 仓库，并把它初始化成 Hermes 物理 vault（计划 §1/§7）。

    建目录、SCHEMA.md/index.md/log.md、旧布局幂等迁移，全在 store.ensure_vault 里
    （它内部先迁移再补特殊文件，最后用一次 commit 落盘）。cli 不再自己 shell out 到 git：
    那条路绕开了 repo 钉死的 GIT_DIR/GIT_WORK_TREE 与显式 utf-8。
    SCHEMA.md 属于 wiki 仓库（见 app/wiki_config.py 的应用配置说明）；本命令只负责把 vault 备好。
    幂等：vault 已是新布局时 ensure_vault 一个文件都不动、也不产生提交。
    """
    fresh = not (settings.wiki_dir / ".git").exists()
    try:
        repo.ensure_repo()
    except repo.GitError as exc:
        sys.exit(f"data/wiki 的版本库没建起来：{exc}")
    if fresh:
        print(f"wiki 仓库已建：{settings.wiki_dir}")
    try:
        result = store.ensure_vault(conn)
    except store.StoreError as exc:
        sys.exit(f"wiki vault 没初始化成：{exc}")
    if result["changed"]:
        print(f"LLM-Wiki vault 已就绪（本次写入：{'、'.join(result['changed'])}）")
    else:
        print("LLM-Wiki vault 已就绪（已经是新布局，没有改动）")


def seed(args: argparse.Namespace) -> None:
    init_db()
    conn = connect()
    try:
        phone = normalize_phone(args.admin_phone or settings.admin_phone)
        password = args.admin_password or settings.admin_password
        if phone is None or not password:
            sys.exit("缺管理员的手机号或密码。用 --admin-phone / --admin-password 传，或写进 .env")
        name = args.admin_name or settings.admin_name
        existing = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO users (name, phone, password_hash, role, created_at, last_login_at)"
                " VALUES (?, ?, ?, 'admin', ?, ?)",
                (name, phone, hash_password(password), utcnow(), utcnow()),
            )
            print(f"管理员账号已建：手机号={phone}")
        else:
            print(f"管理员账号已存在（id={existing['id']}），跳过")
        conn.commit()
        # vault 的初始化（迁移 + SCHEMA/index/log）与账号写在同一轮 seed 里。
        _ensure_wiki(conn, name)
        conn.commit()
    finally:
        conn.close()


def set_admin(args: argparse.Namespace) -> None:
    """把某个手机号设为管理员；没注册就现场建号。

    已存在：只改 role（停用的一并恢复，管理员不能是停用态），不动密码。
    不存在：有 --password 就建；没有就随机密码建号，并打印一次性重设链接，让本人自己设密码。
    这样线上/本地都能用一条命令处理，不需要手工改 SQLite。
    """
    init_db()
    conn = connect()
    try:
        phone = normalize_phone(args.phone)
        if phone is None:
            sys.exit("手机号看起来不对")
        user = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        if user is not None:
            conn.execute(
                "UPDATE users SET role = ?, deactivated_at = NULL WHERE id = ?",
                ("admin" if not args.demote else "member", user["id"]),
            )
            conn.commit()
            action = "已降为成员" if args.demote else "已设为管理员"
            print(f"{action}：{user['name']}（id={user['id']}，手机号={phone}）")
            return

        if args.demote:
            sys.exit(f"手机号 {phone} 还没注册，谈不上降级")
        name = args.name or "管理员"
        password = args.password or secrets.token_urlsafe(24)
        cur = conn.execute(
            "INSERT INTO users (name, phone, password_hash, role, created_at, last_login_at)"
            " VALUES (?, ?, ?, 'admin', ?, ?)",
            (name, phone, hash_password(password), utcnow(), utcnow()),
        )
        user_id = int(cur.lastrowid)
        conn.commit()
        print(f"管理员账号已建：{name}（id={user_id}，手机号={phone}）")
        if args.password:
            print("密码已按 --password 设置，请让本人尽快登录后自行修改。")
        else:
            token = create_reset_invite(conn, user_id, user_id)
            conn.commit()
            base = (args.base_url or "http://127.0.0.1:8000").rstrip("/")
            print(f"一次性设置密码链接（24 小时内尽快使用，用一次失效）：{base}/i/{token}")
    finally:
        conn.close()


def backup(args: argparse.Namespace) -> None:
    """备份数据库、上传目录、解析文本目录，以及 data/wiki 这个事实源（裁定 R35 + R81）。

    wiki 单独一份档，成员形状与 uploads 那份一致（`wiki/...`），恢复同样是
    `tar -xzf data/backups/wiki-*.tar.gz -C data/`。tar 走的是整棵目录树、不跳点文件，
    所以 `data/wiki/.git` 整个进档，恢复出来是**带历史**的仓库，不是一堆没版本的 md。
    缺 .git 就当场退出，而不是"少一份档也算成功"：那正是 R35 这个洞的形状——
    备份看着绿，恢复时才发现事实源没进去。

    裁定 R81：data/extracted_text 也进档（`extracted_text-{stamp}.tar.gz`，恢复
    `tar -xzf ... -C data/`）。它是上传件的解析文本，理论上可再生（reindex 重解析），
    但再生要原件与解析器都在位；db 里的 parsed_text_path 指着它，缺了它恢复出来的
    站点是"材料在、引注与检索全断"。目录由 init_db 幂等建出，与 uploads 同口径：
    存在就直接进档，不单独设"缺了就拒"的闸（那是事实源 wiki 才有的待遇）。
    """
    if not (settings.wiki_dir / ".git").exists():
        sys.exit(f"data/wiki 里没有版本库，事实源备不了份：{settings.wiki_dir}")
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    db_backup = settings.backups_dir / f"db-{stamp}.sqlite3"
    source = sqlite3.connect(settings.db_path)
    try:
        target = sqlite3.connect(db_backup)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    shutil.make_archive(
        str(settings.backups_dir / f"uploads-{stamp}"), "gztar",
        root_dir=settings.data_dir, base_dir="uploads",
    )
    shutil.make_archive(
        str(settings.backups_dir / f"extracted_text-{stamp}"), "gztar",
        root_dir=settings.data_dir, base_dir="extracted_text",
    )
    shutil.make_archive(
        str(settings.backups_dir / f"wiki-{stamp}"), "gztar",
        root_dir=settings.data_dir, base_dir="wiki",
    )
    for pattern in (
        "db-*.sqlite3", "uploads-*.tar.gz", "extracted_text-*.tar.gz", "wiki-*.tar.gz",
    ):
        for path in sorted(settings.backups_dir.glob(pattern))[:-args.keep]:
            path.unlink(missing_ok=True)
    print(
        f"备份完成：db-{stamp}.sqlite3 / uploads-{stamp}.tar.gz /"
        f" extracted_text-{stamp}.tar.gz / wiki-{stamp}.tar.gz，"
        f"各保留最近 {args.keep} 份"
    )


def reindex(args: argparse.Namespace) -> None:
    from .services import search

    init_db()
    conn = connect()
    try:
        counts = search.reindex_all(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"共享检索重建完成：{counts}")


def sync(args: argparse.Namespace) -> None:
    """把本机 Hermes vault 的 outbox 分支重放进线上 wiki（2026-10-30，本地权威）。

    典型用法（在服务器 /opt/open_llm_wiki 下执行，由本机发布脚本通过 ssh 调起）：
        .venv/bin/python -m app.cli sync --branch hermes/mac --json

    `--dry-run` 只比对、不落盘也不推 main；返回体里 failed[] 列出没同步成功的页
    （形状非法 / 引注核对不过 / 并发重试耗尽），本机发布脚本据此决定不 reset。
    """
    from .services import wiki_sync

    init_db()
    try:
        repo.ensure_repo()
    except repo.GitError as exc:
        sys.exit(f"data/wiki 的版本库没建起来：{exc}")
    conn = connect()
    try:
        if args.adopt:
            if not args.branch:
                sys.exit("--adopt 需要 --branch（例如 hermes/mac）")
            report = wiki_sync.adopt(conn, branch=args.branch, remote=args.remote)
        else:
            report = wiki_sync.sync(
                conn, branch=args.branch, remote=args.remote, dry_run=args.dry_run,
            )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_sync_report_text(report))
    if not report.get("ok"):
        sys.exit(1)


def git_pull(args: argparse.Namespace) -> None:
    """服务端：标准 `git pull --rebase`，随后重建 index/SCHEMA 与 FTS。"""
    init_db()
    try:
        repo.ensure_repo()
    except repo.GitError as exc:
        sys.exit(f"data/wiki 的版本库没建起来：{exc}")
    conn = connect()
    try:
        report = git_sync.pull(conn, reason="cli", allow_dirty=args.allow_dirty)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _git_report_text(report))
    if not report.get("ok"):
        sys.exit(1)


def git_push(args: argparse.Namespace) -> None:
    """服务端：标准 `git push`；非快进自动 pull --rebase 重试一次。"""
    init_db()
    try:
        repo.ensure_repo()
    except repo.GitError as exc:
        sys.exit(f"data/wiki 的版本库没建起来：{exc}")
    conn = connect()
    try:
        report = git_sync.push(conn, reason="cli")
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _git_report_text(report))
    if not report.get("ok"):
        sys.exit(1)


def git_status(args: argparse.Namespace) -> None:
    """只读：fetch + ahead/behind/dirty/rebase 状态。"""
    report = git_sync.status()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _git_report_text(report))
    if not report.get("ok"):
        sys.exit(1)


def _git_report_text(report: dict) -> str:
    if report.get("skipped"):
        return f"同步跳过：{report['skipped']}（remote={report.get('remote')}）"
    if report.get("error"):
        extra = report.get("conflicted") or []
        return f"同步失败：{report['error']}" + (
            f"\n冲突文件：{'、'.join(extra)}" if extra else ""
        )
    if "behind" in report:
        return (
            f"远端 {report.get('remote')}/{report.get('branch')}："
            f"落后 {report.get('behind')} / 领先 {report.get('ahead')}；"
            f"工作区{'脏' if report.get('dirty') else '干净'}"
        )
    return f"同步完成：{json.dumps(report, ensure_ascii=False)}"


def _sync_report_text(report: dict) -> str:
    if report.get("error"):
        return f"同步没开始：{report['error']}"
    lines = [
        f"同步{'（dry-run）' if report.get('dry_run') else ''}："
        f"{report.get('branch')} {str(report.get('local_sha') or '')[:8]}",
        f"成功 {report.get('applied', 0)} 页 / 忽略 {report.get('ignored', 0)} 个非页面文件"
        f" / 失败 {len(report.get('failed') or [])} 页",
    ]
    for item in report.get("failed") or []:
        lines.append(f"  失败：{item.get('path')} — {item.get('reason')}")
    if report.get("push_error"):
        lines.append(f"  {report['push_error']}")
    return "\n".join(lines)


def show_plugins(args: argparse.Namespace) -> None:
    """列出 app.toml 启用的插件。"""
    from .plugins import get_plugins

    plugins = get_plugins()
    if args.json:
        print(json.dumps(
            [{"id": p.id, "name": p.name, "nav": p.nav,
              "routers": len(p.routers()), "tools": len(p.tools())}
             for p in plugins],
            ensure_ascii=False, indent=2,
        ))
        return
    if not plugins:
        print("没有启用任何插件（app.toml 的 [plugins] enabled 为空）")
        return
    for plugin in plugins:
        print(f"{plugin.id}\t{plugin.name}\t路由 {len(plugin.routers())} 个\t工具 {len(plugin.tools())} 个")


def show_config(args: argparse.Namespace) -> None:
    """校验 app.toml 并打印解析后的配置（不改任何文件）。"""
    from .wiki_config import get_config

    config = get_config()
    payload = config.as_site_payload()
    payload.update(
        {
            "agent_persona": config.agent_persona,
            "agent_rules": config.agent_rules,
            "ingest_system_prompt": config.ingest_system_prompt,
            "source_path": str(config.source_path) if config.source_path else None,
        }
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.check:
        print("app.toml 校验通过" + (f"：{config.source_path}" if config.source_path else "（使用内置默认值）"))
        return
    print(f"站点名：{config.site_name}")
    print(f"描述：{config.description}")
    print(f"页面类型：{'、'.join(f'{k}={v}' for k, v in config.type_labels.items())}")
    print(f"配置来源：{config.source_path or '内置默认值'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="建管理员账号与 LLM-Wiki vault（SCHEMA/index/log）")
    p_seed.add_argument("--admin-phone")
    p_seed.add_argument("--admin-password")
    p_seed.add_argument("--admin-name")
    p_seed.set_defaults(func=seed)

    p_backup = sub.add_parser(
        "backup", help="备份数据库、上传与解析文本目录、wiki 事实源（含 .git 历史）"
    )
    p_backup.add_argument("--keep", type=int, default=14)
    p_backup.set_defaults(func=backup)

    sub.add_parser("reindex", help="重建共享语料的全文检索").set_defaults(func=reindex)

    p_plugins = sub.add_parser("plugins", help="列出已启用的插件")
    p_plugins.add_argument("--json", action="store_true", help="输出 JSON")
    p_plugins.set_defaults(func=show_plugins)

    p_config = sub.add_parser("config", help="校验并列印 app.toml 解析结果")
    p_config.add_argument("--check", action="store_true", help="只校验，不打印完整配置")
    p_config.add_argument("--json", action="store_true", help="输出 JSON（脚本用）")
    p_config.set_defaults(func=show_config)

    p_git_pull = sub.add_parser(
        "git-pull", help="标准 git pull --rebase（服务端自动同步用；重建 index/FTS）"
    )
    p_git_pull.add_argument("--allow-dirty", action="store_true", help="工作区脏也尝试 pull")
    p_git_pull.add_argument("--json", action="store_true", help="输出 JSON（脚本用）")
    p_git_pull.set_defaults(func=git_pull)

    p_git_push = sub.add_parser("git-push", help="标准 git push（非快进自动 rebase 重试）")
    p_git_push.add_argument("--json", action="store_true", help="输出 JSON（脚本用）")
    p_git_push.set_defaults(func=git_push)

    p_git_status = sub.add_parser("git-status", help="只读：fetch + ahead/behind/dirty")
    p_git_status.add_argument("--json", action="store_true", help="输出 JSON（脚本用）")
    p_git_status.set_defaults(func=git_status)

    p_sync = sub.add_parser(
        "sync", help="【兼容】旧的 outbox 逐页重放（新部署请用 git-pull/git-push）"
    )
    p_sync.add_argument("--branch", help="outbox 分支，例如 hermes/mac；不给则要求远端恰好一个")
    p_sync.add_argument("--remote", default="origin", help="git remote 名（默认 origin）")
    p_sync.add_argument("--dry-run", action="store_true", help="只比对/报告，不写页也不推 main")
    p_sync.add_argument(
        "--adopt", action="store_true",
        help="接入一个已对齐的 clone（只写状态、不重放页面；需要 --branch）",
    )
    p_sync.add_argument("--json", action="store_true", help="输出 JSON（发布脚本解析用）")
    p_sync.set_defaults(func=sync)

    p_admin = sub.add_parser("admin", help="把手机号设为管理员（未注册则建号，可发一次性设密链接）")
    p_admin.add_argument("--phone", required=True)
    p_admin.add_argument("--name")
    p_admin.add_argument("--password")
    p_admin.add_argument("--base-url", help="生成一次性设密链接时用的站点地址")
    p_admin.add_argument("--demote", action="store_true", help="降为普通成员（默认是升管理员）")
    p_admin.set_defaults(func=set_admin)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
