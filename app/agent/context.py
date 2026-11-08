"""一轮智能体跑的上下文：连接 + 会话编号 + 触发这一轮的人 + 护住连接的那把锁。

runner（Task 4）把它当 `Runner.run_streamed(..., context=AgentContext(...))` 交进去，
SDK 原样塞进 RunContextWrapper 的 `.context`（裁定 R-S2-2 已用探针实证注入成立），
于是**工具与 hooks 从同一处**取到同几样东西：

- `conn`：工具的读写、hooks 记 agent_events 都用它；
- `session_id`：hooks 记事件要它（agent_events.seq 是按会话数的）；
- `actor_name`：触发这一轮的人（姓名）。写路径工具（建/改/删页、网页摄入）把它当成
  提交主题里的 actor，日志里能一眼看出这串连锁动作是谁引起的；只有拿不到姓名时
  tools._actor_label 才回退 tools.AGENT_ACTOR；
- `message_id`（裁定 R-S2-20）：这一轮那条 agent 消息的 id。hooks 把它写进
  agent_events.message_id，SSE 尾读据此把事件精确归到一轮——不再靠秒级的
  created_at 划边界（同秒的两轮会串尾巴）。None 时事件行的 message_id 落 NULL，
  尾读对 NULL 行退回 created_at 口径（legacy 兼容）；真跑时 runner 恒传（先开口
  再装配，_build_agent 拿得到 id），只有测试里裸造 AgentContext 才会是 None；
- `db_lock`（裁定 R-S2-17）：护住上面那条连接的**同一把**锁。relay
  （_append_text/_seal/_event）跑在事件循环线程，工具体跑在 asyncio.to_thread 的
  worker 线程，hooks 两头都掺和——max_function_tool_concurrency=1 只把工具与工具
  之间串起来，管不了 relay 与工具这条缝。所有写这条连接的路径都先持锁再动它，
  否则同一条 sqlite 连接上会冒出 "cannot start a transaction within a transaction"，
  而且现象随线程调度漂。7 人规模，串行的代价无感。
- `can_build_wiki`（2026-10-30）：触发这一轮的人有没有建设权限（摄入材料 + 建/改/删页，
  管理员恒有）。runner 从路由交来的 actor dict 里取，工具体据此拒绝越权动作；默认
  True 只为裸造上下文的测试/脚本保持旧行为（见 tools._can_build）。
- `message_owner_id`（2026-10-30）：对话可见范围是 `own` 时，搜索的消息命中只许出现
  这个 user 的会话（None = 不过滤，`all` 模式与管理员都是这个值）。tools 的 search
  把它交给 services.search；默认 None 与迁移前完全一致。

为什么不把裸 conn 当 context 交进去：那样 hooks 就得自己再从构造函数里拿一份
session_id，同一个事实两处存着，装配时漏一个就是"事件记到别的会话里"这种难查的错。
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AgentContext:
    """runner 交给 SDK 的 context 对象。前三个字段是这一轮跑的属性，不给默认值。

    message_id 带默认值 None 只为"测试里裸造 AgentContext（只测工具那一侧）"不用陪绑；
    真跑时 runner 恒传（见模块 docstring）。

    db_lock 带 default_factory 只为装配漏传时不炸；真跑时 runner 传的是与 _Turn
    共用的**同一把**（run_turn 里现造），漏传的话工具与 relay 各锁各的，等于没锁。

    progress 是"这一轮干过什么"的便签（本次新增，随 runner 的生命周期走）：
    hooks 在写路径工具成功时置 `wrote=True`，runner 在收尾时据此决定要不要再跑一次
    lint / 修复轮。用可变 dict 而不是加字段，是因为 AgentContext 是 frozen：加字段
    要在装配处一个个传，而这张便签是**同一轮内**的瞬时状态，不该成为装配契约的一部分。

    can_build_wiki / message_owner_id 都是"触发这一轮的人"的能力与可见范围快照
    （2026-10-30）：前者决定工具能不能改库，后者决定 search 里看不看得到别人的会话。
    两个都带默认值，裸造上下文的旧测试不用陪绑。
    """

    conn: sqlite3.Connection
    session_id: int
    actor_name: str
    message_id: int | None = None
    db_lock: threading.Lock = field(default_factory=threading.Lock)
    progress: dict[str, Any] = field(default_factory=dict)
    can_build_wiki: bool = True
    message_owner_id: int | None = None
    user_id: int | None = None

