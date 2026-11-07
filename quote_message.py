"""引用消息（``Reply``）→ 提示词上下文 + 图片引用。

群里有人回复 / 引用一条更早的消息时，如果那条消息还没发给过当前 alkaid0 会话，
就把它的正文（以及图片）补进这一轮的提示词里 —— 否则模型只看到「你在说什么」，
看不到被引用的内容。

「发过没有」由 :mod:`acp.pool` 维护（每会话最近 32 条消息 ID）；这里只负责从事件里
认出引用的消息、拼出一段可读的上下文，并把图片引用交给调用方。
"""

from __future__ import annotations

import typing as T

from astrbot.core.message.components import At, Face, Image, Plain, Reply

#: 引用上下文的开头（带发送者时是 ``[引用消息｜昵称(QQ)]``）。
QUOTE_HEADER = "[引用消息{who}]"
#: 引用上下文的结尾。
QUOTE_FOOTER = "[/引用消息]"

#: 非文本组件在引用正文里的占位（按 ``ComponentType`` 取值）。
_COMPONENT_LABELS = {
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Forward": "[合并转发]",
    "Json": "[卡片]",
    "Share": "[分享]",
    "Poke": "[戳一戳]",
    "Dice": "[骰子]",
    "RPS": "[猜拳]",
    "Shake": "[窗口抖动]",
}


def message_id_of(event: T.Any) -> str:
    """取消息事件的消息 ID（取不到时返回空串）。"""
    obj = getattr(event, "message_obj", None)
    mid = getattr(obj, "message_id", "") if obj is not None else ""
    return str(mid).strip() if mid is not None else ""


def find_reply(event: T.Any) -> Reply | None:
    """在事件的消息链里找第一条引用消息（``Reply``）；没有就返回 None。"""
    obj = getattr(event, "message_obj", None)
    chain = getattr(obj, "message", None) if obj is not None else None
    if not isinstance(chain, (list, tuple)):
        return None
    for component in chain:
        if isinstance(component, Reply):
            return component
    return None


def reply_id(reply: Reply | None) -> str:
    """引用消息指向的消息 ID（取不到时返回空串）。"""
    if reply is None:
        return ""
    mid = getattr(reply, "id", "")
    return str(mid).strip() if mid is not None else ""


def _component_text(component: T.Any) -> str:
    """把一个消息段转成适合塞进引用上下文的短文本。"""
    if isinstance(component, Plain):
        return str(getattr(component, "text", "") or "")
    if isinstance(component, At):
        name = str(getattr(component, "name", "") or "").strip()
        qq = str(getattr(component, "qq", "") or "").strip()
        if name:
            return f"@{name}"
        return f"@{qq}" if qq else "@"
    if isinstance(component, Face):
        return "[表情]"
    kind = getattr(getattr(component, "type", None), "value", "")
    return _COMPONENT_LABELS.get(str(kind), "")


def reply_text(reply: Reply) -> str:
    """引用消息的正文：优先用它解析好的 ``message_str``，否则从消息链拼。"""
    text = str(getattr(reply, "message_str", "") or "").strip()
    if text:
        return text
    chain = getattr(reply, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return ""
    return "".join(
        _component_text(component)
        for component in chain
        if not isinstance(component, (Image, Reply))
    ).strip()


def reply_image_refs(reply: Reply) -> tuple[str, ...]:
    """引用消息里的图片引用（按出现顺序去重；交给 ``resolve_prompt_images`` 解析）。"""
    chain = getattr(reply, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return ()
    refs: list[str] = []
    seen: set[str] = set()
    for component in chain:
        if not isinstance(component, Image):
            continue
        ref = (
            getattr(component, "url", "")
            or getattr(component, "file", "")
            or getattr(component, "path", "")
            or ""
        )
        text = str(ref).strip()
        if text and text not in seen:
            seen.add(text)
            refs.append(text)
    return tuple(refs)


def _sender_label(reply: Reply) -> str:
    """引用的发送者：``昵称(QQ)``，缺什么省什么。"""
    name = str(getattr(reply, "sender_nickname", "") or "").strip()
    sid = str(getattr(reply, "sender_id", "") or "").strip()
    if name and sid and name != sid:
        return f"{name}({sid})"
    return name or sid


def quote_context(
    event: T.Any, *, was_sent: T.Callable[[str], bool]
) -> tuple[str, tuple[str, ...]]:
    """生成引用上下文与图片引用。

    Args:
        event: AstrBot 消息事件。
        was_sent: 判断某条消息 ID 是否已经发给过当前会话（引用它就不必重复带）。

    Returns:
        ``(上下文文本, 引用里的图片引用)``。没有引用 / 已经发过 / 引用内容为空时返回
        ``("", ())``。
    """
    reply = find_reply(event)
    mid = reply_id(reply)
    if reply is None or not mid or was_sent(mid):
        return "", ()

    body = reply_text(reply)
    images = reply_image_refs(reply)
    if not body and not images:
        return "", ()

    label = _sender_label(reply)
    header = QUOTE_HEADER.format(who=f"｜{label}" if label else "")
    lines = [header, body or "[图片]"]
    if images:
        lines.append(f"（附图 {len(images)} 张）")
    lines.append(QUOTE_FOOTER)
    return "\n".join(lines), images
