"""阻抗决策层的**答复端**：读 WindBot 写下的问题文件，问模型，把答复写回去。

WindBot 侧（`Game/AI/MaiBotBrain.cs`）在**阻抗时点**把问题写进 ``<prefix>.q``，然后轮询
``<prefix>.a`` 等答复；本模块就是那个"外面的 Python 侧"。协议细节见 `MaiBotBrain` 的文件头，
这里只说三条**必须照做、否则会静默坏掉**的约定：

1. **答复必须原子写入**（先写临时文件再 ``os.replace``）。C# 那边是"文件存在就整份读"，
   半写状态它读到的是一份没有 ``answer`` 行的文件——它不会报错，只会继续等到超时，
   而那份坏文件还留在原地，后面的问题也会被它带偏。
2. **答复要带上对方的 ``id``**。C# 只认 id 相同的那一份；id 对不上它会一直等。
3. **答不上来就什么都别写**。C# 等不到答复会按出牌脚本自己的判断继续（并且连续几次之后熔断），
   这比"塞一个瞎猜的答复"安全——决策层是加建议的，不是替脚本做决定的。

问答的发起来源是 `src/config` 之外的东西（宿主的模型网关），所以本模块**不直接调模型**：
调用方注入一个 ``generate(prompt) -> str | None``，这样纯逻辑（解析、组装提示词、超时、缓存、
写入）全都可以在没有宿主的前提下单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import asyncio
import hashlib
import logging
import os

from .cards import CardDatabase, CardDatabaseError


#: 发起一次模型调用的函数：返回答复文本；返回 None / 抛异常都算"没答上来"。
GenerateFunc = Callable[[str], Awaitable[Optional[str]]]

#: 只处理这三类问题（其余种类一律不答，并各记一次日志）。
#: * `chain_choice`：「对面发动效果带来的时点，我方能交的这几张里发哪张 / 都不发」
#:   ——这是"该发谁的效果"那一问（C# 侧 `MaiBotBrain.AskChainChoice`）；
#: * `disable_target`：「这一张无效卡该指向对面哪只怪」——"该去针对谁"（`MaiBotBrain.PickDisableTarget`）；
#: * `negate_gate`：「要不要交这张阻抗」——单张卡的闸门（`MaiBotBrain.Guard`）。
#: 名字务必与 C# 里写下的 `kind=` 一致。
SUPPORTED_KINDS = ("chain_choice", "disable_target", "negate_gate")

#: 卡文塞进提示词的每张上限；太长的卡文（十几行的复合效果）截断即可，判断威胁够用了。
_CARD_TEXT_LIMIT = 320

#: 一次提问最多给多少张候选卡配卡文（候选本身很少超过 5 张，这是防御性上限）。
_MAX_CARD_TEXTS = 12

#: 读问题文件时，遇到"半写状态"最多重试几次、每次间隔多久。
_PARTIAL_READ_RETRIES = 3
_PARTIAL_READ_DELAY = 0.02


@dataclass
class BridgeStats:
    """累计计数。**这些数字是排查"决策层到底在不在工作"的唯一凭据**，必须可读。"""

    asked: int = 0
    answered: int = 0
    cached: int = 0
    timed_out: int = 0
    failed: int = 0
    unsupported: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """给日志用的一层字典。"""

        data = {
            "asked": self.asked,
            "answered": self.answered,
            "cached": self.cached,
            "timed_out": self.timed_out,
            "failed": self.failed,
            "unsupported": self.unsupported,
        }
        data.update({f"kind_{key}": value for key, value in self.by_kind.items()})
        return data


def parse_question(text: str) -> Optional[Dict[str, str]]:
    """把问题文件解析成字典；关键字段缺失时返回 None（视为"还没写完"）。

    格式与 C# 的 ``BuildQuestion`` / ``BuildDisableQuestion`` 一致：每行一条 ``键=值``，
    **同一个键可以出现多次**（``option=`` / ``chain=`` 就是多行），所以重的键收进列表。
    卡名里可能有逗号，所以值一律用**分号**分隔字段（C# 那边也是这个口径，改的时候要一起改）。
    """

    if not text or not text.strip():
        return None
    parsed: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        if key in parsed:
            existing = parsed[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                parsed[key] = [existing, value]
        else:
            parsed[key] = value

    qid = parsed.get("id")
    kind = parsed.get("kind")
    if not isinstance(qid, str) or not qid.isdigit():
        return None
    if not isinstance(kind, str) or not kind:
        return None
    return {key: (value if isinstance(value, str) else "\n".join(value)) for key, value in parsed.items()}


def _split_fields(line: str, expected: int) -> List[str]:
    """把 ``option=1;123;卡名;3000;2500;100`` 这类值拆成字段（缺的补空串）。"""

    parts = line.split(";")
    if len(parts) < expected:
        parts.extend([""] * (expected - len(parts)))
    return parts


def _field(fields: Sequence[str], index: int) -> str:
    """按下标取字段，缺的给空串——**不要用元组解包**。

    为什么不用 ``a, b, c = fields``：两个问题类型带的区域行字段数不一样
    （目标是精简的 ``卡号;卡名;攻;守`` 四段，闸门走 C# 原有的 `AppendContext`，
    是 ``卡号;卡名;攻;守;表示形式`` 五段）。解包固定长度会在**多一个字段**时直接抛
    ValueError，而这条异常发生在组装提示词阶段——表现是"问题文件写出来了、模型一次都没被问到"
    （2026-10-08 真机自测踩到，`failed` 一直加、`asked` 也不为 0，看计数很难看出是解析炸了）。
    """

    return fields[index] if index < len(fields) else ""


class BrainBridge:
    """阻抗决策层的答复端。

    Attributes:
        prefix: 与 C# 的 ``BrainFile=`` 同一个前缀（不含扩展名）；问题写 ``.q``、答复写 ``.a``。
        timeout: 自己这一侧的等模型上限（秒）。**必须比 C# 的 ``BrainTimeoutMs`` 略短**，
            否则"超时"这件事只会由 C# 发现，这里是白白多占一段没人用的时间。
    """

    def __init__(
        self,
        *,
        prefix: Path,
        generate: GenerateFunc,
        card_db: Optional[CardDatabase] = None,
        logger: Optional[logging.Logger] = None,
        timeout: float = 2.0,
        poll_interval: float = 0.03,
        cache_size: int = 64,
        max_answers: int = 0,
    ) -> None:
        self._prefix = Path(prefix)
        self._generate = generate
        self._card_db = card_db
        self._logger = logger
        self._timeout = max(float(timeout), 0.2)
        self._poll_interval = max(float(poll_interval), 0.005)
        self._cache_size = max(int(cache_size), 0)
        #: 本局最多答多少问（0 = 不限）。用来给"模型突然变慢"兜一个上限。
        self._max_answers = max(int(max_answers), 0)

        self._question_path = Path(f"{self._prefix}.q")
        self._answer_path = Path(f"{self._prefix}.a")

        self._running = False
        """是否还在跑循环。"""
        self._last_key: Optional[tuple] = None
        """上一次**已受理**的问题指纹 ``(mtime_ns, size, id)``。

        为什么不能只记 id：C# 的 id 是进程内的自增计数，**新的一局会从 1 重来**（常驻房打完
        一局会自动再开一副、进程重启），只记 id 会把新一局的第 1 问当成"已经答过"而静默漏掉。
        加上 mtime/size 之后，"文件被换过"这件事本身就能让我们重新受理。
        """
        self._cache: Dict[str, str] = {}
        self._stats = BridgeStats()
        self._card_text_warned = False

    # ------------------------------------------------------------------ 对外

    @property
    def stats(self) -> BridgeStats:
        """累计计数（只读视图）。"""

        return self._stats

    def stop(self) -> None:
        """请求停止循环（下一次轮询时退出）。"""

        self._running = False

    async def run(self) -> None:
        """轮询循环：一直跑到 :meth:`stop` 被调用。

        **任何异常都不许把循环打死**：这个任务挂了就等于决策层静默失效，而对局还在继续，
        外面看起来只是"最近又变笨了"。出错记一次日志、计数 +1，然后照常继续轮询。
        """

        self._running = True
        while self._running:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001  循环不能被打死，出错只记账
                self._stats.failed += 1
                if self._logger is not None:
                    self._logger.exception("决策层处理提问失败")
            await asyncio.sleep(self._poll_interval)

    # ------------------------------------------------------------------ 循环内部

    async def _tick(self) -> None:
        """看一眼有没有新问题；有就答一条。"""

        if self._max_answers and self._stats.answered >= self._max_answers:
            return
        try:
            stat = self._question_path.stat()
        except OSError:
            return

        key = (stat.st_mtime_ns, stat.st_size)
        if self._last_key is not None and self._last_key[:2] == key:
            return

        question = await self._read_question()
        if question is None:
            return
        # 受理：**先记指纹再干活**，避免问答期间反复读到同一个问题而重复发问
        self._last_key = (stat.st_mtime_ns, stat.st_size, question.get("id", ""))

        kind = question["kind"]
        self._stats.asked += 1
        self._stats.by_kind[kind] = self._stats.by_kind.get(kind, 0) + 1

        if kind not in SUPPORTED_KINDS:
            self._stats.unsupported += 1
            if self._logger is not None:
                self._logger.warning("决策层收到不支持的问题类型 %s，不答复（按脚本继续）", kind)
            return

        prompt = self._build_prompt(question)
        if prompt is None:
            return

        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        answer = self._cache.get(digest) if self._cache_size else None
        if answer is not None:
            self._stats.cached += 1
        else:
            answer = await self._ask_model(prompt)
            if answer is None:
                return
            if self._cache_size:
                if len(self._cache) >= self._cache_size:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[digest] = answer

        self._write_answer(question, answer, prompt=prompt, kind=kind)

    async def _read_question(self) -> Optional[Dict[str, str]]:
        """读问题文件；读到半写状态就等一下重试（C# 是整份重写，存在被读一半的窗口）。"""

        for attempt in range(_PARTIAL_READ_RETRIES):
            try:
                text = self._question_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            if attempt < _PARTIAL_READ_RETRIES - 1:
                question = parse_question(text)
                if question is not None:
                    return question
                await asyncio.sleep(_PARTIAL_READ_DELAY)
            else:
                return parse_question(text)
        return None

    async def _ask_model(self, prompt: str) -> Optional[str]:
        """问一次模型；超时/异常/空答复一律返回 None（调用方据此"什么都不写"）。"""

        try:
            text = await asyncio.wait_for(self._generate(prompt), timeout=self._timeout)
        except asyncio.TimeoutError:
            self._stats.timed_out += 1
            if self._logger is not None:
                self._logger.warning("决策层等模型答复超时（%.1fs），按脚本继续", self._timeout)
            return None
        except Exception as exc:  # noqa: BLE001  模型不可用不该把对局弄坏
            self._stats.failed += 1
            if self._logger is not None:
                self._logger.warning("决策层调用模型失败：%s: %s", type(exc).__name__, exc)
            return None

        if text is None:
            self._stats.failed += 1
            return None
        text = str(text).strip()
        if not text:
            self._stats.failed += 1
            if self._logger is not None:
                self._logger.warning("决策层拿到空答复（模型可能被思考吃光了额度）")
            return None
        return text

    def _write_answer(self, question: Mapping[str, str], raw: str, *, prompt: str, kind: str) -> None:
        """把答复原子写到 ``<prefix>.a``；写不进去就当作没答（C# 会超时回退）。"""

        answer = self._normalize_answer(kind, raw, question)
        if answer is None:
            self._stats.failed += 1
            if self._logger is not None:
                self._logger.warning("决策层的答复不合语法，丢弃不写：%r", raw)
            return

        body = f"id={question['id']}\nanswer={answer}\n"
        temp_path = Path(f"{self._answer_path}.tmp")
        try:
            # 先删掉上一次可能残留的答复：C# 只认 id 相同的，一份坏答复留在原地不会致命，
            # 但会让"这一次到底答了没有"变得难以判断，所以每次先清干净。
            self._answer_path.unlink(missing_ok=True)
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._answer_path)
        except OSError as exc:
            self._stats.failed += 1
            if self._logger is not None:
                self._logger.warning("决策层写答复失败：%s", exc)
            return

        self._stats.answered += 1
        if self._logger is not None:
            self._logger.info("决策层答复（%s）：%s", kind, answer)
            self._log_evidence(kind, question, answer, prompt)

    def _log_evidence(self, kind: str, question: Mapping[str, str], answer: str, prompt: str) -> None:
        """打一行"这次决策长什么样"的取证日志——下一局复盘全靠它。

        一局大概只问十次左右（实测 8 局：对手回合里我方发动连锁 3~21 次/局），
        所以**每次都记**，不需要抽样，也不会刷屏。
        """

        del prompt
        parts = [f"kind={kind}"]
        source = question.get("source")
        if source:
            parts.append(f"source={source}")
        options = question.get("option")
        if options:
            parts.append(f"候选={len(str(options).splitlines())}张")
        chain = question.get("chain")
        if chain:
            parts.append(f"连锁={len(str(chain).splitlines())}环")
        parts.append(f"答复={answer}")
        if self._logger is not None:
            self._logger.info("【决策层】%s", "｜".join(parts))

    @staticmethod
    def _normalize_answer(kind: str, raw: str, question: Mapping[str, str]) -> Optional[str]:
        """把模型的自由文本收紧成**受限语法**；不合语法返回 None。

        收紧是必须的：这个回复最终会变成 WindBot 的动作（"要不要发动""指向哪张"），
        放任意的自由文本回去等于把控制权交给一段没校验的输出。
        """

        text = " ".join(raw.split()).strip()
        if not text:
            return None
        if kind == "chain_choice":
            # 序号 + 可选理由；序号必须**正是候选行首那个数字**（它是内核下标+1，不保证从 1 连续）
            split = text.split(";", 1)
            head = split[0].strip().lower()
            reason = " ".join(split[1].split())[:60] if len(split) > 1 else ""
            if head.startswith("no") or head == "0":
                return f"no;{reason}" if reason else "no"
            if not head.isdigit():
                return None
            allowed = {
                _field(line.split(";"), 0)
                for line in str(question.get("option", "")).splitlines()
                if line.strip()
            }
            if head not in allowed:
                return None
            return f"{head};{reason}" if reason else head
        if kind == "negate_gate":
            lowered = text.lower()
            if lowered.startswith("no"):
                reason = text.split(";", 1)[1].strip() if ";" in text else ""
                reason = " ".join(reason.split())[:60]
                return f"no;{reason}" if reason else "no"
            if lowered.startswith(("yes", "y", "是", "可以", "交")):
                return "yes"
            return None

        # disable_target：只接受一个序号，其余一律丢弃
        digits = ""
        for char in text:
            if char.isdigit():
                digits += char
            elif digits:
                break
        if not digits:
            return None
        options = str(question.get("option", "")).splitlines()
        index = int(digits)
        if index < 1 or index > len(options):
            return None
        return str(index)

    # ------------------------------------------------------------------ 提示词

    def _build_prompt(self, question: Mapping[str, str]) -> Optional[str]:
        """按问题类型组装提示词。"""

        kind = question["kind"]
        if kind == "chain_choice":
            return self._build_chain_prompt(question)
        if kind == "disable_target":
            return self._build_disable_prompt(question)
        if kind == "negate_gate":
            return self._build_gate_prompt(question)
        return None

    def _build_chain_prompt(self, question: Mapping[str, str]) -> Optional[str]:
        """「对面发动了效果，我方能交的这几张里发哪张 / 都不发」。

        这是决策层的**主问题**（"该发谁的效果"）：它同时回答了另外两件事的入口——
        回答 `序号` 就是"要发 + 发这张"，回答 `no;理由` 就是"都不要"；
        而被选中的那张若是无效系，后面还会再问一次"该去针对谁"（`disable_target`）。
        """

        options = [line for line in str(question.get("option", "")).splitlines() if line.strip()]
        if len(options) < 2:
            return None

        lines: List[str] = [
            "你是游戏王对局的决策助手。对手刚刚发动了一个效果，现在轮到你决定：",
            "手上/场上的这几张里，要发哪一张来应对，还是都不发。",
            "",
            "判断口径：",
            "1. 先看**对手这一步值不值得拦**：它在检索 / 从卡组特召 / 堆墓 / 破坏我的场面吗？",
            "   只是一步无关紧要的动作（通常召唤、盖牌、单纯的场面铺垫）→ 都不发；",
            "2. 要拦时**挑那张拦得住它的**：卡文条件对不上的不要选",
            "   （例如「灰流丽」只能无效『从卡组把卡加入手卡 / 从卡组特召 / 从卡组送墓』的效果；",
            "   「效果遮蒙者」只能无效『怪兽效果』且必须指对方场上的效果怪兽）；",
            "3. 手里还有别的牌时**不要因为『有牌就想用』而交**——这张牌留到后面可能更值；",
            "4. 第一行只写一个序号（发哪张），或者写 no 表示都不发；",
            "   写 no 时同一行用分号接一句简短理由（不超过 20 字），写序号时也可以接理由。",
            "",
            "【当前局面】",
        ]
        lines.extend(self._digest_lines(question))
        chain = [line for line in str(question.get("chain", "")).splitlines() if line.strip()]
        if chain:
            lines.append("")
            lines.append("【对面/链上正在发生的】")
            for line in chain:
                parts = line.split(";")
                who = "对手" if _field(parts, 2) == "1" else "我方"
                lines.append(f"- {_field(parts, 1)}（{who}，卡号 {_field(parts, 0)}）")
            lines.extend(self._card_lines(self._ids_from(chain, 0)))
        lines.append("")
        lines.append("【我可以发的（只能从这些里选，序号就是行首那个数字）】")
        for line in options:
            # option 行的格式（C# 的 BuildChainQuestion）：内核下标+1;卡号;卡名;攻;守;desc=描述号
            parts = line.split(";")
            desc = ""
            for part in parts[5:]:
                if part.startswith("desc="):
                    desc = part[5:]
            suffix = f"，发动描述号 {desc}" if desc and desc != "-1" else ""
            lines.append(
                f"{_field(parts, 0)}. {_field(parts, 2)}"
                f"（卡号 {_field(parts, 1)}，攻{_field(parts, 3)}/守{_field(parts, 4)}{suffix}）"
            )
        lines.append("")
        lines.extend(self._card_lines(self._ids_from(options, 1)))
        numbers = "、".join(_field(line.split(";"), 0) for line in options)
        lines.append("")
        lines.append(f"只回复一个序号（{numbers}）或 no;理由。")
        return "\n".join(lines)

    def _build_disable_prompt(self, question: Mapping[str, str]) -> Optional[str]:
        """「这一张无效卡该指向对面哪只怪」。"""

        options = [line for line in str(question.get("option", "")).splitlines() if line.strip()]
        if len(options) < 2:
            return None

        lines: List[str] = [
            "你是游戏王对局的决策助手，只负责一件事：我现在要发动一张「无效」卡，",
            "它该指向对手场上的哪一只怪兽。",
            "",
            "判断口径：",
            "1. 选「接下来最可能靠自身效果带来优势/压制」的那一只——断掉它收益最大；",
            "2. 卡文里写着「效果不会被无效 / 不受效果影响 / 不能成为效果对象」的那一只不要选,",
            "   选了也是白扔一张牌；",
            "3. 都差不多时，选攻击力更高、或是永续压制类（贴纸）的那一只；",
            "4. 只输出序号（从 1 开始的整数），不要输出任何其它字。",
            "",
            "【当前局面】",
        ]
        lines.extend(self._digest_lines(question))
        lines.append("")
        lines.append("【我准备发动的卡】")
        lines.extend(self._card_lines(self._source_ids(question)))
        lines.append("")
        chain = [line for line in str(question.get("chain", "")).splitlines() if line.strip()]
        if chain:
            lines.append("【连锁上正在发生的】")
            for line in chain:
                parts = line.split(";")
                who = "对手" if _field(parts, 2) == "1" else "我方"
                lines.append(f"- {_field(parts, 1)}（{who}，卡号 {_field(parts, 0)}）")
            lines.extend(self._card_lines(self._ids_from(chain, 1)))
            lines.append("")
        lines.append("【候选（只能从这些里选）】")
        for line in options:
            # option 行的格式（C# 的 BuildDisableQuestion）：序号;卡号;卡名;攻;守;威胁值
            parts = line.split(";")
            lines.append(
                f"{_field(parts, 0)}. {_field(parts, 2)}"
                f"（卡号 {_field(parts, 1)}，攻{_field(parts, 3)}/守{_field(parts, 4)}）"
            )
        lines.append("")
        lines.extend(self._card_lines(self._ids_from(options, 1)))
        lines.append("")
        lines.append(f"只回复一个序号（1-{len(options)}）。")
        return "\n".join(lines)

    def _build_gate_prompt(self, question: Mapping[str, str]) -> str:
        """「要不要现在交这张阻抗」。"""

        lines: List[str] = [
            "你是游戏王对局的决策助手，只负责一件事：现在要不要发动我手上的这张阻抗卡。",
            "",
            "判断口径：",
            "1. 对手这一步如果放任结算，会不会明显扩大它的优势（检索、展开、破坏我的场面）？",
            "   会 → 交；只是一步无关紧要的动作 → 留着；",
            "2. 卡文条件对不上的（例如对手这张不检索/不从卡组特召）不要交；",
            "3. 不要因为「手上有牌就想用」而交——这张牌留到后面可能更值；",
            "4. 第一行只写 yes 或 no；写 no 时同一行用分号接一句简短理由（不超过 20 字）。",
            "",
            "【当前局面】",
        ]
        lines.extend(self._digest_lines(question))
        lines.append("")
        lines.append("【我准备发动的卡】")
        lines.extend(self._card_lines(self._source_ids(question)))
        chain = [line for line in str(question.get("chain", "")).splitlines() if line.strip()]
        if chain:
            lines.append("")
            lines.append("【连锁上正在发生的】")
            for line in chain:
                parts = line.split(";")
                who = "对手" if _field(parts, 2) == "1" else "我方"
                lines.append(f"- {_field(parts, 1)}（{who}，卡号 {_field(parts, 0)}）")
            lines.extend(self._card_lines(self._ids_from(chain, 1)))
        lines.append("")
        lines.append("只回复 yes，或 no;理由。")
        return "\n".join(lines)

    @staticmethod
    def _digest_lines(question: Mapping[str, str]) -> List[str]:
        """局面摘要：把问题里带的标量字段原样列出来（它们已经是"够用就行"的精简形式）。"""

        labels = {
            "turn": "回合数",
            "my_phase": "是否我方回合",
            "phase": "阶段码",
            "my_lp": "我方生命值",
            "opp_lp": "对手生命值",
            "my_damage": "我方能打出的总伤害",
            "last_summon_player": "这次窗口由谁召唤触发（0=我方 1=对手）",
        }
        lines: List[str] = []
        for key, label in labels.items():
            value = question.get(key)
            if value:
                lines.append(f"- {label}：{value}")
        zone_labels = {
            # 判断"要不要交这张坑"必须看得见**对手场上站着谁**（闸门问题才会带 `theirs`，
            # 目标问题的候选是单独列的，没有这一项也不影响）
            "theirs": "对手场上怪兽",
            "their_spell": "对手魔陷区",
            "hand": "我方手牌",
            "mine": "我方场上怪兽",
            "my_spell": "我方魔陷区",
        }
        for key, label in zone_labels.items():
            raw = question.get(key)
            if not raw:
                continue
            entries = [line for line in str(raw).splitlines() if line.strip()]
            if not entries:
                continue
            rendered = []
            for line in entries:
                fields = line.split(";")
                name = _field(fields, 1)
                rendered.append(
                    f"{name}(攻{_field(fields, 2)}/守{_field(fields, 3)})" if name else "（空）"
                )
            lines.append(f"- {label}：{'、'.join(rendered)}")
        # 对手已经露过的卡（C# 原有的 `AppendContext` 会写；判断"这张坑现在交值不值"要看它）
        seen = question.get("their_seen")
        if seen:
            names = [
                _field(line.split(";"), 1)
                for line in str(seen).splitlines()
                if _field(line.split(";"), 1)
            ]
            if names:
                lines.append(f"- 对手已露过的卡：{'、'.join(names[:12])}")
        # 没有连锁时的"这次窗口是什么触发的"（C# 侧只在本阶段确实有召唤时才写）
        summoned = question.get("last_summon")
        if summoned:
            entries = [
                _field(line.split(";"), 1)
                for line in str(summoned).splitlines()
                if _field(line.split(";"), 1)
            ]
            if entries:
                lines.append(f"- 刚刚召唤/特殊召唤的卡：{'、'.join(entries)}")
        return lines

    @staticmethod
    def _source_ids(question: Mapping[str, str]) -> List[int]:
        source = question.get("source", "")
        return BrainBridge._ids_from([source], 0)

    @staticmethod
    def _ids_from(lines: Sequence[str], position: int) -> List[int]:
        """从若干 ``a;b;c`` 行里抠出第 position 段当卡号。"""

        ids: List[int] = []
        for line in lines:
            fields = line.split(";")
            if len(fields) <= position:
                continue
            try:
                card_id = int(fields[position])
            except (TypeError, ValueError):
                continue
            if card_id > 0 and card_id not in ids:
                ids.append(card_id)
        return ids

    def _card_lines(self, card_ids: Sequence[int]) -> List[str]:
        """取卡文（本地 `cards.cdb`，不联网）。

        卡文是这个决策层相对出牌脚本**唯一的信息优势**（脚本只认卡号与几张稀疏的静态表），
        所以必须给；但只给"这次判断用得上"的那几张，全部截断——输入越长答复越慢，
        而对手回合的等待是要挤进内核时限的。
        """

        if self._card_db is None or not self._card_db.available or not card_ids:
            return []
        wanted = list(card_ids)[:_MAX_CARD_TEXTS]
        try:
            details = self._card_db.card_details(wanted)
        except CardDatabaseError as exc:
            if not self._card_text_warned and self._logger is not None:
                self._logger.warning("决策层读不出卡文（%s），这次只凭卡名判断", exc)
                self._card_text_warned = True
            return []
        lines: List[str] = []
        for card_id in wanted:
            detail = details.get(card_id)
            if detail is None:
                continue
            effect = detail.effect[:_CARD_TEXT_LIMIT]
            lines.append(f"- {detail.name}（{detail.type_text}{'，' + detail.stats if detail.stats else ''}）：{effect}")
        return lines
