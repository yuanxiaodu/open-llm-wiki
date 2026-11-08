// S12 全站统一外壳的源码钉子：所有已登录页面包同一个 AppShell，侧栏只有一份，
// 旧 AppHeader / 各模块整块侧栏不回来。
import { describe, expect, it } from "vitest";

import APPSHELL from "./AppShell.tsx?raw";
import SESSION_LIST from "./SessionList.tsx?raw";
import WIKI_SIDEBAR from "./WikiSidebar.tsx?raw";
import CHAT from "../pages/Chat.tsx?raw";
import WIKI_VAULT from "../pages/WikiVault.tsx?raw";
import APP from "../App.tsx?raw";
import CSS from "../vault.css?raw";

describe("AppShell：全站唯一外壳", () => {
  it("App 不再引用 AppHeader；chat/wiki 各自包 AppShell，日志/成员在路由里包", () => {
    expect(APP).not.toContain("AppHeader");
    expect(APP).toContain("<AuthOnly />");
    expect(APP).toContain('path="log"');
    expect(APP).toContain('path="members"');
    expect(APP).toContain('<AppShell mainClassName="paper-main">');
    for (const source of [CHAT, WIKI_VAULT]) {
      expect(source).toContain("<AppShell");
    }
  });

  it("侧栏：品牌 + 四项模块导航（日志/管理仅 admin）+ 上下文插槽 + 身份/退出", () => {
    expect(APPSHELL).toContain("export function AppShell");
    expect(APPSHELL).toContain("sidebar?: ReactNode");
    expect(APPSHELL).toContain('className="brand shell-brand"');
    expect(APPSHELL).toContain('className="shell-nav sb-modules"');
    expect(APPSHELL).toContain('label: "对话"');
    expect(APPSHELL).toContain('label: "wiki"');
    expect(APPSHELL).not.toContain('label: "文档"');
    expect(APPSHELL).toContain('label: "日志"');
    expect(APPSHELL).toContain('label: "管理"');
    expect(APPSHELL).toContain("!item.admin || admin");
    expect(APPSHELL).toContain('className="shell-context"');
    expect(APPSHELL).toContain('className="shell-foot"');
    expect(APPSHELL).toContain('onClick={logout}');
    // 2026-10-30 委托人：身份行不显示「· 仅使用」权限标签
    expect(APPSHELL).not.toContain("仅使用");
    expect(APPSHELL).not.toContain("canBuildWiki");
  });

  it("a11y 兼容别名 + 抽屉焦点/模态纪律只在 AppShell", () => {
    expect(APPSHELL).toContain('className={`sb vault-side shell-side');
    expect(APPSHELL).toContain('className="scrim vault-scrim shell-scrim"');
    expect(APPSHELL).toContain("drawer-btn vault-burger shell-menu");
    expect(APPSHELL).toContain("useFocusTrap(asideRef, drawerOpen, { onClose: closeDrawer, capture: true });");
    expect(APPSHELL).toContain('aria-label="全站导航"');
    expect(APPSHELL).toContain('aria-modal={drawerOpen ? true : undefined}');
    expect(APPSHELL).toContain("aria-current={active === item.key ? \"page\" : undefined}");
  });

  it("旧侧栏整块不回来：SessionList/wiki 都只交内容，没有第二份 aside/品牌/退出", () => {
    for (const source of [SESSION_LIST, WIKI_SIDEBAR]) {
      expect(source).not.toContain("<aside");
      expect(source).not.toContain("useFocusTrap(asideRef");
      expect(source).not.toContain("shell-brand");
      expect(source).not.toContain("onLogout");
    }
    expect(SESSION_LIST).toContain('className="sb-context"');
    expect(WIKI_SIDEBAR).toContain("<SidebarSearch");
  });

  it("统一 CSS：260px 侧栏、#f9f9f9/#fff、≤900 收同一个抽屉", () => {
    expect(CSS).toContain("grid-template-columns: 260px minmax(0, 1fr)");
    expect(CSS).toContain("background: #f9f9f9");
    expect(CSS).toContain("background: #ffffff");
    expect(CSS).toContain("@media (max-width: 900px)");
    expect(CSS).toContain(".shell-side.open");
    expect(CSS).toContain(".shell-topbar-default");
  });

  it("日志页成果行与提交行共用同一套网格，成果标签有独立样式（2026-10-30 合并）", () => {
    expect(CSS).toContain(".act-outcome");
    // 会话成果不再有自己的一套 .outcome-row 网格：它和提交行同一列宽。
    expect(CSS).not.toContain(".outcome-row");
    expect(CSS).toContain(".log-row");
  });
});
