"""ACP v2 的 WebSocket 客户端（JSON-RPC 2.0 over WebSocket）。

只做传输与协议收发，不含任何业务编排：

* 连接 / 断开（``ws://host:port/acp?key=...``，认证走 Query 参数）；
* 请求-响应配对（每个请求一个 ``asyncio.Future``，超时/断线都会失败）；
* 通知分发（``session/update`` 等无 ``id`` 报文，交给订阅者）；
* 服务端发来的请求（``session/request_permission``）的回包。

一个 :class:`ACPClient` 对应**一条 WebSocket 连接**，也就是一个群/一个会话；
「谁复用、谁回收」由 :mod:`acp.pool` 决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import typing as T

import websockets
from websockets.asyncio.client import ClientConnection

from astrbot.api import logger

from . import protocol as acp

#: 客户端在 ``initialize`` 里自报的名字。
DEFAULT_CLIENT_NAME = "astrbot_plugin_0528_v2"

#: 通知处理器：``(method, params)``，可同步可异步。
NotificationHandler = T.Callable[[str, dict], T.Any]
#: 服务端请求处理器：``(params) -> result``。
RequestHandler = T.Callable[[dict], T.Any]

#: 处理函数返回这个哨兵表示「先不回包」：请求记进
#: :attr:`ACPClient.deferred_requests`，由业务层稍后调
#: :meth:`ACPClient.reply_request` 回包（工具审批等用户在聊天里操作时用）。
DEFERRED = object()


class ACPError(RuntimeError):
    """ACP 通信错误基类。"""


class ACPConnectionError(ACPError):
    """连接不可用（未连接、已断开、连接失败）。"""


class ACPTimeoutError(ACPError):
    """请求超时。"""


class ACPProtocolError(ACPError):
    """协议层不匹配（例如服务端返回了别的 protocolVersion）。"""


class ACPClient:
    """一条 ACP WebSocket 连接。

    Args:
        url: WebSocket 地址，形如 ``ws://127.0.0.1:7433/acp?key=<key>``。
        name: 便于日志辨认的标签（一般用群/会话键）。
        connect_timeout: 建连超时（秒）。
        request_timeout: 默认的请求超时（秒）。
        max_message_size: 单帧上限（字节）。ACP 的终端快照可能很大，默认 1 MiB。
        permission_handler: ``session/request_permission`` 的处理函数；
            返回 ``{"outcome": ..., "optionId": ...}``。未设置时一律回
            ``cancelled``（即拒绝），避免在不该批准的时候替用户点头。
    """

    def __init__(
        self,
        url: str,
        *,
        name: str = "",
        connect_timeout: float = 15.0,
        request_timeout: float = 30.0,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
        max_message_size: int = 2**20,
        permission_handler: RequestHandler | None = None,
    ) -> None:
        self.url = url
        self.name = name or url
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_message_size = max_message_size
        self.permission_handler = permission_handler
        #: 已挂起（等业务层回包）的服务端请求：``request_id -> {"method", "params"}``。
        #: 工具审批用得上：插件不替用户点头，先挂起，等聊天里发 ``/aapprove`` 再回包。
        self.deferred_requests: dict[int, dict] = {}

        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._request_seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notify_handlers: list[NotificationHandler] = []
        self._request_handlers: dict[str, RequestHandler] = {}
        #: 服务端初始化响应（``initialize`` 之后可用）。
        self.server_info: dict = {}

    # ── 状态 ─────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        """WebSocket 是否处于已连接状态。"""
        return self._ws is not None

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<ACPClient {self.name!r} {state}>"

    # ── 连接管理 ─────────────────────────────────────────
    async def connect(self) -> None:
        """建立 WebSocket 连接并启动接收循环。

        Raises:
            ACPConnectionError: 建连失败（地址不可达、认证失败等）。
        """
        if self.connected:
            return
        try:
            self._ws = await websockets.connect(
                self.url,
                open_timeout=self.connect_timeout,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                close_timeout=5,
                max_size=self.max_message_size,
            )
        except Exception as exc:  # noqa: BLE001 - 归一成自己的异常类型
            self._ws = None
            raise ACPConnectionError(f"连接 {self.name} 失败：{exc}") from exc

        self._recv_task = asyncio.create_task(
            self._recv_loop(), name=f"acp-recv-{self.name}"
        )
        logger.debug("[0528v2] ACP 连接已建立：%s", self.name)

    async def close(self) -> None:
        """关闭连接并让所有在途请求失败（可重复调用）。"""
        ws, self._ws = self._ws, None
        self._fail_pending(ACPConnectionError("连接已关闭"))
        self.deferred_requests.clear()

        if self._recv_task is not None:
            task, self._recv_task = self._recv_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
            logger.debug("[0528v2] ACP 连接已断开：%s", self.name)

    def _fail_pending(self, exc: Exception) -> None:
        """让所有等待中的请求以 ``exc`` 失败。"""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # ── 收发 ─────────────────────────────────────────────
    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            raise ACPConnectionError(f"{self.name}：连接未建立")
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 连接在使用中掉线
            raise ACPConnectionError(f"{self.name}：发送失败（{exc}）") from exc

    async def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> T.Any:
        """发送请求并等待响应。

        Args:
            method: ACP 方法名。
            params: 请求参数。
            timeout: 超时秒数，默认取构造时的 ``request_timeout``。

        Returns:
            ``result`` 字段（没有时为空字典）。

        Raises:
            ACPConnectionError: 连接不可用。
            ACPTimeoutError: 等待超时。
            ACPError: 服务端返回了 JSON-RPC 错误。
        """
        if not self.connected:
            raise ACPConnectionError(f"{self.name}：连接未建立")

        self._request_seq += 1
        request_id = self._request_seq
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        wait = self.request_timeout if timeout is None else timeout

        try:
            await self._send(acp.jsonrpc_request(request_id, method, params))
            return await asyncio.wait_for(future, timeout=wait)
        except asyncio.TimeoutError:
            raise ACPTimeoutError(
                f"{self.name}：请求 {method} 超时（{wait:g}s）"
            ) from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        """发送通知（不等待响应）。"""
        if not self.connected:
            raise ACPConnectionError(f"{self.name}：连接未建立")
        await self._send(acp.jsonrpc_notification(method, params))

    async def _recv_loop(self) -> None:
        """接收循环：把响应、通知、服务端请求分别送出去。"""
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                try:
                    message = acp.parse_message(raw)
                except ValueError as exc:
                    logger.warning("[0528v2] %s：收到无法解析的报文（%s）", self.name, exc)
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("[0528v2] %s：ACP 连接已关闭（%s）", self.name, exc)
        except Exception as exc:  # noqa: BLE001 - 接收循环不向上抛
            logger.error("[0528v2] %s：接收循环异常：%s", self.name, exc, exc_info=True)
        finally:
            self._ws = None
            self._fail_pending(ACPConnectionError("连接已断开"))

    async def _dispatch(self, message: dict) -> None:
        """按报文类型分发。"""
        if acp.is_response(message):
            self._resolve_response(message)
        elif acp.is_request(message):
            await self._handle_request(message)
        elif acp.is_notification(message):
            await self._handle_notification(message)
        else:
            logger.debug("[0528v2] %s：忽略了无法归类的报文 %s", self.name, message)

    def _resolve_response(self, message: dict) -> None:
        """把响应交给对应的等待者。"""
        request_id = message.get("id")
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            logger.debug("[0528v2] %s：收到无人认领的响应 id=%s", self.name, request_id)
            return
        if "error" in message:
            future.set_exception(
                ACPError(f"{self.name}：{acp.describe_rpc_error(message)}")
            )
        else:
            future.set_result(message.get("result", {}))

    async def _handle_notification(self, message: dict) -> None:
        """把通知转给订阅者。"""
        method = message.get("method", "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {"value": params}
        for handler in list(self._notify_handlers):
            try:
                result = handler(method, params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - 单个订阅者出错不影响其它
                logger.error(
                    "[0528v2] %s：通知处理器异常（%s）：%s", self.name, method, exc,
                    exc_info=True,
                )

    async def _handle_request(self, message: dict) -> None:
        """处理服务端发来的请求并回包。"""
        request_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {"value": params}

        handler = self._request_handlers.get(method)
        if handler is None and method == acp.METHOD_REQUEST_PERMISSION:
            handler = self.permission_handler

        if handler is None:
            logger.warning("[0528v2] %s：没有处理 %s 的处理器", self.name, method)
            await self._reply(
                acp.jsonrpc_error(
                    request_id,
                    acp.ERROR_METHOD_NOT_FOUND,
                    f"unsupported request: {method}",
                )
            )
            return

        try:
            result = handler(params)
            if asyncio.iscoroutine(result):
                result = await result
            if result is DEFERRED:
                # 业务层要自己决定什么时候回包（等待用户在聊天里操作）：
                # 记下来，等 reply_request()；这时**不能**先回包。
                self.deferred_requests[request_id] = {
                    "method": method,
                    "params": params,
                }
                logger.debug(
                    "[0528v2] %s：挂起服务端请求 %s（id=%s），等业务层回包",
                    self.name,
                    method,
                    request_id,
                )
                return
            await self._reply(acp.jsonrpc_result(request_id, result or {}))
        except Exception as exc:  # noqa: BLE001 - 处理失败也要回包，否则服务端挂起
            logger.error("[0528v2] %s：处理 %s 失败：%s", self.name, method, exc)
            await self._reply(
                acp.jsonrpc_error(request_id, acp.ERROR_INTERNAL, str(exc))
            )

    async def _reply(self, payload: dict) -> None:
        """回包（失败只记日志，避免接收循环被打断）。"""
        with contextlib.suppress(Exception):
            await self._send(payload)

    # ── 订阅 ─────────────────────────────────────────────
    def subscribe(self, handler: NotificationHandler) -> T.Callable[[], None]:
        """订阅全部通知，返回取消订阅的函数。"""
        self._notify_handlers.append(handler)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._notify_handlers.remove(handler)

        return _unsubscribe

    def set_request_handler(self, method: str, handler: RequestHandler) -> None:
        """注册某个服务端请求方法的处理器。"""
        self._request_handlers[method] = handler

    def set_permission_handler(self, handler: RequestHandler | None) -> None:
        """替换 ``session/request_permission`` 的处理函数（None 表示回绝所有审批）。"""
        self.permission_handler = handler

    # ── 挂起的服务端请求（工具审批用）──────────────────────
    def pending_permission(self) -> tuple[int, dict] | None:
        """当前挂着的 ``session/request_permission``：``(request_id, params)``。

        插件收到审批请求时不自动回包（见 :data:`DEFERRED`），先在这里挂着；
        ``/aapprove`` 用它找到要放行的那一条。没有挂起的请求时返回 ``None``。
        """
        for request_id, info in self.deferred_requests.items():
            if info.get("method") == acp.METHOD_REQUEST_PERMISSION:
                return request_id, dict(info.get("params") or {})
        return None

    def clear_deferred(self) -> None:
        """丢掉全部挂起的服务端请求（**不回包**）。

        新一轮 prompt 发出前调用：用户没发 ``/aapprove`` 就继续聊，alkaid0 自己会
        把这次审批当作拒绝；这里只清理客户端的内存记录，不需要也不应该再回包。
        """
        self.deferred_requests.clear()

    async def reply_request(self, request_id: int, result: dict) -> bool:
        """给一条挂起的服务端请求回包。

        Returns:
            ``True`` 表示确实回了一条还在挂着的请求；``False`` 表示那条请求已经
            失效（过期、或已被清理），调用方据此提示用户。
        """
        info = self.deferred_requests.pop(request_id, None)
        if info is None:
            return False
        await self._reply(acp.jsonrpc_result(request_id, result or {}))
        return True

    # ── ACP 便捷封装 ──────────────────────────────────────
    async def initialize(self, *, timeout: float | None = None) -> dict:
        """握手：声明 ACP v2 支持并取回服务端能力。

        Returns:
            服务端 ``initialize`` 响应（``protocolVersion`` / ``capabilities`` / ``info``）。

        Raises:
            ACPProtocolError: 服务端协议版本不是 2。
        """
        result = await self.call(
            acp.METHOD_INITIALIZE,
            {
                "protocolVersion": acp.PROTOCOL_VERSION,
                "capabilities": {},
                "info": {"name": DEFAULT_CLIENT_NAME},
            },
            timeout=timeout,
        )
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version != acp.PROTOCOL_VERSION:
            raise ACPProtocolError(
                f"{self.name}：服务端 ACP 版本为 {version}，本客户端只支持 "
                f"{acp.PROTOCOL_VERSION}"
            )
        self.server_info = result if isinstance(result, dict) else {}
        return self.server_info

    async def session_new(self, cwd: str, *, timeout: float | None = None) -> dict:
        """新建 ACP 会话（工作目录必须已存在）。"""
        return await self.call(acp.METHOD_SESSION_NEW, {"cwd": cwd}, timeout=timeout)

    async def session_resume(
        self,
        session_id: str,
        cwd: str,
        *,
        replay_from: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """续接一个已有会话。

        ``replay_from`` **省略**时服务端只重连、不回放历史（ACP v2），也就是
        「load 但不取历史」；给它 ``{"type": "start"}`` 才会回放。

        Args:
            session_id: 要续接的会话 ID（形如 ``sess_1:/path/to/workspace``）。
            cwd: 会话的工作目录，必须与服务端记录的一致，否则报 ``cwd not match``。
            replay_from: 历史回放参数；默认 None（不回放）。

        Returns:
            服务端的 ``session/resume`` 响应（``configOptions`` 等）。
        """
        params: dict = {"sessionId": session_id, "cwd": cwd}
        if replay_from is not None:
            params["replayFrom"] = replay_from
        return await self.call(acp.METHOD_SESSION_RESUME, params, timeout=timeout)

    async def session_list(
        self,
        cwd: str,
        *,
        cursor: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """列出某个工作目录下的会话（按最后活动时间倒序，服务端每页 30 个）。

        Args:
            cwd: 工作目录；必须已存在且被 alkaid0 初始化过（目录里有 ``.alkaid0``），
                否则服务端报 ``cwd not found`` / ``cwd not inited``。
            cursor: 上一页响应里的 ``nextCursor``（不透明字符串），用于翻页。

        Returns:
            服务端响应：``sessions``（``sessionId`` / ``cwd`` / ``title`` /
            ``updatedAt``）与可选的 ``nextCursor``。
        """
        params: dict = {"cwd": cwd}
        if cursor:
            params["cursor"] = cursor
        return await self.call(acp.METHOD_SESSION_LIST, params, timeout=timeout)

    async def session_set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        type: str = acp.CONFIG_TYPE_SELECT,
        timeout: float | None = None,
    ) -> dict:
        """改一个**会话级**配置项（模型、推理强度等）。

        只影响这一个会话（服务端按会话存），改完会向该会话的所有客户端广播
        ``config_option_update``。

        Args:
            session_id: 目标会话 ID。
            config_id: 配置项 ID（:data:`acp.CONFIG_ID_MODEL` /
                :data:`acp.CONFIG_ID_THOUGHT_LEVEL`）。
            value: 目标值；模型形如 ``<index>/<modelConfigID>``（扩展文档 §5.2）。
            type: 配置项类型，下拉选择传 ``"id"``（ACP v2 必填字段）。

        Returns:
            服务端响应（``configOptions`` 已是改完之后的值）。
        """
        return await self.call(
            acp.METHOD_SESSION_SET_CONFIG_OPTION,
            {
                "sessionId": session_id,
                "configId": config_id,
                "type": type,
                "value": value,
            },
            timeout=timeout,
        )

    async def session_prompt(
        self,
        session_id: str,
        text: str,
        *,
        timeout: float | None = None,
    ) -> dict:
        """提交一轮提示词。ACP v2 下这是**立即确认**（``{}``），正文靠通知推送。"""
        return await self.call(
            acp.METHOD_SESSION_PROMPT,
            {"sessionId": session_id, "prompt": acp.prompt_blocks(text)},
            timeout=timeout,
        )

    async def session_cancel(self, session_id: str) -> None:
        """中止当前这一轮（``session/cancel``：**通知**，没有响应可等）。

        取消在 ACP 里是客户端单向通知（不是 ``$/cancel_request`` 那种带 id 的通用
        请求，也没有回包，见 ACP 规范的 Cancellation 一节）：服务端收到后应尽快停掉
        本轮的语言模型请求与工具调用，并以 ``state_update idle`` +
        ``stopReason: "cancelled"`` 收尾。

        因此这里的返回只代表**请求已发出** —— 这一轮到底停在哪、以什么收尾，仍然由
        :meth:`session_prompt` 那一侧的 ``session/update`` 通知流体现。
        """
        await self.notify(acp.METHOD_SESSION_CANCEL, {"sessionId": session_id})

    async def session_close(
        self, session_id: str, *, timeout: float | None = None
    ) -> T.Any:
        """关闭 ACP 会话（服务端会把会话从连接上解绑并延迟释放）。"""
        return await self.call(
            acp.METHOD_SESSION_CLOSE, {"sessionId": session_id}, timeout=timeout
        )

    async def fs_mkdir(
        self, session_id: str, path: str, *, timeout: float | None = None
    ) -> T.Any:
        """递归创建会话工作目录下的子目录（alkaid0 的 ``fs over ACP`` 扩展）。

        等价于 ``mkdir -p``：**递归、幂等**（目录已存在也不报错），所以可以放心重复调用。
        会话的 ``cwd`` 本身必须已存在（``session/new`` 就要求这点），这里建的是它下面的
        相对路径 —— 用来给「新群第一次聊天」补齐工作目录模板渲染出来的目录。

        Args:
            session_id: 已建立会话的 ID（必须先 ``session/new`` 成功）。
            path: 相对该会话 ``cwd`` 的路径；不能以 ``/`` 开头，也不能含 ``.`` / ``..``。
            timeout: 请求超时（秒）。

        Returns:
            服务端响应（成功时是空对象）。
        """
        return await self.call(
            acp.METHOD_FS_MKDIR,
            {"sessionId": session_id, "path": path},
            timeout=timeout,
        )
