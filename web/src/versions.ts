// 版本历史 + 只读 diff（`/api/wiki/{path}/versions`、`/diff?a=&b=`）的形状与纯函数。
//
// 只读：这一页没有编辑/回滚按钮，只做"选两版看改了什么"。diff 的解析只是给行染色：
//   + 行绿、- 行红、@@ / diff / --- / +++ 次墨、其余正文色。
// 首行不是 diff/--- 的输出（空 diff、普通文本、后端换了形状）→ 调用方原样 <pre>，不猜。

import { formatStamp } from "./wiki.ts";

export interface VersionEntry {
  sha: string;
  kind: string;
  action: string;
  slug: string;
  actor: string;
  note: string;
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

/** 版本记录归一化（与日志同一个 LogEntry 形状）：坏条目跳过，时间 parse 不了给 null。 */
export function normalizeVersions(raw: unknown): VersionEntry[] {
  if (!Array.isArray(raw)) return [];
  const out: VersionEntry[] = [];
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

/** 短 sha（7 位）。 */
export function shortSha(sha: string | null | undefined): string {
  const clean = (sha ?? "").trim();
  return clean.length > 7 ? clean.slice(0, 7) : clean;
}

/** 版本行的人话时间（本地时区）。 */
export function versionStamp(version: Pick<VersionEntry, "at">): string {
  const stamp = formatStamp(version.at);
  return stamp === "" ? "未知时间" : stamp;
}

/**
 * 默认对比的两版：列表新到旧，取最近两版，**a 是旧的那版、b 是新的那版**
 * （git diff a b 的出向是 a→b；反过来会把每个改动都染成删/增对调）。
 * 不足两版返回 null（没有可比的东西，页面给一句人话而不是空 diff）。
 */
export function defaultDiffPair(versions: readonly VersionEntry[]): { a: string; b: string } | null {
  if (versions.length < 2) return null;
  return { a: versions[1].sha, b: versions[0].sha };
}

/**
 * 从勾选的两版里定出 diff 的 a/b：**a 永远是较旧的那版**。
 * 列表按新→旧，所以索引大 = 更旧；选反了也能纠正。不足/多出/找不到 → null。
 */
export function pairFromSelected(
  versions: readonly VersionEntry[],
  selected: readonly string[],
): { a: string; b: string } | null {
  if (selected.length !== 2) return null;
  const i0 = versions.findIndex((v) => v.sha === selected[0]);
  const i1 = versions.findIndex((v) => v.sha === selected[1]);
  if (i0 < 0 || i1 < 0 || i0 === i1) return null;
  return i0 > i1 ? { a: selected[0], b: selected[1] } : { a: selected[1], b: selected[0] };
}

/**
 * 勾选一版：最多留两版。取消已选的；再选第三版时丢掉最早选的那个（先进先出），
 * 保证随时是"两版对比"而不是一堆勾选框。
 */
export function pickVersion(selected: readonly string[], sha: string): string[] {
  if (selected.includes(sha)) return selected.filter((s) => s !== sha);
  const next = [...selected, sha];
  while (next.length > 2) next.shift();
  return next;
}

export type DiffLineKind = "add" | "del" | "hunk" | "meta" | "context";

export interface DiffLine {
  text: string;
  kind: DiffLineKind;
}

/**
 * 首行像不像 unified diff。
 * 按任务口径只认 `diff ` 或 `--- `：git 的 diff 一定以 `diff --git` 开头；其它内容
 * （普通文本、被截断只剩 hunk 的残片）宁可按原文展示，也不猜着染色。
 */
export function isUnifiedDiff(text: string): boolean {
  const first = (text ?? "")
    .split(/\r?\n/)
    .find((line) => line.trim() !== "");
  if (first === undefined) return false;
  return /^(diff |--- )/.test(first);
}

/** unified diff → 带行型的数组（只是染色用，不改任何字符）。 */
export function parseDiffLines(text: string): DiffLine[] {
  const src = (text ?? "").replace(/\r\n/g, "\n");
  if (src === "") return [];
  const lines = src.split("\n");
  // split 会给末尾换行留一个空串；diff 常常以换行结尾，这一行没有内容，不渲染
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines.map((line) => {
    if (line.startsWith("@@")) return { text: line, kind: "hunk" };
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) {
      return { text: line, kind: "meta" };
    }
    if (line.startsWith("+")) return { text: line, kind: "add" };
    if (line.startsWith("-")) return { text: line, kind: "del" };
    return { text: line, kind: "context" };
  });
}
