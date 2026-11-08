"""站点信息接口：`GET /api/site`。

公开接口：登录页在人拿到身份之前就要显示站名与界面文案，所以这条路由不能要登录。
响应来自两处，都是**请求时现读**，不做 import 时快照：

- `settings.site_name`：`SITE_NAME` 是部署级覆盖，优先于 `wiki.toml` 的 `site.name`；
- `get_config()`：`wiki.toml` 里的描述、页面类型中文名、欢迎动作。

运营开关（对话可见范围）走 `/api/admin/settings`，不混进这里。注册顺序：这条路由由
`app/routers/api/__init__.py` 汇入聚合 router，早于 `app/main.py` 最后那条 SPA
catch-all，因此 `/api/site` 不会被吞成 index.html。
"""

from __future__ import annotations

from fastapi import APIRouter

from ...config import settings
from ...plugins import plugin_site_payload
from ...wiki_config import get_config

router = APIRouter()


@router.get("/api/site")
def get_site() -> dict[str, object]:
    """公开：不需要登录。返回站名、描述、页面类型中文名与欢迎动作。"""
    payload = get_config().as_site_payload()
    # SITE_NAME 是部署级覆盖（env 优先于 wiki.toml）；归一规则由 config 统一负责。
    payload["name"] = settings.site_name
    payload["plugins"] = plugin_site_payload()
    return payload
