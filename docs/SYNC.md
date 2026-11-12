# 同步：服务器自动，本地一句话触发

wiki 仓库就是一个普通 Git 仓库。没有 outbox 分支，也不在服务端逐页重放：两端都走
`pull --rebase → commit → push`。区别只在触发方式。

## 文件契约

| 文件 | 是否跟踪 | 合并方式 |
|---|---|---|
| `{entities,concepts,comparisons,queries,summaries}/*.md` | 是 | 标准三方合并 |
| `log.md` / `log-*.md` | 是 | `.gitattributes` 的 `merge=union` |
| `index.md` | 否（生成物） | 每次 pull/写页后重建 |
| `SCHEMA.md` | 否（生成物） | 由 `wiki.toml` 重建 |
| `raw/` | 按部署策略 | 材料文件，普通 git / LFS |

生成物不进 Git 是两个 writer 不互相打架的关键：目录和 schema 永远由页面/配置重建，
不在文本层面合并。

## 服务端

应用自动执行，不需要人操作：

- **回合开始**：`git pull --rebase`，重建 `index.md`/`SCHEMA.md` 与全文检索；
  冲突/脏工作区时这一轮不开始，直接告诉用户先处理 git。
- **写页**：store 提交页面 + `log.md`（同一次 commit）。
- **回合结束**：`git push`；非快进自动 `pull --rebase` 重试一次，冲突就停。
- **启动**：先 pull 再 `ensure_vault`；冲突拒绝启动，网络故障只告警。

手动命令：

```bash
python -m app.cli git-pull --json
python -m app.cli git-push --json
python -m app.cli git-status --json
```

环境变量：`WIKI_AUTO_SYNC=1`（默认）、`WIKI_REMOTE=origin`、`WIKI_BRANCH=main`。
没有 remote 的单机 vault / 测试自动跳过。

## 本地

本地不跑后台 daemon。在 Hermes 里说“同步 wiki / 发布 / 对齐线上”，agent 运行：

```bash
scripts/sync-local.sh -m "本次改了什么"
```

脚本做：

1. `git add -A`，提交本地改动（`index.md`/`SCHEMA.md` 被忽略）；
2. `git pull --rebase`；冲突则 abort、列出冲突文件、退出码 2；
3. `git push`；被拒绝则再 `pull --rebase` 一次并重试；
4. 用本地 Python 重建 `index.md`（生成物，不提交）。

只查看状态：

```bash
scripts/sync-local.sh --status
```

Hermes 原装技能不知道这个脚本；在本地 vault 根放一份 `AGENTS.override.md`（建议加进
`.git/info/exclude`，只在本机生效），把用户的话映射到脚本即可，不用改技能：

```markdown
用户说「同步 wiki / 发布 / 对齐线上」时，运行：
scripts/sync-local.sh -m "<本批说明>"
遇到退出码 2 就停下报告冲突文件，不要 force push。
```

## 冲突处理

- 页面冲突：脚本/服务端都 abort，不自动选边；人/agent 解决后重跑。
- `log.md`：`merge=union`，两端追加都保留；重复条目可再跑去重。
- `index.md` / `SCHEMA.md`：不参与合并，pull 后重建。
- 永不 force push；本地提交始终留在历史里。
