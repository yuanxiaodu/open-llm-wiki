#!/usr/bin/env bash
# deploy/deploy.sh —— 一条命令把本仓库部署到服务器；新 wiki 复用本仓库时只改 .env。
#
# 用法：
#   deploy/deploy.sh --dry-run                 # 只打印将执行的命令序列，绝不连服务器/不构建/不装依赖
#   deploy/deploy.sh                           # 常规发布（构建前端 → rsync → 服务器 .env → venv/pip → /healthz）
#   deploy/deploy.sh --bootstrap --systemd     # 新 wiki 首次部署：多跑 seed、装 systemd 单元并重启
#   deploy/deploy.sh --skip-build --systemd    # 只发后端：跳过本机构建；带了 --systemd 会重启
#   deploy/deploy.sh --help
#
# 参数：
#   --dry-run       只打印命令序列，绝不 ssh/rsync/pip/构建
#   --bootstrap     首次部署（等价于 --seed：建管理员账号与 wiki 仓库）
#   --skip-build    跳过本机构建 web/dist（服务器上没有 node，前端产物只能本机出）
#   --skip-install  跳过服务器上的 venv/pip install
#   --systemd       装/更新 systemd 单元（deploy/openllmwiki.service）并重启 openllmwiki
#   --seed          跑 `python -m app.cli seed`（默认只在 --bootstrap 时跑）
#   --help          显示本帮助
#
# 配置全部来自仓库根 .env（模板 .env.example）；命令行环境变量优先。
# 涉及：SITE_NAME / PUBLIC_BASE_URL / DATA_DIR / BIND_HOST / PORT / COOKIE_SECURE /
# ADMIN_NAME / ADMIN_PHONE / ADMIN_PASSWORD / LLM_API_KEY / LLM_BASE_URL /
# LLM_MODEL / LLM_THINKING / WIKI_SERVER_IP / WIKI_SSH_HOST / WIKI_SSH_PORT /
# WIKI_SSH_USER / WIKI_SSH_KEY / WIKI_SERVER_DIR / WIKI_SERVER_DOMAIN。
#
# 本脚本全程走 ssh 密钥；请先把自己的公钥装到服务器（ssh-copy-id 或云厂商控制台）。
# LLM key 只在 dry-run 里以“已设置”占位显示。

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib/env.sh
. "$REPO/scripts/lib/env.sh"

usage() {
  sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

DRY=0
BOOTSTRAP=0
SKIP_BUILD=0
SKIP_INSTALL=0
WITH_SYSTEMD=0
DO_SEED=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --bootstrap) BOOTSTRAP=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --systemd) WITH_SYSTEMD=1 ;;
    --seed) DO_SEED=1 ;;
    -h|--help)
      usage
      exit 0 ;;
    *)
      echo "未知参数：$arg" >&2
      echo >&2
      usage >&2
      exit 2 ;;
  esac
done
if [ "$BOOTSTRAP" = "1" ]; then
  DO_SEED=1
fi

say()  { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '部署中止：%s\n' "$*" >&2; exit 1; }
fail() { local rc="$1"; shift; printf '部署中止：%s\n' "$*" >&2; exit "$rc"; }

# 把命令按“能直接复制粘贴”的样子打出来（带空格/中文的参数加引号，普通 token 不加）
_show_cmd() {
  local a
  for a in "$@"; do
    case "$a" in
      '') printf " ''" ;;
      *[!A-Za-z0-9_./:=@%+-]*) printf ' %s' "$(wiki_shquote "$a")" ;;
      *) printf ' %s' "$a" ;;
    esac
  done
}

