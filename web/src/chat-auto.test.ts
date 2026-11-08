// task-21 的源码钉子：首页输入可用（自动建会话发消息/上传）、上传后尾随自动摄入流、
// 删除摄入模块、项目改名与极简文案。行为部分靠 CDP 实测（报告里有截图/请求计数），
// 这里钉"线路有没有接对"。
//
// task-1 起「站名」不再是源码常量，而是部署期配置 SITE_NAME（后端 /api/site →
// web/src/site.ts）。这个文件只钉"三处展示位没有写死站名、都读 useSiteName()"；
// 静态 title 仍留在 index.html 里当 dev/无 JS 兜底。loadSiteName 的成功/失败由
// site.test.ts 单独钉。
import { describe, expect, it } from "vitest";

import CHAT from "./pages/Chat.tsx?raw";
import COMPOSER from "./components/Composer.tsx?raw";
import WELCOME from "./components/ChatWelcome.tsx?raw";
import APP from "./App.tsx?raw";
import APPSHELL from "./components/AppShell.tsx?raw";
import SESSION_LIST from "./components/SessionList.tsx?raw";
import LOGIN from "./pages/Login.tsx?raw";
import REGISTER from "./pages/Register.tsx?raw";
import INDEX from "../index.html?raw";

const OLD_NAME = ["众", "筹", "共", "写"].join("");
const NEW_NAME = "测试站名";

