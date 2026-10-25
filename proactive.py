"""没有本地 prompt 的主动回合：把 alkaid0 主动跑出来的内容推到聊天里。

正常对话由 :mod:`agent_runner` 在 ``session/prompt`` 期间消费通知；但 cron 唤醒、
其它客户端发起的回合等场景没有本地 prompt，服务端推来的 ``agent_message_chunk``
会落在一条没人消费的连接上 —— 群里就什么也收不到。

本模块给连接池里的每条会话挂一个**常驻监听**：

* 只在会话的锁没被占（没有本地回合）且没被唤醒钩子接管时启动；
* 收到 ``user_message`` / ``state_update running`` 就认为服务端主动开了一轮；
* 收集正文与过程消息，直到 ``state_update idle``，再通过 ``send_text`` 主动发到群里；
* 本地回合、``/acmd``、唤醒钩子的 relay 都持有会话锁（或 ``external_relay`` 标记），
  所以不会重复推送。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from . import agent_runner as runner_mod
from .acp import approval as approval_mod
from .acp import protocol as acp
from .acp.pool import GroupSession, SessionPool

#: 日志前缀。
LOG_PREFIX = "0528v2"

#: 主动推送的流式切分：句末标点 / 空行优先，实在没有就按最大长度硬切。
_STREAM_BREAKS = "。！？!?~…"
_MAX_SEGMENT = 400


class StreamBuffer:
    """把主动回合的正文切成可读的小段，边收边发（QQ 上就是连续几条消息）。

    完整消息（``agent_message``）可能跟在 chunk 后面补发一份全文，这里按
    ``messageId`` 记住已经发到哪，只发新增的后缀，避免重复。
    """

    def __init__(
        self,
        send_text: Callable[[str, str], Awaitable[bool]],
        key: str,
        *,
        interval: float = 0.35,
    ) -> None:
        self._send_text = send_text
        self._key = key
        self._interval = max(0.0, float(interval))
        self._pending = ""
        self._by_message: dict[str, str] = {}
        self._last_sent = 0.0
        self.sent_chars = 0
        self.messages = 0

    async def feed(self, message_id: str, text: str, *, replace: bool = False) -> None:
        """喂入一段正文；``replace=True`` 表示这是整条消息的全文。"""
        if not text:
            return
        previous = self._by_message.get(message_id, "")
        if replace:
            if text == previous:
                return
            if previous and text.startswith(previous):
                delta = text[len(previous) :]
            elif previous:
                # 全文和已收到的 chunk 对不上：多半是重复快照，宁可不发也别发两遍
                return
            else:
                delta = text
            self._by_message[message_id] = text
        else:
            self._by_message[message_id] = previous + text
            delta = text
        self._pending += delta
        await self._drain()

    async def flush(self) -> None:
        """把缓冲区里剩下的半句发出去。"""
        if not self._pending.strip():
            self._pending = ""
            return
        text, self._pending = self._pending, ""
        await self._emit(text)

    async def send_now(self, text: str) -> None:
        """先冲掉正文，再立即发一条过程消息 / 警告。"""
        await self.flush()
        await self._emit(text)

    async def _drain(self) -> None:
        while True:
            cut = self._cut(self._pending)
            if cut <= 0:
                return
            text, self._pending = self._pending[:cut], self._pending[cut:]
            await self._emit(text)

    @staticmethod
    def _cut(text: str) -> int:
        """返回本段应切到的位置；0 表示还不够一段。"""
        limit = min(len(text), _MAX_SEGMENT)
        if limit <= 0:
            return 0
        blank = text.rfind("\n\n", 0, limit)
        if blank >= 0:
            return blank + 2
        for index in range(limit - 1, -1, -1):
            if text[index] in _STREAM_BREAKS:
                return index + 1
        return _MAX_SEGMENT if len(text) >= _MAX_SEGMENT else 0

    async def _emit(self, text: str) -> None:
        body = text.strip()
        if not body:
            return
        if self._last_sent:
            gap = self._interval - (time.monotonic() - self._last_sent)
            if gap > 0:
                await asyncio.sleep(gap)
        try:
            sent = bool(await self._send_text(self._key, body))
        except Exception as exc:  # noqa: BLE001 - 单段失败不影响后续
            logger.warning("%s: 主动推送流式发送失败：%s", LOG_PREFIX, exc)
            sent = False
        self._last_sent = time.monotonic()
        if sent:
            self.sent_chars += len(body)
            self.messages += 1


#: 这些事件说明「服务端主动开了一轮」（本地回合会持锁，进不到这里）。
_TRIGGER_KINDS = frozenset(
    {
        acp.UPDATE_USER_MESSAGE,
        acp.UPDATE_USER_MESSAGE_CHUNK,
        acp.UPDATE_AGENT_MESSAGE,
        acp.UPDATE_AGENT_MESSAGE_CHUNK,
        acp.UPDATE_AGENT_THOUGHT,
        acp.UPDATE_AGENT_THOUGHT_CHUNK,
        acp.UPDATE_TOOL_CALL,
    }
)


@dataclass
class _ActiveTurn:
    """一条正在收集的主动回合。"""

    session: GroupSession
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None


class ProactiveRelay:
    """监听所有会话，把「没有本地 prompt 的回合」转发到群聊。

    Args:
        settings_provider: 返回执行器参数（``echo_events`` / ``turn_timeout_sec``）。
        send_text: ``(unified_msg_origin, text) -> 是否发送成功``；``None`` 表示只收集不发送。
    """

    def __init__(
        self,
        *,
        settings_provider: Callable[[], runner_mod.RunnerSettings],
        send_text: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._send_text = send_text
        self._pool: SessionPool | None = None
        self._active: dict[str, _ActiveTurn] = {}

    # ── 绑定 / 解绑 ───────────────────────────────────────
    def bind(self, pool: SessionPool) -> None:
        """接管连接池：给已有连接补挂监听，之后新建的连接自动挂。"""
        self._pool = pool
        pool.set_session_listener(self.attach)

    async def stop(self) -> None:
        """停止所有收集任务（插件停用 / 重载时调用）。"""
        active = list(self._active.values())
        self._active.clear()
        for turn in active:
            if turn.task is not None and not turn.task.done():
                turn.task.cancel()
        if active:
            await asyncio.gather(
                *(turn.task for turn in active if turn.task is not None),
                return_exceptions=True,
            )
        self._pool = None

    def cancel(self, key: str) -> None:
        """停掉某个群正在收集的主动回合（强制回收连接时用）。"""
        turn = self._active.get(key)
        if turn is None:
            return
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()

    def attach(self, session: GroupSession) -> None:
        """给一条会话挂常驻通知监听（幂等）。"""
        if session._proactive_listening:
            return
        session._proactive_listening = True

        def _on_notification(_method: str, params: dict) -> None:
            try:
                self._on_update(session, params)
            except Exception as exc:  # noqa: BLE001 - 监听里不能把连接搞崩
                logger.warning(
                    "%s: 主动推送处理通知失败（群 %s）：%s",
                    LOG_PREFIX,
                    session.key,
                    exc,
                )

        session.client.subscribe(_on_notification)

    # ── 通知分流 ─────────────────────────────────────────
    def _on_update(self, session: GroupSession, params: Any) -> None:
        if not isinstance(params, dict):
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        update_session = update.get("sessionId") or params.get("sessionId")
        if (
            isinstance(update_session, str)
            and update_session
            and session.session_id
            and update_session != session.session_id
        ):
            return

        turn = self._active.get(session.key)
        if turn is not None:
            turn.queue.put_nowait(update)
            return

        if not self._should_start(session, update):
            return

        turn = _ActiveTurn(session=session)
        self._active[session.key] = turn
        # 标记这一轮由主动推送接管：空闲回收任务据此跳过，别把连接从中间收走。
        session.proactive_active = True
        session.touch()
        turn.queue.put_nowait(update)
        turn.task = asyncio.create_task(
            self._run_turn(turn), name=f"0528v2-proactive-{session.key}"
        )

    def _should_start(self, session: GroupSession, update: dict) -> bool:
        """判断这条通知是否意味着「服务端主动开了一轮」。"""
        if self._send_text is None or self._pool is None:
            return False
        if not session.connected or not session.session_id:
            return False
        if session.external_relay:
            # 唤醒钩子正在接管这一轮（没有本地连接时由它负责转发）
            return False
        if session.lock.locked():
            # 本地回合（AstrBot 消息 / /acmd / 唤醒 relay）正持有会话锁
            return False
        kind = update.get("sessionUpdate")
        if kind == acp.UPDATE_STATE:
            return update.get("state") == acp.STATE_RUNNING
        return kind in _TRIGGER_KINDS

    # ── 收集 / 发送 ───────────────────────────────────────
    async def _run_turn(self, turn: _ActiveTurn) -> None:
        """收集一轮主动回合；无论成功失败都清掉活动状态。"""
        try:
            await self._collect_and_send(turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把连接搞崩
            logger.warning(
                "%s: 主动推送群 %s 失败：%s", LOG_PREFIX, turn.session.key, exc
            )
        finally:
            if self._active.get(turn.session.key) is turn:
                del self._active[turn.session.key]
            turn.session.proactive_active = False
            turn.session.clear_external_grant()

    async def _collect_and_send(self, turn: _ActiveTurn) -> None:
        """收集一轮主动回合，结束后把正文发到群聊。"""
        session = turn.session
        key = session.key
        try:
            settings = self._settings_provider()
        except Exception:  # noqa: BLE001 - 读不到配置就用默认值
            settings = runner_mod.RunnerSettings.from_config(None, None)

        stream = StreamBuffer(self._send, key)
        echo = runner_mod.ProgressEcho(enabled=settings.echo_events)
        timeout = max(5.0, float(settings.turn_timeout_sec))
        deadline = time.monotonic() + timeout
        stop_reason = ""
        error_msg = ""
        last_state = ""
        timed_out = False
        disconnected = False
        sent_hint = False
        #: 自动批准序号起点：本次 requires_action 若已直通就不发等待提示。
        approval_seq = int(getattr(session, "approval_seq", 0))

        while True:
            if not session.connected and turn.queue.empty():
                disconnected = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                update = await asyncio.wait_for(
                    turn.queue.get(), timeout=min(1.0, remaining)
                )
            except asyncio.TimeoutError:
                continue

            kind = str(update.get("sessionUpdate") or "")
            highlight = echo.describe(update)
            if highlight is not None:
                await stream.send_now(highlight)

            if kind in (acp.UPDATE_AGENT_MESSAGE_CHUNK, acp.UPDATE_AGENT_MESSAGE):
                text = acp.extract_text(update.get("content"))
                if text:
                    await stream.feed(
                        str(update.get("messageId") or "msg_0"),
                        text,
                        replace=(kind == acp.UPDATE_AGENT_MESSAGE),
                    )
            elif kind == acp.UPDATE_STATE:
                state = str(update.get("state") or "")
                if state == acp.STATE_IDLE:
                    stop_reason = str(update.get("stopReason") or "")
                    error_msg = str(update.get(acp.KEY_ERROR_MSG) or "")
                    break
                if state == acp.STATE_REQUIRES_ACTION:
                    req_state, _pending = await approval_mod.wait_permission_state(
                        session, approval_seq
                    )
                    if req_state == "auto":
                        approval_seq = int(
                            getattr(session, "approval_seq", approval_seq)
                        )
                        sent_hint = False
                    elif last_state != state and not sent_hint:
                        sent_hint = True
                        await stream.send_now(runner_mod.permission_hint(session))
                last_state = state

        if stop_reason == acp.STOP_REASON_CANCELLED:
            await stream.send_now("🛑 本轮已终止")
        if error_msg:
            await stream.send_now(f"⚠️ {error_msg}")
        if timed_out:
            await stream.send_now("⚠️ 主动推送等待本轮结束超时，以上是已收到的内容。")
        if disconnected:
            await stream.send_now("⚠️ 连接已断开，以上是已收到的内容。")
        await stream.flush()
        if stream.messages:
            logger.info(
                "%s: 已主动流式推送群 %s 的回合（%d 段 / %d 字，stopReason=%s）",
                LOG_PREFIX,
                key,
                stream.messages,
                stream.sent_chars,
                stop_reason or "未知",
            )
        elif disconnected or timed_out:
            logger.warning(
                "%s: 群 %s 的主动回合没有可推送内容（stopReason=%s）",
                LOG_PREFIX,
                key,
                stop_reason or "未知",
            )

    async def _send(self, unified_msg_origin: str, text: str) -> bool:
        """把一段文本主动发到聊天（失败只记日志）。"""
        if not text.strip() or self._send_text is None:
            return False
        try:
            return bool(await self._send_text(unified_msg_origin, text))
        except Exception as exc:  # noqa: BLE001 - 发送失败不该拖垮监听
            logger.warning(
                "%s: 主动推送到 %s 失败：%s", LOG_PREFIX, unified_msg_origin, exc
            )
            return False


__all__ = ["LOG_PREFIX", "ProactiveRelay", "StreamBuffer"]
