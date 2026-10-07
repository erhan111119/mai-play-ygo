# 麦麦玩游戏王（Mai Play YGO）

> 麦麦玩游戏王插件：能让麦麦和群友开房间玩一局游戏王，并且包含游戏王卡牌查询功能。
> 一个插件，两件事：**陪群友打牌** + **帮你查牌**。
> 由「游戏王对局管家」与「游戏王百科检索」合并而来（v1.0.0），**两个虚拟客户端随插件自带**，开箱即用。

```
/开房        →  麦麦开一局，把服务器地址与房间口令发到群里，群友用 MDPro3 / YGOMobile 连进来打
/查房        →  出一张棋盘图（双方 LP、回合、场上每张卡；里侧的卡只画卡背）
/卡组 …      →  卡组池：内置卡组任选，也支持群友投稿卡组码（YDK / YDKE / 萌卡链接）
查卡/发卡图  →  LLM 工具：查卡牌效果、解析卡组码、发卡图（原「百科检索」的能力）
```

> 更细的用法（全部指令、排障、训练与调优）见 [USAGE.md](USAGE.md) 与 [docs/](docs/)；
> 本 README 只讲"这是什么、怎么装起来、怎么配"。

---

## 一、它是怎么打牌的

```
群友说"想打牌" ──► LLM 调用 ygo_duel_start ──► 插件起一副「ygopro 内核 + WindBot」并开闸门
                                                    │
                             地址 + 口令直接发到群里 ◄┘（不经过模型转述，免得被记错）
                                                    │
群友用 MDPro3 / YGOMobile 连进来 ──► 闸门（校验口令、双向透传、旁路记录）──► 内核
                                                    │
                                    打完之后：写一段对局总结发回群里（可关）
                                              并写进聊天记忆（可关）
```

* **一进程一房间**：每一局都是一个独立的 `ygopro.exe` + 一个 `WindBot.exe`，打完收摊，互不影响；
* **两个虚拟客户端在 `clients/` 里**（见 [`clients/README.md`](clients/README.md)）：
  内核 `ygopro`（服务端模式构建）+ 出牌大脑 `WindBot`（含调优过的各卡组执行器）。
  默认配置直接指向它们，路径写**相对于插件目录**的，所以整个插件目录拷到哪都能跑；
* **闸门**是唯一对外入口：群友连的是它，校口令、防串场、记录对局过程都在这一层；
* **出牌用哪套脚本**：新导入的卡组**一律用 WindBot 的通用脚本**（`duel.windbot_deck` 默认
  `generic`——那个喂什么卡表都能打的兜底脚本）。想让它打得更好，就给这副牌写一个**专属出牌脚本**：
  ⚠ **推荐让 AI agent 来写，而且要多轮迭代找问题、修复**（一次成型基本不好用）；
  写什么、怎么编译、怎么量出它真的更强，见 [`executors/README.md`](executors/README.md)。
  插件自己不写脚本、也不编译——导入卡组时只写一份"展开流程"资料给问 AI 用。

## 二、它是怎么查牌的

三个 LLM 工具（模型会在合适的时候自己调用）：

| 工具 | 干什么 |
|---|---|
| `ygo_card_search` | 按中/日/英文名查卡：类型、属性、种族、星数、攻守、完整卡文、FAQ 数（数据来自 ygocdb） |
| `ygo_deck_analyze` | 解析卡组码：YDK 文本 / `ydke://` 链接 / 萌卡分享链接，列出主卡组·额外·副卡组（同名合并计数） |
| `ygo_card_image` | 发卡图：**先用本机卡图**（`clients/art/` 或你自己客户端的卡图目录），没有再走在线 CDN |

**本地优先**：解析卡组码时先用插件自带的 `cards.cdb` 一次查全（离线、快），只有本机没有的新卡才问在线接口；
外网请求全部走 `duel/netguard`（只 https、只公网、限体积）。

## 三、安装

1. 把整个 `mai-play-ygo` 目录放进宿主插件目录：`<MaiBot>/plugins/mai-play-ygo/`；
2. 宿主里启用插件（`[plugin] enabled = true`，或在 WebUI 插件页打开）；
3. 第一次用之前，确认自带客户端在位：

```bash
python tools/setup_clients.py --check-only     # 应输出 ✔（缺文件会列出缺什么）
```

**关于两个客户端**：仓库里已经带了可直接运行的一份（约 95 MB）。换机器 / 升级内核时用

```bash
python tools/setup_clients.py --from <你的 ygopro 环境>    # 从另一套本机环境同步
```

同步来的 `WindBot.exe` 会优先取源码树的 Release 构建（那份才是带最新定制执行器的）。

### 可选：AI 教练（需要 WindBot 源码树）

插件默认用各卡组自带的出牌脚本。想让模型**每回合定一次战术**（`duel.ai_plan_coach = rule|llm`）
时才需要一份 WindBot 源码树：

```bash
git clone https://github.com/IceYGO/windbot
cd windbot && dotnet build WindBot.csproj -c Release
```

然后把 `paths.windbot_src_dir` 指到它。**不做这一步也不影响打牌**，只是没有这个高级能力；
自己或 agent 写的专属执行器也是编在这份源码树里（见 [`executors/README.md`](executors/README.md)）。

## 四、配置（`config.toml`）

分四节，每一项都带注释说明；**路径支持相对于插件目录**：

