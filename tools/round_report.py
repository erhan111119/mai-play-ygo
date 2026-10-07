"""一轮迭代的成绩单：读擂台库（``temp/train/rounds.db``）把每轮的每局摊开看。

用法::

    python tools/round_report.py --arena r1          # 看第 1 轮（含对空白那几组）
    python tools/round_report.py --arena r1 --pairs  # 额外把逐局明细打出来

**为什么要单独一个工具**：一轮里 42 局，只有"胜负"看不出脚本好坏——
真正要找的是「整局不出牌（动作数 <3）」「报错」「超时」这三类异常，
以及"某副牌对某个对手一把没赢"这种成片的问题；把这些直接从库里挑出来，
省掉在几十个日志里翻找。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

# 动作数低于这个值视为「整局基本没出牌」，通常是脚本与卡表不匹配或开局就崩
#
# 口径提醒（2026-10-07 起）：`actions` 由 train/arena.py 汇总，含**盖放**（内核为盖放单发
# MSG_SET，原来没人接）。改之前"只盖陷阱"的局会被误报成"整局没出牌"——虫惑魔/闪刀的后手局
# 大量命中这一类。跨 0.21.106 前后的两轮不能直接比动作数。
SILENT_ACTIONS = 3

# 「打得很憋屈」的门槛：回合数够多（说明不是被速杀）却几乎没动作
LOW_ACTION = 8
LOW_ACTION_TURNS = 4

# 这副牌没有特召＝脚本没在展开（都是"额外卡组就是打法"的牌）
COMBO_DECKS = ("RaiseMoon", "WitchcraftShop", "ToonShop", "KillerTune", "SkyStrikerShop", "TraptrixRagnaraika")


def load_rows(db_path: Path, arena: str) -> List[sqlite3.Row]:
    """取出这一轮的每一局（arena 以 ``<轮号>`` 开头，含 ``-blank`` 那些）。"""

    if not db_path.exists():
        raise SystemExit(f"库不存在：{db_path}（先跑 bash temp/round.sh <轮号>）")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT * FROM duels WHERE arena = ? OR arena LIKE ? ORDER BY rowid",
        (arena, f"{arena}-%"),
    ).fetchall()
    connection.close()
    return rows


def side_names(row: sqlite3.Row) -> Tuple[str, str]:
    """一局的左右两侧名（``left_name`` 是我方、``right_name`` 是对手）。"""

    return str(row["left_name"]), str(row["right_name"])


def style_key(row: sqlite3.Row, *, right: bool = False) -> str:
    """统计用的"这是哪副牌"。

    新导入的卡组还没专属执行器时，脚本名都是通用 ``Test``——按脚本名汇总会把它们混成一行，
    所以遇到 ``Test`` 退回**卡组名**（擂台用的是插件给它的显示名；新四副在 round10.sh 里
    用 ``--left-name`` 传了中文名，右侧则用 ``--right-style`` 之外的名字会丢，所以右侧统一
    用 ``right_style``，必要时退回 ``right_name``）。
    """

    style = str(row["right_style"] if right else row["left_style"])
    name = str(row["right_name"] if right else row["left_name"])
    if style == "Test" and name and name != "我方":
        return name
    return style


def summarize(rows: Sequence[sqlite3.Row]) -> str:
    """按"这是哪副牌"汇总胜负与过程指标（**两边都算**）。

    为什么要两边都算：一轮里每对只跑一次（左＝编号小的那副），只统计左方会把各副牌的局数
    算成 2~18 不等（新导入的牌编号大，几乎不会被当左方）；两边都算后每副牌都是"和其余九副各打一场"。

    新导入的卡组还没专属执行器时脚本名都是通用 ``Test``——按脚本名汇总会把它们混成一行，
    所以遇到 ``Test`` 退回**卡组名**（见 :func:`style_key`）。
    """

    stats: Dict[str, Dict[str, float]] = {}
    for row in rows:
        for style, name, won, actions, effects in (
            (style_key(row), str(row["left_name"]), str(row["winner"]) == str(row["left_name"]),
             row["actions_left"], row["effects_left"]),
            (style_key(row, right=True), str(row["right_name"]), str(row["winner"]) == str(row["right_name"]),
             row["actions_right"], row["effects_right"]),
        ):
            bucket = stats.setdefault(style, {"局": 0, "胜": 0, "动作": 0.0, "回合": 0.0, "效果": 0.0})
            bucket["局"] += 1
            bucket["胜"] += 1 if won else 0
            bucket["动作"] += float(actions or 0)
            bucket["回合"] += float(row["turns"] or 0)
            bucket["效果"] += float(effects or 0)

    lines = ["脚本              局  胜   胜率   平均动作  平均回合  平均效果"]
    for style, item in sorted(stats.items(), key=lambda kv: -kv[1]["胜"] / max(1, kv[1]["局"])):
        games = max(1, int(item["局"]))
        lines.append(
            f"{style:<16}{int(item['局']):>3}{int(item['胜']):>4}"
            f"{item['胜'] / games * 100:>7.0f}%{item['动作'] / games:>10.1f}"
            f"{item['回合'] / games:>10.1f}{item['效果'] / games:>10.1f}"
        )
    return "\n".join(lines)


def sets_of(row: sqlite3.Row, seat: str) -> int:
    """某一侧盖放了几次（``我方``/``对手``）。

    异常行里一定要带上它：动作总数含盖放，只有拆开才分得清"这局 2 个动作"是"盖了 2 张牌"
    （卡手/防守型起手）还是"召唤 1 只 + 发 1 个效果"（动过）。0.21.106 之前落库的行没有这一列
    （补列后是默认 0），所以老轮次读出来一律是 0——那是缺数据，不是"没盖过牌"。
    """

    key = "sets_left" if seat == "我方" else "sets_right"
    try:
        return int(row[key] or 0)
    except (IndexError, KeyError):
        return 0


def find_problems(rows: Sequence[sqlite3.Row]) -> List[str]:
    """挑出需要翻日志的三类局：整局不出牌、报错、超时/空转。"""

    problems: List[str] = []
    for row in rows:
        left, right = side_names(row)
        tag = f"{row['arena']} #{row['duel_id']} {left} vs {right}"
        if row["error"]:
            problems.append(f"报错   {tag}｜{row['error']}")
        for seat, actions in (("我方", row["actions_left"]), ("对手", row["actions_right"])):
            if str(row["arena"]).endswith("-blank") and seat == "对手":
                # 对白空的局里对手不设防（通用脚本且卡表是墙），动作少是**设计如此**，不是问题
                continue
            if int(actions or 0) < SILENT_ACTIONS:
                style = row["left_style"] if seat == "我方" else row["right_style"]
                problems.append(
                    f"不出牌 {tag}｜{seat}（{style}）动作只有 {actions}"
                    f"（其中盖放 {sets_of(row, seat)}），回合 {row['turns']}"
                    f"，终局 LP {row['lp_left']}:{row['lp_right']}"
                )
        if int(row["turns"] or 0) <= 1 and str(row["reason"] or "") != "基本分变成0":
            problems.append(f"空转   {tag}｜回合 {row['turns']}，原因 {row['reason']}")
        # 「打得很憋屈」：打得够久（不是被速杀）却没几个动作，多半是脚本在某个环节空转
        if (int(row["turns"] or 0) >= LOW_ACTION_TURNS
                and int(row["actions_left"] or 0) < LOW_ACTION
                and not str(row["arena"]).endswith("-blank")):
            problems.append(
                f"少动作 {tag}｜我方（{row['left_style']}）动作只有 {row['actions_left']}"
                f"（其中盖放 {sets_of(row, '我方')}）、{row['turns']} 回合、特召 {row['sp_summons_left']}"
                f"、效果 {row['effects_left']}，终局 LP {row['lp_left']}:{row['lp_right']}"
            )
        # 展开副"一个特召都没有"：这类牌做不出额外怪就是脚本问题——**但要赢的局不算**
        # （靠通召打赢的局本来就该没特召，第 10 轮就误报过一次 12 动作赢下的局）
        if (not str(row["arena"]).endswith("-blank") and int(row["turns"] or 0) >= LOW_ACTION_TURNS
                and int(row["sp_summons_left"] or 0) == 0 and str(row["left_style"]) in COMBO_DECKS
                and row["winner"] != row["left_name"]):
            problems.append(
                f"零特召 {tag}｜我方（{row['left_style']}）{row['turns']} 回合一次特召都没有"
                f"，动作 {row['actions_left']}（盖放 {sets_of(row, '我方')}、效果 {row['effects_left']}）"
                f"、终局 LP {row['lp_left']}:{row['lp_right']}"
                f"｜注：对手是有阻抗/封锁的牌，先看对手这一局用了什么再判"
            )
    return problems


def pair_lines(rows: Sequence[sqlite3.Row]) -> List[str]:
    """逐局明细：谁先手、谁赢、各自动作数。"""

    lines: List[str] = []
    for row in rows:
        left, right = side_names(row)
        winner = str(row["winner"] or "?")
        mark = "胜" if winner == left else "负"
        lines.append(
            f"{mark} {row['arena']:<10} {left}（{row['left_style']}） vs {right}（{row['right_style']}）"
            f"｜{row['turns']} 回合 {row['duration_seconds']:.0f}s"
            f"｜动作 {row['actions_left']}/{row['actions_right']}"
            f"｜LP {row['lp_left']}:{row['lp_right']}｜{row['reason']}"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="一轮迭代的成绩单")
    parser.add_argument("--arena", required=True, help="轮号，如 r1")
    parser.add_argument("--db", default="temp/train/rounds.db", help="擂台库路径")
    parser.add_argument("--pairs", action="store_true", help="打印逐局明细（默认只打异常）")
    args = parser.parse_args()

    rows = load_rows(Path(args.db), args.arena)
    if not rows:
        print(f"库里没有 {args.arena} 的局——先跑 bash temp/round.sh {args.arena.lstrip('r')}")
        return 1

    print(summarize(rows))
    problems = find_problems(rows)
    print(f"\n=== 异常 {len(problems)} 处 ===")
    for line in problems or ["（无）"]:
        print(" " + line)
    if args.pairs:
        print("\n=== 逐局 ===")
        for line in pair_lines(rows):
            print(" " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
