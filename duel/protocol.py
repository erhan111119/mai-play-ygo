"""ygopro 协议层：报文分帧、消息号常量与对局报文解析。

协议本体是明文 TCP，帧格式为::

    [uint16 小端：payload 长度 + 1][uint8 消息号][payload]

长度字段的值等于「消息号 1 字节 + payload」的字节数，所以一帧的线上总长是 ``2 + 长度值``。

**关键事实（决定了本模块的规模）**：游戏内部消息的切分在传输层完成。WindBot 的分发循环
(``Game/GameBehavior.cs`` 的 ``OnPacket``) 在读到 ``STOC_GAME_MSG`` 之后直接
``ReadByte()`` 取出游戏消息号并分发，从不循环读取；EDOPro 的 ``GenericDuel::Analyze``
也是每条消息单独发一个 ``STOC_GAME_MSG``。也就是说**一帧只装一条 MSG_\\***。

因此这里不需要维护全部 80 余种消息的隐式长度表——只解析关心的那部分，遇到不认识的消息号
直接忽略也不会影响后续帧（下一帧是独立切分的）。

消息号与各报文的字节宽度不是猜的，而是双重核对过的：

* 消息号取自 ``ygopro-msg-encode`` 的 ``dist/src/protos/msg/proto/*.d.ts``（80 条），
  并与 WindBot 的 ``YGOSharp.OCGWrapper.Enums/GameMessage.cs``（95 项）逐条比对一致；
* 字节宽度逐个对照 WindBot 的解析实现（``OnWin`` / ``OnNewPhase`` / ``OnChaining`` 等）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional, Tuple

import struct


# 帧长度前缀占用的字节数
FRAME_HEADER_SIZE = 2

# 单条游戏内聊天的字符上限，取自服务端 gframe/network.h 的 LEN_CHAT_MSG
LEN_CHAT_MSG = 256

# 协议长度字段是 uint16，单帧最大 65535 字节
MAX_FRAME_LENGTH = 0xFFFF


class Ctos(IntEnum):
    """客户端发往服务器的消息号。"""

    RESPONSE = 0x1
    UPDATE_DECK = 0x2
    HAND_RESULT = 0x3
    TP_RESULT = 0x4
    PLAYER_INFO = 0x10
    CREATE_GAME = 0x11
    JOIN_GAME = 0x12
    LEAVE_GAME = 0x13
    SURRENDER = 0x14
    TIME_CONFIRM = 0x15
    CHAT = 0x16
    EXTERNAL_ADDRESS = 0x17
    HS_TODUELIST = 0x20
    HS_TOOBSERVER = 0x21
    HS_READY = 0x22
    HS_NOTREADY = 0x23
    HS_KICK = 0x24
    HS_START = 0x25
    REMATCH_RESPONSE = 0xF0


class Stoc(IntEnum):
    """服务器发往客户端的消息号。"""

    GAME_MSG = 0x1
    ERROR_MSG = 0x2
    SELECT_HAND = 0x3
    SELECT_TP = 0x4
    HAND_RESULT = 0x5
    TP_RESULT = 0x6
    CHANGE_SIDE = 0x7
    WAITING_SIDE = 0x8
    DECK_COUNT = 0x9
    CREATE_GAME = 0x11
    JOIN_GAME = 0x12
    TYPE_CHANGE = 0x13
    LEAVE_GAME = 0x14
    DUEL_START = 0x15
    DUEL_END = 0x16
    REPLAY = 0x17
    TIME_LIMIT = 0x18
    CHAT = 0x19
    HS_PLAYER_ENTER = 0x20
    HS_PLAYER_CHANGE = 0x21
    HS_WATCH_CHANGE = 0x22
    TEAMMATE_SURRENDER = 0x23
    NEW_REPLAY = 0x30
    CATCHUP = 0xF0
    REMATCH = 0xF1
    WAITING_REMATCH = 0xF2
    CHAT_2 = 0xF3


class Msg(IntEnum):
    """ocgcore 游戏内部消息号。"""

    RETRY = 1
    HINT = 2
    WAITING = 3
    START = 4
    WIN = 5
    UPDATE_DATA = 6
    UPDATE_CARD = 7
    REQUEST_DECK = 8
    SELECT_BATTLECMD = 10
    SELECT_IDLECMD = 11
    SELECT_EFFECTYN = 12
    SELECT_YESNO = 13
    SELECT_OPTION = 14
    SELECT_CARD = 15
    SELECT_CHAIN = 16
    SELECT_PLACE = 18
    SELECT_POSITION = 19
    SELECT_TRIBUTE = 20
    SORT_CHAIN = 21
    SELECT_COUNTER = 22
    SELECT_SUM = 23
    SELECT_DISFIELD = 24
    SORT_CARD = 25
    SELECT_UNSELECT_CARD = 26
    CONFIRM_DECKTOP = 30
    CONFIRM_CARDS = 31
    SHUFFLE_DECK = 32
    SHUFFLE_HAND = 33
    REFRESH_DECK = 34
    SWAP_GRAVE_DECK = 35
    SHUFFLE_SET_CARD = 36
    REVERSE_DECK = 37
    DECK_TOP = 38
    SHUFFLE_EXTRA = 39
    NEW_TURN = 40
    NEW_PHASE = 41
    CONFIRM_EXTRATOP = 42
    MOVE = 50
    POS_CHANGE = 53
    SET = 54
    SWAP = 55
    FIELD_DISABLED = 56
    SUMMONING = 60
    SUMMONED = 61
    SPSUMMONING = 62
    SPSUMMONED = 63
    FLIPSUMMONING = 64
    FLIPSUMMONED = 65
    CHAINING = 70
    CHAINED = 71
    CHAIN_SOLVING = 72
    CHAIN_SOLVED = 73
    CHAIN_END = 74
    CHAIN_NEGATED = 75
    CHAIN_DISABLED = 76
    CARD_SELECTED = 80
    RANDOM_SELECTED = 81
    BECOME_TARGET = 83
    DRAW = 90
    DAMAGE = 91
    RECOVER = 92
    EQUIP = 93
    LPUPDATE = 94
    UNEQUIP = 95
    CARD_TARGET = 96
    CANCEL_TARGET = 97
    PAY_LPCOST = 100
    ADD_COUNTER = 101
    REMOVE_COUNTER = 102
    ATTACK = 110
    BATTLE = 111
    ATTACK_DISABLED = 112
    DAMAGE_STEP_START = 113
    DAMAGE_STEP_END = 114
    MISSED_EFFECT = 120
    BE_CHAIN_TARGET = 121
    CREATE_RELATION = 122
    RELEASE_RELATION = 123
    TOSS_COIN = 130
    TOSS_DICE = 131
    ROCK_PAPER_SCISSORS = 132
    HAND_RES = 133
    ANNOUNCE_RACE = 140
    ANNOUNCE_ATTRIB = 141
    ANNOUNCE_CARD = 142
    ANNOUNCE_NUMBER = 143
    CARD_HINT = 160
    TAG_SWAP = 161
    RELOAD_FIELD = 162
    AI_NAME = 163
    SHOW_HINT = 164
    PLAYER_HINT = 165
    MATCH_KILL = 170
    CUSTOM_MSG = 180
    DUEL_WINNER = 200


class CardLocation(IntEnum):
    """卡片所在区域，取自 WindBot 的 ``CardLocation`` 枚举。

    ``ON_FIELD`` / ``OVERLAY`` 是复合标志位，不直接表示单一区域。
    """

    DECK = 1
    HAND = 2
    MONSTER_ZONE = 4
    SPELL_ZONE = 8
    GRAVE = 16
    REMOVED = 32
    EXTRA = 64
    OVERLAY = 128
    ON_FIELD = 12
    FIELD_ZONE = 256
    PENDULUM_ZONE = 512


class DuelPhase(IntEnum):
    """决斗阶段，取自 WindBot 的 ``DuelPhase`` 枚举。

    注意取值可以到 512，所以 ``MSG_NEW_PHASE`` 是 2 字节字段而非 1 字节。
    """

    DRAW = 1
    STANDBY = 2
    MAIN1 = 4
    BATTLE_START = 8
    BATTLE_STEP = 16
    DAMAGE = 32
    DAMAGE_CAL = 64
    BATTLE = 128
    MAIN2 = 256
    END = 512


PHASE_NAMES = {
    DuelPhase.DRAW: "抽卡阶段",
    DuelPhase.STANDBY: "准备阶段",
    DuelPhase.MAIN1: "主要阶段1",
    DuelPhase.BATTLE_START: "战斗阶段",
    DuelPhase.BATTLE_STEP: "战斗步骤",
    DuelPhase.DAMAGE: "伤害步骤",
    DuelPhase.DAMAGE_CAL: "伤害计算",
    DuelPhase.BATTLE: "战斗阶段",
    DuelPhase.MAIN2: "主要阶段2",
    DuelPhase.END: "结束阶段",
}

# 决斗结束原因，对应 MSG_WIN 的第二个字段
# MSG_WIN 的第二个字节是「结束原因」。取值与文案**照抄内核自带的 strings.conf**：
#   !victory 0x0 投降 / 0x1 基本分变成0 / 0x2 没有卡可抽 / 0x3 超时 / 0x4 失去连接
#   0x10 起是真正的「特殊胜利」（艾克佐迪亚、终焉的倒计时…），每个编号对应一张卡的效果。
# 这里曾经写错过（把 0x1 当成特殊胜利、0x2 当成"生命值或卡组耗尽"），结果训练日志里
# 一堆普通打空血的局被标成「特殊胜利条件达成」——战报与训练数据都被带偏过，所以改成照抄。
WIN_REASONS = {
    0x0: "投降",
    0x1: "基本分变成0",
    0x2: "没有卡可抽",
    0x3: "超时",
    0x4: "失去连接",
}
SPECIAL_WIN_REASON_MIN = 0x10

# 区域名称，用于把 MSG_MOVE 翻译成「送墓」「除外」这类人话
LOCATION_NAMES = {
    CardLocation.DECK: "卡组",
    CardLocation.HAND: "手牌",
    CardLocation.MONSTER_ZONE: "怪兽区",
    CardLocation.SPELL_ZONE: "魔陷区",
    CardLocation.GRAVE: "墓地",
    CardLocation.REMOVED: "除外区",
    CardLocation.EXTRA: "额外卡组",
    CardLocation.FIELD_ZONE: "场地魔法区",
    CardLocation.PENDULUM_ZONE: "灵摆区",
}

EVENT_LABELS = {
    "win": "决斗结束",
    "new_turn": "新回合",
    "new_phase": "阶段推进",
    "summon": "通常召唤",
    "sp_summon": "特殊召唤",
    "flip_summon": "反转召唤",
    "set": "盖放",
    "chain": "发动效果",
    "chain_end": "连锁结束",
    "attack": "攻击宣言",
    "damage": "受到伤害",
    "recover": "恢复生命值",
    "pay_cost": "支付生命值",
    "lp_update": "生命值变化",
    "draw": "抽卡",
    "move": "卡片移动",
}


@dataclass(frozen=True)
class Frame:
    """一条完整的协议帧。"""

    message_id: int
    payload: bytes


@dataclass(frozen=True)
class DuelEvent:
    """从游戏消息里提取出的一条可播报事件。

    Attributes:
        kind: 事件类型标识，取值见 :data:`EVENT_LABELS`。
        player: 关联玩家编号（0/1），不适用时为 -1。
        card_id: 关联卡 ID，不适用时为 0。
        value: 主数值，含义随 ``kind`` 变化。
        extra: 附加数值，含义随 ``kind`` 变化。
        message_id: 原始消息号，便于排查。
        data: 更多数值，含义随 ``kind`` 变化（``move`` 用它装新旧位置：控制者/区域/序号）。
    """

    kind: str
    player: int = -1
    card_id: int = 0
    value: int = 0
    extra: int = 0
    message_id: int = 0
    data: Tuple[int, ...] = ()
    blocks: Tuple[Tuple[int, ...], ...] = ()
    """一条报文里的多组数值（``card_data`` 用它装"一整片区域的卡面数据"：

    每组是 ``(控制者, 区域, 序号, 表示形式, 攻击力, 守备力)``，攻守未知时为 -1）。"""

    @property
    def label(self) -> str:
        """事件类型的中文短标签。"""

        return EVENT_LABELS.get(self.kind, self.kind)


class ProtocolError(ValueError):
    """协议数据异常时抛出。"""


def encode_frame(message_id: int, payload: bytes = b"") -> bytes:
    """把消息号与负载打包成一条可发送的帧。

    Args:
        message_id: :class:`Ctos` 里的消息号。
        payload: 消息负载，可以为空。
    """

    if len(payload) > MAX_FRAME_LENGTH - 1:
        raise ProtocolError(f"负载过长：{len(payload)} 字节")
    return struct.pack("<HB", len(payload) + 1, message_id) + payload


class FrameDecoder:
    """增量式帧解码器。

    TCP 是字节流，一次 ``recv`` 可能拿到半条帧或三条半帧，所以必须缓冲。
    用法是反复调用 :meth:`feed`，把返回的完整帧逐个处理。
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        """当前缓冲区里尚未成帧的字节数。"""

        return len(self._buffer)

    def feed(self, data: bytes) -> List[Frame]:
        """喂入新收到的字节，返回其中所有已完整的帧。"""

        self._buffer.extend(data)
        frames: List[Frame] = []
        while len(self._buffer) >= FRAME_HEADER_SIZE:
            (length_field,) = struct.unpack_from("<H", self._buffer, 0)
            if length_field == 0:
                # 长度为 0 在协议里没有意义，继续留着只会永久卡住解码
                raise ProtocolError("收到长度为 0 的非法帧")
            total_length = FRAME_HEADER_SIZE + length_field
            if len(self._buffer) < total_length:
                break
            payload = bytes(self._buffer[3:total_length])
            frames.append(Frame(self._buffer[2], payload))
            del self._buffer[:total_length]
        return frames


