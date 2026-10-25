"""供其它 AstrBot 插件调用的「主动唤醒」接口。

其它插件可以让 alkaid0 在指定会话（群 / 私聊）里主动跑一轮，用于巡检、定时汇总、
由别的插件触发的工作流等场景。**谁可以唤、带什么系统提示词**由本插件的
``plugin_wakeup`` 配置决定（见 :mod:`plugin_wakeup`），没配置的插件一律拒绝。

用法（调用方插件）::

    from data.plugins.astrbot_plugin_0528_v2.api import wake

    result = await wake(
        "astrbot_plugin_myplugin",        # 调用方插件名（必须写进 plugin_wakeup 配置）
        "aiocqhttp:group:123456",         # 目标会话（AstrBot 的 unified_msg_origin）
        "看看今天的日志有没有报错",        # 这次要它做的事（可空，只用系统提示词）
    )
    if result.ok:
        print(result.text)                # alkaid0 的回复
    else:
        print(result.error)

设计取舍：

* 共享状态挂在进程级模块上（见 :func:`_shared_state`），不依赖本模块的全局变量：
  AstrBot 重载插件时 ``data.plugins.*`` 下的模块会被重建，调用方可能还拿着旧模块对象。
* 唤醒会**占住该群的闸门**（与 cron / 普通消息同一条串行通道）：群里正在跑一轮时
  先排队，等它结束再发，不会两轮并发写同一个会话。
* 回复默认发到群里（``reply_to_chat`` 可配），调用方同时能拿到 ``text``；
  传 ``send=False`` 可以只取结果、不往群里发。
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

#: 插件名（与 metadata.yaml / 目录名一致）。
PLUGIN_NAME = "astrbot_plugin_0528_v2"

#: 进程级共享状态的模块名（别改，调用方与主插件靠它对齐）。
_SHARED_MODULE_NAME = "astrbot_plugin_0528_v2_wake_shared"

#: 共享状态里放服务实例的键。
_SERVICE_KEY = "service"


class WakeError(RuntimeError):
    """唤醒通道本身不可用（插件没启动、参数不合法等）。"""


@dataclass(frozen=True)
class WakeResult:
    """一次主动唤醒的结果。

    Attributes:
        ok: 这一轮是否正常跑完（服务端报错 / 连接断开时为 ``False``）。
        text: alkaid0 的回复正文；``ok`` 为 ``False`` 时可能已有半截内容。
        error: 失败原因（``ok`` 为 ``True`` 时为空串）。
        session_id: 这一轮用的 ACP 会话 ID（诊断用）。
        cwd: 这一轮用的工作目录。
        sent: 是否已经把回复发到聊天里。
    """

    ok: bool
    text: str = ""
    error: str = ""
    session_id: str = ""
    cwd: str = ""
    sent: bool = False


def _shared_state() -> dict[str, Any]:
    """返回进程级共享状态（模块对象挂在 ``sys.modules`` 上，重载不丢）。"""
    module = sys.modules.get(_SHARED_MODULE_NAME)
    if module is None:
        module = types.ModuleType(_SHARED_MODULE_NAME)
        module.state = {}  # type: ignore[attr-defined]
        sys.modules[_SHARED_MODULE_NAME] = module
    state = getattr(module, "state", None)
    if not isinstance(state, dict):
        state = {}
        module.state = state  # type: ignore[attr-defined]
    return state


def register_service(service: Any) -> None:
    """登记本插件的实例（插件 ``initialize`` 时调用）。"""
    _shared_state()[_SERVICE_KEY] = service


def unregister_service(service: Any | None = None) -> None:
    """注销实例（插件 ``terminate`` 时调用）。

    传了 ``service`` 时只在当前登记的确实是它时才清 —— 热重载期间新旧实例交替，
    旧实例的 ``terminate`` 不该把新实例的登记抹掉。
    """
    state = _shared_state()
    if service is None or state.get(_SERVICE_KEY) is service:
        state.pop(_SERVICE_KEY, None)


def service_ready() -> bool:
    """本插件是否已经启动、可以接受唤醒请求。"""
    return _shared_state().get(_SERVICE_KEY) is not None


async def wake(
    plugin: str,
    session: str,
    prompt: str = "",
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    send: bool | None = None,
) -> WakeResult:
    """让 alkaid0 在 ``session`` 里主动跑一轮，并等它结束。

    Args:
        plugin: 调用方插件名，必须出现在本插件的 ``plugin_wakeup`` 配置里
            （否则返回 ``ok=False``，不执行任何东西）。
        session: 目标会话，AstrBot 的 ``unified_msg_origin``
            （形如 ``aiocqhttp:group:123456`` / ``webchat:FriendMessage:webchat!u!x``）。
        prompt: 这次要让 alkaid0 做的事；空串表示只用该插件的系统提示词。
        cwd: 覆盖工作目录；默认按本群的工作目录模板 / ``/aworkdir`` 绑定解析。
        timeout: 最多等多久（秒）；默认用插件的 ``turn_timeout_sec``。
        send: 是否把回复发到群里；``None`` 用该插件的 ``reply_to_chat`` 配置。

    Returns:
        :class:`WakeResult`。未授权、提示词为空、这一轮出错都返回 ``ok=False``
        并把原因放在 ``error`` 里（不抛异常，调用方按需处理）。

    Raises:
        WakeError: 本插件还没启动（未启用 / 加载失败），或 ``plugin`` / ``session``
            为空这类调用方参数错误。
    """
    name = (plugin or "").strip()
    target = (session or "").strip()
    if not name:
        raise WakeError("wake() 需要调用方插件名（plugin）")
    if not target:
        raise WakeError("wake() 需要目标会话（session，AstrBot 的 unified_msg_origin）")

    service = _shared_state().get(_SERVICE_KEY)
    if service is None:
        raise WakeError(
            f"{PLUGIN_NAME} 还没启动：先确认插件已启用且加载成功（/0528v2）"
        )
    return await service.wake_for_plugin(
        name,
        target,
        prompt,
        cwd=cwd,
        timeout=timeout,
        send=send,
    )


__all__ = [
    "PLUGIN_NAME",
    "WakeError",
    "WakeResult",
    "register_service",
    "service_ready",
    "unregister_service",
    "wake",
]
