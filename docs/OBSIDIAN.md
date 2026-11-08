# 本地 Obsidian 与 wiki 仓库

wiki 仓库就是普通 Markdown + Git，可以直接用 Obsidian 打开。它与项目仓库是**两个不同的 Git 仓库**。

## 目录结构

```
WIKI_DIR/
  SCHEMA.md        # 由 wiki.toml 生成，不要手改
  index.md         # 应用自动维护
  log.md           # 应用自动维护
  raw/
  entities/
  concepts/
  comparisons/
  queries/
  summaries/
```

## 方式一：应用直接读写 Obsidian vault

适合单机/本地开发：

```dotenv
WIKI_DIR=$HOME/Documents/Knowledge/my-wiki
WIKI_CONFIG=./wiki.toml
```

应用启动时会在该目录幂等执行 `git init`（如果还不是仓库）、生成 `SCHEMA.md`/`index.md`/`log.md`，之后每次写页都在这个仓库提交。Obsidian 里看到的改动与应用的提交是同一份历史。

注意：`WIKI_DIR` 指向的仓库不会被项目仓库跟踪；升级项目代码（`git pull` / `git merge`）不会动它。

## 方式二：服务端 vault + 本地 clone 同步（多机部署）

适合“线上应用写服务端 vault，作者在本地 Obsidian 编辑再发布”：

- 服务端 `WIKI_DIR=./data/wiki`（应用读写）。
- 本地 `WIKI_LOCAL_VAULT=~/Documents/Knowledge/my-wiki`（独立 clone，Obsidian 打开）。
- 同步脚本做：本地预检 → commit/push outbox → 调服务端 `app.cli sync` 逐页重放 → 成功后对齐。
- `WIKI_LOCAL_VAULT` 只给脚本用，应用不读；两边的 Git 历史最终通过 wiki 仓库的 remote 合并。

本仓库核心提供 `app.cli sync`（服务端逐页重放）；具体同步脚本属于部署方（PM_wiki 私有 fork 的 `scripts/pmwiki/sync_vault_wiki.sh` 是一个案例）。

## 本地 vault 的 .gitignore

同步模式建议让派生文件/材料不进 outbox：

```gitignore
.gitignore
.DS_Store
raw/
index.md
log.md
SCHEMA.md
*.pdf
*.docx
*.pptx
*.xlsx
*.png
```

`SCHEMA.md` 由服务端按 `wiki.toml` 生成；本地手改会在下一次生成时被覆盖。

## 与项目仓库的边界

- 项目仓库：`app/`、`web/`、`wiki.toml`、`deploy/`、`docs/`、`plugins/`。
- wiki 仓库：页面、`SCHEMA.md`、`index.md`、`log.md`、`raw/`。
- 不要把 wiki 页面提交进项目仓库；不要把项目代码提交进 wiki 仓库。
- 备份、remote、升级分别处理：项目仓库 `git pull`，wiki 仓库用应用备份/`git` 历史。
