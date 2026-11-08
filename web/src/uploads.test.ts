// 附件两段式纯函数：暂存响应归一化、消息 FormData 带 upload_ids、空文字+附件可发、
// 删除一条暂存。选文件路径不触发发送由 Chat 源码钉子另测。
import { describe, expect, it } from "vitest";
import {
  canSendMessage,
  messageForm,
  normalizeStagedUpload,
  withoutStaged,
} from "./uploads.ts";
import type { StagedUpload } from "./uploads.ts";

const staged = (id: number): StagedUpload => ({
  id,
  orig_filename: `材料-${id}.pdf`,
  size: 1024,
  mime: "application/pdf",
  parse_status: "none",
});

describe("normalizeStagedUpload：POST /api/uploads 的响应", () => {
  it("upload_id/orig_filename/size/mime/parse_status 归一化", () => {
    expect(
      normalizeStagedUpload({
        upload_id: 7,
        orig_filename: "周记.pdf",
        size: 2048,
        mime: "application/pdf",
        parse_status: "none",
      }),
    ).toEqual({
      id: 7,
      orig_filename: "周记.pdf",
      size: 2048,
      mime: "application/pdf",
      parse_status: "none",
    });
  });

  it("认 id/filename 兜底；数字字符串能转；缺 upload_id → null", () => {
    expect(normalizeStagedUpload({ id: "8", filename: "a.txt" })).toMatchObject({
      id: 8,
      orig_filename: "a.txt",
      parse_status: "none",
    });
    expect(normalizeStagedUpload({ size: 1 })).toBeNull();
    expect(normalizeStagedUpload(null)).toBeNull();
  });
});

describe("messageForm：发送时把 upload_ids 一起带上", () => {
  it("content + 可重复 upload_ids", () => {
    const form = messageForm("整理这些材料", [3, 9]);
    expect(form.get("content")).toBe("整理这些材料");
    expect(form.getAll("upload_ids")).toEqual(["3", "9"]);
  });

  it("空文字 + 附件也照样带 id", () => {
    const form = messageForm("", [3]);
    expect(form.get("content")).toBe("");
    expect(form.getAll("upload_ids")).toEqual(["3"]);
  });
});

describe("canSendMessage / withoutStaged", () => {
  it("有文字或暂存附件就能发；空白文字不算", () => {
    expect(canSendMessage("你好", [])).toBe(true);
    expect(canSendMessage("   ", [staged(1)])).toBe(true);
    expect(canSendMessage("", [staged(1)])).toBe(true);
    expect(canSendMessage("   ", [])).toBe(false);
    expect(canSendMessage("", [])).toBe(false);
  });

  it("删除一条暂存不改入参", () => {
    const list = [staged(1), staged(2)];
    expect(withoutStaged(list, 1).map((s) => s.id)).toEqual([2]);
    expect(list.map((s) => s.id)).toEqual([1, 2]);
  });
});
