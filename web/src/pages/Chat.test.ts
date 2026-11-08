// 对话页这一轮的源码钉子（S3 终审 I1/I2/I5 + 裁定 R-S3-18/R-S3-19/R-S4-1 + 收尾清理 + S4-T2 的抽取）。
//
// 为什么是钉子而不是真跑：这几条都长在 effect 与 JSX 里，要 React + 真 DOM 才跑得起来
// （这个工程没有 jsdom，也不为一个测试引依赖）。所以与 SessionList.test.ts 钉焦点纪律、
// tests/test_api_wiring.py 钉 main.py 装配同一路数：**能纯函数化的都抽出去单测了**
// （mergeRows / resendTexts 在 blocks.test.ts、readErrorEnvelope 在 api.test.ts、
// imageOnlyHint 在 components/AttachChip.test.ts）。
//
// 重试环那一半 S4-T2 抽进了 stream.ts（开流做成了注入点），于是它**不再靠钉子**：
// reconnect.test.ts 交一个假 opener 真跑，断言开了几次流、带了什么头、出了几句横幅。
// 这里剩下的关于 stream.ts 的几条只钉"分界线"——哪一半住在哪个文件里（例如 runner 不许
// 碰 React 状态、演示帧必须靠注入才进得来），那些是抽取本身要保证的形状。
// 一个例外：**开流地址的模板**（`stream?after=${mid}`）仍然只能靠钉子。reconnect.test.ts
// 里的 URL 是测试自己定义的常量，它钉得住"runner 原样把 url 交下去"，钉不住"页面拼对了"
// ——两边一起看才闭环（R-S4-1，见文件末尾那一组）。
//
// 源码一律用 vite 的 `?raw` 读进来（不是 node:fs）：工程没装 @types/node，而 `?raw`
// 是 vite/client 自带的类型，vitest 与 vite build 两边都认。
import { describe, expect, it } from "vitest";

import CHAT from "./Chat.tsx?raw";
import MESSAGES from "../components/MessageList.tsx?raw";
import SESSIONS from "../components/SessionList.tsx?raw";
import API from "../api.ts?raw";
import TYPES from "../types.ts?raw";
import SSE from "../sse.ts?raw";
import STREAM from "../stream.ts?raw";
import COPY from "../copy.ts?raw";
import MOCK from "../mockstream.ts?raw";
import TOOLCARD from "../components/ToolCard.tsx?raw";
import BLOCKS from "../blocks.ts?raw";

