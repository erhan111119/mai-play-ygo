using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using System.Linq;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;
namespace WindBot.Game.AI.Decks
{
    [Deck("Kashtira", "AI_Kashtira")]
    class KashtiraExecutor : DefaultExecutor
    {
        public class CardId
        {
            public const int Nibiru = 27204311;
            public const int KashtiraUnicorn = 68304193;
            public const int KashtiraFenrir = 32909498;
            public const int KashtiraTearlaments = 4928565;
            public const int KashtiraScareclaw = 78534861;
            public const int DimensionShifter = 91800273;
            public const int NemesesCorridor = 72090076;
            public const int KashtiraRiseheart = 31149212;
            public const int G = 23434538;
            public const int AshBlossom = 14558127;
            // 鲜花女男爵（L10 同调）：这副牌**唯一的同调终端**，走"灰流丽 3★ 调整 ＋ 俱舍 7★ ＝ 10"。
            // 实测（对空白 20 局）：不登记它是个"能出但不主动"的卡——要提出场率得自己开一条通召口子
            //（见 AshBlossomAsTuner 与构造函数 ⑨）。
            public const int Baronne = 84815190;
            // 额外卡组里"登记为永不使用"的三张（2026-10-06 核过卡表，写在这里免得每轮体检都报一次）：
            // * 未来No.0 未来皇 霍普 65305468 / 未来No.0 未来龙皇 霍普 26973555——先要 2 只**1★**出未来皇、
            //   再上叠龙皇；这副牌主牌的怪只有 7★×6、6★、4★×2、3★、2★，**一只 1★ 都没有** → 整条线不可达；
            // * 转生炎兽 独角兔 60303245（LINK1，素材＝4★ 以下怪兽）——**可达但没用**：这副牌的展开
            //   不靠 LINK1 补位（阿莱斯哈特是 R7 上叠），出它只会白吃掉一只能够素材的低星怪。
            public const int UtopicFuture = 65305468;
            public const int UtopicDracoFuture = 26973555;
            public const int SalamangreatAlmiraj = 60303245;
            public const int MechaPhantom = 31480215;
            public const int Terraforming = 73628505;
            public const int PotofProsperity = 84211599;
            public const int KashtiraPapiyas = 34447918;
            public const int CalledbytheGrave = 24224830;
            public const int CrossoutDesignator = 65681983;
            public const int KashtiraBirth = 69540484;
            public const int PrimePlanetParaisos = 71832012;
            public const int KashtiraBigBang = 33925864;
            public const int InfiniteImpermanence = 10045474;

            // ===== 全盛俱舍卡表里的关键卡：自带执行器原来一条都没登记，整局一次都用不出来 =====
            // 俱舍怒威族·食人魔：自己场上没怪时从手牌特召（①），主要阶段检索「俱舍怒威族」陷阱（②）
            public const int KashtiraOgre = 94392192;
            // 俱舍怒威族的准备：从手牌或除外的自己怪兽里特召 1 只「俱舍怒威族」
            public const int KashtiraPreparations = 21639276;
            // 增援：检索 4 星以下战士族——这副牌里只有莱斯哈特这一个目标
            public const int ReinforcementOfTheArmy = 32807846;
            // 封印之黄金柜：从卡组选 1 张除外，2 个准备阶段后加手（把「被除外才转起来」的卡送进除外区）
            public const int GoldSarcophagus = 75500286;
            // 死灵之颜：被除外时双方卡组顶 5 张除外
            public const int Necroface = 28297833;
            // 雷仙神：支付 3000 基本分从手牌特召，2700/2400 的 7 星
            public const int ThunderGod = 70493141;

            // ===== 额外卡组里原来没登记的泛用件（同一次补上） =====
            public const int DivineArsenalAAZeus = 90448279;
            public const int SuperStarslayerTYPHON = 93039339;
            public const int Number11BigEye = 80117527;
            public const int RedEyesFlareMetalDragon = 44405066;
            public const int DarkArmedDragonOfAnnihilation = 78144171;
            public const int SPLittleKnight = 29301450;

            public const int ThunderDragonColossus = 15291624;
            public const int BorreloadSavageDragon = 27548199;
            public const int CupidPitch = 21915012;
            public const int KashtiraAriseHeart = 48626373;
            public const int DiablosistheMindHacker = 95474755;
            public const int KashtiraShangriIra = 73542331;
            public const int GalaxyTomahawk = 10389142;
            public const int BagooskatheTerriblyTiredTapir = 90590303;
            public const int MekkKnightCrusadiaAvramax = 21887175;
            public const int MechaPhantomBeastAuroradon = 44097050;
            public const int QliphortGenius = 22423493;
            public const int IP = 65741786;

            public const int Token = 10389143;
            public const int Token_2 = 44097051;
        }
        bool isSummoned = false;
        bool onlyXyzSummon = false;
        bool activate_KashtiraUnicorn_1 = false;
        bool activate_KashtiraFenrir_1 = false;
        bool activate_KashtiraRiseheart_1 = false;
        bool activate_KashtiraRiseheart_2 = false;
        bool activate_PrimePlanetParaisos = false;
        bool activate_KashtiraScareclaw_1 = false;
        bool activate_KashtiraShangriIra = false;
        bool activate_KashtiraTearlaments_1 = false;
        bool activate_DimensionShifter = false;
        bool activate_pre_PrimePlanetParaisos = false;
        bool activate_pre_PrimePlanetParaisos_2 = false;
        bool active_KashtiraPapiyas_1 = false;
        bool active_KashtiraPapiyas_2 = false;
        bool active_KashtiraBirth = false;
        bool active_NemesesCorridor = false;
        bool select_CalledbytheGrave = false;
        bool summon_KashtiraUnicorn = false;
        bool summon_KashtiraFenrir = false;
        bool link_mode = false;
        bool opt_0 = false;
        bool opt_1 = false;
        bool opt_2 = false;

