"""把回复文本里的 at 写法解析成真正的 OneBot at。

支持三种写法（``name`` 只是装饰，只取括号里的号码）：

* ``@name(qq_number)`` —— 全角括号也认；
* ``@qq_number``；
* ``@(qq_number)``。

解析结果是一串 ``("text", 文本)`` / ``("at", QQ号)`` 片段；发送时用
:func:`parts_to_chain` 换成 AstrBot 的 ``At`` 组件 —— aiocqhttp 会把它写成
OneBot 的 ``at`` 段，也就是 CQ 码 ``[CQ:at,qq=...]``。

**只在上游是 OneBot、并且当前是群聊时启用**（:func:`supported`）：私聊里 at
没有意义，其它平台也不认 OneBot 的 at 段。

正文是流式到达的，``@张三(123`` 可能被拆成两段；流式预览要让
:class:`AtFilter` 把可能还没收完整的 at 先压住，收尾时 :meth:`AtFilter.finish`
再吐出来。
"""

from __future__ import annotations

import re
import typing as T

from astrbot.core.message.message_event_result import MessageChain

#: 上游平台名：AstrBot 的 aiocqhttp 适配器就是 OneBot v11；另外几个写法留给
#: 可能出现的 OneBot 适配器别名。
ONEBOT_PLATFORMS = frozenset({"aiocqhttp", "onebot", "onebot11", "onebot_v11"})

#: 一个 QQ 号：5–12 位数字（QQ 号至少 5 位，12 位留足余量）。
#: 只认 ASCII 数字：``\d`` 会把全角数字也算进来，那不是合法的 OneBot 号码。
_QQ = r"[0-9]{5,12}"

#: ``@name(qq)`` 里名字的最大长度（与 :data:`AT_PATTERN` 保持一致）。
_NAME_MAX = 64

#: 流式时最多为「还没出现括号的名字」压多少字；超过就肯定不是 at 了。
_PENDING_NAME_MAX = 32

#: ``@昵称(QQ号)`` / ``@(QQ号)`` / ``@QQ号``。``@`` 前面不能是英文 / 数字 /
#: 下划线，避免把邮箱和普通单词里的 ``@`` 也吃掉；名字里不出现 ``@`` 和括号。
AT_PATTERN = re.compile(
    rf"(?<![0-9A-Za-z_])@(?:"
    rf"[^\s@()（）][^@()（）\r\n]{{0,{_NAME_MAX - 1}}}?[（(]\s*(?P<paren>{_QQ})\s*[)）]"
    rf"|\(\s*(?P<bare>{_QQ})\s*\)"
    rf"|(?P<plain>{_QQ})(?![0-9])"
    rf")"
)

_DIGITS = re.compile(r"[0-9]*")
_SPACES = re.compile(r"\s*")

#: 解析片段：``("text", 文本)``、``("at", QQ号)`` 或 ``("component", 组件)``。
Part = tuple[str, T.Any]


# ── 平台判定 ──────────────────────────────────────────────────────────
def _call_str(event: T.Any, name: str) -> str:
    """调用事件的取字符串方法（方法缺席或返回值不是字符串时返回空串）。"""
    method = getattr(event, name, None)
    if not callable(method):
        return ""
    try:
        value = method()
    except Exception:  # noqa: BLE001 - 事件实现千奇百怪，取不到就当空
        return ""
    return value.strip() if isinstance(value, str) else ""


def platform_of(event: T.Any) -> str:
    """事件所属的平台名（取不到就返回空串）。"""
    return _call_str(event, "get_platform_name").lower()


def supported(event: T.Any) -> bool:
    """这个事件能不能做 at 解析：OneBot 上游 + 群聊。

    私聊没有群号（``get_group_id()`` 为空），at 出去也没有对象，所以不生效。
    """
    return bool(event is not None and platform_of(event) in ONEBOT_PLATFORMS
                and _call_str(event, "get_group_id"))


# ── 文本解析 ──────────────────────────────────────────────────────────
def _qq_of(match: re.Match[str]) -> str:
    """从正则匹配里取 QQ 号（三个分支只会命中一个）。"""
    return match.group("paren") or match.group("bare") or match.group("plain") or ""


