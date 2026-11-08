// 知识库（Hermes 物理 vault）纯函数的钉子。
//
// 这一层不碰 DOM/React，测试就是"给定输入，人话/形状对不对"。最容易写错的几条：
//   - 旧 S4b 平铺形状 → 新物理 path 的降级映射（后端切换期不能白屏）；
//   - 物理 path ↔ URL 的逐段编码（path 带 `/`，不能整串 encodeURIComponent）；
//   - wikilink 断链必须退成普通文字（不能摆死链），`[[cite:*]]` 不能被误当 wikilink；
//   - 物理目录树的五类固定顺序、未知目录、raw 过滤、嵌套目录计数；
//   - updated_at 混着 +08:00 / +00:00，排序必须 parse 不能切字符串。
import { describe, expect, it } from "vitest";

import {
  WIKI_INDEX_PATH,
  buildWikiTree,
  confidenceHint,
  confidenceLabel,
  dirLabel,
  findWikiPage,
  formatStamp,
  headingSlug,
  isSpecialWikiPath,
  isWikiIndexPath,
  latestStamp,
  normalizeConfidence,
  normalizeHeadings,
  normalizeTags,
  normalizeWikiDetail,
  normalizeWikiIndex,
  normalizeWikiList,
  normalizeWikiUrlPath,
  pageTypeLabel,
  parseOutlineHeadings,
  pathStem,
  prepareWikiBody,
  replaceWikiLinks,
  safeMarkdownHref,
  wikiApiUrl,
  wikiPageHref,
  wikiUrlPathFromPathname,
  wikilinksFromPages,
} from "./wiki.ts";
import type { WikiLink, WikiPageMeta } from "./wiki.ts";

function page(over: Partial<WikiPageMeta> & { path: string }): WikiPageMeta {
  return {
    path: over.path,
    slug: over.slug ?? pathStem(over.path),
    title: over.title ?? `页 ${pathStem(over.path)}`,
    page_type: over.page_type ?? "entity",
    tags: over.tags ?? [],
    summary: over.summary ?? "",
    created_at: over.created_at ?? "2026-10-20T00:00:00+00:00",
    updated_at: over.updated_at ?? "2026-10-20T00:00:00+00:00",
    actor: over.actor ?? "张三",
  };
}

describe("normalizeWikiList：新旧形状都能吃", () => {
  it("新形状 {pages,count} 原样归一化", () => {
    const got = normalizeWikiList({
      pages: [
        {
          path: "entities/foo.md",
          slug: "foo",
          title: "福",
          page_type: "entity",
          tags: ["人物", " 人物 ", "创业"],
          summary: "一句话",
          created_at: "2026-10-01T00:00:00+00:00",
          updated_at: "2026-10-02T00:00:00+00:00",
          actor: "张三",
        },
      ],
      count: 7,
    });
    expect(got.count).toBe(7);
    expect(got.pages).toHaveLength(1);
    expect(got.pages[0]).toMatchObject({
      path: "entities/foo.md",
      slug: "foo",
      title: "福",
      page_type: "entity",
      summary: "一句话",
      actor: "张三",
    });
    expect(got.pages[0].tags).toEqual(["人物", "创业"]); // 去空去重
  });

  it("旧 S4b 平铺数组 → 按旧 type 映射到物理目录（topic→entities 等）", () => {
    const got = normalizeWikiList([
      { slug: "a", title: "A", page_type: "topic", updated_at: "2026-10-01T00:00:00+00:00" },
      { slug: "b", title: "B", page_type: "concept" },
      { slug: "c", title: "C", page_type: "comparison" },
      { slug: "d", title: "D", page_type: "query" },
    ]);
    expect(got.pages.map((p) => p.path)).toEqual([
      "entities/a.md",
      "concepts/b.md",
      "comparisons/c.md",
      "queries/d.md",
    ]);
    expect(got.count).toBe(4);
  });

  it("旧大纲页丢掉（书已独立），raw/元文件也过滤", () => {
    const got = normalizeWikiList([
      { slug: "_outline_", title: "大纲", page_type: "outline" },
      { slug: "x", title: "X", page_type: "topic" },
      { path: "raw/uploads/1-a.md", title: "raw" },
      { path: "index.md" },
      { path: "SCHEMA.md" },
      { path: "log.md" },
    ]);
    expect(got.pages.map((p) => p.path)).toEqual(["entities/x.md"]);
  });

  it("count 缺失按数组长度；乱七八糟的形状 → 空列表", () => {
    expect(normalizeWikiList({ pages: [{ path: "entities/a.md" }] }).count).toBe(1);
    expect(normalizeWikiList(null)).toEqual({ pages: [], count: 0 });
    expect(normalizeWikiList("nope")).toEqual({ pages: [], count: 0 });
    expect(normalizeWikiList({ pages: [{ no: "path" }] }).pages).toEqual([]);
  });

  it("title 空就用文件名；tags 字符串也认；不改入参", () => {
    const raw = { pages: [{ path: "entities/no-title.md", title: "  ", tags: "甲, 乙 甲" }] };
    const before = JSON.stringify(raw);
    const got = normalizeWikiList(raw);
    expect(got.pages[0].title).toBe("no-title");
    expect(got.pages[0].tags).toEqual(["甲", "乙"]);
    expect(JSON.stringify(raw)).toBe(before);
  });
});

