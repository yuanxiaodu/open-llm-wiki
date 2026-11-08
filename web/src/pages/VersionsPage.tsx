import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { ApiError, apiGet, errText } from "../api.ts";
import { actionLabel, actorLabel, shortSha } from "../activity.ts";
import {
  defaultDiffPair,
  isUnifiedDiff,
  normalizeVersions,
  pairFromSelected,
  parseDiffLines,
  pickVersion,
  versionStamp,
} from "../versions.ts";
import type { VersionEntry } from "../versions.ts";
import { findWikiPage, normalizeWikiList, wikiApiUrl, wikiPageHref } from "../wiki.ts";
import type { WikiPageMeta } from "../wiki.ts";
import type { WikiOutlet } from "./WikiVault.tsx";

// 版本历史 + 只读 diff（`/wiki/versions?path=<页面 path>`）。
//
// 这是 WikiPage 页头「版本历史」进来的唯一入口；页面**没有**编辑/回滚按钮——只读。
// 流程：先拿 `/api/wiki` 列表校验 path（只接受列表里真存在的页，防手拼 URL 乱查），
// 再拉这一页的 versions；默认勾最近两版，选满两版就拉 diff。
// diff 用 parseDiffLines 染色；首行不像 unified diff 就原样 <pre>，不猜。

const DIFF_PAGE_NOTE = "只读：这里只对比历史版本，不提供回滚。";

function DiffView({ text }: { text: string }) {
  if (text === "") return <p className="muted">这两版没有差异。</p>;
  if (!isUnifiedDiff(text)) {
    return (
      <pre className="diff-raw" aria-label="差异原文">
        {text}
      </pre>
    );
  }
  return (
    <pre className="diff-view" aria-label="逐行差异">
      {parseDiffLines(text).map((line, index) => (
        <span className={`diff-line ${line.kind}`} key={index}>
          {line.text === "" ? " " : line.text}
        </span>
      ))}
    </pre>
  );
}

