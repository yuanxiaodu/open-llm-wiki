"""技能（系统提示词）的契约测试：本项目是"一个智能体 + 一份技能"，不是 skill 库。

这些断言钉的是"技能与 harness 不许各说各话"：

1. 技能文件里的 `tools:` 清单必须与 `tools.WIKI_TOOLS` **完全一致**——任何一边加了
   工具忘了另一边，模型要么看不见工具、要么看见不存在的工具。
2. 硬规则必须还在（逐字引注、真实 slug/标题、base_commit、lint 修到 0）。
3. 不给隐私条款留口子（裁定 5：全站语料可见，判据只有"逐字对得上"）。
4. BASE_STYLE 就是技能正文，frontmatter 不喂给模型（版本号不该出现在指令里）。
"""

from __future__ import annotations

from pathlib import Path

from app.agent import prompt
from app.agent.tools import TOOL_NAMES, WIKI_TOOLS


def test_skill_is_the_bound_file_not_a_runtime_skill_library():
    """技能从 app/agent/skill/llm-wiki.md 构建期装配；没有运行时加载这一层。"""
    assert prompt.SKILL_PATH == Path(prompt.__file__).parent / "skill" / "llm-wiki.md"
    assert prompt.SKILL_PATH.is_file()
    raw = prompt.SKILL_PATH.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    assert prompt.SKILL_VERSION == "1.7.0"
    assert prompt.SKILL_BODY
    # frontmatter 不进 BASE_STYLE：模型不该看到 version/tools 这些元数据行。
    assert "version:" not in prompt.BASE_STYLE
    assert "scope:" not in prompt.BASE_STYLE
    assert prompt.BASE_STYLE == prompt.SKILL_BODY


def test_skill_tool_inventory_matches_the_harness_exactly():
    """防漂移：技能声明的工具 == WIKI_TOOLS（名字与顺序都比）。"""
    assert [tool.name for tool in WIKI_TOOLS] == list(prompt.SKILL_TOOLS)
    assert set(prompt.SKILL_TOOLS) == set(TOOL_NAMES)
    # 每个工具在正文里都要出现一次，模型才知道什么时候用它。
    for name in TOOL_NAMES:
        assert name in prompt.BASE_STYLE, name


def test_skill_keeps_the_hard_rules():
    """安全与正确性的底线，压缩措辞可以，删掉不行。"""
    text = prompt.BASE_STYLE
    # 对齐 Hermes/LLM-wiki：来源是页级 sources，不要求句子级引注。
    assert "sources" in text
    assert "不要写句子级引注" in text
    assert "citations" not in text
    # slug：服务端发，不许凭印象造；目标页还没建时用标题链接（本次修复的工序）。
    assert "不能凭印象造" in text
    assert "[[页面标题]]" in text
    # 改页的并发保护。
    assert "base_commit" in text and "并发保护" in text
    # 收尾：lint 有 error 修到 0；修不掉要如实说"还剩 N 处"（系统记待修）。
    assert "lint()" in text and "error" in text and "还剩 N 处" in text
    # 本轮材料一轮内吸收完：批量建页，别一页一轮（MAX_TURNS 预算的教训）。
    assert "同一轮" in text
    # 执行纪律（2026-10-30 会话 27 的反复请示 + 假合并之后加的硬规则）。
    assert "执行纪律" in text
    assert "直接调工具开工" in text
    assert "不许把没做的说成做了" in text
    assert "本轮未改动" in text
    assert "list_wiki() 获取目录" in text
    # P0 协议（2026-10-30 Hermes 体检）：标题链接必须归一化、每页 ≥2 出链、entity 判定。
    assert "必须改回 `[[slug]]`" in text
    assert "每页至少 2 个出链" in text
    assert "entity 只给真实体" in text
    assert "confidence" in text
    assert "摄入覆盖率" in text and "source_coverage" in text
    # 预算意识（会话 28：7 轮全花在定位上，wrap-up 说"再调一次 write_page 即可"）。
    assert "预算意识" in text
    assert "普通轮次最多 30 次模型调用" in text
    assert "查询结果是否沉淀由你自己判断" in text
    assert "写页优先于凑清单" in text
    assert "SCHEMA.md" in text and "index.md" in text and "log.md" in text


def test_skill_never_reintroduces_private_chat_isolation():
    """裁定 5 / R-S5-5：语料没有私聊隔离，提示词里不许出现"私聊"。"""
    assert "私聊" not in prompt.BASE_STYLE