describe("normalizeWikiDetail / normalizeWikiIndex：缺字段给空数组", () => {
  it("详情：wikilinks/backlinks/tags/sources/citations 全缺也不炸", () => {
    const got = normalizeWikiDetail({
      path: "concepts/x.md",
      title: "X",
      page_type: "concept",
      body_md: "正文",
      head: "abc1234",
    });
    expect(got).toMatchObject({
      path: "concepts/x.md",
      slug: "x",
      title: "X",
      page_type: "concept",
      body_md: "正文",
      head: "abc1234",
      tags: [],
      sources: [],
      wikilinks: [],
      backlinks: [],
    });
    expect(got.updated_at).toBeNull();
  });

  it("详情：wikilinks 断链 path=null、backlinks 标题兜底", () => {
    const got = normalizeWikiDetail({
      path: "entities/a.md",
      title: "A",
      page_type: "entity",
      body_md: "",
      wikilinks: [
        { target: "b", label: "B 页", path: "entities/b.md" },
        { target: "gone", label: "没了", path: null },
      ],
      backlinks: [{ path: "concepts/c.md", title: "" }],
      tags: ["x"],
    });
    expect(got.wikilinks).toEqual([
      { target: "b", label: "B 页", path: "entities/b.md" },
      { target: "gone", label: "没了", path: null },
    ]);
    expect(got.backlinks).toEqual([{ path: "concepts/c.md", title: "c" }]);
    expect(got.tags).toEqual(["x"]);
  });

  it("索引：body_md/updated_at 归一化，path 缺省 index.md", () => {
    expect(normalizeWikiIndex({ body_md: "# 索引" })).toEqual({
      path: "index.md",
      body_md: "# 索引",
      updated_at: null,
    });
    expect(normalizeWikiIndex(null).body_md).toBe("");
  });
});

