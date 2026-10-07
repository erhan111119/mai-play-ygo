"""卡组打法数据（playbook）与"实测挑脚本"的单元测试。

两块都是"决定喂给执行器什么"的纯逻辑：打法数据的校验口径、候选名单与判定标准。
它们出错的后果很隐蔽——卡号写错会让整段组合静默失效、判定写松会让"打不出牌"的脚本被留下，
所以在这里钉死。

直接用 ``python tests/test_playbook.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Set, Tuple

import dataclasses
import importlib.util
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.playbook import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    MAX_ENTRIES,
    PlaybookError,
    parse_playbook,
    to_text,
    write_playbook_file,
)
from duel.room import WindBotSettings  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.session import SessionConfig, DuelSession  # noqa: E402  导入顺序受 sys.path 补丁影响


def load_tool(name: str):
    """按文件路径加载一个 tools/ 下的工具（它们自己会补 sys.path）。"""

    spec = importlib.util.spec_from_file_location(f"tool_{name}", str(_PLUGIN_ROOT / "tools" / name))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_and_format_round_trip() -> None:
    """解析与写回要一致；不认识的键忽略，写错卡号必须报错。

    护栏：卡号写错（比如少一位）在执行器那边表现为"这张牌永远找不到"，整段组合静默失效——
    那正是我们要避免的"看起来做了、其实没做"，所以宁可在这里拒绝整份数据。
    """

    allowed = [14558127, 23434538, 30118811]
    text = """
