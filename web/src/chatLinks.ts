// 对话正文里的 [[wikilink]] → 站内链接。
//
// 智能体会在回复里写 [[slug]] / [[slug|显示文字]]（与 wiki 正文同一套语法），
// 但对话不是 wiki 页，Streamdown 不认这层语法；这里先按 /api/wiki 的页面表把它
// 换成标准 markdown 链接，再交给 Streamdown。
//
// 解析顺序：path（含/不含 .md）→ slug → 标题；找不到的保留原文，宁可看起来是未解析
// 的引用，也不把不存在的 slug 拼成一个必然 404 的链接。
import type { WikiPageMeta } from "./wiki.ts";
import { safeMarkdownHref } from "./wiki.ts";

export interface WikiLinkTarget {
  path: string;
  title: string;
}

export interface WikiLinkIndex {
  byKey: Map<string, WikiLinkTarget>;
}

function addKey(map: Map<string, WikiLinkTarget>, key: string, target: WikiLinkTarget): void {
  const clean = key.trim();
  if (clean === "") return;
  if (!map.has(clean)) map.set(clean, target);
  const lower = clean.toLowerCase();
  if (!map.has(lower)) map.set(lower, target);
}

/** 对话里链接的打开方式：wiki 链接一律新标签页；其余站内链接走 SPA。 */
export function chatLinkMode(href: string): "wiki" | "internal" | "external" | "blocked" {
  const safe = safeMarkdownHref(href);
  if (safe === "") return "blocked";
  if (safe === "/wiki" || safe.startsWith("/wiki/")) return "wiki";
  if (safe.startsWith("#") || safe.startsWith("/")) return "internal";
  return "external";
}

export function buildWikiLinkIndex(pages: readonly WikiPageMeta[]): WikiLinkIndex {
  const byKey = new Map<string, WikiLinkTarget>();
  for (const page of pages) {
    const target = { path: page.path, title: page.title };
    const noMd = page.path.replace(/\.md$/i, "");
    addKey(byKey, page.slug, target);
    addKey(byKey, page.title, target);
    addKey(byKey, page.path, target);
    addKey(byKey, noMd, target);
    addKey(byKey, noMd.split("/").at(-1) ?? "", target);
  }
  return { byKey };
}

function resolve(index: WikiLinkIndex, raw: string): WikiLinkTarget | null {
  let key = raw.trim();
  if (key === "") return null;
  // 先按原样找（path 区分大小写），再按小写找（slug/标题）
  return index.byKey.get(key) ?? index.byKey.get(key.toLowerCase()) ?? null;
}

function wikiHref(path: string): string {
  return "/wiki/" + path.replace(/\.md$/i, "").split("/").map(encodeURIComponent).join("/");
}

function escapeLabel(label: string): string {
  return label.replace(/[\\\]]/g, "\\$&");
}

function replaceInPlain(segment: string, index: WikiLinkIndex): string {
  return segment.replace(/\[\[([^\[\]\n]+?)\]\]/g, (whole, inner: string) => {
    const [rawTarget, rawLabel] = inner.split("|", 2);
    const target = resolve(index, rawTarget ?? "");
    if (target === null) return whole;
    const label = (rawLabel ?? target.title ?? rawTarget ?? "").trim() || target.title || rawTarget;
    return `[${escapeLabel(label)}](${wikiHref(target.path)})`;
  });
}

/**
 * 只处理代码围栏/行内代码之外的 [[...]]；代码块里出现同形字符串不能被改成链接。
 */
export function linkifyWikilinks(markdown: string, index: WikiLinkIndex): string {
  const parts = markdown.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g);
  return parts.map((part, i) => (i % 2 === 0 ? replaceInPlain(part, index) : part)).join("");
}