def _require(payload: bytes, size: int, message_id: int) -> None:
    """校验负载长度，不足时报错而不是静默读出垃圾数据。"""

    if len(payload) < size:
        raise ProtocolError(
            f"消息 0x{message_id:02X} 的负载只有 {len(payload)} 字节，至少需要 {size} 字节"
        )


def _parse_update_data(payload: bytes, message_id: int) -> Optional[DuelEvent]:
    """解析 ``MSG_UPDATE_DATA``（内核下发的卡面数据：**攻守的当前值就在这里**）。

    报文形状（与 WindBot 的 ``GameBehavior.OnUpdateData`` + ``ClientCard.Update`` 一致）::

        u8  玩家编号
        u8  区域
        重复到报文结束：
            u32 长度（**含这 4 字节自身**）
            u8[长度-4] 这一张卡的数据块（``flag`` + 按 flag 排列的字段）

    数据块按 `Query` 的位标志取值，顺序与 ``ClientCard.Update`` 逐个字段对齐；
    只有带 ``POSITION`` 位的数据块才知道自己属于哪一格——没带位置的那些**跳过不动**
    （宁可继续用卡库的卡面数值，也不猜一张卡的位置）。

    一条报文里通常装着一整片区域（5~7 格）的数据块，所以这里返回的是**一组**
    ``(控制者, 区域, 序号, 表示形式, 攻击力, 守备力)``，由记录器逐格套用。
    """

    if len(payload) < 2:
        return None
    player, location = payload[0], payload[1]
    blocks: List[Tuple[int, ...]] = []
    offset = 2
    while offset + 4 <= len(payload):
        (length,) = struct.unpack_from("<I", payload, offset)
        if length < 4 or offset + length > len(payload):
            # 长度不合法：后面的字节已经不可信，直接收手（坏包不该拖垮整局记录）
            break
        block = payload[offset + 4 : offset + length]
        offset += length
        if len(block) < 4:
            # ⚠ `len == 4` 是"这一格这次没有数据"的空条目（真报文里占大多数，
            # 例如怪兽区那几帧 —— WindBot 也是 `len > 8` 才去读）。**必须 continue 而不是 break**：
            # 停下来会把这条报文后面那些真有数据的格一起丢掉。
            continue
        walked = _walk_update_block(block)
        if walked is not None:
            blocks.append(walked)
    if not blocks:
        return None
    return DuelEvent("card_data", player=player, value=location, message_id=message_id, blocks=tuple(blocks))


