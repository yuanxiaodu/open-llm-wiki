"""Plugin discovery contract: identity/nav validation, lifecycle, site payload."""

from __future__ import annotations

import types

import pytest

from app import plugins as plugins_module
from app.plugins import PluginInfo, _load_one, get_plugins, reset_plugins_cache
from app.wiki_config import load_wiki_config


class FakePlugin:
    id = "demo"
    name = "示例插件"
    nav = {"key": "demo", "to": "/demo", "label": "示例", "short": "例"}

    def __init__(self) -> None:
        self.options = None
        self.started = False
        self.stopped = False

    def configure(self, options):
        self.options = options

    def routers(self):
        return []

    def tools(self):
        return []

    def startup(self, conn):
        self.started = True

    def shutdown(self):
        self.stopped = True


def _module(plugin):
    module = types.SimpleNamespace(plugin=plugin)
    return module


def test_load_one_validates_identity_and_configures(monkeypatch):
    fake = FakePlugin()
    monkeypatch.setattr(
        plugins_module.importlib, "import_module", lambda name: _module(fake)
    )
    info = _load_one("demo", {"data_dir": "./demo"})
    assert info.id == "demo" and info.name == "示例插件"
    assert fake.options == {"data_dir": "./demo"}
    assert info.site_payload() == {
        "id": "demo",
        "name": "示例插件",
        "nav": {"key": "demo", "to": "/demo", "label": "示例", "short": "例"},
    }


def test_load_one_rejects_bad_id_name_and_nav(monkeypatch):
    for attr, value, message in (
        ("id", "other", "id"),
        ("name", "", "name"),
        ("nav", None, "nav"),
        ("nav", {"key": "x"}, "nav.to"),
    ):
        fake = FakePlugin()
        setattr(fake, attr, value)
        monkeypatch.setattr(
            plugins_module.importlib, "import_module", lambda name: _module(fake)
        )
        with pytest.raises(RuntimeError, match=message):
            _load_one("demo", {})


def test_load_one_wraps_import_failure(monkeypatch):
    def boom(name):
        raise ImportError("没有这个包")

    monkeypatch.setattr(plugins_module.importlib, "import_module", boom)
    with pytest.raises(RuntimeError, match="加载失败"):
        _load_one("missing", {})


def test_plugin_lifecycle_and_site_payload(monkeypatch):
    fake = FakePlugin()
    info = PluginInfo(id="demo", name="示例插件", nav=fake.nav, instance=fake)
    info.startup(object())
    info.shutdown()
    assert fake.started and fake.stopped
    assert info.tools() == [] and info.routers() == []


def test_get_plugins_honours_enabled_config(monkeypatch, tmp_path):
    (tmp_path / "app.toml").write_text(
        '[plugins]\nenabled = ["demo"]\n\n[plugins.demo]\ndata_dir = "./demo"\n',
        encoding="utf-8",
    )
    config = load_wiki_config(tmp_path / "app.toml")
    assert config.enabled_plugins == ("demo",)
    assert config.plugin_options == {"demo": {"data_dir": "./demo"}}

    fake = FakePlugin()
    monkeypatch.setattr(plugins_module, "get_config", lambda: config)
    monkeypatch.setattr(
        plugins_module.importlib, "import_module", lambda name: _module(fake)
    )
    reset_plugins_cache()
    try:
        loaded = get_plugins()
        assert [p.id for p in loaded] == ["demo"]
        assert loaded[0].instance.options == {"data_dir": "./demo"}
        # second call is cached
        assert get_plugins() is loaded
    finally:
        reset_plugins_cache()


def test_site_router_includes_enabled_plugins(monkeypatch):
    from app.routers.api import site as site_router

    monkeypatch.setattr(
        site_router,
        "plugin_site_payload",
        lambda: [{"id": "demo", "name": "示例", "nav": {"key": "demo", "to": "/demo", "label": "示例", "short": "例"}}],
    )
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/site").json()
    assert body["plugins"][0]["id"] == "demo"
