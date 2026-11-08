import { ApiError } from "./api.ts";
import { ARCHIVED_TEXT } from "./copy.ts";

// DEV-only 演示帧（裁定 R-S3-7）。
//
// 存在的唯一理由：四种横幅（额度 warn、超时 err、409 err、归档 400）要能截图、能手工验收，
// 而真跑一轮要花模型额度、还要等几十秒。这个模块把**录制的帧串**喂给页面，页面走的仍然是
// 生产那条路（pump → toUiEvents → 渲染），所以被演示的是真适配层，不是另写的一套假界面。
//
// 只在 `import.meta.env.DEV` 且 URL 带 `?mockstream=<kind>` 时生效；调用点一律写成
// `import.meta.env.DEV && …`，vite 构建时把 DEV 替成 false、整段死代码删掉，这个文件
// 随之被摇掉——**生产包里不该出现这里的任何一个字**。核对办法（Task 5 报告里贴了输出）：
//
//     corepack pnpm build && grep -rl "openllmwiki-mockstream-fixtures" dist/   # 应当一行都不出
//
// 帧串是手抄后端 app/routers/api/chat.py 的 _frame 与 app/agent/runner.py 的文案，
// id 也按 _frame_id 的算法铸（seq * 1_000_000 + 正文偏移），所以这些字节与线上同形：
// 换个人把真帧录进来，页面代码一个字都不用改。

/** 生产包里不该出现的标记串：构建后 grep 它，找得到就说明摇树没生效。 */
export const MOCK_MARKER = "openllmwiki-mockstream-fixtures";

export type MockKind = "full" | "quota" | "timeout" | "conflict409" | "archived";

const KINDS: readonly MockKind[] = ["full", "quota", "timeout", "conflict409", "archived"];

/** `?mockstream=<kind>` → kind；没带、或带了个不认得的值，一律 null（走真后端）。 */
export function readMockKind(search: string): MockKind | null {
  const raw = new URLSearchParams(search).get("mockstream");
  return raw !== null && (KINDS as readonly string[]).includes(raw) ? (raw as MockKind) : null;
}

// —— 录制的帧串 ————————————————————————————————————————————————

// 后端原文（照抄 app/agent/runner.py，别在这儿改字）
const QUOTA_TEXT =
  "额度暂时用完，你说的和传的都已保存。这一条回复没写出来，等额度回来重发一次。";
const STREAM_TIMEOUT_TEXT =
  "这条流等得太久了，先断开。刷新一下看最新内容，或把刚才那句重发一次。";

// full：一整轮跑完（正文 + 两次工具往返，其中一次 ok=false + done）。
// 正文刻意带 markdown（####、无序列表），streamdown 的渲染要看得见。
const FULL: string[] = [
  "id: 6\nevent: text-delta\ndata: {\"text\":\"先查了全站，\"}\n\n",
  "id: 12\nevent: text-delta\ndata: {\"text\":\"再动笔。\\n\\n\"}\n\n",
  "id: 1000012\nevent: tool-call\ndata: {\"id\":\"call_9a1\",\"name\":\"search\",\"args\":\"{\\\"q\\\":\\\"试听 预约 小程序 漏单\\\",\\\"scope\\\":\\\"all\\\"}\"}\n\n",
  "id: 2000012\nevent: tool-result\ndata: {\"id\":\"call_9a1\",\"name\":\"search\",\"ok\":true,\"summary\":\"命中 4 条：2 条来自你的会话，2 条来自上传件《竞品拆解-实习周记.pptx》\"}\n\n",
  "id: 2000034\nevent: text-delta\ndata: {\"text\":\"#### 已经并进《报名流程线上化》v3\\n\\n\"}\n\n",
  // 无参形状：hooks「没有参数串就整个键不记」，args 键压根不在
  "id: 3000034\nevent: tool-call\ndata: {\"id\":\"call_9a2\",\"name\":\"edit_page\"}\n\n",
  "id: 4000034\nevent: tool-result\ndata: {\"id\":\"call_9a2\",\"name\":\"edit_page\",\"ok\":false,\"summary\":\"改动没落地：这一页正被别人改着，稍后再试\"}\n\n",
  "id: 4000052\nevent: text-delta\ndata: {\"text\":\"- 漏单从每周 6 起降到 1 起\\n\"}\n\n",
  "id: 4000070\nevent: text-delta\ndata: {\"text\":\"- 客服投诉口径统一进小程序工单\\n\\n\"}\n\n",
  "id: 4000100\nevent: text-delta\ndata: {\"text\":\"引注取自你上传的《竞品拆解-实习周记.pptx》第 4 页。\"}\n\n",
  "id: 4000113\nevent: text-delta\ndata: {\"text\":\"正文里没写的部分我没有补。\"}\n\n",
  "id: 4000114\nevent: done\ndata: {\"message_id\":88,\"usage\":null}\n\n",
];

