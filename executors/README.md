# 十副牌的 WindBot 出牌脚本（C#）

本目录收的是**十副牌各自的本家执行器**：它们编译进 `WindBot.exe`，WindBot 用反射扫
`[Deck(...)]` 属性注册，对局时用命令行参数 `Deck=<注册名>` 选中（插件侧见 `paths.windbot_executable`
与 `duel/windbot_decks.py`）。

> ⚠ **名字没注册时 WindBot 不报错**：`DecksManager.Instantiate` 会随机挑一个 `Normal` 档的执行器顶上
> （对局照打，但打出来的数字代表的是别的脚本）。本项目已经因此作废过一整批评估数据，所以留档的
> `host-integration.patch` 里给 `DecksManager` 加了一条告警 + 一行「这一局用的是哪份脚本」的日志。

## 1. 十份脚本

| 注册名（`Deck=`） | 文件 | 对应卡表 | 定位 |
|---|---|---|---|
| `RaiseMoon` | `RaiseMoonExecutor.cs` | `decks/RaiseMoon.ydk` | 升辉月 / 盈彩月夜：本家一张检索都没有，抽到下级就自跳、靠两张引擎魔法滚牌；终场做 R7「一掷乾坤」，而它**只在额外怪兽区有效果**（所以这份会打开 `GameAI.PreferExtraMonsterZone`） |
| `WitchcraftShop` | `WitchcraftShopExecutor.cs` | `decks/WitchcraftShop.ydk` | 魔女术（202605 商标女巫版）：下级「解放自己 + 丢 1 张魔法 → 从卡组拉另一只本家」，真正赚卡靠融合（代理师傅 / 学童组合 / 大魔女 桑德里永） |
| `ToonShop` | `ToonShopExecutor.cs` | `decks/ToonShop.ydk` | 卡通：靠「完美世界 卡通世界」每回合最多 3 次检索，漫画猫/邪魔箱在双方回合动，青眼卡通究极龙给全场直击当斩杀点 |
| `KillerTune` | `KillerTuneExecutor.cs` | `decks/KillerTune.ydk` | 杀手级调整曲：本家下级让**手卡里的调整也能当同调素材**，下级作素材送墓会触发破坏；能斩杀就直接进战阶 |
| `SkyStrikerShop` | `SkyStrikerShopExecutor.cs` | `decks/SkyStrikerShop.ydk` | 闪刀姬：主怪兽区要尽量空着（「闪刀」魔法要求主区无怪），链接怪优先站额外怪兽区；零衣 → 飒天 → 燎里/雫空 |
| `TraptrixRagnaraika` | `TraptrixRagnaraikaExecutor.cs` | `decks/TraptrixRagnaraika.ydk` | 虫惑魔 + 蕾祸：塞拉（LINK1）盖陷阱/拉虫惑魔，毬首检索蕾祸；走线期间自肃锁昆虫·植物·爬虫，终场 LINK5 大王鬼牙 |
| `Yaosheng` | `YaoshengExecutor.cs` | `decks/Yaosheng.ydk` | 耀圣 + 狱神：三只 6 星本家**只能特召到中央主要怪兽区**，中央区只有一个 → 每次只站一只，靠蕾吉娜/回乡/狂奏腾位 |
| `KezmoYixiangming` | `KezmoYixiangmingExecutor.cs` | `decks/KezmoYixiangming.ydk` | 刻魔异响鸣：灵摆起手 → 刻魔融合 → 链接段 → 雷火沸动超量；雷火四只的手卡特召带「只能出 4 阶」自肃，所以顺序必须是链接段在前 |
| `Kashtira` | `KashtiraExecutor.cs` | `decks/Kashtira.ydk` | 全盛俱舍：R7 香格里拉茧 / 阿莱斯哈特，里侧除外压制。**在上游 IceYGO/windbot 同名脚本上加写**（补齐卡表里有、原脚本没登记的卡） |
| `Tearlaments` | `TearlamentsExecutor.cs` | `decks/Tearlaments.ydk` | 全盛珠泪：堆墓融合水仙女人鱼 / 鲁莎卡人鱼，配「守墓的陷阱」+「现世与冥界的逆转」封对方墓地。同样**在上游同名脚本上加写** |

各份文件头部都有长篇注释：引擎怎么转、踩过哪些坑、每张卡的卡号来自哪套印刷号。改之前读它。

卡组编号（日志/工具里用的 `--deck-id`，见 `tools/max_board.py`）：
`88` RaiseMoon，`93` WitchcraftShop，`94` ToonShop，`95` KillerTune，`96` SkyStrikerShop，
`98` TraptrixRagnaraika，`99` Yaosheng，`100` KezmoYixiangming，`101` Kashtira，`102` Tearlaments。

