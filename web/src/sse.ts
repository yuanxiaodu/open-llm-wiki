// SSE 适配层：把后端那条流的字节，翻成界面能直接渲染的事件。
//
// 线上格式来自 app/routers/api/chat.py 的 _frame（S2 交付，已核实）：
//
//     id: <整数>\nevent: <类型>\ndata: <JSON>\n\n
//
// 六种 event：text-delta{text}、tool-call{id,name,args?}、tool-result{id,name,ok,summary}、
// system{text}、error{code,text}、done{message_id,usage}。id 是 _frame_id 铸的
// `seq * 1_000_000 + 正文偏移`，一个整数里压着续读要恢复的两件状态，所以断线重连
// 只能靠它（Last-Event-ID）。
//
// 三条契约（S2 裁定，别在这儿重新发明）：
//   1. done 结束流；error 里 code == "stream_timeout" **也**结束流（先出横幅再 end），
//      其余 code（upstream/internal/max_turns/behavior/cancelled）只出横幅、流还开着。
//   2. system → 琥珀色 warn 横幅。额度用尽就是走这条（runner._fail 刻意丢掉 code，
//      payload 只有 {"text": …}），所以**没有 code 可 switch**，认 type 就够。
//   3. tool-call 可能整个不带 args 键（无参工具，hooks 那边"没参数就不记这个键"）；
//      tool-result 与 tool-call 按 payload 的 id（SDK 的 tool_call_id）配对。
//
// 这个文件**不 import 任何东西**（尤其不 import react）：纯函数才好单测，S4 的vault 面
// 与 Task 5 的对话页共用同一份解析。上层（Task 5 的 Chat）该这样用：
//
//     const decoder = new TextDecoder("utf-8");           // ← stream:true 是承重的
//     let buf = "", lastId: number | null = null;
//     for (;;) {
//       const { value, done } = await reader.read();
//       if (done) break;                                  // 连接关掉也算流结束（兜底）
//       const r = pump(buf, decoder.decode(value, { stream: true }));
//       buf = r.rest;
//       for (const f of r.frames) {
//         lastId = f.id;
//         for (const ev of toUiEvents(f)) { …渲染…; if (ev.kind === "end") 主动 close() }
//       }
//     }
//
// 两个坑写在这儿免得踩：
//   - `decode(value, { stream: true })` 必须带：一个汉字的 UTF-8 三字节可能被 read()
//     切成两块，不带 stream 会在边界上解出 U+FFFD，正文里就多了个豆腐块。pump 是串级的，
//     只管帧边界，管不了字节边界。
//   - 逐帧迭代 **toUiEvents**（复数）：一帧可能出两个事件，stream_timeout 那一帧就是
//     「横幅 + end」——只取第一个的话，超时那一轮会永远停在"还在写"。这里**只有这一个
//     映射入口**：原来还有个单值的 toUiEvent(f)（返第一个事件或 null），它是给"一帧一个
//     事件"的想象形状准备的陷阱面，S3 收尾时删掉了（T3 评审的 M8）。要"第一个事件"就自己
//     写 toUiEvents(f)[0]，那样至少看得见数组里还有别的。
//
// 关于 rest：pump 从不丢字节——`buf + chunk` 要么进了某一帧，要么原样留在 rest 里等下一趟。
// 反过来说，一条永远不发 LF 空行的流会让 rest 一直长；上限由服务端那条流的墙钟
// （STREAM_TIMEOUT_SECONDS → stream_timeout 帧）兜着，这里不做截断（截断就是丢字节）。

import type { Outcome } from "./types.ts";

/** 一帧 SSE。id 恒为安全非负整数（解不出的块不发帧，见 parseBlock）。 */
export interface Frame {
  id: number;
  event: string;
  data: unknown;
}

/** 界面事件。banner 的 level：warn=琥珀（system）、err=赭红（error/坏帧）。 */
export type UiEvent =
  | { kind: "text"; text: string }
  | { kind: "tool"; id: string; name: string; args: string }
  | { kind: "toolresult"; id: string; name: string; ok: boolean; summary: string }
  | { kind: "banner"; text: string; level: "warn" | "err" }
  | { kind: "outcome"; outcome: Outcome }
  | { kind: "end" };

/** 坏帧的横幅文案。界面上不暴露后台原理，就说"帧坏了"，不贴原始 JSON。 */
export const BROKEN_FRAME_TEXT = "服务端帧坏了";

/** id 的判据：十进制 ASCII 数字串（见 parseId 里为什么不用 trim / 不认全角）。 */
const INTEGER_ID = /^\d+$/;

/**
 * 把新到的 chunk 接到 buf 上，切出所有**完整**帧，剩下半截原样还给调用方。
 *
 * 帧界只认 `\n\n`（两个 LF）：后端 _frame 写死的就是它。CRLF 不归一——真收到
 * `\r\n\r\n` 说明中间有东西改写过流，那时候猜它想发什么比不猜更危险（放宽分隔符
 * 会让截断在 \r 上的半截 data 也被当成整帧解出来，人会看见半句正文而不是"没收到"）。
 * 不归一的结果是它整串留在 rest 里，一帧都不出，上层看得见"卡住了"。
 */
