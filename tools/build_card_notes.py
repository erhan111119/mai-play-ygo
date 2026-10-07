"""给**每一张卡**写一行"怎么用 / 该不该拦 / 威胁多大"——**不用模型**，全部从事实推导。

为什么要有这一层（用户的诉求是"每张卡都这样来一遍"）：
让模型逐张写 15,205 张需要二十多个小时，而**大部分信息本来就在库里**——
它能做什么（`card_facts`）、属于哪个系列（`cards.cdb`）、在池子里有没有人带（卡组池）、
是不是通用手坑。这些拼起来就能给出"用法/拦法/威胁度"，**零成本、全覆盖、可复算**。
模型只在"值得精写"的卡上补（见 ``tools/build_card_notes_ai.py``），并且**不覆盖**这一层之上的人工/模型版本。

威胁度怎么定（写在明面上，可复核）：
* **高**：某系列的引擎/起手件（检索＋特召）、或是常用手坑、或是某系列的关键终端；
* **中**：有效果、且进过卡组（能真的出现在对局里）；
* **低**：白板/未被任何卡组使用/只在对局外。

用法::

    python tools/build_card_notes.py            # 全卡推导（几秒）
    python tools/build_card_notes.py --report   # 顺带看看威胁分布
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, default_data_dir, default_knowledge_db, label_of  # noqa: E402

# 手坑/泛用干扰的判定：在所有卡组里出现得够多，且判为阻抗
COMMON_HANDTRAP_DECKS = 3
"""至少出现在这么多副卡组里才算"常用手坑"（池子里只有 83 副牌，3 副是很低的门槛）。"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从事实推导每张卡的用法/拦法/威胁度")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--report", action="store_true", help="打印威胁分布与抽样")
    return parser.parse_args()


def load_cards(connection: sqlite3.Connection) -> Dict[int, Dict[str, object]]:
    """把卡牌事实读成内存表（推导阶段要反复查，读一次最省事）。"""

    cards: Dict[int, Dict[str, object]] = {}
    for row in connection.execute(
        "SELECT card_id, name, kinds, hits, events, self_lock, is_interaction, confidence"
        " FROM card_facts"
    ):
        cards[int(row[0])] = {
            "name": str(row[1] or ""),
            "kinds": [part for part in str(row[2] or "").split(",") if part],
            "hits": [part for part in str(row[3] or "").split(",") if part],
            "events": [part for part in str(row[4] or "").split(",") if part],
            "self_lock": [part for part in str(row[5] or "").split(",") if part],
            "is_interaction": bool(row[6]),
            "confidence": str(row[7] or "text"),
        }
    return cards


