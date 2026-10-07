# WindBot 侧的改动（编译进 exe）

这个目录留档的是**插件要求 WindBot 源码树里有**的改动。出牌脚本和问答通道都是 C#、编译进
`WindBot.exe` 的：WindBot 用反射扫 `[Deck(...)]` 注册执行器，**找不到 `Deck=` 请求的名字时不报错，
会随机挑一个 Normal 档执行器顶上**——所以"改了源码但用旧 exe"会静默测错东西。

## 要改九件事

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `Game/AI/Decks/PlanAwareExecutor.cs` | 新增（本目录同名文件照抄）——计划感知执行器，`Deck=PlanAware` |
| 2 | `Game/AI/MaiBotBrain.cs` | 新增（本目录同名文件照抄）——逐步问 AI 的通道 |
| 3 | `Game/AI/Executor.cs` | 加**两处钩子**（见下），让任何出牌脚本的**发动**决策都能问 AI |
| 4 | `Game/GameAI.cs` | 加**两处钩子**（见下）：**打谁** + **主要阶段这一步做什么**（AI 出牌的入口）；另加**额外怪兽区优先**（见下） |
| 5 | `WindBot.csproj` | 加一行 `<Compile Include="Game\AI\MaiBotBrain.cs" />` |
| 6 | `Game/AI/Decks/RaiseMoonExecutor.cs` | 新增（本目录同名文件照抄）——「升辉月 / 盈彩月夜」的**本家执行器**，`Deck=RaiseMoon` |
| 7 | `Game/AI/Decks/KillerTuneExecutor.cs` | 新增（本目录同名文件照抄）——「杀调 / 杀手级调整曲」的本家执行器，`Deck=KillerTune` |
| 8 | `Game/AI/Decks/SkyStrikerShopExecutor.cs` | 新增（本目录同名文件照抄）——「闪刀姬」的本家执行器，`Deck=SkyStrikerShop`（类名带 Shop 后缀是为了和上游自带的 `SkyStrikerExecutor` 区分；这份会打开 `PreferExtraMonsterZone`，因为"主怪兽区留空"是整副闪刀的前提） |
| 9 | `Game/AI/DecksManager.cs` | 改 `Instantiate`：卡组名没注册时**吼一声**，并打印这一局实际用的执行器（原来会静默换成随机脚本——对局照打，但打出来的数字代表的是别的脚本，这种坑已经让一整批评估数据作废过一次） |

> 注：`WitchcraftShopExecutor.cs`（魔女术）与 `ToonShopExecutor.cs`（卡通）也在
> `<paths.windbot_src_dir>/Game/AI/Decks/` 里，但**本目录还没放镜像**。要复现编译输入，
> 按本文件末尾那条 `diff` 的做法把它们照抄过来即可。

### 第 6 项：升辉月的本家执行器（`Deck=RaiseMoon`）

那副牌没有 WindBot 自带的对应执行器，一直靠通用脚本打（`Lucky` 与它的卡表重合度只有 **0.06**），
通用脚本不知道"抽到下级就自跳、本家没有检索、全靠抽卡滚起来"这条基本盘。
这份执行器基类用 `DoEverythingExecutor`（通用打法），只在**前面**压上四条本家判断
（两张引擎魔法能发就发、蓋輝煌、通召优先卡蘿爾→菈碧、超量先做一擲乾坤再千金大獎）——
WindBot 的判定是"顺序遍历规则、第一条说 yes 的获胜"，插在前面才压得过通用规则。

**注册方式**：`WindBot.csproj` 里 `Game\AI\Decks\*.cs` 是通配包含 ✔，所以**不用改 csproj**，
把文件丢进 `Game/AI/Decks/` 重新编译就会被 `[Deck("RaiseMoon", ...)]` 反射注册。
验证：编译后跑 `tools/evaluate_style.py` 的注册计数（应从 80 变 81），或直接看 exe 里有没有
`RaiseMoonExecutor` 这个类名。

### `Game/GameAI.cs` 的第三处改动：召唤/放置时**额外怪兽区优先**（现在是**卡组级开关**，默认关）

