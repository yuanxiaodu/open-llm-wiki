import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError } from "../api.ts";
import { useFocusTrap } from "../focus.ts";
import { EMPTY_TITLE } from "../copy.ts";
import { useAppShell } from "./AppShell.tsx";
import type { SessionSummary } from "../types.ts";

// 对话页的侧栏上下文（由 AppShell 的 260px 侧栏承载）：新会话 + 按归属者分组 + 条目菜单。
// 品牌/模块导航/身份/退出/抽屉开合与焦点都在 AppShell；这里只管会话列表本身与弹窗。
//
// 五条口径：
//   1. **分组与 React key 都按 user_id**，只有显示出来的组名用 owner_name——名字会撞、
//      会改，id 不会。按名字分组时两个同名的人会被并成一组，而且 React 的 key 会撞
//      （撞 key 的后果是切会话时条目状态串位）。
//   2. 条目菜单（改名/归档）只在**归属者**自己的条目上出现：后端对非归属者是 403，
//      前端就不该摆一个点了只会挨骂的按钮（非归属者连菜单都看不见）。
//   3. 改名 60 字上限（后端 rename 是截断而不是报错，前端直接卡住输入更明白）；
//      归档确认里必须写人话后果：「归档后只读，仍可被搜索和引用」。
//   4. 焦点不许丢：弹窗关掉后还给那条 ⋯（⋯ 不在屏幕上时还 ☰，见 focusAfterModal）；
//      抽屉的打开聚焦/Tab 围栏/Esc 关现在由 AppShell 的 useFocusTrap 管，关掉之后
//      焦点也由 AppShell 还给 ☰。键盘用户不该被扔回 body 从头 Tab。
//   5. 窄屏下**开弹窗先关抽屉**：抽屉与弹窗都是模态浮层，两层叠着的话抽屉那个 capture
//      阶段的 Esc（带 stopPropagation）会替弹窗把 Esc 吃掉，Tab 围栏也把弹窗的控件排除
//      在外。一次只留一层，弹窗自己的 Esc 与焦点归还才说了算。

/** 后端 rename 的上限（app/services/chat.py：title[:60]）。 */
const TITLE_MAX = 60;
/** 归档折叠区的开合记忆：刷新之后保持用户上一次的选择。 */
const ARCHIVED_OPEN_KEY = "openllmwiki.archived.open";
// 「（空会话）」在 copy.ts：对话页的大标题用的是同一句，两处各写一份迟早漂成两句。
const ARCHIVE_CONSEQUENCE = "归档后从列表收起、只读，仍可被搜索和引用；随时可以取消归档。";

export interface SessionGroup {
  /** React key：归属者的 user_id。绝不拿 label 当 key（同名的人会撞）。 */
  key: number;
  /** 显示用的组名 = owner_name（分组判据是 id，显示仍然是名字）。 */
  label: string;
  mine: boolean;
  items: SessionSummary[];
}

/**
 * 按归属者分组：我的组恒在最上，其余组按后端给的顺序（最近动静倒序）首次出现的先后排。
 * 组内顺序原样保留——后端已经排好了，再排一次只是把"最近动静"这个信息弄丢。
 *
 * 键是 user_id：两个同名的人各自成组（组名看着一样，但会话不会串到一起），
 * React 那边的 key 也因此不会撞。
 */
export function groupSessions(sessions: SessionSummary[], meId: number): SessionGroup[] {
  const mine: SessionSummary[] = [];
  const others = new Map<number, SessionGroup>();
  for (const s of sessions) {
    if (s.user_id === meId) {
      mine.push(s);
      continue;
    }
    const group = others.get(s.user_id);
    if (group) group.items.push(s);
    else
      others.set(s.user_id, {
        key: s.user_id,
        label: s.owner_name || "（没有名字）",
        mine: false,
        items: [s],
      });
  }
  const groups: SessionGroup[] = [];
  if (mine.length > 0) groups.push({ key: meId, label: "我的会话", mine: true, items: mine });
  for (const group of others.values()) groups.push(group);
  return groups;
}

/**
 * 归档会话与活跃会话分开（2026-10-30）：活跃列表只留工作区，归档收进底部折叠区。
 * 不改变后端排序——两组各自保持 list_sessions 给的"最近动静"顺序。
 */
export function splitArchived(sessions: SessionSummary[]): {
  active: SessionSummary[];
  archived: SessionSummary[];
} {
  const active: SessionSummary[] = [];
  const archived: SessionSummary[] = [];
  for (const s of sessions) (s.status === "archived" ? archived : active).push(s);
  return { active, archived };
}

