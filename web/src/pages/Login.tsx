import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, apiPost, getIdentity, setIdentity } from "../api.ts";
import { useSiteName } from "../site.ts";

// 登录页：手机号 + 密码 → POST /api/login（JSON，裁定 R11）。
// 后端 app/routers/api/auth.py 给的是 {"error": 人话}（400），失败就直接把
// 这句话摆进红色横幅——不在前端做任何翻译或术语包装。
// 成功：{id,name,role,can_build_wiki} 存 sessionStorage，跳 /。
//
// 已经有身份时（cookie 还活着、tab 没关，人却又回到了 /login）给一条捷径直接进对话
// （裁定 R-S3-6）：守卫本来就会把已登录页面放行，让人再填一遍手机号密码是白折腾。
// 注意这只看 sessionStorage 里的身份，不校验 cookie 是否真还有效——真过期了，
// 进去之后第一个 /api 请求的 401 会把他送回这一页（api.ts 的口径）。

interface LoginResponse {
  id: number;
  name: string;
  role: string;
  can_build_wiki: boolean;
}

export function Login() {
  const navigate = useNavigate();
  const me = getIdentity();
  // 站名是部署期配置：登录页在拿到身份之前就要显示它，所以读的是 /api/site 的 store。
  const siteName = useSiteName();
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const me = await apiPost<LoginResponse>("/api/login", { phone, password });
      // can_build_wiki 决定界面上摆不摆建设入口；后端老版本没这个键时按 true 兜底
      setIdentity({
        id: me.id,
        name: me.name,
        role: me.role,
        can_build_wiki: me.can_build_wiki !== false,
      });
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败，请重试");
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <form className="card auth-card" onSubmit={onSubmit} aria-busy={busy}>
        <h1>{siteName}</h1>
        <p className="sub">用手机号和密码登录</p>

        {me && (
          // 这台浏览器上还留着身份：给一条捷径，别逼人再填一遍（R-S3-6）
          <div className="banner info" role="status" aria-live="polite">
            {me.name}，你在这个浏览器上还登着。
            <Link className="shortcut" to="/">
              已登录？进入首页
            </Link>
          </div>
        )}

        {error && (
          <div className="banner err" role="alert" aria-live="assertive">
            {error}
          </div>
        )}

        <div className="field">
          <label htmlFor="phone">手机号</label>
          <input
            id="phone"
            className="input"
            type="tel"
            inputMode="numeric"
            autoComplete="username"
            placeholder="11 位手机号"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">密码</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="current-password"
            placeholder="登录密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>

        <button className="btn pri" type="submit" disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </button>

        <p className="foot">还没有账号？找管理员要邀请链接</p>
      </form>
    </div>
  );
}
