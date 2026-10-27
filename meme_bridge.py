"""表情包管理器（``meme_manager``）联动 —— ``&&分类键&&`` 由本插件自己解析，再交给它取图。

**正文归本插件管**：回复原样发出去，只把 ``&&分类键&&`` 标记减掉（就是 agent 写的正文，
不经过 meme_manager 的清洗规则）。**标记也归本插件解析**：``&&…&&`` 里的内容就是分类键。

**取图只用它的底层接口**，不经过它的分类配置与选择模式（``compat_*`` /
``_make_meme_parser`` / 语义检索 / 概率 / 每轮张数一律不参与）：

* ``_get_category_meme_files(event, 分类键)`` —— 分类键**直接当目录名**，在当前绑定的
  表情包根目录下找图片（它内部只做路径安全校验，不校验分类是否登记在配置里）；
* ``_convert_to_gif(path)`` —— 静态图转 GIF；
* ``_send_meme_image(event, image)`` —— 按平台发送（base64 / 平台差异都在它那边处理）。

插件没安装 / 没启用（或版本太老、缺这三个方法）时 :func:`bridge_for` 返回 ``None``，
整条路径不生效 —— 标记会像以前一样原样留在回复里。
"""

from __future__ import annotations

import asyncio
import random
import re
import typing as T

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.core.message.message_event_result import MessageChain

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"

#: 表情包管理器的插件名（其 ``metadata.yaml`` 里的 ``name``）。
MEME_PLUGIN_NAME = "meme_manager"

#: 分类标签：``&&分类键&&``，中间不出现 ``&`` 或换行、长度 1..100。
MARKER_PATTERN = re.compile(r"&&[^&\r\n]{1,100}&&")

#: 取图 / 转换 / 发送要用的三个底层方法（缺一个就当版本不匹配，不联动）。
_REQUIRED_METHODS = ("_get_category_meme_files", "_convert_to_gif", "_send_meme_image")


# ── 标记解析（本插件自己实现，规则即「原生分类标签」）──────────────
def parse_markers(text: str) -> tuple[str, list[str]]:
    """解析正文里的分类标签，返回 ``(去掉标记的正文, 分类键列表)``。

    同一个分类写两次只取第一次（一个分类发一张图），顺序按原文出现顺序。

    Args:
        text: 一轮回复的正文（标记还在里面）。

    Returns:
        ``(cleaned, tags)``：正文去掉标记后的样子，以及按出现顺序去重后的分类键。
    """
    if not text:
        return "", []
    tags: list[str] = []
    seen: set[str] = set()

    def _drop(match: re.Match[str]) -> str:
        tag = match.group(0)[2:-2].strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
        return ""

    cleaned = MARKER_PATTERN.sub(_drop, text)
    return cleaned.strip(), tags


def has_marker(text: str) -> bool:
    """正文里有没有分类标签（没有就整条路径都不必惊动它）。"""
    return bool(text) and MARKER_PATTERN.search(text) is not None


class MarkerFilter:
    """流式预览用的标记清理器（本插件自己的实现）。

    正文是边收边发出去的，没有「发之前再清一遍」的机会，所以标记必须在增量到达时
    就挡掉；否则群里会先看到一行 ``&&happy&&``。规则与 :func:`parse_markers` 一致：
    没收完的片段先压着（它可能只是还没到齐的标记），压着的候选超过上限就说明根本
    不是标记，照原文发出去。
    """

    #: 标记正文（两个 ``&&`` 之间）的最长长度。
    MAX_BODY = 100

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> str:
        """喂一段增量，返回现在就能安全发出去的那部分。"""
        buf = self._buffer + (text or "")
        out: list[str] = []
        while True:
            start = buf.find("&&")
            if start < 0:
                # 结尾单个 ``&`` 可能是标记的开头，先压着
                keep = 1 if buf.endswith("&") else 0
                cut = len(buf) - keep
                out.append(buf[:cut])
                self._buffer = buf[cut:]
                return "".join(out)
            tail = buf[start + 2 :]
            end = tail.find("&&")
            if end < 0:
                if len(tail) <= self.MAX_BODY:
                    # 可能是还没收完的标记：前面确定的部分先发
                    out.append(buf[:start])
                    self._buffer = buf[start:]
                    return "".join(out)
                # 太长了，肯定不是标记
                out.append(buf[: start + 2])
                buf = tail
                continue
            body = tail[:end]
            if (
                len(body) > self.MAX_BODY
                or "&" in body
                or "\n" in body
                or "\r" in body
            ):
                # 不是标记：发掉开头这个 ``&&``，从后面接着找
                out.append(buf[: start + 2])
                buf = tail
                continue
            out.append(buf[:start])
            buf = tail[end + 2 :]

    def finish(self) -> str:
        """收尾：把还压着的半截文本原样吐出来。"""
        rest, self._buffer = self._buffer, ""
        return rest


