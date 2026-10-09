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

**它是什么**：**对面发动效果带来的时点**上，把问题写进 `<BrainFile>.q` 让**模型**回答三件事，
插件的答复端是 `duel/brain_bridge.py`。三件事分别对应三种问题：

| 决策 | 问题种类 | C# 入口 | 开关 | 什么时候问 |
|---|---|---|---|---|
| **要不要发效果** | `negate_gate` | `MaiBotBrain.Guard`（挂在单张卡的规则上） | `BrainNegateGate` | 对面发动效果、或对面回合，且脚本手里只有**一张**阻抗可交 |
| **该发谁的效果** | `chain_choice` | `MaiBotBrain.AskChainChoice`（站在 `GameAI.OnSelectChain` 上） | `BrainNegateGate` | 对面是链上最后一个发动者，且我方**同时有 ≥2 张**阻抗可交 |
| **该去针对谁** | `disable_target` | `MaiBotBrain.PickDisableTarget`（`DefaultExecutor` 里调） | `BrainTargetChoice` | 交的是无效系（指向魔陷/怪兽），候选 ≥2 只 |

* `chain_choice` 的答复是**序号**（发哪张，行首那个数字就是内核候选下标+1，不保证连续）或
  `no;理由`（都不发）。**"都不发"只否掉那几张阻抗卡**（`MaiBotBrain._chainVetoedIds`，作用域绑定在
  当前这一次提问上），脚本自己想做的非阻抗动作照旧——不能整问拒答，那会连它的展开一起否掉。
* `disable_target` 现在**也管"刚发效果的那只"**：模型可以把目标改成场上另一只更大的威胁
  （以前那一支写死"刚发效果的那只"）。答不上来才退回脚本口径。
* **时点是并集**（只扩大不缩小）：`LastChainPlayer == 1`（对面是链上最后发动者，含我方回合对面丢手坑）
  **或** `Player == 1`（对面回合）。⚠ 这一条是踩出来的——我一度只留前者，结果真机一局的提问数
  从 37 掉到 1：那些时点里 `LastChainPlayer` 常常是 **-1**（对面召唤/独立窗口，没有连锁），
  而脚本照旧会主动交阻抗。Debug 打开时每个"没问"的单张闸门都会打一行原因，排查就靠它。

**为什么范围这么窄**：老口径（2026-10-07 之前）的钩子包裹**全部** `ExecutorType.Activate`，
等于在展开的每一步问模型一次，三次实测都没收益、机制上 **86% 是在否决脚本本来想做对的事**。
收窄之后"展开期不启用决策层"是**结构性保证**（不是阻抗卡、或时点不成立时，连问答都不会发起；
而且 `AskChainChoice` 只把**阻抗卡**列成候选，脚本自己的展开照旧），
所以那两个展开期老钩子由 `BrainIdleChoice` 默认关死——**只传 `BrainFile=` 就会把它们一起打开**，
这也是为什么那三个开关是 `to_args` 里显式传的。

**延迟为什么可控**：实测 8 局真实对局，"对手回合里我方发动的连锁"只有 **3~21 次/局（平均 10.8）**，
即"阻抗时点"的上界；配合 `BrainTimeoutMs`（默认 2500ms）与"对手回合 15 秒等待预算"
（`BrainOppBudgetMs`），最坏情况是白等几次。**答不上来就什么都不写**，WindBot 按脚本继续；
连续 3 次没答复就熔断本局的问答。

**模型实测（2026-10-08；2026-10-09 补 `deepseek-flash`）——这一条决定它能不能用**：
"够不够快"与"模型聪不聪明"无关，只取决于**有没有关掉思考**。同一个 key、同一个 base_url，
用插件自带提示词（405 prompt tokens / `max_tokens=256`）实测：

