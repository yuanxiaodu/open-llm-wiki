import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentPropsWithoutRef } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api.ts";
import { buildWikiLinkIndex, chatLinkMode, linkifyWikilinks } from "../chatLinks.ts";
import type { WikiLinkIndex } from "../chatLinks.ts";
import { normalizeWikiList, safeMarkdownHref, wikiPageHref } from "../wiki.ts";
import type { WikiPageMeta } from "../wiki.ts";
import { AttachChip } from "./AttachChip.tsx";
import { ChatWelcome } from "./ChatWelcome.tsx";
import { ToolCard } from "./ToolCard.tsx";
import { blocksForMessage, phaseOf, resendTexts, sourcesOf } from "../blocks.ts";
import type { LiveBlock } from "../blocks.ts";
import type { MessageRow } from "../types.ts";

// 消息流：人类气泡（右）+ 智能体回合（左：who 行 + markdown 正文 + 工具卡片 + 横幅）。
//
// 一个回合的正文不是一个串，而是**按到达顺序**的一串块（LiveBlock）：正文可能在工具
// 调用之前就开始流，工具结果回来后又接着说，system/error 帧还要插在中间。所以渲染的
// 单位是块，不是"一条消息一段文本"。
//
// 块从哪来（裁定 R-S3-8 之后有两条，规则在 blocks.ts，这里只负责画）：
//   - 已落库的回合：详情接口每条 agent 消息都带 events，用**与流上同一套**帧语义折成块，
//     刷新页面之后工具卡片与横幅照样在；库赢过 Chat 那边攒下的 extras。
//   - 正在流的那一轮（live）与库里没有 events 的回合（DEV 演示帧、纯正文）：Chat 交进来的块。
// 两条路最后都落到下面同一个 <Blocks>，所以历史卡片与流式卡片长得一模一样。
//
// 另有两件挂在消息上的动作：半截断掉的回合（库里 truncated=1）与 stream_timeout 的页面级
// 横幅各带一个「重发」（裁定 R-S3-13），点了只把那一轮的人类原文**预填**进输入框，不自动发送。
// 归档会话上这两颗都不渲染（裁定 R-S3-19，判据是 canResend）：那时输入区整块 disabled，
// 预填进去那句既改不了也发不出去，一个点了没用的把手比不摆更坏。

/**
 * markdown 渲染器**按需加载**（裁定 R-S3-9）。
 *
 * streamdown 把 shiki 的语法高亮与 remark 全家拖在一起，静态 import 时它们全进首屏
 * chunk（拆之前 792.67 kB，vite 那条 >500 kB 的告警就是它）。改成 lazy 之后首屏只剩
 * React + 路由 + 这几页自己的代码，而登录页与邀请页**一个字节都不会去取它**——
 * 那两页压根没有 markdown 要渲染。
 *
 * 代价是第一次要画正文时多一次网络往返。所以 MessageList 一挂载就预热（下面那个
 * useEffect）：对话页上正文随时可能到，等第一帧来了才去取会让人看见一下空白。
 * 预热与 lazy 指的是同一个 chunk，第二次 import() 直接命中模块缓存。
 *
 * fallback 是 null 而不是"加载中"：预热之后这条边界几乎总在已解析状态，真撞上未解析
 * 的那一瞬，闪一句"加载中"比留白更吵。
 */
const Streamdown = lazy(() => import("streamdown").then((m) => ({ default: m.Streamdown })));

