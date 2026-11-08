// 重连口径的**行为**测试（裁定 R-S3-10 / R-S3-12 / R-S2-25 + S3-T3 的停摆守卫 M3）。
//
// 这一份原来钉的是 pages/Chat.tsx 的源码字符串：重试环长在组件里，要 fetch/AbortController/
// React 状态才跑得起来，而工程里没有 jsdom（也不为一个测试引依赖）。S4-T2 把那一环抽进了
// stream.ts 并把开流做成了注入点（Opener），于是这些钉子换成了真跑：交一个假 opener，
// 它返回脚本化的 ReadableStream，断言**开了几次流、带了什么头、出了几句横幅**。
// 源码改了形状但行为没变，这一份不该红；行为变了，钉源码那份也未必红——所以行为测试更值。
//
// 纯函数那一层（isTransientStatus）照旧直接单测。
import { describe, expect, it } from "vitest";

import { isTransientStatus } from "./sse.ts";
import { runTurnStream, STALL_TEXT } from "./stream.ts";
import type { Opener } from "./stream.ts";

// —— 造流的小工具 ————————————————————————————————————————————————

/** 一帧 SSE，形状照抄后端 app/routers/api/chat.py 的 _frame。 */
function frame(id: number, event: string, data: unknown): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
const text = (id: number, t: string) => frame(id, "text-delta", { text: t });
const done = (id: number, messageId: number) => frame(id, "done", { message_id: messageId, usage: null });
/** 终止性的那一帧：正文半截就断了（sse.ts 把它翻成「横幅 + end」）。 */
const timedOut = (id: number, t: string) => frame(id, "error", { code: "stream_timeout", text: t });

const TIMEOUT_TEXT = "这条流等得太久了，先断开。刷新一下看最新内容，或把刚才那句重发一次。";
const HUMAN_TEXT = "刚才人说的那句原文";
const URL = "/api/chat/3/stream?after=900000001";

/** 一段**走完就关**的流：不发 end 帧，形状就是"服务端/代理把连接静默关掉了"。 */
function closedAfter(chunks: string[], status = 200): Response {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        for (const s of chunks) c.enqueue(enc.encode(s));
        c.close();
      },
    }),
    { status, headers: { "Content-Type": "text/event-stream" } },
  );
}

/**
 * 一段**挂着不放**的流：先交 chunks 里那几帧，然后一直不关——直到 signal 中止才 errored。
 * 真 fetch 就是这个行为（abort 之后 reader.read() 抛 AbortError），假 opener 必须认这个
 * 信号，否则"中止不重试"与"停摆守卫"两条根本测不出来（它们都靠 abort 把 read 掐断）。
 */
function hanging(chunks: string[], signal: AbortSignal, status = 200): Response {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        for (const s of chunks) c.enqueue(enc.encode(s));
        if (signal.aborted) c.error(new Error("aborted"));
        else
          signal.addEventListener(
            "abort",
            () => {
              try {
                c.error(new Error("aborted"));
              } catch {
                // 已经关掉了：重复 error 会抛，那不算失败
              }
            },
            { once: true },
          );
      },
    }),
    { status, headers: { "Content-Type": "text/event-stream" } },
  );
}

/** 一段**读一半就坏**的流：reader.read() 抛，形状是"reader 报错"那条路。 */
function errored(status = 200): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        c.error(new Error("boom"));
      },
    }),
    { status, headers: { "Content-Type": "text/event-stream" } },
  );
}

/**
 * 一个非 2xx 的响应，body 能不能被读到"取消"了看得出来（runner 该 cancel 掉它）。
 *
 * 给了 message 就把它当 JSON 信封交出去并**关掉**流（400/404 那条路要 readErrorEnvelope
 * 读得出后端那句人话）；不给就**不关**——真的 5xx 常常是半截停在那儿的 body，而流已经
 * 关掉的话 `cancel()` 按规范压根不会调到 source 的 cancel，那条断言就成了永远绿。
 */
function refused(status: number, message?: string): { res: Response; cancelled: () => boolean } {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      if (message === undefined) return; // 挂着不关：好让 cancel 被看得见
      c.enqueue(new TextEncoder().encode(JSON.stringify({ error: message })));
      c.close();
    },
    cancel() {
      cancelled = true;
    },
  });
  return { res: new Response(body, { status, headers: { "Content-Type": "application/json" } }), cancelled: () => cancelled };
}

// —— 跑一趟 ——————————————————————————————————————————————————————

interface Log {
  url: string;
  headers: Record<string, string>;
  at: number;
}

interface Run {
  outcome: Awaited<ReturnType<typeof runTurnStream>>;
  notices: { text: string; level: "warn" | "err"; resend?: string }[];
  calls: Log[];
  blockReports: number;
  unauthorized: number;
  ctrl: AbortController;
  elapsed: number;
}

