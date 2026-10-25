"""HTTP 唤醒钩子：把 alkaid0 的 cron 唤醒事件接到 AstrBot。

alkaid0 的 ``Hooks.OnCronjobWakeup`` 会在 cron 定时任务唤醒某个会话后**同步**执行
外部命令（阻塞到命令退出）。本模块**复用 AstrBot 自己的 Web 端口**（默认 6185），
把唤醒接口注册成 AstrBot 插件 Web API：

``/api/v1/plugins/extensions/<插件名>/wakeup``

鉴权走 AstrBot 的 API Key（``X-API-Key``，scope 需要 ``plugin``）；这个 Key 由
「一键安装」在 AstrBot 里自动申请，明文**直接写进钩子命令**（不再走
``Agent.TerminalEnvs``）。插件配置里的 ``wakeup_api_key`` 仍然可用，作为可选的
第二道口令（请求头 ``X-0528V2-Key``），同样明文写进命令。

钩子命令会把 ``SESSION_ID``（以及 ``SESSION_PATH``）交过来：

1. 在会话记录（:class:`~acp.store.SessionStore`）里找出**所有绑定该会话**的群；
2. 为每个群建连并 ``session/resume`` 那个会话（**绝不** ``session/new``）；
3. 等这些连接真的收到这一轮对话的内容（``user_message`` / ``agent_message`` 等），
   再返回 HTTP 响应 —— alkaid0 那边的钩子是阻塞调用，这样它返回时客户端已经就位；
4. 可选（``wakeup_relay_reply``）：把 cron 触发的这一轮正文与过程消息转发到对应
   群聊。否则回复只会落在一条没人消费的连接上。

本模块同时负责「一键写入 alkaid0 的 cron 钩子」：生成一条 ``curl`` 命令（AstrBot
API Key 放进 alkaid0 的 ``Agent.TerminalEnvs``，不直接出现在命令行里），通过远程
配置接口写进 ``Hooks.OnCronjobWakeup``，并保留用户自己的其它钩子。
"""

from __future__ import annotations

import asyncio
import hmac
import posixpath
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.web import json_response, request

from . import agent_runner as runner_mod
from .acp import approval as approval_mod
from .acp import protocol as acp
from .acp.pool import DELIVERY_UPDATE_KINDS, GroupSession, SessionPool
from .acp.store import SessionStore
from .proactive import StreamBuffer

#: 日志前缀（与插件其它模块一致）。
LOG_PREFIX = "0528v2"

#: 唤醒后最多等多久「消息真的送到」（秒）。
DEFAULT_WAIT_SEC = 30

#: 插件在 AstrBot 上的唤醒路由尾段（完整路径由 main.py 注册）。
HOOK_PATH = "/wakeup"

#: AstrBot 插件 Web API 前缀（v1 路由不经过 dashboard 登录中间件，
#: 鉴权由路由依赖 ``require_plugin_scope`` 完成，认 ``X-API-Key``）。
HOOK_API_PREFIX = "/api/v1/plugins/extensions"

#: 旧版本写进 alkaid0 ``Agent.TerminalEnvs`` 的变量名。现在密钥直接写进钩子命令，
#: 这两个名字只在安装 / 移除时用来清掉历史残留。
HOOK_ENV_KEY = "ALKAID0_0528V2_HOOK_KEY"

#: 同上：插件自己的可选口令曾经用过的环境变量名。
HOOK_PLUGIN_ENV_KEY = "ALKAID0_0528V2_PLUGIN_KEY"

#: AstrBot 插件 Web API 的鉴权头（AstrBot API Key，明文写进钩子命令）。
API_KEY_HEADER = "X-API-Key"

#: 第二道口令的请求头 / 查询参数名。
PLUGIN_KEY_HEADER = "X-0528V2-Key"
PLUGIN_KEY_QUERY = "plugin_key"

#: 自动申请的 AstrBot API Key 名字（移除钩子时按它认出并撤销）。
ASTRBOT_KEY_NAME = "astrbot_plugin_0528_v2: cron wakeup"

#: 请求体上限（这个接口只需要一个会话 ID）。
MAX_BODY_BYTES = 64 * 1024