function titleOf(s: SessionSummary): string {
  const t = (s.title ?? "").trim();
  return t === "" ? EMPTY_TITLE : t;
}

/**
 * 弹窗关掉后焦点该交给谁（口径 4）。
 *
 * `"more"` = 打开它的那条 ⋯，`"trigger"` = 窄屏的 ☰，`null` = 两件都不在，让焦点落回 body
 * （也比硬 focus 一个看不见的元素强）。
 *
 * 为什么要有"回 ☰"这一档：≤700px 下弹窗是从抽屉里开的，而开弹窗那一下抽屉就关了
 * （口径 5），列表里那条 ⋯ 随之滑到画布外面。焦点交给一个屏幕外的元素，人只看得见
 * 焦点环消失、读屏念的是一条看不见的条目，Tab 也从那儿接着走——等于把键盘用户扔进
 * 抽屉里。☰ 是重新打开抽屉回到这份列表的入口，落在那儿比落在 body 有用。
 * 宽屏侧栏常驻可见，⋯ 就在眼前，照旧还给它（triggerUsable 在宽屏是 false：☰ 被 CSS
 * display:none 了，压根不该 focus）。
 *
 * 纯函数，所以能在没有 jsdom 的情况下把 8 种组合全钉住（SessionList.test.ts）。
 */
export function focusAfterModal(
  viaDrawer: boolean,
  moreUsable: boolean,
  triggerUsable: boolean,
): "more" | "trigger" | null {
  const order = viaDrawer ? (["trigger", "more"] as const) : (["more", "trigger"] as const);
  const usable = { more: moreUsable, trigger: triggerUsable };
  return order.find((k) => usable[k]) ?? null;
}

/**
 * 这件控件现在**能不能接焦点**：还在文档里、有非零盒子、并且落在视口里。
 *
 * 最后一条是为窄屏抽屉写的：≤700px 下侧栏是 `position: fixed; transform: translateX(-105%)`
 * 的画布外元素（theme.css 的 @media (max-width: 700px)），盒子尺寸照样非零、`focus()`
 * 照样"成功"，只有位置在屏幕外——所以判可见性得看 rect 与视口的交集，`offsetParent`
 * 与 `display` 都判不出来这一档。
 */
function canTakeFocus(el: HTMLElement | null): boolean {
  if (!el || !el.isConnected) return false;
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  return r.right > 0 && r.left < window.innerWidth && r.bottom > 0 && r.top < window.innerHeight;
}

interface Props {
  sessions: SessionSummary[];
  currentId: number | null;
  me: { id: number; name: string; role: string };
  onNew: () => void;
  onPick: (id: number) => void;
  onRename: (id: number, title: string) => Promise<void>;
  onArchive: (id: number) => Promise<void>;
  /** 取消归档（归档折叠区条目菜单）：成功由 Chat 负责刷新列表与详情。 */
  onUnarchive: (id: number) => Promise<void>;
  creating: boolean;
}

