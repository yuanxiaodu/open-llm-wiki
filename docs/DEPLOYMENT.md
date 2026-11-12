# 部署

## 运行要求

- Python 3.11
- Node 22（只用于构建前端；服务器不需要 Node）
- 一个 OpenAI 兼容的模型端点
- 单进程运行：`uvicorn --workers 1`（写锁、对话守卫、摄入队列都是进程内状态）

## 目录布局

```
/opt/open_llm_wiki/         项目仓库
  .env
  .venv/
  data/                     DATA_DIR
    db.sqlite3
    uploads/
    extracted_text/
    backups/
    wiki/                   默认 WIKI_DIR（独立 git 仓库）
  web/dist/                 前端构建产物
```

也可以把 `WIKI_DIR` 指到独立磁盘/Obsidian vault；项目仓库不跟踪它。

## 首次部署

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv git
sudo mkdir -p /opt/open_llm_wiki
sudo chown "$USER":"$USER" /opt/open_llm_wiki
cd /opt/open_llm_wiki
git clone https://github.com/yuanxiaodu/open-llm-wiki.git .
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
$EDITOR .env   # SESSION_SECRET、ADMIN_PHONE/PASSWORD、LLM_API_KEY、PUBLIC_BASE_URL
.venv/bin/python -m app.cli seed
```

前端在本机构建后 rsync：

```bash
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm build
rsync -av --delete web/dist/ server:/opt/open_llm_wiki/web/dist/
```

### systemd

模板在 `deploy/openllmwiki.service`，按实际路径/用户修改后：

```bash
sudo cp deploy/openllmwiki.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openllmwiki
systemctl status openllmwiki --no-pager
```

`ExecStart` 必须保持 `--workers 1`。

### 反向代理

- Caddy：`deploy/Caddyfile`（`DOMAIN` 环境变量，自动 HTTPS）。
- nginx：`deploy/nginx.conf.example`（替换 `example.com`，SSE 关闭缓冲）。
- 上传上限建议 60MB；SSE 的 `proxy_buffering off` 必须保留。

## 升级

```bash
cd /opt/open_llm_wiki
git pull
.venv/bin/pip install -r requirements.txt
# 如果前端有改动：本机构建后 rsync web/dist/
sudo systemctl restart openllmwiki
```

升级前备份：

```bash
.venv/bin/python -m app.cli backup --keep 14
```

wiki 仓库（`WIKI_DIR`）是独立 Git 仓库；项目升级不会改它。`SCHEMA.md` 属于 wiki 仓库，项目升级不会重新生成它；缺失时只补默认骨架。

## 备份

- `app.cli backup`：SQLite + uploads + extracted_text + wiki 仓库（含 `.git`）。
- 生产建议再加对象存储备份（`deploy/litestream.yml`、`deploy/backup.env.example` 是可选模板）。
- 项目仓库与 wiki 仓库分开备份；wiki 是主要不可再生资产。

## 多用户与安全

- 首次登录后立刻更换管理员密码。
- 生产必须 HTTPS；`COOKIE_SECURE=1`。
- 邀请 token 会出现在 access log；不要把日志贴到公开渠道。
- 默认 `WIKI_READONLY=1`：浏览器只读，写由智能体工具完成。
- 若反向代理后面跑，设置 `--proxy-headers --forwarded-allow-ips=127.0.0.1`。
