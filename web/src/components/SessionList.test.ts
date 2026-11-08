// 会话侧栏的钉子。
//
// 四组：
//   1. groupSessions（T5 评审 M8）：分组的**键**是归属者的 user_id，显示出来的组名才是
//      owner_name。旧实现按名字分组，两个同名的人会被并成一组（会话串到别人名下），
//      而且 React 的 key 会撞——撞 key 的后果是切会话时条目的状态串位。
//   2. focusAfterModal（T6b fix round 1 第 2 条）：弹窗关掉后焦点回 ⋯ 还是回 ☰。
//      两件控件在不在屏幕上要看 DOM，所以那半步留在组件里（canTakeFocus），**决策**
//      抽成纯函数，于是没有 jsdom 也能把 8 种组合全钉住。
//   3. 焦点管理的纪律（fix round 1 第 1/2/4 条）长在 effect 与 JSX 里，要 React +
//      真 DOM 才跑得起来（没有 jsdom，也不为一个测试引依赖），所以这里**钉源码**——
//      与 tests/test_api_wiring.py 钉 main.py 装配同一路数。S4-T2 之后围栏那一份住在
//      focus.ts 的 useFocusTrap 里（抽屉与弹窗共用），钉子跟着分成了"钩子里的纪律"与
//      "两个调用点交对了参数"两半。源码是用 vite 的 `?raw` 读进来的（这个工程没装 @types/node）。
//   4. tabMove（R-S4-3）：Tab 那一下焦点挪去哪儿的**决策**同样抽成了纯函数，于是
//      "浮层里零可聚焦元素"那一档（归档弹窗提交中）不用 DOM 也能真跑断言，不只是钉字符串。
//
// 真行为那一面由 CDP 断言在真 Chrome 上跑：`scripts/web-a11y-check.mjs`（S4-T2 入库的那一份，
// 390×844 与 1280×900 两种视口，抽屉/弹窗/流式三组焦点与 aria 断言）。S3 当时的输出记在
// .superpowers/sdd/2026-10-23-s3-frontend-chat/task-6-report.md 的「fix round 1」。
import { describe, expect, it } from "vitest";

import { focusAfterModal, groupSessions, splitArchived } from "./SessionList.tsx";
import { tabMove } from "../focus.ts";
import type { SessionSummary } from "../types.ts";
import LIST from "./SessionList.tsx?raw";
import FOCUS from "../focus.ts?raw";
import CHAT from "../pages/Chat.tsx?raw";
import APPSHELL from "./AppShell.tsx?raw";

/** 一条会话摘要；只关心分组用到的四个键，其余填常量。 */
function s(id: number, userId: number, ownerName: string, title = `会话${id}`): SessionSummary {
  return {
    id,
    user_id: userId,
    title,
    status: "active",
    started_at: "2026-10-23T10:00:00+00:00",
    last_message_at: null,
    owner_name: ownerName,
  };
}