export function pump(buf: string, chunk: string): { frames: Frame[]; rest: string } {
  const acc = buf + chunk;
  const blocks = acc.split("\n\n");
  // 最后一段没有终止空行：可能是半帧，也可能就是空串（acc 正好以 \n\n 结尾）。
  // 用 pop 而不是 slice(0, -1) 是为了让"消费掉的 + rest === acc"这条恒等式显然成立。
  const rest = blocks.pop() ?? "";
  const frames: Frame[] = [];
  for (const block of blocks) {
    const frame = parseBlock(block);
    if (frame !== null) frames.push(frame);
  }
  return { frames, rest };
}

/**
 * 全量映射：一帧 → 0..2 个 UI 事件（按到达顺序）。
 *
 * 对每种 event 都是**全函数**：payload 缺键、类型不对、JSON 压根解不动，一律返回
 * 「服务端帧坏了」的 err 横幅，绝不抛。唯一会返两个事件的是 error{code:stream_timeout}
 * （横幅 + end）。
 *
 * 读不出 payload 时**不结束流**（done 与 stream_timeout 这两种终止帧也一样）：返 end 等于
 * 替前端宣布"这一轮干净地写完了"，而干净的 end 之后是不许带 Last-Event-ID 重连的
 * （R-S2-25——重连拿到的只是整轮重放或又一个 done，纯空转；**同一轮内**的续读是另一回事，
 * 裁定 R-S3-10 允许，见 pages/Chat.tsx 的重试环），于是这一轮剩下的正文全丢。挂着的代价
 * 小得多：连接真关掉时 reader 的 done 会兜底（见文件头那段用法），前端按"没接上"带游标
 * 续读，把剩下的正文接回来。最坏的一种是"坏掉的那一帧恰好就是最后一帧"——白试两次续读
 * 再出一句「连接没响应」，仍然比悄悄少半截正文好。
 */
/** system 事件的 outcome payload → Outcome；形状不对返 null（调用方翻成坏帧横幅）。 */
function outcomeOf(d: Record<string, unknown>): Outcome | null {
  const rawPages = d.pages;
  if (!Array.isArray(rawPages)) return null;
  const pages = rawPages
    .filter((item): item is Record<string, unknown> => isRecord(item))
    .map((item) => ({
      path: typeof item.path === "string" ? item.path : "",
      action: typeof item.action === "string" ? item.action : "edit",
    }))
    .filter((item) => item.path !== "");
  const citations = typeof d.citations === "number" && Number.isFinite(d.citations) ? d.citations : null;
  if (citations === null) return null;
  return {
    pages,
    citations,
    lint_errors: typeof d.lint_errors === "number" ? d.lint_errors : null,
  };
}

export function toUiEvents(f: Frame): UiEvent[] {
  const d = f.data;
  switch (f.event) {
    case "text-delta": {
      const text = str(d, "text");
      return text === null ? [broken()] : [{ kind: "text", text }];
    }
    case "tool-call": {
      const id = optStr(d, "id");
      const name = str(d, "name");
      const args = optStr(d, "args");
      if (id === null || name === null || args === null) return [broken()];
      return [{ kind: "tool", id, name, args }];
    }
    case "tool-result": {
      const id = optStr(d, "id");
      const name = str(d, "name");
      const summary = str(d, "summary");
      const ok = isRecord(d) ? d.ok : null;
      if (id === null || name === null || summary === null || typeof ok !== "boolean") {
        return [broken()];
      }
      return [{ kind: "toolresult", id, name, ok, summary }];
    }
    case "system": {
      // system 有两种 payload：额度/兜底那句 {text}（琥珀 warn 横幅）与收尾成果
      // {kind:"outcome", pages, citations, lint_errors}。成果帧照旧解析成 outcome 块——
      // 对话页自 2026-10-30 起不再画它（不再暴露后台改了几页），但旧后端/历史帧不能
      // 因此变成坏帧；日志页那套数据走后端自己的接口。
      if (isRecord(d) && d.kind === "outcome") {
        const outcome = outcomeOf(d);
        return outcome === null ? [broken()] : [{ kind: "outcome", outcome }];
      }
      // 额度那条走这儿：没有 code，type 就是判据；warn（琥珀）而不是 err。
      const text = str(d, "text");
      return text === null ? [broken()] : [{ kind: "banner", text, level: "warn" }];
    }
    case "error": {
      const text = str(d, "text");
      if (text === null) return [broken()];
      const banner: UiEvent = { kind: "banner", text, level: "err" };
      // 只有 stream_timeout 是终止性的（尾读发完就 return）；其余 code 之后流还开着。
      return str(d, "code") === "stream_timeout" ? [banner, { kind: "end" }] : [banner];
    }
    case "done":
      // done 的 payload（message_id/usage）界面上一个字都不用：usage 恒为 null，
      // message_id 拿来做的是"重拉详情"，而那是 Task 5 在 end 之后自己做的事。
      // 这里只要求 payload 是个对象——读不出来就按上面那条口径不结束流。
      return isRecord(d) ? [{ kind: "end" }] : [broken()];
    default:
      // 不认得的 event（后端故意不发的 retry、心跳注释块、哪天新加的类型）：
      // 没有 UI 事件，也不结束流。
      return [];
  }
}