// quota：额度用尽。system 帧（payload 只有 text，runner._fail 刻意丢掉 code，所以
// 前端没有 code 可 switch，认 event 类型就够）→ 琥珀 warn 横幅；随后 truncated=1 封口，
// 尾读照样发得出 done。
const QUOTA: string[] = [
  "id: 13\nevent: text-delta\ndata: {\"text\":\"我先把这段经历读一遍。\\n\\n\"}\n\n",
  `id: 1000013\nevent: system\ndata: {"text":"${QUOTA_TEXT}"}\n\n`,
  "id: 1000014\nevent: done\ndata: {\"message_id\":88,\"usage\":null}\n\n",
];

// timeout：尾读墙钟。error{code:stream_timeout} 是终止性的（横幅 + 收流），
// 那个 tool-call 因此永远等不到配对的 tool-result——这正是真实形状。
const TIMEOUT: string[] = [
  "id: 7\nevent: text-delta\ndata: {\"text\":\"正在检索全站…\"}\n\n",
  "id: 1000007\nevent: tool-call\ndata: {\"id\":\"call_9a3\",\"name\":\"search\",\"args\":\"{\\\"q\\\":\\\"报名流程\\\"}\"}\n\n",
  `id: 1000008\nevent: error\ndata: {"code":"stream_timeout","text":"${STREAM_TIMEOUT_TEXT}"}\n\n`,
];

const FRAMES: Partial<Record<MockKind, string[]>> = {
  full: FULL,
  quota: QUOTA,
  timeout: TIMEOUT,
};

// conflict409 与 archived 不发帧：它们要演示的是 POST 那一步就被后端挡下来。
const CONFLICT_TEXT = "智能体还在写，等它说完";
// ARCHIVED_TEXT 在 copy.ts：这一档顶替的就是后端那句 400，而对话页输入区的提示位用的是
// 同一句——两处各写一份，改了后端却只改一处，演示帧就会演示一句线上不存在的话。

/** 假 mid：只在页面里当 React key 与乐观消息的 id 用，永不落库、永不上 URL。 */
const FAKE_MID = 900000001;

/**
 * 顶替 POST /api/chat/{id}/messages。
 *
 * 这一条**必须**被顶替，不只是"顺手"：真 POST 会让后端 _start_turn 起一轮真跑
 * （花额度、还往共享的演示库里写行）。所以 full/quota/timeout 也走这里，返一个假 id，
 * 一个字节都不发给后端。
 */
export function mockPostMessage(kind: MockKind, content: string): Promise<{ id: number }> {
  if (kind === "conflict409") {
    return Promise.reject(new ApiError(CONFLICT_TEXT, 409));
  }
  if (kind === "archived") {
    return Promise.reject(new ApiError(ARCHIVED_TEXT, 400));
  }
  void content;
  return Promise.resolve({ id: FAKE_MID });
}

/** archived 这一档还要把会话本身显示成已归档：调用点写在 Chat 里（`mock === "archived"`），
 * 那个判断决定输入区整块禁用与提示位那句 400 人话。 */

const TICK_MS = 45; // 一拍 45ms：看得见"在流"，又不用等
const CHUNK = 23; // 故意不与帧界对齐，好让 pump 的半帧缓冲真被走到

/**
 * 带中止的睡：真流被 abort 时 reader.read() 会**抛**，演示帧也得认这个信号，
 * 否则停摆守卫掐了流、这边的睡还挂着五十秒，收尾状态就对不上了。
 */
function sleepOrAbort(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      resolve();
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** 录制的帧 → 一串碎 chunk（形状与 reader.read() 的交付一致）；被中止就提前收。 */
export async function* mockChunks(
  kind: MockKind,
  signal?: AbortSignal,
): AsyncGenerator<string> {
  if (import.meta.env.DEV) {
    // 用掉标记串：这个文件只要被摇剩下来，标记就跟着在（构建后 grep 它验摇树）
    console.debug(`[mockstream ${MOCK_MARKER}] kind=${kind}`);
  }
  const whole = (FRAMES[kind] ?? []).join("");
  for (let i = 0; i < whole.length; i += CHUNK) {
    await sleepOrAbort(TICK_MS, signal);
    if (signal?.aborted) return;
    yield whole.slice(i, i + CHUNK);
  }
}
