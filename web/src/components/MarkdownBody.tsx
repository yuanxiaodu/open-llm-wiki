import { Suspense, lazy, useEffect } from "react";
import type { ComponentPropsWithoutRef } from "react";
import { Link } from "react-router-dom";
import { safeMarkdownHref } from "../wiki.ts";

// markdown 正文的共用渲染器：wiki 页 / wiki 索引 / 书阅读器都用它。
//
// 三件事收在一个地方，免得三处各写一份：
//   1. streamdown **懒加载**（它把 shiki/remark 全家拖在一起，静态 import 会进首屏；
//      挂载即预热，登录页一个字节都不取它）；
//   2. `a` 组件覆盖：站内链接 → <Link>，其余按白名单；
//   3. urlTransform 白名单（safeMarkdownHref），拦 javascript:/data: 等协议。
//
// 2026-10-30 起没有句子级引注；正文永远交给 streamdown 渲染，**不** dangerouslySetInnerHTML。

const Streamdown = lazy(() => import("streamdown").then((m) => ({ default: m.Streamdown })));

/**
 * streamdown 的 `a` 组件覆盖。
 * `node` 是 react-markdown 传进来的 HAST 节点，不是 DOM 属性，故意不往下传。
 */
function MarkdownLink({ href = "", children }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const safe = safeMarkdownHref(href);
  if (safe === "") return <span>{children}</span>;
  if (safe.startsWith("/")) return <Link to={safe}>{children}</Link>;
  if (safe.startsWith("#")) {
    return (
      <a href={safe} rel="noreferrer">
        {children}
      </a>
    );
  }
  return (
    <a href={safe} rel="noreferrer">
      {children}
    </a>
  );
}

const MD_COMPONENTS = { a: MarkdownLink };

export interface MarkdownBodyProps {
  markdown: string;
  /** 传给 streamdown 的 className（wiki 用 "md"，书阅读器用 "md reader-md"）。 */
  className?: string;
  /** Suspense 里的占位文案（默认「正文加载中…」）。 */
  loadingText?: string;
}

export function MarkdownBody({ markdown, className = "md", loadingText = "正文加载中…" }: MarkdownBodyProps) {
  // 预热 markdown 那块 chunk（同 MessageList：挂载就取，别等正文到了才去取）
  useEffect(() => {
    void import("streamdown").catch(() => {});
  }, []);

  return (
    <Suspense fallback={<p className="vault-state">{loadingText}</p>}>
      {/* tableMaxHeight={0}：同 MessageList——关掉 streamdown 默认的 300px 表格高度上限，
          那层壳依赖 Tailwind 的 overflow 类，本仓库不引 Tailwind，留着会压住下文。 */}
      <Streamdown
        className={className}
        mode="static"
        controls={false}
        animated={false}
        lineNumbers={false}
        components={MD_COMPONENTS}
        urlTransform={safeMarkdownHref}
        tableMaxHeight={0}
      >
        {markdown}
      </Streamdown>
    </Suspense>
  );
}
