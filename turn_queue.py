"""群内并发闸门：一个群同时只跑一轮，别人的插话排队攒批。

AstrBot 的第三方执行器子阶段（``ThirdPartyAgentSubStage``）**不做任何会话级串行**
（内核那把 ``session_lock_manager`` 只用在自带 Agent 那条路上），所以同一个群里几个人
同时说话时，会有多个 ``step_until_done`` 一起来到执行器：

* 群里没人在跑 → 本条消息立刻开跑（:data:`ACTION_RUN`，照旧「一条消息一轮」）；
* 群里正跑着一轮 → 本条消息进等待队列；这一轮结束时，队列里**第一个**等到的任务拿到
  攒批后的那条提示词（:data:`ACTION_MERGED`）接着跑，其余的（:data:`ACTION_ABSORBED`）
  什么都不发 —— 他们的发言已经在那一批里了。

于是「第一个说话的人」照常立刻被处理、立刻拿到回复，插话的人被合成一轮（一次思考、
一条回复），不会为每句插话各跑一轮、也不会互相打断。

排队的插话会**顺手记进 SQLite**（:class:`~acp.store.SessionStore` 的 ``turn_queue`` 表）：
AstrBot 被 kill、机器断电之类的情况下内存队列会没掉，但记录还在 —— 下次启动
:meth:`TurnQueue.restore` 把它们捞回队列，等这个群再来消息时并进那一批，谁也不丢。

用法（在 :class:`~agent_runner.Alkaid0Runner` 里）::

    slot = await queue.enter(key, prompt_text, speaker="张三")
    if slot.action == ACTION_ABSORBED:
        ...  # 已被并进这一批：不占远端、也不回话，**也不用 leave**
        return
    try:
        ...  # 用 slot.text 发这一轮（ACTION_MERGED 时它是攒批后的文本）
    finally:
        await queue.leave(key)

``leave`` 由**拿到带队权的那一轮**调用（``ACTION_RUN`` / ``ACTION_MERGED``）并放在
``finally`` 里：它不只是「放锁」，还负责把队列里的插话交给下一个带队者，漏了就卡死后面
的人。被并进别人那一批的（``ACTION_ABSORBED``）**不能**调 —— 闸门不在它手里，调了等于
把正在跑的那一轮的门放开，第三条消息就会跟它并排跑。

落库只是「锦上添花」：库读写失败一律只告警，队列照常在内存里工作（与会话记录同一个态度）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from astrbot.api import logger

from .acp.store import SessionStore

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"

#: 本条消息立刻开跑（群里没人在跑）。
ACTION_RUN = "run"
#: 本条消息是攒批的带队者：``text`` 是合并后的提示词。
ACTION_MERGED = "merged"
#: 本条消息已经被并进别人那一批了：什么也不用发。
ACTION_ABSORBED = "absorbed"

#: 攒批提示词的开头（``{count}`` 是这一批的条数）。
MERGE_HEADER = "（群里攒了 {count} 条消息，一起看一下）"


@dataclass(frozen=True)
class TurnSlot:
    """一次「进闸门」的结果。

    Attributes:
        action: :data:`ACTION_RUN` / :data:`ACTION_MERGED` / :data:`ACTION_ABSORBED`。
        text: 这一轮该发给远端的提示词（``ACTION_RUN`` 时是原话，
            ``ACTION_MERGED`` 时是攒批合并后的文本）。
        speakers: 这一批里发言的人（攒批时用来标出处，也便于日志）。
        senders: 这一批里每条消息的 ``(发言人, 发送者 ID)``，顺序与 ``text`` 里的发言
            对应 —— 注入 ``[Reminder: @昵称(id)]`` 时按它逐条给（见 :mod:`agent_runner`）。
    """

    action: str
    text: str = ""
    speakers: tuple[str, ...] = ()
    senders: tuple[tuple[str, str], ...] = ()


def strip_leading_slash(text: str) -> str:
    """去掉消息最前面的斜杠（发给 alkaid0 之前用，纯函数）。

    ``/hello`` → ``hello``、``/ hello`` → ``hello``、``//a`` → ``a``；
    没有前导斜杠的原样返回（顺手去掉两端空白），全是斜杠 / 空白时返回空串。
    """

    body = (text or "").strip()
    if not body.startswith("/"):
        return body
    return body.lstrip("/").strip()


def merge_prompt(items: list[_Pending]) -> str:
    """把攒下的几条插话合成一条提示词（纯函数，便于自测）。

    形如::

        （群里攒了 2 条消息，一起看一下）
        [张三] 帮我看看 A
        [李四] 顺便看下 B

    没取到发言人的（``label`` 为空）就只放正文。
    """
    lines = [MERGE_HEADER.format(count=len(items))]
    for item in items:
        body = strip_leading_slash(item.text)
        if not body:
            continue
        lines.append(f"[{item.label}] {body}" if item.label else body)
    return "\n".join(lines)


@dataclass
class _Pending:
    """一条排队等着被处理的插话。

    ``future`` 是那条消息的等待者；为 ``None`` 表示这是**从库里恢复回来的孤儿**
    （当初那条消息的事件早就没了），只能被并进别人的一批，不会再单独回话。
    ``item_id`` 是它在 ``turn_queue`` 表里的行号，没落库（或写失败）时是 ``None``。
    """

    label: str
    text: str
    sender_id: str = ""
    future: asyncio.Future | None = field(default=None, repr=False)
    item_id: int | None = None


def _senders(items: list[_Pending]) -> tuple[tuple[str, str], ...]:
    """把一批插话的 ``(发言人, 发送者 ID)`` 抽出来（两个都空的不算）。"""
    return tuple(
        (item.label, item.sender_id) for item in items if item.label or item.sender_id
    )


class TurnQueue:
    """群键 → 「谁在跑、谁在排队」的闸门；排队时顺手把插话记进 SQLite。

    只在事件循环里用（同一次读写之间没有 ``await``，因此不需要额外的锁）。

    Args:
        store: 插话的落库位置（:class:`~acp.store.SessionStore`）；``None`` 表示不落库
            （自测、或不需要留痕的场景），此时行为与纯内存队列完全一致。
    """

    def __init__(self, store: SessionStore | None = None) -> None:
        self._store = store
        #: 正在跑一轮的群键。
        self._active: set[str] = set()
        #: 群键 → 排着队的插话（按到达顺序）。
        self._waiting: dict[str, list[_Pending]] = {}

    # ── 查询（状态命令 / 日志用）─────────────────────────
    def busy(self, key: str) -> bool:
        """这个群当前是否有一轮在跑。"""
        return key in self._active

    def hold(self, key: str) -> bool:
        """外部回合（cron / 其它客户端）占住闸门：之后来的用户消息排队。

        与 :meth:`enter` 的区别是「只占门、不排队、没有提示词」——外部回合的正文
        由服务端推送，不走本地攒批。已经有一轮在跑时返回 ``False``（不抢占），
        调用方此时也不该在结束时 :meth:`leave`，否则会把别人的门放开。

        Returns:
            是否真的占到了闸门。
        """
        if key in self._active:
            return False
        self._active.add(key)
        logger.info("%s: 群 %s 由外部回合计时占用闸门（用户消息会先排队）", LOG_PREFIX, key)
        return True

    def waiting(self, key: str) -> int:
        """这个群当前排着几条插话。"""
        return len(self._waiting.get(key, ()))

    def snapshot(self) -> dict[str, int]:
        """所有有插话排队的群键 → 条数（状态命令用）。"""
        return {key: len(items) for key, items in self._waiting.items() if items}

    # ── 落库 / 恢复 ─────────────────────────────────────
    def restore(self) -> int:
        """把库里记着的插话捞回队列（启动时调一次），返回捞回了几条。

        捞回来的都是孤儿（``future=None``）：它们原来那条消息的事件早没了，所以不会再
        单独回话 —— 等这个群下次来消息，由那条消息带队把它们一起发出去。库读不出来
        （坏库 / 没配库）就当没有，返回 0。
        """
        if self._store is None:
            return 0
        rows = self._store.pending_entries()
        if not rows:
            return 0
        for row in rows:
            key = str(row.get("key") or "")
            if not key:
                continue
            item_id = row.get("id")
            self._waiting.setdefault(key, []).append(
                _Pending(
                    label=str(row.get("label") or ""),
                    text=str(row.get("text") or ""),
                    item_id=int(item_id) if item_id is not None else None,
                )
            )
        total = sum(len(items) for items in self._waiting.values())
        logger.info(
            "%s: 从库里恢复 %d 条排队插话（%d 个群：%s）",
            LOG_PREFIX,
            total,
            len(self._waiting),
            "、".join(f"{key} {len(items)} 条" for key, items in self._waiting.items()),
        )
        return total

    def _remember(self, key: str, text: str, label: str) -> int | None:
        """把一条插话记进库，拿到行号；没库 / 写失败返回 None。"""
        if self._store is None:
            return None
        return self._store.add_pending(key, text, label)

    def _forget(self, items: list[_Pending]) -> None:
        """把已经安排掉的插话从库里删掉（没落库的忽略）。"""
        if self._store is None:
            return
        ids = [item.item_id for item in items if item.item_id is not None]
        if ids:
            self._store.forget_pending(ids)

    # ── 进 / 出 ─────────────────────────────────────────
    async def enter(
        self,
        key: str,
        text: str,
        *,
        speaker: str = "",
        sender_id: str = "",
    ) -> TurnSlot:
        """申请进闸门：立刻开跑，或者排队等上一轮结束。

        Args:
            key: 群/会话键（一般用 ``event.unified_msg_origin``）。
            text: 本条消息的提示词。
            speaker: 发言的人（攒批时标出处）；取不到就留空。
            sender_id: 发言者的 ID（注入 ``[Reminder: @昵称(id)]`` 用）；取不到就留空。

        Returns:
            :class:`TurnSlot` —— 见类文档里那段用法。

        Raises:
            asyncio.CancelledError: 等待期间本任务被取消（会把自己从队列里摘掉）。
        """
        if key in self._active:
            return await self._wait(key, text, speaker, sender_id)

        recovered = self._waiting.pop(key, None) or []
        self._active.add(key)
        if not recovered:
            mine = _Pending(label=speaker, text=text, sender_id=sender_id)
            return TurnSlot(
                ACTION_RUN, text, (speaker,) if speaker else (), _senders([mine])
            )

        # 这个群还留着上次（服务端被停掉之前）没来得及处理的插话：本条消息带队，
        # 把它们并进同一批发出去 —— 谁也不丢。
        logger.info(
            "%s: 群 %s 带着 %d 条恢复回来的插话开跑",
            LOG_PREFIX,
            key,
            len(recovered),
        )
        self._forget(recovered)
        return _lead(
            [*recovered, _Pending(label=speaker, text=text, sender_id=sender_id)]
        )

    async def leave(self, key: str) -> None:
        """一轮结束：把攒下的插话合成一批交给下一个带队者，没人等就让闸门空出来。"""
        if key not in self._active:
            return
        waiting = self._waiting.pop(key, None) or []
        while (
            waiting
            and waiting[0].future is not None
            and waiting[0].future.cancelled()
        ):
            # 等到一半被撤掉的（停用 / 超时）别带队，顺手把它那条记录也删了
            self._forget([waiting.pop(0)])
        if not waiting:
            self._active.discard(key)
            return

        leader = waiting[0]
        if leader.future is None:
            # 队首是恢复回来、没人接棒的孤儿：原样放回队列、闸门让出来，
            # 等下一个消息来带队（记录先别删，下次启动还能再捞）
            self._waiting.setdefault(key, []).extend(waiting)
            self._active.discard(key)
            return

        self._forget(waiting)  # 这一批马上要发出去，别再当成「没处理的插话」
        text = merge_prompt(waiting)
        speakers = tuple(item.label for item in waiting if item.label)
        if not leader.future.done():
            leader.future.set_result(
                TurnSlot(ACTION_MERGED, text, speakers, _senders(waiting))
            )
        for item in waiting[1:]:
            if item.future is not None and not item.future.done():
                item.future.set_result(TurnSlot(ACTION_ABSORBED))
        logger.info(
            "%s: 群 %s 上一轮结束，把 %d 条插话攒成一批接着跑（%s）",
            LOG_PREFIX,
            key,
            len(waiting),
            "、".join(speakers) or "匿名",
        )

    # ── 内部 ─────────────────────────────────────────────
    async def _wait(
        self, key: str, text: str, speaker: str, sender_id: str
    ) -> TurnSlot:
        """群里正跑着一轮：把这条插话记进队列（顺手落库）然后等这一轮结束。"""
        pending = _Pending(
            label=speaker,
            text=text,
            sender_id=sender_id,
            future=asyncio.get_running_loop().create_future(),
            item_id=self._remember(key, text, speaker),
        )
        queue = self._waiting.setdefault(key, [])
        queue.append(pending)
        logger.info(
            "%s: 群 %s 正在跑一轮，%s的插话先排队（当前 %d 条）",
            LOG_PREFIX,
            key,
            f"{speaker} " if speaker else "",
            len(queue),
        )
        try:
            return await pending.future
        except asyncio.CancelledError:
            # 消息被内核撤了（停用 / 超时）：把自己摘掉，别占着下一批的位置
            if pending in queue:
                queue.remove(pending)
            if not queue:
                self._waiting.pop(key, None)
            self._forget([pending])
            raise


def _lead(items: list[_Pending]) -> TurnSlot:
    """把一批插话合成「带队者」的那一轮（攒批提示词 + 这批的发言人 / 发送者 ID）。"""
    return TurnSlot(
        ACTION_MERGED,
        merge_prompt(items),
        tuple(item.label for item in items if item.label),
        _senders(items),
    )
