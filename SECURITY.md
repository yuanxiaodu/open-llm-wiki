# 安全

## 报告漏洞

请不要在公开 issue 里贴利用细节、密钥或用户数据。通过 GitHub Security Advisory 或私下联系维护者，附最小复现和影响范围。

## 部署者须知

- `.env` 永不进 Git；`SESSION_SECRET` 用长随机串。
- 首次部署后更换管理员密码；删除/停用不需要的账号。
- 生产必须 HTTPS，并设 `COOKIE_SECURE=1`。
- 邀请 token 出现在 URL 与 access log；只私发，泄露后在后台作废。
- 默认 `WIKI_READONLY=1`；不要让不可信用户获得建设权限。
- 单 worker 运行；不要用多进程共享 SQLite 写锁。
- 定期备份 `WIKI_DIR`（含 `.git`）与 SQLite；wiki 是主要不可再生资产。
- 上传材料可能含敏感内容；`DATA_DIR/uploads`、`extracted_text` 按敏感数据保护。

## 依赖

依赖版本固定在 `requirements*.txt` 与 `web/pnpm-lock.yaml`。升级请跑完整测试与构建。
