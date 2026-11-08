// 流式回合 runner：一轮的开流、续读重试、停摆守卫与"这一轮怎么算完"。
//
// 从 pages/Chat.tsx 抽出来（S4-T2），为的是两件事：
//   1. 这一半是**纯流程**（fetch/AbortController/帧折叠），与 React 状态无关。抽出来之后
//      重试预算、退避、停摆、续读游标这些口径能直接单测——注入一个假 opener 返回脚本化的
//      ReadableStream 就行（reconnect.test.ts），不必再钉源码字符串；
//   2. 对话页只剩"页面"的事：状态、会话 CRUD、上传、渲染。
//
// 契约（S2/S3 定的，别在这儿重新发明；sse.ts 文件头是另一半）：
//
//   POST messages（**表单**，见 pages/Chat.tsx 的 postMessage）拿 mid → 立刻开
//   stream?after=mid → 逐帧 pump + toUiEvents 渲染 → end 之后主动关流、重拉详情对齐落库态。
//
// 两种"结束"必须分清（裁定 R-S3-10 + R-S3-12）：
//   - **干净的 end**（done 帧 / stream_timeout）：这一轮到此为止，绝不重连（R-S2-25——
//     done 之后带着 Last-Event-ID 再连，拿到的只是整轮重放或又一个 done，纯空转，还会把
//     这一轮的陈旧游标交到下一轮手上）；
//   - **没接上**（reader 报错、服务端/代理把连接关了而 end 还没来、开流拿到 5xx）：带
//     resumeHeaders(lastSeenId) 续读重试，至多 STREAM_RETRIES 次、每次退避
//     RETRY_BACKOFF_MS。续读游标天然去重（服务端从 lastSeenId 之后接着发），所以重试
//     不会让人看见两遍正文；预算用完才出横幅，而横幅**只有一句**「连接没响应」
//     （R-S3-12 撤掉了按形状分叉的两套文案：reader 报错与被静默关掉，对人来说是同一件事）。
//     开流就被**明确拒**的（400 after 不合法 / 401 会话没了 / 404 会话没了）不重试也不
//     用那句横幅——那是后端想清楚了的答案，原样摆出来（401 还要清身份去登录）；
//     5xx 不在此列，它算瞬时，照样消耗预算继续试（sse.ts 的 isTransientStatus）。
//
// 停摆守卫（S3-T3 评审留下的 M3）：每一趟开流后 stallMs 内一帧都没到 → 横幅「连接没响应」
// 并中止 reader。闹钟是**每趟一个**（consume 里起、consume 退出时清），所以重试会把守卫
// 重新武装一遍，不会被上一趟用掉的时间误杀。停摆本身不触发重试：那条连接是被自己掐的。
//
// 这个文件**不 import react**：runner 是普通异步函数，界面那一半（setLive/setStreaming、
// 收尾重拉详情）留在 pages/Chat.tsx，靠 onBlocks/onNotice/stale 三个把手接上。

import { errText, readErrorEnvelope } from "./api.ts";
import { foldEvent } from "./blocks.ts";
import type { LiveBlock } from "./blocks.ts";
import { isTransientStatus, pump, resumeHeaders, toUiEvents } from "./sse.ts";
import type { UiEvent } from "./sse.ts";

/** 开流后多久没见到任何一帧就算停摆（S3-T3 给 T5 的 note：建议 45 秒）。 */
export const STALL_MS = 45_000;
/**
 * "没接上"的唯一一句人话。停摆、reader 报错重试耗尽、连接被静默关掉重试耗尽、
 * 开流 5xx 重试耗尽——四条路都说它（裁定 R-S3-12：不再按形状分叉文案）。
 * 后端 error 帧里那句 stream_timeout 是另一回事，那条由 sse.ts 原样透出。
 */
export const STALL_TEXT = "连接没响应";

