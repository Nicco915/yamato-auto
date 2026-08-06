# -*- coding: utf-8 -*-
"""分票规则引擎——纯 Python 计算，零 LLM、零 DB 依赖。

把 filled ContentsOfTheContainer 中的柜号按商检工厂归属拆分为票（Ticket），
供下游人工审核（LangGraph interrupt）。
"""

__version__ = "0.1.0"