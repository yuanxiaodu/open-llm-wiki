#!/usr/bin/env bash
# 全路由巡检。用法：scripts/smoke.sh [BASE_URL]
#
# 三组断言（S3 T6a 起的口径）：
#   1. /api JSON 面：未登录打受保护的 /api/* 应该是 401 JSON，不是 302/500；
#      给了 ADMIN_PHONE / ADMIN_PASSWORD 就登录一遍，验登录后的接口真的是 200；
#      再给 MEMBER_PHONE / MEMBER_PASSWORD 就验 member 打 /api/members 是 403。
#   2. SPA 面：**只在构建产物在的时候**才断言（HAS_DIST=1）。
#      GET / 与 GET /chat 必须是 200 且 content-type 是 text/html（catch-all 回退到
#      index.html）。原来这里断言旧渲染面（/wiki /log /members …）是 404/405——那些路径
#      现在一律被 SPA 回退接住返 200 html，旧断言只会假红（裁定 R-S3-4：这条"必须改"，
#      服务器上每 10 分钟跑一次的 openllmwiki-smoke.timer 也吃它）。
#      光断这两条抓不到**白屏**：入口页上去了、/assets/*.js 没上去（或服务没重启，
#      /assets 挂载是 import 期判一次的），那条 <script src="/assets/index-HASH.js"> 会
#      落进 SPA 回退，拿回 200 + text/html，浏览器把 HTML 当 JS 解析，人只看见一张白页
#      而巡检全绿。所以有产物时再从**真拿到的入口页**里抠出第一条 /assets/*.js，GET 它一次：
#      200 且 content-type 是 javascript 才算通（终审 I4）。
#      没有产物时（HAS_DIST=0，只跑后端的开发机）这两条**不是**"退化成只断 200"：
#      app/main.py 的 root() 对 / 回一句 JSON 提示（200），而 catch-all 对 /chat
#      压根没有入口页可回，是 404。所以 /chat 那一支接受 200 或 404，两种都是对的；
#      真坏掉的样子是 5xx 或 302（T6a 评审的 Important：原来那条会假红）。
#   3. schema 卫生：GET /openapi.json 的 paths 里不许出现 "{full_path"——SPA 回退那条
#      catch-all 是 include_in_schema=False 的（裁定 R-S3-3），冒出来就说明跑的是一版
#      装配错了的代码。与 tests/test_api_wiring.py 的同名自查是一对：那边钉源码，
#      这边钉**真跑着的那个实例**（部署错了版本也能被抓到）。
#      （/assets 的文件名带内容哈希，没有稳定的名字可写死；上面那条探的是从入口页里
#      现抠出来的那一个，所以哈希怎么变都跟得上。）
set -uo pipefail

BASE="${1:-${BASE_URL:-http://localhost:8000}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

# web/dist 在不在，决定 SPA 那两条怎么断言。
# 用本机的工作树判（smoke 就在部署目录里跑：服务器上是 /opt/open_llm_wiki，rsync 连 web/dist
# 一起送上去）。看得到 dist 而实例回的不是 html，说明服务没重启（/assets 挂载是 import
# 期判一次的，DEPLOY.md §2 那行 restart 就是为这个）或构建产物没上去——正是要抓的坏。
#
# 本机没有 dist（只跑后端的开发机）时**不是**退化成"只断 200"：那种实例上 / 是一句
# JSON 提示（app/main.py 的 root()，200），而 /chat 是 404——catch-all 没有入口页可回，
# 老实说没有（main.py:spa_fallback 的那个 raise）。所以每条路径自己带一份"没有产物时
# 可接受的状态码"，见下面的调用点。
if [[ -f "$ROOT/web/dist/index.html" ]]; then
  HAS_DIST=1
else
  HAS_DIST=0
fi