/** 对话正文里的链接：/wiki 一律新标签页打开；其它站内链接仍走 SPA。 */
function ChatMarkdownLink({ href = "", children }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const mode = chatLinkMode(href);
  const safe = safeMarkdownHref(href);
  if (mode === "blocked") return <span>{children}</span>;
  if (mode === "wiki" || mode === "external") {
    return (
      <a href={safe} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  }
  if (safe.startsWith("#")) {
    return (
      <a href={safe} rel="noreferrer">
        {children}
      </a>
    );
  }
  return <Link to={safe}>{children}</Link>;
}

const CHAT_MD_COMPONENTS = { a: ChatMarkdownLink };

/** 页面级横幅（409 / 上传失败 / 开流失败 / 停摆 / 流超时），与流内帧横幅分开摆。 */
export interface Notice {
  id: number;
  text: string;
  level: "warn" | "err";
  /**
   * 带这一项就在横幅里挂一个「重发」钮，值是点了要预填进输入框的那句原文
   * （裁定 R-S3-13；**只预填不发送**，见 onResend）。
   *
   * 为什么要页面级的一份：stream_timeout 的横幅只活在流上（后端 _tail 有意不落库），
   * 一轮收尾重拉详情之后 db-wins 会把它抹掉（blocks.ts 的 blocksForMessage）——人刚看见
   * "这条流等得太久了"，一眨眼那句话就没了，只剩自己那句话挂在屏幕上。升一条页面级横幅，
   * 它就在，还带着重发的把手。
   *
   * 归档会话上这一项被 canResend 压掉（裁定 R-S3-19）：横幅照旧说人话，只是不给把手。
   */
  resend?: string;
  /**
   * 一个额外动作（2026-10-30 起）：归档后的「撤销」用它。
   * 与 resend 分开：resend 是"把原话填回输入框"，action 是任意回调。
   */
  action?: { label: string; onClick: () => void };
}

function Blocks({
  blocks, streaming, wikiIndex,
}: {
  blocks: LiveBlock[];
  streaming: boolean;
  wikiIndex: WikiLinkIndex;
}) {
  const sources = sourcesOf(blocks);
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === "text") {
          return (
            <div className="body" key={`t${i}`}>
              <Suspense fallback={null}>
                {/* tableMaxHeight={0}：关掉 streamdown 默认给表格外壳套的 max-height:300px。
                    那层壳的 overflow 是 Tailwind 类，本仓库不引 Tailwind，壳被压扁后表格
                    照旧按内容高度画，多出来的部分就压到后面段落上（2026-11-08 的四段表）。
                    横向溢出仍由 theme.css 的 `.md table { display:block; overflow:auto }` 接手。 */}
                <Streamdown
                  className="md"
                  mode={streaming ? "streaming" : "static"}
                  controls={false}
                  animated={false}
                  lineNumbers={false}
                  components={CHAT_MD_COMPONENTS}
                  urlTransform={safeMarkdownHref}
                  tableMaxHeight={0}
                >
                  {linkifyWikilinks(b.text, wikiIndex)}
                </Streamdown>
              </Suspense>
              {streaming && i === blocks.length - 1 && <span className="caret" aria-hidden="true" />}
            </div>
          );
        }
        if (b.kind === "tool") {
          return <ToolCard key={`c${i}`} name={b.name} args={b.args} result={b.result} />;
        }
        if (b.kind === "outcome") {
          // 对话页不暴露后台改了什么（2026-10-30 委托人口径 2）：outcome 帧照旧由
          // sse.ts/blocks.ts 解析（日志与历史兼容需要它），但这里什么都不画。
          // 工具卡片与下面的「依据」保留——它们回答"答案从哪来"，不是"后台改了几页"。
          return null;
        }
        return (
          <div className={`banner ${b.level} in-stream`} key={`b${i}`} role="status">
            {b.text}
          </div>
        );
      })}
      {!streaming && sources.length > 0 && (
        <div className="sources" aria-label="依据页">
          <span className="src-lab">依据</span>
          {sources.map((path) => (
            <Link className="src-chip" key={path} to={wikiPageHref(path)}>
              {path.replace(/^.*\//, "").replace(/\.md$/i, "")}
            </Link>
          ))}
        </div>
      )}
    </>
  );
}