/** 半截断掉的流重试几次（裁定 R-S3-10）：至多 2 次，加首趟一共 3 次连接。 */
export const STREAM_RETRIES = 2;
/** 重试前的退避。短就够：瞬断（切网络、代理重启）通常几百毫秒内过去，让人干等没意义。 */
export const RETRY_BACKOFF_MS = 600;

/**
 * 开流那一下。注入点：生产是 fetchOpen，测试交一个假的（返脚本化的 Response），
 * 于是"重试几次、带不带 Last-Event-ID、耗尽时说什么"能在没有浏览器也没有 jsdom 的地方跑。
 *
 * 头一律由 sse.ts 的 resumeHeaders 判（首趟不带 Last-Event-ID，续读才带），
 * 调用方别自己搓。
 */
export type Opener = (
  url: string,
  headers: Record<string, string>,
  signal: AbortSignal,
) => Promise<Response>;

/** 真的那一个：一条 fetch。断掉的 reader 不能复用，所以每趟重试都会再调它一次。 */
export const fetchOpen: Opener = (url, headers, signal) => fetch(url, { headers, signal });

/** consume 的五个把手：帧、终止性的超时帧与停摆都从这里报出去。 */
export interface Handlers {
  onEvent: (ev: UiEvent) => void;
  onDoneFrame: (messageId: number | null) => void;
  /** 每见到一帧报一次它的 id：续读的凭据（Last-Event-ID）就是这么攒出来的。 */
  onFrame: (id: number) => void;
  /**
   * 见到 error{code:stream_timeout} 那一帧，值是它的人话原文（终审 I2）。
   *
   * 它与 done 都让 consume 返 true，但对人是两件事：done = 这一轮写完了，stream_timeout =
   * 正文半截就断了。页面级横幅与「重发」把手只挂在后者上，所以得分开报。
   */
  onTimeout: (text: string) => void;
  onStall: () => void;
}

/** done 帧的 message_id：收尾对齐要拿它认"库里哪一条是刚说完的那一轮"。 */
function doneMessageId(data: unknown): number | null {
  if (typeof data !== "object" || data === null) return null;
  const v = (data as Record<string, unknown>).message_id;
  return typeof v === "number" && Number.isSafeInteger(v) ? v : null;
}

/**
 * 消费一串 chunk（真 reader 或演示帧），逐帧过 pump + toUiEvents。
 *
 * 返值是**这一轮有没有干净结束**：见到 end（done / stream_timeout）返 true；chunk 源走完
 * 还没见 end（后端关流、代理掐线）返 false，由调用方决定要不要带 Last-Event-ID 续读重试
 * （裁定 R-S3-10）。reader 报错就直接抛出去，同样交给调用方。end 是超时那一种时，
 * 返回之前先调一次 onTimeout（把那句人话交给页面级横幅）。
 *
 * sse.ts 文件头那两条口径在这儿落地：
 *   - 逐帧迭代 **toUiEvents**（复数）：stream_timeout 那一帧是「横幅 + end」两个事件，
 *     只取第一个的话这一轮会永远停在"正在写"；
 *   - 坏 payload 的帧不结束流，兜底就是这里返 false（chunk 源走完）。
 * 停摆守卫也在这儿：每见到一个 chunk 就把闹钟往后推，到点了 onStall（横幅 + 中止）；
 * 闹钟是每次调用一个，所以重试自然把守卫重置了一遍。
 * buf 不跨调用保留：断线时手里的半帧已经不可信，而续读会从 lastSeenId 之后重发，
 * 半个帧的字节一个都不会丢。
 */
