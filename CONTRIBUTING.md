# 贡献

欢迎 issue、文档修正和 PR。

## 开发环境

- Python 3.11
- Node 22 + pnpm（版本由 `web/package.json` 的 `packageManager` 决定）
- 不需要真实模型 key 就能跑测试；测试会 mock 上游。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q

cd web
corepack pnpm install --frozen-lockfile
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm build
```

## 代码风格

- Python：四空格缩进、`snake_case`。
- TypeScript：两空格、分号、`camelCase`；React 组件 PascalCase。
- 没有配置 formatter/linter；跟随现有文件风格。

## 改 API 时

```bash
python scripts/export_openapi.py --out web/openapi.json
cd web && corepack pnpm gen:api
```

两个生成物必须与后端一致；CI 会检查 diff。

## PR

请说明：

1. 解决什么问题；
2. 行为怎么变（含错误/权限/持久化边界）；
3. 怎么验证（命令、测试、手工步骤）；
4. 涉及 UI 的附截图。

提交信息用中文或英文描述“改了什么、为什么”，不强制 Conventional Commits。

## 不要提交

- `.env`、密钥、token
- `data/`、wiki 页面、上传材料
- `web/node_modules`、`web/dist`
- 个人服务器/域名信息
