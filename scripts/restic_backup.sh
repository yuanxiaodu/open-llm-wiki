#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Open LLM Wiki 的 restic 备份：把整个 data/ 打成快照（wiki git 仓库、uploads/、
# extracted_text/ 全在里面），再按保留策略 forget + prune。
#
# 为什么需要它（而不是只靠 Litestream）：
#   Litestream 只复制 data/db.sqlite3（SQLite 的 WAL）。data/wiki 是文件 + git 仓，
#   data/uploads 是普通文件——这些**只能由 restic 保**。
#   ADR 风险页原话：wiki 是唯一不可再生资产，Litestream 只保 DB。
#
# 用法：
#   sudo /opt/open_llm_wiki/scripts/restic_backup.sh
#   DRY_RUN=1 /opt/open_llm_wiki/scripts/restic_backup.sh      # 只打印将执行的命令，不碰仓库
#   WIKI_DATA_DIR=/tmp/openllmwiki-data DRY_RUN=1 ...         # 本机演练
#
# 依赖：restic（sudo apt-get install -y restic，或用官方 release 二进制）。
# 凭据/仓库地址从 /etc/openllmwiki/backup.env 读（模板见 deploy/backup.env.example）；
# 已在环境里的变量优先，所以 systemd 的 EnvironmentFile= 也能直接把值喂进来。
# 建议由 systemd timer 每日跑一次（unit 片段见 DEPLOY §17）。
# ---------------------------------------------------------------------------
set -euo pipefail

DATA_DIR="${WIKI_DATA_DIR:-/opt/open_llm_wiki/data}"
ENV_FILE="${WIKI_BACKUP_ENV:-/etc/openllmwiki/backup.env}"
TAG="${RESTIC_TAG:-openllmwiki}"
DRY_RUN="${DRY_RUN:-0}"

# 先把可能来自环境的值兜成空串：下面 load_env_file 用 printenv 判断"是否已设置"，
# shell 变量不算设置；同时这也让 shellcheck 不再报"引用了未赋值的变量"。
RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-}"
RESTIC_EXTRA_EXCLUDES="${RESTIC_EXTRA_EXCLUDES:-}"
BACKUP_HOST="${BACKUP_HOST:-}"

log()  { printf '[restic_backup] %s\n' "$*"; }
warn() { printf '[restic_backup] 警告：%s\n' "$*" >&2; }
die()  { printf '[restic_backup] 错误：%s\n' "$*" >&2; exit 1; }

# 读 systemd 风格的 EnvironmentFile（KEY=VALUE，一行一个，# 注释）。
# 不 source：那个格式不是 shell，值里有空格/特殊字符时 source 会把它执行坏。
# 已 export 的同名变量优先（调用方/CI 覆盖文件）。
load_env_file() {
  local file="$1" line key value
  [ -r "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      '' | '#'*) continue ;;
      *=*) ;;
      *) warn "忽略 $file 里不是 KEY=VALUE 的一行：$line"; continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [ -n "$key" ] || continue
    # 去掉值两侧空白（模版本里有意写成 KEY=VALUE 单行，不处理引号）
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if ! printenv "$key" >/dev/null 2>&1; then
      export "$key=$value"
    fi
  done < "$file"
}

# DRY_RUN=1 时只把命令按 shell 安全形式打印出来。
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] '
    printf '%q ' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

command -v restic >/dev/null 2>&1 || die \
  "找不到 restic。先装：sudo apt-get install -y restic（或 https://restic.net 的 release 二进制）"
[ -d "$DATA_DIR" ] || die "备份源不存在：$DATA_DIR"

if [ -r "$ENV_FILE" ]; then
  load_env_file "$ENV_FILE"
else
  # 变量紧挨着中文标点时一律写成 ${VAR}：macOS 自带的 bash 3.2 会把紧随其后的
  # 多字节字符首字节吃进变量名（$ENV_FILE， -> ENV_FILE<0xef>: unbound variable）。
  warn "读不到 ${ENV_FILE}，只靠已经 export 的环境变量"
fi

[ -n "$RESTIC_REPOSITORY" ] || die "缺 RESTIC_REPOSITORY（在 $ENV_FILE 里给，或 export）"
if [ -n "$RESTIC_PASSWORD_FILE" ]; then
  [ -r "$RESTIC_PASSWORD_FILE" ] || die \
    "RESTIC_PASSWORD_FILE 指向的文件读不到：$RESTIC_PASSWORD_FILE"
elif [ -z "$RESTIC_PASSWORD" ]; then
  die "缺仓库密码：给 RESTIC_PASSWORD_FILE（推荐）或 RESTIC_PASSWORD"
fi
if [ -z "$BACKUP_HOST" ]; then
  BACKUP_HOST="$(hostname -s 2>/dev/null || hostname)"
fi

log "源目录：$DATA_DIR"
log "仓库：$RESTIC_REPOSITORY"
log "host：${BACKUP_HOST}；tag：${TAG}"
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN=1：只打印命令，不执行、不碰仓库"
fi

# 排除 data/backups：那是 app.cli backup 的本地备份（db + 三个 tar.gz）。
# 把一堆本地备份再塞进 restic 会让快照体积滚雪球，恢复时也用不上——restic 快照
# 本身就是异地的那一份。--exclude-caches 顺手跳过 CACHEDIR.TAG 目录。
BACKUP_ARGS=(
  "$DATA_DIR"
  --tag "$TAG"
  --host "$BACKUP_HOST"
  --exclude "$DATA_DIR/backups"
  --exclude-caches
)
if [ -n "$RESTIC_EXTRA_EXCLUDES" ]; then
  read -r -a extra_excludes <<< "$RESTIC_EXTRA_EXCLUDES"
  for path in "${extra_excludes[@]}"; do
    BACKUP_ARGS+=(--exclude "$path")
  done
fi

log "1/3 备份 data/（wiki git 仓库、uploads、extracted_text）…"
run restic backup "${BACKUP_ARGS[@]}"

log "2/3 按保留策略清理旧快照（daily 14 / weekly 8，--prune 回收空间）…"
# --tag / --host 是承重的：仓库里可能还有别的来源的快照；不带的 forget 会把它们
# 一起算进保留策略，等于悄悄删别人的备份。
run restic forget --tag "$TAG" --host "$BACKUP_HOST" \
  --keep-daily 14 --keep-weekly 8 --prune

log "3/3 摘要：仓库里最近 3 份快照"
run restic snapshots --tag "$TAG" --host "$BACKUP_HOST" --latest 3 --compact

# 只读 1% 的包校验（小仓库几秒到几分钟）。不想每次跑就把这一段注释掉，
# 改成每周手工 `restic check`。
log "附加：校验仓库（--read-data-subset=1%）"
run restic check --read-data-subset=1%

log "完成。"
