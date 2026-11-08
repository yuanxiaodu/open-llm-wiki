// 会话面的数据形状：后端 app/services/chat.py 的 list_sessions / get_session 交出来的键，
// 原样镜像（键名保持 snake_case，翻译只在渲染时发生）。
//
// 单独一个模块的理由：SessionList、MessageList、Chat 三处都要用这些形状，塞进任何一个
// 组件都会让另一个组件反过来 import 兄弟组件（Chat → MessageList → Chat 那样的环）。
// 计划 File Structure 里没列这个文件，是落地时补的一层，只放类型、不放逻辑。

/** 上传件在会话详情里的元数据（后端只挑这五列，stored_path/sha256 不给）。 */
export interface Attachment {
  id: number;
  orig_filename: string;
  size: number;
  /** 三态：done=已解析、failed=解析失败、none=未解析。 */
  parse_status: string;
  /**
   * 解析报告。界面**只**读它的两个键，而且只在"多页且真有纯图片页"时才说一句话
   * （components/AttachChip.tsx 的 imageOnlyHint → spec §7 的「含 N 页图片，未入检索」）：
   *   pages_total      这份材料一共几页；
   *   pages_image_only 其中几页只有图片、没有可检索的文字。
   * 其余的键一个都不显示——pages_total 对 docx/txt/md 恒为 1，单独把它摆出来就得给它
   * 起名，而起任何名（尤其「页数」）都是撒谎（计划头部那条契约）。所以判据是
   * "pages_total > 1 且 pages_image_only > 0"：单页的图与有文字的材料都不该挨这句话。
   * 完整的形状留着，是给 S4 的引注面用的。
   */
  parse_report: Record<string, unknown> | null;
}

/**
 * 一次工具往返的结果那半截（tool-result 帧的 ok + summary）。
 *
 * 原来这个类型长在 components/ToolCard.tsx 里，而 blocks.ts 要用它描述"一张卡配上结果
 * 之后长什么样"，于是 blocks.ts 反过来 import 了一个组件文件——纯函数模块依赖组件，
 * 依赖方向是反的（blocks.test.ts 也就跟着把 react 拽了进来）。挪到这儿与 MessageRow /
 * AgentEvent 作伴：这一层只放形状，谁都不 import 谁。
 */
export interface ToolResult {
  ok: boolean;
  summary: string;
}

/** 一条 agent_events 行在会话详情里的形状（裁定 R-S3-8）。 */
export interface AgentEvent {
  /** tool-call / tool-result / system / error（库里的 CHECK 白名单；后端原样透出）。 */
  type: string;
  /**
   * 解码后的 payload（后端已 JSON.parse，与 parse_report 同一条纪律）。
   * null = 库里那一行的 payload_json 解不出来；渲染时按坏帧处理（「服务端帧坏了」）。
   */
  payload: Record<string, unknown> | null;
}

/** 一条消息（会话详情里的 messages 项）。 */
export interface MessageRow {
  id: number;
  session_id: number;
  seq: number;
  role: "human" | "agent";
  user_id: number | null;
  /** 发言人；agent 行恒为 null（渲染成「智能体」）。 */
  user_name: string | null;
  content: string;
  created_at: string;
  /** 1 = 这一轮被截断（max_turns / 失败封口），渲染「中断」pill。 */
  truncated: number;
  /** 非 null 就是这一轮封口了（跑完或失败）；只当记号用，不显示 token 数。 */
  usage_json: string | null;
  attachments: Attachment[];
  /**
   * 这一轮的工具/系统/错误事件（按 seq 升序），刷新之后靠它把工具卡片渲染回来。
   * human 行恒为空数组；纯正文的一轮也是空数组（那时渲染走 extras 那条路，见 blocks.ts）。
   */
  events: AgentEvent[];
}

/** 一场会话在两个接口上都有的那几列（chat_sessions 表的原样镜像）。 */
export interface SessionCore {
  id: number;
  user_id: number;
  title: string;
  status: "active" | "archived" | string;
  started_at: string;
  last_message_at: string | null;
}

/** outcome 里动过的一页：页面路径 + 动作（对话页不再展示，解析保留）。 */
export interface OutcomePage {
  path: string;
  /** create / edit / delete（后端给串；认不出按 edit 显示）。 */
  action: string;
}

/**
 * 一轮收尾时对知识库做了什么（runner 的 outcome 事件）。
 *
 * 对话页从 2026-10-30 起不再展示它（委托人口径：不暴露后台改了几页），但解析保留：
 * 旧后端/历史会话的 outcome 帧不能被当成坏帧；日志页的计数另有数据源
 * （`GET /api/log/outcomes`）。
 */
export interface Outcome {
  pages: OutcomePage[];
  citations: number;
  /** 收尾 lint 的 error 数；null/缺省 = 这一轮没体检。 */
  lint_errors?: number | null;
}

/**
 * 会话最近一次知识库变更的计数。
 *
 * 2026-10-30 之前它还挂在会话摘要上（对话列表条目下面那行"3 页 · 18 引注"小字）；
 * 现在对话面不再暴露后台操作数字，这份形状只剩管理员日志页的「会话成果」在用，
 * 数据来自 `GET /api/log/outcomes`（activity.ts 的 SessionOutcome）。
 */
export interface LastOutcome {
  pages: number;
  citations: number;
  lint_errors?: number | null;
}

/** GET /api/chat/sessions 的一项（全站会话，不按 user_id 过滤）。 */
export interface SessionSummary extends SessionCore {
  /** 归属者的名字。列表接口 JOIN 了 users 才有它（侧栏要按归属者分组）。 */
  owner_name: string;
}

/**
 * GET /api/chat/{id} 的响应。
 *
 * **不是** SessionSummary 的超集，所以不许写 `extends SessionSummary`：详情走的是
 * `SELECT * FROM chat_sessions`（services/chat.get_session），压根没有那次 JOIN users，
 * 响应里**没有 owner_name**。原来那么写，detail.owner_name 在类型上是 string、在运行时
 * 恒为 undefined，谁按类型信它谁就显示一句"undefined 的会话"。归属者的名字一律从
 * 会话列表里那条摘要取（pages/Chat.tsx 的 ownerLine）。
 */
export interface SessionDetail extends SessionCore {
  messages: MessageRow[];
}