# 注释行
summon_order=30118811, 3723262
activate_order=14558127
never_activate=94145021
unknown_key=1,2,3
"""
    book = parse_playbook(text, allowed_cards=allowed + [94145021, 3723262])
    assert book.lists["summon_order"] == [30118811, 3723262], book.lists
    assert book.lists["activate_order"] == [14558127], book.lists
    assert "unknown_key" not in book.lists, "不认识的键要忽略（以后加字段不破坏兼容）"

    again = parse_playbook(to_text(book), allowed_cards=allowed + [94145021, 3723262])
    assert again.lists == book.lists, (again.lists, book.lists)

    for bad in ("summon_order=999", "summon_order=灰流丽", "这一行没有等号"):
        try:
            parse_playbook(bad, allowed_cards=allowed)
        except PlaybookError:
            continue
        raise AssertionError(f"这份数据本该被拒绝：{bad}")

    too_long = "summon_order=" + ",".join(str(card) for card in range(1, MAX_ENTRIES + 5))
    try:
        parse_playbook(too_long)
    except PlaybookError as exc:
        assert "上限" in str(exc), exc
    else:
        raise AssertionError("超过条数上限本该被拒绝")


def test_write_playbook_is_atomic_enough() -> None:
    """打法数据写文件：先写临时文件再替换（执行器随时在读，半截内容比旧数据糟得多）。"""

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "sub" / "book.txt"
        write_playbook_file(target, "summon_order=1\n")
        assert target.read_text(encoding="utf-8") == "summon_order=1\n"
        assert not list(target.parent.glob("*.tmp")), "临时文件不该留下"


def test_session_writes_playbook_file_from_deck() -> None:
    """会话把卡组里的打法数据写成文件、并把路径交给 WindBot（房间与擂台共用这一条路）。"""

    from duel.deckpool import StoredDeck

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        deck = StoredDeck(
            deck_id=1,
            group_id="111",
            display_name="测试牌",
            contributor_id="",
            contributor_name="",
            ydk_path=root / "x.ydk",
            source_format="",
            main_count=40,
            extra_count=0,
            side_count=0,
            windbot_deck="PlanAware",
            generated_script="",
            playbook="summon_order=14558127\n",
            in_random=True,
            created_at=0.0,
        )
        config = SessionConfig(
            ygopro_executable=root / "ygopro.exe",
            ygopro_dir=root,
            windbot_executable=root / "WindBot.exe",
            windbot_dir=root,
        )
        session = DuelSession(config, group_id="111", deck=deck)
        path = session._playbook_path()
        assert path is not None and path.is_file(), path
        assert path.read_text(encoding="utf-8").startswith("summon_order="), path

        # 没有数据时不写文件、也不传参数（免得给执行器一个空文件）
        empty = DuelSession(config, group_id="111", deck=dataclasses.replace(deck, playbook=""))
        assert empty._playbook_path() is None


def test_windbot_args_include_playbook() -> None:
    """命令行参数要带上 PlaybookFile（绝对路径，免得受工作目录影响）。"""

    settings = WindBotSettings(
        name="憨憨",
        deck="PlanAware",
        deck_file=Path("x.ydk"),
        playbook_file=Path("D:/tmp/book.txt"),
    )
    args = settings.to_args("127.0.0.1", 1234)
    assert "PlaybookFile=D:\\tmp\\book.txt" in args or "PlaybookFile=D:/tmp/book.txt" in args, args
    plain = WindBotSettings(name="憨憨", deck="Test")
    assert not [item for item in plain.to_args("127.0.0.1", 1234) if item.startswith("PlaybookFile=")]


def test_rank_candidates_always_includes_generic_and_current() -> None:
    """候选名单：相似度只是"少测几个"的省事办法，通用执行器与当前风格必须在名单里。

    护栏：通用执行器是唯一会读打法数据的那一个，漏掉它整套 B 方案就没人执行；
    当前风格是"换个脚本至少不能比现在更差"的基准。
    """

    tool = load_tool("pick_style.py")
    available: Dict[str, Tuple[Set[int], Set[int]]] = {
        "PlanAware": ({1, 2, 3}, {7}),
        "Blue-Eyes": ({1, 2, 9}, {8}),
        "Albaz": ({4, 5, 6}, {9}),
        "Test": ({1, 2, 3}, {7}),
    }
    picked = tool.rank_candidates([1, 2, 3], [7], available, current_style="Albaz", limit=3)
    assert picked[0] == "PlanAware", picked
    assert "Albaz" in picked, picked
    assert len(picked) <= 3, picked

    # 名额够时，相似度最高的那个也要进来（Blue-Eyes 与这副牌重合 2 张）
    wider = tool.rank_candidates([1, 2, 3], [7], available, current_style="Albaz", limit=4)
    assert wider[0] == "PlanAware" and "Blue-Eyes" in wider, wider


def test_rank_candidates_keeps_current_script_outside_the_directory() -> None:
    """当前脚本即使不在 WindBot 卡组目录里，也必须进候选。

    护栏来源：投稿卡组生成的专属脚本（``Gen89`` 这类）编译进 exe 的执行器表，却不出现在卡组目录里。
    以前候选名单要求"名字能在目录里列出来"，于是刚好漏掉最该比的基准——实测「里除出大哥」时
    四个候选全打不出牌、工具报"不改动现状"，而它自己的 ``Gen89`` 其实是能打的。
    """

    tool = load_tool("pick_style.py")
    available: Dict[str, Tuple[Set[int], Set[int]]] = {
        "PlanAware": ({1, 2, 3}, {7}),
        "Blue-Eyes": ({1, 2, 9}, {8}),
    }
    picked = tool.rank_candidates([1, 2, 3], [7], available, current_style="Gen89", limit=3)
    assert picked[0] == "PlanAware", picked
    assert "Gen89" in picked, picked


def test_pick_best_ranks_by_median_not_by_spiky_mean() -> None:
    """选脚本：中位动作数优先，均值与胜率只收尾。

    护栏来源：实测「改922」上 ``Maliss`` 有 27% 的局只打 1~2 次动作，均值却靠几局 55~67 次
    冲到 24.4，和"每局基本都能打"的 ``MalissOCG``（24.6）在均值上打平；中位数是 20.0 对 8.0。
    12 局样本的胜率也分不清 2/12 与 3/12，所以它排在最后。
    """

    tool = load_tool("pick_style.py")
    spiky = tool.ArmResult("甲", "甲", [1, 2, 2, 5, 8, 60, 62, 66], wins=1, total=12)
    steady = tool.ArmResult("乙", "乙", [19, 20, 20, 21, 22, 23, 24, 25], wins=3, total=12)
    assert spiky.average_actions > steady.average_actions, "这份数据的前提就是均值会被爆发拉高"
    assert tool.pick_best([spiky, steady]) is steady

    # 中位打平（差不到 1 次/局）才比均值：19.0 与 19.5 算平手，均值高的那个胜出
    close_low = tool.ArmResult("丙", "丙", [19, 19, 19, 19], wins=4, total=12)
    close_high = tool.ArmResult("丁", "丁", [17, 19, 20, 22], wins=1, total=12)
    assert close_low.median_actions == 19.0 and close_high.median_actions == 19.5
    assert tool.pick_best([close_low, close_high]) is close_high

    # 打不出牌的无条件出局，不管均值多好看
    silent = tool.ArmResult("戊", "戊", [0, 1, 2], wins=12, total=12)
    assert tool.pick_best([silent, steady]) is steady

    # 全都打不出牌 → 没有可选的（调用方会报"不改动现状"）
    assert tool.pick_best([silent]) is None


def test_arm_result_reports_median_and_alive_rate() -> None:
    """一行结论里要有"能动局占比"：它正是"这局是不是又空过了"这个问题的答案。"""

    tool = load_tool("pick_style.py")
    arm = tool.ArmResult("甲", "甲", [0, 2, 5, 20, 22, 24], wins=2, total=6)
    assert arm.median_actions == 12.5
    assert abs(arm.alive_rate - 4 / 6) < 1e-9
    assert "中位 12.5" in arm.describe() and "能动 67%" in arm.describe(), arm.describe()


def test_arm_result_refuses_silent_scripts() -> None:
    """判定标准：打不出牌的一律不可用，不管胜率多好看。

    护栏来源：实测有脚本编译通过但一局动作 0 次；如果只看胜率，这种脚本会靠"对面也打不出来"
    的平局混进候选。
    """

    tool = load_tool("pick_style.py")
    silent = tool.ArmResult("甲", "甲", [0, 1, 2], wins=4, total=4)
    assert silent.usable is False, silent.describe()
    assert "打不出牌" in silent.describe(), silent.describe()

    normal = tool.ArmResult("乙", "乙", [23, 24], wins=1, total=4)
    assert normal.usable is True, normal.describe()
    assert normal.average_actions == 23.5
    assert abs(normal.win_rate - 0.25) < 1e-9
    assert "不可用" not in normal.describe(), normal.describe()

    empty = tool.ArmResult("丙", "丙", [], wins=0, total=0)
    assert empty.usable is False


def test_describe_match_flags_unknown_archetypes() -> None:
    """重合度要能说清"换脚本还有没有用"：几乎不重合的牌，换执行器是死路。

    护栏来源：实测「升辉月」与它最好的执行器只重合 0.06（盈彩月夜系列，WindBot 那 72 个
    自带执行器里没有本家），而「码丽丝(OCG)·改7」与 ``MalissOCG`` 是 0.93。
    两者该走的下一步完全不同——前者要写打法数据或改卡表，后者可以继续调脚本。
    """

    tool = load_tool("pick_style.py")
    available: Dict[str, Tuple[Set[int], Set[int]]] = {
        "MalissOCG": ({1, 2, 3, 4}, {9}),
        "ChaosRitual": ({90, 91}, {99}),
    }
    strict = tool.describe_match([1, 2, 3, 4], [9], available, "MalissOCG")
    assert "0.93" in strict or "1.00" in strict, strict
    assert "认得这副牌" in strict, strict

    stranger = tool.describe_match([1, 2, 3, 4], [9], available, "ChaosRitual")
    assert "换脚本解决不了" in stranger, stranger

    # 不在目录里的执行器（通用/生成的）不按卡表打分
    generic = tool.describe_match([1, 2], [9], available, "PlanAware")
    assert "不按卡表打分" in generic, generic


def test_resolve_opponent_forms() -> None:
    """对手参数的两种写法：``脚本名``（按 WindBot 目录找卡表）与 ``脚本=卡表路径``。"""

    tool = load_tool("pick_style.py")
    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        decks = windbot_dir / "Decks"
        decks.mkdir()
        (decks / "AI_BlueEyes.ydk").write_text("#main\n100\n!side\n", encoding="utf-8")
        available = {"Blue-Eyes": ({100}, {200})}

        style, path = tool.resolve_opponent("Blue-Eyes", available, windbot_dir)
        assert style == "Blue-Eyes" and path is not None and path.is_file(), (style, path)

        explicit = decks / "AI_BlueEyes.ydk"
        style, path = tool.resolve_opponent(f"Blue-Eyes={explicit}", available, windbot_dir)
        assert style == "Blue-Eyes" and path == explicit, (style, path)

        # 找不到就明确返回 None（调用方会报错退出），不静默换一个对手
        assert tool.resolve_opponent("NotARealStyle", available, windbot_dir) == (None, None)
        assert tool.resolve_opponent("", {"X": ({1}, {2})}, windbot_dir) == (None, None)


def test_generate_playbook_prompt_and_extract() -> None:
    """写打法数据的工具：提示词要带卡号清单，抽取要拒绝坏输出。"""

    tool = load_tool("generate_playbook.py")

    class Info:
        """卡牌信息的替身（工具只用到这三个字段）。"""

        def __init__(self, card_id: int, name: str, effect: str) -> None:
            self.card_id = card_id
            self.name = name
            self.effect = effect

    cards = [Info(14558127, "灰流丽", "把手牌丢弃发动"), Info(30118811, "备份员@火灵天星", "召唤时检索")]
    prompt = tool.build_prompt(cards, "耀圣加速均")
    assert "14558127" in prompt and "30118811" in prompt, "提示词里必须列出卡号（模型凭印象写必然写错）"
    for key in ("summon_order", "activate_order", "set_order", "search_order", "never_activate"):
        assert key in prompt, key

    book, error = tool.extract_playbook("summon_order=30118811\nsearch_order=14558127\n", [14558127, 30118811])
    assert book is not None and book.lists["summon_order"] == [30118811], (book, error)
    assert tool.extract_playbook("我觉得这副牌很强", [1])[0] is None
    bad, error = tool.extract_playbook("summon_order=999\n", [14558127])
    assert bad is None and "不在卡库里" in error, error


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
