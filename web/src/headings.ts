import { useCallback, useEffect, useRef } from "react";
import type { MouseEvent, RefObject } from "react";
import type { OutlineHeading } from "./wiki.ts";

// 标题锚点：把 markdown 渲染出来的 h1..h6 按**出现顺序**贴上后端/解析器给的 id，
// 再处理目录点击与地址栏 hash。wiki 正文与书阅读器共用这一个钩子。
//
// 为什么不能让 streamdown 自己给 id：它不生成 id（正文是纯 markdown 渲染），而目录
// 需要 id 才能跳。后端（书）会交一份 headings（含它自己算的 id），我们照它的顺序贴，
// 保证目录 href 与正文标题一致；wiki 页没有目录，也照样贴（深链/将来用得上）。
//
// MutationObserver 是承重的：streamdown 是 lazy 的，第一次 effect 跑的时候正文可能
// 还在 Suspense 里，一个标题都没进 DOM。观察 childList 而不是 attributes，贴 id
// 不会触发自己，不会死循环。

/** 地址栏 hash → 目标 id（浏览器会把中文 hash 编码，这里解回来）。 */
export function hashTarget(): string {
  const raw = window.location.hash.replace(/^#/, "");
  if (raw === "") return "";
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export interface HeadingAnchors {
  /** 点目录项：滚到标题、闪一下、把 hash 写进地址栏（可复制/可后退）。 */
  jump: (e: MouseEvent<HTMLAnchorElement>, heading: OutlineHeading, index: number) => void;
}

export interface HeadingAnchorOptions {
  /** hash 变化（初始/前进后退/手贴 URL 都算）时调，wiki 用它判断要不要展开引注栏。 */
  onHash?: (id: string) => void;
}

/**
 * 给 `ref` 里的正文标题贴 id，并返回目录点击处理器。
 *
 * @param ref      包着 markdown 正文的容器（标题只在它里面找）
 * @param headings 目录标题（后端 headings 或 parseOutlineHeadings 的结果）
 */
export function useHeadingAnchors(
  ref: RefObject<HTMLElement | null>,
  headings: OutlineHeading[],
  { onHash }: HeadingAnchorOptions = {},
): HeadingAnchors {
  const scrolledHash = useRef("");
  const latest = useRef({ onHash });
  latest.current = { onHash };
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const root = ref.current;
    if (root === null || headings.length === 0) return;
    const apply = () => {
      const els = Array.from(root.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6"));
      headings.forEach((heading, index) => {
        const el = els[index];
        if (el !== undefined && heading.id !== "" && el.id !== heading.id) el.id = heading.id;
      });
    };
    const scrollToHash = () => {
      const id = hashTarget();
      if (id === "") return;
      latest.current.onHash?.(id);
      if (id === scrolledHash.current) return;
      const el = document.getElementById(id);
      if (el === null) return;
      scrolledHash.current = id;
      el.scrollIntoView({ block: "start" });
    };
    apply();
    scrollToHash();
    const observer = new MutationObserver(() => {
      apply();
      scrollToHash();
    });
    observer.observe(root, { childList: true, subtree: true });
    const onHashChange = () => {
      scrolledHash.current = "";
      // 浏览器前进/后退/手贴 URL：等一帧让渲染落定再找标题
      window.setTimeout(() => {
        apply();
        scrollToHash();
      }, 0);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => {
      observer.disconnect();
      window.removeEventListener("hashchange", onHashChange);
    };
  }, [ref, headings]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const jump = useCallback(
    (e: MouseEvent<HTMLAnchorElement>, heading: OutlineHeading, index: number) => {
      e.preventDefault();
      const root = ref.current;
      const els = root?.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6");
      const el = (heading.id !== "" ? document.getElementById(heading.id) : null) ?? els?.[index] ?? null;
      if (el === null) return;
      el.scrollIntoView({ block: "start", behavior: "smooth" });
      el.classList.add("heading-hit");
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        el.classList.remove("heading-hit");
        timer.current = null;
      }, 2000);
      scrolledHash.current = heading.id;
      if (heading.id !== "") history.replaceState(null, "", `#${encodeURIComponent(heading.id)}`);
    },
    [ref],
  );

  return { jump };
}
