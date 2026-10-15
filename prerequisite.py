"""前置插件（prerequisite）检查、自动安装与就地激活（多前置版）。

``astrbot_plugin_0528_v2`` 的启动依赖两个前置插件：

1. ``astrbot_plugin_libi18nject`` —— WebUI 文案注入（请求层改写 dashboard 语言包）；
2. ``astrbot_plugin_librunner`` —— Agent Runner 注入接口（本插件靠它注册自定义执行器）。

AstrBot 内核没有插件级依赖声明（``metadata.yaml`` 只支持 pip ``requirements.txt``），
因此这里自己实现一套最小可用的前置管理，思路与 librunner 自带的同名模块一致：

1. :func:`is_installed` —— 前置是否已就位（``data/plugins/<name>/metadata.yaml``）；
2. :func:`install` —— 缺失时自动拉取：先用 ``gh repo clone``（复用用户的 gh 凭据），
   失败再回退 ``git clone``，两者都带超时与错误摘要；
3. :func:`activate` —— 装好后**就地**把前置跑起来：实例化它的 ``Star`` 子类并调用
   ``initialize()``（复用同一个 ``Context``），因此不需要重启 AstrBot 主进程；
4. :func:`ensure_all` —— 上面三步的编排：按 :data:`DEFAULT_PREREQUISITES` 的顺序逐个
   处理（被依赖者在前），全部就绪才算 ``ready``。

与 librunner 版本的差异（刻意的）：

* 支持**多个前置**，并按列表顺序安装与激活；
* **总是**调用 :func:`activate`：冷启动时两个前置都不在内核的加载清单里，「文件在」
  并不等于「能用」，必须就地拉起来（librunner 版只在「刚装上」时才激活）；
* 自动安装记录落在本插件自己的 ``data/plugin_data/astrbot_plugin_0528_v2/prerequisite.json``。

失败时由 :class:`PrerequisiteError` 上报：``main.initialize()`` 抛出它，AstrBot 会把
原因记进「加载失败插件」并在 WebUI 插件页展示（即「向前端报告」的通道），同时本插件
处于未启用状态（停止工作）。自动安装每个进程只尝试一次（并落盘记录），避免
「装了但加载不起来 → 反复克隆」。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

#: 前置插件定义：``(插件名, 默认来源)``，顺序即安装与激活顺序。
#: libi18nject 是 librunner 的前置，所以必须排在 librunner 前面。
DEFAULT_PREREQUISITES: tuple[tuple[str, str], ...] = (
    ("astrbot_plugin_libi18nject", "cxykevin/astrbot_plugin_libi18nject"),
    ("astrbot_plugin_librunner", "cxykevin/astrbot_plugin_librunner"),
)

#: 插件配置项名：允许用户把自动安装来源改成镜像/内网地址。
REPO_CONFIG_KEYS: dict[str, str] = {
    "astrbot_plugin_libi18nject": "prerequisite_libi18nject_repo",
    "astrbot_plugin_librunner": "prerequisite_librunner_repo",
}

#: 单条 clone 命令的超时（秒）。过短会误判慢网络，过长会拖慢 AstrBot 启动。
CLONE_TIMEOUT_SEC = 60

#: 错误摘要保留的最大字符数（塞进日志与「加载失败插件」的说明里）。
_ERROR_SUMMARY_LIMIT = 400

#: 落盘记录用的插件数据目录名（与 ``metadata.yaml`` 的 ``name`` 一致）。
_PLUGIN_DATA_DIR_NAME = "astrbot_plugin_0528_v2"
_ATTEMPT_FILE_NAME = "prerequisite.json"

#: 日志前缀。
LOG_PREFIX = "0528v2"

#: 本进程内已尝试过自动安装的前置名。
_attempted: set[str] = set()


class PrerequisiteError(RuntimeError):
    """前置插件缺失且无法自动安装。

    ``main.initialize()`` 会抛出它：AstrBot 捕获后把原因记入「加载失败插件」，
    在 WebUI 的插件页直接可见，同时本插件处于「未启用」状态（即停止工作）。
    """


@dataclass(frozen=True)
class Prerequisite:
    """一个前置插件。

    Attributes:
        name: 插件目录名（也是 ``metadata.yaml`` 的 ``name``）。
        repo: GitHub ``owner/repo``，或任意可被 git 克隆的地址。
    """

    name: str
    repo: str

    def describe(self) -> str:
        """返回便于展示与日志阅读的描述。"""
        return f"{self.name}（{self.repo}）"


@dataclass
class InstallResult:
    """自动安装的结果。"""

    ok: bool
    message: str
    source: str = ""
    path: Path | None = None


@dataclass
class ActivationResult:
    """就地激活前置插件的结果。"""

    ok: bool
    message: str
    instance: Any = None


@dataclass
class EnsureResult:
    """单个前置就绪检查的最终结果。"""

    ready: bool
    installed_now: bool
    message: str
    activation: ActivationResult | None = None


@dataclass
class MultiEnsureResult:
    """全部前置的就绪检查结果。"""

    ready: bool
    results: dict[str, EnsureResult] = field(default_factory=dict)
    instances: list[Any] = field(default_factory=list)
    message: str = ""

    def describe(self) -> str:
        """返回多行文本，供日志与状态命令使用。"""
        lines = []
        for name, result in self.results.items():
            state = "就绪" if result.ready else "未就绪"
            detail = result.message
            lines.append(f"- {name}: {state}（{detail}）")
        return "\n".join(lines) if lines else "- （没有配置前置插件）"


def build_prerequisites(config: Any = None) -> tuple[Prerequisite, ...]:
    """按插件配置构造前置列表（含镜像/内网来源覆盖）。

    Args:
        config: 插件配置字典；非字典或缺失时用默认来源。

    Returns:
        前置列表，顺序与 :data:`DEFAULT_PREREQUISITES` 一致。
    """
    settings = config if isinstance(config, dict) else {}
    built: list[Prerequisite] = []
    for name, default_repo in DEFAULT_PREREQUISITES:
        repo = default_repo
        key = REPO_CONFIG_KEYS.get(name)
        override = settings.get(key) if key else None
        if isinstance(override, str) and override.strip():
            repo = override.strip()
        built.append(Prerequisite(name, repo))
    return tuple(built)


def plugin_store_path() -> Path:
    """返回 ``data/plugins`` 目录。

    Returns:
        插件目录；内核路径工具不可用时回退到 ``<cwd>/data/plugins``。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

        return Path(get_astrbot_plugin_path())
    except Exception:  # noqa: BLE001 - 内核路径工具缺席时按约定回退
        return Path.cwd() / "data" / "plugins"


