# 插件

插件把可选功能放在 `plugins/<id>/`，不进入核心代码。核心只提供发现、生命周期、路由/工具挂载和前端路由/nav 契约。

## 目录约定

```
plugins/
  my_plugin/
    __init__.py          # module-level plugin 对象
    backend/
      __init__.py
      router.py
      store.py
    web/
      index.tsx          # default export { id, routes }
      my_plugin.css
```

`plugins/` 是仓库根的 Python 包；应用从仓库根启动（`uvicorn app.main:app`、`python -m app.cli ...`、`pytest`），所以 `import plugins.<id>` 可用。

## 启用

`wiki.toml`：

```toml
[plugins]
enabled = ["my_plugin"]

[plugins.my_plugin]
# 任意键值，原样传给 plugin.configure()
data_dir = "./data/my-plugin"
```

默认 `enabled = []`，本仓库不附带任何功能插件。

## 后端接口

`plugins/my_plugin/__init__.py`：

```python
from __future__ import annotations
from typing import Any
from fastapi import APIRouter

class MyPlugin:
    id = "my_plugin"          # 必须等于包名
    name = "示例插件"
    nav = {
        "key": "my_plugin",
        "to": "/my-plugin",
        "label": "示例",
        "short": "例",
        "order": 20,          # 可选；越小越靠前
    }

    def configure(self, options: dict[str, Any]) -> None:
        self.options = options

    def routers(self) -> list[APIRouter]:
        return [my_router]

    def tools(self) -> list:
        return []              # 可选：agent 工具

    def startup(self, conn) -> None: ...
    def shutdown(self) -> None: ...

plugin = MyPlugin()
```

- `id` 不等于包名、`name`/`nav` 缺失、import 失败 → 启动失败，不静默跳过。
- `routers()` 返回的 router 由核心 `include_router(..., include_in_schema=False)` 挂到 `/api` 下。
- `tools()` 返回的 agent 工具会并入 `WIKI_TOOLS`；工具名重复 → 启动失败。工具还必须在 `app/agent/skill/llm-wiki.md` 的 `tools:` 清单里声明。
- `configure(options)` 接收 `[plugins.<id>]` 表。
- `startup(conn)` 在 DB 迁移、vault 初始化之后调用；`shutdown()` 在进程退出时调用。

### 为什么插件路由不进 OpenAPI

插件路由默认 `include_in_schema=False`。这样启用插件的 fork 不需要提交与上游不同的 `web/openapi.json` / `api-types.gen.ts`，升级 upstream 时不会在生成文件上反复冲突。插件自己的前端类型自行维护（或由插件脚本生成）。

## 前端接口

`plugins/my_plugin/web/index.tsx`：

```tsx
import type { FrontendPlugin, PluginRoute } from "@app/plugin-types.ts";
import { MyPage } from "./MyPage.tsx";
import { MyShell } from "./MyShell.tsx";
import "./my_plugin.css";

const routes: PluginRoute[] = [
  {
    path: "my-plugin",
    element: <MyShell />,
    children: [
      { index: true, element: <MyPage /> },
      { path: "*", element: <MyPage /> },
    ],
  },
];

const plugin: FrontendPlugin = { id: "my_plugin", routes };
export default plugin;
```

- `web/src/plugins.ts` 用 Vite `import.meta.glob("../../plugins/*/web/index.tsx", { eager: true })` 自动发现，不需要改核心注册表。
- `@app/*` 别名指向 `web/src/*`，插件可以引用核心组件（`@app/components/AppShell.tsx`、`@app/api.ts` 等）。
- 插件 CSS 由插件入口自己 import。
- `App.tsx` 只挂载 `/api/site` 里启用的插件 id 对应的路由；`AppShell` 只显示这些插件的 nav。
- 插件路由默认放在登录守卫内（需要身份）。如果要做公开页面，目前需要改核心或另开接口，先不要假设。

### 构建期限制

前端插件是构建期发现：禁用插件后，它的代码仍可能被打进 bundle，但路由和导航不会挂载。真正的运行时动态加载不在 v0.1 范围内。

## 测试

后端：

```python
# tests/test_my_plugin.py
from app.plugins import PluginInfo

def test_plugin_nav():
    info = PluginInfo(...)
    assert info.site_payload()["nav"]["to"] == "/my-plugin"
```

核心的插件契约测试在 `tests/test_plugins.py`（合成插件，不需要真实功能插件）。fork 可以把自己的插件测试放在 `plugins/<id>/tests/` 或 `tests/` 下；pytest 会递归收集 `test_*.py`。

前端：`plugins/<id>/web/*.test.ts` 会被 `web/vite.config.ts` 的 `test.include` 自动收集。

## 调试

```bash
python -m app.cli plugins --json
python -m app.cli config --check
```

`/api/site` 会返回已启用插件的 `id`/`name`/`nav`，前端据此挂载路由与导航。
