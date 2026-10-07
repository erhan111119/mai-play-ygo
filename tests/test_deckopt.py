"""卡组进化搜索的纯逻辑测试（不跑对局）。

护栏意义：搜索会跑几百上千局，出问题时很难从"结果"反推原因；所以"判定口径、换牌约束、
上线前体检、牌序排期"这些必须在这里钉死——
实测教训都在：40 局的 62% 只是 1.6σ（不能当"变强"）、共用牌序会让卡组背开局、
换进来的卡本机不认得会让候选"假赢"。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import random
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from train.deckopt import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    SwapProposal,
    accept_candidate,
    apply_swap,
    deck_issues,
    missing_scripts,
    propose_swaps,
    prune_for_dead_cards,
    rounds_needed,
    seed_schedule,
    wilson_interval,
)


def test_accept_requires_interval_not_percentage() -> None:
    """接受判定必须看区间：40 局的 62% 不能算"变强"。"""

    # 40 局 25 胜（62.5%）vs 基线 40 局 15 胜（37.5%）：区间不重叠才接受
    # 100 局里 40 胜 vs 60 胜：区间不重叠（40 局的差距证明不了，见下面的用例）
    ok, reason = accept_candidate(40, 100, 60, 100)
    assert ok is True, reason
    # 40 局 20:20 vs 19:21 这种差别必须拒绝
    ok, reason = accept_candidate(19, 40, 21, 40)
    assert ok is False and "分不开" in reason, reason
    # 零局不能判定
    assert accept_candidate(0, 0, 5, 10)[0] is False


def test_wilson_and_sample_size() -> None:
    """区间不越界；判定 5% 差别要比 3% 少一半局数。"""

    low, high = wilson_interval(55, 100)
    assert 0.44 < low < 0.46 and 0.64 < high < 0.66, (low, high)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert rounds_needed(0.05) < rounds_needed(0.03)
    assert rounds_needed(0.0) == 0


def test_seed_schedule_is_distinct_and_stable() -> None:
    """每个候选拿到一套互不重叠的牌序种子（防"背开局"），且同样输入同样结果。"""

    first = seed_schedule(100, 0, 3)
    second = seed_schedule(100, 1, 3)
    assert first == [100, 101, 102]
    assert set(first) & set(second) == set(), "不同候选不能共用牌序"
    assert seed_schedule(100, 0, 3) == first, "同输入要稳定（可复现）"


def test_deck_issues_catches_real_hazards() -> None:
    """体检要抓出：缺卡、缺脚本、同名超限、张数不足。"""

    deck = [1, 1, 1, 2, 2, 3]
    issues = deck_issues(deck, known_cards=[1, 2, 3], scripted_cards=[1, 2, 3], min_main=4)
    assert issues == [], issues
    assert any("卡库里没有" in item for item in deck_issues(deck, known_cards=[1, 2], scripted_cards=[1, 2], min_main=4))
    assert any("没有卡片脚本" in item for item in deck_issues(deck, known_cards=[1, 2, 3], scripted_cards=[1, 3], min_main=4))
    assert any("同名超过" in item for item in deck_issues([5, 5, 5, 5], known_cards=[5], scripted_cards=[5], min_main=1))
    assert any("少于" in item for item in deck_issues([1], known_cards=[1], scripted_cards=[1], min_main=40))


def test_missing_scripts_is_a_warning_for_subject_but_fatal_for_candidates() -> None:
    """缺脚本的卡：候选卡表一律毙掉，主体卡组只警告。

    护栏（实测踩过）：主体卡组里只要有一张查不到脚本的卡，工具就整份拒跑、秒退，
    群里只看到一句"没能跑完"——牌子是群友投的，几张白板不该让整副牌失去被优化的机会；
    但候选**换进来**的新卡必须是完整的，否则"候选变强"只是没打起来。
    """

    deck = [1, 2, 3]
    assert missing_scripts(deck, known_cards=deck, scripted_cards=[1]) == [2, 3]
    # 卡库里都没有的卡不算"缺脚本"，那是另一条（缺卡）问题
    assert missing_scripts([9], known_cards=[1], scripted_cards=[1]) == []

    assert any(
        "没有卡片脚本" in item
        for item in deck_issues(deck, known_cards=deck, scripted_cards=[1], min_main=1)
    )
    assert deck_issues(
        deck, known_cards=deck, scripted_cards=[1], min_main=1, skip_script_check=True
    ) == []
    # 跳过脚本检查不等于放弃其它检查
    still = deck_issues(
        [9], known_cards=deck, scripted_cards=[1], min_main=1, skip_script_check=True
    )
    assert any("卡库里没有" in item for item in still), still


def test_propose_and_apply_swaps() -> None:
    """换牌：优先换掉没用过的卡、张数守恒、不超同名上限、可复现。"""

    deck = [1, 1, 1, 2, 2, 3, 3, 4]
    proposals = propose_swaps(
        deck,
        unused=[3],
        candidates=[9, 8, 7],
        count=3,
        rng=random.Random(20260921),
        swap_size=1,
    )
    assert len(proposals) == 3, proposals
    assert all(3 in item.remove for item in proposals), "没用过的卡应优先被换出"
    assert all(card in (7, 8, 9) for item in proposals for card in item.add)

    updated = apply_swap(deck, SwapProposal(remove=(3,), add=(9,)))
    assert len(updated) == len(deck), "换牌不该改变张数"
    # 牌组里原本有两张 3：换出只少一张，换进的补上
    assert updated.count(3) == deck.count(3) - 1 and updated.count(9) == 1, updated

    # 同名上限：卡组里已经有 3 张 9 时不能再换进第 4 张
    capped = apply_swap([9, 9, 9, 1], SwapProposal(remove=(1,), add=(9,)), max_copies=3)
    assert capped.count(9) == 3 and 1 not in capped, capped

    # 同种子可复现
    again = propose_swaps(
        deck, unused=[3], candidates=[9, 8, 7], count=3, rng=random.Random(20260921), swap_size=1
    )
    assert [item.remove for item in again] == [item.remove for item in proposals]


def test_propose_swaps_without_pool_or_unused() -> None:
    """候选池为空时不给建议；没有"没用过的卡"时要退化成"换掉占比最高的卡"。"""

    assert propose_swaps([1, 2], unused=[], candidates=[], count=2, rng=random.Random(1)) == []
    fallback = propose_swaps([1, 1, 1, 2], unused=[], candidates=[9], count=1, rng=random.Random(1))
    assert fallback and fallback[0].add == (9,), fallback
    assert fallback[0].reason == "换掉占比最高的卡"


def test_prune_for_dead_cards_needs_evidence() -> None:
    """死牌清理：证据够了才动，且只换"整局没被用过"的卡。

    护栏：这条规则**不靠胜率证明**（几十局证明不了 5% 的差别），全靠"最近 N 局一次没动"
    这个直接观察——所以样本量下限必须真的生效，否则几局的巧合就能改掉一副牌。
    """

    deck = [1, 1, 1, 2, 3]
    assert prune_for_dead_cards(deck, [1], [9, 8], games=5, min_games=20) is None, "证据不足不该动"
    assert prune_for_dead_cards(deck, [], [9], games=99, min_games=20) is None, "没有死牌不该动"
    assert prune_for_dead_cards(deck, [1], [], games=99, min_games=20) is None, "没卡可换不该动"
    assert prune_for_dead_cards(deck, [2], [9], games=5) is None

    proposal = prune_for_dead_cards(deck, [1], [9, 8], games=40, min_games=20)
    assert proposal is not None
    # 换入池只有 2 张，所以最多只换 2 张（不能把卡组换少）
    assert proposal.remove == (1, 1), proposal
    assert proposal.add == (9, 8), proposal
    assert "死牌" in proposal.reason, proposal.reason
    assert len(apply_swap(deck, proposal)) == len(deck), "换牌要保持张数"

    # 死牌里挑占用卡位最多的那张（同名 3 张 vs 1 张）
    most = prune_for_dead_cards([4, 4, 4, 5], [5, 4], [9, 8, 7], games=40, min_games=20)
    assert most is not None and most.remove == (4, 4, 4), most
    assert most.add == (9, 8, 7), most


def test_preferred_cards_are_swapped_in_first() -> None:
    """换入**优先**挑"最近对局里真的会被打出来"的牌。

    护栏（实测踩过）：把一张从没被打出来的牌换成另一张同样从没被打出来的牌，结果是牌表动了、
    机器人打出来的东西一点没变——用户看到两副"改"过的牌与原牌"从根本上一样"，就是这么来的
    （被清掉的「灵王的波动」在 4090 局里只出场 2 次，换进来的 4 张 @火灵天星出场 0 次）。
    """

    # 死牌清理：池子里有"会被打出来"的卡（7）时，必须挑它，而不是按 id 顺序挑 8
    proposal = prune_for_dead_cards(
        [1, 1, 2], [1], [8, 7], games=40, min_games=20, preferred=[7]
    )
    assert proposal is not None and proposal.add[0] == 7, proposal
    assert "真会出场" in proposal.reason, proposal.reason

    # 候选生成：换入的卡也优先取偏好列表里的
    swaps = propose_swaps(
        [1, 1, 2, 3],
        unused=[3],
        candidates=[9, 8, 7],
        count=1,
        rng=random.Random(1),
        swap_size=1,
        preferred=[7],
    )
    assert swaps and swaps[0].add == (7,), swaps

    # 偏好为空时保持原来的行为（池子顺序不变）
    plain = propose_swaps(
        [1, 1, 2, 3], unused=[3], candidates=[9, 8, 7], count=1, rng=random.Random(1), swap_size=1
    )
    assert plain and plain[0].add == (9,), plain


def test_deck_legality_matches_windbot_loader() -> None:
    """按 WindBot 的加载规则判合法性：额外怪兽混进主卡组、额外超 15 张都要拦下。

    护栏（实测踩过）：优化器把「炎凤凰@火灵天星」这种**连接怪兽**换进了主卡组。WindBot 是按
    **卡的类型**统计主/额外的（不是 .ydk 的区段），于是额外变成 16 张 ⇒ ``Deck.Load`` 返回
    null ⇒ 进房时 ``Deck.Cards`` 抛空引用 ⇒ 机器人交不出卡组、房间一直显示"不准备"。
    它不报错、不写日志，所以只能在保存之前自己核一遍。
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "optimize_deck_tool", str(_PLUGIN_ROOT / "tools" / "optimize_deck.py")
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    extra = [900000001, 900000002]
    main = [100000001] * 40
    assert tool.deck_legality(main, [900000001] * 15, extra_cards=extra) == []

    stray = tool.deck_legality(main[:39] + [900000002], [900000001] * 15, extra_cards=extra)
    assert any("额外卡组的卡" in item for item in stray), stray
    assert any("16 张" in item for item in stray), stray

    too_many = tool.deck_legality(main, [900000001] * 15 + [900000002], extra_cards=extra)
    assert any("额外卡组 16 张" in item for item in too_many), too_many

    # 换入池必须排除额外类：主卡组换进一张额外怪兽就是上面那个坑
    cdb = Path(r"D:\Game\ygopro-duel\ygopro\cards.cdb")
    if cdb.is_file():
        import sqlite3

        connection = sqlite3.connect(f"file:{cdb.as_posix()}?mode=ro", uri=True)
        try:
            deck = [row[0] for row in connection.execute("SELECT id FROM datas LIMIT 40")]
            pool = tool.swap_pool(cdb, deck)
            extra_ids = set(tool.extra_card_ids(cdb))
        finally:
            connection.close()
        assert pool, "这套卡表应该能挑出同系列候选"
        assert all(card not in extra_ids for card in pool), "额外卡组的卡不该出现在换入池里"


