# 麦麦玩游戏王（Mai Play YGO）

> 让麦麦和群友开房间打一局游戏王。两个虚拟客户端（对局内核 ygopro + 出牌大脑 WindBot）随插件自带，开箱即用。

**平台：只支持 Windows。** 对局内核与出牌引擎都是 Windows 可执行文件，要由插件直接起进程；
在 Linux / macOS 上插件仍会加载，但只在启动日志里写明对局功能不可用、开房会被明确拒绝，
只有查卡与发卡图能用（不会静默失败到 "麦麦一声不响"）。

**当前版本：1.0.2** —— 1.0.0 是上架评审前的那份，1.0.1 收前三轮评审的修复，1.0.2 收第四轮
（见 [CHANGELOG](CHANGELOG.md)）。

## 功能

- **开房对打**：群里 `@麦麦 想打牌` 或直接 `/开房` → 插件起一局，把服务器地址与房间口令发到群里 → 群友用 MDPro3 / YGOMobile 连进来和麦麦打；打完把胜负与过程播报回群里（可交给模型润色成一段人话）。
- **查房出图**：`/查房` 出一张对局棋盘图——双方 LP、回合与阶段、场上每张卡（里侧的卡只画卡背），在哪个群问都出图。
- **卡组池**：群友发卡组码，用 `/加卡组` 收进池子（YDK / `ydke://` / 萌卡分享链接三种格式），可以放进随机池让麦麦随机抽，也可以固定用某一副。开局时用的**出牌脚本**：自带执行器优先，其次按卡表相似度挑，最后用通用脚本。
- **百科检索**：查卡（中/日/英文名、类型、属性、种族、攻守、完整卡文）与发卡图，卡图本地优先，缺了才走在线 CDN。
- **不主动说话**：开房发完地址口令之后就不再发言（局内挑衅默认关闭，要开可以配台词池）；也不会自己冒泡约战。

## 安装

1. 把整个 `mai-play-ygo` 目录放进宿主插件目录：`<MaiBot>/plugins/mai-play-ygo/`；
2. 复制配置模板并按需修改：`cp config.toml.example config.toml`（仓库里只放模板，本机的 `config.toml` 已 gitignore）；
3. 在 WebUI 插件页启用它（对应 `config.toml` 里 `[plugin] enabled = true`）；
4. 确认自带的两个客户端在位：

```bash
python tools/setup_clients.py --check-only          # 应输出 ✔（缺文件会列出缺什么）
python tools/setup_clients.py --from <你的 ygopro 环境>   # 缺了就从别的环境同步一份
```

插件会以子进程方式拉起这两个外部程序（参数来自配置与常量、不经 shell）：
`clients/ygopro/ygopro.exe` 是**对局内核**（一局一个进程，房间规则由插件传参），
`clients/windbot/WindBot.exe` 是**麦麦的出牌大脑**（按 `Deck=<出牌脚本名>` 挑脚本）。
仓库里的命令行工具（`tools/*.py`）也读同一份配置：没有 `config.toml` 时按 `config.toml.example` 的默认路径来。

## 配置（`config.toml`，模板见 `config.toml.example`）

`[paths]` 默认全部指向插件自带的 `clients/`（相对路径，按插件目录解析），整个插件目录拷到哪台机器都能跑。

`[duel]` 只有群主要用的十项：

| 配置 | 说明 |
| --- | --- |
| `bot_name` | bot 在对局中显示的名字（也可用 `/对局名字` 临时改） |
| `public_host` / `public_port` | 发给群友的地址与端口；留空自动探测局域网地址，内网穿透时填隧道域名与公网端口 |
| `listen_port` | 闸门监听端口，0 = 随机分配 |
| `join_timeout_seconds` | 开完房等群友进来的秒数，超时自动收摊 |
| `announce_result` | 打完是否把对局总结发到群里 |
| `summarize_with_ai` | 总结是否先交给模型写成一段人话 |
| `summary_prompt` | 总结的提示词，留空用内置那份 |
| `taunt_enabled` | 局内是否按概率说挑衅台词 |
| `taunt_lines` | 挑衅台词池（一条一句），留空用内置 20 句 |

`[wiki]` 只有两项：`endpoint`（查卡接口地址，默认 `https://ygocdb.com/api/v0/`）与 `timeout`（在线超时）。
在线卡图固定走 `cdn.233.momobako.com/ygopro/pics/<卡号>.jpg`（本机卡图优先，找不到才用它，域名写在 `wiki.py` 的 `CARD_IMAGE_CDN`）。

> 卡图：想让棋盘图上带卡面，把 `paths.card_art_dir` 指到你客户端的卡图目录（例如 MDPro3 的 `Picture/Art`）；不配也能用，只是图上画卡名框。

## 指令与工具

| 指令 | 作用 |
| --- | --- |
| `/开房` `/开局` | 开一局并把地址与口令发到群里（不依赖模型判断） |
| `/查房` `/战况` `/局面` | 出棋盘图；其余房间再各报一段文字 |
| `/加卡组 <卡组码> [名字]` | 投稿卡组（**卡组码只能用指令导入**，模型不会自动识别群里的卡组码） |
| `/卡组列表` `/卡组` | 看池子（内置 + 所有群的投稿，标出编号与随机池状态） |
| `/卡组详情 <编号或名字>` `/删卡组 <编号或名字>` | 看详情 / 删投稿（**不限本群**、需管理员权限，见下方「卡组池是共享的」；内置卡组删不掉） |
| `/加入随机 <编号或名字>` `/移出随机 <编号或名字>` `/随机池` | 随机池管理（`/加入随机 全部` 一次全放） |
| `/固定卡组 [编号]` | 固定麦麦用某副牌；不带编号 = 取消固定 |
| `/清空卡组` `/对局名字 <名字>` | **清空本群的投稿**（别群的与内置卡组不动） / 改对局内名字（都需操作权限） |

