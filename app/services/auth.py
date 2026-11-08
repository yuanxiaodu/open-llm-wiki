from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time

from ..db import utcnow

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

MAX_FAILS = 5
LOCK_SECONDS = 300


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, salt.hex(), digest.hex()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str) -> str | None:
    """手机号只保留数字；长度不在 7–15 之间视为填错。"""
    digits = _DIGITS.sub("", raw or "")
    return digits if 7 <= len(digits) <= 15 else None


def new_invite_token() -> str:
    return secrets.token_urlsafe(32)


class LoginThrottle:
    """进程内计数：同一手机号连败 MAX_FAILS 次后冷却 LOCK_SECONDS 秒。"""

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def locked_seconds(self, phone: str) -> int:
        until = self._locked_until.get(phone, 0.0)
        return max(0, int(until - time.monotonic()))

    def register_failure(self, phone: str) -> None:
        count = self._fails.get(phone, 0) + 1
        self._fails[phone] = count
        if count >= MAX_FAILS:
            self._locked_until[phone] = time.monotonic() + LOCK_SECONDS
            self._fails[phone] = 0

    def register_success(self, phone: str) -> None:
        self._fails.pop(phone, None)
        self._locked_until.pop(phone, None)

    def reset(self) -> None:
        self._fails.clear()
        self._locked_until.clear()


throttle = LoginThrottle()


def authenticate(conn: sqlite3.Connection, phone: str, password: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM users WHERE phone = ? AND deactivated_at IS NULL", (phone,)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    now = utcnow()
    conn.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"])
    )
    return row


def create_member_direct(
    conn: sqlite3.Connection, name: str, phone: str, password: str
) -> int:
    """不走邀请链接直接建一个成员，返回用户 id。测试与运维脚本用。"""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO users (name, phone, password_hash, role, created_at, last_login_at)"
        " VALUES (?, ?, ?, 'member', ?, ?)",
        (name, phone, hash_password(password), now, now),
    )
    return int(cur.lastrowid)


def login(request, user_id: int) -> None:
    request.session["uid"] = user_id


def logout(request) -> None:
    request.session.clear()


def current_user(request, conn: sqlite3.Connection) -> sqlite3.Row | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return conn.execute(
        "SELECT * FROM users WHERE id = ? AND deactivated_at IS NULL", (uid,)
    ).fetchone()


def try_login(conn: sqlite3.Connection, phone: str, password: str) -> tuple[bool, dict | str]:
    """登录：成功返 (True, {id, name, role})，失败返 (False, 人话)。限速在这里，不在路由里。"""
    normalized = normalize_phone(phone)
    if normalized is None:
        return False, "手机号看起来不对，检查一下"

    remaining = throttle.locked_seconds(normalized)
    if remaining:
        return False, f"密码错太多次了，等 {remaining // 60 + 1} 分钟再试"

    user = authenticate(conn, normalized, password)
    if user is None:
        throttle.register_failure(normalized)
        return False, "手机号或密码不对"

    throttle.register_success(normalized)
    from . import access  # 局部导入：access 只依赖 site_settings，这里避免 import 期成环
    return True, access.identity_payload(user)


def invite_state(conn: sqlite3.Connection, token: str) -> tuple[dict | None, str | None]:
    """返回 (邀请, 问题)。问题为 None 表示这条链接可用，"used" 表示已经有人用过了。"""
    row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None, "这个链接不存在，找管理员再要一个"
    invite = dict(row)
    if invite["revoked_at"]:
        return invite, "这个链接已经作废了，找管理员再要一个"
    if invite["used_count"] >= 1:
        return invite, "used"
    return invite, None


def resolve_invite(conn: sqlite3.Connection, token: str) -> dict | None:
    """链接还在（没被作废）就返这条邀请；不存在或已作废返 None。

    用没用过由调用方看 used_count——一次性语义在 register_with_invite 里把守。
    """
    invite, problem = invite_state(conn, token)
    if invite is None or problem not in (None, "used"):
        return None
    return invite


def mark_invite_opened(conn: sqlite3.Connection, invite_id: int) -> None:
    """记下这条邀请链接第一次被打开的时间（裁定 R12：只此一处）。

    管理员的成员页靠这一列区分"还没点开"，所以只写一次：条件写在 WHERE 里，
    谁先到谁的时间留下，后来者整条 UPDATE 命中 0 行、什么都不改。api 与 HTML
    两个面共用这一条，删旧路由时不会漏掉一份。
    """
    conn.execute(
        "UPDATE invites SET first_opened_at = ?"
        " WHERE id = ? AND first_opened_at IS NULL",
        (utcnow(), invite_id),
    )


