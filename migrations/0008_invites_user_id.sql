-- 0008_invites_user_id.sql：invites 加 user_id（S6b 修复 reset 链接按姓名绑定的 bug）。
--
-- 为什么：重设口令的链接原来只带 name；核对"是不是本人"靠 users.name 相等，于是
-- 改一次名就让已发出的链接失效，而同名不同人还能互相串。加 user_id 之后 reset 分支
-- 按 user_id 绑人；迁移前的老行是 NULL，读侧退回原来的姓名匹配（兼容，不丢老链接）。
--
-- 只给 user_id 非空的 reset 邀请用；注册邀请的 user_id 保持 NULL（那时人还没建）。
-- ADD COLUMN + REFERENCES 可空列是 SQLite 支持的轻量改法（不重建表、不动老数据）。

ALTER TABLE invites ADD COLUMN user_id INTEGER REFERENCES users(id);
