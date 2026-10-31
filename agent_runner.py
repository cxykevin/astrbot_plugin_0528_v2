"""Alkaid0 Agent Runner —— 让 AstrBot 的「执行器」跑在 alkaid0 上。

一轮对话的映射关系（ACP v2）：

1. 从消息事件算出**群键**（``unified_msg_origin``）与**工作目录**（路径模板渲染）；
2. 向 :class:`~acp.pool.SessionPool` 要该群的连接 —— 没有就连一条 WebSocket、
   ``initialize`` 握手、``session/new`` 开一个活跃会话；已有就复用；
3. ``session/prompt`` 把用户消息发过去（ACP v2 是立即确认，正文靠 ``session/update``
   通知推送）；
4. 消费通知：``agent_message_chunk`` 累积正文（流式时逐个推给内核）、
   ``agent_thought_chunk`` 累积思考、过程消息（工具调用 / 子 Agent / 后台任务）按
   :class:`ProgressEcho` 的规则回显进回复、``state_update`` 的 ``idle`` 标记本轮结束；
5. 结束时不主动断连 —— 连接留给下一个群消息复用，空闲超时由连接池回收。

面板配置（``agent_runner.config``）优先、插件配置兜底，取值逻辑见
:meth:`RunnerSettings.from_config`。
"""

from __future__ import annotations

import asyncio
import re
import time
import typing as T
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from . import at_parse, cq_parse, meme_bridge
from .acp import ACPConnectionError, ACPError, GroupSession, SessionPool
from .acp import approval as approval_mod
from .acp import protocol as acp
from .turn_queue import (
    ACTION_ABSORBED,
    ACTION_MERGED,
    TurnQueue,
    TurnSlot,
    strip_leading_slash,
)

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"

#: 执行器类型名（即 ``agent_runner.runner_type`` 的取值）。
RUNNER_TYPE = "alkaid0"

#: 面板下拉框里的展示名。
RUNNER_LABEL = "Alkaid0"

#: 执行器说明（librunner 会登记成 WebUI 词条）。
RUNNER_DESCRIPTION = (
    "把消息交给 Alkaid0 Coding Agent（ACP v2 over WebSocket）。"
    "一个群一条连接、一个活跃会话，群空闲超过阈值就回收连接。"
)

#: 未配置时的默认 ACP 地址（alkaid0 默认监听 7433 的 ``/acp``）。
DEFAULT_ACP_URL = "ws://127.0.0.1:7433/acp"

#: 未配置时的默认工作目录模板：``{groupid}`` 会被换成群号（私聊换成会话号）。
DEFAULT_WORKSPACE_TEMPLATE = "/root/alkaid0/{groupid}"

#: 群空闲多久后回收连接（秒）。
DEFAULT_IDLE_TIMEOUT_SEC = 1800

#: 单轮对话最多等多久（秒）。
DEFAULT_TURN_TIMEOUT_SEC = 900

#: 执行器在面板上的默认配置（同时是字段白名单）。
RUNNER_CONFIG_DEFAULT: dict[str, T.Any] = {
    "acp_url": "",
    "workspace_template": "",
    "idle_timeout_sec": DEFAULT_IDLE_TIMEOUT_SEC,
    "turn_timeout_sec": DEFAULT_TURN_TIMEOUT_SEC,
    "echo_events": True,
    "show_thought": False,
    "at_parse_enable": False,
    "cq_parse_enable": False,
    "auto_approve_enable": False,
    "auto_approve_tools": "",
    "auto_approve_prompt": True,
    "auto_approve_hook": True,
}

#: 执行器配置字段在面板上的说明。
RUNNER_CONFIG_METADATA: dict[str, T.Any] = {
    "acp_url": {
        "description": "Alkaid0 ACP 地址（WebSocket）",
        "type": "string",
        "hint": "形如 ws://127.0.0.1:7433/acp?key=<key>。留空则用插件配置里的默认值。",
    },
    "workspace_template": {
        "description": "工作目录模板",
        "type": "string",
        "hint": (
            "支持 {groupid}（群号，私聊为会话号）、{sessionid}、{platform}。"
            "留空则用插件配置里的默认值；目录需要在 alkaid0 侧已存在。"
        ),
    },
    "idle_timeout_sec": {
        "description": "群空闲回收秒数",
        "type": "int",
        "hint": "某个群超过这么久没有消息，就结束它的 ACP 会话并断开连接；0 表示不自动回收。",
    },
    "turn_timeout_sec": {
        "description": "单轮最大等待秒数",
        "type": "int",
        "hint": "超过这个时间还没等到本轮结束（state_update idle），就按超时报错。",
    },
    "echo_events": {
        "description": "回显过程消息（工具调用 / 子 Agent / 后台任务）",
        "type": "bool",
        "hint": (
            "把 Alkaid0 的过程消息一起发到聊天里：🔧 工具调用、🤖 子 Agent、"
            "✅ 后台任务结束。关闭后只发最终答复（工具审批提示不受此开关影响）。"
        ),
    },
    "show_thought": {
        "description": "把思考过程一起发出去",
        "type": "bool",
        "hint": "开启后 agent_thought 会被放进 reasoning 消息链。",
    },
    "at_parse_enable": {
        "description": "启用 at 解析",
        "type": "bool",
        "hint": (
            "把回复里的 @昵称(QQ号) / @QQ号 / @(QQ号) 解析成真正的 at（只取号码，"
            "昵称不参与）。只在群聊、且上游是 OneBot（aiocqhttp）时生效；"
            "私聊与其它平台保持原文。"
        ),
    },
    "cq_parse_enable": {
        "description": "启用 CQ 码解析",
        "type": "bool",
        "hint": (
            "把回复里的 [CQ:at,qq=…] / [CQ:face,id=…] / [CQ:poke,…] / "
            "[CQ:dice] / [CQ:rps] / [CQ:shake] 解析成组件；其它 CQ 码直接删掉，"
            "不在聊天里显示。只有 OneBot（aiocqhttp）平台会构造组件，"
            "其它平台只删除、不显示。"
        ),
    },
    "auto_approve_enable": {
        "description": "启用工具审批直通（Auto-Approve）",
        "type": "bool",
        "hint": (
            "总开关。关闭时所有审批仍按旧行为挂起，等群友发 /aapprove；"
            "开启后才按「配置白名单 / 提示词 / 外部钩子」三层授权自动放行。"
        ),
    },
    "auto_approve_tools": {
        "description": "自动批准的工具白名单",
        "type": "string",
        "hint": (
            "逗号 / 空格 / 换行分隔，大小写不敏感，支持 * 与 ? 通配；"
            "单独写 * 表示全部工具。留空表示配置层不放行任何工具。"
        ),
    },
    "auto_approve_prompt": {
        "description": "允许提示词临时授权",
        "type": "bool",
        "hint": (
            "开启后，群消息里写 [auto_approve: read, edit] 可只对本轮放行；"
            "不写工具名（[auto_approve]）表示本轮全部放行。标记会从提示词里删掉。"
        ),
    },
    "auto_approve_hook": {
        "description": "允许外部钩子临时授权",
        "type": "bool",
        "hint": (
            "开启后，/wakeup 等外部入口可在请求里带 auto_approve（true 或工具名列表），"
            "只对这次外部回合放行；仍受总开关限制。"
        ),
    },
}

#: 路径模板里的占位符（``{name}``）；不认识的占位符原样保留。
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_TRUE_WORDS = {"1", "true", "yes", "on", "y", "t"}


