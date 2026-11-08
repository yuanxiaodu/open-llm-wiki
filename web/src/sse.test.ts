// SSE 适配层的钉子。帧串一律**手抄**后端 app/routers/api/chat.py 的 _frame：
//
//     id: <n>\nevent: <type>\ndata: <json>\n\n
//
// 字段名后一个空格、LF 行尾、帧与帧之间空一行；id 的形状也是真的（_frame_id：
// seq * ID_STRIDE(1_000_000) + 正文偏移），所以 1000009 = 事件行 seq=1、正文已发 9 字。
// 手抄而不用生成器：生成器抄错格式时，测试会跟着一起错，钉不住线上那串字节。
import { describe, expect, it } from "vitest";

import { BROKEN_FRAME_TEXT, pump, resumeHeaders, toUiEvents } from "./sse";
import type { Frame, UiEvent } from "./sse";

// —— 后端原文（照抄，别在这儿改字）———————————————————————————————
const QUOTA_TEXT =
  "额度暂时用完，你说的和传的都已保存。这一条回复没写出来，等额度回来重发一次。";
const UPSTREAM_TEXT =
  "智能体暂时连不上，你说的和传的都已保存。已经流出来的半截留在上面，稍后重发一次。";
const STREAM_TIMEOUT_TEXT =
  "这条流等得太久了，先断开。刷新一下看最新内容，或把刚才那句重发一次。";

// —— 帧串 fixture ————————————————————————————————————————————————
const TEXT_1 = 'id: 4\nevent: text-delta\ndata: {"text":"先查一下"}\n\n';
const TEXT_2 = 'id: 9\nevent: text-delta\ndata: {"text":"已有的页面"}\n\n';
// args 是**串**（hooks 把 SDK 交来的 tool_arguments 原样 str() 截断），所以是 JSON 里套 JSON，
// 转义两层才对得上线上字节。
const TOOL_CALL =
  'id: 1000009\nevent: tool-call\ndata: {"id":"call_9a1","name":"search","args":"{\\"q\\":\\"新传专业产品经理转型指南\\"}"}\n\n';
const TOOL_RESULT_FAIL =
  'id: 2000009\nevent: tool-result\ndata: {"id":"call_9a1","name":"search","ok":false,"summary":"检索没跑起来：库锁住了"}\n\n';
// 空参形状：hooks 那边"没有参数串就整个键不记"，于是 args 键压根不在。
const TOOL_CALL_NO_ARGS =
  'id: 3000009\nevent: tool-call\ndata: {"id":"call_9a2","name":"list_members"}\n\n';
const TOOL_RESULT_OK =
  'id: 4000009\nevent: tool-result\ndata: {"id":"call_9a2","name":"list_members","ok":true,"summary":"共 7 人"}\n\n';
// system 就是额度那条：payload 只有 text，code 被 runner 刻意丢掉，没得 switch。
const SYSTEM = `id: 5000009\nevent: system\ndata: {"text":"${QUOTA_TEXT}"}\n\n`;
const ERROR_UPSTREAM = `id: 6000009\nevent: error\ndata: {"code":"upstream","text":"${UPSTREAM_TEXT}"}\n\n`;
// stream_timeout 不落 agent_events，是尾读直接发进流的（那会儿可能连 agent 行都没有），
// id 用 offset+1 的手法，所以是个小数。
const ERROR_TIMEOUT = `id: 10\nevent: error\ndata: {"code":"stream_timeout","text":"${STREAM_TIMEOUT_TEXT}"}\n\n`;
const DONE = 'id: 6000010\nevent: done\ndata: {"message_id":88,"usage":null}\n\n';
// 后端只发 LF。CRLF 的形状留在这儿当反例钉住：不许被解析，也不许被"顺手归一"。
const CRLF = 'id: 4\r\nevent: text-delta\r\ndata: {"text":"先查一下"}\r\n\r\n';

// —— 测试用的喂帧把手（模拟 Task 5 的 reader 循环）———————————————————
/** 把 chunk 依次喂进 pump，收齐帧；rest 是最后留在手里的半帧。 */
function feed(chunks: string[]): { frames: Frame[]; rest: string } {
  let buf = "";
  const frames: Frame[] = [];
  for (const chunk of chunks) {
    const r = pump(buf, chunk);
    frames.push(...r.frames);
    buf = r.rest;
  }
  return { frames, rest: buf };
}