export async function consume(
  chunks: AsyncIterable<string>,
  h: Handlers,
  stallMs: number = STALL_MS,
): Promise<boolean> {
  let buf = "";
  let timer = setTimeout(h.onStall, stallMs);
  const bump = () => {
    clearTimeout(timer);
    timer = setTimeout(h.onStall, stallMs);
  };
  try {
    for await (const chunk of chunks) {
      bump();
      const r = pump(buf, chunk);
      buf = r.rest;
      for (const f of r.frames) {
        h.onFrame(f.id);
        if (f.event === "done") h.onDoneFrame(doneMessageId(f.data));
        const evs = toUiEvents(f);
        for (const ev of evs) {
          if (ev.kind === "end") {
            // end 有两种来路：done 帧（这一轮写完了，evs 只有 end 一个事件）与
            // stream_timeout 帧（sse.ts 把它翻成「横幅 + end」，所以同一帧里还带着横幅）。
            // 判据就是这个"还带着横幅"，不再去读一遍 f.data 里的 code：哪种 code 终止流
            // 是 sse.ts 说了算的契约，在这儿抄第二份，哪天后端换个 code 只会改到一处。
            const banner = evs.find((b) => b.kind === "banner");
            if (banner !== undefined && banner.kind === "banner") h.onTimeout(banner.text);
            return true; // done / stream_timeout：这一轮到此为止
          }
          h.onEvent(ev);
        }
      }
    }
  } finally {
    clearTimeout(timer);
  }
  return false; // chunk 源走完还没见 end：半截断掉
}

/** 真流的 chunk 源：fetch 的 body reader + 带 stream:true 的解码器。 */
export async function* readerChunks(res: Response): AsyncGenerator<string> {
  const body = res.body;
  if (!body) return;
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      // { stream: true } 是承重的：一个汉字的 UTF-8 三字节可能被 read() 切成两块，
      // 不带它就在边界上解出 U+FFFD，正文里多个豆腐块（sse.ts 文件头那条坑）。
      yield decoder.decode(value, { stream: true });
    }
  } finally {
    reader.releaseLock();
  }
}

/** runTurnStream 的把手与旋钮。 */
export interface TurnOptions {
  /** 这一轮的流地址：`/api/chat/{sid}/stream?after={mid}`（mid = 刚 POST 出来那条人话的 id）。 */
  url: string;
  /**
   * 这一轮的中止把手。调用方（pages/Chat.tsx）建它、也负责在换会话/卸载时 abort；
   * runner 自己只在两处 abort：停摆守卫掐流，与收尾时"读到 end 就主动关流"由调用方做。
   */
  ctrl: AbortController;
  /**
   * 这一轮人类说的那句原文，**只**为「重发」带着（裁定 R-S3-13）：超时横幅要挂一个把手，
   * 点了把它放回输入框，而那时候详情里可能压根没有这一轮的行（doneId === null 那一种），
   * 没处去查。
   */
  humanText: string;
  /** 每折出一版新的块就报一次（调用方拿去 setLive）。stale 之后不再报。 */
  onBlocks: (blocks: LiveBlock[]) => void;
  /**
   * 页面级横幅。level 缺省是 err（这一路的 warn 都是流内横幅，走 foldEvent 那条路）；
   * resend 给了就在横幅里挂一个「重发」钮。
   */
  onNotice: (text: string, level?: "warn" | "err", resend?: string) => void;
  /** 换过会话 / 卸载了 / 又开了一轮：这一轮的结果一律不许再碰界面。 */
  stale: () => boolean;
  /** 开流就是 401（会话没了）：清身份、整页去登录由调用方做，runner 不碰路由。 */
  onUnauthorized?: () => void;
  /** 开流。默认 fetchOpen；测试注入假的。 */
  open?: Opener;
  /**
   * 演示帧（DEV）：给了就不开真流，也**不重试**——那是一段录好的剧本，断了重放只会让人
   * 看见同一轮说两遍。它仍然认中止信号（停摆守卫掐流时行为要与真 reader 一致）。
   * 注入而不是 import mockstream.ts：那个模块必须在生产包里被摇掉（裁定 R-S3-7）。
   */
  chunks?: (signal: AbortSignal) => AsyncIterable<string>;
  /** 重试预算（默认 STREAM_RETRIES）。测试里调小/调大用来数连接次数。 */
  retries?: number;
  /** 退避毫秒（默认 RETRY_BACKOFF_MS）。测试里调成 0，别让它白等。 */
  backoffMs?: number;
  /** 停摆阈值毫秒（默认 STALL_MS）。 */
  stallMs?: number;
}