/**
 * 跑一轮，把界面上能看见的三样东西记下来：横幅（onNotice）、live 区被刷了几次
 * （onBlocks）、以及开了几次流（假 opener 被调了几次、各自带了什么头）。
 *
 * backoffMs 一律 0：退避是"等 600 毫秒"，测试里等它没有意义，而"退避期间被中止就不该
 * 再试"那条由 goStale/abort 自己验（backoff 的判据是 signal.aborted || stale()）。
 */
async function run(open: Opener, opts: { stallMs?: number; retries?: number } = {}): Promise<Run> {
  const ctrl = new AbortController();
  const notices: Run["notices"] = [];
  const calls: Log[] = [];
  let blockReports = 0;
  let unauthorized = 0;
  const stale = false;
  const t0 = Date.now();

  const outcome = await runTurnStream({
    url: URL,
    ctrl,
    humanText: HUMAN_TEXT,
    stale: () => stale,
    onBlocks: () => {
      blockReports += 1;
    },
    onNotice: (text, level = "err", resend) => {
      notices.push({ text, level, resend });
    },
    onUnauthorized: () => {
      unauthorized += 1;
    },
    open: (url, headers, signal) => {
      calls.push({ url, headers, at: Date.now() - t0 });
      return open(url, headers, signal);
    },
    backoffMs: 0,
    ...opts,
  });

  return {
    outcome,
    notices,
    calls,
    blockReports,
    unauthorized,
    ctrl,
    elapsed: Date.now() - t0,
  };
}

/** 按趟数发不同响应的 opener。用完了就重复最后一个（"一直这样"）。 */
function scripted(...responses: ((signal: AbortSignal, attempt: number) => Response)[]): Opener {
  let n = 0;
  return (_url, _headers, signal) => {
    const make = responses[Math.min(n, responses.length - 1)]!;
    return Promise.resolve(make(signal, n++));
  };
}

// —— 一、纯函数那一层 ————————————————————————————————————————————

describe("isTransientStatus：只有 5xx 算瞬时", () => {
  it.each([500, 502, 503, 504, 599])("%i → 值得再试", (status) => {
    expect(isTransientStatus(status)).toBe(true);
  });

  it.each([400, 401, 403, 404, 409, 422, 499])("%i → 后端想清楚了，立刻摆人话", (status) => {
    expect(isTransientStatus(status)).toBe(false);
  });

  it("2xx 与 0（压根没连上）都不算'瞬时状态码'", () => {
    // 2xx 走不到这个判定；0 是 fetch 抛异常的形状，由 reader 那条路（catch）处理，
    // 那里同样消耗预算，判据不是状态码。
    expect(isTransientStatus(200)).toBe(false);
    expect(isTransientStatus(0)).toBe(false);
  });
});

// —— 二、干净的 end：绝不重连 ——————————————————————————————————————

describe("干净的 end（done / stream_timeout）：这一轮到此为止，绝不重连（R-S2-25）", () => {
  it("done 帧收的尾：一次连接就够，doneId 交回去给收尾对齐", async () => {
    const r = await run(scripted(() => closedAfter([text(12, "先查了全站，"), done(13, 88)])));
    expect(r.calls).toHaveLength(1);
    expect(r.outcome.ended).toBe(true);
    expect(r.outcome.doneId).toBe(88);
    expect(r.notices).toEqual([]);
    // 正文折成一个块；live 区只被刷了一次——done 帧翻出来的是 end，没有可渲染的事件
    expect(r.blockReports).toBe(1);
    expect(r.outcome.blocks).toEqual([{ kind: "text", text: "先查了全站，" }]);
  });

  it("stream_timeout 也是终止：一句页面级横幅带着「重发」的原文，且不开第二次流", async () => {
    const r = await run(
      scripted(() => closedAfter([text(7, "正在检索全站…"), timedOut(8, TIMEOUT_TEXT)])),
    );
    expect(r.calls).toHaveLength(1);
    expect(r.outcome.ended).toBe(true);
    expect(r.outcome.doneId).toBe(null); // 半截断掉，库里没有这一轮的 agent 行
    expect(r.notices).toEqual([{ text: TIMEOUT_TEXT, level: "err", resend: HUMAN_TEXT }]);
  });

  it("首趟开流不带 Last-Event-ID（新开一轮没有断点要续，跨轮重连本来就不该带）", async () => {
    const r = await run(scripted(() => closedAfter([done(1, 7)])));
    expect(r.calls[0]!.headers).toEqual({});
    expect(r.calls[0]!.url).toBe(URL);
  });
});

// —— 三、没接上：带游标续读，预算两次 ————————————————————————————

