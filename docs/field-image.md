# 查房出图（`/查房` 的对局棋盘图）

> 2026-10-07 起：群里问 `/查房`，本流那个房间**发一张图**——双方 LP、第几回合与当前阶段、
> 场上每张卡（表侧用本机客户端的卡图、里侧画卡背）。这是用户要的"像游戏里那样的棋盘"。

## 长什么样

```
┌──────────────────────────────────────────────────────────────┐
│ 游戏王·当前局面  群「xxx」              第 7 回合  主要阶段2 │
│                              [魔陷行 5 格 + 场地区]  3100 ┐  │
│ 对手（画在上半，卡面朝下）   [怪兽行 5 格 + 额外怪兽区]      │  ← LP / 名字 / 卡组
│                                                          ┘  │
│ 我方（画在下半）            [怪兽行 5 格 + 额外怪兽区]      ┐  │
│                              [魔陷行 5 格 + 场地区]  6200 ┘  │
│ 里侧的卡只画卡背（不公开卡面）｜卡图来自本机 MDPro3          │
└──────────────────────────────────────────────────────────────┘
```

* **卡框按卡种上色**：怪兽橙、魔法绿、陷阱紫红；表侧怪右上角是攻守（连接怪只报攻击力，
  内核那个"守"字段放的是链接标记位，不是数值），守备表示左上角有「守」并让卡图转 90°；
* **里侧的卡只有卡背**（硬约束，见下）；没有卡图的卡画"卡名框"（不留白），
  连卡名都查不到就显示卡号。

## 三条硬约束（改这块时别越过）

1. **里侧一律画卡背**：图会发到群里，画卡面等于替对手公开信息。
   `tests/test_field_image.py::test_face_down_card_never_reveals_art_or_name` 钉着这条；
2. **缺图要兜底**：新卡/DIY 卡没有卡图时画卡名框，不能留白；
3. **出图绝不顶掉查房**：拿快照 / 渲染 / 发送任何一步失败都只记一条 warning，
   **退回纯文本**（`plugin.py` 的 `_send_field_image` 返回 False，`cmd_field` 走老路径）。

## 跑法

```bash
# 预览（不需要房间）：出一张演示局面，顺便用本机 Edge 渲染成 PNG
python tools/field_image.py --demo --render --out temp/field-demo.html

# 群里：直接 /查房（本流房间发图；别处还有房间就补一段文字）
```

渲染链条：`duel/field_image.py` 生成 HTML（卡图内联成 data URI）→ 宿主的
`render.html2png`（Playwright + 本机 Edge，`device_scale_factor=2`）→ `send.image`。
两个能力都要在 `_manifest.json` 里声明（已加）。

## 配置

| 配置项 | 作用 |
|---|---|
| `paths.card_art_dir` | 首选卡图目录（默认插件自带的 `clients/art/Art`；也可以指到你客户端的 `Picture/Art`） |
| `paths.card_art_fallback_dir` | 备用目录（默认 `clients/art/Closeup`；Art 里没有的卡常在这里） |

两处都找不到 → 画卡名框。

## 数据从哪来

* **格位与表示形式**：`duel/fieldstate.py` 的 `zones_of(seat)`（`(区域, 序号) → ZoneCard`）。
  表示形式来自 `MSG_SUMMONING/SPSUMMONING/FLIPSUMMONING`、`MSG_SET`、`MSG_MOVE` 与
  **`MSG_POS_CHANGE`**（2026-10-07 才接上：原来翻牌/改守备在台账里看不见）；
* **卡名 / 卡种 / 攻守**：`duel/cards.py` 的 `CardDatabase.card_details`（读 `cards.cdb`）；
* **LP / 回合 / 阶段**：记录器（阶段是 2026-10-07 起记的 `recorder.phase`）；
* **谁在行动**：`recorder.first_player_seat` + `turn_count` 的奇偶。