# 本机命令：dry-run 只打印，不执行
run() {
  if [ "$DRY" = "1" ]; then
    printf '    [dry-run]'
    _show_cmd "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

# ---- 0) 校验配置 -------------------------------------------------------------
say "校验配置（仓库根 .env；模板 .env.example）"
require_env SITE_NAME PUBLIC_BASE_URL ADMIN_NAME
if [ "$DO_SEED" = "1" ]; then
  require_env ADMIN_PHONE ADMIN_PASSWORD
fi

SERVER_DIR="${WIKI_SERVER_DIR:-}"
[ -n "$SERVER_DIR" ] || die "缺 WIKI_SERVER_DIR（服务器项目目录，例如 /opt/open_llm_wiki）：写进 .env"

SSH_TARGET="$(wiki_ssh_host || true)"
[ -n "$SSH_TARGET" ] || die "没有服务器地址：在 .env 里填 WIKI_SSH_HOST（~/.ssh/config 别名），或 WIKI_SERVER_IP + WIKI_SSH_USER"

LLM_KEY="${LLM_API_KEY:-}"
[ -n "$LLM_KEY" ] || die "缺 LLM API key：在 .env 里填 LLM_API_KEY（本地与服务器共用同一个 key）"

# 可选值给默认（与 app/config.py 的默认一致）
DATA_DIR="${DATA_DIR:-./data}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"
PORT="${PORT:-8100}"
COOKIE_SECURE="${COOKIE_SECURE:-0}"
LLM_BASE_URL="${LLM_BASE_URL:-https://api.deepseek.com}"
LLM_MODEL="${LLM_MODEL:-deepseek-flash}"
LLM_THINKING="${LLM_THINKING:-disabled}"

# 服务器 .env 的对外地址/安全开关：本机 .env 的 PUBLIC_BASE_URL/COOKIE_SECURE 是本地开发值
# （localhost:8000 / 0），直接抄到服务器会让邀请链接和 cookie 全错。填了 WIKI_SERVER_DOMAIN
# 就按 https://域名 + 安全 cookie 部署；没填域名（IP:端口 过渡期）就沿用本机 .env 的值。
if [ -n "${WIKI_SERVER_DOMAIN:-}" ]; then
  SERVER_PUBLIC_BASE_URL="https://${WIKI_SERVER_DOMAIN}"
  SERVER_COOKIE_SECURE="1"
else
  SERVER_PUBLIC_BASE_URL="$PUBLIC_BASE_URL"
  SERVER_COOKIE_SECURE="$COOKIE_SECURE"
fi

SSH_OPTS=()
while IFS= read -r _opt; do
  if [ -n "$_opt" ]; then
    SSH_OPTS+=("$_opt")
  fi
done < <(wiki_ssh_args)

say "部署目标"
note "ssh：$SSH_TARGET"
note "目录：$SERVER_DIR"
note "站点：${SITE_NAME}（本机 PUBLIC_BASE_URL=${PUBLIC_BASE_URL}）"
note "服务器站点地址：${SERVER_PUBLIC_BASE_URL}（COOKIE_SECURE=${SERVER_COOKIE_SECURE}）"
note "LLM key：$(redact "$LLM_KEY")"

# ---- 1) ssh 免密连通性 -------------------------------------------------------
say "检查 ssh 免密登录"
if [ "$DRY" = "1" ]; then
  printf '    [dry-run]'
  _show_cmd ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -o BatchMode=yes -o ConnectTimeout=12 "$SSH_TARGET" 'echo WIKI_SSH_OK'
  printf '\n'
else
  if [ -n "${WIKI_SSH_KEY:-}" ]; then
    KEY_PATH="$(wiki_expand_path "$WIKI_SSH_KEY")"
    [ -f "$KEY_PATH" ] || die "找不到私钥：${KEY_PATH}（.env 的 WIKI_SSH_KEY）"
  fi
  if ! ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -o BatchMode=yes -o ConnectTimeout=12 "$SSH_TARGET" 'echo WIKI_SSH_OK' >/dev/null 2>&1; then
    die "ssh 免密登录不通（${SSH_TARGET}）：先把公钥装到服务器；或检查 ~/.ssh/config 与 .env 里的 WIKI_SSH_* 配置"
  fi
  note "ssh 免密 OK"
fi

# ---- 远端执行小工具 ----------------------------------------------------------
# 单条命令：dry-run 只打印；密码/key 绝不拼进命令行。
remote_cmd() {
  local cmd="$1"
  if [ "$DRY" = "1" ]; then
    printf '    [dry-run]'
    _show_cmd ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$SSH_TARGET" "$cmd"
    printf '\n'
    return 0
  fi
  ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$SSH_TARGET" "$cmd"
}

# 多行脚本：通过 stdin 送给远端 `bash -s`（脚本内容不进命令行参数，能在 ps 里被看到）
remote_script() {
  local script="$1"
  if [ "$DRY" = "1" ]; then
    printf '    [dry-run]'
    _show_cmd ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$SSH_TARGET" 'bash -s'
    printf " <<'REMOTE'\n"
    printf '%s\n' "$script" | sed 's/^/        /'
    printf '    [dry-run] REMOTE\n'
    return 0
  fi
  printf '%s\n' "$script" | ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$SSH_TARGET" 'bash -s'
}

# ---- 2) 本机构建前端 ---------------------------------------------------------
if [ "$SKIP_BUILD" = "1" ]; then
  say "跳过本机构建（--skip-build）"
else
  say "构建前端 web/dist（服务器上没 node，前端产物只能本机出）"
  # 构建命令自动探测：WIKI_PNPM 显式指定优先；否则 pnpm；再否则 corepack pnpm
  # （本机 pnpm 不在 PATH、只有 corepack，docs/DEPLOYMENT.md 里也是 corepack pnpm）；都没有才退出。
  # 变量允许带参数（如 "node /path/pnpm.mjs"、"corepack pnpm"），所以真正执行时不加引号。
  if [ -n "${WIKI_PNPM:-}" ]; then
    PNPM_CMD="$WIKI_PNPM"
  elif command -v pnpm >/dev/null 2>&1; then
    PNPM_CMD="pnpm"
  elif command -v corepack >/dev/null 2>&1; then
    PNPM_CMD="corepack pnpm"
  else
    die "本机没有 pnpm / corepack：装 Node 22（自带 corepack），或用 WIKI_PNPM 指定 pnpm 路径；也可以加 --skip-build 跳过构建（前提是 web/dist 已是最新）"
  fi
  note "构建命令：$PNPM_CMD install && $PNPM_CMD build（在 web/ 下执行）"
  if [ "$DRY" = "1" ]; then
    printf '    [dry-run] (cd'
    _show_cmd "$REPO/web"
    printf ' && %s install && %s build)\n' "$PNPM_CMD" "$PNPM_CMD"
  else
    command -v node >/dev/null 2>&1 || die "本机没有 node：装 Node 22（或用 --skip-build，前提是 web/dist 已是最新）"
    # shellcheck disable=SC2086  # PNPM_CMD 允许是 "node /path/pnpm.mjs" / "corepack pnpm" 这种带参数的命令
    ( cd "$REPO/web" && $PNPM_CMD install && $PNPM_CMD build ) || fail $? "前端构建失败：去 web/ 手工跑 $PNPM_CMD install && $PNPM_CMD build 看报错（或加 --skip-build）"
    [ -d "$REPO/web/dist" ] || die "构建结束但 web/dist 不存在，检查 web/vite.config.ts 的产物目录"
    note "web/dist 已就绪"
  fi
fi

# ---- 3) rsync 代码 ----------------------------------------------------------
say "同步代码到 $SSH_TARGET:$SERVER_DIR"
RSYNC_SSH="$(wiki_rsync_ssh)"
run rsync -av \
  --exclude .venv \
  --exclude data \
  --exclude .env \
  --exclude __pycache__ \
  --exclude web/node_modules \
  -e "$RSYNC_SSH" \
  "$REPO/" "$SSH_TARGET:$SERVER_DIR/"

if [ ! -d "$REPO/web/dist" ]; then
  die "web/dist 不存在：先构建（去掉 --skip-build），否则服务器页面只能回一句 JSON 提示"
fi
run rsync -av --delete -e "$RSYNC_SSH" "$REPO/web/dist/" "$SSH_TARGET:$SERVER_DIR/web/dist/"

# ---- 4) 服务器 .env ----------------------------------------------------------
# 远端脚本体一律用 `read -d ''` 赋值，不要写成 "$(cat <<EOF ... )"：macOS 自带的
# bash 3.2 解析器会数错命令替换里的右括号，heredoc 体里一旦出现 `*)` 这种 case 模式，
# 脚本体会被当成命令执行。read 一直读到 EOF，效果一样但不经过命令替换。
#
# 受管的键每次部署按本机 .env 重写；SESSION_SECRET 没有时才现场生成；
# 其余键（SENTRY_DSN/LOG_LEVEL 等）保留服务器上的值。值用单引号字面量拼进去，
# 中文/空格/引号都不会被二次解释。dry-run 打印时把 key/密码换成“已设置”占位。
REMOTE_ENV_TEMPLATE=""
IFS= read -r -d '' REMOTE_ENV_TEMPLATE <<'REMOTE_SCRIPT' || true
set -euo pipefail
cd __WIKI_SERVER_DIR__
umask 077

# 旧 .env 先备份（重跑部署留个后悔药），SESSION_SECRET 沿用旧值——
# 重跑一次部署把所有人踢下线没有道理。
if [ -f .env ]; then
  cp -p .env ".env.bak-$(date +%Y%m%d-%H%M%S)"
fi

read_old() {
  local k="$1" line
  [ -f .env ] || return 0
  while IFS= read -r line; do
    case "$line" in
      "$k="*) printf '%s' "${line#*=}"; return 0 ;;
    esac
  done < .env
  return 0
}

