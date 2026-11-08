import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, apiGet, apiPatch, apiPost, apiPostForm, errText } from "../api.ts";
import { useIdentity } from "../useIdentity.ts";
import { CopyButton } from "../components/CopyButton.tsx";
import { Modal } from "../components/Modal.tsx";
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
} from "../members.ts";
import type { ChatVisibility, Invite, Member, MembersData, SiteSettings } from "../members.ts";

// 管理与设置页（`/members`，管理员）：站点设置 + 成员表 + 邀请链接。
//
// 2026-10-30 委托人裁定把原来三块（成员 / 注册邀请 / 重置邀请）重排成三块：
//   - **站点设置**：对话可见范围（all / own），改完立刻生效；
//   - **成员表**：每个非管理员一个"可建设 wiki"开关（摄入材料 + 改 wiki 共用一档权限）；
//   - **邀请**：注册与重置合成一张表，用"用途"一列区分；两种链接都是一次性。
//
// 权限口径：**不只看 sessionStorage.role**——角色只决定菜单显不显示入口；进页面一律
// 真打 `GET /api/members` 与 `GET /api/admin/settings`，任一回 403 就渲染"只有管理员
// 能看这一块"。这样改了 sessionStorage 也进不来数据（与 LogPage 同一套判据）。
//
// 所有写操作都是弹窗确认或开关即时提交 + 人话横幅；邀请/重置链接只显示一次，
// 复制走 clipboard.ts（HTTP 非安全上下文回退 execCommand）。
//
// 改名与新建邀请用 **FormData**（后端收 Form(...)）；权限与站点设置是 JSON PATCH。

type ModalState =
  | { kind: "rename"; member: Member }
  | { kind: "remove"; member: Member }
  | { kind: "reset"; member: Member }
  | { kind: "revoke"; invite: Invite };

interface TokenPanel {
  title: string;
  link: string;
}

function originOf(): string {
  if (typeof window === "undefined") return "";
  return window.location.origin;
}

/** 成员列表里把某个人的 can_build_wiki 改成 value（乐观更新与失败回滚共用）。 */
function withPermission(
  data: MembersData | null,
  id: number,
  value: boolean,
): MembersData | null {
  if (data === null) return null;
  return {
    ...data,
    users: data.users.map((member) =>
      member.id === id ? { ...member, can_build_wiki: value } : member,
    ),
  };
}

function InviteRow({
  invite,
  onCopyLink,
  onRevoke,
}: {
  invite: Invite;
  onCopyLink: string;
  onRevoke: () => void;
}) {
  const revoked = invite.revoked_at !== null;
  return (
    <tr className={revoked ? "invite-row revoked" : "invite-row"}>
      <td className="invite-name">{invite.name || "（没有名字）"}</td>
      <td>{invitePurposeLabel(invite.purpose)}</td>
      <td className="invite-link">
        <code>{onCopyLink}</code>
        <CopyButton text={onCopyLink} label="复制" className="btn mini" />
      </td>
      <td>{inviteState(invite)}</td>
      <td>{openedLabel(invite)}</td>
      <td>
        {revoked ? (
          <span className="muted">已作废</span>
        ) : (
          <button type="button" className="btn mini danger" onClick={onRevoke}>
            作废
          </button>
        )}
      </td>
    </tr>
  );
}

