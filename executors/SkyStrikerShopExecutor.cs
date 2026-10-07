using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「闪刀姬」——按操作方给的 202607 复刻教程写的决策层（配合群友投稿的 #96 卡表）。
    ///
    /// **这一层只排战术优先级**：合法性内核判、条件与自肃卡脚本声明（和另外几份执行器同一套分工）。
    /// 类名带 Shop 后缀是为了和 WindBot 上游自带的 <c>SkyStrikerExecutor</c> 区分开，
    /// 两套可以同场对照（上游那套走 <c>Deck=SkyStriker</c>）。
    ///
    /// 闪刀姬有两条"不是选牌而是站位/顺序"的硬约束，决策层必须替它守住：
    /// 1. **主怪兽区要尽量空着**。「交闪」「大黄蜂」「抓锚」「连刀」这些「闪刀」魔法都写着
    ///    "自己的主怪兽区没有怪兽存在的场合"才能发动 —— 所以链接怪要**优先放额外怪兽区**，
    ///    主区只是「零衣」通召后马上去用作链接素材的过路站。本类构造里把
    ///    <see cref="GameAI.PreferExtraMonsterZone"/> 打开就是这个用途（和升辉月同一套开关）。
    /// 2. **链接爬升的顺序**：零衣 → 飒天（打脸/堆墓）→ 主要阶段 2 再变燎里（回收墓地「闪刀」魔法）
    ///    或雫空（结束阶段检索）——回收/检索优先于继续铺场，所以额外卡组的选择顺序
    ///    按 <see cref="ExtraDeckPriority"/> 来，而不是基类从候选尾部随手拿。
    ///
    /// 其余打法：交闪先拿大黄蜂（生成 token 做链接素材）再拿连刀/抓锚；抓锚要夺对方怪兽，
    /// 属于"候选里优先选对面"的那一类；手坑一律留在手里（见 <see cref="HandTraps"/>）。
    /// </summary>
    [Deck("SkyStrikerShop", "AI_SkyStrikerShop")]
    class SkyStrikerShopExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            public const int Engage = 63166095;          // 交闪（闪刀起动－エンゲージ）
            public const int EngageAlt = 63166096;
            public const int Raye = 26077387;            // 零衣
            public const int RayeAlt1 = 26077388;
            public const int RayeAlt2 = 26077389;
            public const int Roze = 37351133;            // 露世
            public const int RozeAlt = 37351134;
            public const int HornetDrones = 52340444;    // 大黄蜂（闪刀机－ホーネットドローン）
            public const int WidowAnchor = 98338152;     // 抓锚（闪刀机－ウィドウアンカー）
            public const int Linkage = 9726840;          // 连刀（闪刀机－リンケージ）
            public const int MultiRole = 24010609;       // 多任务战刀机（闪刀机构－マルチロール）
            public const int LinkageGate = 34433770;     // 双纽闪门（闪刀起动－リンク）
            public const int Kaina = 12421694;           // 魁奈
            public const int Kagari = 63288573;          // 燎里（火刀：回收墓地「闪刀」魔法）
            public const int KagariAlt = 63288574;
            public const int Hayate = 8491308;           // 飒天（风刀：打脸 + 堆墓 3）
            public const int Shizuku = 90673289;         // 雫空（水刀：结束阶段检索）
            public const int Camellia = 63013339;        // 卡米丽娅
            public const int Azalea = 98462037;          // 阿泽莉亚
            public const int Zeke = 75147529;            // 泽克
            // ⚠ 这里原来是 80538047（「绚岚之风神」）——被当成了"雫空的另一个印刷号"，
            //   结果 雫空 的另一印刷 90673288 根本没人认，风神的效果却被当成雫空② 处理。
            public const int ShizukuAlt = 90673288;      // 雫空（另一印刷号）
            // ↓ 按 202605 闪刀教程补的：教程的核心是"优先在场上做出 LINK2「零露」"，
            //   而她原来**根本不在我的额外卡组优先级里**（等于引擎随便挑）。
            public const int Kaina0 = 76072561;          // 闪刀姬=零露（LINK2 光刀：①检索闪刀魔法 ②解放→拉零衣+露世并炸1）
            public const int JammingWave = 20508881;     // 绚岚之见神（抽2丢1速攻＝快速凑三魔；②拿旋风）
            public const int AlbazAndEcclesia = 30271097; // 落胤与圣女（炸1 + 堆烙印龙白界龙凑三魔，回合结束盖回来当阻坑）
            public const int TripleTacticsThrust = 35269904; // 三战之号（基准号；盖通常魔法/陷阱；后攻检索交闪补点）
            // ⚠ 卡表（闪刀 shop 牌组）用的是**另一印刷号 35269905**（alias＝35269904）；
            //   **两个印刷号都认**：常量与下面的 AddExecutor 都登记两个号
            //   （只登记基准号时，卡表里那张匹配不上、这条专属规则整条落空）。
            public const int TripleTacticsThrustAlt = 35269905; // 三战之号（卡表里用的是这个印刷号）
            public const int MysticalSpaceTyphoon = 5318639; // 旋风（卡表里有，交给它的时机判据）
            // ↓ 卡表里剩下的"系统外"（教程第四节的绚岚引擎 + 壶/增援/圣冠等）。它们原来**一张都没接线**，
            //   全落进 ⑤ 那条通用兜底：兜底里的"现在能从额外卡组出怪 → 让路"会把它们全按住
            //   （和升辉月「白色幻兽」不发效果同一个坑），所以能用的几张要在下面单独点名。
            public const int Sensou = 67115133;          // 绚岚之献咏（速攻：检索 4 星以下「绚岚」怪 / 旋风）
            public const int Davy = 54143349;            // 绚岚之达维（3 星，自身特召条件宽松，② 检索「绚岚」怪/旋风）
            public const int Fujin = 80538047;           // 绚岚之风神（3 星，② 检索「绚岚」魔陷/旋风）
            public const int Desires = 35261759;         // 强欲而贪欲之壶（里侧除外 10 张 → 抽 2）
            public const int Reinforcement = 32807848;   // 增援（检索 4 星以下战士族＝零衣/露世）
            public const int ForbiddenCrown = 98829635;  // 禁忌的圣冠（速攻：无效 + 不能攻击/不能当素材）
            public const int Moonshadow = 38817295;      // 月女神的至天（速攻：无效对面怪兽效果）
            public const int TripleTacticsTalent = 25311006; // 三战之才（对面发过怪兽效果时：抽2/夺怪/看手牌）

            // ===== 额外卡组里"体检一直报没登记"的三张（2026-10-06 核过卡表，写在这里免得每轮都报）=====
            // * 黑龙之艾克莉西娅 78397661（L8 同调）：要"调整 ＋ 4★"或"调整 ＋ 其它"凑 8 星，
            //   这副牌唯一的调整是「灰流丽」3★，而主牌的怪是 1~4★ 闪刀姬——**凑不出 5★** → 不可达。
            // * 烙印龍 阿爾比昂 87746184 / 吞食圣痕之龙 76666602（L8 融合）：这副牌没有融合手段，
            //   它们只会被「落胤与圣女」① 当**额外牌组的费用**送进墓地（脚本要求"有「阿不思的落胤」
            //   卡名记述"）——所以它们的作用是"喂给落胤与圣女"，不是上场。
            public const int Ecclesia = 78397661;
            public const int Albion = 87746184;
            public const int DevouringDragon = 76666602;
        }

        public SkyStrikerShopExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // 闪刀姬的魔法全都要求主怪兽区空着 —— 链接怪优先放额外怪兽区，主区保持空。
            GameAI.PreferExtraMonsterZone = true;

            // ① 先摘掉基类那几条笼统规则（见 ⑤），换成带护栏的版本。**必须先摘再加自己的**——
            //    基类的 `SpSummon`/`SpellSet` 是"无卡号、无条件"的兜底：`ShouldExecute` 里
            //    `exec.Func == null` 直接算通过，而它们在 Executors 里的位置比本类所有规则都靠前
            //    （基类构造先跑），于是每次主要阶段都会先做连接召唤/先盖一张卡，
            //    把「交闪 → 连刀 → 变身」这一串"发动"整条顶掉（实测第 1 回合终场是"零露 + 连刀盖着"）。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                CardExecutor exec = Executors[i];
                if (exec.CardId != -1)
                    continue;
                if (exec.Type == ExecutorType.SummonOrSet || exec.Type == ExecutorType.Activate
                    || exec.Type == ExecutorType.SpSummon || exec.Type == ExecutorType.SpellSet
                    || exec.Type == ExecutorType.Repos)
                    Executors.RemoveAt(i);
            }

            // ② 检索与展开：交闪、零衣、露世、大黄蜂、连刀
            AddExecutor(ExecutorType.Activate, CardId.Engage, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.EngageAlt, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Raye, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.RayeAlt1, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.RayeAlt2, AlwaysPlay);
            // 「露世」：① 从手牌往主怪兽区跳（有讲究，见 RozeEffect），② 从墓地复活则能用就用
            AddExecutor(ExecutorType.Activate, CardId.Roze, RozeEffect);
            AddExecutor(ExecutorType.Activate, CardId.RozeAlt, RozeEffect);
            AddExecutor(ExecutorType.Activate, CardId.HornetDrones, AlwaysPlay);
            // 「增援」：检索 4 星以下战士族 = 零衣/露世，等于第二张开局（兜底会把它的发动按掉）
            AddExecutor(ExecutorType.Activate, CardId.Reinforcement, AlwaysPlay);
            // 「强欲而贪欲之壶」：里侧除外 10 张换抽 2。⚠ 会把卡组里的零衣/交闪/连刀一起除外，
            //   所以只在"手上已经有起手件"时才开（见 DesiresEffect）。
            AddExecutor(ExecutorType.Activate, CardId.Desires, DesiresEffect);
            // 连刀有两个"时机"讲究（教程第二、三节），不能无脑发，所以单独写一条规则
            // 「连刀」：走线时**盖放收尾**（教程终场是"零露 + 盖双纽闪门/连刀"），不在自己主阶段先花掉。
            AddExecutor(ExecutorType.Activate, CardId.Linkage, LinkagePlanAware);

            // ③ 干扰与回收：抓锚（夺怪）、多任务战刀机、双纽闪门
            // 「抓锚」只在**对面场上有能无效的效果怪兽**时才发（见 WidowAnchorEffect）——
            // 卡脚本把可选目标写成了双方场上，无脑放行会拿自己的怪当靶子。
            AddExecutor(ExecutorType.Activate, CardId.WidowAnchor, WidowAnchorEffect);
            // 「多任务战刀机」① 的对象是"自己场上1张其他卡"（**作为代价送墓**），无条件放行会把刚做出来的
            // 链接怪送掉；② 是结束阶段从墓地重新盖放本回合用过的「闪刀」魔法（续航），能用就用。
            AddExecutor(ExecutorType.Activate, CardId.MultiRole, MultiRoleEffect);
            AddExecutor(ExecutorType.Activate, CardId.LinkageGate, LinkageGateEffect);

            // ③b 教程"投入绚岚之见神/落胤与圣女/三战之号"那一节的四张——它们的共同作用是
            // **快速凑三魔**（墓地 3 张以上魔法时，抓锚能夺怪、交闪能多抽 1），能发就发。
            // ⚠ 这几张都是"分支型效果"（绚岚见神/献咏、落胤与圣女、三战之才…），分支由 OnSelectOption
            //   选：基类 DoEverythingExecutor 那里是 `Rand.Next`——**掷骰子**，会掷出"检索旋风却没旋风
            //   可炸""把整手卡丢掉"这种结果，所以下面给每张登记了想要的支（见 PreferredOptionValues）。
            AddExecutor(ExecutorType.Activate, CardId.JammingWave, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Sensou, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.AlbazAndEcclesia, AlbazEffect);
            AddExecutor(ExecutorType.Activate, CardId.TripleTacticsThrust, AlwaysPlay);
            // ⚠ 卡表里的「三战之号」是另一印刷号 35269905：**两个印刷号都认**，缺了这条专属规则会落空。
            AddExecutor(ExecutorType.Activate, CardId.TripleTacticsThrustAlt, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleTacticsTalent, AlwaysPlay);
            // 绚岚的 3 星（达维/风神）：自身特召条件宽松（墓地有旋风 或 对面没魔陷），
            // 出场还有一个检索，属于白赚的身位——**但不能在"主怪兽区必须空着"的时候上**
            //（它们进的是主区，会把「交闪/连刀」这些要空主区的魔法整轮锁掉，实测整条线停在燎里回收交闪之后）。
            AddExecutor(ExecutorType.SpSummon, CardId.Davy, SensouBodyEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.Fujin, SensouBodyEffect);
            AddExecutor(ExecutorType.Activate, CardId.Davy, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Fujin, AlwaysPlay);
            // 「禁忌的圣冠」「月女神的至天」：速攻，防守用（见 SaintCrown / Moonshadow）。
            AddExecutor(ExecutorType.Activate, CardId.ForbiddenCrown, ForbiddenCrownEffect);
            AddExecutor(ExecutorType.Activate, CardId.Moonshadow, MoonshadowEffect);
            // 零露：①（检索闪刀魔法）与②（解放自身 → 拉零衣+露世并炸 1 张）同名一回合只能用一个，
            // 哪个回合该要哪一个见 Kaina0Effect。
            AddExecutor(ExecutorType.Activate, CardId.Kaina0, Kaina0Effect);

            // ④ 额外卡组的链接怪效果（燎里回收、雫空检索、飒天堆墓、泽克/阿泽莉亚的解场）
            AddExecutor(ExecutorType.Activate, CardId.Kagari, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.KagariAlt, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Shizuku, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Hayate, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Camellia, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Azalea, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Zeke, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Kaina, AlwaysPlay);

            // ⑤ 通用兜底（**顺序就是优先级**）：通召 → 通用发动 → 额外卡组的连接召唤 → 改变表示形式
            //    → 盖放。连接召唤刻意排在"发动"之后：反过来的话每次主要阶段都先做连接召唤，
            //    「交闪 → 连刀 →（变身）燎里 → 雫空」这条链一步都走不出来（基类兜底原来就在那个位置）。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));
            Executors.Add(new CardExecutor(ExecutorType.SpSummon, -1, AllowExtraSummon));
            Executors.Add(new CardExecutor(ExecutorType.Repos, -1, DefaultMonsterRepos));
            // 盖放放最后：能发的先发。两张"终场要盖着"的卡给专门的出口，其余交给基类判据
            //（速攻/陷阱才盖）。注意一套牌每张卡只能有一个 SpellSet 出口，别在前面重复登记。
            Executors.Add(new CardExecutor(ExecutorType.SpellSet, CardId.Linkage, LinkageSpellSet));
            Executors.Add(new CardExecutor(ExecutorType.SpellSet, CardId.LinkageGate, LinkageGateSpellSet));
            Executors.Add(new CardExecutor(ExecutorType.SpellSet, -1, DefaultSpellSet));

            // 斩杀优先：插到最前面（内核的动作循环是"外层遍历规则"，排前面才会先被问到）。
            Executors.Insert(0, new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));
        }

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>
        /// 「闪刀起动-连刀」的两个时机讲究（教程第二、三节）：
        /// * **变身前先打**：对面场上没怪、我们能直接攻击时，别在自己的主阶段 1 就把连刀用掉——
        ///   留到战斗阶段/主阶段 2 再变身，那一轮就能多打一次（教程"靠在战斗阶段让怪兽反复变身来斩杀"）；
        /// * **躲取对象效果**：对手发动效果时连锁连刀，把场上的「闪刀姬」连接怪兽送去墓地换成另一只，
        ///   原效果因失去对象而不处理。
        /// </summary>
        // ============================================================ 计划层（见插件 docs/plan-layer.md）

        /// <summary>
        /// 本回合走哪条起手线（教程第六～九节的 1～2 卡线，终场＝**闪刀姬=零露 + 盖放连刀/双纽闪门**）：
        /// * 零衣线：NS 零衣 → ① 解放自己从额外卡组拉「闪刀姬」连接怪（燎里）→ 露世① 跟跳 →
        ///   燎里+露世 = 零露 → 零露① 检索「连刀」→ **盖放**；
        /// * 交闪线：交闪（主要怪兽区空时）检索「零衣」→ 之后同上。
        /// </summary>
        private enum PlanLine
        {
            None,
            Raye,       // 手牌有「闪刀姬-零衣」
            Engage,     // 手牌有「闪刀起动-交闪」（先拿零衣）
        }

        private PlanLine _planLine = PlanLine.None;

        /// <summary>本回合的起手线算过了没有（每回合只算一次：手牌会因为检索/召唤不断变化，反复判会把线判丢）。</summary>
        private int _planLineTurn = -1;

        /// <summary>这份计划是给第几回合定的（Duel.Turn）。</summary>
        private int _planTurn = -1;

        /// <summary>上一次清"本回合记录"时是谁的回合（0 自己 / 1 对手）。</summary>
        private int _planOwner = -1;

        /// <summary>交闪① 检索过几次（计划里第一次要「零衣」）。</summary>
        private int _planEngagePicks;

        /// <summary>
        /// 本回合已经出过的「闪刀姬」连接怪（只记 :data:`CardId.SequenceOrder` 里那几只）。
        /// **为什么要记**：教程的线是"零露 →（连刀）燎里 → 燎里连接召唤雫空"，各出一次；
        /// 而燎里有**两张**，不排除"出过的"就会一直再拉燎里（固定顺位里它排第二），雫空永远轮不到。
        /// </summary>
        private readonly HashSet<int> _planLinksSummoned = new HashSet<int>();

        /// <summary>是否打印调试信息（命令行 ``Debug=true``）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>已经打过日志的计划回合。</summary>
        private int _loggedPlanTurn = -1;

        /// <summary>
        /// 这个回合**进过场的怪**（卡号）：给斩杀判据用——本回合才上场的怪保守地先不算进"这回合能打多少"
        /// （有的怪写着"特殊召唤的回合不能攻击"），宁可晚一步也不要在打不死时白进战阶。
        /// 回合切换时清（<see cref="OnNewTurn"/>）。
        /// </summary>
        private readonly HashSet<int> _summonedThisTurn = new HashSet<int>();

        /// <summary>每回合清"这个回合才上场的怪"（对手回合里的特召也清：那些怪到我们回合已经能攻击）。</summary>
        public override void OnNewTurn()
        {
            base.OnNewTurn();
            _summonedThisTurn.Clear();
        }

        public override void OnMove(ClientCard card, int previousControler, int previousLocation, int currentControler, int currentLocation)
        {
            base.OnMove(card, previousControler, previousLocation, currentControler, currentLocation);
            if (card != null && currentControler == 0 && currentLocation == (int)CardLocation.MonsterZone)
                _summonedThisTurn.Add(card.Id);
        }

        /// <summary>
        /// 「这回合可以直接打死了」——注册在 `Executors` **最前面**（内核的动作循环是"外层遍历规则、
        /// 内层遍历候选"，战斗阶段要等所有规则都不出手才轮得到；排在后面的话"还有事可做"会一直把战阶推后，
        /// 用户实测的原话就是"明明可以斩杀的非要继续做场"）。
        /// 判据保守：对面**空场**（可以直击）+ 我方表侧攻击表示、且不是本回合才上场的怪攻击力合计 ≥ 对面 LP。
        /// </summary>
        private bool LethalAvailable()
        {
            if (Duel.Player != 0 || !Duel.MainPhase.CanBattlePhase)
                return false;
            if (Enemy.GetMonsterCount() > 0)
            {
                // 对面有怪也要能进战阶（只要我方能**拆掉**对面某只已知的怪）：原来一律 `return false`，
                // 打不动墙就一直站着不打。实测（迭代第 9 轮）有一局 18 回合、动作 68:93、特召 24 次，
                // 最后"没有卡可抽"判负——场面做完不推进战斗，就只会把牌抽干。
                // 里侧未知的不比（那等于赌），交给 :meth:`OnSelectAttackTarget` 去拒绝。
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
        /// 我方有没有一只**表侧攻击表示**的怪能打得穿对面某只**已知（表侧）**的怪。
        /// 攻表示比 ATK、守表示比 DEF——只判"这一刀不亏"，不去预测对面的坑。
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

        /// <summary>保守口径的"这回合能打出来的伤害"（只算表侧攻击表示、且不是本回合才上场的怪）。</summary>
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

        /// <summary>「为了斩杀把蹲着的守备怪转成攻击」：对面空场、转成攻击之后伤害就够了 → 转。</summary>
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
        /// 闪刀的下级/链接怪平时站守备更安全（打点只有 1500），但对面空场、且站攻击就能收掉时，
        /// 表示形式会直接决定这一刀打不打得出去。
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

        /// <summary>
        /// 每回合读一次手牌匹配起手线：本回合的检索、通召、盖放都问它"这条线下一步缺哪一环"。
        /// 只做集合判断，合法性交给内核与卡脚本；没匹配到就回原来的优先级。
        /// </summary>
        private void EnsurePlan()
        {
            // 回合/操控方一变，"本回合出过哪些链接怪"就要清掉：对手回合要用连刀把雫空换成零露
            //（教程的对手回合线），换的目标得按新回合重新算，不能用上个回合的记录。
            if (_planTurn != Duel.Turn || _planOwner != Duel.Player)
            {
                _planTurn = Duel.Turn;
                _planOwner = Duel.Player;
                _planLinksSummoned.Clear();   // 本回合"出过没有"的连接怪记录（见 _planLinksSummoned）
                _planEngagePicks = 0;
                _planLine = PlanLine.None;
                _planLineTurn = -1;
            }
            if (Duel.Player != 0)
            {
                _planLine = PlanLine.None;
                return;
            }
            if (_planLineTurn == Duel.Turn)
                return;
            _planLineTurn = Duel.Turn;
            _planLine = PlanLine.None;
            if (HasInHand(CardId.Raye) || HasInHand(CardId.RayeAlt1) || HasInHand(CardId.RayeAlt2))
                _planLine = PlanLine.Raye;
            else if (HasInHand(CardId.Engage) || HasInHand(CardId.EngageAlt))
                _planLine = PlanLine.Engage;
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
        /// 交闪① 的检索（教程第六～九节）：拿的是"这条线**下一步缺的那一环**"，
        /// 按场上的状态判，不看手里还有没有零衣（原来按"手里有零衣"判 —— 零衣通召完就不在手牌里了，
        /// 于是零衣线里的第一张交闪去拿了「大黄蜂」，而 零露 只能特召一次、这时已经出过，
        /// 整条链当场卡死）：
        /// * 场上**有**能做"变身"的闪刀姬链接怪、且本回合燎里还没出过 → 拿「连刀」：
        ///   连刀把它送墓 → 换成燎里 → 燎里① 回收墓地的这张交闪 → 同一张交闪开第二次
        ///   （教程"燎里① 回收墓地的交闪并发动"就是这么来的）；
        /// * 场上**没有**链接怪 → 拿「大黄蜂」：先生衍生物当链接素材，才做得出来燎里/零露；
        /// * 燎里已经出过（这一回合再生不出第二只燎里）→ 拿「双纽闪门」收尾
        ///   （教程大黄蜂/露世两条线的终场都是"盖放双纽闪门"）。
        /// </summary>
        private int[] EngagePickOrder()
        {
            if (HasStrikerLinkOnField())
            {
                if (!_planLinksSummoned.Contains(CardId.Kagari) && !_planLinksSummoned.Contains(CardId.KagariAlt))
                    return new[] { CardId.Linkage, CardId.WidowAnchor, CardId.MultiRole, CardId.HornetDrones };
                return new[] { CardId.LinkageGate, CardId.Linkage, CardId.WidowAnchor, CardId.MultiRole };
            }
            return new[] { CardId.HornetDrones, CardId.Linkage, CardId.WidowAnchor, CardId.MultiRole };
        }

        /// <summary>
        /// 零露①（检索 1 张「闪刀」魔法卡）：教程里拿的是**交闪**（再靠它拿连刀/双纽闪门），
        /// 没有交闪时才直接拿双纽闪门/连刀。
        /// </summary>
        private static readonly int[] Kaina0PickOrder =
        {
            // 三魔优先：**用了就进墓地的**魔法排在"发不了"的抓锚前面
            //（抓锚要先手对面有效果怪兽才能发，对空白/先手永远不满足）。
            CardId.Engage, CardId.EngageAlt, CardId.LinkageGate, CardId.HornetDrones, CardId.Linkage, CardId.WidowAnchor,
        };

        /// <summary>燎里①（回收 1 张「闪刀」魔法卡）：教程里先回收**交闪**（续航）/大黄蜂（再来一只衍生物）。</summary>
        private static readonly int[] KagariPickOrder =
        {
            // 同理：回收「交闪」（续航 + 再开一次凑三魔）→「大黄蜂」（再用一次也进墓地）→ 连刀 → 抓锚
            CardId.Engage, CardId.EngageAlt, CardId.HornetDrones, CardId.LinkageGate, CardId.Linkage, CardId.WidowAnchor,
        };

        /// <summary>雫空②（结束阶段从卡组拿 1 张同名卡不在墓地的「闪刀」魔法卡）：教程里拿双纽闪门/连刀。</summary>
        private static readonly int[] ShizukuPickOrder =
        {
            CardId.LinkageGate, CardId.HornetDrones, CardId.Linkage, CardId.Engage, CardId.WidowAnchor,
        };

        /// <summary>走线时"这个效果该拿哪张"的顺序；没计划、认不出效果时返回 null（交回通用顺序）。</summary>
        private int[] PlanPickOrder()
        {
            if (!PlanActive())
                return null;
            ClientCard effect = CurrentEffectCard();
            if (effect == null)
                return null;
            if (IsSameCard(effect, CardId.Engage) || IsSameCard(effect, CardId.EngageAlt))
                return EngagePickOrder();
            if (IsSameCard(effect, CardId.Kaina0))
                return Kaina0PickOrder;
            if (IsSameCard(effect, CardId.Kagari) || IsSameCard(effect, CardId.KagariAlt))
                return KagariPickOrder;
            if (IsSameCard(effect, CardId.Shizuku) || IsSameCard(effect, CardId.ShizukuAlt))
                return ShizukuPickOrder;
            if (IsSameCard(effect, CardId.MultiRole))
                return MultiRolePickOrder;      // ② 结束阶段把用过的「闪刀」魔法盖回来
            if (IsSameCard(effect, CardId.Davy))
                return DavyPickOrder;           // ② 检索「绚岚」怪或旋风
            if (IsSameCard(effect, CardId.Fujin))
                return FujinPickOrder;          // ② 检索「绚岚」魔陷或旋风
            return null;
        }

        /// <summary>「多任务战刀机」②（结束阶段从墓地盖回「闪刀」魔法）：先拿**对手回合也能用**的速攻
        ///（抓锚＝无效+夺怪、连刀＝变身躲指向、双纽闪门＝回收+加速连接），交闪/大黄蜂是通常魔法，
        /// 盖着在对手回合发不出去，排后面。</summary>
        private static readonly int[] MultiRolePickOrder =
        {
            CardId.WidowAnchor, CardId.Linkage, CardId.LinkageGate, CardId.Engage, CardId.EngageAlt,
            CardId.HornetDrones,
        };

        /// <summary>「绚岚之达维」②：从卡组拿 1 只「绚岚」怪或「旋风」。先拿能继续检索的（风神），
        /// 再拿旋风（它被破坏时见神/献咏能自己盖回来，是这套引擎的循环件）。</summary>
        private static readonly int[] DavyPickOrder =
        {
            CardId.Fujin, CardId.MysticalSpaceTyphoon, CardId.Sensou,
        };

        /// <summary>「绚岚之风神」②：从卡组拿 1 张「绚岚」魔陷或「旋风」。</summary>
        private static readonly int[] FujinPickOrder =
        {
            CardId.Sensou, CardId.JammingWave, CardId.MysticalSpaceTyphoon,
        };

        /// <summary>走线时"先放一放"的额外怪（会把零衣/露世/燎里这样的身体当素材吃掉的通用连接怪）。</summary>
        private static readonly int[] OffPlanExtraMonsters =
        {
            63013339,   // 闪刀姬-卡米丽娅
            98462037,   // 闪刀姬-阿泽莉娅
            75147529,   // 闪刀姬-泽克
            12421694,   // 闪刀姬-魁奈
            60303245,   // 转生炎兽 独角兔
            29301451,   // S：P小夜骑士
            65741789,   // I：P百变莱娜
        };

        /// <summary>
        /// 额外卡组的特殊召唤（＝连接召唤）总闸门。**这是这套牌最容易出事的一步**：
        /// 每一次主要阶段，只要额外卡组里有"能做出来"的连接怪，内核就会把它列进"可特殊召唤"，
        /// 而连接素材是基类挑的（我们改不了它挑谁）——所以能拦的只有"这一步要不要做连接召唤"。
        ///
        /// 三条判据，按顺序：
        /// 1. **零露永远放行**：她是这套牌的做场核心，只落额外怪兽区、不占主怪兽区；
        /// 2. 走线时把"通用连接怪"（卡米丽娅/泽克/魁奈/S：P…）按住——实测它们会把零衣/露世/燎里
        ///    这些关键身体当素材吃掉，零露就做不出来了；
        /// 3. **别把主怪兽区占掉**：这副牌的「交闪/大黄蜂/抓锚/连刀」全写着"自己的主要怪兽区没有
        ///    怪兽存在的场合"才能发动，主区一被占，一手魔法当场全废。主区已经有怪、额外区也占着时，
        ///    再来一只必然落进主区 → 拦住（除非这一回合有战斗阶段，那是在打斩杀）。
        ///    另外第一回合没有战斗阶段，只靠战斗吃饭的飒天/天津不出（那是打斩杀用的）。
        /// </summary>
        private bool AllowExtraSummon()
        {
            if (IsSameCard(Card, CardId.Kaina0))
                return true;
            if (PlanActive() && IsOffPlanExtraSummon())
                return false;
            if (Bot.GetMonstersInMainZone().Count > 0 && Bot.GetMonstersInExtraZone().Count > 0
                && !BattlePhaseAvailable())
                return false;
            if (!BattlePhaseAvailable() && IsBattleOnlyLink())
                return false;
            return true;
        }

        /// <summary>正在判定的这张额外怪是不是"计划外、会把身体吃掉"的通用连接怪。</summary>
        private bool IsOffPlanExtraSummon()
        {
            if (Card == null)
                return false;
            foreach (int cardId in OffPlanExtraMonsters)
            {
                if (Card.IsCode(cardId) || Card.IsOriginalCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「闪刀姬」的 LINK-1（"1 只闪刀姬怪"就能出的那几只）：飒天（打脸 + 战斗触发堆墓）、
        /// 燎里（回收墓地「闪刀」魔法）、雫空（结束阶段检索）、试号闪刀姬-天津（改写对手 2000+ 打手的效果）。
        /// ⚠ 它们**没有单独的 SpSummon 规则**：额外卡组的连接召唤统一走
        /// <see cref="AllowExtraSummon"/> 那道闸门（0.21.85 之前这里是逐张登记的，
        /// 结果被基类那条"无卡号"的通用 SpSummon 抢在前面执行、闸门从来没生效过）。
        /// </summary>
        private static readonly int[] LinkCycleMonsters =
        {
            8491308,    // 闪刀姬-飒天
            63288573,   // 闪刀姬-燎里
            63288574,   // 闪刀姬-燎里（另一印刷）
            90673289,   // 闪刀姬-雫空
            25072579,   // 试号闪刀姬-天津
        };

        /// <summary>正在判定的这张是不是"只靠战斗吃饭"的连接怪（飒天 / 天津）——第一回合没有战阶，不出。</summary>
        private bool IsBattleOnlyLink()
        {
            return Card != null && (Card.IsCode(BattleOnlyLink[0]) || Card.IsCode(BattleOnlyLink[1]));
        }

        /// <summary>只靠战斗吃饭的连接怪：飒天（打脸 + 战斗触发堆墓）、试号闪刀姬-天津（战斗宣言时炸卡）。</summary>
        private static readonly int[] BattleOnlyLink = { 8491308, 25072579 };

        /// <summary>
        /// 场上有没有"闪刀姬"连接怪（＝能不能拿它做「连刀」的变身素材）。
        /// 只认额外卡组那几只链接怪——主怪兽区的零衣/露世是**通召怪**，连刀送不掉它们（送了也变不出来）。
        /// </summary>
        private bool HasStrikerLinkOnField()
        {
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (!card.HasType(CardType.Link))
                    continue;
                foreach (int cardId in StrikerLinkMonsters)
                {
                    if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        return true;
                }
            }
            return false;
        }

        /// <summary>本家连接怪（额外卡组那几只）的卡号表，用来认"场上的链接怪是不是闪刀姬"。</summary>
        private static readonly int[] StrikerLinkMonsters =
        {
            CardId.Kaina0, CardId.Kagari, CardId.KagariAlt, CardId.Shizuku, CardId.ShizukuAlt,
            CardId.Hayate, CardId.Camellia, CardId.Azalea, CardId.Zeke, CardId.Kaina, 25072579,
        };

        /// <summary>这一回合有没有战斗阶段：**游戏第一回合没有**（内核会把 CanBattlePhase 置否），
        /// 被效果封住时也一样。判"该做斩杀还是该做阻抗"就看它。</summary>
        private bool BattlePhaseAvailable()
        {
            return Duel.Turn != 1 && Duel.MainPhase.CanBattlePhase;
        }

        /// <summary>
        /// 「连刀」的发动时机：**照教程来**——它在自己回合就是要用的（"发动连刀，把场上的零露送去墓地，
        /// 特殊召唤燎里"），对手回合则是变身躲指向；这里只保留原本的"先打"判断（留到战斗阶段之后）。
        /// （上一版曾把它在自己回合整个封掉，那是为了自己推的终场，教程里这条链走不通。）
        /// </summary>
        private bool LinkagePlanAware()
        {
            return Linkage();
        }

        /// <summary>「连刀」的盖放判断：走线时盖；没计划时沿用基类的速攻盖放判断。</summary>
        private bool LinkageSpellSet()
        {
            if (PlanActive())
                return true;
            return DefaultSpellSet();
        }

        private bool Linkage()
        {
            // **对手回合**：变身就是"把场上的链接怪换成零露"（教程的对手回合线：连刀把雫空送墓出零露
            //   → 零露② 解放自己拉零衣+露世并炸 1）。只要场上还有本家链接怪、且本回合零露还没出过就发。
            if (Duel.Player == 1)
            {
                if (!HasStrikerLinkOnField())
                    return false;
                return !_planLinksSummoned.Contains(LinkFamily(CardId.Kaina0));
            }
            // **自己回合** ① "先打"：有战斗阶段、对面空场、我们场上能打时，留到战斗阶段之后再变身
            //   （教程"靠在战斗阶段让怪兽反复变身来斩杀"）——游戏第一回合没有战斗阶段，这条不适用。
            if (BattlePhaseAvailable() && Duel.LastChainPlayer == -1
                && Enemy.GetMonsterCount() == 0 && Bot.GetMonsterCount() > 0)
            {
                return false;   // ① 先打：留着连刀到战斗阶段之后
            }
            // **自己回合** ② "变身"要能换出**本回合还没出过**的那一只，而且要"不花连刀就做不到"：
            //   * 燎里还没出过 → 变身出燎里（燎里① 回收墓地「闪刀」魔法 = 同一张交闪再开一次，
            //     这是零衣线第 4 步；零露写着"不能作为连接素材"，所以这一步只能靠连刀）；
            //   * 燎里用过了、零露还没出过、而且**场上凑不出 2 只本家怪**（做不出连接召唤的零露）
            //     → 变身出零露（教程交闪线的"连刀把燎里送墓出零露"）；
            //   * 其余情况（能直接连接召唤、或者两只都出过了）→ 留着盖放收尾（教程终场"雫空 + 盖放连刀"）。
            if (!HasStrikerLinkOnField())
                return false;
            if (!_planLinksSummoned.Contains(LinkFamily(CardId.Kagari)))
                return true;
            if (!_planLinksSummoned.Contains(LinkFamily(CardId.Kaina0)) && StrikerBodyCount() < 2)
                return true;
            return false;
        }

        /// <summary>场上能当「闪刀姬」连接素材的怪数（零露自己写着"不能作为连接素材"，不算）。</summary>
        private int StrikerBodyCount()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (IsSameCard(card, CardId.Kaina0))
                    continue;
                if (card.Name != null && card.Name.Contains("闪刀姬"))
                    ++count;
            }
            return count;
        }

        // ============================================================ 各卡效果的"要不要应"（见 docs/plan-layer.md）
        // 说明：这些函数都注册成 Activate 规则，而 WindBot 对"可选触发效果"（`～場合才能発動`）
        // 也是拿同一批规则问一遍的（`GameAI.OnSelectEffectYn`），所以返回 false 就等于"这次不应"。

        /// <summary>手牌里有没有"要求自己的主要怪兽区没有怪兽"的「闪刀」魔法（交闪/大黄蜂/抓锚/连刀）。</summary>
        private bool HasZoneRequiringSpell()
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (IsSameCard(card, CardId.Engage) || IsSameCard(card, CardId.EngageAlt)
                    || IsSameCard(card, CardId.HornetDrones) || IsSameCard(card, CardId.WidowAnchor)
                    || IsSameCard(card, CardId.Linkage))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「绚岚之达维/风神」的自身特召：它们是**主怪兽区的身位**（不是链接怪），
        /// 所以只在"主怪兽区可以占"的时候上（见 <see cref="MainZoneMustStayEmpty"/>）——
        /// 自己的回合走线/手里有闪刀魔法时不上，对手回合与线走完之后随便上。
        /// </summary>
        private bool SensouBodyEffect()
        {
            if (Duel.Player == 1)
                return true;
            return !MainZoneMustStayEmpty();
        }

        /// <summary>
        /// 「闪刀姬-露世」：① 从手牌往主怪兽区跳、② 从墓地复活。要挡的只有 ①——
        /// **别把主怪兽区占掉**：教程那条零衣线里露世根本不出场，而主区一占，手里的
        /// 「交闪/大黄蜂/抓锚/连刀」当场全废（它们都写着"自己的主要怪兽区没有怪兽存在的场合"才能发动）。
        /// ② 复活不占代价，能用就用（它给的是身位和后续的链接素材）。
        /// </summary>
        private bool RozeEffect()
        {
            if (Card != null && Card.Location != CardLocation.Hand)
            {
                // ② 从墓地复活：它同样占主区，所以只在"主区还空着"时用（原来写的是"能用就用"，
                // 实测那会把主区堆死——见 <see cref="MainZoneMustStayEmpty"/>）。
                return Bot.GetMonstersInMainZone().Count == 0;
            }
            return !MainZoneMustStayEmpty();
        }

        /// <summary>
        /// 主怪兽区"不能占"：这副牌的「交闪/大黄蜂/抓锚/连刀」全都写着"自己的主要怪兽区没有怪兽存在的场合"
        /// 才能发动，而链接怪（零露/燎里/雫空）都落在额外怪兽区，主区本来就该空着。实测两个把主区占掉
        /// 又立刻断链的情形：
        /// * 零衣线：零露出来后「露世」跳进主区 → 零露① 刚检索上手的「交闪」当场发不出来，整条线停在
        ///   "零露 + 交闪在手里"；
        /// * 交闪/大黄蜂线：「绚岚之达维/风神」自跳进主区 → 燎里回收上来的第二张交闪同样发不出来。
        /// 所以判据是"场上/手里还有没有要靠主区空着才能转的东西"。
        /// </summary>
        private bool MainZoneMustStayEmpty()
        {
            if (HasZoneRequiringSpell())
                return true;                                   // 手里还捏着闪刀魔法
            if (HasStrikerLinkOnField())
                return true;                                   // 场上有本家链接怪：它还要 ①/② 拿魔法、接后续连接
            if (Duel.Player == 0 && PlanActive())
                return true;                                   // 自己回合走线中（教程的线里露世不跳、绚岚怪也不上）
            return false;
        }

        /// <summary>
        /// 「闪刀姬=零露」①（特殊召唤成功时检索 1 张「闪刀」魔法）与 ②（解放自身 →
        /// 从卡组/墓地拉零衣+露世各 1 只，再炸 1 张卡）是"一回合只能用其中一个"。
        ///
        /// 内核把**触发型**效果的提问归一成 `desc = -1`（见 `GameBehavior.OnSelectEffectYn`），
        /// 而 ② 是起动型（desc 是它自己的说明 id）——所以"`ActivateDescription == -1` 且现在是对手回合"
        /// 一定是"刚变身出来那一下的 ①"。教程的对手回合线写得很明确：**零露不开①、用②**
        /// （② 一次给两个身体 + 一炸，比检索一张魔法强），所以那一下要拒掉，把 ② 留出来。
        /// 自己的回合反过来：要 ① 的检索（拿交闪）。
        /// </summary>
        private bool Kaina0Effect()
        {
            if (ActivateDescription == -1 && Duel.Player == 1)
                return false;
            return true;
        }

        /// <summary>
        /// 「闪刀机关-多任务战刀机」：① 本回合对手不能对应自己的魔法发动做事 + 把对象卡送墓
        /// （**代价是自己场上 1 张其他卡**）；② 结束阶段把本回合用过的「闪刀」魔法从墓地盖回来
        /// （同名最多 1 张，"从场上离开的场合除外"）。
        /// ① 只在"场上有白给的身体（衍生物）"时才开——刚做出来的链接怪不能被它当代价吃掉。
        /// ② 纯赚，结束阶段一到就开。
        /// </summary>
        private bool MultiRoleEffect()
        {
            if (Duel.Phase == DuelPhase.End)
                return true;
            return HasTokenOnField();
        }

        /// <summary>
        /// 「闪刀亚式-双纽闪门」（速攻）：① 把墓地的「闪刀姬」怪兽与「闪刀」魔法**各相同数量**
        /// 回卡组（每 3 张弹 1 张场上的卡）；② 在墓地、自己场上有「闪刀」怪兽特召时除外自身 →
        /// 做 1 次「闪刀姬」连接召唤（对手回合的加速连接，整套牌的阻抗核心）。
        /// 自己回合不主动发 ①：盖着/捏着让它进墓地，② 才是它的价值所在（教程的收尾是"盖放双纽闪门"，
        /// 对手回合再发 ① 回收 → 落墓 → ② 上线）。对手回合的窗口就放行。
        /// </summary>
        private bool LinkageGateEffect()
        {
            return Duel.Player == 1;
        }

        /// <summary>「双纽闪门」的盖放出口：盖着才是对手回合的加速连接（见 LinkageGateEffect）。</summary>
        private bool LinkageGateSpellSet()
        {
            return DefaultSpellSet();
        }

        /// <summary>
        /// 「落胤与圣女」①（二选一，见 <see cref="PreferredOptionValues"/>）：
        /// ● 把额外卡组 1 只"有「阿不思的落胤」卡名记述"的怪兽送墓（代价）+ 炸场上 1 张表侧卡；
        /// ● 场上/墓地有「艾克莉西娅」时，把墓地 1 只怪复活到自己场上。
        /// 教程的用法是前者（炸 1 + 送墓，回合结束「烙印龍 阿爾比昂」还会盖 1 张「烙印」魔陷回来）。
        /// **对面场上有表侧卡才开**：没有的话这一支只能炸自己的卡，不值得。
        /// </summary>
        private bool AlbazEffect()
        {
            return HasEnemyCardOnField() || GraveReviveReady();
        }

        /// <summary>
        /// 「强欲而贪欲之壶」：里侧除外卡组顶 10 张换抽 2。**代价是牌库里的 10 张**，会把零衣/交闪/
        /// 连刀一起除外，所以只在"手上已经有一条起手线"（或者手牌够厚）时才开。
        /// </summary>
        private bool DesiresEffect()
        {
            return PlanActive() || Bot.Hand.Count >= 4;
        }

        /// <summary>
        /// 「禁忌的圣冠」：速攻，把场上 1 只表侧怪变成"效果无效 + 不能攻击 + 不会被破坏 +
        /// 不受自身以外的卡的效果影响 + 不能解放 + 不能当素材"。防守件——对手回合挑他最大的一只，
        /// 等于封住它这一回合的攻击与素材用途；自己的回合不发（主阶段要留给展开链）。
        /// </summary>
        private bool ForbiddenCrownEffect()
        {
            return Duel.Player == 1 && BestEnemyThreat() != null;
        }

        /// <summary>
        /// 「月女神的至天」：① 对面怪兽比我方多时，支付 800×N 无效场上若干表侧卡；
        /// ② 对手从手牌/墓地发动怪兽效果时无效它。两张都只在对手回合有意义——优先 ②（无效手坑），
        /// 没有 ② 时用 ① 压对面的场面。
        /// </summary>
        private bool MoonshadowEffect()
        {
            return Duel.Player == 1;
        }

        /// <summary>场上有没有衍生物（能白给的代价）。</summary>
        private bool HasTokenOnField()
        {
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.HasType(CardType.Token))
                    return true;
            }
            return false;
        }

        /// <summary>对面场上有没有表侧表示的卡。</summary>
        private bool HasEnemyCardOnField()
        {
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (card.IsFaceup())
                    return true;
            }
            foreach (ClientCard card in Enemy.GetSpells())
            {
                if (card.IsFaceup())
                    return true;
            }
            return false;
        }

        /// <summary>对面场上最能打的那只（打点最高的表侧怪）。</summary>
        private ClientCard BestEnemyThreat()
        {
            ClientCard best = null;
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (!card.IsFaceup())
                    continue;
                if (best == null || card.Attack > best.Attack)
                    best = card;
            }
            return best;
        }

        /// <summary>「落胤与圣女」分支②的前置：自己场上/墓地有「艾克莉西娅」怪兽。</summary>
        private bool GraveReviveReady()
        {
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.IsMonster() && card.Name != null && card.Name.Contains("艾克莉西娅"))
                    return true;
            }
            foreach (ClientCard card in AllGraves())
            {
                if (card.IsMonster() && card.Name != null && card.Name.Contains("艾克莉西娅"))
                    return true;
            }
            return false;
        }

        /// <summary>我方与对方墓地的卡（「落胤与圣女」两支都可能从对面墓地拿）。</summary>
        private IEnumerable<ClientCard> AllGraves()
        {
            foreach (ClientCard card in Bot.Graveyard)
                yield return card;
            foreach (ClientCard card in Enemy.Graveyard)
                yield return card;
        }

        /// <summary>
        /// 分支型效果的"想要哪一支"：登记的是**内核真正发给客户端的那串选项值**
        /// —— `Util.GetStringId(卡号, k)` ＝ `卡号 * 16 + k`，其中 k 就是卡脚本里
        /// `aux.Stringid(卡号, k)` / `aux.SelectFromOptions(..., aux.Stringid(卡号, k), …)` 的那个下标。
        ///
        /// ⚠ **这里原来登记的是"脚本值"，再由 <c>OptionValue</c> 用 `options[i] % 16 - 1` 去还原**——
        /// 那个换算整体**差一位**（真值是 `卡号*16 + k`，不是 `卡号*16 + 脚本值 + 1`），
        /// 于是登记的分支几乎从来命中不了，每次都落到基类 `DoEverythingExecutor.OnSelectOption` 的
        /// `Rand.Next` 上**掷骰子**——这就是用户反馈的"乱发效果"。
        /// 下面各卡的 k 已逐张对过 `ygopro/script` 下的卡脚本（2026-10-05）：
        /// * 「落胤与圣女」`c30271097.lua:42-43`：k=1＝"破坏"（额外送墓 + 炸 1 张表侧）、k=2＝墓地复活；
        /// * 「三战之才」`c25311006.lua:30-45`：k=0＝抽2、k=1＝夺控制权、k=2＝看手牌回卡组；
        /// * 「月女神的至天」`c38817295.lua:39-41`：k=1＝无效场上、k=2＝无效手牌/墓地的怪兽效果；
        /// * 「绚岚之见神」`c20508881.lua:40-41`：k=2＝抽2丢1速攻、k=3＝检索旋风；
        /// * 「绚岚之献咏」`c67115133.lua:42-43`：k=2＝检索「绚岚」怪、k=3＝检索旋风。
        /// 数组顺序＝优先级（先试第一个，命中就用）。
        /// </summary>
        private int[] PreferredOptionValues(ClientCard effect)
        {
            if (IsSameCard(effect, CardId.JammingWave))          // 绚岚之见神
                // 手上有能丢的（速攻/绚岚）才走"抽2丢1"（k=2），否则去拿旋风（k=3）
                return HandHasSenranOrQuickPlay()
                    ? OptionValues(CardId.JammingWave, 2, 3)
                    : OptionValues(CardId.JammingWave, 3, 2);
            if (IsSameCard(effect, CardId.Sensou))               // 绚岚之献咏
                // k=2＝检索 4 星以下的「绚岚」怪（达维/风神，出场还能再检索一次），k=3＝检索旋风
                return OptionValues(CardId.Sensou, 2, 3);
            if (IsSameCard(effect, CardId.AlbazAndEcclesia))     // 落胤与圣女
                // 对面有表侧卡 → 走"破坏"（k=1）；否则只能炸自己的卡，退而走墓地复活（k=2）
                return HasEnemyCardOnField()
                    ? OptionValues(CardId.AlbazAndEcclesia, 1, 2)
                    : OptionValues(CardId.AlbazAndEcclesia, 2, 1);
            if (IsSameCard(effect, CardId.TripleTacticsTalent))  // 三战之才
                // 沿用原来的先后：抽2（k=0）→ 看手牌回卡组（k=2）→ 夺控制权（k=1）。
                // ⚠ 这张是**动态排表**（哪一支做得出来才列哪一支，列表会压缩，日志里出现过
                //   `options=[404976096,404976098]` 这种缺中间项的），所以只能按值比、不能按下标猜。
                return OptionValues(CardId.TripleTacticsTalent, 0, 2, 1);
            if (IsSameCard(effect, CardId.Moonshadow))           // 月女神的至天
                // 优先无效手/墓的怪兽效果（k=2，手坑），其次无效场上（k=1）
                return OptionValues(CardId.Moonshadow, 2, 1);
            return null;
        }

        /// <summary>
        /// 把"卡号 + 卡脚本里 `aux.Stringid(卡号, k)` 的 k"编成内核给的选项值
        /// （＝ <c>Util.GetStringId</c>，见 <see cref="RaiseMoonExecutor"/> 里的用法：
        /// `desc == Util.GetStringId(卡号, k)`）。登记值里带着卡号，所以不会和别的卡的选项撞上。
        /// </summary>
        private int[] OptionValues(int cardId, params int[] stringIds)
        {
            int[] values = new int[stringIds.Length];
            for (int i = 0; i < stringIds.Length; ++i)
                values[i] = Util.GetStringId(cardId, stringIds[i]);
            return values;
        }

        /// <summary>手牌里（除正在发动的这张之外）有没有「绚岚」卡或速攻魔法——
        /// 「绚岚之见神」分支① 要丢 1 张这类卡，**一张都没有的话是"手牌全部丢弃"**，不能随便应。</summary>
        private bool HandHasSenranOrQuickPlay()
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.HasType(CardType.QuickPlay))
                    return true;
                if (card.Name != null && card.Name.Contains("绚岚"))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 选项：把登记好的分支值（＝ <see cref="PreferredOptionValues"/>，**带卡号的完整编码**）
        /// 拿去和内核给的 `options` **按值比**，命中就把那个值的**下标**回回去（WindBot 的约定是回下标）；
        /// 一个都没命中才交给基类——基类 `DoEverythingExecutor.OnSelectOption` 是 `Rand.Next`，
        /// 登记不上就等于继续掷骰子（用户反馈的"乱发效果"就是这里没命中造成的）。
        /// </summary>
        public override int OnSelectOption(IList<int> options)
        {
            if (options == null || options.Count == 0)
                return base.OnSelectOption(options);
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
            // 探针（只在 Debug=true 时打）：分支型效果的选项清单 + 我们登记的值有没有真的出现在里面。
            // 复核方法：想让某张卡走哪一支，就确认它的"登记值"**原样出现在** `options=[…]` 里、
            // 且"命中"是那个值的下标（不是 -1）。例：要"落胤与圣女"走破坏应看到 30271097*16+1 = 484337553。
            if (_verbose)
            {
                string list = "";
                foreach (int value in options)
                    list += (list.Length == 0 ? "" : ",") + value;
                string wantedText = "";
                if (wanted == null)
                    wantedText = "（未登记）";
                else
                {
                    foreach (int value in wanted)
                        wantedText += (wantedText.Length == 0 ? "" : ",") + value;
                }
                Logger.WriteLine("[探针] 选项：正在结算=" + (effect == null ? "（未知）" : effect.Name)
                    + " options=[" + list + "] 登记值=[" + wantedText + "] 命中=" + (hit >= 0 ? hit.ToString() : "未命中"));
            }
            if (hit >= 0)
                return hit;
            return base.OnSelectOption(options);
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
            // ⚠ 这一行原来只有 94145022，而卡表（c4b388c14dc8.ydk）里放的、以及「小丑与锁鸟」的**基准印刷号**
            //   是 94145021（94145022 的 alias 才指向 94145021）。WindBot 的 `IsCode`/`IsOriginalCode` 比的是
            //   卡片自己的 `Id`/`Alias`（见 `ClientCard.cs:416`），一张 `Id=94145021, Alias=0` 的卡匹配不上
            //   94145022 —— 于是它被当成普通怪兽落进 `DefaultMonsterSummon()`：
            //   实测 `temp/train/sky-fix5-30.log` 局3/8/16/18 与 `mb6-96.log` 局15/16 共 6 次**表侧召唤**上场
            //   （`落位：… ← #94145021` → `(0 's 小丑与锁鸟 from Hand move to MonsterZone)` →
            //   `(0 's 小丑与锁鸟 attack …)`）：白扔一张手坑，还把主怪兽区占住（这套牌的
            //   「交闪/抓锚/连刀」都写着"自己的主要怪兽区没有怪兽存在的场合"才能发动）。
            //   列印刷号时以**基准号**为准：别名指向它的那张（94145022）也能被 `IsCode` 命中。
            94145021,   // 小丑与锁鸟（卡表里的那一张 / 基准号）
            94145022,   // 小丑与锁鸟（另一印刷）
            84192580,   // 欢聚友伴·抖抖海月水母
            42141493,   // 欢聚友伴·茸茸长尾山雀
            73642296,   // 屋敷童
            73642297,
            73642298,
        };

        /// <summary>
        /// 额外卡组的选择顺序：先把能**回收/检索**的做出来（燎里回收墓地「闪刀」魔法、雫空结束阶段检索），
        /// 再考虑堆墓与打点（飒天），最后才是解场用的链接怪。
        /// </summary>
        /// <summary>
        /// 教程那条"单卡线"的连接怪出场顺序（每只一回合出一次）：**零露 → 燎里 → 雫空**。
        /// 之后的打手（飒天）与解场（泽克/阿泽莉亚）不参与这条"出过没有"的去重。
        /// </summary>
        private static readonly int[] SequenceOrder =
        {
            CardId.Kaina0,      // 零露（LINK2，做场核心）
            CardId.Kagari,      // 燎里（LINK1，回收墓地「闪刀」魔法）
            CardId.KagariAlt,
            CardId.Shizuku,     // 雫空（LINK1，结束阶段检索「双纽闪门」）
            CardId.ShizukuAlt,
        };

        /// <summary>
        /// 把"同一只怪的另一个印刷号"归到一起：燎里（63288573/63288574）、雫空（90673289/90673288）
        /// 在额外卡组里都有两张卡号，不归一化的话"本回合出过没有"只挡得住其中一张，
        /// 另一张照样会被再拉一次（而规则上它们是"同名卡，一回合只能特殊召唤1次"）。
        /// </summary>
        private static int LinkFamily(int cardId)
        {
            switch (cardId)
            {
                case CardId.KagariAlt:
                    return CardId.Kagari;
                case CardId.ShizukuAlt:
                    return CardId.Shizuku;
                default:
                    return cardId;
            }
        }

        private static readonly int[] ExtraDeckPriority =
        {
            // ⚠ 按 202605 闪刀教程排序：教程反复强调"**优先在场上做出 LINK2「零露」**"
            //（她的②能一次拉出零衣+露世并炸 1 张，是整副牌的做场核心），所以零露排第一；
            // 燎里（火刀）负责回收墓地「闪刀」魔法续航，紧随其后。
            CardId.Kaina0,          // 闪刀姬=零露（LINK2，做场核心）
            CardId.Kagari,          // 燎里（回收墓地「闪刀」魔法）
            CardId.KagariAlt,
            CardId.Shizuku,         // 雫空（结束阶段检索，先攻收尾用）
            CardId.Hayate,          // 飒天（打脸 + 堆墓）
            CardId.Camellia,        // 卡米丽娅（堆墓「闪刀」卡）
            CardId.Azalea,          // 阿泽莉亚（炸卡，和双纽闪门② 配合）
            CardId.Zeke,            // 泽克（除外，和双纽闪门② 配合）
            CardId.Kaina,
            CardId.MultiRole,
            CardId.ShizukuAlt,
        };

        /// <summary>
        /// 「检索/回收」这类**纯我方候选**时的取卡优先级：先拿能生成链接素材的大黄蜂，
        /// 再拿连刀、抓锚这些「闪刀」魔法。
        /// </summary>
        private static readonly int[] PreferredPicks =
        {
            CardId.HornetDrones,
            CardId.Linkage,
            CardId.WidowAnchor,
            CardId.MultiRole,
            CardId.Engage,
            CardId.EngageAlt,
        };

        /// <summary>
        /// 「表侧召唤 or 里侧盖放」：**这副牌一律表侧召唤**。
        ///
        /// 基类 `DefaultExecutor.OnSelectMonsterSummonOrSet` 的判据是"等级 ≤4 且自己场上没有表侧怪
        /// 且**对手的怪全都打得过我** → 盖放"。零衣/露世 正好是等级 4、攻击力 1500，而闪刀的整套引擎
        /// 都建立在"**召唤**成功"上：
        /// * 零衣① 是"自己·对方回合，把这张卡解放才能发动"（**起动效果**，里侧盖放发不出来）；
        /// * 露世① 是"这张卡**召唤**·特殊召唤成功的场合"（里侧盖放不算召唤）；
        /// 盖下去以后它们就是一堵 1500 的墙，手上却还可能捏着交闪/大黄蜂——整个回合白过。
        /// 对手有更大的怪时要做的不是"盖起来苟"，而是把零衣换成链接怪（不受战斗影响、还能检索）。
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
                foreach (int id in PreferredSummons)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return true;
                }
                // 主怪兽区"不能占"的时候（走线中/手里有闪刀魔法/场上有本家链接怪），
                // 别把通召点花在"不参与这条线的怪"上——绚岚的 3 星上了主区，交闪/连刀立刻发不出来。
                if (MainZoneMustStayEmpty() && !IsStrikerMonster(Card))
                    return false;
                // 主区**已经有怪**时不再往主区铺第二只：两只主区怪既爬不了（零露不能当素材）也发不了
                // 闪刀魔法，纯堆死自己（实测 2026-10-07 房间局：零衣+达维+露世三只留在主区，
                // 手里的抓锚/连刀一整个回合全废）。空主区时照旧通召零衣这条线。
                if (Bot.GetMonstersInMainZone().Count > 0)
                    return false;
            }
            return DefaultMonsterSummon();
        }

        /// <summary>这张卡是不是「闪刀姬」怪兽（含衍生物：名字里就有"闪刀姬"）。</summary>
        private static bool IsStrikerMonster(ClientCard card)
        {
            return card != null && card.IsMonster() && card.Name != null && card.Name.Contains("闪刀姬");
        }

        /// <summary>
        /// 通召优先级：**零衣**（① 把自身送去墓地 → 从额外卡组特召「闪刀姬」连接怪，是整副牌的引擎）
        /// 优先；其次是**露世**（② 送墓检索「闪刀」魔法）。闪刀的通召就是"把零衣摆上去立刻换链接怪"，
        /// 通召点花在别的怪上等于浪费。
        /// </summary>
        private static readonly int[] PreferredSummons =
        {
            CardId.Raye,
            CardId.RayeAlt1,
            CardId.RayeAlt2,
            CardId.Roze,
            CardId.RozeAlt,
        };

        /// <summary>
        /// 手坑、旋风与陷阱的**时机**：一律走 WindBot 原生执行器给这些卡准备好的判据，
        /// 不再"能发就发"——用户实测的就是"开局丢 G""卡组胡乱发效果"：
        /// * 增殖的G —— 只在**对手回合**开（<see cref="DefaultExecutor.DefaultMaxxC"/>）；
        /// * 灰流丽 —— 只在**连锁对手的卡**时开；幽鬼兔/屋敷童 —— 对手召唤后或连锁对手时；
        /// * 效果遮蒙者 —— 交给"要无效对面哪只怪"的判据；
        /// * 锁鸟/朔夜时雨/欢聚友伴 —— 只在对手回合、且连锁对手的卡（否则纯白扔）；
        /// * 旋风 —— 只在对面有值得炸的魔陷（永续/装备/场地/盖牌）时开，不空炸；
        /// * 其余陷阱 —— 对手召唤后或连锁对手时才开。
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
                if (IsAny(Card, HandTrapChainOnly))
                {
                    // 「小丑与锁鸟」是**对称锁**（卡文 94145022："这个回合，**双方**不能从卡组把卡加入手卡"）——
                    // 在自己回合丢它＝把自己这回合的检索全锁死。真人局实测（`logs/app_20261006_005636.log.jsonl`）：
                    // 手牌 {锁鸟, 交闪, 绚岚之献咏, 露世, 落胤与圣女} 的回合，它先丢了锁鸟 →
                    // 交闪/献咏 的检索全废 → **整个主阶段什么都没做就结束了**。
                    // 欢聚友伴那两张（水母/山雀）是"对手召唤才抽"，自己回合丢等于白扔一张。
                    // 这三张**只在对手回合**时交（锁鸟还要"连锁对手的卡"）；
                    // 「欢聚友伴」两张（水母/山雀）是"发动后对手每次召唤我就抽 1"，而**发动条件本身只有
                    // "自己场上没有卡"**（`c84192580.lua`/`c42141493.lua` 的 `s.drcon`）——内核在任何时点
                    // 都可能问一次，原来"连锁对手的卡"会在对面还没出怪时白扔。改成**对手这一回合确实召唤过**
                    // 之后才交（`EnemySummonedThisTurn`，见基类；`Duel.LastSummonPlayer` 在连锁开始与阶段
                    // 开始都会被重置成 -1，连锁里判不出来）。
                    // 「朔夜时雨」只废对面一只怪、不伤自己，对手在我们回合特召时也该跟上，所以任何回合都可以。
                    if (Card.IsCode(84192580) || Card.IsCode(42141493))
                        return MulcharmyReady();   // 档位见 DefaultExecutor.MulcharmyWaitSummon（A/B：环境变量 MULCHARMY_GATE）
                    if (Card.IsCode(94145021) || Card.IsCode(94145022))
                        return Duel.Player == 1 && Duel.LastChainPlayer == 1;
                    return Duel.LastChainPlayer == 1;
                }
                if (Card.IsCode(CardId.MysticalSpaceTyphoon))
                    return DefaultMysticalSpaceTyphoon();
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
        /// <summary>只在"对手回合 + 连锁对手的卡"时才能交的手坑（含「小丑与锁鸟」的两个印刷号：
        /// 卡表里那张是基准号 94145021，漏了它整个判据都不生效，见 <see cref="HandTraps"/>）。</summary>
        private static readonly int[] HandTrapChainOnly = { 94145021, 94145022, 52038441, 84192580, 42141493 };

        /// <summary>
        /// 「闪刀机-黑寡妇抓锚」要不要发动：**只有对面场上有"能无效的表侧效果怪兽"时才发**。
        ///
        /// 卡文（`c98338152.lua:22`）把可选目标写成了**双方场上**：
        /// `Duel.IsExistingTarget(aux.NegateEffectMonsterFilter,tp,LOCATION_MZONE,LOCATION_MZONE,1,nil)`
        /// —— 所以"能发就发"在对面还没铺开时**只有自己的怪能选**，会把这张牌白白扔掉：
        /// 实测 `temp/train/sky-fix4-vs88.log`
        /// * 局3/4 第 1 回合（先手、对面空场）：`(0 's 闪刀姬=零露 activate effect …)` 之后紧接着
        ///   `(0 's 闪刀机-黑寡妇抓锚 from Hand move to SpellZone)` →
        ///   `(MonsterZone 's 闪刀姬=零露 become target)` → `(0 's 闪刀机-黑寡妇抓锚 from SpellZone move to Grave)`；
        /// * 局19 第 1 回合：同样一路，靶子是**自己的燎里**；局13 第 6 回合同理。
        /// 抓锚是这套牌唯一的"无效 + 夺怪"阻抗（教程第一节），白扔在自己身上等于让掉一个对手回合，
        /// 而且顺手把自己怪的效果无效掉。判据照 <c>aux.NegateEffectMonsterFilter</c> 抄：
        /// 表侧、未被无效、且是效果怪兽（普通怪/背面怪都无效不了）。
        /// </summary>
        private bool WidowAnchorEffect()
        {
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (card.IsFaceup() && !card.IsDisabled() && card.HasType(CardType.Effect))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「闪刀机-黑寡妇抓锚」效果处理里"要不要夺控制权"的确认——**教程要求"第一张先不夺"**。
        /// 原文（`docs/tutorials/sky-striker.md` 第二节 1）："第一张**不要夺控制权**（夺了之后主怪兽区
        /// 有对手的怪兽，第二张开不出来）；第二张再夺。"
        ///
        /// 判据里两个条件原来都写歪了：
        /// * 闸门原来写 `Bot.GetMonsterCount() == 0` —— 那是**全部 7 个怪兽区**（含额外怪兽区）。
        ///   这套牌的链接怪按 <see cref="GameAI.PreferExtraMonsterZone"/> 站额外怪兽区，所以场上有零露/燎里时
        ///   这个条件永远是假，闸门等于没接线：夺过来的怪站进主怪兽区后，手里的第二张抓锚
        ///   因为卡文条件"自己的主要怪兽区没有怪兽存在"再也发不出来。
        ///   抓锚自己发动时就要求主怪兽区空着，所以正确的口径是 **`GetMonstersInMainZone()`**。
        /// * 认"正在问的是不是抓锚"原来只看连锁顶：对手接一张连锁就认不出来了。
        ///   抓锚的确认是 <c>Duel.SelectYesNo(tp,aux.Stringid(98338152,0))</c>，desc 是带卡号的定值，
        ///   所以先按 desc 认；认不出时再看正在结算的那张是不是抓锚（见 <see cref="CurrentEffectCard"/>）。
        /// </summary>
        public override bool OnSelectYesNo(int desc)
        {
            if (IsWidowAnchorControlConfirm(desc)
                && Bot.GetMonstersInMainZone().Count == 0 && HasAnotherWidowAnchor())
                return false;
            return base.OnSelectYesNo(desc);
        }

        /// <summary>现在问的是不是「抓锚」那个"要不要夺控制权"的确认。</summary>
        private bool IsWidowAnchorControlConfirm(int desc)
        {
            if (desc == Util.GetStringId(CardId.WidowAnchor, 0))
                return true;
            return IsSameCard(CurrentEffectCard(), CardId.WidowAnchor);
        }

        /// <summary>
        /// 手里或后场还有没有**还能用的**第二张「抓锚」（决定这一张要不要留出主怪兽区）。
        /// * **对手回合**：手里的速攻魔法发不出来（速攻在对手回合只能从场上发动），只认后场盖着的那张；
        /// * **自己回合**：手牌、后场都能用。
        /// （这一张发动时已经进墓地，所以后场里查到的必然是"另一张"。）
        /// </summary>
        private bool HasAnotherWidowAnchor()
        {
            if (Bot.HasInSpellZone(CardId.WidowAnchor))
                return true;
            return Duel.Player == 0 && Bot.HasInHand(CardId.WidowAnchor);
        }

        /// <summary>
        /// 打谁：**能一击打死就直接打脸**。基类 <c>DefaultExecutor.OnSelectAttackTarget</c> 只问
        /// "打得过哪只怪"，于是对面场上有小怪时它会先去撞怪，把能一击致胜的那次直击浪费掉
        /// （用户实测："斩杀根本做不到"）。剩下的判断仍交给基类。
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
        /// 选卡：① 额外卡组的候选按 <see cref="ExtraDeckPriority"/> 挑（决定链接爬升顺序）；
        /// ② 纯我方的检索/回收候选按 <see cref="PreferredPicks"/> 挑；
        /// ③ 候选里**同时有对面和我方的卡**时先选对面的（抓锚这一类夺怪/解场效果都要选对方场上）。
        /// </summary>
        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (Duel.Phase == DuelPhase.BattleStart)
                return null;

            // 探针（只在 Debug=true 时打）：所有"检索/加入手牌"的选卡请求，打印候选清单 + 正在结算的卡 + min/max。
            // 用来区分两种"检索发动了却什么都没拿到"：
            // * 日志里**没有这一行** → 内核根本没来问（卡组+墓地里确实没有合法目标＝合法的空处理）；
            // * 日志里**有这一行** → 内核问了，问题在别处（被连锁的效果无效/改写，或我们的选卡被丢弃）。
            // 复查记录（2026-10-06）：`sky-fix4-vs88.log` 局3/局4「零露① 发动了却没检索」= **我们自己的抓锚**
            // 连锁上去把它无效了（见 <see cref="WidowAnchorEffect"/>），不是选卡被丢；最新的
            // `sky-fix5-30.log`（72 次零露发动）与 `mb6-96.log`（36 次）里这类"无后续"是 0 例。
            if (_verbose && hint == HintMsg.AddToHand)
            {
                ClientCard effect = CurrentEffectCard();
                string candidates = "";
                foreach (ClientCard card in cards)
                    candidates += (candidates.Length == 0 ? "" : ",") + (card.Name ?? "UnKnowCard");
                Logger.WriteLine("[探针] 检索选卡：正在结算=" + (effect == null ? "（未知）" : effect.Name)
                    + " min=" + min + " max=" + max + " 候选=[" + candidates + "]");
            }

            if (min <= 1 && max >= 1 && hint == HintMsg.Release)
            {
                // 解放/吃自己场上的怪当费用：挑**最不心疼的**（token → 不在关键名单里的 → 攻最低），
                // 基类从尾部取会把刚做出来的链接怪吃掉。
                return PickCheapestOwnCard(cards, min);
            }

            // 「连刀」的代价（"自己场上 1 张其他卡送去墓地" → 换成额外卡组的「闪刀姬」）：
            // 挑**刚变完身、效果已经结算过**的那只链接怪（教程："连刀把零露送墓出燎里"）。
            // 基类从候选尾部取，会挑到刚做出来的东西、甚至是盖着的魔陷。
            if (min <= 1 && max >= 1 && hint == HintMsg.ToGrave && HasNoEnemyCard(cards)
                && IsSameCard(CurrentEffectCard(), CardId.Linkage))
            {
                IList<ClientCard> cost = PickLinkageCost(cards);
                if (cost != null)
                    return cost;
            }

            // 「多任务战刀机」① 的代价（"自己场上 1 张其他的卡送去墓地"，见 MultiRoleEffect）：
            // 挑最不心疼的那张 —— **衍生物最优先**（白给的），其余按攻低/墓地也有用的排在前面。
            // 没有这条规则时它落进 ③ 的通用挑法、基类从尾部拿，实测把链接怪当代价吃掉了：
            // `temp/train/sky-fix4-vs88.log` 局11 t2 送掉**零露**、局13 t8 送掉**飒天**、
            // 局20 t4 送掉**燎里**（"`(MonsterZone 's 闪刀姬-燎里 become target)`" →
            // "`(0 's 闪刀姬-燎里 from MonsterZone move to Grave)`"），而这三次场上都有衍生物可送。
            if (min <= 1 && max >= 1 && hint == HintMsg.ToGrave && HasNoEnemyCard(cards)
                && IsSameCard(CurrentEffectCard(), CardId.MultiRole))
            {
                IList<ClientCard> cost = PickCheapestOwnCard(cards, min);
                if (cost != null)
                    return cost;
            }

            // 手牌丢弃（「绚岚之见神」① 的"抽 2 丢 1 速攻"）：丢**速攻里最不心疼的**，
            // 零衣/露世/交闪 这些起手件留到最后（见 PickDiscard）。
            if (min == 1 && max == 1 && hint == HintMsg.Discard && AllInHand(cards))
                return PickDiscard(cards);

            // 计划层：走线时按"这条线下一步缺哪一环"挑（交闪先拿零衣、零露① 拿连刀、燎里① 回收连刀）
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                int[] planOrder = PlanPickOrder();
                if (planOrder != null)
                {
                    ClientCard effect = CurrentEffectCard();
                    if (IsSameCard(effect, CardId.Engage) || IsSameCard(effect, CardId.EngageAlt))
                        ++_planEngagePicks;
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

            // ① 额外卡组：链接召唤时选哪只
            if (min <= 1 && max >= 1 && AllInExtraDeck(cards))
            {
                // ⚠ 这里原来有一支"零衣① 手上有「露世」时改拉 LINK-1 燎里"的分支，**已删掉**：
                //   它推的理由是"直接拉零露会让露世站进主怪兽区、把一手魔法锁死"，但教程的零衣线写得很明白
                //   ——「NS 零衣 → ① 出零露（EMZ）→ 零露① 检索交闪 → 连刀把零露送墓出燎里 → 燎里① 回收交闪
                //   → 燎里连接召唤雫空 → 盖放连刀 → EP 雫空② 检索双纽闪门」，燎里是**后面由连刀变身**出来的。
                //   提前用它当素材的话，后面"燎里连接召唤雫空"就没燎里可用了（实测第 1 回合终场＝零露 + 连刀盖着）。
                //   露世站主区那个副作用改在它自己那边挡（见 RozeEffect：手里还有闪刀魔法时不让它跳）。
                foreach (int cardId in ExtraDeckPriority)
                {
                    // ⚠ **本回合还没出过的优先**：教程那条线是"零露 →（连刀）燎里 → 燎里连接召唤雫空"，
                    // 每个各出一次；而燎里有**两张卡号**、雫空也有两张，不归一化的话"出过没有"只挡得住其中一张，
                    // 第二张（63288574）照样会被再拉一次，雫空永远轮不到——实测雫空 2/30、雫空② 检索双纽闪门只有 2 次。
                    if (System.Array.IndexOf(SequenceOrder, cardId) >= 0
                        && _planLinksSummoned.Contains(LinkFamily(cardId)))
                        continue;
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        {
                            _planLinksSummoned.Add(LinkFamily(cardId));
                            IList<ClientCard> summoned = new List<ClientCard>();
                            summoned.Add(card);
                            return summoned;
                        }
                    }
                }
            }


            // ② 纯我方的检索/回收
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

            // ②b 「闪刀亚式-双纽闪门」① 的收尾（"那把回去的卡每 3 张，最多让场上 1 张卡回到手卡"）：
            // 它是**可选的**，而且弹谁很有讲究——实测（2026-10-07 房间对局，`rooms/…` 那把异解）：
            // 它把对手的持续魔法弹回手，对手当回合就重贴回来，等于白弹。
            // 按"弹回手之后对手越难受"排（见 <see cref="BounceScore"/>），
            // 而且**只有自己的卡可弹时一张都不弹**（这一步是"可以"，不是"必须"）。
            if (min <= 1 && IsSameCard(CurrentEffectCard(), CardId.LinkageGate) && EvenMix(cards))
            {
                IList<ClientCard> bounce = PickBounceTarget(cards, System.Math.Max(min, 1));
                if (bounce != null)
                    return bounce;
            }

            // ③ 双方混在一起时先选对面的
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

        /// <summary>「连刀」代价的取舍顺序：先那只"已经变身完、效果结算过"的链接怪（零露最顺手——
        /// 它的① 检索已经拿到手；燎里次之，它的① 已经回收过），衍生物也在前面，最后才是别的东西。</summary>
        private static readonly int[] LinkageCostOrder =
        {
            CardId.Kaina0,                      // 零露
            CardId.Kagari, CardId.KagariAlt,    // 燎里
            CardId.Hayate,                      // 飒天
            CardId.Camellia, CardId.Azalea, CardId.Zeke, CardId.Kaina,
            CardId.Shizuku, CardId.ShizukuAlt,  // 雫空（终场件，最后才考虑）
        };

        /// <summary>「连刀」的代价挑哪张：按 <see cref="LinkageCostOrder"/>，衍生物最优先（白给的）。</summary>
        private static IList<ClientCard> PickLinkageCost(IList<ClientCard> cards)
        {
            ClientCard pick = null;
            foreach (ClientCard card in cards)
            {
                if (card.Controller != 0)
                    continue;
                if (card.HasType(CardType.Token))
                {
                    pick = card;
                    break;
                }
            }
            if (pick == null)
            {
                foreach (int cardId in LinkageCostOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.Controller == 0 && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                        {
                            pick = card;
                            break;
                        }
                    }
                    if (pick != null)
                        break;
                }
            }
            if (pick == null)
                return null;
            List<ClientCard> chosen = new List<ClientCard>();
            chosen.Add(pick);
            return chosen;
        }

        /// <summary>候选是不是全在我方手牌里（＝正在挑要丢弃的手牌）。</summary>
        private static bool AllInHand(IList<ClientCard> cards)
        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card.Location != CardLocation.Hand)
                    return false;
            }
            return true;
        }

        /// <summary>
        /// 手牌丢弃挑哪张：**先丢多余的速攻魔法**（这套牌的速攻里有"被破坏还能自己盖回来"的
        /// 绚岚见神/献咏，也有纯解场的旋风），再丢多余的怪，零衣/露世/交闪这些起手件留到最后。
        /// </summary>
        private static IList<ClientCard> PickDiscard(IList<ClientCard> cards)
        {
            ClientCard pick = null;
            int bestScore = int.MinValue;
            foreach (ClientCard card in cards)
            {
                int score = CardId.Sensou == card.Id || CardId.JammingWave == card.Id ? 10 : 0;
                if (card.HasType(CardType.QuickPlay))
                    score += 5;
                if (card.IsMonster())
                    score += 2;      // 怪兽还能通召，比通常魔法好留
                foreach (int keepId in PreferredSummons)
                {
                    if (card.IsCode(keepId) || card.IsOriginalCode(keepId))
                        score -= 100;    // 零衣/露世 是引擎，别丢
                }
                if (card.IsCode(CardId.Engage) || card.IsOriginalCode(CardId.Engage)
                    || card.IsCode(CardId.EngageAlt) || card.IsOriginalCode(CardId.EngageAlt))
                    score -= 50;         // 交闪是续航
                if (score > bestScore)
                {
                    bestScore = score;
                    pick = card;
                }
            }
            if (pick == null)
                return null;
            List<ClientCard> chosen = new List<ClientCard>();
            chosen.Add(pick);
            return chosen;
        }

        /// <summary>
        /// 从候选里挑"最不心疼"的一张当费用（解放/吃怪）：token 最优先，其次不在关键名单里的，
        /// 最后按攻击力从低到高。
        /// </summary>
        private IList<ClientCard> PickCheapestOwnCard(IList<ClientCard> cards, int count)
        {
            List<ClientCard> pool = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card.Controller == 0)
                    pool.Add(card);
            }
            if (pool.Count == 0)
                return null;
            pool.Sort(delegate (ClientCard left, ClientCard right)
            {
                int scoreLeft = CostScore(left);
                int scoreRight = CostScore(right);
                return scoreLeft.CompareTo(scoreRight);
            });
            List<ClientCard> picked = new List<ClientCard>();
            for (int i = 0; i < pool.Count && picked.Count < count; ++i)
                picked.Add(pool[i]);
            return picked;
        }

        /// <summary>
        /// 候选里双方都有吗（"场上 1 张卡"这类不分敌我的选择）。
        /// </summary>
        private static bool EvenMix(IList<ClientCard> cards)
        {
            bool theirs = false;
            bool mine = false;
            foreach (ClientCard card in cards)
            {
                if (card.Controller == 1)
                    theirs = true;
                else if (card.Controller == 0)
                    mine = true;
            }
            return theirs && mine;
        }

        /// <summary>「双纽闪门」① 弹谁：只在对方那边排（自己的卡一律不弹）。</summary>
        private static IList<ClientCard> PickBounceTarget(IList<ClientCard> cards, int count)
        {
            List<ClientCard> pool = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card.Controller == 1)
                    pool.Add(card);
            }
            if (pool.Count == 0)
                return new List<ClientCard>();     // 只有自己的卡：这一步是"可以"，那就不弹
            pool.Sort(delegate (ClientCard left, ClientCard right)
            {
                return BounceScore(left).CompareTo(BounceScore(right));
            });
            List<ClientCard> picked = new List<ClientCard>();
            for (int i = 0; i < pool.Count && picked.Count < count; ++i)
                picked.Add(pool[i]);
            return picked;
        }

        /// <summary>
        /// 弹回手的"该弹程度"：越小越先弹。判据只按卡文能确定的事实——
        /// ① 衍生物（回手即永久消失，最赚）；② 攻击表示的怪兽（回手就没了这一波的攻击/素材，
        /// 越大的越先）；③ 盖着的卡（未知，且这一回合用不上了）；④ 持续/场地魔法
        /// （能重贴，价值最低的一档）；⑤ 其余。
        /// ⚠ 「回到手卡」不是破坏：对手回合手握的速攻/陷阱重贴一次就回来了，所以**别把
        /// 持续魔法当成"解掉了"**（实测就是这么白弹的）。
        /// </summary>
        private static int BounceScore(ClientCard card)
        {
            if (card.HasType(CardType.Token))
                return 0;
            if (card.Location == CardLocation.MonsterZone && card.IsFaceup() && card.IsAttack())
                return 10 + System.Math.Min(System.Math.Max(card.Attack, 0) / 100, 40);
            if (!card.IsFaceup())
                return 60;
            if (card.HasType(CardType.Continuous) || card.HasType(CardType.Field))
                return 80;
            return 100;
        }

        /// <summary>当费用的"心疼程度"：越小越舍得。</summary>
        private int CostScore(ClientCard card)
        {
            int score = card.HasType(CardType.Token) ? -1000 : 0;   // token 是白给的
            // 主怪兽区里多余的身体：这副牌"主要怪兽区空着"才能发 交闪/抓锚/连刀（见 MainZoneMustStayEmpty），
            // 而链接怪都落在额外怪兽区——所以主区的怪当费用送掉时不只是"不心疼"，是**顺手把主区清干净**。
            // 实测（2026-10-07 房间局）：终场把零衣/露世/达维留在主区，手里的抓锚/连刀当场全废。
            if (card.Location == CardLocation.MonsterZone && Bot.GetMonstersInMainZone().Contains(card))
                score -= 500;
            bool key = false;
            foreach (int cardId in PreferredPicks)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                {
                    key = true;
                    break;
                }
            }
            if (key)
                score += 1000;                                      // 起手件尽量留着
            return score + System.Math.Max(card.Attack, 0) / 100;
        }

        /// <summary>候选是不是全在额外卡组里（＝正在挑要出场的怪兽）。</summary>
        private static bool AllInExtraDeck(IList<ClientCard> cards)        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card.Location != CardLocation.Extra)
                    return false;
            }
            return true;
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
    }
}
