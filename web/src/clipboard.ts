// 复制到剪贴板（成员页的邀请链接/重置链接要复制）。
//
// 为什么不能只写 `navigator.clipboard.writeText`：**HTTP 非安全上下文里 clipboard 是
// undefined**（部署机如果还没上 TLS 就必踩）——那时候用户点了"复制"什么也不会发生，
// 页面还显示"已复制"，比没有这个按钮更坏。所以：
//   1. 有 clipboard API → 用它；
//   2. 没有/抛错 → 隐藏 textarea + document.execCommand("copy")（同步、老但兼容）；
//   3. 都不行 → 返回 "manual"，页面提示"请手动复制"并展示完整链接。
//
// copyText 把"怎么复制"收成一个可注入的助手：测试在 node 里传假 deps 就能跑，不需要
// jsdom；真正碰 DOM 的只有 legacyCopy（页面运行时才走）。
//
// 注意文件名叫 clipboard.ts 而不是 copy.ts：copy.ts 已经存在，放的是两句共用的人话
// （EMPTY_TITLE / ARCHIVED_TEXT），不要混在一起。

export type CopyResult = "copied" | "manual";

export interface CopyDeps {
  /** navigator.clipboard.writeText；没有就传 null。 */
  writeText?: ((text: string) => Promise<void>) | null;
  /** 选不中/没有 clipboard 时的老回退；返回是否成功。 */
  legacyCopy?: ((text: string) => boolean) | null;
}

/**
 * 复制一段文字。**不抛错**：成功返回 "copied"，需要用户手动复制返回 "manual"。
 * 注入 deps 是为了可测试（node 环境没有 navigator/document）。
 */
export async function copyText(text: string, deps?: CopyDeps): Promise<CopyResult> {
  const env = deps ?? defaultCopyDeps();
  if (env.writeText) {
    try {
      await env.writeText(text);
      return "copied";
    } catch {
      // clipboard 有 API 但被权限/焦点拦下：继续走老回退
    }
  }
  if (env.legacyCopy) {
    try {
      if (env.legacyCopy(text)) return "copied";
    } catch {
      // 老回退也炸了：当作手动
    }
  }
  return "manual";
}

/** 运行时默认 deps：clipboard 可能不存在（HTTP），document 也可能不存在（测试/SSR）。 */
function defaultCopyDeps(): CopyDeps {
  const clipboard =
    typeof navigator !== "undefined" && typeof navigator.clipboard !== "undefined"
      ? navigator.clipboard
      : null;
  return {
    writeText:
      clipboard !== null && typeof clipboard.writeText === "function"
        ? (text: string) => clipboard.writeText(text)
        : null,
    legacyCopy: typeof document !== "undefined" ? legacyCopy : null,
  };
}

/** 隐藏 textarea + execCommand 的老办法。同步，返回是否复制成功。 */
export function legacyCopy(text: string): boolean {
  try {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.left = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, area.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    // 选中 textarea 会把焦点从按钮上抢走：复制完还回去，键盘用户才不会掉到 body
    if (previous !== null && previous.isConnected) previous.focus();
    return ok;
  } catch {
    return false;
  }
}
