"""serialize：新 frontmatter 形状、round-trip 恒等、引注块、旧 frontmatter 兼容。

S5 起一页是 Hermes vault 里的 `{dir}/{slug}.md`，frontmatter 多了
created/updated/tags/sources/confidence/contested/contradictions；type 白名单换成
entity|concept|comparison|query|summary（旧 topic/outline 只在 allow_legacy 迁移路径认）。
"""

import pytest

from app.wiki.serialize import (
    DIRS,
    TYPES,
    Page,
    PageFormatError,
    dump_md,
    page_path,
    parse_md,
    path_for,
    slugify,
)


def _page(**kwargs) -> Page:
    base = dict(
        slug="baoming", title="报名流程线上化", page_type="entity",
        body_md="上线首月漏单降到 1 单。",
        created="2026-10-23", updated="2026-10-23",
    )
    base.update(kwargs)
    return Page(**base)


def test_round_trip():
    page = _page()
    assert parse_md("baoming", dump_md(page)) == page


def test_round_trip_optional_frontmatter():
    page = _page(
        tags=["增长", "方法论"], sources=["raw/uploads/3-notes.md"],
        confidence="high", contested=True, contradictions=["other-slug"],
    )
    assert parse_md("baoming", dump_md(page)) == page


def test_dump_shape_and_key_order():
    """frontmatter 的键序固定；tags/sources/contradictions 稳定排序输出。"""
    page = _page(
        tags=["b", "a"], sources=["raw/uploads/2-y.md", "raw/uploads/1-x.md"],
        confidence="medium", contested=True, contradictions=["z", "a"],
    )
    text = dump_md(page)
    lines = text.splitlines()
    assert lines[0] == "---"
    keys = [line.split(":", 1)[0] for line in lines[1:] if ":" in line and line != "---"]
    assert keys[:8] == [
        "title", "created", "updated", "type", "tags", "sources", "confidence",
        "contested",
    ]
    assert lines[1] == "title: 报名流程线上化"
    assert lines[2] == "created: 2026-10-23"
    assert lines[3] == "updated: 2026-10-23"
    assert lines[4] == "type: entity"
    assert lines[5] == "tags: [a, b]"
    assert lines[6] == "sources: [raw/uploads/1-x.md, raw/uploads/2-y.md]"
    assert lines[7] == "confidence: medium"
    assert lines[8] == "contested: true"
    assert lines[9] == "contradictions: [a, z]"


def test_optional_keys_absent_when_empty():
    text = dump_md(_page())
    assert "sources: []" in text
    assert "confidence:" not in text
    assert "contested:" not in text
    assert "contradictions:" not in text
    assert "tags: []" in text  # tags 这个键永远在（迁移补的也是空清单）


def test_dump_parse_dump_is_idempotent():
    text = dump_md(_page(tags=["a"], sources=["raw/uploads/1-x.md"]))
    assert dump_md(parse_md("baoming", text)) == text


def test_bad_front_matter_raises():
    with pytest.raises(PageFormatError):
        parse_md("x", "没有 front matter 的正文")


def test_bad_type_raises():
    text = "---\ntitle: T\ntype: novel\n---\n\n正文\n"
    with pytest.raises(PageFormatError):
        parse_md("x", text)


def test_missing_title_raises():
    with pytest.raises(PageFormatError):
        parse_md("x", "---\ntitle:\ntype: entity\n---\n\n正文\n")

def test_path_shape_from_type():
    """PATH 形状：`{dir}/{slug}.md`，由 type 决定目录。"""
    assert DIRS == {
        "entity": "entities", "concept": "concepts", "comparison": "comparisons",
        "query": "queries",
    }
    assert TYPES == ("entity", "concept", "comparison", "query")
    assert page_path(_page()) == "entities/baoming.md"
    assert page_path(_page(page_type="comparison")) == "comparisons/baoming.md"
    assert path_for("x", "query") == "queries/x.md"
    with pytest.raises(PageFormatError):
        page_path(_page(page_type="novel"))
    with pytest.raises(PageFormatError):
        path_for("x", "topic")


def test_created_and_updated_are_read_back_verbatim():
    page = _page(created="2020-01-02", updated="2026-10-23")
    got = parse_md("baoming", dump_md(page))
    assert (got.created, got.updated) == ("2020-01-02", "2026-10-23")


def test_tags_with_spaces_round_trip():
    page = _page(tags=["用户 研究"])
    assert parse_md("baoming", dump_md(page)).tags == ["用户 研究"]


def test_legacy_frontmatter_maps_topic_to_entity():
    """迁移要认旧 frontmatter：只有 title/type，topic → entity，缺的键给空值。"""
    old = "---\ntitle: 报名流程\ntype: topic\n---\n\n正文\n"
    page = parse_md("baoming", old, allow_legacy=True)
    assert page.page_type == "entity"
    assert page.title == "报名流程"
    assert page.body_md == "正文"
    assert page.citations == []
    assert (page.created, page.updated, page.tags, page.sources) == ("", "", [], [])


def test_legacy_type_is_rejected_without_the_flag():
    """allow_legacy 只给迁移用：新写路径上 topic 仍然被闸拒（计划 §1 type 白名单）。"""
    old = "---\ntitle: 报名流程\ntype: topic\n---\n\n正文\n"
    with pytest.raises(PageFormatError):
        parse_md("baoming", old)


def test_legacy_outline_is_parsed_for_the_migration():
    old = "---\ntitle: 大纲\ntype: outline\n---\n\n第一章\n"
    page = parse_md("_outline_", old, allow_legacy=True)
    assert page.page_type == "outline"
    assert page.title == "大纲"


def test_slugify_transliterates_chinese_to_pinyin():
    """2026-10-30 起中文标题不再退化成 page-日期，而是拼音语义 slug。"""
    assert slugify("报名流程线上化") == "bao-ming-liu-cheng-xian-shang-hua"
    assert slugify("技术狂热者：高科技营销的首要目标") == "ji-shu-kuang-re-zhe"
    assert slugify("Checklist v2") == "checklist-v2"
    # 纯符号/空标题才走最后兜底
    assert slugify("✨🎉").startswith("page-")


def test_slugify_never_starts_with_underscore_or_dash():
    """P1-2：前导 `_`/`-` 会产生"写得进盘、GET 404、目录看不见"的页。"""
    for title in ("__init__ 记录", "_私有大纲", "_outline_", "-foo"):
        slug = slugify(title)
        assert slug[:1].isalnum(), (title, slug)
        assert slug[0] not in "-_"
def test_unknown_frontmatter_keys_are_preserved():
    """读侧保留未知键，写侧只输出原生键（迁移后不再把扩展键写回文件）。"""
    text = (
        "---\ntitle: T\ncreated: 2026-10-23\nupdated: 2026-10-23\ntype: entity\n"
        "aliases: [别名]\nstatus: draft\n---\n\n正文\n"
    )
    page = parse_md("x", text)
    assert page.extra == {"aliases": "[别名]", "status": "draft"}
    rewritten = dump_md(page)
    assert "aliases:" not in rewritten and "status:" not in rewritten

def test_yaml_special_titles_and_flow_values_round_trip():
    p = _page(title="Why: evidence #notes", tags=["a, b", "method"], sources=["https://example.com/?q=x,y"])
    text = dump_md(p)
    assert 'title: "Why: evidence #notes"' in text
    got = parse_md(p.slug, text)
    assert got.title == p.title
    assert got.tags == sorted(p.tags)
    assert got.sources == p.sources
