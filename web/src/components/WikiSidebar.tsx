import { useCallback } from "react";
import { apiGet } from "../api.ts";
import { normalizeSearch, searchPageHref } from "../search.ts";
import { buildWikiTree, pathStem } from "../wiki.ts";
import { useSiteConfig } from "../site.ts";
import type { WikiPageMeta } from "../wiki.ts";
import { useAppShell } from "./AppShell.tsx";
import { SidebarSearch } from "./SidebarSearch.tsx";
import type { SidebarHit } from "./SidebarSearch.tsx";
import { WikiTree } from "./WikiTree.tsx";

// wiki 的侧栏内容：模块内搜索 + 物理 vault 目录树。
//
// 整块外壳（品牌/模块导航/身份/退出/抽屉/焦点）在 AppShell；本组件只负责本模块的
// 搜索与树。窄屏/宽屏的抽屉切换也由 AppShell 统一管（≤900px 收成同一个抽屉）。

export interface WikiSidebarProps {
  /** `GET /api/wiki` 归一化后的页面列表；树由 buildWikiTree 在这里现算（数据驱动）。 */
  pages: WikiPageMeta[];
  /** 当前页的 vault path（空串 = 索引）。 */
  currentPath: string;
}

export function WikiSidebar({
  pages,
  currentPath,
}: WikiSidebarProps) {
  const { closeDrawer } = useAppShell();
  const { type_labels } = useSiteConfig();
  const tree = buildWikiTree(pages, type_labels);
  /**
   * 本模块搜索：全局 /api/search 只取 pages 组（ref_id 是页 path，heading 是标题）。
   * 后端保留了全站搜索接口，但 UI 不再有独立搜索页——wiki 侧栏只搜 wiki。
   */
  const search = useCallback(async (q: string): Promise<SidebarHit[]> => {
    const raw = await apiGet<unknown>(`/api/search?q=${encodeURIComponent(q)}`);
    const hits: SidebarHit[] = [];
    for (const hit of normalizeSearch(raw).pages.slice(0, 20)) {
      const href = searchPageHref(hit);
      if (href === null) continue;
      hits.push({
        key: `p:${hit.ref_id}`,
        title: hit.heading.trim() || pathStem(hit.ref_id),
        snippet: hit.snippet,
        href,
      });
    }
    return hits;
  }, []);

  return (
    <>
      <SidebarSearch
        ariaLabel="搜索 wiki"
        placeholder="搜索 wiki"
        search={search}
        onNavigate={closeDrawer}
      />

      <WikiTree nodes={tree} currentPath={currentPath} onNavigate={closeDrawer} />
    </>
  );
}
