"""Wiki identity and editorial configuration, loaded from ``wiki.toml``.

The wiki engine itself is domain-neutral. Everything a particular knowledge base
needs to say about itself lives in one TOML file at the repository root (or the
path in ``WIKI_CONFIG``):

- the site name and one-line description;
- the default domain/tag hints for new sites (``SCHEMA.md`` is wiki-owned, not generated);
- legacy default tag hints (the live taxonomy is SCHEMA.md);
- human-readable labels for the five fixed page types;
- optional agent persona / extra rules;
- the system prompt used by long-document ingestion;
- the welcome cards shown in the chat empty state.

``SCHEMA.md`` belongs to the wiki, not to this config. It is tracked and synced
like pages; ``store.ensure_vault`` only writes a default skeleton when the file
is missing. Editing ``wiki.toml`` never overwrites it.

No secrets belong in this file. Credentials stay in ``.env``.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("openllmwiki.wiki_config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "wiki.toml"

DEFAULT_SITE_NAME = "知识库"
DEFAULT_DESCRIPTION = "一个由 AI 维护、可自托管的个人知识库"
DEFAULT_DOMAIN = (
    "示例知识库：记录项目资料、研究笔记与决策依据。"
    "事实句要有出处，页面之间用 wikilink 连成网。"
)
DEFAULT_TAGS: tuple[str, ...] = (
    "概念",
    "方法",
    "案例",
    "工具",
    "人物",
    "组织",
    "研究",
    "其他",
)
DEFAULT_TYPE_LABELS: dict[str, str] = {
    "entity": "实体",
    "concept": "概念",
    "comparison": "对比",
    "query": "查询",
}
DEFAULT_AGENT_PERSONA = "你是这个知识库唯一的维护智能体。说话短，先查再写。"
DEFAULT_AGENT_RULES = ""

# Keys are meaningful: ChatWelcome dispatches on them. Unknown keys would render a
# card whose click does nothing, so they are rejected at load time.
ACTION_KEYS = frozenset({"ask", "feed", "lint", "find", "digest"})


class WikiConfigError(ValueError):
    """The wiki config is unreadable or invalid. Startup fails loudly on purpose."""


@dataclass(frozen=True)
class WelcomeAction:
    key: str
    icon: str
    title: str
    example: str

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "icon": self.icon,
            "title": self.title,
            "example": self.example,
        }


DEFAULT_BUILD_ACTIONS: tuple[WelcomeAction, ...] = (
    WelcomeAction("ask", "🔍", "问知识库", "这个知识库主要记录什么？"),
    WelcomeAction("feed", "📄", "整理一份资料", "把 PDF 拖进来，自动整理成页"),
    WelcomeAction("lint", "🩺", "体检知识库", "查断链、重复页、过期页"),
)
DEFAULT_READONLY_ACTIONS: tuple[WelcomeAction, ...] = (
    WelcomeAction("ask", "🔍", "问知识库", "这个知识库主要记录什么？"),
    WelcomeAction("find", "🗂️", "找已有内容", "知识库里有哪些讲这个主题的页面？"),
    WelcomeAction("digest", "📌", "划个重点", "把这个主题的关键内容梳理一下"),
)


def _default_ingest_prompt(site_name: str, domain: str) -> str:
    return (
        f"你是《{site_name}》知识库的编辑。"
        "你只从用户给的材料块里提炼事实页，不补充块外知识，不确定的内容不写。"
        "输出必须是单个 JSON 对象，不要 Markdown、不要解释。\n"
        f"知识库领域：{domain}"
    )


@dataclass(frozen=True)
class WikiConfig:
    site_name: str
    description: str
    domain: str
    tags: tuple[str, ...]
    type_labels: dict[str, str]
    agent_persona: str
    agent_rules: str
    extra_rules: str
    ingest_system_prompt: str
    welcome_build: tuple[WelcomeAction, ...]
    welcome_readonly: tuple[WelcomeAction, ...]
    enabled_plugins: tuple[str, ...]
    plugin_options: dict[str, dict[str, Any]]
    source_path: Path | None

    def as_site_payload(self) -> dict[str, Any]:
        """The public ``GET /api/site`` payload consumed by the web client."""
        return {
            "name": self.site_name,
            "description": self.description,
            "type_labels": dict(self.type_labels),
            "welcome": {
                "build": [action.to_dict() for action in self.welcome_build],
                "readonly": [action.to_dict() for action in self.welcome_readonly],
            },
        }


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WikiConfigError(f"wiki.toml 的 [{key}] 必须是一张表")
    return value


def _string(value: Any, field: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise WikiConfigError(f"wiki.toml 的 {field} 必须是字符串")
    return value.strip()


def _string_list(value: Any, field: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WikiConfigError(f"wiki.toml 的 {field} 必须是字符串数组")
    out: list[str] = []
    for item in value:
        clean = item.strip()
        if clean and clean not in out:
            out.append(clean)
    return tuple(out)


def _rules(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(item.strip() for item in value if item.strip())
    raise WikiConfigError(f"wiki.toml 的 {field} 必须是字符串或字符串数组")


def _actions(value: Any, field: str, default: tuple[WelcomeAction, ...]) -> tuple[WelcomeAction, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not value:
        raise WikiConfigError(f"wiki.toml 的 {field} 必须是非空动作数组")
    out: list[WelcomeAction] = []
    for index, item in enumerate(value):
        where = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise WikiConfigError(f"wiki.toml 的 {where} 必须是一张表")
        key = _string(item.get("key"), f"{where}.key", "")
        if key not in ACTION_KEYS:
            raise WikiConfigError(
                f"wiki.toml 的 {where}.key 不认识：{key!r}；"
                f"只能取 {'、'.join(sorted(ACTION_KEYS))}"
            )
        out.append(
            WelcomeAction(
                key=key,
                icon=_string(item.get("icon"), f"{where}.icon", "•"),
                title=_string(item.get("title"), f"{where}.title", ""),
                example=_string(item.get("example"), f"{where}.example", ""),
            )
        )
    return tuple(out)


def _resolve_path(path: Path | None) -> tuple[Path, bool]:
    """Return (path, explicit). Explicit paths must exist; the default may be absent."""
    if path is not None:
        return path, True
    raw = (os.environ.get("WIKI_CONFIG") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate, True
    return DEFAULT_CONFIG_PATH, False


def _defaults(source: Path | None = None) -> WikiConfig:
    return WikiConfig(
        site_name=DEFAULT_SITE_NAME,
        description=DEFAULT_DESCRIPTION,
        domain=DEFAULT_DOMAIN,
        tags=DEFAULT_TAGS,
        type_labels=dict(DEFAULT_TYPE_LABELS),
        agent_persona=DEFAULT_AGENT_PERSONA,
        agent_rules=DEFAULT_AGENT_RULES,
        extra_rules="",
        ingest_system_prompt=_default_ingest_prompt(DEFAULT_SITE_NAME, DEFAULT_DOMAIN),
        welcome_build=DEFAULT_BUILD_ACTIONS,
        welcome_readonly=DEFAULT_READONLY_ACTIONS,
        enabled_plugins=(),
        plugin_options={},
        source_path=source,
    )


def load_wiki_config(path: Path | None = None) -> WikiConfig:
    """Load and validate ``wiki.toml``; fall back to built-in defaults when absent."""
    resolved, explicit = _resolve_path(path)
    if not resolved.exists():
        if explicit:
            raise WikiConfigError(f"wiki 配置文件不存在：{resolved}")
        return _defaults()
    try:
        raw = resolved.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise WikiConfigError(f"wiki 配置读不出来：{resolved}：{exc}") from exc

    site = _table(data, "site")
    wiki = _table(data, "wiki")
    taxonomy = _table(wiki, "taxonomy") if "taxonomy" in wiki else {}
    type_labels_raw = _table(wiki, "type_labels") if "type_labels" in wiki else {}
    agent = _table(data, "agent")
    ingest = _table(data, "ingest")
    ui = _table(data, "ui")
    plugins = _table(data, "plugins")

    site_name = _string(site.get("name"), "site.name", DEFAULT_SITE_NAME) or DEFAULT_SITE_NAME
    # SITE_NAME is a deployment-level override; it wins over the file.
    env_name = (os.environ.get("SITE_NAME") or "").strip()
    if env_name:
        site_name = env_name
    description = _string(site.get("description"), "site.description", DEFAULT_DESCRIPTION)
    domain = _string(wiki.get("domain"), "wiki.domain", DEFAULT_DOMAIN) or DEFAULT_DOMAIN
    extra_rules = _string(wiki.get("extra_rules"), "wiki.extra_rules", "")

    tags = _string_list(taxonomy.get("tags"), "wiki.taxonomy.tags", DEFAULT_TAGS)

    type_labels = dict(DEFAULT_TYPE_LABELS)
    for key, value in type_labels_raw.items():
        if key not in DEFAULT_TYPE_LABELS:
            logger.warning("wiki.toml 的 wiki.type_labels 有未知页面类型：%s（已忽略）", key)
            continue
        if not isinstance(value, str):
            raise WikiConfigError(f"wiki.toml 的 wiki.type_labels.{key} 必须是字符串")
        clean = value.strip()
        if clean:
            type_labels[key] = clean

    persona = _string(agent.get("persona"), "agent.persona", DEFAULT_AGENT_PERSONA)
    agent_rules = _rules(agent.get("rules"), "agent.rules")
    ingest_prompt = _string(
        ingest.get("system_prompt"),
        "ingest.system_prompt",
        _default_ingest_prompt(site_name, domain),
    ) or _default_ingest_prompt(site_name, domain)

    build_actions = _actions(
        ui.get("build_actions"), "ui.build_actions", DEFAULT_BUILD_ACTIONS
    )
    readonly_actions = _actions(
        ui.get("readonly_actions"), "ui.readonly_actions", DEFAULT_READONLY_ACTIONS
    )

    enabled_plugins = _string_list(plugins.get("enabled"), "plugins.enabled", ())
    plugin_options: dict[str, dict[str, Any]] = {}
    for key, value in plugins.items():
        if key == "enabled":
            continue
        if not isinstance(key, str) or not isinstance(value, dict):
            raise WikiConfigError(f"wiki.toml 的 plugins.{key} 必须是一张表")
        plugin_options[key] = dict(value)
    for name in enabled_plugins:
        if not name.replace("_", "").isalnum() or not name.islower():
            raise WikiConfigError(f"wiki.toml 的插件名不合法：{name!r}")

    return WikiConfig(
        site_name=site_name,
        description=description,
        domain=domain,
        tags=tags,
        type_labels=type_labels,
        agent_persona=persona,
        agent_rules=agent_rules,
        extra_rules=extra_rules,
        ingest_system_prompt=ingest_prompt,
        welcome_build=build_actions,
        welcome_readonly=readonly_actions,
        enabled_plugins=enabled_plugins,
        plugin_options=plugin_options,
        source_path=resolved,
    )


_CONFIG: WikiConfig | None = None


def get_config() -> WikiConfig:
    """Process-wide cached config. Call ``reset_config_cache`` in tests."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_wiki_config()
    return _CONFIG


def reset_config_cache() -> None:
    global _CONFIG
    _CONFIG = None
