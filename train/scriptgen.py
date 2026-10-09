"""给一副卡组写 WindBot 出牌脚本（C#），并编译进 WindBot。

为什么需要它：WindBot 自带的原型脚本是按它自己的卡表写死 combo 的，喂群友投稿的卡表会整局
一步不出；通用脚本什么卡表都能打，但打法很泛。这里换成第三条路——**针对这副牌现写一个脚本**：

1. 把卡组的卡（卡号、卡名、类型、攻防、效果文本）+ 上游组合推演（`train/combos/`）
   + 作者额外要求整理成提示词；
2. 让模型按 WindBot 的执行器模板写一个 C# 类；
3. 写进源码树并 `dotnet build`；编译失败就把编译器输出回喂给模型重写（最多几轮）；
4. 编译产物（`bin/Release/WindBot.exe`）里就注册了这份脚本，对局时用 `Deck=<名字>` 调它。

**WindBot 的执行器是编译进 exe 的**（它用反射扫自己程序集上的 `DeckAttribute`），
没有插件式加载，所以必须真的有源码树与 .NET 编译环境——`paths.windbot_src_dir` 没配就
干不了这件事，这一点在这里明说，不做"假装写好了"的兜底。

这份实现是从「游戏王对局管家」的 `duel/script_gen.py` 搬过来的（那套提示词与踩过的坑都验过），
改动只在"接到本插件的训练层"：模型回调是 `train/runner` 那一套、卡表读取走本插件的 CardDatabase。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import asyncio
import logging
import re
import zlib

#: 提示词里最多放多少张卡的效果文本。一副 60 张主卡组 + 15 张额外的牌去重后最多 75 张，
#: 给到 80 就等于"**整副牌都进提示词**"——原来只放 45 张，被砍掉的往往是额外卡组
#: （而原型计划基本都在额外卡组里）。
MAX_CARDS_IN_PROMPT = 80

#: 一次模型调用写几张卡的处理函数。**这个数字是从宿主的 30 秒硬超时倒推出来的**：
#: 一批 8 张 ≈ 200 行 ≈ 2000~2500 token，普通模型 15~25 秒能写完；一批塞十几张就会撞上
#: `cap.call` 的 30 秒上限（撞上就是这一批白写、整份脚本少一块）。
#: 脚本的总长度因此不再受"一次回答"限制，而是**批数 × 每批长度**。
HANDLERS_PER_CALL = 8

#: 单次编译的超时（秒）。本机实测 `dotnet build` 这个工程只要两秒左右，给到 5 分钟是余量。
BUILD_TIMEOUT_SECONDS = 300.0

#: 默认最多「生成 → 编译」几轮。
DEFAULT_MAX_ATTEMPTS = 3

#: 同一批处理函数写错（缺函数名 / 被截断 / 编译报在它身上）时最多重问几次。
BATCH_ATTEMPTS = 2

#: 生成的脚本落在这个子目录（相对源码树）。
GENERATED_SUBDIR = Path("Game") / "AI" / "Decks"

#: 生成脚本的出牌思路名统一用这个前缀，避免与 WindBot 自带的名字相撞。
STYLE_PREFIX = "Gen"

#: 某一批回答被截断时回喂的要求。截断是"这一批没写完"，不是"整份脚本写太长"——
#: 分批之后每批本来就不长，所以这里要求**只写这几张卡、写完整**，不再让它砍覆盖范围。
_TRUNCATED_HINT = (
    "上一次回答被截断了（没有写完就结束）。请重新输出**这一批**的处理函数："
    "函数名照抄、每个都写完整（包含收尾大括号），写完最后一个函数就停。"
    "宁可某个条件写得简单一点，也不要留一个写了一半的函数。"
)

#: 提示词里教给模型的 API 速查。这些签名都是从 WindBot 源码里核对过的，
#: 写错任何一条都会让模型生成编译不过的代码。
API_CHEATSHEET = """\
WindBot 出牌脚本的固定结构（必须照这个形状写，Namespace 与父类都不要改）：

```csharp
using YGOSharp.OCGWrapper.Enums;
using System.Collections.Generic;
using WindBot;
using WindBot.Game;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    [Deck("{style_name}", "AI_{style_name}", "Normal")]
    public class {class_name} : DefaultExecutor
    {
        public class CardId
        {
            public const int SomeCard = 12345678;   // 卡号取自下面给出的清单
        }

        public {class_name}(GameAI ai, Duel duel) : base(ai, duel)
        {
            // 在这里按顺序登记「该做什么」，越靠前优先级越高
            AddExecutor(ExecutorType.Activate, CardId.SomeCard, SomeCardHandler);
            AddExecutor(ExecutorType.SummonOrSet);
            AddExecutor(ExecutorType.SpellSet);
            AddExecutor(ExecutorType.Repos, DefaultMonsterRepos);
        }

        private bool SomeCardHandler()
        {
            // 需要指定目标时用 AI.SelectCard(...)
            return true;
        }
    }
}
```

可用的 API（**只用这些**，签名都是从 WindBot 源码里核对过的，写错就编译不过）：

* 登记动作：`AddExecutor(ExecutorType.类型)`、`AddExecutor(ExecutorType.类型, CardId.某卡)`、
  `AddExecutor(ExecutorType.类型, 处理函数)`、`AddExecutor(ExecutorType.类型, CardId.某卡, 处理函数)`
* `ExecutorType` 可取：`Summon`、`SpSummon`、`Repos`、`MonsterSet`、`SpellSet`、`Activate`、
  `SummonOrSet`、`GoToBattlePhase`、`GoToMainPhase2`、`GoToEndPhase`、`Surrender`
* 可直接复用的现成处理器：`DefaultMonsterRepos`、`DefaultMonsterSummon`、`DefaultSpellSet`、
  `DefaultTrap`、`DefaultMaxxC`、`DefaultAshBlossomAndJoyousSpring`、
  `DefaultInfiniteImpermanence`、`DefaultCalledByTheGrave`、`DefaultSolemnJudgment`
