using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「虫惑魔 + 蕾祸」——按操作方给的 202607 教程写的决策层（配合群友投稿的 #98 卡表，
    /// 教程原文见插件 `docs/tutorials/traptrix-ragnaraika.md`）。
    ///
    /// **这一层只排战术优先级**：合法性内核判、条件与自肃卡脚本声明（和另外几份执行器同一套分工）。
    ///
    /// 引擎（按卡文核对过）：
    /// * **虫惑魔侧**：「兰卡之虫惑魔」通召检索「基诺之虫惑魔」、「特莱恩之虫惑魔」通召检索
    ///   「洞」/「落穴」通常陷阱、「基诺之虫惑魔」场上有虫惑魔时从手卡自跳。
    ///   **LINK1「塞拉之虫惑魔」是这条线的中轴**：② 通常陷阱发动时 → 从卡组特召 1 只「虫惑魔」；
    ///   ③ 自己的「虫惑魔」怪兽效果发动时 → 从卡组**盖放 1 张「洞」/「落穴」通常陷阱**。
    /// * **蕾祸侧**：「蕾祸之毬首」把手卡的昆虫·植物·爬虫送墓自跳 → ② 检索最多 2 张「蕾祸」卡
    ///   （动画教程取「矢筈天牛」+「姬邪眼」）**之后把自己 1 张手卡除外**（教程除外「姬邪眼」——
    ///   它从手卡被除外时会在结束阶段按场上昆虫·植物·爬虫的种族种类数抽卡）。
    ///   「矢筈天牛」让除外的 1 只同类怪回卡组、自身从手卡自跳；它**作为「蕾祸」连接素材送墓时**
    ///   还能从墓地守备特召 1 只 4 星以下同类怪（教程用它复活「虫惑魔」）。
    ///   LINK2「武者髑髅」/LINK3「御拜神主」/LINK5「大王鬼牙」都有"回 1 只同类怪 → 自身从墓地自跳"，
    ///   御拜神主① 还能除外墓地 2 只同类怪检索 1 张「蕾祸」陷阱（教程取「蕾祸大轮首狩舞」）。
    /// * **转轴**：LINK2「芳香炽天使-茉莉」② 解放连接区的 1 只怪 → 从卡组特召 1 只**植物族**（＝毬首）。
    ///   ⚠ 它的素材要求是"植物族怪兽 2 只"，两条线都要靠虫惑魔（昆虫/植物）来喂。
    ///
    /// ⚠ **自肃**：毬首②、武者髑髅、御拜神主、大王鬼牙都会把这一回合的**特殊召唤锁成昆虫·植物·爬虫族**
    /// ——所以额外卡组里非该族的（S：P小夜骑士、灾厄之星 提·丰、时间潜行者·表盘修复器）在走线期间不能出，
    /// 见 :meth:`AllowOffRaceExtraSummon`。
    /// </summary>
    [Deck("TraptrixRagnaraika", "AI_TraptrixRagnaraika")]
    class TraptrixRagnaraikaExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            // ---- 虫惑魔（本家）----
            public const int TraptrixSera = 73639099;        // 塞拉之虫惑魔（LINK1，中轴：盖陷阱/拉虫惑魔）
            public const int TraptrixMyrmeleo = 91812341;    // 特莱恩之虫惑魔（检索洞/落穴）
            public const int TraptrixDionaea = 82738277;     // 兰卡之虫惑魔（检索基诺）
            public const int TraptrixMantis = 75416738;      // 基诺之虫惑魔（场上有虫惑魔→手卡自跳）
            public const int TraptrixAtrax = 55428242;       // 阿特拉之虫惑魔
            public const int TraptrixNepenthes = 45803070;   // 蒂奥之虫惑魔（把墓地的洞/落穴盖回来）
            public const int TraptrixGenlisea = 49027020;    // 普蒂卡之虫惑魔
            public const int TraptrixPudica = 74577599;      // 破洞露蒂亚之虫惑魔（陷阱：盖放回合可开，变成 4 星怪）
            public const int TraptrixAllomerus = 59071624;   // 阿洛美勒丝之虫惑魔（R4）
            public const int TraptrixRafflesia = 6511113;    // 芙莉西亚之虫惑魔（R4）
            public const int TraptrixCularia = 952523;       // 库拉莉亚之虫惑魔（LINK2）
            public const int TraptrixAtypus = 48183890;      // 阿蒂普丝之虫惑魔（LINK3）
            public const int TraptrixXyzSera = 1688285;      // 西托莉丝之虫惑魔（LINK1）
            public const int TraptrixGarden = 12801833;      // 虫惑之园（场地）
            // ---- 蕾祸（小轴）----
            public const int RagnaraikaBall = 99153051;      // 蕾祸之毬首（Lv1：送墓自跳 + 检索 2 + 除外手卡）
            public const int RagnaraikaLonghorn = 26548709;  // 蕾祸之矢筈天牛（Lv3：让除外怪回卡组自跳 + 素材送墓复活）
            public const int RagnaraikaJinx = 97262307;      // 蕾祸之姬邪眼（被除外时结束阶段抽卡）
            public const int RagnaraikaSkull = 43129357;     // 蕾祸之武者髑髅（LINK2）
            public const int RagnaraikaShrine = 70514456;    // 蕾祸之御拜神主（LINK3：检索「蕾祸」陷阱）
            public const int RagnaraikaFang = 42307760;      // 蕾祸之大王鬼牙（LINK5，终端）
            public const int RagnaraikaRiot = 4841383;       // 蕾祸缭乱狂咲（永续魔法）
            public const int RagnaraikaStorm = 77573354;     // 蕾祸之曝藤（永续陷阱）
            public const int RagnaraikaDance = 89824842;     // 蕾祸大轮首狩舞（陷阱，教程首选）
            // ---- 转轴与系统外 ----
            public const int Jasmine = 21200905;             // 芳香炽天使-茉莉（LINK2：解放连接区的怪 → 拉植物族）
            public const int Typhon = 93039339;              // 灾厄之星 提·丰（非昆虫植物爬虫 → 走线期间不出）
            public const int TimeThiefPerpetua = 55285840;   // 时间潜行者·表盘修复师（同上）
            public const int SP = 29301450;                  // S：P小夜骑士（同上）
            public const int BottomlessTrapHole = 69599136;  // 无底的落穴
            public const int TrapHole = 29616929;            // 虫惑的落穴
            public const int GravediggerTrapHole = 31548215; // 墓穴洞
            public const int InfiniteImpermanence = 10045474; // 无限泡影
            public const int TripleTactics = 25311006;       // 三战之才
        }

        public TraptrixRagnaraikaExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ⓪ 连接怪优先摆额外怪兽区。卡组依据：「芳香炽天使-茉莉」的箭头是 ↙↘
            //    （卡库 `datas.def`=0x05＝BottomLeft|BottomRight，见 YGOSharp 的 CardLinkMarker），
            //    主怪兽区里的 ↙↘ 只指得到魔陷区——**它站主怪兽区时 ②「把连接区的 1 只自己怪兽解放」
            //    根本没有对象**；只有站额外怪兽区，箭头才伸进主怪兽区（左额外区指 主区1+主区3、
            //    右额外区指 主区3+主区5）。教程原文：「用"主怪兽区正中央以外"的 2 只连 LINK2「茉莉」
            //    → 茉莉② 解放连接区 1 只」——强调"正中央以外"，就是要把主怪兽区正中央那只留场给它解放
            //    （素材挑法见 OnSelectLinkMaterial）。
            //    ⚠ 引擎默认是**主区优先**（`GameAI.OnSelectPlace` 写死 z2→z1→z3→z0→z4，额外区排最后），
            //    不打开这个开关时茉莉会落进主区：整份 `temp/train/maxboard-98.log`（21 局）里
            //    茉莉② 只成功过 1 次（那次刚好是额外区它自己空着）。升辉月/闪刀也是这个卡组级开关。
            GameAI.PreferExtraMonsterZone = true;

            // ① 先摘掉基类那两条笼统规则，换成带护栏的版本（见 ⑦）。必须先摘再加自己的。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].Type == ExecutorType.SummonOrSet || Executors[i].Type == ExecutorType.Activate)
                    Executors.RemoveAt(i);
            }

            // ② 虫惑魔下级："能发就发"（通召检索、手卡自跳都靠它们）。**每张都要点名**——
            //    没点名的卡会掉进最后的通用兜底，而兜底里有"能从额外卡组出怪就让路"那条，
            //    等于把它的效果全否掉（升辉月「白色幻兽」踩过这个坑，见 HANDOFF）。
            AddExecutor(ExecutorType.Activate, CardId.TraptrixMyrmeleo, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixDionaea, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixMantis, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixAtrax, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixNepenthes, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixGenlisea, AlwaysPlay);

            // ③ 中轴与转轴：塞拉（② 拉虫惑魔 / ③ 盖陷阱）、茉莉（② 拉植物族＝毬首）都要发。
            AddExecutor(ExecutorType.Activate, CardId.TraptrixSera, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Jasmine, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixCularia, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixAtypus, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixXyzSera, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixAllomerus, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TraptrixRafflesia, AlwaysPlay);

            // ④ 蕾祸小轴：毬首（自跳+检索）、天牛（自跳+素材送墓复活）、姬邪眼、三个连接怪的
            //    "回一只 → 自身从墓地自跳"、御拜神主① 检索陷阱、大王鬼牙① 破坏/② 自跳。
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaBall, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaLonghorn, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaJinx, RagnaraikaJinx);
            //    ⚠ 三个连接怪的"墓地的自己 + 场上一只回卡组 → 自跳"是**从墓地发动的起动效果**
            //    （脚本 `SetType(EFFECT_TYPE_IGNITION)` + `SetRange(LOCATION_GRAVE)`），代价对象由
            //    :meth:`RagnaraikaSelfRevive` 把关——场上一只零件都没有时宁可不发，
            //    否则基类会从候选尾部取，把刚做出来的终端送回额外卡组
            //    （`maxboard-98.log` 21 局里大王鬼牙被自己的自跳代价送回额外卡组 20 次、御拜神主 22 次）。
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaSkull, RagnaraikaSelfRevive);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaShrine, RagnaraikaSelfRevive);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaFang, RagnaraikaSelfRevive);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaRiot, AlwaysPlay);

            // ⑤ 陷阱：本家的「洞」/「落穴」与「破洞露蒂亚」。
            //    * 破洞露蒂亚**盖放回合就能开**（卡文："也能从手卡丢弃 1 张通常陷阱卡，在盖放的回合发动"）
            //      ——它是这条线的启动件：变 4 星怪站场 + 触发塞拉② 从卡组拉人。
            //      ⚠ 但要**等塞拉站场**再开，否则塞拉② 的时点整个丢掉（见 TraptrixPudica）；
            //    * 其余陷阱走 WindBot 原生判据（对手召唤后/连锁对手时才开），不再"能发就发"。
            AddExecutor(ExecutorType.Activate, CardId.TraptrixPudica, TraptrixPudica);
            AddExecutor(ExecutorType.Activate, CardId.BottomlessTrapHole, DefaultTrap);
            AddExecutor(ExecutorType.Activate, CardId.TrapHole, DefaultTrap);
            AddExecutor(ExecutorType.Activate, CardId.GravediggerTrapHole, DefaultTrap);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaDance, DefaultTrap);
            AddExecutor(ExecutorType.Activate, CardId.RagnaraikaStorm, DefaultTrap);
            //    「无限泡影」是手坑型陷阱，走它自己的判据（要无效对面哪只怪）。

            // ⑥ 系统外与本家魔陷。「虫惑之园」要过 :meth:`TraptrixGarden` 的闸门：从手卡贴场地照常，
            //    但场地区域里的 ③（把自己场上 1 只怪兽除外 → 拉 1 只虫惑魔）**要先有安全祭品才发**。
            AddExecutor(ExecutorType.Activate, CardId.TraptrixGarden, TraptrixGarden);
            AddExecutor(ExecutorType.Activate, CardId.TripleTactics, AlwaysPlay);

            // ⑦ 通用兜底：任何怪都能通召/盖放、任何效果都能发动——但**否决手坑**、给"出额外怪"让路。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));

            // ⑧ 额外卡组的出场顺位：**必须逐张点名注册 SpSummon**（注册顺序＝优先级）。
            //    ⚠ 为什么不能只靠 `ExtraDeckPriority`（OnSelectCard）：额外怪只要**没被点名注册 SpSummon**，
            //    就会掉进基类构造函数里第 0 条"无卡号"的通用 SpSummon（谓词 `DefaultNoExecutor`＝
            //    "这张卡没有任何专属规则"），而它在 Executors 列表里排**最前面** → **谁在内核的候选里
            //    排前面就先出谁**。实测后果（`temp/train/maxboard-98.log`）：首回合先把「塞拉」连成第二只、
            //    再连「武者髑髅」（塞拉+塞拉＝LINK2 就到头了），需要"植物族 2 只"的「茉莉」永远轮不到
            //    —— 整份 21 局里 茉莉② 只成功过 1 次，转轴线从第二步就断。
            //    点名之后通用那条会自动让位（`DefaultNoExecutor` 让给专属规则），顺位＝这里。
            //    教程依据（A 线的爬升）：塞拉(LINK1) → 茉莉(LINK2) → 武者髑髅(LINK2) → 御拜神主(LINK3)
            //    → 大王鬼牙(LINK5)；本家 R4 与其余连接怪是"干扰/续航"，排在爬升链之后。
            //    「茉莉」还要过 :meth:`JasmineSummon` 的闸门（② 没有解放对象就别做，见该处注释）。
            AddExecutor(ExecutorType.SpSummon, CardId.Jasmine, JasmineSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.TraptrixSera, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.RagnaraikaSkull, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.RagnaraikaShrine, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.RagnaraikaFang, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.TraptrixCularia, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.TraptrixAtypus, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.TraptrixXyzSera, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.TraptrixRafflesia, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.TraptrixAllomerus, AlwaysPlay);

            // ⑨ 额外召唤闸门：走线期间不出"非昆虫·植物·爬虫族"的连接怪（S：P小夜骑士 / 灾厄之星 /
            //    时间潜行者）——毬首② 与三个蕾祸连接怪都会把这一回合的特殊召唤锁成那三族，
            //    硬出只会被内核丢掉动作。
            foreach (int cardId in OffRaceExtraMonsters)
                AddExecutor(ExecutorType.SpSummon, cardId, AllowOffRaceExtraSummon);

            // ⑩ 斩杀优先：插到最前面（内核的动作循环是"外层遍历规则"，排前面才会先被问到）。
            Executors.Insert(0, new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));
        }

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>
        /// 「芳香炽天使-茉莉」能不能出（＝它的 ② 有没有解放对象）。
        ///
        /// 卡文：素材「植物族怪兽2只」＋「②：把这张卡所连接区 1 只自己怪兽解放才能发动」，
        /// 而它的箭头是 **↙↘**（卡库 LinkMarker=0x05）——主怪兽区里的 ↙↘ 指不到任何怪兽区，
        /// 只有**站额外怪兽区**时箭头才伸进主怪兽区（左额外区→主区1/主区3，右额外区→主区3/主区5）。
        /// 所以要：① 额外区能腾出来——要么本来就空着，要么场上有「塞拉」（它是站额外区那只，
        /// 当素材之后额外区就归茉莉）；② 场上植物族 ≥3 只——用掉 2 只素材后还剩 1 只留场，供 ② 解放
        /// （教程原文：「用"主怪兽区正中央以外"的 2 只连 LINK2「茉莉」→ 茉莉② 解放连接区 1 只」，
        /// 留场的那只就是被解放的；素材挑法见 :meth:`OnSelectLinkMaterial`）。
        /// 两条都不满足时不做茉莉（把塞拉/植物族白白喂掉、② 还发不了，实测就是这么亏的）。
        /// </summary>
        private bool JasmineSummon()
        {
            int plants = 0;
            foreach (ClientCard card in Bot.MonsterZone)
            {
                if (card != null && card.HasRace(CardRace.Plant))
                    ++plants;
            }
            if (plants < 3)
                return false;
            return Bot.GetMonstersInExtraZone().Count == 0 || BotHasOnField(CardId.TraptrixSera);
        }

        /// <summary>
        /// 「蕾祸之姬邪眼」：① 把手卡的这张卡丢弃 → 从手卡**特殊召唤** 1 只三族怪（卡文）；
        /// ② 这张卡从手卡·墓地被除外时 → 结束阶段按场上三族的种族种类数抽卡。
        ///
        /// ⚠ ① 是"特召"，而本家下级的检索**全是"召唤成功时"**（特莱恩①/兰卡①/普蒂卡①/蒂奥① 的卡文），
        /// 被① 叫下来的那只**一条效果都不发**。而 ④ 这条规则在 Executors 里排在通用
        /// `SummonOrSet`（⑦）之前，实测首回合就是先发① 把「特莱恩」特召下来
        /// （日志里特莱恩紧接着发的是②"特殊召唤成功时"的破坏效果，不是① 的检索）→ 起手整条 A 线报销。
        /// 所以：手里还躺着"通召能发效果"的本家时先不发①（等通召用完，或手里只剩姬邪眼时再发，
        /// 那时它还是一只 1800 打手/毬首② 的除外燃料）；② 是结束阶段的抽卡，照常应。
        /// </summary>
        private bool RagnaraikaJinx()
        {
            if (Card != null && Card.Location == CardLocation.Hand)
            {
                MainPhase main = Duel.MainPhase;
                if (main != null)
                {
                    foreach (ClientCard card in main.SummonableCards)
                    {
                        foreach (int cardId in NormalSummonTriggerCards)
                        {
                            if (card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                                return false;
                        }
                    }
                }
            }
            return true;
        }

        /// <summary>「召唤成功时」能发效果、值得占用这次通召的本家（姬邪眼① 要让位给它们，见 :meth:`RagnaraikaJinx`）。</summary>
        private static readonly int[] NormalSummonTriggerCards =
        {
            CardId.TraptrixMyrmeleo,    // 特莱恩①：检索「洞」/「落穴」（＝破洞露蒂亚，A 线的起手）
            CardId.TraptrixDionaea,     // 兰卡①：检索基诺（白送一只身体）
            CardId.TraptrixGenlisea,    // 普蒂卡①：检索虫惑之园（场地③ 还能多通召一只）
            CardId.TraptrixNepenthes,   // 蒂奥①：复活墓地 1 只虫惑魔
        };

        /// <summary>非昆虫·植物·爬虫族的额外怪：只在**没有自肃**的时候出（见构造函数 ⑨）。</summary>
        private static readonly int[] OffRaceExtraMonsters =
        {
            CardId.Typhon,              // 灾厄之星 提·丰
            CardId.TimeThiefPerpetua,   // 时间潜行者·表盘修复师
            CardId.SP,                  // S：P小夜骑士
        };

        /// <summary>
        /// 「蕾祸」那一串自肃都是"这个回合，自己不是昆虫族·植物族·爬虫类族怪兽不能特殊召唤"——
        /// 而这副牌的额外卡组里有三张不是那三族的（灾厄之星/时间潜行者/S：P）。
        /// 判据：**手上有/场上已经有了会给我们上锁的卡时，这几张就别出**（内核也会拦，但动作被丢掉
        /// 会浪费一次额外召唤机会）。上锁的来源＝毬首（手卡或场上）与三个蕾祸连接怪（场上）。
        /// </summary>
        private bool AllowOffRaceExtraSummon()
        {
            foreach (int cardId in LockSourceCards)
            {
                if (Bot.HasInHand(cardId) || BotHasOnField(cardId))
                    return false;
            }
            return true;
        }

        /// <summary>会给我们上"只能特召昆虫·植物·爬虫"自肃的本家（见 :meth:`AllowOffRaceExtraSummon`）。</summary>
        private static readonly int[] LockSourceCards =
        {
            CardId.RagnaraikaBall,      // 毬首②
            CardId.RagnaraikaSkull,     // 武者髑髅
            CardId.RagnaraikaShrine,    // 御拜神主
            CardId.RagnaraikaFang,      // 大王鬼牙
        };

        /// <summary>
        /// 「破洞露蒂亚之虫惑魔」（陷阱）：卡文"也能从手卡丢弃 1 张通常陷阱卡，**在盖放的回合发动**"
        /// ——它是这条线的启动件（变 4 星怪 + 触发塞拉② 从卡组拉虫惑魔）。
        ///
        /// ⚠ **要等「塞拉」站场再开**：塞拉② 的条件是"通常陷阱卡发动的场合"，且卡脚本把效果
        /// `SetRange(LOCATION_MZONE)` —— 塞拉不在场时先开掉，② 整个丢掉，这只 400/2400 的身体还会被
        /// 顺手当连接素材（实测首回合就是这么打的：盖下去立刻开 → 塞拉连着两只 → 收尾只剩武者髑髅）。
        /// 盖着不动没有代价（它的"盖放回合发动"只在当回合有用，但下一回合起就是普通陷阱，
        /// 照样能被塞拉② 接上）。自己回合交给这里；对手回合走原生陷阱判据。
        /// </summary>
        private bool TraptrixPudica()
        {
            if (Card != null && Duel.Player == 0 && Card.Location == CardLocation.SpellZone && Card.IsFacedown())
                return BotHasOnField(CardId.TraptrixSera);
            return DefaultTrap();
        }

        /// <summary>
        /// 三个「蕾祸」连接怪的"墓地的自己 + 把场上 1 只三族怪回卡组最下面 → 自身自跳"
        /// （武者髑髅②/御拜神主②/大王鬼牙②，卡脚本都是 `EFFECT_TYPE_IGNITION` + `SetRange(LOCATION_GRAVE)`，
        /// 卡文是"以自己场上 1 只…怪兽为对象"）。
        /// 代价对象的顺序由 :meth:`SelfReviveCostOrder` 定：**只能送回已经用过的引擎件/下级**；
        /// 场上没有这类零件时**宁可不跳**——基类 `OnSelectCard` 是从候选尾部取，实测把刚做出来的
        /// 「大王鬼牙」送回额外卡组 20 次、「御拜神主」22 次、「库拉莉亚」20 次（等于自己把终场拆了）。
        /// 在场上的 ①（武者髑髅① 拉墓地蕾祸、大王鬼牙① 炸 2）不受影响；
        /// **只有「御拜神主」① 要过 :meth:`ShrineHasSafeCost` 的闸门**（它的代价是除外墓地，见那里）。
        /// </summary>
        private bool RagnaraikaSelfRevive()
        {
            if (Card != null && Card.Location == CardLocation.Grave)
            {
                foreach (int cardId in SelfReviveCostOrder())
                {
                    if (BotHasOnField(cardId))
                        return true;
                }
                return false;
            }
            // 场上的「御拜神主」①：从自己墓地除外 2 只三族怪 → 检索「蕾祸」陷阱。
            // 墓地里的"安全件"不足时宁可不检索（理由见 ShrineHasSafeCost）。
            if (Card != null && Card.Location == CardLocation.MonsterZone
                && IsSameCard(Card, CardId.RagnaraikaShrine))
                return ShrineHasSafeCost();
            return true;
        }

        /// <summary>
        /// 「御拜神主」①（除外自己墓地 2 只三族怪 → 检索「蕾祸」陷阱）的代价够不够安全：
        /// **墓地里的候选凑不出 2 张"不含三个「蕾祸」连接怪"的卡时，这次检索宁可不发**。
        ///
        /// 判据跟 :data:`ShrineCostOrder` 的分工：那份名单管**先除外谁**（偏好），
        /// 这里管**能不能发**（禁止碰的只有武者髑髅/御拜神主/大王鬼牙——它们是"墓地的自己 + 场上 1 只
        /// 回卡组 → 自跳"的引擎，尤其武者髑髅是 LINK5 收尾的另一半）。
        ///
        /// **日志依据**：`temp/train/interact-98v94.log` 局 9（12:17:35，局 10 的镜像 12:17:48 同一条）——
        /// 那一动之前墓地只剩「蕾祸之矢筈天牛」+「蕾祸之武者髑髅」，`ShrineCostOrder` 只能挑出 1 张，
        /// 于是这段选择落到了 `OnSelectCard` 末尾那条通用分支（"候选里先选对面、再从我方尾部取"），
        /// 把**武者髑髅**当代价除外了：
        /// `(0 's 蕾祸之矢筈天牛 from Grave move to Removed)` /
        /// `(0 's 蕾祸之武者髑髅 from Grave move to Removed)` /
        /// `(0 's 蕾祸大轮首狩舞 from Deck move to Hand)`。
        /// 而武者髑髅正是同一时刻收尾要用的那一只：当时「御拜神主」已经在场（3 点），
        /// 只要它留在墓地，`武者髑髅②` 就能用场上的「毬首」当代价（在 :meth:`SelfReviveCostOrder` 名单里、
        /// 那回合确实在场上）自跳上来 → 御拜神主(3) + 武者髑髅(2) = LINK5「大王鬼牙」。
        /// 实际结果那一回合没做出大王鬼牙（只做了「库拉莉亚」），验收口径的终端整只没了。
        /// 这次搜到的「大轮首狩舞」比不上那个终端；而且它还有另一个来源——「毬首」② 的检索
        /// （见 :data:`BallSearchOrder`）。
        /// </summary>
        private bool ShrineHasSafeCost()
        {
            int safe = 0;
            foreach (ClientCard card in Bot.Graveyard)
            {
                // 代价候选＝自己墓地的"昆虫族·植物族·爬虫类族**怪兽**"（墓地的魔陷不算）
                if (!card.HasType(CardType.Monster) || !IsBugPlantReptile(card))
                    continue;
                if (IsSameCard(card, CardId.RagnaraikaSkull)
                    || IsSameCard(card, CardId.RagnaraikaShrine)
                    || IsSameCard(card, CardId.RagnaraikaFang))
                    continue;
                ++safe;
            }
            return safe >= 2;
        }

        /// <summary>
        /// 额外卡组的挑选顺序（内核只把"这一回合真做得出来的"放进候选，这里只排顺位）。
        /// 按教程的爬升：**茉莉(LINK2) → 塞拉(LINK1) → 武者髑髅(LINK2) → 御拜神主(LINK3) → 大王鬼牙(LINK5)**；
        /// 之后才是"干扰向"的 R4（芙莉西亚）与虫惑魔的连接怪。
        /// ⚠ 这个数组只影响"内核真来问选哪只额外怪"那条路（OnSelectCard）；
        /// **真正决定出场顺位的是构造函数 ⑧ 里逐张点名注册的 SpSummon 规则**（顺序要和这里保持一致），
        /// 因为通用 SpSummon 在内核候选里"谁排前面就先出谁"，根本轮不到这里。
        /// 茉莉排最前：它是转轴（② 从卡组拉毬首），而塞拉第二只是收尾时用天牛② 复活的虫惑魔再做的
        /// （教程 A 线的顺序），先把塞拉吃掉就做不出茉莉了。
        /// </summary>
        private static readonly int[] ExtraDeckPriority =
        {
            CardId.Jasmine,             // 茉莉（LINK2：解放连接区 → 从卡组拉毬首，转轴线第一步）
            CardId.TraptrixSera,        // 塞拉（LINK1：中轴，收尾时用复活的虫惑魔重做）
            CardId.RagnaraikaSkull,     // 武者髑髅（LINK2）
            CardId.RagnaraikaShrine,    // 御拜神主（LINK3：检索「蕾祸」陷阱）
            CardId.RagnaraikaFang,      // 大王鬼牙（LINK5：终端，对方特召时炸 2）
            CardId.TraptrixCularia,     // 库拉莉亚（LINK2）
            CardId.TraptrixAtypus,      // 阿蒂普丝（LINK3）
            CardId.TraptrixXyzSera,     // 西托莉丝（LINK1）
            CardId.TraptrixRafflesia,   // 芙莉西亚（R4：无效特召）
            CardId.TraptrixAllomerus,   // 阿洛美勒丝（R4）
        };

        /// <summary>
        /// 连接素材挑选：true＝第三轮的"连接值刚好的一组 + 塞拉只在做茉莉时优先吃"（新逻辑，现状）；
        /// false＝旧的"按 LinkCount 从大到小贪心 + 站位分"（回退口径，用来做 A/B）。
        /// 判定用 40 局/腿；A/B 结论出来后把败方那段删掉、只留胜方。
        /// </summary>
        private static readonly bool CheapLinkMaterials = true;

        /// <summary>
        /// 连接素材：**先按"目标怪要什么族"过滤，再在候选里挑一组"最省"的**——
        /// 「芳香炽天使-茉莉」要**植物族 2 只**（蒂奥/普蒂卡/破洞露蒂亚），其余本家连接怪只要求
        /// "含昆虫·植物·爬虫"。挑哪一组不靠"连接值大的先吃"，而是把内核给的候选按
        /// <c>Util.GetLinkMaterials</c> 列出**连接值刚好**的所有组合、再按 :meth:`LinkMaterialValue`
        /// ／ :meth:`MaterialKeepPenalty` 打分量取最省的一组（见下面那段注释与 :meth:`MaterialKeepPenalty`）。
        /// 交给基类会在候选里随机取，实测经常凑不够连接值、整个动作被内核丢掉（大王鬼牙因此一直出不来）。
        ///
        /// ⚠ **原来是"按 LinkCount 从大到小贪心"**，加上"候选全是植物族就先吃额外区那只"的猜测——
        /// 实测（`temp/train/mb5-98.log`，种子 1500 那一轮）后果是**把中轴与转轴喂给只要 2 点连接值的
        /// 「武者髑髅」**：04:33:16 那一动是 `普蒂卡 → LINK1「塞拉」` 之后
        /// `塞拉 from MonsterZone move to Grave` + `芳香炽天使-茉莉 from MonsterZone move to Grave`
        /// →「武者髑髅」落地，而场上当时还躺着各算 1 点的「毬首」「天牛」（教程 A 线原文就是
        /// "毬首+天牛 = LINK2「武者髑髅」"）。整份日志里 **塞拉 从场上送墓 57 次，只有 9 次是做茉莉，
        /// 28 次送进了武者髑髅**（另有御拜神主 7／大王鬼牙 3／阿蒂普丝 2／库拉莉亚 1），
        /// 于是验收口径要的首回合终场（**大王鬼牙 + 塞拉 + 大轮首狩舞 + 1 张洞/落穴**）经常少了塞拉。
        /// 那个 `AllPlants` 猜测本身也不成立：候选全是植物族时，除了茉莉还可能是
        /// **只需要"昆虫族·植物族 2 只"的武者髑髅/库拉莉亚**——这条猜测正好把塞拉排到最前面。
        /// 现在**只有真的在做茉莉（`_pendingLinkTarget == Jasmine`）才讲究站位**，其余目标塞拉一律最后才动。
        ///
        /// ⚠ 第三轮这处改动挂在 :data:`CheapLinkMaterials` 开关上（做 A/B 判定用）：
        /// true＝下面这段"连接值刚好的一组"（现状），false＝旧口径
        /// "按 LinkCount 从大到小贪心 + 植物族计划时先吃额外区那只"（等价实现见方法的 `else` 分支）。
        /// </summary>
        public override IList<ClientCard> OnSelectLinkMaterial(IList<ClientCard> cards, int min, int max)
        {
            if (cards == null || cards.Count == 0)
                return base.OnSelectLinkMaterial(cards, min, max);
            int rating = LinkRatingOfTarget();
            bool needPlants = _pendingLinkTarget == CardId.Jasmine;

            List<ClientCard> pool = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (needPlants && !card.HasRace(CardRace.Plant))
                    continue;
                if (!needPlants && !IsBugPlantReptile(card))
                    continue;
                pool.Add(card);
            }

            if (CheapLinkMaterials)
            {
                // 内核列出的就是"连接值刚好做得出来"的那些组合（`CanMakeLinkRating` 允许连接怪按 1
                // 或按自身连接值算），这里只负责在它们之间挑最省的一组。
                List<List<ClientCard>> combos = Util.GetLinkMaterials(pool, rating, min, max > 0 ? max : rating);
                List<ClientCard> best = null;
                int bestValue = int.MaxValue;
                int bestCount = int.MaxValue;
                int bestPenalty = int.MaxValue;
                foreach (List<ClientCard> combo in combos)
                {
                    int value = 0;
                    int penalty = 0;
                    foreach (ClientCard card in combo)
                    {
                        value += LinkMaterialValue(card);
                        penalty += MaterialKeepPenalty(card, needPlants);
                    }
                    // 依次比：① 素材占的连接值合计（越少越省）→ ② 张数（教程的收尾都是两步凑齐）
                    // → ③ 取舍惩罚（能不喂中轴/转轴就不喂）。
                    bool better = value < bestValue
                        || (value == bestValue && combo.Count < bestCount)
                        || (value == bestValue && combo.Count == bestCount && penalty < bestPenalty);
                    if (!better)
                        continue;
                    best = combo;
                    bestValue = value;
                    bestCount = combo.Count;
                    bestPenalty = penalty;
                }
                if (best != null)
                    return best;
                // 一组"刚好连接值"的组合都列不出来（认不出目标连接值等）→ 交回原来的顺序，不猜。
                return base.OnSelectLinkMaterial(cards, min, max);
            }
            else
            {
                // ---- 回退口径（A/B 用；第三轮之前的旧实现，按 `bin/R2/WindBot.exe` 反编译还原）----
                // 植物族计划＝做茉莉，或候选**全是植物族**（旧猜测"候选全植物就先吃额外区那只"。
                // 第三轮已证伪：候选全植物时目标也可能是只要"昆虫·植物 2 只"的武者髑髅/库拉莉亚）。
                bool plantPlan = needPlants || AllPlants(cards);

                // 排序：植物族计划先按站位分（额外区 0 → 主区非中央 1 → 正中央 2）；
                // 其余情况/站位相同时按 LinkCount 从大到小（连接怪在前、连接值大的先吃）。
                pool.Sort((left, right) =>
                {
                    if (plantPlan)
                    {
                        int zone = MaterialZoneRank(left).CompareTo(MaterialZoneRank(right));
                        if (zone != 0)
                            return zone;
                    }
                    return right.LinkCount.CompareTo(left.LinkCount);
                });

                // 贪心：按上面的顺序一张张加，加到"张数 ≥ min 且这堆素材刚好凑得出目标连接值"就停
                // （不回头比较别的组合——这就是原来把中轴塞拉喂给武者髑髅的口径）。
                List<ClientCard> selected = new List<ClientCard>();
                foreach (ClientCard card in pool)
                {
                    if (max > 0 && selected.Count >= max)
                        break;
                    selected.Add(card);
                    if (selected.Count < min)
                        continue;
                    if (Util.CanMakeLinkRating(selected, rating))
                        break;
                }
                if (selected.Count >= min && Util.CanMakeLinkRating(selected, rating))
                    return selected;
                // 贪心凑不出目标连接值 → 交回基类，不猜。
                return base.OnSelectLinkMaterial(cards, min, max);
            }
        }

        /// <summary>
        /// 素材占的连接值：连接怪＝它自己的连接值（内核 `CanMakeLinkRating` 允许按 1 或按自身连接值算，
        /// 这里取上限，好让"多用一只 LINK2 去凑 2 点"比"两只普通怪各 1 点"更贵），普通怪＝1。
        /// 连接值取不到时（没收到 Link 查询）按 1 算，不当作免费素材。
        /// </summary>
        private static int LinkMaterialValue(ClientCard card)
        {
            return card.HasType(CardType.Link) && card.LinkCount > 1 ? card.LinkCount : 1;
        }

        /// <summary>
        /// 素材取舍惩罚（越小越该花）——把"场上的中轴/转轴"和"已经用过的引擎件"分开：
        /// * **「塞拉」是中轴**（② 通常陷阱发动 → 从卡组拉虫惑魔、③ 虫惑魔发效果 → 盖洞/落穴，
        ///   验收口径要求它留在首回合终场）：**只有做茉莉那一步必须吃掉它**（茉莉要站额外怪兽区，
        ///   额外区被塞拉占着，而且茉莉② 要解放连接区那只）——所以 `needPlants` 时记 -1（优先吃），
        ///   其余目标记 +1（最后才动，它的连接值贡献与普通怪一样是 1，白吃它没有任何收益）；
        /// * **「茉莉」是转轴**（② 已经从卡组拉过毬首），记 +1：它更适合留着当 A 线收尾
        ///   「武者髑髅② 回收场上一只」的代价（见 :meth:`SelfReviveCostOrder`），而不是当连接素材；
        /// * 其余植物族在 `needPlants` 时按 :meth:`MaterialZoneRank` 记站位分——先把额外区与
        ///   **主怪兽区正中央以外**那只用掉，把正中央留给茉莉② 解放。
        /// </summary>
        private static int MaterialKeepPenalty(ClientCard card, bool needPlants)
        {
            if (IsSameCard(card, CardId.TraptrixSera))
                return needPlants ? -1 : 1;
            if (IsSameCard(card, CardId.Jasmine))
                return 1;
            return needPlants ? MaterialZoneRank(card) : 0;
        }

        /// <summary>
        /// 连接素材的站位顺位（数字小的先用掉）——**只对"正在做茉莉"那一步（新逻辑）或
        /// "候选全是植物族"（回退口径的植物族计划）生效**
        /// （见 :meth:`OnSelectLinkMaterial` / :meth:`MaterialKeepPenalty`）：
        /// * 0＝额外怪兽区那只。「塞拉」**必须先当素材**，否则额外区被它占着，茉莉进不了额外区、② 就没有连接区可解放；
        /// * 1＝主怪兽区里**不在正中央**的那只（Sequence 0/1/3/4）——用掉它，正中央那只能留场；
        /// * 2＝主怪兽区**正中央**（Sequence 2）那只——留到最后才动。
        /// 依据：茉莉箭头 ↙↘（卡库 LinkMarker=0x05），站左额外区指主区1+主区3、站右额外区指主区3+主区5，
        /// **主区3（正中央）两边都指得到**；教程原文也写着"用'主怪兽区正中央以外'的 2 只连 LINK2「茉莉」"。
        /// </summary>
        private static int MaterialZoneRank(ClientCard card)
        {
            if (card.Sequence >= 5)
                return 0;
            if (card.Sequence != 2)
                return 1;
            return 2;
        }

        /// <summary>
        /// 候选是不是**全是植物族**——旧的"植物族计划"猜测，**只为 A/B 回退口径保留**
        /// （见 :data:`CheapLinkMaterials` 的 `false` 分支）。第三轮已证伪：候选全是植物族时，
        /// 目标也可能是只要"昆虫族·植物族 2 只"的武者髑髅/库拉莉亚，这条猜测正好把中轴塞拉排到最前。
        /// </summary>
        private static bool AllPlants(IList<ClientCard> cards)
        {
            foreach (ClientCard card in cards)
            {
                if (card == null || !card.HasRace(CardRace.Plant))
                    return false;
            }
            return cards.Count > 0;
        }

        /// <summary>
        /// 正在挑的额外怪（`OnSelectCard` 挑完记下来，供 :meth:`OnSelectLinkMaterial` 判断
        /// "这次连接召唤要什么素材"——内核问素材时不一定还挂着正在结算的效果）。
        /// </summary>
        private int _pendingLinkTarget;

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
            73642296,   // 屋敷童
            73642297,
            73642298,
            84192580,   // 欢聚友伴·抖抖海月水母
            42141493,   // 欢聚友伴·茸茸长尾山雀
        };

        /// <summary>手坑的字段（原生判据用；与另外几份执行器同一套）。</summary>
        private static readonly int[] HandTrapMaxxC = { 23434538, 23434539 };
        private static readonly int[] HandTrapAsh = { 14558127, 14558128, 14558129 };
        private static readonly int[] HandTrapOgre = { 59438930, 59438931 };
        private static readonly int[] HandTrapVeiler = { 97268402, 97268403 };
        private static readonly int[] HandTrapBelle = { 73642296, 73642297, 73642298 };
        private static readonly int[] HandTrapChainOnly = { 52038441, 94145022, 84192580, 42141493 };

        /// <summary>
        /// 「表侧召唤 or 里侧盖放」：**这副牌一律表侧召唤**。
        ///
        /// 基类 `DefaultExecutor.OnSelectMonsterSummonOrSet` 的判据是"等级 ≤4 且自己场上没有表侧怪
        /// 且**对手的怪全都打得过我** → 盖放"。虫惑魔下级全是"**召唤**成功时"的检索
        ///（特莱恩① 拿「洞/落穴」、兰卡① 拿基诺、普蒂卡① 拿虫惑之园），**里侧盖放不算召唤成功** →
        /// 盖下去这张卡就只剩"一堵 4 星墙"，而整条线（塞拉 → 茉莉 → …）要从那张检索开始；
        /// 另外盖着的怪**不能当连接素材**（连接召唤要表侧怪），本家又全靠连接爬升。
        /// </summary>
        public override bool OnSelectMonsterSummonOrSet(ClientCard card)
        {
            return false;
        }

        /// <summary>通召/盖放的统一入口：手坑一律否掉；虫惑魔下级按 :meth:`SummonRank` 的优先级挑；其余交给基类。</summary>
        private bool SummonOrSet()
        {
            if (Card != null)
            {
                foreach (int id in HandTraps)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return false;
                }
                int rank = SummonRank(Card);
                // ⚠ 内核的 `MainPhase.SummonableCards` 是**按手牌顺序**排的，而 GameAI 的动作循环是
                //    "外层遍历执行器、内层遍历候选"→ 谁先在候选里出现，谁就被通召。原来对
                //    PreferredSummons 里任何一张都点头，等于把通召权交给了手牌顺序：实测首回合通召的是
                //    「阿特拉」（手牌第 2 张）/「蒂奥」（第 4 张）/「兰卡」（第 1 张），
                //    而特莱恩①「召唤成功时 → 检索「洞」/「落穴」」——整条 A 线的起手——一次都没发。
                //    所以这里改成：**只要还有更该先出的（rank 更小）也在这份候选里，就把这次通召让给它**。
                // ⚠ 名单外的卡（rank=0）也要让位：末段兜底 `DefaultMonsterSummon()` 对"等级 ≤4"一律点头，
                //    姬邪眼（1800 打手）/天牛/毬首 这些只要在手牌里排第 1，就会把整回合的通召吃掉
                //    （`temp/train/mb3-98.log` 局 1 行 79、局 2 行 972：开局第一动就是姬邪眼落地，
                //    而手上躺着特莱恩/蒂奥×2——A 线的检索起手整轮报销）。它们等通召用完再上并不亏。
                MainPhase main = Duel.MainPhase;
                if (main != null)
                {
                    foreach (ClientCard card in main.SummonableCards)
                    {
                        int other = SummonRank(card);
                        if (other > 0 && (rank == 0 || other < rank))
                            return false;
                    }
                }
                if (rank > 0)
                    return true;
            }
            return DefaultMonsterSummon();
        }

        /// <summary>通召优先级（1 最高；0＝不在表里）。</summary>
        private static int SummonRank(ClientCard card)
        {
            if (card == null)
                return 0;
            for (int i = 0; i < PreferredSummons.Length; ++i)
            {
                int cardId = PreferredSummons[i];
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    return i + 1;
            }
            return 0;
        }

        /// <summary>
        /// 通召优先级：**先通召能检索的虫惑魔下级**（教程两条线都是"NS 虫惑魔 → 检索"），
        /// 再考虑别的。蕾祸的毬首是"不占 NS 的两卡线"，排在后面。
        /// </summary>
        private static readonly int[] PreferredSummons =
        {
            CardId.TraptrixMyrmeleo,    // 特莱恩①：检索「洞」/「落穴」（＝破洞露蒂亚，A 线的起手）
            CardId.TraptrixDionaea,     // 兰卡①：检索基诺（白送一只身体）
            CardId.TraptrixGenlisea,    // 普蒂卡①：检索虫惑之园（场地③ 还能多通召一只）
            CardId.TraptrixNepenthes,   // 蒂奥①：复活墓地 1 只虫惑魔（空墓地时是白板，排普蒂卡之后）
            CardId.TraptrixAtrax,       // 阿特拉：1800 打手（①②③ 都是持续效果，没有通召触发）
            CardId.TraptrixMantis,      // 基诺：① 是"场上有虫惑魔时从手卡自跳"，本来就不该占通召
            CardId.RagnaraikaBall,      // 毬首：① 送墓 1 只手卡自跳，兜底时才通召
        };

        /// <summary>
        /// 通用发动的护栏：手坑走原生判据；陷阱走原生判据；**现在能从额外卡组出怪 → 通用效果让路**
        /// （专属规则都注册在前面，不受这条影响——但表里没点名的卡会被它整只否掉，所以本副牌的
        /// 所有本家都点名注册了）。
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
                    // 「小丑与锁鸟」是**对称锁**（卡文 94145022："这个回合，**双方**不能从卡组把卡加入手卡"）：
                    // 自己回合丢它＝把自己的检索全锁死；欢聚友伴两张要"对手召唤才抽"，自己回合丢＝白扔。
                    // 这三张只在对手回合、连锁对手的卡；「朔夜时雨」只废对面一只怪，任何回合都可以。
                    if (Card.IsCode(94145022) || Card.IsCode(84192580) || Card.IsCode(42141493))
                        return Duel.Player == 1 && Duel.LastChainPlayer == 1;
                    return Duel.LastChainPlayer == 1;
                }
                if (Card.HasType(CardType.Trap))
                    return DefaultTrap();
                if (ExtraSummonAvailable())
                    return false;
            }
            return DefaultDontChainMyself();
        }

        /// <summary>现在能不能从额外卡组出怪（内核把额外怪放进了"可召唤/可特殊召唤"列表；只在自己回合采信）。</summary>
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

        /// <summary>现在是谁的效果在问（同一个候选列表在不同效果里正确答案不一样）。</summary>
        private ClientCard CurrentEffectCard()
        {
            ClientCard solving = Duel.GetCurrentSolvingChainCard();
            if (solving != null)
                return solving;
            return Duel.GetCurrentChainCard();
        }

        /// <summary>是不是某张卡（空值安全，同一张卡的多个印刷号都算）。</summary>
        private static bool IsSameCard(ClientCard card, int cardId)
        {
            return card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId));
        }

        /// <summary>候选里有没有这张卡（同一个卡号/原卡号的任一印刷）。</summary>
        private static bool IsAny(ClientCard card, int[] cardIds)
        {
            if (card == null)
                return false;
            foreach (int cardId in cardIds)
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

        /// <summary>手牌里有没有这张卡。</summary>
        private bool HasInHand(int cardId)
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>是不是"昆虫族·植物族·爬虫类族"（这一整副牌的自肃都围着这三族转）。</summary>
        private static bool IsBugPlantReptile(ClientCard card)
        {
            return card != null
                && (card.HasRace(CardRace.Insect) || card.HasRace(CardRace.Plant) || card.HasRace(CardRace.Reptile));
        }

        /// <summary>我方墓地/除外区里有没有「洞」或「落穴」通常陷阱（塞拉的②/③ 与蒂奥① 都看它）。</summary>
        private bool HasHoleTrapInGrave()
        {
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (IsHoleTrap(card))
                    return true;
            }
            return false;
        }

        /// <summary>「洞」/「落穴」通常陷阱（这副牌里就三张：墓穴洞、无底的落穴、虫惑的落穴）。</summary>
        private static bool IsHoleTrap(ClientCard card)
        {
            return card != null && card.HasType(CardType.Trap)
                && (card.IsCode(CardId.GravediggerTrapHole) || card.IsOriginalCode(CardId.GravediggerTrapHole)
                    || card.IsCode(CardId.BottomlessTrapHole) || card.IsOriginalCode(CardId.BottomlessTrapHole)
                    || card.IsCode(CardId.TrapHole) || card.IsOriginalCode(CardId.TrapHole));
        }

        /// <summary>
        /// 选卡：按"正在结算的是谁的效果"给顺序——
        /// * 额外卡组：按 :data:`ExtraDeckPriority` 挑爬升链上的那一只（茉莉过 :meth:`JasmineSummon` 闸门）；
        /// * 「蕾祸之毬首」②：检索**「矢筈天牛」+「姬邪眼」**（教程），随后"除外 1 张手卡"由下面那条处理；
        /// * 「蕾祸之毬首」① 的送墓代价：别把「特莱恩」送掉（见 :data:`BallCostOrder`）；
        /// * 「破洞露蒂亚」① 的丢陷阱代价：优先丢「洞」/「落穴」（教程"拉蒂奥并把它盖回来"）；
        /// * 「蕾祸之矢筈天牛」①：让**除外的「姬邪眼」**回卡组（这样姬邪眼② 的结束阶段抽卡才成立）；
        /// * 「蕾祸之矢筈天牛」②（素材送墓）：复活 4 星以下同类怪，优先**虫惑魔**（教程）;
        /// * 「塞拉之虫惑魔」②：从卡组拉虫惑魔——优先植物族，墓地有洞/落穴时先拉**蒂奥**（把陷阱盖回来）；
        /// * 「塞拉之虫惑魔」③ 与「特莱恩」①：**「洞」/「落穴」陷阱**优先；
        /// * 「芳香炽天使-茉莉」②：从卡组拉**植物族＝毬首**；
        /// * 「蕾祸之御拜神主」①：检索「蕾祸」陷阱，优先**「蕾祸大轮首狩舞」**（教程：曝藤是永续陷阱，
        ///   触发不了塞拉②，所以默认拿大轮首狩舞）；它的除外代价**不许吃三个蕾祸连接怪**（见 :data:`ShrineCostOrder`）；
        /// * 「蕾祸之武者髑髅」①：先拉墓地的**天牛**（爬升没做完时）或**毬首**（续航，教程）；
        /// * 「虫惑之园」③ 的除外代价：只能吃"用掉的引擎件/不心疼的身体"（见 :data:`GardenCostOrder`），
        ///   绝不吃场上的塞拉/茉莉/御拜神主/武者髑髅/大王鬼牙/库拉莉亚（见 :meth:`TraptrixGarden`）；
        /// * 破坏/解放类：候选里有对面时先选对面（大王鬼牙① 是两边各算，见下）。
        /// </summary>
        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (Duel.Phase == DuelPhase.BattleStart)
                return null;

            ClientCard effect = CurrentEffectCard();

            // ① 额外卡组：按爬升顺序挑一只（并记下挑的是谁，连接素材那一步要用）
            if (min <= 1 && max >= 1 && AllInExtraDeck(cards))
            {
                foreach (int cardId in ExtraDeckPriority)
                {
                    if (cardId == CardId.Jasmine && !JasmineSummon())
                        continue;       // ② 没有解放对象就别把她摆上来（见 JasmineSummon）
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        {
                            _pendingLinkTarget = cardId;
                            return Single(card);
                        }
                    }
                }
            }

            // 「破洞露蒂亚」① 的发动代价：**从手卡丢 1 张通常陷阱卡**（卡文"也能从手卡丢弃 1 张通常陷阱卡，
            // 在盖放的回合发动"，脚本 `s.cost` 也是 `c:GetType()==TYPE_TRAP`）。教程原文：
            // "丢的陷阱带「洞」/「落穴」字段就拉蒂奥并把它盖回来"——所以优先丢「洞」/「落穴」
            // （丢进墓地后正好给蒂奥② 盖回来、也把塞拉③ 的"洞/落穴"检索串上），
            // 其次是留在手里基本用不出来的「无限泡影」（它的① 要求自己场上没有卡存在的场合），
            // 别把第二张「破洞露蒂亚」（下一轮的启动件）和「大轮首狩舞」（刚检索回来要盖下去的阻坑）当燃料。
            if (min <= 1 && max >= 1 && hint == HintMsg.Discard && IsSameCard(effect, CardId.TraptrixPudica))
            {
                foreach (int cardId in PudicaDiscardOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.Location == CardLocation.Hand
                            && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                            return Single(card);
                    }
                }
            }

            // 「毬首」① 的代价：**从手卡送 1 只三族怪去墓地** → 自身从手卡特召（卡文/脚本 `s.spop`，hint=ToGrave）。
            // ⚠ 这条代价在**召唤手续**里选，不走连锁（`CurrentEffectCard()` 认不到它），所以只能按
            // hint+候选池认：本副牌里"送手卡去墓地"的代价只有它一个。
            // 送谁有讲究：别把「特莱恩」（A 线的检索起手）当燃料——它进了墓地，整条转轴线就没了。
            // 「姬邪眼」放最前（她进墓地还能被御拜神主① 除外触发结束阶段抽卡，卡文②；毬首② 也还能
            // 从卡组拿另一张），其次是没有发动型效果的身体（阿特拉/基诺），再是能被蒂奥①/天牛② 复活的检索件。
            if (min <= 1 && max >= 1 && hint == HintMsg.ToGrave && HasNoEnemyCard(cards))
            {
                foreach (int cardId in BallCostOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.Location == CardLocation.Hand
                            && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                            return Single(card);
                    }
                }
            }

            // 「御拜神主」① 的代价：**从自己墓地除外 2 只三族怪**（卡文/脚本 `s.thcost`）→ 检索「蕾祸」陷阱。
            // 这一步挑错就把收尾的素材吃掉了：教程 A 线的最后一跳是 御拜神主(3) + 武者髑髅(2) = 大王鬼牙(5)，
            // 而武者髑髅这时正躺在墓地等着自跳——把它当除外代价就再也做不出大王鬼牙。
            // 所以优先除外「姬邪眼」（她被除外会在结束阶段抽卡，卡文②）和已经用过的引擎件/下级，
            // 名单里**不含三个「蕾祸」连接怪**（一个都别除外）。
            if (min >= 1 && max >= 1 && hint == HintMsg.Remove && IsSameCard(effect, CardId.RagnaraikaShrine))
            {
                List<ClientCard> picked = new List<ClientCard>();
                foreach (int cardId in ShrineCostOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (picked.Count >= max)
                            break;
                        if (picked.Contains(card))
                            continue;
                        if (card.Location == CardLocation.Grave
                            && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                            picked.Add(card);
                    }
                }
                if (picked.Count >= min)
                    return picked;
            }

            // 「虫惑之园」③ 的费用：**从自己场上除外 1 只怪兽**（卡文/脚本 `s.spcost`）。
            // 只许挑"已经用掉的引擎件 / 场上有也不心疼的身体"（名单见 GardenCostOrder），
            // 绝不把场上的塞拉（中轴）/茉莉（转轴）/御拜神主·武者髑髅·大王鬼牙·库拉莉亚（场面）除外
            // ——通用分支是从候选**尾部**取，而额外怪兽区那几只正好排在最后，实测代价几乎全是它们
            //（见 :meth:`TraptrixGarden` 的统计）。这里挑不出安全件时也不硬发：闸门已经挡在前面。
            if (min >= 1 && max >= 1 && hint == HintMsg.Remove && IsSameCard(effect, CardId.TraptrixGarden))
            {
                List<ClientCard> picked = new List<ClientCard>();
                foreach (int cardId in GardenCostOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (picked.Count >= max)
                            break;
                        if (picked.Contains(card))
                            continue;
                        if (card.Location == CardLocation.MonsterZone
                            && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                            picked.Add(card);
                    }
                }
                if (picked.Count >= min)
                    return picked;
            }

            // 「毬首」② 的第二段：**"选自己 1 张手卡除外"**——教程除外「姬邪眼」（她被除外时能抽卡）。
            if (min <= 1 && max >= 1 && hint == HintMsg.Remove && IsSameCard(effect, CardId.RagnaraikaBall))
            {
                foreach (ClientCard card in cards)
                {
                    if (card.Location == CardLocation.Hand && card.IsCode(CardId.RagnaraikaJinx))
                        return Single(card);
                }
                // 手里没有姬邪眼（或它在墓地）：除外一张多余的蕾祸件/魔法，别把毬首自己除掉
                foreach (ClientCard card in cards)
                {
                    if (card.Location == CardLocation.Hand && !card.IsCode(CardId.RagnaraikaBall))
                        return Single(card);
                }
            }

            // 走线期的检索/回收顺序（用"正在结算的卡"区分，同一个候选池在不同效果里答案不同）
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards))
            {
                int[] order = PlanPickOrder(cards);
                if (order != null)
                {
                    foreach (int cardId in order)
                    {
                        foreach (ClientCard card in cards)
                        {
                            if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                                return Single(card);
                        }
                    }
                }
            }

            // 「毬首」② 的第一段：从卡组·除外拿**最多 2 张**「蕾祸」卡 → 按教程把两张都拿上
            if (max >= 2 && min >= 1 && IsSameCard(effect, CardId.RagnaraikaBall))
            {
                List<ClientCard> picked = new List<ClientCard>();
                foreach (int cardId in BallSearchOrder)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (picked.Count >= max)
                            break;
                        if (picked.Contains(card))
                            continue;
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                            picked.Add(card);
                    }
                }
                if (picked.Count >= min)
                    return picked;
            }

            // 「蕾祸」连接怪自跳的代价：**送回卡组的必须是"已经用过的引擎件/下级"**
            // （顺序见 SelfReviveCostOrder：毬首 → 天牛 → 茉莉 → 虫惑魔下级），
            // 别把场上的御拜神主/大王鬼牙这些刚做出来的连接怪送回去
            // （那三个**不在名单里**，所以场上只剩它们时 RagnaraikaSelfRevive 会直接不发这个效果）。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards) && IsSelfReviveEffect())
            {
                foreach (int cardId in SelfReviveCostOrder())
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.Location == CardLocation.MonsterZone
                            && (card.IsCode(cardId) || card.IsOriginalCode(cardId)))
                            return Single(card);
                    }
                }
            }

            // 接触/连接素材以外：候选里**同时有对面和我方的卡**时先选对面的（这副牌的破坏效果都选对面）。
            // 「大王鬼牙」① 是"场上 2 只怪兽破坏"（卡文，两边都算），候选里 min=max=2 也会走到这里：
            // 先把对面的选满，不够再补我方的（按基类"从尾部取"的老习惯挑，不去动场面判断）。
            // 验收红线里有"不自己炸自己"（docs/acceptance.md），所以不能像基类那样从尾部随手拿 2 张。
            if (min >= 1 && max >= 1)
            {
                List<ClientCard> picked = new List<ClientCard>();
                foreach (ClientCard card in cards)
                {
                    if (picked.Count >= max)
                        break;
                    if (card.Controller == 1)
                        picked.Add(card);
                }
                for (int i = cards.Count - 1; i >= 0 && picked.Count < max; --i)
                {
                    if (cards[i].Controller == 0 && !picked.Contains(cards[i]))
                        picked.Add(cards[i]);
                }
                if (picked.Count >= min)
                    return picked;
            }

            return base.OnSelectCard(cards, min, max, hint, cancelable);
        }

        /// <summary>「毬首」② 的检索顺序（教程：矢筈天牛 + 姬邪眼）。</summary>
        private static readonly int[] BallSearchOrder =
        {
            CardId.RagnaraikaLonghorn,  // 矢筈天牛（让除外的怪回卡组 → 自身自跳）
            CardId.RagnaraikaJinx,      // 姬邪眼（被除外时结束阶段抽卡）
            CardId.RagnaraikaRiot,      // 缭乱狂咲（续航用的永续魔法）
            CardId.RagnaraikaStorm,     // 曝藤
            CardId.RagnaraikaDance,     // 大轮首狩舞
        };

        /// <summary>
        /// 走线期的取卡顺序（按教程；认不出效果时返回 null 交回通用顺序）。
        /// ``cards`` 用来区分同一个效果的两种结算（塞拉② 拉怪 / ③ 盖陷阱）。
        /// </summary>
        private int[] PlanPickOrder(IList<ClientCard> cards)
        {
            ClientCard effect = CurrentEffectCard();
            if (IsSameCard(effect, CardId.RagnaraikaLonghorn))
            {
                // ① 让**除外的「姬邪眼」**回卡组最下面（她回卡组后毬首的钩子才成立，
                //    也顺带把姬邪眼② 的结束阶段抽卡留在手里）；② 素材送墓时复活虫惑魔。
                return LonghornOrder;
            }
            if (IsSameCard(effect, CardId.TraptrixSera))
            {
                // ② 的候选是「虫惑魔」怪兽（拉人）、③ 的候选是「洞」/「落穴」陷阱（盖放）。
                foreach (ClientCard card in cards)
                {
                    if (card.HasType(CardType.Monster))
                        return SeraOrder();
                }
                return SeraTrapSetOrder;
            }
            if (IsSameCard(effect, CardId.TraptrixMyrmeleo))
                return HoleTrapOrder;      // ① 检索「洞」/「落穴」
            if (IsSameCard(effect, CardId.Jasmine))
                return new[] { CardId.RagnaraikaBall };   // ② 从卡组拉植物族＝毬首
            if (IsSameCard(effect, CardId.RagnaraikaShrine))
                return RagnaraikaTrapOrder;               // ① 检索「蕾祸」陷阱
            if (IsSameCard(effect, CardId.RagnaraikaSkull))
                return SkullOrder();                      // ① 从墓地拉「蕾祸」
            return null;
        }

        /// <summary>
        /// 「蕾祸之武者髑髅② / 御拜神主② / 大王鬼牙②」（墓地的它们 + 以**自己场上 1 只**同类怪为对象 →
        /// 那只回卡组、这张卡自跳）：**优先把已经用过的引擎件/下级送回卡组**——
        /// 把场上的连接怪（尤其刚做出来的御拜神主/大王鬼牙）送回卡组会把自己刚做好的场面拆掉
        /// （实测大王鬼牙被自己的自跳代价送回额外卡组 10 次、库拉莉亚 10 次）。
        /// 「茉莉」排在引擎件之后：A 线的收尾正是把用完的茉莉还给卡组、换武者髑髅上来凑 LINK5。
        /// 认不出正在结算的效果时不动手（交回基类）。
        /// </summary>
        private int[] SelfReviveCostOrder()
        {
            return new[]
            {
                CardId.RagnaraikaBall,      // 毬首（已经用过的引擎件，教程就是把它回卡组）
                CardId.RagnaraikaLonghorn,  // 天牛（同上）
                CardId.Jasmine,             // 茉莉（② 用完的 LINK2，收尾时还给卡组最合适）
                CardId.TraptrixMyrmeleo,    // 下面都是可以再检索/再复活的虫惑魔下级
                CardId.TraptrixNepenthes,
                CardId.TraptrixGenlisea,
                CardId.TraptrixMantis,
                CardId.TraptrixAtrax,
                CardId.TraptrixDionaea,
                CardId.TraptrixPudica,      // 破洞露蒂亚（怪兽形态＝植物族 4 星，最后才考虑送回去）
            };
        }

        /// <summary>现在问的是不是"墓地的蕾祸连接怪回收场上 1 只怪 → 自身自跳"那三个效果。</summary>
        private bool IsSelfReviveEffect()
        {
            ClientCard effect = CurrentEffectCard();
            return IsSameCard(effect, CardId.RagnaraikaSkull)
                || IsSameCard(effect, CardId.RagnaraikaShrine)
                || IsSameCard(effect, CardId.RagnaraikaFang);
        }

        /// <summary>目标连接怪的连接值（凑素材用；认不出按 1 算＝塞拉/西托莉丝那两只 LINK1）。</summary>
        private int LinkRatingOfTarget()
        {
            switch (_pendingLinkTarget)
            {
                case CardId.RagnaraikaFang:
                    return 5;
                case CardId.RagnaraikaShrine:
                case CardId.TraptrixAtypus:
                    return 3;
                case CardId.Jasmine:
                case CardId.RagnaraikaSkull:
                case CardId.TraptrixCularia:
                    return 2;
                default:
                    return 1;
            }
        }

        /// <summary>「矢筈天牛」的取卡顺序（① 回卡组的除外怪 / ② 复活的对象）。</summary>
        private static readonly int[] LonghornOrder =
        {
            CardId.RagnaraikaJinx,      // 姬邪眼（被除外 → 回卡组）
            CardId.TraptrixMyrmeleo,    // 复活虫惑魔：优先特莱恩（能再检索陷阱）
            CardId.TraptrixDionaea,
            CardId.TraptrixNepenthes,
            CardId.TraptrixGenlisea,
            CardId.TraptrixMantis,
            CardId.TraptrixAtrax,
        };

        /// <summary>
        /// 「塞拉」② 拉人（卡文：同名卡不在自己场上存在的 1 只「虫惑魔」从卡组特召）。
        /// 两个讲究：
        /// * **植物族优先**（蒂奥/普蒂卡），「特莱恩」放最后：茉莉的素材写着"**植物族怪兽 2 只**"，
        ///   场上多一只植物族身体才凑得出"用正中央以外那 2 只连茉莉、把正中央留给 ② 解放"的站位
        ///   （见 :meth:`JasmineSummon` / :meth:`MaterialZoneRank`）；而特莱恩① 是**召唤成功时**
        ///   （卡文"这张卡召唤成功时才能发动"），被塞拉② 特召出来是白板（② 还要求对面场上有魔陷）。
        /// * 墓地有「洞」/「落穴」时先拉**蒂奥**：她② 能把那张盖回场上（教程原文"拉蒂奥并把它盖回来"，
        ///   顺带触发塞拉③ 再盖一张）；没有洞/落穴时先拉**普蒂卡**（她② 除外对面特殊召唤的怪，
        ///   是这三张里唯一"特召出来也有事干"的）。
        /// </summary>
        private int[] SeraOrder()
        {
            if (HasHoleTrapInGrave())
                return new[] { CardId.TraptrixNepenthes, CardId.TraptrixGenlisea, CardId.TraptrixMyrmeleo };
            return new[] { CardId.TraptrixGenlisea, CardId.TraptrixNepenthes, CardId.TraptrixMyrmeleo };
        }

        /// <summary>
        /// 「洞」/「落穴」检索顺序（特莱恩①）。
        /// ⚠ **第一张要拿「破洞露蒂亚之虫惑魔」**——教程 A 线的第一步就是"NS 特莱恩 → 检索破洞露蒂亚"：
        /// 它既能在盖放回合发动变成**植物族身体**（茉莉的连接素材）又能触发塞拉② 从卡组拉第二只虫惑魔。
        /// 原来把它排在最后（当成兜底），结果起手拿的是普通的落穴，茉莉凑不出"植物族 2 只"、
        /// 整条转轴线从第二步就断（实测大王鬼牙 0/30）。
        /// </summary>
        private static readonly int[] HoleTrapOrder =
        {
            CardId.TraptrixPudica,          // 破洞露蒂亚（启动件：盖放回合就能开 → 植物族身体 + 触发塞拉②）
            CardId.GravediggerTrapHole,     // 墓穴洞
            CardId.BottomlessTrapHole,      // 无底的落穴
            CardId.TrapHole,                // 虫惑的落穴
        };

        /// <summary>「塞拉」③ 盖陷阱时用的顺序：普通洞/落穴优先（破洞露蒂亚留着手上的检索件/已经被找走）。</summary>
        private static readonly int[] SeraTrapSetOrder =
        {
            CardId.GravediggerTrapHole,     // 墓穴洞
            CardId.BottomlessTrapHole,      // 无底的落穴
            CardId.TrapHole,                // 虫惑的落穴
            CardId.TraptrixPudica,          // 破洞露蒂亚（兜底）
        };

        /// <summary>「蕾祸」陷阱检索顺序（御拜神主①）。</summary>
        private static readonly int[] RagnaraikaTrapOrder =
        {
            CardId.RagnaraikaDance,     // 大轮首狩舞（教程首选：能触发塞拉②）
            CardId.RagnaraikaStorm,     // 曝藤（永续陷阱，触发不了塞拉，兜底）
        };

        /// <summary>
        /// 「破洞露蒂亚」① 发动代价（从手卡丢 1 张通常陷阱）的取舍顺序：
        /// 「洞」/「落穴」最优先——教程原文"丢的陷阱带「洞」/「落穴」字段就拉蒂奥并把它盖回来"，
        /// 丢进墓地后正好被蒂奥② 盖回来、也接得上塞拉③ 的"洞/落穴"检索；其次「无限泡影」
        /// （它的① 要求自己场上没有卡存在的场合，盖了别的卡之后留在手里基本发不出来）；
        /// 第二张「破洞露蒂亚」和「大轮首狩舞」排最后（一个是下一轮的启动件，一个是刚检索回来的阻坑）。
        /// </summary>
        private static readonly int[] PudicaDiscardOrder =
        {
            CardId.GravediggerTrapHole,
            CardId.BottomlessTrapHole,
            CardId.TrapHole,
            CardId.InfiniteImpermanence,
            CardId.TraptrixPudica,
            CardId.RagnaraikaDance,
        };

        /// <summary>
        /// 「虫惑之园」：从手卡贴场地（① 额外通召一只「虫惑魔」/② 昆虫·植物族的战破保护）照常；
        /// **场地区域里发动的 ③「把自己场上 1 只怪兽除外 → 从手卡·墓地特召 1 只「虫惑魔」」要有安全祭品才发**。
        ///
        /// 卡文/脚本（`script/c12801833.lua` 的 `s.spcost`）：③ 的费用是**我们自己场上的一只怪兽**
        /// （`Duel.Hint(HINT_SELECTMSG, tp, HINTMSG_REMOVE)`，候选＝我方怪兽区）。它没在
        /// :meth:`OnSelectCard` 里点名，会掉进末段"先选对面、不够再从我方**候选尾部**取"的通用分支，
        /// 而我方候选的尾端正是**额外怪兽区**那几只连接怪——实测 `temp/train/mb3-98.log`：20 局里 ③ 发了 40 次，
        /// 费用几乎清一色是场上的关键件（御拜神主 11、塞拉 11、阿蒂普丝 5、武者髑髅 3、茉莉 2、库拉莉亚 2、
        /// 大王鬼牙 1），等于**自己把中轴/终端除外掉去换一只下级**（局 7 行 4308～4316：刚连出来的「塞拉」
        /// 下一动就被 ③ 除外，只为把刚当素材的「普蒂卡」从墓地拉回来，随后又拿普蒂卡当素材、再做第二只塞拉）。
        /// 场上只剩名单之外的关键件（塞拉/茉莉/御拜神主/武者髑髅/大王鬼牙/库拉莉亚/阿蒂普丝）时
        /// **宁可不发**——手卡里的场地照样可以贴下来。
        /// </summary>
        private bool TraptrixGarden()
        {
            if (Card == null || Card.Location != CardLocation.SpellZone)
                return true;
            foreach (int cardId in GardenCostOrder)
            {
                if (BotHasOnField(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「虫惑之园」③ 的除外费用顺序（只列"已经用掉的引擎件 / 场上有也不心疼的身体"）：
        /// 姬邪眼（被除外还能在结束阶段抽卡，卡文②）→ 毬首・天牛（用过的引擎件）→
        /// 阿特拉・基诺（① 是手卡自跳/持续效果，场上这只只是身体）→ 能被蒂奥①/破洞露蒂亚② 复活的本家下级。
        /// **名单里不能出现场上的连接怪**（塞拉是盖陷阱/拉人的中轴，茉莉是转轴，御拜神主/武者髑髅/大王鬼牙/
        /// 库拉莉亚/阿蒂普丝是刚做出来的场面）——与 :data:`ShrineCostOrder` 同一条纪律。
        /// </summary>
        private static readonly int[] GardenCostOrder =
        {
            CardId.RagnaraikaJinx,      // 姬邪眼（被除外 → 结束阶段抽卡）
            CardId.RagnaraikaBall,      // 毬首（用过的引擎件，0/0）
            CardId.RagnaraikaLonghorn,  // 天牛（用过的引擎件）
            CardId.TraptrixAtrax,       // 阿特拉（①②③ 都是持续效果）
            CardId.TraptrixMantis,      // 基诺（① 是手卡自跳，场上的这只只是身体）
            CardId.TraptrixGenlisea,    // 下面都是还能被蒂奥①/破洞露蒂亚② 复活的检索件
            CardId.TraptrixNepenthes,
            CardId.TraptrixDionaea,
            CardId.TraptrixMyrmeleo,
            CardId.TraptrixPudica,      // 破洞露蒂亚（怪兽形态＝植物族身体，最后才考虑）
        };

        /// <summary>
        /// 「毬首」① 的送墓代价（从手卡送 1 只三族怪去墓地）的取舍顺序：
        /// 姬邪眼 → 没有发动型效果的身体 → 能被复活的检索件，**「特莱恩」放最后**
        /// （A 线第一步是"NS 特莱恩 → 检索洞/落穴"，把它送墓这条线就没了；见 OnSelectCard 的分支）。
        /// </summary>
        private static readonly int[] BallCostOrder =
        {
            CardId.RagnaraikaJinx,      // 姬邪眼（进墓地也能被御拜神主① 除外 → 结束阶段抽卡）
            CardId.TraptrixAtrax,       // 阿特拉（①②③ 都是持续效果，就是一只 1800 打手）
            CardId.TraptrixMantis,      // 基诺（① 是手卡自跳，进墓地只是少一只身体）
            CardId.TraptrixDionaea,     // 兰卡（检索件，但能被蒂奥①/天牛② 复活）
            CardId.TraptrixGenlisea,    // 普蒂卡（植物族身体，茉莉的素材，尽量别送）
            CardId.TraptrixNepenthes,   // 蒂奥（同上）
            CardId.TraptrixMyrmeleo,    // 特莱恩（A 线的检索起手，最后一个才考虑）
        };

        /// <summary>
        /// 「御拜神主」① 的除外代价（从自己墓地除外 2 只三族怪）的取舍顺序。
        /// **名单里不能出现三个「蕾祸」连接怪**（武者髑髅/御拜神主/大王鬼牙）——
        /// 教程 A 线的最后一跳是 御拜神主(3)+武者髑髅(2)=大王鬼牙(5)，把墓地里的武者髑髅除外掉，
        /// 那一跳就永远做不出来。优先除外「姬邪眼」（被除外还能在结束阶段抽卡，卡文②），
        /// 其余按"已经用过的引擎件 → 下级"排。
        /// </summary>
        private static readonly int[] ShrineCostOrder =
        {
            CardId.RagnaraikaJinx,      // 姬邪眼（被除外 → 结束阶段抽卡）
            CardId.RagnaraikaBall,      // 毬首（用过的引擎件）
            CardId.RagnaraikaLonghorn,  // 天牛（用过的引擎件）
            CardId.TraptrixMyrmeleo,
            CardId.TraptrixDionaea,
            CardId.TraptrixNepenthes,
            CardId.TraptrixGenlisea,
            CardId.TraptrixMantis,
            CardId.TraptrixAtrax,
            CardId.TraptrixPudica,
        };

        /// <summary>「武者髑髅」① 的复活顺序：**爬升还没做完时先拉天牛**（它是下一段连接素材，
        /// 教程"A 线：武者髑髅① 特召墓地天牛 → 武者髑髅+天牛 = 御拜神主"），
        /// 已经做出御拜神主/大王鬼牙之后才换成拉毬首（教程的续航用法：拉毬首再检索一轮）。
        /// </summary>
        private int[] SkullOrder()
        {
            if (BotHasOnField(CardId.RagnaraikaShrine) || BotHasOnField(CardId.RagnaraikaFang))
                return new[] { CardId.RagnaraikaBall, CardId.RagnaraikaLonghorn };
            return new[] { CardId.RagnaraikaLonghorn, CardId.RagnaraikaBall };
        }

        /// <summary>把单张卡包成返回值。</summary>
        private static IList<ClientCard> Single(ClientCard card)
        {
            List<ClientCard> picked = new List<ClientCard>();
            picked.Add(card);
            return picked;
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

        // ============================================================ 分支型效果的选项（OnSelectOption）

        /// <summary>
        /// 「蕾祸缭乱狂咲」④ 的两个分支号＝卡脚本 `c4841383.lua:48` 里
        /// `Duel.SelectOption(tp, aux.Stringid(id,2), aux.Stringid(id,3))` 的两个 `aux.Stringid` 下标。
        /// </summary>
        private const int RiotBranchSearch = 2;      // 检索 1 只「蕾祸」怪 → 再丢 1 手卡（c4841383.lua:55-59、65-73）
        private const int RiotBranchRevive = 3;      // 特召手卡·墓地·除外的 1 只「蕾祸」怪、守备表示（c4841383.lua:59-62、74-81）

        /// <summary>
        /// 「三战之才」的三个分支号＝卡脚本 `c25311006.lua:30/35/40` 里 `aux.Stringid(25311006,0/1/2)` 的下标。
        /// 这张是**动态排表**（`ops` 只列"做得出来"的那几支，列表会被压缩：探针日志里同时见过三支齐全的
        /// `options=[404976096,404976097,404976098]` 和缺中间项的 `options=[404976096,404976098]`），
        /// 所以只能按值比、不能按下标猜。
        /// </summary>
        private const int TalentBranchDraw = 0;         // 抽 2（c25311006.lua:47-52、67-68）
        private const int TalentBranchTakeControl = 1;  // 夺对面 1 只怪的控制权（c25311006.lua:53-57、69-70）
        private const int TalentBranchToDeck = 2;       // 看对手手牌 → 把其中 1 张洗回卡组（c25311006.lua:58-62、71-72）

        /// <summary>
        /// 分支型效果的"想要哪一支"：登记的是**内核真正发给客户端的那串选项值**
        /// —— `Util.GetStringId(卡号, k)` ＝ `卡号 * 16 + k`（实现在 `AIUtil.cs:234-237`，与卡脚本那侧
        /// `utility.lua:79-81` 的 `Auxiliary.Stringid` 同一算式），k 就是脚本里 `aux.Stringid(卡号, k)` 的下标。
        /// ⚠ 不能拿"脚本选项表里的第几项"当选项值（`docs/HANDOFF.md` 第 13 条记的 `卡号*16+脚本值+1` 是错的：
        /// 实测「魔女工坊的慶典」拿到的是 `6958567*16+2`，那个 `+2` 就是脚本 `aux.Stringid(id,2)` 的下标本身）。
        /// 登记不上的分支每次都会落到基类 `DoEverythingExecutor.OnSelectOption` 的 `Rand.Next` 上**掷骰子**
        /// ——用户反馈的"乱发效果"就是这么来的：本副牌的「蕾祸缭乱狂咲」与「三战之才」原来都完全没登记。
        /// 数组顺序＝优先级（列表被压缩掉某一支时按值比会自动落到下一支）。
        /// </summary>
        private int[] PreferredOptionValues(ClientCard effect)
        {
            // 「蕾祸缭乱狂咲」④（`c4841383.lua:41-63`）：**优先"特召"（k=3）**；特召支不可用时（脚本 b2 为假，
            // 选项表里只剩 k=2）按值比会自动落到检索支。
            // 理由：① 特召支**不花手卡**，把已经用掉的「蕾祸」件从墓地/除外拉回来当连接素材；拉「毬首」
            //       更是白赚一段——毬首的检索（脚本 `e3`）是 `EVENT_SPSUMMON_SUCCESS` 触发器
            //       （`c99153051.lua:25-27`），从墓地/除外被特召回来照样能再检索一次「蕾祸」
            //       （也顺带给了"除外姬邪眼 → 姬邪眼② 结束阶段抽卡"的燃料，见 `c97262307.lua:19-25`）；
            //       ② 检索支除了要丢 1 手卡，**丢哪张还会掉进"从候选尾部取"的通用分支**
            //       （`OnSelectCard` 末段沿用基类习惯，这里没给缭乱狂咲登记丢牌顺序）——实测
            //       `temp/train/mb4-98.log` 行 1373～1389：随机掷到检索支后先检索「姬邪眼」，
            //       紧接着把「增殖的G」丢进墓地（手坑白扔）。
            // ⚠ 教程只列了这张的两支、没写优先哪支（`docs/tutorials/traptrix-ragnaraika.md:33`），
            //   上面两条是现有战术能给出的判断，所以按"哪个可用取哪个、优先特召"登记；**待复核**：
            //   跑一局看探针（想让缭乱狂咲走特召，`options=[…]` 里应原样出现 4841383*16+3 = 77462131）。
            if (IsSameCard(effect, CardId.RagnaraikaRiot))
                return OptionValues(CardId.RagnaraikaRiot, RiotBranchRevive, RiotBranchSearch);

            // 「三战之才」（`c25311006.lua:30-45`）：**优先"抽 2"（k=0）**，列表里没有它才退到"看手牌回卡组"
            // （k=2），最后才是"夺控制权"（k=1）——和「闪刀」那副同一张卡、同一套先后
            // （`SkyStrikerShopExecutor.PreferredOptionValues`）。
            if (IsSameCard(effect, CardId.TripleTactics))
                return OptionValues(CardId.TripleTactics, TalentBranchDraw, TalentBranchToDeck, TalentBranchTakeControl);

            return null;
        }

        /// <summary>
        /// 把"卡号 + 卡脚本里 `aux.Stringid(卡号, k)` 的 k"编成内核给的选项值（＝ <c>Util.GetStringId</c>，
        /// 与 `SkyStrikerShopExecutor` / `RaiseMoonExecutor` 里同一套）。登记值里带着卡号，不会和别的卡的选项撞上。
        /// </summary>
        private int[] OptionValues(int cardId, params int[] stringIds)
        {
            int[] values = new int[stringIds.Length];
            for (int i = 0; i < stringIds.Length; ++i)
                values[i] = Util.GetStringId(cardId, stringIds[i]);
            return values;
        }

        /// <summary>
        /// 选项：把登记好的分支值（<see cref="PreferredOptionValues"/>，**带卡号的完整编码**）拿去和内核给的
        /// `options` **按值比**，命中就把那个值的**下标**回回去（WindBot 的约定是回下标、不是回值）；
        /// 一个都没命中才交给基类——基类 `DoEverythingExecutor.OnSelectOption` 是 `Rand.Next`（掷骰子），
        /// 登记不上就等于继续随机选支。按值比还有一层好处：动态排表被压缩掉某一支时（「三战之才」会出现
        /// `[404976096,404976098]` 这种缺中间项的），优先级列表会自动落到下一支，不会错位。
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
            // 探针（只在 Debug=true 时打）：分支型效果的选项清单 + 登记值有没有真的出现在里面。
            // 复核口径：想让某张卡走哪一支，就确认它的"登记值"**原样出现在** `options=[…]` 里、
            // 且"命中"是那个值的下标（不是"未命中"）。例：要「蕾祸缭乱狂咲」走特召应看到 77462131，
            // 要「三战之才」走抽 2 应看到 404976096。若这里打印的是"正在结算=（未知）"，说明这一步认不出
            // 是谁在问选项（会落到基类掷骰子），那时照 `ToonShopExecutor` 的 `_optionValue` 写法，
            // 在对应的 Activate 判据里先把分支值登记下来。
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

        // ============================================================ 斩杀优先（与闪刀/卡通/魔女术同一套）

        /// <summary>是否打印调试信息（命令行 ``Debug=true``）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

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

        /// <summary>记下我方进场的怪（只记怪兽区；本副牌的怪都会经过这里）。</summary>
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
                return false;
            int damage = LethalDamage();
            bool lethal = damage > 0 && damage >= Enemy.LifePoints;
            if (lethal && _verbose)
                Logger.WriteLine("[斩杀] 可以收掉：场上能打的攻击力合计 " + damage + " ≥ 对面 LP " + Enemy.LifePoints + "，直接进战斗阶段");
            return lethal;
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

        /// <summary>「为了斩杀把蹲着的守备怪转成攻击」：对面空场、把它转成攻击之后伤害就够了 → 转。</summary>
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
        /// 这副牌的下级/连接怪平时站守备更划算（打点只有 800～1800，守备还能配合场地② 的战破保护），
        /// 但对面空场、且站攻击就能收掉时，表示形式会直接决定这一刀打不打得出去
        /// （闪刀/卡通那两副就是这么漏掉斩杀的）。所以这里用**保守写法**：只在"对面空场 + 加上这一刀就够了"时点头。
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
    }
}