SESSION_SECRET="$(read_old SESSION_SECRET)"
if [ -z "$SESSION_SECRET" ]; then
  PYGEN="$(command -v python3.11 || command -v python3 || true)"
  if [ -z "$PYGEN" ]; then
    echo "服务器上找不到 python3.11 / python3，没法生成 SESSION_SECRET：先按 docs/DEPLOYMENT.md §1 装运行环境" >&2
    exit 1
  fi
  SESSION_SECRET="$("$PYGEN" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  echo "SESSION_SECRET：服务器上现场生成（只写服务器 .env，不回显）"
else
  echo "SESSION_SECRET：沿用服务器上已有的值"
fi

TMP=".env.new.$$"
{
  printf '%s\n' "# 由 deploy/deploy.sh 生成：受管键重跑会覆盖；服务器手工加的键（SENTRY_DSN/LOG_LEVEL 等）会保留"
  printf 'SESSION_SECRET=%s\n' "$SESSION_SECRET"
  printf 'SITE_NAME=%s\n' __WIKI_SITE_NAME__
  printf 'PUBLIC_BASE_URL=%s\n' __WIKI_PUBLIC_BASE_URL__
  printf 'DATA_DIR=%s\n' __WIKI_DATA_DIR__
  printf 'BIND_HOST=%s\n' __WIKI_BIND_HOST__
  printf 'PORT=%s\n' __WIKI_PORT__
  printf 'COOKIE_SECURE=%s\n' __WIKI_COOKIE_SECURE__
  printf 'ADMIN_NAME=%s\n' __WIKI_ADMIN_NAME__
  printf 'ADMIN_PHONE=%s\n' __WIKI_ADMIN_PHONE__
  printf 'ADMIN_PASSWORD=%s\n' __WIKI_ADMIN_PASSWORD__
  printf 'LLM_API_KEY=%s\n' __WIKI_LLM_KEY__
  printf 'LLM_BASE_URL=%s\n' __WIKI_LLM_BASE_URL__
  printf 'LLM_MODEL=%s\n' __WIKI_LLM_MODEL__
  printf 'LLM_THINKING=%s\n' __WIKI_LLM_THINKING__
} > "$TMP"

