# 架构

## 两个 Git 仓库

| 仓库 | 内容 | 谁维护 |
|---|---|---|
| 项目仓库（本仓库） | `app/`、`web/`、`tests/`、`scripts/`、`deploy/`、`docs/`、`app.toml`、`plugins/` | 开发者 |
| wiki 仓库（`WIKI_DIR`） | `SCHEMA.md`、`index.md`、`log.md`、页面、`raw/` | 应用/智能体（人可读） |

- 项目仓库不跟踪 `WIKI_DIR`；默认 `DATA_DIR/wiki`，也可指向任意独立 vault（如本地 Obsidian）。
- wiki 仓库有独立 `.git`、remote、备份；`SCHEMA.md`/`index.md`/`log.md` 是 wiki 自己的文件，跟页面一起跟踪、同步。
- `WIKI_CONFIG`（默认 `app.toml`）是项目侧应用配置；它不渲染、不写入 `SCHEMA.md`。`SCHEMA.md` 属于 wiki 仓库。

## 后端模块

```
app/
  main.py            FastAPI 装配、lifespan、SPA 回退、错误信封
  config.py          环境变量 → Settings（含 WIKI_DIR）
  wiki_config.py     app.toml 解析 / 校验 / /api/site payload
  plugins.py         插件发现、生命周期、路由/工具、/api/site 插件列表
  db.py              SQLite 连接、迁移、封口
  deps.py            登录/权限依赖
  routers/api/       auth / chat / wiki / ingest / uploads / search / members / admin / site
  services/          chat、llm、uploads、document_ingest、web_ingest、search、auth、access
  wiki/              vault 读写、Git 提交、index/log、lint、quality、sync、migrate
  agent/             Agent 装配、工具、hooks、流式落库
plugins/<id>/        可选插件（后端 + 前端入口）
```

## 一次对话回合

1. `POST /api/chat/{id}/messages` 把人话落库，后台 `asyncio.create_task(run_turn)`。
2. `run_turn` 装配 Agent（技能 + `app.toml` persona/rules + 库现状 + 本轮权限）。
3. 工具在 worker 线程执行，写 `agent_events` / 页面 / Git 提交。
4. 正文增量写 `messages`，SSE 端点从另一条连接尾读，断线可续读。
5. 回合结束封口；错误按配额/上游/内部错误分别落 system/error 事件。

## 写页与 Git

- 页面 path 形如 `{dir}/{slug}.md`，type 决定目录。
- 写路径先做形状/并发/恒等校验，再落盘；页面 + `index.md` + `log.md` 一次 commit。
- `SCHEMA.md` 是 wiki 自己的 schema（默认来自 llm-wiki 模板），跟页面一起跟踪、同步；
  `app.toml` 不生成、不覆盖它。
- `index.md` 是目录，跟页面一起跟踪、同步；服务端写页只 upsert 对应条目，不整体重写。
- `log.md` 是追加式文件；`.gitattributes` 里 `merge=union`，两端追加都保留。
- 服务端每个 agent 回合开始 `git pull --rebase`，回合结束 `git push`；非快进自动 rebase 重试，冲突停止。
- 本地 vault 由 `scripts/sync-local.sh` 做同样的标准 git 流程，由用户在 Hermes 里一句话触发。
- 项目仓库与 wiki 仓库的提交互不影响。

## 插件

- 后端：`app.toml [plugins] enabled` → `plugins.<id>` 包导出 `plugin` 对象 → 路由、工具、lifecycle。
- 前端：`plugins/<id>/web/index.tsx` 由 Vite `import.meta.glob` 构建期发现；`/api/site` 的 `plugins` 决定是否挂载路由/nav。
- 插件路由默认不进核心 OpenAPI schema。
- 详见 [PLUGINS.md](PLUGINS.md)。

## 权限与单进程约束

- 登录用户分管理员/成员；成员可被授予建设权限。
- 浏览器 wiki 默认只读（`WIKI_READONLY=1`）；写只走智能体工具。
- SQLite + 进程内锁/队列，生产必须单 worker。
