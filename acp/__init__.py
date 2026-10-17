"""ACP（Agent Client Protocol）v2 客户端子包。

* :mod:`acp.protocol` —— 协议常量与 JSON-RPC 编解码（无传输依赖）；
* :mod:`acp.client` —— 一条 WebSocket 连接的 JSON-RPC 2.0 收发；
* :mod:`acp.pool` —— 群 → 连接的表：一个群一条连接、一个活跃会话，空闲回收；
* :mod:`acp.commands` —— 远端斜杠命令的列表、能力标记与文案对照表（``/acmd`` 用）；
* :mod:`acp.store` —— 「群 → 会话 ID」的 SQLite 记录，用于回收/重启后接着聊。

只实现了 ACP 协议本身（WebSocket 传输 + 会话/提示词/通知/权限 + 会话 ID 持久化），
业务侧的 AstrBot 适配在插件的 :mod:`agent_runner` 里。
"""

from __future__ import annotations

from . import commands, protocol
from .client import (
    ACPClient,
    ACPConnectionError,
    ACPError,
    ACPProtocolError,
    ACPTimeoutError,
)
from .pool import DEFAULT_IDLE_TIMEOUT_SEC, GroupSession, SessionPool
from .store import SessionStore

__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SEC",
    "ACPClient",
    "ACPConnectionError",
    "ACPError",
    "ACPProtocolError",
    "ACPTimeoutError",
    "GroupSession",
    "SessionPool",
    "SessionStore",
    "commands",
    "protocol",
]
