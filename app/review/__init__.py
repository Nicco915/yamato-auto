"""人工双屏审核界面模块（Node5 前端 + 单据查看 API）。

文件说明：
- router.py       FastAPI 路由：payload 查询 / 原始单据查看（PDF→PNG、Excel→HTML、图片）/ 单页
- static/review.html  原生 JS 双屏单页（左屏单据影像，右屏审核工作区）
- demo_server.py  独立演示服务（mock payload，端口 8001，不依赖真实 LangGraph）

集成方式见《agent设计/人工审核界面设计.md》第 6 节。
"""
