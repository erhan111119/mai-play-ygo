"""``tools/silent_game_audit.py`` 的回归测试：**切段、抓手牌、判归属、拒并发日志**。

这个工具的价值全在"报出来的那一局和它的起手是不是对得上"——切段切错一位，就会把
别的局的手牌贴到异常局上，比不报还糟。下面用一份最小合成日志把这四件事钉住。

直接用 ``python tests/test_silent_game_audit.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

AUDIT = None


def load_tool(stem: str):
    """按文件路径加载 ``tools/`` 下的脚本（它们不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(f"tools_{stem}", _PLUGIN_ROOT / "tools" / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # 先进注册表：模块里用了 @dataclass
    spec.loader.exec_module(module)
    return module


AUDIT = load_tool("silent_game_audit")


def _line(label: str, body: str) -> str:
    """一条带进程标签的日志行（真实日志是 ``INFO [我方] [26-10-07 00:12:00] …``）。"""

    return f"INFO [{label}] [26-10-07 00:12:00] {body}"


def _duel(hands: "dict[str, List[str]]", result: str, *, placements: "List[str] | None" = None) -> List[str]:
    """造一局：起手块（每个标签一块）＋若干落位行＋"打完"那行。"""

    lines: List[str] = []
    for label, cards in hands.items():
        lines.append(_line(label, "*********Bot Hand*********"))
        lines.extend(_line(label, card) for card in cards)
        lines.append(_line(label, "*********Bot Spell*********"))
    for text in placements or []:
        lines.append(_line("我方", f"落位：{text}"))
    lines.append(_line("WindBot", "闸门已关闭"))
    lines.append(result)
    return lines


def _write(tmp_path: Path, lines: List[str], *, parallel: int = 1) -> Path:
    """写一份日志（头部带上"并发 N"，工具靠它拒并发日志）。"""

    header = [f"每轮 10 轮镜像（20 局），并发 {parallel}"]
    path = tmp_path / "run.log"
    path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    return path


def test_flags_low_action_side_and_prints_hands(tmp_path: Path) -> None:
    """动作低于门槛的一侧要连它的起手一起打出来。"""

    log = _write(
        tmp_path,
        _duel(
            {"我方": ["本家怪", "本家怪", "手坑", "手坑", "手坑"],
             "对手(ToonShop)": ["暗黑兔", "漫画猫", "卡通世界", "看透心灵之眼", "邪魔箱"]},
            "  [1/20] 第 0 轮 → 对手(ToonShop)（3 回合，动作 2:27）",
        ),
    )
    sections, parallel = AUDIT.split_sections(log.read_text(encoding="utf-8"))
    assert parallel == 1
    assert len(sections) == 1
    low = sections[0].low_sides(3)
    assert low == [("我方", 2, "对手")], low
    assert sections[0].hands["我方"] == ["本家怪", "本家怪", "手坑", "手坑", "手坑"]
    assert sections[0].hands["对手(ToonShop)"][0] == "暗黑兔"


def test_sections_do_not_leak_hands_across_duels(tmp_path: Path) -> None:
    """两局的起手不能串——切段错一位就会把下一局的手牌贴到上一局。"""

    lines = _duel(
        {"我方": ["第一局A", "第一局B"]},
        "  [1/20] 第 0 轮 → 我方（3 回合，动作 40:20）",
    ) + _duel(
        {"我方": ["第二局A", "第二局B"]},
        "  [2/20] 第 0 轮 → 对手(ToonShop)（3 回合，动作 1:30）",
    )
    sections, _ = AUDIT.split_sections("\n".join(lines))
    assert [s.hands["我方"] for s in sections] == [["第一局A", "第一局B"], ["第二局A", "第二局B"]]
    assert [s.low_sides(3) for s in sections] == [[], [("我方", 1, "对手")]]


def test_keeps_first_dump_only(tmp_path: Path) -> None:
    """同一个标签打多次手牌块时只留**第一次**（回合越往后手牌越少，会被当成"起手"）。"""

    lines = [
        _line("我方", "*********Bot Hand*********"),
        _line("我方", "起手一"),
        _line("我方", "起手二"),
        _line("我方", "起手三"),
        _line("我方", "*********Bot Spell*********"),
        _line("我方", "*********Bot Hand*********"),
        _line("我方", "打剩一张"),
        _line("我方", "*********Bot Spell*********"),
    ] + _duel({"对手(X)": ["对面一"]}, "  [1/20] 第 1 轮 → 我方（3 回合，动作 30:20）")
    sections, _ = AUDIT.split_sections("\n".join(lines))
    assert sections[0].hands["我方"] == ["起手一", "起手二", "起手三"]


def test_side_guess_by_deck_membership(tmp_path: Path) -> None:
    """给了牌表就能判"这手属于哪一边"（日志标签会串，不能只信标签）。"""

    cdb = tmp_path / "cards.cdb"
    import sqlite3

    connection = sqlite3.connect(cdb)
    connection.execute("CREATE TABLE datas (id INTEGER PRIMARY KEY, alias INTEGER DEFAULT 0)")
    connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT)")
    connection.executemany("INSERT INTO texts (id, name) VALUES (?, ?)", [(1, "左卡"), (2, "右卡")])
    connection.executemany("INSERT INTO datas (id, alias) VALUES (?, 0)", [(1,), (2,)])
    connection.commit()
    connection.close()
    left_ydk = tmp_path / "left.ydk"
    left_ydk.write_text("#main\n1\n1\n", encoding="utf-8")
    right_ydk = tmp_path / "right.ydk"
    right_ydk.write_text("#main\n2\n2\n", encoding="utf-8")

    left = AUDIT.load_deck_names(left_ydk, cdb)
    right = AUDIT.load_deck_names(right_ydk, cdb)
    assert left == {"左卡"} and right == {"右卡"}
    assert "左方牌表" in AUDIT.guess_side(["左卡", "左卡"], left, right)
    assert "右方牌表" in AUDIT.guess_side(["右卡", "右卡"], left, right)


def test_rejects_parallel_log(tmp_path: Path) -> None:
    """并发 > 1 的日志切不准 → 工具必须拒（返回 2），不能给一份看着像样的错结果。"""

    log = _write(
        tmp_path,
        _duel({"我方": ["甲"]}, "  [1/20] 第 0 轮 → 我方（3 回合，动作 40:20）"),
        parallel=6,
    )
    assert AUDIT.main(["--log", str(log)]) == 2