原版 `OnSelectPlace` 挑格的优先级是 `z2 → z1 → z3 → z0 → z4 → z6 → z5`——**主怪兽区优先、
额外怪兽区排在最后两位**。于是只要引擎同时给了主区（对手的连接箭头指过来、或效果放置那种
通用选格），额外卡组出来的大怪就必然落在主怪兽区。实测（同一台机器、同一份卡表）：
「升辉月」的大姐（超量怪 `100267022` / `100267023`）落在主怪兽区 3、一局都没进过额外怪兽区
（用户报过两次），而它的效果写着"只要此卡在额外怪兽区域存在"。

改法是：**候选里含额外怪兽区时先选 z6 → z5**；不含时保持原来的顺序。
`OnSelectDisfield`（效果选格那条路）**不动**——那里主区优先是对的。

⚠ **这一条最初写成了全局默认，是个错**：`OnSelectPlace` 是引擎级共享代码，
连接卡组（群里对手用的码丽丝那种）的箭头要靠站位连，被塞进额外区就连接不上、整副牌变弱。
实测同一份卡表镜像 12 局：

| 额外区优先 | 我方胜 | 我方动作 | 我方特召 | 对手特召 |
|---|---|---|---|---|
| 开（旧的全局行为） | 2/12 | 25.9 | 9.1 | 18.0 |
| 关（现在的默认） | **5/12** | **36.2** | **13.4** | 14.8 |

现在是**卡组级开关**：默认取原版顺序，只有需要它的卡组自己在执行器里打开
（`RaiseMoonExecutor` 构造函数第一行 `GameAI.PreferExtraMonsterZone = true;`）；
环境变量 `WINDBOT_EMZ_FIRST=1` 可强制打开，用来复现/量化上面那张表。

```csharp
public static bool PreferExtraMonsterZone =
    System.Environment.GetEnvironmentVariable("WINDBOT_EMZ_FIRST") == "1";

bool extraZoneOffered = PreferExtraMonsterZone && (filter & Zones.ExtraMonsterZones) != 0;
if (extraZoneOffered)
{
    if ((filter & Zones.z6) != 0) sequence = 6;
    else if ((filter & Zones.z5) != 0) sequence = 5;
}
else if ((filter & Zones.z2) != 0) sequence = 2;
// …原来的 z1 → z3 → z0 → z4 → z6 → z5 顺序不变
```

**官方发行版没有这条改动**：要用就得按本目录的步骤自己编译 `windbot-src`。
验证办法：群里 `/查房` 会报"站位"（`额外怪兽区 #卡号`），日志里也会打
`落位：第 N 回合 我方 额外怪兽区 ← 卡名`。

### `Game/GameAI.cs` 的两处钩子

**① 打谁**（`OnSelectBattle`）：

```csharp
result = Executor.OnSelectAttackTarget(attacker, defenders);
if (result != null)
{
    // 打谁问一次 AI（脚本自己决定要打之后才问，所以它只能改选或取消）
    ClientCard target;
    bool skip;
    if (MaiBotBrain.TryOverrideAttackTarget(attacker, defenders, out target, out skip))
        return skip ? null : Attack(attacker, target);      // ← 新增
    return result;
}
```

**② 主要阶段「这一步做什么」**（`OnSelectIdleCmd` 的开头）：

```csharp
// 先问 AI「这一步做什么」：这是"AI 打牌"的入口——脚本写不出的新卡组也能靠它动起来。
if (MaiBotBrain.Enabled)
{
    List<IdleOption> aiOptions = CollectIdleOptions(main);   // 召唤/特召/发动/盖放/进战斗/结束
    if (aiOptions.Count >= 2)
    {
        List<string> labels = ...;
        int aiPick = MaiBotBrain.AskIdleChoice(labels, hasCardOption);
        if (aiPick > 0 && aiPick <= aiOptions.Count)
            return BuildIdleAction(aiOptions[aiPick - 1]);    // ← 新增：AI 选哪个就做哪个
    }
}
```

