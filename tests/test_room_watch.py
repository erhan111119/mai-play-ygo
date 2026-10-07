"""``tools/room_watch.py`` 的回归测试：**每条判据都要能被最小合成日志触发**。

这个工具的价值全在"记下来的那几条是不是真的"——判据写松了会把正常游玩记成问题（用户明确说
"不要打扰正常游玩"），写紧了会漏。下面把每条规则钉在一个最小日志上：

内核脚本报错 / 插件报错 / 插件警告降噪 / 进房未开打 / 开局未结束 / 闸门报错 /
整局无动作 / 同一行只记一次 / 只读新内容（不重扫历史）。

直接用 ``python tests/test_room_watch.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import json
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


WATCH = load_tool("room_watch")


def _line(logger: str, level: str, event: str) -> str:
    """一条结构化日志（宿主日志的格式）。"""

    return json.dumps({"logger": logger, "level": level, "event": event}, ensure_ascii=False)


def _run(lines: List[str], *, now: float = 1_000_000.0) -> List[dict]:
    """把日志喂进监视器，返回这次记下的条目。"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log = tmp_path / "app_test.log.jsonl"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        state = WATCH.WatchState(tmp_path / "state.json")
        findings: List[dict] = []
        watcher = WATCH.Watcher(tmp_path, state, findings_path := tmp_path / "f.jsonl",
                                tmp_path / "r.md")
        watcher.poll(now=now)
        if findings_path.is_file():
            findings = [json.loads(row) for row in findings_path.read_text(encoding="utf-8").splitlines()]
    return findings


def test_kernel_script_error_is_recorded_with_card_ids() -> None:
    """内核脚本加载失败＝那张卡是白板：必须记，而且要把卡号抽出来。"""

    rows = _run([
        _line("", "", '[对局进程:err] "CallCardFunction"(c60811211.initial_effect): attempt to call an error function'),
        _line("", "", '[string "./script/c35761342.lua"]:15: attempt to call a nil value'),
    ])
    assert [r["category"] for r in rows] == ["内核脚本报错", "内核脚本报错"], rows
    assert rows[0]["severity"] == "high", rows[0]
    assert rows[0]["cards"] == ["60811211"], rows[0]
    assert rows[1]["cards"] == ["35761342"], rows[1]


def test_plugin_error_and_warning_noise() -> None:
    """插件 error 记 high；warning 只记"像真问题"的；大模型拒答这类噪声不落账。"""

    rows = _run([
        _line("plugin.yugioh.duel-arena", "error", "开局失败"),
        _line("plugin.yugioh.duel-arena", "warning", "读卡表失败：卡组 3"),
        _line("plugin.yugioh.duel-arena", "warning", "生成复盘点评被拒绝：模型拒答"),
        _line("plugin.yugioh.duel-arena", "info", "房间已就绪，内核端口 52663"),
    ])
    assert [r["category"] for r in rows] == ["插件报错", "插件警告"], rows
    assert rows[0]["severity"] == "high" and rows[1]["severity"] == "medium", rows


def test_player_joined_but_no_duel_started() -> None:
    """进房 3 分钟还没开打，要记一笔（版本不一致/卡握手是常见因）。"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log = tmp_path / "app_test.log.jsonl"
        log.write_text(
            _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 二憨$aN9sW") + "\n",
            encoding="utf-8",
        )
        state = WATCH.WatchState(tmp_path / "state.json")
        watcher = WATCH.Watcher(tmp_path, state, tmp_path / "f.jsonl", tmp_path / "r.md")
        watcher.poll(now=1000.0)
        assert not (tmp_path / "f.jsonl").exists(), "刚进房不该立刻报警"
        watcher.poll(now=1000.0 + WATCH.JOIN_GRACE_SECONDS + 1)
        rows = [json.loads(r) for r in (tmp_path / "f.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["category"] for r in rows] == ["进房未开打"], rows
    assert "二憨" in rows[0]["evidence"], rows[0]


def test_join_then_started_is_clean() -> None:
    """进房后正常开打 → 不记任何东西（不能打扰正常游玩）。"""

    rows = _run([
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 二憨$aN9sW"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_ended"),
    ])
    assert [r["category"] for r in rows] == ["对局摘要"], rows      # 只有摘要，没有告警


def test_duel_stuck_open_is_reported_once() -> None:
    """开局后 20 分钟还没结束 → 记一次，且只记一次。"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log = tmp_path / "app_test.log.jsonl"
        log.write_text(_line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started") + "\n",
                       encoding="utf-8")
        state = WATCH.WatchState(tmp_path / "state.json")
        watcher = WATCH.Watcher(tmp_path, state, tmp_path / "f.jsonl", tmp_path / "r.md")
        watcher.poll(now=2000.0)
        watcher.poll(now=2000.0 + WATCH.DUEL_GRACE_SECONDS + 1)
        watcher.poll(now=2000.0 + WATCH.DUEL_GRACE_SECONDS * 3)
        rows = [json.loads(r) for r in (tmp_path / "f.jsonl").read_text(encoding="utf-8").splitlines()]
    stuck = [r for r in rows if r["category"] == "开局未结束"]
    assert len(stuck) == 1, rows


