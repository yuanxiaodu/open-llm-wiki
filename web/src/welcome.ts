// Welcome cards shown in the chat empty state.
//
// The defaults are domain-neutral; a deployment can override them in app.toml
// under [ui].build_actions / [ui].readonly_actions. The backend serves the
// resolved values through GET /api/site.
//
// `key` is not decorative: ChatWelcome dispatches on it (`feed` opens the file
// picker, `lint` fills the doctor prompt, everything else prefills the composer).

export type WelcomeActionKey = "ask" | "feed" | "lint" | "find" | "digest";

export interface WelcomeAction {
  key: string;
  icon: string;
  title: string;
  example: string;
}

export interface WelcomeConfig {
  build: WelcomeAction[];
  readonly: WelcomeAction[];
}

export const DEFAULT_BUILD_ACTIONS: readonly WelcomeAction[] = [
  { key: "ask", icon: "🔍", title: "问知识库", example: "这个知识库主要记录什么？" },
  { key: "feed", icon: "📄", title: "整理一份资料", example: "把 PDF 拖进来，自动整理成页" },
  { key: "lint", icon: "🩺", title: "体检知识库", example: "查断链、重复页、过期页" },
] as const;

export const DEFAULT_READONLY_ACTIONS: readonly WelcomeAction[] = [
  { key: "ask", icon: "🔍", title: "问知识库", example: "这个知识库主要记录什么？" },
  { key: "find", icon: "🗂️", title: "找已有内容", example: "知识库里有哪些讲这个主题的页面？" },
  { key: "digest", icon: "📌", title: "划个重点", example: "把这个主题的关键内容梳理一下" },
] as const;

export const DEFAULT_WELCOME: WelcomeConfig = {
  build: [...DEFAULT_BUILD_ACTIONS],
  readonly: [...DEFAULT_READONLY_ACTIONS],
};

function normalizeAction(raw: unknown): WelcomeAction | null {
  if (typeof raw !== "object" || raw === null) return null;
  const item = raw as Record<string, unknown>;
  const key = typeof item.key === "string" ? item.key.trim() : "";
  if (key === "") return null;
  return {
    key,
    icon: typeof item.icon === "string" && item.icon.trim() !== "" ? item.icon.trim() : "•",
    title: typeof item.title === "string" ? item.title.trim() : "",
    example: typeof item.example === "string" ? item.example.trim() : "",
  };
}

function normalizeActions(raw: unknown): WelcomeAction[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const out = raw.map(normalizeAction).filter((a): a is WelcomeAction => a !== null);
  return out.length > 0 ? out : null;
}

/** Merge an untyped /api/site `welcome` payload with the built-in defaults. */
export function normalizeWelcome(raw: unknown): WelcomeConfig {
  if (typeof raw !== "object" || raw === null) return DEFAULT_WELCOME;
  const obj = raw as Record<string, unknown>;
  return {
    build: normalizeActions(obj.build) ?? [...DEFAULT_BUILD_ACTIONS],
    readonly: normalizeActions(obj.readonly) ?? [...DEFAULT_READONLY_ACTIONS],
  };
}
