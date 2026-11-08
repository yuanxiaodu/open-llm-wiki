#!/usr/bin/env bash
# scripts/lib/env.sh —— 仓库根 .env 的安全读取器 + 部署/同步脚本共用小工具
#
# 为什么不用 `source .env` / `eval`：.env 是数据，不是代码。这里的解析只做
# "KEY=VALUE → 环境变量"，绝不执行值里的 $(...) / 反引号——有人往 .env 里塞命令也不会跑。
#
# 用法（放在脚本开头、设好 REPO 之后）：
#   REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   . "$REPO/scripts/lib/env.sh"
#
# 行为约定：
#   * 读仓库根 .env（测试可用 WIKI_ENV_FILE 指到别的文件）；没有 .env 就静默返回，
#     一切交给脚本自己的默认值。
#   * 只认 KEY=VALUE 行；空行与 # 注释跳过；值两端成对的 '...' / "..." 去掉；
#     值开头的 ~/ 与 $HOME / ${HOME} 会展开。注意应用侧 app/config.py 不展开 $HOME，
#     所以「应用要读的变量」别写成 $HOME/...，只有 WIKI_* 这类脚本变量可以。
#   * 已存在的同名环境变量优先（哪怕它是空串）——命令行临时覆盖 .env 照样生效：
#       WIKI_SERVER_IP=1.2.3.4 scripts/sync_vault_wiki.sh
#     这跟 app/config.py 的 os.environ.setdefault 是同一个口径。
#   * 不剥行内注释：.env 里不要在值后面写说明，写在同一行会被当成值的一部分。
#
# 提供：
#   require_env NAME...  缺必填变量时中文报错并退出 1（一次报出缺的全部名字）
#   redact VALUE         日志里安全打印敏感值（只报“是否设置/几位”，不回显内容）
#   wiki_expand_path P 展开 P 里的 ~/ 与 $HOME
#   wiki_shquote V     把 V 变成单引号包裹的 shell 字面量（拼远端脚本时用）
#   wiki_ssh_host      打印 ssh 目标：WIKI_SSH_HOST > WIKI_HOST > user@WIKI_SERVER_IP
#   wiki_ssh_args     打印额外 ssh 参数，一行一个（-p 端口 / -i 私钥），没有就不输出
#   wiki_rsync_ssh     打印给 `rsync -e` 用的 ssh 命令串
#
# 兼容 macOS 自带的 bash 3.2：不用 nameref、不用 mapfile。

# 本文件位置 → 仓库根。被复制到别处（比如测试用的临时目录）时按复制后的位置算。
WIKI_LIB_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIKI_REPO_ROOT="$(cd "$WIKI_LIB_ENV_DIR/../.." && pwd)"

# ---- 内部工具 ---------------------------------------------------------------

# 去首尾空白（只用 bash 内置 pattern，不 fork 外部命令）
_wiki_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# 去掉两端成对的单/双引号（不成对就原样保留）
_wiki_unquote() {
  local v="$1"
  local n="${#v}"
  if [ "$n" -ge 2 ]; then
    case "${v:0:1}" in
      "'")
        case "${v:$((n - 1)):1}" in
          "'") printf '%s' "${v:1:$((n - 2))}" ; return 0 ;;
        esac
        ;;
      '"')
        case "${v:$((n - 1)):1}" in
          '"') printf '%s' "${v:1:$((n - 2))}" ; return 0 ;;
        esac
        ;;
    esac
  fi
  printf '%s' "$v"
}

# 展开开头的 ~ / ~/ 与任意位置的 $HOME、${HOME}（只这两个；别的 $ 原样保留）
_wiki_expand_home() {
  local v="$1"
  if [ "$v" = "~" ]; then
    v="$HOME"
  elif [ "${v:0:2}" = "~/" ]; then
    v="$HOME/${v:2}"
  fi
  v="${v//\$\{HOME\}/$HOME}"
  v="${v//\$HOME/$HOME}"
  printf '%s' "$v"
}

