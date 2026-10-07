using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 升辉月 / 盈彩月夜（Raise Moon）专用执行器。
    ///
    /// **为什么需要它**：这副牌没有 WindBot 自带的对应执行器，一直靠通用脚本（`Lucky`/`PlanAware`）打，
    /// 而通用脚本不知道"抽到下级就自跳、本家几乎没有检索、全靠抽卡滚起来"这条基本盘。
    /// 实测（102 局）额外卡组 12 张终端里 11 张出过场，但**通召优先级**与**先铺哪张引擎魔法**一直是随机撞运气。
    ///
    /// 基类是 <see cref="DoEverythingExecutor"/>（WindBot 的通用打法："看着场面做点合理的事"），
    /// 这里只把**这副牌特有的判断**压到它前面——WindBot 的判定是"顺序遍历规则、第一条说 yes 的获胜"
    /// （见 `GameAI.OnSelectIdleCmd`），所以插在前面的这几条会压过通用规则。
    ///
    /// 卡表打法（详见插件的 `combos/88.txt`）：本家一张检索都没有，**抽到下级＝直接特召**，
    /// 再用它们的效果接着抽；两张引擎魔法把它滚起来，最后做 R7「大姐」——
    /// 「一擲乾坤」**只有在额外怪兽区才有效果**（不受其他卡效果影响 + 把对手"从卡组加手"的效果
    /// 改成"我方抽 1"），选格那边已经改成"含额外区就优先额外区"（见 `GameAI.OnSelectPlace`）。
    ///
    /// **这一层只做一件事：在"合法动作"里排战术优先级**（WindBot 的规则表＝有序扫描，第一条说
    /// yes 的获胜，等价于"优先级列表"）。**合法性不在这里判断**，三层分工是：
    ///
    /// | 层 | 负责 |
    /// |---|---|
    /// | 内核（ocgcore） | 起手不触发 `EVENT_DRAW`、抽牌后有其他处理则错过时点、`EFFECT_CANNOT_SPECIAL_SUMMON` 自肃、`EFFECT_REPLACE_DISCARD`（如未眠之城② 把丢手牌换成堆墓）——全部由内核判 |
    /// | 卡脚本（Lua） | 声明"什么条件下能发动 / 发动后做什么 / 自肃过滤什么"（我们引擎里已经跑着的那批 `c*.lua`） |
    /// | 本文件（决策层） | 只看内核给出的合法动作，决定"下一步做什么"，**不重复实现合法性** |
    ///
    /// 所以下面这些规则里没有"是否合法"的判断，只有"先做哪个"的顺序。
    ///
    /// **四条踩过的坑（改动前先读）**：
    /// 1. `AlwaysPlay`＝"只要能用就开"。只给**无条件该开的卡**（引擎魔法、蓋輝煌、超量终端）；
    ///    有代价的效果（西艾蘿要把手牌/场上 1 张卡回牌组底才特召自己、斯奎茲/希耶娜/庫露雪 的弃手抽牌
    ///    要丢掉可能当超量素材的怪、菈碧的对手回合快速超量要花素材）写成"能发就发"会让操作变形、
    ///    白白浪费动点（用户实测投诉）。那些交给基类的通用判断，它会看场面与代价。
    /// 2. `GameAI.ShouldExecute` 里 `Func` 返回 false **只是跳过这一条规则**，不是否决这个动作。
    ///    所以"禁止做某件事"（禁止通召手坑、禁止发动某张卡）必须**替换掉**基类那条笼统规则，
    ///    另加一条禁止规则是没用的。
    /// 3. 基类（`DoEverythingExecutor`）的 `OnSelectCard` 是从候选列表**尾部**随便取的。
    ///    而"以场上 1 张表侧表示的卡为对象"这类效果（台风、落胤与圣女）把**双方**的卡放进同一个
    ///    候选列表——照基类取就会把自己的「未眠之城」炸掉（用户实测："不要自己发陷阱炸自己"）。
    /// 4. 基类 `OnSelectOption` 是**随机**挑一个（`Program.Rand.Next`）。"宣言一个种类"的卡
    ///    （次元障壁）随机到「超量」就把自己的超量全锁死——要用这类卡，就得自己接管选项选择。
    ///    接管时**按"值"匹配**（选项值＝`卡号*16+k`，即 `aux.Stringid(卡号,k)`，见 `Util.GetStringId`），
    ///    不要按"第几项"：序号不带卡号，一旦某次登记没被消费掉（发动被无效／错过时点），
    ///    就会污染下一张卡的选项提问。各卡登记的值与脚本行号写在 `OnSelectOption` 和登记点旁边。
    ///
    /// **引擎自己判的规则不要在这里重写**：起手 5 张不能靠"被抽到"特召、同一连锁抽到复数本家
    /// 只能跳一只、C2 以上错过时点、以及各卡发动后的检索/额外自肃——这些都是内核强制的，
    /// 客户端只负责对内核的提示答 yes/no。
    /// </summary>
    [Deck("RaiseMoon", "AI_RaiseMoon", "Normal")]
    public class RaiseMoonExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            // ⚠ 这套卡号是**群友客户端认的那套**（简体名）。本家在卡库里有**两套号**：
            //   另一套是我们早期自用的 100267xxx（繁体名）。bot 用哪套，对面客户端就认哪套——
            //   曾经 bot 打 100267xxx，群友那边全显示"未知卡片"。所以卡表与这里都必须用下面这套。
            public const int Sheera_Curtain = 75934716;    // 盈彩月夜之帐 西艾萝
            public const int Carol = 11333460;             // 盈彩月夜之雅 卡萝尔（通召不用解放 + 召唤即抽 1）
            public const int Rabi = 47828069;              // 盈彩月夜之翼 菈碧（召唤即抽 1 + 对手回合快速 R7）
            public const int Hiena = 74212164;             // 盈彩月夜之刃 希耶娜（弃手抽 1 + 被抽到特召弹怪）
            public const int Cruse = 10611873;             // 盈彩月夜之灯 库露雪（弃手抽 1 + 被抽到特召弹魔陷）
            public const int Squeeze = 47605577;           // 盈彩月夜之朔 斯奎兹（手牌用：丢自己抽 1）
            public const int Ichigekan = 73090586;         // 盈彩月夜之天 西艾萝-一掷乾坤（大姐，R7）
            public const int Daishou = 9484285;            // 盈彩月夜之望 斯奎兹-千金大奖（R7）
            public const int Seiza = 46983930;             // 盈彩月夜之群星（把本家放卡组顶＝唯一的伪检索）
            public const int Nemuri = 72978038;            // 未眠之城的『盈彩月夜』（每回合抽 1 + 结束阶段洗回）
            public const int Kira = 9362643;               // 盈彩月夜之辉煌（陷阱：从额外特召本家超量 + 当素材）
            public const int Typhoon = 14883228;           // 台风（陷阱：破坏场上 1 张表侧表示魔陷）
            public const int DimensionalBarrier = 83326048; // 次元障壁（陷阱：宣言一个种类）
            public const int KindLockMagic = 94423983;     // 同契魔术（陷阱：双方不能特召自己场上已有种类）
            public const int CipherBladeDragon = 2530830;  // 银河眼光波刃龙（拔 1 素材炸 1 张卡）
            public const int AntimatterDragon = 92517928;  // 银河眼反物质龙（在超量怪上重叠的 R9，连击）
            public const int FoolishBurialGoods = 35726888; // 愚蠢的副葬（从卡组送 1 张魔陷去墓地）
            public const int TripleTactics = 25311006;     // 三战之才（三选一）
            public const int AlbazAndEcclesia = 30271097;   // 落胤与圣女（二选一）
            // ⚠ 它是**非本家**的 3000 打手，但没规则时会掉进通用兜底、被"出额外怪就让路"整只否掉
            //   （2026-10-05 群里反馈"场上只有一个白龙"就是这个）。见 WhitePhantom()。
            public const int WhitePhantom = 30397786;       // 白色幻兽-青眼白龙（基准号）
            // ⚠ 卡表（AI_Gen88.ydk）第 7 行用的是**另一印刷号 30397787**（本家在卡库里有这两个印刷：
            //   30397787 的 alias＝30397786）。只登记基准号 30397786 时，卡表里那张匹配不上
            //   `exec.CardId == -1 || card.IsOriginalCode(exec.CardId)`，专属规则整条落空、
            //   又掉回"出额外怪就让路"的通用兜底。**两个印刷号都认**：下面常量、AddExecutor 与
            //   WhitePhantom() 里的描述号判断都同时登记两个号。
            public const int WhitePhantomAlt = 30397787;    // 白色幻兽-青眼白龙（卡表里用的是这个印刷号）
            // 主牌 1 张（卡表 88 号牌组第 7 行）：被打伤时自跳 + 回 2000，之后三选一（见 Activate()）。
            // 它没有专属规则时会掉进通用 Activate，选项由基类**随机**挑——会有一半概率白挑"什么都不做"。
            public const int UnderworldMessenger = 3635138; // 来自冥府的使者

            // ===== 2026-10-06 补登记：本家之外的引擎与额外终端 =====
            // 来源是 `tools/check_card_coverage.py --deck-id 88` 列出的 9 张"卡表里有、执行器一次都没提"的卡。
            // 它们原来全靠基类兜底，而基类的选卡是"从候选列表尾部随便取"，实测出过三类问题：
            // ① 梯子一回合连爬（千金 1400 → 伊里斯斐尔 800 → 光子咆哮 4500 → 重铠 4000 → 光波刃龙 3200，
            //    素材一路搬过去），后两跳是纯降级；② 阿宙斯 ① 是**双方**全场送墓，没人拦就会把自家
            //    场地/盖牌一起清掉；③ 终端的代价/分支（重铠拔素材、光子咆哮要解放另一只超量、女男爵的③）
            //    没人把关。下面各条按卡文给出闸门，具体理由都写在对应函数上。
            public const int DrawBread = 83838727;          // 抽卡面包（魔法：付 200 LP 抽 1，按抽到卡的属性再抽 1 或弃 1）
            public const int Zeus = 90448279;               // 天霆号 阿宙斯（脚本 c90448282 用的是基准号；卡表里是印刷号 90448282/alias 90448279）
            public const int PhotonHowling = 28331070;      // 超银河眼光子龙-光子咆哮（卡表用的是这个印刷号，脚本 c28331070 也用它）
            public const int PhotonHowlingBase = 28331069;  // 上面那张的基准号（alias）——判卡两个印刷号都认
            public const int Irisfeer = 64626565;           // 黑智天至 伊里斯斐尔（8 阶；8 阶梯子唯一的入口）
            public const int FullArmorPhoton = 39030163;    // 银河眼重铠光子龙（8 阶；上叠在「银河眼」超量怪上）
            public const int UtopicZero = 26973555;         // 未来No.0 未来龙皇 霍普（卡文要「No.」以外的同阶级超量 ×3）
            public const int Baronne = 84815190;            // 鲜花女男爵（10 星同调＝3★ 调整手坑 + 本家 7★）
            public const int Ecclesia = 78397661;           // 黑龙之艾克莉西娅（8 星同调＝1★ 效果遮蒙者 + 本家 7★）
            public const int Albion = 87746184;             // 烙印龍 阿爾比昂（融合；在本副牌里是「落胤与圣女」的额外费用）
        }

        public RaiseMoonExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ⓪ 打开"选格优先额外怪兽区"：这副牌的超量怪（卡 100267022/100267023）只有站
            //    额外怪兽区才有 ① 效果，落主怪兽区就是白板。这是**卡组级开关**——
            //    引擎默认不这么做（连接卡组要靠主区的箭头连，塞进额外区反而变弱）。
            GameAI.PreferExtraMonsterZone = true;
            // ① 先摘掉基类那两条**笼统规则**，它们才是"能动就动"的源头：
            //    · `Activate`（`DefaultDontChainMyself`）＝任何卡能发就发。它的判断是
            //      `Duel.LastChainPlayer != 0`，而这个值在**没有连锁时是 -1**，于是自方主要阶段
            //      也会一路返回 true —— 手牌/盖放的魔法陷阱只要有合法时点就全开，
            //      包括会炸到自己场面的卡（用户投诉："能动就动的脚本是不行的"）。
            //    · `SummonOrSet`（`DefaultMonsterSummon`）＝任何怪都能通召/盖放（手坑禁令见 ⑦）。
            //    ⚠ 这一步**必须在加自己的规则之前**做：IList 没有 RemoveAll，只能按类型倒着删，
            //      放在后面会把刚加进去的专属规则一起删掉（踩过一次）。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].Type == ExecutorType.SummonOrSet || Executors[i].Type == ExecutorType.Activate)
                    Executors.RemoveAt(i);
            }

            // ② 两张引擎魔法"能发就发"：未眠之城（每回合抽 1 + 结束阶段把墓地/除外的本家洗回）
            //    与群星（把本家放卡组顶，下一次抽卡把它抽上来＝唯一的伪检索）。
            //    它们都是"赚一张"的效果，没有需要看时机的代价。
            AddExecutor(ExecutorType.Activate, CardId.Nemuri, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Seiza, AlwaysPlay);

            // ③ 蓋輝煌：一张就能从额外特召 1 只本家超量并把自己当素材，等于凭空多一次干扰/挡刀。
            //    盖下去几乎没损失，所以"能盖就盖"。
            AddExecutor(ExecutorType.SpellSet, CardId.Kira, AlwaysPlay);

            // ④ 通常召唤优先级（这是通用脚本最常搞错的地方）：
            //    卡蘿爾（不用解放就能通召，且**召唤即抽 1**）＞ 菈碧（召唤即抽 1，还能在对手回合补一只 R7）。
            //    本家其它下级要么靠"被抽到"自跳、要么留手里当阻抗，不值得占用通召。
            //    **用 SummonOrSet 而不是 Summon**：Summon 是"一定是攻击表示"，而本家下级是
            //    等级 7、攻 700 / 守 2100——攻击表示等于送（用户实测投诉"明明我攻击力比它高，
            //    它还用攻击表示，不是找打吗"）。具体摆哪边由下面的 OnSelectMonsterSummonOrSet 决定。
            AddExecutor(ExecutorType.SummonOrSet, CardId.Carol, AlwaysPlay);
            AddExecutor(ExecutorType.SummonOrSet, CardId.Rabi, AlwaysPlay);

            // ⑤ 台风：**只在自己场上没有表侧魔陷时才发动**（见 Typhoon()）。
            //    它自己就是"炸自己"最典型的那张——把目标限死，比事后在选卡时补救更硬。
            AddExecutor(ExecutorType.Activate, CardId.Typhoon, Typhoon);

            // ⑥ 超量终端：先做「一擲乾坤」（它那三条效果**只在这个怪站额外怪兽区时才有**），
            //    再考虑「千金大獎」（对手只能打它 + 弹对手的卡）。素材选择交给通用逻辑。
            AddExecutor(ExecutorType.SpSummon, CardId.Ichigekan, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.Daishou, AlwaysPlay);

            // ⑦ 千金大獎的 ②（拔任意数量素材 → 弹对面那个数量的卡）：**每次最多拔 1 个素材**，
            //    别为了一发弹卡把素材全清空（用户反馈："倒头来自己的超量一张素材都没有"）。
            AddExecutor(ExecutorType.Activate, CardId.Daishou, Daishou);

            // ⑧ 银河眼光波刃龙：素材只剩 1 个时**别拔**——拔完它就是个没有效果的白板。
            AddExecutor(ExecutorType.Activate, CardId.CipherBladeDragon, CipherBladeDragon);

            // ⑩ 「白色幻兽-青眼白龙」：**必须给它专属规则**。它没有规则时会掉进最后的通用兜底，
            //    而兜底里那条"现在能从额外卡组出怪 → 通用效果/盖牌让路"会把它的**所有**效果否掉：
            //    实测它在场上连 ③（丢 1 张手卡无效取对象效果）都不发，就是一只 3000 白板
            //    ——群友反馈的"场上只有一个白龙"就是这个现象。
            AddExecutor(ExecutorType.Activate, CardId.WhitePhantom, WhitePhantom);
            // ⚠ 卡表（AI_Gen88.ydk）用的是另一印刷号 30397787：**两个印刷号都认**，两条都登记
            //   （缺了这条，卡表里那张就匹配不上、整条专属规则落空）。
            AddExecutor(ExecutorType.Activate, CardId.WhitePhantomAlt, WhitePhantom);

            // ⑨ 反物质龙：只在"打得死"时才叠它、才开连击（默认关，见 AntimatterLethal）。
            AddExecutor(ExecutorType.SpSummon, CardId.AntimatterDragon, AntimatterLethal);
            AddExecutor(ExecutorType.Activate, CardId.AntimatterDragon, AntimatterLethal);

            // ⑦ 两张"宣言/限制种类"的干扰陷阱：**只在对手回合发动**，用来掐断对面展开
            //    （用户的要求："可以阻止对面展开就行"）。它们一度被我一刀切禁掉——因为
            //    选项由基类的 `OnSelectOption` **随机**挑，随机到「超量」会把自己的超量锁死；
            //    现在改成自己接管选项选择（见 OnSelectOption 与 DimensionalBarrier）。
            AddExecutor(ExecutorType.Activate, CardId.DimensionalBarrier, DimensionalBarrier);
            AddExecutor(ExecutorType.Activate, CardId.KindLockMagic, KindLockMagic);

            // ⑧ 补回一条通用通召规则（任何怪都能通召/盖放），但**它否决手坑**。
            //    放在最后加＝在扫描顺序里排在 ④ 之后，专属优先级不会被它盖掉。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));

            // ⑨ 补回通用发动规则（能发就发）：专属规则（②⑤⑦）没接手的卡都由它上场。
            //    同样放在最后＝专属规则优先。
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));

            // ⑩ 其余（破后场、一般召唤与表示形式、剩下的超量/同调终端，以及引擎自己判的
            //    "起手不能跳 / C2 以上错过时点 / 一次抽多张只能跳一只 / 发动后的自肃"）
            //    全部交给基类的通用规则——那些是内核强制的，脚本只负责对内核的提示答 yes/no。

            // ⑫ 本家之外的引擎与终端（2026-10-06 补登记，见 CardId 里那段说明）：
            //    · 抽卡面包＝补牌件，能发就发；弃哪张由 PickDiscardTarget 挑（基类是从尾部随便取一张）；
            //    · 阿宙斯 ① 清的是**双方**全场，只在"对面掉得比我们多"时才发（ZeusWipeWorthIt）；
            //    · 8 阶以上的终端只能**往上叠**：打点要盖过我们场上最强的那只超量（LadderUpgrade）——
            //      实测基类会一回合连爬三节，把同一批素材换成一只打点更低的壳。
            AddExecutor(ExecutorType.Activate, CardId.DrawBread, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.Zeus, ZeusSummon);
            AddExecutor(ExecutorType.Activate, CardId.Zeus, ZeusEffect);
            //    · 8 阶梯子：伊里斯斐尔是唯一入口（它的上叠条件最松），但打点只有 800，代价是"拿一只本家
            //      R7 换壳"，所以只在我们**还留得住别的超量**时才走（IrisfeerClimb）；上面那三张按打点往上叠。
            AddExecutor(ExecutorType.SpSummon, CardId.Irisfeer, IrisfeerClimb);
            AddExecutor(ExecutorType.SpSummon, CardId.PhotonHowling, LadderUpgrade);
            AddExecutor(ExecutorType.SpSummon, CardId.FullArmorPhoton, LadderUpgrade);
            AddExecutor(ExecutorType.SpSummon, CardId.CipherBladeDragon, LadderUpgrade);
            //    · 未来No.0 要 3 只同阶级超量（本副牌凑不出，见 UtopicZeroClimb）。
            AddExecutor(ExecutorType.SpSummon, CardId.UtopicZero, UtopicZeroClimb);
            //    · 终端的效果：能发就发的交给 AlwaysPlay（**"这一条说了算"**——基类兜底里那条
            //      "现在能从额外卡组出怪 → 通用效果让路"会把没点名的卡的效果一起否掉，白龙就是这么变成
            //      3000 白板的）；有代价/有对称副作用的各自把关（见下面各函数）。
            AddExecutor(ExecutorType.Activate, CardId.Irisfeer, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.UtopicZero, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.FullArmorPhoton, FullArmorPhotonEffect);
            AddExecutor(ExecutorType.Activate, CardId.PhotonHowling, PhotonHowlingEffect);
            AddExecutor(ExecutorType.Activate, CardId.Baronne, BaronneEffect);
            AddExecutor(ExecutorType.Activate, CardId.Ecclesia, AlwaysPlay);

            // ⑪ 斩杀优先：插到 **Executors 最前面**（内核的动作循环是"外层遍历规则、内层遍历候选"，
            //    战斗阶段要等所有规则都不出手才轮得到；排在后面的话"还有事可做"会一直把战阶推后，
            //    用户实测的原话就是"明明可以斩杀却继续做场"）。
            //    与闪刀/卡通/魔女术/杀调/虫惑魔同一套判据（保守：对面空场 + 这一刀够斩杀才进战阶）。
            Executors.Insert(0, new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));
        }

        /// <summary>手坑：留在手里才有用，绝不能通召/盖放上场。</summary>
        private static readonly int[] HandTraps =
        {
            14558129,   // 灰流丽
            23434539,   // 增殖的G
            52038441,   // 朔夜时雨
            59438931,   // 幽鬼兔
            97268403,   // 效果遮蒙者
            94145022,   // 小丑与锁鸟
            84192580,   // 欢聚友伴·抖抖海月水母
            42141493,   // 欢聚友伴·茸茸长尾山雀
        };

        /// <summary>次元障壁这次要宣言的种类（内核的种类 flag，见 DimensionalBarrier()）；0＝没有待办的宣言。</summary>
        private int _declaredKind;

        /// <summary>
        /// 下一次选项提问要选的**内核选项值**（0＝没有待办）；供群星②、未眠之城② 这类"分支"用。
        ///
        /// ⚠ 选项值＝`卡号*16+k`（`aux.Stringid(卡号,k)`，即 <see cref="Util.GetStringId"/>），
        /// 不能按"第几项"记：序号不带卡号，**这次登记没被消费掉时（发动被无效／错过时点）**
        /// 就会污染下一张卡的选项提问。所以待办连同卡号一起登记（见 <see cref="_optionCard"/>），
        /// 提问里没有这张卡的值＝作废，清掉。
        /// </summary>
        private int _optionValue;

        /// <summary>上面这条待办属于哪张卡（卡号）；0＝没有待办。用来判断"这道题是不是它的"。</summary>
        private int _optionCard;

        /// <summary>下一次"选自己几张卡"最多选几张（0＝不限制）；用于别再为了弹一张卡把素材全拔光。</summary>
        private int _materialCap;

        /// <summary>西艾蘿的手牌效果正在选"要洗回牌组的那张卡"（见 SelectReturnTarget）。</summary>
        private bool _sheeraReturn;

        /// <summary>輝煌的 ① 正在选"从额外牌组叫哪只本家超量"（见 PickArchetypeXyz）。</summary>
        private bool _kiraSummon;

        /// <summary>愚蠢的副葬正在选"从卡组送哪张魔陷去墓地"（见 PickDeckSend）。</summary>
        private bool _deckSend;

        /// <summary>群星 ① 正在选"把哪张本家放到牌组顶"（见 PickSeizaPlace）。</summary>
        private bool _seizaPlace;

        /// <summary>未眠之城② 走了"洗回手牌再抽"那条时，接下来的"放顶还是放底"要答**放底**。</summary>
        private bool _nemuriReturn;

        /// <summary>
        /// 通召/盖放的统一入口：手坑平时一律否掉，其余交给基类判断。
        ///
        /// **唯一的例外：拿手坑当调整去凑同调**（见 <see cref="HandTrapTunerSummon"/>）——卡库里
        /// 灰流丽/幽鬼兔/效果遮蒙者/朔夜时雨**都是调整**，本家下级又全是 7 星，所以"通召手坑做同调"
        /// 本来就是这份卡表里写着的路线（额外卡组那两只同调就是为它配的），原来被"手坑一律不通召"
        /// 一刀切封死了。
        /// </summary>
        private bool SummonOrSet()
        {
            if (Card != null)
            {
                foreach (int id in HandTraps)
                {
                    if (Card.IsOriginalCode(id))
                        return HandTrapTunerSummon();
                }
            }
            return DefaultMonsterSummon();
        }

        /// <summary>通用发动入口：交给基类判断（它负责"别连自己的连锁"）。</summary>
        private bool Activate()
        {
            // 西艾蘿的**手牌**效果（把 1 张其他卡洗回牌组底 → 特召自己）紧接着会弹一个选卡提问，
            // 先登记，交给 SelectReturnTarget 按规则挑（她要挑"洗哪张回去"）。
            // 场地上的 ② （双方主阶抽 1）没有这个提问，所以只认手牌位置。
            if (Card != null && Card.IsOriginalCode(CardId.Sheera_Curtain) && Card.Location == CardLocation.Hand
                && AbOn("sheera"))
                _sheeraReturn = true;
            // 輝煌的 ①（从额外牌组特召 1 只本家超量怪）紧跟着是"选哪一只"，
            // 交给我们自己挑——见 PickArchetypeXyz：**优先大姐**。
            if (Card != null && Card.IsOriginalCode(CardId.Kira))
                _kiraSummon = true;
            // 还有一批**有分支可选、而基类会随机挑**的卡，这里把分支定死
            // （群里反馈："毛龟是不乱改了，但是其它有选项的卡他就乱改"）：
            // · 斯奎茲①：●对手加手我就抽（副作用：到**下个回合结束**为止只能从额外叫阶级 7）
            //   ●我方抽 1——取后者，别为了多抽几张把银河眼那一路封死；
            // · 三战之才：●抽 2（这副牌抽到本家就自跳，抽 2 最实在）；
            // · 落胤与圣女：●从额外送 1 只「阿不思」名怪兽去墓地，破坏场上 1 张表侧卡
            //   （另一条自苏要求场上或墓地有「艾克莉西娅」，这副牌通常不满足）；
            // · 愚蠢的副葬：要"从卡组送哪张魔陷去墓地"，交给 PickDeckSend 只送带墓地效果的那两张。
            if (Card != null)
            {
                if (Card.IsOriginalCode(CardId.Squeeze))
                {
                    // 脚本 c47605577 的两个半边（第 50~52 行 `aux.SelectFromOptions`）：
                    // k=1「持续抽卡」＝"对手每次加手我抽 1"（附带"到**下个回合结束**只能从额外叫阶级 7"的自肃）／
                    // k=2「抽 1 张卡」＝我方抽 1。
                    // 操作方的 spec 在后攻突破里要前者，但它会封掉银河眼那条 R9 线——
                    // 所以**只在对面已经有场面时吃**（那时对面还会继续检索，收益才抵得上自肃），
                    // 对面空场时仍取"只抽 1"。这里登记的是**值**（不是序号）：第 50~52 行里 k=1 在前、
                    // k=2 在后，但一旦某个半边不合法，内核的选项表就会少一项，序号会跟着挪。
                    _optionCard = CardId.Squeeze;
                    _optionValue = Util.GetStringId(CardId.Squeeze, Enemy.GetMonsterCount() > 0 ? 1 : 2);
                }
                else if (Card.IsOriginalCode(CardId.TripleTactics))
                {
                    // 脚本 c25311006 第 29~43 行：按 b1/b2/b3 依次把 k=0「自己抽 2 张」／k=1「得到控制权」
                    // ／k=2「确认手卡回卡组」放进选项表（**下标随合法性浮动**，只有 k=0 的意思是固定的）。
                    // 这副牌抽到本家就自跳，抽 2 最实在 → 认 k=0 这个值。
                    _optionCard = CardId.TripleTactics;
                    _optionValue = Util.GetStringId(CardId.TripleTactics, 0);
                }
                else if (Card.IsOriginalCode(CardId.AlbazAndEcclesia))
                {
                    // 脚本 c30271097 第 41~43 行：k=1「破坏」（从额外送 1 只「阿不思」名怪兽去墓地，
                    // 破坏场上 1 张表侧卡）／k=2「特殊召唤」（要求场上或墓地有「艾克莉西娅」，这副牌通常不满足）
                    // → 认 k=1 这个值。
                    // ⚠ 但第 46~52 行的破坏目标是"**双方**场上的 1 张表侧卡、1~1 **必选**"
                    //    （`SelectTarget(tp,Card.IsFaceup,tp,LOCATION_ONFIELD,LOCATION_ONFIELD,1,1,…)`）：
                    //    对面场上没有表侧卡时，候选里只剩我们自己的卡，这一发就变成"白扔 1 只额外怪
                    //    ＋ 炸自己一张"。实测就是这么吃掉「未眠之城」的（我方先攻、对面空场）：
                    //    mb6-88 局16 第 1 回合、mb5-88 局15/16、mb3-88 局11、maxboard-prev-88 局12——
                    //    五局都是"一掷乾坤 已站额外怪兽区"的局，炸完「未眠之城」当场从合格变不合格。
                    //    所以对面没有表侧卡时**不发动**，留着等对面铺出场再炸（对面有表侧卡时，
                    //    下面的 OnSelectCard"先选对面"会保证炸的是对面的卡）。
                    if (!EnemyHasFaceupCard())
                        return false;
                    _optionCard = CardId.AlbazAndEcclesia;
                    _optionValue = Util.GetStringId(CardId.AlbazAndEcclesia, 1);
                }
                else if (Card.IsOriginalCode(CardId.FoolishBurialGoods))
                    _deckSend = true;
                // 「来自冥府的使者」（脚本 c3635138 第 55~58 行）：① 自跳 + 回 2000 之后三选一——
                // k=2「特殊召唤」（手卡·卡组·墓地再叫 1 只自己）／k=3「破坏效果」（对方场上最多 2 只
                // 攻击表示怪兽破坏）／k=4「什么都不做」。卡表里**只有 1 张**，它自跳之后这张牌就在场上，
                // k=2 要的"手上/卡组/墓地还有 1 只"拿不出来，所以真实选项就是 k=3 与 k=4；
                // 基类随机挑会有一半概率白挑"什么都不做"。
                // 对面有攻击表示的怪（脚本第 54 行的判据）→ 认 k=3（白赚一次破坏），否则认 k=4。
                else if (Card.IsOriginalCode(CardId.UnderworldMessenger))
                {
                    bool hasAttackTarget = false;
                    foreach (ClientCard enemy in Enemy.GetMonsters())
                    {
                        if (enemy.IsFaceup() && enemy.IsAttack())
                        {
                            hasAttackTarget = true;
                            break;
                        }
                    }
                    _optionCard = CardId.UnderworldMessenger;
                    _optionValue = Util.GetStringId(CardId.UnderworldMessenger, hasAttackTarget ? 3 : 4);
                }
            }
            // 手坑与陷阱的**时机**：走 WindBot 原生执行器给这些卡准备好的判据，不再"能发就发"
            //（用户实测"开局丢 G""手坑乱扔"）。专属规则（台风/次元障壁/同契魔术）在更前面，先于这里生效。
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
                    // 自己回合丢它＝把自己的检索全锁死（本家全靠抽/检索，更伤）→ 只在对手回合、连锁对手的卡；
                    // 「欢聚友伴」两张（水母/山雀）是"发动后对手每次召唤我就抽 1"，而**发动条件本身只有
                    // "自己场上没有卡"**（`c84192580.lua`/`c42141493.lua` 的 `s.drcon`）——内核在任何时点
                    // 都可能问一次，原来"连锁对手的卡"会在对面还没出怪时（例如它先发动一张魔陷）把牌白扔
                    // （真人局实测）。改成**对手这一回合确实召唤过**之后才交（`EnemySummonedThisTurn`，见基类；
                    // 为什么不用 `Duel.LastSummonPlayer`：它在连锁开始与阶段开始都会被重置成 -1）。
                    if (Card.IsCode(84192580) || Card.IsCode(42141493))
                        return MulcharmyReady();   // 档位见 DefaultExecutor.MulcharmyWaitSummon（A/B：环境变量 MULCHARMY_GATE）
                    if (Card.IsCode(94145022))
                        return Duel.Player == 1 && Duel.LastChainPlayer == 1;
                    // 「朔夜时雨」只废对面一只怪、不伤自己，任何回合都可以。
                    return Duel.LastChainPlayer == 1;
                }
                // 「台风」的判据（<see cref="Typhoon"/>）必须**说了算**：专属规则返回 false 只是"这条不认"，
                // 后面这条通用兜底会接手（陷阱 → `DefaultTrap()`），于是"只在自己场上没有表侧魔陷时才发"
                // 这道闸门被绕过、台风照样发动，再把自家「未眠之城」炸掉（实测 20 局 6 次）。
                if (Card.IsOriginalCode(CardId.Typhoon))
                    return Typhoon();
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

        /// <summary>
        /// 对面场上有表侧表示的卡（怪兽或魔陷/场地）——给「落胤与圣女」这种"**双方**场上的表侧卡"
        /// 的效果用：对面一张表侧卡都没有时，这种效果的目标只能落到我们自己的卡上（脚本
        /// c30271097 第 46~52 行的目标范围是双方 ONFIELD、1~1 必选），开了就是自己炸自己。
        /// </summary>
        private bool EnemyHasFaceupCard()
        {
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (card.IsFaceup())
                    return true;
            }
            // GetSpells() 覆盖整个魔法·陷阱区（含场地区与灵摆区），set 着的卡 IsFaceup() 为假，
            // 正好对应"能不能被选为表侧卡目标"。
            foreach (ClientCard card in Enemy.GetSpells())
            {
                if (card.IsFaceup())
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
        /// <summary>只在"对手回合 + 连锁对手的卡"时才能交的手坑。</summary>
        private static readonly int[] HandTrapChainOnly = { 94145022, 52038441, 84192580, 42141493 };

        /// <summary>
        /// 輝煌（陷阱）①"从额外牌组特召 1 只本家超量怪"时选哪一只：**优先大姐（一擲乾坤）**。
        ///
        /// 群里反馈："这次不做大姐了，做毛龟去了……第一次是陷阱把毛龟放在额外区"。
        /// 陷阱是从额外牌组叫怪，而额外怪只能落到额外怪兽区——**谁先占了额外区，谁的 ① 才有效果**：
        /// 大姐的三条效果全写着"只要此卡在额外怪兽区域存在"，千金大獎则哪都能用。
        /// 所以先叫大姐出来（占住额外区），后面再叠千金大獎（主要怪兽区也能用）就不亏。
        /// 基类是从候选尾部随便取，才会把千金大獎摆进额外区。
        /// </summary>
        private IList<ClientCard> PickArchetypeXyz(IList<ClientCard> cards)
        {
            ClientCard chosen = null;
            foreach (ClientCard card in cards)
            {
                if (card.IsOriginalCode(CardId.Ichigekan))
                {
                    chosen = card;
                    break;
                }
            }
            if (chosen == null)
            {
                foreach (ClientCard card in cards)
                {
                    if (card.IsOriginalCode(CardId.Daishou))
                    {
                        chosen = card;
                        break;
                    }
                }
            }
            if (chosen == null)
                return null;
            IList<ClientCard> selected = new List<ClientCard>();
            selected.Add(chosen);
            return selected;
        }

        /// <summary>本家的展开件（抽到就自跳／继续抽的那批）；洗回牌组时最先排除的就是它们。</summary>
        private static bool IsEngineCard(ClientCard card)
        {
            int[] engine =
            {
                CardId.Sheera_Curtain, CardId.Carol, CardId.Rabi, CardId.Hiena, CardId.Cruse,
                CardId.Squeeze, CardId.Nemuri, CardId.Seiza, CardId.Kira,
            };
            foreach (int id in engine)
            {
                if (card.IsOriginalCode(id))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 西艾蘿手牌效果"选 1 张洗回牌组底"的目标。
        ///
        /// 群里反馈原话："荷官弹荷官再和荷官弹荷官／三张一样的荷官／他不选择去弹其他卡，
        /// 他跑去弹和自己一样的卡／明明可以凑出两场值做大姐的事，他硬是把三卡变一卡"。
        /// 基类是从候选尾部随便取一张，于是会出现"用自己的西艾蘿把另一张西艾蘿洗回去"，
        /// 如果那张在场上，更是直接把自己的场值洗掉。
        ///
        /// 规则：① **只从手牌里挑**，绝不动场上的怪（场上的怪是场值）；
        /// ② 手牌里优先挑**不是本家展开件**的卡（手坑、通用魔陷），本家件留着继续抽；
        /// ③ 手牌里全是本家件时，仍只洗手牌，不下手去动场上。
        /// </summary>
        private IList<ClientCard> SelectReturnTarget(IList<ClientCard> cards)
        {
            ClientCard chosen = null;
            foreach (ClientCard card in cards)
            {
                if (card.Location != CardLocation.Hand || IsEngineCard(card))
                    continue;
                chosen = card;
                break;
            }
            if (chosen == null)
            {
                foreach (ClientCard card in cards)
                {
                    if (card.Location == CardLocation.Hand)
                    {
                        chosen = card;
                        break;
                    }
                }
            }
            if (chosen == null)
                return null;    // 手牌里一张都没有（不该发生）：交回基类处理
            IList<ClientCard> selected = new List<ClientCard>();
            selected.Add(chosen);
            return selected;
        }

        /// <summary>融合·同调·超量·灵摆·连接——"额外卡组系"的种类。</summary>
        private static bool IsExtraKind(ClientCard card)
        {
            return card.HasType(CardType.Fusion) || card.HasType(CardType.Synchro)
                || card.HasType(CardType.Xyz) || card.HasType(CardType.Pendulum)
                || card.HasType(CardType.Link);
        }

        /// <summary>
        /// 次元障壁（宣言一个怪兽种类：这个回合双方不能特召该种类，场上该种类的怪兽效果无效）。
        ///
        /// **只在对手回合发动**：它是拿来掐对面展开的；自己回合发只会连带把自己场上
        /// 超量的效果一起无效掉。宣言哪个种类由 <see cref="PickEnemyKind"/> 按对手场上与墓地的
        /// 种类分布挑，选中的值交给 <see cref="OnSelectOption"/> 回给内核——
        /// 不再像基类那样**随机**挑（随机到「超量」＝把自己的超量锁死）。
        /// 一点线索都没有（对面场上墓地都没有额外卡组怪）时先不发，留着。
        /// </summary>
        private bool DimensionalBarrier()
        {
            if (Duel.Player != 1)
            {
                _declaredKind = 0;
                return false;
            }
            _declaredKind = PickEnemyKind();
            return _declaredKind != 0;
        }

        /// <summary>
        /// 同契魔术（这个回合双方都不能特召"与自己场上已有的种类相同"的怪兽）。
        ///
        /// **只在对手回合、且对手场上已经有融合/同调/超量/连接怪时发动**：这条限制是
        /// "各自按各自场上的种类锁各自"，所以只有对面已经站了那类怪，它才真的锁得住对面。
        /// 它同时也会锁住我们（我们场上有超量时就用不了菈碧的快速超量）——这是换"对面这回合
        /// 别再铺同类"的代价；按用户的要求（阻止对面展开优先）接受。
        /// </summary>
        private bool KindLockMagic()
        {
            if (Duel.Player != 1)
                return false;
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (card.IsFaceup() && IsExtraKind(card))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 从对手的场上与墓地猜他这回合最可能出的种类，返回次元障壁的选项值
        /// （内核的种类 flag：融合 1056、同调 1063、超量 1073、灵摆 1074、仪式 1057）。
        /// 场上的权重更高（正在打的就是这个种类），数量相同时按 融合＞同调＞超量＞灵摆＞仪式 取；
        /// 一个都没找到返回 0＝不发动。
        /// </summary>
        private int PickEnemyKind()
        {
            CardType[] kinds =
            {
                CardType.Fusion, CardType.Synchro, CardType.Xyz, CardType.Pendulum, CardType.Ritual,
            };
            int[] optionOf =
            {
                1056,   // 融合
                1063,   // 同调
                1073,   // 超量
                1074,   // 灵摆
                1057,   // 仪式
            };

            int best = 0;
            int bestOption = 0;
            for (int i = 0; i < kinds.Length; ++i)
            {
                int hits = CountKind(Enemy.GetMonsters(), kinds[i]) * 2 + CountKind(Enemy.Graveyard, kinds[i]);
                if (hits > best)
                {
                    best = hits;
                    bestOption = optionOf[i];
                }
            }
            return bestOption;
        }

        private static int CountKind(IList<ClientCard> cards, CardType kind)
        {
            int count = 0;
            foreach (ClientCard card in cards)
            {
                if (card.HasType(kind))
                    ++count;
            }
            return count;
        }

        /// <summary>
        /// 选项选择：**按"值"匹配**，不再按"第几项"匹配。
        ///
        /// 内核给的每个选项值＝`卡号*16+k`（`aux.Stringid(卡号,k)`，见 <see cref="Util.GetStringId"/>），
        /// 卡号就是出这道题的卡。所以待办登记的是"想要的那个**值** + 它属于哪张卡"，提问时先看选项里
        /// 有没有落在那张卡区间里的值：
        /// · 有 → 是它的提问，把想要的值翻成下标（`options.IndexOf`）回给内核；
        /// · 没有 → 这次登记没被消费掉（发动被无效／错过时点／效果被改），**清掉**，
        ///   绝不用上一张卡的答案回答这一张卡的选项（序号式的老写法就是在这里污染下一张卡的）。
        /// 系统字符串（次元障壁的 1056/1063/1073/1074/1057，见 `strings.conf`）不带卡号，单独按值匹配。
        /// </summary>
        public override int OnSelectOption(IList<int> options)
        {
            int wanted = 0;     // 探针用：这次"想要"的选项值（0＝没有按值登记的待办）
            int result = -1;    // 探针用：命中哪个下标（-1＝没命中，交给基类）

            // ① 分支型效果的待办（群星②、未眠之城②、斯奎兹①、三战之才、落胤与圣女、来自冥府的使者）
            if (_optionCard != 0)
            {
                int cardId = _optionCard;
                wanted = _optionValue;
                _optionCard = 0;        // 只认一次：消费或超时都在这里清
                _optionValue = 0;
                if (IsOptionOfCard(options, cardId))
                    result = wanted > 0 ? options.IndexOf(wanted) : -1;
                else
                    wanted = 0;         // 不属于这张卡＝登记作废（探针里显示"想要=—"）
            }

            // ② 次元障壁的宣言种类（系统字符串，不是卡号编码，只能按值匹配）
            if (result < 0 && _declaredKind != 0)
            {
                wanted = _declaredKind;
                result = options.IndexOf(_declaredKind);
                _declaredKind = 0;      // 只认一次，别让残留值影响后面的选项提问
            }

            // ③ 未眠之城② 的"洗回手牌"那条：跟着问"放牌组顶还是底"时答**放底**
            //（脚本 c72978038 第 81 行：`SelectOption(...)==0` 是 k=4＝顶、否则是 k=5＝底；
            // 放底才不会把群星放在顶上的那张盖回去）
            if (result < 0 && _nemuriReturn)
            {
                bool isTopBottomQuestion = options.Contains(Util.GetStringId(CardId.Nemuri, 4))
                    || options.Contains(Util.GetStringId(CardId.Nemuri, 5));
                if (isTopBottomQuestion)
                {
                    wanted = Util.GetStringId(CardId.Nemuri, 5);    // k=5＝卡组下面
                    result = options.IndexOf(wanted);
                    _nemuriReturn = false;                          // 消费即清
                }
                else if (!IsOptionOfCard(options, CardId.Nemuri))
                {
                    // 提问根本不是未眠之城的（发动被无效／别的卡在问）＝**超时，清掉**。
                    // 不清的话它会把下一张卡的两选项题答成"第 2 项"——这就是那个已知的真 bug。
                    _nemuriReturn = false;
                }
            }

            bool byValue = result >= 0;                 // 探针用：这个下标是"按值命中"还是基类随机给的
            if (result < 0)
                result = base.OnSelectOption(options);  // 没登记的提问照旧交给基类（DoEverythingExecutor：随机）

            if (_verbose)
                Logger.WriteLine("[探针] 选项：正在结算=" + CurrentCardText()
                    + " options=[" + OptionsToText(options) + "]"
                    + " 想要=" + (wanted > 0 ? wanted.ToString() : "—")
                    + " 命中=" + result + (byValue ? "（按值）" : "（基类随机）"));

            return result;
        }

        /// <summary>
        /// 选项是不是"这张卡自己的"：内核的选项值＝`卡号*16+k`（k∈0..15，`aux.Stringid`），
        /// 所以"有值落在 [卡号*16, 卡号*16+16)"就是它的提问。
        /// 系统字符串（1056～1190，见 `strings.conf`）不属于任何卡，只按值匹配，不走这里
        /// （真实卡号都 ≥1000，区间起点 ≥16000，不会和它们撞上）。
        /// </summary>
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

        /// <summary>探针用：这一手在结算／发动哪张卡（链上拿不到就写"—"）。</summary>
        private string CurrentCardText()
        {
            ChainInfo chain = Duel.GetCurrentSolvingChainInfo();
            ClientCard card = chain != null ? chain.RelatedCard : Duel.GetCurrentChainCard();
            if (card == null)
                return "—";
            return card.Name + "(" + card.Id + ")";
        }

        /// <summary>探针用：把选项值拼成 "1,2,3"（值＝`卡号*16+k`，除以 16 是卡号、余数是 k）。</summary>
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

        /// <summary>
        /// 台风（破坏场上 1 张表侧表示的魔法·陷阱卡）：**只在自己场上没有表侧魔陷时才开**。
        ///
        /// 只要自己场上有表侧魔陷（未眠之城就摆在魔法陷阱区/场地区），破坏目标里就包含它们，
        /// 而基类选卡是从候选尾部取的——等于用自己的陷阱炸自己的引擎。
        /// 把条件定在"自己一张表侧魔陷都没有"，这时合法的目标只剩对面的，怎么选都不会伤到自己。
        /// </summary>
        private bool Typhoon()
        {
            // 台风的目标是"场上 1 张**表侧表示**的魔陷"，而且**必选 1 张**（不是"可以选"）——
            // 所以判据必须是"**对面有表侧魔陷**"：不然候选里只剩我们自己的卡，结果就是
            // 自己炸自己（用户实测："bot 发动台风把自己的魔陷飞掉了"）。
            bool enemyFaceup = false;
            foreach (ClientCard card in Enemy.GetSpells())
            {
                if (card.IsFaceup())
                {
                    enemyFaceup = true;
                    break;
                }
            }
            if (!enemyFaceup)
                return false;

            // GetSpells() 返回的是整个魔法·陷阱区（0-4 主区、5 场地区、6-7 灵摆区），
            // 魔法与陷阱都在里面，不用再单独看陷阱区。
            foreach (ClientCard card in Bot.GetSpells())
            {
                if (card.IsFaceup())
                    return false;
            }
            // ⚠ 这里**不能用** DefaultDontChainMyself()：它看到"本卡已经有专属规则"就返回 false，
            //    而我们现在就在这条专属规则里 —— 等于自己否掉自己，这张卡永远不会发动（踩过）。
            return true;
        }

        /// <summary>
        /// 通召时"召唤（攻击表示）还是盖放（守备）"。
        ///
        /// 基类的判断是「等级 ≤4 且自己场上没有表侧怪 且 对手全都打得过我」才盖——
        /// 而本家下级**全是等级 7**，所以按基类逻辑永远摆攻击表示 ✗（用户实测就是这个问题：
        /// 攻 700 的怪站在场上当靶子）。这里的规则更贴合这副牌：
        /// **守备力比攻击力高、且对手场上有打得过我攻击力的怪 → 盖放守备**；
        /// 对手打不过我（或场上没怪）→ 攻击表示，留着打人。
        /// </summary>
        public override bool OnSelectMonsterSummonOrSet(ClientCard card)
        {
            if (card == null || card.Defense <= card.Attack)
                return base.OnSelectMonsterSummonOrSet(card);
            return Util.GetBestPower(Enemy) > card.Attack;
        }

        /// <summary>
        /// 选卡：候选里**同时有对面和我方的卡时，一律先选对面的**。
        ///
        /// 适用场景就是"以场上 1 张（表侧表示的）卡为对象"这类效果——台风、落胤与圣女、
        /// 以及各类"破坏/弹回/除外 1 张场上卡"。这些效果的候选列表把双方都放进来，
        /// 而基类是从尾部取的，于是会炸掉自己的卡（用户实测："不要自己发陷阱炸自己"）。
        ///
        /// 只在**两侧都有候选**时才这么选；纯我方的候选（从牌组送去墓地、把手牌洗回牌组、
        /// 选自己的素材等）原样交给基类——那些是这副牌自己的取舍，不能一刀切。
        /// </summary>
        /// <summary>
        /// 打谁：**能一击打死就直接打脸**（基类只问"打得过哪只怪"，会先去撞怪，把一击致胜的直击浪费掉）。
        /// 其余交给基类。
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

        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            if (Duel.Phase == DuelPhase.BattleStart)
                return null;

            // 「破坏魔法·陷阱（0～N 张）」这类**可选**破坏：候选里**只有我方的卡**时一张都不选。
            // 基类会从候选尾部挑，候选全是自己的就等于"自己炸自己"——用户实测"bot 发动台风
            // 把自己的魔陷飞掉了"，当时对面场上确实一张魔陷都没有。
            if (min == 0 && hint == HintMsg.Destroy)
            {
                bool anyEnemy = false;
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 1)
                    {
                        anyEnemy = true;
                        break;
                    }
                }
                if (!anyEnemy)
                    return new List<ClientCard>();
            }

            // 候选里**同时有对面和我方的卡**时，一律先选对面的（见上面那段说明）。
            // 这条一直只写在注释里、**实现漏了** —— 台风就是踩在这上面的：`Typhoon()` 判据要求
            // "对面有表侧魔陷"之后，目标的候选列表里两边都有（对面那张 + 我们自己的「未眠之城」），
            // 而基类从尾部取 → 炸掉自己的场地（实测 `mb3-88.log` 20 局里 6 次，
            // 其中一次是"群星① 放顶→抽到→当场打出"的未眠之城，同一回合被自己炸掉）。
            // 只处理"破坏/弹回/除外场上的卡"这三类提示，且只在两侧都有候选时生效；
            // 纯我方的候选（检索、选素材、把自己场上的怪当代价）原样交给基类。
            if (min >= 1
                && (hint == HintMsg.Destroy || hint == HintMsg.ReturnToHand || hint == HintMsg.Remove))
            {
                List<ClientCard> enemyCards = new List<ClientCard>();
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 1 && enemyCards.Count < max)
                        enemyCards.Add(card);
                }
                if (enemyCards.Count >= min)
                    return enemyCards;
            }

            // 解放/吃自己场上的怪当费用：挑**最不心疼的**（token → 攻最低），
            // 基类从尾部取会把刚做出来的超量怪吃掉。
            if (min == 1 && max == 1 && hint == HintMsg.Release)
            {
                ClientCard cheapest = null;
                int cheapestScore = int.MaxValue;
                foreach (ClientCard card in cards)
                {
                    if (card.Controller != 0)
                        continue;
                    int score = (card.HasType(CardType.Token) ? -1000 : 0)
                        + System.Math.Max(card.Attack, 0) / 100;
                    if (score < cheapestScore)
                    {
                        cheapestScore = score;
                        cheapest = card;
                    }
                }
                if (cheapest != null)
                {
                    List<ClientCard> picked = new List<ClientCard>();
                    picked.Add(cheapest);
                    return picked;
                }
            }

            // 下面三个"我们自己挑"的分支都只返回 **1 张**卡。引擎给的区间是 min~max：
            // 只要 min > 1（要求选两张以上），返回 1 张就是**非法响应**——效果会被判无效、
            // 这一回合白过（房间局里出现过"3~4 个动作、400 秒"的硬失败）。
            // 所以先校验区间，不满足就整个让给基类。
            bool canPickOne = min <= 1 && max >= 1;

            if (_sheeraReturn)
            {
                _sheeraReturn = false;
                if (canPickOne)
                {
                    IList<ClientCard> returned = SelectReturnTarget(cards);
                    if (returned != null)
                        return returned;
                }
            }

            if (_kiraSummon)
            {
                _kiraSummon = false;
                if (canPickOne)
                {
                    IList<ClientCard> summoned = PickArchetypeXyz(cards);
                    if (summoned != null)
                        return summoned;
                }
            }

            if (_deckSend)
            {
                _deckSend = false;
                if (canPickOne)
                {
                    IList<ClientCard> sent = PickDeckSend(cards);
                    if (sent != null)
                        return sent;
                }
            }

            if (_seizaPlace)
            {
                _seizaPlace = false;
                if (canPickOne)
                {
                    IList<ClientCard> placed = PickSeizaPlace(cards);
                    if (placed != null)
                        return placed;
                }
            }

            // 「未眠之城」② 的 k=1 分支（"把 1 张手牌洗回牌组再抽 1"，脚本 c72978038 第 55~57 行：
            // 提示 HINTMSG_TODECK、候选是我方手牌）与「帐 西艾蘿」的手牌效果（脚本 c75934716 第 74 行）
            // 是同一道题：**别让基类从尾部随便挑一张**——它可能把我们刚抽到的本家洗回去。
            // 两个效果的取舍口径相同（见 SelectReturnTarget：只动手牌、优先非本家），所以一起接。
            // 早先只有西艾蘿那条路（靠 _sheeraReturn 待办）走进了这里；未眠之城这条一直漏着。
            // ⚠ "结束阶段把墓地/除外的本家洗回牌组"那道题（脚本第 96 行起）候选在墓地/除外，不是手牌，
            //   不会落进这里（下面按 Location 过滤）。
            if (min == 1 && max == 1 && hint == HintMsg.ToDeck)
            {
                bool allInHand = true;
                foreach (ClientCard card in cards)
                {
                    if (card.Controller != 0 || card.Location != CardLocation.Hand)
                    {
                        allInHand = false;
                        break;
                    }
                }
                if (allInHand)
                {
                    IList<ClientCard> returned = SelectReturnTarget(cards);
                    if (returned != null)
                        return returned;
                }
            }

            // 抽卡面包的"弃 1 张手卡"（脚本 c83838727 第 31~37 行：抽到怪兽、且那个属性已经在自己墓地里
            // 时才走这条，提示是 HINTMSG_DISCARD）：候选全是我方手牌，基类从尾部随便取一张。
            if (min == 1 && max == 1 && hint == HintMsg.Discard)
            {
                IList<ClientCard> discarded = PickDiscardTarget(cards);
                if (discarded != null)
                    return discarded;
            }

            // 「落胤与圣女」① 的**费用**（脚本 c30271097 第 46~52 行：从额外牌组送 1 只「阿不思」名怪兽
            // 去墓地，提示 HINTMSG_TOGRAVE、候选全在额外牌组）：优先送「烙印龍 阿爾比昂」——
            // 它进墓地才有意义（② 在结束阶段能翻「烙印」魔陷，且「黑龙之艾克莉西娅」的墓地 ② 要
            // "自己墓地有 8 星融合怪兽"配套），基类从尾部取则是在两只里碰运气。
            if (min == 1 && max == 1 && hint == HintMsg.ToGrave && IsExtraDeckCards(cards))
            {
                IList<ClientCard> sent = PickExtraToGrave(cards);
                if (sent != null)
                    return sent;
            }

            // 超量召唤的素材：**只吃卡面要求的最少数量**（见 PickXyzMaterials 的说明）。
            // 这道题的 hint＝513（`strings.conf` 第 97 行"请选择要作为超量素材的卡"）；
            // "取除已有的超量素材"是另一道题（hint＝519，候选是超量素材区的卡），
            // 由下面的 `_materialCap` 分支负责，两道题不会撞车。
            // ⚠ 必须排在上面几个"待办"分支**之后**：輝煌的① 也是"从额外牌组叫怪"，
            //   那道题由 `_kiraSummon` 接手（要优先叫大姐），不能被这里抢走。
            //   PickXyzMaterials 另外只认**怪兽区**上的卡，额外牌组里的候选也会被排除。
            // ⚠ **min ≥ 2**＝真的在挑两只以上的素材（数量由 PickXyzMaterials 管）；
            //   **min ＝ max ＝ 1**＝上叠召唤时"拿哪只当底"（卡文里那句"也能在……超量怪上面重叠来超量召唤"），
            //   交给 PickOverlayBase 挑最不心疼的那只——梯子的 LadderUpgrade 闸门就是按"最低打点的那只"
            //   算的，两边必须同一口径（以前这里交给基类从尾部取，等于把打点最高的那只拿去换壳）。
            if (hint == HintMsg.XyzMaterial && min >= 2)
            {
                List<ClientCard> materials = PickXyzMaterials(cards, min);
                if (materials != null)
                    return materials;
            }
            else if (hint == HintMsg.XyzMaterial && min == 1 && max == 1 && IsOwnXyzMonsters(cards))
            {
                IList<ClientCard> overlayBase = PickOverlayBase(cards);
                if (overlayBase != null)
                    return overlayBase;
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
            {
                // 纯我方候选：只在登记过"最多选几张"时（千金大獎②）插手，其余照旧交给基类
                if (mine.Count > 0 && CapOwnSelection(mine, min, max, out IList<ClientCard> capped))
                    return capped;
                return base.OnSelectCard(cards, min, max, hint, cancelable);
            }

            List<ClientCard> selected = new List<ClientCard>();
            foreach (ClientCard card in theirs)
            {
                if (selected.Count >= max)
                    break;
                selected.Add(card);
            }
            // 对面不够 min 张时用我方的补齐（跟基类同序：从尾部取）
            for (int i = 1; selected.Count < min && i <= mine.Count; ++i)
                selected.Add(mine[mine.Count - i]);
            return selected;
        }

        /// <summary>
        /// 纯我方候选时的"最多选几张"限制（<see cref="_materialCap"/>）：千金大獎的 ② 写的是
        /// "任意数量"，而基类会从候选尾部一路取到上限——等于每触发一次就把素材拔光。
        /// 只限制数量，不改变取哪几张（仍是基类的从尾部取）。
        /// </summary>
        private bool CapOwnSelection(IList<ClientCard> mine, int min, int max, out IList<ClientCard> selected)
        {
            selected = null;
            if (_materialCap <= 0 || min > _materialCap)
                return false;
            int limit = _materialCap < max ? _materialCap : max;
            selected = new List<ClientCard>();
            for (int i = 1; selected.Count < limit && i <= mine.Count; ++i)
                selected.Add(mine[mine.Count - i]);
            _materialCap = 0;      // 只认一次
            return selected.Count > 0;
        }

        /// <summary>
        /// 超量召唤时"要拿哪几张当素材"：从候选里挑 **刚好 `count` 张**（`count` ＝内核给的下限，
        /// 也就是卡面写的最少素材数），**不是**基类那样"从尾部一路取到上限"。
        ///
        /// **为什么必须接管数量**：「千金大獎」的素材写的是"7 星怪兽×2 只以上"
        /// （脚本 `c9484285` 第 4 行 `aux.AddXyzProcedure(c,nil,7,2,nil,nil,99)`：
        /// 下限 2、上限 99＝"随便几只都行"），而基类 `DoEverythingExecutor.OnSelectCard`
        /// 是 `for (i = 1; i &lt;= max; ++i) pick(cards[cards.Count - i])`——**取满 max 张**，
        /// 于是把场上能当素材的怪**全部**塞进去。实测 `interact-88v94.log`：
        /// 局 1（12:11:59）菈碧＋卡萝尔×2、局 15（12:14:58）、局 16（12:15:14）卡萝尔×2＋菈碧，
        /// **三次都是三只叠一只**；而它同一次叠放的 ② 按本文件 `Daishou()` 的规则每次只拔 1 个素材
        /// （多吃的身体换不来第二次弹卡，③ 还会自动从对手卡组顶补素材），第三次那只就是白扔：
        /// 局 15/16 白扔的正好是菈碧——它是这副牌**唯一**能在对手回合加速超量的卡（卡文 47828069 ③）。
        /// 另外 `一掷乾坤`（脚本 `c73090586` 第 4 行 `AddXyzProcedure(...,7,2)`）下限＝上限＝2，
        /// 以前靠"尾部取 2 张"撞运气，现在也走这里按下面的顺序挑。
        ///
        /// **挑哪几张**：「在场上有用的」留到最后才动，其余按"最不心疼"（攻低者先）——
        /// 与本文件 `OnSelectCard` 里 Release 那条同一套口径。保到最后的两张是：
        /// · 帐 西艾蘿（75934716）：场上的 ③ 是自己·对方主要阶段抽 1，每回合白赚一张；
        /// · 菈碧（47828069）：场上的 ③ 是"对手回合加速超量"，本副牌唯一的对手回合干扰/展开点。
        /// 候选不够时照样从这两张里取——数量必须先合法（少给是非法响应，见 `OnSelectCard` 的说明）。
        /// </summary>
        private static List<ClientCard> PickXyzMaterials(IList<ClientCard> cards, int count)
        {
            if (cards == null || count <= 0)
                return null;

            // 只看**自己怪兽区**上的候选：额外牌组里的候选（輝煌①那种"从额外叫怪"的提问）
            // 与超量素材区里的候选（取除素材的提问）都不该由这里决定。
            List<ClientCard> field = new List<ClientCard>();
            foreach (ClientCard card in cards)
            {
                if (card.Controller == 0 && card.Location == CardLocation.MonsterZone)
                    field.Add(card);
            }
            if (field.Count < count)
                return null;

            List<ClientCard> picked = new List<ClientCard>();
            while (picked.Count < count)
            {
                ClientCard best = null;
                int bestScore = int.MaxValue;
                foreach (ClientCard card in field)
                {
                    if (picked.Contains(card))
                        continue;
                    int score = XyzMaterialScore(card);
                    if (score < bestScore)
                    {
                        bestScore = score;
                        best = card;
                    }
                }
                if (best == null)
                    break;
                picked.Add(best);
            }
            return picked.Count == count ? picked : null;
        }

        /// <summary>
        /// 当超量素材时的"心疼程度"（越小越先拿去当素材，见 <see cref="PickXyzMaterials"/>）：
        /// 攻低者先走；「帐」「菈碧」这两张**只在场上有用**的额外记一大笔分，排到最后。
        /// </summary>
        private static int XyzMaterialScore(ClientCard card)
        {
            int score = System.Math.Max(card.Attack, 0) / 100;
            if (card.IsOriginalCode(CardId.Sheera_Curtain) || card.IsOriginalCode(CardId.Rabi))
                score += 10000;
            return score;
        }

        /// <summary>
        /// A/B 测量开关：环境变量 `RAISEMOON_AB` 指定"只开哪一条改动"。
        /// 取值：all（默认，全部生效）／off（全关）／cap（只开"千金大獎每次最多拔 1 素材"）／
        /// sheera（只开"西艾蘿只洗手牌"）。
        /// 只有做对照测量时才设它，生产不设＝all。
        /// </summary>
        private static readonly string AbMode = System.Environment.GetEnvironmentVariable("RAISEMOON_AB") ?? "all";

        private static bool AbOn(string flag)
        {
            return AbMode == "all" || AbMode == flag || AbMode.Contains(flag);
        }

        /// <summary>
        /// 这回合的攻击**能不能真的把人打死**：够得着对面的场面 + 打点总和能到 0。
        ///
        /// 只用于回答"要不要为斩杀多花素材"（见 <see cref="AntimatterLethal"/>），不参与常规出牌。
        /// 群里反馈："第二次甚至想直接叠放斩杀，可是我场上三张卡，他明显斩不了的啊。"
        /// </summary>
        private bool CanPushLethal()
        {
            if (Bot.GetMonsterCount() == 0)
                return false;
            int best = Util.GetBestPower(Enemy);
            if (Util.GetBestAttack(Bot) < best && best > 0)
                return false;       // 连对面最大的怪都打不过，谈不上打人
            int attackers = 0;
            int total = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (!card.IsFaceup() || card.Attack <= 0)
                    continue;
                if (card.Attack >= best)
                    ++attackers;
                total += card.Attack;
            }
            if (attackers <= Enemy.GetMonsterCount())
                return false;       // 攻击次数不够穿过对面的怪
            return total >= Enemy.LifePoints;
        }

        // ============================================================ 斩杀优先（与闪刀/卡通/魔女术/杀调/虫惑魔同一套）

        /// <summary>是否打印调试信息（命令行 ``Debug=true``）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>
        /// 这个回合**进过场的怪**（卡号）：给斩杀判据用——本回合才上场的怪保守地先不算进"这回合能打多少"
        /// （有的怪写着"特殊召唤的回合不能攻击"），宁可晚一步也不要在打不死时白进战阶。
        /// 回合切换时清（<see cref="OnNewTurn"/>）。
        /// </summary>
        private readonly HashSet<int> _summonedThisTurn = new HashSet<int>();

        /// <summary>每回合清"这个回合才上场的怪"（对手回合里的特召也清：那些怪到我们回合已经能攻击了）。</summary>
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
        /// 内层遍历候选"，战斗阶段要等所有规则都不出手才轮得到；排在后面的话"还有事可做"会把战阶一直推后，
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
        /// 升辉月是超量牌、本家下级又都是"攻 700 / 守 2100"的等级 7，平时站守备更稳
        /// （大姐与千金大獎的 ② 都是拔素材，不靠打点吃饭）；但对面空场、且站攻击就能收掉时，
        /// 表示形式会直接决定这一刀打不打得出去。所以这里用**保守写法**：只在
        /// "对面空场 + 加上这一刀就够了"时点头，**不做**"对面空场一律攻击表示"。
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
        /// 群星 ①"从牌组挑 1 张「盈彩月夜」卡放在牌组顶"要挑哪张。
        ///
        /// 这是这副牌**唯一能主动挑**的一张（伪检索），挑什么直接决定下回合引擎开不开；
        /// 而基类是从候选尾部乱取，等于把这次机会浪费在随机一张上。
        /// 优先顺序按"下一抽能立刻把场面推起来"排：
        /// · 卡蘿爾——召唤不用解放、召唤即抽 1，一张牌换一个场面加一次抽；
        /// · 未眠之城（**场上没有时**）——放到顶＝下抽引擎魔法，之后每回合白抽一张；
        /// · 西艾蘿——手牌能洗 1 张回牌组自跳；
        /// · 菈碧／希耶娜／庫露雪／斯奎茲／輝煌——剩下的本家件。
        /// </summary>
        private IList<ClientCard> PickSeizaPlace(IList<ClientCard> cards)
        {
            // **先看自己够不够素材**：手上＋场上加起来不到 2 只等级 7，就先放能自跳的下级
            //（西艾萝 > 卡萝尔），把场面凑起来；够素材时再放场地去滚抽卡。
            // 这一条来自操作方给的展开 spec：「手牌缺少终端素材 → 放西艾萝；否则 → 放未眠之城」。
            //
            // ⚠ 0.21.90 加过一条"素材不够、且未眠之城不在场也不在手牌时也先放它"的例外，0.21.91 曾按 20 局口径回退，
            // **0.21.99 按 30 局口径重新开启**：同种子 30 局两种口径一起量——
            // * 回退版（本条关闭）：达标 **4/30**、空场 6/30；
            // * 开启版（20 局口径那次）：达标 **11/30**、空场 **3/30**。
            // 两个口径都是开启更好（本家零检索、起手摸不到场地，群星① 几乎是它唯一来源，
            // 而验收合格线正是"大姐站额外区 **＋ 未眠之城在场**"），所以 20 局那次回退是**采样误判**，这里改回来。
            // 手里的场地照原 spec 走（那就先放能自跳的下级），只有"不在场也不在手牌"才抢这一次机会。
            bool nemuriMissing = !Bot.HasInSpellZone(CardId.Nemuri, notDisabled: true)
                && !Bot.HasInHand(CardId.Nemuri);
            if (LevelSevenCount() < 2 && !nemuriMissing)
            {
                foreach (int id in new[] { CardId.Sheera_Curtain, CardId.Carol })
                {
                    IList<ClientCard> body = PickFirst(cards, id);
                    if (body != null)
                        return body;
                }
            }
            if (!Bot.HasInSpellZone(CardId.Nemuri, notDisabled: true))
            {
                IList<ClientCard> nemuri = PickFirst(cards, CardId.Nemuri);
                if (nemuri != null)
                    return nemuri;
            }
            int[] wanted =
            {
                CardId.Carol, CardId.Sheera_Curtain, CardId.Rabi, CardId.Hiena, CardId.Cruse,
                CardId.Squeeze, CardId.Kira,
            };
            foreach (int id in wanted)
            {
                IList<ClientCard> picked = PickFirst(cards, id);
                if (picked != null)
                    return picked;
            }
            return null;
        }

        /// <summary>
        /// 手上＋场上等级 7 的怪兽数量——群星① 用它判断"终端素材够不够"：
        /// 不到 2 只时优先把能自跳的下级放到牌组顶（操作方 spec 的起手处理）。
        /// </summary>
        private int LevelSevenCount()
        {
            int count = 0;
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.Level == 7)
                    ++count;
            }
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.Level == 7)
                    ++count;
            }
            return count;
        }

        /// <summary>从候选里挑第一张指定卡号的卡（没有就返回 null）。</summary>
        private static IList<ClientCard> PickFirst(IList<ClientCard> cards, int cardId)
        {
            foreach (ClientCard card in cards)
            {
                if (card.IsOriginalCode(cardId))
                {
                    IList<ClientCard> selected = new List<ClientCard>();
                    selected.Add(card);
                    return selected;
                }
            }
            return null;
        }

        /// <summary>
        /// 「愚蠢的副葬」（从卡组把 1 张魔法·陷阱送去墓地）该送哪张：**只送带墓地效果的那两张**
        /// ——陷阱「盈彩月夜之輝煌」（② 从墓地除外可以抽 1）与本家魔法「盈彩月夜之群星」
        /// （② 从墓地除外可以发动）。
        ///
        /// 群里反馈："副埋堆落圣？月升副埋就两种——一张陷阱、一张本家魔法，不会埋其他的。"
        /// 基类是从候选尾部随便取，才会把「落胤与圣女」这种没有墓地用途的卡埋掉。
        /// </summary>
        private IList<ClientCard> PickDeckSend(IList<ClientCard> cards)
        {
            int[] wanted = { CardId.Kira, CardId.Seiza };
            foreach (int id in wanted)
            {
                foreach (ClientCard card in cards)
                {
                    if (card.IsOriginalCode(id))
                    {
                        IList<ClientCard> selected = new List<ClientCard>();
                        selected.Add(card);
                        return selected;
                    }
                }
            }
            return null;
        }

        /// <summary>
        /// 反物质龙（在超量怪上面重叠召唤的 R9）：**打不死就别做、别开连击**。
        ///
        /// 它的 ① 是"只要还有超量素材，给对手的战斗伤害减半"——这是一张纯进攻的怪，
        /// 只有真的能打死人时做它才不亏；打不死的时候做它＝把一只超量（连同素材）换成减半打点。
        /// 这是本轮唯一的新行为改动，**默认开**（`AbOn("lethal")` 在 `all` 下为真）：
        /// A/B 实测 lethal 腿 2/16、去掉它 1/16，属中性偏好，且它对应群里明确的一条反馈
        /// （"明显斩不了还去叠放斩杀"），所以留在默认里。
        /// </summary>
        private bool AntimatterLethal()
        {
            if (!AbOn("lethal"))
                return true;
            return CanPushLethal();
        }

        /// <summary>
        /// （已删除）"额外怪兽区空着才做大姐"那条规则。
        ///
        /// 0.21.14 加过它，理由是"大姐的三条效果只在自己额外区才有"。**实测把它删掉了**：
        /// 同一副牌同一牌序，三条改动全关 16 局胜 3（18.8%、动作 25.1:33.0），
        /// 只留这一条 16 局**胜 0**（动作 30.2:43.8）——它把大姐拦掉的次数远多于它救到的场面。
        /// 大姐在额外区的问题已由 0.21.16"陷阱优先叫大姐"从源头解决（谁先占额外区谁吃效果），
        /// 不需要再靠不做大姐来回避。
        /// </summary>
        private bool Ichigekan()
        {
            return Bot.GetMonstersInExtraZone().Count == 0;
        }

        /// <summary>
        /// 千金大獎的 ②：拔素材弹对面同样数量的卡。这里只**登记**"最多拔 1 个"，
        /// 真正限制在 OnSelectCard 里做——因为它写的是"任意数量"，而基类选卡是从候选尾部
        /// 一路取到上限，等于每次触发都把素材拔光。
        /// </summary>
        private bool Daishou()
        {
            if (AbOn("cap"))
                _materialCap = 1;
            // ⚠ 同上：在专属规则里再调 DefaultDontChainMyself() 会返回 false（本卡已有专属规则）。
            return true;
        }

        /// <summary>
        /// 银河眼光波刃龙 ①（拔 1 个素材炸场上 1 张卡）：**素材只剩 1 个时不拔**。
        /// 拔完它就是个 3000 白板，而且再也不能用这个效果（用户反馈："自己的光波一张素材都没有"）。
        /// </summary>
        private bool CipherBladeDragon()
        {
            if (Card == null || !Card.HasXyzMaterial(2))
                return false;
            // ⚠ 同上：专属规则里不能用 DefaultDontChainMyself()，它返回 false 等于自己否掉自己。
            return true;
        }

        /// <summary>
        /// 「白色幻兽-青眼白龙」的三条效果（脚本 `c30397786.lua`：①＝描述 0、②＝描述 1、③＝描述 2）。
        /// **它原来没有专属规则**（卡表里是非本家的 1 张打手），于是掉进通用兜底、被"能从额外卡组出怪
        /// 就让路"整条否掉——连保命的 ③ 都不发，就成了一只站在场上不动的 3000 白板。
        /// 规则按卡文写：
        /// * ①（在手牌、被从卡组加入手卡时／自己怪兽被战斗破坏时 → 展示它特召自身）：能发就发；
        /// * ②（从手卡·卡组特召时 → **破坏对方场上全部怪兽**）：**只在对面场上有怪时发**——
        ///   它附带"这个回合自己不用「青眼」怪兽不能直接攻击"，对面空场时开了只是白亏自己的直击；
        /// * ③（场上的它成为效果对象时，丢 1 张手卡 → 那个效果无效）：**有手牌就发**（它是唯一的保命手段）。
        /// ⚠ 两个印刷号都认：卡表（AI_Gen88.ydk）用的是 30397787，它的脚本 `c30397787.lua` 用
        ///   `GetID()`（＝30397787）设描述，所以描述号也必须带上 30397787——只比基准号会永远对不上。
        /// </summary>
        private bool WhitePhantom()
        {
            if (Card == null)
                return true;
            if (Card.Location == CardLocation.Hand)
                return true;
            int desc = ActivateDescription;
            if (desc == Util.GetStringId(CardId.WhitePhantom, 1) || desc == Util.GetStringId(CardId.WhitePhantomAlt, 1))
                return Enemy.GetMonsterCount() > 0;
            if (desc == Util.GetStringId(CardId.WhitePhantom, 2) || desc == Util.GetStringId(CardId.WhitePhantomAlt, 2))
                return Bot.Hand.Count > 0;
            return true;
        }

        // ============================================================ 本家之外的引擎与终端（⑫ 的规则）

        /// <summary>
        /// 阿宙斯的 ①（把场上**其他**卡全部送墓，拔 2 个素材）值不值：**对面场上（怪兽＋魔陷/场地）
        /// 比我们会掉的卡多**才发。卡文写的是"场上的其他卡全部送去墓地"——是**双方**的全场，
        /// 包括我们自己的「未眠之城」「辉煌」和盖着的陷阱，所以只有"对面掉得比我们多"才是赚的。
        /// 两个例外：阿宙斯自己（"其他卡"）；大姐站在额外怪兽区时"不受其他卡的效果影响"
        ///（脚本 c73090586 的 imcon：GetSequence() > 4），送不掉，不计进我们会掉的那一侧。
        /// </summary>
        private bool ZeusWipeWorthIt()
        {
            int enemyCards = Enemy.GetMonsters().Count + Enemy.GetSpells().Count;
            if (enemyCards == 0)
                return false;

            int ourCards = Bot.GetMonsters().Count + Bot.GetSpells().Count;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.IsOriginalCode(CardId.Zeus))
                    --ourCards;
            }
            foreach (ClientCard card in Bot.GetMonstersInExtraZone())
            {
                if (card.IsOriginalCode(CardId.Ichigekan))
                    --ourCards;
            }
            return enemyCards > ourCards;
        }

        /// <summary>
        /// 阿宙斯的召唤：**只在清场划算时才叠**（<see cref="ZeusWipeWorthIt"/>）。
        /// 它是"12 阶 3000 打点 + 一发全场清 + ② 每回合补素材"，但对空白/对面空场时毫无意义——
        /// 而底座（那一只超量）会变成素材、从场上消失。对面没场面时留着现有那只更实在
        ///（操作方的 spec 也是"后手：千金清场，**必要时**叠阿宙斯"）。
        /// </summary>
        private bool ZeusSummon()
        {
            return ZeusWipeWorthIt();
        }

        /// <summary>
        /// 阿宙斯的效果：① 清场（脚本 c90448282 第 8 行，描述 k=1）走 <see cref="ZeusWipeWorthIt"/>；
        /// ② 补素材（描述 k=2：自己场上其他卡被破坏时，从手卡/卡组/额外卡组压 1 张到它下面）是白赚，
        /// 能发就发。**认不出描述时按 ① 处理**（保守：清场是唯一会把自家场面一起带走的那个）。
        /// </summary>
        private bool ZeusEffect()
        {
            if (Card != null && ActivateDescription == Util.GetStringId(CardId.Zeus, 2))
                return true;
            return ZeusWipeWorthIt();
        }

        /// <summary>
        /// 8 阶梯子唯一的入口「黑智天至 伊里斯斐尔」：卡文"也能在这个回合没有在怪兽区域把效果发动的
        /// 自己场上的超量怪兽上面重叠来超量召唤"——它的上叠条件最松（上面那些 8/9/12 阶都要 8 阶或
        /// 「银河眼」底座，绕不开它）。
        ///
        /// 它本体只有 800/2500（① 只在本回合战斗阶段给超量怪 +阶级×100，② 是破坏代替），把一只本家 R7
        /// （大姐/千金）换成它，是**拿本家的效果（大姐的抽卡·免疫、千金的弹卡）换一道墙**。
        /// 这副牌零检索、全靠抽，本家的每一张都很贵，所以只在我们**还留得住别的超量**时才走这一步：
        /// 场上自己的超量怪 ≥ 2 只（含额外怪兽区；大姐虽然不能当素材，但她算"留下的那只"）。
        /// </summary>
        private bool IrisfeerClimb()
        {
            if (!AbOn("ladder"))
                return true;
            int xyz = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.IsFaceup() && card.HasType(CardType.Xyz))
                    ++xyz;
            }
            return xyz >= 2;
        }

        /// <summary>
        /// 梯子上层（光子咆哮 4500／重铠 4000／光波刃龙 3200）：**只在打点盖过场上最强的那只超量时才叠**。
        ///
        /// 这三张都能每回合"拔 1 个素材除 1 张卡"（重铠 ② 与光波 ① 同款；光子咆哮 ② 在
        /// <see cref="PhotonHowlingEffect"/> 里基本不放开），除卡能力等价，所以按打点论高下。
        /// 实测（对空白 30 局）基类一回合连爬：千金(1400) → 伊里斯斐尔(800) → 光子咆哮(4500)
        /// → 重铠(4000) → 光波刃龙(3200)，素材一路跟着搬过去——后两跳就是把同一批素材换个打点更低的壳。
        /// </summary>
        private bool LadderUpgrade()
        {
            if (!AbOn("ladder"))
                return true;
            if (Card == null)
                return false;
            return Card.Attack > StrongestOurXyzAttack();
        }

        /// <summary>场上自己超量怪里最高的打点（大姐也算——"场上最强的那只"就是她；0＝一只都没有）。</summary>
        private int StrongestOurXyzAttack()
        {
            int best = 0;
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (!card.IsFaceup() || !card.HasType(CardType.Xyz))
                    continue;
                int attack = System.Math.Max(card.Attack, 0);
                if (attack > best)
                    best = attack;
            }
            return best;
        }

        /// <summary>
        /// 未来No.0 未来龙皇 霍普：卡文要"「No.」怪兽以外的**相同阶级**超量怪 ×3"
        ///（另一条路是叠在「未来皇」上，那张不在这副牌里）。本副牌的超量只有 2 张千金 + 2 张大姐，
        /// 而大姐写着不能当素材（脚本 c73090586 的 EFFECT_CANNOT_BE_XYZ_MATERIAL）——**凑不出 3 张**。
        /// 所以按卡文把闸门写死：自己场上同阶级、可当素材的超量 ≥ 3 只才做（本副牌实际上永不满足）。
        /// 登记它的意义是"别让基类随机叠出来"——把 3 只超量并成 1 只在"零检索、靠场值"的这副牌里是净亏。
        /// </summary>
        private bool UtopicZeroClimb()
        {
            return CountSameRankXyz() >= 3;
        }

        /// <summary>场上可当素材的超量怪里，同一阶级最多有几只（未来No.0 的闸门用）。</summary>
        private int CountSameRankXyz()
        {
            Dictionary<int, int> byRank = new Dictionary<int, int>();
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (!card.IsFaceup() || !card.HasType(CardType.Xyz))
                    continue;
                if (card.IsOriginalCode(CardId.Ichigekan))
                    continue;       // 大姐写着不能当素材
                int rank = card.Rank;
                if (!byRank.ContainsKey(rank))
                    byRank[rank] = 0;
                ++byRank[rank];
            }
            int best = 0;
            foreach (KeyValuePair<int, int> pair in byRank)
            {
                if (pair.Value > best)
                    best = pair.Value;
            }
            return best;
        }

        /// <summary>
        /// 银河眼重铠光子龙 ②"拔 1 个素材破对面 1 张表侧卡"：**素材只剩 1 个时不拔**——与「光波刃龙」
        /// 同一条口径（拔完就是一只没有效果的白板）。① 是"把最多 2 张装备卡压在下面当素材"，
        /// 这副牌没有装备卡、正常不会出现；认得出 ① 就放行，其余（含描述未知）一律按 ② 把关。
        /// </summary>
        private bool FullArmorPhotonEffect()
        {
            if (Card == null)
                return false;
            if (ActivateDescription == Util.GetStringId(CardId.FullArmorPhoton, 1))
                return true;
            return Card.HasXyzMaterial(2);
        }

        /// <summary>
        /// 超银河眼光子龙-光子咆哮：
        /// · ①（超量召唤成功时从卡组叫 1 只「光子」怪）——**这副牌的卡组里一张「光子」都没有**，
        ///   所以是死效果（内核不会给空发的候选），认得出就点头；
        /// · ②（拔 3 个素材 ＋ **解放自己场上另 1 只超量怪** → 这张卡以外场上全部表侧卡的效果
        ///   直到回合结束无效）是**对称**的：自家场地/盖牌/其他超量的效果也会一起失效，还要再赔一只超量。
        ///   只在对面的回合、对面表侧卡 ≥ 2 张（正在铺场）时才掐这一下，其余一律不发。
        /// ⚠ ② 在脚本里没有 SetDescription（提示是 -1），所以判据反过来写：**认得出 ① 才放行**，
        ///   其余（含描述未知）都按 ② 把关。
        /// </summary>
        private bool PhotonHowlingEffect()
        {
            if (Card == null)
                return false;
            if (ActivateDescription == Util.GetStringId(CardId.PhotonHowling, 0)
                || ActivateDescription == Util.GetStringId(CardId.PhotonHowlingBase, 0))
                return true;
            if (Duel.Player != 1)
                return false;
            int faceupEnemy = 0;
            foreach (ClientCard card in Enemy.GetMonsters())
            {
                if (card.IsFaceup())
                    ++faceupEnemy;
            }
            foreach (ClientCard card in Enemy.GetSpells())
            {
                if (card.IsFaceup())
                    ++faceupEnemy;
            }
            return faceupEnemy >= 2;
        }

        /// <summary>
        /// 鲜花女男爵：①（场上 1 张卡破坏）与 ②（一次：效果发动无效并破坏）都留着；
        /// **③ 不做**——"准备阶段把自己退回额外卡组、从墓地特召 1 只 9 星以下怪"是拿一张**还活着的康**
        ///（3000 打点 + ② 还没用掉）去换一只墓地怪。这副牌的手坑就是调整、女男爵是那两条同调线的落点，
        /// 站住比换掉值钱。③ 的描述是 k=2（脚本 c84815190 第 3 个效果）；认不出描述时不拦。
        /// </summary>
        private bool BaronneEffect()
        {
            if (Card != null && ActivateDescription == Util.GetStringId(CardId.Baronne, 2))
                return false;
            return true;
        }

        /// <summary>
        /// 手坑当调整去通召——**只在这一张手坑真是调整、且真能凑出额外卡组里那张同调时才允许**：
        /// 卡库里灰流丽/幽鬼兔/**朔夜时雨**(3★)、效果遮蒙者(1★) 都是调整，本家下级全是 7 星，
        /// 所以 3★＋7★＝10 →「鲜花女男爵」、1★＋7★＝8 →「黑龙之艾克莉西娅」。
        /// ⚠ **必须查 `CardType.Tuner`**：手坑里还有 1★ 的「小丑与锁鸟」（非调整）、2★ 的「增殖的G」、
        ///   4★ 的欢聚友伴两张（非调整）——只按等级判会把锁鸟当成 1★ 调整放上场（白白丢掉一张阻抗，
        ///   而且它跟 7 星凑不出同调）。等级之外再按等级查"额外卡组里有没有对应星级"。
        /// 另外两点：场上要有另一只 7 星当同调素材；本家的通召点（卡萝尔/菈碧）还在候选里时让路——
        /// 它们不用付手坑的代价，优先级更高。
        /// </summary>
        private bool HandTrapTunerSummon()
        {
            if (!AbOn("synchro"))
                return false;
            if (Card == null || !Card.HasType(CardType.Tuner))
                return false;
            bool synchroFits;
            if (Card.Level == 3 && Bot.HasInExtra(CardId.Baronne))
                synchroFits = true;
            else if (Card.Level == 1 && Bot.HasInExtra(CardId.Ecclesia))
                synchroFits = true;
            else
                synchroFits = false;
            if (!synchroFits)
                return false;
            if (Duel.MainPhase != null)
            {
                foreach (ClientCard candidate in Duel.MainPhase.SummonableCards)
                {
                    if (candidate.IsOriginalCode(CardId.Carol) || candidate.IsOriginalCode(CardId.Rabi))
                        return false;
                }
            }
            foreach (ClientCard monster in Bot.GetMonsters())
            {
                if (monster.IsFaceup() && monster.Level == 7)
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 抽卡面包"弃 1 张手卡"弃哪张（脚本 c83838727 第 31~37 行）：先丢**手上有重复的同名卡**
        ///（多出来的那一张），再丢通用魔陷（落胤与圣女/愚蠢的副葬/台风/次元障壁/同契魔术/三战之才…），
        /// 本家引擎与手坑最后才动（本家是唯一的展开来源、手坑是唯一的阻抗）。
        /// 打分越小越先丢：同名每多一张 +100、本家 +50、手坑 +40、场地/群星 +30。
        /// </summary>
        private IList<ClientCard> PickDiscardTarget(IList<ClientCard> cards)
        {
            ClientCard chosen = null;
            int bestScore = int.MaxValue;
            foreach (ClientCard card in cards)
            {
                if (card.Controller != 0 || card.Location != CardLocation.Hand)
                    continue;
                int score = (CountSameNameInHand(card) - 1) * 100;
                if (IsEngineCard(card))
                    score += 50;
                foreach (int id in HandTraps)
                {
                    if (card.IsOriginalCode(id))
                        score += 40;
                }
                if (card.IsOriginalCode(CardId.Nemuri) || card.IsOriginalCode(CardId.Seiza))
                    score += 30;
                if (score < bestScore)
                {
                    bestScore = score;
                    chosen = card;
                }
            }
            if (chosen == null)
                return null;
            IList<ClientCard> selected = new List<ClientCard>();
            selected.Add(chosen);
            return selected;
        }

        /// <summary>自己手牌里和这张卡同名的有几张（弃牌时优先丢多余的那张）。</summary>
        private int CountSameNameInHand(ClientCard card)
        {
            int count = 0;
            foreach (ClientCard hand in Bot.Hand)
            {
                if (hand.Name == card.Name)
                    ++count;
            }
            return count;
        }

        /// <summary>
        /// 「落胤与圣女」① 的费用送哪只「阿不思」名怪兽去墓地：优先「烙印龍 阿爾比昂」
        ///（它的墓地 ② 才有意义），其次「黑龙之艾克莉西娅」（墓地 ② 要配 8 星融合怪，正好和前者成套）；
        /// 候选里都没有就返回 null，交回基类。
        /// </summary>
        private static IList<ClientCard> PickExtraToGrave(IList<ClientCard> cards)
        {
            int[] wanted = { CardId.Albion, CardId.Ecclesia };
            foreach (int id in wanted)
            {
                IList<ClientCard> picked = PickFirst(cards, id);
                if (picked != null)
                    return picked;
            }
            return null;
        }

        /// <summary>候选是不是全在额外牌组（用来认出「落胤与圣女」那道"从额外牌组送去墓地"的费用提问）。</summary>
        private static bool IsExtraDeckCards(IList<ClientCard> cards)
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

        /// <summary>候选是不是"我们怪兽区上的超量怪"（用来认出上叠召唤那道"拿哪只当底"的提问）。</summary>
        private static bool IsOwnXyzMonsters(IList<ClientCard> cards)
        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card.Controller != 0 || card.Location != CardLocation.MonsterZone
                    || !card.HasType(CardType.Xyz))
                    return false;
            }
            return true;
        }

        /// <summary>
        /// 上叠召唤"拿哪只当底"：挑**打点最低**的那只（最不心疼），与 Release 那条同一套口径。
        /// 大姐写着不能当素材（内核的候选里本来就没有她），这里再兜一层，免得判据两头漂移。
        /// </summary>
        private static IList<ClientCard> PickOverlayBase(IList<ClientCard> cards)
        {
            ClientCard chosen = null;
            int lowest = int.MaxValue;
            foreach (ClientCard card in cards)
            {
                if (card.Controller != 0 || card.Location != CardLocation.MonsterZone)
                    continue;
                if (card.IsOriginalCode(CardId.Ichigekan))
                    continue;
                int attack = System.Math.Max(card.Attack, 0);
                if (attack < lowest)
                {
                    lowest = attack;
                    chosen = card;
                }
            }
            if (chosen == null)
                return null;
            IList<ClientCard> selected = new List<ClientCard>();
            selected.Add(chosen);
            return selected;
        }

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑）。</summary>
        private bool AlwaysPlay()
        {
            // 这两张的 ② 都会给出两个分支，而基类的 `OnSelectOption` 是**随机**挑一个：
            // · 群星（墓地发动）：●双方抽1（脚本 k=3）●我方抽1（脚本 k=4，场上有本家魔法使超量时）
            //   ——随机到前者就是白送对面一张卡（群友反馈："免费帮别人抽卡"）；
            // · 未眠之城：●把 1 张手牌洗回牌组再抽 1（脚本 k=1）●我方抽 1（脚本 k=2）
            //   ——后者不用先亏一张手牌，取后者。
            // 这里只**登记**要选的**值**（`卡号*16+k`），真正回给内核在 OnSelectOption
            // （值取自脚本 c<卡号>.lua 的 `aux.SelectFromOptions`，行号见下面各条）。
            if (Card == null || !Card.IsOriginalCode(CardId.Nemuri))
                _nemuriReturn = false;      // 别让这个待办粘到别的选项提问上
            if (Card != null)
            {
                if (Card.IsOriginalCode(CardId.Seiza) && Card.Location == CardLocation.Grave)
                {
                    // 脚本 c46983930 第 68~70 行：k=3「双方抽卡」／k=4「自己抽卡」→ 认 k=4
                    _optionCard = CardId.Seiza;
                    _optionValue = Util.GetStringId(CardId.Seiza, 4);
                }
                else if (Card.IsOriginalCode(CardId.Nemuri) && Card.Location == CardLocation.SpellZone)
                {
                    // 脚本 c72978038 第 57~59 行：k=1「回卡组并抽卡」／k=2「抽卡」→ 认 k=2
                    _optionCard = CardId.Nemuri;
                    _optionValue = Util.GetStringId(CardId.Nemuri, 2);
                    // 万一本回合选了"洗回手牌再抽"那条（k=1；只有它合法时才会走到），后面还会问
                    // "放牌组顶还是底"——操作方的 spec 要求放**最下面**（脚本第 81 行：
                    // `SelectOption(...)==0` 是 k=4＝顶、否则是 k=5＝底），这样群星放在顶部的那张
                    // 不会被自己盖回去。这个待办只认"放顶/放底"那道题，并且**被消费或超时
                    // （下一次别的卡的提问）时清掉**——见 OnSelectOption ③。
                    _nemuriReturn = true;
                }
                // 群星在场上的 ① 是从牌组挑 1 张本家放到牌组顶（唯一能"挑"的伪检索），
                // 挑哪张交给我们自己——见 PickSeizaPlace
                else if (Card.IsOriginalCode(CardId.Seiza) && Card.Location == CardLocation.SpellZone)
                    _seizaPlace = true;
            }
            return true;
        }
    }
}