describe("没接上：带 Last-Event-ID 续读，至多两次重试（加首趟一共三次连接，R-S3-10）", () => {
  it("第一趟被静默关掉、第二趟接上：一共开两次流，第二次带着上一趟最后一帧的 id", async () => {
    const r = await run(
      scripted(
        () => closedAfter([text(1000012, "前半句")]), // 没有 end：连接被关了
        () => closedAfter([text(2000030, "后半句"), done(2000040, 91)]),
      ),
    );
    expect(r.calls).toHaveLength(2);
    expect(r.calls[0]!.headers).toEqual({});
    expect(r.calls[1]!.headers).toEqual({ "Last-Event-ID": "1000012" });
    expect(r.outcome.ended).toBe(true);
    expect(r.outcome.doneId).toBe(91);
    expect(r.notices).toEqual([]);
    // 续读天然去重：两趟的正文接成一句，没有重的那半截
    expect(r.outcome.blocks).toEqual([{ kind: "text", text: "前半句后半句" }]);
  });

  it("三趟都没接上：只出**一句**「连接没响应」，不开第四次", async () => {
    const r = await run(scripted(() => closedAfter([text(5, "半截")])));
    expect(r.calls).toHaveLength(3);
    expect(r.calls.map((c) => c.headers)).toEqual([
      {},
      { "Last-Event-ID": "5" },
      { "Last-Event-ID": "5" },
    ]);
    expect(r.notices).toEqual([{ text: STALL_TEXT, level: "err", resend: undefined }]);
    expect(r.outcome.ended).toBe(false);
  });

  it("reader 报错走同一个预算、同一句话（R-S3-12 撤掉了按形状分叉的文案）", async () => {
    const r = await run(scripted(() => errored()));
    expect(r.calls).toHaveLength(3);
    expect(r.notices).toEqual([{ text: STALL_TEXT, level: "err", resend: undefined }]);
  });

  it("开流 5xx 算瞬时：消耗预算续读，且那一趟的响应体被 cancel 掉（不占着连接不放）", async () => {
    const bad = refused(503);
    const r = await run(
      scripted(
        () => bad.res,
        () => closedAfter([text(21, "接上了"), done(22, 92)]),
      ),
    );
    expect(r.calls).toHaveLength(2);
    expect(bad.cancelled()).toBe(true);
    expect(r.notices).toEqual([]);
    expect(r.outcome.ended).toBe(true);
    expect(r.outcome.blocks).toEqual([{ kind: "text", text: "接上了" }]);
  });

  it("5xx 三趟都来：预算用完，还是那一句「连接没响应」（不读它的 body，所以没有别的话可摆）", async () => {
    const r = await run(scripted(() => refused(502).res));
    expect(r.calls).toHaveLength(3);
    expect(r.notices).toEqual([{ text: STALL_TEXT, level: "err", resend: undefined }]);
  });
});

// —— 四、被明确拒的：一次都不试 ——————————————————————————————————

describe("开流就被明确拒了：不重试、不用那句兜底横幅（后端想清楚了的答案原样摆）", () => {
  it("400（after 不合法）：一次连接，横幅是后端那句人话", async () => {
    const r = await run(scripted(() => refused(400, "续读位置不合法，刷新一下再发一次").res));
    expect(r.calls).toHaveLength(1);
    expect(r.notices).toEqual([
      { text: "续读位置不合法，刷新一下再发一次", level: "err", resend: undefined },
    ]);
    expect(r.outcome.ended).toBe(false);
  });

  it("404（会话没了）：同上，一次都不试", async () => {
    const r = await run(scripted(() => refused(404, "这场会话不在了").res));
    expect(r.calls).toHaveLength(1);
    expect(r.notices).toEqual([{ text: "这场会话不在了", level: "err", resend: undefined }]);
  });

  it("401：不重试、不出横幅，交给调用方清身份去登录（toLogin）", async () => {
    const r = await run(scripted(() => refused(401).res));
    expect(r.calls).toHaveLength(1);
    expect(r.unauthorized).toBe(1);
    expect(r.notices).toEqual([]);
    expect(r.outcome.toLogin).toBe(true);
  });
});

// —— 五、中止与作废：都不许再试 ————————————————————————————————

