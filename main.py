"""astrbot_plugin_0528_v2 —— AstrBot ↔ alkaid0 中继插件。

当前职责：

1. **前置插件管理**：启动时确保 ``astrbot_plugin_libi18nject``（WebUI 文案注入）与
   ``astrbot_plugin_librunner``（Agent Runner 注入接口）就绪 —— 缺失就自动下载并
   就地启用，装不上则停止工作并把原因交给 WebUI（见 :mod:`prerequisite`）；
2. **注册自定义执行器**：用 librunner 注册 ``alkaid0`` 执行器（见 :mod:`agent_runner`）。
   在 WebUI 的「AI → Agent 执行方式」里选中它之后，群消息就交给 alkaid0 处理
   （ACP v2 over WebSocket）；同一个群里**同时只跑一轮**，这一轮期间别人插的话排队
   攒批（见 :mod:`turn_queue`，插话顺手记进 SQLite，服务端半路被停掉也不丢）；
3. **连接池**：一个群一条 WebSocket 连接、一个活跃 ACP 会话，群空闲超时后自动回收
   （见 :mod:`acp.pool`）；
4. **命令**：``/0528v2`` 查看前置、执行器与连接池的现状；``/actx`` 查看本群会话的
   上下文用量（``context x% (used/size)``）；``/areset`` 重置本群会话、
   ``/astop`` 中止本群正在跑的那一轮（思考中 / 跑工具时都能叫停）；
   ``/amodel`` 查看 / 切换本群会话使用的模型、``/aeffort`` 调推理强度
   （都是会话级，只动这一个群）；``/aresume`` 列出本群工作目录下的历史会话
   （或切回其中某一个）；``/acmd`` 列出并执行远端（alkaid0）自己的斜杠命令；
   ``/aclean`` 强制回收本群连接（保留会话记录，下一条消息续接）；
   ``/acron`` 列出本群会话的定时任务、``/acron remove <id>`` 删除其中一条
   （服务端需声明 ``alk.cxykevin.top/cron``）；命令权限交给 AstrBot 上层配置。

两个前置插件的完整 API 说明、参数语义、示例与坑都在插件目录下的 ``PLUGIN_APIS.md``
里（本地开发参考文档，已在 ``.gitignore`` 中排除，**不要提交进仓库**）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, MutableMapping
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from . import agent_runner as runner_mod
from . import panel as panel_mod
from . import prerequisite as prerequisite_mod
from . import proactive as proactive_mod
from . import remote_config as remote_config_mod
from . import wakeup_hook as wakeup_mod
from .acp import approval as approval_mod
from .acp import commands as acp_commands
from .acp import pool as pool_mod
from .acp import protocol as acp_protocol
from .acp.store import SessionStore
from .panel import mask_url
from .turn_queue import TurnQueue

#: 插件名（与 metadata.yaml / 目录名一致）。
PLUGIN_NAME = "astrbot_plugin_0528_v2"

#: 插件版本（与 metadata.yaml 的 version 保持一致）。
PLUGIN_VERSION = "v0.3.0"

#: 日志前缀。
LOG_PREFIX = prerequisite_mod.LOG_PREFIX

#: ``/amodel`` 的用法说明（列表末尾也会带上）。
MODEL_COMMAND_HELP = "用法：/amodel 列出可选模型，/amodel <编号 或 名字> 切到那个模型"

#: ``/aeffort`` 的用法说明。
EFFORT_COMMAND_HELP = "用法：/aeffort 列出可选推理强度，/aeffort <值 或 名字> 切到那个强度"


def config_choices(option: dict | None) -> list[dict]:
    """把「下拉型」配置项洗成可切换的值列表。

    每项是 ``{"value", "name", "key", "aliases"}``：

    * ``value``：发给服务端的原值（模型形如 ``<index>/<modelConfigID>``，扩展文档 §5.2）；
    * ``key``：列表里给用户看的短标识 —— 模型用 ``/`` 前的编号，推理强度这类就是值本身；
    * ``aliases``：额外认的写法（模型的 ``/`` 后半段，也就是配置里写的模型 ID）。
    """
    choices: list[dict] = []
    for item in acp_protocol.option_choices(option):
        value, name = acp_protocol.option_choice_label(item)
        if not value:
            continue
        head, _, tail = value.partition("/")
        choices.append(
            {"value": value, "name": name, "key": head, "aliases": [tail] if tail else []}
        )
    return choices


def find_choice(choices: list[dict], token: str) -> tuple[dict | None, list[dict]]:
    """按用户输入找配置项的值，返回 ``(命中的那个, 同义候选)``。

    顺序：完整值（``1/a`` / ``low``）→ 短标识、编号（``1``）→ 展示名与别名
    （``Low`` / ``a``）；大小写不敏感。撞车时由调用方提示用户改用编号。
    """
    text = (token or "").strip()
    if not text:
        return None, []
    lowered = text.lower()
    for choice in choices:
        if choice["value"] == text:
            return choice, [choice]
    by_key = [choice for choice in choices if choice["key"].lower() == lowered]
    if by_key:
        return by_key[0], by_key
    by_name = [
        choice
        for choice in choices
        if choice["name"].lower() == lowered
        or any(alias.lower() == lowered for alias in choice.get("aliases") or ())
    ]
    if not by_name:
        return None, []
    return by_name[0], by_name


def render_choice_list(
    choices: list[dict], current: str, *, title: str, help_text: str
) -> str:
    """渲染可切换值列表；``← 当前`` 标出正在用的那个。

    值的 ``key`` 与 ``value`` 不同（模型是 ``编号/模型ID``）时把编号放在行首，
    相同（推理强度）时省掉那份重复。
    """
    lines = [f"{title}（{len(choices)} 个）:"]
    for choice in choices:
        prefix = f"{choice['key']}. " if choice["key"] != choice["value"] else ""
        marker = "  ← 当前" if choice["value"] == current else ""
        lines.append(f"- {prefix}{choice['name']}（{choice['value']}）{marker}")
    lines.append(help_text)
    return "\n".join(lines)


#: ``/aresume`` 的用法说明（列表末尾也会带上）。
RESUME_COMMAND_HELP = (
    "用法：/aresume 列出本群工作目录下的会话，/aresume <会话号 或 sessionId> 切到那个会话"
)


def session_short_id(session_id: str) -> str:
    """``sess_12:/path`` → ``12``（认不出格式就原样返回）。"""
    text = (session_id or "").strip()
    head = text.split(":", 1)[0]
    if head.startswith("sess_") and len(head) > len("sess_"):
        return head[len("sess_") :]
    return text


def resolve_session_arg(token: str, cwd: str) -> str:
    """把 ``/aresume`` 的参数补成完整 sessionId。

    纯数字当作「会话号」，补成 ``sess_<号>:<cwd>``（sessionId 的格式见扩展文档
    §5.1）；其它写法（完整 sessionId）原样透传，交给服务端校验。
    """
    text = (token or "").strip()
    return f"sess_{text}:{cwd}" if text.isdigit() else text


def format_updated_at(value: str) -> str:
    """把服务端的 RFC 3339（UTC）时间转成本地 ``MM-DD HH:MM``。

    服务端对「没记过时间」的老会话返回 Unix 纪元（1970-01-01），这时返回空串。
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.year <= 1970:
        return ""
    return stamp.astimezone().strftime("%m-%d %H:%M")


#: ``/acmd`` 的用法说明（列表末尾也会带上）。
COMMAND_COMMAND_HELP = (
    "用法：/acmd 列出远端命令，/acmd <命令> [参数] 执行它（开头的 / 不用写，会自动补）"
)


def render_command_list(
    commands: list[dict], *, translated: bool, help_text: str = COMMAND_COMMAND_HELP
) -> str:
    """渲染远端（alkaid0）的命令列表。

    Args:
        commands: :func:`acp.commands.available_commands_of` 洗过的命令列表。
        translated: 服务端是否声明了 alkaid0 扩展能力（``v0.4``）—— 声明了才用
            与 alcoh 同步的对照表把英文描述翻成中文，否则照原文显示（语言未知，
            不猜译）。
        help_text: 末尾的用法说明。
    """
    if not commands:
        return (
            "Alkaid0 还没下发命令列表（服务端在开会话 / 续接会话时给一份）。\n"
            "过一条消息再看，或者 /areset 重建本群会话试试。\n" + help_text
        )

    lines = [f"Alkaid0 的远端命令（{len(commands)} 个）:"]
    for item in commands:
        hint = str(item.get("hint") or "").strip()
        description = str(item.get("description") or "").strip()
        if description and translated:
            description = acp_commands.translate_description(description)
        line = f"- /{item.get('name', '')}"
        if hint:
            line += f"（{hint}）"
        if description:
            line += f"  {description}"
        lines.append(line)
    if not translated:
        lines.append("（服务端没声明 alkaid0 扩展能力，命令说明照原文显示）")
    lines.append(help_text)
    return "\n".join(lines)