for key in SENTRY_DSN ENVIRONMENT LOG_LEVEL RELEASE WIKI_READONLY; do
  val="$(read_old "$key")"
  if [ -n "$val" ]; then
    printf '%s=%s\n' "$key" "$val" >> "$TMP"
  fi
done

chmod 600 "$TMP"
mv "$TMP" .env
chmod 600 .env
echo "服务器 .env 已写入：$(pwd)/.env（chmod 600）"
REMOTE_SCRIPT

render_remote_env_script() {
  local mask="${1:-0}"
  local script key_disp pass_disp
  script="$REMOTE_ENV_TEMPLATE"
  script="${script//__WIKI_SERVER_DIR__/$(wiki_shquote "$SERVER_DIR")}"
  script="${script//__WIKI_SITE_NAME__/$(wiki_shquote "$SITE_NAME")}"
  script="${script//__WIKI_PUBLIC_BASE_URL__/$(wiki_shquote "$SERVER_PUBLIC_BASE_URL")}"
  script="${script//__WIKI_DATA_DIR__/$(wiki_shquote "$DATA_DIR")}"
  script="${script//__WIKI_BIND_HOST__/$(wiki_shquote "$BIND_HOST")}"
  script="${script//__WIKI_PORT__/$(wiki_shquote "$PORT")}"
  script="${script//__WIKI_COOKIE_SECURE__/$(wiki_shquote "$SERVER_COOKIE_SECURE")}"
  script="${script//__WIKI_ADMIN_NAME__/$(wiki_shquote "$ADMIN_NAME")}"
  script="${script//__WIKI_ADMIN_PHONE__/$(wiki_shquote "${ADMIN_PHONE:-}")}"
  script="${script//__WIKI_LLM_BASE_URL__/$(wiki_shquote "$LLM_BASE_URL")}"
  script="${script//__WIKI_LLM_MODEL__/$(wiki_shquote "$LLM_MODEL")}"
  script="${script//__WIKI_LLM_THINKING__/$(wiki_shquote "$LLM_THINKING")}"
  if [ "$mask" = "1" ]; then
    key_disp="$(wiki_shquote '<已设置，不回显>')"
    pass_disp="$(wiki_shquote '<已设置，不回显>')"
  else
    key_disp="$(wiki_shquote "$LLM_KEY")"
    pass_disp="$(wiki_shquote "${ADMIN_PASSWORD:-}")"
  fi
  script="${script//__WIKI_ADMIN_PASSWORD__/$pass_disp}"
  script="${script//__WIKI_LLM_KEY__/$key_disp}"
  printf '%s\n' "$script"
}

