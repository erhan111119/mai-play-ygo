"""教练：把局面翻译成一份作战计划。

阶段 1 先做**规则教练**——用它验证"计划 → 执行器 → 行为"这条管线通不通、
以及胜率有没有变化；管线验通了，把 ``plan_for`` 换成一次模型调用就是"AI 定战术"，
执行器与擂台都不用动。

规则教练的思路（都是能讲清道理的，不是拍脑袋）：

* **落后就更凶**：自己 LP 落后时倾向抢血翻盘，领先时倾向保场不冒险；
* **场面不劣就打脸**：对面怪兽不多于自己时，直接攻击比撞怪兽划算；
* **保场思路下留手坑**：打得保守时，阻止类卡片留着等对面关键展开。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

import asyncio
import inspect
import logging
import re

from .plan import DuelPlan

# 落后/领先多少 LP 视为"该凶/该稳"（经验值，先能用再谈调优）
_LP_SWING = 6000.0


@dataclass
class TurnContext:
    """做决策时能看到的局面（只用确定能拿到的字段，不猜）。

    数据来自对局记录器：LP 与场地占用是它跟着报文维护的（``duel/fieldstate.py``）；
    手牌数、墓地数这类暂时没进快照，等要用再加。
    """

    turn: int
    my_lp: int
    opponent_lp: int
    my_monsters: int
    my_spells: int
    opponent_monsters: int
    opponent_spells: int

    @property
    def lp_diff(self) -> int:
        """自己 LP 减对手 LP：负数表示落后。"""

        return self.my_lp - self.opponent_lp


async def rule_based_plan(context: TurnContext) -> DuelPlan:
    """规则教练：按 LP 差与场面出计划。"""

    # 落后（lp_diff<0）→ aggression 上升；领先 → 下降。0.5 是中性
    aggression = 0.5 - context.lp_diff / (2 * _LP_SWING)
    aggression = min(1.0, max(0.0, aggression))

    prefer_direct = context.opponent_monsters <= context.my_monsters
    hold_handtraps = aggression < 0.4

    parts = []
    if context.lp_diff < 0:
        parts.append(f"落后 {-context.lp_diff}")
    elif context.lp_diff > 0:
        parts.append(f"领先 {context.lp_diff}")
    parts.append("对面场面更大" if context.opponent_monsters > context.my_monsters else "场面不吃亏")

    return DuelPlan(
        aggression=aggression,
        hold_handtraps=hold_handtraps,
        prefer_direct=prefer_direct,
        turn=context.turn,
        notes="、".join(parts),
    )


# 教练的接口：给一份局面，回一份计划（异步，好接模型调用）
Coach = Callable[[TurnContext], Awaitable[Optional[DuelPlan]]]


# 让模型输出好解析的说明：只要三行 key=value，不要解释。
# aggression 特意给成几档而不是连续小数：实测小模型被问「0.0~1.0 的小数」时
# 一律回 0.5（惰性取整），等于这个维度根本没参与决策。
# 档位表另起一段、格式行里只留占位符：实测把「0.2 或 0.5 或 0.8」写在格式行里，
# 模型会把这句原样抄回来，解析必然失败。
_LLM_INSTRUCTION = """你在替游戏王玩家「麦麦」决定接下来几个回合的打法。只输出下面三行，不要解释、不要复述本说明：
aggression=<在下方的档位里选一档>
hold_handtraps=<0/1>
prefer_direct=<0/1>

