"""UI 包：工作台/批次详情/对话页面路由 + UI 专用 API（与 review demo/backend 语义分离）。

路由极薄，业务逻辑全部在 app.api.service；页面静态文件在 ui/static/
（由前端线并行开发，缺失时页面路由返回 503，不影响 API）。
"""
from app.ui.router import router

__all__ = ["router"]
