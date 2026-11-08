// 成员管理（`/api/members*`）与站点设置（`/api/admin/settings`）的形状与纯函数：
// 成员表、邀请链接、站点开关、停用/改名/权限规则。
//
// 后端返回的是 users/invites 表行（去掉 password_hash）。这里只做"归一化 + 人话 +
// 链接拼接 + 按钮显隐判据"，写操作留给页面（FormData/JSON 的差别在页面那头更清楚）。

import { formatStamp } from "./wiki.ts";

export interface Member {
  id: number;
  name: string;
  phone: string;
  role: string;
  created_at: string;
  last_login_at: string | null;
  /** 非 null = 已停用（后端删人其实是打这个时间戳）。 */
  deactivated_at: string | null;
  /**
   * 能不能建设 wiki（摄入材料 + 建/改/删页）。管理员恒 true（后端已折算）；
   * 旧数据没有这一列时按 true 兜底——迁移前所有成员本来都能建设。
   */
  can_build_wiki: boolean;
}

export interface Invite {
  id: number;
  name: string;
  token: string;
  /** register（开新号）| reset（给已有账号换密码）。两种都是一次性。 */
  purpose: string;
  /** reset 链接绑定的账号；register 链接为 null。 */
  user_id: number | null;
  used_count: number;
  first_opened_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface MembersData {
  users: Member[];
  invites: Invite[];
}

/** 站点设置：对话可见范围。all = 全员可见（默认），own = 成员只看自己的。 */
export type ChatVisibility = "all" | "own";

export interface SiteSettings {
  chat_visibility: ChatVisibility;
}

function obj(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function strOrNull(v: unknown): string | null {
  return typeof v === "string" && v !== "" ? v : null;
}

function num(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

/**
 * 布尔列归一化：后端给 0/1（SQLite）或 true/false（已经归一过的 JSON）。
 * 缺列/认不出时走 `fallback`——建设权限缺省 true（迁移前人人能建设），
 * 其它判据请显式传 false（"已作废"这类判断不能拿缺省当 true）。
 */
function bool(v: unknown, fallback: boolean): boolean {
  if (typeof v === "boolean") return v;
  if (v === 1 || v === "1") return true;
  if (v === 0 || v === "0") return false;
  return fallback;
}

export function normalizeMembers(raw: unknown): MembersData {
  const r = obj(raw);
  const usersRaw = Array.isArray(r.users) ? (r.users as unknown[]) : [];
  const invitesRaw = Array.isArray(r.invites) ? (r.invites as unknown[]) : [];
  const users: Member[] = [];
  for (const item of usersRaw) {
    const u = obj(item);
    const id = num(u.id);
    if (id <= 0) continue;
    users.push({
      id,
      name: str(u.name) || "（没有名字）",
      phone: str(u.phone),
      role: str(u.role) || "member",
      created_at: str(u.created_at),
      last_login_at: strOrNull(u.last_login_at),
      deactivated_at: strOrNull(u.deactivated_at),
      can_build_wiki: bool(u.can_build_wiki, true),
    });
  }
  const invites: Invite[] = [];
  for (const item of invitesRaw) {
    const inv = obj(item);
    const id = num(inv.id);
    const token = str(inv.token);
    if (id <= 0 || token === "") continue;
    const userId = num(inv.user_id);
    invites.push({
      id,
      name: str(inv.name),
      token,
      purpose: str(inv.purpose),
      user_id: userId > 0 ? userId : null,
      used_count: num(inv.used_count),
      first_opened_at: strOrNull(inv.first_opened_at),
      revoked_at: strOrNull(inv.revoked_at),
      created_at: str(inv.created_at),
    });
  }
  return { users, invites };
}

/**
 * `GET /api/admin/settings` 归一化。只认 all / own，其余（缺字段、旧后端、写坏的值）
 * 一律回退 all——把站点误锁成"每个人只能看自己"比维持旧行为更糟（与后端
 * services/site_settings.py 的读侧口径一致）。
 */
export function normalizeSiteSettings(raw: unknown): SiteSettings {
  const value = str(obj(raw).chat_visibility).trim().toLowerCase();
  return { chat_visibility: value === "own" ? "own" : "all" };
}

/** 邀请/重置链接：origin + "/i/" + token（origin 末尾的 / 去掉，避免双斜杠）。 */
export function inviteUrl(origin: string, token: string): string {
  const base = (origin ?? "").trim().replace(/\/+$/, "");
  const t = (token ?? "").trim();
  return `${base}/i/${t}`;
}

/** 角色人话：admin→管理员，member→成员；认不出的原样。 */
export function roleLabel(role: string | null | undefined): string {
  const r = (role ?? "").trim();
  if (r === "admin") return "管理员";
  if (r === "member") return "成员";
  return r === "" ? "成员" : r;
}

/** 成员状态：停用时间戳非空就是「已停用」。 */
export function memberStatus(member: Pick<Member, "deactivated_at">): {
  label: string;
  off: boolean;
} {
  return member.deactivated_at !== null
    ? { label: "已停用", off: true }
    : { label: "正常", off: false };
}

/**
 * 能不能显示「停用」按钮：只对**别的、还在用的、非管理员**成员显示。
 * 判据与后端 auth.deactivate 一致（管理员与自己都不能停），界面不摆点了必挨骂的按钮。
 */
export function canRemove(
  member: Pick<Member, "id" | "role" | "deactivated_at">,
  meId: number | null,
): boolean {
  if (member.role !== "member") return false;
  if (member.deactivated_at !== null) return false;
  if (meId !== null && member.id === meId) return false;
  return true;
}

/**
 * 邀请用途人话：register → 注册（开新号）；reset → 重置口令（给已有账号换密码）。
 * 认不出的原样显示，不硬翻成"其他"把信息丢掉（与 activity.ts 的 actionLabel 同一条纪律）。
 */
export function invitePurposeLabel(purpose: string | null | undefined): string {
  const p = (purpose ?? "").trim();
  if (p === "") return "邀请";
  if (p === "register") return "注册";
  if (p === "reset") return "重置口令";
  return p;
}

/**
 * 邀请状态人话：已作废 > 已使用（一次性）> 还没用。
 *
 * 2026-10-30 起注册与重置合并成一张表，两种链接都是**一次性**的，所以不再显示
 * "已用 N 次"——N 只可能是 1，那个数字只是把"已经用掉了"说得更绕。
 */
export function inviteState(invite: Pick<Invite, "used_count" | "revoked_at">): string {
  if (invite.revoked_at !== null) return "已作废";
  if (invite.used_count > 0) return "已使用（一次性）";
  return "还没用";
}

/** 邀请列表里"首次点开"那格：没有就「还没点开」。 */
export function openedLabel(invite: Pick<Invite, "first_opened_at">): string {
  const stamp = formatStamp(invite.first_opened_at);
  return stamp === "" ? "还没点开" : stamp;
}

/** 成员加入时间人话（坏时间由 formatStamp 给空串，这里兜底「未知」）。 */
export function joinedLabel(member: Pick<Member, "created_at">): string {
  const stamp = formatStamp(member.created_at);
  return stamp === "" ? "未知" : stamp;
}

/** 改名上限：后端路由不吃长度参数，但姓名栏是人看的，前端卡 60 字与客户端一致。 */
export const MEMBER_NAME_MAX = 60;

/** 姓名输入是否可提交（去空白后非空且不超长）。 */
export function validMemberName(name: string): boolean {
  const clean = (name ?? "").trim();
  return clean !== "" && clean.length <= MEMBER_NAME_MAX;
}