# ── 找插件实例 ────────────────────────────────────────────────────────
def find_plugin(context: T.Any) -> T.Any | None:
    """取已安装且已启用的 meme_manager 插件实例；拿不到就返回 ``None``。

    只认「注册表里有它、``activated`` 为真、``star_cls`` 还活着、三个底层方法都在」
    的实例 —— 这正是 AstrBot 里「装了并且在插件管理页启用着」的判定口径；没装、
    停用或版本太老都会落回 ``None``，调用方据此整条路径跳过。

    Args:
        context: 本插件的 AstrBot ``Context``（用它查注册表）。

    Returns:
        已实例化的插件对象；不可用时 ``None``。
    """
    getter = getattr(context, "get_registered_star", None)
    if not callable(getter):
        return None
    try:
        meta = getter(MEME_PLUGIN_NAME)
    except Exception as exc:  # noqa: BLE001 - 查注册表失败不该挡住消息
        logger.debug("%s: 查插件 %s 失败：%s", LOG_PREFIX, MEME_PLUGIN_NAME, exc)
        return None
    if meta is None or not bool(getattr(meta, "activated", True)):
        return None
    plugin = getattr(meta, "star_cls", None)
    if plugin is None:
        return None
    for name in _REQUIRED_METHODS:
        if not callable(getattr(plugin, name, None)):
            logger.info(
                "%s: 表情包管理器没有提供 %s 接口，跳过表情包联动", LOG_PREFIX, name
            )
            return None
    return plugin


