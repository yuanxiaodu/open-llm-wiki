// 块折叠的钉子：流上的帧与库里的事件行走的是**同一份**规则（blocks.ts），
// 这两条路的输出因此必须一致——刷新前后同一轮长得不一样，就是这里的某一条红了。
// 末尾两组钉的是**行**级的同源问题：库里的行 vs 乐观占位的行（mergeRows，裁定 R-S3-18）、
// 半截那一轮该重发哪句（resendTexts，裁定 R-S3-13）。
//
// payload 一律照抄后端落库的形状（app/agent/hooks.py：tool-call{id,name,args?}、
// tool-result{id,name,ok,summary}；runner._event：system{text}、error{code,text}），
// 文案也照抄 runner.py，别在这儿改字。
import { describe, expect, it } from "vitest";

import { blocksForMessage, blocksFromDb, foldEvent, mergeRows, phaseOf, resendTexts, sourcesOf } from "./blocks";
import type { LiveBlock } from "./blocks";
import { BROKEN_FRAME_TEXT } from "./sse";
import type { UiEvent } from "./sse";
import type { AgentEvent, MessageRow } from "./types";

const QUOTA_TEXT =
  "额度暂时用完，你说的和传的都已保存。这一条回复没写出来，等额度回来重发一次。";
const UPSTREAM_TEXT =
  "智能体暂时连不上，你说的和传的都已保存。已经流出来的半截留在上面，稍后重发一次。";

/** 一条已落库的 agent 消息（详情接口的形状）。 */
function row(content: string, events: AgentEvent[] = [], over: Partial<MessageRow> = {}): MessageRow {
  return {
    id: 88,
    session_id: 1,
    seq: 2,
    role: "agent",
    user_id: null,
    user_name: null,
    content,
    created_at: "2026-10-23T10:00:00+00:00",
    truncated: 0,
    usage_json: "{}",
    attachments: [],
    events,
    ...over,
  };
}

function ev(type: string, payload: Record<string, unknown> | null): AgentEvent {
  return { type, payload };
}

const CALL = ev("tool-call", {
  id: "call_9a1",
  name: "search",
  args: '{"q": "试听 预约 漏单"}',
});
const RESULT_OK = ev("tool-result", {
  id: "call_9a1",
  name: "search",
  ok: true,
  summary: "命中 4 条",
});
const RESULT_FAIL = ev("tool-result", {
  id: "call_9a2",
  name: "edit_page",
  ok: false,
  summary: "改动没落地：这一页正被别人改着，稍后再试",
});

describe("blocksFromDb（历史回合，裁定 R-S3-8）", () => {
  it("正文一块在前，事件按 seq 折在后面（与尾读重放整轮的近似同口径）", () => {
    const blocks = blocksFromDb(row("已经并进《报名流程线上化》v3。", [CALL, RESULT_OK]));
    expect(blocks).toEqual([
      { kind: "text", text: "已经并进《报名流程线上化》v3。" },
      {
        kind: "tool",
        id: "call_9a1",
        name: "search",
        args: '{"q": "试听 预约 漏单"}',
        result: { ok: true, summary: "命中 4 条" },
      },
    ]);
  });

  it("没有 events 就返 null（交给 live/extras 那条路），而不是一个空数组", () => {
    expect(blocksFromDb(row("纯正文的一轮。"))).toBeNull();
    expect(blocksFromDb(row("纯正文的一轮。", []))).toBeNull();
  });

  it("失败的 tool-result 折成 ok=false 的那张卡（失败必须看得见）", () => {
    const blocks = blocksFromDb(row("", [RESULT_FAIL]));
    expect(blocks).toEqual([
      {
        kind: "tool",
        id: "call_9a2",
        name: "edit_page",
        args: "",
        result: { ok: false, summary: "改动没落地：这一页正被别人改着，稍后再试" },
      },
    ]);
  });

  it("配不上对的 tool-result 单独摆一张，不悄悄扔掉", () => {
    // 库里只有结果、没有那次调用（call 行没落库）：卡片照摆，args 是空的
    expect(blocksFromDb(row("好。", [RESULT_OK]))).toEqual([
      { kind: "text", text: "好。" },
      {
        kind: "tool",
        id: "call_9a1",
        name: "search",
        args: "",
        result: { ok: true, summary: "命中 4 条" },
      },
    ]);
  });

  it("system → 琥珀 warn 横幅；error → 赭红 err 横幅（与流上同一套映射）", () => {
    const blocks = blocksFromDb(
      row("", [ev("system", { text: QUOTA_TEXT }), ev("error", { code: "upstream", text: UPSTREAM_TEXT })]),
    );
    expect(blocks).toEqual([
      { kind: "banner", text: QUOTA_TEXT, level: "warn" },
      { kind: "banner", text: UPSTREAM_TEXT, level: "err" },
    ]);
  });

  it("payload 是 null（库里那行 payload_json 坏了）→ 「服务端帧坏了」的 err 横幅", () => {
    // 后端 _parse_payload 对坏 JSON 交 null；sse.ts 的形状校验把它认成坏帧。
    // 历史这一路因此与流上收到坏 payload 时说的是同一句话。
    expect(blocksFromDb(row("好。", [ev("tool-call", null)]))).toEqual([
      { kind: "text", text: "好。" },
      { kind: "banner", text: BROKEN_FRAME_TEXT, level: "err" },
    ]);
  });

  it("不认得的 type 不产生块（后端哪天加了新类型，历史这一路不至于炸）", () => {
    expect(blocksFromDb(row("好。", [ev("heartbeat", {})]))).toEqual([
      { kind: "text", text: "好。" },
    ]);
  });
});

