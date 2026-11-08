"""RunHooks：工具跑之前挡一道白名单，跑之后把这一次工具调用记进 agent_events。

裁定 R-S2-9 把"工具事件落库"这件事从 runner 的流事件搬运里挪到 hooks 上：流事件
（RunItemStreamEvent）要等 SDK 把一轮跑攒出 item 才来，而 hooks 是贴着工具调用的
前后各一次，一一对应、不会漏也不会重。SSE 尾读（Task 5）就从这张表按
(session_id, seq) 拼帧。

SDK 0.24.0 交进来的入参（实证在 agents/run_internal/tool_execution.py）：
`_execute_single_tool_body` 调 `on_tool_start(tool_context, agent, func_tool)`，
tool_context 是 ToolContext（runner 的 `context=` 那个对象挂在 `.context` 上）；
调用结束调 `on_tool_end(tool_context, agent, func_tool, final_result)`，final_result
就是工具函数的返回值本身（我们五个工具一律返 str，类型标的是 object，所以还是兜一手）。

两条硬规矩：

1. **记账失败绝不打断这一轮跑。** agent_events 是给人看的旁路，库锁死、表被人删、
   连接被关，都只该在服务端日志里留一条 warning；模型该调的工具照调，人该拿到的
   回复照拿。所以 _record 整个包在 try/except Exception 里（CancelledError 是
   BaseException，不在此列——取消照旧能穿过去）。
2. **白名单那一道不能被上一条吞掉。** 工具名不在 TOOL_NAMES 里就抛中文 RuntimeError，
   抛在 try 之外，让 runner 的 catch-all 去兜（记一条 error 事件 + 封口）。这是第三道
   防线：模型只拿到这五个工具（Agent(tools=WIKI_TOOLS)），store 与工具内部各有一道
   形状闸；这一道防的是"装配错了/将来加了工具忘了改白名单"，顺带把企图记进日志。

seq 不数在对象上：`WikiHooks()` 是无状态的，seq 由 append_event 在库里按会话原子分配
（与 messages.seq 同一个手法，裁定 R68）。这样同一个 hooks 实例被两轮跑共用、或两轮跑
并发，都不会把编号数错——计划里 `self.seq = itertools.count(1)` 那写法在并发下会撞
UNIQUE(session_id, seq)。

fix round 1 起（裁定 R-S2-20/21）：事件行还带 message_id（从 AgentContext 拿，这条
事件属于哪条 agent 消息，SSE 尾读据此划一轮的边界，不再靠秒级 created_at 猜）；
tool-call/tool-result 的 payload 带 SDK 的 tool_call_id（`"id"` 键，spec §6.5 的形状，
S3 的工具卡片按 id 配对 call/result）——探针实证见 _call_id 的 docstring。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Tool

from ..db import utcnow
from .context import AgentContext
from .tools import FAIL_PREFIX, TOOL_NAMES, WRITE_TOOL_NAMES

logger = logging.getLogger("openllmwiki.agent")

# agent_events.type 的 CHECK 白名单里，hooks 只写 tool-call 与 tool-result 这两个；
# system/error 是 runner 的（429 额度用尽、上游连不上、内部异常），runner 复用这里的
# append_event 写——事件落库的口子只此一个，别处不再拼 INSERT。
EVENT_TOOL_CALL = "tool-call"
EVENT_TOOL_RESULT = "tool-result"
EVENT_SYSTEM = "system"

SUMMARY_MAX = 200    # 一行事件里 summary 的截断长度（计划口径：前 200 字）
ARGS_MAX = 200       # tool-call 里 args 的截断长度（裁定 R-S2-12），与 summary 同口径

# ok 的判据：Task 2 定死的返回串约定——写路径失败一律以 tools.FAIL_TEXT 开头，成功以
# "已生效："开头，读路径失败是"读不到："。这里只认写路径那一段（裁定的口径）：读不到
# 不是这一轮写砸了，SSE 上仍按成功的一次工具调用显示。
# 那段前缀**不在这里写第二遍**（裁定 R-S2-14）：FAIL_PREFIX 从 tools 导入，与工具返回串
# 用的是同一个常量，所以改文案改不到 ok 的判据头上（两处各写一份时，改一半就让 ok 一路 True）。


def append_event(
    conn: sqlite3.Connection, session_id: int, event_type: str, payload: dict,
    db_lock: threading.Lock | None = None, message_id: int | None = None,
) -> int:
    """往 agent_events 追加一条，返回新行的 id（不是 seq）。

    message_id（裁定 R-S2-20）：这条事件属于哪条 agent 消息。hooks 从 AgentContext
    拿（runner 恒传），runner 自己记 system/error 时从 _Turn 拿；不传就落 NULL，
    SSE 尾读对 NULL 行退回 created_at 口径（legacy 兼容，见 chat._pending_events）。

    seq 用单条 `INSERT ... SELECT COALESCE(MAX(seq), 0) + 1 ... WHERE session_id = ?`
    原子分配，与 chat.append_human 完全同一个手法（裁定 R68）：SQLite 下单条写语句
    自始至终持写锁，读 MAX 与写 INSERT 之间插不进第二个写者，所以并发的两轮各自看到
    对方已提交的 seq，永不撞 UNIQUE(session_id, seq)。不带 GROUP BY 的聚合 SELECT
    恒返一行（这场会话还没有事件时 MAX 为 NULL，COALESCE 兜成 1），因此恰好插一行。

    自己 commit：runner 是后台任务，没有 get_conn 那道"请求结束才提交"的收尾，
    不 commit 的话 SSE 尾读（另一条连接）在这一轮跑完之前一条事件都看不到。
    与 Task 2 的工具自己 commit 是同一个理由，也继承同一个前提——**提交之后不可回滚**
    （裁定 R-S2-7）。

    db_lock（裁定 R-S2-17）：给了就先持锁再动连接。hooks._record 传 AgentContext 那把，
    runner._event 传 _Turn 那把（同一把）；这条连接上还有 relay 与工具体两个写者，
    不持锁就可能撞出 "cannot start a transaction within a transaction"。不传则不锁
    ——调用方（测试脚本一类，单线程自己写自己读）自己负责。
    """
    with db_lock or contextlib.nullcontext():
        cur = conn.execute(
            "INSERT INTO agent_events"
            " (session_id, seq, type, payload_json, created_at, message_id)"
            " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?"
            " FROM agent_events WHERE session_id = ?",
            (session_id, event_type, json.dumps(payload, ensure_ascii=False), utcnow(),
             message_id, session_id),
        )
        conn.commit()
    return int(cur.lastrowid)


def _agent_context(context: RunContextWrapper[Any]) -> AgentContext:
    """从 RunContextWrapper 里取出 runner 交进来的 AgentContext；不是就报错。

    报错只在 _record 里被翻译成一条 warning（这一轮跑照旧），所以这里敢直说：
    装配错了要能在日志里一眼看出来，而不是悄悄记到 session_id=None 上。
    """
    value = getattr(context, "context", None)
    if not isinstance(value, AgentContext):
        raise RuntimeError(
            f"runner 交进来的 context 不是 AgentContext（收到 {type(value).__name__}）"
        )
    return value


class WikiHooks(RunHooks):
    """一轮跑的工具事件记账员。无状态：一轮一个实例也行，共用一个也行。"""

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Tool
    ) -> None:
        name = getattr(tool, "name", "") or ""
        try:
            _agent_context(context).progress.setdefault("tools", set()).add(name)
        except Exception:
            pass
        if name not in TOOL_NAMES:
            # 先记日志再抛：抛出去之后 runner 只会看到这一句中文，运维要的是"谁调了什么"。
            logger.error(
                "智能体要调一个不在白名单里的工具（%r），白名单是 %s；这一轮到此为止",
                name, "、".join(TOOL_NAMES),
            )
            raise RuntimeError(
                f"智能体要调的工具 {name or '（连名字都没有）'} 不在白名单里"
                f"（只许 {'、'.join(TOOL_NAMES)}），这一轮到此为止"
            )
        # args 也记（裁定 R-S2-12）：S3 的工具卡片要显示"调了什么、参数是什么"，而这是
        # 唯一能拿到原始参数的时机（on_tool_end 那边只有返回值）。截到 ARGS_MAX 字——
        # write_page 的参数里可能是整页正文，落库不值当；截断只影响卡片上的显示，
        # 真跑用的是 SDK 手里那份完整的。
        # 没有参数串就整个键不记（无参工具上游可能交进来一个空串）：留 `{"name": …}`
        # 一种形状，前端 `"args" in payload` 一判就够，不用再判一次"值是不是空的"。
        payload: dict[str, Any] = {}
        call_id = self._call_id(context)
        if call_id:
            payload["id"] = call_id       # spec §6.5：tool-call{id,name,args}（裁定 R-S2-21）
        payload["name"] = name
        args = getattr(context, "tool_arguments", "") or ""
        if args:
            payload["args"] = str(args)[:ARGS_MAX]
        self._record(context, EVENT_TOOL_CALL, payload)

    async def on_tool_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Tool, result: object
    ) -> None:
        # result 的类型标的是 object：结构化输出的工具可以返别的形状。我们五个一律返
        # str，这一手只为"哪天换了工具也不至于把整轮跑炸在 hooks 里"。
        text = result if isinstance(result, str) else str(result or "")
        payload: dict[str, Any] = {}
        call_id = self._call_id(context)
        if call_id:
            # tool-result 也带 id（spec §6.5：tool-result{id,name,ok,summary}）：SDK 交给
            # on_tool_end 的是**同一个** ToolContext（实证见模块 docstring），call_id 拿
            # 得到，S3 的工具卡片就能按 id 配对 call/result，不用按"名字 + 出现顺序"猜。
            payload["id"] = call_id
        payload.update({
            "name": getattr(tool, "name", "") or "",
            "ok": not text.startswith(FAIL_PREFIX),
            "summary": text[:SUMMARY_MAX],
        })
        self._record(context, EVENT_TOOL_RESULT, payload)
        # 写路径成功 = 这一轮动过 wiki。runner 收尾时据此决定要不要跑一次 lint / 修复轮
        # （没动过 wiki 的回合不该为一个"查一句话"付 lint 的代价）。标记失败不算数：
        # 没写进去就没有断链需要修。
        name = getattr(tool, "name", "") or ""
        if name in WRITE_TOOL_NAMES and not text.startswith(FAIL_PREFIX):
            try:
                progress = _agent_context(context).progress
                progress["wrote"] = True
                match = re.search(r"path=([^\s（]+)", text)
                if match:
                    progress.setdefault("paths", set()).add(match.group(1))
            except Exception as exc:   # noqa: BLE001 —— 便签失败同样不许拖垮这一轮
                logger.warning("写路径进度没记上（tool=%r）：%r", name, exc)

    @staticmethod
    def _call_id(context: RunContextWrapper[Any]) -> str:
        """SDK 交进来的工具调用 id（ToolContext.tool_call_id）。

        探针实证（裁定 R-S2-21，SDK 0.24.0）：`ToolContext.tool_call_id` 是**必填**
        字段（不传直接 ValueError，见 agents/tool_context.py 的
        `_assert_must_pass_tool_call_id`），值取自上游 tool_call 的 id
        （run_internal/tool_execution.py 的 `extract_tool_call_id`），且 on_tool_start
        与 on_tool_end 拿的是同一个 ToolContext——两头都拿得到，call/result 按 id 配对
        成立。getattr 兜一手只为"交进来的不是 ToolContext"时不炸在 hooks 里（记账失败
        不许打断这一轮跑），那种形状下 id 键整个不记。
        """
        return str(getattr(context, "tool_call_id", "") or "")

    def _record(self, context: RunContextWrapper[Any], event_type: str, payload: dict) -> None:
        """写一条事件；写不成就只记日志。**这个 except 是承重的，别收窄。**"""
        try:
            agent_ctx = _agent_context(context)
            append_event(agent_ctx.conn, agent_ctx.session_id, event_type, payload,
                         db_lock=agent_ctx.db_lock, message_id=agent_ctx.message_id)
        except Exception as exc:   # noqa: BLE001 —— 旁路记账不许拖垮这一轮跑
            logger.warning(
                "agent_events 没记上（type=%s，payload=%r，actor=%r）：%r；工具照跑、这一轮不断",
                event_type, payload,
                getattr(getattr(context, "context", None), "actor_name", "?"), exc,
                exc_info=exc,
            )
