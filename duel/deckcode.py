"""游戏王卡组码的解析与 .ydk 生成。

群友可能粘贴三种不同来源的卡组码，本模块统一解析成 :class:`Deck`：

1. ``ydke://<main>!<extra>!<side>!``
   EDOPro / MDPro3 的 YDKE 导出格式，三段各自是标准 base64 编码的 uint32 小端卡 ID 数组，
   同一张卡有几张就重复出现几次。
2. ``http://deck.ourygo.top?ygotype=deck&v=1&d=<base64url>``
   萌卡（MyCard）体系的卡组分享链接，也是 MDPro3 的「卡组码」分享形式。
   位流结构：8bit 主卡组张数 + 4bit 额外张数 + 4bit 副卡组张数，
   之后每张卡占 29bit（2bit 数量前缀 + 27bit 卡 ID），按主/额外/副顺序排列。
3. 裸 base64 字符串（卡组码本体）
   解出来是 uint32 小端数组，每个 uint32 的布局为：
   低 28 位卡 ID、bit28-29 卡组类型（0 主 / 1 额外 / 2 副）、bit30-31 数量减一。

格式 2 与格式 3 都是裸 base64，只能靠结构校验区分，因此 :func:`parse_deck_code`
在两者同时成立时会标记 ``ambiguous``，由调用方决定是否提醒用户。

另需注意卡组张数上有**三条**限制，出自三个不同的地方，混成一条就会得出错误结论
（三条都是 2026-10-10 用 `tools/probe_deck_size.py` 实测的，那个工具能重新量）：

1. **对局引擎**：一场对局实际用的主卡组**最多 60 张**。注册多少张它都收，但对局开场只把 60 张
   放进卡组——注册 61 / 235 / 250 张时，内核发给客户端的分组报文里都写着"对局内主卡组 = 60"
   （多出来的牌等于白带，WindBot 自己的卡组计数器还会因此一直报 mismatch）。
2. **内核**（``clients/ygopro`` 的 ``ygopro.exe`` 服务端模式）：注册的卡组**主卡组+额外最多 250 张**，
   超了当场回 ``STOC_ERROR_MSG``（``msg=2`` DECKERROR）并把 bot 踢出房间；而 WindBot 对这条
   **静默退出**（它只在 DEBUG 构建里打日志）。群友看到的现象就是"房间开好了但 bot 进不去"。
3. **WindBot** 的 ``Deck.Load``：主卡组 > 60、额外 > 15、副卡组 > 15 都**丢弃整副卡组**，
   于是 bot 带着空卡组进房（随后是异常退出）。它的 60 与第 1 条同源，所以没必要去放行。

:func:`describe_issues` 是**投稿路径**的规则（40~60 张、同名 ≤3，那是"像一副正常卡组"）；
:func:`describe_room_limits` 是**开房前**的硬限制（只留三条里真正会炸的那些，不查同名张数——
"赖皮卡组"那种 5 张同名是要能打的）。开房前拦一道比让内核把 bot 踢出去、群里对着一个
没有 bot 的房间干等要好得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import base64
import binascii
import re
import struct


# 卡组容量限制，与 ygopro 客户端及 WindBot 的校验保持一致
MAIN_MIN = 40
MAIN_MAX = 60
EXTRA_MAX = 15
SIDE_MAX = 15

# 一场对局实际会用的主卡组上限（实测：注册 61/235/250 张时，内核发给客户的报文里都写着 60）。
# 与 :data:`MAIN_MAX` 数值相同但含义不同：`MAIN_MAX` 是"投稿要像正常卡组"的规则，
# 这里是"对局引擎真的只放这么多张"的事实。
DUEL_MAIN_MAX = 60

# 裸卡组码格式中卡 ID 占用的位数，以及 YDKE / 萌卡格式中卡 ID 的位数
_RAW_ID_MASK = (1 << 28) - 1
_YAO_ID_MASK = (1 << 27) - 1

# base64 字符集校验（同时容忍标准与 URL 安全两种字母表）
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")

# 格式标识
FORMAT_YDK = "ydk"
FORMAT_YDKE = "ydke"
FORMAT_OURYGO = "ourygo"
FORMAT_RAW = "code"


class DeckCodeError(ValueError):
    """卡组码无法解析时抛出。"""


class DeckUnplayableError(RuntimeError):
    """卡组本身能解析，但开局这一套跑不起来（内核收不下 / 卡表读不到）。

    与 :class:`DeckCodeError` 分开：那个是"卡组码格式不对"，这个是"这副牌内核打不了"，
    调用方对两者的处理完全不同（前者让群友重发卡组码，后者要去改池子里的卡表）。
    """


@dataclass(frozen=True)
class Deck:
    """一副解析完成的卡组。

    三个卡组字段都保留重复项（同一种卡有 3 张就出现 3 次），
    因为它们最终要写成 .ydk 交给 WindBot 读取。
    """

    main: Tuple[int, ...]
    extra: Tuple[int, ...]
    side: Tuple[int, ...]
    source_format: str = ""
    """解析时命中的格式标识，取值见模块顶部的 FORMAT_* 常量。"""

    ambiguous: bool = False
    """格式 2 与格式 3 同时成立时为 True，表示解析结果可能不是用户的原意。"""

    @property
    def main_kinds(self) -> int:
        """主卡组的卡种数量（去重后）。"""

        return len(set(self.main))

    @property
    def card_count(self) -> int:
        """主卡组+额外卡组的总张数，即实际参与决斗的张数。"""

        return len(self.main) + len(self.extra)

    def to_ydk(self, comment: str = "") -> str:
        """渲染成 .ydk 文本。

        WindBot 只认 ``!side`` 分隔符，``#`` 开头的行一律跳过，
        因此 ``#main`` / ``#extra`` 只是给人看的注释。

        Args:
            comment: 写在首行注释里的说明文字，例如卡组名与投稿人。
        """

        # 注释头用**现在的插件名**（合并前写的是 "MaiBot duel-arena"，2026-10-09 清掉）：
        # 它只是给人看的首行注释（解析时被忽略），但对不上现名会让人误以为是别的插件的产物
        lines = [f"#created by mai-play-ygo{' - ' + comment if comment else ''}"]
        lines.append("#main")
        lines.extend(str(card_id) for card_id in self.main)
        lines.append("#extra")
        lines.extend(str(card_id) for card_id in self.extra)
        lines.append("!side")
        lines.extend(str(card_id) for card_id in self.side)
        return "\n".join(lines) + "\n"

    def to_deck_code(self) -> str:
        """反向编码成裸卡组码（格式 3），用于回显或核对。"""

        buffer = bytearray()
        for deck_type, cards in ((0, self.main), (1, self.extra), (2, self.side)):
            for card_id, count in _count_cards(cards).items():
                if not 1 <= count <= 4:
                    raise DeckCodeError(f"卡 {card_id} 的数量 {count} 超出 1-4 的范围")
                value = (card_id & _RAW_ID_MASK) | (deck_type << 28) | ((count - 1) << 30)
                buffer.extend(struct.pack("<I", value))
        return _b64url_encode(bytes(buffer))


def _count_cards(cards: Sequence[int]) -> Dict[int, int]:
    """统计卡组中每种卡的数量，返回「卡 ID -> 张数」且保持首次出现顺序。"""

    counts: Dict[int, int] = {}
    for card_id in cards:
        counts[card_id] = counts.get(card_id, 0) + 1
    return counts


def _b64url_encode(data: bytes) -> str:
    """编码成 URL 安全 base64 并去掉尾部填充。"""

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    """解码 base64 文本，同时容忍标准与 URL 安全字母表。

    Args:
        text: 待解码文本，允许缺少 ``=`` 填充。

    Raises:
        DeckCodeError: 文本不是合法 base64 时抛出。
    """

    normalized = text.strip().replace("-", "+").replace("_", "/")
    if not normalized:
        raise DeckCodeError("卡组码为空")
    normalized += "=" * (-len(normalized) % 4)
    try:
        return base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DeckCodeError(f"卡组码不是合法的 base64：{exc}") from exc


class _BitReader:
    """按 MSB 优先顺序读取位流的游标。

    萌卡卡组码的位流不是字节对齐的，直接用位移读比拼二进制字符串更省事也更快。
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    @property
    def position(self) -> int:
        """当前已消费的位数。"""

        return self._position

    @property
    def remaining(self) -> int:
        """剩余可读位数。"""

        return len(self._data) * 8 - self._position

    def read(self, width: int) -> int:
        """读取 ``width`` 位并返回其无符号整数值。

        Raises:
            DeckCodeError: 剩余位数不足时抛出。
        """

        if self.remaining < width:
            raise DeckCodeError("卡组码数据不完整")
        value = 0
        for _ in range(width):
            byte_index, bit_offset = divmod(self._position, 8)
            bit = (self._data[byte_index] >> (7 - bit_offset)) & 1
            value = (value << 1) | bit
            self._position += 1
        return value


