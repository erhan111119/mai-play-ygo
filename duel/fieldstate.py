"""从报文流里维护一份轻量局面：双方生命值与场地占用。

`/查房` 要能报出「双方 LP、怪兽数量、魔陷数量」，而这三样都必须自己从报文里算——
内核没有查询接口，客户端也不会把场面告诉我们。做法是吃下 :class:`DuelEvent`：

* 生命值：**按 ``damage`` / ``pay_cost`` / ``recover`` 事件自己累计**，``lp_update`` 只当权威
  覆盖值。这一点是实测出来的：这套内核不为每次掉血发 ``MSG_LPUPDATE``（整局一条都没有），
  只看 ``lp_update`` 的话生命值会一直显示满血；
* 场地占用：``move`` 给出一张卡的原位置与新位置（控制者/区域/序号），据此维护场地格位占用。

格位用 ``(控制者, 区域, 序号)`` 当键，这样同一张卡移动、控制权转移、灵摆摆到魔陷区
都能自然对上。只关心怪兽区与魔陷区，别的区域（卡组、墓地、除外、额外）不进统计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .protocol import CardLocation, DuelEvent

# 统计里关心的区域：怪兽区、魔陷区（灵摆刻度也算在魔陷区里）
MONSTER_ZONE = int(CardLocation.MONSTER_ZONE)
SPELL_ZONES = (int(CardLocation.SPELL_ZONE), int(CardLocation.PENDULUM_ZONE))
# 堆叠计数关心的三个区域（查房出图上那些"牌堆"上的数字）
GRAVE_ZONE = int(CardLocation.GRAVE)
REMOVED_ZONE = int(CardLocation.REMOVED)
EXTRA_ZONE = int(CardLocation.EXTRA)

# 场地格位键：控制者、区域、序号
ZoneKey = Tuple[int, int, int]

# 表示形式位（ygopro 约定，与 WindBot 的 CardPosition 一致）：
# 0x1 表侧攻击、0x2 里侧攻击、0x4 表侧守备、0x8 里侧守备；派生位 0x5=表侧、0xA=里侧、0x3=攻击、0xC=守备
FACEDOWN = 0xA


@dataclass
class ZoneCard:
    """场上一格里的卡：卡号 + 表示形式。

    为什么要连表示形式一起存：查房出图要画"表侧/里侧、攻击/守备"——**里侧的卡只能画卡背**，
    少了这个位就会把对手的盖牌当表侧画出来（等于替对手公开手牌）。
    """

    card_id: int
    position: int = 0

    @property
    def face_up(self) -> bool:
        """是不是表侧表示（0x1 表侧攻击 / 0x4 表侧守备）。"""

        return (self.position & 0x5) != 0

    @property
    def attack_position(self) -> bool:
        """是不是攻击表示（含里侧攻击）。"""

        return (self.position & 0x3) != 0


@dataclass
class PlayerField:
    """单个座位的局面。"""

    seat: int
    lp: int = 0
    monsters: int = 0
    spells: int = 0
    #: 堆叠计数（查房出图上的"牌堆"数字）：**当前**在这一区域里的卡数。
    #: 由 `FieldState._move` 逐次移动维护（进、出都算），所以是"现在几张"而不是"进过几张"；
    #: ⚠ 额外卡组的**初始**张数不在里面（那是构筑信息，报文里没有），只统计"这一局里被送去额外/从额外出来"的卡。
    grave: int = 0
    banished: int = 0
    extra: int = 0

    def label_lp(self) -> str:
        """生命值的显示文本。"""

        return f"LP {self.lp}"


@dataclass
class FieldState:
    """一局中的局面快照。

    Args:
        start_lp: 初始生命值，用于在第一次 ``lp_update`` 到达之前也能报出正确的数值。
    """

    start_lp: int = 8000
    turn_count: int = 0
    players: Dict[int, PlayerField] = field(
        default_factory=lambda: {0: PlayerField(0, 8000), 1: PlayerField(1, 8000)}
    )
    # (控制者, 区域, 序号) -> 卡 ID；只放怪兽区与魔陷区
    zones: Dict[ZoneKey, int] = field(default_factory=dict)
    #: (座位, 区域) -> 现在有几张卡；只有墓地/除外/额外三个区域（`PlayerField` 的三个计数字段读它）
    _stacks: Dict[Tuple[int, int], int] = field(default_factory=dict, repr=False)
    #: 每个座位**见过的卡**（卡号 → 出现次数）：查房图靠它在没拿到对面卡组名时"看牌猜卡组"
    _seen: Dict[int, Dict[int, int]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """把初始生命值填进两个座位。"""

        for seat, player in self.players.items():
            player.lp = self.start_lp

    def apply(self, event: DuelEvent) -> None:
        """吃下一条事件，更新局面。"""

        # 见过的卡（每个座位一份）：查房图靠它"看牌猜卡组"（对面卡组名内核不给）
        if event.card_id and event.player in (0, 1):
            seat_seen = self._seen.setdefault(event.player, {})
            seat_seen[event.card_id] = seat_seen.get(event.card_id, 0) + 1

        if event.kind == "lp_update":
            # 内核直接下发的权威数值。注意它不是每次掉血都会发（实测很多对局一条都没有），
            # 所以下面还得按伤害/回复事件自己累计，这里只当覆盖值用。
            player = self.players.get(event.player)
            if player is not None:
                player.lp = event.value
            return

        if event.kind in ("damage", "pay_cost", "recover"):
            # 受伤与支付生命值都是扣，回复是加；生命值不会低于 0（内核也是这么截的）
            player = self.players.get(event.player)
            if player is not None:
                delta = event.value if event.kind == "recover" else -event.value
                player.lp = max(0, player.lp + delta)
            return

        if event.kind == "new_turn":
            self.turn_count += 1
            return

        if event.kind == "pos_change":
            self._pos_change(event)
            return

        if event.kind == "move":
            self._move(event)

    def _move(self, event: DuelEvent) -> None:
        """处理一次移动：清掉原格位，占住新格位。"""

        if len(event.data) < 6:
            # 老报文没带完整坐标（理论上不会发生）：退化成按区域计数不可靠，
            # 这里就不动格位表，只把目标区域记在新位置上
            return
        old_controller, old_location, old_sequence, new_controller, new_location, new_sequence = (
            event.data[:6]
        )
        new_position = int(event.data[6]) if len(event.data) > 6 else 0
        self.zones.pop((old_controller, old_location, old_sequence), None)
        if new_controller in self.players and (
            new_location == MONSTER_ZONE or new_location in SPELL_ZONES
        ):
            self.zones[(new_controller, new_location, new_sequence)] = ZoneCard(
                event.card_id, new_position
            )
        # 堆叠计数：从旧区域扣、往新区域加——进出都算，所以"墓地现在几张"始终是当前值
        # （只记墓地/除外/额外三个区域，见 PlayerField.grave/banished/extra）
        self._stack_delta(int(old_controller), int(old_location), -1)
        self._stack_delta(int(new_controller), int(new_location), +1)
        self._recount()

    def _stack_delta(self, seat: int, location: int, delta: int) -> None:
        """堆叠计数的一次增减；数量归零就把这个键删掉（不会留下 0 或负数的残留）。"""

        if location not in (GRAVE_ZONE, REMOVED_ZONE, EXTRA_ZONE):
            return
        key = (seat, location)
        current = self._stacks.get(key, 0) + delta
        if current <= 0:
            self._stacks.pop(key, None)
        else:
            self._stacks[key] = current

    def _pos_change(self, event: DuelEvent) -> None:
        """翻开盖牌 / 改成守备：把那一格的表示形式换成新值（区域与序号以报文为准）。"""

        if len(event.data) < 3:
            return
        location, sequence, new_position = int(event.data[0]), int(event.data[1]), int(event.data[2])
        card = self.zones.get((event.player, location, sequence))
        if card is not None:
            card.position = new_position

    def _recount(self) -> None:
        """按格位表重算双方的怪兽与魔陷数量，并把堆叠计数抄进各自的面板。"""

        for player in self.players.values():
            player.monsters = 0
            player.spells = 0
            player.grave = self._stacks.get((player.seat, GRAVE_ZONE), 0)
            player.banished = self._stacks.get((player.seat, REMOVED_ZONE), 0)
            player.extra = self._stacks.get((player.seat, EXTRA_ZONE), 0)
        for (controller, location, _sequence) in self.zones:
            player = self.players.get(controller)
            if player is None:
                continue
            if location == MONSTER_ZONE:
                player.monsters += 1
            elif location in SPELL_ZONES:
                player.spells += 1

    def seen_ids(self, seat: int, *, limit: int = 60) -> List[int]:
        """这个座位**见过的卡号**（按出现次数从多到少，取前 `limit` 个）。

        用处：查房图上"对面用的是哪副牌"内核不给，只能按双方见过的卡名猜（见
        `duel/field_image.py` 的 `_archetype_guess`）。
        """

        seen = self._seen.get(seat, {})
        return [card_id for card_id, _count in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]

    def slot_of(self, card_id: int) -> Optional[Tuple[int, int]]:
        """这张卡在哪个格位（``(区域号, 序号)``）；不在场上就返回 ``None``。"""
        for (controller, location, sequence), value in self.zones.items():
            del controller
            if value.card_id == card_id:
                return location, sequence
        return None

    def zones_of(self, seat: int) -> Dict[Tuple[int, int], ZoneCard]:
        """这个座位场上每格的卡（键＝``(区域号, 序号)``）——查房出图按它排版。"""

        return {
            (location, sequence): card
            for (controller, location, sequence), card in self.zones.items()
            if controller == seat
        }

    def describe_slots(self, seat: int) -> List[str]:
        """这个座位**每只怪在哪一格**（额外怪兽区会点名）。

        为什么要报到格号：额外卡组出来的怪本该进额外怪兽区（额外怪兽区只有一个、
        主怪兽区有五个），而**放置不是出牌脚本与 AI 决定的**——是 WindBot 的
        ``GameAI.OnSelectPlace`` 按写死的优先级挑的（主怪兽区优先、额外怪兽区最后），
        我们的问题文件里也不含格号。所以"大姐到底放哪了"只有这里看得出来。
        """

        slots: List[str] = []
        for sequence in range(7):
            card = self.zones.get((seat, MONSTER_ZONE, sequence))
            if card is None:
                continue
            slots.append(f"{zone_label(sequence)} #{card.card_id}")
        return slots


def zone_label(sequence: int) -> str:
    """格位序号 → 人话（0~4 主怪兽区、5~6 额外怪兽区）。"""

    if sequence in (5, 6):
        return "额外怪兽区"
    if 0 <= sequence <= 4:
        return f"主怪兽区{sequence + 1}"
    return f"格{sequence}"


def spell_zone_label(location: int, sequence: int) -> str:
    """魔陷区格位 → 人话（``SPELL_ZONE``：0~4 魔法陷阱区、5 场地区；``PENDULUM_ZONE``：0~1 灵摆区）。

    加它是因为「落位」记录原来只写怪兽（见 ``recorder._note_placement``）：验收①里
    「未眠之城」这种**场地魔法**有没有第 1 回合就摆上去，没有别的可靠来源
    （客户端流里我们的卡有时是 ``UnKnowCard``，按卡名数会漏）。
    """

    if location == int(CardLocation.PENDULUM_ZONE):
        return f"灵摆区{sequence + 1}"
    if sequence == 5:
        return "场地区"
    if 0 <= sequence <= 4:
        return f"魔法陷阱区{sequence + 1}"
    return f"格{sequence}"
