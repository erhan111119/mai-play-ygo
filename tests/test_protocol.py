"""协议层单元测试。

直接用 ``python tests/test_protocol.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import struct
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.protocol import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    CardLocation,
    Ctos,
    DuelEvent,
    FrameDecoder,
    Msg,
    ProtocolError,
    Stoc,
    encode_chat_payload,
    encode_frame,
    parse_frame_payload,
    parse_game_message,
    parse_join_game_password,
    parse_join_game_version,
    parse_player_enter,
    parse_player_info_name,
    parse_stoc_error,
    parse_type_change,
)


def test_split_and_coalesced_frames() -> None:
    """半条帧要能续上，一次收到多条帧要全部吐出。"""

    first = encode_frame(Stoc.GAME_MSG, b"\x28\x01")
    second = encode_frame(Stoc.CHAT, b"hello")
    decoder = FrameDecoder()

    # 先喂前半条，此时不应产出任何帧
    half = len(first) // 2
    assert decoder.feed(first[:half]) == []
    assert decoder.buffered_bytes == half

    # 补齐剩余部分，并一次性再喂第二条
    frames = decoder.feed(first[half:] + second)
    assert len(frames) == 2
    assert frames[0].message_id == Stoc.GAME_MSG
    assert frames[0].payload == b"\x28\x01"
    assert frames[1].message_id == Stoc.CHAT
    assert frames[1].payload == b"hello"
    assert decoder.buffered_bytes == 0


def test_frame_length_semantics() -> None:
    """长度字段的值应当等于「消息号 + payload」的字节数。"""

    frame = encode_frame(Ctos.HS_READY, b"")
    assert frame == struct.pack("<HB", 1, Ctos.HS_READY)
    assert len(frame) == 3

    frame = encode_frame(Ctos.RESPONSE, b"\x00" * 10)
    length_field = struct.unpack_from("<H", frame, 0)[0]
    assert length_field == 11
    assert len(frame) == 2 + length_field


def test_zero_length_frame_rejected() -> None:
    """长度为 0 的帧必须报错，否则解码器会永久卡住。"""

    decoder = FrameDecoder()
    try:
        decoder.feed(b"\x00\x00\x99")
    except ProtocolError:
        return
    raise AssertionError("长度为 0 的帧本应抛出 ProtocolError")


def test_message_ids_match_windbot_enum() -> None:
    """消息号是双重核对过的，这里作为回归护栏固定下来。"""

    expected = {
        "WIN": 5,
        "SELECT_IDLECMD": 11,
        "CONFIRM_DECKTOP": 30,
        "NEW_TURN": 40,
        "NEW_PHASE": 41,
        "MOVE": 50,
        "SET": 54,
        "SUMMONING": 60,
        "SPSUMMONING": 62,
        "FLIPSUMMONING": 64,
        "CHAINING": 70,
        "CHAIN_END": 74,
        "DRAW": 90,
        "DAMAGE": 91,
        "LPUPDATE": 94,
        "PAY_LPCOST": 100,
        "ATTACK": 110,
        "HAND_RES": 133,
        "PLAYER_HINT": 165,
    }
    for name, value in expected.items():
        assert int(Msg[name]) == value, f"{name} 期望 {value}，实际 {int(Msg[name])}"


def test_win_event() -> None:
    """MSG_WIN 要解出获胜方与结束原因。"""

    event = parse_game_message(Msg.WIN, bytes([0, 2]))
    assert event is not None
    assert event.kind == "win"
    assert event.player == 0
    assert event.value == 2

    # reason 缺失时不应报错，只把原因置为未知
    event = parse_game_message(Msg.WIN, bytes([1]))
    assert event is not None
    assert event.player == 1
    assert event.value == -1


def test_new_phase_reads_two_bytes() -> None:
    """阶段值是 2 字节且可以超过 255，这是最容易搞错的地方。"""

    event = parse_game_message(Msg.NEW_PHASE, struct.pack("<h", 512))
    assert event is not None
    assert event.value == 512
    assert event.label == "阶段推进"


def test_summoning_variants() -> None:
    """三种召唤消息共用一套字段布局，但事件类型要区分开。"""

    payload = struct.pack("<IBhh", 89631139, 1, 2, 1)
    for msg, kind in (
        (Msg.SUMMONING, "summon"),
        (Msg.SPSUMMONING, "sp_summon"),
        (Msg.FLIPSUMMONING, "flip_summon"),
    ):
        event = parse_game_message(msg, payload)
        assert event is not None
        assert event.kind == kind
        assert event.card_id == 89631139
        assert event.player == 1


def test_chaining_event() -> None:
    """连锁事件要能取出被发动的卡。"""

    payload = struct.pack("<IBBbbBhi", 46986414, 0, 4, 0, 0, 0, 0, 1234)
    event = parse_game_message(Msg.CHAINING, payload)
    assert event is not None
    assert event.kind == "chain"
    assert event.card_id == 46986414
    assert event.player == 0


def test_life_point_events() -> None:
    """伤害、回复、支付代价、LP 更新各自解出正确数值。"""

    damage = parse_game_message(Msg.DAMAGE, struct.pack("<Bi", 1, 2400))
    assert damage is not None and damage.kind == "damage" and damage.value == 2400 and damage.player == 1

    recover = parse_game_message(Msg.RECOVER, struct.pack("<Bi", 0, 1000))
    assert recover is not None and recover.kind == "recover" and recover.value == 1000

    cost = parse_game_message(Msg.PAY_LPCOST, struct.pack("<Bi", 0, 800))
    assert cost is not None and cost.kind == "pay_cost" and cost.value == 800

    update = parse_game_message(Msg.LPUPDATE, struct.pack("<Bi", 0, 5600))
    assert update is not None and update.kind == "lp_update" and update.value == 5600


def test_attack_direct_and_targeted() -> None:
    """攻击方位置为 0 时是直接攻击，否则要能读出被攻击方所在区域。"""

    # 攻击方 0 号玩家怪兽区序号 1，目标为 1 号玩家怪兽区序号 0
    payload = bytes([0, int(CardLocation.MONSTER_ZONE), 1, 1, 1, int(CardLocation.MONSTER_ZONE), 0, 1])
    event = parse_game_message(Msg.ATTACK, payload)
    assert event is not None
    assert event.player == 0
    assert event.value == int(CardLocation.MONSTER_ZONE)
    assert event.value != 0, "有目标的攻击不该被当成直接攻击"

    direct = bytes([0, int(CardLocation.MONSTER_ZONE), 1, 1, 0, 0, 0, 0])
    event = parse_game_message(Msg.ATTACK, direct)
    assert event is not None
    assert event.value == 0, "被攻击方区域为 0 表示直接攻击"


def test_move_event() -> None:
    """卡片移动要能读出原区域与目标区域。"""

    payload = struct.pack(
        "<IBBbBBBbBI",
        89631139,
        0,
        int(CardLocation.HAND),
        0,
        1,
        0,
        int(CardLocation.GRAVE),
        0,
        1,
        0x40,
    )
    event = parse_game_message(Msg.MOVE, payload)
    assert event is not None
    assert event.kind == "move"
    assert event.card_id == 89631139
    assert event.value == int(CardLocation.HAND)
    assert event.extra == int(CardLocation.GRAVE)


def test_ignored_and_malformed_messages() -> None:
    """不关心的消息返回 None；负载过短的已知消息要报错而不是读出垃圾。"""

    assert parse_game_message(Msg.SELECT_IDLECMD, b"\x00" * 32) is None

    try:
        parse_game_message(Msg.NEW_PHASE, b"\x01")
    except ProtocolError:
        pass
    else:
        raise AssertionError("1 字节的 MSG_NEW_PHASE 本应抛出 ProtocolError")


def test_parse_frame_payload() -> None:
    """整帧负载的入口要按「首字节是消息号」处理。"""

    event = parse_frame_payload(bytes([int(Msg.NEW_TURN), 1]))
    assert isinstance(event, DuelEvent)
    assert event.kind == "new_turn"
    assert event.player == 1

    assert parse_frame_payload(b"") is None
    assert parse_frame_payload(bytes([int(Msg.SELECT_CARD)]) + b"\x00" * 8) is None


def test_encode_chat_payload_format() -> None:
    """CTOS_CHAT 是 UTF-16LE + NUL 终止符、没有长度前缀（与 STOC_CHAT 不同）。

    终止符这条是要命的：内核 ``CreateChatPacket`` 检查最后一个码元是否为 0，
    不补终止符的话整条聊天会被静默丢弃——实测漏掉时对手一个字节都收不到。
    """

    payload = encode_chat_payload("你好")
    assert payload == "你好".encode("utf-16-le") + b"\x00\x00", payload
    # 前两字节就是第一个字符，说明没有长度前缀
    assert payload[:2] == "你".encode("utf-16-le")
    # 末两字节是终止符
    assert payload[-2:] == b"\x00\x00"
    assert len(payload) % 2 == 0, "服务端要求字节数为偶数"

    # 终止符占一个码元，所以文本上限是 LEN_CHAT_MSG - 1
    assert len(encode_chat_payload("a" * 255)) == 512
    for bad in ("", "   ", "a" * 256):
        try:
            encode_chat_payload(bad)
        except ProtocolError:
            continue
        raise AssertionError(f"{bad[:12]!r} 本应抛出 ProtocolError")


def test_handshake_field_layouts() -> None:
    """握手字段的偏移是闸门校验密码的基础，必须逐字节钉住。"""

    # CTOS_JOIN_GAME：u16 版本 + 2 字节填充 + u32 gameid + 20 个 UTF-16 字符的 pass，共 48 字节
    payload = struct.pack("<H", 0x1362) + b"\x00\x00" + struct.pack("<I", 0) + "mai#8k3f".encode(
        "utf-16-le"
    ).ljust(40, b"\x00")
    assert len(payload) == 48
    assert parse_join_game_password(payload) == "mai#8k3f"
    assert parse_join_game_version(payload) == 0x1362

    # CTOS_PLAYER_INFO：20 个 UTF-16 字符的昵称
    name_payload = "群友A".encode("utf-16-le").ljust(40, b"\x00")
    assert parse_player_info_name(name_payload) == "群友A"

    # STOC_TYPE_CHANGE：低 4 位座位号，高 4 位房主标志
    assert parse_type_change(bytes([0x02])) == (2, False)
    assert parse_type_change(bytes([0x10])) == (0, True)
    assert parse_type_change(bytes([0x11])) == (1, True)


def test_stoc_error_layout() -> None:
    """STOC_ERROR_MSG 是 1 字节消息号 + 3 字节对齐 + int32 附带码。

    消息号 2（DECKERROR）是"内核不收这副卡组"的唯一线索：实测张数超上限时收到的就是它，
    附带码为 0。布局按 WindBot 的 ``OnErrorMsg`` 写，所以这里也照那 8 字节验一遍。
    """

    payload = bytes([2]) + b"\x00\x00\x00" + struct.pack("<i", 0)
    assert parse_stoc_error(payload) == (2, 0)

    # 附带卡号的情形（高 4 位是类别标志，低 28 位是卡号）
    card_payload = bytes([2]) + b"\x00\x00\x00" + struct.pack("<i", 0x10000000 | 89631139)
    msg, pcode = parse_stoc_error(card_payload)
    assert msg == 2
    assert pcode & 0xFFFFFFF == 89631139

    try:
        parse_stoc_error(bytes([2]) + b"\x00" * 6)
    except ProtocolError:
        pass
    else:
        raise AssertionError("不足 8 字节的 ERROR_MSG 本应报错")


def test_player_enter_layout() -> None:
    """STOC_HS_PLAYER_ENTER 是 40 字节昵称加 1 字节座位号。"""

    payload = "WindBot".encode("utf-16-le").ljust(40, b"\x00") + bytes([1])
    assert len(payload) == 41
    name, position = parse_player_enter(payload)
    assert name == "WindBot"
    assert position == 1


def test_short_handshake_payloads_rejected() -> None:
    """握手负载不完整时要报错，避免把半包数据当成有效密码。"""

    for func, payload in (
        (parse_join_game_password, b"\x00" * 47),
        (parse_player_info_name, b"\x00" * 39),
        (parse_player_enter, b"\x00" * 40),
    ):
        try:
            func(payload)
        except ProtocolError:
            continue
        raise AssertionError(f"{func.__name__} 对不完整负载本应抛出 ProtocolError")


def main() -> int:
    """无 pytest 环境下逐个执行测试函数。"""

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
