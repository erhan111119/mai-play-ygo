using System.Collections.Generic;
using WindBot.Game.AI.Enums;
using YGOSharp.OCGWrapper.Enums;

namespace WindBot.Game.AI
{
    /// <summary>
    /// **阻抗决策层的确定性部分**：阻抗卡表 + 目标过滤 + 威胁序。
    ///
    /// 三层分工（与 <see cref="EnemyTargeting"/> 同一套口径）：合法性交给内核与卡脚本、
    /// 条件与自肃由卡脚本声明、**本文件只回答"哪些候选是能选的、按确定性口径谁更该被选"**，
    /// 真正要"读卡文才判得出"的那一步才交给模型（见 <see cref="MaiBotBrain.PickDisableTarget"/>）。
    ///
    /// 为什么确定性部分必须存在、而且必须先判（2026-10-08 定的口径）：
    /// * **不能让模型去回答本来有确定答案的问题**——对着"效果已经无效过"的怪再无效一次，
    ///   答案再"合理"也是白扔一张手坑，而引入模型只会给这种零收益动作加上随机性；
    /// * 判定为零收益的场合直接不问模型 ⇒ 省等待、省 token，而"对手回合的等待预算只有
    ///   15 秒"（见 <see cref="MaiBotBrain"/> 的 `BrainOppBudgetMs`），省下来的就是能不能问下一次。
    /// </summary>
    public static class NegateDecision
    {
        /// <summary>
        /// 阻抗卡表（我方"无效/阻断对方"的手段）——**只有这一层用它**：决定"要不要问模型"。
        ///
        /// ⚠ **不要按规则类型判**（别写成"凡是 Activate 规则都问"）：那正是 2026-10-07 之前
        /// 逐步问 AI 的老口径，钩子包裹全部 `ExecutorType.Activate`，于是模型在**展开**的每一步
        /// 都插一脚，实测三次都没收益、86% 是在否决脚本自己想做对的事。收窄到"这张卡是阻抗卡"
        /// 之后，展开路径连问都不会问（结构性保证，不靠提示词里写"展开时别管"）。
        ///
        /// ⚠ **不含「增殖的G」与「欢聚友伴」**：它们不是"要不要无效对方这张"，而是"越早交抽得越多"
        /// 的资源牌，档位由 `MULCHARMY_GATE` 那个量过的实验守着（`DefaultExecutor.MulcharmyReady`），
        /// 再让模型能否决它们等于把一个已经有结论的实验重新搅开。
        /// </summary>
        public static readonly int[] NegateCardIds =
        {
            14558127,   // 灰流丽
            94145021,   // 小丑与锁鸟
            97268402,   // 效果遮蒙者
            10045474,   // 无限泡影
            73642296,   // 屋敷童
            59438930,   // 幽鬼兔
            67750322,   // 骷髅大王
            27204311,   // 原始生命态 尼比鲁
            91800273,   // 次元吸引者
            24224830,   // 墓穴的指名者
            65681983,   // 抹杀之指名者
            83326048,   // 次元障壁
            24299458,   // 禁忌的一滴
            61740673,   // 王宫的敕命
            51452091,   // 王宫的通告
            41420027,   // 神之宣告
            84749824,   // 神之警告
            40605147,   // 神之通告
        };

        /// <summary>这张是不是"无效/阻断对方"的手段（决定要不要问模型）。</summary>
        public static bool IsNegateCard(ClientCard card)
        {
            if (card == null)
                return false;
            foreach (int id in NegateCardIds)
            {
                if (card.IsOriginalCode(id))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 这张卡**能不能被"无效类效果"指向**——不行的直接排除掉，不要拿去问模型。
        ///
        /// 三条硬排除，都是"选了等于白扔一张牌"：
        /// * <see cref="ClientCard.IsDisabled"/>：效果**已经被无效过**（内核的 STATUS_DISABLED），
        ///   再无效一次换不来任何东西；
        /// * <see cref="CardExtension.IsShouldNotBeTarget"/>：不能被效果选为对象
        ///   （例如「超魔导龙骑士-真红眼龙骑士」写着"双方不能把这张卡作为效果的对象"）；
        /// * <see cref="CardExtension.IsShouldNotBeSpellTrapTarget"/>：**只在发动源是魔法/陷阱时**判
        ///   （「无限泡影」「突破技能」是陷阱，「效果遮蒙者」是怪兽怪效，对后者拿这条去卡
        ///   会误杀合法目标）；
        /// * <see cref="CardType.Normal"/>：通常怪兽没有可无效的效果。
        /// </summary>
        /// <param name="card">候选卡。</param>
        /// <param name="spellTrapSource">这张无效卡的发动源是不是魔法/陷阱。</param>
        public static bool IsDisableCandidate(ClientCard card, bool spellTrapSource)
        {
            if (card == null || card.Controller != 1)
                return false;
            if (!card.IsMonster() || !card.IsFaceup())
                return false;
            if (card.IsDisabled())
                return false;
            if (card.HasType(CardType.Normal))
                return false;
            if (card.IsShouldNotBeTarget())
                return false;
            if (spellTrapSource && card.IsShouldNotBeSpellTrapTarget())
                return false;
            return true;
        }

        /// <summary>
        /// 从一片区域里挑出可无效的候选（保持区域顺序，便于与内核候选列表对照）。
        ///
        /// 没找到就返回空表——**空表表示"没得选"，调用方要沿用脚本自己的口径，不要硬选一张**。
        /// </summary>
        public static List<ClientCard> CollectDisableCandidates(IEnumerable<ClientCard> zone, bool spellTrapSource)
        {
            List<ClientCard> candidates = new List<ClientCard>();
            if (zone == null)
                return candidates;
            foreach (ClientCard card in zone)
            {
                if (IsDisableCandidate(card, spellTrapSource))
                    candidates.Add(card);
            }
            return candidates;
        }

        /// <summary>
        /// 威胁序（**确定性**的兜底口径，也作为给模型看的参考值）。
        ///
        /// 全部复用上游已有的信号，不新造指标：
        /// 1. <see cref="CardExtension.IsFloodgate"/>：永续/场地类"贴纸"，它一直在压我们，最该先废；
        /// 2. <see cref="CardExtension.IsMonsterDangerous"/>：上游判"打它危险"的怪；
        /// 3. 攻击力高的（打点高＝威胁大）。
        ///
        /// 数值只用来排序，不要当阈值用。
        /// </summary>
        public static int ThreatRank(ClientCard card)
        {
            if (card == null)
                return int.MinValue;
            int rank = 0;
            if (card.IsFloodgate())
                rank += 100000;
            if (card.IsMonsterDangerous())
                rank += 50000;
            rank += card.Attack;
            return rank;
        }

        /// <summary>按威胁序挑一张（最大的优先）；候选为空返回 null。</summary>
        public static ClientCard PickByThreat(IList<ClientCard> candidates)
        {
            if (candidates == null || candidates.Count == 0)
                return null;
            ClientCard best = null;
            foreach (ClientCard card in candidates)
            {
                if (best == null || ThreatRank(card) > ThreatRank(best))
                    best = card;
            }
            return best;
        }

        /// <summary>日志用：卡名（读不到就给卡号）。</summary>
        public static string NameOf(ClientCard card)
        {
            if (card == null)
                return "（无）";
            return string.IsNullOrEmpty(card.Name) ? card.Id.ToString() : card.Name;
        }
    }
}
