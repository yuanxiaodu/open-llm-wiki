"""Source identity and integrity helpers; no network calls or database dependency."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def split_raw(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            # Writers insert a single blank separator; it is not source content.
            body = "".join(lines[index + 1:])
            if body.startswith("\n"):
                body = body[1:]
            return meta, body
        key, sep, value = line.partition(":")
        if sep:
            value = value.strip()
            if value.startswith('"'):
                try: value = json.loads(value)
                except ValueError: pass
            meta[key.strip()] = str(value)
    return {}, text


def canonical_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
        port = parts.port
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port and (parts.scheme.lower(), port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def source_file(root: Path, source: str) -> Path | None:
    if canonical_url(source):
        return None
    try:
        target = (root / source).resolve()
        if target.is_relative_to(root.resolve()) and target.is_file():
            return target
    except (OSError, ValueError, RuntimeError):
        pass
    return None


@lru_cache(maxsize=128)
def _source_info(path: Path, modified: int, changed: int, size: int):
    meta, body = split_raw(path.read_text(encoding="utf-8"))
    return meta, content_hash(body)


def source_info(path: Path) -> tuple[dict[str, str], str]:
    stat = path.stat()
    meta, digest = _source_info(path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    return dict(meta), digest


def independent_ids(sources, root: Path) -> set[str]:
    """Collapse aliases by URL, original bytes, exact text and declared derivation.

    This only measures distinct evidence identities, never proves agreement or truth.
    """
    parents = {}
    def find(value):
        parents.setdefault(value, value)
        if parents[value] != value:
            parents[value] = find(parents[value])
        return parents[value]
    def join(tokens):
        tokens = sorted(set(tokens))
        if not tokens: return None
        first = find(tokens[0])
        for token in tokens[1:]: parents[find(token)] = first
        return first
    def identity(source: str, seen: set[str]):
        if source in seen: return set()
        target = source_file(root, source)
        if target is None:
            url = canonical_url(source)
            return {join(["url:" + url])} if url else set()
        try:
            meta, digest = source_info(target)
        except (OSError, UnicodeError, ValueError):
            return set()
        file_token = "file:" + source
        derived = meta.get("derived_from", "")
        if derived:
            try: values = json.loads(derived)
            except ValueError: values = [derived]
            if isinstance(values, str): values = [values]
            if isinstance(values, list):
                return set().union(*(identity(str(value), seen | {source}) for value in values))
        tokens = [file_token, "content:" + digest]
        url = canonical_url(meta.get("source_url", ""))
        if url: tokens.append("url:" + url)
        original = meta.get("original_sha256")
        if not original and not meta.get("content_sha256"):
            original = meta.get("sha256")
        if original: tokens.append("original:" + original)
        return {join(tokens)}
    identities = set().union(*(identity(str(source), set()) for source in (sources or [])))
    return {find(value) for value in identities if value}