def _parse_ydk_text(text: str) -> Deck:
    """解析 .ydk 文本。

    ``#`` 开头的行按注释处理，其中包含 main/extra/side 关键字时切换当前分区；
    ``!side`` 之后的内容进入副卡组。
    """

    main: List[int] = []
    extra: List[int] = []
    side: List[int] = []
    current = main
    found_card = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("!"):
            lowered = stripped.lower()
            if "extra" in lowered:
                current = extra
            elif "side" in lowered:
                current = side
            elif "main" in lowered:
                current = main
            continue
        if not stripped.isdigit():
            continue
        current.append(int(stripped))
        found_card = True

    if not found_card:
        raise DeckCodeError("YDK 文本里没有找到任何卡")
    return Deck(tuple(main), tuple(extra), tuple(side), source_format=FORMAT_YDK)


def _parse_ydke(text: str) -> Deck:
    """解析 ``ydke://`` 格式的卡组码。"""

    body = text.strip()
    if body.lower().startswith("ydke://"):
        body = body[len("ydke://") :]
    parts = body.split("!")
    if len(parts) < 2:
        raise DeckCodeError("YDKE 格式错误：至少需要主卡组与额外卡组两段")

    def decode_segment(segment: str) -> Tuple[int, ...]:
        """解码单段卡 ID 数组，长度为空视为空卡组。"""

        if not segment:
            return ()
        raw = _b64_decode(segment)
        if len(raw) % 4:
            raise DeckCodeError("YDKE 分段长度不是 4 的倍数")
        return tuple(struct.unpack(f"<{len(raw) // 4}I", raw))

    return Deck(
        decode_segment(parts[0]),
        decode_segment(parts[1]),
        decode_segment(parts[2]) if len(parts) > 2 else (),
        source_format=FORMAT_YDKE,
    )