档位含义：
aggression=0.2 保场不冒险，aggression=0.5 正常打，aggression=0.8 全力抢血
hold_handtraps：1=留住灰流丽这类阻止卡，等对面关键展开再交；0=有机会就用
prefer_direct：1=优先直接攻击打脸；0=优先处理对面场面"""

# 档位与中文说法：模型两种写法都收，认不出来就退回兜底教练（不猜）
_AGGRESSION_LEVELS = (0.2, 0.5, 0.8)
_AGGRESSION_WORDS = (
    ("保场", 0.2),
    ("保守", 0.2),
    ("防守", 0.2),
    ("正常", 0.5),
    ("中性", 0.5),
    ("抢血", 0.8),
    ("激进", 0.8),
    ("进攻", 0.8),
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_YES_WORDS = ("1", "true", "yes", "y", "on", "是", "留")
_NO_WORDS = ("0", "false", "no", "n", "off", "否", "不留")
# 认到多远的档位就不认了：说 0.6 可以归到 0.5，说 0.9 与哪一档都差得远，拒收
_LEVEL_TOLERANCE = 0.2


class LlmCoach:
    """模型教练：把局面写成一小段文字问模型，解析它的三行回答。

    Args:
        client: ``train.llm.ModelClient``（只为拿到 ``chat``，接口更宽松的替身也行）。
        fallback: 模型调用失败或输出解析不出来时用的教练（默认规则教练）。
        logger: 日志器。

    设计取舍：**输出格式固定成三行 key=value**，而不是让模型写自由文本或 JSON。
    原因是一局要问几十次，解析必须稳；而这个格式本身就是计划文件的格式，
    对不上就退回规则教练，实验不会因为模型抽风而中断。
    """

    def __init__(
        self,
        client,
        *,
        fallback: Optional[Coach] = rule_based_plan,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._client = client
        self._fallback = fallback
        self._logger = logger or logging.getLogger(__name__)
        self.calls = 0
        self.failures = 0
        self.fallbacks = 0

    def _build_prompt(self, context: TurnContext) -> str:
        """把局面写成模型能读的一小段文字。"""

        parts = [
            _LLM_INSTRUCTION,
            "",
            f"局面：第 {context.turn} 回合；"
            f"我方生命值 {context.my_lp}，对方 {context.opponent_lp}；"
            f"我方怪兽 {context.my_monsters} 只、魔陷 {context.my_spells} 张；"
            f"对方怪兽 {context.opponent_monsters} 只、魔陷 {context.opponent_spells} 张。",
        ]
        if context.lp_diff < 0:
            parts.append(f"（我方落后 {-context.lp_diff} 点生命值）")
        elif context.lp_diff > 0:
            parts.append(f"（我方领先 {context.lp_diff} 点生命值）")
        if context.opponent_monsters > context.my_monsters:
            parts.append("（对面场面更大）")
        return "\n".join(parts)

    async def __call__(self, context: TurnContext) -> Optional[DuelPlan]:
        """问模型要计划；失败就退回兜底教练。"""

        self.calls += 1
        prompt = self._build_prompt(context)
        try:
            answer = await self._ask(prompt)
        except Exception as exc:  # noqa: BLE001  模型失败不该中断对局
            self.failures += 1
            self._logger.warning("模型教练调用失败，改用规则教练：%s", exc)
            return await self._use_fallback(context)

        plan = self._parse(answer, context)
        if plan is None:
            self.failures += 1
            self._logger.warning("模型教练输出无法解析，改用规则教练：%r", answer[:120])
            return await self._use_fallback(context)
        if self._logger is not None:
            self._logger.debug("模型教练：aggression=%.2f hold=%s direct=%s", plan.aggression,
                               plan.hold_handtraps, plan.prefer_direct)
        return plan

    async def _ask(self, prompt: str) -> str:
        """调一次模型。

        两种客户端都要支持：训练侧是同步的 ``train.llm.ModelClient``（丢线程里跑，
        别堵住事件循环），插件侧是宿主的异步 ``ctx.llm.generate``（直接 await）。
        """

        chat = self._client.chat
        if inspect.iscoroutinefunction(chat):
            return str(await chat(prompt))
        # max_tokens 给到 200：小模型爱先垫一句再答题，给 60 会把答案截断（实测只回来
        # 一行 prefer_direct=1），截断的回答按解析失败处理，等于白花一次调用
        return str(await asyncio.to_thread(chat, prompt, max_tokens=200, temperature=0.2))

    async def _use_fallback(self, context: TurnContext) -> Optional[DuelPlan]:
        """兜底路径（默认规则教练）。"""

        if self._fallback is None:
            return None
        self.fallbacks += 1
        return await self._fallback(context)

    @staticmethod
    def _read_lines(answer: str) -> Dict[str, str]:
        """把回答读成 ``{键: 值}``：同一个键出现多次时以最后一次为准。

        容错点都在这儿：容忍 markdown 的 ``**``/反引号、中文冒号、行首列表符号；
        取最后一个是因为模型可能先复述格式再给答案（答案在后面）。
        """

        values: Dict[str, str] = {}
        for line in answer.splitlines():
            separator = "=" if "=" in line else ("：" if "：" in line else (":" if ":" in line else ""))
            if not separator:
                continue
            key, _, raw = line.partition(separator)
            cleaned_key = key.strip().strip("*`#- 「」").lower()
            values[cleaned_key] = raw.strip().strip("*`「」,")
        return values

    @staticmethod
    def _parse_aggression(raw: str) -> Optional[float]:
        """读 aggression：数字或中文档位都收；说不清是哪一档就返回 None。"""

        named = {level for word, level in _AGGRESSION_WORDS if word in raw}
        for token in _NUMBER_RE.findall(raw):
            try:
                value = float(token)
            except ValueError:
                continue
            if not 0.0 <= value <= 1.0:
                continue
            level = min(_AGGRESSION_LEVELS, key=lambda entry: abs(entry - value))
            if abs(level - value) <= _LEVEL_TOLERANCE:
                named.add(level)
        # 复述题目（例如「0.5 或 0.8」）会同时命中两档，这时不能替模型选一个
        if len(named) != 1:
            return None
        return named.pop()

    @staticmethod
    def _parse_flag(raw: str) -> Optional[bool]:
        """读 0/1 开关：数字、true/false、中英文说法都收；两种都出现算读不出。"""

        lowered = raw.strip().lower()
        no = any(word in lowered for word in _NO_WORDS)
        # 先抠掉否定说法再找肯定词：「不留」里的「留」不该算肯定
        stripped = lowered
        for word in _NO_WORDS:
            stripped = stripped.replace(word, "")
        yes = any(word in stripped for word in _YES_WORDS)
        if yes == no:  # 都没有，或「0 或 1」这种全都要
            return None
        return yes

    @classmethod
    def _parse(cls, answer: str, context: TurnContext) -> Optional[DuelPlan]:
        """解析模型的三行回答；缺项、说两档、读不出开关都返回 None（交给兜底）。"""

        values = cls._read_lines(answer)
        if "aggression" not in values:
            return None
        aggression = cls._parse_aggression(values["aggression"])
        if aggression is None:
            return None
        hold_handtraps = cls._parse_flag(values.get("hold_handtraps", ""))
        prefer_direct = cls._parse_flag(values.get("prefer_direct", ""))
        if hold_handtraps is None or prefer_direct is None:
            return None

        return DuelPlan(
            aggression=aggression,
            hold_handtraps=hold_handtraps,
            prefer_direct=prefer_direct,
            turn=context.turn,
            notes="模型决定",
        )


def make_coach(name: str, *, llm: Optional[object] = None, logger: Optional[logging.Logger] = None) -> Optional[Coach]:
    """按名字取教练；``none`` 表示不下计划（纯通用打法）。

    Args:
        name: ``rule``（规则）/ ``none``（不下计划）/ ``llm``（模型）。
        llm: 模型教练要用的客户端（``train.llm.ModelClient``）；``llm`` 模式必填。
        logger: 日志器。
    """

    cleaned = name.strip().lower()
    if cleaned in ("", "none", "off", "无"):
        return None
    if cleaned in ("rule", "规则"):
        return rule_based_plan
    if cleaned in ("llm", "ai", "模型"):
        if llm is None:
            raise ValueError("llm 教练需要一个模型客户端（见 train/llm.py 的 ModelClient）")
        return LlmCoach(llm, logger=logger)
    raise ValueError(f"未知的教练：{name}（可用：rule / llm / none）")
