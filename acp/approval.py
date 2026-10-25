"""工具审批直通（Auto-Approve）的判定与提示词解析。

审批请求（``session/request_permission``）是否自动放行，由三层授权合并决定：

1. **配置白名单**（``auto_approve_tools``）：插件配置 / 执行器面板里的工具名清单；
2. **提示词授权**（``auto_approve_prompt``）：本轮消息里带 ``[auto_approve: read, edit]``
   这样的标记，只对这一轮生效；标记会在发给 alkaid0 前删掉；
3. **外部钩子授权**（``auto_approve_hook``）：``/wakeup`` 等外部入口按请求参数传入，
   只对这一次外部回合生效。

三层都受 :data:`RunnerSettings.auto_approve_enable` 总开关控制；任何一层没命中就
维持原来的行为：请求挂起（``DEFERRED``），等群友发 ``/aapprove``。

工具名匹配大小写不敏感，支持 ``*`` / ``?`` 通配（``*`` 单独写表示全部工具）。
这里只负责判定，不发聊天消息；业务层通过 :func:`wait_permission_state` 决定要不要
发「等待审批」提示。
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import time
import typing as T
from dataclasses import dataclass

from astrbot.api import logger

from . import protocol as acp
from .client import DEFERRED

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"

#: 提示词里的授权标记：``[auto_approve]`` / ``[auto_approve: read, edit]``，
#: 也认 ``[auto-approve: ...]``、``【自动批准：...】``。
_PROMPT_MARKER = re.compile(
    r"[\[【]\s*(?:auto[\s_-]*approve|自动批准|自动审批)\s*"
    r"(?:[:：]\s*([^\]】\n]*))?[\]】]",
    re.IGNORECASE,
)

#: 工具名清单的分隔符：英文/中文逗号、分号、空白。
_TOOL_SEPARATOR = re.compile(r"[,，;；\s]+")

_CALL_TITLE = re.compile(r"^\s*\[Call\s+([^\]]+)\]", re.IGNORECASE)
_APPROVE_TITLE = re.compile(
    r"^\s*Approve\s+tool\s+call\s*[:：]\s*(.+)$", re.IGNORECASE
)

_ALL_WORDS = frozenset(
    {"*", "all", "全部", "所有", "any", "true", "yes", "on", "1"}
)
#: 这些值表示「什么也不放行」（主要来自外部钩子的 false / no / off）。
_NONE_WORDS = frozenset({"", "false", "no", "off", "none", "0"})


@dataclass(frozen=True)
class ApprovalGrant:
    """一份工具授权（来自配置 / 提示词 / 外部钩子）。

    Attributes:
        names: 允许自动批准的工具名（已小写；可含 ``*`` / ``?`` 通配）。
        all_tools: 是否放行全部工具（显式写了 ``*`` / ``all`` / 空标记）。
        source: 给日志看的中文来源，例如「配置白名单」「提示词」「外部钩子」。
    """

    names: frozenset[str] = frozenset()
    all_tools: bool = False
    source: str = ""

    def __bool__(self) -> bool:
        return self.all_tools or bool(self.names)

    def allows(self, tool_name: str) -> bool:
        """这份授权是否允许某个工具。"""
        name = normalise_tool(tool_name)
        if not name:
            return False
        if self.all_tools:
            return True
        return any(
            name == pattern or fnmatch.fnmatchcase(name, pattern)
            for pattern in self.names
        )


#: 空授权（什么也不放行）。
EMPTY_GRANT = ApprovalGrant()


def normalise_tool(name: T.Any) -> str:
    """工具名归一成小写、去空白；不是字符串时返回空串。"""
    return name.strip().lower() if isinstance(name, str) else ""


def parse_tool_list(value: T.Any) -> tuple[frozenset[str], bool]:
    """把配置 / 提示词里的工具名清单洗成 ``(names, all_tools)``。

    接受字符串、字符串序列、布尔值；字符串按逗号/分号/空白拆分。
    ``True``、``"*"``、``"all"``、``"全部"`` 表示全部工具；``False`` / ``None`` /
    空串表示什么都不放行。
    """
    if value is None or value is False:
        return frozenset(), False
    if value is True:
        return frozenset(), True

    names: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _NONE_WORDS:
            return frozenset(), False
        names = [item for item in _TOOL_SEPARATOR.split(text) if item]
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, str):
                names.extend(part for part in _TOOL_SEPARATOR.split(item) if part)
            elif item is True:
                return frozenset(), True
    else:
        return frozenset(), False

    all_tools = False
    cleaned: set[str] = set()
    for name in names:
        lowered = normalise_tool(name)
        if not lowered or lowered in _NONE_WORDS:
            continue
        if lowered in _ALL_WORDS:
            all_tools = True
        else:
            cleaned.add(lowered)
    return frozenset(cleaned), all_tools


def grant_from_value(value: T.Any, *, source: str = "") -> ApprovalGrant:
    """把配置值 / 提示词参数 / 外部钩子参数转成 :class:`ApprovalGrant`。"""
    names, all_tools = parse_tool_list(value)
    return ApprovalGrant(names=names, all_tools=all_tools, source=source)


def merge_grants(*grants: ApprovalGrant) -> ApprovalGrant:
    """把多份授权并成一份（任何一份放行即放行）。"""
    names: set[str] = set()
    all_tools = False
    sources: list[str] = []
    for grant in grants:
        if not isinstance(grant, ApprovalGrant) or not grant:
            continue
        names.update(grant.names)
        all_tools = all_tools or grant.all_tools
        if grant.source and grant.source not in sources:
            sources.append(grant.source)
    return ApprovalGrant(
        names=frozenset(names), all_tools=all_tools, source="、".join(sources)
    )


def matching_source(grants: T.Iterable[ApprovalGrant], tool_name: str) -> str:
    """返回第一份允许这个工具的授权来源；没有命中时返回空串。"""
    for grant in grants:
        if isinstance(grant, ApprovalGrant) and grant.allows(tool_name):
            return grant.source
    return ""


def _calling_name(content: T.Any) -> str:
    """从 ``alk.cxykevin.top/calling_info`` 内容块里取真实工具名。"""
    items = list(content) if isinstance(content, (list, tuple)) else [content]
    for item in items:
        if not isinstance(item, dict) or item.get("type") != acp.KEY_CALLING_INFO:
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def tool_name_of(params: T.Any) -> str:
    """从 ``session/request_permission`` 参数里取工具名（取不到返回空串）。

    真机 alkaid0 的形状（扩展文档 §3.4）::

        {"title": "Approve tool call: edit",
         "subject": {"type": "tool_call",
                     "toolCall": {"title": "[Call edit]tid",
                                  "content": [{"type": ".../calling_info",
                                               "name": "edit"}]}}}

    优先 ``subject.toolCall.content`` 里的 ``calling_info.name``，其次
    ``[Call xxx]`` 标题，再次 ``Approve tool call: xxx`` 标题；不从 ``kind``
    猜（``kind`` 是 edit/other 这类大类，拿它批工具太宽）。
    """
    if not isinstance(params, dict):
        return ""

    subject = params.get("subject")
    tool = subject.get("toolCall") if isinstance(subject, dict) else None
    if isinstance(tool, dict):
        name = _calling_name(tool.get("content"))
        if name:
            return name
        title = tool.get("title")
        if isinstance(title, str):
            match = _CALL_TITLE.match(title)
            if match:
                return match.group(1).strip()

    title = params.get("title")
    if isinstance(title, str):
        match = _APPROVE_TITLE.match(title)
        if match:
            return match.group(1).strip()
        stripped = title.strip()
        if stripped and not stripped.lower().startswith("approve tool call"):
            # 测试 / 兼容旧形状：title 直接就是工具名
            return stripped
    return ""


def permission_option(params: T.Any) -> str:
    """挑一个「放行」选项的 ``optionId``；没有可放行的选项时返回空串。

    只认 ``allow_once`` / ``allow_always`` / kind 以 ``allow`` 开头的选项，
    避免在服务端只给拒绝选项时误选第一个。
    """
    if not isinstance(params, dict):
        return ""
    options = params.get("options")
    if not isinstance(options, list):
        return ""
    candidates: list[tuple[int, str]] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("optionId") or "").strip()
        if not option_id:
            continue
        kind = str(option.get("kind") or "").strip().lower()
        priority = 0
        if option_id == acp.PERMISSION_OPTION_ALLOW_ONCE or kind == "allow_once":
            priority = 1
        elif kind == "allow_always" or option_id == "allow_always":
            priority = 2
        elif kind.startswith("allow") or "allow" in option_id.lower():
            priority = 3
        if priority:
            candidates.append((priority, option_id))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def approval_result(params: T.Any) -> dict | None:
    """构造 ``{"outcome": "selected", "optionId": ...}``；没有可放行选项时返回 None。"""
    option_id = permission_option(params)
    if not option_id:
        return None
    return {
        "outcome": acp.PERMISSION_OUTCOME_SELECTED,
        "optionId": option_id,
    }


def extract_prompt_grant(prompt: str, *, enabled: bool) -> tuple[str, ApprovalGrant]:
    """从本轮提示词里取出自动批准标记，并把它删掉。

    Args:
        prompt: 已经加过前缀 / 提醒的提示词。
        enabled: 是否允许提示词授权（总开关 + ``auto_approve_prompt``）。

    Returns:
        ``(清理后的提示词, 授权)``；没有标记或未开启时原样返回提示词与空授权。

    标记写法::

        [auto_approve]                 本轮放行全部工具
        [auto_approve: read, edit]     本轮只放行 read / edit
        【自动批准：read，grep】        同样的中文写法

    标记可以出现在任意位置，匹配到的都会删掉（避免被当成正文发给 alkaid0）。
    """
    text = prompt if isinstance(prompt, str) else ""
    if not enabled or not text:
        return text, EMPTY_GRANT

    matches = list(_PROMPT_MARKER.finditer(text))
    if not matches:
        return text, EMPTY_GRANT

    names: list[str] = []
    all_tools = False
    for match in matches:
        raw = match.group(1)
        if raw is None or not raw.strip():
            all_tools = True
            continue
        part_names, part_all = parse_tool_list(raw)
        names.extend(part_names)
        all_tools = all_tools or part_all

    cleaned = _PROMPT_MARKER.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, ApprovalGrant(
        names=frozenset(names), all_tools=all_tools, source="提示词"
    )


def _setting(settings: T.Any, key: str, default: T.Any) -> T.Any:
    """从 RunnerSettings / 映射里取一个配置值；取不到用默认值。"""
    if settings is None:
        return default
    if isinstance(settings, dict) or hasattr(settings, "get"):
        try:
            value = settings.get(key, default)
        except Exception:  # noqa: BLE001 - 形状不认识就当没配
            return default
        return default if value is None else value
    return getattr(settings, key, default)


def _current_grants(session: T.Any, settings: T.Any) -> list[ApprovalGrant]:
    """当前这一轮生效的三层授权。"""
    grants: list[ApprovalGrant] = [
        grant_from_value(_setting(settings, "auto_approve_tools", ""), source="配置白名单")
    ]
    if bool(_setting(settings, "auto_approve_prompt", True)):
        grants.append(getattr(session, "prompt_grant", EMPTY_GRANT))
    if bool(_setting(settings, "auto_approve_hook", True)):
        getter = getattr(session, "active_external_grant", None)
        if callable(getter):
            try:
                grants.append(getter())
            except Exception:  # noqa: BLE001 - 授权缓存坏了不影响手动审批
                pass
        else:
            grants.append(getattr(session, "external_grant", EMPTY_GRANT))
    return [grant for grant in grants if isinstance(grant, ApprovalGrant)]


def mark_auto_approved(session: T.Any, tool_name: str, source: str) -> None:
    """记一笔自动批准（供回合循环判断「刚才是自动放行的，不要发等待提示」）。"""
    try:
        session.approval_seq = int(getattr(session, "approval_seq", 0)) + 1
        session.last_approval = {
            "tool": tool_name,
            "source": source,
            "at": time.monotonic(),
        }
    except Exception:  # noqa: BLE001 - 记录失败不影响回包
        pass


def build_permission_handler(
    session: T.Any,
    *,
    label: str = "",
    settings_provider: T.Callable[[], T.Any] | None = None,
) -> T.Callable:
    """构造 ``session/request_permission`` 处理函数：命中授权就回包，否则挂起。

    与旧行为兼容：没有配置、没有提示词/外部授权、或者取不到工具名时返回
    :data:`DEFERRED`，由聊天里的 ``/aapprove`` 手动放行。
    """

    async def _handle(params: dict) -> T.Any:
        settings: T.Any = None
        if settings_provider is not None:
            try:
                settings = settings_provider()
            except Exception as exc:  # noqa: BLE001 - 读配置失败按「不自动批准」
                logger.warning("%s: 读取自动审批配置失败：%s", LOG_PREFIX, exc)
                settings = None

        enabled = bool(_setting(settings, "auto_approve_enable", False))
        tool_name = tool_name_of(params)
        if not enabled or not tool_name:
            logger.info(
                "%s: %s 请求审批「%s」，先挂起；在聊天里发 /aapprove 放行",
                LOG_PREFIX,
                label or getattr(session, "key", "会话"),
                tool_name or "工具调用",
            )
            return DEFERRED

        source = matching_source(_current_grants(session, settings), tool_name)
        result = approval_result(params)
        if not source or result is None:
            logger.info(
                "%s: %s 请求审批「%s」，未命中自动批准；在聊天里发 /aapprove 放行",
                LOG_PREFIX,
                label or getattr(session, "key", "会话"),
                tool_name,
            )
            return DEFERRED

        mark_auto_approved(session, tool_name, source)
        logger.info(
            "%s: %s 自动批准工具「%s」（%s）",
            LOG_PREFIX,
            label or getattr(session, "key", "会话"),
            tool_name,
            source,
        )
        return result

    return _handle


async def wait_permission_state(
    session: T.Any,
    seen_seq: int,
    *,
    timeout: float = 0.2,
) -> tuple[str, tuple[int, dict] | None]:
    """等一小会儿，判断这次 ``requires_action`` 是自动放行还是等人工。

    alkaid0 先广播 ``requires_action``、再发 ``session/request_permission``，两个
    事件在不同任务里到达，直接查挂起请求会有一瞬间的空窗。这里轮询最多
    ``timeout`` 秒：

    Returns:
        ``("manual", (request_id, params))`` —— 请求已挂起，等 ``/aapprove``；
        ``("auto", None)`` —— 已经被自动批准（``approval_seq`` 变了）；
        ``("unknown", None)`` —— 没等到请求（服务端没发 / 已失效），用通用提示。
    """
    getter = getattr(getattr(session, "client", None), "pending_permission", None)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if callable(getter):
            try:
                pending = getter()
            except Exception:  # noqa: BLE001 - 查询失败按没挂起处理
                pending = None
            if pending is not None:
                return "manual", pending
        if int(getattr(session, "approval_seq", 0)) > int(seen_seq):
            return "auto", None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "unknown", None
        await asyncio.sleep(min(0.02, remaining))


__all__ = [
    "EMPTY_GRANT",
    "ApprovalGrant",
    "approval_result",
    "build_permission_handler",
    "extract_prompt_grant",
    "grant_from_value",
    "mark_auto_approved",
    "matching_source",
    "merge_grants",
    "normalise_tool",
    "parse_tool_list",
    "permission_option",
    "tool_name_of",
    "wait_permission_state",
]