export function VersionsPage() {
  const [params] = useSearchParams();
  const rawPath = params.get("path") ?? "";
  const { pages: treePages } = useOutletContext<WikiOutlet>();

  const [page, setPage] = useState<WikiPageMeta | null>(null);
  const [versions, setVersions] = useState<VersionEntry[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState("");
  const [diff, setDiff] = useState("");
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState("");

  const seq = useRef(0);
  const diffSeq = useRef(0);

  const load = useCallback(async () => {
    const my = (seq.current += 1);
    setLoading(true);
    setError("");
    setNotFound(false);
    setPage(null);
    setVersions([]);
    setSelected([]);
    if (rawPath.trim() === "") {
      setLoading(false);
      return;
    }
    try {
      const wikiRaw = await apiGet<unknown>("/api/wiki");
      if (seq.current !== my) return;
      const list = normalizeWikiList(wikiRaw);
      const found = findWikiPage(list.pages, rawPath);
      if (found === null) {
        setNotFound(true);
        return;
      }
      setPage(found);
      const versionsRaw = await apiGet<unknown>(`${wikiApiUrl(found.path)}/versions`);
      if (seq.current !== my) return;
      const got = normalizeVersions(versionsRaw);
      setVersions(got);
      const pair = defaultDiffPair(got);
      setSelected(pair !== null ? [pair.a, pair.b] : got.slice(0, 1).map((v) => v.sha));
    } catch (e) {
      if (seq.current !== my) return;
      if (e instanceof ApiError && e.status === 401) return; // apiGet 已整页跳登录
      setError(errText(e, "版本记录没取到"));
    } finally {
      if (seq.current === my) setLoading(false);
    }
  }, [rawPath]);

  useEffect(() => {
    void load();
    return () => {
      seq.current += 1;
      diffSeq.current += 1;
    };
  }, [load]);

  // 两版都选好 → 拉 diff（默认最近两版也走这里）
  const pair = page !== null ? pairFromSelected(versions, selected) : null;
  useEffect(() => {
    const my = (diffSeq.current += 1);
    setDiff("");
    setDiffError("");
    if (page === null || pair === null) {
      setDiffLoading(false);
      return;
    }
    setDiffLoading(true);
    void (async () => {
      try {
        const url =
          `${wikiApiUrl(page.path)}/diff?a=${encodeURIComponent(pair.a)}` +
          `&b=${encodeURIComponent(pair.b)}`;
        const raw = await apiGet<{ diff?: unknown }>(url);
        if (diffSeq.current !== my) return;
        setDiff(typeof raw?.diff === "string" ? raw.diff : "");
      } catch (e) {
        if (diffSeq.current !== my) return;
        if (e instanceof ApiError && e.status === 401) return;
        setDiffError(errText(e, "差异没取到"));
      } finally {
        if (diffSeq.current === my) setDiffLoading(false);
      }
    })();
  }, [page, pair?.a, pair?.b]); // eslint 不装；a/b 是唯一依赖的字符串

  const backPage = page ?? findWikiPage(treePages, rawPath);

  return (
    <div className="vault-scroll">
      <div className="versions-page">
        <header className="vault-index-head versions-head">
          <Link className="vault-backline" to={backPage !== null ? wikiPageHref(backPage.path) : "/wiki"}>
            ← 回页面
          </Link>
          <h1 className="versions-title">版本历史</h1>
          <p className="paper-sub">
            {backPage !== null ? backPage.title : rawPath || "未指定页面"} · {DIFF_PAGE_NOTE}
          </p>
        </header>

        {loading ? (
          <p className="vault-state" role="status">
            版本记录加载中…
          </p>
        ) : rawPath.trim() === "" ? (
          <div className="vault-empty">
            <h1>没有指定页面</h1>
            <p>从 wiki 页面右上角的「版本历史」进来，或者先选一页。</p>
            <Link className="btn" to="/wiki">
              回 wiki
            </Link>
          </div>
        ) : notFound ? (
          <div className="vault-empty">
            <h1>没有这一页的版本记录</h1>
            <p>这一页可能已经不在 wiki 列表里了。回 wiki 看看现在有什么。</p>
            <Link className="btn" to="/wiki">
              回 wiki
            </Link>
          </div>
        ) : error !== "" ? (
          <div className="vault-error">
            <div className="banner err" role="alert">
              {error}
            </div>
            <div className="acts">
              <button type="button" className="btn" onClick={() => void load()}>
                重试
              </button>
            </div>
          </div>
        ) : versions.length === 0 ? (
          <p className="vault-state">这一页还没有提交记录。</p>
        ) : (
          <>
            <section className="paper-section" aria-labelledby="version-list-title">
              <h2 id="version-list-title">
                提交记录<span className="count">{versions.length} 版</span>
              </h2>
              <p className="paper-sub small">勾选两版看改了什么（默认最近两版）。</p>
              <ul className="version-list">
                {versions.map((version) => {
                  const checked = selected.includes(version.sha);
                  return (
                    <li className={checked ? "version-row on" : "version-row"} key={version.sha}>
                      <input
                        type="checkbox"
                        className="version-check"
                        id={`ver-${version.sha}`}
                        checked={checked}
                        onChange={() => setSelected((prev) => pickVersion(prev, version.sha))}
                      />
                      <label className="version-main" htmlFor={`ver-${version.sha}`}>
                        <code className="version-sha" title={version.sha}>
                          {shortSha(version.sha)}
                        </code>
                        <span className={`log-action act-${version.action}`}>
                          {actionLabel(version.action)}
                        </span>
                        <span className="version-note">{version.note || "（没有说明）"}</span>
                        <span className="version-meta">
                          {actorLabel(version.actor)} · {versionStamp(version)}
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className="paper-section" aria-labelledby="version-diff-title">
              <h2 id="version-diff-title">
                改了什么
                {pair !== null && (
                  <span className="count">
                    {shortSha(pair.a)} → {shortSha(pair.b)}
                  </span>
                )}
              </h2>
              {versions.length < 2 ? (
                <p className="muted">只有一版，没有可对比的旧版本。</p>
              ) : pair === null ? (
                <p className="muted">
                  选两版对比（已选 {selected.length} 版，默认最近两版）。
                  {selected.length > 0 && (
                    <button
                      type="button"
                      className="link"
                      onClick={() => {
                        const pair = defaultDiffPair(versions);
                        setSelected(pair !== null ? [pair.a, pair.b] : []);
                      }}
                    >
                      恢复默认
                    </button>
                  )}
                </p>
              ) : diffLoading ? (
                <p className="vault-state" role="status">
                  差异加载中…
                </p>
              ) : diffError !== "" ? (
                <div className="banner err" role="alert">
                  {diffError}
                </div>
              ) : (
                <DiffView text={diff} />
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
