// 工具卡说人话：内部工具名 → 中文动作 + 对象（2026-10-30）。
import { describe, expect, it } from "vitest";

import { toolLabel, toolTarget } from "./ToolCard.tsx";
import TOOLCARD from "./ToolCard.tsx?raw";

describe("toolLabel：对话框里不出现内部词", () => {
  it("十个工具都有中文动作", () => {
    expect(toolLabel("read")).toBe("读材料");
    expect(toolLabel("search")).toBe("找资料");
    expect(toolLabel("write_page")).toBe("建页");
    expect(toolLabel("edit_page")).toBe("改页");
    expect(toolLabel("lint")).toBe("体检");
    expect(toolLabel("ingest_document")).toBe("长文摄入");
  });

  it("认不出的原样显示，不藏", () => {
    expect(toolLabel("mystery")).toBe("mystery");
  });
});

describe("toolTarget：只挑一个对象名，不摆整段 JSON", () => {
  it("read/edit 取文件名，write 取标题，search 取查询", () => {
    expect(toolTarget("read", JSON.stringify({ path: "concepts/a.md" }))).toBe("a");
    expect(toolTarget("write_page", JSON.stringify({ title: "报名流程" }))).toBe("报名流程");
    expect(toolTarget("search", JSON.stringify({ query: "实习" }))).toBe("实习");
  });

  it("坏参数/无参工具返回空串", () => {
    expect(toolTarget("read", "not-json")).toBe("");
    expect(toolTarget("lint", "{}")).toBe("");
  });
});

describe("markup：卡片上显示的是中文标签", () => {
  it("用 toolLabel(name) 而不是直接 {name}", () => {
    expect(TOOLCARD).toContain("{label}");
    expect(TOOLCARD).not.toContain('className="nm">{name}');
  });
});