/** 喂 chunk 并把帧映射成 UI 事件（一律 toUiEvents：一帧可能出两个事件）。 */
function eventsOf(chunks: string[]): UiEvent[] {
  return feed(chunks).frames.flatMap((f) => toUiEvents(f));
}

/** 按 17 字一切把整串打碎：切点故意不对齐帧边界。 */
function ragged(whole: string): string[] {
  const out: string[] = [];
  for (let i = 0; i < whole.length; i += 17) out.push(whole.slice(i, i + 17));
  return out;
}

describe("pump — 帧切分与半帧缓冲", () => {
  it("单帧一次到齐：解出一帧，rest 清空", () => {
    expect(pump("", TEXT_1)).toEqual({
      frames: [{ id: 4, event: "text-delta", data: { text: "先查一下" } }],
      rest: "",
    });
  });

  it("两帧同一个 chunk：按到达顺序解出两帧", () => {
    const r = pump("", TEXT_1 + TOOL_CALL_NO_ARGS);
    expect(r.frames).toEqual([
      { id: 4, event: "text-delta", data: { text: "先查一下" } },
      { id: 3000009, event: "tool-call", data: { id: "call_9a2", name: "list_members" } },
    ]);
    expect(r.rest).toBe("");
  });

  it("一帧分三段到（半帧缓冲）：前两段一帧都出不来，第三段整帧出现", () => {
    const a = TEXT_1.slice(0, 8); // 'id: 4\nev'
    const b = TEXT_1.slice(8, 30); // 切在 data 的 JSON 中间
    const c = TEXT_1.slice(30);
    expect(a.length + b.length + c.length).toBe(TEXT_1.length);

    const r1 = pump("", a);
    expect(r1.frames).toEqual([]);
    expect(r1.rest).toBe(a);

    const r2 = pump(r1.rest, b);
    expect(r2.frames).toEqual([]);
    expect(r2.rest).toBe(a + b);

    const r3 = pump(r2.rest, c);
    expect(r3.frames).toEqual([{ id: 4, event: "text-delta", data: { text: "先查一下" } }]);
    expect(r3.rest).toBe("");
  });

  it("没有终止空行的尾巴留在 rest 里，不当帧解", () => {
    const r = pump("", 'id: 4\nevent: text-delta\ndata: {"text":"先查一下"}\n');
    expect(r.frames).toEqual([]);
    expect(r.rest).toBe('id: 4\nevent: text-delta\ndata: {"text":"先查一下"}\n');
  });

  it("逐字符喂也拼得回同样的帧（与一次喂完逐字相同）", () => {
    const whole = TEXT_1 + TOOL_CALL + TOOL_RESULT_FAIL + DONE;
    expect(feed([...whole]).frames).toEqual(feed([whole]).frames);
    expect(feed([...whole]).rest).toBe("");
  });

  it("任意切分点都不丢帧：两帧串在每个位置切一刀，结果与一次喂完相同", () => {
    const whole = TEXT_2 + TOOL_RESULT_OK;
    const once = feed([whole]).frames;
    expect(once).toHaveLength(2);
    for (let i = 0; i <= whole.length; i += 1) {
      const r = feed([whole.slice(0, i), whole.slice(i)]);
      expect(r.frames, `切在 ${i}`).toEqual(once);
      expect(r.rest, `切在 ${i}`).toBe("");
    }
  });

  it("rest 永远是已收字节的后缀：消费掉的 + 留下的 = 收到的", () => {
    const whole = TEXT_1 + TOOL_CALL_NO_ARGS + SYSTEM;
    const chunks = ragged(whole);
    let buf = "";
    let seen = "";
    for (const chunk of chunks) {
      seen += chunk;
      buf = pump(buf, chunk).rest;
      // 只要 rest 始终是 seen 的后缀，就说明没有字节被吃掉、也没有被重复留着
      expect(seen.endsWith(buf)).toBe(true);
    }
    expect(buf).toBe("");
    expect(feed(chunks).frames).toEqual(feed([whole]).frames);
  });

  it("代理对（emoji）被切在两半也不坏：缓冲是串级的，拼回来再解析", () => {
    const whole = 'id: 4\nevent: text-delta\ndata: {"text":"📖 已归档"}\n\n';
    const at = whole.indexOf("📖") + 1; // 落在高位代理之后、低位代理之前
    expect(feed([whole.slice(0, at), whole.slice(at)]).frames).toEqual([
      { id: 4, event: "text-delta", data: { text: "📖 已归档" } },
    ]);
  });

  it("CRLF 帧不解析，整串原样留在 rest", () => {
    // 为什么不顺手把 \r\n 归一成 \n：后端 _frame 写死了 \n，真收到 CRLF 就说明中间有东西
    // 改写过这条流。放宽分隔符的代价是"猜"——\r\n\r\n 里没有 \n\n，一旦认它当帧界，
    // 截断在 \r 上的半截 data 也可能被当成整帧解出来，人会看见半句正文而不是"没收到"。
    // 留在 rest 里，上层看得见"一帧都没出来"，这比静悄悄解错好。
    const r = pump("", CRLF);
    expect(r.frames).toEqual([]);
    expect(r.rest).toBe(CRLF);
    expect(pump(CRLF, "").frames).toEqual([]); // 再喂空串也不会自己解出来
  });

  it("CRLF 半帧后面跟一条正常 LF 帧：CRLF 那半不会浮成正文", () => {
    const evs = eventsOf([CRLF + TEXT_2]);
    expect(evs.filter((e) => e.kind === "text")).toEqual([]);
    // 合并成一个块后 data 解不动 → 「服务端帧坏了」，而不是把 CRLF 里的正文放出来
    expect(evs).toEqual([{ kind: "banner", text: "服务端帧坏了", level: "err" }]);
  });

  it("没有整数 id 的块不是帧：丢掉，不留半帧", () => {
    // id 是承重的（Last-Event-ID 全靠它），所以 Frame.id 恒为安全非负整数；
    // 解不出整数 id 的块宁可不发帧，也不把一个坏 id 交给续读。
    // /^\d+$/ 是 ASCII 口径，正好躲开后端 R-S2-23 踩过的坑：全角"１２"在 Python 里
    // isdigit() 为真、int() 却抛；JS 的 \d 压根不认它。
    const bad = [
      'event: text-delta\ndata: {"text":"x"}\n\n', // 没有 id 行
      'id: -1\nevent: done\ndata: {}\n\n', // 负数
      'id: 1.5\nevent: done\ndata: {}\n\n', // 小数
      'id: 1e3\nevent: done\ndata: {}\n\n', // 科学计数
      'id: 9007199254740993\nevent: done\ndata: {}\n\n', // 2^53+1，JS 里已经不准
      'id: １２\nevent: done\ndata: {}\n\n', // 全角数字
      'id: 4 \nevent: done\ndata: {}\n\n', // 尾随空格
    ];
    for (const block of bad) {
      expect(pump("", block).frames, block).toEqual([]);
      expect(pump("", block).rest, block).toBe("");
    }
  });

  it("冒号后没有空格也认（SSE 规范允许），一个空格不算值的一部分", () => {
    expect(pump("", 'id:4\nevent:text-delta\ndata:{"text":"x"}\n\n').frames).toEqual([
      { id: 4, event: "text-delta", data: { text: "x" } },
    ]);
  });

  it("data 拆成多行按规范用 \\n 拼接再解析（后端不发这种，防哪天改成 pretty-print）", () => {
    expect(pump("", 'id: 4\nevent: text-delta\ndata: {"text":\ndata: "先查一下"}\n\n').frames).toEqual([
      { id: 4, event: "text-delta", data: { text: "先查一下" } },
    ]);
  });

  it("注释行与不认得的字段（retry 等）忽略：后端故意不发 retry", () => {
    expect(pump("", ": keep-alive\n\n").frames).toEqual([]);
    expect(pump("", 'id: 4\nretry: 3000\nevent: text-delta\ndata: {"text":"x"}\n\n').frames).toEqual([
      { id: 4, event: "text-delta", data: { text: "x" } },
    ]);
  });

  it("不认得的 event 照样出帧（映射那一步才决定要不要理它）", () => {
    expect(pump("", 'id: 4\nevent: heartbeat\ndata: {}\n\n').frames).toEqual([
      { id: 4, event: "heartbeat", data: {} },
    ]);
  });

  it("data 的 JSON 坏了不抛：原串留在 data 上，交给 toUiEvents 判", () => {
    const r = pump("", 'id: 8\nevent: text-delta\ndata: {"text":"半截\n\n');
    expect(r.frames).toEqual([{ id: 8, event: "text-delta", data: '{"text":"半截' }]);
  });

  it("没有 data 行的帧：data 为 null（不是 undefined，形状得稳）", () => {
    expect(pump("", "id: 8\nevent: done\n\n").frames).toEqual([
      { id: 8, event: "done", data: null },
    ]);
  });
});