def _parse_update_card(payload: bytes, message_id: int) -> Optional[DuelEvent]:
    """解析 ``MSG_UPDATE_CARD``：**场上这张卡现在的攻守**走的就是它。

    报文形状::

        u8  玩家编号
        u8  区域
        u8  序号
        u32 数据块长度（含自身）
        u8[长度-4] 数据块（``flag`` + 按 flag 排列的字段）

    为什么必须单接这一条：``MSG_UPDATE_DATA``（整片区域）里的**怪兽区**数据块只带
    "卡号 + 位置"，**不带攻守**——真机上抓包确认过（141+141 条怪兽区报文全是 `flag=0x3`）。
    场上怪的当前攻守（含装备/场地加成）只在这条里下发，少了它面板只能显示卡面数值。
    """

    if len(payload) < 7:
        return None
    controller, location, sequence = payload[0], payload[1], payload[2]
    walked = _walk_update_block(payload[7:], need_position=False)
    if walked is None:
        return None
    _block_controller, _block_location, _block_sequence, position, attack, defense = walked
    # 位置以报文头为准（它一定带），表示形式与攻守从数据块里取
    return DuelEvent(
        "card_data",
        player=controller,
        value=location,
        message_id=message_id,
        blocks=((controller, location, sequence, position, attack, defense),),
    )


