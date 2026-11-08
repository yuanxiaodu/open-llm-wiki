// 成员页纯函数：邀请链接拼接、角色/状态人话、停用按钮判据、姓名校验、
// 站点设置与权限字段归一化（2026-10-30 管理页改造）。
import { describe, expect, it } from "vitest";
import {
  MEMBER_NAME_MAX,
  canRemove,
  invitePurposeLabel,
  inviteState,
  inviteUrl,
  joinedLabel,
  memberStatus,
  normalizeMembers,
  normalizeSiteSettings,
  openedLabel,
  roleLabel,
  validMemberName,
} from "./members.ts";

describe("normalizeMembers：剔掉坏行，权限/用途字段归一", () => {
  it("剔掉坏行；缺字段给兜底；can_build_wiki 缺省 true", () => {
    const got = normalizeMembers({
      users: [
        { id: 1, name: "张三", phone: "138", role: "member", created_at: "2026-10-01" },
        { id: 2, name: "李四", role: "member", can_build_wiki: 0 },
        { id: 3, name: "王五", role: "member", can_build_wiki: 1 },
        {},
      ],
      invites: [
        { id: 2, name: "李婷", token: "t", used_count: 1, purpose: "register" },
        { id: 3, name: "赵六", token: "u", purpose: "reset", user_id: 9 },
        { token: "" },
      ],
    });
    expect(got.users).toHaveLength(3);
    expect(got.users[0].deactivated_at).toBeNull();
    expect(got.users[0].can_build_wiki).toBe(true);
    expect(got.users[1].can_build_wiki).toBe(false);
    expect(got.users[2].can_build_wiki).toBe(true);
    expect(got.invites).toHaveLength(2);
    expect(got.invites[0].used_count).toBe(1);
    expect(got.invites[0].purpose).toBe("register");
    expect(got.invites[0].user_id).toBeNull();
    expect(got.invites[1].purpose).toBe("reset");
    expect(got.invites[1].user_id).toBe(9);
  });

  it("非数组/空 → 空表", () => {
    expect(normalizeMembers(null)).toEqual({ users: [], invites: [] });
    expect(normalizeMembers({})).toEqual({ users: [], invites: [] });
  });
});

describe("normalizeSiteSettings：只认 all / own，其余回退 all", () => {
  it("认两个合法值", () => {
    expect(normalizeSiteSettings({ chat_visibility: "all" })).toEqual({ chat_visibility: "all" });
    expect(normalizeSiteSettings({ chat_visibility: "own" })).toEqual({ chat_visibility: "own" });
  });

  it("缺字段/写坏/大小写 → all（误锁成 own 比维持旧行为更糟）", () => {
    expect(normalizeSiteSettings({})).toEqual({ chat_visibility: "all" });
    expect(normalizeSiteSettings({ chat_visibility: "nope" })).toEqual({ chat_visibility: "all" });
    expect(normalizeSiteSettings(null)).toEqual({ chat_visibility: "all" });
    expect(normalizeSiteSettings({ chat_visibility: " OWN " })).toEqual({ chat_visibility: "own" });
  });
});

describe("inviteUrl：origin + /i/token（HTTP 也成立）", () => {
  it("拼 link，origin 末尾斜杠去掉", () => {
    expect(inviteUrl("http://127.0.0.1:8000", "abc")).toBe("http://127.0.0.1:8000/i/abc");
    expect(inviteUrl("https://site.test/", "abc")).toBe("https://site.test/i/abc");
    expect(inviteUrl("", "abc")).toBe("/i/abc");
  });
});

describe("人话与判据", () => {
  it("roleLabel / memberStatus", () => {
    expect(roleLabel("admin")).toBe("管理员");
    expect(roleLabel("member")).toBe("成员");
    expect(roleLabel("x")).toBe("x");
    expect(memberStatus({ deactivated_at: null })).toEqual({ label: "正常", off: false });
    expect(memberStatus({ deactivated_at: "2026-10-23" })).toEqual({ label: "已停用", off: true });
  });

  it("canRemove：只对别的、还在用的、非管理员成员", () => {
    const member = { id: 2, role: "member", deactivated_at: null };
    expect(canRemove(member, 1)).toBe(true);
    expect(canRemove(member, 2)).toBe(false); // 自己
    expect(canRemove({ ...member, role: "admin" }, 1)).toBe(false);
    expect(canRemove({ ...member, deactivated_at: "x" }, 1)).toBe(false);
  });

  it("邀请用途：register → 注册；reset → 重置口令；认不出原样", () => {
    expect(invitePurposeLabel("register")).toBe("注册");
    expect(invitePurposeLabel("reset")).toBe("重置口令");
    expect(invitePurposeLabel("gift")).toBe("gift");
    expect(invitePurposeLabel("")).toBe("邀请");
    expect(invitePurposeLabel(null)).toBe("邀请");
  });

  it("邀请状态：已作废 > 已使用（一次性）> 还没用", () => {
    expect(inviteState({ used_count: 0, revoked_at: null })).toBe("还没用");
    expect(inviteState({ used_count: 1, revoked_at: null })).toBe("已使用（一次性）");
    expect(inviteState({ used_count: 0, revoked_at: "x" })).toBe("已作废");
    // 已作废优先：用过的又作废了，显示"已作废"（这条链接现在确实不能用）
    expect(inviteState({ used_count: 1, revoked_at: "x" })).toBe("已作废");
    expect(openedLabel({ first_opened_at: null })).toBe("还没点开");
    expect(openedLabel({ first_opened_at: "2026-10-23T03:00:00+00:00" })).not.toBe("还没点开");
  });

  it("joinedLabel 坏时间兜底；validMemberName 去空白且卡 60 字", () => {
    expect(joinedLabel({ created_at: "" })).toBe("未知");
    expect(joinedLabel({ created_at: "2026-10-01T00:00:00+00:00" })).not.toBe("未知");
    expect(validMemberName(" 张三 ")).toBe(true);
    expect(validMemberName("   ")).toBe(false);
    expect(validMemberName("a".repeat(MEMBER_NAME_MAX + 1))).toBe(false);
  });
});
