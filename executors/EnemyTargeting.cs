using System.Collections.Generic;
using WindBot.Game.AI.Enums;

namespace WindBot.Game.AI
{
    /// <summary>
    /// 通用层的**选目标**判据：这些提问每副牌都会遇到，所以不写在某一副执行器里
    /// （2026-10-08 群友反馈："效果点不对对象"、"他上来炸我黑森林"是通用问题，不只某一副牌）。
    ///
    /// 背景：上游对"该选哪张卡"的兜底是**按内核给的候选顺序取**——`DoEverythingExecutor.OnSelectCard`
    /// 直接取候选尾部、`GameAI` 的最后兜底取前 N 张。于是「落胤与圣女」的"以场上 1 张表侧卡为对象，
    /// 那张卡破坏"炸了对面刚用「黑魔导的幕帘」拉出来的「黑森林的魔女」：`c78010363.lua` 写着
    /// "这张卡从场上送去墓地的场合发动。从卡组把 1 只守备力 1500 以下的怪兽加入手卡"，
    /// 我们付了额外卡组 1 只「阿不思」名怪兽当费用、对面还白拿一次检索（群友原话"他上来炸我黑森林"）。
    ///
    /// 这里只按**卡文**判"该选谁"，不猜战术优先级（三层分工见各执行器文件头）：
    /// 合法性内核判、条件与自肃卡脚本声明、本文件只回答"同一批候选里挑哪张"。
    /// </summary>
    public static class EnemyTargeting
    {
        /// <summary>
        /// 「弄掉它对**对面**反而是帮忙」的怪：从场上离开/送去墓地/被破坏时**自己**触发收益的。
        /// 每条都要有卡文出处，别凭印象加——这张表是通用层的，影响所有卡组。
        /// * 78010363「黑森林的魔女」（`c78010363.lua`）："这张卡从场上送去墓地的场合发动。
        ///   从卡组把 1 只守备力 1500 以下的怪兽加入手卡"。2026-10-08 群友实测：
        ///   我们的「落胤与圣女」炸掉对面刚拉出来的这张，等于白送对面一次检索。
        /// </summary>
        public static readonly int[] RemovalBackfireIds =
        {
            78010363,   // 黑森林的魔女
        };

        /// <summary>上面那张表里的卡（判"弄掉它会不会帮对面"）。</summary>
        public static bool IsRemovalBackfire(ClientCard card)
        {
            foreach (int id in RemovalBackfireIds)
            {
                if (card.IsOriginalCode(id))
                    return true;
            }
            return false;
        }

        /// <summary>这个提示是不是"把对面上面的卡弄掉一张"（破坏/除外/弹回手牌/洗回卡组）。</summary>
        public static bool IsRemovalHint(int hint)
        {
            return hint == HintMsg.Destroy || hint == HintMsg.Remove
                || hint == HintMsg.ReturnToHand || hint == HintMsg.ToDeck;
        }

        /// <summary>
        /// "把对面场上的卡弄掉一张"时该挑哪张（候选里**同时有双方的卡**也只会挑对面的）。
        ///
        /// 挑法（按卡文）：
        /// 1. **排除** <see cref="RemovalBackfireIds"/> 里的怪（弄掉它＝帮对面）；
        /// 2. 破坏类**排除**弄不动的目标：`IsMonsterInvincible()`（不会被战斗/效果破坏，来自 WindBot 的
        ///    卡表登记）与 `IsShouldNotBeTarget()`（不能被效果选为对象，例如对面的
        ///    「超魔导龙骑士-真红眼龙骑士」写着"不会被效果破坏，双方不能把这张卡作为效果的对象"
        ///    ——对着它发动就是白费一张牌）；
        /// 3. **先挑对面的表侧魔陷**（场地/永续/装备＝对面的引擎件，也是本方最怕的贴纸），
        ///    再按攻击力挑怪（打点高＝威胁大）；
        /// 4. 一张都不合适就返回 null，让调用方沿用上游兜底（不硬选）。
        /// </summary>
        public static IList<ClientCard> PickEnemyRemovalTarget(IList<ClientCard> cards, int hint, int max)
        {
            ClientCard bestSpell = null;
            ClientCard bestMonster = null;
            foreach (ClientCard card in cards)
            {
                if (card == null || card.Controller != 1)
                    continue;
                // 破坏类：动不了的目标不选（不是"破坏"的移除（除外/弹回/洗回）不受这两条限制）
                if (hint == HintMsg.Destroy
                    && (card.IsMonsterInvincible() || card.IsShouldNotBeTarget()))
                    continue;
                if (card.IsMonster() && IsRemovalBackfire(card))
                    continue;
                if (!card.IsMonster())
                {
                    if (card.IsFaceup() && bestSpell == null)
                        bestSpell = card;
                    continue;
                }
                if (bestMonster == null || card.Attack > bestMonster.Attack)
                    bestMonster = card;
            }

            List<ClientCard> picked = new List<ClientCard>();
            if (bestSpell != null && max >= 1)
                picked.Add(bestSpell);
            if (picked.Count < max && bestMonster != null)
                picked.Add(bestMonster);
            if (picked.Count == 0)
                return null;

            // 只在这条规则**推翻了朴素挑法**（内核候选里第一张对面的卡）时打一行日志：
            // 下一局对局实录里出现 `[目标]` 就说明它生效了，也不用担心刷屏。
            ClientCard naive = null;
            foreach (ClientCard card in cards)
            {
                if (card != null && card.Controller == 1)
                {
                    naive = card;
                    break;
                }
            }
            if (naive != picked[0])
            {
                Logger.WriteLine("[目标] 弄掉对面的卡：跳过「" + NameOf(naive) + "」，改选「"
                    + NameOf(picked[0]) + "」（候选 " + cards.Count + " 张）");
            }
            return picked;
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
