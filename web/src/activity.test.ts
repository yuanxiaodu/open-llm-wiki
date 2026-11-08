// 日志/版本共用的纯函数（行动词、actor、按天分组、会话成果计数）。
import { describe, expect, it } from "vitest";
import {
  actionLabel,
  actorLabel,
  groupLogByDay,
  groupLogItemsByDay,
  logDay,
  mergeLogItems,
  normalizeLog,
  normalizeOutcomes,
  outcomeSummary,
  shortSha,
} from "./activity.ts";
import type { LogEntry, SessionOutcome } from "./activity.ts";

function entry(over: Partial<LogEntry> = {}): LogEntry {
  return {
    sha: "0123456789abcdef",
    kind: "wiki",
    action: "create",
    slug: "entities/a",
    actor: "张三",
    note: "",
    at: "2026-10-23T03:00:00+00:00",
    ...over,
  };
}

describe("normalizeLog", () => {
  it("数组归一化；坏条目/非数组 → 空", () => {
    expect(normalizeLog([{ sha: "abc", action: "edit" }, { no: "sha" }, null])).toHaveLength(1);
    expect(normalizeLog(null)).toEqual([]);
    expect(normalizeLog({})).toEqual([]);
  });
});

describe("actionLabel / actorLabel / shortSha", () => {
  it("action 中文；未知原样；空说「其他」", () => {
    expect(actionLabel("create")).toBe("新建");
    expect(actionLabel("edit")).toBe("编辑");
    expect(actionLabel("revert")).toBe("回滚");
    expect(actionLabel("ingest")).toBe("摄入");
    expect(actionLabel("archive")).toBe("archive");
    expect(actionLabel("")).toBe("其他");
  });

  it("agent → 智能体；空 → 未知", () => {
    expect(actorLabel("agent")).toBe("智能体");
    expect(actorLabel("Agent")).toBe("智能体");
    expect(actorLabel("system")).toBe("系统");
    expect(actorLabel("张三")).toBe("张三");
    expect(actorLabel("")).toBe("未知");
  });

  it("shortSha 取 7 位，短的/空的原样", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456");
    expect(shortSha("abc")).toBe("abc");
    expect(shortSha(null)).toBe("");
  });
});

describe("groupLogByDay", () => {
  it("按本地日期分组并保持后端顺序（新到旧）", () => {
    const groups = groupLogByDay([
      entry({ at: "2026-10-23T10:00:00+08:00" }), // 10-23 10:00
      entry({ at: "2026-10-23T03:00:00+00:00" }), // 10-23 11:00
      entry({ at: "2026-10-22T03:00:00+00:00" }),
    ]);
    expect(groups.map((g) => g.items.length)).toEqual([2, 1]);
    expect(groups[0].day).toBe(logDay(entry({ at: "2026-10-23T10:00:00+08:00" })));
    expect(groups[1].day).not.toBe(groups[0].day);
  });

  it("坏时间归「未知日期」；空列表 → 空分组", () => {
    expect(groupLogByDay([entry({ at: null }), entry({ at: "坏" })])[0].day).toBe("未知日期");
    expect(groupLogByDay([])).toEqual([]);
  });
});

describe("outcomeSummary：会话成果那行计数", () => {
  it("页数 / 引注 / 待修，没有就不摆 0", () => {
    expect(outcomeSummary({ pages: 3, citations: 18, lint_errors: 0 })).toBe("3 页 · 18 引注");
    expect(outcomeSummary({ pages: 0, citations: 2, lint_errors: 2 })).toBe("2 引注 · 待修 2");
    expect(outcomeSummary({ pages: 3, citations: 0 })).toBe("3 页");
    expect(outcomeSummary({ pages: 0, citations: 0, lint_errors: 0 })).toBe("");
    expect(outcomeSummary(null)).toBe("");
    expect(outcomeSummary(undefined)).toBe("");
  });
});

describe("normalizeOutcomes：/api/log/outcomes 的容错", () => {
  const ok = {
    id: 3,
    title: "整理资料",
    owner_name: "张三",
    status: "active",
    at: "2026-10-30T01:00:00+00:00",
    outcome: { pages: 3, citations: 11, lint_errors: 0 },
  };

  it("认 {outcomes:[...]} 与裸数组；非数组/空 → 空", () => {
    expect(normalizeOutcomes({ outcomes: [ok] })).toHaveLength(1);
    expect(normalizeOutcomes([ok])).toHaveLength(1);
    expect(normalizeOutcomes({})).toEqual([]);
    expect(normalizeOutcomes(null)).toEqual([]);
    expect(normalizeOutcomes([])).toEqual([]);
  });

  it("坏条目跳过：id 非数字 / outcome 残缺 / 行不是对象", () => {
    const got = normalizeOutcomes({
      outcomes: [
        ok,
        { ...ok, id: "3" }, // 字符串 id 点不到会话
        { ...ok, outcome: {} },
        { ...ok, outcome: { pages: 1 } }, // 缺 citations
        { ...ok, outcome: { citations: 2 } }, // 缺 pages
        { ...ok, outcome: null },
        null,
      ],
    });
    expect(got.map((o) => o.id)).toEqual([3]);
  });

  it("缺字段给空串/null；lint_errors 缺省 → null", () => {
    expect(normalizeOutcomes([{ id: 7, outcome: { pages: 2, citations: 0 } }])).toEqual([
      {
        id: 7,
        title: "",
        owner_name: "",
        status: "",
        at: null,
        outcome: { pages: 2, citations: 0, lint_errors: null },
      },
    ]);
  });
});

describe("mergeLogItems / groupLogItemsByDay：日志合并成一条时间线", () => {
  function outcome(over: Partial<SessionOutcome> = {}): SessionOutcome {
    return {
      id: 3,
      title: "整理资料",
      owner_name: "张三",
      status: "active",
      at: "2026-10-23T04:00:00+00:00",
      outcome: { pages: 3, citations: 11, lint_errors: 0 },
      ...over,
    };
  }

  it("提交与成果按 at 新到旧混排；同一个时间保持传入顺序", () => {
    const items = mergeLogItems(
      [
        entry({ sha: "a", at: "2026-10-23T03:00:00+00:00" }),
        entry({ sha: "b", at: "2026-10-23T01:00:00+00:00" }),
      ],
      [outcome({ at: "2026-10-23T04:00:00+00:00" }), outcome({ id: 4, at: "2026-10-23T03:00:00+00:00" })],
    );
    expect(items.map((item) => (item.kind === "commit" ? item.entry.sha : `o${item.outcome.id}`)))
      .toEqual(["o3", "a", "o4", "b"]);
  });

  it("缺时间/坏时间排最后，并归到「未知日期」", () => {
    const items = mergeLogItems(
      [entry({ at: null })],
      [outcome({ at: "坏" }), outcome({ id: 5, at: "2026-10-23T00:00:00+00:00" })],
    );
    expect(items[0]!.kind).toBe("outcome");
    const groups = groupLogItemsByDay(items);
    expect(groups[0]!.day).not.toBe("未知日期");
    expect(groups[groups.length - 1]!.day).toBe("未知日期");
    expect(groups[groups.length - 1]!.items).toHaveLength(2);
  });

  it("同一天的两类条目只出一组；空输入 → 空", () => {
    const groups = groupLogItemsByDay(mergeLogItems(
      [entry({ at: "2026-10-23T10:00:00+08:00" })],
      [outcome({ at: "2026-10-23T11:00:00+08:00" })],
    ));
    expect(groups).toHaveLength(1);
    expect(groups[0]!.items).toHaveLength(2);
    expect(groupLogItemsByDay([])).toEqual([]);
  });
});
