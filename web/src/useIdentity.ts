// 身份的外部 store 订阅（React 侧只有这一处）。
//
// 为什么需要它：`api.ts` 的 getIdentity() 是个普通函数，渲染时读一次快照。AuthOnly 在
// **每次整页加载**都用 `/api/me` 刷新身份（管理员可能刚改了这个人的建设权限），只把新值
// 写回 sessionStorage 的话，已经渲染好的 Chat / AppShell / WikiIndex 不会重渲染——
// 界面会停在旧权限上（附件按钮照旧可点、侧栏照旧写着"成员"）。
//
// 所以身份用一个极小的外部 store：写侧（setIdentity / clearIdentity）通知订阅者，
// 读侧用 useSyncExternalStore 订阅。快照引用稳定由 api.ts 里的 cachedIdentity 保证
// （useSyncExternalStore 要求 getSnapshot 返回同一个引用，否则会无限重渲染）。
import { useSyncExternalStore } from "react";
import { getIdentity, subscribeIdentity } from "./api.ts";
import type { Identity } from "./api.ts";

/** 当前身份（可订阅）。登录/刷新身份/401 清身份都会让用它的组件重渲染。 */
export function useIdentity(): Identity | null {
  return useSyncExternalStore(subscribeIdentity, getIdentity, getIdentity);
}