def parts_of(text: str) -> list[Part]:
    """按出现顺序把文本拆成 ``("text", ...)`` / ``("at", ...)`` 片段。"""
    if not text:
        return []
    parts: list[Part] = []
    position = 0
    for match in AT_PATTERN.finditer(text):
        if match.start() > position:
            parts.append(("text", text[position : match.start()]))
        qq = _qq_of(match)
        if qq:
            parts.append(("at", qq))
        position = match.end()
    if position < len(text):
        parts.append(("text", text[position:]))
    return parts


def parts_to_chain(
    parts: T.Iterable[Part], chain: MessageChain | None = None
) -> MessageChain:
    """把片段拼成消息链：文本是 ``Plain``，at / component 是现成组件。"""
    result = chain if chain is not None else MessageChain()
    for kind, value in parts:
        if kind == "at":
            result.at("", value)
        elif kind == "component":
            if value is not None:
                result.chain.append(value)
        elif value:
            result.message(value)
    return result


def message_chain(text: str, chain: MessageChain | None = None) -> MessageChain:
    """把一段正文转成消息链（非流式路径用）。"""
    return parts_to_chain(parts_of(text), chain)


# ── 流式分片 ──────────────────────────────────────────────────────────
def _word_char(char: str) -> bool:
    """是否算「单词字符」（英文 at 边界判断用）。"""
    return char.isascii() and (char.isalnum() or char == "_")


def _maybe_paren_prefix(inner: str) -> bool:
    """``(`` 之后的片段是否还可能是还没收完的 ``QQ号)``。"""
    head = _SPACES.match(inner)
    rest = inner[head.end() :] if head else inner
    digits_match = _DIGITS.match(rest)
    digits = digits_match.group(0) if digits_match else ""
    rest = rest[len(digits) :]
    if len(digits) > 12:
        return False
    stripped = rest.strip()
    if not stripped:
        return True
    # 号码和右括号都齐了（或括号里根本不是号码）：前者交给 parts_of，
    # 后者肯定不是 at；两种情况都不需要再压着。
    return False


def _maybe_name_prefix(body: str) -> bool:
    """``@`` 之后的名字片段是否还可能是尚未收完的 ``名字(QQ号)``。"""
    if not body or body[0].isspace():
        return False
    for index, char in enumerate(body):
        if char in "@\r\n":
            return False
        if char in "(（":
            return _maybe_paren_prefix(body[index + 1 :])
        if char in ")）":
            return False
    return len(body) <= _PENDING_NAME_MAX


def _maybe_at_prefix(snippet: str) -> bool:
    """``snippet``（以 ``@`` 开头）是否可能是一个还没收完的 at。"""
    if not snippet.startswith("@"):
        return False
    body = snippet[1:]
    if not body:
        return True
    first = body[0]
    if first in "(（":
        return _maybe_paren_prefix(body[1:])
    if first.isascii() and first.isdigit():
        digits_match = _DIGITS.match(body)
        digits = digits_match.group(0) if digits_match else ""
        if len(digits) > 12:
            return False
        if len(digits) >= 5:
            # 已经是完整的 ``@QQ号``：按 atom 立刻解析，后面跟文字也一样。
            return False
        return True
    return _maybe_name_prefix(body)


def _pending_len(text: str) -> int:
    """尾部还有多长可能是「没收完的 at」（0 表示不用压）。"""
    limit = min(len(text), _NAME_MAX + 24)
    for offset in range(1, limit + 1):
        index = len(text) - offset
        if text[index] != "@":
            continue
        if index > 0 and _word_char(text[index - 1]):
            continue
        if _maybe_at_prefix(text[index:]):
            return offset
    return 0


class AtFilter:
    """流式预览用的 at 清理器。

    增量到达时，把完整的 at 换成 ``("at", QQ号)``，并把尾部可能还没收完的
    ``@…`` 先压住；:meth:`feed` 返回现在就能安全发出去的部分，
    :meth:`finish` 在收尾时把压着的文本原样吐出。
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[Part]:
        """喂一段增量，返回现在就能发出去的片段。"""
        buf = self._buffer + (text or "")
        keep = _pending_len(buf)
        cut = len(buf) - keep
        ready = buf[:cut]
        self._buffer = buf[cut:]
        return parts_of(ready)

    def finish(self) -> list[Part]:
        """收尾：把还压着的半截文本原样解析（含完整的 at）。"""
        rest, self._buffer = self._buffer, ""
        return parts_of(rest)
