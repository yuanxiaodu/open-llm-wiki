// S5/S5b 前端的源码钉子（与 pages/Chat.test.ts、components/SessionList.test.ts 同一路数）。
//
// 没有 jsdom（也不为一个测试引依赖），所以能纯函数化的都抽去各自的 *.test.ts 了：
// wiki.ts / activity.ts / search.ts / members.ts / versions.ts / clipboard.ts。
// 这里只钉那些"跑不起来但承重"的形状：路由拓扑、全局顶栏（含管理员项）、只读纪律、
// 搜索高亮不用 innerHTML、成员页的权限兜底与复制回退、版本页的只读 diff。
//
// 为什么值得钉：
//   - 三卡首页取消后，`/` 必须是 Chat、`/chat` 必须重定向过去；改回去会让登录落点漂；
//   - 整站只读：多一颗「编辑/保存/回滚」按钮，人就会去点，点了才挨 403——界面在撒谎；
//   - 搜索高亮与复制链接都容易顺手写成 innerHTML / clipboard-only，那两样在 HTTP 部署
//     或 XSS 面上都会坏。
import { describe, expect, it } from "vitest";

import APP from "./App.tsx?raw";
import APPSHELL from "./components/AppShell.tsx?raw";
import CHAT from "./pages/Chat.tsx?raw";
import SESSION_LIST from "./components/SessionList.tsx?raw";
import MESSAGE_LIST from "./components/MessageList.tsx?raw";
import COMPOSER from "./components/Composer.tsx?raw";
import MODAL from "./components/Modal.tsx?raw";
import COPY_BUTTON from "./components/CopyButton.tsx?raw";
import WIKI_TREE from "./components/WikiTree.tsx?raw";
import WIKI_SIDEBAR from "./components/WikiSidebar.tsx?raw";
import MARKDOWN_BODY from "./components/MarkdownBody.tsx?raw";
import SIDEBAR_SEARCH from "./components/SidebarSearch.tsx?raw";
import LOG_PAGE from "./pages/LogPage.tsx?raw";
import MEMBERS_PAGE from "./pages/MembersPage.tsx?raw";
import VERSIONS_PAGE from "./pages/VersionsPage.tsx?raw";
import WIKI_VAULT from "./pages/WikiVault.tsx?raw";
import WIKI_INDEX from "./pages/WikiIndex.tsx?raw";
import WIKI_PAGE from "./pages/WikiPage.tsx?raw";
import LOGIN from "./pages/Login.tsx?raw";
import REGISTER from "./pages/Register.tsx?raw";
import API from "./api.ts?raw";
import USE_IDENTITY from "./useIdentity.ts?raw";

const UI_SOURCES = [
  APPSHELL,
  CHAT,
  SESSION_LIST,
  MESSAGE_LIST,
  COMPOSER,
  MODAL,
  COPY_BUTTON,
  WIKI_TREE,
  WIKI_SIDEBAR,
  MARKDOWN_BODY,
  SIDEBAR_SEARCH,
  LOG_PAGE,
  MEMBERS_PAGE,
  VERSIONS_PAGE,
  WIKI_VAULT,
  WIKI_INDEX,
  WIKI_PAGE,
];

