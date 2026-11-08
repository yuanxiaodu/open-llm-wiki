import { ApiError, fallbackMessage } from "./api.ts";

// 邀请页 /i/:token 的**文案与状态判定**。全部是纯函数，pages/Register.tsx 只管摆。
//
// 为什么单开一个文件：这三件事都要能被 vitest 钉住（裁定 R-S3-6 的八项里有四项落在
// 这儿），而 Register.tsx 一 import 就把 react / react-router 拖进测试环境。与
// sse.ts、blocks.ts 同一条纪律：纯函数单独放，页面只画。
//
// 三条口径：
//   1. 邀请有两种用途（migrations/0001_init.sql 的 CHECK：purpose IN ('register','reset')）。
//      reset 链接不是"再注册一个号"，是**给已有账号换密码**（services/auth.py 的
//      register_with_invite）：只改链接绑定的那本账号，手机号对不上就 400，永远不建新号。
//      两种用途说同一套话，拿着 reset 链接来的人会以为自己在开新号——所以文案按 purpose 分叉。
//   2. 链接打不开分两种：404（后端 resolve_invite 认不出这条链接 = 失效/被吊销）与
//      其余（断网、5xx、代理挂了）。前者说"链接作废了，再要一个"，后者说"链接暂时
//      打不开，等会儿再试"——把服务器故障说成链接作废，人只会去找管理员重发，
//      而重发的那条照样打不开。
//   3. 确认密码在**提交之前**先自己判一遍。后端也会判（auth.py:210，同一句话），
//      前端这一道只是省一次往返：输错了立刻在框下面说，不用等一圈网络。

/** GET /api/invites/{token} 的响应（app/routers/api/auth.py:60）。 */
export interface InviteInfo {
  name: string;
  purpose: string;
  used: boolean;
}

/** POST /api/register 的响应。role 是**后端给的真值**，前端不猜（R-S3-6）。 */
export interface RegisterResponse {
  id: number;
  name: string;
  role: string;
  /** 建设 wiki 的权限（重设链接走进来时是既有账号的值）；旧后端缺省按 true。 */
  can_build_wiki: boolean;
}

/** 一套表单文案。isReset 留着给页面做别的分支（例如成功后的去向）。 */
export interface InviteCopy {
  isReset: boolean;
  heading: string;
  sub: string;
  phoneLabel: string;
  phonePlaceholder: string;
  passwordLabel: string;
  passwordPlaceholder: string;
  confirmLabel: string;
  confirmPlaceholder: string;
  submit: string;
  submitting: string;
}

/**
 * purpose → 文案。认得的只有 'reset'，**其余一律按注册讲**：
 * purpose 是库里的 CHECK 约束值，将来加了第三种用途，宁可用注册那套话（人还能看懂
 * "设置手机号和密码"），也不要因为认不出就摆一张空页。
 */
export function inviteCopy(purpose: string, name: string): InviteCopy {
  if (purpose === "reset") {
    return {
      isReset: true,
      heading: "重设密码",
      sub: `这条链接是给「${name}」的账号换一个新密码。账号还是原来那个，换完直接用它登录。`,
      phoneLabel: "这个账号的手机号",
      phonePlaceholder: "本来登录用的那个手机号",
      passwordLabel: "新密码",
      passwordPlaceholder: "设一个新密码",
      confirmLabel: "再输一遍新密码",
      confirmPlaceholder: "确认新密码",
      submit: "换好密码，进入",
      submitting: "正在换密码…",
    };
  }
  return {
    isReset: false,
    heading: "接受邀请，注册账号",
    sub: "设置手机号和密码，之后用它们登录",
    phoneLabel: "手机号",
    phonePlaceholder: "11 位手机号",
    passwordLabel: "密码",
    passwordPlaceholder: "设一个密码",
    confirmLabel: "再输一遍密码",
    confirmPlaceholder: "确认密码",
    submit: "注册并进入",
    submitting: "注册中…",
  };
}

/**
 * 链接已经被用过（used_count >= 1）时的文案。也按 purpose 分叉：
 * 注册链接用过 = "这个号已经开出来了"，重设链接用过 = "密码已经换过一次了"。
 * 后端对已用过的链接照样回 name/purpose/used（services/auth.py:171 的 resolve_invite
 * 认 problem == "used"），所以这一页拿得到 purpose，不用瞎猜。
 */
export function usedCopy(purpose: string): { heading: string; sub: string; warn: string } {
  if (purpose === "reset") {
    return {
      heading: "链接已经用过了",
      sub: "这条链接已经换过一次密码了，一个链接只能用一次",
      warn: "如果密码不是你自己换的，找管理员核实",
    };
  }
  return {
    heading: "链接已经用过了",
    sub: "这条邀请已经完成注册，一个链接只能用一次",
    warn: "如果账号不是你自己注册的，找管理员核实",
  };
}

/** 两种"打不开"：gone = 链接本身作废了；unreachable = 这一下没连上/服务器出错。 */
export type InviteFailure =
  | { kind: "gone"; heading: string; sub: string; message: string }
  | { kind: "unreachable"; heading: string; sub: string; message: string };

export const GONE_HEADING = "链接失效了";
export const GONE_SUB = "这条链接用不了，找管理员再要一个";
export const UNREACHABLE_HEADING = "链接暂时打不开";
export const UNREACHABLE_SUB = "不是链接作废，是这一下没取到。等一会儿刷新重试；一直打不开再找管理员";

/**
 * 加载邀请失败 → 哪一种"打不开"。
 *
 * 只有 **404** 算链接作废（后端 resolve_invite 认不出这条 token，含被吊销的那种）。
 * 其余（ApiError 的 5xx/400、或 fetch 抛了 = status 0）一律 unreachable：
 * message 用后端那句人话，压根没连上就用 api.ts 的兜底（「连不上服务器」）。
 */
export function inviteFailure(err: unknown): InviteFailure {
  if (err instanceof ApiError && err.status === 404) {
    return { kind: "gone", heading: GONE_HEADING, sub: GONE_SUB, message: err.message };
  }
  return {
    kind: "unreachable",
    heading: UNREACHABLE_HEADING,
    sub: UNREACHABLE_SUB,
    message: err instanceof ApiError ? err.message : fallbackMessage(0),
  };
}

/** 两次密码不一样时的那句话。与后端 services/auth.py:211 **逐字相同**，两边不会各说一套。 */
export const CONFIRM_MISMATCH = "两次输入的密码不一样";

/**
 * 确认密码的提交前预检：不一样就返那句人话，一样（或第二个框还空着）返 null。
 *
 * 第二个框空着**不算错**：人正在第一个框里打字时就把"两次不一样"糊在脸上，
 * 是最招人烦的一种校验。空着 = 还没填完，交给 HTML 的 required 与后端兜底。
 */
export function confirmMismatch(password: string, confirm: string): string | null {
  if (confirm.length === 0 || password === confirm) return null;
  return CONFIRM_MISMATCH;
}
