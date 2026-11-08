// 外壳的钉子：index.html 的 <head> 与 theme.css 里那几条**不进组件**的规则（终审 I3、收尾清理）。
//
// 这两处都不是逻辑，没法"跑"起来验，但都是承重的：
//   - 抽屉关着时那条 visibility 一去掉，≤700px 上键盘用户按 Tab 就会一路走进画布外那个
//     看不见的侧栏（焦点环消失、读屏念的是屏幕上没有的条目）；
//   - 打开方向那条 `visibility 0s` 一改回带延迟的，SessionList 的抽屉 effect 在 React 提交后
//     同步调的那次 focus() 就会落在一个 hidden 元素上被浏览器拒收，「＋ 新会话」拿不到焦点；
//   - <meta name="referrer"> 一去掉，/i/:token 里的邀请 token 就会跟着 Referer 出站到任何外链。
// 与 reconnect.test.ts / pages/Chat.test.ts 同一路数：形状钉死在文件里，谁改回去这条就红。
import { describe, expect, it } from "vitest";

import CSS from "./theme.css?raw";
import HTML from "../index.html?raw";

/** 全站只有一条 media query（≤700px 的窄屏）：它之前的是宽屏/通用规则，之后的是窄屏规则。 */
const MEDIA_AT = CSS.indexOf("@media (max-width: 700px) {");
const WIDE = CSS.slice(0, MEDIA_AT);
const NARROW = CSS.slice(MEDIA_AT);

describe("I3：≤700px 关着的抽屉退出 Tab 序", () => {
  it("两条规则都在 media query 里：关着 hidden、开着 visible", () => {
    expect(MEDIA_AT).toBeGreaterThan(0);
    expect(NARROW).toContain(".sb:not(.open) {\n    visibility: hidden;");
    expect(NARROW).toContain("visibility: visible;");
    // 各只有一处：visibility 不许在别的地方也被改一遍（那就是两套口径）
    expect(NARROW.match(/visibility: hidden;/g)).toHaveLength(1);
    expect(NARROW.match(/visibility: visible;/g)).toHaveLength(1);
  });

  it("宽屏（>700px）一条 visibility 都不挂：常驻侧栏必须一直在 Tab 序里", () => {
    expect(WIDE).not.toContain("visibility");
  });

  it("visibility 在 transition 里，两个方向的时长故意不一样", () => {
    // 关：延迟 0.18s = transform 的时长，等滑出去动画走完再隐（动画照旧看得见）
    expect(NARROW).toContain("transition: transform 0.18s ease, visibility 0s linear 0.18s;");
    // 开：0s、无延迟，加上 .open 那一下立刻可见，focus() 才不会被浏览器拒收
    expect(NARROW).toContain("transition: transform 0.18s ease, visibility 0s;");
  });

  it("滑进滑出那条 transform 过渡没被顺手改掉（开合动画还在）", () => {
    expect(NARROW).toContain("transform: translateX(-105%);");
    expect(NARROW).toContain(".sb.open {\n    transform: none;");
    expect(NARROW.match(/transform 0\.18s ease/g)?.length).toBeGreaterThanOrEqual(3);
  });
});

describe("index.html 的头", () => {
  it("不带 Referer 出站（URL 里有邀请 token）", () => {
    expect(HTML).toContain('<meta name="referrer" content="no-referrer" />');
  });

  it("图标是内联 SVG data URI，于是浏览器不再去请求 /favicon.ico", () => {
    // 那条请求会落进 app/main.py 的 SPA 回退，拿回一整页 index.html 当图标（浏览器只会
    // 忽略它），access log 里平白多一行、每次冷启动多一次往返
    expect(HTML).toContain('rel="icon"');
    expect(HTML).toContain('href="data:image/svg+xml,');
    // 也不许换成另一个要请求的文件（那就得动 deploy 的 rsync 清单与 /assets 挂载）
    expect(HTML).not.toMatch(/href="\/[^"]*\.(ico|png|svg|webp)"/);
    expect(HTML.match(/rel="icon"/g)).toHaveLength(1);
  });

  it("入口页的基本形状没动（挂载点与 module 脚本还在）", () => {
    expect(HTML).toContain('<html lang="zh-CN">');
    expect(HTML).toContain('<div id="root"></div>');
    expect(HTML).toContain('<script type="module" src="/src/main.tsx"></script>');
  });
});
