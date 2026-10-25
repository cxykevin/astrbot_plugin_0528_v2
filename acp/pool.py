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

#: 唤醒钩子判断「会话已经开始往外推内容」的事件类型。
#:
#: 开会话 / 续接时会顺带推 ``available_commands_update`` / ``config_option_update``，
#: 那些只是会话元数据，不能算「收到消息」；这里只认一轮对话真正产生的事件。
DELIVERY_UPDATE_KINDS = frozenset(
    {
        acp.UPDATE_USER_MESSAGE,
        acp.UPDATE_USER_MESSAGE_CHUNK,
        acp.UPDATE_AGENT_MESSAGE,
        acp.UPDATE_AGENT_MESSAGE_CHUNK,
        acp.UPDATE_AGENT_THOUGHT,
        acp.UPDATE_AGENT_THOUGHT_CHUNK,
        acp.UPDATE_TOOL_CALL,
        acp.UPDATE_STATE,
        acp.UPDATE_PLAN,
        acp.UPDATE_USAGE,
        acp.UPDATE_SUMMARY,
        acp.UPDATE_TERMINAL,
        acp.UPDATE_SHELL_STOP,
        acp.UPDATE_REWIND,
    }
)

#: 客户端工厂：``(url, name) -> ACPClient``（未连接状态）。测试可注入替身。
ClientFactory = T.Callable[[str, str], ACPClient]

