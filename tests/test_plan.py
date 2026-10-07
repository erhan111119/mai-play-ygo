"""作战计划、教练与擂台辅助逻辑的单元测试。

这一段的价值全在"接线是否可靠"上：计划文件写坏了执行器就打不出效果、
牌序不轮换则多打几局等于重复同一局、教练的规则写反了会把保守当局促。
都不需要真跑对局，所以放在回归里。

直接用 ``python tests/test_plan.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import asyncio
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from train.arena import shuffle_deck  # noqa: E402  导入顺序受 sys.path 补丁影响
from train.coach import TurnContext, make_coach, rule_based_plan  # noqa: E402
from train.plan import DuelPlan, clear_plan, plan_path, read_plan, write_plan  # noqa: E402

YDK_TEXT = """#main
111
222
333
444
555
#extra
999
!side
888
"""


def write_deck(directory: Path) -> Path:
    """写一副最小卡表。"""

    path = Path(directory) / "AI_Test.ydk"
    path.write_text(YDK_TEXT, encoding="utf-8")
    return path


def main_side(text: str) -> List[str]:
    """取出 .ydk 的主卡组行。"""

    lines = text.splitlines()
    start = lines.index("#main") + 1
    end = lines.index("#extra")
    return [line for line in lines[start:end] if line.strip()]


def test_plan_round_trip() -> None:
    """计划要能原样写出去、原样读回来。"""

    plan = DuelPlan(aggression=0.8, hold_handtraps=True, prefer_direct=True, turn=5, notes="落后 2000")
    restored = DuelPlan.from_text(plan.to_text())
    assert abs(restored.aggression - plan.aggression) < 0.01
    assert restored.hold_handtraps is True
    assert restored.prefer_direct is True
    assert restored.turn == 5
    assert restored.notes == "落后 2000"


def test_plan_clamps_and_sanitises() -> None:
    """越界数值要收敛，备注里的换行要去掉——跨行会破坏 key=value 的行结构。"""

    plan = DuelPlan(aggression=9.0, notes="第一行\n第二行").clamped()
    assert plan.aggression == 1.0
    assert "\n" not in plan.to_text().splitlines()[4], plan.to_text()
    assert DuelPlan(aggression=-3).clamped().aggression == 0.0


def test_plan_file_is_atomic_and_readable() -> None:
    """写计划走临时文件 + 原子替换，并且能读回、能清掉。"""

    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        target = plan_path(windbot_dir)
        assert read_plan(target) is None  # 还没写
        path = write_plan(target, DuelPlan(aggression=0.3, turn=2))
        assert path == target
        assert not path.with_name(path.name + ".tmp").exists(), "临时文件要被替换掉，不留残渣"
        plan = read_plan(target)
        assert plan is not None and abs(plan.aggression - 0.3) < 0.01 and plan.turn == 2
        clear_plan(target)
        assert read_plan(target) is None


def test_parallel_plan_files_do_not_collide() -> None:
    """并发对局各写各的计划文件，互不干扰。

    这条是实测教训的护栏：5 个并发对局共用同一个 MaiBotPlan.txt 时，
    在 Windows 上写计划会互相撞（文件占用），整批对局都被判失败。
    """

    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        first = windbot_dir / "MaiBotPlans" / "plan_a.txt"
        second = windbot_dir / "MaiBotPlans" / "plan_b.txt"
        write_plan(first, DuelPlan(aggression=0.9, notes="甲"))
        write_plan(second, DuelPlan(aggression=0.1, notes="乙"))
        assert read_plan(first).notes == "甲"
        assert read_plan(second).notes == "乙"
        assert read_plan(first).notes != read_plan(second).notes, "两个对局不该读到彼此的计划"
        clear_plan(first)
        assert read_plan(first) is None and read_plan(second) is not None


def test_rule_coach_shifts_by_lp() -> None:
    """规则教练：落后要更凶、领先要更稳，并在保守时留手坑。"""

    behind = asyncio.run(
        rule_based_plan(
            TurnContext(
                turn=1, my_lp=1000, opponent_lp=7000,
                my_monsters=1, my_spells=0, opponent_monsters=1, opponent_spells=0,
            )
        )
    )
    ahead = asyncio.run(
        rule_based_plan(
            TurnContext(
                turn=9, my_lp=7000, opponent_lp=1000,
                my_monsters=2, my_spells=2, opponent_monsters=0, opponent_spells=0,
            )
        )
    )
    assert behind.aggression > 0.5, behind
    assert ahead.aggression < 0.5, ahead
    assert ahead.hold_handtraps is True, "打得保守时手坑该留着"
    assert "落后" in behind.notes and "领先" in ahead.notes


def test_rule_coach_prefers_direct_when_board_is_fine() -> None:
    """场面不吃亏时倾向打脸；对面铺得更大时不去撞。"""

    even = asyncio.run(
        rule_based_plan(
            TurnContext(
                turn=3, my_lp=4000, opponent_lp=4000,
                my_monsters=2, my_spells=1, opponent_monsters=2, opponent_spells=1,
            )
        )
    )
    behind_board = asyncio.run(
        rule_based_plan(
            TurnContext(
                turn=3, my_lp=4000, opponent_lp=4000,
                my_monsters=0, my_spells=0, opponent_monsters=3, opponent_spells=2,
            )
        )
    )
    assert even.prefer_direct is True
    assert behind_board.prefer_direct is False
    assert "对面场面更大" in behind_board.notes


def test_make_coach_names() -> None:
    """教练名字解析：none 表示不干预，rule 是规则教练，其它名字要报错而不是静默。"""

    assert make_coach("") is None
    assert make_coach("none") is None
    assert make_coach("rule") is not None
    try:
        make_coach("写错了")
    except ValueError as exc:
        assert "未知的教练" in str(exc)
    else:
        raise AssertionError("未知教练名本该报错")


def test_llm_coach_parses_real_model_answers() -> None:
    """模型教练的解析要认得出真实模型的输出，也要拒绝"复述题目"这种含糊回答。

    这里的两条坏样本是从真实运行日志里抄下来的：免费小模型把提示里的
    "0.5 或 0.8" 原样抄了回来、以及答案被截断到只剩一行。
    两者都必须判为"读不出"（退回兜底教练），绝不能替它挑一个档位。
    """

    from train.coach import LlmCoach

    context = TurnContext(turn=4, my_lp=2500, opponent_lp=6000, my_monsters=1, my_spells=1, opponent_monsters=3, opponent_spells=2)

    def parse(answer: str):
        return LlmCoach._parse(answer, context)

    # 标准三行
    plan = parse("aggression=0.8\nhold_handtraps=0\nprefer_direct=1")
    assert plan is not None and plan.aggression == 0.8
    assert plan.hold_handtraps is False and plan.prefer_direct is True

    # 中文档位 + 括号注解
    plan = parse("aggression=抢血（对面场面更大）\nhold_handtraps=1（留着）\nprefer_direct=1（打脸）")
    assert plan is not None and plan.aggression == 0.8 and plan.hold_handtraps is True

    # 「不留」里的「留」不该被当成肯定
    plan = parse("aggression=0.2\nhold_handtraps=0（不留）\nprefer_direct=1")
    assert plan is not None and plan.hold_handtraps is False

    # markdown 星号 + 冒号分隔
    plan = parse("**aggression**: 0.5\n- hold_handtraps: 1\n- prefer_direct: 0")
    assert plan is not None and plan.aggression == 0.5 and plan.hold_handtraps is True

    # 先说一句再答题：以最后一次出现的键为准
    plan = parse("根据局面：\naggression=0.2\nhold_handtraps=1\nprefer_direct=0")
    assert plan is not None and plan.aggression == 0.2 and plan.prefer_direct is False

    # 实测坏样本：复述题目（同时出现两档）→ 读不出，交给兜底
    assert parse("aggression=0.5 或 0.8\nhold_handtraps=1\nprefer_direct=1") is None
    # 实测坏样本：答案被截断 → 读不出
    assert parse("prefer_direct=1") is None
    # 开关写成「0或1」这种两可 → 读不出
    assert parse("aggression=0.5\nhold_handtraps=0或1\nprefer_direct=1") is None
    # 越界数字（0.95）要收敛到最近档位 0.8，而不是原样写进计划
    plan = parse("aggression=0.95\nhold_handtraps=0\nprefer_direct=1")
    assert plan is not None and plan.aggression == 0.8


def test_plan_write_survives_reader_lock_and_reports_failure() -> None:
    """计划文件被读端占用时要重试；一直占用则报错，不能悄悄丢掉这一次写入。

    Windows 上 .NET 读文件时不允替换，而执行器每几次决策就读一次 —— 这是实测撞出来的。
    """

    import os as os_module

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "plan_lock.txt"
        real_replace = os_module.replace
        attempts: List[int] = []

        def flaky_replace(src, dst):  # type: ignore[no-untyped-def]
            """第一次假装被占用，之后放行。"""

            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError(13, "文件被占用")
            return real_replace(src, dst)

        os_module.replace = flaky_replace  # type: ignore[assignment]
        try:
            write_plan(target, DuelPlan(aggression=0.4, turn=1))
        finally:
            os_module.replace = real_replace  # type: ignore[assignment]
        assert len(attempts) == 2, "第一次被占用后该重试"
        assert read_plan(target) is not None

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "plan_always_locked.txt"

        def always_locked(src, dst):  # type: ignore[no-untyped-def]
            raise PermissionError(13, "文件被占用")

        os_module.replace = always_locked  # type: ignore[assignment]
        try:
            write_plan(target, DuelPlan(aggression=0.4, turn=1))
        except RuntimeError as exc:
            assert "写计划失败" in str(exc)
        else:
            raise AssertionError("一直写不进去本该报错，而不是静默丢弃")
        finally:
            os_module.replace = real_replace  # type: ignore[assignment]


def test_shuffle_deck_is_deterministic_and_main_only() -> None:
    """固定种子洗牌：同样种子得到同样顺序（可复现、可配对），且只动主卡组。"""

    with tempfile.TemporaryDirectory() as directory:
        path = write_deck(directory)
        original = path.read_text(encoding="utf-8")

        shuffle_deck(path, seed=7)
        first = path.read_text(encoding="utf-8")
        assert main_side(first) != main_side(original), "洗过之后顺序该变"
        assert sorted(main_side(first)) == sorted(main_side(original)), "只该换顺序，不该多/少牌"
        assert first.splitlines()[-4:] == ["#extra", "999", "!side", "888"], "额外/副卡组不该动"

        # 同样的初始牌序 + 同样的种子必须复现同样的顺序（否则镜像配对失去意义）。
        # 注意洗牌是"就地"的：想在同一个文件上复现，得先回到初始牌序再洗
        # （擂台就是这么做的：每轮从初始牌序重新洗，避免一轮轮叠加）
        path.write_text(original, encoding="utf-8")
        shuffle_deck(path, seed=7)
        assert main_side(path.read_text(encoding="utf-8")) == main_side(first), "同种子同初始牌序该一致"

        path.write_text(original, encoding="utf-8")
        shuffle_deck(path, seed=8)
        assert main_side(path.read_text(encoding="utf-8")) != main_side(first), "换种子该换顺序"


def test_shuffle_deck_survives_odd_files() -> None:
    """文件不存在、没有 #main、主卡组只有一张，都不该抛异常（擂台不该因此中断）。"""

    with tempfile.TemporaryDirectory() as directory:
        shuffle_deck(Path(directory) / "不存在.ydk", seed=1)
        empty = Path(directory) / "empty.ydk"
        empty.write_text("!side\n1\n", encoding="utf-8")
        shuffle_deck(empty, seed=1)
        single = Path(directory) / "single.ydk"
        single.write_text("#main\n111\n#extra\n999\n", encoding="utf-8")
        shuffle_deck(single, seed=1)
        assert main_side(single.read_text(encoding="utf-8")) == ["111"]


def main() -> int:
    """逐个执行测试函数。"""

    tests = [(name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures: List[str] = []
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
