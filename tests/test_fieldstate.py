"""局面跟踪与挑衅台词的单元测试。

这两块都是「看报文/掷骰子」的纯逻辑，不需要进程，也不联网。

直接用 ``python tests/test_fieldstate.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import random
import struct
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.fieldstate import FieldState  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.protocol import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    CardLocation,
    DuelEvent,
    Frame,
    Msg,
    Stoc,
    parse_game_message,
)
from duel.recorder import DuelRecorder  # noqa: E402
from duel.taunts import TAUNT_LINES, TauntPicker  # noqa: E402

MONSTER = int(CardLocation.MONSTER_ZONE)
SPELL = int(CardLocation.SPELL_ZONE)
GRAVE = int(CardLocation.GRAVE)
HAND = int(CardLocation.HAND)


def move(card_id: int, *, old: tuple, new: tuple) -> DuelEvent:
    """造一条 move 事件（控制者/区域/序号 三元组）。"""

    return DuelEvent(
        "move",
        player=old[0],
        card_id=card_id,
        value=old[1],
        extra=new[1],
        data=(old[0], old[1], old[2], new[0], new[1], new[2]),
    )


def enter_room(
    recorder: DuelRecorder,
    *,
    room_seat: int,
    duel_index: int,
    names: dict,
) -> DuelRecorder:
    """按真实报文把记录器推进到「已入房、已开打」：房间座位、昵称、对局玩家号。

    三个报文**都走真实的解析路径**（不是直接赋属性），这样编号映射那套逻辑也在覆盖范围内：
    ``TYPE_CHANGE``（我在房间里的槽位）→ ``HS_PLAYER_ENTER``（各槽位的昵称）→
    ``MSG_START``（我在**对局里**的玩家号，服务器会为先后手重排它）。
    """

    recorder.feed("to_client", Frame(Stoc.TYPE_CHANGE, bytes([room_seat | (1 << 4)])))
    for slot, name in names.items():
        payload = name.encode("utf-16-le").ljust(40, b"\x00") + bytes([slot])
        recorder.feed("to_client", Frame(Stoc.HS_PLAYER_ENTER, payload))
    recorder.feed("to_client", Frame(Stoc.GAME_MSG, bytes([Msg.START, duel_index])))
    return recorder


def test_lp_update_and_start_value() -> None:
    """双方生命值要能从 lp_update 跟踪，且开局前显示的是配置的初始值。"""

    field = FieldState(start_lp=8000)
    assert field.players[0].lp == 8000 and field.players[1].lp == 8000

    field.apply(DuelEvent("lp_update", player=1, value=6200))
    assert field.players[1].lp == 6200
    assert field.players[0].lp == 8000, "另一方的生命值不该被带动"

    field.apply(DuelEvent("lp_update", player=0, value=3400))
    assert field.players[0].lp == 3400


def test_lp_follows_damage_events() -> None:
    """这套内核不为每次掉血发 lp_update，所以生命值要按伤害/回复事件自己累计。

    这条是实测教训的护栏：自检对局里 bot 掉了 8500 血，而 /查房 报出来的还是满血 8000——
    因为整局都没收到一条 MSG_LPUPDATE。
    """

    field = FieldState(start_lp=8000)
    field.apply(DuelEvent("damage", player=0, value=2500))
    assert field.players[0].lp == 5500
    field.apply(DuelEvent("recover", player=0, value=500))
    assert field.players[0].lp == 6000
    field.apply(DuelEvent("pay_cost", player=0, value=1000))
    assert field.players[0].lp == 5000
    assert field.players[1].lp == 8000, "对手的生命值不该被带动"

    # 伤害超过剩余生命值时按 0 截断（内核里生命值也是这样）
    field.apply(DuelEvent("damage", player=0, value=9999))
    assert field.players[0].lp == 0

    # lp_update 到了就以它为准（权威覆盖，可用于纠正累计误差）
    field.apply(DuelEvent("lp_update", player=0, value=1234))
    assert field.players[0].lp == 1234


def test_set_card_lands_in_its_zone_and_kernel_stats_are_applied() -> None:
    """盖放要真的进格位表，内核下发的攻守/表示形式要覆盖到那张卡上。

    两条都是真机上"看不到"的来源：

    * 盖放**只发 MSG_SET**（不是 MSG_MOVE），第一版没处理它 → 场上的盖卡整片看不见；
    * 攻守只有内核的 `MSG_UPDATE_DATA` 说了才有（装备/场地加成之后与卡面数值不一样），
      第一版压根没记 → 面板上永远没有攻守角标。
    """

    field = FieldState()
    # 自己盖一张魔陷到魔陷区 2 号位（里侧守备位 0x8）
    field.apply(DuelEvent("set", player=0, card_id=300, data=(SPELL, 1, 0x8)))
    card = field.zones.get((0, SPELL, 1))
    assert card is not None, "盖卡必须进格位表，否则面板上那一格是空的"
    assert card.card_id == 300 and card.face_up is False, card
    assert field.players[0].spells == 1, "盖放的魔陷也要算进占用数"
    assert card.known_stats is False, "内核还没说过攻守，就不能假装知道"

    # 内核下发这一片的当前数据：把这张翻开成表侧攻击，攻守 3000/2500
    field.apply(DuelEvent("card_data", player=0, blocks=((0, SPELL, 1, 0x1, 3000, 2500),)))
    assert card.face_up is True and card.attack_position is True, card
    assert (card.attack, card.defense) == (3000, 2500), card
    assert card.known_stats is True

    # 数据块里位置对不上（别的格）就不该动到这一张
    field.apply(DuelEvent("card_data", player=0, blocks=((0, SPELL, 3, 0x4, 1800, 1200),)))
    assert (card.attack, card.defense) == (3000, 2500), card


def test_update_data_message_parses_blocks_with_position_and_stats() -> None:
    """`MSG_UPDATE_DATA` 的报文形状要解对（攻守的**当前值**只有它给）。"""

    def block(flags: int, body: bytes) -> bytes:
        """按 WindBot 的读法造一块：u32 长度（含自身与 flag）+ u32 flag + 按 flag 排的字段。

        长度值 = 4（长度字段自身）+ 4（flag）+ len(body)，即"这一块的线上总字节数"。
        """

        return struct.pack("<I", len(body) + 8) + struct.pack("<I", flags) + body

    # 位置位（0x02）+ 攻击力（0x100）+ 守备力（0x200）
    with_position = block(
        0x02 | 0x100 | 0x200,
        struct.pack("<BBBB", 0, MONSTER, 1, 0x1) + struct.pack("<ii", 3000, 2500),
    )
    # 只有卡号、没有位置：不知道是哪一格，应当被跳过
    code_only = block(0x01, struct.pack("<I", 89631139))
    payload = bytes([0, MONSTER]) + with_position + code_only

    event = parse_game_message(Msg.UPDATE_DATA, payload)
    assert event is not None and event.kind == "card_data", event
    assert event.blocks == ((0, MONSTER, 1, 0x1, 3000, 2500),), event.blocks

    # 一整片区域里没有一块带位置的：这条报文没有可用信息
    assert parse_game_message(Msg.UPDATE_DATA, bytes([0, MONSTER]) + code_only) is None


def test_set_message_carries_the_zone_it_lands_in() -> None:
    """`MSG_SET` 要带出"哪一格"：不然盖放收不进格位表（场上看不到盖卡）。"""

    payload = struct.pack("<IBBBB", 89631139, 1, MONSTER, 2, 0x8)
    event = parse_game_message(Msg.SET, payload)
    assert event is not None and event.kind == "set", event
    assert event.player == 1 and event.card_id == 89631139, event
    assert event.data == (MONSTER, 2, 0x8), event.data


def test_zone_occupancy_follows_moves() -> None:
    """怪兽区与魔陷区的占用要跟着 move 增删，同一张卡移走就不会再被算。"""

    field = FieldState()
    # 自己通常召唤一只怪兽：手牌 → 怪兽区 1 号位
    field.apply(move(89631139, old=(0, HAND, 0), new=(0, MONSTER, 0)))
    assert field.players[0].monsters == 1
    # 盖一张魔陷
    field.apply(move(44095762, old=(0, HAND, 3), new=(0, SPELL, 0)))
    assert field.players[0].spells == 1
    # 再召唤一只
    field.apply(move(10000, old=(0, HAND, 2), new=(0, MONSTER, 1)))
    assert field.players[0].monsters == 2
    # 对手也铺场
    field.apply(move(12345, old=(1, HAND, 0), new=(1, MONSTER, 0)))
    field.apply(move(23456, old=(1, HAND, 1), new=(1, SPELL, 1)))
    assert (field.players[1].monsters, field.players[1].spells) == (1, 1)

    # 一号位的怪兽进墓地：怪兽数要减回去
    field.apply(move(89631139, old=(0, MONSTER, 0), new=(0, GRAVE, 0)))
    assert field.players[0].monsters == 1, "移走的那只不该继续算"
    assert field.players[0].spells == 1

    # 换位置（同一区域内移动）不该重复计数
    field.apply(move(10000, old=(0, MONSTER, 1), new=(0, MONSTER, 3)))
    assert field.players[0].monsters == 1


def test_control_change_swaps_side() -> None:
    """控制权转移后，这只怪兽要算到新控制者头上。"""

    field = FieldState()
    field.apply(move(89631139, old=(0, HAND, 0), new=(0, MONSTER, 0)))
    assert field.players[0].monsters == 1 and field.players[1].monsters == 0
    # 被对手抢过去（原位置：自己怪兽区；新位置：对手怪兽区）
    field.apply(move(89631139, old=(0, MONSTER, 0), new=(1, MONSTER, 0)))
    assert field.players[0].monsters == 0
    assert field.players[1].monsters == 1


def test_snapshot_lines_follow_self_seat() -> None:
    """快照要按「自己在前、对手在后」给两行，且带上名字。"""

    # 麦麦在房间 1 号槽位、对局里也是 1 号玩家（两套编号一致的情况）
    recorder = enter_room(
        DuelRecorder(start_lp=8000), room_seat=1, duel_index=1, names={0: "打憨憨", 1: "憨憨"}
    )
    recorder._apply(DuelEvent("lp_update", player=1, value=6200))
    recorder._apply(move(89631139, old=(1, HAND, 0), new=(1, MONSTER, 0)))
    recorder._apply(move(44095762, old=(0, HAND, 0), new=(0, SPELL, 2)))

    lines = recorder.field_snapshot()
    # 自己在前、对手在后；有怪的一方多一行"站位"（格号也报出来，见下面的专项测试）
    assert len(lines) == 3, lines
    assert lines[0].startswith("憨憨（我方）：LP 6200｜怪兽 1｜魔陷 0"), lines[0]
    assert lines[1] == "　站位：主怪兽区1 #89631139", lines[1]
    assert lines[2].startswith("打憨憨（对方）：LP 8000｜怪兽 0｜魔陷 1"), lines[2]


def test_slots_name_the_extra_monster_zone() -> None:
    """额外怪兽区的怪要点名报出来，并记进"落位"清单。

    护栏来源：**放置不是 AI 与脚本决定的**（WindBot 的 ``GameAI.OnSelectPlace`` 自己挑格，
    优先级是主怪兽区优先、额外怪兽区最后），问题文件里也不含格号——用户问了两次
    "大姐为什么没放额外怪兽区"，而当时除了这份记录没有任何地方看得见格号。
    """

    recorder = DuelRecorder(start_lp=8000)
    recorder._apply(move(100267023, old=(0, int(CardLocation.EXTRA), 0), new=(0, MONSTER, 5)))
    joined = "\n".join(recorder.field_snapshot())
    assert "　站位：额外怪兽区 #100267023" in joined, joined
    assert recorder.placements, "落位清单要记下来"
    assert "额外怪兽区" in recorder.placements[-1], recorder.placements
    # 主怪兽区的怪照旧报主区（别把格号搞混）
    recorder._apply(move(100267016, old=(0, HAND, 0), new=(0, MONSTER, 2)))
    assert "主怪兽区3 #100267016" in "\n".join(recorder.field_snapshot()), recorder.field_snapshot()


def test_spell_placement_is_recorded_with_zone_name() -> None:
    """魔陷/场地落位也要记（``spell_zone_label``）：验收①里的场地件只有这里说得清。

    `first_turn_accept.py` 就是靠这份记录回答"未眠之城第 1 回合摆上去了没有"——
    落位原来只记怪兽，场地件只能去客户端流里按卡名数，而我们的卡在对方客户端上
    有时是 ``UnKnowCard``（实测同一份日志里"达标 2/28"与"大姐站额外区 27/30 局"对不上）。
    """

    recorder = DuelRecorder(start_lp=8000)
    # 场地魔法进魔陷区第 6 格（序号 5＝场地区）、普通魔陷进第 3 格（序号 2）
    recorder._apply(move(72978038, old=(0, int(CardLocation.HAND), 0), new=(0, SPELL, 5)))
    recorder._apply(move(30271097, old=(0, int(CardLocation.HAND), 1), new=(0, SPELL, 2)))
    assert recorder.placements, "落位清单要记下来"
    assert "场地区 ← #72978038" in recorder.placements[0], recorder.placements
    assert "魔法陷阱区3 ← #30271097" in recorder.placements[1], recorder.placements


def test_recorder_passes_start_lp_to_field_state() -> None:
    """记录器的初始生命值要透到局面快照里。

    内核按房间设置的 LP 开局（训练时 4000 是为了缩短单局），快照却按内核默认的
    8000 起算，于是"没怎么掉血"的对局会报出对不上的数字——训练日志里的 LP 列
    和胜负原因（基本分变成0）自相矛盾，就是这么来的。
    """

    recorder = DuelRecorder(start_lp=4000)
    assert recorder.field_state.players[0].lp == 4000
    assert recorder.field_state.players[1].lp == 4000
    recorder._apply(DuelEvent("damage", player=0, value=1500))
    assert recorder.field_state.players[0].lp == 2500
    # 掉血不会掉到负数：LP 归零后还有溢出伤害时仍报 0
    recorder._apply(DuelEvent("damage", player=0, value=99999))
    assert recorder.field_state.players[0].lp == 0


def test_room_seat_and_duel_index_can_differ() -> None:
    """房间座位 ≠ 对局玩家号时，双方信息与胜负都不能说反。

    **这是实测踩的坑**：麦麦先入房（``TYPE_CHANGE`` 报房间座位 0），内核却在 ``MSG_START``
    里把对局玩家号给成 1（WindBot 的 ``OnStart`` 为这个写了注释：服务器会为先后手重排玩家号，
    房间昵称仍留在入房槽位）。对局报文的 player 字段装的是**对局玩家号**，所以：

    * 拿房间座位当参照系 ⇒ 双方信息互换、胜负说反（群里会出现"憨憨 输掉了这一局"，
      而实际输的是群友）；
    * 昵称是按入房槽位送来的 ⇒ 也要经过一次交换才能贴到对的那一方头上。
    """

    recorder = enter_room(
        DuelRecorder(start_lp=8000),
        room_seat=0,  # 麦麦先入房，占 0 号槽位
        duel_index=1,  # 但内核把对局玩家号给了 1
        names={0: "憨憨", 1: "群友A"},
    )
    assert recorder.self_seat == 1, "对局里的『我方』是对局玩家号"

    # 对局玩家号 1 = 麦麦自己：掉血要记在麦麦头上
    recorder._apply(DuelEvent("damage", player=1, value=300))
    assert recorder.field_state.players[1].lp == 7700, "伤害该记在麦麦（对局号 1）头上"
    assert recorder.field_state.players[0].lp == 8000, "对手不该掉血"

    # move 的控制者同理
    recorder._apply(
        DuelEvent(
            "move",
            card_id=89631139,
            data=(0, HAND, 0, 0, MONSTER, 0),  # 对局号 0 = 对手
        )
    )
    assert recorder.field_state.players[0].monsters == 1, "该记在对手场地"
    assert recorder.field_state.players[1].monsters == 0

    # 名字要跟着交换后的对应关系走：0 号槽位的"憨憨"属于对局号 1
    lines = recorder.field_snapshot()
    assert lines[0].startswith("憨憨（我方）"), lines[0]
    assert lines[1].startswith("群友A（对方）"), lines[1]

    # 胜负：对局号 1 获胜 = 麦麦赢（旧代码在这里会报"输了"，就是群里那条反的播报）
    recorder._apply(DuelEvent("win", player=1, value=1))
    assert recorder.winner_seat == 1
    assert recorder.to_dict()["winner_is_self"] is True

    # 对局号 0 获胜 = 群友赢
    other = enter_room(
        DuelRecorder(start_lp=8000), room_seat=0, duel_index=1, names={0: "憨憨", 1: "群友A"}
    )
    other._apply(DuelEvent("win", player=0, value=2))
    assert other.to_dict()["winner_is_self"] is False
    assert "（对方）" in str(other.to_dict()["winner_name"]), other.to_dict()["winner_name"]

    # 对局号还不知道（还没收到 MSG_START）时不瞎猜：字段保持原值，别按房间座位换一遍
    unknown = DuelRecorder(start_lp=8000)
    unknown._apply(DuelEvent("damage", player=0, value=100))
    assert unknown.field_state.players[0].lp == 7900


def test_result_marks_self_and_opponent() -> None:
    """结果必须带上「赢的是不是我」这种一眼可判的事实，并且标签要标出谁是麦麦。

    护栏来自实测：群友赢了，播报却说麦麦赢了。原因是机器记录里双方只有各自客户端里的
    名字（麦麦在游戏内叫"憨憨"），而写播报的模型只能自己猜"憨憨是不是我"——猜反就说反。
    所以标签一律带身份，胜负额外给一个布尔事实。
    """

    recorder = DuelRecorder(start_lp=8000)
    recorder._set_player_name(0, "群友A")
    recorder._set_player_name(1, "憨憨")  # 麦麦在游戏里用的名字
    recorder.room_seat = 1
    recorder.duel_index = 1  # 两套编号一致：憨憨在房间 1 号槽位、也是对局号 1
    recorder._refresh_names()
    recorder.started = True
    recorder.finished = True
    # 给双方各记一点动作：没有动作时复述里不会出现逐条统计行
    recorder._apply(DuelEvent("damage", player=1, value=2400))
    recorder.players[0].normal_summons = 1
    recorder.players[1].sp_summons = 1

    # 群友赢
    recorder.winner_seat = 0
    recorder.win_reason = 1
    data = recorder.to_dict()
    assert data["winner_is_self"] is False, "群友赢时必须判成「不是我赢」"
    assert data["self_seat"] == 1
    assert "（对方）" in str(data["winner_name"]), data["winner_name"]
    summary = "\n".join(recorder.summary_lines())
    assert "群友A（对方） 获胜" in summary, summary
    assert "憨憨（我方）" in summary, "我方那侧也要标出来，否则读的人分不清哪个是自己"

    # 麦麦赢
    recorder.winner_seat = 1
    data = recorder.to_dict()
    assert data["winner_is_self"] is True
    assert "（我方）" in str(data["winner_name"])

    # 编号未知时不硬猜（宁可给 None，也不给一个可能反的布尔值）
    recorder.duel_index = None
    recorder.room_seat = None
    assert recorder.to_dict()["winner_is_self"] is None


def test_taunt_picker_avoids_immediate_repeat() -> None:
    """台词要随机但不连着说同一句；单句池时也不该死循环。"""

    picker = TauntPicker(rng=random.Random(20260920))
    picks = [picker.pick() for _ in range(60)]
    assert all(line in TAUNT_LINES for line in picks)
    assert len(set(picks)) > 5, f"60 次只挑出 {len(set(picks))} 种，随机性太差"
    for before, after in zip(picks, picks[1:]):
        assert before != after, "连着重复同一句最出戏，必须避开"

    single = TauntPicker(["只有一句"], rng=random.Random(1))
    assert single.pick() == "只有一句"
    assert single.pick() == "只有一句"


def test_taunt_picker_uses_configured_lines() -> None:
    """配置里给了台词池就只用那几句（2026-10-07 用户要求"具体说什么"能自己改）。"""

    custom = ["这局的剧本我写好了", "轮到你了，别发呆"]
    picker = TauntPicker(custom, rng=random.Random(11))
    picks = [picker.pick() for _ in range(20)]
    assert set(picks) <= set(custom), picks
    assert set(picks) == set(custom), "两句池子里应当都用得上"

    # 空池子（配置成 []）不能挑出东西，也不能抛异常——关掉挑衅由 taunt_enabled 负责
    assert TauntPicker([]).pick() == ""


def test_taunt_chance_bounds() -> None:
    """概率为 0 时永不触发、为 1 时每 tick 都触发。"""

    picker = TauntPicker(rng=random.Random(7))
    assert not any(picker.should_taunt(0.0) for _ in range(50))
    assert all(picker.should_taunt(1.0) for _ in range(10))

    # 3% 的长期命中率应当接近 3%（这里只做宽松的区间检查）
    hits = sum(1 for _ in range(20000) if picker.should_taunt(0.03))
    assert 400 < hits < 800, hits


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
