"""卡组码解析模块的单元测试。

直接用 ``python tests/test_deckcode.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

import base64
import struct
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.deckcode import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    DUEL_MAIN_MAX,
    FORMAT_OURYGO,
    FORMAT_RAW,
    FORMAT_YDK,
    Deck,
    DeckCodeError,
    describe_issues,
    describe_room_limits,
    detect_format,
    parse_deck_code,
)


# 一副用于测试的 40 张主卡组 + 5 张额外 + 3 张副卡组
SAMPLE_MAIN: List[int] = [89631139, 89631139, 89631139, 46986414, 46986414, 46986414] + [
    100000000 + index for index in range(34)
]
SAMPLE_EXTRA: List[int] = [100200001, 100200002, 100200002, 100200003, 100200004]
SAMPLE_SIDE: List[int] = [100300001, 100300002, 100300002]


def _encode_ourygo(main: Sequence[int], extra: Sequence[int], side: Sequence[int]) -> str:
    """按萌卡位流格式独立实现一份编码器，用来验证解析器的正确性。"""

    bits: List[int] = []

    def push(value: int, width: int) -> None:
        """把 value 按高位在前写入 width 位。"""

        bits.extend((value >> (width - 1 - offset)) & 1 for offset in range(width))

    def kinds(cards: Sequence[int]) -> List[int]:
        """按出现顺序取出去重后的卡 ID。"""

        return list(dict.fromkeys(cards))

    def counts(cards: Sequence[int]) -> Dict[int, int]:
        """统计每张卡的张数。"""

        result: Dict[int, int] = {}
        for card_id in cards:
            result[card_id] = result.get(card_id, 0) + 1
        return result

    push(len(kinds(main)), 8)
    push(len(kinds(extra)), 4)
    push(len(kinds(side)), 4)
    for cards in (main, extra, side):
        counter = counts(cards)
        for card_id in kinds(cards):
            push(counter[card_id], 2)
            push(card_id, 27)

    while len(bits) % 8:
        bits.append(0)
    payload = bytearray()
    for index in range(0, len(bits), 8):
        byte = 0
        for bit in bits[index : index + 8]:
            byte = (byte << 1) | bit
        payload.append(byte)
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


def _encode_raw(main: Sequence[int], extra: Sequence[int], side: Sequence[int]) -> str:
    """独立实现裸卡组码编码器。"""

    buffer = bytearray()
    for deck_type, cards in ((0, main), (1, extra), (2, side)):
        counter: Dict[int, int] = {}
        for card_id in cards:
            counter[card_id] = counter.get(card_id, 0) + 1
        for card_id, count in counter.items():
            buffer.extend(struct.pack("<I", card_id | (deck_type << 28) | ((count - 1) << 30)))
    return base64.urlsafe_b64encode(bytes(buffer)).decode("ascii").rstrip("=")


def test_format_detection() -> None:
    """三种格式的外形判断应当互不干扰。"""

    assert detect_format("ydke://abc!def!") == "ydke"
    assert detect_format("http://deck.ourygo.top?ygotype=deck&v=1&d=abc") == FORMAT_OURYGO
    assert detect_format("#main\n89631139\n#extra\n!side") == FORMAT_YDK


def test_ydk_roundtrip() -> None:
    """自己写出的 .ydk 应当能被自己解析回同一副卡组。"""

    deck = Deck(tuple(SAMPLE_MAIN), tuple(SAMPLE_EXTRA), tuple(SAMPLE_SIDE))
    parsed = parse_deck_code(deck.to_ydk("测试卡组"))
    assert parsed.main == deck.main, parsed.main
    assert parsed.extra == deck.extra, parsed.extra
    assert parsed.side == deck.side, parsed.side


def test_raw_deck_code_roundtrip() -> None:
    """裸卡组码应当能反向编码再解析回同一副卡组。"""

    deck = Deck(tuple(SAMPLE_MAIN), tuple(SAMPLE_EXTRA), tuple(SAMPLE_SIDE))
    encoded = deck.to_deck_code()
    parsed = parse_deck_code(encoded)
    assert parsed.main == deck.main
    assert parsed.extra == deck.extra
    assert parsed.side == deck.side
    # 典型的真实卡组不应被误判成萌卡格式
    assert parsed.source_format == FORMAT_RAW, parsed.source_format
    assert parsed.ambiguous is False


def test_raw_deck_code_matches_independent_encoder() -> None:
    """与独立实现的编码器产出一致。"""

    expected = _encode_raw(SAMPLE_MAIN, SAMPLE_EXTRA, SAMPLE_SIDE)
    deck = Deck(tuple(SAMPLE_MAIN), tuple(SAMPLE_EXTRA), tuple(SAMPLE_SIDE))
    assert deck.to_deck_code() == expected


def test_ourygo_link_parse() -> None:
    """萌卡分享链接应当被正确解析，且不会误判成裸卡组码。"""

    payload = _encode_ourygo(SAMPLE_MAIN, SAMPLE_EXTRA, SAMPLE_SIDE)
    url = f"http://deck.ourygo.top?name=测试&ygotype=deck&v=1&d={payload}"
    parsed = parse_deck_code(url)
    assert parsed.main == tuple(SAMPLE_MAIN)
    assert parsed.extra == tuple(SAMPLE_EXTRA)
    assert parsed.side == tuple(SAMPLE_SIDE)
    assert parsed.source_format == FORMAT_OURYGO
    assert parsed.ambiguous is False


def test_ydke_parse() -> None:
    """ydke:// 链接的三段应当分别落到主/额外/副卡组。"""

    def encode_segment(cards: Sequence[int]) -> str:
        """把卡 ID 序列编码成一段标准 base64。"""

        raw = b"".join(struct.pack("<I", card_id) for card_id in cards)
        return base64.b64encode(raw).decode("ascii")

    url = "ydke://{}!{}!{}!".format(
        encode_segment(SAMPLE_MAIN),
        encode_segment(SAMPLE_EXTRA),
        encode_segment(SAMPLE_SIDE),
    )
    parsed = parse_deck_code(url)
    assert parsed.main == tuple(SAMPLE_MAIN)
    assert parsed.extra == tuple(SAMPLE_EXTRA)
    assert parsed.side == tuple(SAMPLE_SIDE)


