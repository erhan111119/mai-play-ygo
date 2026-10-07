"""知识库体检：覆盖率、样例、以及"当前缺什么"。

**为什么要有这个**：知识库是长期生长的东西（新卡包、新系列、新坑都要补），
没有一个统一的体检就只能靠感觉判断"够不够用"。这里把该看的数字一次打出来：
卡牌事实覆盖了多少张、有多少条是低置信度（没有脚本、只能从文本猜）、
线路里有多少条是人工整理的、交互规则覆盖了几张坑、库里最后更新时间。

用法::

    python tools/knowledge_status.py
    python tools/knowledge_status.py --samples 5     # 多打几条样例
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import Knowledge  # noqa: E402  导入顺序受 sys.path 补丁影响


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知识库体检")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--samples", type=int, default=3, help="每类打几条样例")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    from duel.knowledge import default_data_dir

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    knowledge = Knowledge.from_data_dir(data_dir, plugin_root=Path(args.plugin_root))
    if not knowledge.available:
        print(f"[还没有知识库] {knowledge.path}")
        print("先跑：python tools/build_card_facts.py && python tools/build_deck_plans.py")
        return 1

    counts = knowledge.counts()
    print(f"知识库：{knowledge.path}")
    for name, label in (
        ("card_facts", "卡牌事实"),
        ("deck_plans", "线路/系列知识"),
        ("interactions", "交互规则"),
        ("decisions", "决策日志"),
    ):
        print(f"  {label:<14}{counts.get(name, -1)} 条")

    # 覆盖率与置信度：卡牌事实这一层是"全都有"的核心，所以看它的分布
    connection = sqlite3.connect(f"file:{knowledge.path.as_posix()}?mode=ro", uri=True)
    try:
        total = connection.execute("SELECT COUNT(*) FROM card_facts").fetchone()[0]
        scripted = connection.execute(
            "SELECT COUNT(*) FROM card_facts WHERE confidence = 'script'"
        ).fetchone()[0]
        interactions = connection.execute(
            "SELECT COUNT(*) FROM card_facts WHERE is_interaction = 1"
        ).fetchone()[0]
        locks = connection.execute(
            "SELECT COUNT(*) FROM card_facts WHERE self_lock != ''"
        ).fetchone()[0]
        curated_plans = connection.execute(
            "SELECT COUNT(*) FROM deck_plans WHERE source = 'curated'"
        ).fetchone()[0]
        auto_plans = connection.execute(
            "SELECT COUNT(*) FROM deck_plans WHERE source != 'curated'"
        ).fetchone()[0]
        handtraps = connection.execute(
            "SELECT COUNT(DISTINCT handtrap_id) FROM interactions"
        ).fetchone()[0]
        built_at = connection.execute(
            "SELECT value FROM meta WHERE key = 'deck_plans_built_at'"
        ).fetchone()

        print("\n卡牌事实（覆盖率就是「全部卡」这件事的底气）：")
        print(f"  有脚本（高置信）{scripted}/{total}（{scripted / max(1, total):.0%}）")
        print(f"  判为阻抗 {interactions} 张；带自肃 {locks} 张")
        # 阻抗的"能拦什么"是"该不该交坑"这条线的原料：空着等于没资料（实测踩过：
        # 1,161 张阻抗里 1,123 张的能拦是空的，等于这条线没有数据）
        no_hits = connection.execute(
            "SELECT COUNT(*) FROM card_facts WHERE is_interaction = 1 AND hits = ''"
        ).fetchone()[0]
        print(f"  阻抗里「能拦什么」为空：{no_hits} 张（应当接近 0）")
        top_hits: Dict[str, int] = {}
        for (hits,) in connection.execute(
            "SELECT hits FROM card_facts WHERE is_interaction = 1 AND hits != ''"
        ):
            for label in str(hits).split(","):
                if label:
                    top_hits[label] = top_hits.get(label, 0) + 1
        ranked = sorted(top_hits.items(), key=lambda item: -item[1])[:6]
        print("  最常出现的命中标签：" + "、".join(f"{label}×{count}" for label, count in ranked))
        print("\n线路/系列知识：")
        print(f"  人工整理 {curated_plans} 条、自动生成 {auto_plans} 条")
        print(f"  交互规则覆盖 {handtraps} 张坑")
        if built_at:
            print(f"  最近生成：{time.strftime('%Y-%m-%d %H:%M', time.localtime(float(built_at[0])))}")

        print(f"\n样例（前 {args.samples} 条）：")
        for key, title, body in connection.execute(
            "SELECT plan_key, title, body FROM deck_plans ORDER BY source DESC, plan_key LIMIT ?",
            (int(args.samples),),
        ):
            print(f"  [{key}] {title}")
            print(f"      {body[:180]}…")
    finally:
        connection.close()
    knowledge.close()
    print("\n缺什么怎么办：新卡包 → 重跑 build_card_facts；新系列/新坑 → 补 tools/series_notes.py 再跑 build_deck_plans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
