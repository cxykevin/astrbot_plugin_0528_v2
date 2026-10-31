"""插件配置面板：配置树、i18n 兜底文案、脱敏与保存。

WebUI 的「插件可视化视图」把 ``views/settings/`` 装进受限 iframe，页面通过
``window.AstrBotPluginView`` 的 ``apiGet`` / ``apiPost`` 调用这里注册的两个接口：

* ``GET  /<插件名>/panel/config``：返回整棵配置树（分组 → 字段）；
* ``POST /<插件名>/panel/config``：接收 ``{"updates": {字段: 新值}}`` 并保存。

设计上与 alcoh 的 TUI 配置编辑器对齐：

* 配置按「分组 → 字段」组织，前端只负责展开/收起与编辑；
* 字段顺序、类型、默认值和中文兜底文案都从 ``_conf_schema.json`` 读取，
  以后新增配置项不会从面板里漏掉；
* 返回前统一脱敏：URL 查询参数里的 ``key`` / ``token`` / ``secret`` /
  ``password`` 与 userinfo 密码显示成 ``***``；保存时再把 ``***`` 还原成
  旧值，因此「打开面板点保存」不会把 key 冲掉；
* 只接受 schema 里存在的字段与标量类型，未知字段、非法类型、越界值一律拒绝；
* 保存走 ``AstrBotConfig.save_config_async()``，写盘失败时回滚内存里的改动。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

#: 脱敏后的占位符；保存时遇到它表示「保留旧值」。
MASK = "***"

#: URL 查询参数里需要打码的名字（大小写不敏感）。
SECRET_PARAMS = frozenset(
    {
        "key",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "password",
        "passwd",
        "apikey",
        "api_key",
        "auth",
    }
)

#: 改完需要重载插件才生效的字段（前置检查只在 initialize 里跑）。
RELOAD_KEYS = frozenset(
    {
        "enforce_prerequisite",
        "prerequisite_librunner_repo",
        "prerequisite_libi18nject_repo",
    }
)

#: 字符串配置的最大长度（防止把整个文件塞进输入框）。
MAX_STRING_LENGTH = 4096

#: 数值字段的范围；不在这里的数值字段不限制。
_MINIMUMS = {
    "idle_timeout_sec": 0,
    "turn_timeout_sec": 1,
    "wakeup_wait_sec": 1,
}
_MAXIMUMS = {
    "idle_timeout_sec": 31_536_000,
    "turn_timeout_sec": 604_800,
    "wakeup_wait_sec": 600,
}

#: 分组顺序与每组包含的字段。schema 里不在这些组里的字段归入「其它」。
GROUP_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "prerequisites",
        "前置插件",
        "管理本插件依赖的两个前置：librunner（执行器注入）与 libi18nject（WebUI 文案）。",
        (
            "enforce_prerequisite",
            "prerequisite_librunner_repo",
            "prerequisite_libi18nject_repo",
        ),
    ),
    (
        "connection",
        "连接与超时",
        "Alkaid0 的 ACP 地址、群空闲回收与单轮等待时间。",
        ("acp_url", "idle_timeout_sec", "turn_timeout_sec"),
    ),
    (
        "workspace",
        "工作目录",
        "一个群一个工作目录，支持 {groupid} / {sessionid} / {platform} 占位符。",
        ("workspace_template",),
    ),
    (
        "chat",
        "聊天回显",
        "控制工具调用等过程消息、思考过程与 at / CQ 码解析是否发到群里。",
        ("echo_events", "show_thought", "at_parse_enable", "cq_parse_enable"),
    ),
    (
        "auto_approve",
        "工具审批直通",
        "按配置白名单 / 提示词 / 外部钩子自动放行工具审批；没命中的仍挂起等 /aapprove。",
        (
            "auto_approve_enable",
            "auto_approve_tools",
            "auto_approve_prompt",
            "auto_approve_hook",
        ),
    ),
    (
        "wakeup",
        "唤醒钩子（cronjob）",
        "复用 AstrBot 自己的 Web 端口，供 alkaid0 的 cron 唤醒钩子调用；"
        "在 alkaid0 远程配置页点「一键安装唤醒钩子」即可写入。",
        (
            "wakeup_enable",
            "wakeup_api_key",
            "wakeup_public_url",
            "wakeup_wait_sec",
            "wakeup_relay_reply",
            "wakeup_auto_approve",
        ),
    ),
)


def _load_schema() -> dict[str, dict]:
    """读取插件自己的 ``_conf_schema.json``（读不到就返回空表）。"""
    path = Path(__file__).with_name("_conf_schema.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("0528v2: 读取 _conf_schema.json 失败，配置面板没有字段：%s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, dict)}


_SCHEMA = _load_schema()


def _schema_type(entry: Mapping[str, Any]) -> tuple[str | None, bool]:
    """把 schema 的 ``type`` 归一到面板支持的四种类型。

    Returns:
        ``(类型, 是否多行文本)``；不支持的类型返回 ``(None, False)``。
    """
    raw = str(entry.get("type") or "string")
    if raw == "text":
        return "string", True
    if raw in ("string", "int", "float", "bool"):
        return raw, False
    return None, False


def _default_for(kind: str) -> Any:
    """某种类型的空默认值。"""
    return {"bool": False, "int": 0, "float": 0.0, "string": ""}[kind]


def _mask_mode_for(key: str) -> str | None:
    """这个字段该怎么脱敏：``url``（只打码查询参数 / userinfo）或 ``secret``。"""
    lowered = key.lower()
    if key in ("acp_url", "wakeup_public_url") or lowered.endswith("_repo"):
        return "url"
    if any(
        token in lowered
        for token in ("api_key", "apikey", "access_key", "secret", "token", "password")
    ):
        return "secret"
    return None


@dataclass(frozen=True)
class FieldSpec:
    """一个可编辑配置项的静态描述。"""

    key: str
    type: str
    default: Any
    title: str
    hint: str
    group: str
    mask: str | None = None
    min: float | None = None
    max: float | None = None
    multiline: bool = False
    requires_reload: bool = False
    url_kind: str | None = None


def build_field_specs(schema: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[FieldSpec, ...]:
    """从 schema 构造字段列表：分组内的按分组顺序，未分组的排在最后。

    只收录面板能编辑的标量类型；``object`` / ``list`` / ``template_list``
    这类复杂配置暂时跳过（它们的编辑界面不属于本面板的范围）。
    """
    schema = _SCHEMA if schema is None else schema

    def _make(key: str, entry: Mapping[str, Any], group: str) -> FieldSpec | None:
        kind, multiline = _schema_type(entry)
        if kind is None:
            return None
        default = entry.get("default")
        if default is None:
            default = _default_for(kind)
        return FieldSpec(
            key=key,
            type=kind,
            default=default,
            title=str(entry.get("description") or key),
            hint=str(entry.get("hint") or ""),
            group=group,
            mask=_mask_mode_for(key),
            min=_MINIMUMS.get(key),
            max=_MAXIMUMS.get(key),
            multiline=multiline,
            requires_reload=key in RELOAD_KEYS,
            url_kind="websocket" if key == "acp_url" else None,
        )

    specs: list[FieldSpec] = []
    used: set[str] = set()
    for group, _title, _desc, keys in GROUP_SPECS:
        for key in keys:
            entry = schema.get(key)
            if entry is None or key in used:
                continue
            spec = _make(key, entry, group)
            if spec is not None:
                specs.append(spec)
                used.add(key)
    for key, entry in schema.items():
        if key in used:
            continue
        group = "other"
        spec = _make(key, entry, group)
        if spec is not None:
            specs.append(spec)
    return tuple(specs)


FIELD_SPECS = build_field_specs()
FIELD_MAP: dict[str, FieldSpec] = {spec.key: spec for spec in FIELD_SPECS}


# ── URL / 密钥脱敏 ────────────────────────────────────────────────────
def _mask_userinfo(netloc: str) -> str:
    """把 ``user:pass@host`` 的凭证部分打码（无密码时连用户名一起打码）。"""
    userinfo, at, host = netloc.rpartition("@")
    if not at:
        return netloc
    user, colon, _password = userinfo.partition(":")
    if colon:
        return f"{user}:{MASK}@{host}"
    return f"{MASK}@{host}"


def _restore_userinfo(new_netloc: str, old_netloc: str) -> str:
    """把新 URL 里打码的 userinfo 换回旧值。"""
    new_userinfo, at, new_host = new_netloc.rpartition("@")
    if not at:
        return new_netloc
    old_userinfo, old_at, _old_host = old_netloc.rpartition("@")
    if not old_at:
        return new_netloc
    if new_userinfo == MASK:
        return f"{old_userinfo}@{new_host}"
    new_user, new_colon, new_password = new_userinfo.partition(":")
    old_user, _old_colon, old_password = old_userinfo.partition(":")
    if new_colon and new_password == MASK:
        return f"{new_user or old_user}:{old_password}@{new_host}"
    return new_netloc


def _mask_query(query: str) -> str:
    """把查询串里敏感参数的值替换成 ``***``，其余原样保留。"""
    parts: list[str] = []
    for item in query.split("&"):
        if not item:
            continue
        name, sep, _value = item.partition("=")
        if sep and name.strip().lower() in SECRET_PARAMS:
            parts.append(f"{name}={MASK}")
        else:
            parts.append(item)
    return "&".join(parts)


def _restore_query(new_query: str, old_query: str) -> str:
    """把新查询串里值为 ``***`` 的敏感参数换回旧值。"""
    old_values: dict[str, str] = {}
    for item in old_query.split("&"):
        name, sep, value = item.partition("=")
        if sep:
            old_values[name.strip().lower()] = value
    parts: list[str] = []
    for item in new_query.split("&"):
        if not item:
            continue
        name, sep, value = item.partition("=")
        lowered = name.strip().lower()
        if sep and lowered in SECRET_PARAMS and value.strip() == MASK:
            if lowered in old_values:
                parts.append(f"{name}={old_values[lowered]}")
                continue
        parts.append(item)
    return "&".join(parts)


def _has_unresolved_mask(text: str) -> bool:
    """脱敏占位符是否还留在敏感位置（说明没有旧值可还原）。"""
    try:
        parts = urlsplit(text)
    except ValueError:
        return MASK in text
    if MASK in parts.netloc:
        return True
    for item in parts.query.split("&"):
        name, sep, value = item.partition("=")
        if sep and name.strip().lower() in SECRET_PARAMS and value.strip() == MASK:
            return True
    return False


def has_unresolved_mask(text: str) -> bool:
    """公开别名：远程配置面板保存前也要检查 ``***``。"""
    return _has_unresolved_mask(text)


def mask_url(text: str) -> str:
    """把 URL 里的凭证打码：``ws://h/acp?key=abc`` → ``ws://h/acp?key=***``。

    只改 userinfo 与敏感查询参数，主机、路径、其它参数和片段原样保留；
    不是 URL 的字符串（例如 ``owner/repo``）会原样返回。
    """
    raw = (text or "").strip()
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        # 极端畸形 URL：退化成只处理查询串，至少别把明文 key 带出去
        head, sep, query = raw.partition("?")
        return f"{head}{sep}{_mask_query(query)}" if sep else raw
    return urlunsplit(
        (
            parts.scheme,
            _mask_userinfo(parts.netloc),
            parts.path,
            _mask_query(parts.query),
            parts.fragment,
        )
    )


def restore_masked_url(new_text: str, old_text: str) -> str:
    """保存前把 ``***`` 还原成旧 URL 里的真实凭证（无法还原时保留 ``***``）。"""
    raw = (new_text or "").strip()
    old = (old_text or "").strip()
    if not raw or not old:
        return raw
    if raw == mask_url(old):
        return old
    try:
        parts = urlsplit(raw)
        old_parts = urlsplit(old)
    except ValueError:
        return raw
    return urlunsplit(
        (
            parts.scheme,
            _restore_userinfo(parts.netloc, old_parts.netloc),
            parts.path,
            _restore_query(parts.query, old_parts.query),
            parts.fragment,
        )
    )


def mask_value(spec: FieldSpec, value: Any) -> Any:
    """按字段的脱敏模式返回可安全发给前端的值。"""
    if value is None:
        return ""
    if spec.mask == "url":
        return mask_url(str(value))
    if spec.mask == "secret":
        return MASK if str(value) else ""
    return value


# ── 类型校验 ──────────────────────────────────────────────────────────
class CoerceError(ValueError):
    """配置值不合法；``code`` 是给前端做 i18n 的稳定错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y", "t"}:
            return True
        if text in {"0", "false", "no", "off", "n", "f"}:
            return False
    raise CoerceError("invalid_type")