# ── 一轮回复的交棒器 ──────────────────────────────────────────────────
class MemeBridge:
    """把一轮回复里的分类标签交给 meme_manager 取图、发图。

    只在「表情包管理器装了并启用着」时存在（见 :func:`bridge_for`）；构造之后
    就从不管它还在不在 —— 一轮回复期间插件不会凭空消失。
    """

    def __init__(self, plugin: T.Any, event: T.Any) -> None:
        self.plugin = plugin
        self.event = event
        self.filter = MarkerFilter()

    # -- 正文与标记（本插件自己解析）--
    def has_marker(self, text: str) -> bool:
        """正文里有没有分类标签（没有就不必惊动表情包管理器）。"""
        return has_marker(text)

    def parse(self, text: str) -> tuple[str, list[str]]:
        """解析分类标签：返回 ``(去掉标记的正文, 分类键列表)``。"""
        return parse_markers(text)

    def filter_delta(self, text: str) -> str:
        """流式正文增量：把标记挡在聊天外面。"""
        return self.filter.feed(text)

    def flush(self) -> str:
        """流式收尾：吐出清理器压着的尾巴。"""
        return self.filter.finish()

    # -- 取图（只用它的底层接口）--
    async def build(self, tags: list[str]) -> dict | None:
        """按分类键取图，返回 ``{"images", "temp_files", "tags"}``。

        分类键**直接当目录名**交给 ``_get_category_meme_files``（不看它的分类配置，
        也不走语义检索）；取不到图片的分类跳过。全部失败时返回 ``None``。

        Args:
            tags: :meth:`parse` 给出的分类键（已去重、按出现顺序）。

        Returns:
            待发送的图片组件、需要清理的临时 GIF，以及实际取到图的分类。
        """
        if not tags:
            return None
        get_files = getattr(self.plugin, "_get_category_meme_files", None)
        # 它只在「静态图转 GIF」打开时才真转（``convert_static_to_gif`` 关着就原样返回
        # 路径），所以这里拿到什么用什么：返回的路径变了才当临时文件记一笔。
        convert = getattr(self.plugin, "_convert_to_gif", None)
        if not callable(convert):
            convert = None
        images: list[T.Any] = []
        temp_files: list[str] = []
        used: list[str] = []
        for tag in tags:
            try:
                choices = list(get_files(self.event, tag) or [])
            except Exception as exc:  # noqa: BLE001 - 一个分类取不到不该影响其它
                logger.warning(
                    "%s: 取表情包分类「%s」失败：%s", LOG_PREFIX, tag, exc
                )
                continue
            if not choices:
                logger.info("%s: 表情包分类「%s」里没有图片", LOG_PREFIX, tag)
                continue
            path = str(random.choice(choices))
            try:
                final_path = await asyncio.to_thread(convert, path)
            except Exception as exc:  # noqa: BLE001 - 转换失败就用原图
                logger.warning("%s: 转换表情包「%s」失败：%s", LOG_PREFIX, path, exc)
                final_path = path
            if final_path != path:
                temp_files.append(final_path)
            try:
                images.append(Image.fromFileSystem(final_path))
            except Exception as exc:  # noqa: BLE001 - 图片组件造不出来就跳过
                logger.warning(
                    "%s: 打包表情包图片「%s」失败：%s", LOG_PREFIX, final_path, exc
                )
                continue
            used.append(tag)
        if not images:
            self._cleanup(temp_files)
            return None
        logger.info("%s: 交给表情包管理器取图：%s", LOG_PREFIX, used)
        return {"images": images, "temp_files": temp_files, "tags": used}

    # -- 发图 --
    async def send_images(self, prepared: dict) -> None:
        """自己把图片发出去（正文已经发出去了，或这一轮本来就没有正文）。"""
        images = list(prepared.get("images") or [])
        try:
            for image in images:
                await self._send_one(image)
        finally:
            self._cleanup(prepared.get("temp_files") or [])

    async def _send_one(self, image: T.Any) -> None:
        """发一张图：优先用它的 ``_send_meme_image``（平台差异都在它那边）。"""
        sender = getattr(self.plugin, "_send_meme_image", None)
        try:
            if callable(sender):
                await sender(self.event, image)
            else:  # 兜底：版本太老没有发送方法时，直接交给平台
                await self.event.send(MessageChain([image]))
        except Exception as exc:  # noqa: BLE001 - 发图失败不该影响已经发出去的正文
            logger.warning("%s: 表情包发送失败：%s", LOG_PREFIX, exc, exc_info=True)

    @staticmethod
    def _cleanup(temp_files: T.Iterable[str]) -> None:
        """删掉 :meth:`build` 转出来的临时 GIF。"""
        import os

        for path in temp_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as exc:  # noqa: BLE001 - 临时文件清不掉只记日志
                logger.warning("%s: 清理表情包临时文件失败：%s", LOG_PREFIX, exc)

    def defer_images(self, prepared: dict) -> bool:
        """非流式：把图片挂到事件上，交给它自己的 ``after_message_sent`` 补发。

        这样正文照常走内核的发送链路（链接转义、Markdown 处理都还在），图片在正文
        之后单独发出，临时文件也由它清理。

        Returns:
            是否挂上了。挂不上（对方的 ``after_message_sent`` 不在）时由调用方
            改用 :meth:`send_images` 兜底。
        """
        images = list(prepared.get("images") or [])
        if not images:
            return True
        if not callable(getattr(self.plugin, "_after_message_sent_impl", None)):
            return False
        setter = getattr(self.event, "set_extra", None)
        if not callable(setter):
            return False
        try:
            setter("meme_manager_pending_images", images)
            temp_files = list(prepared.get("temp_files") or [])
            if temp_files:
                setter("meme_manager_temp_files", temp_files)
        except Exception as exc:  # noqa: BLE001 - 事件不支持 extras 时走兜底
            logger.debug("%s: 挂起表情图片失败：%s", LOG_PREFIX, exc)
            return False
        return True


def bridge_for(context: T.Any, event: T.Any) -> MemeBridge | None:
    """取一轮回复的交棒器；表情包管理器不在场 / 没有事件时返回 ``None``。"""
    if event is None:
        return None
    plugin = find_plugin(context)
    if plugin is None:
        return None
    return MemeBridge(plugin, event)