def test_idle_side_is_flagged_from_placements() -> None:
    """整局 ≥3 回合、某一侧一件卡都没落到场上 → 记「整局无动作」。

    ⚠ 名册必须从**进房行**攒：只放落位行的话，"一件都没放"的那一侧根本不会出现在名册里，
    这条规则就永远触发不了（这个 bug 就是 2026-10-07 这个用例抓出来的）。
    """

    rows = _run([
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：机器人 憨憨"),
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 二憨$aN9sW"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 1 回合 憨憨（我方） 主怪兽区3 ← 「珠淚哀歌 雪蓮」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 3 回合 憨憨（我方） 主怪兽区2 ← 「珠淚哀歌 梅露」"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_ended"),
    ])
    idle = [r for r in rows if r["category"] == "整局无动作"]
    assert len(idle) == 1, rows
    assert "二憨" in idle[0]["evidence"], idle[0]
    assert "一件卡都没落到场上" in idle[0]["evidence"], idle[0]


def test_short_duel_without_placements_is_not_flagged() -> None:
    """2 回合就结束的局不判「整局无动作」（可能是正常速杀/投降，不打扰）。"""

    rows = _run([
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 二憨$aN9sW"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 2 回合 憨憨（我方） 主怪兽区3 ← 「珠淚哀歌 雪蓮」"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_ended"),
    ])
    assert [r["category"] for r in rows] == ["对局摘要"], rows