say "生成/更新服务器 .env（SESSION_SECRET 没有才生成；其余受管键按本机 .env 重写）"
remote_script "$(render_remote_env_script "$DRY")"

# ---- 5) 服务器 venv + 依赖 ---------------------------------------------------
if [ "$SKIP_INSTALL" = "1" ]; then
  say "跳过服务器依赖安装（--skip-install）"
else
  say "服务器建 venv 并装依赖（--skip-install 可跳过）"
  REMOTE_INSTALL_TEMPLATE=""
  IFS= read -r -d '' REMOTE_INSTALL_TEMPLATE <<'REMOTE_SCRIPT' || true
set -euo pipefail
cd __WIKI_SERVER_DIR__
PYBIN="$(command -v python3.11 || command -v python3 || true)"
if [ -z "$PYBIN" ]; then
  echo "服务器上找不到 python3.11 / python3：先按 docs/DEPLOYMENT.md §1 装运行环境" >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "建虚拟环境 .venv（$("$PYBIN" -V 2>&1)）"
  "$PYBIN" -m venv .venv
else
  echo "虚拟环境 .venv 已存在，复用"
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "Python 依赖已装好：$(.venv/bin/python -V 2>&1)"
REMOTE_SCRIPT
  install_script="$REMOTE_INSTALL_TEMPLATE"
  install_script="${install_script//__WIKI_SERVER_DIR__/$(wiki_shquote "$SERVER_DIR")}"
  remote_script "$install_script"
