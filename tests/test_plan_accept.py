"""``tools/plan_accept.py`` / ``tools/max_board.py`` 的回归测试：**切局与取窗的口径**。

这两个工具量的是"验收①：无干扰能不能做出最大场"，数字一错整轮结论就跟着错，
而它们踩过的坑都很隐蔽（都在下面的用例里钉住）：

* **每份日志其实有 2 局**（`--rounds 1` 的镜像）。两局是**并发**跑的、进度行是每局打完之后才打的，
  按进度行切只能得到"第一块＝两局混在一起"，逐局统计于是只认得出 1 局。
  ⇒ :func:`test_concurrent_mirror_log_counts_both_games`。
* **手坑会在对手回合就被丢掉**——窗口起点若锚在"我方第一张卡的移动"上，
  就会在轮到我方之前撞上 `(Go to End)` 关窗，量出来是空场。
  ⇒ :func:`test_handtrap_in_opponent_turn_does_not_close_window`。
* **Xyz 素材贴上去时只打一行 `overlay`**，不认它就会一直挂在场上（实测 b88 r2 报 11 只怪）。
  ⇒ :func:`test_xyz_material_leaves_the_board`。

直接用 ``python tests/test_plan_accept.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

import importlib.util
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# 我方（假）卡组：验收件＝杀调甲（怪兽）+ 同调魔法（魔陷）
OUR_CARDS = ["杀调甲", "杀调乙", "杀调·手坑", "同调魔法", "超量怪"]


def load_tool(stem: str):
    """按文件路径加载 ``tools/`` 下的脚本（它们不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(f"tools_{stem}", _PLUGIN_ROOT / "tools" / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先进 sys.modules 再 exec：模块里用了 @dataclass，它会去 sys.modules 找自己的模块对象
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ACCEPT = load_tool("plan_accept")
MAX_BOARD = load_tool("max_board")


def _label(label: str, body: str) -> str:
    """给一行正文套上日志前缀（真实日志是 ``INFO [我方] [26-10-06 16:06:05] …``）。"""

    return f"INFO [{label}] [26-10-06 16:06:05] {body}".rstrip()


def _dump(hand: Sequence[str] = (), spells: Sequence[str] = (), monsters: Sequence[str] = ()) -> List[str]:
    """一段场面 dump（WindBot 每个回合的准备阶段打印**自己的**手牌/魔陷/怪兽）。"""

    return [
        "*********Bot Hand*********", *hand,
        "*********Bot Spell*********", *spells,
        "*********Bot Monster*********", *monsters,
        "*********Finish*********",
    ]


def _write_log(tmp_path: Path, lines: Sequence[str]) -> Path:
    log = tmp_path / "b99.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def _read(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _interleave(streams: Dict[str, List[str]], *, tail: Sequence[str] = ()) -> List[str]:
    """把几条 bot 流按行轮转交错成一份日志（模拟擂台并发跑镜像时的日志）。

    轮转交错是有意的：进度行会被夹在"还没打完的那一局"中间——旧版按进度行切局就是在这一步切错的。
    """

    merged: List[str] = []
    cursors = {label: 0 for label in streams}
    while True:
        progressed = False
        for label, rows in streams.items():
            index = cursors[label]
            if index < len(rows):
                merged.append(_label(label, rows[index]))
                cursors[label] = index + 1
                progressed = True
        if not progressed:
            break
    merged.extend(tail)
    return merged


# --------------------------------------------------------------------------- 夹具

def _game_one_streams() -> Dict[str, List[str]]:
    """第 1 局：我方＝会话那台 bot（前缀 ``WindBot``），能看到的是对家 ``对手(Test)`` 那条流。

    这一局我方后攻（第 1 回合是对手的），我方第 1 回合＝全场第 2 回合。
    """

    opponent = [  # 「对手(Test)」流：这条流的 bot 是对手，我方在里面是 1 号位
        "WindBot starting...", "执行器：AI_Test",
        "(0 draw 5 card)", "(1 draw 5 card)",
        "(Go to Draw)",  # 第 1 回合＝对手（本流 0 号位）
        *_dump(hand=["魂虎"]),
        "(Go to Standby)", "(Go to Main1)",
        "(0 's 魂虎 from Hand move to MonsterZone)",
        "(Go to End)",
        "(Go to Draw)", "(1 draw 1 card)",  # 第 2 回合＝我方首回合
        "(1 's 杀调甲 from Hand move to MonsterZone)",
        "(1 's 同调魔法 from Deck move to Hand)",
        "(1 's 同调魔法 from Hand move to SpellZone)",
        "(Go to End)",
        "(Go to Draw)", "(0 draw 1 card)",  # 第 3 回合＝对手，窗口到此为止
        "(0 's 魂虎 from MonsterZone move to Grave)",
    ]
    session = [  # 「WindBot」流：会话自己那台 bot（我方）的原始输出，赛事日志里两局都叫这个名字
        "WindBot starting...", "执行器：AI_杀调",
        "(0 draw 5 card)", "(1 draw 5 card)",
        "(Go to Draw)", "(0 draw 1 card)",
        "(0 's 魂虎 from MonsterZone move to Grave)",
        "WindBot starting...", "执行器：AI_Test",  # 并发跑的镜像另一局的会话 bot
        "(0 draw 5 card)",
    ]
    return {"对手(Test)": opponent, "WindBot": session}


def _game_two_stream() -> List[str]:
    """第 2 局：我方＝会话的对家（前缀 ``我方``，本流 0 号位），**我方后攻**。

    对手第 1 回合里我方就丢了手坑（旧版把窗口锚在这里 → 在轮到我方之前就关窗 → 报空场）。
    """

    return [
        "WindBot starting...", "执行器：AI_杀调",
        "(0 draw 5 card)", "(1 draw 5 card)",
        "(Go to Draw)",  # 第 1 回合＝对手（本流 1 号位）
        *_dump(hand=["杀调·手坑", "杀调甲"]),
        "(Go to Standby)", "(Go to Main1)",
        "(1 's 魂虎 from Hand move to MonsterZone)",
        "(0 's 杀调·手坑 from Hand move to Grave)",  # 手坑在**对手回合**被丢掉
        "(Go to End)",
        "(Go to Draw)", "(0 draw 1 card)",  # 第 2 回合＝我方第 1 回合
        *_dump(hand=["杀调乙", "同调魔法"]),
        "(Go to Standby)", "(Go to Main1)",
        "(0 's 杀调乙 from Hand move to MonsterZone)",
        "(0 's 同调魔法 from Deck move to Hand)",
        "(0 's 同调魔法 from Hand move to SpellZone)",
        "(Go to End)",
        "(Go to Draw)", "(1 draw 1 card)",  # 第 3 回合＝对手
    ]


def _mirror_log(tmp_path: Path) -> Path:
    """一份 ``--rounds 1`` 的镜像日志（2 局并发；进度行在每局打完之后）。"""

    lines = [
        "DEBUG Using proactor: IocpProactor",
        "左：我方｜脚本 AI_杀调｜卡表 ours.ydk｜问 AI 不问",
        "右：对手(Test)｜脚本 Test｜卡表 blank-wall.ydk｜问 AI 不问",
        "INFO 对局进程 已启动（pid=1）",
        "INFO 对局进程 已启动（pid=2）",
        "INFO WindBot 已启动（pid=3）",
        "INFO [对手(Test)] [26-10-06 16:06:05] WindBot starting...",
    ]
    merged = _interleave({"对手(Test)": [""] + _game_one_streams()["对手(Test)"][1:],
                          "WindBot": _game_one_streams()["WindBot"],
                          "我方": _game_two_stream()},
                         tail=["  [1/2] 第 0 轮 → 我方（7 回合，动作 20:3）",
                               "  [2/2] 第 0 轮 → 我方（7 回合，动作 21:3）",
                               "===== 结果 =====", "  局数 2：我方 胜 2"])
    lines.extend(merged)
    return _write_log(tmp_path, lines)


# --------------------------------------------------------------------------- 用例

def test_concurrent_mirror_log_counts_both_games(tmp_path: Path) -> None:
    """镜像日志有 2 局：两条"带阵营名的流"各算一局（进度行只当交叉核对，不拿来切局）。"""

    log = _mirror_log(tmp_path)
    report = ACCEPT.analyze_log(_read(log), OUR_CARDS,
                                monster=("杀调甲",), spell=("同调魔法",))

    assert report.stream_count == 2, "两局各有一条带阵营名的流；WindBot 那条不能算成第三局"
    assert [stats.label for stats in report.games] == ["对手(Test)", "我方"]
    assert report.skipped == [], report.skipped
    first, second = report.games
    # 第 1 局：我方是会话那台 bot ⇒ 在这条流里是"对面"，首回合＝全场第 2 回合
    assert (first.seat, first.first_turn) == (1, 2)
    assert first.board == {"杀调甲@MonsterZone": 1, "同调魔法@SpellZone": 1}
    assert first.full, "两件验收件都在场 ⇒ 这一局该算达标"
    # 第 2 局：我方就是这条流的 bot ⇒ 0 号位；对手先攻，我方首回合同样是全场第 2 回合
    assert (second.seat, second.first_turn) == (0, 2)
    assert second.board == {"杀调乙@MonsterZone": 1, "同调魔法@SpellZone": 1}
    assert not second.empty_board
    assert not second.full, "第 2 局只出了一件 ⇒ 不算达标（按局判定，不能被另一局平均掉）"


def test_handtrap_in_opponent_turn_does_not_close_window(tmp_path: Path) -> None:
    """对手回合丢掉的手坑**不能**当窗口起点：否则轮到我方之前就关窗，量出来是空场。

    （现象②：杀调 r2 报 ``空场局 1/1``，可日志里我方第 1 回合结束明明站着「旋钮手」。）
    """

    log = _mirror_log(tmp_path)
    report = ACCEPT.analyze_log(_read(log), OUR_CARDS)
    second = report.games[1]

    assert "杀调·手坑@Grave" not in second.board, "对手回合丢掉的手坑不属于我方首回合终场"
    assert second.board.get("杀调乙@MonsterZone") == 1, "我方首回合招出来的怪必须数得到"
    assert not second.empty_board


def test_xyz_material_leaves_the_board(tmp_path: Path) -> None:
    """Xyz 素材贴上主怪之后就不在怪兽区了（只打一行 ``overlay``，没有 move 行）。"""

    stream = [
        "WindBot starting...", "执行器：AI_杀调",
        "(0 draw 5 card)", "(1 draw 5 card)",
        "(Go to Draw)",  # 第 1 回合＝我方（先攻的第 1 回合不抽牌）
        *_dump(hand=["杀调甲", "杀调乙", "超量怪"]),
        "(Go to Standby)", "(Go to Main1)",
        "(0 's 杀调甲 from Hand move to MonsterZone)",
        "(0 's 杀调乙 from Hand move to MonsterZone)",
        "(0 's 超量怪 from Extra move to MonsterZone)",
        "(0 's 超量怪 overlay 杀调甲)",
        "(0 's 超量怪 overlay 杀调乙)",
        "(Go to End)",
        "(Go to Draw)", "(1 draw 1 card)",  # 第 2 回合＝对手；回合归属靠这行抽牌定
    ]
    log = _write_log(tmp_path, _interleave({"我方": stream}))
    report = ACCEPT.analyze_log(_read(log), OUR_CARDS)

    assert len(report.games) == 1
    stats = report.games[0]
    assert stats.first_turn == 1 and stats.seat == 0
    assert stats.board == {"超量怪@MonsterZone": 1}, stats.board


def test_hand_filter_skips_games_where_our_hand_is_invisible(tmp_path: Path) -> None:
    """``--hand`` 要按我方**起手**筛；看不到我方起手的那一局宁可跳过也不猜。"""

    log = _mirror_log(tmp_path)
    lines = _read(log)

    # 第 2 局（我方＝本流的 bot）起手＝杀调·手坑 + 杀调甲（它在对手回合就丢掉了手坑）
    matched = ACCEPT.analyze_log(lines, OUR_CARDS, hand=("杀调甲",))
    assert [stats.label for stats in matched.games] == ["我方"]
    assert matched.skipped[0][0] == 1 and "起手" in matched.skipped[0][1]

    missing = ACCEPT.analyze_log(lines, OUR_CARDS, hand=("超量怪",))
    assert missing.games == []
    assert missing.skipped and "起手不含" in missing.skipped[1][1]

    # 第 1 局（我方＝会话那台 bot）：这条流里只有对手的 dump ⇒ 我方起手不可见 ⇒ 跳过并说明原因
    invisible = ACCEPT.analyze_log(lines, OUR_CARDS, hand=("杀调甲",), label="对手(Test)")
    assert invisible.games == []
    assert invisible.skipped and "--hand" in invisible.skipped[0][1]


def test_max_board_summary_counts_by_game(tmp_path: Path) -> None:
    """``max_board`` 的汇总口径：达标局、空场局、平均件数都按"局"算。"""

    log = _mirror_log(tmp_path)
    report = ACCEPT.analyze_log(_read(log), OUR_CARDS,
                                monster=("杀调甲",), spell=("同调魔法",))
    stats = MAX_BOARD._summarize(report)

    assert stats["games"] == 2 and stats["streams"] == 2
    # 第 1 局两件都在（达标 1 局）；第 2 局只有魔陷那一件
    assert (stats["full"], stats["avg"], stats["need"], stats["empty"]) == (1, 3, 4, 0)


def test_max_board_can_load_plan_accept_by_path() -> None:
    """``max_board`` 是按文件路径加载 ``plan_accept`` 的：加载本身要能过（@dataclass 需要 sys.modules）。"""

    module = MAX_BOARD._load_accept()
    assert callable(module.analyze_log)
    assert callable(module.read_deck_cards)
    # 2026-10-06 起是**十副牌**（补齐了 99 耀圣 / 100 刻魔异响鸣 / 101 俱舍 / 102 珠泪）
    assert set(MAX_BOARD.DECKS) == {"88", "93", "94", "95", "96", "98", "99", "100", "101", "102"}


if __name__ == "__main__":
    from inspect import signature

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        for name, value in sorted(globals().items()):
            if not (name.startswith("test_") and callable(value)):
                continue
            if signature(value).parameters:
                value(tmp)
            else:
                value()
            print(f"✔ {name}")
    print("全部通过")
