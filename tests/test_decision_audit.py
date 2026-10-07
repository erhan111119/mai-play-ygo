"""``tools/decision_audit.py`` 的回归测试：**七条规则的判据**。

这个工具的价值全在"报出来的那几条是不是真的"——判据写松了会刷屏（人就不再看了），
写紧了会漏掉真问题。下面把每条规则钉在一个最小合成日志上：
手坑空放（我方回合 / 对面 0 动作）、大怪干站（攒两个回合才报）、本家被弃、额外卡组出场、选项登记没命中、
整局 0 额外召唤、我方回合动作 ≤1。

直接用 ``python tests/test_decision_audit.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

import importlib.util
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

#: 假卡组：用来让"按卡名过滤"认得出来
OUR_CARDS = ["本家怪", "大怪", "鲜花女男爵"]


def load_tool(stem: str):
    """按文件路径加载 ``tools/`` 下的脚本（它们不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(f"tools_{stem}", _PLUGIN_ROOT / "tools" / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # 先进注册表：模块里用了 @dataclass
    spec.loader.exec_module(module)
    return module


AUDIT = load_tool("decision_audit")


def _label(label: str, body: str) -> str:
    """给正文套上日志前缀（真实日志是 ``INFO [我方] [26-10-06 16:06:05] …``）。"""

    return f"INFO [{label}] [26-10-06 16:06:05] {body}".rstrip()


def _stream(lines: Sequence[str]):
    """按工具的切分口径取出唯一的一条流（测试里只放一条）。"""

    streams = AUDIT._load_accept()._streams(list(lines))
    assert len(streams) == 1, f"应该只有一条流，实得 {len(streams)}"
    return streams[0]


def _audit(lines: Sequence[str], attacks: Dict[str, int] | None = None, seat: int = 0,
           limit: int = 12, our_cards: Sequence[str] = OUR_CARDS):
    stream = _stream(lines)
    return AUDIT.audit_stream(stream, seat, list(our_cards), attacks or {}, limit, game_no=1)


def _findings(lines: Sequence[str], **kwargs) -> List[str]:
    """只要异常清单（其余三项见 :func:`_audit`）。"""

    return _audit(lines, **kwargs)[0]


def test_hand_trap_on_our_turn_is_flagged() -> None:
    """我方回合交反应型手坑＝空放（对手的动作发生在它自己的回合）。"""

    lines = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 增殖的G activate effect from Hand)"),
        _label("我方", "(0 's 本家怪 from Hand move to MonsterZone)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
    ]
    findings = _findings(lines)
    assert any("手坑空放" in line and "增殖的G" in line for line in findings), findings


def test_preemptive_hand_trap_early_on_enemy_turn_is_not_flagged() -> None:
    """主动型手坑（G/欢聚友伴）在**对手回合**里早交是正常时机，不算空放。

    实测教训（2026-10-07）：原先这条规则把"对手这一回合 0 动作时交的 G"一律报成空放，
    于是对空白墙的每一局都在报——而 G 与欢聚友伴的收益是"发动**之后**对手每次特召/召唤抽 1"，
    对手回合里越早交抽得越多（A/B 实测：早交 21 抽 / 等召唤再交 8 抽，18 局）。
    """

    # 对手回合、对手还没动作就交 G → 不报
    enemy_turn = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(1 's 某魔陷 activate effect from SpellZone)"),
        _label("我方", "(0 's 增殖的G activate effect from Hand)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(enemy_turn)
    assert not [line for line in findings if "手坑空放" in line], findings
    # 但**自己回合**交主动型手坑照样报（对面多半不会在我们回合特召）
    # ⚠ 回合玩家是从 "(N draw 1 card)" 认出来的：第 1 回合不抽牌，所以要给第 2 回合才能反推出第 1 回合是谁的
    own_turn = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 增殖的G activate effect from Hand)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(own_turn)
    assert any("手坑空放" in line and "主动型" in line for line in findings), findings


def test_big_monster_idle_needs_two_turns() -> None:
    """≥2000 打点的怪在场、我方回合没进战阶：**攒够 2 个回合**才报（1 个回合不刷屏）。"""

    # 只 1 个我方回合 → 不报
    one_turn = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 大怪 from Hand move to MonsterZone)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
    ]
    findings = _findings(one_turn, attacks={"大怪": 2500})
    # ⚠ 只断言"大怪干站"这条不报：这段最小日志本来就没有额外召唤、我方回合也只有 1 个动作，
    # 会被另外两条规则（整局 0 额外召唤 / 我方回合动作 ≤1）命中——它们正是为此而生。
    assert not [line for line in findings if "大怪干站" in line], findings

    # 两个我方回合都没进战阶 → 报一次（并点名那只怪）
    two_turns = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 大怪 from Hand move to MonsterZone)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(two_turns, attacks={"大怪": 2500})
    assert len([line for line in findings if "大怪干站" in line]) == 1, findings
    assert any("大怪" in line and "2 个我方回合" in line for line in findings), findings

    # 进了战阶就不报
    with_battle = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 大怪 from Hand move to MonsterZone)"),
        _label("我方", "(Go to BattleStart)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(with_battle, attacks={"大怪": 2500})
    assert not [line for line in findings if "大怪干站" in line], findings


def test_extra_deck_entry_and_discard_are_counted() -> None:
    """额外卡组入场次数与"本家被弃"各记一笔。"""

    lines = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 鲜花女男爵 from Extra move to MonsterZone)"),
        _label("我方", "(0 's 本家怪 from Hand move to Grave)"),
        _label("我方", "(0 's 本家怪 overlay 大怪)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
    ]
    _, extra, resource, _timing = _audit(lines, our_cards=["本家怪", "大怪", "鲜花女男爵"])
    assert extra.get("鲜花女男爵") == 1, extra
    assert resource.get("本家被弃（手牌→墓地）") == 1, resource
    assert resource.get("本家被当素材（overlay）") == 1, resource


