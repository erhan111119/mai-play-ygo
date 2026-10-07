"""低动作局审计：把一份 verbose 对局日志里"某一侧几乎没出牌"的局挑出来，并把它**开局手牌**摆出来。

**为什么要有它**（2026-10-07）：项目里"整局不出牌"这类异常一直靠 ``tools/round_report.py`` 从擂台库
挑出来，但库里的行**没有手牌**——于是永远分不清两种情况：

* **卡手**：起手就是一堆打不出去的牌（手坑/需要配合的件），盖两张就过 —— 记进卡表问题清单，不是 bug；
* **卡死**：手里明明有动点，脚本却一步不走 —— 真 bug，要拿 ``decision_audit``＋探针继续查。

手牌只有 verbose 日志里有（WindBot 每回合打 ``*********Bot Hand*********`` 块），而**只有串行
（``--parallel 1``）跑出来的日志**才能按局切段：并发一开，两个 bot 的输出互相穿插，段与段分不开。
所以本工具会先读日志头的"并发 N"，N>1 时直接判红（那份日志切不准，别拿来下结论）。

⚠ **口径**：动作数含**盖放**（2026-10-06 起，见 ``duel/recorder.py`` 的 ``sets``）。所以
"动作 2"可能是"盖了 2 张牌"，也可能是"召唤 1 只 + 发 1 个效果"——本工具把两侧的手牌都打出来，
就是为了让人一眼看出是哪一种。

⚠ **手牌不是精确的"起手 5 张"**：那是 WindBot 每回合自己打的块，第一次 dump 时它**已经出过 0~1 张**
（实测有手牌块是 4 张的）。判"卡手 vs 卡死"够用（看得出整手是不是手坑堆），别拿它当精确手牌记录。
另外日志标签（``[我方]``/``[对手(X)]``/``[WindBot]``）会串——同一副牌的手牌有时挂在别的标签下，
所以给了 ``--left-ydk``/``--right-ydk`` 时工具按**牌表归属**判"这手属于哪一边"，比标签可信。

用法::

    # 串行 + verbose 跑一份日志（复现单局必须不洗切，见 train/arena.py 的 shuffle_seed_base）
    python tools/brain_eval.py --deck-id 93 --left-style WitchcraftShop --opponent <右方.ydk> \\
      --right-style SkyStrikerShop --rounds 10 --parallel 1 --verbose-bots \\
      --windbot-exe <WindBot 源码树>/bin/Release/WindBot.exe \\
      --db temp/train/rounds.db --arena silent93 > temp/rounds/silent93.log
    python tools/silent_game_audit.py --log temp/rounds/silent93.log

判读：把"低动作那一侧的**开局手牌**"拿去对牌表——整手都是手坑/无动点 ⇒ 卡手；有动点却没走上 ⇒ 卡死。

⚠ **"零特召"那类异常这里挑不出来**（日志的结果行只有动作数，没有特召数）。那类先去库里挑：
``SELECT duel_id, sp_summons_left, sp_summons_right FROM duels WHERE arena='S1' ORDER BY rowid``
——**并行 1 时行序＝日志里"每局打完"那行的顺序**，第 n 行就是第 n 局，再拿 ``--section n`` 看它的起手。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: 默认卡库（查卡名用；只有给了 --left-ydk/--right-ydk 才需要）：用插件自带客户端里那份
_CARDS_CDB = Path(__file__).resolve().parent.parent / "clients" / "ygopro" / "cards.cdb"

#: 擂台每局打完那行：``  [3/20] 第 4 轮 → 对手(SkyStrikerShop)（2 回合，动作 0:17）``
_RESULT_RE = re.compile(
    r"^\s*\[(?P<done>\d+)/(?P<total>\d+)\]\s+第\s+(?P<round>\d+)\s+轮\s+→\s+(?P<winner>.+?)"
    r"（(?P<turns>\d+)\s+回合，动作\s+(?P<left>\d+):(?P<right>\d+)）\s*$"
)

#: 日志里带进程标签的行：``INFO [我方] [26-10-07 00:12:00] WindBot starting...``
_LABELED_RE = re.compile(r"^INFO \[(?P<who>[^\]]*)\] \[[^\]]*\] (?P<body>.*)$")

#: 并发数那行：``每轮 10 轮镜像（20 局），并发 6``
_PARALLEL_RE = re.compile(r"并发\s+(?P<n>\d+)")

#: WindBot 的手牌块头：``*********Bot Hand*********``
_HAND_HEAD = "Bot Hand"
_BLOCK_END = "*******"


class DuelSection:
    """一局的日志段（同一局里两个 bot 的输出）。"""

    def __init__(self, index: int, winner: str, turns: int, left: int, right: int, round_index: int) -> None:
        self.index = index
        self.winner = winner
        self.turns = turns
        self.actions_left = left
        self.actions_right = right
        self.round_index = round_index
        self.hands: Dict[str, List[str]] = {}
        """标签 → 开局手牌（每个标签只留**第一次** dump，那才是起手）。"""

        self.lines: List[str] = []

    def feed(self, line: str) -> None:
        """把一行喂进这一局（顺手抽手牌与落位行）。"""

        self.lines.append(line)
        match = _LABELED_RE.match(line)
        if not match:
            return
        who, body = match.group("who"), match.group("body").strip()
        if _HAND_HEAD in body:
            self._pending = who
            self._cards = []
            return
        pending = getattr(self, "_pending", None)
        if pending is None:
            return
        if body.startswith(_BLOCK_END) or not body:
            if self._cards:
                self.hands.setdefault(pending, list(self._cards))
            self._pending = None
            return
        self._cards.append(body)

    def low_sides(self, min_actions: int) -> List[Tuple[str, int, str]]:
        """动作数低于门槛的侧：``[(侧, 动作数, 对手标签)]``。"""

        both = [("我方", self.actions_left), ("对手", self.actions_right)]
        if min(self.actions_left, self.actions_right) >= min_actions:
            return []
        return [(seat, count, both[1][0] if seat == "我方" else both[0][0]) for seat, count in both
                if count < min_actions]

    def placement_lines(self) -> List[str]:
        """这一局里 recorder 打的落位行（谁在第几回合把什么摆到了哪个格）。"""

        return [line for line in self.lines if "落位：" in line]


def split_sections(text: str) -> Tuple[List[DuelSection], int]:
    """按"每局打完那行"把日志切成段，返回 ``(各局, 日志里的并发数)``。

    切法是"**结果行之前**的行属于这一局"（擂台是打完立刻打印结果行）：攒着行，遇到结果行就
    用攒下的行建一局、清空重攒。**反过来（结果行之后算这一局）会整体错位一局**——段与段能对上数，
    手牌却贴到了下一局身上，比不报还糟（`tests/test_silent_game_audit.py` 钉着这条）。
    """

    parallel = 1
    for line in text.splitlines():
        found = _PARALLEL_RE.search(line)
        if found:
            parallel = int(found.group("n"))
            break
    sections: List[DuelSection] = []
    pending: List[str] = []
    for line in text.splitlines():
        match = _RESULT_RE.match(line)
        if not match:
            pending.append(line)
            continue
        section = DuelSection(
            index=len(sections) + 1,
            winner=match.group("winner").strip(),
            turns=int(match.group("turns")),
            left=int(match.group("left")),
            right=int(match.group("right")),
            round_index=int(match.group("round")),
        )
        for buffered in pending:
            section.feed(buffered)
        section.lines.append(line)
        sections.append(section)
        pending.clear()
    return sections, parallel


def load_deck_names(ydk: Path, cdb: Path) -> "set[str]":
    """一副牌的**全部卡名**（主卡组 + 额外卡组；异画/复刻号按 alias 也认）。

    为什么要查卡名而不是卡号：日志里的手牌是**卡名**（WindBot 自己打的），而 .ydk 里是卡号。
    """

    ids: List[int] = []
    section = ""
    for line in ydk.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if text.startswith("#main") or text.startswith("#extra"):
            section = "main"
            continue
        if text.startswith("!side"):
            section = "side"
            continue
        if section in ("main", "") and text.isdigit():
            ids.append(int(text))
    if not ids:
        return set()
    connection = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True)
    try:
        names = set()
        for card_id in ids:
            row = connection.execute("SELECT alias FROM datas WHERE id = ?", (card_id,)).fetchone()
            for candidate in (card_id, int(row[0]) if row and row[0] else 0):
                if not candidate:
                    continue
                found = connection.execute("SELECT name FROM texts WHERE id = ?", (candidate,)).fetchone()
                if found and found[0]:
                    names.add(str(found[0]))
        return names
    finally:
        connection.close()


def guess_side(cards: Sequence[str], left: "set[str]", right: "set[str]") -> str:
    """这一手起手更像哪一边的牌表（日志标签不可靠时用；两边都没给牌表就返回空串）。"""

    if not left and not right:
        return ""
    in_left = sum(1 for card in cards if card in left)
    in_right = sum(1 for card in cards if card in right)
    if in_left == in_right:
        return "（两边牌表都含这些卡，判不出）"
    return f"（{in_left}/{len(cards)} 张属左方牌表）" if in_left > in_right else f"（{in_right}/{len(cards)} 张属右方牌表）"


def describe(
    section: DuelSection,
    min_actions: int,
    *,
    hands: bool,
    placements: bool,
    left_names: "set[str]",
    right_names: "set[str]",
) -> List[str]:
    """把一局压成要打印的几行。"""

    lines = [
        f"--- 第 {section.index} 局（第 {section.round_index} 轮）：{section.winner} 胜，"
        f"{section.turns} 回合，动作 我方 {section.actions_left} / 对手 {section.actions_right}"
    ]
    for seat, count, _other in section.low_sides(min_actions):
        lines.append(f"    ⚠ {seat} 动作只有 {count}（门槛 {min_actions}）")
    if hands:
        for who, cards in section.hands.items():
            hint = guess_side(cards, left_names, right_names)
            lines.append(f"    起手[{who}]{hint}：{'/'.join(cards) if cards else '（空）'}")
    if placements:
        for line in section.placement_lines()[:8]:
            lines.append("    " + line.split("] ")[-1])
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="低动作局审计：挑出几乎没出牌的局并摆出它的起手")
    parser.add_argument("--log", required=True, help="verbose 日志（**必须 --parallel 1 跑出来**）")
    parser.add_argument("--min-actions", type=int, default=3, help="低于这个动作数算「几乎没出牌」（默认 3）")
    parser.add_argument("--all", action="store_true", help="不只看异常局，打印每一局")
    parser.add_argument(
        "--section",
        type=int,
        default=0,
        help="只看第 N 局（1 起数）——配合擂台库挑出来的局用：库里有 sp/盖放逐局读数，日志里没有",
    )
    parser.add_argument("--no-hands", action="store_true", help="不打起手手牌")
    parser.add_argument("--placements", action="store_true", help="把落位行也打出来（看它到底摆了什么）")
    parser.add_argument("--left-ydk", default="", help="左方卡表 .ydk：给了就能判「这手属于哪一边」")
    parser.add_argument("--right-ydk", default="", help="右方卡表 .ydk")
    parser.add_argument("--cards-cdb", default=str(_CARDS_CDB), help="卡库（查卡名用）")
    args = parser.parse_args(argv)

    path = Path(args.log)
    if not path.is_file():
        print(f"[错误] 找不到日志：{path}")
        return 2
    left_names = load_deck_names(Path(args.left_ydk), Path(args.cards_cdb)) if args.left_ydk else set()
    right_names = load_deck_names(Path(args.right_ydk), Path(args.cards_cdb)) if args.right_ydk else set()
    sections, parallel = split_sections(path.read_text(encoding="utf-8", errors="ignore"))
    if not sections:
        print("[错误] 这份日志里没有「每局打完」那行——它可能是没开 --verbose-bots，或者根本不是擂台日志。")
        return 2
    if parallel > 1:
        print(
            f"[错误] 这份日志是 --parallel {parallel} 跑出来的：两个 bot 的输出互相穿插，局与局切不开，"
            "手牌会对错局。请用 --parallel 1 重跑一份。"
        )
        return 2

    flagged = [s for s in sections if s.low_sides(args.min_actions)]
    print(f"日志：{path}｜共 {len(sections)} 局｜低动作（<{args.min_actions}）{len(flagged)} 局")
    if args.section:
        picked = [s for s in sections if s.index == args.section]
        if not picked:
            print(f"[错误] 这份日志里没有第 {args.section} 局（共 {len(sections)} 局）")
            return 2
        shown = picked
    else:
        shown = sections if args.all else flagged
    if not shown:
        print("（这一批没有低动作局）")
        return 0
    for section in shown:
        for line in describe(
            section,
            args.min_actions,
            hands=not args.no_hands,
            placements=args.placements,
            left_names=left_names,
            right_names=right_names,
        ):
            print(line)
    print(
        "\n判读：低动作那一侧的**起手**全是没有动点的牌（手坑/需要配合的件）⇒ 卡手；"
        "起手有动点却一步不走 ⇒ 卡死，拿 tools/decision_audit.py 继续查。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
