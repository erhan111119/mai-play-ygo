using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「刻魔异响鸣」——按操作方给的教程（`docs/tutorials/刻魔异响鸣.md`）写的专属决策层，
    /// 配合卡组 #100 的 40 主 + 15 额外卡表。
    ///
    /// **这一层只排战术优先级**（WindBot 的规则表＝有序扫描，第一条说 yes 的获胜）。
    /// 合法性（能不能发动、自肃、素材等级、格子够不够）全部由内核对卡脚本判断，这里不重复实现。
    ///
    /// 主线（教程一/二节，目标终场：狮鹫 + 哥布林 + R6 大怒涛 + R4 死旋爆震机 + 律导）：
    /// 1. 「天使之声」/「恶魔之声」①（手卡）丢 1 张 → 自己贴 P、另一张从卡组贴 P（3/5 刻度）；
    /// 2. 「天魔之声选姬」通召 → ① 检索速攻「异响鸣的邀请」；
    /// 3. 「异响鸣的邀请」第二个效果 → 从卡组拿 1 只灵摆怪、另 1 只表侧进额外 → 灵摆召唤 4 星；
    /// 4. 「恶魔之声」LINK1「刻魔的镇魂棺」→ 解放自身拉 4 星「红泪之魔 落泪」→ 堆墓「刻魔的怜歌」
    ///    → 墓地镇魂棺装备给红泪 → 墓地怜歌② 融合 6 星「刻魔 落泪之日」→ ① 特召墓地红泪；
    /// 5. 选姬 + 红泪 R4「雷火沸动油电双动机」③ 拔 2 张拿「剑式阴极」「内燃」；
    /// 6. 6 星落泪之日 + R4 LINK2「刻魔的大圣棺」→ ① 用墓地怪兽（镇魂棺 + 落泪之日）融合 9 星
    ///    「刻魔 赫赫君王」→ ① 丢 1 手堆「刻印群魔的刻魔锻冶师」；
    /// 7. 赫赫君王 + 大圣棺 LINK2「破械神王 阎摩」→ ① 检索「破械神 萨玛」；
    /// 8. 锻冶师（墓）③ 洗回墓地 1 只光恶魔、自身特召 → 锻冶师① 检索「刻魔的詠聖」→ 詠聖① 拿「魔轰神 路里」
    ///    后丢弃 → 路里自跳 → 锻冶师 + 路里 LINK2「梦幻崩影·哥布林」（① 丢 1 手，多 1 次通召）；
    /// 9. 用哥布林① 的追加通召出「内燃雷火沸动机」（召唤时从卡组拉「节式阳极」）；
    /// 10. 阎摩 + 内燃 + 节式 LINK4「梦幻崩影·狮鹫」（① 丢 1 手从墓地盖「律导之异响鸣」，与哥布林互相连接再抽 1）；
    /// 11. 墓地萨玛② 炸掉盖卡自身特召 + 墓地阎摩② 除外自身拉锻冶师 → 两体 R6「DDD 怒涛大王 决策凯撒」；
    /// 12. 手卡「剑式阴极」特召 → ② 检索「外燃」→ 外燃① 送墓额外「蚀之双子」并特召
    ///     → 剑式 + 外燃 R4「雷火沸动死旋爆震机」（① 吸墓地「蚀之双子」= 3 素材 3 次阻抗）。
    ///
    /// **六条踩过的坑（改之前先读）**：
    /// 1. `Func` 返回 false **只跳过这一条规则**，不是否决动作 → "禁止某行为"必须替换掉基类那条笼统规则；
    ///    所以下面 ⓪ 先把基类 `CardId == -1` 的四条笼统规则全摘掉，再按想要的顺序重加。
    /// 2. 规则匹配按**注册顺序**扫（`GameAI.OnSelectIdleCmd`）→ "这张卡先发"必须排在前面；
    ///    战阶那两条用 `Insert(0)` 加，否则"还有事可做"会一直把战阶推后（对面是墙时会被抽干判负）。
    /// 3. 手卡 ignition 的候选卡是 **`Card`**（不是 `CurrentEffect()`），分支选项值＝`卡号*16 + aux.Stringid 序号`。
    /// 4. `OnSelectCard` 框架默认从**尾部**取 → 素材、检索、丢弃、拿哪张都自己写偏好分支。
    /// 5. **触发型效果的 `ActivateDescription` 常常是 `-1`**（2026-10-09 用 `KZ_DIAG=1` 实测到）
    ///    → 判定一律走 `IsEffect(desc, 卡号, 序号)`（"-1 也算"）。写成 `desc == Util.GetStringId(...)`
    ///    的闸门**永远不成立**：本文件里三个闸门就这么变成了死代码，其中「刻魔 落泪之日」③ 的后果最重
    ///    ——它把墓地里的镇魂棺洗回额外卡组，直接掐断 大圣棺① → 赫赫君王 → 阎摩 那半条线。
    /// 6. **`HintMsg.Target` 也要自己写分支**（2026-10-09 加）：漏了会掉进 `default`（按丢弃顺序挑），
    ///    「落泪之日」① 就会去拉镇魂棺而不是 4 星的「红泪之魔 落泪」，R4/大圣棺当场缺素材。
    ///
    /// **雷火沸动四只（内燃/剑式/节式/外燃）的手卡特召都带自肃**（脚本里 `splimit`：
    /// "这个效果特召后，自己不能从额外卡组特召 4 阶以外的怪"）→ 它们的特召规则必须排在
    /// 链接段（大圣棺/阎摩/哥布林/狮鹫）**之后**，否则后面全被内核拒绝（见 <see cref="RyzealSummon"/>）。
    /// </summary>
    [Deck("KezmoYixiangming", "AI_KezmoYixiangming")]
    public class KezmoYixiangmingExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            // ==================== 异响鸣（ヴァルモニカ）====================
            public const int AngelVoice = 3048768;       // 天使之声（光·天使·4★·刻度 3）
            public const int DevilVoice = 30432463;      // 恶魔之声（光·恶魔·4★·刻度 5）
            public const int ChooserHime = 23093373;     // 天魔之声选姬（4★，召唤/灵摆召唤检索「异响鸣」）
            public const int Invitation = 38491852;      // 异响鸣的邀请（速攻：①卡组特召 / ②1 入手 1 进额外）
            public const int Stage = 39210885;           // 天魔之声选器-『异响鸣琴』（场地：检索 + 指示物牛怪）
            public const int Shelter = 5605529;          // 异响鸣的选择（通常魔法：回血抽 / 扣血检索）
            public const int Versare = 42193638;         // 异响鸣的倒水（通常魔法）
            public const int Dissonance = 65496951;      // 异响鸣的不调和（通常魔法）
            public const int Ritsudo = 4582942;          // 律导之异响鸣（陷阱·狮鹫① 从墓地盖它）
            public const int StormDemon = 2815176;       // 异响鸣之神异-风暴恶魔（LINK1：代破 + 复制本家墓效）

            // ==================== 刻魔（刻まれし魔）====================
            public const int SoulCoffin = 2463794;       // 刻魔的镇魂棺（LINK1：解放拉红泪 / 墓地当装备）
            public const int Lacrimosa4 = 28803166;      // 红泪之魔 落泪（4★：召唤堆怜歌 / 墓地对手回合拉 LINK）
            public const int Renge = 26434972;           // 刻魔的怜歌（陷阱·墓效②：除外自身融合 1 只刻魔融合怪）
            public const int Lacrimosa6 = 46640168;      // 刻魔 落泪之日（6★融合：①特召墓地光恶魔）
            public const int DaiSeikan = 49867899;       // 刻魔的大圣棺（LINK2：①墓地素材融合 / ②当装备给对象抗性）
            public const int RexTremende = 11464648;     // 刻魔 赫赫君王（9★融合：①丢 1 手堆光恶魔）
            public const int Demonsmith = 60764609;      // 刻印群魔的刻魔锻冶师（6★：手①检索 / 墓③自跳）
            public const int Eisei = 98567237;           // 刻魔的詠聖（通常魔法：拿光恶魔再丢 1 → 路里自跳）
            public const int Shinseikan = 32991300;      // 刻魔的神圣棺（LINK3：战阶拉墓地光恶魔 + 装备加攻）
            public const int Yama = 24269961;            // 破械神王 阎摩（LINK2：①检索破械 / 墓②破坏触发拉恶魔族）
            public const int Shyama = 88554436;          // 破械神 萨玛（6★：墓②炸自己 1 张卡自身特召）
            public const int Lurrie = 97651499;          // 魔轰神 路里（1★：被丢弃即自跳）

            // ==================== 雷火沸动（ライゼオル）====================
            public const int DuoDrive = 7511613;         // 雷火沸动油电双动机（R4：③拔 2 拿 2 只本家）
            public const int DeadNader = 34909328;       // 雷火沸动死旋爆震机（R4：①吸墓地 / ②对手发效果即炸）
            public const int Ice = 8633261;              // 内燃雷火沸动机（4★：召唤 → 卡组拉本家）
            public const int Sword = 35844557;           // 剑式阴极雷火沸动机（4★：②检索炎族·光）
            public const int Node = 72238166;            // 节式阳极雷火沸动机（4★：②拉墓地本家）
            public const int Exhaust = 34022970;         // 外燃雷火沸动机（4★：①送墓额外 1 只超量自跳）
            public const int EclipseTwins = 45852939;    // 蚀之双子（R4：只当外燃① 的费用与死旋的素材用）

            // ==================== 梦幻崩影 / 无光之影 / DDD ====================
            public const int Griffon = 65330383;         // 梦幻崩影·狮鹫（LINK4：封锁 + 盖魔陷 + 互连抽 1）
            public const int Goblin = 39064822;          // 梦幻崩影·哥布林（LINK2：①丢 1 手多 1 次通召 + 互连对象抗性）
            public const int AbaoAku = 4731783;          // 无光之影 阿-宝·阿·库（LINK4）
            public const int Kaiser = 79559912;          // DDD 怒涛大王 决策凯撒（R6：无卡名一回合一次，无效含特召的效果）

            // ==================== 额外里的其它 ====================
            public const int Nemure = 90590304;          // No.41 泥睡魔兽 睡梦貘（本副牌不主动出，见 AllowBlockedSummon）

            // ==================== 泛用 ====================
            public const int Terraforming = 73628505;    // 星球改造（检索场地）
            public const int TripleTactics = 25311006;   // 三战之才
            public const int TripleThrust = 35269904;    // 三战之号
            public const int CalledByGrave = 24224830;   // 墓穴的指名者（异画 24224831）
            public const int BlackGoat = 49299410;       // 嗤笑的黑山羊
            public const int Ash = 14558127;             // 灰流丽（异画 14558128）
            public const int MaxxC = 23434538;           // 增殖的G
            public const int Mulcharmy = 84192580;       // 欢聚友伴·抖抖海月水母
        }

        /// <summary>是否打印调试信息（命令行 ``Debug=true``，与基类同一套开关）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>
        /// 「梦幻崩影·狮鹫」的 A/B 档位（环境变量 `KZ_GRYPHON_ALWAYS=1`）：置 1 ＝ 内核只要给候选就出，
        /// 不再等"阎摩先在场上"。默认 0 ＝ 原来那条线（阎摩 → 狮鹫，互相连接）。
        /// 来由见 <see cref="LateLinkSummon"/> 里狮鹫那一支的注释。
        /// </summary>
        private static readonly bool GryphonAlways =
            System.Environment.GetEnvironmentVariable("KZ_GRYPHON_ALWAYS") == "1";

        /// <summary>
        /// 「梦幻崩影·哥布林」的 A/B 档位（环境变量 `KZ_GOBLIN_FREE=1`）：置 1 ＝ 不再要求
        /// 「刻印群魔的刻魔锻冶师」先在场上，只要场上有 ≥2 只可烧的恶魔族就出。
        /// 来由见 <see cref="LateLinkSummon"/> 里哥布林那一支的注释（对空白 35 次候选只出 1 次）。
        /// </summary>
        private static readonly bool GoblinFree =
            System.Environment.GetEnvironmentVariable("KZ_GOBLIN_FREE") == "1";

        /// <summary>
        /// 诊断输出（`KZ_DIAG=1` 时才打）：核对"内核问的效果序号 / 分支选项 / 选择目标"三条链路
        /// 跟脚本的假设对不对得上。**只在排查时开**，正常对局是关的（每步一行会刷屏）。
        /// </summary>
        private static readonly bool Diag =
            System.Environment.GetEnvironmentVariable("KZ_DIAG") == "1";

        public KezmoYixiangmingExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ⓪ 先摘掉基类那四条**笼统规则**（`CardId == -1`）：`SpSummon`（能做就做）、
            //    `Activate`（能发就发）、`SummonOrSet`（什么怪都通召）、`SpellSet`（能盖就盖）。
            //    它们的优先级会压过后面加的专属规则，所以必须清掉再按下面的顺序重加。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].CardId == -1)
                    Executors.RemoveAt(i);
            }

            // ============================================================
            // 一、手坑与阻抗：**只在连锁对手的动作时发**（教程"不要乱扔手坑"的意图闸门）
            // ============================================================
            AddExecutor(ExecutorType.Activate, CardId.Ash, HandTrapAsh);
            AddExecutor(ExecutorType.Activate, CardId.MaxxC, HandTrapMaxxC);
            AddExecutor(ExecutorType.Activate, CardId.Mulcharmy, HandTrapChainOnly);
            AddExecutor(ExecutorType.Activate, CardId.CalledByGrave, CalledByGraveActivate);

            // ============================================================
            // 二、异响鸣起手：手卡贴 P（教程第一节，全副牌的起点）
            // ============================================================
            AddExecutor(ExecutorType.Activate, CardId.AngelVoice, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.DevilVoice, AlwaysPlay);

            // ============================================================
            // 三、检索与展开件
            // ============================================================
            AddExecutor(ExecutorType.Activate, CardId.Stage, AlwaysPlay);          // 场地：检索 + 记号
            AddExecutor(ExecutorType.Activate, CardId.ChooserHime, AlwaysPlay);    // 选姬①：检索「异响鸣」
            AddExecutor(ExecutorType.Activate, CardId.Invitation, InvitationActivate); // 邀请（分支见 OnSelectOption）
            AddExecutor(ExecutorType.Activate, CardId.Shelter, AlwaysPlay);        // 选择
            AddExecutor(ExecutorType.Activate, CardId.Versare, AlwaysPlay);        // 倒水
            AddExecutor(ExecutorType.Activate, CardId.Dissonance, AlwaysPlay);     // 不调和
            AddExecutor(ExecutorType.Activate, CardId.Ritsudo, TrapActivate);      // 律导（陷阱，按基类时机）
            AddExecutor(ExecutorType.Activate, CardId.StormDemon, AlwaysPlay);     // 风暴恶魔（代破 / 复制墓效）

            // ============================================================
            // 四、刻魔轴（教程第一/二节）
            // ============================================================
            AddExecutor(ExecutorType.Activate, CardId.SoulCoffin, SoulCoffinActivate);   // 镇魂棺 ①②
            AddExecutor(ExecutorType.Activate, CardId.Renge, AlwaysPlay);                 // 怜歌②：墓地融合
            AddExecutor(ExecutorType.Activate, CardId.Lacrimosa4, AlwaysPlay);            // 红泪墓效（对手回合拉 LINK）
            AddExecutor(ExecutorType.Activate, CardId.Lacrimosa6, Lacrimosa6Activate);     // 落泪之日①（拉墓地红泪）/③（扣 1200）
            AddExecutor(ExecutorType.Activate, CardId.DaiSeikan, DaiSeikanActivate);       // 大圣棺 ①②
            AddExecutor(ExecutorType.Activate, CardId.RexTremende, AlwaysPlay);           // 赫赫君王①（丢 1 手堆光恶魔）/③（回收）
            AddExecutor(ExecutorType.Activate, CardId.Demonsmith, DemonsmithActivate);     // 锻冶师 ①②③
            AddExecutor(ExecutorType.Activate, CardId.Eisei, AlwaysPlay);                 // 詠聖①（拿路里再丢）/②（墓地融合）
            AddExecutor(ExecutorType.Activate, CardId.Yama, AlwaysPlay);                  // 阎摩①（检索萨玛）/②（墓地拉恶魔族）
            AddExecutor(ExecutorType.Activate, CardId.Shyama, ShyamaActivate);           // 萨玛①②（炸卡）
            AddExecutor(ExecutorType.Activate, CardId.Shinseikan, AlwaysPlay);            // 神圣棺①（拉墓地光恶魔 + 装备）
            AddExecutor(ExecutorType.Activate, CardId.AbaoAku, AlwaysPlay);               // 无光之影①
            AddExecutor(ExecutorType.Activate, CardId.Lurrie, AlwaysPlay);                // 路里（被丢即跳，一般自动）

            // ============================================================
            // 五、雷火沸动
            // ============================================================
            AddExecutor(ExecutorType.Activate, CardId.Ice, AlwaysPlay);                   // 内燃②（召唤 → 卡组拉本家）
            AddExecutor(ExecutorType.Activate, CardId.Sword, AlwaysPlay);                 // 剑式②（检索炎族·光）
            AddExecutor(ExecutorType.Activate, CardId.Node, AlwaysPlay);                  // 节式②（拉墓地本家）
            AddExecutor(ExecutorType.Activate, CardId.Exhaust, AlwaysPlay);               // 外燃②（检索）
            AddExecutor(ExecutorType.Activate, CardId.DuoDrive, AlwaysPlay);              // 油电双动机①（吸素材）/③（拔 2 拿 2）
            AddExecutor(ExecutorType.Activate, CardId.DeadNader, DeadNaderActivate);       // 死旋爆震机①②③
            AddExecutor(ExecutorType.Activate, CardId.EclipseTwins, AlwaysPlay);          // 蚀之双子（墓效：拉 R4 + 当素材）

            // ============================================================
            // 六、泛用魔法与陷阱
            // ============================================================
            AddExecutor(ExecutorType.Activate, CardId.Terraforming, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleTactics, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.TripleThrust, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.BlackGoat, BlackGoatActivate);

            // ============================================================
            // 六·五、主线第一步：通召起手件（教程每节的第一步）
            // ⚠ 2026-10-09 加：不单独排在这里的话，通召被压在规则表最后一节（八），
            //    只要场上先攒出别的素材，链接/超量的规则就把通召点让出去了。实测决策日志里
            //    选姬的通召被推到"贴 P → 发选择 → 发邀请 → 出镇魂棺 → 特召内燃"之后，
            //    而教程二/三/四节的第一步都是「通召选姬 / 通召恶魔之声」。
            // ============================================================
            AddExecutor(ExecutorType.SummonOrSet, CardId.ChooserHime, SummonStarter);
            AddExecutor(ExecutorType.SummonOrSet, CardId.DevilVoice, SummonStarter);

            // ============================================================
            // 七、额外卡组的召唤顺序（教程第二节的展开顺序，一条一条排队）
            // ============================================================
            AddExecutor(ExecutorType.SpSummon, CardId.SoulCoffin, AllowLinkSummon);   // 1. 镇魂棺（起手）
            AddExecutor(ExecutorType.SpSummon, CardId.DuoDrive, XyzSummon);          // 2. 油电双动机（选姬 + 红泪）
            AddExecutor(ExecutorType.SpSummon, CardId.DaiSeikan, AllowLinkSummon);   // 3. 大圣棺（落泪之日 + R4）
            AddExecutor(ExecutorType.SpSummon, CardId.Yama, AllowLinkSummon);        // 4. 阎摩（赫赫君王 + 大圣棺）
            AddExecutor(ExecutorType.SpSummon, CardId.Goblin, AllowLinkSummon);      // 5. 哥布林（锻冶师 + 路里）
            AddExecutor(ExecutorType.SpSummon, CardId.Griffon, AllowLinkSummon);     // 6. 狮鹫（阎摩 + 内燃 + 节式）
            AddExecutor(ExecutorType.SpSummon, CardId.Kaiser, XyzSummon);        // 7. 大怒涛（萨玛 + 锻冶师）
            AddExecutor(ExecutorType.SpSummon, CardId.DeadNader, XyzSummon);     // 8. 死旋爆震机（剑式 + 外燃）
            AddExecutor(ExecutorType.SpSummon, CardId.Shinseikan, LateLinkSummon); // 9. 神圣棺（收尾/斩杀）
            AddExecutor(ExecutorType.SpSummon, CardId.AbaoAku, LateLinkSummon);  // 10. 无光之影（收尾）
            AddExecutor(ExecutorType.SpSummon, CardId.StormDemon, LateLinkSummon);// 11. 风暴恶魔（需要 3 记号，很晚）
            // 雷火沸动四只的**手卡特召**（都带"只能从额外特召 4 阶"的自肃）→ 必须等链接段走完。
            AddExecutor(ExecutorType.SpSummon, CardId.Sword, RyzealSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.Exhaust, RyzealSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.Ice, RyzealSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.Node, RyzealSummon);
            // 蚀之双子 / 泥睡魔兽：本副牌不主动出（蚀之双子只当外燃① 的费用与死旋的素材）。
            AddExecutor(ExecutorType.SpSummon, CardId.EclipseTwins, AllowBlockedSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.Nemure, AllowBlockedSummon);
            // 灵摆召唤的入口：内核把 P 区的刻度卡放进"可特召"列表来表示"现在能灵摆召唤"，
            // 所以这两条必须放行（它们**不是**要特召这两张卡）。
            AddExecutor(ExecutorType.SpSummon, CardId.AngelVoice, AllowPendulumSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.DevilVoice, AllowPendulumSummon);

            // ============================================================
            // 八、通召（一回合只有一次，优先级排在这里：链接/超量机会都试过之后）
            // ============================================================
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));

            // ============================================================
            // 九、兜底发动（不属于上面任何一张的卡）；盖放只留给本家陷阱
            // ============================================================
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));
            Executors.Add(new CardExecutor(ExecutorType.SpellSet, -1, SpellSet));

            // ============================================================
            // 十、斩杀优先（插入到规则表最前面）
            // ============================================================
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));
            Executors.Insert(0, new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
        }

        // ==================================================================
        // 规则体
        // ==================================================================

        /// <summary>表里点名的卡一律"该出就出"（时机与目标交给内核与卡脚本）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>灰流丽：只在**连锁对手的卡**时发（基类判据自带 `LastChainPlayer == 1`）。</summary>
        private bool HandTrapAsh()
        {
            return DefaultAshBlossomAndJoyousSpring();
        }

        /// <summary>增殖的G：只在**对手回合**发（自己回合丢 G 等于白扔）。</summary>
        private bool HandTrapMaxxC()
        {
            return DefaultMaxxC();
        }

        /// <summary>
        /// 欢聚友伴·水母：只在**对手回合、且对手这一回合确实召唤过**之后才发。
        /// 卡文是"对手召唤/特召时抽 1"，而**发动条件本身只有"自己场上没有卡"**（`c84192580.lua` 的 `s.drcon`）
        /// ——内核在任何时点都可能问一次，原来"连锁对手的动作"会在对面还没出怪时（例如它先发动一张魔陷）
        /// 白扔一张。判据用基类的 `EnemySummonedThisTurn`（自己记的，见 `DefaultExecutor`：
        /// `Duel.LastSummonPlayer` 在连锁开始与阶段开始都会被重置成 -1，连锁里判不出来）。
        /// </summary>
        private bool HandTrapChainOnly()
        {
            // 档位见 `DefaultExecutor.MulcharmyWaitSummon`（默认＝等对手召唤过；A/B：环境变量 MULCHARMY_GATE）。
            return MulcharmyReady();
        }

        /// <summary>墓穴的指名者：对手的墓地里有怪、且在连锁对手的卡时才发（基类判据）。</summary>
        private bool CalledByGraveActivate()
        {
            return Duel.LastChainPlayer == 1 && Enemy.GetGraveyardMonsters().Count > 0;
        }

        /// <summary>陷阱类（律导之异响鸣）：走基类的陷阱时机，别在自己主要阶段空发。</summary>
        private bool TrapActivate()
        {
            return Duel.LastChainPlayer == 1 || Duel.LastChainPlayer == -1 && Duel.LastSummonPlayer == 1;
        }

        /// <summary>
        /// 「异响鸣的邀请」：① 从卡组特召 1 只本家（会给"非本家怪兽不能发效果"的自肃）；
        /// ② 场上有非灵摆的本家怪时，从卡组选 2 只卡名不同的本家灵摆怪，1 入手 1 表侧进额外
        ///（＝凑 3/5 刻度，教程第一节的关键一步）。分支在 <see cref="OnSelectOption"/> 里定。
        /// </summary>
        private bool InvitationActivate()
        {
            // ① 的特召对象只有「选姬」；② 需要场上有非灵摆的本家（选姬/风暴恶魔）。
            // 两个都合法时交给 OnSelectOption 按"还缺不缺刻度"选；这里只负责放行。
            return true;
        }

        /// <summary>
        /// 「刻魔的镇魂棺」：①（场上）解放自身 → 从卡组·手卡特召 1 只「刻魔」（红泪之魔 落泪）；
        /// ②（墓地/场上）把自己当装备给场上 1 只光恶魔族**非连接**怪（+600 攻，并让它能当怜歌② 的融合素材）。
        /// **② 只在墓地发动**：场上发动等于把 LINK1 的链接值换成一件装备，后面 阎摩/狮鹫 就没素材了。
        /// ⚠ 这里的 `== GetStringId(...)` 判定在触发型效果上是**拿不到 desc 的**（见 `IsEffect` 注释），
        /// 实际等效于"两条都放行"。没有改成 -1 判定是因为 ①/② **都能从场上问**，靠 desc 也分不开；
        /// 实测这段行为是对的（镇魂棺 ① 拉红泪 → 墓地装备 → 怜歌② 融合，链条走通），所以保持原样。
        /// </summary>
        private bool SoulCoffinActivate()
        {
            if (ActivateDescription == Util.GetStringId(CardId.SoulCoffin, 1))
                return Card.Location != CardLocation.MonsterZone;
            return true;
        }

        /// <summary>
        /// 「刻魔的大圣棺」：①（场上）用**墓地**怪兽当融合素材，融合 1 只恶魔族融合怪（赫赫君王）；
        /// ②（墓地/场上）当装备给光恶魔族非连接怪（给对象抗性）。
        /// **② 只在墓地发动**：它的素材价值比一件装备高得多（教程用它 + 赫赫君王 出阎摩）。
        /// ⚠ 同 `SoulCoffinActivate`：触发型效果拿不到 desc，这条判定实际等效于"两条都放行"。
        /// 实测该放行是对的（大圣棺 ① 融合赫赫君王 / ② 墓地装备 都在用），保持原样只加说明。
        /// </summary>
        private bool DaiSeikanActivate()
        {
            if (ActivateDescription == Util.GetStringId(CardId.DaiSeikan, 1))
                return Card.Location != CardLocation.MonsterZone;
            return true;
        }

        /// <summary>
        /// 判断"内核这次问的是不是这张卡的第 `index` 个效果"。
        ///
        /// ⚠ **触发型效果的 `ActivateDescription` 常常是 `-1`**——实测诊断（`KZ_DIAG=1`）：
        /// `[诊断] 落泪之日 被询问：desc=-1`，而那一次问的正是 ①。所以判定必须写成
        /// "`-1` 也算"，只写 `== Util.GetStringId(...)` 会**永远不成立**：本文件里三个闸门
        ///（镇魂棺②／大圣棺②／落泪之日③）原来就是这么变成死代码的。后果最重的是 ③ —— 它会把
        /// 墓地的「刻魔的镇魂棺」洗回额外卡组，于是「大圣棺①」当场没有融合素材，
        /// 教程的后半条线（赫赫君王→阎摩→哥布林→狮鹫）整条断在这里。
        /// </summary>
        private bool IsEffect(int description, int cardId, int index)
        {
            return description == -1 || description == Util.GetStringId(cardId, index);
        }

        /// <summary>
        /// 「刻魔 落泪之日 6★」：①（融合召唤成功）把墓地/除外的 1 只光·恶魔族特召或加手；
        /// ③（进墓）把墓地 1 只光·恶魔族洗回卡组/额外换 1200 伤害。
        /// **③ 要挑时机**：它的费用会把墓地一只光恶魔族洗回去，而墓地里的「镇魂棺」正是
        /// 大圣棺① 融合赫赫君王的必备素材——实测（`temp/rounds/agent-kezmo.log` 第 4 回合）
        /// 6★ 一进墓就把镇魂棺洗回额外，紧接着大圣棺① 无素材可用，整条线断。
        /// 所以只在"墓地里躺着便宜的光恶魔"（大圣棺/神圣棺/路里）时才换这 1200。
        /// ⚠ ① 与 ③ 都是触发型、`ActivateDescription` 都可能是 `-1`，所以按**卡在哪**分：
        /// 在墓地＝问 ③，在场上＝问 ①（① 的拉谁由 `OnSelectCard` 的 `HintMsg.Target` 分支定）。
        /// </summary>
        private bool Lacrimosa6Activate()
        {
            bool asksThird = Card != null && Card.Location == CardLocation.Grave;
            if (!asksThird)
                asksThird = IsEffect(ActivateDescription, CardId.Lacrimosa6, 1)
                    && !IsEffect(ActivateDescription, CardId.Lacrimosa6, 0);
            bool allow = asksThird ? HasCheapLightFiendInGrave() : true;
            if (Diag)
                Logger.WriteLine("[诊断] 落泪之日 被询问：desc=" + ActivateDescription
                    + " 位置=" + (Card != null ? Card.Location.ToString() : "?")
                    + "（desc0=" + Util.GetStringId(CardId.Lacrimosa6, 0)
                    + " desc1=" + Util.GetStringId(CardId.Lacrimosa6, 1) + "）"
                    + " 判为=" + (asksThird ? "③进墓" : "①融合")
                    + " 墓地·除外的光恶魔=" + DescribeLightFiends()
                    + " → " + (allow ? "发" : "不发"));
            return allow;
        }

        /// <summary>诊断用：把墓地/除外里的光·恶魔族列出来（看"该拉谁"到底有没有得选）。</summary>
        private string DescribeLightFiends()
        {
            string text = "";
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (card == null || !card.IsMonster())
                    continue;
                if (!card.HasRace(CardRace.Fiend) || !card.HasAttribute(CardAttribute.Light))
                    continue;
                text += (text.Length > 0 ? "、" : "") + card.Name;
            }
            foreach (ClientCard card in Bot.Banished)
            {
                if (card == null || !card.IsMonster())
                    continue;
                if (!card.HasRace(CardRace.Fiend) || !card.HasAttribute(CardAttribute.Light))
                    continue;
                text += (text.Length > 0 ? "、" : "") + card.Name + "(除外)";
            }
            return text.Length > 0 ? text : "（无）";
        }

        /// <summary>
        /// 墓地里有没有"洗回去不心疼"的光·恶魔族（大圣棺 / 神圣棺 / 路里）。
        ///
        /// ⚠ **「刻魔的镇魂棺」不算**（2026-10-06 修）：它是「刻魔 赫赫君王」的融合素材之一
        ///（赫赫君王＝「刻魔」融合怪 ＋ **融合·连接怪**，而镇魂棺正是这副牌躺在墓地的那只连接怪）。
        /// 实测（`temp/train/diag-100.log`，对空白 20 局）：镇魂棺从墓地回额外 **15 次**，
        /// 而「刻魔的大圣棺」①（用墓地素材融合赫赫君王）**一次都没发动**——素材被 ③ 自己洗走了；
        /// 连锁反应是 阎摩（要"赫赫君王＋大圣棺"同时在场）只出 1 次、狮鹫 **0 次**，
        /// 教程的终场整条断在这。所以白名单只留"真的能回收再来"的那几张。
        /// </summary>
        private bool HasCheapLightFiendInGrave()
        {
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (card == null || !card.IsMonster())
                    continue;
                if (!card.HasRace(CardRace.Fiend) || !card.HasAttribute(CardAttribute.Light))
                    continue;
                if (card.IsCode(CardId.DaiSeikan) || card.IsCode(CardId.Shinseikan)
                    || card.IsCode(CardId.Lurrie))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「刻印群魔的刻魔锻冶师」：
        /// ①（手卡）丢自身 → 检索 1 张「刻魔」魔陷（优先「詠聖」，教程第二节"锻冶师①检索咏圣"）；
        /// ②（场上）把 1 张「刻魔」装备和场上 1 只怪送墓＝解场（教程第五节），只在对面有威胁时用；
        /// ③（墓地）洗回 1 只光恶魔族 → 自身特召（教程第八步）。
        /// </summary>
        private bool DemonsmithActivate()
        {
            if (ActivateDescription == Util.GetStringId(CardId.Demonsmith, 1))
            {
                // ② 解场要花掉一张「刻魔」装备 + 场上 1 只怪：只在对面站着我们拆不掉的怪时才做，
                // 而且优先牺牲墓地里那件镇魂棺（见 OnSelectCard 的解场偏好）。
                return Enemy.GetMonsters().Count > 0 && Bot.GetSpellCountWithoutField() > 0
                    && EnemyHasProblemMonster();
            }
            return true;
        }

        /// <summary>
        /// 「破械神 萨玛」：①（场上）炸自己 1 张卡、之后可再炸场上 1 张魔陷；
        /// ②（墓地）炸自己 1 张卡后自身特召。
        /// 两条都要**牺牲自己的一张卡**去触发「阎摩墓②」（教程第二节最后一步），
        /// 所以只在"阎摩已经在墓地 + 还有锻冶师能拉"时才放行；② 另外允许用来特召一个 6 星身体。
        /// </summary>
        private bool ShyamaActivate()
        {
            bool triggerYama = Bot.HasInGraveyard(CardId.Yama)
                && (Bot.HasInGraveyard(CardId.Demonsmith) || BotHasOnField(CardId.Demonsmith));
            if (Card.Location == CardLocation.Grave)
                return triggerYama;
            // 场上的 ① 是解场：对面有威胁时才炸自己的卡换对面的魔陷。
            return EnemyHasProblemMonster();
        }

        /// <summary>
        /// 「DDD 怒涛大王 决策凯撒」：**没有卡名一回合一次**，对手发动含特召的效果时拔素材无效并破坏
        ///（2 素材＝2 次）。只在对手连锁里发动。
        /// </summary>
        private bool DeadNaderActivate()
        {
            if (ActivateDescription == Util.GetStringId(CardId.DeadNader, 1))
                return Duel.LastChainPlayer == 1;      // ② 的意图闸门：只连锁对手的卡
            return true;
        }

        /// <summary>
        /// 「嗤笑的黑山羊」：宣言一个卡名，双方都不能特召/发动该卡的怪兽效果。
        /// 只在**对手回合**发动（自己回合宣言等于给自己上锁）；宣言对象见 <see cref="OnAnnounceCard"/>。
        /// </summary>
        private bool BlackGoatActivate()
        {
            return Duel.Player == 1;
        }

        // ==================================================================
        // 额外卡组：召唤闸门
        // ==================================================================

        /// <summary>本次正在被内核询问的额外怪（给素材偏好用，见 OnSelectCard）。</summary>
        private ClientCard _pendingSummon;

        /// <summary>记录"这一步要出的是哪只"，供 <see cref="OnSelectCard"/> 的素材选择用。</summary>
        private bool RecordSummon()
        {
            _pendingSummon = Card;
            if (_verbose && Card != null)
            {
                // 顺带把**内核这一回合给的全部特召候选**打出来（照耀圣 `ExtraSummon` 的口径）：
                // 判"某只额外怪为什么从来不出"必须先分清"没进候选"（材料/条件不满足，卡组问题）
                // 与"进了候选但我们的闸门拦了"（脚本问题）。狮鹫（65330383）卡的就是这一格。
                string candidates = "";
                if (Duel.MainPhase != null)
                {
                    foreach (ClientCard candidate in Duel.MainPhase.SpecialSummonableCards)
                    {
                        if (candidate.Controller != 0)
                            continue;
                        candidates += (candidates.Length > 0 ? "、" : "") + candidate.Name;
                    }
                }
                Logger.WriteLine("[刻魔异响鸣] 出额外怪：" + Card.Name + "（链接值 " + Card.LinkCount
                    + "）｜第 " + Duel.Turn + " 回合｜玩家 " + Duel.Player
                    + "｜候选＝" + candidates);
            }
            return true;
        }

        /// <summary>超量召唤：闸门＝"素材到位"（否则先攒素材，别把该留的怪吃掉）。</summary>
        private bool XyzSummon()
        {
            if (Card == null)
                return false;
            if (Card.IsCode(CardId.DuoDrive))
            {
                // 油电双动机：教程是"6 星落泪之日特召红泪之后 选姬 + 红泪 叠"。
                // 太早出会把场上唯一的光恶魔族（恶魔之声）吃掉，后面镇魂棺/怜歌融合全部断。
                if (BotHasOnField(CardId.Lacrimosa6))
                    return RecordSummon();
                // 刻魔轴已经不可能再走（镇魂棺不在额外了）→ 放行，至少留个 R4 站场。
                return !BotExtraHas(CardId.SoulCoffin) && CountLevel4OnField() >= 2 && RecordSummon();
            }
            if (Card.IsCode(CardId.Kaiser))
            {
                // 大怒涛：萨玛 + 锻冶师（两只 6 星恶魔族）——教程最后的 R6。
                return CountLevel6FiendsOnField() >= 2 && RecordSummon();
            }
            if (Card.IsCode(CardId.DeadNader))
            {
                // 死旋爆震机：两只 4 星「雷火沸动」（剑式 + 外燃）。它自带的吸素材让终场变 3 素材。
                return CountLevel4RyzealOnField() >= 2 && RecordSummon();
            }
            return RecordSummon();
        }

        /// <summary>
        /// 链接召唤：额外的链接怪按教程顺序一条条排队，每条自带"素材到位"的闸门，
        /// 免得把后面要用的怪提前吃掉（比如把阎摩当哥布林的素材）。
        /// </summary>
        private bool AllowLinkSummon()
        {
            if (Card == null)
                return false;
            int id = Card.Id;
            if (id == CardId.SoulCoffin)
            {
                // 镇魂棺的素材必须是光·恶魔族（恶魔之声 / 天使之声 / 红泪）；一张都没有就别出。
                return CountLightFiendsOnField() >= 1 && RecordSummon();
            }
            if (id == CardId.DaiSeikan)
            {
                // 大圣棺：教程是"6 星落泪之日 + R4"。① 要能用墓地素材融合，前提是墓地里有镇魂棺；
                // 另外**必须有一只合适的第二素材**（油电双动机/天使/恶魔之声/选姬/红泪/路里…），
                // 否则基类会拿「破械神 萨玛」（6★，大怒涛的素材）当链接素材——萨玛不是融合/链接怪，
                // 大圣棺① 的融合条件当场凑不齐（实测 `temp/rounds/agent-kezmo.log` 第 4 回合）。
                return BotHasOnField(CardId.Lacrimosa6) && Bot.HasInGraveyard(CardId.SoulCoffin)
                    && CountDaiSeikanPartners() >= 1 && RecordSummon();
            }
            if (id == CardId.Yama)
            {
                // 阎摩：赫赫君王 + 大圣棺（两只恶魔族，其中一只是链接怪）。
                return BotHasOnField(CardId.RexTremende) && BotHasOnField(CardId.DaiSeikan) && RecordSummon();
            }
            if (id == CardId.Goblin)
            {
                // 哥布林：卡文是"卡名不同的怪兽 2 只"（链接值 2），① 自己回合连接召唤时丢 1 手
                // **追加一次通召**（互相连接还能抽 1）——是链里的加速件，也是教程终场名单里的一员
                // （终场：狮鹫 + 哥布林 + R6 大怒涛 + R4 死旋爆震机 + 律导）。
                // 原闸门要求「刻印群魔的刻魔锻冶师」**先在场上**（教程第八步就是"锻冶师 + 路里＝哥布林"）。
                //
                // ⚠ **A/B 开关**（环境变量 `KZ_GOBLIN_FREE=1`）：**只按卡文**——哥布林要求"卡名不同的怪兽 2 只"
                // （内核自己会判合法性），原闸门却额外要求"锻冶师在场 + 场上有 ≥2 只可烧的恶魔族"。
                // 可信口径下的实测：**它 T≤2 落位 0/20**、整套验收件平均只到 0.1/4 件
                // （`tools/first_turn_accept.py`/`max_board.py` 读 blankR-100.log）——而它是这副牌
                // 自报终场（狮鹫+哥布林+大怒涛+死旋爆震机）的一员、① 还送一次追加通召。
                // 置 1 ＝ 内核给候选就出（材料价值由内核的候选表把关）。
                if (GoblinFree)
                    return RecordSummon();
                // ⚠ 绝对不能拿「刻魔 落泪之日 6★」或赫赫君王当素材——
                // 实测（`temp/rounds/agent-kezmo-trace.log` 第 1 回合）它俩被吃了以后，
                // 大圣棺① 的融合素材当场消失，整条 赫赫君王 → 阎摩 → 狮鹫 → 大怒涛 全断。
                return BotHasOnField(CardId.Demonsmith) && CountGoblinMaterials() >= 2 && RecordSummon();
            }
            if (id == CardId.Griffon)
            {
                // 狮鹫：卡文是"卡名不同的怪兽 2 只以上"（链接值 4），① 盖回墓地 1 张魔陷、② 只要在场
                // 双方就不能发动"不在连接状态的特殊召唤怪兽"的效果——所以原闸门要求**阎摩先在场上**
                // （阎摩和它做互相连接，② 才不会锁到自己；这条线是 阎摩 → 狮鹫）。
                //
                // ⚠ **A/B 开关**（环境变量 `KZ_GRYPHON_ALWAYS`，与 `RAISEMOON_AB`/`MULCHARMY_GATE` 同款）：
                // 对空白 20 局的探针（`temp/train/blankR-100.log` 的"候选＝…"那一段）显示，
                // 内核 **7 次**把狮鹫放进合法候选，而每次都因为"阎摩还在场上/还没出来"被这道闸门拦下，
                // 改出了哥布林/大圣棺/镇魂棺这些链上的件。也就是说：拦它是对的（链优先），
                // 但"链断了的局里要不要用狮鹫当妥协场（2500 + 锁）"从来没量过。
                // 置 1 = 只要内核给候选就出（＝拿它当妥协场），量完再定默认。
                if (GryphonAlways)
                    return RecordSummon();
                return BotHasOnField(CardId.Yama) && Bot.GetMonsterCount() >= 3 && RecordSummon();
            }
            return RecordSummon();
        }

        /// <summary>
        /// 收尾用链接怪（神圣棺 / 无光之影 / 风暴恶魔）：素材价值高、时机晚，
        /// 只在"链接段已经走完"（哥布林/狮鹫/无光之影 已在场，或阎摩已经进墓）时放行。
        /// </summary>
        private bool LateLinkSummon()
        {
            if (Card == null)
                return false;
            if (!LinkStageDone() && !BotHasOnField(CardId.Griffon))
                return false;
            return RecordSummon();
        }

        /// <summary>
        /// 雷火沸动四只的**手卡特召**：脚本里都带自肃
        ///「这个效果特召后，自己不能从额外卡组把 4 阶以外的怪特召」。
        /// 所以必须等链接段（大圣棺/阎摩/哥布林/狮鹫）走完才能放行，否则后面全被内核拒绝；
        /// 反过来说，它们出来之后只能出 R4（大怒涛是 R6，也要排在它们前面）。
        /// </summary>
        private bool RyzealSummon()
        {
            if (Card == null)
                return false;
            // 教程里这一步在狮鹫/大怒涛之后：链接段没走完就先别跳。
            if (!LinkStageDone())
                return false;
            return RecordSummon();
        }

        /// <summary>本副牌不主动出的额外怪（蚀之双子只当费用/素材；泥睡魔兽不在线路上）。</summary>
        private bool AllowBlockedSummon()
        {
            return false;
        }

        /// <summary>
        /// 灵摆召唤的入口：内核把**刻度卡**放进"可特殊召唤"列表来表示"现在能灵摆召唤"，
        /// 所以这一条必须放行——它并不是要特召这两张卡本身（内核只会给合法动作，
        /// 而这副牌里没有别的效果能把它们从手卡/卡组特召出来）。
        /// ⚠ **不能判 `Location == PendulumZone`**：新规则下 P 区就是魔陷区最左/最右两格
        ///（`Util.GetPZone` 走 `SpellZone[0]/[4]`），按 PendulumZone 判会一次都不命中，
        /// 整条灵摆线（本副牌的核心）当场作废——实测第一版就是这么把 恶魔之声 卡在额外卡组的。
        /// </summary>
        private bool AllowPendulumSummon()
        {
            return Card != null && Card.HasType(CardType.Pendulum);
        }

        // ==================================================================
        // 通召 / 通用发动 / 盖放
        // ==================================================================

        /// <summary>手坑：留在手里才有用，绝不能通召/盖放上场。</summary>
        private static readonly int[] HandTraps =
        {
            CardId.Ash,          // 灰流丽（异画 14558128 由 CardExecutor 归一到主号）
            CardId.MaxxC,        // 增殖的G
            CardId.Mulcharmy,    // 欢聚聚伴·水母
            CardId.CalledByGrave,// 墓穴的指名者
            CardId.BlackGoat,    // 嗤笑的黑山羊（陷阱，但同理不该占怪兽区）
        };

        /// <summary>
        /// 通召优先级（一回合只有一次，教程给的通召点）：
        /// 「天魔之声选姬」（召唤即检索，整套牌的第一步）＞「内燃雷火沸动机」（哥布林① 的追加通召点，
        /// 召唤即从卡组拉「节式阳极」）＞ 天使/恶魔之声（P 区已经贴好时当 4 星身体）＞「刻魔锻冶师」。
        /// </summary>
        private static readonly int[] PreferredSummons =
        {
            CardId.ChooserHime,  // 选姬（① 检索「异响鸣」，教程每条线都从它开始）
            CardId.Ice,          // 内燃（哥布林① 追加通召 → 从卡组拉节式）
            CardId.DevilVoice,   // 恶魔之声（P 区满了就当 4 星身体，还能复制本家墓效）
            CardId.AngelVoice,   // 天使之声（同上）
            CardId.Demonsmith,   // 锻冶师（素材不够时的补点）
        };

        /// <summary>
        /// 主线的起手通召（教程一/二/三/四节的第一步都是"通召选姬 / 通召恶魔之声"）。
        /// 只放行这两张：别的怪（内燃、路里…）仍走最后那条通用 `SummonOrSet`，免得把
        /// 一回合一次的通召点浪费在它们身上（内燃的追加通召窗口由哥布林① 开，见通用规则里的闸门）。
        /// </summary>
        private bool SummonStarter()
        {
            if (Card == null || Card.Location != CardLocation.Hand)
                return false;
            if (Card.IsCode(CardId.ChooserHime))
                return true;
            if (Card.IsCode(CardId.DevilVoice))
                return !Bot.HasInHand(CardId.ChooserHime);   // 手上有选姬时先出选姬（它能检索）
            return false;
        }

        private bool SummonOrSet()
        {
            if (Card == null)
                return false;
            foreach (int id in HandTraps)
            {
                if (Card.IsCode(id))
                    return false;
            }
            // 走线时按"下一步要谁上场"挑，避免把通召点浪费在别的怪上。
            foreach (int id in PreferredSummons)
            {
                if (Card.IsCode(id))
                {
                    // 「内燃」只在哥布林① 的追加通召窗口里才是对的（这时场上已经有哥布林）。
                    if (id == CardId.Ice && !BotHasOnField(CardId.Goblin))
                        continue;
                    return true;
                }
            }
            return DefaultMonsterSummon();
        }

        /// <summary>
        /// 兜底发动：不属于上面任何一张的卡。原来的 `DefaultDontChainMyself` 会在"没有连锁"时
        /// 一路 true（自己主要阶段把所有能发的都发掉），这里只对**明确的坏时机**说不。
        /// </summary>
        private bool Activate()
        {
            if (Card != null)
            {
                if (Card.IsCode(CardId.Ash))
                    return HandTrapAsh();
                if (Card.IsCode(CardId.MaxxC))
                    return HandTrapMaxxC();
                if (Card.IsCode(CardId.Mulcharmy))
                    return HandTrapChainOnly();
            }
            return DefaultDontChainMyself();
        }

        /// <summary>
        /// 盖放：**只盖本家陷阱**，而且最多占 2 个魔陷区——刻魔的装备（镇魂棺/大圣棺/神圣棺）
        /// 要占魔陷区，盖满了 怜歌② 的融合素材就没地方放（教程里装备是融合素材）。
        /// </summary>
        private bool SpellSet()
        {
            if (Card == null || !Card.IsTrap())
                return false;
            if (!Card.IsCode(CardId.Ritsudo) && !Card.IsCode(CardId.Renge) && !Card.IsCode(CardId.BlackGoat))
                return false;
            return Bot.GetSpellCountWithoutField() < 2;
        }

        // ==================================================================
        // 素材 / 检索 / 丢弃：OnSelectCard 的偏好分支
        // ==================================================================

        /// <summary>
        /// 融合素材偏好（怜歌②、大圣棺①、詠聖② 都是"用场上/墓地的「刻魔」怪兽融合"）：
        /// 优先把镇魂棺 / 落泪之日 / 红泪这些刻魔件选进去（教程第一/二节）。
        /// </summary>
        private static readonly int[] FusionMaterialOrder =
        {
            CardId.SoulCoffin,   // 镇魂棺（LINK1，满足"融合·链接怪兽"那一半）
            CardId.Lacrimosa6,   // 落泪之日 6★（满足"刻魔融合怪兽"那一半）
            CardId.Lacrimosa4,   // 红泪之魔 落泪
            CardId.DaiSeikan,    // 大圣棺
            CardId.RexTremende,  // 赫赫君王
        };

        /// <summary>链接素材偏好（按"正在出哪只"分开，见 <see cref="_pendingSummon"/>）。</summary>
        private int[] LinkMaterialOrder()
        {
            int id = _pendingSummon == null ? 0 : _pendingSummon.Id;
            if (id == CardId.SoulCoffin)
                return new[] { CardId.DevilVoice, CardId.AngelVoice, CardId.Lacrimosa4, CardId.Demonsmith };
            if (id == CardId.DaiSeikan)
                return new[] { CardId.Lacrimosa6, CardId.DuoDrive, CardId.AngelVoice, CardId.DevilVoice,
                               CardId.ChooserHime, CardId.Lacrimosa4 };
            if (id == CardId.Yama)
                return new[] { CardId.RexTremende, CardId.DaiSeikan };
            if (id == CardId.Goblin)
                return new[] { CardId.Demonsmith, CardId.Lurrie, CardId.AngelVoice, CardId.DevilVoice,
                               CardId.Ice, CardId.Node };
            if (id == CardId.Griffon)
                return new[] { CardId.Yama, CardId.Ice, CardId.Node, CardId.Lurrie,
                               CardId.Demonsmith, CardId.DevilVoice, CardId.AngelVoice, CardId.ChooserHime };
            if (id == CardId.Shinseikan || id == CardId.AbaoAku)
                return new[] { CardId.SoulCoffin, CardId.DaiSeikan, CardId.Demonsmith, CardId.Lurrie,
                               CardId.AngelVoice, CardId.DevilVoice };
            if (id == CardId.StormDemon)
                return new[] { CardId.Lurrie, CardId.ChooserHime, CardId.Demonsmith,
                               CardId.AngelVoice, CardId.DevilVoice };
            // 兜底：先牺牲最不值钱的怪。
            return new[] { CardId.Lurrie, CardId.Ice, CardId.Node, CardId.ChooserHime,
                           CardId.AngelVoice, CardId.DevilVoice };
        }

        /// <summary>
        /// 超量素材偏好：油电双动机＝选姬 + 红泪（教程第二步）、大怒涛＝萨玛 + 锻冶师、
        /// 死旋爆震机＝剑式 + 外燃（这两只本来就是拿去当素材的）。
        /// </summary>
        private int[] XyzMaterialOrder()
        {
            int id = _pendingSummon == null ? 0 : _pendingSummon.Id;
            if (id == CardId.DuoDrive)
                return new[] { CardId.ChooserHime, CardId.Lacrimosa4, CardId.DevilVoice, CardId.AngelVoice };
            if (id == CardId.Kaiser)
                return new[] { CardId.Shyama, CardId.Demonsmith };
            if (id == CardId.DeadNader)
                return new[] { CardId.Sword, CardId.Exhaust, CardId.Ice, CardId.Node };
            return new[] { CardId.ChooserHime, CardId.AngelVoice, CardId.DevilVoice };
        }

        /// <summary>
        /// 丢弃费用偏好（天使/恶魔之声①、赫赫君王①、狮鹫①、哥布林①、詠聖① 都要丢 1 张手卡）：
        /// 先丢"进墓地反而更好用"的卡——怜歌（墓地才能融合）、萨玛（墓地才能自跳）、路里（被丢即跳）、
        /// 律导（狮鹫① 就是从墓地盖它）；手坑和本家动点尽量留。
        /// </summary>
        private static readonly int[] DiscardOrder =
        {
            CardId.Lurrie,       // 魔轰神 路里（被丢弃即自跳＝白赚一个身体，最适合当费用）
            CardId.Renge,        // 刻魔的怜歌（陷阱：要在墓地才能融合）
            CardId.Shyama,      // 破械神 萨玛（要在墓地才能自跳）
            CardId.Ritsudo,      // 律导之异响鸣（狮鹫① 从墓地盖）
            CardId.BlackGoat,    // 嗤笑的黑山羊（墓效）
            CardId.Shelter,      // 异响鸣的选择
            CardId.Versare,      // 异响鸣的倒水
            CardId.Dissonance,   // 异响鸣的不调和
            CardId.Terraforming, // 星球改造
            CardId.TripleTactics,
            CardId.TripleThrust,
            CardId.Demonsmith,   // 锻冶师（墓③ 能自跳，但手① 更值钱）
        };

        /// <summary>融合召唤要选"出哪一只融合怪"时的顺序：先赫赫君王，再 6 星落泪之日。</summary>
        private static readonly int[] FusionTargetOrder =
        {
            CardId.RexTremende,  // 刻魔 赫赫君王（9★，教程的融合目标）
            CardId.Lacrimosa6,   // 刻魔 落泪之日（6★）
        };

        /// <summary>额外卡组里的弃子（外燃① 的发动费用＝把额外 1 只超量送墓）：只认「蚀之双子」。</summary>
        private static readonly int[] ExtraFodderOrder =
        {
            CardId.EclipseTwins, // 蚀之双子（送墓后可以被死旋爆震机① 吸回来当素材）
            CardId.Nemure,       // No.41 泥睡魔兽（次选）
        };

        /// <summary>
        /// "洗回卡组/额外卡组"的费用顺序：**先洗额外卡组的怪**（镇魂棺有整场一次的限制、
        /// 洗回额外等于零代价；大圣棺回额外也不心疼），再轮到卡组里的光恶魔族。
        /// 教程第四节最后一步用的就是"回收墓地 LINK2 大圣棺"。
        /// </summary>
        private static readonly int[] ReturnToExtraOrder =
        {
            CardId.DaiSeikan,    // 刻魔的大圣棺（教程第四节的"回收墓地 LINK2 大圣棺"）
            CardId.Shinseikan,   // 刻魔的神圣棺
            CardId.Lurrie,       // 魔轰神 路里
            CardId.Demonsmith,   // 刻魔锻冶师（自己的③会把它排除，轮不到）
            CardId.SoulCoffin,   // 刻魔的镇魂棺（**排最后**：大圣棺① 还要用它当融合素材）
            CardId.Lacrimosa4,   // 红泪之魔 落泪
        };

        /// <summary>检索偏好（选姬① / 场地① / 邀请② 的"哪张进手"等）：按还缺哪一环动态排。</summary>
        private int[] SearchOrder()
        {
            List<int> order = new List<int>();
            if (!Bot.HasInHand(CardId.Invitation) && !Bot.HasInGraveyard(CardId.Invitation))
                order.Add(CardId.Invitation);          // 速攻「邀请」＝灵摆线的开关
            if (!BotHasOnField(CardId.ChooserHime) && !Bot.HasInHand(CardId.ChooserHime))
                order.Add(CardId.ChooserHime);         // 选姬（场地① 的默认检索目标、多一个 4 星）
            if (!BotHasOnField(CardId.Stage) && !Bot.HasInHand(CardId.Stage))
                order.Add(CardId.Stage);               // 场地（① 追加通召 + 检索）
            order.Add(CardId.AngelVoice);              // 3 刻度
            order.Add(CardId.DevilVoice);              // 5 刻度
            order.Add(CardId.Ritsudo);
            order.Add(CardId.Shelter);
            order.Add(CardId.Versare);
            order.Add(CardId.Dissonance);
            order.Add(CardId.StormDemon);
            return order.ToArray();
        }

        /// <summary>
        /// 现在该拿哪张「刻魔」魔陷（锻冶师①）：默认「詠聖」（拿路里再丢 → 路里自跳，教程第二节）；
        /// 但如果「怜歌」既不在墓地也不在手卡，那里才是融合的钥匙，先拿它。
        /// </summary>
        private int[] KezmoSpellTrapOrder()
        {
            List<int> order = new List<int>();
            if (!Bot.HasInGraveyard(CardId.Renge) && !Bot.HasInHand(CardId.Renge))
                order.Add(CardId.Renge);
            order.Add(CardId.Eisei);
            order.Add(CardId.Renge);
            return order.ToArray();
        }

        /// <summary>
        /// 光·恶魔族怪兽的取用顺序（赫赫君王① 的堆墓 / 锻冶师③ 的费用）：
        /// 教程第二节指定堆「锻冶师」（墓③ 能自跳，是链接段与 R6 的素材）。
        /// </summary>
        private static readonly int[] LightFiendOrder =
        {
            CardId.Demonsmith,   // 刻魔锻冶师（墓③ 自跳 + 是 R6 大怒涛的素材）
            CardId.Lurrie,       // 魔轰神 路里
            CardId.Lacrimosa4,   // 红泪之魔 落泪
        };

        /// <summary>「雷火沸动」检索顺序（油电双动机③ 拔 2 张拿 2 只）：剑式 + 内燃（教程第二/三节）。</summary>
        private static readonly int[] RyzealSearchOrder =
        {
            CardId.Sword,        // 剑式阴极（手卡自跳 + ②检索外燃）
            CardId.Ice,          // 内燃（召唤 → 卡组拉节式）
            CardId.Node,         // 节式阳极
            CardId.Exhaust,      // 外燃（剑式② 可以检索它，一般不用这里拿）
        };

        /// <summary>
        /// 墓地「异响鸣」魔陷的复制/除外偏好：
        /// 天使之声② 复制"回复"分支（配「倒水」）、恶魔之声② 复制"扣血"分支（配「选择」）——教程第三节。
        /// </summary>
        private int[] CopySourceOrder()
        {
            if (Card != null && Card.IsCode(CardId.AngelVoice))
                return new[] { CardId.Versare, CardId.Shelter, CardId.Dissonance, CardId.Invitation, CardId.Ritsudo };
            if (Card != null && Card.IsCode(CardId.DevilVoice))
                return new[] { CardId.Shelter, CardId.Versare, CardId.Dissonance, CardId.Invitation, CardId.Ritsudo };
            return new[] { CardId.Shelter, CardId.Versare, CardId.Dissonance, CardId.Ritsudo };
        }

        /// <summary>把两张偏好表拼起来用（去重），比如"先堆怜歌、再堆锻冶师"。</summary>
        private static int[] CombinedOrder(int[] first, int[] second)
        {
            List<int> merged = new List<int>();
            foreach (int id in first)
            {
                if (!merged.Contains(id))
                    merged.Add(id);
            }
            foreach (int id in second)
            {
                if (!merged.Contains(id))
                    merged.Add(id);
            }
            return merged.ToArray();
        }

        /// <summary>按偏好顺序从候选里挑 min..max 张（挑不满就用剩下的补足 min）。</summary>
        private IList<ClientCard> PickPreferred(IList<ClientCard> cards, int min, int max, int[] order)
        {
            List<ClientCard> selected = new List<ClientCard>();
            if (order != null)
            {
                foreach (int id in order)
                {
                    if (selected.Count >= max)
                        break;
                    foreach (ClientCard card in cards)
                    {
                        if (selected.Count >= max)
                            break;
                        if (card == null || selected.Contains(card))
                            continue;
                        if (card.IsCode(id))
                            selected.Add(card);
                    }
                }
            }
            foreach (ClientCard card in cards)
            {
                if (selected.Count >= min)
                    break;
                if (card == null || selected.Contains(card))
                    continue;
                selected.Add(card);
            }
            return selected;
        }

        /// <summary>
        /// **绝不能当素材烧掉的卡**：刻魔的融合体与场上的终端件。它们要么是后续融合的素材
        /// （大圣棺① 要墓地里的镇魂棺 + 落泪之日），要么是终场的阻抗（大怒涛/死旋爆震机/狮鹫）。
        /// 只在"补链接值"这种兜底补牌时排除；偏好表里点名要用它们时不受影响。
        /// </summary>
        private static readonly int[] NeverAsMaterial =
        {
            CardId.Lacrimosa6,   // 刻魔 落泪之日 6★（大圣棺① 的融合素材）
            CardId.RexTremende,  // 刻魔 赫赫君王（要跟大圣棺 出阎摩）
            CardId.DaiSeikan,    // 刻魔的大圣棺
            CardId.Shinseikan,   // 刻魔的神圣棺
            CardId.SoulCoffin,   // 刻魔的镇魂棺
            CardId.Lacrimosa4,   // 红泪之魔 落泪（是融合素材的一半）
            CardId.Yama,         // 破械神王 阎摩（要跟内燃+节式 出狮鹫）
            CardId.Goblin,       // 梦幻崩影·哥布林（要跟狮鹫互连）
            CardId.Griffon,      // 梦幻崩影·狮鹫
            CardId.Kaiser,       // DDD 大怒涛
            CardId.DeadNader,    // 死旋爆震机
            CardId.DuoDrive,     // 油电双动机（死旋爆震机的素材来源）
        };

        /// <summary>这张卡是不是"不能当素材烧掉"的关键件。</summary>
        private static bool IsNeverMaterial(ClientCard card)
        {
            foreach (int id in NeverAsMaterial)
            {
                if (card.IsCode(id))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 链接素材：除了偏好顺序，还要保证**链接值合计**够（阎摩是 LINK2，算 2 点）。
        /// 内核自己会校验；这里按"正在出的那只"的 LinkCount 补牌，补牌时**跳过关键件**。
        /// </summary>
        private IList<ClientCard> PickLinkMaterial(IList<ClientCard> cards, int min, int max)
        {
            List<ClientCard> selected = new List<ClientCard>(PickPreferred(cards, min, max, LinkMaterialOrder()));
            int rating = 0;
            foreach (ClientCard card in selected)
                rating += LinkRating(card);
            int target = _pendingSummon != null && _pendingSummon.LinkCount > 0 ? _pendingSummon.LinkCount : min;
            foreach (ClientCard card in cards)
            {
                if (rating >= target && selected.Count >= min)
                    break;
                if (card == null || selected.Contains(card) || selected.Count >= max)
                    continue;
                if (IsNeverMaterial(card))
                    continue;
                selected.Add(card);
                rating += LinkRating(card);
            }
            return selected;
        }

        /// <summary>链接值（非链接怪算 1）。</summary>
        private static int LinkRating(ClientCard card)
        {
            return card != null && card.HasType(CardType.Link) && card.LinkCount > 0 ? card.LinkCount : 1;
        }

        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (cards == null || cards.Count == 0)
                return null;
            // 没有选择余地（要的比候选还多）→ 全选，省得框架从尾部乱取。
            if (min > 0 && min >= cards.Count && (Duel.Phase != DuelPhase.BattleStart))
            {
                List<ClientCard> all = new List<ClientCard>();
                foreach (ClientCard card in cards)
                    if (card != null)
                        all.Add(card);
                if (all.Count > 0)
                    return all;
            }

            switch (hint)
            {
                case HintMsg.Discard:
                    return PickPreferred(cards, min, max, DiscardOrder);
                case HintMsg.ToGrave:
                {
                    // 送墓类要按"候选是什么"分开判：
                    // ① 额外卡组的超量怪 = 外燃① 的发动费用 → 只认「蚀之双子」；
                    // ② 「刻魔的怜歌」在候选里（＝还没进墓地）→ 先堆它（教程第一节：红泪之魔① 堆怜歌，
                    //    墓地怜歌② 才是融合 6 星落泪之日的钥匙，堆错了整条刻魔线当场断）；
                    // ③ 其余（赫赫君王① 的"堆 1 只光·恶魔族"）→ 堆「锻冶师」（墓③ 自跳 + R6 素材）。
                    bool anyMonster = false;
                    bool allExtra = true;
                    foreach (ClientCard card in cards)
                    {
                        if (card == null)
                            continue;
                        if (card.IsMonster())
                            anyMonster = true;
                        if (card.Location != CardLocation.Extra)
                            allExtra = false;
                    }
                    if (allExtra)
                        return PickPreferred(cards, min, max, ExtraFodderOrder);
                    int[] order = CombinedOrder(KezmoSpellTrapOrder(), LightFiendOrder);
                    if (!anyMonster)
                        order = KezmoSpellTrapOrder();
                    return PickPreferred(cards, min, max, order);
                }
                case HintMsg.Release:
                    return PickPreferred(cards, min, max, new[]
                    {
                        CardId.ChooserHime, CardId.Lurrie, CardId.AngelVoice, CardId.DevilVoice,
                    });
                case HintMsg.ToDeck:
                {
                    // 洗回卡组/额外卡组的费用：**优先把额外卡组的怪洗回额外**
                    //（镇魂棺有"整场只能特召一次"，洗回去等于零代价；大圣棺回到额外也不心疼）。
                    // ⚠ 这一条是「刻魔 落泪之日 6★」③（进墓时洗回 1 只光恶魔换 1200 伤害）与
                    // 「锻冶师」③（洗回 1 只光恶魔自身特召）共用的费用口：**不写这一条的话，
                    // 基类会从尾部随便抓**，实测就把墓地里的「镇魂棺」洗回卡组——
                    // 于是「大圣棺①」再也凑不出赫赫君王的融合素材（教程第四节那条线当场断）。
                    bool anyEnemy = false;
                    foreach (ClientCard card in cards)
                    {
                        if (card != null && card.Controller == 1)
                            anyEnemy = true;
                    }
                    if (anyEnemy)
                        return PickDestroyTarget(cards, min, max);
                    return PickPreferred(cards, min, max, ReturnToExtraOrder);
                }
                case HintMsg.FusionMaterial:
                    return PickPreferred(cards, min, max, FusionMaterialOrder);
                case HintMsg.XyzMaterial:
                case HintMsg.RemoveXyz:
                case HintMsg.DeattachFrom:
                    return PickPreferred(cards, min, max, XyzMaterialOrder());
                case HintMsg.LinkMaterial:
                    return PickLinkMaterial(cards, min, max);
                case HintMsg.AddToHand:
                case HintMsg.ReturnToHand:
                    return PickPreferred(cards, min, max, SearchOrder());
                case HintMsg.Remove:
                    // 除外费用（天使/恶魔之声② 除外墓地 1 张本家魔陷来复制效果）→ 见 CopySourceOrder。
                    return PickPreferred(cards, min, max, CopySourceOrder());
                case HintMsg.Equip:
                    // 装备对象（镇魂棺/大圣棺/神圣棺 给场上 1 只光恶魔族非链接怪）：
                    // 优先给「赫赫君王」（有装备就不受刻魔以外的效果影响＝伪全抗，教程第四节）。
                    return PickPreferred(cards, min, max, new[]
                    {
                        CardId.RexTremende, CardId.Lacrimosa4, CardId.Demonsmith, CardId.Lacrimosa6,
                    });
                case HintMsg.Target:
                {
                    // ⚠ **2026-10-09 补**：这条原来是漏的 → 掉进 `default`（按 DiscardOrder 挑），
                    // 于是「刻魔 落泪之日」① 去拉了「刻魔的镇魂棺」，教程要的 4 星身体
                    // 「红泪之魔 落泪」没回来，紧接着大圣棺①/油电双动机都缺素材、主线当场断。
                    // ① 的对象是"墓地·除外的光恶魔"，教程每一条线都靠它把红泪拉回来：
                    if (Diag)
                    {
                        string names = "";
                        foreach (ClientCard card in cards)
                            if (card != null)
                                names += (names.Length > 0 ? "、" : "") + card.Name + "(" + card.Location + ")";
                        Logger.WriteLine("[诊断] 取对象：候选=" + names + " min=" + min + " max=" + max
                            + " hint=" + hint + " → 命中 Target 分支");
                    }
                    return PickPreferred(cards, min, max, new[]
                    {
                        CardId.Lacrimosa4,     // 红泪之魔 落泪：4 星身体（R4 油电双动机 / 大圣棺 都等它）
                        CardId.Lacrimosa6,
                        CardId.Demonsmith,     // 锻冶师：墓③ 自跳 + R6 素材
                        CardId.Lurrie,         // 路里
                        CardId.SoulCoffin,     // 镇魂棺：只有前几张都没有时才拿它（它回场只是 1 点链接值）
                        CardId.DaiSeikan,
                    });
                }
                case HintMsg.Destroy:
                    return PickDestroyTarget(cards, min, max);
                case HintMsg.SpSummon:
                    // 这个 hint 有三种用途，按候选是哪一类分开处理：
                    // * 全是灵摆怪 → 灵摆召唤选"把哪几只摆上场"：**全选**（都是 4 星好身体）；
                    // * 全是额外卡组的融合怪 → 融合召唤选"出哪一只"（`procedure.lua` 里
                    //   `Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_SPSUMMON)` + `fusion_targets:Select`）：
                    //   **优先「刻魔 赫赫君王」**，别熔成第二只 6 星落泪之日；
                    // * 否则是「刻魔的镇魂棺」① 的"从卡组·手卡特召 1 只刻魔"：
                    //   教程要拉「红泪之魔 落泪」（它进墓地/场上才是 怜歌② 的融合素材），
                    //   抓成锻冶师整条 6 星落泪之日 → 大圣棺 → 赫赫君王 就断了。
                {
                    bool anyPendulum = false;
                    bool allExtra = true;
                    foreach (ClientCard card in cards)
                    {
                        if (card == null)
                            continue;
                        if (card.HasType(CardType.Pendulum))
                            anyPendulum = true;
                        if (card.Location != CardLocation.Extra)
                            allExtra = false;
                    }
                    if (anyPendulum)
                        return PickPreferred(cards, min, max, null);
                    if (allExtra)
                        return PickPreferred(cards, min, max, FusionTargetOrder);
                    return PickPreferred(cards, min, max, new[]
                    {
                        CardId.Lacrimosa4,   // 红泪之魔 落泪（镇魂棺① 的默认目标）
                        CardId.Demonsmith,   // 刻魔锻冶师（红泪不在卡组/手卡时的备选）
                    });
                }
                default:
                    return PickDefault(cards, min, max);
            }
        }

        /// <summary>
        /// 破坏/取对象类：候选里同时有双方卡片时**先选对面的**（基类从尾部取会炸掉自己场上的卡——
        /// 实测"自己发陷阱炸自己"）。对面的又优先选攻击力最高的怪、其次魔陷。
        /// </summary>
        private IList<ClientCard> PickDestroyTarget(IList<ClientCard> cards, int min, int max)
        {
            List<ClientCard> selected = new List<ClientCard>();
            List<ClientCard> enemy = new List<ClientCard>();
            List<ClientCard> mine = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card == null)
                    continue;
                if (card.Controller == 1)
                    enemy.Add(card);
                else
                    mine.Add(card);
            }
            // 对面的怪兽（攻高的先炸）→ 对面的魔陷 → 自己的（最不值钱的先）
            enemy.Sort((a, b) => b.GetAttackPower().CompareTo(a.GetAttackPower()));
            foreach (ClientCard card in enemy)
            {
                if (selected.Count >= max)
                    break;
                selected.Add(card);
            }
            if (selected.Count < min)
            {
                foreach (ClientCard card in mine)
                {
                    if (selected.Count >= min)
                        break;
                    selected.Add(card);
                }
            }
            return selected;
        }

        /// <summary>兜底：破坏/回收/回手这类没有专门分支的选择，尽量别动自己的关键件。</summary>
        private IList<ClientCard> PickDefault(IList<ClientCard> cards, int min, int max)
        {
            bool anyEnemy = false;
            foreach (ClientCard card in cards)
                if (card != null && card.Controller == 1)
                    anyEnemy = true;
            if (anyEnemy)
                return PickDestroyTarget(cards, min, max);
            return PickPreferred(cards, min, max, DiscardOrder);
        }

        // ==================================================================
        // 选项 / 是-否 / 灵摆召唤 / 表示形式
        // ==================================================================

        /// <summary>
        /// 分支选项：**按值匹配**（值＝`卡号*16 + aux.Stringid 序号`，见 `Util.GetStringId`），
        /// 不要按序号——序号不带卡号，一旦某次没被消费掉就会污染下一张卡的提问。
        /// </summary>
        public override int OnSelectOption(IList<int> options)
        {
            if (options == null || options.Count == 0)
                return base.OnSelectOption(options);

            // 「异响鸣的邀请」：① 卡组特召（值 id*16+1）/ ② 1 入手 1 进额外（值 id*16+2）。
            // 教程第一节走的是 ②——凑 3/5 刻度；已经有 2 张刻度在场（只是要给灵摆找个身体）时才走 ①。
            int pair = Util.GetStringId(CardId.Invitation, 2);
            int spSummon = Util.GetStringId(CardId.Invitation, 1);
            if (options.Contains(pair) && options.Contains(spSummon))
                return options.IndexOf(NeedPendulumScales() ? pair : spSummon);
            if (options.Contains(pair) && !NeedPendulumScales() && options.Contains(spSummon))
                return options.IndexOf(spSummon);

            // 「刻魔 落泪之日」①：`Duel.SelectOption(tp,1190,1152)` → 1152（特殊召唤）才是
            // "特召墓地的红泪之魔"（教程第一步的最后一下）；1190 是加入手卡。
            // ⚠ 2026-10-09：原来还要求 `options.Count == 2`，等于"选项表里多一个就不管了"；
            // 现在只要两个值都在就按 1152 走（多余的值只会是别处插进来的）。
            if (options.Contains(1152) && options.Contains(1190))
            {
                if (Diag)
                    Logger.WriteLine("[诊断] 落泪之日① 的选项表 → 选「特殊召唤」(1152)");
                return options.IndexOf(1152);
            }

            // 异响鸣三张魔法的两支：op1＝回血线、op2＝扣血线。
            // 教程第三节要的是**扣血线**（受 500 之后检索本家魔陷 / 把律导送墓 / 给 P 区恶魔加指示物），
            // 但**每一发都要自己掉 500 LP**——三张牌在长局里反复用会把自己扣死
            //（实测 `temp/rounds/agent-kezmo.log` 第 3 局：连扣 5 次 500，35 回合后把我方送走）。
            // 所以血线宽裕（>2500）才走扣血线，否则走回血线（+500 并把手卡洗回卡组底再抽 2）。
            bool lpSafe = Bot.LifePoints > 2500;
            foreach (int id in new[] { CardId.Shelter, CardId.Versare, CardId.Dissonance })
            {
                int damageBranch = Util.GetStringId(id, 2);
                int recoverBranch = Util.GetStringId(id, 1);
                if (options.Contains(damageBranch) && options.Contains(recoverBranch))
                    return options.IndexOf(lpSafe ? damageBranch : recoverBranch);
            }

            // 「三战之才」：id*16+0 抽 2 / id*16+1 牛怪 / id*16+2 看手牌丢 1。优先抽 2。
            int draw = Util.GetStringId(CardId.TripleTactics, 0);
            if (options.Contains(draw))
                return options.IndexOf(draw);

            // 「无光之影」：id*16+2 破坏场上 1 张卡（解场）/ id*16+3 除外自身拉光暗怪。优先解场。
            int abaoDestroy = Util.GetStringId(CardId.AbaoAku, 2);
            if (options.Contains(abaoDestroy))
                return options.IndexOf(abaoDestroy);

            return base.OnSelectOption(options);
        }

        /// <summary>P 区还不是 3/5 两个刻度 → 需要「邀请」的"1 入手 1 进额外"那一支。</summary>
        private bool NeedPendulumScales()
        {
            int counts = 0;
            for (int seq = 0; seq < 2; ++seq)
            {
                ClientCard scale = Util.GetPZone(0, seq);
                if (scale != null && scale.HasType(CardType.Pendulum))
                    ++counts;
            }
            return counts < 2;
        }

        /// <summary>
        /// 是-否：默认一律 yes（基类行为），只对"会白炸自己一张卡"的那一问说不。
        /// </summary>
        public override bool OnSelectYesNo(int desc)
        {
            // 「破械神王 阎摩」墓② 拉完人之后的可选破坏：`aux.Stringid(24269961,2)`。
            // 它是"破坏自己场上的 1 张卡"，教程里没这一步——白白少一张卡，答 no。
            if (desc == Util.GetStringId(CardId.Yama, 2))
                return false;
            return base.OnSelectYesNo(desc);
        }

        /// <summary>
        /// 灵摆召唤：全选（本副牌的灵摆怪只有 4 星的天使/恶魔之声，都是好身体，没有取舍）。
        /// </summary>
        public override IList<ClientCard> OnSelectPendulumSummon(IList<ClientCard> cards, int min, int max)
        {
            List<ClientCard> selected = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card != null && selected.Count < max)
                    selected.Add(card);
            }
            return selected;
        }

        /// <summary>
        /// 选格：链接怪要**互相连接**才有对象抗性（教程第三节摆位）——哥布林放正中央（←→ 箭头）、
        /// 狮鹫放在哥布林旁边（←→ 与它互指），阎摩放 z1/z3（↙↘ 指向中间）。
        /// 其余交给引擎默认（主怪兽区 z2→z1→z3→z0→z4→额外区）。
        /// </summary>
        public override int OnSelectPlace(int cardId, int player, CardLocation location, int available)
        {
            if (player != 0 || location != CardLocation.MonsterZone)
                return 0;
            if (cardId == CardId.Goblin && (available & Zones.z2) != 0)
                return Zones.z2;
            if (cardId == CardId.Griffon)
            {
                if (BotHasOnField(CardId.Goblin))
                {
                    if ((available & Zones.z3) != 0)
                        return Zones.z3;
                    if ((available & Zones.z1) != 0)
                        return Zones.z1;
                }
                if ((available & Zones.z3) != 0)
                    return Zones.z3;
            }
            if (cardId == CardId.Yama)
            {
                if ((available & Zones.z1) != 0)
                    return Zones.z1;
                if ((available & Zones.z3) != 0)
                    return Zones.z3;
            }
            return 0;
        }

        /// <summary>
        /// 表示形式：**只有"这一刀能让斩杀成立"时才改攻击表示**，其余交给基类。
        /// （这副牌很多怪蹲守备更好：选姬 1200/1200、油电双动机 2500/2000、
        /// 狮鹫 2500/170 都是"站场比打点重要"。）
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
                if (damage >= Enemy.LifePoints && damage > 0)
                    return CardPosition.FaceUpAttack;
            }
            return base.OnSelectPosition(cardId, positions);
        }

        /// <summary>宣言卡名（嗤笑的黑山羊）：报对面场上/墓地最多的怪兽名，认不出来就报最常见的泛用手坑。</summary>
        public override int OnAnnounceCard(IList<int> avail)
        {
            int best = 0;
            int bestCount = 0;
            for (int player = 1; player >= 0; --player)
            {
                ClientField field = Duel.Fields[player];
                foreach (ClientCard card in field.Graveyard)
                {
                    if (card == null || !card.IsMonster() || !avail.Contains(card.Id))
                        continue;
                    int count = 0;
                    foreach (ClientCard other in field.Graveyard)
                        if (other != null && other.Id == card.Id)
                            ++count;
                    foreach (ClientCard other in field.GetMonsters())
                        if (other != null && other.Id == card.Id)
                            ++count;
                    if (count > bestCount)
                    {
                        bestCount = count;
                        best = card.Id;
                    }
                }
            }
            if (best != 0)
                return best;
            if (avail.Contains(CardId.Ash))
                return CardId.Ash;
            return avail.Count > 0 ? avail[0] : 0;
        }

        // ==================================================================
        // 战阶闸门（教程：能拆墙就进战阶，别把回合空转成抽死）
        // ==================================================================

        private readonly HashSet<int> _summonedThisTurn = new HashSet<int>();

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
        /// 进战斗阶段的判据：
        /// * **能斩杀**（本回合能打出的血 ≥ 对面 LP）→ 进；
        /// * **主线还有下一步**（<see cref="ComboHasNextStep"/>）→ **不进**，先把场做完；
        /// * 其余（对面空场且打不死 / 对面有怪且能拆一只）→ 进。
        ///
        /// ⚠ 2026-10-09 修：原来只有第三档——对面有怪、我方能拆一只就进战阶。教程的主线是
        /// "先做完整场再打"，这条规则让它在**做到「刻魔 落泪之日」就掉头去打**：
        /// 实测决策日志（`Game/AI/Decks/` 那局的 `[诊断]`/落位记录）里 R4→大圣棺→赫赫君王→
        /// 阎摩→哥布林→狮鹫→大怒涛→死旋 一条都没走。所以加了第二档"还有事做就别进"。
        /// 反过来也不能写成"对面有怪就不进"：实测那样会被一只 0 攻的墙钉住几十回合、最后抽干判负。
        /// </summary>
        private bool LethalAvailable()
        {
            if (Duel.Player != 0 || Duel.MainPhase == null || !Duel.MainPhase.CanBattlePhase)
                return false;
            int damage = LethalDamage();
            if (damage > 0 && damage >= Enemy.LifePoints)
                return true;   // 真斩杀：什么时候都进
            if (ComboHasNextStep())
            {
                if (Diag)
                    Logger.WriteLine("[诊断] 主线还有下一步 → 先不进战阶");
                return false;
            }
            if (Enemy.GetMonsterCount() > 0)
                return CanBreakDefender();
            return damage > 0 && damage >= Enemy.LifePoints;
        }

        /// <summary>
        /// 教程的主线还有没有下一步可做（"今天还能接着做场就先别打"）。
        ///
        /// 只看**教程里真实存在的下一步**，不做泛化的"手上还有牌就不打"——后者会让它
        /// 在长局里一直做场、把战阶永远推后（对面是墙时又回到"抽干判负"的老毛病）。
        /// </summary>
        private bool ComboHasNextStep()
        {
            // 1) 起手件还在手上、这一回合还没通召 → 教程一/二/三/四节的第一步都还没走
            if (!_summonedThisTurn.Contains(CardId.ChooserHime) && !_summonedThisTurn.Contains(CardId.DevilVoice))
            {
                if (Bot.HasInHand(CardId.ChooserHime) || Bot.HasInHand(CardId.DevilVoice)
                    || Bot.HasInHand(CardId.AngelVoice))
                    return true;
            }
            // 2) 「刻魔 落泪之日」在场上 + 墓地有「红泪之魔 落泪」/镇魂棺 + 额外还有大圣棺
            //    → 教程的"落泪之日 + 红泪 LINK2 大圣棺 → 融合赫赫君王"这一步还没走
            //
            // ⚠ 这里**不**看"手上有本家魔陷"：那些牌（邀请/选择/倒水）拿着不等于现在能落地，
            //    按它拦战阶会把"对面站着一只拆不掉的怪"的长局拖成几十回合不进攻（老毛病：
            //    被一只 0 攻墙钉住、抽干判负）。只认版面与墓地里**已经成立**的下一步。
            if (BotHasOnField(CardId.Lacrimosa6) && ExtraHas(CardId.DaiSeikan)
                && (Bot.HasInGraveyard(CardId.Lacrimosa4) || Bot.HasInGraveyard(CardId.SoulCoffin)))
                return true;
            // 3) 场上两只 4 星 + 额外还有 R4 油电双动机 → 中段的叠放还没做
            int fourStar = 0;
            foreach (ClientCard monster in Bot.GetMonsters())
            {
                if (monster != null && monster.IsFaceup() && monster.Level == 4)
                    ++fourStar;
            }
            if (fourStar >= 2 && ExtraHas(CardId.DuoDrive))
                return true;
            // 4) 墓地里躺着「刻印群魔的刻魔锻冶师」而它还能自跳（洗回 1 只光恶魔）→ 链接段还没完
            if (Bot.HasInGraveyard(CardId.Demonsmith) && HasCheapLightFiendInGrave()
                && (ExtraHas(CardId.Goblin) || ExtraHas(CardId.Yama) || ExtraHas(CardId.Griffon)))
                return true;
            return false;
        }

        /// <summary>额外卡组里还有没有这张（判"链接/超量段还剩谁没出"）。</summary>
        private bool ExtraHas(int cardId)
        {
            foreach (ClientCard card in Bot.ExtraDeck)
            {
                if (card != null && card.IsCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>我方有没有一只表侧攻击表示的怪能打得穿对面某只**表侧**的怪。</summary>
        private bool CanBreakDefender()
        {
            foreach (ClientCard attacker in Bot.GetMonsters())
            {
                if (attacker == null || !attacker.IsFaceup() || !attacker.IsAttack())
                    continue;
                foreach (ClientCard defender in Enemy.GetMonsters())
                {
                    if (defender == null)
                        continue;
                    if (!defender.IsFaceup())
                    {
                        // **里侧的未知怪**：按攻击力分档（≥2000 才敢撞），不许"一律不比"——
                        // 实测只比表侧怪时，对面整场只盖里侧（空白墙那种 0/2100 的盖卡）会出现
                        // "永远不进战斗阶段"，两边僵到 35 回合我方自己把自己耗死
                        //（`temp/rounds/agent-kezmo.log` 第 3 局 0 轮：对手 35 回合取胜）。
                        if (attacker.GetAttackPower() >= UnknownSetSafeAttack)
                            return true;
                        continue;
                    }
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
                if (card == null || !card.IsFaceup() || !card.IsAttack())
                    continue;
                if (_summonedThisTurn.Contains(card.Id))
                    continue;
                total += card.GetAttackPower();
            }
            return total;
        }

        /// <summary>为了斩杀把蹲着的守备怪转成攻击（对面空场时）。</summary>
        private bool ReposForLethal()
        {
            if (Duel.Player != 0 || Duel.MainPhase == null || !Duel.MainPhase.CanBattlePhase)
                return false;
            if (Card == null || !Card.IsFaceup() || !Card.IsDefense())
                return false;
            if (_summonedThisTurn.Contains(Card.Id))
                return false;
            if (Enemy.GetMonsterCount() == 0)
                return LethalDamage() + Card.GetAttackPower() >= Enemy.LifePoints;
            // 对面有怪时也要能"站起来打"：只要这只转攻击之后能拆掉对面某只（里侧按 2000 攻分档，
            // 与 <see cref="CanBreakDefender"/> 同一套口径）。少了这一条，蹲守备的怪会一直蹲着
            //（`LethalAvailable` 要求"表侧攻击表示"），对面只盖里侧时又变回僵局——
            // 实测 `temp/rounds/agent-kezmo.log` 第 6 局：41 回合只进过 2 次战斗阶段。
            int myAttack = Card.GetAttackPower();
            foreach (ClientCard defender in Enemy.GetMonsters())
            {
                if (defender == null)
                    continue;
                if (!defender.IsFaceup())
                {
                    if (myAttack >= UnknownSetSafeAttack)
                        return true;
                    continue;
                }
                int power = defender.IsAttack() ? defender.GetAttackPower() : defender.GetDefensePower();
                if (myAttack > power)
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 攻击目标：**里侧的未知怪要有 2000 攻才敢撞**（对面盖一张 2100 防的墙时，
        /// 1500 攻的剑式撞上去等于白送）。表侧的按"攻对攻 / 攻对守"比大小。都不行就直击。
        /// </summary>
        public override BattlePhaseAction OnSelectAttackTarget(ClientCard attacker, IList<ClientCard> defenders)
        {
            if (attacker == null || defenders == null)
                return null;
            foreach (ClientCard defender in defenders)
            {
                if (defender == null)
                    continue;
                if (defender.IsFacedown())
                {
                    // 里侧未知：攻击力不够就不撞（教程给的档位是 2000）。
                    if (attacker.GetAttackPower() >= UnknownSetSafeAttack)
                        return AI.Attack(attacker, defender);
                    continue;
                }
                int power = defender.IsAttack() ? defender.GetAttackPower() : defender.GetDefensePower();
                if (attacker.GetAttackPower() > power)
                    return AI.Attack(attacker, defender);
            }
            if (attacker.CanDirectAttack)
                return AI.Attack(attacker, null);
            return null;
        }

        /// <summary>打里侧未知怪需要的攻击力档位。</summary>
        private const int UnknownSetSafeAttack = 2000;

        // ==================================================================
        // 场上状态小工具
        // ==================================================================

        private ClientCard FindOnField(int cardId)
        {
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card != null && card.IsCode(cardId))
                    return card;
            }
            return null;
        }

        private bool BotHasOnField(int cardId)
        {
            return FindOnField(cardId) != null;
        }

        /// <summary>额外卡组里还有没有这张（判断"这条线还能不能走"）。</summary>
        private bool BotExtraHas(int cardId)
        {
            foreach (ClientCard card in Bot.ExtraDeck)
            {
                if (card != null && card.IsCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>场上表侧的光·恶魔族怪数量（镇魂棺的链接素材条件）。</summary>
        private int CountLightFiendsOnField()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card != null && card.IsMonster() && card.HasRace(CardRace.Fiend)
                    && card.HasAttribute(CardAttribute.Light))
                    ++count;
            }
            return count;
        }

        /// <summary>场上的 4 星怪数量（油电双动机的素材条件）。</summary>
        private int CountLevel4OnField()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card != null && card.IsMonster() && card.Level == 4)
                    ++count;
            }
            return count;
        }

        /// <summary>场上的 6 星恶魔族数量（大怒涛的素材条件）。</summary>
        private int CountLevel6FiendsOnField()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card != null && card.IsMonster() && card.Level == 6 && card.HasRace(CardRace.Fiend))
                    ++count;
            }
            return count;
        }

        /// <summary>场上的 4 星「雷火沸动」数量（死旋爆震机的素材条件）。</summary>
        private int CountLevel4RyzealOnField()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card != null && card.IsMonster() && card.Level == 4 && card.HasSetcode(0x1be))
                    ++count;
            }
            return count;
        }

        /// <summary>
        /// 大圣棺① 的"第二素材"候选：油电双动机（教程第二节指定）或一只便宜身体。
        /// 不能让基类拿「萨玛」「赫赫君王」「阎摩」去凑数（它们得留着走 R6/狮鹫）。
        /// </summary>
        private int CountDaiSeikanPartners()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card == null || !card.IsMonster())
                    continue;
                if (card.IsCode(CardId.Lacrimosa6))
                    continue;
                if (card.IsCode(CardId.DuoDrive) || card.IsCode(CardId.AngelVoice)
                    || card.IsCode(CardId.DevilVoice) || card.IsCode(CardId.ChooserHime)
                    || card.IsCode(CardId.Lacrimosa4) || card.IsCode(CardId.Lurrie)
                    || card.IsCode(CardId.Ice) || card.IsCode(CardId.Node)
                    || card.IsCode(CardId.Sword) || card.IsCode(CardId.Exhaust)
                    || card.IsCode(CardId.SoulCoffin))
                    ++count;
            }
            return count;
        }

        /// <summary>
        /// 场上"可以当哥布林素材"的恶魔族数量。**白名单**而不是黑名单：
        /// 只有这几张可以烧（锻冶师 + 路里/天使/恶魔之声/内燃/节式/选姬 这类便宜身体）。
        /// 「刻魔 落泪之日 6★」「赫赫君王」「大圣棺」「阎摩」这些必须留着走后面的线，
        /// 实测把它们当哥布林素材吃掉以后，大圣棺① 的融合素材当场没了（见 AllowLinkSummon 哥布林段）。
        /// </summary>
        private int CountGoblinMaterials()
        {
            int count = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card == null || !card.IsMonster() || !card.HasRace(CardRace.Fiend))
                    continue;
                if (card.IsCode(CardId.Demonsmith) || card.IsCode(CardId.Lurrie)
                    || card.IsCode(CardId.AngelVoice) || card.IsCode(CardId.DevilVoice)
                    || card.IsCode(CardId.ChooserHime) || card.IsCode(CardId.Ice)
                    || card.IsCode(CardId.Node) || card.IsCode(CardId.Exhaust)
                    || card.IsCode(CardId.Sword))
                    ++count;
            }
            return count;
        }

        /// <summary>
        /// 链接段是不是**已经走完**（决定雷火沸动那几只的手卡特召能不能放行，见 <see cref="RyzealSummon"/>）。
        /// 两条判据之一成立就算走完：
        /// ① 收尾件已经在场（哥布林/狮鹫/无光之影/神圣棺），或者阎摩已经进了墓地（说明链接段做过一轮）；
        /// ② 这条线**这一回合已经走不动了**（<see cref="LinkLineStillLive"/> 为假）——
        ///    否则轻则雷火沸动那几只永远不下场（终场少一个死旋爆震机），重则整局空过。
        /// </summary>
        private bool LinkStageDone()
        {
            if (BotHasOnField(CardId.Goblin) || BotHasOnField(CardId.Griffon)
                || BotHasOnField(CardId.AbaoAku) || BotHasOnField(CardId.Shinseikan)
                || Bot.HasInGraveyard(CardId.Yama))
                return true;
            return !LinkLineStillLive();
        }

        /// <summary>
        /// 链接/融合段还有没有下一步可走。**判据都看"眼前还差什么"**，不看历史：
        /// * 「刻魔的怜歌」还在墓地＝融合 6 星落泪之日 随时能做（用完会除外自身，见脚本 bfgcost）；
        /// * 6 星落泪之日 + 墓地镇魂棺＝大圣棺① 还能融合赫赫君王；
        /// * 大圣棺在场＝它自己就是链接素材（阎摩）或还能融合；
        /// * 赫赫君王 + 大圣棺＝阎摩；锻冶师 + 便宜恶魔族＝哥布林；阎摩 + 3 只怪＝狮鹫。
        /// </summary>
        private bool LinkLineStillLive()
        {
            if (Bot.HasInGraveyard(CardId.Renge))
                return true;
            if (Bot.HasInGraveyard(CardId.Eisei) && CountLightFiendsOnField() >= 2)
                return true;
            if (BotHasOnField(CardId.Lacrimosa6) && Bot.HasInGraveyard(CardId.SoulCoffin))
                return true;
            if (BotHasOnField(CardId.DaiSeikan))
                return true;
            if (BotHasOnField(CardId.RexTremende) && Bot.GetMonsterCount() >= 2)
                return true;
            if (BotHasOnField(CardId.Demonsmith) && CountGoblinMaterials() >= 2)
                return true;
            if (BotHasOnField(CardId.Yama) && Bot.GetMonsterCount() >= 3)
                return true;
            return false;
        }

        /// <summary>对面有没有"我们打不过、必须解掉"的表侧怪。</summary>
        private bool EnemyHasProblemMonster()
        {
            int best = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card != null && card.IsMonster())
                    best = System.Math.Max(best, card.GetAttackPower());
            }
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (card != null && card.IsFaceup() && card.GetAttackPower() > best)
                    return true;
            }
            return false;
        }
    }
}