def plugin_data_path() -> Path:
    """返回 ``data/plugin_data`` 目录（持久化目录，不会被插件更新覆盖）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path())
    except Exception:  # noqa: BLE001 - 同上
        return Path.cwd() / "data" / "plugin_data"


def installed_path(prereq: Prerequisite, *, store: Path | str | None = None) -> Path | None:
    """返回前置插件的安装目录。

    Args:
        prereq: 前置描述。
        store: 插件目录，默认取 :func:`plugin_store_path`（自测可注入临时目录）。

    Returns:
        安装目录（含 ``metadata.yaml``）；未安装时返回 None。
    """
    base = Path(store) if store is not None else plugin_store_path()
    directory = base / prereq.name
    if (directory / "metadata.yaml").is_file():
        return directory
    return None


def is_installed(prereq: Prerequisite, *, store: Path | str | None = None) -> bool:
    """前置插件是否已经就位（只看文件，不要求内核已加载）。"""
    return installed_path(prereq, store=store) is not None


def clone_commands(
    prereq: Prerequisite, target: Path, *, use_gh: bool = True
) -> list[tuple[str, list[str]]]:
    """构造可用的克隆命令列表（按优先级排列）。

    Args:
        prereq: 前置描述。
        target: 目标目录。
        use_gh: 是否优先尝试 ``gh repo clone``。

    Returns:
        ``(可读命令名, argv)`` 列表；本机没有 gh/git 时为空。
    """
    commands: list[tuple[str, list[str]]] = []
    if use_gh:
        gh = shutil.which("gh")
        if gh:
            commands.append(
                ("gh repo clone", [gh, "repo", "clone", prereq.repo, str(target)])
            )
    git = shutil.which("git")
    if git:
        commands.append(("git clone", [git, "clone", clone_url(prereq.repo), str(target)]))
    return commands


def clone_url(repo: str) -> str:
    """把 ``owner/repo`` 补成完整 URL；已是地址时原样返回。"""
    if repo.startswith(("http://", "https://", "git@", "ssh://", "file://", "/", ".")):
        return repo
    return f"https://github.com/{repo}.git"


async def install(
    prereq: Prerequisite,
    *,
    store: Path | str | None = None,
    timeout: float = CLONE_TIMEOUT_SEC,
    use_gh: bool = True,
) -> InstallResult:
    """尝试自动安装前置插件。

    Args:
        prereq: 前置描述。
        store: 插件目录，默认 :func:`plugin_store_path`。
        timeout: 单条命令的超时秒数。
        use_gh: 是否优先尝试 ``gh repo clone``。

    Returns:
        安装结果；失败时 ``message`` 里是各条命令的错误摘要。
    """
    base = Path(store) if store is not None else plugin_store_path()
    target = base / prereq.name
    if is_installed(prereq, store=base):
        return InstallResult(True, "前置插件已存在", "skip", target)

    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return InstallResult(False, f"无法创建插件目录 {base}：{exc}", "", target)

    if target.exists():
        # 上次 clone 失败留下的半成品目录：清掉重来（只动前置自己的目录）。
        shutil.rmtree(target, ignore_errors=True)

    commands = clone_commands(prereq, target, use_gh=use_gh)
    if not commands:
        return InstallResult(False, "本机找不到 gh 或 git，无法自动安装", "", target)

    errors: list[str] = []
    for label, argv in commands:
        ok, detail = await _run_command(argv, timeout=timeout)
        if ok and is_installed(prereq, store=base):
            return InstallResult(True, f"已通过 {label} 安装", label, target)
        errors.append(f"{label} 失败：{detail}")
        shutil.rmtree(target, ignore_errors=True)
    return InstallResult(False, "；".join(errors), "", target)


async def _run_command(argv: list[str], *, timeout: float) -> tuple[bool, str]:
    """执行一条命令并返回 ``(是否成功, 输出摘要)``。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        return False, f"命令不可用（{exc}）"
    except OSError as exc:
        return False, f"无法启动命令（{exc}）"

    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):  # 进程可能已经退出
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return False, f"超过 {timeout:g} 秒未完成"

    summary = _summarize(output)
    if process.returncode == 0:
        return True, summary or "成功"
    return False, summary or f"退出码 {process.returncode}"


