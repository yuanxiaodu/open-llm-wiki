# Open LLM Wiki

一个可自托管、由 AI 智能体维护的知识库：聊天对话、摄入材料、写 wiki 页面、维护互链与 Git 历史。页面是普通 Markdown，可直接用 Obsidian 打开。

An open-source, self-hosted knowledge base maintained by an AI agent. Chat with it, feed it documents and links, and it writes linked Markdown pages into a Git-backed vault you can open in Obsidian. Plugins can extend the backend and the web UI.

- 后端：Python 3.11 / FastAPI / SQLite / OpenAI Agents SDK
- 前端：React 19 / TypeScript / Vite
- wiki 存储：Markdown + Git（独立仓库，Obsidian 兼容）
- 许可：MIT

## 特性

- **智能体维护 wiki**：`read` / `search` / `write_page` / `edit_page` / `lint` 等工具，写页自动追加 `log.md` 并提交 Git；`index.md` / `SCHEMA.md` 是生成物，不进 Git。
- **标准 git 同步**：服务器每个回合开始 `pull`、结束 `push`；本地 vault 由一句话触发 `scripts/sync-local.sh`（commit → pull --rebase → push）。冲突停在人面前，不 force push。
- **多来源摄入**：PDF、Word、PPT、Excel、图片、网页、粘贴文字；长文档走后台分块编译（`ingest_document`）。
- **多用户**：登录、邀请注册、管理员、只读/建设权限、对话可见范围。
- **SSE 流式对话**：工具事件、断线续读、消息落库。
- **可配置 wiki**：`wiki.toml` 定义站名、领域、标签体系、页面类型中文名、智能体人设、长文摄入提示词、欢迎语。
- **插件系统**：后端插件包 + 构建期前端入口；默认不启用任何功能插件。
- **独立 wiki 仓库**：项目代码与知识库数据是两个 Git 仓库；`WIKI_DIR` 可指向任意独立 vault，包括本地 Obsidian vault。

## 架构：两个 Git 仓库

```
project repo (this repo)                 wiki repo (WIKI_DIR)
  app/ web/ tests/ scripts/                SCHEMA.md index.md log.md
  deploy/ docs/ wiki.toml                  entities/ concepts/ raw/ ...
        │                                       ▲
        └──────────── 应用运行时读写 ────────────┘
```

- **项目仓库**只放代码、配置、部署、文档、`wiki.toml`。
- **wiki 仓库**只放页面、`SCHEMA.md`、`index.md`、`log.md`、`raw/`；它有独立提交历史、remote 和备份。
- `WIKI_CONFIG` 指向项目侧配置；`WIKI_DIR` 指向 wiki 仓库（默认 `DATA_DIR/wiki`）。
- `SCHEMA.md` 由 `wiki.toml` 生成；不要手改。

如果希望应用直接读写本地 Obsidian vault，把 `WIKI_DIR` 指过去即可；如果像多机部署那样保留一份本地 clone，再用同步脚本发布，见 [docs/OBSIDIAN.md](docs/OBSIDIAN.md)。

## 快速开始

要求 Python 3.11、Node 22、pnpm（版本由 `web/package.json` 的 `packageManager` 决定）。

```bash
git clone https://github.com/yuanxiaodu/open-llm-wiki.git
cd open-llm-wiki

python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# 必填：SESSION_SECRET、ADMIN_PHONE、ADMIN_PASSWORD、LLM_API_KEY
# 可选：SITE_NAME / WIKI_DIR / WIKI_CONFIG

python -m app.cli seed
uvicorn app.main:app --reload
```

另开一个终端：

```bash
cd web
pnpm install --frozen-lockfile
pnpm dev
```

打开 Vite 给出的地址（默认 `http://localhost:5173`），用 `.env` 里的管理员账号登录。

`WIKI_READONLY=1`（默认）时浏览器只能读 wiki，写只由智能体工具完成；本地要用 `/api/wiki` 写接口造数据时设 `WIKI_READONLY=0`。

## 配置 wiki

编辑项目根 `wiki.toml`，重启应用即可：

```toml
[site]
name = "知识库"

[wiki]
domain = "这个知识库记录什么、事实依据怎么标。"

[wiki.taxonomy]
tags = ["概念", "方法", "案例", "工具"]

[wiki.type_labels]
entity = "实体"
concept = "概念"
```

完整字段见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。配置变化会在下次启动时重写 wiki 仓库里的 `SCHEMA.md` 并提交。

## 插件

插件是 `plugins/<id>/` 下的 Python 包 + 可选 `web/index.tsx`。默认 `[plugins] enabled = []`，本仓库不附带任何功能插件。

```toml
[plugins]
enabled = ["my-plugin"]

[plugins.my-plugin]
# 传给 plugin.configure() 的选项
```

后端接口、生命周期、前端路由/nav、测试与 OpenAPI 约定见 [docs/PLUGINS.md](docs/PLUGINS.md)。插件路由默认不进入核心 OpenAPI schema，避免 fork 升级时生成文件冲突。

## 开发与测试

```bash
python -m pytest -q

cd web
pnpm test
pnpm exec tsc --noEmit
pnpm build
```

改后端 API 后重新生成契约：

```bash
python scripts/export_openapi.py --out web/openapi.json
cd web && pnpm gen:api
```

## 部署

- 通用部署（systemd + Caddy/nginx、单 worker、备份）见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。
- 项目与 wiki 仓库的边界、Obsidian 同步见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/OBSIDIAN.md](docs/OBSIDIAN.md)。
- 一个真实部署案例（私有 fork + 书稿阅读器插件）见 [docs/DEPLOYMENT_CASE_PMWIKI.md](docs/DEPLOYMENT_CASE_PMWIKI.md)。

生产环境必须使用单 worker：写锁、对话执行守卫和后台摄入队列都是进程内状态。

## 安全

- 永不提交 `.env`、`data/`、wiki 内容或密钥。
- 首次部署后更换管理员密码与 `SESSION_SECRET`。
- 邀请 token 会出现在访问日志里；生产请启用 HTTPS 并只私发链接。
- 细节见 [SECURITY.md](SECURITY.md)。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。提交信息用中文或英文描述“改了什么、为什么”，PR 请写清验证方式。

## License

[MIT](LICENSE)
