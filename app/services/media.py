"""文档里内嵌的图片 → 视觉文本（PPT / PDF / DOCX）。

2026-10-30 起补齐：之前只有**独立图片文件**走视觉，PPT/PDF/DOCX 里的图片被静默忽略
（扫描版 PDF 甚至整份落 failed）。这里把内嵌图片也送 `vision.describe_image`，把描述
拼回文档正文，使"图片里的文字与结构"也能被检索、引注。

三条纪律：
1. **不静默丢**：每份文档的图片数、成功数、失败数、是否因预算截断都进 parse_report；
2. **去重与预算**：同一张图（按 sha256）只送一次；一份文档最多 IMAGE_DESC_MAX 张、
   总耗时不超过 IMAGE_DESC_BUDGET_SECONDS；超了记 `images_truncated: true`，注里说清；
3. **失败不连累整份文档**：单张图视觉失败只计入 images_failed，正文照常落库。

并发：视觉调用是网络阻塞，用 4 个线程并行；返回顺序按图片在文档中的出现顺序，
所以正文里的 `【第 N 页图片】` 不会乱。
"""

from __future__ import annotations

import hashlib
import io
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import vision

logger = logging.getLogger(__name__)

# 上限与预算（"不用太节省"：给到 1000 张/份、9 分钟/份；真正阻止无限烧钱的是去重与
# 视觉单图 10MB 上限，不是把数量卡在二三十张）
IMAGE_DESC_MAX = 1000
IMAGE_DESC_WORKERS = 4
IMAGE_DESC_BUDGET_SECONDS = 540
# 明显是图标/装饰的小图（<1 KB 且 <16x16）不必送视觉；其余一律描述。
IMAGE_MIN_BYTES = 1024
IMAGE_MIN_SIDE = 16

_SUPPORTED_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize(data: bytes, suffix: str) -> tuple[bytes, str] | None:
    """把任意图片字节转成视觉接口认识的一种：能直传就直传，否则 Pillow 转 PNG。"""
    suffix = suffix.lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    if suffix in _SUPPORTED_SUFFIX and data:
        return data, suffix
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
            return buffer.getvalue(), ".png"
    except Exception as exc:  # noqa: BLE001 - 转不了就不描述这一张
        logger.warning("文档内图片转 PNG 失败（%s）：%r", suffix, exc)
        return None


class ImageDescriptions:
    """一份文档里的内嵌图片收集与描述。

    用法：先 `add(label, data, suffix)` 收集，最后 `describe()` 一次并行描述；
    返回 [(label, text)]，调用方按 label 拼回对应页/文档尾部。
    """

    def __init__(self) -> None:
        self.total = 0
        self.described = 0
        self.failed = 0
        self.skipped = 0
        self.truncated = False
        self._seen: set[str] = set()
        self._jobs: list[tuple[str, bytes, str]] = []
        self._started = time.monotonic()

    def add(self, label: str, data: bytes | None, suffix: str) -> None:
        if not data:
            return
        self.total += 1
        normalized = _normalize(data, suffix)
        if normalized is None:
            self.skipped += 1
            return
        raw, suffix = normalized
        digest = _sha(raw)
        if digest in self._seen:
            # 同一张图在 PPT 母版/页脚里重复出现：只描述一次，拼回时由调用方决定是否重复放。
            self.skipped += 1
            return
        self._seen.add(digest)
        if len(raw) < IMAGE_MIN_BYTES:
            self.skipped += 1
            return
        if len(self._jobs) >= IMAGE_DESC_MAX:
            self.truncated = True
            return
        self._jobs.append((label, raw, suffix))

    def _describe_one(self, job: tuple[str, bytes, str]) -> tuple[str, str | None]:
        label, raw, suffix = job
        if time.monotonic() - self._started > IMAGE_DESC_BUDGET_SECONDS:
            self.truncated = True
            return label, None
        with tempfile.TemporaryDirectory(prefix="openllmwiki-docimg-") as tmp:
            path = Path(tmp) / f"image{suffix}"
            try:
                path.write_bytes(raw)
                return label, vision.describe_image(path)
            except OSError as exc:
                logger.warning("文档内图片落临时文件失败（%s）：%r", label, exc)
                return label, None

    def describe(self) -> list[tuple[str, str]]:
        """并行描述，按加入顺序返回成功项；失败/超预算的只计数。"""
        if not self._jobs:
            return []
        results: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=IMAGE_DESC_WORKERS) as pool:
            for label, text in pool.map(self._describe_one, self._jobs):
                if text is None:
                    self.failed += 1
                else:
                    self.described += 1
                    results.append((label, text))
        return results

    def report(self) -> dict:
        """能塞进 parse_report 的统计；没有图就返回空（旧材料的报告形状不变）。"""
        if self.total == 0:
            return {}
        data: dict = {
            "images_total": self.total,
            "images_described": self.described,
            "images_failed": self.failed,
        }
        if self.skipped:
            data["images_skipped"] = self.skipped
        if self.truncated:
            data["images_truncated"] = True
        return data

    def note(self) -> str | None:
        if self.total == 0:
            return None
        if self.truncated:
            return f"图片太多/超时，只描述了前 {self.described} 张"
        if self.described == 0 and self.failed > 0:
            return "检测到图片，但视觉模型没有返回可用描述"
        return None
