using YGOSharp.OCGWrapper.Enums;
using System;
using System.Collections.Generic;
using System.IO;
using WindBot;
using WindBot.Game;

namespace WindBot.Game.AI
{
    /// <summary>
    /// **阻抗决策层**的问答通道（所有出牌脚本共用）：把"这一张无效卡该指向对面哪只怪"
    /// 写进 ``&lt;BrainFile&gt;.q``，等外面的 Python 侧把答复写进 ``&lt;BrainFile&gt;.a``（带同一个 id）。
    ///
    /// **范围（2026-10-08 定）**：只服务**阻抗时点**——对手回合里"要不要交这张阻抗"（`BrainNegateGate`，
    /// 默认关）与"无效哪一只"（`BrainTargetChoice`，默认开）。**展开期一步都不问**，而且是结构性保证：
    /// 钩子的入口就按"对手回合 + 这张是 <see cref="NegateDecision.IsNegateCard"/> 里的卡"卡住，
    /// 再加上旧的两个展开期钩子（闲时选动作 / 改选攻击目标）由 `BrainIdleChoice` 默认关死。
    ///
    /// **为什么必须收窄**：老口径（2026-10-07 之前）钩子包裹**全部** `ExecutorType.Activate` 规则，
    /// 于是模型在展开的每一步插一脚；三次实测都没收益，机制上 **86% 是在否决脚本本来想做对的事**——
    /// 模型看得见场面，看不见脚本的整套计划。而实测 8 局真实对局后，"对手回合里我方发动的连锁"
    /// 只有 **3~21 次/局（平均 10.8）**，收窄后一局十次左右的问答，延迟才谈得上可控。
    ///
    /// **为什么放在基类**：问答通道原先只做在通用执行器（PlanAware）里，于是"开 AI 就得换成通用
    /// 执行器"。实测这个代价值得警惕——同一副牌同一对手：专属脚本 13% 胜/16.0 动作，通用脚本
    /// 0%/8.7。把钩子提到基类之后，任何卡组自带的出牌脚本都能问 AI，不必再牺牲脚本。
    ///
    /// 四条规矩，都是实测教训：
    ///   * **没传 ``BrainFile=`` 就完全不起作用**（77 个自带执行器零影响）；
    ///   * 等答复有上限（``BrainTimeoutMs``），超时按脚本自己的判断继续；**连续 3 次没拿到答复就
    ///     熔断本局的问答**（模型不可用时继续等只会把内核时限吃光）；
    ///   * **每回合问 AI 的总等待有上限**（``BrainBudgetMs`` 自己回合 / ``BrainOppBudgetMs``
    ///     对手回合）：一个回合的问与答都要落在内核的**单回合时限**里，问得太多会把时限吃光——
    ///     实测整局一步没走、结局是"超时"，也实测过 40 局里 5 局因响应窗口等太久直接输掉；
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

        /// <summary>
        /// 「无效哪一只」（目标选择）开关，配置 `BrainTargetChoice`（默认开）。
        ///
        /// 这是决策层里**风险最低的一半**：它只在"脚本本来就要交这张无效卡"的前提下改目标，
        /// 不会让脚本少交一张牌；而且模型在这里有真信息优势——脚本只认卡号与几张稀疏的静态表
        /// （`ShouldNotBeTarget` 六十行、`ShouldNotBeSpellTrapTarget` 只有六条），模型认识卡文。
        /// </summary>
        private static bool _targetChoiceEnabled = true;

        /// <summary>
        /// 「要不要交这张阻抗」开关，配置 `BrainNegateGate`（**默认关**）。
        ///
        /// 为什么默认关：这一半就是 2026-10-07 之前逐决策问 AI 的老口径，实测三次都没收益，
        /// 机制是 **86% 在否决脚本本来想做的事**——模型看得见场面，看不见脚本的整套计划
        /// （"我这回合准备做什么、手里还剩几张阻抗、这套牌的资源循环转到哪一步了"）。
        /// 按项目纪律：策略类改动先做成开关，跑 ≥80 局/腿的镜像 A/B 之后再谈默认值。
        /// </summary>
        private static bool _negateGateEnabled;

