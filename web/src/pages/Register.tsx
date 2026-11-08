import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError, apiGet, apiPost, setIdentity } from "../api.ts";
import { confirmMismatch, inviteCopy, inviteFailure, usedCopy } from "../invite.ts";
import type { InviteFailure, InviteInfo, RegisterResponse } from "../invite.ts";

// 邀请页 /i/:token。进页先 GET /api/invites/{token}，链接四态各给一页人话：
//
//   加载中      → aria-busy 的占位（不闪横幅）
//   404         → 失效（后端 resolve_invite 认不出这条链接，含被吊销）
//   其余错误    → 暂时打不开（断网/5xx，跟"作废"分开说，见 invite.ts 第 2 条口径）
//   used=true   → 已用（used_count >= 1，名额已经用掉了）
//   其余        → 未开，正常出表单
//
// 表单里姓名只读——名字是管理员建邀请时定好的，注册者不能改（后端也不收）。
// 标题与字段文案按 invite.purpose 分叉（invite.ts 的 inviteCopy）：register 讲"开新号"，
// reset 讲"给已有账号换密码"。
//
// 提交 POST /api/register {token,phone,password,confirm}。提交前先自己判一遍两次密码
// 是否一致（invite.ts 的 confirmMismatch，与后端同一句话），不一致就不发这一趟。
// 失败照旧把 {"error"} 的人话摆进横幅；成功返回 **{id,name,role}**（裁定 R-S3-6：
// role 用后端给的真值，前端不写死 member——重设链接走进来时换的是既有账号，那个账号
// 完全可能是管理员），存 sessionStorage 后跳 /。
//
// 这页不受登录守卫管，api.ts 的 401 跳转也特意跳过 /i/*——注册者本来就没身份。

type LinkState =
  | { kind: "loading" }
  | { kind: "failed"; failure: InviteFailure }
  | { kind: "used"; invite: InviteInfo }
  | { kind: "open"; invite: InviteInfo };

export function Register() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const [state, setState] = useState<LinkState>({ kind: "loading" });
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiGet<InviteInfo>(`/api/invites/${encodeURIComponent(token)}`)
      .then((invite) => {
        if (cancelled) return;
        setState(invite.used ? { kind: "used", invite } : { kind: "open", invite });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({ kind: "failed", failure: inviteFailure(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // 边打字边说：两个框都填了而对不上，就在框下面提一句（不等到提交才炸）
  const mismatch = confirmMismatch(password, confirm);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy || state.kind !== "open") return;
    if (mismatch !== null) {
      // 前端预检（R-S3-6）：这一趟压根不发出去。后端也会判同一句，前端只是省一次往返。
      setError(mismatch);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const me = await apiPost<RegisterResponse>("/api/register", {
        token,
        phone,
        password,
        confirm,
      });
      // role 用后端返的真值：重设链接走进来时这是既有账号的角色（可能是 admin）
      setIdentity({
        id: me.id,
        name: me.name,
        role: me.role,
        can_build_wiki: me.can_build_wiki !== false,
      });
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "没成，稍后再试一次");
      setBusy(false);
    }
  }

  if (state.kind === "loading") {
    return (
      <div className="auth-page" aria-busy="true">
        <div className="card auth-card">
          <p className="sub" role="status" aria-live="polite">
            正在打开链接…
          </p>
        </div>
      </div>
    );
  }

  if (state.kind === "failed") {
    const f = state.failure;
    // gone（404）与 unreachable（断网/5xx）用同一张卡，但标题与提示分开：
    // 前者要人去找管理员重发，后者要人等一会儿再刷。
    return (
      <div className="auth-page">
        <div className="card auth-card">
          <h1>{f.heading}</h1>
          <p className="sub">{f.sub}</p>
          <div className="banner err" role="alert" aria-live="assertive">
            {f.message}
          </div>
          <p className="foot">
            <a href="/login">去登录</a>
          </p>
        </div>
      </div>
    );
  }

  if (state.kind === "used") {
    const u = usedCopy(state.invite.purpose);
    return (
      <div className="auth-page">
        <div className="card auth-card">
          <h1>{u.heading}</h1>
          <p className="sub">{u.sub}</p>
          <div className="banner warn" role="status" aria-live="polite">
            {u.warn}
          </div>
          <p className="foot">
            <a href="/login">去登录</a>
          </p>
        </div>
      </div>
    );
  }

  const { invite } = state;
  const copy = inviteCopy(invite.purpose, invite.name);
  return (
    <div className="auth-page">
      <form className="card auth-card" onSubmit={onSubmit}>
        <h1>{copy.heading}</h1>
        <p className="sub">{copy.sub}</p>

        {error && (
          <div className="banner err" role="alert" aria-live="assertive">
            {error}
          </div>
        )}

        <div className="field">
          <label htmlFor="name">你的名字</label>
          <input id="name" className="input" type="text" value={invite.name} readOnly />
        </div>

        <div className="field">
          <label htmlFor="phone">{copy.phoneLabel}</label>
          <input
            id="phone"
            className="input"
            type="tel"
            inputMode="numeric"
            autoComplete="username"
            placeholder={copy.phonePlaceholder}
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">{copy.passwordLabel}</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="new-password"
            placeholder={copy.passwordPlaceholder}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="confirm">{copy.confirmLabel}</label>
          <input
            id="confirm"
            className="input"
            type="password"
            autoComplete="new-password"
            placeholder={copy.confirmPlaceholder}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            aria-invalid={mismatch !== null}
            aria-describedby={mismatch !== null ? "confirm-tip" : undefined}
            required
          />
          {mismatch !== null && (
            <div className="tip" id="confirm-tip" role="alert">
              {mismatch}
            </div>
          )}
        </div>

        <button className="btn pri" type="submit" disabled={busy}>
          {busy ? copy.submitting : copy.submit}
        </button>
      </form>
    </div>
  );
}
