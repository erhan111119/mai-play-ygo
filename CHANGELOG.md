# 更新日志

> 合并之前「游戏王对局管家」的历史（0.21.1 ~ 0.21.112）在
> [docs/changelog-yugioh-duel-arena.md](docs/changelog-yugioh-duel-arena.md)；
> 本文件从「麦麦玩游戏王」1.0.0 重新起版。

## 1.0.0

### 合并：一个插件 = 对局管家 + 百科检索

* 「游戏王对局管家」（yugioh.duel-arena）与「游戏王百科检索」（yugioh.wiki）合并为
  **麦麦玩游戏王（mai-play-ygo）**，对外只有一个插件：一个入口类、一份配置、一套测试。
* 百科检索的三个工具并入（`wiki.py` 的 `YugiohWikiTools`，混入主插件类；宿主用 `dir(instance)`
  采集组件，所以混入类的方法一样能被注册）：
  * `ygo_card_search`：查卡（中/日/英文名、类型、属性、种族、星数、攻守、卡文、FAQ 数）；
  * `ygo_deck_analyze`：解析卡组码（YDK / YDKE / **萌卡分享链接**三种格式自动识别）；
  * `ygo_card_image`：发卡图。
  工具名统一成 `ygo_` 前缀（本插件所有组件同一前缀，避免与别的插件撞名），参数与返回结构不变。
* **合一带来的三处改进**：
  1. 解析卡组码**本地优先**：先用插件自带的 `cards.cdb` 一次查全（离线、快），只有本机没有的卡
     才问在线接口——原实现是"每张卡一次 HTTP 请求"，一副 40 张的卡组要等十几秒；
  2. 发卡图**本地优先**：先找本机卡图（`clients/art/` 或你客户端自带的卡图目录），没有再走 CDN；
  3. 百科检索的外网请求全部改走 `duel/netguard`（只 https、只公网 IP、限制重定向与响应体积）。

### 两个虚拟客户端**随插件自带**（`clients/`）

* `clients/ygopro/`：对局内核（ygopro.exe、cards.cdb、strings.conf、lflist.conf、
  `script/` 约 1.4 万个效果脚本、`expansions/` 先行卡包）；
* `clients/windbot/`：出牌大脑（WindBot.exe——取源码树的 Release 构建，含各卡组定制执行器、
  `x64/sqlite3.dll`、`Decks/` 内置卡组、`Dialogs/` 与 MIT 许可文本）；
* **配置默认值全部改成"相对于插件目录"**（`clients/ygopro` / `clients/windbot` / `clients/art`），
  插件不再依赖机器的绝对路径——整个插件目录拷到别的机器也能跑；填绝对路径也照旧认。
* 新增 `tools/setup_clients.py`：换机器/升级客户端时一条命令同步（`--from <源环境>`），
  以及 `--check-only` 体检；`clients/README.md` 写清每个文件的来源与许可。
* 卡图与 WindBot 源码树**不进包**（前者太大且版权归属不明，后者约 250 MB 且只在编译自定义
  执行器/计划感知执行器时才需要），要用的把配置项指过去即可。

### 配置项再调整（2026-10-07 用户口径）

* **挑衅台词可配**：新增 `duel.taunt_lines`（集合，一条一句），留空用内置 20 句。
  以前台词只能改 `duel/taunts.py` 的源码，现在配置页就能改。
* **对局总结的提示词可配**：新增 `duel.summary_prompt`，留空用内置那份；
  可用占位符 `self_name` / `report` / `verdict` / `winner` / `turns`。
  占位符写错（名字不对、少个花括号）时**启动就报错**、那一局不发 AI 润色那段——
  不拿半截提示词去问模型，也不静默失败（启动时试渲染一次，日志里直接点名哪个占位符不对）。
* **开房等人超时回到配置面**：`join_timeout_seconds` 重新可见（默认 300 秒）。
* **百科侧收敛成两项**：`wiki.endpoint`（查卡接口地址，换自建/镜像用）+ `wiki.timeout`（在线超时）。
  原来的 `max_search_results` / `send_card_image` / `cache_ttl` / `api_timeout` 去掉：
  条数上限与缓存时长改成 `wiki.py` 里的常量，`send_card_image` 本来就是**没人用的死配置**。
* `[duel]` 的可见面因此定为十项（`bot_name` / `public_host` / `public_port` / `listen_port` /
  `join_timeout_seconds` / `announce_result` / `summarize_with_ai` / `summary_prompt` /
  `taunt_enabled` / `taunt_lines`），护栏测试同步更新。

### 修好的问题

