// 复制助手：clipboard → execCommand 回退 → 手动，三层都有测试（node 无 DOM）。
import { describe, expect, it } from "vitest";
import { copyText } from "./clipboard.ts";

describe("copyText：HTTP 非安全上下文也要能用", () => {
  it("有 clipboard：用它，不走回退", async () => {
    const calls: string[] = [];
    const got = await copyText("链接", {
      writeText: async (t) => {
        calls.push(t);
      },
      legacyCopy: () => {
        calls.push("legacy");
        return true;
      },
    });
    expect(got).toBe("copied");
    expect(calls).toEqual(["链接"]);
  });

  it("clipboard 抛错 → 回退 execCommand 成功", async () => {
    const got = await copyText("链接", {
      writeText: async () => {
        throw new Error("NotAllowed");
      },
      legacyCopy: () => true,
    });
    expect(got).toBe("copied");
  });

  it("没有 clipboard → 回退；都失败/都没有 → manual（页面提示手动复制）", async () => {
    expect(await copyText("x", { writeText: null, legacyCopy: () => true })).toBe("copied");
    expect(await copyText("x", { writeText: null, legacyCopy: () => false })).toBe("manual");
    expect(await copyText("x", { writeText: null, legacyCopy: null })).toBe("manual");
    expect(await copyText("x", { writeText: async () => {}, legacyCopy: null })).toBe("copied");
  });
});
