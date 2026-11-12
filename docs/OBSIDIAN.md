# 本地 Obsidian 与 wiki 仓库

wiki 仓库就是普通 Markdown + Git，可以直接用 Obsidian 打开。它与项目仓库是**两个不同的 Git 仓库**。

## 目录结构

```
WIKI_DIR/
  SCHEMA.md        # wiki 自己的 schema；跟踪、同步
  index.md         # 目录；跟踪、同步
  log.md           # 追加式行动日志；跟踪，merge=union
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

应用启动时会在该目录幂等执行 `git init`（如果还不是仓库），只在缺失时补 `SCHEMA.md`/`index.md`/`log.md`，之后每次写页都在这个仓库提交。Obsidian 里看到的改动与应用的提交是同一份历史。

注意：`WIKI_DIR` 指向的仓库不会被项目仓库跟踪；升级项目代码（`git pull` / `git merge`）不会动它。

## 方式二：服务端 vault + 本地 clone 同步（多机部署）

适合“线上应用写服务端 vault，作者在本地 Obsidian 编辑再发布”：

- 服务端 `WIKI_DIR=./data/wiki`（应用读写），`origin` 指向共享 bare 仓库。
- 本地 `WIKI_LOCAL_VAULT=~/Documents/Knowledge/my-wiki`（独立 clone，Obsidian 打开）。
- 服务端：每个 agent 回合开始 `git pull --rebase`，回合结束 `git push`；没有 remote 时自动跳过。
- 本地：在 Hermes 里说“同步 wiki / 发布 / 对齐线上”，agent 调 `scripts/sync-local.sh`：
  commit → `pull --rebase` → push → 重建本地 `index.md`。冲突时脚本 abort rebase、退出码 2。

两端都是标准 git，不再有 outbox 分支或 `app.cli sync` 逐页重放。`SCHEMA.md` / `index.md`
是 wiki 自己的文件，跟页面一起跟踪、同步；`log.md` 用 `.gitattributes` 的
`merge=union` 追加合并。

服务端也可以手动跑：

```bash
python -m app.cli git-pull    # pull --rebase + 重建 index/FTS
python -m app.cli git-push    # push；非快进自动 rebase 重试
python -m app.cli git-status  # 只读：ahead/behind/dirty
```

## 本地 vault 的 .gitignore

同步模式建议：

```gitignore
.DS_Store
*.pdf
*.docx
*.pptx
*.xlsx
*.png
```

`SCHEMA.md` / `index.md` / `log.md` 都跟踪：它们是 wiki 的一部分，不是服务端生成物。
`raw/` 里的 Markdown 来源默认也跟踪，跟着页面一起同步；大文件按需换 LFS 或单独 rsync。
同步脚本会自动补齐 `.gitignore` / `.gitattributes`，并把旧 vault 里误跟踪的
`index.md` / `SCHEMA.md` 从索引移除（文件留在磁盘）。

## 与项目仓库的边界

- 项目仓库：`app/`、`web/`、`wiki.toml`、`deploy/`、`docs/`、`plugins/`。
- wiki 仓库：页面、`SCHEMA.md`、`index.md`、`log.md`、`raw/`。
- 不要把 wiki 页面提交进项目仓库；不要把项目代码提交进 wiki 仓库。
- 备份、remote、升级分别处理：项目仓库 `git pull`，wiki 仓库用应用备份/`git` 历史。
