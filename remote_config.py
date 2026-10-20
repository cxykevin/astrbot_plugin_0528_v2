"""alkaid0 远程配置面板后端。

通过 ACP 扩展方法读写 alkaid0 的**全局配置**（不是本插件的 AstrBot 配置）：

* ``alk.cxykevin.top/config/get``：读取完整配置；
* ``alk.cxykevin.top/config/set``：部分更新并持久化；
* ``alk.cxykevin.top/config/reload``：从磁盘重载配置。

``config/get`` 会返回 ``ProviderKey`` / ``Server.key`` / 搜索 API key 等明文，
所以发给浏览器之前统一脱敏成 ``***``。前端只提交改动过的叶子节点，未改动的
敏感字段根本不出现在 patch 里；保存前本模块再按路径把这些 ``***`` 还原（URL）
或直接丢弃（secret），避免「改一个同级的字段把 key 冲掉」。

配置文件 schema 取自 alkaid0 的 ``docs/schemas/config.json``，打包在同目录的
``alkaid0_config_schema.json`` 里。alkaid0 新增了 schema 以外的字段时，前端仍
可通过 JSON 模式看到/编辑它们（``config/set`` 会按结构体字段应用）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .acp import pool as pool_mod
from .acp.client import ACPConnectionError, ACPError, ACPTimeoutError
from .panel import MASK, has_unresolved_mask, mask_url, restore_masked_url

#: 远程配置方法（见 alkaid0 ``docs/acp/extension.md`` §3.5 / §3.6）。
METHOD_CONFIG_GET = "alk.cxykevin.top/config/get"
METHOD_CONFIG_SET = "alk.cxykevin.top/config/set"
METHOD_CONFIG_RELOAD = "alk.cxykevin.top/config/reload"

#: 打包进插件的 alkaid0 配置 schema。
SCHEMA_FILE_NAME = "alkaid0_config_schema.json"

#: patch 的上限（防止误把整个文件塞进来）。
MAX_PATCH_BYTES = 1_000_000

#: schema 已知字段的键名里出现这些词就按敏感字段处理（大小写不敏感）；
#: 未在 schema 中声明的字段默认不敏感。
_SECRET_HINTS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
)

#: 字段名以这些后缀结尾时按 URL 处理（只打码 userinfo / 查询参数）。
_URL_SUFFIXES = ("url", "uri", "proxy")

#: 改完需要重启 alkaid0 才生效的字段（仅用于前端提示）。
RESTART_PATHS: tuple[str, ...] = (
    "Server.host",
    "Server.port",
    "Server.key",
    "Server.path",
    "Server.disableStdioServer",
    "ignoreSignals",
)


class RemoteConfigError(RuntimeError):
    """远程配置操作失败；``code`` 是给前端做 i18n 的稳定错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_schema(path: Path | None = None) -> dict[str, Any]:
    """读取 alkaid0 配置 JSON Schema；失败时返回一个空对象 schema。"""
    target = path or Path(__file__).with_name(SCHEMA_FILE_NAME)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("0528v2: 读取 alkaid0 配置 schema 失败：%s", exc)
        return {"type": "object", "properties": {}}
    if not isinstance(raw, dict):
        return {"type": "object", "properties": {}}
    if not isinstance(raw.get("properties"), dict):
        raw["properties"] = {}
    return raw


def _path_text(path: list[Any]) -> str:
    """把路径渲染成 ``Model.Models.1.ProviderKey`` / ``Args[0]`` 形式。"""
    parts: list[str] = []
    for item in path:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts) or "<root>"


def schema_path_known(schema: Mapping[str, Any], path: list[Any]) -> bool:
    """这条路径是否由 schema 声明（含 map / array 的声明值）。

    ``ak.0`` 升级后可能新增 schema 快照里没有的字段；这些字段默认按普通字段
    处理（不自动打码）。动态 map 的键（如 ``TerminalEnvs.OPENAI_API_KEY``）
    只要值类型由 ``additionalProperties`` / ``items`` 声明过，仍算已知。
    """
    node: Any = schema
    for item in path:
        if not isinstance(node, Mapping):
            return False
        if isinstance(item, int):
            items = node.get("items")
            if isinstance(items, Mapping):
                node = items
                continue
            if (
                isinstance(items, list)
                and 0 <= item < len(items)
                and isinstance(items[item], Mapping)
            ):
                node = items[item]
                continue
            return False

        key = str(item)
        properties = node.get("properties")
        if isinstance(properties, Mapping) and key in properties:
            node = properties[key]
            continue

        patterns = node.get("patternProperties")
        if isinstance(patterns, Mapping):
            matched = False
            for pattern, child in patterns.items():
                try:
                    if re.search(str(pattern), key):
                        node = child
                        matched = True
                        break
                except re.error:
                    continue
            if matched:
                continue

        additional = node.get("additionalProperties")
        if isinstance(additional, Mapping) and additional:
            node = additional
            continue
        return False
    return True


