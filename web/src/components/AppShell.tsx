import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { apiPost, clearIdentity } from "../api.ts";
import { useIdentity } from "../useIdentity.ts";
import { useSiteConfig, useSiteName } from "../site.ts";
import type { PluginNav } from "../site.ts";
import { useFocusTrap } from "../focus.ts";

// 全站唯一外壳（S12）：左侧 260px 侧栏 + 右侧主区；移动端收成同一个抽屉。
//
// 结构：
//   品牌（link /）
//   模块导航 对话 / wiki / 日志（admin） / 管理（admin）——aria-current 按路由
//   sidebar 插槽：chat 的会话列表、wiki 的搜索+树；日志/管理可空
//   底部身份 + 退出
//
// 各页面/布局路由自己包一层 AppShell（不再有全局顶栏）：Chat 传入自定义 topbar
// （会话标题/操作），其它页面用 AppShell 的默认移动端顶栏（☰ + 当前模块名）。
//
// 兼容旧 a11y 选择的别名：aside 同时带 .sb/.vault-side，遮罩带 .scrim/.vault-scrim，
// 菜单钮带 .drawer-btn/.vault-burger；抽屉开合/焦点/模态属性仍只由 AppShell 管。

export interface AppShellProps {
  /** 侧栏上下文（chat=新会话+会话列表；wiki=搜索+树；可空）。 */
  sidebar?: ReactNode;
  /** 页面自定义顶栏（Chat 的会话条）；不传则用默认的移动端顶栏。 */
  topbar?: ReactNode;
  /** 主区额外 class（chat / vault-main 等）。 */
  mainClassName?: string;
  children: ReactNode;
}

interface ShellContextValue {
  drawerOpen: boolean;
  openDrawer: () => void;
  closeDrawer: () => void;
  menuRef: RefObject<HTMLButtonElement | null>;
}

const ShellContext = createContext<ShellContextValue | null>(null);

/** 页面里的 ☰ / 抽屉开关：AppShell 提供，子页面只消费。 */
export function useAppShell(): ShellContextValue {
  const ctx = useContext(ShellContext);
  if (ctx === null) throw new Error("useAppShell 必须在 <AppShell> 里用");
  return ctx;
}

export interface ShellNavItem {
  key: string;
  to: string;
  label: string;
  short: string;
  admin?: boolean;
  match: (pathname: string) => boolean;
}

const CORE_HEAD: ShellNavItem[] = [
  {
    key: "chat", to: "/", label: "对话", short: "对",
    match: (p) => p === "/" || p === "/chat" || p.startsWith("/chat/"),
  },
  { key: "wiki", to: "/wiki", label: "wiki", short: "w", match: (p) => p.startsWith("/wiki") },
];

const CORE_TAIL: ShellNavItem[] = [
  // 日志只有管理员可见：普通成员连入口都不摆；直达 /log 由页面按 403 拦。
  { key: "log", to: "/log", label: "日志", short: "日", admin: true, match: (p) => p.startsWith("/log") },
  // 管理（原「成员」）：站点设置 + 成员 + 邀请，同样只给管理员。
  { key: "members", to: "/members", label: "管理", short: "管", admin: true, match: (p) => p.startsWith("/members") },
];

/** 核心导航 + /api/site 里启用的插件 nav。插件排在 wiki 之后、管理员入口之前。 */
export function shellNavItems(plugins: readonly PluginNav[]): ShellNavItem[] {
  const pluginItems: ShellNavItem[] = plugins
    .map((plugin) => ({
      key: plugin.nav.key,
      to: plugin.nav.to,
      label: plugin.nav.label,
      short: plugin.nav.short,
      match: (p: string) => p === plugin.nav.to || p.startsWith(plugin.nav.to + "/"),
    }))
    .sort((a, b) => {
      const orderA = plugins.find((item) => item.nav.key === a.key)?.nav.order ?? 100;
      const orderB = plugins.find((item) => item.nav.key === b.key)?.nav.order ?? 100;
      return orderA - orderB;
    });
  return [...CORE_HEAD, ...pluginItems, ...CORE_TAIL];
}

export function shellActiveKey(pathname: string, items: readonly ShellNavItem[]): string {
  return items.find((item) => item.match(pathname))?.key ?? "chat";
}

/** 统一的移动端 ☰；桌面由 CSS 藏掉（沿用 theme.css 的 .btn.icon 纪律）。 */
export function ShellMenuButton() {
  const { drawerOpen, openDrawer, menuRef } = useAppShell();
  return (
    <button
      type="button"
      className="btn icon drawer-btn vault-burger shell-menu"
      ref={menuRef}
      aria-label="打开导航"
      aria-expanded={drawerOpen}
      onClick={openDrawer}
    >
      ☰
    </button>
  );
}

