using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「耀圣」（エルフェンノーツ）+「狱神」小轴 专用执行器（卡组 #99，40 主 + 15 额外）。
    /// 实现依据＝插件仓库 `docs/tutorials/耀圣.md`（用户逐字给的教程），每一步都在下面引用章节。
    ///
    /// **这副牌为什么需要专属脚本**：本家几乎不占通常召唤，全靠"手卡自跳的 6 星 + 中央主要怪兽区"这套
    /// 节奏——通用脚本（`Test`/`DoEverythingExecutor`）不知道"中央区只有一个，腾不出来就接不下去"，
    /// 也不会把检索的目标挑成"下一块拼图"，所以对空白墙虽然能赢，却打不出任何本家 combo。
    ///
    /// **引擎（按卡脚本逐张核对过）**：
    /// * 三只 6 星（卢西娜 13597785 / 狄娜 59581480 / 福尔图娜 85976588）的① 都是
    ///   `EFFECT_SPSUMMON_PROC`＋`SetValue → 0, 0x4`，即**只能特召到中央主要怪兽区**（脚本 c13597785 第 41~45 行）。
    ///   中央区只有一个 → 这就是整副牌的核心约束：**每次只站一只，要接着下第二只必须先把它挪走**
    ///   （教程第一节列了四条腾中央区的路：蕾吉娜① 当 cost、回乡②、狂奏②、通常召唤/同调）。
    /// * 蕾吉娜（56651978）：① 是**手卡发动的快速效果**，代价＝把手卡·场上的「耀圣」卡送墓（除自己），
    ///   然后特召自己；② 是"**在中央区**召唤·特召成功时"从卡组特召 1 只本家（脚本第 52~56 行 `c:GetSequence()==2`）；
    ///   ④ 作为同调素材送墓时回收自身回手（教程第四节末尾的"手卡蕾吉娜"就是这个）。
    /// * 狱神精（12375297）：1 星调整。② 双方主要阶段、以**自己中央区**的怪兽为对象 +3 星，之后可立刻同调
    ///   （6+3=9，和它自己 1 星合 10 → 「鲜花女男爵」/「耀圣之诗」；教程第二、四节）。③ 作为同调素材送墓 → 检索 1 张本家。
    ///   ⚠ 它还在场时**封掉非同步的额外特召**（脚本 e1），所以要用它就别想着同时超量/连接。
    /// * 回乡之平行体（64491754）② 是**自己主要阶段的起动效果**：把 1 只怪兽送墓 → 从**卡组**守备特召 1 只
    ///   "原本属性不同"的本家；狂奏之狂想曲（24092792）② 是**快速效果**：把 1 只怪兽送墓 → 从**墓地**特召
    ///   1 只"原本属性不同"的本家（之后还能无效对面 1 张表侧卡）。两条路都是"腾中央区 + 补场"。
    /// * 狱神小轴：米底乌斯（97556336，Lv4 暗·天使）① 检索 1 只狱神（**可以选"加入手卡"或"直接特召"**，
    ///   是两个系统选项 1190/1152）；狱神门（25661743）堆 1 只狱神 + 检索暗·天使＝米底乌斯；
    ///   比托利姆（70488851，P 怪）的 P 效果破坏自身 → 从手卡·墓地特召 1 只狱神（教程第三节的两条线都靠它）；
    ///   朱诺白化精（10266279）② 除外卡组顶 3 张 → 破坏自身，从额外把「调狱神 朱诺拉」当作同调召唤特召。
    ///
    /// **这一层只做一件事：在"内核给出的合法动作"里排战术优先级**（`GameAI.OnSelectIdleCmd` 是
    /// "外层按注册顺序遍历规则、内层遍历候选，第一条说 yes 的获胜"，所以越靠前的规则越优先）。
    /// 合法性一律不在这里判：自肃、时点、中央区是否空着，全是内核＋卡脚本说了算。
    ///
    /// **动手改之前先读这几条踩过的坑**（与升辉月/虫惑魔/闪刀那几份执行器同一套结论）：
    /// 1. 基类那两条笼统规则（`SummonOrSet`/`Activate`，`CardId == -1`）**必须在加自己的规则之前摘掉**，
    ///    否则"能动就动"会把手坑、把不该发的效果全打出去；摘完再在最后补回带护栏的版本。
    /// 2. `Func` 返回 false **只跳过这一条规则**，不是否决这个动作——所以"禁止某行为"必须替换掉基类那条。
    /// 3. 手牌 ignition 的候选卡在框架里是 `Card`（`SetCard` 塞进来的 `ClientCard`），**不是** `CurrentEffect()`。
    /// 4. 分支选项要按**值**匹配（值＝`卡号*16+k`＝`Util.GetStringId`），别按序号、别用随机兜底：
    ///    某次登记没被消费掉（发动被无效）就会污染下一张卡的选项提问。
    /// 5. 基类 `OnSelectCard` 是**从候选尾部取**的 → "选对面 / 选最该选的"必须自己写；（`GameAI.OnSelectPlace`
    ///    的默认顺序是先 z2＝**中央区优先**，正好是这副牌要的，所以只要把"不想站中央的怪"排除掉即可。）
    /// </summary>
    [Deck("Yaosheng", "AI_Yaosheng", "Normal")]
    public class YaoshengExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            // ---- 主卡组：耀圣本家 ----
            public const int Lucina = 13597785;       // 耀圣之花诗 卢西娜（Lv6 炎·魔法师；手卡自跳中央区 + ①检索 + ③换位）
            public const int Dina = 59581480;         // 耀圣之波诗 狄娜（Lv6 水·魔法师；①把「回乡」从卡组表侧放后场）
            public const int Fortuna = 85976588;      // 耀圣之月诗 福尔图娜（Lv6 光·魔法师；①把「狂奏」从手卡·卡组表侧放后场）
            public const int Regina = 56651978;       // 耀圣之风诗 蕾吉娜（Lv6 风·魔法师；①自跳 + ②中央区拉卡组 + ④素材送墓回收自身）
            public const int Shinju = 12375297;       // 耀圣诗之狱神精（Lv1 调整；②中央区怪兽 +3 星并可立刻同调；③素材送墓检索本家）
            public const int Homecoming = 64491754;   // 耀圣之诗～回乡之平行体～（永续魔法：送墓 1 只怪兽 → 卡组守备特召原本属性不同的本家）
            public const int Rhapsody = 24092792;     // 耀圣之诗～狂奏之狂想曲～（永续陷阱：送墓 1 只怪兽 → 墓地特召原本属性不同的本家）
            // ---- 陆神小轴 ----
            public const int Medius = 97556336;       // 无垢者 米底乌斯（Lv4 暗·天使 0x1e8；①检索狱神；③墓地自跳（离场除外））
            public const int Bitlym = 70488851;       // 混绝狱神 比托利姆（P 怪 Lv12 暗·天使 0x1ce；P 效果破坏自身 → 手卡·墓地特召狱神）
            public const int Junold = 10266279;       // 狱神影精-朱诺白化精（P 怪 Lv10 暗·恶魔 0x1ce+0x1d8；②自爆 → 把「朱诺拉」当同调特召）
            public const int Terminus = 25661743;     // 绝解的狱神门-忒耳弥努斯（通常魔法：堆 1 只狱神 +（可）检索 1 只暗·天使）
            // ---- 额外卡组 ----
            public const int Junora = 5914858;        // 调狱神 朱诺拉（Lv10；同调召唤时无效对方全场表侧卡）
            public const int ElfenNotes = 5559570;    // 高傲自豪之耀圣诗-耀圣之诗（Lv10；②**在中央区**回额外 → 拉手卡·卡组·墓地本家）
            public const int Strelitzia = 42302563;   // 狱花之大耀圣 鹤望兰（Lv7；②拉手卡·墓地 6 星以下本家 +（可）全场降 3 星；站中央区 3000 攻）
            public const int Baronne = 84815190;      // 鲜花女男爵（Lv10；①炸 1 张卡 ②无效并破坏 ③准备阶段换人）
            public const int CrystalWing = 50954680;  // 水晶翼同调龙（Lv8；无效并破坏对手**怪兽**效果）
            public const int AccelStardust = 30983281; // 加速同调星尘龙（Lv8；特召成功时从墓地拉 1 只 2 星以下调整）
            public const int Aketis = 87188910;       // 饥鳄龙 古鱼龙（Lv9；同调召唤时抽 1）
            public const int Librarian = 90953320;    // 科技属 超图书馆员（Lv5；每次同调抽 1，教程第五节的中轴）
            public const int Punisher = 60465049;     // 念力终结处刑者（Lv11；教程第三节解场线的收尾）
            public const int Albion = 87746184;       // 烙印龍 阿爾比昂（融合 Lv8；这副牌没有融合手段 → 只当「落胤与圣女」的代价）
            public const int ChaosAngel = 22850703;   // 混沌之双翼（Lv10；教程第五节终场）
            public const int Ecclesia = 78397661;     // 黑龙之艾克莉西娅（Lv8；同调）
            public const int DiveBomber = 66122213;   // 地狱俯冲轰炸机（Lv7；破坏 + 伤害）
            public const int BlackRose = 73580472;    // 黑蔷薇龙（Lv7；**登场炸全场**——会连自己一起炸，本脚本不出它）
            public const int FALightning = 33158448;  // 方程式运动员 电光赛道名将（Lv7）
            // ---- 系统外 ----
            public const int Fallen = 30271097;       // 落胤与圣女（速攻：从额外送 1 只「阿不思」名怪兽 → 破坏场上 1 张表侧卡）
            public const int TripleTactics = 25311006; // 三战之才（三选一：抽 2 / 夺控制权 / 看手卡）
            public const int Thrust = 35269905;       // 三战之号（从卡组拿/盖 1 张通常魔法·通常陷阱）
            public const int Crossout = 65681983;     // 抹杀之指名者（宣言 1 个卡名，把卡组里的同名卡除外并无效对面）
            public const int MindForce = 66247039;    // 神圣心灵防护罩 -心灵之力-（通常陷阱：破坏 + 无效对面表侧卡）
            public const int AshBlossom = 14558128;   // 灰流丽
            public const int MaxxC = 23434538;        // 增殖的G
            public const int GhostBelle = 73642297;   // 屋敷童（异画号 73642296）
            public const int LockBird = 94145022;     // 小丑与锁鸟（异画号 94145021）
            public const int EffectVeiler = 97268403; // 效果遮蒙者（异画号 97268402）
            public const int Jellyfish = 84192580;    // 欢聚友伴·抖抖海月水母
            public const int Chickadee = 42141493;    // 欢聚友伴·茸茸长尾山雀
        }

        // ============================================================ 常量

        /// <summary>「耀圣」的 setcode（cards.cdb：四只 6 星、狱神精、两张永续魔陷都是 0x1d8＝472）。</summary>
        private const int SetcodeYaosheng = 0x1d8;
        /// <summary>「狱神」的 setcode（比托利姆/朱诺白化精/狱神精/朱诺拉是 0x1ce＝462）。</summary>
        private const int SetcodeJugami = 0x1ce;
        /// <summary>把灵摆怪兽放进灵摆刻度时内核给的 desc（strings.conf 的 1160）。</summary>
        private const int DescPendulumActivate = 1160;
        /// <summary>中央主要怪兽区（zone 2）的位掩码。</summary>
        private const int CenterZone = Zones.z2;

        // ============================================================ 静态名单

        /// <summary>手坑：留在手里才有用，绝不通召/盖放（基类的笼统通召规则会干这事，靠 <see cref="SummonOrSet"/> 拦）。</summary>
        private static readonly int[] HandTraps =
        {
            14558128,   // 灰流丽
            23434538,   // 增殖的G
            94145022,   // 小丑与锁鸟
            97268403,   // 效果遮蒙者
            73642297,   // 屋敷童
            84192580,   // 欢聚友伴·抖抖海月水母
            42141493,   // 欢聚友伴·茸茸长尾山雀
        };

        /// <summary>手卡的六星（可以自跳中央区的那三只）——"下一只要不要 6 星"的判据用。</summary>
        private static readonly int[] SixStars =
        {
            13597785,   // 卢西娜
            59581480,   // 狄娜
            85976588,   // 福尔图娜
        };

        /// <summary>额外卡组里**我们不会主动出**的：黑蔷薇龙登场会炸掉全场（连自己的场一起），
        /// 这副牌的终场（鲜花/水晶翼/耀圣之诗）比它稳，所以从出场名单里剔掉。
        /// 注意它**没有**专属 SpSummon 规则时会掉进基类那条笼统规则（`DefaultNoExecutor`＝"没人认领就我上"），
        /// 所以这里必须显式登记一条"永远不出"的规则（见构造函数 ⑨）。</summary>
        private static readonly int[] NeverSummonExtra =
        {
            73580472,   // 黑蔷薇龙
            87746184,   // 烙印龍 阿爾比昂（融合，本副牌没有融合手段）
        };

        // ============================================================ 状态

        /// <summary>这次"选卡"提问要按哪套策略挑（None＝没有待办，交给基类）。一次性：消费/超时都清掉。</summary>
        private PickKind _pick = PickKind.None;

        /// <summary>下一次选项提问要选的**值**（0＝没有待办）；值＝`卡号*16+k`，见 <see cref="AIUtil.GetStringId"/>。</summary>
        private int _optionValue;

        /// <summary>上面这条待办属于哪张卡（卡号）；0＝没有待办——提问里没有这张卡的值就作废，避免污染下一张卡。</summary>
        private int _optionCard;

        /// <summary>三战之号刚选出来的是哪张（决定接下来"盖放 / 加手"答哪个系统值）。</summary>
        private int _thrustPicked;

        /// <summary>抹杀之指名者这次要宣言的卡名（0＝没登记）。</summary>
        private int _announceCode;

        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>这次选卡提问要用的策略。</summary>
        private enum PickKind
        {
            None,
            YaoshengSearch,     // 卡组检索「耀圣」怪兽（卢西娜① / 蕾吉娜② / 米底乌斯①…）
            YaoshengSearchAny,  // 卡组检索「耀圣」任意卡（狱神精③：魔陷也能拿）
            JugamiSearch,       // 卡组检索「狱神」（米底乌斯① / 比托利姆③）
            DeckPlace,          // 从卡组把「回乡 / 狂奏」表侧放到后场（狄娜① / 福尔图娜①）
            TerminusSend,       // 狱神门：从卡组·额外送 1 只狱神去墓地
            ReginaCost,         // 蕾吉娜①：把手卡·场上的 1 张「耀圣」卡送墓（代价）
            HomecomingCost,     // 回乡②：把 1 只怪兽送墓（代价）
            HomecomingTarget,   // 回乡②：从卡组守备特召哪只本家
            RhapsodyCost,       // 狂奏②：把 1 只怪兽送墓（代价）
            RhapsodyTarget,     // 狂奏②：从墓地特召哪只本家
            BitlymTarget,       // 比托利姆的 P 效果：从手卡·墓地特召哪只狱神
            StrelitziaTarget,   // 鹤望兰②：从手卡·墓地特召哪只 6 星以下本家
            ElfenNotesTarget,   // 耀圣之诗②：从手卡·卡组·墓地特召哪几只本家
            JunoldTarget,       // 朱诺白化精②：从额外把「朱诺拉」叫出来
            FallenCost,         // 落胤与圣女：从额外送 1 只「阿不思」名怪兽去墓地（代价）
            ThrustPick,         // 三战之号：从卡组挑 1 张通常魔法·通常陷阱（并记下它，好回答"盖放/加手"）
            MediusCost,         // 米底乌斯③：把手卡·场上 1 只怪兽放回卡组（代价）
        }

        // ============================================================ 构造函数（规则＝优先级列表）

        public YaoshengExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ⓪ 先摘掉基类那两条笼统规则（`Activate` 是"任何卡能发就发"，`SummonOrSet` 是"任何怪都能通召"）。
            //    ⚠ 这一步**必须在加自己的规则之前**：IList 没有 RemoveAll，放后面会把刚加的专属规则一起删掉。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].Type == ExecutorType.SummonOrSet || Executors[i].Type == ExecutorType.Activate)
                    Executors.RemoveAt(i);
            }

            // ① 三只 6 星的手卡自跳（内核只在中央区空着时才会把它们放进"可特召"列表，所以这里一律点头）。
            //    顺序＝中央区先站谁：教程第四/五节的起手线都是从卢西娜或狄娜开始，卢西娜最自足
            //    （她一个人就能接出"蕾吉娜→狱神精→鲜花"），所以排最前。
            AddExecutor(ExecutorType.SpSummon, CardId.Lucina, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.Dina, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.Fortuna, AlwaysPlay);

            // ② "先做能生资源的事"：检索 / 把永续魔陷铺好。规则扫描是**按注册顺序**的，
            //    所以这几条一定排在"把场上的怪拿去同调"（③④）之前——不然卢西娜① 的检索会连着它自己一起被同调掉。
            AddExecutor(ExecutorType.Activate, CardId.Lucina, SearchYaosheng);       // 卢西娜①：检索本家怪（教程第四节的"检索蕾吉娜"）
            AddExecutor(ExecutorType.Activate, CardId.Dina, PlaceHomecoming);        // 狄娜①：贴「回乡之平行体」
            AddExecutor(ExecutorType.Activate, CardId.Fortuna, PlaceRhapsody);       // 福尔图娜①：贴「狂奏之狂想曲」
            AddExecutor(ExecutorType.Activate, CardId.Terminus, Terminus);           // 狱神门（教程第三节）
            AddExecutor(ExecutorType.Activate, CardId.Medius, Medius);               // 米底乌斯①/③
            AddExecutor(ExecutorType.Activate, CardId.Bitlym, Bitlym);               // 比托利姆：贴 P + P 效果自炸拉狱神

            // ③ 展开主体：蕾吉娜①（手卡自跳，代价＝送 1 张耀圣卡）→ 狱神精②（中央区 +3 星并立刻同调）
            AddExecutor(ExecutorType.Activate, CardId.Regina, ReginaSummon);
            AddExecutor(ExecutorType.Activate, CardId.Shinju, ShinjuLevelUp);
            AddExecutor(ExecutorType.Activate, CardId.Junold, Junold);

            // ④ 两条"腾中央区 + 补场"的永续魔陷（教程第一节的办法 2 与 3）
            AddExecutor(ExecutorType.Activate, CardId.Homecoming, Homecoming);
            AddExecutor(ExecutorType.Activate, CardId.Rhapsody, Rhapsody);

            // ⑤ 本家同调终端的效果（鹤望兰② 拉怪、耀圣之诗② 回收换人）
            AddExecutor(ExecutorType.Activate, CardId.Strelitzia, Strelitzia);
            AddExecutor(ExecutorType.Activate, CardId.ElfenNotes, ElfenNotes);
            AddExecutor(ExecutorType.Activate, CardId.Librarian, AlwaysPlay);

            // ⑥ 康：**只连锁对手**（水晶翼的脚本条件只要求"是怪兽效果"、鲜花② 只要求"可无效"，
            //    不对自己人设闸门的话，它们会把我们自己的效果无效掉）。
            AddExecutor(ExecutorType.Activate, CardId.Baronne, Baronne);
            AddExecutor(ExecutorType.Activate, CardId.CrystalWing, ChainEnemyOnly);
            AddExecutor(ExecutorType.Activate, CardId.Junora, AlwaysPlay);

            // ⑦ 系统外（各自带意图闸门，见各方法注释）
            AddExecutor(ExecutorType.Activate, CardId.Fallen, Fallen);
            AddExecutor(ExecutorType.Activate, CardId.TripleTactics, TripleTactics);
            AddExecutor(ExecutorType.Activate, CardId.Thrust, Thrust);
            AddExecutor(ExecutorType.Activate, CardId.Crossout, Crossout);
            AddExecutor(ExecutorType.Activate, CardId.MindForce, DefaultTrap);

            // ⑧ 额外卡组的出场顺位（注册顺序＝优先级）。教程的终端优先级：
            //    水晶翼同调龙 / 鲜花女男爵 / 高傲自豪之耀圣诗-耀圣之诗 / 混沌之双翼。
            //    ⚠ 实测（对空白 6 局）：把「耀圣之诗」排在鲜花前面时，10 星这一档全被它占了、
            //    「鲜花女男爵」一次都没出（`temp/rounds/agent-yaosheng.log` 第一版）。
            //    耀圣之诗 的 ② 要求它**站中央区**（`GetSequence()==2`），而额外召唤通常只能落额外怪兽区，
            //    条件经常不成立；鲜花则是万能康 + 3000 打点。所以这里把鲜花排前面（10 星优先出鲜花）。
            AddExecutor(ExecutorType.SpSummon, CardId.Baronne, ExtraSummon);      // Lv10：万能康
            AddExecutor(ExecutorType.SpSummon, CardId.ElfenNotes, ExtraSummon);   // Lv10：中央区回收换人，续航核心
            AddExecutor(ExecutorType.SpSummon, CardId.Junora, ExtraSummon);       // Lv10：登场擦对面全场表侧卡
            AddExecutor(ExecutorType.SpSummon, CardId.CrystalWing, ExtraSummon);  // Lv8：康怪兽效果
            AddExecutor(ExecutorType.SpSummon, CardId.Strelitzia, ExtraSummon);   // Lv7：拉怪 + 干扰
            AddExecutor(ExecutorType.SpSummon, CardId.Aketis, ExtraSummon);       // Lv9：抽 1
            AddExecutor(ExecutorType.SpSummon, CardId.AccelStardust, ExtraSummon);// Lv8：拉调整，接"星尘→古鱼龙"链
            AddExecutor(ExecutorType.SpSummon, CardId.Librarian, ExtraSummon);    // Lv5：同调抽卡，教程第五节的中轴
            AddExecutor(ExecutorType.SpSummon, CardId.Punisher, ExtraSummon);     // Lv11：解场线的收尾
            AddExecutor(ExecutorType.SpSummon, CardId.ChaosAngel, ExtraSummon);   // Lv10：光暗同调终场
            AddExecutor(ExecutorType.SpSummon, CardId.Ecclesia, ExtraSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.DiveBomber, ExtraSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.FALightning, ExtraSummon);
            // ⑨ 明确不出的额外怪（黑蔷薇龙会炸自己的场；阿爾比昂没有融合手段）
            foreach (int cardId in NeverSummonExtra)
                AddExecutor(ExecutorType.SpSummon, cardId, NeverPlay);

            // ⑩ 补回带护栏的通用通召（否决手坑）与通用发动（手坑/陷阱走原生判据），
            //    放在最后加＝在扫描顺序里排在①②③④⑤⑥ 之后，专属优先级不会被它们盖掉。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));

            // ⑪ 战阶闸门插到**最前面**：内核的动作循环是"外层遍历规则、内层遍历候选"，
            //    战斗阶段要等所有规则都不出手才轮得到；排在后面的话"还有事可做"会一直把战阶推后。
            //    判据见 BattleWorthwhile（**不是**"对面有怪就不进"）。
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, BattleWorthwhile));
        }

        // ============================================================ 顶层闸门

        /// <summary>无条件可用：只在"内核已经把这张卡放进合法动作列表"的前提下才会被问到。</summary>
        private static bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>正在出的那只额外怪（由 <see cref="ExtraSummon"/> 记录）：同调素材按它的等级凑。</summary>
        private ClientCard _pendingSynchro;

        /// <summary>
        /// 额外卡组的出场闸门（行为与 <see cref="AlwaysPlay"/> 相同）：**顺带打一条探针**
        /// （``Debug=true`` 时才有输出），把"这次内核给了哪些特召候选、我们命中了哪一张"记下来。
        ///
        /// 为什么需要它（2026-10-06 实测）：对空白 20 局里「高傲自豪之耀圣诗-耀圣之诗」**每局都出**（20/20），
        /// 而注册顺序排在它前面的「鲜花女男爵」只出 1/20——按"扫描＝优先级"的读法，鲜花只要在候选里就该它赢。
        /// 所以要么鲜花当时**不在候选**（材料/等级不满足），要么规则没按预期命中；这条探针就是拿来分辨这两者的
        ///（`Duel.MainPhase.SpecialSummonableCards` 是内核刚给的那份候选表）。
        /// </summary>
        private bool ExtraSummon()
        {
            _pendingSynchro = Card;     // 同调素材的挑选要用它的等级（见 PickSynchroMaterials）
            if (Card == null)
                return true;
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
            string extra = "";
            foreach (ClientCard card in Bot.ExtraDeck)
            {
                if (card == null || card.Id == 0)
                    continue;
                extra += (extra.Length > 0 ? "、" : "") + card.Name;
            }
            Logger.WriteLine("[探针] 额外召唤命中 " + Card.Name + "｜候选＝" + candidates + "｜额外卡组＝" + extra);
            return true;
        }

        /// <summary>永远不做（黑蔷薇龙这种会炸自己场的额外怪）。</summary>
        private static bool NeverPlay()
        {
            return false;
        }

        /// <summary>只连锁对手的动作（康专用）。<c>Duel.LastChainPlayer</c>：0＝我方、1＝对手、-1＝没有连锁。</summary>
        private bool ChainEnemyOnly()
        {
            return Duel.LastChainPlayer == 1;
        }

        /// <summary>通召入口：手坑一律否掉；只给"狱神轴的两只"留通召权。</summary>
        private bool SummonOrSet()
        {
            if (Card == null)
                return false;
            if (IsHandTrap(Card))
                return false;
            // 教程里通召只用在这两处：第三节"通召米底乌斯①检索比托利姆"、
            // 第一节办法 4"通召 1 星调整狱神精，直接和场上的本家同调"。
            // 四只 6 星**不通召**：它们本来就能从手卡自跳，而通召 6 星要解放（基类的 DefaultMonsterSummon
            // 会把刚铺的场当祭品吃掉）。
            return Card.IsOriginalCode(CardId.Medius) || Card.IsOriginalCode(CardId.Shinju);
        }

        /// <summary>通用发动入口（专属规则没接手的卡都到这里）。</summary>
        private bool Activate()
        {
            if (Card == null)
                return false;

            // 手坑的**时机**：走 WindBot 原生判据，不再"能发就发"（盲发的代价是白扔一张手牌）。
            if (IsHandTrap(Card))
            {
                if (Card.IsOriginalCode(CardId.MaxxC))
                    return DefaultMaxxC();                      // 只在对手回合
                if (Card.IsOriginalCode(CardId.AshBlossom))
                    return DefaultAshBlossomAndJoyousSpring();  // 只在连锁对手时
                if (Card.IsOriginalCode(CardId.GhostBelle))
                    return DefaultGhostBelleAndHauntedMansion();
                if (Card.IsOriginalCode(CardId.EffectVeiler))
                    return DefaultEffectVeiler();
                // 「小丑与锁鸟」是对称锁（双方都不能从卡组加手）：自己回合丢＝把自己的检索全锁死；
                // 两张「欢聚友伴」都写着"自己场上没有卡"＋"那个回合对手每次召唤抽 1"——只有对手回合才有意义。
                return Duel.Player == 1 && Duel.LastChainPlayer == 1;
            }

            // 陷阱走原生判据（对手召唤后 / 连锁对手 / 没有别的连锁）。
            if (Card.HasType(CardType.Trap))
                return DefaultTrap();

            // 现在能从额外卡组出怪 → 通用效果让路（专属规则在前面，不受影响）。
            // 这条是防止"该同调的时候去点了个无关的起效"。
            if (ExtraSummonAvailable())
                return false;

            return DefaultDontChainMyself();
        }

        /// <summary>
        /// 现在能不能从额外卡组出怪：内核把额外怪放进了这回合"可召唤/可特殊召唤"的列表里。
        /// 只在自己回合采信（对手回合那份列表是陈数据）。
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

        // ============================================================ 战阶闸门

        /// <summary>
        /// 该不该进战斗阶段。**不要**写成"对面有怪就不进战阶"——本项目实测那会导致打不动对面的墙、
        /// 双方一直抽卡，43 回合把自己抽干判负。分档：
        ///  ① 对面空场 → 我方有表侧攻击表示的怪就进（打脸是这副牌唯一的赢法）；
        ///  ② 对面有怪 → 我方有表侧攻击表示的怪能**拆掉**对面某只已知（表侧）的怪，就进；
        ///  ③ 对面只有里侧怪（未知）→ 攻击力 ≥ 2000 才打（里侧可能是任何东西；
        ///     本副牌 2200~3000 的中型终端拆得动 2100 的墙，2000 的蕾吉娜不去撞）。
        /// </summary>
        private bool BattleWorthwhile()
        {
            if (Duel.Player != 0)
                return false;
            if (Bot.GetMonsterCount() == 0)
                return false;

            List<ClientCard> mine = Bot.GetMonsters();
            List<ClientCard> theirs = Enemy.GetMonsters();

            if (theirs.Count == 0)
            {
                // 直击：只要有一个能打的就是纯赚（对面是空白墙时这就是主要输出）
                foreach (ClientCard attacker in mine)
                {
                    if (attacker.IsFaceup() && attacker.IsAttack() && attacker.Attack > 0)
                        return true;
                }
                return false;
            }

            foreach (ClientCard attacker in mine)
            {
                if (!attacker.IsFaceup() || !attacker.IsAttack() || attacker.Attack <= 0)
                    continue;
                foreach (ClientCard defender in theirs)
                {
                    if (defender.IsFaceup())
                    {
                        // 已知怪：拆得掉（打点严格大于它的守备力/攻击力）才值得进战阶
                        if (attacker.Attack > defender.GetDefensePower())
                            return true;
                    }
                    else if (attacker.Attack >= 2000)
                    {
                        // 未知里侧：按攻击力分档（<2000 不打）
                        return true;
                    }
                }
            }
            return false;
        }

        // ============================================================ 本家：检索 / 铺场

        /// <summary>
        /// 卢西娜①（场上的起动效果）：从卡组把 1 只「耀圣」**怪兽**加入手卡。
        /// 教程第四节："卢西娜在场 → 检索蕾吉娜，蕾吉娜① 把卢西娜送墓，蕾吉娜站中央区"——
        /// 所以默认就要蕾吉娜；已经拿到了就换成 1 星调整狱神精（那一步的下一步一定是同调）。
        /// </summary>
        private bool SearchYaosheng()
        {
            _pick = PickKind.YaoshengSearch;
            return true;
        }

        /// <summary>狄娜①：从卡组把「回乡之平行体」表侧表示放到自己后场（教程第一节办法 2）。</summary>
        private bool PlaceHomecoming()
        {
            _pick = PickKind.DeckPlace;
            return true;
        }

        /// <summary>福尔图娜①：把「狂奏之狂想曲」从手卡·卡组表侧表示放到自己后场（教程第一节办法 3）。</summary>
        private bool PlaceRhapsody()
        {
            _pick = PickKind.DeckPlace;
            return true;
        }

        /// <summary>
        /// 狱神门（通常魔法）：从卡组·额外送 1 只「狱神」去墓地，然后可检索 1 只暗·天使（＝米底乌斯）。
        /// 教程第三节的起点（单卡＝米底乌斯 + 朱诺白化精两张手卡）。**总是发动**：
        /// 送墓是这套牌的启动成本（狱神精进墓地才能被比托利姆/狂奏拉起来），检索是净 +1。
        /// ⚠ 代价是"这回合非狱神怪不能攻击"（脚本 e3），所以进战阶的判据要跟它错开——这里不额外处理：
        /// 内核会把不可攻击的怪排除出攻击列表，浪费的只是一个战阶。
        /// </summary>
        private bool Terminus()
        {
            _pick = PickKind.TerminusSend;
            return true;
        }

        /// <summary>
        /// 米底乌斯（Lv4 暗·天使，正常召唤权）：
        /// ①/②（召唤·特召成功，教程第三节"通召米底乌斯①检索比托利姆"）——
        ///   脚本 c97556336 第 40~52 行会先问"**加入手卡 / 特殊召唤**"（系统选项 1190 / 1152），
        ///   本副牌要的是**加入手卡**（比托利姆要贴到灵摆刻度才有效果，站到怪兽区就是个白板）。
        /// ③（墓地自跳：把手卡·场上 1 只怪放回卡组）——只在场上有余裕时才发（见 MediusSelfRevive）。
        /// </summary>
        private bool Medius()
        {
            if (Card != null && Card.Location == CardLocation.Grave)
                return MediusSelfRevive();
            _pick = PickKind.JugamiSearch;
            _optionCard = CardId.Medius;
            _optionValue = 1190;    // 1190＝"加入手卡"（另一个 1152 是"特殊召唤"，见卡脚本第 45 行）
            return true;
        }

        /// <summary>
        /// 米底乌斯③（墓地起动的自跳）：把 1 只怪兽从**手卡·场上放回卡组**才能跳回来
        /// （换来的是离场时除外）。这是"用一张手牌换一个 4 星场值"，只有确实还要接着做场时才值：
        /// 场上已经 2 只以上（有富余的怪可以回），或者手上有 2 只以上怪兽（回合结束留着也没用）。
        /// </summary>
        private bool MediusSelfRevive()
        {
            bool fieldHasSpare = Bot.GetMonsterCount() >= 2;
            int handMonsters = 0;
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.IsMonster())
                    ++handMonsters;
            }
            if (!fieldHasSpare && handMonsters < 2)
                return false;
            _pick = PickKind.MediusCost;
            return true;
        }

        /// <summary>
        /// 比托利姆（P 怪）：
        /// * desc 1160＝**放进灵摆刻度**：只有"要用它的 P 效果"时才放（教程第三节：贴 P 自炸再拉狱神）。
        ///   直接铺到怪兽区没有意义（它是"不能通常召唤"的特召怪，且我们从不特召它当打手）。
        /// * 正常发动＝P 效果，脚本 c70488851 第 62~73 行两个分支（值 k=1 拉狱神 / k=2 破坏自己＋给
        ///   12 星狱神 +5000 攻——本副牌没有第二只 12 星狱神，k=2 等于白炸自己）→ 只在 k=1 可行时发动。
        /// </summary>
        private bool Bitlym()
        {
            if (ActivateDescription == DescPendulumActivate)
            {
                // 放进刻度：手上有别的狱神可以拉，或者墓地已经有狱神 → 值得贴
                return HasJugamiInHandOrGrave();
            }
            // P 效果：只有"手卡·墓地里有狱神 + 怪兽区有空位"时才炸自己拉人
            if (!HasJugamiInHandOrGrave())
                return false;
            if (Bot.GetMonsterCount() >= 5)
                return false;
            _optionCard = CardId.Bitlym;
            _optionValue = Util.GetStringId(CardId.Bitlym, 1);   // k=1＝"从手卡·墓地特召 1 只狱神"
            _pick = PickKind.BitlymTarget;                       // 紧接着"拉哪只狱神"（优先 1 星调整狱神精）
            return true;
        }

        // ============================================================ 本家：展开主体

        /// <summary>
        /// 蕾吉娜①（**手卡**发动的快速效果）：把手卡·场上 1 张「耀圣」卡送墓（除自己）→ 特召自己。
        /// 教程第一节办法 1 与第四节：这是"把中央区的 6 星换成蕾吉娜"的那一步——
        /// 代价送的是卢西娜（中央区），蕾吉娜特召到中央区后 ② 才成立。
        /// 代价挑法见 <see cref="CostScore"/>（中央区的 6 星得分最低＝最先送）。
        /// </summary>
        private bool ReginaSummon()
        {
            _pick = PickKind.ReginaCost;
            return true;
        }

        /// <summary>
        /// 狱神精②（双方主要阶段，快速效果）：以**自己中央区**的怪兽为对象 +3 星，之后可立刻同调。
        /// 教程第二、四节：把 6 星的蕾吉娜变 9 星 → 1+9 同调「鲜花女男爵」；也能把 6 星变 9 → 10 星同调「耀圣之诗」。
        /// 闸门：
        ///  * 中央区必须是**本家 6 星**（或朱诺白化精这类能给同调凑数的 Lv≥6 怪）——狱神精自己在中央区时
        ///    目标就是它自己（+3 后 4 星，什么都凑不出），跳过；
        ///  * 中央区的怪如果是卢西娜且她这回合的检索还没用，**先搜再同调**（规则顺序：② 的检索规则排在 ③ 前面）。
        /// </summary>
        private bool ShinjuLevelUp()
        {
            ClientCard center = CenterMonster();
            if (center == null || !center.IsFaceup())
                return false;
            if (center.IsCode(CardId.Shinju))
                return false;       // 目标是它自己，凑不出同调
            if (center.Level < 6)
                return false;       // 6+3=9 才是"1+9=10"的那条线（教程第四节）
            if (!IsYaosheng(center) && !center.IsCode(CardId.Junold))
                return false;
            return true;
        }

        /// <summary>
        /// 朱诺白化精：
        /// * desc 1160＝**放进灵摆刻度**：贴上去之后，每次本家/狱神特召成功都能用它的 P 效果①
        ///   （自炸 + 抽 2 丢 1，自炸后③再从墓地·额外捡 1 张本家/狱神回手＝净 +1 张）。所以值得贴。
        /// * k=0＝上面那条 P 效果①本身：**一定发**（赚卡，没有额外代价）。
        /// * k=1＝场上的②（除外卡组顶 3 张 → 破坏自身，把「朱诺拉」当作同调召唤从额外特召）：
        ///   教程第三节的解场线。只在"对面场上有表侧的卡"时发——朱诺拉① 是"无效对方场上全部表侧卡"，
        ///   对面空场时这一发等于白除外自己 3 张卡。
        /// * k=2＝③（回额外时检索）是内核自己处理的触发，走到这里就点头。
        /// </summary>
        private bool Junold()
        {
            if (ActivateDescription == DescPendulumActivate)
                return true;
            if (ActivateDescription == Util.GetStringId(CardId.Junold, 0))
                return true;
            if (ActivateDescription != Util.GetStringId(CardId.Junold, 1))
                return true;
            if (!Bot.HasInExtra(CardId.Junora))
                return false;
            if (Bot.Deck.Count < 3)
                return false;
            // 对面**场上一张卡都没有**时才不发：除外自己卡组顶 3 张是实打实的代价，
            // 对面空场时既没有东西可无效、也不缺这一刀；对面有卡（哪怕只是盖着的墙）就换一个 3100/3800 的场面。
            if (!EnemyHasAnyCard())
                return false;
            _pick = PickKind.JunoldTarget;
            return true;
        }

        /// <summary>
        /// 回乡之平行体②（**自己主要阶段的起动效果**）：把 1 只怪兽送墓 → 从卡组守备特召 1 只"原本属性不同"的本家。
        /// 教程第一节办法 2 + 第四节："狄娜在场 → 用它②解除狄娜，把蕾吉娜特召到中央区"。
        /// 代价与目标要**配对挑**（代价的属性决定能拉谁），见 <see cref="PickHomecomingCost"/>。
        /// </summary>
        private bool Homecoming()
        {
            _pick = PickKind.HomecomingCost;
            return true;
        }

        /// <summary>
        /// 狂奏之狂想曲②（快速效果）：把 1 只怪兽送墓 → 从**墓地**特召 1 只"原本属性不同"的本家，
        /// 之后还能无效对面 1 张表侧卡（yes/no 由 OnSelectYesNo 答）。
        /// 教程第一节办法 3 + 第四节："把手卡 1 只怪兽送墓，从墓地特召狱神精，与福尔图娜同调"。
        /// ⚠ 卡脚本给的 hint timing 是 `TIMING_END_PHASE`，所以内核**可能只在结束阶段**把它放进
        /// 可发动列表（教程把它写在主要阶段里，实际能不能发以内核为准——这里只负责"内核给了就发"）。
        /// </summary>
        private bool Rhapsody()
        {
            _pick = PickKind.RhapsodyCost;
            return true;
        }

        /// <summary>
        /// 鹤望兰②（双方主要阶段，快速效果）：从手卡·墓地特召 1 只 6 星以下本家，
        /// 之后可选"场上全部 4 星以上怪兽降 3 星"（yes/no 见 OnSelectYesNo）。
        /// 教程第二节与第四节：主要是把狱神精从墓地拉回来，接着 7+1 出「水晶翼同调龙」。
        /// </summary>
        private bool Strelitzia()
        {
            _pick = PickKind.StrelitziaTarget;
            return true;
        }

        /// <summary>
        /// 耀圣之诗②（**必须在中央区**，双方主要阶段快速效果）：把自身回到额外卡组 →
        /// 从手卡·卡组·墓地特召本家（每个区域最多 1 只）。（教程第四节末"对手主要阶段发动"的那一发。）
        /// </summary>
        private bool ElfenNotes()
        {
            if (CenterMonster() == null || !CenterMonster().IsCode(CardId.ElfenNotes))
                return false;
            _pick = PickKind.ElfenNotesTarget;
            return true;
        }

        /// <summary>
        /// 鲜花女男爵：
        /// ①（起动）炸场上 1 张卡——脚本 c84815190 第 41~46 行的目标范围是**双方**的 ONFIELD，
        ///   必选 1 张：对面一张卡都没有时会炸到我们自己的卡（本项目的"台风炸自己"同款坑）。
        ///   所以要求对面场上有卡才发；真的是自己场上的卡被选中时由 OnSelectCard 的"先选对面"兜住。
        /// ②（康）只连锁对手；③（准备阶段换人）交给通用逻辑。
        /// </summary>
        private bool Baronne()
        {
            if (ActivateDescription == Util.GetStringId(CardId.Baronne, 1))
                return Duel.LastChainPlayer == 1;
            if (ActivateDescription == Util.GetStringId(CardId.Baronne, 0))
                return EnemyHasAnyCard();
            return true;
        }

        // ============================================================ 系统外

        /// <summary>
        /// 落胤与圣女：从额外送 1 只「阿不思」名怪兽去墓地（代价）→ 破坏场上 1 张表侧卡。
        /// 目标同样是**双方**的卡、1~1 必选 → 只在对面有表侧卡时才发（不然是"白送 1 张额外 + 炸自己"）。
        /// 送的额外怪由 <see cref="PickKind.FallenCost"/> 挑（先送打不出来的阿爾比昂）。
        /// </summary>
        private bool Fallen()
        {
            if (!EnemyHasFaceupCard())
                return false;
            _pick = PickKind.FallenCost;
            return true;
        }

        /// <summary>三战之才：三选一（值 k=0 抽 2 / k=1 夺控制权 / k=2 看手卡回卡组）→ 认 k=0。</summary>
        private bool TripleTactics()
        {
            _optionCard = CardId.TripleTactics;
            _optionValue = Util.GetStringId(CardId.TripleTactics, 0);
            return true;
        }

        /// <summary>
        /// 三战之号：从卡组拿/盖 1 张通常魔法·通常陷阱。它自己要求"对手这个回合发动过怪兽效果"
        /// （脚本 s.condition），所以只有后手才有得发——内核不满足就不会问我们。
        /// 拿哪张＋"盖放还是加手"见 OnSelectCard / OnSelectOption。
        /// </summary>
        private bool Thrust()
        {
            _thrustPicked = 0;
            _pick = PickKind.ThrustPick;
            return true;
        }

        /// <summary>
        /// 抹杀之指名者：宣言 1 个卡名 → 把卡组里的同名卡除外，无效对面那个卡名的效果。
        /// 闸门（**别盲发**）：只有"我们已经看见对手用过某张卡"（在对手墓地·除外区里躺着）、
        /// 且那张卡我们卡组里也有（脚本要求的宣告范围＝自己卡组里的卡名）时才发。
        /// 对手还没露过手牌/墓地时盲宣言＝白扔自己一张卡 + 除外自己卡组一张。
        /// </summary>
        private bool Crossout()
        {
            int code = KnownEnemyCodeWeRun();
            if (code == 0)
                return false;
            _announceCode = code;
            return true;
        }

        /// <summary>对手墓地·除外区里、我们卡组里也有的卡（＝抹杀之指名者能宣告并命中的目标）。</summary>
        private int KnownEnemyCodeWeRun()
        {
            foreach (ClientCard card in Enemy.Graveyard)
            {
                if (Bot.HasInDeck(card.Id))
                    return card.Id;
            }
            foreach (ClientCard card in Enemy.Banished)
            {
                if (Bot.HasInDeck(card.Id))
                    return card.Id;
            }
            return 0;
        }

        /// <summary>
        /// 同调素材：从内核给的那份候选里挑**刚好凑出目标等级**的一套，优先"最不心疼"的
        /// （打点总和最小；候选已经被内核按目标卡的素材条件过滤过，所以不用再判种族/属性）。
        ///
        /// **为什么必须接管数量**：基类 `OnSelectCard` 是 `for (i = 1; i <= max; ++i) cards[cards.Count - i]`
        /// ——**取满 max 张**，而同调的 max 常常是 5。多给一张，等级和就不等于要出的那只，
        /// 内核退回重问 → 这次同调白等。实测（对空白 20 局）「鲜花女男爵」只出 1 次，
        /// 而额外召唤探针显示它经常在合法候选里（`temp/train/probe-99.log`），
        /// 「水晶翼同调龙」却出得很多——差别就在素材张数能不能一次给对。
        ///
        /// 目标等级取 <see cref="_pendingSynchro"/>（＝刚被 <see cref="ExtraSummon"/> 放行的那只）。
        /// 张数从 `min` 往上试（先试"最少张数"，同调一般 2 张），组合数不大；一张都不合适就返回 null，
        /// 交回基类（宁可让它试，也不给一套内核不认的）。
        /// </summary>
        private IList<ClientCard> PickSynchroMaterials(IList<ClientCard> cards, int min, int max)
        {
            if (_pendingSynchro == null || _pendingSynchro.Level <= 0)
                return null;
            int target = _pendingSynchro.Level;
            List<ClientCard> pool = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card != null && card.Controller == 0 && card.Location == CardLocation.MonsterZone)
                    pool.Add(card);
            }
            if (pool.Count < min || min > 3)
                return null;        // 候选张数还不够，或下限就超过 3 张（这副牌没有那种同调）→ 不猜
            int limit = System.Math.Min(max, pool.Count);
            for (int size = min; size <= limit; ++size)
            {
                List<ClientCard> best = null;
                int bestScore = int.MaxValue;
                foreach (List<ClientCard> combo in Combinations(pool, size))
                {
                    int sum = 0;
                    int score = 0;
                    foreach (ClientCard card in combo)
                    {
                        sum += card.Level;
                        score += System.Math.Max(card.Attack, 0) / 100;
                    }
                    if (sum != target || score >= bestScore)
                        continue;
                    bestScore = score;
                    best = combo;
                }
                if (best != null)
                    return best;
            }
            return null;
        }

        /// <summary>从 <paramref name="pool"/> 里取 <paramref name="size"/> 张的全部组合（同调素材 ≤3 张，直接穷举）。</summary>
        private static List<List<ClientCard>> Combinations(List<ClientCard> pool, int size)
        {
            List<List<ClientCard>> result = new List<List<ClientCard>>();
            if (size <= 0 || size > pool.Count)
                return result;
            int[] picked = new int[size];
            for (int i = 0; i < size; ++i)
                picked[i] = i;
            while (true)
            {
                List<ClientCard> combo = new List<ClientCard>();
                foreach (int index in picked)
                    combo.Add(pool[index]);
                result.Add(combo);
                int position = size - 1;
                while (position >= 0 && picked[position] == pool.Count - size + position)
                    --position;
                if (position < 0)
                    break;
                ++picked[position];
                for (int i = position + 1; i < size; ++i)
                    picked[i] = picked[i - 1] + 1;
            }
            return result;
        }

        // ============================================================ 选卡（OnSelectCard）

        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (Duel.Phase == DuelPhase.BattleStart)
                return null;

            // ① 破坏/弹回/除外/无效这类"以场上的卡为对象"的提问：候选里**同时**有对面和我方的卡时，
            //   一律先选对面的。基类（DoEverythingExecutor）是从候选**尾部**取的，
            //   对面一张都没有时会选到我们自己的卡（本项目的台风/落胤与圣女都踩过这个坑）。
            //   纯我方的候选（检索、当代价）不会走到这里。
            if (min >= 1 && max >= 1 && IsEnemyTargetHint(hint))
            {
                List<ClientCard> enemies = new List<ClientCard>();
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 1 && enemies.Count < max)
                        enemies.Add(card);
                }
                if (enemies.Count >= min)
                    return enemies;
            }

            // ② 登记过的策略：一次性消费（拿不出东西就作废，别污染后面的提问）。
            PickKind kind = _pick;
            _pick = PickKind.None;
            if (kind != PickKind.None)
            {
                IList<ClientCard> picked = PickByPolicy(kind, cards, min, max);
                if (picked != null && picked.Count >= min)
                    return picked;
            }

            // ③ "丢弃手卡"（朱诺白化精 P 效果的抽 2 丢 1 等）：候选全在我方手里，
            //    基类从**尾部**取会把手上的关键件丢掉 → 用 CostScore 挑最不心疼的那张。
            // 同调素材（512）：**只挑"刚好凑出目标等级"的一套**，不是基类那样"从候选尾部取满 max 张"。
            // 基类的写法（`for i = 1..max: cards[cards.Count - i]`）在多给一张时等级和就不对，
            // 内核会退回重问，这次同调就白等了——实测对空白 20 局「鲜花女男爵」只出 1 次，
            // 而探针显示它经常**就在合法候选里**（`temp/train/probe-99.log`）。
            if (min >= 1 && max >= 1 && hint == HintMsg.SynchroMaterial)
            {
                IList<ClientCard> materials = PickSynchroMaterials(cards, min, max);
                if (materials != null)
                    return materials;
            }

            if (min >= 1 && max >= 1 && hint == HintMsg.Discard)
            {
                IList<ClientCard> cheap = PickCheapest(cards, min, max, CostScore);
                if (cheap != null)
                    return cheap;
            }

            // ④ 兜底：基类（从候选尾部取）。同调素材那些走 OnSelectSum，不经过这里。
            return base.OnSelectCard(cards, min, max, hint, cancelable);
        }

        /// <summary>"以场上的卡为对象"的提示（这些提示下候选是双方的，必须先选对面）。</summary>
        private static bool IsEnemyTargetHint(int hint)
        {
            return hint == HintMsg.Destroy
                || hint == HintMsg.ReturnToHand
                || hint == HintMsg.Remove
                || hint == HintMsg.Disable
                || hint == HintMsg.Faceup
                || hint == HintMsg.FaceupAttack
                || hint == HintMsg.FaceupDefense
                || hint == HintMsg.Target;
        }

        /// <summary>按登记的策略挑卡（返回 null＝这次不认，交给基类）。</summary>
        private IList<ClientCard> PickByPolicy(PickKind kind, IList<ClientCard> cards, int min, int max)
        {
            switch (kind)
            {
                case PickKind.YaoshengSearch:
                    return PickHighestByScore(cards, min, max, YaoshengSearchScore, CardLocation.Deck);
                case PickKind.YaoshengSearchAny:
                    return PickHighestByScore(cards, min, max, YaoshengSearchAnyScore, CardLocation.Deck);
                case PickKind.JugamiSearch:
                    return PickHighestByScore(cards, min, max, JugamiSearchScore, CardLocation.Deck);
                case PickKind.DeckPlace:
                    return PickByScore(cards, min, max, DeckPlaceScore);
                case PickKind.TerminusSend:
                    return PickHighestByScore(cards, min, max, TerminusSendScore, (CardLocation)0);
                case PickKind.ReginaCost:
                case PickKind.MediusCost:
                    return PickCheapest(cards, min, max, CostScore);
                case PickKind.HomecomingCost:
                {
                    // 代价的属性决定"能从卡组拉谁"（脚本要求原本属性不同）→ 两件事一起挑，见 PickPairingCost
                    IList<ClientCard> cost = PickPairingCost(cards, min, max, forGrave: false);
                    if (cost != null)
                        _pick = PickKind.HomecomingTarget;   // 紧接着就是"拉谁"的提问
                    return cost;
                }
                case PickKind.RhapsodyCost:
                {
                    IList<ClientCard> cost = PickPairingCost(cards, min, max, forGrave: true);
                    if (cost != null)
                        _pick = PickKind.RhapsodyTarget;     // 紧接着就是"从墓地拉谁"的提问
                    return cost;
                }
                case PickKind.HomecomingTarget:
                    return PickHighestByScore(cards, min, max, HomecomingTargetScore, CardLocation.Deck);
                case PickKind.RhapsodyTarget:
                    return PickHighestByScore(cards, min, max, RhapsodyTargetScore, CardLocation.Grave);
                case PickKind.BitlymTarget:
                    return PickHighestByScore(cards, min, max, JugamiSearchScore, (CardLocation)0);
                case PickKind.StrelitziaTarget:
                    return PickHighestByScore(cards, min, max, StrelitziaTargetScore, (CardLocation)0);
                case PickKind.ElfenNotesTarget:
                    return PickHighestByScore(cards, min, max, ElfenNotesTargetScore, (CardLocation)0);
                case PickKind.JunoldTarget:
                    return PickHighestByScore(cards, min, max, JunoldTargetScore, CardLocation.Extra);
                case PickKind.FallenCost:
                    return PickHighestByScore(cards, min, max, FallenCostScore, CardLocation.Extra);
                case PickKind.ThrustPick:
                {
                    IList<ClientCard> picked = PickHighestByScore(cards, min, max, ThrustScore, CardLocation.Deck);
                    if (picked != null && picked.Count > 0)
                        _thrustPicked = picked[0].Id;    // 后面那道"盖放/加手"的选项题要用
                    return picked;
                }
                default:
                    return null;
            }
        }

        // ------------------------------------------------------------ 打分（分数越高＝越先选）

        /// <summary>
        /// 「耀圣」检索（卢西娜① 只拿怪兽；蕾吉娜② 从卡组拉；耀圣之诗② 从卡组拉）：
        /// 顺序＝教程的"下一块拼图"——蕾吉娜（中枢）→ 狱神精（同调素材）→ 下一只 6 星 → 永续魔陷。
        /// </summary>
        private int YaoshengSearchScore(ClientCard card)
        {
            if (card.IsCode(CardId.Regina) && !HasAnywhereMine(CardId.Regina))
                return 100;
            if (card.IsCode(CardId.Shinju) && !HasAnywhereMine(CardId.Shinju))
                return 90;
            if (card.IsCode(CardId.Lucina) && !HasAnywhereMine(CardId.Lucina))
                return 80;
            if (card.IsCode(CardId.Dina) && !HasAnywhereMine(CardId.Dina))
                return 70;
            if (card.IsCode(CardId.Fortuna) && !HasAnywhereMine(CardId.Fortuna))
                return 60;
            return 10;
        }

        /// <summary>
        /// 「耀圣」任意卡检索（狱神精③：作为同调素材送墓时检索 1 张本家，**魔陷也能拿**）。
        /// 教程第四节：这一步拿的是"下一只 6 星"（狄娜），好让中央区空了以后接着跳；
        /// 蕾吉娜/狱神精还缺就先补它们，都有了再拿永续魔陷。
        /// 6 星之间的顺序给卢西娜略高（最自足：她一个人就能接出"检索蕾吉娜 → 蕾吉娜站中央区"）。
        /// </summary>
        private int YaoshengSearchAnyScore(ClientCard card)
        {
            if (card.IsCode(CardId.Regina) && !HasAnywhereMine(CardId.Regina))
                return 100;
            if (card.IsCode(CardId.Shinju) && !HasAnywhereMine(CardId.Shinju))
                return 90;
            if (card.IsCode(CardId.Lucina) && !HasAnywhereMine(CardId.Lucina))
                return 85;
            if (card.IsCode(CardId.Dina) && !HasAnywhereMine(CardId.Dina))
                return 80;
            if (card.IsCode(CardId.Fortuna) && !HasAnywhereMine(CardId.Fortuna))
                return 70;
            if (card.IsCode(CardId.Homecoming) && !HasOnField(CardId.Homecoming))
                return 40;
            if (card.IsCode(CardId.Rhapsody) && !HasOnField(CardId.Rhapsody))
                return 35;
            return 10;
        }

        /// <summary>「狱神」检索（米底乌斯① / 比托利姆③）：优先比托利姆（教程第三节的 P 效果启动件）。</summary>
        private int JugamiSearchScore(ClientCard card)
        {
            if (card.IsCode(CardId.Bitlym) && !HasAnywhereMine(CardId.Bitlym))
                return 100;
            if (card.IsCode(CardId.Shinju) && !HasAnywhereMine(CardId.Shinju))
                return 90;
            if (card.IsCode(CardId.Junold) && !HasAnywhereMine(CardId.Junold))
                return 50;
            return 10;
        }

        /// <summary>狄娜①/福尔图娜①：从**卡组**表侧放永续魔陷到后场（手上的那张留着，等于白赚一张）。</summary>
        private static int DeckPlaceScore(ClientCard card)
        {
            // 卡组里的那张优先（相当于多抽一张）；同分时按"哪条引擎先铺"排：回乡 → 狂奏
            if (card.IsCode(CardId.Homecoming) && card.Location == CardLocation.Deck)
                return 100;
            if (card.IsCode(CardId.Rhapsody) && card.Location == CardLocation.Deck)
                return 90;
            if (card.IsCode(CardId.Homecoming))
                return 50;
            return 10;
        }

        /// <summary>
        /// 狱神门"送 1 只狱神去墓地"：教程第三节要的是**狱神精**（1 星调整进墓地才能被比托利姆/狂奏拉起来）。
        /// 绝不送额外卡组的朱诺拉（那是朱诺白化精② 的目标，送掉就没了）。
        /// </summary>
        private static int TerminusSendScore(ClientCard card)
        {
            if (card.IsCode(CardId.Shinju))
                return 100;
            if (card.IsCode(CardId.Junold))
                return 60;
            if (card.IsCode(CardId.Bitlym))
                return 30;
            if (card.IsCode(CardId.Junora))
                return -100;
            return 0;
        }

        /// <summary>回乡②/狂奏② 的目标分：先狱神精（同调素材兼检索），再 6 星，再其余本家。</summary>
        private static int HomecomingTargetScore(ClientCard card)
        {
            if (card.IsCode(CardId.Shinju))
                return 100;
            if (card.IsCode(CardId.Regina))
                return 90;      // 蕾吉娜站中央区能立刻 ② 再拉一只
            if (card.IsCode(CardId.Lucina))
                return 80;
            if (card.IsCode(CardId.Dina))
                return 70;
            if (card.IsCode(CardId.Fortuna))
                return 60;
            return 10;
        }

        private static int RhapsodyTargetScore(ClientCard card)
        {
            return HomecomingTargetScore(card);
        }

        /// <summary>鹤望兰②：先拉狱神精（7+1 出水晶翼），其次 6 星。</summary>
        private static int StrelitziaTargetScore(ClientCard card)
        {
            if (card.IsCode(CardId.Shinju))
                return 100;
            if (card.IsCode(CardId.Lucina))
                return 80;
            if (card.IsCode(CardId.Dina))
                return 70;
            if (card.IsCode(CardId.Fortuna))
                return 60;
            if (card.IsCode(CardId.Regina))
                return 50;
            return 10;
        }

        /// <summary>耀圣之诗②：拉回来的这批是"下一回合的展开点"，两只 6 星优先（教程第四节：墓地狄娜 + 卡组卢西娜）。</summary>
        private static int ElfenNotesTargetScore(ClientCard card)
        {
            if (card.IsCode(CardId.Dina))
                return 100;
            if (card.IsCode(CardId.Lucina))
                return 95;
            if (card.IsCode(CardId.Fortuna))
                return 90;
            if (card.IsCode(CardId.Regina))
                return 85;
            if (card.IsCode(CardId.Shinju))
                return 80;
            return 10;
        }

        /// <summary>朱诺白化精②：从额外叫「朱诺拉」（按卡脚本，只有它一个候选）。</summary>
        private static int JunoldTargetScore(ClientCard card)
        {
            return card.IsCode(CardId.Junora) ? 100 : 0;
        }

        /// <summary>落胤与圣女的代价：「阿不思」名（阿爾比昂/艾克莉西娅）里先送**打不出来**的阿爾比昂。</summary>
        private static int FallenCostScore(ClientCard card)
        {
            if (card.IsCode(CardId.Albion))
                return 100;
            if (card.IsCode(CardId.Ecclesia))
                return 50;
            return 0;
        }

        /// <summary>
        /// 三战之号从卡组挑哪张：脚本只允许"通常魔法 / 通常陷阱"。
        /// 优先狱神门（狱神轴的启动件）→ 三战之才（抽 2）→ 心灵防护罩（盖着的干扰）。
        /// </summary>
        private static int ThrustScore(ClientCard card)
        {
            if (card.IsCode(CardId.Terminus))
                return 100;
            if (card.IsCode(CardId.TripleTactics))
                return 80;
            if (card.IsCode(CardId.MindForce))
                return 60;
            return 10;
        }

        /// <summary>
        /// "把自己的卡送墓 / 放回卡组"当代价时挑哪张：**分数越低越先送**。
        /// * 中央区的 6 星：-100（送走它正好把中央区腾出来，这是教程第一节的核心节奏）；
        /// * 手卡里重复的 6 星：-60；
        /// * 场上的 6 星（非中央）：-30；
        /// * 狱神精 / 朱诺白化精 / 米底乌斯：0（它们进墓地反而能被拉起来）；
        /// * 蕾吉娜：+200（中枢，绝不能当柴烧）；
        /// * 永续魔陷（回乡/狂奏）与手坑：+300（留在手上/场上才有用）。
        /// </summary>
        private static int CostScore(ClientCard card)
        {
            if (card.IsCode(CardId.Regina))
                return 200;
            if (card.IsCode(CardId.Homecoming) || card.IsCode(CardId.Rhapsody))
                return 300;
            if (IsHandTrapId(card.Id))
                return 250;
            if (card.IsCode(CardId.Lucina) || card.IsCode(CardId.Dina) || card.IsCode(CardId.Fortuna))
            {
                if (card.Location == CardLocation.MonsterZone && card.Sequence == 2)
                    return -100;
                if (card.Location == CardLocation.Hand)
                    return -60;
                if (card.Location == CardLocation.MonsterZone)
                    return -30;
                return 0;
            }
            if (card.IsCode(CardId.Shinju) || card.IsCode(CardId.Junold) || card.IsCode(CardId.Medius))
                return 0;
            return 10;
        }

        // ------------------------------------------------------------ 挑卡的通用工具

        /// <summary>要求候选来自指定区域（区域不符＝这次提问不是我们要等的那道，作废）。</summary>
        private static IList<ClientCard> PickHighestByScore(IList<ClientCard> cards, int min, int max,
            System.Func<ClientCard, int> score, CardLocation requiredLocation)
        {
            return PickByScoreInternal(cards, min, max, score, highest: true, requiredLocation);
        }

        private static IList<ClientCard> PickByScore(IList<ClientCard> cards, int min, int max,
            System.Func<ClientCard, int> score)
        {
            return PickByScoreInternal(cards, min, max, score, highest: true, requiredLocation: 0);
        }

        /// <summary>挑"分数最低"的那几张（代价用：越低越不心疼）。</summary>
        private static IList<ClientCard> PickCheapest(IList<ClientCard> cards, int min, int max,
            System.Func<ClientCard, int> score)
        {
            return PickByScoreInternal(cards, min, max, score, highest: false, requiredLocation: 0);
        }

        private static IList<ClientCard> PickByScoreInternal(IList<ClientCard> cards, int min, int max,
            System.Func<ClientCard, int> score, bool highest, CardLocation requiredLocation)
        {
            if (requiredLocation != 0)
            {
                bool any = false;
                foreach (ClientCard card in cards)
                {
                    if ((card.Location & requiredLocation) != 0)
                    {
                        any = true;
                        break;
                    }
                }
                if (!any)
                    return null;    // 区域不对＝不是我们要等的那道题
            }

            List<ClientCard> pool = new List<ClientCard>();
            foreach (ClientCard card in cards)
                pool.Add(card);
            // 稳定排序：同分时保持内核给的顺序（这样行为可复现，不会今天一个样明天一个样）
            pool.Sort((a, b) =>
            {
                int sa = score(a);
                int sb = score(b);
                return highest ? sb.CompareTo(sa) : sa.CompareTo(sb);
            });

            int count = min;
            if (count < 1)
                count = 1;
            if (count > pool.Count)
                count = pool.Count;
            if (max >= 1 && count > max)
                count = max;
            List<ClientCard> selected = new List<ClientCard>();
            for (int i = 0; i < count && i < pool.Count; ++i)
                selected.Add(pool[i]);
            return selected.Count > 0 ? selected : null;
        }

        // ============================================================ 分支选项（OnSelectOption）

        public override int OnSelectOption(IList<int> options)
        {
            int wanted = 0;
            int result = -1;

            // ① 按"值"登记的待办（卡号*16+k）。值不在这次提问里＝登记作废（发动被无效时就会这样）。
            if (_optionCard != 0)
            {
                int cardId = _optionCard;
                wanted = _optionValue;
                _optionCard = 0;
                _optionValue = 0;
                if (IsOptionOfCard(options, cardId))
                    result = wanted > 0 ? options.IndexOf(wanted) : -1;
            }

            // ② 三战之号：选出来的那张卡决定"盖放（1153）/ 加手（1190）"。
            //    脚本 c35269905 第 37 行 `Duel.SelectOption(tp,1153,1190)==0` → 下标 0 是 1153（盖放）。
            //    这张牌本来就没用过——留着当后手的续航：通常魔法（狱神门/三战之才）加手，通常陷阱盖下去。
            if (result < 0 && _thrustPicked != 0)
            {
                int picked = _thrustPicked;
                _thrustPicked = 0;
                YGOSharp.OCGWrapper.NamedCard pickedCard = YGOSharp.OCGWrapper.NamedCard.Get(picked);
                bool isTrap = pickedCard != null && pickedCard.HasType(CardType.Trap);
                wanted = isTrap ? 1153 : 1190;
                result = options.IndexOf(wanted);
            }

            if (result < 0)
                result = base.OnSelectOption(options);

            if (_verbose)
                Logger.WriteLine("[耀圣] 选项提问 options=[" + OptionsToText(options) + "] 想要="
                    + (wanted > 0 ? wanted.ToString() : "—") + " 命中=" + result);
            return result;
        }

        /// <summary>选项是不是"这张卡自己的"：值＝`卡号*16+k`（`aux.Stringid`），落在 [卡号*16, 卡号*16+16) 就是它。</summary>
        private static bool IsOptionOfCard(IList<int> options, int cardId)
        {
            int low = cardId * 16;
            foreach (int option in options)
            {
                if (option >= low && option < low + 16)
                    return true;
            }
            return false;
        }

        private static string OptionsToText(IList<int> options)
        {
            string text = "";
            foreach (int option in options)
            {
                if (text.Length > 0)
                    text += ",";
                text += option;
            }
            return text;
        }

        // ============================================================ yes/no（OnSelectYesNo）

        /// <summary>
        /// 内核的 yes/no 提问：desc＝提示串的值（`aux.Stringid(卡号,k)`）。
        /// 默认一律答"是"（"要不要同调召唤""要不要无效""要不要加手"都该是是），只在一个地方压回"否"：
        /// 鹤望兰② 的连锁处理"让场上全部 4 星以上怪兽降 3 星"（脚本 c42302563 第 66 行 k=1）
        /// ——它**连我们自己的怪一起降**，对面场上没有 4 星以上的怪兽时纯粹是自伤。
        /// </summary>
        public override bool OnSelectYesNo(int desc)
        {
            if (desc == Util.GetStringId(CardId.Strelitzia, 1))
            {
                bool worth = false;
                foreach (ClientCard card in Enemy.GetMonsters())
                {
                    if (card.IsFaceup() && card.Level >= 4)
                    {
                        worth = true;
                        break;
                    }
                }
                if (_verbose)
                    Logger.WriteLine("[耀圣] 鹤望兰的降星提问 → " + (worth ? "是（对面有 4 星以上表侧怪）" : "否（只降到自己）"));
                return worth;
            }
            return true;
        }

        // ============================================================ 宣言卡名（OnAnnounceCard）

        /// <summary>
        /// 抹杀之指名者的宣言：优先用登记好的那张（＝我们能命中的、对手用过的卡），
        /// 没登记就退回候选表第一个（`GameAI` 也会兜这一层）。
        /// </summary>
        public override int OnAnnounceCard(IList<int> avail)
        {
            if (_announceCode != 0)
            {
                int code = _announceCode;
                _announceCode = 0;
                if (avail.Contains(code))
                    return code;
            }
            return avail.Count > 0 ? avail[0] : 0;
        }

        // ============================================================ 摆位（OnSelectPlace）

        /// <summary>
        /// 摆位：**中央主要怪兽区（zone 2）是这副牌最稀缺的资源**（三只 6 星只能站那里，蕾吉娜②、
        /// 耀圣之诗②、鹤止兰的 3000 攻也都要求 `GetSequence()==2`）。
        /// 内核默认顺序本来就是 z2 优先（`GameAI.OnSelectPlace` 里写死 z2→z1→z3→z0→z4），
        /// 但"不想站中央的怪"（狱神精、米底乌斯这些）如果占了中央，下一只 6 星就跳不出来——
        /// 所以这里把中央**从它们的候选里剔掉**，把中央留给下一只本家。
        /// （`(executor_selected & filter) > 0` 才生效，所以返回一个不在候选里的位是安全的。）
        /// </summary>
        public override int OnSelectPlace(int cardId, int player, CardLocation location, int available)
        {
            if (player != 0 || location != CardLocation.MonsterZone)
                return 0;
            int filter = available & Zones.MonsterZones;
            if (filter == 0)
                return 0;

            if (WantsCenterZone(cardId))
                return (filter & CenterZone) != 0 ? CenterZone : 0;

            int withoutCenter = filter & ~CenterZone;
            return withoutCenter != 0 ? withoutCenter : 0;
        }

        /// <summary>要占中央区的卡：三只 6 星（内核本来也只给中央）、蕾吉娜（② 要求站中央）、
        /// 鹤望兰（站中央 3000 攻）、耀圣之诗（② 要求站中央）。</summary>
        private static bool WantsCenterZone(int cardId)
        {
            return cardId == CardId.Lucina
                || cardId == CardId.Dina
                || cardId == CardId.Fortuna
                || cardId == CardId.Regina
                || cardId == CardId.Strelitzia
                || cardId == CardId.ElfenNotes;
        }

        // ============================================================ 局面工具

        /// <summary>当前中央主要怪兽区（zone 2）上的我方怪兽（没有就 null）。</summary>
        private ClientCard CenterMonster()
        {
            return Bot.MonsterZone[2];
        }

        private static bool IsYaosheng(ClientCard card)
        {
            return card != null && card.IsMonster() && card.HasSetcode(SetcodeYaosheng);
        }

        private static bool IsJugami(ClientCard card)
        {
            return card != null && card.IsMonster() && card.HasSetcode(SetcodeJugami);
        }

        private static bool IsHandTrapId(int cardId)
        {
            foreach (int id in HandTraps)
            {
                if (cardId == id)
                    return true;
            }
            return false;
        }

        private static bool IsHandTrap(ClientCard card)
        {
            return card != null && card.IsMonster() && IsHandTrapId(card.Id);
        }

        /// <summary>我方的场上 / 手卡 / 墓地 / 除外里有没有这张（"还需不需要检索它"的判据）。</summary>
        private bool HasAnywhereMine(int cardId)
        {
            if (Bot.HasInHand(cardId) || Bot.HasInGraveyard(cardId) || Bot.HasInBanished(cardId))
                return true;
            return Bot.HasInMonstersZone(cardId) || Bot.HasInSpellZone(cardId);
        }

        private bool HasOnField(int cardId)
        {
            return Bot.HasInMonstersZone(cardId) || Bot.HasInSpellZone(cardId);
        }

        /// <summary>手卡·墓地里有「狱神」怪兽（比托利姆 P 效果的拉人目标）。</summary>
        private bool HasJugamiInHandOrGrave()
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (IsJugami(card))
                    return true;
            }
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (IsJugami(card))
                    return true;
            }
            return false;
        }

        /// <summary>对面场上有表侧表示的卡（怪兽或魔陷/场地）。</summary>
        private bool EnemyHasFaceupCard()
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

        /// <summary>对面场上有没有卡（不管表里），给鲜花的"① 炸卡"判断用。</summary>
        private bool EnemyHasAnyCard()
        {
            if (Enemy.GetMonsterCount() > 0)
                return true;
            return Enemy.GetSpellCount() > 0;
        }

        // ============================================================ 代价与目标的"属性配对"（回乡 / 狂奏）

        /// <summary>本家怪兽的原本属性（脚本要求"原本属性不同"，所以只能用卡库里的固定值算）。</summary>
        private static int YaoshengAttribute(int cardId)
        {
            if (cardId == CardId.Lucina || cardId == CardId.Shinju)
                return (int)CardAttribute.Fire;     // 卢西娜 / 狱神精：炎
            if (cardId == CardId.Dina)
                return (int)CardAttribute.Water;    // 狄娜：水
            if (cardId == CardId.Fortuna)
                return (int)CardAttribute.Light;    // 福尔图娜：光
            if (cardId == CardId.Regina)
                return (int)CardAttribute.Wind;     // 蕾吉娜：风
            return 0;
        }

        /// <summary>卡组里还有没有这只（用来看"这一发能拉到谁"）。</summary>
        private bool DeckHas(int cardId)
        {
            return Bot.HasInDeck(cardId);
        }

        /// <summary>
        /// 选"送墓的代价"时顺便算：这个属性当代价的话，**墓地**里能拉起哪只本家（分数越高越该配它）。
        /// 狂奏② 用：目标必须在墓地、且原本属性与代价**互不重叠**（`c:GetOriginalAttribute()&attr==0`）。
        /// </summary>
        private int BestRhapsodyTargetScore(int costAttribute)
        {
            int best = -1;
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (!IsYaosheng(card))
                    continue;
                int attr = YaoshengAttribute(card.Id);
                if (attr == 0 || (attr & costAttribute) != 0)
                    continue;
                int score = HomecomingTargetScore(card);
                if (score > best)
                    best = score;
            }
            return best;
        }

        /// <summary>
        /// 回乡②/狂奏② 的**代价**挑法：代价的属性会决定"这一发能拉起谁"，所以不能只看"哪张不心疼"。
        /// 对每个候选算"拿它当代价能拉起的最好的目标"，目标分最高的那个优先；
        /// 目标分相同再按 <see cref="CostScore"/>（越不心疼越先送）。
        /// 例子（教程第四节）：狄娜(WATER)在场 + 场上有「回乡」→ 拿狄娜当代价能拉蕾吉娜(WIND)，
        /// 这就是"用它②解除狄娜，把蕾吉娜特召到中央区"那一步。
        /// </summary>
        private IList<ClientCard> PickPairingCost(IList<ClientCard> cards, int min, int max, bool forGrave)
        {
            ClientCard best = null;
            int bestTarget = int.MinValue;
            int bestCost = int.MaxValue;
            foreach (ClientCard card in cards)
            {
                int attribute = CardOriginAttribute(card);
                int target = forGrave
                    ? BestRhapsodyTargetScore(attribute)
                    : BestHomecomingTargetScore(attribute);
                int cost = CostScore(card);
                if (target > bestTarget || (target == bestTarget && cost < bestCost))
                {
                    best = card;
                    bestTarget = target;
                    bestCost = cost;
                }
            }
            if (best == null)
                return null;
            List<ClientCard> selected = new List<ClientCard>();
            selected.Add(best);
            return selected;
        }

        /// <summary>卡的原本属性：本家四只 + 狱神精用固定值（卡库里的原本属性），其余用当前属性。</summary>
        private static int CardOriginAttribute(ClientCard card)
        {
            int known = YaoshengAttribute(card.Id);
            return known != 0 ? known : card.Attribute;
        }

        /// <summary>同上，但看**卡组**（回乡② 用）。</summary>
        private int BestHomecomingTargetScore(int costAttribute)
        {
            int best = -1;
            int[] candidates = { CardId.Regina, CardId.Shinju, CardId.Lucina, CardId.Dina, CardId.Fortuna };
            foreach (int id in candidates)
            {
                if (!DeckHas(id))
                    continue;
                int attr = YaoshengAttribute(id);
                if (attr == 0 || (attr & costAttribute) != 0)
                    continue;
                int score = HomecomingTargetScoreForId(id);
                if (score > best)
                    best = score;
            }
            return best;
        }

        private static int HomecomingTargetScoreForId(int cardId)
        {
            if (cardId == CardId.Shinju)
                return 100;
            if (cardId == CardId.Regina)
                return 90;
            if (cardId == CardId.Lucina)
                return 80;
            if (cardId == CardId.Dina)
                return 70;
            if (cardId == CardId.Fortuna)
                return 60;
            return 10;
        }

        // ============================================================ 回合钩子

        public override void OnNewTurn()
        {
            base.OnNewTurn();
            // 上一回合留下的"待办选项/待办选卡"一并清掉：发动被无效、或者回合结束时还有没消费掉的登记，
            // 留着只会污染下一张卡的选项提问（这是本项目记录过的真 bug）。
            _pick = PickKind.None;
            _optionCard = 0;
            _optionValue = 0;
            _announceCode = 0;
            _thrustPicked = 0;
        }
    }
}
