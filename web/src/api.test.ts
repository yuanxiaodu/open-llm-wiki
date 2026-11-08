// api.ts 里几个纯函数的钉子（裁定 R-S3-6 的两项：兜底文案不带状态码、401 豁免归一尾斜杠；
// 加上 S3 收尾抽出来的 readErrorEnvelope）。
//
// 这几件事都不是"顺手写的"：
//   - 兜底文案带状态码（旧形状「请求失败（502）」）等于把后台的形状甩给填表的人；
//   - 401 豁免不认尾斜杠时，站在 /login/ 上的人会被整页 assign 一次，页面刷两遍；
//   - 错误信封的解法原来有两份（request() 里一份、pages/Chat.tsx 开流失败那条路自己抄了
//     一份），两份迟早漂成"同一个后端错误两种说法"，所以拎成 readErrorEnvelope 钉在这儿。
//   - 身份里的 can_build_wiki（2026-10-30）：旧 sessionStorage 没有这个键，按 false 兜底会
//     把所有人瞬间锁成只读，所以正常化规则单独拎出来钉住。
// request() 本身要 fetch/window，不在这里测（那部分的形状由 pages 上的手工验收钉）；
// readErrorEnvelope 收的是一个现成的 Response，node 环境里就能跑，不用 jsdom。
import { describe, expect, it } from "vitest";

import {
  canBuildWiki,
  fallbackMessage,
  isAuthPath,
  normalizeIdentity,
  readErrorEnvelope,
} from "./api.ts";

describe("fallbackMessage：后端没给人话时才用它，且不暴露状态码", () => {
  it("0（网络层压根没连上）→「连不上服务器」", () => {
    expect(fallbackMessage(0)).toBe("连不上服务器");
  });

  it.each([500, 502, 503, 504, 599])("%i → 「服务器出错了」", (status) => {
    expect(fallbackMessage(status)).toBe("服务器出错了");
  });

  it.each([400, 401, 403, 404, 409, 413, 422, 499])("%i → 「请求没成功」", (status) => {
    expect(fallbackMessage(status)).toBe("请求没成功");
  });

  it("任何一档都不把状态码写进文案", () => {
    for (const status of [0, 400, 404, 418, 500, 502]) {
      expect(fallbackMessage(status)).not.toMatch(/\d/);
    }
  });
});

describe("isAuthPath：401 不跳转的两个地方，尾斜杠归一", () => {
  it("/login 与它带尾斜杠的变体都算", () => {
    expect(isAuthPath("/login")).toBe(true);
    expect(isAuthPath("/login/")).toBe(true);
    expect(isAuthPath("/login///")).toBe(true);
  });

  it("/i/:token 与它的尾斜杠变体都算", () => {
    expect(isAuthPath("/i/abc123")).toBe(true);
    expect(isAuthPath("/i/abc123/")).toBe(true);
    expect(isAuthPath("/i/deep/er/path")).toBe(true);
  });

  it("其余路径不算（该跳登录就跳）", () => {
    // "/i" 与 "/i/" 不是路由（router 只定义 /i/:token），落进 catch-all 会被送去
    // /chat 再由守卫送回 /login——那种情况下"跳转"正是对的行为，所以不豁免。
    for (const p of ["/", "/chat", "/chat/7", "/login-help", "/images/x", "/i", "/i/", "/wiki"]) {
      expect(isAuthPath(p)).toBe(false);
    }
  });
});

