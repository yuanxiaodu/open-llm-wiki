"""一轮智能体跑的驱动层：把 SDK 的流式事件落成库里的行。

路由（Task 5）在 POST /api/chat/{id}/messages 把人话落库之后，把 `run_turn` 丢给
asyncio.create_task；SSE 端点从**另一条连接**尾读两张表：

- `messages` 里那条 role='agent' 的行：正文随 text-delta 一段段长出来；
- `agent_events`：工具事件由 hooks 写（裁定 R-S2-9），system/error 由这里写。

所以这个文件只管"数据面"，一帧 SSE 都不发。三条硬规矩：

1. **写一处，别写两处。** _relay 只认正文增量；tool-call/tool-result 一律交给
   WikiHooks（on_tool_start/on_tool_end 贴着工具调用，一一对应）。这里再按
   RunItemStreamEvent 写一遍，每个工具事件就都是两条（R-S2-9 的交接结论）。
2. **每写一点就提交。** runner 的连接是 `db.connect()`（默认隔离级别，**不是**
   autocommit，S1/T3 的裁定），而尾读是另一条连接：不 commit 的话，一整轮跑完之前
   前端一个字都看不见。提交粒度 = 一段 text-delta 一次、封口一次；hooks 与工具体
   各自也是写完就提交（同一个理由）。代价是提交之后不可回滚（裁定 R-S2-7）。
3. **跑完之后不许留下"没封口"的行。** usage_json IS NULL AND truncated=0 是
   "这一轮还在跑"的唯一记号，尾读据此决定要不要发 done。所以每一条出口
   （正常收尾、429、上游断流、超轮、内部异常、被取消）都要封口；只有"进程被杀"
   这一种封不了，那由启动时的 db.seal_orphan_agent_messages 兜（N9）。
   封口必须是一轮的**最后一次写**（裁定 R-S2-18）：done 判据一满足，尾读随时可能
   退出，解释性的 system/error 行得先落库（_fail 的顺序就是为这个）。
4. **写这条共享连接之前先持 db_lock**（裁定 R-S2-17）。relay 在事件循环线程，
   工具体在 asyncio.to_thread 的 worker 线程；_open_agent_message/_append_text/
   _seal/_event、hooks.append_event 与五个工具体拿的是 AgentContext（与 _Turn）
   上同一把锁，谁也不许裸写。

两处与派遣单/简报口径不同、以探针实证为准的地方（详见 task-4-report.md）：

- `RawResponsesStreamEvent.data` 不是 openai 的 ChatCompletionChunk，而是 Responses
  形状的语义事件。正文增量是 `ResponseTextDeltaEvent`（`.delta` 是 str）；工具参数
  增量是 `ResponseFunctionCallArgumentsDeltaEvent`，它**也有** `.delta`。所以 _relay
  按类型认，不按属性名认——只认属性名会把工具参数当正文追加进回复里。
- `RunConfig` 的字段叫 `tool_execution`（不是 `tool_execution_config`，后者 TypeError）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

import openai
from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.run_config import RunConfig, ToolExecutionConfig
from agents.stream_events import RawResponsesStreamEvent
from openai.types.responses import ResponseTextDeltaEvent

from ..db import utcnow
from ..config import settings
from ..services import chat as chat_service, git_sync
from ..services.llm import make_model, model_settings
from ..wiki import lint as wiki_lint
from ..wiki_config import get_config
from ..wiki import store
from .context import AgentContext
from .hooks import EVENT_SYSTEM, WikiHooks, append_event
from .prompt import BASE_STYLE, SKILL_VERSION
from .tools import WIKI_TOOLS, lint_errors

logger = logging.getLogger("openllmwiki.agent")

AGENT_NAME = "wiki"        # Agent(name=…)：SDK 内部与 tracing 里认的就是这个名字
WORKFLOW_NAME = "wiki 智能体"
MAX_TURNS = 30             # 2026-10-30 大幅上调：7 轮在长查询/整理上总被工序吃满
# 带附件的摄入轮单独给预算：工序是"定位 → 读材料 → 搜 → 批量建页 → 补链 → lint →
#（有错再修）→ 报告"，长材料需要更多轮。这里按"有没有 [附件]"判，不按页数猜——
# 材料大小/页数只有模型读到才知道。
INGEST_MAX_TURNS = 40
# 动手类请求的预算（2026-10-30 会话 28 实测 12 轮仍不够）：执行/继续/整理/合并/
# 治理这类请求给更长预算，让它真的写到页并收尾，而不是用完轮次只说"再调一次就行"。
TASK_MAX_TURNS = 50
# 只认"动手"的词；"做"太宽（"怎么做产品"不该白拿任务档预算），不列。
_TASK_HINTS = (
    "执行", "继续", "整理", "合并", "枢纽", "体检", "治理", "修复",
    "孤立页", "全部", "按这个", "同意", "建页", "改页", "删页",
)
# 收尾修复轮的预算：主轮跑完/超轮之后，若这一轮动过 wiki 且 lint 还有 error，
# 再给一次"只修 error"的机会。read+edit+lint 多轮修链时就多给几轮。
REPAIR_TURNS = 6
HISTORY_MAX = 24           # 喂给模型的最近多少条人话+智能体回复
# 历史的字符预算（裁定 R-S2-31）：按新→旧累加，累满就停。条数上限管不住字数——
# 24 条每条都顶到 search.MAX_BODY（20000 字）就是 48 万字，任何窗口都装不下，上游
# 只会回一句 400 context length。截掉的是**最老**的那几条（模型要用旧材料还有
# search/read 两个工具），最新的一条永远保留。
HISTORY_CHAR_BUDGET = 60000

# 回合开始的自动同步失败（2026-11-13）：冲突/脏工作区时不硬跑，让人先处理 git。
SYNC_TEXT = (
    "知识库仓库同步失败，这一轮没有开始：{detail}。"
    "请先处理 wiki 仓库的 git 状态（冲突/未提交改动），再重发这条消息。"
)

# 收尾那一句：超轮之后不带工具再要一次，模型往往就乖乖给一句人话（计划 §19）。
WRAP_UP_TEXT = (
    "工具预算用完了，不要再调用工具。现在直接回答提问者最后的问题：先给结论与已经查到的"
    "可用内容，再简短说明哪一点没查到；不要只汇报过程，也不要提“还没写成页 / 本轮未改动”"
    "这类内部状态。"
)

# 几条兜底文案。都是给人看的（SSE 上原样显示），所以一律中文、不带异常类型与
# traceback——那些只进服务端日志。
QUOTA_TEXT = "额度暂时用完，你说的和传的都已保存。这一条回复没写出来，等额度回来重发一次。"
UPSTREAM_TEXT = "智能体暂时连不上，你说的和传的都已保存。已经流出来的半截留在上面，稍后重发一次。"
MAX_TURNS_TEXT = "这一轮工具调得太多次没收住，先停在这里。已经写进去的内容都留着，换个说法再试一次。"
BEHAVIOR_TEXT = "模型这一轮没按规矩出牌，你说的和传的都已保存。换个说法再试一次。"
INTERNAL_TEXT = "智能体这一轮出错了，你说的和传的都已保存。请稍后再试一次。"
CANCELLED_TEXT = "这一轮回复被取消了，已经流出来的部分留在上面。"
# 上下文超限（裁定 R-S2-31）：400 且报文里带 context/length 字样的那种。它不归并进
# UPSTREAM_TEXT——"连不上，稍后重发"叫人原样重试，而原样重试对窗口超限**必然**再撞
# 同一堵墙（历史只会长不会短）；出这句叫人开新会话或缩小范围，才是能走通的动作。
CONTEXT_TEXT = "这一会话太长了，装不进模型的窗口。开一场新会话，或让我只检索不带头脑再试一次。"

# error 事件的 code 词汇表（M18，S3 的前端可以按它 switch 渲染）：
#
#   upstream   上游故障：状态码非 429/402、非上下文超限的 APIStatusError、连不上、断流、超时
#              （文本可能是 UPSTREAM_TEXT 或 CONTEXT_TEXT，按 text 区分，code 相同）
#   internal   我们自己的异常（装配炸了、没接住的 bug），traceback 只进日志
#   max_turns  工具轮烧穿了 MAX_TURNS 且收尾那一次也没要到人话
#   behavior   ModelBehaviorError：模型没按规矩出牌（换说法才有出路的那种）
#   cancelled  这一轮在进程内被取消（CancelledError）
#   stream_timeout  不在这个文件里：SSE 尾读（chat._tail）等一轮等穿了墙钟上限时
#              **直接发进流**、不落 agent_events（那时可能压根没有 agent 行可归属）
#
# 额度用尽（429 速率/日额度、402 余额不足）**不在**这张表里：它走 type=="system" 的事件
# （spec §8，payload 只有 {"text": …}，code 被刻意丢掉），前端按 type==system + 文案识别
# "待重试"，别按 code 找。

# 封口写进 usage_json 的那个空对象。S2 拿不到 token 用量（SDK 的流式结果不给，
# 计划 Global Constraints），所以它**不是**用量，而是"这一轮跑完了"的记号：
# 尾读的收尾判据是 `usage_json IS NOT NULL OR truncated=1`（Task 5），
# 而启动封口只挑 `usage_json IS NULL AND truncated=0`（db.seal_orphan_agent_messages）——
# 记号写成 NULL 的话，跑完了的行会被下次启动当成"半截"再补一次「（回复中断）」
# （那个标记的字面量在 db.SEAL_MARK，这里不写第二遍）。
#
# 于是一行 agent 消息只可能是这三种形状之一，**区分"跑完了"与"断了"看 truncated，
# 不看 usage_json 是不是 NULL**（T4a 报告 §4 末那条口径提醒，Task 5 的尾读照此写）：
#
#   usage_json='{}'  truncated=0  这一轮正常收尾（本文件 _seal 的默认）
#   usage_json=NULL  truncated=1  这一轮失败了，runner 自己封的口（本文件 _fail）
#   usage_json='{}'  truncated=1  进程被杀留下的半截，下次启动由 db 那边封的口
DONE_USAGE = "{}"


@dataclass
class _Turn:
    """一轮跑的"随身账本"：连接、会话、那条 agent 消息、喂进去的上下文。

    db_lock 与 AgentContext.db_lock 是**同一把**（run_turn 里现造，两边都交进去，
    裁定 R-S2-17）：relay 在这条事件循环线程上写 conn，工具体在 to_thread 的 worker
    线程上写同一条 conn，锁不共用的话等于各锁各的。
    """

    conn: sqlite3.Connection
    session_id: int
    db_lock: threading.Lock
    message_id: int
    history: list[dict[str, str]]


# --- 对外的那一个入口 ---------------------------------------------------------


async def run_turn(
    conn: sqlite3.Connection,
    session_id: int,
    actor: dict[str, Any],
    *,
    max_turns: int = MAX_TURNS,
) -> None:
    """后台任务：跑一轮智能体，把正文与事件落库。**不抛异常。**

    人类消息已由路由落库（append_human），这里只补那条 agent 行。conn 是调用方
    （Task 5：路由用 `db.connect()` 现开一条）交进来的**独立**连接，不是请求那条
    ——S1 审查立下的规矩：请求连接不交给后台任务。这个函数不关它，谁开谁关。

    actor 是触发这一轮的人（{"id": …, "name": …}）：name 进 AgentContext 供日志与
    hooks 用；agent 行的 user_id 一律留 NULL（这条回复不是谁说的，是智能体说的）。
    2026-10-30 起 actor 还带两个能力/可见性快照：`can_build_wiki`（缺省 True）与
    `message_owner_id`（缺省 None），由路由按 access 的两个判据算好交进来——runner
    只负责透传进 AgentContext，不在这里再查一遍库（判据只留 access 一处）。

    为什么不抛：它跑在 asyncio.create_task 里，抛出去只会变成一条
    "Task exception was never retrieved"，人什么也看不见，而那条 agent 行还张着嘴
    等一个不会来的结尾。所以所有出口都在这里封好口、记好日志。
    """
    actor_name = str(actor.get("name") or "")
    can_build_wiki, message_owner_id = _turn_permissions(actor)
    # 这一轮的**同一把**锁（裁定 R-S2-17）：_Turn 与 AgentContext 都拿它，relay、
    # hooks、工具体三方写这条共享连接之前都先持锁（各处注释见下）。
    db_lock = threading.Lock()
    turn: _Turn | None = None
    try:
        # 先开口再装配：装配本身也可能炸（模型没配好、SDK 版本不对），那时候也得有
        # 一条行能封口、能挂上"这一轮出错了"的事件——先装配的话，_fail 手里连
        # message_id 都没有，前端就是一条永远停在"正在输入"的空气泡。
        turn = _Turn(
            conn=conn,
            session_id=session_id,
            db_lock=db_lock,
            message_id=_open_agent_message(conn, session_id, db_lock),
            history=[],
        )
        # 2026-11-13：回合开始先对齐远端，保证这一轮读到的库现状是新的。冲突/
        # 脏工作区就停在这里，不拿旧状态继续写。
        sync_report = await asyncio.to_thread(git_sync.pull_before_turn, conn)
        if not sync_report.get("ok"):
            detail = sync_report.get("error") or "未知原因"
            conflicted = sync_report.get("conflicted") or []
            if conflicted:
                detail += "（冲突文件：" + "、".join(conflicted) + "）"
            logger.warning("回合开始同步失败：%s", detail)
            _fail(turn, "sync", SYNC_TEXT.format(detail=detail), None)
            return
        agent, ctx = _build_agent(
            conn, session_id, actor_name, db_lock, turn.message_id,
            can_build_wiki=can_build_wiki, message_owner_id=message_owner_id, user_id=actor.get("id"),
        )
        turn.history = _history(conn, session_id)
        max_turns = _turn_budget(turn.history, max_turns)
        result = await _drive(agent, ctx, turn, max_turns)
        # 收尾自检：这一轮真写过 wiki、且 lint 还有 error，就再给一次"只修 error"的
        # 修复轮（本次修复）。超轮那条路也走到这里——生产上那次"错误没修"正是超轮
        # 收尾，修复轮就是为它补的。
        await _repair_lint(ctx, turn, result)
        if ctx.progress.get("tools"):
            try:
                from ..wiki.maintenance import maintain
                action = "lint" if "lint" in ctx.progress["tools"] else "query"
                maintain(actor=actor_name, action=action,
                         subject="知识查询（已沉淀）" if ctx.progress.get("wrote") else "知识查询（未沉淀）")
            except Exception as exc:
                logger.warning("维护导航与审计失败：%r", exc)
        # 收尾成果（本次新增）：把这一轮对知识库做了什么折叠成一条 outcome 事件。
        # 对话页自 2026-10-30 起不再展示它（不暴露后台改了几页），但管理员日志页的
        # 「会话成果」仍以它为数据源；必须在 _seal 之前写（封口是一轮的最后一次写）。
        outcome = None
        try:
            outcome = _record_outcome(ctx, turn)
        except Exception as exc:   # noqa: BLE001 —— 成果事件是增强项，失败不许拖垮封口
            logger.warning("收尾成果没记上（session=%s message=%s）：%r",
                           turn.session_id, turn.message_id, exc)
        # 零写入声明核对（2026-10-30）：正文声称"已合并/已删除/已改好"但这一轮 0 页改动时，
        # 记一条 system 提示——模型口述与工具事实对不上时，人先看到的是这句，不是那句假话。
        try:
            _claim_guard(turn, outcome)
        except Exception as exc:   # noqa: BLE001 —— 核对是增强项，失败不许拖垮封口
            logger.warning("零写入声明核对没跑成（session=%s message=%s）：%r",
                           turn.session_id, turn.message_id, exc)
        _seal(turn)
    except asyncio.CancelledError:
        # 取消是 BaseException，不被下面那条 except Exception 接住，所以单列一支：
        # 进程内被取消（Task 5 的关停一类）也要封口，别留给下次启动去封。
        _fail(turn, "cancelled", CANCELLED_TEXT, None)
        raise
    except MaxTurnsExceeded as exc:
        logger.warning("智能体这一轮超了 %s 轮（actor=%s）：%r", max_turns, actor_name, exc)
        _fail(turn, "max_turns", MAX_TURNS_TEXT, exc)
    except openai.APIStatusError as exc:
        # 429/402 与其它状态码分开：额度/余额问题是人能自己等一等的事，出 system 事件
        # （spec §8 的口径，前端据此把会话标"待重试"）；别的状态码是上游故障，出 error。
        # 402 是 DeepSeek 的"余额不足"，对人与 429 同一件事（2026-10 切基座时补的口径）。
        if exc.status_code in (429, 402):
            logger.warning("上游额度用尽（%s，actor=%s）：%r", exc.status_code, actor_name, exc)
            # 这一支走 system 而不是 error（spec §8）：额度用完不是故障，是"等一等就好"，
            # 前端据此把会话标"待重试"，不该按出错渲染成红的。
            _fail(turn, "quota", QUOTA_TEXT, exc, event_type=EVENT_SYSTEM)
        elif _is_context_length_error(exc):
            # 裁定 R-S2-31：上下文超限的 400 有自己的文案（CONTEXT_TEXT），不并进
            # UPSTREAM_TEXT——那句叫人"稍后重发"，而原样重发对窗口超限必然再撞。
            logger.warning("上下文超限（actor=%s）：%r", actor_name, exc)
            _fail(turn, "upstream", CONTEXT_TEXT, exc)
        else:
            logger.warning("上游返回 %s（actor=%s）：%r", exc.status_code, actor_name, exc)
            _fail(turn, "upstream", UPSTREAM_TEXT, exc)
    except openai.OpenAIError as exc:
        # APIConnectionError / APITimeoutError 一类（上游断流就在这里）：与状态码故障
        # 同一支文案，因为它们对人是同一件事——"连不上，稍后再试"。
        logger.warning("上游连接出问题（actor=%s）：%r", actor_name, exc)
        _fail(turn, "upstream", UPSTREAM_TEXT, exc)
    except ModelBehaviorError as exc:
        # SDK 的"模型没按规矩出牌"（调用形状不对、同一个 call_id 重复调一类）。
        # 不归并进 internal：INTERNAL_TEXT 叫人"稍后再试"，而这种错原样重试大概率
        # 再撞同一堵墙——模型的行为与时间无关，换个说法才有出路。
        logger.warning("模型没按规矩出牌（actor=%s）：%r", actor_name, exc)
        _fail(turn, "behavior", BEHAVIOR_TEXT, exc)
    except Exception as exc:   # noqa: BLE001 —— 后台任务的最后一道，见上面的"为什么不抛"
        logger.error("智能体这一轮出错（actor=%s）：%r", actor_name, exc, exc_info=exc)
        _fail(turn, "internal", INTERNAL_TEXT, exc)
    finally:
        # 2026-11-13：回合结束把 store 的提交推回共享仓库。失败只记日志，不拖垮
        # 这一轮已经发给用户的回答；下一次回合开始会再 pull/重试。
        try:
            await asyncio.to_thread(git_sync.push_after_turn, conn)
        except Exception as exc:  # noqa: BLE001 —— 同步是旁路，封口已经完成
            logger.warning("回合结束同步失败（actor=%s）：%r", actor_name, exc)


# --- 装配 --------------------------------------------------------------------


def _is_context_length_error(exc: openai.APIStatusError) -> bool:
    """这条 400 是不是"上下文装不下"（裁定 R-S2-31）。

    判据是报文里同时带 context 与 length 字样：DeepSeek/OpenAI 兼容端的这类 400 长的是
    "This model's maximum context length is N tokens…"、code 是 context_length_exceeded，
    两种拼写都盖住。只认 status_code==400 会把"参数不合法"一类也吞进来（那种该走
    UPSTREAM_TEXT/日志排查），所以必须再看报文。str(exc) 里就带着 message 与 body。
    """
    if exc.status_code != 400:
        return False
    # 报文可能落在 str(exc)、exc.message 或 exc.body 的任何一处，全兜进来再判：
    # 只要同时出现 context 与 length 两个词就算（maximum context length / context_length_exceeded）。
    parts = [str(exc), str(getattr(exc, "message", "") or ""), str(getattr(exc, "body", "") or "")]
    text = " ".join(parts).lower()
    return "context" in text and "length" in text


LIBRARY_STATE_LOG_ENTRIES = 25     # 库现状里带几条最近动作
LIBRARY_STATE_MAX_CHARS = 6000    # 库现状的硬上限：它每轮都进 instructions，不许膨胀


def _recent_log_entries(limit: int = LIBRARY_STATE_LOG_ENTRIES) -> list[str]:
    """log.md 最近的几条动作（`## [日期] action | subject` 的标题行）。读不到就算了。"""
    try:
        text = (settings.wiki_dir / "log.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries = [line[3:].strip() for line in text.splitlines() if line.startswith("## [")]
    return entries[-limit:]


def _library_state() -> str:
    """库现状：页数 + 可用标签 + 最近动作（本轮新增，每轮注入 instructions）。

    Hermes 技能要求"每次会话先读 SCHEMA/index/log"；绑定的智能体没必要为此花 3 轮
    工具预算——把**压缩版**（页数、tag 清单、最近 5 条动作）直接拼进系统提示词，
    模型开场就知道库有多大、标签有哪些、刚发生了什么。全量 index 不注入（200 页
    的目录会顶掉正文窗口），要细节还是 read("index.md")。

    读盘失败不许拖垮装配：页数读不到就写"读不到"，标签/日志同理——这一块是定位
    辅助，不是写路径的判据。
    """
    parts = [f"【库现状】技能 {SKILL_VERSION or '未标注'}"]
    try:
        parts.append(f"页面 {len(store.page_files())} 页")
    except OSError:
        parts.append("页面数读不到")
    try:
        tags = wiki_lint.schema_tags() or set()
    except (OSError, UnicodeDecodeError):
        tags = set()
    if tags:
        parts.append("可用标签：" + "、".join(sorted(tags)))
    schema = store.read_special("SCHEMA.md")
    if schema:
        parts.append("领域约定：" + schema["body_md"][:2500])
    recent = _recent_log_entries()
    if recent:
        parts.append("最近动作：" + " / ".join(recent))
    return "；".join(parts)[:LIBRARY_STATE_MAX_CHARS]


def _turn_permissions(actor: dict[str, Any]) -> tuple[bool, int | None]:
    """从路由交来的 actor dict 取这一轮的两个快照：(can_build_wiki, message_owner_id)。

    缺省值 True / None 是"与迁移前一致"的口径：旧调用方（脚本、测试里直接调 run_turn）
    的 actor dict 只有 id/name，不能因此把它们判成"没有权限"或"只看自己"。
    判据本身不算在这里（路由按 access.can_build_wiki / access.message_owner_filter 算好），
    runner 只做透传——同一套可见性判据全项目只留 access 一处。
    """
    can_build_wiki = bool(actor.get("can_build_wiki", True))
    raw_owner = actor.get("message_owner_id")
    message_owner_id = None if raw_owner is None else int(raw_owner)
    return can_build_wiki, message_owner_id


PERMISSION_BUILD_TEXT = (
    "【本轮权限】可以建设 wiki：摄入材料（网页/整本）、建页、改页、删页都允许。"
)
PERMISSION_QUERY_TEXT = (
    "【本轮权限】当前账号有使用权限：查询、搜索、阅读，并可新建/编辑 query/comparison 页。"
    "不能摄入材料、不能建或改 entity/concept、不能删页。"
    "这是查询轮：先直接基于已有页面回答。如果你判断这次回答成体系、可复用，就自行新建"
    " query/comparison 页（标题自己拟），不要问用户要不要存、也不要说"
    "「需要建设权限 / 当前是查询权限不能写」；不值得沉淀就只回答，连保存建议都别提。"
    "用户要求联网检索或摄入材料时，说明当前权限不支持，再用知识库已有内容回答。"
)
ANSWER_STYLE_TEXT = (
    "【回答格式】给用户看的聊天正文不是 wiki 页面：不要写 [^c1] 这类脚注角标、"
    "frontmatter，也不要主动汇报“本轮未改动知识库”“还没写成汇总页”这类内部状态；"
    "没有改动就不必声明，用户问起再如实说。需要给依据时说页面名或材料名。"
    "回答里如果要写 [[slug]]，必须从 list_wiki/read/search 的结果里逐字复制完整 slug，"
    "不能截短、改写或凭印象猜；找不到对应页就写页面标题，不要写不存在的链接。"
    "查询结果是否沉淀由你自己判断：值得就自行新建 query/comparison 页，"
    "不要把“要不要存成页/标题你定”交回用户；不值得就只回答。"
)


def _permission_state(can_build_wiki: bool) -> str:
    """这一轮的能力提示（2026-10-30，每轮追加在 instructions 末尾）。

    为什么要进系统提示词、而不是只靠工具拒绝：模型看不见"我有没有权限"时，会把
    拒绝当成临时故障反复重试（白烧轮次），也可能先写一大段"我这就改"再被工具打脸。
    开场就说清楚它能做什么，比事后逐条纠正省事得多。工具的拒绝串仍保留——提示词是
    引导，工具闸才是保证。
    """
    return PERMISSION_BUILD_TEXT if can_build_wiki else PERMISSION_QUERY_TEXT


def _base_instructions() -> str:
    """技能正文 + app.toml 里可选的 agent persona / rules。"""
    config = get_config()
    parts: list[str] = []
    if config.agent_persona.strip():
        parts.append(config.agent_persona.strip())
    parts.append(BASE_STYLE)
    if config.agent_rules.strip():
        parts.append(config.agent_rules.strip())
    return "\n\n".join(parts)


def _build_agent(conn: sqlite3.Connection, session_id: int, actor_name: str,
                 db_lock: threading.Lock, message_id: int,
                 *, can_build_wiki: bool = True,
                 message_owner_id: int | None = None, user_id: int | None = None):
    """造 Agent 与它的 context。装配失败（模型没配好一类）也走 run_turn 的兜底。

    db_lock 必须是 run_turn 里造的那一把（与 _Turn 共用）：AgentContext 的
    default_factory 会造一把新的，各锁各的等于没锁（裁定 R-S2-17）。
    message_id 是这一轮那条 agent 行的 id（裁定 R-S2-20）：hooks 从 ctx 拿它写进
    agent_events.message_id，SSE 尾读据此把事件精确归到这一轮。

    can_build_wiki / message_owner_id（2026-10-30）是路由算好的能力与可见性快照，
    进 AgentContext 供工具与 search 用；默认值让"脚本/旧测试直接装配"保持旧行为。
    """
    ctx = AgentContext(conn=conn, session_id=session_id, actor_name=actor_name,
                       message_id=message_id, db_lock=db_lock,
                       can_build_wiki=can_build_wiki,
                       message_owner_id=message_owner_id,
                       user_id=user_id)
    agent = Agent(
        name=AGENT_NAME,
        model=make_model(),
        # 思考模式开关（默认关）：DeepSeek 默认开思考，而带 tools 的请求要求回传历史
        # reasoning_content——harness 不存它，开着会被 400。见 llm.model_settings。
        model_settings=model_settings(),
        # 技能正文 + 每轮现算的库现状（页数/标签/最近动作）+ 本轮权限（能做什么）。
        # 技能是静态章程；库现状与权限是这一轮的输入——分开拼，改技能不必动代码。
        instructions=_base_instructions() + "\n\n" + _library_state() + "\n\n"
        + ANSWER_STYLE_TEXT + "\n\n" + _permission_state(can_build_wiki),
        tools=WIKI_TOOLS,          # 裁定 R-S2-6：模块级 list 常量，不是 WIKI_TOOLS(conn)
    )
    return agent, ctx


def _run_config() -> RunConfig:
    """这一轮跑的 RunConfig。两个字段都是承重的：

    - `tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1)`（裁定 N5/R-S2-13）：
      默认是"一轮里的工具全部并发跑"，串行执行让事件按 call→result→call→result 的顺序
      落 agent_events，7 人规模无感。字段名以装好的 SDK 为准：0.24.0 的 RunConfig 上是
      `tool_execution`，N5 里写的 `tool_execution_config` 那个名字不存在（dataclass 不认，
      直接 TypeError）。实证：`[f.name for f in dataclasses.fields(RunConfig)]` 里有
      tool_execution。
      **但它消不掉共享连接的并发写**（裁定 R-S2-17，原先的注释把这一点说反了）：
      它只把工具与工具之间串起来；relay（_append_text/_seal/_event）在事件循环线程，
      工具体在 asyncio.to_thread 的 worker 线程，两边照样可能同时在那条 sqlite 连接上
      开隐式事务（"cannot start a transaction within a transaction"）。那条缝由
      AgentContext.db_lock 管：所有写路径先持锁再动连接。
    - `tracing_disabled=True`：SDK 默认把每一轮的 trace 发去 OpenAI 的后端，我们用的是
      DeepSeek 的兼容端点，那些请求既没用也不该发（计划的验收项之一就是"抓包无 api.openai.com"）。
      main.py 的 lifespan 还会再调一次 set_tracing_disabled(True)（Task 5），这里是每一轮
      自带一道，测试里不起 lifespan 也照样干净。
    """
    return RunConfig(
        tracing_disabled=True,
        workflow_name=WORKFLOW_NAME,
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
    )


_ATTACHMENT_STATE = {"done": "已解析", "failed": "解析失败", "none": "待解析"}


def _attachment_line(upload) -> str:
    """一条附件摘要：`upload/{id}《文件名》（状态）`。"""
    state = _ATTACHMENT_STATE.get(str(upload["parse_status"]), "待解析")
    name = " ".join(str(upload["orig_filename"] or "").split()) or "未命名文件"
    return f"upload/{upload['id']}《{name}》（{state}）"


def _turn_budget(history: list[dict[str, str]], requested: int) -> int:
    """这一轮的模型轮次预算。带 `[附件]` 的摄入轮给 INGEST_MAX_TURNS，其余照请求值。

    判据只看**最新一条** human 消息里有没有 `_history` 追加的 `[附件] upload/…` 行：
    那是 S11 两段式定下的"这次要摄入"的唯一记号，不靠会话标题/文件名猜。显式传入
    更大的 max_turns（测试/脚本）时取大者，不往下压。
    """
    last_user = next(
        (item.get("content") or "" for item in reversed(history) if item.get("role") == "user"),
        "",
    )
    if "[附件]" in last_user:
        return max(requested, INGEST_MAX_TURNS)
    if any(hint in last_user for hint in _TASK_HINTS):
        return max(requested, TASK_MAX_TURNS)
    return requested


def _history(conn: sqlite3.Connection, session_id: int) -> list[dict[str, str]]:
    """喂给模型的上下文：这场会话最近 HISTORY_MAX 条人话与智能体回复（N10），
    并按新→旧累加字符、到 HISTORY_CHAR_BUDGET 就停（裁定 R-S2-31）。

    - 人话带 `[消息{id}]` 前缀：那个 id 就是引注的 source_id，模型看得到才填得对
      （工具 search/read 给的命中里也是这个编号，两边对得上）。
    - 智能体的回复原样交回去（role=assistant），不加前缀：那不是语料，是它自己上一轮的话。
    - 上传材料**不**内联：整份解析文本能把窗口顶爆，模型要用 read/search 自己去取。
      但会追加一行 `[附件] upload/{id}《文件名》（已解析/待解析/解析失败）`：用户消息里
      出现这行就代表"这次要摄入的附件"，模型据此去 read / ingest_document（S11 两段式）。
      附件按 message_id IN (...) 一次批量查，不 N+1。
    - 空正文的行跳过：这一轮自己那条 agent 行就是空的（正文还没流出来），
      交上去等于让模型看一条空回复。
    - 字符预算截的是**最老**的那几条：窗口装不下时，保住最新的上下文才有得答；
      旧材料模型仍能用 search/read 两个工具去取（这正是"只检索不带头脑"那条路）。

    **M14：这是共享连接唯一一处不在 db_lock 下的访问，且必须保持安全的前提**——
    它在 run_turn 里跑在 _drive 之前：那时这一轮的工具体与 relay 都还没起来，
    这条连接上只有本函数一个使用者，锁没有可争的对象。哪天有人把取历史挪到
    _drive 之后（或跑中途再取一次），就必须改成持 turn.db_lock 再查，否则与
    工具体/relay 撞出 "cannot start a transaction within a transaction"。
    """
    rows = conn.execute(
        "SELECT id, role, content FROM messages"
        " WHERE session_id = ? AND role IN ('human', 'agent') AND content <> ''"
        " ORDER BY seq DESC LIMIT ?",
        (session_id, HISTORY_MAX),
    ).fetchall()
    # 附件行：这一批人话的消息 id 一次 IN 查完（不 N+1）。
    human_ids = [int(row["id"]) for row in rows if row["role"] == "human"]
    attachments: dict[int, list] = {}
    if human_ids:
        marks = ",".join("?" * len(human_ids))
        for up in conn.execute(
            "SELECT id, message_id, orig_filename, parse_status FROM upload_files"
            f" WHERE message_id IN ({marks}) ORDER BY id",
            human_ids,
        ):
            attachments.setdefault(int(up["message_id"]), []).append(up)

    out: list[dict[str, str]] = []
    used = 0
    for row in rows:                # 库里取的是新到旧：预算从最新的那条开始累
        if used >= HISTORY_CHAR_BUDGET:
            break                   # 预算用尽，更老的一律不带（最新一条永远保留）
        used += len(row["content"] or "")
        if row["role"] == "human":
            content = f"[消息{row['id']}] {row['content']}"
            files = attachments.get(int(row["id"]), [])
            if files:
                content += "\n[附件] " + "；".join(_attachment_line(up) for up in files)
            out.append({"role": "user", "content": content})
        else:
            out.append({"role": "assistant", "content": row["content"]})
    out.reverse()                   # 交上去要旧到新
    if not out:
        # 正常到不了这里：路由先落人话再起这一轮。真到了（有人直接调 run_turn），
        # 交一个空列表 SDK 会当"没有输入"，不如给一句人话让它有的答。
        out.append({"role": "user", "content": "（这场会话还没有内容）"})
    return out


# --- 跑：驱动 SDK 的流，把正文增量落库 ----------------------------------------


async def _drive(agent, ctx: AgentContext, turn: _Turn, max_turns: int):
    """跑这一轮。超轮时先不带工具再要一句人话（计划 §19），要不到才抛出去。

    返回这一跑的 RunResult（收尾结果，拿得到就返回；理论上的空盒子返 None）：
    收尾修复轮要靠它的 `to_input_list()` 接着上下文说话。
    """
    hooks = WikiHooks()               # 无状态（N2）：连接/会话/actor 都从 ctx 走
    config = _run_config()
    # MaxTurnsExceeded 是在吃流吃到一半时抛出来的，那时 result 已经造好了；用一个盒子
    # 把它接住，收尾那一次要靠它拿 to_input_list()（烧掉的那几轮的完整输入）。
    box: list = []
    try:
        await _stream_once(agent, ctx, turn, config, max_turns, hooks, turn.history, box)
    except MaxTurnsExceeded:
        # 收尾那一次不给工具：没有工具就不会再超轮，也不会再往 agent_events 里记事件。
        wrap_up = Agent(name=AGENT_NAME, model=make_model(),
                        instructions=_base_instructions() + "\n\n" + ANSWER_STYLE_TEXT,
                        model_settings=model_settings())
        # 喂给它上一跑**真实的输入**（to_input_list：原上下文 + 每一轮的工具调用与
        # 结果），WRAP_UP_TEXT 放在最后。只给一句 WRAP_UP_TEXT 的话模型不知道自己刚才
        # 干到哪一步，那句收尾只能是编的——"办到了哪一步、还差什么"要说得出来，它得
        # 先看见自己烧掉的那几轮。盒子里万一没有（跑都没跑起来就超轮，理论形状），
        # 退回 history。
        prior = box[0].to_input_list() if box else list(turn.history)
        try:
            await _stream_once(
                wrap_up, ctx, turn, config, 1, hooks,
                prior + [{"role": "user", "content": WRAP_UP_TEXT}],
            )
        except openai.OpenAIError:
            # 上游故障归上游，别吞成"超轮"：收尾那一次照样要打上游，它撞 429 时
            # 人该看到的是"额度暂时用完"（前端据此标"待重试"），不是"工具调得太多次"
            # ——后者会叫人去改问法，而改问法对额度一点用没有。APIStatusError 是
            # OpenAIError 的子类，所以这一支同时接住状态码故障与连不上/超时。
            raise
        except Exception as exc:      # noqa: BLE001 —— 收尾也失败就退回超轮那条兜底
            logger.warning("超轮之后想再要一句人话，也没要到：%r", exc)
            raise MaxTurnsExceeded(f"max_turns={max_turns} 且收尾失败") from exc
    return box[0] if box else None


REPAIR_TEXT = (
    "收尾自检没通过：lint 还有 {n} 条 error。现在**只修这些**，不要再建新页。\n"
    "{detail}\n"
    "改法：先 read 拿 base_commit，用 edit_page 改（断链就把不存在的目标改成真实 slug "
    "或 [[页面标题]]）。改完再调一次 lint()；error 清零后停手，用一句话说明修了什么。"
)


async def _repair_lint(ctx: AgentContext, turn: _Turn, result) -> None:
    """收尾自检 + 修复轮：把"lint 有 error 就同轮修到 0"从提示词愿望变成 harness 保证。

    只在**这一轮真写过 wiki**（hooks 置了 progress["wrote"]）时跑；查一句话的回合不付
    这个代价。error 只在内存里判断，不往正文里塞解释——修干净了就当没发生过；修不干净
    才记一条 system 事件（待修状态，数据层），人能看到也能再让我修。

    修复轮失败（超轮/上游故障/工具错）不许反过来把主轮判失败：主轮的正文与页面已经
    落地，这里只在日志与待修状态里留痕（自带 try/except）。
    """
    if result is None or not ctx.progress.get("wrote"):
        return
    touched = ctx.progress.get("paths")
    def scoped_errors():
        return lint_errors(turn.conn, paths=touched) if touched else lint_errors(turn.conn)
    try:
        errors = scoped_errors()
    except Exception as exc:      # noqa: BLE001 —— 自检是增强项，失败不许拖垮主轮
        logger.warning("收尾 lint 没跑成（session=%s message=%s）：%r",
                       turn.session_id, turn.message_id, exc)
        return
    if not errors:
        ctx.progress["lint_errors"] = 0
        return
    detail = "\n".join(
        f"- {item.get('path') or '（全库）'}：{item.get('message') or ''}" for item in errors
    )
    logger.info("收尾 lint 还有 %s 条 error，起修复轮（session=%s message=%s）",
                len(errors), turn.session_id, turn.message_id)
    if touched:
        ctx.progress["repair_paths"] = set(touched)
    repair_agent = Agent(
        name=AGENT_NAME, model=make_model(), instructions=_base_instructions(),
        model_settings=model_settings(),
        tools=[tool for tool in WIKI_TOOLS if tool.name in {"read", "list_wiki", "search", "edit_page", "lint"}],
    )
    try:
        await _stream_once(
            repair_agent, ctx, turn, _run_config(), REPAIR_TURNS, WikiHooks(),
            result.to_input_list()
            + [{"role": "user", "content": REPAIR_TEXT.format(n=len(errors), detail=detail)}],
        )
    except Exception as exc:      # noqa: BLE001 —— 修复轮失败只留日志，不翻主轮
        logger.warning("收尾修复轮没跑完（session=%s message=%s）：%r",
                       turn.session_id, turn.message_id, exc)
    try:
        remaining = scoped_errors()
    except Exception as exc:      # noqa: BLE001
        logger.warning("修复后复检没跑成（session=%s）：%r", turn.session_id, exc)
        return
    ctx.progress["lint_errors"] = len(remaining)
    if not remaining:
        return
    sample = "；".join(item.get("path") or "（全库）" for item in remaining[:5])
    logger.warning("收尾修复后仍有 %s 条 error（session=%s）：%s",
                   len(remaining), turn.session_id, sample)
    try:
        _event(turn, EVENT_SYSTEM, {
            "text": f"还有 {len(remaining)} 处 lint error 没修（{sample}），可以让我再修一次。"
        })
    except Exception as exc:      # noqa: BLE001 —— 待修状态是旁路，写不进也不能拖垮封口
        logger.warning("待修状态没记上（session=%s）：%r", turn.session_id, exc)


_OUTCOME_PATH_RE = re.compile(r"已(?:生效|删除)：path=([^\s（]+)")
_OUTCOME_CITES_RE = re.compile(r"引注 (\d+) 条")


def _record_outcome(ctx: AgentContext, turn: _Turn) -> dict | None:
    """把这一轮对 wiki 的改动折成一条 outcome 事件（管理员日志页「会话成果」的数据源）。

    数据不另开表：这一轮写路径的 tool-result 已经落在 agent_events 里，摘要串里有
    `已生效：path=…` / `已删除：path=…` 与 `引注 N 条`，这里只做一次解析与去重
    （同一页被改多次只记最后一次的引注数）。`lint_errors` 来自 _repair_lint 的收尾复检。

    形状（system 事件的 payload，用 kind 区分，不动 CHECK 白名单）：
        {"kind":"outcome","pages":[{"path":…,"action":"create|edit|delete"}],
         "citations":N,"lint_errors":N?}
    """
    if not ctx.progress.get("wrote"):
        return None
    rows = turn.conn.execute(
        "SELECT payload_json FROM agent_events"
        " WHERE message_id = ? AND type = 'tool-result' ORDER BY seq",
        (turn.message_id,),
    ).fetchall()
    pages: dict[str, str] = {}
    cites: dict[str, int] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            continue
        if not isinstance(payload, dict) or not payload.get("ok"):
            continue
        summary = str(payload.get("summary") or "")
        match = _OUTCOME_PATH_RE.search(summary)
        if match is None:
            continue
        path = match.group(1)
        pages[path] = "delete" if summary.startswith("已删除") else (
            "edit" if payload.get("name") == "edit_page" else "create"
        )
        cite_match = _OUTCOME_CITES_RE.search(summary)
        if cite_match is not None:
            cites[path] = int(cite_match.group(1))
    outcome = {
        "kind": "outcome",
        "pages": [{"path": path, "action": action} for path, action in pages.items()],
        "citations": sum(cites.values()),
    }
    lint_errors = ctx.progress.get("lint_errors")
    if lint_errors is not None:
        outcome["lint_errors"] = lint_errors
    if not outcome["pages"] and outcome["citations"] == 0 and lint_errors is None:
        return None
    _event(turn, EVENT_SYSTEM, outcome)
    return outcome


# 正文里"我做完了"那类说法（只认知识库写操作，不认"体检报告做完了"）。与 _claim_guard 配对：
# 只有这里没命中、或命中但哪一步都没写，才值得给人提一句。
_CLAIM_RE = re.compile(
    r"已(?:经)?(?:新建|建好|建了|新增|改好|改了|修改好|删除|删掉|合并|修好|修掉|修复)"
)
_CLAIM_GUARD_TEXT = (
    "注意：这一轮没有实际写入知识库（0 页改动）；正文里说的「已完成/已合并/已删除」"
    "只是模型口述，请打开知识库看页面实际有没有变。"
)


def _claim_guard(turn: _Turn, outcome: dict | None) -> None:
    """零写入声明核对：正文声称改了 wiki、但这一轮 outcome 里一页都没有时记一条提示。

    为什么需要它：2026-10-30 生产会话 27 里，模型在**没有任何 write/edit/delete 调用**的
    一轮里写了"monica 三页已经合并完成"（另两页其实还在盘上、git 也没提交）。这是模型
    口述与工具事实不符，靠人读正文很难发现；把"0 页"这件事由 harness 说出来，比让模型
    自我声明可信。
    """
    row = turn.conn.execute(
        "SELECT content FROM messages WHERE id = ?", (turn.message_id,)
    ).fetchone()
    text = str(row["content"] or "") if row is not None else ""
    if text == "":
        return
    pages = len((outcome or {}).get("pages") or [])
    if pages > 0:
        return
    claim = _CLAIM_RE.search(text)
    if claim is None:
        return
    logger.warning(
        "零写入声明：正文声称「%s」但本轮 0 页改动（session=%s message=%s）",
        claim.group(0), turn.session_id, turn.message_id,
    )
    _event(turn, EVENT_SYSTEM, {"text": _CLAIM_GUARD_TEXT})


async def _stream_once(
    agent,
    ctx: AgentContext,
    turn: _Turn,
    config: RunConfig,
    max_turns: int,
    hooks: WikiHooks,
    inputs: list,
    out: list | None = None,
) -> None:
    """起一次 run_streamed 并把流吃完，输入就是交进来的 inputs。异常原样往外抛。

    out 给了的话，result 一造出来就放进去：调用方在 except MaxTurnsExceeded 分支里
    靠它拿 to_input_list()（见 _drive）。
    """
    result = Runner.run_streamed(
        agent,
        inputs,
        context=ctx,
        max_turns=max_turns,
        hooks=hooks,
        run_config=config,
    )
    if out is not None:
        out.append(result)
    async for event in result.stream_events():
        _relay(turn, event)


def _relay(turn: _Turn, event: Any) -> None:
    """一个流事件 → 库里的动作。只认正文增量，其余一概放过（N3）。

    放过的那些各有归属：

    - `RunItemStreamEvent`（tool_called / tool_output / message_output_created）：
      工具事件由 hooks 写 agent_events；message_output_created 那条的正文就是
      text-delta 累加出来的同一串，再写一遍就重了。
    - `ResponseFunctionCallArgumentsDeltaEvent`：工具参数的增量，不是给人看的正文
      （它有 `.delta`，按属性名认就会把 `{"query": …}` 追加进回复里）。
      参数已经在 tool-call 事件的 args 里（hooks 记的），这里不重复。
    - `ResponseCreatedEvent` / `…ContentPartAdded` / `…ItemDone` / `ResponseCompletedEvent`：
      纯生命周期，尾读不看它们。
    """
    if not isinstance(event, RawResponsesStreamEvent):
        return
    if not isinstance(event.data, ResponseTextDeltaEvent):
        return
    text = event.data.delta or ""
    if not text:
        return
    if "\ufffd" in text:
        # 2026-10-23 生产 session 16 出现过一次：上游正文流里带了 U+FFFD（三个替换字符
        # 替掉了一个汉字）。DB 里存到的就是坏字符，前端只是照实渲染；留一条 warning
        # 便于以后判断是模型/网关偶发还是解析层问题。不改写正文（remove 会把字吞掉）。
        logger.warning(
            "模型正文增量含 U+FFFD（session=%s message=%s）：%r",
            turn.session_id, turn.message_id, text[:120],
        )
    _append_text(turn, text)


def _append_text(turn: _Turn, text: str) -> None:
    """把一段正文接到 agent 行的 content 尾巴上，并**立刻提交**（N6）。

    `content = content || ?` 是在 SQL 里接的，不在 Python 里读回来再拼：这一轮跑期间
    没有第二个写者会动这一行（同会话不并发跑，Task 5 的 409 闸），但读-改-写要多一次
    往返，而且中途被打断时会把别人写的字盖掉。

    提交粒度选"每段一次"：一段 delta 就几个字，一次提交在 WAL 下是几十微秒的事，
    而尾读那头看到的就是"字在往外蹦"。攒批（比如每 200 字或每 100ms）能省提交，
    代价是前端一顿一顿的，还要多一套定时 flush 的状态机——7 人规模不值当。

    先持 db_lock 再动连接（裁定 R-S2-17）：这一段跑在事件循环线程，而工具体在
    to_thread 的 worker 线程上写**同一条**连接；max_function_tool_concurrency=1 只管
    工具与工具，管不了这条缝。
    """
    with turn.db_lock:
        turn.conn.execute(
            "UPDATE messages SET content = content || ? WHERE id = ?", (text, turn.message_id)
        )
        turn.conn.commit()


# --- 落库：开口 / 封口 / 事件 -------------------------------------------------


def _open_agent_message(conn: sqlite3.Connection, session_id: int,
                        db_lock: threading.Lock) -> int:
    """开一条空的 agent 行，返回它的 id。

    空正文 + usage_json NULL + truncated=0 就是"正在跑"的记号：尾读看见这个形状就
    继续等，启动封口看见这个形状就知道上次没写完。seq 用单条 INSERT…SELECT 原子分配
    （与 chat.append_human、hooks.append_event 同一个手法，裁定 R68）。

    提交之后才对尾读可见——不提交的话前端连"智能体开始写了"这个气泡都出不来。
    这里**不**顺手推 chat_sessions.last_message_at：那一下留给封口（_seal），
    免得一轮跑里同一列被改两次。锁照持（裁定 R-S2-17）：这一下写发生在装配之前，
    那时还没有别的写者，但"所有写路径都持锁"这条规矩不留例外。
    """
    with db_lock:
        cur = conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at, truncated)"
            " SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', '', ?, 0"
            " FROM messages WHERE session_id = ?",
            (session_id, utcnow(), session_id),
        )
        conn.commit()
    return int(cur.lastrowid)


def _seal(turn: _Turn, *, truncated: int = 0, usage_json: str | None = DONE_USAGE) -> None:
    """封口：把这条 agent 行标成"跑完了"，尾读据此发 done。

    usage_json 的两种取值都是有意的（见 DONE_USAGE 的注释）：
    正常收尾写 `'{}'`（跑完了、没有用量可记），失败路径写 NULL（真没有用量）。
    失败路径靠 truncated=1 收尾，所以尾读照样发得出 done。

    **封口必须是一轮的最后一次写**（裁定 R-S2-18）：done 判据一满足，Task 5 的尾读
    随时可能退出，之后再写的行它看不见了。所以 _fail 先记事件、后调这里；成功路径
    天然满足（封口之后没有写了）。持锁同 _append_text（裁定 R-S2-17）。
    """
    with turn.db_lock:
        turn.conn.execute(
            "UPDATE messages SET usage_json = ?, truncated = ? WHERE id = ?",
            (usage_json, truncated, turn.message_id),
        )
        if not truncated:
            # 只有真跑完了才把这条会话的"最近动静"推到现在：列表排序看这一列，
            # 而这条 agent 回复不经 append_human（chat.touch 的 docstring 就是给这里写的）。
            chat_service.touch(turn.conn, turn.session_id)
        turn.conn.commit()


def _event(turn: _Turn, event_type: str, payload: dict) -> None:
    """往 agent_events 记一条 system/error（tool-* 归 hooks，这里不写）。

    message_id 传这一轮那条 agent 行的 id（裁定 R-S2-20，与 hooks 同一个口径）：
    429/断流/内部异常那些解释行也是**这一轮的**，尾读按 message_id 过滤时它们
    得跟着这一轮走，而不是退回 created_at 的模糊边界。

    锁交给 append_event 去拿（db_lock= 传的就是 turn 这把，裁定 R-S2-17）；这一层
    不再套一遍——threading.Lock 不可重入，同一条调用链拿两次当场死锁。
    """
    append_event(turn.conn, turn.session_id, event_type, payload, db_lock=turn.db_lock,
                 message_id=turn.message_id)


def _fail(
    turn: _Turn | None,
    code: str,
    text: str,
    exc: BaseException | None,
    *,
    event_type: str = "error",
) -> None:
    """失败出口：先记事件，**再**封口（裁定 R-S2-18）。绝不抛出去（后台任务抛了没人接）。

    顺序是承重的：尾读的 done 判据是 `usage_json IS NOT NULL OR truncated=1`，先封口
    后写事件就有一段"done 已满足、解释行还没落库"的窗口——Task 5 的循环可能恰好在那
    一瞬读到 done 退出，「额度暂时用完…」永远到不了人眼前。封口必须是一轮的最后一次
    写，成功路径本来如此，失败路径也得一致。

    turn 为 None 只有一种可能：连那条 agent 行都没开出来（连接本身坏了）。那时候
    没有行可封口、也没有连接可记事件，只剩日志；那条"跑"在库里压根没留痕，
    前端顶多看到一个没有回音的气泡，重发一次就好。

    **_event 有自己的 try（I6）**：解释行写不进去（磁盘满、库锁死、agent_events
    被人删了）时，旧写法会连 _seal 一起跳过——那一行就停在"还在跑"的形状，尾读
    永远等不到 done，得等下次启动的 seal_orphan_agent_messages 才收。事件是旁路，
    封口是出口：旁路断了也要把出口走完，所以事件失败只记日志，_seal 照跑。
    """
    if turn is None:
        return
    # system 那支**故意丢掉 code**（spec §8：system 的 payload 形状就是
    # {"text": …}，前端只渲染 text）；quota 之类的 code 只进服务端日志与测试。
    payload = {"text": text} if event_type == EVENT_SYSTEM else {"code": code, "text": text}
    try:
        _event(turn, event_type, payload)
    except Exception as event_exc:   # noqa: BLE001 —— 旁路记账断了不许拖走封口
        logger.error(
            "解释事件没写成（code=%s，原始异常=%r）：%r；封口照走",
            code, exc, event_exc, exc_info=event_exc,
        )
    try:
        _seal(turn, truncated=1, usage_json=None)
    except Exception as seal_exc:   # noqa: BLE001 —— 连兜底都写不动时只留日志
        logger.error(
            "兜底没写成（code=%s，原始异常=%r）：%r；这一行留给启动封口",
            code, exc, seal_exc, exc_info=seal_exc,
        )
