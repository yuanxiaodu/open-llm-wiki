"""Publication contract for reusable analyses. Metadata stays in the Markdown artifact."""
from __future__ import annotations

import json
from datetime import date

from pydantic import BaseModel, Field

from .provenance import canonical_url, source_file


class ParagraphEvidence(BaseModel):
    paragraph: str
    sources: list[str]


class BasisVersion(BaseModel):
    path: str
    head: str


class AnalysisSpec(BaseModel):
    question: str = Field(default="", max_length=1000)
    scope: str = Field(default="", max_length=1000)
    as_of: str = ""
    basis_paths: list[str] = Field(default_factory=list, max_length=20)
    basis_versions: list[BasisVersion] = Field(default_factory=list, max_length=20)
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    uncertainties: str = Field(default="", max_length=2000)
    # Paragraph label -> raw paths; lightweight provenance, no mandatory quote checking.
    paragraph_sources: list[ParagraphEvidence] = Field(default_factory=list)


def prepare(conn, page_type: str, body: str, sources, spec: AnalysisSpec | None,
            status: str, *, previous: dict | None = None, author_id: int | None = None):
    from ..config import settings
    from . import store

    if status not in ("draft", "published"):
        raise ValueError("status 只能是 draft / published")
    metadata = dict((previous or {}).get("metadata") or {})
    if spec is None and metadata.get("analysis"):
        spec = AnalysisSpec.model_validate_json(metadata["analysis"])
    spec = spec or AnalysisSpec()
    inherited = set(sources or [])
    basis = []
    problems = []
    expected_versions = {item.path: item.head for item in spec.basis_versions}
    for path in spec.basis_paths:
        page = store.get_page(conn, path)
        if page is None:
            problems.append(f"依据页不存在：{path}")
            continue
        if page.get("metadata", {}).get("status") == "draft":
            problems.append(f"依据页尚为草稿：{path}")
        expected = expected_versions.get(path) or expected_versions.get(page["path"])
        if not expected or not store._base_matches_head(expected, page["head"]):
            problems.append(f"依据版本缺失或已变化：{path}，重新 read 后填 basis_versions")
        inherited.update(page["sources"])
        basis.append({"path": page["path"], "head": page["head"]})
    if not spec.question.strip(): problems.append("缺原始问题")
    if not spec.scope.strip(): problems.append("缺适用条件")
    if not spec.uncertainties.strip(): problems.append("缺不确定性说明（可明确写未发现）")
    if not basis: problems.append("缺可追溯的依据页")
    if not inherited: problems.append("缺原始来源")
    if spec.as_of:
        try: date.fromisoformat(spec.as_of)
        except ValueError: problems.append("as_of 必须为 YYYY-MM-DD")
    else:
        problems.append("缺分析时间 as_of")
    if page_type == "comparison":
        if len(set(spec.dimensions)) < 2: problems.append("比较页至少需要两个统一比较维度")
        if "|" not in body: problems.append("比较页需包含比较表格，未知项明确标注")
    pages = store.all_pages()
    resolve = store.link_resolver(pages)
    links = {resolve(t) for t, _ in store.iter_wikilinks(body)} - {None}
    if len(links) < 2: problems.append("至少链接两个真实依据页")
    for source in inherited:
        if not canonical_url(source) and source_file(settings.wiki_dir, source) is None:
            problems.append(f"来源不存在：{source}")
    mapped = {s for item in spec.paragraph_sources for s in item.sources}
    if mapped - inherited: problems.append("段落来源必须属于本页 sources")
    if len(inherited) >= 3 and not spec.paragraph_sources:
        problems.append("多来源综合缺段落来源映射 paragraph_sources")
    if status == "published" and problems:
        raise ValueError("发布条件未满足：" + "；".join(problems))
    metadata.update(status=status, analysis=spec.model_dump_json(),
                    basis_versions=json.dumps(basis, ensure_ascii=False),
                    review_notes=json.dumps(problems, ensure_ascii=False))
    if "author_id" not in metadata and author_id is not None:
        metadata["author_id"] = str(author_id)
    return sorted(inherited), metadata