## 2. 怎么装

```bash
# 0) 目录：<windbot-src> = 你的 WindBot 源码树（`git clone` 后自己 build 出来的那份），<plugin> = 本插件仓库根

# 1) 先打宿主侧的两个 patch（对上游 HEAD 的 diff，git apply 用默认 -p1，从源码树根跑）
cd <windbot-src>
git apply <plugin>/executors/DefaultExecutor.patch
git apply <plugin>/executors/host-integration.patch

# 2) 十份执行器 → Game/AI/Decks/
cp <plugin>/executors/*Executor.cs Game/AI/Decks/

# 3) AI 问答通道 → Game/AI/（KillerTuneExecutor.cs 直接调用它，缺了编译不过）
cp <plugin>/executors/MaiBotBrain.cs Game/AI/

# 4) 编译
dotnet build WindBot.csproj -c Release
```

编译完**还要让对局实际用的那份 exe 变成这次产物**（`<windbot-src>/bin/Release/WindBot.exe`
与 `paths.windbot_executable` 常常不是同一个目录，不一致时源码改动在真实房间里一行都不生效）：

```bash
uv run python tools/check_windbot_build.py            # 不一致会报错并给出命令
uv run python tools/check_windbot_build.py --deploy   # 备份后直接覆盖
```

### 两个 patch 分别是什么

