"""录像解析（``tools/analyze_replay.py``）的单元测试。

录像格式是**逆向出来**的，所以这里钉死两件事：

* 头部与卡表段的布局（用合成的录像做往返，防止以后被人悄悄改坏）；
* 真实录像能解出"两段卡表 + 两个名字"（本机有录像时才跑，没录像就跳过）。

**明确不测的**：逐回合的动作序列——``.yrp`` 里根本没有（ygopro 只存玩家的应答、
不存引擎的提问），那是"对局实录"该干的事，不是这个解析器的锅。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import importlib.util
import struct
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

REPLAY_DIR = _PLUGIN_ROOT / "clients" / "ygopro" / "replay"
"""本机 ygopro 客户端的录像目录（默认用插件自带客户端那份；不存在就跳过那一条测试）。"""


def load_tool():
    """按文件路径加载录像解析工具。"""

    spec = importlib.util.spec_from_file_location(
        "tool_analyze_replay", str(_PLUGIN_ROOT / "tools" / "analyze_replay.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_replay(
    *,
    names: Tuple[str, str],
    decks: Tuple[Tuple[List[int], List[int]], Tuple[List[int], List[int]]],
    start_lp: int = 8000,
    start_time: int = 1790072527,
) -> bytes:
    """按逆向出来的布局造一份录像（头部 + 两个名字 + 两段卡表）。"""

    head = bytearray()
    head += b"yrp2"
    head += struct.pack("<I", 4962)          # version
    head += struct.pack("<I", 0x10)          # flag = REPLAY_UNIFORM
    head += struct.pack("<I", 0)             # seed
    head += struct.pack("<I", 0)             # datasize
    head += struct.pack("<I", start_time)
    head += b"\x00" * 8                      # props（LZMA 属性，未压缩时全零）
    head += b"\x00" * 0x20                   # 种子序列/摘要那一带
    head += struct.pack("<I", 1)             # header_version
    head += b"\x00" * 8
    for name in names:                       # 两个名字：UTF-16LE + NUL 结尾
        head += name.encode("utf-16-le") + b"\x00\x00"
    head += b"\x00" * (0xA0 - len(head))
    head += struct.pack("<I", start_lp)
    head += struct.pack("<I", 5)             # 起手张数
    head += struct.pack("<I", 1)             # 抽卡数
    head += struct.pack("<I", 0)
    for main, extra in decks:
        head += struct.pack("<I", len(main))
        head += b"".join(struct.pack("<I", card_id) for card_id in main)
        head += struct.pack("<I", len(extra))
        head += b"".join(struct.pack("<I", card_id) for card_id in extra)
    return bytes(head)


def test_synthetic_replay_round_trip() -> None:
    """合成的录像要能解回名字与两段卡表（防止布局被改坏）。"""

    tool = load_tool()
    main_a = [100267017 + index for index in range(40)]
    extra_a = [78397661 + index for index in range(15)]
    main_b = [200267017 + index for index in range(40)]
    extra_b = [88397661 + index for index in range(15)]
    blob = build_replay(
        names=("打憨憨$VzdP8", "憨憨"),
        decks=((main_a, extra_a), (main_b, extra_b)),
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "round-trip.yrp"
        path.write_bytes(blob)
        info = tool.parse_replay(path)
    assert info.version == 4962, info
    assert info.start_lp == 8000, info
    assert info.names == ("打憨憨$VzdP8", "憨憨"), info.names
    assert len(info.decks) == 2, info.decks
    assert list(info.decks[0].main) == main_a and list(info.decks[1].main) == main_b, info.decks
    assert list(info.decks[0].extra) == extra_a and list(info.decks[1].extra) == extra_b, info.decks


def test_deck_without_extra_is_still_found() -> None:
    """一副**没有额外卡组**的牌（60 张主卡组 + 0 张额外）也要能解出来。

    这是实测抓到的解析器 bug：卡表段的判据里，额外卡组张数写的是 0~15，
    但"这串是不是卡号"的检查当时写成"张数 <= 0 就不算"，于是额外 0 张的那一段
    整段被跳过 —— 报告变成"只找到 1 段卡表，录像格式可能变了"，
    把一副正常的牌（本机真有人这么带）判成了格式问题。
    """

    tool = load_tool()
    main_a = [100267017 + index for index in range(60)]
    main_b = [200267017 + index for index in range(40)]
    extra_b = [88397661 + index for index in range(15)]
    blob = build_replay(
        names=("打憨憨$VzdP8", "憨憨"),
        decks=((main_a, []), (main_b, extra_b)),
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "no-extra.yrp"
        path.write_bytes(blob)
        info = tool.parse_replay(path)
    assert list(info.decks[0].main) == main_a, info.decks
    assert list(info.decks[0].extra) == [], info.decks
    assert list(info.decks[1].extra) == extra_b, info.decks


def test_non_replay_file_is_rejected() -> None:
    """格式不对要说清楚并抛错——不猜、不返回空卡表（那样看起来像"这局没带牌"）。"""

    tool = load_tool()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "not-a-replay.yrp"
        path.write_bytes(b"definitely not a replay" * 8)
        try:
            tool.parse_replay(path)
        except ValueError as exc:
            assert "yrp2" in str(exc), exc
        else:
            raise AssertionError("不是录像时应当抛 ValueError")


def test_deck_diff_spots_the_differences() -> None:
    """两副牌比差异：只在一侧出现的卡与张数不同的卡都要报出来。"""

    tool = load_tool()
    left = tool.DeckList(main=(1, 1, 2, 3), extra=(9,))
    right = tool.DeckList(main=(1, 2, 2, 4), extra=(9,))
    lines = "\n".join(tool.describe_deck_diff(left, right, None))
    # 没给卡库时只打印卡号（卡名要查库，这里不依赖卡库）
    assert "只有左边带的：3×1" in lines, lines
    assert "只有右边带的：4×1" in lines, lines
    assert "卡 1 2→1" in lines or "1 2→1" in lines, lines
    # 完全一样时不要硬编差异（两边都玩同一系列时这很常见）
    same = tool.DeckList(main=(1, 2), extra=(9,))
    same_lines = "\n".join(tool.describe_deck_diff(same, same, None))
    assert "（没有）" in same_lines, same_lines


#: 小于这个字节数的 .yrp 一律当"残局 stub"（对局被掐断时 ygopro 也会落一份没卡表的头部）
_MIN_REPLAY_BYTES = 1024


def test_real_replays_if_available() -> None:
    """本机有录像的话，真实录像也要能解出两段卡表与两个名字。"""

    if not REPLAY_DIR.is_dir():
        print("      （跳过：本机没有 ygopro 录像目录）")
        return
    files = sorted(REPLAY_DIR.glob("*.yrp"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not files:
        print("      （跳过：录像目录是空的）")
        return
    # **先滤掉"残局 stub"**：对局被中途掐断（`--max-duration`、手动停轮次、进程被杀）时
    # ygopro 也会落一份几百字节的录像，里面只有头部、没有卡表——它不是"录像格式变了"，
    # 而是那一局没打完。实测（2026-10-06）：轮次被我掐掉后，最新那份 412 字节的 stub
    # 直接把这条测试判红。真实的录像至少几 KB。
    files = [path for path in files if path.stat().st_size >= _MIN_REPLAY_BYTES]
    if not files:
        print("      （跳过：最近的录像都是残局 stub）")
        return
    tool = load_tool()
    parsed = 0
    for path in files[:5]:
        info = tool.parse_replay(path)
        assert len(info.decks) == 2, (path.name, info.decks)
        for deck in info.decks:
            assert 20 <= len(deck.main) <= 60, (path.name, len(deck.main))
            assert 0 <= len(deck.extra) <= 15, (path.name, len(deck.extra))
            assert all(card_id > 0 for card_id in deck.main), path.name
        assert info.names, f"{path.name}：真实录像应当能解出玩家名"
        parsed += 1
    print(f"      （解析了 {parsed} 份真实录像）")


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