def _try_ourygo(raw: bytes) -> Optional[Deck]:
    """尝试按萌卡位流格式解析 ``raw``，结构不合法时返回 None。"""

    reader = _BitReader(raw)
    try:
        main_kinds = reader.read(8)
        extra_kinds = reader.read(4)
        side_kinds = reader.read(4)
        if main_kinds > MAIN_MAX or extra_kinds > EXTRA_MAX or side_kinds > SIDE_MAX:
            return None

        def read_section(kinds: int) -> Optional[Tuple[int, ...]]:
            """按卡种数读取一个分区，任意一项不合法就整体判定失败。"""

            cards: List[int] = []
            for _ in range(kinds):
                count = reader.read(2)
                card_id = reader.read(27)
                if count == 0 or card_id == 0:
                    return None
                cards.extend([card_id] * count)
            return tuple(cards)

        main = read_section(main_kinds)
        extra = read_section(extra_kinds)
        side = read_section(side_kinds)
        if main is None or extra is None or side is None:
            return None
    except DeckCodeError:
        return None

    # 合法数据只会补零到字节边界，剩余位数不可能超过 7
    if reader.remaining > 7:
        return None
    return Deck(main, extra, side, source_format=FORMAT_OURYGO)


def _try_raw_code(raw: bytes) -> Optional[Deck]:
    """尝试按 uint32 小端数组格式解析 ``raw``，结构不合法时返回 None。"""

    if not raw or len(raw) % 4:
        return None
    main: List[int] = []
    extra: List[int] = []
    side: List[int] = []
    for (value,) in struct.iter_unpack("<I", raw):
        card_id = value & _RAW_ID_MASK
        deck_type = (value >> 28) & 0x3
        count = ((value >> 30) & 0x3) + 1
        if card_id == 0 or deck_type == 3:
            return None
        if deck_type == 0:
            main.extend([card_id] * count)
        elif deck_type == 1:
            extra.extend([card_id] * count)
        else:
            side.extend([card_id] * count)
    return Deck(tuple(main), tuple(extra), tuple(side), source_format=FORMAT_RAW)


