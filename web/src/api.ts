// API 客户端：全站唯一的取数入口。三条硬约定（裁定 R11 + S3 计划）：
//
//   1. 请求体一律 JSON（登录/注册/建会话…），错误一律 {"error": 人话}。
//      非 2xx 时这里把 error 解出来抛 ApiError，页面只管把 message 摆到横幅上，
//      不做任何"翻译"——后端给的就已经是人话。解法唯一出处是 readErrorEnvelope：
//      开流那条路（pages/Chat.tsx 自己发的 fetch，不走这个客户端）也调它。
//   2. 401 = 会话没了（cookie 过期/被登出）。清掉 sessionStorage 里的身份，
//      整页跳 /login。两个地方例外：/login 自己（登录失败的 401 不该循环跳转，
//      虽然当前后端登录失败给的是 400）和 /i/:token 邀请注册页（注册者本来
//      就没有身份，靠链接说话）。判据在 isAuthPath，尾斜杠归一（/login/ 也算）。
//   3. extra 承载 {"error"} 之外的附加键（校验失败的 diff/failed、文档的 slug
//      等）。后端今天在这些接口上只回 error，但 S4 的保存冲突会带 diff，
//      形状先钉在这儿。
//
// 身份存 sessionStorage（不是 localStorage）：关标签页即退，和 cookie session
// 的生命周期观感一致。键名 "identity"，值 {id,name,role,can_build_wiki}。

/**
 * sessionStorage 里的身份条目。
 *
 * `role` 只用来决定界面措辞，不做权限判断（权限在后端）；`can_build_wiki` 同理——它决定
 * 界面上摆不摆"建设"入口（摄入材料 / 让智能体整理 / 附件按钮），真正的 403 仍由后端给。
 * 管理员恒有建设权限，后端在 identity payload 里已经折算成 true（见 app/services/access.py）。
 */
export interface Identity {
  id: number;
  name: string;
  role: string;
  /** 能不能摄入材料 / 让智能体改 wiki；只有使用权限的成员为 false。 */
  can_build_wiki: boolean;
}

const IDENTITY_KEY = "identity";

/**
 * sessionStorage 里的身份 JSON → Identity；坏形状给 null。
 *
 * 旧版本存的是 {id,name,role}，**没有** can_build_wiki——那种老数据一律按 true 处理
 * （迁移前所有成员本来都能建设，按 false 兜底会把所有人瞬间锁成只读）。
 * 有这一项但被写成 0/1 也认（手写 sessionStorage / 旧序列化都可能这样）。
 */
export function normalizeIdentity(raw: unknown): Identity | null {
  if (typeof raw !== "object" || raw === null) return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.id !== "number" || typeof v.name !== "string" || typeof v.role !== "string") {
    return null;
  }
  const flag = v.can_build_wiki;
  return {
    id: v.id,
    name: v.name,
    role: v.role,
    can_build_wiki: typeof flag === "boolean" ? flag : flag === 0 ? false : true,
  };
}

/**
 * 当前账号能不能建设 wiki：管理员恒 true，其余看 can_build_wiki（缺省按 true，
 * 与 normalizeIdentity 的老数据口径一致）。身份为空时 false。
 */
export function canBuildWiki(me: Identity | null): boolean {
  if (me === null) return false;
  if (me.role === "admin") return true;
  return me.can_build_wiki !== false;
}

/**
 * 身份快照缓存 + 订阅。
 *
 * 为什么要有这一层：AuthOnly 每次整页加载都用 `/api/me` 刷新身份（管理员可能刚改了
 * 某人的建设权限），而页面里的 Chat / AppShell / WikiIndex 又直接读 getIdentity()。
 * 只把新身份写回 sessionStorage 的话，它们不会重渲染——权限改了、界面还停在旧状态。
 * 所以读侧给一份稳定快照（useSyncExternalStore 要求 getSnapshot 返回同一个引用，
 * 每次都 JSON.parse 出新对象会无限重渲染），写侧通知订阅者（web/src/useIdentity.ts）。
 */
let cachedIdentity: Identity | null | undefined;
const identityListeners = new Set<() => void>();

function notifyIdentity(): void {
  for (const listener of identityListeners) listener();
}

/** 订阅身份变化；返回取消订阅（给 useSyncExternalStore 用）。 */
export function subscribeIdentity(listener: () => void): () => void {
  identityListeners.add(listener);
  return () => {
    identityListeners.delete(listener);
  };
}

export function getIdentity(): Identity | null {
  if (cachedIdentity !== undefined) return cachedIdentity;
  try {
    const raw = sessionStorage.getItem(IDENTITY_KEY);
    cachedIdentity = raw ? normalizeIdentity(JSON.parse(raw)) : null;
  } catch {
    cachedIdentity = null; // 存进去的不是 JSON 就当没有，别把页面炸了
  }
  return cachedIdentity;
}

export function setIdentity(id: Identity): void {
  sessionStorage.setItem(IDENTITY_KEY, JSON.stringify(id));
  cachedIdentity = id;
  notifyIdentity();
}

export function clearIdentity(): void {
  sessionStorage.removeItem(IDENTITY_KEY);
  cachedIdentity = null;
  notifyIdentity();
}

