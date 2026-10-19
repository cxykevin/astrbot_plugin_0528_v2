# astrbot_plugin_0528_v2

> AstrBot ↔ **Alkaid0 Coding Agent** 中继插件：把 AstrBot 的「Agent 执行器」接到 alkaid0 上，
> 走 **ACP（Agent Client Protocol）v2 over WebSocket**。

当前版本（v0.2.0）做了这些事：

1. **注册执行器**：启动时用前置插件 librunner 注册名为 `alkaid0` 的执行器 ——
   在 WebUI「AI → Agent 执行方式」里选中它，群消息就交给 alkaid0 处理；
2. **按群维护连接 + 群内串行**：**一个群 = 一条 WebSocket 连接 = 一个活跃 ACP 会话**
   （连接懒建立：某个群的第一条消息才连）。同一个群里**同时只跑一轮**：这一轮期间别人
   插的话先排队，等这一轮结束时**攒成一批**交给最早等着的那个接着跑（一次思考、一条回复），
   同一批里其余的消息不再单独回话 —— 群里不会为一句插话多出一条回复；排队的内容还会
   顺手记进 SQLite，AstrBot 半路被停掉也能在下次启动时捞回来（见下）；
3. **记住每个群的会话**：会话 ID 存在插件自己的 SQLite 里（`sessions.db`）。连接被回收、
   AstrBot 重启之后，下一条消息会用 `session/resume` 续接同一个 ACP 会话
   —— **只要会话，不回放历史**（不带 `replayFrom`；想切回更早的会话用 `/aresume`）；
4. **空闲回收 + `/areset`**：某个群超过阈值（默认 30 分钟）没有消息，就结束它的 ACP 会话
   并断开连接（记录留着，下次续接）；想从头开始就在群里发 `/areset`，
   它会抹掉记录并断开连接，下一条消息重新开会话。回收在后台进行，插件停用时断开全部连接。
5. **过程消息回显**：把 alkaid0 的过程消息（工具调用、子 Agent 切换、后台任务结束）
   回显成聊天里的小行（`🔧 / 🤖 / ✅`），和正文按到达顺序插在一起，
   这样群里能看出「它正在干什么」；不想要就把 `echo_events` 关掉，只发最终答复。
   工具审批提示（`⏳`）是必要信息，不受这个开关影响，始终会发。
6. **查看 / 切换模型与推理强度**：群里发 `/amodel`、`/aeffort` 列出本群会话可选的模型 /
   推理强度（`← 当前` 标出正在用的），带参数就切过去 —— 走 ACP 的
   `session/set_config_option`，**只改这一个会话**，别的群不受影响，也不用重开会话。
7. **看 / 换历史会话**：`/aresume` 列出本群工作目录下的会话（编号、标题、最后活动时间，
   `← 当前` 标出正在续接的那个），`/aresume <会话号 或 sessionId>` 就把本群切回那一个
   —— 只换「续接谁」，不复述聊天记录，alkaid0 侧的历史也不动。