# ── 配置解析 ──────────────────────────────────────────────────────────
def _as_bool(value: T.Any, default: bool = False) -> bool:
    """把配置值转成布尔（认字符串形式的 true/false）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        return default if not text else text in _TRUE_WORDS
    return default


def _as_float(value: T.Any, default: float) -> float:
    """把配置值转成浮点数，转换不了就用默认值。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _pick(
    provider_config: T.Any,
    plugin_config: T.Any,
    key: str,
) -> T.Any:
    """取值：面板配置优先，插件配置兜底；都没有时返回 None。

    ``None``、空串以及只由空白组成的字符串都算「没配」，继续往下找。
    """
    for source in (provider_config, plugin_config):
        if not isinstance(source, Mapping):
            continue
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        return value
    return None


@dataclass(frozen=True)
class RunnerSettings:
    """一轮对话用到的连接参数。

    Attributes:
        url: ACP WebSocket 地址。
        workspace_template: 工作目录模板（支持 ``{groupid}`` 等占位符）。
        idle_timeout_sec: 群空闲回收阈值（秒），``0`` 表示不自动回收。
        turn_timeout_sec: 单轮最大等待秒数。
        echo_events: 是否把过程消息（工具调用 / 子 Agent / 后台任务）回显进回复。
        show_thought: 是否把思考过程也作为 reasoning 消息发出。
        at_parse_enable: 是否把回复里的 ``@昵称(QQ号)`` 等写法解析成真正的 at。
        cq_parse_enable: 是否把回复里的 CQ 码解析成组件（不支持的 CQ 码直接删掉）。
        auto_approve_enable: 工具审批直通总开关。
        auto_approve_tools: 自动批准的工具白名单（逗号 / 空格分隔，支持通配）。
        auto_approve_prompt: 是否允许提示词里的 ``[auto_approve: ...]`` 临时授权。
        auto_approve_hook: 是否允许外部钩子按请求参数临时授权。
    """

    url: str = DEFAULT_ACP_URL
    workspace_template: str = DEFAULT_WORKSPACE_TEMPLATE
    idle_timeout_sec: float = float(DEFAULT_IDLE_TIMEOUT_SEC)
    turn_timeout_sec: float = float(DEFAULT_TURN_TIMEOUT_SEC)
    echo_events: bool = True
    show_thought: bool = False
    at_parse_enable: bool = False
    cq_parse_enable: bool = False
    auto_approve_enable: bool = False
    auto_approve_tools: str = ""
    auto_approve_prompt: bool = True
    auto_approve_hook: bool = True

    @classmethod
    def from_config(
        cls,
        provider_config: T.Any = None,
        plugin_config: T.Any = None,
    ) -> RunnerSettings:
        """按「面板值优先、插件配置兜底、最后代码默认」解析执行器参数。"""
        url = _pick(provider_config, plugin_config, "acp_url")
        template = _pick(provider_config, plugin_config, "workspace_template")
        return cls(
            url=url.strip() if isinstance(url, str) and url.strip() else DEFAULT_ACP_URL,
            workspace_template=(
                template.strip()
                if isinstance(template, str) and template.strip()
                else DEFAULT_WORKSPACE_TEMPLATE
            ),
            idle_timeout_sec=_as_float(
                _pick(provider_config, plugin_config, "idle_timeout_sec"),
                float(DEFAULT_IDLE_TIMEOUT_SEC),
            ),
            turn_timeout_sec=_as_float(
                _pick(provider_config, plugin_config, "turn_timeout_sec"),
                float(DEFAULT_TURN_TIMEOUT_SEC),
            ),
            echo_events=_as_bool(
                _pick(provider_config, plugin_config, "echo_events"), True
            ),
            show_thought=_as_bool(
                _pick(provider_config, plugin_config, "show_thought"), False
            ),
            at_parse_enable=_as_bool(
                _pick(provider_config, plugin_config, "at_parse_enable"), False
            ),
            cq_parse_enable=_as_bool(
                _pick(provider_config, plugin_config, "cq_parse_enable"), False
            ),
            auto_approve_enable=_as_bool(
                _pick(provider_config, plugin_config, "auto_approve_enable"), False
            ),
            auto_approve_tools=str(
                _pick(provider_config, plugin_config, "auto_approve_tools") or ""
            ).strip(),
            auto_approve_prompt=_as_bool(
                _pick(provider_config, plugin_config, "auto_approve_prompt"), True
            ),
            auto_approve_hook=_as_bool(
                _pick(provider_config, plugin_config, "auto_approve_hook"), True
            ),
        )


# ── 执行器面板（agent_runner.config）───────────────────────────────────
#: AstrBot 全局配置里执行器面板所在的段。
AGENT_RUNNER_SECTION = "agent_runner"


def panel_config_of(config: T.Any) -> Mapping[str, T.Any] | None:
    """从 AstrBot 全局配置里取出执行器面板的实配值（``agent_runner.config``）。

    消息路径由内核把这一份配置当作 ``provider_config`` 传给 Runner；命令路径
    （``/0528v2``、``/amodel``、``/aeffort``、``/aresume``、``/acmd``）没有这条路，
    得自己读 —— 否则会回落到插件配置，两条路解析出的 ACP 地址不一致：同一个群的
    连接会在两种地址之间被反复「参数已变更」回收重建，缺 key 时直接 401。

    Args:
        config: AstrBot 全局配置（``Context.get_config()`` 的返回值）。

    Returns:
        面板配置映射；结构不认识（不是映射、缺 ``agent_runner`` 段或其中的
        ``config``）时返回 ``None``，由 :meth:`RunnerSettings.from_config`
        继续回落到插件配置。
    """
    if not isinstance(config, Mapping):
        return None
    section = config.get(AGENT_RUNNER_SECTION)
    if not isinstance(section, Mapping):
        return None
    panel = section.get("config")
    return panel if isinstance(panel, Mapping) else None


# ── 事件 → 群键 / 工作目录 ─────────────────────────────────────────────
def _attr_str(event: T.Any, name: str) -> str:
    """取事件的字符串属性（非字符串一律当没取到）。"""
    value = getattr(event, name, None)
    return value.strip() if isinstance(value, str) else ""


def _call_str(event: T.Any, name: str) -> str:
    """调用事件的取字符串方法（方法缺席或返回值不是字符串时返回空串）。"""
    method = getattr(event, name, None)
    if not callable(method):
        return ""
    try:
        value = method()
    except Exception:  # noqa: BLE001 - 事件实现千奇百怪，取不到就当空
        return ""
    return value.strip() if isinstance(value, str) else ""


def object_key(event: T.Any) -> str:
    """群/会话的键：一个键对应一条连接。

    优先用 ``unified_msg_origin``（``平台:类型:会话``，天然区分平台与群/私聊），
    退路是群号，再退到会话号。
    """
    umo = _attr_str(event, "unified_msg_origin")
    if umo:
        return umo
    return (
        _call_str(event, "get_group_id")
        or _call_str(event, "get_session_id")
        or "default"
    )


def workspace_values(event: T.Any) -> dict[str, str]:
    """模板可用的占位符取值。

    ``{groupid}`` 在群里是群号；私聊没有群号，退化成会话号（保证每个会话仍有
    独立目录）。
    """
    session_id = _call_str(event, "get_session_id") or _attr_str(event, "session_id")
    group_id = _call_str(event, "get_group_id")
    return {
        "groupid": group_id or session_id or "default",
        "sessionid": session_id or group_id or "default",
        "platform": _call_str(event, "get_platform_name") or _attr_str(event, "platform")
        or "unknown",
    }


def render_workspace(template: str, event: T.Any) -> str:
    """渲染工作目录模板；不认识的占位符原样保留。"""
    text = (template or "").strip() or DEFAULT_WORKSPACE_TEMPLATE
    values = workspace_values(event)
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), text)


