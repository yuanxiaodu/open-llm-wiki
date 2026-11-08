import { useId, useRef } from "react";
import type { MouseEvent, ReactNode } from "react";
import { useFocusTrap } from "../focus.ts";

// 通用确认/输入弹窗（成员页的改名、停用、重置口令、作废邀请各一次）。
//
// 复用 theme.css 的 .overlay/.modal/.banner/.acts 形状（文档页在 .paper-page 里，
// 这些 class 会继承纸本令牌）。焦点纪律用 focus.ts 的 useFocusTrap：
//   打开那一下焦点进弹窗、Tab 在里面打转、Esc 关掉；按钮顺序是「取消」在前，
//   破坏性动作的确认按钮用 .btn.danger。
//
// busy 时不响应 Esc/取消（半截关掉会把"正在提交"的那一趟留在飞着的状态里）——
// 与 SessionList 的弹窗口径一致。

export interface ModalProps {
  title: string;
  children?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  confirmDisabled?: boolean;
  /** 后端人话错误（显示在弹窗里，不关弹窗）。 */
  error?: string;
  onConfirm: () => void;
  onClose: () => void;
}

export function Modal({
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  confirmDisabled = false,
  error = "",
  onConfirm,
  onClose,
}: ModalProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(cardRef, true, { onClose: busy ? undefined : onClose });

  function onOverlayDown(e: MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget && !busy) onClose();
  }

  return (
    <div className="overlay" role="presentation" onMouseDown={onOverlayDown}>
      <div
        ref={cardRef}
        className="card modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h2 id={titleId}>{title}</h2>
        {error !== "" && (
          <div className="banner err" role="alert">
            {error}
          </div>
        )}
        {children}
        <div className="acts">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className={danger ? "btn danger" : "btn pri"}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
