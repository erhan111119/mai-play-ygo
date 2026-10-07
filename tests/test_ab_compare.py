"""``tools/ab_compare.py`` 的回归测试：**"未改动牌的漂移"要算对**。

A/B 的结论全靠这个口径：改过的牌要拿自己的差减掉"没改动的牌在这两轮里的平均漂移"，
所以三件事必须钉死——漂移只统计未改动的牌、改过的行要能标出来、区间不能在小样本上乱给。
直接用 ``python tests/test_ab_compare.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import sqlite3
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_tool(stem: str):
    """按文件路径加载 ``tools/`` 下的脚本（它们不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(f"tools_{stem}", _PLUGIN_ROOT / "tools" / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AB = load_tool("ab_compare")


def test_drift_only_counts_untouched_decks() -> None:
    """漂移＝未改动牌的平均差；改动过的牌不参与漂移，单独报净变化。"""

    base = {"A": (50, 100), "B": (50, 100), "Touched": (50, 100)}
    treat = {"A": (60, 100), "B": (60, 100), "Touched": (70, 100)}
    rows, drift = AB.compare(base, treat, ("Touched",))
    assert abs(drift - 0.10) < 1e-9, drift          # A、B 都 +10 点
    touched = [r for r in rows if r[0] == "Touched"][0]
    assert abs(touched[3] - 0.20) < 1e-9, touched    # 改过的牌自己 +20 点
    # 净变化 = 自己的差 − 漂移
    assert abs((touched[3] - drift) - 0.10) < 1e-9


def test_missing_rounds_are_skipped() -> None:
    """两轮里缺一边的牌不出一行（否则会拿 0 局当 0% 算）。"""

    rows, drift = AB.compare({"A": (10, 20), "B": (5, 10)}, {"A": (8, 20)}, ("A",))
    styles = [r[0] for r in rows]
    assert styles == ["A"], styles
    assert drift == 0.0, drift          # 没有"未改动的牌"可比时漂移按 0 处理


def test_wilson_interval_behaves() -> None:
    """区间：小样本宽、过半居中、全胜时上界仍是 100%。"""

    lo, hi = AB.wilson(1, 10)
    assert 0.0 <= lo < 0.1 and 0.3 < hi < 0.5, (lo, hi)
    lo, hi = AB.wilson(50, 100)
    assert 0.40 < lo < 0.50 < hi < 0.60, (lo, hi)
    lo, hi = AB.wilson(10, 10)
    assert hi == 1.0 and lo > 0.6, (lo, hi)
    assert AB.wilson(0, 0) == (0.0, 0.0)


def test_scores_fall_back_to_name_for_test_style() -> None:
    """通用执行器 `Test` 的牌按**卡组名**汇总（与 round_report 的口径一致）。"""

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "r.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE duels (arena TEXT, left_style TEXT, right_style TEXT, "
            "left_name TEXT, right_name TEXT, winner TEXT)"
        )
        connection.executemany(
            "INSERT INTO duels VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("s1", "Test", "RaiseMoon", "空白墙", "对手(RaiseMoon)", "对手(RaiseMoon)"),
                ("s1", "Test", "RaiseMoon", "空白墙", "对手(RaiseMoon)", "空白墙"),
            ],
        )
        connection.commit()
        connection.close()

        scores = AB.load_scores(db, ("s1",))
        assert scores["空白墙"] == (1, 2), scores
        assert scores["RaiseMoon"] == (1, 2), scores


def _run_all() -> int:
    """不装 pytest 时的自跑入口（与其它测试文件一致）。"""

    tests: List = [
        test_drift_only_counts_untouched_decks,
        test_missing_rounds_are_skipped,
        test_wilson_interval_behaves,
        test_scores_fall_back_to_name_for_test_style,
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
