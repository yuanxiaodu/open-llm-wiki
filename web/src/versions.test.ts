// 版本页纯函数：默认两版、勾选、东西向、diff 行染色。
import { describe, expect, it } from "vitest";
import {
  defaultDiffPair,
  isUnifiedDiff,
  pairFromSelected,
  normalizeVersions,
  parseDiffLines,
  pickVersion,
  shortSha,
  versionStamp,
} from "./versions.ts";
import type { VersionEntry } from "./versions.ts";

function v(sha: string, over: Partial<VersionEntry> = {}): VersionEntry {
  return {
    sha,
    kind: "wiki",
    action: "edit",
    slug: "entities/a",
    actor: "张三",
    note: "",
    at: "2026-10-23T03:00:00+00:00",
    ...over,
  };
}

describe("normalizeVersions / 小工具", () => {
  it("坏条目跳过；短 sha/时间人话", () => {
    expect(normalizeVersions([{ sha: "abcdef1234", at: null }, { no: "sha" }])).toEqual([
      {
        sha: "abcdef1234",
        kind: "",
        action: "",
        slug: "",
        actor: "",
        note: "",
        at: null,
      },
    ]);
    expect(shortSha("abcdef1234")).toBe("abcdef1");
    expect(versionStamp({ at: null })).toBe("未知时间");
    expect(versionStamp({ at: "2026-10-23T03:00:00+00:00" })).not.toBe("未知时间");
  });
});

describe("defaultDiffPair：a 旧 b 新", () => {
  it("最近两版，a=较旧、b=较新；不足两版 → null", () => {
    expect(defaultDiffPair([v("new"), v("old"), v("older")])).toEqual({ a: "old", b: "new" });
    expect(defaultDiffPair([v("only")])).toBeNull();
    expect(defaultDiffPair([])).toBeNull();
  });
});

describe("pairFromSelected：a 永远是较旧那版", () => {
  it("选反了会纠正；不足两版/找不到 → null", () => {
    const list = [v("new"), v("mid"), v("old")];
    expect(pairFromSelected(list, ["new", "old"])).toEqual({ a: "old", b: "new" });
    expect(pairFromSelected(list, ["old", "new"])).toEqual({ a: "old", b: "new" });
    expect(pairFromSelected(list, ["new"])).toBeNull();
    expect(pairFromSelected(list, ["new", "nope"])).toBeNull();
    expect(pairFromSelected(list, ["new", "new"])).toBeNull();
  });
});

describe("pickVersion：最多两版、可取消、第三版挤掉最早的", () => {
  it("选中/取消/先进先出", () => {
    expect(pickVersion([], "a")).toEqual(["a"]);
    expect(pickVersion(["a"], "b")).toEqual(["a", "b"]);
    expect(pickVersion(["a", "b"], "c")).toEqual(["b", "c"]);
    expect(pickVersion(["a", "b"], "a")).toEqual(["b"]);
  });
});

describe("isUnifiedDiff / parseDiffLines", () => {
  it("首行是 diff/--- 才算（任务口径）；空/普通文本/只剩 hunk 都不算", () => {
    expect(isUnifiedDiff("diff --git a/x b/x\nindex 1..2")).toBe(true);
    expect(isUnifiedDiff("--- a/x\n+++ b/x")).toBe(true);
    expect(isUnifiedDiff("@@ -1 +1 @@")).toBe(false);
    expect(isUnifiedDiff("+++ b/x")).toBe(false);
    expect(isUnifiedDiff("普通文本")).toBe(false);
    expect(isUnifiedDiff("")).toBe(false);
  });

  it("行型：add/del/hunk/meta/context；不改字符、尾换行不多渲染一行", () => {
    const lines = parseDiffLines(
      [
        "diff --git a/x b/x",
        "--- a/x",
        "+++ b/x",
        "@@ -1,2 +1,2 @@",
        " 上下文",
        "-旧行",
        "+新行",
        "",
      ].join("\n"),
    );
    expect(lines.map((l) => l.kind)).toEqual([
      "meta",
      "meta",
      "meta",
      "hunk",
      "context",
      "del",
      "add",
    ]);
    expect(lines[5].text).toBe("-旧行");
    expect(parseDiffLines("")).toEqual([]);
  });
});
