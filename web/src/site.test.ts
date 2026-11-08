// site.ts 的钉子：站名是异步来的展示信息，默认值 / 成功 / 失败三种状态都必须稳。
//
// 为什么这三件事承重（task-1）：
//   - 初值必须立刻可读（默认站名），否则登录页在接口回来前会闪一屏空白；
//   - 成功时必须同时更新推送快照与 document.title，并通知订阅者——只更新快照的话，
//     已挂载的 AppShell / Login / Chat 不会重渲染，界面停在旧值；
//   - **失败必须静默**：站名只是装饰，接口挂了/断网了不能让登录或对话跟着挂，更不能
//     借 api.ts 的 401 跳转语义把人整页踢去 /login（那是站点名接口不该有的权力）。
//
// vitest 跑在 node 环境、不读 .env，所以 DEFAULT_SITE_NAME 是确定值；模块级快照要靠
// vi.resetModules() + 动态 import 拿干净实例，绝不能依赖用例执行顺序。
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SITE_SRC from "./site.ts?raw";

const DEFAULT = "知识库";

beforeEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** 一个够 loadSiteName 用的假响应（不依赖真实网络，也不依赖 node 一定有 Response）。 */
function jsonResponse(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 500, json: async () => body };
}

async function freshSite() {
  return await import("./site.ts");
}

function stubFetch(impl: () => Promise<unknown>) {
  const mock = vi.fn(impl);
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("DEFAULT_SITE_NAME 与 useSiteName 的初值", () => {
  it("默认站名是「知识库」，与后端 app/config.py 的 DEFAULT_SITE_NAME 同值", async () => {
    const { DEFAULT_SITE_NAME } = await freshSite();
    expect(DEFAULT_SITE_NAME).toBe(DEFAULT);
  });

  it("接口还没回来时 useSiteName 用默认值：登录页不闪空白", async () => {
    const { useSiteName, DEFAULT_SITE_NAME } = await freshSite();
    function Probe() {
      return createElement("span", null, useSiteName());
    }
    expect(renderToStaticMarkup(createElement(Probe))).toBe(
      `<span>${DEFAULT_SITE_NAME}</span>`,
    );
  });
});

describe("loadSiteName：成功时更新快照、标题与订阅者", () => {
  it("GET /api/site 的 name 写进快照和 document.title，并通知订阅者", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ name: "某站" }));
    const doc = { title: "旧标题" };
    vi.stubGlobal("document", doc);

    const { loadSiteName, getSiteName, subscribeSiteName } = await freshSite();
    const seen: string[] = [];
    const unsubscribe = subscribeSiteName(() => seen.push(getSiteName()));

    await loadSiteName();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/site");
    expect(getSiteName()).toBe("某站");
    expect(doc.title).toBe("某站");
    expect(seen).toEqual(["某站"]);
    unsubscribe();
  });

  it("服务端已 strip 过，这里再 strip 一次也不会把带空格的站名原样塞进标题", async () => {
    stubFetch(async () => jsonResponse({ name: "  某站  " }));
    const { loadSiteName, getSiteName } = await freshSite();
    await loadSiteName();
    expect(getSiteName()).toBe("某站");
  });

  it("成功后不再重复请求：App 的 effect 只该打一次接口", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ name: "某站" }));
    const { loadSiteName } = await freshSite();
    await loadSiteName();
    await loadSiteName();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("并发触发只打一次接口（StrictMode 会把 effect 跑两次）", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ name: "某站" }));
    const { loadSiteName } = await freshSite();
    await Promise.all([loadSiteName(), loadSiteName()]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("loadSiteName：失败一律静默，继续用默认站名", () => {
  it.each([
    ["网络层抛错", async () => { throw new Error("连不上"); }],
    ["非 2xx", async () => jsonResponse({ name: "不该采信" }, false)],
    [
      "响应体不是 JSON",
      async () => ({ ok: true, status: 200, json: async () => { throw new Error("坏 JSON"); } }),
    ],
    ["缺 name", async () => jsonResponse({})],
    ["name 不是字符串", async () => jsonResponse({ name: 42 })],
    ["name 全是空白", async () => jsonResponse({ name: "   " })],
  ])("%s → 不抛、不改快照", async (_label, impl) => {
    const fetchMock = stubFetch(impl);
    const { loadSiteName, getSiteName } = await freshSite();
    await expect(loadSiteName()).resolves.toBeUndefined();
    expect(getSiteName()).toBe(DEFAULT);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("失败之后还能重试（失败不把 loaded 置位）", async () => {
    let calls = 0;
    const fetchMock = stubFetch(async () => {
      calls += 1;
      if (calls === 1) throw new Error("第一次失败");
      return jsonResponse({ name: "第二次成了" });
    });
    const { loadSiteName, getSiteName } = await freshSite();
    await loadSiteName();
    expect(getSiteName()).toBe(DEFAULT);
    await loadSiteName();
    expect(getSiteName()).toBe("第二次成了");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("没有 document 时也不炸（node/SSR 里拿不到站点名接口）", async () => {
    stubFetch(async () => jsonResponse({ name: "某站" }));
    const { loadSiteName, getSiteName } = await freshSite();
    await expect(loadSiteName()).resolves.toBeUndefined();
    expect(getSiteName()).toBe("某站");
  });
});

describe("插件与 settled：/api/site 的 plugins 只保留合法项", () => {
  it("plugins 归一化：缺字段/坏形状丢弃，合法项进快照", async () => {
    stubFetch(async () => jsonResponse({
      name: "某站",
      plugins: [
        { id: "demo", name: "示例", nav: { key: "demo", to: "/demo", label: "示例", short: "例", order: 20 } },
        { id: "", name: "坏", nav: { key: "x", to: "/x", label: "x", short: "x" } },
        { id: "bad", name: "坏", nav: { key: "bad" } },
      ],
    }));
    const { loadSiteConfig, getSiteConfig } = await freshSite();
    await loadSiteConfig();
    expect(getSiteConfig().plugins).toEqual([
      { id: "demo", name: "示例", nav: { key: "demo", to: "/demo", label: "示例", short: "例", order: 20 } },
    ]);
  });

  it("首次请求成功/失败都 settled，深层链接据此决定是否等待", async () => {
    stubFetch(async () => jsonResponse({ name: "某站" }));
    const ok = await freshSite();
    expect(ok.getSiteConfigSettled()).toBe(false);
    await ok.loadSiteConfig();
    expect(ok.getSiteConfigSettled()).toBe(true);

    vi.resetModules();
    stubFetch(async () => { throw new Error("断网"); });
    const fail = await freshSite();
    await fail.loadSiteConfig();
    expect(fail.getSiteConfigSettled()).toBe(true);
    expect(fail.getSiteName()).toBe(DEFAULT);
  });
});

describe("不让站名请求获得重定向的权力", () => {
  it("site.ts 不走 api.ts 的客户端（401 会整页跳 /login）", () => {
    // api.ts 的 request() 对 401 会 clearIdentity + location.assign("/login")。
    // 站名接口是公开的，失败也只是展示降级——但它一旦 import 那套客户端，将来某次
    // cookie 过期就会让"拉站名"变成一次整页跳转。这里钉住它只裸 fetch。
    expect(SITE_SRC).not.toMatch(/^import .*from "\.\/api\.ts"/m);
    expect(SITE_SRC).not.toContain("apiGet(");
    expect(SITE_SRC).not.toContain("handle401");
    expect(SITE_SRC).toContain('fetch("/api/site")');
  });
});
