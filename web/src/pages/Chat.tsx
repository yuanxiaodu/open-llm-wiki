import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ApiError,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPostForm,
  canBuildWiki,
  clearIdentity,
  errText,
} from "../api.ts";
import { useIdentity } from "../useIdentity.ts";
import { useSiteName } from "../site.ts";
import { mergeRows } from "../blocks.ts";
import type { LiveBlock } from "../blocks.ts";
import { runTurnStream } from "../stream.ts";
import type { TurnOutcome } from "../stream.ts";
import { BODY_MAX, Composer } from "../components/Composer.tsx";
import { AppShell, ShellMenuButton } from "../components/AppShell.tsx";
import { ChatWelcome } from "../components/ChatWelcome.tsx";
import { MessageList, Notices } from "../components/MessageList.tsx";
import type { Notice } from "../components/MessageList.tsx";
import { SessionList } from "../components/SessionList.tsx";
import { ARCHIVED_TEXT, EMPTY_TITLE } from "../copy.ts";
import type { MessageRow, SessionDetail, SessionSummary } from "../types.ts";
import { canSendMessage, messageForm, normalizeStagedUpload, withoutStaged } from "../uploads.ts";
import type { StagedUpload } from "../uploads.ts";
import { mockChunks, mockPostMessage, readMockKind } from "../mockstream.ts";

// 对话页：会话侧栏（260px）+ 消息流 + 输入区（方向 B 样张的「对话」页）。
//
// 数据面只有三处：/api/chat/sessions（侧栏）、/api/chat/{id}（详情 = 消息 + 附件 + 事件）、
// /api/chat/{id}/stream?after=<mid>（一轮的流）。没有状态库；URL 是"当前哪一场会话"的
// 唯一来源（App.tsx 里钉的那条约定），侧栏点击走 router navigate。
//
// 发送流程（契约，别改顺序）：
//   POST messages（**表单**，见 postMessage）拿 mid → 立刻开 stream?after=mid →
//   逐帧渲染 → end 之后主动关流、重拉详情对齐落库态。
//
// 中间那半——开流、续读重试的预算与退避、45 秒停摆守卫、
// 「连接没响应」那句唯一的兜底横幅、done 与 stream_timeout 两种结束的分别——住在
// **stream.ts**（S4-T2 抽出去的），口径与常数写在它的文件头。这一页只交三样东西给它：
// 一个 AbortController、一组回调（onBlocks/onNotice/stale），以及收尾时拿回来的
// TurnOutcome（blocks/doneId/toLogin）。
//
// 这一页自己管的是：会话 CRUD 与上传、页面级横幅那一份状态、乐观行与"库里没有的块"
// 的兜底副本（extras）、窄屏抽屉的开关与焦点归还，以及渲染。

/** 发消息。这条路由收的是**表单**不是 JSON（chat.py：`content: str = Form("")`）：
 * 拿 apiPost 发 JSON 的话后端读到的 content 是空串，回来一句 400「空消息不入库」，
 * 人只看见"我明明写了字"。apiPostForm 不手设 Content-Type，让浏览器带上 multipart 的
 * boundary（api.ts 里那条注释）。
 */
type PostedMessage = {
  id: number;
  attachments?: { upload_id: number; parse_status: string | null }[];
};

function postMessage(
  sid: number,
  content: string,
  uploadIds: readonly number[],
): Promise<PostedMessage> {
  // 两段式：附件先 /api/uploads 暂存，这里把 upload_ids 与内容一起提交。
  return apiPostForm<PostedMessage>(`/api/chat/${sid}/messages`, messageForm(content, uploadIds));
}

// 另两句共用的人话（归档会话的提示位、空标题的会话叫什么）在 copy.ts：归档那句
// mockstream 还要一份、空标题那句 SessionList 也要一份，各写一遍迟早漂成两句。

/** 乐观行：POST 成功了但详情还没重拉，先用它在流里占位（真模式重拉后被库里的行替掉）。 */
function humanRow(id: number, content: string): MessageRow {
  return {
    id,
    session_id: 0,
    seq: 0,
    role: "human",
    user_id: null,
    user_name: null, // 自己发的，who 行不显示
    content,
    created_at: "",
    truncated: 0,
    usage_json: null,
    attachments: [],
    events: [], // 还没落库，自然也没有事件行
  };
}

