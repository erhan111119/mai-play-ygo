"""死牌表 / 到手率表：把一副牌的主卡逐张对台账，回答"哪张是死牌""该不该加第几张"。

用法::

    python tools/card_vitality.py --style KezmoYixiangming --ydk <卡表.ydk>
    python tools/card_vitality.py --style RaiseMoon --ydk ... --arenas s8 --limit 0

**为什么要它**：`card_usage` 原本就是为卡组进化搜索准备的（"卡在卡组里但整局从没被用过
⇒ 大概率是死牌"），但一直没有工具去读它。这张表同时回答三件事：

1. **死牌**：整局 0 事件 ⇒ 要么执行器没给它写规则、要么它的条件在这副牌里从没凑齐；
2. **使用率 / 到手率 / 转化率**（2026-10-07 起 `card_seen` 也在库里）：
   到手率＝这局它进过手牌（抽到 + 被检索/回收进手）的局数 / 总局数；
   转化率＝使用率 ÷ 到手率 ⇒ **"到手就能用"还是"到手了也用不上"**。
   这才是"该加第几张"的分母：加第 3 张只提高到手率，若转化率本来就低，加它没用。
3. **事件/局**：被反复召唤/发动的次数。**注意口径**：这是事件不是"抽到次数"，
   同一张在场上来回循环的卡天然事件多（实测卡通「邪魔箱」4.8 事件/局），
   所以"事件/局 高 ⇒ 该加第 3 张"**推不出来**，别拿它当"加卡"依据。

口径与可信轮次（重要）：`card_usage` / `card_seen` 都是**左方（被评估的那副牌）**的，
`*_opponent` 是右方的；这张表按 `left_style` / `right_style` 取对应那一列。
**2026-10-07 05:08 之前录的轮次不能读**：
- s1/s2（03:40~04:34）录于 `duel/recorder.py` 分座位之前 ⇒ 那一列是**双方混记**；
- s3（04:51~05:05）录于 `train/arena.py` 左右映射修好（05:08）之前 ⇒ 左右可能张冠李戴。
- `card_seen` 是**当天更晚**才加的列，早于它录的行里是空表（工具会把到手率显示成 `-`）：
  用 `--arenas s8` 这类新轮次才有到手率。对手那一份基本永远是空的（手牌是隐藏信息）。
默认只读 `s4,s5,s81`（270+450+180 局，每副牌 180 局），要看别的轮次就用 `--arenas` 显式指定。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: 插件根目录（卡库默认用插件自带客户端里那份，换机器不用改）
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# 只读这几个轮次：映射与分座位都修好之后录的（见模块 docstring 的口径说明）
DEFAULT_ARENAS = ("s4", "s5", "s81")
DEFAULT_DB = Path("temp/train/rounds.db")
DEFAULT_CDB = _PLUGIN_ROOT / "clients" / "ygopro" / "cards.cdb"


def load_deck(ydk: Path) -> List[int]:
    """读 `.ydk` 的**主卡**卡号（`#extra` / `!side` 之前的那一段）。"""

    deck: List[int] = []
    in_main = False
    for raw in ydk.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("#main"):
            in_main = True
            continue
        if line.startswith(("#extra", "!side")):
            in_main = False
            continue
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if in_main and line.isdigit():
            deck.append(int(line))
    if not deck:
        raise SystemExit(f"没读到主卡：{ydk}")
    return deck


def load_names(cdb: Path, ids: Sequence[int]) -> Dict[int, str]:
    """从 `cards.cdb` 取卡名；查不到就留空（调用方按卡号显示）。"""

    if not cdb.exists():
        raise SystemExit(f"卡库不存在：{cdb}")
    connection = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True)
    names: Dict[int, str] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ",".join("?" * len(chunk))
        for card_id, name in connection.execute(
            f"SELECT id, name FROM texts WHERE id IN ({marks})", chunk
        ):
            names[int(card_id)] = str(name)
    connection.close()
    return names


def load_usage(
    db_path: Path, style: str, arenas: Sequence[str]
) -> Tuple[Dict[int, int], Dict[int, int], Dict[int, int], Dict[int, int], int, int]:
    """汇总事件数、用过它的局数、**进过手牌的局数**、以及"又到手又用上"的局数。

    ⚠ **两个坑，都会让转化率算出 >100%**（2026-10-07 实测，都踩过）：

    1. **分母不同**：记录器只挂在它对局的那台 bot 上，而擂台镜像会让被评估的那副牌
       **一半的局坐 0 号位**（＝被观测，手牌可见）、另一半坐 1 号位（手牌是隐藏信息）。
       所以"到手率"要除以 ``games_observed``，不能除以 ``games``。
    2. **"用过"不等于"到过手"**：从卡组/墓地/额外卡组直接特召出来的卡会被记进
       `card_usage`，但它从来没进过手牌（实测「卡萝尔」一局被用 7 次、手牌里一次都没有）。
       所以转化率要按**同一局里既到手又用上**（``games_seen_used``）÷ ``games_seen`` 来算，
       不能拿"总使用局数 ÷ 到手局数"。

    返回值：``(事件数, 用过局数, 到手局数, 到手且用过局数, 总局数, 被观测局数)``。
    `card_seen` 是 0.21.110 起才有的列，早于它录的轮次里全是空表（``games_observed`` 为 0）。
    """

    if not db_path.exists():
        raise SystemExit(f"库不存在：{db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(duels)")}
    if "card_seen" not in columns:
        raise SystemExit("这个库里还没有 card_seen 列（是新列）——请用当天新跑的轮次，或重跑一轮")
    marks = ",".join("?" * len(arenas))
    rows = connection.execute(
        "SELECT left_style, right_style, card_usage, card_usage_opponent, card_seen, card_seen_opponent "
        "FROM duels "
        f"WHERE arena IN ({marks}) AND (left_style = ? OR right_style = ?)",
        (*arenas, style, style),
    ).fetchall()
    connection.close()

    events: Dict[int, int] = {}
    games_used: Dict[int, int] = {}
    games_seen: Dict[int, int] = {}
    games_seen_used: Dict[int, int] = {}
    games = 0
    games_observed = 0
    for left_style, right_style, left_usage, right_usage, left_seen, right_seen in rows:
        # 镜子局（左右同名）只算左方那一份，避免同一局记两次
        is_left = left_style == style
        try:
            usage = json.loads((left_usage if is_left else right_usage) or "{}")
            seen = json.loads((left_seen if is_left else right_seen) or "{}")
        except json.JSONDecodeError:
            continue
        games += 1
        if seen:
            games_observed += 1
        for card_id, count in usage.items():
            card_id = int(card_id)
            events[card_id] = events.get(card_id, 0) + int(count)
            games_used[card_id] = games_used.get(card_id, 0) + 1
        for card_id in seen:
            card_id = int(card_id)
            games_seen[card_id] = games_seen.get(card_id, 0) + 1
            if card_id in {int(k) for k in usage}:
                # 这一局它既到手又出过场 ⇒ 才计入"到手就用上了"
                games_seen_used[card_id] = games_seen_used.get(card_id, 0) + 1
    return events, games_used, games_seen, games_seen_used, games, games_observed


def card_label(card_id: int, names: Dict[int, str]) -> str:
    """`12345 卡名`，查不到卡名就只给卡号。"""

    return f"{card_id} {names[card_id]}" if card_id in names else str(card_id)


def report(
    style: str,
    deck: Sequence[int],
    names: Dict[int, str],
    events: Dict[int, int],
    games_used: Dict[int, int],
    games_seen: Dict[int, int],
    games: int,
    games_seen_used: Optional[Dict[int, int]] = None,
    games_observed: Optional[int] = None,
    limit: int = 12,
) -> None:
    """打印死牌、到手率与转化率（转化率＝"又到手又用上"的局数 ÷ 到手局数）。"""

    copies: Dict[int, int] = {}
    for card_id in deck:
        copies[card_id] = copies.get(card_id, 0) + 1

    # 到手率的分母是"被记录器观测到的局"，回退成总局数只为让单测不用构造那一列
    base = games_observed or games
    seen_used = games_seen_used or {}
    print(f"=== {style}：{games} 局，主卡 {len(deck)} 张（{len(copies)} 种）===")
    if games_observed is not None and games_observed != games:
        print(f"（其中 {games_observed} 局能读到我们的手牌，②表按这 {games_observed} 局算）")
    if not games:
        print("这个风格名在这几个轮次里一局都没有，检查 --style / --arenas")
        return

    # ① 死牌：台账里一次都没出现（这张卡在这几个轮次里从没被召唤/发动/盖放过）
    dead = [card_id for card_id in sorted(copies) if not events.get(card_id)]
    print(f"\n① 死牌（{len(dead)} 种，整局 0 事件）：")
    if not dead:
        print("   无——主卡每一张都在对局里被用过")
    for card_id in dead:
        print(f"   {copies[card_id]}x  {card_label(card_id, names)}")

    # ② 转化率最低的：**到手了却很少用得上** ⇒ 要么它是局面件（正常），要么脚本不会用（问题）。
    #    ⚠ 分子必须是"又到手又用上"的局数：从卡组/墓地直接特召的卡"用过但从没到手"，
    #    拿总使用局数当分子会算出 >100%（见 load_usage 的说明）。
    converted = [
        (seen_used.get(card_id, 0) / games_seen[card_id], card_id)
        for card_id in copies
        if games_seen.get(card_id)
    ]
    converted.sort()
    shown_conv = converted if limit <= 0 else converted[:limit]
    if converted:
        print(f"\n② 转化率最低的 {len(shown_conv)}/{len(converted)} 种（到手→用上，同分母）：")
        for rate, card_id in shown_conv:
            arrived = games_seen[card_id]
            both = seen_used.get(card_id, 0)
            print(
                f"   {copies[card_id]}x  到手 {arrived / base:5.1%} → 其中用上 {both / base:5.1%}"
                f"（转化 {rate:4.0%}） 事件 {events.get(card_id, 0)}  {card_label(card_id, names)}"
            )
    else:
        print("\n② 转化率：这批轮次里没有 card_seen 数据（这列是 0.21.110 起才有的）")

    # ③ 使用率最低的：带得多但很少用得上的，优先怀疑脚本不会用
    ranked = sorted(
        (card_id for card_id in copies if events.get(card_id)),
        key=lambda card_id: (games_used.get(card_id, 0) / games, events.get(card_id, 0)),
    )
    shown = ranked if limit <= 0 else ranked[:limit]
    print(f"\n③ 使用率最低的 {len(shown)}/{len(ranked)} 种（用上它的局数 / 总局数）：")
    for card_id in shown:
        used = games_used.get(card_id, 0)
        print(
            f"   {copies[card_id]}x  {used / games:5.1%} ({used}/{games}) "
            f"事件 {events[card_id]}（{events[card_id] / games:.1f}/局）  {card_label(card_id, names)}"
        )


def parse_args() -> argparse.Namespace:
    """命令行：风格名 + 卡表必填，库/轮次/卡库可覆盖。"""

    parser = argparse.ArgumentParser(description="死牌表 / 到手率表：主卡逐张对台账")
    parser.add_argument("--style", required=True, help="台账里的风格名（left_style/right_style）")
    parser.add_argument("--ydk", required=True, type=Path, help="这副牌的 .ydk")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"结果库（默认 {DEFAULT_DB}）")
    parser.add_argument("--cdb", type=Path, default=DEFAULT_CDB, help="cards.cdb 路径")
    parser.add_argument(
        "--arenas",
        default=",".join(DEFAULT_ARENAS),
        help=f"逗号分隔的轮次（默认 {','.join(DEFAULT_ARENAS)}；到手率要 card_seen 之后录的轮次）",
    )
    parser.add_argument("--limit", type=int, default=12, help="②③ 表各打前几名；0 = 全打")
    return parser.parse_args()


def main() -> None:
    """读卡表与台账，打印这张表。"""

    args = parse_args()
    arenas = tuple(a.strip() for a in args.arenas.split(",") if a.strip())
    deck = load_deck(args.ydk)
    names = load_names(args.cdb, sorted(set(deck)))
    events, games_used, games_seen, games_seen_used, games, games_observed = load_usage(
        args.db, args.style, arenas
    )
    print(f"轮次：{'、'.join(arenas)}")
    report(
        args.style, deck, names, events, games_used, games_seen, games,
        games_seen_used, games_observed, args.limit,
    )


if __name__ == "__main__":
    main()