        /// <summary>
        /// 连续超时次数；连续 3 次就把这一整套问答**本局关掉**（一个 WindBot 进程一局）。
        ///
        /// 为什么要有熔断：对手回合的等待预算只有 15 秒（`BrainOppBudgetMs`），而当年
        /// 40 局里有 5 局是"响应窗口等太久"直接被内核判超时输掉的。模型不可用时每次
        /// 白等 2.5 秒、一回合两三次就把预算吃光——**这时候按脚本打，而不是继续等**。
        /// 熔断只影响本局，不写盘、不带进下一局。
        /// </summary>
        private static int _consecutiveTimeouts;
        private static bool _tripped;
        private const int MaxConsecutiveTimeouts = 3;

        /// <summary>
        /// 旧的两个"展开期"钩子（闲时选动作 / 改选攻击目标）的开关，配置 `BrainIdleChoice`，
        /// **默认关**。
        ///
        /// ⚠ 为什么必须显式关掉、而不是"反正不会调"：这两个钩子**只判 `_enabled`**，而 `_enabled`
        /// 由"传没传 `BrainFile=`"决定。也就是说只要开了决策层，它们就一起活过来——而它们问的正是
        /// **展开期的每一步**（"主要阶段这一步做什么""打哪只"），恰好是 2026-10-07 那次
        /// 三次实测没收益、86% 否决的老口径。决策层的范围是"阻抗时点"，不能顺手把展开也交出去。
        /// </summary>
        private static bool _idleChoiceEnabled;

        /// <summary>超时熔断是否已触发（供日志查询）。</summary>
        public static bool Tripped
        {
            get { return _tripped; }
        }

        /// <summary>`AskChainChoice` 的"我不发表意见，交给脚本"返回值。</summary>
        public static readonly int NoOpinion = int.MinValue;

        /// <summary>这一卡号是不是被决策层在**当前这次链上提问**里否决了。</summary>
        private static readonly List<int> _chainVetoedIds = new List<int>();

        /// <summary>
        /// 是否正处在一次链上提问的裁决过程中。
        ///
        /// ⚠ 这个开关是修出来的：第一版只靠 `_chainVetoedIds` 的内容来判断，而那个表**只在
        /// `AskChainChoice` 里清空**——可是 `ShouldExecute` 在别的提问里（例如"这张效果要不要发动"）
        /// 也会被调用，于是上一次链上的否决残留下来，把后面的卡全挡掉了。
        /// 现象很隐蔽：真机一局的提问数从 37 次掉到 2 次（`Guard` 根本跑不到），决策层看起来"更省事"，
        /// 实际是被自己的残留状态掐住了。所以否决**必须绑定在"当前这一次提问"上**。
        /// </summary>
        private static bool _inChainPrompt;

        /// <summary>`GameAI.OnSelectChain` 进入时调：清空否决表并打开开关。</summary>
        public static void BeginChainPrompt()
        {
            _chainVetoedIds.Clear();
            _inChainPrompt = true;
        }

        /// <summary>`GameAI.OnSelectChain` 退出时调（含提前 return，用 finally 保证）。</summary>
        public static void EndChainPrompt()
        {
            _inChainPrompt = false;
        }

        /// <summary>这张卡是不是被决策层在这一问里否决了（`GameAI.ShouldExecute` 每张都会问一次）。</summary>
        public static bool IsChainVetoed(ClientCard card)
        {
            if (!_inChainPrompt || _chainVetoedIds.Count == 0 || card == null)
                return false;
            return _chainVetoedIds.Contains(card.Id);
        }

