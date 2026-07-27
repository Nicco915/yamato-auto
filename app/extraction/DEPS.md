# 提取验证线（extraction + validation）额外依赖记录

以下包已在本机用 `pip3 install --user` 安装（注意：本机 Python 3.13 存在 SSL 证书问题，
pip 需要加 `--trusted-host pypi.org --trusted-host files.pythonhosted.org` 才能安装）：

- pydantic 2.13.4        # ExtractedItem 结构化约束
- openai 2.48.0          # OpenAI 兼容客户端（走硅基流动中转）
- python-dotenv 1.2.2    # 读取 app/.env
- xlrd 2.0.2             # 旧版 .xls 读取（pandas engine）
- （依赖链自动带入：httpx, pydantic-core, annotated-types, jiter, distro, sniffio, tqdm, typing-inspection）

已预装、直接使用的包：
- pandas 3.0.3
- openpyxl（pandas 依赖链中已可用，拆分合并单元格用）
- PyMuPDF (fitz) 1.28.0  # PDF 渲染图片

可选（未安装）：
- pyxlsb                 # 仅当遇到 .xlsb 文件时才需要
- LibreOffice (soffice)  # 本机未检测到；.doc/.docx 回退 macOS 自带 textutil（见下）

运行时开关（环境变量）：
- LLM_ENABLE_THINKING    # 默认 "1" 保持现状；"0" 时请求体加 enable_thinking=False
                         # （OpenAI SDK extra_body），reasoning_tokens 归零，响应大幅提速

系统工具（非 pip）：
- textutil               # macOS 自带，.doc/.docx → HTML 兜底通道（doc_channel.py）；
                         # 非 macOS 或转换失败时维持原行为记 unsupported_files