def detect_format(text: str) -> str:
    """判断卡组码文本属于哪种格式，识别不出时返回空字符串。

    只做前缀与外形判断，不做结构校验；裸 base64 一律报 ``code``，
    真正的格式归属由 :func:`parse_deck_code` 的结构校验决定。
    """

    stripped = text.strip()
    lowered = stripped.lower()
    if lowered.startswith("ydke://"):
        return FORMAT_YDKE
    if "deck.ourygo.top" in lowered:
        return FORMAT_OURYGO
    if "#main" in lowered or "#extra" in lowered or "!side" in lowered:
        return FORMAT_YDK
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if lines and all(line.isdigit() for line in lines):
        return FORMAT_YDK
    if _BASE64_RE.match(stripped) and len(stripped) >= 8:
        return FORMAT_RAW
    return ""


def _extract_ourygo_payload(text: str) -> str:
    """从萌卡分享链接或裸参数中取出 ``d=`` 的值。"""

    match = re.search(r"[?&]d=([^&\s]+)", text)
    if match:
        return match.group(1)
    return text.strip()


def parse_deck_code(text: str) -> Deck:
    """把任意一种卡组码解析成 :class:`Deck`。

    Args:
        text: 群友粘贴的卡组码，可以是三种格式中的任意一种。

    Raises:
        DeckCodeError: 三种格式都无法解释该文本时抛出，错误信息里会说明原因。
    """

    if not text or not text.strip():
        raise DeckCodeError("卡组码为空")

    detected = detect_format(text)
    if detected == FORMAT_YDK:
        return _parse_ydk_text(text)
    if detected == FORMAT_YDKE:
        return _parse_ydke(text)

    payload = _extract_ourygo_payload(text) if detected == FORMAT_OURYGO else text.strip()
    raw = _b64_decode(payload)

    candidates: List[Deck] = []
    # 萌卡链接明确指向格式 2，不做格式 3 的尝试
    if detected != FORMAT_OURYGO:
        raw_deck = _try_raw_code(raw)
        if raw_deck is not None:
            candidates.append(raw_deck)
    ourygo_deck = _try_ourygo(raw)
    if ourygo_deck is not None:
        candidates.append(ourygo_deck)

    if not candidates:
        raise DeckCodeError(
            "无法识别这段卡组码：既不是 ydke:// 链接、萌卡分享链接，"
            "也不是合法的卡组码本体。请确认是否复制完整。"
        )
    if len(candidates) == 1:
        return candidates[0]

    # 两种解释都成立时会落在同一个 base64 文本上，取萌卡格式并把歧义如实上报
    for candidate in candidates:
        if candidate.source_format == FORMAT_OURYGO:
            return Deck(
                candidate.main,
                candidate.extra,
                candidate.side,
                source_format=FORMAT_OURYGO,
                ambiguous=True,
            )
    return candidates[0]


