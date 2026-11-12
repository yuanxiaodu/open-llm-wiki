# 原生 LLM-Wiki 2.1.0 契约

这个项目只维护一种 vault 形状，和 Hermes 原装 `llm-wiki` 2.1.0 对齐：

```
SCHEMA.md
index.md
log.md
raw/
  articles/
  papers/
  transcripts/
  assets/
entities/
concepts/
comparisons/
queries/
_meta/topic-map.md      # 页面超过 200 时允许
_archive/               # 原生归档目录，可选
```

- 页面 `type` 只有 `entity/concept/comparison/query`。
- 页面 frontmatter 只有原生键：`title/created/updated/type/tags/sources/confidence/contested/contradictions`。
- `SCHEMA.md` 是 wiki 自己的文件，跟页面一起跟踪、同步；`app.toml` 不生成、不覆盖。
- `index.md` 是目录，跟页面一起跟踪、同步；服务端写页只 upsert 本页条目，pull 后只补缺页/去 ghost。
- `log.md` 是追加式历史，跟踪，合并用 `merge=union`。
- `raw/` 使用四个原生子目录；不再有 `raw/uploads` / `raw/messages`。
- 没有 `summaries/`、`summary` 类型、`status`、`analysis`、`author_id`、`basis_versions`、`review_notes`。
- 多来源综合用 `^[raw/路径]` 段落来源标记；不要写 `[^c1]` 脚注。