describe("路由（App.tsx）：/ 即对话，旧地址与 catch-all 都回 /", () => {
  it("三卡首页已删除：/ 是 Chat，/chat 重定向 /", () => {
    expect(APP).not.toContain("Home");
    expect(APP).toMatch(/<Route index element=\{<Chat \/>\} \/>/);
    expect(APP).toMatch(/<Route path="chat" element=\{<Navigate to="\/" replace \/>\} \/>/);
    expect(APP).toContain('path="chat/:sessionId"');
  });

  it("新增 日志/成员/版本 路由；版本页在 /wiki 嵌套里（静态段优先于 splat）", () => {
    expect(APP).toContain('path="log"');
    expect(APP).not.toContain('path="search"');
    expect(APP).toContain('path="members"');
    expect(APP).toMatch(/<Route path="versions" element=\{<VersionsPage \/>\} \/>/);
    expect(APP).toMatch(/<Route path="\*" element=\{<WikiPage \/>\} \/>/);
  });

  it("catch-all → /；登录落 /；插件深链等配置 settled", () => {
    expect(APP).not.toContain("/demo/_outline_");
    expect(APP).toContain('<Navigate to="/" replace />');
    expect(APP).toContain("useSiteConfigSettled");
    expect(APP).toContain("pluginRoutes");
    expect(LOGIN).toMatch(/navigate\("\/", \{ replace: true \}\)/);
  });

  it("每次整页加载都真打 /api/me 刷新身份，并写回 sessionStorage", () => {
    expect(APP).toContain('apiGet<Identity>("/api/me")');
    expect(APP).toContain("setIdentity(fresh);");
    // 有身份时不再"跳过这一趟"（权限可能被管理员改过）
    expect(APP).not.toContain("if (getIdentity() !== null) {");
    // 身份走订阅：刷新写回后页面跟着重渲染，不会停在旧权限上
    expect(APP).toContain("const identity = useIdentity();");
    expect(USE_IDENTITY).toContain("useSyncExternalStore(subscribeIdentity, getIdentity, getIdentity)");
    expect(API).toContain("export function subscribeIdentity(");
    expect(APPSHELL).toContain("const me = useIdentity();");
    expect(CHAT).toContain("const me = useIdentity();");
  });

  it("登录/注册的响应写进身份时带 can_build_wiki", () => {
    expect(LOGIN).toContain("can_build_wiki: me.can_build_wiki !== false");
    expect(REGISTER).toContain("can_build_wiki: me.can_build_wiki !== false");
  });
});