        /// <summary>
        /// **对面发动效果带来的时点**：问模型"我方这几张里发哪张 / 都不发"。
        ///
        /// 这是决策层的第二问（第一问是"要不要交"，第三问是"该针对谁"）。**为什么不复用 `Guard`**：
        /// `Guard` 是挂在**单张卡的规则**上的，那时脚本已经决定"这张要发"了，模型只能否决它；
        /// 而"我手上同时有灰流丽和无限泡影，该交哪张"是个**选择题**——只有站在
        /// `OnSelectChain`（所有候选卡与规则都摆在这里）这一层才问得出来。
        ///
        /// 调用时机与代价：
        /// * **对面必须是链上最后那个发动者**（`LastChainPlayer == 1`）。⚠ 这里**不再限定"对手回合"**——
        ///   对面在我方回合丢手坑（灰流丽/无限泡影都是）同样是"对面发动效果带来的时点"，
        ///   2026-10-08 之前把它漏了；
        /// * 我方候选里**至少两张是阻抗卡**才问——一张就没得挑，那一张由 `Guard` 负责；
        /// * 问不到（超时/熔断/没开闸门）一律返回 <see cref="NoOpinion"/>，脚本照旧。
        /// </summary>
        /// <returns>
        /// 要发动的那张在 `cards` 里的下标；<see cref="NoOpinion"/> 表示"没意见/不该问"；
        /// -1 表示"都不发"（调用方据此把这几张阻抗卡记进否决表，再交回脚本）。
        /// </returns>
        public static int AskChainChoice(IList<ClientCard> cards, IList<int> descs, Duel duel)
        {
            Init();
            if (!_enabled || _tripped || !_negateGateEnabled || duel == null || cards == null)
                return NoOpinion;
            // 时点：对面是链上最后一个发动者（`-1` = 没有连锁，那属于召唤响应窗口，由别的钩子管）
            if (duel.LastChainPlayer != 1)
                return NoOpinion;
            // 我方能交的阻抗卡：从内核给的候选里筛（它已经保证这些卡现在**真的能发动**）
            List<int> negateIndexes = new List<int>();
            for (int i = 0; i < cards.Count; ++i)
            {
                if (NegateDecision.IsNegateCard(cards[i]))
                    negateIndexes.Add(i);
            }
            if (negateIndexes.Count < 2)
            {
                if (_verbose && negateIndexes.Count == 1)
                {
                    Logger.WriteLine("[决策] 链上只有 1 张阻抗可交（" + NegateDecision.NameOf(cards[negateIndexes[0]])
                        + "），改由单张闸门那问处理；候选共 " + cards.Count + " 张");
                }
                return NoOpinion;
            }
            int budgetLeft = BudgetLeft(duel);
            if (budgetLeft <= 0)
                return NoOpinion;

            _seq++;
            string answer = AskRaw(
                _seq, BuildChainQuestion(_seq, cards, descs, negateIndexes, duel), budgetLeft);
            if (string.IsNullOrEmpty(answer))
                return NoOpinion;
            int split = answer.IndexOf(';');
            string verdict = (split >= 0 ? answer.Substring(0, split) : answer).Trim().ToLowerInvariant();
            string reason = split >= 0 ? answer.Substring(split + 1).Trim() : "";
            if (verdict == "no" || verdict == "0")
            {
                foreach (int index in negateIndexes)
                    _chainVetoedIds.Add(cards[index].Id);
                Logger.WriteLine("[决策] 对面「" + NameOfLastChain(duel) + "」这一步都不交（"
                    + negateIndexes.Count + " 张可选）"
                    + (reason.Length > 0 ? "：理由＝" + reason : "（模型没给理由）"));
                return -1;
            }
            int choice;
            if (!int.TryParse(verdict, out choice))
                return NoOpinion;
            int picked = negateIndexes.IndexOf(choice - 1);
            if (picked < 0)
                return NoOpinion;
            int cardIndex = negateIndexes[picked];
            // 其余的阻抗卡在这一问里也剔掉：模型已经在"发哪张"上做了选择，脚本不该再拿别的顶上来
            foreach (int index in negateIndexes)
            {
                if (index != cardIndex)
                    _chainVetoedIds.Add(cards[index].Id);
            }
            Logger.WriteLine("[决策] 交「" + NegateDecision.NameOf(cards[cardIndex]) + "」"
                + (reason.Length > 0 ? "：理由＝" + reason : ""));
            return cardIndex;
        }

        /// <summary>
        /// 链上最后一张卡的名字（只为日志）。**不能用 `Executor.Util`**：那是每个执行器实例自己持有的，
        /// 静态类拿不到（编译期就报"上下文中不存在 Util"）。
        /// </summary>
        private static string NameOfLastChain(Duel duel)
        {
            if (duel == null || duel.CurrentChain == null || duel.CurrentChain.Count == 0)
                return "（未知）";
            return NegateDecision.NameOf(duel.CurrentChain[duel.CurrentChain.Count - 1]);
        }

