import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { ApiError, apiGet, apiPost, errText } from "../api.ts";
import { MarkdownBody } from "../components/MarkdownBody.tsx";
import { useSiteConfig } from "../site.ts";
import { useIdentity } from "../useIdentity.ts";
import {
  formatStamp,
  normalizeWikiIndex,
  pageTypeLabel,
  prepareWikiBody,
  wikiApiUrl,
  wikiPageHref,
  wikilinksFromPages,
} from "../wiki.ts";
import type { WikiIndexDoc, WikiPageMeta } from "../wiki.ts";
import type { WikiOutlet } from "./WikiVault.tsx";

// wiki 索引（`/wiki`）：渲染后端自动维护的 `index.md` 正文（streamdown）。
//
// 数据源是 `GET /api/wiki/index`（契约：{path,body_md,updated_at}）。index.md 里是
// `[[slug]]` 形式的互链，但那个接口**不返回** wikilinks 映射，所以这里用页面列表
// 现造一份（wikilinksFromPages：slug/title → path），断链退化成普通文字。
//
// 降级：接口 404/500（后端并行、还没落地）或正文为空时，退到本地卡片索引（页面列表
// 还在，人不至于面对一页错误）。不摆编辑入口——索引由服务端自动维护，人类面只读。

function fallbackList(pages: WikiPageMeta[], type_labels: Record<string, string>) {
  return (
    <section className="vault-group" aria-labelledby="grp-all">
      <h2 id="grp-all">
        全部页面<span className="count">{pages.length} 页</span>
      </h2>
      <ul className="page-cards">
        {pages.map((page) => (
          <li key={page.path}>
            <Link className="page-card" to={wikiPageHref(page.path)}>
              <span className="pc-type">{pageTypeLabel(page.page_type, type_labels)}</span>
              <h3>{page.title}</h3>
              {page.summary !== "" && <p className="pc-summary">{page.summary}</p>}
              <p className="pc-meta">
                {formatStamp(page.updated_at) || "还没有版本"}
                {page.actor ? ` · ${page.actor}` : ""}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

interface WikiSyncReport {
  ok: boolean;
  skipped?: string;
  error?: string;
  pulled?: boolean;
  behind?: number;
}

export function WikiIndex() {
  const { type_labels } = useSiteConfig();
  const me = useIdentity();
  const canSync = me?.role === "admin";
  const { pages, loading: pagesLoading, error: pagesError, reload } =
    useOutletContext<WikiOutlet>();

  const [doc, setDoc] = useState<WikiIndexDoc | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const seq = useRef(0);

  const loadIndex = useCallback(async () => {
    const my = (seq.current += 1);
    setLoading(true);
    setError("");
    try {
      const data = await apiGet<unknown>(wikiApiUrl(""));
      if (seq.current !== my) return;
      const index = normalizeWikiIndex(data);
      setDoc(index.body_md.trim() === "" ? null : index);
    } catch (e) {
      if (seq.current !== my) return;
      if (e instanceof ApiError && e.status === 401) return; // apiGet 已整页跳登录
      setError(errText(e, "索引没取到"));
      setDoc(null);
    } finally {
      if (seq.current === my) setLoading(false);
    }
  }, []);

  const syncWiki = useCallback(async () => {
    setSyncing(true);
    setSyncMsg("");
    try {
      const report = await apiPost<WikiSyncReport>("/api/wiki/sync");
      if (report.skipped) {
        setSyncMsg(report.skipped === "no-remote" ? "没有配置远端，跳过同步" : `同步跳过：${report.skipped}`);
      } else if (report.pulled) {
        setSyncMsg(`已拉取 ${report.behind ?? 0} 个提交`);
      } else {
        setSyncMsg("已是最新");
      }
      reload();
      await loadIndex();
    } catch (e) {
      setSyncMsg(errText(e, "同步没做成"));
    } finally {
      setSyncing(false);
    }
  }, [loadIndex, reload]);

  const syncBar = canSync ? (
    <div className="vault-sync-bar">
      <button
        type="button"
        className="btn"
        onClick={() => void syncWiki()}
        disabled={syncing}
        title="从远端拉取最新 wiki，并刷新本页目录"
      >
        {syncing ? "同步中…" : "同步 wiki"}
      </button>
      {syncMsg !== "" && <span className="vault-sub" role="status">{syncMsg}</span>}
    </div>
  ) : null;

  useEffect(() => {
    void loadIndex();
    return () => {
      seq.current += 1;
    };
  }, [loadIndex]);

  if (loading && doc === null && pages.length === 0) {
    return (
      <div className="vault-scroll">
        {syncBar}
        <p className="vault-state" role="status">
          wiki 索引加载中…
        </p>
      </div>
    );
  }

  // 有 index.md：渲染正文（wikilink 用页面列表现造的映射）
  if (doc !== null) {
    return (
      <div className="vault-scroll">
        {syncBar}
        <article className="vault-index wiki-index">
          <div className="wbody">
            <MarkdownBody markdown={prepareWikiBody(doc.body_md, wikilinksFromPages(pages))} />
          </div>
        </article>
      </div>
    );
  }

  // 索引接口没有/空了：本地卡片兜底
  if (pages.length > 0) {
    return (
      <div className="vault-scroll">
        {syncBar}
        <article className="vault-index wiki-index">
          <header>
            <h1>wiki 索引</h1>
            {error !== "" && <p className="vault-sub">索引正文暂时取不到，下面是页面列表。</p>}
          </header>
          {fallbackList(pages, type_labels)}
        </article>
      </div>
    );
  }

  if (pagesLoading) {
    return (
      <div className="vault-scroll">
        {syncBar}
        <p className="vault-state" role="status">
          目录加载中…
        </p>
      </div>
    );
  }

  if (pagesError !== "" || error !== "") {
    return (
      <div className="vault-scroll">
        {syncBar}
        <div className="vault-error">
          <div className="banner err" role="alert">
            {pagesError || error}
          </div>
          <div className="acts">
            <button
              type="button"
              className="btn"
              onClick={() => {
                reload();
                void loadIndex();
              }}
            >
              重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="vault-scroll">
      {syncBar}
      <div className="vault-empty">
        <h1>还没有页面</h1>
        <p>
          wiki 还是空的。去对话里让智能体把材料写成一页，写完这里就有了——这个界面只负责读。
        </p>
        <div className="acts">
          <Link className="btn pri" to="/chat">
            去对话
          </Link>
        </div>
      </div>
    </div>
  );
}