describe("toUiEvents — 六种帧到 UI 事件", () => {
  it("text-delta → text（一帧一个事件）", () => {
    expect(toUiEvents({ id: 4, event: "text-delta", data: { text: "先查一下" } })).toEqual([
      { kind: "text", text: "先查一下" },
    ]);
  });

  it("text-delta 序列 → 正文按到达顺序累积成整句", () => {
    const evs = eventsOf(ragged(TEXT_1 + TEXT_2));
    expect(evs.flatMap((e) => (e.kind === "text" ? [e.text] : []))).toEqual([
      "先查一下",
      "已有的页面",
    ]);
    expect(evs.flatMap((e) => (e.kind === "text" ? [e.text] : [])).join("")).toBe(
      "先查一下已有的页面",
    );
  });

  it("tool-call（带 args）→ tool，args 是 payload 里的原串", () => {
    expect(toUiEvents(feed([TOOL_CALL]).frames[0]!)).toEqual([
      { kind: "tool", id: "call_9a1", name: "search", args: '{"q":"新传专业产品经理转型指南"}' },
    ]);
  });

  it("tool-call 缺 args 键（空参工具）→ tool，args 为空串而不是 undefined", () => {
    expect(
      toUiEvents({ id: 3000009, event: "tool-call", data: { id: "call_9a2", name: "list_members" } }),
    ).toEqual([{ kind: "tool", id: "call_9a2", name: "list_members", args: "" }]);
  });

  it("tool-call 缺 id 键（SDK 没交 call id 的兜底形状）→ tool，id 为空串", () => {
    expect(toUiEvents({ id: 1, event: "tool-call", data: { name: "search", args: "{}" } })).toEqual([
      { kind: "tool", id: "", name: "search", args: "{}" },
    ]);
  });

  it("tool-result → toolresult，ok=false 时 summary 是中文失败原因", () => {
    expect(toUiEvents(feed([TOOL_RESULT_FAIL]).frames[0]!)).toEqual([
      {
        kind: "toolresult",
        id: "call_9a1",
        name: "search",
        ok: false,
        summary: "检索没跑起来：库锁住了",
      },
    ]);
  });

  it("tool-call 与 tool-result 按 payload 的 id 配对", () => {
    const evs = eventsOf([TOOL_CALL_NO_ARGS, TOOL_RESULT_OK]);
    const calls = evs.flatMap((e) => (e.kind === "tool" ? [e] : []));
    const results = evs.flatMap((e) => (e.kind === "toolresult" ? [e] : []));
    expect(calls).toHaveLength(1);
    expect(results).toHaveLength(1);
    expect(results[0]!.id).toBe(calls[0]!.id);
    expect(results[0]!.id).toBe("call_9a2");
    expect(results[0]!.name).toBe("list_members");
    expect(results[0]!.ok).toBe(true);
  });

  it("system → banner warn（额度那条就是这么来的，没有 code 可 switch）", () => {
    expect(toUiEvents(feed([SYSTEM]).frames[0]!)).toEqual([
      { kind: "banner", text: QUOTA_TEXT, level: "warn" },
    ]);
  });

  it("error{code:upstream} → banner err，且**不**结束流", () => {
    const evs = toUiEvents(feed([ERROR_UPSTREAM]).frames[0]!);
    expect(evs).toEqual([{ kind: "banner", text: UPSTREAM_TEXT, level: "err" }]);
    expect(evs.some((e) => e.kind === "end")).toBe(false);
  });

  it("error 缺 code（后端不发这种）→ 用 payload 的 text 出横幅，按非终止处理", () => {
    // text 是给人看的那一半，code 只是"要不要收流"的开关：缺开关就当不收流，
    // 但绝不把人话换成「服务端帧坏了」——那句是留给连 text 都读不出来的帧的。
    const evs = toUiEvents({ id: 13, event: "error", data: { text: UPSTREAM_TEXT } });
    expect(evs).toEqual([{ kind: "banner", text: UPSTREAM_TEXT, level: "err" }]);
    expect(evs.some((e) => e.kind === "end")).toBe(false);
  });

  it("error{code:stream_timeout} → banner err 之后 end（先给人看见，再收流）", () => {
    const evs = toUiEvents(feed([ERROR_TIMEOUT]).frames[0]!);
    expect(evs).toEqual([
      { kind: "banner", text: STREAM_TIMEOUT_TEXT, level: "err" },
      { kind: "end" },
    ]);
    // 第一个事件必须是横幅（人先看见那句话），end 排在它后面：顺序就是渲染顺序
    expect(evs[0]).toEqual({ kind: "banner", text: STREAM_TIMEOUT_TEXT, level: "err" });
  });

  it("done → end", () => {
    expect(toUiEvents(feed([DONE]).frames[0]!)).toEqual([{ kind: "end" }]);
    // done 的 payload 前端一个字都不消费：message_id 由 Chat 自己从帧上取（doneMessageId）
    expect(toUiEvents({ id: 1, event: "done", data: { message_id: 88, usage: null } })).toEqual([
      { kind: "end" },
    ]);
  });

  it("不认得的 event → 空数组（没有 UI 事件，也不结束）", () => {
    for (const event of ["heartbeat", "message", "", "text_delta", "Done"]) {
      expect(toUiEvents({ id: 1, event, data: {} }), event).toEqual([]);
      expect(toUiEvents({ id: 1, event, data: {} })[0], event).toBeUndefined();
    }
  });
});

