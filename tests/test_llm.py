"""llm 这一层（S2 起）只剩两件事：装配 SDK 要的那个模型、把 openai 的异常翻成我们自己的两种。

不打真上游、也不花委托人的配额：

- `make_model` 只装配不发请求，所以"用哪个模型名、指哪个端点、client 自己还重不重试"
  直接读装配出来的对象就能钉住；
- `model_settings` 只造配置不发请求：思考模式默认关（DeepSeek 默认开，而带 tools 的
  请求要求回传历史 reasoning_content，harness 不存它）；
- 错误映射用手工造的 `APIStatusError` / `APIConnectionError` / `APITimeoutError` 打
  （429/402 与别的状态码必须分成两种异常，前端要据此把会话标"待重试"还是"出错了"）。

真发请求的那一层（SDK 把 chat-completions 的 SSE 翻成什么事件、429/402 从上游真回来时
runner 怎么分流）在 Task 4b 的线格式测试里用本地 mock 服务器验，那边顺带也把
`make_model` 走了一遍——它把 `settings.llm_base_url` 指过去，或者把本模块的
`AsyncOpenAI` 换成走自定义 transport 的（断流那种形状演不了服务器）。**所以这两处是
承重的注入口子：`AsyncOpenAI` 必须留在模块级、`make_model` 必须读 `settings`。**
"""

from __future__ import annotations

import httpx
import pytest
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.services import llm


def _set_settings(**fields):
    """改 settings 的字段并登记还原（frozen dataclass 单例，只能 object.__setattr__）。"""
    for name, value in fields.items():
        _RESTORE.append((name, getattr(settings, name)))
        object.__setattr__(settings, name, value)


_RESTORE: list[tuple[str, object]] = []


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    while _RESTORE:
        name, original = _RESTORE.pop()
        object.__setattr__(settings, name, original)


def _status_error(code: int) -> APIStatusError:
    """照 openai 的形状造一个状态码异常（不打网络，只要 response/body 两样入参）。"""
    return APIStatusError(
        f"上游说了 {code}",
        response=httpx.Response(code, request=httpx.Request("POST", "http://x")),
        body=None,
    )


# --- make_model：装配 ------------------------------------------------------------


def test_make_model_wires_the_deepseek_endpoint_and_turns_off_client_retries():
    """模型名与端点都取自 settings，client 自己那层重试关掉。

    `max_retries=0` 是承重的：openai 的 client 默认自己重试两次（429 也重试），SDK 那头
    还有一层自己的重试。两层叠着，一次额度用尽要等三轮往返才落到人眼前，SSE 上就是
    "发了消息半天没动静"——重试只该有一层，留给 SDK（它有 retry-after 的判断）。
    """
    _set_settings(
        llm_api_key="k1",
        llm_base_url="http://127.0.0.1:9/v1",
        text_model="deepseek-test",
    )

    model = llm.make_model()

    assert isinstance(model, OpenAIChatCompletionsModel)
    assert model.model == "deepseek-test"
    client = model._client
    assert isinstance(client, AsyncOpenAI)          # 流式那一路要的是异步 client
    assert client.api_key == "k1"
    assert str(client.base_url).startswith("http://127.0.0.1:9/v1")
    assert client.max_retries == 0


def test_make_model_without_a_key_says_so_in_chinese():
    """没配 key 时不许把 openai 那句英文抛给人：一句中文指路 .env.example。"""
    _set_settings(llm_api_key="")

    with pytest.raises(llm.LLMError) as caught:
        llm.make_model()

    assert "DEEPSEEK_API_KEY" in str(caught.value)
    assert ".env.example" in str(caught.value)


# --- model_settings：思考模式开关 --------------------------------------------------


def test_model_settings_disables_thinking_by_default():
    """默认必须显式关思考：DeepSeek 思考默认开，而带 tools 的请求要求回传历史
    reasoning_content——harness 不存它，开着就会被 400 打回。"""
    _set_settings(llm_thinking="disabled")

    settings_obj = llm.model_settings()

    assert settings_obj.extra_body == {"thinking": {"type": "disabled"}}


def test_model_settings_can_enable_thinking_for_a_future_path():
    """`LLM_THINKING=enabled` 只作为将来可选路径：这里钉住它确实会透传到 extra_body。"""
    _set_settings(llm_thinking="enabled")

    assert llm.model_settings().extra_body == {"thinking": {"type": "enabled"}}


# --- 错误映射：429/402 与其它 ----------------------------------------------------


def test_a_429_maps_to_quota_exhausted():
    """spec §8 的分流依据：额度用尽是"等一等就好"，不是故障，必须是**另一种**异常。"""
    exc = _status_error(429)

    assert llm.is_quota_error(exc) is True
    mapped = llm.translate_openai_error(exc)
    assert isinstance(mapped, llm.QuotaExhausted)
    # 两种异常不许有继承关系：runner 按类型分流（429 出 system 事件，其它出 error），
    # QuotaExhausted 若是 LLMError 的子类，`except LLMError` 会把额度用尽也吃成故障。
    assert not isinstance(mapped, llm.LLMError)
    # 原文留在 str 里：服务端日志要看得出上游到底说了什么
    assert str(exc) in str(mapped)


def test_a_402_insufficient_balance_is_quota_too():
    """DeepSeek 的 402（余额不足）与 429 同口径：人该看到"额度暂时用完"，不是上游故障。"""
    exc = _status_error(402)

    assert llm.is_quota_error(exc) is True
    mapped = llm.translate_openai_error(exc)
    assert isinstance(mapped, llm.QuotaExhausted)
    assert not isinstance(mapped, llm.LLMError)
    assert str(exc) in str(mapped)


def test_other_status_codes_map_to_llm_error_carrying_the_code():
    mapped = llm.translate_openai_error(_status_error(503))
    assert isinstance(mapped, llm.LLMError) and not isinstance(mapped, llm.QuotaExhausted)
    assert "503" in str(mapped)

    assert llm.is_quota_error(_status_error(503)) is False
    assert isinstance(llm.translate_openai_error(_status_error(401)), llm.LLMError)


def test_connection_and_timeout_errors_map_to_llm_error():
    """连不上与超时对人是一件事（"稍后再试"），都归 LLMError，不是 QuotaExhausted。"""
    request = httpx.Request("POST", "http://x")
    for exc in (APIConnectionError(request=request), APITimeoutError(request=request)):
        mapped = llm.translate_openai_error(exc)
        assert isinstance(mapped, llm.LLMError), exc
        assert not isinstance(mapped, llm.QuotaExhausted)
        assert llm.is_quota_error(exc) is False


def test_a_non_openai_exception_is_handed_back_unchanged():
    """不是 openai 的异常不许被翻成 LLMError：那是我们自己的 bug，翻掉就查不出来了。"""
    exc = RuntimeError("别的")

    assert llm.translate_openai_error(exc) is exc
    assert llm.is_quota_error(exc) is False


# --- S1 那两个自己拼 messages 的入口已经删干净 -----------------------------------


def test_the_s1_call_paths_are_gone():
    """S2 的模型调用整条交给 SDK：留着 stream_text/chat_json 就是第二条调用路径。

    第二条路径的代价是"重试、错误映射、流式解析各写两份"，而两份一定会漂。
    同步的 `OpenAI` client 也一并删了（make_model 要的是 AsyncOpenAI）。
    """
    for name in ("stream_text", "chat_json", "OpenAI"):
        assert not hasattr(llm, name), name
    assert hasattr(llm, "AsyncOpenAI")               # 线格式测试 monkeypatch 的就是它
