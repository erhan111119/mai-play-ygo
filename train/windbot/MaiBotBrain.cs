using YGOSharp.OCGWrapper.Enums;
using System;
using System.Collections.Generic;
using System.IO;
using WindBot;
using WindBot.Game;

namespace WindBot.Game.AI
{
    /// <summary>
    /// 逐步问 AI 的通道（**所有出牌脚本共用**）：把"要不要发动这张卡、有哪些选项"写进
    /// ``&lt;BrainFile&gt;.q``，等外面的 Python 侧把答复写进 ``&lt;BrainFile&gt;.a``（带同一个 id）。
    ///
    /// **为什么放在基类**：问答通道原先只做在通用执行器（PlanAware）里，于是"开 AI 就得换成通用
    /// 执行器"。实测这个代价值得警惕——同一副牌同一对手：专属脚本 13% 胜/16.0 动作，通用脚本
    /// 0%/8.7。把钩子提到基类之后，任何卡组自带的出牌脚本都能问 AI，不必再牺牲脚本。
    ///
    /// 三条规矩，都是实测教训：
    ///   * **没传 ``BrainFile=`` 就完全不起作用**（77 个自带执行器零影响）；
    ///   * 等答复有上限（``BrainTimeoutMs``，默认 40000ms——一次"会思考的模型"答复要约 13 秒，
    ///     30 秒的老上限会在负载高时把答复丢掉），超时按脚本自己的判断继续；
    ///   * **每回合问 AI 的总等待有上限**（``BrainBudgetMs``，默认 8000ms）：一个回合的问与答
    ///     都要落在内核的**单回合时限**里，问得太多会把时限吃光——实测整局一步没走、
    ///     结局是"超时"（30 秒时限、问了 30 次的组合回合）。预算用完之后这一回合照脚本打；
    ///   * 任何异常都吞掉并回退脚本——问 AI 绝不能把一局打不出来。
    /// </summary>
    public static class MaiBotBrain
    {
        private static string _path = "";
        private static bool _enabled;
        private static bool _initialized;
        private static int _seq;
        private static int _timeoutMs = 40000;
        private static int _budgetMs = 60000;
        private static int _oppBudgetMs = 15000;
        private static int _turnBudget = 60000;
        private static int _spentMs;
        private static int _budgetTurn = -1;
        private static int _maxIdlePerTurn = 8;
        private static int _idleTurn = -1;
        private static int _idleUsed;
        private static readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>这条路通不通（没传 BrainFile 就是不通）。</summary>
        public static bool Enabled
        {
            get
            {
                Init();
                return _enabled;
            }
        }

        private static void Init()
        {
            if (_initialized)
                return;
            _initialized = true;
            string configured = Config.GetString("BrainFile", "");
            if (!string.IsNullOrEmpty(configured))
            {
                _path = configured;
                _enabled = true;
            }
            _timeoutMs = Config.GetInt("BrainTimeoutMs", 40000);
            _budgetMs = Config.GetInt("BrainBudgetMs", 60000);
            _oppBudgetMs = Config.GetInt("BrainOppBudgetMs", 15000);
            _turnBudget = _budgetMs;
            _maxIdlePerTurn = Config.GetInt("BrainMaxIdlePerTurn", 8);
        }

        /// <summary>
        /// 问一次 AI；返回答复（``yes`` / ``no`` / 序号），没问到返回空串。
        /// </summary>
        public static string Ask(string kind, ClientCard card, IList<ClientCard> choices, Duel duel)
        {
            Init();
            if (!_enabled || string.IsNullOrEmpty(_path) || duel == null)
                return "";
            int budgetLeft = BudgetLeft(duel);
            if (budgetLeft <= 0)
                return "";
            _seq++;
            return AskRaw(_seq, BuildQuestion(_seq, kind, card, choices, duel), budgetLeft);
        }