describe("toUiEvents — 坏帧：不抛、出人话横幅、不结束流", () => {
  const broken: Frame[] = [
    // pump 把解不动的 data 原串留着，所以这一条是真会发生的形状（半截 JSON）
    { id: 1, event: "text-delta", data: '{"text":"半截' },
    { id: 2, event: "text-delta", data: { text: 42 } },
    { id: 3, event: "text-delta", data: null },
    { id: 4, event: "text-delta", data: ["先查一下"] },
    { id: 5, event: "tool-call", data: { id: "c", name: 7 } },
    { id: 6, event: "tool-call", data: { name: "search", args: { q: "x" } } }, // args 不是串
    { id: 7, event: "tool-result", data: { id: "c", name: "search", ok: "true", summary: "s" } },
    { id: 8, event: "tool-result", data: { id: "c", name: "search", ok: true } }, // 缺 summary
    { id: 9, event: "system", data: { text: 1 } },
    { id: 10, event: "system", data: "额度用完了" }, // 形状不对：串不是对象
    // 读不出 text 的 error：哪怕 code 是 stream_timeout 也不许结束流——
    // 提前收流的代价（这一轮剩下的正文全丢，且按 R-S2-25 不许重连）比挂着大得多
    { id: 11, event: "error", data: { code: "stream_timeout" } },
    { id: 12, event: "error", data: "boom" },
    // done 的 payload 前端并不消费，但"读不出 payload 就不结束"这条口径统一适用（见报告）
    { id: 14, event: "done", data: "oops" },
    { id: 15, event: "done", data: null },
  ];

  for (const f of broken) {
    it(`${f.event} #${f.id} 的坏 data → 「服务端帧坏了」且不 end`, () => {
      expect(() => toUiEvents(f)).not.toThrow();
      expect(toUiEvents(f)).toEqual([
        { kind: "banner", text: "服务端帧坏了", level: "err" },
      ]);
    });
  }
});

