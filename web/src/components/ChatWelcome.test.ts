// 对话空态：建设账号三张卡（问 / 整理 / 体检），只读账号三张卡（问 / 找 / 划重点）。
import { describe, expect, it } from "vitest";
import {
  WELCOME_ACTIONS,
  READONLY_ACTIONS,
  SUGGESTIONS,
  READONLY_SUGGESTIONS,
  ChatWelcome,
} from "./ChatWelcome.tsx";
import WELCOME from "./ChatWelcome.tsx?raw";
import CHAT from "../pages/Chat.tsx?raw";
import MESSAGES from "./MessageList.tsx?raw";

describe("WELCOME_ACTIONS：正好三张，覆盖知识库三件事", () => {
  it("key 固定为 ask / feed / lint，顺序也固定", () => {
    expect(WELCOME_ACTIONS.map((a) => a.key)).toEqual(["ask", "feed", "lint"]);
    expect(WELCOME_ACTIONS.every((a) => a.title !== "" && a.example !== "")).toBe(true);
  });

  it("SUGGESTIONS 是同一份例子文案（不各抄一遍）", () => {
    expect(SUGGESTIONS).toEqual(WELCOME_ACTIONS.map((a) => a.example));
  });

  it("markup：action-card；ask 只调 onSuggest（不发送），feed/lint 各走各的回调", () => {
    expect(WELCOME).toContain("action-row");
    expect(WELCOME).toContain("action-card");
    expect(WELCOME).toContain("onSuggest(action.example)");
    expect(WELCOME).toContain("onFeed()");
    expect(WELCOME).toContain("onDoctor()");
    expect(WELCOME).not.toMatch(/apiPost|fetch\(|onSend|dispatchEvent/);
  });

  it("极简空态：标题 + 三张卡，没有副标题/新建按钮/机制说明", () => {
    expect(WELCOME).toContain("从知识库开始");
    expect(WELCOME).not.toContain("<p");
    expect(WELCOME).not.toContain("onNew");
    expect(WELCOME).not.toContain("新会话");
    expect(typeof ChatWelcome).toBe("function");
  });
});

describe("两个入口都用它，且三件动作都接上", () => {
  it("Chat 的 no-session 欢迎块与 MessageList 的空消息流都用 ChatWelcome", () => {
    expect(CHAT).toContain("<ChatWelcome");
    expect(CHAT).toContain("onSuggest={suggest}");
    expect(CHAT).toContain("onFeed={feed}");
    expect(CHAT).toContain("onDoctor={doctor}");
    expect(CHAT).toContain("canFeed={canBuild}");
    expect(MESSAGES).toContain("<ChatWelcome");
    expect(MESSAGES).toContain("onFeed={onFeed}");
    expect(MESSAGES).toContain("onDoctor={onDoctor}");
    expect(MESSAGES).toContain("canFeed={canFeed}");
  });

  it("suggest() 只 setDraft：卡片不建会话、不发送", () => {
    expect(CHAT).toMatch(/function suggest\(text: string\) \{[\s\S]{0,220}setDraft\(text\);/);
    expect(CHAT).not.toMatch(/function suggest[\s\S]{0,260}(newSession|runTurn|postMessage|void send\()/);
  });
});

describe("能力感知：canFeed=false 时换成只读动作组", () => {
  it("只读三张卡：问 / 找 / 划重点，不含整理与体检", () => {
    expect(READONLY_ACTIONS.map((a) => a.key)).toEqual(["ask", "find", "digest"]);
    expect(READONLY_ACTIONS.every((a) => a.title !== "" && a.example !== "")).toBe(true);
    expect(READONLY_SUGGESTIONS).toEqual(READONLY_ACTIONS.map((a) => a.example));
    const titles = READONLY_ACTIONS.map((a) => a.title).join(" ");
    expect(titles).not.toContain("整理");
    expect(titles).not.toContain("体检");
    expect(READONLY_ACTIONS.map((a) => a.example).join(" ")).not.toContain("PDF");
  });

  it("卡片整组按 canFeed 切换；两条动作路径各走各的回调", () => {
    expect(WELCOME).toContain("const actions = canFeed ? welcome.build : welcome.readonly;");
    expect(WELCOME).not.toContain('canFeed || action.key !== "feed"');
    expect(WELCOME).toContain('if (action.key === "feed") onFeed();');
    expect(WELCOME).toContain('else if (action.key === "lint") onDoctor();');
    expect(WELCOME).toContain("else onSuggest(action.example);");
  });

  it("props 必填：两个调用点都交同一个 canBuild", () => {
    expect(WELCOME).toContain("canFeed: boolean;");
    expect(CHAT).toContain("const canBuild = canBuildWiki(me);");
    expect(CHAT).toContain("canFeed={canBuild}");
    // MessageList 只透传，不自己猜权限
    expect(MESSAGES).toContain("canFeed: boolean;");
  });
});
