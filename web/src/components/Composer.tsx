import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { humanSize, iconLabel } from "./AttachChip.tsx";
import type { StagedUpload } from "../uploads.ts";

// 输入区：textarea + 附件 + 发送 + 右侧提示（方向 B 样张的 .composer）。
//
// 五条口径：
//   1. 归档会话整块禁用（textarea/附件/发送都 disabled），提示位换成后端那句 400 人话
//      「这个会话已归档，不能再发言」——不是前端自己编的"只读"，是它真会得到的回答。
//   2. 正文超 BODY_MAX（20000 字）时发送禁用并把话说明白；后端也会 400 兜底，
//      前端这一道只是省一次往返（计划 Task 5）。
//   3. Enter 发送、Shift+Enter 换行；**中文输入法组词期间的 Enter 不算发送**
//      （isComposing），否则选词就把半句话发出去了。
//   4. 焦点不许丢：点了「发送」之后它自己就 disabled 了，浏览器会把焦点扔回 body，
//      键盘用户得从头 Tab 一遍——所以点完立刻把焦点交回输入框（T5 评审的 M4）。
//   5. 只有使用权限的账号（allowUploads=false）不能摄入材料：附件按钮与文件选择框
//      整块不渲染、拖拽/粘贴的文件一律忽略。**文字收发照旧**——受限账号仍能查询、
//      搜索、阅读，只是不能把材料喂进来、不能改 wiki（2026-10-30 口径）。

/** 正文上限，与后端 search.MAX_BODY 同一个数（超了后端 400 人话）。 */
export const BODY_MAX = 20000;

/** 受限账号的输入框 placeholder：不再说"把资料拖进来"，那件事它做不了。 */
const PLACEHOLDER_UPLOAD = "问一句，或把资料拖进来";
const PLACEHOLDER_READONLY = "问一句，知识库里有答案就引用";

/**
 * 文件选择框的 accept（S6 多来源摄入）。
 *
 * 只是给系统文件选择器一个提示，不是安全边界：后端仍按自己的白名单校验。这里把
 * 后端支持的全列上（含图片与老 Office 格式——老格式后端尽力解析）。
 * 网页链接不在这里：它走 agent 的抓取工具，直接在对话里发 URL。
 */
export const UPLOAD_ACCEPT =
  ".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.epub,.png,.jpg,.jpeg,.webp,.gif,.bmp,.txt,.md";

/** 正常状态不占提示位（极简）；只有禁用/超长/上传/流式才需要一句话。 */
const HINT_DEFAULT = "";

interface Props {
  draft: string;
  onDraft: (v: string) => void;
  onSend: () => void;
  onFiles: (files: File[]) => void;
  /** 没有会话可选、或会话已归档：整块禁用。 */
  disabled: boolean;
  /** 禁用时提示位显示的人话（归档就是那句 400）。 */
  disabledHint?: string;
  /**
   * 这个账号能不能摄入材料（建设 wiki 的权限）。false = 附件按钮与文件选择框禁用、
   * 拖拽/粘贴的文件忽略；文字收发不受影响。
   *
   * 判据在调用方（Chat 交的是 canBuildWiki(me)）：页面知道当前身份，输入区只负责画
   * 与拦。**必填**——漏传就编不过，不会悄悄把受限账号放行（与 MessageList 的 canResend
   * 同一条纪律）。
   */
  allowUploads: boolean;
  /** POST 消息 / 上传在飞：发送与附件先按住，避免双发。 */
  sending: boolean;
  /**
   * 一轮在跑（流没结束）。**只用来换提示位那句话**，不参与任何禁用判定。
   *
   * 整轮期间发送钮一直是 disabled 的——但那不是这个 prop 干的：Chat 的 send() 在
   * POST 之前就把 sending 按住（`setSending(true)` → `await runTurn(...)` → finally 复位），
   * 而 runTurn 要到流收尾才返回，所以「发送中…」从点下去一直挂到这一轮写完。
   * 因此不存在"在流没结束时点发送、吃一个 409"这条路；409 只可能来自别处
   * （另一个标签页、或别人在同一场会话上发了一句），那种情况照样由横幅说。
   */
  streaming: boolean;
  uploading: boolean;
  /** 已暂存、还没随消息发出的附件（POST /api/uploads 的结果）。 */
  staged: readonly StagedUpload[];
  /** 点暂存 chip 的 ×：删掉这条暂存（DELETE /api/uploads/{id}）。 */
  onRemoveStaged: (id: number) => void;
  /**
   * 空态「整理一份资料」卡的信号：每 +1 就打开一次文件选择器。
   * 用信号而不是把 file input 提到 Chat：选择器仍归输入区自己管，焦点/禁用口径不变。
   */
  feedSignal: number;
}

