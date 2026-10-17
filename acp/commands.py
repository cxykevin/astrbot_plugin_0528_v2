"""alkaid0 的斜杠命令（``/acmd`` 的底座）。

本模块只放「远端有哪些命令、它们的文案怎么显示」这些知识：

1. **能力标记**：服务端在 ``initialize`` 里声明 ``alk.cxykevin.top/alkaid0/v0.4``
   才代表它支持 alkaid0 私有扩展（扩展文档 §1.1；v0.5 / v0.6 是它之上的增量能力）；
2. **命令列表**：服务端在 ``session/new`` / ``session/resume`` 时广播
   ``available_commands_update``，每项形如
   ``{"name": "compress", "description": "Compress the history",
   "input": {"type": "text", "hint": "(no args)"}}``（隐藏命令不在列表里）；
3. **服务端文案的中文对照表**：alkaid0 的命令描述只有英文原文（服务端不做 i18n），
   客户端在服务端声明 v0.4 能力后按自己的界面语言翻译这些文本。

对照表与 alcoh 的 ``internal/i18n/server.go`` 同步（alcoh 那边又要求与 alkaid0
``server/actions/commands.go`` 的 ``commandMaps`` 逐字一致）：键是服务端英文原文，
值是中文译文；未收录的条目回退原文（宁可显示英文，也不猜译）。alcoh 更新对照表后，
这里要跟着同步。
"""

from __future__ import annotations

from typing import Any

#: alkaid0 私有扩展协议的能力标记；``initialize`` 的 ``capabilities`` 里出现即表示支持。
CAPABILITY_ALKAID0_V04 = "alk.cxykevin.top/alkaid0/v0.4"

#: 服务端命令描述的中文对照表（键 = alkaid0 下发的英文原文，逐字包含标点与大小写）。
#: 与 alcoh ``internal/i18n/server.go`` 的 ``serverZh`` 一致，每条上方的注释标明对应命令。
SERVER_ZH: dict[str, str] = {
    # /background
    "Set background mode on/off — keep session alive after all clients disconnect": "开启/关闭后台模式——所有客户端断开后保持会话存活",
    # /compress
    "Compress the history": "压缩历史记录",
    # /feedback
    "Submit feedback to the feedback server": "向反馈服务器提交反馈",
    # /help
    "Show this help message": "显示本帮助信息",
    # /index
    "Build codebase index (extract LSP symbols → submit embedding tasks). Subcommands: clean (clear db), status (show progress), cancel (stop running index), lsp-reset (reset LSP fail counters)": "构建代码库索引（提取 LSP 符号 → 提交嵌入任务）。子命令：clean（清空数据库）、status（查看进度）、cancel（停止正在运行的索引）、lsp-reset（重置 LSP 失败计数）",
    # /init
    "Analyze the codebase and generate an AGENTS.md guidance file": "分析代码库并生成 AGENTS.md 指导文件",
    # /mask
    "Manage custom mask values — add <value> masks a value outbound and restores it in the response, del <value> stops masking it": "管理自定义掩码值——add <value> 在发送时掩码该值并在响应中还原，del <value> 停止掩码",
    # /reload
    "Reload config file from disk": "从磁盘重新加载配置文件",
    # /s
    "Send a configured phrase — /s <short> expands the phrase to its full text and sends it to the model; /s with no args lists all configured phrases": "发送预置短语——/s <short> 把短语展开为完整文本后发给模型；不带参数则列出全部已配置短语",
    # /title
    "Set the conversation title, or reset it (no args) to fall back to the AI-generated title": "设置会话标题；不带参数则重置，回退到 AI 生成的标题",
    # /usage
    "Show global token usage statistics, or reset them": "查看全局 token 用量统计，或重置统计",
    # /version
    "Show Alkaid0 version information": "显示 Alkaid0 版本信息",
}


def has_capability(server_info: Any, capability: str) -> bool:
    """服务端的 ``initialize`` 响应里是否声明了某个能力标记。

    Args:
        server_info: ``initialize`` 的响应（``ACPClient.server_info``）。
        capability: 要查的能力标记（如 :data:`CAPABILITY_ALKAID0_V04`）。

    Returns:
        声明了为 ``True``；形状不对（没握手 / 没有 capabilities）时一律 ``False``。
    """
    if not isinstance(server_info, dict):
        return False
    capabilities = server_info.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    return capability in capabilities


def translate_description(text: Any) -> str:
    """把服务端的命令描述翻成中文；不是字符串或没收录的条目原样返回。"""
    if not isinstance(text, str):
        return ""
    return SERVER_ZH.get(text, text)


def _hint_of(item: dict) -> str:
    """命令的参数提示（``input.hint``，ACP v2 里 ``input`` 形如 ``{type, hint}``）。"""
    value = item.get("input")
    if not isinstance(value, dict):
        return ""
    hint = value.get("hint")
    return hint.strip() if isinstance(hint, str) else ""


def available_commands_of(payload: Any) -> list[dict]:
    """从 ``available_commands_update`` 的 update 里取命令列表。

    归一成 ``{"name", "description", "hint"}``：``name`` 去掉前导 ``/``，缺名字
    的条目直接跳过。``availableCommands`` 就在 update 顶层（与其它事件字段同层）。

    Args:
        payload: ``session/update`` 通知里的 ``params["update"]``。

    Returns:
        命令列表；形状不对时返回空列表。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("availableCommands")
    if not isinstance(raw, (list, tuple)):
        return []

    commands: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        name = name.strip().lstrip("/") if isinstance(name, str) else ""
        if not name:
            continue
        description = item.get("description")
        commands.append(
            {
                "name": name,
                "description": description.strip() if isinstance(description, str) else "",
                "hint": _hint_of(item),
            }
        )
    return commands


def slash_command_text(token: str) -> str:
    """把用户输入补成发给服务端的命令文本（缺开头的 ``/`` 就补上）。

    服务端判断「这一轮是不是命令」只看首字符是否为 ``/``，命令名与参数之间用
    **第一个空格**分隔（``server/actions/prompt.go``），因此参数里的空格原样保留。

    Args:
        token: 用户给的命令（``compress`` / ``/compress`` / ``s 干活``）。

    Returns:
        补好 ``/`` 的命令文本；空白输入返回空串。
    """
    text = (token or "").strip()
    if not text:
        return ""
    return text if text.startswith("/") else f"/{text}"
