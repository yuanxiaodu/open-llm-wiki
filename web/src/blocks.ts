// 一个回合的"块"，与它的两条来源：流上的帧（live）与库里的事件行（历史）。
// 文件末尾还有一对**行**级的同源问题（库里的行 vs 乐观占位的行、半截那一轮的人类原文），
// 放在一起的理由与块一样：两边必须走同一份规则，而这份规则要能在没有 jsdom 的情况下单测。
//
// 为什么单独一个模块（计划 File Structure 里没列，落地时补的一层，理由同 types.ts）：
// 这两条来源必须走**同一套**折叠规则——工具卡片按 payload 的 id 配对、配不上就单独摆一张、
// system 是琥珀横幅、error 是赭红横幅。规则写在 Chat.tsx 里，历史那条路（MessageList 从
// message.events 渲染）就得再抄一份，两份迟早漂（漂移的样子是"刷新之后卡片配错了对/错"）。
// 所以折叠在这儿，Chat 与 MessageList 都调它。
//
// 这个文件运行时只 import sse.ts（一个不依赖任何库的纯函数模块）：ToolResult / MessageRow /
// AgentEvent 都是 `import type`，编译后一个字都不剩，所以 blocks.test.ts 能在纯 node 环境里
// 跑起来，不需要 jsdom、也不需要把 streamdown 拉进来。（ToolResult 原来长在
// components/ToolCard.tsx 里，那会让这个纯函数模块 import 一个组件、把 react 拽进测试，
// 所以它搬去了 types.ts。）
//
// 历史块的顺序（承重的近似）：库里只留了正文的终态（messages.content）与事件的 seq，
// **没有**"事件落库那一刻正文写到第几个字"，所以重放一整轮时无从还原交错顺序。这里的口径
// 与服务端尾读重放整轮时完全一致（app/routers/api/chat.py 的 _tail：正文合成一帧排在事件
// 之前）——两边同一个近似，刷新前后看到的顺序才不会自己变。

import { toUiEvents } from "./sse.ts";
import type { UiEvent } from "./sse.ts";
import type { MessageRow, Outcome, ToolResult } from "./types.ts";

/** 一个回合里按到达顺序排布的块。 */
export type LiveBlock =
  | { kind: "text"; text: string }
  | { kind: "tool"; id: string; name: string; args: string; result?: ToolResult }
  | { kind: "banner"; text: string; level: "warn" | "err" }
  | { kind: "outcome"; outcome: Outcome };

/**
 * 这次结果该配哪张卡：从后往前找第一张还没结果的 tool。
 *
 * 配对的凭据是 payload 的 id（SDK 的 tool_call_id）。两边都没有 id 的兜底形状
 * （sse.ts 的 optStr：缺键 → 空串）退回按工具名配——宁可配错一张同名的，也不把
 * 一次失败悄悄扔掉（失败的 summary 是人要看的）。id 明确时对不上就继续往前找，
 * 不退回按名字猜。
 */
function findCall(blocks: LiveBlock[], id: string, name: string): number {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i];
    if (b === undefined || b.kind !== "tool" || b.result) continue;
    if (id !== "") {
      if (b.id === id) return i;
      continue;
    }
    if (b.id === "" && b.name === name) return i;
  }
  return -1;
}

/**
 * 把一个 UI 事件折进块列表，返回**新**数组（纯函数：调用方拿着旧数组也不会被改）。
 *
 * 流式与历史共用这一份，所以四条规则只写一遍：
 *   - text 接在最后一个 text 块尾巴上（没有就新开一块）；
 *   - tool-call 开一张卡；
 *   - tool-result 按 id 配回那张卡（配不上就单独摆一张带结果的，不悄悄扔掉）；
 *   - banner 原地插一块（system 琥珀 / error 赭红，level 由 sse.ts 判）。
 * `end` 不产生块：它是"这一轮到此为止"的记号，由 consume 拿去当返回用。
 */
