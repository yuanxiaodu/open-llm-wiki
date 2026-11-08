# 配置

## wiki.toml（项目仓库）

`wiki.toml` 是 wiki 的身份与编辑策略，不包含密钥。字段：

| 字段 | 说明 |
|---|---|
| `site.name` | 站名；`SITE_NAME` 环境变量优先 |
| `site.description` | 一句话描述，`/api/site` 下发 |
| `wiki.domain` | 领域说明，写入生成的 `SCHEMA.md` 并进入长文摄入提示词 |
| `wiki.extra_rules` | 追加到 `SCHEMA.md` conventions 的编辑规则 |
| `wiki.taxonomy.tags` | 标签白名单；lint 用它校验 |
| `wiki.type_labels` | entity/concept/comparison/query/summary 的中文名 |
| `agent.persona` | 智能体人设，拼在技能正文前 |
| `agent.rules` | 额外规则，拼在技能正文后 |
| `ingest.system_prompt` | 可选；长文摄入系统提示词，不写则用内置模板 |
| `ui.build_actions` / `ui.readonly_actions` | 对话空态动作卡（key/icon/title/example） |
| `plugins.enabled` | 启用的插件 id 列表，默认空 |
| `[plugins.<id>]` | 传给插件 `configure()` 的表 |

`SCHEMA.md` 由 `wiki.toml` 生成；改配置后重启应用，应用会在 wiki 仓库提交新的 SCHEMA。

## 环境变量（.env）

### 应用读取

| 变量 | 说明 |
|---|---|
| `SITE_NAME` | 站名覆盖；空则用 `wiki.toml` |
| `PUBLIC_BASE_URL` | 对外地址 |
| `DATA_DIR` | 运行数据（DB、uploads、extracted_text、backups）；默认 `./data` |
| `WIKI_DIR` | wiki 仓库路径；空则 `DATA_DIR/wiki`。可指向本地 Obsidian vault |
| `WIKI_CONFIG` | wiki.toml 路径；空则项目根 `wiki.toml` |
| `SESSION_SECRET` | 必填，会话签名 |
| `ADMIN_NAME` / `ADMIN_PHONE` / `ADMIN_PASSWORD` | seed 管理员 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容端点 |
| `LLM_THINKING` | DeepSeek thinking 开关，默认 disabled |
| `WIKI_READONLY` | 1（默认）浏览器只读；0 允许 `/api/wiki` 写路由 |
| `SENTRY_DSN` / `ENVIRONMENT` / `LOG_LEVEL` / `RELEASE` | 可观测性，可选 |

`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `TEXT_MODEL` 仍可作为旧名别名。

### 部署/同步脚本读取（应用不读）

`WIKI_SERVER_*`、`WIKI_SSH_*`、`WIKI_LOCAL_VAULT`、`WIKI_OBSIDIAN_VAULT`、`WIKI_SYNC_BRANCH` 等只给 `deploy/` 或你 fork 里的同步脚本用。注意：

- `WIKI_DIR` 是应用直接读写的 vault。
- `WIKI_LOCAL_VAULT` 是本地 Obsidian clone，只给同步脚本用；两者可以相同，但多机部署通常分开。

## 文件位置

```
DATA_DIR/
  db.sqlite3
  uploads/
  extracted_text/
  backups/
  wiki/            # 默认 WIKI_DIR；独立 git 仓库
```

`WIKI_CONFIG` 在项目仓库；`WIKI_DIR` 在项目仓库之外或 `data/`（已被 .gitignore 忽略）。
