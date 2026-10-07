using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「卡通」——按操作方给的 202605 教程写的决策层（配合群友投稿的 #94 卡表）。
    ///
    /// **这一层只排战术优先级**，合法性由内核判、条件与自肃由卡脚本声明（和另外两份执行器同一套分工）：
    ///
    /// | 层 | 负责 |
    /// |---|---|
    /// | 内核（ocgcore） | 起手不触发 `EVENT_DRAW`、自肃、把"解放/除外的卡"这类代价判清楚 |
    /// | 卡脚本（Lua） | 「卡通世界」② 一回合最多 3 次检索、「漫画猫」② 能在对方场上取解放目标…… |
    /// | 本文件 | 只看内核给出的合法动作，决定先做哪个 |
    ///
    /// 引擎（按卡文核对过）：
    /// * **「完美世界 卡通世界」(7293697)**：① 当作「卡通世界」；② 每回合最多 3 次检索「卡通」卡；
    ///   ③ 可以把场上 1 只卡通怪兽"直到那个效果处理完"除外＝**替身闪避**（自动，交给内核）。
    /// * **「滑稽暗黑兔」(45536531)**：① 召唤·特召时给一次**额外通召**（限"记述卡通世界"的怪兽）；
    ///   ③ 从卡组拿/表侧放置一张「卡通」场地·永续魔法——STEP 1 的起手点。
    /// * **「邪魔箱」(8915275)**：① 场上有「卡通世界」时从手牌自跳，并可检索/盖放一张「卡通」**陷阱**
    ///   （优先「看透心灵之眼」）；③ 双方回合一次，把任意一方墓地 1 张卡洗回牌组最下面。
    /// * **「漫画猫」(72921536)**：② 双方主要阶段，解放自己场上 1 只怪兽 → 从手卡·卡组无视召唤条件
    ///   特召"记述卡通世界"的怪兽（**场上有卡通世界时，也能解放对方的怪兽** —— 后攻的解场手段）。
    /// * **「青眼卡通龙」(53183600)**：对方没有卡通怪兽时可直接攻击（3000）。
    /// * **「青眼卡通究极龙」(71808988)**：① 让我方卡通怪兽**全部**可以直接攻击（斩杀点）。
    ///
    /// 另外按老规矩带两条护栏：手坑不许通召（见 <see cref="HandTraps"/>）、
    /// 候选里同时有双方时先选对面的（「漫画猫」「邪魔箱③」「看透心灵之眼②」都要选对面的卡）。
    /// </summary>
    [Deck("ToonShop", "AI_Toon")]
    class ToonShopExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            public const int PerfectWorld = 7293697;     // 完美世界 卡通世界（场地）
            public const int DarkRabbit = 45536531;      // 滑稽暗黑兔
            public const int ComicCat = 72921536;        // 漫画猫
            public const int BoxOfFriends = 8915275;     // 邪魔箱（=群友说的"恶魔箱"）
            public const int BlueEyesToon = 53183600;    // 青眼卡通龙
            public const int ToonUltimate = 71808988;    // 青眼卡通究极龙（融合，全场直击＝斩杀点）
            public const int MindEye = 34298391;         // 看透心灵之眼（陷阱）
            public const int ToonTerror = 53094821;      // 卡通恐怖（反击陷阱）
            public const int Bookmark = 91500017;        // 卡通书签
            public const int TableOfContents = 89997728; // 卡通目录
            public const int Terraforming = 73628505;    // 星球改造
            public const int TripleTactics = 25311006;   // 三战之才
            public const int TripleTacticsThrust = 35269904; // 三战之号
            public const int SuperPolymerization = 48130397; // 超融合（基准号）
            // ⚠ 卡表用的是**另一印刷号 48130398**（alias＝48130397）；**两个印刷号都认**：
            //   常量与下面的 AddExecutor 都登记两个号（只登记基准号时，卡表里那张匹配不上、
            //   专属规则整条落空）。
            public const int SuperPolymerizationAlt = 48130398; // 超融合（卡表里用的是这个印刷号）
            public const int CalledByTheGrave = 24224831;    // 墓穴的指名者
            public const int DimensionalShifter = 91800273;  // 次元吸引者（用错会封住自己的墓地）
            public const int DarkEyeIllusionist = 34314989;   // 暗眼幻想师·无脸幻想师（①铺看透心灵之眼、②墓地拉漫画猫）
            public const int InfiniteImpermanence = 10045474; // 无限泡影（陷阱，从手牌也能发）
            public const int DominusImpulse = 40366668;       // 灵王的波动（陷阱，对手特召时的手坑式阻抗）
            public const int FallenAndVirtuous = 30271097;    // 落胤与圣女（①破坏表侧卡／②墓地特召，二选一）
            public const int AlbionTheBrandedDragon = 87746184; // 烙印龙 阿尔比昂（额外卡组；③从卡组把「烙印」魔陷加入手卡/盖放）
            public const int Number60Dugares = 66011101;      // No.60 刻不知之杜加雷斯（额外卡组；①抽2丢1／墓地守备特召／攻击力2倍）
        }

        /// <summary>
        /// 额外卡组出哪只的优先级。**候选来自内核**（＝这一回合真的做得出来的那些），
        /// 我们只在这堆里挑顺位最高的，做不出来的因为不在候选里会自动跳过。
        ///
        /// 为什么需要它：没有这段时走的是基类"从候选尾部取一只"，容易挑到当回合做不出来的融合怪，
        /// **动作被内核丢掉**——用户看到的就是"展开一通场上只有一个怪"。
        ///
        /// 顺位：先出斩杀用的「青眼卡通究极龙」（全场直击），再出便宜的融合怪，
        /// 最后才是连接怪（通用件）。
        /// </summary>
        private static readonly int[] ExtraDeckPriority =
        {
            CardId.ToonUltimate,    // 青眼卡通究极龙 Lv12：全场直击＝这副牌的斩杀点
            96334243,               // 忒修斯魔栖物 Lv5
            13243125,               // 至爱英雄 火焰翼侠 Lv6
            11765832,               // 共命之翼 迦楼罗 Lv6
            54757758,               // 沼地的泥龙王 Lv4（便宜的泛用融合）
            69946549,               // 捕食植物 犀角龙 Lv8
            27118421,               // 四天之龍 飢餓毒液結合龍 Lv8
            87746184,               // 烙印龍 阿爾比昂 Lv8
            60303245,               // 转生炎兽 独角兔（连接）
            29301451,               // S：P小夜骑士（连接）
            65741789,               // I：P百变莱娜（连接）
        };

        public ToonShopExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ① 先摘掉基类那两条笼统规则，换成带护栏的版本（见 ⑧）。必须先摘再加自己的。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].Type == ExecutorType.SummonOrSet || Executors[i].Type == ExecutorType.Activate)
                    Executors.RemoveAt(i);
            }

            // ② 引擎件"能发就发"：场地（②最多 3 次检索）、暗黑兔（额外通召＋拿场地）、
            //    邪魔箱（自跳＋拿卡通陷阱/洗墓地）、漫画猫（解放→从卡组无视条件特召）
            AddExecutor(ExecutorType.Activate, CardId.PerfectWorld, PerfectWorldActivate);
            AddExecutor(ExecutorType.Activate, CardId.DarkRabbit, AlwaysPlay);
            // 邪魔箱：手牌① 照旧"能跳就跳"；场上的③（洗墓地）要看有没有值得洗的目标（见 BoxOfFriendsActivate）。
            AddExecutor(ExecutorType.Activate, CardId.BoxOfFriends, BoxOfFriendsActivate);
            AddExecutor(ExecutorType.Activate, CardId.ComicCat, AlwaysPlay);
            // 暗眼幻想师·无脸幻想师：① 丢自己 → 把永续陷阱「看透心灵之眼」表侧放置（教程的核心循环第一步，
            // 用来防手坑）；② 从墓地拉「漫画猫」。两张效果都"能发就发"。
            // 「暗眼幻想师」的时机：第二支（从墓地拿 1 只再特召）**等漫画猫进墓地再发**——
            // 漫画猫是被自己的② 解放进墓的，之前发动就只能拿回暗黑兔（实测终场因此少一只漫画猫）。
            AddExecutor(ExecutorType.Activate, CardId.DarkEyeIllusionist, DarkEyeActivate);

            // ③ 检索类：书签/目录/星球改造（拿场地）、三战之才/三战之号、超融合、指名者
            AddExecutor(ExecutorType.Activate, CardId.Bookmark, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TableOfContents, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Terraforming, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleTactics, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleTacticsThrust, AlwaysPlay);
            // 「超融合」走线时不开：它会把场上的暗黑兔/漫画猫当素材吃掉（自己回合对面场上没东西可拿）。
            AddExecutor(ExecutorType.Activate, CardId.SuperPolymerization, SuperPolymerizationActivate);
            // ⚠ 卡表里的「超融合」是另一印刷号 48130398：**两个印刷号都认**，缺了这条专属规则会落空。
            AddExecutor(ExecutorType.Activate, CardId.SuperPolymerizationAlt, SuperPolymerizationActivate);
            AddExecutor(ExecutorType.Activate, CardId.CalledByTheGrave, AlwaysPlay);
            // 「落胤与圣女」(30271097)：① 二选一——(1) 从额外卡组把 1 只"记述阿不思的落胤"的怪送墓当 cost，
            //    破坏场上 1 张**表侧**卡（脚本 c30271097.lua 的目标范围是 `LOCATION_ONFIELD, LOCATION_ONFIELD`
            //    ＝两边都行）；(2) 自己场上/墓地有「艾克莉西娅」怪时，特召任意一方墓地的 1 只怪。
            //    原来它只有通用兜底：分支由基类 `Rand.Next` 掷骰子（一半的局走"破坏"），而"候选只有我方的卡"
            //    时通用选卡会从尾部取 → **炸掉自己的终场件**（实测 fin3-94：炸掉自己的「邪魔箱」、
            //    把自己表侧的「看透心灵之眼」送进墓地）。这里按卡文定死：对面上有表侧卡才走"破坏"，
            //    否则走"从墓地特召"（见 <see cref="FallenAndVirtuousActivate"/>）。
            AddExecutor(ExecutorType.Activate, CardId.FallenAndVirtuous, FallenAndVirtuousActivate);

            // ④ 陷阱：看透心灵之眼（宣言卡名无效）与卡通恐怖（发动无效）——**能盖就盖**，
            //    但发动要走 <see cref="Activate"/> 的陷阱时机（只在对手召唤后／连锁对手时），
            //    不再"能发就发"（自己回合掀掉陷阱等于白送）
            //    ⚠ 实测（toon-opt，第二轮修复后）：「看透心灵之眼」② 还是会在**自己回合**开——
            //      `(Go to Main1) … (0 's 看透心灵之眼 activate effect from SpellZone) (Go to End)`，
            //      以及自己抽牌阶段（`(Go to Draw)` 紧跟激活）。原因是基类 `DefaultTrap()` 的第一个条件
            //      `Duel.LastChainPlayer == -1 && Duel.LastSummonPlayer != 0`：连锁结束后 LastChainPlayer 是 -1，
            //      而 LastSummonPlayer 在**每个阶段开始时被重置成 -1**（GameBehavior.OnNewPhase），
            //      `-1 != 0` 成立 → 空场时机一律放行（不是"对手召唤后"）。
            //      这不算浪费：② 是"宣言一个卡名 → 这个回合同名卡发动的效果无效化"，自己回合开正好
            //      用来防对手的手坑（教程口径），而且它是"1回合1次"、不占对手回合的那次。
            //      宣言了谁要看 `[探针] 宣言卡名`（见 OnAnnounceCard）。
            AddExecutor(ExecutorType.SpellSet, CardId.MindEye, AlwaysPlay);
            AddExecutor(ExecutorType.SpellSet, CardId.ToonTerror, AlwaysPlay);
            // 「完美世界 卡通世界」**一律发动、不许盖放**（谓词恒 false 的点名规则）——它的作用不是"盖"，
            // 而是让基类那条"万事皆可盖放"的通用规则让位（见 <see cref="NeverSetSpell"/> 与基类那条
            // `AddExecutor(ExecutorType.SpellSet)` 的 `DefaultNoExecutor` 判据）。
            AddExecutor(ExecutorType.SpellSet, CardId.PerfectWorld, NeverSetSpell);

            // ⑤「次元吸引者」**只在对手回合**开：它会让"送去墓地的卡改为除外"，
            //    而我们的「漫画猫」解放、「青眼卡通究极龙」的接触融合都要用墓地——自己回合开等于自断。
            AddExecutor(ExecutorType.Activate, CardId.DimensionalShifter, DimensionalShifter);

            // ⑤.5「青眼卡通究极龙」②（墓地回收）与 ③（被攻击时的除外闪避）要有**专属规则**：
            //    没有专属规则时它们掉进下面 ⑥ 的通用兜底，而那条在"还能从额外卡组出怪"时会一律
            //    `return false`（让路逻辑，见 <see cref="Activate"/>）。实测首回合 50 局（maxboard-94 +
            //    perf-blank-94）：究极龙站过十几局、② 只发动 1～2 次——例如 perf-blank-94 第 8 局，
            //    首回合究极龙已经出场、墓地里躺着「卡通目录/卡通书签/暗眼幻想师/完美世界 卡通世界」
            //    （② 完全可发），但整回合一次没发；而整局看它是有在发的（maxboard-94 的 20 局共 13 次），
            //    差别就是"首回合场上怪多、额外卡组还能出连接怪"。
            //    教程第 7 步明确要它"回收卡通目录（借场地③ 刷新再回收暗眼）"——后面的
            //    "目录检索书签 → 书签检索邪魔箱"整段全靠这一下，线就断在这里。
            AddExecutor(ExecutorType.Activate, CardId.ToonUltimate, ToonUltimateActivate);

            // ⑥ 通用兜底：任何怪都能通召/盖放、任何效果都能发动——但**否决手坑**（见 HandTraps）。
            //    放在最后加＝专属规则优先、通用兜底。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));

            // ⑦ 计划层的额外召唤白名单：走线时**只放行这条线要用的「青眼卡通究极龙」**（接触融合），
            //    其余额外怪（S：P小夜骑士 / I：P百变莱娜 / 独角兔 / 各种融合怪）这一回合先不出——
            //    实测首回合它们会把场上的暗黑兔/邪魔箱当素材吃掉，整条卡通线当场断掉（16 局里终场一件都没留下）。
            //    「青眼卡通龙」也不能自己跳：它要解放 2 只怪，正是线里要留的暗黑兔。
            //    只对点名的卡生效：基类那条笼统的 SpSummon（DefaultNoExecutor）会自动让位给点名规则。
            foreach (int cardId in OffPlanExtraMonsters)
                AddExecutor(ExecutorType.SpSummon, cardId, AllowOtherExtraSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.BlueEyesToon, AllowOtherExtraSummon);
            // 「青眼卡通究极龙」的接触融合要等**场上凑够 2 只暗黑兔 + 手卡青眼卡通龙**（教程那一步）。
            // 不拦的话它一有机会就融合——实测第一次融合就把场上的暗黑兔和邪魔箱顺手吃掉，
            // 后面的"漫画猫② 拉第二只暗黑兔 → 目录拿青眼卡通龙"整条线全断（首回合终场一件不剩）。
            AddExecutor(ExecutorType.SpSummon, CardId.ToonUltimate, AllowUltimateSummon);

            // ⑧ 斩杀优先：**插到列表最前面**。内核的动作循环是"外层遍历规则、内层遍历候选"，
            //    只有排在前面才会在"还有事可做"（场地②/检索/融合…）之前被问到；排在后面的话
            //    战斗阶段会被一直推后（用户实测：明明能一刀收掉却在继续做场）。
            //    第二条是"为了这一刀把蹲着的守备怪转成攻击表示"（表示形式的选择见 OnSelectPosition）。
            Executors.Insert(0, new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));
        }

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>
        /// 这条线的场地**只许发动、不许盖放**（登记在构造函数 ④ 那张 SpellSet 名单里）。
        ///
        /// 为什么需要它：盖放（SpellSet）在基类 `DoEverythingExecutor` 里有一条**对所有卡生效**的通用规则
        /// （`DoEveryThingExecutor.cs:21` `AddExecutor(ExecutorType.SpellSet)`，判据是 `Executor.DefaultNoExecutor`），
        /// 而它排在所有点名规则**前面** → 手里的「完美世界 卡通世界」会先被它"摆"进魔陷区：日志里只有
        /// `(0 's 完美世界 卡通世界 from Hand move to SpellZone)`，**没有** `activate effect`（对照：
        /// 同一局的对手真发动「机械驱动之夜」时是 `… move to SpellZone` 紧跟 `… activate effect from SpellZone`）。
        /// 而场地区只能有一张——**第二张一贴就把第一张破坏掉**：
        /// 真人局 2026-10-06 14:27（局2）`… from Hand move to SpellZone`（日志 4081）→
        /// `… from SpellZone move to Grave`（4083）→ `… from Hand move to SpellZone`（4084）：
        /// 两张场地只换来**一次**② 的机会（4094），还被对手「魔女术的歪曲」连人带效果清掉（4099-4101）→
        /// 那一局**0 次检索**；14:58 那局同型（`… move to SpellZone`（12877）→ `… move to Grave`（12878）
        /// → `… move to SpellZone`（12879））。
        /// 登记这条点名规则（谓词恒 false）后，基类那条通用规则会因为 `DefaultNoExecutor` 让位，
        /// 场地就交给 <see cref="PerfectWorldActivate"/> 去**发动**。
        /// </summary>
        private bool NeverSetSpell()
        {
            return false;
        }

        /// <summary>
        /// 场地的发动闸门：**场上已经有一张"② 还没用过"的场地时，不要再从手牌贴第二张**。
        ///
        /// 卡脚本 `c7293697.lua`：② 是 `e2:SetCountLimit(1)`（每张卡一次）＋
        /// `Duel.GetFlagEffect(tp,id)<3`（每回合最多三次）。所以"贴新场地顶掉旧场地"这个循环
        /// **只在旧场地的 ② 已经用掉之后才有意义**；旧场地还没给过检索（`_planFieldSearches == 0`）时
        /// 从手牌再贴一张＝白扔一张场地（旧的那张被规则破坏，这一回合的检索次数也不会变多）。
        /// 只拦"从手牌贴"（`Card.Location == Hand`）：② 自己发动时 `Card` 在魔陷区，不受这条影响。
        /// 没走线时（`_planFieldSearches` 只在计划层里计数）保持原来的"能发就发"，不把这条闸门变成
        /// "永远不贴第二张"。
        /// </summary>
        private bool PerfectWorldActivate()
        {
            if (PlanActive() && Card != null && Card.Location == CardLocation.Hand
                && BotHasOnField(CardId.PerfectWorld) && _planFieldSearches == 0)
            {
                return false;
            }
            return true;
        }

        /// <summary>
        /// 手坑与陷阱的**时机**：一律走 WindBot 原生执行器给这些卡准备好的判据，
        /// 不再"能发就发"——用户实测的问题就是"开局自己回合丢 G""手坑乱扔""陷阱在自己回合开"：
        /// * 增殖的G —— 只在**对手回合**开（<see cref="DefaultExecutor.DefaultMaxxC"/>）；
        /// * 灰流丽 —— 只在**连锁对手的卡**时开；
        /// * 幽鬼兔/屋敷童 —— 对手召唤后或连锁对手时；
        /// * 效果遮蒙者/无限泡影 —— 交给它们的"要无效对面哪只怪"判据；
        /// * 锁鸟/朔夜时雨/欢聚友伴 —— 只在对手回合、且连锁对手的卡（否则纯白扔）；
        /// * 其余陷阱 —— 对手召唤后或连锁对手时才开（自己回合开陷阱＝空过）。
        /// </summary>
        private bool Activate()
        {
            if (Card != null)
            {
                if (IsAny(Card, HandTrapMaxxC))
                    return DefaultMaxxC();
                if (IsAny(Card, HandTrapAsh))
                    return DefaultAshBlossomAndJoyousSpring();
                if (IsAny(Card, HandTrapOgre))
                    return DefaultGhostOgreAndSnowRabbit();
                if (IsAny(Card, HandTrapVeiler))
                    return DefaultEffectVeiler();
                if (IsAny(Card, HandTrapBelle))
                    return DefaultGhostBelleAndHauntedMansion();
                if (Card.IsCode(CardId.InfiniteImpermanence))
                    return DefaultInfiniteImpermanence();
                if (IsAny(Card, HandTrapChainOnly))
                {
                    // 「小丑与锁鸟」是**对称锁**（卡文 94145022："这个回合，**双方**不能从卡组把卡加入手卡"）：
                    // 在自己回合丢它＝把自己这回合的检索全锁死（真人局实测过一次整回合空过）。
                    // 欢聚友伴两张（水母/山雀）是"对手召唤才抽"，自己回合丢等于白扔一张。
                    // 欢聚友伴两张（水母/山雀）是"发动后对手每次召唤我就抽 1"，而**发动条件本身只有
                    // "自己场上没有卡"**（`c84192580.lua`/`c42141493.lua` 的 `s.drcon`）——内核在任何时点
                    // 都可能问一次，原来"连锁对手的卡"会在对面还没出怪时白扔。改成**对手这一回合确实召唤过**
                    // 之后才交（`EnemySummonedThisTurn`，见基类；`Duel.LastSummonPlayer` 在连锁开始与阶段
                    // 开始都会被重置成 -1，连锁里判不出来）。
                    if (Card.IsCode(84192580) || Card.IsCode(42141493))
                        return MulcharmyReady();   // 档位见 DefaultExecutor.MulcharmyWaitSummon（A/B：环境变量 MULCHARMY_GATE）
                    // 「锁鸟」不变：对手回合 + 连锁对手的卡（对称锁，自己回合丢＝自锁检索）。
                    if (Card.IsCode(94145022))
                        return Duel.Player == 1 && Duel.LastChainPlayer == 1;
                    // 「朔夜时雨」只废对面一只怪、不伤自己，对手在我们回合特召时也该跟上，所以任何回合都可以。
                    return Duel.LastChainPlayer == 1;
                }
                if (Card.HasType(CardType.Trap))
                    return DefaultTrap();
                // 现在能从额外卡组出怪 → 通用效果/盖牌让路（专属规则在前面，不受影响）
                if (ExtraSummonAvailable())
                    return false;
            }
            return DefaultDontChainMyself();
        }

        /// <summary>
        /// 现在能不能从额外卡组出怪：内核把额外怪放进了这回合"可召唤/可特殊召唤"的列表里。
        /// 只在**自己的回合**采信（对手回合那份列表是陈数据）。
        /// </summary>
        private bool ExtraSummonAvailable()
        {
            if (Duel.Player != 0)
                return false;
            MainPhase main = Duel.MainPhase;
            if (main == null)
                return false;
            foreach (ClientCard card in main.SpecialSummonableCards)
            {
                if (card.Controller == 0 && card.Location == CardLocation.Extra)
                    return true;
            }
            foreach (ClientCard card in main.SummonableCards)
            {
                if (card.Controller == 0 && card.Location == CardLocation.Extra)
                    return true;
            }
            return false;
        }

        /// <summary>这张卡是不是列表里的某一张（同一张卡的多个印刷号都算）。</summary>
        private static bool IsAny(ClientCard card, int[] ids)
        {
            foreach (int id in ids)
            {
                if (card.IsCode(id) || card.IsOriginalCode(id))
                    return true;
            }
            return false;
        }

        private static readonly int[] HandTrapMaxxC = { 23434538, 23434539 };                 // 增殖的G
        private static readonly int[] HandTrapAsh = { 14558127, 14558128, 14558129 };         // 灰流丽
        private static readonly int[] HandTrapOgre = { 59438930, 59438931 };                  // 幽鬼兔
        private static readonly int[] HandTrapVeiler = { 97268402, 97268403 };                // 效果遮蒙者
        private static readonly int[] HandTrapBelle = { 73642296, 73642297, 73642298 };       // 屋敷童
        /// <summary>只在"对手回合 + 连锁对手的卡"时才能交的手坑（自己回合丢出去纯亏一张）。</summary>
        private static readonly int[] HandTrapChainOnly = { 94145022, 52038441, 84192580, 42141493 };

        /// <summary>次元吸引者：只在对手回合发动（自己回合会封掉我们自己的墓地利用）。</summary>
        private bool DimensionalShifter()
        {
            return Duel.Player == 1;
        }

        /// <summary>
        /// 「落胤与圣女」① 的分支：**对面上有表侧卡才走"破坏"**（而且只炸对面的），否则走"从墓地特召"。
        ///
        /// 卡文（cards.cdb 30271097 与脚本 c30271097.lua）：① 二选一——
        /// ●从额外卡组把有「阿不思的落胤」卡名记述的 1 只怪兽送去墓地，以**场上 1 张表侧表示卡**
        ///   为对象（选对象用的是 `Duel.SelectTarget(..., LOCATION_ONFIELD, LOCATION_ONFIELD, ...)`，
        ///   两边场上的表侧卡都在候选里）→ 那张卡破坏；
        /// ●自己的场上或墓地有「艾克莉西娅」怪兽的场合，以自己或对方的墓地 1 只怪兽为对象 → 那只在自己场上特召。
        ///
        /// 为什么必须定死分支：基类 `DoEverythingExecutor.OnSelectOption` 是 `Rand.Next`（掷骰子），
        /// 一半的局会走"破坏"，而"候选里全是我方的卡"时通用选卡会从尾部取 → 实测 fin3-94 里
        /// 炸掉自己的「邪魔箱」、把自己表侧的「看透心灵之眼」送墓（都是终场件，红线"不自己炸自己"）。
        ///
        /// ⚠ 光"登记分支"还不够：**"特召"那一支列不出来的时候（对面上没有表侧卡、自己场上/墓地又没有
        /// 「艾克莉西娅」），选项表里只剩"破坏"，而这个效果的破坏目标范围含我方的卡** → 登记得再对也只会
        /// 炸自己。所以这里改用"**两个条件都不满足就整个不发动**"（详见下面的内联注释）。
        /// </summary>
        private bool FallenAndVirtuousActivate()
        {
            if (HasEnemyFaceupCard())
            {
                _optionValue = EncodedOption(CardId.FallenAndVirtuous, FallenBranchDestroy);   // 分支 1＝破坏表侧卡
                return true;
            }
            // 对面没有表侧卡可炸 → 只剩"从墓地特召"这一支有意义。
            // ⚠ 但这一支有脚本写死的前置条件（c30271097.lua `s.cfilter2`：**自己场上/墓地要有
            //   「艾克莉西娅」怪兽**，setcode 0x1d7）：前置不成立时内核算出的 `b2=false`，
            //   **选项表里根本不会列出"特召"这一项**；而"破坏"那一项一定列得出来
            //   （它的目标范围是 `LOCATION_ONFIELD, LOCATION_ONFIELD` ＝**双方场上的表侧卡都算**），
            //   于是效果只会去炸**自己场上**的表侧卡。所以这种局面下**一张都不该发动**。
            //   实测（toon-fix1-30 第 7 局＝第 3 回合，我方）：
            //     「(0 's 落胤与圣女 activate effect from SpellZone)」
            //     →「[探针] 选项：正在结算=落胤与圣女 options=[484337553] 登记值=484337555」
            //     →「(0 's 黑龙之艾克莉西娅 from Extra move to Grave)」（交了破坏分支的费用）
            //     →「(SpellZone 's 完美世界 卡通世界 become target)」
            //     →「(0 's 完美世界 卡通世界 from SpellZone move to Grave)」——自己的场地被自己炸掉。
            //   同一份日志里同样的路子还炸过「看透心灵之眼」×5、「青眼卡通究极龙」×2、「漫画猫」×2、
            //   「暗眼幻想师」×2、「邪魔箱」、「卡通书签」。
            if (!HasEcclesiaForFallen())
                return false;
            _optionValue = EncodedOption(CardId.FallenAndVirtuous, FallenBranchRevive);      // 分支 2＝从墓地特召
            return true;
        }

        /// <summary>
        /// 「落胤与圣女」① 的两个分支号＝卡脚本 c30271097.lua 里
        /// `{b1,aux.Stringid(id,1),1},{b2,aux.Stringid(id,2),2}` 的**Stringid 序号**（也是分支号）。
        /// </summary>
        private const int FallenBranchDestroy = 1;   // 破坏场上 1 张表侧卡（双方都行 → 要自己盯住"别炸自己"）
        private const int FallenBranchRevive = 2;    // 从自己或对方的墓地特召 1 只怪

        /// <summary>
        /// 「落胤与圣女」② 的前置：自己场上/墓地有没有「艾克莉西娅」怪兽
        /// （脚本 c30271097.lua `s.cfilter2`：`c:IsSetCard(0x1d7)`）。
        /// 本副牌只有额外卡组的「黑龙之艾克莉西娅」(78397661) 带这个字段，
        /// 所以它进墓地之前 ② 这一支根本发不出来。
        /// </summary>
        private bool HasEcclesiaForFallen()
        {
            foreach (ClientCard card in Bot.MonsterZone)
            {
                if (card != null && card.HasSetcode(0x1d7))
                    return true;
            }
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (card != null && card.HasSetcode(0x1d7))
                    return true;
            }
            return false;
        }

        /// <summary>对面场上有没有表侧表示卡（「落胤与圣女」① 的破坏目标两边都行，但要先看有没有对面的）。</summary>
        private bool HasEnemyFaceupCard()
        {
            foreach (ClientCard card in Enemy.MonsterZone)
            {
                if (card != null && card.IsFaceup())
                    return true;
            }
            foreach (ClientCard card in Enemy.SpellZone)
            {
                if (card != null && card.IsFaceup())
                    return true;
            }
            return false;
        }

        /// <summary>手坑：留在手里才有用，绝不能通召/盖放上场。同一张卡的多个印刷号都列上。</summary>
        private static readonly int[] HandTraps =
        {
            14558127,   // 灰流丽
            14558128,
            14558129,
            23434538,   // 增殖的G
            23434539,
            52038441,   // 朔夜时雨
            59438930,   // 幽鬼兔
            59438931,
            97268402,   // 效果遮蒙者
            97268403,
            94145022,   // 小丑与锁鸟
            84192580,   // 欢聚友伴·抖抖海月水母
            42141493,   // 欢聚友伴·茸茸长尾山雀
        };

        /// <summary>
        /// 「从卡组特召/拉人」（漫画猫② 等）的顺序——按 202605 教程的核心循环：
        /// 漫画猫解放自己之后**拉第二只「滑稽暗黑兔」**（用它的③继续贴新场地、把场地②的第 3 次检索刷出来），
        /// 而不是去拉青眼卡通龙（青眼卡通龙是给"2 只暗黑兔 + 手卡青眼卡通龙 → 青眼卡通究极龙"接触融合用的）。
        /// </summary>
        private static readonly int[] SummonPriority =
        {
            CardId.DarkRabbit,     // 滑稽暗黑兔（继续循环）
            CardId.ComicCat,       // 漫画猫
            CardId.BoxOfFriends,   // 邪魔箱
            CardId.DarkEyeIllusionist, // 暗眼幻想师·无脸幻想师（②从墓地拉漫画猫）
        };

        /// <summary>
        /// 「检索/拿卡」这类**纯我方候选**时的取卡优先级——**按 202605 教程的展开顺序**排：
        ///
        /// 场地②（一回合能发 3 次，靠换新场地刷新）依次拿：
        /// **暗眼幻想师·无脸幻想师**（手牌效果丢自己→把永续陷阱「看透心灵之眼」表侧放置，防手坑）
        /// → **漫画猫**（解放自己→特召第二只暗黑兔）→ **卡通目录**（再拿青眼卡通龙去做究极龙）；
        /// 目录/书签拿：**青眼卡通龙**（接触融合素材）→ **卡通书签** → **邪魔箱**；
        /// 卡通陷阱里先拿**卡通恐怖/叙述者**（三色康反击），「看透心灵之眼」是暗眼幻想师铺的。
        /// </summary>
        private static readonly int[] PreferredPicks =
        {
            CardId.DarkEyeIllusionist,   // 暗眼幻想师·无脸幻想师（先铺看透心灵之眼）
            CardId.ComicCat,             // 漫画猫
            CardId.TableOfContents,      // 卡通目录
            CardId.BlueEyesToon,         // 青眼卡通龙（和 2 只暗黑兔做青眼卡通究极龙）
            CardId.Bookmark,             // 卡通书签
            CardId.BoxOfFriends,         // 邪魔箱
            CardId.ToonTerror,           // 卡通恐怖/叙述者（反击陷阱，邪魔箱① 检索）
            CardId.MindEye,              // 看透心灵之眼
            CardId.PerfectWorld,         // 完美世界 卡通世界（场地，通常由暗黑兔③ 直接放置）
            CardId.DarkRabbit,           // 滑稽暗黑兔
        };

        /// <summary>
        /// 「从墓地回收」的优先级（青眼卡通究极龙②、邪魔箱③ 这类）：教程里是先回收**卡通目录**
        /// （拿回手上继续检索）→ 再回收**暗眼幻想师·无脸幻想师**（用它②从墓地特召漫画猫）。
        /// </summary>
        private static readonly int[] RecoverPriority =
        {
            CardId.TableOfContents,
            CardId.DarkEyeIllusionist,
            CardId.Bookmark,
            CardId.PerfectWorld,
        };

        /// <summary>手牌选卡（丢手牌当费用这类）：优先丢不在关键名单里的，同档优先丢重复的。</summary>
        private bool PickForCost(IList<ClientCard> cards)
        {
            List<ClientCard> hand = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card.Location == CardLocation.Hand)
                    hand.Add(card);
            }
            if (hand.Count == 0)
                return false;
            Dictionary<int, int> counts = new Dictionary<int, int>();
            foreach (ClientCard card in hand)
                counts[card.Id] = counts.ContainsKey(card.Id) ? counts[card.Id] + 1 : 1;
            for (int pass = 0; pass < 3; ++pass)
            {
                foreach (ClientCard card in hand)
                {
                    bool key = IsPreferredPick(card);
                    bool duplicated = counts[card.Id] > 1;
                    bool ok;
                    if (pass == 0)
                        ok = !key && duplicated;
                    else if (pass == 1)
                        ok = !key;
                    else
                        ok = key && duplicated;
                    if (!ok)
                        continue;
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(card);
                    _handPick = picked;
                    return true;
                }
            }
            return false;
        }

        /// <summary>这次"手牌选卡"的结果（见 PickForCost）。</summary>
        private IList<ClientCard> _handPick;

        // ============================================================ 计划层（见插件 docs/plan-layer.md）

        /// <summary>
        /// 本回合走哪条起手线。卡通四条单卡线**共用同一个终场**（青眼卡通究极龙 + 漫画猫 + 邪魔箱
        /// ＋ 场地「完美世界 卡通世界」），只是起点不同——所以计划层的活是"把同一条 10 步线走对"。
        /// </summary>
        private enum PlanLine
        {
            /// <summary>没匹配到起手线 → 全走原来的优先级。</summary>
            None,
            /// <summary>手牌有「滑稽暗黑兔」：NS → ③ 表侧放置场地 → 场地② 依次检索 → ① 追加通召漫画猫 → …</summary>
            Rabbit,
            /// <summary>手牌有「漫画猫」：② 解放自己拉第二只暗黑兔 → 之后同暗黑兔线。</summary>
            Cat,
            /// <summary>手牌有「完美世界 卡通世界」：先贴场地 → 场地② 先拿漫画猫 → …</summary>
            Field,
            /// <summary>手牌有「卡通书签」：书签先拿暗黑兔 → 之后同暗黑兔线。</summary>
            Bookmark,
        }

        private PlanLine _planLine = PlanLine.None;

        /// <summary>这份计划是给第几回合定的（Duel.Turn）；换回合重新读手牌。</summary>
        private int _planTurn = -1;

        /// <summary>场地② 本回合发了几次（依次拿：暗眼幻想师 → 漫画猫 → 卡通目录）。</summary>
        private int _planFieldSearches;

        /// <summary>卡通目录① 本回合发了几次（第一次拿青眼卡通龙，之后拿书签/邪魔箱）。</summary>
        private int _planContentsPicks;

        /// <summary>暗眼幻想师① 本回合用了几次（先铺看透心灵之眼，再从墓地拉漫画猫）。</summary>
        private int _planDarkEyeUses;

        /// <summary>这次接触融合已经挑了几张素材（内核是"一次问一张"，靠它给 暗黑兔×2 → 青眼卡通龙）。</summary>
        private int _planFusionPicks;

        /// <summary>本回合「漫画猫」② 是不是已经用掉了（卡名一回合一次；用它决定追加通召还给不给漫画猫）。</summary>
        private bool _planCatUsed;

        /// <summary>
        /// 这一回合**开始那一刻**的手牌快照（计划层的四条起手线按它来认）。
        ///
        /// 为什么不能等"第一次真的要出牌"再读手牌：场地线与书签线的起手件是**先发动、发动之后**
        /// 才轮到内核来问"该拿哪张"的（「场地②」的检索、「书签①」的检索），那时那张卡已经离开手牌了，
        /// `EnsurePlan` 读到的是一副"没有起手件"的手牌 → 计划永远认成 None。
        /// 实测（maxboard-94，开局手牌明明有「完美世界 卡通世界」或「卡通书签」的局）：
        /// 日志里一条"计划"都没有，检索按通用优先级去拿了暗眼幻想师——书签没去找暗黑兔，
        /// 整条循环没起步（那些局首回合场地② 只检索 0～1 次）。
        /// </summary>
        private List<ClientCard> _turnStartHand = new List<ClientCard>();

        /// <summary>
        /// 这个回合**进过场的怪**（卡号）。给斩杀判据用：本家「青眼卡通龙/青眼卡通究极龙」写着
        /// "这张卡在特殊召唤的回合不能攻击"，所以这个回合才上场的怪一律先不算进"这回合能打多少"。
        /// 回合切换时清空（<see cref="OnNewTurn"/>）——对手回合里特召出来的怪，到我们回合就解禁了。
        /// </summary>
        private readonly HashSet<int> _summonedThisTurn = new HashSet<int>();

        /// <summary>
        /// 回合切换时清"这个回合才上场的怪"。注意：对手回合里的特召**也要**被我们自己的回合清掉
        /// （那些怪到我们回合已经能攻击了），所以这里不区分是谁的回合。
        /// </summary>
        public override void OnMove(ClientCard card, int previousControler, int previousLocation, int currentControler, int currentLocation)
        {
            base.OnMove(card, previousControler, previousLocation, currentControler, currentLocation);
            if (card != null && currentControler == 0 && currentLocation == (int)CardLocation.MonsterZone)
                _summonedThisTurn.Add(card.Id);
        }

        /// <summary>
        /// 「这回合可以直接打死了」——**注册在 `Executors` 的最前面**（内核的动作循环是
        /// "外层遍历规则、内层遍历候选"，`GoToBattlePhase` 只有排在前面才会在其它动作之前被问到；
        /// 排在后面的话，"场地② 检索 / 漫画猫② / 融合"这些"还有事可做"会一直把战斗阶段推后，
        /// 用户实测的原话就是"明明可以斩杀的非要继续做场"）。
        ///
        /// 判据（**保守**，宁可晚一步也不要打不死时白进战阶）：
        /// * 对面**场上没有怪**（＝可以直击）；
        /// * 我方**表侧攻击表示**、且**不是这个回合才上场**的怪，攻击力合计 ≥ 对面 LP。
        /// </summary>
        private bool LethalAvailable()
        {
            if (Duel.Player != 0 || !Duel.MainPhase.CanBattlePhase)
                return false;
            if (Enemy.GetMonsterCount() > 0)
                return false;
            int damage = LethalDamage();
            bool lethal = damage > 0 && damage >= Enemy.LifePoints;
            if (lethal && _verbose)
                Logger.WriteLine("[斩杀] 可以收掉：场上能打的攻击力合计 " + damage + " ≥ 对面 LP " + Enemy.LifePoints + "，直接进战斗阶段");
            return lethal;
        }

        /// <summary>保守口径的"这回合能打出来的伤害"：只算表侧攻击表示、且不是本回合才上场的怪。</summary>
        private int LethalDamage()
        {
            int total = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (!card.IsFaceup() || !card.IsAttack())
                    continue;
                if (_summonedThisTurn.Contains(card.Id))
                    continue;
                total += card.GetAttackPower();
            }
            return total;
        }

        /// <summary>
        /// 「为了斩杀把蹲着的守备怪改成攻击表示」：对面空场、且**把它转成攻击之后**伤害就够了 → 转。
        /// 实测（真人局日志）：对面只剩 100 血，场上的「漫画猫」蹲在守备表示没参战，
        /// 结果对方「颉颃胜负」把全场里侧除外，白白错过斩杀。
        /// 只对本回合还没改过表示形式、且不是这个回合才上场的怪有效（内核自己会判合法）。
        /// </summary>
        private bool ReposForLethal()
        {
            if (Duel.Player != 0 || !Duel.MainPhase.CanBattlePhase || Enemy.GetMonsterCount() > 0)
                return false;
            if (Card == null || !Card.IsFaceup() || !Card.IsDefense())
                return false;
            if (_summonedThisTurn.Contains(Card.Id))
                return false;
            bool enough = LethalDamage() + Card.GetAttackPower() >= Enemy.LifePoints;
            if (enough && _verbose)
                Logger.WriteLine("[斩杀] 把守备表示的「" + Card.Name + "」转成攻击表示抢这一刀");
            return enough;
        }

        /// <summary>
        /// 表示形式：**对面空场时一律选攻击表示**（其余情况交给基类）。
        ///
        /// 基类对"攻击力 0"的怪给 FaceUpDefence，其余返回 0（＝无偏好），而内核在无偏好时取候选里的
        /// 第一个——于是「漫画猫②」这种"无视召唤条件特殊召唤"（卡文没写表示形式）会把怪摆成守备，
        /// 那一刀就白白交出去了。这副牌是"全场直击"的斩杀牌，能站攻击就站攻击。
        /// </summary>
        public override CardPosition OnSelectPosition(int cardId, IList<CardPosition> positions)
        {
            if (Duel.Player == 0 && Enemy.GetMonsterCount() == 0
                && positions.Contains(CardPosition.FaceUpAttack))
                return CardPosition.FaceUpAttack;
            return base.OnSelectPosition(cardId, positions);
        }

        /// <summary>
        /// 回合一开始就把手牌拍下来（只在**自己的回合**记；对手回合的计划一律 None）。
        /// 不在这里算计划本身：计划要等首次真的要用时才展开（那时手里可能已经抽到了新牌），
        /// 快照只管"回合开始时手上有什么"。
        /// </summary>
        public override void OnNewTurn()
        {
            base.OnNewTurn();
            _summonedThisTurn.Clear();   // 新的回合：上一回合上场的怪这一回合已经能攻击了
            _planTurn = -1;
            _turnStartHand = new List<ClientCard>();
            if (Duel.Player != 0)
                return;
            foreach (ClientCard card in Bot.Hand)
            {
                if (card != null)
                    _turnStartHand.Add(card);
            }
        }

        /// <summary>起手件是不是在"这一回合开始时的手牌"里（印刷号不同的同一张卡都算）。</summary>
        private bool HasPlanStarter(int cardId)
        {
            foreach (ClientCard card in _turnStartHand)
            {
                if (IsSameCard(card, cardId))
                    return true;
            }
            // 快照是"抽牌前"的手牌：抽到的新牌也算（没拍到快照时也走这条，等于原来的行为）。
            return HasInHand(cardId);
        }

        /// <summary>是否打印调试信息（命令行 ``Debug=true``）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>已经打过日志的计划回合。</summary>
        private int _loggedPlanTurn = -1;

        /// <summary>
        /// 每回合读一次手牌匹配起手线——本回合的检索、通召、融合素材都问它"这条线下一步缺哪一环"。
        /// 只做集合判断，合法性交给内核与卡脚本；没匹配到就回原来的优先级（不会把原来会打的局打坏）。
        /// </summary>
        private void EnsurePlan()
        {
            if (Duel.Player != 0)
            {
                _planLine = PlanLine.None;
                return;
            }
            if (_planTurn == Duel.Turn)
                return;
            _planTurn = Duel.Turn;
            _planLine = PlanLine.None;
            _planFieldSearches = 0;
            _planContentsPicks = 0;
            _planDarkEyeUses = 0;
            _planFusionPicks = 0;
            _planCatUsed = false;
            // 按教程的起手件优先级匹配（四条线终场相同，先匹配到的先用）。
            // ⚠ 用手牌快照（<see cref="_turnStartHand"/>）而不是当前手牌：场地/书签这两条的起手件
            // 是"发动之后"才轮到我们回答的，那时它已经不在手牌里了（见快照的注释）。
            if (HasPlanStarter(CardId.DarkRabbit))
                _planLine = PlanLine.Rabbit;
            else if (HasPlanStarter(CardId.ComicCat))
                _planLine = PlanLine.Cat;
            else if (HasPlanStarter(CardId.PerfectWorld))
                _planLine = PlanLine.Field;
            else if (HasPlanStarter(CardId.Bookmark))
                _planLine = PlanLine.Bookmark;
            if (_verbose && _planLine != PlanLine.None && _loggedPlanTurn != Duel.Turn)
            {
                _loggedPlanTurn = Duel.Turn;
                Logger.WriteLine("第 " + Duel.Turn + " 回合计划：" + _planLine + " 线");
            }
        }

        /// <summary>手牌里有没有这张卡（同一张卡的多个印刷号都算）。</summary>
        private bool HasInHand(int cardId)
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>场上（自己的怪兽区 + 魔陷区）有没有这张卡。场地魔法在魔陷区，两边都要看。</summary>
        private bool BotHasOnField(int cardId)
        {
            foreach (ClientCard card in Bot.MonsterZone)
            {
                if (card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                    return true;
            }
            foreach (ClientCard card in Bot.SpellZone)
            {
                if (card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                    return true;
            }
            return false;
        }

        /// <summary>本回合有没有一条正在执行的起手线。</summary>
        private bool PlanActive()
        {
            EnsurePlan();
            return Duel.Player == 0 && _planLine != PlanLine.None;
        }

        /// <summary>是不是某张卡（空值安全，同一张卡的多个印刷号都算）。</summary>
        private static bool IsSameCard(ClientCard card, int cardId)
        {
            return card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId));
        }

        /// <summary>
        /// 现在是谁的效果在问（正在发动/结算的那张卡）。发动时的费用/目标选择用 GetCurrentChainCard()，
        /// 效果结算中用 GetCurrentSolvingChainCard()。
        /// </summary>
        private ClientCard CurrentEffectCard()
        {
            ClientCard solving = Duel.GetCurrentSolvingChainCard();
            if (solving != null)
                return solving;
            return Duel.GetCurrentChainCard();
        }

        /// <summary>
        /// 「青眼卡通究极龙」的接触融合闸门：true＝还要等线路进度（教程第 7 步的位置，现状）；
        /// false＝只看素材够不够（放宽口径）。**A/B 开关**，判定用 40 局/腿 + 同 build 重跑当噪声基线。
        /// </summary>
        private static readonly bool StrictUltimateGate = false;

        /// <summary>计划进行中要"先放一放"的额外怪：通用连接怪与超融合用的融合怪（会把暗黑兔/邪魔箱当素材吃掉）。</summary>
        private static readonly int[] OffPlanExtraMonsters =
        {
            29301451,   // S：P小夜骑士
            65741789,   // I：P百变莱娜
            60303245,   // 转生炎兽 独角兔
            96334243,   // 忒修斯魔栖物
            27118421,   // 四天之龍 飢餓毒液結合龍
            69946549,   // 捕食植物 犀角龙
            87746184,   // 烙印龍 阿爾比昂
            13243125,   // 至爱英雄 火焰翼侠
            11765832,   // 共命之翼 迦楼罗
            54757758,   // 沼地的泥龙王
            78397661,   // 黑龙之艾克莉西娅
            93039340,   // 灾厄之星 提·丰
            66011101,   // No.60 刻不知之杜加雷斯
        };

        /// <summary>
        /// 额外召唤的闸门（见构造函数 ⑦）：
        /// * 计划进行中 → 让路（原来的行为）；
        /// * **没认到起手线**（`_planLine == None`，手牌里没有暗黑兔/漫画猫/场地/书签这四张起手件）
        ///   → **也要看场上还有没有线件**：只看 `PlanActive()` 会漏掉一大片"手牌没起手件"的回合，
        ///   而那些回合正是"额外怪把场上的线件当素材吃掉"的高发区。
        ///
        /// 为什么必须补这一条（toon-opt，第二轮修复后的 10 局，用户口径 ③"不浪费资源"）：
        /// 一次连接召唤就把终场件送掉——`(0 's 邪魔箱 from MonsterZone move to Grave)`、
        /// `(0 's 漫画猫 from MonsterZone move to Grave)` 紧跟
        /// `(0 's S：P小夜骑士 from Extra move to MonsterZone)`；
        /// 同一份日志里另一处连着两跳：暗眼幻想师＋漫画猫 → S：P小夜骑士 →
        /// 青眼卡通究极龙＋小夜骑士＋青眼卡通龙＋滑稽暗黑兔 → I：P百变莱娜，
        /// 一整场做出来的怪全变成连接素材；还有 `(0 's 漫画猫 from MonsterZone move to Grave)`
        /// 紧跟 `(0 's 转生炎兽 独角兔 from Extra move to MonsterZone)`。
        /// 判定只看**自己场上**（怪兽区＋魔陷区）还在不在名单里的线件；场上只剩身体
        /// （次元吸引者这类非线件）时照旧放行，不把这条闸门变成"永远不出额外怪"。
        /// </summary>
        private bool AllowOtherExtraSummon()
        {
            if (PlanActive())
                return false;
            return !LinePieceOnField();
        }

        /// <summary>
        /// 自己场上还有没有"线件"（见 <see cref="ProtectedOnField"/>）。用来挡住额外怪拿它们当素材
        /// （<see cref="AllowOtherExtraSummon"/>）。
        /// </summary>
        private bool LinePieceOnField()
        {
            foreach (int cardId in ProtectedOnField)
            {
                if (BotHasOnField(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// "不许被额外怪当素材吃掉"的线件（用户口径 ③ 点名的就是这批）：暗黑兔/漫画猫/邪魔箱/暗眼幻想师/
        /// 青眼卡通龙（接触融合素材）＋融合出来的「青眼卡通究极龙」（4500 直击的斩杀点）。
        /// 只收怪兽：连接/融合的素材只能是怪兽，「看透心灵之眼」这类魔陷放进来只会把闸门白白收紧。
        /// </summary>
        private static readonly int[] ProtectedOnField =
        {
            CardId.DarkRabbit,           // 滑稽暗黑兔（③ 贴场地 = 循环的发动机）
            CardId.ComicCat,             // 漫画猫（② 从卡组拉暗黑兔 / 终场件）
            CardId.BoxOfFriends,         // 邪魔箱（③ 洗墓地 / 终场件）
            CardId.DarkEyeIllusionist,   // 暗眼幻想师·无脸幻想师（② 从墓地拉漫画猫）
            CardId.BlueEyesToon,         // 青眼卡通龙（接触融合素材）
            CardId.ToonUltimate,         // 青眼卡通究极龙（终端）
        };

        /// <summary>
        /// 「青眼卡通究极龙」接触融合的闸门：走线时要"素材齐（暗黑兔线＝场上 2 只暗黑兔；场地线＝
        /// 暗黑兔＋邪魔箱）＋ 手卡/场上/墓地有青眼卡通龙"才放行（素材可以在手卡·场上·墓地，所以三处都数一遍）。
        /// 没计划时照旧"能做就做"。
        /// </summary>
        private bool AllowUltimateSummon()
        {
            if (!PlanActive())
                return true;
            // 教程第 7 步的位置：**场地② 已经检索到第 3 步（卡通目录）之后**才融合。
            // 只看"有几只暗黑兔"挡不住提前融合（引擎给的融合候选里不一定有第二只兔子，实测它就顺手
            // 把邪魔箱当第二只卡通素材吃掉，后面的漫画猫/目录整条线断掉）；用"场地② 检索到第几步"
            // 这个线路状态来卡，才和教程的步骤顺序对齐。
            // ⚠ `StrictUltimateGate` 是 A/B 开关（0.21.91）：false ＝ 只看素材不看线路进度（放宽口径），
            // 用来判"要不要为了线路顺序多等一步"；判定要 40 局/腿 + 同 build 重跑当噪声基线。
            if (StrictUltimateGate && _planFieldSearches < PlanFieldSteps().Length)

                return false;
            int blueEyes = Bot.Hand.GetCardCount(CardId.BlueEyesToon)
                + Bot.MonsterZone.GetCardCount(CardId.BlueEyesToon)
                + Bot.Graveyard.GetCardCount(CardId.BlueEyesToon);
            if (blueEyes < 1)
                return false;
            // 素材要求（卡文："「青眼卡通龙」＋卡通怪兽×2"）：**按线分开数**。
            // * 场地线（教程第三行）：素材是"暗黑兔＋邪魔箱＋青眼卡通龙"（漫画猫在这条线早被自己②
            //   解放进墓地、要留给暗眼幻想师② 拉回来，不能当素材）→ 要 1 只暗黑兔＋1 只邪魔箱；
            // * 其余三条线（教程第一/二/四行）：素材是"场上 2 只暗黑兔＋青眼卡通龙"。
            // ⚠ **这里是 A/B 变体的开关**（2026-10-05 按用户"谁强用谁"定的案：**部署严格版**）。
            // 定案依据（同格各 20 局，对闪刀；基线 40%）：
            // * **严格（当前部署）**：45%（9–11），且教程终场"三件齐"达成率更高（历史读数 14% vs 3.6%）；
            // * 放宽（把下面几行注释掉即可切回）：40%（8–12）。
            // 两者差 1 局，**在 n=20 的噪声内**（±25%）——下一个想重判的人请用 40 局/腿，
            // 并且先给评测加上"传任意 WindBot 命令行参数"的能力（现在换变体要重新编译）。
            // A/B 变体 **strict**：
            int rabbitsForFusion = Bot.Hand.GetCardCount(CardId.DarkRabbit)
                + Bot.MonsterZone.GetCardCount(CardId.DarkRabbit);
            if (_planLine == PlanLine.Field)
            {
                int boxesForFusion = Bot.Hand.GetCardCount(CardId.BoxOfFriends)
                    + Bot.MonsterZone.GetCardCount(CardId.BoxOfFriends);
                if (rabbitsForFusion < 1 || boxesForFusion < 1)
                    return false;
                _planFusionPicks = 0;   // 这一次融合从第 0 张开始数（素材是一张一张问的，见 OnSelectCard）
                return true;
            }
            if (rabbitsForFusion < 2)
                return false;
            _planFusionPicks = 0;       // 这一次融合从第 0 张开始数
            return true;
        }

        /// <summary>
        /// 「暗眼幻想师」的时机：走线时，**手牌/场上还捏着漫画猫就别发第二支**——它在等漫画猫被自己的②
        /// 解放进墓地之后再拉回来（教程第 10 步）。漫画猫不在手/场/墓（没抽到）时不拦，照旧"能发就发"。
        /// </summary>
        private bool DarkEyeActivate()
        {
            if (!PlanActive())
                return true;
            bool catPending = Bot.HasInHand(CardId.ComicCat) || BotHasOnField(CardId.ComicCat);
            if (catPending)
                return false;
            return true;
        }

        /// <summary>「超融合」（两个印刷号 48130397/48130398 都认）走线时不开
        /// （会把场上的暗黑兔/漫画猫当素材吃掉）。</summary>
        private bool SuperPolymerizationActivate()
        {
            return !PlanActive();
        }

        /// <summary>
        /// 「邪魔箱」的两支效果各自该不该发（注册在构造函数 ②）：
        /// * **手牌的①**（自跳 ＋ 从卡组检索/盖放一张「卡通」陷阱）——能跳就跳（原来就是 AlwaysPlay）；
        /// * **场上的③**（双方回合 1 次，把任意一方墓地 1 张卡洗回牌组最下面）——**先看有没有值得洗的目标**。
        ///
        /// 为什么③ 要在"发动前"就拦：③ 是可选效果，而它的目标选择是 `min=1` 的**必选**
        /// （脚本 c8915275.lua `s.tdtg`：`Duel.SelectTarget(..., 1,1,nil)`），一旦发动就必然洗走 1 张。
        /// `OnSelectCard` 里的 ToDeck 分支只能做到"有别的候选时优先挑用完的场地、避开暗眼/漫画猫"——
        /// 候选里**只剩**关键件时它拦不住（会掉回通用检索优先级，`PreferredPicks` 的第一个命中就是漫画猫）。
        ///
        /// 实测（toon-opt，第二轮修复后的 10 局；第二轮已加了"优先回收用完的场地 + 避开暗眼/漫画猫"）：
        /// `(0 's 邪魔箱 activate effect from MonsterZone)` 紧跟 `(0 's 漫画猫 from Grave move to Deck)`
        /// ——漫画猫 ×3、暗眼幻想师 ×2、青眼卡通龙 ×1 次。这三张都是"放在墓地里才有用"的那一环：
        /// 漫画猫是被自己② 解放进墓、要留给暗眼幻想师② 从墓地拉回来的；暗眼幻想师是"究极龙② 回收后再铺一次"
        /// 的那一环；青眼卡通龙是接触融合的素材（手卡/场上/墓地都算）。洗回牌组＝把"回收→复活"这一对拆掉。
        /// 同一份日志里用完的场地被洗回去了 19 次（那条路照旧放行），说明闸门不会把③ 整体关掉。
        /// </summary>
        private bool BoxOfFriendsActivate()
        {
            // 手牌①（自跳 + 检索/盖「卡通恐怖」）：能跳就跳。
            // 用位置区分是哪一支：手牌上只有①、怪兽区上只有③（脚本 c8915275.lua 的 e1 范围是手牌、e3 范围是怪兽区）。
            if (Card == null || Card.Location != CardLocation.MonsterZone)
                return true;
            // 对面墓地有卡 → 洗对面的（墓地干扰），照开：下面的通用选卡会先挑对面的卡。
            foreach (ClientCard card in Enemy.Graveyard)
            {
                if (card != null && card.Id != 0)
                    return true;
            }
            // 只有自己的墓地：有**不心疼的目标**才开（用完的场地、卡通目录/书签、次元吸引者……）。
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (card == null || card.Id == 0)
                    continue;
                if (!KeepInGrave(card))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 墓地里"留着有用"的名单（「邪魔箱」③ 别把它们洗回牌组，见 <see cref="BoxOfFriendsActivate"/>）：
        /// 暗眼幻想师（② 从墓地拉漫画猫）、漫画猫（被拉回来的那一环）、青眼卡通龙（接触融合素材）。
        /// 其余（用完的场地、目录、书签、次元吸引者……）随便洗。
        /// </summary>
        private static bool KeepInGrave(ClientCard card)
        {
            return IsSameCard(card, CardId.DarkEyeIllusionist)
                || IsSameCard(card, CardId.ComicCat)
                || IsSameCard(card, CardId.BlueEyesToon);
        }

        /// <summary>
        /// 场地② 的三次检索顺序——**按起手线分开**（教程对四条单卡线各写了一遍，第二张拿什么不一样）。
        /// 三次的收益都靠"用新场地顶掉已经发过②的旧场地"（+ 场地③ 刷新暗黑兔的③）刷出来，
        /// 所以第二张拿错＝少一次检索、也常常直接断线：
        ///
        /// * **暗黑兔 / 卡通书签**（教程："之后同暗黑兔线"）：暗眼幻想师 → 漫画猫 → 卡通目录；
        /// * **漫画猫**（教程："第二张场地检索改成'搜索暗黑兔'"）：暗眼幻想师 → **暗黑兔** → 卡通目录
        ///   ——因为漫画猫② 是"这个卡名的②的效果1回合只能使用1次"（脚本 c72921536.lua
        ///   `e2:SetCountLimit(1,id)`），这条线开头它已经解放自己用掉了；第二张再搜它只能拿一只
        ///   "② 已经发不出来"的猫（实测 6 局 Cat 线全是场地②＝1～2 次、究极龙 0 只），
        ///   换成第二只暗黑兔则是**新实例、③ 是全新的一次** → 才拿得到第三张场地；
        /// * **完美世界 卡通世界**（教程那张表的第三行）：漫画猫 → 邪魔箱 → 卡通目录
        ///   ——这条线手牌里没有暗黑兔/漫画猫（有的话就认成上面两条线了），先搜漫画猫当身体，
        ///   第二张邪魔箱（① 自跳 + 盖「卡通恐怖」），第三张目录 → 青眼卡通龙，
        ///   融合素材按教程是"暗黑兔＋邪魔箱＋青眼卡通龙"。最后一张（目录）四条线都一样。
        /// </summary>
        private static readonly int[] FieldSearchStepsRabbitLine =
        {
            CardId.DarkEyeIllusionist,   // 丢自己铺「看透心灵之眼」（顺带防手坑）
            CardId.ComicCat,             // 解放自己 → 拉第二只暗黑兔
            CardId.TableOfContents,      // 目录 → 青眼卡通龙（接触融合素材）
        };

        private static readonly int[] FieldSearchStepsCatLine =
        {
            CardId.DarkEyeIllusionist,   // 丢自己铺「看透心灵之眼」
            CardId.DarkRabbit,           // 第二只暗黑兔（新实例 → ③ 再来一张场地）
            CardId.TableOfContents,      // 目录 → 青眼卡通龙
        };

        private static readonly int[] FieldSearchStepsFieldLine =
        {
            CardId.ComicCat,             // 这条线的手牌里没有怪：先拿漫画猫当身体
            CardId.BoxOfFriends,         // 自跳 + 盖「卡通恐怖」（也是接触融合的第二只卡通怪）
            CardId.TableOfContents,      // 目录 → 青眼卡通龙
        };

        /// <summary>本回合这条起手线用哪张检索顺序表（见上）。</summary>
        private int[] PlanFieldSteps()
        {
            if (_planLine == PlanLine.Cat)
                return FieldSearchStepsCatLine;
            if (_planLine == PlanLine.Field)
                return FieldSearchStepsFieldLine;
            return FieldSearchStepsRabbitLine;
        }

        /// <summary>场地② 走完三步之后的兜底顺序（书签 → 邪魔箱 → 卡通恐怖）。</summary>
        private static readonly int[] FieldSearchFallback =
        {
            CardId.Bookmark, CardId.BoxOfFriends, CardId.ToonTerror, CardId.DarkRabbit, CardId.PerfectWorld,
        };

        /// <summary>卡通目录①：第一次拿青眼卡通龙（接触融合素材），之后拿书签/邪魔箱。</summary>
        private static readonly int[] ContentsFirstPick = { CardId.BlueEyesToon, CardId.Bookmark, CardId.BoxOfFriends, CardId.ToonTerror };
        private static readonly int[] ContentsLaterPick = { CardId.Bookmark, CardId.BoxOfFriends, CardId.ToonTerror, CardId.BlueEyesToon };

        /// <summary>卡通书签①：暗黑兔线里拿邪魔箱（自跳 + 盖卡通恐怖）；书签起步的线里先拿暗黑兔。</summary>
        private static readonly int[] BookmarkPickRabbitLine = { CardId.BoxOfFriends, CardId.BlueEyesToon, CardId.ToonTerror };
        private static readonly int[] BookmarkPickBookmarkLine = { CardId.DarkRabbit, CardId.ComicCat, CardId.BoxOfFriends };

        /// <summary>邪魔箱① 的卡通陷阱：先拿「卡通恐怖」（三色康反击）。</summary>
        private static readonly int[] BoxTrapPick = { CardId.ToonTerror };

        /// <summary>暗眼幻想师① 的第二支（从墓地拿"记述卡通世界"的怪）：把**漫画猫**拉回来。</summary>
        private static readonly int[] DarkEyeGravePick = { CardId.ComicCat, CardId.DarkRabbit, CardId.BlueEyesToon };

        /// <summary>接触融合「青眼卡通究极龙」的素材顺序：暗黑兔 2 只 + 青眼卡通龙（漫画猫留下站场）。</summary>
        private static readonly int[] UltimateFusionPick = { CardId.DarkRabbit, CardId.BlueEyesToon, CardId.BoxOfFriends };

        /// <summary>走线时"这个效果该拿哪张"的顺序；没计划、认不出效果时返回 null（交回通用顺序）。</summary>
        private int[] PlanPickOrder()
        {
            if (!PlanActive())
                return null;
            ClientCard effect = CurrentEffectCard();
            if (effect == null)
                return null;
            if (IsSameCard(effect, CardId.PerfectWorld))
            {
                int[] steps = PlanFieldSteps();
                if (_planFieldSearches < steps.Length)
                {
                    int[] step = { steps[_planFieldSearches] };
                    return step;
                }
                return FieldSearchFallback;
            }
            if (IsSameCard(effect, CardId.TableOfContents))
                return _planContentsPicks == 0 ? ContentsFirstPick : ContentsLaterPick;
            if (IsSameCard(effect, CardId.Bookmark))
                return _planLine == PlanLine.Bookmark ? BookmarkPickBookmarkLine : BookmarkPickRabbitLine;
            if (IsSameCard(effect, CardId.BoxOfFriends))
                return BoxTrapPick;
            if (IsSameCard(effect, CardId.DarkEyeIllusionist))
                return DarkEyeGravePick;
            return null;
        }

        /// <summary>在候选里找某张卡（同一张卡的多个印刷号都算；找不到返回 null）。</summary>
        private static ClientCard FindFusionMaterial(IList<ClientCard> cards, int cardId)
        {
            foreach (ClientCard card in cards)
            {
                if (card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                    return card;
            }
            return null;
        }

        /// <summary>调试用：把候选拼成一行（只在开 Debug 时用）。</summary>
        private static string DescribeCardsForDebug(IList<ClientCard> cards)
        {
            string text = "";
            foreach (ClientCard card in cards)
                text += (text.Length == 0 ? "" : "、") + (card == null ? "（空）" : card.Name);
            return text.Length == 0 ? "（空）" : text;
        }

        /// <summary>候选里有没有这张卡（同一张卡的多个印刷号都算）。</summary>
        private static bool ContainsCard(IList<ClientCard> cards, int cardId)
        {
            foreach (ClientCard card in cards)
            {
                if (card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                    return true;
            }
            return false;
        }

        /// <summary>接触融合素材：先按 <see cref="UltimateFusionPick"/> 挑（暗黑兔要 2 只），不够再按候选补。</summary>
        private static IList<ClientCard> PickFusionMaterials(IList<ClientCard> cards, int min, int max)
        {
            List<ClientCard> picked = new List<ClientCard>();
            AddWanted(cards, picked, CardId.DarkRabbit, 2, max);
            AddWanted(cards, picked, CardId.BlueEyesToon, 1, max);
            AddWanted(cards, picked, CardId.BoxOfFriends, 1, max);
            if (picked.Count < min)
            {
                foreach (ClientCard card in cards)
                {
                    if (picked.Count >= max)
                        break;
                    if (card.Controller != 0 || picked.Contains(card))
                        continue;
                    picked.Add(card);
                }
            }
            return picked.Count >= min ? picked : null;
        }

        /// <summary>往融合素材里补 `want` 张（同卡号的，含印刷号），最多补到 max。</summary>
        private static void AddWanted(IList<ClientCard> cards, List<ClientCard> picked, int cardId, int want, int max)
        {
            int have = 0;
            foreach (ClientCard card in picked)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    ++have;
            }
            foreach (ClientCard card in cards)
            {
                if (have >= want || picked.Count >= max)
                    break;
                if (picked.Contains(card))
                    continue;
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                {
                    picked.Add(card);
                    ++have;
                }
            }
        }

        /// <summary>内核问"融合素材"时走这里（接触融合的候选含手卡/场上/墓地）。</summary>
        public override IList<ClientCard> OnSelectFusionMaterial(IList<ClientCard> cards, int min, int max)
        {
            // 只在这条线要出「青眼卡通究极龙」时介入：候选里得看得到青眼卡通龙，
            // 否则（超融合做别的融合怪）交回基类。
            bool looksLikeUltimate = false;
            foreach (ClientCard card in cards)
            {
                if (card != null && (card.IsCode(CardId.BlueEyesToon) || card.IsOriginalCode(CardId.BlueEyesToon)))
                    looksLikeUltimate = true;
            }
            if (PlanActive() && looksLikeUltimate)
            {
                IList<ClientCard> picked = PickFusionMaterials(cards, min, max);
                if (picked != null)
                    return picked;
            }
            return base.OnSelectFusionMaterial(cards, min, max);
        }

        /// <summary>这张卡是不是"关键名单"里的一员（检索时优先拿、当费用时优先留）。</summary>
        private static bool IsPreferredPick(ClientCard card)
        {
            foreach (int cardId in PreferredPicks)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「表侧召唤 or 里侧盖放」：**这副牌一律表侧召唤**。
        ///
        /// 基类 `DefaultExecutor.OnSelectMonsterSummonOrSet` 的判据是"等级 ≤4 且自己场上没有表侧怪
        /// 且**对手的怪全都打得过我** → 盖放"。卡通的怪**必须表侧**才有意义：
        /// 「卡通」怪兽只有在「卡通世界」在场时才表侧存在，且**里侧盖放的回合数不能攻击**
        ///（卡通怪是"召唤·反转召唤的回合不能攻击"那一类），而整套打法是"卡通怪直击"——
        /// 盖下去等于把这一回合的伤害和暗黑兔③/漫画猫② 的循环全丢了。
        /// </summary>
        public override bool OnSelectMonsterSummonOrSet(ClientCard card)
        {
            return false;
        }

        /// <summary>通召/盖放的统一入口：手坑一律否掉；起手件优先（见 PreferredSummons）；其余交给基类。</summary>
        private bool SummonOrSet()
        {
            if (Card != null)
            {
                foreach (int id in HandTraps)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return false;
                }
                // 走线时按"这条线下一步要谁上场"挑：起手件还在手上 → 通召只认它（内核给的可通召列表
                // 顺序不固定，别的怪排在前面就会把通召点抢走）；起手件已经下去 → 这次是**暗黑兔① 的
                // 追加通召**，要的是「漫画猫」（它 ② 解放自己从卡组拉第二只暗黑兔）。
                if (PlanActive())
                {
                    // 起手件还在手上、**而且还没下过场** → 通召只认它。注意"下过场"要看场上：
                    // 场上已经有暗黑兔时再黏着它，追加通召就不会给漫画猫了（实测卡在这里）。
                    // ⚠ "还在手上"不等于"还没下过场"：暗黑兔被「命王的螺旋」这类卡**弹回手牌**后卡还在手上，
                    //   但它 ① 的追加通召是**一回合一次**（脚本 c45536531.lua `s.sumop`：
                    //   `if Duel.GetFlagEffect(tp,id)~=0 then return end`），再把它通召一次＝把这次追加通召
                    //   白扔在一只"① 已经发过"的兔子上，计划下一步要的「漫画猫」（② 解放自己 → 从卡组拉
                    //   第二只暗黑兔，新兔子 ③ 全新 → 再贴一张场地）就再也没机会上场。
                    //   实测（真人局 2026-10-06 14:37＝局3，对手灵摆/霸王龙扎克，app_20261006_141705.jsonl）：
                    //   `第 2 回合计划：Rabbit 线`（5777）→ 通召暗黑兔（5842/5843）→
                    //   对手 `(1 's 命王的螺旋 activate effect from SpellZone)`（5867）→
                    //   `(0 's 滑稽暗黑兔 from MonsterZone move to Hand)`（5875）→ **又**把它通召回去
                    //   （5878，追加通召就这么用掉了）→ ③（5880）被「神之通告」无效、兔子进墓（5912）→
                    //   本回合 `(Go to End)`（5921），手牌里的「漫画猫」一直没下过场（5969-5971 还在手上）。
                    //   所以加一条"这一回合已经上过场就不再认它"（`_summonedThisTurn` 是现成的回合内名单，
                    //   见 OnMove/OnNewTurn），让这次通召落回下面的"追加通召要的就是漫画猫"。
                    if (_planLine == PlanLine.Rabbit && HasInHand(CardId.DarkRabbit)
                        && !BotHasOnField(CardId.DarkRabbit)
                        && !_summonedThisTurn.Contains(CardId.DarkRabbit))
                        return Card.IsCode(CardId.DarkRabbit) || Card.IsOriginalCode(CardId.DarkRabbit);
                    if (_planLine == PlanLine.Cat && HasInHand(CardId.ComicCat)
                        && !BotHasOnField(CardId.ComicCat) && !_planCatUsed)
                        return Card.IsCode(CardId.ComicCat) || Card.IsOriginalCode(CardId.ComicCat);
                    if (_planLine == PlanLine.Bookmark && HasInHand(CardId.DarkRabbit)
                        && !BotHasOnField(CardId.DarkRabbit)
                        && !_summonedThisTurn.Contains(CardId.DarkRabbit))
                        return Card.IsCode(CardId.DarkRabbit) || Card.IsOriginalCode(CardId.DarkRabbit);
                    // Cat 线：漫画猫的② 已经用掉（卡名一回合一次；它自己已经进了墓地）→ 这次是
                    // 暗黑兔① 给的追加通召，**要的是检索来的第二只「滑稽暗黑兔」**：教程这条线的
                    // "第二张场地检索改成搜索暗黑兔"就是为它准备的，新兔子是新实例、③ 也是全新的一次
                    // → 才拿得到第三张场地。再通召一只"② 已经发不出来"的漫画猫等于把这次通召白送
                    // （实测 maxboard-94 的 6 局 Cat 线：场地② 全是 1～2 次、究极龙 0 只）。
                    if (_planLine == PlanLine.Cat && _planCatUsed)
                        return Card.IsCode(CardId.DarkRabbit) || Card.IsOriginalCode(CardId.DarkRabbit);
                    // 暗黑兔① 的追加通召：要的就是「漫画猫」（它 ② 解放自己从卡组拉第二只暗黑兔）
                    if (Card.IsCode(CardId.ComicCat) || Card.IsOriginalCode(CardId.ComicCat))
                        return true;
                    if (Card.IsCode(CardId.DarkRabbit) || Card.IsOriginalCode(CardId.DarkRabbit))
                        return false;   // 第二只暗黑兔靠漫画猫② 从卡组拉，不占通召点
                    // 走线时这一次通召**只许花在"线上下一步的身体"上**：漫画猫还在手里、② 还没用过
                    // （＝这次追加通召本来就该给它）时，别的怪一律否掉——放它掉到下面的
                    // `PreferredSummons`/`DefaultMonsterSummon()` 就会**按内核给的可通召列表顺序**
                    // 把通召点送给排在前面的那张牌（`DefaultMonsterSummon()` 对等级 ≤4 一律放行）。
                    //
                    // 实测（真人局 2026-10-06 10:24，对手「码丽丝」，日志 app_20261006_101432.jsonl）：
                    // 手牌＝看透心灵之眼/青眼卡通龙/暗眼幻想师/漫画猫/滑稽暗黑兔，计划 Rabbit 线；
                    // `(0 's 滑稽暗黑兔 from Hand move to MonsterZone)` 之后对手翻「霆王的闪光」
                    // →`(0 's 滑稽暗黑兔 from MonsterZone move to Removed)`（起手件被除外），紧接着
                    // `(0 's 暗眼幻想师·无脸幻想师 from Hand move to MonsterZone)`——追加通召点被
                    // **暗眼幻想师**用掉：它的① 是**手牌**效果（丢自己铺「看透心灵之眼」），站场只剩
                    // 「和它战斗的怪兽都不会被那次战斗破坏」的 0 攻身体；而「漫画猫」整局一直躺在手里
                    // 没下过场（日志 02:25:04 手牌里还有它）。
                    // 那张猫的②（卡文：解放自己场上 1 只怪兽 → 从手卡·卡组把有「卡通世界」的卡名记述的
                    // 1 只怪兽无视召唤条件特殊召唤）**不需要「卡通世界」也能用**（c72921536.lua
                    // `s.spcon` 只要 `Duel.IsMainPhase()`）——起手件被除外后，这条② 是唯一还能从卡组拉出
                    // 「滑稽暗黑兔」→ ③ 贴场地 → 场地② 检索 的翻盘线；被暗眼幻想师顶掉通召点后，
                    // 这一局我方就只剩「进战斗→结束」（日志：`(Go to BattleStart)` 紧跟 `(Go to End)`）。
                    if (HasInHand(CardId.ComicCat) && !_planCatUsed && !BotHasOnField(CardId.ComicCat))
                        return false;
                }
                foreach (int id in PreferredSummons)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return true;
                }
            }
            return DefaultMonsterSummon();
        }

        /// <summary>
        /// 通召优先级：**滑稽暗黑兔**是操作方 spec 里的"伪单卡展开"起点（① 给一次额外通召、
        /// ③ 从卡组拿／表侧放置「卡通」场地），先把它摆上去；其次是**漫画猫**
        ///（② 解放 1 只怪 → 从卡组无视条件特召"记述卡通世界"的怪＝把场面做大）。
        /// 通召一回合只有一次，选错就等于把整条链断在第一步。
        /// </summary>
        private static readonly int[] PreferredSummons =
        {
            CardId.DarkRabbit,  // 滑稽暗黑兔
            CardId.ComicCat,    // 漫画猫
            // 「青眼卡通龙」也在通召名单里：它是 8 星，抽到手只能解放召唤；不写进来的话它会**一直烂在手里**
            // （实测 10 局在手 28 次、一次没发动），而这副牌就是靠它 3000 打点 + 直击赢的。
            CardId.BlueEyesToon,
        };

        /// <summary>
        /// 选项：**分支型效果一律按"值"匹配，不掷骰子**。登记值＝内核真正发给客户端的那串选项值
        /// （＝ <see cref="OptionValues"/> / <see cref="EncodedOption"/>：`Util.GetStringId(卡号, k)`
        /// ＝ `卡号 * 16 + k`，k 是卡脚本里 `aux.Stringid(卡号, k)` 的下标）：命中就把那个值在
        /// `options` 里的**下标**回回去（WindBot 的约定是回下标）。
        ///
        /// 为什么必须按值：基类 `DoEverythingExecutor.OnSelectOption` 是 `Program.Rand.Next`（**掷骰子**）
        /// ——登记不上的分支每次都由它随机挑，用户反馈的"乱发效果"就是这么来的。
        /// 为什么不能按下标：这些分支表是**动态拼的**（`aux.SelectFromOptions` / 手写 ops 表只列
        /// "做得出来"的那几支，列表会被压缩——日志实测「暗眼幻想师」出现过
        /// `options=[549039825,549039826]`、也出现过只有 `[549039825]`），按下标猜就会挑到另一支。
        ///
        /// 复核口径（探针里也照这个打）：想让某张卡走哪一支，就确认它的"登记值"**原样出现在**
        /// `options=[…]` 里、且"命中"是那个值的下标（不是"未命中"）。例：
        /// 要「落胤与圣女」走"破坏"应看到 `30271097*16+1 = 484337553`（见 <see cref="EncodedOption"/>）；
        /// 要「滑稽暗黑兔」③ 贴场地应看到 `45536531*16+3 = 728584499`。
        /// </summary>
        public override int OnSelectOption(IList<int> options)
        {
            if (options == null || options.Count == 0)
                return base.OnSelectOption(options);
            // ① 已经"登记"好的分支值（目前只有「落胤与圣女」：登记在它的 Activate 闸门里，
            //    见 <see cref="FallenAndVirtuousActivate"/>）：值里带着卡号，不会和别的卡的选项撞上；
            //    命中了才清，免得被别的卡的选项请求提前吃掉。
            int registeredHit = _optionValue == 0 ? -1 : options.IndexOf(_optionValue);
            // ② 其余分支卡：按"正在结算的那张卡"取优先值表（见 <see cref="PreferredOptionValues"/>），
            //    按值比——命中就用（列表被压缩掉某一支时会自动落到下一支）。
            ClientCard effect = CurrentEffectCard();
            int[] wanted = PreferredOptionValues(effect);
            int hit = -1;
            if (wanted != null)
            {
                foreach (int value in wanted)
                {
                    hit = options.IndexOf(value);
                    if (hit >= 0)
                        break;
                }
            }
            // 探针（只在 Debug=true 时打）：选项清单 + 登记值有没有真的出现在里面（复核口径见上面注释）。
            if (_verbose)
            {
                string list = "";
                foreach (int value in options)
                    list += (list.Length == 0 ? "" : ",") + value;
                string wantedText = "";
                if (_optionValue != 0)
                    wantedText += _optionValue;
                if (wanted != null)
                {
                    foreach (int value in wanted)
                        wantedText += (wantedText.Length == 0 ? "" : ",") + value;
                }
                Logger.WriteLine("[探针] 选项：正在结算=" + (effect == null ? "（未知）" : effect.Name)
                    + " options=[" + list + "] 登记值=[" + (wantedText.Length == 0 ? "（未登记）" : wantedText) + "]"
                    + " 命中=" + (registeredHit >= 0 ? registeredHit.ToString() : (hit >= 0 ? hit.ToString() : "未命中")));
            }
            if (registeredHit >= 0)
            {
                _optionValue = 0;
                return registeredHit;
            }
            if (hit >= 0)
                return hit;
            return base.OnSelectOption(options);
        }

        /// <summary>
        /// 「青眼卡通究极龙」的判据（② 回收 / ③ 被攻击时的除外闪避）：② 一律发；③ 只在"这一仗打不赢"
        /// 时才用——卡文/教程："③ 自己的卡通怪兽被攻击的伤害计算时才能发动。那只怪兽直到伤害步骤
        /// 结束时除外" → 攻击落空、还不产生卷回。打得赢的仗让对面那只怪撞掉更好，
        /// 把攻击本身作废等于白放对手一命。判据读不到攻击方/被攻击方时照旧放行（＝原来的行为）。
        /// </summary>
        private bool ToonUltimateActivate()
        {
            if (Duel.Phase != DuelPhase.DamageCal || !Bot.UnderAttack)
                return true;
            ClientCard defender = Bot.BattlingMonster;
            ClientCard attacker = Enemy.BattlingMonster;
            if (defender == null || attacker == null)
                return true;
            int attackerPower = attacker.GetAttackPower();
            int defenderPower = defender.IsDefense() ? defender.Defense : defender.GetAttackPower();
            return attackerPower >= defenderPower;
        }

        /// <summary>现在是不是「暗眼幻想师·无脸幻想师」在问选项（发动时用 GetCurrentChainCard）。</summary>
        private bool IsDarkEyeAsking()
        {
            ClientCard effect = Duel.GetCurrentChainCard();
            if (effect == null)
                effect = Duel.GetCurrentSolvingChainCard();
            return effect != null
                && (effect.IsCode(CardId.DarkEyeIllusionist) || effect.IsOriginalCode(CardId.DarkEyeIllusionist));
        }

        /// <summary>要不要先把「看透心灵之眼」铺上（还没铺过就要）。</summary>
        private bool WantMindEyeFirst()
        {
            return !BotHasOnField(CardId.MindEye) && !Bot.HasInSpellZone(CardId.MindEye);
        }

        /// <summary>
        /// 打谁：**能一击打死就直接打脸**。基类只问"打得过哪只怪"，对面有小怪时会先去撞怪，
        /// 把能一击致胜的那次直击浪费掉（「青眼卡通究极龙」让全场直击就是为这个）。其余交给基类。
        /// </summary>
        public override BattlePhaseAction OnSelectAttackTarget(ClientCard attacker, IList<ClientCard> defenders)
        {
            if (attacker != null && attacker.CanDirectAttack
                && attacker.GetAttackPower() >= Enemy.LifePoints)
            {
                return AI.Attack(attacker, null);
            }
            return base.OnSelectAttackTarget(attacker, defenders);
        }

        /// <summary>
        /// 选卡：候选里**同时有对面和我方的卡**时先选对面的——「漫画猫」②（场上有卡通世界时
        /// 可解放对方的怪）、「邪魔箱」③（洗任意一方墓地的卡）、「看透心灵之眼」②（宣言卡名）
        /// 都要在双方的卡里挑，照基类从尾部取会挑到自己。
        /// 纯我方的候选再按 <see cref="PreferredPicks"/> 挑检索目标；手牌的候选按费用规则丢。
        /// </summary>
        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (Duel.Phase == DuelPhase.BattleStart)
                return null;

            // 额外卡组：从内核给的候选里按顺位挑一只（挑不到就交给下面的通用逻辑）
            if (min <= 1 && max >= 1 && AllInExtraDeck(cards))
            {
                foreach (int cardId in ExtraDeckPriority)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        {
                            IList<ClientCard> summoned = new List<ClientCard>();
                            summoned.Add(card);
                            return summoned;
                        }
                    }
                }
            }

            // ============================================================ 三个"该挑哪张"的接口（各自有卡文依据）
            // ①「青眼卡通究极龙」② 的墓地回收（脚本 c71808988.lua e3：`Duel.Hint(HINT_SELECTMSG,tp,
            //    HINTMSG_ATOHAND)` + 在自己墓地里选 1 张）。教程："究极龙② 回收目录（借场地③ 刷新再回收暗眼）
            //    → 目录检索书签 → 书签检索邪魔箱"。原来的通用优先级按 PreferredPicks 先拿暗眼幻想师，
            //    等于把"目录还能再检索一次"的机会丢掉（RecoverPriority 一直躺在文件里没接线，这里接上）。
            if (min <= 1 && max >= 1 && hint == HintMsg.AddToHand && HasNoEnemyCard(cards)
                && IsSameCard(CurrentEffectCard(), CardId.ToonUltimate))
            {
                foreach (int cardId in RecoverPriority)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        {
                            IList<ClientCard> recovered = new List<ClientCard>();
                            recovered.Add(card);
                            return recovered;
                        }
                    }
                }
            }

            // ②「邪魔箱」③ 的洗回目标（脚本 c8915275.lua e3：`Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_TODECK)`，
            //    范围是自己或对方的墓地 1 张 → 回牌组最下面）。教程："展开时回收自己用完的场地，
            //    也可回收对手墓地的卡当干扰"。原来的通用优先级在"只有我方墓地"时按 PreferredPicks 拿，
            //    第一个就是**暗眼幻想师**（实测 maxboard-94 的 20 局里"暗眼幻想师 from Grave move to Deck"
            //    26 次、"漫画猫 … 16 次"）——这两张正是这条线的"回收→复活"对（究极龙② 回收暗眼 →
            //    暗眼② 从墓地拉漫画猫），洗回牌组等于把复活源扔掉。改成：先回收**用完的场地**
            //    （回牌组 = 暗黑兔③ 还能再贴一张），其次避开那对关键件（名单见 KeepInGrave，含融合素材
            //    青眼卡通龙）；有对面的卡时交给下面的通用逻辑（对面优先＝墓地干扰）。
            //    ⚠ 这一层只解决"有别的候选"的情形：候选里**只剩**关键件时这里挑不动（会掉到下面的通用
            //      检索优先级，把漫画猫挑走），所以"该不该发③"要在发动前判——见 BoxOfFriendsActivate。
            if (min <= 1 && max >= 1 && hint == HintMsg.ToDeck && HasNoEnemyCard(cards))
            {
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 0
                        && (card.IsCode(CardId.PerfectWorld) || card.IsOriginalCode(CardId.PerfectWorld)))
                    {
                        IList<ClientCard> recycled = new List<ClientCard>();
                        recycled.Add(card);
                        return recycled;
                    }
                }
                foreach (ClientCard card in cards)
                {
                    if (card.Controller != 0 || KeepInGrave(card))
                        continue;
                    IList<ClientCard> recycled = new List<ClientCard>();
                    recycled.Add(card);
                    return recycled;
                }
            }

            // ③「完美世界 卡通世界」③ 的除外目标（脚本 c7293697.lua s.rmop：`Duel.Hint(HINT_SELECTMSG,tp,
            //    HINTMSG_REMOVE)` + `Duel.SelectMatchingCard(..., LOCATION_MZONE,0,...)` → 只从**我方怪兽区**
            //    选 1 只，而且"这个回合这个卡名的这个效果不能把原本卡名相同的怪兽除外"）。
            //    这是**刷新**手段（被除外的怪回场后算新的一张卡，它自己"1回合1次（不带卡名）"的效果能再用一次）：
            //    * 「滑稽暗黑兔」③ 不带卡名（c45536531.lua `e4:SetCountLimit(1)`）→ 刷它 = 再贴一张场地
            //      = 场地② 的下一次检索，教程的核心循环就靠这一下（新场地顶掉旧场地）；
            //    * 「邪魔箱」③ 与「青眼卡通究极龙」② 同样不带卡名（c8915275.lua/c71808988.lua 都是
            //      `SetCountLimit(1)`）→ 刷它们 = 洗墓地 / 回收各再来一次（教程："可被场地③ 刷新做 2 次"）；
            //    * **「漫画猫」② 是"卡名一回合一次"（c72921536.lua `e2:SetCountLimit(1,id)`）→ 刷了也发不出来**，
            //      而且把它暂时除外会让它② 结算时的"解放自己"选不到自己（实测首回合的局里它被除外后
            //      解放的是场上的暗黑兔 → 融合素材少一只、漫画猫反而被当素材吃掉）。
            //    所以别的怪兽在场时不要挑它。只对我方怪兽区的候选生效（这次选择只可能是这个效果在问）。
            if (min <= 1 && max >= 1 && hint == HintMsg.Remove && HasNoEnemyCard(cards)
                && AllOnMonsterZone(cards))
            {
                int[] removeOrder = Duel.Player == 0 ? FieldRemovePriority : FieldRemovePriorityDefense;
                foreach (int cardId in removeOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.Controller == 0 && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                        {
                            IList<ClientCard> refreshed = new List<ClientCard>();
                            refreshed.Add(card);
                            return refreshed;
                        }
                    }
                }
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 0 && !IsSameCard(card, CardId.ComicCat))
                    {
                        IList<ClientCard> refreshed = new List<ClientCard>();
                        refreshed.Add(card);
                        return refreshed;
                    }
                }
            }

            // 解放/吃自己场上的怪当费用：挑**最不心疼的**（token → 不是起手件的 → 攻最低），
            // 基类从尾部取会把刚做出来的怪吃掉。
            if (min <= 1 && max >= 1 && hint == HintMsg.Release)
            {
                // 「漫画猫」② 的解放（脚本 c72921536.lua：`Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_RELEASE)`，
                // 且有「卡通世界」时候选里把对方怪兽一起列出来——卡文："自己场上有「卡通世界」存在的场合，
                // 也能从对方场上选解放的怪兽"）。**对面有怪就先解放对面的**：解放是效果（不是代价），
                // 既能解掉对手一只、又把自己的身体（漫画猫/两只暗黑兔）留在场上；对面没有怪时才解放自己
                // ——教程的单卡线正是这种先手局面（把漫画猫送进墓地，等「暗眼幻想师」② 再把它拉回来）。
                if (IsSameCard(CurrentEffectCard(), CardId.ComicCat))
                {
                    _planCatUsed = true;   // 漫画猫② 用掉了（"这个卡名的②的效果1回合只能使用1次"）
                    ClientCard threat = null;
                    foreach (ClientCard card in cards)
                    {
                        if (card.Controller != 1)
                            continue;
                        if (threat == null || card.GetAttackPower() > threat.GetAttackPower())
                            threat = card;
                    }
                    if (threat != null)
                    {
                        IList<ClientCard> remove = new List<ClientCard>();
                        remove.Add(threat);
                        return remove;
                    }
                    if (PlanActive())
                    {
                        foreach (ClientCard card in cards)
                        {
                            if (card.Controller == 0
                                && (card.IsCode(CardId.ComicCat) || card.IsOriginalCode(CardId.ComicCat)))
                            {
                                IList<ClientCard> self = new List<ClientCard>();
                                self.Add(card);
                                return self;
                            }
                        }
                    }
                }
                ClientCard cheapest = null;
                int cheapestScore = int.MaxValue;
                foreach (ClientCard card in cards)
                {
                    if (card.Controller != 0)
                        continue;
                    int score = (card.HasType(CardType.Token) ? -1000 : 0)
                        + (IsPreferredPick(card) ? 1000 : 0)
                        + System.Math.Max(card.Attack, 0) / 100;
                    if (score < cheapestScore)
                    {
                        cheapestScore = score;
                        cheapest = card;
                    }
                }
                if (cheapest != null)
                {
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(cheapest);
                    return picked;
                }
            }

            // 计划层：走线时按"这条线的下一步缺哪一环"挑（用正在结算的卡区分是哪个效果——
            // 同一个候选池在不同效果里正确答案不同：场地② 要暗眼幻想师、目录要青眼卡通龙、邪魔箱要卡通陷阱）。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                int[] planOrder = PlanPickOrder();
                if (planOrder != null)
                {
                    ClientCard effect = CurrentEffectCard();
                    if (IsSameCard(effect, CardId.PerfectWorld))
                        ++_planFieldSearches;    // 场地② 发过几次（依次拿：暗眼幻想师 → 漫画猫 → 卡通目录）
                    if (IsSameCard(effect, CardId.TableOfContents))
                        ++_planContentsPicks;    // 目录① 发过几次（第一次拿青眼卡通龙）
                    if (IsSameCard(effect, CardId.DarkEyeIllusionist))
                        ++_planDarkEyeUses;      // 暗眼幻想师① 用过几次（先铺眼睛、再拿墓地）
                    foreach (int cardId in planOrder)
                    {
                        foreach (ClientCard card in cards)
                        {
                            if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                            {
                                IList<ClientCard> planned = new List<ClientCard>();
                                planned.Add(card);
                                return planned;
                            }
                        }
                    }
                }
            }

            if (_verbose && PlanActive() && min >= 2)
            {
                Logger.WriteLine("[计划层] 多选请求：min=" + min + " max=" + max + " hint=" + hint
                    + " 候选=" + DescribeCardsForDebug(cards));
            }

            // 接触融合「青眼卡通究极龙」的素材（min>=2）：青眼卡通龙 + 2 只暗黑兔——**漫画猫和邪魔箱
            // 都留着站场**（它们是终场件）。基类从尾部取会把漫画猫/邪魔箱塞进素材里。
            // ⚠ 口径：内核给这次选择的 hint **不一定**是 HintMsg.FusionMaterial（实测走的是普通选择，
            // 所以按 hint 接的钩子从来没命中过）——改成按"候选里有青眼卡通龙 + 要挑 2 张以上"来认，
            // 走线时超融合已经被闸门挡住，所以这个特征不会误伤别的多选。
            // 接触融合「青眼卡通究极龙」的素材：内核走的是 **MSG_SELECT_UNSELECT**——一次只问一张
            //（min=0/1、max=1），反复问到够为止。所以按"这次已经挑了几张"给：暗黑兔 ×2 → 青眼卡通龙
            // → 收手（min==0 时返回空列表＝结束选择）。漫画猫/邪魔箱因此留在场上（它们是终场件）。
            if (hint == HintMsg.FusionMaterial && PlanActive())
            {
                if (_verbose)
                    Logger.WriteLine("[计划层] 融合素材请求：min=" + min + " max=" + max
                        + " 已挑=" + _planFusionPicks + " 候选=" + DescribeCardsForDebug(cards));
                if (_planFusionPicks < 2)
                {
                    ClientCard rabbit = FindFusionMaterial(cards, CardId.DarkRabbit);
                    if (rabbit != null)
                    {
                        ++_planFusionPicks;
                        return new List<ClientCard> { rabbit };
                    }
                }
                if (_planFusionPicks < 3)
                {
                    ClientCard dragon = FindFusionMaterial(cards, CardId.BlueEyesToon);
                    if (dragon != null)
                    {
                        _planFusionPicks = 3;
                        return new List<ClientCard> { dragon };
                    }
                }
                // 第 4 次询问＝还差一只"卡通怪兽"（卡文："「青眼卡通龙」＋卡通怪兽×2"）：
                // 这时要挑「邪魔箱」——教程第三行的融合素材就是"暗黑兔＋邪魔箱＋青眼卡通龙"。
                // **必须在这里拦下来**：让它掉到下面的通用检索优先级时，`PreferredPicks` 里排第二的
                // 「漫画猫」会顶掉邪魔箱（实测 perf-blank-94：候选=邪魔箱、漫画猫 → 结果
                // "漫画猫 from MonsterZone move to Deck"，把终场件（四件套之一）当素材喂掉了）。
                if (_planFusionPicks >= 3 && min >= 1)
                {
                    ClientCard box = FindFusionMaterial(cards, CardId.BoxOfFriends);
                    if (box != null)
                        return new List<ClientCard> { box };
                }
                if (min == 0)
                {
                    _planFusionPicks = 0;   // 这一次融合挑完了，下次重新数
                    return new List<ClientCard>();
                }
            }

            // 「破坏魔法·陷阱（0～N 张）」这类**可选**破坏：候选里只有我方的卡时一张都不选
            //（基类从尾部挑＝自己炸自己，实测"台风把自己的魔陷飞掉"）。
            if (min == 0 && hint == HintMsg.Destroy && HasNoEnemyCard(cards))
                return new List<ClientCard>();

            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                // 手牌：多半是"丢手牌当费用"，按费用规则挑
                if (AnyInHand(cards) && PickForCost(cards))
                {
                    IList<ClientCard> pick = _handPick;
                    _handPick = null;
                    return pick;
                }
                // 「从卡组特召」（漫画猫②等）：hint 是 SpSummon/Summon——按**身体**优先级挑，
            // 不要走下面的检索优先级（那会把一次白给的身体浪费在检索件上）
            if (min <= 1 && max >= 1 && (hint == HintMsg.SpSummon || hint == HintMsg.Summon))
            {
                foreach (int cardId in SummonPriority)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        {
                            IList<ClientCard> summoned = new List<ClientCard>();
                            summoned.Add(card);
                            return summoned;
                        }
                    }
                }
            }

            // 非手牌：检索/拿卡，按教程的优先级挑——但**已经有的卡不再拿**
                //（用户实测过"完美世界检索完美世界"：重复的场地／续作等于把这次检索白送掉）。
                // 先把"手上/场上还没有的"按优先级过一遍，全都已有才退回按优先级拿。
                for (int pass = 0; pass < 2; ++pass)
                {
                    foreach (int cardId in PreferredPicks)
                    {
                        bool owned = Bot.HasInHand(cardId)
                            || Bot.HasInSpellZone(cardId)
                            || Bot.HasInMonstersZone(cardId);
                        if (pass == 0 && owned)
                            continue;
                        foreach (ClientCard card in cards)
                        {
                            if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                            {
                                IList<ClientCard> picked = new List<ClientCard>();
                                picked.Add(card);
                                return picked;
                            }
                        }
                    }
                }
            }

            List<ClientCard> theirs = new List<ClientCard>();
            List<ClientCard> mine = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                // Controller：0＝自己、1＝对手（WindBot 内部的双方编号）
                if (card.Controller == 1)
                    theirs.Add(card);
                else if (card.Controller == 0)
                    mine.Add(card);
            }
            if (theirs.Count == 0 || mine.Count == 0)
                return base.OnSelectCard(cards, min, max, hint, cancelable);

            List<ClientCard> selected = new List<ClientCard>();
            foreach (ClientCard card in theirs)
            {
                if (selected.Count >= max)
                    break;
                selected.Add(card);
            }
            for (int i = 1; selected.Count < min && i <= mine.Count; ++i)
                selected.Add(mine[mine.Count - i]);
            return selected;
        }

        /// <summary>候选里有没有对面的卡。</summary>
        private static bool HasNoEnemyCard(IList<ClientCard> cards)
        {
            foreach (ClientCard card in cards)
            {
                if (card.Controller == 1)
                    return false;
            }
            return true;
        }

        /// <summary>候选里有没有手牌。</summary>
        private static bool AnyInHand(IList<ClientCard> cards)
        {
            foreach (ClientCard card in cards)
            {
                if (card.Location == CardLocation.Hand)
                    return true;
            }
            return false;
        }

        /// <summary>候选是不是全在额外卡组里（＝正在挑要出场的额外怪）。</summary>
        private static bool AllInExtraDeck(IList<ClientCard> cards)
        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card.Location != CardLocation.Extra)
                    return false;
            }
            return true;
        }

        /// <summary>候选是不是全在我方怪兽区（＝「完美世界 卡通世界」③ 在问"把哪只除外"）。</summary>
        private static bool AllOnMonsterZone(IList<ClientCard> cards)
        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card.Location != CardLocation.MonsterZone)
                    return false;
            }
            return true;
        }

        /// <summary>
        /// 「完美世界 卡通世界」③ 该想刷新的怪（按价值排）：暗黑兔③（再贴一张场地→场地② 的下一次检索）
        /// → 邪魔箱③（墓地干扰/回收再来一次）→ 青眼卡通究极龙②（再回收一张）。
        /// 漫画猫不在这里：它的② 是"卡名一回合一次"，刷了也发不出来（见 OnSelectCard 里的长注释）。
        /// </summary>
        private static readonly int[] FieldRemovePriority =
        {
            CardId.DarkRabbit,
            CardId.BoxOfFriends,
            CardId.ToonUltimate,
        };

        /// <summary>
        /// 对手回合的刷新顺序：暗黑兔③ 在脚本里是 `EFFECT_TYPE_IGNITION` + "自己主要阶段"
        /// （c45536531.lua e4）——**对手回合刷了也发不出来**，这时先刷邪魔箱③（脚本写的是
        /// "自己·对方回合 1 次"）与究极龙②。
        /// </summary>
        private static readonly int[] FieldRemovePriorityDefense =
        {
            CardId.BoxOfFriends,
            CardId.ToonUltimate,
            CardId.DarkRabbit,
        };

        // ============================================================ 选项式效果（二选一）的编码

        /// <summary>
        /// 分支型效果「选项」的**实际显示值**：卡脚本拿 `aux.Stringid(id, n)` 当选项字符串，
        /// 内核把它原样发给客户端，而 `aux.Stringid(id, n) == id * 16 + n`
        /// （本副牌的探针实测反推过三次：「滑稽暗黑兔」脚本写 `aux.Stringid(id,3)`
        /// → 日志里 `options=[1190,728584499]`，`45536531*16+3 = 728584499`；
        /// 「暗眼幻想师」脚本写 `aux.Stringid(id,1/2)` → 日志里 `options=[549039825,549039826]`；
        /// 「落胤与圣女」脚本写 `aux.Stringid(id,1)` → 日志里 `options=[484337553]`）。
        ///
        /// ⚠ 这里原来是 `cardId * 16 + scriptValue + 1`——那个"+1"是从魔女术「慶典」反推的**巧合**：
        /// 那张卡的 `aux.SelectFromOptions` 表里第 3 列（脚本自己用的分支值）与第 2 列（Stringid 序号）
        /// 刚好差 1（`{b1,aux.Stringid(id,2),1}`），换一张卡就不成立。
        /// 「落胤与圣女」的 Stringid 序号与分支号相同，于是登记值**永远比真选项大 1**、一次都匹配不上，
        /// 分支选择权整个落回基类 `DoEverythingExecutor.OnSelectOption` 的 `Rand.Next`
        /// ——"自己炸自己"就是这么来的（实测见 <see cref="FallenAndVirtuousActivate"/>）。
        /// </summary>
        private static int EncodedOption(int cardId, int stringIndex)
        {
            return cardId * 16 + stringIndex;
        }

        /// <summary>登记好、等着内核来问的选项值（见 <see cref="EncodedOption"/>）。</summary>
        private int _optionValue;

        /// <summary>
        /// 系统提示号（`ygopro/strings.conf`）：1190＝加入手卡、1153＝盖放。
        /// ⚠ 这两个**不带卡号**（是全卡通用的提示字符串，不属于"卡号*16+k"那套编码），
        /// 所以不能进 <see cref="_optionValue"/>（那样会和别家的选项撞上），只能配合
        /// <see cref="PreferredOptionValues"/> 按"正在结算的卡"限定后按值匹配。
        /// </summary>
        private const int OptionAddToHand = 1190;
        private const int OptionSet = 1153;

        /// <summary>
        /// 把"卡号 + 卡脚本里 `aux.Stringid(卡号, k)` 的 k"编成内核真正发给客户端的选项值
        /// （＝ `Util.GetStringId`：`卡号 * 16 + k`，与 <see cref="EncodedOption"/> 同一套口径；
        /// `Util.GetStringId` 的用法见 `RaiseMoonExecutor`：`desc == Util.GetStringId(卡号, k)`）。
        /// 值里带着卡号，所以不会和别的卡的选项撞上；数组顺序＝优先级（见 <see cref="PreferredOptionValues"/>）。
        /// </summary>
        private int[] OptionValues(int cardId, params int[] stringIndexes)
        {
            int[] values = new int[stringIndexes.Length];
            for (int i = 0; i < stringIndexes.Length; ++i)
                values[i] = Util.GetStringId(cardId, stringIndexes[i]);
            return values;
        }

        /// <summary>
        /// 分支卡的"想要哪一支"——按**正在结算（认不出来时＝正在发动）的那张卡**认，
        /// 逐张对过 `ygopro/script` 下的卡脚本（2026-10-05）。数组顺序＝优先级：先试第一个，命中就用
        /// （某一支因为条件不成立被脚本压缩掉时，会自动落到下一支——见 <see cref="OnSelectOption"/>）。
        /// 每张卡的 k 与其对应的分支：
        /// * 「暗眼幻想师·无脸幻想师」`c34314989.lua`（`aux.SelectFromOptions`
        ///   `{b1,Stringid(id,1),1}/{b2,Stringid(id,2),2}`）→ k1＝把「看透心灵之眼」表侧放置、k2＝从墓地拿 1 只再特召；
        /// * 「滑稽暗黑兔」`c45536531.lua:70`（`Duel.SelectOption(tp,1190,aux.Stringid(id,3))`）
        ///   → k3＝在自己场上表侧放置；
        /// * 「邪魔箱」`c8915275.lua:64` / 「烙印龙 阿尔比昂」`c87746184.lua:88`
        ///   （`Duel.SelectOption(tp,1190,1153)`）→ 1190＝加入手卡、1153＝盖放；
        /// * 「三战之才」`c25311006.lua:30-45`（手写 ops 表）→ k0＝抽2、k1＝夺控制权、k2＝看手牌回卡组；
        /// * 「三战之号」`c35269904.lua:37`（`Duel.SelectOption(tp,1153,1190)`）→ 加入手卡 / 盖放；
        /// * 「No.60 刻不知之杜加雷斯」`c66011101.lua:32-35`（`aux.SelectFromOptions` 三支）
        ///   → k0＝抽2丢1（挂 EFFECT_SKIP_DP）、k1＝自己墓地 1 只守备特召（挂 EFFECT_SKIP_M1）、
        ///   k2＝自己场上 1 只攻击力变 2 倍（挂 EFFECT_SKIP_BP）。
        /// 认不出来（别的卡/取名失败）返回 null，交回基类——不猜。
        /// </summary>
        private int[] PreferredOptionValues(ClientCard effect)
        {
            // 「暗眼幻想师·无脸幻想师」① 的二选一：沿用原来的意图——走线时**先铺「看透心灵之眼」**
            // （防手坑的核心循环第一步），铺过（场上已有）才去"从墓地拿 1 只"（等漫画猫进墓地再拉回来）。
            // 认卡用两个来源各查一次：结算中（CurrentEffectCard）+ 发动时的连锁卡（IsDarkEyeAsking）。
            if (IsSameCard(effect, CardId.DarkEyeIllusionist) || IsDarkEyeAsking())
                return WantMindEyeFirst() ? OptionValues(CardId.DarkEyeIllusionist, 1, 2)
                                          : OptionValues(CardId.DarkEyeIllusionist, 2, 1);
            // 「滑稽暗黑兔」③：要**在自己场上表侧表示放置**（k3），不是加入手卡（1190）——
            // 教程的核心循环靠"贴新场地顶掉旧场地"刷新场地② 的"一回合最多 3 次"，拿在手里就断链。
            if (IsSameCard(effect, CardId.DarkRabbit))
                return OptionValues(CardId.DarkRabbit, 3);
            // 「邪魔箱」①：要**盖放**（1153）。按执行器现在的口径（见 FieldSearchStepsFieldLine
            // "第二张邪魔箱（① 自跳 + 盖「卡通恐怖」）"）：检索目标「卡通恐怖」是**反击陷阱**，
            // 从卡组盖放 → 下个自己回合就能开；选加入手卡的话还要再花一次盖放动作、当回合盖的卡
            // 当回合又不能发动 → 整整慢一个回合，而这条线正指望它当三色康（BoxTrapPick）。
            if (IsSameCard(effect, CardId.BoxOfFriends))
                return new int[] { OptionSet, OptionAddToHand };
            // 「烙印龙 阿尔比昂」③：要**加入手卡**（1190）。这副牌里 0x15d（烙印）魔陷只有
            // 「落胤与圣女」一张（cards.cdb：type=0x10002＝速攻魔法、setcode 0x15d），
            // 拿在手里当回合就能响应/解场（本文件把它当"能发就发"的解场·复活支，见
            // FallenAndVirtuousActivate）；盖放则当回合发不出来。
            if (IsSameCard(effect, CardId.AlbionTheBrandedDragon))
                return new int[] { OptionAddToHand, OptionSet };
            // 「三战之才」①：先"抽 2"（k0）——本副牌的瓶颈是"手上有起手件"（暗黑兔/漫画猫/场地），
            // 抽 2 也是三支里唯一与对面场面无关、列表里一定列得出来的；其次"夺控制权"（k1，解场＋
            // 多个身位）；最后才是纯干扰的"看手牌回卡组"（k2）。
            if (IsSameCard(effect, CardId.TripleTactics))
                return OptionValues(CardId.TripleTactics, 0, 1, 2);
            // 「三战之号」①：要**加入手卡**（1190）。检索目标（卡通目录/卡通书签/星球改造/超融合
            // 这些通常·速攻魔法）拿在手里当回合就能发动，盖放要等下个自己回合（当回合盖的卡不能发动）。
            // ⚠ 脚本里的顺序是 `Duel.SelectOption(tp,1153,1190)`（盖放在前），按下标取会拿错支。
            if (IsSameCard(effect, CardId.TripleTacticsThrust))
                return new int[] { OptionAddToHand, OptionSet };
            // 「No.60 刻不知之杜加雷斯」①：选"抽 2 丢 1"（k0）。三支都带"跳过自己回合某个阶段"的代价
            // （脚本 c66011101.lua：op1 挂 EFFECT_SKIP_DP、op2 挂 EFFECT_SKIP_M1、op3 挂 EFFECT_SKIP_BP，
            // reset 都写到自己回合 2 次）——本副牌的赢法是"主阶段1 展开 + 战阶直击"，被跳掉主要阶段1
            // 或战斗阶段比"少一次抽牌"更伤；而 k0 是唯一能**立刻**换到展开件（抽 2 丢 1）的支。
            if (IsSameCard(effect, CardId.Number60Dugares))
                return OptionValues(CardId.Number60Dugares, 0, 1, 2);
            return null;
        }

        // ============================================================ 宣言卡名（看透心灵之眼②）

        /// <summary>
        /// 「看透心灵之眼」② 要宣言 1 个卡名（脚本 c34298391.lua：`Duel.AnnounceCard(tp)`；
        /// 卡文："宣言 1 个同一连锁上没有把效果发动的卡名……直到回合结束原本卡名和宣言的卡相同的卡
        /// 发动的效果无效化"）。
        ///
        /// 不写这段会怎样：基类 `Executor.OnAnnounceCard` 返回 0，`GameAI` 再退回 `avail[0]`
        /// ——那是卡号最小的一张无关卡，等于这次 ② 白开（实测 maxboard-94 的 20 局里 ② 发动了 86 次）。
        ///
        /// 教程口径：**"康不了能二速发动的速攻魔法（C1 开②，对手能 C2 发动那张速攻）→ 那种交给卡通恐怖"**
        /// ——所以宣言时跳过速攻魔法，挑对手**下一手最可能用出来的**卡名：「看透心灵之眼」① 让对手手卡
        /// 持续公开，所以先看手牌（怪 → 场地/永续 → 陷阱 → 其它），再看对面场上的表侧卡，最后看墓地。
        /// 挑不到就返回 0（交回 GameAI 的默认），不会比现在更差。
        ///
        /// ⚠ 探针：原来这段**一行日志都不打**，所以"这轮到底宣言了谁"在日志里查不到（第二轮的
        /// toon-opt / 真人局都只能看到 `看透心灵之眼 activate effect from SpellZone`，看不到卡名）。
        /// 用户这轮的问题正是"宣言的是不是对手真会用的卡名"，所以补一行 Debug 输出：宣言值 + 位置 +
        /// 那局对手手牌/场上能看到什么（下一轮用 `[探针] 宣言卡名` 复查）。
        /// </summary>
        public override int OnAnnounceCard(IList<int> avail)
        {
            if (avail == null || avail.Count == 0)
                return 0;
            ClientCard pick = PickAnnounceTarget();
            if (pick == null || pick.Id == 0)
                return 0;
            foreach (int id in avail)
            {
                if (pick.IsCode(id) || pick.IsOriginalCode(id))
                {
                    if (_verbose)
                        Logger.WriteLine("[探针] 宣言卡名：「" + pick.Name + "」(" + id + "，位置="
                            + pick.Location + "，对手可见手牌 " + Enemy.Hand.Count + " 张，候选 " + avail.Count + " 张）");
                    return id;   // 内核只认清单里的卡号（同一张卡的不同印刷号不能直接回）
                }
            }
            if (_verbose)
                Logger.WriteLine("[探针] 宣言卡名：「" + pick.Name + "」不在候选清单里（候选 " + avail.Count
                    + " 张）→ 交回内核的 avail[0]（等于白开）");
            return 0;
        }

        /// <summary>宣言谁：对手手牌 → 对手场上的表侧卡 → 对手墓地（见 OnAnnounceCard 的口径）。</summary>
        private ClientCard PickAnnounceTarget()
        {
            ClientCard best = null;
            int bestScore = 0;
            foreach (ClientCard card in Enemy.Hand)
                ConsiderAnnounce(card, 100, ref best, ref bestScore);
            foreach (ClientCard card in Enemy.MonsterZone)
            {
                if (card != null && card.IsFaceup())
                    ConsiderAnnounce(card, 50, ref best, ref bestScore);
            }
            foreach (ClientCard card in Enemy.SpellZone)
            {
                if (card != null && card.IsFaceup())
                    ConsiderAnnounce(card, 50, ref best, ref bestScore);
            }
            foreach (ClientCard card in Enemy.Graveyard)
                ConsiderAnnounce(card, 10, ref best, ref bestScore);
            return best;
        }

        /// <summary>
        /// 宣言候选的分数：同位置上"怪 &gt; 场地/永续 &gt; 陷阱 &gt; 其它"，**速攻魔法不宣言**
        /// （教程：对手能连锁发动它来躲）。
        /// </summary>
        private static void ConsiderAnnounce(ClientCard card, int locationBonus, ref ClientCard best, ref int bestScore)
        {
            if (card == null || card.Id == 0)
                return;
            int score;
            if (card.HasType(CardType.QuickPlay))
                score = 0;                                    // 速攻魔法：康不了，跳过
            else if (card.HasType(CardType.Monster))
                score = 4;
            else if (card.HasType(CardType.Field) || card.HasType(CardType.Continuous))
                score = 3;
            else if (card.HasType(CardType.Trap))
                score = 2;
            else
                score = 1;
            if (score == 0)
                return;
            score += locationBonus;
            if (score > bestScore)
            {
                bestScore = score;
                best = card;
            }
        }
    }
}