export function SessionList({
  sessions,
  currentId,
  me,
  onNew,
  onPick,
  onRename,
  onArchive,
  onUnarchive,
  creating,
}: Props) {
  const { drawerOpen, closeDrawer, menuRef: drawerTriggerRef } = useAppShell();
  const { active, archived } = splitArchived(sessions);
  const groups = groupSessions(active, me.id);
  // 归档折叠区默认收起；开合状态记在本机（刷新/换页保持）。
  const [archivedOpen, setArchivedOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem(ARCHIVED_OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });

  function toggleArchived() {
    setArchivedOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(ARCHIVED_OPEN_KEY, next ? "1" : "0");
      } catch {
        // 隐私模式/存储被禁：只影响记忆，不影响开合
      }
      return next;
    });
  }
  // 条目菜单一次只开一个：值是开着的那个会话 id
  const [menuFor, setMenuFor] = useState<number | null>(null);
  // 弹窗：{kind, session, viaDrawer}；改名与归档共用一个位置，一次只可能有一个。
  // viaDrawer = 这个弹窗是不是从窄屏抽屉里开出来的（决定关掉后焦点回 ☰ 还是回 ⋯）
  const [modal, setModal] = useState<{
    kind: "rename" | "archive";
    session: SessionSummary;
    viaDrawer: boolean;
  } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  /** 每条 ⋯ 按钮的 DOM，键 = 会话 id：弹窗关掉时把焦点还回它（口径 4）。 */
  const moreRefs = useRef(new Map<number, HTMLButtonElement>());

  // 点外面 / Esc 关掉条目菜单
  useEffect(() => {
    if (menuFor === null) return;
    function onDown(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuFor(null);
    }
    function onKey(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setMenuFor(null);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuFor]);

  // 切会话时把菜单收起来，别让它挂在已经看不见的那一条上
  useEffect(() => setMenuFor(null), [currentId]);

  /** 一条会话（活跃与归档共用）。isArchived 只影响 muted 样式、小字里的归属者与菜单项。 */
  function renderItem(s: SessionSummary, isArchived: boolean) {
    const on = s.id === currentId;
    const owner = s.user_id === me.id;
    // 小字只剩归档条目的归属者（2026-10-30：知识库操作计数从对话列表整体退场，
    // 后台数字只留给管理员日志页）。
    const sub = isArchived ? s.owner_name || "（没有名字）" : "";
    return (
      <div className={`item${on ? " on" : ""}${isArchived ? " archived" : ""}`} key={s.id}>
        {/* 标题与小字包在同一列里：行末的 ⋯ 永远和标题同一行，绝不换行。 */}
        <div className="ti">
          <button
            type="button"
            className="t"
            title={titleOf(s)}
            onClick={() => {
              closeDrawer();
              onPick(s.id);
            }}
          >
            {titleOf(s)}
          </button>
          {sub !== "" && <span className="sub">{sub}</span>}
        </div>
        {owner && (
          <div className="menu-wrap" ref={menuFor === s.id ? menuRef : undefined}>
            <button
              type="button"
              className="more"
              aria-haspopup="menu"
              aria-expanded={menuFor === s.id}
              aria-label={`「${titleOf(s)}」的操作`}
              ref={(el) => {
                if (el) moreRefs.current.set(s.id, el);
                else moreRefs.current.delete(s.id);
              }}
              onClick={() => setMenuFor(menuFor === s.id ? null : s.id)}
            >
              ⋯
            </button>
            {menuFor === s.id && (
              <div className="menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuFor(null);
                    closeDrawer();
                    setModal({ kind: "rename", session: s, viaDrawer: drawerOpen });
                  }}
                >
                  改名
                </button>
                {isArchived ? (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuFor(null);
                      closeDrawer();
                      void onUnarchive(s.id);
                    }}
                  >
                    取消归档
                  </button>
                ) : (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuFor(null);
                      closeDrawer();
                      setModal({ kind: "archive", session: s, viaDrawer: drawerOpen });
                    }}
                  >
                    归档
                  </button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      <div className="sb-context">
        <button
          type="button"
          className="newbtn"
          disabled={creating}
          onClick={() => {
            closeDrawer();
            onNew();
          }}
        >
          {creating ? "正在开…" : "＋ 新会话"}
        </button>

        {groups.map((g) => (
          <div className="group" key={g.key}>
            <div className="grp">{g.label}</div>
            {g.items.map((s) => renderItem(s, false))}
          </div>
        ))}

        {groups.length === 0 && archived.length === 0 && (
          <p className="sb-empty">还没有会话。</p>
        )}

        {archived.length > 0 && (
          <div className="arch-block">
            <button
              type="button"
              className="arch-toggle"
              aria-expanded={archivedOpen}
              onClick={toggleArchived}
            >
              <span>已归档（{archived.length}）</span>
              <span className="chev" aria-hidden="true">
                {archivedOpen ? "▾" : "▸"}
              </span>
            </button>
            {archivedOpen && (
              <div className="arch-list">{archived.map((s) => renderItem(s, true))}</div>
            )}
          </div>
        )}

      </div>

      {modal && createPortal(
        <SessionModal
          // key 是"哪一个弹窗"的身份证：改名 ↔ 归档、或换一个会话再开，都是**另一个**弹窗，
          // 必须整个重挂载。不挂 key 的话 React 会复用同一个实例——标题输入框里留着上一场
          // 会话的名字（useState 的初值只在挂载时读一次），焦点也不会重新走一遍
          // "改名进输入框并全选 / 归档落取消"那一下。
          key={`${modal.kind}:${modal.session.id}`}
          kind={modal.kind}
          session={modal.session}
          onClose={() => {
            setModal(null);
            // 焦点还给打开它的那件控件（口径 4）。放在 setModal 之后同步调：这一刻弹窗
            // 还在 DOM 里，但焦点已经离开它了，随后 React 卸载弹窗不会把焦点拽回 body。
            //
            // 还给谁由 focusAfterModal 定：从抽屉里开的（≤700px，抽屉在开弹窗那一下就关了）
            // 回 ☰，宽屏回那条 ⋯；两件都不在屏幕上就让焦点落回 body，绝不 focus 一个
            // 看不见的元素（那等于把键盘用户扔进画布外的抽屉里）。
            const more = moreRefs.current.get(modal.session.id) ?? null;
            const trigger = drawerTriggerRef.current ?? null;
            const which = focusAfterModal(
              modal.viaDrawer,
              canTakeFocus(more),
              canTakeFocus(trigger),
            );
            if (which === "more") more?.focus();
            else if (which === "trigger") trigger?.focus();
          }}
          onRename={onRename}
          onArchive={onArchive}
        />,
        document.body,
      )}
    </>
  );
}

interface ModalProps {
  kind: "rename" | "archive";
  session: SessionSummary;
  onClose: () => void;
  onRename: (id: number, title: string) => Promise<void>;
  onArchive: (id: number) => Promise<void>;
}

/** 低频操作不铺内联表单（计划 Global Constraints）：入口在条目菜单里，改动在弹窗里做。 */
function SessionModal({ kind, session, onClose, onRename, onArchive }: ModalProps) {
  const [title, setTitle] = useState(() => (session.title ?? "").trim());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 归档那一支没有输入框：焦点落「取消」（确认框默认停在最不具破坏性的那件上）。 */
  const cancelRef = useRef<HTMLButtonElement>(null);
  /** 焦点陷阱要的那个根：弹窗卡片本身（不是遮罩——Tab 围栏该围着卡片转）。 */
  const cardRef = useRef<HTMLDivElement>(null);

  /**
   * 与抽屉同一份焦点纪律（focus.ts）：Esc 关、Tab 在弹窗里打转。
   *
   * autoFocus 交给下面那个 effect：弹窗"焦点落哪儿"是有讲究的（改名要进输入框**并全选**，
   * 归档要停在「取消」），钩子那个"落第一件"的默认动作在这儿是错的。
   * onClose 在 busy 时交 undefined：正在改名/归档的那一趟不许被 Esc 半截关掉
   * （关掉之后请求还在飞，成功了界面却已经当成没这回事）。围栏照旧装着。
   */
  useFocusTrap(cardRef, true, { onClose: busy ? undefined : onClose, autoFocus: false });

  useEffect(() => {
    // 改名：焦点进输入框并全选（原来的行为）。归档：焦点落「取消」。
    //
    // 两种弹窗都必须**自己接住焦点**，不能留在 body 上：≤700px 下弹窗是从抽屉里开的，
    // 开弹窗那一下抽屉就关了（口径 5），于是 Chat 那句"抽屉关了就把焦点还给 ☰"与这里
    // 撞在同一次提交里——被动效果是孩子先跑、父亲后跑，所以这里先把焦点收进弹窗，
    // Chat 那边看见焦点已经在 aria-modal 里就不抢（pages/Chat.tsx 的抽屉 effect）。
    if (inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    } else {
      cancelRef.current?.focus();
    }
  }, []);

  const empty = title.trim().length === 0;

  async function submit() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (kind === "rename") await onRename(session.id, title.trim());
      else await onArchive(session.id);
      onClose();
    } catch (err) {
      // 后端给的就是人话（403「只有开这个会话的人能改名」、400「标题不能是空的」），
      // 原样摆出来，不翻译。
      setError(err instanceof ApiError ? err.message : "没成，稍后再试一次");
      setBusy(false);
    }
  }

  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="modal card"
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="session-modal-title"
      >
        {kind === "rename" ? (
          <>
            <h2 id="session-modal-title">改名</h2>
            <p className="sub">这场会话现在叫「{titleOf(session)}」</p>
            {error && (
              <div className="banner err" role="alert">
                {error}
              </div>
            )}
            <label className="sr" htmlFor="rename-title">
              新名字
            </label>
            <input
              id="rename-title"
              className="input"
              ref={inputRef}
              value={title}
              maxLength={TITLE_MAX}
              onChange={(e) => setTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !empty) void submit();
              }}
            />
            <div className="count">
              {title.length}/{TITLE_MAX}
            </div>
            {empty && <div className="tip">标题不能是空的</div>}
            <div className="acts">
              <button type="button" className="btn" onClick={onClose} disabled={busy}>
                取消
              </button>
              <button type="button" className="btn pri" onClick={() => void submit()} disabled={busy || empty}>
                {busy ? "改着…" : "保存"}
              </button>
            </div>
          </>
        ) : (
          <>
            <h2 id="session-modal-title">归档这场会话</h2>
            <p className="sub">「{titleOf(session)}」</p>
            {error && (
              <div className="banner err" role="alert">
                {error}
              </div>
            )}
            <p className="consequence">{ARCHIVE_CONSEQUENCE}</p>
            <div className="acts">
              <button type="button" className="btn" ref={cancelRef} onClick={onClose} disabled={busy}>
                取消
              </button>
              <button type="button" className="btn danger" onClick={() => void submit()} disabled={busy}>
                {busy ? "归档中…" : "确认归档"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