* 处理函数签名是 `private bool Xxx()`：返回 `true` 表示就用这个动作，`false` 表示放弃
  （⚠ 返回 `false` 只是跳过**这一条**登记，不是"禁止这张卡"）
* 选择目标 / 放置 / 表态：`AI.SelectCard(卡号)`、`AI.SelectCard(卡号1, 卡号2, ...)`、
  `AI.SelectCard(ClientCard 对象)`、`AI.SelectCard(IList<ClientCard>)`、
  `AI.SelectPlace(Zones.MainMonsterZones)`（不指定就 `AI.SelectPlace(0)`）、
  `AI.SelectPosition(CardPosition.FaceUpAttack)`、`AI.SelectOption(序号)`、`AI.SelectYesNo(true/false)`
* `Zones` 常量：`Zones.z0`~`Zones.z6`（单格位，`z0` 是最左）、`Zones.MainMonsterZones`、
  `Zones.ExtraMonsterZones`、`Zones.SpellZones`、`Zones.PendulumZones`、`Zones.FieldZone`
* 回合与场面：`Duel.Turn`、`Duel.Player`（0＝自己）、`Duel.Phase` 与
  `DuelPhase.Standby / Main1 / Battle / Main2 / End` 比较
* `Bot`（自己）与 `Enemy`（对手）都是 `ClientField`，可用：`.Hand`、`.MonsterZone[位号]`、
  `.SpellZone[位号]`、`.Graveyard`、`.Deck`（元素是 `ClientCard`），以及
  `HasInHand(卡号)`、`HasInDeck(卡号)`、`HasInGraveyard(卡号)`、`HasInExtra(卡号)`、
  `HasInBanished(卡号)`、`HasInMonstersZone(卡号)`、`HasInSpellZone(卡号)`、
  `GetMonsters()`、`GetSpells()`、`GetMonstersInExtraZone()`（后几个返回 `List<ClientCard>`）
* `ClientCard` 常用成员（**只有这些**）：`Id`、`Alias`、`Attack`、`Defense`、`Level`、`Race`、`Attribute`、
  `Controller`、`Location`、`LinkCount`、`IsCode(卡号)`、`IsMonster()`、`IsSpell()`、`IsTrap()`、
  `IsFaceup()`、`IsFacedown()`、`IsAttack()`、`IsDefense()`、`IsExtraCard()`、
  `HasType(CardType.xxx)`、`HasRace(CardRace.xxx)`、`HasAttribute(CardAttribute.xxx)`
  * 种族/属性/种类判断**必须**写成 `card.HasRace(CardRace.Psycho)`、`card.HasAttribute(CardAttribute.Light)`、
    `card.HasType(CardType.Synchro)` 这种形式
  * `CardRace` 可用值：`Warrior` `SpellCaster` `Fairy` `Fiend` `Zombie` `Machine` `Aqua` `Pyro` `Rock`
    `WindBeast` `Plant` `Insect` `Thunder` `Dragon` `Beast` `BeastWarrior` `Dinosaur` `Fish` `SeaSerpent`
    `Reptile` `Psycho` `DivineBeast` `Wyrm` `Cyberse` `Illusion`（念动力是 `Psycho`，**不是** `Psychic`）
  * `CardAttribute` 可用值：`Earth` `Water` `Fire` `Wind` `Light` `Dark` `Divine`
  * `CardType` 可用值：`Monster` `Spell` `Trap` `Normal` `Effect` `Fusion` `Ritual` `Synchro` `Xyz` `Link`
    `Pendulum` `Tuner` `QuickPlay` `Continuous` `Equip` `Field` `Counter`
  * ⚠ **不要发明方法名**：`c.IsPsychic()` / `c.IsLight()` / `c.IsSynchro()` 这类都不存在，写了就是编译错误
    （真机上连着两轮都栽在这一条）。速查表里没有的判断就用数值字段自己比较：
    "星级 4 以下"写 `c.Level <= 4`、"攻击力够不够"写 `c.Attack >= 2000`
* 工具（执行器上的 `Util`，类型是 `AIUtil`）——**返回类型要看清，别把数值当卡用**：
  * `int Util.GetBestAttack(Bot)` / `int Util.GetBestAttack(Enemy)`：返回的是**攻击力数值**，不是卡
    （要找"场上攻击力最高的那只怪"自己写循环或 `Bot.GetMonsters().OrderByDescending(c => c.Attack).FirstOrDefault()`）
  * `int Util.GetBestPower(ClientField, bool onlyATK)`、`bool Util.IsAllEnemyBetter(bool onlyATK)`、
    `bool Util.IsAllEnemyBetterThanValue(int value, bool onlyATK)`、`bool Util.IsOneEnemyBetter(bool onlyATK)`
  * `ClientCard Util.GetBestEnemySpell(bool onlyFaceup)`（这一个是**卡**，可以直接 `AI.SelectCard(...)`）
  * `List<T> Util.ShuffleList<T>(IList<T>)`、`void Util.ShuffleListInPlace<T>(IList<T>)`
* 需要改"**默认**怎么选"（不是某一张卡的效果）时才重写基类方法，签名与返回类型照抄，
  并且处理不了的情况要 `return base.方法(...)`：
  * `public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)`
  * `public override IList<ClientCard> OnSelectTribute(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)`
  * `public override IList<ClientCard> OnSelectFusionMaterial(IList<ClientCard> cards, int min, int max)`
    （以及 `OnSelectSynchroMaterial` / `OnSelectXyzMaterial` / `OnSelectLinkMaterial` / `OnSelectRitualTribute`，签名相同）
  * `public override int OnSelectPlace(int cardId, int player, CardLocation location, int available)`
  * `public override BattlePhaseAction OnBattle(IList<ClientCard> attackers, IList<ClientCard> defenders)`
  * `public override BattlePhaseAction OnSelectAttackTarget(ClientCard attacker, IList<ClientCard> defenders)`
  * `public override bool OnSelectYesNo(int desc)`
