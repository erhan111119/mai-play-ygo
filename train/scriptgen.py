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
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

import asyncio
import logging
import re
import zlib

#: 提示词里最多放多少张卡的效果文本（放太多会把上下文撑爆，模型反而写不完代码）。
MAX_CARDS_IN_PROMPT = 45

#: 单次编译的超时（秒）。本机实测 `dotnet build` 这个工程只要两秒左右，给到 5 分钟是余量。
BUILD_TIMEOUT_SECONDS = 300.0

#: 默认最多「生成 → 编译」几轮。
DEFAULT_MAX_ATTEMPTS = 3

#: 生成的脚本落在这个子目录（相对源码树）。
GENERATED_SUBDIR = Path("Game") / "AI" / "Decks"

#: 生成脚本的出牌思路名统一用这个前缀，避免与 WindBot 自带的名字相撞。
STYLE_PREFIX = "Gen"

#: 回答被截断时回喂的要求。实测"什么卡都登记"的执行器会被输出上限截在半路
#: （第一份生成物只有 40 行、停在卡号常量中间），所以这里明确要求写短、写完。
_TRUNCATED_HINT = (
    "上一次回答被截断了（没有写完就结束）。请重新输出一份**完整**的脚本："
    "只登记最关键的几张卡的处理函数，总行数控制在一百五十行以内，"
    "并确保写到最后一行（包含结尾的大括号）。宁可少写几张卡，也不要写一半。"
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

可用的 API（只用这些，不要用其它成员）：

* `AddExecutor(ExecutorType.类型)` / `AddExecutor(ExecutorType.类型, CardId.某卡)` /
  `AddExecutor(ExecutorType.类型, 处理函数)` / `AddExecutor(ExecutorType.类型, CardId.某卡, 处理函数)`
* `ExecutorType` 可取：`Summon`、`SpSummon`、`Repos`、`MonsterSet`、`SpellSet`、`Activate`、
  `SummonOrSet`、`GoToBattlePhase`、`GoToMainPhase2`、`GoToEndPhase`、`Surrender`
* 可直接复用的现成处理器：`DefaultMonsterRepos`、`DefaultMonsterSummon`、`DefaultSpellSet`、
  `DefaultTrap`、`DefaultMaxxC`、`DefaultAshBlossomAndJoyousSpring`、
  `DefaultInfiniteImpermanence`、`DefaultCalledByTheGrave`、`DefaultSolemnJudgment`
* 处理函数签名是 `private bool Xxx()`：返回 `true` 表示就用这个动作，`false` 表示放弃
* 指定选择目标：`AI.SelectCard(卡号)`、`AI.SelectCard(卡号1, 卡号2, ...)`、
  `AI.SelectCard(ClientCard 对象)`、`AI.SelectPlace(Zone 常量)`、`AI.SelectPosition(...)`
* 常见上下文：`Duel.Turn`（回合数）、`Duel.Player`（当前回合玩家）、`Duel.Fields[0]`（自己的场面）、
  `Duel.Fields[0].Hand` / `.MonsterZone` / `.SpellZone` / `.Graveyard`，这几个集合的元素是
  `ClientCard`，可以用 `card.Id` 比较、`card.IsCode(卡号)` 判断
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

    def build_prompt(self, request: DeckScriptRequest, *, previous_error: str = "") -> str:
        """拼出生成脚本的提示词。"""

        style_name = self._style_name or self.style_name_for(request.deck_id)
        class_name = self.class_name_for(style_name)
        lines = [
            "你是 WindBot（一个游戏王对局机器人）的出牌脚本作者。",
            "请为下面这副卡组写一个 C# 出牌脚本，让机器人按这副牌自己的打法出牌。",
            "",
            f"卡组名：{request.deck_name}",
            "",
            "卡表（编号 卡名（所在卡组·类型）攻防 与效果）：",
        ]
        for card in request.cards[:MAX_CARDS_IN_PROMPT]:
            lines.append(card.describe())
        if len(request.cards) > MAX_CARDS_IN_PROMPT:
            lines.append(f"- （另有 {len(request.cards) - MAX_CARDS_IN_PROMPT} 张未列出）")
        lines.append("")

        # 用 replace 而不是 format：速查表里全是 C# 的花括号，format 会把它们当占位符
        lines.append(API_CHEATSHEET.replace("{style_name}", style_name).replace("{class_name}", class_name))
        lines.append("")

        if request.combo_guide.strip():
            lines.append("这副牌的 combo 推演（**优先按这个顺序实现**，它是按卡文推出来的）：")
            lines.append(request.combo_guide.strip()[:6000])
            lines.append("")
        if request.extra_prompt.strip():
            lines.append("作者的额外要求（必须遵守）：")
            lines.append(request.extra_prompt.strip()[:2000])
            lines.append("")
        if request.feedback.strip():
            lines.append("上一轮实测发现的问题（这一轮要改掉）：")
            lines.append(request.feedback.strip()[:4000])
            lines.append("")

        lines.append("硬性要求：")
        lines.append("1. 只输出一个 C# 代码块，不要任何解释文字。")
        lines.append("2. 必须能通过 `dotnet build` 编译：只用上面列出的 API，不要引入新的 using 或依赖。")
        lines.append("3. CardId 里只用上面卡表里出现过的卡号；不要凭记忆写卡号。")
        lines.append("4. 至少要登记这几个通用行为，保证任何场面都能动起来：")
        lines.append("   AddExecutor(ExecutorType.SummonOrSet); AddExecutor(ExecutorType.SpellSet);")
        lines.append("   AddExecutor(ExecutorType.Repos, DefaultMonsterRepos);")
        lines.append("5. 针对关键卡按顺序登记更优先的行为（检索、特召、展开），把最重要的放前面。")
        lines.append("6. 每个处理函数都要考虑场面为空、找不到目标的情况，不能抛异常。")
        lines.append("7. 风格档位固定写 Normal。")
        lines.append(
            "8. **必须写完整**：只登记最关键的 5~10 张卡，总行数控制在一百五十行以内，"
            "最后一行必须是收尾的大括号。写长被截断等于白写——宁可少写几张卡。"
        )
        lines.append(
            f'9. **名字必须照抄**：特性写成 `[Deck("{style_name}", "AI_{style_name}", "Normal")]`，'
            f"类名写成 `class {class_name}`，不要自己另起名字。"
        )
        if previous_error:
            lines.append("")
            lines.append("上一轮生成的代码编译失败了，编译器输出如下，请修正后重新输出完整代码：")
            lines.append("```")
            lines.append(previous_error.strip()[:4000])
            lines.append("```")
        return "\n".join(lines)

    async def generate(self, request: DeckScriptRequest) -> GeneratedScript:
        """生成脚本并编译，失败时把编译器输出回喂重试。

        Raises:
            ScriptGenerationError: 重试用尽仍未编译通过，或源码树不可用时抛出。
        """

        self.ensure_ready()
        style_name = self._style_name or self.style_name_for(request.deck_id)
        class_name = self.class_name_for(style_name)
        target = self._source_dir / GENERATED_SUBDIR / f"{class_name}.cs"

        previous_error = ""
        last_code = ""
        for attempt in range(1, self._max_attempts + 1):
            prompt = self.build_prompt(request, previous_error=previous_error)
            raw = ""
            try:
                raw = await self._generate(prompt)
            except Exception as exc:  # noqa: BLE001  模型那层的失败要带上"是模型的问题"
                previous_error = f"调用模型失败：{exc}"
                if self._logger is not None:
                    self._logger.warning("第 %s 轮调用模型失败：%s", attempt, exc)
                continue
            truncated = self._truncated(raw)
            code = self._extract_code(raw)
            if not code:
                # 三种情况要分开说：模型没回话 / 回了但被截断 / 回了但没给代码块。
                # 它们的处置完全不同（换模型 / 让它写短 / 改提示词），糊成一句"生成失败"
                # 会让排查方向全错——实测就被"没有输出可识别的 C# 代码块"带偏过一次
                if not str(raw or "").strip():
                    previous_error = "模型返回了空内容（没回话）。"
                    if self._logger is not None:
                        self._logger.warning("第 %s 轮模型返回空内容，重试", attempt)
                else:
                    previous_error = _TRUNCATED_HINT if truncated else "模型没有输出可识别的 C# 代码块。"
                    if self._logger is not None:
                        self._logger.warning(
                            "第 %s 轮没有拿到可用代码（截断=%s，%s 字符）", attempt, truncated, len(raw)
                        )
                continue
            if truncated:
                # 截断的代码注定编译不过，先让模型写短一点，省一轮无效编译
                if self._logger is not None:
                    self._logger.warning(
                        "第 %s 轮回答被截断（%s 字符），回喂「写短一点」后重试", attempt, len(raw)
                    )
                previous_error = _TRUNCATED_HINT
                continue
            if not self._declares_identity(code, style_name, class_name):
                # 名字对不上的话，脚本能编译、能注册，但对局时按约定名找不到它——
                # WindBot 会随机挑一个执行器顶上，"用了错的脚本还看不出来"就是这么来的
                if self._logger is not None:
                    self._logger.warning("第 %s 轮代码没声明约定的名字，要求重写", attempt)
                previous_error = (
                    "脚本名与类名必须和约定的一致：特性写成"
                    f'`[Deck("{style_name}", "AI_{style_name}", "Normal")]`，'
                    f"类名写成 `class {class_name}`，不要另起名字。请重新输出完整代码。"
                )
                continue
            last_code = code
            target.write_text(code, encoding="utf-8")
            self._write_deck_file(style_name, request)

            ok, output = await self._build()
            if ok:
                return GeneratedScript(
                    style_name=style_name,
                    file_path=target,
                    code=code,
                    attempts=attempt,
                    build_output=output,
                )
            previous_error = output
            if self._logger is not None:
                head = " ".join(line.strip() for line in output.splitlines() if "error" in line.lower())[:400]
                self._logger.warning(
                    "第 %s 轮生成的脚本编译失败，回喂错误重试：%s", attempt, head or output[:200]
                )

        # 编译没通过时把生成物从源码树删掉，避免它一直拖垮后续编译；
        # 但**留下最后一次尝试**另存一份：想弄清"模型到底写了什么"只能靠它。
        #
        # 存放位置很讲究：**绝不能留在源码树里**——csproj 会把 Game/AI/Decks 下的所有 .cs
        # 都编进去，那样这份失败的尝试就成了同一个类的第二份定义，之后每轮编译都报
        # CS0101/CS0579（实测把三轮重试全毒死了）。放到运行目录下，谁也不编它。
        target.unlink(missing_ok=True)
        failed_path = self._failed_dir() / f"{target.stem}.failed.cs"
        if last_code:
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(last_code, encoding="utf-8")
        raise ScriptGenerationError(
            f"生成 {self._max_attempts} 轮都没能编译通过，最后一段代码没被采用"
            + (f"（最后一次尝试留在 {failed_path}，可以直接打开看）" if last_code else "")
            + f"。最后一次编译器输出：\n{previous_error.strip()[:800]}"
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

    @staticmethod
    def _extract_code(raw: str) -> str:
        """从模型输出里取出 C# 代码块。

        **容忍"没写完"的回答**：模型被输出上限截断时只有开头的栅栏而没有收尾的，
        早先的正则要求两端栅栏齐全，这种回答会被判成"没有代码块"，于是每轮重试都在做无用功
        （实测连续失败三轮就是这么来的）。现在收尾栅栏可选，截断的回答照样取出部分代码，
        交给编译去报真实错误。
        """

        text = str(raw or "")
        match = re.search(r"```(?:csharp|cs|C#)?\s*(.*?)(?:```|\Z)", text, re.S)
        code = match.group(1) if match else text
        code = code.strip()
        if "class" not in code or "Deck(" not in code:
            return ""
        return code

    @staticmethod
    def _declares_identity(code: str, style_name: str, class_name: str) -> bool:
        """代码里是否真的声明了约定的脚本名与类名。"""

        return f'Deck("{style_name}"' in code and f"class {class_name}" in code

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
        return process.returncode == 0, output
