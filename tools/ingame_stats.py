"""局内数据看板：每副牌"一局平均"打出了多少东西。

用户关心的那几项，各自最可靠的数据源不同：

| 指标 | 来源 | 为什么 |
|---|---|---|
| 动作数 / **特殊召唤** / **效果发动** | `arena.db` | 由报文流解析器逐条统计（``duel/recorder.py`` 的 sp_summons/effects），比 grep 日志准 |
| **手坑发动** | verbose 日志 | 只有日志里带"从手牌发动"的卡名 |
| **直接攻击** | verbose 日志 | 日志里有 ``(<卡名> direct attack!!)``，按卡名归到所属卡组 |
| **额外卡组出场 / 终端出场** | verbose 日志 | ``(0 's <卡> from Extra move to MonsterZone)`` |

用法::

    python tools/ingame_stats.py --db temp/train/arena.db --logs "temp/train/rr3-*.log" \\
        --deck 88:RaiseMoon:77ebc397f862 --deck 93:WitchcraftShop:ca2f4b1d6cf6 ...

日志文件名约定 ``<前缀>-<左方 id>-<右方 id>.log``（擂台工具就是这么命名的），据此把两侧归到卡组。
"""

from __future__ import annotations

import argparse
import glob
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

# 手坑名单（日志里只有卡名，所以按名字匹配；别名/异画都算同一张）
HAND_TRAPS = (
    "灰流丽", "增殖的G", "效果遮蒙者", "幽鬼兔", "屋敷童", "小丑与锁鸟",
    "朔夜时雨", "欢聚友伴", "无限泡影", "灵王的波动", "次元吸引者",
    "PSY骨架装备",
)

_LABEL = re.compile(r"INFO \[([^\]]+)\] \[\d\d-\d\d-\d\d \d\d:\d\d:\d\d\] (.*)$")
_FNAME = re.compile(r"-(?P<left>\d+)-(?P<right>\d+)\.log$")
_SINGLE = re.compile(r"(?<!\d)(?P<deck>\d+)(?!\d)")


def read_ydk(path: Path) -> Set[int]:
    ids: Set[int] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "!")) and line.isdigit():
            ids.add(int(line))
    return ids


def read_names(cdb: Optional[Path], ids: Sequence[int]) -> Dict[int, str]:
    if cdb is None or not cdb.is_file():
        return {}
    con = sqlite3.connect(str(cdb))
    try:
        cur = con.cursor()
        out: Dict[int, str] = {}
        for card_id in set(ids):
            cur.execute("SELECT name FROM texts WHERE id=?", (card_id,))
            row = cur.fetchone()
            if row and row[0]:
                out[card_id] = row[0]
        return out
    finally:
        con.close()


def scan_log(path: Path, left_id: str, right_id: str) -> Dict[str, Dict[str, object]]:
    """按阵营统计一份日志里的：手坑发动、直接攻击、额外出场、终端出场。"""

    stats: Dict[str, Dict[str, object]] = {
        left_id: {"hand_trap": 0, "direct": 0, "extra": 0, "extra_detail": Counter()},
        right_id: {"hand_trap": 0, "direct": 0, "extra": 0, "extra_detail": Counter()},
    }
    games_seen: Dict[str, int] = {left_id: 0, right_id: 0}
    game_open = False

    def deck_of_card(name: str) -> Optional[str]:
        """直接攻击行没有阵营，只能按"这张卡属于哪副牌"归属（镜像局会歧义，跳过）。"""

        in_left = any(name in n for n in names_by_deck.get(left_id, ()))
        in_right = any(name in n for n in names_by_deck.get(right_id, ()))
        if in_left and not in_right:
            return left_id
        if in_right and not in_left:
            return right_id
        return None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "WindBot starting..." in raw:
            if game_open:
                games_seen[left_id] += 1
                games_seen[right_id] += 1
            game_open = True
            continue
        m = _LABEL.match(raw.rstrip())
        if not m:
            continue
        label, body = m.group(1).strip(), m.group(2).strip()
        if label == "WindBot":
            continue
        deck = left_id if label == "我方" else right_id
        if "direct attack" in body:
            name = body.strip("() ").replace(" direct attack!!", "").strip()
            owner = deck_of_card(name)
            if owner:
                stats[owner]["direct"] = int(stats[owner]["direct"]) + 1
            continue
        ma = re.match(r"\(0 's (.+?)\)$", body)
        if not ma:
            continue
        action = ma.group(1)
        card = action.split(" activate effect")[0].split(" from ")[0].strip()
        if "activate effect" in action and any(ht in card for ht in HAND_TRAPS):
            stats[deck]["hand_trap"] = int(stats[deck]["hand_trap"]) + 1
        if "from Extra move to MonsterZone" in action:
            stats[deck]["extra"] = int(stats[deck]["extra"]) + 1
            stats[deck]["extra_detail"][card] += 1      # type: ignore[index]
    if game_open:
        games_seen[left_id] += 1
        games_seen[right_id] += 1
    for deck, count in games_seen.items():
        stats[deck]["games"] = count
    return stats


names_by_deck: Dict[str, List[str]] = {}