describe("groupSessions：键是 user_id，label 是 owner_name", () => {
  it("我的组恒在最上，key 是我的 id", () => {
    const groups = groupSessions([s(1, 9, "别人"), s(2, 7, "我")], 7);
    expect(groups[0]).toMatchObject({ key: 7, label: "我的会话", mine: true });
    expect(groups[0]!.items.map((x) => x.id)).toEqual([2]);
    expect(groups[1]).toMatchObject({ key: 9, label: "别人", mine: false });
  });

  it("同名的两个人是两组（label 看着一样，key 不一样，会话不串）", () => {
    const groups = groupSessions([s(1, 9, "小林"), s(2, 12, "小林")], 7);
    expect(groups).toHaveLength(2);
    expect(groups.map((g) => g.label)).toEqual(["小林", "小林"]);
    expect(groups.map((g) => g.key)).toEqual([9, 12]);
    expect(groups[0]!.items.map((x) => x.id)).toEqual([1]);
    expect(groups[1]!.items.map((x) => x.id)).toEqual([2]);
    // React 拿 key 当唯一标识：这里必须两两不同，否则渲染会串位
    expect(new Set(groups.map((g) => g.key)).size).toBe(groups.length);
  });

  it("同一个人改了名字，仍然是**一组**（id 没变）", () => {
    // 后端一次响应里 owner_name 是同一个值，这条钉的是"键不跟着名字走"这件事：
    // 哪怕列表里出现了两个写法（改名前后各一半），也并按 id 归一组，label 取首次出现的。
    const mixed = [s(1, 9, "小林"), s(2, 9, "小林（改名前）")];
    const groups = groupSessions(mixed, 7);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.key).toBe(9);
    expect(groups[0]!.label).toBe("小林");
    expect(groups[0]!.items.map((x) => x.id)).toEqual([1, 2]);
  });

  it("其余组按首次出现的先后排，组内顺序原样保留（后端已按最近动静排过）", () => {
    const groups = groupSessions(
      [s(1, 9, "甲"), s(2, 12, "乙"), s(3, 9, "甲"), s(4, 15, "丙"), s(5, 12, "乙")],
      7,
    );
    expect(groups.map((g) => g.key)).toEqual([9, 12, 15]);
    expect(groups[0]!.items.map((x) => x.id)).toEqual([1, 3]);
    expect(groups[1]!.items.map((x) => x.id)).toEqual([2, 5]);
  });

  it("owner_name 是空串时给一句人话，key 仍然是 id", () => {
    const groups = groupSessions([s(1, 9, "")], 7);
    expect(groups[0]).toMatchObject({ key: 9, label: "（没有名字）" });
  });

  it("没有会话 → 没有组；只有我的会话 → 只有一组", () => {
    expect(groupSessions([], 7)).toEqual([]);
    const only = groupSessions([s(1, 7, "我"), s(2, 7, "我")], 7);
    expect(only).toHaveLength(1);
    expect(only[0]!.mine).toBe(true);
    expect(only[0]!.items).toHaveLength(2);
  });
});

describe("focusAfterModal：弹窗关掉后焦点回 ⋯ 还是回 ☰", () => {
  it("宽屏（不是从抽屉里开的）：⋯ 在屏幕上就还给它", () => {
    expect(focusAfterModal(false, true, true)).toBe("more");
    expect(focusAfterModal(false, true, false)).toBe("more");
  });

  it("宽屏但 ⋯ 已经不在了（会话刚从列表里消失）：退到 ☰，☰ 也不在就落回 body", () => {
    expect(focusAfterModal(false, false, true)).toBe("trigger");
    expect(focusAfterModal(false, false, false)).toBe(null);
  });

  it("从抽屉里开的（≤700px，抽屉已经关了、⋯ 滑到画布外）：优先回 ☰", () => {
    // ☰ 与 ⋯ 都"可用"时也选 ☰：抽屉一关 ⋯ 就要滑出屏幕（0.18s 的 transform 过渡里
    // 它可能还压着视口边缘），把焦点交给一个正在离场的元素等于把键盘用户扔进抽屉。
    expect(focusAfterModal(true, true, true)).toBe("trigger");
    expect(focusAfterModal(true, false, true)).toBe("trigger");
  });

  it("从抽屉里开的但 ☰ 不可用（窄屏拉宽到 >700px 的边角）：退回 ⋯，都不行才 null", () => {
    expect(focusAfterModal(true, true, false)).toBe("more");
    expect(focusAfterModal(true, false, false)).toBe(null);
  });

  it("8 种组合都有一个明确答案（没有 undefined 漏出去）", () => {
    const all = [true, false].flatMap((v) =>
      [true, false].flatMap((m) =>
        [true, false].map((t) => focusAfterModal(v, m, t)),
      ),
    );
    expect(all).toHaveLength(8);
    for (const x of all) expect(x === "more" || x === "trigger" || x === null).toBe(true);
  });
});