        /// <summary>
        /// 链上选择问题的正文：**我方候选逐张列出（带卡文由 Python 侧补）+ 对面刚才做了什么**。
        /// </summary>
        private static List<string> BuildChainQuestion(
            int id, IList<ClientCard> cards, IList<int> descs, List<int> negateIndexes, Duel duel)
        {
            List<string> lines = new List<string>();
            lines.Add("id=" + id);
            lines.Add("kind=chain_choice");
            lines.Add("turn=" + duel.Turn);
            lines.Add("my_phase=" + (duel.Player == 0 ? "1" : "0"));
            lines.Add("phase=" + (int)duel.Phase);
            lines.Add("my_lp=" + (duel.Fields[0] != null ? duel.Fields[0].LifePoints : 0));
            lines.Add("opp_lp=" + (duel.Fields[1] != null ? duel.Fields[1].LifePoints : 0));
            // 对面刚才发动的是什么（模型判断"要不要拦"的第一依据）
            if (duel.CurrentChain != null && duel.CurrentChain.Count > 0)
            {
                foreach (ClientCard item in duel.CurrentChain)
                {
                    if (item != null)
                        lines.Add("chain=" + item.Id + ";" + item.Name + ";" + item.Controller);
                }
            }
            AppendZone(lines, "mine", duel.Fields[0] != null ? duel.Fields[0].MonsterZone : null);
            AppendZone(lines, "my_spell", duel.Fields[0] != null ? duel.Fields[0].SpellZone : null);
            AppendZone(lines, "theirs", duel.Fields[1] != null ? duel.Fields[1].MonsterZone : null);
            AppendZone(lines, "their_spell", duel.Fields[1] != null ? duel.Fields[1].SpellZone : null);
            // 我方能交的这几张：**只列阻抗卡**（非阻抗的照旧交脚本自己排），带内核给的发动描述
            foreach (int index in negateIndexes)
            {
                ClientCard card = cards[index];
                int desc = (descs != null && index < descs.Count) ? descs[index] : -1;
                lines.Add("option=" + (index + 1) + ";" + card.Id + ";" + card.Name + ";"
                    + card.Attack + ";" + card.Defense + ";desc=" + desc);
            }
            return lines;
        }

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
            _targetChoiceEnabled = Config.GetBool("BrainTargetChoice", true);
            _negateGateEnabled = Config.GetBool("BrainNegateGate", false);
            _idleChoiceEnabled = Config.GetBool("BrainIdleChoice", false);