USE_ALREADY_TEXT = "这条链接已经用过了，直接用手机号和密码登录"
PHONE_TAKEN_TEXT = "这个手机号已经被别人用了，换一个，或者找管理员"
ACCOUNT_DISABLED_TEXT = "这个手机号对应的账号已停用，找管理员恢复"
# 重设链接填了一个库里没有的手机号：这不是"要开新号"，是这条链接只管原来那个账号。
# 2026-10-23 线上 陈旭豪 / 王欣阳 / 张语纯 各自多出一本账号，就是这句话没拦住：
# 手机号找不到人时旧代码一路滑到下面的 INSERT，把重设链接当成了注册链接。
RESET_PHONE_MISMATCH_TEXT = (
    "手机号对不上：这条链接是给「{name}」的账号换密码的，要填这个账号原来登录用的手机号。"
    "要是这个号已经不用了，找管理员重新发一条邀请"
)
RESET_TARGET_GONE_TEXT = "这条链接对应的账号已经不在了，找管理员重新发一条"


def _claim_invite(conn: sqlite3.Connection, invite_id: int, now: str) -> bool:
    """原子占用一次性邀请：只有 used_count 还是 0 的那一次能抢到。

    先 SELECT 再 INSERT/UPDATE 的老写法在并发下两个请求都能过（都读到 0），于是同一条
    链接被用两次。把判据写进 WHERE 里之后，SQLite 的写锁保证只会有一个 UPDATE 命中
    1 行；输家 rowcount==0，按"已经用过了"拒绝。调用方（路由）只在成功时提交，
    抢到之后后续步骤失败会随请求事务一起回滚。
    """
    cur = conn.execute(
        "UPDATE invites SET used_count = used_count + 1, first_used_at = ?"
        " WHERE id = ? AND used_count = 0",
        (now, invite_id),
    )
    return cur.rowcount == 1


