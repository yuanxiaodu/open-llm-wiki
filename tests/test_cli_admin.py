"""`app.cli admin`：手机号升/降管理员；未注册时建号并发一次性设密链接。"""

from __future__ import annotations

from argparse import Namespace

from app.cli import set_admin
from app.db import connect, utcnow
from app.services.auth import hash_password


def _ns(**over):
    base = dict(phone="18900000000", name=None, password=None, base_url="http://testserver", demote=False)
    base.update(over)
    return Namespace(**base)


def _add_user(phone: str, role: str = "member") -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, phone, password_hash, role, created_at, last_login_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("某人", phone, hash_password("pass-123"), role, utcnow(), utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_promotes_existing_member_and_reactivates(client, capsys):
    uid = _add_user("18900000001")
    conn = connect()
    conn.execute("UPDATE users SET deactivated_at = ? WHERE id = ?", (utcnow(), uid))
    conn.commit()
    conn.close()
    set_admin(_ns(phone="18900000001"))
    conn = connect()
    row = conn.execute("SELECT role, deactivated_at FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    assert row["role"] == "admin"
    assert row["deactivated_at"] is None
    assert "已设为管理员" in capsys.readouterr().out


def test_demote_existing_member(client, capsys):
    uid = _add_user("18900000002", role="admin")
    set_admin(_ns(phone="18900000002", demote=True))
    conn = connect()
    role = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()["role"]
    conn.close()
    assert role == "member"
    assert "已降为成员" in capsys.readouterr().out


def test_creates_missing_admin_with_one_time_password_link(client, capsys):
    set_admin(_ns(phone="18900000003", name="新管理员"))
    conn = connect()
    user = conn.execute("SELECT * FROM users WHERE phone = '18900000003'").fetchone()
    assert user is not None and user["role"] == "admin"
    invite = conn.execute(
        "SELECT * FROM invites WHERE purpose = 'reset' AND user_id = ?", (user["id"],)
    ).fetchone()
    conn.close()
    assert invite is not None and invite["used_count"] == 0
    out = capsys.readouterr().out
    assert "admin" not in out.lower() or "管理员" in out
    assert "http://testserver/i/" in out