def _summarize(output: bytes | None) -> str:
    """把命令输出压成一行摘要。"""
    if not output:
        return ""
    text = " ".join(output.decode("utf-8", errors="replace").split())
    if len(text) > _ERROR_SUMMARY_LIMIT:
        text = "…" + text[-_ERROR_SUMMARY_LIMIT:]
    return text


def loaded_plugin(context: Any, name: str) -> Any:
    """返回内核已加载并激活的插件实例（没有则返回 None）。"""
    if context is None:
        return None
    try:
        star = context.get_registered_star(name)
    except Exception:  # noqa: BLE001 - 上下文不完整时视为未加载
        return None
    if star is None or not getattr(star, "activated", False):
        return None
    return getattr(star, "star_cls", None)


def import_plugin_module(name: str, module: str = "main") -> Any | None:
    """导入插件目录下的模块。

    刚 clone 出来、内核还没来得及加载的前置插件只能这样拿到它的类；每次调用都
    重新走 :mod:`importlib`，因此「先失败、后补装」的场景也能接上。

    Args:
        name: 插件目录名。
        module: 模块名（不含 ``.py``）。

    Returns:
        模块对象；都导入失败时返回 None。
    """
    for candidate in (
        f"data.plugins.{name}.{module}",
        f"astrbot.builtin_stars.{name}.{module}",
    ):
        try:
            return importlib.import_module(candidate)
        except Exception as exc:  # noqa: BLE001 - 逐个候选尝试
            logger.debug("%s: 导入 %s 失败：%s", LOG_PREFIX, candidate, exc)
    return None


