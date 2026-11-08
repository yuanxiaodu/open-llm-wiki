"""启动自检（裁定 R73）：一台从没跑过 `python -m app.cli seed` 的机器也要能开。

未 seed 时 data/wiki 压根不存在，读侧的 git 子进程连 cwd 都进不去，GET /api/log
直接 500 中文信封（GET /api/wiki 在"data/wiki 有页但没 .git"这种半开形状上同样 500）。
R73 之后 lifespan 幂等地把仓库建出来，空站的读接口就是 200 + 空列表。

lifespan **只**建仓库：管理员账号与空大纲页仍然归 cli seed（裁定 R34），
下面按"没有"把这条边界钉住。

S2 起 lifespan 还多干一件事：把 OpenAI Agents SDK 的 tracing 关掉（计划 Global
Constraints 的验收项是"抓包确认无发往 api.openai.com 的请求"），最后一条测试钉它。
"""

from __future__ import annotations

import pytest
from agents import tracing
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import auth
from tests.conftest import ADMIN_PASSWORD, ADMIN_PHONE, fetch_one, make_admin


@pytest.fixture
def fresh_data_dir(tmp_path):
    """把 settings 指到一个全新的 DATA_DIR，模拟刚部署、没 seed 过的机器。

    settings 是 frozen dataclass 的模块级单例，全项目都 `from ..config import settings`
    拿同一个对象，而 db_path/wiki_dir/uploads_dir 全是 @property 现算的（没有一处
    模块级缓存），所以改 data_dir 一个字段就把整套路径挪走了。frozen 实例只能
    object.__setattr__（monkeypatch.setattr 走 setattr，会 FrozenInstanceError）；
    用完必须还原，否则本文件之后的测试会往这个临时目录里写。
    """
    original = settings.data_dir
    original_wiki = settings.wiki_dir_override
    object.__setattr__(settings, "data_dir", tmp_path)
    object.__setattr__(settings, "wiki_dir_override", None)
    auth.throttle.reset()  # 登录限速是模块级状态，别被前一个测试的失败次数连坐
    try:
        yield tmp_path
    finally:
        object.__setattr__(settings, "data_dir", original)
        object.__setattr__(settings, "wiki_dir_override", original_wiki)


def test_unseeded_instance_serves_empty_wiki_and_log(fresh_data_dir):
    wiki_dir = fresh_data_dir / "wiki"
    assert not wiki_dir.exists(), "这台机器没 seed 过，data/wiki 本该不存在"

    # 进 with 才跑 lifespan：TestClient 不进上下文管理器就不触发启动事件
    with TestClient(app) as client:
        # 启动把仓库与 LLM-Wiki vault 建出来了（SCHEMA/index/log + 一次初始化 commit），
        # 但没顺手建管理员（那是 cli seed 的活）
        assert (wiki_dir / ".git").exists()
        for name in ("SCHEMA.md", "index.md", "log.md"):
            assert (wiki_dir / name).exists(), name
        assert not (wiki_dir / "_outline_.md").exists()
        assert fetch_one("SELECT COUNT(*) AS n FROM users")["n"] == 0

        # 两条读接口都要登录，所以就地插一个管理员。这是测试自己写的一行，
        # 不是 cli seed —— seed 会连带建仓库与大纲页，正是这条测试要避开的。
        make_admin()
        login = client.post(
            "/api/login", json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD}
        )
        assert login.status_code == 200, login.text[:400]

        resp = client.get("/api/wiki")
        assert resp.status_code == 200, resp.text[:400]
        assert resp.json() == {"pages": [], "count": 0}

        resp = client.get("/api/log")
        assert resp.status_code == 200, resp.text[:400]
        assert [e["note"] for e in resp.json()] == ["初始化 LLM-Wiki vault"]

    # 再启动一次：仓库已在，ensure_repo 幂等（不重复 init、不凭空多出提交）
    with TestClient(app) as client:
        assert (wiki_dir / ".git").exists()
        client.post("/api/login", json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD})
        resp = client.get("/api/log")
        assert resp.status_code == 200, resp.text[:400]
        assert [e["note"] for e in resp.json()] == ["初始化 LLM-Wiki vault"]


def test_lifespan_disables_sdk_tracing(fresh_data_dir):
    """计划 Global Constraints：进程启动时调一次 `set_tracing_disabled(True)`。

    SDK 默认把每一轮的 trace 发去 OpenAI 自己的后端（api.openai.com）。我们用的是 DeepSeek 的
    兼容端点，那些请求既没用也不该发——验收项之一就是"抓包或日志确认进程启动后无发往
    api.openai.com 的请求"。这里钉的是那个开关本身：真去抓包不在单测的能力范围内。

    0.24.0 没有公开的读取口（`agents.tracing` 只导出 set_tracing_disabled），能读的只有
    provider 上那个 `_disabled`（set_disabled 写 _manual_disabled，再由
    _refresh_disabled_flag 与 env 的那一路合成到 _disabled）——所以断言读的是它。
    SDK 升级换了内部名字，这条会红，那时候去找新的读取口，别把断言删掉。

    先摁回 False 再看：这个开关是**进程级全局**，同一次 pytest 里别的用例跑过 lifespan
    之后它就已经是 True 了，不摁回去的话这条断言恒真、什么都测不出来。
    """
    provider = tracing.get_trace_provider()
    provider.set_disabled(False)
    assert provider._disabled is False

    with TestClient(app):        # 进 with 才跑 lifespan
        assert provider._disabled is True, "lifespan 没把 SDK 的 tracing 关掉"