interface Props {
  messages: MessageRow[];
  /**
   * 已收尾回合里**库没有**的那些块（键 = agent 消息 id），由 Chat 在重拉详情后交给这里。
   * 只是兜底：这条消息的 events 非空时一律以库为准（见 blocks.ts 的 db-wins）。
   */
  extras: Map<number, LiveBlock[]>;
  /** 正在流的那一轮；null 表示没在跑。 */
  live: LiveBlock[] | null;
  streaming: boolean;
  meName: string;
  notices: Notice[];
  onDismiss: (id: number) => void;
  /**
   * 这一场会话能不能重发（裁定 R-S3-19）。false = 两颗「重发」都不渲染：回合上那颗与
   * 页面级横幅里那颗。
   *
   * 判据在调用方（pages/Chat.tsx 交的是 `!archived`，即会话状态不是 archived），这里只
   * 负责画不画——组件不自己去猜会话状态：它手里只有 messages，而 MessageRow 上没有
   * status 这一列（types.ts 的 SessionCore 才有），从消息去反推会话状态只会推错。
   *
   * 为什么是**不渲染**而不是 disabled：一个灰掉的「重发」还是占着正文下面那一行，读屏的
   * 人也还是会念到它，而点它什么都不会发生——半死钮比没有钮更让人怀疑界面坏了。与
   * blocks.ts 里 resendTexts"原文是空白就不给键"是同一条纪律。
   */
  canResend: boolean;
  /**
   * 「重发」被点了：把这一轮人类说的那句原文放回输入框（裁定 R-S3-13）。
   *
   * 只是**预填**，不自动发送——那一轮为什么断的（额度、断流、超轮）界面并不知道，
   * 替人决定"再发一次"是越权；话回到框里，人自己看一眼再按发送。
   */
  onResend: (text: string) => void;
  /**
   * 空会话欢迎块里点了建议 chip：把文本填进输入框（**不发送**）。
   * 与「重发」的 onResend 一样只是预填；空消息流的欢迎块需要它。
   */
  onSuggest: (text: string) => void;
  /** 空态「整理一份资料」：打开文件选择器（Composer 接信号）。 */
  onFeed: () => void;
  /** 空态「体检知识库」：把体检指令填进输入框（不发送）。 */
  onDoctor: () => void;
  /**
   * 这个账号能不能摄入材料（透传给 ChatWelcome）：false 时空消息流里不摆
   * 「整理一份资料」卡。与 Composer 的 allowUploads 是同一个判据，由 Chat 交同一个值。
   */
  canFeed: boolean;
}