| 文件 | 内容 | 为什么装脚本必须带上 |
|---|---|---|
| `DefaultExecutor.patch` | 上游 `Game/AI/DefaultExecutor.cs` 的 diff（+66/-4） | 七份执行器（RaiseMoon / WitchcraftShop / ToonShop / KillerTune / SkyStrikerShop / TraptrixRagnaraika / KezmoYixiangming）用了它新增的成员：`EnemySummonedThisTurn`、`MulcharmyReady()`、`MulcharmyWaitSummon`。缺了**编译不过** |
| `host-integration.patch` | `Game/GameAI.cs`、`Game/AI/Executor.cs`、`Game/AI/DecksManager.cs`、`WindBot.csproj` 四个上游文件的 diff | ① `GameAI.PreferExtraMonsterZone` 字段——RaiseMoon / SkyStrikerShop / TraptrixRagnaraika 三份在构造函数里打开它（把链接怪/超量怪优先放额外怪兽区），缺了**编译不过**；② `WindBot.csproj` 加一行 `<Compile Include="Game\AI\MaiBotBrain.cs" />`（老式 csproj 是显式文件清单，`Game\AI\Decks\*.cs` 那种通配不覆盖 `Game\AI\` 根下的新文件）；③ `MaiBotBrain` 的四处钩子（`Executor.AddExecutor` / `Executor.SetCard` / `GameAI` 的打谁与主要阶段提案）；④ `DecksManager` 的未注册告警与「执行器：」日志 |

patch 都是用 `git diff` 从上游 HEAD（`7acd93d`）导出的，已验证能干净地 `git apply` 到 HEAD；
`Game/AI/Decks/KashtiraExecutor.cs`、`TearlamentsExecutor.cs` 本身就是上游文件，本目录里的版本是
**在它们之上加写的**，所以直接覆盖同名文件即可（不需要 patch）。

MaiBotBrain 相关的钩子在没传 `BrainFile=` 时是零开销的空转：只调用 `MaiBotBrain.MaybeWrap` /
`AskIdleChoice` 的守卫，不改变脚本自己的决策。所以「只用十份脚本、不开 AI」也照这份步骤装。

### 装完怎么确认生效

1. 编译日志里能看到 `Decks initialized, N found`，N 应比上游原版多 10；
2. 对局日志里应有 `执行器：<卡表名>` 这一行（`host-integration.patch` 加的），
   若出现「`Deck 'X' 没在这份 exe 里注册，改用随机执行器 'Y'`」就是脚本没编进去；
3. 用 `Deck=<注册名>` 起一局，看有没有本家卡才有的动作（具体验收件见 `docs/acceptance.md`）。

## 3. 推荐做法：卡组脚本用 AI agent 迭代着写

十份脚本都是这么磨出来的，**不要指望一次写对**：

1. **写一版**——把「内核给的合法动作里先做哪个」写成有序规则（WindBot 的判定就是"外层按注册顺序
   遍历规则、第一条说 yes 的获胜"，等价于优先级列表）；合法性一律交给内核与卡脚本，不重复实现。
2. **开几局**（WindBot 侧 `Debug=true` 打决策日志；一局的动作数与终场直接看日志，
   也可以用 `tools/room_watch.py` 扫真实对局的日志）。
3. **看日志里哪一步没走对**——是没发动、发早了、选了错的对象，还是根本没登记这张卡
   （本项目最常查出来的就是"卡表里有、脚本里一条都没登记 → 每局纯白板"）。
4. **改一处**，再测。一轮只动一个变量，否则分不清是哪条起的作用。
5. **重复到验收件稳定**（首回合终场、动作数/局、胜率），再进下一副。

为什么这样比手写快：一副牌的引擎细节（时点、自肃、印刷号、选项值）散在卡脚本和内核里，
手写一轮要背一遍；而 agent 可以"读卡表 → 读卡脚本 → 改一行 → 跑一局 → 读日志"这样闭环几十轮，
每轮的证据都落在日志里。为什么比通用脚本强：通用脚本不知道这副牌的基本盘
（升辉月"本家没有检索"、耀圣"中央区只有一个"、闪刀"主区要空着"、刻魔"雷火的自肃顺序"），
这些恰好是胜负手。

**留几个开关做 A/B**（改行为前先用环境变量把它做成两臂，同一个 exe 不用重编译）：

| 开关 | 在哪 | 管什么 |
|---|---|---|
| `MULCHARMY_GATE` | `DefaultExecutor.cs` | 「欢聚友伴」的发动闸门：`chain`（默认）/ `wait_summon` |
| `RAISEMOON_AB` | `RaiseMoonExecutor.cs` | 升辉月的一处分臂 |
| `KT_ALLOW_PLAN_EXTRA` | `KillerTuneExecutor.cs` | 杀调额外怪"计划中不放行" |
| `KZ_GRYPHON_ALWAYS` / `KZ_GOBLIN_FREE` | `KezmoYixiangmingExecutor.cs` | 刻魔的狮鹫/哥布林闸门 |
| `WINDBOT_EMZ_FIRST=1` | `GameAI.cs` | 强制打开"额外怪兽区优先" |

其中三处已经量过、结论是「保持默认」，**别再重复做**：见 `docs/deck-audit.md` §3。

## 4. 卡表（`decks/`）

| 文件 | 主卡组 | 额外卡组 | 副卡组 |
|---|---|---|---|
| `decks/RaiseMoon.ydk` | 40 | 15 | 15 |
| `decks/WitchcraftShop.ydk` | 40 | 15 | 0 |
| `decks/ToonShop.ydk` | 40 | 15 | 0 |
| `decks/KillerTune.ydk` | 40 | 15 | 0 |
| `decks/SkyStrikerShop.ydk` | 40 | 15 | 0 |
| `decks/TraptrixRagnaraika.ydk` | 40 | 15 | 0 |
| `decks/Yaosheng.ydk` | 40 | 15 | 0 |
| `decks/KezmoYixiangming.ydk` | 40 | 15 | 0 |
| `decks/Kashtira.ydk` | 40 | 15 | 0 |
| `decks/Tearlaments.ydk` | 40 | 15 | 0 |

十副全部「40 主 + 15 额外」，只有升辉月另带 15 张副卡组（`!side` 段）。
`.ydk` 是明文文本：`#main` / `#extra` / `!side` 三段，每行一个卡号。卡号可能不是脚本里写的基准号
（同一张卡的不同印刷号＝alias），各执行器对这种卡会**两个号都登记**，改卡表时留意文件头注释里的提醒。

## 5. 和仓库里其它留档的关系

* 本目录（`executors/`）就是**十副牌 + 宿主侧依赖**的完整一份，给"照着装一遍"用：
  十个 `.cs` + `MaiBotBrain.cs` + 两个 patch 就是全部输入。
* `MaiBotBrain.cs` 是当年"逐步问 AI"的客户端钩子：**插件侧的 AI 打牌已按 2026-10-07 用户口径删除**，
  现在没有任何人会写它读的那两个问答文件，钩子在没传 `BrainFile=` 时是零开销空转——
  保留它只是为了让这十份脚本原样编译，不影响出牌。
* `PlanAwareExecutor.cs`（`Deck=PlanAware`）**不在这十份脚本的依赖里**：十份里只有代码注释提到过
  "PlanAware/通用脚本"，没有任何一处引用它的类型，所以没收进来。
* 插件运行时用到的卡表在宿主数据目录（`data/plugins/mai-play-ygo/decks/<群>/<uuid>.ydk`），
  `decks/` 里这十份是**同内容的可读命名副本**，方便对照与重建。