describe("全站侧栏（AppShell）：核心四项、管理员项、插件项、aria-current", () => {
  it("核心四项：对话(/)、wiki、日志与管理（日志/管理仅 admin）；插件由 /api/site 追加", () => {
    expect(APPSHELL).toContain('to: "/"');
    expect(APPSHELL).toContain('to: "/wiki"');
    expect(APPSHELL).toContain('to: "/log"');
    expect(APPSHELL).toContain('to: "/members"');
    expect(APPSHELL).not.toContain('to: "/demo"');
    expect(APPSHELL).toContain("shellNavItems");
    expect(APPSHELL).toContain("plugins");
    expect(APPSHELL).not.toContain('to: "/search"');
    expect(APPSHELL).not.toContain("/ingest");
    expect((APPSHELL.match(/key: "/g) ?? []).length).toBe(4);
    expect(APPSHELL).toContain("admin?: boolean");
    expect(APPSHELL).toContain("!item.admin || admin");
    expect(APPSHELL).toContain('me?.role === "admin"');
    expect(APPSHELL).toContain('label: "日志"');
    expect(APPSHELL).toContain('label: "管理"');
  });

  it("aria-current 按当前路由；/wiki/versions 也算 wiki", () => {
    expect(APPSHELL).toContain('aria-current={active === item.key ? "page" : undefined}');
    expect(APPSHELL).toContain('startsWith("/wiki")');
    expect(APPSHELL).toContain('startsWith("/log")');
    expect(APPSHELL).toContain('startsWith("/members")');
  });

  it("侧栏是唯一外壳：品牌 + 模块导航 + 身份/退出 + 一个抽屉", () => {
    expect(APPSHELL).toContain('className={`sb vault-side shell-side');
    expect(APPSHELL).toContain('className="brand shell-brand"');
    expect(APPSHELL).toContain('className="shell-nav sb-modules"');
    expect(APPSHELL).toContain('className="scrim vault-scrim shell-scrim"');
    expect(APPSHELL).toContain("useFocusTrap(asideRef, drawerOpen");
    expect(APPSHELL).toContain('aria-modal={drawerOpen ? true : undefined}');
    expect(APPSHELL).toContain("drawer-btn vault-burger shell-menu");
    expect(APPSHELL).toContain("shell-foot");
  });
});

describe("只读纪律：没有任何写入口，也不直接刷 HTML", () => {
  it("所有前端界面文件里都没有 dangerouslySetInnerHTML", () => {
    for (const source of UI_SOURCES) {
      expect(source).not.toMatch(/dangerouslySetInnerHTML\s*[:=]/);
    }
  });

  it("wiki/版本页没有编辑/保存/回滚/删除/导入把手", () => {
    for (const source of [WIKI_INDEX, WIKI_PAGE, VERSIONS_PAGE]) {
      expect(source).not.toMatch(/编辑这一页|保存这一页|回滚到|删除这一页|导入这一页/);
    }
    // 版本页明确只读；WikiPage 的入口只叫「版本历史」
    expect(VERSIONS_PAGE).toContain("只读");
  });
});

describe("对话页（S8 DeepSeek 式工作区）", () => {
  it("全站统一 AppShell，已无全局顶栏；Chat 用自定义会话条", () => {
    expect(APP).not.toContain("AppHeader");
    expect(APP).toContain("<AuthOnly />");
    // 新标签页从 cookie 恢复身份：sessionStorage 空时走 /api/me
    expect(APP).toContain('apiGet<Identity>("/api/me")');
    expect(APP).toContain("setIdentity(fresh)");
    expect(CHAT).toContain("<AppShell");
    expect(CHAT).toContain('mainClassName="chat"');
    expect(CHAT).toContain("topbar={head}");
    expect(CHAT).toContain("<SessionList");
    expect(CHAT).not.toContain("className=\"chat\"");
  });

  it("A11y/CDP 依赖的 class 与属性都还在", () => {
    expect(CHAT).toContain('mainClassName="chat"');
    expect(CHAT).toContain('className="main"');
    expect(APPSHELL).toContain("drawer-btn");
    expect(APPSHELL).toContain("aria-expanded={drawerOpen}");
    expect(APPSHELL).toContain('className="scrim vault-scrim shell-scrim"');
    expect(CHAT).toContain("<Composer");
    expect(COMPOSER).toContain('id="composer-text"');
    expect(COMPOSER).toContain('className={`composer${disabled ? " off" : ""}${dragging ? " dragging" : ""}`}');
    expect(MESSAGE_LIST).toContain('className="msg user"');
    expect(MESSAGE_LIST).toContain('className="msg agent"');
    expect(MESSAGE_LIST).toContain('className="msg agent live"');
    expect(MESSAGE_LIST).toContain('aria-live="polite"');
    expect(MESSAGE_LIST).toContain("data-live=\"1\"");
    expect(APPSHELL).toContain('className={`sb vault-side shell-side');
  });

  it("侧栏模块入口在 AppShell：wiki/日志，管理员多日志与成员；不再有搜索", () => {
    expect(APPSHELL).toContain('className="shell-nav sb-modules"');
    for (const to of ["/wiki", "/log"]) {
      expect(APPSHELL).toContain(`to: "${to}"`);
    }
    expect(APPSHELL).toContain("item.admin");
    expect(APPSHELL).toContain('aria-label="模块导航"');
    expect(APPSHELL).not.toContain("/search");
    expect(SESSION_LIST).not.toContain("sb-modules");
  });

  it("会话栏有 ☰/标题/pill/⋯ 菜单；⋯ 只对会话归属者显示", () => {
    expect(CHAT).toContain("head-more");
    expect(CHAT).toContain('aria-label="会话操作"');
    expect(CHAT).toContain('aria-haspopup="menu"');
    expect(CHAT).toContain("detail.user_id === me.id");
    expect(CHAT).toContain("<ShellMenuButton />");
  });

  it("消息排版：用户气泡 + 附件在外、智能体无气泡、流式轻量 pill", () => {
    expect(MESSAGE_LIST).toMatch(/<div className="msg user" key=\{m\.id\}>\s*<div className="user-col">/);
    expect(MESSAGE_LIST).toContain('className="who-user"');
    expect(MESSAGE_LIST).toMatch(
      /\{m\.content\.trim\(\) !== "" && \(\s*<div className="b">\s*<div className="text">\{m\.content\}<\/div>\s*<\/div>\s*\)\}\s*\{m\.attachments\.length > 0 && \(\s*<div className="chips">/,
    );
    expect(MESSAGE_LIST).toContain('className="dot" aria-hidden="true"');
    expect(MESSAGE_LIST).toContain('{phaseOf(live) ?? "正在写"}');
  });

  it("空态欢迎：大标题 + 3 个建议 chip，复用 ChatWelcome", () => {
    expect(MESSAGE_LIST).toContain("<ChatWelcome");
    expect(MESSAGE_LIST).toContain("onFeed={onFeed}");
    expect(MESSAGE_LIST).toContain("onDoctor={onDoctor}");
    expect(CHAT).toContain("<ChatWelcome");
    expect(CHAT).toContain("onSuggest={suggest}");
    expect(CHAT).toContain("onFeed={feed}");
  });

  it("对话页不暴露「本次变更」：outcome 块跳过，工具卡与「依据」保留", () => {
    // 卡没了，但 outcome 帧照旧解析（sse.ts/blocks.ts 的兼容不动，只在这里不渲染）
    expect(MESSAGE_LIST).not.toContain("OutcomeCard");
    expect(MESSAGE_LIST).not.toContain("本次变更");
    expect(MESSAGE_LIST).toContain('if (b.kind === "outcome")');
    expect(MESSAGE_LIST).toContain("<ToolCard");
    expect(MESSAGE_LIST).toContain('aria-label="依据页"');
    // 「修一下」整套动作跟着成果卡一起退场
    expect(CHAT).not.toContain("repairLint");
    expect(CHAT).not.toContain("onRepair");
  });
});

describe("模块内搜索（wiki 一份，全局搜索已删）", () => {
  it("全局 /search 页面与入口都清掉，/search 回落 /（catch-all）", () => {
    expect(APP).not.toContain("SearchPage");
    expect(APP).not.toContain('path="search"');
    expect(APP).toContain('<Navigate to="/" replace />');
    expect(APPSHELL).not.toContain("/search");
    expect(SESSION_LIST).not.toContain("/search");
  });

  it("wiki 侧栏：aria-label=搜索 wiki、250ms 防抖、只取 /api/search 的 pages 组", () => {
    expect(WIKI_SIDEBAR).toContain('ariaLabel="搜索 wiki"');
    expect(WIKI_SIDEBAR).toContain('placeholder="搜索 wiki"');
    expect(WIKI_SIDEBAR).toContain('`/api/search?q=${encodeURIComponent(q)}`');
    expect(WIKI_SIDEBAR).toContain("normalizeSearch(raw).pages");
    expect(WIKI_SIDEBAR).toContain("searchPageHref");
    expect(WIKI_SIDEBAR).toContain("<SidebarSearch");
  });

  it("搜索组件：250ms 防抖、≤20 条、Esc 清空、高亮用 React 节点（不 innerHTML）", () => {
    expect(SIDEBAR_SEARCH).toContain("window.setTimeout");
    expect(SIDEBAR_SEARCH).toContain("}, 250);");
    expect(SIDEBAR_SEARCH).toContain(".slice(0, 20)");
    expect(SIDEBAR_SEARCH).toContain('e.key === "Escape"');
    expect(SIDEBAR_SEARCH).toContain("splitSnippet");
    expect(SIDEBAR_SEARCH).toContain("<mark");
    expect(SIDEBAR_SEARCH).not.toMatch(/dangerouslySetInnerHTML\s*[:=]/);
    expect(SIDEBAR_SEARCH).toContain("没有找到");
  });

  it("术语：侧栏/树用 wiki；插件自己的模块名由插件 nav 提供", () => {
    expect(APPSHELL).toContain('label: "wiki"');
    expect(WIKI_TREE).toContain('label = "wiki 结构"');
  });
});

describe("日志页（/log，仅管理员）", () => {
  it("取 /api/log + /api/wiki + /api/log/outcomes，按天分组、中文 action/actor", () => {
    expect(LOG_PAGE).toContain('apiGet<unknown>("/api/log")');
    expect(LOG_PAGE).toContain('apiGet<unknown>("/api/wiki")');
    expect(LOG_PAGE).toContain('apiGet<unknown>("/api/log/outcomes")');
    expect(LOG_PAGE).toContain("groupLogItemsByDay");
    expect(LOG_PAGE).toContain("actionLabel");
    expect(LOG_PAGE).toContain("actorLabel");
    expect(LOG_PAGE).toContain("提交记录仅最近 200 条");
  });

  it("403 渲染人话页（真打接口拿 403，不只信 sessionStorage.role）", () => {
    expect(LOG_PAGE).toContain(".status === 403");
    expect(LOG_PAGE).toContain("只有管理员能看这一块");
    expect(LOG_PAGE).toContain("回对话");
  });

  it("提交与会话成果合并成一条时间线（不再分「会话成果」独立区块）", () => {
    expect(LOG_PAGE).toContain("mergeLogItems(entries, outcomes)");
    expect(LOG_PAGE).toContain('item.kind === "outcome"');
    expect(LOG_PAGE).toContain("act-outcome");
    expect(LOG_PAGE).toContain("/chat/${o.id}");
    expect(LOG_PAGE).toContain("EMPTY_TITLE");
    expect(LOG_PAGE).toContain("outcomeSummary");
    // 成果不再单独摆一个 section：整页只有一个按天分组的时间线
    expect(LOG_PAGE).not.toContain('aria-labelledby="outcomes-title"');
    expect(LOG_PAGE.match(/groupLogItemsByDay\(/g)).toHaveLength(1);
  });

  it("slug 能在页面列表里找到才给链接", () => {
    expect(LOG_PAGE).toContain("findPage(pages, entry.slug)");
    expect(LOG_PAGE).toContain("wikiPageHref");
  });
});

describe("管理与设置页（/members，仅管理员）", () => {
  it("403 渲染人话页（真打接口，不只靠 sessionStorage.role）", () => {
    expect(MEMBERS_PAGE).toContain('apiGet<unknown>("/api/members")');
    expect(MEMBERS_PAGE).toContain('apiGet<unknown>("/api/admin/settings")');
    expect(MEMBERS_PAGE).toContain(".status === 403");
    expect(MEMBERS_PAGE).toContain("只有管理员能看这一块");
    expect(MEMBERS_PAGE).toContain("管理与设置");
  });

  it("站点设置：GET/PATCH chat_visibility，单选 all/own + 保存按钮", () => {
    expect(MEMBERS_PAGE).toContain('apiPatch<unknown>("/api/admin/settings"');
    expect(MEMBERS_PAGE).toContain("normalizeSiteSettings");
    expect(MEMBERS_PAGE).toContain('name="chat_visibility"');
    expect(MEMBERS_PAGE).toContain('value="all"');
    expect(MEMBERS_PAGE).toContain('value="own"');
    expect(MEMBERS_PAGE).toContain("管理员始终能看到全部会话");
    expect(MEMBERS_PAGE).toContain("保存设置");
  });

  it("成员权限开关：PATCH /api/members/{id}/permissions；失败回滚并挂横幅", () => {
    expect(MEMBERS_PAGE).toContain('apiPatch<{ ok: boolean }>(`/api/members/${member.id}/permissions`');
    expect(MEMBERS_PAGE).toContain("can_build_wiki: next");
    expect(MEMBERS_PAGE).toContain("withPermission(prev, member.id, member.can_build_wiki)");
    expect(MEMBERS_PAGE).toContain("全部权限");
    expect(MEMBERS_PAGE).toContain("可建设 wiki");
  });

  it("邀请只保留一张表：数据来自 /api/members，不再调 reset-invites", () => {
    expect(MEMBERS_PAGE).not.toContain("reset-invites");
    expect(MEMBERS_PAGE).toContain("invitePurposeLabel");
    expect(MEMBERS_PAGE).toContain("用途");
    expect(MEMBERS_PAGE).toContain("首次点开");
    expect(MEMBERS_PAGE).toContain("inviteState");
  });

  it("改名/新建邀请用 FormData；停用/重置/作废是 POST", () => {
    expect(MEMBERS_PAGE).toContain("apiPostForm");
    expect(MEMBERS_PAGE).toMatch(/form\.set\("name"/);
    expect(MEMBERS_PAGE).toContain('apiPost<{ ok: boolean }>(`/api/members/${modal.member.id}/remove`)');
    expect(MEMBERS_PAGE).toContain("reset-login");
    expect(MEMBERS_PAGE).toContain("/revoke");
  });

  it("一次性链接 + 复制回退；所有写操作走 Modal 确认", () => {
    expect(MEMBERS_PAGE).toContain("tokenPanel");
    expect(MEMBERS_PAGE).toContain("CopyButton");
    expect(MEMBERS_PAGE).toContain("inviteUrl");
    expect(MEMBERS_PAGE).toContain("<Modal");
    expect(COPY_BUTTON).toContain("copyText");
    expect(MODAL).toContain("useFocusTrap");
    expect(MODAL).toContain('role="dialog"');
  });
});

describe("版本页（/wiki/versions）", () => {
  it("path 只接受 /api/wiki 列表里真存在的页；versions/diff 拼对", () => {
    expect(VERSIONS_PAGE).toContain("findWikiPage");
    expect(VERSIONS_PAGE).toContain("/versions`");
    expect(VERSIONS_PAGE).toContain("/diff?a=");
    expect(VERSIONS_PAGE).toContain("pairFromSelected");
  });

  it("diff 染色走 parseDiffLines；非 unified diff 原样 <pre>", () => {
    expect(VERSIONS_PAGE).toContain("parseDiffLines");
    expect(VERSIONS_PAGE).toContain("isUnifiedDiff");
    expect(VERSIONS_PAGE).toContain("diff-line");
    expect(VERSIONS_PAGE).toContain("diff-raw");
  });

  it("WikiPage 有且只有「版本历史」这一个入口", () => {
    expect(WIKI_PAGE).toContain("/wiki/versions?path=");
    expect(WIKI_PAGE).toContain("版本历史");
  });
});

describe("wiki / 引注的既有形状没被改坏", () => {
  it("wiki 侧栏仍用物理树；正文仍 prepareWikiBody + 页级来源", () => {
    expect(WIKI_SIDEBAR).toMatch(/buildWikiTree\(pages, type_labels\)/);
    expect(WIKI_PAGE).toContain("prepareWikiBody(");
    expect(WIKI_PAGE).toContain("来源");
    expect(WIKI_PAGE).not.toContain("CiteRail");
    expect(WIKI_INDEX).toContain("MarkdownBody");
  });

  it("目录树 a11y 保持", () => {
    expect(WIKI_TREE).toContain("aria-expanded={open}");
    expect(WIKI_TREE).toContain('aria-current={current ? "page" : undefined}');
  });
});

describe("markdown 表格不设高度上限（2026-11-08 表格压住下文）", () => {
  it("两处 Streamdown 都传 tableMaxHeight={0}", () => {
    // streamdown 默认给表格外壳套 max-height:300px，其 overflow 靠 Tailwind 类；本仓库
    // 不引 Tailwind，壳被压扁而表格照旧按内容高度画，多出来的部分就压到后面段落上。
    // 0 = 关掉上限（streamdown 文档口径）；横向滚动仍由 theme.css 的 .md table 负责。
    expect(MESSAGE_LIST).toContain("tableMaxHeight={0}");
    expect(MARKDOWN_BODY).toContain("tableMaxHeight={0}");
  });
});

describe("知识库动作入口", () => {
  it("wiki 索引只提供浏览和会话入口", () => {
    expect(WIKI_INDEX).not.toContain("体检知识库");
    expect(WIKI_INDEX).not.toContain("让智能体整理");
    expect(WIKI_INDEX).not.toContain("WikiHealth");
    expect(WIKI_INDEX).toContain('to="/chat"');
  });
});