describe("resumeHeaders — Last-Event-ID", () => {
  it("null（第一次连流）→ 空头，让服务端整轮重放", () => {
    expect(resumeHeaders(null)).toEqual({});
  });

  it("有 lastId → 一个 Last-Event-ID 头，值是十进制整数串", () => {
    expect(resumeHeaders(4)).toEqual({ "Last-Event-ID": "4" });
    expect(resumeHeaders(1000009)).toEqual({ "Last-Event-ID": "1000009" });
    expect(resumeHeaders(9007199254740991)).toEqual({
      "Last-Event-ID": "9007199254740991",
    });
  });

  it("0 是合法 id（等于「从头重放」），照发", () => {
    expect(resumeHeaders(0)).toEqual({ "Last-Event-ID": "0" });
  });

  it("非整数一律不发：后端解不动会当 0，解得动但越界是 400「续读位置不合法」", () => {
    const bad = [
      1.5,
      -1,
      Number.NaN,
      Number.POSITIVE_INFINITY,
      Number.NEGATIVE_INFINITY,
      2 ** 53, // Number.MAX_SAFE_INTEGER + 1：JS 里已经表示不准
      2 ** 53 + 2,
    ];
    for (const id of bad) expect(resumeHeaders(id), String(id)).toEqual({});
  });

  it("从 pump 出来的任何 Frame.id 都直接可发（不会铸出非整数）", () => {
    const whole = TEXT_1 + TOOL_CALL + TOOL_RESULT_FAIL + SYSTEM + ERROR_UPSTREAM + DONE;
    for (const f of feed(ragged(whole)).frames) {
      expect(Number.isSafeInteger(f.id), `${f.id}`).toBe(true);
      expect(f.id).toBeGreaterThanOrEqual(0);
      expect(resumeHeaders(f.id)).toEqual({ "Last-Event-ID": String(f.id) });
    }
  });

  it("JS 调用方手滑传进串或 undefined 也不发（运行时兜底，不只靠类型）", () => {
    const sneaky = resumeHeaders as (v: unknown) => Record<string, string>;
    expect(sneaky("4")).toEqual({});
    expect(sneaky(undefined)).toEqual({});
    expect(sneaky({})).toEqual({});
  });
});

