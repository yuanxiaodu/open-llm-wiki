# Wiki Schema

> 本文件属于 wiki 本身，跟页面一起跟踪、一起同步；由维护者/智能体编辑。
> 它不是 `wiki.toml` 的生成物，服务端启动时只会在文件缺失时补一份默认骨架。

## Domain

（待补充：这个知识库记录什么、事实依据怎么标。）

## Conventions

- 页面 path 形如 `{dir}/{slug}.md`；`type` 决定目录，创建后不可改。
- 来源写在 frontmatter 的 `sources` 里；正文不强制逐句引注。
- 页面互链用 `[[slug]]` 或 `[[slug|显示文字]]`；每页至少 2 个出链。
- `index.md`、`SCHEMA.md`、`log.md` 都是 wiki 的一部分，跟页面一起跟踪、同步。
- `log.md` 是追加式历史，合并用 `merge=union`。
- 矛盾显式记录：frontmatter 写 `contested: true` 与 `contradictions: [other-slug]`。

## tags

- 概念
- 方法
- 案例
- 工具
- 人物
- 组织
- 研究
- 其他