#: 审批处理函数工厂：``(GroupSession) -> handler``；连接池建好每条会话就装上，
#: 这样本地回合、主动推送、外部唤醒钩子走的是同一套自动批准 / 挂起逻辑。
PermissionHandlerFactory = T.Callable[["GroupSession"], T.Any]

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
    #: 本轮提示词里带的自动批准授权（只在本轮有效，回合结束清掉）。
    prompt_grant: T.Any = None
    #: 外部钩子（cron 唤醒等）带来的自动批准授权（带过期时间）。
    external_grant: T.Any = None
    #: 外部授权的过期时刻（``time.monotonic()``）；``0`` 表示没有授权。
    external_grant_expires: float = 0.0
    #: 自动批准的序号：每自动批准一次 +1，回合循环据此判断是否要发「等待审批」提示。
    approval_seq: int = 0
    #: 最近一次自动批准的摘要（工具名 / 来源 / 时间）。
    last_approval: dict | None = None
    #: 已经收到的「对话内容」更新条数（唤醒钩子用它判断消息是否真的送达）。
    delivery_seq: int = 0
    #: 最近一条对话内容更新的 ``sessionUpdate`` 类型。
    last_update_kind: str = ""
    #: 外部 relay（唤醒钩子）正在消费这一轮：主动推送不要重复处理。
    external_relay: bool = False
    #: 常驻主动推送正在收集一轮（cron / 其它客户端发起）：回收任务要跳过。
    proactive_active: bool = False
    #: 是否已经挂上配置项监听（内部标记，避免重复订阅）。
    _config_listening: bool = field(default=False, repr=False)
    #: 是否已经挂上送达计数监听（内部标记，避免重复订阅）。
    _delivery_listening: bool = field(default=False, repr=False)
    #: 是否已经挂上命令列表监听（内部标记，避免重复订阅）。
    _commands_listening: bool = field(default=False, repr=False)
    #: 是否已经挂上用量监听（内部标记，避免重复订阅）。
    _usage_listening: bool = field(default=False, repr=False)
    #: 是否已经挂上主动推送监听（内部标记，避免重复订阅）。
    _proactive_listening: bool = field(default=False, repr=False)

    @property
    def connected(self) -> bool:
        """底层 WebSocket 是否还在。"""
        return self.client.connected

    @property
    def busy(self) -> bool:
        """是否正在跑一轮对话（回收任务会跳过）。"""
        return self.lock.locked() or self.proactive_active

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

    @property
    def supports_cron(self) -> bool:
        """服务端是否声明了定时任务能力（``alk.cxykevin.top/cron``）。"""
        return acp_commands.has_capability(
            getattr(self.client, "server_info", None),
            acp.CAPABILITY_CRON,
        )

    async def cron_jobs(self) -> list[dict]:
        """列出本会话的定时任务（``session/cron/list``，见扩展文档 §3.11）。

        Returns:
            任务列表（``id`` / ``cron`` / ``prompt`` / ``runOnce`` / ``workdir`` /
            ``nextRuns``）；形状不对的条目会被跳过。
        """
        session_id = await self.ensure_session()
        result = await self.client.cron_list(session_id)
        self.touch()
        jobs = result.get("jobs") if isinstance(result, dict) else None
        return [job for job in jobs or [] if isinstance(job, dict)]

    async def delete_cron_job(self, job_id: int) -> dict:
        """删除本会话的一条定时任务（只影响自己的工作目录 + 会话）。

        Raises:
            ACPError: 会话不可用，或任务不属于本会话 / 已经被删掉。
        """
        session_id = await self.ensure_session()
        result = await self.client.cron_delete(session_id, job_id)
        self.touch()
        return result if isinstance(result, dict) else {}

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

    # ── 自动批准授权（提示词 / 外部钩子）───────────────
    def set_prompt_grant(self, grant: T.Any) -> None:
        """记下本轮提示词里的自动批准授权（空授权也要记，覆盖上一轮）。"""
        self.prompt_grant = grant

    def clear_prompt_grant(self) -> None:
        """清掉本轮提示词授权（回合结束 / 异常收尾都调用）。"""
        self.prompt_grant = None

    def set_external_grant(self, grant: T.Any, *, ttl: float) -> None:
        """记下外部钩子的自动批准授权，并设置过期时间（秒）。"""
        names = getattr(grant, "names", frozenset()) if grant is not None else frozenset()
        all_tools = bool(getattr(grant, "all_tools", False)) if grant is not None else False
        if not names and not all_tools:
            self.clear_external_grant()
            return
        self.external_grant = grant
        self.external_grant_expires = time.monotonic() + max(1.0, float(ttl))

    def active_external_grant(self) -> T.Any:
        """取当前有效的外部授权；过期 / 没设置时返回空授权。"""
        from .approval import EMPTY_GRANT

        if self.external_grant is None or self.external_grant_expires <= 0:
            return EMPTY_GRANT
        if time.monotonic() >= self.external_grant_expires:
            self.clear_external_grant()
            return EMPTY_GRANT
        return self.external_grant

    def clear_external_grant(self) -> None:
        """清掉外部钩子的自动批准授权。"""
        self.external_grant = None
        self.external_grant_expires = 0.0

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

    def watch_deliveries(self) -> None:
        """开始统计这个会话收到的对话内容更新（幂等）。

        唤醒钩子在 ``session/resume`` **之前**调用：服务端会先补发正在流式输出的
        消息快照，再继续推后续 chunk —— 监听挂晚了就会把「已经送到」误判成没送到。
        """
        self._listen_delivery()

    def _listen_delivery(self) -> None:
        """订阅通知，给「真的有对话内容送达」计数（幂等）。

        只认 :data:`DELIVERY_UPDATE_KINDS` 里的事件；``available_commands_update`` /
        ``config_option_update`` 这类开会话时顺带推的元数据不算。只统计当前会话的更新
        （``sessionId`` 对不上就忽略，防止别的会话串进来）。
        """
        if self._delivery_listening:
            return
        self._delivery_listening = True

        def _on_notification(_method: str, params: dict) -> None:
            update = params.get("update") if isinstance(params, dict) else None
            if not isinstance(update, dict):
                return
            update_session = update.get("sessionId") or (
                params.get("sessionId") if isinstance(params, dict) else None
            )
            if (
                isinstance(update_session, str)
                and update_session
                and self.session_id
                and update_session != self.session_id
            ):
                # 广播里带的是别的会话（同一条连接上不该发生，防御一下）
                return
            kind = update.get("sessionUpdate")
            if not isinstance(kind, str) or kind not in DELIVERY_UPDATE_KINDS:
                return
            self.delivery_seq += 1
            self.last_update_kind = kind

        self.client.subscribe(_on_notification)

    async def wait_delivery(
        self, after_seq: int, timeout: float
    ) -> tuple[bool, str]:
        """等这个会话再送出一条对话内容更新。

        Args:
            after_seq: 调用方记下的起始计数（:attr:`delivery_seq`）。
            timeout: 最多等多少秒；``<= 0`` 时只检查一次。

        Returns:
            ``(是否等到了, 最后一条更新的类型)``。超时返回 ``(False, 已知类型)``。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.delivery_seq <= after_seq:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, self.last_update_kind
            await asyncio.sleep(min(0.05, remaining))
        return True, self.last_update_kind

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
            self._listen_delivery()

        if self.session_id is None:
            self.session_id = await self._open_session()

        self.touch()
        return self.session_id

    async def ensure_existing(self, session_id: str) -> str:
        """续接一个**已经存在**的会话，绝不退回 ``session/new``。

        唤醒钩子专用：会话记录就指向 ``session_id``，这里只负责把连接建好并
        ``session/resume`` 它。续接失败（会话被删 / cwd 对不上）就原样抛错，
        不会悄悄开一个新会话把记录覆盖掉。

        Args:
            session_id: 要续接的完整会话 ID（``sess_<号>:<cwd>``）。

        Returns:
            续接后的会话 ID（就是传入的 ``session_id``）。

        Raises:
            ACPError: 建连、握手失败，或服务端拒绝续接；连接已经绑在**另一个**
                会话上时也抛错（不能把别人正在用的会话挤掉）。
        """
        if not self.client.connected:
            self.session_id = None
            await self.client.connect()
            await self.client.initialize()
            # 同 ensure_session：先挂监听，别错过续接时的快照与广播
            self._listen_config_options()
            self._listen_available_commands()
            self._listen_usage()
            self._listen_delivery()

        if self.session_id is not None and self.session_id != session_id:
            raise ACPError(
                f"{self.key}：连接已绑定会话 {self.session_id}，不能改去唤醒 {session_id}"
            )
        if self.session_id == session_id:
            self.touch()
            return session_id

        result = await self.client.session_resume(session_id, self.cwd)
        self.session_id = session_id
        # 换了（续接了）会话：旧的用量不属于它
        self.usage = None
        self._remember_config_options(result)
        self.touch()
        logger.info(
            "%s: 群 %s 已唤醒 ACP 会话 %s（工作目录 %s，不回放历史）",
            LOG_PREFIX,
            self.key,
            session_id,
            self.cwd,
        )
        return session_id

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
        #: 新连接对象创建时的回调（主动推送用它挂常驻监听）。
        self._session_listener: T.Callable[[GroupSession], None] | None = None
        #: 审批处理函数工厂：每条会话建好就装上，外部回合也能自动批准 / 挂起。
        self._permission_handler_factory: PermissionHandlerFactory | None = None

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

    def set_session_listener(
        self, listener: T.Callable[[GroupSession], None] | None
    ) -> None:
        """给「新连接对象」挂一个回调，并把已有的连接也补挂一遍。

        主动推送用它订阅每条连接的 ``session/update``：不建连、不发请求，
        只监听服务端主动推来的内容（cron 唤醒等没有本地 prompt 的回合）。
        """
        self._session_listener = listener
        if listener is None:
            return
        for session in list(self._sessions.values()):
            listener(session)

    def set_permission_handler_factory(
        self, factory: PermissionHandlerFactory | None
    ) -> None:
        """设置审批处理函数工厂，并给已有 / 之后新建的连接都装上。

        自动批准不能只装在「本地回合」上：cron 唤醒、其它客户端发起的回合没有
        ``agent_runner._run_gated`` 这段代码，必须由连接池统一兜底。
        """
        self._permission_handler_factory = factory
        if factory is None:
            return
        for session in list(self._sessions.values()):
            self._install_permission_handler(session)

    def _install_permission_handler(self, session: GroupSession) -> None:
        """把工厂产出的处理函数装到一条会话上（失败只记日志）。"""
        factory = self._permission_handler_factory
        if factory is None:
            return
        try:
            handler = factory(session)
            session.client.set_permission_handler(handler)
        except Exception as exc:  # noqa: BLE001 - 装不上就退回默认「拒绝」
            logger.warning(
                "%s: 给群 %s 装审批处理函数失败：%s", LOG_PREFIX, session.key, exc
            )

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
        session = await self._get_or_create(key, url=url, cwd=cwd, cwd_root=cwd_root)
        try:
            await session.ensure_session()
        except Exception:
            await self._drop(key, session, reason="建立会话失败")
            raise
        return session

    async def _get_or_create(
        self, key: str, *, url: str, cwd: str, cwd_root: str = ""
    ) -> GroupSession:
        """取得（或新建）该群的连接对象，但**不**建连、不建会话。"""
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
                    if self._session_listener is not None:
                        self._session_listener(session)
                    self._install_permission_handler(session)
        return session

    async def prepare(
        self, key: str, *, url: str, cwd: str, cwd_root: str = ""
    ) -> GroupSession:
        """取得连接对象但不建连（唤醒钩子要在续接前挂监听，见 :meth:`acquire_existing`）。"""
        return await self._get_or_create(key, url=url, cwd=cwd, cwd_root=cwd_root)

    async def acquire_existing(
        self, key: str, *, url: str, cwd: str, session_id: str
    ) -> GroupSession:
        """唤醒一个**已经存在**的会话（``session/resume``，绝不 ``session/new``）。

        与 :meth:`acquire` 的区别：这里只认传入的 ``session_id``，续接失败就抛错，
        不会顺手开一个新会话把记录覆盖掉。

        Raises:
            ACPError: 建连 / 续接失败，或连接已经绑在别的会话上。
        """
        session = await self._get_or_create(key, url=url, cwd=cwd)
        session.watch_deliveries()
        try:
            await session.ensure_existing(session_id)
        except Exception:
            await self._drop(key, session, reason="唤醒已有会话失败")
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