模型可调用的工具：`ygo_duel_start`（叫麦麦打牌时建房）、`ygo_duel_status`、`ygo_duel_stop`（收摊）、`ygo_deck_list`、`ygo_card_search`、`ygo_card_image`。

## 卡组池是共享的

卡组池不按群隔离：任何群看到的都是同一份列表（内置 72 副 + 所有群的投稿），谁都能用、也都能管理。

| 操作 | 范围 |
| --- | --- |
| `/删卡组 <编号>` | 删掉列表里那一副——**可能是别群投稿的牌**（回执里会标出来源与投稿人）。因为范围是全局的，这条指令**要管理员权限**。 |
| `/清空卡组` | **只清本群的投稿**，别群的投稿与 WindBot 自带卡组都不动（清空是破坏性操作，别群辛苦投的牌不该被不相干的人一键清掉）。 |
| `/固定卡组`、`/加入随机` | 全局设置，对所有群生效。 |

## 安全与边界

* **闸门默认监听 `0.0.0.0`**，房间口令是 6 位随机字符、由插件直接发到群里。开房打牌本来就要对外开口子；建议在防火墙上**只放行闸门端口**（`listen_port`），别把内核端口也暴露出去。
* **出网请求都过 `duel/netguard`**：只允许 http/https、只连公网地址（内网/回环/链路本地/保留地址一律拒绝）、最多跟 3 跳重定向且每跳重新校验、不允许走代理隧道。响应体默认上限 512 KiB，查卡接口给到 2 MiB、在线卡图给到 4 MiB。解析与连接之间的换址窗口也堵上了——**校验发生在连接那一刻的目标 IP 上**（`_pinned_connect`），DNS rebinding 换不到内网。
* 卡图与卡号只发给 `ygocdb.com` 与卡图 CDN；对局总结只把"双方统计"交给模型，聊天记录、用户 ID、密钥都不外传。
* 卡组码**只能用指令导入**，模型不会自动识别群里贴的卡组码。

## 目录结构

```
mai-play-ygo/
├── plugin.py           # 插件主体：开房、卡组池、指令与工具
├── wiki.py             # 百科检索：查卡、发卡图
├── config.toml.example # 配置模板（每项都有注释；本机的 config.toml 不进仓库）
├── clients/            # 自带的两个虚拟客户端（ygopro + WindBot），见其中的 README
├── decks/              # 随插件附带的十副卡表（与 executors/ 里的执行器配套）
├── executors/          # 十份专属出牌脚本（C#）与安装说明
├── duel/               # 对局运行时：房间、闸门、报文记录、查房出图、卡库、卡组码解析
├── tools/              # 运维工具：客户端同步、引擎体检、出图预览、录像分析、对局监视
├── tests/              # 测试套件（pytest）
└── docs/               # 查房出图与对局监视的说明、各卡组的教程摘要
```

## 开发

```bash
python -m pytest tests -q --asyncio-mode=auto          # 测试
python tools/field_image.py --demo --render            # 单独看查房出图长什么样
python tools/check_engine_data.py --ygopro-dir clients/ygopro   # 内核 / 卡库 / 卡脚本是否同源
python tools/room_watch.py --once                      # 只读扫描对局日志里的问题
```

想出牌更强，就给某副牌写一个**专属出牌脚本**：见 [`executors/README.md`](executors/README.md)（推荐让 AI agent 多轮迭代着写）。

## 许可与随包内容

插件本体 **MIT**（见 [LICENSE](LICENSE)）；`LICENSE` 只覆盖插件代码，`clients/` 下的第三方内容
各自保留原许可与来源，逐项清单见 [`clients/README.md`](clients/README.md)。

**发行结论**（2026-10-07 应市场评审要求定下，不再反复）：**继续随包分发**两个客户端与卡库。
理由是插件的主打能力就是"开箱即用"——没有 `cards.cdb`，投稿时的缺卡核对和麦麦出牌都做不了，
要求每个部署者自己凑一套 ygopro 环境等于把插件废掉一半。按权利状态分三类处置：

| 随包内容 | 权利状态 | 处置 |
| --- | --- | --- |
| `clients/ygopro/ygopro.exe`、`clients/ygopro/script/`（卡牌脚本） | GPL-2.0（上游已声明） | 随包分发，附上游源码地址（`clients/README.md`），满足 GPL 的源码可获取要求 |
| `clients/windbot/`、`executors/` | MIT | 随包分发，许可证随包（`clients/windbot/LICENSE`） |
| `clients/ygopro/cards.cdb`、`clients/ygopro/expansions/`、卡图 | **上游从未声明许可** | 作为社区资料**原样**随包（不主张任何权利、不单独收费），仅用于让插件跑起来；来源逐项写在 `clients/README.md` |

第三类是 ygopro 生态里长期公开流通、各家客户端都在分发的数据，本站照实标注而不假装有许可。
**权利方若要我们停止分发，在仓库开 Issue 说明即可，我们会立刻把对应文件从仓库与 Release 里剔除**
（不要求举证）。不想带第三类的部署者可以删掉 `clients/ygopro/cards.cdb` 与 `clients/ygopro/expansions/`，
改让使用者用 `python tools/setup_clients.py --from <自己的 ygopro 环境>` 从本机客户端同步，
或把配置里的 `[paths]` 直接指向自己客户端的同名文件。

`executors/` 里的十份 C# 执行器同属 WindBot 生态（MIT），其中 `MaiBotBrain.cs` 是留给客户端编译用的空转钩子。

