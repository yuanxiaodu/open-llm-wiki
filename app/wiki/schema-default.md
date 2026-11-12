# Wiki Schema

 > 本文件属于 wiki 本身，跟页面一起跟踪、一起同步；由维护者/智能体编辑。
 > 它不是 `wiki.toml` 的生成物，服务端启动时只会在文件缺失时补一份默认骨架。

## Domain

（待补充：这个知识库记录什么、事实依据怎么标。）

## Conventions

- 页面 path 形如 `{dir}/{slug}.md`；`type` 决定目录，创建后不可改（entity/concept/comparison/query）。
- 来源写在 frontmatter 的 `sources` 里；正文不强制逐句引注。
- 页面互链用 `[[slug]]` 或 `[[slug|显示文字]]`；每页至少 2 个出链。
- 每页 ≤ 200 行；超过就拆页。
- `index.md`、`SCHEMA.md`、`log.md` 都是 wiki 的一部分，跟页面一起跟踪、同步。
- `log.md` 是追加式历史，合并用 `merge=union`。
- 多来源综合在段落末尾用 `^[raw/路径]` 标来源；不要写 `[^c1]` 脚注。
- 矛盾显式记录：frontmatter 写 `contested: true` 与 `contradictions: [other-slug]`。

## Tag Taxonomy

- 概念
- 方法
- 案例
- 工具
- 人物
- 组织
- 研究
- 其他

## Page Thresholds

- 一个实体/概念在 2+ 来源出现，或是一份材料的核心，才建页。
- 通过提及、细节、域外内容不建页；同主题先改既有页。
- 页面超过 200 行拆成子主题并互链。
- 内容被完全取代时移到 `_archive/`，从 index 删除。

## Entity Pages

一页一个实体：概述、关键事实/日期、与其它实体的关系、来源。

## Concept Pages

一页一个概念/主题：定义、当前知识状态、开放问题/争议、相关概念。

## Comparison Pages

对比页：比较什么、维度、结论/综合、来源。

## Update Policy

1. 先看日期，新来源一般覆盖旧来源。
2. 真矛盾时并列两说并标日期/来源。
3. frontmatter 写 `contradictions`，lint 报告给人复核。