describe("焦点管理的纪律（源码钉子，T6b fix round 1 + S4-T2 的泛化）", () => {
  it("陷阱只在 false→true 那一下聚焦，绝不每渲染一次就抢一次（focus.ts）", () => {
    // 承重的两行：prevActive 记上一次的值，focus() 只在 justOpened 时调
    expect(FOCUS).toContain("const justOpened = active && !prevActive.current;");
    expect(FOCUS).toContain("prevActive.current = active;");
    expect(FOCUS).toContain("if (justOpened && latest.current.autoFocus) {");
    expect(FOCUS).toContain("const first = root?.querySelector<HTMLElement>(FOCUSABLE) ?? null;");
    expect(FOCUS).toContain("if (first !== null) first.focus();");
    // 一件可聚焦的都没有 → 落浮层根，而不是把焦点留在 body 上（R-S4-3）
    expect(FOCUS).toContain("else focusBox(root);");
    // 整个 focus.ts 里"找浮层第一件"只此一处（Tab 围栏用的是 querySelectorAll，另一回事）
    expect(FOCUS.match(/querySelector<HTMLElement>\(FOCUSABLE\)/g)).toHaveLength(1);
    // 回调走 ref、依赖数组里只有 active 与 ref：调用方传内联箭头也不会让 effect 每渲染
    // 一次就重跑一次（流式期间每帧都渲染，重跑一趟就 focus 一趟）
    expect(FOCUS).toContain("latest.current = { onClose, autoFocus, capture };");
    expect(FOCUS).toMatch(/\}, \[active, ref\]\);\s*\}\s*$/);
    expect(FOCUS).toContain("latest.current.onClose?.();");
  });

  it("抽屉的三件事（聚焦/Esc/Tab 围栏）交给 AppShell，SessionList 只管弹窗", () => {
    expect(APPSHELL).toContain(
      "useFocusTrap(asideRef, drawerOpen, { onClose: closeDrawer, capture: true });",
    );
    expect(APPSHELL).toContain('import { useFocusTrap } from "../focus.ts";');
    // 自己写的围栏不许留半份：两处围栏迟早漂成两套口径
    expect(LIST).not.toContain("FOCUSABLE");
    expect(LIST).not.toContain("e.shiftKey");
    expect(LIST).not.toContain("prevOpen");
    // capture 那一档是抽屉独有的（条目菜单的 Esc 还在冒泡阶段听着）
    expect(FOCUS).toContain("if (useCapture) e.stopPropagation();");
  });

  it("弹窗用同一个钩子：Esc 关 + Tab 围栏，而「焦点落哪儿」仍由弹窗自己定", () => {
    expect(LIST).toContain(
      "useFocusTrap(cardRef, true, { onClose: busy ? undefined : onClose, autoFocus: false });",
    );
    // busy 时不响应 Esc（半截关掉会把在飞的那一趟留下），围栏照旧装着
    expect(LIST).toContain("onClose: busy ? undefined : onClose");
    // 陷阱要的那个根挂在弹窗卡片上（不是遮罩）
    expect(LIST).toContain("ref={cardRef}");
    expect(LIST).toContain('const cardRef = useRef<HTMLDivElement>(null);');
  });

  it("SessionModal 挂了 key：换一种弹窗、换一场会话，都整个重挂载", () => {
    // 不挂 key 时 React 复用同一个实例：标题输入框里留着上一场会话的名字（useState 的
    // 初值只在挂载时读一次），焦点也不会重新走"改名进输入框并全选 / 归档落取消"那一下
    expect(LIST).toContain("key={`${modal.kind}:${modal.session.id}`}");
  });

  it("AppShell 的 closeDrawer 是稳定引用，焦点回 ☰", () => {
    expect(APPSHELL).toContain("const closeDrawer = useCallback(");
    expect(APPSHELL).toContain("menuRef.current?.focus();");
    expect(APPSHELL).toContain("const [drawerOpen, setDrawerOpen] = useState(false);");
    expect(CHAT).not.toContain("useState(false);\n\n  const abortRef");
  });

  it("从 ⋯ 菜单开弹窗之前先把抽屉关上（两层模态浮层不许叠着）", () => {
    // 改名与归档两处：closeDrawer() 紧跟着 setModal(...)，中间不许有别的东西
    expect(
      LIST.match(
        /closeDrawer\(\);\s+setModal\(\{ kind: "(?:rename|archive)", session: s, viaDrawer: drawerOpen \}\);/g,
      ),
    ).toHaveLength(2);
  });

  it("弹窗关掉时按 focusAfterModal 还焦点，并且只在控件真能接焦点时才 focus", () => {
    expect(LIST).toContain("const which = focusAfterModal(");
    expect(LIST).toContain('if (which === "more") more?.focus();');
    expect(LIST).toContain('else if (which === "trigger") trigger?.focus();');
    // 可见性判据是 rect 与视口的交集（画布外的抽屉 offsetParent 照样非 null，判不出来）
    expect(LIST).toContain(
      "return r.right > 0 && r.left < window.innerWidth && r.bottom > 0 && r.top < window.innerHeight;",
    );
  });

  it("两种弹窗都自己接住焦点（归档那支没有输入框，落「取消」）", () => {
    expect(LIST).toContain("cancelRef.current?.focus();");
    expect(LIST).toContain("cancelRef");
    // AppShell 看见焦点已经在弹窗里就不抢，否则 ☰ 会把弹窗的焦点拽走
    expect(APPSHELL).toContain(`a.closest(".modal") !== null`);
    expect(APPSHELL).toContain("menuRef.current?.focus();");
  });

  it("抽屉展开时才挂 role=dialog + aria-modal=true（在 AppShell 的 aside 上）", () => {
    expect(APPSHELL).toContain('role={drawerOpen ? "dialog" : undefined}');
    expect(APPSHELL).toContain("aria-modal={drawerOpen ? true : undefined}");
    // 那两个属性确实长在 <aside> 上（不是弹窗那一层）
    expect(APPSHELL).toMatch(/<aside[\s\S]{0,600}role=\{drawerOpen \? "dialog" : undefined\}/);
    // 写死的 role="dialog" 只剩弹窗卡片那一处：宽屏侧栏常驻可见，无条件挂
    // aria-modal="true" 等于宣布主内容不可读
    expect((LIST.match(/role="dialog"/g) ?? []).length + (APPSHELL.match(/role="dialog"/g) ?? []).length).toBe(1);
  });
});