def _coerce_int(spec: FieldSpec, value: Any) -> int:
    if isinstance(value, bool):
        raise CoerceError("invalid_type")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str):
        try:
            result = int(value.strip(), 10)
        except ValueError as exc:
            raise CoerceError("invalid_type") from exc
    else:
        raise CoerceError("invalid_type")
    if spec.min is not None and result < spec.min:
        raise CoerceError("out_of_range")
    if spec.max is not None and result > spec.max:
        raise CoerceError("out_of_range")
    return result


def _coerce_float(spec: FieldSpec, value: Any) -> float:
    if isinstance(value, bool):
        raise CoerceError("invalid_type")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CoerceError("invalid_type") from exc
    if not math.isfinite(result):
        raise CoerceError("invalid_type")
    if spec.min is not None and result < spec.min:
        raise CoerceError("out_of_range")
    if spec.max is not None and result > spec.max:
        raise CoerceError("out_of_range")
    return result


def coerce_value(spec: FieldSpec, value: Any) -> Any:
    """按字段类型把前端传来的值转成配置值。

    Raises:
        CoerceError: 类型不对、超出范围、字符串过长或 URL 不合法。
    """
    if spec.type == "bool":
        return _coerce_bool(value)
    if spec.type == "int":
        return _coerce_int(spec, value)
    if spec.type == "float":
        return _coerce_float(spec, value)

    if not isinstance(value, str):
        raise CoerceError("invalid_type")
    text = value.strip()
    if len(text) > MAX_STRING_LENGTH:
        raise CoerceError("too_long")
    if spec.url_kind == "websocket" and text:
        try:
            parts = urlsplit(text)
        except ValueError as exc:
            raise CoerceError("invalid_url") from exc
        if parts.scheme not in ("ws", "wss") or not parts.netloc:
            raise CoerceError("invalid_url")
    return text


