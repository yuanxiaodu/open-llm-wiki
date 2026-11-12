// LLM-Wiki（Hermes 物理 vault）的数据形状与纯函数。
//
// 这一层只放**不需要 DOM、不需要 React** 的东西：接口形状归一化、物理路径 ↔ URL、
// wikilink 解析替换、物理目录树、标题锚点、引注角标、时间/来源人话。渲染组件从这里
// 取结果，自己不做判断——同一件事不会在两个组件里各写一份（wiki.test.ts 钉的就是它）。
//
// 后端契约（S5 计划 §2；同事并行落地，字段/形状缺失时在这里归一化降级）：
//   GET /api/wiki        → {pages:[{path,slug,title,page_type,tags,summary,created_at,
//                            updated_at,actor}], count}（按 updated_at 倒序）
//   GET /api/wiki/index  → {path:"index.md", body_md, updated_at}
//   GET /api/wiki/{path} → {path,slug,title,page_type,body_md,head,tags,sources,
//                            created_at,updated_at,citations[],
//                            wikilinks:[{target,label,path|null}],
//                            backlinks:[{path,title}]}
// 页面 path 是相对 vault 的路径（`entities/foo.md`），URL 里的 path 不带 `.md`。
//
// 旧 S4b 的平铺形状（数组 + slug/topic）也认：normalizeWikiList 会把它折成新形状，
// 这样后端切换期间前端不会白屏（旧 outline/topic 映射到 entity 目录；书已独立，丢掉）。

/** 索引不是一页 wiki，是 `/wiki` 这个路由（树里的 path 用空串表示）。 */
export const WIKI_INDEX_PATH = "";

// ---------- 归一化后的接口形状 ----------

export interface WikiPageMeta {
  /** 相对 vault 的物理路径，带 `.md`（canonical）。 */
  path: string;
  slug: string;
  title: string;
  page_type: string;
  tags: string[];
  summary: string;
  created_at: string | null;
  updated_at: string | null;
  actor: string | null;
}

export interface WikiList {
  pages: WikiPageMeta[];
  count: number;
}

/** 后端解析出的 wikilink：target 是正文里 `[[...]]` 的原文，path 为 null 即断链。 */
export interface WikiLink {
  target: string;
  label: string;
  path: string | null;
}

export interface WikiBacklink {
  path: string;
  title: string;
}

export interface WikiPageDetail {
  contested?: boolean;
  contradictions?: string[];
  metadata?: Record<string, string>;
  path: string;
  slug: string;
  title: string;
  page_type: string;
  /** 证据强度 high/medium/low；空串 = 这一版没填（老数据/异常值都归到这里）。 */
  confidence: string;
  body_md: string;
  head: string | null;
  tags: string[];
  sources: string[];
  created_at: string | null;
  updated_at: string | null;
  wikilinks: WikiLink[];
  backlinks: WikiBacklink[];
}

/** `GET /api/wiki/index`。 */
export interface WikiIndexDoc {
  path: string;
  body_md: string;
  updated_at: string | null;
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

/** tags 可能是数组（新）、逗号串/空白串（旧/手写），统一成去空去重的 string[]。 */
export function normalizeTags(raw: unknown): string[] {
  const list = Array.isArray(raw)
    ? raw.map((t) => String(t))
    : typeof raw === "string"
      ? raw.split(/[,，\s]+/)
      : [];
  const out: string[] = [];
  for (const t of list) {
    const tag = t.trim();
    if (tag !== "" && !out.includes(tag)) out.push(tag);
  }
  return out;
}

function normalizeSources(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((s) => String(s).trim()).filter((s) => s !== "");
}

/** 从 path / slug 里拿一个显示用的名字（去掉目录与 `.md`）。 */
export function pathStem(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const last = clean.split("/").pop() ?? "";
  return last.replace(/\.md$/i, "");
}

/** 物理路径 → URL path（去掉 `.md`，逐段编码，保留 `/`）。空路径就是索引。 */
export function wikiPageHref(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\.md$/i, "").replace(/^\/+|\/+$/g, "");
  if (clean === "") return "/wiki";
  return `/wiki/${clean.split("/").map(encodeURIComponent).join("/")}`;
}

