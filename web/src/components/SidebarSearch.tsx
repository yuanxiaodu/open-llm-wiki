import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, errText } from "../api.ts";
import { splitSnippet } from "../search.ts";

// 侧栏里的模块内搜索（wiki各一份）：输入 250ms 防抖，结果最多 20 条。
//
// 边界：
//   - 只搜本模块（调用方注入 search 回调），不碰全局、不碰会话；
//   - Esc 或清空输入 → 回到目录树（结果区消失）；
//   - 结果标题 + 片段（后端 FTS 的【】用 React 节点切 <mark>，不 innerHTML）；
//   - 没结果说「没有找到」，出错给后端人话；搜索失败不挡住目录树。
//
// 组件本身不 import wiki 的取数，调用方各交一个稳定的 search(q) 回调。

export interface SidebarHit {
  key: string;
  title: string;
  snippet: string;
  href: string;
}

export interface SidebarSearchProps {
  /** 输入框的无障碍名字（「搜索 wiki」/「搜索文档」）。 */
  ariaLabel: string;
  placeholder: string;
  /** 稳定回调（useCallback）：按 q 取本模块的结果。 */
  search: (q: string) => Promise<SidebarHit[]>;
  /** 点了结果之后（窄屏把抽屉收起来）。 */
  onNavigate: () => void;
}

function Highlighted({ snippet }: { snippet: string }) {
  const parts = splitSnippet(snippet);
  if (parts.length === 0) return null;
  return (
    <>
      {parts.map((part, index) =>
        part.mark ? (
          <mark className="search-mark" key={index}>
            {part.text}
          </mark>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </>
  );
}

export function SidebarSearch({
  ariaLabel,
  placeholder,
  search,
  onNavigate,
}: SidebarSearchProps) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SidebarHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const timer = useRef<number | null>(null);
  // search 走 ref：调用方即使传内联箭头，也不会让防抖 effect 每渲染重跑
  const latestSearch = useRef(search);
  latestSearch.current = search;

  useEffect(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    const my = (seq.current += 1);
    const term = q.trim();
    if (term === "") {
      seq.current += 1;
      setHits(null);
      setError("");
      setLoading(false);
      return;
    }
    setLoading(true);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      void (async () => {
        try {
          const got = await latestSearch.current(term);
          if (seq.current !== my) return;
          setHits(got.slice(0, 20));
          setError("");
        } catch (e) {
          if (seq.current !== my) return;
          if (e instanceof ApiError && e.status === 401) return; // apiGet 已整页跳登录
          setHits([]);
          setError(errText(e, "搜索失败"));
        } finally {
          if (seq.current === my) setLoading(false);
        }
      })();
    }, 250);
    return () => {
      seq.current += 1;
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
        timer.current = null;
      }
    };
  }, [q]);

  function clear() {
    seq.current += 1;
    setQ("");
    setHits(null);
    setError("");
    setLoading(false);
  }

  const term = q.trim();
  return (
    <div className="side-search">
      <label className="sr" htmlFor="side-search-input">
        {ariaLabel}
      </label>
      <input
        id="side-search-input"
        className="input side-search-input"
        type="search"
        value={q}
        aria-label={ariaLabel}
        placeholder={placeholder}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") clear();
        }}
      />
      {term !== "" && (
        <div className="side-search-panel" aria-live="polite">
          {loading ? (
            <p className="side-search-hint">搜索中…</p>
          ) : error !== "" ? (
            <p className="side-search-hint err">{error}</p>
          ) : hits !== null && hits.length === 0 ? (
            <p className="side-search-hint">没有找到</p>
          ) : (
            <ul className="side-search-hits">
              {(hits ?? []).map((hit) => (
                <li className="side-search-hit" key={hit.key}>
                  <Link
                    to={hit.href}
                    onClick={() => {
                      clear();
                      onNavigate();
                    }}
                  >
                    <span className="side-search-hit-title">{hit.title}</span>
                    {hit.snippet !== "" && (
                      <span className="side-search-hit-snippet">
                        <Highlighted snippet={hit.snippet} />
                      </span>
                    )}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
