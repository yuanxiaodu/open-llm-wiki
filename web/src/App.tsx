import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { apiGet, getIdentity, setIdentity } from "./api.ts";
import type { Identity } from "./api.ts";
import { AppShell } from "./components/AppShell.tsx";
import { useIdentity } from "./useIdentity.ts";
import { loadSiteConfig, useSiteConfig, useSiteConfigSettled } from "./site.ts";
import { pluginRoutes } from "./plugins.ts";
import type { PluginRoute } from "./plugin-types.ts";
import { Chat } from "./pages/Chat.tsx";
import { LogPage } from "./pages/LogPage.tsx";
import { Login } from "./pages/Login.tsx";
import { MembersPage } from "./pages/MembersPage.tsx";
import { Register } from "./pages/Register.tsx";
import { VersionsPage } from "./pages/VersionsPage.tsx";
import { SourcePage } from "./pages/SourcePage.tsx";
import { WikiIndex } from "./pages/WikiIndex.tsx";
import { WikiPage } from "./pages/WikiPage.tsx";
import { WikiVault } from "./pages/WikiVault.tsx";

// 路由表：
//   /login            登录页              —— 公开
//   /i/:token         邀请注册页          —— 公开（注册者本来就没身份）
//   /                 对话页（无会话）    —— 要身份
//   /chat             → /（旧地址兼容）
//   /chat/:id         对话页（带会话）    —— 要身份
//   /wiki             wiki 索引           —— 要身份
//   /wiki/versions?path=… 版本历史 + diff —— 要身份（只读）
//   /wiki/{path...}   wiki 页面           —— 要身份（path 不带 .md）
//   插件路由           由 /api/site 里启用的插件决定（构建期注册在 web/src/plugins.ts）
//   /log              改动日志            —— 仅管理员（非管理员由页面按 403 渲染人话页）
//   /members          管理与设置          —— 要身份（非管理员由页面按 403 渲染人话）
//   其余路径          一律回 /（登录守卫再决定去不去 /login）
//
// 已登录页面不再有全局顶栏：Chat/WikiVault 各自把内容包进 <AppShell>；日志/成员在
// 路由这里直接包。认证守卫只剩一条无 path 的布局路由 <AuthOnly/>。
//
// /wiki 与 /wiki/{path} 是嵌套路由：模块外壳（AppShell、目录数据）只挂一次，换页时
// 侧栏不重建。splat 路由（`*`）接带斜杠的 path，页面里再从 location.pathname 解出
// 相对 path（避开 URL 编码的坑）。/wiki/versions 是静态段，React Router 会优先于
// splat 匹配。

/** 已登录路由的守卫层：没有身份就整页去登录；有身份则渲染子路由（子页面自理 AppShell）。
 *
 * sessionStorage 是每个标签页一份：从对话里点 wiki 链接在新标签页打开时，cookie 还在、
 * 但 sessionStorage 是空的。所以**每次整页加载都真打一次 `/api/me`**：
 *   - 新标签页靠它用 cookie 换回身份，再放行；
 *   - 已有身份的标签页靠它刷新 role / can_build_wiki——管理员改了某人的建设权限或站点
 *     设置，对方刷新一下就该生效，不用等 sessionStorage（关标签页才清）过期。
 * 有身份时先按本地身份渲染（不让人对着空白屏等一个往返），拿到后写回 sessionStorage
 * 并重渲染；没有身份时才必须等这一趟。401 由 apiGet 整页跳 /login 兜底。
 */
function AuthOnly() {
  const identity = useIdentity();
  const [checking, setChecking] = useState(() => getIdentity() === null);

  useEffect(() => {
    let alive = true;
    apiGet<Identity>("/api/me")
      .then((fresh) => {
        if (!alive) return;
        setIdentity(fresh);
        setChecking(false);
      })
      .catch(() => {
        if (!alive) return;
        setChecking(false);
      });
    return () => { alive = false; };
  }, []);

  if (checking) return <div className="shell-boot" aria-busy="true" />;
  return identity ? <Outlet /> : <Navigate to="/login" replace />;
}

/** 把插件声明的路由树渲染成 React Router 的 <Route> 数组。 */
function renderPluginRoutes(routes: PluginRoute[]): ReactNode[] {
  return routes.map((route, index) => {
    const key = `${route.path ?? "index"}-${index}`;
    if (route.children !== undefined && route.children.length > 0) {
      return (
        <Route key={key} path={route.path} element={route.element}>
          {renderPluginRoutes(route.children)}
        </Route>
      );
    }
    if (route.index === true) {
      return <Route key={key} index element={route.element} />;
    }
    return <Route key={key} path={route.path} element={route.element} />;
  });
}

export function App() {
  // 站点配置（app.toml + SITE_NAME）整页加载时拉一次：store 会通知所有读者重渲染
  // （useSiteConfig/useSiteName），登录页 / 侧栏 / wiki 页 / 对话空态共用这一份。
  // 失败由 site.ts 静默吞掉——登录和对话绝不能因为展示信息没拉到而挂住或跳走。
  useEffect(() => {
    void loadSiteConfig();
  }, []);

  const { plugins } = useSiteConfig();
  const settled = useSiteConfigSettled();
  const pluginRouteNodes = plugins.flatMap((plugin) =>
    renderPluginRoutes(pluginRoutes(plugin.id)),
  );

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/i/:token" element={<Register />} />

        <Route element={<AuthOnly />}>
          <Route index element={<Chat />} />
          <Route path="chat" element={<Navigate to="/" replace />} />
          <Route path="chat/:sessionId" element={<Chat />} />
          <Route path="wiki" element={<WikiVault />}>
            <Route index element={<WikiIndex />} />
            <Route path="versions" element={<VersionsPage />} />
            <Route path="source" element={<SourcePage />} />
            <Route path="*" element={<WikiPage />} />
          </Route>
          {pluginRouteNodes}
          <Route
            path="log"
            element={
              <AppShell mainClassName="paper-main">
                <LogPage />
              </AppShell>
            }
          />
          <Route
            path="members"
            element={
              <AppShell mainClassName="paper-main">
                <MembersPage />
              </AppShell>
            }
          />
        </Route>

        {/* 配置还没回来时先等：直接打开插件深链（如插件页面）不应该被提前踢回 /。
            请求成功或失败都会 settled，之后未知路径才真正回首页。 */}
        <Route
          path="*"
          element={
            settled ? (
              <Navigate to="/" replace />
            ) : (
              <div className="shell-boot" aria-busy="true" />
            )
          }
        />
      </Routes>
    </BrowserRouter>
  );
}
