"""``tools/card_vitality.py`` 的回归测试：**读卡表、按左右取对那一路台账、认得出死牌**。

这个工具的结论（"十副牌零死牌"）是牌表级决策的依据，所以三个容易错的地方都要钉住：

1. `--ydk` 只能算**主卡**（`#extra`/`!side` 里的卡不进死牌表——它们本来就不该被"用"）；
2. 台账的**左右**不能读错：`card_usage` 是左方（被评估那副）的用卡、`card_usage_opponent` 是右方的；
   读反了会把对手的用卡记成我们的（2026-10-07 之前就是混记的，s1~s3 的轮次因此不可读）；
3. 死牌判据是"整局 0 事件"，而**盖放也算事件**（否则"只盖陷阱"的牌会被误报成死牌）。

直接用 ``python tests/test_card_vitality.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_tool(stem: str):
    """按文件路径加载 ``tools/`` 下的脚本（它们不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(f"tools_{stem}", _PLUGIN_ROOT / "tools" / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VITALITY = load_tool("card_vitality")


def _write_ydk(path: Path, main: List[int], extra: List[int]) -> Path:
    """写一份最小卡表：主卡 + 额外（额外必须被忽略）。"""

    body = ["#main"] + [str(card) for card in main]
    body += ["#extra"] + [str(card) for card in extra]
    body += ["!side", "1"]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _write_db(path: Path, rows: List[dict]) -> Path:
    """写一份最小结果库：只建 `duels` 表里这个工具要读的六列。"""

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE duels (arena TEXT, left_style TEXT, right_style TEXT, "
        "card_usage TEXT, card_usage_opponent TEXT, card_seen TEXT, card_seen_opponent TEXT)"
    )
    for row in rows:
        connection.execute(
            "INSERT INTO duels VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("arena", "s4"),
                row["left_style"],
                row["right_style"],
                json.dumps(row.get("card_usage", {})),
                json.dumps(row.get("card_usage_opponent", {})),
                json.dumps(row.get("card_seen", {})),
                json.dumps(row.get("card_seen_opponent", {})),
            ),
        )
    connection.commit()
    connection.close()
    return path


def test_main_deck_ignores_extra_and_side() -> None:
    """卡表解析只取 `#main` 段。"""

    with tempfile.TemporaryDirectory() as tmp:
        ydk = _write_ydk(Path(tmp) / "d.ydk", [1000, 1000, 2000], [999999])
        assert VITALITY.load_deck(ydk) == [1000, 1000, 2000]


def test_usage_reads_the_right_column_per_side() -> None:
    """我方在右时读 `card_usage_opponent`（`card_seen` 同理）；分母只算"被观测到的局"。"""

    with tempfile.TemporaryDirectory() as tmp:
        db = _write_db(
            Path(tmp) / "r.db",
            [
                # 一局：我们在左（card_usage / card_seen 是我们的）
                {"left_style": "Mine", "right_style": "Other",
                 "card_usage": {"1000": 3}, "card_usage_opponent": {"7000": 9},
                 "card_seen": {"1000": 1}, "card_seen_opponent": {"7000": 1}},
                # 一局：我们在右（带 _opponent 的那两列才是我们的）
                {"left_style": "Other", "right_style": "Mine",
                 "card_usage": {"7000": 9}, "card_usage_opponent": {"1000": 2},
                 "card_seen": {"7000": 1}, "card_seen_opponent": {"1000": 1}},
                # 一局：我们坐了对面的位置 ⇒ 手牌是隐藏信息，card_seen 是空的（**不算进到手率的分母**）
                {"left_style": "Other", "right_style": "Mine",
                 "card_usage": {"7000": 1}, "card_usage_opponent": {"1000": 1},
                 "card_seen": {}, "card_seen_opponent": {}},
                # 一局：别的牌桌（一行都不该算进来）
                {"left_style": "Other", "right_style": "Third", "card_usage": {"1000": 99}},
            ],
        )
        events, used, seen, seen_used, games, games_observed = VITALITY.load_usage(
            db, "Mine", ("s4",)
        )
        assert games == 3, games
        assert games_observed == 2, games_observed
        # 9000 张的对手用卡（7000）绝不能进我们的账
        assert events == {1000: 6}, events
        assert used == {1000: 3}, used
        # 那张没被观测到的局里的用卡照样计入使用率，但不进到手率/转化率的分母
        assert seen == {1000: 2}, seen
        # 两局里"又到手又用上"的都是 1000（第 3 局没手牌数据，不进这里）
        assert seen_used == {1000: 2}, seen_used


