"""把回复文本里的 OneBot CQ 码翻译成 AstrBot 组件。

开启 ``cq_parse_enable`` 后：

* 只认这几种：``at`` / ``face`` / ``poke`` / ``dice`` / ``rps`` / ``shake``；
* 其它 CQ 码（image、json、xml、合并转发、mface 等）一律从文本里删掉，不显示；
* 只有 OneBot 上游（aiocqhttp）才构造组件；其它平台只删除、不显示。

CQ 码里常见的转义（``&#91;`` / ``&#93;`` / ``&#44;`` / ``&amp;``）也认。
"""

from __future__ import annotations

import re
import typing as T

from astrbot.api.message_components import RPS, At, AtAll, Dice, Face, Poke, Shake

from . import at_parse

#: CQ 码开始 / 结束（原始形式与转义形式）。
_RAW_START = "[CQ:"
_RAW_END = "]"
_ESC_START = "&#91;CQ:"
_ESC_END = "&#93;"

#: 流式时最多为一个还没闭合的 CQ 码压多少字；超过就肯定不是 CQ 码。
_MAX_CQ_LEN = 2048

#: CQ 类型只允许这些字符。
_TYPE_RE = re.compile(r"[A-Za-z0-9_.-]*")

#: 完整的 CQ 码：``[CQ:type,key=value,...]`` 或转义后的
#: ``&#91;CQ:type,key=value,...&#93;``。
CQ_PATTERN = re.compile(
    r"(?:\[CQ:(?P<raw_type>[A-Za-z0-9_.-]+)(?P<raw_args>(?:,[^\]]*)?)\])"
    r"|(?:&#91;CQ:(?P<esc_type>[A-Za-z0-9_.-]+)"
    r"(?P<esc_args>(?:,(?:(?!&#93;).)*)?)&#93;)"
)


def platform_supported(event: T.Any) -> bool:
    """这个事件是不是 OneBot 上游（只有它认识 CQ 码对应的组件）。"""
    return (
        event is not None
        and at_parse.platform_of(event) in at_parse.ONEBOT_PLATFORMS
    )


# ── 参数解析 ──────────────────────────────────────────────────────────
def _unescape(text: str) -> str:
    """还原 CQ 码里的 HTML 实体转义。"""
    return (
        (text or "")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _parse_args(raw: str) -> dict[str, str]:
    """``,key=value,key=value`` → 字典（键大小写不敏感）。"""
    params: dict[str, str] = {}
    for item in (raw or "").lstrip(",").split(","):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key:
            params[key] = _unescape(value)
    return params


# ── CQ → 组件 ─────────────────────────────────────────────────────────
def _component(
    kind: str, params: T.Mapping[str, str], *, enabled: bool
) -> T.Any | None:
    """把一个 CQ 码转成组件；不处理的类型返回 ``None``（调用方会删掉它）。"""
    if not enabled:
        return None
    kind = kind.lower()
    if kind == "at":
        qq = (params.get("qq") or "").strip()
        if not qq:
            return None
        if qq.lower() == "all":
            return AtAll()
        return At(qq=qq) if qq.isdigit() else None
    if kind == "face":
        raw = (params.get("id") or "").strip()
        return Face(id=int(raw)) if raw.isdigit() else None
    if kind == "poke":
        target = (params.get("id") or params.get("qq") or "").strip()
        if not target:
            return None
        poke_type = (params.get("type") or "").strip() or None
        return Poke(poke_type=poke_type, id=target)
    if kind == "dice":
        return Dice()
    if kind == "rps":
        return RPS()
    if kind == "shake":
        return Shake()
    return None


def _match_parts(
    match: re.Match[str],
) -> tuple[str, dict[str, str]]:
    """从正则匹配里取出 ``(类型, 参数字典)``。"""
    kind = match.group("raw_type") or match.group("esc_type") or ""
    raw = match.group("raw_args") or match.group("esc_args") or ""
    return kind, _parse_args(raw)


def parts_of(
    text: str, *, components: bool = True
) -> list[at_parse.Part]:
    """按出现顺序拆文本：``("text", 文本)`` / ``("component", 组件)``。

    Args:
        text: 一段回复正文。
        components: 是否真的构造组件。非 OneBot 平台传 ``False`` —— 这时
            所有 CQ 码仍然会被删掉，只是不会变成组件。
    """
    if not text:
        return []
    parts: list[at_parse.Part] = []
    position = 0
    for match in CQ_PATTERN.finditer(text):
        if match.start() > position:
            parts.append(("text", text[position : match.start()]))
        kind, params = _match_parts(match)
        component = _component(kind, params, enabled=components)
        if component is not None:
            parts.append(("component", component))
        # 不支持的 CQ 码直接跳过：片段被删掉，不会显示原文。
        position = match.end()
    if position < len(text):
        parts.append(("text", text[position:]))
    return parts


# ── 流式分片 ──────────────────────────────────────────────────────────
def _pending_len(text: str) -> int:
    """尾部还有多长可能是「还没闭合的 CQ 码」（0 表示不用压）。"""
    limit = min(len(text), _MAX_CQ_LEN)
    window_start = len(text) - limit
    raw_start = text.rfind(_RAW_START, window_start)
    esc_start = text.rfind(_ESC_START, window_start)
    start = max(raw_start, esc_start)
    if start < 0:
        return 0
    tail = text[start:]
    if tail.startswith(_RAW_START):
        if _RAW_END in tail:
            return 0
        body = tail[len(_RAW_START) :]
    else:
        if _ESC_END in tail:
            return 0
        body = tail[len(_ESC_START) :]
    type_part = body.split(",", 1)[0]
    return len(tail) if _TYPE_RE.fullmatch(type_part) else 0


class CqFilter:
    """流式预览用的 CQ 码清理器。

    增量到达时，把完整 CQ 码转成组件（或删掉），并把尾部可能还没闭合的
    ``[CQ:…`` 先压住；收尾时 :meth:`finish` 把剩下的文本处理掉。
    """

    def __init__(self, *, components: bool = True) -> None:
        self.components = components
        self._buffer = ""

    def feed(self, text: str) -> list[at_parse.Part]:
        """喂一段增量，返回现在就能发出去的片段。"""
        buf = self._buffer + (text or "")
        keep = _pending_len(buf)
        cut = len(buf) - keep
        ready = buf[:cut]
        self._buffer = buf[cut:]
        return parts_of(ready, components=self.components)

    def finish(self) -> list[at_parse.Part]:
        """收尾：处理还压着的半截文本（未闭合的 CQ 码会原样留下）。"""
        rest, self._buffer = self._buffer, ""
        return parts_of(rest, components=self.components)