#: ``Query`` 的位标志（取自 WindBot 的 ``YGOSharp.OCGWrapper.Enums/Query.cs``）
#: 字段的**读取顺序**与 ``ClientCard.Update`` 逐行一致，读错一位后面全歪。
_QUERY_CODE = 0x01
_QUERY_POSITION = 0x02
_QUERY_ALIAS = 0x04
_QUERY_TYPE = 0x08
_QUERY_LEVEL = 0x10
_QUERY_RANK = 0x20
_QUERY_ATTRIBUTE = 0x40
_QUERY_RACE = 0x80
_QUERY_ATTACK = 0x100
_QUERY_DEFENCE = 0x200

#: 走到攻守为止需要依次跳过的字段（``(位, 字节数, 名字)``）。
#: ⚠ **位置位前面还有 code**：忘了跳过它就会把卡号的低字节当成"控制者/区域/序号"读出来。
#: 真机上这一处踩过——面板上每个格子的攻守都不对，抓一份真报文（`YGO_DUMP_FRAMES`）一比对就现形。
#: 攻守之后的字段（原攻守、原因、装备卡、目标卡、超量素材、指示物、等级列表…）这里不需要，
#: 所以列表到守备力为止。
_UPDATE_FIELDS: Tuple[Tuple[int, int, str], ...] = (
    (_QUERY_CODE, 4, "code"),
    (_QUERY_POSITION, 4, "position"),
    (_QUERY_ALIAS, 4, "alias"),
    (_QUERY_TYPE, 4, "type"),
    (_QUERY_LEVEL, 4, "level"),
    (_QUERY_RANK, 4, "rank"),
    (_QUERY_ATTRIBUTE, 4, "attribute"),
    (_QUERY_RACE, 4, "race"),
    (_QUERY_ATTACK, 4, "attack"),
    (_QUERY_DEFENCE, 4, "defence"),
)