describe("path/URL/路由小工具", () => {
  it("pathStem / dirLabel", () => {
    expect(pathStem("entities/人物/foo.md")).toBe("foo");
    expect(pathStem("foo.md")).toBe("foo");
    expect(dirLabel("entities")).toBe("实体");
    expect(dirLabel("concepts")).toBe("概念");
    expect(dirLabel("comparisons")).toBe("对比");
    expect(dirLabel("queries")).toBe("查询");
    expect(dirLabel("summaries")).toBe("摘要");
    expect(dirLabel("misc")).toBe("misc");
  });

  it("wikiPageHref：去 .md、逐段编码、空 path 是 /wiki", () => {
    expect(wikiPageHref("entities/foo.md")).toBe("/wiki/entities/foo");
    expect(wikiPageHref("/concepts/a b.md")).toBe("/wiki/concepts/a%20b");
    expect(wikiPageHref("entities/人物/阿明.md")).toBe(
      "/wiki/entities/%E4%BA%BA%E7%89%A9/%E9%98%BF%E6%98%8E",
    );
    expect(wikiPageHref("")).toBe("/wiki");
  });

  it("wikiApiUrl：空 path 打 index 端点，其余逐段编码", () => {
    expect(wikiApiUrl("")).toBe("/api/wiki/index");
    expect(wikiApiUrl("entities/foo.md")).toBe("/api/wiki/entities/foo.md");
    expect(wikiApiUrl("entities/a b.md")).toBe("/api/wiki/entities/a%20b.md");
  });

  it("normalizeWikiUrlPath：去前缀斜杠、逐段解码、坏编码不炸", () => {
    expect(normalizeWikiUrlPath("/entities/foo.md")).toBe("entities/foo.md");
    expect(normalizeWikiUrlPath("/entities/%E4%BA%BA%E7%89%A9/")).toBe("entities/人物");
    expect(normalizeWikiUrlPath("%E0%A4%A")).toBe("%E0%A4%A");
  });

  it("wikiUrlPathFromPathname / isWikiIndexPath：/wiki 是索引，别的模块不算", () => {
    expect(wikiUrlPathFromPathname("/wiki")).toBe(WIKI_INDEX_PATH);
    expect(wikiUrlPathFromPathname("/wiki/")).toBe(WIKI_INDEX_PATH);
    expect(wikiUrlPathFromPathname("/wiki/entities/foo")).toBe("entities/foo");
    expect(wikiUrlPathFromPathname("/wiki/entities/%E4%BA%BA%E7%89%A9")).toBe(
      "entities/人物",
    );
    expect(wikiUrlPathFromPathname("/demo/x")).toBe("");
    expect(isWikiIndexPath("/wiki/")).toBe(true);
    expect(isWikiIndexPath("/wiki/entities/foo")).toBe(false);
  });

  it("isSpecialWikiPath：raw 与元文件不进树", () => {
    for (const p of ["raw/uploads/1.md", "index.md", "SCHEMA.md", "log.md"]) {
      expect(isSpecialWikiPath(p)).toBe(true);
    }
    for (const p of ["entities/foo.md", "queries/q.md"]) {
      expect(isSpecialWikiPath(p)).toBe(false);
    }
  });

  it("findWikiPage：按去 .md 的 path 找，大小写不敏感", () => {
    const pages = [page({ path: "entities/Foo.md", slug: "foo", title: "Foo" })];
    expect(findWikiPage(pages, "entities/foo")?.path).toBe("entities/Foo.md");
    expect(findWikiPage(pages, "entities/nope")).toBeNull();
    expect(findWikiPage(pages, "")).toBeNull();
  });
});

describe("pageTypeLabel / normalizeTags", () => {
  it.each([
    ["entity", "实体"],
    ["concept", "概念"],
    ["comparison", "对比"],
    ["query", "查询"],
    ["summary", "摘要"],
    ["topic", "主题"],
    ["outline", "其他"],
    ["mystery", "其他"],
    ["", "其他"],
  ] as const)("%s → %s", (type, label) => {
    expect(pageTypeLabel(type)).toBe(label);
  });

  it("normalizeTags 去空去重，字符串按逗号/空白切", () => {
    expect(normalizeTags(["a", " a ", "", 3])).toEqual(["a", "3"]);
    expect(normalizeTags("甲, 乙，丙 甲")).toEqual(["甲", "乙", "丙"]);
    expect(normalizeTags(undefined)).toEqual([]);
  });
});