@dataclass
class ApplyResult:
    """一次保存的结果。"""

    ok: bool
    code: str = ""
    data: dict[str, Any] | None = None


# ── 面板服务 ──────────────────────────────────────────────────────────
class PanelService:
    """配置树的读取、校验与持久化。

    Args:
        config: 插件配置对象（运行期是 ``AstrBotConfig``；只要支持写入与
            ``save_config`` 即可）。
        plugin_name: 插件名，用于 Web API 路由前缀。
        version: 插件版本，展示在面板标题旁。
        panel_config_provider: 返回执行器面板实配值（``agent_runner.config``）；
            用来在字段上标注「面板值优先」。
        after_save: 保存成功后的回调，接收 ``{字段: 新值}``，返回「是否需要
            重载插件」；可以是同步或异步函数。
        hook_action: 「写入 / 移除 alkaid0 cron 钩子」按钮的回调，接收
            ``"install"`` / ``"remove"``，返回要交给前端的 ``dict``；缺席时不注册
            该接口（面板上也不显示按钮）。
        hook_status_provider: 返回钩子现状（``installed`` / ``note`` / ``endpoint``
            等），随配置树一起下发。
        hook_status_async_provider: 同上的异步版本（会回读 alkaid0），供
            ``GET panel/hook`` 使用；缺席时回落到同步版本。
    """

    def __init__(
        self,
        config: Any,
        *,
        plugin_name: str,
        version: str,
        panel_config_provider: Callable[[], Any] | None = None,
        after_save: Callable[[dict[str, Any]], Any] | None = None,
        hook_action: Callable[[str], Any] | None = None,
        hook_status_provider: Callable[[], Any] | None = None,
        hook_status_async_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.plugin_name = plugin_name
        self.version = version
        self.panel_config_provider = panel_config_provider
        self.after_save = after_save
        self.hook_action = hook_action
        self.hook_status_provider = hook_status_provider
        self.hook_status_async_provider = hook_status_async_provider
        self._lock = asyncio.Lock()

    # ── 读取 ─────────────────────────────────────────────
    def _current_value(self, spec: FieldSpec) -> Any:
        """配置里的当前值；缺失或为 ``None`` 时用 schema 默认值。"""
        if not isinstance(self.config, Mapping):
            return spec.default
        value = self.config.get(spec.key)
        return spec.default if value is None else value

    def _panel_value(self, key: str) -> Any:
        """执行器面板里的覆盖值；空串 / 缺失视为未覆盖。"""
        provider = self.panel_config_provider
        if provider is None:
            return None
        try:
            panel = provider()
        except Exception:  # noqa: BLE001 - 读不到面板配置就当没覆盖
            return None
        if not isinstance(panel, Mapping) or key not in panel:
            return None
        value = panel.get(key)
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def _effective_value(self, spec: FieldSpec, current: Any) -> Any:
        """真正生效的值：面板值优先，其次插件配置，空串回落到默认值。"""
        panel_value = self._panel_value(spec.key)
        if panel_value is not None:
            return panel_value
        if current is None:
            return spec.default
        if isinstance(current, str) and not current.strip():
            return spec.default
        return current

    def _snapshot_field(self, spec: FieldSpec) -> dict[str, Any]:
        current = self._current_value(spec)
        panel_value = self._panel_value(spec.key)
        return {
            "key": spec.key,
            "type": spec.type,
            "title": spec.title,
            "hint": spec.hint,
            "value": mask_value(spec, current),
            "default": mask_value(spec, spec.default),
            "effective": mask_value(spec, self._effective_value(spec, current)),
            "panel_value": (
                mask_value(spec, panel_value) if panel_value is not None else None
            ),
            "overridden": panel_value is not None,
            "requires_reload": spec.requires_reload,
            "mask": spec.mask,
            "min": spec.min,
            "max": spec.max,
            "multiline": spec.multiline,
        }

    def snapshot(self) -> dict[str, Any]:
        """整棵配置树（分组 → 字段），所有值已脱敏。"""
        groups: list[dict[str, Any]] = []
        grouped: set[str] = set()
        for group, title, description, keys in GROUP_SPECS:
            fields = []
            for key in keys:
                spec = FIELD_MAP.get(key)
                if spec is None:
                    continue
                grouped.add(key)
                fields.append(self._snapshot_field(spec))
            if fields:
                groups.append(
                    {
                        "key": group,
                        "title": title,
                        "description": description,
                        "fields": fields,
                    }
                )
        extras = [spec for spec in FIELD_SPECS if spec.key not in grouped]
        if extras:
            groups.append(
                {
                    "key": "other",
                    "title": "其它",
                    "description": "schema 里尚未归类的配置项。",
                    "fields": [self._snapshot_field(spec) for spec in extras],
                }
            )
        return {
            "plugin": self.plugin_name,
            "version": self.version,
            "groups": groups,
            "reload_keys": sorted(RELOAD_KEYS & {spec.key for spec in FIELD_SPECS}),
            "hook": self._hook_info(),
        }

    def _hook_info(self) -> dict[str, Any]:
        """钩子现状（供面板显示按钮旁的状态）；没有回调时返回空表。"""
        provider = self.hook_status_provider
        if provider is None:
            return {}
        try:
            data = provider()
        except Exception as exc:  # noqa: BLE001 - 状态读不到不该让配置树打不开
            logger.warning("0528v2: 读取 cron 钩子状态失败：%s", exc)
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    # ── 保存 ─────────────────────────────────────────────
    async def _persist(self) -> None:
        """把当前配置写盘；配置对象不支持保存时抛 ``RuntimeError``。"""
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            saved = await save_async()
            if saved is False:
                raise RuntimeError("save_config_async returned False")
            return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            await asyncio.to_thread(save)
            return
        raise RuntimeError("配置对象没有 save_config 方法")

    async def _notify_after_save(self, changed: Mapping[str, Any]) -> bool:
        """调用运行时刷新回调；回调缺席或出错时按 RELOAD_KEYS 判断。"""
        fallback = any(key in RELOAD_KEYS for key in changed)
        if self.after_save is None:
            return fallback
        try:
            result = self.after_save(dict(changed))
            if inspect.isawaitable(result):
                result = await result
            return fallback if result is None else bool(result)
        except Exception as exc:  # noqa: BLE001 - 刷新失败不该让保存回滚
            logger.warning("0528v2: 面板保存后的运行时刷新失败：%s", exc)
            return fallback

    async def apply(self, updates: Mapping[str, Any]) -> ApplyResult:
        """校验并保存一批配置改动（只写真正变化的字段）。"""
        if not isinstance(updates, Mapping) or not updates:
            return ApplyResult(False, "no_changes")

        changed: dict[str, Any] = {}
        for raw_key, raw_value in updates.items():
            key = str(raw_key)
            spec = FIELD_MAP.get(key)
            if spec is None:
                return ApplyResult(False, f"unknown_field:{key}")

            old_value = self._current_value(spec)
            value = raw_value
            if spec.mask == "url":
                value = restore_masked_url(str(raw_value), str(old_value or ""))
                if _has_unresolved_mask(str(value)):
                    return ApplyResult(False, f"unresolved_mask:{key}")
            elif spec.mask == "secret" and str(raw_value) == MASK:
                value = old_value

            try:
                coerced = coerce_value(spec, value)
            except CoerceError as exc:
                return ApplyResult(False, f"{exc.code}:{key}")
            if coerced != old_value:
                changed[key] = coerced

        if not changed:
            return ApplyResult(
                True,
                data={
                    "saved": [],
                    "reload_required": False,
                    "config": self.snapshot(),
                },
            )

        old_values = {
            key: self._current_value(FIELD_MAP[key]) for key in changed
        }
        if not isinstance(self.config, MutableMapping):
            return ApplyResult(False, "not_writable")
        for key, value in changed.items():
            self.config[key] = value  # type: ignore[index]

        try:
            await self._persist()
        except Exception as exc:  # noqa: BLE001 - 写盘失败要把内存改回去
            for key, value in old_values.items():
                self.config[key] = value  # type: ignore[index]
            logger.warning("0528v2: 面板保存配置失败：%s", exc)
            return ApplyResult(False, "save_failed")

        reload_required = await self._notify_after_save(changed)
        logger.info(
            "0528v2: 配置面板保存了 %d 项：%s",
            len(changed),
            "、".join(changed),
        )
        return ApplyResult(
            True,
            data={
                "saved": list(changed),
                "reload_required": reload_required,
                "config": self.snapshot(),
            },
        )

    # ── 手动动作（写入 / 移除 alkaid0 cron 钩子）───────────
    async def apply_hook_action(self, action: str) -> ApplyResult:
        """执行一次钩子动作（``install`` / ``remove``）。

        「写进 alkaid0 配置」是外部副作用，只由面板上的按钮触发 —— 保存插件配置、
        插件启用都不再自动改 alkaid0。
        """
        if self.hook_action is None:
            return ApplyResult(False, "hook_unavailable")
        if action not in ("install", "remove"):
            return ApplyResult(False, "bad_payload")
        try:
            result = self.hook_action(action)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - alkaid0 没起来 / 写不进去
            logger.warning("0528v2: 手动同步 alkaid0 cron 钩子失败：%s", exc)
            return ApplyResult(False, "hook_failed")
        data = dict(result) if isinstance(result, Mapping) else {}
        return ApplyResult(True, data=data)

    async def run_hook_action(self):
        """``POST panel/hook``：面板按钮调用的接口。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("bad_payload")
        async with self._lock:
            result = await self.apply_hook_action(str(payload.get("action") or ""))
        if not result.ok:
            return error_response(result.code)
        return json_response({"status": "ok", "data": result.data})

    async def run_hook_status(self):
        """``GET panel/hook``：回读 alkaid0，返回真实的钩子状态。"""
        provider = self.hook_status_async_provider
        if provider is None:
            data = self._hook_info()
        else:
            try:
                result = provider()
                if inspect.isawaitable(result):
                    result = await result
                data = dict(result) if isinstance(result, Mapping) else {}
            except Exception as exc:  # noqa: BLE001 - 状态读不到不该让页面打不开
                logger.warning("0528v2: 回读 cron 钩子状态失败：%s", exc)
                data = self._hook_info()
        return json_response({"status": "ok", "data": data})

    # ── Web API ──────────────────────────────────────────
    async def get_config(self):
        """``GET panel/config``：返回配置树。"""
        async with self._lock:
            data = self.snapshot()
        return json_response({"status": "ok", "data": data})

    async def save_config(self):
        """``POST panel/config``：保存 ``{"updates": {字段: 新值}}``。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("bad_payload")
        updates = payload.get("updates")
        if not isinstance(updates, dict):
            return error_response("bad_payload")
        async with self._lock:
            result = await self.apply(updates)
        if not result.ok:
            return error_response(result.code)
        return json_response({"status": "ok", "data": result.data})

    def register(self, context: Any) -> None:
        """把两个接口注册进 AstrBot 的插件 Web API 表（重复注册会覆盖）。"""
        if context is None:
            return
        base = f"/{self.plugin_name}/panel/config"
        context.register_web_api(base, self.get_config, ["GET"], "插件配置树")
        context.register_web_api(base, self.save_config, ["POST"], "保存插件配置")
        if self.hook_action is not None:
            hook_path = f"/{self.plugin_name}/panel/hook"
            context.register_web_api(
                hook_path,
                self.run_hook_status,
                ["GET"],
                "读取 alkaid0 cron 钩子状态",
            )
            context.register_web_api(
                hook_path,
                self.run_hook_action,
                ["POST"],
                "写入 / 移除 alkaid0 cron 钩子",
            )
        logger.info("0528v2: 配置面板接口已注册 %s（GET/POST）", base)

    def unregister(self, context: Any) -> None:
        """插件停用时摘掉自己注册的路由，避免留下指向旧实例的 handler。"""
        routes = getattr(context, "registered_web_apis", None)
        if not isinstance(routes, list):
            return
        prefix = f"/{self.plugin_name}/panel/"
        routes[:] = [
            item
            for item in routes
            if not (
                isinstance(item, tuple)
                and item
                and str(item[0]).startswith(prefix)
            )
        ]


__all__ = [
    "FIELD_MAP",
    "FIELD_SPECS",
    "GROUP_SPECS",
    "MAX_STRING_LENGTH",
    "MASK",
    "PanelService",
    "RELOAD_KEYS",
    "SECRET_PARAMS",
    "ApplyResult",
    "CoerceError",
    "build_field_specs",
    "coerce_value",
    "mask_url",
    "mask_value",
    "restore_masked_url",
]
