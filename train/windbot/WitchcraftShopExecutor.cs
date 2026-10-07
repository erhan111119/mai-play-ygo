using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 「魔女术」——按 **202605 构筑（商标女巫版，群友投稿 #93）** 写的执行器。
    ///
    /// **为什么不继承 WindBot 自带的 `WitchcraftExecutor`**（试过，更差）：那个 2815 行的执行器是
    /// 为另一副构筑（贪欲壶/雷击/PSY 那套）写的，除了规则表按它自己的卡号写死，还重写了
    /// `OnSelectCard` / `OnSelectPosition` / `OnChainEnd`——这些重写在**我们这副卡表**上会返回
    /// "不选"，把整局动作吞掉。实测同一副牌、同一批牌序：
    ///
    /// | 出牌脚本 | 动作/局 | 特召/局 |
    /// |---|---|---|
    /// | 继承基类（旧写法） | 4.3 | 0.5 |
    /// | 直接用通用脚本 `Test` | 9.5 | 2.0 |
    ///
    /// 所以这里和「升辉月」一样，基类用 `DoEverythingExecutor`（"看着场面做点合理的事"的通用打法），
    /// 把**这副牌特有的引擎判断**压在它前面，并在最后补回两条带护栏的通用规则。
    ///
    /// 引擎（按 202605 教程）：本家下级大多有"解放自己＋丢 1 张魔法 → 从卡组特召另一只本家"的 ①，
    /// 以及"把墓地的这张卡除外"的 ②；真正赚卡靠**融合**——速攻魔法「魔女工坊的慶典」能把自己
    /// 墓地·除外的魔法师族当融合素材；「魔女术代理师傅」（阻坑＋回收）、「魔女术学童组合」（每回合
    /// 检索本家魔法）、「大魔女 桑德里永」（融合出场时从卡组拉最多 3 只本家）是三个赚卡点。
    /// </summary>
    [Deck("WitchcraftShop", "AI_Witchcraft")]
    class WitchcraftShopExecutor : DoEverythingExecutor
    {
        public new class CardId
        {
            public const int ViceMadame = 6071005;        // 魔女术工匠·商标女巫
            public const int BystreetNight = 32353566;    // 魔女的圣夜行（场地）
            public const int Celebration = 6958567;       // 魔女工坊的慶典（速攻融合）
            public const int Terracotta = 70686400;       // 魔女工坊 陶俑魔像
            public const int Pupils = 69964858;           // 魔女术学童组合（融合）
            public const int Tears = 73664385;            // 结晶魔术 光之泪
            public const int Fara = 82344137;             // 初始之神 法拉
            public const int Sandrion = 33475154;         // 大魔女 桑德里永（融合）
            public const int Patronus = 9603252;          // 魔女术代理师傅（融合）
            public const int DarkMagicDestroyer = 59400890; // 毁灭之黑魔术师（融合／可除外自家 6★ 暗魔法师族替代召唤）
            // 蒂迈欧线（＝「毁灭之黑魔术师」→「超魔导龙骑士」）的两张卡，见 :meth:`DarkMagicDestroyerSummon`
            // 与那道收窄后的禁喂名单 :meth:`IsTerminalOnBoard`：蒂迈欧之眼光 ① 以自己场上·墓地的「黑魔术师」为对象
            //（「毁灭之黑魔术师」① 在场上·墓地当作「黑魔术师」，脚本 `c59400890.lua:33`）→ 只用它一只当素材融合出
            // 「超魔导龙骑士」（脚本 `c22283204.lua:18~26` 的 filter 只认 46986414/38033121，
            // 卡表里那张 22283204 的 ① 检索是由 毁灭之黑魔术师 ② 的 `aux.IsCodeOrListed(c,46986414)` 带进来的）。
            public const int TimaeusEye = 22283204;         // 蒂迈欧之眼光（把场上的「黑魔术师」送回卡组 → 融合出超魔导）
            public const int Dragun = 37818795;             // 超魔导龙骑士-真红眼龙骑士（蒂迈欧线做出来的终端；卡表里是这个印刷号）
            public const int Draping = 69748261;          // 魔女术的歪曲（反击陷阱）
            public const int Tire = 83301414;             // 魔女术的怠工（结束阶段回手，当 cost）
            public const int GlassServant = 22623509;     // 恩底弥翁的侍女 玻璃
            public const int GenniServant = 7656689;      // 恩底弥翁的侍女 杰妮
            public const int MagicalizeFusion = 38943357; // 魔力统辖
            public const int SuperPolymerization = 48130397; // 超融合（基准号）
            // ⚠ 卡表用的是**另一印刷号 48130398**（alias＝48130397）；**两个印刷号都认**：
            //   常量、AddExecutor 与 IsFusionMaterialRequest() 都同时登记两个号
            //   （只登记基准号时，卡表里那张匹配不上、专属规则整条落空）。
            public const int SuperPolymerizationAlt = 48130398; // 超融合（卡表里用的是这个印刷号）
            public const int Creation = 57916305;         // 魔女术的创造
            public const int Veil = 21522601;             // 魔女术师傅·玻璃女巫（基准号）
            // ⚠ 卡表用的是**另一印刷号 21522602**（alias＝21522601）；**两个印刷号都认**：
            //   常量、AddExecutor，以及下面各名单/判据（KeepOnBoard、NeverFeedFromBoard、
            //   CraftsmanChainBaseOrder/CraftsmanChainOrderNow、PreferredSummons、InterruptionCards、
            //   DeckSendPriority、HasInHand/IsDeckFieldSearchEffect、送墓与融合素材保护）都同时登记两个号。
            public const int VeilAlt = 21522602;          // 魔女术师傅·玻璃女巫（卡表里用的是这个印刷号）
            public const int Bystreet = 83289866;         // 魔女术的小巷
            public const int Schmietta = 21744288;        // 魔女术工匠·锻造女巫
            public const int Pittore = 95245544;          // 魔女术工匠·绘画女巫
            public const int Potterie = 59851535;         // 魔女术工匠·陶器女巫
            public const int Haine = 84523092;            // 魔女术工匠·服装女巫
            public const int Genni = 64756282;            // 魔女术工匠·万能杰妮
            public const int Sheep = 50277355;            // 交织绵羊（连接 2）
            public const int Unveiling = 70226289;        // 魔女术的演示
        }

        public WitchcraftShopExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            // ① 先摘掉基类那两条笼统规则（Activate / SummonOrSet），换成带护栏的版本（见 ⑧）。
            //    基类的 SummonOrSet 会把**手坑**也通召上去；Activate 则是"能发就发"没有护栏。
            //    ⚠ 必须先摘再加自己的规则：IList 没有 RemoveAll，按类型倒着删会连刚加的一起删。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                CardExecutor exec = Executors[i];
                if (exec.CardId != -1)
                    continue;
                // 也摘掉基类那两条"无卡号"的 SpSummon / SpellSet：**顺序就是优先级**，
                // 它们在列表里排在本类所有规则前面（基类构造先跑），`exec.Func == null` 直接通过 →
                // 于是每次主要阶段"先盖一张魔法 / 先做额外召唤"，把本家引擎整条顶掉。
                // 实测（真人局 `logs/app_20261006_005636.log.jsonl` 01:36）：
                // 手牌 {服装女巫, 陶器女巫, 锁鸟, 蒂迈欧之眼光, 玻璃}，它先把**唯一的魔法**
                // 「蒂迈欧之眼光」盖下去，再通召「陶器女巫」——而工匠链（锻造/万能杰妮/陶器/绘画）① 的
                // 发动条件是"**解放自己 + 从手卡丢弃 1 张魔法卡**"（卡文 `59851535`），手里已经没有魔法可丢
                // → ① 发不出来、整个回合就剩"盖一张 + 上一个怪"。
                if (exec.Type == ExecutorType.SummonOrSet || exec.Type == ExecutorType.Activate
                    || exec.Type == ExecutorType.SpSummon || exec.Type == ExecutorType.SpellSet)
                    Executors.RemoveAt(i);
            }

            // ② 引擎：场地「魔女的圣夜行」（② 把"为本家效果丢手牌"改成"从卡组堆本家魔陷"，是省钱关键）
            AddExecutor(ExecutorType.Activate, CardId.BystreetNight, AlwaysPlay);
            // ③「魔女工坊的慶典」：① 的两个分支取"融合"（脚本 c6958567：1＝炸 1 换 1，2＝墓地融合）
            AddExecutor(ExecutorType.Activate, CardId.Celebration, Celebration);
            // ④「魔女术学童组合」：① 取"检索本家魔法"（脚本 c69964858：1＝检索，2＝展示手牌魔法并适用）
            AddExecutor(ExecutorType.Activate, CardId.Pupils, Pupils);
            // ⑤「结晶魔术 光之泪」：自己回合堆墓做素材，对手回合从卡组特召当阻抗（脚本 c73664385：1/2）
            AddExecutor(ExecutorType.Activate, CardId.Tears, Tears);
            // ⑥ 剩下几张"能发就发"：陶俑魔像（① 手牌自跳＋回收、② 手牌场上融合）、法拉、超融合、魔力统辖、
            //    恩底弥翁的侍女们（② 特召时检索本家魔陷）、商标女巫（① 解放自己检索场地/永续）、歪曲（反击）
            AddExecutor(ExecutorType.Activate, CardId.Terracotta, Terracotta);
            AddExecutor(ExecutorType.Activate, CardId.Fara, AlwaysPlay);
            // 「超融合」照旧"能发就发"：它确实会吃掉场上的本家怪，但也给这副牌补了一个融合身体
            //（对打实测撤不撤都在噪声里，而**卡通关那次是明显有害**）——2026-10-04 试过闸门后回滚：
            // 与另外四副的 6 局对打在 4 组里有 3 组下降，虽然样本小，但没有理由留着。
            AddExecutor(ExecutorType.Activate, CardId.SuperPolymerization, AlwaysPlay);
            // ⚠ 卡表里的「超融合」是另一印刷号 48130398：**两个印刷号都认**，缺了这条专属规则会落空。
            AddExecutor(ExecutorType.Activate, CardId.SuperPolymerizationAlt, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.MagicalizeFusion, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.GlassServant, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.GenniServant, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.ViceMadame, ViceMadame);
            AddExecutor(ExecutorType.Activate, CardId.Draping, AlwaysPlay);
            AddExecutor(ExecutorType.SpellSet, CardId.Draping, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Creation, AlwaysPlay);

            // ⑧ **本家下级的效果全部"能发就发"**——这是这副牌特召的主要来源：
            //    下级的 ① 是「把手卡·场上的这张卡解放＋丢 1 张魔法 → 从卡组特召另一只本家」，
            //    ② 多半是「把墓地的这张卡除外」的追加减益/回收。只靠通用兜底时它们经常不动，
            //    实测特召只有 1.9 次/局；这里逐张点名，让引擎进入"要不要发动 → 选代价"的流程。
            AddExecutor(ExecutorType.Activate, CardId.Schmietta, AlwaysPlay);   // 锻造女巫
            AddExecutor(ExecutorType.Activate, CardId.Pittore, AlwaysPlay);     // 绘画女巫
            AddExecutor(ExecutorType.Activate, CardId.Potterie, AlwaysPlay);    // 陶器女巫
            AddExecutor(ExecutorType.Activate, CardId.Haine, AlwaysPlay);       // 服装女巫
            // 「万能杰妮」①（场上）照旧能发就发；**从墓地发动的是 ②（复制本家魔法）**，那条要顺手登记
            // 选项值（复制到的是慶典就取融合支），见 :meth:`Genni`。
            AddExecutor(ExecutorType.Activate, CardId.Genni, Genni);            // 万能杰妮
            AddExecutor(ExecutorType.Activate, CardId.Veil, AlwaysPlay);        // 师傅·玻璃女巫
            // ⚠ 卡表里的「魔女术师傅·玻璃女巫」是另一印刷号 21522602：**两个印刷号都认**，
            //   缺了这条她就没有专属规则（会掉回通用兜底）。
            AddExecutor(ExecutorType.Activate, CardId.VeilAlt, AlwaysPlay);     // 师傅·玻璃女巫（卡表里的印刷号）
            // ⑨ 融合/连接怪的赚卡与展开：桑德里永融合出场能从手牌·卡组拉最多 3 只 7 星以下本家，
            //    代理师傅 ① 会在魔法师族/魔法效果发动时三选一（炸卡／从卡组特召／回收墓地本家魔陷）。
            //    「交织绵羊」原来在这里是"该出就出"，但它**两次把终场件当连接素材吃掉**
            //    （代理师傅＋陶俑魔像、服装女巫＋侍女玻璃）——改成下面的条件放行。
            AddExecutor(ExecutorType.Activate, CardId.Sandrion, AlwaysPlay);
            AddExecutor(ExecutorType.Activate, CardId.Patronus, AlwaysPlay);
            AddExecutor(ExecutorType.SpSummon, CardId.Sheep, AllowOtherExtraSummon);
            // 「毁灭之黑魔术师」必须有一条专属规则：基类的兜底 SpSummon 是"这张卡没有专属规则时随便出"，
            // 而它的替代召唤（除外自己场上 1 只 6★ 以上暗属性魔法师族）会把刚融合出来的「代理师傅」吃掉
            // （实测 30 局里 9 次，终场直接没了）。
            // ⚠ 2026-10-06 更正：**"有专属规则之后基类兜底对它自动失效"是错的**——本类构造函数末尾自己加的
            // 那条 `SpSummon(-1, AllowAnyExtraSummon)` 对任何卡都成立，这条专属规则返回 false 也照样出
            // （`temp/train/wc-fix14-30.log` 30 局里出场 32 次，其中 17 次除外的代价是「服装女巫」、
            // 1 次是刚出场的「代理师傅」）。真正让它生效的是 :meth:`AllowAnyExtraSummon` 改成
            // "有专属规则的卡一律让路"（见那里的实测）。
            AddExecutor(ExecutorType.SpSummon, CardId.DarkMagicDestroyer, DarkMagicDestroyerSummon);

            // ⑦ 通召优先给"堆墓手"：展开指南 STEP 3~4 是"通召本家怪兽 → 用它的效果把本家魔法堆进墓地"，
            //    锻造女巫/绘画女巫 就是这两个堆墓点（② 从卡组堆墓、① 还能接别的本家），
            //    所以它们排在通用兜底之前——能通召它们时别把通召点花在别的怪上。
            //    ⚠ 第 3 轮实测：这么排外部腿 8/20 → **3/20**（镜像质量反而更高）→ 已回退，规则先不启用。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, SummonOrSet));
            Executors.Add(new CardExecutor(ExecutorType.Activate, -1, Activate));
            Executors.Add(new CardExecutor(ExecutorType.SpSummon, -1, AllowAnyExtraSummon));
            Executors.Add(new CardExecutor(ExecutorType.Repos, -1, ReposForLethal));
            // 盖放**放在最后**：能发的先发。这条对这副牌尤其重要——工匠链① 要"从手卡丢弃 1 张魔法卡"，
            // 先把魔法盖下去等于自断（见构造函数① 里的实测）。
            Executors.Add(new CardExecutor(ExecutorType.SpellSet, -1, SpellSet));

            // 斩杀优先：插到最前面（内核的动作循环是"外层遍历规则"，排前面才会先被问到）。
            Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, LethalAvailable));

            // 计划层的额外召唤白名单：走线时把通用连接怪（塞勒涅/访问码语者/四花缭乱/小夜骑士/百变莱娜/
            // 变幻舞夜）先放一放——实测它们会把学徒/代理师傅这类终场件当连接素材吃掉，终场就少东西。
            // （「交织绵羊」也走同一道闸门，见 :meth:`AllowOtherExtraSummon`。）
            foreach (int cardId in OffPlanExtraMonsters)
                AddExecutor(ExecutorType.SpSummon, cardId, AllowOtherExtraSummon);
        }

        /// <summary>
        /// 终场/引擎里"不能被当素材吃掉"的怪（写在 :meth:`ExpendableDistinctNames` 用）。
        /// 依据是验收口径里那张合格线场（代理师傅 + 学童组合 + 服装女巫 + 玻璃女巫 + 歪曲）+ 这条链的中继。
        /// </summary>
        private static readonly int[] KeepOnBoard =
        {
            9603252,    // 魔女术代理师傅（终端）
            69964858,   // 魔女术学童组合（终端/检索中继）
            33475154,   // 大魔女 桑德里永（终端/展开核心）
            84523092,   // 魔女术工匠·服装女巫（终端/阻坑）
            21522601,   // 魔女术师傅·玻璃女巫（阻坑）
            21522602,   // 魔女术师傅·玻璃女巫（另一印刷）
            22623509,   // 恩底弥翁的侍女 玻璃（检索中继）
            7656689,    // 恩底弥翁的侍女 杰妮（特召中继）
            21744288,   // 魔女术工匠·锻造女巫（本家特召中继）
            64756282,   // 魔女术工匠·万能杰妮（本家特召中继）
            59851535,   // 魔女术工匠·陶器女巫（本家特召中继）
            95245544,   // 魔女术工匠·绘画女巫（本家特召中继）
            59400890,   // 毁灭之黑魔术师（超魔导的融合素材）
            37818795,   // 超魔导龙骑士-真红眼龙骑士（阻抗终端）
        };

        /// <summary>这张卡是不是"要留在场上"的（空值算留，免得被当成素材）。</summary>
        private static bool IsKeepOnBoard(ClientCard card)
        {
            if (card == null)
                return true;
            foreach (int cardId in KeepOnBoard)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 场上"不心疼的怪"里有几个**名字不同的**——连接怪至少要两只不同名的怪，所以用名字数当门槛。
        /// 排除 :data:`KeepOnBoard` 里的终场件与中继（挨个点名拦是打地鼠：拦了交织绵羊，S：P小夜骑士照样吃）。
        /// </summary>
        private int ExpendableDistinctNames()
        {
            HashSet<string> names = new HashSet<string>();
            foreach (ClientCard card in Bot.MonsterZone.GetMonsters())
            {
                if (IsKeepOnBoard(card))
                    continue;
                names.Add(card.Name);
            }
            return names.Count;
        }

        /// <summary>
        /// 「交织绵羊」以及其它通用额外怪（塞勒涅/访问码语者/S：P小夜骑士…）能不能做。
        /// **两条都过才做**：① 不在走线中；② 场上真有 2 只不同名的不心疼怪。
        /// ② 是关键——连接素材由基类挑、我们改不了它挑谁，实测 交织绵羊 吃过 代理师傅＋陶俑魔像、
        /// 服装女巫＋侍女玻璃，S：P小夜骑士 也吃过 代理师傅＋学童组合（终场直接空掉）。
        ///
        /// ⚠ 这条闸门 2026-10-05 加进来之后**一直没真正生效**：通用兜底 :meth:`AllowAnyExtraSummon`
        /// 排在它前面、且当时对任何卡都放行（实测 30 局里这些额外怪出场 36 次，其中 14 次是在
        /// "计划进行中"——**光这一条就该直接拦住**；「交织绵羊」出场 11 次里有 7 次的连接素材
        /// 就是刚做出来的「代理师傅」，第 26 局还把「代理师傅」＋「学童组合」一起送进墓地）。
        /// 2026-10-06 把兜底改成"有专属规则的卡一律让路"之后，这条闸门才开始起作用。
        /// </summary>
        private bool AllowOtherExtraSummon()
        {
            if (PlanActive())
                return false;
            return ExpendableDistinctNames() >= 2;
        }

        /// <summary>
        /// 「毁灭之黑魔术师」的替代召唤（魔法卡效果发动的回合，除外自己场上 1 只 6★ 以上的暗属性魔法师族）
        /// 能不能做。**只有场上有"可以牺牲的"那只时才做**——实测它把刚用「慶典」融合出来的「代理师傅」
        /// 除外了（30 局 9 次，等于那一步白做）；教程在这两处的指定代价件都是 6★ 暗属性的「商标女巫」。
        ///
        /// ⚠⚠ 2026-10-06 第七轮修复：**这条闸门还顺手把「蒂迈欧之眼光」那条线一起掐了**（＝超魔导的回归点）。
        /// 两份同种子 30 局日志（`temp/train/`）按"这次替代召唤除外的代价件是谁"逐次归类：
        ///
        /// | 代价件 | wc-fix14-30.log（原顺序：蒂迈欧 **24** 局、超魔导 **24** 局） | wc-fix17-30.log（RA：蒂迈欧 **7**、超魔导 **7**） |
        /// |---|---|---|
        /// | 魔女术工匠·服装女巫 | **17 次**（其中 **14 次**紧接着 ② 发动） | **0 次** ❌ |
        /// | 魔女术工匠·商标女巫 | 13 次（8 次 ②） | 11 次（6 次 ②） |
        /// | 初始之神 法拉 | 1 次（0 次 ②） | 4 次（2 次 ②） |
        /// | 魔女术代理师傅 | 1 次（1 次 ②） | 0 次 |
        ///
        /// 算总账：fix14 里 `毁灭之黑魔术师 activate effect`（②＝检索）是 **21** 次，
        /// **减掉"服装女巫当代价"那 14 次，正好是 RA 的 7 次**——也就是说这条线的断点就是这一步：
        /// 「服装女巫」在 :data:`KeepOnBoard` 里 → 本方法判 false → 那次替代召唤不发生 →
        /// `毁灭之黑魔术师` 起不来 → 它 ② 的检索（脚本 `c59400890.lua:87` 的 thfilter 只认「黑魔术师」
        /// 或"有那个卡名记述的卡"，这副牌里唯一的目标就是「蒂迈欧之眼光」）自然无从发动，
        /// 后面 `蒂迈欧之眼光 发动 → 超魔导龙骑士 出场` 整条归零。
        /// fix14 那版之所以还有这 17 次，是因为当时通用兜底 `AllowAnyExtraSummon` 对任何卡都放行、
        /// 把这条闸门整个顶掉（见那里的实测）；改成"有专属规则的卡一律让路"之后本方法才真正生效，
        /// 于是**回归就藏在这里**（`wc-fix17-30.log` 30 局里替代召唤只剩 15 次、其中服装女巫 0 次）。
        ///
        /// ✅ 2026-10-06 第八轮修法（本轮）：把"不许喂"的名单**收窄成只禁终端件**，闸门保留。
        /// 判据换成 :meth:`IsTerminalOnBoard`（＝ :data:`NeverFeedFromBoard` 里已被点名的终场件与超魔导线
        /// ＋ :data:`KeepOnBoard` 里标着"终端/展开核心"的「大魔女 桑德里永」），**「服装女巫」显式摘出**：
        /// * 「代理师傅」「学童组合」「大魔女」「玻璃女巫（两个印刷号）」照旧一律不许吃
        ///   （fix14 里唯一一次吃「代理师傅」的那次实测就是白做，见 changelog 第五轮）；
        /// * :data:`NeverFeedFromBoard` 已点名的超魔导线两张同理不许吃——「超魔导龙骑士」是终端，
        ///   「毁灭之黑魔术师」是蒂迈欧之眼光的融合素材（被吃掉这条线当场归零）；
        /// * 「服装女巫」**回到可喂**：7★ 暗·魔法师族，ATK 2400，站场只是打点阻坑，
        ///   而上表 fix14 那 17 次（其中 14 次紧接着 ② 检索）正是这条线真正的燃料。
        ///
        /// **为什么"收窄名单"等价于 0.21.88 的行为**：0.21.88 那版里通用兜底 :meth:`AllowAnyExtraSummon`
        /// 是无条件 true、把本闸门**整个顶掉** = "任何" 6★+ 暗·魔法师族都能喂，其中就包括「服装女巫」
        /// （17 次）；本轮的名单只把**终端件**挡在外面，而「服装女巫」不在终端件里 → 对她的放行结果与
        /// 0.21.88 那版一致，同时把 0.21.88 丢掉的那道护栏补回来（fix14 里那 1 次吃「代理师傅」、
        /// 以及随时可能发生的吃「毁灭之黑魔术师」都不再发生）。
        /// ⚠ 与 RB 那次的区别：RB 走的是"线还活着才放宽"的**第二圈**（已删除，见下方注记），
        /// 放宽口径被 `TimaeusLineAlive()` 卡着；本轮不再有第二个开关——服装女巫能不能喂**只看这份名单**，
        /// 所以 A/B 要验证的只有"收窄名单"这一件事。
        ///
        /// **A/B 判据（30 局同种子口径）**：「服装女巫」被喂（`服装女巫 from MonsterZone move to Grave`）
        /// **≥12 次**、`超魔导龙骑士`出场 **≥20**；对照 RA（按 :data:`KeepOnBoard` 拦、服装女巫 0 次）
        /// 应是"服装女巫 0 → ≥12、超魔导 7 → ≥20"的单向变化。若超魔导不升反降，说明断点不在这份名单里，
        /// 要去接 CHANGELOG 0.21.95 那条待办：在 `OnSelectCard` 的 `HintMsg.Remove` 分支单独排"代价选谁"。
        /// </summary>
        private bool DarkMagicDestroyerSummon()
        {
            // 唯一的否决条件＝代价件是**终端件**（:meth:`IsTerminalOnBoard`）。
            // ⚠ 这里**不再**拿整张 :data:`KeepOnBoard` 当名单：那张名单连"中继"一起保护，
            //   会把这条线唯一还喂得起的代价件「服装女巫」一并拦掉——她就是第七轮定位到的回归点。
            foreach (ClientCard card in Bot.MonsterZone.GetMonsters())
            {
                if (card.Level >= 6 && card.HasRace(CardRace.SpellCaster)
                    && card.HasAttribute(CardAttribute.Dark) && !IsTerminalOnBoard(card))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 替代召唤的代价**唯一还禁喂的**一类：真正的终端件（2026-10-06 第八轮收窄）。
        /// **优先复用现成名单**，只做两处增删：
        /// * 复用 :data:`NeverFeedFromBoard`——它本来就是"已被点名的终场件＋超魔导线"
        ///   （代理师傅 / 学童组合 / 玻璃女巫两个印刷号 / 超魔导龙骑士 / 毁灭之黑魔术师）；
        /// * **摘出「服装女巫」**：她在 :data:`NeverFeedFromBoard` 里是为了保护**融合素材/破坏代价**
        ///   那条链路（:meth:`FeedCostScore` 仍照原样用那张名单，一个成员都没动），
        ///   与替代召唤的代价（本方法的用途）是两条独立的链路；
        /// * **补上「大魔女 桑德里永」**：:data:`NeverFeedFromBoard` 没点名她（那张名单只收
        ///   "合格线件 + 超魔导线"），但 :data:`KeepOnBoard` 里她是"终端/展开核心"，必须一起留下。
        /// 名单之外一律可喂；「服装女巫」（7★ 暗·魔法师族）正是这条线要的代价类型。
        /// （空值算"不许喂"，与 :meth:`IsKeepOnBoard` 同一个口径。）
        /// </summary>
        private static bool IsTerminalOnBoard(ClientCard card)
        {
            if (card == null)
                return true;
            // 「服装女巫」显式放回可喂：她是 :data:`NeverFeedFromBoard` 的成员，这里先摘出来。
            if (IsThatCard(card, CardId.Haine))
                return false;
            if (IsAny(card, NeverFeedFromBoard))
                return true;
            // 「大魔女 桑德里永」：:data:`KeepOnBoard` 里标着"终端/展开核心"，但上面那张名单没点名她。
            return IsThatCard(card, CardId.Sandrion);
        }

        // ⚠ 2026-10-06 第八轮：RB 留下的那两个"额外放宽"开关（`AllowFeedHaineForTimaeus` 常量与
        // `TimaeusLineAlive()` 方法：只在"线还活着"时特别放行喂「服装女巫」）**已删除**——它与本轮
        // "把禁喂名单收窄"是**两套互相叠加的放宽**，留着的话 A/B 分不清是谁在生效；现在"服装女巫能不能喂"
        // 只有 :meth:`IsTerminalOnBoard` 一个判据。RB 那一轮的数据留档于此供复查：30 局事件数
        // 蒂迈欧之眼光 **17** / 超魔导龙骑士 **0**（比 RA 的 7 / 7 更差，当轮已回退、未部署；
        // CHANGELOG 0.21.95）。它连带删掉的三条判据（"超魔导龙骑士不在场"、"蒂迈欧之眼光还在手牌·魔陷区"、
        // "蒂迈欧之眼光还在卡组"）就是"线还活着"的三个可见处，以后要再接别的放宽从这里长。

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>
        /// 「魔女术工匠·万能杰妮」：①（场上：解放自己＋丢 1 张魔法 → 从卡组特召另一只本家）照旧放行；
        /// **②（墓地：除外自己＋1 张本家魔法 → 适用那张魔法发动时的效果）要顺手登记分支值**。
        ///
        /// 依据是教程第 6/11 步：**「大魔女 桑德里永」就是靠万能杰妮② 复制墓地的「慶典」融合出来的**
        /// （"除外商标女巫 → 特召毁灭之黑魔术师"之后那条："墓地万能杰妮② 复制墓地的慶典 → 把除外区的
        /// 商标女巫/万能杰妮 + 墓地的玻璃女巫 当素材 → 融合 大魔女"）。而 慶典 的选项编码里带的是
        /// **被复制那张卡（慶典）的卡号**（探针实测 `正在结算=魔女术工匠·万能杰妮 options=[111337074,111337075]
        /// 登记值=0`），不登记就只能落到基类 `Rand.Next`——一半的局会走成"炸我方 1 只本家 + 对面 1 张"，
        /// 实测 30 局里有 2 局把场上的「服装女巫」炸掉换对面的魂虎。编码依据见 :meth:`EncodedOption`。
        ///
        /// ⚠ 只在**自己回合**登记：教程的对手回合用法是「学童组合 展示手卡的慶典 → 复制它"破坏对方卡片"
        /// 的效果」（18 步线末尾的对方回合序列），那条要走分支①；而 万能杰妮② 是起动效果、只可能在自己
        /// 回合发动，所以这条闸门不会挡住教程第 11 步。
        /// </summary>
        private bool Genni()
        {
            if (Card != null && Card.Location == CardLocation.Grave && Duel.Player == 0)
                _optionValue = EncodedOption(CardId.Celebration, 2);   // 脚本值 2＝墓地融合
            return true;
        }

        /// <summary>
        /// 「魔女术工匠·商标女巫」①（解放自己 → 从卡组把 1 张「魔女术」场地·永续魔法加入手卡）。
        /// **"唯一的身体"不该拿去做"多余的检索"**：实测（对空白第 6 局，全魔法起手）手里已经有
        /// 「魔女的圣夜行」了，她还是把自己解放掉再搜一张，最后场上一个怪都没有——而这副牌
        /// "场上有魔法师族"是 杰妮①/玻璃① 能自跳、也是融合素材的前提。
        /// 只有两种情形才用：**① 场地还没到手**（这一步是教程 STEP 2 的起步）；
        /// **② 场上还有别的怪**（不差她这一只）。在手牌里发动不占身体，照旧放行。
        /// </summary>
        private bool ViceMadame()
        {
            if (Card != null && Card.Location == CardLocation.MonsterZone)
            {
                bool fieldMissing = !Bot.HasInHand(CardId.BystreetNight) && !Bot.HasInSpellZone(CardId.BystreetNight);
                if (!fieldMissing && Bot.MonsterZone.GetMonsters().Count <= 1)
                    return false;
            }
            return true;
        }

        /// <summary>「魔女工坊的慶典」：分支值 2＝融合，取它。**但分支② 不成立时干脆别开这张卡**——
        /// 内核那时只给分支①（炸我方 1 只本家 ＋ 炸对面 1 张），实测把自己刚融合出来的「学童组合」
        /// 炸掉去换对面的魂虎，回合结束场面全空（对空白 30 局里的第 8/9 局就是这条）。
        /// 两个放行条件：**① 墓地/除外的魔法师族够融合**（≥2 只、含 1 只本家）；**② 场上有"不心疼的"怪**
        /// 能给分支① 吃。两条都不满足时才不开（留着当下回合的手牌 cost 也比白送掉终端强）。
        /// </summary>
        private bool Celebration()
        {
            if (_verbose)
            {
                string graveInfo = "";
                foreach (ClientCard card in AllGraveAndBanished())
                {
                    if (card == null || !card.IsMonster())
                        continue;
                    graveInfo += (graveInfo.Length == 0 ? "" : "、") + card.Name
                        + (card.HasRace(CardRace.SpellCaster) ? "(魔)" : "(非魔)");
                }
                Logger.WriteLine("[探针] 慶典判定：墓地/除外怪兽=" + (graveInfo.Length == 0 ? "（空）" : graveInfo)
                    + "｜素材齐=" + FusionMaterialsReady() + "｜场上不心疼=" + ExpendableDistinctNames());
            }
            if (!FusionMaterialsReady() && ExpendableDistinctNames() < 1)
                return false;
            _optionValue = EncodedOption(CardId.Celebration, 2);   // 脚本值 2＝融合
            return true;
        }

        /// <summary>
        /// 内核给"分支型效果"的选项值编码：**`卡号 * 16 + 描述下标`**（探针实测：
        /// 慶典 脚本值 1（炸卡）→ `6958567*16+2 = 111337074`、脚本值 2（融合）→ `+3 = 111337075`；
        /// 「结晶魔术 光之泪」脚本值 0 → `73664385*16+1`）。
        /// **原来这里直接写 `_optionValue = 1/2`，跟内核给的值对不上**——于是每次都落到基类
        /// `DoEveryThingExecutor.OnSelectOption` 的 `Rand.Next`，等于"分支全靠掷骰子"：
        /// 慶典 一半的局被掷成"炸自己一只本家"，把自己刚做出来的终端炸掉（这就是"打得菜"的直接来源）。
        /// 好在编码里带着卡号，所以登记值可以一直留到真正命中，不怕被别的卡的选项问掉。
        ///
        /// ⚠ **描述下标不等于脚本值**：内核发过来的是脚本里 `aux.Stringid(卡号, k)` 的那个 k
        /// （＝选项文案的下标），而各脚本按自己的习惯编号——
        /// * 慶典 / 学童组合：`aux.SelectFromOptions(..., aux.Stringid(id, 脚本值+1), 脚本值)` → 用本方法（+1）；
        /// * 结晶魔术 光之泪：`aux.Stringid(id, 1)` / `aux.Stringid(id, 2)` → **下标就是脚本值**，
        ///   要用 :meth:`EncodedStringId`（不然差一位，实测 30 局里登记的 1178630162/1178630163
        ///   从来没命中过内核给的 1178630161）。
        /// </summary>
        private static int EncodedOption(int cardId, int scriptValue)
        {
            return cardId * 16 + scriptValue + 1;
        }

        /// <summary>
        /// 直接按"描述下标"登记（＝脚本里 `aux.Stringid(卡号, index)` 的 index）。
        /// 给那些下标与脚本值一致的脚本用（如「结晶魔术 光之泪」：1＝从卡组送墓、2＝特召）。
        /// </summary>
        private static int EncodedStringId(int cardId, int stringIndex)
        {
            return cardId * 16 + stringIndex;
        }

        /// <summary>
        /// 「慶典」分支②（墓地·除外的魔法师族回牌组融合）的素材条件：墓地+除外里的魔法师族 ≥2 只，
        /// 且其中至少 1 只是「魔女术」怪兽——融合怪（学童组合/代理师傅）要的是「魔女术」＋魔法师族。
        /// 「恩底弥翁的侍女 玻璃/杰妮」卡名里没有"魔女术"，但规则上当作「魔女术」卡，所以按卡号一起认
        /// （漏掉它们会把能融合的场面判成不能融合 → 白白不开慶典）。
        /// </summary>
        private bool FusionMaterialsReady()
        {
            int spellcasters = 0;
            bool hasArchetype = false;
            foreach (ClientCard card in AllGraveAndBanished())
            {
                if (card == null || !card.HasRace(CardRace.SpellCaster))
                    continue;
                ++spellcasters;
                if (card.Name.Contains("魔女术") || card.IsCode(CardId.GlassServant) || card.IsCode(CardId.GenniServant))
                    hasArchetype = true;
            }
            return spellcasters >= 2 && hasArchetype;
        }

        /// <summary>我方墓地 + 除外区的卡（「慶典」分支② 的素材来源就是这两处）。</summary>
        private IEnumerable<ClientCard> AllGraveAndBanished()
        {
            foreach (ClientCard card in Bot.Graveyard)
                yield return card;
            foreach (ClientCard card in Bot.Banished)
                yield return card;
        }

        /// <summary>
        /// 「魔女术学童组合」①：自己回合取"检索本家魔法"（脚本值 1）；**对手回合取"展示手牌魔法并适用其效果"**
        /// （脚本值 2）——展开指南里对方回合要靠它复制「演示」做干扰、复制「小巷」给代破。
        /// </summary>
        private bool Pupils()
        {
            _optionValue = EncodedOption(69964858, Duel.Player == 0 ? 1 : 2);
            return true;
        }

        /// <summary>
        /// 「结晶魔术 光之泪」：脚本值 1＝从卡组把魔法师族/魔法送去墓地（做融合素材），
        /// 2＝对手发动效果时从手卡·卡组特召一只本家（当阻抗）。按**当前是谁的回合**取。
        /// ⚠ 它的选项文案是 `aux.Stringid(id,1)` / `aux.Stringid(id,2)`（＝**下标＝脚本值**），
        /// 所以要用 :meth:`EncodedStringId`——原来那版按 `EncodedOption(cardId, 1/2)` 登记，
        /// 内核给的是 `1178630161`、我们登记 `1178630162/1178630163`，**一次都没命中**
        /// （实测 30 局日志里 8 次探针全是这个差一）；单选项时基类恰好还是取那唯一的选项，
        /// 所以看不出代价，但"对手回合想特召挡一下"时就会掷骰子。
        /// </summary>
        private bool Tears()
        {
            _optionValue = EncodedStringId(73664385, Duel.Player == 0 ? 1 : 2);
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
        };

        /// <summary>下一次选项提问要选的**值**（0＝没有待办）。</summary>
        private int _optionValue;

        /// <summary>通召/盖放的统一入口：手坑一律否掉，其余交给基类判断。</summary>
        // ============================================================ 计划层（见插件 docs/plan-layer.md）

        /// <summary>
        /// 本回合走哪条起手线。魔女术的终端（代理师傅 + 服装女巫 + 反击陷阱「歪曲」）由几条**同一个方向**
        /// 的单卡线拼出来，起手件不同、步骤顺序略有差别：
        /// * 商标女巫线：她① 从手牌解放自己 → 检索「魔女的圣夜行」（场地）→ 场地② 依次堆墓 → 融合；
        /// * 玻璃女巫线：玻璃② 检索场地（或直接融合出代理师傅）；
        /// * 场地线：手上有场地就直接发动，场地② 先把「慶典」等送墓。
        /// 计划层的活是"这条线下一步缺哪一环"——检索/送墓/回收的顺序已经在各自的优先级数组里，
        /// 这里只补两件它们在单卡层面看不到的事：**通召只认起手件** 和 **别乱出通用连接怪**。
        /// </summary>
        private enum PlanLine
        {
            None,
            Madame,     // 手牌有「魔女术工匠·商标女巫」（① 手牌检索场地）
            Glass,      // 手牌有「恩底弥翁的侍女 玻璃」/「魔女术师傅·玻璃女巫」
            Field,      // 手牌有「魔女的圣夜行」（场地）
        }

        private PlanLine _planLine = PlanLine.None;

        /// <summary>这份计划是给第几回合定的（Duel.Turn）。</summary>
        private int _planTurn = -1;

        /// <summary>教程的三只融合终端，出场顺序：大魔女 → 学童组合 → 代理师傅。</summary>
        private static readonly int[] PlanFusionOrder =
        {
            33475154,   // 大魔女 桑德里永
            69964858,   // 魔女术学童组合
            9603252,    // 魔女术代理师傅
            // ⚠ **别把顺序换成"学童组合 → 代理师傅 → 大魔女"**（2026-10-05 试过并回滚）：
            // 试的那版读到 代理师傅 4/30、服装女巫 7/30；但**同代码重跑也在 4~7 区间浮动**
            // （基类 `Rand.Next` 的噪声在这个样本上约 ±3），所以"变差"其实判不出来。
            // 真正让人保留教程顺序的是**机制**：大魔女① 融合出场时会从卡组拉最多 3 只 7 星以下本家
            // ——「服装女巫」（合格线件）和后续身体主要就是这一步来的。
            // 当时"出代理师傅的那 8 局融合序列都是 学童组合→代理师傅、一次没经过大魔女"是**幸存者偏差**
            // （那 8 局是大魔女素材不齐、被跳过才轮到的）。要动这个顺序，按"每格 40 局 + 同 build 重跑"量。
        };

        /// <summary>
        /// 本回合已经融合召唤出过的终端（只记 :data:`PlanFusionOrder` 里那三只）。
        /// **为什么记"出过没有"而不是"出过几次"**：教程的 1→2→3 次是理想线（大魔女素材齐、每回合都能出）
        /// 才成立的；大魔女经常因为素材不齐不在候选里，序号一错位，第 2 次融合就被额外卡组里第二张
        /// 「学童组合」顶掉，「代理师傅」永远排不上——实测 6 局里代理师傅只出了 1 次，且当场被当连接素材吃掉。
        /// </summary>
        private readonly HashSet<int> _planFusionPicked = new HashSet<int>();

        /// <summary>是否打印调试信息（命令行 ``Debug=true``）。</summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>
        /// 这个回合**进过场的怪**（卡号）：给斩杀判据用——这个回合才上场的怪保守地先不算进"这回合能打多少"
        /// （有的怪写着"特殊召唤的回合不能攻击"，宁可晚一步也不要打不死时白进战阶）。
        /// 回合切换时清空（<see cref="OnNewTurn"/>）：对手回合里特召出来的怪，到我们回合就解禁了。
        /// </summary>
        private readonly HashSet<int> _summonedThisTurn = new HashSet<int>();

        /// <summary>每回合清"这个回合才上场的怪"（对手回合里的特召也清：那些怪到我们回合已经能攻击了）。</summary>
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
        /// 「这回合可以直接打死了」——**注册在 `Executors` 最前面**（内核的动作循环是
        /// "外层遍历规则、内层遍历候选"，战斗阶段要等所有规则都不出手才轮得到；排在后面的话
        /// "还有事可做"会把战阶一直推后，出现"对面只剩几百血却在继续做场"）。
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

        /// <summary>
        /// 「为了斩杀把蹲着的守备怪转成攻击」：对面空场、把它转成攻击之后伤害就够了 → 转。
        /// 只对本回合还没改过表示形式、且不是本回合才上场的怪提问（内核自己会判合法）。
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
        /// 这副牌的下级打点低（900～1800），平时蹲守备比站攻击划算；但对面空场、且战攻击就能收掉时，
        /// 表示形式的选择会直接决定这一刀打得出去打不出去（实测卡通就是这么漏掉斩杀的）。
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
        /// 盖放魔陷：基类判据（速攻/陷阱才盖）+ **"手里还要留魔法给本家当丢弃代价"时不盖**。

        /// 工匠链（锻造/万能杰妮/陶器/绘画）① 的发动条件是"解放自己 + **从手卡丢弃 1 张魔法卡**"，
        /// 先把唯一的魔法盖下去就等于自断（真人局实测：整个回合只剩"盖一张 + 上一个怪"）。
        /// </summary>
        private bool SpellSet()
        {
            if (HoldSpellForEngine())
                return false;
            return DefaultSpellSet();
        }

        /// <summary>手里还有没发过① 的工匠、手里也还有魔法 → 那张魔法要留着当丢弃代价。</summary>
        private bool HoldSpellForEngine()
        {
            if (!HasMagicInHand())
                return false;
            foreach (ClientCard card in Bot.Hand)
            {
                foreach (int cardId in ArtisanChain)
                {
                    if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        return true;
                }
            }
            return false;
        }

        /// <summary>手牌里有没有魔法卡（工匠链① 的代价就是它）。</summary>
        private bool HasMagicInHand()
        {
            foreach (ClientCard card in Bot.Hand)
            {
                if (card.HasType(CardType.Spell))
                    return true;
            }
            return false;
        }

        /// <summary>工匠链：锻造女巫 / 万能杰妮 / 陶器女巫 / 绘画女巫（① 都是"解放自己 + 丢 1 张魔法"）。</summary>
        private static readonly int[] ArtisanChain = { 21744288, 64756282, 59851535, 95245544 };

        /// <summary>
        /// 通用兜底"额外卡组的特殊召唤"：**只接手"没有专属 SpSummon 规则的额外怪"**
        /// （超魔导龙骑士、共命之翼、沼地泥龙王…照旧放行），有名有姓的那几张交回它们自己的闸门。
        ///
        /// ⚠ 原来这里是 `return true`，等于把上面两条专属闸门（:meth:`DarkMagicDestroyerSummon`、
        /// :meth:`AllowOtherExtraSummon`）整条顶掉：内核的动作循环是"**外层遍历规则、内层遍历候选**"，
        /// 而 `GameAI.ShouldExecute` 里通用规则的判据是 `exec.CardId == -1 || card.IsOriginalCode(exec.CardId)`
        /// ——**`-1` 的规则对任何卡都成立**。所以"这张卡有专属规则"并不会让兜底失效：专属规则返回
        /// false 只是让内核继续往后问，问到下面这条 `-1` 规则照样放行。
        /// 实测（`temp/train/wc-fix14-30.log`，30 局）：
        /// * 第 14 局 8899~8906 行：刚用「万能杰妮」② 复制墓地的「慶典」融合出来的「魔女术代理师傅」
        ///   （8★ 暗属性魔法师族，卡在 :data:`KeepOnBoard` 里）立刻被除外，就为了出「毁灭之黑魔术师」；
        ///   30 局里「毁灭之黑魔术师」出场 32 次，其中**17 次吃的是「魔女术工匠·服装女巫」、1 次吃「代理师傅」**
        ///   （这两张都是验收口径里的终场件）——`DarkMagicDestroyerSummon` 判 false 也拦不住。
        /// * 通用连接怪 30 局里出场 36 次，**其中 14 次是在"计划进行中"**（第 4/8/17/23/24 局，而
        ///   `AllowOtherExtraSummon` 的第一条就是 `PlanActive() → false`）；「交织绵羊」单独出场 11 次，
        ///   **其中 7 次的连接素材就是刚做出来的「代理师傅」**（第 4/8/9/10/17/25/26 局，都是
        ///   `代理师傅 from MonsterZone move to Grave` 紧跟 `交织绵羊 from Extra move to MonsterZone`）。
        /// 写法照抄基类的 `DefaultNoExecutor`（同一份语义：本卡有专属规则就交给它）。
        /// </summary>
        private bool AllowAnyExtraSummon()
        {
            foreach (CardExecutor exec in Executors)
            {
                if (exec.Type != ExecutorType.SpSummon || exec.CardId == -1)
                    continue;
                if (Card.IsOriginalCode(exec.CardId))
                    return false;
            }
            return true;
        }

        /// <summary>已经打过日志的计划回合。</summary>
        private int _loggedPlanTurn = -1;

        /// <summary>每回合读一次手牌匹配起手线；没匹配到就回原来的优先级。</summary>
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
            _planFusionPicked.Clear();
            if (HasInHand(CardId.ViceMadame))
                _planLine = PlanLine.Madame;
            // ⚠ 「玻璃女巫」两个印刷号都认（21522601 基准号 / 21522602 卡表里的号）。
            else if (HasInHand(CardId.GlassServant) || HasInHand(CardId.Veil) || HasInHand(CardId.VeilAlt))
                _planLine = PlanLine.Glass;
            else if (HasInHand(CardId.BystreetNight))
                _planLine = PlanLine.Field;
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

        /// <summary>商标女巫① / 玻璃② 的"从卡组拿 1 张本家场地·永续魔法"顺序：**场地优先**（教程第一步）。</summary>
        private static readonly int[] FieldSearchFirst =
        {
            CardId.BystreetNight,   // 魔女的圣夜行（场地）
            CardId.Creation,        // 魔女术的创造（教程：起手有场地时玻璃② 拿创造，反之拿场地）
            CardId.Bystreet,        // 魔女术的小巷（永续魔法）
            CardId.Celebration,     // 魔女工坊的慶典
            CardId.Unveiling,       // 魔女术的演示
            CardId.Draping,         // 魔女术的歪曲（多余的检索就拿它，回头盖放当阻坑）
        };

        /// <summary>
        /// 场地①（魔女的圣夜行）的检索：它是**从卡组拿 1 只「魔女术」怪兽**（还要再丢 1 张手卡）——
        /// 18 步教程第一步就是"拿「恩底弥翁的侍女 玻璃」"（她 ① 能从手牌自跳、② 再检索本家魔陷）。
        /// </summary>
        private static readonly int[] FieldMonsterSearchOrder =
        {
            22623509,   // 恩底弥翁的侍女 玻璃（教程第一步）
            7656689,    // 恩底弥翁的侍女 杰妮（手牌自跳 + 把场上的魔法师族换成别的本家）
            21744288,   // 魔女术工匠·锻造女巫（工匠链的起点）
            95245544,   // 魔女术工匠·绘画女巫
            59851535,   // 魔女术工匠·陶器女巫
            64756282,   // 魔女术工匠·万能杰妮
        };

        /// <summary>
        /// 「魔女术的创造」①（从卡组把 1 只「魔女术」怪兽加入手卡）拿谁：**锻造女巫优先**——
        /// 教程第 3 步"创造① 检索 锻造女巫"、第 4 步"NS 锻造女巫 → ① 解放自己、堆墓慶典 → 特召 万能杰妮"，
        /// 锻造① 是整条链的起点。其次才是能自跳的侍女玻璃/杰妮与其余下级（锻造不在卡组时兜住）。
        /// </summary>
        private static readonly int[] CreationMonsterSearchOrder =
        {
            21744288,   // 魔女术工匠·锻造女巫（链子起点：把慶典送进墓地）
            22623509,   // 恩底弥翁的侍女 玻璃（手牌自跳 + ② 检索本家魔陷）
            7656689,    // 恩底弥翁的侍女 杰妮
            59851535,   // 魔女术工匠·陶器女巫
            64756282,   // 魔女术工匠·万能杰妮
            95245544,   // 魔女术工匠·绘画女巫
            84523092,   // 魔女术工匠·服装女巫
        };

        /// <summary>
        /// 「魔女术学童组合」① 的第一支（从卡组拿 1 张本家魔法）拿什么：**慶典**优先——
        /// 18 步教程拿它就是为了第二次融合出「代理师傅」（"发动学童组合的效果，检索速攻魔法慶典"）。
        /// </summary>
        private static readonly int[] PupilsMagicSearchOrder =
        {
            CardId.Celebration,     // 魔女工坊的慶典（第二次融合）
            CardId.Creation,        // 魔女术的创造
            CardId.Bystreet,        // 魔女术的小巷
            CardId.Unveiling,       // 魔女术的演示
            CardId.Draping,         // 魔女术的歪曲
        };

        /// <summary>
        /// 工匠链特召顺序的**底表**（无脑对调之前的原顺序：**商标女巫在玻璃女巫之前**）。
        /// 锻造①/万能杰妮①/陶器①/绘画① 都是"解放自己 + 丢 1 张魔法 → 特召 1 只本家"，
        /// 教程的顺序是 **万能杰妮 → 陶器 → 商标女巫**（商标是 6★ 暗属性魔法师，正好给
        /// 「毁灭之黑魔术师」当"除外自己场上 1 只 6★ 以上暗魔法师"的素材）。
        ///
        /// ⚠ 2026-10-06 第四轮（历史）：为解决"玻璃女巫 30/30 局全缺"（她上场的路只有工匠链这一条），
        /// 曾把「魔女术师傅·玻璃女巫」（21522601/21522602）**无条件**提到「商标女巫」前面。依据是：
        /// * **只有工匠链拉得到她**：大魔女① 的 spfilter 是 `IsLevelBelow(7)`（脚本 `c33475154.lua:37`）、
        ///   代理师傅② 的 spfilter 是 `IsLevelBelow(6)`（`c9603252.lua:66`）——两张 8★ 卡都拉不到 8★ 的她；
        ///   而工匠链① 的 spfilter 只有 `IsSetCard(0x128)`（`c95245544.lua:58`、`c59851535.lua:59` 同型），
        ///   **没有等级上限**。她也没有任何召唤限制，所以"从卡组特召 1 只本家"每次都会把她放进候选。
        /// * 实测（`temp/train/wc-fix14-30.log`，30 局）：她"从卡组上场"6 次，**每一次前面紧跟的都是某只
        ///   工匠的 `activate effect from MonsterZone` → `from MonsterZone move to Grave`**
        ///   （第 15/16/19/20/23/24 局）＝确实全走这条链；而她到**首回合终场一次都没上过场**
        ///   （`tools/plan_accept.py` 五件口径 0/30；唯一做到 4 件的第 7 局就差她一张）。
        /// * 原来她排第 4，前面垫着 万能杰妮(1 张)／陶器(1 张)／商标女巫(3 张)**共 5 张**，所以只有它们
        ///   全部离开卡组之后才轮得到她——第 23/24 局的"`商标女巫 from Deck move to MonsterZone` 之后
        ///   下一环才是她"就是这条顺位的指纹。
        ///
        /// ⚠⚠ 2026-10-06 第五轮：**不能无条件对调**——同种子 30 局 A/B 把代价量出来了
        /// （`temp/train/wc-fix16-30.log` 无脑对调后 vs `wc-fix14-30.log` 对调前）：
        ///
        /// | 指标 | 对调前（wc-fix14-30.log） | 无脑对调后（wc-fix16-30.log） |
        /// |---|---|---|
        /// | 玻璃女巫从卡组上场 | 0 | **40 次** ✅ |
        /// | 毁灭之黑魔术师出场 | 32 | **12** ⬇ |
        /// | 超魔导龙骑士出场 | **24** | **0** ❌ |
        ///
        /// 原因：「商标女巫」是「毁灭之黑魔术师」替代召唤**唯一**的 6★ 暗属性魔法师素材
        /// （`c59400890.lua:50` 的 spfilter：DARK + `IsLevelAbove(6)`，且 `c59400890.lua:58` 限定
        /// `LOCATION_MZONE`＝必须**站在场上**）。她顺位后移后，需要"收尾位只剩一个"的那条线整局做不出来
        /// → 超魔导龙骑士（这副牌的终端之一）出场直接归零。
        ///
        /// 所以这张底表按**原顺序**（商标在前）保留，实际顺序由 :meth:`CraftsmanChainOrderNow` 按当前
        /// 场面算：**这条线的素材看得见（怪兽区/手牌/墓地）且「毁灭之黑魔术师」还没做出来**时把玻璃
        /// 提到前面（趁这次机会带她出来），否则原样返回，保证毁灭之黑魔术师/超魔导龙骑士那条线不断。
        /// ⚠ 2026-10-06 第六轮把"商标已经站在怪兽区"放宽成"素材可见即可"——实测账与卡点
        /// （玻璃 0→10、超魔导 24→0，`acc-93.log` / `wc-fix14-30.log`）见 :meth:`CanPromoteGlassFirst`。
        /// </summary>
        private static readonly int[] CraftsmanChainBaseOrder =
        {
            64756282,   // 魔女术工匠·万能杰妮
            59851535,   // 魔女术工匠·陶器女巫
            6071005,    // 魔女术工匠·商标女巫（6★ 暗：给毁灭之黑魔术师当素材；默认排在玻璃之前）
            21522601,   // 魔女术师傅·玻璃女巫（基准号；与下一行另一印刷号**两个都认**）
            21522602,   // 魔女术师傅·玻璃女巫（验收件＋阻坑；没有① → 只能是链条的收尾）
            84523092,   // 魔女术工匠·服装女巫
            95245544,   // 魔女术工匠·绘画女巫
            21744288,   // 魔女术工匠·锻造女巫
        };

        /// <summary>
        /// 本次"从卡组特召 1 只本家"要按什么顺序挑（工匠链的**条件顺位**）：
        /// * :meth:`CanPromoteGlassFirst` 成立（**素材可见 + 这条线还没做出来**，判据见那里）
        ///   → **玻璃女巫优先**（她只有工匠链① 这一条上场路，趁这次机会把她带出来；两个印刷号都认）；
        /// * 否则维持底表原顺序（**商标在前**），保证「毁灭之黑魔术师」的替代召唤素材不断。
        /// 依据与实测代价见 :data:`CraftsmanChainBaseOrder` 的注释（同种子 30 局 A/B 表）。
        ///
        /// ⚠ 2026-10-06 第六轮：判据从第五轮的"商标**已经站在怪兽区**"放宽成"**这条线的素材可见即可**"
        /// （手牌/墓地也算，再叠一条"这条线还没做出来"的护栏），实测账与卡点见 :meth:`CanPromoteGlassFirst`。
        /// </summary>
        private int[] CraftsmanChainOrderNow()
        {
            if (!CanPromoteGlassFirst())
                return CraftsmanChainBaseOrder;
            // 素材可见、且这条线还没做出来：把「玻璃女巫」（两个印刷号）插到「商标女巫」之前，原位置跳过。
            List<int> order = new List<int>();
            foreach (int cardId in CraftsmanChainBaseOrder)
            {
                if (cardId == CardId.ViceMadame)
                {
                    order.Add(CardId.Veil);
                    order.Add(CardId.VeilAlt);
                }
                if (cardId == CardId.Veil || cardId == CardId.VeilAlt)
                    continue;
                order.Add(cardId);
            }
            return order.ToArray();
        }

        /// <summary>
        /// 这次"从卡组特召 1 只本家"能不能先把「玻璃女巫」提上来（＝放行 :meth:`CraftsmanChainOrderNow` 的对调）。
        /// **两条同时成立才放宽**：
        ///
        /// ① **「商标女巫」这条线的素材可见**（:meth:`IsViceMadameMaterialVisible`：怪兽区／手牌／墓地
        ///    任一处有她即可——"看得见"＝不是"丢了就没了"，所以这次可以把卡组的那一跳先让给玻璃）；
        /// ② **「毁灭之黑魔术师」这条线还没做出来**（:meth:`IsDarkMagicDestroyerMade`）——做出来了就不急，
        ///    按原顺序慢慢来（护栏）。
        ///
        /// ⚠ 2026-10-06 第六轮（为什么放宽）：第五轮那版只认"商标**已经站在自己怪兽区**"，实测不够。
        /// 同种子 30 局（对空白、窗口含结束阶段）的账（这一轮只改本条判据，别的修复一个字没动）：
        ///
        /// | 指标 | 原始顺序（wc-fix14-30.log） | 无脑对调（wc-fix16-30.log） | 第五轮：只认"商标在怪兽区"（acc-93.log，R9 构建） |
        /// |---|---|---|---|
        /// | 玻璃女巫从卡组上场 | 0（首回合终场 0/30） | **40 次** ✅ | **10 次**（有效果，但离 ≥30 差得远） |
        /// | 毁灭之黑魔术师出场 | 32 | 12 | 24 |
        /// | 超魔导龙骑士出场 | **24** | **0** ❌ | **0** ❌ |
        ///
        /// 卡点在**条件本身几乎不成立**：工匠链第 1 步就是从卡组拉人，而那个时点「商标女巫」通常还在
        /// **手牌/墓地**（她自己就是"被拉的对象"），她站到怪兽区上只可能是"链条已经拉过她一次"之后——
        /// 那时链条往往已经走完，玻璃自然还是排不上（acc-93 里只拿到 10 次，超魔导仍然是 0）。
        /// 放宽成"素材可见"＝拿"素材还在手牌/墓地/场上，不会丢"去换"玻璃这次先走一步"。
        ///
        /// ⚠ 这一版**仍然有风险**（留给复核量，本轮没有改）：
        /// * **手牌**不等于素材已就位：工匠链① 的起手是"通召一只工匠上场 → 解放它自己"，通召点被链条
        ///   自己吃掉之后，手里的商标女巫当回合未必还能站到怪兽区；而替代召唤（`c59400890.lua:58`）**限定
        ///   `LOCATION_MZONE`**——所以"她在手牌"只保证"没丢"，**不保证这条线还做得出来**；
        /// * **墓地**那条更没把握：本执行器没有"把商标女巫从墓地拉回来"的点名规则（代理师傅①/蒂奥类效果
        ///   都不在本类的判据里），**她能不能被拉回场我不确定**；留着这一条是因为"墓地里的魔法师族"本来
        ///   就是这副牌的融合素材来源（慶典②），多认它不会挡别的线。
        /// </summary>
        private bool CanPromoteGlassFirst()
        {
            // **默认关掉**：见 PromoteGlassInCraftsmanChain 的实测表——玻璃和超魔导是在抢同一次"链条跳"，
            // 现有机制下是零和（原顺序 超魔导 24 局 : 玻璃 6 局；对调后 3 : 18），保超魔导更值。
            if (!PromoteGlassInCraftsmanChain)
                return false;
            // ② 护栏：「毁灭之黑魔术师」已经做出来了 → 按原顺序慢慢来，不再为它留素材。
            if (IsDarkMagicDestroyerMade())
                return false;
            // ① 素材可见：怪兽区（最稳）／手牌／墓地，任一处有「商标女巫」就够。
            return IsViceMadameMaterialVisible();
        }

        /// <summary>
        /// 玻璃女巫的顺位提升开关（**默认 false**＝维持底表原顺序、商标女巫在前）。
        ///
        /// 为什么默认关：**玻璃和超魔导是在抢同一次"链条跳"**。同种子 30 局、按"出现该事件的**局数**"数：
        /// | 指标 | 原顺序 | 无脑对调 | 条件版（商标在场上时） |
        /// |---|---|---|---|
        /// | 玻璃女巫从卡组上场 | 6 | **18** | 5 |
        /// | 毁灭之黑魔术师 | 29 | 6 | 12 |
        /// | 蒂迈欧之眼光发动 | 24 | 3 | 4 |
        /// | **超魔导龙骑士** | **24** | 3 | 4 |
        /// 第六轮把条件放宽到"商标在**手牌/墓地**也算"之后，条件在前期几乎恒真（商标在手牌正是最常见的开局）
        /// → 实际等价于最宽那档 → 预期仍是"玻璃上去了、超魔导接近 0"，**两头都够不着**。
        /// 结论：现有机制下这是零和，**保超魔导**（这副牌的真终端）；要让玻璃稳定上场得**另开一条出口**
        /// （大魔女① 是 `IsLevelBelow(7)`、代理师傅② 是 `IsLevelBelow(6)`，都拉不到 8★ 的她），
        /// 那是下一刀单独量的活（判据：30 局口径下 玻璃 ≥30 次**且** 超魔导 ≥20 次）。
        /// </summary>
        private static readonly bool PromoteGlassInCraftsmanChain = false;

        /// <summary>
        /// 「商标女巫」（6071005）这条线的素材**看不看得见**：怪兽区（＝已经站在 `LOCATION_MZONE`，
        /// 可以直接被替代召唤除外，最稳）／手牌（能通召，但要花掉通召点）／墓地（要靠别的东西拉回来，
        /// 见 :meth:`CanPromoteGlassFirst` 的风险说明）——**任一处有就算**。
        /// 认 6071005 的 `IsCode`/`IsOriginalCode`（"两个印刷号都认"的统一口径）。
        /// </summary>
        private bool IsViceMadameMaterialVisible()
        {
            if (IsViceMadameOnBoard())
                return true;
            // 手牌：本类的 `HasInHand` 就是"两个印刷号都认"的写法。
            if (HasInHand(CardId.ViceMadame))
                return true;
            // 墓地：`Bot.HasInGraveyard` 只认 `IsCode`，这里按本类口径自己过一遍（也认 `IsOriginalCode`）。
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (IsThatCard(card, CardId.ViceMadame))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「毁灭之黑魔术师」（59400890）这条线**是不是已经做出来了**（护栏判据）。按"看得见的地方"判：
        /// * 自己怪兽区（含额外怪兽区，`Bot.GetMonsters()`）上已经有它 → **做出来了**（替代召唤/融合出场过）；
        /// * 怪兽区上没有它、额外卡组（`Bot.ExtraDeck`）里还看得见它 → **还没做出来**（它还在额外卡组里躺着）；
        /// * 额外卡组里也看不见、但墓地/除外里看得见 → 融合召唤出场后送进了墓地/除外 → 也算**做出来了**；
        /// * 哪儿都看不见 → **保守按"还没做出来"处理**：里侧的额外卡组卡在内核把卡号发过来之前本来就是
        ///   未知的，不能把"看不见"当成"已经做出来"——否则护栏会一直判死，第六轮的放宽整条失效。
        /// 用现成常量 :data:`CardId.DarkMagicDestroyer`，各处都认 `IsCode`/`IsOriginalCode`。
        /// </summary>
        private bool IsDarkMagicDestroyerMade()
        {
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (IsThatCard(card, CardId.DarkMagicDestroyer))
                    return true;
            }
            foreach (ClientCard card in Bot.ExtraDeck)
            {
                if (IsThatCard(card, CardId.DarkMagicDestroyer))
                    return false;
            }
            foreach (ClientCard card in Bot.Graveyard)
            {
                if (IsThatCard(card, CardId.DarkMagicDestroyer))
                    return true;
            }
            foreach (ClientCard card in Bot.Banished)
            {
                if (IsThatCard(card, CardId.DarkMagicDestroyer))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 商标女巫有没有站在**自己的怪兽区**上（`Bot.GetMonsters()` 就是自己的主怪兽区，对应
        /// 「毁灭之黑魔术师」替代召唤要求的 `LOCATION_MZONE`）。认 6071005 的 `IsCode`/`IsOriginalCode`。
        /// </summary>
        private bool IsViceMadameOnBoard()
        {
            foreach (ClientCard card in Bot.GetMonsters())
            {
                if (card.IsCode(CardId.ViceMadame) || card.IsOriginalCode(CardId.ViceMadame))
                    return true;
            }
            return false;
        }

        /// <summary>现在问的是不是"从卡组拿本家场地·永续魔法"那两个效果（商标女巫① / 玻璃②）。</summary>
        private bool IsDeckFieldSearchEffect()
        {
            ClientCard effect = Duel.GetCurrentSolvingChainCard();
            if (effect == null)
                effect = Duel.GetCurrentChainCard();
            if (effect == null)
                return false;
            // 「玻璃女巫」两个印刷号都认（21522601 基准号 / 21522602 卡表里的号）。
            return IsThatCard(effect, CardId.ViceMadame)
                || IsThatCard(effect, CardId.GlassServant)
                || IsThatCard(effect, CardId.Veil)
                || IsThatCard(effect, CardId.VeilAlt);
        }

        /// <summary>现在是谁的效果在问（发动时用 GetCurrentChainCard，结算中用 GetCurrentSolvingChainCard）。</summary>
        private ClientCard CurrentEffect()
        {
            ClientCard solving = Duel.GetCurrentSolvingChainCard();
            if (solving != null)
                return solving;
            return Duel.GetCurrentChainCard();
        }

        /// <summary>
        /// 现在问的是不是"融合召唤"那几张：慶典①、陶俑魔像②，以及**万能杰妮②（复制墓地的慶典）**——
        /// 18 步教程第 6 步的「大魔女 桑德里永」就是靠万能杰妮② 复制的慶典融合出来的，
        /// 那个时点"正在结算的卡"是万能杰妮，只认慶典会漏掉（顺位表跟着不更新，后面 学童/代理 顺位全乱）。
        /// </summary>
        private bool IsFusionEffect()
        {
            ClientCard effect = CurrentEffect();
            return IsThatCard(effect, CardId.Celebration)
                || IsThatCard(effect, 70686400)
                || IsThatCard(effect, 64756282);
        }

        /// <summary>
        /// 现在结算的是不是「慶典」的分支①（炸我方 1 只「魔女工坊」＋对面 1 张）——**包括被复制过来的场合**：
        /// 「万能杰妮」② 复制墓地魔法、「学童组合」① 第二支展示手卡魔法，都会让内核用**被复制那张卡**的
        /// 选项/选卡来提问（探针实测 `正在结算=魔女术工匠·万能杰妮 options=[111337074,111337075]`）。
        /// </summary>
        private bool IsCelebrationCopy()
        {
            ClientCard effect = CurrentEffect();
            return IsThatCard(effect, CardId.Celebration)
                || IsThatCard(effect, CardId.Genni)
                || IsThatCard(effect, CardId.Pupils);
        }

        /// <summary>
        /// 本回合还没出过的融合终端，按教程顺序排（大魔女 → 学童组合 → 代理师傅）。
        /// 空集合时返回完整顺序；三只都出过就返回空，调用方会落到下面的固定顺位。
        /// </summary>
        private int[] FusionOrderForThisTurn()
        {
            List<int> order = new List<int>();
            foreach (int cardId in PlanFusionOrder)
            {
                if (!_planFusionPicked.Contains(cardId))
                    order.Add(cardId);
            }
            return order.ToArray();
        }

        /// <summary>候选里有没有怪兽（用来把"场地① 检索本家怪"和"玻璃② 检索本家魔陷"分开）。</summary>
        private static bool ContainsMonster(IList<ClientCard> cards)
        {
            foreach (ClientCard card in cards)
            {
                if (card != null && card.HasType(CardType.Monster))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 把候选卡名拼成一行（探针用）：`卡名(位置)`，位置用 H/G/M/R/D 首字母区分，
        /// 这样"手牌丢弃/卡组送墓"这类请求里**候选到底长什么样**能直接从日志看出来。
        /// </summary>
        private static string CardNames(IList<ClientCard> cards)
        {
            string text = "";
            foreach (ClientCard card in cards)
            {
                if (card == null)
                    continue;
                char where = '?';
                if (card.Location == CardLocation.Hand)
                    where = 'H';
                else if (card.Location == CardLocation.Deck)
                    where = 'D';
                else if (card.Location == CardLocation.MonsterZone)
                    where = 'M';
                else if (card.Location == CardLocation.Grave)
                    where = 'G';
                else if (card.Location == CardLocation.Removed)
                    where = 'R';
                else if (card.Location == CardLocation.SpellZone)
                    where = 'S';
                text += (text.Length == 0 ? "" : "、") + card.Name + "(" + where + ")";
            }
            return text.Length == 0 ? "（空）" : text;
        }

        /// <summary>现在问的是不是工匠链那四只（锻造①/万能杰妮①/陶器①/绘画①）。</summary>
        private bool IsCraftsmanChainEffect()
        {
            ClientCard effect = CurrentEffect();
            return IsThatCard(effect, 21744288)   // 锻造
                || IsThatCard(effect, 64756282)   // 万能杰妮
                || IsThatCard(effect, 59851535)   // 陶器
                || IsThatCard(effect, 95245544);  // 绘画
        }

        /// <summary>是不是某张卡（空值安全，同一张卡的多个印刷号都算）。</summary>
        private static bool IsThatCard(ClientCard card, int cardId)
        {
            return card != null && (card.IsCode(cardId) || card.IsOriginalCode(cardId));
        }

        /// <summary>计划进行中要"先放一放"的额外怪（通用连接怪，会把终场件当素材吃掉）。</summary>
        private static readonly int[] OffPlanExtraMonsters =
        {
            45819647,   // 神圣魔皇后 塞勒涅
            86066373,   // 访问码语者
            27519978,   // 四花缭乱之灵使
            29301451,   // S：P小夜骑士
            65741789,   // I：P百变莱娜
            4993187,    // W：P变幻舞夜
        };

        /// <summary>
        /// 「表侧召唤 or 里侧盖放」：**这副牌一律表侧召唤**。
        ///
        /// 基类 `DefaultExecutor.OnSelectMonsterSummonOrSet` 的判据是"等级 ≤4 且自己场上没有表侧怪
        /// 且**对手的怪全都打得过我** → 盖放"。魔女术的下级（工匠/侍女/学童组合…）效果几乎全是
        /// "**召唤·特殊召唤成功的场合**"（锻造① 解放自己堆墓、陶器① 检索、商标① 检索场地、
        /// 万能杰妮① 复制墓地魔法…）——**里侧盖放不算召唤成功**，盖下去等于把唯一的动点扔掉；
        /// 这副牌靠的是"每回合用下级滚一次引擎"，不是靠一堵盖着的墙活着。
        /// </summary>
        public override bool OnSelectMonsterSummonOrSet(ClientCard card)
        {
            return false;
        }

        private bool SummonOrSet()
        {
            EnsurePlan();   // 回合开头把计划算出来（高频入口，保证第一次决策就有线）
            if (Card != null)
            {
                foreach (int id in HandTraps)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return false;
                }
                // 「魔女术工匠·商标女巫」：**场地还在卡组里时不通召她**。
                // 她的 ① 是"解放手牌的自己 → 从卡组检索一张本家场地/永续魔法"，也就是展开指南
                // STEP 2 的起步（拿「魔女的圣夜行」，之后丢手牌就能换成从卡组堆本家魔陷）。
                // 把她召唤上场等于把这一步浪费掉——实测 10 局里她被召唤上去、**一次都没进过墓地**
                // （＝那个检索效果从来没发动），场地也一次没落位，整条启动线就断在这里。
                // 只在"场地还没到手、还没落位"时留着她（她的手牌效果能把场地检索出来）；
                // **场地到手/落位之后就要让她下场**——实测原来"卡组里还有场地就永远不通召"把她整局封死：
                // 手牌效果一次性、之后场上一个魔法师族都没有，玻璃/服装全跳不出来，首回合两三个动作就结束。
                if ((Card.IsCode(CardId.ViceMadame) || Card.IsOriginalCode(CardId.ViceMadame))
                    && Bot.HasInDeck(CardId.BystreetNight)
                    && !Bot.HasInHand(CardId.BystreetNight)
                    && !Bot.HasInSpellZone(CardId.BystreetNight))
                    return false;
                foreach (int id in PreferredSummons)
                {
                    if (Card.IsCode(id) || Card.IsOriginalCode(id))
                        return true;
                }
            }
            return DefaultMonsterSummon();
        }

        /// <summary>
        /// 通召优先级。这副牌的下级几乎都能从手牌自跳，但**杰妮①/玻璃① 都需要"场上有魔法师族"**，
        /// 万能杰妮① 又要把自己解放掉——所以先摆一个**魔法师族下级**上去，后面那几个才跳得出来。
        /// 不写这条时走基类"挑最像打手的"，实测第一个回合常常只剩一个怪站场、被对面直接攻击打死
        /// （循环赛 8 局里挨了 26 次伤害，超融合一次都没用上——场上根本没素材）。
        /// </summary>
        private static readonly int[] PreferredSummons =
        {
            21744288,   // 魔女术工匠·锻造女巫（检索本家魔陷）
            95245544,   // 魔女术工匠·绘画女巫
            59851535,   // 魔女术工匠·陶器女巫
            84523092,   // 魔女术工匠·服装女巫
            64756282,   // 魔女术工匠·万能杰妮（解放自己从卡组拉本家）
            70686400,   // 魔女工坊 陶俑魔像（能从墓地回收 + 融合）
            21522601,   // 魔女术师傅·玻璃女巫（基准号）
            21522602,   // 魔女术师傅·玻璃女巫（卡表里用的是这个印刷号；两个印刷号都认）
        };

        /// <summary>
        /// 额外卡组出哪只的优先级。**候选来自内核**（＝这一回合真的做得出来的那些），
        /// 我们只在这堆里挑顺位最高的；做不出来的因为不在候选里会自动跳过。
        ///
        /// 为什么需要它：没有这段时走基类"从候选尾部取一只"，很容易挑到当回合做不出来的融合/连接怪，
        /// **动作被内核丢掉**——用户看到的就是"展开一通场上只有一个怪 / 二回合搞半天只有一个交织绵羊"。
        /// </summary>
        private static readonly int[] ExtraDeckPriority =
        {
            // ⚠ 顺序按教程改过：**先出「魔女术学徒」检索融合魔法，再用被检索的融合出「魔女术代理师傅」**
            // （202605 教程"四种调度"第 4 条 + 常见问题）。原来把代理师傅排在前面，等于先出终端再补魔法。
            // 18 步教程的出场顺序：**大魔女 → 学童组合 → 代理师傅**
            // （大魔女① 直接拉 3 只本家；学童组合① 检索「慶典」；慶典再出代理师傅并回收「歪曲」）。
            33475154,   // 大魔女 桑德里永 Lv8（要玻璃女巫当素材，所以只有够素材时才会被选中）
            69964858,   // 魔女术学童组合 Lv8：检索本家魔法
            9603252,    // 魔女术代理师傅 Lv8：回收本家魔陷 + 从卡组拉本家 + 炸卡阻坑
            59400890,   // 毁灭之黑魔术师 Lv8
            37818795,   // 超魔导龙骑士-真红眼龙骑士 Lv8
            11765832,   // 共命之翼 迦楼罗 Lv6
            54757758,   // 沼地的泥龙王 Lv4
            50277355,   // 交织绵羊（连接）
            45819647,   // 神圣魔皇后 塞勒涅（连接）
            86066373,   // 访问码语者（连接）
            27519978,   // 四花缭乱之灵使（连接）
            29301451,   // S：P小夜骑士（连接）
            65741787,   // I：P百变莱娜（连接）
            4993187,    // W：P变幻舞夜（连接）
        };

        /// <summary>
        /// 下级①（解放自己＋丢 1 魔法）与场地②（代替丢弃）都要**从卡组送 1 张本家魔陷去墓地**，
        /// 送哪张直接决定这条链能不能接下去。按教程"常见问题 3"的顺序：
        /// **第一次送「慶典」（参与展开）→ 第二次送反击陷阱「歪曲」（给代理师傅回收当阻坑）
        /// → 之后送「小巷」「怠工」（结束阶段能自己回手当后续 cost）→ 最后才送「玻璃女巫」。**
        /// </summary>
        private static readonly int[] DeckSendPriority =
        {
            CardId.Celebration,     // 魔女术的庆祝会（速攻融合，展开核心）
            CardId.Draping,         // 魔女术的歪曲（反击陷阱，代理师傅回收后盖放＝阻坑）
            CardId.Bystreet,        // 魔女术的小巷（结束阶段回场）
            CardId.Tire,            // 魔女术的怠工（结束阶段回手，当 cost）
            CardId.Veil,            // 魔女术师傅·玻璃女巫（基准号 21522601；大魔女的融合素材，最后再送）
            CardId.VeilAlt,         // 魔女术师傅·玻璃女巫（卡表里用的是这个印刷号；两个印刷号都认）
        };

        /// <summary>
        /// 「魔女术代理师傅」回收墓地本家魔陷时先拿什么：教程给的用法是**回收反击陷阱「歪曲」盖放做阻坑**，
        /// 其次才是继续展开用的「慶典」。
        /// </summary>
        private static readonly int[] RecoverPriority =
        {
            CardId.Draping,
            CardId.Celebration,
            CardId.Bystreet,
            CardId.Tire,
        };

        /// <summary>
        /// 「大魔女 桑德里永」融合召唤时从卡组拉最多 3 只 7 星以下本家（同属性最多 1 只）。
        /// 教程"常见问题 2"的顺序：**服装女巫（7 星阻坑）→ 陶俑魔像（和场上的大魔女融合出代理师傅）
        /// → 一只还没发过①的下级（用它①堆墓反击陷阱、特召玻璃女巫做阻坑）**。
        /// </summary>
        private static readonly int[] SummonPriority =
        {
            CardId.Haine,           // 魔女术工匠·服装女巫（7 星，站场即阻坑）
            CardId.Terracotta,      // 魔女工坊 陶俑魔像（②和场上的大魔女融合出学童组合）
            CardId.Pittore,         // 绘画女巫（18 步教程第 13 步：大魔女拉 服装/赤陶偶/绘画 三只）
            CardId.Schmietta,       // 锻造女巫
            CardId.Potterie,        // 陶器女巫
            CardId.Genni,           // 万能杰妮
        };

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

        /// <summary>
        /// 手坑与陷阱的**时机**：一律走 WindBot 原生执行器给这些卡准备好的判据，
        /// 不再"能发就发"（用户实测"开局丢 G""手坑乱扔"就是这么来的）：
        /// * 增殖的G —— 只在**对手回合**开；灰流丽 —— 只在**连锁对手的卡**时开；
        /// * 幽鬼兔/屋敷童 —— 对手召唤后或连锁对手时；遮蒙者 —— 交给"要无效对面哪只怪"的判据；
        /// * 锁鸟/朔夜时雨/欢聚友伴 —— 只在对手回合、且连锁对手的卡（否则纯白扔）；
        /// * 其余陷阱（含本家的「魔女术的歪曲」）—— 对手召唤后或连锁对手时才开。
        /// </summary>
        private bool Activate()
        {
            if (Card != null)
            {
                // **场上阻抗件**：对手在动（连锁对手的卡）时，场上的这些怪要主动发效果。
                // 它们原来**没有任何时机规则**，只会走通用兜底（"能发就发/不连锁自己"），
                // 实战表现就是"我展开顺风顺水、它完全不干扰"（审计：代理师傅 出场 2 次发动 0 次、
                // 超魔导龙骑士 出场 3 次发动 0 次）。这条保证它们在对手发动效果时优先发动。
                if (Duel.LastChainPlayer == 1 && IsAny(Card, InterruptionCards))
                    return true;
                // **牌库快空时，墓地的②效果不再发动**：这副牌的墓地②（锻造②堆墓、绘画②抽1丢1、
                // 陶器②回收）都会继续消耗牌库，实测对"打不穿的墙"（空白 0 攻/2100 守）打到 27 回合
                // **自己把 40 张牌抽干判负**（败因"没有卡可抽"）。留 8 张缓冲，够打完这回合就行。
                if (Card.Location == CardLocation.Grave && Bot.Deck.Count < 8)
                    return false;
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
                    // 自己回合丢它＝把自己的检索全锁死（真人局实测过一次整回合空过）；
                    // 欢聚友伴两张要"对手召唤才抽"，自己回合丢＝白扔。这三张只在对手回合、连锁对手的卡；
                    // 「朔夜时雨」只废对面一只怪，任何回合都可以。
                    if (Card.IsCode(94145022) || Card.IsCode(84192580) || Card.IsCode(42141493))
                        return Duel.Player == 1 && Duel.LastChainPlayer == 1;
                    return Duel.LastChainPlayer == 1;
                }
                if (Card.HasType(CardType.Trap))
                    return DefaultTrap();
                // 「蒂迈欧线」三张——「毁灭之黑魔术师」②（检索）/「蒂迈欧之眼光」①（融合）/
                // 「超魔导龙骑士-真红眼龙骑士」②③——**不能被下面那条"能让路就让路"吞掉**。
                // 它们在:data:`InterruptionCards` 里本来就被当作"要主动发"（代理师傅/玻璃女巫/服装女巫
                // 都有专属 Activate 规则、走不到这里），而这三张**在本类里没有专属 Activate 规则**
                // （只登记了 SpSummon 闸门与各种名单），所以它们的效果一律落到这条通用兜底上，
                // 于是被下面那条闸门**整条掐死**：
                // * 点掉「毁灭之黑魔术师」的条件本身就要求它出现在 `main.SpecialSummonableCards` 里
                //   （＝它落场那一刻 `ExtraSummonAvailable()` 对它**恒真**），所以 ② 的触发必然被答成
                //   "不应" → 「蒂迈欧之眼光」上不了手 → 「超魔导龙骑士」一局都做不出来；
                // * 即便 ② 过了，**通常魔法**「蒂迈欧之眼光」在同一个主要阶段也过不了同一条闸门
                //   （`Duel.MainPhase` 只在 `SELECT_IDLECMD` 时刷新＝**上一次主要阶段的缓存**，
                //   那张额外怪会一直留在列表里，整个回合都为真 —— 见 `GameBehavior.OnSelectIdleCmd`）。
                //
                // 现场证据（真人局 `logs/app_20261006_110745.log.jsonl`，剧本局2）：
                // 1752~1754 行「商标女巫」被除外（替代召唤的代价）→「毁灭之黑魔术师」从额外卡组特召上场，
                // **全场没有一条它的 `activate effect`**；把 logs 目录里所有 jsonl 一起数，
                // `毁灭之黑魔术师 activate effect` 与 `蒂迈欧之眼光` 都是 **0 次**，而
                // :meth:`DarkMagicDestroyerSummon` 的验收口径要的是"超魔导 ≥20/30 局、服装女巫 ≥12 次"。
                // 这一局也正是死在这里：对手 1946~1954 行用「完美电子多元驱动蛇·神龙」（ATK 5000，
                // 连接召唤成功时破坏对方场上全部怪兽）一次清掉我方三只并直击 5000（7400 → 2400），
                // 而我方手里（服装女巫＋山雀）**没有一张能应它**——唯一的解就是这条被闸门掐掉的
                // 「超魔导龙骑士」③（丢 1 张手卡把这个发动无效并破坏），它在场的话那一击还能被直接无效。
                if (IsAny(Card, InterruptionCards) || IsThatCard(Card, CardId.TimaeusEye))
                    return true;
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
        /// 场上的关键阻抗件（对手发动效果时优先发动；见施工单 `docs/interruption-audit.md`）：
        /// 代理师傅 = 回收本家魔陷 + 炸卡阻坑；超魔导龙骑士 = 效果无效；玻璃女巫 = 对方全场怪兽效果无效；
        /// 服装女巫 = 破坏场上 1 张表侧卡。
        /// </summary>
        private static readonly int[] InterruptionCards =
        {
            9603252,    // 魔女术代理师傅
            37818795,   // 超魔导龙骑士-真红眼龙骑士
            21522601,   // 魔女术师傅·玻璃女巫（基准号）
            21522602,   // 魔女术师傅·玻璃女巫（卡表里用的是这个印刷号；两个印刷号都认）
            84523092,   // 魔女术工匠·服装女巫
            59400890,   // 毁灭之黑魔术师
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
        /// 选项：把上面登记的"分支值"换成引擎要的**下标**回回去（WindBot 的约定是回下标，
        /// 见 `options.IndexOf(值)`）。没登记时交给基类。
        /// </summary>
        public override int OnSelectOption(IList<int> options)
        {
            // 探针：分支型效果的选项清单（内核只在"这一支做得出来"时才把它列进来——
            // 这就是"慶典 为什么会走成炸自己人"的直接证据）
            if (_verbose && CurrentEffect() != null)
            {
                string list = "";
                foreach (int value in options)
                    list += (list.Length == 0 ? "" : ",") + value;
                Logger.WriteLine("[探针] 选项：正在结算=" + CurrentEffect().Name
                    + " options=[" + list + "] 登记值=" + _optionValue);
            }
            // 登记过的分支（值来自 :meth:`EncodedOption`，**带卡号**）：命中了才清，免得被别的卡
            // 的选项请求提前吃掉（原来一被问就清，等于经常轮不到真正的效果用）。
            if (_optionValue != 0)
            {
                for (int i = 0; i < options.Count; ++i)
                {
                    if (options[i] == _optionValue)
                    {
                        _optionValue = 0;
                        return i;
                    }
                }
            }
            return base.OnSelectOption(options);
        }

        /// <summary>
        /// 选卡：候选里**同时有对面和我方的卡**时先选对面的——"破坏/弹回/除外场上一张卡"这类效果
        /// 会把双方都放进候选，从尾部取会选到自己的卡（另一副牌踩过："别自己发陷阱炸自己"）。
        /// 纯我方的候选（检索、送墓、选素材）原样交给基类。
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
            if (_verbose && min >= 2)
            {
                string names = "";
                foreach (ClientCard card in cards)
                    names += (names.Length == 0 ? "" : "、") + (card == null ? "（空）" : card.Name);
                Logger.WriteLine("[探针] 多选请求：min=" + min + " max=" + max + " hint=" + hint
                    + " 阶段=" + Duel.Phase + " 候选=" + (names.Length == 0 ? "（空）" : names));
            }
            EnsurePlan();   // 检索、送墓、回收这些分支都要先知道本回合走哪条线

            // 额外卡组：从内核给的候选里按顺位挑一只（挑不到就交给下面的通用逻辑）
            if (_verbose && min <= 1 && max >= 1 && AllInExtraDeck(cards))
            {
                string names = "";
                foreach (ClientCard card in cards)
                    names += (names.Length == 0 ? "" : "、") + (card == null ? "（空）" : card.Name);
                Logger.WriteLine("[探针] 额外卡组挑选：候选=" + names
                    + "｜正在结算=" + (CurrentEffect() == null ? "（无）" : CurrentEffect().Name));
            }
            if (min <= 1 && max >= 1 && AllInExtraDeck(cards))
            {
                // 融合怪：按"本回合还没出过的终端"挑（教程 大魔女 → 学童组合 → 代理师傅）。
                // 只在这三张出现在候选里时介入，其余（毁灭之黑魔术师、超魔导、连接怪）走固定顺位。
                // ⚠ 两轮：**先挑"场上还没有的"**——已经在场的那只再做一遍，① 这一回合也用不出来
                //（脚本 `SetCountLimit(1,卡号)`＝每种融合怪 1 回合 1 次），实测第 23 局就是拿场上的
                // 「魔女术学童组合」＋「服装女巫」当素材又做了一只「学童组合」（净亏两只终场件）。
                // 场上的都试过才退回按顺位（宁可做一只重复的，也别把这次融合白费）。
                if (IsFusionEffect())
                {
                    int[] fusionOrder = FusionOrderForThisTurn();
                    for (int pass = 0; pass < 2; ++pass)
                    {
                        foreach (int cardId in fusionOrder)
                        {
                            if (pass == 0 && Bot.HasInMonstersZone(cardId))
                                continue;
                            foreach (ClientCard card in cards)
                            {
                                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                                {
                                    _planFusionPicked.Add(cardId);
                                    IList<ClientCard> fused = new List<ClientCard>();
                                    fused.Add(card);
                                    return fused;
                                }
                            }
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

            // 「从卡组送 1 张本家魔陷去墓地」（下级①、场地② 的代替丢弃）：按教程的顺序送
            //（慶典 → 歪曲 → 小巷/怠工 → 玻璃女巫），这是"能不能接上代理师傅回收"的关键
            // 「破坏魔法·陷阱（0～N 张）」这类**可选**破坏：候选里只有我方的卡时一张都不选
            //（基类从尾部挑＝自己炸自己）。
            if (min == 0 && hint == HintMsg.Destroy && HasNoEnemyCard(cards))
                return new List<ClientCard>();
            // 「我方 1 只本家 ＋ 对面 1 张」这种**必选**的破坏（「慶典」分支①）自家那只一定要挑不心疼的：
            // 实测基类从尾部挑，把场上的「服装女巫」（合格线终场件）炸了去换对面的魂虎。
            // 候选全是终场件时不动手（挑谁都是亏），交回基类。
            if (min >= 1 && max >= 1 && hint == HintMsg.Destroy && HasNoEnemyCard(cards))
            {
                foreach (ClientCard card in cards)
                {
                    if (IsKeepOnBoard(card))
                        continue;
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(card);
                    return picked;
                }
            }
            // 同一条分支，但内核把两侧的候选**合并**给过来（脚本：`g1:Merge(g2)` 之后
            // `SelectSubGroup(2,2)`，过滤条件是"至少含 1 张我方的 + 1 张对方的"）：
            // 所以这里要自己配一组——**对面随便挑 1 张，我方那只挑不心疼的**。
            // 我方那侧全是终场件时不选（下面的通用逻辑会兜住，且 :meth:`Celebration` 已经尽量不让这张卡
            // 在这种场面开出来）。
            if (min >= 2 && hint == HintMsg.Destroy && !HasNoEnemyCard(cards))
            {
                ClientCard enemy = null;
                ClientCard ownCheap = null;
                foreach (ClientCard card in cards)
                {
                    if (card.Controller == 1)
                    {
                        if (enemy == null)
                            enemy = card;
                    }
                    else if (!IsKeepOnBoard(card) && ownCheap == null)
                    {
                        ownCheap = card;
                    }
                }
                if (enemy != null && ownCheap != null)
                {
                    List<ClientCard> picked = new List<ClientCard>();
                    picked.Add(enemy);
                    picked.Add(ownCheap);
                    return picked;
                }
            }

            if (min <= 1 && max >= 1 && hint == HintMsg.ToGrave && HasNoEnemyCard(cards))
            {
                // 效果感知：**「锻造女巫」的墓地②**（除外自身 → 从卡组送 1 张本家卡进墓地）在 18 步教程里
                // 是专门去送「魔女术师傅·玻璃女巫」的（她随后当「大魔女 桑德里永」的融合素材；
                // 慶典的融合素材允许墓地+除外区，脚本里 pre_select_mat_location 已确认）。
                // 场地的②（换手卡的代替送墓）才用 DeckSendPriority 的默认顺序（先慶典、再歪曲）。
                bool fromSchmiettaGrave = IsThatCard(CurrentEffect(), CardId.Schmietta);
                if (fromSchmiettaGrave)
                {
                    ClientCard veil = null;
                    foreach (ClientCard card in cards)
                    {
                        // 「玻璃女巫」两个印刷号都认（21522601 基准号 / 21522602 卡表里的号）。
                        if (card.IsCode(CardId.Veil) || card.IsOriginalCode(CardId.Veil)
                            || card.IsCode(CardId.VeilAlt) || card.IsOriginalCode(CardId.VeilAlt))
                            veil = card;
                    }
                    if (veil != null)
                    {
                        IList<ClientCard> picked = new List<ClientCard>();
                        picked.Add(veil);
                        return picked;
                    }
                }
                // 剩下按 DeckSendPriority 的顺序（先慶典、再歪曲…），但**先挑"墓地/除外里还没有的"那张**
                //（同名卡在卡组里不止一张，见 :meth:`PickDeckSend`）。
                ClientCard send = PickDeckSend(cards);
                if (send != null)
                {
                    List<ClientCard> picked = new List<ClientCard>();
                    picked.Add(send);
                    return picked;
                }
            }

            // 工匠链（锻造①/万能杰妮①/陶器①/绘画① 的"解放自己 + 丢 1 张魔法 → 特召 1 只本家"）：
            // 必须排在下面那条笼统的 SpSummon 分支之前——否则会被 SummonPriority 抢走服装女巫，
            // 无法按教程的 万能杰妮 → 陶器 → 商标（商标 6★ 暗，正好给毁灭之黑魔术师当除外素材）铺链。
            // ⚠ 做过一次 A/B（2026-10-04，与卡通/杀调/闪刀各 10 局）：这一版与"回滚成服装优先"版
            // 战绩相当（都在 15～18%），所以保留教程顺序——它对"把 18 步链走通"是必要条件。
            // ⚠ 2026-10-06 第五轮：顺位表改成**按当前场面算**（:meth:`CraftsmanChainOrderNow`）——
            // 条件成立才把「玻璃女巫」提前，否则维持"商标在前"。
            // 无脑对调会掐断「毁灭之黑魔术师」的替代召唤素材（超魔导龙骑士 24 → 0，实测表见
            // :data:`CraftsmanChainBaseOrder` 的注释）——她是全牌组唯一拉不出来的验收件（0/30），
            // 而这条链是她唯一的上场路，所以只在素材看得见时给她优先。
            // ⚠ 第六轮：条件放宽成"**素材可见（怪兽区/手牌/墓地）+ 这条线还没做出来**"——第五轮那版
            // 只认"商标已经在怪兽区"，实测玻璃只有 10 次、超魔导仍是 0（`acc-93.log`），
            // 卡点与实测账见 :meth:`CanPromoteGlassFirst`。这里的分支一个字没动。
            if (min >= 1 && hint == HintMsg.SpSummon && HasNoEnemyCard(cards) && IsCraftsmanChainEffect())
            {
                foreach (int cardId in CraftsmanChainOrderNow())
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

            // 「大魔女 桑德里永」从卡组拉最多 3 只本家（内核的候选就是"能拉的"）：
            // 按教程顺序挑——服装女巫（阻坑）→ 陶俑魔像（和场上的大魔女融合出代理师傅）→ 没发过①的下级
            if (min >= 1 && max >= 1 && hint == HintMsg.SpSummon && HasNoEnemyCard(cards))
            {
                List<ClientCard> picked = new List<ClientCard>();
                foreach (int cardId in SummonPriority)
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

            // 「除外自己场上一只怪」这类代价（内核给 HintMsg.Remove）：**别把终场件除外掉**——
            // 实测「毁灭之黑魔术师」的替代召唤条件（除外自己场上 1 只 6 星以上暗属性魔法师族）把刚融合出来的
            // 「代理师傅」除外了（30 局里 8 次），终场直接消失；「侍女杰妮②」的"除外自己场上的魔法师族"是同款。
            // 这两处教程的指定代价件都是「商标女巫」（6★ 暗属性魔法师族），所以先挑它、再挑其它不心疼的；
            // 场上凑不出 min 张更便宜的（比如只剩终场件）就交回基类，不去动"本来就得喂"的融合素材。
            if (min >= 1 && max >= 1 && hint == HintMsg.Remove && HasNoEnemyCard(cards) && AllInMonsterZone(cards))
            {
                List<ClientCard> expendable = new List<ClientCard>();
                foreach (ClientCard card in cards)
                {
                    if (!IsKeepOnBoard(card))
                        expendable.Add(card);
                }
                if (expendable.Count >= min)
                {
                    List<ClientCard> picked = new List<ClientCard>();
                    foreach (ClientCard card in expendable)
                    {
                        if (card.IsCode(CardId.ViceMadame) || card.IsOriginalCode(CardId.ViceMadame))
                            picked.Add(card);
                    }
                    foreach (ClientCard card in expendable)
                    {
                        if (!picked.Contains(card))
                            picked.Add(card);
                    }
                    return Util.CheckSelectCount(picked, cards, min, max);
                }
            }

            // 「魔女术工匠·万能杰妮」② 的**除外代价**：除外墓地的自己 + 1 张「魔女术」魔法（脚本
            // c64756282.cpcost：`Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_REMOVE)` 之后从 LOCATION_GRAVE 选 1 张）。
            // 教程第 11 步要的是"复制墓地的「慶典」去融合大魔女"，所以只要墓地素材够，先拿慶典；
            // 素材不齐时不硬拿（那时慶典只剩"炸我方 1 只 + 对面 1 张"那一支，白亏一只怪——和
            // :meth:`Celebration` 的闸门同一个道理）。拿不到就交回后面的通用逻辑。
            if (min <= 1 && max >= 1 && hint == HintMsg.Remove && HasNoEnemyCard(cards) && AllInGrave(cards)
                && IsThatCard(CurrentEffect(), CardId.Genni))
            {
                if (FusionMaterialsReady())
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(CardId.Celebration) || card.IsOriginalCode(CardId.Celebration))
                        {
                            IList<ClientCard> picked = new List<ClientCard>();
                            picked.Add(card);
                            return picked;
                        }
                    }
                }
            }

            // 「融合素材」：**内核不走 `OnSelectFusionMaterial`**——「超融合」「陶俑魔像」②「慶典」②
            // 都是 `FusionSpell` 库 → `Duel.SelectFusionMaterial`，内核按"一次问一张"发到 `OnSelectCard`
            //（实测 30 局日志里"融合素材请求"探针**一次都没打印**，而每一次素材进墓前面都跟着
            // `MonsterZone 's X become target`）。原来这里没有分支 → 落到下面"检索优先级"那条通用分支，
            // 它按 :data:`PreferredPicks` 挑 → **先把「服装女巫」当素材送掉**（实测 16 次从怪兽区进墓里
            // 11 次是这么死的：超融合 6、陶俑魔像② 5，其中一次还顺手喂掉了场上的「学童组合」）。
            // 这里改成按"最舍得给"的顺序挑（见 :meth:`FeedCostScore`）：陶俑魔像/商标女巫先让，
            // 大魔女/侍女/下层次之，合格线件与超魔导线最后——教程第 13 步的素材正是「陶俑魔像 + 大魔女」。
            // ⚠ 多加一道 `AllMonsters`：融合素材的候选一定全是怪兽（"手牌·场上/墓地·除外的怪兽"），
            // 而 :meth:`IsFusionEffect` 也会在「万能杰妮② 复制别的魔法」时命中——复制「圣夜行」① 的那次
            // "丢手牌"候选里就有魔法卡，不该被这里吃掉（那条交给下面的手牌分支）。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards) && IsFusionMaterialRequest() && AllMonsters(cards))
            {
                ClientCard cheap = PickCheapestFeed(cards);
                if (cheap != null)
                {
                    if (_verbose)
                        Logger.WriteLine("[探针] 融合素材：正在结算=" + (CurrentEffect() == null ? "（无）" : CurrentEffect().Name)
                            + " 挑=" + cheap.Name + "｜候选=" + cards.Count + " 张");
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(cheap);
                    return picked;
                }
            }

            // 「慶典」分支①（我方 1 只「魔女工坊」怪獸 + 对手 1 张卡破坏）的**自毁那只**：
            // 脚本是 `g1:Merge(g2)` 之后 `SelectSubGroup(...,2,2,s.gcheck)`（要求"至少 1 张我方 + 1 张对面"），
            // 内核把它拆成"一次一张"的 SelectUnselect 请求发过来（min/max 报的是 0/1，所以上面那条
            // `min >= 2` 的合并分支从来没命中过）。候选里有对面的卡时，原来会落到通用 PreferredPicks 分支
            // → 把场上的「服装女巫」炸掉换对面的魂虎（实测第 16/20 局）。这里改成：**自己那只挑最舍得给的**。
            if (min <= 1 && max >= 1 && hint == HintMsg.Destroy && !HasNoEnemyCard(cards) && IsCelebrationCopy())
            {
                ClientCard cheap = PickCheapestFeed(cards);
                if (cheap != null)
                {
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(cheap);
                    return picked;
                }
            }

            // 「场地②」的**代替送墓**：下级① 的"丢 1 张魔法"代价会把"卡组里的本家魔陷"也放进候选
            //（脚本 c21744288.spcost / c64756282.spcost：costfilter 含 LOCATION_DECK，提示是
            // `Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_DISCARD)` + `g:Select(1,1)`），教程"常见问题 3"要的是
            // **卡组那张**（"场地② 精准堆墓 4 次：第 1 次慶典参与展开、第 2 次反击陷阱「歪曲」当阻坑"）——
            // 手卡魔法要留给 超魔导/服装女巫/玻璃女巫 当 cost。原来没有这条分支，卡组候选落到下面那条
            // "通用检索优先级"，实测 30 局里把「魔女术的创造」从卡组送墓 **39 次**、「魔女术的歪曲」只
            // **1 次** → 代理师傅① 回收时墓地没有歪曲可拿，终场自然盖不出反击陷阱（歪曲 2/30 的直接原因）。
            if (min <= 1 && max >= 1 && hint == HintMsg.Discard && HasNoEnemyCard(cards))
            {
                ClientCard send = PickDeckSend(cards);
                if (send != null)
                {
                    if (_verbose)
                        Logger.WriteLine("[探针] 丢弃代价（Discard）：挑卡组「" + send.Name + "」｜候选=" + CardNames(cards));
                    IList<ClientCard> picked = new List<ClientCard>();
                    picked.Add(send);
                    return picked;
                }
                // 卡组里没有优先名单上的卡时**不要直接往下掉**：下面的通用检索分支（PreferredPicks）
                // 会把「创造」当成"最该拿的一张"送墓（实测 `wc-fix14-30.log` 第 5 局 117/119 行：
                // 锻造① 与 服装女巫① 连着两次把「创造」从卡组送进墓地）。这里留个探针，
                // 把"候选里到底有什么"打出来，下一轮据此决定要不要再补一档顺序。
                if (_verbose)
                    Logger.WriteLine("[探针] 丢弃代价（Discard）：卡组优先名单没命中，落到通用分支｜候选=" + CardNames(cards));
            }

            // 「魔女术的创造」①（从卡组把 1 只「魔女术」怪兽加入手卡）：教程第 3 步的检索目标是
            // **「锻造女巫」**——锻造① 才是"解放自己＋丢 1 张魔法 → 从卡组特召本家"的**链子起点**，
            // 也靠场地② 把「慶典」从卡组送进墓地。原来没有专属分支，落到下面的通用 PreferredPicks
            // → 一律先拿「恩底弥翁的侍女 杰妮」（实测 30 局里被检索 35 次），起手常常就此停在"手上一只
            // 下位、场上一个怪不留"，正是空场局的那条链。
            // ⚠ 2026-10-06 第三轮：补 `hint == HintMsg.AddToHand` 闸门。理由是这类"某张卡专属检索"分支
            // **只该接它的检索步骤**——同一张卡结算过程中还会有别的选卡请求（代价、丢弃、目标），
            // 只按 `CurrentEffect()` + `ContainsMonster(cards)` 判会把它们一起接走（同型 bug 的现场
            // 见下面 :meth:`圣夜行` 那支的实测）。创造① 的检索在脚本里写死 `HINTMSG_ATOHAND`，闸门不影响它。
            if (min <= 1 && max >= 1 && hint == HintMsg.AddToHand && HasNoEnemyCard(cards)
                && IsThatCard(CurrentEffect(), CardId.Creation) && ContainsMonster(cards))
            {
                foreach (int cardId in CreationMonsterSearchOrder)
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

            // 「代理师傅」回收墓地本家魔陷：先拿反击陷阱「歪曲」盖放做阻坑，再拿「慶典」续展开
            // 检索 vs 回收要分开：**商标女巫① / 玻璃② 都是"从卡组拿 1 张本家场地·永续魔法"**（教程第一步
            // 要的是场地「魔女的圣夜行」）；它们和"代理师傅回收墓地"共用 HINTMSG_ATOHAND，原来一起落到
            // 回收顺序 RecoverPriority（第三条是小巷）——首回合拿的是小巷、场地整局没落位，链断在起手。
            // 计划层（教程顺序）：场地① 检索本家**怪**；工匠链特召下一环。
            //
            // ⚠⚠ 2026-10-06 第三轮修复（"检索上手 2 步内被从手牌消耗"的**根因**）：
            // **这条分支必须只接"检索"那一步，也就是 `hint == HintMsg.AddToHand`。**
            // 「魔女的圣夜行」①（脚本 `c32353566.thop`）是**同一个效果里的两步选择**：
            //   ① `Duel.Hint(HINT_SELECTMSG, tp, HINTMSG_ATOHAND)` → `SelectMatchingCard(LOCATION_DECK)`
            //      从卡组检索 1 只本家怪；
            //   ② 检索成功后 `Duel.BreakEffect()` → `HINTMSG_DISCARD` → `SelectMatchingCard(Card.IsDiscardable,
            //      LOCATION_HAND)` **从手牌丢 1 张**（这一步没有"代替"途径，是必须交的）。
            // 两步的"正在结算的卡"都是圣夜行，而原来这里只判 `CurrentEffect()` + `ContainsMonster(cards)`
            // ——第二步（丢弃）的候选**全是手牌**，其中只要有 1 张怪（检索上来的那张通常就是），
            // 这个分支就会被命中，然后按 :data:`FieldMonsterSearchOrder` 从**手牌**里挑第一张匹配的怪送墓。
            // 也就是说 **它把刚检索上手的怪（或手牌里顺位最高的玻璃/杰妮/锻造）当成"检索目标"丢掉了**。
            //
            // 实测（`temp/train/wc-fix14-30.log`，30 局；`mb5-93.log`，20 局，同型均现）：
            // `X from Deck move to Hand` 之后 2 步内出现同一张 `X from Hand move to Grave`：
            //   锻造 11（+12）、侍女玻璃 8（+3）、杰妮 5（+5）、绘画 3（+4）、万能杰妮 2（+2）、商标 1（+3）
            //   ——名字分布与 :data:`FieldMonsterSearchOrder` 的**顺序完全一致**（玻璃→杰妮→锻造→绘画→
            //   陶器→万能杰妮），这是"被检索分支接走"的指纹。
            // 典型现场（第 4 局 166~174 行）：
            //   166 圣夜行① 发动 → 167 学童组合① 发动 → 170 学童拿「创造」→ 172 圣夜行拿「侍女玻璃」
            //   → **174 `恩底弥翁的侍女 玻璃 from Hand move to Grave`**；
            //   第 6/8/10 回合同样：248 拿玻璃 → 250 丢玻璃；289 拿杰妮 → 291 丢杰妮。
            // 这也解释了上一轮"侍女玻璃 13、锻造 11"为什么明知道它们在 `KeepOnBoard`/`PreferredPicks`
            // 里还照丢：**根本轮不到下面那条手牌保护分支**（它在这条之后）。
            // 补上闸门后，第二步会落到 :meth:`…手牌丢弃分支`，按"不在关键名单里的先丢"处理。
            if (min <= 1 && max >= 1 && hint == HintMsg.AddToHand && HasNoEnemyCard(cards)
                && IsThatCard(CurrentEffect(), CardId.BystreetNight) && ContainsMonster(cards))
            {
                foreach (int cardId in FieldMonsterSearchOrder)
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
            // 「魔女术学童组合」① 第一支的检索（教程：拿慶典）
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards) && IsThatCard(CurrentEffect(), 69964858))
            {
                foreach (int cardId in PupilsMagicSearchOrder)
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

            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards) && IsDeckFieldSearchEffect())
            {
                // 两轮：先挑"手上/场上还没有的"（避免重复拿同一张），全都已有才退回按顺序拿。
                for (int pass = 0; pass < 2; ++pass)
                {
                    foreach (int cardId in FieldSearchFirst)
                    {
                        bool owned = Bot.HasInHand(cardId) || Bot.HasInSpellZone(cardId);
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

            if (min <= 1 && max >= 1 && hint == HintMsg.AddToHand && HasNoEnemyCard(cards))
            {
                foreach (int cardId in RecoverPriority)
                {
                    foreach (ClientCard card in cards)
                    {
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                        {
                            List<ClientCard> picked = new List<ClientCard>();
                            picked.Add(card);
                            return picked;
                        }
                    }
                }
            }

            // 解放/吃自己场上的怪当费用（万能杰妮①、庆典的融合素材等）：挑**最不心疼的**
            //（token → 不是起手件的 → 攻最低），基类从尾部取会把刚做出来的融合怪吃掉。
            if (min <= 1 && max >= 1 && hint == HintMsg.Release)
            {
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
                    List<ClientCard> picked = new List<ClientCard>();
                    picked.Add(cheapest);
                    return picked;
                }
            }

            // 「检索/堆墓/特召」这类**纯我方候选**：按展开指南的优先级挑一张。
            // ⚠ 只在候选**不在手牌**时才用这套优先级：手牌的选卡多半是"丢手牌当费用"，
            //    照优先级挑会把手里的关键牌（商标女巫/创造/庆典）丢掉——这是"乱开"里最伤的一种。
            // 也只在"只要 1 张"时替引擎决定：一次选多张的语义各卡不同，交给基类。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards) && !AnyInHand(cards))
            {
                // 场地已经在场上时，检索优先拿"能解放自己从卡组特召本家"的下级
                //（锻造/绘画/万能杰妮 = 这副牌真正的特召引擎）；场地没上手才先拿商标女巫去取场地。
                if (Bot.HasInSpellZone(CardId.BystreetNight, notDisabled: true))
                {
                    foreach (int cardId in new[] { CardId.Schmietta, CardId.Pittore, CardId.Genni })
                    {
                        foreach (ClientCard card in cards)
                        {
                            if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                            {
                                List<ClientCard> engineFirst = new List<ClientCard>();
                                engineFirst.Add(card);
                                return engineFirst;
                            }
                        }
                    }
                }
                // 先过一遍"手上/场上还没有的"（重复拿场地/慶典等于把这次检索白送），
                // 全都有了才退回按优先级拿。
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
                                List<ClientCard> picked = new List<ClientCard>();
                                picked.Add(card);
                                return picked;
                            }
                        }
                    }
                }
            }

            // 手牌的选卡（丢手牌当费用这类）：**先丢"不在关键名单里"的**，同一档里优先丢重复的。
            // 依据：展开指南的结束阶段清单——「杰妮」要留着当下回合的特召点、「商标女巫」要留在墓地
            // 当对手回合的干扰，所以它们（以及创造/庆典等引擎件）都不该被当费用丢掉。
            if (min <= 1 && max >= 1 && HasNoEnemyCard(cards) && AnyInHand(cards))
            {
                if (_verbose)
                    Logger.WriteLine("[探针] 手牌丢弃：hint=" + hint + "｜正在结算="
                        + (CurrentEffect() == null ? "（无）" : CurrentEffect().Name) + "｜候选=" + CardNames(cards));
                List<ClientCard> handCards = new List<ClientCard>();
                foreach (ClientCard card in cards)
                {
                    if (card.Location == CardLocation.Hand)
                        handCards.Add(card);
                }
                Dictionary<int, int> counts = new Dictionary<int, int>();
                foreach (ClientCard card in handCards)
                {
                    counts[card.Id] = counts.ContainsKey(card.Id) ? counts[card.Id] + 1 : 1;
                }
                // ① 不在关键名单里的重复牌 ② 任何不在关键名单里的 ③ 关键牌里重复的
                // ⚠ 关键名单＝:data:`PreferredPicks` ＋ :data:`KeepOnBoard` ＋ 手坑：实测（30 局日志）
                // 「恩底弥翁的侍女 玻璃」11 次、「魔女术工匠·锻造女巫」10 次在**刚被检索上手 3 步内**
                // 就被当成"丢 1 张手卡"的代价丢掉（圣夜行① 的丢弃；第 1 局最典型：场地① 搜锻造 → 立刻
                // 把锻造丢了 → 那个回合场上一个怪都没留下）。只看 PreferredPicks 时这两张都不在名单里，
                // 于是"pass 1＝第一个不在名单里的候选"正好命中刚检索回来的那张。
                // ⚠⚠ 2026-10-06 第三轮复核：这条保护**原来根本没被执行**——圣夜行① 的丢弃步骤被上面
                // 那条"场地① 检索本家怪"分支抢走了（见那里的实测），所以这些丢卡不是名单漏卡，
                // 而是**压根没走到这里**。给那条分支补了 `hint == HintMsg.AddToHand` 之后，
                // 丢弃步骤才会落到这里按下面三档挑最不心疼的。
                for (int pass = 0; pass < 3; ++pass)
                {
                    foreach (ClientCard card in handCards)
                    {
                        bool key = IsPreferredPick(card) || IsKeepOnBoard(card) || IsAny(card, HandTraps);
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
                        if (_verbose)
                            Logger.WriteLine("[探针] 手牌丢弃：挑「" + card.Name + "」"
                                + (key ? "（关键牌但手里重复）" : "（非关键）") + " pass=" + pass);
                        List<ClientCard> picked = new List<ClientCard>();
                        picked.Add(card);
                        return picked;
                    }
                }
                // ④⑤ 关键牌全在手里、且一张都没重复（例：手牌 = {刚检索上来的服装女巫, 灰流丽}）——
                // 三档全落空时**绝不能交回基类**：`DoEverythingExecutor.OnSelectCard` 的实现是
                // "select the last cards"（取候选列表**最后** max 张），而候选列表是按手牌序号排的、
                // **刚被检索/回收进手的新卡在末位**，于是基类每次都正好把刚检索上来的那张丢掉。
                // 实测（`temp/train/wc-fix14-30.log`）「魔女术的歪曲」墓地②（除外自己 → 从卡组检索
                // 1 只 5★以上魔法师族 → 再丢 1 张手牌）第 3 局 step90~93：检索「服装女巫」上手后
                // **紧接着就 `服装女巫 from Hand move to Grave`**（丢完的 Bot Hand 只剩「灰流丽」，
                // 说明当时手牌只有"刚检索的服装女巫 + 灰流丽"，两张都在保护名单里、都不重复）；
                // 第 4/9/10 局同型（检索「商标女巫」→ 立刻丢掉）。全日志 25 次歪曲墓地② 里有 4 次
                // 现场能确认"刚一上手就被丢"。
                // 兜底改成：**从列表前面挑**（越靠前＝在手里放得越久，刚进手的在末位），
                // 并且先避开手坑（手坑丢了整局就没有阻抗，宁可丢一张还能被检索/回收的本家件）。
                // ⚠ 两道限定：① 只在**候选全是手牌**时兜（＝这就是"丢手牌"这类请求；混着卡组候选的
                // 工匠链代价不能这么挑——那条路的上策是走卡组代替，交回基类/后面的分支反而对）；
                // ② 场地「魔女的圣夜行」永不主动丢（它是检索链起点，也是"代价代替"的来源，
                // 实测第 1 局就是先把唯一的手牌魔法盖/丢掉导致整条链断掉）。min == 0 的可选请求也不兜。
                if (min >= 1 && cards.Count == handCards.Count)
                {
                    for (int pass = 3; pass < 5; ++pass)
                    {
                        foreach (ClientCard card in handCards)
                        {
                            if (card.IsCode(CardId.BystreetNight) || card.IsOriginalCode(CardId.BystreetNight))
                                continue;
                            bool handTrap = IsAny(card, HandTraps);
                            if (pass == 3 && handTrap)
                                continue;   // ④ 先避开手坑；⑤ 实在只剩手坑才给
                            if (_verbose)
                                Logger.WriteLine("[探针] 手牌丢弃：挑「" + card.Name + "」（关键牌兜底 pass=" + pass
                                    + (handTrap ? "，只剩手坑" : "") + "）");
                            List<ClientCard> picked = new List<ClientCard>();
                            picked.Add(card);
                            return picked;
                        }
                    }
                }
                if (_verbose)
                    Logger.WriteLine("[探针] 手牌丢弃：三档都没命中（候选可能全是关键牌），交给基类");
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

        /// <summary>
        /// 「从卡组送 1 张本家魔陷去墓地」（下级① 的代替丢弃、锻造② 的堆墓、光之泪①）挑哪张。
        /// 顺序来自教程"常见问题 3"（**第 1 次慶典参与展开 → 第 2 次反击陷阱「歪曲」当阻坑 →
        /// 之后留给这回合没发过①的本家魔法**，见 :data:`DeckSendPriority`），但**先挑"墓地/除外里还
        /// 没有的那张"**：这副牌 慶典 有 2 张、创造 3 张，不跳过"已经送过一张的"就会把两次送墓都花在
        /// 同一个卡名上——实测（`wc-fix13-30.log`）「创造」被从卡组送墓 39 次、「歪曲」只有 1 次，
        /// 代理师傅① 回收时墓地没有歪曲可拿，终场自然盖不出那张反击陷阱（歪曲 2/30 的直接原因）。
        /// 全都在墓地/除外里了才退回按顺序拿（第二遍）。
        /// </summary>
        private ClientCard PickDeckSend(IList<ClientCard> cards)
        {
            for (int pass = 0; pass < 2; ++pass)
            {
                foreach (int cardId in DeckSendPriority)
                {
                    if (pass == 0 && Bot.HasInGraveyardOrInBanished(cardId))
                        continue;      // 这一档"已经有一张在墓地/除外了"，把这次送墓让给下一档（歪曲 只有 1 张）
                    foreach (ClientCard card in cards)
                    {
                        if (card.Location != CardLocation.Deck)
                            continue;
                        if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                            return card;
                    }
                }
            }
            return null;
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

        /// <summary>候选是不是全在怪兽区（用来把"场上的除外代价"和"墓地·除外的除外代价"分开）。</summary>
        private static bool AllInMonsterZone(IList<ClientCard> cards)
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
        /// 候选是不是**全是怪兽**——融合素材（手牌·场上 / 墓地·除外的魔法师族）一定满足这条；
        /// 用来把"丢 1 张手卡当费用"（候选里混着魔法卡）那种请求排除在融合素材分支之外。
        /// </summary>
        private static bool AllMonsters(IList<ClientCard> cards)
        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card == null || !card.HasType(CardType.Monster))
                    return false;
            }
            return true;
        }

        /// <summary>候选是不是全在墓地（「万能杰妮②」的除外代价就是从这里挑 1 张本家魔法）。</summary>
        private static bool AllInGrave(IList<ClientCard> cards)
        {
            if (cards.Count == 0)
                return false;
            foreach (ClientCard card in cards)
            {
                if (card.Location != CardLocation.Grave)
                    return false;
            }
            return true;
        }

        /// <summary>
        /// "被当素材/代价吃掉"时**最后才动**的卡：验收口径的合格线件（代理师傅＋学童组合＋服装女巫＋
        /// 玻璃女巫＋歪曲）与超魔导那条线（超魔导龙骑士、它的融合素材毁灭之黑魔术师）。
        /// 依据：HANDOFF"验收口径"；实测（`temp/train/wc-fix13-30.log` 我方进程）
        /// `魔女术工匠·服装女巫 from MonsterZone move to Grave` 共 16 次，逐次回放下来
        /// **全部是自己这边吃掉的**：融合素材 11 次（「超融合」6、「魔女工坊 陶俑魔像」② 5）、
        /// 「慶典」分支① 5 次（含被「学童组合」/「万能杰妮」② 复制过去的那两次）。
        /// </summary>
        private static readonly int[] NeverFeedFromBoard =
        {
            9603252,    // 魔女术代理师傅
            69964858,   // 魔女术学童组合
            84523092,   // 魔女术工匠·服装女巫
            21522601,   // 魔女术师傅·玻璃女巫
            21522602,   // 魔女术师傅·玻璃女巫（另一印刷）
            37818795,   // 超魔导龙骑士-真红眼龙骑士
            59400890,   // 毁灭之黑魔术师
        };

        /// <summary>
        /// 把这张卡当"融合素材 / 破坏代价"的代价评分：**越小越舍得给**。
        /// 三档来自教程自己的取舍：
        /// * 0 —— `KeepOnBoard` 之外的（陶俑魔像＝① 召出来的融合工具人、商标女巫＝6★ 暗专门喂
        ///   「毁灭之黑魔术师」的代价件）：教程里就是拿去用的；
        /// * 1 —— 做完事的中继（大魔女① 拉完 3 只身体之后，教程第 13 步就是把她和陶俑魔像融成学童组合；
        ///   侍女玻璃/杰妮、下级们同上）；
        /// * 2 —— :data:`NeverFeedFromBoard`（合格线件与超魔导线），最后才动。
        /// </summary>
        private static int FeedCostScore(ClientCard card)
        {
            if (!IsKeepOnBoard(card))
                return 0;
            foreach (int cardId in NeverFeedFromBoard)
            {
                if (card.IsCode(cardId) || card.IsOriginalCode(cardId))
                    return 2;
            }
            return 1;
        }

        /// <summary>
        /// 从候选里挑"最舍得给"的那张（场上/手牌的怪兽里评分最低的；同分优先手牌——手牌那张不吃站场）。
        /// 只考虑我方、且不在卡组/墓地的候选（墓地·除外的素材本来就是"用过的资源"，不在这里介入）。
        /// 一个都没挑到时返回 null，交给后面的既有逻辑。
        /// </summary>
        private static ClientCard PickCheapestFeed(IList<ClientCard> cards)
        {
            ClientCard best = null;
            int bestScore = int.MaxValue;
            foreach (ClientCard card in cards)
            {
                if (card == null || card.Controller != 0)
                    continue;
                if (card.Location != CardLocation.MonsterZone && card.Location != CardLocation.Hand)
                    continue;
                int score = FeedCostScore(card);
                int order = card.Location == CardLocation.Hand ? 0 : 1;   // 同分先给手牌
                int bestOrder = best == null ? 2 : (best.Location == CardLocation.Hand ? 0 : 1);
                if (score < bestScore || (score == bestScore && order < bestOrder))
                {
                    bestScore = score;
                    best = card;
                }
            }
            return best;
        }

        /// <summary>
        /// 现在问的是不是"把场上的怪兽当融合素材"（「超融合」/「陶俑魔像」②/「慶典」② 这些
        /// 都走 `FusionSpell` 库 → `Duel.SelectFusionMaterial`，内核按"一次问一张"发过来）。
        /// </summary>
        private bool IsFusionMaterialRequest()
        {
            // ⚠ 超融合两个印刷号都认（48130397 基准号 / 48130398 卡表里的号）。
            return IsFusionEffect()
                || IsThatCard(CurrentEffect(), CardId.SuperPolymerization)
                || IsThatCard(CurrentEffect(), CardId.SuperPolymerizationAlt);
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

        /// <summary>候选里有没有手牌（手牌候选多半是"丢手牌当费用"，不能按检索的优先级挑）。</summary>
        private static bool AnyInHand(IList<ClientCard> cards)
        {
            foreach (ClientCard card in cards)
            {
                if (card.Location == CardLocation.Hand)
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 「魔女工坊 陶俑魔像」：场上的 ②（手牌·场上融合）该开；**手牌的 ① 不要乱开**——
        /// ① 带上"这个回合我方除了「魔女工坊」怪兽以外不能从额外牌组特殊召唤"的自肃，
        /// 而展开指南 STEP 7 要做「交织绵羊」、收官要用「访问码语者」/「超魔导龙骑士」，
        /// 被这条锁住就全做不出来了。
        /// </summary>
        private bool Terracotta()
        {
            if (Card != null && Card.Location == CardLocation.Hand)
                return false;
            return true;
        }

        /// <summary>
        /// 「检索 / 堆墓 / 从卡组特召」这类**纯我方候选**时的取卡优先级，来自群友给的展开指南：
        ///
        /// 1. **魔女术工匠·商标女巫**——①（解放自己）检索场地，是起手 STEP 1~2 的核心；
        ///    同时是 Combo 2 的墓地干扰件（「结晶魔术 光之泪」优先把她堆进墓地 = 那边 STEP 1）；
        /// 2. **魔女术的创造**——STEP 4 场地把它从卡组堆下去，STEP 5 用它的墓效从卡组特召杰妮；
        /// 3. **魔女工坊的慶典**——融合魔法，学童组合检索它、锻造女巫堆它；
        /// 4. **魔女的圣夜行**——STEP 2 商标女巫检索它。它自己那两条堆墓按卡文都排除同名卡，
        ///    所以放这一位不会出现"把自己堆掉"；
        /// 5. **魔女术的演示 / 小巷**——对手回合复制做干扰与代破；
        /// 6. 侍女杰妮 / 服装女巫——从卡组特召本家时的补位（STEP 6 的目标）。
        /// </summary>
        private static readonly int[] PreferredPicks =
        {
            // 教程"四种调度"第 2 条：**绝大多数情况用「玻璃」② 检索本家场地「魔女的圣夜行」直接发动**
            //（万能杰妮复制不了场地魔法），所以场地摆在最前面；其余按展开链的先后排。
            CardId.BystreetNight,
            CardId.Creation,
            CardId.Celebration,
            CardId.Unveiling,
            CardId.Bystreet,
            CardId.GenniServant,
            CardId.Haine,
            CardId.ViceMadame,
        };

        /// <summary>
        /// 融合素材：基类（`Executor.OnSelectFusionMaterial`）默认 `return null`＝"不选"，
        /// 于是**每一次融合都做不出来**——而这副牌的赚卡全在融合上（庆典/陶俑魔像/大魔女）。
        /// 这里按引擎给的候选选满 `max` 张：优先用**非关键**的魔法师族（侍女、多余下级），
        /// 把「魔女术师傅·玻璃女巫」留到最后（只有做大魔女时她才必须当素材）。
        ///
        /// ⚠⚠ 2026-10-05 复核（30 局 verbose 日志）：**这里现在进不来**——内核问融合素材时不再调
        /// `OnSelectFusionMaterial`，而是按"一次一张"发到 :meth:`OnSelectCard`
        /// （`GameAI.OnSelectCard` 先问 `Executor.OnSelectCard`，我们的通用分支抢先返回；
        /// 30 局里 `[探针] 融合素材请求` 一次都没打印，而 `[探针] 多选请求` 也是 0 次）。
        /// 所以下面注释里那两版"保护关键件"的 A/B **很可能量的是噪声**（改动根本没生效），
        /// 真正的素材选择在 :meth:`OnSelectCard` 的融合素材分支里（2026-10-05 新增：按
        /// :meth:`FeedCostScore` 挑"最舍得给"的那张）。留着这段是因为别的内核版本/其它融合路径
        /// 仍可能走这里；要再动素材策略，**先看这条探针有没有打印**。
        /// </summary>
        public override IList<ClientCard> OnSelectFusionMaterial(IList<ClientCard> cards, int min, int max)
        {
            if (_verbose)
            {
                string names = "";
                foreach (ClientCard card in cards)
                    names += (names.Length == 0 ? "" : "、") + (card == null ? "（空）" : card.Name);
                Logger.WriteLine("[探针] 融合素材请求：min=" + min + " max=" + max + " 阶段=" + Duel.Phase
                    + " 正在结算=" + (CurrentEffect() == null ? "（无）" : CurrentEffect().Name) + " 候选=" + names);
            }
            List<ClientCard> picked = new List<ClientCard>();
            // 玻璃女巫放最后（大魔女非她不可，别的融合能省则省）。
            // 试过两版"保护更多关键件"（第 2 轮保护杰妮、第 4 轮限定只在场上/手牌时保护
            // 玻璃/杰妮/玻璃女巫），外部腿分别掉到 1/20 与 6/30 → **两版都已回退**：
            // 这副牌的融合素材本来就该用"已经用过一次"的资源，缩手反而更伤。
            // 候选 ≥3 张（＝这次融合能取 3 只）时，把「魔女术师傅·玻璃女巫」也排进去——
            // 18 步教程第 12 步的「大魔女 桑德里永」非她不可（玻璃女巫 + 魔法师族 ×2）。
            // 只取 2 只的融合仍然把她省下来（老经验：能省则省，别把她喂给学童/代理）。
            bool bigFusion = max >= 3;
            foreach (ClientCard card in cards)
            {
                if (picked.Count >= max)
                    break;
                // 「玻璃女巫」两个印刷号都认（21522601 基准号 / 21522602 卡表里的号）。
                if (!bigFusion && (card.IsCode(CardId.Veil) || card.IsOriginalCode(CardId.Veil)
                    || card.IsCode(CardId.VeilAlt) || card.IsOriginalCode(CardId.VeilAlt)))
                    continue;
                picked.Add(card);
            }
            foreach (ClientCard card in cards)
            {
                if (picked.Count >= max)
                    break;
                if (!picked.Contains(card))
                    picked.Add(card);
            }
            if (picked.Count < min)
                return null;       // 凑不够就交回，避免给出非法的素材组合
            return picked;
        }
    }
}