        int flag = -1;
        int pre_link_mode = -1;
        List<ClientCard> select_Cards = new List<ClientCard>();
        /// <summary>
        /// 「抹杀之指名者」的探针开关（``Debug=true`` 时才有输出）。
        /// 为什么要有它：s4 的 54 局里这张卡 **0 次发动**（同批香格里拉茧 51 局、独角兽 50 局、
        /// 墓穴的指名者 21 局都正常），而它的条件链是"对手连锁的正好是那 12 张之一
        /// **且我们卡组里还剩一张同名卡**"——把这两个判据打出来，才能分清"根本没窗口"
        /// 与"有窗口但被条件挡住"（见 :meth:`CrossoutDesignatorCheck`）。
        /// </summary>
        private readonly bool _verbose = Config.GetBool("Debug", false);
        /// <summary>
        /// 本次「检索/回收」想要的卡号，按优先级排列。
        /// **为什么要有它**：新增的那几张卡（增援、黄金柜、食人魔②、准备①）都要从卡组/手牌/除外区选卡，
        /// 而它们各自的目标跟原有分支完全不同，混进原有分支会把别的效果的选卡搞乱。
        /// 用法照抄原有的 activate_pre_PrimePlanetParaisos：在 Activate 里填好、在 OnSelectCard 里用掉即清，
        /// 这样只有"刚刚确实发动了那张卡"的时候才会命中，没发动过就永远是空的。
        /// </summary>
        List<int> pendingCardsId = new List<int>();
        List<int> Impermanence_list = new List<int>();
        List<int> should_not_negate = new List<int>
        {
            81275020, 28985331
        };
        public KashtiraExecutor(GameAI ai, Duel duel)
          : base(ai, duel)
        {
            // ⓪ 先摘掉基类那条笼统通召规则（`AddExecutor(SummonOrSet, DefaultMonsterSummon)`），
            //    等下按"专属规则在前、通用在后"补回（同升辉月/耀圣/刻魔的做法）：
            //    IList 没有 RemoveAll，而这条笼统规则排在所有专属规则**前面**，不摘掉的话
            //    「灰流丽当调整」那条永远轮不到（它匹配任何怪、且默认返回 true）。
            //    ⚠ 这一步必须在加自己的规则之前做，放后面会把刚加的规则一起删掉。
            for (int i = Executors.Count - 1; i >= 0; --i)
            {
                if (Executors[i].Type == ExecutorType.SummonOrSet)
                    Executors.RemoveAt(i);
            }
            AddExecutor(ExecutorType.Activate, CardId.ThunderDragonColossus);
            AddExecutor(ExecutorType.SpSummon, CardId.ThunderDragonColossus);
            AddExecutor(ExecutorType.Activate, CardId.Nibiru, NibiruEffect);
            AddExecutor(ExecutorType.Activate, CardId.InfiniteImpermanence, Impermanence_activate);
            AddExecutor(ExecutorType.Activate, CardId.DimensionShifter, DimensionShifterEffect);
            AddExecutor(ExecutorType.Activate, CardId.G, DefaultMaxxC);
            AddExecutor(ExecutorType.Activate, CardId.AshBlossom, DefaultAshBlossomAndJoyousSpring);
            AddExecutor(ExecutorType.Activate, CardId.CalledbytheGrave, CalledbytheGraveEffect);
            AddExecutor(ExecutorType.Activate, CardId.BorreloadSavageDragon, BorreloadSavageDragonEffect);
            AddExecutor(ExecutorType.Activate, CardId.CrossoutDesignator, CrossoutDesignatorEffect);
            AddExecutor(ExecutorType.Activate, CardId.Terraforming, TerraformingEffect);
            AddExecutor(ExecutorType.Activate, CardId.PotofProsperity, PotofProsperityEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraUnicorn, KashtiraUnicornEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraFenrir, KashtiraFenrirEffect);
            AddExecutor(ExecutorType.Activate, CardId.PrimePlanetParaisos, PrimePlanetParaisosEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraBirth, KashtiraBirthEffect);
            AddExecutor(ExecutorType.Activate, CardId.DiablosistheMindHacker, DiablosistheMindHackerEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraFenrir, KashtiraFenrirSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraUnicorn,() => { summon_KashtiraUnicorn = true; return true; });
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraFenrir, () => {  summon_KashtiraFenrir = true; return true; });
            AddExecutor(ExecutorType.Summon, CardId.KashtiraUnicorn, DefaultSummon);
            AddExecutor(ExecutorType.Summon, CardId.KashtiraFenrir, DefaultSummon);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraPapiyas, KashtiraPapiyasEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraTearlaments, KashtiraTearlamentsEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraScareclaw, KashtiraScareclawEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraBirth, KashtiraBirthEffect_2);
            AddExecutor(ExecutorType.Activate, CardId.GalaxyTomahawk);
            AddExecutor(ExecutorType.SpSummon, CardId.GalaxyTomahawk, GalaxyTomahawkSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraShangriIra, KashtiraShangriIraSummon);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraShangriIra, KashtiraShangriIraEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraAriseHeart, KashtiraAriseHeartSummon_2);
            AddExecutor(ExecutorType.SpSummon, CardId.DiablosistheMindHacker, DiablosistheMindHackerSummon_2);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraAriseHeart, KashtiraAriseHeartEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraAriseHeart, KashtiraAriseHeartSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.DiablosistheMindHacker, DiablosistheMindHackerSummon);
            //link mode
            AddExecutor(ExecutorType.SpSummon, CardId.QliphortGenius, QliphortGeniusSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.IP, IPSummon);
            AddExecutor(ExecutorType.Activate, CardId.IP, IPEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.MechaPhantomBeastAuroradon, MechaPhantomBeastAuroradonSummon);
            AddExecutor(ExecutorType.Activate, CardId.MechaPhantomBeastAuroradon, MechaPhantomBeastAuroradonEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.CupidPitch, CupidPitchSummon);
            AddExecutor(ExecutorType.Activate, CardId.CupidPitch);
            AddExecutor(ExecutorType.SpSummon, CardId.BorreloadSavageDragon, BorreloadSavageDragonSummon);
            AddExecutor(ExecutorType.Activate, CardId.NemesesCorridor, NemesesCorridorEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.MekkKnightCrusadiaAvramax, MekkKnightCrusadiaAvramaxSummon);
            AddExecutor(ExecutorType.Activate, CardId.MekkKnightCrusadiaAvramax, MekkKnightCrusadiaAvramaxEffect);
            //link mode
            AddExecutor(ExecutorType.Activate, CardId.KashtiraRiseheart, KashtiraRiseheartEffect_2);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraRiseheart, KashtiraRiseheartEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraBigBang, KashtiraBigBangEffect);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraPapiyas, KashtiraPapiyasEffect_2);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraBirth, KashtiraBirthEffect_3);
            AddExecutor(ExecutorType.Summon, CardId.KashtiraRiseheart, KashtiraRiseheartSummon);
            AddExecutor(ExecutorType.Summon, CardId.KashtiraTearlaments, DefaultSummon);
            // ===== 新增：卡表里有、原来完全没登记的关键卡 =====
            // 食人魔：①自己场上没怪时手牌自跳（等级7，等于多一份超量素材）；②主要阶段检索「俱舍」陷阱
            AddExecutor(ExecutorType.SpSummon, CardId.KashtiraOgre, KashtiraOgreSummon);
            AddExecutor(ExecutorType.Activate, CardId.KashtiraOgre, KashtiraOgreEffect);
            // 雷仙神：支付 3000 基本分从手牌特召，补一只 7 星身体
            AddExecutor(ExecutorType.SpSummon, CardId.ThunderGod, ThunderGodSummon);
            // 增援：检索莱斯哈特（这副牌唯一的 4 星以下战士族，也是"除外一张俱舍卡"的起手）
            AddExecutor(ExecutorType.Activate, CardId.ReinforcementOfTheArmy, ReinforcementOfTheArmyEffect);
            // 封印之黄金柜：把死灵之颜（被除外时双方卡组顶 5 张除外）或「俱舍」怪送进除外区
            AddExecutor(ExecutorType.Activate, CardId.GoldSarcophagus, GoldSarcophagusEffect);
            // 俱舍怒威族的准备：从手牌/除外区把「俱舍」怪特召回来（被除外的独角兽/芬里尔是资源不是废牌）
            AddExecutor(ExecutorType.Activate, CardId.KashtiraPreparations, KashtiraPreparationsEffect);
            // 额外卡组的泛用件：登记之后才有"超额身体→第二只超量"的出口
            AddExecutor(ExecutorType.SpSummon, CardId.Number11BigEye, Number11BigEyeSummon);
            AddExecutor(ExecutorType.Activate, CardId.Number11BigEye, Number11BigEyeEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.RedEyesFlareMetalDragon, Rank7SurplusSummon);
            AddExecutor(ExecutorType.SpSummon, CardId.DarkArmedDragonOfAnnihilation, DarkArmedDragonSummon);
            AddExecutor(ExecutorType.Activate, CardId.DarkArmedDragonOfAnnihilation, DarkArmedDragonEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.DivineArsenalAAZeus, ZeusSummon);
            AddExecutor(ExecutorType.Activate, CardId.DivineArsenalAAZeus, ZeusEffect);
            AddExecutor(ExecutorType.SpSummon, CardId.SPLittleKnight, SPLittleKnightSummon);
            AddExecutor(ExecutorType.Activate, CardId.SPLittleKnight, SPLittleKnightEffect);
            AddExecutor(ExecutorType.SpellSet, SpellSet);
            AddExecutor(ExecutorType.Repos, DefaultRepos);

            // ⑨ 鲜花女男爵线：灰流丽当调整（见 AshBlossomAsTuner）——放在"补回笼统通召"之前才是专属。
            AddExecutor(ExecutorType.SummonOrSet, CardId.AshBlossom, AshBlossomAsTuner);

            // ⑩ 补回笼统通召（任何怪都能通召/盖放）：放在最后＝优先级在专属规则之后。
            Executors.Add(new CardExecutor(ExecutorType.SummonOrSet, -1, DefaultMonsterSummon));
        }

        /// <summary>
        /// 灰流丽**当调整**通召（鲜花女男爵线）：灰流丽 3★ ＋ 本家 7★ ＝ 10 星。
        ///
        /// 基类的通召判据不会为了同调去通召一只 0 攻的手坑，所以这一条要自己开。实测
        ///（2026-10-06，对空白 20 局）：不登记时「鲜花女男爵」偶尔会出（1~2/20 局，是基类的
        /// 通用特召闸门放的），要提出场率就得主动去通召调整。条件三条：
        /// ① 这张手坑确实是调整（`CardType.Tuner`——手坑里 1★ 的「小丑与锁鸟」**不是**调整，别按等级误判）；
        /// ② 额外卡组里还有鲜花；③ 场上已经有本家 7 星当另一个素材。
        /// 代价是这张灰流丽不再是手坑——与升辉月那条同调线同一个取舍（那边 40 局/腿 A/B 量过是中性）。
        /// </summary>
        private bool AshBlossomAsTuner()
        {
            if (Card == null || !Card.HasType(CardType.Tuner))
                return false;
            if (!Bot.HasInExtra(CardId.Baronne))
                return false;
            foreach (ClientCard monster in Bot.GetMonsters())
            {
                if (monster.IsFaceup() && monster.Level == 7)
                    return true;
            }
            return false;
        }

        public override bool OnSelectHand()
        {
            // go first
            return true;
        }

        public override void OnNewTurn()
        {
            if (pre_link_mode < 0) pre_link_mode = Program.Rand.Next(2);
            isSummoned = false;
            onlyXyzSummon = false;
            activate_KashtiraUnicorn_1 = false;
            activate_KashtiraFenrir_1 = false;
            activate_KashtiraRiseheart_1 = false;
            activate_KashtiraRiseheart_2 = false;
            activate_PrimePlanetParaisos = false;
            activate_KashtiraScareclaw_1 = false;
            activate_KashtiraTearlaments_1 = false;
            activate_KashtiraShangriIra = false;
            activate_pre_PrimePlanetParaisos_2 = false;
            active_KashtiraPapiyas_1 = false;
            active_KashtiraPapiyas_2 = false;
            active_KashtiraBirth = false;
            active_NemesesCorridor = false;
            link_mode = false;
            summon_KashtiraUnicorn = false;
            summon_KashtiraFenrir = false;
            opt_0 = false;
            opt_1 = false;
            opt_2 = false;
            if (flag >= 0) ++flag;
            if (flag >= 2) { flag = -1; activate_DimensionShifter = false; }
            base.OnNewTurn();
        }
        public override void OnChainEnd()
        {
            select_Cards.Clear();
            // 效果被无效/选卡没发生时要清干净，免得下一次选卡被上一张卡的意图劫走
            pendingCardsId.Clear();
            base.OnChainEnd();
        }
        public override bool OnSelectYesNo(int desc)
        {
            if (desc == 1149312192)
            {
                activate_pre_PrimePlanetParaisos = true;
            }
            return base.OnSelectYesNo(desc);
        }
        public override CardPosition OnSelectPosition(int cardId, IList<CardPosition> positions)
        {
            if (cardId == 27204312|| cardId==CardId.MechaPhantom)
            {
                return CardPosition.FaceUpDefence;
            }
            return base.OnSelectPosition(cardId, positions);
        }
        public override int OnSelectOption(IList<int> options)
        {
            if (options.Count == 2 && options[1] == Util.GetStringId(CardId.KashtiraBirth, 0))
                return 1;
            if (options.Count == 2 && options.Contains(Util.GetStringId(CardId.KashtiraTearlaments, 1)))
            {
                return (isEffectByRemove() || Enemy.Deck.Count <= 3) ? 1 : 0;
            }
            if (options.Contains(Util.GetStringId(CardId.MechaPhantomBeastAuroradon, 3)))
            {
                if (opt_1) return options.IndexOf(Util.GetStringId(CardId.MechaPhantomBeastAuroradon, 3));
                else if (opt_0) return 0;
                return options[options.Count - 1];
            }
            return base.OnSelectOption(options);
        }
        public override int OnSelectPlace(int cardId, int player, CardLocation location, int available)
        {
            if (cardId == CardId.GalaxyTomahawk)
            {
                if ((available & Zones.z5) > 0) return Zones.z5;
                if ((available & Zones.z6) > 0) return Zones.z6;
            }
            return base.OnSelectPlace(cardId, player, location, available);
        }

        public override uint OnSelectDisfield(int hint, int count, uint available)
        {
            ClientCard currentChainCard = Duel.GetCurrentChainCard();
            if (currentChainCard != null && currentChainCard.Controller == 0
                && currentChainCard.IsCode(CardId.KashtiraShangriIra) && count == 1)
            {
                for (int i = 4; i >= 0; --i)
                {
                    uint zone = 1u << (16 + i);
                    if ((available & zone) != 0) return zone;
                }
                for (int i = 4; i >= 0; --i)
                {
                    uint zone = 1u << (24 + i);
                    if ((available & zone) != 0) return zone;
                }
            }
            return base.OnSelectDisfield(hint, count, available);
        }

        public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)
        {
            // 新增卡（增援/黄金柜/食人魔②/准备①）的选卡：只认"刚刚发动的那张卡"留下的意图，
            // 命中不了就直接清掉、落回原有的分支，不会影响原来的选卡逻辑。
            if (pendingCardsId.Count > 0)
            {
                IList<int> wanted = new List<int>(pendingCardsId);
                pendingCardsId.Clear();
                IList<ClientCard> wantedCards = CardsIdToClientCards(wanted, cards);
                if (wantedCards.Count > 0) return Util.CheckSelectCount(wantedCards, cards, min, max);
            }
            if (cards.Any(card => card != null && card.Location == CardLocation.Extra
                 && hint == HintMsg.Remove && min == 1 && max == 1))
            {
                //Can't get card info in enemy extral.
                int index = Program.Rand.Next(cards.Count());
                if (index < 0 || index >= cards.Count()) return null;
                IList<ClientCard> res = new List<ClientCard>();
                res.Add(cards.ElementAtOrDefault(index));
                return Util.CheckSelectCount(res, cards, min, max);
            }
            if (cards.Any(card => card != null && card.Location == CardLocation.Grave && card.Controller == 1)
                && hint == HintMsg.Remove && ((min == 1 && max == 1) || (min == 3 && max == 3)))
            {
                if (select_CalledbytheGrave) { select_CalledbytheGrave = false; return null; }
                List<ClientCard> copyCards = new List<ClientCard>(cards);
                List<int> keyCardsId = new List<int>()
                {
                    44097050,15291624,63288573,70369116,83152482,72329844,
                    24094258,86066372,74997493,85289965,21887175,11738489,
                    98127546,50588353,10389142,90590303,27548199,
                };
                List<ClientCard> preCards = new List<ClientCard>();
                List<ClientCard> resCards = new List<ClientCard>();
                foreach (var card in copyCards)
                {
                    if (card != null && (keyCardsId.Contains(card.Id) || (card.Alias != 0 && keyCardsId.Contains(card.Alias))))
                        preCards.Add(card);
                    else resCards.Add(card);
                }
                if (preCards.Count > 0) return Util.CheckSelectCount(preCards, cards, min, max);
                resCards = FilterdRepeatIdCards(resCards);
                if (resCards != null)
                {
                    resCards.Sort(CardContainer.CompareCardAttack);
                    resCards.Reverse();
                    return Util.CheckSelectCount(resCards, cards, min, max);
                }
                copyCards.Sort(CardContainer.CompareCardAttack);
                copyCards.Reverse();
                return Util.CheckSelectCount(copyCards, cards, min, max);
            }
            if (cards.Any(card => card != null && card.Location == CardLocation.Extra && card.Controller == 0)
                && hint == HintMsg.Remove && min == 3 && max == 3)
            {
                //pre_link_mode == 0 xyz_mode
                //pre_link_mode == 1 link_mode
                List<ClientCard> repeatIdCards = FilterdRepeatIdCards(cards);
                if (repeatIdCards?.Count >= 3)
                {
                    if (pre_link_mode == 1) return Util.CheckSelectCount(repeatIdCards, cards, min, max);
                }
                List<ClientCard> resCards = new List<ClientCard>();
                List<int> cardsId = new List<int>()
                {
                    CardId.BagooskatheTerriblyTiredTapir,CardId.BorreloadSavageDragon,CardId.CupidPitch,
                    CardId.QliphortGenius,CardId.IP,CardId.MechaPhantomBeastAuroradon,CardId.MekkKnightCrusadiaAvramax,
                    CardId.ThunderDragonColossus
                };
                List<ClientCard> filterCards = CardsIdToClientCards(cardsId, cards).ToList();
                if (repeatIdCards?.Count > 0 && pre_link_mode == 0) resCards.AddRange(repeatIdCards);
                if (filterCards?.Count > 0) resCards.AddRange(filterCards);
                if (repeatIdCards?.Count > 0 && pre_link_mode == 1) resCards.AddRange(repeatIdCards);
                if (resCards.Count > 0)
                {
                    return Util.CheckSelectCount(resCards, cards, min, max);
                }
                return null;
            }
            if (hint == HintMsg.Remove && cards.Any(card => card != null && (card.Location == CardLocation.Hand || card.Location == CardLocation.Grave) && card.Controller == 0) && min == 1 && max == 1)
            {
                List<ClientCard> selectedCards = select_Cards.Where(cards.Contains).ToList();
                select_Cards.Clear();
                if (selectedCards.Count > 0)
                {
                    return Util.CheckSelectCount(selectedCards, cards, min, max);
                }
                else
                {
                    IList<ClientCard> grave_cards = cards.GetMatchingCards(card => card != null && card.Location == CardLocation.Grave);
                    if (grave_cards.Count > 0) return Util.CheckSelectCount(grave_cards, cards, min, max);
                    return null;
                }

            }
            if (hint == HintMsg.XyzMaterial && cards.Any(card => card != null && card.Location == CardLocation.Removed) && min == 1 && max == 1)
            {
                List<ClientCard> m_cards = new List<ClientCard>();
                List<ClientCard> e_cards_u = new List<ClientCard>();
                List<ClientCard> e_cards_d = new List<ClientCard>();
                foreach (var card in cards)
                {
                    if (card != null && card.Controller == 0)
                        m_cards.Add(card);
                    if (card != null && card.Controller == 1 && card.IsFaceup())
                        e_cards_u.Add(card);
                    if (card != null && card.Controller == 1 && card.IsFacedown())
                        e_cards_d.Add(card);
                }
                List<ClientCard> res = new List<ClientCard>();
                if (e_cards_u.Count > 0)
                {
                    e_cards_u.Sort(CardContainer.CompareCardAttack);
                    e_cards_u.Reverse();
                    res.AddRange(e_cards_u);
                }
                IList<int> cardsId = new List<int>() { CardId.KashtiraBigBang, CardId.KashtiraPapiyas };
                IList<ClientCard> m_pre_cards = CardsIdToClientCards(cardsId, m_cards, false);
                if (m_pre_cards?.Count > 0) res.AddRange(m_pre_cards);
                else if (m_cards.Count > 0) res.AddRange(m_cards);
                if (e_cards_d.Count > 0) res.AddRange(e_cards_d);
                if (res.Count <= 0) return null;
                return Util.CheckSelectCount(res, cards, min, max);
            }
            if (hint == HintMsg.Release && cards.Any(card => card != null && card.Location == CardLocation.MonsterZone))
            {
                List<ClientCard> tRelease = new List<ClientCard>();
                List<ClientCard> nRelease = new List<ClientCard>();
                foreach (var card in cards)
                {
                    if (card == null || (card.IsExtraCard() && card.Id != CardId.DiablosistheMindHacker) || card.IsFacedown()) continue;
                    if (card.Id == CardId.Token || card.Id == CardId.Token_2)
                        tRelease.Add(card);
                    else nRelease.Add(card);
                }
                if (opt_1)
                {
                    IList<int> cardsId = new List<int>() { CardId.Token, CardId.Token_2 };
                    tRelease = CardsIdToClientCards(cardsId, tRelease, false).ToList();
                    if(tRelease?.Count > 0) nRelease.AddRange(tRelease);
                    if (nRelease.Count <= 0) return null;
                    return Util.CheckSelectCount(nRelease, cards, min, max);
                }
                else
                {
                    tRelease.AddRange(nRelease);
                    if(tRelease.Count <= 0) return null;
                    return Util.CheckSelectCount(tRelease, cards, min, max);
                }
            }
            if (hint == HintMsg.Destroy && cards.Any(card => card != null && card.Controller == 1 && (card.Location & CardLocation.Onfield) > 0) && min==1 && max==1)
            {
                ClientCard card = Util.GetBestEnemyCard();
                List<ClientCard> res = new List<ClientCard>();
                if (card != null && cards.Contains(card))
                {
                    res.Add(card);
                    return Util.CheckSelectCount(res, cards, min, max);
                }
                res = cards.Where(_card => _card != null && _card.Controller == 1).ToList();
                if (res.Count <= 0) return null; 
                res.Sort(CardContainer.CompareCardAttack);
                res.Reverse();
                return Util.CheckSelectCount(res, cards, min, max);
            }
            if (activate_pre_PrimePlanetParaisos)
            {
                activate_pre_PrimePlanetParaisos = false;
                IList<int> cardsId = new List<int>();
                if (!Bot.HasInHand(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1 && Bot.HasInDeck(CardId.KashtiraUnicorn)) cardsId.Add(CardId.KashtiraUnicorn);
                if (!Bot.HasInHand(CardId.KashtiraFenrir) && !activate_KashtiraFenrir_1 && Bot.HasInDeck(CardId.KashtiraFenrir)) cardsId.Add(CardId.KashtiraFenrir);
                if (!Bot.HasInHand(CardId.KashtiraScareclaw) && !activate_KashtiraScareclaw_1 && Bot.HasInDeck(CardId.KashtiraScareclaw)) cardsId.Add(CardId.KashtiraScareclaw);
                if (!Bot.HasInHand(CardId.KashtiraTearlaments) && !activate_KashtiraTearlaments_1 && Bot.HasInDeck(CardId.KashtiraTearlaments)) cardsId.Add(CardId.KashtiraTearlaments);
                if (!Bot.HasInHand(CardId.KashtiraRiseheart) && (!activate_KashtiraRiseheart_2 || !activate_KashtiraRiseheart_1) && Bot.HasInDeck(CardId.KashtiraRiseheart)) cardsId.Add(CardId.KashtiraRiseheart);
                IList<ClientCard> copyCards = new List<ClientCard>(cards);
                IList<ClientCard> res = CardsIdToClientCards(cardsId, copyCards);
                if (res?.Count <= 0) return null;
                return Util.CheckSelectCount(res, cards, min, max);

            }
            return base.OnSelectCard(cards, min, max, hint, cancelable);
        }
        #region CopyImpermanence
        public bool Impermanence_activate()
        {
            // negate before effect used
            foreach (ClientCard m in Enemy.GetMonsters())
            {
                if (m.IsMonsterShouldBeDisabledBeforeItUseEffect() && !m.IsDisabled() && Duel.LastChainPlayer != 0)
                {
                    if (Card.Location == CardLocation.SpellZone)
                    {
                        for (int i = 0; i < 5; ++i)
                        {
                            if (Bot.SpellZone[i] == Card)
                            {
                                Impermanence_list.Add(i);
                                break;
                            }
                        }
                    }
                    if (Card.Location == CardLocation.Hand)
                    {
                        AI.SelectPlace(SelectSTPlace(Card, true));
                    }
                    AI.SelectCard(m);
                    return true;
                }
            }

            ClientCard LastChainCard = Util.GetLastChainCard();

            // negate spells
            if (Card.Location == CardLocation.SpellZone)
            {
                int this_seq = -1;
                int that_seq = -1;
                for (int i = 0; i < 5; ++i)
                {
                    if (Bot.SpellZone[i] == Card) this_seq = i;
                    if (LastChainCard != null
                        && LastChainCard.Controller == 1 && LastChainCard.Location == CardLocation.SpellZone && Enemy.SpellZone[i] == LastChainCard) that_seq = i;
                    else if (Duel.Player == 0 && Util.GetProblematicEnemySpell() != null
                        && Enemy.SpellZone[i] != null && Enemy.SpellZone[i].IsFloodgate()) that_seq = i;
                }
                if ((this_seq * that_seq >= 0 && this_seq + that_seq == 4)
                    || (Util.IsChainTarget(Card))
                    || (LastChainCard != null && LastChainCard.Controller == 1 && LastChainCard.IsCode(_CardId.HarpiesFeatherDuster)))
                {
                    List<ClientCard> enemy_monsters = Enemy.GetMonsters();
                    enemy_monsters.Sort(CardContainer.CompareCardAttack);
                    enemy_monsters.Reverse();
                    foreach (ClientCard card in enemy_monsters)
                    {
                        if (card.IsFaceup() && !card.IsShouldNotBeTarget() && !card.IsShouldNotBeSpellTrapTarget())
                        {
                            AI.SelectCard(card);
                            Impermanence_list.Add(this_seq);
                            return true;
                        }
                    }
                }
            }
            if ((LastChainCard == null || LastChainCard.Controller != 1 || LastChainCard.Location != CardLocation.MonsterZone
                || LastChainCard.IsDisabled() || LastChainCard.IsShouldNotBeTarget() || LastChainCard.IsShouldNotBeSpellTrapTarget()))
                return false;
            // negate monsters
            if (is_should_not_negate() && LastChainCard.Location == CardLocation.MonsterZone) return false;
            if (Card.Location == CardLocation.SpellZone)
            {
                for (int i = 0; i < 5; ++i)
                {
                    if (Bot.SpellZone[i] == Card)
                    {
                        Impermanence_list.Add(i);
                        break;
                    }
                }
            }
            if (Card.Location == CardLocation.Hand)
            {
                AI.SelectPlace(SelectSTPlace(Card, true));
            }
            if (LastChainCard != null) AI.SelectCard(LastChainCard);
            else
            {
                List<ClientCard> enemy_monsters = Enemy.GetMonsters();
                enemy_monsters.Sort(CardContainer.CompareCardAttack);
                enemy_monsters.Reverse();
                foreach (ClientCard card in enemy_monsters)
                {
                    if (card.IsFaceup() && !card.IsShouldNotBeTarget() && !card.IsShouldNotBeSpellTrapTarget())
                    {
                        AI.SelectCard(card);
                        return true;
                    }
                }
            }
            return true;
        }
        public int SelectSTPlace(ClientCard card = null, bool avoid_Impermanence = false)
        {
            List<int> list = new List<int> { 0, 1, 2, 3, 4 };
            Util.ShuffleListInPlace(list);
            foreach (int seq in list)
            {
                int zone = (int)System.Math.Pow(2, seq);
                if (Bot.SpellZone[seq] == null)
                {
                    if (card != null && card.Location == CardLocation.Hand && avoid_Impermanence && Impermanence_list.Contains(seq)) continue;
                    return zone;
                };
            }
            return 0;
        }
        public bool is_should_not_negate()
        {
            ClientCard last_card = Util.GetLastChainCard();
            if (last_card != null
                && last_card.Controller == 1 && last_card.IsCode(should_not_negate))
                return true;
            return false;
        }
        #endregion
        private List<ClientCard> FilterdRepeatIdCards(IList<ClientCard> cards)
        {
            IList<ClientCard> temp = new List<ClientCard>();
            List<ClientCard> res = new List<ClientCard>();
            foreach (var card in cards)
            {
                if (card == null) continue;
                if (temp.Count(_card => _card != null && _card.Id == card.Id) > 0 && res.Count(_card=>_card != null && _card.Id == card.Id) <= 0)
                    res.Add(card);
                else
                    temp.Add(card);
            }
            return res.Count < 0 ? null : res;
        }
        private IList<ClientCard> CardsIdToClientCards(IList<int> cardsId, IList<ClientCard> cardsList, bool uniqueId = true, bool alias = true)
        {
            if (cardsList?.Count() <= 0 || cardsId?.Count() <= 0) return new List<ClientCard>();
            List<ClientCard> res = new List<ClientCard>();
            foreach (var cardid in cardsId)
            {
                List<ClientCard> cards = cardsList.Where(card => card != null && (card.Id == cardid || ((card.Alias != 0 && cardid == card.Alias) & alias))).ToList();
                if (cards?.Count <= 0) continue;
                cards.Sort(CardContainer.CompareCardAttack);
                if (uniqueId) res.Add(cards.First());
                else res.AddRange(cards);
            }
            return res;
        }
        private IList<int> ClientCardsToCardsId(IList<ClientCard> cardsList, bool uniqueId = false, bool alias = false)
        {
            if (cardsList == null) return null;
            if (cardsList.Count <= 0) return new List<int>();
            IList<int> res = new List<int>();
            foreach (var card in cardsList)
            {
                if (card == null) continue;
                if (card.Alias != 0 && alias && !(res.Contains(card.Alias) & uniqueId)) res.Add(card.Alias);
                else if (card.Id != 0 && !(res.Contains(card.Id) & uniqueId)) res.Add(card.Id);
            }
            return res.Count < 0 ? null : res;
        }
        private bool DefaultRepos()
        {
            if (Card.Id == CardId.KashtiraScareclaw || (Card.Id == CardId.KashtiraShangriIra && Card.Attack<2000)) return false;
            return DefaultMonsterRepos();
        }
        private bool CrossoutDesignatorCheck(ClientCard LastChainCard, int id)
        {
            if (!LastChainCard.IsCode(id))
                return false;
            bool inDeck = Bot.HasInDeck(id);
            if (_verbose)
                Logger.WriteLine("[探针] 抹杀之指名者：对手连锁 " + (LastChainCard.Name ?? "?")
                    + "｜卡组里还有同名=" + inDeck + "｜手里有同名=" + Bot.HasInHand(id));
            if (inDeck)
            {
                AI.SelectAnnounceID(id);
                return true;
            }
            return false;
        }
        private bool CrossoutDesignatorEffect()
        {
            ClientCard LastChainCard = Util.GetLastChainCard();
            if (LastChainCard == null || Duel.LastChainPlayer != 1) return false;
            if (CrossoutDesignatorCheck(LastChainCard, CardId.Nibiru)
                || CrossoutDesignatorCheck(LastChainCard, CardId.AshBlossom)
                || CrossoutDesignatorCheck(LastChainCard, CardId.G)
                || CrossoutDesignatorCheck(LastChainCard, CardId.NemesesCorridor)
                || CrossoutDesignatorCheck(LastChainCard, CardId.InfiniteImpermanence)
                || CrossoutDesignatorCheck(LastChainCard, CardId.CalledbytheGrave)
                || CrossoutDesignatorCheck(LastChainCard, CardId.Terraforming)
                || CrossoutDesignatorCheck(LastChainCard, CardId.PotofProsperity)
                || CrossoutDesignatorCheck(LastChainCard, CardId.KashtiraPapiyas)
                || CrossoutDesignatorCheck(LastChainCard, CardId.KashtiraUnicorn)
                || CrossoutDesignatorCheck(LastChainCard, CardId.KashtiraFenrir)
                || CrossoutDesignatorCheck(LastChainCard, CardId.KashtiraBirth))
            {
                if (Card.Location == CardLocation.Hand)
                {
                    AI.SelectPlace(SelectSTPlace(Card, true));
                }
                return true;
            }
            return false;
        }
        private bool MekkKnightCrusadiaAvramaxEffect()
        {
            if (Card.Location == CardLocation.Grave)
            {
                List<ClientCard> cards = Enemy.GetMonsters();
                cards.Sort(CardContainer.CompareCardAttack);
                cards.Reverse();
                cards.AddRange(Enemy.GetSpells());
                if (cards.Count <= 0) return false;
                AI.SelectCard(cards);
                return true;
            }
            else return true;
        }
        private bool SpellSet()
        {
            return Card.HasType(CardType.QuickPlay) || Card.HasType(CardType.Trap);
        }
        private bool NibiruEffect()
        {
            if (Bot.HasInMonstersZone(CardId.KashtiraAriseHeart, true, false, true) && Util.GetBestAttack(Bot) > Util.GetBestAttack(Enemy)) return false;
            return Bot.GetMonsterCount() <= 0 || Bot.GetMonsterCount() < Enemy.GetMonsterCount();
        }
        private bool CalledbytheGraveEffect()
        {
            ClientCard card = Util.GetLastChainCard();
            if (card == null) return false;
            int id = card.Id;
            List<ClientCard> g_cards = Enemy.GetGraveyardMonsters().Where(g_card => g_card != null && g_card.Id == id).ToList();
            if (Duel.LastChainPlayer != 0 && card != null)
            {
                if (Card.Location == CardLocation.Hand)
                {
                    AI.SelectPlace(SelectSTPlace(Card, true));
                }
                if (card.Location == CardLocation.Grave && card.HasType(CardType.Monster))
                {
                    AI.SelectCard(card);
                }
                else if (g_cards.Count() > 0 && card.HasType(CardType.Monster))
                {
                    AI.SelectCard(g_cards);
                }
                else return false;
                select_CalledbytheGrave = true;
                return true;
            }
            return false;
        }
        private bool CupidPitchSummon()
        {
            if (!Bot.HasInMonstersZone(CardId.Token_2) && !Bot.HasInMonstersZone(CardId.MechaPhantom)) return false;
            IList<int> cardsId = new List<int>() { CardId.MechaPhantom, CardId.Token_2 };
            IList<ClientCard> cards = CardsIdToClientCards(cardsId, Bot.GetMonsters(), false);
            if (cards?.Count <= 0) return false;
            AI.SelectMaterials(cards);
            return true;

        }
        private bool BorreloadSavageDragonSummon()
        {
            if (!Bot.HasInMonstersZone(CardId.Token_2) && !Bot.HasInMonstersZone(CardId.CupidPitch)) return false;
            IList<int> cardsId = new List<int>() { CardId.CupidPitch, CardId.Token_2 };
            IList<ClientCard> cards = CardsIdToClientCards(cardsId, Bot.GetMonsters(), false);
            if (cards?.Count <= 0) return false;
            AI.SelectMaterials(cards);
            link_mode = false;
            return true;
        }
        private bool BorreloadSavageDragonEffect()
        {
            if (ActivateDescription == -1)
            {
                AI.SelectCard(new[] { CardId.MekkKnightCrusadiaAvramax, CardId.MechaPhantomBeastAuroradon, CardId.IP, CardId.QliphortGenius});
                return true;
            }
            return true;

        }
        private bool IPSummon()
        {
            if (!Bot.HasInMonstersZone(CardId.Token) && !Bot.HasInMonstersZone(CardId.Token_2)) return false;
            if (!Bot.HasInExtra(CardId.MechaPhantomBeastAuroradon) || !(Bot.HasInExtra(CardId.MekkKnightCrusadiaAvramax) && (Bot.HasInExtra(CardId.QliphortGenius) || Bot.HasInMonstersZone(CardId.QliphortGenius, false, false, true)))) return false;
            List<ClientCard> cards = Bot.GetMonsters().Where(card => card != null && !card.HasType(CardType.Link) && card.IsFaceup() && card.HasType(CardType.Monster) && !card.HasType(CardType.Xyz)).ToList();
            if (cards?.Count < 2 && !link_mode) return false;
            IList<int> cardsId = new List<int> { CardId.GalaxyTomahawk, CardId.Token };
            IList<ClientCard> pre_cards = CardsIdToClientCards(cardsId, cards, false);
            if (pre_cards?.Count >= 2) { AI.SelectMaterials(pre_cards); return true; }
            cards.Sort(CardContainer.CompareCardAttack);
            AI.SelectMaterials(cards);
            return true;
        }
        private bool IPEffect()
        {
            if (!Bot.HasInExtra(CardId.MekkKnightCrusadiaAvramax) && !(Bot.HasInMonstersZone(CardId.QliphortGenius,false,false,true) ||
                Bot.HasInMonstersZone(CardId.MechaPhantomBeastAuroradon, false, false, true))) return false;
            IList<int> cardsId = new List<int> {CardId.MechaPhantomBeastAuroradon,CardId.IP,CardId.QliphortGenius};
            IList<ClientCard> pre_m = CardsIdToClientCards(cardsId, Bot.GetFaceupMonsters());
            if (pre_m?.Count <= 0) return false;
            List<ClientCard> materials = Util.GetLinkMaterials(pre_m, 4, 2, 4)
                .FirstOrDefault(list => list.Contains(Card));
            if (materials == null) return false;
            AI.SelectCard(CardId.MekkKnightCrusadiaAvramax);
            AI.SelectMaterials(materials);
            return true;

        }
        private bool MechaPhantomBeastAuroradonEffect()
        {
            if (ActivateDescription == -1) return true;
            else
            {
                if (!Bot.HasInDeck(CardId.MechaPhantom)
                    && GetEnemyOnFields().Count <= 0 && Bot.Graveyard.Count(card => card != null && card.HasType(CardType.Trap))<=0) return false;
                List<ClientCard> tRelease = new List<ClientCard>();
                List<ClientCard> nRelease = new List<ClientCard>();
                foreach (var card in Bot.GetMonsters())
                {
                    if (card == null || (card.IsExtraCard() && card.Id != CardId.DiablosistheMindHacker) || card.IsFacedown()) continue;
                    if (card.Id == CardId.Token || card.Id == CardId.Token_2)
                        tRelease.Add(card);
                    else nRelease.Add(card);
                }
                int count = tRelease.Count() + nRelease.Count();
                opt_0 = false;
                opt_1 = false;
                opt_2 = false;
                if (count >= 3 && Bot.Graveyard.Count(card => card != null && card.HasType(CardType.Trap)) >0 ) opt_2 = true;
                if (count >= 2 && Bot.HasInDeck(CardId.MechaPhantom)) opt_1 = true;
                if (count >= 1 && GetEnemyOnFields().Count > 0) opt_0 = true;
                if (!opt_0 && !opt_1 && !opt_2) return false;
                return true;
            }

        }
        private bool QliphortGeniusSummon()
        {
            List<ClientCard> cards = Bot.GetMonsters().Where(card => card != null && card.Id==CardId.Token).ToList();
            if (cards.Count <= 2) return false;
            AI.SelectMaterials(cards);
            return true;
        }
        private bool MekkKnightCrusadiaAvramaxSummon()
        {
            IList<int> cardsId = new List<int>() {CardId.MechaPhantomBeastAuroradon,CardId.IP,CardId.QliphortGenius};
            List<ClientCard> cards = CardsIdToClientCards(cardsId, Bot.GetFaceupMonsters()).ToList();
            if (cards.Count <= 0) return false;
            List<ClientCard> materials = Util.GetLinkMaterials(cards, 4, 2, 4).FirstOrDefault();
            if (materials == null) return false;
            AI.SelectMaterials(materials);
            return true;
        }
        private bool MechaPhantomBeastAuroradonSummon()
        {
            if (!Bot.HasInMonstersZone(CardId.QliphortGenius,false,false,true) && !Bot.HasInMonstersZone(CardId.Token, false, false, true)) return false;
            List<ClientCard> m = new List<ClientCard>();
            List<ClientCard> m1 = Bot.GetFaceupMonsters().Where(card => card.Id == CardId.QliphortGenius).ToList();
            List<ClientCard> m2 = Bot.GetFaceupMonsters().Where(card => card.Id == CardId.Token).ToList();
            if (m1.Count > 0) m.AddRange(m1);
            if (m2.Count > 0) m.AddRange(m2);
            List<ClientCard> materials = Util.GetLinkMaterials(m, 3, 2, 3).FirstOrDefault();
            if (materials == null) return false;
            AI.SelectMaterials(materials);
            return true;
        }
        private bool KashtiraFenrirSummon()
        {
            if (Bot.HasInHandOrInSpellZone(CardId.KashtiraBirth) && Bot.HasInHandOrInSpellZone(CardId.KashtiraPapiyas)) { summon_KashtiraFenrir = true; return true; }
            return false;
        }
        private bool DiablosistheMindHackerEffect()
        {
            AI.SelectCard(CardId.KashtiraFenrir, CardId.KashtiraUnicorn, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments);
            return true;
        }
        private bool KashtiraRiseheartSummon()
        {
            isSummoned = true;
            return !activate_KashtiraRiseheart_2;
        }
        private bool DimensionShifterEffect()
        {
            if (activate_DimensionShifter) return false;
            flag = -1;
            ++flag;
            activate_DimensionShifter = true;
            return true;
        }
        private bool KashtiraBigBangEffect()
        {
            if (Card.Location == CardLocation.Removed)
            {
                AI.SelectCard(CardId.KashtiraShangriIra);
                AI.SelectNextCard(CardId.KashtiraUnicorn,CardId.KashtiraFenrir,CardId.KashtiraTearlaments,CardId.KashtiraScareclaw);
                return true;
            }
            else if(Card.Location==CardLocation.SpellZone)
            {
                if (Enemy.GetMonsterCount() <= 0) return false;
                if (Bot.GetMonsterCount() <= 1) return true;
                return Bot.GetMonsterCount()< Enemy.GetMonsterCount();
            }
            return false;
        }
        private bool SpellActivate()
        {
            return Card.Location == CardLocation.Hand || (Card.IsFacedown() && (Card.Location == CardLocation.SpellZone || Card.Location == CardLocation.FieldZone));
        }
        private bool PrimePlanetParaisosEffect()
        {
            if (SpellActivate()) { activate_pre_PrimePlanetParaisos_2 = true; return true; }
            if (activate_pre_PrimePlanetParaisos_2 || activate_pre_PrimePlanetParaisos) return false;
            List<ClientCard> cards = GetEnemyOnFields().Where(card => card != null && !card.IsShouldNotBeTarget()).ToList();
            if (cards == null || cards.Count <= 0) return false;
            return true;
        }
        private bool DiablosistheMindHackerSummon()
        {
            List<ClientCard> cards = Bot.GetMonsters();
            cards.Sort(CardContainer.CompareCardAttack);
            AI.SelectMaterials(cards);
            return true;
        }
        private bool XyzCheck()
        {
            if (Bot.GetMonsters().Count(card => card != null && card.IsFaceup() && card.Level == 7) >= 4 && Bot.HasInExtra(CardId.KashtiraShangriIra)) return false;
            if ((active_KashtiraPapiyas_1 || !Bot.HasInHandOrInSpellZone(CardId.KashtiraPapiyas))
                && (activate_KashtiraUnicorn_1 || !Bot.HasInHand(CardId.KashtiraUnicorn) || isSummoned || !Bot.HasInSpellZone(CardId.KashtiraBirth, true, true))
                && (activate_KashtiraFenrir_1 || !Bot.HasInHand(CardId.KashtiraFenrir) || isSummoned || !Bot.HasInSpellZone(CardId.KashtiraBirth, true, true))
                && (activate_KashtiraScareclaw_1 || !Bot.HasInHand(CardId.KashtiraScareclaw) || isSummoned || !Bot.HasInSpellZone(CardId.KashtiraBirth, true, true))
                && (activate_KashtiraTearlaments_1 || !Bot.HasInHand(CardId.KashtiraTearlaments) || isSummoned || !Bot.HasInSpellZone(CardId.KashtiraBirth, true, true))
                && (activate_KashtiraRiseheart_2 || !Bot.HasInHand(CardId.KashtiraRiseheart) || activate_KashtiraRiseheart_1 || isSummoned)) return true;
            return false;
        
        }
        private bool GalaxyTomahawkSummon()
        {
            if (!Bot.HasInDeck(CardId.MechaPhantom)) return false;
            if (Bot.GetMonsterCount() >= 4) return false;
            if (onlyXyzSummon || activate_DimensionShifter || Bot.HasInMonstersZone(CardId.KashtiraAriseHeart,true,false,true)) return false;
            if (!Bot.HasInExtra(CardId.MekkKnightCrusadiaAvramax) && !(Bot.HasInExtra(CardId.CupidPitch) || Bot.HasInExtra(CardId.BorreloadSavageDragon))) return false;
            //if (!XyzCheck()) return false;
            link_mode = true;
            return DiablosistheMindHackerSummon();
        }
        private bool DiablosistheMindHackerSummon_2()
        {
            if (Bot.HasInMonstersZone(CardId.DiablosistheMindHacker)) return false;
            return DiablosistheMindHackerSummon();
        }
        private bool isEffectByRemove()
        {
            return activate_DimensionShifter || Bot.HasInMonstersZone(CardId.KashtiraAriseHeart, true, false, true) || Enemy.HasInMonstersZone(CardId.KashtiraAriseHeart, true, false, true);
        }
        private bool NemesesCorridorEffect()
        {
            if (Card.Location == CardLocation.Hand)
            {
                if(Bot.GetMonsterCount()<=0) return true;
                if (onlyXyzSummon || !Bot.HasInExtra(CardId.ThunderDragonColossus)) return false;
                else return true;
            }
            return false;
        }
        private bool KashtiraTearlamentsEffect()
        {
            if (Card.Location == CardLocation.Hand)
            {
                if (Duel.Player != 0) return false;
                if (Duel.CurrentChain.Count > 0) return false;
                if (!ActivateLimit(Card.Id)) return false;
                activate_KashtiraTearlaments_1 = true;
                return true;
            }
            if (Card.Location == CardLocation.MonsterZone)
            {
                if (isEffectByRemove() && Enemy.Deck.Count >= 3) return true;
                if (!isEffectByRemove() && Bot.Deck.Count > 10) return true;
                return false;
            }
            if (Card.Location == CardLocation.Grave)
            {
                if (isEffectByRemove()) return false;
                return Bot.Deck.Count > 10;
            }
            return false;
        }
        private bool ActivateLimit(int cardId)
        {
            if (Bot.MonsterZone.Count() <= 0
                && ((Bot.HasInHand(CardId.KashtiraFenrir) && !activate_KashtiraFenrir_1)
                || (Bot.HasInHand(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1))) return false;
            if (Bot.HasInHand(CardId.PrimePlanetParaisos) && !activate_pre_PrimePlanetParaisos_2) return false;
            List<ClientCard> cards = new List<ClientCard>();
            List<ClientCard> hand_cards = Bot.Hand.GetMatchingCards(card=>card!=null && card.HasSetcode(0x189)).ToList();
            List<ClientCard> grave_cards = Bot.Graveyard.GetMatchingCards(card => card != null && card.HasSetcode(0x189)).ToList();
            List<int> cardsid = new List<int>();
            if (grave_cards.Count <= 0)
            {
                if ((Bot.HasInSpellZone(CardId.KashtiraBirth, true, true) && hand_cards.Count(card=>card!=null && card.Id ==  CardId.KashtiraBirth)>0)
                    || hand_cards.Count(card => card != null && card.Id == CardId.KashtiraBirth) > 1)
                    cardsid.Add(CardId.KashtiraBirth);
                if ((active_KashtiraPapiyas_1 && hand_cards.Count(card => card != null && card.Id == CardId.KashtiraPapiyas) > 0)
                    || hand_cards.Count(card => card != null && card.Id == CardId.KashtiraPapiyas) > 1)
                    cardsid.Add(CardId.KashtiraPapiyas);
                if (((activate_KashtiraFenrir_1 || summon_KashtiraFenrir) && hand_cards.Count(card => card != null && card.Id == CardId.KashtiraFenrir) > 0)
                    || hand_cards.Count(card => card != null && card.Id == CardId.KashtiraFenrir) > 1)
                    cardsid.Add(CardId.KashtiraFenrir);
                if (((activate_KashtiraUnicorn_1 || summon_KashtiraUnicorn) && hand_cards.Count(card => card != null && card.Id == CardId.KashtiraUnicorn) > 0)
                    || hand_cards.Count(card => card != null && card.Id == CardId.KashtiraUnicorn) > 1)
                    cardsid.Add(CardId.KashtiraUnicorn);
                if ((cardId != CardId.KashtiraScareclaw && hand_cards.Count(card => card != null && card.Id == CardId.KashtiraScareclaw) > 0)
                    || (hand_cards.Count(card => card != null && card.Id == CardId.KashtiraScareclaw) > 1))
                    cardsid.Add(CardId.KashtiraScareclaw);
                if ((activate_KashtiraRiseheart_2 && hand_cards.Count(card => card != null && card.Id == CardId.KashtiraRiseheart) > 0)
                    || hand_cards.Count(card => card != null && card.Id == CardId.KashtiraRiseheart) > 1)
                    cardsid.Add(CardId.KashtiraRiseheart);
                if (cardId != CardId.KashtiraTearlaments && hand_cards.Count(card => card != null && card.Id == CardId.KashtiraTearlaments) > 0)
                    cardsid.Add(CardId.KashtiraTearlaments);
                if(hand_cards.Count(card => card != null && card.Id == CardId.KashtiraBigBang) > 0)
                   cardsid.Add(CardId.KashtiraBigBang);
                if (cardsid.Count <= 0) return false;
            }
            if (Bot.HasInHand(CardId.KashtiraFenrir) && !activate_KashtiraFenrir_1 && Bot.GetMonsterCount() <= 0) return false;
            if (Bot.HasInHand(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1 && Bot.GetMonsterCount() <= 0) return false;
            select_Cards.Clear();
            select_Cards.AddRange(grave_cards);
            select_Cards.AddRange(CardsIdToClientCards(cardsid, hand_cards,false));
            return true;
        }
        private bool KashtiraScareclawEffect()
        {
            if (Card.Location == CardLocation.Hand)
            {
                if (Duel.Player != 0) return false;
                if (Duel.CurrentChain.Count > 0) return false;
                if (!ActivateLimit(Card.Id)) return false;
                activate_KashtiraScareclaw_1 = true;
                return true;
            }
            return false;

        }
        private bool KashtiraAriseHeartEffect()
        {
            if (Card.IsDisabled()) return false;
            if (ActivateDescription == Util.GetStringId(CardId.KashtiraAriseHeart, 1))
            {
                return true;
            }
            else
            {
                return SelectEnemyCard(false,true);
            }
        }
        private bool KashtiraAriseHeartSummon_2()
        {
            int xcount = 0;
            int xcount_2 = 0;
            int xcount_3 = 0;
            foreach (var card in Bot.GetMonsters())
            {
                if (card == null || card.IsFacedown()) continue;
                if (card.Level == 7) ++xcount;
                if (card.Level == 7 && card.HasSetcode(0x189))++xcount_2;
                if (card.Level != 7 && card.HasSetcode(0x189) && !card.HasType(CardType.Xyz)) ++xcount_3;
            }
            if (xcount >= 2 && (xcount_3 > 0 || xcount - xcount_2 > 0)) return KashtiraAriseHeartSummon();
            return false;
        }
        private bool KashtiraAriseHeartSummon()
        {
            if (Bot.HasInMonstersZone(CardId.KashtiraShangriIra,false,false,true) && !activate_KashtiraShangriIra) return false;
            if (activate_KashtiraShangriIra)
            {
                List<ClientCard> materials = Bot.GetMonsters().GetMatchingCards(card => !card.IsExtraCard() && card.IsFaceup() && Card.HasSetcode(0x189)).ToList();
                if (materials.Count() <= 0) return false;
                materials.Sort(CardContainer.CompareCardAttack);
                materials.Sort(CardContainer.CompareCardLevel);
                AI.SelectMaterials(materials);
                return true;
            }
            else
            {
                return DiablosistheMindHackerSummon();
            }
        }
        private bool DefaultSummon()
        {
            if (Bot.HasInSpellZone(CardId.KashtiraBirth, true, true) && Bot.GetMonstersInMainZone().Count<5)
            { 
                isSummoned = true;
                if (Card.Id == CardId.KashtiraUnicorn) summon_KashtiraUnicorn = true;
                else if(Card.Id == CardId.KashtiraFenrir) summon_KashtiraFenrir = true;
                return true; 
            }
            return false;
        }
        private bool KashtiraShangriIraSummon()
        {
            if (Bot.HasInMonstersZone(CardId.KashtiraShangriIra, true, false, true)) return false;
            List<ClientCard> materials = new List<ClientCard>();
            foreach (var card in Bot.GetMonsters())
            {
                if (materials.Count() >= 2) break;
                if (card != null && card.IsFaceup() && card.Level == 7)
                {
                    materials.Add(card);
                }
            }
            if (materials.Count() < 2) return false;
            materials.Sort(CardContainer.CompareCardAttack);
            AI.SelectMaterials(materials);
            return true;
        }
        private List<ClientCard> GetEnemyOnFields()
        {
            List<ClientCard> res = new List<ClientCard>();
            List<ClientCard> m_cards = Enemy.GetMonsters();
            List<ClientCard> s_cards = Enemy.GetSpells();
            if (m_cards.Count > 0) res.AddRange(m_cards);
            if(s_cards.Count > 0) res.AddRange(s_cards);
            return res;
        }
        private bool KashtiraShangriIraEffect()
        {
            if (!Bot.HasInMonstersZone(CardId.KashtiraUnicorn, true, false, true) && Enemy.ExtraDeck.Count > 0 && Bot.HasInDeck(CardId.KashtiraUnicorn))
            {
                AI.SelectCard(CardId.KashtiraUnicorn);
            }
            else if (!Bot.HasInMonstersZone(CardId.KashtiraFenrir, true, false, true) && Bot.HasInDeck(CardId.KashtiraFenrir))
            {
                AI.SelectCard(CardId.KashtiraFenrir);
            }
            else if (!Bot.HasInMonstersZone(CardId.KashtiraScareclaw, true, false, true) && Bot.HasInDeck(CardId.KashtiraScareclaw))
            {
                AI.SelectCard(CardId.KashtiraScareclaw);
            }
            else
            {
                AI.SelectCard(CardId.KashtiraTearlaments, CardId.KashtiraUnicorn, CardId.KashtiraFenrir, CardId.KashtiraScareclaw);
            }
            activate_KashtiraShangriIra = true;
            return true;
            
        }
        private void DefaultAddCardId(List<int> cardsid)
        {
            if (!Bot.HasInHand(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1) cardsid.Add(CardId.KashtiraUnicorn);
            if (!Bot.HasInHand(CardId.KashtiraFenrir) && !activate_KashtiraFenrir_1) cardsid.Add(CardId.KashtiraFenrir);
            if (!Bot.HasInHand(CardId.KashtiraRiseheart) && !activate_KashtiraRiseheart_2) cardsid.Add(CardId.KashtiraRiseheart);
            if (!Bot.HasInHand(CardId.KashtiraTearlaments) && !activate_KashtiraTearlaments_1) cardsid.Add(CardId.KashtiraTearlaments);
            if (!Bot.HasInHand(CardId.KashtiraScareclaw) && !activate_KashtiraScareclaw_1) cardsid.Add(CardId.KashtiraScareclaw);
        }
        private bool KashtiraPapiyasEffect_2()
        {
            if (Card.Location == CardLocation.Removed)
            {
                List<int> cardsid = new List<int>();
                DefaultAddCardId(cardsid);
                if (!Bot.HasInExtra(CardId.KashtiraAriseHeart)) cardsid.Add(CardId.KashtiraAriseHeart);
                if (!Bot.HasInExtra(CardId.KashtiraShangriIra)) cardsid.Add(CardId.KashtiraShangriIra);
                cardsid.AddRange(new List<int>() { CardId.KashtiraBigBang, CardId.KashtiraUnicorn, CardId.KashtiraFenrir, CardId.KashtiraRiseheart, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments });
                AI.SelectCard(cardsid);
                active_KashtiraPapiyas_2 = true;
                return true;
            }
            else
            {
                AI.SelectCard(CardId.KashtiraFenrir);
                List<int> cardsid = new List<int>();
                DefaultAddCardId(cardsid);
                cardsid.AddRange(new List<int>() { CardId.KashtiraUnicorn, CardId.KashtiraFenrir, CardId.KashtiraRiseheart, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments });
                AI.SelectNextCard(cardsid);
                if (Card.Location == CardLocation.Hand)
                {
                    AI.SelectPlace(SelectSTPlace(Card, true));
                }
                active_KashtiraPapiyas_1 = true;
                onlyXyzSummon = true;
                return true;
            }
        }
        private bool KashtiraPapiyasEffect()
        {
            if (link_mode) return false;
            return KashtiraPapiyasEffect_2();
        }
        private bool SelectEnemyCard(bool faceUp = true, bool isXyz= false)
        {
            ClientCard card = Util.GetLastChainCard();
            if (card != null && card.Controller == 1 && card.IsFaceup() && (card.HasType(CardType.Monster)
                || card.HasType(CardType.Continuous) || card.HasType(CardType.Equip) || card.HasType(CardType.Field))
                && (card.Location & CardLocation.Onfield)>0 && !card.IsShouldNotBeTarget())
            { AI.SelectCard(card);if (isXyz)AI.SelectNextCard(card); return true; }
            if (GetEnemyOnFields().Count(_card => _card != null && !_card.IsShouldNotBeTarget() && !(faceUp & !_card.IsFaceup()) && !_card.HasType(CardType.Token)) <= 0) return false;
            ClientCard dcard = GetEnemyOnFields().GetDangerousMonster(true);
            if (Duel.Phase >= DuelPhase.BattleStart || Util.GetBestAttack(Enemy) >= Util.GetBestAttack(Bot) || dcard != null)
            {
                if (dcard != null) { AI.SelectCard(dcard); if(isXyz)AI.SelectNextCard(dcard) ; return true; }
                List<ClientCard> cards = GetEnemyOnFields().Where(_card => _card != null && !_card.IsShouldNotBeTarget() && !(!_card.IsFaceup() & faceUp)).ToList();
                cards.Sort(CardContainer.CompareCardAttack);
                cards.Reverse();
                if (cards.Count <= 0) return false;
                AI.SelectCard(cards);
                if (isXyz) AI.SelectNextCard(cards);
                return true;
            }
            return false;
        }
        private bool KashtiraFenrirEffect()
        {
            if (Card.IsDisabled()) return false;
            if (ActivateDescription == Util.GetStringId(CardId.KashtiraFenrir, 1))
            {
                IList<int> cardsId = new List<int>();
                if ((!Bot.HasInHandOrInSpellZone(CardId.KashtiraBirth) || isSummoned)
                    && !Bot.HasInHand(CardId.KashtiraRiseheart) && (!activate_KashtiraRiseheart_2 && (!activate_KashtiraRiseheart_1 || !isSummoned)) && Bot.HasInDeck(CardId.KashtiraRiseheart))
                    cardsId.Add(CardId.KashtiraRiseheart);
                if (Bot.HasInHandOrInSpellZone(CardId.KashtiraBirth) && !isSummoned && !Bot.HasInHand(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1 && Bot.HasInDeck(CardId.KashtiraUnicorn))
                    cardsId.Add(CardId.KashtiraUnicorn);
                if (!Bot.HasInHand(CardId.KashtiraTearlaments) && !activate_KashtiraTearlaments_1 && Bot.HasInDeck(CardId.KashtiraTearlaments))
                    cardsId.Add(CardId.KashtiraTearlaments);
                if (!Bot.HasInHand(CardId.KashtiraScareclaw) && !activate_KashtiraScareclaw_1 && Bot.HasInDeck(CardId.KashtiraScareclaw))
                    cardsId.Add(CardId.KashtiraScareclaw);
                cardsId.Add(CardId.KashtiraUnicorn);
                cardsId.Add(CardId.KashtiraRiseheart);
                activate_KashtiraFenrir_1 = true;
                AI.SelectCard(cardsId);
                return true;
            }
            else
            {
                if (Duel.LastChainPlayer == 0 && Util.GetLastChainCard() != null &&
                Util.GetLastChainCard().Id == CardId.PrimePlanetParaisos) return false;
                List<ClientCard> cards = GetEnemyOnFields().Where(card => card != null && card.IsFaceup()).ToList();
                if (cards.Count > 0)
                {
                    cards.Sort(CardContainer.CompareCardAttack);
                    cards.Reverse();
                    AI.SelectCard(cards);
                }
                return true;
            }
        }

        private bool TerraformingEffect()
        {
            if (Card.Location == CardLocation.Hand)
            {
                AI.SelectPlace(SelectSTPlace(Card, true));
            }
            return true;
        }

        private bool PotofProsperityEffect()
        {
            if (Bot.ExtraDeck.Count <= 3) return false;
            List<int> cardsId = new List<int>();
            if (!Bot.HasInHandOrInSpellZone(CardId.PrimePlanetParaisos) && !activate_PrimePlanetParaisos)
                cardsId.Add(CardId.PrimePlanetParaisos);
            if (!Bot.HasInHandOrInSpellZone(CardId.PrimePlanetParaisos) && !activate_PrimePlanetParaisos && Bot.HasInDeck(CardId.PrimePlanetParaisos))
                cardsId.Add(CardId.Terraforming);
            if (!Bot.HasInHand(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1)
                cardsId.Add(CardId.KashtiraUnicorn);
            if(!Bot.HasInHand(CardId.KashtiraFenrir) && !activate_KashtiraFenrir_1)
                cardsId.Add(CardId.KashtiraFenrir);
            if (!Bot.HasInHand(CardId.KashtiraPapiyas) && !active_KashtiraPapiyas_1)
                cardsId.Add(CardId.KashtiraPapiyas);
            if (!Bot.HasInHand(CardId.KashtiraRiseheart) && !activate_KashtiraRiseheart_2)
                cardsId.Add(CardId.KashtiraRiseheart);
            if(!Bot.HasInHandOrInSpellZone(CardId.KashtiraBirth))
                cardsId.Add(CardId.KashtiraBirth);
            if(!Bot.HasInHand(CardId.KashtiraScareclaw) && !activate_KashtiraScareclaw_1)
                cardsId.Add(CardId.KashtiraScareclaw);
            if(!Bot.HasInHand(CardId.KashtiraTearlaments) && !activate_KashtiraTearlaments_1)
                cardsId.Add(CardId.KashtiraTearlaments);
            if (Bot.HasInExtra(CardId.ThunderDragonColossus) && Bot.Banished.Count(card=>card!=null && card.IsFaceup() && card.HasType(CardType.Monster))>0)
                cardsId.Add(CardId.NemesesCorridor);
            if (!Bot.HasInHand(CardId.G))
                cardsId.Add(CardId.G);
            if (!Bot.HasInHand(CardId.AshBlossom))
                cardsId.Add(CardId.AshBlossom);
            cardsId.AddRange(new List<int>() { CardId.CrossoutDesignator, CardId.CalledbytheGrave, CardId.Nibiru, CardId.InfiniteImpermanence });
            if (Card.Location == CardLocation.Hand)
            {
                AI.SelectPlace(SelectSTPlace(Card, true));
            }
            AI.SelectCard(cardsId);
            return true;
        }
        private bool KashtiraRiseheartEffect_2()
        {
            if (Card.Location != CardLocation.Hand)
            {
                if (Bot.HasInDeck(CardId.KashtiraBigBang) && Bot.GetMonsters().GetMatchingCards(card => card != null && card.HasType(CardType.Xyz)
                        && card.HasSetcode(0x189) && card.IsFaceup() && card.Overlays.Count > 0).Count > 0)
                {
                    AI.SelectCard(CardId.KashtiraBigBang);
                }
                else if (Bot.HasInHandOrInSpellZone(CardId.KashtiraBirth) && !active_KashtiraBirth)
                {
                    if (!Bot.HasInGraveyardOrInBanished(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1
                        && Bot.HasInDeck(CardId.KashtiraUnicorn) && !active_KashtiraPapiyas_1
                         && !Bot.HasInHand(CardId.KashtiraPapiyas) && Bot.HasInDeck(CardId.KashtiraPapiyas))
                        AI.SelectCard(CardId.KashtiraUnicorn);
                    else if (!Bot.HasInGraveyardOrInBanished(CardId.KashtiraFenrir) && !activate_KashtiraFenrir_1
                        && Bot.HasInDeck(CardId.KashtiraFenrir))
                        AI.SelectCard(CardId.KashtiraFenrir);
                    else if (!Bot.HasInGraveyardOrInBanished(CardId.KashtiraUnicorn) && !activate_KashtiraUnicorn_1
                        && Bot.HasInDeck(CardId.KashtiraUnicorn))
                        AI.SelectCard(CardId.KashtiraFenrir);
                    else if (Bot.Graveyard.Count(card => card != null && card.HasType(CardType.Monster) && card.HasSetcode(0x189) && !card.HasType(CardType.Xyz))
                    + Bot.Banished.Count(card_2 => card_2 != null && card_2.HasType(CardType.Monster) && card_2.HasSetcode(0x189) && !card_2.HasType(CardType.Xyz)) <= 0)
                        AI.SelectCard(CardId.KashtiraFenrir, CardId.KashtiraUnicorn, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments, CardId.KashtiraRiseheart);
                    else
                        AI.SelectCard(CardId.KashtiraFenrir, CardId.KashtiraUnicorn, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments, CardId.KashtiraRiseheart);
                }
                else if (Bot.HasInHand(CardId.NemesesCorridor) && !active_NemesesCorridor &&
                   Bot.Banished.Count(card_2 => card_2 != null && card_2.HasType(CardType.Monster)) <= 0 && Bot.HasInExtra(CardId.ThunderDragonColossus))
                {
                    AI.SelectCard(CardId.KashtiraFenrir, CardId.KashtiraUnicorn, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments, CardId.KashtiraRiseheart);
                }
                else if (!active_KashtiraPapiyas_2 && Bot.HasInDeck(CardId.KashtiraPapiyas) && Bot.Banished.GetMatchingCardsCount(card => card != null && card.IsFaceup() && card.HasSetcode(0x189) && card.Id != CardId.KashtiraPapiyas) > 0)
                {
                    AI.SelectCard(CardId.KashtiraPapiyas, CardId.KashtiraFenrir, CardId.KashtiraUnicorn, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments, CardId.KashtiraRiseheart);
                }
                else
                {
                    AI.SelectCard(CardId.KashtiraFenrir, CardId.KashtiraUnicorn, CardId.KashtiraScareclaw, CardId.KashtiraTearlaments, CardId.KashtiraRiseheart);
                }
                activate_KashtiraRiseheart_2 = true;
                return true;
            }
            return false;
        }
        private bool KashtiraRiseheartEffect()
        {
            if (Card.Location == CardLocation.Hand)
            {
                activate_KashtiraRiseheart_1 = true;
                onlyXyzSummon = true;
                return true;
            }
            return false;
        }
        private bool KashtiraBirthEffect()
        {
            if ((Card.Location == CardLocation.Hand || (Card.Location==CardLocation.SpellZone && Card.IsFacedown()))
                && !Bot.HasInSpellZone(CardId.KashtiraBirth, true, true))
            {
                if (Card.Location == CardLocation.Hand)
                {
                    AI.SelectPlace(SelectSTPlace(Card, true));
                }
                return true;
            }
            return false;
        }
        private bool KashtiraBirthEffect_2()
        {
            if (link_mode) return false;
            return KashtiraBirthEffect_3();
        }
        private bool KashtiraBirthEffect_3()
        {
            if (Card.Location == CardLocation.Hand || (Card.Location == CardLocation.SpellZone && Card.IsFacedown())) return false;
            List<int> cardsid = new List<int>();
            if (!activate_KashtiraUnicorn_1 && !active_KashtiraPapiyas_1 && (Bot.HasInHand(CardId.KashtiraPapiyas) || Bot.HasInDeck(CardId.KashtiraPapiyas))) cardsid.Add(CardId.KashtiraPapiyas);
            if (!activate_KashtiraFenrir_1) cardsid.Add(CardId.KashtiraFenrir);
            if (!activate_KashtiraUnicorn_1) cardsid.Add(CardId.KashtiraUnicorn);
            if (!activate_KashtiraRiseheart_2) cardsid.Add(CardId.KashtiraRiseheart);
            cardsid.Add(CardId.KashtiraFenrir);
            cardsid.Add(CardId.KashtiraUnicorn);
            cardsid.Add(CardId.KashtiraTearlaments);
            cardsid.Add(CardId.KashtiraScareclaw);
            cardsid.Add(CardId.KashtiraRiseheart);
            AI.SelectCard(cardsid);
            return true;
        }
        private bool KashtiraUnicornEffect()
        {
            if (Card.IsDisabled()) return false;
            if (ActivateDescription == Util.GetStringId(CardId.KashtiraUnicorn, 1))
            {
                if ((!Bot.HasInHand(CardId.KashtiraPapiyas) && !active_KashtiraPapiyas_1)
                    ||(Bot.HasInHandOrInSpellZone(CardId.KashtiraBirth) && !Bot.HasInHand(CardId.KashtiraPapiyas)))
                    AI.SelectCard(CardId.KashtiraPapiyas, CardId.KashtiraBirth);
                else
                    AI.SelectCard(CardId.KashtiraBirth, CardId.KashtiraPapiyas);
                activate_KashtiraUnicorn_1 = true;
                return true;
            }
            else 
            {
                return true;
            }
        }

        // ===================== 新增：全盛俱舍卡表里原来没登记的关键卡 =====================
        // 这一段只做「新增规则用到的判断」，不动上面任何一条原有逻辑。

        /// <summary>
        /// 俱舍怒威族·食人魔①：自己场上没有怪兽的场合可以从手牌特殊召唤。
        /// 和独角兽/芬里尔是同一档自跳条件，合法性由核心筛（只有能跳时才会问到这里），
        /// 所以直接答应——多一只 7 星就是多一份香格里拉茧/巨眼的素材。
        /// </summary>
        private bool KashtiraOgreSummon()
        {
            return true;
        }

        /// <summary>
        /// 俱舍怒威族·食人魔的效果。
        /// ②（主要阶段·检索）：这副牌里「俱舍怒威族」陷阱只有六世坏根清净一张，
        ///   它能在场上有「俱舍」超量时把双方场面压到各 1 只——是唯一的清场牌，必须优先拿。
        /// ③（攻击宣言时/对方发动怪兽效果时·里侧除外对方卡组顶 1 张）：不花资源，
        ///   但触发点本身就已经是"对方有动作"，所以直接发；没必要再加条件（加了反而漏掉应发的时机）。
        /// </summary>
        private bool KashtiraOgreEffect()
        {
            if (Card.IsDisabled()) return false;
            if (ActivateDescription == Util.GetStringId(CardId.KashtiraOgre, 1))
            {
                if (!Bot.HasInDeck(CardId.KashtiraBigBang)) return false;
                pendingCardsId.Add(CardId.KashtiraBigBang);
                return true;
            }
            return true;
        }

        /// <summary>
        /// 雷仙神①：支付 3000 基本分从手牌特殊召唤（2700/2400 的 7 星）。
        /// 代价是 3000 基本分，训练局起始只有 4000，所以先看钱包；再确认这只身体**真的能变成超量素材**
        /// （场上已有表侧 7 星，或已经有香格里拉茧/阿莱斯哈特当底子）才付。
        /// 不这么卡的话，起手就掉 3000 血换一只打完架就留下的白板，得不偿失。
        /// </summary>
        private bool ThunderGodSummon()
        {
            if (Bot.LifePoints <= 3000) return false;
            if (Bot.GetFaceupMonsters().Any(card => card != null && card.Level == 7)) return true;
            if (Bot.HasInMonstersZone(CardId.KashtiraShangriIra, true, false, true)
                || Bot.HasInMonstersZone(CardId.KashtiraAriseHeart, true, false, true)) return true;
            return false;
        }

        /// <summary>
        /// 增援：从卡组把 1 只 4 星以下战士族加入手卡。
        /// 这副牌里唯一符合的是莱斯哈特——它是"从卡组除外 1 张「俱舍怒威族」卡、自己变成 7 星"的起手，
        /// 而且 7 星的莱斯哈特正是香格里拉茧最容易凑到的第二只素材。卡组里没有目标就不发。
        /// </summary>
        private bool ReinforcementOfTheArmyEffect()
        {
            if (!Bot.HasInDeck(CardId.KashtiraRiseheart)) return false;
            if (Card.Location == CardLocation.Hand) AI.SelectPlace(SelectSTPlace(Card, true));
            pendingCardsId.Add(CardId.KashtiraRiseheart);
            return true;
        }

        /// <summary>
        /// 封印之黄金柜：从卡组选 1 张除外，第 2 次自己的准备阶段加入手卡。
        /// 这副牌要的是"进除外区"而不是"上手"：
        ///   死灵之颜——被除外的瞬间双方卡组顶 5 张除外（自己的「俱舍」怪进除外区，能被停泊地②/准备①拉回来）；
        ///   「俱舍怒威族」怪——直接躺在除外区等停泊地②或准备①特召，同时把「除外的自己怪兽」数量喂给停泊地。
        /// 卡组里一个目标都没有（都上手/在场了）就不发，免得白白除外一张好牌。
        /// </summary>
        private bool GoldSarcophagusEffect()
        {
            List<int> ids = new List<int>();
            if (Bot.HasInDeck(CardId.Necroface)) ids.Add(CardId.Necroface);
            if (Bot.HasInDeck(CardId.KashtiraUnicorn)) ids.Add(CardId.KashtiraUnicorn);
            if (Bot.HasInDeck(CardId.KashtiraFenrir)) ids.Add(CardId.KashtiraFenrir);
            if (Bot.HasInDeck(CardId.KashtiraOgre)) ids.Add(CardId.KashtiraOgre);
            if (ids.Count <= 0) return false;
            if (Card.Location == CardLocation.Hand) AI.SelectPlace(SelectSTPlace(Card, true));
            pendingCardsId.AddRange(ids);
            return true;
        }

        /// <summary>
        /// 俱舍怒威族的准备（永续陷阱）。
        /// ①（自己·对方回合·速攻）：从手牌以及除外的自己怪兽里特召 1 只「俱舍怒威族」。
        ///   这是把这副牌"被除外"的代价变成场面的正解——独角兽/芬里尔被除外之后本来就是资源。
        ///   只在怪兽区还有空位时发。
        /// ②（对方发动陷阱效果时）：确认对方手卡并里侧除外 1 张。触发点自带"对方有动作"，
        ///   直接发；选哪张手卡由核心/默认选择器处理，这里不额外指定（指定也看不到对方手牌）。
        /// </summary>
        private bool KashtiraPreparationsEffect()
        {
            if (ActivateDescription == Util.GetStringId(CardId.KashtiraPreparations, 1)) return true;
            // ①要先有"能从手牌/除外拉出来的「俱舍」怪"才值得把这张陷阱翻过来；
            // 一个目标都没有时（比如刚盖下、手牌也没怪）就让它继续盖着，别为翻开而翻开。
            bool hasTarget = Bot.Hand.Any(card => card != null && card.HasSetcode(0x189))
                || Bot.Banished.Any(card => card != null && card.IsFaceup() && card.HasSetcode(0x189));
            if (!hasTarget) return false;
            if (Bot.GetMonstersInMainZone().Count >= 5) return false;
            // 优先级：能当素材的 7 星怪 > 能检索的怪 > 莱斯哈特（4 星，要自己变 7 星）
            IList<int> ids = new List<int>
            {
                CardId.KashtiraUnicorn, CardId.KashtiraFenrir, CardId.KashtiraOgre,
                CardId.KashtiraScareclaw, CardId.KashtiraTearlaments, CardId.KashtiraRiseheart
            };
            pendingCardsId.AddRange(ids);
            return true;
        }

        /// <summary>
        /// 香格里拉茧/阿莱斯哈特已经落地之后，还剩下的表侧 7 星怪——泛用 7 星超量的公用素材。
        /// **为什么要这个闸门**：这些泛用件（巨眼、钢炎龙、暗黑武装）跟主终端抢素材，
        /// 主终端没做出来之前绝不能让它们先吃掉两只 7 星。返回 null 表示"没富余，别做"。
        /// </summary>
        private List<ClientCard> SurplusLevel7Materials()
        {
            bool mainBoardDone = Bot.HasInMonstersZone(CardId.KashtiraShangriIra, true, false, true)
                || Bot.HasInMonstersZone(CardId.KashtiraAriseHeart, true, false, true);
            if (!mainBoardDone) return null;
            List<ClientCard> materials = Bot.GetMonsters()
                .Where(card => card != null && card.IsFaceup() && !card.IsExtraCard() && card.Level == 7)
                .ToList();
            if (materials.Count < 2) return null;
            return materials;
        }

        /// <summary>
        /// No.11 巨眼（7 星×2）：抢对方 1 只怪兽。主终端做完、素材有富余时才做。
        /// </summary>
        private bool Number11BigEyeSummon()
        {
            List<ClientCard> materials = SurplusLevel7Materials();
            if (materials == null) return false;
            AI.SelectMaterials(materials.Take(2).ToList());
            return true;
        }

        /// <summary>
        /// 巨眼①：得到对方 1 只怪兽的控制权（这个效果发动的回合这张卡不能攻击）。
        /// 只在对方有能抢的表侧怪兽、且不是"抢过来也没用"的小怪时才发。
        /// </summary>
        private bool Number11BigEyeEffect()
        {
            if (Card.IsDisabled()) return false;
            return SelectEnemyCard(true, false);
        }

        /// <summary>
        /// 真红眼钢炎龙（7 星×2）：2800/2400 的第七星身体，持有素材时对方每次发动效果烧 500。
        /// 本身没有需要选的发动效果（③只能苏生「真红眼」通常怪兽，这副牌没有），所以只登记召唤。
        /// </summary>
        private bool Rank7SurplusSummon()
        {
            List<ClientCard> materials = SurplusLevel7Materials();
            if (materials == null) return false;
            AI.SelectMaterials(materials.Take(2).ToList());
            return true;
        }

        /// <summary>
        /// 击灭龙 暗黑武装（7 星×2 以上）：拔 1 个素材破坏对方 1 张卡，之后从自己墓地除外 1 张。
        /// 额外卡组里少数几张"解场"牌之一，代价只是墓地 1 张卡（这副牌墓地本来就会被阿莱斯哈特除外）。
        /// </summary>
        private bool DarkArmedDragonSummon()
        {
            List<ClientCard> materials = SurplusLevel7Materials();
            if (materials == null) return false;
            // ②效果要"墓地暗属性怪兽正好 5 只"才能叠在 5 星以上龙族上；这里走的是普通 7 星×2 路线，
            // 素材选攻击力最高的两只，把低的留给别的超量/链接。
            materials.Sort(CardContainer.CompareCardAttack);
            materials.Reverse();
            AI.SelectMaterials(materials.Take(2).ToList());
            return true;
        }

        private bool DarkArmedDragonEffect()
        {
            if (Card.IsDisabled()) return false;
            return SelectEnemyCard(true, false);
        }

        /// <summary>
        /// 天霆号 阿宙斯：可以重叠在"这回合打过架的超量怪兽"上（合法性由核心判）。
        /// ①是"场上其他卡全部送去墓地"——会把自己刚做好的场面一起清掉，
        /// 所以只在**我方落后**（对方场面比我们大）时才做；对空场墙永远不做。
        /// </summary>
        private bool ZeusSummon()
        {
            int enemyBoard = Enemy.GetMonsterCount() + Enemy.GetSpellCount();
            int myBoard = Bot.GetMonsterCount() + Bot.GetSpellCount();
            if (enemyBoard < 2 || enemyBoard <= myBoard) return false;
            List<ClientCard> materials = Bot.GetMonsters()
                .Where(card => card != null && card.IsFaceup() && card.HasType(CardType.Xyz))
                .OrderByDescending(card => card.Overlays.Count)
                .ToList();
            if (materials.Count <= 0) return false;
            AI.SelectMaterials(materials.Take(1).ToList());
            return true;
        }

        /// <summary>
        /// 阿宙斯①（速攻）：拔 2 个素材，场上其他卡全送墓。同样是"落后才清场"的闸门——
        /// 领先时清场等于自己拆自己的场（②可以从手卡·卡组·额外补素材，属于被动收益，不用管）。
        /// </summary>
        private bool ZeusEffect()
        {
            if (Card.IsDisabled()) return false;
            if (ActivateDescription == Util.GetStringId(CardId.DivineArsenalAAZeus, 1))
            {
                if (Card.Overlays.Count < 2) return false;
                int enemyBoard = Enemy.GetMonsterCount() + Enemy.GetSpellCount();
                int myBoard = Bot.GetMonsterCount() + Bot.GetSpellCount();
                return enemyBoard >= 2 && enemyBoard > myBoard;
            }
            // ②（场上其他卡被破坏时补 1 个超量素材）：纯收益，触发即发
            return true;
        }

        /// <summary>
        /// S：P小夜骑士（效果怪兽 2 只）：①用融合/同调/超量/连接怪兽当过素材时，
        /// 连接召唤成功可以除外场上或墓地 1 张卡。
        /// 素材只取**非超量**的表侧怪兽——场上那两只超量（香格里拉茧/阿莱斯哈特）是终端，
        /// 绝不能当链接素材吃掉，所以根本没有富余的怪时就不做。
        /// </summary>
        private bool SPLittleKnightSummon()
        {
            List<ClientCard> materials = Bot.GetMonsters()
                .Where(card => card != null && card.IsFaceup() && !card.IsExtraCard() && card.HasType(CardType.Effect))
                .ToList();
            if (materials.Count < 2) return false;
            // 只在主计划已经落地（场上有超量）时才把两只散怪换成小夜骑士；
            // 否则这两只可能正是下一只香格里拉茧的素材。
            if (!Bot.HasInMonstersZone(CardId.KashtiraShangriIra, true, false, true)
                && !Bot.HasInMonstersZone(CardId.KashtiraAriseHeart, true, false, true)) return false;
            materials.Sort(CardContainer.CompareCardAttack);
            AI.SelectMaterials(materials.Take(2).ToList());
            return true;
        }

        /// <summary>
        /// 小夜骑士②（速攻）：把包含自己怪兽的场上 2 只表侧怪兽直到结束阶段除外。
        /// 是"躲效果"用的，只在对方有取对象/破坏类动作之后才发——由连锁点本身保证，
        /// 这里再加一条"对方场上有牌"的判断，避免空场时把自己两只怪白白除外。
        /// </summary>
        private bool SPLittleKnightEffect()
        {
            if (Card.IsDisabled()) return false;
            if (GetEnemyOnFields().Count <= 0) return false;
            List<ClientCard> mine = Bot.GetFaceupMonsters().Where(card => card != null).ToList();
            if (mine.Count <= 0) return false;
            List<ClientCard> target = new List<ClientCard> { Card };
            ClientCard other = mine.FirstOrDefault(card => card != Card && !card.IsShouldNotBeTarget());
            if (other == null) return false;
            target.Add(other);
            AI.SelectCard(target);
            return true;
        }
    }

}