def _walk_update_block(
    block: bytes, *, need_position: bool = True
) -> Optional[Tuple[int, int, int, int, int, int]]:
    """按位走一遍数据块，取出 ``(控制者, 区域, 序号, 表示形式, 攻击力, 守备力)``。

    数据块里"没带"的字段保持 ``-1``（攻守为 -1＝内核没说过，不能拿卡库的数值假装是它给的）。

    Args:
        need_position: 是否必须有位置位。``MSG_UPDATE_DATA`` 要靠它才知道这一块属于哪一格，
            所以缺了就整块跳过；``MSG_UPDATE_CARD`` 的"哪一格"在报文头里，不要求块里再带一遍。

    Returns:
        走不通时返回 ``None``（没带位置 / 块被截断——这一块的信息不可信，跳过）。
    """

    if len(block) < 4:
        return None
    (flags,) = struct.unpack_from("<I", block, 0)
    if need_position and not flags & _QUERY_POSITION:
        return None
    controller = location = sequence = position = -1
    attack = defense = -1
    offset = 4
    for flag, size, key in _UPDATE_FIELDS:
        if not flags & flag:
            continue
        if offset + size > len(block):
            # 块本身被截断（长度字段与实际不符）：这一块整个不可信
            return None
        if key == "position":
            controller, location, sequence, position = struct.unpack_from("<BBBB", block, offset)
        elif key == "attack":
            (attack,) = struct.unpack_from("<i", block, offset)
        elif key == "defence":
            (defense,) = struct.unpack_from("<i", block, offset)
        offset += size
    return int(controller), int(location), int(sequence), int(position), int(attack), int(defense)


