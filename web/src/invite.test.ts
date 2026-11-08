// 邀请页文案与状态判定的钉子（裁定 R-S3-6 的四项：purpose 分叉、非 404 的"打不开"、
// 确认密码预检，以及"链接作废"与"服务器出错"不许混为一谈）。
//
// 文案照抄后端能给出的那几句（services/auth.py:211「两次输入的密码不一样」、
// routers/api/auth.py:58「这条链接用不了，找管理员再要一个」），别在这儿改字。
import { describe, expect, it } from "vitest";

import { ApiError } from "./api.ts";
import {
  CONFIRM_MISMATCH,
  GONE_HEADING,
  UNREACHABLE_HEADING,
  confirmMismatch,
  inviteCopy,
  inviteFailure,
  usedCopy,
} from "./invite.ts";

describe("inviteCopy：purpose 决定这一页在说什么", () => {
  it("register（以及任何认不出的值）讲'开新号'", () => {
    for (const purpose of ["register", "", "whatever-s4-invents"]) {
      const c = inviteCopy(purpose, "小林");
      expect(c.isReset).toBe(false);
      expect(c.heading).toBe("接受邀请，注册账号");
      expect(c.sub).toContain("设置手机号和密码");
      expect(c.submit).toBe("注册并进入");
      // 注册那套话里不许出现"密码重设/重置"这类词：人是来开号的
      expect(c.heading + c.sub + c.passwordLabel).not.toContain("重设");
    }
  });

  it("reset 讲'给已有账号换密码'，并把名字说出来", () => {
    const c = inviteCopy("reset", "小林");
    expect(c.isReset).toBe(true);
    expect(c.heading).toBe("重设密码");
    expect(c.sub).toContain("小林");
    expect(c.sub).toContain("账号还是原来那个");
    expect(c.passwordLabel).toBe("新密码");
    expect(c.confirmLabel).toBe("再输一遍新密码");
    expect(c.submit).toContain("换好密码");
  });

  it("reset 的文案里没有后台术语（purpose/reset/令牌/token 都不许露出来）", () => {
    const c = inviteCopy("reset", "小林");
    const blob = Object.values(c).filter((v) => typeof v === "string").join(" ");
    for (const jargon of ["purpose", "reset", "令牌", "token", "邀请码"]) {
      expect(blob.toLowerCase()).not.toContain(jargon.toLowerCase());
    }
  });
});

describe("usedCopy：链接用过了也按 purpose 说话", () => {
  it("注册链接用过 = 号已经开出来了", () => {
    expect(usedCopy("register").sub).toContain("完成注册");
    expect(usedCopy("register").warn).toContain("账号不是你自己注册");
  });

  it("重设链接用过 = 密码已经换过一次", () => {
    expect(usedCopy("reset").sub).toContain("换过一次密码");
    expect(usedCopy("reset").warn).toContain("密码不是你自己换");
  });
});

describe("inviteFailure：404 是作废，其余是打不开", () => {
  it("404 → gone，标题仍是「链接失效了」，message 用后端那句人话", () => {
    const f = inviteFailure(new ApiError("这条链接用不了，找管理员再要一个", 404));
    expect(f.kind).toBe("gone");
    expect(f.heading).toBe(GONE_HEADING);
    expect(f.message).toBe("这条链接用不了，找管理员再要一个");
  });

  it.each([500, 502, 503])( "%i → unreachable，标题是「链接暂时打不开」", (status) => {
    const f = inviteFailure(new ApiError("服务器出错了，请稍后再试", status));
    expect(f.kind).toBe("unreachable");
    expect(f.heading).toBe(UNREACHABLE_HEADING);
    expect(f.heading).not.toBe(GONE_HEADING);
    expect(f.message).toBe("服务器出错了，请稍后再试");
    // 重试的提示必须在这一页上（否则人只会去重发链接）
    expect(f.sub).toContain("刷新重试");
  });

  it("压根没连上（不是 ApiError）→ unreachable + api.ts 的网络兜底", () => {
    const f = inviteFailure(new TypeError("Failed to fetch"));
    expect(f.kind).toBe("unreachable");
    expect(f.heading).toBe(UNREACHABLE_HEADING);
    expect(f.message).toBe("连不上服务器");
  });

  it("400 也不算作废（后端不会用 400 回邀请查询，真来了也当'打不开'）", () => {
    expect(inviteFailure(new ApiError("请求没成功", 400)).kind).toBe("unreachable");
  });
});

describe("confirmMismatch：提交前先自己判一遍", () => {
  it("两次一样 → null（放行）", () => {
    expect(confirmMismatch("abc123", "abc123")).toBeNull();
  });

  it("两次不一样 → 后端同一句话", () => {
    expect(confirmMismatch("abc123", "abc124")).toBe(CONFIRM_MISMATCH);
    expect(CONFIRM_MISMATCH).toBe("两次输入的密码不一样");
  });

  it("确认框还空着 → null：人正在打字，这时候不该报错", () => {
    expect(confirmMismatch("abc123", "")).toBeNull();
    expect(confirmMismatch("", "")).toBeNull();
  });

  it("只差一个空格也算不一样（不做 trim：密码里的空格是有意义的）", () => {
    expect(confirmMismatch("abc123", "abc123 ")).toBe(CONFIRM_MISMATCH);
  });
});