describe("buildWikiTree：物理目录树", () => {
  const pages: WikiPageMeta[] = [
    page({ path: "entities/foo.md", slug: "foo", title: "实体甲" }),
    page({ path: "entities/人物/bar.md", slug: "bar", title: "实体乙" }),
    page({ path: "concepts/baz.md", slug: "baz", title: "概念丙", page_type: "concept" }),
    page({ path: "comparisons/c1.md", slug: "c1", title: "对比丁", page_type: "comparison" }),
    page({ path: "queries/q1.md", slug: "q1", title: "查询戊", page_type: "query" }),
    page({ path: "summaries/s1.md", slug: "s1", title: "摘要己", page_type: "summary" }),
    page({ path: "misc/x.md", slug: "x", title: "杂项" }),
    page({ path: "root-page.md", slug: "root-page", title: "根页" }),
  ];

  it("顶层永远是「索引」文件，下面按五类固定顺序，未知目录靠后", () => {
    const tree = buildWikiTree(pages);
    expect(tree[0]).toMatchObject({ kind: "file", label: "索引", href: "/wiki", path: "" });
    expect(tree.slice(1).map((n) => n.label)).toEqual([
      "实体",
      "概念",
      "对比",
      "查询",
      "摘要",
      "misc",
      "根页",
    ]);
    expect(tree[1].kind).toBe("folder");
    // 根目录的散页挂在最后（正常 vault 不该有，但旧数据可能）
    expect(tree[tree.length - 1]).toMatchObject({ kind: "file", label: "根页" });
  });

  it("目录按 path 真实层级递归，计数是叶子页数", () => {
    const tree = buildWikiTree(pages);
    const entities = tree[1];
    expect(entities.count).toBe(2);
    expect(entities.children?.map((n) => n.label)).toEqual(["人物", "实体甲"]);
    const people = entities.children?.[0];
    expect(people).toMatchObject({ kind: "folder", label: "人物", count: 1 });
    expect(people?.children?.[0]).toMatchObject({
      kind: "file",
      label: "实体乙",
      href: "/wiki/entities/%E4%BA%BA%E7%89%A9/bar",
      path: "entities/人物/bar.md",
      type: "entity",
    });
  });

  it("同目录文件保持后端顺序；空列表只剩索引；raw/元文件不出现", () => {
    const tree = buildWikiTree([
      page({ path: "entities/z.md", slug: "z", title: "Z" }),
      page({ path: "entities/a.md", slug: "a", title: "A" }),
      page({ path: "raw/uploads/1.md", title: "raw" }),
      page({ path: "index.md", title: "index" }),
    ]);
    expect(tree[1].children?.map((n) => n.label)).toEqual(["Z", "A"]);
    expect(buildWikiTree([])).toEqual([
      { kind: "file", label: "索引", href: "/wiki", path: "", type: "index" },
    ]);
  });

  it("不改入参", () => {
    const input = [...pages];
    buildWikiTree(input);
    expect(input).toEqual(pages);
  });
});

describe("wikilink 解析/替换", () => {
  const links: WikiLink[] = [
    { target: "foo", label: "实体甲", path: "entities/foo.md" },
    { target: "概念丙", label: "概念丙", path: "concepts/baz.md" },
    { target: "gone", label: "没了", path: null },
  ];

  it("有 path → markdown 站内链接；断链 → 纯文字（不摆死链）", () => {
    expect(replaceWikiLinks("见 [[foo]] 与 [[gone]]。", links)).toBe(
      "见 [实体甲](/wiki/entities/foo) 与 没了。",
    );
  });

  it("[[target|显示文字]] 用显式 label；按 title 也能解析", () => {
    expect(replaceWikiLinks("[[foo|另一个名字]]", links)).toBe("[另一个名字](/wiki/entities/foo)");
    expect(replaceWikiLinks("[[概念丙]]", links)).toBe("[概念丙](/wiki/concepts/baz)");
  });

  it("没有映射/后端还没给 wikilinks：退成普通文字（不显示方括号）", () => {
    expect(replaceWikiLinks("见 [[foo]]。", [])).toBe("见 foo。");
    expect(replaceWikiLinks("见 [[foo|显示]]。", null)).toBe("见 显示。");
  });

  it("label 里的 markdown 标点会被转义，不会破版", () => {
    // 注意：wikilink 的 label 不能含 `]`（那是标记的结束符），这里验的是星号/下划线/反引号
    expect(replaceWikiLinks("[[foo|重要 甲*]]", links)).toBe(
      "[重要 甲\\*](/wiki/entities/foo)",
    );
  });

  it("prepareWikiBody：wikilink 换成站内链接，断链退成文字", () => {
    expect(prepareWikiBody("事实，见 [[foo]]。", links)).toBe(
      "事实，见 [实体甲](/wiki/entities/foo)。",
    );
  });
  it("wikilinksFromPages：slug 与 title 都能当 target", () => {
    const got = wikilinksFromPages([
      page({ path: "entities/foo.md", slug: "foo", title: "实体甲" }),
    ]);
    expect(got).toEqual([
      { target: "foo", label: "实体甲", path: "entities/foo.md" },
      { target: "实体甲", label: "实体甲", path: "entities/foo.md" },
    ]);
  });
});

describe("引注角标 / 链接白名单", () => {
  it("safeMarkdownHref：站内/hash/http(s) 放行，javascript:/data: 清空", () => {
    for (const ok of ["/wiki/x", "#cite-c1", "./a.md", "../b", "https://x.test", "mailto:a@b.test"]) {
      expect(safeMarkdownHref(ok)).toBe(ok);
    }
    expect(safeMarkdownHref("javascript:alert(1)")).toBe("");
    expect(safeMarkdownHref("data:text/html,x")).toBe("");
    expect(safeMarkdownHref("")).toBe("");
  });
});

