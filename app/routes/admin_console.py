"""
管理控制台页面路由。

页面 HTML 放在独立静态文件 ``app/static/console.html``，本模块只负责把它返回，
保持路由模块简洁（参考常规 Web 框架「路由薄、静态资源独立」的做法）。

- GET /admin/console  返回控制台页面（概览 / 沙箱 / 队列统计 / 资源配置多 Tab）。

页面本身在中间件中豁免 API Key（浏览器直开无法带凭证），但其调用的数据接口仍受
保护：读取需 API Key（若启用），写入另需 Admin Token——均在页面内输入并存本地。
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["Admin Console"])

# app/routes/ -> app/static/console.html
_CONSOLE_HTML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static",
    "console.html",
)


@router.get("/admin/console", include_in_schema=False)
async def admin_console() -> FileResponse:
    """可视化运维 + 资源配置控制台页面（浏览器直接打开）。"""
    if not os.path.isfile(_CONSOLE_HTML_PATH):
        raise HTTPException(status_code=404, detail="控制台页面文件缺失")
    return FileResponse(_CONSOLE_HTML_PATH, media_type="text/html; charset=utf-8")