describe("一整轮走一遍（乱切 chunk）", () => {
  const whole =
    TEXT_1 + TEXT_2 + TOOL_CALL + TOOL_RESULT_FAIL + TOOL_CALL_NO_ARGS + TOOL_RESULT_OK + SYSTEM + DONE;

  it("正文累积、工具按 id 配对、system 出琥珀横幅、done 收尾且只收一次", () => {
    const evs = eventsOf(ragged(whole));
    expect(evs.flatMap((e) => (e.kind === "text" ? [e.text] : [])).join("")).toBe(
      "先查一下已有的页面",
    );

    const callIds = evs.flatMap((e) => (e.kind === "tool" ? [e.id] : []));
    const resultIds = evs.flatMap((e) => (e.kind === "toolresult" ? [e.id] : []));
    expect(callIds).toEqual(["call_9a1", "call_9a2"]);
    expect(resultIds).toEqual(["call_9a1", "call_9a2"]); // 每个 result 都能按 id 找回它的 call
    expect(evs.flatMap((e) => (e.kind === "tool" ? [e.name] : []))).toEqual([
      "search",
      "list_members",
    ]);
    expect(evs.flatMap((e) => (e.kind === "toolresult" ? [e.ok] : []))).toEqual([false, true]);

    expect(evs.filter((e) => e.kind === "banner")).toEqual([
      { kind: "banner", text: QUOTA_TEXT, level: "warn" },
    ]);
    expect(evs.filter((e) => e.kind === "end")).toEqual([{ kind: "end" }]);
    expect(evs.at(-1)).toEqual({ kind: "end" }); // done 是最后一个事件
  });

  it("stream_timeout 收尾：横幅之后 end，之后不再有事件", () => {
    const evs = eventsOf(ragged(TEXT_1 + TOOL_CALL_NO_ARGS + ERROR_TIMEOUT));
    expect(evs.slice(-2)).toEqual([
      { kind: "banner", text: STREAM_TIMEOUT_TEXT, level: "err" },
      { kind: "end" },
    ]);
    expect(evs.filter((e) => e.kind === "end")).toHaveLength(1);
  });

  it("非终止 error 之后流还开着：upstream 横幅之后还能继续收正文与 done", () => {
    const evs = eventsOf(ragged(ERROR_UPSTREAM + TEXT_2 + DONE));
    expect(evs).toEqual([
      { kind: "banner", text: UPSTREAM_TEXT, level: "err" },
      { kind: "text", text: "已有的页面" },
      { kind: "end" },
    ]);
  });
});

describe("system 的 outcome（2026-10-30 收尾成果）", () => {
  it("带 kind=outcome → outcome 事件，不是横幅", () => {
    const evs = toUiEvents({
      id: 6000001,
      event: "system",
      data: {
        kind: "outcome",
        pages: [{ path: "concepts/a.md", action: "create" }],
        citations: 18,
        lint_errors: 0,
      },
    });
    expect(evs).toEqual([
      {
        kind: "outcome",
        outcome: {
          pages: [{ path: "concepts/a.md", action: "create" }],
          citations: 18,
          lint_errors: 0,
        },
      },
    ]);
  });

  it("形状不对 → 坏帧横幅，绝不静默丢掉", () => {
    const evs = toUiEvents({ id: 6000002, event: "system", data: { kind: "outcome" } });
    expect(evs).toEqual([{ kind: "banner", text: BROKEN_FRAME_TEXT, level: "err" }]);
  });
});
