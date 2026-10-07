# 麦麦玩游戏王（Mai Play YGO）

让麦麦和群友开房间打一局游戏王，顺带能查卡、发卡图。

**平台：只支持 Windows**（对局内核 `ygopro.exe` 与出牌引擎 `WindBot.exe` 都是 Windows 程序；
别的系统上插件照常加载，但开房会被明确拒绝，只有查卡与发卡图可用）。

**当前版本：1.0.4**

## 功能

- **开房对打**：群里 `@麦麦 想打牌` 或 `/开房` → 起一局，把服务器地址与房间口令发到群里，群友用 MDPro3 / YGOMobile 连进来打；打完把胜负与过程播报回群里（可关，也可交给模型润色）。
- **查房出图**：`/查房` 出一张棋盘图（双方 LP、回合与阶段、场上每张卡，里侧的只画卡背）。在哪个群问就在哪个群出图、并列出全部进行中的房间——这是明确的设计选择（开房是“麦麦和群友的公共牌局”）。
- **卡组池**：`/加卡组 <卡组码>` 投稿（YDK / `ydke://` / 萌卡链接），可放进随机池让麦麦随机抽，也可固定用一副。
- **百科检索**：查卡（中/日/英文名、效果、攻守、FAQ）与发卡图；卡图本地优先，缺了才走在线 CDN。

## 安装

1. 把整个 `mai-play-ygo` 目录放进 `<MaiBot>/plugins/`；
2. `cp config.toml.example config.toml`（仓库只放模板，本机配置已 gitignore）；
3. 在 WebUI 插件页启用；
4. 确认自带的两个客户端在位：`python tools/setup_clients.py --check-only`（缺了用 `--from <你的 ygopro 环境>` 同步一份）。

## 配置（`config.toml`）

`[paths]` 默认全部指向插件自带的 `clients/`（相对路径，整个目录拷到哪都能跑）。
`[duel]` 只有群主要用的十项：`bot_name`、`public_host` / `public_port`、`listen_port`、`join_timeout_seconds`、
`announce_result`、`summarize_with_ai`、`summary_prompt`、`taunt_enabled`、`taunt_lines`；`[wiki]` 只有 `endpoint` 与 `timeout`。
`summary_prompt` 留空用内置那份，可用五个占位符：`{self_name}` `{report}` `{verdict}` `{winner}` `{turns}`
（写错会在启动日志里报错，不会拿半截提示词去问模型）。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/开房` `/开局` | 开一局并把地址与口令发到群里 |
| `/查房` `/战况` `/局面` | 出棋盘图；其余房间再各报一段文字 |
| `/加卡组 <卡组码> [名字]` | 投稿卡组（**卡组码只能用指令导入**，模型不会自动识别） |
| `/卡组列表` `/卡组详情` `/删卡组` | 看池子 / 看详情 / 删投稿（`/删卡组` 是**管理员**操作） |
| `/加入随机` `/移出随机` `/随机池` `/固定卡组` | 随机池与固定卡组 |
| `/清空卡组` `/对局名字` | **只清本群的投稿** / 改对局内名字（都需操作权限） |

模型可调用的工具：`ygo_duel_start`、`ygo_duel_status`、`ygo_duel_stop`、`ygo_deck_list`、`ygo_card_search`、`ygo_card_image`。

**卡组池是共享的**：任何群看到的是同一份列表，谁都能用；`/清空卡组` 只清本群的投稿，`/删卡组` 是全局删除（所以要管理员）。

## 安全与边界

- 闸门默认监听 `0.0.0.0`，房间口令随机生成、由插件直接发到群里；建议只放行闸门端口（`listen_port`）。
- 出网请求都过 `duel/netguard`：只 http/https、只连公网地址（内网/回环/保留地址拒绝）、重定向逐跳校验、连接时校验目标 IP、响应体有上限（查卡 2 MiB、卡图 4 MiB）。
- 卡图与卡号只发给 `ygocdb.com` 与卡图 CDN；对局总结只把“双方统计”交给模型，聊天记录、用户 ID、密钥都不外传。

## 许可与随包内容

**发行结论**：插件本体 **MIT**（见 [LICENSE](LICENSE)），`clients/` 下第三方组件逐项来源见 [clients/README.md](clients/README.md)。

- `ygopro.exe` 与卡牌脚本是 **GPL-2.0**（随包附上游源码地址）；`WindBot.exe` 与 `executors/` 是 **MIT**（许可证随包）。
- `cards.cdb`、`expansions/`、卡图**上游从未声明许可**：作为社区资料**原样随包**（不主张权利、不单独收费）。
  权利方在仓库开 Issue 说一声，即从仓库与 Release 删除（不要求举证）；不想带的部署者可以删掉这两项，
  让使用者跑 `python tools/setup_clients.py --from <自己的 ygopro 环境>` 自行同步。

## 目录与开发

```
plugin.py / wiki.py           # 插件主体（开房、卡组池、指令与工具）与百科检索
clients/                      # 自带的两个虚拟客户端（ygopro + WindBot）
executors/                    # 十份专属出牌脚本（C#）与安装说明：推荐让 agent 多轮迭代着写
duel/ tools/ tests/ docs/     # 对局运行时、运维工具、测试、深入文档（出图与对局监视）
```

```bash
python -m pytest tests -q --asyncio-mode=auto                    # 测试
python tools/field_image.py --demo --render                      # 单独看查房出图长什么样
python tools/check_engine_data.py --ygopro-dir clients/ygopro    # 内核 / 卡库 / 卡脚本是否同源
```