* **`/查房` 在别的群不出图**：以前只认本流的房间，在没开房的群里问就只剩文字。
  现在本群没开打时画**最近开打的那局**，图上写明是哪个群的（群名来自 `chat.get_all_streams`）。
* **棋盘图常常缺卡图**（"没渲染成功就发出来"）：两个原因一起修——
  ① 卡图原来按 CSS 背景图嵌**原尺寸**（一副场两兆多 base64），Chromium 还没解码完就可能被截屏；
  现在缩到 200 像素宽（`ART_MAX_WIDTH`，pillow 缩放 + 结果缓存）并改用 `<img>`，
  ② 渲染参数补上 `wait_until="networkidle"` 与 `wait_for_timeout_ms=300`，等页面彻底静下来再截。
  实测满场 11 张卡面的 HTML 从兆级降到 200 KB 级，全部卡面都画得出。
* **`ygo_card_image` 永远发不出卡图**：函数开头 `del kwargs`、后面又 `.get("stream_id")`，
  一发成功就 `UnboundLocalError` 被外层 except 吞掉（群里只看到"取卡图失败"）。
  改成统一的 `_session_from(kwargs)` 取会话号，顺手去掉那个没用的 `getattr(self, "_stream_id", None)`。
* **擂台回填决策日志缺导入**：`train/arena.py` 用了 `duel_outcome_text` 却没 import，
  走到那一行就 `NameError`（被 except 吞成"决策日志回填失败"）。

### 瘦身：只留群主要用的东西

* **对局配置只暴露群主要用的项**（见上一节：现在是十项）。
  AI 教练、常驻房、空闲约战、训练用模型、房间规则这些**内部参数仍在代码里**（老配置里的键照旧
  能读、行为不变），但标了 `hidden`——插件配置页与 `config.toml` 模板上不再出现。
* **去掉"自动写脚本"整条链路**：`duel/script_gen.py`（让模型写 C# 执行器并编译）、
  `tools/generate_deck_script.py`、`/重生成脚本` 指令，以及
  `auto_generate_script` / `script_model` / `script_max_tokens` / `search_endpoint` /
  `search_allow_private_host` 五个配置项全部删除。
  **新导入的卡组一律用 WindBot 的通用脚本**；想要专属执行器改用 AI agent 按
  `executors/README.md` 多轮迭代着写（一次成型基本不如通用脚本）。
  卡牌清单这份口径（卡号+卡名+效果）挪到 `duel/cards.py` 的 `collect_card_info()`，写打法数据
  的 CLI 继续用同一份。
* **去掉"自动导入卡组码"**：`ygo_deck_submit` 工具删除，**卡组码只能用指令投稿**
  （`/加卡组 <卡组码> [名字]`）。
* 仓库附带作者自写的**十副卡表**（`decks/`）与配套的**十份执行器**（`executors/`，含
  模板补丁、宿主集成补丁与编写说明）；除这十副与 WindBot 自带卡组外不再保留投稿卡组。
* 测试套件随之收敛：脚本生成相关用例删除，投稿类用例改走指令入口，新增两条护栏
  （"可见配置项恰好是那七项"、"模型手里没有导入卡组的工具"）；顺手把"默认指向自带 clients"
  之后失效的用例改成显式造环境（临时卡库 / 空 WindBot 目录），281 条全绿。

### 配置与清单重写

* 版本号重置为 **1.0.0**（`_manifest.json` 与 `[plugin] config_version` 同步）；
* 插件 id 改为 `mai-play-ygo`、名称改为**麦麦玩游戏王**、能力声明按合并后的实际用法重列
  （`send.text` / `send.image` / `llm.generate` / `maisaka.context.append` /
  `chat.get_all_streams` / `render.html2png`）；
* `config.toml` 重新生成：`[plugin]` / `[paths]` / `[duel]` / `[wiki]` 四节，
  每一项都带"默认值 + 作用"注释，**不含任何个人机器路径**；
* 适配当前 SDK 规范（`sdk.min_version = 2.5.0`、`plugin_type: extension`、
  组件元数据带 `visibility="visible"`）。

### 顺带修好的（合并期间发现）

* `PathsConfig` 新增 `resolved_*()` 系列：把配置里的相对路径按插件目录解析；
  **空值返回 `None` 而不是 `Path("")`**（后者会变成当前工作目录，把"没配"悄悄当成"配了"）。
* 生命周期测试的假会话改成照真实 API 写（`session.recorder` 是**方法**不是属性）——
  这个不匹配曾经让 `/查房` 出图在真机上静默退文本。
