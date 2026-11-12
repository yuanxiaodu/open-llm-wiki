#!/usr/bin/env bash
# 本地 wiki vault → 共享 wiki 仓库（标准 git）。
#
# 服务端在 agent 回合开始/结束时自动 pull/push；本地不跑后台 daemon，由你在
# Hermes 里说“同步 wiki / 发布 / 对齐线上”，agent 调本脚本。流程：
#   commit → git pull --rebase → git push → 重建本地 index（生成物，不提交）
# 冲突时 abort rebase，退出码 2；绝不 force push。
#
# 用法：
#   scripts/sync-local.sh                  # 默认提交说明
#   scripts/sync-local.sh -m "本次说明"    # 语义化提交说明
#   scripts/sync-local.sh --status         # 只 fetch + 显示 ahead/behind
#   scripts/sync-local.sh --dry-run        # 只显示将做什么
#
# 配置（环境变量优先，其次仓库根 .env）：
#   WIKI_LOCAL_VAULT  本地 vault（默认 ~/wiki）
#   WIKI_REMOTE       remote 名（默认 origin）
#   WIKI_BRANCH       分支（默认 main）
#   WIKI_LOCAL_PYTHON 重建 index 用的 python（默认本仓 .venv/bin/python）

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/env.sh
. "$REPO/scripts/lib/env.sh"

VAULT="${WIKI_LOCAL_VAULT:-$HOME/wiki}"
REMOTE="${WIKI_REMOTE:-origin}"
BRANCH="${WIKI_BRANCH:-main}"
PY="${WIKI_LOCAL_PYTHON:-$REPO/.venv/bin/python}"
MESSAGE=""
STATUS=0
DRY=0

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "同步中止：$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    -m|--message) [ "$#" -ge 2 ] || die "$1 需要一个提交说明"; MESSAGE="$2"; shift 2 ;;
    --status) STATUS=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（-m / --status / --dry-run / --help）" ;;
  esac
done

[ -d "$VAULT" ] || die "本地 vault 不存在：$VAULT"
git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1 || die "不是 git 仓库：$VAULT"
git -C "$VAULT" remote get-url "$REMOTE" >/dev/null 2>&1 || die "没有 remote：$REMOTE"

LOCKDIR="$VAULT/.git/openllmwiki-sync.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  die "另一个同步正在进行（锁：${LOCKDIR}）"
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

for line in index.md SCHEMA.md; do
  grep -qxF "$line" "$VAULT/.gitignore" 2>/dev/null || echo "$line" >> "$VAULT/.gitignore"
done
for line in "log.md merge=union" "log-*.md merge=union"; do
  grep -qxF "$line" "$VAULT/.gitattributes" 2>/dev/null || echo "$line" >> "$VAULT/.gitattributes"
done

# 旧 vault 里生成物可能还被跟踪：从索引移除，文件留在磁盘。
for f in index.md SCHEMA.md; do
  if git -C "$VAULT" ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    git -C "$VAULT" rm --cached -q -- "$f"
  fi
done

git -C "$VAULT" fetch "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"

if [ "$STATUS" = "1" ]; then
  counts="$(git -C "$VAULT" rev-list --left-right --count "$REMOTE/$BRANCH"...HEAD)"
  echo "本地 $VAULT"
  echo "远端 ${REMOTE}/${BRANCH}：落后 $(echo "$counts" | awk '{print $1}') / 领先 $(echo "$counts" | awk '{print $2}')"
  git -C "$VAULT" status --short
  exit 0
fi

if [ "$DRY" = "1" ]; then
  echo "dry-run：将提交本地改动、pull --rebase、push 到 ${REMOTE}/${BRANCH}。"
  git -C "$VAULT" status --short
  exit 0
fi

git -C "$VAULT" add -A
if ! git -C "$VAULT" diff --cached --quiet; then
  [ -n "$MESSAGE" ] || MESSAGE="wiki: sync $(date '+%Y-%m-%d %H:%M:%S')"
  git -C "$VAULT" commit -q -m "$MESSAGE"
  echo "== 已提交：$MESSAGE"
else
  echo "== 本地没有新改动"
fi

if ! git -C "$VAULT" pull --rebase --no-stat "$REMOTE" "$BRANCH"; then
  echo "同步冲突：rebase 已中止。需要处理的文件：" >&2
  git -C "$VAULT" diff --name-only --diff-filter=U >&2 || true
  git -C "$VAULT" rebase --abort >/dev/null 2>&1 || true
  exit 2
fi

if ! git -C "$VAULT" push "$REMOTE" "HEAD:refs/heads/$BRANCH"; then
  echo "== push 被拒绝，pull --rebase 后重试" >&2
  if ! git -C "$VAULT" pull --rebase --no-stat "$REMOTE" "$BRANCH"; then
    echo "同步冲突：rebase 已中止。冲突文件：" >&2
    git -C "$VAULT" diff --name-only --diff-filter=U >&2 || true
    git -C "$VAULT" rebase --abort >/dev/null 2>&1 || true
    exit 2
  fi
  git -C "$VAULT" push "$REMOTE" "HEAD:refs/heads/$BRANCH" || exit 1
fi

if [ -x "$PY" ]; then
  ( cd "$REPO" && WIKI_DIR="$VAULT" WIKI_CONFIG="${WIKI_CONFIG:-wiki.toml}" \
      "$PY" -c 'from app.wiki import store; store.write_schema(); store.rebuild_index()' ) \
    >/dev/null 2>&1 || echo "提示：本地 index 重建失败（不影响已推送的页面）" >&2
fi

echo "完成：$(git -C "$VAULT" rev-parse --short HEAD) → ${REMOTE}/${BRANCH}"