def parse_game_message(message_id: int, payload: bytes) -> Optional[DuelEvent]:
    """解析一条游戏消息，返回可播报事件；不关心的消息返回 None。

    只处理播报需要的消息。量大的决策类消息（``MSG_SELECT_*``）一律忽略：它们的长度取决于
    当前场面，解析它们既没必要也不安全。

    Raises:
        ProtocolError: 负载长度不足以容纳该消息的必备字段时抛出。
    """

    if message_id == Msg.START:
        # 对局的"开场"消息：本体第一个字节的低 4 位是**我在对局里的玩家号**
        # （WindBot 的 OnStart: ``IsFirst = (type & 0xF) == 0``）。这个号码是权威的：
        # 服务器会为先后手重排它，**和房间座位号不是一回事**（见 recorder 里的说明）。
        if not payload:
            return DuelEvent("duel_start", player=-1, message_id=message_id)
        return DuelEvent("duel_start", player=payload[0] & 0xF, message_id=message_id)

    if message_id == Msg.WIN:
        # 官方字段是 [player, reason]；WindBot 只读第一个字节，这里对 reason 做容错
        if not payload:
            return DuelEvent("win", message_id=message_id)
        reason = payload[1] if len(payload) > 1 else -1
        return DuelEvent("win", player=payload[0], value=reason, message_id=message_id)

    if message_id == Msg.NEW_TURN:
        _require(payload, 1, message_id)
        return DuelEvent("new_turn", player=payload[0], message_id=message_id)

    if message_id == Msg.NEW_PHASE:
        # 2 字节：阶段值最大 512，与同族的 1 字节字段不同
        _require(payload, 2, message_id)
        (phase,) = struct.unpack_from("<h", payload, 0)
        return DuelEvent("new_phase", value=phase, message_id=message_id)

    if message_id in (Msg.SUMMONING, Msg.SPSUMMONING, Msg.FLIPSUMMONING):
        # u32 卡 ID、u8 控制者、u8 位置、i8 序号、i8 表示形式
        _require(payload, 8, message_id)
        code, controller = struct.unpack_from("<IB", payload, 0)
        kind = {
            Msg.SUMMONING: "summon",
            Msg.SPSUMMONING: "sp_summon",
            Msg.FLIPSUMMONING: "flip_summon",
        }[Msg(message_id)]
        # 表示形式（第 8 个字节）也带上：查房出图要靠它画"表侧/里侧、攻击/守备"，
        # 少了它就分不出盖着的卡——那会把里侧的卡面画出来（等于把对手的盖牌公开）。
        return DuelEvent(kind, player=controller, card_id=code, message_id=message_id,
                         data=(payload[7],))

    if message_id == Msg.CHAINING:
        # u32 卡 ID、u8 控制者、u8 位置、i8 序号、i8 子序号、u8 连锁玩家、i16 触发位置、i32 描述
        _require(payload, 15, message_id)
        (code,) = struct.unpack_from("<I", payload, 0)
        return DuelEvent("chain", player=payload[4], card_id=code, message_id=message_id)

    if message_id == Msg.CHAIN_END:
        return DuelEvent("chain_end", message_id=message_id)

    if message_id in (Msg.DAMAGE, Msg.PAY_LPCOST, Msg.RECOVER):
        # u8 玩家、i32 数值
        _require(payload, 5, message_id)
        (value,) = struct.unpack_from("<i", payload, 1)
        kind = {
            Msg.DAMAGE: "damage",
            Msg.PAY_LPCOST: "pay_cost",
            Msg.RECOVER: "recover",
        }[Msg(message_id)]
        return DuelEvent(kind, player=payload[0], value=value, message_id=message_id)

    if message_id == Msg.LPUPDATE:
        # u8 玩家、i32 剩余生命值
        _require(payload, 5, message_id)
        (lp,) = struct.unpack_from("<i", payload, 1)
        return DuelEvent("lp_update", player=payload[0], value=lp, message_id=message_id)

    if message_id == Msg.DRAW:
        # u8 玩家、u8 张数，随后是每张卡的 u32 ID
        _require(payload, 2, message_id)
        count = payload[1]
        # 卡号也要带出来（原来只取了张数）：抽到的卡＝**进过手牌**的卡，是回答
        # "该不该加第几张"唯一的分母（`tools/card_vitality.py`）。对手那侧的卡号通常是 0
        # （客户端看不见），所以只有**观测方自己**的这一份可信——recorder 按座位分开记。
        codes: Tuple[int, ...] = ()
        if count and len(payload) >= 2 + 4 * count:
            codes = struct.unpack_from(f"<{count}I", payload, 2)
        return DuelEvent("draw", player=payload[0], value=count, message_id=message_id, data=codes)

    if message_id == Msg.ATTACK:
        # u8 攻击方控制者/位置/序号/表示形式、u8 被攻击方控制者/位置/序号/表示形式
        _require(payload, 8, message_id)
        return DuelEvent(
            "attack",
            player=payload[0],
            card_id=0,
            # 被攻击方所在区域；为 0 表示没有目标，即直接攻击
            value=payload[5],
            # 攻击方所在区域，便于区分怪兽攻击与直接攻击
            extra=payload[1],
            message_id=message_id,
        )

    if message_id == Msg.SET:
        # u32 卡 ID、u8 控制者、u8 位置、i8 序号、i8 表示形式
        _require(payload, 8, message_id)
        code, controller = struct.unpack_from("<IB", payload, 0)
        # 位置与序号一起带出来：盖放的怪兽/魔陷**只发这一条 MSG_SET**（不是 MSG_MOVE），
        # 少了这两个数就不知道该把这张卡放进哪一格——真机上表现为「场上看不到盖卡」。
        # 第 8 个字节是表示形式（盖放一律是里侧）：查房出图要靠它画卡背。
        return DuelEvent(
            "set",
            player=controller,
            card_id=code,
            message_id=message_id,
            data=(payload[5], payload[6], payload[7]),  # 位置、序号、表示形式
        )

    if message_id == Msg.UPDATE_DATA:
        return _parse_update_data(payload, message_id)

    if message_id == Msg.UPDATE_CARD:
        return _parse_update_card(payload, message_id)

    if message_id == Msg.POS_CHANGE:
        # u32 卡 ID、u8 控制者、u8 位置、i8 序号、i8 原表示形式、i8 新表示形式
        # （与 WindBot 的 ``OnPosChange`` 一致）。原来这条报文整条没人接——于是"翻开盖牌/改成守备"
        # 在查房与出图里都看不到，效果也一并漏记（`flip_summon` 之外的翻面走的就是它）。
        _require(payload, 9, message_id)
        (code,) = struct.unpack_from("<I", payload, 0)
        return DuelEvent(
            "pos_change",
            player=payload[4],
            card_id=code,
            message_id=message_id,
            data=(payload[5], payload[6], payload[8], payload[7]),   # 区域、序号、新形式、原形式
        )

    if message_id == Msg.MOVE:
        # u32 卡 ID、原位置四元组（u8/i8×4）、新位置四元组、u32 原因
        _require(payload, 16, message_id)
        (code,) = struct.unpack_from("<I", payload, 0)
        return DuelEvent(
            "move",
            player=payload[4],
            card_id=code,
            value=payload[5],
            extra=payload[9],
            message_id=message_id,
            # 原/新位置的控制者、区域、序号：查房要靠它维护场地占用；
            # 第 7 项是**新位置的表示形式**（查房出图要画"表侧/里侧、攻击/守备"）
            data=(
                payload[4],
                payload[5],
                payload[6],
                payload[8],
                payload[9],
                payload[10],
                payload[11],
            ),
        )

    return None


