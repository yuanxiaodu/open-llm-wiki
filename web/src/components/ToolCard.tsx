import { useState } from "react";
import type { ToolResult } from "../types.ts";

// 工具卡片：一次工具往返（tool-call 与它的 tool-result 按 payload 的 id 配对，
// 配对在 blocks.ts 的 foldEvent 里做——流上与历史同一份规则，这里只管渲染一对）。
//
// 折叠是默认的（一轮里可能十几次调用，全展开会把正文挤没），两条例外：
//   1. ok=false 的卡片默认展开——失败要人看见，不该藏在一次点击后面；
//   2. 人点过之后一律听人的（resultToggled），流式过程中结果后到也不会把
//      人刚收起来的卡片再弹开。
// 结果还没到时显示「…」（工具在跑），到点后换成 ✓ / ✘；✘ 的卡片整只走红边。
//
// ToolResult 这个形状住在 types.ts，不住在这儿：blocks.ts（纯函数、运行时只 import
// sse.ts）也要用它描述"配上结果的一张卡"，让它反过来 import 一个组件文件是把依赖
// 方向弄反了。

interface Props {
  name: string;
  /** tool-call 的 args 原串（可能整个键缺失 → 空串，见 sse.ts 的 optStr）。 */
  args: string;
  result?: ToolResult;
}

/** 工具名 → 人话动作。对话框里不出现 read/write_page/lint 这些内部词。 */
const TOOL_LABELS: Record<string, string> = {
  read: "读材料",
  list_wiki: "看目录",
  search: "找资料",
  write_page: "建页",
  edit_page: "改页",
  delete_page: "删页",
  lint: "体检",
  ingest_url: "抓网页",
  ingest_document: "长文摄入",
  ingest_text: "归档原文",
  web_search: "联网找资料",
};

/** 工具名 → 人话动作（认不出的原样显示，不藏）。 */
export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name;
}

/** 这一次在操作什么：从参数里挑一个最短的对象名，不摆整段 JSON。 */
export function toolTarget(name: string, args: string): string {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(args) as Record<string, unknown>;
  } catch {
    return "";
  }
  if (parsed === null || typeof parsed !== "object") return "";
  const text = (key: string): string =>
    typeof parsed[key] === "string" ? (parsed[key] as string) : "";
  switch (name) {
    case "read":
    case "edit_page":
    case "delete_page":
      return text("path").replace(/^.*\//, "").replace(/\.md$/i, "");
    case "write_page":
      return text("title");
    case "search":
    case "web_search":
      return text("query");
    case "ingest_url":
      return text("url").replace(/^https?:\/\//i, "");
    case "ingest_text":
      return typeof parsed.message_id === "number" ? `消息 ${parsed.message_id}` : "本次原文";
    case "ingest_document":
      return typeof parsed.upload_id === "number" ? `材料 ${parsed.upload_id}` : "";
    default:
      return "";
  }
}

/** args 是 JSON 串就美化一下（缩进两格），不是就原样摆——摆原串总比摆一句谎话好。 */
function prettyArgs(args: string): string {
  if (!args) return "";
  try {
    return JSON.stringify(JSON.parse(args), null, 2);
  } catch {
    return args;
  }
}

/** 收起时的一行预览：有参数看参数，没参数看结果，都没有就什么也不说。 */
function preview(args: string, result?: ToolResult): string {
  const raw = args || result?.summary || "";
  const one = raw.replace(/\s+/g, " ").trim();
  return one.length > 70 ? `${one.slice(0, 70)}…` : one;
}

export function ToolCard({ name, args, result }: Props) {
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = toggled ?? (result ? !result.ok : false);
  const failed = result?.ok === false;
  const pretty = prettyArgs(args);
  const label = toolLabel(name);
  const target = toolTarget(name, args);
  // 收起时只显示"动作 + 对象"；展开时参数与结果都在下面，头上不再重复。
  const line = target || (open ? "" : preview(args, result));

  return (
    <div className={`tool${failed ? " fail" : ""}${result ? "" : " run"}`}>
      <button
        type="button"
        className="tr"
        aria-expanded={open}
        onClick={() => setToggled(!open)}
      >
        <span className="lab">{result ? "已调用" : "调用中"}</span>
        <span className="nm">{label}</span>
        {line && <span className="pv">{line}</span>}
        <span className="mark" aria-hidden="true">
          {result ? (result.ok ? "✓" : "✘") : "…"}
        </span>
        <span className="chev" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="tbody">
          {pretty && <pre className="args">{pretty}</pre>}
          {result && (
            <div className={`tres${failed ? " fail" : ""}`}>
              <span className="mark" aria-hidden="true">
                {result.ok ? "✓" : "✘"}
              </span>
              <span>{result.summary}</span>
            </div>
          )}
          {!result && !pretty && <div className="tres">这一次没有参数。</div>}
        </div>
      )}
      {/* 屏幕阅读器的一句话总结：折叠状态下 ✓/✘ 只是个符号，得有文字 */}
      <span className="sr">
        {result
          ? `${label}${target ? ` ${target}` : ""} ${result.ok ? "成功" : "失败"}：${result.summary}`
          : `正在${label}${target ? ` ${target}` : ""}`}
      </span>
    </div>
  );
}
