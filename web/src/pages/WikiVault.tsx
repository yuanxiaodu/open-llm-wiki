import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { ApiError, apiGet, errText } from "../api.ts";
import { AppShell } from "../components/AppShell.tsx";
import { WikiSidebar } from "../components/WikiSidebar.tsx";
import { normalizeWikiList, wikiUrlPathFromPathname } from "../wiki.ts";
import type { WikiPageMeta } from "../wiki.ts";

// wiki 的外壳：AppShell 的 260px 侧栏里放搜索+物理目录树，右侧内容由子路由渲染。
//
// 路由（App.tsx 注册，登录守卫在外层）：
//   /wiki             索引（GET /api/wiki/index 的 body_md）
//   /wiki/{path...}   页面（path 不带 .md）
//
// 为什么是"布局路由 + <Outlet>"：/wiki → /wiki/entities/foo 时侧栏不能整个重建
// （树里展开/折叠的状态、已拉到的目录都该留在原地），所以目录数据住在这层，
// 子路由只负责"右侧画什么"。抽屉/焦点/退出统一由 AppShell 管。
//
// 数据形状由 wiki.ts 的 normalizeWikiList 归一化：后端契约是 `{pages,count}`，
// 切换期间旧 S4b 的平铺数组也能用（映射到物理 path），不会白屏。
//
// 这一层**只读**：没有任何编辑/保存/回滚/删除入口，写只由智能体做。

/** 子路由拿到的上下文（useOutletContext）。 */
export interface WikiOutlet {
  pages: WikiPageMeta[];
  /** 页面总数（后端 count；缺了按数组长度）。 */
  count: number;
  loading: boolean;
  /** 目录没取到时的人话（空串 = 没出错）。 */
  error: string;
  /** 当前 URL 对应的 vault path（空串 = 索引）；树用它挂 aria-current。 */
  currentPath: string;
  reload: () => void;
}

export function WikiVault() {
  const location = useLocation();

  const [pages, setPages] = useState<WikiPageMeta[]>([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  /** 拉目录的作废计数：重试/卸载之后，迟到的响应不许再落地。 */
  const listSeq = useRef(0);

  const loadPages = useCallback(async () => {
    const my = (listSeq.current += 1);
    setLoading(true);
    setError("");
    try {
      const data = await apiGet<unknown>("/api/wiki");
      if (listSeq.current !== my) return;
      const list = normalizeWikiList(data);
      setPages(list.pages);
      setCount(list.count);
    } catch (e) {
      if (listSeq.current !== my) return;
      // 401 由 apiGet 统一整页跳登录，这儿不再摆横幅
      if (e instanceof ApiError && e.status === 401) return;
      setError(errText(e, "目录没取到"));
    } finally {
      if (listSeq.current === my) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPages();
    return () => {
      listSeq.current += 1; // 卸载/重跑之后，迟到的目录响应作废
    };
  }, [loadPages]);

  const currentPath = wikiUrlPathFromPathname(location.pathname);

  return (
    <AppShell
      mainClassName="vault-main vault"
      sidebar={<WikiSidebar pages={pages} currentPath={currentPath} />}
    >
      <div className="vault-body">
        <Outlet
          context={
            {
              pages,
              count,
              loading,
              error,
              currentPath,
              reload: () => void loadPages(),
            } satisfies WikiOutlet
          }
        />
      </div>
    </AppShell>
  );
}
