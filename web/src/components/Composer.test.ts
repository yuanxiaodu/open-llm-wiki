// 输入区的多来源摄入适配（S6）：文件选择框的 accept 与那行类型说明。
//
// 没有 jsdom，所以这里用 vite 的 `?raw` 读源码钉形状 + 直接断言导出的常量；真正的
// 文件选择行为在手工验收/a11y 脚本里看。
import { describe, expect, it } from "vitest";

import { Composer, UPLOAD_ACCEPT, BODY_MAX } from "./Composer.tsx";
import COMPOSER from "./Composer.tsx?raw";

describe("UPLOAD_ACCEPT：后端支持的格式都列上", () => {
  it("pdf/doc/docx/ppt/pptx/xls/xlsx/epub/图片/txt/md", () => {
    for (const ext of [
      ".pdf",
      ".docx",
      ".doc",
      ".pptx",
      ".ppt",
      ".xlsx",
      ".xls",
      ".epub",
      ".png",
      ".jpg",
      ".jpeg",
      ".webp",
      ".gif",
      ".bmp",
      ".txt",
      ".md",
    ]) {
      expect(UPLOAD_ACCEPT.split(",")).toContain(ext);
    }
  });

  it("input 真的用了 accept（不是只导出常量）", () => {
    expect(COMPOSER).toContain("accept={UPLOAD_ACCEPT}");
    expect(COMPOSER).toContain('type="file"');
    expect(COMPOSER).toContain("multiple");
  });
});

describe("拖拽 / 粘贴 / 空态整理信号（2026-10-30）", () => {
  it("粘贴文件走 onFiles；拖入时整块高亮并只在那时说一句", () => {
    expect(COMPOSER).toContain("onPaste");
    expect(COMPOSER).toContain("e.clipboardData.files");
    expect(COMPOSER).toContain("onDrop");
    expect(COMPOSER).toContain("onDragOver");
    expect(COMPOSER).toContain('className="drop-hint"');
    expect(COMPOSER).toContain("松手后自动整理成页");
  });

  it("空态「整理一份资料」的信号会打开文件选择器", () => {
    expect(COMPOSER).toContain("feedSignal");
    expect(COMPOSER).toContain("fileRef.current?.click()");
  });

  it("没加新的上传/网址按钮", () => {
    // 附件按钮 + 发送按钮 + 每个暂存 chip 的移除按钮（URL 走对话，不摆网址按钮）
    const buttons = COMPOSER.match(/<button/g) ?? [];
    expect(buttons).toHaveLength(3);
  });

  it("正文上限/组件导出没被顺手改掉", () => {
    expect(BODY_MAX).toBe(20000);
    expect(typeof Composer).toBe("function");
  });

  it("DeepSeek 式悬浮卡片 + 圆形附件/发送（另有暂存 chip 的移除按钮）", () => {
    expect(COMPOSER).toContain('className="composer-card"');
    expect(COMPOSER).toContain("attach-btn");
    expect(COMPOSER).toContain("send-btn");
    expect(COMPOSER).toContain('className="paperclip"');
    expect(COMPOSER).toContain('className="send-arrow"');
    // 圆形按钮里的可读名字用 sr-only，a11y 脚本按 textContent.includes('发送') 仍找得到
    expect(COMPOSER).toContain('<span className="sr">{sending ? "发送中…" : "发送"}</span>');
    expect((COMPOSER.match(/<button/g) ?? []).length).toBe(3);
  });

  it("附件两段式：暂存 chip 在 composer 上方，可移除；仅暂存附件也能点发送", () => {
    expect(COMPOSER).toContain('className="chips composer-staged"');
    expect(COMPOSER).toContain("staged.map");
    expect(COMPOSER).toContain("onRemoveStaged(item.id)");
    expect(COMPOSER).toContain('aria-label={`移除附件 ${item.orig_filename}`}');
    expect(COMPOSER).toContain("const canSend = draft.trim() !== \"\" || staged.length > 0;");
    expect(COMPOSER).toContain("|| !canSend");
  });

  it("快捷键/自动增高没被改掉", () => {
    expect(COMPOSER).toContain('e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing');
    expect(COMPOSER).toContain("Math.min(el.scrollHeight, 200)");
  });
});

describe("能力受限的账号（allowUploads=false，2026-10-30）", () => {
  it("不再显示“只能使用 wiki，不能摄入材料”的权限提示语", () => {
    expect(COMPOSER).not.toContain("UPLOAD_LOCKED_TEXT");
    expect(COMPOSER).not.toContain("当前账号只能使用 wiki");
  });

  it("附件按钮与文件选择框整块不渲染（不是 disabled）", () => {
    // 2026-10-30 委托人：没有摄入权限时上传按钮应该“看不见”，不是灰着可点。
    expect(COMPOSER).toContain("{allowUploads && (");
    expect(COMPOSER).toMatch(/\{allowUploads && \(\s*<>[\s\S]{0,200}className="sr-file"/);
    expect(COMPOSER).toMatch(/\{allowUploads && \(\s*<>[\s\S]{0,1600}attach-btn/);
    // 不再把允许条件写进 disabled：整块不可见，disabled 与否都无关
    expect(COMPOSER).not.toContain("disabled={disabled || sending || !allowUploads}");
    expect(COMPOSER).toContain('title="附件"');
  });

  it("拖拽与文件粘贴一律忽略；空态整理信号也不再打开选择器", () => {
    expect(COMPOSER).toContain("const canAttach = allowUploads && !disabled && !uploading;");
    expect(COMPOSER).toContain("const droppable = canAttach;");
    expect(COMPOSER).toContain("if (!canAttach) return;");
    expect(COMPOSER).toContain("if (canAttach) fileRef.current?.click();");
  });

  it("文字收发不受限：输入框 placeholder 换一句不承诺'拖进来'的话", () => {
    expect(COMPOSER).toContain("placeholder={allowUploads ? PLACEHOLDER_UPLOAD : PLACEHOLDER_READONLY}");
    expect(COMPOSER).not.toContain('placeholder="问一句，或把资料拖进来"');
    // 发送按钮的三条判据里没有 allowUploads：受限账号照样能发消息
    expect(COMPOSER).toContain("disabled={disabled || sending || uploading || tooLong || !canSend}");
  });
});