describe("readErrorEnvelope：非 2xx 响应体 → 人话 + 其余键（全站唯一一处解法）", () => {
  /** 一个错误响应。content-type 故意不给全：解法只看字节，不看头。 */
  function res(body: BodyInit | null, status: number): Response {
    return new Response(body, { status });
  }

  it("后端给了 {\"error\": 人话} → 用后端那句，其余键进 extra", async () => {
    const got = await readErrorEnvelope(
      res(JSON.stringify({ error: "智能体还在写，等它说完", diff: { slug: "a" } }), 409),
    );
    expect(got).toEqual({ message: "智能体还在写，等它说完", extra: { diff: { slug: "a" } } });
  });

  it("没有其余键时 extra 是空对象（不是 undefined，形状得稳）", async () => {
    expect(await readErrorEnvelope(res('{"error": "没有这个会话"}', 404))).toEqual({
      message: "没有这个会话",
      extra: {},
    });
  });

  it.each(['{"error": ""}', '{"error": 42}', '{"detail": "Not Found"}', "{}", "[]"])(
    "解不出 error（%s）→ 退回兜底文案，且文案里不带状态码",
    async (body) => {
      const got = await readErrorEnvelope(res(body, 404));
      expect(got.message).toBe("请求没成功");
      expect(got.message).not.toMatch(/\d/);
    },
  );

  it("响应体不是 JSON（网关直接回了 HTML）→ 兜底文案，不抛", async () => {
    const got = await readErrorEnvelope(res("<html>502 Bad Gateway</html>", 502));
    expect(got).toEqual({ message: "服务器出错了", extra: {} });
  });

  it("空响应体也不抛（5xx 常常是空的）", async () => {
    expect((await readErrorEnvelope(res(null, 500))).message).toBe("服务器出错了");
    expect((await readErrorEnvelope(res("", 503))).message).toBe("服务器出错了");
  });

  it("与 fallbackMessage 同一套兜底：5xx 一句、其余一句", async () => {
    for (const status of [400, 401, 403, 404, 409, 413, 422]) {
      expect((await readErrorEnvelope(res(null, status))).message, String(status)).toBe(
        fallbackMessage(status),
      );
    }
  });
});

describe("normalizeIdentity / canBuildWiki：旧数据不把所有人锁成只读", () => {
  it("新形状原样保留 can_build_wiki", () => {
    expect(normalizeIdentity({ id: 1, name: "张三", role: "member", can_build_wiki: false })).toEqual(
      { id: 1, name: "张三", role: "member", can_build_wiki: false },
    );
    expect(normalizeIdentity({ id: 1, name: "张三", role: "admin", can_build_wiki: true })).toEqual(
      { id: 1, name: "张三", role: "admin", can_build_wiki: true },
    );
  });

  it("旧数据缺 can_build_wiki → true（迁移前人人能建设）", () => {
    expect(normalizeIdentity({ id: 2, name: "李四", role: "member" })?.can_build_wiki).toBe(true);
    expect(normalizeIdentity({ id: 2, name: "李四", role: "member", can_build_wiki: null })).toEqual({
      id: 2,
      name: "李四",
      role: "member",
      can_build_wiki: true,
    });
  });

  it("手写/旧序列化给 0/1 也认", () => {
    expect(normalizeIdentity({ id: 3, name: "王五", role: "member", can_build_wiki: 0 })?.can_build_wiki).toBe(false);
    expect(normalizeIdentity({ id: 3, name: "王五", role: "member", can_build_wiki: 1 })?.can_build_wiki).toBe(true);
  });

  it("坏形状 → null（当没身份，别把页面炸了）", () => {
    expect(normalizeIdentity(null)).toBeNull();
    expect(normalizeIdentity("x")).toBeNull();
    expect(normalizeIdentity({ id: "1", name: "张三", role: "member" })).toBeNull();
    expect(normalizeIdentity({ id: 1, role: "member" })).toBeNull();
    expect(normalizeIdentity({ id: 1, name: "张三", role: 3 })).toBeNull();
  });

  it("canBuildWiki：管理员恒 true；其余看字段；没身份 false", () => {
    expect(canBuildWiki(null)).toBe(false);
    expect(canBuildWiki({ id: 1, name: "管理员", role: "admin", can_build_wiki: false })).toBe(true);
    expect(canBuildWiki({ id: 2, name: "成员", role: "member", can_build_wiki: true })).toBe(true);
    expect(canBuildWiki({ id: 2, name: "成员", role: "member", can_build_wiki: false })).toBe(false);
  });
});