function agentRow(id: number, content: string): MessageRow {
  return { ...humanRow(id, content), role: "agent" };
}

export function Chat() {
  const me = useIdentity();
  // 没有会话标题时（侧栏还没加载/新建会话）大标题退回站名；站名不写死，来自 /api/site。
  const siteName = useSiteName();
  const navigate = useNavigate();
  const location = useLocation();
  const { sessionId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();

  const parsed = sessionId === undefined ? Number.NaN : Number(sessionId);
  const sid = Number.isInteger(parsed) && parsed > 0 ? parsed : null;

  /**
   * 当前账号能不能建设 wiki（摄入材料 / 让智能体改页）。管理员恒 true。
   * 三个入口共用这一个值：Composer 的附件（allowUploads）、欢迎卡与空消息流里的
   * 「整理一份资料」（canFeed）、以及 feed() 的守卫——三处各算一次迟早漂。
   */
  const canBuild = canBuildWiki(me);

  // DEV 演示帧（裁定 R-S3-7）：三处调用点全部带 import.meta.env.DEV，
  // 构建时 DEV → false，整段死代码连同 mockstream.ts 一起被摇掉。
  const mock = import.meta.env.DEV ? readMockKind(location.search) : null;

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [notices, setNotices] = useState<Notice[]>([]);
  /** 正在流的那一轮的块；null = 没在跑。 */
  const [live, setLive] = useState<LiveBlock[] | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [creating, setCreating] = useState(false);
  /** 选文件后只暂存（POST /api/uploads），发送时才随消息提交 upload_ids。 */
  const [staged, setStaged] = useState<StagedUpload[]>([]);
  /** 空态「整理一份资料」卡的信号：+1 = 让 Composer 打开文件选择器。 */
  const [feedSignal, setFeedSignal] = useState(0);
  /** 演示帧模式下没有落库的行，靠它把这一轮留在流里（真模式重拉详情后清空）。 */
  const [optimistic, setOptimistic] = useState<MessageRow[]>([]);
  /**
   * 已收尾回合的工具卡/横幅：键 = agent 消息 id。
   *
   * 裁定 R-S3-8 之后详情接口自己就带 events，库能说的话一律听库的（blocks.ts 的 db-wins），
   * 这份客户端副本只剩两种用处：库里没有事件行的回合（纯正文的一轮里那条只活在流上的
   * stream_timeout 横幅）与 DEV 演示帧合成的行（压根不落库）。
   */
  const [extras, setExtras] = useState<Map<number, LiveBlock[]>>(() => new Map());
  const abortRef = useRef<AbortController | null>(null);
  type PendingAction = {
    kind: "send";
    text: string;
    uploadIds: number[];
    staged: StagedUpload[];
  };
  /**
   * 首页没有会话时先建会话、再执行这一趟动作。建会话要 navigate 到 /chat/:id，
   * 换会话 effect 会把在飞状态清掉——所以动作**不能**在 navigate 前启动；先存在这里，
   * 等新会话的详情落地后由一个 effect 取走执行（见下面 flush 那段）。
   */
  const pendingAction = useRef<PendingAction | null>(null);

  /** 会话栏右侧的 ⋯ 菜单（窄屏也能用；归档入口与侧栏那份同源）。 */
  const [headMenu, setHeadMenu] = useState(false);
  const headMenuRef = useRef<HTMLDivElement>(null);
  const headMoreRef = useRef<HTMLButtonElement>(null);
  /**
   * 两个作废计数器，分工不同：
   *   epoch   —— 换会话 / 卸载时 +1：作废"这一趟 POST 或上传"的收尾（别再动新会话的按钮态）；
   *   turnSeq —— 每开一轮流 +1（换会话/卸载时也 +1）：作废在飞的帧回调与收尾对齐。
   * 分成两个是因为 runTurn 自己也要 +1，而 send 的收尾不能被它作废掉。
   */
  const epoch = useRef(0);
  const turnSeq = useRef(0);
  const noticeSeq = useRef(1);

  /**
   * 页面级横幅。
   *
   * level 默认 err（这一页的 warn 都是流内横幅，走 foldEvent 那条路）。
   * resend（裁定 R-S3-13）：给了就在横幅里挂一个「重发」钮，值是点了要预填进输入框的
   * 那句原文；目前只有 stream_timeout 那一条会给它。
   */
  const pushNotice = useCallback(
    (
      text: string,
      level: "warn" | "err" = "err",
      resend?: string,
      action?: Notice["action"],
    ) => {
      // 自动摄入轮没有"人说的话"可重发：空的 resend 不挂把手（别摆一个点了没用的钮）
      const resendText = resend !== undefined && resend.trim() !== "" ? resend : undefined;
      const id = noticeSeq.current++;
      setNotices((prev) => [
        ...prev.filter((n) => n.text !== text), // 同一句话不叠两遍（坏帧会连着来）
        { id, text, level, resend: resendText, action },
      ]);
      return id;   // 调用方要"撤销"时拿它把自己的那条横幅收掉
    },
    [],
  );

  const dismiss = useCallback((id: number) => {
    setNotices((prev) => prev.filter((n) => n.id !== id));
  }, []);

  /**
   * 「重发」被点了：把那一轮人类说的原文放回输入框，**不自动发送**（裁定 R-S3-13）。
   *
   * 就 setDraft 一件事。为什么不顺手 send()：那一轮为什么断的（额度、断流、超轮、超时）
   * 界面并不知道，替人决定"再发一次"是越权；而且发送钮此刻可能正被按住（另一轮还在写、
   * 或正在上传），自动发送只会撞出一个 409。spec §8 的"一点重发"就是这个意思：话回到
   * 框里，人自己看一眼、自己按发送。
   *
   * useCallback 出来的稳定引用，理由与下面的 closeDrawer 同一条（别让它每帧换一个身份）。
   */
  const prefillResend = useCallback((text: string) => setDraft(text), []);

  const loadSessions = useCallback(async () => {
    try {
      setSessions(await apiGet<SessionSummary[]>("/api/chat/sessions"));
    } catch (e) {
      pushNotice(errText(e, "会话列表没拉下来"));
    }
  }, [pushNotice]);

  const loadDetail = useCallback(
    async (id: number): Promise<SessionDetail | null> => {
      // 进门先把 epoch 记下来（终审 I1）。await 的这段时间里人可能已经点了侧栏另一场会话
      // （或整页卸载了），那时候再 setDetail 就是把**上一场**的消息摆到**这一场**的标题
      // 下面：侧栏点快一点就能看见 A 的会话里挂着 B 的对话，而 URL 说的是 A。判据与
      // send()/upload() 用的是同一个计数器（epoch 由换会话那个 effect +1）。
      // 出错那一支同样作废：迟到的失败不许把新会话的详情清成 null、也不该弹一条关于
      // 一场已经离开的会话的横幅。
      const token = epoch.current;
      try {
        const d = await apiGet<SessionDetail>(`/api/chat/${id}`);
        if (epoch.current !== token) return null;
        setDetail(d);
        return d;
      } catch (e) {
        if (epoch.current !== token) return null;
        setDetail(null);
        // 详情 404 = 会话不存在，或被管理员切到"成员只看自己的"之后不可见（后端对
        // 不可见与不存在一律回 404，见 chat.py 的 _visible_session_or_404）。留在这一页
        // 只会在标题下挂着空消息流，所以回首页；说明借 location.state 过河——换到 /
        // 那一下 sid 变化的 effect 会 setNotices([])，跳转前 push 的话一帧都活不下来。
        if (e instanceof ApiError && e.status === 404) {
          navigate("/", {
            replace: true,
            state: { notice: errText(e, "这场会话看不到了") },
          });
          return null;
        }
        pushNotice(errText(e, "这场会话没拉下来"));
        return null;
      }
    },
    [pushNotice, navigate],
  );

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  // 换会话：中止还挂着的流，把上一场的在飞状态全清掉（URL 是唯一来源）
  useEffect(() => {
    epoch.current += 1;
    turnSeq.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    setDetail(null);
    setLive(null);
    setStreaming(false);
    setSending(false);
    setUploading(false);
    setOptimistic([]);
    setExtras(new Map());
    setNotices([]);
    if (sid !== null) void loadDetail(sid);
  }, [sid, loadDetail]);

  // 自动建会话之后：等新会话的详情落地，再执行排队的那一趟发送/上传。
  // 只在 detail.id === sid 时动手，避免拿旧会话的详情去发到新 sid 上。
  useEffect(() => {
    const action = pendingAction.current;
    if (action === null || sid === null || detail === null || detail.id !== sid) return;
    pendingAction.current = null;
    void sendTo(sid, action.text, action.uploadIds, action.staged);
    // sendTo 是函数声明（提升），依赖只跟"会话与详情落定"有关
  }, [sid, detail]);

  // 卸载：别把流挂着
  useEffect(
    () => () => {
      epoch.current += 1;
      turnSeq.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
    },
    [],
  );

  // 借 location.state 带回来的那一条横幅（目前只有"详情 404 → 回首页"用它）。
  // 这个 effect 必须排在上面「换会话清空 notices」之后：先清、后补，落地后才看得见。
  // 补完把 state 擦掉（replace 到同一路径），刷新/后退不会重复弹。
  useEffect(() => {
    const state = location.state as { notice?: unknown } | null;
    const text = state !== null && typeof state.notice === "string" ? state.notice : "";
    if (text === "") return;
    pushNotice(text);
    navigate(location.pathname, { replace: true, state: null });
  }, [location.state, location.pathname, navigate, pushNotice]);

  async function newSession() {
    if (creating) return;
    setCreating(true);
    try {
      const created = await apiPost<{ id: number }>("/api/chat/sessions");
      await loadSessions();
      navigate(`/chat/${created.id}`);
    } catch (e) {
      pushNotice(errText(e, "新会话没开成"));
    } finally {
      setCreating(false);
    }
  }

  /** 改名/归档：出错就抛给弹窗自己摆人话（403「只有开这个会话的人能改名」等）。 */
  async function rename(id: number, title: string): Promise<void> {
    await apiPatch<{ ok: boolean }>(`/api/chat/${id}`, { title });
    await loadSessions();
    if (id === sid) await loadDetail(id);
  }

  async function archive(id: number): Promise<void> {
    await apiPost<{ ok: boolean }>(`/api/chat/${id}/archive`);
    await loadSessions();
    if (id === sid) await loadDetail(id);
    // 归档可逆：给一条 5 秒内可点的撤销（侧栏同时把它收进「已归档」折叠区）。
    const noticeId = pushNotice("已归档", "warn", undefined, {
      label: "撤销",
      onClick: () => {
        dismiss(noticeId);
        void unarchive(id);
      },
    });
  }

  async function unarchive(id: number): Promise<void> {
    try {
      await apiPost<{ ok: boolean }>(`/api/chat/${id}/unarchive`);
      await loadSessions();
      if (id === sid) await loadDetail(id);
    } catch (e) {
      pushNotice(errText(e, "取消归档没成功"));
    }
  }

  /** 点欢迎页建议：只把文本填进输入框（不发送）；没会话就先开一场，免得框是禁用的。 */
  function suggest(text: string) {
    // chip 只预填，不建会话、不发送——发送/上传时才会自动建。
    setDraft(text);
    window.setTimeout(() => document.getElementById("composer-text")?.focus(), 0);
  }

  /** 空态「整理一份资料」：只开文件选择器，选择后的暂存/发送口径与 📎 完全一致。 */
  function feed() {
    if (archived || !canBuild) return;
    setFeedSignal((n) => n + 1);
  }

  /** 空态「体检知识库」：把指令填进输入框（不发送，人可以改）。 */
  function doctor() {
    suggest("请给知识库做一次体检：列出断链、重复页、过期页和可疑页。");
  }

  // ⋯ 菜单：点外面 / Esc 关；Esc 把焦点还给 ⋯（与 SessionList 的条目菜单同一套）。
  useEffect(() => {
    if (!headMenu) return;
    function onDown(e: MouseEvent) {
      if (headMenuRef.current && !headMenuRef.current.contains(e.target as Node)) setHeadMenu(false);
    }
    function onKey(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") {
        setHeadMenu(false);
        headMoreRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [headMenu]);

  /**
   * 收尾：把流里攒下的工具卡/横幅按 message id 存起来。
   *
   * R-S3-8 之后这只是**兜底**：详情接口自己带 events，重拉之后渲染一律以库为准
   * （blocks.ts 的 db-wins）。留着的理由是库里没有的那两种块——只活在流上的
   * stream_timeout 横幅，与 DEV 演示帧合成的行。
   */
  function keepExtras(messageId: number, blocks: LiveBlock[]) {
    // 只有正文就不必存：库里的 content 已经是同一串，存了只是白占内存
    if (!blocks.some((b) => b.kind !== "text")) return;
    setExtras((prev) => new Map(prev).set(messageId, blocks));
  }

  /**
   * 跑一轮。humanText 是这一轮人类说的那句原文，**只**为「重发」带着（裁定 R-S3-13）：
   * 超时横幅要挂一个把手，点了把它放回输入框，而那时候详情里可能压根没有这一轮的行
   * （id === null 那一种），没处去查。
   *
   * 流那半（开流、续读重试、停摆守卫、「连接没响应」那句唯一的兜底横幅、done 与
   * stream_timeout 的分别）住在 stream.ts 的 runTurnStream 里，口径写在它的文件头。
   * 这一页只交三样东西过去：中止把手、一组回调、作废判据；拿回来的是 TurnOutcome，
   * 用它去和库里对齐（下面那半）。
   */
  async function runTurn(targetSid: number, mid: number, humanText: string) {
    const ctrl = new AbortController();
    const myTurn = (turnSeq.current += 1);
    abortRef.current = ctrl;
    /** 换过会话 / 卸载了 / 又开了一轮：这一轮的结果一律不许再碰状态。 */
    const stale = () => turnSeq.current !== myTurn;

    // 演示帧（裁定 R-S3-7）：给了 chunks 就不开真流、也不重试（录好的剧本断了重放只会
    // 让人看见同一轮说两遍），但它仍然认中止信号，停摆守卫掐流时行为与真 reader 一致。
    // 注入而不是让 stream.ts 去 import mockstream：那个模块必须在生产包里被摇掉，
    // 而 `import.meta.env.DEV &&` 这个前缀就是摇树的开关（构建时整句折成 undefined）。
    const devChunks =
      import.meta.env.DEV && mock ? (signal: AbortSignal) => mockChunks(mock, signal) : undefined;

    setLive([]);
    setStreaming(true);
    let out: TurnOutcome | null = null;
    try {
      out = await runTurnStream({
        // 开流带 after=<刚 POST 出来的那条人话 id>：不带的话有历史的会话会先把上一轮
        // （可能早已封口）整条重放一遍。
        url: `/api/chat/${targetSid}/stream?after=${mid}`,
        ctrl,
        humanText,
        stale,
        // 折叠规则在 blocks.ts（与历史那条路同一份）；stale 之后 runner 自己就不报了
        onBlocks: (blocks) => setLive(blocks),
        onNotice: pushNotice,
        // 开流 401 = 会话没了：口径同 api.ts（清身份、整页去登录），不再往下拉详情
        onUnauthorized: () => {
          clearIdentity();
          navigate("/login", { replace: true });
        },
        chunks: devChunks,
      });
    } finally {
      // 读到 end 就主动关流：done 之后不许再连（R-S2-25），这一句 abort 就是 close()
      ctrl.abort();
      if (abortRef.current === ctrl) abortRef.current = null;
      if (!stale()) setStreaming(false);
    }
    if (out === null) return; // runner 把错误都翻成横幅了，走不到这儿；真走到就别收尾

    const { blocks, doneId, toLogin } = out;
    if (stale() || toLogin) return;

    if (import.meta.env.DEV && mock) {
      // 演示帧没有落库的行：合成一条 agent 行把这一轮留在流里，形状与真模式收尾后一致。
      // 一个块都没收到（停摆守卫掐掉了）就不合成：摆一条空白的智能体消息是撒谎。
      if (blocks.length === 0) {
        setLive(null);
        return;
      }
      const text = blocks
        .filter((b): b is Extract<LiveBlock, { kind: "text" }> => b.kind === "text")
        .map((b) => b.text)
        .join("");
      const fakeId = -Date.now(); // 负数：不会撞上库里任何真 id
      keepExtras(fakeId, blocks);
      setOptimistic((prev) => [...prev, agentRow(fakeId, text)]);
      setLive(null);
      return;
    }

    // 真模式：重拉详情对齐落库态。正文、工具卡与横幅都以库里为准（详情带 events，
    // blocks.ts 的 db-wins）；extras 只兜库里没有的那一种（只活在流上的 stream_timeout）。
    const d = await loadDetail(targetSid);
    // 这一趟 await 里人可能换了会话：loadDetail 自己会返 null，但下面的 setOptimistic /
    // setLive(null) 仍会落到**新会话**的状态上（把新会话刚起来的乐观行/live 区清掉）。
    // 换会话那一支已经清过一遍，这里补一次复检即可（S4 收口的 P2-3）。
    if (stale()) return;
    if (d) {
      // 这一下清空只是"别让它一直长"，**不是**画面去重的手段：不出现两遍靠的是渲染处
      // 那次按 id 求差（blocks.ts 的 mergeRows，裁定 R-S3-18）。setDetail 与 setOptimistic
      // 落在两个 await 之后，React 并不保证把它们并进同一次渲染——把"不重复"押在
      // 同批 flush 上，就是押在一个实现细节上。
      setOptimistic([]);
      // id === null = 库里没有比 mid 新的 agent 行：这一轮压根没起来（墙钟比建行来得早）。
      // 那时候这一轮的块无处可挂，setLive(null) 之后屏幕上只剩人自己刚说的那句话；解释与
      // 「重发」的把手由上面那条页面级横幅给（onTimeout），所以不至于让人对着自己的话发呆。
      const id =
        doneId ?? d.messages.filter((m) => m.role === "agent" && m.id > mid).at(-1)?.id ?? null;
      if (id !== null) keepExtras(id, blocks);
    }
    setLive(null);
    void loadSessions(); // 首条消息会把标题改成它的前 20 字，侧栏要跟着变
  }

  /** 建一场新会话，然后让 flush effect 执行排队的那一趟动作。 */
  async function createSessionFor(action: PendingAction) {
    if (creating) return;
    setCreating(true);
    try {
      const created = await apiPost<{ id: number }>("/api/chat/sessions");
      await loadSessions();
      pendingAction.current = action;
      navigate(`/chat/${created.id}`);
    } catch (e) {
      pendingAction.current = null;
      setSending(false);
      pushNotice(errText(e, "新会话没开成"));
    } finally {
      setCreating(false);
    }
  }

  /** 发送（已确定 sid）：把内容 + upload_ids 一起 POST，再起这一轮的流。 */
  async function sendTo(
    targetSid: number,
    text: string,
    uploadIds: number[],
    stagedForSend: StagedUpload[],
  ) {
    const token = epoch.current; // 换会话会把 epoch 推走，这一趟的收尾就自己认输
    setSending(true);
    try {
      const posted: PostedMessage =
        import.meta.env.DEV && mock
          ? await mockPostMessage(mock, text)
          : await postMessage(targetSid, text, uploadIds);
      // 迟到的 POST（S4 收口补）：await 期间人可能换过会话或整页卸载了。成功续体里
      // 清草稿/清暂存/塞乐观行/起流，都只属于发起这一趟时的那个会话。
      if (epoch.current !== token) return;
      // POST 成功了才清空草稿与暂存：409「智能体还在写」或 400 时话和附件都得留着
      setDraft("");
      setStaged((prev) => prev.filter((s) => !uploadIds.includes(s.id)));
      const parseStatuses = new Map(
        posted.attachments?.map((attachment) => [attachment.upload_id, attachment.parse_status]),
      );
      const optimisticAttachments = stagedForSend.map((s) => ({
        id: s.id,
        orig_filename: s.orig_filename,
        size: s.size,
        parse_status: parseStatuses.get(s.id) ?? "none",
        parse_report: null,
      }));
      setOptimistic((prev) => [
        ...prev,
        { ...humanRow(posted.id, text), attachments: optimisticAttachments },
      ]);
      await runTurn(targetSid, posted.id, text);
    } catch (e) {
      if (epoch.current === token) pushNotice(errText(e, "发不出去，检查一下网络再试"));
    } finally {
      if (epoch.current === token) setSending(false);
    }
  }

  // /wiki 的「体检知识库 / 让智能体整理」入口带 ?draft=… 过来：填进输入框后把参数擦掉，
  // 刷新/前进不会又把同一句塞回来（URL 仍是"当前哪一场会话"的唯一来源）。
  useEffect(() => {
    const preset = searchParams.get("draft");
    if (preset === null || preset === "") return;
    setDraft(preset);
    const next = new URLSearchParams(searchParams);
    next.delete("draft");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  async function send() {
    if (archived || sending || creating || uploading) return;
    const text = draft.trim();
    const uploadIds = staged.map((s) => s.id);
    if (!canSendMessage(text, staged)) return;
    if (text.length > BODY_MAX) {
      // 前端先说一句（后端也会 400 兜底）：省一次往返，也别让人以为发出去了
      pushNotice(`这条太长了（最长 ${BODY_MAX} 字），拆成几条再发`);
      return;
    }
    if (sid === null) {
      // 首页直接可用：先建会话；动作排到 flush effect 里再发，避免被换会话 effect 清掉。
      setSending(true);
      void createSessionFor({ kind: "send", text, uploadIds, staged });
      return;
    }
    await sendTo(sid, text, uploadIds, staged);
  }

  /**
   * 选文件：只调 POST /api/uploads 暂存，**不建消息、不跑智能体、不开流**。
   * 不依赖 sid/详情，所以首页无会话时也能先选好再发。
   */
  async function stageFiles(files: File[]) {
    // 受限账号不进上传这条路：附件按钮/拖拽/粘贴在 Composer 那头已经拦了，这是兜底
    // （后端 /api/uploads 也会 403，前端不该先把一件注定失败的事发出去）。
    if (archived || uploading || !canBuild) return;
    setUploading(true);
    try {
      const added: StagedUpload[] = [];
      for (const f of files) {
        const form = new FormData();
        form.set("file", f, f.name);
        try {
          const raw = await apiPostForm<unknown>("/api/uploads", form);
          const got = normalizeStagedUpload(raw);
          if (got === null) throw new Error("上传响应不完整");
          added.push(got);
        } catch (e) {
          // 一件失败不连累其余的：逐条人话（413 超限/格式不支持/断网都走这儿）
          pushNotice(errText(e, `《${f.name}》没传上去`));
        }
      }
      if (added.length > 0) setStaged((prev) => [...prev, ...added]);
    } finally {
      setUploading(false);
    }
  }

  /** 删掉一条暂存：DELETE /api/uploads/{id}；失败就保留 chip 并报人话。 */
  async function removeStaged(id: number) {
    try {
      await apiDelete<{ ok?: boolean }>(`/api/uploads/${id}`);
      setStaged((prev) => withoutStaged(prev, id));
    } catch (e) {
      pushNotice(errText(e, "附件没删掉"));
    }
  }

  if (!me) return <Navigate to="/login" replace />;

  const summary = sessions.find((s) => s.id === sid) ?? null;
  // 归档的会话整块禁用输入区，提示位就是后端那句 400（不是前端自己编的"只读"）
  const archived = detail?.status === "archived" || (import.meta.env.DEV && mock === "archived");
  /**
   * 「重发」跟着会话状态一起灭（裁定 R-S3-19）：归档会话上输入区整块 disabled，预填进去那句
   * 既改不了也发不出去，旁边还写着「这个会话已归档，不能再发言」——一颗点了只会把话塞进灰框里
   * 的按钮比不摆更坏（与 blocks.ts 里 resendTexts"原文是空白就不给键"同一条纪律）。
   *
   * 判据**只**是归档，不抄 Composer 那个三合一的 `sid === null || detail === null || archived`：
   * 后两件是"还没有一场会话可发"，那时消息流没渲染，欢迎块那处 Notices 也不可能带着 resend
   * （换会话那一下 setNotices([]) 把带把手的横幅一并清了），没有半死钮可消；把三件事共用的
   * 那个开关抄过来，哪天两个渲染位分开走，这一句就成了错的。
   */
  const canResend = !archived;
  // 渲染处的对齐（裁定 R-S3-18）：库里的行 + 还没进库的乐观行，按 id 集合的差去重。
  const messages = mergeRows(detail?.messages ?? [], optimistic);
  const title = detail === null ? null : detail.title.trim() === "" ? EMPTY_TITLE : detail.title;
  /**
   * 标题下面那行"谁的会话"。
   *
   * 归属者的名字**只能从会话列表取**（SessionSummary.owner_name）：详情接口是
   * `SELECT * FROM chat_sessions`，没有那次 JOIN users，压根不交 owner_name——所以
   * types.ts 里 SessionDetail 也不再 extends SessionSummary（原来那么写，这个键在类型上
   * 是 string、在运行时恒为 undefined）。列表还没拉到的那一瞬也就没有名字可用；那时候
   * 拿一个代词去顶名字的位置，读起来像是后端真给了一个名叫那个代词的人。拿不到名字
   * 就老实说"别人的会话"，不编占位名字。
   */
  const ownerName = summary?.owner_name.trim() ?? "";
  const ownerLine =
    detail === null
      ? ""
      : detail.user_id === me.id
        ? "我的会话"
        : ownerName === ""
          ? "别人的会话"
          : `${ownerName} 的会话`;

  const head = (
    <div className="head">
      <ShellMenuButton />
      <span className="h1">{title ?? siteName}</span>
      {archived && <span className="pill">归档</span>}
      {import.meta.env.DEV && mock && <span className="pill mock">演示帧 {mock}</span>}
      <span className="meta">
        {ownerLine === ""
          ? "智能体能看到全站会话与材料"
          : `${ownerLine} · 智能体能看到全站会话与材料`}
      </span>
      {sid !== null && detail !== null && detail.user_id === me.id && (
        <div className="menu-wrap head-menu-wrap" ref={headMenuRef}>
          <button
            type="button"
            className="more head-more"
            ref={headMoreRef}
            aria-haspopup="menu"
            aria-expanded={headMenu}
            aria-label="会话操作"
            onClick={() => setHeadMenu((v) => !v)}
          >
            ⋯
          </button>
          {headMenu && (
            <div className="menu" role="menu">
              {archived ? (
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setHeadMenu(false);
                    void unarchive(sid);
                  }}
                >
                  取消归档
                </button>
              ) : (
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setHeadMenu(false);
                    void archive(sid).catch((e: unknown) => pushNotice(errText(e, "归档没成功")));
                  }}
                >
                  归档会话
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );

  return (
    <AppShell
      mainClassName="chat"
      topbar={head}
      sidebar={
        <SessionList
          sessions={sessions}
          currentId={sid}
          me={me}
          creating={creating}
          onNew={() => void newSession()}
          onPick={(id) => navigate(`/chat/${id}`)}
          onRename={rename}
          onArchive={archive}
          onUnarchive={unarchive}
        />
      }
    >
      <div className="main">
        {sid === null || detail === null ? (
          <div className="stream">
            <Notices
              notices={notices}
              onDismiss={dismiss}
              canResend={canResend}
              onResend={prefillResend}
              className="notices top"
            />
            <ChatWelcome onSuggest={suggest} onFeed={feed} onDoctor={doctor} canFeed={canBuild} />
          </div>
        ) : (
          <MessageList
            messages={messages}
            extras={extras}
            live={live}
            streaming={streaming}
            meName={me.name}
            notices={notices}
            onDismiss={dismiss}
            canResend={canResend}
            onResend={prefillResend}
            onSuggest={suggest}
            onFeed={feed}
            onDoctor={doctor}
            canFeed={canBuild}
          />
        )}

        <Composer
          draft={draft}
          onDraft={setDraft}
          onSend={() => void send()}
          onFiles={(files) => void stageFiles(files)}
          disabled={archived}
          disabledHint={archived ? ARCHIVED_TEXT : undefined}
          allowUploads={canBuild}
          sending={sending}
          streaming={streaming}
          uploading={uploading}
          staged={staged}
          onRemoveStaged={(id) => void removeStaged(id)}
          feedSignal={feedSignal}
        />
      </div>
    </AppShell>
  );
}
