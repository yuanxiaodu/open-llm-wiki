// 全站搜索（`GET /api/search?q=`）的形状与纯函数。
//
// 后端把三样语料（会话人话 / 上传材料 / wiki 页）分开返回三组：
//   {sessions:[{ref_id,heading,snippet,session_id}], uploads:[...], pages:[...]}
// sessions 组多一个 session_id（ref_id 是消息 id，不能直接当会话 id 跳）。
//
// 两件容易做坏的事收在这里：
//   1. **snippet 高亮必须用 React 节点切**：后端 FTS 分支把命中词用【】包住，前端解析成
//      [{text,mark}] 再渲染 <mark>；不允许 innerHTML（LIKE 分支没有标记就原样文本）。
//   2. 组/字段缺失、ref_id 不是整数时全部降级成空数组/纯文本，不让一条坏命中把整页拖炸。

export interface SearchHit {
  ref_id: string;
  heading: string;
  snippet: string;
  /** 仅 sessions 组：这条消息所在会话；null = 消息已不在库里，别渲染链接。 */
  session_id: number | null;
}

export interface SearchResult {
  sessions: SearchHit[];
  uploads: SearchHit[];
  pages: SearchHit[];
}

function obj(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  return "";
}

function normalizeHit(raw: unknown): SearchHit | null {
  const r = obj(raw);
  const refId = str(r.ref_id);
  if (refId === "") return null;
  const sessionId = typeof r.session_id === "number" ? r.session_id : null;
  return {
    ref_id: refId,
    heading: str(r.heading),
    snippet: str(r.snippet),
    session_id: sessionId,
  };
}

function normalizeGroup(raw: unknown): SearchHit[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[])
    .map(normalizeHit)
    .filter((hit): hit is SearchHit => hit !== null);
}

/** `GET /api/search` 归一化：三组缺哪组都给空数组。 */
export function normalizeSearch(raw: unknown): SearchResult {
  const r = obj(raw);
  return {
    sessions: normalizeGroup(r.sessions),
    uploads: normalizeGroup(r.uploads),
    pages: normalizeGroup(r.pages),
  };
}

/** 一次搜索有没有任何结果。 */
export function totalHits(result: SearchResult): number {
  return result.sessions.length + result.uploads.length + result.pages.length;
}

export interface SnippetPart {
  text: string;
  /** true = 这个片段是后端标出的命中词，渲染成 <mark>。 */
  mark: boolean;
}

/**
 * 把后端 snippet 里的 `【命中词】` 切成普通/高亮片段。
 *
 * 规则：
 *   - `【…】` 成对才高亮；落单的 `【` 或 `】` 当普通文字（后端换了一种标记时也别吞字）；
 *   - 空片段不产出；完全没有标记就整段一个 mark=false；
 *   - 只处理这两个字符，不解释任何别的 markdown/HTML（下游是 React 文本节点）。
 */
export function splitSnippet(snippet: string): SnippetPart[] {
  const text = snippet ?? "";
  if (text === "") return [];
  const parts: SnippetPart[] = [];
  let buffer = "";
  const flush = () => {
    if (buffer !== "") {
      parts.push({ text: buffer, mark: false });
      buffer = "";
    }
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "【") {
      const end = text.indexOf("】", i + 1);
      if (end === -1) {
        buffer += ch;
        i += 1;
        continue;
      }
      flush();
      const marked = text.slice(i + 1, end);
      if (marked !== "") parts.push({ text: marked, mark: true });
      i = end + 1;
      continue;
    }
    buffer += ch;
    i += 1;
  }
  flush();
  return parts;
}

/** sessions 组一跳：有 session_id 才给 /chat/{id}，否则 null（纯文本，不摆死链）。 */
export function searchSessionHref(hit: SearchHit): string | null {
  return hit.session_id !== null ? `/chat/${hit.session_id}` : null;
}

/** pages 组：ref_id 是页面 path（带 .md），转成 /wiki/... 的 URL 形态。 */
export function searchPageHref(hit: SearchHit): string | null {
  const id = hit.ref_id.trim();
  if (id === "") return null;
  return `/wiki/${id.replace(/\.md$/i, "").split("/").map(encodeURIComponent).join("/")}`;
}