* 需要防止空引用：场面位置可能是 `null`，取值前先判空
"""


class ScriptGenerationError(RuntimeError):
    """生成或编译出牌脚本失败。"""


@dataclass(frozen=True)
class CardInfo:
    """提示词里描述一张卡所需的信息。"""

    card_id: int
    name: str
    zone: str = "主卡组"
    type_text: str = ""
    stats: str = ""
    effect: str = ""

    def describe(self) -> str:
        """渲染成一行，供提示词使用。"""

        parts = [f"{self.card_id} {self.name}（{self.zone}"]
        if self.type_text:
            parts.append(f"· {self.type_text}")
        parts.append("）")
        head = "".join(parts)
        if self.stats:
            head += f" {self.stats}"
        line = f"- {head}"
        if self.effect:
            line += f"\n  效果：{self.effect}"
        return line


@dataclass
class DeckScriptRequest:
    """生成一副卡组的脚本所需的一切。"""

    deck_id: int
    deck_name: str
    cards: List[CardInfo]
    combo_guide: str = ""
    """上游的 combo 推演（`train/combos/*.txt`）。有它，脚本里的展开顺序才不是模型瞎猜的。"""

    extra_prompt: str = ""
    """作者额外要求（面板上填的那一栏）：想强调什么打法、禁掉什么行为都写在这。"""

    feedback: str = ""
    """上一轮擂台/复盘得到的结论，作为这一轮的修改要求（自动迭代就靠它回喂）。"""

    previous_code: str = ""
    """上一版脚本的源码（自动迭代的上一轮产物）。

    有它的时候是"**改**这份脚本"，不是"从零重写"——迭代要真的越改越好，模型就得先看见自己
    上一轮写了什么。留空时由生成器自己去找这副牌已有的脚本文件当基座。
    """


@dataclass
class GeneratedScript:
    """一次成功的生成结果。"""

    style_name: str
    file_path: Path
    code: str
    attempts: int = 1
    build_output: str = ""
    warnings: List[str] = field(default_factory=list)


def collect_card_info(card_db: Any, main: Sequence[int], extra: Sequence[int]) -> List[CardInfo]:
    """把卡组整理成生成脚本用的卡牌清单（含效果文本）。

    提示词里"有没有效果文本"直接决定模型能不能写对处理函数：只给卡号它只能瞎猜，
    给了卡文它才知道这张卡是检索、特召还是阻抗。
    """

    if card_db is None or not getattr(card_db, "available", False):
        return []
    try:
        details = card_db.card_details(list(main) + list(extra))
    except Exception:  # noqa: BLE001  卡库读不动时返回空清单，由调用方决定怎么报
        return []
    cards: List[CardInfo] = []
    for zone, card_ids in (("主卡组", dict.fromkeys(main)), ("额外卡组", dict.fromkeys(extra))):
        for card_id in card_ids:
            info = details.get(int(card_id))
            if info is None:
                continue
            cards.append(
                CardInfo(
                    card_id=int(card_id),
                    name=str(info.name),
                    zone=zone,
                    type_text=str(info.type_text),
                    stats=str(info.stats),
                    effect=str(info.effect),
                )
            )
    return cards


class DeckScriptGenerator:
    """把一副卡组变成 WindBot 能用的出牌脚本。

    Args:
        generate: 模型调用回调，签名 ``async (prompt) -> str``（超时由调用方那层执行）。
        source_dir: WindBot 源码树目录（含 WindBot.csproj）。
        windbot_dir: WindBot 运行目录（含 Decks/，生成的卡表也写到这里）。
        style_name: 生成脚本的出牌思路名（`Deck("名字")`），留空按卡组编号生成。
        max_attempts: 生成 → 编译的最大轮数。
        logger: 日志器。
    """

    def __init__(
        self,
        generate: Callable[[str], Awaitable[str]],
        *,
        source_dir: Path,
        windbot_dir: Path,
        style_name: str = "",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        build_timeout: float = BUILD_TIMEOUT_SECONDS,
        dotnet: str = "dotnet",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._generate = generate
        self._source_dir = Path(source_dir)
        self._windbot_dir = Path(windbot_dir)
        self._style_name = style_name
        self._max_attempts = max(1, int(max_attempts))
        self._build_timeout = build_timeout
        self._dotnet = dotnet
        self._logger = logger or logging.getLogger(__name__)
        self._retry_batches: List[int] = []
        """下一次编译失败时只重问哪几批处理函数（由 :meth:`generate` 按报错行号填）。"""

    @staticmethod
    def style_name_for(deck_id: int) -> str:
        """给一副卡组生成出牌思路名（同时用作类名、文件名与 Deck 属性名）。

        用纯 ASCII：它要出现在命令行参数里，也要当 C# 类名，保持 ASCII 最省事。
        """

        return f"{STYLE_PREFIX}{int(deck_id)}"

    @staticmethod
    def class_name_for(style_name: str) -> str:
        """由出牌思路名推出 C# 类名（中文之类不能当标识符时用 CRC 生成稳定后缀）。"""

        cleaned = re.sub(r"[^A-Za-z0-9_]", "", style_name)
        if not cleaned:
            cleaned = f"Deck{zlib.crc32(style_name.encode('utf-8')) & 0xFFFFFF:06x}"
        return f"{cleaned}Executor"

    def ensure_ready(self) -> None:
        """检查源码树与运行目录是否具备生成条件。

        Raises:
            ScriptGenerationError: 缺哪一样就说哪一样——不做"假装写好了"的兜底。
        """

        project = self._source_dir / "WindBot.csproj"
        if not project.is_file():
            raise ScriptGenerationError(
                f"源码树里找不到 WindBot.csproj：{project}（配置 paths.windbot_src_dir）"
            )
        if not (self._source_dir / GENERATED_SUBDIR).is_dir():
            raise ScriptGenerationError(f"源码树结构不对，缺少目录：{self._source_dir / GENERATED_SUBDIR}")
        if not (self._windbot_dir / "Decks").is_dir():
            raise ScriptGenerationError(f"WindBot 运行目录里没有 Decks/：{self._windbot_dir}")

    async def generate(self, request: DeckScriptRequest) -> GeneratedScript:
        """生成脚本并编译。

        **分两段写**，这是"第一次就写出大脚本"的关键：

        1. **骨架由代码拼**（不花模型额度、不可能写错）：`CardId` 常量表、每张卡登记哪几种动作、
           末尾的通用兜底行为，都是按卡表与卡文确定性生成的；
        2. **处理函数分批让模型写**：每批 `HANDLERS_PER_CALL` 张卡。宿主的单次模型调用有
           **30 秒硬超时**（`cap.call`），一次让它写完四十张卡必然被掐断；分批之后每批都在预算内，
           脚本总长度变成"批数 × 每批长度"——所以第一次就能写足量。

        编译失败时按报错行号定位到**是哪一批**，只重问那一批。某一批最终没写成的卡，
        不登记它的 `Activate`（没判断就"能发就发"比少做一件事更糟），并在结果的
        `warnings` 里如实列出。

        Raises:
            ScriptGenerationError: 重试用尽仍没编译通过、或源码树不可用时抛出。
        """

        self.ensure_ready()
        style_name = self._style_name or self.style_name_for(request.deck_id)
        class_name = self.class_name_for(style_name)
        target = self._source_dir / GENERATED_SUBDIR / f"{class_name}.cs"

        header, cards = self._layout(request)
        batches = self._handler_batches(cards)
        # 上一版的源码：显式给的优先，否则用这副牌已有的那份（"再写一次"＝改进而不是重写）
        base_code = str(request.previous_code or "").strip() or self._read_existing(target)
        base_handlers = self._handlers_in(base_code, batches)

        written: Dict[int, str] = {}
        batch_error = ""
        batch_lines: List[Tuple[int, int, int]] = []
        last_code = ""
        self._retry_batches = list(range(len(batches)))

        for round_index in range(1, max(1, self._max_attempts) + 1):
            # 第一轮写全部批次；之后只补"还没写成的"和"编译报错落在它身上的"那几批
            broken = set(self._retry_batches)
            self._retry_batches = []
            targets = [
                index
                for index, batch in enumerate(batches)
                if round_index == 1
                or index in broken
                or any(card.card_id not in written for card in batch)
            ]
            fresh = dict(written)
            for index in targets:
                batch = batches[index]
                # 这一批上一版长什么样：优先用本轮已经写出来的那份（让它改自己的代码），
                # 没有才是最初给的基座（上一版脚本）
                previous = "\n\n".join(
                    written[card.card_id] for card in batch if written.get(card.card_id)
                ) or base_handlers.get(index, "")
                try:
                    fresh.update(
                        await self._write_batch(
                            request,
                            batch,
                            previous_handlers=previous,
                            previous_error=batch_error if round_index > 1 or index in broken else "",
                        )
                    )
                except ScriptGenerationError as exc:
                    # 这一批没写成：其余批次照旧，最后在 warnings 里说明少了哪几张
                    batch_error = str(exc)
                    if self._logger is not None:
                        self._logger.warning("第 %s 轮这一批没写成：%s", round_index, exc)
            written = fresh
            code, batch_lines = self._assemble(header, cards, batches, written)
            last_code = code
            if not written:
                # 一张卡的处理函数都没写成：这份代码只有骨架，编译它没有意义
                continue
            target.write_text(code, encoding="utf-8")
            self._write_deck_file(style_name, request)
            ok, output = await self._build()
            if ok:
                missing = self._missing_cards(cards, written)
                warnings = self._warnings(cards, written, base_code)
                if missing:
                    warnings.append(
                        "这些卡没写出处理函数，只登记了通用行为：" + "、".join(missing)
                    )
                return GeneratedScript(
                    style_name=style_name,
                    file_path=target,
                    code=code,
                    attempts=round_index,
                    build_output=output,
                    warnings=warnings,
                )
            batch_error = output
            broken = self._broken_batches(output, batch_lines)
            if not broken:
                # 报错不在任何一批处理函数里 → 是骨架或编译环境的问题，不是模型的锅，
                # 再问几轮也没用，直接如实抛出来
                raise ScriptGenerationError(
                    f"生成的骨架编译不过（报错不在处理函数里，属于插件的问题）：\n{output.strip()[:800]}"
                )
            self._retry_batches = [
                index for index in range(len(batches)) if index in broken
            ]
            # 把报错那几行**原文**放最前面（后面才是 msbuild 的原始输出）：只给"第 911 行类型不匹配"
            # 它还得自己去数行，实测会照着原来的写法再写一遍（真机上就是这么连着两轮报同一个错的）
            excerpt = self._error_excerpt(last_code, output)
            batch_error = (excerpt + "\n\n" if excerpt else "") + output
            if self._logger is not None:
                head = " ".join(line.strip() for line in output.splitlines() if "error" in line.lower())[:300]
                self._logger.warning(
                    "第 %s 轮编译失败，只重问第 %s 批：%s",
                    round_index,
                    "、".join(str(index + 1) for index in self._retry_batches),
                    head or output[:200],
                )

        # 走到这里说明没编译通过：**先把上一版还回去**——卡组池里记的还是这个名字，
        # 源码树里没有这个类时，对局按 `Deck=<名字>` 找不到它，WindBot 会静默换一个随机
        # 执行器顶上（"用了错的脚本还看不出来"就是这么来的）。第一次写（本来就没有上一版）
        # 才真的删掉，免得半份脚本留在源码树里：
        # 绝不能留在源码树里——csproj 会把 Game/AI/Decks 下的所有 .cs 都编进去，
        # 这份失败的尝试就成了同一个类的第二份定义，之后每轮编译都报 CS0101/CS0579
        # （实测把三轮重试全毒死了）。
        if str(base_code or "").strip():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(base_code, encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        # 失败的那一份另存一份给人看：想弄清"模型到底写了什么"只能靠它（放在运行目录下，不参与编译）
        failed_path = self._failed_dir() / f"{target.stem}.failed.cs"
        if last_code:
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(last_code, encoding="utf-8")
        reason = batch_error.strip()[:800] or "（没有拿到可编译的代码）"
        raise ScriptGenerationError(
            f"生成 {self._max_attempts} 轮都没能编译通过"
            + (
                "（已把上一版脚本原样放回，对局不受影响）"
                if str(base_code or "").strip()
                else "，最后一段代码没被采用"
            )
            + (f"（失败的这一版留在 {failed_path}，可以直接打开看）" if last_code else "")
            + f"。最后的失败原因：\n{reason}"
        )

    # ---------------------------------------------------------------- 内部实现

    def _failed_dir(self) -> Path:
        """失败尝试的存放目录（在 WindBot 运行目录下，不参与编译）。"""

        return self._windbot_dir / "FailedScripts"

    def exported_exe(self) -> Path:
        """编译产物在哪（`bin/Release/WindBot.exe`）。

        对局用的是这一份（`tools/style_ab.py` 与插件都优先找它），所以"编译成功"之后
        要拿它去验证脚本真的注册上了。
        """

        return self._source_dir / "bin" / "Release" / "WindBot.exe"

    # ---------------------------------------------------------------- 骨架（确定性部分）

    @staticmethod
    def _card_ident(card_id: int) -> str:
        """卡号在脚本里的常量名。

        用卡号而不是卡名：卡名是中文/日文，当 C# 标识符容易出岔子（编码、重名、特殊字符），
        而模型写的处理函数要**一字不差**地对上登记表——用卡号最不容易错。卡名写在注释里。
        """

        return f"Card{int(card_id)}"

    @classmethod
    def _handler_name(cls, card_id: int) -> str:
        """这张卡的处理函数名（与登记表里的名字必须一致）。"""

        return f"{cls._card_ident(card_id)}Handler"

    @staticmethod
    def _executor_types(card: CardInfo) -> List[str]:
        """按卡的种类决定登记哪几种动作（顺序即优先级）。

        这不是判断"这张卡该什么时候用"（那正是模型要写的处理函数），只是"内核在什么时候
        会来问这张卡"：怪兽能被通常召唤、魔陷能被发动/盖放、额外卡组的能被特殊召唤。
        登记得全一点没坏处——处理函数判完条件返回 `false` 就是了，漏登记则是"这张卡永远不会动"。
        """

        if card.zone == "额外卡组":
            return ["SpSummon", "Activate"]
        if card.type_text.startswith("陷阱"):
            return ["SpellSet", "Activate"]
        if card.type_text.startswith("魔法"):
            return ["Activate"]
        return ["Activate", "SummonOrSet"]

    @staticmethod
    def _needs_handler(card: CardInfo) -> bool:
        """要不要给这张卡单独写处理函数。

        没有效果文本的卡（通常怪兽、白板额外）不需要：它的"处理函数"没有内容可写，
        交给我下面那条通用兜底（`SummonOrSet` / `SpellSet` / `Repos`）就够了。
        """

        return bool(str(card.effect or "").strip())

    @staticmethod
    def _fallback_keeps(kind: str) -> bool:
        """没写成处理函数时，这种动作还登不登记。

        `Activate` 不登记：登记了就是"内核一问就发"，而这张卡恰恰是没人判断过的
        （实测"无脑发动"比"少做一件事"更伤）。召唤/盖放这类动作照旧登记，
        至少保证这张卡还能被拍出来。
        """

        return kind != "Activate"

    def _layout(self, request: DeckScriptRequest) -> Tuple[List[str], List[CardInfo]]:
        """生成脚本的"头"（usings / 类 / CardId 常量），并给出要处理的卡。

        构造函数与处理函数由 :meth:`_assemble` 拼——因为**登记表要知道哪些处理函数真的写出来了**
        （没写出来的卡不能引用一个不存在的函数名，否则编译报错还找不到是谁的锅）。
        """

        cards = list(request.cards)[:MAX_CARDS_IN_PROMPT]
        style_name = self._style_name or self.style_name_for(request.deck_id)
        class_name = self.class_name_for(style_name)
        header = [
            "using YGOSharp.OCGWrapper.Enums;",
            "using System.Collections.Generic;",
            "using System.Linq;",
            "using WindBot;",
            "using WindBot.Game;",
            "using WindBot.Game.AI;",
            "",
            "namespace WindBot.Game.AI.Decks",
            "{",
            f'    [Deck("{style_name}", "AI_{style_name}", "Normal")]',
            f"    public class {class_name} : DefaultExecutor",
            "    {",
            "        public class CardId",
            "        {",
        ]
        for card in cards:
            note = card.name if not card.type_text else f"{card.name}（{card.type_text}）"
            header.append(f"            public const int {self._card_ident(card.card_id)} = {card.card_id};  // {note}")
        header += ["        }", "", f"        public {class_name}(GameAI ai, Duel duel) : base(ai, duel)", "        {"]
        return header, cards

    def _constructor_lines(self, cards: Sequence[CardInfo], written: Dict[int, str]) -> List[str]:
        """构造函数里的登记表（按卡表顺序＝优先级顺序）。"""

        lines: List[str] = []
        for card in cards:
            types = self._executor_types(card)
            if not self._needs_handler(card):
                # 白板卡没有可判断的东西：只登记"能被拍出来/盖下去"，不登记发动
                types = [kind for kind in types if kind != "Activate"]
                lines += [f"            AddExecutor(ExecutorType.{kind}, CardId.{self._card_ident(card.card_id)});" for kind in types]
                continue
            has_handler = bool(written.get(card.card_id))
            for kind in types:
                if has_handler:
                    lines.append(
                        f"            AddExecutor(ExecutorType.{kind}, CardId.{self._card_ident(card.card_id)},"
                        f" {self._handler_name(card.card_id)});"
                    )
                elif self._fallback_keeps(kind):
                    lines.append(f"            AddExecutor(ExecutorType.{kind}, CardId.{self._card_ident(card.card_id)});")
        lines += [
            "            // 通用兜底：保证任何场面都能动起来（不依赖上面那些判断）",
            "            AddExecutor(ExecutorType.SummonOrSet);",
            "            AddExecutor(ExecutorType.SpellSet);",
            "            AddExecutor(ExecutorType.Repos, DefaultMonsterRepos);",
            "        }",
        ]
        return lines

    def _handler_batches(self, cards: Sequence[CardInfo]) -> List[List[CardInfo]]:
        """把要写处理函数的卡分批（每批 `HANDLERS_PER_CALL` 张）。"""

        pending = [card for card in cards if self._needs_handler(card)]
        return [
            pending[index : index + HANDLERS_PER_CALL]
            for index in range(0, len(pending), HANDLERS_PER_CALL)
        ]

    def _assemble(
        self,
        header: List[str],
        cards: Sequence[CardInfo],
        batches: Sequence[Sequence[CardInfo]],
        written: Dict[int, str],
    ) -> Tuple[str, List[Tuple[int, int, int]]]:
        """拼出整份代码，返回 ``(代码, 每批占用的行号区间)``。

        行号区间用来在编译失败时定位"是哪一批写坏了"：编译器报的是 `文件(行,列)`，
        拿它跟区间一比就知道该重问哪一批，而不是整份重来。
        """

        lines = list(header) + self._constructor_lines(cards, written) + [""]
        batch_lines: List[Tuple[int, int, int]] = []
        for index, batch in enumerate(batches):
            start = len(lines) + 1
            for card in batch:
                code = written.get(card.card_id, "").strip()
                if not code:
                    continue
                lines.extend(self._indent(code))
                lines.append("")
            batch_lines.append((index, start, max(start, len(lines))))
        lines += ["    }", "}", ""]
        return "\n".join(lines), batch_lines

    @staticmethod
    def _indent(code: str) -> List[str]:
        """把模型给的函数体对齐到类里（缩进不统一只影响观感，但这份代码是给人看的）。"""

        rows = str(code).splitlines()
        widths = [len(row) - len(row.lstrip()) for row in rows if row.strip()]
        shift = 8 - (min(widths) if widths else 8)
        if shift == 0:
            return rows
        if shift > 0:
            return [(" " * shift + row) if row.strip() else row for row in rows]
        return [row[min(-shift, len(row) - len(row.lstrip())) :] if row.strip() else row for row in rows]

    def _read_existing(self, target: Path) -> str:
        """读这副牌已有的脚本（没有就空串）：再写一次＝在它基础上改。"""

        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _handlers_in(
        self, code: str, batches: Sequence[Sequence[CardInfo]]
    ) -> Dict[int, str]:
        """从上一版代码里挑出每批卡的处理函数（迭代时当"改之前的版本"用）。"""

        text = str(code or "")
        if not text.strip():
            return {}
        result: Dict[int, str] = {}
        for batch in batches:
            names = [self._handler_name(card.card_id) for card in batch]
            found, _missing = self._extract_handlers(text, names)
            for card in batch:
                name = self._handler_name(card.card_id)
                if found.get(name):
                    result[card.card_id] = found[name]
        return result

    def _missing_cards(self, cards: Sequence[CardInfo], written: Dict[int, str]) -> List[str]:
        """哪些卡最终没写出处理函数（要在结果里如实说）。"""

        return [
            f"{card.name}（{card.card_id}）"
            for card in cards
            if self._needs_handler(card) and not written.get(card.card_id)
        ]

    @staticmethod
    def _warnings(cards: Sequence[CardInfo], written: Dict[int, str], base_code: str) -> List[str]:
        """结果里要带的说明（改的还是从零写的、额外卡组的卡有没有被提示词截掉）。"""

        warnings: List[str] = []
        if str(base_code or "").strip():
            warnings.append("这一版是在已有脚本基础上改的（不是从零重写）")
        if len(cards) >= MAX_CARDS_IN_PROMPT:
            warnings.append(f"卡表超过 {MAX_CARDS_IN_PROMPT} 张，只处理了前 {MAX_CARDS_IN_PROMPT} 张")
        return warnings

    @staticmethod
    def count_handlers(code: str) -> int:
        """数一数脚本里写了多少个处理函数（面板与记录用它显示"写了多少"）。"""

        return len(re.findall(r"private\s+bool\s+\w+\s*\(\s*\)", str(code or "")))

    # ---------------------------------------------------------------- 处理函数（模型写）

    async def _write_batch(
        self,
        request: DeckScriptRequest,
        batch: Sequence[CardInfo],
        *,
        previous_handlers: str,
        previous_error: str,
    ) -> Dict[int, str]:
        """写一批卡的处理函数，最多试 `BATCH_ATTEMPTS` 次。

        Raises:
            ScriptGenerationError: 这批卡最终没写成（原因在里面，调用方会如实记进 warnings）。
        """

        names = [self._handler_name(card.card_id) for card in batch]
        error = previous_error
        found: Dict[int, str] = {}
        for attempt in range(1, BATCH_ATTEMPTS + 1):
            prompt = self._build_handler_prompt(
                request,
                batch,
                previous_handlers=previous_handlers,
                previous_error=error,
            )
            try:
                raw = await self._generate(prompt)
            except Exception as exc:  # noqa: BLE001  模型那层的问题要带上"是模型/宿主的问题"
                error = f"调用模型失败：{exc}"
                if self._logger is not None:
                    self._logger.warning("这一批第 %s 次调用模型失败：%s", attempt, exc)
                continue
            if not str(raw or "").strip():
                error = "模型返回了空内容（没回话）。换一只不思考的模型再来。"
                continue
            if self._truncated(raw):
                error = _TRUNCATED_HINT
                if self._logger is not None:
                    self._logger.warning("这一批第 %s 次回答被截断（%s 字符）", attempt, len(raw))
                continue
            got, missing = self._extract_handlers(str(raw), names)
            found.update({card.card_id: got[self._handler_name(card.card_id)] for card in batch if got.get(self._handler_name(card.card_id))})
            if not missing:
                return found
            error = "下面这些处理函数没有写出来（函数名要一字不差）：" + "、".join(missing)
            if self._logger is not None:
                self._logger.warning("这一批第 %s 次少了函数：%s", attempt, "、".join(missing))
        if found:
            # 试了两轮还是缺几张：**先把写出来的用上**，缺的那几张会在结果里如实列出，
            # 下一轮（如果还有）再补——整批丢掉等于白白浪费掉已经写好的部分
            return found
        raise ScriptGenerationError(f"这一批（{len(batch)} 张卡）{BATCH_ATTEMPTS} 次都没写成：{error}")

    def _build_handler_prompt(
        self,
        request: DeckScriptRequest,
        batch: Sequence[CardInfo],
        *,
        previous_handlers: str,
        previous_error: str,
    ) -> str:
        """写一批处理函数用的提示词。"""

        style_name = self._style_name or self.style_name_for(request.deck_id)
        lines = [
            "你是 WindBot（一个游戏王对局机器人）的出牌脚本作者。",
            f"这副牌（{request.deck_name}）的脚本骨架已经写好了：卡号常量、登记表、通用兜底都在。",
            f"你**只写下面这 {len(batch)} 张卡的处理函数**，别的东西一行都不要再写（别输出 using、"
            "namespace、类壳、CardId 常量或构造函数）。",
            "",
            "这一批的卡（编号 卡名（所在卡组·类型）攻防 与效果）：",
        ]
        for card in batch:
            lines.append(card.describe())
        lines += ["", "要你写的函数（**函数名一字不差**，每个都必须有）："]
        for card in batch:
            kinds = "、".join(self._executor_types(card))
            lines.append(
                f"- `private bool {self._handler_name(card.card_id)}()`  ← {card.name}"
                f"（卡号 {card.card_id}，内核会在这些时点问它：{kinds}）"
            )
        lines += [
            "",
            "整份脚本的登记顺序（越靠前优先级越高，处理函数返回 false 就跳过这一条登记）：",
            "```csharp",
        ]
        for card in request.cards[:MAX_CARDS_IN_PROMPT]:
            mark = "   // ← 这一张由你写" if card in batch else ""
            for kind in self._executor_types(card):
                lines.append(
                    f"AddExecutor(ExecutorType.{kind}, CardId.{self._card_ident(card.card_id)});{mark}"
                )
        lines += [
            "AddExecutor(ExecutorType.SummonOrSet);",
            "AddExecutor(ExecutorType.SpellSet);",
            "AddExecutor(ExecutorType.Repos, DefaultMonsterRepos);",
            "```",
            "",
            "硬性要求：",
            "1. **只输出函数本身**（`private bool …() { … }`），不要代码块以外的解释文字，也不要任何别的东西。",
            "2. 只用下面速查表里列出的 API，以及 `CardId.` 开头的常量；卡号必须来自上面的清单。",
            "3. 每个函数都要判空、判场面：找不到目标或条件不成立就 `return false`，绝不能抛异常。",
            "4. **一定要写选择逻辑**：效果要取对象时用 `AI.SelectCard(...)`（候选按优先级排成 "
            "`AI.SelectCard(a, b, c)`），要放格子时用 `AI.SelectPlace(...)`，"
            "要表态时用 `AI.SelectPosition(...)` / `AI.SelectYesNo(...)` / `AI.SelectOption(...)`。"
            "只写「能发就发」而不写选谁，等于把关键决定交给默认启发式——那正是这份脚本存在的意义。",
            "5. `SummonOrSet` 的处理函数是「**现在要不要把这张卡通常召唤/盖下去**」："
            "只有它确实是启动点、且现在就该用它展开时才 `return true`；不确定就 `return false`。",
            "6. `Activate` 的处理函数是「这张卡现在发动/用不用」：手坑与阻抗要写清触发条件"
            "（谁的回合、对面场面够不够、自己有没有更该留的后手、这张是不是该压到关键时点）；"
            "展开件要写清「现在是不是展开的下一步」——不确定就 `return false`（留着比乱发好）。",
            "7. 每张卡都要把卡文里的效果**逐条覆盖**：检索、特召、破坏、取对象、抽卡、续航、"
            "给自己留后手，各写一段判断；有条件先做加分项（比如先看对面有没有反击、自己墓地有没有资源）。",
            "8. 写完这一批就停（最后一行是最后一个函数的收尾大括号）。",
        ]
        if request.combo_guide.strip():
            lines += [
                "",
                "这副牌的 combo 推演（**优先按这个顺序实现**，它是按卡文推出来的）：",
                request.combo_guide.strip()[:4000],
            ]
        if request.extra_prompt.strip():
            lines += ["", "作者的额外要求（必须遵守）：", request.extra_prompt.strip()[:2000]]
        if request.feedback.strip():
            lines += [
                "",
                "上一轮实测发现的问题（这一轮要改掉，别的地方保持不动）：",
                request.feedback.strip()[:4000],
            ]
        if previous_handlers.strip():
            lines += [
                "",
                "这一批卡**上一版**的处理函数（在它们的基础上按上面的要求改，不要从头重写）：",
                "```csharp",
                previous_handlers.strip()[:6000],
                "```",
            ]
        if previous_error.strip():
            lines += ["", "上一次这一批没通过，原因如下（含报错处的代码原文），请修正后重新输出整批函数：", previous_error.strip()[:4000]]
        lines += ["", "可用的 API（只用这些）：", API_CHEATSHEET.replace("{style_name}", style_name)]
        return "\n".join(lines)

    # ---------------------------------------------------------------- 解析模型输出

    @staticmethod
    def _slice_function(text: str, name: str) -> str:
        """从一段代码里抠出某个函数的完整源码（按大括号配对，跳过注释与字符串）。"""

        match = re.search(rf"private\s+bool\s+{re.escape(name)}\s*\(\s*\)", text)
        if match is None:
            return ""
        # 从行首开始切：不然取出来的函数会顶格，拼进类里看着像被截断过
        line_start = text.rfind("\n", 0, match.start()) + 1
        start = text.find("{", match.end())
        if start < 0:
            return ""
        depth = 0
        index = start
        in_string = False
        in_line_comment = False
        while index < len(text):
            char = text[index]
            if in_line_comment:
                if char == "\n":
                    in_line_comment = False
            elif in_string:
                if char == "\\":
                    index += 1
                elif char == '"':
                    in_string = False
            elif char == "/" and index + 1 < len(text) and text[index + 1] == "/":
                in_line_comment = True
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[line_start : index + 1]
            index += 1
        return ""

    @classmethod
    def _extract_handlers(cls, raw: str, names: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
        """从回答里取出要求的那几个处理函数，返回 ``(找到的, 缺的)``。"""

        text = str(raw or "")
        fenced = re.search(r"```(?:csharp|cs|C#)?\s*(.*?)(?:```|\Z)", text, re.S)
        body = fenced.group(1) if fenced else text
        found: Dict[str, str] = {}
        missing: List[str] = []
        for name in names:
            code = cls._slice_function(body, name) or cls._slice_function(text, name)
            if code:
                found[name] = code
            else:
                missing.append(name)
        return found, missing

    @staticmethod
    def _error_excerpt(code: str, output: str, context: int = 3) -> str:
        """把编译器报错的那几行代码原文摘出来（±`context` 行），附在回喂内容后面。

        模型没法从"第 911 行"直接看到问题代码；把它贴出来，改起来才不会照着原样重写。
        """

        rows = str(code or "").splitlines()
        picked: List[str] = []
        for line in DeckScriptGenerator._error_lines(output):
            if not 1 <= line <= len(rows):
                continue
            start = max(1, line - context)
            end = min(len(rows), line + context)
            picked.append(f"第 {line} 行附近（报错行用 ← 标出）：")
            picked += [
                f"    {number:4} {rows[number - 1]}{'  ←' if number == line else ''}"
                for number in range(start, end + 1)
            ]
        return "\n".join(picked[:60])

    @staticmethod
    def _error_lines(output: str) -> List[int]:
        """编译器输出里的行号（`文件(行,列): error CSxxxx`）。"""

        return [int(match.group(1)) for match in re.finditer(r"\.cs\((\d+)\s*,\s*\d+\)", str(output or ""))]

    @classmethod
    def _broken_batches(cls, output: str, batch_lines: Sequence[Tuple[int, int, int]]) -> set:
        """这次编译报错落在哪几批上（空集＝报错不在处理函数里）。"""

        broken: set = set()
        for line in cls._error_lines(output):
            for index, start, end in batch_lines:
                if start <= line <= end:
                    broken.add(index)
        return broken

    @staticmethod
    def _truncated(raw: str) -> bool:
        """回答是不是被截断了（有开栅栏没有收尾栅栏，或代码没写到结尾大括号）。

        有围栏时只数栅栏：成对就是写完了——不能拿"文本结尾是不是 ``}``"去判断，
        因为写完的回答结尾是收尾栅栏而不是大括号（这条写错会把所有正常回答都判成截断）。
        """

        text = str(raw or "").strip()
        if not text:
            return False
        if "```" in text:
            return text.count("```") % 2 == 1
        return not text.endswith("}")

    def _write_deck_file(self, style_name: str, request: DeckScriptRequest) -> None:
        """把卡组的 .ydk 写进 WindBot 的 Decks 目录。

        脚本里 `[Deck(名字, "AI_<名字>")]` 指向它；插件平时会用 DeckFile 显式指定卡表，
        这里写一份是为了让脚本自身也是自洽的。
        """

        cards = {"主卡组": [], "额外卡组": [], "副卡组": []}
        for card in request.cards:
            cards.setdefault(card.zone, []).append(card.card_id)
        lines = ["#created by mai-play-ygo", "#main"]
        lines += [str(cid) for cid in cards.get("主卡组", [])]
        lines += ["#extra"] + [str(cid) for cid in cards.get("额外卡组", [])]
        lines += ["!side"] + [str(cid) for cid in cards.get("副卡组", [])]
        (self._windbot_dir / "Decks" / f"AI_{style_name}.ydk").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    async def _build(self) -> Tuple[bool, str]:
        """调用 dotnet 编译源码树，返回 ``(是否成功, 输出)``。"""

        try:
            process = await asyncio.create_subprocess_exec(
                self._dotnet,
                "build",
                "WindBot.csproj",
                "-c",
                "Release",
                "--nologo",
                cwd=str(self._source_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise ScriptGenerationError(f"找不到 {self._dotnet} 命令（要装 .NET SDK 才能编译脚本）") from exc
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._build_timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ScriptGenerationError(f"编译超时（{self._build_timeout:.0f} 秒）") from exc
        output = stdout.decode("utf-8", errors="replace")
        if process.returncode != 0:
            self._raise_if_exe_locked(output)
        return process.returncode == 0, output

    @staticmethod
    def _raise_if_exe_locked(output: str) -> None:
        """把"exe 被占用"这种编译失败翻译成一句人话。

        实测（2026-10-08）：有人正在跟机器人打的时候跑「写脚本」，`dotnet build` 会在
        "把 obj\\Release\\WindBot.exe 复制到 bin\\Release\\WindBot.exe" 这一步失败
        （MSB3026 / 另一个程序正在使用此文件）——因为那一局正跑着这个 exe。
        原始输出是一堆 MSBuild 警告，看不懂的人只会以为"生成坏了"；这里直接说清楚
        "等这局打完再写"，并且**不当成重试理由**（重试也还是锁着，白烧三轮）。
        """

        markers = ("MSB3026", "MSB3027", "MSB3021", "being used by another process", "另一个程序正在使用")
        if not any(mark in output for mark in markers):
            return
        raise ScriptGenerationError(
            "编译没法完成：WindBot.exe 正被占用（很可能有人正在跟机器人打这一局）。"
            "这不是生成的问题——等这局打完再写脚本。原始输出里可以看到 "
            + next(mark for mark in markers if mark in output)
        )
