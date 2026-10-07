# 对局监视（`tools/room_watch.py`）——**只记录，不修**

> 用户口径（2026-10-07）：**"从现在开始一直监视对局，把问题都找出来记录下来。等我说要修了就一起修，
> 不要打扰正常游玩。"** 这份文档就是那条流水线的说明书。

## 它在做什么

一个常驻进程盯着宿主的日志目录 `<MaiBot>/logs/app_*.log.jsonl`（插件的日志与**内核 stdout**
都在这里面），把可疑的行抽出来落成两份产物：

| 产物 | 内容 |
|---|---|
| `temp/room-watch/findings.jsonl` | 一条一个 JSON：时间、类别、严重度、去重键、证据行原文、（有卡号就带）卡号 |
| `temp/room-watch/report.md` | 给人看的一页：类别计数 + 累计观察局数 + 最近 40 条 |
| `temp/room-watch/state.json` | 进度（每个日志读到哪、当前局、名册）；**`--once` 与常驻进程共用它** |

跑法：

```bash
python tools/room_watch.py --follow --interval 30   # 常驻（每 30 秒扫一次）
python tools/room_watch.py --once                   # 扫一遍就退出（定时任务用，接着上次进度）
python tools/room_watch.py --once --from-start      # 把历史日志也补扫一遍（补录用）
```

**零打扰约束**（改这个工具时别越过）：只读日志（不写、不锁、不删、不轮转）；不起房间、不发消息、
不调插件接口；默认 30 秒轮询、只读新增字节；**发现问题只记录，不自动修**。

## 记录了哪些类别

| 类别 | 严重度 | 判据 |
|---|---|---|
| 内核脚本报错 | high | `attempt to call an error function` / `[对局进程:err]` / `[string "./script/…`——**某张卡的脚本加载失败＝那张卡在对局里是白板**（2026-10-07 异解那局就是这么表现的：能召唤、能送墓，效果全空） |
| 插件报错 | high | 插件 logger 的 `error` 级 |
| 插件警告 | medium | 插件 `warning` 级里带"失败/错误/异常/超时/占用/崩溃"的（大模型拒答这类噪声只记 info、不落账） |
| 闸门/房间异常 | medium | 闸门与房间的启动/收摊报错 |
| 进房未开打 | medium | 玩家进房 3 分钟还没 `duel_started`（版本不一致/卡握手最常见） |
| 开局未结束 | medium | `duel_started` 之后 20 分钟还没 `duel_ended`（卡住/超时）；只报一次 |
| 整局无动作 | medium | 一局 ≥3 回合、某一侧**一件卡都没落到场上**（按 recorder 的落位行判） |
| 对局摘要 | info | 每局一行：时长、回合数、双方落位件数——**判断上面那些条目时要拿它当背景** |

**这是原始记录，不是结论**：每条都要先看证据行原文再判。已知的两类"看着像问题其实正常"：
① 对空白/无阻抗局的"零额外召唤"（反场面件要对手有场面才值得出）；② 大模型拒答导致的
"生成复盘点评被拒绝"（已被降噪成 info、不落账）。

## 要修的时候怎么交接

1. 看 `temp/room-watch/report.md` 的类别计数，挑一类（通常先修 `内核脚本报错`——它是"卡没效果"，
   直接影响群友体验）；
2. 用 `findings.jsonl` 里的**证据行原文**去定位：内核脚本报错带卡号，直接对上
   `script/c<卡号>.lua`；其余条目回宿主日志里按时间戳翻上下文；
3. 修完**不要删记录**：在 `report.md` 顶部追加一行"已修：<类别> <日期> <对应提交/命令>"
   （保留原始证据，便于回看"上次修的是不是同一个问题"）。

## 相关

* 引擎脚本类问题的根因与修法：记忆 `yugioh-engine-data-must-match-kernel`；
  自检：`python tools/check_engine_data.py --ygopro-dir <你的 ygopro 目录>`（不填就用插件自带的 `clients/ygopro`）。
* 真人局日志的审计规则（`--label WindBot` 那三条）另见 `tools/decision_audit.py`。