def _reset_target_user(conn: sqlite3.Connection, invite: dict) -> sqlite3.Row | None:
    """这条 reset 链接绑定的账号。

    新链接在 invites.user_id 上绑死了人（迁移 0008）：改名不会让旧链接失效，同名不同人
    也不会串。迁移前的老行 user_id 是 NULL，退回按姓名找——但必须**唯一命中**才算：
    成员表允许同名不同人（团队里真有），猜错人等于把别人的密码改掉，宁可让本人去找
    管理员重发。
    """
    bound = invite.get("user_id")
    if bound is not None:
        return conn.execute("SELECT * FROM users WHERE id = ?", (bound,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM users WHERE name = ?", (invite["name"],)
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def register_with_invite(
    conn: sqlite3.Connection, token: str, phone: str, password: str, confirm: str
) -> tuple[bool, dict | str]:
    """凭邀请链接注册成员；本人拿着重设链接来时只换密码。

    成功返 (True, {id, name, role, reset})，失败返 (False, 人话)。
    链接一次性：先原子占位（_claim_invite），再建号/改密码；链接用过了就 400 人话。

    两种用途是**两条互斥的路**，reset 这条永远不建号：
      register → 手机号没人占用才 INSERT 一个新成员；
      reset    → 只改 _reset_target_user 绑定的那本账号的密码。手机号必须正好是
                 那本账号的号，否则 400（不清耗链接，本人可以拿对的号再试一次）。
    旧写法把 reset 分支藏在 `clash is not None` 里面，手机号打错/打成一个新号时
    直接落到 register 那条 INSERT 上——同一本账号的第二次注册，正是线上重复成员的根因。
    """
    invite, problem = invite_state(conn, token)
    if problem == "used":
        return False, USE_ALREADY_TEXT
    if problem is not None:
        return False, problem

    normalized = normalize_phone(phone)
    if normalized is None:
        return False, "手机号看起来不对，检查一下"
    if password != confirm:
        return False, "两次输入的密码不一样"
    if len(password) < 6:
        return False, "密码至少 6 位"

    now = utcnow()
    is_reset = invite["purpose"] == "reset"
    target = _reset_target_user(conn, invite) if is_reset else None
    if is_reset and target is None:
        return False, RESET_TARGET_GONE_TEXT

    # 按手机号找人**不加 deactivated 过滤**：停用账号仍占着 users.phone 的 UNIQUE，
    # 滤掉它去 INSERT 只会撞唯一约束变成 500。停用的人该收到一句"找管理员恢复"。
    clash = conn.execute(
        "SELECT * FROM users WHERE phone = ?", (normalized,)
    ).fetchone()

    if is_reset:
        if clash is None:
            # 库里没有这个号：不建号、不消耗链接。重设链接不是注册链接。
            return False, RESET_PHONE_MISMATCH_TEXT.format(name=invite["name"])
        if clash["deactivated_at"]:
            return False, ACCOUNT_DISABLED_TEXT
        if int(clash["id"]) != int(target["id"]):
            # 填的是别人的号（同名不同人也算）：别动任何账号的密码。
            return False, PHONE_TAKEN_TEXT
        if not _claim_invite(conn, invite["id"], now):
            return False, USE_ALREADY_TEXT
        conn.execute(
            "UPDATE users SET password_hash = ?, last_login_at = ? WHERE id = ?",
            (hash_password(password), now, clash["id"]),
        )
        from . import access
        payload = access.identity_payload(clash)
        payload["reset"] = True
        return True, payload

    if clash is not None:
        if clash["deactivated_at"]:
            return False, ACCOUNT_DISABLED_TEXT
        return False, PHONE_TAKEN_TEXT

    if not _claim_invite(conn, invite["id"], now):
        return False, USE_ALREADY_TEXT
    try:
        cur = conn.execute(
            "INSERT INTO users (invite_id, name, phone, password_hash, role, created_at,"
            " last_login_at) VALUES (?, ?, ?, ?, 'member', ?, ?)",
            (invite["id"], invite["name"], normalized, hash_password(password), now, now),
        )
    except sqlite3.IntegrityError:
        # 并发同一手机号：两个不同的邀请各自抢到了自己的名额，后插的那个人撞
        # users.phone UNIQUE。翻成人话 400；请求事务回滚时它的邀请占位也一起回滚。
        return False, PHONE_TAKEN_TEXT
    return True, {
        "id": int(cur.lastrowid), "name": invite["name"], "role": "member",
        "can_build_wiki": True, "reset": False,
    }


# --- 成员管理（Task 9 从旧 routers/admin.py 下沉，语义原样保留） ----------------


def create_invite(conn: sqlite3.Connection, name: str, created_by: int) -> str:
    """发一条注册邀请，返回 token。"""
    token = new_invite_token()
    conn.execute(
        "INSERT INTO invites (token, name, purpose, created_by, used_count, created_at)"
        " VALUES (?, ?, 'register', ?, 0, ?)",
        (token, name, created_by, utcnow()),
    )
    return token


def revoke_invite(conn: sqlite3.Connection, invite_id: int) -> None:
    """作废一条邀请链接。幂等：已经作废的不再动它的时间戳。"""
    conn.execute(
        "UPDATE invites SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (utcnow(), invite_id),
    )


def deactivate(conn: sqlite3.Connection, user_id: int, actor_id: int) -> bool:
    """停用（= 移除）一个成员；历史消息、引注、版本全部保留。

    不能停用自己，也不能停用管理员（WHERE role='member' 把守，与旧 admin 路由同语义）。
    停不成一律返 False，路由翻 400。
    """
    if user_id == actor_id:
        return False
    cur = conn.execute(
        "UPDATE users SET deactivated_at = ? WHERE id = ? AND role = 'member'",
        (utcnow(), user_id),
    )
    return cur.rowcount > 0


def rename_member(conn: sqlite3.Connection, user_id: int, name: str) -> bool:
    """改名。空名字不动库、返 False（路由翻 400）。

    邀请链接上的名字跟着一起改：成员页与 /api/invites/{token} 显示的是同一个姓名，
    两边漂了会对不上人。
    """
    if not name:
        return False
    conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
    conn.execute(
        "UPDATE invites SET name = ? WHERE id = (SELECT invite_id FROM users WHERE id = ?)",
        (name, user_id),
    )
    # 重设链接现在按 user_id 绑人（迁移 0008），改名不再影响它可用；把显示名一起改掉，
    # 旧链接的落地页才不会还摆着改名前的姓名。
    conn.execute("UPDATE invites SET name = ? WHERE user_id = ?", (name, user_id))
    return True


def create_reset_invite(conn: sqlite3.Connection, user_id: int, created_by: int) -> str | None:
    """给某个人发一条重设密码的链接；查无此人返 None（路由翻 404）。"""
    person = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if person is None:
        return None
    token = new_invite_token()
    conn.execute(
        "INSERT INTO invites (token, name, purpose, created_by, used_count, created_at,"
        " user_id) VALUES (?, ?, 'reset', ?, 0, ?, ?)",
        (token, person["name"], created_by, utcnow(), int(person["id"])),
    )
    return token