def find_star_class(module: Any) -> type | None:
    """在模块里找出第一个 ``Star`` 子类（插件主类）。"""
    try:
        from astrbot.api.star import Star
    except Exception:  # noqa: BLE001 - 内核 API 变动时放弃激活
        return None
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Star) and value is not Star:
            return value
    return None


async def activate(prereq: Prerequisite, context: Any) -> ActivationResult:
    """就地激活前置插件，不重启 AstrBot 主进程。

    内核没有「重载任意插件」的公开接口（``PluginManager`` 只能从
    ``AstrBotCoreLifecycle`` 拿到），所以这里直接在同一个 ``Context`` 上实例化前置
    插件的 ``Star`` 子类并调用 ``initialize()``：它的补丁与登记随即生效，等价于
    「只重启了前置插件」。

    幂等：前置已经由内核加载并激活时直接返回成功（``instance`` 为 None，表示
    释放时不需要本插件插手）。

    Args:
        prereq: 前置描述。
        context: 当前插件的 ``Context``（与前置共用同一个实例）。

    Returns:
        激活结果；``instance`` 是本插件手动拉起的插件实例，调用方应在 ``terminate()``
        里调用它的 ``terminate()`` 以免补丁泄漏。
    """
    if loaded_plugin(context, prereq.name) is not None:
        return ActivationResult(True, "前置插件已由内核加载")

    module = import_plugin_module(prereq.name)
    if module is None:
        return ActivationResult(False, f"无法导入 {prereq.name} 的模块")

    star_cls = find_star_class(module)
    if star_cls is None:
        return ActivationResult(False, f"{prereq.name} 里没有找到 Star 子类")

    try:
        instance = star_cls(context=context)
        await instance.initialize()
    except Exception as exc:  # noqa: BLE001 - 激活失败不影响本插件自身
        return ActivationResult(False, f"激活 {prereq.name} 失败：{exc}")
    logger.info("%s: 已就地激活前置插件 %s（无需重启 AstrBot）。", LOG_PREFIX, prereq.name)
    return ActivationResult(True, f"已就地激活 {prereq.name}", instance)


async def ensure(
    prereq: Prerequisite,
    *,
    context: Any = None,
    auto_install: bool = True,
    store: Path | str | None = None,
    timeout: float = CLONE_TIMEOUT_SEC,
    use_gh: bool = True,
) -> EnsureResult:
    """确保单个前置插件就绪（缺失则安装 + 就地激活）。

    Args:
        prereq: 前置描述。
        context: 当前插件上下文，用于就地激活前置。
        auto_install: 缺失时是否尝试自动安装。
        store: 插件目录，默认 :func:`plugin_store_path`。
        timeout: 单条命令超时秒数。
        use_gh: 是否优先尝试 ``gh repo clone``。

    Returns:
        结果；``ready`` 为 False 时调用方应停止工作并上报。
    """
    if is_installed(prereq, store=store):
        return EnsureResult(True, False, f"前置插件 {prereq.name} 已安装")

    if not auto_install:
        return EnsureResult(False, False, f"前置插件 {prereq.describe()} 未安装")

    if already_attempted(prereq):
        return EnsureResult(
            False,
            False,
            f"前置插件 {prereq.describe()} 仍未就绪（本进程已尝试过自动安装）",
        )

    mark_attempted(prereq)
    result = await install(prereq, store=store, timeout=timeout, use_gh=use_gh)
    if not result.ok:
        record_attempt(prereq, result)
        return EnsureResult(False, False, f"自动安装前置插件失败：{result.message}")

    activation = await activate(prereq, context) if context is not None else None
    record_attempt(
        prereq, result, activated=activation.message if activation is not None else ""
    )
    message = f"已自动安装前置插件 {prereq.describe()}"
    if activation is not None and not activation.ok:
        message += f"；就地激活未完成（{activation.message}）"
    return EnsureResult(True, True, message, activation)