export function Composer({
  draft,
  onDraft,
  onSend,
  onFiles,
  disabled,
  disabledHint,
  allowUploads,
  sending,
  streaming,
  uploading,
  staged,
  onRemoveStaged,
  feedSignal,
}: Props) {
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const wasSending = useRef(false);
  const lastFeed = useRef(feedSignal);
  const [dragging, setDragging] = useState(false);
  const tooLong = draft.length > BODY_MAX;
  /** 有文字或有暂存附件就能发（空文字+附件允许）。 */
  const canSend = draft.trim() !== "" || staged.length > 0;
  const canAttach = allowUploads && !disabled && !uploading;

  // 跟着内容长高，最多 200px（再高就该滚动，别把消息流挤没）。
  useEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft, disabled]);

  // 空态「整理资料」卡：信号变化就打开文件选择器（这里是点击的同一条调用栈之后，
  // 浏览器仍认作一次用户手势；不要改成 setTimeout，那会被拦）。
  useEffect(() => {
    if (feedSignal === lastFeed.current) return;
    lastFeed.current = feedSignal;
    // 受限账号没有文件选择器可开：信号照样消费掉，免得以后权限放开时突然自动弹一个
    if (canAttach) fileRef.current?.click();
  }, [feedSignal, canAttach]);

  // 兜底：一轮结束（sending 复位）时，若焦点已经掉回 body 就把它交回输入框。
  // 只在"确实丢了"的时候做——人可能已经 Tab 去看工具卡片，那时候抢焦点是添乱。
  useEffect(() => {
    if (wasSending.current && !sending && document.activeElement === document.body) {
      areaRef.current?.focus();
    }
    wasSending.current = sending;
  }, [sending]);

  /**
   * 发送，并同步把焦点交回输入框（口径 4）。
   *
   * 这一下会让「发送」自己变 disabled（Chat 把 sending 按住整轮），浏览器随即把焦点
   * 扔回 body。focus() 是在同一次事件处理里同步调的，早于 React 的重渲染，所以焦点
   * 已经落在输入框上，那次 disabled 抢不走它。
   */
  function submit() {
    onSend();
    areaRef.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    e.preventDefault();
    if (!disabled && !sending && !tooLong) submit();
  }

  const hint = disabled
    ? (disabledHint ?? "这一场现在不能发言")
    : tooLong
      ? `这条太长了（最长 ${BODY_MAX} 字），拆成几条再发`
      : uploading
        ? "正在上传…"
        : streaming
          ? "智能体正在写，写完再发下一句"
          : sending
            ? "正在提交消息，附件可能需要时间解析…"
            : staged.length > 0
              ? "附件已上传，点击发送后开始解析"
              : HINT_DEFAULT;

  // 暂存附件与发送消息独立，等待回复期间可以先选下一份材料。
  const droppable = canAttach;
  return (
    <div
      className={`composer${disabled ? " off" : ""}${dragging ? " dragging" : ""}`}
      onDragOver={(e) => {
        if (!droppable) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        // 拖到子元素上也会触发 leave：只有真正离开整块才算
        if (e.currentTarget.contains(e.relatedTarget as Node)) return;
        setDragging(false);
      }}
      onDrop={(e) => {
        if (!droppable) return;
        e.preventDefault();
        setDragging(false);
        const files = Array.from(e.dataTransfer.files ?? []);
        if (files.length > 0) onFiles(files);
      }}
    >
      <div className="composer-card">
        {dragging && (
          <div className="drop-hint" aria-hidden="true">
            松手后自动整理成页
          </div>
        )}
        <label className="sr" htmlFor="composer-text">
          要说的话
        </label>
        {staged.length > 0 && (
          <ul className="chips composer-staged" aria-label="附件">
            {staged.map((item) => (
              <li className="chip staged" key={item.id}>
                <span className="ic" aria-hidden="true">
                  {iconLabel(item.orig_filename)}
                </span>
                <span className="nm">{item.orig_filename}</span>
                {item.size > 0 && <span className="sz">{humanSize(item.size)}</span>}
                <button
                  type="button"
                  className="chip-x"
                  aria-label={`移除附件 ${item.orig_filename}`}
                  title="移除附件"
                  onClick={() => onRemoveStaged(item.id)}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        <textarea
          id="composer-text"
          className="box"
          ref={areaRef}
          rows={1}
          placeholder={allowUploads ? PLACEHOLDER_UPLOAD : PLACEHOLDER_READONLY}
          value={draft}
          disabled={disabled}
          onChange={(e) => onDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={(e) => {
            // 没有建设权限就不吃文件粘贴：留住浏览器默认行为（不 preventDefault），
            // 也不把文件交给上传那条路；纯文字粘贴照旧。
            if (!canAttach) return;
            const files = Array.from(e.clipboardData.files ?? []);
            if (files.length === 0) return;
            e.preventDefault();
            onFiles(files);
          }}
        />
        <div className="row">
          {allowUploads && (
            <>
              <input
                ref={fileRef}
                type="file"
                multiple
                accept={UPLOAD_ACCEPT}
                className="sr-file"
                tabIndex={-1}
                aria-hidden="true"
                disabled={!canAttach}
                onChange={(e) => {
                  const picked = Array.from(e.target.files ?? []);
                  // 立刻清空：同一个文件连传两次也要能触发 change
                  e.target.value = "";
                  if (picked.length > 0) onFiles(picked);
                }}
              />
              <button
                type="button"
                className="btn attach-btn"
                aria-label={uploading ? "上传中…" : "附件"}
                title="附件"
                disabled={!canAttach}
                onClick={() => fileRef.current?.click()}
              >
                <span className="paperclip" aria-hidden="true">
                  📎
                </span>
                <span className="sr">{uploading ? "上传中…" : "附件"}</span>
              </button>
            </>
          )}
          <button
            type="button"
            className="btn pri send-btn"
            aria-label={sending ? "发送中…" : "发送"}
            title={sending ? "发送中…" : "发送"}
            disabled={disabled || sending || uploading || tooLong || !canSend}
            onClick={submit}
          >
            <span className="send-arrow" aria-hidden="true">
              ↑
            </span>
            <span className="sr">{sending ? "发送中…" : "发送"}</span>
          </button>
          <span className={`hint${tooLong ? " bad" : ""}`}>{hint}</span>
        </div>
      </div>
    </div>
  );
}
