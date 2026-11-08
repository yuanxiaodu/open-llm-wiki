"""JSON 接口都挂在这个包下，路径一律以 /api 开头（/healthz 例外）。

Task 9 之后这是全站唯一的一组路由：旧 HTML 面（app/routers/{public,chat,wiki,admin}.py
与 app/render.py、app/templates/）已删除，前端在 S3 接入。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import admin, auth, chat, ingest, members, search, site, uploads, wiki
from ...plugins import get_plugins

router = APIRouter()
router.include_router(auth.router)
# 站点信息（任务 task-1）：GET /api/site 公开吐站名，登录页在拿到身份之前也要显示它。
# 必须在这里汇入（而不是 app/main.py 里单独加），否则会注册在最后那条 SPA catch-all
# 之后，/api/site 永远落不到——Starlette 按注册顺序匹配。
router.include_router(site.router)
router.include_router(members.router)
# 管理员后台的站点设置（2026-10-30）：对话可见范围等运营开关。
router.include_router(admin.router)
# 裁定 R52：新路由在本任务内就接进应用，不等 T9 的完整装配。app/main.py 已经
# include 了这个聚合 router，所以在这里加一行就等于挂上了 main 里的 app；
# T9 重写 main.py 时不需要再记着补一条 include。
router.include_router(chat.router)
router.include_router(wiki.router)
# S5b 多来源摄入：网页链接 → 材料（POST /api/ingest/url）。
router.include_router(ingest.router)
router.include_router(uploads.router)
router.include_router(search.router)

# 插件路由：在核心路由之后、SPA catch-all 之前挂上。
# include_in_schema=False 是有意的：插件路由不属于核心 OpenAPI 契约，这样 fork
# 启用插件后不需要重新生成/提交不同的 web/openapi.json，升级 upstream 时不会在
# 生成文件上反复冲突。插件自己的前端类型是手写的（或由插件自行生成）。
for _plugin in get_plugins():
    for _plugin_router in _plugin.routers():
        router.include_router(_plugin_router, include_in_schema=False)