/**
 * 续读头。lastId 为 null（第一次连流）或**不是**一个能安全表示的非负整数时返回空头：
 * 后端 _parse_last_event_id 解不动的值当 0（整轮重放），而解得动却越界的值是 400
 * 「续读位置不合法」，所以宁可不发也不发一个怪的。跨轮重连本来就该不带这个头
 * （R-S2-28：id 空间里没有 message_id，中段游标交给下一轮只会错乱）。
 */
export function resumeHeaders(lastId: number | null): Record<string, string> {
  if (typeof lastId !== "number" || !Number.isSafeInteger(lastId) || lastId < 0) return {};
  return { "Last-Event-ID": String(lastId) };
}

/**
 * 开流被拒时，这个状态码值不值得再试一次（裁定 R-S3-12）。
 *
 * **只有 5xx 算瞬时**：服务器正在重启、网关短暂 502/504、后端进程被 OOM 掐了又拉起来
 * ——这几种在几百毫秒到几秒之后就过去了，而这一轮的正文还在服务端往流里写，续读接得上。
 *
 * 4xx 一律**不**试：400（after 不合法）/401（会话没了）/404（会话没了）是后端想清楚了
 * 给的答案，再问三次还是同一个答案，只是把人晾着。那几种要立刻把后端那句人话摆出来
 * （401 还要清身份去登录），走不到重试环里。
 *
 * 5xx 与"连接被静默关掉"共用同一个重试预算（STREAM_RETRIES）：两者对人来说是同一件事
 * ——"没接上"，而预算要防的正是"一直重试一直失败"这种空转。
 */
export function isTransientStatus(status: number): boolean {
  return status >= 500 && status <= 599;
}

// —— 内部 ————————————————————————————————————————————————————————

/**
 * 一个块（两帧界之间的字节）→ Frame，或 null（这块不是帧）。
 *
 * 字段按 SSE 规范的口径处理：冒号后**一个**空格不算值的一部分；同名字段后写的赢
 * （data 例外，是多行累加）；行首冒号是注释（心跳），没有冒号的行忽略；不认得的
 * 字段名（retry 等）忽略。
 *
 * 没有可用整数 id 的块**不发帧**：id 是续读的全部凭据，Frame.id 恒为安全非负整数
 * 这条不变式值一帧正文——真收到这种块，说明流已经被改写坏了，那帧正文也不可信。
 */
function parseBlock(block: string): Frame | null {
  let id: number | null = null;
  let event = "";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    const field = line.slice(0, colon);
    let value = line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") {
      id = parseId(value);
    } else if (field === "event") {
      event = value;
    } else if (field === "data") {
      dataLines.push(value);
    }
  }
  if (id === null) return null;
  return { id, event, data: dataLines.length > 0 ? parseData(dataLines.join("\n")) : null };
}

/**
 * `id:` 的值 → 整数，或 null。
 *
 * 只认十进制 ASCII 数字串：负号、小数点、指数、尾随空白、全角数字一律不算。
 * /^\d+$/ 是 ASCII 口径，正好避开后端 R-S2-23 踩过的那个坑（Python 的
 * `"２".isdigit()` 为真而 `int("２")` 抛；JS 的 \d 压根不认全角）。
 * 再过一道 isSafeInteger：超过 2^53-1 的数在 JS 里已经表示不准，把它交给
 * Last-Event-ID 就是送后端一个 400。
 */
function parseId(value: string): number | null {
  if (!INTEGER_ID.test(value)) return null;
  const n = Number(value);
  return Number.isSafeInteger(n) ? n : null;
}

/**
 * data 的 JSON → 值；解不动就**原样把串留在 data 上**。
 *
 * 不在这儿报错也不在这儿丢帧：串没有 .text/.name 这些键，toUiEvents 的形状校验自然会
 * 判成坏帧，出「服务端帧坏了」。这样"帧切分"与"payload 语义"两件事各管一半，
 * 单测也各钉各的。
 */
function parseData(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 必填的串字段：不在或不是串 → null（= 坏帧）。 */
function str(d: unknown, key: string): string | null {
  if (!isRecord(d)) return null;
  const v = d[key];
  return typeof v === "string" ? v : null;
}

/**
 * 可缺的串字段：键整个不在 → ""（空参的 tool-call 就是这形状），在了但不是串 → null。
 * 注意 null 不算"缺"：后端缺键时是**不写这个键**，写 null 是另一种形状，当坏帧处理。
 */
function optStr(d: unknown, key: string): string | null {
  if (!isRecord(d)) return null;
  const v = d[key];
  if (v === undefined) return "";
  return typeof v === "string" ? v : null;
}

/** 每次返新对象：同一个横幅对象被复用会让上层按引用去重/当 key 时出错。 */
function broken(): UiEvent {
  return { kind: "banner", text: BROKEN_FRAME_TEXT, level: "err" };
}
