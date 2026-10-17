"""astrbot_plugin_0528_v2 离线自测 / 回归检查。

覆盖：前置插件的定义与来源配置、安装检测、克隆命令构造、用本地 git 仓库离线演练
自动安装、ensure/ensure_all 的就绪判定、就地激活失败时的如实上报、插件 initialize
的「强制 / 放行」两条路径、状态文本、落盘记录、terminate 释放顺序，以及仓库文件约束
（配置 schema、metadata 一致性、``PLUGIN_APIS.md`` 不进仓库）。

另一大半覆盖 alkaid0 侧（全部用假客户端，不联网）：

* ACP v2 协议层：方法名/事件常量、JSON-RPC 报文构造与应答判别、ContentBlock 文本提取；
* 群键与工作目录模板：``{groupid}`` 等占位符渲染、私聊退化、未知占位符保留；
* 执行器参数解析：面板优先 / 插件兜底 / 类型兜底 / 布尔字符串；
* 群连接池：一群一连接、参数变更换连接、空闲回收（busy 跳过）、全量释放、建连失败不留垃圾；
* 会话记录（SQLite）：写入/覆盖/落盘/删除/降级，有记录走 ``session/resume``（不带
  ``replayFrom``）续接、续接失败回退新开、``reset_session`` 与 ``/areset`` 的表现；
* Runner 端到端（假客户端）：累积正文、过程消息回显（去重 / 限流 / 可关闭）、
  流式增量、出错上报、断线速报、结束不断连；
* 会话级配置项与 ``/amodel`` ``/aeffort``：开会话时记下 ``configOptions``、
  ``config_option_update`` 广播刷新缓存、按值 / 名字 / 编号切模型与推理强度
  （只动当前会话、两者互不干扰）、同名与找不到的提示、服务端拒绝与连不上的如实上报；
* 会话查看 / 切换与 ``/aresume``：``session/list`` 的列表渲染（编号 / 标题 / 截断 / 时间 /
  当前标记）、会话号与完整 ID 的解析、``switch_session`` 关旧会话再续接新会话并记账、
  失败不动记录、切到当前会话是空操作；
* 远端命令与 ``/acmd``：alkaid0 扩展能力标记（v0.4）、``available_commands_update``
  的解析与「开会话前挂监听才收得到」、与 alcoh 同步的服务端文案对照表（声明能力才翻，
  否则照原文）、命令文本自动补 ``/``、命令轮的通知消费（正文 / 无输出 / refusal 报错 /
  cancelled 不算错 / 掉线速报 / 等不到 idle 按超时）；
* 中止当前轮与 ``/astop``：``session/cancel`` 走**通知**（没有 id、不等回包）、
  没在跑 / 连接池没起来 / 发不出去都如实上报、排队的插话在回执里被点名、
  被中止的一轮照发半截正文并标出「本轮已终止」；
* 上下文用量与 ``/actx``：``usage_update`` 的解析（缺字段当 0、坏值不算）、会话缓存与
  换 / 新开会话时清空、重连续接同一会话时保留、``context x% (used/size)`` 的渲染口径
  （整数截断、封顶 100%、窗口未知只报已用）、命令各分支（没池子 / 没会话 / 没跑过 / 正常上报）；
* 工具审批：请求挂起（不回包）、聊天里发提示、``/aapprove`` 回 ``allow_once``；

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
import time
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")
if "ASTRBOT_ROOT" not in os.environ:
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="0528v2-selftest-")

from data.plugins.astrbot_plugin_0528_v2 import agent_runner as runner_mod
from data.plugins.astrbot_plugin_0528_v2 import main as own_main
from data.plugins.astrbot_plugin_0528_v2 import prerequisite as prereq_mod
from data.plugins.astrbot_plugin_0528_v2 import turn_queue as turn_queue_mod
from data.plugins.astrbot_plugin_0528_v2.acp import commands as acp_commands
from data.plugins.astrbot_plugin_0528_v2.acp import pool as pool_mod
from data.plugins.astrbot_plugin_0528_v2.acp import protocol as acp_proto
from data.plugins.astrbot_plugin_0528_v2.acp import store as store_mod
from data.plugins.astrbot_plugin_0528_v2.acp.client import DEFERRED, ACPError
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

# --------------------------------------------------- 9. ACP v2 协议层
check("协议版本为 2", acp_proto.PROTOCOL_VERSION == 2)
check(
    "方法名与 ACP v2 一致",
    (
        acp_proto.METHOD_INITIALIZE,
        acp_proto.METHOD_SESSION_NEW,
        acp_proto.METHOD_SESSION_LIST,
        acp_proto.METHOD_SESSION_PROMPT,
        acp_proto.METHOD_SESSION_CANCEL,
        acp_proto.METHOD_SESSION_CLOSE,
        acp_proto.METHOD_REQUEST_PERMISSION,
    )
    == (
        "initialize",
        "session/new",
        "session/list",
        "session/prompt",
        "session/cancel",
        "session/close",
        "session/request_permission",
    ),
)
check(
    "事件名与 alkaid0 扩展一致",
    acp_proto.UPDATE_AGENT_MESSAGE_CHUNK == "agent_message_chunk"
    and acp_proto.UPDATE_AGENT_THOUGHT_CHUNK == "agent_thought_chunk"
    and acp_proto.UPDATE_STATE == "state_update"
    and acp_proto.STATE_IDLE == "idle"
    and acp_proto.KEY_ERROR_MSG == "alk.cxykevin.top/error_msg"
    and acp_proto.UPDATE_TERMINAL == "alk.cxykevin.top/terminal_update",
)
check(
    "错误码对齐 JSON-RPC",
    acp_proto.ERROR_METHOD_NOT_FOUND == -32601
    and acp_proto.ERROR_SERVER == -32099
    and acp_proto.PERMISSION_OPTION_ALLOW_ONCE == "allow_once",
)

_rpc_request = acp_proto.jsonrpc_request(7, "session/prompt", {"sessionId": "s"})
_rpc_notification = acp_proto.jsonrpc_notification("session/update", {"sessionId": "s"})
_rpc_response = acp_proto.jsonrpc_result(7, {})
_rpc_error = acp_proto.jsonrpc_error(7, -32601, "nope")
check(
    "请求报文带 jsonrpc/id/method/params",
    _rpc_request["jsonrpc"] == "2.0"
    and _rpc_request["id"] == 7
    and _rpc_request["method"] == "session/prompt"
    and _rpc_request["params"] == {"sessionId": "s"},
    str(_rpc_request),
)
check("通知没有 id", "id" not in _rpc_notification, str(_rpc_notification))
check(
    "响应 / 请求 / 通知三态判别互斥",
    acp_proto.is_response(_rpc_response)
    and not acp_proto.is_request(_rpc_response)
    and not acp_proto.is_notification(_rpc_response)
    and acp_proto.is_request(
        {"jsonrpc": "2.0", "id": 1, "method": "session/request_permission", "params": {}}
    )
    and acp_proto.is_notification(_rpc_notification)
    and not acp_proto.is_response(_rpc_notification),
)
check("错误响应可读", "-32601" in acp_proto.describe_rpc_error(_rpc_error))
_non_object_rejected = False
try:
    acp_proto.parse_message("[1, 2, 3]")
except ValueError:
    _non_object_rejected = True
check("顶层不是对象的报文判为非法", _non_object_rejected)
check(
    "字节帧也能解析",
    acp_proto.parse_message(b'{"jsonrpc":"2.0","method":"x"}')["method"] == "x",
)
check(
    "文本提取兼容三种形状",
    acp_proto.extract_text("abc") == "abc"
    and acp_proto.extract_text({"type": "text", "text": "abc"}) == "abc"
    and acp_proto.extract_text(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    )
    == "ab"
    and acp_proto.extract_text({"content": {"type": "text", "text": "n"}}) == "n"
    and acp_proto.extract_text(123) == "",
)
check(
    "prompt 数组是文本块",
    acp_proto.prompt_blocks("hi") == [{"type": "text", "text": "hi"}],
)
check(
    "usage_update 解析：取 used/size，缺字段当 0，坏值一律不算",
    acp_proto.usage_of(
        {"sessionUpdate": "usage_update", "used": 11724, "size": 1024000}
    )
    == (11724, 1024000)
    and acp_proto.usage_of({"sessionUpdate": "usage_update", "used": 5}) == (5, 0)
    and acp_proto.usage_of({"sessionUpdate": "usage_update"}) == (0, 0)
    and acp_proto.usage_of(
        {"sessionUpdate": "usage_update", "used": "12", "size": 1}
    )
    is None
    and acp_proto.usage_of(
        {"sessionUpdate": "usage_update", "used": 1, "size": -1}
    )
    is None
    and acp_proto.usage_of(
        {"sessionUpdate": "agent_message_chunk", "used": 1, "size": 2}
    )
    is None
    and acp_proto.usage_of(None) is None
    and acp_proto.usage_of("oops") is None,
    str(acp_proto.usage_of({"sessionUpdate": "usage_update", "used": 11724, "size": 1024000})),
)
_config_option_sample = [
    {
        "configId": "model",
        "name": "Model",
        "type": "id",
        "currentValue": "1/b",
        "options": [
            {"value": "0/a", "name": "gpt-5"},
            {"value": "1/b"},  # 没有 name 时用 value 兜底
        ],
    },
    {"configId": "thought_level", "type": "id", "currentValue": "unset", "options": []},
]
check(
    "configOptions 取用：按 configId 找 / 读当前值 / 洗可选值",
    acp_proto.config_options_of({"configOptions": _config_option_sample})
    == _config_option_sample
    and acp_proto.config_options_of({"configOptions": "oops"}) == []
    and acp_proto.config_options_of(None) == []
    and acp_proto.option_current(
        acp_proto.find_config_option(_config_option_sample, "model")
    )
    == "1/b"
    and acp_proto.option_current(None) == ""
    and acp_proto.find_config_option(_config_option_sample, "nope") is None
    and len(
        acp_proto.option_choices(
            acp_proto.find_config_option(_config_option_sample, "model")
        )
    )
    == 2
    and acp_proto.option_choices(None) == []
    and acp_proto.option_choice_label({"value": "0/a", "name": " gpt-5 "})
    == ("0/a", "gpt-5")
    and acp_proto.option_choice_label({"value": "1/b"}) == ("1/b", "1/b")
    and acp_proto.option_choice_label("oops") == ("", ""),
)
check(
    "配置项常量与 alkaid0 对齐（model / thought_level / id）",
    acp_proto.CONFIG_ID_MODEL == "model"
    and acp_proto.CONFIG_ID_THOUGHT_LEVEL == "thought_level"
    and acp_proto.CONFIG_TYPE_SELECT == "id"
    and acp_proto.UPDATE_CONFIG_OPTION == "config_option_update"
    and acp_proto.METHOD_SESSION_SET_CONFIG_OPTION == "session/set_config_option",
)

# --------------------------------------------------- 9b. 远端命令层（/acmd）
ALKAID0_CAPABILITIES = {"alk.cxykevin.top/alkaid0/v0.4": {}}
#: 命令描述用 alkaid0 的英文原文：翻译表按原文逐字命中，正好验证同步关系。
COMPRESS_DESCRIPTION = "Compress the history"
BACKGROUND_DESCRIPTION = (
    "Set background mode on/off — keep session alive after all clients disconnect"
)


def _available_commands():
    """假 alkaid0 下发的命令列表（形状同真机的 ``availableCommands``）。"""
    return [
        {
            "name": "background",
            "description": BACKGROUND_DESCRIPTION,
            "input": {"type": "text", "hint": "on|off"},
        },
        {
            "name": "compress",
            "description": COMPRESS_DESCRIPTION,
            "input": {"type": "text", "hint": "(no args)"},
        },
        {
            "name": "weird",
            "description": "Third party text",
            "input": {"type": "text", "hint": ""},
        },
    ]


check(
    "能力标记：认 alkaid0 v0.4，其它形状一律为假",
    acp_commands.CAPABILITY_ALKAID0_V04 == "alk.cxykevin.top/alkaid0/v0.4"
    and acp_commands.has_capability(
        {"capabilities": ALKAID0_CAPABILITIES}, acp_commands.CAPABILITY_ALKAID0_V04
    )
    is True
    and acp_commands.has_capability(
        {"capabilities": {"other": {}}}, acp_commands.CAPABILITY_ALKAID0_V04
    )
    is False
    and acp_commands.has_capability({}, acp_commands.CAPABILITY_ALKAID0_V04) is False
    and acp_commands.has_capability(None, acp_commands.CAPABILITY_ALKAID0_V04) is False,
    str(acp_commands.has_capability({"capabilities": ALKAID0_CAPABILITIES}, "x")),
)
check(
    "文案对照表（与 alcoh 同步）：命中的翻中文，没收录 / 非字符串回退原文",
    acp_commands.translate_description(COMPRESS_DESCRIPTION) == "压缩历史记录"
    and acp_commands.translate_description(BACKGROUND_DESCRIPTION)
    == "开启/关闭后台模式——所有客户端断开后保持会话存活"
    and acp_commands.translate_description("Third party text") == "Third party text"
    and acp_commands.translate_description("") == ""
    and acp_commands.translate_description(None) == "",
    acp_commands.translate_description(COMPRESS_DESCRIPTION),
)
check(
    "对照表条目数与 alcoh 的 serverZh 一致（12 条，改了要跟着同步）",
    len(acp_commands.SERVER_ZH) == 12
    and set(acp_commands.SERVER_ZH.values()) != set(acp_commands.SERVER_ZH),
    str(len(acp_commands.SERVER_ZH)),
)
check(
    "命令列表解析：洗成 name/description/hint，坏条目跳过",
    acp_commands.available_commands_of(
        {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [
                _available_commands()[1],
                {"name": "/help", "description": "Show this help message"},
                {"name": "  ", "description": "没有名字"},
                "oops",
            ],
        }
    )
    == [
        {"name": "compress", "description": COMPRESS_DESCRIPTION, "hint": "(no args)"},
        {"name": "help", "description": "Show this help message", "hint": ""},
    ]
    and acp_commands.available_commands_of({"availableCommands": "oops"}) == []
    and acp_commands.available_commands_of({}) == []
    and acp_commands.available_commands_of(None) == [],
    str(acp_commands.available_commands_of({"availableCommands": _available_commands()})),
)
check(
    "slash_command_text：缺 / 才补，参数里的空格原样留着",
    acp_commands.slash_command_text("compress") == "/compress"
    and acp_commands.slash_command_text("/compress") == "/compress"
    and acp_commands.slash_command_text("  mask add secret  ") == "/mask add secret"
    and acp_commands.slash_command_text("") == ""
    and acp_commands.slash_command_text("   ") == "",
    acp_commands.slash_command_text("mask add secret"),
)

# --------------------------------------------------- 10. 群键与工作目录模板
class FakeEvent:
    """假消息事件：只提供 runner 用到的几个取值。"""

    def __init__(
        self,
        *,
        group_id="",
        session_id="",
        platform="aiocqhttp",
        umo="",
        sender_name="",
        sender_id="",
        group_name="",
        group_api_name=None,
    ):
        self._group_id = group_id
        self._session_id = session_id
        self._platform = platform
        #: 发言人：攒批拼提示词时标出处用（ID 缺省退到会话号，跟真机一样总有值）
        self._sender_name = sender_name
        self._sender_id = sender_id or session_id
        #: 群名（``group_name_display`` 注入用）：消息里自带的、``get_group()`` 查到的
        self._group_api_name = group_api_name
        #: 消息对象：注入时优先看 ``message_obj.group.group_name``
        self.message_obj = SimpleNamespace(
            group=SimpleNamespace(group_name=group_name) if group_name else None
        )
        self.unified_msg_origin = (
            umo or f"{platform}:group:{session_id or group_id or 'default'}"
        )

    def get_group_id(self):
        return self._group_id

    def get_session_id(self):
        return self._session_id

    def get_platform_name(self):
        return self._platform

    def get_sender_name(self):
        return self._sender_name

    def get_sender_id(self):
        return self._sender_id

    async def get_group(self):
        """模拟「消息里没带群名时去查群信息」（aiocqhttp 的 get_group_info）。"""
        if self._group_api_name is None:
            return None
        return SimpleNamespace(group_name=self._group_api_name)


class FakeCommandEvent(FakeEvent):
    """假命令事件：多一个 ``plain_result``，够 ``/areset``、``/amodel``、``/aeffort`` 用。

    群号 / 会话号默认取 ``umo`` 最后一段（``aiocqhttp:group:661`` → ``661``），
    这样 ``{groupid}`` 能正常渲染。
    """

    def __init__(self, umo, *, group_id=None, session_id=None):
        tail = umo.rsplit(":", 1)[-1]
        super().__init__(
            umo=umo,
            group_id=tail if group_id is None else group_id,
            session_id=tail if session_id is None else session_id,
        )
        self.replies = []

    def plain_result(self, text):
        self.replies.append(text)
        return ("plain", text)


group_event = FakeEvent(group_id="987654", session_id="987654", umo="aiocqhttp:group:987654")
private_event = FakeEvent(group_id="", session_id="10001", umo="aiocqhttp:friend:10001")
check("群键用 unified_msg_origin", runner_mod.object_key(group_event) == "aiocqhttp:group:987654")
check(
    "不同群 / 私聊各占一个键",
    runner_mod.object_key(group_event) != runner_mod.object_key(private_event),
)
check("取值全缺时兜底 default", runner_mod.object_key(object()) == "default")
check(
    "{groupid} 渲染成群号",
    runner_mod.render_workspace("/srv/alk/{groupid}", group_event) == "/srv/alk/987654",
)
check(
    "私聊没有群号时退化用会话号",
    runner_mod.render_workspace("/srv/alk/{groupid}", private_event) == "/srv/alk/10001",
)
check(
    "多个占位符一起渲染",
    runner_mod.render_workspace("/srv/{platform}/{groupid}/{sessionid}", group_event)
    == "/srv/aiocqhttp/987654/987654",
)
check(
    "不认识的占位符原样保留",
    runner_mod.render_workspace("/srv/{whoami}/{groupid}", group_event)
    == "/srv/{whoami}/987654",
)
check(
    "空模板回退默认模板",
    runner_mod.render_workspace("", group_event)
    == runner_mod.DEFAULT_WORKSPACE_TEMPLATE.replace("{groupid}", "987654"),
    runner_mod.render_workspace("", group_event),
)

# --------------------------------------------------- 11. 执行器参数解析
_default_settings = runner_mod.RunnerSettings.from_config(None, None)
check(
    "全默认时用代码默认值",
    _default_settings.url == runner_mod.DEFAULT_ACP_URL
    and _default_settings.workspace_template == runner_mod.DEFAULT_WORKSPACE_TEMPLATE
    and _default_settings.idle_timeout_sec == runner_mod.DEFAULT_IDLE_TIMEOUT_SEC,
    str(_default_settings),
)
_panel_settings = runner_mod.RunnerSettings.from_config(
    {"acp_url": "ws://panel/acp", "idle_timeout_sec": 60},
    {
        "acp_url": "ws://plugin/acp",
        "workspace_template": "/plugin/{groupid}",
        "idle_timeout_sec": 10,
        "show_thought": "true",
    },
)
check(
    "面板值优先、插件兜底",
    _panel_settings.url == "ws://panel/acp"
    and _panel_settings.workspace_template == "/plugin/{groupid}"
    and _panel_settings.idle_timeout_sec == 60,
    str(_panel_settings),
)
check("布尔字符串也能解析", _panel_settings.show_thought is True)
check(
    "过程消息回显默认开启，也能按字符串关掉",
    _default_settings.echo_events is True
    and runner_mod.RunnerSettings.from_config({"echo_events": "no"}, None).echo_events
    is False,
    str(runner_mod.RunnerSettings.from_config({"echo_events": "no"}, None)),
)
check(
    "空字符串算「没配」",
    runner_mod.RunnerSettings.from_config(
        {"acp_url": "   "}, {"acp_url": "ws://plugin/acp"}
    ).url
    == "ws://plugin/acp",
)
check(
    "非法数值回退默认",
    runner_mod.RunnerSettings.from_config(
        {"idle_timeout_sec": "abc", "turn_timeout_sec": None}, None
    ).idle_timeout_sec
    == runner_mod.DEFAULT_IDLE_TIMEOUT_SEC,
)
check(
    "非字典配置不炸",
    runner_mod.RunnerSettings.from_config("oops", 42).url == runner_mod.DEFAULT_ACP_URL,
)
check(
    "面板配置从全局配置里取（agent_runner.config）",
    runner_mod.panel_config_of(
        {
            "agent_runner": {
                "runner_type": runner_mod.RUNNER_TYPE,
                "config": {"acp_url": "ws://127.0.0.1:7433/acp?key=a"},
            }
        }
    )
    == {"acp_url": "ws://127.0.0.1:7433/acp?key=a"},
)
check(
    "面板配置结构不认识时返回 None（回落插件配置）",
    runner_mod.panel_config_of(None) is None
    and runner_mod.panel_config_of("oops") is None
    and runner_mod.panel_config_of({}) is None
    and runner_mod.panel_config_of({"agent_runner": {}}) is None
    and runner_mod.panel_config_of({"agent_runner": {"config": []}}) is None,
)
check(
    "命令路径也认面板里的地址（插件配置里那份缺 key 的不生效）",
    runner_mod.RunnerSettings.from_config(
        runner_mod.panel_config_of(
            {"agent_runner": {"config": {"acp_url": "ws://127.0.0.1:7433/acp?key=a"}}}
        ),
        {"acp_url": "ws://127.0.0.1:7433/acp"},
    ).url
    == "ws://127.0.0.1:7433/acp?key=a",
)
check(
    "状态里的地址会打掉凭证",
    own_main.mask_url("ws://h/acp?key=abc") == "ws://h/acp?key=***"
    and own_main.mask_url("ws://h/acp?key=abc&x=1") == "ws://h/acp?key=***&x=1"
    and own_main.mask_url("ws://h/acp") == "ws://h/acp"
    and own_main.mask_url("") == "",
    own_main.mask_url("ws://h/acp?key=abc&x=1"),
)


class _PanelContext:
    """只为读全局配置而造的假 context（真 context 要跑起整个内核才有）。"""

    def __init__(self, config):
        self._config = config

    def get_config(self):
        return self._config


_panel_plugin = Plugin0528V2(
    context=_PanelContext(
        {
            "agent_runner": {
                "runner_type": runner_mod.RUNNER_TYPE,
                "config": {
                    "acp_url": "ws://127.0.0.1:7433/acp?key=supersecret",
                    "idle_timeout_sec": 120,
                },
            }
        }
    ),
    config={"acp_url": "ws://127.0.0.1:7433/acp", "idle_timeout_sec": 1800},
)
_panel_settings = _panel_plugin._settings()
check(
    "命令路径（/0528v2 等）用面板里的地址与阈值",
    _panel_settings.url == "ws://127.0.0.1:7433/acp?key=supersecret"
    and _panel_settings.idle_timeout_sec == 120,
    str(_panel_settings),
)
_panel_status = _panel_plugin._render_status()
check(
    "状态文本显示面板里的地址",
    "ws://127.0.0.1:7433/acp" in _panel_status,
    _panel_status,
)
check(
    "状态文本里看不到 key（发到群里也不泄露）",
    "supersecret" not in _panel_status and "key=***" in _panel_status,
    _panel_status,
)
check(
    "拿不到全局配置时回落插件配置",
    Plugin0528V2(context=None, config={"acp_url": "ws://plugin/acp"})._settings().url
    == "ws://plugin/acp",
)

# ── 工作目录：模板根 / 相对路径校验 / 实际目录解析 ──
check(
    "workspace_root：取第一个占位符之前的目录",
    runner_mod.workspace_root("/root/alkaid0/{groupid}") == "/root/alkaid0"
    and runner_mod.workspace_root("/srv/{platform}/{groupid}") == "/srv"
    and runner_mod.workspace_root("/srv/fixed") == "/srv"
    and runner_mod.workspace_root("{groupid}") == ""
    and runner_mod.workspace_root("") == "/root/alkaid0",
    f"{runner_mod.workspace_root('/root/alkaid0/{groupid}')} / "
    f"{runner_mod.workspace_root('/srv/fixed')} / {runner_mod.workspace_root('')}",
)
check(
    "validate_rel_path：合法输入规范化",
    runner_mod.validate_rel_path("a/b") == "a/b"
    and runner_mod.validate_rel_path("  a//b/./c  ") == "a/b/c"
    and runner_mod.validate_rel_path("a\\b") == "a/b"
    and runner_mod.validate_rel_path(".") == "."
    and runner_mod.validate_rel_path("...") == "...",
    str(runner_mod.validate_rel_path("  a//b/./c  ")),
)
check(
    "validate_rel_path：绝对路径 / 盘符 / .. 一律拒绝",
    runner_mod.validate_rel_path("/etc") is None
    and runner_mod.validate_rel_path("\\a") is None
    and runner_mod.validate_rel_path("C:/x") is None
    and runner_mod.validate_rel_path("a/../b") is None
    and runner_mod.validate_rel_path("") is None,
)
check(
    "resolve_cwd：未绑定按模板、绑定用模板根",
    runner_mod.resolve_cwd("/root/alkaid0/{groupid}", None, group_event)
    == "/root/alkaid0/987654"
    and runner_mod.resolve_cwd("/root/alkaid0/{groupid}", "work", group_event)
    == "/root/alkaid0/work"
    and runner_mod.resolve_cwd("/root/alkaid0/{groupid}", ".", group_event)
    == "/root/alkaid0"
    and runner_mod.resolve_cwd("/srv/{platform}/{groupid}", "a/b", group_event)
    == "/srv/a/b",
    runner_mod.resolve_cwd("/root/alkaid0/{groupid}", "work", group_event),
)
check(
    "resolve_cwd：模板推不出根时忽略绑定",
    runner_mod.resolve_cwd("{groupid}", "work", group_event) == "987654",
    runner_mod.resolve_cwd("{groupid}", "work", group_event),
)

# ── 请求注入（provider_settings）──
_INJECT_CFG = {
    "prompt_prefix": "P:{{prompt}}",
    "identifier": True,
    "group_name_display": True,
}
check(
    "注入：前缀替换 + 每人一行 reminder + 群名",
    runner_mod.apply_prompt_settings(
        "在吗",
        _INJECT_CFG,
        senders=(("张三", "10001"), ("李四", "10002")),
        group_name="摸鱼群",
    )
    == "P:在吗\n[Reminder: @张三(10001)]\n[Reminder: @李四(10002)]\nGroup name: 摸鱼群",
)
check(
    "注入：没有 {{prompt}} 时前缀直接拼",
    runner_mod.apply_prompt_settings("hi", {"prompt_prefix": "[群聊] "}) == "[群聊] hi",
)
check(
    "注入：开关关闭时不加任何东西",
    runner_mod.apply_prompt_settings(
        "在吗",
        {"identifier": False, "group_name_display": False},
        senders=(("张三", "1"),),
    )
    == "在吗",
)
check(
    "注入：命令形式的提示词原样返回",
    runner_mod.apply_prompt_settings(
        "/compress", _INJECT_CFG, senders=(("张三", "1"),), group_name="群"
    )
    == "/compress",
)
check(
    "注入：非映射配置原样返回",
    runner_mod.apply_prompt_settings("在吗", None) == "在吗"
    and runner_mod.apply_prompt_settings("在吗", "oops") == "在吗",
)
check(
    "注入：布尔字符串开关也能认",
    runner_mod.apply_prompt_settings(
        "在吗", {"identifier": "true"}, senders=(("张三", "1"),)
    )
    == "在吗\n[Reminder: @张三(1)]",
)
check(
    "群名：优先消息自带的，N/A 时走 get_group 兜底",
    asyncio.run(runner_mod.resolve_group_name(FakeEvent(group_name="自带的"))) == "自带的"
    and asyncio.run(
        runner_mod.resolve_group_name(
            FakeEvent(group_id="123", group_name="N/A", group_api_name="查到的")
        )
    )
    == "查到的"
    and asyncio.run(runner_mod.resolve_group_name(FakeEvent())) == "",
)
check(
    "provider_settings 读不到时返回空字典（注入不炸）",
    isinstance(runner_mod.read_provider_settings(), dict),
)

# --------------------------------------------------- 12. 工具审批（挂起 + /aapprove）
_defer_handler = runner_mod.build_permission_handler(
    SimpleNamespace(key="group:a"), label="group:a"
)
_deferred = asyncio.run(
    _defer_handler(
        {
            "title": "Approve tool call: edit",
            "options": [{"optionId": "allow_once"}, {"optionId": "reject_once"}],
        }
    )
)
check(
    "审批请求先挂起（不自动批准、也不主动拒绝）",
    _deferred is DEFERRED,
    repr(_deferred),
)
check(
    "审批提示：有挂起请求时带上工具名与 /aapprove 用法",
    runner_mod.permission_hint(
        SimpleNamespace(
            client=SimpleNamespace(pending_permission=lambda: (7, {"title": "edit"}))
        )
    )
    == "⏳ 工具审批：edit（回复 /aapprove 允许）",
)
check(
    "审批提示：没挂起 / 没标题 / 假客户端都退化成通用提示",
    runner_mod.permission_hint(SimpleNamespace(client=SimpleNamespace()))
    == "⏳ 工具审批等待中（回复 /aapprove 允许）"
    and runner_mod.permission_hint(
        SimpleNamespace(client=SimpleNamespace(pending_permission=lambda: (7, {})))
    )
    == "⏳ 工具审批等待中（回复 /aapprove 允许）"
    and runner_mod.permission_hint(object())
    == "⏳ 工具审批等待中（回复 /aapprove 允许）",
)

# ACPClient 的「挂起 → /aapprove 回包」往返（不联网，直接调分发逻辑）
_probe_client = pool_mod.default_client_factory("ws://stub/acp", "probe")
_probe_responses = []


async def _capture_reply(payload):
    _probe_responses.append(payload)


_probe_client._reply = _capture_reply
_probe_client.set_permission_handler(lambda params: DEFERRED)


async def _permission_roundtrip():
    await _probe_client._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session/request_permission",
            "params": {"title": "edit", "options": [{"optionId": "allow_once"}]},
        }
    )
    before = _probe_client.pending_permission()
    replied = await _probe_client.reply_request(
        5, {"outcome": "selected", "optionId": "allow_once"}
    )
    after = _probe_client.pending_permission()
    stale = await _probe_client.reply_request(999, {"outcome": "selected"})
    return before, replied, after, stale


_before, _replied, _after, _stale = asyncio.run(_permission_roundtrip())
check(
    "ACPClient：审批请求挂起不回包，/aapprove 再回 allow_once",
    _before == (5, {"title": "edit", "options": [{"optionId": "allow_once"}]})
    and _replied is True
    and _after is None
    and _stale is False
    and _probe_responses
    == [
        {
            "jsonrpc": "2.0",
            "id": 5,
            "result": {"outcome": "selected", "optionId": "allow_once"},
        }
    ],
    f"{_before} / {_probe_responses}",
)

# --------------------------------------------------- 13. 群连接池 + Runner（假客户端）
def _session_config_options(current="0/a", effort="unset"):
    """假 alkaid0 的 ``configOptions``（模型 + 推理强度，形状同真机）。"""
    return [
        {
            "configId": "model",
            "name": "Model",
            "category": "model",
            "type": "id",
            "currentValue": current,
            "options": [
                {"value": "0/a", "name": "gpt-5"},
                {"value": "1/b", "name": "claude-x"},
                {"value": "2/c", "name": "echo-flash"},
            ],
        },
        {
            "configId": "thought_level",
            "name": "Thought Level",
            "category": "thought_level",
            "type": "id",
            "currentValue": effort,
            "options": [
                {"value": "unset", "name": "Unset"},
                {"value": "low", "name": "Low"},
                {"value": "medium", "name": "Medium"},
                {"value": "high", "name": "High"},
                {"value": "max", "name": "Max"},
                {"value": "xhigh", "name": "XHigh"},
            ],
        },
    ]


class FakeClient:
    """假 ACP 客户端：记录调用、按脚本回放通知，不碰网络。"""

    def __init__(
        self,
        url,
        name,
        *,
        fail_connect=False,
        updates=None,
        drop_after_prompt=False,
        fail_resume=False,
        config_options=None,
        sessions=None,
        commands=None,
        capabilities=None,
        fail_prompt=None,
        fail_cancel=None,
        existing_cwds=None,
    ):
        self.url = url
        self.name = name
        self.fail_connect = fail_connect
        self.fail_resume = fail_resume
        self.updates = list(updates or [])
        self.drop_after_prompt = drop_after_prompt
        self.calls = []
        self.connected = False
        self.closed = False
        self.permission_handler = None
        #: 模拟客户端挂起的服务端请求（/aapprove 用）与已经回出去的响应。
        self.deferred_requests = {}
        self.replied_requests = []
        self.resume_replay = "未调用"
        self._handlers = []
        #: 非空时只认这些工作目录（别的 ``session/new`` 报 ``cwd not found``）——
        #: 用来模拟「模板渲染出来的目录还没建」；``fs/mkdir`` 会把新目录记进去。
        self.existing_cwds = set(existing_cwds) if existing_cwds is not None else None
        #: 会话 ID → 它的 cwd（``fs/mkdir`` 要知道相对谁建目录）。
        self._session_cwds = {}
        #: ``initialize`` 的响应（真机里 capabilities 带着 alkaid0 扩展能力标记）。
        self.server_info = {
            "protocolVersion": 2,
            "capabilities": json.loads(json.dumps(capabilities or {})),
        }
        #: 非空时 ``session/prompt`` 直接报错（模拟服务端不认这条命令）。
        self.fail_prompt = fail_prompt
        #: 非空时 ``session/cancel`` 直接报错（模拟连接刚断、发不出去）。
        self.fail_cancel = fail_cancel
        #: 服务端在开会话 / 续接时广播的命令列表（形状同真机的 availableCommands）。
        self.commands = json.loads(json.dumps(commands or []))
        #: 会话级配置项（模型列表）；开会话 / 续接时原样返回。
        self.config_options = json.loads(json.dumps(config_options or []))
        #: 非空时 ``session/set_config_option`` 直接报错（模拟服务端拒绝）。
        self.set_config_error = None
        #: ``session/list`` 返回的会话（形状同真机：sessionId / cwd / title / updatedAt）。
        self.sessions = json.loads(json.dumps(sessions or []))
        #: ``session/list`` 的翻页游标（真机里有更早的会话时才有）。
        self.session_list_cursor = ""
        #: 非空时 ``session/list`` 直接报错（模拟「目录没初始化」等）。
        self.session_list_error = None

    async def connect(self):
        if self.fail_connect:
            raise ACPError(f"连不上 {self.url}")
        self.connected = True

    async def initialize(self, timeout=None):
        self.calls.append(("initialize", {}))
        return self.server_info

    def _broadcast(self, session_id, update):
        """把一条 ``session/update`` 推给所有订阅者（模拟服务端广播）。"""
        for handler in list(self._handlers):
            handler("session/update", {"sessionId": session_id, "update": update})

    def _broadcast_commands(self, session_id):
        """开会话 / 续接时广播命令列表（真机正是在这一步发 available_commands_update）。"""
        if not self.commands:
            return
        self._broadcast(
            session_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": json.loads(json.dumps(self.commands)),
            },
        )

    async def session_new(self, cwd, timeout=None):
        self.calls.append(("session/new", {"cwd": cwd}))
        if self.existing_cwds is not None and cwd not in self.existing_cwds:
            raise ACPError(f"{self.name}：cwd not found: {cwd}")
        session_id = f"sess_{self.name}:{cwd}"
        self._session_cwds[session_id] = cwd
        self._broadcast_commands(session_id)
        return {
            "sessionId": session_id,
            "configOptions": json.loads(json.dumps(self.config_options)),
        }

    async def session_resume(self, session_id, cwd, *, replay_from=None, timeout=None):
        self.calls.append(("session/resume", {"sessionId": session_id, "cwd": cwd}))
        self.resume_replay = replay_from
        if self.fail_resume:
            raise ACPError(f"{self.name}：续接 {session_id} 失败")
        self._broadcast_commands(session_id)
        return {"configOptions": json.loads(json.dumps(self.config_options))}

    async def session_list(self, cwd, *, cursor=None, timeout=None):
        self.calls.append(("session/list", {"cwd": cwd}))
        if self.session_list_error:
            raise ACPError(self.session_list_error)
        payload = {"sessions": json.loads(json.dumps(self.sessions))}
        if self.session_list_cursor:
            payload["nextCursor"] = self.session_list_cursor
        return payload

    async def session_set_config_option(
        self, session_id, config_id, value, *, type="id", timeout=None
    ):
        """模拟会话级配置项改写：记调用、可报错、把 currentValue 改到位再返回。"""
        self.calls.append(
            (
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": config_id,
                    "type": type,
                    "value": value,
                },
            )
        )
        if self.set_config_error:
            raise ACPError(self.set_config_error)
        options = json.loads(json.dumps(self.config_options))
        for option in options:
            if isinstance(option, dict) and option.get("configId") == config_id:
                option["currentValue"] = value
        return {"configOptions": options}

    async def session_prompt(self, session_id, text, timeout=None):
        self.calls.append(("session/prompt", {"sessionId": session_id, "text": text}))
        if self.fail_prompt:
            # 真机里不认这条命令时：先广播 refusal 的 idle，再回这个 JSON-RPC 错误
            raise ACPError(self.fail_prompt)
        for update in self.updates:
            self._broadcast(session_id, update)
        if self.drop_after_prompt:
            # 模拟服务端重启 / 网络掉线：提示词发出后连接就没了，不会再推 idle
            self.connected = False
        return {}

    async def session_cancel(self, session_id):
        """中止当前轮：真机里是**通知**（发出去就不管了），这里只记调用。"""
        self.calls.append(("session/cancel", {"sessionId": session_id}))
        if self.fail_cancel:
            raise ACPError(self.fail_cancel)
        return {}

    async def session_close(self, session_id, timeout=None):
        self.calls.append(("session/close", {"sessionId": session_id}))
        return {}

    async def fs_mkdir(self, session_id, path, timeout=None):
        """模拟 alkaid0 的 ``fs/mkdir`` 扩展：记调用，并把新目录记进「已存在」。"""
        self.calls.append(("fs/mkdir", {"sessionId": session_id, "path": path}))
        base = self._session_cwds.get(session_id, "")
        if self.existing_cwds is not None:
            self.existing_cwds.add(f"{base}/{path}" if base else path)
        return {}

    def set_permission_handler(self, handler):
        self.permission_handler = handler

    def pending_permission(self):
        """模拟 ACPClient.pending_permission：找挂着的审批请求。"""
        for request_id, info in self.deferred_requests.items():
            if info.get("method") == "session/request_permission":
                return request_id, dict(info.get("params") or {})
        return None

    async def reply_request(self, request_id, result):
        """模拟 ACPClient.reply_request：回包并记下来。"""
        if self.deferred_requests.pop(request_id, None) is None:
            return False
        self.replied_requests.append((request_id, result))
        return True

    def clear_deferred(self):
        """模拟新一轮 prompt 前清掉挂起的审批记录（不回包）。"""
        self.deferred_requests.clear()

    async def close(self):
        self.closed = True
        self.connected = False

    def subscribe(self, handler):
        self._handlers.append(handler)

        def _unsubscribe():
            if handler in self._handlers:
                self._handlers.remove(handler)

        return _unsubscribe


def _chain_text(chain):
    """把消息链里的纯文本拼起来（断言用）。"""
    return "".join(getattr(component, "text", "") for component in (chain.chain or []))


async def alkaid0_checks():
    """连接池与 Runner 的全部检查（共用一个事件循环）。"""
    created = []

    def factory(url, name):
        client = FakeClient(url, name)
        created.append(client)
        return client

    pool = pool_mod.SessionPool(client_factory=factory, idle_timeout=300)
    first = await pool.acquire("group:a", url="ws://acp", cwd="/w/a")
    again = await pool.acquire("group:a", url="ws://acp", cwd="/w/a")
    second = await pool.acquire("group:b", url="ws://acp", cwd="/w/b")
    third = await pool.acquire("group:c", url="ws://acp", cwd="/w/c")

    check(
        "一个群一条连接、一个活跃会话",
        first is again
        and first.session_id == "sess_group:a:/w/a"
        and len(pool) == 3
        and len(created) == 3,
        f"len(pool)={len(pool)} len(created)={len(created)}",
    )
    check(
        "每条连接都做了握手 + session/new",
        created[0].calls[:2]
        == [("initialize", {}), ("session/new", {"cwd": "/w/a"})],
        str(created[0].calls),
    )
    check(
        "重复取用不会重复握手 / 开会话",
        len([call for call in created[1].calls if call[0] == "initialize"]) == 1
        and len([call for call in created[1].calls if call[0] == "session/new"]) == 1,
        str(created[1].calls),
    )
    check(
        "快照列出每个群",
        {item["key"] for item in pool.snapshot()} == {"group:a", "group:b", "group:c"},
        str(pool.snapshot()),
    )

    rebuilt_cwd = await pool.acquire("group:b", url="ws://acp", cwd="/w/b2")
    check(
        "工作目录变了就换连接",
        rebuilt_cwd.client is not second.client
        and second.client.closed
        and len(pool) == 3,
        f"closed={second.client.closed} len(pool)={len(pool)}",
    )
    rebuilt_url = await pool.acquire("group:c", url="ws://other/acp", cwd="/w/c")
    check(
        "地址变了也换连接",
        rebuilt_url.client is not third.client and third.client.closed,
    )

    # 空闲回收：到点了就回收，回收时先关会话
    pool.idle_timeout = 60
    first.last_active -= 120
    check("空闲超时的连接被回收", await pool.reap_idle() == ["group:a"], str(pool.snapshot()))
    check("回收后连接确实断开", first.client.closed and first.connected is False)
    check(
        "回收前先关闭 ACP 会话",
        ("session/close", {"sessionId": "sess_group:a:/w/a"}) in first.client.calls,
        str(first.client.calls),
    )

    # 忙的连接不回收，闲下来再收
    busy_target = pool.peek("group:b")
    busy_target.last_active -= 600
    async with busy_target.lock:
        reaped_while_busy = await pool.reap_idle()
    check("正在跑对话的连接不会被回收", reaped_while_busy == [], str(reaped_while_busy))
    check("闲下来之后照常回收", await pool.reap_idle() == ["group:b"])

    check(
        "空闲阈值 <=0 时不回收",
        await _no_reap_when_disabled(pool),
    )

    # 建连失败不留垃圾
    failing_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name, fail_connect=True),
        idle_timeout=60,
    )
    failure = None
    try:
        await failing_pool.acquire("group:x", url="ws://acp", cwd="/w/x")
    except ACPError as exc:
        failure = str(exc)
    check("建连失败会抛错", bool(failure and "连不上" in failure), str(failure))
    check("建连失败的连接不留池子里", len(failing_pool) == 0, str(failing_pool.snapshot()))

    # 回收任务开关
    pool.start_reaper()
    check("回收任务已启动", pool.reaper_running)
    await pool.stop_reaper()
    check("回收任务可停止", not pool.reaper_running)
    check("close_all 断开全部连接", await pool.close_all() == 1, str(pool.snapshot()))

    # ── 会话记录（SQLite）──────────────────────────────
    db_dir = Path(tempfile.mkdtemp(prefix="0528v2-store-"))
    db_path = db_dir / "sessions.db"
    store = store_mod.SessionStore(db_path)
    check("会话记录：空库读不到东西", store.get("group:r") is None and store.count() == 0)
    store.remember("group:r", "sess_7:/w/r", "/w/r")
    check(
        "会话记录：写入后可读回",
        store.get("group:r") == "sess_7:/w/r" and store.count() == 1,
    )
    store.remember("group:r", "sess_8:/w/r", "/w/r")
    check(
        "会话记录：同键覆盖且不新增行",
        store.get("group:r") == "sess_8:/w/r" and store.count() == 1,
    )
    check(
        "会话记录：entries 带 cwd",
        [entry for entry in store.entries() if entry["key"] == "group:r"][0]["cwd"] == "/w/r",
        str(store.entries()),
    )
    check(
        "会话记录：落盘了（新实例读得到）",
        store_mod.SessionStore(db_path).get("group:r") == "sess_8:/w/r",
    )
    check(
        "会话记录：forget 会删掉",
        store.forget("group:r") and store.get("group:r") is None,
    )
    check("会话记录：forget 不存在的键返回 False", store.forget("group:r") is False)
    check(
        "会话记录：路径不可用时降级为「没有记录」",
        store_mod.SessionStore("/proc/1/nope/sessions.db").get("x") is None,
    )

    # 有记录 → session/resume（不带 replayFrom）续接
    record_store = store_mod.SessionStore(db_dir / "resume.db")
    record_store.remember("group:keep", "sess_42:/w/keep", "/w/keep")
    keep_created = []

    def keep_factory(url, name):
        client = FakeClient(url, name)
        keep_created.append(client)
        return client

    keep_pool = pool_mod.SessionPool(
        client_factory=keep_factory, idle_timeout=300, store=record_store
    )
    kept = await keep_pool.acquire("group:keep", url="ws://acp", cwd="/w/keep")
    check(
        "有记录时用记录里的会话",
        kept.session_id == "sess_42:/w/keep"
        and kept.session_id == record_store.get("group:keep"),
        str(keep_created[0].calls),
    )
    check(
        "续接参数带 sessionId + cwd",
        ("session/resume", {"sessionId": "sess_42:/w/keep", "cwd": "/w/keep"})
        in keep_created[0].calls,
        str(keep_created[0].calls),
    )
    check("续接不带 replayFrom（不回放历史）", keep_created[0].resume_replay is None)
    check(
        "有记录时不新开会话",
        not [call for call in keep_created[0].calls if call[0] == "session/new"],
        str(keep_created[0].calls),
    )

    await keep_pool.release("group:keep", reason="测试回收")
    kept_again = await keep_pool.acquire("group:keep", url="ws://acp", cwd="/w/keep")
    check(
        "回收后重连仍续接同一个会话",
        kept_again.session_id == "sess_42:/w/keep"
        and len(keep_created) == 2
        and ("session/resume", {"sessionId": "sess_42:/w/keep", "cwd": "/w/keep"})
        in keep_created[1].calls
        and not [call for call in keep_created[1].calls if call[0] == "session/new"],
        str(keep_created[1].calls),
    )

    # 续接失败（会话没了 / cwd 不匹配）→ 新开 + 更新记录
    stale_store = store_mod.SessionStore(db_dir / "stale.db")
    stale_store.remember("group:stale", "sess_gone:/w/stale", "/w/stale")
    stale_created = []

    def stale_factory(url, name):
        client = FakeClient(url, name, fail_resume=True)
        stale_created.append(client)
        return client

    stale_pool = pool_mod.SessionPool(
        client_factory=stale_factory, idle_timeout=300, store=stale_store
    )
    stale = await stale_pool.acquire("group:stale", url="ws://acp", cwd="/w/stale")
    check(
        "续接失败时改为新开会话",
        stale.session_id == "sess_group:stale:/w/stale"
        and ("session/new", {"cwd": "/w/stale"}) in stale_created[0].calls,
        str(stale_created[0].calls),
    )
    check(
        "续接失败后记录更新为新会话",
        stale_store.get("group:stale") == stale.session_id,
        str(stale_store.entries()),
    )

    # 没有记录 → 新开 + 记账
    fresh_store = store_mod.SessionStore(db_dir / "fresh.db")
    fresh_created = []

    def fresh_factory(url, name):
        client = FakeClient(url, name)
        fresh_created.append(client)
        return client

    fresh_pool = pool_mod.SessionPool(
        client_factory=fresh_factory, idle_timeout=300, store=fresh_store
    )
    fresh = await fresh_pool.acquire("group:fresh", url="ws://acp", cwd="/w/fresh")
    check(
        "没有记录时新开会话并记账",
        fresh_store.get("group:fresh") == fresh.session_id
        and ("session/new", {"cwd": "/w/fresh"}) in fresh_created[0].calls,
        str(fresh_created[0].calls),
    )

    # reset_session（/areset 的底层）
    check(
        "reset_session 抹掉记录并断开连接",
        await fresh_pool.reset_session("group:fresh")
        and fresh_store.get("group:fresh") is None
        and fresh_pool.peek("group:fresh") is None
        and fresh_created[0].closed,
    )
    after_reset = await fresh_pool.acquire("group:fresh", url="ws://acp", cwd="/w/fresh")
    check(
        "重置后下次重新开会话",
        after_reset.session_id != fresh.session_id
        and ("session/new", {"cwd": "/w/fresh"}) in fresh_created[1].calls
        and not [call for call in fresh_created[1].calls if call[0] == "session/resume"],
        str(fresh_created[1].calls),
    )
    check(
        "重置不存在的键也不炸",
        await pool_mod.SessionPool(
            client_factory=fresh_factory, idle_timeout=60, store=fresh_store
        ).reset_session("nobody")
        is False,
    )

    # ── /areset 命令（直接调插件方法）──────────────────
    cmd_store = store_mod.SessionStore(db_dir / "cmd.db")
    cmd_created = []

    def cmd_factory(url, name):
        client = FakeClient(url, name)
        cmd_created.append(client)
        return client

    cmd_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
    cmd_plugin._pool = pool_mod.SessionPool(
        client_factory=cmd_factory, idle_timeout=300, store=cmd_store
    )
    cmd_event = FakeCommandEvent("aiocqhttp:group:888")
    await cmd_plugin._pool.acquire(
        cmd_event.unified_msg_origin, url="ws://acp", cwd="/w/888"
    )
    replies = [chunk async for chunk in cmd_plugin.reset_session(cmd_event)]
    joined = "\n".join(cmd_event.replies)
    check(
        "/areset 回报清除了记录与连接",
        len(replies) == 1
        and cmd_store.get("aiocqhttp:group:888") is None
        and cmd_plugin._pool.peek("aiocqhttp:group:888") is None
        and "已清除本群记录的会话" in joined
        and "已断开该群的连接" in joined,
        joined,
    )
    check("状态文本报出会话记录条数", "会话记录" in cmd_plugin._render_status())

    no_pool_event = FakeCommandEvent("aiocqhttp:group:999")
    no_pool_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
    [chunk async for chunk in no_pool_plugin.reset_session(no_pool_event)]
    check(
        "连接池没起来时 /areset 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_event.replies),
        "\n".join(no_pool_event.replies),
    )

    # ── /astop 命令（中止当前轮）───────────────────────
    stop_event = FakeCommandEvent("aiocqhttp:group:888")
    stop_session = await cmd_plugin._pool.acquire(
        stop_event.unified_msg_origin, url="ws://acp", cwd="/w/888"
    )
    stop_client = cmd_created[-1]

    stop_event.replies.clear()
    [chunk async for chunk in cmd_plugin.stop_turn(stop_event)]
    check(
        "/astop 没在跑时只提示、不发 cancel",
        "没有在跑的 Alkaid0 轮次" in "\n".join(stop_event.replies)
        and not [call for call in stop_client.calls if call[0] == "session/cancel"],
        f"{stop_event.replies} / {stop_client.calls}",
    )

    stop_event.replies.clear()
    async with stop_session.lock:
        stop_replies = [chunk async for chunk in cmd_plugin.stop_turn(stop_event)]
    check(
        "/astop 中止正在跑的那一轮（发 session/cancel，带会话 ID）",
        ("session/cancel", {"sessionId": stop_session.session_id})
        in stop_client.calls
        and len(stop_replies) == 1
        and "已请求 Alkaid0 中止本群当前这一轮" in "\n".join(stop_event.replies),
        f"{stop_event.replies} / {stop_client.calls}",
    )
    check(
        "/astop 讲清中止的范围：已说出的内容照发、队列不在这条命令里",
        "这一轮已经说出来的内容还会发到群里" in "\n".join(stop_event.replies),
        "\n".join(stop_event.replies),
    )

    stop_event.replies.clear()
    stop_client.fail_cancel = "连接刚断了"
    async with stop_session.lock:
        [chunk async for chunk in cmd_plugin.stop_turn(stop_event)]
    check(
        "/astop 发不出去时如实上报",
        "发送中止请求失败：连接刚断了" in "\n".join(stop_event.replies),
        "\n".join(stop_event.replies),
    )
    stop_client.fail_cancel = None

    no_pool_stop_event = FakeCommandEvent("aiocqhttp:group:999")
    no_pool_stop_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    [chunk async for chunk in no_pool_stop_plugin.stop_turn(no_pool_stop_event)]
    check(
        "连接池没起来时 /astop 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_stop_event.replies),
        "\n".join(no_pool_stop_event.replies),
    )

    # 回执里点名排队的插话（它们不属于这条命令中止的范围）
    queued_stop_event = FakeCommandEvent("aiocqhttp:group:888")
    cmd_plugin._turn_queue = turn_queue_mod.TurnQueue()
    await cmd_plugin._turn_queue.enter(queued_stop_event.unified_msg_origin, "跑着呢")
    queued_stop_task = asyncio.create_task(
        cmd_plugin._turn_queue.enter(queued_stop_event.unified_msg_origin, "排着")
    )
    await asyncio.sleep(0.01)
    async with stop_session.lock:
        [chunk async for chunk in cmd_plugin.stop_turn(queued_stop_event)]
    check(
        "/astop 回执里点名排队的插话（不在中止范围内）",
        "还排着 1 条插话" in "\n".join(queued_stop_event.replies),
        "\n".join(queued_stop_event.replies),
    )
    queued_stop_task.cancel()
    try:
        await queued_stop_task
    except asyncio.CancelledError:
        pass
    await cmd_plugin._turn_queue.leave(queued_stop_event.unified_msg_origin)
    cmd_plugin._turn_queue = None

    # ── /aapprove 命令（放行挂起的审批）─────────────────
    approve_event = FakeCommandEvent("aiocqhttp:group:888")
    approve_session = cmd_plugin._pool.peek(approve_event.unified_msg_origin)
    approve_client = approve_session.client

    approve_event.replies.clear()
    [chunk async for chunk in cmd_plugin.approve_tool(approve_event)]
    check(
        "没有挂起的审批时 /aapprove 只提示",
        "没有等待审批的工具调用" in "\n".join(approve_event.replies),
        "\n".join(approve_event.replies),
    )

    approve_client.deferred_requests = {
        77: {
            "method": "session/request_permission",
            "params": {
                "title": "edit",
                "options": [{"optionId": "reject_once"}, {"optionId": "allow_once"}],
            },
        }
    }
    approve_event.replies.clear()
    [chunk async for chunk in cmd_plugin.approve_tool(approve_event)]
    check(
        "/aapprove 回 allow_once 并清掉挂起记录",
        approve_client.replied_requests
        == [(77, {"outcome": "selected", "optionId": "allow_once"})]
        and approve_client.deferred_requests == {}
        and "已允许：edit" in "\n".join(approve_event.replies),
        f"{approve_client.replied_requests} / {approve_event.replies}",
    )

    approve_client.replied_requests.clear()
    approve_client.deferred_requests = {
        78: {
            "method": "session/request_permission",
            "params": {"title": "no-option", "options": []},
        }
    }
    approve_event.replies.clear()
    [chunk async for chunk in cmd_plugin.approve_tool(approve_event)]
    check(
        "/aapprove 没有可选项时如实说明",
        "没有可选的放行选项" in "\n".join(approve_event.replies)
        and approve_client.replied_requests == [],
        f"{approve_client.replied_requests} / {approve_event.replies}",
    )

    no_pool_approve_event = FakeCommandEvent("aiocqhttp:group:999")
    no_pool_approve_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    [chunk async for chunk in no_pool_approve_plugin.approve_tool(no_pool_approve_event)]
    check(
        "连接池没起来时 /aapprove 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_approve_event.replies),
        "\n".join(no_pool_approve_event.replies),
    )

    # ── /actx 命令（看上下文用量）───────────────────────
    ctx_created = []

    def ctx_factory(url, name):
        client = FakeClient(url, name)
        ctx_created.append(client)
        return client

    ctx_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
    ctx_plugin._pool = pool_mod.SessionPool(client_factory=ctx_factory, idle_timeout=300)

    no_pool_ctx_event = FakeCommandEvent("aiocqhttp:group:695")
    no_pool_ctx_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    [chunk async for chunk in no_pool_ctx_plugin.context_usage(no_pool_ctx_event)]
    check(
        "连接池没起来时 /actx 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_ctx_event.replies),
        "\n".join(no_pool_ctx_event.replies),
    )

    idle_ctx_event = FakeCommandEvent("aiocqhttp:group:695")
    [chunk async for chunk in ctx_plugin.context_usage(idle_ctx_event)]
    check(
        "本群还没会话时 /actx 如实说（也不去建会话）",
        "还没有活跃的 Alkaid0 会话" in "\n".join(idle_ctx_event.replies)
        and ctx_created == [],
        f"{idle_ctx_event.replies} / {ctx_created}",
    )

    ctx_session = await ctx_plugin._pool.acquire(
        "aiocqhttp:group:695", url="ws://acp", cwd="/w/695"
    )
    fresh_ctx_event = FakeCommandEvent("aiocqhttp:group:695")
    [chunk async for chunk in ctx_plugin.context_usage(fresh_ctx_event)]
    check(
        "还没跑过一轮时 /actx 说明原因",
        "还没跑过一轮" in "\n".join(fresh_ctx_event.replies),
        "\n".join(fresh_ctx_event.replies),
    )

    ctx_created[0]._broadcast(
        ctx_session.session_id,
        {"sessionUpdate": "usage_update", "used": 11724, "size": 1024000},
    )
    used_ctx_event = FakeCommandEvent("aiocqhttp:group:695")
    ctx_replies = [chunk async for chunk in ctx_plugin.context_usage(used_ctx_event)]
    check(
        "/actx 报出 context x% (used/limit) 一行",
        len(ctx_replies) == 1
        and "\n".join(used_ctx_event.replies) == "context 1% (11724/1024000)",
        str(used_ctx_event.replies),
    )

    # ── 客户端把 session/cancel 发成通知（不是请求）────
    cancel_client = pool_mod.default_client_factory("ws://stub/acp", "stub")
    cancel_captured = {}

    async def _stub_notify(method, params=None):
        cancel_captured.update(method=method, params=params)

    cancel_client.notify = _stub_notify
    await cancel_client.session_cancel("sess_9:/w")
    check(
        "客户端把 session/cancel 包成通知（没有 id，也不等回包）",
        cancel_captured
        == {"method": "session/cancel", "params": {"sessionId": "sess_9:/w"}},
        str(cancel_captured),
    )

    # ── 客户端把 set_config_option 包成 ACP 请求 ────────
    stub_client = pool_mod.default_client_factory("ws://stub/acp", "stub")
    stub_captured = {}
    stub_options = _session_config_options("1/b")

    async def _stub_call(method, params=None, *, timeout=None):
        stub_captured.update(method=method, params=params, timeout=timeout)
        return {"configOptions": stub_options}

    stub_client.call = _stub_call
    stub_result = await stub_client.session_set_config_option("sess_9:/w", "model", "1/b")
    check(
        "客户端把 set_config_option 包成 ACP 请求",
        stub_captured
        == {
            "method": "session/set_config_option",
            "params": {
                "sessionId": "sess_9:/w",
                "configId": "model",
                "type": "id",
                "value": "1/b",
            },
            "timeout": None,
        }
        and stub_result == {"configOptions": stub_options},
        str(stub_captured),
    )

    # ── 客户端把 session_list 包成 ACP 请求 ────────────
    async def _stub_list_call(method, params=None, *, timeout=None):
        stub_captured.update(method=method, params=params, timeout=timeout)
        return {"sessions": [{"sessionId": "sess_1:/w"}], "nextCursor": "cur"}

    stub_client.call = _stub_list_call
    stub_list_result = await stub_client.session_list("/w/x", cursor="cur")
    check(
        "客户端把 session_list 包成 ACP 请求（带 cursor）",
        stub_captured
        == {
            "method": "session/list",
            "params": {"cwd": "/w/x", "cursor": "cur"},
            "timeout": None,
        }
        and stub_list_result["nextCursor"] == "cur",
        str(stub_captured),
    )
    await stub_client.session_list("/w/x")
    check(
        "session_list 不传 cursor 时省略该参数",
        stub_captured["params"] == {"cwd": "/w/x"},
        str(stub_captured),
    )

    # ── 会话级配置项：开会话时记下、能被广播刷新 ────────
    conf_created = []

    def conf_factory(url, name):
        client = FakeClient(url, name, config_options=_session_config_options("0/a"))
        conf_created.append(client)
        return client

    conf_pool = pool_mod.SessionPool(client_factory=conf_factory, idle_timeout=300)
    conf_session = await conf_pool.acquire("group:conf", url="ws://acp", cwd="/w/conf")
    check(
        "开会话时记下 configOptions（模型列表）",
        conf_session.current_model() == "0/a"
        and len(acp_proto.option_choices(conf_session.config_option("model"))) == 3,
        str(conf_session.config_options),
    )
    await conf_session.set_config_option("model", "1/b")
    check(
        "改配置项走 ACP 请求并刷新本地缓存",
        conf_created[0].calls[-1]
        == (
            "session/set_config_option",
            {
                "sessionId": "sess_group:conf:/w/conf",
                "configId": "model",
                "type": "id",
                "value": "1/b",
            },
        )
        and conf_session.current_model() == "1/b",
        f"{conf_created[0].calls[-1]} / {conf_session.current_model()}",
    )
    for _handler in list(conf_created[0]._handlers):
        _handler(
            "session/update",
            {
                "sessionId": "sess_group:conf:/w/conf",
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": _session_config_options("2/c"),
                },
            },
        )
    check(
        "config_option_update 广播会刷新本地缓存",
        conf_session.current_model() == "2/c",
        conf_session.current_model(),
    )
    check(
        "连接快照里带上当前模型",
        [item for item in conf_pool.snapshot() if item["key"] == "group:conf"][0]["model"]
        == "2/c",
        str(conf_pool.snapshot()),
    )

    # ── 命令列表：开会话时记下、能被广播刷新 ────────────
    cmds_created = []

    def cmds_factory(url, name):
        client = FakeClient(
            url,
            name,
            commands=_available_commands(),
            capabilities=ALKAID0_CAPABILITIES,
        )
        cmds_created.append(client)
        return client

    cmds_pool = pool_mod.SessionPool(client_factory=cmds_factory, idle_timeout=300)
    cmds_session = await cmds_pool.acquire("group:cmds", url="ws://acp", cwd="/w/cmds")
    check(
        "开会话时记下命令列表（监听挂在开会话之前才收得到）",
        [item["name"] for item in cmds_session.available_commands]
        == ["background", "compress", "weird"]
        and cmds_session.available_commands[1]["description"] == COMPRESS_DESCRIPTION,
        str(cmds_session.available_commands),
    )
    check(
        "服务端声明了 alkaid0 扩展能力（v0.4）",
        cmds_session.supports_alkaid0 is True,
        str(cmds_created[0].server_info),
    )
    cmds_created[0]._broadcast(
        "sess_group:cmds:/w/cmds",
        {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [
                {"name": "help", "description": "Show this help message"}
            ],
        },
    )
    check(
        "available_commands_update 广播会刷新本地缓存",
        [item["name"] for item in cmds_session.available_commands] == ["help"],
        str(cmds_session.available_commands),
    )
    cmds_created[0]._broadcast(
        "sess_group:cmds:/w/cmds",
        {"sessionUpdate": "available_commands_update", "availableCommands": []},
    )
    check(
        "空命令列表不会把已知的列表清掉",
        [item["name"] for item in cmds_session.available_commands] == ["help"],
        str(cmds_session.available_commands),
    )

    plain_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name), idle_timeout=300
    )
    plain_session = await plain_pool.acquire("group:plain", url="ws://acp", cwd="/w/plain")
    check(
        "服务端没声明扩展能力 / 还没下发命令时都是「空」",
        plain_session.supports_alkaid0 is False
        and plain_session.available_commands == [],
        f"{plain_session.supports_alkaid0} / {plain_session.available_commands}",
    )

    # ── 上下文用量：跟 usage_update 缓存，换 / 新开会话时清空 ──
    usage_created = []

    def usage_factory(url, name):
        client = FakeClient(url, name)
        usage_created.append(client)
        return client

    usage_pool = pool_mod.SessionPool(client_factory=usage_factory, idle_timeout=300)
    usage_session = await usage_pool.acquire("group:usage", url="ws://acp", cwd="/w/usage")
    check("开会话时还没有用量（服务端只在模型响应后推）", usage_session.usage is None)
    usage_created[0]._broadcast(
        "sess_group:usage:/w/usage",
        {"sessionUpdate": "usage_update", "used": 11724, "size": 1024000},
    )
    check(
        "usage_update 广播会记下最新一份",
        usage_session.usage == (11724, 1024000),
        str(usage_session.usage),
    )
    usage_created[0]._broadcast(
        "sess_group:usage:/w/usage",
        {"sessionUpdate": "usage_update", "used": 20000, "size": 1024000},
    )
    check(
        "一轮里推多条时留最新那份",
        usage_session.usage == (20000, 1024000),
        str(usage_session.usage),
    )
    usage_created[0]._broadcast(
        "sess_other:/w/x", {"sessionUpdate": "usage_update", "used": 1, "size": 2}
    )
    check(
        "别的会话的用量不会串进来",
        usage_session.usage == (20000, 1024000),
        str(usage_session.usage),
    )
    usage_created[0]._broadcast(
        "sess_group:usage:/w/usage", {"sessionUpdate": "usage_update", "used": "oops"}
    )
    check(
        "形状不对的用量不覆盖已有缓存",
        usage_session.usage == (20000, 1024000),
        str(usage_session.usage),
    )

    # 换会话：旧的用量不属于新会话
    await usage_session.switch_session("sess_9:/w/usage")
    check(
        "换会话后用量清空（新会话得重新跑一轮）",
        usage_session.session_id == "sess_9:/w/usage" and usage_session.usage is None,
        str(usage_session.usage),
    )

    # 重连：续接同一个会话时留着；记录失效回落 session/new 则清空
    reused_store = store_mod.SessionStore(db_dir / "usage-resume.db")
    reused_store.remember("group:ur", "sess_keep:/w/ur", "/w/ur")
    reused_created = []

    def reused_factory(url, name):
        client = FakeClient(url, name)
        reused_created.append(client)
        return client

    reused_pool = pool_mod.SessionPool(
        client_factory=reused_factory, idle_timeout=300, store=reused_store
    )
    reused_session = await reused_pool.acquire("group:ur", url="ws://acp", cwd="/w/ur")
    reused_created[0]._broadcast(
        "sess_keep:/w/ur", {"sessionUpdate": "usage_update", "used": 777, "size": 1000}
    )
    reused_created[0].connected = False
    await reused_session.ensure_session()
    check(
        "重连续接同一个会话：用量留着（还是那个会话）",
        reused_session.session_id == "sess_keep:/w/ur"
        and reused_session.usage == (777, 1000),
        f"{reused_session.session_id} / {reused_session.usage}",
    )

    gone_store = store_mod.SessionStore(db_dir / "usage-gone.db")
    gone_store.remember("group:ug", "sess_gone:/w/ug", "/w/ug")
    gone_created = []

    def gone_factory(url, name):
        client = FakeClient(url, name, fail_resume=True)
        gone_created.append(client)
        return client

    gone_pool = pool_mod.SessionPool(
        client_factory=gone_factory, idle_timeout=300, store=gone_store
    )
    gone_session = await gone_pool.acquire("group:ug", url="ws://acp", cwd="/w/ug")
    gone_created[0]._broadcast(
        gone_session.session_id,
        {"sessionUpdate": "usage_update", "used": 42, "size": 100},
    )
    check("先有一份用量", gone_session.usage == (42, 100), str(gone_session.usage))
    gone_created[0].connected = False
    renewed_session = await gone_session.ensure_session()
    check(
        "回落 session/new 后旧用量清空（新会话不认旧数字）",
        renewed_session == "sess_group:ug:/w/ug" and gone_session.usage is None,
        f"{renewed_session} / {gone_session.usage}",
    )

    # ── 换会话（/aresume 的底层）───────────────────────
    switch_store = store_mod.SessionStore(db_dir / "switch.db")
    switch_created = []

    def switch_factory(url, name):
        client = FakeClient(
            url,
            name,
            config_options=_session_config_options("1/b"),
            sessions=[
                {
                    "sessionId": "sess_7:/w/switch",
                    "cwd": "/w/switch",
                    "title": "旧会话",
                    "updatedAt": "2026-10-16T12:00:00Z",
                },
                {
                    "sessionId": "sess_9:/w/switch",
                    "cwd": "/w/switch",
                    "title": "更早的",
                    "updatedAt": "2026-10-01T03:04:05Z",
                },
            ],
        )
        switch_created.append(client)
        return client

    switch_pool = pool_mod.SessionPool(
        client_factory=switch_factory, idle_timeout=300, store=switch_store
    )
    switch_session = await switch_pool.acquire(
        "group:switch", url="ws://acp", cwd="/w/switch"
    )
    original_id = switch_session.session_id
    await switch_session.switch_session("sess_9:/w/switch")
    check(
        "换会话：关旧会话、续接新会话并记账",
        switch_session.session_id == "sess_9:/w/switch"
        and ("session/close", {"sessionId": original_id}) in switch_created[0].calls
        and ("session/resume", {"sessionId": "sess_9:/w/switch", "cwd": "/w/switch"})
        in switch_created[0].calls
        and switch_store.get("group:switch") == "sess_9:/w/switch",
        str(switch_created[0].calls),
    )
    check(
        "换会话时不带 replayFrom（不复述历史）",
        switch_created[0].resume_replay is None,
    )
    check(
        "换会话后配置项缓存跟着新会话走",
        switch_session.current_model() == "1/b",
        str(switch_session.config_options),
    )
    await switch_session.switch_session("sess_9:/w/switch")
    check(
        "切到当前会话是空操作（不重复续接）",
        len([call for call in switch_created[0].calls if call[0] == "session/resume"]) == 1,
        str(switch_created[0].calls),
    )

    # 换会话失败：如实抛错、记录不动（下次消息按记录里的旧会话重来）
    fail_switch_store = store_mod.SessionStore(db_dir / "switch-fail.db")
    fail_switch_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name, fail_resume=True),
        idle_timeout=300,
        store=fail_switch_store,
    )
    fail_switch_session = await fail_switch_pool.acquire(
        "group:sf", url="ws://acp", cwd="/w/sf"
    )
    switch_error = None
    try:
        await fail_switch_session.switch_session("sess_404:/w/sf")
    except ACPError as exc:
        switch_error = str(exc)
    check(
        "换会话失败：抛错、记录保持原样、内存里的会话置空",
        bool(switch_error and "续接" in switch_error)
        and fail_switch_store.get("group:sf") == "sess_group:sf:/w/sf"
        and fail_switch_session.session_id is None,
        f"{switch_error} / 记录 {fail_switch_store.get('group:sf')}",
    )

    # ── /amodel 命令（只改当前会话）────────────────────
    model_cmd_created = []

    def model_cmd_factory(url, name):
        client = FakeClient(url, name, config_options=_session_config_options("0/a"))
        model_cmd_created.append(client)
        return client

    model_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    model_plugin._pool = pool_mod.SessionPool(
        client_factory=model_cmd_factory, idle_timeout=300
    )

    list_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.model(list_event)]
    listed = "\n".join(list_event.replies)
    current_lines = [line for line in listed.splitlines() if "gpt-5" in line]
    check(
        "/amodel 列出可选模型并标出当前",
        "本群会话可用的模型（3 个）" in listed
        and len(current_lines) == 1
        and current_lines[0].rstrip().endswith("← 当前")
        and "claude-x" in listed
        and "echo-flash" in listed
        and "用法：/amodel" in listed,
        listed,
    )
    check(
        "/amodel 没会话时按「本群第一条消息」建会话（cwd 走模板）",
        model_plugin._pool.peek("aiocqhttp:group:661").cwd == "/w/661"
        and model_plugin._pool.peek("aiocqhttp:group:661").session_id
        == "sess_aiocqhttp:group:661:/w/661",
        str(model_plugin._pool.snapshot()),
    )

    index_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.model(index_event, "1")]
    switched = "\n".join(index_event.replies)
    check(
        "/amodel <编号> 把本群会话切到该模型",
        "已把本群会话的模型切成 claude-x" in switched
        and model_cmd_created[0].calls[-1][1]["value"] == "1/b"
        and model_plugin._pool.peek("aiocqhttp:group:661").current_model() == "1/b",
        switched,
    )

    other_event = FakeCommandEvent("aiocqhttp:group:662")
    [chunk async for chunk in model_plugin.model(other_event)]
    other_text = "\n".join(other_event.replies)
    check(
        "只改当前会话：另一个群还是它自己的模型",
        [line for line in other_text.splitlines() if "gpt-5" in line][0]
        .rstrip()
        .endswith("← 当前")
        and model_plugin._pool.peek("aiocqhttp:group:661").current_model() == "1/b"
        and model_plugin._pool.peek("aiocqhttp:group:662").current_model() == "0/a",
        other_text,
    )

    name_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.model(name_event, "ECHO-FLASH")]
    check(
        "/amodel <名字> 大小写不敏感",
        model_cmd_created[0].calls[-1][1]["value"] == "2/c",
        str(model_cmd_created[0].calls[-1]),
    )

    value_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.model(value_event, "0/a")]
    check(
        "/amodel <完整值> 也能切",
        model_cmd_created[0].calls[-1][1]["value"] == "0/a",
        str(model_cmd_created[0].calls[-1]),
    )

    # 解析与渲染的纯函数（/amodel 与 /aeffort 共用）
    model_option = acp_proto.find_config_option(_session_config_options(), "model")
    effort_option = acp_proto.find_config_option(
        _session_config_options(), "thought_level"
    )
    model_choices = own_main.config_choices(model_option)
    effort_choices = own_main.config_choices(effort_option)
    check(
        "config_choices：模型拿编号当短标识，推理强度用值本身",
        [choice["key"] for choice in model_choices] == ["0", "1", "2"]
        and model_choices[1]["aliases"] == ["b"]
        and [choice["key"] for choice in effort_choices]
        == ["unset", "low", "medium", "high", "max", "xhigh"]
        and effort_choices[1]["aliases"] == [],
        str(model_choices[:2] + effort_choices[:2]),
    )
    check(
        "find_choice：完整值 / 编号 / 名字 / 别名，大小写不敏感",
        [
            own_main.find_choice(model_choices, token)[0]["value"]
            for token in ("1/b", "1", "CLAUDE-X", "b")
        ]
        == ["1/b", "1/b", "1/b", "1/b"]
        and own_main.find_choice(effort_choices, "HIGH")[0]["value"]
        == own_main.find_choice(effort_choices, "high")[0]["value"]
        == "high"
        and own_main.find_choice(effort_choices, "unset")[0]["value"] == "unset"
        and own_main.find_choice(effort_choices, "nope") == (None, [])
        and own_main.find_choice(model_choices, "") == (None, []),
        str(own_main.find_choice(effort_choices, "HIGH")),
    )
    check(
        "列表渲染：模型带编号，推理强度不重复值",
        "- 0. gpt-5（0/a）  ← 当前"
        in own_main.render_choice_list(model_choices, "0/a", title="模型", help_text="h")
        and "- Low（low）"
        in own_main.render_choice_list(effort_choices, "low", title="强度", help_text="h"),
        own_main.render_choice_list(model_choices, "0/a", title="模型", help_text="h"),
    )

    # ── /aeffort 命令（同一个会话上的另一项配置）────────
    effort_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.effort(effort_event)]
    effort_list = "\n".join(effort_event.replies)
    effort_current = [line for line in effort_list.splitlines() if "Unset" in line]
    check(
        "/aeffort 列出推理强度并标出当前",
        "本群会话可用的推理强度（6 个）" in effort_list
        and len(effort_current) == 1
        and effort_current[0].rstrip().endswith("← 当前")
        and "XHigh" in effort_list
        and "用法：/aeffort" in effort_list,
        effort_list,
    )

    effort_switch_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.effort(effort_switch_event, "HIGH")]
    check(
        "/aeffort <名字> 大小写不敏感地切推理强度",
        model_cmd_created[0].calls[-1]
        == (
            "session/set_config_option",
            {
                "sessionId": "sess_aiocqhttp:group:661:/w/661",
                "configId": "thought_level",
                "type": "id",
                "value": "high",
            },
        )
        and "已把本群会话的推理强度切成 High（high）"
        in "\n".join(effort_switch_event.replies),
        str(model_cmd_created[0].calls[-1]),
    )
    check(
        "推理强度与模型互不干扰（同一个会话上）",
        model_plugin._pool.peek("aiocqhttp:group:661").current_model() == "0/a"
        and acp_proto.option_current(
            model_plugin._pool.peek("aiocqhttp:group:661").config_option("thought_level")
        )
        == "high",
        str(model_plugin._pool.peek("aiocqhttp:group:661").config_options),
    )

    effort_value_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.effort(effort_value_event, "xhigh")]
    check(
        "/aeffort <值> 直接切",
        model_cmd_created[0].calls[-1][1]["value"] == "xhigh",
        str(model_cmd_created[0].calls[-1]),
    )

    effort_miss_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.effort(effort_miss_event, "nope")]
    check(
        "/aeffort 找不到时提示并附上列表",
        "没找到叫「nope」的推理强度" in "\n".join(effort_miss_event.replies)
        and "XHigh" in "\n".join(effort_miss_event.replies),
        "\n".join(effort_miss_event.replies),
    )

    set_calls_before = len(
        [
            call
            for call in model_cmd_created[0].calls
            if call[0] == "session/set_config_option"
        ]
    )
    miss_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.model(miss_event, "nope")]
    miss_text = "\n".join(miss_event.replies)
    check(
        "/amodel 找不到时不发请求，并把列表附上",
        "没找到叫「nope」的模型" in miss_text
        and "gpt-5" in miss_text
        and len(
            [
                call
                for call in model_cmd_created[0].calls
                if call[0] == "session/set_config_option"
            ]
        )
        == set_calls_before,
        miss_text,
    )

    # 服务端拒绝（模型不存在 / 被隐藏）时如实上报，缓存不动
    model_cmd_created[0].set_config_error = "model not found: 9/z"
    fail_event = FakeCommandEvent("aiocqhttp:group:661")
    [chunk async for chunk in model_plugin.model(fail_event, "2")]
    check(
        "/amodel 切换失败时如实上报",
        "切换失败：model not found: 9/z" in "\n".join(fail_event.replies)
        and model_plugin._pool.peek("aiocqhttp:group:661").current_model() == "0/a",
        "\n".join(fail_event.replies),
    )

    # 同名模型：提示用编号指定（解析规则 + 命令分支）
    dup_hit, dup_candidates = own_main.find_choice(
        [
            {"value": "0/a", "name": "same", "key": "0", "aliases": ["a"]},
            {"value": "1/b", "name": "same", "key": "1", "aliases": ["b"]},
        ],
        "same",
    )
    check(
        "同名模型解析成「多个候选」",
        dup_hit["value"] == "0/a" and len(dup_candidates) == 2,
        str((dup_hit, dup_candidates)),
    )
    dup_created = []

    def dup_factory(url, name):
        client = FakeClient(
            url,
            name,
            config_options=[
                {
                    "configId": "model",
                    "type": "id",
                    "currentValue": "0/a",
                    "options": [
                        {"value": "0/a", "name": "same"},
                        {"value": "1/b", "name": "same"},
                    ],
                }
            ],
        )
        dup_created.append(client)
        return client

    dup_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    dup_plugin._pool = pool_mod.SessionPool(client_factory=dup_factory, idle_timeout=300)
    dup_event = FakeCommandEvent("aiocqhttp:group:665")
    [chunk async for chunk in dup_plugin.model(dup_event, "same")]
    check(
        "/amodel 同名时提示用编号指定，不发请求",
        "对上了多个模型" in "\n".join(dup_event.replies)
        and not [
            call for call in dup_created[0].calls if call[0] == "session/set_config_option"
        ],
        "\n".join(dup_event.replies),
    )

    # 服务端没给模型列表 / 连接池没起来 / 连不上
    bare_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    bare_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name), idle_timeout=300
    )
    bare_event = FakeCommandEvent("aiocqhttp:group:663")
    [chunk async for chunk in bare_plugin.model(bare_event)]
    check(
        "服务端没给模型列表时如实提示",
        "没有给出可选模型" in "\n".join(bare_event.replies),
        "\n".join(bare_event.replies),
    )
    bare_effort_event = FakeCommandEvent("aiocqhttp:group:663")
    [chunk async for chunk in bare_plugin.effort(bare_effort_event)]
    check(
        "服务端没给推理强度时也如实提示",
        "没有给出可选推理强度" in "\n".join(bare_effort_event.replies),
        "\n".join(bare_effort_event.replies),
    )

    no_pool_model_event = FakeCommandEvent("aiocqhttp:group:664")
    no_pool_model_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    [chunk async for chunk in no_pool_model_plugin.model(no_pool_model_event)]
    check(
        "连接池没起来时 /amodel 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_model_event.replies),
        "\n".join(no_pool_model_event.replies),
    )

    down_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    down_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name, fail_connect=True),
        idle_timeout=300,
    )
    down_event = FakeCommandEvent("aiocqhttp:group:666")
    [chunk async for chunk in down_plugin.model(down_event)]
    check(
        "/amodel 连不上时如实上报",
        "连不上 Alkaid0 或开不了会话" in "\n".join(down_event.replies),
        "\n".join(down_event.replies),
    )

    # ── /aresume 命令（列会话 + 切回旧会话）────────────
    resume_store = store_mod.SessionStore(db_dir / "resume-cmd.db")
    resume_created = []

    def resume_factory(url, name):
        client = FakeClient(
            url,
            name,
            config_options=_session_config_options("0/a"),
            sessions=[
                {
                    # 本群刚建的那个会话也在列表里（真机就是这样）
                    "sessionId": "sess_aiocqhttp:group:671:/w/671",
                    "cwd": "/w/671",
                    "title": "当前这次",
                    "updatedAt": "2026-10-16T20:00:00Z",
                },
                {
                    "sessionId": "sess_12:/w/671",
                    "cwd": "/w/671",
                    "title": "修一下 diff 渲染",
                    "updatedAt": "2026-10-16T12:30:00Z",
                },
                {
                    "sessionId": "sess_9:/w/671",
                    "cwd": "/w/671",
                    "title": "更早那次",
                    "updatedAt": "2026-10-01T03:04:05Z",
                },
                {
                    # 时间缺失（纪元）+ 超长标题：看看渲染兜底
                    "sessionId": "sess_3:/w/671",
                    "cwd": "/w/671",
                    "title": "很长的标题" * 8,
                    "updatedAt": "1970-01-01T00:00:00Z",
                },
            ],
        )
        resume_created.append(client)
        return client

    resume_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    resume_plugin._pool = pool_mod.SessionPool(
        client_factory=resume_factory, idle_timeout=300, store=resume_store
    )

    resume_list_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in resume_plugin.resume(resume_list_event)]
    resume_list = "\n".join(resume_list_event.replies)
    resume_lines = resume_list.splitlines()
    resume_current = [line for line in resume_lines if line.endswith("← 当前")]
    check(
        "/aresume 列出工作目录下的会话（编号 / 标题 / 时间 / 当前）",
        "本群工作目录下的会话（4 个，最近活动在前）" in resume_list
        and len(resume_current) == 1
        # 假客户端的会话 ID 是 sess_<name>:<cwd>，短号就是 sess_ 后面那段
        and resume_current[0].startswith("- aiocqhttp. 当前这次")
        and any(line.startswith("- 12. 修一下 diff 渲染") for line in resume_lines)
        and any(line.startswith("- 9. 更早那次") for line in resume_lines)
        and "用法：/aresume" in resume_list
        and ("session/list", {"cwd": "/w/671"}) in resume_created[0].calls,
        resume_list,
    )
    check(
        "过长标题会截断、纪元时间不显示",
        any(line.startswith("- 3. ") and line.endswith("…") for line in resume_lines),
        resume_list,
    )

    resume_created[0].session_list_cursor = "next-page"
    resume_more_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in resume_plugin.resume(resume_more_event)]
    check(
        "还有更早的会话时给出提示",
        "还有更早的会话" in "\n".join(resume_more_event.replies),
        "\n".join(resume_more_event.replies),
    )
    resume_created[0].session_list_cursor = ""

    resume_switch_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in resume_plugin.resume(resume_switch_event, "9")]
    resume_switch_text = "\n".join(resume_switch_event.replies)
    check(
        "/aresume <会话号> 切到那个会话并记账",
        "已把本群的会话切到 9（sess_9:/w/671）" in resume_switch_text
        and resume_plugin._pool.peek("aiocqhttp:group:671").session_id == "sess_9:/w/671"
        and resume_store.get("aiocqhttp:group:671") == "sess_9:/w/671"
        and ("session/resume", {"sessionId": "sess_9:/w/671", "cwd": "/w/671"})
        in resume_created[0].calls,
        resume_switch_text,
    )

    resume_same_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in resume_plugin.resume(resume_same_event, "sess_9:/w/671")]
    check(
        "/aresume 切到当前会话时只提示、不再发请求",
        "本群当前就是这个会话" in "\n".join(resume_same_event.replies)
        and len([call for call in resume_created[0].calls if call[0] == "session/resume"])
        == 1,
        "\n".join(resume_same_event.replies),
    )

    resume_created[0].fail_resume = True
    resume_bad_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in resume_plugin.resume(resume_bad_event, "404")]
    check(
        "/aresume 切到不存在的会话时如实上报，记录不动",
        "切换失败" in "\n".join(resume_bad_event.replies)
        and resume_store.get("aiocqhttp:group:671") == "sess_9:/w/671",
        "\n".join(resume_bad_event.replies),
    )

    resume_created[0].fail_resume = False
    resume_created[0].session_list_error = "cwd not inited"
    resume_list_fail_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in resume_plugin.resume(resume_list_fail_event)]
    check(
        "/aresume 列会话失败时如实上报",
        "列会话失败：cwd not inited" in "\n".join(resume_list_fail_event.replies),
        "\n".join(resume_list_fail_event.replies),
    )

    no_pool_resume_event = FakeCommandEvent("aiocqhttp:group:672")
    no_pool_resume_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    [chunk async for chunk in no_pool_resume_plugin.resume(no_pool_resume_event)]
    check(
        "连接池没起来时 /aresume 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_resume_event.replies),
        "\n".join(no_pool_resume_event.replies),
    )

    resume_down_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    resume_down_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name, fail_connect=True),
        idle_timeout=300,
    )
    resume_down_event = FakeCommandEvent("aiocqhttp:group:673")
    [chunk async for chunk in resume_down_plugin.resume(resume_down_event)]
    check(
        "/aresume 连不上时如实上报",
        "连不上 Alkaid0 或开不了会话" in "\n".join(resume_down_event.replies),
        "\n".join(resume_down_event.replies),
    )

    # 纯函数：会话号解析 / 时间格式化 / 空列表
    check(
        "session_short_id / resolve_session_arg：认会话号也认完整 ID",
        own_main.session_short_id("sess_12:/w/x") == "12"
        and own_main.session_short_id("怪东西") == "怪东西"
        and own_main.resolve_session_arg("9", "/w/x") == "sess_9:/w/x"
        and own_main.resolve_session_arg(" 12 ", "/w/x") == "sess_12:/w/x"
        and own_main.resolve_session_arg("sess_9:/w/x", "/w/x") == "sess_9:/w/x",
    )
    check(
        "时间格式化：RFC 3339 → 本地 MM-DD HH:MM，纪元 / 非法值当没有时间",
        len(own_main.format_updated_at("2026-10-16T12:30:00Z")) == 11
        and own_main.format_updated_at("2026-10-16T12:30:00Z")[2] == "-"
        and own_main.format_updated_at("1970-01-01T00:00:00Z") == ""
        and own_main.format_updated_at("") == ""
        and own_main.format_updated_at("不是时间") == "",
        own_main.format_updated_at("2026-10-16T12:30:00Z"),
    )
    check(
        "目录里一个会话都没有时给出空提示",
        "还没有会话" in own_main.render_session_list([], "", cwd="/w/x"),
        own_main.render_session_list([], "", cwd="/w/x"),
    )
    check(
        "用量渲染：整数截断、封顶 100%，窗口未知时只报已用",
        own_main.render_context_usage((11724, 1024000)) == "context 1% (11724/1024000)"
        and own_main.render_context_usage((12840, 200000)) == "context 6% (12840/200000)"
        and own_main.render_context_usage((199999, 200000))
        == "context 99% (199999/200000)"
        and own_main.render_context_usage((300000, 200000))
        == "context 100% (300000/200000)"
        and own_main.render_context_usage((500, 0)) == "context 500"
        and own_main.render_context_usage((0, 0)) == "context 0"
        and own_main.render_context_usage(None) == "",
        own_main.render_context_usage((11724, 1024000)),
    )

    # ── 远程建目录：新群第一次开会话时自动建出模板目录 ──
    mkdir_created = []

    def mkdir_factory(url, name):
        client = FakeClient(url, name, existing_cwds={"/root/base"})
        mkdir_created.append(client)
        return client

    mkdir_pool = pool_mod.SessionPool(client_factory=mkdir_factory, idle_timeout=300)
    mkdir_session = await mkdir_pool.acquire(
        "group:mkdir", url="ws://acp", cwd="/root/base/999", cwd_root="/root/base"
    )
    check(
        "新群第一次聊天：借临时会话建目录再重试开会话",
        mkdir_session.session_id == "sess_group:mkdir:/root/base/999"
        and ("fs/mkdir", {"sessionId": "sess_group:mkdir:/root/base", "path": "999"})
        in mkdir_created[0].calls
        and ("session/close", {"sessionId": "sess_group:mkdir:/root/base"})
        in mkdir_created[0].calls,
        str(mkdir_created[0].calls),
    )

    no_root_created = []

    def no_root_factory(url, name):
        client = FakeClient(url, name, existing_cwds={"/root/base"})
        no_root_created.append(client)
        return client

    no_root_pool = pool_mod.SessionPool(
        client_factory=no_root_factory, idle_timeout=300
    )
    no_root_error = None
    try:
        await no_root_pool.acquire("group:noroot", url="ws://acp", cwd="/root/base/888")
    except ACPError as exc:
        no_root_error = str(exc)
    check(
        "没给模板根时原样报错（不瞎建目录）",
        bool(no_root_error and "cwd not found" in no_root_error)
        and not [call for call in no_root_created[0].calls if call[0] == "fs/mkdir"],
        f"{no_root_error} / {no_root_created[0].calls}",
    )
    check(
        "相对路径剥根：在根下 / 等于根 / 不在根下",
        pool_mod._relative_under("/root/base", "/root/base/a/b") == "a/b"
        and pool_mod._relative_under("/root/base", "/root/base") == "."
        and pool_mod._relative_under("/root/base", "/root/other") == ""
        and pool_mod._relative_under("", "/x") == "",
    )

    # ── Runner 发提示词前按 provider_settings 注入 ─────
    inject_runner = runner_mod.Alkaid0Runner(
        pool_mod.SessionPool(
            client_factory=lambda url, name: FakeClient(url, name), idle_timeout=300
        ),
        {"acp_url": "ws://plugin/acp"},
        turn_queue_mod.TurnQueue(),
        settings_provider=lambda: {
            "prompt_prefix": "P:{{prompt}}",
            "identifier": True,
            "group_name_display": True,
        },
    )
    inject_event = FakeEvent(
        group_id="555",
        session_id="555",
        umo="aiocqhttp:group:555",
        sender_name="小明",
        sender_id="20002",
        group_name="摸鱼群",
    )
    decorated = await inject_runner._decorated_prompt(
        turn_queue_mod.TurnSlot(
            turn_queue_mod.ACTION_MERGED,
            "（群里攒了 2 条消息，一起看一下）\n[小明] 在吗\n[小红] 在",
            ("小明", "小红"),
            (("小明", "20002"), ("小红", "20003")),
        ),
        inject_event,
    )
    check(
        "Runner 发提示词前注入前缀 / 每人 reminder / 群名",
        decorated.startswith("P:")
        and "[Reminder: @小明(20002)]" in decorated
        and "[Reminder: @小红(20003)]" in decorated
        and "Group name: 摸鱼群" in decorated,
        decorated,
    )

    # ── 每会话工作目录：存储 + /aworkdir ───────────────
    wd_store = store_mod.SessionStore(db_dir / "workdir.db")
    check("工作目录绑定：空库读不到", wd_store.get_workdir("group:wd") is None)
    wd_store.remember_workdir("group:wd", "work/a")
    check("工作目录绑定：写入后可读回", wd_store.get_workdir("group:wd") == "work/a")
    wd_store.remember_workdir("group:wd", ".")
    check(
        "工作目录绑定：同键覆盖（. 表示根目录）",
        wd_store.get_workdir("group:wd") == ".",
    )
    check(
        "工作目录绑定：删除",
        wd_store.forget_workdir("group:wd") is True
        and wd_store.get_workdir("group:wd") is None,
    )
    check(
        "工作目录绑定：坏库降级为「没绑定」",
        store_mod.SessionStore("/proc/1/nope/wd.db").get_workdir("x") is None,
    )

    async def _always_true(root, rel):
        """假建目录：不真连服务器。"""
        return True

    wd_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/root/base/{groupid}",
        },
    )
    wd_plugin._store = wd_store
    wd_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name), idle_timeout=300
    )

    wd_list_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in wd_plugin.workdir(wd_list_event, "")]
    wd_list = "\n".join(wd_list_event.replies)
    check(
        "/aworkdir 不带参：模板 / 未绑定 / 绝对路径 / 用法",
        "模板: /root/base/{groupid}" in wd_list
        and "绑定: 未设置" in wd_list
        and "/root/base/671" in wd_list
        and "用法: /aworkdir" in wd_list,
        wd_list,
    )

    wd_bad_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in wd_plugin.workdir(wd_bad_event, "/etc/passwd")]
    check(
        "/aworkdir 拒绝绝对路径",
        "非法相对路径" in "\n".join(wd_bad_event.replies),
        "\n".join(wd_bad_event.replies),
    )

    wd_plugin._mkdir_remote = _always_true
    wd_set_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in wd_plugin.workdir(wd_set_event, "work/a")]
    wd_set = "\n".join(wd_set_event.replies)
    check(
        "/aworkdir 设置成功：落库 + 拼成绝对路径",
        "已把本群的工作目录设为：/root/base/work/a" in wd_set
        and wd_store.get_workdir("aiocqhttp:group:671") == "work/a",
        wd_set,
    )
    wd_cwd = wd_plugin._cwd_for(
        "aiocqhttp:group:671",
        FakeCommandEvent("aiocqhttp:group:671"),
        wd_plugin._settings(),
    )
    check("绑定后工作目录解析跟着走", wd_cwd == "/root/base/work/a", wd_cwd)

    wd_reset_event = FakeCommandEvent("aiocqhttp:group:671")
    [chunk async for chunk in wd_plugin.workdir(wd_reset_event, "reset")]
    check(
        "/aworkdir reset：清掉绑定、回到模板默认",
        "已恢复为模板默认目录" in "\n".join(wd_reset_event.replies)
        and wd_store.get_workdir("aiocqhttp:group:671") is None,
        "\n".join(wd_reset_event.replies),
    )

    wd_busy_created = []

    def wd_busy_factory(url, name):
        client = FakeClient(url, name)
        wd_busy_created.append(client)
        return client

    wd_plugin._pool = pool_mod.SessionPool(
        client_factory=wd_busy_factory, idle_timeout=300
    )
    wd_busy_session = await wd_plugin._pool.acquire(
        "aiocqhttp:group:672", url="ws://acp", cwd="/root/base/672"
    )
    async with wd_busy_session.lock:
        wd_busy_event = FakeCommandEvent("aiocqhttp:group:672")
        [chunk async for chunk in wd_plugin.workdir(wd_busy_event, "work/b")]
    check(
        "/aworkdir 本群正在跑一轮时不切（提示先 /astop）",
        "正在跑一轮" in "\n".join(wd_busy_event.replies)
        and wd_store.get_workdir("aiocqhttp:group:672") is None,
        "\n".join(wd_busy_event.replies),
    )

    # ── /acmd 命令（列远端命令 + 发原始命令）────────────
    acmd_created = []

    def acmd_factory(url, name):
        client = FakeClient(
            url,
            name,
            commands=_available_commands(),
            capabilities=ALKAID0_CAPABILITIES,
            updates=[
                {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "cmd_1_1",
                    "content": {"type": "text", "text": "压缩完成，共 12 条摘要。"},
                },
                {
                    "sessionUpdate": "state_update",
                    "state": "idle",
                    "stopReason": "end_turn",
                },
            ],
        )
        acmd_created.append(client)
        return client

    acmd_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    acmd_plugin._pool = pool_mod.SessionPool(
        client_factory=acmd_factory, idle_timeout=300
    )

    acmd_list_event = FakeCommandEvent("aiocqhttp:group:681")
    [chunk async for chunk in acmd_plugin.remote_command(acmd_list_event, "")]
    acmd_list = "\n".join(acmd_list_event.replies)
    check(
        "/acmd 列出远端命令，并用 alcoh 同步的对照表翻成中文",
        "Alkaid0 的远端命令（3 个）:" in acmd_list
        and "- /compress（(no args)）  压缩历史记录" in acmd_list
        and "- /background（on|off）  开启/关闭后台模式——所有客户端断开后保持会话存活"
        in acmd_list
        and "Third party text" in acmd_list  # 没收录的条目照原文
        and COMPRESS_DESCRIPTION not in acmd_list
        and "用法：/acmd" in acmd_list,
        acmd_list,
    )
    check(
        "/acmd 没会话时按「这个群的第一条消息」建会话（cwd 走模板）",
        acmd_plugin._pool.peek("aiocqhttp:group:681").cwd == "/w/681"
        and acmd_plugin._pool.peek("aiocqhttp:group:681").session_id
        == "sess_aiocqhttp:group:681:/w/681",
        str(acmd_plugin._pool.snapshot()),
    )

    acmd_run_event = FakeCommandEvent("aiocqhttp:group:681")
    [chunk async for chunk in acmd_plugin.remote_command(acmd_run_event, "compress")]
    check(
        "/acmd <命令> 自动补 / 发过去，并把输出回给群里",
        "\n".join(acmd_run_event.replies) == "压缩完成，共 12 条摘要。"
        and acmd_created[0].calls[-1]
        == (
            "session/prompt",
            {"sessionId": "sess_aiocqhttp:group:681:/w/681", "text": "/compress"},
        ),
        f"{acmd_run_event.replies} / {acmd_created[0].calls[-1]}",
    )

    acmd_slash_event = FakeCommandEvent("aiocqhttp:group:681")
    [chunk async for chunk in acmd_plugin.remote_command(acmd_slash_event, "/compress")]
    check(
        "/acmd 里已经带 / 的命令不重复补",
        acmd_created[0].calls[-1][1]["text"] == "/compress",
        str(acmd_created[0].calls[-1]),
    )

    acmd_quiet_created = []

    def acmd_quiet_factory(url, name):
        client = FakeClient(
            url,
            name,
            commands=_available_commands(),
            capabilities=ALKAID0_CAPABILITIES,
            updates=[
                {"sessionUpdate": "state_update", "state": "idle", "stopReason": "end_turn"}
            ],
        )
        acmd_quiet_created.append(client)
        return client

    quiet_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    quiet_plugin._pool = pool_mod.SessionPool(
        client_factory=acmd_quiet_factory, idle_timeout=300
    )
    quiet_event = FakeCommandEvent("aiocqhttp:group:682")
    [chunk async for chunk in quiet_plugin.remote_command(quiet_event, "background on")]
    check(
        "/acmd 带参数的命令原样发（只补开头的 /），没输出时如实说一句",
        acmd_quiet_created[0].calls[-1][1]["text"] == "/background on"
        and "执行完了，没有输出" in "\n".join(quiet_event.replies),
        f"{quiet_event.replies} / {acmd_quiet_created[0].calls[-1]}",
    )

    acmd_created[0].fail_prompt = "0528v2：[-32099] invalid command"
    acmd_fail_event = FakeCommandEvent("aiocqhttp:group:681")
    [chunk async for chunk in acmd_plugin.remote_command(acmd_fail_event, "nope")]
    check(
        "/acmd 服务端不认这条命令时如实上报",
        "执行失败：0528v2：[-32099] invalid command"
        in "\n".join(acmd_fail_event.replies),
        "\n".join(acmd_fail_event.replies),
    )
    acmd_created[0].fail_prompt = None

    raw_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    raw_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(
            url, name, commands=_available_commands()
        ),
        idle_timeout=300,
    )
    raw_event = FakeCommandEvent("aiocqhttp:group:683")
    [chunk async for chunk in raw_plugin.remote_command(raw_event, "")]
    raw_text = "\n".join(raw_event.replies)
    check(
        "/acmd 服务端没声明扩展能力时照原文显示，并说明原因",
        COMPRESS_DESCRIPTION in raw_text
        and "压缩历史记录" not in raw_text
        and "没声明 alkaid0 扩展能力" in raw_text,
        raw_text,
    )
    check(
        "/acmd 还没拿到命令列表时如实提示",
        "还没下发命令列表" in own_main.render_command_list([], translated=True),
        own_main.render_command_list([], translated=True),
    )

    no_pool_acmd_event = FakeCommandEvent("aiocqhttp:group:684")
    no_pool_acmd_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    [chunk async for chunk in no_pool_acmd_plugin.remote_command(no_pool_acmd_event, "help")]
    check(
        "连接池没起来时 /acmd 只提示不报错",
        "连接池还没启动" in "\n".join(no_pool_acmd_event.replies),
        "\n".join(no_pool_acmd_event.replies),
    )

    acmd_down_plugin = Plugin0528V2(
        context=None,
        config={
            "enforce_prerequisite": False,
            "acp_url": "ws://acp",
            "workspace_template": "/w/{groupid}",
        },
    )
    acmd_down_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name, fail_connect=True),
        idle_timeout=300,
    )
    acmd_down_event = FakeCommandEvent("aiocqhttp:group:685")
    [chunk async for chunk in acmd_down_plugin.remote_command(acmd_down_event, "help")]
    check(
        "/acmd 连不上时如实上报",
        "连不上 Alkaid0 或开不了会话" in "\n".join(acmd_down_event.replies),
        "\n".join(acmd_down_event.replies),
    )

    # ── run_slash_command 的边角（直接调）────────────────
    cancel_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(
            url,
            name,
            updates=[
                {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "cmd_1_1",
                    "content": {"type": "text", "text": "摘要好了"},
                },
                # 手动摘要这类异步命令收尾时服务端报的是 cancelled，不是出错
                {
                    "sessionUpdate": "state_update",
                    "state": "idle",
                    "stopReason": "cancelled",
                },
            ],
        ),
        idle_timeout=300,
    )
    cancel_session = await cancel_pool.acquire("group:cmd", url="ws://acp", cwd="/w/cmd")
    cancelled_text = await runner_mod.run_slash_command(
        cancel_session, "/compress", timeout=5
    )
    check(
        "异步命令以 cancelled 收尾不算失败（/compress 就是这样）",
        cancelled_text == "摘要好了",
        cancelled_text,
    )

    refusal_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(
            url,
            name,
            updates=[
                {"sessionUpdate": "state_update", "state": "idle", "stopReason": "refusal"}
            ],
        ),
        idle_timeout=300,
    )
    refusal_session = await refusal_pool.acquire(
        "group:cmd-r", url="ws://acp", cwd="/w/cmd-r"
    )
    refusal_error = None
    try:
        await runner_mod.run_slash_command(refusal_session, "/nope", timeout=5)
    except ACPError as exc:
        refusal_error = str(exc)
    check(
        "idle 标成 refusal 且没有正文时如实报错",
        refusal_error == "Alkaid0 拒绝了这条命令",
        str(refusal_error),
    )

    hang_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(
            url, name, updates=[{"sessionUpdate": "state_update", "state": "running"}]
        ),
        idle_timeout=300,
    )
    hang_session = await hang_pool.acquire("group:cmd-h", url="ws://acp", cwd="/w/cmd-h")
    hang_error = None
    hang_started = time.monotonic()
    try:
        await runner_mod.run_slash_command(hang_session, "/help", timeout=0.4)
    except ACPError as exc:
        hang_error = str(exc)
    check(
        "等不到本轮结束时按超时报错（不无限等）",
        bool(hang_error and "超时" in hang_error)
        and time.monotonic() - hang_started < 3,
        f"{hang_error}（{time.monotonic() - hang_started:.2f}s）",
    )

    drop_cmd_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(
            url, name, updates=[], drop_after_prompt=True
        ),
        idle_timeout=300,
    )
    drop_cmd_session = await drop_cmd_pool.acquire(
        "group:cmd-d", url="ws://acp", cwd="/w/cmd-d"
    )
    drop_cmd_error = None
    drop_cmd_started = time.monotonic()
    try:
        await runner_mod.run_slash_command(drop_cmd_session, "/help", timeout=30)
    except ACPError as exc:
        drop_cmd_error = str(exc)
    check(
        "命令轮掉线时立刻报错（不干等超时）",
        bool(drop_cmd_error and "连接已断开" in drop_cmd_error)
        and time.monotonic() - drop_cmd_started < 3,
        f"{drop_cmd_error}（{time.monotonic() - drop_cmd_started:.2f}s）",
    )

    # ── Runner 端到端（假客户端回放通知）────────────────
    updates = [
        {"sessionUpdate": "state_update", "state": "running"},
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "msg_1",
            "content": {"type": "text", "text": "你"},
        },
        # 工具调用的流式预览与重复状态都不该回显
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "title": "[Call edit]",
            "status": "streaming",
            "content": [
                {
                    "type": "alk.cxykevin.top/calling_info",
                    "name": "edit",
                    "args": "{}",
                }
            ],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "title": "[Call edit]",
            "status": "streaming",
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "title": "[Call edit]",
            "status": "completed",
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "title": "[Call edit]",
            "status": "completed",
        },
        # 子 Agent 只在第一次出现时说一声
        {
            "sessionUpdate": "agent_thought_chunk",
            "messageId": "th_1",
            "content": {"type": "text", "text": "想想"},
            "alk.cxykevin.top/agent_status": "explore",
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "msg_2",
            "content": {"type": "text", "text": "好"},
            "alk.cxykevin.top/agent_status": "explore",
        },
        {"sessionUpdate": "state_update", "state": "requires_action"},
        {"sessionUpdate": "state_update", "state": "requires_action"},
        {
            "sessionUpdate": "alk.cxykevin.top/shell_stop",
            "runId": "@temp/run/3",
            "command": "npm test",
            "success": True,
        },
        {"sessionUpdate": "state_update", "state": "idle", "stopReason": "end_turn"},
    ]
    runner_created = []

    def runner_factory(url, name):
        client = FakeClient(url, name, updates=updates)
        runner_created.append(client)
        return client

    runner_pool = pool_mod.SessionPool(client_factory=runner_factory, idle_timeout=300)
    runner = runner_mod.Alkaid0Runner(runner_pool, {"acp_url": "ws://plugin/acp"})
    event = FakeEvent(group_id="555", session_id="555", umo="aiocqhttp:group:555")
    await runner.reset(
        request=SimpleNamespace(prompt="在吗"),
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        agent_hooks=None,
        provider_config={
            "acp_url": "ws://panel/acp",
            "workspace_template": "/panel/{groupid}",
            "idle_timeout_sec": 60,
        },
        streaming=True,
    )
    responses = [response async for response in runner.step_until_done()]

    final_text = _chain_text(responses[-1].data["chain"])
    delta_texts = [_chain_text(response.data["chain"]) for response in responses[:-1]]
    check(
        "流式：过程回显与正文增量一起实时推给内核",
        [response.type for response in responses]
        == ["streaming_delta"] * 6 + ["llm_result"],
        str([response.type for response in responses]),
    )
    check(
        "回显与正文按到达顺序增量送出",
        delta_texts
        == [
            "你",
            "\n🔧 调用 edit（已完成）\n",
            "\n🤖 子 Agent：explore\n",
            "好",
            "\n⏳ 工具审批等待中（回复 /aapprove 允许）\n",
            "\n✅ 后台任务 @temp/run/3 结束：npm test\n",
        ],
        str(delta_texts),
    )
    check(
        "最终回复里正文成段、连续的过程回显挤在一起",
        final_text.split("\n\n")
        == [
            "你",
            "🔧 调用 edit（已完成）\n🤖 子 Agent：explore",
            "好",
            "⏳ 工具审批等待中（回复 /aapprove 允许）\n✅ 后台任务 @temp/run/3 结束：npm test",
        ],
        final_text,
    )
    check(
        "工具调用只在状态变化时回显一次（streaming 预览不刷屏）",
        final_text.count("🔧 调用 edit（已完成）") == 1
        and "streaming" not in final_text
        and final_text.count("🤖 子 Agent：explore") == 1
        and final_text.count("⏳ 工具审批等待中") == 1,
        final_text,
    )
    check(
        "Runner 结束状态与最终结果就绪",
        runner.done() and runner.get_final_llm_resp() is not None,
    )
    check(
        "按面板参数开会话（模板优先于插件配置）",
        runner_pool.peek("aiocqhttp:group:555").cwd == "/panel/555",
        str(runner_pool.snapshot()),
    )
    check("空闲阈值跟随面板", runner_pool.idle_timeout == 60, str(runner_pool.idle_timeout))
    check(
        "把用户消息原样发给 alkaid0",
        [call for call in runner_created[0].calls if call[0] == "session/prompt"]
        == [
            (
                "session/prompt",
                {
                    "sessionId": "sess_aiocqhttp:group:555:/panel/555",
                    "text": "在吗",
                },
            )
        ],
        str(runner_created[0].calls),
    )
    check(
        "一轮结束后解绑本轮订阅，只留会话自己的常驻监听（配置项 / 命令列表 / 用量）",
        len(runner_created[0]._handlers) == 3
        and "_listen_config_options"
        in getattr(runner_created[0]._handlers[0], "__qualname__", "")
        and "_listen_available_commands"
        in getattr(runner_created[0]._handlers[1], "__qualname__", "")
        and "_listen_usage"
        in getattr(runner_created[0]._handlers[2], "__qualname__", ""),
        str(runner_created[0]._handlers),
    )
    check("给连接装上了审批处理函数", callable(runner_created[0].permission_handler))

    await runner.close()
    check(
        "结束一轮不断开连接（留给该群下一条消息复用）",
        runner_pool.peek("aiocqhttp:group:555") is not None
        and runner_created[0].connected,
    )

    # ── 关掉回显：过程消息不进聊天 ─────────────────────
    muted_client = FakeClient("ws://muted/acp", "muted", updates=updates)
    muted_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: muted_client, idle_timeout=300
    )
    muted_runner = runner_mod.Alkaid0Runner(muted_pool, {"echo_events": False})
    muted_event = FakeEvent(group_id="777", session_id="777", umo="aiocqhttp:group:777")
    await muted_runner.reset(
        request=SimpleNamespace(prompt="安静点"),
        run_context=SimpleNamespace(context=SimpleNamespace(event=muted_event)),
        agent_hooks=None,
        provider_config=None,
        streaming=False,
    )
    muted_responses = [response async for response in muted_runner.step_until_done()]
    muted_text = _chain_text(muted_responses[-1].data["chain"])
    check(
        "关掉回显后过程消息不再进聊天（审批提示除外，非流式也照常给最终结果）",
        [response.type for response in muted_responses] == ["llm_result"]
        and muted_text == "你\n\n好\n\n⏳ 工具审批等待中（回复 /aapprove 允许）"
        and "🔧 调用 edit" not in muted_text
        and "🤖 子 Agent" not in muted_text
        and "✅ 后台任务" not in muted_text,
        f"{[response.type for response in muted_responses]} / {muted_text}",
    )
    await muted_runner.close()
    await muted_pool.close_all()

    # ── 出错路径：idle + error_msg ─────────────────────
    error_updates = [
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "msg_1",
            "content": {"type": "text", "text": "半截输出"},
        },
        {
            "sessionUpdate": "state_update",
            "state": "idle",
            "stopReason": "refusal",
            "alk.cxykevin.top/error_msg": "模型报错",
        },
    ]
    error_created = []

    def error_factory(url, name):
        client = FakeClient(url, name, updates=error_updates)
        error_created.append(client)
        return client

    error_pool = pool_mod.SessionPool(client_factory=error_factory, idle_timeout=300)
    error_runner = runner_mod.Alkaid0Runner(error_pool, {"acp_url": "ws://plugin/acp"})
    await error_runner.reset(
        request=SimpleNamespace(prompt="炸一个"),
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        agent_hooks=None,
        provider_config={"acp_url": "ws://panel/acp"},
        streaming=False,
    )
    error_responses = [response async for response in error_runner.step_until_done()]
    error_text = _chain_text(error_responses[-1].data["chain"])
    check(
        "出错时推 err 响应",
        error_responses[-1].type == "err" and error_runner.done(),
        str([response.type for response in error_responses]),
    )
    check(
        "出错时保留已产出的正文并带上原因",
        "半截输出" in error_text and "模型报错" in error_text,
        error_text,
    )
    check(
        "非流式时不推增量",
        [response.type for response in error_responses] == ["err"],
        str([response.type for response in error_responses]),
    )

    # ── 拿不到事件时如实报错，不去连服务器 ─────────────
    empty_runner = runner_mod.Alkaid0Runner(error_pool, {"acp_url": "ws://plugin/acp"})
    await empty_runner.reset(
        request=SimpleNamespace(prompt="你好"),
        run_context=SimpleNamespace(context=SimpleNamespace(event=None)),
        agent_hooks=None,
        provider_config={"acp_url": "ws://panel/acp"},
        streaming=False,
    )
    empty_responses = [response async for response in empty_runner.step_until_done()]
    check(
        "拿不到消息事件时如实报错",
        empty_responses[-1].type == "err"
        and "拿不到消息事件" in _chain_text(empty_responses[-1].data["chain"]),
        _chain_text(empty_responses[-1].data["chain"]),
    )

    # ── 连接中途断开：立刻报错，不干等到 turn_timeout ──
    drop_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(
            url, name, updates=[], drop_after_prompt=True
        ),
        idle_timeout=300,
    )
    drop_runner = runner_mod.Alkaid0Runner(drop_pool, {"acp_url": "ws://plugin/acp"})
    await drop_runner.reset(
        request=SimpleNamespace(prompt="断线测试"),
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        agent_hooks=None,
        provider_config={"acp_url": "ws://panel/acp", "turn_timeout_sec": 5},
        streaming=False,
    )
    drop_started = time.monotonic()
    drop_responses = [response async for response in drop_runner.step_until_done()]
    drop_elapsed = time.monotonic() - drop_started
    drop_text = _chain_text(drop_responses[-1].data["chain"])
    check(
        "连接中途断开时立刻报错（不干等 turn_timeout）",
        drop_responses[-1].type == "err"
        and "连接已断开" in drop_text
        and drop_elapsed < 3,
        f"{drop_text}（耗时 {drop_elapsed:.2f}s）",
    )

    # ── 被中止的一轮（/astop）：半截正文照发，末尾标明已终止 ──
    cancel_updates = [
        {"sessionUpdate": "state_update", "state": "running"},
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "msg_c",
            "content": {"type": "text", "text": "我先看一下"},
        },
        {"sessionUpdate": "state_update", "state": "idle", "stopReason": "cancelled"},
    ]
    stopped_pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name, updates=cancel_updates),
        idle_timeout=300,
    )
    stopped_runner = runner_mod.Alkaid0Runner(
        stopped_pool, {"acp_url": "ws://plugin/acp"}
    )
    await stopped_runner.reset(
        request=SimpleNamespace(prompt="跑个长任务"),
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        agent_hooks=None,
        provider_config={"acp_url": "ws://panel/acp"},
        streaming=False,
    )
    stopped_responses = [
        response async for response in stopped_runner.step_until_done()
    ]
    stopped_text = _chain_text(stopped_responses[-1].data["chain"])
    check(
        "被中止的一轮：半截正文照发，并标出「本轮已终止」",
        stopped_responses[-1].type == "llm_result"
        and "我先看一下" in stopped_text
        and "🛑 本轮已终止" in stopped_text,
        stopped_text,
    )


async def _no_reap_when_disabled(pool):
    """空闲阈值设为 0 时，回收应当什么都不做。"""
    original = pool.idle_timeout
    pool.idle_timeout = 0
    try:
        session = pool.peek("group:c")
        session.last_active -= 100000
        return await pool.reap_idle() == [] and pool.peek("group:c") is not None
    finally:
        pool.idle_timeout = original


asyncio.run(alkaid0_checks())

# --------------------------------------------------- 14. 执行器注册（librunner 在场时）
try:
    from data.plugins.astrbot_plugin_librunner.api import (
        get_runner,
        has_runner,
        registered_types,
    )
except Exception as exc:  # noqa: BLE001 - 前置不在场就跳过
    print(f"[SKIP] 本机没有可导入的 librunner，跳过执行器注册检查（{exc}）")
else:

    async def registration_checks():
        """注册 / 注销执行器，并顺带确认连接池受插件生命周期管理。"""
        reg_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
        check("连接池懒创建 + 回收任务启动", reg_plugin._ensure_pool().reaper_running)
        check(
            "执行器注册成功",
            reg_plugin._register_runner() and has_runner(runner_mod.RUNNER_TYPE),
        )
        check(
            "librunner 的已注册列表里能看到它",
            runner_mod.RUNNER_TYPE in registered_types(),
            str(registered_types()),
        )

        info = get_runner(runner_mod.RUNNER_TYPE)
        check(
            "注册信息带上展示名、描述与面板字段",
            info is not None
            and info.label == runner_mod.RUNNER_LABEL
            and info.owner == PLUGIN_NAME
            and set(info.config_default) == set(runner_mod.RUNNER_CONFIG_DEFAULT),
            str(info),
        )

        reg_plugin._unregister_runner()
        check("注销后 librunner 里不再有该执行器", not has_runner(runner_mod.RUNNER_TYPE))
        check("已注销标记复位", reg_plugin._runner_registered is False)
        await reg_plugin._shutdown_pool()
        check("停用后连接池已释放", reg_plugin._pool is None)

    asyncio.run(registration_checks())

# --------------------------------------------------- 15. 仓库文件约束
schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
check(
    "配置 schema 提供全部配置项",
    set(schema)
    == {
        "enforce_prerequisite",
        "prerequisite_librunner_repo",
        "prerequisite_libi18nject_repo",
        "acp_url",
        "workspace_template",
        "idle_timeout_sec",
        "turn_timeout_sec",
        "echo_events",
        "show_thought",
    },
    str(sorted(schema)),
)
check(
    "schema 里的 ACP 默认值与执行器默认值一致",
    schema["acp_url"]["default"] == runner_mod.DEFAULT_ACP_URL
    and schema["workspace_template"]["default"]
    == runner_mod.DEFAULT_WORKSPACE_TEMPLATE
    and schema["idle_timeout_sec"]["default"] == runner_mod.DEFAULT_IDLE_TIMEOUT_SEC
    and schema["turn_timeout_sec"]["default"] == runner_mod.DEFAULT_TURN_TIMEOUT_SEC,
    str({k: schema[k]["default"] for k in ("acp_url", "workspace_template", "idle_timeout_sec")}),
)
check(
    "执行器面板字段与插件配置项同名同集合",
    set(runner_mod.RUNNER_CONFIG_DEFAULT)
    == {
        "acp_url",
        "workspace_template",
        "idle_timeout_sec",
        "turn_timeout_sec",
        "echo_events",
        "show_thought",
    }
    and set(runner_mod.RUNNER_CONFIG_METADATA) == set(runner_mod.RUNNER_CONFIG_DEFAULT),
    str(sorted(runner_mod.RUNNER_CONFIG_DEFAULT)),
)
check(
    "面板字段都有说明文案",
    all(
        isinstance(runner_mod.RUNNER_CONFIG_METADATA[key], dict)
        and runner_mod.RUNNER_CONFIG_METADATA[key].get("description")
        for key in runner_mod.RUNNER_CONFIG_DEFAULT
    ),
    str(sorted(runner_mod.RUNNER_CONFIG_METADATA)),
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

# --------------------------------------------------- 16. 群内闸门与排队落库
async def turn_queue_checks():
    """群内闸门：立刻开跑 / 排队攒批 / 静默收尾 / 取消摘队；插话落库与启动恢复。"""
    queue_dir = Path(tempfile.mkdtemp(prefix="0528v2-queue-"))

    merged = turn_queue_mod.merge_prompt(
        [
            SimpleNamespace(label="张三", text=" 帮我看看 A "),
            SimpleNamespace(label="", text="顺便看下 B"),
            SimpleNamespace(label="李四", text="   "),
        ]
    )
    check(
        "merge_prompt：带表头、标发言人、跳过空发言",
        merged == "（群里攒了 3 条消息，一起看一下）\n[张三] 帮我看看 A\n顺便看下 B",
        merged,
    )
    check(
        "发言名取昵称，取不到退到发言者 ID、再取不到就空",
        runner_mod.speaker_name(FakeEvent(sender_name="小明")) == "小明"
        and runner_mod.speaker_name(FakeEvent(session_id="10001")) == "10001"
        and runner_mod.speaker_name(object()) == "",
    )

    # ── 闸门语义：第一条立刻开跑，后来的排队，上一轮结束攒成一批 ──
    queue = turn_queue_mod.TurnQueue()
    first = await queue.enter("group:a", "我先说", speaker="张三")
    check(
        "群里没人在跑：第一条消息立刻开跑",
        first.action == turn_queue_mod.ACTION_RUN
        and first.text == "我先说"
        and first.speakers == ("张三",),
        str(first),
    )
    check(
        "开跑后这个群算「在跑」，别的群不受影响",
        queue.busy("group:a") and not queue.busy("group:b"),
    )

    second = asyncio.create_task(queue.enter("group:a", "我说一句", speaker="李四"))
    third = asyncio.create_task(queue.enter("group:a", "还有我"))
    await asyncio.sleep(0.01)
    check(
        "同一群的后两条消息先排队（快照按群计数）",
        queue.waiting("group:a") == 2 and queue.snapshot() == {"group:a": 2},
        str(queue.snapshot()),
    )

    await queue.leave("group:a")
    slot_second, slot_third = await second, await third
    check(
        "上一轮结束：第一个等到的带队，提示词里带上这一批所有人",
        slot_second.action == turn_queue_mod.ACTION_MERGED
        and slot_second.text
        == "（群里攒了 2 条消息，一起看一下）\n[李四] 我说一句\n还有我",
        slot_second.text,
    )
    check(
        "带队者记着这一批的发言人（没名字的不算）",
        slot_second.speakers == ("李四",),
        str(slot_second.speakers),
    )
    check(
        "同一批里的其他人静默：什么都不发",
        slot_third.action == turn_queue_mod.ACTION_ABSORBED and slot_third.text == "",
        str(slot_third),
    )
    check(
        "带队者接着跑，闸门还在它手里",
        queue.busy("group:a") and queue.waiting("group:a") == 0,
    )
    await queue.leave("group:a")
    check("这一批也跑完，闸门空出来", not queue.busy("group:a") and queue.snapshot() == {})

    # ── 落库：排队时进库、攒进一批后出库 ────────────────
    store = store_mod.SessionStore(queue_dir / "queue.db")
    gated = turn_queue_mod.TurnQueue(store)
    await gated.enter("group:c", "第一条", speaker="张三")
    waiter = asyncio.create_task(gated.enter("group:c", "插话", speaker="李四"))
    await asyncio.sleep(0.01)
    rows = store.pending_entries()
    check(
        "排队的插话顺手记进库（群键 / 发言人 / 正文都在）",
        len(rows) == 1
        and rows[0]["key"] == "group:c"
        and rows[0]["label"] == "李四"
        and rows[0]["text"] == "插话",
        str(rows),
    )
    await gated.leave("group:c")
    await waiter
    check(
        "插话攒进这一批后，库里的记录删掉（不会重复处理）",
        store.pending_entries() == [],
        str(store.pending_entries()),
    )

    # ── 模拟「服务端半路被停掉」：库里留着没处理完的插话 ──
    store.add_pending("group:d", "断电前那句", "王五")
    revived = turn_queue_mod.TurnQueue(store)
    check(
        "启动时把库里留着的插话捞回队列（这个群不算「在跑」）",
        revived.restore() == 1
        and revived.waiting("group:d") == 1
        and not revived.busy("group:d"),
        str(revived.snapshot()),
    )
    led = await revived.enter("group:d", "新的消息", speaker="赵六")
    check(
        "恢复回来的插话由下一条消息带队发出去（标注出处）",
        led.action == turn_queue_mod.ACTION_MERGED
        and led.text == "（群里攒了 2 条消息，一起看一下）\n[王五] 断电前那句\n[赵六] 新的消息"
        and led.speakers == ("王五", "赵六"),
        led.text,
    )
    check("带队发出去之后，恢复的记录也从库里清掉", store.pending_entries("group:d") == [])
    await revived.leave("group:d")
    check("恢复的那一批跑完，闸门空出来", not revived.busy("group:d"))

    # ── 取消：等待者被撤掉时别卡住后面的人 ──────────────
    doomed = turn_queue_mod.TurnQueue(store)
    await doomed.enter("group:e", "跑着呢")
    cancelled_waiter = asyncio.create_task(
        doomed.enter("group:e", "这条会被撤", speaker="甲")
    )
    await asyncio.sleep(0.01)
    cancelled_waiter.cancel()
    try:
        await cancelled_waiter
    except asyncio.CancelledError:
        pass
    check(
        "等待被撤销：自己摘出队列，记录也删掉",
        doomed.waiting("group:e") == 0 and store.pending_entries("group:e") == [],
        str(store.pending_entries("group:e")),
    )
    later = asyncio.create_task(doomed.enter("group:e", "后来的", speaker="乙"))
    await asyncio.sleep(0.01)
    await doomed.leave("group:e")
    check(
        "撤掉的那条不带队，后面的照常接着上",
        (await later).action == turn_queue_mod.ACTION_MERGED,
    )
    await doomed.leave("group:e")

    # ── 坏库降级：读写失败只告警，队列照常 ──────────────
    broken = turn_queue_mod.TurnQueue(
        store_mod.SessionStore("/proc/1/nope/queue.db")
    )
    check("坏库也能恢复（当作没有记录）", broken.restore() == 0)
    await broken.enter("group:f", "照样跑")
    queued = asyncio.create_task(broken.enter("group:f", "照样排队"))
    await asyncio.sleep(0.01)
    check("坏库不挡排队", broken.waiting("group:f") == 1)
    await broken.leave("group:f")
    check("坏库也能攒批接着跑", (await queued).action == turn_queue_mod.ACTION_MERGED)
    await broken.leave("group:f")

    # ── Runner 侧：闸门真拦住远端；静默那条什么都不发、也不放门 ──
    release = asyncio.Event()

    class GatedClient(FakeClient):
        """提示词发出去后先卡住，等测试放行才回 idle：用来观察谁还拿着闸门。"""

        async def session_prompt(self, session_id, text, timeout=None):
            self.calls.append(
                ("session/prompt", {"sessionId": session_id, "text": text})
            )
            await release.wait()
            for update in self.updates:
                self._broadcast(session_id, update)
            return {}

    shared_store = store_mod.SessionStore(queue_dir / "runners.db")
    shared_queue = turn_queue_mod.TurnQueue(shared_store)
    group_key = "aiocqhttp:group:777"
    await shared_queue.enter(group_key, "上一轮还在跑", speaker="先说的")

    runner_clients = []

    def gated_factory(url, name):
        client = GatedClient(
            url,
            name,
            updates=[
                {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "msg_1",
                    "content": {"type": "text", "text": "好"},
                },
                {
                    "sessionUpdate": "state_update",
                    "state": "idle",
                    "stopReason": "end_turn",
                },
            ],
        )
        runner_clients.append(client)
        return client

    gate_pool = pool_mod.SessionPool(client_factory=gated_factory, idle_timeout=300)
    leader_runner = runner_mod.Alkaid0Runner(
        gate_pool, {"acp_url": "ws://plugin/acp"}, shared_queue
    )
    absorbed_runner = runner_mod.Alkaid0Runner(
        gate_pool, {"acp_url": "ws://plugin/acp"}, shared_queue
    )
    for runner, text, sender in (
        (leader_runner, "我来问 A", "小明"),
        (absorbed_runner, "顺便问 B", "小红"),
    ):
        await runner.reset(
            request=SimpleNamespace(prompt=text),
            run_context=SimpleNamespace(
                context=SimpleNamespace(
                    event=FakeEvent(
                        group_id="777",
                        session_id="777",
                        umo=group_key,
                        sender_name=sender,
                    )
                )
            ),
            agent_hooks=None,
            provider_config={
                "acp_url": "ws://panel/acp",
                "workspace_template": "/panel/{groupid}",
            },
            streaming=False,
        )

    async def _collect(runner):
        return [response async for response in runner.step_until_done()]

    leader_task = asyncio.create_task(_collect(leader_runner))
    absorbed_task = asyncio.create_task(_collect(absorbed_runner))
    await asyncio.sleep(0.05)
    check(
        "闸门把两条消息都拦住了：一个远端连接都还没建",
        runner_clients == [] and shared_queue.waiting(group_key) == 2,
        f"{len(runner_clients)} 个连接 / 排队 {shared_queue.waiting(group_key)}",
    )
    check(
        "排队的插话都落了库（这会儿服务端被停掉也不丢）",
        len(shared_store.pending_entries(group_key)) == 2,
        str(shared_store.pending_entries(group_key)),
    )

    await shared_queue.leave(group_key)
    absorbed_responses = await absorbed_task
    check(
        "被并进这批的那条静默收尾（链里留个空 Plain，避开内核「没返回结果」的兜底）",
        absorbed_responses == []
        and absorbed_runner.done()
        and absorbed_runner.get_final_llm_resp().role == "assistant"
        and _chain_text(absorbed_runner.get_final_llm_resp().result_chain) == ""
        and len(absorbed_runner.get_final_llm_resp().result_chain.chain) == 1,
        str(absorbed_responses),
    )
    check(
        "静默那条不许放开闸门（带队的还在跑，门该留在它手里）",
        shared_queue.busy(group_key) and not leader_task.done(),
        f"busy={shared_queue.busy(group_key)} leader_done={leader_task.done()}",
    )

    release.set()
    leader_responses = await leader_task
    sent_prompts = [
        call[1]["text"] for call in runner_clients[0].calls if call[0] == "session/prompt"
    ]
    check(
        "带队的那条把两条插话一起发出去（攒批提示词带发言人）",
        sent_prompts
        == ["（群里攒了 2 条消息，一起看一下）\n[小明] 我来问 A\n[小红] 顺便问 B"],
        str(sent_prompts),
    )
    check(
        "带队的照常拿到回复",
        [response.type for response in leader_responses] == ["llm_result"]
        and _chain_text(leader_responses[-1].data["chain"]) == "好",
        str([response.type for response in leader_responses]),
    )
    check(
        "只有一个远端连接（静默那条没去连服务器）",
        len(runner_clients) == 1,
        str(len(runner_clients)),
    )
    check(
        "整批跑完：闸门空出来、库里没有残留",
        not shared_queue.busy(group_key)
        and shared_queue.snapshot() == {}
        and shared_store.pending_entries(group_key) == [],
        str(shared_store.pending_entries(group_key)),
    )

    # ── 状态命令能看到闸门 ──────────────────────────────
    status_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    status_plugin._turn_queue = turn_queue_mod.TurnQueue()
    await status_plugin._turn_queue.enter("group:g", "跑着呢")
    queued_task = asyncio.create_task(status_plugin._turn_queue.enter("group:g", "排着"))
    await asyncio.sleep(0.01)
    status_text = status_plugin._render_status()
    check(
        "状态文本报出群内闸门与排队条数",
        "群内闸门: 插话排队 1 条（group:g 1 条）" in status_text
        and "连接池: 未启动" in status_text,
        status_text,
    )
    queued_task.cancel()
    try:
        await queued_task
    except asyncio.CancelledError:
        pass
    await status_plugin._turn_queue.leave("group:g")

    # 有连接时，那一行也要标出「跑着一轮、插话排队 N 条」
    busy_plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
    busy_plugin._turn_queue = turn_queue_mod.TurnQueue()
    busy_plugin._pool = pool_mod.SessionPool(
        client_factory=lambda url, name: FakeClient(url, name), idle_timeout=300
    )
    await busy_plugin._pool.acquire("group:g", url="ws://acp", cwd="/w/g")
    await busy_plugin._turn_queue.enter("group:g", "跑着呢")
    busy_waiter = asyncio.create_task(busy_plugin._turn_queue.enter("group:g", "排着"))
    await asyncio.sleep(0.01)
    busy_text = busy_plugin._render_status()
    check(
        "连接那一行也标出「跑着一轮、插话排队 N 条」",
        "；跑着一轮、插话排队 1 条；" in busy_text,
        busy_text,
    )
    busy_waiter.cancel()
    try:
        await busy_waiter
    except asyncio.CancelledError:
        pass
    await busy_plugin._turn_queue.leave("group:g")
    await busy_plugin._shutdown_pool()

    # ── 插件级：闸门与连接池共用一份记录，启动时捞回插话 ──
    plugin = Plugin0528V2(context=None, config={"enforce_prerequisite": False})
    check(
        "闸门懒创建 + 与连接池共用同一份 SQLite",
        plugin._ensure_queue() is plugin._ensure_queue()
        and plugin._ensure_pool().store is plugin._ensure_store()
        and plugin._ensure_store().path.name == "sessions.db",
        str(plugin._ensure_store().path),
    )
    seeded = plugin._ensure_store().add_pending("group:h", "重启前那句", "钱七")
    revived_plugin = Plugin0528V2(
        context=None, config={"enforce_prerequisite": False}
    )
    revived_queue = revived_plugin._ensure_queue()
    check(
        "插件启动时把库里留着的插话捞回闸门（并记下条数）",
        revived_queue.waiting("group:h") == 1
        and revived_plugin._restored_pending == 1,
        str(revived_queue.snapshot()),
    )
    check(
        "状态文本报出「启动时从库里恢复 N 条」",
        "启动时从库里恢复 1 条" in revived_plugin._render_status(),
        revived_plugin._render_status(),
    )
    plugin._ensure_store().forget_pending([seeded] if seeded is not None else [])
    await plugin.terminate()
    check("停用后闸门引用也放下", plugin._turn_queue is None)


asyncio.run(turn_queue_checks())

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
