"""DeepSeek 的 OpenAI 兼容客户端 —— S2 起只干三件事：装配模型、拼思考开关、翻译错误。

S1 那套自己拼 messages、自己迭代 chunk 的 `stream_text` / `chat_json` 删掉了（连带
同步的 `OpenAI` client）：S2 的模型调用整条交给 OpenAI Agents SDK（app/agent/runner.py），
循环、工具、流式事件都是它的活。这一层只剩 SDK 需要的一个 `Model` 对象、一份
`ModelSettings`（思考模式开关，见 `model_settings`），和"把 openai 的异常翻成我们自己的两种"。

**两处承重的注入口子**（Task 4b 的线格式测试靠它们打桩，别改成局部 import，也别写成
`openai.AsyncOpenAI(...)`）：

- `AsyncOpenAI` 留在模块级：断流那种形状 mock 服务器演不出来，测试把这个名字换成走
  自定义 httpx transport 的 client；
- `make_model()` 现读 `settings`：测"真装配 + 假上游"时把 `settings.llm_base_url`
  指到本地 mock 服务器，这样装配本身也在链路里，不用在 runner 上另开一个注入口子。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openai import APIStatusError, AsyncOpenAI, OpenAIError

from ..config import settings

if TYPE_CHECKING:
    from agents import ModelSettings
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel


class QuotaExhausted(Exception):
    """429/402：上游日额度、速率上限，或账户余额不足。

    **不是** LLMError 的子类，两者也没有继承关系：runner 按类型分流（429/402 出
    system 事件、前端标"待重试"，其它出 error），有继承关系就会被 `except LLMError`
    吃成故障。
    """


class LLMError(Exception):
    """上游的其它故障（5xx、连不上、超时、没配 key）。"""


def make_model() -> OpenAIChatCompletionsModel:
    """装配 SDK 要的那个 chat-completions 模型（计划 Task 4 的口径）。

    `max_retries=0` 是承重的：openai 的 client 默认自己重试两次（429 也重试），
    一次额度用尽要等三轮往返才落到人眼前，SSE 上就是"发了消息半天没动静"。
    更正一句早先注释里的错话（M2）：SDK 那头的重试是 **opt-in** 的（ModelSettings
    一类的显式配置），我们没有开——所以今天整条链上的重试层数是**零**，不存在
    "client 一层 + SDK 一层"的叠加；client 上这个 max_retries=0 是唯一的闸，
    把它拿掉（或改回默认值）就等于凭空多两层看不见的重试。

    SDK 的 import 留在函数里（照计划 Task 4 的写法）：import `app.services.llm` 的脚本
    一类不必连带把整个 agents 拖进来。先判 key 再 import，是为了"没配 key"这句人话
    不被一句 ImportError 顶掉——那是运维最先撞上的一种错。
    """
    if not settings.llm_api_key:
        raise LLMError("LLM_API_KEY（旧名 DEEPSEEK_API_KEY） 没配。复制 .env.example 填上")
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        max_retries=0,
    )
    return OpenAIChatCompletionsModel(model=settings.text_model, openai_client=client)


def model_settings() -> ModelSettings:
    """这一轮调用要带的 ModelSettings：只做一件承重的事——把 DeepSeek 的思考模式
    显式关掉（默认取值见 settings.llm_thinking）。

    为什么必须显式关：DeepSeek 的思考模式**默认打开**，而它要求携带 tools 的请求把
    历史里的 `reasoning_content` 完整回传，否则直接 400。我们的 harness（Agents SDK 的
    chat-completions 适配层）只保留 assistant 正文与 tool_calls，不保存思考内容，所以
    默认开着思考 = 第一次工具调用之后的每一次请求都可能被上游打回。Chat Completions
    的开关在请求体顶层 `thinking` 里，SDK 的 ModelSettings 没有对应字段，只能走
    `extra_body`（SDK 0.24.0 的 OpenAIChatCompletionsModel 会把它原样塞进 create_kwargs）。

    `LLM_THINKING=enabled` 只留给将来"harness 能回传 reasoning_content"的可选路径，
    默认路径不许开——见 settings 里那段注释。
    """
    from agents import ModelSettings

    return ModelSettings(extra_body={"thinking": {"type": settings.llm_thinking}})


def is_quota_error(exc: BaseException) -> bool:
    """额度类：429（速率/日额度）与 402（余额不足）。

    402 是 DeepSeek 的"余额不够"，对人是同一件事——等额度回来再发，所以和 429 同口径
    走 system 事件，不该按上游故障渲染成红的。
    """
    return isinstance(exc, APIStatusError) and exc.status_code in (429, 402)


def translate_openai_error(exc: BaseException) -> BaseException:
    """openai 的异常 → 我们自己的两种；不是 openai 的原样交回去（同一个对象，不包一层）。

    429/402 → QuotaExhausted，其它状态码 → 带状态码的 LLMError，连不上/超时 → LLMError。
    不是 openai 的异常不许翻：那是我们自己的 bug，翻成 LLMError 就查不出源头了。

    runner 的分流不靠它（它直接 `except openai.APIStatusError` 读 status_code，
    因为 SDK 把 openai 的异常原样抛出来，翻一道反而多一层间接）；这一支是给
    "手上只有一个异常对象、想知道是不是额度问题"的调用方用的（脚本、S3 的重试提示）。
    """
    if is_quota_error(exc):
        return QuotaExhausted(str(exc))
    if isinstance(exc, APIStatusError):
        return LLMError(f"模型返回 {exc.status_code}: {exc}")
    if isinstance(exc, OpenAIError):
        return LLMError(f"模型调用失败: {exc}")
    return exc