| 节 | 关键项 | 说明 |
|---|---|---|
| `[plugin]` | `enabled` / `config_version` | 开关与配置版本 |
| `[paths]` | `ygopro_dir` / `windbot_dir` / `cards_cdb` / `card_art_dir` / `windbot_src_dir` | 默认都指向插件自带 `clients/`；填绝对路径也认 |
| `[duel]` | **只有群主要用的十项**：`bot_name` / `public_host` / `public_port` / `listen_port` / `join_timeout_seconds` / `announce_result` / `summarize_with_ai` / `summary_prompt` / `taunt_enabled` / `taunt_lines` | 开房地址与等人超时、bot 名字、打完要不要总结、总结的提示词、局内要不要挑衅、挑衅说什么 |
| `[wiki]` | **只有两项**：`endpoint` / `timeout` | 查卡接口地址（换自建/镜像时改）与在线超时 |

`[duel]` 只剩群主要用的那几项：AI 教练、常驻房、空闲约战、训练用模型这些**内部参数仍在代码里**
（老配置里写过的键照旧能读、行为不变），但不再出现在插件配置页与这份模板上——配置页打开就是那几项，不用在几十项里找。
`[wiki]` 同理：结果条数上限、查询缓存时长、卡图 CDN 都写在代码里（`wiki.py` 顶部常量），不需要调。

**两个"要说人话"的配置项怎么填**：

* `summary_prompt`（对局总结的提示词）留空用内置那份。自己写可以用五个占位符——
  `{self_name}`（bot 在群里的名字）、`{report}`（对局记录原文）、`{verdict}`（机器判定的胜负）、
  `{winner}`（胜者名字，判不出时是空串）、`{turns}`（回合数，未知时是"未知"）。
  占位符写错（名字不对、少个花括号）时插件启动会在日志里报错、那一局就不发 AI 润色那段，
  不会拿半截提示词去问模型。**播报最前面那行胜负是代码写死的**，提示词怎么改都不会把它写反。
* `taunt_lines`（挑衅台词池）是个集合，一条一句；留空＝用内置的 20 句（`duel/taunts.py`）。
  填了就只用你写的这些，同一句不会连着说两次；`taunt_enabled = false` 时它不生效。

**网络**：群友在同一局域网时不用配；走内网穿透/公网时填 `public_host`（隧道域名）与 `public_port`
（隧道公网端口映射到本地闸门端口时填公网那个）。

**卡图**：想让 `/查房` 的图上带卡面，把 `paths.card_art_dir` 指到你客户端的卡图目录
（例如 MDPro3 的 `Picture/Art`，缺图自动退 `Picture/Closeup`）；不配也能用，只是图上画卡名框。
出图前会把卡图缩到 200 像素宽再嵌进页面（`duel/field_image.py` 的 `ART_MAX_WIDTH`），
并等页面彻底静下来才截屏——这是"卡图必须画全"的两条硬要求，改渲染参数前先读那段注释。

## 五、群里的指令

| 指令 | 作用 |
|---|---|
| `/开房` | 开一局并发出地址与口令（房间名、密码随机生成） |
| `/查房` | 出一张棋盘图：双方 LP、回合与阶段、场上每张卡（里侧画卡背）。**在哪个群问都出图**——本群有对局就画本群那局，否则画最近开的那局（图上写明是哪个群的）；没开房时提示怎么开 |
| `/收摊` | 立刻结束当前房间 |
| `/卡组` 系列 | 看卡组池、**用指令投稿卡组**（`/加卡组 <卡组码> [名字]`；模型不会自动收录群里的卡组码）、加入/移出随机池、固定某副牌 |
| `/出牌模式` `/我的名字` … | 出牌思路、bot 在对局里的名字等（管理员） |

完整清单见 [USAGE.md](USAGE.md)；所有组件都带 `ygo_` 前缀，避免与别的插件撞名。

## 六、目录结构

```
mai-play-ygo/
├── plugin.py          # 入口：插件类（对局管家）+ 生命周期
├── wiki.py            # 百科检索：三个 LLM 工具（混入主类）
├── config.toml        # 配置模板（每项都有注释；[duel] 只有群主要用的那几项、[wiki] 只有两项）
├── clients/           # 两个虚拟客户端（ygopro + WindBot，见其 README）
├── decks/             # 随仓库附带的十副卡表（作者自写，与 executors/ 里的执行器配套）
├── executors/         # 十份专属出牌脚本（C#）+ 安装/编写说明（推荐让 agent 多轮迭代着写）
├── duel/              # 对局运行时：房间、闸门、报文记录、查房出图、卡库、卡组码…
├── train/             # 训练/评估（擂台、AI 教练、计划层）
├── tools/             # 运维与开发工具（setup_clients / 出图预览 / 体检 / 分析…）
├── tests/             # 测试套件（pytest，281 条）
└── docs/              # 深入文档：交接说明、牌表体检、出图说明、教程…
```

## 七、开发

```bash
python -m pytest tests -q --asyncio-mode=auto                     # 281 条测试
python tools/check_card_coverage.py                               # 卡表 vs 执行器登记体检
python tools/check_engine_data.py --ygopro-dir clients/ygopro     # 内核/卡库/脚本是否同源
python tools/field_image.py --demo --render                       # 单独看查房出图长什么样
```

## 八、许可

插件本体 **MIT**。`clients/` 里的第三方组件许可见
[`clients/README.md`](clients/README.md)（ygopro 与卡牌脚本为 GPL-2.0，WindBot 为 MIT，
卡库/卡图为上游未声明许可——**公开发布前请自行判断如何取舍**）。
