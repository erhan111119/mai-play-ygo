"""从 verbose 对局日志里读「展开质量」与「手坑/陷阱时机」。

**为什么需要它**：用户反馈的是"展开一通场上只有一个怪""开局丢 G""手坑/陷阱乱发"这类
**过程**问题，而 brain_eval 只报胜率与动作数——动作数多不等于展开做出来了（乱发效果也算动作）。

只取三类**可靠**信号，宁可少也不要猜错：

1. **额外卡组出场**：落位行自带回合号与是哪一方，卡号在这副牌的额外卡表里就算一次额外展开。
2. **回合开始时的场面**：WindBot 每回合会打印一次自己的 `***Bot Monster***`，
   所以"某一方的第 N 个 dump"＝它第 N 个回合开始时的场面（不需要绝对回合号）。
3. **阻抗时机**：WindBot 打印的每个动作都带 `(0 's …)` / `(1 's …)`，其中 **0＝这个 bot 自己、
   1＝它的对手**（两边各自打印一遍整局）。所以看某次手坑发动**之前**最近的动作是谁的，
   就知道这次是"响应对手"还是"自己在自己回合白扔"。

日志行格式（WindBot + 插件 recorder）::

    INFO [我方] [26-10-03 14:06:05] *********Bot Monster*********
    INFO [我方] [26-10-03 14:06:05] 闪刀姬-燎里
    INFO [我方] [26-10-03 14:06:05] (0 's 闪刀姬-零衣 activate effect from MonsterZone)
    INFO [我方] [26-10-03 14:06:05] (1 's 增殖的G activate effect from Hand)
    INFO 落位：第 1 回合 我方（我方） 额外怪兽区 ← 闪刀姬-燎里

用法::

    python tools/board_report.py --log temp/train/smoke-96.log --deck <ydk> --cards-cdb <cards.cdb>
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

_LABEL_RE = re.compile(r"INFO \[([^\]]+)\] \[\d\d-\d\d-\d\d \d\d:\d\d:\d\d\] (.*)$")
_PLACEMENT_RE = re.compile(r"落位：第 (\d+) 回合 (.+?)（(我方|对方)） (\S+) ← (.*)$")
_PLACEMENT_VIEWER_RE = re.compile(r"落位：第 \d+ 回合 .+?（(我方|对方)）")
_ACTION_RE = re.compile(r"^\(([01]) 's (.*?) ((?:activate effect|from .*)$|.*\)$)")
_DUMP_RE = re.compile(r"^\*+(?:Bot (Hand|Spell|Monster)|(Finish))\*+$")

#: 需要看时机的卡（卡号 → 卡名）：手坑 + 常见"要掐时机"的阻抗
WATCH_IDS: Dict[int, str] = {
    14558127: "灰流丽",
    14558129: "灰流丽",
    23434538: "增殖的G",
    23434539: "增殖的G",
    94145022: "小丑与锁鸟",
    97268402: "效果遮蒙者",
    97268403: "效果遮蒙者",
    59438930: "幽鬼兔",
    59438931: "幽鬼兔",
    73642296: "屋敷童",
    73642297: "屋敷童",
    73642298: "屋敷童",
    52038441: "朔夜时雨",
    84192580: "欢聚友伴·海月水母",
    42141493: "欢聚友伴·长尾山雀",
    10045474: "无限泡影",
    40366668: "灵王的波动",
    91800273: "次元吸引者",
}


@dataclass
class TurnRow:
    """某方某个回合（按 dump 序号计）的记录。"""

    order: int
    board: List[str] = field(default_factory=list)
    hand: int = 0
    extra_cards: List[str] = field(default_factory=list)
    """这一回合从额外卡组出场的怪名（来自落位行，按绝对回合号匹配）。"""
    summons_all: int = 0
    """这一回合**所有来源**摆到怪兽区的次数（手牌/卡组/墓地/额外）＝"造了几个身体"。"""
    activated: List[str] = field(default_factory=list)
    """这一回合**我方发动过效果的卡名**（用来对照"这副牌的引擎件有没有用上"）。"""
    abs_turn: Optional[int] = None


@dataclass
class TimingEvent:
    """一次需要看时机的发动。"""

    side: str
    turn: Optional[int]
    card: str
    own_immediate: int
    """紧挨着的前 2 个动作里，有几个是"我们自己的"。"""
    enemy_immediate: int
    """紧挨着的前 2 个动作里，有几个是"对手的"。"""
    turn_owner: str = ""
    """发动时**是谁的回合**：靠"最近一次手牌转储是哪一方打印的"判断（WindBot 在自己回合开始时
    会打印自己的手牌，所以最后打印的那一方就是当前回合方）。这张表比动作流可靠。"""
    own_before: int = 0
    enemy_before: int = 0

    @property
    def verdict(self) -> str:
        """时机判断。

        ⚠ 只看**紧挨着的前 2 个动作**：早先按"前 8 个动作谁多"判，会把"我们回合刚做完一串、
        对手回合一开始就连锁手坑"误判成乱发（实测 40 局里这种假阳性很多）。真正能说明问题的是
        "这次发动是不是紧接着对手的动作"——脚本层现在也是按这个（连锁对手／对手回合）来拦的。
        """

        if self.enemy_immediate > 0 and self.own_immediate == 0:
            return "紧接对手动作 ✔"
        if self.own_immediate > 0 and self.enemy_immediate == 0:
            return "紧跟自己动作（可疑）"
        return "前后都有（需人工看）"


def read_deck(path: Path, cdb: Optional[Path]) -> Tuple[Set[int], Dict[int, str]]:
    """读 .ydk → (额外卡组卡号集合, 卡号→卡名)。"""

    extra: List[int] = []
    every: List[int] = []
    section = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("#main"):
            section = "main"
            continue
        if line.startswith("#extra"):
            section = "extra"
            continue
        if line.startswith("!side"):
            section = "side"
            continue
        if not line or line.startswith("#"):
            continue
        try:
            card_id = int(line)
        except ValueError:
            continue
        every.append(card_id)
        if section == "extra":
            extra.append(card_id)
    names: Dict[int, str] = {}
    if cdb is not None and cdb.is_file():
        con = sqlite3.connect(str(cdb))
        try:
            cur = con.cursor()
            for card_id in set(every):
                cur.execute("SELECT name FROM texts WHERE id=?", (card_id,))
                row = cur.fetchone()
                if row and row[0]:
                    names[card_id] = row[0]
        finally:
            con.close()
    return set(extra), names


def _looks_like_watched(card_name: str, watched_names: Sequence[str]) -> Optional[str]:
    for name in watched_names:
        if name and name in card_name:
            return name
    return None


def analyze(log_path: Path, extra_ids: Set[int], names: Dict[int, str]) -> Tuple[
    List[Tuple[Dict[str, List[TurnRow]], List[TimingEvent]]], List[str]
]:
    """解析一份日志（含多局）。返回 (每局的 (回合记录, 时机事件), 备注)。"""

    games: List[Tuple[Dict[str, List[TurnRow]], List[TimingEvent]]] = []
    rows: Dict[str, List[TurnRow]] = defaultdict(list)
    events: List[TimingEvent] = []
    notes: List[str] = []
    watched_names = sorted(set(WATCH_IDS.values()), key=len, reverse=True)

    dump_side: Optional[str] = None
    dump_kind = ""
    recent: Dict[str, List[int]] = defaultdict(list)  # label → 最近动作是谁（0 自己 / 1 对手）
    current_abs_turn: Optional[int] = None
    _seen_placements: Set[Tuple[str, str, str, str]] = set()
    extra_names = {names.get(i, f"#{i}"): i for i in extra_ids}
    turn_owner = ""

    def flush() -> None:
        """收束一局（日志里一局接一局，回合号会从头再来）。"""

        nonlocal rows, events
        if rows or events:
            games.append((rows, events))
        rows = defaultdict(list)
        events = []
        recent.clear()
        _seen_placements.clear()
        current_abs_turn = None

    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "WindBot starting..." in raw:
            flush()
            continue
        m_place = _PLACEMENT_RE.search(raw)
        if m_place:
            turn_no, placer, _viewer, zone, token = m_place.groups()
            # 每个 bot 各记一份落位（同一件事、两个"自己"视角：左边那份写「我方（我方）」，
            # 右边那份写「对手(SkyStrikerShop)（我方）」）——按 (回合, 谁放的, 格, 卡) 去重。
            side = "我方" if placer.strip() == "我方" else "对手"
            key = (turn_no, side, zone, token.strip())
            if key in _seen_placements:
                continue
            _seen_placements.add(key)
            current_abs_turn = int(turn_no)
            card_name = None
            m_id = re.search(r"#(\d+)", token)
            if m_id and int(m_id.group(1)) in extra_ids:
                card_name = names.get(int(m_id.group(1)), f"#{m_id.group(1)}")
            else:
                for name, cid in extra_names.items():
                    if name and name in token:
                        card_name = name
                        break
            if card_name:
                # 落到该方"当前这一回合"的记录上（还没有就建一条）
                if not rows[side] or rows[side][-1].abs_turn not in (None, current_abs_turn):
                    rows[side].append(TurnRow(order=len(rows[side]) + 1, abs_turn=current_abs_turn))
                rows[side][-1].extra_cards.append(card_name)
                if rows[side][-1].abs_turn is None:
                    rows[side][-1].abs_turn = current_abs_turn
            continue

        m_label = _LABEL_RE.match(raw)
        if not m_label:
            continue
        label, body = m_label.groups()
        if label.strip() == "WindBot":
            # brain_eval 会把两边 bot 的原始输出**再透传一份**（统一叫 WindBot），
            # 和带阵营的那份完全重复；只留带阵营的那份，不然每件事都算两遍。
            continue
        body = body.strip()
        side_key = "我方" if label.strip() == "我方" else "对手"
        label = side_key
        m_dump = _DUMP_RE.match(body)
        if m_dump:
            kind = m_dump.group(1) or m_dump.group(2)
            if kind == "Finish":
                dump_side = None
                dump_kind = ""
            else:
                dump_kind = kind
                dump_side = label
                if kind == "Hand":
                    # 谁在自己回合开始打印手牌 ⇒ 这一回合归谁
                    turn_owner = label
                    rows[label].append(TurnRow(order=len(rows[label]) + 1))
            continue
        if dump_side == label and dump_kind in ("Hand", "Monster") and body:
            if not rows[label]:
                rows[label].append(TurnRow(order=1))
            row = rows[label][-1]
            if dump_kind == "Hand":
                row.hand += 1
            else:
                row.board.append(body)
            continue
        m_action = _ACTION_RE.match(body)
        if m_action:
            actor_digit = int(m_action.group(1))
            card_name = m_action.group(2).strip()
            raw_action = m_action.group(3).strip()
            recent[label].append(actor_digit)
            if actor_digit == 0:
                # 额外卡组出场：动作流里写得最清楚（落位记录会漏掉一部分额外召唤）
                if "from Extra move to MonsterZone" in raw_action:
                    if not rows[label]:
                        rows[label].append(TurnRow(order=1))
                    rows[label][-1].extra_cards.append(card_name)
                    if rows[label][-1].abs_turn is None:
                        rows[label][-1].abs_turn = current_abs_turn
                elif "move to MonsterZone" in raw_action:
                    # 从手牌/卡组/墓地摆上场的怪（＝"这一回合造了几个身体"，
                    # 卡通这种靠手牌·卡组铺场的牌只看额外召唤会低估）
                    if not rows[label]:
                        rows[label].append(TurnRow(order=1))
                    rows[label][-1].summons_all += 1
                    if rows[label][-1].abs_turn is None:
                        rows[label][-1].abs_turn = current_abs_turn
                if "activate effect" in raw_action:
                    # 这一回合我方发动过效果的卡（用来看"引擎件有没有用上"）
                    if not rows[label]:
                        rows[label].append(TurnRow(order=1))
                    rows[label][-1].activated.append(card_name)
                watched = _looks_like_watched(card_name, watched_names)
                if watched:
                    before = recent[label][-9:-1]
                    immediate = recent[label][-3:-1]
                    events.append(
                        TimingEvent(
                            side=label,
                            turn=current_abs_turn,
                            card=watched,
                            own_immediate=sum(1 for d in immediate if d == 0),
                            enemy_immediate=sum(1 for d in immediate if d == 1),
                            turn_owner=turn_owner,
                            own_before=sum(1 for d in before if d == 0),
                            enemy_before=sum(1 for d in before if d == 1),
                        )
                    )
            continue
    flush()
    if not games:
        notes.append("没解析出任何回合记录——日志格式或 verbose 开关不对？")
    return games, notes


def report(games: Sequence[Tuple[Dict[str, List[TurnRow]], List[TimingEvent]]]) -> None:
    """打印汇总（跨局聚合）。"""

    print(f"共 {len(games)} 局")
    per_side_first: Dict[str, List[int]] = defaultdict(list)
    per_side_max: Dict[str, List[int]] = defaultdict(list)
    per_side_bodies: Dict[str, List[int]] = defaultdict(list)
    per_side_board1: Dict[str, List[int]] = defaultdict(list)
    timing: Counter = Counter()
    cards: Counter = Counter()
    for rows, events in games:
        for side, turns in rows.items():
            if not turns:
                continue
            per_side_first[side].append(len(turns[0].extra_cards))
            per_side_max[side].append(max(len(t.extra_cards) for t in turns))
            per_side_bodies[side].append(turns[0].summons_all)
            if turns[0].board:
                per_side_board1[side].append(len([b for b in turns[0].board if b]))
        for ev in events:
            timing[ev.verdict] += 1
            if ev.turn_owner:
                timing["  · 我的回合" if ev.turn_owner == ev.side else "  · 对手回合"] += 1
            cards[ev.card] += 1
    print("\n=== 额外出场（动作流里 'from Extra move to MonsterZone'） ===")
    for side in per_side_first:
        first = per_side_first[side]
        best = per_side_max[side]
        avg_first = sum(first) / len(first)
        zero = sum(1 for v in first if v == 0)
        ok = sum(1 for v in first if v >= 2)
        print(f"  {side}: 首回合平均 {avg_first:.1f} 只（0 只 {zero}/{len(first)} 局，"
              f"**≥2 只 {ok}/{len(first)} 局 = {ok / len(first):.0%}**）"
              f"｜单回合最多 {max(best)}｜明细 {first}")
    print("\n=== 首回合造身体数（所有来源摆到怪兽区） ===")
    for side, values in per_side_bodies.items():
        avg = sum(values) / len(values) if values else 0
        ok = sum(1 for v in values if v >= 2)
        print(f"  {side}: 平均 {avg:.1f} 只（≥2 只 {ok}/{len(values)} 局）｜明细 {values}")
    print("\n=== 第 1 个回合发动过效果的卡（前 20 名）===")
    for side in per_side_first:
        activated: Counter = Counter()
        for rows, _events in games:
            turns = rows.get(side) or []
            if turns:
                activated.update(turns[0].activated)
        top = "、".join(f"{name}×{count}" for name, count in activated.most_common(20))
        print(f"  {side}: {top if top else '(无)'}")
    print("\n=== 第 1 个回合开始时的场面怪数 ===")
    for side, values in per_side_board1.items():
        print(f"  {side}: {values}")

    print("\n=== 手坑/阻抗的时机 ===")
    for verdict, count in timing.most_common():
        print(f"  {verdict}: {count}")
    if cards:
        print("  按卡：", dict(cards.most_common()))


def main() -> int:
    parser = argparse.ArgumentParser(description="从 verbose 日志看展开量与阻抗时机")
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--deck", required=True, type=Path, help="这副牌的 .ydk")
    parser.add_argument("--cards-cdb", type=Path, default=None)
    args = parser.parse_args()

    extra_ids, names = read_deck(args.deck, args.cards_cdb)
    games, notes = analyze(args.log, extra_ids, names)
    report(games)
    for note in notes:
        print("⚠", note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
