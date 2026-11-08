# 部署案例：PM_wiki

这是一个真实使用 open-llm-wiki 的私有部署案例，用来说明“项目仓库 / wiki 仓库 / 插件”如何组合。案例本身不随开源仓库发布代码，也不包含任何私有数据或服务器信息。

## 拓扑

```
open-llm-wiki (public project)
        │  fork + merge upstream
PM_wiki (private project fork)
        │  WIKI_DIR
PM_wiki wiki repo (separate git, own remote/history)
        │  WIKI_LOCAL_VAULT (optional)
local Obsidian clone (separate git, sync scripts)
```

- **项目仓库**：与上游 core 保持一致；只新增部署覆盖层。
- **wiki 仓库**：独立 Git 仓库，存页面和生成的 `SCHEMA.md`/`index.md`/`log.md`。
- **本地 Obsidian clone**：可选的另一份 clone，用于离线编辑和同步；不是应用的 `WIKI_DIR`。

## 覆盖层

PM_wiki fork 相对上游只新增以下内容（不改 `app/`、`web/src/`、`tests/` 等 core 文件）：

```
wiki.pmwiki.toml          # 站点/领域/标签/欢迎语；[plugins] enabled = ["book"]
plugins/book/             # 书稿阅读器插件（私有二次开发）
deploy/pmwiki/            # 真实部署的 systemd/nginx/备份模板
scripts/pmwiki/           # 同步与测试脚本
README.pmwiki.md          # 本部署说明
tests/test_book_plugin.py # 书稿插件测试
```

`.env` 与 `data/` 被 Git 忽略；`WIKI_DIR=./data/wiki` 指向上面的独立 wiki 仓库。

## 书稿插件

书稿阅读器是 fork 自己的插件示例，不在开源仓库中：

- 后端：`plugins/book/__init__.py` 导出 `plugin`，提供 `/api/book/*` 只读路由；`[plugins.book] data_dir` 可配置书稿目录。
- 前端：`plugins/book/web/index.tsx` 默认导出 `{ id: "book", routes }`；核心的 `import.meta.glob` 自动发现，`/api/site` 启用后才挂载 `/book` 与导航。
- 数据：`data/book/` 是普通只读目录，不是 Git 仓库；可用 rsync/脚本从本地 Obsidian 导入。

这验证了插件契约：核心不需要知道书稿、`/book` 或任何书稿数据；禁用 `[plugins] enabled = ["book"]` 后，路由与导航都不出现。

## 配置

```toml
# wiki.pmwiki.toml（项目仓库）
[site]
name = "示例知识库"

[wiki]
domain = "示例领域：记录项目资料与研究笔记。"

[wiki.taxonomy]
tags = ["研究", "方法", "案例", "工具"]

[plugins]
enabled = ["book"]

[plugins.book]
data_dir = "./book"
```

```dotenv
# .env（不提交）
WIKI_CONFIG=wiki.pmwiki.toml
WIKI_DIR=./data/wiki
WIKI_LOCAL_VAULT=$HOME/Documents/Knowledge/my-wiki
WIKI_OBSIDIAN_VAULT=$HOME/Documents/Knowledge
```

## 升级

```bash
git fetch upstream
git merge upstream/main
```

因为覆盖层只新增文件、不改 core，合并通常没有冲突；插件路由不进核心 OpenAPI，`web/openapi.json` 也不会漂移。

## 备份

- 项目仓库：正常 `git` + 部署目录备份。
- wiki 仓库：`tar` 整个 `WIKI_DIR`（含 `.git`）+ `app.cli backup`。
- 本地 Obsidian clone：它只是同步端，按需备份。

## 不包含什么

本案例文档不包含：真实域名/IP、服务器凭据、wiki 页面内容、书稿内容、`.env`。书稿插件代码只存在于私有 fork。