def main() -> int:
    parser = argparse.ArgumentParser(description="局内数据看板（每副牌一局平均）")
    parser.add_argument("--db", type=Path, required=True, help="擂台结果库 arena.db")
    parser.add_argument("--logs", required=True, help="日志 glob，如 temp/train/rr3-*.log")
    parser.add_argument("--deck", action="append", default=[], required=True,
                        help="<id>:<style>:<ydk 文件名>，可重复")
    parser.add_argument("--deck-dir", type=Path, required=True, help="ydk 所在目录")
    parser.add_argument("--cards-cdb", type=Path, default=None)
    parser.add_argument("--since", type=float, default=0.0, help="只统计这个时间之后的库记录")
    parser.add_argument("--until", type=float, default=float("inf"), help="只统计这个时间之前的库记录")
    args = parser.parse_args()

    decks: Dict[str, Dict[str, str]] = {}
    cards_by_deck: Dict[str, Set[int]] = {}
    names_map: Dict[int, str] = {}
    for item in args.deck:
        deck_id, style, fname = item.split(":", 2)
        decks[deck_id] = {"style": style, "ydk": str(args.deck_dir / fname)}
        ids = read_ydk(args.deck_dir / fname)
        cards_by_deck[deck_id] = ids
        names_map.update(read_names(args.cards_cdb, sorted(ids)))
    for deck_id, info in decks.items():
        names_by_deck[deck_id] = [names_map.get(i, "") for i in cards_by_deck[deck_id]]

    # ---- 库里的硬指标 ----
    styles = {info["style"] for info in decks.values()}
    con = sqlite3.connect(str(args.db))
    agg: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT left_style, right_style, winner, turns, duration_seconds,"
            " sp_summons_left, sp_summons_right, effects_left, effects_right,"
            " actions_left, actions_right FROM duels WHERE created_at >= ? AND created_at <= ?",
            (args.since, args.until),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    for left_style, right_style, winner, turns, duration, sp_l, sp_r, ef_l, ef_r, ac_l, ac_r in rows:
        for style, is_left in ((left_style, True), (right_style, False)):
            if style not in styles:
                continue
            a = agg[style]
            a["games"] += 1
            a["turns"] += turns or 0
            a["duration"] += duration or 0
            a["sp"] += (sp_l if is_left else sp_r) or 0
            a["effects"] += (ef_l if is_left else ef_r) or 0
            a["actions"] += (ac_l if is_left else ac_r) or 0
            if (is_left and winner == "我方") or ((not is_left) and winner and winner.startswith("对手")):
                a["wins"] += 1

    # ---- 日志里的指标 ----
    log_agg: Dict[str, Dict[str, object]] = defaultdict(lambda: {"hand_trap": 0, "direct": 0, "extra": 0, "detail": Counter()})
    for path in sorted(glob.glob(args.logs)):
        m = _FNAME.search(path)
        if m:
            left_id, right_id = m.group("left"), m.group("right")
        else:
            # 镜像自战日志常见命名 v6-95.log（只有一个卡组号）：两侧都是这副牌。
            # ⚠ 不能直接找"第一串数字"——"v9-88" 里的 9 也会被匹配到；只在**已知卡组号**里找。
            stem = Path(path).stem
            matched = [deck_id for deck_id in decks if re.search(rf"(?<!\d){deck_id}(?!\d)", stem)]
            if not matched:
                continue
            left_id = right_id = matched[0]
        if left_id not in decks or right_id not in decks:
            continue
        per_side = scan_log(Path(path), left_id, right_id)
        for deck_id, s in per_side.items():
            log_agg[deck_id]["hand_trap"] = int(log_agg[deck_id]["hand_trap"]) + int(s["hand_trap"])
            log_agg[deck_id]["direct"] = int(log_agg[deck_id]["direct"]) + int(s["direct"])
            log_agg[deck_id]["extra"] = int(log_agg[deck_id]["extra"]) + int(s["extra"])
            log_agg[deck_id]["detail"].update(s["extra_detail"])       # type: ignore[arg-type]

    print("按卡组汇总（每副牌参与的对局合计）")
    print(f"{'卡组':<16}{'局数':>5}{'胜率':>8}{'回合':>7}{'秒':>7}{'动作':>8}{'特召':>8}{'效果':>8}{'手坑':>8}{'直击':>8}{'额外出场':>9}")
    for deck_id, info in decks.items():
        style = info["style"]
        a = agg.get(style)
        if not a or not a["games"]:
            continue
        n = a["games"]
        lg = log_agg.get(deck_id) or {"hand_trap": 0, "direct": 0, "extra": 0}
        print(
            f"{style:<16}{int(n):>5}{a['wins'] / n:>7.0%}{a['turns'] / n:>7.1f}"
            f"{a['duration'] / n:>7.0f}{a['actions'] / n:>8.1f}{a['sp'] / n:>8.1f}"
            f"{a['effects'] / n:>8.1f}{int(lg['hand_trap']) / n:>8.1f}"
            f"{int(lg['direct']) / n:>8.1f}{int(lg['extra']) / n:>9.1f}"
        )
    print("\n各卡组最常出场的额外怪（前 5）：")
    for deck_id, info in decks.items():
        detail = (log_agg.get(deck_id) or {}).get("detail") or Counter()
        top = "、".join(f"{name}×{cnt}" for name, cnt in detail.most_common(5))
        print(f"  {info['style']}: {top if top else '(无)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