describe("blocksForMessage（db-wins）", () => {
  it("库里交了 events 就以库为准，客户端攒下的 extras 一概不看", () => {
    // extras 里那张卡是"库没有的"（例如只活在流上的东西）：db 赢，它就不出现
    const live: LiveBlock[] = [
      { kind: "text", text: "已经并进《报名流程线上化》v3。" },
      { kind: "tool", id: "call_live", name: "read_page", args: "" },
      { kind: "banner", text: "这条流等得太久了，先断开。", level: "err" },
    ];
    const blocks = blocksForMessage(row("已经并进《报名流程线上化》v3。", [CALL, RESULT_OK]), live);
    expect(blocks).toEqual([
      { kind: "text", text: "已经并进《报名流程线上化》v3。" },
      {
        kind: "tool",
        id: "call_9a1",
        name: "search",
        args: '{"q": "试听 预约 漏单"}',
        result: { ok: true, summary: "命中 4 条" },
      },
    ]);
  });

  it("没有 events 时行为不变：正文串一致就原样用 live（交错顺序是当时看到的那个）", () => {
    const live: LiveBlock[] = [
      { kind: "text", text: "先说一句，" },
      { kind: "tool", id: "c1", name: "search", args: "" },
      { kind: "text", text: "再说一句。" },
    ];
    expect(blocksForMessage(row("先说一句，再说一句。", [], {}), live)).toEqual(live);
  });

  it("没有 events 也没有 live 时，只有库里那段正文", () => {
    expect(blocksForMessage(row("好。"))).toEqual([{ kind: "text", text: "好。" }]);
  });

  it("没有 events 而正文对不上（截断/重放）时以库为准，工具卡与横幅原地保留", () => {
    const live: LiveBlock[] = [
      { kind: "text", text: "半截就被掐了的" },
      { kind: "tool", id: "c1", name: "search", args: "", result: { ok: true, summary: "命中 4 条" } },
      { kind: "banner", text: QUOTA_TEXT, level: "warn" },
    ];
    expect(blocksForMessage(row("…（这一轮没写完）", []), live)).toEqual([
      { kind: "text", text: "…（这一轮没写完）" },
      live[1],
      live[2],
    ]);
  });
});