            // 把"决策层这一局到底开没开、开了哪几半"打出来。**这行是排查"改了配置没反应"
            // 的第一现场**：`BrainFile=` 一旦没传进来，整套问答就是静默空转（连错误都没有），
            // 从外面看只会觉得"又变笨了"。所以这行无条件打，不放在 _verbose 里。
            Logger.WriteLine("阻抗决策层：" + (_enabled
                ? "开（前缀 " + _path + "）"
                : "关（没收到 BrainFile= 参数）")
                + "；无效目标 " + (_targetChoiceEnabled ? "问模型" : "按脚本")
                + "；交不交 " + (_negateGateEnabled ? "问模型" : "按脚本")
                + "；等答复上限 " + _timeoutMs + "ms");
        }

        /// <summary>
        /// 问一次 AI；返回答复（``yes`` / ``no`` / 序号），没问到返回空串。
        /// </summary>
        public static string Ask(string kind, ClientCard card, IList<ClientCard> choices, Duel duel)
        {
            Init();
            if (!_enabled || _tripped || string.IsNullOrEmpty(_path) || duel == null)
                return "";
            if (kind == "activate")
                kind = "negate_gate";
            int budgetLeft = BudgetLeft(duel);
            if (budgetLeft <= 0)
                return "";
            _seq++;
            return AskRaw(_seq, BuildQuestion(_seq, kind, card, choices, duel), budgetLeft);
        }

        /// <summary>
        /// 「这一张无效卡该指向对面哪只怪」——决策层的目标问题（配置 `BrainTargetChoice`，默认开）。
        ///
        /// 调用点：`DefaultExecutor.DefaultGetDisableMonsterTarget` 的**启发式兜底那一支**
        /// （`Duel.Player == 1` 时按"会解放/除外自己去发效果"的静态名单猜一只）。那一支是这个方法里
        /// **唯一有选择权**的地方——"刚发效果的那只"是链上确定的（2026-10-08 修过，不在这里动），
        /// 战斗相关的两只（`EaterOfMillions` / `NumberS39`）是特定时点，也不是选择题。
        ///
        /// 三道闸都是"省延迟"，不是策略：
        /// * 候选少于 2 张就没得选，直接不问（一次问答 0.6~2.5 秒，白问不如不问）；
        /// * 只在对手回合问（`Duel.Player == 1`）——自己回合的无效系目标不是"该无效谁"的问题；
        /// * 超时熔断后不再问。
        ///
        /// 返回 null 表示"没问到 / 不该问"，调用方**必须沿用脚本自己的口径**（不做兜底选择）。
        /// </summary>
        /// <param name="candidates">已按确定性口径过滤过的候选（见 <see cref="NegateDecision.CollectDisableCandidates"/>）。</param>
        /// <param name="sourceCard">正在判定的这张无效卡（用来判定魔法/陷阱源，也写进问题）。</param>
        /// <param name="duel">当前对局。</param>
        public static ClientCard PickDisableTarget(
            IList<ClientCard> candidates, ClientCard sourceCard, Duel duel)
        {
            Init();
            if (!_enabled || _tripped || !_targetChoiceEnabled || duel == null)
                return null;
            if (candidates == null || candidates.Count < 2)
                return null;
            // 时机：要么正在响应对面的发动（我方回合对面丢手坑也算），要么就是对面的回合。
            // ⚠ 原来只判 `duel.Player == 1`，把我方回合里响应对面手坑的时点漏掉了。
            if (duel.LastChainPlayer != 1 && duel.Player != 1)
                return null;
            int budgetLeft = BudgetLeft(duel);
            if (budgetLeft <= 0)
                return null;

            _seq++;
            string answer = AskRaw(
                _seq, BuildDisableQuestion(_seq, candidates, sourceCard, duel), budgetLeft);
            int choice;
            if (!int.TryParse(answer, out choice) || choice < 1 || choice > candidates.Count)
                return null;
            ClientCard picked = candidates[choice - 1];
            if (picked == null)
                return null;
            // 日志不在这里打：调用方（DefaultExecutor）同时知道"脚本原本会选哪只"，
            // 那才是复盘时要看的对照；这里再打一行就是同一件事说两遍。
            return picked;
        }

        /// <summary>
        /// 目标问题的正文：**只写判断"该无效谁"必须的东西**。
        ///
        /// 为什么不复用 <see cref="AppendContext"/>：那是给"这张卡要不要发动"写的，会把双方全场
        /// 每张卡 + 对手已露过的 12 种都塞进去。目标问题只需要"这张无效卡 + 这几只候选 + 连锁上
        /// 在发生什么"，输入越短答复越快——而对手回合的等待是要挤进内核时限的。
        /// </summary>
        private static List<string> BuildDisableQuestion(
            int id, IList<ClientCard> candidates, ClientCard sourceCard, Duel duel)
        {
            List<string> lines = new List<string>();
            lines.Add("id=" + id);
            lines.Add("kind=disable_target");
            lines.Add("source=" + (sourceCard != null ? sourceCard.Id : 0) + ";"
                + (sourceCard != null ? sourceCard.Name : ""));
            lines.Add("turn=" + duel.Turn);
            lines.Add("my_phase=" + (duel.Player == 0 ? "1" : "0"));
            lines.Add("phase=" + (int)duel.Phase);
            lines.Add("my_lp=" + (duel.Fields[0] != null ? duel.Fields[0].LifePoints : 0));
            lines.Add("opp_lp=" + (duel.Fields[1] != null ? duel.Fields[1].LifePoints : 0));
            // 我方能打出的伤害（判断"现在要不要留牌防守"最直接的量）
            int damage = 0;
            if (duel.Fields[0] != null)
            {
                foreach (ClientCard card in duel.Fields[0].MonsterZone)
                {
                    if (card != null && card.IsFaceup() && card.IsAttack())
                        damage += card.GetAttackPower();
                }
            }
            lines.Add("my_damage=" + damage);
            AppendSlimZone(lines, "mine", duel.Fields[0] != null ? duel.Fields[0].MonsterZone : null);
            AppendSlimZone(lines, "my_spell", duel.Fields[0] != null ? duel.Fields[0].SpellZone : null);
            AppendSlimZone(lines, "their_spell", duel.Fields[1] != null ? duel.Fields[1].SpellZone : null);
            if (duel.CurrentChain != null)
            {
                foreach (ClientCard item in duel.CurrentChain)
                {
                    if (item != null)
                        lines.Add("chain=" + item.Id + ";" + item.Name + ";" + item.Controller);
                }
            }
            for (int i = 0; i < candidates.Count; ++i)
            {
                ClientCard card = candidates[i];
                lines.Add("option=" + (i + 1) + ";" + card.Id + ";" + card.Name + ";"
                    + card.Attack + ";" + card.Defense + ";"
                    + NegateDecision.ThreatRank(card));
            }
            return lines;
        }

        /// <summary>精简区域：只写卡号/卡名/攻守，不写表示形式（目标问题用不上）。</summary>
        private static void AppendSlimZone(List<string> lines, string prefix, IList<ClientCard> zone)
        {
            if (zone == null)
                return;
            int count = 0;
            foreach (ClientCard item in zone)
            {
                if (item == null)
                    continue;
                count++;
                lines.Add(prefix + "=" + item.Id + ";" + item.Name + ";" + item.Attack + ";" + item.Defense);
            }
            if (count == 0)
                lines.Add(prefix + "=（空）");
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
            if (!_enabled || _tripped || !_idleChoiceEnabled
                || string.IsNullOrEmpty(_path) || labels == null || labels.Count < 2)
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
                            _consecutiveTimeouts = 0;
                            if (_verbose)
                                Logger.WriteLine("AI 答复（" + lines[1] + "）：" + answer);
                            return answer;
                        }
                    }
                    System.Threading.Thread.Sleep(50);
                }
                _spentMs += (int)watch.ElapsedMilliseconds;
                NoteMiss("等 AI 答复超时（" + waitLimit + "ms，按脚本自己的判断继续）");
            }
            catch (Exception ex)
            {
                NoteMiss("问 AI 出错（按脚本自己的判断继续）：" + ex.Message);
            }
            return "";
        }

        /// <summary>
        /// 记一次"没拿到答复"；连续 <see cref="MaxConsecutiveTimeouts"/> 次就熔断本局的问答。
        ///
        /// 熔断时**必须打一行日志**：静默地变成"一直按脚本打"会让人以为决策层在工作，
        /// 而这正是当年排查"改了配置没反应"最费时间的地方。
        /// </summary>
        private static void NoteMiss(string reason)
        {
            Logger.WriteLine(reason);
            _consecutiveTimeouts++;
            if (_tripped || _consecutiveTimeouts < MaxConsecutiveTimeouts)
                return;
            _tripped = true;
            Logger.WriteLine("AI 决策层连续 " + _consecutiveTimeouts + " 次没拿到答复，"
                + "本局不再问 AI（按出牌脚本打；检查网关是否可达 / 换更快的模型）");
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

        /// <summary>
        /// 装在执行器规则上的守卫：**只在"对手回合 + 这张卡是阻抗卡"时**问一次 AI 要不要交。
        ///
        /// ⚠ 2026-10-08 收窄过（见 <see cref="NegateDecision.IsNegateCard"/>）：原来这里对
        /// **所有** `ExecutorType.Activate` 规则都问，等于在展开的每一步插一脚，实测三次没收益、
        /// 86% 是否决。收窄之后"展开时不启用决策层"是**结构性保证**——自己回合、或者不是阻抗卡，
        /// 连问答都不会发起。`BrainNegateGate` 默认关（这一半要先跑镜像 A/B）。
        /// </summary>
        private static bool Guard(Func<bool> original)
        {
            if (!_enabled || _tripped || !_negateGateEnabled)
                return original == null || original();
            bool own = original == null || original();
            if (!own)
                return false;
            Duel duel = CurrentDuel();
            // 时点（**并集**，只扩大不缩小）：
            //   * 对面是链上最后那个发动者（`LastChainPlayer == 1`）——"对面发动效果带来的时点"，
            //     包括**我方回合**对面丢手坑（灰流丽/无限泡影都是）。2026-10-08 之前只判"对手回合"，
            //     把这种情况漏了；
            //   * 或者就是**对面的回合**（`Player == 1`）——实测这些时点里 `LastChainPlayer` 常常是 **-1**
            //     （对面召唤/独立窗口，没有连锁），而脚本在这时照样会主动交阻抗（通用脚本的
            //     `DefaultDontChainMyself` 对 -1 也返回 yes）。⚠ 我一度把判据**收窄**成只看
            //     `LastChainPlayer == 1`，结果真机一局的提问数从 37 掉到 1——这类"静默少问"是
            //     决策层最难查的故障，判据只能往宽了写、再靠后面的开关分层。
            bool atOpponentMoment = duel != null && (duel.LastChainPlayer == 1 || duel.Player == 1);
            if (duel == null || !atOpponentMoment || !NegateDecision.IsNegateCard(_currentCard))
            {
                // 只在 Debug 下记一行"为什么没问"——**且只记"这张是阻抗卡、但时点不成立"那一种**。
                // 最初是无条件记，结果一局打了两百多行、99% 是"判定卡=某张普通卡（是阻抗卡=False）"，
                // 把真正要看的那几行淹了（2026-10-08 真人对局日志里就是这个现象）。
                if (_verbose && NegateDecision.IsNegateCard(_currentCard))
                {
                    Logger.WriteLine("[决策] 未问单张闸门：LastChainPlayer="
                        + (duel == null ? -99 : duel.LastChainPlayer)
                        + "｜Player=" + (duel == null ? -99 : duel.Player)
                        + "｜判定卡=" + NegateDecision.NameOf(_currentCard));
                }
                return true;
            }
            string answer = Ask("negate_gate", _currentCard, null, duel);
            if (answer == null)
                return true;
            int split = answer.IndexOf(';');
            string verdict = (split >= 0 ? answer.Substring(0, split) : answer).Trim().ToLowerInvariant();
            if (verdict != "no" && verdict != "0")
                return true;
            string reason = split >= 0 ? answer.Substring(split + 1).Trim() : "";
            Logger.WriteLine("[决策] 不交「" + NegateDecision.NameOf(_currentCard) + "」"
                + (reason.Length > 0 ? "：理由＝" + reason : "（模型没给理由）"));
            return false;
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
            if (!_enabled || _tripped || !_idleChoiceEnabled
                || attacker == null || defenders == null || defenders.Count == 0)
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
            // 没有连锁时，这个窗口多半是**对手召唤**引出来的：把最近一次召唤写出来。
            // 少了它模型只能从场面猜"到底是什么事件触发了我这次响应"——实测它猜"对手只是通常召唤"
            // 并猜对了，但那是猜；而"要不要交阻抗"恰恰取决于触发它的是什么。
            // 只在本阶段确实发生过召唤时才写（`LastSummonPlayer` 在阶段开始/连锁开始会被重置成 -1），
            // 这样既新鲜又与"无连锁"这个前提自洽。
            // ⚠ 实测局限（2026-10-08 真机一局 37 次提问）：**一次都没触发**——因为内核在"连锁开始"
            // 就会把它重置成 -1，而大部分"无连锁窗口"其实是**连锁刚结算完**之后。所以它只在
            // "对手召唤、且还没开过连锁"这个窄窗口里有值，**不要当成可靠信号**；留着是因为那种窗口
            // 恰好是"模型只能猜"的场合，多一行不亏。
            else if (duel.LastSummonPlayer != -1 && duel.LastSummonedCards != null)
            {
                lines.Add("last_summon_player=" + duel.LastSummonPlayer);
                foreach (ClientCard item in duel.LastSummonedCards)
                {
                    if (item != null)
                        lines.Add("last_summon=" + item.Id + ";" + item.Name);
                }
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
        ///
        /// ⚠ 空区域**必须写一行 ``=（空）``**：这里的 ``cards`` 数的是**真卡张数**，
        /// 不是数组长度。2026-10-08 修过一次——原来数的是"遍历了多少格"，而怪兽区/魔陷区是
        /// WindBot 的**定长数组**（7 / 8 格，空位是 null），于是"我方空场"这种局面**一行都不写**。
        /// 后果是模型分不清"我方场上没怪"和"这一项没发过来"：真机抓到的 4 份问题里
        /// `mine=` 一次都没出现过，而那种局面下"我要不要留牌防守"完全要看我方是不是空场。
        /// （`hand` 是 List、空的时候原来就对，所以这个 bug 只影响定长的那几个区域。）
        /// </summary>
        private static void AppendZone(List<string> lines, string prefix, IList<ClientCard> zone)
        {
            if (zone == null)
                return;
            int cards = 0;
            foreach (ClientCard item in zone)
            {
                if (item == null)
                    continue;
                cards++;
                lines.Add(prefix + "=" + item.Id + ";" + item.Name + ";" + item.Attack + ";"
                    + item.Defense + ";" + (int)item.Position);
            }
            if (cards == 0)
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