| 配置 | 延迟 | 思考 | 答复 |
|---|---|---|---|
| `deepseek-flash` + **关思考**（2026-10-09 新登记的 `name = "deepseek-flash"` 那条，插件现在用它） | **2.0~2.2 秒** | 0 token | 干净的序号（如 `2`） |
| └ 同一条在 2026-10-08 的探针里临时关思考量到 | 1.08 秒 | 0 token | 干净的序号（两种量法差在是否经宿主服务，取 2 秒那档更保守） |
| `deepseek-chat` + **关思考**（0.5 秒档，插件 2026-10-09 前用它） | **0.58~0.93 秒** | 0 token | 干净的序号（如 `2`） |
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
（本机叫 `deepseekV4.1flash` 那条反而显式写着 `thinking = {type = "enabled"}, reasoning_effort = "max"`，
那是给回复用的，别动它——决策层的模型名与它**同名不同条目**：插件填的是 `deepseek-flash`，
即 2026-10-09 新登记的、`model_identifier` 也是 `deepseek-flash` 但 thinking 关掉的那条）。
⚠ **等待上限要跟着模型走**：`deepseek-flash` 单次约 2 秒，所以 `llm.decision_timeout_ms` 配成 4000；
2500（Python 侧 2100）只适合 0.6~0.9 秒档的模型，配 2 秒的模型会卡在边缘、频繁超时。

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

**实测结论（2026-10-08，`--gate` 那一半＝"要不要发 + 该发谁"）**：

| 批次 | 牌组 | 局数 | 决策层赢 | 脚本赢 | 胜率 | 95% 区间 |
|---|---|---|---|---|---|---|
| 基线（两边都不开） | 合成手坑牌组 | 48 | 19 | 29 | 39.6% | 27.0~53.7% |
| 正式腿 | **合成手坑牌组**（15 遮蒙者 + 5 泡影 + 20 青眼，`Deck=Test`） | 300 | 187 | 113 | **62.3%** | **56.7~67.6%** |
| 正式腿 | **真实卡组：全盛俱舍**（`Deck=Kashtira`） | 160 | 71 | 89 | **44.4%** | 36.9~52.1% |
| 正式腿 | 同上，**并给模型补上对手场上卡文 + 禁止它凭记忆编卡** | 160 | 65 | 95 | **40.6%** | **33.3~48.4%** |

两条腿**方向相反**——所以不能只看合成牌组那 62.3% 就下结论：

* **合成手坑牌组**上决策层是一大笔收益，机制也看得见：它把交牌次数**减半**（1.09 对 2.32/局），
  一局问 17 次（300 局共 5152 次）。那副牌是"20 张手坑 + 20 张白板大怪 + 通用脚本"，
  **"别急着交、留着"在那副牌上几乎就是最优策略**——引擎没有任何需要保护的展开，早交没有收益。
* **全盛俱舍**上点估计是**负的**（44.4%），而伤害集中在它真动手的地方：
  把这一批按"决策层有没有真问过"切开——**真动作过的 97 局里只赢 38（39.2%）**，
  而**一次没问的 63 局是 52.4%**（后者是同批内部对照 ≈ 50%，说明这套 A/B 本身没偏）。
  俱舍一局只问 3.1 次（61% 的局问过至少一次），所以它在很多局里几乎没参与；参与了的局反而掉分
  ——**"保守留着"在"该花就得花"的牌组上是亏的**。

**⚠ 用户口径（2026-10-08，定这条线）**：**镜像自测看不出体感差异**——同一副牌三腿 480 局
（平常 40.6% / 激进 45.0% / 激进复测 42%+）都在 40~45%、区间跨或贴着 50%，分不出高下；
而体感（"对面发效果时它到底有没有按我的想法去交"）只有**真人对战**才测得出来。
所以：**决策层的档位与开关由实战体感定，不再用镜像 A/B 下结论**（A/B 只用于排除"完全没在工作"这类硬故障）。
按这个口径，`brain_negate_gate` 与 `decision_style` 现在**默认开 + 激进**。

**决策档位（2026-10-08 新增）**：提示词里那段"判断口径"做成了三档，**只改提示词、不用重编译 exe**——
`conservative`（保守：有疑问就不交、留着以后）/ `normal`（平常）/ `aggressive`（激进：能拦就拦、宁可早一点），
配置项 `llm.decision_style`（中英文都收）。做它的理由：同一份口径在不同牌组上一正一负，
"该不该保守"本身就该按牌组调，而不是把某个口径写死在提示词里。