def render_session_list(
    sessions: list[dict], current: str, *, cwd: str, more: bool = False
) -> str:
    """渲染会话列表；``← 当前`` 标出本群正在续接的那个。"""
    if sessions:
        lines = [f"本群工作目录下的会话（{len(sessions)} 个，最近活动在前）:"]
    else:
        lines = [f"本群工作目录（{cwd}）下还没有会话。"]
    for item in sessions:
        session_id = str(item.get("sessionId") or "")
        title = str(item.get("title") or "").strip() or "（无标题）"
        if len(title) > 24:
            title = f"{title[:23]}…"
        when = format_updated_at(str(item.get("updatedAt") or ""))
        marker = "  ← 当前" if session_id and session_id == current else ""
        stamp = f"  {when}" if when else ""
        lines.append(f"- {session_short_id(session_id)}. {title}{stamp}{marker}")
    if more:
        lines.append("（还有更早的会话，这里只显示最近一批）")
    lines.append(RESUME_COMMAND_HELP)
    return "\n".join(lines)


def render_context_usage(usage: tuple[int, int] | None) -> str:
    """把 ``(used, size)`` 渲染成 ``context x% (used/size)``。

    百分比与 alcoh（同一套客户端口径）保持一致：``min(int(used / size * 100), 100)``
    —— 整数截断、封顶 100%（``used`` 含本轮输出，理论上可能超过窗口）。
    服务端没报出窗口大小（``size`` 为 0 或字段缺失）时只报已用量。

    Args:
        usage: :attr:`acp.pool.GroupSession.usage` 里的 ``(used, size)``；``None`` 表示还没有。

    Returns:
        一行文本；``usage`` 为 ``None`` 时返回空串（调用方据此提示「还没拿到用量」）。
    """
    if usage is None:
        return ""
    used, size = usage
    if size <= 0:
        return f"context {used}"
    return f"context {min(int(used / size * 100), 100)}% ({used}/{size})"


#: ``/acron`` 的用法说明。
CRON_COMMAND_HELP = (
    "用法：/acron 列出本群会话的定时任务，/acron remove <id> 删除其中一条"
)


