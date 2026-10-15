"""astrbot_plugin_0528_v2 离线自测 / 回归检查。

覆盖：前置插件的定义与来源配置、安装检测、克隆命令构造、用本地 git 仓库离线演练
自动安装、ensure/ensure_all 的就绪判定、就地激活失败时的如实上报、插件 initialize
的「强制 / 放行」两条路径、状态文本、落盘记录、terminate 释放顺序，以及仓库文件约束
（配置 schema、metadata 一致性、``PLUGIN_APIS.md`` 不进仓库）。

用法（在 AstrBot 仓库中，使用该实例的 uv 环境）::

    uv run python data/plugins/astrbot_plugin_0528_v2/selftest.py

脚本会把 ``ASTRBOT_ROOT`` 指向临时目录（除非调用方已显式指定），因此既不会读写真实
实例的 ``data/plugin_data``，也不会真的联网安装插件。全部检查通过时退出码为 0。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")
if "ASTRBOT_ROOT" not in os.environ:
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="0528v2-selftest-")

from data.plugins.astrbot_plugin_0528_v2 import main as own_main
from data.plugins.astrbot_plugin_0528_v2 import prerequisite as prereq_mod
from data.plugins.astrbot_plugin_0528_v2.main import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
    Plugin0528V2,
)

NAME_I18N = "astrbot_plugin_libi18nject"
NAME_RUNNER = "astrbot_plugin_librunner"
PLUGIN_DIR = Path(__file__).resolve().parent
FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# --------------------------------------------------- 0. 自测隔离（先确认再往下跑）
store_path = prereq_mod.plugin_store_path()
data_path = prereq_mod.plugin_data_path()
check(
    "自测隔离在临时 ASTRBOT_ROOT 下（不会碰真实实例）",
    str(store_path).startswith(os.environ["ASTRBOT_ROOT"])
    and str(data_path).startswith(os.environ["ASTRBOT_ROOT"]),
    f"{store_path} / {data_path}",
)
if FAILURES:
    print("\n隔离检查未通过：后续检查可能影响真实实例，已中止。")
    raise SystemExit(1)

# --------------------------------------------------- 1. 前置定义与来源配置
check(
    "默认定义两个前置",
    tuple(name for name, _ in prereq_mod.DEFAULT_PREREQUISITES) == (NAME_I18N, NAME_RUNNER),
    str(prereq_mod.DEFAULT_PREREQUISITES),
)
check(
    "libi18nject 排在 librunner 前（被依赖者优先）",
    [name for name, _ in prereq_mod.DEFAULT_PREREQUISITES].index(NAME_I18N)
    < [name for name, _ in prereq_mod.DEFAULT_PREREQUISITES].index(NAME_RUNNER),
)
defaults = dict(prereq_mod.DEFAULT_PREREQUISITES)
check(
    "默认来源指向作者的 GitHub 仓库",
    defaults[NAME_I18N] == "cxykevin/astrbot_plugin_libi18nject"
    and defaults[NAME_RUNNER] == "cxykevin/astrbot_plugin_librunner",
    str(defaults),
)
check(
    "每个前置都有对应的配置项键",
    set(prereq_mod.REPO_CONFIG_KEYS) == {NAME_I18N, NAME_RUNNER},
    str(prereq_mod.REPO_CONFIG_KEYS),
)

built = prereq_mod.build_prerequisites()
check(
    "build_prerequisites() 默认与常量一致",
    tuple((p.name, p.repo) for p in built) == prereq_mod.DEFAULT_PREREQUISITES,
    str([(p.name, p.repo) for p in built]),
)
check("build_prerequisites(None) 不炸", len(prereq_mod.build_prerequisites(None)) == 2)
check("build_prerequisites(非字典) 回退默认", len(prereq_mod.build_prerequisites("oops")) == 2)

overridden = prereq_mod.build_prerequisites(
    {
        "prerequisite_librunner_repo": "  https://git.example.com/librunner.git  ",
        "prerequisite_libi18nject_repo": "/srv/mirror/libi18nject",
    }
)
check(
    "配置里的来源覆盖生效并去掉首尾空白",
    overridden[0].repo == "/srv/mirror/libi18nject"
    and overridden[1].repo == "https://git.example.com/librunner.git",
    str([(p.name, p.repo) for p in overridden]),
)
blank = prereq_mod.build_prerequisites({"prerequisite_librunner_repo": "   "})
check(
    "空白的来源配置回退默认值",
    blank[1].repo == defaults[NAME_RUNNER],
    blank[1].repo,
)

# --------------------------------------------------- 2. 安装检测
empty_store = Path(tempfile.mkdtemp(prefix="0528v2-detect-")) / "plugins"
probe = prereq_mod.Prerequisite(NAME_I18N, defaults[NAME_I18N])
check("目录不存在时判定为未安装", prereq_mod.is_installed(probe, store=empty_store) is False)
stub = empty_store / probe.name
stub.mkdir(parents=True)
check("只有空目录仍算未安装", prereq_mod.is_installed(probe, store=empty_store) is False)
(stub / "metadata.yaml").write_text(f"name: {probe.name}\n", encoding="utf-8")
check("含 metadata.yaml 即算已安装", prereq_mod.is_installed(probe, store=empty_store) is True)
check("解析出安装目录", prereq_mod.installed_path(probe, store=empty_store) == stub)

# --------------------------------------------------- 3. 克隆命令构造
git_only = prereq_mod.clone_commands(probe, empty_store / "target", use_gh=False)
check(
    "git 回退命令使用完整 GitHub 地址",
    len(git_only) == 1
    and git_only[0][0] == "git clone"
    and git_only[0][1][-2] == "https://github.com/cxykevin/astrbot_plugin_libi18nject.git",
    str(git_only),
)
check(
    "已是完整地址时不再拼接",
    prereq_mod.clone_url("git@github.com:cxykevin/astrbot_plugin_librunner.git")
    == "git@github.com:cxykevin/astrbot_plugin_librunner.git"
    and prereq_mod.clone_url("cxykevin/astrbot_plugin_librunner")
    == "https://github.com/cxykevin/astrbot_plugin_librunner.git",
)

# --------------------------------------------------- 4. 离线安装演练（本地 git 仓库）
if shutil.which("git") is None:
    print("[SKIP] 本机没有 git，跳过自动安装演练")
else:
    seed_root = Path(tempfile.mkdtemp(prefix="0528v2-seed-"))
    seeds = {NAME_I18N: seed_root / "seed-i18n", NAME_RUNNER: seed_root / "seed-runner"}
    for name, path in seeds.items():
        path.mkdir(parents=True, exist_ok=True)
        (path / "metadata.yaml").write_text(f"name: {name}\n", encoding="utf-8")
        (path / "main.py").write_text("# 前置占位\n", encoding="utf-8")

    async def _seed_repo(path):
        env = dict(os.environ)
        env.update(
            GIT_AUTHOR_NAME="selftest",
            GIT_AUTHOR_EMAIL="selftest@example.com",
            GIT_COMMITTER_NAME="selftest",
            GIT_COMMITTER_EMAIL="selftest@example.com",
        )
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "seed"]):
            process = await asyncio.create_subprocess_exec(*argv, cwd=str(path), env=env)
            await process.wait()

    for path in seeds.values():
        asyncio.run(_seed_repo(path))

    local_prereqs = tuple(
        prereq_mod.Prerequisite(name, str(seeds[name])) for name, _ in prereq_mod.DEFAULT_PREREQUISITES
    )
    install_store = Path(tempfile.mkdtemp(prefix="0528v2-install-")) / "plugins"

    single = asyncio.run(
        prereq_mod.install(local_prereqs[0], store=install_store, use_gh=False, timeout=30)
    )
    check("前置缺失时能自动安装", single.ok, single.message)
    check(
        "安装后前置就位",
        prereq_mod.is_installed(local_prereqs[0], store=install_store) is True,
    )
    repeat_single = asyncio.run(
        prereq_mod.install(local_prereqs[0], store=install_store, use_gh=False, timeout=30)
    )
    check("已安装时不再重复克隆", repeat_single.ok and repeat_single.source == "skip", repeat_single.message)

    prereq_mod.reset_attempts()
    report = asyncio.run(
        prereq_mod.ensure_all(
            local_prereqs, context=None, store=install_store, use_gh=False, timeout=30
        )
    )
    check("ensure_all 逐个装好全部前置", report.ready, report.message)
    check(
        "全部前置都已就位",
        all(prereq_mod.is_installed(p, store=install_store) for p in local_prereqs),
    )
    check(
        "检查结果按定义顺序返回",
        list(report.results) == [NAME_I18N, NAME_RUNNER],
        str(list(report.results)),
    )
    check("没有 context 时不就地拉起实例", report.instances == [], str(report.instances))

    second = asyncio.run(
        prereq_mod.ensure_all(
            local_prereqs, context=None, store=install_store, use_gh=False, timeout=30
        )
    )
    check(
        "已就绪时不再重复克隆",
        second.ready and all(not r.installed_now for r in second.results.values()),
        str([(n, r.installed_now) for n, r in second.results.items()]),
    )

    broken = tuple(
        prereq_mod.Prerequisite(name, str(seed_root / "nope" / name))
        for name, _ in prereq_mod.DEFAULT_PREREQUISITES
    )
    broken_store = Path(tempfile.mkdtemp(prefix="0528v2-broken-")) / "plugins"
    prereq_mod.reset_attempts()
    broken_report = asyncio.run(
        prereq_mod.ensure_all(broken, context=None, store=broken_store, use_gh=False, timeout=30)
    )
    check(
        "来源不可用时如实报告未就绪",
        not broken_report.ready and bool(broken_report.message),
        broken_report.message,
    )
    check(
        "未就绪时报告里点名哪个前置失败",
        NAME_I18N in broken_report.message,
        broken_report.message,
    )

    prereq_mod.reset_attempts()
    missing = prereq_mod.Prerequisite(NAME_RUNNER, str(seed_root / "still-nope"))
    fresh_store = Path(tempfile.mkdtemp(prefix="0528v2-once-")) / "plugins"
    first_try = asyncio.run(prereq_mod.ensure(missing, store=fresh_store, use_gh=False, timeout=30))
    check(
        "ensure 在装不上时返回未就绪",
        not first_try.ready and "自动安装前置插件失败" in first_try.message,
        first_try.message,
    )
    second_try = asyncio.run(prereq_mod.ensure(missing, store=fresh_store, use_gh=False, timeout=30))
    check(
        "同一进程内不重复尝试自动安装",
        "已尝试过自动安装" in second_try.message,
        second_try.message,
    )
    prereq_mod.reset_attempts()

# --------------------------------------------------- 5. 就地激活的如实上报
check(
    "插件模块不存在时导入失败返回 None",
    prereq_mod.import_plugin_module("astrbot_plugin_not_installed_probe") is None,
)
probe_prereq = prereq_mod.Prerequisite("astrbot_plugin_not_installed_probe", "cxykevin/nope")
activation = asyncio.run(prereq_mod.activate(probe_prereq, context=None))
check(
    "前置不在场时就地激活如实失败",
    not activation.ok and "无法导入" in activation.message,
    activation.message,
)
check("context 为 None 时不视为已加载", prereq_mod.loaded_plugin(None, NAME_I18N) is None)
check(
    "find_star_class 能找到本插件的 Star 子类",
    prereq_mod.find_star_class(own_main) is Plugin0528V2,
    str(prereq_mod.find_star_class(own_main)),
)

# --------------------------------------------------- 6. 插件 initialize 两条路径
bad_root = Path(tempfile.mkdtemp(prefix="0528v2-unreachable-"))
strict_config = {
    "enforce_prerequisite": True,
    "prerequisite_libi18nject_repo": str(bad_root / "missing-i18n"),
    "prerequisite_librunner_repo": str(bad_root / "missing-runner"),
}
prereq_mod.reset_attempts()
strict_plugin = Plugin0528V2(context=None, config=strict_config)
strict_error = None
try:
    asyncio.run(strict_plugin.initialize())
except prereq_mod.PrerequisiteError as exc:
    strict_error = str(exc)
check(
    "装不上时抛 PrerequisiteError（由内核在 WebUI 报告）",
    bool(strict_error and "前置插件未就绪" in strict_error),
    str(strict_error),
)
check(
    "失败时记录了未就绪报告",
    strict_plugin._prereq_report is not None and strict_plugin._prereq_report.ready is False,
)
check("失败时没有拉起任何实例", strict_plugin._activated == [], str(strict_plugin._activated))

prereq_mod.reset_attempts()
lax_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
lax_error = None
try:
    asyncio.run(lax_plugin.initialize())
except Exception as exc:  # noqa: BLE001 - 放行路径不应抛错
    lax_error = exc
check("关闭强制后缺前置也照常启用", lax_error is None, repr(lax_error))
check(
    "关闭强制时仍如实记录未就绪",
    lax_plugin._prereq_report is not None and lax_plugin._prereq_report.ready is False,
)

status_text = lax_plugin._render_status()
check(
    "状态文本含插件名与版本",
    PLUGIN_NAME in status_text and PLUGIN_VERSION in status_text,
    status_text,
)
check(
    "状态文本列出两个前置",
    status_text.count(NAME_I18N) >= 1 and status_text.count(NAME_RUNNER) >= 1,
    status_text,
)
check("状态文本反映强制开关状态", "强制前置插件: 关闭" in status_text, status_text)
check("状态文本说明实例归属", "terminate 时释放" in status_text, status_text)

# --------------------------------------------------- 7. terminate 释放顺序
class FakeInstance:
    """假的前置实例：记录释放顺序，可选地抛错。"""

    def __init__(self, name, log, boom=False):
        self.name = name
        self.log = log
        self.boom = boom

    async def terminate(self):
        self.log.append(self.name)
        if self.boom:
            raise RuntimeError("boom")


release_log = []
shutdown_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
shutdown_plugin._activated = [
    FakeInstance(NAME_I18N, release_log),
    FakeInstance(NAME_RUNNER, release_log),
]
asyncio.run(shutdown_plugin.terminate())
check(
    "terminate 逆序释放就地拉起的前置实例",
    release_log == [NAME_RUNNER, NAME_I18N],
    str(release_log),
)
check("terminate 后清空实例列表", shutdown_plugin._activated == [])

boom_log = []
boom_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
boom_plugin._activated = [FakeInstance(NAME_RUNNER, boom_log, boom=True)]
boom_error = None
try:
    asyncio.run(boom_plugin.terminate())
except Exception as exc:  # noqa: BLE001 - 释放失败不应冒泡
    boom_error = exc
check("单个实例释放失败不阻塞卸载", boom_error is None and boom_log == [NAME_RUNNER], repr(boom_error))

# --------------------------------------------------- 8. 落盘记录
check(
    "安装记录落在本插件自己的数据目录",
    prereq_mod.attempts_path().name == "prerequisite.json"
    and prereq_mod.attempts_path().parent.name == PLUGIN_NAME,
    str(prereq_mod.attempts_path()),
)
probe_result = prereq_mod.InstallResult(True, "已通过 selftest 安装", "selftest")
prereq_mod.record_attempt(prereq_mod.Prerequisite(NAME_RUNNER, defaults[NAME_RUNNER]), probe_result, activated="已就地激活")
logged = prereq_mod.read_attempts()
check(
    "安装结果会落盘并可读回",
    isinstance(logged, dict) and logged.get(NAME_RUNNER, {}).get("message") == "已通过 selftest 安装",
    str(logged),
)
repeat_before = prereq_mod.read_attempts().get(NAME_RUNNER, {}).get("attempts", 0)
prereq_mod.record_attempt(prereq_mod.Prerequisite(NAME_RUNNER, defaults[NAME_RUNNER]), probe_result)
repeat_after = prereq_mod.read_attempts().get(NAME_RUNNER, {}).get("attempts", 0)
check(
    "重复尝试时累计次数",
    isinstance(repeat_before, int)
    and isinstance(repeat_after, int)
    and repeat_after == repeat_before + 1,
    f"{repeat_before} -> {repeat_after}",
)

# --------------------------------------------------- 9. 仓库文件约束
schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
check(
    "配置 schema 提供三项配置",
    set(schema) == {"enforce_prerequisite", "prerequisite_librunner_repo", "prerequisite_libi18nject_repo"},
    str(sorted(schema)),
)
check(
    "schema 里的默认来源与常量一致",
    schema["prerequisite_librunner_repo"]["default"] == defaults[NAME_RUNNER]
    and schema["prerequisite_libi18nject_repo"]["default"] == defaults[NAME_I18N],
)
metadata = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
check("metadata 的 name 与插件名一致", f"name: {PLUGIN_NAME}" in metadata, metadata)
check("metadata 的 version 与常量一致", f"version: {PLUGIN_VERSION}" in metadata, metadata)
gitignore_lines = [
    line.strip() for line in (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
]
check(
    "PLUGIN_APIS.md 已被 gitignore（不进仓库）",
    "PLUGIN_APIS.md" in gitignore_lines,
    str(gitignore_lines[-3:]),
)
check("插件目录名与插件名一致", PLUGIN_DIR.name == PLUGIN_NAME, PLUGIN_DIR.name)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