⚠ **但别指望调档位能把负的变正的**（2026-10-08 在俱舍上量过，每腿 160 局）：
平常口径 40.6%（区间 33.3~48.4）／激进口径 45.0%（区间 37.5~52.7）——两腿的区间大幅重叠、
差异在噪声之内，都跨在 50% 附近。而且那次激进腿**本身还不干净**：模型换了措辞后改成写
"序号 + 换行 + 理由："，而解析器当时只认分号，**88/566 = 15.6% 的答复被整条丢弃**
（现象是 `失败：63`，对照平常腿是 0）。已修（`_split_verdict` 按前导 token 解析，
中文"都不发"也认）并补了回归单测；教训是**答复格式会随提示词措辞变，解析器不能假设分隔符**。

**"是模型不懂游戏王吧？"——是，但那不是主因。** 这一条单独查过：

* **知识缺口真实存在**：直接问它「珠泪哀歌族型俱舍怒威族」，它答"融合怪兽、不能通常召唤，
  只能融合召唤"——而卡库原文是"主卡组怪兽、主要阶段从手卡自跳 + 除外 + 从卡组顶堆 3 张"；
  问「俱舍怒威族的准备」也答错；连「灰流丽」都多编了一个"破坏"（它只无效、不破坏）。
  根因是提示词原来只给"链上那张 + 我方候选"补卡文，**对手场上的卡只有卡名**，
  模型只能靠记忆补，补出来的就是编的。
* **补上卡文没救回来**：给对手场上的卡也补文本、并明确写"只按我给的卡文判断、不要凭记忆编卡的效果"
  之后，同样 160 局打出来是 **40.6%**（区间 33.3~48.4，**这次排除了 50%**）——
  和补之前的 44.4% 在噪声范围内、方向没变（而且卡文补丁本身是对的，得留着：
  让判断建立在真卡文上，而不是假记忆上）。
* **所以问题在策略而不在知识**：决策层的倾向是"先别交、留着"，机制上它把交牌次数压低了
  （26.99 对 34.95/局）。这在"20 手坑 + 20 白板大怪"那种**没有展开需要保护**的牌组上是对的，
  在"该断就得断"的俱舍上就是亏的。把"要不要交"的默认口径从"模型说交才交"翻成
  "脚本说交就交、模型只在有具体理由时拦"是另一个设计，得单独量。

**因此当前默认关掉**（`brain_negate_gate = false`）：合成牌组 +13 个点、真实牌组两腿 -5.6 / -9.4 个点
（其中一腿的区间已排除 50%），这种证据不支持默认开。两条可走的路：**按牌组分别量**
（池子里十副各跑 80 局/腿，约两三小时）只在对它有利的牌组上打开；
或者承认这一层在真实卡组上不划算，**按 2026-10-07 的口径整条撤掉**。

**怎么判它有没有用（镜像 A/B）**：`python plugins/mai-play-ygo/tools/brain_ab.py --duels 300 --gate`
——两边**完全同一副牌、同一个脚本、同一份 exe**，唯一变量是"某一边有没有开决策层"，座位逐局交替。
真实卡组加 `--deck-style Kashtira --deck-file <池子里那副的 .ydk>`（合成牌组不用指定，默认就是它）。
四条纪律都写在工具里：①**`--gate` 必须开**，否则量的是空转（只开目标选择那一半时真机一次都不触发）；
②每局都记"差分有没有真的跑出来"（`asks>0`）与**座位映射核对**（两边起不同昵称，
从结果里反查"决策层那一侧的名字对不对"——这个错一旦发生胜率会整体翻转）；
③结尾打 **Wilson 区间**而不是只打百分比（极端比例下正态近似会给出"0.0%~0.0%"这种假确定）；
④**别两条腿并行跑**（实测把墙钟拖到 ~90 秒/局，串行只要 ~15 秒/局）。

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
