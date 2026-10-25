"""ACP（Agent Client Protocol）v2 协议层常量与编解码小工具。

本模块只放**与传输无关**的协议知识：

* 方法名（``initialize`` / ``session/new`` / ``session/prompt`` …）；
* ``session/update`` 的事件类型（``agent_message_chunk`` / ``state_update`` …）；
* JSON-RPC 2.0 报文构造与应答判别；
* ContentBlock 的文本提取（alkaid0 的标准事件字段放在 update 顶层，形状随事件而异）。

传输（WebSocket）见 :mod:`acp.client`，会话编排见 :mod:`acp.pool`。

协议基线：ACP v2（``protocolVersion: 2``）+ alkaid0 的 ``alk.cxykevin.top/`` 扩展，
细节以 alkaid0 仓库的 ``docs/acp/extension.md`` 为准。
"""

from __future__ import annotations

import json
from typing import Any

#: ACP 协议版本。alkaid0 只接受 2（其他版本在 ``initialize`` 里报错）。
PROTOCOL_VERSION = 2

# ── 客户端 → 服务端方法 ────────────────────────────────────────────────
METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SESSION_RESUME = "session/resume"
METHOD_SESSION_LIST = "session/list"
METHOD_SESSION_PROMPT = "session/prompt"
METHOD_SESSION_CANCEL = "session/cancel"
METHOD_SESSION_CLOSE = "session/close"
METHOD_SESSION_UPDATE = "session/update"
METHOD_SESSION_SET_CONFIG_OPTION = "session/set_config_option"

#: 服务端 → 客户端方法（需要客户端回包）。
METHOD_REQUEST_PERMISSION = "session/request_permission"

# ── session/update 的 sessionUpdate 取值 ───────────────────────────────
UPDATE_USER_MESSAGE = "user_message"
UPDATE_USER_MESSAGE_CHUNK = "user_message_chunk"
UPDATE_AGENT_MESSAGE = "agent_message"
UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
UPDATE_AGENT_THOUGHT = "agent_thought"
UPDATE_AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
UPDATE_TOOL_CALL = "tool_call_update"
UPDATE_STATE = "state_update"
UPDATE_PLAN = "plan_update"
UPDATE_USAGE = "usage_update"
UPDATE_CONFIG_OPTION = "config_option_update"
UPDATE_AVAILABLE_COMMANDS = "available_commands_update"
UPDATE_SESSION_INFO = "session_info_update"

#: alkaid0 扩展事件（统一挂 ``alk.cxykevin.top/`` 前缀）。
UPDATE_AGENT_STATUS = "alk.cxykevin.top/agent_status"
UPDATE_SUMMARY = "alk.cxykevin.top/summary"
UPDATE_TERMINAL = "alk.cxykevin.top/terminal_update"
UPDATE_SHELL_STOP = "alk.cxykevin.top/shell_stop"
UPDATE_REWIND = "alk.cxykevin.top/session/rewind"

# ── state_update 的 state / stopReason ────────────────────────────────
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_REQUIRES_ACTION = "requires_action"

STOP_REASON_END_TURN = "end_turn"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_MAX_TURN_REQUESTS = "max_turn_requests"
STOP_REASON_REFUSAL = "refusal"
STOP_REASON_CANCELLED = "cancelled"

# ── 扩展字段键 ────────────────────────────────────────────────────────
EXT_PREFIX = "alk.cxykevin.top/"
KEY_ERROR_MSG = EXT_PREFIX + "error_msg"
KEY_RUN_ID = EXT_PREFIX + "run_id"
KEY_TERMINAL_ID = EXT_PREFIX + "terminal_id"
KEY_SESSION_CURSOR = EXT_PREFIX + "session/cursor"
KEY_CALLING_INFO = EXT_PREFIX + "calling_info"
#: agent_message / agent_thought 上标注「这条是哪个子 Agent 产出」的字段。
KEY_AGENT_STATUS = EXT_PREFIX + "agent_status"