        /// <summary>
        /// 问 AI「这一步做什么」：候选是当前主要阶段所有合法动作（召唤/特召/发动/盖放/进战斗/结束）。
        ///
        /// **这是"AI 打牌"的入口**：脚本层（``Executor.AddExecutor`` 那层）只能**否决**脚本自己
        /// 想做的事，所以遇到脚本不认识的卡组（新系列、投稿卡组）它一步都走不出来——实测用户反馈
        /// "脚本根本无法正常展开"就是这个。这里给的是**提议权**：AI 选哪个就执行哪个。
        ///
        /// 三个刹车：没传 ``BrainFile`` 直接返回 0；每回合最多问 ``BrainMaxIdlePerTurn`` 次
        /// （防重复问 + 防"同一个状态反复问"死循环）；每回合的总等待预算与其它问题共用
        /// （对手回合的预算更小，见 :meth:`BudgetLeft`）。
        /// </summary>
        /// <param name="labels">候选菜单（有序）。</param>
        /// <param name="hasCardOption">菜单里是否有"卡类"选项（召唤/特召/发动/盖放）；
        /// 只有进战斗/结束时不必问——那种局面没有可展开的东西，问一句只是白等一秒。</param>
        public static int AskIdleChoice(IList<string> labels, bool hasCardOption)
        {
            Init();
            if (!_enabled || string.IsNullOrEmpty(_path) || labels == null || labels.Count < 2)
                return 0;
            // 菜单里只有"进战斗/结束"时不要问：那种局面没有可做的展开，问一句只是白等一秒
            if (!hasCardOption)
                return 0;
            Duel duel = CurrentDuel();
            if (duel == null)
                return 0;
            if (duel.Turn != _idleTurn)
            {
                _idleTurn = duel.Turn;
                _idleUsed = 0;
            }
            if (_idleUsed >= _maxIdlePerTurn)
                return 0;
            int budgetLeft = BudgetLeft(duel);
            if (budgetLeft <= 0)
                return 0;

            _idleUsed++;
            _seq++;
            List<string> lines = new List<string>();
            lines.Add("id=" + _seq);
            lines.Add("kind=idle_action");
            AppendContext(lines, null, duel);
            for (int i = 0; i < labels.Count; ++i)
                lines.Add("option=" + (i + 1) + ";" + labels[i]);
            string answer = AskRaw(_seq, lines, budgetLeft);
            int pick;
            if (!int.TryParse(answer, out pick) || pick < 0 || pick > labels.Count)
                return 0;
            if (_verbose)
                Logger.WriteLine("AI 选择：第 " + pick + " 项" + (pick > 0 ? "（" + labels[pick - 1] + "）" : "（不干预）"));
            return pick;
        }

        /// <summary>本回合还剩多少等待预算（毫秒）；同时负责在换回合时清零。

        /// **对手回合的预算更小**：那时候我们在做的是"要不要响应/交坑"，一个响应窗口等太久会被内核
        /// 判成超时（实测 40 局里有 5 局是因为这个输掉的）；而自己的回合要在主要阶段一步步做展开，
        /// 需要更大的额度。
        /// </summary>
        private static int BudgetLeft(Duel duel)
        {
            if (duel.Turn != _budgetTurn)
            {
                _budgetTurn = duel.Turn;
                _spentMs = 0;
                _turnBudget = duel.Player == 0 ? _budgetMs : _oppBudgetMs;
            }
            return _turnBudget - _spentMs;
        }

        /// <summary>把问题写下去、等答复：``Ask`` 与 ``AskIdleChoice`` 共用这一段。</summary>
        private static string AskRaw(int id, List<string> lines, int budgetLeft)
        {
            int waitLimit = _timeoutMs < budgetLeft ? _timeoutMs : budgetLeft;
            string questionPath = _path + ".q";
            string answerPath = _path + ".a";
            try
            {
                File.WriteAllLines(questionPath, lines.ToArray());
                System.Diagnostics.Stopwatch watch = System.Diagnostics.Stopwatch.StartNew();
                while (watch.ElapsedMilliseconds < waitLimit)
                {
                    if (File.Exists(answerPath))
                    {
                        string answer = "";
                        int answerId = -1;
                        foreach (string line in File.ReadAllLines(answerPath))
                        {
                            int split = line.IndexOf('=');
                            if (split <= 0)
                                continue;
                            string key = line.Substring(0, split).Trim().ToLowerInvariant();
                            string value = line.Substring(split + 1).Trim();
                            if (key == "id")
                                int.TryParse(value, out answerId);
                            else if (key == "answer")
                                answer = value;
                        }
                        if (answerId == id && answer.Length > 0)
                        {
                            try { File.Delete(answerPath); } catch { }
                            _spentMs += (int)watch.ElapsedMilliseconds;
                            if (_verbose)
                                Logger.WriteLine("AI 答复（" + lines[1] + "）：" + answer);
                            return answer;
                        }
                    }
                    System.Threading.Thread.Sleep(50);
                }
                _spentMs += (int)watch.ElapsedMilliseconds;
                if (_verbose)
                    Logger.WriteLine("等 AI 答复超时（" + waitLimit + "ms，按脚本自己的判断继续）");
            }
            catch (Exception ex)
            {
                Logger.WriteLine("问 AI 出错（按脚本自己的判断继续）：" + ex.Message);
            }
            return "";
        }

