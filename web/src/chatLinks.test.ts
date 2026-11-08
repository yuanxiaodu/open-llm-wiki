import { describe, expect, it } from "vitest";

import { buildWikiLinkIndex, chatLinkMode, linkifyWikilinks } from "./chatLinks.ts";
import type { WikiPageMeta } from "./wiki.ts";

const pages: WikiPageMeta[] = [
  { path: "concepts/chasm.md", slug: "chasm", title: "鸿沟", page_type: "concept", tags: [], summary: "", created_at: "", updated_at: "", actor: null },
  { path: "entities/seagull.md", slug: "seagull", title: "海鸥", page_type: "entity", tags: [], summary: "", created_at: "", updated_at: "", actor: null },
];

describe("linkifyWikilinks", () => {
  const index = buildWikiLinkIndex(pages);
  it("slug 改成 /wiki 链接，带显示文字时用显示文字", () => {
    expect(linkifyWikilinks("见 [[chasm]]。", index)).toBe("见 [鸿沟](/wiki/concepts/chasm)。");
    expect(linkifyWikilinks("见 [[chasm|这道坎]]。", index)).toBe("见 [这道坎](/wiki/concepts/chasm)。");
  });
  it("完整 path（含/不含 .md）也能解析", () => {
    expect(linkifyWikilinks("[[entities/seagull]]", index)).toBe("[海鸥](/wiki/entities/seagull)");
    expect(linkifyWikilinks("[[entities/seagull.md]]", index)).toBe("[海鸥](/wiki/entities/seagull)");
  });
  it("未知目标保留原文，不生成 404 链接", () => {
    expect(linkifyWikilinks("[[concept-chasm-marketing-strategy]]", index)).toBe(
      "[[concept-chasm-marketing-strategy]]",
    );
  });
  it("wiki 链接新标签页，站内/外链/非法协议分流", () => {
    expect(chatLinkMode("/wiki/concepts/a")).toBe("wiki");
    expect(chatLinkMode("/wiki")).toBe("wiki");
    expect(chatLinkMode("/chat/1")).toBe("internal");
    expect(chatLinkMode("#cite-1")).toBe("internal");
    expect(chatLinkMode("https://example.com/a")).toBe("external");
    expect(chatLinkMode("javascript:alert(1)")).toBe("blocked");
  });
  it("代码围栏/行内代码里的双链不动", () => {
    const text = "`[[chasm]]`\n\n```md\n[[chasm]]\n```\n\n[[chasm]]";
    expect(linkifyWikilinks(text, index)).toContain("`[[chasm]]`");
    expect(linkifyWikilinks(text, index)).toContain("```md\n[[chasm]]\n```");
    expect(linkifyWikilinks(text, index).endsWith("[鸿沟](/wiki/concepts/chasm)")).toBe(true);
  });
});
