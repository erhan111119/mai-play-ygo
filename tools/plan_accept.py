"""按"计划层验收"的方式统计一张 verbose 对局日志：**逐局看我方第 1 回合结束时摆出了什么**。

**为什么不能只看总次数**：`docs/plan-layer.md` 的验收标准是"三项同时出现的**局数占比**"——
一场里同一张卡刷了三次、另一场一次没出，总次数会把它平均成"看起来还行"（这个项目被均值骗过多次）。
所以要按局切开、逐局判定"第 1 回合结束时场上有没有这几件"。

**怎么按局切（这一版的关键）**：一局 ＝ **一条"带阵营名的 bot 流"**，不是"进度行切出来的块"。

* 擂台一跑就是镜像 2 局（`--rounds 1`），而且这两局**是并发跑的**：同一个日志文件里两局的每一行都交错在一起。
  进度行 ``  [1/2] 第 0 轮 → …`` 是**每局打完才打的**，按它切只能得到"第一块＝两局混在一起，后面几块只剩尾巴"，
  于是逐局统计只认得出 1 局——**失准现象①**：每份日志其实有 2 局，汇总里却总是 ``达标 0/1``、``平均 x/1``。
* 能切干净的那条流是**会话那台 bot 的对家**：它的日志前缀就是它那一方的展示名
  （``我方`` / ``对手(Test)`` / ``对手(WitchcraftShop)`` / ``对手(blank-wall)``），一局一条、互不交错。
* **不能拿 ``WindBot`` 前缀当一局**：那是会话自己起的那台 bot 的原始输出，并发跑镜像时
  **两台会话 bot 都叫这个名字**（同一个前缀里混着两局、两台不同卡组的 bot），切不开；
  好在每一局都恰好有一条带阵营名的流（＝会话那台的对手），所以 ``WindBot`` 整条不用。
* 同一个展示名在串行日志里会出现很多次（`--parallel 1` 跑 15 轮镜像＝15 局都叫 ``我方``）：
  用每个 WindBot 进程开局的 ``WindBot starting...`` 把它再切开，每块就是一局。

**怎么取"第 1 回合结束时的场面"**：

* "我方第 1 回合"按**回合归属**算：每个回合里 ``(N draw 1 card)`` 的 N 就是这回合的回合玩家
  （回合玩家每回合交替，所以第 1 回合这个"不抽牌"的回合可以从第 2 回合反推）。
  窗口 ＝ [我方首回合的 ``(Go to Draw)``, 下一回合的 ``(Go to Draw)``)：结束阶段才成型的终端也收得到
  （闪刀雫空② 拿双纽闪门、魔女术小巷/怠工/学童的结束阶段效果都是在结束阶段才落地）。
* ⚠ **不要**拿"我方第一张卡的移动"当窗口起点：手坑会在**对手回合**就被丢掉/送墓
  （实测 r2 的杀调：对手 T1 就丢了「欢聚友伴·茸茸长尾山雀」），窗口从那里开始就会在轮到我方之前
  撞上 ``(Go to End)`` 关窗，量出来是"我方还没开始打"的空场——**失准现象②**：杀调 r2 报 ``空场局 1/1``，
  可日志里我方第 1 回合结束明明站着「杀手级调整曲·旋钮手」（落位行 ``主怪兽区3 ← #17209452``）。
  上一版想用 ``_is_our_turn_at`` 顺延补救，但那个判据本身不成立——``(Go to Standby)`` 两边**每个回合**都会打，
  往回找只会得出"在我方回合内"，顺延永远不触发。
* 场面用**移动事件**回放出来（``(0 's X from A move to B)``）：比读 dump 准——dump 是每回合开头
  打印一次的，碰到"对手回合里我方自己把 5 星解放掉"（再混音手② 就是这么用的）会看错。

用法::

    python tools/plan_accept.py --log temp/train/plan-95-1.log \\
        --monster "杀手级调整曲·再混音手" "杀手级调整曲·唱片师" \\
        --spell "杀手级调整曲同调"

`tools/max_board.py` 会在进程内调用 :func:`analyze_log` 拿结构化结果（不解析 stdout，免得两边漂移）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import argparse
import re
import sqlite3
import sys

#: 每局结束时打的进度行：`  [12/12] 第 5 轮 → 我方（9 回合，动作 39:3）`
#: 只当**交叉核对**用（"进度行说有几局" vs "切出几条 bot 流"）：**不能**拿它切局——
#: 它是每局打完之后才打的，而镜像两局是并发跑的，按它切会把两局的内容切错（失准现象①）。
_PROGRESS_RE = re.compile(r"^\s*\[\d+/\d+\]\s*第\s*\d+\s*轮")
#: 行首的 `INFO [我方] [26-10-04 10:02:57] ` 前缀（正文可能为空）
_LABEL_RE = re.compile(r"^INFO \[([^\]]+)\] \[[^\]]+\] ?(.*)$")
#: 一个 WindBot 进程开局的标志行：靠它把同一个展示名下的多局切开
_START_RE = re.compile(r"WindBot starting\.\.\.")
#: 场面 dump 的分段标题
_DUMP_RE = re.compile(r"^\*+Bot (Hand|Spell|Monster)\*+$")
#: 计划层的调试行：`第 1 回合计划：Announcer 线`
_PLAN_RE = re.compile(r"第 (\d+) 回合计划：(\w+)")
#: 移动事件：`(0 's 杀手级调整曲·提示员 from Deck move to Hand)`
_MOVE_RE = re.compile(r"\(([01]) 's (.+?) from (\w+) move to (\w+)\)")
#: 新卡落场：`(0 's X appear in MonsterZone)`
_APPEAR_RE = re.compile(r"\(([01]) 's (.+?) appear in (\w+)\)")
#: 叠放素材：`(0 's 银河眼光波刃龙 overlay 盈彩月夜之天 西艾萝-一掷乾坤)`——素材**不会**
#: 再打 "from MonsterZone move to …"，不减掉就会一直挂在场上（见 `_board_in_turn`）。
#: 反向的 `… deattach <素材>` 不用管：它后面紧跟一行 `… appear in Grave`，那行已经把落点记上了。
_OVERLAY_RE = re.compile(r"\(([01]) 's (.+?) overlay (.+)\)$")
#: 抽牌：`(1 draw 1 card)`——这一行的数字就是**该回合的回合玩家**（本流坐标）
_DRAW_RE = re.compile(r"^\(([01]) draw (\d+) card\)$")
#: 回合起点（每个回合的抽牌阶段都会打一行）
_DRAW_PHASE_RE = re.compile(r"^\(Go to Draw\)$")
#: 发动效果：`(0 's 增殖的G activate effect from Hand)`
_ACTIVATE_RE = re.compile(r"\(([01]) 's .+? activate effect from ")

#: 会话侧 bot 的日志前缀：并发跑多局时它名下混着好几台 bot，不能当作"一局"（见模块 docstring）
_SESSION_LABEL = "WindBot"


@dataclass
class GameStats:
    """一局（＝一条 bot 流）的统计结果。

    `tools/max_board.py` 直接读这个结构（不再解析 stdout：之前那边要认 ``怪兽 ✔`` / ``怪兽 ✘(n/m)``
    两种写法，第一版就漏了一种，两边容易漂移）。
    """

    index: int
    """这份日志里的第几局（按流在日志里出现的先后，1 起）。"""

    label: str
    """这条流的日志前缀（＝会话那台 bot 的对家展示名）。"""

    first_line: int
    last_line: int
    """这条流在日志里的行号范围（1 起，含两端）——排查时直接跳过去看。"""

    seat: int
    """我方在这条流里的座位号：0＝这条流那只 bot 就是我方，1＝我方是它的对手。"""

    first_turn: int
    """我方第 1 回合是全场第几个回合（1 起：1＝我方先攻，2＝我方后攻）。"""

    turn_count: int
    """这条流里一共打了几个回合（日志被截断时用来看这一局是否完整）。"""

    board: Dict[str, int]
    """我方第 1 回合结束时的场面（键 ``卡名@区域`` → 张数，只留怪兽区/魔陷区/墓地）。"""

    board_source: str = "ledger"
    """这个场面是怎么来的：``dump``＝我方客户端的完整快照（可信）、``ledger``＝按动作回放的账目
    （依赖"我方卡有名字、动作没被筛掉"，可能漏；见 :func:`_board_snapshot`）。"""

    matched: int = 0
    """口径里的件命中了几件（按调用方给的 --monster/--spell/--grave 算）。"""

    required: int = 0
    """口径里一共几件。"""

    hit_monster: List[str] = field(default_factory=list)
    hit_spell: List[str] = field(default_factory=list)
    hit_grave: List[str] = field(default_factory=list)
    """命中的卡名（按口径分类），用来在日志里点名"缺哪件"。"""

    plan: str = "-"
    """这一局第一条计划行（``第 N 回合计划：X``），没有就 ``-``。"""

    interrupts: int = 0
    """我方在**对手回合**发动效果的次数（`--interrupt` 才统计）。"""

    hand: Optional[List[str]] = None
    """我方起手（本流的 bot 就是我方时才有：第一条 dump 的 Hand 段）。

    ``None`` ＝ 这条流里看不到我方起手（我方是会话那台 bot，它的输出混在 ``WindBot`` 前缀里切不开）。
    """

    @property
    def hits(self) -> int:
        """命中的件数。"""

        return len(self.hit_monster) + len(self.hit_spell) + len(self.hit_grave)

    @property
    def full(self) -> bool:
        """口径里的件是否全部在场（"最大场达标"）。"""

        return self.required > 0 and self.matched == self.required

    @property
    def empty_board(self) -> bool:
        """首回合结束时场上一个怪都没有（"空场局"）。"""

        return not any(key.endswith("@MonsterZone") for key in self.board)


@dataclass
class LogReport:
    """一份日志的统计结果。"""

    games: List[GameStats] = field(default_factory=list)
    skipped: List[Tuple[int, str]] = field(default_factory=list)
    """没统计成的局：(局序号, 原因)——原因要打出来，不能悄悄少算几局。"""

    stream_count: int = 0
    """切出来的一局一条流的总数（含跳过的），用来核对"这份日志到底跑了几局"。"""


@dataclass
class _Stream:
    """一条 bot 流：日志里同一个前缀、同一个进程（一局）的那些正文行。"""

    label: str
    lines: List[str] = field(default_factory=list)
    first_line: int = 0
    last_line: int = 0


@dataclass
class _Turn:
    """一条流里的一个回合。"""

    order: int
    """第几个回合（1 起）。"""

    start: int
    end: int
    """该回合在流里的下标范围（``[start, end)``，end ＝ 下一个回合的起点）。"""

    player: Optional[int] = None
    """回合玩家（本流坐标）：0＝这条流那只 bot，1＝它的对手。"""


def read_deck_cards(deck: Path, cards_cdb: Path) -> List[str]:
    """读 .ydk 的全部卡号再从 cards.cdb 换成卡名（我方卡的过滤名单）。

    **要用整副牌的名字**：只拿一个片段（比如"卡通"）会漏掉名字里没有它的卡
    （实测漏了「漫画猫」「邪魔箱」，把满场的终场算成 0 件）。
    """

    wanted = {int(text.strip()) for text in deck.read_text(encoding="utf-8", errors="ignore").splitlines()
              if text.strip().isdigit()}
    if not wanted:
        return []
    con = sqlite3.connect(str(cards_cdb))
    try:
        placeholders = ",".join("?" for _ in wanted)
        return [name for (name,) in con.execute(
            f"select name from texts where id in ({placeholders})", tuple(wanted))]
    finally:
        con.close()


def _raw_for(lines: Sequence[str], label: str) -> List[str]:
    """取某个前缀打印的正文（去掉行首前缀）。"""

    result: List[str] = []
    for line in lines:
        match = _LABEL_RE.match(line)
        if match and match.group(1) == label:
            result.append(match.group(2))
    return result


def _dump_spans(lines: Sequence[str]) -> List[Tuple[int, Dict[str, List[str]]]]:
    """把 `***Bot Hand/Spell/Monster***` 分段抽成 ``(段首下标, 各段内容)``。

    **保留下标是为了拿"某一回合结束时的场面快照"**：客户端每个回合的准备阶段都会打印一次自己的
    Hand/Spell/Monster（``_dumps`` 原来只给内容、丢了下标），而"我方第 1 回合结束时"的场面
    就＝紧跟其后的那一次 dump——这是**完整客户端快照**，不受"我方那条流是插件筛过的日志"影响
    （见 :func:`_board_snapshot` 的说明）。
    """

    spans: List[Tuple[int, Dict[str, List[str]]]] = []
    current: Optional[Dict[str, List[str]]] = None
    zone: Optional[str] = None
    for index, text in enumerate(lines):
        header = _DUMP_RE.match(text)
        if header:
            zone = header.group(1)
            if zone == "Hand":
                current = {"Hand": [], "Spell": [], "Monster": []}
                spans.append((index, current))
            continue
        if text.startswith("*") and text.endswith("*"):
            zone = None
            current = None
            continue
        if current is not None and zone is not None and text.strip():
            current[zone].append(text.strip())
    return spans


def _dumps(lines: Sequence[str]) -> List[Dict[str, List[str]]]:
    """把 `***Bot Hand/Spell/Monster***` 分段抽成 dump 列表（每次从 Hand 开始到 Finish 结束）。"""

    return [sections for _, sections in _dump_spans(lines)]


def _board_snapshot(spans: Sequence[Tuple[int, Dict[str, List[str]]]], first_turn: "_Turn",
                    our_cards: Sequence[str]) -> Optional[Dict[str, int]]:
    """我方第 1 回合**结束时**的场面快照（取自紧跟其后的那次 dump）；拿不到就返回 ``None``。

    **为什么需要它**（2026-10-06 实测）：:func:`_board_in_turn` 的账目是"按动作回放"的，依赖两点——
    ① 这条流里我方卡**有名字**（对面客户端那边我们的卡有时显示成 ``UnKnowCard``）；
    ② 这条流里我方动作**没被筛掉**（``我方`` 前缀那条是插件自己的筛选日志，实测有局里"大姐已经
    直击打了 1900"却整条流没有它的入场行）。两点任一不成立，场面就记成空的
    （升辉月对空白 30 局被记成"空场 13/28"）。
    而每个客户端都会在**每个回合的准备阶段**打印自己的 Hand/Spell/Monster 三段——那是完整快照：
    **能拿到就优先用快照**（怪兽区/魔陷区），拿不到再退回账目。

    "这条流的 dump 是不是我方的"：dump 的 Hand 段里出现我方卡组的卡。
    "我方第 1 回合结束时"：**紧跟该回合窗口之后的第一次 dump**（＝下一个回合的准备阶段，
    这时我方回合刚结束、对面还没动）。
    """

    if not spans:
        return None
    ours = any(any(any(card in entry for card in our_cards) for entry in sections["Hand"])
               for _, sections in spans)
    if not ours:
        return None      # 这条流是对方客户端，它的 dump 是它自己的场面
    after = [item for item in spans if item[0] >= first_turn.end]
    if not after:
        return None      # 日志在"我方第 1 回合"就断了，没有下一个准备阶段
    sections = after[0][1]
    board: Dict[str, int] = {}
    for zone, suffix in (("Monster", "@MonsterZone"), ("Spell", "@SpellZone")):
        for entry in sections[zone]:
            if not any(card in entry for card in our_cards):
                continue
            key = f"{entry}{suffix}"
            board[key] = board.get(key, 0) + 1
    return board


def _streams(lines: Sequence[str], only_label: Optional[str] = None) -> List[_Stream]:
    """把日志切成"一局一条流"：按前缀分组，组内再按 WindBot 进程开局行切开。

    - 没有 ``INFO [前缀] [时间] `` 前缀的行（记录器的「落位」等）不属于任何流，直接忽略；
    - ``WindBot`` 前缀不用（会话侧那台 bot：并发跑镜像时两台都叫这个名字，见模块 docstring），
      除非调用方用 ``--label`` 明确点名；
    - 排序按"这条流第一行在日志里的位置"＝真实的对局先后。
    """

    grouped: Dict[str, List[Tuple[int, str]]] = {}
    for index, line in enumerate(lines):
        match = _LABEL_RE.match(line)
        if not match:
            continue
        grouped.setdefault(match.group(1), []).append((index, match.group(2)))

    streams: List[_Stream] = []
    for label, rows in grouped.items():
        if only_label is not None and label != only_label:
            continue
        if only_label is None and label == _SESSION_LABEL:
            continue
        current = _Stream(label=label)
        for index, text in rows:
            if _START_RE.search(text) and current.lines:
                # 新的 WindBot 进程开局＝新的一局（串行日志里同一个前缀会有很多局）
                streams.append(current)
                current = _Stream(label=label)
            if not current.lines:
                current.first_line = index + 1
            current.lines.append(text)
            current.last_line = index + 1
        if current.lines:
            streams.append(current)
    streams.sort(key=lambda stream: stream.first_line)
    return streams


def _turns(lines: Sequence[str]) -> List[_Turn]:
    """按 ``(Go to Draw)`` 切回合，并认出每个回合的回合玩家。

    **回合玩家只能从抽牌行拿**：``(N draw 1 card)`` 的 N 就是这回合的回合玩家（本流坐标）。
    第 1 回合不抽牌（先攻第一回合没有抽牌阶段的效果），所以用"回合玩家逐回合交替"
    从第 2 回合反推——比"按第几个 ``(Go to End)`` 数"稳（先攻不一定是 0 号位）。
    """

    starts = [index for index, text in enumerate(lines) if _DRAW_PHASE_RE.match(text.strip())]
    turns: List[_Turn] = []
    for order, start in enumerate(starts, start=1):
        end = starts[order] if order < len(starts) else len(lines)
        turns.append(_Turn(order=order, start=start, end=end))
    for turn in turns:
        for text in lines[turn.start:turn.end]:
            match = _DRAW_RE.match(text.strip())
            if match and int(match.group(2)) == 1:
                turn.player = int(match.group(1))
                break
    if len(turns) >= 2 and turns[0].player is None and turns[1].player is not None:
        turns[0].player = 1 - turns[1].player
    return turns


def _seat(lines: Sequence[str], our_cards: Sequence[str], marker: str = "") -> Optional[int]:
    """认我方在这条流里的座位：这条流那只 bot 自己（0）还是它的对手（1）。

    **为什么不是"第一张我方卡的移动"**：两副牌都有灰流丽这类通用卡时，对手先丢一张就会把座位认反。
    这里改成比"谁更像我方卡组"：数 ``0 's`` / ``1 's`` 的动作里各有多少我方卡组的卡，
    再加上"这条流那只 bot 自己的开局手牌"（第一条 dump 的 Hand 段）——那是它自己的手牌，
    算作 0 号位的证据。票数相等时才退回按 ``marker`` 认（第一张含 marker 的卡归谁）。
    """

    own = other = 0
    for text in lines:
        move = _MOVE_RE.search(text) or _APPEAR_RE.search(text)
        if not move:
            continue
        name = move.group(2)
        if "UnKnowCard" in name:
            continue
        if not any(card in name for card in our_cards):
            continue
        if int(move.group(1)) == 0:
            own += 1
        else:
            other += 1
    own_dumps = _dumps(lines)
    if own_dumps:
        own += sum(1 for text in own_dumps[0]["Hand"] if any(card in text for card in our_cards))
    if own == 0 and other == 0:
        return None
    if own != other:
        return 0 if own > other else 1
    if marker:
        for text in lines:
            move = _MOVE_RE.search(text) or _APPEAR_RE.search(text)
            if move and marker in move.group(2):
                return int(move.group(1))
    return None


def _board_in_turn(lines: Sequence[str], turn: _Turn, seat: int,
                    our_cards: Sequence[str]) -> Dict[str, int]:
    """回放某个回合里我方场面的变化（只报告怪兽区/魔陷区/墓地，内部按全区域记账）。

    窗口就是这个回合本身（``(Go to Draw)`` 到下一回合的 ``(Go to Draw)``）：
    结束阶段才成型的终端也算在里面，而**对手回合里我方丢掉的手坑不算**
    （失准现象② 就是被这个窗口起点害的，见模块 docstring）。

    **必须处理"叠放素材"**：Xyz 召唤时素材只会打一行 ``(0 's 主怪 overlay 素材)``，
    不会再有 ``from MonsterZone move to …``。早先只认 move/appear，素材就一直挂在场上：
    实测 b88 r2 回放数出 11 只怪（场上一共才 7 个格子），而场面 dump 只有 3 只。
    素材离开的是"它自己当前所在"的区域（绝大多数是怪兽区，从墓地/手牌当素材的效果也有），
    所以按 move/appear 记下来的全区域账目里挑一个有存货的减掉。
    """

    zones: Dict[str, Dict[str, int]] = {}

    def record(name: str, zone: str, delta: int) -> None:
        """按全区域记账（内部用；素材出账时要靠它找到这张卡原来在哪个区域）。"""

        slot = zones.setdefault(name, {})
        slot[zone] = max(0, slot.get(zone, 0) + delta)

    def bump(name: str, zone: str, delta: int) -> None:
        if not any(card in name for card in our_cards):
            return
        record(name, zone, delta)

    def bump_overlay_out(material: str) -> None:
        """叠放素材离场：从它现在所在的区域里减掉一张。"""

        if not any(card in material for card in our_cards):
            return
        slot = zones.get(material)
        if not slot:
            return
        for zone in ("MonsterZone", "SpellZone", "Grave", "Hand", "Deck", "Extra", "Removed", "Overlay"):
            if slot.get(zone, 0) > 0:
                record(material, zone, -1)
                return

    for text in lines[turn.start:turn.end]:
        overlay = _OVERLAY_RE.search(text)
        if overlay and int(overlay.group(1)) == seat:
            bump_overlay_out(overlay.group(3))
            continue
        move = _MOVE_RE.search(text)
        if move and int(move.group(1)) == seat:
            bump(move.group(2), move.group(3), -1)
            bump(move.group(2), move.group(4), +1)
            continue
        appear = _APPEAR_RE.search(text)
        if appear and int(appear.group(1)) == seat:
            bump(appear.group(2), appear.group(3), +1)

    result: Dict[str, int] = {}
    for name, slot in zones.items():
        for zone in ("MonsterZone", "SpellZone", "Grave"):
            if slot.get(zone, 0) > 0:
                result[f"{name}@{zone}"] = slot[zone]
    return result


def _interrupt_count(lines: Sequence[str], turns: Sequence[_Turn], seat: int) -> int:
    """我方在**对手回合**里发动效果的次数（＝这一回合实际用出来的阻抗次数）。

    回合归属直接用抽牌行算出来的 ``turn.player``（不是按 ``(Go to End)`` 数：
    两边每个回合都会打这一行，数不出是谁的回合）。
    """

    count = 0
    for turn in turns:
        if turn.player == seat:
            continue
        for text in lines[turn.start:turn.end]:
            match = _ACTIVATE_RE.search(text)
            if match and int(match.group(1)) == seat:
                count += 1
    return count


def _plan_of(lines: Sequence[str], turns: Sequence[_Turn], seat: int) -> str:
    """这一局我方第一条计划行（``第 N 回合计划：X``），没有就 ``-``。

    ⚠ 计划行**没有"是谁打的"标记**，而一条流里两边 bot 的回合都会出现：所以按"计划行写的回合号
    必须是我方回合"来挑（回合归属由抽牌行算出）。早先直接取第一条，会把对手的计划名算成我们的
    （实测 dbg93-94 里读 93 的牌，却报出 94 的「Rabbit 线」）。
    """

    our_turns = {turn.order for turn in turns if turn.player == seat}
    for text in lines:
        match = _PLAN_RE.search(text)
        if match and int(match.group(1)) in our_turns:
            return f"{match.group(2)}（第 {match.group(1)} 回合）"
    return "-"


def analyze_game(stream: _Stream, our_cards: Sequence[str], *, index: int, marker: str = "",
                 monster: Sequence[str] = (), spell: Sequence[str] = (),
                 grave: Sequence[str] = (), hand: Sequence[str] = (),
                 interrupt: bool = False) -> Tuple[Optional[GameStats], str]:
    """统计一局。返回 ``(结果, 跳过原因)``——跳过原因非空时结果为空。"""

    if not our_cards:
        return None, "没给我方卡名过滤名单（--deck/--cards-cdb 或 --deck-cards）"
    seat = _seat(stream.lines, our_cards, marker)
    if seat is None:
        return None, "这条流里没有我方卡组的卡（不是这一副牌的对局？）"
    turns = _turns(stream.lines)
    opening_hand: Optional[List[str]] = None
    if seat == 0:
        dumps = _dumps(stream.lines)
        opening_hand = list(dumps[0]["Hand"]) if dumps else []
        if hand and not all(any(want in text for text in opening_hand) for want in hand):
            return None, "起手不含 --hand 指定的卡"
    elif hand:
        # 我方是这条流那只 bot 的对手 ⇒ 它的 dump 里只有它自己的手牌，看不到我方起手。
        # **宁可跳过也不猜**：猜错会把"起手不匹配"的局算进验收。
        return None, "--hand 需要我方起手，而这条流里看不到（我方是会话那台 bot）"
    first_turn = next((turn for turn in turns if turn.player == seat), None)
    if first_turn is None:
        return None, "认不出我方回合（日志被截断？）"
    # ⚠ 2026-10-06：试过"用客户端 dump 快照当场面"（`_board_snapshot`），**已回退**——
    # 日志里 `我方` 那条流是两个客户端**混在一起**的（实测同一流里 `(0 draw 5 card)` 与
    # `(1 draw 5 card)` 各出现两次、两套 Bot Hand 交错），"第 k 个 dump"取到的可能是**对面**的
    # 场面快照（对面 Hand 里当然不会有我方卡，可一旦碰上同名牌就会错认）。要按 dump 记场面，
    # 必须按"每个客户端各自的那一份"，那需要改成按客户端切流（见 HANDOFF 的下一步）。
    # 现在这条账目仍然是**下界**：`我方` 前缀是插件的筛选日志、对方客户端里我方卡有时是
    # `UnKnowCard`，都可能漏。别把它当绝对值用。
    board = _board_in_turn(stream.lines, first_turn, seat, our_cards)
    board_source = "ledger"
    hit_monster = [name for name in monster if board.get(f"{name}@MonsterZone", 0) > 0]
    hit_spell = [name for name in spell if board.get(f"{name}@SpellZone", 0) > 0]
    hit_grave = [name for name in grave if board.get(f"{name}@Grave", 0) > 0]
    stats = GameStats(
        index=index,
        label=stream.label,
        first_line=stream.first_line,
        last_line=stream.last_line,
        seat=seat,
        first_turn=first_turn.order,
        turn_count=len(turns),
        board=board,
        board_source=board_source,
        matched=len(hit_monster) + len(hit_spell) + len(hit_grave),
        required=len(monster) + len(spell) + len(grave),
        hit_monster=hit_monster,
        hit_spell=hit_spell,
        hit_grave=hit_grave,
        plan=_plan_of(stream.lines, turns, seat),
        interrupts=_interrupt_count(stream.lines, turns, seat) if interrupt else 0,
        hand=opening_hand,
    )
    return stats, ""


def analyze_log(lines: Sequence[str], our_cards: Sequence[str], *, label: str = "",
                marker: str = "", monster: Sequence[str] = (), spell: Sequence[str] = (),
                grave: Sequence[str] = (), hand: Sequence[str] = (),
                interrupt: bool = False) -> LogReport:
    """统计整份日志（多局）：一局一条"带阵营名的 bot 流"，见模块 docstring。"""

    streams = _streams(lines, only_label=label or None)
    report = LogReport(stream_count=len(streams))
    for index, stream in enumerate(streams, start=1):
        stats, reason = analyze_game(stream, our_cards, index=index, marker=marker,
                                     monster=monster, spell=spell, grave=grave, hand=hand,
                                     interrupt=interrupt)
        if stats is None:
            report.skipped.append((index, reason))
        else:
            report.games.append(stats)
    return report


def _flags(stats: GameStats, monster: Sequence[str], spell: Sequence[str],
           grave: Sequence[str]) -> str:
    """把命中情况拼成 ``怪兽 ✘(1/2)`` 这种旗标（max_board 用不到，但人看日志最快）。"""

    parts: List[str] = []
    for names, hits, label in ((monster, stats.hit_monster, "怪兽"),
                               (spell, stats.hit_spell, "魔陷"),
                               (grave, stats.hit_grave, "墓地")):
        if not names:
            continue
        parts.append(f"{label} " + ("✔" if len(hits) == len(names) else f"✘({len(hits)}/{len(names)})"))
    return " ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="逐局统计首回合终场（计划层验收）")
    parser.add_argument("--log", required=True, type=Path, help="verbose 对局日志（--verbose-bots 跑出来）")
    parser.add_argument("--label", default="", help="只看某个日志前缀的流；不给就自动认（跳过 WindBot）")
    parser.add_argument("--marker", default="杀手级", help="认不出座位时用来救场的卡名片段（默认按杀调）")
    parser.add_argument("--monster", nargs="*", default=[], help="首回合结束时要求在**怪兽区**的卡名")
    parser.add_argument("--spell", nargs="*", default=[], help="首回合结束时要求在**魔陷区**的卡名")
    parser.add_argument("--hand", nargs="*", default=[],
                        help="**只看起手含这些卡的局**（验收具体线路时用：比如魔女术 18 步线的起手件）")
    parser.add_argument("--interrupt", action="store_true",
                        help="统计对面回合里我方发动效果的次数（＝实际用出来的阻抗次数）")
    parser.add_argument("--grave", nargs="*", default=[],
                        help="要求在**墓地**的卡名（教程有些终端是「盖放后发动、最后进墓地」，只看魔陷区会漏）")
    parser.add_argument("--deck-cards", nargs="*", default=None,
                        help="我方卡组的卡名；给了 --deck + --cards-cdb 时会自动读整副牌的名字")
    parser.add_argument("--deck", type=Path, default=None, help="我方 .ydk（自动取出这副牌的全部卡名当过滤名单）")
    parser.add_argument("--cards-cdb", type=Path, default=None, help="卡库 path（配合 --deck 查卡名）")
    args = parser.parse_args()

    lines = args.log.read_text(encoding="utf-8", errors="ignore").splitlines()
    our_cards = list(args.deck_cards) if args.deck_cards else []
    if not our_cards and args.deck and args.cards_cdb:
        our_cards = read_deck_cards(args.deck, args.cards_cdb)
    if not our_cards:
        our_cards = [args.marker]

    report = analyze_log(lines, our_cards, label=args.label, marker=args.marker,
                         monster=args.monster, spell=args.spell, grave=args.grave,
                         hand=args.hand, interrupt=args.interrupt)
    if report.stream_count == 0:
        print(f"**这份日志切不出一局**（{args.log.name}）：确认它是 `--verbose-bots` 跑出来的——"
              "统计只认带阵营名的 bot 流（`INFO [我方] …`）。")
        return 2

    print(f"共 {report.stream_count} 局（{args.log.name}）")
    progress = sum(1 for line in lines if _PROGRESS_RE.match(line))
    if progress and progress != report.stream_count:
        # 进度行是每局打完才打的：数量对不上说明日志被截断（或最后一局还没打完就收工），
        # 报出来比让数字悄悄少几局好。
        print(f"  ⚠ 进度行说有 {progress} 局，但只切出 {report.stream_count} 条 bot 流（日志截断？）")
    plans: Dict[str, int] = {}
    for index, reason in report.skipped:
        print(f"  第 {index:>2} 局：跳过（{reason}）")
    for stats in report.games:
        plans[stats.plan] = plans.get(stats.plan, 0) + 1
        shown = "、".join(f"{name}×{count}" for name, count in sorted(stats.board.items()))
        extra = f"｜对手回合拦截 {stats.interrupts} 次" if args.interrupt else ""
        if stats.seat == 0:
            seat_note = "我在本流"
        else:
            seat_note = "我在对面"
        print(f"  第 {stats.index:>2} 局〔{stats.label}｜行 {stats.first_line}~{stats.last_line}〕："
              f"座位{stats.seat}（{seat_note}）｜我方第 1 回合＝全场第 {stats.first_turn} 回合"
              f"｜{_flags(stats, args.monster, args.spell, args.grave)}"
              f"｜计划 {stats.plan}{extra}｜结束局面：{shown or '（空）'}")
    valid = len(report.games)
    matched_all = sum(1 for stats in report.games if stats.full)
    percent = f"{matched_all / valid * 100:.1f}%" if valid else "—"
    line = f"\n结果：{matched_all}/{valid} 局三件齐（{percent}）；计划分布 {plans}"
    if valid != report.stream_count:
        line += f"；另有 {report.stream_count - valid} 局跳过（见上）"
    print(line)
    interrupts = [stats.interrupts for stats in report.games] if args.interrupt else []
    if interrupts:
        hits = sum(1 for value in interrupts if value > 0)
        print(f"对手回合拦截：平均 {sum(interrupts) / len(interrupts):.2f} 次/局，"
              f"至少拦 1 次的局 {hits}/{len(interrupts)}（{hits / len(interrupts) * 100:.0f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
