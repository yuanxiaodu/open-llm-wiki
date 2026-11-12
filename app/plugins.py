"""Plugin discovery and the small interface a plugin implements.

A plugin is a Python package under the repository's ``plugins/`` directory. It
exposes a module-level ``plugin`` object with:

- ``id`` / ``name`` / ``nav`` — identity and the sidebar entry shown in the SPA;
- ``configure(options)`` — receive the ``[plugins.<id>]`` table from ``app.toml``;
- ``routers()`` — FastAPI routers to mount under ``/api``;
- ``tools()`` — optional agent tools (must also be declared in the skill file);
- ``startup(conn)`` / ``shutdown()`` — optional lifecycle hooks.

Plugins are enabled in ``app.toml``::

    [plugins]
    enabled = ["example"]

    [plugins.example]
    data_dir = "./data/example"

The frontend half is build-time: ``web/src/plugins.ts`` maps a plugin id to its
routes. The backend remains the source of truth for whether the plugin is enabled;
``/api/site`` advertises the enabled plugins and ``App`` only mounts their routes.

This module deliberately has no import-time dependency on a specific plugin. A
broken plugin fails startup loudly instead of silently disappearing.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter

from .wiki_config import get_config

logger = logging.getLogger("openllmwiki.plugins")

NAV_KEYS = ("key", "to", "label", "short")


@dataclass(frozen=True)
class PluginInfo:
    id: str
    name: str
    nav: dict[str, Any]
    instance: Any

    def configure(self, options: dict[str, Any]) -> None:
        configure = getattr(self.instance, "configure", None)
        if callable(configure):
            configure(options)

    def routers(self) -> list[APIRouter]:
        routers = getattr(self.instance, "routers", None)
        value = routers() if callable(routers) else []
        if not isinstance(value, list):
            raise RuntimeError(f"插件 {self.id} 的 routers() 必须返回 list")
        return value

    def tools(self) -> list[Any]:
        tools = getattr(self.instance, "tools", None)
        value = tools() if callable(tools) else []
        if not isinstance(value, list):
            raise RuntimeError(f"插件 {self.id} 的 tools() 必须返回 list")
        return value

    def startup(self, conn) -> None:
        hook = getattr(self.instance, "startup", None)
        if callable(hook):
            hook(conn)

    def shutdown(self) -> None:
        hook = getattr(self.instance, "shutdown", None)
        if callable(hook):
            hook()

    def site_payload(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "nav": dict(self.nav)}


_PLUGINS: list[PluginInfo] | None = None


def _load_one(name: str, options: dict[str, Any]) -> PluginInfo:
    try:
        module = importlib.import_module(f"plugins.{name}")
    except Exception as exc:  # noqa: BLE001 - startup should surface the real cause
        raise RuntimeError(f"插件 {name} 加载失败：{exc}") from exc
    instance = getattr(module, "plugin", None)
    if instance is None:
        raise RuntimeError(f"插件 {name} 没有导出 plugin 对象")
    plugin_id = getattr(instance, "id", "")
    if plugin_id != name:
        raise RuntimeError(f"插件 {name} 的 id 是 {plugin_id!r}，必须与包名一致")
    title = getattr(instance, "name", "")
    if not isinstance(title, str) or not title.strip():
        raise RuntimeError(f"插件 {name} 缺 name")
    nav = getattr(instance, "nav", None)
    if not isinstance(nav, dict):
        raise RuntimeError(f"插件 {name} 缺 nav")
    for key in NAV_KEYS:
        if not isinstance(nav.get(key), str) or not nav[key].strip():
            raise RuntimeError(f"插件 {name} 的 nav.{key} 必须是非空字符串")
    info = PluginInfo(id=plugin_id, name=title.strip(), nav=dict(nav), instance=instance)
    info.configure(options)
    logger.info("已加载插件 %s（%s）", info.id, info.name)
    return info


def get_plugins() -> list[PluginInfo]:
    """Load enabled plugins once per process; config changes require a restart."""
    global _PLUGINS
    if _PLUGINS is None:
        config = get_config()
        loaded: list[PluginInfo] = []
        for name in config.enabled_plugins:
            loaded.append(_load_one(name, config.plugin_options.get(name, {})))
        _PLUGINS = loaded
    return _PLUGINS


def reset_plugins_cache() -> None:
    global _PLUGINS
    _PLUGINS = None


def plugin_site_payload() -> list[dict[str, Any]]:
    return [plugin.site_payload() for plugin in get_plugins()]