def is_secret_path(
    path: list[Any], schema: Mapping[str, Any] | None = None
) -> bool:
    """这个叶子是不是敏感字段（看路径最后一段的名字）。

    覆盖 ``Server.key``、``ProviderKey``、``TerminalEnvs.OPENAI_API_KEY``、
    ``RewriteHeaders.<regex>.Authorization`` 等；数组下标本身不算字段名。
    给了 ``schema`` 时，**未在 schema 中声明的字段默认不敏感**。
    """
    if not path:
        return False
    if schema is not None and not schema_path_known(schema, path):
        return False
    last = path[-1]
    if isinstance(last, int):
        return False
    lowered = str(last).lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def is_url_path(
    path: list[Any], schema: Mapping[str, Any] | None = None
) -> bool:
    """这个叶子是不是 URL / 代理地址（只做 userinfo 与查询参数打码）。

    给了 ``schema`` 时，**未在 schema 中声明的字段默认不敏感**。
    """
    if not path:
        return False
    if schema is not None and not schema_path_known(schema, path):
        return False
    last = path[-1]
    if isinstance(last, int):
        return False
    name = str(last)
    lowered = name.lower()
    if lowered.endswith(_URL_SUFFIXES) or lowered == "fetchproxy":
        return True
    # Python.Source 是 pip 源地址，可能有 userinfo / token。
    return len(path) >= 2 and str(path[-2]) == "Python" and name == "Source"


