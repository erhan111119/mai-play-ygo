"""两轮对比：把"改过的那几副"和"没动的那几副"放在一起看（A/B 的标准读法）。

用法::

    python tools/ab_compare.py --base s8 --treat sh9 --changed KillerTune,Yaosheng

**为什么要它**：单看"改过的这副牌涨了几个点"会被两件事污染——

* **换了一批洗牌**（`--shuffle` 档不可复现，见 `docs/deck-audit.md` 的口径说明），
  同一副牌换一轮本来就会漂 ±5 个点；
* **环境漂移**（同一轮里各副牌的相对强弱、对手的临场）。

所以正确的读法是**差分对照**：拿"没改动的那些牌"在这两轮里的平均漂移当基准，
看改过的牌是"**超出**这个漂移"还是"跟着一起漂"。工具直接把三列并排打出来：
基线 / 处理 / 差，最后给"未改动平均漂移"和"调整后的净变化"。

⚠ 单卡互换的效应本来就小（预计 0~5 点），而整轮每副牌只有 ~144 局（95% 区间约 ±8 点）⇒
**这个量具只能否掉"大幅变差"，证不了"小幅变好"**。要判小改动得攒多轮（把 --base/--treat
换成多轮的逗号列表，工具会把它们合起来算）。
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

DEFAULT_DB = Path("temp/train/rounds.db")


def load_scores(db_path: Path, arenas: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    """`风格名 → (胜, 局)`：左右两侧都算（同一副牌在两侧时按各自那一侧记）。"""

    if not db_path.exists():
        raise SystemExit(f"库不存在：{db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    marks = ",".join("?" * len(arenas))
    rows = connection.execute(
        f"SELECT * FROM duels WHERE arena IN ({marks})", tuple(arenas)
    ).fetchall()
    connection.close()

    scores: Dict[str, List[int]] = {}
    for row in rows:
        winner = str(row["winner"])
        # 每一局都算给左右两副牌各一次（"这一侧赢了吗"），与 round_report 的汇总口径一致
        for right in (False, True):
            style = str(row["right_style"] if right else row["left_style"])
            name = str(row["right_name"] if right else row["left_name"])
            if style == "Test" and name and name != "我方":  # 与 round_report.style_key 同一口径
                style = name
            bucket = scores.setdefault(style, [0, 0])
            bucket[1] += 1
            if winner == name:
                bucket[0] += 1
    return {style: (wins, games) for style, (wins, games) in scores.items()}


def wilson(wins: int, games: int) -> Tuple[float, float]:
    """胜率的 95% Wilson 区间（小样本下比正态近似稳）。"""

    if not games:
        return (0.0, 0.0)
    z, p = 1.96, wins / games
    denom = 1 + z * z / games
    center = (p + z * z / (2 * games)) / denom
    half = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / denom
    return (center - half, center + half)


def compare(
    base: Dict[str, Tuple[int, int]],
    treat: Dict[str, Tuple[int, int]],
    changed: Sequence[str],
) -> Tuple[List[Tuple[str, float, float, float, int]], float]:
    """算出每一副的差与"未改动牌的平均漂移"。

    返回 `(每副一行, 未改动平均漂移)`；每行是 `(风格名, 基线胜率, 处理胜率, 差, 局数)`。
    """

    rows: List[Tuple[str, float, float, float, int]] = []
    drifts: List[float] = []
    for style in sorted(set(base) | set(treat)):
        b = base.get(style, (0, 0))
        t = treat.get(style, (0, 0))
        if not b[1] or not t[1]:
            continue
        rate_b, rate_t = b[0] / b[1], t[0] / t[1]
        rows.append((style, rate_b, rate_t, rate_t - rate_b, t[1]))
        if style not in changed:
            drifts.append(rate_t - rate_b)
    drift = sum(drifts) / len(drifts) if drifts else 0.0
    return rows, drift


def parse_args() -> argparse.Namespace:
    """命令行：两轮（或两批轮次）与"改过哪几副"。"""

    parser = argparse.ArgumentParser(description="两轮对比（含未改动牌的漂移对照）")
    parser.add_argument("--base", required=True, help="基线轮次，逗号分隔（如 s8）")
    parser.add_argument("--treat", required=True, help="处理轮次，逗号分隔（如 sh9）")
    parser.add_argument("--changed", default="", help="改过的风格名，逗号分隔（这几行会标 ←）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"结果库（默认 {DEFAULT_DB}）")
    return parser.parse_args()


def main() -> None:
    """读两批轮次，打印对照表。"""

    args = parse_args()
    base_arenas = tuple(a.strip() for a in args.base.split(",") if a.strip())
    treat_arenas = tuple(a.strip() for a in args.treat.split(",") if a.strip())
    changed = tuple(c.strip() for c in args.changed.split(",") if c.strip())

    base = load_scores(args.db, base_arenas)
    treat = load_scores(args.db, treat_arenas)
    rows, drift = compare(base, treat, changed)
    print(f"基线：{'、'.join(base_arenas)}｜处理：{'、'.join(treat_arenas)}")
    print(f"{'牌':22s} {'基线':>14s} {'处理':>14s} {'差':>8s}")
    for style, rate_b, rate_t, delta, games in rows:
        mark = " ←" if style in changed else ""
        lo, hi = wilson(int(rate_t * games), games)
        print(f"{style:22s} {rate_b:6.1%}        {rate_t:6.1%}（{lo:.0%}~{hi:.0%}） {delta:+7.1%}{mark}")
    print(f"\n未改动牌的**平均漂移**：{drift:+.1%}"
          f"（{'改动过的牌要' if changed else '要'}拿自己的差减掉它才算净变化）")
    for style, rate_b, rate_t, delta, _ in rows:
        if style in changed:
            print(f"  {style}: 差 {delta:+.1%} − 漂移 {drift:+.1%} = **净 {delta - drift:+.1%}**")


if __name__ == "__main__":
    main()