def test_probe_miss_and_hit_are_split() -> None:
    """``[探针]`` 行：按值命中与"想要不在选项表"分开数；**单选项的题不算落空**。"""

    lines = [
        _label("我方", "[探针] 选项：正在结算=某卡(12345678) options=[197530848,197530849]"
                      " 想要=197530849 命中=1（按值）"),
        # 真落空：内核给了 2 个选项，我们要的那个都不在 → 这次登记写错了时机
        _label("我方", "[探针] 选项：正在结算=某卡(12345678) options=[197530848,197530850]"
                      " 想要=197530849 命中=0（基类随机）"),
        # 单选项：没得选，不算落空（实测升辉月那一批 32 次全是这一类）
        _label("我方", "[探针] 选项：正在结算=另一张卡(87654321) options=[1402469136]"
                      " 想要=1402469137 命中=0（基类随机）"),
    ]
    asked, by_value, missed, miss_by_card = AUDIT.scan_probe(lines)
    assert (asked, by_value, missed) == (3, 1, 1)
    assert miss_by_card == {"某卡(12345678)": 1}, miss_by_card


def test_only_big_monsters_count_as_idle() -> None:
    """打点低于门槛的怪站着不打不算问题（这副牌的下级本来就该守备）。"""

    lines = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 本家怪 from Hand move to MonsterZone)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(lines, attacks={"本家怪": 700})
    assert not [line for line in findings if "大怪干站" in line], findings


def test_zero_extra_summon_and_thin_turn_are_flagged() -> None:
    """两条新规则（真人局 P1 的量具）：整局 0 额外召唤、我方回合动作 ≤1。

    这两条是"输了但看不出为什么没做出来"那条反馈的抓手：先用它们把"根本没展开"的局与
    "那一回合几乎没动"的回合挑出来，再看起手（``Bot Hand`` 段）判卡手还是脚本问题。
    """

    # 一整局只通召一次、没有额外卡组怪 → 两条都该报
    thin = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 本家怪 from Hand move to MonsterZone)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(thin)
    assert any("整局 0 额外召唤" in line for line in findings), findings
    assert any("我方回合动作 ≤1" in line and "第1回合 1 个" in line for line in findings), findings

    # 有额外卡组怪上过场、我方每回合都有 3 个动作 → 两条都不报
    normal = [
        _label("我方", "WindBot starting..."),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(0 's 本家怪 from Hand move to MonsterZone)"),
        _label("我方", "(0 's 鲜花女男爵 from Extra move to MonsterZone)"),
        _label("我方", "(0 's 本家怪 activate effect from MonsterZone)"),
        _label("我方", "(Go to End)"),
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings = _findings(normal)
    assert not [line for line in findings if "整局 0 额外召唤" in line], findings
    assert not [line for line in findings if "我方回合动作 ≤1" in line], findings


def test_mulcharmy_timing_and_extra_draws_are_counted() -> None:
    """「欢聚友伴」的兑现指标：**对手召唤后交** / 提前交，以及对手回合里我方抽到的卡。

    这是 P2 那两条闸门口的 A/B 指标（口径见模块 docstring 第 8 条）：收紧闸门会让"提前交"变成 0，
    代价是少掉"先交先抽"的那几张——所以要同时看"对手回合里我方抽卡"这个收益读数。
    """

    lines = [
        _label("我方", "WindBot starting..."),
        # 对手回合：对手先发动一张魔陷（还没出怪）→ 我方提前交水母（旧口径允许、新口径会拦）
        _label("我方", "(Go to Draw)"),
        _label("我方", "(1 draw 1 card)"),
        _label("我方", "(1 's 某魔陷 activate effect from SpellZone)"),
        _label("我方", "(0 's 欢聚友伴·抖抖海月水母 activate effect from Hand)"),
        _label("我方", "(0 's 欢聚友伴·抖抖海月水母 from Hand move to Grave)"),
        # 对手召唤 → 我方山雀在"召唤之后"交（新口径允许）
        _label("我方", "(1 's 某怪 from Hand move to MonsterZone)"),
        _label("我方", "(0 's 欢聚友伴·茸茸长尾山雀 activate effect from Hand)"),
        _label("我方", "(0 's 欢聚友伴·茸茸长尾山雀 from Hand move to Grave)"),
        # 对手回合里我方抽到的卡（＝这两张的收益）
        _label("我方", "(0 draw 1 card)"),
        _label("我方", "(0 draw 1 card)"),
        _label("我方", "(Go to End)"),
    ]
    findings, _extra, _res, timing = _audit(lines)
    assert timing["欢聚友伴·提前交"] == 1, timing
    assert timing["欢聚友伴·对手召唤后交"] == 1, timing
    assert timing["对手回合里我方抽卡"] == 2, timing
    assert any("欢聚友伴提前交" in line for line in findings), findings


def _run_all() -> int:
    """不装 pytest 也能跑（和 ``tests/test_plan_accept.py`` 一样）。"""

    tests: List = [
        test_hand_trap_on_our_turn_is_flagged,
        test_preemptive_hand_trap_early_on_enemy_turn_is_not_flagged,
        test_big_monster_idle_needs_two_turns,
        test_extra_deck_entry_and_discard_are_counted,
        test_probe_miss_and_hit_are_split,
        test_only_big_monsters_count_as_idle,
        test_zero_extra_summon_and_thin_turn_are_flagged,
        test_mulcharmy_timing_and_extra_draws_are_counted,
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
