import { useEffect, useRef } from "react";
import type { RefObject } from "react";

// 焦点陷阱：一层模态浮层的键盘纪律。
//
// 全站有三层浮层要用同一份纪律：窄屏的会话抽屉（SessionList 的 <aside>）、改名/归档弹窗
// （SessionList 的 SessionModal）、以及 S4 vault 面那些确认弹窗（回滚 / 移除成员 / 作废链接，
// components/ConfirmModal.tsx）。原来这份纪律长在抽屉那一个 effect 里，弹窗只有半份
// （自己接住焦点 + Esc 关，没有 Tab 围栏）——于是弹窗开着时按 Tab 会走到背景页面上去，
// 读屏的人以为还在弹窗里，明眼人只看见焦点环消失在遮罩后面。抽出来共用（S4-T2）。
//
// 三件事，一件都不能少：
//   1. **打开那一下**把焦点收进浮层（autoFocus）：不落在 body 上，键盘用户不必从头 Tab；
//   2. **Tab 围栏**：在浮层里打转，走到最后一件再按 Tab 回第一件，Shift+Tab 反过来；
//      焦点已经跑到外面（点过遮罩、浏览器自己挪的）就拽回第一件；
//   3. **Esc 关掉**（onClose）：关掉之后"焦点还给谁"不归这儿管——触发钮在调用方手里
//      （抽屉还给 ☰，弹窗按 focusAfterModal 还 ⋯ 或 ☰），陷阱只负责说"我要关了"。
//
// 自己写这十几行而不是引库：全站就这三层浮层，为一个围栏加依赖不划算
// （S3/S4 计划的 Global Constraints 都钉着"不引新依赖"）。
//
// 一条纪律写在最前面：**只在打开的那一下聚焦**。这个 effect 每渲染一次就重跑一次的话，
// 每趟都会 focus() 一次浮层里的第一件——流式期间每一帧都渲染一次，键盘用户在抽屉里
// Tab 到哪儿都被下一帧拽回开头，抽屉压根没法用（S3 T6b fix round 1 第 1 条）。所以：
//   - 聚焦只发生在 active 的 **false→true 过渡**上（prevActive 记着上一次的值）；
//   - 回调一律走 ref，不进依赖数组：调用方传内联箭头也跳不起来（不能只靠调用方守规矩）。
//
// 还有一档容易漏的：**浮层里一件可聚焦的东西都没有**（R-S4-3）。归档弹窗提交中就是这形状
// ——「取消」与「确认归档」两颗钮都 disabled，而 FOCUSABLE 排除 disabled。这时候围栏要是
// 什么都不做，Tab 就走浏览器的默认行为：焦点从 body 起沿文档往下找，直接走到遮罩后面的
// 背景页上去（读屏的人还以为自己在弹窗里）。所以这一档由钩子兜住：给浮层根挂
// tabindex="-1"（能 focus() 而不进 Tab 序），把焦点按在它身上。tabindex 由钩子加也由钩子撤
// ——调用方不必在 JSX 里写死，写死了就等于把宽屏那层常驻侧栏（active 恒 false）也塞进 Tab 序。

/**
 * 浮层里能被 Tab 停下的元素。
 *
 * 选择器与全站浮层里实际会出现的控件对齐：按钮、链接、输入框、文本域、下拉框、可编辑区，
 * 以及显式 tabindex 的东西。`[tabindex="-1"]` 排除掉——那种是"能被 focus() 但不进 Tab
 * 序"的（例如附件那个藏起来的 file input），把它算进围栏会让人按 Tab 时焦点停在一个
 * 看不见的元素上。
 *
 * S4 收口补 textarea/select/contenteditable：这份纪律原来只服务窄屏抽屉（那里没有文本域），
 * 抽成"全站浮层纪律"之后，任何带输入框的弹窗（改名、将来的备注框）都要被围栏收住。
 */