def parse_cron_job_id(token: str) -> int | None:
    """把 ``/acron remove`` 的参数解析成正整数任务 id；不合法返回 ``None``。"""
    text = (token or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


def _one_line(text: object, limit: int = 60) -> str:
    """把多行文本压成一行并截断（任务提示词可能很长）。"""
    raw = " ".join(str(text or "").split())
    return f"{raw[: limit - 1]}…" if len(raw) > limit else raw


def render_cron_jobs(jobs: list[dict]) -> str:
    """渲染本群会话的定时任务（id / cron / 周期 / 下次触发 / 提示词摘要）。"""
    if not jobs:
        return f"本群会话没有定时任务。\n{CRON_COMMAND_HELP}"
    lines = [f"本群会话的定时任务（{len(jobs)} 个）:"]
    for job in jobs:
        kind = "一次性" if job.get("runOnce") else "周期"
        next_runs = job.get("nextRuns")
        next_text = f"  下次 {next_runs[0]}" if isinstance(next_runs, list) and next_runs else ""
        lines.append(
            f"- {job.get('id', '?')}. {str(job.get('cron') or '?').strip()}"
            f"  {kind}{next_text}"
        )
        prompt = _one_line(job.get("prompt"))
        if prompt:
            lines.append(f"    {prompt}")
    lines.append(CRON_COMMAND_HELP)
    return "\n".join(lines)


#: 状态命令会把生效地址发到群里，所以地址脱敏由 :mod:`panel` 统一处理
#: （``?key=***``、userinfo 里的密码也会打码）。


class Plugin0528V2(Star):
    """0528 v2 插件主体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        # 保留传入的 AstrBotConfig 对象（面板保存要靠它的 save_config_async）；
        # 空配置也不能换成普通 dict。
        self.plugin_config: dict[str, Any] = config if config is not None else {}
        #: 最近一次启动时的前置检查结果（供状态命令展示）。
        self._prereq_report: prerequisite_mod.MultiEnsureResult | None = None
        #: 本插件就地拉起的前置实例，terminate 时释放。
        self._activated: list[Any] = []
        #: 连接池与闸门共用的 SQLite（懒创建）。
        self._store: SessionStore | None = None
        #: 群 → ACP 连接的表（懒创建，terminate 时释放）。
        self._pool: pool_mod.SessionPool | None = None
        #: 群内并发闸门（懒创建；插话落库，启动时恢复）。
        self._turn_queue: TurnQueue | None = None
        #: 启动时从库里恢复回来的插话条数（状态命令展示用）。
        self._restored_pending = 0
        #: ``alkaid0`` 执行器是否已注册进 librunner。
        self._runner_registered = False
        #: WebUI 配置面板（配置树 + 保存接口），initialize 时注册路由。
        self._panel = panel_mod.PanelService(
            self.plugin_config,
            plugin_name=PLUGIN_NAME,
            version=PLUGIN_VERSION,
            panel_config_provider=self._panel_config,
            after_save=self._after_panel_save,
            hook_action=self._panel_hook_action,
            hook_status_provider=self._panel_hook_status,
            hook_status_async_provider=self._panel_hook_status_async,
        )
        #: alkaid0 全局配置的远程读写（走 ACP config/get|set|reload）。
        self._remote_config = remote_config_mod.RemoteConfigService(
            plugin_name=PLUGIN_NAME,
            url_provider=lambda: self._settings().url,
        )
        #: 唤醒钩子：复用 AstrBot 自己的 Web 端口（注册成插件 Web API）。
        self._hook = wakeup_mod.WakeupHookService(
            plugin_name=PLUGIN_NAME,
            config_provider=lambda: self.plugin_config,
            runner_settings_provider=self._settings,
            store_provider=self._ensure_store,
            pool_provider=lambda: self._pool,
            send_text=self._send_to_chat,
            dashboard_base_provider=self._dashboard_base,
        )
        #: 最近一次手动写入 / 移除 alkaid0 cron 钩子的结果（面板与状态命令展示）。
        self._hook_sync_note = ""
        #: 本插件写的钩子当前是否在 alkaid0 里；``None`` 表示还没手动同步过。
        self._hook_installed: bool | None = None
        #: 没有本地 prompt 的主动回合（cron 唤醒 / 其它客户端发起）→ 推回群聊。
        self._proactive = proactive_mod.ProactiveRelay(
            settings_provider=self._settings,
            send_text=self._send_to_chat,
        )

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件启用：先确保两个前置插件就绪，再注册执行器、拉起连接池。"""
        self._panel.register(self.context)
        self._remote_config.register(self.context)
        self._register_wakeup_route()
        await self._ensure_prerequisites()
        self._ensure_pool()
        self._ensure_queue()
        self._register_runner()
        await self._hook.sync()
        logger.info("%s: 插件已启用（%s）", LOG_PREFIX, PLUGIN_VERSION)

    async def terminate(self) -> None:
        """插件停用：先撤自己的注册与连接，再释放就地拉起的前置实例。

        顺序不能反：执行器注册与连接都是「业务」，要在前置（librunner / libi18nject）
        还在的时候撤销；随后再逆序释放前置实例。
        """
        self._panel.unregister(self.context)
        self._remote_config.unregister(self.context)
        self._unregister_wakeup_route()
        self._unregister_runner()
        await self._hook.stop()
        await self._shutdown_pool()
        # 队列里还排着的插话留在库里，下次启动捞回来接着处理（见 _ensure_queue）
        self._turn_queue = None

        for instance in reversed(self._activated):
            try:
                await instance.terminate()
            except Exception as exc:  # noqa: BLE001 - 释放失败不应阻塞卸载
                logger.warning(
                    "%s: 释放就地拉起的前置实例失败：%s", LOG_PREFIX, exc, exc_info=True
                )
        if self._activated:
            logger.info("%s: 已释放 %d 个就地拉起的前置实例", LOG_PREFIX, len(self._activated))
        self._activated.clear()

    # ---------------------------------------------------------- 前置插件管理
    async def _ensure_prerequisites(self) -> prerequisite_mod.MultiEnsureResult:
        """确保全部前置插件就绪；未就绪且开启强制时抛 :class:`PrerequisiteError`。"""
        enforce = bool(self.plugin_config.get("enforce_prerequisite", True))
        prereqs = prerequisite_mod.build_prerequisites(self.plugin_config)
        report = await prerequisite_mod.ensure_all(
            prereqs,
            context=self.context,
            auto_install=enforce,
        )
        self._prereq_report = report
        self._activated = list(report.instances)

        if report.ready:
            logger.info("%s: 前置插件全部就绪\n%s", LOG_PREFIX, report.describe())
            return report

        detail = report.describe()
        if enforce:
            # 抛出后由内核记入插件页的「加载失败插件」——本插件停止工作（不打补丁、
            # 不注册任何东西），避免带着残缺的前置跑起来。
            raise prerequisite_mod.PrerequisiteError(
                "前置插件未就绪：\n"
                f"{detail}\n"
                "（可在插件配置里关掉「强制前置插件」先用起来，或把「前置插件来源」"
                "换成可访问的镜像地址）"
            )
        logger.warning(
            "%s: 前置插件未就绪，但「强制前置插件」已关闭，继续启用\n%s",
            LOG_PREFIX,
            detail,
        )
        return report

    # ------------------------------------------------------------ 执行器注册
    def _store_path(self) -> Path:
        """会话记录与排队插话（SQLite）的落盘位置。

        放在插件自己的数据目录里 —— 插件更新/重装不会动它，既实现「重启后接着聊」，
        也实现「重启后排队插话不丢」（见 :mod:`turn_queue`）。
        """
        return prerequisite_mod.plugin_data_path() / PLUGIN_NAME / "sessions.db"

    def _ensure_store(self) -> SessionStore:
        """连接池与闸门共用的那份 SQLite（懒创建，只建一次）。"""
        if self._store is None:
            self._store = SessionStore(self._store_path())
        return self._store

    def _ensure_queue(self) -> TurnQueue:
        """创建群内并发闸门（幂等），并把库里留着的插话捞回队列。

        捞回来的插话是「孤儿」（原来那条消息的事件早没了），等这个群**下一条消息**来带队：
        那条消息会和它们攒成一批一起发出去 —— 上一轮跑到一半服务端被停掉也不会丢话。
        """
        if self._turn_queue is None:
            queue = TurnQueue(self._ensure_store())
            self._restored_pending = queue.restore()
            if self._restored_pending:
                logger.info(
                    "%s: 已把上次没处理完的 %d 条插话捞回队列（等下一个消息带队）",
                    LOG_PREFIX,
                    self._restored_pending,
                )
            self._turn_queue = queue
        return self._turn_queue

    def _panel_config(self) -> Any:
        """读执行器面板里的实配值（``agent_runner.config``）；读不到就返回 None。

        消息路径由内核把这份配置当作 ``provider_config`` 传给 Runner；命令路径
        （``/0528v2``、``/amodel``、``/aeffort``、``/aresume``、``/acmd``）没有这条路，
        得自己从全局配置里读 —— 两条路必须解析出同一份参数，否则同一个群的连接会在
        两种地址之间被反复「参数已变更」回收重建，缺 key 的那种直接 401。
        """
        try:
            config = self.context.get_config()
        except Exception as exc:  # noqa: BLE001 - 读不到配置不该挡住命令
            logger.debug("%s: 读取全局配置失败，回落到插件配置：%s", LOG_PREFIX, exc)
            return None
        return runner_mod.panel_config_of(config)

    def _settings(self) -> runner_mod.RunnerSettings:
        """解析连接参数：面板值优先、插件配置兜底、最后代码默认。"""
        return runner_mod.RunnerSettings.from_config(
            self._panel_config(), self.plugin_config
        )

    def _dashboard_base(self) -> str:
        """AstrBot WebUI 的基地址（唤醒钩子复用它的端口）。"""
        try:
            config = self.context.get_config()
        except Exception:  # noqa: BLE001 - 读不到就用默认端口
            config = {}
        dashboard = (
            config.get("dashboard") if isinstance(config, Mapping) else None
        )
        dashboard = dashboard if isinstance(dashboard, Mapping) else {}
        try:
            port = int(dashboard.get("port") or 6185)
        except (TypeError, ValueError):
            port = 6185
        host = str(dashboard.get("host") or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def _register_wakeup_route(self) -> None:
        """把唤醒接口注册成 AstrBot 插件 Web API（复用 6185 端口）。"""
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.debug(
                "%s: 当前 context 不支持注册 Web API，跳过唤醒路由", LOG_PREFIX
            )
            return
        route = f"/{PLUGIN_NAME}{wakeup_mod.HOOK_PATH}"
        register(
            route,
            self._hook.handle_wakeup,
            ["GET", "POST"],
            "alkaid0 cron 唤醒钩子",
        )
        logger.info("%s: 唤醒钩子接口已注册 %s", LOG_PREFIX, route)

    def _unregister_wakeup_route(self) -> None:
        """插件停用时摘掉唤醒接口。"""
        routes = getattr(self.context, "registered_web_apis", None)
        if not isinstance(routes, list):
            return
        route = f"/{PLUGIN_NAME}{wakeup_mod.HOOK_PATH}"
        routes[:] = [
            item
            for item in routes
            if not (
                isinstance(item, tuple)
                and item
                and str(item[0]) == route
            )
        ]

    async def _drop_wakeup_api_keys(self) -> int:
        """删除本插件自动申请的 AstrBot API Key（按固定名字）。"""
        db = self.context.get_db()
        removed = 0
        for key in await db.list_api_keys():
            if str(getattr(key, "name", "")) != wakeup_mod.ASTRBOT_KEY_NAME:
                continue
            if await db.delete_api_key(key.key_id):
                removed += 1
        return removed

    async def _create_wakeup_api_key(self) -> str:
        """申请（并替换）一个 ``plugin`` scope 的 AstrBot API Key，返回明文。"""
        from astrbot.dashboard.services.api_key_service import ApiKeyService

        db = self.context.get_db()
        await self._drop_wakeup_api_keys()
        service = ApiKeyService(db)
        result = await service.create_api_key(
            {"name": wakeup_mod.ASTRBOT_KEY_NAME, "scopes": ["plugin"]},
            created_by=PLUGIN_NAME,
        )
        return str(result.get("api_key") or "")

    async def _after_panel_save(self, changed: dict[str, Any]) -> bool:
        """配置面板保存后的运行时刷新；返回是否需要重载插件才生效。

        连接池的空闲阈值是启动时抄进池子里的，保存后要同步一下；其它参数
        （地址 / 模板 / 超时 / 回显开关）在下一条消息重新解析配置时自然生效，
        不需要断连接。前置插件相关的开关只在 ``initialize`` 里读，必须重载。
        """
        idle = changed.get("idle_timeout_sec")
        if idle is not None and self._pool is not None:
            try:
                self._pool.set_idle_timeout(float(idle))
            except Exception as exc:  # noqa: BLE001 - 刷新失败不该让保存失败
                logger.warning(
                    "%s: 面板保存后同步空闲回收阈值失败：%s", LOG_PREFIX, exc
                )

        if any(key in changed for key in wakeup_mod.CONFIG_KEYS):
            try:
                await self._hook.sync()
            except Exception as exc:  # noqa: BLE001 - 重启钩子失败不该让保存失败
                logger.warning("%s: 面板保存后同步唤醒钩子失败：%s", LOG_PREFIX, exc)
            install_keys = {
                "wakeup_api_key",
                "wakeup_public_url",
                "wakeup_wait_sec",
                "wakeup_auto_approve",
            }
            if self._hook_installed and (install_keys & set(changed)):
                # 已经写进 alkaid0 的钩子命令里是旧地址 / 旧参数；不自动改 alkaid0，
                # 只提醒用户回面板点一下「一键安装」。
                self._hook_sync_note = "唤醒钩子设置已改，记得点一次「一键安装」"
        return any(key in panel_mod.RELOAD_KEYS for key in changed)

    def _panel_hook_status(self) -> dict[str, Any]:
        """面板要显示的钩子现状（随配置树下发）。"""
        settings = self._hook.settings()
        return {
            "installed": self._hook_installed,
            "note": self._hook_sync_note,
            "endpoint": self._hook.endpoint() if settings.enabled else "",
            "usable": settings.usable,
        }

    async def _panel_hook_status_async(self) -> dict[str, Any]:
        """回读 alkaid0，返回真实的安装状态（远程配置页的一键按钮用）。"""
        status = self._panel_hook_status()
        try:
            remote = await self._remote_config.get_cronjob_hook(
                marker=self._hook.marker(),
                env_names=(
                    wakeup_mod.HOOK_ENV_KEY,
                    wakeup_mod.HOOK_PLUGIN_ENV_KEY,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - alkaid0 没起来也能打开面板
            status["error"] = str(exc)
            return status
        installed = bool(remote.get("installed"))
        self._hook_installed = installed
        status["installed"] = installed
        status["endpoint"] = (
            self._hook.endpoint() if self._hook.settings().enabled else ""
        )
        status["env"] = remote.get("env") or {}
        if not self._hook_sync_note:
            status["note"] = (
                "已写入 alkaid0 的 OnCronjobWakeup"
                if installed
                else "还没写入"
            )
        return status

    async def _panel_hook_action(self, action: str) -> dict[str, Any]:
        """面板按钮：写入 / 移除 alkaid0 的 cron 唤醒钩子。"""
        install = action == "install"
        ok = await self._sync_alkaid0_hook(install=install)
        return {
            "ok": ok,
            "installed": self._hook_installed,
            "note": self._hook_sync_note,
            "endpoint": self._hook.endpoint(),
        }

    async def _enable_wakeup(self) -> None:
        """一键安装时顺便把唤醒开关打开并写盘（路由才会真正可用）。"""
        try:
            if self.plugin_config.get("wakeup_enable"):
                return
            self.plugin_config["wakeup_enable"] = True
        except Exception as exc:  # noqa: BLE001 - 配置对象不可写就只记日志
            logger.warning("%s: 自动启用唤醒钩子失败：%s", LOG_PREFIX, exc)
            return
        save = getattr(self.plugin_config, "save_config_async", None)
        if callable(save):
            try:
                await save()
            except Exception as exc:  # noqa: BLE001 - 开关已生效，保存失败只警告
                logger.warning("%s: 保存唤醒开关失败：%s", LOG_PREFIX, exc)

    async def _sync_alkaid0_hook(self, *, install: bool) -> bool:
        """一键写入 / 移除 alkaid0 的 ``Hooks.OnCronjobWakeup``。

        安装时自动在 AstrBot 里申请一个 ``plugin`` scope 的 API Key（名字固定，
        先删同名旧 Key 再建），**明文直接写进钩子命令**的 ``X-API-Key`` 头；
        插件自己的 ``wakeup_api_key``（如果配了）作为第二道口令，同样明文写进
        命令。移除时把 Key 与钩子命令一起清掉；旧版本留在 ``Agent.TerminalEnvs``
        里的变量顺带删除，用户自己的其它钩子原样保留。
        """
        settings = self._hook.settings()
        if install and not settings.enabled:
            await self._enable_wakeup()
            settings = self._hook.settings()
        if install and not settings.usable:
            self._hook_sync_note = "未写入：先把「启用唤醒钩子」打开并保存"
            logger.warning("%s: %s", LOG_PREFIX, self._hook_sync_note)
            return False

        # 旧版本把密钥放在 Agent.TerminalEnvs；现在密钥直接写进命令，
        # 这里只保留「删除旧变量」的补丁（``None`` = 删除该键）。
        env: dict[str, str | None] = {
            wakeup_mod.HOOK_ENV_KEY: None,
            wakeup_mod.HOOK_PLUGIN_ENV_KEY: None,
        }
        command: str | None = None
        if install:
            try:
                raw_key = await self._create_wakeup_api_key()
            except Exception as exc:  # noqa: BLE001 - 数据库 / 权限问题都要如实上报
                self._hook_sync_note = f"申请 AstrBot API Key 失败：{exc}"
                logger.warning("%s: %s", LOG_PREFIX, self._hook_sync_note)
                return False
            if not raw_key:
                self._hook_sync_note = "申请 AstrBot API Key 失败：返回为空"
                logger.warning("%s: %s", LOG_PREFIX, self._hook_sync_note)
                return False
            command = self._hook.command(settings, api_key=raw_key)
        else:
            try:
                removed = await self._drop_wakeup_api_keys()
                if removed:
                    logger.info(
                        "%s: 已撤销 %d 个唤醒用 AstrBot API Key",
                        LOG_PREFIX,
                        removed,
                    )
            except Exception as exc:  # noqa: BLE001 - 撤销失败不挡住移除钩子
                logger.warning(
                    "%s: 撤销唤醒用 AstrBot API Key 失败：%s", LOG_PREFIX, exc
                )

        try:
            result = await self._remote_config.set_cronjob_hook(
                command=command,
                env=env,
                marker=self._hook.marker(),
            )
        except Exception as exc:  # noqa: BLE001 - alkaid0 没起来 / 配置写不进去
            self._hook_sync_note = f"同步失败：{exc}"
            logger.warning("%s: %s", LOG_PREFIX, self._hook_sync_note)
            return False

        warning = str((result or {}).get("warning") or "")
        self._hook_installed = install
        self._hook_sync_note = f"已{'写入' if install else '移除'} alkaid0 的 OnCronjobWakeup" + (
            "（回读失败，去远程配置里点一下重新读取）" if warning else ""
        )
        logger.info("%s: %s", LOG_PREFIX, self._hook_sync_note)
        return True

    async def _send_to_chat(self, unified_msg_origin: str, text: str) -> bool:
        """主动往聊天里发一段文本（cron 回合转发用）。"""
        if not text.strip():
            return False
        try:
            chain = MessageChain().message(text)
            sent = await self.context.send_message(unified_msg_origin, chain)
        except Exception as exc:  # noqa: BLE001 - 平台不支持 / 会话失效都只记日志
            logger.warning(
                "%s: 主动发消息到 %s 失败：%s", LOG_PREFIX, unified_msg_origin, exc
            )
            return False
        return bool(sent)

    def _cwd_for(
        self, key: str, event: AstrMessageEvent, settings: runner_mod.RunnerSettings
    ) -> str:
        """这个群实际用的工作目录：``/aworkdir`` 绑过就用绑定，否则按模板渲染。"""
        store = self._ensure_store()
        return runner_mod.resolve_cwd(
            settings.workspace_template, store.get_workdir(key), event
        )

    @staticmethod
    def _cwd_root(settings: runner_mod.RunnerSettings) -> str:
        """模板里占位符之前的那段 —— 新群第一次开会话时用它开临时会话建目录。"""
        return runner_mod.workspace_root(settings.workspace_template)

    async def _drop_group_connection(self, key: str, *, reason: str) -> None:
        """断开某个群的连接（下一条消息按新的工作目录重建会话）。"""
        pool = self._pool
        if pool is not None:
            await pool.release(key, reason=reason)

    async def _mkdir_remote(self, root: str, rel: str) -> bool:
        """借一条临时连接，在 ``root`` 下把 ``rel`` 建出来（``fs/mkdir``，幂等）。

        ``/aworkdir`` 设置目录时用：这时候该群自己的连接可能还没建（cwd 还不存在）、
        或还指着旧目录，所以另起一条短连接：开临时会话 → 建目录 → 关会话 → 断连。
        """
        if not root or rel == ".":
            return True
        settings = self._settings()
        client = pool_mod.default_client_factory(
            settings.url, f"{PLUGIN_NAME}:mkdir"
        )
        session_id = ""
        try:
            await client.connect()
            await client.initialize()
            result = await client.session_new(root)
            session_id = str((result or {}).get("sessionId") or "")
            if not session_id:
                return False
            await client.fs_mkdir(session_id, rel)
            logger.info("%s: 已建好目录 %s/%s", LOG_PREFIX, root, rel)
            return True
        except Exception as exc:  # noqa: BLE001 - 建目录失败如实回给用户
            logger.warning("%s: 建目录 %s/%s 失败：%s", LOG_PREFIX, root, rel, exc)
            return False
        finally:
            if session_id:
                with contextlib.suppress(Exception):
                    await client.session_close(session_id)
            with contextlib.suppress(Exception):
                await client.close()

    def _permission_handler(self, session: pool_mod.GroupSession) -> Any:
        """给每条连接装审批处理函数（本地回合 / cron / 其它客户端共用）。"""
        return runner_mod.build_permission_handler(
            session,
            label=session.key,
            settings_provider=self._settings,
        )

    def _ensure_pool(self) -> pool_mod.SessionPool:
        """创建连接池（含会话记录表）并启动空闲回收任务（幂等）。"""
        if self._pool is None:
            settings = self._settings()
            store = self._ensure_store()
            self._pool = pool_mod.SessionPool(
                idle_timeout=settings.idle_timeout_sec,
                store=store,
            )
            # 每条连接建好就装审批处理函数：外部回合没有 _run_gated，也要能直通。
            self._pool.set_permission_handler_factory(self._permission_handler)
            # 常驻监听：cron / 其它客户端发起的回合没有本地 prompt，也要把内容推回群聊
            self._proactive.bind(self._pool)
            self._pool.start_reaper()
            logger.info(
                "%s: ACP 连接池已启动（群空闲 %g 秒后回收连接；会话记录 %s，现有 %d 条）",
                LOG_PREFIX,
                settings.idle_timeout_sec,
                store.path,
                store.count(),
            )
        return self._pool

    async def _shutdown_pool(self) -> None:
        """停止回收任务并断开全部连接。"""
        pool, self._pool = self._pool, None
        if pool is None:
            return
        await self._proactive.stop()
        await pool.stop_reaper()
        closed = await pool.close_all(reason="插件停用")
        logger.info("%s: 已断开 %d 条 ACP 连接", LOG_PREFIX, closed)

    def _register_runner(self) -> bool:
        """把 ``alkaid0`` 执行器注册进 librunner。

        librunner 不在场（例如关掉了「强制前置插件」又没装）时只记警告，不抛异常 ——
        插件其余部分仍然可用。

        Returns:
            是否注册成功。
        """
        if self._runner_registered:
            return True
        try:
            from data.plugins.astrbot_plugin_librunner.api import register_runner
        except Exception as exc:  # noqa: BLE001 - 前置缺席时不该拖垮插件
            logger.warning("%s: 无法导入 librunner，跳过执行器注册（%s）", LOG_PREFIX, exc)
            return False

        try:
            register_runner(
                runner_mod.RUNNER_TYPE,
                factory=lambda: runner_mod.Alkaid0Runner(
                    self._ensure_pool(), self.plugin_config, self._ensure_queue()
                ),
                config_default=dict(runner_mod.RUNNER_CONFIG_DEFAULT),
                config_metadata=dict(runner_mod.RUNNER_CONFIG_METADATA),
                label=runner_mod.RUNNER_LABEL,
                description=runner_mod.RUNNER_DESCRIPTION,
                owner=PLUGIN_NAME,
            )
        except Exception as exc:  # noqa: BLE001 - 注册失败只上报，不影响插件启用
            logger.error(
                "%s: 注册执行器 %s 失败：%s",
                LOG_PREFIX,
                runner_mod.RUNNER_TYPE,
                exc,
                exc_info=True,
            )
            return False

        self._runner_registered = True
        logger.info(
            "%s: 已注册执行器 %s（WebUI「AI → Agent 执行方式」可选）",
            LOG_PREFIX,
            runner_mod.RUNNER_TYPE,
        )
        return True

    def _unregister_runner(self) -> None:
        """撤销执行器注册（owner 必须一致，否则 librunner 会拒绝）。"""
        if not self._runner_registered:
            return
        self._runner_registered = False
        try:
            from data.plugins.astrbot_plugin_librunner.api import unregister_runner

            unregister_runner(runner_mod.RUNNER_TYPE, owner=PLUGIN_NAME)
            logger.info("%s: 已注销执行器 %s", LOG_PREFIX, runner_mod.RUNNER_TYPE)
        except Exception as exc:  # noqa: BLE001 - 注销失败不影响卸载流程
            logger.warning("%s: 注销执行器失败：%s", LOG_PREFIX, exc)

    # ------------------------------------------------------------ 状态命令
    @filter.command("0528v2")
    async def status(self, event: AstrMessageEvent):
        """查看本插件与两个前置插件的状态。"""
        yield event.plain_result(self._render_status())

    def _render_status(self) -> str:
        """渲染状态文本（也用于自测断言）。"""
        enforce = bool(self.plugin_config.get("enforce_prerequisite", True))
        prereqs = prerequisite_mod.build_prerequisites(self.plugin_config)
        settings = self._settings()
        lines = [
            f"{PLUGIN_NAME} {PLUGIN_VERSION}",
            f"强制前置插件: {'开启' if enforce else '关闭'}",
            f"前置插件（{len(prereqs)}）:",
        ]
        for prereq in prereqs:
            installed = prerequisite_mod.is_installed(prereq)
            loaded = prerequisite_mod.loaded_plugin(self.context, prereq.name) is not None
            lines.append(
                f"- {prereq.name}: {'已安装' if installed else '未安装'}；"
                f"{'内核已加载' if loaded else '内核未加载'}"
                f"（来源 {mask_url(prereq.repo)}）"
            )
        if self._prereq_report is not None:
            lines.append("最近一次启动检查:")
            lines.append(self._prereq_report.describe())
        lines.append(
            f"本插件就地拉起的前置实例: {len(self._activated)} 个（terminate 时释放）"
        )

        lines.append(
            f"执行器 {runner_mod.RUNNER_TYPE}: "
            f"{'已注册' if self._runner_registered else '未注册'}"
        )
        auto = (
            f"工具审批直通: {'开启' if settings.auto_approve_enable else '关闭'}"
            f"；配置白名单 {settings.auto_approve_tools or '（空）'}"
            f"；提示词 {'允许' if settings.auto_approve_prompt else '禁止'}"
            f"；外部钩子 {'允许' if settings.auto_approve_hook else '禁止'}"
        )
        lines.append(auto)
        lines.append(f"唤醒钩子（复用 AstrBot 端口）: {self._hook.status_line()}")
        if self._hook.settings().enabled:
            lines.append(f"  端点: {self._hook.endpoint()}")
            lines.append(
                "  alkaid0 cron 钩子: "
                + (
                    self._hook_sync_note
                    or (
                        "已写入（远程配置页可一键更新 / 移除）"
                        if self._hook_installed
                        else "未写入（远程配置页点「一键安装唤醒钩子」）"
                    )
                )
            )
        lines.append(f"ACP 地址（面板值优先）: {mask_url(settings.url)}")
        lines.append(f"工作目录模板（面板值优先）: {settings.workspace_template}")
        queue = self._turn_queue
        if self._pool is None:
            lines.append("连接池: 未启动")
        else:
            snapshot = self._pool.snapshot()
            lines.append(
                f"连接池: {len(snapshot)} 条连接"
                f"（空闲 {self._pool.idle_timeout:g} 秒回收）"
            )
            for item in snapshot:
                busy = queue is not None and queue.busy(item["key"])
                waiting = queue.waiting(item["key"]) if queue is not None else 0
                lines.append(
                    f"- {item['key']}: {'已连接' if item['connected'] else '已断开'}"
                    f"；会话 {item['sessionId'] or '未建立'}"
                    f"；模型 {item['model'] or '未知'}"
                    f"；{'跑着一轮' if busy else '没在跑'}"
                    f"{f'、插话排队 {waiting} 条' if waiting else ''}"
                    f"；空闲 {item['idleSec']:g}s；工作目录 {item['cwd']}"
                )
            store = self._pool.store
            if store is not None:
                lines.append(f"会话记录: {store.count()} 条（{store.path}）")
        if queue is not None:
            pending = queue.snapshot()
            detail = "、".join(
                f"{key} {count} 条" for key, count in pending.items()
            )
            lines.append(
                f"群内闸门: 插话排队 {sum(pending.values())} 条"
                + (f"（{detail}）" if detail else "")
                + (
                    f"；启动时从库里恢复 {self._restored_pending} 条"
                    if self._restored_pending
                    else ""
                )
            )
        return "\n".join(lines)

    # ------------------------------------------------------------ 上下文用量（/actx）
    @filter.command("actx")
    async def context_usage(self, event: AstrMessageEvent):
        """查看本群会话的上下文用量：``context x% (used/size)``。

        数字来自服务端推的 ``usage_update``（每次模型响应后一份，插件缓存最新的一份）：
        ``used`` 是最近一次模型响应的 token 总量（含输出），``size`` 是当前模型的上下文窗口。
        开会话与续接都不推用量，所以刚重连、或 ``/areset`` 之后要先跑一轮才有数字。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        session = pool.peek(key)
        if session is None or not session.connected or not session.session_id:
            yield event.plain_result(
                "本群还没有活跃的 Alkaid0 会话：先发一条消息把它建起来，再看用量。"
            )
            return

        if session.usage is None:
            yield event.plain_result(
                "本群这个会话还没跑过一轮，没有用量可报：Alkaid0 在每次模型响应后才推 "
                "usage_update（开会话 / 续接都不推，/areset 之后也会清空）。"
            )
            return

        text = render_context_usage(session.usage)
        logger.info("%s: /actx 群 %s 上报用量 %s", LOG_PREFIX, key, text)
        yield event.plain_result(text)

    # ------------------------------------------------------------ 会话重置
    @filter.command("areset")
    async def reset_session(self, event: AstrMessageEvent):
        """重置本群的 Alkaid0 会话：抹掉记录的会话 ID 并断开连接。

        下一条消息会重新 ``session/new`` 开一个干净会话（alkaid0 侧的聊天记录
        不会被删，只是不再续接）。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        store = pool.store
        remembered = store.get(key) if store is not None else None
        had_connection = pool.peek(key) is not None
        await pool.reset_session(key, reason="用户执行 /areset")

        lines = []
        lines.append(
            f"已清除本群记录的会话：{remembered}" if remembered else "本群没有记录过会话"
        )
        lines.append(
            "已断开该群的连接" if had_connection else "该群当前没有活跃连接"
        )
        lines.append("下一条消息会新建一个 Alkaid0 会话。")
        logger.info(
            "%s: /areset 重置群 %s（原会话 %s，连接 %s）",
            LOG_PREFIX,
            key,
            remembered or "无",
            "已断开" if had_connection else "无",
        )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 强制回收（/aclean）
    @filter.command("aclean")
    async def clean_session(self, event: AstrMessageEvent):
        """强制回收本群的 ACP 连接与会话（保留记录，下一条消息续接）。

        与 ``/areset`` 不同：**不抹会话记录**，下次消息会 ``session/resume`` 回同一个
        会话；与空闲回收不同的是它不看空闲计时、也不等在跑的那一轮结束 —— 直接
        ``session/close`` 并断开连接，正在跑的一轮会因此报错收尾。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        session = pool.peek(key)
        if session is None:
            yield event.plain_result("本群当前没有活跃的 ACP 连接，不需要回收。")
            return

        session_id = session.session_id or "未建立"
        busy = session.busy
        # 先停掉主动推送的收集任务，避免回收后还把半截内容发到群里
        self._proactive.cancel(key)
        released = await pool.release(key, reason="用户执行 /aclean")
        if not released:
            yield event.plain_result("本群连接刚刚已经不在了，没有需要回收的。")
            return

        logger.info(
            "%s: /aclean 强制回收群 %s（会话 %s，%s）",
            LOG_PREFIX,
            key,
            session_id,
            "当时正在跑一轮" if busy else "当时空闲",
        )
        lines = [
            f"已强制回收本群的 ACP 连接（会话 {session_id}）。",
            "会话记录保留，下一条消息会 session/resume 续接。",
        ]
        if busy:
            lines.append("刚才正在跑的那一轮已被断开。")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 中止当前轮（/astop）
    @filter.command("astop")
    async def stop_turn(self, event: AstrMessageEvent):
        """中止本群正在跑的那一轮（给 alkaid0 发 ACP ``session/cancel`` 通知）。

        与 ``/areset`` 不同：这里**不动会话、不断连接**，只让 alkaid0 把当前这一轮
        停下来 —— 正在生成的回复、正在跑的工具 / 终端都会尽快中止，服务端以
        ``stopReason: "cancelled"`` 收尾（这一轮已经说出来的内容仍会发到群里，
        末尾带一行「🛑 本轮已终止」）。

        群里排队的插话不受影响：那是别人还没被处理过的消息，这一轮停下后照常接着跑。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        queue = self._turn_queue
        waiting = queue.waiting(key) if queue is not None else 0
        queued_note = (
            f"\n本群还排着 {waiting} 条插话，它们不在这条命令的范围内"
            "（这一轮停下后照常接着跑）。"
            if waiting
            else ""
        )

        session = pool.peek(key)
        if session is None or not session.connected or not session.session_id:
            yield event.plain_result(
                "本群没有正在跑的 Alkaid0 轮次（也没有活跃连接）。" + queued_note
            )
            return
        if not session.busy:
            yield event.plain_result(
                "本群现在没有在跑的 Alkaid0 轮次。" + queued_note
            )
            return

        try:
            await session.client.session_cancel(session.session_id)
        except Exception as exc:  # noqa: BLE001 - 连接刚断 / 发不出去都如实说
            logger.warning(
                "%s: /astop 请求群 %s 中止当前轮失败：%s", LOG_PREFIX, key, exc
            )
            yield event.plain_result(f"发送中止请求失败：{exc}")
            return

        logger.info(
            "%s: /astop 已请求群 %s 中止当前轮（会话 %s，排队插话 %d 条）",
            LOG_PREFIX,
            key,
            session.session_id,
            waiting,
        )
        yield event.plain_result(
            "已请求 Alkaid0 中止本群当前这一轮：正在生成的回复会停下，"
            "正在跑的工具 / 终端也会被一起中止。\n"
            "这一轮已经说出来的内容还会发到群里（末尾会标一行「🛑 本轮已终止」）。"
            + queued_note
        )

    # ------------------------------------------------------------ 工具审批（/aapprove）
    @filter.command("aapprove")
    async def approve_tool(self, event: AstrMessageEvent):
        """放行本群当前挂着的工具审批（``/aapprove``）。

        审批开关与策略都在 alkaid0 自己的配置里：插件收到
        ``session/request_permission`` 时**不替用户点头**，只把请求挂起、往聊天里
        发一行提示；用户发这条命令才回 ``allow_once``。不发批准、直接继续发消息就
        等于拒绝（alkaid0 侧自己判定），所以这里不提供「拒绝」命令。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        session = pool.peek(key)
        if session is None or not session.connected or not session.session_id:
            yield event.plain_result(
                "本群还没有活跃的 Alkaid0 会话，没有等待审批的工具调用。"
            )
            return

        pending = session.client.pending_permission()
        if pending is None:
            yield event.plain_result(
                "本群现在没有等待审批的工具调用。"
                "（审批提示会带着工具名发在聊天里，看到之后再发 /aapprove）"
            )
            return

        request_id, params = pending
        option_id = approval_mod.permission_option(params)
        if not option_id:
            yield event.plain_result(
                "这条审批请求里没有可选的放行选项，没法批准（服务端没提供选项）。"
            )
            return

        try:
            replied = await session.client.reply_request(
                request_id,
                {
                    "outcome": acp_protocol.PERMISSION_OUTCOME_SELECTED,
                    "optionId": option_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 连接刚断 / 回包发不出去都如实说
            logger.warning("%s: /aapprove 回包失败（群 %s）：%s", LOG_PREFIX, key, exc)
            yield event.plain_result(f"发送批准失败：{exc}")
            return
        if not replied:
            yield event.plain_result(
                "这条审批请求已经失效（可能超时，或者已经被新一轮消息当作拒绝）。"
            )
            return

        title = str(params.get("title") or "").strip()
        logger.info(
            "%s: /aapprove 已允许群 %s 的工具审批「%s」（request=%s）",
            LOG_PREFIX,
            key,
            title or "工具调用",
            request_id,
        )
        yield event.plain_result(
            f"✅ 已允许：{title or '工具调用'}\nAlkaid0 会继续执行这个工具。"
        )

    # ------------------------------------------------------------ 会话配置项（/amodel /aeffort）
    @filter.command("amodel")
    async def model(self, event: AstrMessageEvent, arg: str = ""):
        """查看 / 切换本群 Alkaid0 会话使用的模型（``configId: "model"``）。"""
        async for chunk in self._config_option_command(
            event,
            arg,
            config_id=acp_protocol.CONFIG_ID_MODEL,
            label="模型",
            title="本群会话可用的模型",
            help_text=MODEL_COMMAND_HELP,
        ):
            yield chunk

    @filter.command("aeffort")
    async def effort(self, event: AstrMessageEvent, arg: str = ""):
        """查看 / 切换本群 Alkaid0 会话的推理强度（``thought_level``）。

        取值 ``unset`` / ``low`` / ``medium`` / ``high`` / ``max`` / ``xhigh``。
        """
        async for chunk in self._config_option_command(
            event,
            arg,
            config_id=acp_protocol.CONFIG_ID_THOUGHT_LEVEL,
            label="推理强度",
            title="本群会话可用的推理强度",
            help_text=EFFORT_COMMAND_HELP,
        ):
            yield chunk

    async def _config_option_command(
        self,
        event: AstrMessageEvent,
        arg: str,
        *,
        config_id: str,
        label: str,
        title: str,
        help_text: str,
    ):
        """列 / 切一个会话级配置项（``/amodel`` 与 ``/aeffort`` 共用）。

        只改**当前会话**（服务端按会话存，别的群不受影响）：本质是一次
        ``session/set_config_option``，因此不会像 ``/areset`` 那样断连接、
        也不打断正在跑的那一轮。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        settings = self._settings()
        cwd = self._cwd_for(key, event, settings)
        try:
            # 本群还没会话就按「这个群的第一条消息」建起来：同一个会话，后续消息直接复用
            session = await pool.acquire(
                key,
                url=settings.url,
                cwd=cwd,
                cwd_root=self._cwd_root(settings),
            )
        except Exception as exc:  # noqa: BLE001 - 连不上 / 开不了会话就如实说
            logger.error(
                "%s: 打开群 %s 的会话失败（%s）：%s", LOG_PREFIX, key, config_id, exc
            )
            yield event.plain_result(
                f"连不上 Alkaid0 或开不了会话：{exc}\n先看看 /0528v2。"
            )
            return

        option = session.config_option(config_id)
        choices = config_choices(option)
        current = acp_protocol.option_current(option)
        if not choices:
            yield event.plain_result(
                f"Alkaid0 没有给出可选{label}（configOptions 里没有 {config_id}），"
                "检查一下服务端的配置。"
            )
            return

        token = (arg or "").strip()
        if not token:
            yield event.plain_result(
                render_choice_list(choices, current, title=title, help_text=help_text)
            )
            return

        choice, candidates = find_choice(choices, token)
        if choice is None:
            yield event.plain_result(
                f"没找到叫「{token}」的{label}。\n\n"
                + render_choice_list(choices, current, title=title, help_text=help_text)
            )
            return
        if len(candidates) > 1:
            names = "、".join(f"{item['key']}.{item['name']}" for item in candidates)
            yield event.plain_result(
                f"「{token}」对上了多个{label}（{names}），请用编号或完整值指定。"
            )
            return

        try:
            await session.set_config_option(config_id, choice["value"])
        except Exception as exc:  # noqa: BLE001 - 服务端拒绝（值不存在 / 被隐藏等）
            logger.warning(
                "%s: 把群 %s 的 %s 切到 %s 失败：%s",
                LOG_PREFIX,
                key,
                config_id,
                choice["value"],
                exc,
            )
            yield event.plain_result(f"切换失败：{exc}")
            return

        now = (
            acp_protocol.option_current(session.config_option(config_id))
            or choice["value"]
        )
        logger.info(
            "%s: 把群 %s 的 %s 切成 %s（%s）",
            LOG_PREFIX,
            key,
            config_id,
            choice["name"],
            now,
        )
        yield event.plain_result(
            f"已把本群会话的{label}切成 {choice['name']}（{now}）\n"
            "只影响这个群/会话，其它群照旧；下一条消息就用新值。"
        )

    # ------------------------------------------------------------ 工作目录（/aworkdir）
    @filter.command("aworkdir")
    async def workdir(self, event: AstrMessageEvent, arg: GreedyStr):
        """查看 / 设置本群的 Alkaid0 工作目录（相对工作目录模板里的根目录）。

        ``/aworkdir`` 查看当前绑定与绝对路径；``/aworkdir .`` 绑到根目录本身；
        ``/aworkdir <相对路径>`` 绑到根目录下的子目录（会先在 alkaid0 侧建出来）；
        ``/aworkdir reset`` 恢复为模板默认目录。
        """
        key = runner_mod.object_key(event)
        settings = self._settings()
        store = self._ensure_store()
        template = settings.workspace_template
        root = runner_mod.workspace_root(template)
        current_rel = store.get_workdir(key)
        current = runner_mod.resolve_cwd(template, current_rel, event)
        token = (arg or "").strip()

        if not token:
            lines = ["📂 本群的 Alkaid0 工作目录:"]
            lines.append(
                f"  模板: {template}"
                + (f"（根目录 {root}）" if root else "（以占位符开头，推不出根目录）")
            )
            if current_rel:
                lines.append(
                    "  绑定: " + ("根目录本身" if current_rel == "." else current_rel)
                )
            else:
                lines.append("  绑定: 未设置（按模板渲染）")
            lines.append(f"  绝对路径: {current}")
            pool = self._pool
            live = pool.peek(key) if pool is not None else None
            if live is not None and live.cwd != current:
                lines.append(f"  ⚠️ 当前连接用的还是 {live.cwd}，下一条消息会切过来")
            lines.append(
                "💡 用法: /aworkdir <相对路径>（自动创建）| /aworkdir .（绑到根目录）"
                " | /aworkdir reset（恢复模板默认）"
            )
            yield event.plain_result("\n".join(lines))
            return

        if token == "reset":
            if not current_rel:
                yield event.plain_result("ℹ️ 本群没有绑定工作目录，无需重置。")
                return
            store.forget_workdir(key)
            await self._drop_group_connection(key, reason="工作目录绑定已重置")
            default = runner_mod.render_workspace(template, event)
            yield event.plain_result(
                f"✅ 已恢复为模板默认目录：{default}\n"
                "本群旧连接已断开，下一条消息用新目录重建会话。"
            )
            return

        rel = runner_mod.validate_rel_path(token)
        if rel is None:
            yield event.plain_result(
                f"❌ 非法相对路径：{token}\n"
                "要求：不以 / 开头、不带盘符、不含 .. 路径段。"
            )
            return
        if not root:
            yield event.plain_result(
                f"❌ 模板 {template} 以占位符开头，推不出根目录，不能绑定工作目录。"
            )
            return
        if rel == current_rel:
            yield event.plain_result(f"ℹ️ 工作目录没变（当前：{current}）")
            return

        pool = self._pool
        live = pool.peek(key) if pool is not None else None
        if live is not None and live.busy:
            yield event.plain_result(
                "⏳ 本群正在跑一轮，先 /astop 或等它跑完再切工作目录。"
            )
            return

        target = root if rel == "." else f"{root}/{rel}"
        if not await self._mkdir_remote(root, rel):
            yield event.plain_result(
                f"❌ 创建目录失败：{target}\n"
                "检查模板根目录是否存在、Alkaid0 是否在线"
                "（服务端需要支持 fs/mkdir 扩展）。"
            )
            return
        store.remember_workdir(key, rel)
        await self._drop_group_connection(key, reason="工作目录已变更")
        yield event.plain_result(
            f"✅ 已把本群的工作目录设为：{target}\n"
            f"（相对 {root}：{'根目录本身' if rel == '.' else rel}）\n"
            "旧连接已断开，下一条消息用新目录重建会话。"
        )

    # ------------------------------------------------------------ 会话查看 / 切换（/aresume）
    @filter.command("aresume")
    async def resume(self, event: AstrMessageEvent, arg: str = ""):
        """列出本群工作目录下的会话，或把本群的会话切到过去的某一个。

        不带参数列出（最近活动在前）；带会话号 / sessionId 就换过去 —— 只改
        「本群续接哪个会话」，不复述聊天记录，alkaid0 侧的历史也不会动。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        settings = self._settings()
        cwd = self._cwd_for(key, event, settings)
        try:
            # 没有连接就按「这个群的第一条消息」建起来（列会话也得先有个连接）
            session = await pool.acquire(
                key, url=settings.url, cwd=cwd, cwd_root=self._cwd_root(settings)
            )
        except Exception as exc:  # noqa: BLE001 - 连不上 / 开不了会话就如实说
            logger.error("%s: /aresume 打开群 %s 的会话失败：%s", LOG_PREFIX, key, exc)
            yield event.plain_result(
                f"连不上 Alkaid0 或开不了会话：{exc}\n先看看 /0528v2。"
            )
            return

        token = (arg or "").strip()
        if not token:
            try:
                result = await session.client.session_list(cwd)
            except Exception as exc:  # noqa: BLE001 - 目录没初始化 / 服务端报错
                logger.warning(
                    "%s: /aresume 列群 %s 的会话失败：%s", LOG_PREFIX, key, exc
                )
                yield event.plain_result(f"列会话失败：{exc}")
                return
            raw = result if isinstance(result, dict) else {}
            found = [
                item for item in (raw.get("sessions") or []) if isinstance(item, dict)
            ]
            yield event.plain_result(
                render_session_list(
                    found,
                    session.session_id or "",
                    cwd=cwd,
                    more=bool(raw.get("nextCursor")),
                )
            )
            return

        target = resolve_session_arg(token, cwd)
        if target == session.session_id:
            yield event.plain_result(f"本群当前就是这个会话（{target}），不用切。")
            return
        try:
            # 关掉当前会话再续接目标会话；成功才写回记录表
            await session.switch_session(target)
        except Exception as exc:  # noqa: BLE001 - 没有这个会话 / cwd 对不上
            logger.warning(
                "%s: /aresume 把群 %s 的会话切到 %s 失败：%s",
                LOG_PREFIX,
                key,
                target,
                exc,
            )
            yield event.plain_result(f"切换失败：{exc}")
            return

        logger.info("%s: /aresume 把群 %s 的会话切到 %s", LOG_PREFIX, key, target)
        yield event.plain_result(
            f"已把本群的会话切到 {session_short_id(target)}（{target}）\n"
            "只影响这个群；下一条消息就接着这个会话聊（历史记录不动，也不会复述）。"
        )

    # ------------------------------------------------------------ 远端命令（/acmd）
    @filter.command("acmd")
    async def remote_command(self, event: AstrMessageEvent, arg: GreedyStr):
        """把一条原始命令发给远端 Alkaid0 执行（``/acmd``）。

        不带参数列出远端命令（服务端声明 alkaid0 扩展能力时用与 alcoh 同步的对照表
        翻成中文）；带参数就把命令原样发过去 —— 开头的 ``/`` 不用写，会自动补上，
        参数里的空格原样保留（``/acmd s 干活`` → ``/s 干活``）。
        """
        key = runner_mod.object_key(event)
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        settings = self._settings()
        cwd = self._cwd_for(key, event, settings)
        try:
            # 本群还没会话就按「这个群的第一条消息」建起来（命令也得先有个会话可发）
            session = await pool.acquire(
                key,
                url=settings.url,
                cwd=cwd,
                cwd_root=self._cwd_root(settings),
            )
        except Exception as exc:  # noqa: BLE001 - 连不上 / 开不了会话就如实说
            logger.error("%s: /acmd 打开群 %s 的会话失败：%s", LOG_PREFIX, key, exc)
            yield event.plain_result(
                f"连不上 Alkaid0 或开不了会话：{exc}\n先看看 /0528v2。"
            )
            return

        text = acp_commands.slash_command_text(arg)
        if not text:
            yield event.plain_result(
                render_command_list(
                    session.available_commands, translated=session.supports_alkaid0
                )
            )
            return

        try:
            reply = await runner_mod.run_slash_command(
                session,
                text,
                timeout=settings.turn_timeout_sec,
                echo_events=settings.echo_events,
            )
        except Exception as exc:  # noqa: BLE001 - 服务端不认这条命令 / 超时 / 断线
            logger.warning(
                "%s: /acmd 在群 %s 执行 %s 失败：%s", LOG_PREFIX, key, text, exc
            )
            yield event.plain_result(f"执行失败：{exc}")
            return

        logger.info("%s: /acmd 在群 %s 执行了 %s", LOG_PREFIX, key, text)
        yield event.plain_result(reply or f"{text} 执行完了，没有输出。")

    # ------------------------------------------------------------ 定时任务（/acron）
    @filter.command("acron")
    async def cronjobs(self, event: AstrMessageEvent, arg: GreedyStr):
        """列出本群会话的 Alkaid0 定时任务，或删除其中一条。

        ``/acron`` 列出本群会话的任务（id / cron / 周期 / 下次触发 / 提示词摘要）；
        ``/acron remove <id>`` 删除其中一条。``/acron all`` 已移除；创建 / 修改
        任务仍由 alkaid0 的 ``cron`` 工具完成；命令权限交给 AstrBot 上层。
        """
        pool = self._pool
        if pool is None:
            yield event.plain_result(
                "连接池还没启动，检查一下插件是否加载成功（/0528v2）。"
            )
            return

        tokens = (arg or "").split()
        action = tokens[0].lower() if tokens else ""
        if not action:
            job_list = await self._cronjobs_for_event(event, pool)
            if isinstance(job_list, str):
                yield event.plain_result(job_list)
                return
            yield event.plain_result(render_cron_jobs(job_list))
            return
        if action != "remove":
            yield event.plain_result(
                f"不认识的参数：{action}\n{CRON_COMMAND_HELP}"
            )
            return

        async for chunk in self._remove_cronjob(event, pool, tokens):
            yield chunk

    async def _cronjobs_for_event(self, event: AstrMessageEvent, pool):
        """读取本群会话的任务（列表 / 删除前确认）；出错时返回错误文本。"""
        key = runner_mod.object_key(event)
        settings = self._settings()
        cwd = self._cwd_for(key, event, settings)
        try:
            session = await pool.acquire(
                key,
                url=settings.url,
                cwd=cwd,
                cwd_root=self._cwd_root(settings),
            )
        except Exception as exc:  # noqa: BLE001 - 连不上 / 开不了会话
            logger.error("%s: /acron 打开群 %s 的会话失败：%s", LOG_PREFIX, key, exc)
            return f"连不上 Alkaid0 或开不了会话：{exc}\n先看看 /0528v2。"
        if not session.supports_cron:
            return (
                "Alkaid0 没有声明 cron 能力（alk.cxykevin.top/cron），"
                "这个版本没法查询 / 删除定时任务。"
            )
        try:
            return await session.cron_jobs()
        except Exception as exc:  # noqa: BLE001 - 服务端报错 / 掉线
            logger.warning("%s: /acron 读取群 %s 的任务失败：%s", LOG_PREFIX, key, exc)
            return f"读取定时任务失败：{exc}"

    async def _remove_cronjob(self, event: AstrMessageEvent, pool, tokens: list[str]):
        """``/acron remove <id>``：删掉本群会话里的一条任务。"""
        if len(tokens) != 2:
            yield event.plain_result(
                "用法：/acron remove <id>（id 从 Alkaid0 的定时任务列表里拿）"
            )
            return
        job_id = parse_cron_job_id(tokens[1])
        if job_id is None:
            yield event.plain_result("任务 id 必须是正整数。")
            return

        job_list = await self._cronjobs_for_event(event, pool)
        if isinstance(job_list, str):
            yield event.plain_result(job_list)
            return
        key = runner_mod.object_key(event)
        session = pool.peek(key)
        if session is None:
            yield event.plain_result("本群还没有活跃的 Alkaid0 会话。")
            return
        if not any(job.get("id") == job_id for job in job_list):
            yield event.plain_result(
                f"本群会话里没有 id 为 {job_id} 的定时任务。\n{CRON_COMMAND_HELP}"
            )
            return

        try:
            await session.delete_cron_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 任务没了 / 掉线都如实说
            logger.warning(
                "%s: /acron 删除群 %s 的任务 %s 失败：%s",
                LOG_PREFIX,
                key,
                job_id,
                exc,
            )
            yield event.plain_result(f"删除失败：{exc}")
            return

        logger.info("%s: /acron 已删除群 %s 的定时任务 %s", LOG_PREFIX, key, job_id)
        yield event.plain_result(f"已删除本群会话的定时任务 {job_id}。")