**为什么这两处是分不开的**：脚本层（`Executor.AddExecutor`）只能**否决**脚本自己想做的事，
所以碰上它不认识的卡组（新系列、投稿卡组）它一步都走不出来——用户实测反馈"脚本根本无法正常展开"
就是这个。②给的是**提议权**：AI 选哪个动作就执行哪个，卡组没有专属脚本也能动起来。

### 五个钩子的分工

| 位置 | 管什么 | AI 的权力 |
|---|---|---|
| `Executor.AddExecutor` | 要不要发动/连锁某张卡 | **只能否决**（脚本想做的事） |
| `GameAI.OnSelectBattle` | 让谁打、打谁 | 改选 / 取消 |
| `GameAI.OnSelectIdleCmd` | **主要阶段这一步做什么** | **提议**（召唤/特召/发动/盖放/进战斗/结束） |
| `Executor.SetCard` | 记录当前判定的卡 | 只读上下文 |

```csharp
public void SetCard(ExecutorType type, ClientCard card, int description, int timing = -1)
{
    Type = type;
    Card = card;
    ActivateDescription = description;
    CurrentTiming = timing;
    // 让"逐步问 AI"的守卫拿到当前正在判定的卡片与执行器（没开 AI 时这行没副作用）
    MaiBotBrain.NoteCurrent(this, card);          // ← 新增
}
```

```csharp
public void AddExecutor(ExecutorType type, int cardId, Func<bool> func)   // 三个重载都是这个形状
{
    Executors.Add(MaiBotBrain.MaybeWrap(new CardExecutor(type, cardId, func)));   // ← 外面套一层
}
```

### 一处细节：`Executor.cs` 的发动钩子

```csharp
public void AddExecutor(ExecutorType type, int cardId, Func<bool> func)   // 三个重载都是这个形状
{
    Executors.Add(MaiBotBrain.MaybeWrap(new CardExecutor(type, cardId, func)));   // ← 外面套一层
}
```

（同一个文件里的 `SetCard` 还要加一行 `MaiBotBrain.NoteCurrent(this, card);`，见 3.5 那一节。）

钩子都挂在**基类**上而不是只做在 `PlanAware` 里：**开 AI 不该逼你放弃卡组的专属脚本**。
实测同一副牌同一对手，专属脚本 13% 胜/16.0 动作，通用脚本 0%/8.7——之前"开 AI 就得换成通用
执行器"的代价比 AI 带来的收益还大。钩子提上来之后，78 个自带执行器 + 投稿卡组生成的执行器
都能问 AI。没传 `BrainFile=` 时 `MaybeWrap` 原样返回、`Ask` 直接返回空串，等于零改动、零开销。

## 编译

```bash
msbuild WindBot.csproj -p:Configuration=Release -p:OutputPath=bin/Release/
```

⚠ **编译完必须让"对局实际用的那份 exe"也变成这次的产物**。这两处往往是**不同的目录**：

- 编译产物：`<paths.windbot_src_dir>/bin/Release/WindBot.exe`
- 对局在用的：`paths.windbot_executable`（本项目里是 `D:\Game\ygopro-duel\windbot\WindBot\WindBot.exe`）

不一致时**源码里的改动在真实房间里一行都不会生效**（本项目踩过：连着手坑禁令、选卡规则、
额外区挑格好几版都没进房间局，因为房间一直在跑另一份 9 月中的原版 exe）。
拷过去或者改配置都行，核对用一条命令：

```bash
uv run python tools/check_windbot_build.py            # 不一致会报错并给出命令
uv run python tools/check_windbot_build.py --deploy   # 备份后直接覆盖
```

要单独输出、不覆盖正在被对局占用的 exe 时，换个 `OutputPath` 并把 `windbot_executable` 配到那个目录。

## 命令行参数

