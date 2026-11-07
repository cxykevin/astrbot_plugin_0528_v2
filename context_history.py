"""上下文感知：从最近的群聊记录里挑 n 条上文补进提示词。

规则（用户要求）：

* 从最新的一条往回数，凑够 ``n`` 条；
* 已经在**累计队列**（``turn_queue`` 的等待队列）里的消息不参与计数；
* 字数超过 ``k`` 的消息跳过、不计数，但最多只遍历 ``2n`` 条；
* 当前这一轮真正要发出去的消息（攒批里的那几条）本来就在提示词里，
  不参与上下文筛选，也不受长消息跳过规则影响。

这里只做纯函数（筛选 + 渲染），消息的落库 / 读取在 :mod:`acp.store`，
调用方在 :mod:`agent_runner`。
"""

from __future__ import annotations

import typing as T
from dataclasses import dataclass

#: 上下文块的开头。
CONTEXT_HEADER = "[更早的群聊记录（仅供参考）]"
#: 上下文块的结尾。
CONTEXT_FOOTER = "[/更早的群聊记录]"


@dataclass(frozen=True)
class ContextMessage:
    """一条可供上下文使用的历史消息。"""

    message_id: str = ""
    speaker: str = ""
    text: str = ""
    image_count: int = 0

    @property
    def body(self) -> str:
        """给模型看的正文：空文本但有图片时用 ``[图片]`` 占位。"""
        body = (self.text or "").strip()
        if not body:
            return "[图片]" if self.image_count else ""
        if self.image_count:
            return f"{body}（附图 {self.image_count} 张）"
        return body


def select_context(
    messages: T.Iterable[ContextMessage],
    *,
    limit: int,
    max_chars: int,
    exclude_ids: T.Container[str] = frozenset(),
    queued_ids: T.Container[str] = frozenset(),
) -> list[ContextMessage]:
    """从「新的在前」的历史消息里挑最多 ``limit`` 条上文（返回按时间正序）。

    Args:
        messages: 历史消息，**新的在前**。
        limit: 最多带几条（``<= 0`` 表示关闭上下文感知）。
        max_chars: 超过这么多字的正文跳过、不计数；``<= 0`` 表示不限字数。
        exclude_ids: 本轮已经要发出去的消息 ID（当前消息 / 攒批里的消息），不算上文。
        queued_ids: 还在累计队列里等着的消息 ID，计数时跳过。

    Returns:
        选中的消息，按时间正序（最旧的在前），方便直接拼到提示词前面。
    """
    if limit <= 0:
        return []

    excluded = {str(item) for item in exclude_ids if str(item)}
    queued = {str(item) for item in queued_ids if str(item)}
    picked: list[ContextMessage] = []
    seen: set[str] = set()
    scanned = 0
    budget = max(1, limit * 2)

    for message in messages:
        mid = str(message.message_id or "").strip()
        if mid:
            if mid in seen:
                continue
            seen.add(mid)
            # 本轮真正要发出去的消息本来就在提示词里：完全跳过，也不占 2n 的遍历额度。
            if mid in excluded:
                continue
        if scanned >= budget:
            break
        scanned += 1
        if mid and mid in queued:
            continue
        body = message.body
        if not body:
            continue
        if max_chars > 0 and len(body) > max_chars:
            continue
        picked.append(message)
        if len(picked) >= limit:
            break

    picked.reverse()
    return picked


def render_context(messages: T.Sequence[ContextMessage]) -> str:
    """把选中的上文拼成一段带边界标记的提示词；没有消息时返回空串。"""
    if not messages:
        return ""
    lines = [CONTEXT_HEADER]
    for message in messages:
        body = message.body
        if not body:
            continue
        speaker = (message.speaker or "").strip()
        lines.append(f"[{speaker}] {body}" if speaker else body)
    if len(lines) == 1:
        return ""
    lines.append(CONTEXT_FOOTER)
    return "\n".join(lines)