describe("I1：换会话时迟到的详情不许落地", () => {
  it("loadDetail 进门先记 epoch，成功与失败两支都在 setDetail 之前判它", () => {
    // 进门第一件事就是取 epoch（中间只许有注释）
    expect(CHAT).toMatch(
      /async \(id: number\): Promise<SessionDetail \| null> => \{\s*(?:\/\/[^\n]*\n\s*)*const token = epoch\.current;/,
    );
    // 成功那一支：await 之后先比作废、再 setDetail
    expect(CHAT).toMatch(
      /const d = await apiGet<SessionDetail>\([^)]*\);\s*if \(epoch\.current !== token\) return null;\s*setDetail\(d\);/,
    );
    // 出错那一支同样作废：迟到的失败不许把新会话的详情清成 null，也不该弹一条
    // 关于一场已经离开的会话的横幅
    expect(CHAT).toMatch(
      /catch \(e\) \{\s*if \(epoch\.current !== token\) return null;\s*setDetail\(null\);/,
    );
  });

  it("判据是 epoch（换会话/卸载那个计数器），不是 turnSeq", () => {
    // 两个计数器分工不同：turnSeq 每开一轮也 +1，拿它判详情的话，同一场会话里发一句话
    // 就会把刚拉到的详情判成陈旧的
    expect(CHAT).toMatch(/const loadDetail = useCallback\([\s\S]{0,1200}epoch\.current !== token/);
    expect(CHAT).not.toMatch(/loadDetail[\s\S]{0,1200}turnSeq\.current !== token/);
  });
});

describe("I2：stream_timeout 升一条页面级横幅（流里那条会被 reconcile 抹掉）", () => {
  it("consume 认出终止性的那一帧，把它的人话交给 onTimeout（stream.ts）", () => {
    expect(STREAM).toContain("onTimeout: (text: string) => void;");
    // 判据是"同一帧里还带着横幅"（sse.ts 把 stream_timeout 翻成「横幅 + end」），
    // 不在 runner 里再读一遍 payload 的 code——那条契约只能有一份
    expect(STREAM).toContain('const banner = evs.find((b) => b.kind === "banner");');
    expect(STREAM).toContain(
      'if (banner !== undefined && banner.kind === "banner") h.onTimeout(banner.text);',
    );
    expect(STREAM).not.toMatch(/stream_timeout["']?\s*===|===\s*["']stream_timeout/);
    expect(CHAT).not.toMatch(/stream_timeout["']?\s*===|===\s*["']stream_timeout/);
  });

  it("onTimeout 推的是**页面级**横幅（onNotice → Chat 的 pushNotice），不是又一条流内块", () => {
    expect(STREAM).toContain('if (!stale()) onNotice(text, "err", humanText);');
    // 三个把手都在 handlers 上挂着（onTimeout 掉了就等于这条修复没接线）
    expect(STREAM).toMatch(/onTimeout,\s*onStall,/);
    // 页面这一头把横幅接到自己的 notice 状态上（resend 那个把手就是这么传到 Notices 的）
    expect(CHAT).toContain("onNotice: pushNotice,");
  });

  it("这一轮的人类原文一路带到 runTurn / runTurnStream，超时横幅才有话可预填", () => {
    expect(CHAT).toContain(
      "async function runTurn(targetSid: number, mid: number, humanText: string) {",
    );
    expect(CHAT).toContain("await runTurn(targetSid, posted.id, text);");
    expect(CHAT).toMatch(/runTurnStream\(\{[\s\S]{0,400}humanText,/);
  });

  it("id === null（这一轮压根没起来）那一种也有人说话：横幅不依赖 agent 行存在", () => {
    // keepExtras 只在 id !== null 时挂得上；页面级横幅是 setLive(null) 之后唯一还在的解释
    expect(CHAT).toMatch(/if \(id !== null\) keepExtras\(id, blocks\);/);
    expect(STREAM).toContain("onTimeout");
  });
});

describe("I5 + R-S3-13：「重发」只预填、不自动发送", () => {
  it("Chat 那一下只 setDraft，不调 send", () => {
    expect(CHAT).toContain(
      "const prefillResend = useCallback((text: string) => setDraft(text), []);",
    );
    // 两个渲染位（消息流与欢迎块）用的是同一个稳定引用
    expect(CHAT.match(/onResend=\{prefillResend\}/g)).toHaveLength(2);
  });

  it("半截断掉的回合（truncated=1）在正文之后挂一个「重发」", () => {
    expect(MESSAGES).toContain('import { blocksForMessage, phaseOf, resendTexts, sourcesOf } from "../blocks.ts";');
    expect(MESSAGES).toContain("const resend = resendTexts(messages);");
    expect(MESSAGES).toContain("{canResend && resend.has(m.id) && (");
    expect(MESSAGES).toContain("onClick={() => onResend(resend.get(m.id)!)}");
    expect(MESSAGES).toContain("重发");
  });

  it("stream_timeout 的横幅里也挂一个，判据是 notice 带没带 resend", () => {
    expect(MESSAGES).toContain("resend?: string;");
    expect(MESSAGES).toContain("{canResend && n.resend !== undefined && (");
    expect(MESSAGES).toContain("onClick={() => onResend(n.resend!)}");
  });

  it("页面级横幅只有一个渲染位（Notices 组件），两处不再各写一份 markup", () => {
    // 原来是 MessageList 里一份、Chat 的欢迎块里一份；「重发」加进来之后两份就开始漂
    expect(MESSAGES).toContain("export function Notices({");
    expect(MESSAGES).toContain(
      "<Notices notices={notices} onDismiss={onDismiss} canResend={canResend} onResend={onResend} />",
    );
    expect(CHAT).toContain('className="notices top"');
    expect(CHAT).not.toContain('role="alert"');
  });
});

describe("R-S3-19：归档会话上不挂「重发」（半死钮）", () => {
  it("Chat 判一次 canResend = !archived，两个渲染位都交同一个值", () => {
    expect(CHAT).toContain("const canResend = !archived;");
    // 欢迎块那处 Notices 与消息流里那处（后者由 MessageList 自己往下传）
    expect(CHAT.match(/canResend=\{canResend\}/g)).toHaveLength(2);
  });

  it("判据是**会话状态**，不是 Composer 那个三合一的 disabled", () => {
    // 抄 `sid === null || detail === null || archived` 也能让归档会话上按钮消失，但那两件
    // 是"还没选会话"，与能不能重发无关；两个开关合成一个，将来谁改一处就悄悄改坏另一处
    expect(CHAT).not.toContain("canResend = !(sid === null");
    expect(CHAT).not.toContain("canResend={!archived}");
    expect(CHAT).toContain('const archived = detail?.status === "archived"');
  });

  it("两颗按钮都过 canResend：回合上那颗与横幅里那颗都不许漏", () => {
    // 漏掉任何一颗，归档会话上就还剩一个点了发不出去的把手
    expect(MESSAGES).toContain("{canResend && resend.has(m.id) && (");
    expect(MESSAGES).toContain("{canResend && n.resend !== undefined && (");
    // 组件自己不猜会话状态：MessageRow 上没有 status（types.ts 的 SessionCore 才有），
    // 所以判据只能由调用方交进来，两个组件上它都是必填项（漏传就编不过）
    expect(MESSAGES).toContain("canResend: boolean;");
    expect(MESSAGES.match(/canResend: boolean;/g)).toHaveLength(2);
    expect(MESSAGES).not.toMatch(/status\s*===?\s*["']archived["']/);
  });

  it("是**不渲染**，不是又给它加一个 disabled", () => {
    // 灰掉的「重发」还占着正文下面那一行、读屏还会念到它，点下去什么都不发生——
    // 半死钮比没有钮更让人怀疑界面坏了
    expect(MESSAGES).not.toMatch(/className="resend"[\s\S]{0,120}disabled/);
    expect(MESSAGES).not.toMatch(/disabled[\s\S]{0,120}className="resend"/);
  });

  it("消息流里那颗由 MessageList 往下传给 Notices（不再各写一份判据）", () => {
    expect(MESSAGES).toContain(
      "<Notices notices={notices} onDismiss={onDismiss} canResend={canResend} onResend={onResend} />",
    );
  });
});

describe("R-S3-18：乐观行在**渲染处**按 id 集合的差对齐", () => {
  it("渲染用的是 mergeRows，不是把两个数组直接接起来", () => {
    expect(CHAT).toContain(
      "const messages = mergeRows(detail?.messages ?? [], optimistic);",
    );
    expect(CHAT).not.toContain("[...(detail?.messages ?? []), ...optimistic]");
    expect(CHAT).toContain('import { mergeRows } from "../blocks.ts";');
  });

  it("清空 optimistic 那两下仍然在（去重不靠它，但不能让它一直长）", () => {
    // 换会话那一下、runTurn 收尾（附件两段式后 upload 不再发消息，没有第三处）
    expect(CHAT.match(/setOptimistic\(\[\]\)/g)).toHaveLength(2);
  });
});

describe("收尾清理：单点化与形状", () => {
  it("错误信封的解法只剩 api.ts 一处（开流失败那条路也调它）", () => {
    expect(API).toContain("export async function readErrorEnvelope(res: Response)");
    expect(API).toContain("const { message, extra } = await readErrorEnvelope(res);");
    expect(STREAM).toContain("const { message } = await readErrorEnvelope(res);");
    // 抄的那一份删干净了（连它用的 fallbackMessage 也不再需要 import）
    expect(CHAT).not.toContain("streamErrorText");
    expect(CHAT).not.toContain("fallbackMessage");
    expect(STREAM).not.toContain("streamErrorText");
    expect(STREAM).not.toContain("fallbackMessage");
    expect(API.match(/let message = fallbackMessage/g)).toHaveLength(1);
  });

  it("单值的 toUiEvent 删了：映射入口只剩 toUiEvents", () => {
    expect(SSE).not.toContain("export function toUiEvent(");
    expect(SSE).toContain("export function toUiEvents(f: Frame): UiEvent[] {");
    expect(CHAT).not.toContain("toUiEvent(");
    expect(STREAM).not.toContain("toUiEvent(");
    expect(STREAM).toContain("const evs = toUiEvents(f);");
  });

  it("SessionDetail 不再假装自己有 owner_name（详情接口不 JOIN users）", () => {
    expect(TYPES).not.toContain("interface SessionDetail extends SessionSummary");
    expect(TYPES).toContain("export interface SessionDetail extends SessionCore {");
    expect(TYPES).toMatch(
      /export interface SessionSummary extends SessionCore \{\s*\/\*\*[\s\S]*?owner_name: string;/,
    );
  });

  it("归属者名字从会话列表取，不再拿「别人」当名字用", () => {
    expect(CHAT).toContain('const ownerName = summary?.owner_name.trim() ?? "";');
    expect(CHAT).toContain('"别人的会话"');
    expect(CHAT).not.toContain('?? "别人"');
    expect(CHAT).not.toContain("别人 的会话");
  });

  it("ToolResult 住在 types.ts：纯函数模块（blocks.ts）不再反过来 import 一个组件", () => {
    expect(TYPES).toContain("export interface ToolResult {");
    expect(TOOLCARD).not.toContain("export interface ToolResult");
    expect(TOOLCARD).toContain('import type { ToolResult } from "../types.ts";');
    expect(BLOCKS).toContain('import type { MessageRow, Outcome, ToolResult } from "./types.ts";');
    expect(BLOCKS).not.toContain('from "./components/ToolCard.tsx"');
  });

  it("两句共用的人话只在 copy.ts 里定义一份（调用点一律 import）", () => {
    expect(COPY).toContain('export const EMPTY_TITLE = "（空会话）";');
    expect(COPY).toContain('export const ARCHIVED_TEXT = "这个会话已归档，不能再发言";');
    // 三个调用点都不许再自己 const 一份：那就是"改了一句、另一句还是老话"的病根
    for (const [name, src, which] of [
      ["Chat.tsx", CHAT, ["EMPTY_TITLE", "ARCHIVED_TEXT"]],
      ["SessionList.tsx", SESSIONS, ["EMPTY_TITLE"]],
      ["mockstream.ts", MOCK, ["ARCHIVED_TEXT"]],
    ] as const) {
      for (const k of which) expect(src, `${name} 的 ${k}`).not.toContain(`const ${k} =`);
    }
    expect(CHAT).toContain('import { ARCHIVED_TEXT, EMPTY_TITLE } from "../copy.ts";');
    expect(SESSIONS).toContain('import { EMPTY_TITLE } from "../copy.ts";');
    expect(MOCK).toContain('import { ARCHIVED_TEXT } from "./copy.ts";');
  });
});

describe("S4-T2：流那半住在 stream.ts，这一页只剩页面自己的事", () => {
  it("Chat 不再自己开流：SSE 那三件与重试环、停摆守卫都搬走了", () => {
    expect(CHAT).not.toContain("resumeHeaders");
    expect(CHAT).not.toContain("isTransientStatus");
    expect(CHAT).not.toContain("pump(");
    expect(CHAT).not.toContain("STREAM_RETRIES");
    expect(CHAT).not.toContain("STALL_TEXT");
    expect(CHAT).not.toContain("readerChunks");
    expect(CHAT).toContain('import { runTurnStream } from "../stream.ts";');
    expect(CHAT).toContain('import type { TurnOutcome } from "../stream.ts";');
  });

  it("stream.ts 不 import react，也不 import mockstream（演示帧靠注入，生产包才摇得掉）", () => {
    expect(STREAM).not.toMatch(/from "react"/);
    expect(STREAM).not.toMatch(/from "\.\/mockstream/);
    expect(STREAM).not.toContain("navigate(");
    expect(STREAM).toContain("chunks?: (signal: AbortSignal) => AsyncIterable<string>;");
    // 开流是注入点：默认真 fetch，测试交一个假的（reconnect.test.ts 现在就是真跑行为）
    expect(STREAM).toContain(
      "export const fetchOpen: Opener = (url, headers, signal) => fetch(url, { headers, signal });",
    );
    expect(STREAM).toContain("open = fetchOpen");
  });

  it("中止把手与「正在写」的状态仍然由页面收（runner 一个 setState 都不碰）", () => {
    expect(STREAM).not.toMatch(/\bset(Live|Streaming|Detail|Notices|Sending|Optimistic|Extras)\(/);
    expect(CHAT).toMatch(
      /finally \{\s*\/\/ 读到 end 就主动关流[\s\S]{0,200}ctrl\.abort\(\);[\s\S]{0,140}if \(!stale\(\)\) setStreaming\(false\);/,
    );
    expect(CHAT).toContain("onBlocks: (blocks) => setLive(blocks),");
  });

  it("401 那一支的路由跳转留在页面（runner 只报一声 onUnauthorized）", () => {
    expect(STREAM).toContain("onUnauthorized?.();");
    expect(CHAT).toMatch(
      /onUnauthorized: \(\) => \{\s*clearIdentity\(\);\s*navigate\("\/login", \{ replace: true \}\);/,
    );
  });

  it("演示帧那一路仍然要 DEV 才认（构建时整段连同 mockstream 一起被摇掉）", () => {
    expect(CHAT).toContain(
      "import.meta.env.DEV && mock ? (signal: AbortSignal) => mockChunks(mock, signal) : undefined;",
    );
    expect(CHAT).toContain("chunks: devChunks,");
    expect(MOCK).toContain("export const MOCK_MARKER");
  });
});

// R-S4-1：终审那条 Important —— after= 的锚定当时只有 reconnect.test.ts 里一个**测试自己
// 定义的** URL 常量（`/api/chat/3/stream?after=900000001`）在钉，钉的是"runner 原样把 url
// 交下去"，页面拼那一条其实没人看着：把 Chat.tsx 里的 `?after=${mid}` 删掉，全套测试照绿，
// 而线上表现是"有历史的会话每开一轮都先把上一轮（可能早已封口）整条重放一遍"。
describe("R-S4-1：开流地址的 after= 由页面拼（游标锚在刚 POST 出来那条人话的 id 上）", () => {
  it("runTurnStream 的 url 就是 `/api/chat/${targetSid}/stream?after=${mid}`", () => {
    // 掉了 ?after= 这一行就红（这才是承重的钉子）
    expect(CHAT).toContain("url: `/api/chat/${targetSid}/stream?after=${mid}`,");
    // mid 从哪儿来：send() 把 POST 的响应 id 交给 runTurn（不是会话 id，也不是时间戳）
    expect(CHAT).toContain("await runTurn(targetSid, posted.id, text);");
    expect(CHAT).toContain(
      "async function runTurn(targetSid: number, mid: number, humanText: string) {",
    );
  });

  it("Chat 里带 /stream 的地址只此一处，且必须带 after=（别处不许再拼一条裸的）", () => {
    // 只认反引号包起来的字面量：文件头那两行注释里也写着 /api/chat/{id}/stream，
    // 那是给人看的口径，不是拼出来的地址（`[^\s`]` 顺带把跨行匹配也挡掉了）
    const urls = CHAT.match(/`\/api\/chat\/[^\s`]*stream[^\s`]*`/g) ?? [];
    expect(urls).toEqual(["`/api/chat/${targetSid}/stream?after=${mid}`"]);
    for (const u of urls) expect(u, u).toContain("stream?after=${");
  });

  it("runner 不自己拼地址：open() 收到的一律是 opts 交进来那个 url", () => {
    // 拼在 runner 里的话"哪一轮"就有两处口径（页面的 mid 与 runner 的模板），
    // 而 runner 压根不知道自己该锚在谁身上——它连 POST 都没发。
    // 这里只认代码，不认注释：stream.ts 的文件头与 TurnOptions.url 的 doc 里都写着
    // `/api/chat/{sid}/stream?after={mid}` 那串字（那是给人看的口径），所以判据是
    // "open() 从没被喂过字面量/模板串"，而不是"文件里没出现过 /api"。
    expect(STREAM).not.toMatch(/open\(\s*[`'"]/);
    expect(STREAM).not.toMatch(/fetch\(\s*[`'"]/);
    expect(STREAM).toContain(
      "const res = await open(url, resumeHeaders(lastSeenId), ctrl.signal);",
    );
  });
});

describe("归档可逆：撤销与取消归档（2026-10-30）", () => {
  it("归档成功后给一条带「撤销」的 notice；顶栏与侧栏都能取消归档", () => {
    expect(CHAT).toContain("/unarchive");
    expect(CHAT).toContain("label: \"撤销\"");
    expect(CHAT).toContain("取消归档");
    expect(CHAT).toContain("onUnarchive={unarchive}");
    // 撤销动作由 Notice.action 渲染出来（不是又一颗"重发"）
    expect(MESSAGES).toContain("n.action !== undefined");
  });
});

describe("能力感知：一个 canBuild 喂三处（2026-10-30）", () => {
  it("canBuild 由 api.ts 的 canBuildWiki(me) 算一次，三处共用", () => {
    expect(CHAT).toContain("const canBuild = canBuildWiki(me);");
    expect(CHAT).toContain("allowUploads={canBuild}");
    expect(CHAT).toContain("canFeed={canBuild}");
    expect(CHAT).toMatch(/function feed\(\) \{\s*if \(archived \|\| !canBuild\) return;/);
    // 上传那条路也有兜底：受限账号不走 POST /api/uploads
    expect(CHAT).toMatch(
      /async function stageFiles\(files: File\[\]\) \{[\s\S]{0,400}if \(archived \|\| uploading \|\| !canBuild\) return;/,
    );
  });

  it("MessageList 把 canFeed 透传给空消息流的欢迎块，自己不猜权限", () => {
    expect(MESSAGES).toContain("canFeed: boolean;");
    expect(MESSAGES).toContain("canFeed={canFeed}");
    expect(MESSAGES).not.toContain("canBuildWiki");
  });
});

describe("可见范围：当前会话详情 404 时提示并回 /（2026-10-30）", () => {
  it("404 借 location.state 把话带到首页（直接 push 会被换会话 effect 清掉）", () => {
    expect(CHAT).toContain('if (e instanceof ApiError && e.status === 404) {');
    expect(CHAT).toContain('navigate("/", {');
    expect(CHAT).toContain("state: { notice: errText(e, \"这场会话看不到了\") }");
    // 落地后补横幅、再把 state 擦掉：刷新/后退不重复弹
    expect(CHAT).toContain("const state = location.state as { notice?: unknown } | null;");
    expect(CHAT).toContain("pushNotice(text);");
    expect(CHAT).toContain("navigate(location.pathname, { replace: true, state: null });");
    // 404 走回首页那条路；其它错误仍就地挂兜底横幅
    expect(CHAT).toContain('pushNotice(errText(e, "这场会话没拉下来"));');
  });
});
