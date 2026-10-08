"""读 ygopro 录像（``.yrp``）：这一局是谁跟谁打的、双方各带了什么牌。

**能解出来的**：头部（录制时间、初始 LP、版本/标志位）、双方玩家名、**双方卡表**（主卡组 + 额外卡组）。
**解不出来的**：逐回合的动作序列。这是 ygopro 录像的设计决定的——**它只存玩家的"应答"、
不存引擎的"提问"**（客户端靠"同一颗随机种子重放整局"来还原画面）。
没有 ocgcore 就没法把那些应答放回原来的问题里，所以"谁在第几回合做了什么、怪放在哪一格"
从 ``.yrp`` 里读不出来。

要看动作序列得靠**我们自己的对局实录**（闸门看得到全部报文，见 ``duel/session.py`` 的记录器），
本轮先把这个工具做出来：它解决"群友带了什么牌、跟我们的有什么不同"，
导出的 ``.ydk`` 可以直接投稿进卡组池（``/加卡组``）用来做镜像对练。

用法::

    python tools/analyze_replay.py --latest 1                    # 最近一局
    python tools/analyze_replay.py --replay "D:/.../xxx.yrp"
    python tools/analyze_replay.py --latest 3 --export-dir temp/replays
    python tools/analyze_replay.py --latest 1 --compare 88       # 跟卡组池第 88 号比卡表

退出码：0=成功，1=没有可读的录像，2=参数/路径有问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import argparse
import struct
import sys
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.cards import CardDatabase  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.deckcode import parse_deck_code  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.deckpool import DeckPool  # noqa: E402  导入顺序受 sys.path 补丁影响

# ⚠ 原来这里是 `from duel.knowledge import default_data_dir`：AI 打牌整条链路（含
# duel/knowledge.py）已按 2026-10-07 用户口径删除，而这个工具**还要用"插件数据目录在哪"**
# （去找卡组池 deck_pool.db），所以把那一小段路径推导原样搬到这里（逻辑与原来一致）。


def default_data_dir(plugin_root: Path) -> Path:
    """插件数据目录（宿主约定：``<MaiBot>/data/plugins/<插件 id>/``）。"""

    import json

    manifest = Path(plugin_root) / "_manifest.json"
    plugin_id = ""
    try:
        plugin_id = str(json.loads(manifest.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        plugin_id = ""
    if not plugin_id:
        return Path(plugin_root) / "data"
    return Path(plugin_root).parent.parent / "data" / "plugins" / plugin_id


REPLAY_MAGIC_YRP2 = b"yrp2"
"""录像魔数（``b"yrp2"`` = mycard/ygopro 的 yrp2 格式）。"""

MAX_CARD_ID = 0x20000000
"""卡号上限（超过它的一定不是卡号——用来判定"这段不是卡表"）。"""

MAIN_RANGE = (20, 60)
"""主卡组张数的合理范围（40 是常态，60 是上限）。"""

EXTRA_RANGE = (0, 15)


@dataclass(frozen=True)
class DeckList:
    """录像里一方带的主卡组与额外卡组。"""

    main: Tuple[int, ...]
    extra: Tuple[int, ...]

    def counts(self) -> Dict[int, int]:
        """卡号 → 张数（两个区一起统计）。"""

        merged: Dict[int, int] = {}
        for card_id in (*self.main, *self.extra):
            merged[card_id] = merged.get(card_id, 0) + 1
        return merged


@dataclass(frozen=True)
class ReplayInfo:
    """一份录像解出来的东西。"""

    path: Path
    version: int
    flags: int
    start_time: int
    start_lp: int
    names: Tuple[str, ...]
    decks: Tuple[DeckList, ...]

    @property
    def when(self) -> str:
        """录制时间（本地时区，``YYYY-MM-DD HH:MM``）。"""

        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.start_time))


def read_u32(data: bytes, offset: int) -> int:
    """读一个 uint32（小端）。偏移越界时抛 ``ValueError``（不静默返回 0）。"""

    if offset < 0 or offset + 4 > len(data):
        raise ValueError(f"读越界：0x{offset:x}（文件只有 {len(data)} 字节）")
    return struct.unpack_from("<I", data, offset)[0]


def looks_like_cards(data: bytes, offset: int, count: int) -> bool:
    """``offset`` 起的 ``count`` 个 uint32 是否都像卡号（**0 张也算像**）。"""

    # ⚠ ``count == 0`` 必须算"像"：额外卡组可以为空（60 张主卡组、0 张额外的牌很常见，
    # 本机实测就有）。原来这里写 ``count <= 0`` 返回 False，后果是**整个卡表段被跳过**：
    # 那份录像只剩另一位玩家的卡表，解析器报"只找到 1 段卡表"，
    # 看着像"录像格式变了"——其实是把一副正常牌判成了格式问题。
    if count < 0 or offset + count * 4 > len(data):
        return False
    for index in range(count):
        card_id = read_u32(data, offset + index * 4)
        if not 0 < card_id < MAX_CARD_ID:
            return False
    return True


def find_decks(data: bytes) -> List[Tuple[int, DeckList]]:
    """按结构特征找出录像里的卡表段（**不写死偏移**，跨版本更稳），返回 ``(偏移, 卡表)``。

    判据：从一个 uint32 张数开始（20~60），紧跟着同样多个合法卡号；
    再跟一个 0~15 的额外卡组张数与同样多个合法卡号。两个玩家各一段、前后相邻。
    偏移也返回，是因为名字在卡表之前——解析名字要拿第一段的偏移当上界。
    """

    found: List[Tuple[int, DeckList]] = []
    offset = 0x40
    while offset + 8 <= len(data):
        main_count = read_u32(data, offset)
        if not MAIN_RANGE[0] <= main_count <= MAIN_RANGE[1]:
            offset += 1
            continue
        if not looks_like_cards(data, offset + 4, main_count):
            offset += 1
            continue
        extra_offset = offset + 4 + main_count * 4
        if extra_offset + 4 > len(data):
            break
        extra_count = read_u32(data, extra_offset)
        if not EXTRA_RANGE[0] <= extra_count <= EXTRA_RANGE[1]:
            offset += 1
            continue
        if not looks_like_cards(data, extra_offset + 4, extra_count):
            offset += 1
            continue
        main = tuple(
            read_u32(data, offset + 4 + index * 4) for index in range(main_count)
        )
        extra = tuple(
            read_u32(data, extra_offset + 4 + index * 4) for index in range(extra_count)
        )
        found.append((offset, DeckList(main=main, extra=extra)))
        # 跳过这一段，继续找下一个玩家
        offset = extra_offset + 4 + extra_count * 4
        if len(found) >= 2:
            break
    return found


def _is_name_char(value: str) -> bool:
    """这个名字字符算不算"人取的昵称"里该有的（ASCII 可见字符或中日韩汉字/假名）。"""

    code = ord(value)
    return 0x20 <= code < 0x7F or 0x3000 <= code <= 0x9FFF or 0xFF00 <= code <= 0xFFEF


def find_names(data: bytes, end: int) -> Tuple[str, ...]:
    """从头部区域里挑出 UTF-16LE 的玩家名（``end`` 之前都算头部）。

    名字里既有中文也有 ASCII（群里是 ``打憨憨$VzdP8`` 这种带后缀的昵称），所以**按 2 字节单位解码**：
    中文的第二个字节不是 0，用"低字节可见、高字节为 0"那种 ASCII 假设会直接把中文名字漏掉
    （实测就只解出了后缀 ``$VzdP8``）。对齐可能落在奇偶任一相位，两边都试。
    """

    limit = min(end, len(data))
    found: List[Tuple[int, str]] = []
    for phase in (0, 1):
        # 从 0x40 之后开始：0x20~0x3F 是种子/摘要一类的二进制，按 UTF-16 解会得到一堆假汉字
        offset = 0x44 + phase
        while offset + 2 <= limit:
            chars: List[str] = []
            cursor = offset
            while cursor + 2 <= limit:
                chunk = data[cursor:cursor + 2]
                if chunk == b"\x00\x00":
                    break
                try:
                    value = chunk.decode("utf-16-le")
                except UnicodeDecodeError:
                    break
                if not _is_name_char(value):
                    break
                chars.append(value)
                cursor += 2
            text = "".join(chars).strip()
            # 名字是 NUL 结尾的字段：要求前面是空位、后面紧跟 00 00，长度至少 2（群里真有叫「憨憨」的）
            if (
                len(text) >= 2
                and cursor + 2 <= limit
                and data[cursor:cursor + 2] == b"\x00\x00"
                and data[max(0, offset - 2):offset] == b"\x00\x00"
            ):
                found.append((offset, text))
                offset = cursor + 2
                continue
            offset += 2
    # 去重（奇偶两个相位会把同一段扫两次），按出现顺序取前几个
    unique: List[str] = []
    for _offset, text in sorted(found):
        if text not in unique:
            unique.append(text)
    return tuple(unique[:4])


def parse_replay(path: Path) -> ReplayInfo:
    """解一份录像；格式对不上时抛 ``ValueError``（不猜、不用兜底糊过去）。"""

    data = Path(path).read_bytes()
    if data[:4] != REPLAY_MAGIC_YRP2:
        raise ValueError(f"不是 yrp2 格式的录像：{path.name}（前 4 字节 {data[:4]!r}）")
    version = read_u32(data, 0x04)
    flags = read_u32(data, 0x08)
    start_time = read_u32(data, 0x14)
    regions = find_decks(data)
    if len(regions) < 2:
        raise ValueError(
            f"没能从录像里定位到两段卡表（找到 {len(regions)} 段）：{path.name}。"
            "录像格式可能变了——这是解析器该改的信号，不要当成'这局没带牌'。"
        )
    # 名字在卡表之前，而固定头部（初始 LP 等）从 0xA0 开始——扫描上界取两者中更小的那个，
    # 否则会把 0xA0 之后的规则字段当成名字（实测出现过「嘀稀搀倀㠀」这种二进制假名）
    names = find_names(data, min(regions[0][0], 0xA0))
    return ReplayInfo(
        path=Path(path),
        version=version,
        flags=flags,
        start_time=start_time,
        start_lp=read_u32(data, 0xA0),
        names=names,
        decks=(regions[0][1], regions[1][1]),
    )


def write_ydk(deck: DeckList, target: Path) -> None:
    """把一方的卡表写成 ``.ydk``（可以直接投稿进卡组池）。"""

    # 注释头用现在的插件名（合并前写的是 yugioh-duel-arena，2026-10-07 第五轮评审顺带指出）
    lines = ["#created by mai-play-ygo", "#main"]
    lines.extend(str(card_id) for card_id in deck.main)
    lines.append("#extra")
    lines.extend(str(card_id) for card_id in deck.extra)
    lines.append("!side")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def card_label(db: Optional[CardDatabase], card_id: int) -> str:
    """卡号 → 卡名（没有卡库时退回卡号）。"""

    if db is None:
        return str(card_id)
    return db.name(card_id) or str(card_id)


def describe_deck(deck: DeckList, db: Optional[CardDatabase], *, top: int = 12) -> List[str]:
    """卡表摘要：主/额外的张数与出现最多的几张（带张数）。"""

    lines = [f"主卡组 {len(deck.main)} 张、额外卡组 {len(deck.extra)} 张"]
    ranked = sorted(deck.counts().items(), key=lambda item: (-item[1], item[0]))[:top]
    lines.append(
        "带得最多的："
        + "、".join(f"{card_label(db, card_id)}×{count}" for card_id, count in ranked)
    )
    return lines


def describe_deck_diff(
    left: DeckList, right: DeckList, db: Optional[CardDatabase], *, top: int = 10
) -> List[str]:
    """两副牌的差异（只看张数不同的卡）——两边都玩同一系列时，这才是信息量所在。"""

    left_counts = left.counts()
    right_counts = right.counts()
    only_left = [(c, n) for c, n in sorted(left_counts.items()) if c not in right_counts]
    only_right = [(c, n) for c, n in sorted(right_counts.items()) if c not in left_counts]
    differs = [
        (c, left_counts[c], right_counts[c])
        for c in sorted(set(left_counts) & set(right_counts))
        if left_counts[c] != right_counts[c]
    ]
    lines: List[str] = []
    lines.append("只有左边带的：" + ("、".join(f"{card_label(db, c)}×{n}" for c, n in only_left[:top]) or "（没有）"))
    lines.append("只有右边带的：" + ("、".join(f"{card_label(db, c)}×{n}" for c, n in only_right[:top]) or "（没有）"))
    if differs:
        lines.append(
            "张数不同的："
            + "、".join(f"{card_label(db, c)} {l}→{r}" for c, l, r in differs[:top])
        )
    return lines


def compare_with_pool(
    deck: DeckList, deck_id: int, data_dir: Path, db: Optional[CardDatabase]
) -> List[str]:
    """跟卡组池里某副牌比卡表（``deck_id`` 是**数据库编号**，不是 /卡组列表 的位置号）。"""

    pool = DeckPool(data_dir, default_windbot_deck="Test")
    try:
        everything = list(pool.list_decks("")) + list(pool.own_decks(""))
        stored = next((item for item in everything if item.deck_id == int(deck_id)), None)
        if stored is None:
            known = "、".join(str(item.deck_id) for item in everything[:8])
            return [f"[错误] 卡组池里没有数据库编号 {deck_id} 的卡组（例如：{known} …）"]
        parsed = parse_deck_code(Path(stored.ydk_path).read_text(encoding="utf-8", errors="replace"))
    finally:
        pool.close()
    mine = DeckList(main=tuple(parsed.main), extra=tuple(parsed.extra))
    return [f"对比对象：「{stored.display_name}」（数据库编号 {stored.deck_id}）"] + describe_deck_diff(
        mine, deck, db
    )


def replay_paths(args: argparse.Namespace) -> List[Path]:
    """要分析哪些录像：``--replay`` 指定文件，``--latest N`` 取最新的几份。"""

    if args.replay:
        return [Path(args.replay)]
    directory = Path(args.replay_dir)
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.yrp"), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[: max(1, int(args.latest))]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读 ygopro 录像：双方卡表与元信息")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--replay", default="", help="指定一份 .yrp")
    parser.add_argument(
        "--replay-dir",
        default=str(_PLUGIN_ROOT / "clients" / "ygopro" / "replay"),
        help="录像目录（ygopro 客户端把它存在自己的 replay/ 下，默认用插件自带客户端那份）",
    )
    parser.add_argument("--latest", type=int, default=1, help="取最新的几份（默认 1）")
    parser.add_argument("--export-dir", default="", help="把双方卡表导出成 .ydk 到哪个目录")
    parser.add_argument("--compare", type=int, default=0, help="跟卡组池里第几号卡组比卡表")
    parser.add_argument("--cards-cdb", default="", help="卡库路径；给了才打印卡名")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    paths = replay_paths(args)
    if not paths:
        print(f"没有找到录像：{args.replay or args.replay_dir}")
        return 1
    db: Optional[CardDatabase] = None
    if args.cards_cdb:
        candidate = Path(args.cards_cdb)
        if candidate.is_file():
            db = CardDatabase(candidate)
        else:
            print(f"（卡库不存在，只打印卡号：{candidate}）")

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    export_dir = Path(args.export_dir) if args.export_dir else None
    failed = 0
    for path in paths:
        print(f"\n===== {path.name} =====")
        try:
            info = parse_replay(path)
        except (OSError, ValueError) as exc:
            print(f"[错误] {exc}")
            failed += 1
            continue
        print(f"录制于 {info.when}｜初始 LP {info.start_lp}｜格式 yrp2 v{info.version} flag=0x{info.flags:x}")
        print("玩家：" + ("、".join(info.names) if info.names else "（没解出名字）"))
        for index, deck in enumerate(info.decks, start=1):
            who = info.names[index - 1] if index - 1 < len(info.names) else f"玩家{index}"
            print(f"  {who}：")
            for line in describe_deck(deck, db):
                print(f"    {line}")
            if export_dir is not None:
                target = export_dir / f"{path.stem.replace(' ', '_')}_p{index}.ydk"
                write_ydk(deck, target)
                print(f"    已导出：{target}")
        if len(info.decks) >= 2:
            left, right = info.decks[0], info.decks[1]
            left_name = info.names[0] if info.names else "玩家1"
            right_name = info.names[1] if len(info.names) > 1 else "玩家2"
            print(f"  两边卡表的差异（{left_name} vs {right_name}）：")
            for line in describe_deck_diff(left, right, db):
                print(f"    {line}")
        if args.compare:
            for index, deck in enumerate(info.decks, start=1):
                who = info.names[index - 1] if index - 1 < len(info.names) else f"玩家{index}"
                print(f"  {who} 的卡表对比：")
                for line in compare_with_pool(deck, args.compare, data_dir, db):
                    print(f"    {line}")
    if db is not None:
        db.close()
    print(
        "\n说明：录像里只有卡表与元信息。ygopro 的录像不存引擎的提问、只存玩家的应答，"
        "所以逐回合的动作序列（谁做了什么、怪放哪一格）读不出来；那部分要靠我们自己的对局实录。"
    )
    return 2 if failed == len(paths) else 0


if __name__ == "__main__":
    raise SystemExit(main())
