import { Fragment, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { WikiTreeNode } from "../wiki.ts";

// 通用目录树（Obsidian 那种可折叠目录）：wiki 的物理 vault 与书的部/章/节共用。
//
// 组件只管**怎么画、怎么折叠**，不管"树该长什么样"——那是 wiki.ts 的 buildWikiTree
// 与数据源的事。节点自带 `href`，所以同一个组件能画 /wiki/... 的入口。
//
// 交互口径：
//   - 文件夹 = <button aria-expanded>，原生支持 Enter/Space 切换，不手写 keydown；
//   - 文件 = <Link>，当前页挂 aria-current="page"（按 path 与当前 path 比）；
//   - 默认展开：collapsed 集合初始为空 = 所有文件夹都开着。异步拿到目录之后新出现的
//     文件夹也自动是开的（集合里没有它的 key）。
//   - 折叠状态按**位置路径**记（"父/序号:标题"），不是按 label：两页同名也不会串。
//
// 整棵树包在 <nav aria-label="…"> 里（默认「wiki 结构」；文档传「文档目录」）。

export interface WikiTreeProps {
  nodes: WikiTreeNode[];
  /** 当前页 path（空串 = 索引/根）；文件节点据此挂 aria-current。 */
  currentPath: string;
  /** 点了任意文件节点之后调（窄屏抽屉要借此关掉）。 */
  onNavigate?: () => void;
  /** nav 的无障碍名字（默认「wiki 结构」）。 */
  label?: string;
}

/** 文件节点 → 路由地址。给了 href 用它；否则回退到索引。 */
export function wikiNodeHref(node: Pick<WikiTreeNode, "href">): string {
  return node.href !== undefined && node.href !== "" ? node.href : "/wiki";
}

function samePath(a: string, b: string): boolean {
  const norm = (p: string) =>
    p.replace(/\\/g, "/").replace(/\.md$/i, "").replace(/^\/+|\/+$/g, "").toLowerCase();
  return norm(a) === norm(b);
}

function renderLevel(
  nodes: WikiTreeNode[],
  parentPath: string,
  collapsed: ReadonlySet<string>,
  currentPath: string,
  onNavigate: (() => void) | undefined,
  toggle: (key: string) => void,
): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${parentPath}/${index}:${node.label}`;
    // 分隔线必须是 <ul> 的直接子 <li>，不能塞进条目自己的 <li> 里（嵌套 li 非法）。
    // Fragment 在 DOM 里不产生节点，于是两个 <li> 是平级兄弟。
    const separator = node.dividerBefore ? (
      <li className="vtree-sep" role="presentation" aria-hidden="true" />
    ) : null;

    if (node.kind === "folder") {
      const open = !collapsed.has(key);
      const children = node.children ?? [];
      return (
        <Fragment key={key}>
          {separator}
          <li className="vtree-li">
            <button
              type="button"
              className="vtree-row vtree-folder"
              aria-expanded={open}
              onClick={() => toggle(key)}
            >
              <span className="vtree-chev" aria-hidden="true">
                {open ? "▾" : "▸"}
              </span>
              <span className="vtree-label">{node.label}</span>
              {typeof node.count === "number" && node.count > 0 && (
                <span className="vtree-count" aria-hidden="true">
                  {node.count}
                </span>
              )}
            </button>
            {open && children.length > 0 && (
              <ul className="vtree">
                {renderLevel(children, key, collapsed, currentPath, onNavigate, toggle)}
              </ul>
            )}
          </li>
        </Fragment>
      );
    }

    const current = node.path !== undefined && samePath(node.path, currentPath);
    return (
      <Fragment key={key}>
        {separator}
        <li className="vtree-li">
          <Link
            to={wikiNodeHref(node)}
            className="vtree-row vtree-file"
            aria-current={current ? "page" : undefined}
            onClick={onNavigate}
          >
            <span className="vtree-dot" aria-hidden="true" />
            <span className="vtree-label">{node.label}</span>
          </Link>
        </li>
      </Fragment>
    );
  });
}

export function WikiTree({ nodes, currentPath, onNavigate, label = "wiki 结构" }: WikiTreeProps) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set<string>());

  function toggle(key: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <nav className="vtree-nav" aria-label={label}>
      <ul className="vtree">
        {renderLevel(nodes, "", collapsed, currentPath, onNavigate, toggle)}
      </ul>
    </nav>
  );
}
