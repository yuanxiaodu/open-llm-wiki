import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useOutletContext } from "react-router-dom";
import { ApiError, apiGet, errText } from "../api.ts";
import { MarkdownBody } from "../components/MarkdownBody.tsx";
import { useSiteConfig } from "../site.ts";
import { actorLabel } from "../activity.ts";
import { useHeadingAnchors } from "../headings.ts";
import {
  findWikiPage,
  formatStamp,
  normalizeWikiDetail,
  confidenceHint,
  confidenceLabel,
  pageTypeLabel,
  parseOutlineHeadings,
  prepareWikiBody,
  wikiApiUrl,
  wikiPageHref,
  wikiUrlPathFromPathname,
} from "../wiki.ts";
import type { WikiPageDetail } from "../wiki.ts";
import type { WikiOutlet } from "./WikiVault.tsx";

// wiki 页面（`/wiki/{path...}`，path 不带 `.md`）：纸本衬线正文 + 页级来源。
//
// 2026-10-30：对齐本地 Hermes/LLM-wiki —— 不再有句子级引注/引注栏；来源是 frontmatter
// 的 `sources`（页级），页面底部列出来（URL 可点，raw 路径按文本显示）。
//
// 三件容易做坏的事：
// 1. **正文里的 `[[wikilink]]` 不拼 HTML**：prepareWikiBody 把 wikilink 换成 markdown
//    链接（后端 wikilinks 给了 path 才是链接，断链退成普通文字）；正文整个交给 streamdown。
// 2. **streamdown 懒加载**在 components/MarkdownBody.tsx 里（与对话面共用同一份 chunk）。
// 3. **标题 id** 由 useHeadingAnchors 在渲染后按顺序贴回 DOM（懒加载 + MutationObserver
//    兜住迟到渲染），深链与将来的目录都靠它。
//
// 页面类型里不再有「大纲」：不再有单独的阅读器路由。

export function WikiPage() {
  const { type_labels } = useSiteConfig();
  const location = useLocation();
  const { pages } = useOutletContext<WikiOutlet>();
  const rawPath = wikiUrlPathFromPathname(location.pathname);

  const [detail, setDetail] = useState<WikiPageDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState("");

  const articleRef = useRef<HTMLDivElement>(null);
  /** 正文页的作废计数：换页/卸载之后迟到的详情不许落地。 */
  const detailSeq = useRef(0);

  const headings = useMemo(
    () => (detail === null ? [] : parseOutlineHeadings(detail.body_md)),
    [detail],
  );

  useHeadingAnchors(articleRef, headings);

  const loadDetail = useCallback(async (target: string) => {
    const my = (detailSeq.current += 1);
    setLoading(true);
    setNotFound(false);
    setError("");
    setDetail(null);
    try {
      const data = await apiGet<unknown>(wikiApiUrl(target));
      if (detailSeq.current !== my) return;
      setDetail(normalizeWikiDetail(data));
    } catch (e) {
      if (detailSeq.current !== my) return;
      if (e instanceof ApiError && e.status === 401) return; // apiGet 已整页跳登录
      if (e instanceof ApiError && e.status === 404) setNotFound(true);
      else setError(errText(e, "这一页没取到"));
    } finally {
      if (detailSeq.current === my) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDetail(rawPath);
    return () => {
      detailSeq.current += 1;
    };
  }, [rawPath, loadDetail]);

  const meta = findWikiPage(pages, rawPath);
  const updated = formatStamp(detail?.updated_at ?? meta?.updated_at ?? null);
  const actor = meta?.actor ?? null;
  const actorName = actor === null ? "" : actorLabel(actor);

  if (loading) {
    return (
      <div className="vault-scroll">
        <p className="vault-state" role="status">
          这一页加载中…
        </p>
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="vault-scroll">
        <div className="vault-empty">
          <h1>没有这一页</h1>
          <p>可能还没写，或者路径变了。回索引看看现有页面。</p>
          <Link className="btn" to="/wiki">
            回索引
          </Link>
        </div>
      </div>
    );
  }

  if (detail === null) {
    return (
      <div className="vault-scroll">
        <div className="vault-error">
          <div className="banner err" role="alert">
            {error || "这一页没取到"}
          </div>
          <div className="acts">
            <button type="button" className="btn" onClick={() => void loadDetail(rawPath)}>
              重试
            </button>
            <Link className="btn" to="/wiki">
              回索引
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const title = detail.title.trim() === "" ? detail.slug : detail.title;
  const shortSha = detail.head === null ? "" : detail.head.slice(0, 7);
  const body = prepareWikiBody(detail.body_md, detail.wikilinks);

  return (
    <div className="vault-scroll">
      <article className="vault-article">
          <Link to="/wiki" className="vault-backline">
            ← 索引
          </Link>
          <h1 className="vault-title">{title}</h1>
          {detail.contested && <p className="banner warn" role="status">存在尚未解决的争议，请同时查阅双方观点和来源。</p>}
          <div className="vault-meta">
            <span className="pill">{pageTypeLabel(detail.page_type, type_labels)}</span>
            {detail.confidence !== "" && (
              <span
                className={`pill conf-${detail.confidence}`}
                title={confidenceHint(detail.confidence)}
              >
                {confidenceLabel(detail.confidence)}
              </span>
            )}
            <span>{updated === "" ? "还没有版本" : `更新于 ${updated}`}</span>
            {actorName !== "" && <span>· {actorName}</span>}
            {shortSha !== "" ? (
              <span className="sha" title={detail.head ?? ""}>
                版本 {shortSha}
              </span>
            ) : (
              <span>· 还没有版本</span>
            )}
            <Link
              className="vault-versions-link"
              to={`/wiki/versions?path=${encodeURIComponent(detail.path)}`}
            >
              版本历史
            </Link>
            {detail.tags.length > 0 && (
              <span className="wiki-tags">
                {detail.tags.map((tag) => (
                  <span className="tag" key={tag}>
                    {tag}
                  </span>
                ))}
              </span>
            )}
          </div>

          <div className="wbody" ref={articleRef}>
            <MarkdownBody markdown={body} />
          </div>

          {detail.sources.length > 0 && (
            <section className="wiki-backlinks" aria-labelledby="sources-title">
              <h2 id="sources-title">来源</h2>
              <ul>
                {detail.sources.map((src) => (
                  <li key={src}>
                    {/^https?:\/\//i.test(src) ? (
                      <a href={src} target="_blank" rel="noreferrer">
                        {src}
                      </a>
                    ) : (
                      <Link to={`/wiki/source?path=${encodeURIComponent(src)}`}>{src}</Link>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {detail.backlinks.length > 0 && (
            <section className="wiki-backlinks" aria-labelledby="backlinks-title">
              <h2 id="backlinks-title">被这些页面引用</h2>
              <ul>
                {detail.backlinks.map((back) => (
                  <li key={back.path}>
                    <Link to={wikiPageHref(back.path)}>{back.title}</Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
      </article>
    </div>
  );
}
