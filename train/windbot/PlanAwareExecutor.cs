using YGOSharp.OCGWrapper.Enums;
using System;
using System.Collections.Generic;
using System.IO;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    /// <summary>
    /// 计划感知执行器：先按 WindBot 自带的通用打法走（继承 DoEverythingExecutor，也就是它那套
    /// "看着场面做点合理的事"），再按"作战计划"微调几处决策。
    ///
    /// 改动的三处：
    ///   * 是否发动（沿用继承来的通用规则，但套一层"留手坑"的判断）；
    ///   * 攻击手与攻击目标（aggression / prefer_direct）；
    ///   * 表示形式（aggression 高时倾向攻击表示）。
    ///
    /// 逐步问 AI 那层**不在这里**：它挂在基类的 ``Executor.AddExecutor`` 上（见 Game/AI/MaiBotBrain.cs），
    /// 所以任何出牌脚本（含各卡组的专属执行器）都能问 AI，不必为了开 AI 换成这个通用执行器。
    /// 本文件只在"攻击目标"这一处直接调 ``MaiBotBrain.Ask("attack_target", ...)``。
    ///
    /// 计划来自工作目录下的 MaiBotPlan.txt（key=value，见插件里的 train/plan.py）。
    /// 文件不存在、读不动、字段缺失，一律当"没有计划"，也就是纯通用打法——
    /// 读不到计划只会让麦麦打得"普通"，不会把这一局打歪。
    ///
    /// 为什么是这个思路：WindBot 的棋力改不动，但它的"脾气"可以调；而"该抢血还是该保场、
    /// 这张手坑现在交还是留着"恰好是模型擅长判断、而固定脚本判断不好的事。
    ///
    /// 这份文件同时被复制在插件仓库的 train/windbot/ 下留档，改动请两处同步。
    ///
    /// **部署时必须和比赛用的 exe 对上**：出牌脚本是编译进 exe 的（WindBot 用反射扫
    /// [Deck(...)] 注册），而 WindBot 找不到 ``Deck=`` 请求的名字时不报错，会随机挑一个
    /// Normal 档执行器顶上（Game/AI/DecksManager.cs）。拿原版 exe 跑 ``Deck=PlanAware``
    /// 就会落到"计划没人读、打得还像别的卡组"的坑里。
    /// </summary>
    [Deck("PlanAware", "AI_PlanAware", "Normal")]
    public class PlanAwareExecutor : DoEverythingExecutor
    {
        /// <summary>计划文件名（与 Python 侧 train/plan.py 的约定一致）。</summary>
        private const string PlanFileName = "MaiBotPlan.txt";

        /// <summary>卡组打法数据文件名（与 Python 侧 duel/playbook.py 的约定一致）。</summary>
        private const string PlaybookFileName = "MaiBotPlaybook.txt";

        /// <summary>卡组打法数据文件路径（命令行 ``PlaybookFile=<绝对路径>`` 可覆盖）。</summary>
        private string _bookPath = PlaybookFileName;

        private bool _bookLoaded;
        private List<int> _bookSummon = new List<int>();
        private List<int> _bookActivate = new List<int>();
        private List<int> _bookSet = new List<int>();
        private List<int> _bookSearch = new List<int>();
        private List<int> _bookNever = new List<int>();

        /// <summary>
        /// 计划文件路径：默认工作目录下的 PlanFileName，也可以用命令行参数
        /// ``PlanFile=<绝对路径>`` 指定。
        ///
        /// 为什么需要参数：擂台会并发跑好几个对局，它们共用同一个 WindBot 工作目录——
        /// 大家写同一个 MaiBotPlan.txt 就会互相撞（Windows 上表现为文件占用报错）。
        /// 每个对局用各自的文件，互不干扰。
        /// </summary>
        private string _planPath = PlanFileName;

        /// <summary>
        /// 计划最多当多少回合用。超过就视为过期，退回纯通用打法。
        ///
        /// 为什么需要：教练出一份计划要几秒（模型更久），而一个回合可能只持续一两秒——
        /// 让一份"好几回合前"的战术继续生效只会帮倒忙。默认 3 个回合：既给慢教练留余量，
        /// 也不至于抱着过期战术不放。
        /// </summary>
        private const int PlanMaxAgeTurns = 3;

        /// <summary>每 N 次决策重新读一次计划：文件很小，读它比一次模型调用便宜得多。</summary>
        private const int ReloadInterval = 8;

        private float _aggression = 0.5f;
        private bool _holdHandtraps;
        private bool _preferDirect;
        private int _planTurn;
        private int _reloadCounter;
        private bool _planLoaded;

        /// <summary>
        /// 是否打印调试信息（命令行 ``Debug=true``）。
        ///
        /// 不用 ``Logger.DebugWriteLine``：它在 Release 构建里被 ``#if DEBUG`` 编译掉了，
        /// 而擂台跑的就是 Release 版 —— 那样等于什么都不打，出问题时无从查起。
        /// </summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);

        /// <summary>已经打过日志的计划回合，避免每个决策点重复打同一份计划。</summary>
        private int _loggedTurn = -1;

        /// <summary>是否已经打过"决策入口被调用"的日志（每个进程一次足够）。</summary>
        private bool _loggedFirstCall;

        /// <summary>是否已经打过"读计划失败"的日志。</summary>
        private bool _loggedReadFailure;

        /// <summary>
        /// 记下第一次决策入口被调用的时刻。
        ///
        /// 排查"计划没人读"时第一步要分清的正是这件事：执行器到底有没有被调用。
        /// 只有它被调用了、却什么也没读到，才轮到怀疑文件读写。
        /// </summary>
        private void NoteCall(string name)
        {
            if (!_verbose || _loggedFirstCall)
                return;
            _loggedFirstCall = true;
            Logger.WriteLine("计划感知执行器开始工作（首次决策：" + name + "，计划文件：" + _planPath + "）");
        }

        /// <summary>阻止类卡片（手坑）：计划说"留手坑"时，这些卡不进连锁。</summary>
        private static readonly int[] HandtrapIds =
        {
            14558127, // 灰流丽
            23434538, // 增殖的G
            94145021, // 小丑与锁鸟
            97268402, // 效果遮蒙者
            42141493, // 欢聚友伴·茸茸长尾山雀
            84192580, // 欢聚友伴·抖抖海月水母
            24224830, // 墓穴的指名者
            10045474, // 无限泡影
            65681983, // 原始生命态 尼比鲁
            59438930, // 幽鬼兔
            24508238, // 三重赫利俄斯
        };

        public PlanAwareExecutor(GameAI ai, Duel duel)
            : base(ai, duel)
        {
            string configured = Config.GetString("PlanFile", PlanFileName);
            if (!string.IsNullOrEmpty(configured))
                _planPath = configured;
            string book = Config.GetString("PlaybookFile", PlaybookFileName);
            if (!string.IsNullOrEmpty(book))
                _bookPath = book;
            ReloadPlan(true);
            if (_verbose && !_planLoaded)
                Logger.WriteLine("没读到计划文件（按通用打法继续）：" + _planPath);
            EnsurePlaybook();
            WrapActivateRules();
        }

        /// <summary>
        /// 给继承来的"通用发动规则"套上计划判断。
        ///
        /// 为什么要替换而不是新增：WindBot 的判定是"第一条说 yes 的规则获胜"
        /// （GameAI.OnSelectChain 顺序遍历 Executors），新增一条规则拦不住后续规则——
        /// 想"这次别发动"只能把原规则包起来。
        /// </summary>
        private void WrapActivateRules()
        {
            for (int i = 0; i < Executors.Count; ++i)
            {
                CardExecutor exec = Executors[i];
                if (exec.Type != ExecutorType.Activate || exec.CardId != -1 || exec.Func == null)
                    continue;
                Func<bool> original = exec.Func;
                // 只套计划里的"留手坑"判断：问 AI 那层由 Executor.AddExecutor 统一装（见 MaiBotBrain）
                Executors[i] = new CardExecutor(ExecutorType.Activate, -1, () => HoldGuard(original));
            }
        }

        /// <summary>留手坑判断：计划要求留、且当前判定的正是手坑，就否决这次发动。</summary>
        private bool HoldGuard(Func<bool> original)
        {
            NoteCall("HoldGuard");
            ReloadPlan(false);
            if (_holdHandtraps && PlanIsFresh() && IsHandtrap(Card))
                return false;
            return original();
        }

        /// <summary>读取计划文件；返回是否读到了一份可用计划。</summary>
        private bool ReloadPlan(bool force)
        {
            if (!force && _planLoaded && (_reloadCounter++ % ReloadInterval) != 0)
                return true;

            try
            {
                if (!File.Exists(_planPath))
                    return false;
                Dictionary<string, string> values = new Dictionary<string, string>();
                foreach (string line in File.ReadAllLines(_planPath))
                {
                    int split = line.IndexOf('=');
                    if (split <= 0)
                        continue;
                    values[line.Substring(0, split).Trim().ToLowerInvariant()] = line.Substring(split + 1).Trim();
                }

                string raw;
                if (values.TryGetValue("aggression", out raw))
                {
                    float parsed;
                    if (float.TryParse(raw, out parsed))
                        _aggression = parsed < 0f ? 0f : (parsed > 1f ? 1f : parsed);
                }
                _holdHandtraps = IsOn(values, "hold_handtraps");
                _preferDirect = IsOn(values, "prefer_direct");
                if (values.TryGetValue("turn", out raw))
                {
                    int parsedTurn;
                    if (int.TryParse(raw, out parsedTurn))
                        _planTurn = parsedTurn;
                }
                _planLoaded = true;
                if (_verbose && _planTurn != _loggedTurn)
                {
                    _loggedTurn = _planTurn;
                    Logger.WriteLine(
                        "计划已读：turn=" + _planTurn
                        + " aggression=" + _aggression.ToString("0.00")
                        + " 留手坑=" + _holdHandtraps
                        + " 优先打脸=" + _preferDirect);
                }
                return true;
            }
            catch (IOException)
            {
                // 读不到计划就当没有计划：继续用通用打法，不影响这一局。
                // 但要在日志里留一句——否则"计划没人读"会变成查不出来的怪事
                if (_verbose && !_loggedReadFailure)
                {
                    _loggedReadFailure = true;
                    Logger.WriteLine("读计划失败（可能正被写入），先按通用打法继续：" + _planPath);
                }
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        /// <summary>计划是否还有效（没写回合数的一律当作有效，便于手工调参）。</summary>
        private bool PlanIsFresh()
        {
            if (_planTurn <= 0)
                return true;
            return Duel.Turn - _planTurn <= PlanMaxAgeTurns;
        }

        private static bool IsOn(Dictionary<string, string> values, string key)
        {
            string raw;
            if (!values.TryGetValue(key, out raw))
                return false;
            return raw == "1" || raw.ToLowerInvariant() == "true";
        }

        /// <summary>是否是"留着别用"的阻止类卡片。</summary>
        private static bool IsHandtrap(ClientCard card)
        {
            if (card == null)
                return false;
            foreach (int id in HandtrapIds)
            {
                if (card.IsOriginalCode(id))
                    return true;
            }
            return false;
        }

        /// <summary>当前计划是给第几回合的（日志用）。</summary>
        public int PlanTurn
        {
            get { return _planTurn; }
        }

        // ------------------------------------------------------------ 卡组打法数据

        /// <summary>
        /// 读卡组打法数据（``key=value``，见插件里的 duel/playbook.py），只在第一次决策前读一次。
        ///
        /// **为什么要有这份数据**：出牌脚本是编译进 exe 的，给每副牌现写一个执行器要编译、会崩、
        /// 写成空壳还看不出来（实测生成过"一张牌都不出"的脚本）。改成"模型写数据、这里读数据"以后，
        /// 没有编译步骤，读不懂的字段直接忽略，坏了最多是"这一步照通用打法做"，不会整局不动。
        ///
        /// 三类应用：
        ///   * 优先通常召唤 / 发动 / 盖放表里点名的卡（插到规则表最前面，见 RegisterBookRules）；
        ///   * 选卡（检索、取对象）时按表里的顺序挑；
        ///   * ``never_activate`` 只是**提醒**（写进给 AI 的问题里当"谨慎"标记），不再直接否决——
        ///     实测模型会把灰流丽这类手坑列进去，硬禁等于那些卡永远不发动。
        /// </summary>
        private void EnsurePlaybook()
        {
            if (_bookLoaded)
                return;
            _bookLoaded = true;
            if (!File.Exists(_bookPath))
            {
                if (_verbose)
                    Logger.WriteLine("没有卡组打法数据（按通用打法继续）：" + _bookPath);
                return;
            }
            try
            {
                foreach (string line in File.ReadAllLines(_bookPath))
                {
                    string text = line.Trim();
                    if (text.Length == 0 || text.StartsWith("#") || !text.Contains("="))
                        continue;
                    int split = text.IndexOf('=');
                    string key = text.Substring(0, split).Trim().ToLowerInvariant();
                    List<int> cards = ParseCardList(text.Substring(split + 1));
                    if (cards.Count == 0)
                        continue;
                    switch (key)
                    {
                        case "summon_order": _bookSummon = cards; break;
                        case "activate_order": _bookActivate = cards; break;
                        case "set_order": _bookSet = cards; break;
                        case "search_order": _bookSearch = cards; break;
                        case "never_activate": _bookNever = cards; break;
                    }
                }
            }
            catch (Exception ex)
            {
                // 读不动就当没有这份数据：它是"锦上添花"，不该让整局打不出来
                Logger.WriteLine("读卡组打法数据失败（按通用打法继续）：" + ex.Message);
                return;
            }
            RegisterBookRules();
            if (_verbose)
            {
                Logger.WriteLine("已读卡组打法数据：" + _bookPath
                    + "｜召唤 " + _bookSummon.Count
                    + " 发动 " + _bookActivate.Count
                    + " 盖放 " + _bookSet.Count
                    + " 检索 " + _bookSearch.Count
                    + " 禁用 " + _bookNever.Count);
            }
        }

        /// <summary>解析 ``1,2,3`` 形式的卡号表；非法项跳过（不因为一个错字让整份数据失效）。</summary>
        private static List<int> ParseCardList(string raw)
        {
            List<int> cards = new List<int>();
            foreach (string item in raw.Replace("，", ",").Split(','))
            {
                int card;
                if (int.TryParse(item.Trim(), out card) && card > 0 && !cards.Contains(card))
                    cards.Add(card);
            }
            return cards;
        }

        /// <summary>
        /// 把"优先出这些卡"插到规则表**最前面**。
        ///
        /// WindBot 的判定是"顺序遍历规则、第一条说 yes 的获胜"（见 GameAI.OnSelectIdleCmd），
        /// 所以只有插在前面才压得过继承来的通用规则。倒着插是为了让最终顺序与表里的顺序一致
        /// （越靠前优先级越高）。
        /// </summary>
        private void RegisterBookRules()
        {
            for (int i = _bookSummon.Count - 1; i >= 0; --i)
                Executors.Insert(0, new CardExecutor(ExecutorType.Summon, _bookSummon[i], null));
            for (int i = _bookActivate.Count - 1; i >= 0; --i)
                Executors.Insert(0, new CardExecutor(ExecutorType.Activate, _bookActivate[i], AlwaysPlay));
            for (int i = _bookSet.Count - 1; i >= 0; --i)
                Executors.Insert(0, new CardExecutor(ExecutorType.SpellSet, _bookSet[i], null));
        }

        /// <summary>表里点名的卡一律"该出就出"（具体时机与目标交给通用逻辑与选卡钩子）。</summary>
        private bool AlwaysPlay()
        {
            return true;
        }

        /// <summary>选卡优先级：检索目标 → 关键件 → 想站场的怪。</summary>
        private List<int> SelectionPriority()
        {
            List<int> merged = new List<int>();
            merged.AddRange(_bookSearch);
            merged.AddRange(_bookActivate);
            merged.AddRange(_bookSummon);
            return merged;
        }

        public override IList<ClientCard> OnSelectCard(
            IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            EnsurePlaybook();
            // 只在"要我挑一张"时按表里的顺序介入：多张的场合（素材、祭品）交给通用逻辑，
            // 免得挑错数量——那比不挑更糟。
            if (cards != null && cards.Count > 0 && min == 1 && max == 1)
            {
                foreach (int wanted in SelectionPriority())
                {
                    foreach (ClientCard candidate in cards)
                    {
                        if (candidate != null && candidate.IsCode(wanted))
                        {
                            List<ClientCard> picked = new List<ClientCard>();
                            picked.Add(candidate);
                            return picked;
                        }
                    }
                }
            }
            return base.OnSelectCard(cards, min, max, hint, cancelable);
        }

        public override ClientCard OnSelectAttacker(IList<ClientCard> attackers, IList<ClientCard> defenders)
        {
            NoteCall("OnSelectAttacker");
            ReloadPlan(false);
            ClientCard chosen = base.OnSelectAttacker(attackers, defenders);
            if (!PlanIsFresh())
                return chosen;

            // 低进攻：能不打就不打（保场），但被动挨打时仍按通用逻辑反击
            if (_aggression <= 0.2f)
                return null;

            // 高进攻：通用逻辑不肯出人时，只要有人能打就派一个上去
            if (_aggression >= 0.75f && chosen == null && attackers != null && attackers.Count > 0)
                return attackers[0];

            return chosen;
        }

        public override BattlePhaseAction OnSelectAttackTarget(ClientCard attacker, IList<ClientCard> defenders)
        {
            ReloadPlan(false);
            // 打谁问一次 AI 那层不在这里：它挂在 GameAI.OnSelectBattle 上（见 MaiBotBrain），
            // 任何出牌脚本（含各卡组专属执行器）的"打谁"都会被问一次，不必为了开 AI 换成通用执行器
            if (!PlanIsFresh())
                return base.OnSelectAttackTarget(attacker, defenders);

            if (_preferDirect && attacker != null && attacker.CanDirectAttack)
                return AI.Attack(attacker, null); // 优先打脸

            if (_aggression <= 0.2f && attacker != null && defenders != null && defenders.Count > 0)
                return null; // 保场：不去撞对面的怪兽

            return base.OnSelectAttackTarget(attacker, defenders);
        }

        public override bool OnSelectBattleDirectAttack(ClientCard attacker, bool preselectedAnswer)
        {
            ReloadPlan(false);
            if (!PlanIsFresh())
                return base.OnSelectBattleDirectAttack(attacker, preselectedAnswer);
            if (_preferDirect || _aggression >= 0.7f)
                return true; // 能打脸就打脸
            if (_aggression <= 0.15f)
                return false;
            return base.OnSelectBattleDirectAttack(attacker, preselectedAnswer);
        }

        public override CardPosition OnSelectPosition(int cardId, IList<CardPosition> positions)
        {
            ReloadPlan(false);
            if (!PlanIsFresh())
                return base.OnSelectPosition(cardId, positions);
            if (positions != null)
            {
                if (_aggression >= 0.8f && positions.Contains(CardPosition.FaceUpAttack))
                    return CardPosition.FaceUpAttack;
                if (_aggression <= 0.2f && positions.Contains(CardPosition.FaceUpDefence))
                    return CardPosition.FaceUpDefence;
            }
            return base.OnSelectPosition(cardId, positions);
        }
    }
}