def parse_frame_payload(payload: bytes) -> Optional[DuelEvent]:
    """解析一帧 ``STOC_GAME_MSG`` 的负载。

    负载的第一个字节是游戏消息号，后面是消息本体。按照服务器实现，一帧只装一条消息，
    所以这里不尝试在帧内继续切分——那需要每种消息的隐式长度表，而这是没必要的。
    """

    if not payload:
        return None
    return parse_game_message(payload[0], payload[1:])


def decode_wide_string(data: bytes, chars: int = 20) -> str:
    """解码协议里的 UTF-16LE 定长字符串字段，去掉 NUL 填充。

    Args:
        data: 以该字段开头的字节串，超出 ``chars`` 个字符的部分会被忽略。
        chars: 字段容量（协议里昵称与房间密码都是 20 个 UTF-16 字符）。
    """

    text = data[: chars * 2].decode("utf-16-le", errors="ignore")
    return text.split("\x00", 1)[0].strip()


def parse_player_info_name(payload: bytes) -> str:
    """从 ``CTOS_PLAYER_INFO`` 负载里取出玩家昵称。

    结构：``uint16 name[20]``，共 40 字节。
    """

    _require(payload, 40, Ctos.PLAYER_INFO)
    return decode_wide_string(payload, 20)


def parse_join_game_password(payload: bytes) -> str:
    """从 ``CTOS_JOIN_GAME`` 负载里取出 pass 字段。

    结构（服务端 ``gframe/network.h`` 与 WindBot 发送端一致，共 48 字节）::

        uint16 version      // 偏移 0
        byte   padding[2]   // 偏移 2
        uint32 gameid       // 偏移 4
        uint16 pass[20]     // 偏移 8，UTF-16LE

    客户端「加入房间」界面里填在密码框的内容会原样落在这个字段，闸门就是靠它校验口令。
    """

    _require(payload, 48, Ctos.JOIN_GAME)
    return decode_wide_string(payload[8:], 20)