def describe_issues(deck: Deck) -> List[str]:
    """检查卡组是否满足可对战条件，返回中文问题列表（空列表表示没有问题）。

    这些限制全部来自 ygopro 客户端与 WindBot 的硬校验：任何一条不满足，
    WindBot 都会静默丢弃整个卡组，导致 bot 拿一副空卡组上场。
    """

    issues: List[str] = []
    if not MAIN_MIN <= len(deck.main) <= MAIN_MAX:
        issues.append(f"主卡组 {len(deck.main)} 张，需要在 {MAIN_MIN}-{MAIN_MAX} 张之间")
    if len(deck.extra) > EXTRA_MAX:
        issues.append(f"额外卡组 {len(deck.extra)} 张，不能超过 {EXTRA_MAX} 张")
    if len(deck.side) > SIDE_MAX:
        issues.append(f"副卡组 {len(deck.side)} 张，不能超过 {SIDE_MAX} 张")
    for name, cards in (("主卡组", deck.main), ("额外卡组", deck.extra), ("副卡组", deck.side)):
        over_limit = [card_id for card_id, count in _count_cards(cards).items() if count > 3]
        # 额外卡组同样最多 3 张同名，卡组码里出现 4 张必然是编码错误
        if over_limit:
            issues.append(f"{name}里有卡出现超过 3 张：{over_limit}")
    return issues


def describe_room_limits(deck: Deck) -> Optional[str]:
    """开房前的硬限制：卡组踩到其中一条就返回中文原因（都没踩返回 None）。

    只留**真会炸**的三条，都与"同名几张"无关（那是投稿规则的事）：

    * 主卡组 > :data:`DUEL_MAIN_MAX`：对局只放 60 张进卡组，多出来的牌等于白带；
    * 额外 > :data:`EXTRA_MAX` / 副卡组 > :data:`SIDE_MAX`：WindBot 直接丢掉整副卡组，
      bot 会带着空卡组进房然后异常退出。

    主卡组+额外 > 250 那条（内核注册上限）不用单独查：上面两条成立时最多 75 张。
    """

    if len(deck.main) > DUEL_MAIN_MAX:
        return (
            f"主卡组 {len(deck.main)} 张，超过对局上限 {DUEL_MAIN_MAX} 张"
            f"（内核注册最多能收 250 张，但对局开场只把 {DUEL_MAIN_MAX} 张放进卡组，"
            f"多出来的牌打不出来）"
        )
    if len(deck.extra) > EXTRA_MAX:
        return f"额外卡组 {len(deck.extra)} 张，超过 {EXTRA_MAX} 张——WindBot 会丢掉整副卡组"
    if len(deck.side) > SIDE_MAX:
        return f"副卡组 {len(deck.side)} 张，超过 {SIDE_MAX} 张——WindBot 会丢掉整副卡组"
    return None


def deck_summary(deck: Deck, *, card_lookup: Any = None) -> str:
    """生成一行人类可读的卡组摘要，用于群里播报。

    Args:
        deck: 目标卡组。
        card_lookup: 可选的「卡 ID -> 卡名」查询函数，缺省时只报张数。
    """

    def top_cards(cards: Sequence[int], limit: int = 3) -> List[str]:
        """挑出张数最多的前几张卡用于展示。"""

        ranked = sorted(_count_cards(cards).items(), key=lambda item: (-item[1], item[0]))[:limit]
        if card_lookup is None:
            return [f"{card_id}x{count}" for card_id, count in ranked]

        def label(card_id: int) -> str:
            """查卡名；查不到时退回编号，避免摘要里出现光秃秃的 “x3”。"""

            name = str(card_lookup(card_id) or "").strip()
            return name or f"#{card_id}"

        return [f"{label(card_id)}x{count}" for card_id, count in ranked]

    parts = [f"主{len(deck.main)}/额外{len(deck.extra)}/副{len(deck.side)}"]
    highlights = top_cards(deck.main + deck.extra)
    if highlights:
        parts.append("主力：" + "、".join(highlights))
    return "，".join(parts)