def workspace_root(template: str) -> str:
    """模板里「第一个占位符之前」的那段路径 —— 自动建目录时的根（必须已存在）。

    * ``/root/alkaid0/{groupid}`` → ``/root/alkaid0``（新群要建的是它下面的 ``987654``）；
    * ``/srv/{platform}/{groupid}`` → ``/srv``；
    * ``/srv/fixed``（不带占位符）→ ``/srv``（整个模板就是要建的目录，根是它的父目录）；
    * 模板以占位符开头（``{groupid}``）时推不出根，返回空串（这时不自动建目录）。
    """
    text = (template or "").strip() or DEFAULT_WORKSPACE_TEMPLATE
    match = _PLACEHOLDER.search(text)
    if match is not None:
        head = text[: match.start()]
    elif "/" in text:
        head = text.rsplit("/", 1)[0]
    else:
        return ""
    return head.rstrip("/")


def validate_rel_path(raw: str) -> str | None:
    """校验并规范化「相对模板根目录」的路径；非法时返回 ``None``。

    拒绝绝对路径（以 ``/`` 或 ``\\`` 开头）、盘符（``C:``）与含 ``..`` 的路径段；
    ``\\`` 当 ``/`` 处理，空段与 ``.`` 段丢掉；只由 ``.`` 组成时返回 ``"."``
    （表示根目录本身）。
    """
    text = (raw or "").strip()
    if not text or text.startswith("/") or text.startswith("\\"):
        return None
    if len(text) >= 2 and text[0].isalpha() and text[1] == ":":
        return None
    parts = [
        segment
        for segment in text.replace("\\", "/").split("/")
        if segment not in ("", ".")
    ]
    if any(segment == ".." for segment in parts):
        return None
    if not parts:
        return "."
    return "/".join(parts)


def resolve_cwd(template: str, rel: str | None, event: T.Any) -> str:
    """解析某个群**实际**用的工作目录（纯函数，便于自测）。

    * 没绑定（``rel`` 为空）→ 按模板渲染（``{groupid}`` 等）；
    * 绑定了（``/aworkdir``）→ 「模板根 + rel」，``.`` 表示根目录本身；
    * 模板以占位符开头（推不出根）时忽略绑定，仍按模板渲染。
    """
    if not rel:
        return render_workspace(template, event)
    root = workspace_root(template)
    if not root:
        return render_workspace(template, event)
    return root if rel == "." else f"{root}/{rel}"


def speaker_name(event: T.Any) -> str:
    """发言人的展示名（昵称取不到就退到发言者 ID）。

    只用来给攒批的提示词与日志标出处，不参与任何判断。
    """
    return _call_str(event, "get_sender_name") or _call_str(event, "get_sender_id")


def sender_id(event: T.Any) -> str:
    """发言者的 ID（注入 ``[Reminder: @昵称(id)]`` 用）；取不到就退到会话号。"""
    return _call_str(event, "get_sender_id") or _call_str(event, "get_session_id")


# ── 请求注入（AstrBot 的 provider_settings）─────────────────────────────
def read_provider_settings() -> dict[str, T.Any]:
    """读 AstrBot 全局配置里的 ``provider_settings``（拿不到时返回空字典）。

    走第三方执行器时内核**不会**替我们做这些注入（那是自带 Agent 那条路上的事），
    所以要照 :func:`apply_prompt_settings` 自己补一份。
    """
    try:
        from astrbot.core import astrbot_config

        settings = astrbot_config.get("provider_settings")
    except Exception as exc:  # noqa: BLE001 - 读不到就当这些开关全关着
        logger.debug("%s: 读取 provider_settings 失败：%s", LOG_PREFIX, exc)
        return {}
    return dict(settings) if isinstance(settings, Mapping) else {}


async def resolve_group_name(event: T.Any) -> str:
    """取群名：先看消息自带的，``N/A``/空时再用 ``event.get_group()`` 兜底。

    只服务于 ``group_name_display``；取不到就返回空串（调用方那一行就不加）。
    """
    group = getattr(getattr(event, "message_obj", None), "group", None)
    name = getattr(group, "group_name", None) if group is not None else None
    if isinstance(name, str) and name.strip() and name.strip() != "N/A":
        return name.strip()
    getter = getattr(event, "get_group", None)
    if callable(getter) and _call_str(event, "get_group_id"):
        try:
            fetched = await getter()
        except Exception as exc:  # noqa: BLE001 - 拉群信息失败就当没有名字
            logger.debug("%s: 取群名失败：%s", LOG_PREFIX, exc)
            fetched = None
        fallback = getattr(fetched, "group_name", None) if fetched is not None else None
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
    return name.strip() if isinstance(name, str) and name.strip() else ""


def apply_prompt_settings(
    prompt: str,
    config: T.Mapping[str, T.Any] | None,
    *,
    senders: T.Sequence[tuple[str, str]] = (),
    group_name: str = "",
) -> str:
    """按 ``provider_settings`` 给提示词加前缀与系统提醒（纯函数，便于自测）。

    复刻内核自带 Agent 那条路上的行为（老版 0528 也是这么补的）：

    * ``prompt_prefix``：带 ``{{prompt}}`` 就替换，否则拼在前面；
    * ``identifier``：每个发言人一行 ``[Reminder: @昵称(用户ID)]``（攒批时逐条给）；
    * ``group_name_display``：一行 ``Group name: 群名``。

    发送前会去掉提示词最前面的 ``/``（避免被 alkaid0 当成远端斜杠命令），去掉后
    按普通消息注入；去掉后为空则返回空串，配置不是映射时只做去斜杠。
    """
    body = strip_leading_slash(prompt)
    if not body:
        return ""
    if not isinstance(config, Mapping):
        return body

    prefix = config.get("prompt_prefix")
    if isinstance(prefix, str) and prefix:
        body = (
            prefix.replace("{{prompt}}", body)
            if "{{prompt}}" in prefix
            else f"{prefix}{body}"
        )

    reminders: list[str] = []
    if _as_bool(config.get("identifier"), False):
        for label, user_id in senders:
            if label or user_id:
                reminders.append(f"[Reminder: @{label}({user_id})]")
    if _as_bool(config.get("group_name_display"), False) and group_name:
        reminders.append(f"Group name: {group_name}")
    if reminders:
        extra = "\n".join(reminders)
        body = f"{body}\n{extra}" if body else extra
    return body


