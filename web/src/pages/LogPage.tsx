import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, apiGet, errText } from "../api.ts";
import {
  actionLabel,
  actorLabel,
  groupLogItemsByDay,
  mergeLogItems,
  normalizeLog,
  normalizeOutcomes,
  outcomeSummary,
} from "../activity.ts";
import type { LogEntry, SessionOutcome } from "../activity.ts";
import { EMPTY_TITLE } from "../copy.ts";
import { formatStamp, normalizeWikiList, wikiPageHref } from "../wiki.ts";
import type { WikiPageMeta } from "../wiki.ts";

// 日志页（`/log`）：**仅管理员**（2026-10-30 口径）。
//
// 三路数据：
//   - `GET /api/log`：wiki 仓库 git log 的摊平（新到旧，最多 200 条）；
//   - `GET /api/log/outcomes`：每场会话最近一次知识库变更的计数；
//   - `GET /api/wiki`：只用来把提交里的 slug 变链接，取不到就不给链接。
//
// 提交与会话成果**合成一条按天的时间线**（2026-10-30 委托人反馈：原来分成「会话成果」
// 和提交记录两块太割裂）。同一天里成果行带「成果」标签，提交行带 action 标签，其余列
// 对齐；排序键是各自的 at（提交时刻 / 会话最近动静），由 mergeLogItems 统一处理。
//
// 权限判据与成员页同一套：**不只信 sessionStorage.role**——角色只决定顶栏显不显示入口，
// 进页面一律真打接口，任意一个管理员接口回 403 就整页渲染"只有管理员能看这一块"。
// 这样改了 sessionStorage 也进不来数据。
//
// 只读：这不是编辑入口，只是"谁在什么时候动了哪一页、哪场会话留下了什么"。

function findPage(pages: WikiPageMeta[], slug: string): WikiPageMeta | null {
  const want = slug.trim();
  if (want === "") return null;
  for (const page of pages) {
    if (page.slug === want) return page;
    const path = page.path.replace(/\.md$/i, "");
    if (path === want.replace(/\.md$/i, "")) return page;
  }
  return null;
}

function timeOnly(at: string | null): string {
  const stamp = formatStamp(at);
  return stamp === "" ? "" : stamp.slice(11);
}

function sessionTitle(o: SessionOutcome): string {
  const t = o.title.trim();
  return t === "" ? EMPTY_TITLE : t;
}

export function LogPage() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [outcomes, setOutcomes] = useState<SessionOutcome[]>([]);
  const [pages, setPages] = useState<WikiPageMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = (seq.current += 1);
    setLoading(true);
    setError("");
    setForbidden(false);
    try {
      const [logRaw, wikiRaw, outcomesRaw] = await Promise.allSettled([
        apiGet<unknown>("/api/log"),
        apiGet<unknown>("/api/wiki"),
        apiGet<unknown>("/api/log/outcomes"),
      ]);
      if (seq.current !== my) return;
      // 403 = 管理员闸。/api/log 与 /api/log/outcomes 都是管理员专属，任意一个回 403
      // 就整页拦；401 由 apiGet 自己整页跳登录。
      for (const res of [logRaw, outcomesRaw]) {
        if (res.status !== "rejected" || !(res.reason instanceof ApiError)) continue;
        if (res.reason.status === 401) return;
        if (res.reason.status === 403) {
          setForbidden(true);
          setEntries([]);
          setOutcomes([]);
          return;
        }
      }
      if (logRaw.status === "rejected") {
        setError(errText(logRaw.reason, "日志没取到"));
        return;
      }
      setEntries(normalizeLog(logRaw.value));
      // 成果区块挂了不该把提交时间线一起拖黑：取不到就当成没有成果
      setOutcomes(outcomesRaw.status === "fulfilled" ? normalizeOutcomes(outcomesRaw.value) : []);
      // 页面列表只是用来把 slug 变链接：取不到就不给链接，不影响日志本身
      if (wikiRaw.status === "fulfilled") {
        setPages(normalizeWikiList(wikiRaw.value).pages);
      }
    } catch (e) {
      if (seq.current !== my) return;
      setError(errText(e, "日志没取到"));
    } finally {
      if (seq.current === my) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      seq.current += 1;
    };
  }, [load]);

  // 403 的人话页照抄成员页（MembersPage.tsx）：同一件事在两处长得不一样，
  // 人会以为其中一个页面坏了。
  if (forbidden) {
    return (
      <div className="paper-page">
        <div className="paper-page-inner">
          <div className="vault-empty">
            <h1>只有管理员能看这一块</h1>
            <p>改动日志与会话成果是管理员的事；你要是想看知识库的现状，回对话里问一句就行。</p>
            <Link className="btn" to="/">
              回对话
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const timeline = mergeLogItems(entries, outcomes);
  const empty = timeline.length === 0;

  return (
    <div className="paper-page">
      <div className="paper-page-inner">
        <header className="paper-head">
          <h1>改动日志</h1>
          <p className="paper-sub">全站 wiki 的提交记录与会话成果，按本地时区按天归拢。</p>
        </header>

        {loading ? (
          <p className="vault-state" role="status">
            日志加载中…
          </p>
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
        ) : empty ? (
          <div className="vault-empty">
            <h1>还没有改动</h1>
            <p>等智能体第一次写页，这里就会记下一条提交。</p>
            <Link className="btn" to="/">
              回对话
            </Link>
          </div>
        ) : (
          <div className="log-days">
            {groupLogItemsByDay(timeline).map((group) => (
              <section className="log-day" key={group.day} aria-label={`${group.day} 的改动`}>
                <h2 className="log-day-title">{group.day}</h2>
                <ul className="log-list">
                  {group.items.map((item) => {
                    if (item.kind === "outcome") {
                      const o = item.outcome;
                      const counts = outcomeSummary(o.outcome);
                      return (
                        <li className="log-row" key={`o-${o.id}`}>
                          <span className="log-action act-outcome">成果</span>
                          <span className="log-subject">
                            <Link to={`/chat/${o.id}`}>{sessionTitle(o)}</Link>
                            {counts !== "" && <span className="log-note">{counts}</span>}
                          </span>
                          <span className="log-actor">{o.owner_name || "（没有名字）"}</span>
                          <time className="log-time" dateTime={o.at ?? undefined}>
                            {timeOnly(o.at) || "—"}
                          </time>
                        </li>
                      );
                    }
                    const entry = item.entry;
                    const page = findPage(pages, entry.slug);
                    return (
                      <li className="log-row" key={entry.sha}>
                        <span className={`log-action act-${entry.action}`}>
                          {actionLabel(entry.action)}
                        </span>
                        <span className="log-subject">
                          {page !== null ? (
                            <Link to={wikiPageHref(page.path)}>{page.title}</Link>
                          ) : (
                            <span className="log-slug">{entry.slug || "—"}</span>
                          )}
                          {entry.note !== "" && <span className="log-note">{entry.note}</span>}
                        </span>
                        <span className="log-actor">{actorLabel(entry.actor)}</span>
                        <time className="log-time" dateTime={entry.at ?? undefined}>
                          {timeOnly(entry.at) || "—"}
                        </time>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        )}

        <footer className="paper-foot">提交记录仅最近 200 条 · 时间按本地时区显示</footer>
      </div>
    </div>
  );
}
