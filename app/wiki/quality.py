"""Evidence breadth is distinct from claim confidence. Defaults never assert high."""
from __future__ import annotations

from .provenance import independent_ids

LEVELS = ("high", "medium", "low")


def independent_sources(sources) -> set[str]:
    from ..config import settings
    return independent_ids(sources, settings.wiki_dir)


def default_confidence(sources) -> str:
    return "medium" if sources else "low"


def normalize_confidence(value: str | None, *, field: str = "confidence") -> str:
    level = (value or "").strip().lower()
    if level == "" or level in LEVELS:
        return level
    raise ValueError(f"{field} 只能是 high / medium / low（收到 {value!r}）")


def confidence_for(sources, value: str | None = None, *, contested: bool = False) -> str:
    level = normalize_confidence(value) or default_confidence(sources)
    if level == "high" and (contested or len(independent_sources(sources)) < 2):
        return "medium" if sources else "low"
    return level