check() {
  local path="$1" want="$2" jar="${3:-}"
  local code
  if [[ -n "$jar" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -b "$jar" "$BASE$path")
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$path")
  fi
  if [[ "$code" == "$want" ]]; then
    printf 'ok    %-30s %s\n' "$path" "$code"
  else
    printf 'FAIL  %-30s 拿到 %s，应为 %s\n' "$path" "$code" "$want"
    fail=1
  fi
}

check_spa() {
  # $1=path  $2=没有构建产物时可接受的状态码（空格分隔，默认只认 200）
  #
  # 有产物（HAS_DIST=1）：200 **且** content-type 是 text/html，两个都要；再从入口页里
  # 抠出第一条 /assets/*.js 探一遍（check_first_asset，白屏就是它抓的）。
  # 没产物（HAS_DIST=0）：只验状态码落在 $2 里，content-type 压根不看（那种实例回的是
  # JSON 提示或 404 信封，都不是 html），资产那条也无从探起（没有入口页）。
  local path="$1" allow="${2:-200}" out code ctype want
  out=$(curl -s -o /dev/null -w '%{http_code}\n%{content_type}' --max-time 15 "$BASE$path")
  code=${out%%$'\n'*}
  ctype=${out#*$'\n'}
  if [[ "$HAS_DIST" == "1" ]]; then
    if [[ "$code" != "200" ]]; then
      printf 'FAIL  %-30s 拿到 %s，应为 200\n' "$path" "$code"
      fail=1
      return
    fi
    if [[ "$ctype" != text/html* ]]; then
      printf 'FAIL  %-30s 200 但 content-type 是 %s，应为 text/html（web/dist 在，该回 index.html；服务重启了吗？）\n' \
        "$path" "${ctype:-（空）}"
      fail=1
      return
    fi
    printf 'ok    %-30s %s %s\n' "$path" "$code" "$ctype"
    check_first_asset
    return
  fi
  for want in $allow; do
    if [[ "$code" == "$want" ]]; then
      printf 'ok    %-30s %s（本机没有 web/dist，接受 %s 之一，跳过 content-type 断言）\n' \
        "$path" "$code" "$allow"
      return
    fi
  done
  printf 'FAIL  %-30s 拿到 %s，本机没有 web/dist，应为 %s 之一\n' "$path" "$code" "$allow"
  fail=1
}

# 白屏那一种坏（终审 I4）：入口页上去了、它指着的 /assets/*.js 没上去（或 rsync 之后忘了
# 重启，/assets 的挂载是 import 期判一次的）。这时候 GET / 与 GET /chat 都是 200 +
# text/html，上面那两条断言全绿，而浏览器把回退来的 HTML 当 JS 解析，人只看见一张白页。
# 所以从**真拿到的入口页**里抠出第一条 /assets/*.js 再 GET 它一次：200 且 content-type 是
# javascript 才算通。名字是现抠的，哈希怎么变都跟得上——这正是原来那句"构建产物的文件名带
# 内容哈希，没有稳定的名字可断言"的解法。
#
# 只跑一次（/ 与 /chat 拿到的是同一个入口页，探两遍只是多两条日志）。
ASSET_CHECKED=0
check_first_asset() {
  [[ "$ASSET_CHECKED" == "1" ]] && return
  ASSET_CHECKED=1
  local html asset out code ctype
  html=$(curl -s --max-time 15 "$BASE/")
  asset=$(printf '%s' "$html" | grep -oE '/assets/[A-Za-z0-9._-]+\.js' | head -1)
  if [[ -z "$asset" ]]; then
    printf 'FAIL  %-30s 入口页里找不到一条 /assets/*.js（送上来的不是构建产物？白屏就是这么来的）\n' \
      "/（入口页的资产）"
    fail=1
    return
  fi
  out=$(curl -s -o /dev/null -w '%{http_code}\n%{content_type}' --max-time 15 "$BASE$asset")
  code=${out%%$'\n'*}
  ctype=${out#*$'\n'}
  if [[ "$code" != "200" ]]; then
    printf 'FAIL  %-30s 拿到 %s，应为 200（入口页指着它；它不在，页面就是白的）\n' "$asset" "$code"
    fail=1
    return
  fi
  case "$ctype" in
    *javascript*)
      printf 'ok    %-30s %s %s\n' "$asset" "$code" "$ctype"
      ;;
    *)
      printf 'FAIL  %-30s 200 但 content-type 是 %s，应为 javascript（十有八九是 SPA 回退把它接住了：/assets 挂载没生效，服务重启了吗？）\n' \
        "$asset" "${ctype:-（空）}"
      fail=1
      ;;
  esac
}

check_schema_hygiene() {
  local body
  body=$(curl -s --max-time 15 "$BASE/openapi.json")
  if [[ -z "$body" ]]; then
    printf 'FAIL  %-30s 拿不到响应体\n' "/openapi.json"
    fail=1
  elif [[ "$body" != *'"paths"'* ]]; then
    printf 'FAIL  %-30s 响应里没有 "paths"（不是 openapi 文档？）\n' "/openapi.json"
    fail=1
  elif [[ "$body" == *'{full_path'* ]]; then
    printf 'FAIL  %-30s schema 里出现了 catch-all（SPA 回退该是 include_in_schema=False）\n' \
      "/openapi.json"
    fail=1
  else
    printf 'ok    %-30s paths 里没有 {full_path}\n' "/openapi.json"
  fi
}

api_login() {
  # $1=jar $2=phone $3=password。/api/login 只收 JSON body（裁定 R11），成功 200。
  local jar="$1" phone="$2" password="$3" code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -c "$jar" \
    -H 'Content-Type: application/json' \
    -d "{\"phone\": \"$phone\", \"password\": \"$password\"}" "$BASE/api/login")
  if [[ "$code" != "200" ]]; then
    printf 'FAIL  %-30s 拿到 %s，应为 200\n' "/api/login" "$code"
    exit 1
  fi
  printf 'ok    %-30s %s\n' "/api/login" "$code"
}