def test_arena_filter_excludes_other_rounds() -> None:
    """`--arenas` 之外的行不参与统计（s1~s3 是坏口径，默认不读）。"""

    with tempfile.TemporaryDirectory() as tmp:
        db = _write_db(
            Path(tmp) / "r.db",
            [
                {"arena": "s3", "left_style": "Mine", "right_style": "Other", "card_usage": {"1000": 8}},
                {"arena": "s4", "left_style": "Mine", "right_style": "Other", "card_usage": {"1000": 1}},
            ],
        )
        events, _, _, _, games, _ = VITALITY.load_usage(db, "Mine", ("s4",))
        assert (events, games) == ({1000: 1}, 1), (events, games)


def test_dead_card_is_reported_and_sets_count_as_events() -> None:
    """整局 0 事件的卡报成死牌；只被**盖放**过的卡不算死牌。"""

    with tempfile.TemporaryDirectory() as tmp:
        db = _write_db(
            Path(tmp) / "r.db",
            [{"left_style": "Mine", "right_style": "Other", "card_usage": {"2000": 1}}],
        )
        out = io.StringIO()
        with redirect_stdout(out):
            VITALITY.report(
                "Mine",
                [1000, 2000, 2000, 3000],
                {1000: "死牌怪", 2000: "盖放陷阱", 3000: "另一张死牌"},
                {2000: 1},      # 1000/3000 一次都没出现（这里键就是"用过的卡"）
                {2000: 1},
                {},             # 没有到手率数据（老轮次）
                1,
            )
        text = out.getvalue()
        assert "死牌（2 种" in text, text
        assert "死牌怪" in text and "另一张死牌" in text, text
        # 被用过的那张（哪怕只有 1 次事件）不进死牌名单
        assert "2x  盖放陷阱" not in text.split("①")[1].split("②")[0], text
        # 没有 card_seen 数据时要说清，而不是假装转化率是 0
        assert "没有 card_seen 数据" in text, text


def test_conversion_needs_both_arrival_and_use() -> None:
    """转化率＝"又到手又用上"的局数 ÷ 到手局数：到手却没用上的排最前，两者都有的才不会超 100%。"""

    with tempfile.TemporaryDirectory() as tmp:
        out = io.StringIO()
        with redirect_stdout(out):
            VITALITY.report(
                "Mine",
                [1000, 2000, 3000, 4000, 5000],
                {1000: "上手就用", 2000: "到手常闲着", 3000: "到手从没用上",
                 4000: "从没到手", 5000: "用过但从没到手"},
                {1000: 10, 2000: 2, 5000: 4},       # 事件数
                {1000: 10, 2000: 2, 5000: 3},       # 用过它的局数（5000 是被从卡组/墓地拉出来的）
                {1000: 10, 2000: 10, 3000: 5},      # 到手局数（4000/5000 一次都没到手）
                10,
                {1000: 10, 2000: 2},                # **又到手又用上**的局数
                10,
            )
        text = out.getvalue()
        section = text.split("② 转化率最低的")[1].split("③")[0]
        # 0% < 20% < 100%：到手却一次没用上的排最前，100% 的排最后
        assert section.index("到手从没用上") < section.index("到手常闲着"), section
        assert section.index("到手常闲着") < section.index("上手就用"), section
        assert "转化  20%" in section, section
        # 分子只认"又到手又用上"：5000 用过 3 局但从没到手，不能进这张表
        assert "用过但从没到手" not in section, section
        # 从没到手（分母 0）的卡也不进这张表
        assert "从没到手" not in section, section


def _run_all() -> int:
    """不装 pytest 时的自跑入口（与其它测试文件一致）。"""

    tests: List = [
        test_main_deck_ignores_extra_and_side,
        test_usage_reads_the_right_column_per_side,
        test_arena_filter_excludes_other_rounds,
        test_dead_card_is_reported_and_sets_count_as_events,
        test_conversion_needs_both_arrival_and_use,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✔ {test.__name__}")
        except AssertionError as error:
            failed += 1
            print(f"✘ {test.__name__}: {error}")
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