/** 这一轮跑完的结果：调用方拿它去收尾对齐（重拉详情、存 extras、合成演示帧的行）。 */
export interface TurnOutcome {
  /** 这一轮折出来的块（正文/工具卡/流内横幅），顺序即到达顺序。 */
  blocks: LiveBlock[];
  /** done 帧里的 message_id；没见到 done 就是 null（超时、断流、开流被拒都算）。 */
  doneId: number | null;
  /** 开流拿到 401：调用方要清身份去登录，别再收尾。 */
  toLogin: boolean;
  /** 是不是干净结束的（done / stream_timeout）。false = 半截断掉或压根没接上。 */
  ended: boolean;
}

/**
 * 跑一轮的流：开流 → 逐帧折叠 → 没接上就带游标续读（至多 retries 次）→ 报结果。
 *
 * 它**不管界面状态**（「正在流」那个开关与 live 区都由调用方在自己的 finally 里收），也不管
 * 收尾对齐（重拉详情、把库里没有的块存进 extras）：那两件事都要 React 状态，留在这儿就把
 * 这一半又变得没法单测了。
 *
 * 抛不出去：所有错误都翻成 onNotice 的一句人话（ApiError 用后端原话，其余一律
 * 「连接没响应」），所以调用方不必再包一层 try。
 */