function InviteTable({
  invites,
  emptyText,
  origin,
  onRevoke,
}: {
  invites: Invite[];
  emptyText: string;
  origin: string;
  onRevoke: (invite: Invite) => void;
}) {
  if (invites.length === 0) return <p className="muted member-empty">{emptyText}</p>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">姓名</th>
            <th scope="col">用途</th>
            <th scope="col">链接</th>
            <th scope="col">状态</th>
            <th scope="col">首次点开</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {invites.map((invite) => (
            <InviteRow
              invite={invite}
              key={invite.id}
              onCopyLink={inviteUrl(origin, invite.token)}
              onRevoke={() => onRevoke(invite)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MembersPage() {
  const me = useIdentity();
  const meId = me?.id ?? null;

  const [data, setData] = useState<MembersData | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ level: "ok" | "err"; text: string } | null>(null);

  /** 站点设置：settings = 后端已保存的值；visibility = 单选里正在选的值。 */
  const [settings, setSettings] = useState<SiteSettings | null>(null);
  const [visibility, setVisibility] = useState<ChatVisibility>("all");
  const [settingsError, setSettingsError] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);

  /** 正在提交权限开关的那个成员 id：一颗开关一趟，避免连点打成两趟。 */
  const [permissionBusy, setPermissionBusy] = useState<number | null>(null);

  const [inviteName, setInviteName] = useState("");
  const [creating, setCreating] = useState(false);
  const [tokenPanel, setTokenPanel] = useState<TokenPanel | null>(null);

  const [modal, setModal] = useState<ModalState | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [modalError, setModalError] = useState("");
  const [busy, setBusy] = useState(false);

  const seq = useRef(0);
  /** 打开弹窗的那颗按钮：关掉/成功后焦点还给谁（键盘用户不许被扔回 body）。 */
  const returnFocus = useRef<HTMLElement | null>(null);

  const load = useCallback(async () => {
    const my = (seq.current += 1);
    setLoading(true);
    setError("");
    setForbidden(false);
    setSettingsError("");
    try {
      const [membersRes, settingsRes] = await Promise.allSettled([
        apiGet<unknown>("/api/members"),
        apiGet<unknown>("/api/admin/settings"),
      ]);
      if (seq.current !== my) return;
      // 两个接口都是管理员专属：任一 403 就整页拦。401 由 apiGet 整页跳登录。
      for (const res of [membersRes, settingsRes]) {
        if (res.status !== "rejected" || !(res.reason instanceof ApiError)) continue;
        if (res.reason.status === 401) return;
        if (res.reason.status === 403) {
          setForbidden(true);
          setData(null);
          return;
        }
      }
      if (membersRes.status === "rejected") {
        setError(errText(membersRes.reason, "成员列表没取到"));
        return;
      }
      setData(normalizeMembers(membersRes.value));
      // 设置是附加的一块：它挂了不该把成员表一起拖黑，这一块自己说"没取到"并可重试。
      if (settingsRes.status === "fulfilled") {
        const saved = normalizeSiteSettings(settingsRes.value);
        setSettings(saved);
        setVisibility(saved.chat_visibility);
      } else {
        setSettings(null);
        setSettingsError(errText(settingsRes.reason, "站点设置没取到"));
      }
    } catch (e) {
      if (seq.current !== my) return;
      setError(errText(e, "成员列表没取到"));
    } finally {
      if (seq.current === my) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      seq.current += 1;
    };
  }, [load]);

  function rememberFocus() {
    returnFocus.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  function restoreFocus() {
    const target = returnFocus.current;
    returnFocus.current = null;
    if (target !== null && target.isConnected) target.focus();
  }

  function openRename(member: Member) {
    rememberFocus();
    setModalError("");
    setRenameValue(member.name);
    setModal({ kind: "rename", member });
  }

  function openRemove(member: Member) {
    rememberFocus();
    setModalError("");
    setModal({ kind: "remove", member });
  }

  function openReset(member: Member) {
    rememberFocus();
    setModalError("");
    setModal({ kind: "reset", member });
  }

  function openRevoke(invite: Invite) {
    rememberFocus();
    setModalError("");
    setModal({ kind: "revoke", invite });
  }

  function closeModal() {
    if (busy) return;
    setModal(null);
    setModalError("");
    restoreFocus();
  }

  async function reloadAfter(okText: string) {
    setNotice({ level: "ok", text: okText });
    await load();
  }

  async function confirmModal() {
    if (modal === null) return;
    setBusy(true);
    setModalError("");
    try {
      if (modal.kind === "rename") {
        const form = new FormData();
        form.set("name", renameValue.trim());
        await apiPostForm<{ ok: boolean }>(`/api/members/${modal.member.id}/rename`, form);
        setModal(null);
        restoreFocus();
        await reloadAfter("姓名已改。");
      } else if (modal.kind === "remove") {
        await apiPost<{ ok: boolean }>(`/api/members/${modal.member.id}/remove`);
        setModal(null);
        restoreFocus();
        await reloadAfter("成员已停用。");
      } else if (modal.kind === "reset") {
        const got = await apiPost<{ token: string }>(`/api/members/${modal.member.id}/reset-login`);
        setModal(null);
        restoreFocus();
        setTokenPanel({
          title: `${modal.member.name} 的重置链接`,
          link: inviteUrl(originOf(), got.token),
        });
        await reloadAfter("重置链接已生成，只显示这一次。");
      } else {
        await apiPost<{ ok: boolean }>(`/api/members/invites/${modal.invite.id}/revoke`);
        setModal(null);
        restoreFocus();
        await reloadAfter("邀请链接已作废。");
      }
    } catch (e) {
      setModalError(errText(e, "操作没成功"));
    } finally {
      setBusy(false);
    }
  }

  /** 站点设置保存：PATCH 回来以服务端归一化后的值为准（不拿本地选择当结果）。 */
  async function saveSettings() {
    if (savingSettings || settings === null || visibility === settings.chat_visibility) return;
    setSavingSettings(true);
    setNotice(null);
    try {
      const raw = await apiPatch<unknown>("/api/admin/settings", { chat_visibility: visibility });
      const saved = normalizeSiteSettings(raw);
      setSettings(saved);
      setVisibility(saved.chat_visibility);
      setSettingsError("");
      setNotice({ level: "ok", text: "站点设置已保存，下一次请求就生效。" });
    } catch (e) {
      // 保存失败就把单选退回"当前真的生效的那个值"，别让界面显示一个没写进去的选择。
      setVisibility(settings.chat_visibility);
      setNotice({ level: "err", text: errText(e, "设置没保存") });
    } finally {
      setSavingSettings(false);
    }
  }

  /**
   * 成员「可建设 wiki」开关。先乐观改（开关必须跟手），失败回滚并挂横幅——
   * 403/400 的人话由后端给（管理员那行压根没有开关，正常点不出来）。
   */
  async function togglePermission(member: Member) {
    if (permissionBusy !== null) return;
    const next = !member.can_build_wiki;
    setPermissionBusy(member.id);
    setNotice(null);
    setData((prev) => withPermission(prev, member.id, next));
    try {
      await apiPatch<{ ok: boolean }>(`/api/members/${member.id}/permissions`, {
        can_build_wiki: next,
      });
      setNotice({
        level: "ok",
        text: next
          ? `${member.name} 现在可以建设 wiki。`
          : `${member.name} 现在只能使用 wiki。`,
      });
    } catch (e) {
      setData((prev) => withPermission(prev, member.id, member.can_build_wiki));
      setNotice({ level: "err", text: errText(e, "权限没改成") });
    } finally {
      setPermissionBusy(null);
    }
  }

  async function createInvite(e: FormEvent) {
    e.preventDefault();
    const name = inviteName.trim();
    if (name === "" || creating) return;
    setCreating(true);
    setNotice(null);
    try {
      const form = new FormData();
      form.set("name", name);
      const got = await apiPostForm<{ token: string }>("/api/members/invites", form);
      setInviteName("");
      setTokenPanel({ title: `给 ${name} 的注册邀请`, link: inviteUrl(originOf(), got.token) });
      await reloadAfter("注册邀请已生成，只显示这一次。");
    } catch (err) {
      setNotice({ level: "err", text: errText(err, "邀请没建成") });
    } finally {
      setCreating(false);
    }
  }

  if (loading && data === null && !forbidden) {
    return (
      <div className="paper-page">
        <div className="paper-page-inner">
          <p className="vault-state" role="status">
            管理与设置加载中…
          </p>
        </div>
      </div>
    );
  }

  if (forbidden) {
    return (
      <div className="paper-page">
        <div className="paper-page-inner">
          <div className="vault-empty">
            <h1>只有管理员能看这一块</h1>
            <p>成员、邀请与站点设置是管理员的事；你要是需要加人，找管理员发一条邀请链接。</p>
            <Link className="btn" to="/">
              回对话
            </Link>
          </div>
        </div>
      </div>
    );
  }

  if (error !== "" || data === null) {
    return (
      <div className="paper-page">
        <div className="paper-page-inner">
          <div className="vault-error">
            <div className="banner err" role="alert">
              {error || "成员列表没取到"}
            </div>
            <div className="acts">
              <button type="button" className="btn" onClick={() => void load()}>
                重试
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="paper-page">
      <div className="paper-page-inner">
        <header className="paper-head">
          <h1>管理与设置</h1>
          <p className="paper-sub">
            共 {data.users.length} 人。站点设置改完立刻生效；邀请链接只显示一次。
          </p>
        </header>

        {notice !== null && (
          <div className={`banner ${notice.level === "ok" ? "info" : "err"}`} role="status">
            {notice.text}
          </div>
        )}

        {tokenPanel !== null && (
          <section className="token-panel" aria-label="一次性链接">
            <div className="token-head">
              <strong>{tokenPanel.title}</strong>
              <button
                type="button"
                className="links"
                aria-label="收起这条链接"
                onClick={() => setTokenPanel(null)}
              >
                ×
              </button>
            </div>
            <p className="token-link">
              <code>{tokenPanel.link}</code>
              <CopyButton text={tokenPanel.link} />
            </p>
            <p className="token-hint">这条链接只显示这一次；刷新后要去下面的邀请表里找。</p>
          </section>
        )}

        <section className="paper-section" aria-labelledby="settings-title">
          <h2 id="settings-title">站点设置</h2>
          {settings === null ? (
            <div className="vault-error">
              <div className="banner err" role="alert">
                {settingsError || "站点设置没取到"}
              </div>
              <div className="acts">
                <button type="button" className="btn" onClick={() => void load()}>
                  重试
                </button>
              </div>
            </div>
          ) : (
            <>
              <fieldset className="setting-group">
                <legend className="setting-legend">对话可见范围</legend>
                <label className="setting-option">
                  <input
                    type="radio"
                    name="chat_visibility"
                    value="all"
                    checked={visibility === "all"}
                    onChange={() => setVisibility("all")}
                  />
                  <span>
                    所有人可见
                    <span className="setting-hint">成员能看到并参与全站会话（默认）</span>
                  </span>
                </label>
                <label className="setting-option">
                  <input
                    type="radio"
                    name="chat_visibility"
                    value="own"
                    checked={visibility === "own"}
                    onChange={() => setVisibility("own")}
                  />
                  <span>
                    成员只看自己的
                    <span className="setting-hint">各自的会话只对本人可见；管理员仍看全部</span>
                  </span>
                </label>
              </fieldset>
              <p className="setting-note">管理员始终能看到全部会话，不受这个开关影响。</p>
              <div className="acts">
                <button
                  type="button"
                  className="btn pri"
                  disabled={savingSettings || visibility === settings.chat_visibility}
                  onClick={() => void saveSettings()}
                >
                  {savingSettings ? "保存中…" : "保存设置"}
                </button>
              </div>
            </>
          )}
        </section>

        <section className="paper-section" aria-labelledby="members-title">
          <h2 id="members-title">
            成员<span className="count">{data.users.length} 人</span>
          </h2>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">姓名</th>
                  <th scope="col">角色</th>
                  <th scope="col">权限</th>
                  <th scope="col">加入时间</th>
                  <th scope="col">状态</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {data.users.map((user) => {
                  const status = memberStatus(user);
                  return (
                    <tr className={status.off ? "user-row off" : "user-row"} key={user.id}>
                      <td>
                        <span className="user-name">{user.name}</span>
                        {user.id === meId && <span className="muted">（我）</span>}
                      </td>
                      <td>{roleLabel(user.role)}</td>
                      <td>
                        {user.role === "admin" ? (
                          <span className="muted">全部权限</span>
                        ) : (
                          <label
                            className="perm-toggle"
                            title={
                              user.can_build_wiki
                                ? "关闭后只能查询、搜索、阅读；不能摄入材料或改/删已有页（查询结果仍可整理成查询/对照/摘要页）"
                                : "打开后可以摄入材料、让智能体建/改/删 wiki 页"
                            }
                          >
                            <input
                              type="checkbox"
                              checked={user.can_build_wiki}
                              disabled={permissionBusy === user.id}
                              onChange={() => void togglePermission(user)}
                            />
                            可建设 wiki
                          </label>
                        )}
                      </td>
                      <td>{joinedLabel(user)}</td>
                      <td>{status.label}</td>
                      <td className="row-actions">
                        <button type="button" className="btn mini" onClick={() => openRename(user)}>
                          改名
                        </button>
                        <button type="button" className="btn mini" onClick={() => openReset(user)}>
                          重置口令
                        </button>
                        {canRemove(user, meId) && (
                          <button
                            type="button"
                            className="btn mini danger"
                            onClick={() => openRemove(user)}
                          >
                            停用
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="paper-section" aria-labelledby="invites-title">
          <h2 id="invites-title">
            邀请链接<span className="count">{data.invites.length} 条</span>
          </h2>
          <form className="inline-form" onSubmit={(e) => void createInvite(e)}>
            <label className="sr" htmlFor="invite-name">
              受邀人姓名
            </label>
            <input
              id="invite-name"
              className="input"
              value={inviteName}
              onChange={(e) => setInviteName(e.target.value)}
              placeholder="受邀人姓名"
              maxLength={MEMBER_NAME_MAX}
            />
            <button type="submit" className="btn pri" disabled={creating || inviteName.trim() === ""}>
              {creating ? "生成中…" : "新建邀请"}
            </button>
          </form>
          <InviteTable
            invites={data.invites}
            emptyText="还没有邀请。"
            origin={originOf()}
            onRevoke={openRevoke}
          />
        </section>

        <footer className="paper-foot">
          停用成员不会删除历史消息、引注与版本；他们只是不能再登录。
        </footer>
      </div>

      {modal !== null && modal.kind === "rename" && (
        <Modal
          title={`给「${modal.member.name}」改名`}
          confirmLabel="保存"
          busy={busy}
          error={modalError}
          confirmDisabled={!validMemberName(renameValue)}
          onConfirm={() => void confirmModal()}
          onClose={closeModal}
        >
          <label className="field" htmlFor="rename-input">
            新姓名
          </label>
          <input
            id="rename-input"
            className="input"
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            maxLength={MEMBER_NAME_MAX}
            autoFocus
          />
          <p className="modal-tip">姓名会显示在会话、引注与成员列表里。</p>
        </Modal>
      )}

      {modal !== null && modal.kind === "remove" && (
        <Modal
          title={`停用「${modal.member.name}」？`}
          confirmLabel="停用"
          danger
          busy={busy}
          error={modalError}
          onConfirm={() => void confirmModal()}
          onClose={closeModal}
        >
          <p className="consequence">
            停用后他不能再登录；历史消息、引注与版本都会保留，不会删数据。
          </p>
        </Modal>
      )}

      {modal !== null && modal.kind === "reset" && (
        <Modal
          title={`给「${modal.member.name}」发重置链接？`}
          confirmLabel="生成链接"
          busy={busy}
          error={modalError}
          onConfirm={() => void confirmModal()}
          onClose={closeModal}
        >
          <p className="consequence">
            会生成一条只能重设口令的邀请链接，发给他之后由他自己设置新密码。链接只显示一次。
          </p>
        </Modal>
      )}

      {modal !== null && modal.kind === "revoke" && (
        <Modal
          title="作废这条邀请链接？"
          confirmLabel="作废"
          danger
          busy={busy}
          error={modalError}
          onConfirm={() => void confirmModal()}
          onClose={closeModal}
        >
          <p className="consequence">
            作废后这条链接立刻失效；已经用它注册或重置过的人不受影响。
          </p>
        </Modal>
      )}
    </div>
  );
}
