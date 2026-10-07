"""擂台的单元测试：胜率统计、空过检测、镜像配对与 CLI 的对战组合计算。

不需要真跑对局的部分都在这里钉住；真跑对局的部分由 ``tools/train_arena.py``
手工触发（跑一局要十几秒，不适合放进日常回归）。

直接用 ``python tests/test_arena.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import gc
import importlib.util
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from train.arena import ArenaConfig, DuelOutcome, Fighter  # noqa: E402  导入顺序受 sys.path 补丁影响
from train.store import DuelStore  # noqa: E402


def load_cli():
    """按文件路径加载 CLI 模块（``tools/`` 不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(
        "train_arena_cli", str(_PLUGIN_ROOT / "tools" / "train_arena.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_outcome(
    left: str,
    right: str,
    *,
    winner: str,
    actions_left: int = 10,
    actions_right: int = 10,
    turns: int = 5,
    error: str = "",
    round_index: int = -1,
) -> DuelOutcome:
    """造一条结果。"""

    return DuelOutcome(
        left=left,
        right=right,
        left_seat=0,
        winner=winner,
        turns=turns,
        duration_seconds=12,
        reason="lp_zero",
        actions_left=actions_left,
        actions_right=actions_right,
        lp_left=4000,
        lp_right=0,
        error=error,
        round_index=round_index,
    )


def test_win_rate_counts_both_sides() -> None:
    """同一副牌坐左边或右边都要算进胜率；未分出胜负的局不计入。"""

    with tempfile.TemporaryDirectory() as directory:
        store = DuelStore(Path(directory) / "arena.db")
        try:
            # A 坐左边赢一局
            store.record("t", make_outcome("A", "B", winner="A"))
            # A 坐右边赢一局（左右视角要被合并统计）
            store.record("t", make_outcome("B", "A", winner="A"))
            # 一局没分出胜负：不进胜率统计（分母也算不出胜负的局）
            store.record("t", make_outcome("A", "B", winner="unknown"))

            table = dict((name, (wins, total, rate)) for name, wins, total, rate in store.win_rates("t"))
            assert table["A"][:2] == (2, 2), table["A"]
            assert table["A"][2] == 1.0, table["A"]
            assert table["B"][:2] == (0, 2), table["B"]
            assert table["B"][2] == 0.0, table["B"]
            assert table["A"][2] >= table["B"][2], "胜率表要按胜率降序"
            assert store.count("t") == 3
            assert store.count() == 3
            assert store.count("别的擂台") == 0
        finally:
            store.close()


def test_silent_scripts_detected() -> None:
    """动作数过低的局要能被单独列出来——脚本与卡表不匹配时就是这种表现。"""

    with tempfile.TemporaryDirectory() as directory:
        store = DuelStore(Path(directory) / "arena.db")
        try:
            store.record("t", make_outcome("哑巴", "正常", winner="正常", actions_left=0))
            store.record("t", make_outcome("正常", "哑巴", winner="正常", actions_right=1))
            store.record("t", make_outcome("正常", "正常", winner="正常"))
            silent = dict(store.silent_scripts("t", threshold=3))
            assert silent == {"哑巴": 2}, silent
            assert store.silent_scripts("别的擂台") == []
        finally:
            store.close()


def test_pair_alternates_seats() -> None:
    """镜像配对：同一对对手要各坐一次先攻，否则先手优势会盖住脚本差异。"""

    import asyncio
    import tempfile

    from train.arena import DuelArena

    calls: List[tuple] = []

    class _RecordingArena(DuelArena):
        """把 play_duel 的调用记下来，不真的起进程。

        卡表内容要在**调用当时**读走：擂台跑完就把这一轮的副本删了（不留垃圾文件）。
        """

        async def play_duel(  # type: ignore[override]
            self,
            left,
            right,
            *,
            left_seat=0,
            left_deck_file=None,
            right_deck_file=None,
            round_index=-1,
        ):
            calls.append(
                (
                    left.name,
                    right.name,
                    left_seat,
                    left_deck_file.read_text(encoding="utf-8") if left_deck_file else "",
                    right_deck_file.read_text(encoding="utf-8") if right_deck_file else "",
                    left_deck_file,
                    right_deck_file,
                )
            )
            return make_outcome(left.name, right.name, winner=left.name)

    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        left_deck = windbot_dir / "a.ydk"
        right_deck = windbot_dir / "b.ydk"
        left_deck.write_text("#main\n100\n!side\n", encoding="utf-8")
        right_deck.write_text("#main\n200\n!side\n", encoding="utf-8")
        config = ArenaConfig(
            ygopro_executable=Path("x"),
            ygopro_dir=Path("x"),
            windbot_executable=Path("x"),
            windbot_dir=windbot_dir,
        )
        arena = _RecordingArena(config)
        left = Fighter(name="A", deck_file=left_deck, style="A")
        right = Fighter(name="B", deck_file=right_deck, style="B")
        outcomes = asyncio.run(arena.play_pair(left, right, rounds=1))

        # 两局的对手顺序不变（结果口径一致），只有 left 坐的座位从 0 换成 1
        assert [(row[0], row[1], row[2]) for row in calls] == [("A", "B", 0), ("A", "B", 1)], calls
        assert len(outcomes) == 2
        # 记账时 left/right 不应随座位变化：两局的 left 都该是 A
        assert outcomes[0].left == "A" and outcomes[0].right == "B", outcomes[0]
        assert outcomes[1].left == "A" and outcomes[1].right == "B", outcomes[1]

        # **双方各打各的牌**：两副不同的牌对战，绝不能把左方的牌发给右方
        # （踩过：右方被迫打左方的牌，执行器不认识那些卡就整局一步不走）
        assert "100" in calls[0][3] and "200" not in calls[0][3], calls[0][3]
        assert "200" in calls[0][4] and "100" not in calls[0][4], calls[0][4]

        # 共用卡表的模式（同一副牌比两个脚本）仍然只发一份牌
        calls.clear()
        asyncio.run(arena.play_pair(left, right, rounds=1, shared_deck=True))
        assert calls[0][5] == calls[0][6], calls[0]
        assert "100" in calls[0][3]


def test_cli_builds_round_robin_and_explicit_pairs() -> None:
    """CLI 的组合计算：指名卡组做巡回赛（两两都打）、--pairs 只打指定组合。"""

    module = load_cli()
    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        decks = windbot_dir / "Decks"
        decks.mkdir(parents=True)
        for stem in ("AI_Albaz", "AI_BlueEyes", "AI_Kashtira"):
            (decks / f"{stem}.ydk").write_text("#main\n89631139\n!side\n", encoding="utf-8")

        class _Args:
            """参数替身。"""

            pairs: List[str] = []
            deck_names = "Albaz,Blue-Eyes,Kashtira"
            decks = 0

        matchups = module.build_matchups(_Args(), windbot_dir)
        assert len(matchups) == 3, matchups  # C(3,2)
        names = {frozenset((left.name, right.name)) for left, right in matchups}
        assert frozenset(("Albaz", "Blue-Eyes")) in names
        assert frozenset(("Blue-Eyes", "Kashtira")) in names

        class _PairsArgs:
            """只打指定组合的参数替身。"""

            pairs = ["Albaz,Kashtira"]
            deck_names = ""
            decks = 0

        pairs = module.build_matchups(_PairsArgs(), windbot_dir)
        # --pairs 的展示名用卡表文件名（投稿卡组给的是 .ydk 路径，路径当名字读不出来）
        assert [(left.name, right.name) for left, right in pairs] == [("AI_Albaz", "AI_Kashtira")]
        # 脚本名仍按内置名解析：拿到路径时不能把路径当脚本名（那会落进随机执行器）
        assert [left.style for left, _ in pairs] == ["Albaz"] and [right.style for _, right in pairs] == ["Kashtira"]


def test_cli_reports_missing_paths() -> None:
    """路径没配齐时要明确报缺哪一项，而不是跑起来才发现。"""

    module = load_cli()
    missing = module.check_paths({"ygopro_exe": "", "ygopro_dir": "C:/不存在", "windbot_exe": "", "windbot_dir": ""})
    assert any("ygopro.exe" in item for item in missing)
    assert any("不存在" in item for item in missing)
    assert len(missing) == 4
    assert module.check_paths(
        {"ygopro_exe": sys.executable, "ygopro_dir": str(_PLUGIN_ROOT), "windbot_exe": sys.executable, "windbot_dir": str(_PLUGIN_ROOT)}
    ) == []


def test_ab_fighters_require_plan_aware_executor() -> None:
    """A/B 实验缺"带计划感知执行器的 exe"时必须报错，不能拿原版 exe 凑合。

    护栏来自一次真实的误测：WindBot 找不到 ``Deck=PlanAware`` 时不报错，而是随机挑一个
    Normal 档执行器顶上 —— 计划文件没人读、双方行为与计划无关，一整批 A/B 数字全是废的。
    所以这里缺文件就直接抛，让它没法安静地跑起来。
    """

    cli = load_cli()
    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        decks = windbot_dir / "Decks"
        decks.mkdir()
        (decks / "AI_BlueEyes.ydk").write_text("#main\n89631139\n", encoding="utf-8")
        missing_exe = windbot_dir / "bin" / "PlanAware" / "WindBot.exe"

        try:
            cli.build_ab_fighters("Blue-Eyes", "Test", windbot_dir, missing_exe)
        except FileNotFoundError as exc:
            assert "PlanAware" in str(exc)
        else:
            raise AssertionError("缺计划感知执行器本该报错")

        # 给了 exe 就把试验组指到它上面，对照组仍用配置里那个
        plan_exe = windbot_dir / "WindBot.exe"
        plan_exe.write_bytes(b"MZ")
        treatment, control = cli.build_ab_fighters("Blue-Eyes", "Test", windbot_dir, plan_exe)
        assert treatment.executable == plan_exe
        assert control.executable is None, "对照组不该被指到计划感知的那份 exe"
        assert treatment.style == "PlanAware" and control.style == "Test"
        assert treatment.deck_file == control.deck_file, "A/B 的唯一差别是脚本，卡表必须相同"


def test_pair_styles_takes_precedence_over_ab_plan() -> None:
    """命令行同时给了 --pair-styles 与 --ab-plan 时，必须走「脚本排名」那条路。

    这两个参数都要用 --ab-plan 传卡组名，判断顺序写反会让排名那批**安静地**变成 A/B 实验：
    跑完胜率表里写着 PlanAware，看起来一切正常，其实根本没在比两个出厂脚本。
    实测踩过一次（那一批 300 局的数据只能改当 A/B 用）。
    """

    import argparse

    cli = load_cli()
    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        decks = windbot_dir / "Decks"
        decks.mkdir()
        (decks / "AI_BlueEyes.ydk").write_text("#main\n89631139\n", encoding="utf-8")
        args = argparse.Namespace(
            pair_styles="Test,Blue-Eyes", ab_plan="Blue-Eyes", control_style="Test"
        )
        left, right = cli.select_matchups(args, windbot_dir, Path("不存在的计划执行器"))[0]
        assert (left.style, right.style) == ("Test", "Blue-Eyes"), (left.style, right.style)
        assert left.executable is None and right.executable is None, "排名两条腿都用出厂 exe"


def test_pair_streams_outcomes_and_records_round() -> None:
    """每局打完立刻回调一次（结果要能边打边落库），并且带上轮次号。

    为什么这两件事要紧：400 局一批要跑十几分钟，等全部跑完再落库的话，中途既看不到进度、
    也分不清"慢"和"卡死"（实测在这上面白等过二十分钟）；而轮次号是复现的关键——
    同一轮的牌序由洗牌种子唯一决定，没有它就没法把"某局为什么空过"重跑一遍。
    """

    import asyncio

    from train.arena import DuelArena

    seen: List[tuple] = []

    class _RecordingArena(DuelArena):
        """把轮次记下来直接返回，不真的起进程。"""

        async def play_duel(  # type: ignore[override]
            self,
            left,
            right,
            *,
            left_seat=0,
            left_deck_file=None,
            right_deck_file=None,
            round_index=-1,
        ):
            await asyncio.sleep(0)
            return make_outcome(left.name, right.name, winner=left.name, round_index=round_index)

    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory)
        left_deck = windbot_dir / "a.ydk"
        right_deck = windbot_dir / "b.ydk"
        left_deck.write_text("#main\n100\n!side\n", encoding="utf-8")
        right_deck.write_text("#main\n200\n!side\n", encoding="utf-8")
        config = ArenaConfig(
            ygopro_executable=Path("x"),
            ygopro_dir=Path("x"),
            windbot_executable=Path("x"),
            windbot_dir=windbot_dir,
        )
        arena = _RecordingArena(config)
        left = Fighter(name="A", deck_file=left_deck, style="A")
        right = Fighter(name="B", deck_file=right_deck, style="B")
        outcomes = asyncio.run(
            arena.play_pair(
                left, right, rounds=2, on_outcome=lambda outcome: seen.append(outcome.round_index)
            )
        )

        assert seen == [0, 0, 1, 1], f"每局都该回调一次并带上轮次：{seen}"
        assert len(outcomes) == 4
    assert [outcome.round_index for outcome in outcomes] == [0, 0, 1, 1]