describe("foldEvent（流式那一路，与历史共用）", () => {
  it("连续的 text 事件接在同一块尾巴上", () => {
    let blocks: LiveBlock[] = [];
    for (const text of ["先查了全站，", "再动笔。"]) {
      blocks = foldEvent(blocks, { kind: "text", text } satisfies UiEvent);
    }
    expect(blocks).toEqual([{ kind: "text", text: "先查了全站，再动笔。" }]);
  });

  it("tool-result 按 id 配回那张卡；id 对不上就继续往前找，不退回按名字猜", () => {
    let blocks = foldEvent([], { kind: "tool", id: "c1", name: "search", args: "" });
    blocks = foldEvent(blocks, { kind: "tool", id: "c2", name: "search", args: "" });
    blocks = foldEvent(blocks, {
      kind: "toolresult",
      id: "c1",
      name: "search",
      ok: false,
      summary: "检索没跑起来",
    });
    expect(blocks).toEqual([
      { kind: "tool", id: "c1", name: "search", args: "", result: { ok: false, summary: "检索没跑起来" } },
      { kind: "tool", id: "c2", name: "search", args: "" },
    ]);
  });

  it("两边都没有 id 时退回按工具名配（宁可配错一张同名的，也不把失败扔掉）", () => {
    let blocks = foldEvent([], { kind: "tool", id: "", name: "list_members", args: "" });
    blocks = foldEvent(blocks, {
      kind: "toolresult",
      id: "",
      name: "list_members",
      ok: true,
      summary: "共 7 人",
    });
    expect(blocks).toEqual([
      { kind: "tool", id: "", name: "list_members", args: "", result: { ok: true, summary: "共 7 人" } },
    ]);
  });

  it("end 不产生块，也不改动交进来的数组", () => {
    const before: LiveBlock[] = [{ kind: "text", text: "好。" }];
    expect(foldEvent(before, { kind: "end" })).toBe(before);
  });

  it("是纯函数：交进去的数组不被就地改掉（Chat 那边拿着旧引用也不会看见突变）", () => {
    const before: LiveBlock[] = [{ kind: "text", text: "先说一句，" }];
    const after = foldEvent(before, { kind: "text", text: "再说一句。" });
    expect(before).toEqual([{ kind: "text", text: "先说一句，" }]);
    expect(after).toEqual([{ kind: "text", text: "先说一句，再说一句。" }]);
  });
});

/** 一条人类消息（乐观行与库里的行同形，只是 role 与 user_* 不一样）。 */
function human(id: number, content: string, over: Partial<MessageRow> = {}): MessageRow {
  return row(content, [], { id, role: "human", user_id: 7, user_name: "张三", ...over });
}

describe("mergeRows：库里的行 + 乐观行按 id 求差（裁定 R-S3-18）", () => {
  it("库里已经有了同一条 → 乐观行让位，画面上不会出现两遍", () => {
    // 这一条就是 R-S3-18 要治的：setDetail 与 setOptimistic([]) 落在两个 await 之后，
    // React 不保证同批 flush；靠"清空"去重的话，中间那一帧就是同一句话说两遍。
    const fromDb = [human(11, "报名流程怎么改"), row("已经并进 v3。", [], { id: 12 })];
    expect(mergeRows(fromDb, [human(11, "报名流程怎么改")]).map((m) => m.id)).toEqual([11, 12]);
  });

  it("库里还没有（POST 刚回来、详情还没重拉）→ 乐观行拼在后面占位", () => {
    const fromDb = [human(11, "上一轮的问题"), row("上一轮的回答", [], { id: 12 })];
    expect(mergeRows(fromDb, [human(13, "这一轮的问题")]).map((m) => m.id)).toEqual([11, 12, 13]);
  });

  it("详情压根没拉到（空数组）→ 乐观行原样都在", () => {
    expect(mergeRows([], [human(13, "这一轮的问题")]).map((m) => m.id)).toEqual([13]);
  });

  it("只去掉库里已有的那几条，其余乐观行留着（混合的一批）", () => {
    const fromDb = [human(11, "甲")];
    const optimistic = [human(11, "甲"), human(12, "乙"), human(13, "丙")];
    expect(mergeRows(fromDb, optimistic).map((m) => m.id)).toEqual([11, 12, 13]);
  });

  it("DEV 演示帧的负数假 id 不会被误判成库里的行（压根不会撞）", () => {
    const fromDb = [human(11, "甲"), row("乙", [], { id: 12 })];
    expect(mergeRows(fromDb, [row("演示帧合成的回答", [], { id: -1761234567890 })]).map((m) => m.id))
      .toEqual([11, 12, -1761234567890]);
  });

  it("两边都空 → 空；库里的顺序与乐观行的顺序都原样保留", () => {
    expect(mergeRows([], [])).toEqual([]);
    const fromDb = [human(3, "三"), human(1, "一"), human(2, "二")]; // 不按 id 排也照原样
    expect(mergeRows(fromDb, []).map((m) => m.id)).toEqual([3, 1, 2]);
  });

  it("是纯函数：交进来的两个数组都不被就地改掉", () => {
    const fromDb = [human(11, "甲")];
    const optimistic = [human(11, "甲"), human(12, "乙")];
    mergeRows(fromDb, optimistic);
    expect(fromDb.map((m) => m.id)).toEqual([11]);
    expect(optimistic.map((m) => m.id)).toEqual([11, 12]);
  });
});

