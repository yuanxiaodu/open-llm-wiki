"""图片 → 文本：DeepSeek 视觉（OpenAI 兼容 `chat.completions`）。

多来源摄入（S5b）里图片走这里：把图片转成 base64 data URL，和一段"抽取图中文字 +
描述结构"的提示语一起交给视觉模型。模型就是 `settings.text_model`（deepseek-flash
支持图像理解），上游与 key 与文本模型同一套（`settings.llm_base_url/llm_api_key`）。

接口口径（2026-10 DeepSeek 视觉文档核实）：
- 支持格式：JPEG / PNG / GIF / WebP；**BMP 不在支持列表**——这里用 Pillow 先转 PNG
  再送（Pillow 由 python-pptx 传递依赖，环境里本来就有）。
- OpenAI 兼容 content 形状：`[{"type":"text",...},{"type":"image_url","image_url":{...}}]`，
  `detail` 可用 `low`（更快更省 token）或 `high/original`；抽文字与图表数据要看清，
  这里用 `high`。图片只能出现在 user 消息里。
- 单图上限 32 MiB：我们自己在 `VISION_MAX_BYTES=10 MB` 处先拦（与任务口径一致），
  到不了上游的硬上限。

失败一律返回 None（没配 key、调用炸了、模型回空）——调用方（uploads.extract）据此把
parse_status 记 failed，上传本身不受影响。**测试一律 monkeypatch `describe_image`**：
这条链路会真花钱，CI 不碰真上游。
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

# 图片大小上限：超过就不送视觉模型（任务口径 10 MB；上游单图硬上限是 32 MiB，够不着）。
VISION_MAX_BYTES = 10 * 1024 * 1024

# 直接支持的 MIME（与 DeepSeek 视觉文档一致）。BMP 要转 PNG，所以不在此表。
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
IMAGE_SUFFIXES = tuple(MIME_BY_SUFFIX) + (".bmp",)

MAX_TOKENS = 4096

PROMPT = (
    "请把这张图片里的信息整理成 markdown：\n"
    "1) 提取图中全部文字，按原有的标题层级、列表、表格结构组织（表格用 markdown 表格）；\n"
    "2) 图表/照片/截图描述其结构与关键数据（坐标轴、图例、趋势、数值）；\n"
    "3) 看不清或图中没有的内容不要编造，宁可少写。"
)


class VisionError(RuntimeError):
    """视觉调用本身出问题（没配 key、上游报错）。消息是给人看的中文。"""


def describe_image(path: Path) -> str | None:
    """一张图 → 文本（markdown）；失败/空返 None。

    大小先在本层再挡一次（调用方也会挡）：>10 MB 直接 None，日志里留痕。
    .bmp 先经 Pillow 转 PNG 再送（DeepSeek 不支持 BMP）；转换失败同样 None。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("读不到图片 %s：%r", path.name, exc)
        return None
    if size > VISION_MAX_BYTES:
        logger.warning("图片 %s 超过 10 MB（%d 字节），不送视觉模型", path.name, size)
        return None
    data_url = _data_url(path)
    if data_url is None:
        return None
    if not settings.llm_api_key:
        logger.warning("图片 %s 没送视觉模型：LLM_API_KEY（旧名 DEEPSEEK_API_KEY） 没配", path.name)
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            max_retries=0,
            # 单图最多等 60s：文档里有几十张图时，一次挂死的调用不能把整份解析拖住
            timeout=60.0,
        )
        response = client.chat.completions.create(
            model=settings.text_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": data_url, "detail": "high"}},
                ],
            }],
            max_tokens=MAX_TOKENS,
        )
        content = response.choices[0].message.content
    except Exception as exc:  # noqa: BLE001 - 视觉是"尽力而为"：失败只让这一份材料解析失败
        logger.warning("图片 %s 视觉调用失败：%r", path.name, exc)
        return None
    text = (content or "").strip()
    if not text:
        logger.warning("图片 %s 视觉模型回了空文本", path.name)
        return None
    return text


def _data_url(path: Path) -> str | None:
    """图片 → `data:<mime>;base64,...`。BMP 先转 PNG（DeepSeek 不认 BMP）。"""
    suffix = path.suffix.lower()
    if suffix == ".bmp":
        png = _bmp_png_bytes(path)
        if png is None:
            return None
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        logger.warning("图片 %s 的格式不支持（%s）", path.name, suffix)
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("读不到图片 %s：%r", path.name, exc)
        return None
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _bmp_png_bytes(path: Path) -> bytes | None:
    """BMP → PNG 字节。Pillow 不在/转不动就 None（不抛，调用方按解析失败处理）。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - 缺 Pillow/坏 bmp 都只是这一份解析失败
        logger.warning("BMP 转 PNG 失败 %s：%r", path.name, exc)
        return None