export function foldEvent(blocks: LiveBlock[], ev: UiEvent): LiveBlock[] {
  switch (ev.kind) {
    case "text": {
      const last = blocks[blocks.length - 1];
      if (last !== undefined && last.kind === "text") {
        return [...blocks.slice(0, -1), { ...last, text: last.text + ev.text }];
      }
      return [...blocks, { kind: "text", text: ev.text }];
    }
    case "tool":
      return [...blocks, { kind: "tool", id: ev.id, name: ev.name, args: ev.args }];
    case "toolresult": {
      const result: ToolResult = { ok: ev.ok, summary: ev.summary };
      const at = findCall(blocks, ev.id, ev.name);
      const call = at >= 0 ? blocks[at] : undefined;
      if (call === undefined || call.kind !== "tool") {
        // 配不上（call 行没落库、或 id 对不上任何在等的调用）：单独摆一张
        return [...blocks, { kind: "tool", id: ev.id, name: ev.name, args: "", result }];
      }
      return [...blocks.slice(0, at), { ...call, result }, ...blocks.slice(at + 1)];
    }
    case "banner":
      return [...blocks, { kind: "banner", text: ev.text, level: ev.level }];
    case "outcome":
      return [...blocks, { kind: "outcome", outcome: ev.outcome }];
    case "end":
      return blocks;
  }
}

/**
 * 库里那一轮的块（裁定 R-S3-8）：正文合成一块排最前，后面按 seq 折 events。
 *
 * events 为空就返 **null**（不是空数组）——那不是"这一轮什么也没发生"，而是"库里没有
 * 可渲染的事件"，此时该走 live/extras 那条路（human 行、纯正文的一轮、DEV 演示帧合成的
 * 行都是这个形状）。
 *
 * 每条事件都过一遍 sse.ts 的 toUiEvents：解析与形状校验因此与流上**同一份**——payload
 * 缺键、类型不对、后端交上来一个 null（详情接口对坏 JSON 的兜底），一律翻成
 * 「服务端帧坏了」的 err 横幅，与当时流上看到的那句一模一样。构造 Frame 时 id 给 0：
 * id 只用于续读游标，历史渲染用不上它，而 toUiEvents 压根不读这个字段。
 */
export function blocksFromDb(msg: MessageRow): LiveBlock[] | null {
  const events = msg.events ?? [];
  if (events.length === 0) return null;
  let blocks: LiveBlock[] = [];
  if (msg.content !== "") blocks.push({ kind: "text", text: msg.content });
  for (const e of events) {
    for (const ev of toUiEvents({ id: 0, event: e.type, data: e.payload })) {
      blocks = foldEvent(blocks, ev);
    }
  }
  return blocks;
}

/**
 * 一条已落库的 agent 消息该渲染哪些块。db-wins（裁定 R-S3-8）：
 * 详情接口交了 events 就**以库为准**，Chat 那边流式期间攒下的 extras 一概不看。
 *
 * 为什么让库赢，而不是"流式看到的交错顺序更真"：extras 是客户端自己攒的一份副本，
 * 而库里的 content 与 agent_events 是这一轮唯一的事实（截断封口、失败封口都只写在库里）。
 * 两份并存就得挑，挑错的样子是"刷新前后同一轮长得不一样"——那比丢掉一点交错顺序更骗人。
 * 代价说清楚：一轮收尾重拉详情时，工具卡片会从正文中间跳到正文之后（与刷新之后一致）；
 * 只活在流上、不落库的 stream_timeout 横幅会随之消失（后端 _tail 有意不落它）。
 *
 * events 为空时才走 extras：
 *   - 正文串一致（正常情况）就原样用块列表——工具卡片与正文的交错顺序是流式期间看到的
 *     那个顺序，重排会骗人（DEV 演示帧合成的行只有这条路可走，它压根不落库）；
 *   - 不一致（断线、重放、后端截断）时以库里为准：把第一个正文块换成库里的整段、
 *     其余正文块丢掉，工具卡与横幅原地保留。
 */
export function blocksForMessage(msg: MessageRow, live?: LiveBlock[]): LiveBlock[] {
  const fromDb = blocksFromDb(msg);
  if (fromDb !== null) return fromDb;
  const text: LiveBlock = { kind: "text", text: msg.content };
  if (!live || live.length === 0) return [text];
  const streamed = live
    .filter((b): b is Extract<LiveBlock, { kind: "text" }> => b.kind === "text")
    .map((b) => b.text)
    .join("");
  if (streamed === msg.content) return live;
  const at = live.findIndex((b) => b.kind === "text");
  if (at < 0) return [...live, text];
  return [...live.slice(0, at), text, ...live.slice(at + 1).filter((b) => b.kind !== "text")];
}