#: 与本模块相关的插件配置项（面板保存后要重新同步）。
CONFIG_KEYS = frozenset(
    {
        "wakeup_enable",
        "wakeup_api_key",
        "wakeup_public_url",
        "wakeup_wait_sec",
        "wakeup_relay_reply",
        "wakeup_auto_approve",
    }
)

_TRUE_WORDS = {"1", "true", "yes", "on", "y", "t"}


def _as_bool(value: Any, default: bool = False) -> bool:
    """把配置值转成布尔（认字符串形式的 true/false）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        return default if not text else text in _TRUE_WORDS
    return default


def _as_int(value: Any, default: int) -> int:
    """把配置值转成整数；转不了用默认值（布尔不算数字）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return default
    return default


def clean_path(text: str) -> str:
    """把工作目录归一成用于比较的形式（统一斜杠、去掉尾随分隔符与 ``.`` 段）。"""
    raw = (text or "").strip().replace("\\", "/")
    if not raw:
        return ""
    return posixpath.normpath(raw)


def parse_session_token(text: str) -> tuple[str, str]:
    """解析调用方给的会话标识。

    Returns:
        ``("full", "sess_12:/path")`` —— 完整 sessionId；
        ``("id", "12")`` —— 只有 alkaid0 的会话号（``SESSION_ID``）；
        ``("", "")`` —— 无法识别。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("sess_"):
        rest = raw[len("sess_") :]
        head, sep, cwd = rest.partition(":")
        if sep:
            if head.isdigit() and cwd.strip():
                return "full", f"sess_{head}:{cwd.strip()}"
            return "", ""
        return ("id", head) if head.isdigit() else ("", "")
    if raw.isdigit():
        return "id", raw
    return "", ""


def match_bindings(
    entries: list[dict] | tuple[dict, ...],
    token: str,
    cwd: str = "",
) -> list[dict]:
    """在会话记录里找出绑定这个会话的群。

    Args:
        entries: :meth:`acp.store.SessionStore.entries` 的结果。
        token: 完整 sessionId（精确匹配）或 alkaid0 的会话号（可能对应多个 cwd）。
        cwd: 可选的 ``SESSION_PATH``；给了就只保留同一工作目录的绑定
            （会话号在 alkaid0 里是按工作目录分库的，不传 cwd 时可能匹配到多个）。

    Returns:
        ``[{key, sessionId, cwd, updatedAt}]``，最近活动的在前。
    """
    kind, value = parse_session_token(token)
    if not kind:
        return []
    want = clean_path(cwd)
    matched: list[dict] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or "").strip()
        session_id = str(entry.get("sessionId") or "").strip()
        if not key or not session_id:
            continue
        entry_cwd = str(entry.get("cwd") or "").strip()
        head, sep, id_cwd = session_id.partition(":")
        if kind == "full":
            if session_id != value:
                continue
            candidate_cwd = entry_cwd or id_cwd
        else:
            if not sep or head != f"sess_{value}":
                continue
            candidate_cwd = id_cwd or entry_cwd
        if want and clean_path(candidate_cwd) != want:
            continue
        matched.append(
            {
                "key": key,
                "sessionId": session_id,
                "cwd": candidate_cwd,
                "updatedAt": float(entry.get("updatedAt") or 0.0),
            }
        )
    matched.sort(key=lambda item: item["updatedAt"], reverse=True)
    return matched


def hook_url(base_url: str, plugin_name: str) -> str:
    """插件唤醒接口的完整地址（AstrBot 端口，不含查询参数）。"""
    encoded = quote(plugin_name, safe="")
    return f"{base_url.rstrip('/')}{HOOK_API_PREFIX}/{encoded}{HOOK_PATH}"


def hook_marker(plugin_name: str) -> str:
    """写进钩子命令里的识别串（``tag=<插件名>``）。"""
    return f"tag={quote(plugin_name, safe='')}"


def build_hook_command(
    base_url: str,
    plugin_name: str,
    wait_sec: float,
    auto_approve: str = "",
    api_key: str = "",
    plugin_key: str = "",
) -> str:
    """生成写进 alkaid0 ``Hooks.OnCronjobWakeup`` 的命令。

    * ``SESSION_ID`` / ``SESSION_PATH`` 由 alkaid0 的钩子注入；
    * AstrBot API Key **明文**写进 ``X-API-Key`` 请求头；
    * 配了可选的 ``wakeup_api_key`` 时，再加一道 ``X-0528V2-Key`` 头
      （同样明文写进命令）；
    * URL 上带 ``tag=<插件名>``，重装 / 改地址时据此认出并替换旧命令；
    * ``auto_approve`` 非空时作为查询参数带上，让 cron 回合按它临时直通审批。
    """
    timeout = int(max(5.0, float(wait_sec) + 10.0))
    url = f"{hook_url(base_url, plugin_name)}?{hook_marker(plugin_name)}"
    if auto_approve:
        url += f"&auto_approve={quote(auto_approve, safe=',')}"
    payload = """'{"session_id":"'"$SESSION_ID"'","cwd":"'"$SESSION_PATH"'"}'"""
    headers = ""
    if api_key:
        headers += f"-H {shlex.quote(f'{API_KEY_HEADER}: {api_key}')} "
    if plugin_key:
        headers += f"-H {shlex.quote(f'{PLUGIN_KEY_HEADER}: {plugin_key}')} "
    return (
        f"curl -fsS -m {timeout} -X POST '{url}' "
        f"{headers}"
        "-H 'Content-Type: application/json' "
        f"-d {payload}"
    )


def is_our_hook(command: str, plugin_name: str) -> bool:
    """这条 alkaid0 钩子命令是不是本插件写的（按 URL 上的 tag 认）。"""
    return hook_marker(plugin_name) in (command or "")


@dataclass(frozen=True)
class WakeupSettings:
    """唤醒钩子的运行参数（全部来自插件配置）。"""

    enabled: bool = False
    api_key: str = ""
    public_url: str = ""
    wait_sec: float = float(DEFAULT_WAIT_SEC)
    relay: bool = True
    auto_approve: str = ""

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> WakeupSettings:
        """按插件配置解析；缺失 / 空串用默认值。"""
        source: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
        return cls(
            enabled=_as_bool(source.get("wakeup_enable"), False),
            api_key=str(source.get("wakeup_api_key") or "").strip(),
            public_url=str(source.get("wakeup_public_url") or "").strip(),
            wait_sec=float(_as_int(source.get("wakeup_wait_sec"), DEFAULT_WAIT_SEC)),
            relay=_as_bool(source.get("wakeup_relay_reply"), True),
            auto_approve=str(source.get("wakeup_auto_approve") or "").strip(),
        )

    @property
    def usable(self) -> bool:
        """唤醒接口由 AstrBot 提供，打开开关即可用。"""
        return self.enabled

    def base_url(self, dashboard_base: str) -> str:
        """生成钩子命令时用的地址；``wakeup_public_url`` 优先。"""
        if self.public_url:
            return self.public_url.rstrip("/")
        return (dashboard_base or "").rstrip("/") or "http://127.0.0.1:6185"


def _update_of(params: Any, session_id: str) -> dict | None:
    """从通知参数里取属于 ``session_id`` 的 update；不是就返回 ``None``。"""
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    update_session = update.get("sessionId") or params.get("sessionId")
    if isinstance(update_session, str) and update_session and update_session != session_id:
        return None
    return update


class WakeupHookService:
    """唤醒接口（AstrBot 路由）+ alkaid0 钩子同步。

    Args:
        plugin_name: 插件名（URL 里的 tag、日志前缀）。
        config_provider: 返回插件配置（每次请求现读，改了不用重启）。
        runner_settings_provider: 返回 ACP 连接参数（地址等）。
        store_provider: 返回会话记录表。
        pool_provider: 返回连接池；插件还没起来时返回 ``None``。
        send_text: 把一段文本主动发到某个群（``unified_msg_origin``）；``None``
            表示不转发 cron 回合的回复。
        dashboard_base_provider: 返回 AstrBot WebUI 的基地址（例如
            ``http://127.0.0.1:6185``），用来拼钩子命令里的 URL。
    """

    def __init__(
        self,
        *,
        plugin_name: str,
        config_provider: Callable[[], Mapping[str, Any]],
        runner_settings_provider: Callable[[], runner_mod.RunnerSettings],
        store_provider: Callable[[], SessionStore],
        pool_provider: Callable[[], SessionPool | None],
        send_text: Callable[[str, str], Awaitable[bool]] | None = None,
        dashboard_base_provider: Callable[[], str] | None = None,
    ) -> None:
        self.plugin_name = plugin_name
        self._config_provider = config_provider
        self._runner_settings_provider = runner_settings_provider
        self._store_provider = store_provider
        self._pool_provider = pool_provider
        self._send_text = send_text
        self._dashboard_base_provider = dashboard_base_provider
        self._relay_tasks: set[asyncio.Task] = set()
        self._wake_locks: dict[str, asyncio.Lock] = {}
        #: 最近一次同步 / 调用失败的原因（状态命令展示）。
        self.last_error = ""

    # ── 配置 ─────────────────────────────────────────────
    def settings(self) -> WakeupSettings:
        """现读插件配置（面板保存后立即生效）。"""
        try:
            config = self._config_provider()
        except Exception:  # noqa: BLE001 - 读配置失败按默认值处理
            config = None
        return WakeupSettings.from_config(config)

    def dashboard_base(self) -> str:
        """AstrBot WebUI 的基地址（例如 ``http://127.0.0.1:6185``）。"""
        provider = self._dashboard_base_provider
        if provider is None:
            return "http://127.0.0.1:6185"
        try:
            return str(provider() or "").rstrip("/") or "http://127.0.0.1:6185"
        except Exception:  # noqa: BLE001 - 读配置失败用默认值
            return "http://127.0.0.1:6185"

    def endpoint(self) -> str:
        """外部要调用的完整地址（不含 apikey / 查询参数）。"""
        return hook_url(
            self.settings().base_url(self.dashboard_base()), self.plugin_name
        )

    def marker(self) -> str:
        """写进 alkaid0 钩子命令里的识别串。"""
        return hook_marker(self.plugin_name)

    def command(
        self,
        settings: WakeupSettings | None = None,
        *,
        api_key: str = "",
    ) -> str:
        """要写进 ``Hooks.OnCronjobWakeup`` 的 curl 命令。

        ``api_key`` 是本次一键安装刚申请的 AstrBot API Key，明文写进命令。
        """
        current = settings or self.settings()
        return build_hook_command(
            current.base_url(self.dashboard_base()),
            self.plugin_name,
            current.wait_sec,
            current.auto_approve,
            api_key,
            current.api_key,
        )

    def status_line(self) -> str:
        """一行运行状态（``/0528v2`` 用）。"""
        settings = self.settings()
        if not settings.enabled:
            return "未启用"
        return (
            f"AstrBot 路由 {self.endpoint()}"
            f"（等待消息最多 {settings.wait_sec:g} 秒）"
        )

    # ── 生命周期 ─────────────────────────────────────────
    async def sync(self, *, force: bool = False) -> bool:
        """路由由 AstrBot 提供；这里只维护状态并清理旧的转发任务。"""
        settings = self.settings()
        if not settings.usable:
            await self.stop()
            return False
        self.last_error = ""
        return True

    async def stop(self) -> None:
        """停掉所有转发任务（可重复调用）。"""
        tasks = list(self._relay_tasks)
        self._relay_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── HTTP 处理（AstrBot 插件 Web API）──────────────────
    @staticmethod
    def _plugin_key_ok(settings: WakeupSettings) -> bool:
        """可选的第二道口令（``X-0528V2-Key`` / ``?plugin_key=``）。

        AstrBot 的 API Key 鉴权由框架（``require_plugin_scope``）完成；
        这里只处理插件自己配置的额外口令。
        """
        expected = settings.api_key
        if not expected:
            return True
        supplied = (
            request.headers.get(PLUGIN_KEY_HEADER)
            or request.query.get(PLUGIN_KEY_QUERY)
            or ""
        ).strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    async def handle_wakeup(self) -> Any:
        """``POST /<插件名>/wakeup``：唤醒绑定某个 alkaid0 会话的所有群。

        AstrBot 的插件 Web API 不给处理函数传 request，而是把当前请求放进
        :mod:`astrbot.api.web` 的上下文代理（``request``），所以这里不接收形参。
        """
        settings = self.settings()
        method = str(request.method or "").upper() or "?"
        remote = str(request.client_host or "") or "未知来源"
        # alkaid0 的 cron 钩子是阻塞调用，所以每个分支都要在日志里留痕：
        # 至少能确认「这一轮到底有没有被唤醒」以及为什么没成功。
        if not settings.enabled:
            logger.warning(
                "%s: 收到唤醒请求，但唤醒钩子未启用（%s，来自 %s）",
                LOG_PREFIX,
                method,
                remote,
            )
            return json_response(
                {"ok": False, "error": "disabled"}, status_code=503
            )
        if not self._plugin_key_ok(settings):
            logger.warning(
                "%s: 唤醒请求的第二道口令不正确，已拒绝（%s，来自 %s）",
                LOG_PREFIX,
                method,
                remote,
            )
            return json_response(
                {"ok": False, "error": "unauthorized"}, status_code=401
            )

        payload: dict[str, Any] = {}
        if method == "POST":
            try:
                raw = await request.json(default={})
            except Exception:  # noqa: BLE001 - 空 body / 非 JSON 都当没带
                raw = None
            if isinstance(raw, dict):
                payload = raw

        session_arg = str(
            payload.get("session_id")
            or payload.get("sessionId")
            or request.query.get("session_id")
            or ""
        ).strip()
        cwd = str(
            payload.get("cwd")
            or payload.get("session_path")
            or request.query.get("cwd")
            or ""
        ).strip()
        if not session_arg:
            logger.warning(
                "%s: 唤醒请求没带 session_id，已拒绝（%s，来自 %s）",
                LOG_PREFIX,
                method,
                remote,
            )
            return json_response(
                {"ok": False, "error": "missing_session_id"}, status_code=400
            )

        # 外部钩子的「直通」：true / 工具名列表 / 逗号分隔字符串都认。
        auto_approve = payload.get(
            "auto_approve",
            payload.get(
                "autoApprove",
                request.query.get("auto_approve", request.query.get("autoApprove")),
            ),
        )
        logger.info(
            "%s: 收到唤醒请求：session_id=%s cwd=%s（%s，来自 %s，auto_approve=%s）",
            LOG_PREFIX,
            session_arg,
            cwd or "-",
            method,
            remote,
            "带" if auto_approve not in (None, "") else "无",
        )
        result = await self.wake(session_arg, cwd, auto_approve=auto_approve)
        status = 200
        if not result.get("ok"):
            error = str(result.get("error") or "")
            status = {
                "invalid_session_id": 400,
                "no_binding": 404,
                "not_ready": 503,
                "no_message": 504,
                "wake_failed": 502,
            }.get(error, 502)
        bindings = result.get("bindings") or []
        if result.get("ok"):
            logger.info(
                "%s: 唤醒完成：会话 %s（%d 个群，转发 %d 个，用时 %ss）",
                LOG_PREFIX,
                session_arg,
                len(bindings),
                sum(1 for item in bindings if item.get("relay")),
                result.get("elapsed"),
            )
        else:
            reasons = "；".join(
                f"{item.get('conv_key') or '?'}: {item.get('error')}"
                for item in bindings
                if item.get("error")
            )
            logger.warning(
                "%s: 唤醒失败（%s）：会话 %s，cwd=%s%s，用时 %ss",
                LOG_PREFIX,
                result.get("error") or "unknown",
                session_arg,
                cwd or "-",
                f"，原因：{reasons}" if reasons else "",
                result.get("elapsed"),
            )
        return json_response(result, status_code=status)

    # ── 唤醒逻辑 ─────────────────────────────────────────
    async def wake(
        self,
        session_arg: str,
        cwd: str = "",
        *,
        auto_approve: Any = None,
    ) -> dict[str, Any]:
        """唤醒绑定 ``session_arg`` 的所有群，并等它们收到这一轮的消息。

        ``auto_approve`` 来自外部钩子的请求参数：``True`` 表示本轮全部工具直通，
        也可以是工具名列表 / 逗号分隔字符串；仍受插件配置的总开关和
        ``auto_approve_hook`` 限制。
        """
        kind, _value = parse_session_token(session_arg)
        started = time.monotonic()
        if not kind:
            return {
                "ok": False,
                "error": "invalid_session_id",
                "session_id": session_arg,
                "bindings": [],
            }

        try:
            entries = self._store_provider().entries()
        except Exception as exc:  # noqa: BLE001 - 记录读不出来只能如实上报
            logger.warning("%s: 唤醒钩子读取会话记录失败：%s", LOG_PREFIX, exc)
            return {
                "ok": False,
                "error": "store_error",
                "session_id": session_arg,
                "detail": str(exc),
                "bindings": [],
            }

        bindings = match_bindings(entries, session_arg, cwd)
        if not bindings:
            return {
                "ok": False,
                "error": "no_binding",
                "session_id": session_arg,
                "cwd": cwd,
                "bindings": [],
            }

        pool = self._pool_provider()
        if pool is None:
            return {
                "ok": False,
                "error": "not_ready",
                "session_id": session_arg,
                "bindings": [],
            }

        settings = self.settings()
        results: list[dict[str, Any]] = []
        for binding in bindings:
            results.append(
                await self._wake_one(
                    binding, settings, pool, auto_approve=auto_approve
                )
            )

        woken = [item for item in results if item.get("woken")]
        received = [item for item in results if item.get("received")]
        ok = bool(results) and len(received) == len(results)
        if ok:
            error = ""
        elif not woken:
            error = "wake_failed"
        elif not received:
            error = "no_message"
        else:
            error = "partial"
        return {
            "ok": ok,
            "error": error,
            "session_id": session_arg,
            "cwd": cwd,
            "bindings": results,
            "elapsed": round(time.monotonic() - started, 3),
        }

    async def _wake_one(
        self,
        binding: Mapping[str, Any],
        settings: WakeupSettings,
        pool: SessionPool,
        *,
        auto_approve: Any = None,
    ) -> dict[str, Any]:
        """唤醒一个群，并（可选）把 cron 回合的回复转发到群聊。

        ``auto_approve`` 是外部钩子带的一次性授权；只在本次唤醒的会话上生效，
        到本轮结束（或 TTL 到期）就清掉。
        """
        key = str(binding.get("key") or "")
        session_id = str(binding.get("sessionId") or "")
        cwd = str(binding.get("cwd") or "")
        started = time.monotonic()
        # 已经有活跃连接且绑的就是这个会话：常驻主动推送会处理这一轮，
        # 这里只负责等消息送达，不要再 relay 一遍（否则群里会收到两份）。
        result: dict[str, Any] = {
            "conv_key": key,
            "session_id": session_id,
            "cwd": cwd,
            "woken": False,
            "received": False,
            "update": "",
            "relay": False,
            "error": "",
        }
        if not key or not cwd:
            result["error"] = "会话记录缺少群键或工作目录"
            return result

        try:
            runner_settings = self._runner_settings_provider()
        except Exception as exc:  # noqa: BLE001 - 配置读不出来就只能失败
            result["error"] = f"读取连接参数失败：{exc}"
            return result

        lock = self._wake_locks.setdefault(f"{key}\x00{session_id}", asyncio.Lock())
        session: GroupSession | None = None
        async with lock:
            try:
                session = await pool.prepare(key, url=runner_settings.url, cwd=cwd)
                if auto_approve is not None:
                    if (
                        runner_settings.auto_approve_enable
                        and runner_settings.auto_approve_hook
                    ):
                        grant = approval_mod.grant_from_value(
                            auto_approve, source="外部钩子"
                        )
                    else:
                        logger.warning(
                            "%s: 外部钩子请求自动批准，但插件总开关或 auto_approve_hook 未开启，忽略",
                            LOG_PREFIX,
                        )
                        grant = approval_mod.EMPTY_GRANT
                    ttl = max(
                        60.0,
                        float(runner_settings.turn_timeout_sec) + settings.wait_sec,
                    )
                    session.set_external_grant(grant, ttl=ttl)
                # 在续接之前挂监听：服务端补发的流式快照也要算「已经收到」
                session.watch_deliveries()
                before = session.delivery_seq
                # prepare 可能因为地址 / 目录变了换掉连接，所以在这里重新判断
                # 「是不是已有活跃连接」，别拿 prepare 之前的快照下结论。
                owns_wake = not (
                    session.connected and session.session_id == session_id
                )
                result["relay"] = bool(
                    settings.relay and self._send_text is not None and owns_wake
                )
                logger.debug(
                    "%s: 唤醒群 %s：已有活跃连接=%s，转发=%s",
                    LOG_PREFIX,
                    key,
                    not owns_wake,
                    result["relay"],
                )
                if result["relay"]:
                    await self._relay(
                        session, binding, settings, runner_settings, before, result
                    )
                elif not owns_wake:
                    # 已有活跃连接：这一轮归常驻主动推送消费。这里只等消息送达，
                    # **不要拿会话锁** —— 否则会挡住主动推送判断「没有本地回合」，
                    # 群里反而收不到 cron 的回复。
                    result["woken"] = True
                    received, kind = await session.wait_delivery(
                        before, settings.wait_sec
                    )
                    result["received"] = received
                    result["update"] = kind
                else:
                    # 这一轮由唤醒钩子负责（配置关闭 relay 时也不让主动推送插手）
                    session.external_relay = True
                    try:
                        async with session.lock:
                            await session.ensure_existing(session_id)
                            result["woken"] = True
                            received, kind = await session.wait_delivery(
                                before, settings.wait_sec
                            )
                            result["received"] = received
                            result["update"] = kind
                    finally:
                        session.external_relay = False
            except Exception as exc:  # noqa: BLE001 - 单个群失败不影响其它群
                result["error"] = str(exc) or exc.__class__.__name__
                # 续接失败 / 还没真正唤醒：这次外部授权不能留给下一轮本地对话。
                if session is not None and not result["woken"]:
                    session.clear_external_grant()
                logger.warning(
                    "%s: 唤醒群 %s 的会话 %s 失败：%s",
                    LOG_PREFIX,
                    key,
                    session_id,
                    exc,
                )
            result["elapsed"] = round(time.monotonic() - started, 3)
            return result

    async def _relay(
        self,
        session: GroupSession,
        binding: Mapping[str, Any],
        settings: WakeupSettings,
        runner_settings: runner_mod.RunnerSettings,
        before: int,
        result: dict[str, Any],
    ) -> None:
        """续接会话、等消息送达，并起一个后台任务把这一轮回复发到群里。"""
        session_id = str(binding["sessionId"])
        queue: asyncio.Queue[dict] = asyncio.Queue()
        unsubscribe = session.client.subscribe(
            lambda _method, params: queue.put_nowait(params)
        )
        ready = asyncio.Event()
        failure: dict[str, BaseException] = {}

        async def _serve() -> None:
            try:
                async with session.lock:
                    try:
                        await session.ensure_existing(session_id)
                    except BaseException as exc:  # noqa: BLE001 - 交给调用方上报
                        failure["error"] = exc
                        return
                    finally:
                        ready.set()
                    await self._consume_relay(
                        session, binding, runner_settings, queue
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台转发失败只记日志
                logger.warning(
                    "%s: 转发群 %s 的 cron 回合失败：%s", LOG_PREFIX, session.key, exc
                )
            finally:
                unsubscribe()
                session.external_relay = False
                session.clear_external_grant()
                session.touch()

        task = asyncio.create_task(
            _serve(), name=f"0528v2-cron-relay-{session.key}"
        )
        # 后台 relay 接管这一轮：常驻主动推送不要重复收集（任务启动前就置位，
        # 避免 resume 太快、第一条通知抢在标记之前到达）。
        session.external_relay = True
        logger.info(
            "%s: 群 %s 续接会话 %s，开始转发 cron 回合",
            LOG_PREFIX,
            session.key,
            session_id,
        )
        self._relay_tasks.add(task)
        task.add_done_callback(self._relay_tasks.discard)

        try:
            await asyncio.wait_for(ready.wait(), timeout=max(5.0, settings.wait_sec))
        except asyncio.TimeoutError:
            task.cancel()
            raise RuntimeError(
                f"等待续接会话超时（{settings.wait_sec:g}s）"
            ) from None
        if "error" in failure:
            raise failure["error"]
        result["woken"] = True
        received, kind = await session.wait_delivery(before, settings.wait_sec)
        result["received"] = received
        result["update"] = kind

    async def _consume_relay(
        self,
        session: GroupSession,
        binding: Mapping[str, Any],
        runner_settings: runner_mod.RunnerSettings,
        queue: asyncio.Queue[dict],
    ) -> None:
        """消费这一轮的通知，结束后把正文与过程消息发到群里。"""
        key = str(binding["key"])
        session_id = str(binding["sessionId"])
        stream = StreamBuffer(self._send, key)
        echo = runner_mod.ProgressEcho(enabled=runner_settings.echo_events)
        turn_timeout = max(5.0, float(runner_settings.turn_timeout_sec))
        deadline = time.monotonic() + turn_timeout
        #: 一直没消息就早点收工（多半是这一轮早在连接前就跑完了）
        first_deadline = time.monotonic() + max(1.0, float(self.settings().wait_sec))
        seen = False
        stop_reason = ""
        error_msg = ""
        last_state = ""
        timed_out = False
        #: 自动批准的序号起点：确认这次 requires_action 是否已经直通。
        approval_seq = int(getattr(session, "approval_seq", 0))

        while True:
            now = time.monotonic()
            if not seen and now >= first_deadline:
                break
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                logger.warning(
                    "%s: 转发群 %s 的 cron 回合超时（%g 秒）",
                    LOG_PREFIX,
                    key,
                    turn_timeout,
                )
                break
            try:
                params = await asyncio.wait_for(
                    queue.get(), timeout=min(1.0, remaining)
                )
            except asyncio.TimeoutError:
                continue

            update = _update_of(params, session_id)
            if update is None:
                continue
            kind = str(update.get("sessionUpdate") or "")
            if kind in DELIVERY_UPDATE_KINDS:
                seen = True

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
                    elif last_state != state:
                        # 未命中直通：群里得知道可以 /aapprove（同普通回合的提示）
                        await stream.send_now(runner_mod.permission_hint(session))
                last_state = state
            else:
                logger.debug(
                    "%s: 转发群 %s 时忽略事件 %s", LOG_PREFIX, key, kind
                )

        if stop_reason == acp.STOP_REASON_CANCELLED:
            await stream.send_now("🛑 本轮已终止")
        if error_msg:
            await stream.send_now(f"⚠️ {error_msg}")
        if timed_out:
            await stream.send_now("⚠️ 未能等到 cron 回合结束，以上是已收到的内容。")
        await stream.flush()
        if stream.messages:
            logger.info(
                "%s: 已把 cron 回合的回复流式转发到群 %s（%d 段 / %d 字，stopReason=%s）",
                LOG_PREFIX,
                key,
                stream.messages,
                stream.sent_chars,
                stop_reason or "未知",
            )

    async def _send(self, unified_msg_origin: str, text: str) -> bool:
        """把一段文本主动发到聊天里（失败只记日志）。"""
        if not text.strip() or self._send_text is None:
            return False
        try:
            return bool(await self._send_text(unified_msg_origin, text))
        except Exception as exc:  # noqa: BLE001 - 发送失败不该拖垮转发任务
            logger.warning(
                "%s: 主动发送到 %s 失败：%s", LOG_PREFIX, unified_msg_origin, exc
            )
            return False


__all__ = [
    "API_KEY_HEADER",
    "ASTRBOT_KEY_NAME",
    "CONFIG_KEYS",
    "DEFAULT_WAIT_SEC",
    "HOOK_API_PREFIX",
    "HOOK_ENV_KEY",
    "HOOK_PATH",
    "HOOK_PLUGIN_ENV_KEY",
    "PLUGIN_KEY_HEADER",
    "PLUGIN_KEY_QUERY",
    "WakeupHookService",
    "WakeupSettings",
    "build_hook_command",
    "clean_path",
    "hook_marker",
    "hook_url",
    "is_our_hook",
    "match_bindings",
    "parse_session_token",
]