def parse_join_game_version(payload: bytes) -> int:
    """从 ``CTOS_JOIN_GAME`` 负载里取出客户端协议版本号。

    自建房间下用不到版本校验，但把它记进日志能在客户端连不上时快速定位原因。
    """

    _require(payload, 2, Ctos.JOIN_GAME)
    return struct.unpack_from("<H", payload, 0)[0]


def encode_chat_payload(text: str) -> bytes:
    """把一句话编码成 ``CTOS_CHAT`` 的负载。

    格式是**UTF-16LE 字符串 + 一个 NUL 终止符、没有长度前缀**：

    * 服务端 ``netserver.cpp`` 对 ``CTOS_CHAT`` 只校验「字节数为偶数」与「不超过
      ``LEN_CHAT_MSG`` 个码元」，不读长度字段；
    * 但 ``NetServer::CreateChatPacket`` 会检查**最后一个码元是否为 0**，不是就直接返回 0，
      整条聊天被静默丢弃——所以终止符必须自己补上。实测漏掉它时，内核一声不吭地吞掉台词。
      这正是 WindBot 用 ``WriteUnicodeAutoLength`` 而不是裸写字符串的原因。

    终止符占掉一个码元，所以实际文本上限是 ``LEN_CHAT_MSG - 1``。
    注意 ``STOC_CHAT`` 的格式不同（它前面还有一个 uint16 的发送者类型），两者不能互套。

    Raises:
        ProtocolError: 内容为空或超过字符上限时抛出。
    """

    if not text.strip():
        raise ProtocolError("聊天内容为空")
    if len(text) > LEN_CHAT_MSG - 1:
        raise ProtocolError(f"聊天内容过长：{len(text)} 个字符，上限 {LEN_CHAT_MSG - 1}")
    return text.encode("utf-16-le") + b"\x00\x00"


def parse_stoc_error(payload: bytes) -> Tuple[int, int]:
    """解析 ``STOC_ERROR_MSG``，返回 ``(消息号, 附带码)``。

    布局与 WindBot 的 ``Game/GameBehavior.cs::OnErrorMsg`` 一致：1 字节消息号 + 3 字节对齐
    + int32 附带码。``消息号 == 2``（``ERRMSG_DECKERROR``）表示内核不收这副卡组，此时附带码
    带的是出问题的那张卡（或张数）的信息——实测"张数超上限"时这个值为 0。
    """

    _require(payload, 8, Stoc.ERROR_MSG)
    msg = payload[0]
    pcode = struct.unpack_from("<i", payload, 4)[0]
    return msg, pcode


def parse_type_change(payload: bytes) -> tuple:
    """解析 ``STOC_TYPE_CHANGE``，返回 ``(座位号, 是否房主)``。

    结构与 WindBot 的 ``OnTypeChange`` 一致：低 4 位是座位号，高 4 位非零表示房主。
    """

    _require(payload, 1, Stoc.TYPE_CHANGE)
    value = payload[0]
    return value & 0xF, ((value >> 4) & 0xF) != 0


def parse_player_enter(payload: bytes) -> tuple:
    """解析 ``STOC_HS_PLAYER_ENTER``，返回 ``(昵称, 座位号)``。

    结构：``uint16 name[20]`` + ``uint8 pos``。
    """

    _require(payload, 41, Stoc.HS_PLAYER_ENTER)
    return decode_wide_string(payload, 20), payload[40]