export function AppShell({ sidebar, topbar, mainClassName, children }: AppShellProps) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const me = useIdentity();
  const { plugins } = useSiteConfig();
  // 品牌字是部署期配置（SITE_NAME），后端 /api/site 给；这里不写死站名。
  const siteName = useSiteName();
  const nav = shellNavItems(plugins);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const asideRef = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  const closeDrawer = useCallback(() => {
    // 焦点归还交给下面的 [drawerOpen] effect：它跑在子组件（弹窗）的自动聚焦之后，
    // 这时才判断得出"焦点已经在弹窗里"，不会把弹窗的焦点抢回 ☰。
    setDrawerOpen(false);
  }, []);

  const openDrawer = useCallback(() => setDrawerOpen(true), []);

  // 抽屉开着时：Esc 关、Tab 围栏、焦点进抽屉（focus.ts 的同一套钩子）
  useFocusTrap(asideRef, drawerOpen, { onClose: closeDrawer, capture: true });

  // 换路由自动收抽屉（不抢焦点；页面/导航会自己把焦点放好）
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  // 关闭由点遮罩/导航/弹窗动作触发时把焦点还给 ☰；但**弹窗自己开着**时让弹窗
  // 收焦点（父 effect 在子 effect 之后跑，这时 activeElement 已经在弹窗输入框里）。
  useEffect(() => {
    if (wasOpen.current && !drawerOpen) {
      const a = document.activeElement;
      const inModal = a instanceof Element && a.closest(".modal") !== null;
      if (!inModal) menuRef.current?.focus();
    }
    wasOpen.current = drawerOpen;
  }, [drawerOpen]);

  const active = shellActiveKey(pathname, nav);
  const admin = me?.role === "admin";

  function logout() {
    void (async () => {
      try {
        await apiPost<{ ok: boolean }>("/api/logout");
      } catch {
        // 会话本来就没了也算退出成功
      }
      clearIdentity();
      navigate("/login", { replace: true });
    })();
  }

  const moduleLabel = nav.find((item) => item.key === active)?.label ?? "对话";
  return (
    <ShellContext.Provider value={{ drawerOpen, openDrawer, closeDrawer, menuRef }}>
      <div className={`shell${drawerOpen ? " shell-open" : ""}`}>
        {drawerOpen && (
          <div
            className="scrim vault-scrim shell-scrim"
            role="presentation"
            onClick={closeDrawer}
          />
        )}

        <aside
          ref={asideRef}
          className={`sb vault-side shell-side${drawerOpen ? " open" : ""}`}
          aria-label="全站导航"
          role={drawerOpen ? "dialog" : undefined}
          aria-modal={drawerOpen ? true : undefined}
        >
          <div className="shell-brand-row">
            <div className="brand shell-brand">
              <Link to="/" onClick={closeDrawer}>
                {siteName}
              </Link>
            </div>
            <button
              type="button"
              className="btn icon shell-close vault-close"
              aria-label="关闭导航"
              onClick={closeDrawer}
            >
              ×
            </button>
          </div>

          <nav className="shell-nav sb-modules" aria-label="模块导航">
            {nav.filter((item) => !item.admin || admin).map((item) => (
              <Link
                key={item.key}
                to={item.to}
                className={`shell-nav-item${active === item.key ? " on" : ""}`}
                aria-current={active === item.key ? "page" : undefined}
                onClick={closeDrawer}
              >
                <span
                  className="shell-nav-short"
                  aria-hidden="true"
                  data-short={item.short}
                />
                <span>{item.label}</span>
              </Link>
            ))}
          </nav>

          <div className="shell-divider" />

          <div className="shell-context">{sidebar}</div>

          <div className="shell-foot">
            <span className="shell-me" title={me?.name ?? ""}>
              {me?.name ?? ""} · {admin ? "管理员" : "成员"}
            </span>
            <button type="button" className="link shell-logout" onClick={logout}>
              退出
            </button>
          </div>
        </aside>

        <div className={`shell-main${mainClassName ? ` ${mainClassName}` : ""}`}>
          {topbar ?? (
            <div className="shell-topbar shell-topbar-default">
              <ShellMenuButton />
              <span className="shell-module-title">{moduleLabel}</span>
            </div>
          )}
          {children}
        </div>
      </div>
    </ShellContext.Provider>
  );
}
