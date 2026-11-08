import { useEffect, useRef, useState } from "react";
import { copyText } from "../clipboard.ts";

// 「复制链接」按钮：navigator.clipboard 不可用（HTTP 非安全上下文）时回退 execCommand，
// 两种都失败就提示手动复制——**不假装复制成功**。
//
// aria-live 把结果播给读屏：按钮文字变了（复制→已复制），但焦点还在按钮上，读屏用户
// 需要那一句在 live region 里被念出来。

export interface CopyButtonProps {
  text: string;
  label?: string;
  className?: string;
}

type CopyState = "idle" | "copied" | "manual";

export function CopyButton({ text, label = "复制链接", className = "btn" }: CopyButtonProps) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  async function onCopy() {
    const result = await copyText(text);
    setState(result === "copied" ? "copied" : "manual");
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      setState("idle");
      timer.current = null;
    }, 2200);
  }

  return (
    <span className="copy-wrap">
      <button type="button" className={className} onClick={() => void onCopy()}>
        {state === "copied" ? "已复制" : label}
      </button>
      <span className="copy-hint" role="status" aria-live="polite">
        {state === "copied"
          ? "已复制"
          : state === "manual"
            ? "复制没成功，请手动选中链接复制。"
            : ""}
      </span>
    </span>
  );
}
