from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .wiki_config import DEFAULT_SITE_NAME, get_config

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # 可选的独立 wiki vault 路径（WIKI_DIR）。留空 = data_dir/wiki。
    # 它通常是另一条 git 仓库（甚至就是本地 Obsidian vault），项目仓库不跟踪它。
    wiki_dir_override: Path | None
    session_secret: str
    public_base_url: str
    admin_name: str
    admin_phone: str
    admin_password: str
    # 站名（SITE_NAME）：部署方唯一要改的品牌字。后端 /api/site 与 SPA 入口页标题
    # 都从这一份出，前端不再把站名写死在源码里——换站名只改 env，不改代码。
    site_name: str
    # 智能体模型基座（S4：DeepSeek，裁定 R-S4-18）。字段名不带厂商：换基座只改 env，
    # 读代码的地方不必跟着改名。
    llm_api_key: str
    llm_base_url: str
    # 思考模式："disabled"（默认）|"enabled"。DeepSeek 思考默认**开**，而它要求带 tools
    # 的请求把历史 reasoning_content 完整回传——我们的 harness 不保存思考内容，所以默认
    # 必须关（见 app/services/llm.py 的 model_settings）。"enabled" 只留给将来可选的路径。
    llm_thinking: str
    text_model: str
    # wiki 只读开关（裁定 R-S4-20）：默认开。人类面（浏览器 + /api/wiki 的人改路由）
    # 只能看不能写；写只走智能体的工具（它们直接调 store，不经 HTTP）。测试与
    # 开发机把它设成 0 才能用 API 造数据。
    wiki_readonly: bool
    # 服务端自动 git 同步（2026-11-13）：每个回合开始 pull、结束 push；没有 remote
    # 的测试/单机 vault 自动跳过。默认开，`WIKI_AUTO_SYNC=0` 可关。
    wiki_auto_sync: bool
    wiki_git_remote: str
    wiki_git_branch: str
    # 可观测性（S12 最小集）：全部可选，默认不联网。
    # sentry_dsn 为空 = 完全关闭 Sentry（进程不发任何网络请求）；有值时由
    # app/observability.init_sentry 初始化一次，DSN 不落日志。
    sentry_dsn: str
    environment: str          # production（默认）/ development（控制台可读日志）
    log_level: str            # CRITICAL/ERROR/WARNING/INFO/DEBUG，非法值落回 INFO
    release: str              # 可选版本号（git sha / tag），空则不传给 Sentry

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def extracted_text_dir(self) -> Path:
        return self.data_dir / "extracted_text"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def wiki_dir(self) -> Path:
        return self.wiki_dir_override or (self.data_dir / "wiki")


# SITE_NAME 的默认值来自 app.toml（见 app/wiki_config.py）；前端
# web/src/site.ts 的 DEFAULT_SITE_NAME 与它同值。两边由 tests/test_site.py
# 与 web/src/site.test.ts 各自的钉子钉住。

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})


def _site_name(raw: str, default: str = DEFAULT_SITE_NAME) -> str:
    """SITE_NAME 归一：空串/纯空白落回默认。

    站名是纯展示字，.env 里写成 `SITE_NAME=` 或顺手多打几个空格时，页面标题不该
    变成一个空串（浏览器标签页会显示成地址），silently 回到默认值就是人想要的结果。
    默认值来自 app.toml 的 site.name；本函数的 default 参数让「配置里的站名」
    也能享受同一条归一规则。
    """
    return (raw or "").strip() or default or DEFAULT_SITE_NAME


def _log_level(raw: str) -> str:
    """LOG_LEVEL 白名单：非法值/空串落回 INFO（日志级别配错不该让进程起不来）。"""
    value = (raw or "").strip().upper()
    return value if value in _LOG_LEVELS else "INFO"


def _resolve(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _resolve_optional(raw: str) -> Path | None:
    """空串/纯空白 = 没配置；其余按项目根解析（支持 ~ 与绝对路径）。"""
    clean = (raw or "").strip()
    return _resolve(clean) if clean else None


settings = Settings(
    data_dir=_resolve(os.environ.get("DATA_DIR", "./data")),
    wiki_dir_override=_resolve_optional(os.environ.get("WIKI_DIR", "")),
    session_secret=os.environ.get("SESSION_SECRET", ""),
    public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
    admin_name=os.environ.get("ADMIN_NAME", "管理员"),
    admin_phone=os.environ.get("ADMIN_PHONE", ""),
    admin_password=os.environ.get("ADMIN_PASSWORD", ""),
    site_name=_site_name(os.environ.get("SITE_NAME", ""), get_config().site_name),
    # 通用变量优先、DeepSeek 旧名兜底：换兼容 OpenAI 的网关不用改代码。
    llm_api_key=os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""),
    llm_base_url=(
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or "https://api.deepseek.com"
    ),
    # 只认字面量 "enabled"；拼错/留空一律落回 disabled——思考模式开着而 harness 不回传
    # reasoning_content 的话，带 tools 的请求会被上游 400，宁可静默关掉也不能猜着开。
    llm_thinking="enabled"
    if os.environ.get("LLM_THINKING", "").strip().lower() == "enabled"
    else "disabled",
    text_model=os.environ.get("LLM_MODEL") or os.environ.get("TEXT_MODEL", "deepseek-flash"),
    wiki_readonly=os.environ.get("WIKI_READONLY", "1").strip().lower()
    not in ("0", "false", "no", "off"),
    wiki_auto_sync=os.environ.get("WIKI_AUTO_SYNC", "1").strip().lower()
    not in ("0", "false", "no", "off"),
    wiki_git_remote=(os.environ.get("WIKI_REMOTE", "origin").strip() or "origin"),
    wiki_git_branch=(os.environ.get("WIKI_BRANCH", "main").strip() or "main"),
    # 可观测性（S12）：默认全关/本地可读。
    sentry_dsn=os.environ.get("SENTRY_DSN", "").strip(),
    environment=(os.environ.get("ENVIRONMENT", "production").strip() or "production"),
    log_level=_log_level(os.environ.get("LOG_LEVEL", "INFO")),
    release=os.environ.get("RELEASE", "").strip(),
)
