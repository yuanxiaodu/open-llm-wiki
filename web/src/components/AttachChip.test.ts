// 附件胶囊那句「含 N 页图片，未入检索」的钉子（spec §7 的原文）。
//
// 为什么要钉：这句话是**唯一**一处界面向人承认"检索里缺东西"的地方，说错了两种坏——
// 该说的不说，人以为材料全进了检索，引注找不到出处时只会怀疑智能体；不该说的说了
// （单页的图、docx/txt/md 这些 pages_total 恒为 1 的材料），就是一句凭空编出来的属性。
// 判据因此是"两条都成立"，而 N 取的是 pages_image_only（缺的那几页）而不是 pages_total。
//
// imageOnlyHint 是纯函数（收 parse_report，返一句话或 null），所以不需要 jsdom；
// 胶囊本身怎么摆由 theme.css 与手工验收看。
import { describe, expect, it } from "vitest";

import { iconLabel, imageCountHint, imageOnlyHint } from "./AttachChip.tsx";

describe("imageOnlyHint：只有多页且真有纯图片页才开口", () => {
  it("12 页里 3 页只有图 → 「含 3 页图片，未入检索」（N 是缺的那几页，不是一共几页）", () => {
    expect(imageOnlyHint({ pages_total: 12, pages_image_only: 3 })).toBe("含 3 页图片，未入检索");
  });

  it("整本都是图（total === imageOnly）也说，N 就是页数", () => {
    expect(imageOnlyHint({ pages_total: 8, pages_image_only: 8 })).toBe("含 8 页图片，未入检索");
  });

  it("最小的那一档：2 页里 1 页只有图", () => {
    expect(imageOnlyHint({ pages_total: 2, pages_image_only: 1 })).toBe("含 1 页图片，未入检索");
  });

  it("单页（pages_total === 1）不说：截图、单页扫描件，以及 pages_total 恒为 1 的 docx/txt/md", () => {
    // 这一条是承重的：把 pages_total 当"页数"摆出来的老毛病就是栽在这儿——
    // 一个 docx 也会得到 pages_total: 1，凭它就开口等于给它编一个"图片页"的属性。
    expect(imageOnlyHint({ pages_total: 1, pages_image_only: 1 })).toBeNull();
    expect(imageOnlyHint({ pages_total: 1, pages_image_only: 0 })).toBeNull();
  });

  it("一页图都没有（pages_image_only === 0）不说：没缺就别吓人", () => {
    expect(imageOnlyHint({ pages_total: 40, pages_image_only: 0 })).toBeNull();
  });

  it("报告缺失、空对象、键不在 → 不说", () => {
    expect(imageOnlyHint(null)).toBeNull();
    expect(imageOnlyHint({})).toBeNull();
    expect(imageOnlyHint({ pages_total: 12 })).toBeNull();
    expect(imageOnlyHint({ pages_image_only: 3 })).toBeNull();
  });

  it.each([
    { pages_total: "12", pages_image_only: "3" }, // 串不是数（后端交的是 int，但形状是自由的）
    { pages_total: null, pages_image_only: 3 },
    { pages_total: 12, pages_image_only: null },
    { pages_total: Number.NaN, pages_image_only: 3 },
    { pages_total: 12, pages_image_only: Number.POSITIVE_INFINITY },
  ])("键不是有限的数（%o）→ 不说，绝不拼出「含 NaN 页图片」", (report) => {
    const got = imageOnlyHint(report);
    expect(got).toBeNull();
  });

  it("负数与「图片页比总页数还多」这种脏报告也不说", () => {
    expect(imageOnlyHint({ pages_total: -1, pages_image_only: 3 })).toBeNull();
    expect(imageOnlyHint({ pages_total: 3, pages_image_only: -2 })).toBeNull();
  });

  it("其余的键一概不读（pages_with_text 一类摆出来就得给它起名，起名就是撒谎）", () => {
    expect(
      imageOnlyHint({ pages_total: 12, pages_image_only: 0, pages_with_text: 12, chars: 40213 }),
    ).toBeNull();
  });
});


describe("iconLabel：多来源摄入新增的类型也认", () => {
  it("pdf/word/excel/ppt/epub/图片/文本各有短标签；bmp 归图片", () => {
    expect(iconLabel("a.pdf")).toBe("PDF");
    expect(iconLabel("a.docx")).toBe("DOC");
    expect(iconLabel("a.pptx")).toBe("PPT");
    expect(iconLabel("a.epub")).toBe("EPUB");
    expect(iconLabel("a.xlsx")).toBe("表");
    expect(iconLabel("a.bmp")).toBe("图");
    expect(iconLabel("a.md")).toBe("MD");
    expect(iconLabel("a.bin")).toBe("文件");
  });
});

describe("imageCountHint：文档内嵌图片的识别情况（2026-10-30）", () => {
  it("没有图片键就什么都不说", () => {
    expect(imageCountHint(null)).toBeNull();
    expect(imageCountHint({ pages_total: 2 })).toBeNull();
    expect(imageCountHint({ images_total: 0 })).toBeNull();
  });

  it("有图就报总数与识别数；截断时说清只识别了前 N 张", () => {
    expect(imageCountHint({ images_total: 5, images_described: 5 })).toBe("含 5 张图片，已识别 5 张");
    expect(imageCountHint({ images_total: 5, images_described: 2, images_truncated: true }))
      .toBe("含 5 张图片，已识别前 2 张");
    expect(imageCountHint({ images_total: 5, images_described: 0 })).toBe("含 5 张图片，未识别");
  });
});
