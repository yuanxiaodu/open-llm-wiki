// 站点配置的外部 store：站名、描述、页面类型中文名、欢迎动作。
//
// 数据来自公开的 `GET /api/site`。用模块级快照 + useSyncExternalStore（与
// useIdentity.ts 同一套写法）：App 整页加载时拉一次，登录页、侧栏、wiki 页、
// 对话空态共用这一份，谁读谁重渲染。
//
// 失败必须静默：配置是展示信息，取不到就用内置默认值；绝不能因为配置没拉到挡住
// 登录/对话，也不能借用 api.ts 的 401 跳转语义。只有 name 合法才算成功，失败不置位，
// 下次挂载还能重试。
import { useSyncExternalStore } from "react";

import {
  DEFAULT_WELCOME,
  normalizeWelcome,
  type WelcomeConfig,
} from "./welcome.ts";

export interface PluginNav {
  id: string;
  name: string;
  nav: {
    key: string;
    to: string;
    label: string;
    short: string;
    order?: number;
  };
}

export interface SiteConfig {
  name: string;
  description: string;
  type_labels: Record<string, string>;
  welcome: WelcomeConfig;
  plugins: PluginNav[];
}

/** 与 app/wiki_config.py 的 DEFAULT_SITE_NAME 同值（前端拿不到 Python 常量）。 */
export const DEFAULT_SITE_NAME = "知识库";

export const DEFAULT_TYPE_LABELS: Record<string, string> = {
  entity: "实体",
  concept: "概念",
  comparison: "对比",
  query: "查询",
};

export const DEFAULT_SITE_CONFIG: SiteConfig = {
  name: DEFAULT_SITE_NAME,
  description: "",
  type_labels: { ...DEFAULT_TYPE_LABELS },
  welcome: DEFAULT_WELCOME,
  plugins: [],
};

const TYPE_KEYS = Object.keys(DEFAULT_TYPE_LABELS);

let siteConfig: SiteConfig = DEFAULT_SITE_CONFIG;
/** 成功拉过一次就不再打接口；失败不置位，下次挂载还能重试。 */
let loaded = false;
/** 首次请求已经结束（成功或失败）。深层链接的 catch-all 用它决定“再等一会”。 */
let settled = false;
/** 同一时刻只有一个请求：App 的 effect 在 StrictMode 下会跑两次。 */
let inFlight: Promise<void> | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

function normalizePlugins(raw: unknown): PluginNav[] {
  if (!Array.isArray(raw)) return [];
  const out: PluginNav[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) continue;
    const obj = item as Record<string, unknown>;
    const id = typeof obj.id === "string" ? obj.id.trim() : "";
    const name = typeof obj.name === "string" ? obj.name.trim() : "";
    const navRaw = obj.nav;
    if (id === "" || typeof navRaw !== "object" || navRaw === null) continue;
    const nav = navRaw as Record<string, unknown>;
    const key = typeof nav.key === "string" ? nav.key.trim() : "";
    const to = typeof nav.to === "string" ? nav.to.trim() : "";
    const label = typeof nav.label === "string" ? nav.label.trim() : "";
    const short = typeof nav.short === "string" ? nav.short.trim() : "";
    if (key === "" || to === "" || label === "" || short === "") continue;
    const order = typeof nav.order === "number" && Number.isFinite(nav.order) ? nav.order : undefined;
    out.push({ id, name: name || label, nav: { key, to, label, short, ...(order === undefined ? {} : { order }) } });
  }
  return out;
}

function normalizeTypeLabels(raw: unknown): Record<string, string> {
  const out: Record<string, string> = { ...DEFAULT_TYPE_LABELS };
  if (typeof raw !== "object" || raw === null) return out;
  for (const key of TYPE_KEYS) {
    const value = (raw as Record<string, unknown>)[key];
    if (typeof value === "string" && value.trim() !== "") out[key] = value.trim();
  }
  return out;
}

/** 当前配置快照。对象只在加载成功时整体替换，引用稳定，适合 useSyncExternalStore。 */
export function getSiteConfig(): SiteConfig {
  return siteConfig;
}

/** 当前站名快照（大部分组件只需要这个）。 */
export function getSiteName(): string {
  return siteConfig.name;
}

export function subscribeSiteConfig(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 兼容旧调用点：订阅站点配置（与 subscribeSiteConfig 同一份订阅）。 */
export const subscribeSiteName = subscribeSiteConfig;

/** 订阅完整站点配置。 */
export function useSiteConfig(): SiteConfig {
  return useSyncExternalStore(subscribeSiteConfig, getSiteConfig, getSiteConfig);
}

/** 订阅当前站名。 */
export function useSiteName(): string {
  return useSyncExternalStore(subscribeSiteConfig, getSiteName, getSiteName);
}

export function getSiteConfigSettled(): boolean {
  return settled;
}

/** 首次 /api/site 请求是否已结束（成功或失败）。 */
export function useSiteConfigSettled(): boolean {
  return useSyncExternalStore(subscribeSiteConfig, getSiteConfigSettled, getSiteConfigSettled);
}

function normalizePayload(data: unknown): SiteConfig | null {
  if (typeof data !== "object" || data === null) return null;
  const obj = data as Record<string, unknown>;
  const name = typeof obj.name === "string" ? obj.name.trim() : "";
  if (name === "") return null;
  return {
    name,
    description: typeof obj.description === "string" ? obj.description.trim() : "",
    type_labels: normalizeTypeLabels(obj.type_labels),
    welcome: normalizeWelcome(obj.welcome),
    plugins: normalizePlugins(obj.plugins),
  };
}

/**
 * 拉一次 `GET /api/site`：成功则更新快照 + `document.title`，失败静默。
 *
 * 不抛异常是契约的一部分（App 用 `void loadSiteConfig()` 触发，没人 catch）：断网、
 * 非 2xx、响应体不是 JSON、name 缺失/不是字符串/全是空白，一律继续用默认值。
 */
export async function loadSiteConfig(): Promise<void> {
  if (loaded) return;
  if (inFlight !== null) return inFlight;
  inFlight = (async () => {
    try {
      const res = await fetch("/api/site");
      if (!res.ok) return;
      const data: unknown = await res.json();
      const next = normalizePayload(data);
      if (next === null) return;
      siteConfig = next;
      loaded = true;
      if (typeof document !== "undefined") {
        // 生产环境服务端已经按 SITE_NAME 注入过 <title>；这里对齐 dev（vite 直接服务
        // index.html）与站名在页面加载后变更的场景。
        document.title = next.name;
      }
    } catch {
      // 网络层失败 / 响应体不是 JSON：配置只是展示信息，静默用默认值。
    } finally {
      inFlight = null;
      settled = true;
      notify();
    }
  })();
  return inFlight;
}

/** 兼容旧调用点：站名与配置一起加载。 */
export const loadSiteName = loadSiteConfig;