        /// <summary>
        /// 装钩子：**开 AI 且这是"发动"类规则**时，给它套一层守卫；其他类型原样返回。
        ///
        /// 为什么在 ``AddExecutor`` 里做：任何出牌脚本注册规则都要经过它，所以钩子能覆盖到
        /// **所有**执行器（自带 77 个 + 投稿卡组生成的），不必要求"开 AI 就换成通用执行器"。
        /// 没传 ``BrainFile`` 时这个函数原样返回，等于什么都不做。
        /// </summary>
        public static CardExecutor MaybeWrap(CardExecutor rule)
        {
            Init();
            if (!_enabled || rule == null || rule.Type != ExecutorType.Activate)
                return rule;
            Func<bool> original = rule.Func;
            return new CardExecutor(rule.Type, rule.CardId, () => Guard(original));
        }

        /// <summary>装在执行器规则上的守卫：脚本愿意发动时问一次 AI。</summary>
        private static bool Guard(Func<bool> original)
        {
            if (!_enabled)
                return original == null || original();
            bool own = original == null || original();
            if (!own)
                return false;
            string answer = Ask("activate", _currentCard, null, CurrentDuel());
            if (answer == "no" || answer == "0")
                return false;
            return true;
        }

        /// <summary>
        /// "打谁"这一层：脚本已经决定让某只怪攻击时问一次 AI，它可以改选或取消这次攻击。
        ///
        /// 挂在这里的原因和发动那边一样——**开 AI 不该只对通用脚本生效**。GameAI.OnSelectBattle
        /// 是所有出牌脚本"打谁"的唯一出口（``Executor.OnSelectAttackTarget`` 的结果由它采用），
        /// 所以钩子放在那儿，任何执行器（含卡组专属的）都会被问一次。
        ///
        /// 没问 AI、答不上来、答案不是合法序号时返回 ``false``：**脚本的选择原样生效**。
        /// </summary>
        /// <param name="target">改选的目标；``skip`` 为 true 时无意义。</param>
        /// <param name="skip">true 表示"这只怪这次不攻击"。</param>
        public static bool TryOverrideAttackTarget(
            ClientCard attacker, IList<ClientCard> defenders, out ClientCard target, out bool skip)
        {
            target = null;
            skip = false;
            Init();
            if (!_enabled || attacker == null || defenders == null || defenders.Count == 0)
                return false;
            string answer = Ask("attack_target", attacker, defenders, CurrentDuel());
            int choice;
            if (!int.TryParse(answer, out choice) || choice < 0 || choice > defenders.Count)
                return false;
            if (choice == 0)
            {
                skip = true;
                return true;
            }
            target = defenders[choice - 1];
            return true;
        }

        // Guard 里拿不到当前的执行器实例，用两个静态引用由 Executor.SetCard 记录
        private static Executor _current;
        private static ClientCard _currentCard;

        /// <summary>把"当前正在判定的执行器与卡片"记下来（``Executor.SetCard`` 每次判定都会调）。</summary>
        public static void NoteCurrent(Executor executor, ClientCard card)
        {
            _current = executor;
            _currentCard = card;
        }

        private static Duel CurrentDuel()
        {
            return _current != null ? _current.Duel : null;
        }

        private static List<string> BuildQuestion(
            int id, string kind, ClientCard card, IList<ClientCard> choices, Duel duel)
        {
            List<string> lines = new List<string>();
            lines.Add("id=" + id);
            lines.Add("kind=" + kind);
            lines.Add("card=" + (card != null ? card.Id : 0));
            lines.Add("card_name=" + (card != null ? card.Name : ""));
            AppendContext(lines, card, duel);
            if (kind == "activate" && IsCautionCard(card))
                lines.Add("caution=1");
            if (choices != null)
            {
                List<string> ids = new List<string>();
                List<string> names = new List<string>();
                foreach (ClientCard item in choices)
                {
                    if (item == null)
                        continue;
                    ids.Add(item.Id.ToString());
                    names.Add(item.Name);
                }
                lines.Add("choice_ids=" + string.Join(",", ids.ToArray()));
                lines.Add("choice_names=" + string.Join(",", names.ToArray()));
            }
            return lines;
        }

