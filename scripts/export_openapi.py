#!/usr/bin/env python3
"""把 FastAPI 的 OpenAPI schema 导出成 JSON（给 openapi-typescript 生成前端类型用）。

为什么单独一个脚本：前端 `web/src/api-types.gen.ts` 是从这份 JSON 生成的，而 CI 要
能只跑"导出 + 生成 + git diff"就发现前后端契约漂移（ADR 2026-10-24 决策 1）。
直接 `python -c "from app.main import app; ..."` 也能做，但那样每个调用方都得自己
记得先设环境变量（SESSION_SECRET 不设的话 app 还能导入，但没人想在 CI 里踩这个坑）。

三条硬约定：
1. 所有环境变量都在 `import app.*` **之前**设好（app/config.py 在 import 时就读 env）。
   默认值只够生成 schema 用：
   - DATA_DIR 指到 /tmp（不碰仓库的 data/，也不建库）；
   - SESSION_SECRET 给一个明显的假值（不会用于任何真实会话）；
   - WIKI_READONLY=1（导出只需要读路由表，读不读写不写与 schema 无关，但保持与
     线上默认一致，免得有人本地导出后误以为写路由不存在）；
   - SITE_NAME=知识库（强制赋值，不读本机 .env）：它会进 info.title，生成物必须与
     开发机环境无关，否则 CI 的 `git diff --exit-code` 会红。
2. **不触发 lifespan、不建库**：只 `import app.main` 然后调 `app.openapi()`——FastAPI
   的 schema 是纯路由元数据，lifespan 里那些 init_db / ensure_repo / 迁移都不会跑。
3. 输出稳定、可 diff：键排序 + 固定缩进 + 末尾换行。CI 拿 `git diff --exit-code`
   比对的就是这个文件的字节，任何"顺序抖一下"都会变成假红。

用法：
    python scripts/export_openapi.py                    # 写到 stdout
    python scripts/export_openapi.py --out web/openapi.json
    cd web && pnpm gen:api                              # 由 JSON 生成 src/api-types.gen.ts

注意：改了后端路由/模型之后，先重跑本脚本再重跑 `pnpm gen:api`，两者都要提交；
CI 的 api-types job 会重跑一遍并检查 git 是否干净。

版本说明（为什么 web 上是 openapi-typescript 6.x 而不是 7.x）：7.x 在运行时 import
TypeScript 编译器的 AST API（peerDependency: typescript ^5.x），而本仓库的 TypeScript
是 7.0.2（原生编译器，JS 包只导出 {version, versionMajorMinor}，没有 ts.factory），
7.x 一执行就 `TypeError: Cannot read properties of undefined (reading 'createKeywordTypeNode')`。
6.x 是字符串拼装生成、不依赖 TS 运行时 API，产出的 paths/components 形状与 7.x 一致。
等 TypeScript 侧回到 5.x，或 openapi-typescript 支持 TS7，再升级（升级后只需要重跑
本脚本 + `pnpm gen:api`，生成物结构不变）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 仓库根（scripts/ 的上一级）。用绝对路径插 sys.path，脚本从任何 cwd 运行都成立。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 这些默认值只在调用方没给时生效（setdefault），所以本地/CI 可以覆盖。
_MIN_ENV = {
    # schema 生成不需要真的数据目录；指到 /tmp 避免在仓库里留下 data/。
    "DATA_DIR": "/tmp/openllmwiki-openapi",
    # 只要非空即可（app/main.py 的 lifespan 才要求真实值，而这里不跑 lifespan）。
    "SESSION_SECRET": "openapi-export-not-a-real-secret-0123456789abcdef0123456789abcdef",
    # 与线上默认一致：人改路由在只读模式下不暴露写能力（schema 仍是同一份）。
    "WIKI_READONLY": "1",
    "PUBLIC_BASE_URL": "http://localhost:8000",
    # 不联网、不调模型；给空 key 让 config 正常构造。
    "LLM_API_KEY": "",
}


def _install_env() -> None:
    for key, value in _MIN_ENV.items():
        os.environ.setdefault(key, value)
    # 站点名（SITE_NAME）会进 OpenAPI 的 info.title（app/main.py 的 FastAPI(title=…)）。
    # 生成物必须与本机的 .env / shell 环境无关：app/config.py 用 setdefault 把仓库根
    # 真实 .env 读进 os.environ，本机配了 SITE_NAME 时导出物就会带真实站名，而 CI
    # 没有 .env、生成的是默认值——两边比 git diff 必红。所以这里用**赋值**（不是
    # setdefault：shell 里 export 过 SITE_NAME 时 setdefault 盖不住），强制默认站名。
    os.environ["SITE_NAME"] = "知识库"


def build_spec() -> dict:
    """导入 app 并返回 OpenAPI dict。import 必须发生在环境变量设好之后。"""
    _install_env()
    # 确保 `app` 包能被导入（脚本可能从任意 cwd 被调用）。
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    from app.main import app  # noqa: PLC0415 —— 故意延迟到 env 设好之后

    return app.openapi()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导出 FastAPI OpenAPI JSON（默认 stdout；不跑 lifespan、不建库）",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="输出文件路径（例如 web/openapi.json）；省略则写 stdout",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        metavar="N",
        help="JSON 缩进（默认 2；CI diff 依赖固定输出，一般不用改）",
    )
    args = parser.parse_args(argv)

    spec = build_spec()
    # sort_keys + ensure_ascii=False：中文标题/描述原样保留，键序稳定，方便 diff。
    text = json.dumps(spec, ensure_ascii=False, indent=args.indent, sort_keys=True) + "\n"

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = Path.cwd() / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        paths = spec.get("paths") or {}
        print(
            f"OpenAPI 已导出：{len(paths)} 个 path，{len(text.encode('utf-8'))} 字节 → {out}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
