# astrbot_plugin_0528_v2

> 开发中的 AstrBot 插件骨架：启动时自动确认两个**前置插件**（`astrbot_plugin_librunner`
> 与 `astrbot_plugin_libi18nject`），缺失就自动下载并**就地启用**，装不上则停止工作并把
> 原因交给 WebUI。

> [!NOTE]
> 当前版本（v0.1.0）只搭好了开发环境与前置机制，业务功能写在 `main.py` 的
> `Plugin0528V2.initialize()` 里（已标注 `TODO(业务)`）。

---

## 前置插件

AstrBot 内核没有插件级依赖声明（`metadata.yaml` 只支持 pip `requirements.txt`），所以
本插件自带一套最小可用的前置管理（`prerequisite.py`）：

| 阶段 | 行为 |
| --- | --- |
| 检测 | 看 `data/plugins/<前置名>/metadata.yaml` 是否存在（只看文件，不要求内核已加载） |
| 自动安装 | 缺失时先 `gh repo clone <owner/repo>`（复用你已登录的 gh），失败回退 `git clone <完整 URL>`；每条命令最长等 60 秒 |
| 就地启用 | 装好后在同一个 `Context` 上实例化前置的 `Star` 类并调用 `initialize()` —— **不重启 AstrBot 主进程** |
| 继续启用 | 两个前置都就绪后，本插件照常初始化 |
| 失败上报 | 装不上则**停止工作**并抛出 `PrerequisiteError`，AstrBot 把它记进插件页的「加载失败插件」 |

依赖链与安装顺序（`DEFAULT_PREREQUISITES` 的顺序即安装/激活顺序）：

```
libi18nject  ←  librunner  ←  astrbot_plugin_0528_v2
```

其他要点：

- 自动安装**每个进程只尝试一次**，结果记录在
  `data/plugin_data/astrbot_plugin_0528_v2/prerequisite.json`，反复重载不会反复克隆；
- 换源（内网 / 镜像）改配置里的「前置插件来源」即可；
- `terminate()` 时本插件会把自己就地拉起的前置实例**逆序**释放（先 librunner 后
  libi18nject），不留补丁。

---

## 目录结构

```
astrbot_plugin_0528_v2/
├── main.py           插件主体：前置检查 + `/0528v2` 状态命令 + 业务 TODO
├── prerequisite.py   前置插件管理（多前置版：检测 / 安装 / 就地激活 / 落盘）
├── selftest.py       离线自测（隔离在临时 ASTRBOT_ROOT，不联网、不碰真实实例数据）
├── _conf_schema.json 插件配置（强制前置开关 + 两个来源）
├── metadata.yaml     插件元数据
└── README.md
```

---

## 开发环境

在 AstrBot 仓库根目录（`/path/to/AstrBot`）用该实例的 uv 环境执行：

```bash
# 自测：全程离线，ASTRBOT_ROOT 指向临时目录，用例覆盖前置检测/来源覆盖/
# 本地 git 仓库演练安装/装不上时的停止工作/terminate 释放顺序等
uv run python data/plugins/astrbot_plugin_0528_v2/selftest.py

# 代码检查
uv run ruff check data/plugins/astrbot_plugin_0528_v2/
```

运行时观察：

| 手段 | 看什么 |
| --- | --- |
| 聊天里 `/0528v2` | 两个前置是否已安装 / 已由内核加载、强制开关、最近一次启动检查结果 |
| 聊天里 `/librunner` | librunner 侧状态（补丁是否生效、已注册的驱动器列表） |
| 日志 `grep 0528v2` | `已就地激活前置插件`、`前置插件全部就绪`、`插件已启用` |
| `data/plugin_data/astrbot_plugin_0528_v2/prerequisite.json` | 每次自动安装的结果（`attempts` / `message` / `activated`） |

---

## 配置

| 配置项 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enforce_prerequisite` | bool | `true` | 前置缺失时是否强制处理：开启则尝试自动安装（装不上就停用本插件并把原因显示在插件页）；关闭则只记警告并照常启用 |
| `prerequisite_librunner_repo` | 字符串 | `cxykevin/astrbot_plugin_librunner` | 自动安装 librunner 时使用的来源，也可写完整 git 地址（内网 / 镜像） |
| `prerequisite_libi18nject_repo` | 字符串 | `cxykevin/astrbot_plugin_libi18nject` | 自动安装 libi18nject 时使用的来源（它是 librunner 的前置，会先装） |

---

## 已知事项

- **业务功能待实现**：当前只有前置机制与状态命令；
- **卸载顺序**：本插件会用到 librunner 提供的执行器注册能力，卸载前请先在 WebUI 把
  执行器切回内置 Agent，避免配置里残留无法识别的类型；
- **启动耗时**：前置缺失且网络不通时，这个插件的加载会多等几条命令的超时（默认单条
  60 秒），可以用镜像来源或临时关闭「强制前置插件」规避；
- **加载失败时的自救**：插件页的配置面板此时可能打不开，可以直接编辑
  `data/config/astrbot_plugin_0528_v2_config.json`，保存后重载插件。