/**
 * 渲染用的消息行 = 库里的行 + 还没进库的乐观行，按 **id 集合的差**去重（裁定 R-S3-18）。
 *
 * 库里已经有的 id 一律听库的，乐观行里剩下的才拼在后面。
 *
 * 为什么在渲染处判、而不是靠"重拉详情之后把乐观行清空"：那是两次 setState，分别落在两个
 * await 之后（setDetail 在 loadDetail 里、setOptimistic([]) 在它的调用方），React 并不保证
 * 把它们并进同一次渲染——中间那一帧就是"库里那条 + 乐观那条"并排摆着，人看见自己刚说的
 * 一句话出现了两遍。按 id 求差之后，两次更新谁先谁后都不影响画面：库里的行一到，同 id 的
 * 乐观行当场就不算了（清空那一下仍然留着，是为了不让 optimistic 无限长）。
 *
 * "同 id"就是"同一条消息"，不存在猜：乐观行的 id 是 POST 回来的真 id（DEV 演示帧那一路
 * 用负数假 id，压根不会撞上库里的行，见 pages/Chat.tsx 的 humanRow/agentRow）。
 */
export function mergeRows(fromDb: MessageRow[], optimistic: MessageRow[]): MessageRow[] {
  const inDb = new Set(fromDb.map((m) => m.id));
  return [...fromDb, ...optimistic.filter((m) => !inDb.has(m.id))];
}

/**
 * 哪些回合该挂「重发」，以及点了要往输入框里放什么（裁定 R-S3-13）。
 *
 * 键 = 半截收尾的 agent 行 id（库里 `truncated = 1`：max_turns 超限、上游断流、失败封口
 * 都写在它身上），值 = 那一轮**人类说的那句原文**。找原文的办法是从前往后记住最近一条
 * human 行：后端只在 POST 消息时起一轮（上传只落一条播报消息、不起轮，chat.py 的
 * post_upload），所以紧挨在 agent 行前面的那条 human 行就是这一轮的提问，一趟扫完就够。
 *
 * 记不住原文就不给键（第一条就是 agent 行，例如启动时封口的那些孤儿行）；原文是空白也不给
 * ——没有话可预填，摆一个点了什么都不发生的按钮比不摆更坏。
 *
 * 只"预填"、不自动发送：那一轮为什么断的（额度、断流、超轮）界面并不知道，替人决定
 * "再发一次"是越权。spec §8 的"一点重发"就是这么个一点：把话放回框里，人自己按发送。
 */
export function resendTexts(messages: MessageRow[]): Map<number, string> {
  const out = new Map<number, string>();
  let human = "";
  for (const m of messages) {
    if (m.role === "human") {
      human = m.content;
      continue;
    }
    if (m.truncated === 1 && human.trim() !== "") out.set(m.id, human);
  }
  return out;
}


/**
 * 这一轮的"依据页"：从读过的页与搜索命中里抽，给回答底部的 chips。
 *
 * 只认两种工具：`read` 的 args.path（页面文件，跳过 raw/ 与特殊文件）与 `search` 的
 * 结果摘要（`【页面 <path>】`）。写入/删除不算"依据"——那是变更，对话页不再展示
 * （2026-10-30 口径：这些后台数字只在管理员日志页出现）。
 * 最多 6 个，顺序按出现先后，去重。
 */
export function sourcesOf(blocks: LiveBlock[]): string[] {
  const out: string[] = [];
  const push = (raw: string): void => {
    const path = raw.trim();
    if (!path.endsWith(".md") || path.startsWith("raw/")) return;
    if (["index.md", "SCHEMA.md", "log.md"].includes(path)) return;
    if (!out.includes(path)) out.push(path);
  };
  for (const block of blocks) {
    if (block.kind !== "tool") continue;
    if (block.name === "read") {
      try {
        const args = JSON.parse(block.args) as { path?: unknown };
        if (typeof args.path === "string") push(args.path);
      } catch {
        // args 不是 JSON 就不猜
      }
    } else if (block.name === "search" && block.result) {
      for (const hit of block.result.summary.matchAll(/【页面 ([^】]+)】/g)) push(hit[1] ?? "");
    }
  }
  return out.slice(0, 6);
}

/** 正在流的那一轮现在在干什么（给"正在写"那个 pill 换个动词）。 */
export function phaseOf(blocks: LiveBlock[]): string | null {
  const last = [...blocks].reverse().find((b) => b.kind === "tool");
  if (last === undefined || last.kind !== "tool") return null;
  switch (last.name) {
    case "ingest_text":
    case "ingest_document":
    case "ingest_url":
    case "read":
      return "读材料";
    case "search":
    case "list_wiki":
    case "web_search":
      return "找资料";
    case "write_page":
    case "edit_page":
    case "delete_page":
      return "整理成页";
    case "lint":
      return "体检";
    default:
      return null;
  }
}