8. **直接使唤远端**：`/acmd` 列出 alkaid0 自己的斜杠命令（`/compress`、`/init`、`/usage`…），
   `/acmd compress` 就把命令发给远端执行、把结果收回群里 —— 开头的 `/` 不用写（会自动补），
   参数原样保留。服务端声明 alkaid0 扩展能力（`v0.4`）时，命令说明按与
   [alcoh](https://github.com/cxykevin/alcoh) 同步的中文对照表显示。
9. **随时叫停**：群里发 `/astop` 就中止**当前这一轮** —— 正在生成的回复、正在跑的工具 / 终端
   都会被一起停下（走 ACP 的 `session/cancel` 通知），服务端以 `stopReason: "cancelled"` 收尾。
   它不动会话、不断连接（想重开会话用 `/areset`），这一轮已经说出来的内容仍会发到群里、
   末尾标一行 `🛑 本轮已终止`，排队的插话也不受影响。
10. **看上下文用量**：群里发 `/actx` 回一行 `context 6% (12840/200000)` —— 数字来自服务端每次
    模型响应后推的 `usage_update`（`used` = 最近一次响应的 token 总量（含输出）、`size` = 当前
    模型的上下文窗口），插件按会话缓存最新一份；百分比与 [alcoh](https://github.com/cxykevin/alcoh)
    同口径（整数截断、封顶 100%），窗口未知时只报已用量。
11. **新群自动建工作目录 + `/aworkdir`**：`session/new` 要求 `cwd` 已存在，所以第一次聊天失败时
    插件会用模板里占位符之前的那段（比如 `/root/alkaid0`）开一个临时会话、走 alkaid0 的
    `alk.cxykevin.top/fs/mkdir` 扩展（幂等，等价 `mkdir -p`）把目录建出来，再重试开会话 ——
    新群不用再手动建目录。还能用 `/aworkdir` 把本群绑到模板根下的任意子目录（同样会自动建），
    `/aworkdir reset` 回到模板默认。
12. **请求注入**：照 AstrBot 全局的 `provider_settings` 给发往 alkaid0 的提示词补上
    `prompt_prefix`、`[Reminder: @昵称(用户ID)]`（`identifier`）与 `Group name: 群名`
    （`group_name_display`）—— 走第三方执行器时内核不会替我们做这些。
    发出去之前会去掉消息最前面的 `/`（`/hello` → `hello`，否则会被 alkaid0 当成远端斜杠
    命令执行），去掉后按普通消息照常注入；`/acmd` 是显式发命令，不经过这里。
13. **工具审批走 `/aapprove`**：alkaid0 请求工具审批时，插件**不自动批准、也不主动拒绝**，
    只把请求挂起、往聊天里发一行「⏳ 工具审批：工具名（回复 /aapprove 允许）」；
    发 `/aapprove` 才回 `allow_once`。不批准、直接继续发消息就等于拒绝（alkaid0 侧
    自己判定），所以不需要「拒绝」命令。审批开关与策略都在 alkaid0 自己的配置里。

> [!NOTE]
> 这一版是**协议壳子**：ACP 传输（连接/握手/会话/提示词/通知/工具审批）、连接池与会话记录
> 已经打通，过程消息（工具调用 / 子 Agent / 后台任务）也会回显到聊天；终端输出细节、
> 历史回放等还没做（见文末「已知事项」）。

---

## 前置插件

AstrBot 内核没有插件级依赖声明（`metadata.yaml` 只支持 pip `requirements.txt`），所以
本插件自带一套最小可用的前置管理（`prerequisite.py`）：

| 阶段 | 行为 |
| --- | --- |
| 检测 | 看 `data/plugins/<前置名>/metadata.yaml` 是否存在（只看文件，不要求内核已加载） |
| 自动安装 | 缺失时先 `gh repo clone <owner/repo>`（复用你已登录的 gh），失败回退 `git clone <完整 URL>`；每条命令最长等 60 秒 |
| 就地启用 | 装好后在同一个 `Context` 上实例化前置的 `Star` 类并调用 `initialize()` —— **不重启 AstrBot 主进程** |
| 继续启用 | 两个前置都就绪后，本插件照常初始化（注册执行器 + 拉起连接池） |
| 失败上报 | 装不上则**停止工作**并抛出 `PrerequisiteError`，AstrBot 把它记进插件页的「加载失败插件」 |

依赖链与安装顺序（`DEFAULT_PREREQUISITES` 的顺序即安装/激活顺序）：

```
libi18nject  ←  librunner  ←  astrbot_plugin_0528_v2
```

其他要点：

- 自动安装**每个进程只尝试一次**，结果记录在
  `data/plugin_data/astrbot_plugin_0528_v2/prerequisite.json`，反复重载不会反复克隆；
- 换源（内网 / 镜像）改配置里的「前置插件来源」即可；
- `terminate()` 时本插件先注销自己的执行器、断开全部 ACP 连接，再把自己就地拉起的
  前置实例**逆序**释放（先 librunner 后 libi18nject），不留补丁。

---

## ACP 通信模型

```
群 A ──┐                                   ┌─ ACP 会话 sess_1:/root/alkaid0/A
       ├─ 每条消息 → 找到/建立该群的连接 ──┤
群 B ──┘                                   └─ ACP 会话 sess_2:/root/alkaid0/B
```

| 环节 | 做法 |
| --- | --- |
| 群键 | `event.unified_msg_origin`（`平台:类型:会话`）—— 群聊里天然一群一键，私聊也各自独立 |
| 建连 | `ws://…/acp?key=…` 连上后 `initialize`（`protocolVersion: 2`）握手 |
| 开会话 | 先查 SQLite 里的记录：有就 `session/resume` 续接，没有（或续接失败）才 `session/new`；`cwd` 由**工作目录模板**渲染（支持 `{groupid}`），本群绑过 `/aworkdir` 时用「模板根 + 绑定路径」；`session/new` 报目录不存在时，用模板根开临时会话发 `fs/mkdir`（幂等）建好再重试一次 |
| 一轮对话 | `session/prompt`（ACP v2 立即确认）→ 消费 `session/update` 通知 → `state_update idle` 结束 |
| 群内并发 | 一个群同时**只跑一轮**（`turn_queue.TurnQueue` 闸门）：消息先「进闸门」——没人在跑就立刻开跑，有人在跑就排队等它结束；这一轮结束时，队列里最早等到的那个拿到**攒批提示词**（`（群里攒了 N 条消息，一起看一下）` + 每行 `[发言人] 正文`）接着跑，同一批里其余的消息什么都不发（不占远端、也不多出一条回复）|
| 排队落库 | 排着队的插话顺手写进 SQLite（表 `turn_queue`：`conv_key` / `label` / `text` / `created_at`）；攒进某一批时删掉。AstrBot 半路被停掉时内存队列会没，这些记录还在 —— 下次启动捞回队列，等这个群**下一条消息**带队一起发出去 |
| 正文 | `agent_message_chunk` 累积（流式时逐块推给内核）；`agent_thought(_chunk)` 视配置走 reasoning 链 |
| 过程消息 | `tool_call_update`（工具调用）、`agent_status` 字段（子 Agent 切换）、`alk.cxykevin.top/shell_stop`（后台任务结束）按配置回显成 `🔧 / 🤖 / ✅` 小行，和正文按到达顺序插在一起；同一状态只发一次，工具流式预览不发。`requires_action`（等审批）单独处理——审批提示是必要信息，不受 `echo_events` 影响 |
| 会话配置项 | `session/new` / `session/resume` 响应与 `config_option_update` 广播里的 `configOptions` 都记在本群会话上（可选模型 / 推理强度就在这里）；`/amodel`（`configId: "model"`）与 `/aeffort`（`configId: "thought_level"`）都走 `session/set_config_option`（`type: "id"`），**只改这一个会话** |
| 工具审批 | 服务端发 `session/request_permission`：插件**挂起不回包**（`DEFERRED`），往聊天里提示「⏳ 工具审批：工具名（回复 /aapprove 允许）」；`/aapprove` 才回 `allow_once`。不发批准、继续发消息 = 拒绝（alkaid0 侧判定），客户端只在新一轮 prompt 前清掉挂起记录。审批开关在 alkaid0 自己的配置里 |
| 上下文用量 | 服务端在**每次模型响应**后推 `usage_update`（`used` = 最近一次响应的 token 总量（含输出）、`size` = 当前模型的 `TokenLimit`，真机一块 1M 窗口的模型报的是 `1024000`）；一次响应可能推好几条，只留最新一份。开会话与 `session/resume`（不带 `replayFrom`）都**不**推。`/actx` 把它渲染成 `context x% (used/size)`：**整数截断、封顶 100%**（与 alcoh 同口径），窗口未知（`size` 为 0 / 缺失）时只报已用量 |
| 断线/回收 | 空闲超时或插件停用 → 先 `session/close` 再断开 WebSocket；**会话 ID 留在库里**，下次消息 `session/resume` 续接（不带 `replayFrom`，不回放历史） |
| `/areset` | 抹掉该群的会话记录 + 断开连接 → 下次消息重新 `session/new`（alkaid0 侧聊天记录不删） |
| `/aworkdir` | 查看 / 设置本群的工作目录（相对模板根）：不带参显示模板、绑定与绝对路径；`/aworkdir .` 绑到根目录本身；`/aworkdir <相对路径>` 先经 `fs/mkdir` 建好再绑定（拒绝绝对路径 / 盘符 / `..`）；`/aworkdir reset` 恢复模板默认。绑定存在 SQLite 的 `conv_workdirs` 表；切换时断开本群旧连接（正在跑一轮时提示先 `/astop`） |
| `/astop` | 中止本群**正在跑的那一轮**：发 `session/cancel`（ACP 里的**通知** —— 没有 `id`、也不等回包）→ 服务端停掉本轮的语言模型请求与工具调用、以 `stopReason: "cancelled"` 收尾。不动会话、不断连接；排队的插话照常等下一批 |
| `/aapprove` | 放行本群当前挂着的工具审批：回 `{"outcome": "selected", "optionId": "allow_once"}`（服务端没给 `allow_once` 时选第一个可选项）。没有挂起请求 / 请求已失效都如实提示；不提供拒绝命令——不批准继续发消息就是拒绝 |
| `/aresume` | 不带参数：`session/list` 列出该工作目录下的会话（编号 / 标题 / 最后活动时间）；带会话号或完整 `sessionId`：先 `session/close` 当前会话、再 `session/resume` 目标会话（同一个 `cwd`），**成功之后**才更新本群记录（不带 `replayFrom`，不复述历史） |
| 命令列表 | 服务端在 `session/new` / `session/resume` 时广播 `available_commands_update`（`availableCommands`：`name` / `description` / `input.hint`），插件在**开会话之前**就挂好监听，把列表记在本群会话上；隐藏命令不在其中 |
| `/acmd` | 不带参数：列出上面这份命令列表（服务端声明 alkaid0 扩展能力 `alk.cxykevin.top/alkaid0/v0.4` 时按与 alcoh 同步的对照表把说明翻成中文，否则照原文）；带参数：补上开头的 `/` 后走 `session/prompt` 发给远端，收 `agent_message_chunk` 攒成本轮的回复（同一把会话锁，不会和正在跑的一轮打架） |

工作目录模板支持的占位符：`{groupid}`（群号；私聊没有群号时用会话号）、`{sessionid}`、
`{platform}`；不认识的占位符原样保留。模板里**第一个占位符之前**的那段叫「模板根」
（`/root/alkaid0/{groupid}` → `/root/alkaid0`），它必须在 alkaid0 侧已存在；模板渲染出来的
子目录（以及 `/aworkdir` 绑定的目录）插件会用 `alk.cxykevin.top/fs/mkdir` 自动建出来
（幂等，等价 `mkdir -p`）——`session/new` 本身不建目录，所以插件是在它报错后建好再重试。

---

## 目录结构

```
astrbot_plugin_0528_v2/
├── main.py            插件主体：前置检查 + 注册执行器 + 连接池生命周期 + `/0528v2` `/actx` `/areset` `/astop` `/aapprove` `/amodel` `/aeffort` `/aresume` `/aworkdir` `/acmd`
├── agent_runner.py    Alkaid0Runner（一轮对话 → 一次 ACP prompt turn）+ `run_slash_command`（一轮命令）
├── acp/
│   ├── protocol.py    ACP v2 常量与 JSON-RPC 编解码（无传输依赖）
│   ├── client.py      一条 WebSocket 连接的 JSON-RPC 2.0 收发
│   ├── pool.py        群 → 连接的表 + 空闲回收 + 会话续接/重置 + 配置项/命令列表缓存
│   ├── commands.py    远端命令：能力标记、`availableCommands` 解析、与 alcoh 同步的文案对照表
│   └── store.py       「群 → 会话 ID」「排队插话」「群 → 工作目录绑定」的 SQLite 记录（sessions.db）
├── turn_queue.py      群内闸门：一个群同时只跑一轮、插话排队攒批（排队顺手落库、启动时恢复）
├── prerequisite.py    前置插件管理（检测 / 安装 / 就地激活 / 落盘）
├── selftest.py        离线自测（隔离在临时 ASTRBOT_ROOT，不联网、不碰真实实例数据）
├── _conf_schema.json  插件配置（前置开关/来源 + ACP 地址/目录模板/空闲阈值…）
├── metadata.yaml      插件元数据
└── README.md
```

---

## 配置

两处都能配，规则是**执行器面板优先、插件配置兜底**：

- 执行器面板：WebUI「AI → Agent 执行方式」里选中 `Alkaid0` 之后的配置区
  （`agent_runner.config`，字段由本插件注册时声明）；
- 插件配置：插件页的「0528 v2」配置，即下面的 `_conf_schema.json`。

| 配置项 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enforce_prerequisite` | bool | `true` | 前置缺失时是否强制处理：开启则尝试自动安装（装不上就停用本插件并把原因显示在插件页）；关闭则只记警告并照常启用 |
| `prerequisite_librunner_repo` | 字符串 | `cxykevin/astrbot_plugin_librunner` | 自动安装 librunner 时使用的来源，也可写完整 git 地址（内网 / 镜像） |
| `prerequisite_libi18nject_repo` | 字符串 | `cxykevin/astrbot_plugin_libi18nject` | 自动安装 libi18nject 时使用的来源（它是 librunner 的前置，会先装） |
| `acp_url` | 字符串 | `ws://127.0.0.1:7433/acp` | alkaid0 的 ACP WebSocket 地址，形如 `ws://127.0.0.1:7433/acp?key=<key>` |
| `workspace_template` | 字符串 | `/root/alkaid0/{groupid}` | 每个群的工作目录模板；开会话（`session/new`）和续接（`session/resume`）都用它作为 `cwd`。第一个占位符之前那段是「模板根」（须已存在），子目录会被自动建出来（`fs/mkdir`） |
| `idle_timeout_sec` | int | `1800` | 群空闲这么久就回收连接；`0` 表示不自动回收 |
| `turn_timeout_sec` | int | `900` | 提交 prompt 后最多等多久（等到本轮 `idle`） |
| `echo_events` | bool | `true` | 是否把过程消息（工具调用 / 子 Agent / 后台任务结束）回显到聊天里；关闭后只发最终答复（工具审批提示始终发） |
| `show_thought` | bool | `false` | 是否把思考过程作为 reasoning 消息一起发出 |

> 面板上的「执行器配置」在切换执行器时会被内核用注册时的默认值覆盖（内核既有行为）；
> 常用参数建议也写进插件配置，兜底更稳。

---

## 开发环境

在 AstrBot 仓库根目录（`/path/to/AstrBot`）用该实例的 uv 环境执行：

```bash
# 自测：全程离线（不联网、不碰真实实例数据），覆盖前置机制、ACP 协议层、
# 群连接池与空闲回收、会话记录/续接/重置/换会话、会话级配置项与 /amodel /aeffort /aresume、
# 远端命令与 /acmd（能力标记、命令列表解析与刷新、文案对照表）、过程消息回显、
# 中止当前轮与 /astop（session/cancel 走通知、没在跑 / 发不出去都如实上报）、
# 上下文用量与 /actx（usage_update 解析、会话缓存与换/新开会话清空、渲染口径）、
# 群内闸门与排队落库（攒批提示词 / 静默收尾 / 取消摘队 / 落库往返 / 启动恢复 / 坏库降级）、
# 工作目录（模板根 / 相对路径校验 / 绑定解析 / 自动建目录重试）、
# 请求注入（provider_settings / 消息开头的 / 会被去掉）、
# 工具审批（挂起不回包 / 审批提示 / /aapprove 回包与失效）、
# /aworkdir 各分支、执行器注册/注销、状态文本、schema 一致性
uv run python data/plugins/astrbot_plugin_0528_v2/selftest.py

# 代码检查（selftest.py 里 10 个 E402 是刻意的 import 顺序，其余文件应零告警）
uv run ruff check data/plugins/astrbot_plugin_0528_v2/
```

运行时观察：

| 手段 | 看什么 |
| --- | --- |
| 聊天里 `/0528v2` | 两个前置是否已安装 / 已由内核加载、执行器是否已注册、当前几条 ACP 连接（每条的会话 ID / 当前模型 / 是否跑着一轮 / 插话排队几条 / 空闲秒数 / 工作目录）、群内闸门排了多少插话（启动时从库里恢复了几条）、会话记录条数与库路径 |
| 聊天里 `/amodel` | 本群会话可选的模型（`← 当前` 标出正在用的那个），以及怎么切；`/amodel 1`、`/amodel <名字>`、`/amodel 1/a` 都能切 |
| 聊天里 `/aeffort` | 本群会话可选的推理强度（`unset`/`low`/`medium`/`high`/`max`/`xhigh`，`← 当前` 标出正在用的那个）；`/aeffort high`、`/aeffort High` 都行 |
| 聊天里 `/aresume` | 本群工作目录下的会话列表（编号 / 标题 / 最后活动时间，`← 当前` 标出正在续接的那个）；`/aresume 9` 或 `/aresume sess_9:/path` 切过去 |
| 聊天里 `/acmd` | 远端命令列表（`/compress` `/help` `/usage` …，声明 alkaid0 扩展能力时中文说明）；`/acmd compress`、`/acmd mask add <value>` 直接发给远端执行 |
| 聊天里 `/aworkdir` | 本群的工作目录：模板、是否绑定（相对路径）、绝对路径、当前连接用的目录；`/aworkdir work/a` 设置并自动建目录、`/aworkdir .` 绑根、`/aworkdir reset` 回默认 |
| 聊天里 `/areset` | 重置本群的会话：抹掉记录的会话 ID 并断开连接，下一条消息重新开会话 |
| 聊天里 `/astop` | 中止本群正在跑的那一轮（思考中 / 跑工具时都行）：回执说明「已请求中止」、这轮的回复会在取消处停下；没在跑时只提示，不会乱发请求 |
| 聊天里 `/aapprove` | 放行本群当前挂着的工具审批（先看到「⏳ 工具审批：工具名」提示再发）；没有挂起 / 已失效都如实提示 |
| 聊天里 `/actx` | 本群会话的上下文用量，一行 `context 1% (11801/1024000)`；还没跑过一轮 / 刚 `/areset` / 刚被回收重连时如实说明「没有可报的用量」 |
| 聊天里 `/librunner` | librunner 侧状态（补丁是否生效、已注册的驱动器列表里应出现 `alkaid0`） |
| 日志 `grep 0528v2` | `ACP 连接池已启动`、`已注册执行器 alkaid0`、`已建立 ACP 会话`、`已续接 ACP 会话`、`已回收群 … 的连接`、`群 … 正在跑一轮，…的插话先排队（当前 N 条）`、`群 … 上一轮结束，把 N 条插话攒成一批接着跑（…）`、`群 … 这条消息已并进当前这一批里，不单独回话`、`从库里恢复 N 条排队插话`、`/areset 重置群 …`、`把群 … 的 model / thought_level 切成 …`、`/aresume 把群 … 的会话切到 …`、`/acmd 在群 … 执行了 …`、`命令 /compress 结束（stopReason=…）`、`/astop 已请求群 … 中止当前轮（会话 …）`、`/actx 群 … 上报用量 context …` |
| `data/plugin_data/astrbot_plugin_0528_v2/sessions.db` | 每个群记着的 ACP 会话 ID（表 `group_sessions`：`conv_key` / `session_id` / `cwd` / `updated_at`）、**排着队的插话**（表 `turn_queue`：`conv_key` / `label` / `text` / `created_at`）与**工作目录绑定**（表 `conv_workdirs`：`conv_key` / `rel` / `updated_at`） |
| `data/plugin_data/astrbot_plugin_0528_v2/prerequisite.json` | 每次自动安装的结果（`attempts` / `message` / `activated`） |

---

## 已知事项

- **目录会自动建，但模板根得先有**：`session/new` 的 `cwd` 必须已存在，插件在它报错后会用
  模板根开临时会话、发 `alk.cxykevin.top/fs/mkdir`（幂等）把子目录建好再重试；**模板根本身**
  （如 `/root/alkaid0`）推不出来或不存在时仍然会报错（建目录要先有一个能开会话的目录）；
  服务端不支持这个扩展（非 alkaid0）时同样原样报错；
- **请求注入依赖 `provider_settings`**：`prompt_prefix` / `identifier` / `group_name_display`
  都照 AstrBot 全局配置来（读不到就一个都不加）；群名优先用消息自带的，`N/A`/缺失时才调
  `get_group()` 查一次（仅 aiocqhttp 这类平台支持）；消息最前面的 `/` 会先去掉
  （`/hello` → `hello`）再注入；`/acmd` 是显式命令通道，不受影响；
- **`/aworkdir` 会断开本群旧连接**：绑定变了，下一条消息用新目录重建会话（记录里的旧会话
  `cwd` 对不上会被自动丢弃）；正在跑一轮时直接拒绝切换（提示先 `/astop`），不会打断它；
- **续接但不回放历史**：`session/resume` 故意不带 `replayFrom`（ACP v2 下省略即只重连）。
  `/aresume` 只负责「列会话 / 换会话」，**不会**把历史消息翻出来；真要回放得显式传
  `replayFrom: {"type": "start"}` 并用 `alk.cxykevin.top/session/cursor` 翻页，当前没做；
- **记录跟着工作目录模板走**：改了 `workspace_template` 之后，记录里的会话 `cwd` 会对不上，
  服务端回 `cwd not match`，插件会自动删掉旧记录改用 `session/new`；
- **终端内容仍不展示**：`alk.cxykevin.top/terminal_update` 的增量与快照只记日志
  （`🔧` 工具调用、`✅` 后台任务收尾已经回显；终端输出留给后续功能）；
- **模型与推理强度是会话级的**：`/amodel`、`/aeffort` 改的都是当前会话（服务端把选择存在这个
  会话上），连接空闲回收后按记录续接、选择跟着会话走；`/areset` 之后是新会话，两项都回到
  服务端默认值。可选值全部来自服务端（`configOptions`），被 `hide` 的模型不会出现在列表里；
- **`/acmd` 是原样转发**：不做命令白名单（服务端声明的隐藏命令如 `__alk_rewind` 也能发），
  认不认识由服务端说了算 —— 不认时报 `invalid command`，插件原样回给群里；命令轮和普通对话
  共用同一把会话锁，所以群里正在跑一轮时 `/acmd` 会等它先结束。异步命令（`/compress`、`/init`）
  也算一轮，等到 `idle` 才回话；
- **远端命令列表来自服务端**：`/acmd` 用的是 `available_commands_update` 下发的那份
  （服务端在开会话 / 续接时给）；还没收到过列表时只提示、不瞎猜（`/acmd <命令>` 照样能发）；
- **命令说明的中文对照表要跟着同步**：`acp/commands.py` 里的表与
  [alcoh](https://github.com/cxykevin/alcoh) 的 `internal/i18n/server.go` 对齐（而 alcoh 又要求与
  alkaid0 `server/actions/commands.go` 的命令描述**逐字一致**）。只在服务端声明 alkaid0 扩展能力
  （`alk.cxykevin.top/alkaid0/v0.4`）时才翻译，没收录的条目照原文显示 —— 宁可显示英文，也不猜译；
- **群内串行 + 攒批**：一个群同时只跑一轮（`turn_queue.py` 的闸门），插话排队、轮结束时攒成一批
  交给最早等着的那个接着跑；**被并进这批的消息什么都不发**（不占远端连接、也不多一条回复）。
  闸门只管普通消息，`/acmd` 不走它（它靠会话锁等正在跑的那一轮结束）；
- **排队插话的恢复要等下一条消息**：库里留着的插话只在插件启动时捞回队列，真正发出去得等这个群
  **再来一条消息**带队（没人来就一直等着，不会自己起一轮）。另外「攒批已经交出去、提示词还没发完」
  那一瞬间崩掉的话会丢那几条 —— 跟单条消息一样，重发就是了；
- **流式平台上的静默回合**：插话被并进别人那一批时本插件什么都不发；QQ/aiocqhttp 这类平台
  不会收到任何消息，而 WebUI 的 webchat 平台会把「本轮结束」当事件推一个空的 `complete`
  （前端可能显示一条空气泡）—— 群聊场景不受影响；
- **`/actx` 的数字是「最近一次模型响应」的用量**：服务端每次响应后推 `usage_update`（`used` 是那次
  响应的 token 总量、**含输出**；`size` 是当前模型的上下文窗口），插件只留最新一份 —— 所以它跟
  alkaid0 自己的自动压缩判断是同一个口径。开会话与续接（不带 `replayFrom`）都不推用量，因此
  **重连之后要等下一轮**才有新数字，`/areset` 之后是新会话、数字清空；`used` 理论上可以超过
  `size`（本轮输出也算进去了），这时百分比封顶显示 `100%`；
- **`/astop` 只中止当前这一轮**：走 ACP 的 `session/cancel`（**通知**），不动会话、不断连接，
  也不动排队的插话（那是别人还没被处理过的消息，这一轮停下后照常接着跑）。规范里服务端
  **应当**尽快停掉本轮的语言模型请求与工具调用 —— 本机 alkaid0 0.6 实测：正在跑的终端会被停、
  本轮以 `stopReason: "cancelled"` 收尾；但「提示词刚发出、服务端还没开始处理」的那一瞬间
  按下去可能打空（客户端侧无从确认，再按一次就行）。这条路径只在真实 alkaid0 上用探针验过
  （会话锁持有时发 cancel → 立刻以 cancelled 收尾），**还没在真实群聊里手动按过**；
- **工具审批不替用户点头**：`session/request_permission` 到达时插件只挂起（不回包）并在聊天里提示，
  用户发 `/aapprove` 才回 `allow_once`；不批准、直接继续发消息就等于拒绝（alkaid0 侧自己判定，
  插件这边只清掉挂起记录）。审批开关与策略都在 alkaid0 自己的配置里，插件没有「自动批准」开关，
  也不提供拒绝命令；还没批准时连接被回收 / 插件停用，挂起记录随连接一起丢（服务端那边会按超时处理）；
- **卸载顺序**：卸载本插件前先在 WebUI 把执行器切回内置 Agent，避免配置里残留
  `alkaid0` 类型（librunner 会按 `orphan_runner_policy` 降级，但切回去更干净）；
- **加载失败时的自救**：插件页的配置面板此时可能打不开，可以直接编辑
  `data/config/astrbot_plugin_0528_v2_config.json`，保存后重载插件。