describe("标题锚点", () => {
  it("parseOutlineHeadings：层级/中文/去标点/重名/代码围栏", () => {
    const hs = parseOutlineHeadings(
      ["# 第一章", "", "## 1.1 起头", "```", "# 代码里的", "```", "## 1.1 起头"].join("\n"),
    );
    expect(hs.map((h) => h.level)).toEqual([1, 2, 2]);
    expect(hs.map((h) => h.text)).toEqual(["第一章", "1.1 起头", "1.1 起头"]);
    expect(hs.map((h) => h.id)).toEqual(["第一章", "11-起头", "11-起头-2"]);
  });

  it("headingSlug 中文保留、标点去掉、空兜底 section", () => {
    expect(headingSlug("Hello, World! 你好，世界")).toBe("hello-world-你好世界");
    expect(headingSlug("!!!")).toBe("section");
  });

  it("normalizeHeadings：后端给了就用；缺 id 用 headingSlug；没有就从正文解析", () => {
    expect(normalizeHeadings([{ level: 2, text: "小节", id: "sec" }], "## 正文标题")).toEqual([
      { level: 2, text: "小节", id: "sec" },
    ]);
    expect(normalizeHeadings([{ level: 2, text: "小节" }], "")).toEqual([
      { level: 2, text: "小节", id: "小节" },
    ]);
    expect(normalizeHeadings(undefined, "## 正文标题")).toEqual([
      { level: 2, text: "正文标题", id: "正文标题" },
    ]);
  });
});

describe("formatStamp / latestStamp", () => {
  it("同一时刻的两种时区写法给出同一个结果（parse 而不是切字符串）", () => {
    const a = formatStamp("2026-10-23T23:30:00+08:00");
    expect(a).not.toBe("");
    expect(a).toBe(formatStamp("2026-10-23T15:30:00+00:00"));
    expect(a).toMatch(/^2026\/10\/23 \d{2}:\d{2}$/);
  });

  it("空/坏值 → 空串", () => {
    for (const v of [null, undefined, "", "不是时间"]) expect(formatStamp(v)).toBe("");
    expect(formatStamp("2026-10-23")).toBe("2026/10/23");
  });

  it("latestStamp：按真实时刻取最新，坏值跳过", () => {
    expect(
      latestStamp([
        "2026-10-23T10:00:00+08:00", // 02:00Z
        "2026-10-23T03:00:00+00:00", // 03:00Z（最新）
        null,
        "坏",
      ]),
    ).toBe("2026-10-23T03:00:00+00:00");
    expect(latestStamp([null, ""])).toBe("");
  });
});


describe("confidence：三档归一 + 中文标签（2026-10-31 体检）", () => {
  it("只认 high/medium/low，大小写与空白归一，其余当没填", () => {
    expect(normalizeConfidence("high")).toBe("high");
    expect(normalizeConfidence(" HIGH ")).toBe("high");
    expect(normalizeConfidence("Medium")).toBe("medium");
    expect(normalizeConfidence("low")).toBe("low");
    expect(normalizeConfidence("certain")).toBe("");
    expect(normalizeConfidence("")).toBe("");
    expect(normalizeConfidence(null)).toBe("");
    expect(normalizeConfidence(3)).toBe("");
  });

  it("详情归一化：confidence 缺失/非法都不炸，合法值原样透出", () => {
    const base = { path: "concepts/x.md", title: "X", page_type: "concept", body_md: "正文" };
    expect(normalizeWikiDetail({ ...base, confidence: "high" }).confidence).toBe("high");
    expect(normalizeWikiDetail({ ...base, confidence: "  LOW " }).confidence).toBe("low");
    expect(normalizeWikiDetail({ ...base, confidence: "??" }).confidence).toBe("");
    expect(normalizeWikiDetail(base).confidence).toBe("");
  });

  it("标签与悬停说明：不认识的等级不渲染", () => {
    expect(confidenceLabel("high")).toBe("可信度：高");
    expect(confidenceLabel("medium")).toBe("可信度：中");
    expect(confidenceLabel("low")).toBe("可信度：低");
    expect(confidenceLabel("")).toBe("");
    expect(confidenceLabel("other")).toBe("");
    expect(confidenceHint("high")).toContain("多份独立来源");
    expect(confidenceHint("low")).toContain("来源薄弱");
    expect(confidenceHint("other")).toBe("");
  });
});
