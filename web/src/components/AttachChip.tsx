import type { Attachment } from "../types.ts";

// 附件胶囊：类型图标 + 文件名 + 大小 + 解析三态文案（+ 纯图片页那句提示）。
//
// 三态文案是契约（计划头部）：
//   done   → 已解析
//   failed → 解析失败
//   none   → 未解析（没有已完成的解析结果）
// 2026-10-30 起不提供原件下载：胶囊只显示状态，不再是下载链接；原件留在服务器上
// 作为解析/引注的来源，人类面能读的是解析后的正文与 wiki 页。
//
// parse_report 只读两个键（pages_total / pages_image_only），而且只在下面 imageOnlyHint
// 那两条判据都成立时才说一句话；其余的键一个都不显示——把 pages_total 单独摆出来就得给
// 它起名，而起任何名（尤其「页数」）都是撒谎：它对 docx/txt/md 恒为 1。

/** 三态文案。后端 CHECK 只放这三个值，认不出来的一律按 none 说（最保守：不承诺解析过）。 */
const STATE_COPY: Record<string, string> = {
  done: "已解析",
  failed: "解析失败",
  none: "未解析",
};

/** 扩展名 → 图标里的短标签（图标就是这三个字母，不引图片资源）。 */
export function iconLabel(filename: string): string {
  const dot = filename.lastIndexOf(".");
  const ext = dot >= 0 ? filename.slice(dot + 1).toLowerCase() : "";
  switch (ext) {
    case "pdf":
      return "PDF";
    case "epub":
      return "EPUB";
    case "ppt":
    case "pptx":
      return "PPT";
    case "doc":
    case "docx":
      return "DOC";
    case "xls":
    case "xlsx":
    case "csv":
      return "表";
    case "txt":
      return "TXT";
    case "md":
      return "MD";
    case "png":
    case "jpg":
    case "jpeg":
    case "gif":
    case "webp":
    case "bmp":
      return "图";
    default:
      return "文件";
  }
}

/** 字节数 → 人话（B / KB / MB，一位小数；后端上限 50 MB，用不着 GB）。 */
export function humanSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 102.4) / 10} KB`;
  return `${Math.round(bytes / (102.4 * 1024)) / 10} MB`;
}

/** 报告里的一个整数键；缺失、不是数、NaN/Infinity 一律 null（= 那句话不说）。 */
function numOf(report: Record<string, unknown> | null, key: string): number | null {
  if (report === null) return null;
  const v = report[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * 「含 N 页图片，未入检索」（spec §7 的原文）——**两条判据都成立才说**，否则返 null：
 *
 *   pages_total > 1        单页的图（一张截图、一页扫描件）说"含 N 页图片"是废话；更要紧的
 *                          是 pages_total 对 docx/txt/md 恒为 1，那几种材料压根没有"图片页"
 *                          这回事，凭这个键就开口等于给它们编一个属性；
 *   pages_image_only > 0   真有一页只有图、没有可检索的文字，检索里就是缺它。这句话的分量
 *                          全在"未入检索"三个字上，没缺就别吓人。
 *
 * N 用的是 pages_image_only（缺的那几页）而不是 pages_total（一共几页）：说"含 12 页图片"
 * 而实际只有 2 页没进检索，是把材料说得比它更不可用。
 *
 * 报告缺失或形状不对也返 null：parse_report 是解析器写的自由形状，宁可少说一句提示，
 * 也不要拼出一句「含 NaN 页图片」摆在那儿。
 */
export function imageOnlyHint(report: Record<string, unknown> | null): string | null {
  const total = numOf(report, "pages_total");
  const imageOnly = numOf(report, "pages_image_only");
  if (total === null || imageOnly === null) return null;
  if (total <= 1 || imageOnly <= 0) return null;
  return `含 ${imageOnly} 页图片，未入检索`;
}

/**
 * 文档内嵌图片的识别情况（2026-10-30）：`含 N 张图片，已识别 M 张`。
 * 只认 images_total / images_described（后端的 parse_report 新键）；没有就是 null，
 * 不拿 0 编一句"含 0 张图片"。
 */
export function imageCountHint(report: Record<string, unknown> | null): string | null {
  const total = numOf(report, "images_total");
  if (total === null || total <= 0) return null;
  const described = numOf(report, "images_described") ?? 0;
  const truncated = report?.images_truncated === true;
  if (described <= 0) return `含 ${total} 张图片，未识别`;
  if (truncated) return `含 ${total} 张图片，已识别前 ${described} 张`;
  return `含 ${total} 张图片，已识别 ${described} 张`;
}

export function AttachChip({ att }: { att: Attachment }) {
  const state = att.parse_status === "none" && att.parse_report?.unsupported === true
    && typeof att.parse_report.note === "string"
    ? att.parse_report.note
    : STATE_COPY[att.parse_status] ?? STATE_COPY.none!;
  const size = humanSize(att.size);
  const imageOnly = imageOnlyHint(att.parse_report);
  const imageCount = imageCountHint(att.parse_report);
  // 纯展示：下载入口已移除（2026-10-30），三态只说明"进没进检索"。
  return (
    <span className={`chip st-${att.parse_status}`} title={state}>
      <span className="ic" aria-hidden="true">
        {iconLabel(att.orig_filename)}
      </span>
      <span className="nm">{att.orig_filename}</span>
      {size && <span className="sz">{size}</span>}
      <span className="st">{state}</span>
      {imageOnly && <span className="img-only">{imageOnly}</span>}
      {imageCount && <span className="img-only">{imageCount}</span>}
    </span>
  );
}
