"""复盘：拿决策日志反查"知识库 / AI 提示词的哪里不对"。

为什么要有这个：知识库和提示词改到现在，**判断哪一条知识有用、哪一条在害人**不能靠感觉。
每次问答都落进了 ``decisions`` 表（问题类型、检索命中、答复、耗时），这份报告把它变成可读的结论：

* **过度否决**：`activate` 里答 "no" 的比例过高（说明知识/提示词在让它"省牌"）；
* **过度放手**：`idle_action` 里答 "0"（不插手）的比例过高（等于把主导权交回脚本）；
* **太慢**：平均答复耗时（思考型模型一次十几秒，会直接吃掉出牌次数）；
* **检索命中**：各类知识被取用的次数（没人用的知识等于没写）。

用法::

    python tools/review_decisions.py                       # 全部
    python tools/review_decisions.py --deck-id 89          # 只看某副牌
    python tools/review_decisions.py --arena room          # 只看某个来源（如房间里打的那些局）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import default_data_dir, default_knowledge_db, result_of_outcome  # noqa: E402

# 触发提醒的阈值（超过就值得看一眼；不是硬性错误）
NO_RATE_WARN = 0.6
"""``activate`` 里 "no"（否决）超过这个比例 → 提示"可能在过度省牌"。"""
IDLE_ZERO_WARN = 0.4
"""``idle_action`` 里 "0"（不插手）超过这个比例 → 提示"主导权交回脚本了"。"""
SLOW_MS_WARN = 8000
"""平均答复耗时超过这个值 → 提示"一回合问不了几次"。"""


def fetch_by_duel(connection: sqlite3.Connection) -> List[Tuple]:
    """按局聚合（新在前）：``(局标识, 来源, 卡组, 结果, 问次数, activate 次数, 否决次数)``。

    ``duel_key`` 为空的是 0.20.0 之前的老数据（那时日志里还没有"这一局"的概念），跳过。
    老库可能连这一列都还没有（补列发生在写入侧）——那就当没有可分的局。
    """

    columns = {row[1] for row in connection.execute("PRAGMA table_info(decisions)")}
    if "duel_key" not in columns:
        return []
    return connection.execute(
        "SELECT duel_key, MAX(arena), MAX(deck_key), MAX(outcome), COUNT(*),"
        " SUM(CASE WHEN kind = 'activate' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN kind = 'activate' AND answer = 'no' THEN 1 ELSE 0 END)"
        " FROM decisions WHERE duel_key <> ''"
        " GROUP BY duel_key ORDER BY MAX(created_at) DESC"
    ).fetchall()


def filter_duels(duels: List[Tuple], deck_id: int, arena: str) -> List[Tuple]:
    """按与决策行同一套条件筛局（过滤在 Python 里做，SQL 里只有写死的语句）。"""

    if deck_id:
        duels = [row for row in duels if str(row[2] or "") == str(int(deck_id))]
    if arena:
        duels = [row for row in duels if arena in str(row[1] or "")]
    return duels


def report_outcomes(duels: List[Tuple]) -> None:
    """按胜负分组看否决率：**输了的那几局，它是不是否决得更多**。

    这是相关关系、不是因果：输的局往往局面本来就差，"拦下来"也可能是对的判断。
    但两组差得离谱时，它至少告诉我们要去看那几局的提示词与场面。
    """

    if not duels:
        print(
            "\n否决与胜负：还没有可分的局——0.20.0 起每局打完会把胜负回填进决策日志"
            "（房间与擂台都会写），补上之后这里才有数据。"
        )
        return
    known = [row for row in duels if result_of_outcome(str(row[3] or ""))]
    print(f"\n否决与胜负（{len(known)}/{len(duels)} 局有结果）——相关关系，不是因果：")
    if not known:
        print("  有决策的局都还没有结果（多是中途取消或还没打完）。")
        return
    groups: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
    for row in known:
        activations = int(row[5] or 0)
        vetos = int(row[6] or 0)
        rate = vetos / activations if activations else 0.0
        groups[result_of_outcome(str(row[3] or ""))].append((rate, int(row[4] or 0)))
    label_of_result = {"win": "赢的局", "loss": "输的局", "draw": "平局", "unknown": "没打完"}
    for result in sorted(groups, key=lambda name: -len(groups[name])):
        items = groups[result]
        rate = sum(item[0] for item in items) / len(items)
        asks = sum(item[1] for item in items) / len(items)
        print(
            f"  {label_of_result.get(result, result):<6} {len(items):>3} 局｜"
            f"平均否决率 {rate:>5.0%}｜平均每局问 {asks:.0f} 次"
        )
    wins = groups.get("win", [])
    losses = groups.get("loss", [])
    if wins and losses:
        gap = sum(item[0] for item in losses) / len(losses) - sum(item[0] for item in wins) / len(wins)
        # 方向词：正 = 输的局否决得更多（"更爱拦车"），负 = 输的局反而更放手
        direction = "更爱拦车" if gap > 0 else "更放手"
        print(f"  两组差 {abs(gap):.0%}：输的局{direction}（{len(losses)} 局对 {len(wins)} 局，样本小别当结论）")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复盘决策日志：AI 哪类判断有问题、哪些知识没人用")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--deck-id", type=int, default=0, help="只看这副牌")
    parser.add_argument("--arena", default="", help="只看这个来源（子串匹配，例如 room / WindBot）")
    parser.add_argument("--limit", type=int, default=5, help="每种问题列几条样例")
    return parser.parse_args()


def fetch_all(connection: sqlite3.Connection) -> List[Tuple]:
    """全部决策（SQL 写死在这里，不带任何外部片段）。"""

    return connection.execute(
        "SELECT created_at, arena, deck_key, kind, card_id, answer, cost_ms, retrieved_json"
        " FROM decisions ORDER BY id"
    ).fetchall()


def fetch_by_deck(connection: sqlite3.Connection, deck_key: str) -> List[Tuple]:
    """某副牌的决策（卡组编号走占位符绑定）。"""

    return connection.execute(
        "SELECT created_at, arena, deck_key, kind, card_id, answer, cost_ms, retrieved_json"
        " FROM decisions WHERE deck_key = ? ORDER BY id",
        (deck_key,),
    ).fetchall()


def report_deck(deck_key: str, items: List[Tuple], limit: int) -> None:
    """一副牌的决策画像 + 可疑模式提醒。"""

    counts = Counter(row[3] for row in items)
    latency = sum(int(row[6] or 0) for row in items) / max(1, len(items))
    print(f"\n===== 卡组 {deck_key}（{len(items)} 次决策，平均答复 {latency / 1000:.1f} 秒）=====")
    for kind, total in counts.most_common():
        answers = Counter(str(row[5]) for row in items if row[3] == kind)
        rendered = "、".join(f"{answer or '（空）'}×{count}" for answer, count in answers.most_common(5))
        print(f"  {kind:<14} {total:>4} 次 → {rendered}")
        if kind == "activate":
            no_rate = answers.get("no", 0) / max(1, total)
            if no_rate >= NO_RATE_WARN:
                print(f"        ⚠️ 否决率 {no_rate:.0%}：可能在过度「省牌」（知识/提示词让它不敢发动）")
        if kind == "idle_action":
            zero_rate = answers.get("0", 0) / max(1, total)
            if zero_rate >= IDLE_ZERO_WARN:
                print(f"        ⚠️ 不插手 {zero_rate:.0%}：主导权交回脚本了，值得看提示词怎么写的")
    if latency >= SLOW_MS_WARN:
        print(f"        ⚠️ 平均答复 {latency / 1000:.1f} 秒：一回合问不了几次，出牌次数会被时间吃掉")

    slowest = sorted(items, key=lambda row: -int(row[6] or 0))[:limit]
    print("  最慢的几次：" + "；".join(
        f"{row[3]}/card={row[4]} {int(row[6] or 0)}ms" for row in slowest
    ))


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    database = default_knowledge_db(data_dir)
    if not database.is_file():
        print(f"还没有知识库：{database}")
        return 1
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        if args.deck_id:
            rows = fetch_by_deck(connection, str(int(args.deck_id)))
        else:
            rows = fetch_all(connection)
        duels = fetch_by_duel(connection)
    finally:
        connection.close()
    if args.arena:
        # 来源过滤放在 Python 里做：SQL 里就不会出现任何由外部输入构造的片段
        rows = [row for row in rows if args.arena in str(row[1] or "")]
    if not rows:
        print("没有可复盘的决策日志——先打几局（房间与擂台都会写）")
        return 1

    by_deck: Dict[str, List[Tuple]] = defaultdict(list)
    for row in rows:
        by_deck[str(row[2] or "?")].append(row)

    print(
        f"决策日志 {len(rows)} 条，涉及 {len(by_deck)} 副牌"
        f"（{time.strftime('%m-%d %H:%M', time.localtime(rows[0][0]))} ~ "
        f"{time.strftime('%m-%d %H:%M', time.localtime(rows[-1][0]))}）"
    )
    print("来源分布：" + "、".join(
        f"{name}×{count}" for name, count in Counter(row[1] for row in rows).most_common(5)
    ))

    hits = Counter()
    for row in rows:
        for chunk in str(row[7] or "").split("｜"):
            if chunk.startswith("【"):
                hits[chunk.split("】", 1)[0] + "】"] += 1
    if hits:
        print("\n检索命中（各类知识被取用的次数）——没人用的知识等于没写：")
        for label, count in hits.most_common(8):
            print(f"  {count:>6}  {label}")

    for deck_key, items in sorted(by_deck.items(), key=lambda kv: -len(kv[1]))[:8]:
        report_deck(deck_key, items, args.limit)

    # 按局看：这一局的干预与这一局的胜负有没有关系（0.20.0 起日志里才有结果）
    report_outcomes(filter_duels(duels, args.deck_id, args.arena))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