# 读一份 .env 到当前 shell（已存在的同名变量跳过）
_wiki_load_env() {
  local file="$1"
  local line key value

  if [ ! -f "$file" ]; then
    return 0
  fi

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}" # 兼容 CRLF 的 .env（Windows 上编辑过）
    line="$(_wiki_trim "$line")"
    [ -n "$line" ] || continue
    case "$line" in
      \#*) continue ;;
    esac
    # 允许 `export KEY=VALUE` 写法；没有 = 的行当注释忽略
    case "$line" in
      export[[:space:]]*)
        line="${line#export}"
        line="$(_wiki_trim "$line")"
        ;;
    esac
    case "$line" in
      *=*) : ;;
      *) continue ;;
    esac

    key="$(_wiki_trim "${line%%=*}")"
    value="$(_wiki_trim "${line#*=}")"

    # 键名必须像 shell 变量：防脏名字，也防把 URL 之类误当键
    case "$key" in
      '' | [0-9]* | *[!A-Za-z0-9_]*) continue ;;
    esac

    # 已存在（含空串）优先：命令行/父进程覆盖 .env
    if declare -p "$key" >/dev/null 2>&1; then
      continue
    fi

    value="$(_wiki_unquote "$value")"
    value="$(_wiki_expand_home "$value")"
    # 值里不可能有 `=` 之外的危险（不 eval）；export 一定要 quoted，否则会二次展开。
    export "$key=$value"
  done < "$file"

  return 0
}

# ---- 加载 .env --------------------------------------------------------------

_wiki_load_env "${WIKI_ENV_FILE:-$WIKI_REPO_ROOT/.env}"

# ---- 公共小工具 -------------------------------------------------------------

# 把值变成单引号包裹的 shell 字面量：值里有 ' 时按 '\'' 转义。
# 用在「把本机变量拼进远端脚本」的地方，避免引号/空格/中文被二次解释。
wiki_shquote() {
  local v="${1:-}"
  local sq="'"
  v="${v//$sq/$sq\\$sq$sq}"
  printf '%s%s%s' "$sq" "$v" "$sq"
}

# 展开路径里的 ~/ 与 $HOME（命令行传进来的值不会自动展开）
wiki_expand_path() {
  _wiki_expand_home "${1:-}"
}

# 缺必填项：一次性列出所有缺失名字，再退出 1（别让用户跑一步错一步）
require_env() {
  local name value missing=""
  for name in "$@"; do
    value=""
    if [ -n "${!name+x}" ]; then
      value="${!name}"
    fi
    if [ -z "$value" ]; then
      missing="$missing $name"
    fi
  done
  if [ -n "$missing" ]; then
    printf '缺必填配置：%s\n' "${missing# }" >&2
    printf '在仓库根 .env 里补上（模板见 .env.example）；没有 .env 就先：cp .env.example .env && chmod 600 .env\n' >&2
    exit 1
  fi
}

# 日志里打印敏感值：只报状态，绝不回显内容
redact() {
  local v="${1:-}"
  if [ -z "$v" ]; then
    printf '（未设置）'
  else
    printf '（已设置，%s 位，不回显）' "${#v}"
  fi
}

# ssh 目标：WIKI_SSH_HOST（~/.ssh/config 别名，优先）> WIKI_HOST（原 PMWIKI_HOST，同样按别名）
# > WIKI_SSH_USER@WIKI_SERVER_IP。都没有返回 1，由调用方决定怎么报错/回落。
wiki_ssh_host() {
  if [ -n "${WIKI_SSH_HOST:-}" ]; then
    printf '%s' "$WIKI_SSH_HOST"
    return 0
  fi
  if [ -n "${WIKI_HOST:-}" ]; then
    printf '%s' "$WIKI_HOST"
    return 0
  fi
  if [ -n "${WIKI_SERVER_IP:-}" ]; then
    if [ -n "${WIKI_SSH_USER:-}" ]; then
      printf '%s@%s' "$WIKI_SSH_USER" "$WIKI_SERVER_IP"
    else
      printf '%s' "$WIKI_SERVER_IP"
    fi
    return 0
  fi
  return 1
}

# 额外 ssh 参数：一行一个。-p 只在显式配了非 22 端口时带上（别名自己的 Port 优先，
# 别拿 22 去覆盖 ~/.ssh/config 里配的端口）；-i 配了私钥才带。
wiki_ssh_args() {
  if [ -n "${WIKI_SSH_PORT:-}" ] && [ "${WIKI_SSH_PORT}" != "22" ]; then
    printf '%s\n' -p "$WIKI_SSH_PORT"
  fi
  if [ -n "${WIKI_SSH_KEY:-}" ]; then
    printf '%s\n' -i "$(_wiki_expand_home "$WIKI_SSH_KEY")"
  fi
  return 0
}

# 给 `rsync -e` 用：ssh 命令串，需要时才加引号（私钥路径含空格也不会散）
wiki_rsync_ssh() {
  local cmd="ssh" opt
  while IFS= read -r opt; do
    [ -n "$opt" ] || continue
    case "$opt" in
      *[!A-Za-z0-9_./:=@%+-]*) cmd="$cmd $(wiki_shquote "$opt")" ;;
      *) cmd="$cmd $opt" ;;
    esac
  done < <(wiki_ssh_args)
  printf '%s' "$cmd"
}