/**
 * 非 2xx 一律抛这个。message 是后端 {"error"} 里的人话（解析失败才用兜底文案），
 * status 是 HTTP 状态码，extra 是响应里除 error 之外的其余键（diff/failed/slug…）。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly extra: Record<string, unknown>;

  constructor(message: string, status: number, extra: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.extra = extra;
  }
}

/**
 * 后端没给人话时的兜底文案（裁定 R-S3-6）。**不带状态码**：一屏「请求失败（502）」
 * 对填表的人什么也没说，只把后台的形状甩到他脸上。三档就够：
 *
 *   0（fetch 抛了 = 断网/代理挂了/跨源被拦）→「连不上服务器」
 *   5xx（服务器自己倒了，网关也一样）        →「服务器出错了」
 *   其余（响应体不是 JSON 的 4xx 等）        →「请求没成功」
 *
 * 只在**解不出** {"error"} 时用；后端给了人话就一律用后端那句（本文件第 1 条约定）。
 */
export function fallbackMessage(status: number): string {
  if (status === 0) return "连不上服务器";
  if (status >= 500) return "服务器出错了";
  return "请求没成功";
}

/**
 * 401 时哪些路径不跳转：登录页自己，和邀请注册页（它们本来就不要求身份）。
 *
 * 尾斜杠先归一（裁定 R-S3-6）：`/login/` 与 `/login` 是同一个页面——router 的
 * `/login` 不认尾斜杠时会落进 catch-all 再被替回 `/login`，人已经站在登录页上了，
 * 这时候还整页 assign 一次只是把页面刷两遍。`/i/abc/` 同理。
 *
 * 只剥尾斜杠、不做别的归一：`/i`（没有 token）压根不是路由，落进 catch-all 会被送去
 * /chat 再由守卫送回 /login，那种情况下"跳转"正是对的行为，不该豁免。
 */
export function isAuthPath(pathname: string): boolean {
  const p = pathname.replace(/\/+$/, "");
  return p === "/login" || p.startsWith("/i/");
}

function onAuthPage(): boolean {
  return isAuthPath(window.location.pathname);
}

function handle401(): void {
  clearIdentity();
  if (!onAuthPage()) {
    // 整页跳转而不是 router navigate：api.ts 不 import react-router，
    // 跳转后 React 树整个重建，守卫状态不会残留。
    window.location.assign("/login");
  }
}

/**
 * 非 2xx 的响应体 → {"error": 人话} + 其余键。全站**唯一**的一处解法。
 *
 * 原来这段逻辑在 request() 里写着，pages/Chat.tsx 开流失败那条路（fetch 是它自己发的，
 * 不走 request）又抄了一份——两份迟早漂，而漂的样子是"同一个后端错误，走 API 客户端时
 * 说的是人话、走流时说的是兜底文案"。所以拎出来共用，两边都只剩一行调用。
 *
 * 解不出就退回 fallbackMessage(res.status)：响应体不是 JSON（网关直接回了 HTML）、
 * error 不是非空串、压根没有这个键，三种都算"后端没给人话"。绝不把状态码拼进文案
 * （fallbackMessage 的 docstring）。**不抛**：读一个错误响应体失败本身不是错误。
 */
export async function readErrorEnvelope(res: Response): Promise<{
  message: string;
  extra: Record<string, unknown>;
}> {
  let message = fallbackMessage(res.status);
  let extra: Record<string, unknown> = {};
  try {
    const data: unknown = await res.json();
    if (typeof data === "object" && data !== null) {
      const { error, ...rest } = data as Record<string, unknown>;
      if (typeof error === "string" && error) message = error;
      extra = rest;
    }
  } catch {
    // 响应体不是 JSON（比如网关直接回了 HTML），用上面的兜底文案
  }
  return { message, extra };
}

/**
 * 是 ApiError 就说后端那句人话，否则用调用方给的兜底文案。
 *
 * 全站 catch 里"这句话怎么摆"就这一处判据：对话页的取数、上传，与开流那条不走本客户端
 * 的路（stream.ts）都用它。原来各写一份 `e instanceof ApiError ? e.message : …`，
 * 三份迟早漂成"同一个错误两种说法"。
 */
export function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

interface RequestSpec {
  method: string;
  body?: BodyInit;
  /** true 时手动写 application/json；form-data 必须留空让浏览器自己带 boundary。 */
  json?: boolean;
}

async function request<T>(path: string, spec: RequestSpec): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: spec.method,
      headers: spec.json ? { "Content-Type": "application/json" } : undefined,
      body: spec.body,
    });
  } catch {
    // 网络层错误（断网/代理挂了）。不装成后端的话，直接给一句人话。
    throw new ApiError(fallbackMessage(0), 0);
  }

  if (res.status === 401) handle401();

  if (!res.ok) {
    // 信封的解法只有一处（readErrorEnvelope）：兜底文案不带状态码，解出了 {"error"} 就用它。
    const { message, extra } = await readErrorEnvelope(res);
    throw new ApiError(message, res.status, extra);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    json: body !== undefined,
  });
}

export function apiDelete<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

export function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: body === undefined ? undefined : JSON.stringify(body),
    json: body !== undefined,
  });
}

/**
 * multipart 上传（S3-T5 的附件用）。FormData 交给 fetch 自己序列化，
 * **不要**手动设 Content-Type——浏览器要带 boundary，手设反而解不出文件。
 */
export function apiPostForm<T>(path: string, form: FormData): Promise<T> {
  return request<T>(path, { method: "POST", body: form });
}