describe("中止与作废：这一轮已经不算数了，一次都不再试", () => {
  it("被中止（换会话 / 卸载 / 停摆守卫掐的）：reader 抛了也不重试、不出横幅", async () => {
    const ctrl = new AbortController();
    const notices: string[] = [];
    const calls: number[] = [];
    await runTurnStream({
      url: URL,
      ctrl,
      humanText: HUMAN_TEXT,
      stale: () => false,
      onBlocks: () => {},
      onNotice: (t) => notices.push(t),
      backoffMs: 0,
      open: (_u, _h, signal) => {
        calls.push(1);
        // 真形状是"人点了侧栏另一场会话"：这一趟开出来 20ms 之后整轮被掐，
        // read() 随之抛 AbortError —— 那条 catch 的第一句判据就是 signal.aborted
        setTimeout(() => ctrl.abort(), 20);
        return Promise.resolve(hanging([text(31, "半句")], signal));
      },
    });
    expect(calls).toHaveLength(1);
    expect(notices).toEqual([]);
    expect(ctrl.signal.aborted).toBe(true);
  });

  it("作废（stale）之后：不再开流，也不再出横幅", async () => {
    const ctrl = new AbortController();
    const notices: string[] = [];
    const calls: number[] = [];
    let stale = false;
    await runTurnStream({
      url: URL,
      ctrl,
      humanText: HUMAN_TEXT,
      stale: () => stale,
      onBlocks: () => {},
      onNotice: (t) => notices.push(t),
      backoffMs: 0,
      open: () => {
        calls.push(1);
        stale = true; // 开流这一下人就走到另一场会话上去了
        return Promise.resolve(refused(503).res);
      },
    });
    // 503 本来要退避再试；这一轮已经作废，退避回来第一件事就是判 stale 然后收工
    expect(calls).toHaveLength(1);
    expect(notices).toEqual([]);
    ctrl.abort();
  });

  it("作废之后帧也不再上报（迟到的正文不许动新会话的 live 区）", async () => {
    let stale = false;
    let reports = 0;
    const ctrl = new AbortController();
    await runTurnStream({
      url: URL,
      ctrl,
      humanText: HUMAN_TEXT,
      stale: () => stale,
      onBlocks: () => {
        reports += 1;
        stale = true; // 第一帧之后就换会话了
      },
      onNotice: () => {},
      backoffMs: 0,
      // 三帧连着来、然后连接被关掉：作废之后剩下那两帧不许再上报，也不许因为
      // "没见着 end" 就去续读（stale 那一下就该收工）
      open: () => Promise.resolve(closedAfter([text(41, "一"), text(42, "二"), text(43, "三")])),
    });
    expect(reports).toBe(1);
    ctrl.abort();
  });
});

// —— 六、停摆守卫 ————————————————————————————————————————————————

describe("停摆守卫：一趟开流后 stallMs 内一帧都没到 → 一句横幅 + 掐掉这一轮", () => {
  it("停摆不触发重试（那条连接是被自己掐的），横幅只有那一句", async () => {
    const ctrl = new AbortController();
    const notices: string[] = [];
    const calls: number[] = [];
    await runTurnStream({
      url: URL,
      ctrl,
      humanText: HUMAN_TEXT,
      stale: () => false,
      onBlocks: () => {},
      onNotice: (t) => notices.push(t),
      backoffMs: 0,
      stallMs: 40,
      open: (_u, _h, signal) => {
        calls.push(1);
        return Promise.resolve(hanging([], signal)); // 一帧都不来
      },
    });
    expect(calls).toHaveLength(1);
    expect(notices).toEqual([STALL_TEXT]);
    expect(ctrl.signal.aborted).toBe(true); // reader 被掐了，连接不挂着
  });

  it("闹钟是**每趟一个**：重试把守卫重新武装，第二趟的停摆从第二趟起算", async () => {
    const t0 = Date.now();
    const calls: number[] = [];
    const stalls: { text: string; at: number }[] = [];
    const ctrl = new AbortController();
    await runTurnStream({
      url: URL,
      ctrl,
      humanText: HUMAN_TEXT,
      stale: () => false,
      onBlocks: () => {},
      onNotice: (t) => stalls.push({ text: t, at: Date.now() - t0 }),
      backoffMs: 0,
      stallMs: 60,
      open: (_u, _h, signal) => {
        calls.push(Date.now() - t0);
        // 第一趟：来一帧、20ms 后被静默关掉 —— 守卫该被这一帧往后推，不该在这一趟响
        if (calls.length === 1) {
          return Promise.resolve(
            new Response(
              new ReadableStream<Uint8Array>({
                start(c) {
                  c.enqueue(new TextEncoder().encode(text(51, "来了一帧")));
                  setTimeout(() => c.close(), 20);
                },
              }),
              { status: 200, headers: { "Content-Type": "text/event-stream" } },
            ),
          );
        }
        // 第二趟：一帧都不来 —— 响的必须是这一趟自己的闹钟
        return Promise.resolve(hanging([], signal));
      },
    });
    expect(calls).toHaveLength(2);
    expect(stalls.map((s) => s.text)).toEqual([STALL_TEXT]);
    // 停摆那一下发生在第二趟开流之后至少 stallMs：闹钟确实是第二趟重新武装的那个
    expect(stalls[0]!.at).toBeGreaterThanOrEqual(calls[1]! + 55);
    expect(ctrl.signal.aborted).toBe(true);
  });
});
