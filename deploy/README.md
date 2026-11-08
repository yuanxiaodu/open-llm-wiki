# 部署模板

这些文件是可选的部署素材，详细步骤见 [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)。

- `openllmwiki.service`：应用 systemd 单元（`--workers 1`）。
- `openllmwiki-backup.*` / `openllmwiki-restic.*` / `openllmwiki-litestream.*`：备份模板。
- `openllmwiki-smoke.*`：定时巡检模板。
- `Caddyfile`、`nginx.conf.example`：反向代理模板。
- `deploy.sh`：rsync + 远端 venv + systemd 的发布脚本。
- `backup.env.example`、`rclone.conf.example`：备份凭据模板。

使用前把 `/opt/open_llm_wiki`、用户、域名、`LLM_*` 等改成你的部署值。所有真实凭据只放服务器上的 `.env` / `/etc/openllmwiki/backup.env`，不要进 Git。