echo "== 巡检 $BASE =="

check /healthz 200
# 有产物：两条都得是 200 + text/html。
# 没产物：/ 是 main.py root() 那句 JSON 提示（200）；/chat 走 catch-all，没有入口页可回
# 就是 404（main.py:spa_fallback 的 raise）——两种都对，5xx/302 才是坏。
check_spa /
check_spa /chat "200 404"
check_schema_hygiene

# 未登录一律 401 JSON
for path in /api/chat/sessions /api/wiki /api/log /api/members "/api/search?q=x"; do
  check "$path" 401
done

if [[ -n "${ADMIN_PHONE:-}" && -n "${ADMIN_PASSWORD:-}" ]]; then
  echo "== 带管理员身份再巡检一遍 =="
  jar=$(mktemp)
  trap 'rm -f "$jar" "${mjar:-}"' EXIT
  api_login "$jar" "$ADMIN_PHONE" "$ADMIN_PASSWORD"

  check /api/chat/sessions 200 "$jar"
  check /api/wiki 200 "$jar"
  check /api/log 200 "$jar"
  check "/api/search?q=x" 200 "$jar"
  check /api/members 200 "$jar"
else
  echo "（没给 ADMIN_PHONE / ADMIN_PASSWORD，跳过带身份的巡检）"
fi

if [[ -n "${MEMBER_PHONE:-}" && -n "${MEMBER_PASSWORD:-}" ]]; then
  echo "== 带参与人身份验一遍：读的一样通，成员管理 403 =="
  mjar=$(mktemp)
  trap 'rm -f "${jar:-}" "$mjar"' EXIT
  api_login "$mjar" "$MEMBER_PHONE" "$MEMBER_PASSWORD"

  check /api/chat/sessions 200 "$mjar"
  check /api/wiki 200 "$mjar"
  check /api/log 200 "$mjar"
  check /api/members 403 "$mjar"
fi

if [[ "$fail" == "0" ]]; then
  echo "== 全绿 =="
else
  echo "== 有失败项 =="
fi
exit "$fail"
