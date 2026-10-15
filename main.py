"""astrbot_plugin_0528_v2 —— 开发中的插件骨架。

当前职责：

1. **前置插件管理**：启动时确保 ``astrbot_plugin_libi18nject``（WebUI 文案注入）与
   ``astrbot_plugin_librunner``（Agent Runner 注入接口）就绪 —— 缺失就自动下载并
   就地启用，装不上则停止工作并把原因交给 WebUI（见 :mod:`prerequisite`）；
2. **状态命令**：``/0528v2`` 查看两个前置的安装/加载情况与最近一次启动检查结果；
3. **业务留白**：真正的功能写在 :meth:`Plugin0528V2.initialize` 的 ``TODO`` 处：

   * 要注册自定义 Agent 执行器 → 用 librunner 的 ``api.register_runner``；
   * 要给 WebUI 补文案 → 用 libi18nject 的 ``api.register_i18n_entries`` /
     ``register_plain_text_sections``。

   两个插件的完整 API 说明、参数语义、示例与坑都在插件目录下的 ``PLUGIN_APIS.md``
   里（本地开发参考文档，已在 ``.gitignore`` 中排除，**不要提交进仓库**）。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import prerequisite as prerequisite_mod

#: 插件名（与 metadata.yaml / 目录名一致）。
PLUGIN_NAME = "astrbot_plugin_0528_v2"

#: 插件版本（与 metadata.yaml 的 version 保持一致）。
PLUGIN_VERSION = "v0.1.0"

#: 日志前缀。
LOG_PREFIX = prerequisite_mod.LOG_PREFIX


class Plugin0528V2(Star):
    """0528 v2 插件主体（开发中）。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.plugin_config: dict[str, Any] = config or {}
        #: 最近一次启动时的前置检查结果（供状态命令展示）。
        self._prereq_report: prerequisite_mod.MultiEnsureResult | None = None
        #: 本插件就地拉起的前置实例，terminate 时释放。
        self._activated: list[Any] = []

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件启用：先确保两个前置插件就绪，再做本插件自己的初始化。"""
        await self._ensure_prerequisites()

        # TODO(业务): 在这里写你的初始化逻辑，例如注册一个自定义执行器：
        #
        #     from data.plugins.astrbot_plugin_librunner.api import register_runner
        #
        #     register_runner(
        #         "mytype",
        #         factory=lambda: MyRunner(),
        #         config_default={"api_base": "http://127.0.0.1:8080"},
        #         label="MyType",
        #         owner=PLUGIN_NAME,
        #     )
        #
        # 注意：librunner 的补丁与「就地启用」在上一步已经完成，这里可以直接注册；
        # 详细参数语义、字段白名单、文案登记等见 PLUGIN_APIS.md。
        logger.info("%s: 插件已启用（%s）", LOG_PREFIX, PLUGIN_VERSION)

    async def terminate(self) -> None:
        """插件停用：释放本插件就地拉起的前置实例。

        逆序释放：先卸下依赖方（librunner 撤内核补丁、注销自带执行器），再卸下被依赖
        方（libi18nject 撤响应层改写），避免留下半截补丁。由内核自己加载的前置不归
        本插件管（``instance`` 为 None，不会出现在 ``_activated`` 里）。
        """
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

    # ------------------------------------------------------------ 状态命令
    @filter.command("0528v2")
    async def status(self, event: AstrMessageEvent):
        """查看本插件与两个前置插件的状态。"""
        yield event.plain_result(self._render_status())

    def _render_status(self) -> str:
        """渲染状态文本（也用于自测断言）。"""
        enforce = bool(self.plugin_config.get("enforce_prerequisite", True))
        prereqs = prerequisite_mod.build_prerequisites(self.plugin_config)
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
                f"（来源 {prereq.repo}）"
            )
        if self._prereq_report is not None:
            lines.append("最近一次启动检查:")
            lines.append(self._prereq_report.describe())
        lines.append(
            f"本插件就地拉起的前置实例: {len(self._activated)} 个（terminate 时释放）"
        )
        return "\n".join(lines)
