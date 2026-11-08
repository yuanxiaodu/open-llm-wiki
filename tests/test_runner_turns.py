"""轮次预算与库现状注入（本次修复的两块 runner 行为）。

- 预算：普通回合 7 轮；带 [附件] 的摄入回合单独放宽。
- 库现状：每轮把页数/可用标签/最近动作拼进 instructions，省掉 Hermes 技能要求的
  "开场先读 SCHEMA/index/log"三轮工具预算。
"""

from __future__ import annotations

import threading

from app.agent import runner
from app.wiki import store


def test_ingest_turn_gets_its_own_budget():
    history = [
        {"role": "user", "content": "上一条普通消息"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "看这个\n[附件] upload/7《产品经理求职指南.pdf》（已解析）"},
    ]
    assert runner._turn_budget(history, runner.MAX_TURNS) == runner.INGEST_MAX_TURNS
    assert runner.INGEST_MAX_TURNS > runner.MAX_TURNS


def test_task_turns_get_a_bigger_budget():
    """会话 28 实测：7 轮全花在定位上，模型只能 wrap-up 说"再调一次 write_page"。"""
    for text in (
        "继续做孤立页治理：先 list_wiki 拿全量清单",
        "按来源分组，每轮写 1–3 个枢纽页，写完 lint",
        "请给知识库做一次体检",
        "执行。",
    ):
        assert runner._turn_budget([{"role": "user", "content": text}],
                                   runner.MAX_TURNS) == runner.TASK_MAX_TURNS, text


def test_plain_turn_keeps_the_default_budget():
    assert runner._turn_budget([{"role": "user", "content": "最近摄入了什么？"}],
                               runner.MAX_TURNS) == runner.MAX_TURNS
    assert runner._turn_budget([], runner.MAX_TURNS) == runner.MAX_TURNS


def test_explicit_larger_budget_is_not_downgraded():
    """显式传更大的 max_turns（脚本/测试）时不能被摄入档压回去。"""
    history = [{"role": "user", "content": "[附件] upload/1《x》（已解析）"}]
    assert runner._turn_budget(history, 80) == 80


def test_turn_budgets_are_raised_for_long_query_rounds():
    """2026-10-30 委托人：工具预算总是不够，取消或大幅提升。保留硬上限做安全网，
    但普通查询/摄入/动手任务都大幅上调。"""
    assert runner.MAX_TURNS >= 30
    assert runner.INGEST_MAX_TURNS >= 40
    assert runner.TASK_MAX_TURNS >= 50
    assert runner.REPAIR_TURNS >= 6


def test_library_state_reports_pages_tags_and_recent_actions(conn, vault):
    store.write_page_sync(
        conn, path=None, title="现状演示页", page_type="concept",
        body_md="正文", citations=[], actor="张三",
    )
    state = runner._library_state()
    assert state.startswith("【库现状】技能 ")
    assert "页面 1 页" in state
    assert "可用标签：" in state
    assert "最近动作：" in state and "create" in state


def test_library_state_has_a_hard_size_cap(vault, monkeypatch):
    """它每轮都进 instructions，必须有上限（库再大也不能把系统提示词撑爆）。"""
    monkeypatch.setattr(runner, "LIBRARY_STATE_MAX_CHARS", 12)
    assert len(runner._library_state()) <= 12


def test_agent_instructions_carry_skill_then_library_state(conn, member, vault):
    agent, _ctx = runner._build_agent(
        conn, member.session_id, "张三", threading.Lock(), 0,
    )
    base = runner._base_instructions()
    assert agent.instructions.startswith(base)
    assert "【库现状】" in agent.instructions[agent.instructions.index(base) + len(base):]
    # 技能版本进状态串，方便排查"这个行为是哪版技能跑出来的"。
    assert runner.SKILL_VERSION
    assert runner.SKILL_VERSION in agent.instructions
    # 2026-10-30：instructions 末尾追加本轮权限提示
    assert "【本轮权限】" in agent.instructions
    assert agent.instructions.rstrip().endswith(runner._permission_state(True))
    # 回答格式约束：聊天正文不写 cite 角标、不主动汇报“未改动知识库”
    assert runner.ANSWER_STYLE_TEXT in agent.instructions
    assert "【回答格式】" in agent.instructions


# --- 2026-10-30：本轮权限（actor dict → AgentContext → instructions） ---------------


def test_turn_permissions_default_to_legacy_values():
    """旧调用方的 actor dict 只有 id/name：缺省 True/None，不能被判成"没权限/只看自己"。"""
    assert runner._turn_permissions({"id": 1, "name": "张三"}) == (True, None)


def test_turn_permissions_read_the_actor_snapshot():
    """路由按 access 算好两个快照，runner 只透传：False / user id 原样带出来。"""
    assert runner._turn_permissions({
        "id": 1, "name": "张三", "can_build_wiki": False, "message_owner_id": 7,
    }) == (False, 7)
    assert runner._turn_permissions({
        "id": 1, "name": "张三", "can_build_wiki": True, "message_owner_id": None,
    }) == (True, None)


def test_permission_state_says_query_only_when_build_is_off():
    """只读权限的提示：能沉淀 query/comparison/summary 页，不需要另要建设权限。"""
    text = runner._permission_state(False)
    assert "【本轮权限】" in text
    assert "使用权限" in text and "查询" in text
    assert "page_type=query/comparison/summary" in text
    assert "自行新建" in text and "不要问用户" in text
    # 查询轮要直接回答；联网/摄入要说明权限不支持
    assert "直接基于已有页面回答" in text
    assert "权限不支持" in text
    # 有权限时不出现"只读"的限制话术
    allowed = runner._permission_state(True)
    assert "【本轮权限】" in allowed and "摄入" in allowed


def test_agent_context_and_instructions_carry_the_permission_snapshot(conn, member, vault):
    """关闭权限的账号：AgentContext 带上快照，instructions 末尾是 query-only 提示。"""
    agent, ctx = runner._build_agent(
        conn, member.session_id, "张三", threading.Lock(), 0,
        can_build_wiki=False, message_owner_id=member.user_id,
    )
    assert ctx.can_build_wiki is False
    assert ctx.message_owner_id == member.user_id
    assert agent.instructions.rstrip().endswith(runner._permission_state(False))
    assert "使用权限" in agent.instructions.split("【本轮权限】", 1)[1]


def test_agent_context_defaults_keep_old_behaviour(conn, member, vault):
    """不传新参数时默认 True/None（裸造上下文的旧测试与脚本不用陪绑）。"""
    _, ctx = runner._build_agent(
        conn, member.session_id, "张三", threading.Lock(), 0,
    )
    assert ctx.can_build_wiki is True
    assert ctx.message_owner_id is None