describe("resendTexts：哪些回合挂「重发」、点了放哪句回去（裁定 R-S3-13）", () => {
  it("truncated=1 的 agent 行 → 它前面最近那条人类消息的原文", () => {
    const messages = [
      human(11, "帮我把试听那段并进去"),
      row("…（这一轮没写完）", [], { id: 12, truncated: 1 }),
    ];
    expect(resendTexts(messages).get(12)).toBe("帮我把试听那段并进去");
  });

  it("写完的那一轮（truncated=0）不给把手：没什么可重发的", () => {
    const messages = [human(11, "帮我把试听那段并进去"), row("已经并进 v3。", [], { id: 12 })];
    expect(resendTexts(messages).size).toBe(0);
  });

  it("多轮各配各的，不串到上一轮的问题上", () => {
    const messages = [
      human(11, "第一问"),
      row("第一答", [], { id: 12 }),
      human(13, "第二问"),
      row("…（断了）", [], { id: 14, truncated: 1 }),
      human(15, "第三问"),
      row("…（又断了）", [], { id: 16, truncated: 1 }),
    ];
    const got = resendTexts(messages);
    expect([...got.keys()]).toEqual([14, 16]);
    expect(got.get(14)).toBe("第二问");
    expect(got.get(16)).toBe("第三问");
  });

  it("第一条就是 agent 行（启动时封口的孤儿）→ 不给键：没有原文可预填", () => {
    expect(resendTexts([row("…（上次没写完）", [], { id: 5, truncated: 1 })]).size).toBe(0);
  });

  it("人类那句是空白 → 不给键（摆一个点了什么都不发生的按钮比不摆更坏）", () => {
    const messages = [human(11, "   "), row("…（断了）", [], { id: 12, truncated: 1 })];
    expect(resendTexts(messages).size).toBe(0);
  });

  it("human 行自己带 truncated=1 也不算（判据是 role，不是那一列）", () => {
    const messages = [
      human(11, "问一句", { truncated: 1 }),
      row("答完了", [], { id: 12 }),
    ];
    expect(resendTexts(messages).size).toBe(0);
  });

  it("原文原样交出去（不 trim、不改字）：人要看见的就是自己当时打的那串", () => {
    const messages = [
      human(11, "  带空白的原句\n第二行  "),
      row("…（断了）", [], { id: 12, truncated: 1 }),
    ];
    expect(resendTexts(messages).get(12)).toBe("  带空白的原句\n第二行  ");
  });

  it("是纯函数：交进来的数组不被就地改掉", () => {
    const messages = [human(11, "问"), row("…", [], { id: 12, truncated: 1 })];
    resendTexts(messages);
    expect(messages.map((m) => m.id)).toEqual([11, 12]);
  });
});

describe("outcome / 依据页 / 阶段（2026-10-30）", () => {
  it("foldEvent 追加 outcome 块（与历史走同一套）", () => {
    const outcome = { pages: [{ path: "concepts/a.md", action: "create" }], citations: 2, lint_errors: 0 };
    expect(foldEvent([], { kind: "outcome", outcome })).toEqual([{ kind: "outcome", outcome }]);
  });

  it("sourcesOf：读页与搜索命中都算依据；raw、重复、特殊文件不算", () => {
    const blocks: LiveBlock[] = [
      { kind: "tool", id: "c1", name: "read", args: JSON.stringify({ path: "concepts/a.md" }) },
      { kind: "tool", id: "c2", name: "read", args: JSON.stringify({ path: "raw/uploads/1-x.md" }) },
      { kind: "tool", id: "c3", name: "read", args: JSON.stringify({ path: "index.md" }) },
      {
        kind: "tool", id: "c4", name: "search", args: "{}",
        result: { ok: true, summary: "【页面 concepts/b.md】标题：片段" },
      },
      { kind: "tool", id: "c5", name: "read", args: JSON.stringify({ path: "concepts/a.md" }) },
    ];
    expect(sourcesOf(blocks)).toEqual(["concepts/a.md", "concepts/b.md"]);
  });

  it("phaseOf：最近一次工具决定「在干什么」", () => {
    const tool = (name: string): LiveBlock => ({ kind: "tool", id: name, name, args: "" });
    expect(phaseOf([{ kind: "text", text: "嗯" }])).toBeNull();
    expect(phaseOf([tool("read")])).toBe("读材料");
    expect(phaseOf([tool("search")])).toBe("找资料");
    expect(phaseOf([tool("write_page")])).toBe("整理成页");
    expect(phaseOf([tool("lint")])).toBe("体检");
    expect(phaseOf([tool("mystery")])).toBeNull();
  });
});
