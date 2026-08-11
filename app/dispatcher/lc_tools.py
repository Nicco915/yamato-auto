"""调度 Agent 工具注册表的 LangChain 包装层（create_react_agent 迁移用）。

设计要点：

1. 闭包工厂、每次请求重建：build_tools(session, session_id, phase,
   on_progress) 每次请求现建一套 LangChain 工具，session / on_progress /
   session_id 全部绑定进闭包——session_id 绝不出现在工具签名里，LLM 无法
   伪造或越权操作别的会话。

2. 铁律不变：LLM 只解析不做决策。
   - 只读工具（risk="read"）包装成 StructuredTool 直接执行，结果 JSON
     截断回喂；
   - 写工具（risk="write"）生成**影子工具**：与注册表同名、同参数 schema，
     但绝不执行——只调 preview 生成预览、把 action 信封存进
     session.pending_action（服务端持有，防 LLM/前端在确认间隙篡改），
     然后硬停本轮 react 循环，等 execute_confirmed 走人工确认通道。
     一次一确认：pending_action 非空时拒绝再发起（代码保险，与
     loop.py 语义一致）；
   - preview 返回 blocked=True（业务硬校验失败）时不让 LLM 圆场，直接转
     clarify 文案终止循环。

   硬停机制（spike 实证，勿改回 goto=END）：langgraph 1.2.9 中
   Command(goto=END) 是 no-op——create_react_agent 的 tools→agent
   静态边恒触发，END 不参与分支路由（state.py _control_branch）。
   唯一可用的硬停是 StructuredTool(return_direct=True) + 工具返回
   Command(update={"messages": [ToolMessage(确认文案)]})：update 负责
   回写消息，return_direct 负责改挂 route_tool_responses 条件边终止图。
   确认文案必须放在 ToolMessage.content（末尾再注入 AIMessage 会让
   route_tool_responses 倒序扫描撞见 AIMessage 而 break，硬停失效）；
   因此末条消息就是这条 ToolMessage，引擎层直接取其 content 作为
   用户可见文本。

3. request_clarification（仅 phase>=2）：黄灯反问重建——模型推测用户想
   执行写操作但不确定时调用，代码生成确认式反问文案、存 soft_pending
   软挂起（字段名 {"target_tool","slots","question","armed"} 与
   sessions.set_soft_pending 契约一致，供入口短路消费），同样走
   return_direct 硬停等用户答复。

4. 工具侧信息（ask_guide references / clarify 文案 / 软确认反问）经
   RequestCollector 带回引擎层，由引擎组装对外返回契约——工具不直接
   碰对外响应。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Annotated, Callable, Literal, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, create_model

from app.dispatcher import debug_log, sessions
from app.dispatcher.sessions import DispatcherSession
from app.dispatcher.tools import TOOLS, Tool, validate_args, visible_tools

logger = logging.getLogger(__name__)

TOOL_RESULT_CAP = 6000  # 只读工具结果 JSON 序列化后回喂 LLM 的字符上限（同 loop.py）

# 一次一确认的竞态防线：ToolNode 对同一条 AIMessage 的多个工具调用是
# 线程池并发执行的（executor.map），「查 pending 为空 → preview → 存
# pending」若非原子，并行双写会双双通过检查、后写覆盖先写（出两张卡）。
# 模块级锁 + 双重检查（preview 前快查、存卡前锁内复查）保证一次一确认：
# 先完成存卡者胜，后到者按「已有一个待确认的操作」拒绝（对外契约不变）。
_PENDING_ACTION_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 请求级收集器：工具侧信息带回引擎组装返回契约
# ---------------------------------------------------------------------------

@dataclass
class RequestCollector:
    """一次请求内工具执行产出的附加信息（由引擎层读取组装对外响应）。"""

    references: list = field(default_factory=list)   # ask_guide 命中的知识条目引用
    clarify_text: str | None = None                  # preview blocked 转的 clarify 文案
    soft_confirm_question: str | None = None         # request_clarification 生成的反问文案
    file_selection_request: dict | None = None       # request_file_selection 请求参数


# ---------------------------------------------------------------------------
# 紧凑摘要（与 loop.py 同口径：写 tool_history / on_progress 用，截断防爆）
# ---------------------------------------------------------------------------

def _args_summary(args: dict) -> str:
    """工具参数的紧凑摘要（≤300 字符）。"""
    return json.dumps(args, ensure_ascii=False, default=str)[:300]


def _result_summary(result: dict) -> str:
    """工具结果的紧凑摘要（≤300 字符）。"""
    return json.dumps(result, ensure_ascii=False, default=str)[:300]


# ---------------------------------------------------------------------------
# JSON Schema → pydantic args_schema（注册表的 parameters 都很简单，
# 覆盖 string/integer/number/boolean/array/object + required/enum/
# description/default 即可，不追求完整 JSON Schema）
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    # 嵌套 object/array 宽松落为 dict/list（注册表里最深也就一层 items）
    "array": list,
    "object": dict,
}


def _prop_to_type(prop: dict) -> type:
    """单个 property 的 JSON Schema → Python 类型；enum 收紧为 Literal。"""
    prop = prop or {}
    enum = prop.get("enum")
    if isinstance(enum, list) and enum and all(
            isinstance(v, (str, int, float, bool)) for v in enum):
        return Literal.__getitem__(tuple(enum))  # type: ignore[return-value]
    return _TYPE_MAP.get(prop.get("type"), str)


def _schema_to_fields(schema: dict) -> dict:
    """JSON Schema properties/required → create_model 的字段定义 dict。"""
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    fields: dict = {}
    for key, prop in properties.items():
        prop = prop or {}
        py_type = _prop_to_type(prop)
        description = prop.get("description")
        if key in required:
            fields[key] = (py_type, Field(..., description=description))
        else:
            # 可选参数允许 LLM 显式传 null（与 validate_args「可选 None
            # 视为未传」语义一致），故用 Optional + 缺省 None
            fields[key] = (Optional[py_type],
                           Field(prop.get("default"), description=description))
    return fields


def _json_schema_to_model(tool_name: str, schema: dict,
                          extra_fields: dict | None = None) -> type[BaseModel]:
    """用 pydantic create_model 把注册表 parameters 动态转成 args_schema。

    extra_fields：追加不进 LLM 可见 schema 的注入字段（如写影子工具的
    tool_call_id: InjectedToolCallId，由 ToolNode 从 ToolCall 注入，
    LLM 伪造不了）。
    """
    fields = _schema_to_fields(schema)
    if extra_fields:
        fields.update(extra_fields)
    return create_model(f"dispatcher_{tool_name}_args", **fields)


# ---------------------------------------------------------------------------
# 只读工具包装（risk="read"）：直接执行，结果截断回喂
# ---------------------------------------------------------------------------

def _wrap_read_tool(tool: Tool, session: DispatcherSession,
                    session_id: str | None, collector: RequestCollector,
                    on_progress: Callable[[dict], None] | None) -> Callable:
    """只读工具闭包：进度事件 + 执行 + 审计记录 + ask_guide 引用收集。"""

    def _run(**kwargs) -> str:
        # 剔除 None（可选参数未传），与 validate_args 语义一致
        args = {k: v for k, v in kwargs.items() if v is not None}
        if on_progress:
            on_progress({"type": "tool_call", "tool": tool.name,
                         "args_summary": _args_summary(args)})
        # func 契约内部不抛异常（错误走 {"error": ...}），仍兜底防个别例外
        try:
            result = tool.func(args)
        except Exception as e:  # noqa: BLE001 工具层绝不抛出
            result = {"error": f"{type(e).__name__}: {e}"}
        sessions.record_tool(session, tool=tool.name,
                             args_summary=_args_summary(args),
                             result_summary=_result_summary(result))
        debug_log.log_tool_result(session_id=session_id, name=tool.name,
                                  args=args, result=result)
        if on_progress:
            if isinstance(result, dict) and result.get("error"):
                on_progress({"type": "tool_error", "tool": tool.name,
                             "error": str(result.get("error"))[:300]})
            else:
                on_progress({"type": "tool_result", "tool": tool.name,
                             "result_summary": _result_summary(result)})
        # 特例：ask_guide 的知识引用带回引擎层组装对外响应
        if tool.name == "ask_guide" and isinstance(result, dict):
            refs = result.get("references")
            if isinstance(refs, list):
                collector.references.extend(refs)
        return json.dumps(result, ensure_ascii=False, default=str)[:TOOL_RESULT_CAP]

    return _run


# ---------------------------------------------------------------------------
# build_pending_action：写工具确认门的共享实现（影子工具 / 软确认路径复用）
# ---------------------------------------------------------------------------

def build_pending_action(tool_name: str, args: dict,
                         session: DispatcherSession,
                         collector: RequestCollector | None = None,
                         on_progress: Callable[[dict], None] | None = None,
                         session_id: str | None = None) -> dict:
    """写工具预览 + 存 pending_action（绝不执行），与 loop.py 确认门语义一致。

    返回 {"ok": bool, "clarify": bool, "msg_text": str, "history_text": str,
    "action": dict | None}：
    - ok=False：参数校验失败 / 已有待确认操作 / 预览生成异常——不写任何状态，
      msg_text 回喂模型让它向用户反问或修正；
    - ok=True, clarify=True：preview blocked（业务硬校验失败），直接转
      clarify 文案，不出确认卡；
    - ok=True, clarify=False：action 信封已存 session.pending_action，
      msg_text 是确认提示（前端确认卡用），history_text 含预览逐行详情
      （写对话历史用，LLM 后续轮次能回答追问）。
    """
    tool = TOOLS.get(tool_name)
    if tool is None or tool.risk != "write" or tool.preview is None:
        msg = f"未知写工具：{tool_name}。请从可用工具清单中选择。"
        return {"ok": False, "clarify": False, "action": None,
                "msg_text": msg, "history_text": msg}

    # 第一道防线：参数校验（防幻觉参数；不写任何状态）
    clean_args, arg_error = validate_args(args, tool.parameters)
    if arg_error:
        msg = f"参数校验未通过：{arg_error}。请修正后重新调用。"
        return {"ok": False, "clarify": False, "action": None,
                "msg_text": msg, "history_text": msg}

    # 一次一确认（代码保险）：已有待确认操作时拒绝再发起（preview 前快查，
    # 并发双写的最终防线在存卡前的锁内复查，见 _PENDING_ACTION_LOCK 注释）
    if session.pending_action:
        msg = "已有一个待确认的操作，请先确认或取消后再发起新的。"
        return {"ok": False, "clarify": False, "action": None,
                "msg_text": msg, "history_text": msg}

    # preview 契约未保证不抛，兜底为 ok=False 错误文案
    try:
        # 透传 session_id：写工具 preview 内部做 pinned scope 检查
        preview = tool.preview(clean_args, session_id)
    except Exception as exc:  # noqa: BLE001 工具层绝不抛出
        logger.warning("写工具预览生成失败 | 工具=%s | 错误=%s", tool_name, exc)
        msg = f"预览生成失败：{exc}。"
        return {"ok": False, "clarify": False, "action": None,
                "msg_text": msg, "history_text": msg}

    # 业务硬校验失败（如工厂名不在对照表）：不让 LLM 圆场，直接转 clarify
    if isinstance(preview, dict) and preview.get("blocked"):
        clarify_text = "操作无法发起：" + "；".join(
            str(w) for w in (preview.get("warnings")
                             or [preview.get("summary", "")]))
        if collector is not None:
            collector.clarify_text = clarify_text
        debug_log.log_event("preview_blocked", session_id=session_id,
                            tool=tool_name, reason=clarify_text)
        logger.info("预览被业务硬校验拦截，转 clarify | 工具=%s", tool_name)
        return {"ok": True, "clarify": True, "action": None,
                "msg_text": clarify_text, "history_text": clarify_text}

    # 组信封（字段与 loop.py 确认门完全一致），服务端持有防篡改；
    # 存卡前锁内复查——并发双写时先完成存卡者胜，后到者按一次一确认拒绝
    action = {
        "kind": "dispatcher_tool",
        "tool": tool_name,
        "args": clean_args,
        "summary": preview["summary"],
        "preview_lines": preview["lines"],
        "warnings": preview.get("warnings", []),
        "created_at": time.time(),
        # W5 透传：工厂名对照预扫结果随信封留存，刷新恢复确认卡时仍完整
        "factory_scan": preview.get("factory_scan"),
    }
    with _PENDING_ACTION_LOCK:
        if session.pending_action:
            msg = "已有一个待确认的操作，请先确认或取消后再发起新的。"
            return {"ok": False, "clarify": False, "action": None,
                    "msg_text": msg, "history_text": msg}
        session.pending_action = action
        sessions.persist_pending(session)

    sessions.record_tool(session, tool=tool_name,
                         args_summary=_args_summary(clean_args),
                         result_summary="待人工确认", confirmed=None)
    debug_log.log_confirm_gate(session_id=session_id, name=tool_name,
                               args=clean_args, summary=str(preview["summary"]))
    logger.info("确认门拦截写工具（影子模式）| 工具=%s | 预览摘要=%s",
                tool_name, str(preview["summary"])[:200])

    # 前端确认卡消息（不含 preview_lines，前端已单独渲染）
    msg_text = action["summary"] + "\n请确认是否执行（确认后生效）。"
    # 对话历史写入预览详情（LLM 后续轮次能回答追问，如"哪些存疑"）
    history_text = msg_text
    if action["preview_lines"]:
        history_text = (action["summary"] + "\n"
                        + "\n".join(action["preview_lines"]) + "\n"
                        + "请确认是否执行（确认后生效）。")
    if on_progress:
        on_progress({"type": "pending_confirmation",
                     "tool": tool_name, "preview": action["preview_lines"],
                     "message": msg_text, "action": action,
                     "warnings": action["warnings"],
                     "factory_scan": preview.get("factory_scan")})
    return {"ok": True, "clarify": False, "action": action,
            "msg_text": msg_text, "history_text": history_text}


# ---------------------------------------------------------------------------
# 写影子工具（risk="write"）：绝不执行，只生成预览确认卡
# ---------------------------------------------------------------------------

def _wrap_write_shadow(tool: Tool, session: DispatcherSession,
                       session_id: str | None, collector: RequestCollector,
                       on_progress: Callable[[dict], None] | None) -> Callable:
    """写工具影子闭包：走 build_pending_action 后硬停本轮 react 循环。

    失败（ok=False）返回纯字符串让模型向用户反问/修正（不硬停）；
    成功返回 Command(update=[ToolMessage(确认文案)])——确认文案放
    ToolMessage.content（return_direct 硬停的要求，见模块 docstring），
    引擎层取末条 ToolMessage.content 作为用户可见文本。
    """

    def _run(**kwargs):
        tool_call_id = kwargs.pop("tool_call_id", "")
        args = {k: v for k, v in kwargs.items() if v is not None}
        outcome = build_pending_action(tool.name, args, session,
                                       collector=collector,
                                       on_progress=on_progress,
                                       session_id=session_id)
        if not outcome["ok"]:
            return outcome["msg_text"]
        return Command(update={"messages": [
            ToolMessage(content=outcome["msg_text"], tool_call_id=tool_call_id),
        ]})

    return _run


# ---------------------------------------------------------------------------
# UI 工具包装（risk="ui"）：硬停 react 循环，等用户在界面交互
# ---------------------------------------------------------------------------

def _wrap_ui_tool(tool: Tool, session: DispatcherSession,
                  session_id: str | None, collector: RequestCollector,
                  on_progress: Callable[[dict], None] | None) -> Callable:
    """UI 工具闭包：存 pending 状态 + 硬停 react 循环，等用户在界面交互。

    与写影子工具类似用 return_direct 硬停，但不走确认门——
    用户交互的结果（如文件路径）作为新用户消息在下一轮进入对话历史，
    Agent 自然看到并继续推理。
    """

    def _run(**kwargs):
        tool_call_id = kwargs.pop("tool_call_id", "")
        args = {k: v for k, v in kwargs.items() if v is not None}

        if on_progress:
            on_progress({"type": "tool_call", "tool": tool.name,
                         "args_summary": _args_summary(args)})

        if tool.name == "request_file_selection":
            file_type = args.get("type", "dir")
            extensions = args.get("extensions")
            title = args.get("title", "选择路径")

            # 存 pending 状态
            sessions.set_file_selection_request(
                session, file_type=file_type,
                extensions=extensions, title=title)
            collector.file_selection_request = {
                "type": file_type,
                "extensions": extensions,
                "title": title,
            }

            sessions.record_tool(
                session, tool=tool.name,
                args_summary=_args_summary(args),
                result_summary=f"待用户选择 ({file_type})", confirmed=None)
            debug_log.log_event("file_selection_requested",
                                session_id=session_id,
                                file_type=file_type,
                                extensions=extensions, title=title)
            logger.info("UI 工具挂起等待用户选择 | 工具=%s | type=%s",
                        tool.name, file_type)

            msg_text = f"请在界面上选择{title}（类型: {file_type}）"
            if extensions:
                msg_text += f"，仅限 {extensions} 格式"
            return Command(update={"messages": [
                ToolMessage(content=msg_text, tool_call_id=tool_call_id),
            ]})

        return f"未知的 UI 工具：{tool.name}"

    return _run


# ---------------------------------------------------------------------------
# request_clarification：黄灯反问重建（仅 phase>=2 暴露）
# ---------------------------------------------------------------------------

# 黄灯区确认式反问的工具中文名映射（从 __init__.py 复制，避免循环 import）
_TOOL_CN = {
    "create_batch": "发起新批次",
    "rerun": "整批重跑",
    "retry_factory": "重试当前工厂",
    "force_extract_file": "指定文件重新提取",
    "submit_review": "提交审核",
    "set_paths": "修改路径配置",
    "curate_kb": "排查知识库",
    "start_split": "启动分票",
    "confirm_split": "确认分票方案",
    "reset_split": "重置分票",
    "generate_declarations": "生成报关单",
    "upsert_product_mapping": "维护产品映射",
}


def _soft_confirm_question(tool: str, slots: dict) -> str:
    """黄灯区缺省确认式反问（代码拼装，与 __init__.py 同逻辑）。

    槽位摘要只翻译 thread_id（"批次 X"）/ factory（"工厂 X"）两键，
    其余不展开（Dispatcher 对外文案不得暴露内部参数名）。
    """
    cn = _TOOL_CN.get(tool, "该操作")
    parts = []
    if slots.get("thread_id"):
        parts.append(f"批次 {slots['thread_id']}")
    if slots.get("factory"):
        parts.append(f"工厂 {slots['factory']}")
    summary = f"（{'、'.join(parts)}）" if parts else ""
    return f"您是想要{cn}{summary}吗？回复“是”即可继续。"


_CLARIFY_DESCRIPTION = (
    "当你推测用户可能想执行某项写操作（发起批次/重跑/改数审核/改路径等）"
    "但不确定时调用——绝不直接调用写工具。用户表述模糊、参数靠推测、"
    "指代不清时必须先调本工具。"
)

_CLARIFY_ARGS_SCHEMA = create_model(
    "dispatcher_request_clarification_args",
    target_action=(str, Field(..., description="目标写工具名")),
    args=(dict, Field(default_factory=dict,
                      description="已收集的参数，可为空")),
    question=(str, Field(..., description="疑点简述")),
    tool_call_id=(Annotated[str, InjectedToolCallId], ...),
)


def _build_request_clarification(session: DispatcherSession,
                                 collector: RequestCollector,
                                 on_progress: Callable[[dict], None] | None
                                 ) -> BaseTool:
    """黄灯反问工具：代码生成反问文案 + 存 soft_pending 软挂起。

    失败（目标非写工具/参数校验不过）返回纯字符串让模型修正或向用户
    核实；成功存 soft_pending（一轮有效，等用户「是/算了」由入口短路
    消费）后 return Command(update=[ToolMessage(反问文案)]) 硬停本轮
    循环（return_direct，见模块 docstring）。
    """

    def _run(**kwargs):
        tool_call_id = kwargs.pop("tool_call_id", "")
        target_action = str(kwargs.get("target_action") or "")
        raw_args = kwargs.get("args") or {}
        question = str(kwargs.get("question") or "")

        tool = TOOLS.get(target_action)
        if tool is None or tool.risk != "write":
            return (f"目标工具 {target_action} 不是写工具。"
                    "request_clarification 仅用于写操作意图确认，"
                    "请修正 target_action 后重新调用。")

        clean_args, arg_error = validate_args(raw_args, tool.parameters)
        if arg_error:
            return (f"已收集参数校验未通过：{arg_error}。"
                    "请向用户核实参数后重新调用。")

        # 代码生成反问文案（绝不播模型自由发挥）；模型疑点简述附后供用户核对
        question_text = _soft_confirm_question(target_action, clean_args)
        if question.strip():
            question_text += f"\n（疑点：{question.strip()}）"

        sessions.set_soft_pending(session, target_action, clean_args,
                                  question_text)
        collector.soft_confirm_question = question_text
        logger.info("黄灯反问已布防 | 目标工具=%s | 参数=%s",
                    target_action, _args_summary(clean_args))
        debug_log.log_event("soft_confirm_asked",
                            tool=target_action, question=question_text)
        return Command(update={"messages": [
            ToolMessage(content=question_text, tool_call_id=tool_call_id),
        ]})

    return StructuredTool.from_function(
        func=_run,
        name="request_clarification",
        description=_CLARIFY_DESCRIPTION,
        args_schema=_CLARIFY_ARGS_SCHEMA,
        return_direct=True,  # 硬停：见模块 docstring 的 spike 实证说明
    )


# ---------------------------------------------------------------------------
# 主入口：每次请求现建一套工具（闭包绑定 session / on_progress）
# ---------------------------------------------------------------------------

def build_tools(session: DispatcherSession, session_id: str | None,
                phase: int,
                on_progress: Callable[[dict], None] | None
                ) -> tuple[list[BaseTool], RequestCollector]:
    """把注册表 visible_tools(phase) 包装成 LangChain 工具。

    - 只读工具：StructuredTool 直接执行；
    - 写工具：同名影子工具（description 尾部注明只生成预览确认卡），
      args_schema 追加 tool_call_id: InjectedToolCallId（ToolNode 注入，
      LLM 不可见也伪造不了）；
    - phase>=2 追加 request_clarification（黄灯反问）。

    返回 (tools, collector)：collector 收集工具侧信息（ask_guide 引用 /
    clarify 文案 / 软确认反问），由引擎层组装对外返回契约。
    """
    collector = RequestCollector()
    tools: list[BaseTool] = []
    for tool in visible_tools(phase):
        if tool.risk == "read":
            lc_tool = StructuredTool.from_function(
                func=_wrap_read_tool(tool, session, session_id,
                                     collector, on_progress),
                name=tool.name,
                description=tool.description,
                args_schema=_json_schema_to_model(tool.name, tool.parameters),
            )
        elif tool.risk == "ui":
            # UI 工具：硬停 react 循环，等用户在界面交互
            lc_tool = StructuredTool.from_function(
                func=_wrap_ui_tool(tool, session, session_id,
                                   collector, on_progress),
                name=tool.name,
                description=tool.description,
                args_schema=_json_schema_to_model(
                    tool.name, tool.parameters,
                    extra_fields={"tool_call_id": (
                        Annotated[str, InjectedToolCallId], ...)}),
                return_direct=True,  # 硬停，等用户交互
            )
        else:
            lc_tool = StructuredTool.from_function(
                func=_wrap_write_shadow(tool, session, session_id,
                                        collector, on_progress),
                name=tool.name,
                description=(tool.description
                             + "（调用后仅生成预览确认卡，不会真正执行）"),
                args_schema=_json_schema_to_model(
                    tool.name, tool.parameters,
                    extra_fields={"tool_call_id": (
                        Annotated[str, InjectedToolCallId], ...)}),
                return_direct=True,  # 硬停：见模块 docstring 的 spike 实证说明
            )
        tools.append(lc_tool)
    if phase >= 2:
        tools.append(_build_request_clarification(session, collector,
                                                  on_progress))
    return tools, collector


__all__ = ["RequestCollector", "build_tools", "build_pending_action"]