async def ensure_all(
    prereqs: Iterable[Prerequisite] | None = None,
    *,
    context: Any = None,
    auto_install: bool = True,
    store: Path | str | None = None,
    timeout: float = CLONE_TIMEOUT_SEC,
    use_gh: bool = True,
) -> MultiEnsureResult:
    """确保全部前置插件就绪（逐个安装 + 就地激活）。

    与 :func:`ensure` 的区别：**已安装但内核没加载**的前置也会被就地激活。冷启动时
    两个前置都不在内核的加载清单里，「文件存在」并不等于「功能可用」，所以这里对每个
    前置都走一遍 :func:`activate`（它是幂等的，内核已经加载时直接跳过）。

    Args:
        prereqs: 前置列表，默认 :func:`build_prerequisites` 的默认来源。
        context: 当前插件上下文，用于就地激活前置。
        auto_install: 缺失时是否尝试自动安装。
        store: 插件目录，默认 :func:`plugin_store_path`。
        timeout: 单条命令超时秒数。
        use_gh: 是否优先尝试 ``gh repo clone``。

    Returns:
        汇总结果；``ready`` 为 False 时调用方应停止工作并上报。
    """
    targets = tuple(prereqs) if prereqs is not None else build_prerequisites()
    report = MultiEnsureResult(ready=True)
    failures: list[str] = []

    for prereq in targets:
        installed_before = is_installed(prereq, store=store)
        result = await ensure(
            prereq,
            context=None,  # 安装与激活分开做，下面统一激活
            auto_install=auto_install,
            store=store,
            timeout=timeout,
            use_gh=use_gh,
        )
        if not result.ready:
            report.ready = False
            report.results[prereq.name] = result
            failures.append(f"{prereq.describe()}：{result.message}")
            continue

        activation = await activate(prereq, context) if context is not None else None
        ready = True
        message = result.message
        if activation is not None:
            message = f"{message}；{activation.message}"
            if not activation.ok:
                ready = False
                failures.append(f"{prereq.describe()}：{activation.message}")
            elif activation.instance is not None:
                report.instances.append(activation.instance)

        if not installed_before:
            log_install_detail(prereq, result, activation)

        report.results[prereq.name] = EnsureResult(
            ready=ready,
            installed_now=result.installed_now,
            message=message,
            activation=activation,
        )
        if not ready:
            report.ready = False

    report.message = "前置插件全部就绪" if report.ready else "；".join(failures)
    return report


def log_install_detail(
    prereq: Prerequisite, result: EnsureResult, activation: ActivationResult | None
) -> None:
    """把一次自动安装的关键信息写进日志（便于诊断）。"""
    if result.installed_now:
        logger.info("%s: %s", LOG_PREFIX, result.message)
    if activation is not None and activation.instance is not None:
        logger.info("%s: 已接管前置 %s 的生命周期（terminate 时释放）", LOG_PREFIX, prereq.name)


def attempts_path() -> Path:
    """自动安装记录的落盘路径。"""
    return plugin_data_path() / _PLUGIN_DATA_DIR_NAME / _ATTEMPT_FILE_NAME


def read_attempts() -> dict[str, Any]:
    """读取自动安装记录；文件缺失或损坏时返回空字典。"""
    try:
        data = json.loads(attempts_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 记录只是诊断信息
        return {}
    return data if isinstance(data, dict) else {}


def record_attempt(prereq: Prerequisite, result: InstallResult, *, activated: str = "") -> None:
    """把一次自动安装的结果落盘。

    Args:
        prereq: 前置描述。
        result: 安装结果。
        activated: 就地激活的结果描述。
    """
    path = attempts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_attempts()
        previous = data.get(prereq.name)
        attempts = 1
        if isinstance(previous, dict):
            attempts = int(previous.get("attempts", 0)) + 1
        data[prereq.name] = {
            "attempts": attempts,
            "last_time": time.time(),
            "ok": result.ok,
            "message": result.message,
            "source": result.source,
            "activated": activated,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 记录失败不影响主流程
        logger.debug("%s: 写入前置安装记录失败", LOG_PREFIX, exc_info=True)


def already_attempted(prereq: Prerequisite) -> bool:
    """本进程内是否已经尝试过自动安装。"""
    return prereq.name in _attempted


def mark_attempted(prereq: Prerequisite) -> None:
    """标记本进程已尝试过自动安装。"""
    _attempted.add(prereq.name)


def reset_attempts() -> None:
    """清空进程内尝试记录（自测与诊断用）。"""
    _attempted.clear()