def mask_config(
    value: Any,
    path: list[Any] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> Any:
    """递归复制配置并把敏感值替换成 ``***``（不修改入参）。

    传 ``schema`` 时，只有 schema 声明过的路径才按敏感 / URL 处理；
    schema 里没有的字段默认原样保留。
    """
    path = [] if path is None else path
    if isinstance(value, dict):
        return {
            str(key): mask_config(child, [*path, str(key)], schema)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            mask_config(child, [*path, index], schema)
            for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        if is_secret_path(path, schema):
            return MASK if value else ""
        if is_url_path(path, schema):
            return mask_url(value)
    return value


_DROP = object()


def restore_patch(
    patch: Any,
    current: Any,
    path: list[Any] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> Any:
    """保存前把 patch 里的 ``***`` 还原成旧值 / 丢弃秘密占位符。

    Args:
        patch: 前端提交的嵌套 patch。
        current: ``config/get`` 拿到的当前完整配置（明文，仅在本进程内使用）。
        path: 递归路径。
        schema: 配置 schema；给了就只对 schema 声明过的路径做敏感还原。

    Returns:
        处理后的 patch；某个分支全是已丢弃的秘密时返回 :data:`_DROP`。
    """
    path = [] if path is None else path
    if isinstance(patch, dict):
        restored: dict[str, Any] = {}
        for key, child in patch.items():
            name = str(key)
            child_current = current.get(name) if isinstance(current, Mapping) else None
            value = restore_patch(child, child_current, [*path, name], schema)
            if value is not _DROP:
                restored[name] = value
        # 整棵子树都只是未修改的 ***：当作没有改动，避免空对象 patch。
        if patch and not restored:
            return _DROP
        return restored
    if isinstance(patch, list):
        restored_list: list[Any] = []
        for index, child in enumerate(patch):
            child_current = (
                current[index]
                if isinstance(current, list) and index < len(current)
                else None
            )
            value = restore_patch(child, child_current, [*path, index], schema)
            if value is _DROP:
                if child_current is None:
                    raise RemoteConfigError(f"unresolved_mask:{_path_text(path)}")
                restored_list.append(child_current)
            else:
                restored_list.append(value)
        return restored_list
    if isinstance(patch, str):
        if is_secret_path(path, schema) and patch == MASK:
            return _DROP
        if is_url_path(path, schema) and MASK in patch:
            restored = restore_masked_url(patch, str(current or ""))
            if has_unresolved_mask(restored):
                raise RemoteConfigError(f"unresolved_mask:{_path_text(path)}")
            return restored
    return patch


def flatten_patch(patch: Any, path: list[Any] | None = None) -> list[str]:
    """把嵌套 patch 摊平成叶子路径列表（前端显示保存了几项）。"""
    path = [] if path is None else path
    if isinstance(patch, dict):
        paths: list[str] = []
        for key, child in patch.items():
            paths.extend(flatten_patch(child, [*path, str(key)]))
        return paths or [_path_text(path)]
    return [_path_text(path)]


def validate_patch(
    patch: Any,
    schema: Mapping[str, Any],
    current: Mapping[str, Any] | None = None,
) -> None:
    """检查 patch 大小与顶层字段。

    顶层字段允许两类：schema 里有的，以及**当前配置里已经存在**的。
    后者是为了兼容 alkaid0 升级后新增、但打包 schema 还没跟上的字段；
    凭空写一个不存在的顶层键仍然拒绝（多半是拼错了）。
    """
    if not isinstance(patch, dict) or not patch:
        raise RemoteConfigError("no_changes")
    try:
        raw = json.dumps(patch, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RemoteConfigError("bad_payload") from exc
    if len(raw) > MAX_PATCH_BYTES:
        raise RemoteConfigError("too_large")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    existing = current if isinstance(current, Mapping) else {}
    for key in patch:
        if key not in properties and key not in existing:
            raise RemoteConfigError(f"unknown_field:{key}")


def _short_error(exc: BaseException, limit: int = 400) -> str:
    text = " ".join(str(exc).split())
    return text[:limit]


class RemoteConfigService:
    """alkaid0 全局配置的读取、脱敏、部分更新与重载。

    Args:
        plugin_name: 插件名，用于 Web API 路由前缀。
        url_provider: 返回当前生效的 ACP 地址（含 ``?key=``）；每次请求现取。
        schema: 配置 JSON Schema；不传就读取同目录的打包文件。
        timeout: 单次远程请求超时秒数。
        client_factory: 造 ACP 客户端的函数；自测时注入假客户端。
    """

    def __init__(
        self,
        *,
        plugin_name: str,
        url_provider: Callable[[], str],
        schema: Mapping[str, Any] | None = None,
        timeout: float = 20.0,
        client_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.plugin_name = plugin_name
        self.url_provider = url_provider
        self.schema = dict(schema) if isinstance(schema, Mapping) else load_schema()
        self.timeout = float(timeout)
        self.client_factory = client_factory or pool_mod.default_client_factory
        self._lock = asyncio.Lock()

    # ── 连接 ─────────────────────────────────────────────
    def remote_url(self) -> str:
        try:
            return str(self.url_provider() or "").strip()
        except Exception:  # noqa: BLE001 - 读配置失败不应炸面板
            return ""

    async def _with_client(self, handler: Callable[[Any], Any]) -> Any:
        url = self.remote_url()
        if not url:
            raise RemoteConfigError("remote_unconfigured")
        client = self.client_factory(url, f"{self.plugin_name}:remote-config")
        try:
            await client.connect()
            await client.initialize(timeout=self.timeout)
            return await handler(client)
        except RemoteConfigError:
            raise
        except ACPTimeoutError as exc:
            raise RemoteConfigError("remote_timeout") from exc
        except ACPConnectionError as exc:
            raise RemoteConfigError(
                f"remote_unreachable:{_short_error(exc)}"
            ) from exc
        except ACPError as exc:
            text = str(exc)
            if "-32601" in text or "method not found" in text.lower():
                raise RemoteConfigError("remote_unsupported") from exc
            raise RemoteConfigError(f"remote_error:{_short_error(exc)}") from exc
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    # ── 读 / 写 ──────────────────────────────────────────
    async def _get_on(self, client: Any) -> dict[str, Any]:
        result = await client.call(METHOD_CONFIG_GET, {}, timeout=self.timeout)
        config = result.get("config") if isinstance(result, Mapping) else None
        if not isinstance(config, dict):
            raise RemoteConfigError("bad_config")
        return config

    def snapshot(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """把完整配置包装成前端要的 ``config + schema + meta``（已脱敏）。"""
        return {
            "config": mask_config(dict(config), schema=self.schema),
            "schema": self.schema,
            "meta": {
                "plugin": self.plugin_name,
                "server": mask_url(self.remote_url()),
                "mask": MASK,
                "restart_paths": list(RESTART_PATHS),
                "schema_title": self.schema.get("title") or "alkaid0 Config",
            },
        }

    async def fetch(self) -> dict[str, Any]:
        async with self._lock:
            config = await self._with_client(self._get_on)
            return self.snapshot(config)

    async def apply(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await self._with_client(
                lambda client: self._apply_on(client, patch)
            )

    async def _apply_on(self, client: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
        current = await self._get_on(client)
        validate_patch(patch, self.schema, current)
        restored = restore_patch(dict(patch), current, schema=self.schema)
        if restored is _DROP or not isinstance(restored, dict) or not restored:
            return {
                **self.snapshot(current),
                "saved": [],
                "warning": "",
            }

        await client.call(
            METHOD_CONFIG_SET, {"config": restored}, timeout=self.timeout
        )
        warning = ""
        try:
            fresh = await self._get_on(client)
        except (RemoteConfigError, ACPError):
            # 写入已经成功，只是回读失败（超时 / 掉线 / 返回结构不对）；
            # 让用户点「重新读取」拿最新值，不要把一次成功的保存报成失败。
            fresh = current
            warning = "refresh_failed"
        paths = flatten_patch(restored)
        logger.info(
            "0528v2: 已保存 alkaid0 远程配置 %d 项（顶层：%s）",
            len(paths),
            "、".join(sorted(restored)),
        )
        return {
            **self.snapshot(fresh),
            "saved": paths,
            "warning": warning,
        }

    async def reload(self) -> dict[str, Any]:
        async with self._lock:
            return await self._with_client(self._reload_on)

    async def _reload_on(self, client: Any) -> dict[str, Any]:
        await client.call(METHOD_CONFIG_RELOAD, {}, timeout=self.timeout)
        config = await self._get_on(client)
        return self.snapshot(config)

    # ── Web API ──────────────────────────────────────────
    async def get_config(self):
        """``GET remote/config``：拉取并脱敏 alkaid0 全局配置。"""
        try:
            data = await self.fetch()
        except RemoteConfigError as exc:
            return error_response(exc.code)
        return json_response({"status": "ok", "data": data})

    async def save_config(self):
        """``POST remote/config``：保存 ``{"patch": {嵌套改动}}``。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("bad_payload")
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            return error_response("bad_payload")
        try:
            data = await self.apply(patch)
        except RemoteConfigError as exc:
            return error_response(exc.code)
        return json_response({"status": "ok", "data": data})

    async def reload_config(self):
        """``POST remote/reload``：让 alkaid0 从磁盘重载配置。"""
        try:
            data = await self.reload()
        except RemoteConfigError as exc:
            return error_response(exc.code)
        return json_response({"status": "ok", "data": data})

    def register(self, context: Any) -> None:
        """注册远程配置的读 / 写 / 重载接口。"""
        if context is None:
            return
        base = f"/{self.plugin_name}/remote/config"
        reload_path = f"/{self.plugin_name}/remote/reload"
        context.register_web_api(base, self.get_config, ["GET"], "alkaid0 远程配置")
        context.register_web_api(
            base, self.save_config, ["POST"], "保存 alkaid0 远程配置"
        )
        context.register_web_api(
            reload_path, self.reload_config, ["POST"], "重载 alkaid0 配置"
        )
        logger.info(
            "0528v2: alkaid0 远程配置接口已注册 %s（GET/POST）、%s（POST）",
            base,
            reload_path,
        )

    def unregister(self, context: Any) -> None:
        """插件停用时摘掉远程配置接口。"""
        routes = getattr(context, "registered_web_apis", None)
        if not isinstance(routes, list):
            return
        prefix = f"/{self.plugin_name}/remote/"
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
    "MAX_PATCH_BYTES",
    "METHOD_CONFIG_GET",
    "METHOD_CONFIG_RELOAD",
    "METHOD_CONFIG_SET",
    "RESTART_PATHS",
    "RemoteConfigError",
    "RemoteConfigService",
    "SCHEMA_FILE_NAME",
    "flatten_patch",
    "is_secret_path",
    "is_url_path",
    "load_schema",
    "mask_config",
    "restore_patch",
    "schema_path_known",
    "validate_patch",
]