export async function runTurnStream(opts: TurnOptions): Promise<TurnOutcome> {
  const {
    url,
    ctrl,
    humanText,
    onBlocks,
    onNotice,
    stale,
    onUnauthorized,
    open = fetchOpen,
    chunks,
    retries = STREAM_RETRIES,
    backoffMs = RETRY_BACKOFF_MS,
    stallMs = STALL_MS,
  } = opts;

  let blocks: LiveBlock[] = [];
  // 折叠规则在 blocks.ts（与历史那条路同一份）：正文接尾、工具卡按 id 配对、横幅原地插。
  const applyEvent = (ev: UiEvent) => {
    if (stale()) return;
    blocks = foldEvent(blocks, ev);
    onBlocks(blocks);
  };

  let doneId: number | null = null;
  /** 见到过的最后一帧的 id：续读的凭据（Last-Event-ID），跨重试保留。 */
  let lastSeenId: number | null = null;
  let toLogin = false;
  let ended = false;

  const onStall = () => {
    // 停摆守卫：stallMs 一帧都没见着，说人话并把 reader 掐了
    if (!stale()) onNotice(STALL_TEXT);
    ctrl.abort();
  };
  /**
   * 终止性的那一帧（error{code:stream_timeout}）：正文半截，这一轮到此为止。
   *
   * 流里那条横幅之外**再升一条页面级横幅**（终审 I2），不是重复摆两遍：流里那条只活在
   * 流上（后端 _tail 有意不落库），收尾重拉详情之后 db-wins 会把它抹掉——人刚读完
   * "这条流等得太久了"，一眨眼那句话就没了。更坏的是"这一轮压根没起来"那一种：详情里
   * 没有比 mid 新的 agent 行，keepExtras 无处可挂，live 区被一把收走，屏幕上
   * 只剩自己刚说的那句话，一句解释都没有。页面级横幅两种都兜得住，而且带着「重发」的
   * 把手（裁定 R-S3-13）。
   */
  const onTimeout = (text: string) => {
    if (!stale()) onNotice(text, "err", humanText);
  };
  const handlers: Handlers = {
    onEvent: applyEvent,
    onDoneFrame: (messageId: number | null) => {
      doneId = messageId;
    },
    onFrame: (id: number) => {
      lastSeenId = id;
    },
    onTimeout,
    onStall,
  };
  /** 重试前等一小会儿。返 true = 期间被中止/换过会话，别再试了。 */
  const backoff = async (): Promise<boolean> => {
    await new Promise((resolve) => setTimeout(resolve, backoffMs));
    return ctrl.signal.aborted || stale();
  };

  try {
    if (chunks) {
      ended = await consume(chunks(ctrl.signal), handlers, stallMs);
    } else {
      for (let attempt = 0; ; attempt++) {
        let attemptEnded = false;
        try {
          // 每一趟都重开一次流：断掉的 reader 不能复用。头一律由 sse.ts 的
          // resumeHeaders 判，别在这儿手搓——首趟 lastSeenId 还是 null（新开一轮，
          // 没有断点要续，于是不发 Last-Event-ID），重试才带上它（同一轮内续读，
          // 这正是 R-S2-25 允许的那一种）。
          const res = await open(url, resumeHeaders(lastSeenId), ctrl.signal);
          if (res.status === 401) {
            // 会话没了：口径同 api.ts（清身份、整页去登录），不再往下拉详情
            toLogin = true;
            onUnauthorized?.();
            break;
          }
          if (!res.ok && !isTransientStatus(res.status)) {
            // 开流就被**明确拒**了（400 after 不合法 / 404 会话没了 / 403）：重试改不了
            // 这个答案，立刻把后端那句人话摆出来，一次都不试。
            // 信封的解法调 api.ts 的 readErrorEnvelope——全站唯一一处。这一条流不走
            // 那个客户端，原来在这儿抄了一份解析，两份迟早漂成"同一个错误两种说法"。
            const { message } = await readErrorEnvelope(res);
            if (!stale()) onNotice(message);
            break;
          }
          // 5xx 落到这里：不读它的响应体，attemptEnded 保持 false，直接消耗一次重试预算
          // （R-S3-12——服务器重启/网关抖动是瞬时的，而这一轮的正文还在服务端写着，
          // 带 Last-Event-ID 续读接得上）。
          // 不读也得**把它放掉**：body 一直没人读，这条连接就被浏览器挂在池子里等着
          // （HTTP/1.1 下不能复用，Chrome 对同一 origin 的并发连接还有上限），重试
          // 几趟就攒出几条占着不放的连接。cancel() 是"我不读了，你可以收了"。
          // 尾巴上那个 catch 不是装饰：流已经 errored 时 cancel() 按规范返回一个
          // **被拒**的 promise，不接住就是一条 unhandled rejection（5xx 的 body 恰恰
          // 最容易半截断掉）。401 那条分支不用管——它 break 出去，调用方收尾时的
          // ctrl.abort() 顺手就把 body 掐了；这里要重试，abort 得等到预算用完才来。
          if (!res.ok) void res.body?.cancel().catch(() => {});
          attemptEnded = res.ok ? await consume(readerChunks(res), handlers, stallMs) : false;
        } catch {
          // 被自己中止（停摆守卫 / 换会话 / 卸载）不算错：该出的横幅已经出过了
          if (ctrl.signal.aborted || stale()) break;
          if (attempt >= retries) {
            // reader 抛错且预算用完：与"连接被静默关掉"说同一句话（R-S3-12 撤掉了分叉）
            onNotice(STALL_TEXT);
            break;
          }
          if (await backoff()) break; // 半截断掉（reader 抛错）：退避一下再续读
          continue;
        }
        ended = attemptEnded;
        if (attemptEnded) break; // 干净的 end（done / stream_timeout）：绝不重连（R-S2-25）
        if (ctrl.signal.aborted || stale()) break;
        if (attempt >= retries) {
          // 没接上（连接被关了 / 开流 5xx）而 end 始终没来：预算用完，同一句话
          onNotice(STALL_TEXT);
          break;
        }
        if (await backoff()) break;
      }
    }
  } catch (e) {
    // 走到这儿的是循环外面抛的（演示帧那条路，或重试判定本身出了岔子）。
    // 是 ApiError 就原样说后端那句，否则也是「连接没响应」——全站只有这一句兜底。
    if (!ctrl.signal.aborted && !stale()) onNotice(errText(e, STALL_TEXT));
  }

  return { blocks, doneId, toLogin, ended };
}
