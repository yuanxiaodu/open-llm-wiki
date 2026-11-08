// 附件两段式的纯函数（S11）：选文件只暂存，发送时把 upload_ids 随消息提交。
//
// 后端契约：
//   POST /api/uploads (multipart file) → {upload_id, orig_filename, size, mime, parse_status}
//   DELETE /api/uploads/{id}
//   POST /api/chat/{sid}/messages 的 Form 字段 upload_ids（可重复）
//
// 这一层不碰 React：暂存附件归一化、消息 FormData 组装、能否发送、移除一条暂存。
// 页面（Chat）只管调接口/摆状态，测试钉这一层。

export interface StagedUpload {
  id: number;
  orig_filename: string;
  size: number;
  mime: string;
  /** 暂存件通常是 none（还没解析）；后端也可能给别的值，原样显示。 */
  parse_status: string;
}

function obj(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function nonNeg(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

/** `POST /api/uploads` 响应 → 一条暂存附件；没有合法 upload_id 就 null。 */
export function normalizeStagedUpload(raw: unknown): StagedUpload | null {
  const r = obj(raw);
  const id = nonNeg(r.upload_id ?? r.id);
  if (id <= 0) return null;
  return {
    id,
    orig_filename: str(r.orig_filename) || str(r.filename) || `upload/${id}`,
    size: nonNeg(r.size),
    mime: str(r.mime),
    parse_status: str(r.parse_status) || "none",
  };
}

/** 发送时组装消息 FormData：内容 + 可重复的 upload_ids。纯函数，node 里也有 FormData。 */
export function messageForm(content: string, uploadIds: readonly number[]): FormData {
  const form = new FormData();
  form.set("content", content);
  for (const id of uploadIds) form.append("upload_ids", String(id));
  return form;
}

/**
 * 能不能发：有文字，或者有暂存附件（空文字+附件允许；后端会生成一句播报正文）。
 * 纯空白文字不算文字。
 */
export function canSendMessage(text: string, staged: readonly Pick<StagedUpload, "id">[]): boolean {
  return text.trim() !== "" || staged.length > 0;
}

/** 从暂存列表里去掉一条（删除接口成功之后调用）。不改入参。 */
export function withoutStaged<T extends { id: number }>(list: readonly T[], id: number): T[] {
  return list.filter((item) => item.id !== id);
}