export function MessageList({
  messages,
  extras,
  live,
  streaming,
  meName,
  notices,
  onDismiss,
  canResend,
  onResend,
  onSuggest,
  onFeed,
  onDoctor,
  canFeed,
}: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  // 只有人本来就贴着底时才跟着滚：往上翻看历史的时候被强行拽回底部是最烦的一种交互。
  const stickRef = useRef(true);
  // 哪些回合半截断了、以及各自该重发哪句（blocks.ts 的 resendTexts，一趟扫完）。
  const resend = resendTexts(messages);
  /** 对话正文里的 [[wikilink]] 要按 wiki 页面表换成 /wiki 链接；拉一次即可。 */
  const [wikiPages, setWikiPages] = useState<WikiPageMeta[]>([]);
  useEffect(() => {
    let alive = true;
    apiGet<unknown>("/api/wiki")
      .then((raw) => { if (alive) setWikiPages(normalizeWikiList(raw).pages); })
      .catch(() => { /* 取不到就保留原始 [[...]]，不打断对话 */ });
    return () => { alive = false; };
  }, []);
  const wikiIndex = useMemo(() => buildWikiLinkIndex(wikiPages), [wikiPages]);

  // 预热 markdown 渲染器那块 chunk（见文件头 R-S3-9 那段）：一进对话页就取，
  // 别等第一帧正文到了才去取。失败不吵——真到用的时候 lazy 会自己再试一次。
  useEffect(() => {
    void import("streamdown").catch(() => {});
  }, []);

  function onScroll() {
    const el = boxRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  // 不写依赖数组：流式期间每次渲染都要跟一次底，而"要不要跟"由 stickRef 决定。
  useEffect(() => {
    const el = boxRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  });

  return (
    <div className="stream" ref={boxRef} onScroll={onScroll}>
      {messages.length === 0 && !live && (
        <ChatWelcome
          onSuggest={onSuggest}
          onFeed={onFeed}
          onDoctor={onDoctor}
          canFeed={canFeed}
        />
      )}

      {messages.map((m) =>
        m.role === "human" ? (
          <div className="msg user" key={m.id}>
            <div className="user-col">
              {m.user_name && m.user_name !== meName && (
                <div className="who-user">{m.user_name}</div>
              )}
              {m.content.trim() !== "" && (
                <div className="b">
                  <div className="text">{m.content}</div>
                </div>
              )}
              {m.attachments.length > 0 && (
                <div className="chips">
                  {m.attachments.map((a) => (
                    <AttachChip key={a.id} att={a} />
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="msg agent" key={m.id}>
            <div className="who">
              <span className="dot" aria-hidden="true" />
              智能体
              {m.truncated === 1 && <span className="pill cut">中断</span>}
            </div>
            <Blocks
              blocks={blocksForMessage(m, extras.get(m.id))}
              streaming={false}
              wikiIndex={wikiIndex}
            />
            {canResend && resend.has(m.id) && (
              // 这一轮半截断了（库里 truncated=1）：把当时那句提问放回输入框，人自己再发一次。
              // 摆在正文之后而不是 who 行里——「中断」那个 pill 是状态，这一件是动作。
              // 归档会话上整颗不渲染（canResend=false，裁定 R-S3-19）：输入区那时是灰的，
              // 预填进去也发不出去，「中断」那个 pill 照旧说清了这一轮的状态。
              <button
                type="button"
                className="resend"
                title="把这一轮你说的那句放回输入框"
                onClick={() => onResend(resend.get(m.id)!)}
              >
                重发
              </button>
            )}
          </div>
        ),
      )}

      {live && (
        // 正在流的那一轮挂 aria-live="polite"（T5 评审 M4）：不挂的话读屏的人压根
        // 不知道智能体已经开口了。polite 而不是 assertive——正文是一帧一帧长出来的，
        // assertive 会一句一句打断人正在听的东西。aria-busy 标"还在写"；收尾时 live
        // 撤下、这一轮进 messages（不在 live 区里），这块区域自然就安静了。
        <div className="msg agent live" data-live="1" aria-live="polite" aria-busy={streaming}>
          <div className="who">
            <span className="dot" aria-hidden="true" />
            智能体
            {streaming && <span className="pill on">{phaseOf(live) ?? "正在写"}</span>}
          </div>
          <Blocks blocks={live} streaming={streaming} wikiIndex={wikiIndex} />
        </div>
      )}

      <Notices notices={notices} onDismiss={onDismiss} canResend={canResend} onResend={onResend} />
    </div>
  );
}

/**
 * 页面级横幅那一条（409 / 上传失败 / 开流失败 / 停摆 / 流超时）。
 *
 * 抽出来是因为它有**两个**渲染位：消息流末尾，与还没选会话时对话页的欢迎块
 * （pages/Chat.tsx）。原来两处各写一份 markup——「重发」这个把手加进来之后它们就开始漂
 * （一处有、一处没有），而同一句话在页面两个位置长得不一样比写两遍更糟。className 只在
 * 欢迎块那处换成 "notices top"（间距不同，其余一模一样）。canResend 同理：两处都由调用方
 * 交同一个值，不然归档会话上横幅里那颗就会单独活下来（裁定 R-S3-19）。
 */
export function Notices({
  notices,
  onDismiss,
  canResend,
  onResend,
  className = "notices",
}: {
  notices: Notice[];
  onDismiss: (id: number) => void;
  /** false = 横幅照旧说人话，只是不挂「重发」（裁定 R-S3-19；理由见 Props.canResend）。 */
  canResend: boolean;
  onResend: (text: string) => void;
  className?: string;
}) {
  if (notices.length === 0) return null;
  return (
    <div className={className}>
      {notices.map((n) => (
        <div className={`banner ${n.level}`} key={n.id} role="alert">
          <span>{n.text}</span>
          {n.action !== undefined && (
            <button type="button" className="resend" onClick={n.action.onClick}>
              {n.action.label}
            </button>
          )}
          {canResend && n.resend !== undefined && (
            <button
              type="button"
              className="resend"
              title="把刚才那句放回输入框"
              onClick={() => onResend(n.resend!)}
            >
              重发
            </button>
          )}
          <button type="button" className="x" onClick={() => onDismiss(n.id)} aria-label="收起">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