export const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), '
  + 'select:not([disabled]), [contenteditable]:not([contenteditable="false"]), '
  + '[tabindex]:not([tabindex="-1"])';

export interface FocusTrapOptions {
  /**
   * Esc 按下时调它。**不给就是这一层不响应 Esc**（弹窗正在提交时就是这个用法：
   * `busy ? undefined : onClose`，半截关掉会把"改着…"的那一趟留在飞着的状态里）。
   */
  onClose?: () => void;
  /**
   * 打开那一下要不要把焦点收进浮层的第一件（默认要）。
   *
   * 弹窗传 false：它有自己那一下——改名要把焦点送进输入框**并全选**（直接打字就换名），
   * 归档要停在「取消」上（破坏性操作不该一开就停在确认那件）。围栏照旧装上，
   * 只是"焦点落哪儿"由弹窗自己说了算。
   */
  autoFocus?: boolean;
  /**
   * keydown 挂在 capture 阶段，并且 Esc 那一下带 stopPropagation（默认都不）。
   *
   * 只有窄屏抽屉要：它是从侧栏里长出来的一层，底下还有条目菜单自己的 Esc 处理器
   * （冒泡阶段），不掐掉的话一次 Esc 会同时关两层。弹窗不需要——它开着的时候抽屉已经
   * 关了（SessionList 口径 5：两层模态浮层一次只留一层）。
   */
  capture?: boolean;
}

/**
 * Tab 那一下把焦点挪去哪儿。**纯函数**：这一档的判断（尤其是"一件可聚焦的都没有"）
 * 不用真 DOM 也能钉住，见 components/SessionList.test.ts。
 *
 * - `none`：走浏览器默认（浮层里还有别的东西可去，围栏不插手）；
 * - `first` / `last`：绕回第一件 / 最后一件（含"焦点已经跑到浮层外面"那种拽回来）；
 * - `hold`：浮层里零可聚焦元素，把焦点按在浮层根上（R-S4-3）。
 */
export type TabMove = "none" | "first" | "last" | "hold";

export function tabMove(o: {
  /** 浮层里此刻能被 Tab 停下的件数（已过滤掉不在渲染里的）。 */
  focusables: number;
  /** 焦点还在浮层里吗。 */
  inside: boolean;
  /** 这一下带没带 Shift。 */
  shift: boolean;
  /** 焦点是不是正停在浮层根自身（tabindex="-1"，被 hold 泊在那儿，或点了浮层的空白处）。 */
  onRoot: boolean;
  /** 焦点是不是正停在第一件上。 */
  atFirst: boolean;
  /** 焦点是不是正停在最后一件上。 */
  atLast: boolean;
}): TabMove {
  // 零可聚焦：先判这一档，否则下面的 first/last 压根不存在，Tab 会走出去
  if (o.focusables === 0) return "hold";
  // 焦点已经跑到浮层外面（点过遮罩、或浏览器自己挪的）：不管哪个方向都拽回第一件
  if (!o.inside) return "first";
  // 焦点泊在浮层根上：两个方向都得送进浮层里，不能放浏览器默认那一步走。
  // 根在文档序里排在它自己的孩子**之前**，所以 Shift+Tab 的默认行为是走到浮层前面去
  // ——也就是背景页（hold 之后又冒出可聚焦件的那一档，例如归档弹窗提交失败、两颗钮重新
  // 可用，焦点还停在根上）。
  if (o.onRoot) return o.shift ? "last" : "first";
  if (o.shift && o.atFirst) return "last";
  if (!o.shift && o.atLast) return "first";
  return "none";
}

/**
 * 把焦点按在浮层根自身。它有 tabindex="-1"（钩子在生效期间挂着，没有就补一个），
 * 于是能被 focus() 而不进 Tab 序——零可聚焦元素时唯一的落脚点（R-S4-3）。
 */