def test_reported_number_is_the_list_position() -> None:
    """报给群里的"编号"必须是 ``/卡组列表`` 里的位置号，不是数据库 ID。

    护栏（实测踩过）：优化器报的是 ``StoredDeck.deck_id``（数据库自增 ID），而群里的指令按
    **位置**解析（``plugin._deck_by_argument`` 取 ``decks[index-1]``）。池子里有内置卡组打底时
    两者不相等——把"编号 83"报给了用户，而列表里那副牌是第 73 位，照报的数字去敲指令会操作
    到另一副牌。
    """

    import importlib.util
    import tempfile

    from duel.deckcode import Deck
    from duel.deckpool import DeckPool

    spec = importlib.util.spec_from_file_location(
        "optimize_deck_tool", str(_PLUGIN_ROOT / "tools" / "optimize_deck.py")
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            # 先塞两副"内置"卡组垫底，制造"数据库 ID ≠ 位置"的局面
            for name in ("内置甲", "内置乙"):
                pool.add(
                    group_id="__builtin__",
                    display_name=name,
                    contributor_id="",
                    contributor_name="WindBot 自带",
                    ydk_text=Deck((89631139,), (1,), ()).to_ydk(name),
                    deck_code="",
                    source_format="builtin",
                    main_count=1,
                    extra_count=1,
                    side_count=0,
                )
            added = pool.add(
                group_id="__builtin__",
                display_name="优化产物",
                contributor_id="",
                contributor_name="自动优化",
                ydk_text=Deck((89631139,), (1,), ()).to_ydk("优化产物"),
                deck_code="",
                source_format="auto-optimize",
                main_count=1,
                extra_count=1,
                side_count=0,
            )
            # 位置按列表顺序算：这里内置那两副在前，所以新卡组在最后
            decks = pool.list_decks("111")
            expected = next(
                index for index, deck in enumerate(decks, start=1) if deck.deck_id == added.deck_id
            )
            reported = tool.list_position(pool, "111", added.deck_id)
            assert reported == expected, (reported, expected)
            assert decks[reported - 1].deck_id == added.deck_id, "按报出的编号要能取回同一副牌"
            assert tool.list_position(pool, "111", 999999) == 0, "找不到时返回 0，不瞎指一副"
        finally:
            pool.close()


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
