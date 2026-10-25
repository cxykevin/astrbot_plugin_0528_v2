"""给其它 AstrBot 插件用的「主动唤醒」配置（``plugin_wakeup``）。

其它插件通过 :mod:`api` 调 :func:`wake` 让 alkaid0 在某个会话里跑一轮；**谁能唤、
带什么系统提示词**由本模块解析这份配置：

.. code-block:: json

    {
      "my_plugin": "你是巡检助手，只回答巡检相关的问题",
      "another_plugin": {
        "system_prompt": "你是客服，语气友好。用户问题：{prompt}",
        "enabled": true,
        "reply_to_chat": true
      }
    }

* 值写成字符串 = 只有系统提示词（``enabled`` / ``reply_to_chat`` 用默认值）；
* 值写成对象 = ``system_prompt`` / ``enabled`` / ``reply_to_chat`` 三项；
* 不在配置里的插件一律**没有唤醒权限**（默认拒绝，避免任意插件都能驱动 bot）；
* 系统提示词里写 ``{prompt}`` 时替换成调用方这次传入的正文，否则拼在正文前面。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 配置项名（与 ``_conf_schema.json`` 一致）。
CONFIG_KEY = "plugin_wakeup"

#: 系统提示词里代表「调用方正文」的占位符。
PROMPT_PLACEHOLDER = "{prompt}"


def _as_bool(value: Any, default: bool = True) -> bool:
    """把配置里的真假值转成 bool（字符串 ``false`` / ``no`` / ``0`` 都认）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"", "none", "null"}:
        return default
    return text not in {"false", "0", "no", "off", "disable", "disabled"}


@dataclass(frozen=True)
class PluginProfile:
    """一个插件的唤醒授权。

    Attributes:
        plugin: 插件名（配置里的键，也是 ``api.wake(plugin=...)`` 传的名字）。
        system_prompt: 这个插件唤醒 bot 时固定加在前面的系统提示词。
        enabled: 是否允许唤醒（配了但写 ``enabled: false`` 等于暂时停用）。
        reply_to_chat: 默认是否把这一轮的回复发到群里（调用方可以按次覆盖）。
    """

    plugin: str
    system_prompt: str = ""
    enabled: bool = True
    reply_to_chat: bool = True


def parse_profiles(raw: Any) -> dict[str, PluginProfile]:
    """把配置里的 ``plugin_wakeup`` 洗成 ``{插件名: PluginProfile}``。

    坏条目（值不是字符串 / 对象、键为空）直接跳过 —— 配置写错不该让插件起不来。
    """
    if not isinstance(raw, Mapping):
        return {}
    profiles: dict[str, PluginProfile] = {}
    for key, value in raw.items():
        plugin = str(key or "").strip()
        if not plugin:
            continue
        if isinstance(value, str):
            profiles[plugin] = PluginProfile(
                plugin=plugin, system_prompt=value.strip()
            )
            continue
        if not isinstance(value, Mapping):
            continue
        profiles[plugin] = PluginProfile(
            plugin=plugin,
            system_prompt=str(
                value.get("system_prompt") or value.get("systemPrompt") or ""
            ).strip(),
            enabled=_as_bool(value.get("enabled"), True),
            reply_to_chat=_as_bool(value.get("reply_to_chat"), True),
        )
    return profiles


def build_prompt(profile: PluginProfile, prompt: str = "") -> str:
    """拼这次要发给 alkaid0 的提示词：系统提示词 + 调用方正文。

    系统提示词里有 ``{prompt}`` 就替换（调用方正文为空时替换成空串），否则
    系统提示词在前、正文在后，中间空一行；两者都为空时返回空串（调用方应拒绝）。
    """
    system = (profile.system_prompt or "").strip()
    body = (prompt or "").strip()
    if not system:
        return body
    if PROMPT_PLACEHOLDER in system:
        return system.replace(PROMPT_PLACEHOLDER, body).strip()
    if not body:
        return system
    return f"{system}\n\n{body}"


def describe_profiles(profiles: Mapping[str, PluginProfile]) -> str:
    """一行摘要（状态命令 / 日志用）。"""
    if not profiles:
        return "未配置（其它插件不能唤醒 bot）"
    parts = []
    for name, profile in sorted(profiles.items()):
        state = "启用" if profile.enabled else "停用"
        prompt = profile.system_prompt
        prompt = f"{prompt[:16]}…" if len(prompt) > 16 else prompt
        parts.append(f"{name}（{state}{'，' + prompt if prompt else ''}）")
    return "、".join(parts)


__all__ = [
    "CONFIG_KEY",
    "PROMPT_PLACEHOLDER",
    "PluginProfile",
    "build_prompt",
    "describe_profiles",
    "parse_profiles",
]