// R-S4-3：浮层里**一件可聚焦的东西都没有**时，Tab 不许走到遮罩后面去。
// 这一档是真能走到的：归档弹窗提交中（busy），「取消」与「确认归档」两颗钮一起 disabled，
// 而 FOCUSABLE 排除 disabled —— 浏览器把焦点扔回 body，下一次 Tab 从文档开头往下找，
// 第一个能停下的就是背景页上的东西（侧栏条目 / ☰ / 输入框）。原来那一档是
// `if (items.length === 0) return;`：围栏什么都不做，等于放行给浏览器。
// 判断抽成了纯函数 tabMove（focus.ts），所以"零可聚焦"这一档不用 DOM 也能真断言。
describe("R-S4-3：零可聚焦元素时焦点按在浮层根上", () => {
  it("focusables === 0 → hold：Tab / Shift+Tab 一样，焦点在不在浮层里也一样", () => {
    for (const shift of [false, true]) {
      for (const inside of [false, true]) {
        expect(
          tabMove({
            focusables: 0,
            inside,
            shift,
            onRoot: inside,
            atFirst: false,
            atLast: false,
          }),
        ).toBe("hold");
      }
    }
    // 焦点还停在"刚刚 disabled 掉的那颗钮"上（浏览器 blur 之前的那一瞬）也一样：
    // inside/atFirst/atLast 全真也不许放行给浏览器
    expect(
      tabMove({ focusables: 0, inside: true, shift: false, onRoot: false, atFirst: true, atLast: true }),
    ).toBe("hold");
    expect(
      tabMove({ focusables: 0, inside: true, shift: true, onRoot: false, atFirst: true, atLast: true }),
    ).toBe("hold");
  });

  it("焦点泊在浮层根上时，两个方向都送进浮层（根排在孩子之前，Shift+Tab 默认会走出去）", () => {
    // 这一档是 hold 的下游：归档弹窗提交失败、两颗钮重新可用，而焦点还在根上
    const root = { focusables: 2, inside: true, onRoot: true, atFirst: false, atLast: false };
    expect(tabMove({ ...root, shift: false })).toBe("first");
    expect(tabMove({ ...root, shift: true })).toBe("last");
  });

  it("有可聚焦的东西时围栏那三条口径原样（这一档没被 R-S4-3 改动）", () => {
    const inBox = { focusables: 3, inside: true, onRoot: false };
    // 中间那几件：不插手，走浏览器默认
    expect(tabMove({ ...inBox, shift: false, atFirst: false, atLast: false })).toBe("none");
    expect(tabMove({ ...inBox, shift: true, atFirst: false, atLast: false })).toBe("none");
    // 两端绕回
    expect(tabMove({ ...inBox, shift: false, atFirst: false, atLast: true })).toBe("first");
    expect(tabMove({ ...inBox, shift: true, atFirst: true, atLast: false })).toBe("last");
    // 只有一件可聚焦：它既是第一件也是最后一件，两个方向都绕回它自己
    const only = { focusables: 1, inside: true, onRoot: false, atFirst: true, atLast: true };
    expect(tabMove({ ...only, shift: false })).toBe("first");
    expect(tabMove({ ...only, shift: true })).toBe("last");
    // 焦点跑到浮层外面（点过遮罩）：两个方向都拽回第一件
    const out = { focusables: 3, inside: false, onRoot: false, atFirst: false, atLast: false };
    expect(tabMove({ ...out, shift: false })).toBe("first");
    expect(tabMove({ ...out, shift: true })).toBe("first");
  });

  it("hold 落成 preventDefault + focusBox，而根上有 tabindex=-1（能 focus()，不进 Tab 序）", () => {
    expect(FOCUS).toContain('if (move === "none") return;');
    expect(FOCUS).toContain("e.preventDefault();");
    expect(FOCUS).toContain('if (move === "hold") focusBox(box);');
    expect(FOCUS).toContain('if (box.getAttribute("tabindex") === null) box.tabIndex = -1;');
    expect(FOCUS).toContain("box.focus();");
    // 根自身算不算"焦点在里面"要单独判：box.contains(box) 是真，光靠 inside 判不出来
    expect(FOCUS).toContain("onRoot: current === box,");
    // 原来那条"零可聚焦就放行"的早退删干净了（留着就等于 R-S4-3 没接上）
    expect(FOCUS).not.toContain("if (items.length === 0) return;");
    // tabindex="-1" 仍然不算进围栏：FOCUSABLE 那条排除式没被改松
    expect(FOCUS).toContain('[tabindex]:not([tabindex="-1"])');
    expect(FOCUS).toContain("button:not([disabled]), [href], input:not([disabled])");
  });

  it("tabindex 由钩子加、也由钩子撤（active 为假时不许把常驻侧栏塞进 Tab 序）", () => {
    expect(FOCUS).toContain(
      'const addedTabIndex = root !== null && root.getAttribute("tabindex") === null;',
    );
    expect(FOCUS).toContain("if (addedTabIndex && root !== null) root.tabIndex = -1;");
    expect(FOCUS).toContain('root.removeAttribute("tabindex");');
    // cleanup 里既卸监听也撤 tabindex（撤的只是自己加的那个：JSX 里本来就有的不动）
    expect(FOCUS).toMatch(
      /return \(\) => \{\s*document\.removeEventListener\("keydown", onKey, useCapture\);[\s\S]{0,200}removeAttribute\("tabindex"\);/,
    );
    // 关着就一件都不装：`if (!active) return;` 在挂 tabindex 之前
    expect(FOCUS).toMatch(/if \(!active\) return;[\s\S]{0,600}root\.tabIndex = -1;/);
  });

  it("归档弹窗提交中确实就是这个形状：两颗钮都 disabled，那一支里再没别的可聚焦元素", () => {
    expect(LIST).toContain(
      "useFocusTrap(cardRef, true, { onClose: busy ? undefined : onClose, autoFocus: false });",
    );
    const at = LIST.indexOf("归档这场会话");
    expect(at).toBeGreaterThan(0);
    // 归档那一支的 JSX（SessionModal 是文件里最后一个组件，切到文件尾就是它的全部）
    const branch = LIST.slice(at);
    expect(branch.match(/<button/g)).toHaveLength(2);
    expect(branch.match(/disabled=\{busy\}/g)).toHaveLength(2);
    expect(branch).not.toContain("<input");
    expect(branch).not.toContain("<a ");
    expect(branch).not.toContain("tabIndex");
    // 改名那一支的「取消」也 disabled={busy}（全文件第三处），但它还有输入框，不是零可聚焦
    expect(LIST.match(/disabled=\{busy\}/g)).toHaveLength(3);
  });
});

describe("对话列表不暴露后台操作计数（2026-10-30）", () => {
  it("小字只剩归档条目的归属者；不再读 last_outcome / 摆「几页 · 几引注」", () => {
    expect(LIST).not.toContain("last_outcome");
    expect(LIST).not.toContain("outcomeSummary");
    expect(LIST).not.toContain("引注");
    expect(LIST).toContain("（没有名字）");
  });
});

describe("会话条目布局：⋯ 永远在行末（2026-10-30 反馈）", () => {
  it("标题与小字包在 .ti 列里，菜单/归档 pill 是它的兄弟而不是下一行", () => {
    expect(LIST).toContain('className="ti"');
    expect(LIST).toMatch(/className="ti">[\s\S]{0,400}className="t"/);
    // 菜单在 .ti 之后（flex 行内靠右），不是 .sub 之后被折到下一行
    expect(LIST.indexOf('className="ti"')).toBeLessThan(LIST.indexOf("menu-wrap"));
    // 小字在 .ti 里面（标题下面），不是 .item 的直接子项
    const tiStart = LIST.indexOf('className="ti"');
    expect(LIST.indexOf('className="sub"')).toBeGreaterThan(tiStart);
  });
});

describe("归档折叠区（2026-10-30）", () => {
  const session = (id: number, status: string): SessionSummary => ({
    id, user_id: 1, owner_name: "张三", title: `会话 ${id}`, status,
    started_at: "2026-10-30T00:00:00+00:00", last_message_at: null,
  });

  it("splitArchived 把归档会话从工作区拆出去，顺序不变", () => {
    const { active, archived } = splitArchived([
      session(1, "active"), session(2, "archived"), session(3, "active"), session(4, "archived"),
    ]);
    expect(active.map((s) => s.id)).toEqual([1, 3]);
    expect(archived.map((s) => s.id)).toEqual([2, 4]);
  });

  it("侧栏渲染归档折叠区：默认收起、可展开、菜单里是取消归档", () => {
    expect(LIST).toContain("已归档（");
    expect(LIST).toContain("arch-block");
    expect(LIST).toContain("ARCHIVED_OPEN_KEY");
    expect(LIST).toContain("toggleArchived");
    expect(LIST).toContain("取消归档");
    expect(LIST).toContain("renderItem(s, true)");
  });

  it("归档确认文案说明可逆", () => {
    expect(LIST).toContain("归档后从列表收起、只读");
    expect(LIST).toContain("随时可以取消归档");
  });
});
