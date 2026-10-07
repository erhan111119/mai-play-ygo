"""问 AI 这条链路的接线测试：素材（打法数据 + 攻略要点）有没有真的进提示词、计数能不能读到。

这些是"量出来的数字可不可信"的前提：素材没进去，A/B 比的就只是"有 AI"和"没 AI"；
计数读不到，就说不清多花的那几十次模型调用换来了什么。

直接用 ``python tests/test_brain_eval.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import logging
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from train.arena import ArenaConfig, DuelOutcome, DuelArena, Fighter  # noqa: E402


def load_tool(name: str):
    """按文件路径加载 tools/ 下的脚本（它们不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(
        f"tool_{name}", str(_PLUGIN_ROOT / "tools" / f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_config(root: Path) -> ArenaConfig:
    """造一份最小擂台配置（测试不会真起进程，路径只要填上）。"""

    return ArenaConfig(
        ygopro_executable=root / "ygopro.exe",
        ygopro_dir=root,
        windbot_executable=root / "WindBot.exe",
        windbot_dir=root,
    )


def make_outcomes(left: str, right: str, *, wins: int, total: int) -> List[DuelOutcome]:
    """造一批结果：前 ``wins`` 局左方赢，其余右方赢。"""

    outcomes: List[DuelOutcome] = []
    for index in range(total):
        outcomes.append(
            DuelOutcome(
                left=left,
                right=right,
                left_seat=index % 2,
                winner=left if index < wins else right,
                turns=6,
                duration_seconds=20,
                reason="lp_zero",
                actions_left=12,
                actions_right=9,
                lp_left=4000,
                lp_right=0,
                round_index=index // 2,
            )
        )
    return outcomes


def test_arena_hands_combo_guide_and_playbook_to_the_model() -> None:
    """攻略要点（展开流程）与打法数据都要进提示词。

    攻略要点是踩过的坑：没有它时模型会凭常识"省牌"，把展开链上的关键卡省下来不发
    （用户实测"连基础展开都断了"）。房间那边一直带着它，擂台若不带，量到的就不是线上那套东西。
    """

    import train.ai_brain as brain

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "combos").mkdir()
        (root / "combos" / "83.txt").write_text("先出 A 再出 B（展开链）\n", encoding="utf-8")
        captured = {}

        def fake_decider(settings, **kwargs):
            captured.update(kwargs)
            return lambda question: "yes"

        original_settings = brain.load_brain_models
        original_decider = brain.make_model_decider
        # 擂台走 load_brain_models（默认模型 + 可选的"会思考的强模型"）
        brain.load_brain_models = lambda data_dir: (
            brain.BrainModelSettings(base_url="https://example.invalid", api_key="k", model="m"),
            brain.BrainModelSettings(
                base_url="https://example.invalid", api_key="k", model="strong", max_tokens=4096
            ),
        )
        brain.make_model_decider = fake_decider
        try:
            arena = DuelArena(make_config(root), logger=logging.getLogger("test"))
            arena._data_dir = lambda: root  # 数据目录指到临时目录
            decider = arena._make_llm_decider(
                Fighter(
                    name="我方",
                    deck_file=root / "d.ydk",
                    style="MalissOCG",
                    playbook="summon_order=1",
                    brain="llm",
                    deck_id=83,
                )
            )
        finally:
            brain.load_brain_models = original_settings
            brain.make_model_decider = original_decider

        assert decider is not None
        assert "先出 A 再出 B" in captured["combo_guide"], captured
        assert captured["playbook"] == "summon_order=1"
        assert captured["deck_name"] == "我方"
        # 请求要走带护栏的那条（模型服务地址只允许 http/https、拒绝内网）
        assert captured["request"] is not None

        # 没有攻略的卡组不编一份出来：deck_id 为 0 时就是空
        assert brain.load_combo_guide(root, 87) == ""


def test_brain_server_is_collectable_for_reporting() -> None:
    """答复服务要能被调用方拿到——报告里的"问了多少次"读的就是它。"""

    from train.ai_brain import BrainServer

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        collected: List[object] = []

        class ObservingArena(DuelArena):
            def _make_brain_server(self, prefix, decide):
                server = super()._make_brain_server(prefix, decide)
                collected.append(server)
                return server

        arena = ObservingArena(make_config(root), logger=logging.getLogger("test"))
        server = arena._make_brain_server(root / "brain.txt", lambda question: "yes")
        assert isinstance(server, BrainServer)
        assert collected == [server]
        assert server.answered == 0


def test_brain_eval_resolves_script_names_not_file_names() -> None:
    """``Deck=`` 要的是脚本名（``Albaz``），传文件名（``AI_Albaz``）会静默换成随机执行器。

    实测踩过：对手那一侧被传了文件名，WindBot 不报错、随机挑了个执行器顶上，
    那个执行器不认识这副牌，整局一步不动——外面看起来像"AI 一侧大胜"，其实是量错了东西。
    """

    tool = load_tool("brain_eval")
    cli = tool.load_tool("train_arena")
    styles = cli.WIND_BOT_DECK_NAMES

    assert tool.style_for("Albaz", styles) == "Albaz"
    assert tool.style_for("MalissOCG", styles) == "MalissOCG"
    assert tool.style_for("AI_Albaz", styles) == "Test", "文件名不是脚本名"
    assert tool.style_for("D:/x/deck.ydk", styles) == "Test", "任意 .ydk 没有专属脚本"

    ok, detail = tool.style_registered("Albaz", Path(styles and "x"), styles)
    assert ok and "内置" in detail
    with tempfile.TemporaryDirectory() as directory:
        fake = Path(directory) / "not-windbot.exe"
        fake.write_bytes(b"MZ" + b"\x00" * 64)
        bad, why = tool.style_registered("AI_Albaz", fake, styles)
        assert not bad, why

        # 生成的脚本按"类名在不在 exe 里"判定。
        # **别拿它跟原版 exe 注册数比大小**：两边常常是同一份源码树产物，
        # 那样比永远判"没编译进去"（实测把编译好的 Gen88 误报成缺失）。
        with_gen = Path(directory) / "with-gen.exe"
        with_gen.write_bytes(b"MZ" + b"Gen42Executor" + b"\x00" * 32)
        good, why = tool.style_registered("Gen42", with_gen, styles)
        assert good and "Gen42Executor" in why, why
        missing, why = tool.style_registered("Gen99", with_gen, styles)
        assert not missing and "Gen99Executor" in why, why


def test_brain_eval_baseline_and_verdict() -> None:
    """基线比较一律用 95% 区间：分不开就返回 3，让人知道"这批样本没看出变化"。"""

    tool = load_tool("brain_eval")

    assert tool.parse_baseline("") is None
    assert tool.parse_baseline("4/30") == (4, 30)
    try:
        tool.parse_baseline("4 局")
    except ValueError as exc:
        assert "胜局/局数" in str(exc)
    else:  # pragma: no cover - 写错了必须报错，不能静默当成没有基线
        raise AssertionError("写错的基线应该直接报错")

    # 明显更好：区间整体高于基线
    code = tool.report(
        make_outcomes("我方", "对手", wins=9, total=10),
        left_name="我方",
        right_name="对手",
        answered=42,
        answers={"yes": 30, "no": 12},
        baseline=(4, 30),
    )
    assert code == 0
    # 与基线同量级：分不开，返回 3（不是"变好了"）
    code = tool.report(
        make_outcomes("我方", "对手", wins=1, total=10),
        left_name="我方",
        right_name="对手",
        answered=0,
        baseline=(4, 30),
    )
    assert code == 3
    # 一局都没打成：报错退出，不能给个 0% 胜率充数
    assert (
        tool.report(
            [], left_name="我方", right_name="对手", answered=0, baseline=None
        )
        == 2
    )


def main() -> int:
    """逐个执行测试函数。"""

    tests = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
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
