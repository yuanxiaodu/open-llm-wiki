"""Load the repository's bound wiki skill once, keeping version and tool inventory
available for drift checks. The Markdown skill is the reviewable agent policy;
permissions, evidence integrity and publication are also enforced by code.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("openllmwiki.agent")

# 技能文件位置：与 prompt.py 同目录下的 skill/llm-wiki.md。用 __file__ 定位，不吃
# cwd（服务是 systemd 起的，cwd 不保证是仓库根）；也不放 data/——它属于代码，跟着
# 部署清单走，不跟数据备份走。
SKILL_PATH = Path(__file__).resolve().parent / "skill" / "llm-wiki.md"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)
_TOOLS_RE = re.compile(r"^tools:\s*(.+)$", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """`---\\nfrontmatter\\n---\\n正文` → (frontmatter, 正文)；没有 frontmatter 就整篇当正文。"""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return "", text
    return match.group(1), text[match.end():]


def _load_skill() -> tuple[str, str, tuple[str, ...]]:
    """读技能文件，返回 (正文, 版本, 工具清单)。

    文件不存在就让 import 直接炸：这是部署事故（rsync 漏了 app/agent/skill/），
    不该降级成"没有系统提示词照样跑"——那会让智能体用默认人格乱写知识库。
    """
    front, body = _split_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    version_match = _VERSION_RE.search(front)
    tools_match = _TOOLS_RE.search(front)
    version = version_match.group(1) if version_match else ""
    tools = tuple(
        item.strip() for item in (tools_match.group(1).split(",") if tools_match else [])
        if item.strip()
    )
    return body.strip(), version, tools


# 正文、版本、工具清单三样在 import 时定死。runner 只认 BASE_STYLE；版本给日志与
# 测试用；工具清单给 tests/test_prompt.py 钉技能与 harness 的一致性。
SKILL_BODY, SKILL_VERSION, SKILL_TOOLS = _load_skill()
BASE_STYLE = SKILL_BODY

logger.debug("已装配 wiki 技能 %s（%s，%d 字）", SKILL_VERSION, SKILL_PATH.name, len(BASE_STYLE))