def test_identical_event_is_recorded_once_even_in_two_files() -> None:
    """**同一条事件在日志里会出现多份**（实测：同一秒的 `duel_ended` 散在 2~3 个 app_*.log.jsonl 里，
    同一文件里也可能重复）⇒ 按「时间戳 + 正文」去重：一局只能算一局、一条问题只记一次。"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        bad = _line("", "", '[对局进程:err] "CallCardFunction"(c60811211.initial_effect): attempt to call an error function')
        started = _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started")
        ended = _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_ended")
        (tmp_path / "app_a.log.jsonl").write_text("\n".join([started, bad, ended]) + "\n", encoding="utf-8")
        (tmp_path / "app_b.log.jsonl").write_text("\n".join([started, bad, ended]) + "\n", encoding="utf-8")
        state = WATCH.WatchState(tmp_path / "state.json")
        findings = tmp_path / "f.jsonl"
        watcher = WATCH.Watcher(tmp_path, state, findings, tmp_path / "r.md")
        watcher.poll(now=4000.0)
        rows = [json.loads(r) for r in findings.read_text(encoding="utf-8").splitlines()]
    assert [r["category"] for r in rows] == ["内核脚本报错", "对局摘要"], rows
    assert state.duels == 1, state.duels


def test_lobby_name_and_seat_label_are_one_person() -> None:
    """名册里的「未报名」和落位里的「座位0」是同一个人 ⇒ **不能**报「整局无动作」。

    实测背景（2026-10-07）：群友客户端用默认名进房（日志里是「未报名」），记录器在他那一侧拿不到
    握手昵称、只能标成「座位N」；按名字硬比会把同一个人算成"没出牌的第二个人"，连报两局误报。
    """

    rows = _run([
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：机器人 憨憨"),
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 未报名"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 1 回合 座位0（对方） 主怪兽区4 ← 「威光魔人」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 1 回合 憨憨（我方） 主怪兽区3 ← 「漫画猫」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 3 回合 座位0（对方） 主怪兽区2 ← 「瞳之魔女 梦根娜」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 3 回合 憨憨（我方） 主怪兽区3 ← 「漫画猫」"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_ended"),
    ])
    assert [r["category"] for r in rows] == ["对局摘要"], rows


def test_renamed_player_between_rooms_is_not_idle() -> None:
    """同一个人换了昵称（名册里两个玩家名，落位里是另一个游戏内名）⇒ 两侧都落过牌，不该报。

    实测背景（2026-10-07 第二轮）：名册里同时有「嘻嘻$aN9sW」和「库里波是最强的！」，落位里是
    「嘻嘻$aN9sW」与「憨憨」——一局只有两侧，两侧都落过牌 ⇔ 没人"整局无动作"。
    """

    rows = _run([
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：机器人 憨憨"),
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 嘻嘻$aN9sW"),
        _line("plugin.yugioh.duel-arena", "info", "有客户端进入房间：玩家 库里波是最强的！"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_started"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 1 回合 嘻嘻$aN9sW（对方） 主怪兽区3 ← 「威光魔人」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 1 回合 憨憨（我方） 主怪兽区3 ← 「漫画猫」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 3 回合 嘻嘻$aN9sW（对方） 主怪兽区2 ← 「No.59 背反之料理人」"),
        _line("plugin.yugioh.duel-arena", "info", "落位：第 3 回合 憨憨（我方） 主怪兽区4 ← 「闪刀姬-零衣」"),
        _line("plugin.yugioh.duel-arena", "info", "对局阶段变化：duel_ended"),
    ])
    assert [r["category"] for r in rows] == ["对局摘要"], rows


def test_identical_line_twice_is_deduped_but_changed_line_is_new() -> None:
    """完全一样的重复行只记一次；正文变了（真新增）才再记。"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log = tmp_path / "app_test.log.jsonl"
        bad = _line("", "", '[对局进程:err] "CallCardFunction"(c60811211.initial_effect): attempt to call an error function')
        log.write_text(bad + "\n", encoding="utf-8")
        state = WATCH.WatchState(tmp_path / "state.json")
        findings = tmp_path / "f.jsonl"
        watcher = WATCH.Watcher(tmp_path, state, findings, tmp_path / "r.md")
        watcher.poll(now=3000.0)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(bad + "\n")                   # 完全一样：重复行，不该再记
        watcher.poll(now=3001.0)
        assert len(findings.read_text(encoding="utf-8").splitlines()) == 1
        with log.open("a", encoding="utf-8") as handle:
            handle.write(bad.replace("c60811211", "c7428507") + "\n")   # 正文变了：新的一条
        watcher.poll(now=3002.0)
        assert len(findings.read_text(encoding="utf-8").splitlines()) == 2


def test_gate_failure_recorded() -> None:
    """闸门/收摊报错要记（high 之外的中等级别，便于和脚本报错区分）。"""

    rows = _run([
        _line("plugin.yugioh.duel-arena", "info", "收摊失败：端口仍被占用"),
        _line("plugin.yugioh.duel-arena", "warning", "闸门启动失败：端口 7911 被占用"),
    ])
    assert [r["category"] for r in rows] == ["闸门/房间异常", "插件警告"], rows


def _run_all() -> int:
    """不装 pytest 时的自跑入口（与其它测试文件一致）。"""

    tests: List = [
        test_kernel_script_error_is_recorded_with_card_ids,
        test_plugin_error_and_warning_noise,
        test_player_joined_but_no_duel_started,
        test_join_then_started_is_clean,
        test_duel_stuck_open_is_reported_once,
        test_idle_side_is_flagged_from_placements,
        test_short_duel_without_placements_is_not_flagged,
        test_identical_event_is_recorded_once_even_in_two_files,
        test_lobby_name_and_seat_label_are_one_person,
        test_renamed_player_between_rooms_is_not_idle,
        test_identical_line_twice_is_deduped_but_changed_line_is_new,
        test_gate_failure_recorded,
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
