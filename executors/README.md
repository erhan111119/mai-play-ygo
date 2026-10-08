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

# 1) 先打宿主侧的四个 patch（对上游 HEAD 的 diff，git apply 用默认 -p1，从源码树根跑）
cd <windbot-src>
git apply <plugin>/executors/DefaultExecutor.patch
git apply <plugin>/executors/DefaultExecutor-targeting.patch
git apply <plugin>/executors/DoEveryThingExecutor.patch
git apply <plugin>/executors/host-integration.patch

# 2) 十份执行器 → Game/AI/Decks/
cp <plugin>/executors/*Executor.cs Game/AI/Decks/

# 3) AI 问答通道 + 通用选目标判据 + 阻抗决策层的确定性部分 → Game/AI/
#    KillerTuneExecutor.cs 直接调用 MaiBotBrain；EnemyTargeting 是"该炸/该弹对面哪张"的共享判据
#    （DoEverything / RaiseMoon 都调它）；NegateDecision 是阻抗决策层的卡表与目标过滤
#    （MaiBotBrain / DefaultExecutor 都调它）——四个都缺一编译不过
cp <plugin>/executors/MaiBotBrain.cs <plugin>/executors/EnemyTargeting.cs <plugin>/executors/NegateDecision.cs Game/AI/

# 3.5) 老式 csproj 是显式文件清单：新加的 Game/AI/ 根下文件各要一行
#      <Compile Include="Game\AI\EnemyTargeting.cs" />（MaiBotBrain 那行在 host-integration.patch 里）

# 4) 编译
dotnet build WindBot.csproj -c Release
```

编译完**还要让对局实际用的那份 exe 变成这次产物**（`<windbot-src>/bin/Release/WindBot.exe`
与 `paths.windbot_executable` 常常不是同一个目录，不一致时源码改动在真实房间里一行都不生效）：

```bash
uv run python tools/check_windbot_build.py            # 不一致会报错并给出命令
uv run python tools/check_windbot_build.py --deploy   # 备份后直接覆盖
```

### 四个 patch 分别是什么

| 文件 | 内容 | 为什么装脚本必须带上 |
|---|---|---|
| `DefaultExecutor.patch` | 上游 `Game/AI/DefaultExecutor.cs` 的 diff（+66/-4） | 七份执行器（RaiseMoon / WitchcraftShop / ToonShop / KillerTune / SkyStrikerShop / TraptrixRagnaraika / KezmoYixiangming）用了它新增的成员：`EnemySummonedThisTurn`、`MulcharmyReady()`、`MulcharmyWaitSummon`。缺了**编译不过** |
| `DefaultExecutor-targeting.patch` | 同一个文件的第二段 diff：`DefaultGetDisableMonsterTarget()` 的**判定顺序**（先看"刚发效果的那只"、再看"最该被废的那只"）+ `PickEnemyRemovalTarget` 的薄封装 + **阻抗决策层的接入点**（`PickDisableTargetByAI`） | 通用层的"该无效哪只怪"（群友实测：对面「白龙之落胤」发效果、我们的「效果遮蒙者」却去点了「魔女术师傅·玻璃女巫」）。缺了只是**行为退回旧口径**，不影响编译 |
| `DoEveryThingExecutor.patch` | 上游 `Game/AI/Decks/DoEveryThingExecutor.cs` 的 diff | 通用脚本（`Test`）的选卡兜底：上游是"取候选尾部"，投稿卡组全吃这条 → 改成"破坏/除外/弹回/洗回先按卡文挑对面那张"。缺了行为退回旧口径，不影响编译 |
| `host-integration.patch` | `Game/GameAI.cs`、`Game/AI/Executor.cs`、`Game/AI/DecksManager.cs`、`WindBot.csproj` 四个上游文件的 diff | ① `GameAI.PreferExtraMonsterZone` 字段——RaiseMoon / SkyStrikerShop / TraptrixRagnaraika 三份在构造函数里打开它（把链接怪/超量怪优先放额外怪兽区），缺了**编译不过**；② `WindBot.csproj` 加一行 `<Compile Include="Game\AI\MaiBotBrain.cs" />`（老式 csproj 是显式文件清单，`Game\AI\Decks\*.cs` 那种通配不覆盖 `Game\AI\` 根下的新文件）；③ `MaiBotBrain` 的四处钩子（`Executor.AddExecutor` / `Executor.SetCard` / `GameAI` 的打谁与主要阶段提案）；④ `DecksManager` 的未注册告警与「执行器：」日志 |

`Game/AI/EnemyTargeting.cs`（本目录里同名文件，不在 patch 之列）是**通用选目标判据**的落点：
"弄掉对面哪张卡"的打分与「弄掉它反而帮对面」的卡表（每条都带卡文出处）都写在那里；
`DoEveryThingExecutor.patch` 与升辉月执行器都调它，所以两份 patch 和这个文件要一起装。

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
| `AI_BATTLE_PRESSURE` | `EnemyTargeting.cs` | 对面**空场**时要不要压血：默认就能打就进战阶；`=0` 回到旧口径（只有一击斩杀才进，用于 A/B） |
| `RAISEMOON_AB` | `RaiseMoonExecutor.cs` | 升辉月的一处分臂 |
| `KT_ALLOW_PLAN_EXTRA` | `KillerTuneExecutor.cs` | 杀调额外怪"计划中不放行" |
| `KZ_GRYPHON_ALWAYS` / `KZ_GOBLIN_FREE` | `KezmoYixiangmingExecutor.cs` | 刻魔的狮鹫/哥布林闸门 |
| `WINDBOT_EMZ_FIRST=1` | `GameAI.cs` | 强制打开"额外怪兽区优先" |
| `BrainTargetChoice` | `MaiBotBrain.cs` | **阻抗决策层**：是否让模型决定"无效哪只怪"（默认开） |
| `BrainNegateGate` | `MaiBotBrain.cs` | **阻抗决策层**：是否让模型决定"要不要交这张阻抗"（默认关，有 86% 否决前科） |
| `BrainIdleChoice` | `MaiBotBrain.cs` | **旧的两个展开期钩子**（闲时选动作 / 改选攻击目标）的总开关，**默认关**；开了等于回到"展开每一步都问模型"的老口径 |

其中三处已经量过、结论是「保持默认」，**别再重复做**：见 `docs/deck-audit.md` §3。

## 3.5 阻抗决策层（2026-10-08）

**它是什么**：对手回合里、且当前判定的是"阻抗卡"（`NegateDecision.NegateCardIds` 里的卡号）时，
把问题写进 `<BrainFile>.q` 让**模型**回答两件事之一，插件的答复端是 `duel/brain_bridge.py`：

* `BrainTargetChoice`（默认开）：**这一张无效卡该指向对面哪只怪**。只在
  `DefaultGetDisableMonsterTarget` 的启发式兜底那一支生效——"刚发效果的那只"是链上确定的
  （2026-10-08 修过的那个 bug），不在模型的可选范围里；战斗相关那两只也不是选择题。
* `BrainNegateGate`（默认关）：**要不要交这张阻抗**。这一半有前科（见下）。

**为什么范围这么窄**：老口径（2026-10-07 之前）的钩子包裹**全部** `ExecutorType.Activate`，
等于在展开的每一步问模型一次，三次实测都没收益、机制上 **86% 是在否决脚本本来想做对的事**。
收窄之后"展开期不启用决策层"是**结构性保证**（自己回合、或不是阻抗卡，连问答都不会发起），
所以那两个展开期老钩子由 `BrainIdleChoice` 默认关死——**只传 `BrainFile=` 就会把它们一起打开**，
这也是为什么那三个开关是 `to_args` 里显式传的。

**延迟为什么可控**：实测 8 局真实对局，"对手回合里我方发动的连锁"只有 **3~21 次/局（平均 10.8）**，
即"阻抗时点"的上界；配合 `BrainTimeoutMs`（默认 2500ms）与"对手回合 15 秒等待预算"
（`BrainOppBudgetMs`），最坏情况是白等几次。**答不上来就什么都不写**，WindBot 按脚本继续；
连续 3 次没答复就熔断本局的问答。

**模型实测（2026-10-08）——这一条决定它能不能用，也解释了为什么默认值是 `deepseek-chat`**：
"够不够快"与"模型聪不聪明"无关，只取决于**有没有关掉思考**。同一个 key、同一个 base_url，
用插件自带提示词（405 prompt tokens / `max_tokens=256`）实测：

| 配置 | 延迟 | 思考 | 答复 |
|---|---|---|---|
| `deepseek-chat` + **关思考**（默认值就是它） | **0.58~0.93 秒** | 0 token | 干净的序号（如 `2`） |
| `deepseek-flash` + 关思考 | 1.08 秒 | 0 token | 干净的序号 |
| `deepseek-v4-pro` + 关思考 | 1.08 秒 | 0 token | 干净的序号 |
| `deepseek-flash`（本机原来那条 `deepseekV4.1flash` 的现状） | 2.21 秒 | **256 token（撞满额度）** | **空串** |
| `ds`（插件默认的 `utils` 任务） | 17.1 秒 | 7039 字 | **空串** |
| `火山mini3` / `火山glm2` / `glm-5.3` | 十几秒 | 4 千字以上 | **空串** |
| `glm-4.7-flash` / `硅基qwen` | — | — | provider 侧 30 秒超时 |
| `dsv4` / `glm4.7` / `small` / `glm星火` / `qwen3.6` / `百度glm` | — | — | 欠费 / 认证失败 / 未实名 |
| `DMXglm1..4` | 1.0~1.4 秒 | — | "服务器负载过高" |

**判读要点（比"换哪只模型"更重要）**：本机大部分模型是**思考型**，思考会把 `max_tokens` 吃光，
所以 `response` 是空串——**"把 max_tokens 调大"解决不了**（只是让它思考更久，实测 17 秒/7039 字）。
真正的开关是模型条目里的 `extra_params = {thinking = {type = "disabled"}}`
（本机 `deepseek-flash` 那条反而显式写着 `thinking = {type = "enabled"}, reasoning_effort = "max"`，
那是给回复用的，别动它——决策层用新登记的 `deepseek-chat` 那一条）。

所以两个开关的默认值是：**`BrainTargetChoice` 开**（目标选择，低风险、有信息优势）、
**`BrainNegateGate` 关**（有 86% 否决前科，先跑镜像 A/B）。换 provider / 换模型之后用
`tools/brain_model_probe.py` 再量一遍——上面那张表只是一次快照。

**怎么本地验证链路**：`python plugins/mai-play-ygo/tools/brain_channel_check.py`
（起真房间 + 第二个 WindBot 当对手，被测 bot 用 `Deck=Test` 通用脚本 + 一手指抗卡组，
答复端换成**秒答的假模型**）——这样测的是"C# → `.q` → bridge → `.a` → C#"这条契约本身，
**不受本机模型好不好用影响**。2026-10-08 用这办法通过（3 问 3 答 / 零失败），
并因此抓到一个真 bug：闸门问题走 C# 原有的 `AppendContext`（区域行**五段**），
bridge 原来按四段解包 → 组装提示词阶段抛 ValueError → 现象是"问题文件写出来了、模型一次都没被问到"。
`tests/test_brain_bridge.py` 里已补上这个形状的回归用例。

**换成"配模型"这件事**：`python plugins/mai-play-ygo/tools/brain_model_probe.py`
按同一份提示词逐个量候选模型的单次延迟，报出"是否拿到正文"与"思考字段多长"
（区分思考型的判据：`ok=false` 且思考几万字、`response` 为空）。

**怎么判它有没有用（镜像 A/B）**：`python plugins/mai-play-ygo/tools/brain_ab.py --duels 160 --gate`
——两边**完全同一副牌、同一个脚本、同一份 exe**，唯一变量是"某一边有没有开决策层"，座位逐局交替。
三条纪律都写进工具里了：①**`--gate` 必须开**，否则量的是空转（只开目标选择那一半时真机一次都不触发）；
②每局都记"差分有没有真的跑出来"（`asks>0`）与**座位映射核对**（给两边起不同昵称，
从结果里反查"决策层那一侧的名字对不对"——这个错一旦发生胜率会整体翻转）；
③结尾打 **Wilson 区间**而不是只打百分比（极端比例下正态近似会给出"0.0%~0.0%"这种假确定）。

**实测结论（2026-10-08，`--gate` 那一半）**：

| 批次 | 局数 | 决策层赢 | 脚本赢 | 胜率 | 95% 区间 |
|---|---|---|---|---|---|
| 基线（两边都不开） | 48 | 19 | 29 | 39.6% | 27.0%~53.7% |
| 正式腿（第一次） | 150 | 94 | 56 | 62.7% | 54.7%~70.0% |
| 正式腿（复现，带映射核对） | 20 | 11 | 9 | 55.0% | 34.2%~74.2% |
| **正式腿合计** | **170** | **105** | **65** | **61.8%** | **54.3%~68.7%** |

机制读数同向（这是比胜率可靠的证据，每次否决都会留下痕迹）：**决策层那一侧的"发动效果"次数
只有脚本侧的一半**（1.15 / 1.05 对 2.35 / 2.15）——它确实在少交牌，而不是"开了但没动作"。

⚠ **这套结论只覆盖"通用脚本（`Deck=Test`）+ 手坑重的合成牌组"**：真实卡组一副要 4~10 分钟，
150 局要跑十几个小时，攒不到样本；而那副合成牌组恰好是"手坑泛滥"的极端情形，
"少交牌"在那里天然占便宜——**换到十副真实卡组上未必成立**，要量得另开腿。
另外基线 39.6% 的区间跨过 50%（说明这套镜像对局没有偏向"被标为决策层的那一侧"，
若说有偏也是偏弱），正式腿合计的区间则**没有**跨过 50%。

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
