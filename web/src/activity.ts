// 全站改动日志（`GET /api/log`）、会话成果（`GET /api/log/outcomes`）与版本记录
// （`GET /api/wiki/{path}/versions`）共用的形状与纯函数。日志与版本接口返回同一个
// `repo.LogEntry`（{sha,kind,action,slug,actor,note,at}），会话成果则是每场会话的 outcome
// 计数；所以归一化、action 中文、actor 人话、短 sha、成果计数都收在这一层，日志页/版本页
// 各取所需。

import { formatStamp } from "./wiki.ts";
import type { LastOutcome } from "./types.ts";

/** 一条提交记录（后端 app/wiki/repo.py 的 LogEntry，键名原样镜像）。 */
export interface LogEntry {
  sha: string;
  /** wiki | raw …（提交主题里的 kind；前端只用于 Debug，不显示原始词）。 */
  kind: string;
  /** create | edit | revert | ingest。 */
  action: string;
  /** 页面 slug（旧口径）或 path（新 vault 口径），日志页拿它去页面列表里找链接。 */
  slug: string;
  /** 提交者；智能体写的是 agent。 */
  actor: string;
  note: string;
  /** ISO 时间（后端混 +08:00 / +00:00，一律 parse）。 */
  at: string | null;
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

/** `GET /api/log` / `versions` 的数组归一化：缺字段给空串，坏条目跳过。 */
export function normalizeLog(raw: unknown): LogEntry[] {
  if (!Array.isArray(raw)) return [];
  const out: LogEntry[] = [];
  for (const item of raw as unknown[]) {
    const r = obj(item);
    const sha = str(r.sha);
    if (sha === "") continue;
    out.push({
      sha,
      kind: str(r.kind),
      action: str(r.action),
      slug: str(r.slug),
      actor: str(r.actor),
      note: str(r.note),
      at: strOrNull(r.at),
    });
  }
  return out;
}

const ACTION_LABELS: Record<string, string> = {
  create: "新建",
  edit: "编辑",
  revert: "回滚",
  ingest: "摄入",
};

/** action → 人话（认不出的原样显示，不把常用动作硬翻成「其他」丢掉信息）。 */
export function actionLabel(action: string | null | undefined): string {
  const key = (action ?? "").trim();
  if (key === "") return "其他";
  return ACTION_LABELS[key] ?? key;
}

/** actor → 人话：agent→智能体、system→系统（初始化/迁移那类机器提交），其余原样。 */
export function actorLabel(actor: string | null | undefined): string {
  const name = (actor ?? "").trim();
  if (name === "") return "未知";
  const lower = name.toLowerCase();
  if (lower === "agent") return "智能体";
  if (lower === "system") return "系统";
  return name;
}

/** 提交 sha 的短形式（7 位；本来就是短的或空的原样）。 */
export function shortSha(sha: string | null | undefined): string {
  const clean = (sha ?? "").trim();
  return clean.length > 7 ? clean.slice(0, 7) : clean;
}

/** 一条记录属于哪一天（本地时区）：formatStamp 的前 10 个字符（YYYY/MM/DD）。 */
export function logDay(entry: Pick<LogEntry, "at">): string {
  const stamp = formatStamp(entry.at);
  return stamp === "" ? "未知日期" : stamp.slice(0, 10);
}

export interface LogDay {
  /** 分组键/显示用的一天（YYYY/MM/DD；坏时间归「未知日期」）。 */
  day: string;
  items: LogEntry[];
}

/**
 * 按天分组，**保持后端顺序**（新到旧）：同一天的条目聚在一组，组之间按首次出现的
 * 日期先后排。不重新按字符串排序——混合时区时字符串顺序不等于时间顺序。
 */
export function groupLogByDay(entries: readonly LogEntry[]): LogDay[] {
  const groups: LogDay[] = [];
  const index = new Map<string, LogDay>();
  for (const entry of entries) {
    const day = logDay(entry);
    let group = index.get(day);
    if (group === undefined) {
      group = { day, items: [] };
      index.set(day, group);
      groups.push(group);
    }
    group.items.push(entry);
  }
  return groups;
}

/**
 * 一场会话的成果计数（`GET /api/log/outcomes` 的一项）。
 *
 * 原来这份数据挂在对话列表的每条会话上（SessionSummary.last_outcome），对话页/侧栏
 * 各显示一份小字；2026-10-30 口径改了：后台操作数字从对话面整体退场，只留给管理员
 * 日志页。于是形状、归一化与那行小字的格式也跟着搬到这儿来。
 */
export interface SessionOutcome {
  /** 会话 id：日志页拿它链到 `/chat/{id}`。 */
  id: number;
  /** 会话标题（空串由调用方换成人话）。 */
  title: string;
  owner_name: string;
  status: string;
  /** `COALESCE(last_message_at, started_at)`；坏时间给 null。 */
  at: string | null;
  outcome: LastOutcome;
}

/**
 * `GET /api/log/outcomes` 的归一化。
 *
 * 两种外层都认：裸数组，或 `{outcomes: [...]}`（当前后端形状）。坏条目跳过而不是拿空值
 * 顶上——没有数字 id 的行点不到会话，`pages`/`citations` 不是数字的行说不出"改了几页"，
 * 摆一行瞎话比不摆更糟。`lint_errors` 缺省/null 是合法的（这一轮没体检）。
 */
export function normalizeOutcomes(raw: unknown): SessionOutcome[] {
  const wrapped = obj(raw).outcomes;
  const list: unknown[] = Array.isArray(raw)
    ? (raw as unknown[])
    : Array.isArray(wrapped)
      ? (wrapped as unknown[])
      : [];
  const out: SessionOutcome[] = [];
  for (const item of list) {
    const r = obj(item);
    if (typeof r.id !== "number" || !Number.isFinite(r.id)) continue;
    const o = obj(r.outcome);
    if (typeof o.pages !== "number" || !Number.isFinite(o.pages)) continue;
    if (typeof o.citations !== "number" || !Number.isFinite(o.citations)) continue;
    const lint = o.lint_errors;
    out.push({
      id: r.id,
      title: str(r.title),
      owner_name: str(r.owner_name),
      status: str(r.status),
      at: strOrNull(r.at),
      outcome: {
        pages: o.pages,
        citations: o.citations,
        lint_errors: typeof lint === "number" && Number.isFinite(lint) ? lint : null,
      },
    });
  }
  return out;
}

/**
 * 会话成果那行计数（"3 页 · 18 引注"），日志时间线里跟在会话标题后面。
 *
 * 没有可说的计数就返回空串，不拿 0 填：纯问答的会话不该在日志里假装"改了 0 页"。
 */
export function outcomeSummary(last: LastOutcome | null | undefined): string {
  if (!last) return "";
  const parts: string[] = [];
  if (last.pages > 0) parts.push(`${last.pages} 页`);
  if (last.citations > 0) parts.push(`${last.citations} 引注`);
  if ((last.lint_errors ?? 0) > 0) parts.push(`待修 ${last.lint_errors}`);
  return parts.join(" · ");
}

/**
 * 日志时间线的一项：一次提交（git log）或一场会话的成果。
 *
 * 2026-10-30 委托人反馈「会话成果」和提交记录分成两块太割裂，合并成一条按天的
 * 时间线：同一天里谁的会话留下了什么、谁动了哪一页，按时间混排。
 */
export type LogItem =
  | { kind: "commit"; at: string | null; entry: LogEntry }
  | { kind: "outcome"; at: string | null; outcome: SessionOutcome };

/** ISO 时间 → 毫秒；坏时间/缺时间给 -Infinity（排到最后，归「未知日期」）。 */
function stampMs(at: string | null | undefined): number {
  if (!at) return -Infinity;
  const ms = Date.parse(at);
  return Number.isNaN(ms) ? -Infinity : ms;
}

/**
 * 把提交与会话成果合成一条**新到旧**的时间线。
 *
 * 排序键是各自的 at（提交时刻 / 会话最近动静）：

 *   - 时间相等时保持传入顺序（提交在前、成果在后）——JS 的 sort 是稳定排序，
 *     这里再带原始下标比一次，视图不会因为引擎实现不同而抖；
 *   - 没有/坏时间排最后（-Infinity），由 groupLogItemsByDay 归进「未知日期」。
 *
 * 不在这里做去重：同一场会话的成果只出现一次（后端只给最近一条），与提交是两类事实，
 * 各自都有存在的理由。
 */
export function mergeLogItems(
  entries: readonly LogEntry[],
  outcomes: readonly SessionOutcome[],
): LogItem[] {
  const items: LogItem[] = [
    ...entries.map((entry): LogItem => ({ kind: "commit", at: entry.at, entry })),
    ...outcomes.map((outcome): LogItem => ({ kind: "outcome", at: outcome.at, outcome })),
  ];
  return items
    .map((item, index) => ({ item, index, ms: stampMs(item.at) }))
    .sort((a, b) => (b.ms - a.ms) || (a.index - b.index))
    .map(({ item }) => item);
}

export interface LogItemDay {
  day: string;
  items: LogItem[];
}

/**
 * 按本地日期给合并后的时间线分组，**保持传入顺序**（已经是新到旧）。
 *
 * 与 groupLogByDay 同口径（logDay 取本地时区日期、坏时间归「未知日期」），只是元素
 * 从 LogEntry 换成 LogItem；日志页只用这一份，提交/成果各分一次组会让同一天出现两个标题。
 */
export function groupLogItemsByDay(items: readonly LogItem[]): LogItemDay[] {
  const groups: LogItemDay[] = [];
  const index = new Map<string, LogItemDay>();
  for (const item of items) {
    const day = logDay({ at: item.at });
    let group = index.get(day);
    if (group === undefined) {
      group = { day, items: [] };
      index.set(day, group);
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
}
