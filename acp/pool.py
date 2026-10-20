"""群 → 会话连接池：一个群一条连接、一个活跃 ACP 会话，空闲就回收。

设计（对应本插件的目标形态）：

* **一个群 = 一条 WebSocket 连接 = 一个活跃 ACP 会话**；群多了连接就多，彼此互不干扰；
* 连接是**懒建立**的：某个群的第一条消息才去连；
* **会话 ID 记在 SQLite 里**（:class:`~acp.store.SessionStore`）：开会话前先看记录，
  有就 ``session/resume``（**不带** ``replayFrom``，即只要会话、不回放历史）续接，
  没有或续接失败才 ``session/new`` 并把新 ID 记下来；
* 群空闲超过 ``idle_timeout`` 秒后，回收任务会 ``session/close`` 并断开该群的连接，
  但**记录留着** —— 下次来消息带着同一个 ID 续接，对话不会断片；
* 每个 :class:`GroupSession` 自带一把锁：同一个群的消息串行执行，不会两条提示词
  同时压进一个 ACP 会话。

连接参数（URL、工作目录）由调用方（runner）算好传进来；池子只管「按 key 复用、
参数变了就换、闲着就丢」。``/areset`` 对应 :meth:`SessionPool.reset_session`：
抹掉记录并断开连接，下次消息重新 ``session/new``。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing as T
from dataclasses import dataclass, field

from astrbot.api import logger

from . import commands as acp_commands
from . import protocol as acp
from .client import ACPClient, ACPError
from .store import SessionStore

#: 没有配置空闲时间时的默认值（秒）。
DEFAULT_IDLE_TIMEOUT_SEC = 1800.0

#: 客户端工厂：``(url, name) -> ACPClient``（未连接状态）。测试可注入替身。
ClientFactory = T.Callable[[str, str], ACPClient]

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"


def default_client_factory(url: str, name: str) -> ACPClient:
    """默认的客户端工厂：一条 URL 一条真 WebSocket 连接。"""
    return ACPClient(url, name=name)


@dataclass
class GroupSession:
    """一个群/会话对应的连接与 ACP 会话。

    Attributes:
        key: 群/会话的标识（一般用 ``event.unified_msg_origin``）。
        url: 该群使用的 ACP WebSocket 地址。
        cwd: 该群的工作目录（由路径模板渲染而来）。
        cwd_root: 工作目录模板里「占位符之前」的那段路径（必须已存在）—— 新群第一次
            ``session/new`` 失败时，用它开临时会话把 ``cwd`` 建出来（见 :meth:`_ensure_cwd`）。
        client: 底层 ACP 客户端（一条 WebSocket 连接）。
        session_id: 当前活跃的 ACP 会话 ID；断开后置空，重连时按记录续接。
        lock: 该群的串行锁（同群消息不并发进同一个 ACP 会话）。
        store: 会话 ID 的持久化表；None 表示不记录（只开会话、不续接）。
        config_options: 服务端给的**会话级配置项**（``configOptions``，模型列表在这里）；
            开会话 / 续接响应里拿一次，之后跟着 ``config_option_update`` 保持最新。
        available_commands: 服务端给的**命令列表**（``availableCommands``，
            ``/acmd`` 用它列出远端命令）；同样是开会话 / 续接时来一份。
        usage: 最近一次 ``usage_update`` 的上下文用量 ``(used, size)``（``/actx`` 用它）；
            开会话 / 续接时服务端都**不**推，只有这个连接上跑过一轮之后才有值。
    """

    key: str
    url: str
    cwd: str
    client: ACPClient
    store: SessionStore | None = None
    #: 模板里占位符之前的那段路径（自动建目录时的根；空串表示推不出根、不自动建）。
    cwd_root: str = ""
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    session_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    config_options: list[dict] = field(default_factory=list)
    #: 服务端下发的命令列表（``available_commands_update``，见 :mod:`acp.commands`）；
    #: 开会话 / 续接时各来一份，之后按广播刷新。
    available_commands: list[dict] = field(default_factory=list)
    #: 最近一次 ``usage_update`` 的上下文用量 ``(used, size)``；还没收到时是 None。
    usage: tuple[int, int] | None = None
    #: 是否已经挂上配置项监听（内部标记，避免重复订阅）。
    _config_listening: bool = field(default=False, repr=False)
    #: 是否已经挂上命令列表监听（内部标记，避免重复订阅）。
    _commands_listening: bool = field(default=False, repr=False)
    #: 是否已经挂上用量监听（内部标记，避免重复订阅）。
    _usage_listening: bool = field(default=False, repr=False)

    @property
    def connected(self) -> bool:
        """底层 WebSocket 是否还在。"""
        return self.client.connected

    @property
    def busy(self) -> bool:
        """是否正在跑一轮对话（回收任务会跳过）。"""
        return self.lock.locked()

    def idle_seconds(self) -> float:
        """距上次活动过了多少秒。"""
        return time.monotonic() - self.last_active

    def touch(self) -> None:
        """标记「刚刚有活动」，重置空闲计时。"""
        self.last_active = time.monotonic()

    # ── 会话级配置项（模型 / 推理强度）─────────────────────
    def config_option(self, config_id: str) -> dict | None:
        """按 ``configId`` 取本会话的配置项（可选模型列表在 ``model`` 里）。"""
        return acp.find_config_option(self.config_options, config_id)

    def current_model(self) -> str:
        """本会话当前的模型值（形如 ``<index>/<modelConfigID>``；未知时为空串）。"""
        return acp.option_current(self.config_option(acp.CONFIG_ID_MODEL))

    @property
    def supports_alkaid0(self) -> bool:
        """服务端是否声明了 alkaid0 私有扩展能力（``v0.4``，见 :mod:`acp.commands`）。

        声明了才认它下发的命令文案、才按中文对照表翻译（alcoh 是同一套判断）；
        第三方 ACP 服务端的描述语言未知，一律照原文显示。
        """
        return acp_commands.has_capability(
            getattr(self.client, "server_info", None),
            acp_commands.CAPABILITY_ALKAID0_V04,
        )

    async def set_config_option(
        self, config_id: str, value: str, *, type: str = acp.CONFIG_TYPE_SELECT
    ) -> dict:
        """改本会话的配置项（**只影响这个会话**），并更新本地缓存。

        Returns:
            服务端响应（``configOptions`` 是改完之后的值）。

        Raises:
            ACPError: 会话不可用，或服务端拒绝（模型不存在 / 被隐藏等）。
        """
        session_id = await self.ensure_session()
        result = await self.client.session_set_config_option(
            session_id, config_id, value, type=type
        )
        self._remember_config_options(result)
        self.touch()
        return result

    def _remember_config_options(self, payload: T.Any) -> None:
        """记下 ``configOptions``（响应与 ``config_option_update`` 事件形状一致）。"""
        options = acp.config_options_of(payload)
        if options:
            self.config_options = options

    def _remember_commands(self, payload: T.Any) -> None:
        """记下服务端下发的命令列表（只认 ``available_commands_update``）。

        列表为空（服务端没给 / 形状不对）时保持原样，免得一次异常广播把已知的
        命令清空。
        """
        if not isinstance(payload, dict):
            return
        if payload.get("sessionUpdate") != acp.UPDATE_AVAILABLE_COMMANDS:
            return
        commands = acp_commands.available_commands_of(payload)
        if commands:
            self.available_commands = commands

    def _listen_config_options(self) -> None:
        """订阅通知，跟着 ``config_option_update`` 刷新本地缓存（幂等）。"""
        if self._config_listening:
            return
        self._config_listening = True

        def _on_notification(_method: str, params: dict) -> None:
            update = params.get("update") if isinstance(params, dict) else None
            if not isinstance(update, dict):
                return
            if update.get("sessionUpdate") != acp.UPDATE_CONFIG_OPTION:
                return
            update_session = update.get("sessionId")
            if (
                isinstance(update_session, str)
                and update_session
                and self.session_id
                and update_session != self.session_id
            ):
                # 广播里带的是别的会话（同一条连接上不该发生，防御一下）
                return
            self._remember_config_options(update)

        self.client.subscribe(_on_notification)

    def _listen_available_commands(self) -> None:
        """订阅通知，跟住 ``available_commands_update``（幂等）。

        服务端在开会话 / 续接会话时就广播一份命令列表，所以要在开会话**之前**
        挂上监听，否则第一份列表会错过（同 :meth:`_listen_config_options`）。
        """
        if self._commands_listening:
            return
        self._commands_listening = True

        def _on_notification(_method: str, params: dict) -> None:
            update = params.get("update") if isinstance(params, dict) else None
            self._remember_commands(update)

        self.client.subscribe(_on_notification)

    def _listen_usage(self) -> None:
        """订阅通知，记下最新的 ``usage_update``（幂等）。

        服务端在**每次模型响应**后推用量（一次响应可能推好几条：流式中间帧与收尾兜底，
        值相同或递增），这里只留最新一份 —— 与 alcoh 的取值口径一致。
        开会话（``session/new``）与 ``session/resume``（不带 ``replayFrom``）都**不**推，
        所以缓存里的值只代表「这条连接上最近跑过的那一轮」。
        """
        if self._usage_listening:
            return
        self._usage_listening = True

        def _on_notification(_method: str, params: dict) -> None:
            update = params.get("update") if isinstance(params, dict) else None
            update_session = params.get("sessionId") if isinstance(params, dict) else None
            if (
                isinstance(update_session, str)
                and update_session
                and self.session_id
                and update_session != self.session_id
            ):
                # 广播里带的是别的会话（同一条连接上不该发生，防御一下）
                return
            usage = acp.usage_of(update)
            if usage is not None:
                self.usage = usage

        self.client.subscribe(_on_notification)

    async def ensure_session(self) -> str:
        """确保连接可用且存在活跃 ACP 会话，返回 ``sessionId``。

        连接断了内存里的绑定就没了（服务端在连接断开时即解绑会话），此时回到
        :meth:`_open_session`：按记录 ``session/resume`` 续接，或 ``session/new``。

        Returns:
            可用的 ``sessionId``。

        Raises:
            ACPError: 建连、握手、续接后开会话都失败。
        """
        if not self.client.connected:
            self.session_id = None
            await self.client.connect()
            await self.client.initialize()
            # 先挂上监听，免得错过开会话期间的配置项 / 命令列表 / 用量广播
            self._listen_config_options()
            self._listen_available_commands()
            self._listen_usage()

        if self.session_id is None:
            self.session_id = await self._open_session()

        self.touch()
        return self.session_id

    async def _open_session(self) -> str:
        """拿到本群该用的会话 ID：优先按记录续接，否则新开并记账。

        续接走 ``session/resume`` 且**不带** ``replayFrom``（只要会话、不回放历史）。
        记录里的会话已经不存在、或工作目录对不上（服务端会报 ``cwd not match``）时，
        删掉旧记录改用 ``session/new``，保证消息始终发得出去。
        """
        remembered = self.store.get(self.key) if self.store is not None else None
        if remembered:
            try:
                resumed = await self.client.session_resume(remembered, self.cwd)
            except ACPError as exc:
                logger.warning(
                    "%s: 群 %s 续接会话 %s 失败，改为新开会话（%s）",
                    LOG_PREFIX,
                    self.key,
                    remembered,
                    exc,
                )
                if self.store is not None:
                    self.store.forget(self.key)
            else:
                self._remember_config_options(resumed)
                logger.info(
                    "%s: 群 %s 已续接 ACP 会话 %s（工作目录 %s，不回放历史）",
                    LOG_PREFIX,
                    self.key,
                    remembered,
                    self.cwd,
                )
                return remembered

        try:
            result = await self.client.session_new(self.cwd)
        except ACPError as exc:
            # 新群第一次聊天：模板渲染出来的目录还不存在（session/new 只认已存在的
            # cwd）。借模板根开个临时会话、用 fs/mkdir 建出来，然后再试一次。
            if not await self._ensure_cwd():
                raise
            logger.info(
                "%s: 群 %s 已建好工作目录 %s（session/new 原来报 %s），重试开会话",
                LOG_PREFIX,
                self.key,
                self.cwd,
                exc,
            )
            result = await self.client.session_new(self.cwd)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise ACPError(f"{self.key}：session/new 没有返回 sessionId（{result!r}）")
        session_id = str(session_id)
        # 新会话：旧的用量作废（服务端开会话不推 usage，留着会张冠李戴）
        self.usage = None
        self._remember_config_options(result)
        if self.store is not None:
            self.store.remember(self.key, session_id, self.cwd)
        logger.info(
            "%s: 群 %s 已建立 ACP 会话 %s（工作目录 %s）",
            LOG_PREFIX,
            self.key,
            session_id,
            self.cwd,
        )
        return session_id

    async def _ensure_cwd(self) -> bool:
        """把 ``cwd`` 相对 ``cwd_root`` 的那一段建出来（``fs/mkdir``，幂等）。

        ``session/new`` 要求工作目录已存在，所以新群第一次聊天会被服务端拒绝 ——
        这里借 ``cwd_root``（模板里占位符之前那段，必须已存在）开一个**临时会话**，
        用它发 ``fs/mkdir``（递归、幂等），然后关掉临时会话。

        Returns:
            是否真的建了目录。推不出相对路径（没配 ``cwd_root``、或 ``cwd`` 不在
            它下面）、临时会话开不起来、服务端不支持扩展方法时都返回 ``False``，
            由调用方原样抛出 ``session/new`` 的错。
        """
        rel = _relative_under(self.cwd_root, self.cwd)
        if not rel or rel == ".":
            return False
        temp_session = ""
        try:
            result = await self.client.session_new(self.cwd_root)
            temp_session = str((result or {}).get("sessionId") or "")
            if not temp_session:
                return False
            await self.client.fs_mkdir(temp_session, rel)
        except Exception as exc:  # noqa: BLE001 - 建不了就照原样报 session_new 的错
            logger.warning(
                "%s: 群 %s 建工作目录失败（cwd=%s，root=%s，rel=%s）：%s",
                LOG_PREFIX,
                self.key,
                self.cwd,
                self.cwd_root,
                rel,
                exc,
            )
            return False
        finally:
            if temp_session:
                with contextlib.suppress(Exception):
                    await self.client.session_close(temp_session)
        logger.info("%s: 群 %s 已建好工作目录 %s", LOG_PREFIX, self.key, self.cwd)
        return True

    async def switch_session(self, session_id: str) -> None:
        """把这条连接的活跃会话换成另一个已有会话（``/aresume``）。

        服务端一条连接只绑一个会话，所以先 ``session/close`` 掉当前会话、再
        ``session/resume`` 目标会话；**成功之后**才把新会话 ID 记进记录表
        （失败时记录不动，免得把本群指到一个用不了的会话上）。

        Raises:
            ACPError: 会话不可用，或服务端拒绝（会话不存在 / cwd 对不上等）。
        """
        await self.ensure_session()
        if session_id == self.session_id:
            self.touch()
            return
        if self.connected and self.session_id:
            with contextlib.suppress(Exception):
                await self.client.session_close(self.session_id)
        # 先清空再续接：万一续接失败，下次消息会按记录表里的旧会话重来
        self.session_id = None
        result = await self.client.session_resume(session_id, self.cwd)
        self.session_id = session_id
        # 换了会话：旧的用量不属于它
        self.usage = None
        self._remember_config_options(result)
        if self.store is not None:
            self.store.remember(self.key, session_id, self.cwd)
        self.touch()

    async def release(self, *, reason: str = "") -> None:
        """结束 ACP 会话并断开连接（尽力而为，不抛异常）。"""
        if self.connected and self.session_id:
            with contextlib.suppress(Exception):
                await self.client.session_close(self.session_id)
        self.session_id = None
        with contextlib.suppress(Exception):
            await self.client.close()
        if reason:
            logger.info("%s: 已回收群 %s 的连接（%s）", LOG_PREFIX, self.key, reason)


class SessionPool:
    """按群维护连接的表。

    Args:
        client_factory: 客户端工厂，默认建真 WebSocket 客户端。
        idle_timeout: 空闲回收阈值（秒）；``<= 0`` 表示不自动回收。
        reaper_interval: 回收任务的检查间隔上限（秒），实际取空闲阈值的一半、最多 60 秒。
        store: 会话 ID 的持久化表；``None`` 表示只用内存（连接一断就开新会话）。
    """

    def __init__(
        self,
        *,
        client_factory: ClientFactory = default_client_factory,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SEC,
        reaper_interval: float = 60.0,
        store: SessionStore | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._store = store
        self.idle_timeout = float(idle_timeout)
        self.reaper_interval = float(reaper_interval)
        self._sessions: dict[str, GroupSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    # ── 查询 ─────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._sessions)

    def peek(self, key: str) -> GroupSession | None:
        """看看某个群是否已有连接（不建立、不续期）。"""
        return self._sessions.get(key)

    def snapshot(self) -> list[dict]:
        """当前所有连接的状态快照（供状态命令 / 日志使用）。"""
        return [
            {
                "key": session.key,
                "url": session.url,
                "cwd": session.cwd,
                "sessionId": session.session_id or "",
                "model": session.current_model(),
                "connected": session.connected,
                "busy": session.busy,
                "idleSec": round(session.idle_seconds(), 1),
            }
            for session in sorted(self._sessions.values(), key=lambda s: s.created_at)
        ]

    def set_idle_timeout(self, seconds: float) -> None:
        """更新空闲回收阈值（执行器面板上的值变了就调一下）。

        ``0`` 表示关闭自动回收，必须同步进去；负数按 ``0`` 处理。
        """
        self.idle_timeout = max(0.0, float(seconds))

    @property
    def reaper_running(self) -> bool:
        """后台空闲回收任务是否在跑。"""
        return self._reaper_task is not None and not self._reaper_task.done()

    @property
    def store(self) -> SessionStore | None:
        """会话 ID 的持久化表（未配置时为 None）。"""
        return self._store

    # ── 取用 / 归还 ───────────────────────────────────────
    async def acquire(
        self, key: str, *, url: str, cwd: str, cwd_root: str = ""
    ) -> GroupSession:
        """取得该群的连接（没有就建，参数变了就换），并保证会话可用。

        Args:
            key: 群/会话标识。
            url: 该群要用的 ACP WebSocket 地址。
            cwd: 该群要用的工作目录。
            cwd_root: 工作目录模板里占位符之前的那段 —— 新群第一次开会话失败时，
                用它开临时会话把 ``cwd`` 建出来；空串表示推不出根、不自动建目录。

        Returns:
            已连接、已有活跃 ACP 会话的 :class:`GroupSession`。

        Raises:
            ACPError: 建连或开会话失败（失败的连接不会留在池子里）。
        """
        stale: GroupSession | None = None
        async with self._lock:
            session = self._sessions.get(key)
            if session is not None and (session.url != url or session.cwd != cwd):
                # 面板上改了 URL 或工作目录模板：老连接按新参数重建
                del self._sessions[key]
                stale, session = session, None

        if stale is not None:
            await stale.release(reason="连接参数已变更")

        if session is None:
            async with self._lock:
                session = self._sessions.get(key)
                if session is None:
                    client = self._client_factory(url, key)
                    session = GroupSession(
                        key=key,
                        url=url,
                        cwd=cwd,
                        client=client,
                        store=self._store,
                        cwd_root=cwd_root,
                    )
                    self._sessions[key] = session

        try:
            await session.ensure_session()
        except Exception:
            await self._drop(key, session, reason="建立会话失败")
            raise
        return session

    async def release(self, key: str, *, reason: str = "") -> bool:
        """回收某个群的连接；返回是否真的回收了一个。"""
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return False
        await session.release(reason=reason)
        return True

    async def reset_session(self, key: str, *, reason: str = "手动重置会话") -> bool:
        """重置某个群的会话：抹掉记录 + 断开连接，下次消息重新 ``session/new``。

        ``/areset`` 走这里。**不等**该群正在跑的那一轮结束（用户是主动重置，就该
        立刻生效）—— 在途的那一轮会因为连接断开而报错收尾。

        Returns:
            是否真的动了什么（删掉了记录，或断开了一条连接）。
        """
        forgotten = (
            self._store.forget(key) if self._store is not None else False
        )
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return forgotten
        await session.release(reason=reason)
        return True

    async def close_all(self, *, reason: str = "插件停用") -> int:
        """回收全部连接（插件停用时调用）。"""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.release(reason=reason)
        return len(sessions)

    async def _drop(self, key: str, session: GroupSession, *, reason: str) -> None:
        """把某个连接从表里摘掉并释放（仅当表里还是它）。"""
        async with self._lock:
            if self._sessions.get(key) is session:
                del self._sessions[key]
        await session.release(reason=reason)

    # ── 空闲回收 ─────────────────────────────────────────
    async def reap_idle(self) -> list[str]:
        """回收所有空闲超时的连接，返回被回收的 key 列表。

        正在跑一轮对话（``busy``）的连接会跳过，等它闲下来再说。
        """
        timeout = self.idle_timeout
        if timeout <= 0:
            return []
        expired = [
            session
            for session in list(self._sessions.values())
            if not session.busy and session.idle_seconds() >= timeout
        ]
        for session in expired:
            await self._drop(
                session.key, session, reason=f"空闲超过 {timeout:g} 秒"
            )
        if expired:
            logger.info(
                "%s: 空闲回收完成，共断开 %d 条连接，当前 %d 条",
                LOG_PREFIX,
                len(expired),
                len(self._sessions),
            )
        return [session.key for session in expired]

    def _next_interval(self) -> float:
        """下一轮检查的间隔：空闲阈值的一半，夹在 1~``reaper_interval`` 秒之间。"""
        if self.idle_timeout <= 0:
            return self.reaper_interval
        return max(1.0, min(self.reaper_interval, self.idle_timeout / 2))

    def start_reaper(self) -> None:
        """启动后台回收任务（重复调用只保留一个）。"""
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(), name="0528v2-acp-reaper"
        )

    async def stop_reaper(self) -> None:
        """停止后台回收任务。"""
        task, self._reaper_task = self._reaper_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _reaper_loop(self) -> None:
        """回收循环：按 :meth:`_next_interval` 定期清理空闲连接。"""
        while True:
            await asyncio.sleep(self._next_interval())
            try:
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 回收失败不能拖垮后台任务
                logger.error("%s: 空闲回收出错：%s", LOG_PREFIX, exc, exc_info=True)


# ── 工具 ──────────────────────────────────────────────────────────────
def _relative_under(root: str, path: str) -> str:
    """``path`` 相对 ``root`` 的那一段；不在 ``root`` 下面时返回空串。

    给「自动建目录」用：``fs/mkdir`` 只接受**相对**会话 cwd 的路径，所以要先从
    绝对工作目录里剥掉模板根；``path == root`` 时返回 ``"."``（根目录本身，无需创建）。
    """
    if not root:
        return ""
    if path == root:
        return "."
    prefix = root if root.endswith("/") else f"{root}/"
    if not path.startswith(prefix):
        return ""
    return path[len(prefix) :].strip("/")