def deck_usage(data_dir: Path) -> Tuple[Counter, Dict[int, List[str]]]:
    """池子里每张卡被几副牌带着；以及它出现在哪些牌里（名字给人看）。"""

    counts: Counter = Counter()
    where: Dict[int, List[str]] = defaultdict(list)
    pool = data_dir / "deck_pool.db"
    if not pool.is_file():
        return counts, where
    connection = sqlite3.connect(f"file:{pool.as_posix()}?mode=ro", uri=True)
    try:
        for display_name, ydk_path in connection.execute("SELECT display_name, ydk_path FROM decks"):
            try:
                text = Path(str(ydk_path)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.isdigit():
                    card_id = int(stripped)
                    counts[card_id] += 1
                    if len(where[card_id]) < 2 and str(display_name) not in where[card_id]:
                        where[card_id].append(str(display_name))
    finally:
        connection.close()
    return counts, where


def series_of(cards_cdb: Path) -> Dict[int, int]:
    """每张卡属于哪个系列（低 16 位；没有就 0）。"""

    connection = sqlite3.connect(f"file:{cards_cdb.as_posix()}?mode=ro", uri=True)
    try:
        return {
            int(cid): int(sc or 0) & 0xFFFF
            for cid, sc in connection.execute("SELECT id, setcode FROM datas")
        }
    finally:
        connection.close()


def compose_note(
    card_id: int,
    fact: Dict[str, object],
    *,
    decks: int,
    deck_names: Sequence[str],
    series_name: str,
) -> Tuple[str, str, str]:
    """推出一行 ``(怎么用, 该不该拦, 威胁度)``。"""

    kinds = list(fact["kinds"])  # type: ignore[arg-type]
    hits = list(fact["hits"])  # type: ignore[arg-type]
    locks = list(fact["self_lock"])  # type: ignore[arg-type]
    engine = [kind for kind in ("search", "ss") if kind in kinds]

    # ---- 怎么用
    uses: List[str] = []
    if fact["is_interaction"]:
        if hits:
            uses.append("干扰牌：拦「" + "/".join(label_of(tag) for tag in hits[:3]) + "」")
        else:
            uses.append("干扰牌")
    if engine:
        uses.append("引擎件：" + "/".join(label_of(tag) for tag in engine))
    if "destroy" in kinds or "banish" in kinds:
        uses.append("解场")
    if "draw" in kinds:
        uses.append("续航")
    if not uses:
        uses.append("按效果文本使用" if fact["confidence"] == "script" else "白板/未被使用")
    usage = "；".join(uses[:3])

    # ---- 该不该拦
    if fact["is_interaction"]:
        block = "它是干扰牌，通常留到对手的关键时点再交"
    elif engine and decks:
        block = "**是引擎起手件：该拦**（灰流丽/无效系优先交给它这类）"
    elif engine:
        block = "是引擎件：遇到就拦"
    elif "damage" in kinds or decks >= 3:
        block = "视局面拦（不是引擎，拦它优先级低）"
    else:
        block = "不值得专门拦"
    if locks:
        block += f"；它自带自肃（{'/'.join(locks[:2])}），可以等它自己卡住"

    # ---- 威胁度
    # 威胁度必须**可复核**，所以判据写死在明面上：
    #   高 = 常用干扰牌（多副牌都带）／引擎起手件**且真的进过卡组**
    #   中 = 有效果但池子里没人带（能出现但没人用）／非引擎但进了卡组
    #   低 = 白板或完全没有卡组带
    # 第一版把"属于某个系列 + 会特召"都算高，结果 1/3 的卡都是"高"（4,943 张）——那种等级没有信息量。
    if fact["is_interaction"] and decks >= COMMON_HANDTRAP_DECKS:
        threat = "高（常用干扰牌）"
    elif engine and decks:
        threat = "高（引擎起手件，池子里有牌在带）"
    elif decks:
        threat = "中（进过卡组但非引擎件）"
    elif fact["confidence"] == "script":
        threat = "低（没有卡组在带它）"
    else:
        threat = "低（无卡牌脚本）"
    if deck_names:
        threat += f"｜出现在：{'、'.join(deck_names)}"

    return usage, block, threat


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    import tomllib

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    database = default_knowledge_db(data_dir)
    if not database.is_file():
        print(f"[错误] 还没有知识库：{database}（先跑 tools/build_card_facts.py）")
        return 2
    paths = tomllib.loads((Path(args.plugin_root) / "config.toml").read_text(encoding="utf-8")).get("paths") or {}
    cards_cdb = Path(str(paths.get("cards_cdb") or "")) if paths.get("cards_cdb") else (
        Path(str(paths.get("ygopro_dir") or "")) / "cards.cdb"
    )
    if not cards_cdb.is_file():
        print(f"[错误] 找不到卡片数据库：{cards_cdb}")
        return 2

    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    cards = load_cards(connection)
    usage_count, usage_where = deck_usage(data_dir)
    series = series_of(cards_cdb)

    # 系列名（有人工/模型说明的用它的标题，读起来更像人话）
    series_titles: Dict[int, str] = {}
    for key, title in connection.execute("SELECT plan_key, title FROM deck_plans WHERE kind = 'series'"):
        text = str(key)
        if text.startswith("series:0x"):
            try:
                series_titles[int(text.split("0x", 1)[1], 16)] = str(title)
            except ValueError:
                continue

    rows: List[Tuple[int, str, str, str, str, float]] = []
    for card_id, fact in cards.items():
        code = series.get(card_id, 0)
        usage, block, danger = compose_note(
            card_id,
            fact,
            decks=usage_count.get(card_id, 0),
            deck_names=usage_where.get(card_id, []),
            series_name=series_titles.get(code, f"0x{code:x}" if code else ""),
        )
        rows.append((int(card_id), usage, block, danger, "derived", time.time()))

    # 只重写"推导层"：人工/模型写过的（ai/hand）保留
    connection.execute("DELETE FROM card_notes WHERE source = 'derived'")
    connection.executemany(
        "INSERT OR REPLACE INTO card_notes (card_id, usage, block_advice, threat, source, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()

    if args.report:
        print("威胁度分布：")
        for label, count in connection.execute(
            "SELECT CASE WHEN threat LIKE '高%' THEN '高' WHEN threat LIKE '中%' THEN '中'"
            " WHEN threat LIKE '低%' THEN '低' ELSE '?' END AS level, COUNT(*)"
            " FROM card_notes WHERE source='derived' GROUP BY level ORDER BY COUNT(*) DESC"
        ):
            print(f"  {label}: {count}")
        print("\n抽样（威胁=高的前 6 张）：")
        for card_id, usage, block, threat in connection.execute(
            "SELECT card_id, usage, block_advice, threat FROM card_notes"
            " WHERE source='derived' AND threat LIKE '高%' LIMIT 6"
        ):
            name = str(cards.get(int(card_id), {}).get("name", card_id))
            print(f"  {name}｜{usage}｜该拦：{block}｜威胁：{threat}")
    total = connection.execute("SELECT COUNT(*) FROM card_notes").fetchone()[0]
    connection.close()
    print(f"完成：card_notes 共 {total} 条（本次推导 {len(rows)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
