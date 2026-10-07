using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「杀手级调整曲」——按操作方给的 202607 复刻教程写的决策层（配合群友投稿的 #95 卡表）。
    ///
    /// **这一层只排战术优先级**：合法性内核判、条件与自肃卡脚本声明（和另外几份执行器同一套分工）。
    ///
    /// 引擎（按卡文核对过）：
    /// * 本家下级都带一句 **"场上的这张卡为素材作同调召唤的场合，手卡 1 只调整也能作为同调素材"** ——
    ///   也就是**手牌里的调整能直接当同调素材**，所以先手是"通召一只 → 拉/检索 → 手卡同调"。
    /// * 下级**作为同调素材送去墓地时会触发破坏**：混音手② 破坏对方 1 只怪兽、唱片师② 破坏对方 1 张魔陷、
    ///   音轨制作人②（同调怪）把对方 1 张卡弹回手卡、旋钮手② 洗对方墓地/看手牌。
    /// * **点唱机酒吧「杀手级调整曲」(14442329)**：① 每回合多一次调整召唤；② 对方场/墓地有调整时
    ///   「响度战争」攻击力 +3300；③ 解放自己场上 1 只调整 → 从卡组拿/特召一只本家
    ///   （**发动后到回合结束不是调整不能特召**＝自肃，交给内核）。
    /// * **「响度战争」(41069676)**：① 保护其他调整不被效果破坏/取对象；② 对方发效果时可以除外墓地
    ///   一只本家**适用它的送墓效果**＝用墓地的破坏效果当阻抗。
    /// * **「B2B」(65961304)**：③ 对方发怪兽效果时回收墓地/除外的调整并追加一次同调（最多 2 次＝耐久的干扰）。
    /// * **「削波手」(43904702)**：① 对方主要阶段从手牌自跳并立刻同调一次＝对手回合的打断。
    ///
    /// 护栏沿用另外几副：手坑禁止通召、候选含双方时先选对面的（送墓效果都要选对方场上的卡）；
    /// **能斩杀就直接进战阶**（`LethalAvailable`/`ReposForLethal`/`OnSelectPosition`，判据保守：
    /// 对面空场 + 我方表侧攻击表示、且不是本回合才上场的怪攻击力合计 ≥ 对面 LP）。
    /// </summary>
    [Deck("KillerTune", "AI_KillerTune")]
    class KillerTuneExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            public const int JukeboxBar = 14442329;      // 点唱机酒吧「杀手级调整曲」（场地）
            public const int Announcer = 16387555;       // 提示员
            public const int Mixer = 16509007;           // 混音手
            public const int Knob = 17209452;            // 旋钮手
            public const int Clipper = 43904702;         // 削波手
            public const int Recordist = 89392810;       // 唱片师
            public const int TrackMaker = 42781164;      // 音轨制作人（同调）
            public const int LoudnessWar = 41069676;     // 响度战争（同调）
            public const int BackToBack = 65961304;      // B2B（同调）
            public const int Remixer = 88170262;         // 再混音手（同调）
            public const int RedStamp = 15665977;        // 红印鉴唱片师（同调）
            public const int CrackleClipper = 39576656;  // 噼啪削波手（同调）
            public const int TuneUp = 78058681;          // 杀手级调整曲同调
            public const int TripleTactics = 25311006;   // 三战之才
            public const int TripleTacticsThrust = 35269904; // 三战之号（基准号）
            // ⚠ 卡表用的是**另一印刷号 35269905**（alias＝35269904）；**两个印刷号都认**：
            //   常量、下面的 AddExecutor 与 PreferredOptionValues() 里的判据都登记两个号。
            //   两个印刷的脚本（c35269904.lua / c35269905.lua）内容一致，选项值同样是系统串
            //   1153/1190，所以只需把"匹配"补全。
            public const int TripleTacticsThrustAlt = 35269905; // 三战之号（卡表里用的是这个印刷号）
            public const int PotOfProsperity = 84211599; // 金满而谦虚之壶
            public const int LockDragonLock = 4891376;   // 锁缚龙 锁镰（三色康终端）
            public const int AbyssDragon = 67886896;     // 苍之深渊 渊眼白龙（旋钮手限 1 后的补救检索点）
            public const int PsyFrameDriver = 49036338;  // PSY骨架驱动者（γ/δ 的弹药：只该待在卡组·手卡·墓地）
            // PSY骨架装备·γ（2026-10-06 登记）：它**本来就一直在用**——γ/δ 靠通用路径自跳
            //（条件是"自己场上没怪 + 对手怪兽效果发动时"，见 IsSameCard(Card, PsyFrameDriver) 那套），
            // 体检报它只是因为源码里没出现 38814750 这个号。这里点名只为把审计清掉，不改行为。
            public const int PsyFrameGearGamma = 38814750;
            // 「欢聚友伴」两张：唯一条件是"自己场上没有卡"（见 MulcharmyActivate）——它们的时机与
            // 其它手坑不同，必须**抢在自己铺场之前**，所以单独登记卡号、单独一条前置规则。
            public const int MulcharmyFuwalos = 42141493; // 欢聚友伴·茸茸长尾山雀（对方从卡组·额外特召时抽）
            public const int MulcharmyPurulia = 84192580; // 欢聚友伴·抖抖海月水母（对方从手卡召唤·特召时抽）

            // ===== 卡表里体检一直报"没登记"的两张魔法（2026-10-06 补）=====
            // 都是"纯赚一张"的搜索件，所以登记成"能发就发"（原来掉进通用兜底，会被"能从额外卡组出怪
            // 就让路"那条挡掉，整局一次都用不出来）：
            // * 同调超车 99243014：展示额外 1 只同调怪 → 从卡组·墓地拿它卡名记述的 1 只同调素材
            //  （加手或特召）。⚠ 它带"这回合只能出同调怪"的自肃，所以**该发在链接/超量段之前**
            //  —— 这一步的"展示哪只、拿哪只"还由通用逻辑挑，等有日志证据再写专属选取（别按猜的写）。
            // * 决斗者创世纪 97474300：场/墓有调整时 → 从卡组拿 1 张「同调」魔陷（这副牌就是
            //  「杀手级调整曲同调」/「同调超车」），同样"能发就发"。
            public const int SynchroOvertake = 99243014;   // 同调超车
            public const int DuelistGenesis = 97474300;    // 决斗者创世纪
        }

        public KillerTuneExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ① 先摘掉基类那两条笼统规则，换成带护栏的版本（见 ⑥）。必须先摘再加自己的。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].Type == ExecutorType.SummonOrSet || Executors[i].Type == ExecutorType.Activate)
                    Executors.RemoveAt(i);
            }

            // ② 引擎件"能发就发"：场地（额外召唤/检索/特召）、
            //    下级四只（提示员拉调整、混音手/唱片师检索、旋钮手追加召唤）、削波手（对方回合自跳）
            AddExecutor(ExecutorType.Activate, CardId.JukeboxBar, JukeboxActivate);
            // 两张"纯赚"的搜索件：能发就发（见常量表里的说明）
            AddExecutor(ExecutorType.Activate, CardId.SynchroOvertake, () => true);
            AddExecutor(ExecutorType.Activate, CardId.DuelistGenesis, () => true);
            AddExecutor(ExecutorType.Activate, CardId.Announcer, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Mixer, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Recordist, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Knob, KnobActivate);
            // 「削波手」：① 只有"对方主要阶段"一个窗口（脚本 `c43904702.lua`：
            // `spcon = Duel.IsMainPhase() and Duel.GetTurnPlayer()==1-tp`）→ 自跳 + 之后 1 次加速同调；
            // ② 是"作为同调素材送去墓地的场合，把对方额外卡组的里侧卡**随机** 1 张除外"——
            // 脚本里是 `g:RandomSelect(tp,1)`，**内核自己随机挑，没有可选的地方**，
            // 所以施工单里"削波手的 ②"在 WindBot 侧只需要保证它**作为素材被用掉**（①→同调那一步）。
            AddExecutor(ExecutorType.Activate, CardId.Clipper, AlwaysPlay);

            // ③ 同调怪：音轨制作人的检索、响度战争的墓地效果回收、B2B 的回收+追加同调、
            //    再混音手的对手回合展开、红印鉴/噼啪削波手的无效与补刀
            AddExecutor(ExecutorType.Activate, CardId.TrackMaker, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.LoudnessWar, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.BackToBack, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Remixer, RemixerActivate);
            AddExecutor(ExecutorType.Activate, CardId.RedStamp, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.CrackleClipper, AlwaysPlay);

            // ④ 魔法与手坑类：同调、三战、壶
            // 「杀手级调整曲同调」：走线时**盖放**收尾（教程每条单卡线的最后一步，留到对手回合），
            // 不在主要阶段发动把它花掉；没有计划时维持原来的"能发就发"。
            AddExecutor(ExecutorType.Activate, CardId.TuneUp, TuneUpActivate);
            // 盖放出口：基类的通用盖放规则只给"没人管"的卡，这里点名管这张（见 TuneUpSpellSet）。
            Executors.Add(new CardExecutor(ExecutorType.SpellSet, CardId.TuneUp, TuneUpSpellSet));
            // 「苍之深渊 渊眼白龙」：2026 七月表「旋钮手」限一之后，这副牌用它从手牌发动效果
            // 把「旋钮手」（或「效果遮蒙者」）加入手牌——教程"零、补救"那一节的起点，能发就发。
            AddExecutor(ExecutorType.Activate, CardId.AbyssDragon, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleTactics, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleTacticsThrust, AlwaysPlay);
            // ⚠ 卡表里的「三战之号」是另一印刷号 35269905：**两个印刷号都认**，缺了这条专属规则会落空。
            AddExecutor(ExecutorType.Activate, CardId.TripleTacticsThrustAlt, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.PotOfProsperity, PotOfProsperityActivate);

            // ⑤ 通用兜底：任何怪都能通召/盖放、任何效果都能发动——但**否决手坑**（见 HandTraps）；
            //    额外卡组的同调召唤交给基类的 SpSummon 规则（"能做就做"）。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));

            // ⑥ 计划层的"额外召唤白名单"（都是"这一回合按线路走"时才生效）：
            //    * 「音轨制作人」：线路上只要**第一只**（它 ① 拿去拿场地）；再多出一只就会把场地③ 拉出来的
            //      「唱片师」当素材吃掉（实测：唱片师 3 + 手卡手坑 1 = 4 星，终场少一个 3 星站场）；
            //    * 「再混音手」：计划还捏着手卡「混音手」时先不急着同调——手卡素材会把场上的「唱片师」
            //      一起吃掉，教程要的是"混音手在场上 + 手卡 3 星"那一次；
            //    * 其余额外怪：计划进行中一律先放一放（响度战争会吃「再混音手」）。
            //    这些规则只对**点名的卡**生效：基类那条笼统的 SpSummon（DefaultNoExecutor）会自动
            //    让位给点名规则，正好用来做"这一只先别出"；没有计划时全部放行，维持"能做就做"。
            // ⑨ **手坑预算**（2026-10-07 群友实测口径："两只兔子做场拿一只当素材用是没有问题的，
            //    但全用掉就有点难崩了"）：额外卡组这十只的召唤闸门都先过一遍
            //    `AllowExtraSummonKeepingHandTrap()`——只剩最后一张手坑、而这次召唤只能靠它凑素材时
            //    否掉这次召唤，把手坑留着打断对面（见那个方法的注释里有实测日志）。
            //    ⚠ 必须**包在既有的点名闸门里**：基类那条笼统的 SpSummon 只对"没有点名规则的卡"生效，
            //      而点名规则之间是"先注册的先说话"——另起一条预算规则会在既有闸门说 true 时被跳过。
            AddExecutor(ExecutorType.SpSummon, CardId.TrackMaker,
                () => AllowExtraSummonKeepingHandTrap() && AllowTrackMakerSummon());
            AddExecutor(ExecutorType.SpSummon, CardId.Remixer,
                () => AllowExtraSummonKeepingHandTrap() && AllowRemixerSummon());
            // 「锁缚龙」：两卡线的指定终端，但**素材到位之前先别出**（见 AllowLockDragonSummon）。
            // 它不在 OffPlanExtraMonsters 里（它不是"浪费素材"，是线的收尾），所以单独一条闸门。
            AddExecutor(ExecutorType.SpSummon, CardId.LockDragonLock,
                () => AllowExtraSummonKeepingHandTrap() && AllowLockDragonSummon());
            // ⚠ 这张表**原来只声明了、没有注册**（2026-10-05 发现）：没有专属规则时，基类那条笼统的
            // SpSummon（谓词 `DefaultNoExecutor`）会对这些卡生效，而且它在 Executors 里排在前面
            // （基类构造函数先跑）——所以"计划进行中先放一放"从来没生效过。实测后果：走「提示员线」的
            // 第 1 回合，它用"场上的提示员 3★ + 手牌幽鬼兔 3★"出了 6★「响度战争」，把该出的
            // 4★「音轨制作人」（① 检索场地，教程每条单卡线都从它开始）挤掉，整条线在第 2 步就断。
            // 这些规则只在**自己的回合+计划进行中**生效（`PlanActive()` 里判了 `Duel.Player == 0`），
            // 所以对手回合照样能做这些阻抗件。
            foreach (int cardId in OffPlanExtraMonsters)
                AddExecutor(ExecutorType.SpSummon, cardId,
                    () => AllowExtraSummonKeepingHandTrap() && AllowOtherExtraSummon());
            // ⚠ 按验收口径（**终端件数 + 一回合能对对面展开的阻抗次数**）修正：
            // 原来这里把"其余额外怪"整轮锁死，等于把**阻抗资源也锁掉了**——响度战争/红印鉴/B2B/鲜花
            // 本身就是场上的阻抗件（各带 1 次②的打断），而对面一动手它又出不来，纯亏。
            // 现在只拦"纯浪费"的三处：第二只音轨制作人、场上已有 3 星时用手卡混音手做的再混音手、以及壶。
            // 其余一律放行（基类"能做就做"）。

            // ⑦ 斩杀优先：插到 `Executors` 最前面（内核的动作循环是"外层遍历规则、内层遍历候选"，
            //    战斗阶段要等所有规则都不出手才轮得到；排在后面的话"还有事可做"会一直把战阶推后）。
            //    判据（**保守**，见 LethalAvailable）：对面空场 + 我方表侧攻击表示、且**不是本回合才上场**
            //    的怪攻击力合计 ≥ 对面 LP → 立刻进战斗阶段；配套 ③ 把"蹲着的守备怪"当场转攻击
            //    （ReposForLethal）。两卡线的终端「响度战争」攻 0/守 3000 蹲守备，这一条就是它参战的口子。
            Executors.Insert(0, new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));

            // ⑧ 「欢聚友伴」两张的**空场窗口**（见 MulcharmyActivate）——必须排在**所有规则之前**，
            //    所以要放在构造函数最后用 Insert(0) 加，而不是跟别的手坑一起走通用段。
            //    为什么顺序是硬要求：`GameAI.OnSelectChain`（Game/AI/GameAI.cs:499-513）是
            //    "外层遍历规则、内层遍历候选"，**按规则表顺序取第一条愿意发动的**；同一个连锁窗口里
            //    「削波手①」（② 里注册的 `AlwaysPlay`）与「欢聚友伴」都能发时，谁排在前面谁拿这个窗口。
            //    而 削波手① 一自跳就把场铺开（10:36:33 的 `from Hand move to MonsterZone`），
            //    等于把"自己场上没有卡"这个条件永久破坏——本局就是这么把整张手坑扔掉的。
            //    ⚠ 斩杀那两条是别的 ExecutorType（Repos / GoToBattlePhase），不会和这条 Activate 规则抢；
            //      而且本条的判据里写了"只在对手回合"，自己回合恒为 false，不会耽误进战斗阶段。
            Executors.Insert(0, MaiBotBrain.MaybeWrap(new CardExecutor(
                ExecutorType.Activate, CardId.MulcharmyFuwalos, MulcharmyActivate)));
            Executors.Insert(0, MaiBotBrain.MaybeWrap(new CardExecutor(
                ExecutorType.Activate, CardId.MulcharmyPurulia, MulcharmyActivate)));
        }

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>
        /// 「再混音手」的发动：①/② 都"能做就做"，但这里留一行调试日志——
        /// 它的 ② 要**解放自己**（离场换两只调整再同调），排查"终场为什么不见了"时就看这一行。
        /// </summary>
        private bool RemixerActivate()
        {
            if (_verbose && Card != null)
                Logger.WriteLine("[计划层] 再混音手发动：位置=" + Card.Location + "｜回合=" + Duel.Turn
                    + "｜回合玩家=" + Duel.Player + "｜desc=" + ActivateDescription);
            return true;
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
            73642296,   // 屋敷童
            73642297,
            73642298,
        };

        /// <summary>
        /// 「检索/拿卡」这类**纯我方候选**时的取卡优先级——按 202605 教程的展开链排：
        /// * **场地「点唱机酒吧」**：音轨制作人① 第一个要拿的就是它（① 追加通召、③ 解放调整从卡组拉本家）；
        /// * **旋钮手**：提示员① 要特召的就是它（3 星 + 1 星 = 4 星音轨制作人，教程每一条单卡线都这么开）；
        /// * **混音手/唱片师**：互相检索，凑 2+3 出 5 星；
        /// * **速攻魔法「杀手级调整曲同调」**：补点 + 对手回合加速同调，教程里默认盖放它收尾；
        /// * **削波手**：第二张加速同调（对方主要阶段自跳）。
        /// 「跳过手里已有的」两轮逻辑会把这个顺序变成"先补当前最缺的那一环"。
        /// </summary>
        private static readonly int[] PreferredPicks =
        {
            CardId.JukeboxBar,      // 点唱机酒吧（场地）
            CardId.Knob,            // 旋钮手（1 星，提示员① 的默认目标）
            CardId.Mixer,           // 混音手（2 星）
            CardId.Recordist,       // 唱片师（3 星）
            CardId.Announcer,       // 提示员（3 星，第二次通常召唤用）
            CardId.TuneUp,          // 速攻魔法「杀手级调整曲同调」
            CardId.CrackleClipper,  // 削波手（第二张加速同调）
        };

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

        // ============================================================ 计划层（见插件 docs/plan-layer.md）

        /// <summary>
        /// 本回合走哪条起手线。开局（自己回合的第一次决策）读一次手牌匹配，之后这一回合都按它走。
        ///
        /// 为什么要有这一层：WindBot 是"每个请求单独回答"，没人记得"这一回合要摆出什么"——
        /// 手里有「提示员 + 唱片师」时它可能先把唱片师摆上去（少一次检索），做出 4 星音轨制作人后
        /// 又可能用掉不该用的素材。实测（对空白卡组 12 局）首回合**一次都没有**做出过 5 星「再混音手」，
        /// 链在 4 星「音轨制作人」之后就断了。
        /// </summary>
        private enum PlanLine
        {
            /// <summary>没匹配到起手线 → 完全走原来的优先级。</summary>
            None,
            /// <summary>手牌有「提示员」：①拉旋钮手 → 3+1=4★音轨制作人 → ①拿场地 → ③解放音轨制作人拉唱片师 → ①检索混音手 → 场地①追加通召混音手 → ①检索 3 星 → 混音手+手卡 3 星=5★再混音手 → 盖速攻。</summary>
            Announcer,
            /// <summary>手牌有「唱片师」：①检索旋钮手 → 旋钮手①召唤 → 3+1=4★音轨制作人 → ①拿场地 → ③解放音轨制作人拉混音手 → ①检索提示员 → 场地①追加通召提示员 → ①拉回墓地音轨制作人 → 混音手+提示员=5★再混音手 → 盖速攻。</summary>
            Recordist,
            /// <summary>
            /// 手牌有「旋钮手」+ 任意 3 星调整（教程第七节）：旋钮手① 追加召唤 → 旋钮手+手卡 3 星=4★音轨制作人
            /// → 音轨制作人① 检索**提示员** → NS 提示员 → ① 特召混音手 → 混音手① 检索唱片师
            /// → 音轨制作人+手卡唱片师=7★**锁缚龙**；提示员+混音手=5★再混音手 → 盖速攻。
            /// **终场：锁缚龙 + 再混音手 + 盖放「杀手级调整曲同调」。**
            /// </summary>
            Knob3,
            /// <summary>
            /// 手牌有「混音手」+ 任意 3 星调整（教程第八节）：NS 混音手 → ① 检索旋钮手 → 旋钮手① 追加召唤
            /// → 旋钮手+手卡 3 星=4★音轨制作人 → ① 检索场地 → 场地③ 解放混音手 → 提示员加手
            /// → 场地① 追加通召提示员 → ① 特召唱片师 → 唱片师① 检索混音手
            /// → 音轨制作人+提示员=7★**锁缚龙**；唱片师+手卡混音手=5★再混音手 → 盖速攻。
            /// **终场：锁缚龙 + 再混音手 + 盖放「杀手级调整曲同调」。**
            /// </summary>
            Mixer3,
            /// <summary>
            /// 手牌有「混音手」+ 任意 1 星调整（教程第九节）：NS 混音手 → ① 检索旋钮手 → 旋钮手① 追加召唤
            /// → 场上 2 只 + 手卡 1 星调整（3 只）= 4★音轨制作人 → ① 检索场地 → 场地③ 解放混音手 → 提示员加手
            /// → 场地① 追加通召提示员 → ① 特召唱片师 → 唱片师① 检索混音手 → 唱片师+手卡混音手=5★再混音手 → 盖速攻。
            /// **终场：提示员 + 再混音手 + 盖放「杀手级调整曲同调」**（终场较小，是稀有的牌型）。
            /// </summary>
            Mixer1,
        }

        /// <summary>本回合的计划（<see cref="EnsurePlan"/> 定，之后只读）。</summary>
        private PlanLine _planLine = PlanLine.None;

        /// <summary>这份计划是给第几回合定的（<see cref="Duel.Turn"/>），换回合要重新读手牌。</summary>
        private int _planTurn = -1;

        /// <summary>是否打印调试信息（命令行 ``Debug=true``，与基类同一套开关）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>已经打过日志的计划回合，避免每个决策点重复打同一份计划。</summary>
        private int _loggedPlanTurn = -1;

        /// <summary>
        /// 每回合读一次手牌、匹配一条起手线——**"这一回合要摆出什么"的唯一来源**：
        /// 后面的检索、拉人、追加通召都问它"这条线还缺哪一环"。
        ///
        /// 只做集合判断（手牌里有没有 X），**不判断合法性**：能不能发动、有没有自肃、
        /// 素材等级够不够，全部交给内核与卡脚本（三层分工）。
        /// 没匹配到就回到原来的优先级——不会把原来会打的局打坏。
        /// </summary>
        private void EnsurePlan()
        {
            if (Duel.Player != 0)
            {
                // 对手回合不计划：这份计划只描述"我这一回合怎么铺场"（对手回合的动作是另一层的事）。
                _planLine = PlanLine.None;
                return;
            }
            if (_planTurn == Duel.Turn)
                return;
            _planTurn = Duel.Turn;
            _planLine = PlanLine.None;
            _planRecordistSearches = 0;   // 新回合：唱片师① 的"第几次检索"重新数
            _planTrackMakerSummoned = false;
            // 按教程的优先级匹配：单卡线（提示员 → 唱片师）优先，两卡线排后面。
            // **为什么两卡线排后面**：手上有提示员/唱片师时单卡线的资源更多（plan-layer.md 里
            // 提示员线三件齐 88% 是已经验收过的线），两卡线只在"没有单卡起手件"时才是最优解；
            // 顺序不动，避免把已验证的线挤掉。
            // 两卡线的匹配条件＝教程第七/八/九节的起手：旋钮手/混音手 + 任意 3 星（或 1 星）调整。
            if (HasInHand(CardId.Announcer))
                _planLine = PlanLine.Announcer;
            else if (HasInHand(CardId.Recordist))
                _planLine = PlanLine.Recordist;
            else if (HasInHand(CardId.Knob) && HasTunerInHandOfLevel(3))
                _planLine = PlanLine.Knob3;
            else if (HasInHand(CardId.Mixer) && HasTunerInHandOfLevel(3))
                _planLine = PlanLine.Mixer3;
            else if (HasInHand(CardId.Mixer) && HasTunerInHandOfLevel(1))
                _planLine = PlanLine.Mixer1;
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

        /// <summary>场上（自己的怪兽区）有没有这张卡。</summary>
        private bool BotHasOnField(int cardId)
        {
            foreach (ClientCard card in Bot.MonsterZone)
            {
                if (card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 手牌里有没有"这条两卡线要用的那只调整"（教程第七/八/九节的起手条件"任意 3 星 / 1 星调整"）。
        /// **「旋钮手」自己不算**：它 ①（脚本 `c17209452.lua`）的 cost 是"把手卡的这张卡和**另一只**调整
        /// 给对方观看"——`Duel.IsExistingMatchingCard(s.cfilter, tp, LOCATION_HAND, 0, 1, c)` 里
        /// `s.cfilter` 是"调整且未公开"，而且把自身 `c` 排除掉了。所以"混音手 + 旋钮手"两张牌凑不出
        /// 那条线，手牌里必须真的还有第三只调整（不然会误判成有计划、让后面的闸门全部误触发）。
        /// </summary>
        private bool HasTunerInHandOfLevel(int level)
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (card == null || card.Level != level || !card.IsTuner())
                    continue;
                if (card.IsCode(CardId.Knob) || card.IsOriginalCode(CardId.Knob))
                    continue;
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

        /// <summary>
        /// 这条线是不是"**NS 混音手开的**"两卡线（教程第八/九节）——它们的收尾是"手卡混音手当素材"
        /// （"3 星唱片师 + 手卡 2 星混音手 = 再混音手"）、而且场地③ 要解放场上的混音手去拿提示员，
        /// 所以额外的召唤闸门与解放取舍都要单独放行。
        /// </summary>
        private bool IsMixerLine()
        {
            return _planLine == PlanLine.Mixer3 || _planLine == PlanLine.Mixer1;
        }

        /// <summary>
        /// 这条线是不是"手里还捏着「混音手」、等着把它铺到场上"——两道额外召唤闸门都用它：
        /// 计划进行中（提示员/唱片师线）场上还没混音手，而手卡里有一张时，
        /// **先别做那些会把素材吃掉的额外召唤**（手卡的混音手当素材会把场上的唱片师一起带走）。
        /// 混音手上了场、或者手里没有混音手，就自动放行。
        /// </summary>
        private bool PlanHoldsMixerForField()
        {
            if (!PlanActive())
                return false;
            // 两卡线的收尾**要的就是手卡的「混音手」当素材**——教程第八/九节原文：
            // "3 星唱片师 + 手卡 2 星混音手 = 再混音手"。这两条线不吃这道闸门（否则 5 星终端做不出来）。
            if (IsMixerLine())
                return false;
            if (!HasInHand(CardId.Mixer))
                return false;
            return !BotHasOnField(CardId.Mixer);
        }

        /// <summary>
        /// 计划进行中要"先放一放"的额外怪：它们会把这条线要用的素材（混音手/场上的怪）吃掉。
        /// ⚠ **A/B 开关**（环境变量 `KT_ALLOW_PLAN_EXTRA`，与 `RAISEMOON_AB`/`MULCHARMY_GATE` 同款）：
        /// 上面这段注释说这些卡里"响度战争/红印鉴/B2B/鲜花本身就是场上的阻抗件"，而这张表又把它们
        /// 在计划进行中一律封住 —— 实测后果是 **鲜花女男爵/PSY骨架王·Ω/加速同调星尘龙/噼啪削波手
        /// 对空白 20 局一次都没出场**（`temp/train/blankR-95.log` 的"从没出场"清单就是这四张，
        /// 而它们的素材是够的：6★ 调整「PSY骨架驱动者」+ 4★「欢聚友伴」= 10、3★ 调整 + 2★ = 5/8）。
        /// 封它们的理由是"会把线里的素材吃掉"，所以哪种更强只能量：`KT_ALLOW_PLAN_EXTRA=1`
        /// 让这道闸门在计划进行中也放行，其余行为不变。
        /// </summary>
        private static readonly bool AllowPlanExtraSummon =
            System.Environment.GetEnvironmentVariable("KT_ALLOW_PLAN_EXTRA") == "1";

        private static readonly int[] OffPlanExtraMonsters =
        {
            CardId.LoudnessWar,     // 响度战争 Lv6：实测就是它把"手卡混音手 + 场上音轨制作人"当素材出掉了
            CardId.RedStamp,        // 红印鉴唱片师 Lv5
            CardId.CrackleClipper,  // 噼啪削波手 Lv5
            CardId.BackToBack,      // B2B Lv10
            // ⚠ 锁缚龙（4891376）**不在这里**：社区公认它是这副牌的**核心三色康**（"默认合格＝锁缚龙 1 康 +
            // 重混/响度战争 1 个加速干扰"），上一版把它一起封掉正好和"能打断对面"相反——2026-10-04 移除。
            84815190,               // 鲜花女男爵 Lv10
            74586817,               // PSY骨架王·Ω Lv8
            30983281,               // 加速同调星尘龙 Lv8
        };

        /// <summary>
        /// 场地「点唱机酒吧」的发动时机：这张卡本身**贴到场 + ① 追加通召**随时都能用；
        /// ③（解放调整 → 从卡组拉人）在线路上要**等音轨制作人先出场**——教程那一步是"解放音轨制作人拉唱片师"，
        /// 提前开就会先去吃场上的提示员/旋钮手，"3+1 出音轨制作人"整步直接没了（实测首回合就是这么断的）。
        /// </summary>
        private bool JukeboxActivate()
        {
            EnsurePlan();
            if (_planLine == PlanLine.None)
                return true;
            if (ActivateDescription == Util.GetStringId(CardId.JukeboxBar, 1))
                return _planTrackMakerSummoned;
            return true;
        }

        /// <summary>
        /// 「金满而谦虚之壶」的时机：走线时**不开**——它的代价是从额外卡组除外 6 张（挑哪 6 张还是随机的），
        /// 实测一次就把两张「再混音手」和两张「音轨制作人」全除外了，整条线当场作废。
        /// </summary>
        private bool PotOfProsperityActivate()
        {
            return !PlanActive();
        }

        /// <summary>额外召唤的闸门（再混音手）：见 <see cref="PlanHoldsMixerForField"/>。</summary>
        private bool AllowRemixerSummon()
        {
            return !PlanHoldsMixerForField();
        }

        /// <summary>
        /// 「计划进行中先放一放」的额外怪（<see cref="OffPlanExtraMonsters"/>）的闸门。
        /// 它们会把这条线要用的素材（手卡混音手、场上的音轨制作人/唱片师）当同调素材吃掉；
        /// 同时这条"专属规则"也顺便**顶掉基类那条笼统的 SpSummon**（`DefaultNoExecutor` 只在
        /// "这张卡没有专属规则"时才生效）。
        /// 只在自己的回合、且计划还在走的时候拦——**对手回合照旧放行**（B2B/响度战争 那些正是
        /// 对手回合的加速同调与阻抗，不能一起锁掉）。
        /// </summary>
        private bool AllowOtherExtraSummon()
        {
            EnsurePlan();
            if (AllowPlanExtraSummon)
                return true;        // A/B：计划进行中也放行（见 AllowPlanExtraSummon 的说明）
            return !PlanActive();
        }

        /// <summary>
        /// 额外召唤的闸门（音轨制作人）：线路上只要**第一只**（① 去拿场地）。
        /// 第二只实测会把场地③ 拉出来的「唱片师」当素材吃掉（手卡手坑当 1 星素材凑 4 星），
        /// 终场就少一个 3 星站场——所以这一回合放过一只之后就拦住。
        /// </summary>
        private bool AllowTrackMakerSummon()
        {
            if (!PlanActive())
                return true;
            if (_planTrackMakerSummoned)
                return false;
            _planTrackMakerSummoned = true;
            return true;
        }

        /// <summary>
        /// 额外召唤的闸门（锁缚龙 锁镰）：**两卡线中途先别出**——教程给这只 7 星的素材很具体：
        /// 第七节"**场上 4 星音轨制作人 + 手卡 3 星唱片师** = 锁缚龙"（那张唱片师是「混音手①」检索来的），
        /// 第八节"4 星音轨制作人 + 3 星提示员 = 锁缚龙"。
        /// 不拦的话：音轨制作人一出场、提示员一进手（或手牌里那只 3 星手坑还没用掉），内核就能把
        /// 提示员/手坑当素材提前出 7 星——而这两个身体是线路**后面**做「再混音手」（提示员/混音手）
        /// 与 5 星终端的料，等于把终场拆掉。实测的同型事故（`temp/train/maxboard-95.log` 第 4/5 局，
        /// 那两局的手牌就是"旋钮手 + 3 星调整"）：走线时音轨制作人 ① **检索了场地**（教程第七节要的是提示员）、
        /// 「同调」被当场发动花掉 → 终场只剩 6 星响度战争 + 一只混音手，教程要的 5 星再混音手/锁缚龙 全没出来。
        /// 只在**两卡线进行中**拦；没计划 / 对手回合 / 单卡线（提示员线、唱片师线）一律照旧"能做就做"
        /// ——单卡线的 5 星终端是再混音手，锁缚龙 照样在线尾出现（见 OffPlanExtraMonsters 的注释）。
        /// </summary>
        private bool AllowLockDragonSummon()
        {
            EnsurePlan();
            if (!PlanActive())
                return true;
            // 单卡线（提示员/唱片师）不拦：它们的 5 星终端是再混音手，锁缚龙 是顺路做出来的（见注释上方）。
            if (_planLine == PlanLine.Announcer || _planLine == PlanLine.Recordist)
                return true;
            // "素材到位"＝教程指定的那只已经到手/在场：
            // * 手卡 3 星「唱片师」＝ 场上的音轨制作人（4）+ 手卡它（3）= 7 星（第七/八/九节通用的那只）；
            // * 混音手线（第八节）再加一支：**场上 3 星「提示员」**（音轨制作人 + 提示员 = 7 星）。
            // 其余情况一律先不出——素材还没到手时能凑出的 7 星只能拿"手卡里的 3 星提示员/手坑"当料，
            // 而那只 3 星是线路下一步要用的身体或检索点。
            if (HasInHand(CardId.Recordist))
                return true;
            if (_planLine == PlanLine.Mixer3)
                return BotHasOnField(CardId.Announcer);
            return false;
        }

        /// <summary>
        /// 「旋钮手」①（手卡效果：展示手卡 2 只调整 → 召唤 1 只调整）的时机：
        /// * 唱片师线要靠它开（教程第二步就是"旋钮手① 召唤"）→ 照常发动；
        /// * 提示员线**要先把提示员通常召唤下去**（提示员① 从卡组拉旋钮手，3+1 出音轨制作人）——
        ///   这时候先把旋钮手① 用掉，就会用掉场上那只旋钮手当素材、还把通召点留在手里慢慢做，
        ///   整条线的顺序全乱（实测首回合就是这么丢了"场地① 追加通召混音手"那一环）。
        /// </summary>
        private bool KnobActivate()
        {
            EnsurePlan();
            // 「旋钮手」②（被当作同调素材送去墓地时）是**二选一**（脚本 `c17209452.lua:81-83`）：
            //   `{b1,aux.Stringid(id,2),1}`＝从**对方墓地**把 1 张卡回到卡组最下面；
            //   `{b2,aux.Stringid(id,3),2}`＝**确认对手全部手卡**，之后从卡组检索 1 张本家魔陷。
            // 教程把后者当成这副牌的胜负手（"看手 = 全透明对局，不用去防不存在的牌"），而且它还顺带
            // **检索一张本家魔陷**——所以选的是 `aux.Stringid(id,3)` 那一支，**k=3**。
            // 不登记这个值的话会落到基类 `Rand.Next` 掷骰子。
            // （① 没有选支，所以在这里无条件登记不会污染它；登记值带卡号，也不会被别的卡的选项误吃。）
            // ⚠ 这里原来写的是 `EncodedOption(CardId.Knob, 2)`（"卡号*16 + 脚本值 + 1"那套）：那个 2 是
            // `aux.SelectFromOptions` 表里**第 3 列**（脚本自己用的分支值），不是 Stringid 的下标；
            // 对这个表来说"脚本值 + 1"恰好等于 Stringid 的下标（1/2 与 2/3 差 1），所以旧值**碰巧**是对的，
            // 现在按脚本读出来的 `aux.Stringid(id,3)` 写死成 k=3（见 :meth:`EncodedOption`）。
            // 另一支的 k=2（对方墓地回底）在 <see cref="PreferredOptionValues"/> 里当后备。
            _optionValue = EncodedOption(CardId.Knob, 3);
            if (_planLine == PlanLine.Announcer && HasInHand(CardId.Announcer))
                return false;
            return true;
        }

        /// <summary>
        /// 内核给"分支型效果"的选项值编码：**`卡号 * 16 + k`**，k 就是卡脚本里 `aux.Stringid(卡号, k)`
        /// 的序号（内核把那个字符串**原样**当选项值发给客户端）——也就是 `Util.GetStringId(卡号, k)`
        /// （`Util` 是 `Executor` 的实例属性 `AIUtil`，用法见 `RaiseMoonExecutor`：
        /// `desc == Util.GetStringId(卡号, k)`）。
        /// **直接写 1/2 是匹配不上的**——那样会静默落到基类 `DoEveryThingExecutor.OnSelectOption` 的
        /// `Rand.Next`，表现就是"同一个效果一半的局做出蠢事"。
        ///
        /// ⚠ 这里原来是 `卡号 * 16 + 脚本值 + 1`——那个"+1"是从魔女术「慶典」反推出来的**巧合**：
        /// 那张卡的 `aux.SelectFromOptions` 表里第 3 列（脚本自己用的分支值）与第 2 列（`aux.Stringid`
        /// 的序号 k）刚好差 1（`{b1,aux.Stringid(id,2),1}`），换一张卡就不成立。
        /// 实测口径（2026-10-05 的探针日志）：卡通那副 `options=[1190,728584499]`，
        /// 而 `45536531 * 16 + 3 = 728584499`；同一份日志里「落胤与圣女」登记成 `…+1` 的值永远差一位、
        /// 一次都没命中（同款修正见 `ToonShopExecutor` / `SkyStrikerShopExecutor`）。
        /// 对本副牌的「旋钮手」② 来说旧值**碰巧**等于正确的 k=3，所以这里按脚本写死 k、不再沿用偏移。
        /// </summary>
        private int EncodedOption(int cardId, int stringIndex)
        {
            return Util.GetStringId(cardId, stringIndex);
        }

        /// <summary>
        /// 系统提示号（`ygopro/strings.conf`）：1190＝加入手卡、1153＝盖放、1152＝特殊召唤。
        /// ⚠ 这三个**不带卡号**（是全局通用字符串，不属于"卡号 * 16 + k"那套编码），
        /// 所以不能塞进 <see cref="_optionValue"/>（那样会和别家的选项撞上），只能配合
        /// <see cref="PreferredOptionValues"/> 按"正在结算的那张卡"限定之后按值匹配。
        /// </summary>
        private const int OptionAddToHand = 1190;
        private const int OptionSpecialSummon = 1152;
        private const int OptionSet = 1153;

        /// <summary>
        /// 把"卡号 + 卡脚本里 `aux.Stringid(卡号, k)` 的 k"编成内核真正发给客户端的选项值
        /// （＝ `Util.GetStringId(卡号, k)` ＝ `卡号 * 16 + k`，与 <see cref="EncodedOption"/> 同一套口径）。
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
        /// 「锁缚龙 锁镰」② 的选支顺序：**康对面的那个效果，永远别康自己的**（2026-10-07 群友实测问题）。
        ///
        /// 脚本 `c4891376.lua:58-60` 的两支分别指向链上相邻的两环：
        /// * k2 → `Duel.NegateEffect(ev)`＝**锁缚龙连锁的那个效果**（链上倒数第 1 个）；
        /// * k3 → `Duel.NegateEffect(ev-1)`＝**那个效果连锁的卡的效果**（链上倒数第 2 个）。
        /// 哪一支是"对面的效果"取决于连锁怎么排：
        /// * 锁缚龙**直接连锁对手**的效果（教程之外最常见的场面）→ 倒数第 1 个是对面的 → **k2**；
        /// * 自己先用速攻「同调」垫了一环、锁缚龙再康链根（教程 §四 的 C2/C3）→ 倒数第 1 个是自己的、
        ///   倒数第 2 个才是对面的 → **k3**。
        ///
        /// 归属从 `Duel.CurrentChainInfo` 逐环读（每一环都带 `ActivatePlayer`）。自己的那一环可能已经进链、
        /// 也可能还没进，所以先按卡号把它认出来再往前数。两环都不是对面的（判不出来）就退回原来的口径
        /// （先 k3）——那种局面本来就不该发锁缚龙，闸门见 :meth:`Activate` 里 `InterruptionCards` 那条。
        /// </summary>
        private int[] LockDragonOptionOrder()
        {
            IList<ChainInfo> chain = Duel.CurrentChainInfo;
            int last = chain == null ? -1 : chain.Count - 1;
            if (last >= 0 && IsSameCard(chain[last].RelatedCard, CardId.LockDragonLock))
                last--;     // 自己已经进链：往前退一环，`last` 才是"锁缚龙连锁的那个效果"
            bool topIsEnemy = last >= 0 && chain[last].ActivatePlayer == 1;
            bool rootIsEnemy = last - 1 >= 0 && chain[last - 1].ActivatePlayer == 1;
            if (_verbose)
                Logger.WriteLine("[探针] 锁缚龙选支：链上倒数"
                    + (topIsEnemy ? "第 1 个是对面的 → k2" : rootIsEnemy ? "第 2 个是对面的 → k3" : "两环都不是对面的 → 退回 k3")
                    + "（链长 " + (chain == null ? 0 : chain.Count) + "）");
            if (topIsEnemy)
                return OptionValues(CardId.LockDragonLock, 2, 3);
            return OptionValues(CardId.LockDragonLock, 3, 2);
        }

        /// <summary>
        /// 分支卡的"想要哪一支"——按**正在结算（认不出来时＝正在发动）的那张卡**认，
        /// 逐张对过 `ygopro/script` 下的卡脚本（2026-10-05 复核）。数组顺序＝优先级：先试第一个，
        /// 命中就用（某一支因为条件不成立被脚本压缩掉时会自动落到下一支——见 <see cref="OnSelectOption"/>）。
        /// 每张卡的 k（＝脚本里 `aux.Stringid(卡号, k)` 的序号）与它对应的分支：
        /// * 「旋钮手」② `c17209452.lua:81-83`（`aux.SelectFromOptions` 的
        ///   `{b1,aux.Stringid(id,2),1}` / `{b2,aux.Stringid(id,3),2}`）→ k2＝对方墓地 1 张回卡组最下面、
        ///   k3＝确认对手全部手卡 + 检索 1 张本家魔陷；
        /// * 「提示员」② `c16387555.lua:95`（`Duel.SelectOption(tp,aux.Stringid(id,2),aux.Stringid(id,3))==1`）
        ///   → k2＝剩下的那张回卡组最上面、k3＝回卡组最下面；
        /// * 「B2B」③ `c65961304.lua:78-80`（`aux.SelectFromOptions` 的 `{b1,1190,1}` / `{b2,1152,2}`）
        ///   → 1190＝加入手卡、1152＝特殊召唤（**系统串**，不是卡号编码）；
        /// * 「锁缚龙 锁镰」② `c4891376.lua:58-60`（`{b1,aux.Stringid(id,2),1}` / `{b2,aux.Stringid(id,3),2}`）
        ///   → k2＝无效 `Duel.IsChainDisablable(ev)` 那个（链上**最新发动**的效果）并破坏、
        ///   k3＝无效 `ev-1` 那个（**被这次连锁所指**、更早的那个效果）并破坏；
        /// * 「三战之才」① `c25311006.lua:26-45`（手写 `ops` 表 + `Duel.SelectOption(tp,table.unpack(ops))`）
        ///   → k0＝自己抽 2、k1＝夺对方 1 只的控制权到结束阶段、k2＝看对方手卡并把 1 张回卡组；
        /// * 「三战之号」① `c35269904.lua:37`（`Duel.SelectOption(tp,1153,1190)==0`）
        ///   → 1153＝盖放、1190＝加入手卡（**系统串**；注意盖放在前，按下标取会拿错）。
        ///   ⚠ 卡表里的另一印刷号 35269905 的脚本内容一致，匹配上**两个印刷号都认**
        ///   （见 :meth:`PreferredOptionValues` 里的判据），选项值不受印刷号影响。
        /// 认不出来（别的卡 / 卡信息还没读出来）返回 null，交回基类——不猜。
        /// </summary>
        private int[] PreferredOptionValues(ClientCard effect)
        {
            if (effect == null)
                return null;
            // 「旋钮手」②：要**确认对手全部手卡 + 检索 1 张本家魔陷**（k3）——教程"二、本家相同的发动条件"
            // 把它当胜负手（"看手 = 全透明对局"），还顺带检索一张本家魔陷（「同调」/「播放列表」）。
            // k2（对方墓地 1 张回卡组最下面）是纯干扰，只在 k3 那一支做不出来（对方没手卡）时当后备。
            if (IsSameCard(effect, CardId.Knob))
                return OptionValues(CardId.Knob, 3, 2);
            // 「提示员」②：要**把剩下的那张放回卡组最下面**（k3）。卡文："从对方卡组上面把 2 张卡翻开，
            // 从那之中把 1 张除外，另 1 张回到卡组最上面或最下面"；脚本 `c16387555.lua:95` 里
            // `…==1` 那一支才 `Duel.MoveSequence(g:GetFirst(),SEQ_DECKBOTTOM)`，即 k2＝留最上面、k3＝放最下面。
            // ⚠ 用户此前的 spec 是"把对手卡组顶的**好卡**除外"，但**内核把翻开的卡当未知卡送过来**
            // （见 OnSelectCard 里 `hint == HintMsg.Remove` 那段保留的结论：探针实测 `候选卡号=0、0`，
            // 判不了哪张是好卡），所以这里只做"顶/底"这一层选择：既然认不出好坏，就按**对卡组顶的干扰
            // 最大化**处理——2 张里 1 张已经被除外（那一步只能盲选）、另 1 张压到卡组最下面，让对手下回合
            // 抽到一张全新的牌（放回顶上等于对面这次抽牌和没发效果一样）。
            // ⚠ 反方向的理由：教程 §二 的"看手组合"（"提示员② 看对手抽牌阶段抽到的那张"）要的正是 k2
            // ——**待探针/对局验证**：若更看重这条情报链，把顺序改成 `OptionValues(CardId.Announcer, 2, 3)`。
            if (IsSameCard(effect, CardId.Announcer))
                return OptionValues(CardId.Announcer, 3, 2);
            // 「B2B」③：要**特殊召唤**（1152）。脚本 `c65961304.lua:78-80` 是
            // `{b1,1190,1}`＝加入手卡 / `{b2,1152,2}`＝特殊召唤。
            // 教程 §四 的两条线都是"再拉墓地**唱片师** + 场上削波手 → 5 星红印鉴；第二次拉墓地
            // **音轨制作人** + 手卡混音手 → 6 星响度战争"：那只 4 星「音轨制作人」**必须落在场上**才能当
            // 同调素材、也才能给出"手卡 1 只调整作同调素材"的效果外文本（拿在手里两条素材都在手，
            // 同调召唤根本做不出来）；特召本身还多一个身位。这条与下面那条通用口径（1190 → 取另一支）
            // 结果一致，单独登记是为了不再依赖通用逻辑。
            if (IsSameCard(effect, CardId.BackToBack))
                return new int[] { OptionSpecialSummon, OptionAddToHand };
            // 「锁缚龙 锁镰」②：**按连锁归属选支**——康对面的那个效果，别康自己的（见 :meth:`LockDragonOptionOrder`）。
            // 脚本 `c4891376.lua:58-60`：
            //   k2＝`{b1,aux.Stringid(id,2),1}` → `NegateEffect(ev)`＝无效**锁缚龙连锁的那个效果**；
            //   k3＝`{b2,aux.Stringid(id,3),2}` → `NegateEffect(ev-1)`＝无效**那个效果连锁的卡的效果**。
            // ⚠ 原来这里写死 `OptionValues(…, 3, 2)`（先 k3）：那个顺序来自教程 §四 的"C2 速攻「同调」、
            // C3 锁缚龙康对手 C1"——链根是**对手**的牌，所以 k3 对。但锁缚龙**直接连锁对手效果**时
            // （链根是自己的牌）k3 就打到自己人身上了：2026-10-07 群友实测（日志
            // `MaiBot/logs/app_20261007_211344.log.jsonl` 21:59:36）链是
            // `[再混音手②(我方), 结晶魔术 光之泪(对手), 锁缚龙(我方)]`，两个选项都给全了
            // `options=[78262018,78262019]`（k2,k3），执行器按登记顺序命中 k3 → 无效并破坏了自己的
            // 「再混音手」，对手的「光之泪」照样结算（43 秒后拉出「魔女术师傅·玻璃女巫」）。
            if (IsSameCard(effect, CardId.LockDragonLock))
                return LockDragonOptionOrder();
            // 「三战之才」①：先"抽 2"（k0）。`c25311006.lua:26-45` 的 ops 表是**动态拼**的
            // （`ops[off]=aux.Stringid(25311006,k)`，只把"这一支做得出来"的列进去），所以要按值逐支回落：
            // k0＝抽 2（唯一与对面场面无关、一定列得出来）、k1＝夺 1 只控制权到结束阶段、k2＝看对方手卡回卡组。
            // 口径与另一副（`ToonShopExecutor` 对同一张卡）一致；若想走这副牌的"看手"主题（k2），
            // 改顺序即可——**待对局验证**。
            if (IsSameCard(effect, CardId.TripleTactics))
                return OptionValues(CardId.TripleTactics, 0, 1, 2);
            // 「三战之号」①：要**加入手卡**（1190）。脚本 `c35269904.lua:37` 是
            // `Duel.SelectOption(tp,1153,1190)==0`（0＝盖放、1＝加入手卡），**盖放那一支在前**，
            // 按下标取会拿错；原来它落到下面那条通用口径（"带 1190 就取另一支"）手里、一直按"盖放"走。
            // 加入手卡当回合就能开（能拿的通常魔法/通常陷阱里有三战之才/壶），盖放的那张
            // "这个效果盖放的卡在这个回合不能发动"整整慢一个回合（同款口径见 `ToonShopExecutor`）。
            // ⚠ 卡表用的是另一印刷号 35269905：**两个印刷号都认**（它的脚本 c35269905.lua 内容一致）。
            if (IsSameCard(effect, CardId.TripleTacticsThrust) || IsSameCard(effect, CardId.TripleTacticsThrustAlt))
                return new int[] { OptionAddToHand, OptionSet };
            return null;
        }

        /// <summary>
        /// 现在是谁的效果在问（正在发动/结算的那张卡）。同一个候选列表在不同效果里的正确答案不一样
        /// （场地③ 要从卡组拿「唱片师」，唱片师① 要拿「混音手」），只能按效果来源区分：
        /// 发动时的费用/目标选择用 GetCurrentChainCard()，效果结算中用 GetCurrentSolvingChainCard()。
        /// </summary>
        private ClientCard CurrentEffectCard()
        {
            ClientCard solving = Duel.GetCurrentSolvingChainCard();
            if (solving != null)
                return solving;
            return Duel.GetCurrentChainCard();
        }

        /// <summary>场地③「解放调整 → 从卡组加入手卡或特召」：提示员线要「唱片师」（① 检索混音手），唱片师线要「唱片师」（① 检索混音手 → 场地① 追加通召它 → 这只 3 星留在场上）。</summary>
        private static readonly int[] FieldPickAnnouncerLine = { CardId.Recordist, CardId.Mixer, CardId.Announcer, CardId.Knob };

        /// <summary>
        /// 同上的唱片师线版本＝**教程原文**（拉「混音手」，它 ① 检索提示员 → 场地① 追加通召提示员 → 提示员① 拉回墓地音轨制作人）。
        /// 试过改成拉「唱片师」，但**「唱片师①」是卡名一回合一次**：第一只通常召唤时已经用过 ①，第二只拉出来发不了效果，
        /// 整条线当场断在那里（实测 0/5）。
        /// </summary>
        private static readonly int[] FieldPickRecordistLine = { CardId.Mixer, CardId.Recordist, CardId.Announcer, CardId.Knob };

        /// <summary>唱片师①「3 星以外」的**第一次**检索：要「旋钮手」（旋钮手① 召唤后 3+1=4 星音轨制作人）。</summary>
        private static readonly int[] RecordistSearchFirst = { CardId.Knob, CardId.Mixer, CardId.Clipper };

        /// <summary>唱片师① 的**第二次**检索（场地③ 拉出来的那只）：要「混音手」（场地① 的追加通召点）。</summary>
        private static readonly int[] RecordistSearchLater = { CardId.Mixer, CardId.Knob, CardId.Clipper };

        /// <summary>提示员线的唱片师①（场地③ 拉出来的）：直接要「混音手」——它只有这一次检索。</summary>
        private static readonly int[] RecordistSearchAnnouncerLine = { CardId.Mixer, CardId.Knob, CardId.Clipper };

        /// <summary>
        /// 教程第八/九节：场地③ 从卡组拿「提示员」。
        /// 与单卡线不同：**这里必须是"加入手卡"那一支**——「提示员①」的卡文是"这张卡**召唤**的场合才能发动"，
        /// 特殊召唤出来的提示员发不了 ①（它 ① 是整条两卡线的下一环），所以先加手、再用场地① 的额外召唤摆上场。
        /// </summary>
        private static readonly int[] FieldPickMixerLine = { CardId.Announcer, CardId.Recordist, CardId.Mixer, CardId.Knob };

        /// <summary>教程第八/九节：混音手① 检索「旋钮手」（旋钮手① 的追加召唤是两卡线的第二步）。</summary>
        private static readonly int[] MixerSearchMixerLine = { CardId.Knob, CardId.Recordist, CardId.Announcer };

        /// <summary>教程第八/九节：提示员① 特召「唱片师」（下一步 唱片师① 检索混音手）。</summary>
        private static readonly int[] AnnouncerSummonMixerLine = { CardId.Recordist, CardId.Mixer, CardId.Knob };

        /// <summary>教程第七节：提示员① 特召「混音手」（它 ① 检索唱片师 → 锁缚龙的素材；本身还是再混音手的一半）。</summary>
        private static readonly int[] AnnouncerSummonKnob3 = { CardId.Mixer, CardId.Recordist, CardId.Knob };

        /// <summary>教程第八/九节：唱片师① 检索「混音手」（"3 星唱片师 + 手卡 2 星混音手 = 再混音手"）。</summary>
        private static readonly int[] RecordistSearchMixerLine = { CardId.Mixer, CardId.Knob, CardId.Clipper };

        /// <summary>
        /// 教程第七节：音轨制作人① 检索「提示员」（再 NS 提示员 → ① 拉混音手）。
        /// 这条路**不用场地**：旋钮手① 的追加召唤（脚本 `Duel.Summon(tp,tc,true,nil)`）不占通常召唤，
        /// 通召点留给提示员正好。
        /// </summary>
        private static readonly int[] TrackMakerSearchKnob3 = { CardId.Announcer, CardId.JukeboxBar, CardId.TuneUp, CardId.Clipper };

        /// <summary>本回合「唱片师①」结算到第几次（同一个效果在这条线里要用两次：先旋钮手、后混音手）。</summary>
        private int _planRecordistSearches;

        /// <summary>
        /// 登记给 `OnSelectOption` 的分支值（见 :meth:`EncodedOption`，**按值匹配、不带偏移**）——
        /// **带卡号**，所以命中才清，不会被别的卡的选项请求误吃。
        /// 当前只有「旋钮手」② 用它（在 <see cref="KnobActivate"/> 里登记"看手 + 检索本家魔陷"那一支）；
        /// 其余分支卡走 <see cref="PreferredOptionValues"/> 那张按"正在结算的卡"取值的表。
        /// </summary>
        private int _optionValue;

        /// <summary>本回合是不是已经放过一只「音轨制作人」（线路只要第一只，见 AllowTrackMakerSummon）。</summary>
        private bool _planTrackMakerSummoned;

        /// <summary>混音手①「2 星以外」的检索：**要 3 星**（和场上的 2 星混音手凑 5 星再混音手）——手材料那条要 3 星，别拿 1 星旋钮手。
        /// （旋钮手线（教程第七节）也用这张表：那一步要的就是「唱片师」——"混音手检索唱片师 → 场上 4 星音轨制作人 + 手卡 3 星唱片师 = 锁缚龙"。）</summary>
        private static readonly int[] MixerSearchAnnouncerLine = { CardId.Recordist, CardId.Announcer, CardId.Knob };

        /// <summary>同上的唱片师线版本＝教程原文（拿「提示员」，再由提示员① 把墓地的音轨制作人拉回来）。</summary>
        private static readonly int[] MixerSearchRecordistLine = { CardId.Announcer, CardId.Recordist, CardId.Knob };

        /// <summary>提示员①「拉 1 只调整」：提示员线要「旋钮手」（3+1 出音轨制作人），唱片师线要墓地里的「音轨制作人」。</summary>
        private static readonly int[] AnnouncerSummonAnnouncerLine = { CardId.Knob, CardId.Mixer, CardId.Recordist };

        /// <summary>同上的唱片师线版本。</summary>
        private static readonly int[] AnnouncerSummonRecordistLine = { CardId.TrackMaker, CardId.Knob, CardId.Mixer };

        /// <summary>音轨制作人①「拿 1 张本家卡」：场地优先（① 追加通召、③ 解放调整从卡组拉人）。</summary>
        private static readonly int[] TrackMakerSearchOrder = { CardId.JukeboxBar, CardId.TuneUp, CardId.Clipper };

        /// <summary>
        /// 走线时"这个效果该拿哪张"的顺序；没计划、认不出效果、或者这个效果该拿的东西这回合用不上时返回 null
        /// （交回原来的 <see cref="PreferredPicks"/>）。只影响"挑哪张"，不影响"能不能做"。
        /// </summary>
        private int[] PlanPickOrder()
        {
            if (!PlanActive())
                return null;
            ClientCard effect = CurrentEffectCard();
            if (effect == null)
                return null;
            if (effect.IsCode(CardId.JukeboxBar) || effect.IsOriginalCode(CardId.JukeboxBar))
            {
                // 教程第八/九节：场地③ 从卡组拿的是**提示员**（多一步"加手→通常召唤"）；
                // 单卡线那两条拿的是唱片师（那张"召唤·特殊召唤"都能触发 ①，可以直接特召）。
                if (IsMixerLine())
                    return FieldPickMixerLine;
                return _planLine == PlanLine.Recordist ? FieldPickRecordistLine : FieldPickAnnouncerLine;
            }
            if (effect.IsCode(CardId.Recordist) || effect.IsOriginalCode(CardId.Recordist))
            {
                if (IsMixerLine())
                    return RecordistSearchMixerLine;   // 教程第八/九节：这只唱片师要「混音手」
                return _planLine == PlanLine.Recordist
                    ? (_planRecordistSearches == 0 ? RecordistSearchFirst : RecordistSearchLater)
                    : RecordistSearchAnnouncerLine;
            }
            if (effect.IsCode(CardId.Mixer) || effect.IsOriginalCode(CardId.Mixer))
            {
                if (IsMixerLine())
                    return MixerSearchMixerLine;       // 教程第八/九节：混音手① 检索「旋钮手」
                return _planLine == PlanLine.Recordist ? MixerSearchRecordistLine : MixerSearchAnnouncerLine;
            }
            if (effect.IsCode(CardId.Announcer) || effect.IsOriginalCode(CardId.Announcer))
            {
                if (_planLine == PlanLine.Knob3)
                    return AnnouncerSummonKnob3;      // 教程第七节：提示员① 拉「混音手」
                if (IsMixerLine())
                    return AnnouncerSummonMixerLine;   // 教程第八/九节：提示员① 拉「唱片师」
                return _planLine == PlanLine.Recordist ? AnnouncerSummonRecordistLine : AnnouncerSummonAnnouncerLine;
            }
            if (effect.IsCode(CardId.TrackMaker) || effect.IsOriginalCode(CardId.TrackMaker))
                // 教程第七节：两卡线的音轨制作人 ① 拿去换**提示员**（单卡线换场地）。
                return _planLine == PlanLine.Knob3 ? TrackMakerSearchKnob3 : TrackMakerSearchOrder;
            return null;
        }

        /// <summary>调试用：把一串卡名拼成一行（只在开 Debug 时用）。</summary>
        private static string DescribeCards(IList<ClientCard> cards)
        {
            string text = "";
            foreach (ClientCard card in cards)
                text += (text.Length == 0 ? "" : "、") + DescribeCard(card);
            return text.Length == 0 ? "（空）" : text;
        }

        /// <summary>调试用：一张卡的名字（空位写成"空"）。</summary>
        private static string DescribeCard(ClientCard card)
        {
            if (card == null)
                return "（无）";
            return card.Name;
        }

        /// <summary>「杀手级调整曲同调」的发动判断：走线时**留着盖放**（教程每条线——含两卡线第七/八/九节——的最后一步都是"盖放「同调」"），没计划时照旧"能发就发"。</summary>
        private bool TuneUpActivate()
        {
            return !PlanActive();
        }

        /// <summary>
        /// 「杀手级调整曲同调」的盖放判断：走线时盖（留到对手回合；基类通用盖放规则只给"没人点名"的卡，
        /// 这张在 Activate 里已经点名，所以必须自己给一个盖放出口）。没计划时沿用基类的
        /// <see cref="DefaultSpellSet"/> 判断（速攻/陷阱且魔陷区没满）。
        /// </summary>
        private bool TuneUpSpellSet()
        {
            if (PlanActive())
                return true;
            return DefaultSpellSet();
        }

        /// <summary>
        /// 「表侧召唤 or 里侧盖放」：**这副牌一律表侧召唤**。
        ///
        /// 基类 `DefaultExecutor.OnSelectMonsterSummonOrSet` 的判据是"等级 ≤4 且自己场上没有表侧怪
        /// 且**对手的怪全都打得过我** → 盖放"，对这副牌是有害的：本家下级的效果几乎全是
        /// "**召唤**成功时／召唤的场合"才发动（提示员① 拉调整、混音手① 检索旋钮手、
        /// 唱片师① 检索混音手、音轨制作人① 检索提示员），而**里侧盖放不算召唤成功** →
        /// 手牌里唯一的动点被盖成一张白板，整个回合直接空过。
        ///
        /// 用户实测（`logs/app_20261005_230210.log.jsonl`，23:34:32）：我方回合手牌只有
        /// 「提示员 + PSY骨架装备·γ」，计划也认出了 `Announcer 线`，却
        /// `提示员 from Hand move to MonsterZone` 之后**立刻 `(Go to End)`** ——
        /// 对面场上攻击力比 900 高（提示员攻 900/守 1900，等级 3），基类判据就把它盖了下去。
        ///
        /// 这副牌的怪要么是本家引擎（必须表侧、必须触发召唤效果），要么是手坑
        ///（在 <see cref="SummonOrSet"/> 里已经被否掉），所以"盖一张墙"从来不是正确选择。
        /// </summary>
        public override bool OnSelectMonsterSummonOrSet(ClientCard card)
        {
            return false;
        }

        // ============================================================ 斩杀优先（照卡通/闪刀那套判据）

        /// <summary>
        /// 这个回合**进过场的怪**（卡号）：给斩杀判据用——本回合才上场的怪保守地先不算进"这回合能打多少"
        /// （和卡通/闪刀同一套口径；这副牌的额外怪里有"召唤回合不能攻击"这一类的风险，而音轨制作人 0/2500、
        /// 响度战争 0/3000 本来就是蹲守备的身板，多算一只就会"以为够了、进了战阶却打不死"）。
        /// 宁可晚一步也不要在打不死时白进战阶。
        /// 回合切换时清（<see cref="OnNewTurn"/>）——对手回合里特召出来的怪，到我们回合就解禁了。
        /// </summary>
        private readonly HashSet<int> _summonedThisTurn = new HashSet<int>();

        /// <summary>每回合清"这个回合才上场的怪"（对战阶/表示形式的判据保守一点：新上场的先不算参战）。</summary>
        public override void OnNewTurn()
        {
            base.OnNewTurn();
            _summonedThisTurn.Clear();
        }

        /// <summary>记录"我方有怪进场"（召唤/特殊召唤/盖放都算；对手回合里的也算，下个回合开头会清掉）。</summary>
        public override void OnMove(ClientCard card, int previousControler, int previousLocation, int currentControler, int currentLocation)
        {
            base.OnMove(card, previousControler, previousLocation, currentControler, currentLocation);
            if (card != null && currentControler == 0 && currentLocation == (int)CardLocation.MonsterZone)
                _summonedThisTurn.Add(card.Id);
        }

        /// <summary>
        /// 「这回合可以直接打死了」——注册在 `Executors` 的**最前面**（见构造函数 ⑦）。
        /// 判据（**保守**，宁可晚一步也不要打不死时白进战阶）：
        /// * 对面**场上没有怪**（＝我们的怪都能直击）；
        /// * 我方**表侧攻击表示**、且**不是这个回合才上场**的怪，攻击力合计 ≥ 对面 LP。
        /// 只在自己的回合、且内核说这一步能进战阶（`MainPhase.CanBattlePhase`）时才成立。
        /// </summary>
        private bool LethalAvailable()
        {
            if (Duel.Player != 0 || !Duel.MainPhase.CanBattlePhase)
                return false;
            if (Enemy.GetMonsterCount() > 0)
            {
                // 对面**有怪**也要能进战阶：只要我方有一只表侧攻击表示的怪能**拆掉**对面某只已知的怪。
                // 原来这里一律 `return false`，后果是"对面站一只 0 攻/2100 守的墙，我方 3200 攻的怪
                // 被钉在原地"——实测（迭代第 6 轮 `temp/rounds/r6/b95.log`）对空白墙 **43 回合只有
                // 2 次攻击宣言**，最后"没有卡可抽"判负（库里 3 局抽死有 2 局是这副牌）。
                // 里侧未知的怪不比（那等于赌）：留给 :meth:`OnSelectAttackTarget` 拒绝。
                bool breakable = CanBreakDefender();
                if (breakable && _verbose)
                    Logger.WriteLine("[战斗] 对面有怪，但我方能拆掉一只 → 进战斗阶段");
                return breakable;
            }
            int damage = LethalDamage();
            bool lethal = damage > 0 && damage >= Enemy.LifePoints;
            if (lethal && _verbose)
                Logger.WriteLine("[斩杀] 可以收掉：场上能打的攻击力合计 " + damage + " ≥ 对面 LP " + Enemy.LifePoints + "，直接进战斗阶段");
            return lethal;
        }

        /// <summary>
        /// 打"未知里侧怪"需要的攻击力门槛：低于它的怪不许撞里侧（那是赌），
        /// 高于它就该打——否则对面把墙盖着的时候整局都打不出伤害（见 :meth:`OnSelectAttackTarget`）。
        /// </summary>
        private const int UnknownSetSafeAttack = 2000;

        /// <summary>
        /// 我方有没有一只**表侧攻击表示**的怪能打得穿对面某只**已知（表侧）**的怪。
        /// 攻表示比 ATK、守表示比 DEF——只判"这一刀不亏"，不去预测对面的坑（那是另一层的事）。
        /// </summary>
        private bool CanBreakDefender()
        {
            foreach (ClientCard attacker in Bot.GetMonsters())
            {
                if (attacker == null || !attacker.IsFaceup() || !attacker.IsAttack())
                    continue;
                foreach (ClientCard defender in Enemy.GetMonsters())
                {
                    if (defender == null || !defender.IsFaceup())
                        continue;
                    bool wins = defender.IsAttack()
                        ? attacker.GetAttackPower() > defender.GetAttackPower()
                        : attacker.GetAttackPower() > defender.GetDefensePower();
                    if (wins)
                        return true;
                }
            }
            return false;
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
        /// 「为了斩杀把蹲着的守备怪转成攻击」：对面空场、且**把它转成攻击之后**伤害就够了 → 转。
        /// 这副牌的额外怪攻守很偏（音轨制作人 0/2500、响度战争 0/3000），基类/内核很容易把它们摆成守备；
        /// 对面只剩一点血时，表示形式直接决定这一刀打不打得出去。
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
        /// 表示形式：**只有"这一刀能让斩杀成立"时才选攻击表示**，其余交给基类。
        /// 照闪刀那份的写法（**不是**卡通那份"对面空场一律攻击"）：这副牌额外组的攻守分配是刻意的
        /// （音轨制作人 0/2500、响度战争 0/3000 都是"当墙更好"的身板），无脑站攻击只会在对面空场时
        /// 白送打点；只有"它站攻击 + 场上已有的攻击表示的怪 ≥ 对面 LP"时才改。
        /// </summary>
        public override CardPosition OnSelectPosition(int cardId, IList<CardPosition> positions)
        {
            if (Duel.Player == 0 && Enemy.GetMonsterCount() == 0 && positions.Contains(CardPosition.FaceUpAttack))
            {
                int damage = LethalDamage();
                foreach (ClientCard card in Bot.GetMonsters())
                {
                    if (card.Id == cardId && card.IsFaceup() && card.IsDefense() && !_summonedThisTurn.Contains(card.Id))
                        damage += card.GetAttackPower();
                }
                if (damage >= Enemy.LifePoints)
                    return CardPosition.FaceUpAttack;
            }
            return base.OnSelectPosition(cardId, positions);
        }

        /// <summary>通召/盖放的统一入口：手坑一律否掉；**「PSY骨架驱动者」一律否掉**；起手件优先（见 PreferredSummons）；其余交给基类。</summary>
        private bool SummonOrSet()
        {
            if (Card != null)
            {
                foreach (int id in HandTraps)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return false;
                }
                // 「PSY骨架驱动者」也一律否掉——它不是这副牌的身体，是 γ/δ 的**弹药**。
                // * 卡文：「PSY骨架装备·δ/γ」(74203495/38814750)①都是"手卡的这张卡和**自己的手卡·卡组·墓地**
                //   1只「PSY骨架驱动者」特殊召唤，那个发动无效并破坏"——它待在手里/卡组/墓地才是一张阻抗，
                //   摆到场上只是一个 2500/0 的凡骨（**不是调整**、不是本家，做不了任何本家同调素材）。
                // * 更要命的是它会**吃解放**：基类 `DefaultMonsterSummon` 对"等级 >4"的怪按
                //   `ceil((等级-4)/2)` 数祭品、只要场上有"守备力 < 它攻击力"的怪就放行——本家下级的守备力
                //   （旋钮手 800、提示员 1900、混音手 2000、唱片师 800、削波手 1000）**全都低于 2500**，
                //   于是它会解放一只本家调整去上级召唤这个凡骨。
                // * 日志实证（`temp/train/maxboard-95.log`，2026-10-05 23:59:36，对空白卡组那一轮的第 1 回合）：
                //   `(0 's 杀手级调整曲·旋钮手 activate effect from Hand)` → `… from Hand move to MonsterZone`
                //   → `(0 's 杀手级调整曲·旋钮手 from MonsterZone move to Grave)`
                //   → 落位 `第 1 回合 我方（我方） 主怪兽区3 ← #49036338` → `(0 's PSY骨架驱动者 from Hand move to MonsterZone)`
                //   ——首回合把**卡组里唯一一张**「旋钮手」（提示员①/混音手①/唱片师① 都要检索的 1 星调整、
                //   3+1 出 4★音轨制作人的那一环）当祭品烧掉，整条线当场断（那一轮的两个座位各一次）。
                if (IsSameCard(Card, CardId.PsyFrameDriver))
                    return false;
                // 走线时按"这条线下一步要谁上场"挑：起手件（提示员/唱片师）还在手上 → **通召只认它**
                //（内核给的可通召列表顺序不固定，混音手排在前面就会被抢走通召——实测就是这么丢掉
                // "提示员① 从卡组拉旋钮手"那一环的）；起手件已经用掉 → 这次是场地① 的**追加通召**，
                // 要的是「混音手」（5 星「再混音手」的指定素材，换成别的怪整个终场就做不出来）。
                if (PlanActive())
                {
                    // 两卡线（教程第七/八/九节）的通召点：
                    // * 混音手线（八/九）：**第一步就是 NS「混音手」**（它 ① 检索旋钮手）；混音手已经在场上
                    //   之后，通召/追加通召点留给「提示员」（场地③ 加手的那只，靠 场地① 的额外召唤摆上场）；
                    // * 旋钮手线（七）：旋钮手是靠它自己的 ① 上场的——脚本 `c17209452.lua` 的 `sumop` 用
                    //   `Duel.Summon(tp,tc,true,nil)`（第二参 true＝无视召唤次数），**不占通召点**，
                    //   所以通召点要留给「提示员」。
                    if (IsMixerLine())
                    {
                        if (HasInHand(CardId.Mixer) && !BotHasOnField(CardId.Mixer))
                            return Card.IsCode(CardId.Mixer) || Card.IsOriginalCode(CardId.Mixer);
                        if (HasInHand(CardId.Announcer))
                            return Card.IsCode(CardId.Announcer) || Card.IsOriginalCode(CardId.Announcer);
                    }
                    if (_planLine == PlanLine.Knob3 && HasInHand(CardId.Announcer))
                        return Card.IsCode(CardId.Announcer) || Card.IsOriginalCode(CardId.Announcer);
                    if (_planLine == PlanLine.Announcer && HasInHand(CardId.Announcer))
                        return Card.IsCode(CardId.Announcer) || Card.IsOriginalCode(CardId.Announcer);
                    if (_planLine == PlanLine.Recordist && HasInHand(CardId.Recordist))
                        return Card.IsCode(CardId.Recordist) || Card.IsOriginalCode(CardId.Recordist);
                    if (Card.IsCode(CardId.Mixer) || Card.IsOriginalCode(CardId.Mixer))
                        return true;
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
        /// 通召优先级（操作方 spec 的单卡展开点）。**通召一回合只有一次**，把起手件换成别的怪，
        /// 整条链就断在第一步——用户实测的"展开一通场上只有一个怪"多半就是这么来的。
        /// 顺位：提示员（能从手牌/卡组/墓地拉调整＝单卡起手）→ 唱片师/混音手（检索）→ 旋钮手（追加通召）。
        /// </summary>
        private static readonly int[] PreferredSummons =
        {
            CardId.Announcer,   // 提示员
            CardId.Recordist,   // 唱片师
            CardId.Mixer,       // 混音手
            CardId.Knob,        // 旋钮手
            CardId.Clipper,     // 削波手
        };        /// <summary>
        /// 手坑与陷阱的**时机**：一律走 WindBot 原生执行器给这些卡准备好的判据，
        /// 不再"能发就发"（用户实测"开局丢 G""手坑乱扔"就是这么来的）：
        /// * 增殖的G —— 只在**对手回合**开；灰流丽 —— 只在**连锁对手的卡**时开；
        /// * 幽鬼兔/屋敷童 —— 对手召唤后或连锁对手时；遮蒙者 —— 交给"要无效对面哪只怪"的判据；
        /// * 锁鸟/朔夜时雨/欢聚友伴 —— 只在对手回合、且连锁对手的卡（否则纯白扔）；
        /// * 其余陷阱 —— 对手召唤后或连锁对手时才开。
        /// </summary>
        private bool Activate()
        {
            if (Card != null)
            {
                // **场上阻抗件**：对手在动（连锁对手的卡）时，场上的这些怪要主动发效果——它们原来
                // 没有时机规则，只走通用兜底，实战表现是"完全不干扰"（审计：红印鉴 出场 1 次发动 0 次、
                // 再混音手 1/0；升辉月对局里我方关键位合计只出场 9 次见 docs/interruption-audit.md）。
                if (Duel.LastChainPlayer == 1 && IsAny(Card, InterruptionCards))
                    return true;
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
                if (IsAny(Card, HandTrapChainOnly))
                {
                    // 「小丑与锁鸟」是**对称锁**（卡文 94145022："这个回合，**双方**不能从卡组把卡加入手卡"）：
                    // 自己回合丢它＝把自己这回合的检索（提示员①/混音手①/唱片师①…）全锁死
                    // → 只在对手回合、连锁对手的卡；
                    // 「欢聚友伴」两张（水母/山雀）是"发动后对手每次召唤我就抽 1"，而**发动条件本身只有
                    // "自己场上没有卡"**（`c84192580.lua`/`c42141493.lua` 的 `s.drcon`）——内核在任何时点
                    // 都可能问一次，原来"连锁对手的卡"会在对面还没出怪时白扔。改成**对手这一回合确实召唤过**
                    // 之后才交（`EnemySummonedThisTurn`，见基类；`Duel.LastSummonPlayer` 在连锁开始与阶段
                    // 开始都会被重置成 -1，连锁里判不出来）。
                    if (Card.IsCode(84192580) || Card.IsCode(42141493))
                        return MulcharmyReady();   // 档位见 DefaultExecutor.MulcharmyWaitSummon（A/B：环境变量 MULCHARMY_GATE）
                    if (Card.IsCode(94145022))
                        return Duel.Player == 1 && Duel.LastChainPlayer == 1;
                    // 「朔夜时雨」只废对面一只怪、不伤自己，对手在我们回合特召时也该跟上，任何回合都可以。
                    return Duel.LastChainPlayer == 1;
                }
                if (Card.HasType(CardType.Trap))
                    return DefaultTrap();
                // 现在能从额外卡组出怪 → **先把下位选择让出来**：不再用通用效果/盖牌把自己的回合
                // 填满（"能发就发"会让 AI 在能出额外怪之前先把素材和时机花掉）。
                // 专属规则在前面，早就生效过了，不受这条影响。
                if (ExtraSummonAvailable())
                    return false;
            }
            return DefaultDontChainMyself();
        }

        /// <summary>
        /// 现在能不能从额外卡组出怪：内核把额外怪放进了这回合"可召唤/可特殊召唤"的列表里。
        /// 只在**自己的回合**采信——对手回合那份列表是上一次空闲指令留下的陈数据。
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

        /// <summary>
        /// 场上的关键阻抗件（对手发动效果时优先发动，见 `docs/interruption-audit.md`）：
        /// 红印鉴③ = 除外墓地 1 调整 → 无效场上 1 张卡（本家唯一三色擦）；响度战争② = 复制墓地本家的
        /// 送墓效果当阻抗；锁缚龙 = 康效果；鲜花女男爵 = 泛用无效；再混音手②/削波手 = 对手回合加速同调
        /// （送墓时会触发本家下级的②炸卡）。
        /// </summary>
        private static readonly int[] InterruptionCards =
        {
            CardId.RedStamp,
            CardId.LoudnessWar,
            4891376,                // 锁缚龙 锁镰
            84815190,               // 鲜花女男爵
            CardId.Remixer,
            CardId.CrackleClipper,
            CardId.BackToBack,      // B2B③ 对手发怪兽效果时加速同调（最多 2 次）
        };

        /// <summary>是不是某张卡（空值安全，同一张卡的多个印刷号都算）。</summary>
        private static bool IsSameCard(ClientCard card, int cardId)
        {
            return card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId));
        }

        /// <summary>
        /// 「响度战争②」除外哪只本家当费用（复制它的送墓效果）：音轨制作人②＝对方场上 1 张卡回手（最稳）
        /// → 混音手②＝破坏对方 1 只怪兽 → 唱片师②＝破坏对方 1 张魔陷 → 其余。
        /// </summary>
        private static readonly int[] LoudnessWarCostOrder =
        {
            CardId.TrackMaker, CardId.Mixer, CardId.Recordist, CardId.CrackleClipper, CardId.Knob, CardId.Announcer,
        };

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
        /// <summary>只在"对手回合 + 连锁对手的卡"时才能交的手坑。</summary>
        private static readonly int[] HandTrapChainOnly = { 94145022, 52038441, 84192580, 42141493 };

        /// <summary>
        /// 「欢聚友伴」两张（茸茸长尾山雀 / 抖抖海月水母）的**空场窗口**：这两张手坑的唯一条件是
        /// **自己场上没有卡**（`c42141493.lua:18-20`：`s.drcon = Duel.GetFieldGroupCount(tp,LOCATION_ONFIELD,0)==0`，
        /// `c84192580.lua` 同款），而且**只判发动的那一瞬间**——发动后是"这个回合中，以下效果适用"的
        /// 整回合持续效果，所以"先交它、再铺场"一点都不冲突；反过来说，**先铺场就等于把手里这两张整局作废**。
        ///
        /// 本局实证（`logs/app_20261006_101432.log.jsonl`，起手 10:36:24 的手牌里就有「欢聚友伴·茸茸长尾山雀」）：
        /// 对手回合 10:36:31 发动「码丽丝梦游地下界」，**同一个连锁窗口**里我方手里同时能发「削波手①」
        /// （自跳 + 一次加速同调）与这张欢聚友伴；规则表里 削波手① 排在通用 Activate 规则前面，窗口被
        /// 削波手 拿走 → 10:36:33 削波手 落场 → 我方场上此后再没空过（10:36:50 又特召了「提示员」）→
        /// 欢聚友伴 从起手一直躺到 10:39:54 的手牌打印，整局一次都没发出去。而对手那一回合从
        /// **卡组·额外卡组**特召了 5 次（电子界小男巫 5466／连接解码员 5472／白棋捆绑 5518／
        /// 访问码语者 5674／转码语者 5683）——按卡文这正好是 5 张抽卡，一张都没拿到。
        ///
        /// 判据只认卡文那一句：**自己场上没有卡**（`Bot.GetFieldCount()`＝怪兽区 + 魔陷区，含场地）
        /// ＋对手回合（这两张只认对手的召唤/特召，自己回合丢＝白扔，与 <see cref="Activate"/> 里
        /// <see cref="HandTrapChainOnly"/> 的口径一致）。能不能发、这个窗口对不对由内核与卡脚本判
        /// （`drcost` 还要求它可被丢弃），这里不重复实现、也不另设兜底。
        /// ⚠「PSY骨架装备·γ/δ」**不并进这一条**：它们虽然也要求空场，但触发点绑在"对方把**怪兽**的
        /// 效果发动时"——为了一个未必来的触发点压着 削波手① 不出，可能白白丢掉整次自跳，
        /// 它们的时机仍走下面的通用段（`DefaultDontChainMyself`）。
        /// </summary>
        private bool MulcharmyActivate()
        {
            // 对手回合 + 档位判据（用户 2026-10-07 的 P2 口径：等对手真的开始出怪再交，见
            // `DefaultExecutor.MulcharmyWaitSummon`；A/B 用环境变量 MULCHARMY_GATE=chain 切回旧口径）。
            if (!MulcharmyReady())
                return false;
            return Bot.GetFieldCount() == 0;
        }

        /// <summary>
        /// 额外卡组出哪只的优先级。**候选来自内核**（它给的候选就是"这一回合真的做得出来"的那些），
        /// 我们只是在这堆里挑顺位最高的一只——所以顺位可以按"最想要"排，做不出来的会因为不在候选里而自动跳过。
        ///
        /// 为什么需要它：没有这段时，选额外怪走的是基类"从候选尾部取一只"，很容易挑到当回合做不出来
        /// 的那只——**动作会被内核丢掉**，整回合看着就像"只会通召一下"（用户实测：杀调首回合几乎不做额外
        /// 召唤、卡通一回合只有一个怪、魔女术展开完只有一个）。闪刀之所以一直在爬链接，就是因为
        /// `SkyStrikerShopExecutor` 里有同样的这段。
        ///
        /// 顺位：先出低等级、能接链条的本家同调（音轨制作人检索 → 响度战争 → …），
        /// 再考虑大怪与泛用终端。
        /// </summary>
        private static readonly int[] ExtraDeckPriority =
        {
            CardId.TrackMaker,      // 音轨制作人 Lv4（3+1 出的检索点，教程每条线都从它开始）
            // ⚠ **回滚第一嫌疑人**：按"正确教程"改的那版把「锁缚龙 锁镰」(Lv7) 提到了第二位，
            // 于是只要有 4 星音轨制作人 + 手卡 3 星，AI 就会先出 7 星锁缚龙；而教程的单卡线第二只
            // 明确是 **5 星的「再混音手」**（3+2），锁缚龙只出现在两卡线里。先把 5 星放回去，
            // 用对局数据验证 30% 的下滑是不是它造成的。
            CardId.RedStamp,        // 红印鉴唱片师 Lv5（本职最强终端：等级干扰 + 三色擦）
            CardId.Remixer,         // 再混音手 Lv5（单卡线的第二个终端，对手回合加速同调）
            CardId.LoudnessWar,     // 响度战争 Lv6（保护其他调整 + 用墓地效果当阻抗）
            CardId.CrackleClipper,  // 噼啪削波手 Lv5
            CardId.BackToBack,      // B2B Lv10：回收 + 追加同调（最多 2 次）
            4891376,                // 锁缚龙 锁镰 Lv7（两卡线用）
            84815190,               // 鲜花女男爵 Lv10（泛用无效）
            74586817,               // PSY骨架王·Ω Lv8
            30983281,               // 加速同调星尘龙 Lv8
        };

        /// <summary>
        /// 打谁：**能一击打死就直接打脸**。基类只问"打得过哪只怪"，对面有小怪时会先去撞怪，
        /// 把能一击致胜的那次直击浪费掉。其余交给基类。
        /// </summary>
        public override BattlePhaseAction OnSelectAttackTarget(ClientCard attacker, IList<ClientCard> defenders)
        {
            if (attacker != null && attacker.CanDirectAttack
                && attacker.GetAttackPower() >= Enemy.LifePoints)
            {
                return AI.Attack(attacker, null);
            }
            // 「未知的里侧怪」不拿小怪去撞：基类把里侧怪的数值当 0 来比（DefaultExecutor.OnSelectAttackTarget
            // 的 `attacker.RealPower > defender.RealPower`），于是 100 攻的旋钮手也被判成"打得过"。
            // 实测（迭代第 1 轮 temp/rounds/r1/b95.log）：旋钮手撞里侧怪 → 翻出后我方 LP 4000→2000，
            // 白掉 2000 血、什么都没换到。本家下级攻击力都 ≤1500 → 它们不撞里侧。
            //
            // ⚠ 但**不能一刀切**：里侧怪盖着的时候，全场只有它是目标，若连 3200 攻的终端都不许打，
            // 整局就再也打不出伤害——实测（迭代第 6 轮 temp/rounds/r6/b95.log）对空白墙 43 回合
            // 只有 2 次攻击宣言、最后"没有卡可抽"判负。所以按**攻击力**分档：
            // ≥ 2000 攻的怪照打里侧（最坏也是打死一只未知怪），小怪不打。
            bool strongEnough = attacker != null && attacker.GetAttackPower() >= UnknownSetSafeAttack;
            if (defenders == null)
                return null;
            List<ClientCard> known = new List<ClientCard>();
            foreach (ClientCard defender in defenders)
            {
                if (defender == null || (defender.IsFacedown() && !strongEnough))
                    continue;
                known.Add(defender);
            }
            if (known.Count == 0)
                return null;
            return base.OnSelectAttackTarget(attacker, known);
        }

        /// <summary>
        /// 「金满而谦虚之壶」除外额外卡组时"先牺牲谁"的顺序（越靠前越不心疼）。
        /// 依据是对空白 30 局里各额外怪的出场次数：鲜花女男爵 0、PSY骨架王·Ω 0、加速同调星尘龙 3、
        /// 噼啪削波手 6；而 锁缚龙 13、音轨制作人 11、响度战争 25 都是线的主件。
        /// 第 2 张的重复件放中间（少一张还能打），**一只的终端留到最后**。
        /// </summary>
        private static readonly int[] PotBanOrder =
        {
            84815190,   // 鲜花女男爵（30 局 0 次出场）
            74586817,   // PSY骨架王·Ω（0 次）
            30983281,   // 加速同调星尘龙（3 次）
            39576656,   // 杀手级调整曲·噼啪削波手（6 次）
            CardId.BackToBack,      // B2B（2 张的重复件）
            CardId.RedStamp,        // 红印鉴唱片师
            CardId.LoudnessWar,     // 响度战争
            CardId.TrackMaker,      // 音轨制作人
            CardId.Remixer,         // 再混音手
            CardId.LockDragonLock,  // 锁缚龙 锁镰（终端，最后才动）
        };

        /// <summary>只有一个元素的顺位表：给 <see cref="PickFirstMatching"/> 用来"单独把某只提出来"。</summary>
        private static readonly int[] LockDragonOnly = { CardId.LockDragonLock };

        /// <summary>
        /// 「再混音手②」从自己墓地选 2 只调整（1 只回手 + 1 只特召）时的顺序——**教程第四节的对手回合序列**：
        /// "特召墓地**唱片师** + 回收**旋钮手** → 唱片师+旋钮手 = 音轨制作人（C1 检索提示员 / C2 唱片师检索削波手）"。
        /// 先给唱片师：它被特召出来 ① 还能检索（"3 星以外"＝削波手/混音手）；旋钮手回手当"手卡 1 星调整"，
        /// 和场上的唱片师 3+1 就是下一次加速同调的 4★音轨制作人。
        /// 脚本 `c88170262.lua` 的 `s.sporthGroup` 要求"这 2 只里正好 1 只能特召、1 只能回手"，
        /// 内核的 `SelectSubGroup(...,2,2)` 会把它转成 MSG_SELECT_UNSELECT（WindBot 侧＝反复问
        /// `OnSelectCard(min=0/1, max=1)`，见 Game/GameBehavior.cs 的 OnSelectUnselectCard），
        /// 所以下面这个顺序是**一次给一张**用的；之后内核会再问一次"哪只加入手卡"（hint=ATOHAND）——
        /// 那一步走 <see cref="PreferredPicks"/>，正好是「旋钮手」，与教程一致。
        /// </summary>
        private static readonly int[] RemixerPairOrder = { CardId.Recordist, CardId.Knob };

        /// <summary>
        /// 「B2B③」回收哪只调整（对方发动怪兽效果时，从自己墓地·除外选 1 只 10 星以外的调整）——
        /// **教程第四节原文**："B2B 再拉墓地**唱片师** + 场上削波手 → 5 星红印鉴；第二次拉墓地**音轨制作人**
        /// + 手卡混音手 → 6 星响度战争"。所以顺序是 唱片师 → 音轨制作人 → 其余（都凑不出同调时至少回收一张）。
        /// </summary>
        private static readonly int[] BackToBackPickOrder =
        {
            CardId.Recordist, CardId.TrackMaker, CardId.Announcer, CardId.Mixer, CardId.Knob, CardId.Clipper,
        };

        /// <summary>按顺序挑第一张命中的卡（挑不到返回 null，交回后面的通用逻辑——不猜、不兜底）。</summary>
        private static IList<ClientCard> PickFirstMatching(IList<ClientCard> cards, int[] order)
        {
            foreach (int cardId in order)
            {
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
            return null;
        }

        /// <summary>
        /// 选项选择：**分支型效果一律按"值"匹配，不掷骰子**。基类 `DoEverythingExecutor.OnSelectOption`
        /// 是 `Program.Rand.Next`（掷骰子），登记不上的分支每次都由它随机挑——用户反馈的"乱发效果"
        /// 就是这么来的。场地③「从卡组选 1 只本家怪**加入手卡或特殊召唤**」原来也是 50/50，
        /// 掷错就少一次 ① 检索（实测首回合做不出 5 星的原因之一），所以这里沿用卡通那副的写法：
        /// 两个选项里带 1190（strings.conf：1190＝加入手卡）的那支不选，另一支＝**特殊召唤**。
        ///
        /// 内核给客户端的选项值＝卡脚本里 `aux.Stringid(卡号, k)` 那个字符串**原样**发过来
        /// （＝ `Util.GetStringId(卡号, k)` ＝ `卡号 * 16 + k`，见 :meth:`EncodedOption` / :meth:`OptionValues`）；
        /// 命中哪个值就回**那个值在 `options` 里的下标**（WindBot 的约定是回下标）。
        ///
        /// 为什么不能按下标猜：这些分支表是脚本**动态拼**的（`aux.SelectFromOptions` / 手写 ops 表
        /// 只把"这一支做得出来"的列进去，列表会被压缩），按值匹配时某一支缺席会自动落到下一支
        /// （见 <see cref="PreferredOptionValues"/>）。
        /// </summary>
        public override int OnSelectOption(IList<int> options)
        {
            if (options == null || options.Count == 0)
                return base.OnSelectOption(options);
            // ① 已经"登记"好的分支值（值来自 :meth:`EncodedOption`，**带卡号、按值不按偏移**）：
            //    命中了才清，免得被别的卡的选项请求提前吃掉。
            int registeredHit = _optionValue == 0 ? -1 : options.IndexOf(_optionValue);
            // ② 其余分支卡：按"正在结算（认不出来时＝正在发动）的那张卡"取优先值表
            //    （<see cref="PreferredOptionValues"/>），逐支按值比——命中就用。
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
            // ③ 探针（只在 Debug=true 时打）：选项清单 + 登记值有没有真的出现在里面。
            //    复核口径：想让某张卡走哪一支，就看它的"登记值"**原样出现在** options=[…] 里、
            //    且"命中"是那个值的下标（不是"未命中"）。
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
            // 「金满而谦虚之壶」的两个选项＝**从额外卡组除外 3 张还是 6 张**（脚本 `c84211599.lua`：
            // `op = Duel.SelectOption(...)`，`ct = op == 0 and 3 or 6`，选项 0 就是 3 张）。
            // 原来没管这两支 → 基类 `Rand.Next` 掷骰子，一半的局按 6 张走，等于把额外卡组挖掉 6 张
            // （15 张的卡组里含 锁缚龙/再混音手 这样的终端）。**取 3 张那支**：代价小得多，
            // "翻 3 张拿 1 张"的信息量也够用。
            if (options.Count == 2 && IsSameCard(effect, CardId.PotOfProsperity))
                return 0;
            // 两卡线（教程第八/九节）：场地③ 拿「提示员」时必须选**"加入手卡"那一支**（1190）。
            // 依据是卡文：「提示员①」是"这张卡**召唤**的场合才能发动"——特殊召唤出来的提示员发不了 ①，
            // 而两卡线的下一步正是"提示员① 特召唱片师/混音手"，选错整条线就断。原来这里一律取
            // "特殊召唤"（1152），那是给单卡线的「唱片师」准备的（那张是"召唤·特殊召唤"都能检索）。
            if (PlanActive() && IsMixerLine()
                && options.Count == 2 && options.Contains(OptionAddToHand)
                && IsSameCard(effect, CardId.JukeboxBar))
                return options.IndexOf(OptionAddToHand);
            if (options.Count == 2 && options.Contains(OptionAddToHand))
                return options.IndexOf(OptionAddToHand) == 0 ? 1 : 0;
            return base.OnSelectOption(options);
        }

        /// <summary>
        /// 「加速同调」那一问（`Duel.SelectYesNo`）的时机：**削波手① / 「杀手级调整曲同调」① 的同调都是可选的**
        /// （`c43904702.lua:60-62` 与 `c78058681.lua:33-34`：自跳/发动之后各问一句 `Duel.SelectYesNo`），
        /// 原来这一问落到基类的"一律 yes"（<see cref="DefaultExecutor.OnSelectYesNo"/>）——而这次同调的素材**只能从手卡抓**时
        /// 就会把要留的件烧掉。实测（`temp/train/mb5-95.log`，种子 1500 那一轮，那一轮里同款共十几处）：
        /// * 第 1 局（04:28:23）：手牌「提示员 + 削波手 + 同调 + 决斗者创世纪×2」，削波手在**对手回合**自跳，
        ///   接着 `提示员 from Hand move to Grave` + `削波手 from MonsterZone move to Grave` →
        ///   出 5★「噼啪削波手」——换掉的是这副牌**唯一的单卡动点**（提示员① 从卡组/手卡/墓地拉调整），
        ///   拿到的只有一只 800/2000 的身体（② 随机除外对手额外 1 张），下一个我方回合手牌只剩
        ///   「决斗者创世纪×2 + 旋钮手 + 场地」，整条线从第一步就没了；
        /// * 同款还有 削波手烧「唱片师」（04:28:42 / 04:28:52，「唱片师线」的起手件），
        ///   以及「同调」烧掉**刚检索上手**的「混音手」（04:27:38：`同调 activate effect`
        ///   → `混音手 from Deck move to Hand` → `混音手 from Hand move to Grave`）。
        /// ⚠ 「PSY骨架装备·γ/δ」**不在要留的名单里**（它们本来就不在 <see cref="HandTraps"/>）——卡文
        /// `c74203495.lua:27` 的发动条件写着"自己场上没有怪兽存在"，场上一铺开它就已经是死牌，
        /// 拿去当素材不算浪费。
        /// 判据见 <see cref="AccelSynchroWouldBurnHandPiece"/>：**只有"这次同调不动用手卡"、
        /// 或者手卡里还有"可以烧的调整"时才答应**——手坑（留着打断）与线件（提示员/唱片师/混音手/旋钮手，
        /// 留着下回合展开）都不当加速同调的料。自跳/发动本身已经生效，谢绝这一问只是"不做那次可选同调"，
        /// 身体与手牌都留着。
        /// </summary>
        public override bool OnSelectYesNo(int desc)
        {
            if (desc == Util.GetStringId(CardId.Clipper, 2) || desc == Util.GetStringId(CardId.TuneUp, 1))
                return !AccelSynchroWouldBurnHandPiece();
            return base.OnSelectYesNo(desc);
        }

        /// <summary>
        /// 加速同调"要留的件"：这副牌单卡线/两卡线的起点与关键环（提示员① 拉调整、唱片师① 检索混音手、
        /// 混音手① 检索旋钮手、旋钮手① 追加召唤）——它们当同调素材送去墓地就等于把下个回合的展开烧掉。
        /// </summary>
        private static readonly int[] KeepForLine =
        {
            CardId.Announcer, CardId.Recordist, CardId.Mixer, CardId.Knob,
        };

        /// <summary>
        /// 这次可选加速同调会不会把手卡里的关键件烧掉：场地上的怪自己就能提供 2 只以上素材时不必动用手卡
        /// （放行）；否则第二只素材只能从手卡拿——只有手卡里的调整还剩下"可以烧的"（既不是
        /// <see cref="HandTraps"/>、也不是 <see cref="KeepForLine"/>）才放行，全是该留的就谢绝这一问。
        /// 只用"场上怪数"与"手卡调整"两个集合判断，不做等级运算：真要凑不出等级内核也不会问这一句
        /// （`s.spfilter` 要求额外卡组里存在"现在就能同调召唤"的调整怪）。
        /// </summary>
        private bool AccelSynchroWouldBurnHandPiece()
        {
            // 场上的怪（GetMonsters 已经把空位滤掉了）：≥2 只时素材可以全从场上出。
            if (Bot.GetMonsters().Count >= 2)
                return false;
            // 场上不够 → 第二只素材只能从手卡的调整里拿（本家下级都带"手卡 1 只调整也能作为同调素材"）。
            foreach (ClientCard card in Bot.Hand)
            {
                if (card == null || !card.IsTuner())
                    continue;
                if (IsHandTrap(card) || IsAny(card, KeepForLine))
                    continue;
                return false;   // 手卡里还有可以烧的调整 → 这次加速同调照做
            }
            return true;
        }

        /// <summary>
        /// 选卡：① 额外卡组的候选按 <see cref="ExtraDeckPriority"/> 挑；
        /// ② 候选里**同时有对面和我方的卡**时先选对面的——这副牌的送墓效果（混音手② 破坏怪兽、
        /// 唱片师② 破坏魔陷、音轨制作人② 弹回手卡、红印鉴③ 无效）都要选**对方场上**的卡，
        /// 照基类从尾部取会选到自己。纯我方的候选按 <see cref="PreferredPicks"/> 挑。
        /// </summary>
        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (Duel.Phase == DuelPhase.BattleStart)
                return null;

            // 解放/吃自己场上的调整当费用（**场地③ 解放调整 → 从卡组拉本家**）：教程里解放的就是
            // 「音轨制作人」（它检索完就没别的用途了），所以先吃它；其余按"最不心疼"
            // （token → 手坑/普通件 → **场上的终端/阻抗件排在最后**）挑。
            if (min <= 1 && max >= 1 && hint == HintMsg.Release)
            {
                if (_verbose)
                    Logger.WriteLine("[计划层] 解放候选：" + DescribeCards(cards)
                        + "｜正在结算：" + DescribeCard(CurrentEffectCard()));
                // 两卡线（教程第八/九节）：**这一步解放的是场上的「混音手」**——原文是
                // "场地③ 解放混音手 → 提示员 加入手卡"。场上的音轨制作人后面还要和提示员做锁缚龙，
                // 不能被吃掉；而按下面的"先吃音轨制作人"顺位正好会把线的收尾吃掉。
                if (PlanActive() && IsMixerLine()
                    && IsSameCard(CurrentEffectCard(), CardId.JukeboxBar))
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.Controller == 0 && (card.IsCode(CardId.Mixer) || card.IsOriginalCode(CardId.Mixer)))
                        {
                            IList<ClientCard> releaseMixer = new List<ClientCard>();
                            releaseMixer.Add(card);
                            return releaseMixer;
                        }
                    }
                }
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 0 && card.IsCode(CardId.TrackMaker))
                    {
                        IList<ClientCard> release = new List<ClientCard>();
                        release.Add(card);
                        return release;
                    }
                }
                // ⚠ **场上的终端/阻抗件不能当费用**（<see cref="InterruptionCards"/> 那一档）。这批额外怪的
                // 攻守是刻意的：响度战争 0/3000、音轨制作人 0/2500、噼啪削波手 800/2000——而下面这条
                // "攻最低＝最不心疼"的打分正好把 **0 攻的墙 / 低攻的康** 排在最前面（它们又不在
                // <see cref="PreferredPicks"/> 里、拿不到那 1000 分）。日志实证
                // （`MaiBot/logs/app_20261006_123114.log.jsonl`，第 5 回合 13:01:48）：
                // `[计划层] 解放候选：锁缚龙 锁镰、杀手级调整曲·唱片师` → `(0 's 锁缚龙 锁镰 from MonsterZone
                // move to Grave)`——把场上唯一的 7★「锁缚龙 锁镰」（2800/2100，本家三色康）喂给场地③，
                // 只换来一只 3★「提示员」；第 7 回合同款（13:03:04 解放「响度战争」换「削波手」）。
                // 加一档高于 PreferredPicks（1000）的重罚 ＝ **只在场上没有别的调整时才轮到终端**；
                // 终端之间的先后仍按攻最低（0 攻的墙先走），"先吃音轨制作人"那一步在上面、不受影响。
                ClientCard cheapest = null;
                int cheapestScore = int.MaxValue;
                foreach (ClientCard card in cards)
                {
                    if (card.Controller != 0)
                        continue;
                    int score = (card.HasType(CardType.Token) ? -1000 : 0)
                        + (IsPreferredPick(card) ? 1000 : 0)
                        + (IsAny(card, InterruptionCards) ? 2000 : 0)
                        + System.Math.Max(card.Attack, 0) / 100;
                    if (score < cheapestScore)
                    {
                        cheapestScore = score;
                        cheapest = card;
                    }
                }
                if (cheapest != null)
                {
                    IList<ClientCard> release = new List<ClientCard>();
                    release.Add(cheapest);
                    return release;
                }
            }

            // 计划层/B 类：**场上的阻抗件的 cost**——「红印鉴③」与「响度战争②」都是"从自己墓地除外 1 只调整"，
            // 候选全是我方墓地的调整（hint == Remove）。选哪只直接决定这次阻抗有没有用：
            // * 红印鉴③：纯费用，先送最没用的（手坑、重复件），本家留到最后（复用下面的"最不心疼"打分）。
            // ⚠ **「提示员」② 的"翻对手卡组顶 2 张、把好卡除外"做不了**（2026-10-05 探查后放弃）：
            // 内核把翻开的卡当作**未知卡**送过来（探针实测 `候选卡号=0、0`，连名字都没有），
            // 所以"哪张是好卡"无从判断——只有能看到翻开牌的人类玩家才能这么打。
            // 这条路留给"以后内核/WindBot 支持了再加"，现在不写死规则（写了也是随机）。
            // （② 后面那一步"剩下的那张回卡组**最上面 or 最下面**"是**选项**，不在这里选卡，
            //   已经登记在 <see cref="PreferredOptionValues"/> 里——当前取"放最下面"，判不了好坏的结论同上。）
            if (min <= 1 && max >= 1 && hint == HintMsg.Remove && HasNoEnemyCard(cards))
            {
                ClientCard effect = CurrentEffectCard();
                if (IsSameCard(effect, CardId.LoudnessWar))
                {
                    if (_verbose)
                        Logger.WriteLine("[B类] 响度战争② cost：候选=" + DescribeCards(cards));
                    foreach (int cardId in LoudnessWarCostOrder)
                    {
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
                // 红印鉴③（或响度战争没找到合适的）：按"最不心疼"挑——手坑/非关键件优先（攻最低的次之）。
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
                    if (_verbose)
                        Logger.WriteLine("[B类] 除外墓地的调整当费用：" + DescribeCard(cheapest)
                            + "｜正在结算：" + DescribeCard(effect));
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(cheapest);
                    return picked;
                }
            }

            // 「金满而谦虚之壶」的 cost：**从额外卡组挑 3 张里侧除外**。这一步原来落到基类的通用选卡
            // （从尾部随机取），实测 30 局里有 3 次把「再混音手」除外掉——那一步是整个线的 5 星终端，
            // 整局当场作废。这里按"最不心疼"的顺序挑：先用从来没进过场的泛用件（鲜花/PSY/星尘龙）、
            // 再用第 2 张的重复件，**一只的终端（锁缚龙/再混音手）留到最后**。
            if (min >= 2 && hint == HintMsg.Remove && AllInExtraDeck(cards))
            {
                IList<ClientCard> banished = new List<ClientCard>();
                foreach (int cardId in PotBanOrder)
                {
                    if (banished.Count >= min)
                        break;
                    foreach (ClientCard card in cards)
                    {
                        if (banished.Count >= min)
                            break;
                        if (banished.Contains(card))
                            continue;
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                            banished.Add(card);
                    }
                }
                if (banished.Count >= min)
                {
                    if (_verbose)
                        Logger.WriteLine("[壶] 除外额外卡组：" + DescribeCards(banished));
                    return banished;
                }
            }

            // 对手回合的两处"选谁"（教程第三节/第四节；也是 docs/interruption-audit.md 的 B 类施工单
            // "再混音手② / 削波手｜对手主要阶段｜加速同调时选哪只"）：
            // * 「再混音手②」：教程第四节＝"特召墓地**唱片师** + 回收**旋钮手**"（见 RemixerPairOrder）。
            //   排除 hint==ATOHAND：那是脚本紧接着问的"哪只加入手卡"，不能在这里抢答成唱片师
            //   （唱片师要留着被特召，回手的应该是旋钮手）。
            // * 「B2B③」：教程第四节"再拉墓地唱片师 → 5 星红印鉴；第二次拉墓地音轨制作人 → 6 星响度战争"
            //   （见 BackToBackPickOrder）。
            // 两张都只在"正在结算的就是它"时接管，判不出来一律交回后面的通用逻辑。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                ClientCard solving = CurrentEffectCard();
                if (IsSameCard(solving, CardId.Remixer) && hint != HintMsg.AddToHand && hint != HintMsg.ToDeck)
                {
                    IList<ClientCard> pair = PickFirstMatching(cards, RemixerPairOrder);
                    if (pair != null)
                        return pair;
                }
                if (IsSameCard(solving, CardId.BackToBack))
                {
                    IList<ClientCard> recycle = PickFirstMatching(cards, BackToBackPickOrder);
                    if (recycle != null)
                        return recycle;
                }
            }

            // ① 额外卡组：从内核给的候选里按顺位挑一只（挑不到就交给下面的通用逻辑）。
            //    走线时**先把「再混音手」挑出来**——教程每条单卡线的 5 星终端，别的顺位先让路。
            if (min <= 1 && max >= 1 && AllInExtraDeck(cards))
            {
                // 两卡线（教程第七/八节）：**锁缚龙要在自己回合就出**。教程第四节的原文是
                // "（两卡线）自己回合就出锁缚龙 → 再混音手② 不会被对手 C2「墓穴的指名者」无效，
                // 因为 C3 能用锁缚龙康掉"——所以这两条线里 7 星锁缚龙的顺位在 5 星再混音手之前。
                if (_planLine == PlanLine.Knob3 || _planLine == PlanLine.Mixer3)
                {
                    IList<ClientCard> lockDragonFirst = PickFirstMatching(cards, LockDragonOnly);
                    if (lockDragonFirst != null)
                        return lockDragonFirst;
                }
                // 对手回合（教程第三节/第四节）：削波手那条加速同调的落点是
                // "用场上音轨制作人 + 手卡提示员加速同调 **7 星「锁缚龙 锁镰」**"——
                // 对手回合的额外怪挑选把锁缚龙提到最前（锁缚龙② 是能连锁对手 C1 的康，
                // 社区口径也是"合格先手＝锁缚龙 1 康"）。**固定顺位 ExtraDeckPriority 一个字不动**：
                // 上一轮把它提到固定第 2 位，交叉战绩 48%→30%（docs/plan-layer.md 的"已知坑"），
                // 所以这里只做"对手回合"这一层临时提升。
                if (Duel.Player == 1)
                {
                    IList<ClientCard> lockDragonDefense = PickFirstMatching(cards, LockDragonOnly);
                    if (lockDragonDefense != null)
                        return lockDragonDefense;
                }
                if (PlanActive() && !BotHasOnField(CardId.Remixer))
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(CardId.Remixer) || card.IsOriginalCode(CardId.Remixer))
                        {
                            IList<ClientCard> remixed = new List<ClientCard>();
                            remixed.Add(card);
                            return remixed;
                        }
                    }
                }
                // 线路主件（再混音手）已经站场之后，**把「锁缚龙」提到最前**：社区公认它是这副牌的
                // 核心三色康（"合格先手＝锁缚龙 1 康 + 重混/响度战争 1 个加速干扰"）。
                // 只在"计划已收尾"时提，不动固定顺位（上一次盲目改固定顺位，交叉战绩 48%→30%）。
                if (PlanActive() && BotHasOnField(CardId.Remixer))
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(4891376) || card.IsOriginalCode(4891376))
                        {
                            IList<ClientCard> lockDragon = new List<ClientCard>();
                            lockDragon.Add(card);
                            return lockDragon;
                        }
                    }
                }
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

            // ② 走线时的检索/拉人：按"这条线的下一步还缺哪一环"挑（用正在结算的卡区分是哪个效果）。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                int[] planOrder = PlanPickOrder();
                if (planOrder != null)
                {
                    ClientCard effect = CurrentEffectCard();
                    if (effect != null && (effect.IsCode(CardId.Recordist) || effect.IsOriginalCode(CardId.Recordist)))
                        ++_planRecordistSearches;   // 唱片师① 结算过一次（下一次要换检索目标，见 PlanPickOrder）
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

            // 「破坏魔法·陷阱（0～N 张）」这类**可选**破坏：候选里只有我方的卡时一张都不选
            //（基类从尾部挑＝自己炸自己）。
            if (min == 0 && hint == HintMsg.Destroy && HasNoEnemyCard(cards))
                return new List<ClientCard>();

            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                foreach (int cardId in PreferredPicks)
                {
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

            List<ClientCard> theirs = new List<ClientCard>();
            List<ClientCard> mine = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
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

        /// <summary>
        /// 同调素材的选择。这副牌的核心机制是**手牌里的调整也能当同调素材**（本家下级都写着这一句），
        /// 而且下级／本家怪**作为同调素材送去墓地时会触发效果**——所以素材不能随便凑：
        /// * 优先用**手牌的调整**（场上的怪留着继续做场）；
        /// * 其次用**本家怪**（送墓有效果）；
        /// * 素材数量越少越省。
        ///
        /// 基类这里返回 null＝完全按内核给的顺序拿，等于把整条链交给运气（用户实测"一回合只做出两张"）。
        /// 只在**算得出精确等级组合**时才回话，算不出来就交回基类（不会把原来的行为改坏）。
        /// </summary>
        public override IList<ClientCard> OnSelectSynchroMaterial(IList<ClientCard> cards,
            IList<ClientCard> mandatoryCards, int sum, int min, int max)
        {
            if (cards == null || cards.Count == 0)
                return null;
            if (sum <= 0)
            {
                // "等级和"已经由内核定死的那条提示（`HintMsg.SynchroMaterial` 走 `GameAI` 的 OnSelectCard
                // 分支时 sum=0）：没有组合可枚举，按同一套打分挑最省心的 `min` 张。
                // ⚠ 这条路径以前是**一律 return null 交给基类**——基类按内核给的顺序拿，
                // 正好把「幽鬼兔 / 屋敷童」当 3 星素材垫进去：2026-10-07 群友实测的三次手坑被烧
                // （`MaiBot/logs/app_20261007_211344.log.jsonl` 21:59:07 / 21:59:27 / 21:59:28）
                // 就是从这儿走的，而 sum>0 那条路径的打分（下面的 -10）根本没机会跑。
                return PickMaterialsByScore(cards, min);
            }

            List<List<ClientCard>> combos = Util.GetSynchroMaterials(cards, sum, 1, 0, true, true);
            if (combos.Count == 0)
                return null;

            List<ClientCard> best = null;
            int bestScore = int.MinValue;
            foreach (List<ClientCard> combo in combos)
            {
                int score = -combo.Count;               // 素材越少越省
                foreach (ClientCard card in combo)
                    score += MaterialScore(card);
                if (score > bestScore)
                {
                    bestScore = score;
                    best = combo;
                }
            }
            if (best == null || best.Count < min || (max > 0 && best.Count > max))
                return null;
            // 约定：mandatory 由内核自己拼，这里只回"我们额外挑的"那些（见 GameAI.IsValidSumSelection）
            return best;
        }

        /// <summary>
        /// 单张素材的"省心分"（越高越优先）：场上的本家件优先、手坑垫底，走线时还要避开该留的牌。
        ///
        /// **手坑的罚分分两档**（2026-10-07 群友实测口径："拿一只当素材没问题，全用掉就难崩"）：
        /// * 用掉它**不会**让手牌清空 → ‑10（原来只有这一档）；
        /// * 用完就**一张手坑都不剩** → ‑10000（实战意义上是"别的素材都能凑就别动它"）。
        /// </summary>
        private int MaterialScore(ClientCard card)
        {
            int score = 0;
            if (card.Location == CardLocation.Hand)
            {
                // **手坑不能当同调素材**：教程里它们是留着（对手回合还能被「再混音手」回收）的阻抗，
                // 不是同调的肥料。原来一律给 +5，实测（提示员线第 1 回合）「场上的提示员 3★ +
                // 手牌幽鬼兔 3★」比「提示员 3★ + 场上的旋钮手 1★」高 2 分 → 烧掉幽鬼兔做了 6★
                // 响度战争，该出的 4★「音轨制作人」（① 检索场地，教程每条单卡线都从它开始）
                // 没了，整条线从第 2 步就断。
                if (IsHandTrap(card))
                    score += HandTrapsInHand() <= 1 ? -10000 : -10;
                else
                    score += 5;
            }
            if (IsArchetypeCard(card))
                score += 3;                     // 本家：送墓会触发效果
            // 走线时把"这条线要留的东西"也计进去：**场上的「唱片师」别当素材**（教程要的是
            // "混音手（场上）+ 3 星（手卡）"那一次，唱片师留着站场），**场上的「混音手」优先当素材**
            // （它是 5 星的指定素材）。不加这两条时两种组合同分，枚举顺序一偏就把唱片师吃掉。
            if (PlanActive() && card.Controller == 0 && card.Location == CardLocation.MonsterZone)
            {
                if (card.IsCode(CardId.Recordist) || card.IsOriginalCode(CardId.Recordist))
                    score -= 4;
                if (card.IsCode(CardId.Mixer) || card.IsOriginalCode(CardId.Mixer))
                    score += 3;
            }
            return score;
        }

        /// <summary>
        /// 从候选里按"省心分"挑 `count` 张（`sum<=0` 那条提示用：内核只给了候选与张数）。
        /// **以内核给的顺序为基线**（越靠前越优先）——候选里没有手坑时，结果与基类
        /// `GetSelectedCards()` 完全一致，只有"手坑垫在后面"这一处差别，改动面最小。
        /// </summary>
        private IList<ClientCard> PickMaterialsByScore(IList<ClientCard> cards, int count)
        {
            if (count <= 0 || cards.Count < count)
                return null;
            List<KeyValuePair<int, ClientCard>> pool = new List<KeyValuePair<int, ClientCard>>();
            for (int i = 0; i < cards.Count; ++i)
                pool.Add(new KeyValuePair<int, ClientCard>(-i + MaterialScore(cards[i]), cards[i]));
            pool.Sort((left, right) => right.Key.CompareTo(left.Key));
            List<ClientCard> picked = new List<ClientCard>();
            for (int i = 0; i < count; ++i)
                picked.Add(pool[i].Value);
            return picked;
        }

        /// <summary>这张卡是不是手坑（<see cref="HandTraps"/> 里那些）——它们不做同调素材。</summary>
        private static bool IsHandTrap(ClientCard card)
        {
            return IsAny(card, HandTraps);
        }

        /// <summary>手牌里还剩几张手坑——这是对手回合的阻抗，不是同调肥料。</summary>
        private int HandTrapsInHand()
        {
            int count = 0;
            foreach (ClientCard card in Bot.Hand)
            {
                if (IsHandTrap(card))
                    count++;
            }
            return count;
        }

        /// <summary>这批素材里会烧掉几张手坑（只数**手卡里**的，场上的手坑已经不算阻抗了）。</summary>
        private static int HandTrapsUsed(IEnumerable<ClientCard> materials)
        {
            int count = 0;
            foreach (ClientCard card in materials)
            {
                if (card != null && card.Location == CardLocation.Hand && IsHandTrap(card))
                    count++;
            }
            return count;
        }

        /// <summary>
        /// 不烧手坑的话，这个等级凑得出来吗？
        ///
        /// 只看"等级和"，**不校验调整 / 同调素材那些具体条件**——判不准时宁可回答"凑得出来"：
        /// 那样只是维持原来"能做就做"的行为，不会凭空少做场（宁放过、不误杀）。
        /// 素材池＝我方场上的怪 + 手卡里**不是手坑**的调整（卡文写着"场上的这张卡为素材作同调召唤的场合，
        /// 手卡 1 只调整也能作为同调素材"，所以手卡调整要算；手坑不算——它们正是不想动的那批）。
        /// </summary>
        private bool CanReachLevelWithoutHandTrap(int level)
        {
            List<int> pool = new List<int>();
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.Controller == 0 && card.Level > 0)
                    pool.Add(card.Level);
            }
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.HasType(CardType.Tuner) && !IsHandTrap(card) && card.Level > 0)
                    pool.Add(card.Level);
            }
            int count = pool.Count;
            if (count > 16)
                return true;        // 素材太多就不枚举了（这副牌不可能出现）
            for (int mask = 1; mask < (1 << count); ++mask)
            {
                int total = 0;
                for (int i = 0; i < count; ++i)
                {
                    if ((mask & (1 << i)) != 0)
                        total += pool[i];
                }
                if (total == level)
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 额外卡组召唤的"手坑预算"闸门：**别把最后一张手坑做没**。
        ///
        /// 用户口径（2026-10-07）："两只兔子做场拿一只当素材用是没有问题的，但全用掉就有点难崩了。"
        /// 判据（只在**自己回合**、且**不是斩杀回**时生效）：
        /// * 手上 ≥2 张手坑 → 放行（用掉一张还剩得下）；
        /// * 手上 0 张手坑 → 放行（没得保）；
        /// * 只剩 1 张、而这次召唤**只能**靠手坑凑等级 → 否掉，留着它打断对面；
        /// * 只剩 1 张、但有别的素材凑得出来 → 放行（<see cref="OnSelectSynchroMaterial"/> 的打分会挑别的）。
        ///
        /// 为什么要它（实测日志 `MaiBot/logs/app_20261007_211344.log.jsonl` 21:58-21:59）：起手
        /// 幽鬼兔×2 + 屋敷童，一回合连做 音轨制作人 → 再混音手 → 锁缚龙 三次同调，把三张手坑
        /// **全部**当素材烧掉（`(0 's 幽鬼兔 from Hand move to Grave)` ×2、`(0 's 屋敷童 …)`），
        /// 场上只剩锁缚龙、手里一张阻抗都没有，对面下一回合直接斩杀。
        /// </summary>
        private bool AllowExtraSummonKeepingHandTrap()
        {
            if (Card == null || Card.Location != CardLocation.Extra || Card.Level <= 0)
                return true;
            if (Duel.Player != 0)
                return true;                    // 对手回合的加速同调不在这里省（那是阻抗本身）
            if (HandTrapsInHand() != 1)
                return true;                    // 0 张没得保；≥2 张用掉一张还剩得下
            if (LethalAvailable())
                return true;                    // 能斩杀就别省了
            bool blocked = !CanReachLevelWithoutHandTrap(Card.Level);
            if (blocked && _verbose)
                Logger.WriteLine("[手坑预算] 不放行「" + Card.Name + "」（Lv" + Card.Level
                    + "）：只能用最后一张手坑凑素材，留着打断对面");
            return !blocked;
        }

        /// <summary>是不是这副牌的本家怪（用执行器里登记过的卡号判断）。</summary>
        private static bool IsArchetypeCard(ClientCard card)
        {
            int[] archetype =
            {
                CardId.JukeboxBar, CardId.Announcer, CardId.Mixer, CardId.Knob, CardId.Clipper,
                CardId.Recordist, CardId.TrackMaker, CardId.LoudnessWar, CardId.BackToBack,
                CardId.Remixer, CardId.RedStamp, CardId.CrackleClipper,
            };
            return IsAny(card, archetype);
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
    }
}