describe("首页输入直接可用：没有会话时自动建会话", () => {
  it("发送：排队 pendingAction，建会话后等详情落地再执行", () => {
    expect(CHAT).toContain("pendingAction");
    expect(CHAT).toContain('createSessionFor({ kind: "send", text, uploadIds, staged })');
    expect(CHAT).toMatch(/useEffect\(\(\) => \{[\s\S]{0,500}detail\.id !== sid/);
    expect(CHAT).toContain("void sendTo(sid, action.text, action.uploadIds, action.staged);");
  });

  it("发送时用确定的新 sid（targetSid），并把 upload_ids 一起 POST", () => {
    expect(CHAT).toContain("async function sendTo(");
    expect(CHAT).toContain("uploadIds: number[]");
    expect(CHAT).toContain("await postMessage(targetSid, text, uploadIds)");
    expect(CHAT).toContain("await runTurn(targetSid, posted.id, text);");
  });

  it("Composer 只有归档才 disable；没有会话不再是禁用条件", () => {
    expect(CHAT).toContain("disabled={archived}");
    expect(CHAT).not.toMatch(/disabled=\{sid === null \|\| detail === null/);
  });

  it("建议 chip 只预填：suggest 里没有建会话/发送", () => {
    expect(CHAT).toMatch(/function suggest\(text: string\) \{[\s\S]{0,160}setDraft\(text\);/);
    expect(CHAT).not.toMatch(/function suggest[\s\S]{0,220}(newSession|runTurn|postMessage)/);
    expect(WELCOME).not.toContain("onNew");
  });
});

describe("附件两段式：选文件只暂存，发送才随消息提交", () => {
  it("选文件只 POST /api/uploads：不建会话、不发消息、不起流", () => {
    expect(CHAT).toMatch(/async function stageFiles\(files: File\[\]\)/);
    const stagePart = CHAT.slice(
      CHAT.indexOf("async function stageFiles"),
      CHAT.indexOf("async function removeStaged"),
    );
    expect(stagePart).toContain('apiPostForm<unknown>("/api/uploads", form)');
    expect(stagePart).not.toContain("createSessionFor");
    expect(stagePart).not.toContain("postMessage(");
    expect(stagePart).not.toContain("runTurn(");
    expect(stagePart).not.toContain("/api/chat/");
  });

  it("staged 用 upload_ids 随消息提交；空文字+附件允许", () => {
    expect(CHAT).toContain("canSendMessage(text, staged)");
    expect(CHAT).toContain("messageForm(content, uploadIds)");
    expect(CHAT).toContain("const uploadIds = staged.map((s) => s.id);");
  });

  it("发送成功才清草稿/暂存；失败保留（清空在 POST 成功之后）", () => {
    const postAt = CHAT.indexOf("await postMessage(targetSid, text, uploadIds)");
    expect(postAt).toBeGreaterThan(0);
    const clearDraftAt = CHAT.indexOf('setDraft("")', postAt);
    const clearStagedAt = CHAT.indexOf("setStaged((prev) => prev.filter", postAt);
    expect(clearDraftAt).toBeGreaterThan(postAt);
    expect(clearStagedAt).toBeGreaterThan(postAt);
    // catch 里只报人话，不清理
    const catchPart = CHAT.slice(CHAT.indexOf("} catch (e) {", postAt), CHAT.indexOf("} finally", postAt));
    expect(catchPart).not.toContain("setStaged");
    expect(catchPart).not.toContain('setDraft("")');
  });

  it("删除暂存调 DELETE /api/uploads/{id}，成功才从列表移除", () => {
    expect(CHAT).toContain("apiDelete<{ ok?: boolean }>(`/api/uploads/${id}`)");
    expect(CHAT).toContain("withoutStaged(prev, id)");
    expect(CHAT).toContain("errText(e, \"附件没删掉\")");
  });

  it("旧的 auto_ingest / uploadTo 尾随逻辑已经移除", () => {
    expect(CHAT).not.toContain("auto_ingest");
    expect(CHAT).not.toContain("autoIngest");
    expect(CHAT).not.toContain("async function uploadTo");
    expect(CHAT).not.toContain("/api/chat/${targetSid}/upload");
  });
});

describe("删除摄入模块：导航/路由/页面引用清干净", () => {
  it("App/顶栏/对话侧栏都没有 /ingest 或「摄入」入口", () => {
    for (const source of [APP, APPSHELL, SESSION_LIST]) {
      expect(source).not.toContain("/ingest");
      expect(source).not.toContain("摄入");
    }
    expect(APP).not.toContain("IngestPage");
  });

  it("侧栏核心模块是 对话/wiki/日志（+ 管理员管理）；插件由 /api/site 追加", () => {
    for (const label of ["对话", "wiki", "日志", "管理"]) {
      expect(APPSHELL).toContain(`label: "${label}"`);
    }
    expect((APPSHELL.match(/key: "/g) ?? []).length).toBe(4);
  });
});

describe("项目名与极简文案", () => {
  it("站名不写死：顶栏、登录页、对话栏 fallback 都读 useSiteName()", () => {
    // 三处展示位必须从同一个 store 读（AppShell 品牌字 / Login 的 h1 / Chat 的大标题
    // fallback）。任何一处重新写死中文站名，换 SITE_NAME 时就会有三套说法。
    for (const source of [APPSHELL, LOGIN, CHAT]) {
      expect(source).toContain("useSiteName()");
      expect(source).not.toContain(NEW_NAME);
    }
    for (const source of [INDEX, APPSHELL, LOGIN, CHAT]) {
      expect(source).not.toContain(OLD_NAME);
    }
  });

  it("index.html 的静态 title 留作 dev/无 JS 兜底", () => {
    // 生产环境的 / 由 app/main.py 读 index.html 后把 <title> 换成 SITE_NAME；这里的
    // 静态值只在 vite dev（没有服务端注入）与 JS 没跑起来时露脸，所以不改成占位符。
    expect(INDEX).toContain("<title>Open LLM Wiki</title>");
  });

  it("注册成功也落首页", () => {
    expect(REGISTER).toMatch(/navigate\("\/", \{ replace: true \}\)/);
  });

  it("极简：欢迎没有副标题/新建按钮；输入区只有一句短提示", () => {
    expect(WELCOME).not.toContain("<p");
    expect(WELCOME).not.toContain("新会话");
    // 受限账号的 placeholder 换了一句（不再承诺"拖进来"），有权限时仍是原来那句
    expect(COMPOSER).toContain('const PLACEHOLDER_UPLOAD = "问一句，或把资料拖进来";');
    // 不再常驻机制说明行：话由空态动作卡与附件胶囊自己说。
    expect(COMPOSER).not.toContain('className="types"');
    expect(COMPOSER).not.toContain("智能体写的改动会直接生效");
  });
});
