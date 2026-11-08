## 问题

<!-- 这次改动解决什么？关联 issue/设计文档。 -->

## 行为变化

<!-- 用户可见的行为、API、权限、持久化、插件或配置变化。 -->

## 验证

<!-- 跑过的命令/测试/手工步骤；UI 改动附截图。 -->

- [ ] `python -m pytest -q`
- [ ] `cd web && pnpm test && pnpm exec tsc --noEmit && pnpm build`
- [ ] 改了 API 时已重新生成 `web/openapi.json` 与 `web/src/api-types.gen.ts`

## 边界与风险

<!-- 迁移、兼容性、权限、失败模式；没有就写“无”。 -->