# ── 工具审批 ──────────────────────────────────────────────────────────
def _pending_permission(session: T.Any) -> tuple[int, dict] | None:
    """会话当前挂着的审批请求 ``(request_id, params)``；没有就 ``None``。

    只认 :class:`acp.client.ACPClient` 提供的查询方法；假客户端 / 别的实现
    没有这个方法时当作「没有挂起」（提示退化成通用文案，不影响主流程）。
    """
    getter = getattr(getattr(session, "client", None), "pending_permission", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - 查询失败不该挡审批提示
        return None


def permission_hint(session: T.Any) -> str:
    """等待审批时发到聊天里的一行提示（**不受** ``echo_events`` 开关影响）。

    alkaid0 在等审批时会推 ``state_update requires_action``；提示里带上工具名和
    ``/aapprove`` 的用法 —— 群友得知道怎么放行。审批请求本身由客户端挂起
    （``DEFERRED``），用户发 ``/aapprove`` 之后才回 ``allow_once``；不发批准、
    直接继续发消息就等于拒绝（alkaid0 侧自己的判定）。
    """
    pending = _pending_permission(session)
    title = ""
    if pending is not None:
        params = pending[1] or {}
        title = approval_mod.tool_name_of(params) or str(params.get("title") or "").strip()
    return (
        f"⏳ 工具审批：{title}（回复 /aapprove 允许）"
        if title
        else "⏳ 工具审批等待中（回复 /aapprove 允许）"
    )


def build_permission_handler(
    session: T.Any,
    label: str = "",
    *,
    settings_provider: T.Callable[[], T.Any] | None = None,
) -> T.Callable:
    """构造 ``session/request_permission`` 的处理函数。

    命中「配置白名单 / 本轮提示词授权 / 外部钩子授权」就立即回 ``allow_once``，
    否则维持旧行为：返回 :data:`acp.client.DEFERRED`，请求挂起、往聊天里提示，
    等群友发 ``/aapprove`` 才回包。

    Args:
        session: 审批请求落在哪个群会话上（日志 / 授权缓存在它身上）。
        label: 日志里用来辨认群的标签（缺省用 ``session.key``）。
        settings_provider: 返回 :class:`RunnerSettings`（或同名字段映射）的函数；
            每次审批现读，配置改了下一条审批就生效。

    Returns:
        一个 ``async (params) -> result | DEFERRED`` 的协程函数，直接塞给
        :attr:`acp.client.ACPClient.permission_handler` 即可。
    """
    return approval_mod.build_permission_handler(
        session, label=label, settings_provider=settings_provider
    )


# ── 过程消息回显 ──────────────────────────────────────────────────────
#: ``tool_call_update.status`` 的中文说法。
_TOOL_STATUS_ZH = {
    acp.TOOL_STATUS_PENDING: "待执行",
    acp.TOOL_STATUS_COMPLETED: "已完成",
    acp.TOOL_STATUS_CANCELLED: "已取消",
}


def _calling_name(update: dict) -> str:
    """取 ``alk.cxykevin.top/calling_info`` 里的**真实工具名**；没有就返回空串。"""
    content = update.get("content")
    items = list(content) if isinstance(content, (list, tuple)) else [content]
    for item in items:
        if not isinstance(item, dict) or item.get("type") != acp.KEY_CALLING_INFO:
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _tool_title(update: dict) -> str:
    """工具名的兜底展示串：``title``（形如 ``[Call edit]``）→ ``kind`` → 空串。"""
    for key in ("title", "kind"):
        value = update.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class ProgressEcho:
    """把 ``session/update`` 里的过程消息渲染成「给聊天看的一行文本」。

    只挑值得让群里看见的事件，并且自带去重/限流（同一轮里复用一个实例）：

    * ``tool_call_update``：``streaming`` 预览跳过、状态没变也跳过，
      于是同一条调用只在「待执行 / 已完成 / 已取消」各出现一次
      → ``🔧 调用 edit（已完成）``；
    * 子 Agent 切换：``agent_message`` / ``agent_thought`` 上带
      ``alk.cxykevin.top/agent_status`` 字段（值就是子 Agent 的 ID），变了才说一声
      → ``🤖 子 Agent：explore``；
    * 后台任务结束（``alk.cxykevin.top/shell_stop``）
      → ``✅ 后台任务 @temp/run/3 结束：npm test``。

    工具审批（``state_update requires_action``）不在这里处理：那是**必要提示**，
    不受 ``echo_events`` 开关影响，由调用方用 :func:`permission_hint` 单独发。

    正文（``agent_message*``）与思考（``agent_thought*``）一律返回 ``None`` ——
    它们由调用方按 messageId 累积；``user_message*`` 也不回显：那是服务端把**我们
    自己刚发出去**的提示词广播回来（协议回声），发到群里就重复了。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._tool_status: dict[str, str] = {}
        self._tool_names: dict[str, str] = {}
        self._subagent = ""

    def describe(self, update: dict) -> str | None:
        """给一行回显文本；没什么值得说的就返回 ``None``。"""
        if not self.enabled or not isinstance(update, dict):
            return None

        subagent = update.get(acp.KEY_AGENT_STATUS)
        if isinstance(subagent, str) and subagent.strip() and subagent != self._subagent:
            self._subagent = subagent
            return f"🤖 子 Agent：{subagent}"

        kind = update.get("sessionUpdate", "")
        if kind == acp.UPDATE_TOOL_CALL:
            return self._describe_tool(update)
        if kind == acp.UPDATE_SHELL_STOP:
            return self._describe_shell_stop(update)
        return None

    def _describe_tool(self, update: dict) -> str | None:
        """工具调用：只在状态真正变化时给一行。

        工具名往往只出现在首次/流式的完整快照里（``streaming`` 预览带着
        ``calling_info``，收尾的更新可能只剩 ``title``），所以真名见到就记下来，
        之后再优先用它。
        """
        tool_call_id = str(update.get("toolCallId") or "")
        real_name = _calling_name(update)
        if tool_call_id and real_name:
            self._tool_names[tool_call_id] = real_name
        status = str(update.get("status") or "")
        if status == acp.TOOL_STATUS_STREAMING:
            return None
        if tool_call_id and self._tool_status.get(tool_call_id) == status:
            return None
        if tool_call_id:
            self._tool_status[tool_call_id] = status
        name = (
            (self._tool_names.get(tool_call_id, "") if tool_call_id else "")
            or real_name
            or _tool_title(update)
        )
        state = _TOOL_STATUS_ZH.get(status) or status or "更新"
        line = f"🔧 调用 {name or '工具'}（{state}）"
        terminal_id = update.get(acp.KEY_TERMINAL_ID)
        if isinstance(terminal_id, str) and terminal_id:
            line += f" → {terminal_id}"
        return line

    def _describe_shell_stop(self, update: dict) -> str | None:
        """后台 run 任务结束：带上群号/命令，让群里知道跑的是什么。"""
        run_id = str(update.get(acp.SHELL_STOP_RUN_ID) or "")
        command = str(update.get(acp.SHELL_STOP_COMMAND) or "").strip()
        if update.get(acp.SHELL_STOP_KILLED):
            icon, state = "🛑", "已终止"
        elif update.get(acp.SHELL_STOP_SUCCESS):
            icon, state = "✅", "结束"
        else:
            icon, state = "❌", "失败"
        line = f"{icon} 后台任务 {run_id or '?'} {state}"
        return f"{line}：{command}" if command else line


class _TurnTranscript:
    """一轮对话里按**到达顺序**攒正文与过程消息，最后拼成一条回复。

    正文按 ``messageId`` 合并（chunk 追加、整消息 upsert 覆盖），过程消息各自一行；
    两者共用同一个列表，所以「说一句 → 调工具 → 再说一句」的顺序会被保留。
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._text_index: dict[str, int] = {}

    def add_text(self, message_id: str, text: str, *, replace: bool = False) -> None:
        """累积某条正文；``replace=True`` 表示整消息 upsert（覆盖该条已攒的文本）。"""
        index = self._text_index.get(message_id)
        if index is None:
            self._items.append({"kind": "text", "text": text})
            self._text_index[message_id] = len(self._items) - 1
        elif replace:
            self._items[index]["text"] = text
        else:
            self._items[index]["text"] += text

    def add_progress(self, text: str) -> None:
        """追加一行过程消息。"""
        self._items.append({"kind": "progress", "text": text})

    def text(self) -> str:
        """只要正文（按到达顺序拼接）。"""
        return "\n\n".join(
            item["text"].strip()
            for item in self._items
            if item["kind"] == "text" and item["text"].strip()
        )

    def render(self) -> str:
        """正文 + 过程消息拼成一条回复：正文各自成段，连续的过程行挤在一起。"""
        blocks: list[str] = []
        progress: list[str] = []

        def _flush() -> None:
            if progress:
                blocks.append("\n".join(progress))
                progress.clear()

        for item in self._items:
            if item["kind"] == "text":
                _flush()
                text = item["text"].strip()
                if text:
                    blocks.append(text)
            else:
                progress.append(item["text"])
        _flush()
        return "\n\n".join(blocks).strip()


#: 公开别名：唤醒钩子把 cron 回合的输出转发到群里时复用同一套拼装逻辑。
TurnTranscript = _TurnTranscript


# ── 远端斜杠命令（/acmd）──────────────────────────────────────────────
async def run_slash_command(
    session: GroupSession,
    command_text: str,
    *,
    timeout: float = float(DEFAULT_TURN_TIMEOUT_SEC),
    echo_events: bool = False,
) -> str:
    """把一条斜杠命令发给 alkaid0，攒回它的输出（``/acmd`` 用）。

    命令轮和普通对话走同一条 ACP 通道：``session/prompt`` 立即确认，命令的回复
    （带 ``cmd_`` messageId 的 ``agent_message_chunk``）与状态都靠 ``session/update``
    推送，本轮结束同样以 ``state_update idle`` 为准。服务端不认这条命令时会先把
    idle 标成 ``refusal``（带 ``invalid command``），再回一个 JSON-RPC 错误，因此
    调用方接口是「出文本就返回文本，出错误就抛异常」。

    全程在该群的 :attr:`GroupSession.lock` 里跑：不会和正在跑的一轮打架（要等它
    先结束），进行中也不会被空闲回收掐掉。

    Args:
        session: 目标群的连接（URL / 工作目录由调用方按配置渲染好）。
        command_text: 要发的命令文本，形如 ``/compress``（缺 ``/`` 由调用方补）。
        timeout: 最多等多久（秒）。
        echo_events: 是否把过程消息（工具调用 / 子 Agent 等）也拼进返回的文本里。

    Returns:
        命令的回复文本；命令没有输出时为空串。

    Raises:
        ACPError: 服务端拒绝这条命令、连接不可用，或等不到本轮结束。
    """
    session_id = await session.ensure_session()
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    def _on_notification(method: str, params: dict) -> None:
        queue.put_nowait((method, params))

    unsubscribe = session.client.subscribe(_on_notification)
    transcript = _TurnTranscript()
    echo = ProgressEcho(enabled=echo_events)
    stop_reason = ""
    error_msg = ""
    deadline = time.monotonic() + timeout
    #: 审批提示去重（同一条请求只提示一次；没有挂起时只发一次通用提示）。
    approval_seen: set[int] = set()
    approval_generic = False

    try:
        async with session.lock:
            session.touch()
            await session.client.session_prompt(
                session_id, command_text, timeout=timeout
            )

            while True:
                if not session.client.connected:
                    # 服务端重启 / 网络断了：没有 idle 可等，别干耗到超时
                    raise ACPConnectionError(
                        f"群 {session.key}：ACP 连接已断开，命令没跑完"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ACPError(f"群 {session.key}：等命令结束超时（{timeout:g}s）")
                try:
                    _method, params = await asyncio.wait_for(
                        queue.get(), timeout=min(0.5, remaining)
                    )
                except asyncio.TimeoutError:
                    continue

                update = params.get("update")
                if not isinstance(update, dict):
                    continue
                kind = update.get("sessionUpdate", "")

                highlight = echo.describe(update)
                if highlight is not None:
                    transcript.add_progress(highlight)

                if kind in (acp.UPDATE_AGENT_MESSAGE_CHUNK, acp.UPDATE_AGENT_MESSAGE):
                    text = acp.extract_text(update.get("content"))
                    if not text:
                        continue
                    transcript.add_text(
                        str(update.get("messageId") or "msg_0"),
                        text,
                        replace=(kind == acp.UPDATE_AGENT_MESSAGE),
                    )
                elif kind == acp.UPDATE_STATE and update.get("state") == acp.STATE_IDLE:
                    stop_reason = str(update.get("stopReason") or "")
                    error_msg = str(update.get(acp.KEY_ERROR_MSG) or "")
                    break
                elif (
                    kind == acp.UPDATE_STATE
                    and update.get("state") == acp.STATE_REQUIRES_ACTION
                ):
                    # 命令轮也可能等审批：把提示拼进命令输出（用户发 /aapprove 放行）
                    pending = _pending_permission(session)
                    if pending is not None:
                        if pending[0] in approval_seen:
                            continue
                        approval_seen.add(pending[0])
                    elif approval_generic:
                        continue
                    else:
                        approval_generic = True
                    transcript.add_progress(permission_hint(session))
    finally:
        unsubscribe()

    # 只有 refusal 才算失败：手动摘要这类异步命令收尾时服务端报的是 cancelled
    # （ReasonMap 把「用户触发的手动操作结束」也映射成 cancelled），不是出错。
    if not error_msg and stop_reason == acp.STOP_REASON_REFUSAL:
        error_msg = "Alkaid0 拒绝了这条命令"

    body = transcript.render()
    if error_msg:
        logger.warning(
            "%s: 群 %s 命令 %s 出错：%s", LOG_PREFIX, session.key, command_text, error_msg
        )
        if not body:
            raise ACPError(error_msg)
        body = f"{body}\n\n⚠️ {error_msg}"

    logger.info(
        "%s: 群 %s 命令 %s 结束（stopReason=%s，输出 %d 字）",
        LOG_PREFIX,
        session.key,
        command_text,
        stop_reason or "未知",
        len(body),
    )
    return body


# ── Runner ────────────────────────────────────────────────────────────
class Alkaid0Runner(BaseAgentRunner[AstrAgentContext]):
    """把一轮 AstrBot 对话映射成一次 ACP prompt turn。

    一轮的边界由 :class:`~turn_queue.TurnQueue` 把关：同一个群里**同时只跑一轮**，
    这一轮期间别人插的话先排队，结束后攒成一批交给下一个人接着跑（见 :mod:`turn_queue`）。

    Args:
        pool: 群 → 连接的表（插件级共享，生命周期由插件管理）。
        plugin_config: 插件配置（``_conf_schema.json`` 的内容），作为面板值的兜底。
        turn_queue: 群内并发闸门（插件级共享，包含排队插话的落库与恢复）；不传就自己起一个
            只在内存里排队的（不落库）。
        settings_provider: 读 AstrBot ``provider_settings`` 的函数（注入提示前缀 /
            用户识别 / 群名）；不传就用 :func:`read_provider_settings`，自测时可注入假配置。
        context: 本插件的 AstrBot ``Context``，用来找同装的插件（目前只用于
            「表情包管理器」联动）；不传就退到 ``run_context.context.context``。
    """

    def __init__(
        self,
        pool: SessionPool,
        plugin_config: Mapping | None = None,
        turn_queue: TurnQueue | None = None,
        settings_provider: T.Callable[[], Mapping] | None = None,
        context: T.Any = None,
    ) -> None:
        self._pool = pool
        self._plugin_config: dict = dict(plugin_config or {})
        self._turn_queue = turn_queue if turn_queue is not None else TurnQueue()
        self._settings_provider = settings_provider or read_provider_settings
        self._context = context
        self._settings = RunnerSettings.from_config(None, self._plugin_config)

        self.request: ProviderRequest | None = None
        """本轮请求（内核构造）。"""
        self.run_context: ContextWrapper[AstrAgentContext] | None = None
        """运行时上下文（可从 ``context.event`` 拿到消息事件）。"""
        self.agent_hooks: BaseAgentRunHooks[AstrAgentContext] | None = None
        """Agent 钩子。"""
        self.streaming: bool = False
        """本轮是否走流式输出。"""
        self.final_llm_resp: LLMResponse | None = None
        """最终结果，供内核聚合。"""
        self._state = AgentState.IDLE

    # ── 内核接口 ─────────────────────────────────────────
    async def reset(
        self,
        request: ProviderRequest | None = None,
        run_context: ContextWrapper[AstrAgentContext] | None = None,
        agent_hooks: BaseAgentRunHooks[AstrAgentContext] | None = None,
        provider_config: Mapping[str, T.Any] | None = None,
        **kwargs: T.Any,
    ) -> None:
        """重置本轮状态（内核在每次请求前调用）。"""
        self.request = request
        self.run_context = run_context
        self.agent_hooks = agent_hooks
        self.streaming = bool(kwargs.get("streaming", False))
        self._settings = RunnerSettings.from_config(provider_config, self._plugin_config)
        # 面板上的空闲阈值同步给连接池（后台回收任务读它）
        self._pool.set_idle_timeout(self._settings.idle_timeout_sec)
        self.final_llm_resp = None
        self._state = AgentState.IDLE

    async def step(self) -> AsyncGenerator[AgentResponse, None]:
        """单步执行：本执行器一轮走完，直接委托给 :meth:`step_until_done`。"""
        async for response in self.step_until_done():
            yield response

    async def step_until_done(
        self, max_step: int = 128
    ) -> AsyncGenerator[AgentResponse, None]:
        """执行到本轮结束（内核只调这个方法）。

        先在 :attr:`_turn_queue` 的闸门里排队：群里已经有一轮在跑就等着；等到的那一批由
        :meth:`_run_turn` 发出去，被并进别人那一批的（``ACTION_ABSORBED``）什么都不发 ——
        群里不会为一句插话多出一条回复。闸门靠 ``finally`` 里的
        :meth:`~turn_queue.TurnQueue.leave` 让给下一个带队者，这里不能漏。
        """
        if self._state in (AgentState.DONE, AgentState.ERROR):
            return

        self._transition_state(AgentState.RUNNING)
        await self._announce_begin()

        # 发到 alkaid0 之前去掉消息开头的斜杠：否则会被当成远端斜杠命令
        # （``/hello`` → ``hello``；``/acmd`` 走的命令通道不经过这里）。
        prompt_text = strip_leading_slash(getattr(self.request, "prompt", "") or "")
        event = self._event()
        if not prompt_text:
            async for response in self._fail("提示词为空，没有内容可以发给 Alkaid0"):
                yield response
            return
        if event is None:
            async for response in self._fail("拿不到消息事件（run_context 不完整）"):
                yield response
            return

        key = object_key(event)
        cwd = self._cwd_for(key, event)

        # 群内闸门：一个群同时只跑一轮；这一轮期间别人插的话先排队，结束后攒成一批
        slot = await self._turn_queue.enter(
            key,
            prompt_text,
            speaker=speaker_name(event),
            sender_id=sender_id(event),
        )
        if slot.action == ACTION_ABSORBED:
            # 这一条已经并进别人那一批了：不占远端、也不回话。
            # 闸门在**带队的那一轮**手里，这里千万不能替它 leave（那会把门放开，
            # 让第三条消息跟正在跑的那轮并排跑）。
            logger.info(
                "%s: 群 %s 这条消息已并进当前这一批里，不单独回话", LOG_PREFIX, key
            )
            await self._finish_without_reply()
            return

        try:
            if slot.action == ACTION_MERGED:
                logger.info(
                    "%s: 群 %s 带着 %d 条插话开跑（%s）",
                    LOG_PREFIX,
                    key,
                    len(slot.speakers),
                    "、".join(slot.speakers) or "匿名",
                )
            async for response in self._run_gated(slot, key, cwd, event):
                yield response
        finally:
            await self._turn_queue.leave(key)

    def done(self) -> bool:
        """本轮是否已结束。"""
        return self._state in (AgentState.DONE, AgentState.ERROR)

    def get_final_llm_resp(self) -> LLMResponse | None:
        """返回最终结果，供内核的结果聚合器使用。"""
        return self.final_llm_resp

    async def close(self) -> None:
        """内核在请求结束时调用。

        **不**关闭 WebSocket —— 连接归连接池所有，留给该群的下一条消息复用，
        空闲回收由 :meth:`SessionPool.reap_idle` 负责。这里只放下本轮的引用。
        """
        self.request = None
        self.run_context = None
        self.agent_hooks = None

    # ── 内部 ─────────────────────────────────────────────
    def _event(self) -> T.Any:
        """从运行上下文里取出消息事件。"""
        context = getattr(self.run_context, "context", None)
        if context is None:
            return None
        return getattr(context, "event", None)

    def _plugin_context(self) -> T.Any:
        """本插件的 AstrBot ``Context``（找同装插件用，目前只有表情包联动）。

        优先用构造时传进来的那份；没传就顺着 ``run_context`` 往上找 ——
        ``AstrAgentContext`` 上挂着插件 ``Context``（``.context``）。自测里用的
        假上下文没有这一层，取不到就是 ``None``（等于「没装任何联动插件」）。
        """
        if self._context is not None:
            return self._context
        agent_context = getattr(self.run_context, "context", None)
        return getattr(agent_context, "context", None)

    def _cwd_for(self, key: str, event: T.Any) -> str:
        """这个群这一轮用的工作目录：绑过 ``/aworkdir`` 就用绑定，否则按模板渲染。"""
        store = getattr(self._pool, "store", None)
        rel = store.get_workdir(key) if store is not None else None
        return resolve_cwd(self._settings.workspace_template, rel, event)

    async def _decorated_prompt(self, slot: TurnSlot, event: T.Any) -> str:
        """按 ``provider_settings`` 给这一轮的提示词加前缀与系统提醒。

        读配置 / 拉群名失败都不该挡住消息（照老版 0528：注入是锦上添花）。
        """
        try:
            config = self._settings_provider()
        except Exception as exc:  # noqa: BLE001 - 注入失败照常发原话
            logger.warning("%s: 读取 provider_settings 失败：%s", LOG_PREFIX, exc)
            return slot.text
        if not config:
            return slot.text
        senders = slot.senders or ((speaker_name(event), sender_id(event)),)
        group_name = ""
        if _as_bool(config.get("group_name_display"), False):
            group_name = await resolve_group_name(event)
        return apply_prompt_settings(
            slot.text, config, senders=senders, group_name=group_name
        )

    async def _run_gated(
        self, slot: TurnSlot, key: str, cwd: str, event: T.Any
    ) -> AsyncGenerator[AgentResponse, None]:
        """闸门里的这一轮：开会话（复用）→ 发提示词 → 消费通知。"""
        prompt_text = await self._decorated_prompt(slot, event)
        try:
            session = await self._pool.acquire(
                key,
                url=self._settings.url,
                cwd=cwd,
                cwd_root=workspace_root(self._settings.workspace_template),
            )
        except Exception as exc:  # noqa: BLE001 - 连接问题统一上报给用户
            logger.error("%s: 连接 Alkaid0 失败（群 %s）：%s", LOG_PREFIX, key, exc)
            async for response in self._fail(f"连接 Alkaid0 失败：{exc}"):
                yield response
            return

        session.client.set_permission_handler(
            build_permission_handler(
                session, label=key, settings_provider=lambda: self._settings
            )
        )

        try:
            async with session.lock:
                async for response in self._run_turn(session, prompt_text, event):
                    yield response
        except Exception as exc:  # noqa: BLE001 - 执行期问题同样上报
            logger.error(
                "%s: 群 %s 执行出错：%s", LOG_PREFIX, key, exc, exc_info=True
            )
            async for response in self._fail(f"Alkaid0 执行出错：{exc}"):
                yield response
            return
        finally:
            # 无论成败都算「这个群刚活动过」，重置空闲计时
            session.touch()

    async def _finish_without_reply(self) -> None:
        """本轮什么都不发地结束（这条消息已经被并进别人那一批时用）。

        内核的结果聚合器把「最终结果为空」当作「Runner 没返回结果」，会补一条兜底错误
        发到群里；所以这里给一条**只有组件、没有内容**的消息链（``Plain("")``）：内核
        的响应阶段会判它「空消息」直接跳过 —— 群里既不会多出一条空消息，也不会冒出
        那句兜底错误。
        """
        self.final_llm_resp = LLMResponse(
            role="assistant", result_chain=MessageChain().message("")
        )
        self._transition_state(AgentState.DONE)
        await self._announce_done()

    async def _run_turn(
        self, session: GroupSession, prompt_text: str, event: T.Any
    ) -> AsyncGenerator[AgentResponse, None]:
        """提交提示词并消费通知，直到本轮 ``state_update idle``。

        表情包管理器（``meme_manager``）装了并启用着时，这一轮的正文会先交给它处理：
        流式增量里先把 ``&&分类键&&`` 挡掉，收尾时再用它返回的干净正文和选好的图片
        拼回复（见 :mod:`meme_bridge`）。
        """
        settings = self._settings
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        #: 表情包联动（没装 / 没启用时为 ``None``，整条路径不生效）
        meme = meme_bridge.bridge_for(self._plugin_context(), event)
        #: at 解析：只在 OneBot 上游的群聊里启用（私聊 / 其它平台保持原文）
        at_filter = (
            at_parse.AtFilter()
            if settings.at_parse_enable and at_parse.supported(event)
            else None
        )
        #: CQ 码解析：所有平台都会删掉 CQ 码，只有 OneBot 才构造组件。
        cq_filter = (
            cq_parse.CqFilter(components=cq_parse.platform_supported(event))
            if settings.cq_parse_enable
            else None
        )

        def _expand_at(
            parts: list[at_parse.Part], *, streaming: bool
        ) -> list[at_parse.Part]:
            """文本片段再过一遍 at 解析（流式走 AtFilter，最后走纯函数）。"""
            out: list[at_parse.Part] = []
            for kind, value in parts:
                if kind != "text":
                    out.append((kind, value))
                elif at_filter is None:
                    if value:
                        out.append((kind, value))
                elif streaming:
                    out.extend(at_filter.feed(value))
                else:
                    out.extend(at_parse.parts_of(value))
            return out

        def _stream_parts(raw: str) -> list[at_parse.Part]:
            """流式增量：CQ 过滤 → at 解析。"""
            cq_parts = (
                cq_filter.feed(raw)
                if cq_filter is not None
                else ([("text", raw)] if raw else [])
            )
            return _expand_at(cq_parts, streaming=True)

        def _flush_parts(raw: str) -> list[at_parse.Part]:
            """收尾：吐出 CQ 与 at 两个过滤器压着的内容。"""
            if cq_filter is not None:
                cq_parts = cq_filter.feed(raw) + cq_filter.finish()
            else:
                cq_parts = [("text", raw)] if raw else []
            parts = _expand_at(cq_parts, streaming=True)
            if at_filter is not None:
                parts.extend(at_filter.finish())
            return parts

        def _on_notification(method: str, params: dict) -> None:
            queue.put_nowait((method, params))

        unsubscribe = session.client.subscribe(_on_notification)
        transcript = _TurnTranscript()
        echo = ProgressEcho(enabled=settings.echo_events)
        thoughts: dict[str, str] = {}
        thought_order: list[str] = []
        stop_reason = ""
        error_msg = ""
        deadline = time.monotonic() + settings.turn_timeout_sec
        #: 已经提示过的审批请求（同一条只发一次）；没有挂起请求时只发一次通用提示。
        approval_seen: set[int] = set()
        approval_generic = False
        #: 自动批准的序号起点：新自动批准一次，这个数就会变大。
        approval_seq = int(getattr(session, "approval_seq", 0))
        prompt_text, prompt_grant = approval_mod.extract_prompt_grant(
            prompt_text,
            enabled=settings.auto_approve_enable and settings.auto_approve_prompt,
        )
        session.set_prompt_grant(prompt_grant)
        if not prompt_text:
            # 只写了授权标记：不能把空提示词发给 alkaid0；给一句无害的继续语。
            prompt_text = "继续"

        def _accumulate(target: dict[str, str], seq: list[str], mid: str, text: str) -> None:
            """按 messageId 累积思考文本（先记顺序，chunk 追加）。"""
            if mid not in target:
                seq.append(mid)
                target[mid] = ""
            target[mid] += text

        try:
            # 上一轮没批准就继续发消息 = 拒绝（alkaid0 侧自己判定）；客户端这边
            # 只把挂着的审批记录丢掉，不必也不应该再回包。
            clear_deferred = getattr(session.client, "clear_deferred", None)
            if callable(clear_deferred):
                clear_deferred()
            await session.client.session_prompt(
                session.session_id, prompt_text, timeout=settings.turn_timeout_sec
            )
            logger.info(
                "%s: 群 %s 已提交 prompt（%d 字），等待 Alkaid0 响应",
                LOG_PREFIX,
                session.key,
                len(prompt_text),
            )

            while True:
                if not session.client.connected:
                    # 服务端重启 / 网络断了：这里没有 idle 事件可等，别干等到超时
                    raise ACPConnectionError(
                        f"群 {session.key}：ACP 连接已断开，本轮中断"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ACPError(
                        f"等待 Alkaid0 响应超时（{settings.turn_timeout_sec:g}s）"
                    )
                try:
                    _method, params = await asyncio.wait_for(
                        queue.get(), timeout=min(1.0, remaining)
                    )
                except asyncio.TimeoutError:
                    continue

                update = params.get("update")
                if not isinstance(update, dict):
                    continue
                kind = update.get("sessionUpdate", "")

                # 过程消息回显（工具调用 / 子 Agent / 后台任务 / 等待审批）
                highlight = echo.describe(update)
                if highlight is not None:
                    transcript.add_progress(highlight)
                    if self.streaming:
                        yield AgentResponse(
                            type="streaming_delta",
                            data=AgentResponseData(
                                chain=MessageChain().message(f"\n{highlight}\n")
                            ),
                        )

                if kind in (acp.UPDATE_AGENT_MESSAGE_CHUNK, acp.UPDATE_AGENT_MESSAGE):
                    text = acp.extract_text(update.get("content"))
                    if not text:
                        continue
                    message_id = str(update.get("messageId") or "msg_0")
                    # 整消息 upsert 直接覆盖（重放同一条不会翻倍），chunk 追加
                    transcript.add_text(
                        message_id, text, replace=(kind == acp.UPDATE_AGENT_MESSAGE)
                    )
                    if kind == acp.UPDATE_AGENT_MESSAGE_CHUNK and self.streaming:
                        # 表情包标记边收边挡：群里只看到正文，标记交给 meme_manager。
                        # 整消息 upsert 不走增量（同一段正文会被重复发一次）。
                        visible = meme.filter_delta(text) if meme is not None else text
                        # CQ / at 解析同样边收边做：两个过滤器都会压住没收完的部分。
                        parts = _stream_parts(visible)
                        delta_chain = at_parse.parts_to_chain(parts)
                        if delta_chain.chain:
                            yield AgentResponse(
                                type="streaming_delta",
                                data=AgentResponseData(chain=delta_chain),
                            )
                elif kind in (acp.UPDATE_AGENT_THOUGHT_CHUNK, acp.UPDATE_AGENT_THOUGHT):
                    text = acp.extract_text(update.get("content"))
                    if not text:
                        continue
                    message_id = str(update.get("messageId") or "thought_0")
                    if kind == acp.UPDATE_AGENT_THOUGHT:
                        if message_id not in thoughts:
                            thought_order.append(message_id)
                        thoughts[message_id] = text
                    else:
                        _accumulate(thoughts, thought_order, message_id, text)
                        if self.streaming and settings.show_thought:
                            yield AgentResponse(
                                type="streaming_delta",
                                data=AgentResponseData(
                                    chain=MessageChain(type="reasoning").message(text)
                                ),
                            )
                elif kind == acp.UPDATE_STATE:
                    state = update.get("state", "")
                    if state == acp.STATE_IDLE:
                        stop_reason = str(update.get("stopReason") or "")
                        error_msg = str(update.get(acp.KEY_ERROR_MSG) or "")
                        break
                    if state == acp.STATE_REQUIRES_ACTION:
                        # 服务和客户端是两个任务：requires_action 先到、审批请求
                        # 后到。等一小会儿再判定，免得自动批准了还发「等审批」。
                        req_state, pending = await approval_mod.wait_permission_state(
                            session, approval_seq
                        )
                        if req_state == "auto":
                            approval_seq = int(
                                getattr(session, "approval_seq", approval_seq)
                            )
                            logger.info(
                                "%s: 群 %s 本轮工具审批已自动放行，不发等待提示",
                                LOG_PREFIX,
                                session.key,
                            )
                            continue
                        if req_state == "manual" and pending is not None:
                            if pending[0] in approval_seen:
                                continue
                            approval_seen.add(pending[0])
                        elif req_state == "unknown":
                            if approval_generic:
                                continue
                            approval_generic = True
                        else:
                            continue
                        hint = permission_hint(session)
                        logger.info("%s: 群 %s %s", LOG_PREFIX, session.key, hint)
                        transcript.add_progress(hint)
                        if self.streaming:
                            yield AgentResponse(
                                type="streaming_delta",
                                data=AgentResponseData(
                                    chain=MessageChain().message(f"\n{hint}\n")
                                ),
                            )
                elif kind == acp.UPDATE_TOOL_CALL:
                    logger.debug(
                        "%s: 群 %s 工具调用 %s（%s）",
                        LOG_PREFIX,
                        session.key,
                        update.get("title", ""),
                        update.get("status", ""),
                    )
                elif kind == acp.UPDATE_USAGE:
                    # 用量由会话的常驻监听缓存（/actx 读它），这里只留一行便于排查
                    logger.debug(
                        "%s: 群 %s 上下文用量 %s",
                        LOG_PREFIX,
                        session.key,
                        acp.usage_of(update),
                    )
                elif kind in (acp.UPDATE_TERMINAL, acp.UPDATE_SHELL_STOP):
                    # 终端增量/快照留给后续功能；shell_stop 的收尾已由 ProgressEcho 回显
                    logger.debug("%s: 群 %s 终端事件 %s", LOG_PREFIX, session.key, kind)
                else:
                    logger.debug("%s: 群 %s 忽略事件 %s", LOG_PREFIX, session.key, kind)
        finally:
            session.clear_prompt_grant()
            unsubscribe()

        if stop_reason == acp.STOP_REASON_CANCELLED:
            # 这一轮被中止了（``/astop`` 发的 session/cancel，或服务端自己取消）：
            # 明确说一声，免得群里的半截回复看起来像莫名断掉。这是**状态**而不是过程
            # 消息，所以不受 echo_events 开关影响。
            transcript.add_progress("🛑 本轮已终止")

        tail = ""
        if meme is not None:
            # 流式收尾：清理器可能还压着半截文本（正好停在 ``&&`` 中间，或者它的
            # 解析器按行缓冲），这一轮已经结束，把剩下的吐出去。
            tail = meme.flush()
            if tail:
                logger.debug(
                    "%s: 群 %s 流式正文补发尾巴 %d 字", LOG_PREFIX, session.key, len(tail)
                )
        # CQ / at 清理器也可能压着半截：收尾时一并吐出。
        tail_parts = _flush_parts(tail)
        if tail_parts and self.streaming:
            tail_chain = at_parse.parts_to_chain(tail_parts)
            if tail_chain.chain:
                yield AgentResponse(
                    type="streaming_delta",
                    data=AgentResponseData(chain=tail_chain),
                )

        body = transcript.text()
        rendered = transcript.render()
        thought = "".join(thoughts[mid] for mid in thought_order).strip()

        if error_msg:
            logger.error(
                "%s: 群 %s 本轮出错：%s（stopReason=%s）",
                LOG_PREFIX,
                session.key,
                error_msg,
                stop_reason,
            )
            detail = f"Alkaid0 报错：{error_msg}"
            if body:
                detail = f"{body}\n\n{detail}"
            async for response in self._fail(detail):
                yield response
            return

        chain = MessageChain()
        if thought and settings.show_thought:
            chain = MessageChain(type="reasoning").message(thought)

        # 表情包标记由本插件自己解析：正文减掉 ``&&分类键&&`` 就是回复（原样发，
        # 不经过 meme_manager 的清洗规则），分类键直接交给它的底层接口取图。
        reply_text = rendered
        prepared: dict | None = None
        if meme is not None and rendered:
            reply_text, tags = meme.parse(rendered)
            if tags:
                prepared = await meme.build(tags)
        if reply_text:
            if cq_filter is not None or at_filter is not None:
                cq_parts = (
                    cq_parse.parts_of(
                        reply_text, components=cq_filter.components
                    )
                    if cq_filter is not None
                    else [("text", reply_text)]
                )
                at_parse.parts_to_chain(
                    _expand_at(cq_parts, streaming=False), chain
                )
                if not chain.chain:
                    # 正文全被 CQ 解析删掉了：给一个空组件让内核跳过，别发兜底错误
                    chain.message("")
            else:
                chain.message(reply_text)

        if prepared is not None:
            await self._deliver_memes(meme, prepared, has_text=bool(reply_text))

        self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
        self._transition_state(AgentState.DONE)
        await self._announce_done()
        logger.info(
            "%s: 群 %s 本轮结束（stopReason=%s，输出 %d 字）",
            LOG_PREFIX,
            session.key,
            stop_reason or "未知",
            len(rendered),
        )
        yield AgentResponse(type="llm_result", data=AgentResponseData(chain=chain))

    async def _deliver_memes(
        self, meme: T.Any, prepared: dict, *, has_text: bool
    ) -> None:
        """把表情包管理器选好的图片送出去。

        真流式时正文已经边收边发了，这一轮不会再有正文消息，图片只能自己发；纯表情
        回复同理（内核那边是空消息链，挂起的图没人触发）。其余情况交给它的
        ``after_message_sent``，图片跟在正文后面单独发出，临时文件也由它清理。
        """
        if not prepared.get("images"):
            return
        if self.streaming or not has_text or not meme.defer_images(prepared):
            await meme.send_images(prepared)

    async def _fail(self, text: str) -> AsyncGenerator[AgentResponse, None]:
        """统一的失败路径：标记出错、记下最终结果、推一个 ``err`` 响应。"""
        self._transition_state(AgentState.ERROR)
        chain = MessageChain().message(text)
        self.final_llm_resp = LLMResponse(role="err", completion_text=text)
        yield AgentResponse(type="err", data=AgentResponseData(chain=chain))

    async def _announce_begin(self) -> None:
        """触发 on_agent_begin 钩子（失败不影响主流程）。"""
        if self.agent_hooks is None or self.run_context is None:
            return
        try:
            await self.agent_hooks.on_agent_begin(self.run_context)
        except Exception as exc:  # noqa: BLE001 - 钩子失败不该挡回复
            logger.warning("%s: on_agent_begin 钩子失败：%s", LOG_PREFIX, exc)

    async def _announce_done(self) -> None:
        """触发 on_agent_done 钩子（失败不影响主流程）。"""
        if (
            self.agent_hooks is None
            or self.run_context is None
            or self.final_llm_resp is None
        ):
            return
        try:
            await self.agent_hooks.on_agent_done(self.run_context, self.final_llm_resp)
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.warning("%s: on_agent_done 钩子失败：%s", LOG_PREFIX, exc)