#: alkaid0 扩展方法（客户端 → 服务端）：fs over ACP 的 ``mkdir``。
#: 等价于 ``mkdir -p``（递归、幂等），``path`` 是**相对会话 cwd** 的路径。
METHOD_FS_MKDIR = EXT_PREFIX + "fs/mkdir"

#: alkaid0 扩展方法：查询 / 删除当前已绑定会话的定时任务（ext §3.11）。
METHOD_CRON_LIST = EXT_PREFIX + "session/cron/list"
METHOD_CRON_DELETE = EXT_PREFIX + "session/cron/delete"

#: alkaid0 扩展能力标记：服务端支持 ``session/cron/*`` 才为真。
CAPABILITY_CRON = EXT_PREFIX + "cron"

# ── tool_call_update 的 status ────────────────────────────────────────
TOOL_STATUS_PENDING = "pending"
#: alkaid0 的流式预览（0.1s 限流推完整快照），回显时要跳过以免刷屏。
TOOL_STATUS_STREAMING = "streaming"
TOOL_STATUS_COMPLETED = "completed"
TOOL_STATUS_CANCELLED = "cancelled"

#: ``alk.cxykevin.top/shell_stop``（后台 run 任务结束）的字段名，见扩展文档 §2.3。
SHELL_STOP_RUN_ID = "runId"
SHELL_STOP_COMMAND = "command"
SHELL_STOP_SUCCESS = "success"
SHELL_STOP_KILLED = "killed"

# ── configOptions（session/new / session/resume 与 config_option_update）──
#: 配置项类型（ACP v2：``id`` 是下拉选择，``boolean`` 是开关）。
CONFIG_TYPE_SELECT = "id"
CONFIG_TYPE_BOOLEAN = "boolean"

#: alkaid0 当前提供的配置项（扩展文档 §4.2）。
CONFIG_ID_MODEL = "model"
CONFIG_ID_THOUGHT_LEVEL = "thought_level"

# ── session/request_permission ────────────────────────────────────────
PERMISSION_OUTCOME_SELECTED = "selected"
PERMISSION_OUTCOME_CANCELLED = "cancelled"
PERMISSION_OPTION_ALLOW_ONCE = "allow_once"
PERMISSION_OPTION_REJECT_ONCE = "reject_once"

# ── JSON-RPC 错误码 ───────────────────────────────────────────────────
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603
#: alkaid0 服务端错误统一用这个码。
ERROR_SERVER = -32099


def text_block(text: str) -> dict[str, Any]:
    """构造一个文本 ContentBlock。"""
    return {"type": "text", "text": text}


def prompt_blocks(text: str) -> list[dict[str, Any]]:
    """把一段文本包成 ``session/prompt`` 的 ``prompt`` 数组。"""
    return [text_block(text)]


def extract_text(content: Any) -> str:
    """从 ContentBlock / ContentBlock[] / 字符串里尽力取出文本。

    alkaid0 的 ``content`` 在不同事件里的形状不一致：流式 chunk 是单个
    ContentBlock，整消息 upsert 是数组，扩展事件可能直接给字符串。这里统一兜住；
    拿不到文本时返回空串（调用方据此跳过即可）。

    Args:
        content: 任意形状的内容字段。

    Returns:
        提取到的文本；无法识别时为空串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        # 少数事件把正文放在嵌套的 content 里
        nested = content.get("content")
        return extract_text(nested) if nested is not None else ""
    if isinstance(content, (list, tuple)):
        return "".join(extract_text(item) for item in content)
    return ""


def _option_list(value: Any) -> list[dict]:
    """把 ``configOptions`` / ``options`` 这类字段洗成「字典列表」。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def config_options_of(payload: Any) -> list[dict]:
    """从响应 / 事件里取 ``configOptions``。

    该字段出现在 ``session/new`` / ``session/resume`` 的响应里，也出现在
    ``config_option_update`` 事件的 update 顶层（形状相同）。形状不对时返回空列表。
    """
    if not isinstance(payload, dict):
        return []
    return _option_list(payload.get("configOptions"))