def test_start_brain_returns_log_for_backfill() -> None:
    """``_start_brain`` 的返回值里必须带上这一局的决策日志（打完要靠它回填胜负）。

    护栏来源：把 ``_make_llm_decider`` 改成返回二元组时漏改了一处返回值，
    结果 ``decide, log = ...`` 解包直接抛错、整批对局全变成"0 回合"——
    而单元测试当时全绿（只有真打一局才会走到那条路）。这里把返回形状钉死。
    """

    from train.arena import ArenaConfig, DuelArena, Fighter

    import asyncio

    class _TempDataDirArena(DuelArena):
        """把数据目录换成临时目录（默认那份指向插件的真实数据目录）。"""

        def __init__(self, *args, data_dir: Path, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._temp_data_dir = data_dir

        def _data_dir(self) -> Path:
            return self._temp_data_dir

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        windbot_dir = root / "windbot"
        windbot_dir.mkdir()
        (windbot_dir / "MaiBotPlans").mkdir()
        deck = root / "a.ydk"
        deck.write_text("#main\n100\n!side\n", encoding="utf-8")
        data_dir = root / "data"
        (data_dir / "knowledge").mkdir(parents=True)
        # 让决策日志有地方落（Knowledge.from_data_dir 要求库存在；这里只要文件在）
        import sqlite3 as _sqlite3

        from duel.knowledge import SCHEMA as _SCHEMA

        connection = _sqlite3.connect(data_dir / "knowledge" / "knowledge.db")
        connection.executescript(_SCHEMA)
        connection.close()
        (data_dir / "brain_model.toml").write_text(
            'base_url = "https://api.deepseek.com"\napi_key = "sk-test"\nmodel = "deepseek-chat"\n',
            encoding="utf-8",
        )

        config = ArenaConfig(
            ygopro_executable=Path("x"),
            ygopro_dir=Path("x"),
            windbot_executable=Path("x"),
            windbot_dir=windbot_dir,
        )
        arena = _TempDataDirArena(config, data_dir=data_dir)

        async def scenario() -> None:
            """在事件循环里跑（起答复任务要用 create_task）。"""

            # 不问 AI：三项都是空
            assert arena._start_brain(Fighter(name="A", deck_file=deck, style="A")) == (
                None,
                None,
                None,
            )
            # 固定答复：有前缀与任务，但没有决策日志（它不写日志）
            prefix, task, log = arena._start_brain(
                Fighter(name="A", deck_file=deck, style="A", brain="rule")
            )
            assert prefix is not None and task is not None and log is None, (prefix, task, log)
            task.cancel()

        asyncio.run(scenario())

        # 问模型：日志必须一起返回（打完用它回填胜负）
        decide, decision_log = arena._make_llm_decider(
            Fighter(name="A", deck_file=deck, style="A", brain="llm", deck_id=89), duel_key="duel-1"
        )
        assert callable(decide), decide
        assert decision_log is not None and decision_log.duel_key == "duel-1", decision_log
        decision_log.close()
        # 擂台内部的 Knowledge 连接随实例回收（真实用法是"一局一个 arena"，连接随进程结束）；
        # 这里只是为了让 Windows 能删掉临时目录
        del arena, decide, decision_log
        gc.collect()


def test_style_gate_verdicts() -> None:
    """评估闸门的判定：区间整体在 50% 之上才算赢，压在下面算丢，压着 50% 只能说样本不够。

    这条护栏来自一次真实的教训：51.7% 这种"看起来赢了"的数字在 400 局里只是 +0.7σ，
    当时如果直接采信就会把噪声当成收益发布出去。
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "evaluate_style", str(_PLUGIN_ROOT / "tools" / "evaluate_style.py")
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    # 400 局 51.7% —— 看着赢了，其实还在噪声里
    result, reason = gate.verdict(207, 400)
    assert result == "unclear", reason
    assert "样本不够" in reason
    # 200 局 60% —— 区间整体在 50% 之上，可以留
    assert gate.verdict(120, 200)[0] == "keep"
    # 200 局 40% —— 明确更差，丢掉
    assert gate.verdict(80, 200)[0] == "discard"
    # 一局没打成
    assert gate.verdict(0, 0)[0] == "unclear"

    low, high = gate.wilson_interval(55, 100)
    assert 0.44 < low < 0.46 and 0.64 < high < 0.66, (low, high)
    assert gate.rounds_needed(0.03) > 1000, "判定 3% 差别需要上千局，不该谎称小样本能看出来"

    # 注册检测的两个输入：类名规则、以及"读不出脚本数"时的兜底
    assert gate.style_class_name("Deck42") == "Deck42Executor"
    with tempfile.TemporaryDirectory() as directory:
        fake = Path(directory) / "not-windbot.exe"
        fake.write_bytes(b"MZ" + b"\x00" * 64)
        assert gate.registered_deck_count(fake, timeout=5) is None, "不是 WindBot 就该读不出来"

    # 同名对手要跳过：两边同一个脚本时"胜率"只剩先手优势，测不出脚本强弱
    kept, skipped = gate.filter_opponents(["Test", "Blue-Eyes"], "Test")
    assert kept == ["Blue-Eyes"] and skipped == ["Test"], (kept, skipped)


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