function focusBox(box: HTMLElement | null): void {
  if (box === null) return;
  if (box.getAttribute("tabindex") === null) box.tabIndex = -1;
  box.focus();
}

/**
 * 给一层模态浮层装上焦点陷阱。`active` = 这一层现在开着吗（关着就一件都不装：
 * 宽屏的常驻侧栏 drawerOpen 恒 false，于是它压根不在 Tab 序上设围栏，主内容照常可达）。
 *
 * ref 指向**浮层那个根元素**（<aside class="sb">、<div class="modal">），不是遮罩。
 * 钩子在生效期间会给它挂 `tabindex="-1"`（cleanup 里撤掉，JSX 里本来就有的不动），
 * 好让"零可聚焦元素"那一档有地方安放焦点（R-S4-3）。
 */
export function useFocusTrap(
  ref: RefObject<HTMLElement | null>,
  active: boolean,
  { onClose, autoFocus = true, capture = false }: FocusTrapOptions = {},
): void {
  // 回调与开关走 ref，effect 的依赖里只有 active：调用方给的若不是稳定引用（内联箭头），
  // 依赖数组里有它就会让 effect 每渲染一次重跑一次，理由见文件头那条纪律。
  const latest = useRef({ onClose, autoFocus, capture });
  latest.current = { onClose, autoFocus, capture };
  /** 上一次的 active：判"这一次是不是刚从关到开"（只在过渡那一下聚焦）。 */
  const prevActive = useRef(false);

  useEffect(() => {
    const justOpened = active && !prevActive.current;
    prevActive.current = active;
    if (!active) return;
    // capture 在装卸时必须同一个值，所以取一次就用它（它也不该中途变）
    const useCapture = latest.current.capture;
    // 浮层根在陷阱生效期间要能自己接住 focus()：零可聚焦元素时（归档弹窗提交中，两颗钮
    // 都 disabled）焦点得有地方待，否则它掉回 body，下一次 Tab 就从文档开头走——走到遮罩
    // 后面的背景页上去（R-S4-3）。自己加的 tabindex 自己在 cleanup 里撤：JSX 里本来就有的
    // 那个不许动，也不许在 active 为假时留着（宽屏那层常驻侧栏压根不该进 Tab 序）。
    const root = ref.current;
    const addedTabIndex = root !== null && root.getAttribute("tabindex") === null;
    if (addedTabIndex && root !== null) root.tabIndex = -1;

    if (justOpened && latest.current.autoFocus) {
      const first = root?.querySelector<HTMLElement>(FOCUSABLE) ?? null;
      if (first !== null) first.focus();
      else focusBox(root);
    }

    function onKey(e: globalThis.KeyboardEvent) {
      const box = ref.current;
      if (e.key === "Escape") {
        if (useCapture) e.stopPropagation();
        latest.current.onClose?.();
        return;
      }
      if (e.key !== "Tab" || !box) return;
      // offsetParent 判"这件在不在渲染里"（display:none 的祖先会让它是 null）；
      // 画布外的抽屉整块 visibility:hidden，那种情况由 theme.css 与 active 一起挡掉
      const items = Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      const current = document.activeElement;
      const move = tabMove({
        focusables: items.length,
        inside: box.contains(current),
        shift: e.shiftKey,
        onRoot: current === box,
        atFirst: current === items[0],
        atLast: current === items[items.length - 1],
      });
      if (move === "none") return;
      // 三种挪法都不许再走浏览器默认那一步（默认那一步就是"走出浮层"）
      e.preventDefault();
      if (move === "hold") focusBox(box);
      else if (move === "first") items[0]!.focus();
      else items[items.length - 1]!.focus();
    }
    document.addEventListener("keydown", onKey, useCapture);
    return () => {
      document.removeEventListener("keydown", onKey, useCapture);
      if (addedTabIndex && root !== null && root.getAttribute("tabindex") === "-1") {
        root.removeAttribute("tabindex");
      }
    };
  }, [active, ref]);
}
