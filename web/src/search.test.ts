// 搜索页纯函数：三组形状归一化 + 【】高亮切片 + 各组跳转。
import { describe, expect, it } from "vitest";
import {
  normalizeSearch,
  searchPageHref,
  searchSessionHref,
  splitSnippet,
  totalHits,
} from "./search.ts";
import type { SearchHit } from "./search.ts";

const hit = (over: Partial<SearchHit> = {}): SearchHit => ({
  ref_id: "1",
  heading: "标题",
  snippet: "正文",
  session_id: null,
  ...over,
});

describe("normalizeSearch", () => {
  it("三组归一化；缺组/非数组 → 空数组", () => {
    const got = normalizeSearch({
      sessions: [{ ref_id: 3, heading: "张三", snippet: "正文", session_id: 7 }],
      uploads: [{ ref_id: "9", heading: "周记.pdf", snippet: "" }],
      pages: [],
    });
    expect(got.sessions[0]).toEqual({
      ref_id: "3",
      heading: "张三",
      snippet: "正文",
      session_id: 7,
    });
    expect(got.uploads).toHaveLength(1);
    expect(normalizeSearch(null)).toEqual({ sessions: [], uploads: [], pages: [] });
    expect(totalHits(got)).toBe(2);
  });

  it("没有 ref_id 的坏命中跳过；session_id 不是数字 → null", () => {
    const got = normalizeSearch({ sessions: [{ heading: "x" }, { ref_id: "1", session_id: "7" }] });
    expect(got.sessions).toHaveLength(1);
    expect(got.sessions[0].session_id).toBeNull();
  });
});

describe("splitSnippet：必须用文本节点切，不 innerHTML", () => {
  it("【】成对高亮；前后普通文字保留", () => {
    expect(splitSnippet("漏单【降到】1 起，【投诉】也降")).toEqual([
      { text: "漏单", mark: false },
      { text: "降到", mark: true },
      { text: "1 起，", mark: false },
      { text: "投诉", mark: true },
      { text: "也降", mark: false },
    ]);
  });

  it("没有标记 → 整段普通；空串 → 空数组", () => {
    expect(splitSnippet("普通正文")).toEqual([{ text: "普通正文", mark: false }]);
    expect(splitSnippet("")).toEqual([]);
  });

  it("落单的【/】当普通文字，不吞字；空【】不产出空 mark", () => {
    expect(splitSnippet("a【b")).toEqual([{ text: "a【b", mark: false }]);
    expect(splitSnippet("a】b")).toEqual([{ text: "a】b", mark: false }]);
    expect(splitSnippet("甲【】乙")).toEqual([
      { text: "甲", mark: false },
      { text: "乙", mark: false },
    ]);
  });
});

describe("三组跳转", () => {
  it("sessions：有 session_id 才给 /chat/id", () => {
    expect(searchSessionHref(hit({ session_id: 7 }))).toBe("/chat/7");
    expect(searchSessionHref(hit({ session_id: null }))).toBeNull();
  });

  it("pages：ref_id 的 .md 去掉、逐段编码", () => {
    expect(searchPageHref(hit({ ref_id: "entities/foo.md" }))).toBe("/wiki/entities/foo");
    expect(searchPageHref(hit({ ref_id: "entities/人物/阿明.md" }))).toBe(
      "/wiki/entities/%E4%BA%BA%E7%89%A9/%E9%98%BF%E6%98%8E",
    );
    expect(searchPageHref(hit({ ref_id: "" }))).toBeNull();
  });
});