/** 相对 vault 的 path → `/api/wiki/...`（逐段编码，保留 `/`；空 = index 端点）。 */
export function wikiApiUrl(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  if (clean === "") return "/api/wiki/index";
  return `/api/wiki/${clean.split("/").map(encodeURIComponent).join("/")}`;
}

/** 地址栏 pathname → vault 相对 path（`/wiki` = 索引的空串）。页面路由工具。 */
export function wikiUrlPathFromPathname(pathname: string): string {
  const clean = (pathname ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (clean === "" || clean === "/wiki") return WIKI_INDEX_PATH;
  if (!clean.startsWith("/wiki/")) return WIKI_INDEX_PATH;
  return normalizeWikiUrlPath(clean.slice("/wiki/".length));
}

/** 这个 pathname 是不是 wiki 的索引页（/wiki 或 /wiki/）。 */
export function isWikiIndexPath(pathname: string): boolean {
  return (pathname ?? "").replace(/\/+$/, "") === "/wiki";
}

/** URL path（已去掉 `/wiki/` 前缀）→ 相对 path。保留 `.md` 与否都能显示。 */
export function normalizeWikiUrlPath(raw: string): string {
  let clean = (raw ?? "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  try {
    clean = clean
      .split("/")
      .map((seg) => decodeURIComponent(seg))
      .join("/");
  } catch {
    // 坏编码就按原样（后端会 404/400，界面报人话）
  }
  return clean;
}

/** 旧 S4b 的 page_type → 物理目录（迁移期兜底；新类型直接按 type 复数走目录）。 */
const LEGACY_DIR: Record<string, string> = {
  topic: "entities",
  entity: "entities",
  concept: "concepts",
  comparison: "comparisons",
  query: "queries",
};

/** page_type → 人话标签（entity→实体…）。旧 topic 保留旧名，未知说「其他」。 */
export const TYPE_LABELS: Record<string, string> = {
  entity: "实体",
  concept: "概念",
  comparison: "对比",
  query: "查询",
  topic: "主题",
  other: "其他",
};

export function pageTypeLabel(
  type: string | null | undefined,
  labels: Record<string, string> = TYPE_LABELS,
): string {
  const key = (type ?? "").trim();
  return labels[key] ?? TYPE_LABELS[key] ?? "其他";
}

/** frontmatter 的 confidence 只有三档；大小写/空白归一，别的一律当没填。 */
export function normalizeConfidence(value: unknown): string {
  const level = str(value).trim().toLowerCase();
  return level === "high" || level === "medium" || level === "low" ? level : "";
}

const CONFIDENCE_LABELS: Record<string, string> = {
  high: "可信度：高",
  medium: "可信度：中",
  low: "可信度：低",
};

/** 页面元信息栏里的中文标签；不认识的等级返回空串（调用方据此不渲染）。 */
export function confidenceLabel(level: string): string {
  return CONFIDENCE_LABELS[level] ?? "";
}

const CONFIDENCE_HINTS: Record<string, string> = {
  high: "多份独立来源经内容交叉核对，仍需结合适用条件判断",
  medium: "有来源支持，尚未确认独立来源间的相互印证",
  low: "来源薄弱，或没有来源材料",
};

/** 悬停说明：让读者一眼知道"高/中/低"是按什么判的。 */
export function confidenceHint(level: string): string {
  return CONFIDENCE_HINTS[level] ?? "";
}

/** 物理目录名 → 对应的 page_type；左树标签从 type_labels 派生。 */
const DIR_TO_TYPE: Record<string, string> = {
  entities: "entity",
  concepts: "concept",
  comparisons: "comparison",
  queries: "query",
};

/** 物理目录名 → 左树里的中文标签；不认识的目录用原名。 */
export function dirLabel(
  name: string,
  labels: Record<string, string> = TYPE_LABELS,
): string {
  const type = DIR_TO_TYPE[name];
  return type === undefined ? name : pageTypeLabel(type, labels);
}

/** 这些是 vault 的元文件/素材目录，不进左树（页面列表本身也不该有它们，防御一下）。 */
export function isSpecialWikiPath(path: string): boolean {
  const clean = path.replace(/^\/+/, "");
  const segments = clean.split("/");
  if (segments[0] === "raw") return true;
  // 根目录的元文件（index.md / SCHEMA.md / log.md）不是页面
  if (segments.length === 1 && /^(index|SCHEMA|log)\.md$/i.test(clean)) return true;
  return false;
}

function normalizePageMeta(raw: unknown): WikiPageMeta | null {
  const r = obj(raw);
  let path = str(r.path).replace(/\\/g, "/").replace(/^\/+/, "");
  let slug = str(r.slug);
  let pageType = str(r.page_type);
  // 书已经独立（S5）：旧 vault 的大纲页不进知识库
  if (pageType === "outline" || slug === "_outline_") return null;
  const title = str(r.title);
  // 旧形状：只有 slug/page_type，没有 path —— 按旧 type 推到物理目录
  if (path === "" && slug !== "") path = `${LEGACY_DIR[pageType] ?? ""}/${slug}.md`.replace(/^\/+/, "");
  if (path === "") return null;
  if (slug === "") slug = pathStem(path);
  if (pageType === "") pageType = "entity";
  if (isSpecialWikiPath(path)) return null;
  return {
    path,
    slug,
    title: title.trim() === "" ? pathStem(path) : title,
    page_type: pageType,
    tags: normalizeTags(r.tags),
    summary: str(r.summary).trim(),
    created_at: strOrNull(r.created_at),
    updated_at: strOrNull(r.updated_at),
    actor: strOrNull(r.actor),
  };
}

/**
 * `GET /api/wiki` 的形状归一化：
 *   - 新形状 `{pages:[...], count}` → 直接用（count 缺失按数组长度）；
 *   - 旧形状 `[...]`（S4b 平铺数组）→ 映射成新形状（旧 outline 页丢掉：书已独立）；
 *   - 其他乱七八糟 → 空列表。
 * 同时过滤 raw/元文件。**不改入参**。
 */
export function normalizeWikiList(raw: unknown): WikiList {
  const source: unknown[] = Array.isArray(raw)
    ? raw
    : Array.isArray(obj(raw).pages)
      ? (obj(raw).pages as unknown[])
      : [];
  const pages: WikiPageMeta[] = [];
  source.forEach((item) => {
    const meta = normalizePageMeta(item);
    if (meta !== null) pages.push(meta);
  });
  const countRaw = obj(raw).count;
  const count = typeof countRaw === "number" && Number.isFinite(countRaw) ? countRaw : pages.length;
  return { pages, count };
}

/** 页面详情归一化：缺 wikilinks/backlinks/tags/sources 时给空数组。 */
export function normalizeWikiDetail(raw: unknown): WikiPageDetail {
  const r = obj(raw);
  const path = str(r.path).replace(/\\/g, "/").replace(/^\/+/, "");
  const slug = str(r.slug) || pathStem(path);
  const wikilinks: WikiLink[] = Array.isArray(r.wikilinks)
    ? (r.wikilinks as unknown[]).map((item) => {
        const w = obj(item);
        return {
          target: str(w.target),
          label: str(w.label) || str(w.target),
          path: strOrNull(w.path),
        };
      })
    : [];
  const backlinks: WikiBacklink[] = Array.isArray(r.backlinks)
    ? (r.backlinks as unknown[]).map((item) => {
        const b = obj(item);
        const p = str(b.path);
        return { path: p, title: str(b.title) || pathStem(p) };
      })
    : [];
  return {
    path,
    slug,
    title: str(r.title).trim() === "" ? pathStem(path) : str(r.title),
    page_type: str(r.page_type) || "entity",
    confidence: normalizeConfidence(r.confidence),
    contested: r.contested === true,
    contradictions: normalizeTags(r.contradictions),
    metadata: obj(r.metadata) as Record<string, string>,
    body_md: str(r.body_md),
    head: strOrNull(r.head),
    tags: normalizeTags(r.tags),
    sources: normalizeSources(r.sources),
    created_at: strOrNull(r.created_at),
    updated_at: strOrNull(r.updated_at),
    wikilinks,
    backlinks,
  };
}

/** `GET /api/wiki/index` 归一化。 */
export function normalizeWikiIndex(raw: unknown): WikiIndexDoc {
  const r = obj(raw);
  return {
    path: str(r.path) || "index.md",
    body_md: str(r.body_md),
    updated_at: strOrNull(r.updated_at),
  };
}

// ---------- 物理目录树 ----------

/**
 * 树节点。**纯数据**：长什么样由 buildWikiTree 决定，怎么折叠/怎么画由
 * components/WikiTree.tsx 决定。`href` 让节点自己带路由地址。
 */
export interface WikiTreeNode {
  kind: "folder" | "file";
  label: string;
  /** 文件叶子的站内地址（索引 = /wiki，页面 = /wiki/entities/foo）。 */
  href?: string;
  /** 文件的物理/逻辑路径（索引为空串），用于和当前 URL 比 aria-current。 */
  path?: string;
  /** 文件的 page_type，或文件夹的分组键。 */
  type?: string;
  /** 文件夹里挂了几页。 */
  count?: number;
  /** 在这一项前面画一条分隔线。 */
  dividerBefore?: boolean;
  children?: WikiTreeNode[];
}

interface DirBucket {
  dirs: Map<string, DirBucket>;
  pages: WikiPageMeta[];
}

const ROOT_DIR_ORDER = ["entities", "concepts", "comparisons", "queries"];

function emptyBucket(): DirBucket {
  return { dirs: new Map(), pages: [] };
}

function bucketOf(root: DirBucket, segments: string[]): DirBucket {
  let bucket = root;
  for (const seg of segments) {
    let child = bucket.dirs.get(seg);
    if (child === undefined) {
      child = emptyBucket();
      bucket.dirs.set(seg, child);
    }
    bucket = child;
  }
  return bucket;
}

function countLeaves(bucket: DirBucket): number {
  let n = bucket.pages.length;
  for (const child of bucket.dirs.values()) n += countLeaves(child);
  return n;
}

function pageFile(page: WikiPageMeta): WikiTreeNode {
  return {
    kind: "file",
    label: page.title.trim() === "" ? pathStem(page.path) : page.title,
    href: wikiPageHref(page.path),
    path: page.path,
    type: page.page_type,
  };
}

function dirNodes(
  bucket: DirBucket,
  prefix: string,
  top: boolean,
  labels: Record<string, string>,
): WikiTreeNode[] {
  const names = [...bucket.dirs.keys()];
  names.sort((a, b) => {
    if (top) {
      const ai = ROOT_DIR_ORDER.indexOf(a);
      const bi = ROOT_DIR_ORDER.indexOf(b);
      if (ai !== -1 || bi !== -1) {
        // 已知的五类按固定顺序，未知目录排在后面
        return (ai === -1 ? ROOT_DIR_ORDER.length : ai) - (bi === -1 ? ROOT_DIR_ORDER.length : bi);
      }
    }
    return a.localeCompare(b, "zh-Hans-CN");
  });
  const folders = names.map((name) => {
    const child = bucket.dirs.get(name)!;
    return {
      kind: "folder" as const,
      label: dirLabel(name, labels),
      type: name,
      count: countLeaves(child),
      children: dirNodes(child, `${prefix}${name}/`, false, labels),
    };
  });
  // 文件沿用后端给的顺序（按 updated_at 倒序 = 最近改的在上面）
  return [...folders, ...bucket.pages.map(pageFile)];
}

/**
 * 页面列表 → 物理目录树：
 *
 *   索引（/wiki）
 *   实体（entities/…）
 *   概念（concepts/…）
 *   对比（comparisons/…）
 *   查询（queries/…）
 *   摘要（summaries/…）
 *
 * 目录按 path 的真实层级递归（`entities/人物/foo.md` 会多一层，将来加结构不用改组件）；
 * 已知五类固定顺序，未知目录按名字排在后面；空目录不渲染。**不改入参**。
 */
export function buildWikiTree(
  pages: readonly WikiPageMeta[],
  labels: Record<string, string> = TYPE_LABELS,
): WikiTreeNode[] {
  const root = emptyBucket();
  for (const page of pages) {
    const path = page.path.replace(/\\/g, "/").replace(/^\/+/, "");
    if (path === "" || isSpecialWikiPath(path)) continue;
    const segments = path.split("/");
    segments.pop(); // 最后一段是文件名
    bucketOf(root, segments).pages.push(page);
  }
  return [
    { kind: "file", label: "索引", href: "/wiki", path: WIKI_INDEX_PATH, type: "index" },
    ...dirNodes(root, "", true, labels),
  ];
}

/** 从页面列表造一份「slug/title → path」的 wikilink 映射（index.md 没有后端解析时用）。 */
export function wikilinksFromPages(pages: readonly WikiPageMeta[]): WikiLink[] {
  const links: WikiLink[] = [];
  for (const page of pages) {
    links.push({ target: page.slug, label: page.title, path: page.path });
    if (page.title !== "" && page.title !== page.slug) {
      links.push({ target: page.title, label: page.title, path: page.path });
    }
  }
  return links;
}

/** 页面列表里某一页（大小写/`.md` 不敏感地比 path）。 */
export function findWikiPage(
  pages: readonly WikiPageMeta[],
  urlPath: string,
): WikiPageMeta | null {
  const want = normalizeWikiUrlPath(urlPath).replace(/\.md$/i, "").toLowerCase();
  if (want === "") return null;
  for (const page of pages) {
    const p = page.path.replace(/\.md$/i, "").toLowerCase();
    if (p === want) return page;
  }
  return null;
}

// ---------- wikilink / 角标替换 ----------

/** markdown 行内文字的安全化转义（wikilink 的显示文字可能含标点）。 */
function escapeMdText(text: string): string {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/([\[\]*_`<>])/g, "\\$1")
    .replace(/\r?\n/g, " ")
    .trim();
}

/** `[[target]]` / `[[target|显示文字]]`。`cite:` 开头的**不**算 wikilink（留给角标）。 */
const WIKILINK_RE = /\[\[([^[\]|]+?)(?:\|([^[\]]+?))?\]\]/g;

/**
 * 正文里的 `[[wikilink]]` → markdown 链接（path 已知）或纯文字（断链/未解析）。
 *
 * 后端在页详情里给 `wikilinks` 映射；索引页拿不到映射时可以用
 * `wikilinksFromPages(pages)` 现造一份。**不碰** `[[cite:...]]`（角标先/后单独处理）。
 * 仍然走 streamdown 渲染，不拼 HTML、不 dangerouslySetInnerHTML。
 */
export function replaceWikiLinks(
  body: string,
  links: readonly WikiLink[] | null | undefined,
): string {
  const map = new Map<string, WikiLink>();
  for (const link of links ?? []) {
    const key = link.target.trim();
    if (key !== "") map.set(key, link);
  }
  const re = new RegExp(WIKILINK_RE.source, "g");
  return body.replace(re, (_whole, targetRaw: string, labelRaw?: string) => {
    const target = targetRaw.trim();
    const entry = map.get(target);
    const label = (labelRaw ?? "").trim() || entry?.label || target;
    const path = entry?.path ?? null;
    if (path !== null && path !== "") {
      return `[${escapeMdText(label)}](${wikiPageHref(path)})`;
    }
    // 断链 / 后端没给映射：显示成普通文字（lint 会报 broken link），不摆死链
    return escapeMdText(label);
  });
}

/** 正文渲染前的总处理：wikilink → 链接/文字（不再有句子级引注）。 */
export function prepareWikiBody(
  body: string,
  links: readonly WikiLink[] | null | undefined,
): string {
  return replaceWikiLinks(body, links);
}

/**
 * 正文链接的白名单（渲染层把它交给 streamdown 的 urlTransform）。
 * 站内路径、hash（含角标 #cite-）、相对路径、http(s)、mailto 放行；javascript:/data:
 * 等一律换成空串（渲染层再画成普通文字，点了什么也不会发生）。
 */
export function safeMarkdownHref(url: string): string {
  const u = (url ?? "").trim();
  if (u === "") return "";
  if (
    u.startsWith("/") ||
    u.startsWith("#") ||
    u.startsWith("./") ||
    u.startsWith("../")
  ) {
    return u;
  }
  if (/^(https?:|mailto:)/i.test(u)) return u;
  if (/^[a-z][a-z0-9+.-]*:/i.test(u)) return "";
  return u; // 没有协议的相对链接（foo.md 一类）照常放行
}

// ---------- 标题锚点 ----------

export interface OutlineHeading {
  level: number;
  text: string;
  /** 稳定锚点：小写、去标点、空格转 -，中文保留，重名加 -2/-3。 */
  id: string;
}

/** Markdown 的 ATX 标题：# ~ ######（setext 的下划线标题不支持）。 */
const ATX_HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/;

/** 把标题原文收拾成"读得出来的字"：去角标、去行内代码/强调/链接外壳、去 HTML 标签。 */
function headingPlainText(raw: string): string {
  return raw
    .replace(/\[\[cite:[^\]]+\]\]/g, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`+([^`]*)`+/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/[*_~]+/g, "")
    .replace(/\\(.)/g, "$1")
    .trim();
}

/** 一个标题文本 → 锚点 id。小写、空白 → `-`、只留文字/数字/`_`/`-`，空标题兜底 `section`。 */
export function headingSlug(text: string): string {
  const cleaned = headingPlainText(text)
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^\p{L}\p{N}_-]+/gu, "")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned === "" ? "section" : cleaned;
}

/** 从 markdown 抽标题（跳过围栏代码块；重名依次 -2/-3）。书模块没有 headings 时兜底。 */
export function parseOutlineHeadings(md: string): OutlineHeading[] {
  const out: OutlineHeading[] = [];
  const used = new Set<string>();
  let fence: string | null = null;
  for (const line of md.split(/\r?\n/)) {
    const fenceMatch = line.match(/^\s*(`{3,}|~{3,})/);
    if (fenceMatch) {
      const marker = fenceMatch[1][0];
      if (fence === null) fence = marker;
      else if (fence === marker) fence = null;
      continue;
    }
    if (fence !== null) continue;
    const m = line.match(ATX_HEADING);
    if (!m) continue;
    const text = headingPlainText(m[2]);
    if (text === "") continue;
    const base = headingSlug(text);
    let id = base;
    let n = 2;
    while (used.has(id)) id = `${base}-${n++}`;
    used.add(id);
    out.push({ level: m[1].length, text, id });
  }
  return out;
}

/** 后端 headings（可能缺 id）→ 统一形状；缺字段用 parseOutlineHeadings 兜底。 */
export function normalizeHeadings(
  raw: unknown,
  bodyMd: string,
): OutlineHeading[] {
  if (Array.isArray(raw) && raw.length > 0) {
    const out: OutlineHeading[] = [];
    for (const item of raw as unknown[]) {
      const h = obj(item);
      const level = typeof h.level === "number" ? h.level : Number(h.level);
      const text = str(h.text).trim();
      const id = str(h.id).trim() || headingSlug(text);
      if (!Number.isFinite(level) || level < 1 || level > 6 || text === "" || id === "") continue;
      out.push({ level, text, id });
    }
    if (out.length > 0) return out;
  }
  return parseOutlineHeadings(bodyMd);
}

// ---------- 时间与来源人话 ----------

const STAMP_DATE_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const STAMP_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/**
 * ISO 时间 → 本地时区人话。**必须 parse**：后端混着 `+08:00` 与 `+00:00`。
 * 空值/解不出来 → 空串（调用方自己决定显示"暂无"）。
 */
export function formatStamp(iso: string | null | undefined): string {
  if (!iso) return "";
  // 日期-only（`2026-10-23`，frontmatter 的 created/updated）只显示日期：
  // Date.parse 会把它当 UTC 零点，再按本地时区格式化就会凭空多出一个 08:00。
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const d = new Date(`${iso}T00:00:00`);
    return Number.isNaN(d.getTime()) ? "" : STAMP_DATE_FORMAT.format(d);
  }
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return STAMP_FORMAT.format(new Date(t));
}

/** 一组 ISO 时间里最新的那个（首页统计用）；空/坏值返回 ""。 */
export function latestStamp(values: Iterable<string | null | undefined>): string {
  let best = "";
  let bestAt = -Infinity;
  for (const value of values) {
    const at = value ? Date.parse(value) : NaN;
    if (!Number.isNaN(at) && at > bestAt) {
      bestAt = at;
      best = value as string;
    }
  }
  return best;
}