| 参数 | 作用 |
|---|---|
| `Deck=<风格名>` | 出牌脚本；`PlanAware` = 计划感知执行器，卡组专属脚本用卡表分析出的名字 |
| `PlanFile=<绝对路径>` | 作战计划（`key=value`，见 `train/plan.py`） |
| `PlaybookFile=<绝对路径>` | 卡组打法数据（`key=value`，见 `duel/playbook.py`） |
| `BrainFile=<前缀>` | 开逐步问 AI：执行器写 `<前缀>.q`，Python 侧写 `<前缀>.a`。**不给就不问** |
| `BrainTimeoutMs=<毫秒>` | 等单次答复的上限，默认 20000；超时按脚本自己的判断继续 |
| `BrainBudgetMs=<毫秒>` | **每回合**为"问 AI"最多花多少等待时间，默认 8000。用完之后这一回合照脚本打 |
| `Debug=true` | 打决策日志（Release 下 `Logger.DebugWriteLine` 被编译掉了，只能用这个） |

问题文件里目前会写这些字段（Python 侧按 `key=value` 解析，**多写的字段会被忽略、缺的用默认值**，
所以两边版本可以不严格对齐）：

| 字段 | 含义 |
|---|---|
| `id` / `kind` / `turn` / `my_lp` / `opp_lp` | 这次问的编号、类型（`activate` / `attack_target`）、回合、双方 LP |
| `card` / `card_name` | 要判定的那张卡 |
| `hand` / `mine` / `my_spell` / `theirs` / `their_spell` | 区域，每张卡一行：`卡号;卡名;攻击力;守备力;表示形式` |
| `grave_mine` / `grave_theirs` | 墓地张数 |
| `banish_mine` / `banish_theirs` | **除外区张数**（P1.5 起） |
| `chain_depth` | **连锁深度**：0＝自由时点，≥1＝正在应对别人的效果（P1.5 起） |
| `their_seen` | **对手已经露出来的卡**（场上/墓地/除外，最多 12 种）（P1.5 起） |
| `chain` | **连锁上正在处理的卡**：`卡号;卡名;控制者`（0.12.0 起）——判断"该不该交坑"就看这里 |
| `my_phase` / `phase` | 是不是我的回合、当前阶段 |
| `caution` | 这张牌在打法数据里被标成"谨慎"（`never_activate`；只提醒、不禁止） |
| `choice_ids` / `choice_names` | 候选（打谁时的可攻击对象） |

`BrainBudgetMs` 为什么必须有：一个回合的问与答都落在内核的**单回合时限**里，问得太多会把时限
吃光——实测整局一步没走、结局是"超时"（30 秒时限、问了 30 次的组合回合）。预算把 AI 的代价
钉死在"每回合最多 8 秒"。

## 留档与同步

本目录两份 `.cs` 与 WindBot 源码树里的同名文件应当**只差"留档说明"那两行注释**，对拍：

```bash
diff <(sed 's/\r$//' train/windbot/PlanAwareExecutor.cs) \
     <(sed 's/\r$//' /path/to/windbot-src/Game/AI/Decks/PlanAwareExecutor.cs)
```

改动任何一份都请同步另一份。


---

## 留档怎么维护（2026-10-05 起：工具化，别再手抄）

**背景**：本目录原来靠"手工照抄"维护，两天内就过期了（缺魔女术/卡通，另外三份停在 10-03，
而看目录的人不会知道）——所以改成**清单 + 校验工具**：

```bash
python tools/check_windbot_sources.py           # 校验：留档副本 = 源码树 = 哈希清单
python tools/check_windbot_sources.py --sync    # 用源码树刷新本目录的副本 + 更新 sources.json
```

* `sources.json` 记"相对路径 → sha256"；留档的 7 个文件：
  `RaiseMoonExecutor.cs`、`KillerTuneExecutor.cs`、`SkyStrikerShopExecutor.cs`、**`WitchcraftShopExecutor.cs`（新增）**、
  **`ToonShopExecutor.cs`（新增）**、`Decks/PlanAwareExecutor.cs`、`MaiBotBrain.cs`。
* **改执行器只改源码树**（`D:\Game\ygopro-duel\windbot-src`），改完编译 → `python tools/check_windbot_build.py --deploy`
  → `python tools/check_windbot_sources.py --sync` 三连，缺一步就会出现"留档/对局用的/源码"三者不一致。
* 上面第 3~5 条（`Executor.cs`、`GameAI.cs`、`WindBot.csproj`）是**对上游文件的修改**，不适合整份照抄，
  仍按"要改什么"的清单维护；`DecksManager.cs` 同理。