        /// <summary>局面部分（两种问题共用）：回合、LP、双方区域、墓地/除外、连锁深度、对手已见卡。</summary>
        private static void AppendContext(List<string> lines, ClientCard card, Duel duel)
        {
            lines.Add("turn=" + duel.Turn);
            lines.Add("my_lp=" + (duel.Fields[0] != null ? duel.Fields[0].LifePoints : 0));
            lines.Add("opp_lp=" + (duel.Fields[1] != null ? duel.Fields[1].LifePoints : 0));
            AppendZone(lines, "hand", duel.Fields[0].Hand);
            AppendZone(lines, "mine", duel.Fields[0].MonsterZone);
            AppendZone(lines, "my_spell", duel.Fields[0].SpellZone);
            AppendZone(lines, "theirs", duel.Fields[1].MonsterZone);
            AppendZone(lines, "their_spell", duel.Fields[1].SpellZone);
            lines.Add("grave_mine=" + duel.Fields[0].Graveyard.Count);
            lines.Add("grave_theirs=" + duel.Fields[1].Graveyard.Count);
            // 除外区：本家的回收与"资源还有多少"都要看它（缺了这两行，问 AI 只能靠猜）
            lines.Add("banish_mine=" + duel.Fields[0].Banished.Count);
            lines.Add("banish_theirs=" + duel.Fields[1].Banished.Count);
            // 连锁深度：区分"我自己主动展开"和"我在顺着对手的效果应对"
            lines.Add("chain_depth=" + (duel.CurrentChain != null ? duel.CurrentChain.Count : 0));
            // 连锁上的卡：判断"这张坑该不该交"必须知道**对手现在在做什么**
            // （只看"我手里有灰流丽"是没法决定的）
            if (duel.CurrentChain != null && duel.CurrentChain.Count > 0)
            {
                List<string> chainLines = new List<string>();
                foreach (ClientCard item in duel.CurrentChain)
                {
                    if (item == null)
                        continue;
                    chainLines.Add("chain=" + item.Id + ";" + item.Name + ";" + item.Controller);
                }
                lines.AddRange(chainLines);
            }
            // 对手已经亮出来的卡：它用了哪些坑、露过哪些怪——判断"这张坑现在交值不值"要用
            AppendSeen(lines, "their_seen", duel.Fields[1].Graveyard);
            AppendSeen(lines, "their_seen", duel.Fields[1].Banished);
            AppendSeen(lines, "their_seen", duel.Fields[1].MonsterZone);
            AppendSeen(lines, "their_seen", duel.Fields[1].SpellZone);
            lines.Add("my_phase=" + (duel.Player == 0 ? "1" : "0"));
            lines.Add("phase=" + (int)duel.Phase);
        }

        /// <summary>
        /// 把"对手已经露出来的卡"写进问题（同名卡只写一次，最多 12 种）。
        ///
        /// 为什么要它：判断"这张坑现在交不交"靠的是"对手手里还有没有东西"，
        /// 而场上/墓地/除外区里露过的卡是唯一能看见的线索。
        /// </summary>
        private static void AppendSeen(List<string> lines, string prefix, IList<ClientCard> zone)
        {
            if (zone == null)
                return;
            List<int> seen = new List<int>();
            foreach (ClientCard item in zone)
            {
                if (item == null || seen.Contains(item.Id) || seen.Count >= 12)
                    continue;
                seen.Add(item.Id);
                lines.Add(prefix + "=" + item.Id + ";" + item.Name);
            }
        }

        /// <summary>
        /// 把一片区域写进问题：**每张卡一行**，``区域前缀=卡号;卡名;攻击力;守备力;表示形式``。
        ///
        /// 为什么每张一行、用分号分隔：卡名里可能有逗号，用逗号拼一行会被拆错——
        /// 拆错等于把"3000 攻"读成"0 攻"，AI 就会拿小怪去撞大怪（实测踩过）。
        /// </summary>
        private static void AppendZone(List<string> lines, string prefix, IList<ClientCard> zone)
        {
            if (zone == null)
                return;
            int index = 0;
            foreach (ClientCard item in zone)
            {
                index++;
                if (item == null)
                    continue;
                lines.Add(prefix + "=" + item.Id + ";" + item.Name + ";" + item.Attack + ";"
                    + item.Defense + ";" + (int)item.Position);
            }
            if (index == 0)
                lines.Add(prefix + "=（空）");
        }

        /// <summary>打法数据里列为"谨慎"的卡（``PlaybookFile=`` 的 ``never_activate``）。</summary>
        private static bool IsCautionCard(ClientCard card)
        {
            if (card == null)
                return false;
            string path = Config.GetString("PlaybookFile", "");
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return false;
            try
            {
                foreach (string line in File.ReadAllLines(path))
                {
                    int split = line.IndexOf('=');
                    if (split <= 0)
                        continue;
                    if (line.Substring(0, split).Trim().ToLowerInvariant() != "never_activate")
                        continue;
                    foreach (string item in line.Substring(split + 1).Split(','))
                    {
                        int id;
                        if (int.TryParse(item.Trim(), out id) && id == card.Id)
                            return true;
                    }
                }
            }
            catch (Exception)
            {
                return false;
            }
            return false;
        }
    }
}