def find_config_option(options: Any, config_id: str) -> dict | None:
    """在配置项列表里按 ``configId`` 找一项；找不到返回 ``None``。"""
    for item in _option_list(options):
        if item.get("configId") == config_id:
            return item
    return None


def option_choices(option: Any) -> list[dict]:
    """配置项的可选值列表（``options``）；形状不对时返回空列表。"""
    return _option_list(option.get("options")) if isinstance(option, dict) else []


def option_current(option: Any) -> str:
    """配置项的当前值（``currentValue``）；非字符串当空串。"""
    value = option.get("currentValue") if isinstance(option, dict) else None
    return value if isinstance(value, str) else ""


def option_choice_label(choice: Any) -> tuple[str, str]:
    """取可选值的 ``(value, 展示名)``；``name`` 为空时用 ``value`` 兜底。"""
    if not isinstance(choice, dict):
        return "", ""
    value = choice.get("value")
    value = value if isinstance(value, str) else ""
    name = choice.get("name")
    name = name.strip() if isinstance(name, str) else ""
    return value, (name or value)


def _token_count(value: Any) -> int | None:
    """把 token 计数字段洗成非负整数。

    字段缺席算 0（alkaid0 的 ``used`` / ``size`` 带 ``omitempty``，值为 0 时字段不出现）；
    其它不是非负数字的（字符串、负数、布尔）一律返回 ``None``，由调用方当作形状不对。
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def usage_of(payload: Any) -> tuple[int, int] | None:
    """从 ``usage_update`` 的 update 里取上下文用量 ``(used, size)``。

    * ``used``：最近一次模型响应的 token 总量（含输出）—— 服务端就用它判断要不要压缩；
    * ``size``：当前模型的上下文窗口（alkaid0 取模型配置的 ``TokenLimit``，取不到时 8192）。

    不是 ``usage_update``、或两个字段里有一个不是非负数字时返回 ``None``（调用方忽略这条）。
    """
    if not isinstance(payload, dict) or payload.get("sessionUpdate") != UPDATE_USAGE:
        return None
    used = _token_count(payload.get("used"))
    size = _token_count(payload.get("size"))
    if used is None or size is None:
        return None
    return used, size


def jsonrpc_request(request_id: Any, method: str, params: dict | None = None) -> dict:
    """构造 JSON-RPC 2.0 请求报文。"""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def jsonrpc_notification(method: str, params: dict | None = None) -> dict:
    """构造 JSON-RPC 2.0 通知报文（没有 ``id``）。"""
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def jsonrpc_result(request_id: Any, result: Any) -> dict:
    """构造 JSON-RPC 2.0 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    """构造 JSON-RPC 2.0 错误响应。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_message(raw: str | bytes) -> dict:
    """把 WebSocket 收到的原始帧解析成 JSON-RPC 报文。

    Raises:
        ValueError: 不是合法 JSON，或顶层不是对象。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError(f"JSON-RPC 报文必须是对象，实际是 {type(message).__name__}")
    return message


def is_response(message: dict) -> bool:
    """该报文是不是「对某个请求的响应」（有 ``id`` 且有 ``result`` / ``error``）。"""
    return "id" in message and ("result" in message or "error" in message)


def is_request(message: dict) -> bool:
    """该报文是不是「服务端发来的请求」（有 ``id`` 且有 ``method``）。"""
    return "id" in message and "method" in message


def is_notification(message: dict) -> bool:
    """该报文是不是「通知」（有 ``method``、没有 ``id``）。"""
    return "method" in message and "id" not in message


def describe_rpc_error(message: dict) -> str:
    """把错误响应压成一行便于日志阅读的文本。"""
    error = message.get("error")
    if not isinstance(error, dict):
        return "未知错误"
    code = error.get("code", "?")
    text = error.get("message", "")
    data = error.get("data")
    if data:
        return f"[{code}] {text}（{data}）"
    return f"[{code}] {text}"
