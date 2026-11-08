"""WIKI_DIR: the wiki vault is a separate Git repository from the project repo."""

from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT, _resolve_optional, settings


def test_resolve_optional_empty_and_relative():
    assert _resolve_optional("") is None
    assert _resolve_optional("   ") is None
    assert _resolve_optional("data/wiki") == (PROJECT_ROOT / "data/wiki").resolve()
    absolute = Path("/tmp/some-vault")
    assert _resolve_optional(str(absolute)) == absolute


def test_wiki_dir_defaults_to_data_dir(tmp_path, monkeypatch):
    original = settings.data_dir
    try:
        object.__setattr__(settings, "data_dir", tmp_path)
        object.__setattr__(settings, "wiki_dir_override", None)
        assert settings.wiki_dir == tmp_path / "wiki"
    finally:
        object.__setattr__(settings, "data_dir", original)


def test_wiki_dir_override_uses_independent_vault(tmp_path):
    original = settings.wiki_dir_override
    vault = tmp_path / "obsidian-vault"
    vault.mkdir()
    try:
        object.__setattr__(settings, "wiki_dir_override", vault)
        assert settings.wiki_dir == vault
    finally:
        object.__setattr__(settings, "wiki_dir_override", original)


def test_ensure_repo_and_vault_use_wiki_dir_override(tmp_path):
    from app.wiki import repo, store

    original = settings.wiki_dir_override
    vault = tmp_path / "obsidian-vault"
    try:
        object.__setattr__(settings, "wiki_dir_override", vault)
        repo.ensure_repo()
        assert (vault / ".git").is_dir()
        store.ensure_vault()
        for name in ("SCHEMA.md", "index.md", "log.md"):
            assert (vault / name).is_file(), name
        # 项目仓库不跟踪这个目录：它只是 tmp 下的另一个仓库
        assert (vault / ".git").is_dir()
    finally:
        object.__setattr__(settings, "wiki_dir_override", original)