fi

# ---- 6) seed -----------------------------------------------------------------
if [ "$DO_SEED" = "1" ]; then
  say "建管理员账号 + 初始化 wiki（app.cli seed）"
  remote_cmd "cd $(wiki_shquote "$SERVER_DIR") && .venv/bin/python -m app.cli seed" \
    || fail $? "seed 失败：管理员手机号/密码看 .env 的 ADMIN_*；上面是服务器上的原始报错"
else
  say "跳过 seed（首次部署加 --bootstrap；想单独跑加 --seed）"
fi

# ---- 7) systemd --------------------------------------------------------------
if [ "$WITH_SYSTEMD" = "1" ]; then
  say "装/更新 systemd 单元并重启 openllmwiki"
  remote_cmd "cd $(wiki_shquote "$SERVER_DIR") && sudo -n cp deploy/openllmwiki.service /etc/systemd/system/openllmwiki.service && sudo -n systemctl daemon-reload && sudo -n systemctl enable openllmwiki && sudo -n systemctl restart openllmwiki" \
    || fail $? "systemd 安装/重启失败：服务器上需要免密 sudo（ubuntu 用户通常已配）；也可按 docs/DEPLOYMENT.md §6 手工执行"
else
  say "跳过 systemd 安装/重启（加 --systemd 才会做；换了 web/dist 必须重启，否则白屏）"
fi

# ---- 8) /healthz 冒烟 --------------------------------------------------------
say "冒烟：远端 /healthz"
REMOTE_HEALTH_TEMPLATE=""
IFS= read -r -d '' REMOTE_HEALTH_TEMPLATE <<'REMOTE_SCRIPT' || true
set -uo pipefail
PORT_GUESS=__WIKI_PORT__
# 实际监听端口以 systemd 单元为准（deploy/openllmwiki.service 模板是 8100；
# .env 的 PORT 只给 smoke 定时器读，两者可能不同）。
if command -v systemctl >/dev/null 2>&1; then
  UNIT_PORT="$(systemctl show openllmwiki -p ExecStart --value 2>/dev/null | grep -oE -- '--port [0-9]+' | head -n 1 | cut -d' ' -f2)"
  if [ -n "$UNIT_PORT" ]; then
    PORT_GUESS="$UNIT_PORT"
  fi
fi
echo "探测：http://127.0.0.1:$PORT_GUESS/healthz"
curl -fsS -m 10 "http://127.0.0.1:$PORT_GUESS/healthz"
echo
REMOTE_SCRIPT
health_script="$REMOTE_HEALTH_TEMPLATE"
health_script="${health_script//__WIKI_PORT__/$(wiki_shquote "$PORT")}"
remote_script "$health_script" || fail $? "健康检查没过：看服务器 journalctl -u openllmwiki -n 50 --no-pager；端口对不上就检查 deploy/openllmwiki.service 的 --port"

# ---- 结束 --------------------------------------------------------------------
if [ "$DRY" = "1" ]; then
  say "dry-run 结束：以上只是预览——没有构建、没有连服务器、没有装依赖、没有重启任何服务"
  exit 0
fi

say "部署完成：$SSH_TARGET:$SERVER_DIR"
if [ "$WITH_SYSTEMD" = "1" ]; then
  note "openllmwiki 已重启"
else
  note "没有 --systemd，应用没重启：换了 web/dist 之后必须重启（/assets 挂载是 import 期判一次的，不重启会白屏）"
fi
if [ -n "${WIKI_SERVER_DOMAIN:-}" ]; then
  note "对外地址：https://${WIKI_SERVER_DOMAIN}（nginx/Caddy 与证书见 docs/DEPLOYMENT.md）"
fi