def test_ydke_allows_empty_segment() -> None:
    """额外卡组为空时该段允许留空。"""

    main_segment = base64.b64encode(
        b"".join(struct.pack("<I", card_id) for card_id in SAMPLE_MAIN)
    ).decode("ascii")
    parsed = parse_deck_code(f"ydke://{main_segment}!!")
    assert parsed.main == tuple(SAMPLE_MAIN)
    assert parsed.extra == ()
    assert parsed.side == ()


def test_invalid_input_raises() -> None:
    """明显不是卡组码的文本应当抛出可读的错误。"""

    for bad in ("", "   ", "今天天气不错", "not-a-deck!"):
        try:
            parse_deck_code(bad)
        except DeckCodeError:
            continue
        raise AssertionError(f"{bad!r} 本应解析失败")


def test_describe_issues_reports_limits() -> None:
    """超限的卡组必须被拦下，因为 WindBot 会静默丢弃它。"""

    undersized_main = Deck(tuple(SAMPLE_MAIN[:39]), tuple(SAMPLE_EXTRA), ())
    assert any("主卡组" in issue for issue in describe_issues(undersized_main))

    oversized_main = Deck(tuple(SAMPLE_MAIN) + tuple(range(999999000, 999999021)), (), ())
    assert any("主卡组" in issue for issue in describe_issues(oversized_main))

    oversized_extra = Deck(tuple(SAMPLE_MAIN), tuple(range(100200001, 100200017)), ())
    assert any("额外卡组" in issue for issue in describe_issues(oversized_extra))

    too_many_copies = Deck((89631139,) * 4 + tuple(SAMPLE_MAIN), (), ())
    assert any("超过 3 张" in issue for issue in describe_issues(too_many_copies))

    assert describe_issues(Deck(tuple(SAMPLE_MAIN), tuple(SAMPLE_EXTRA), tuple(SAMPLE_SIDE))) == []


def test_room_limits_boundary() -> None:
    """开房前的硬限制：主卡组 60 张是边界（实测 >60 时对局只放 60 张进卡组）。

    边界写成测试是为了它被改动时立刻有反馈：拦松了会开出一个"多出来的牌白带"或"bot 进不去"
    的房间，拦紧了会把能打的娱乐牌挡在门外（同名张数**不在**这里的规则里——"赖皮卡组"
    那种 5 张同名是要能打的）。
    """

    at_limit = Deck(tuple(range(100000000, 100000000 + DUEL_MAIN_MAX)), (), ())
    assert describe_room_limits(at_limit) is None

    over_limit = Deck(tuple(range(100000000, 100000000 + DUEL_MAIN_MAX + 1)), (), ())
    issue = describe_room_limits(over_limit)
    assert issue is not None and "主卡组" in issue and "60" in issue, issue

    # 同名 10 张不在这里拦（投稿规则才会拦）
    duplicates = Deck((89631139,) * DUEL_MAIN_MAX, (), ())
    assert describe_room_limits(duplicates) is None

    # 额外/副卡组超 15 张：WindBot 会丢掉整副卡组
    extra_over = Deck(tuple(range(100000000, 100000040)), tuple(range(200000000, 200000016)), ())
    extra_issue = describe_room_limits(extra_over)
    assert extra_issue is not None and "额外卡组" in extra_issue, extra_issue
    side_over = Deck(
        tuple(range(100000000, 100000040)), (), tuple(range(200000000, 200000016))
    )
    side_issue = describe_room_limits(side_over)
    assert side_issue is not None and "副卡组" in side_issue, side_issue


def test_main_kinds_and_card_count() -> None:
    """统计属性应当反映去重后的卡种数与实际参战张数。"""

    deck = Deck(tuple(SAMPLE_MAIN), tuple(SAMPLE_EXTRA), tuple(SAMPLE_SIDE))
    assert deck.main_kinds == len(set(SAMPLE_MAIN))
    assert deck.card_count == len(SAMPLE_MAIN) + len(SAMPLE_EXTRA)


def main() -> int:
    """无 pytest 环境下逐个执行测试函数。"""

    tests = [(name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
