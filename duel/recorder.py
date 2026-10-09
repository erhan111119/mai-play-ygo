"""对局记录器：把报文流变成「谁赢了、怎么赢的」。

记录器消费闸门旁路出来的报文，维护一份对局统计，最后产出一段人类可读的复述。
它不依赖 LLM——胜负与关键统计都是确定性的，LLM 只适合在这之后把复述润色成更自然的说法。

设计上的两个取舍：

* **只统计，不重放**。客户端侧的报文流缺少随机种子与对手的选择，无法在本地复刻一局决斗；
  所以这里只累积计数与少数极值（最大单次伤害、最活跃的卡），而不是维护完整场面。
* **胜负来源单一但可靠**。``MSG_WIN`` 是内核在对局结束时发出的终结消息，字段是
  ``[获胜方, 原因]``。若整局都没观测到它（例如客户端中途断线），结果就如实标记为未知，
  不会猜一个出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import logging
import os
import time

from .card_images import DEFAULT_CACHE_DIR, request_async as request_card_pic
from .cards import CardDatabase
from .fieldstate import FieldState, spell_zone_label, zone_label
from .protocol import (
    CardLocation,
    DuelEvent,
    Frame,
    PHASE_NAMES,
    SPECIAL_WIN_REASON_MIN,
    Stoc,
    WIN_REASONS,
    parse_frame_payload,
    parse_player_enter,
    parse_type_change,
)


# 记入「最活跃的卡」榜单的卡数量上限
_TOP_CARD_LIMIT = 3

#: 抓帧调试开关：设了环境变量 `YGO_DUMP_FRAMES=<文件路径>` 就把每条对局报文原样落盘
#: （每行 `消息号 十六进制payload`）。面板上某个数字不对时拿真报文逐字节核对——`MSG_UPDATE_DATA`
#: 那套字段（攻守的当前值）就是这么查出来的，别当成"以后可能用不上"的东西删掉。
_FRAME_DUMP_ENV = "YGO_DUMP_FRAMES"

# 亮点事件保留条数上限，避免长局把内存吃满
_HIGHLIGHT_LIMIT = 24
_PLACEMENT_LIMIT = 40

#: 魔陷区（含灵摆）——落位记录要认这些区域（见 ``_note_placement``）
_SPELL_ZONES = (int(CardLocation.SPELL_ZONE), int(CardLocation.PENDULUM_ZONE))
"""落位记录最多留多少条（一局几十次召唤很正常，只留最近的、够回溯即可）。"""

# 单次伤害达到这个值才值得单独记一笔
_BIG_HIT_THRESHOLD = 2000

#: 调试用：``MAIBOT_DEBUG_DRAW=1`` 时把每条抽牌报文（座位/张数/卡号）打到 stdout。
#: 用来确认"进手台账"到底收到了什么——对手那一侧的卡号是 0（手牌在客户端侧隐藏），
#: 实测过：观测方自己的起手 5 张也是走这条报文，而 MSG_START 里**没有**手牌信息。
_DEBUG_DRAW = os.environ.get("MAIBOT_DEBUG_DRAW") == "1"


@dataclass
class PlayerStats:
    """单个座位的统计数据。"""

    seat: int
    name: str = ""
    normal_summons: int = 0
    sp_summons: int = 0
    effects: int = 0
    # 盖放（怪兽里侧 + 魔陷）是「出牌」但**不是**召唤：内核为它单发 MSG_SET，
    # 与 MSG_SUMMONING/SPSUMMONING 互斥。原来整条报文没人接——于是"只盖牌的局"
    # 在台账（动作数 / card_usage / 播报）里看起来像"整局没出牌"（2026-10-06 修）
    sets: int = 0
    draws: int = 0
    attacks: int = 0
    direct_attacks: int = 0
    damage_taken: int = 0
    damage_dealt: int = 0
    biggest_hit_taken: int = 0
    lp_recovered: int = 0
    lp_final: Optional[int] = None
    sent_to_grave: int = 0
    banished: int = 0
    card_usage: Dict[int, int] = field(default_factory=dict)
    """这个座位用到的卡与次数（召唤/特殊召唤/反转召唤/发动效果/盖放）。

    按座位分开记（2026-10-07 修）：以前是**双方混记**的一张表，于是
    `/优化卡组` 的"死牌"判定与 `first_turn_accept.py` 的"这副牌用上过哪些卡"会把
    "对手打过的同一张卡"算成我们也用过（实测 r24~r27 里 15 局是"只有对手带这张卡却进了台账"）。
    """

    cards_seen: Dict[int, int] = field(default_factory=dict)
    """这个座位**进过手牌**的卡与次数（抽到 + 被卡组/墓地/除外区检索或回收进手）。

    它是"该不该加第几张"的分母：只有知道"这张卡这局到底有没有到手"，才能把
    使用率拆成"到手就能用"与"到手了也用不上"。⚠ 只有**观测方自己**这一份是完整的——
    对手的手牌在客户端侧是隐藏的（那些报文的卡号是 0，被 `_mark_seen` 丢弃）。
    """

    def label(self) -> str:
        """播报里使用的称呼。"""

        return self.name or f"座位{self.seat}"


@dataclass
class DuelResult:
    """一局决斗的结论。"""

    winner_seat: Optional[int]
    winner_name: str
    reason: str
    is_draw: bool = False
    resolved: bool = True


@dataclass
class DuelRecorder:
    """累积一局对局的过程数据并生成复述。

    Args:
        card_db: 卡名数据库，用于把卡 ID 翻成卡名；为 None 时只能显示编号。
        group_id: 关联的群号，仅用于落库与定位。
        human_name_hint: 已知的群友昵称，作为座位名字缺失时的兜底提示。
        start_lp: 初始生命值，用于「查房」在第一次掉血之前也能报出正确数值。
        logger: 日志器（可选）。用来记**落位**——放置由 WindBot 的 ``GameAI.OnSelectPlace``
            按写死的优先级挑（主怪兽区优先、额外怪兽区最后），AI 层看不到格号，
            所以"额外卡组出来的怪放哪了"只能靠这一行日志回溯（见 :meth:`_note_placement`）。
    """

    card_db: Optional[CardDatabase] = None
    group_id: str = ""
    human_name_hint: str = ""
    start_lp: int = 8000
    logger: Optional[logging.Logger] = None
    players: Dict[int, PlayerStats] = field(
        default_factory=lambda: {0: PlayerStats(seat=0), 1: PlayerStats(seat=1)}
    )
    room_seat: Optional[int] = None
    """入房座位号（``STOC_TYPE_CHANGE`` 的低 4 位）——**只用来把昵称对到人**。

    它和对局里的玩家号是两套编号：服务器为了先后手会重排后者（见 :attr:`duel_index`），
    实测同一局里可以一个是 0、另一个是 1。拿它当对局报文的参照系会把"谁赢"说反。
    """
    duel_index: Optional[int] = None
    """我在**对局里**的玩家号（``MSG_START`` 本体第一个字节的低 4 位）。

    **这是对局报文的参照系**：damage / recover / pay_cost / lp_update / win / new_turn /
    move 的控制者这些字段装的都是这个号码（0/1），不是"相对于收报人"的 0/1。

    依据是 WindBot 自己的 ``GetLocalPlayer(p) = IsFirst ? p : 1 - p``：它拿到这些字段一律
    先换算成自己那份 ``Fields`` 的下标（``Fields[0]`` 放的是 bot 自己的卡组与 LP，见
    ``GameBehavior.OnStart``）。如果字段本来就是"0＝我"，这个换算在 ``IsFirst`` 为假时会把
    双方数据对调，WindBot 就不可能打得对。

    而 ``OnStart`` 里还有一句关键注释：**服务器会为先后手重排玩家号，房间昵称仍留在入房槽位**
    （``_chatPlayerOrderSwapped = roomTeam != (_duel.IsFirst ? 0 : 1)``）。实测（本机内核，
    bot 先入房）：``TYPE_CHANGE`` 报房间座位 0，``MSG_START`` 报对局玩家号 1——两套编号整体差
    一次交换。所以"我方是哪一格"必须问 MSG_START，问 TYPE_CHANGE 会说反。
    """
    room_names: Dict[int, str] = field(default_factory=dict)
    """按**入房槽位**记的昵称（来自 ``STOC_HS_PLAYER_ENTER``）。"""
    started: bool = False
    finished: bool = False
    turn_count: int = 0
    phase: str = ""
    """当前阶段的中文名（`new_phase` 报文；查房出图要报"主要阶段 2"这类）。"""

    first_player_seat: Optional[int] = None
    winner_seat: Optional[int] = None
    win_reason: int = -1
    started_at: float = 0.0
    ended_at: float = 0.0
    highlights: List[str] = field(default_factory=list)
    placements: List[str] = field(default_factory=list)
    """怪兽落位记录（"第 3 回合 我方 额外怪兽区 ← 一擲乾坤"）。

    只记**怪兽落进怪兽区**这一种移动（召唤/特召/控制权转移都算）。
    用途：放置由 WindBot 的 ``OnSelectPlace`` 决定、AI 层看不到格号，
    所以"额外卡组出来的怪有没有进额外怪兽区"只能从这份记录里回溯。
    """
    unparsed_frames: int = 0
    # 实时局面（双方 LP 与场地占用），「查房」读它
    field_state: FieldState = field(default_factory=FieldState)

    def __post_init__(self) -> None:
        """把房间的初始生命值透给局面快照。

        内核是按 ``RoomSettings.start_lp`` 开局的，快照不跟上就会在第一次掉血之前
        报出内核的默认值（8000）——训练时把 LP 调低到 4000，两边数字对不上就是这么来的。
        """

        self.field_state = FieldState(start_lp=self.start_lp)

    # ------------------------------------------------------------------ 喂数据

    def feed(self, direction: str, frame: Frame) -> None:
        """喂入一条报文。方向为 ``"to_client"`` 时表示服务器发给客户端。"""

        if direction != "to_client":
            return
        if frame.message_id == Stoc.GAME_MSG:
            self._on_game_message(frame)
            return
        try:
            if frame.message_id == Stoc.TYPE_CHANGE:
                # 这里拿到的是**入房座位**，只用来对昵称；对局参照系是 MSG_START（见 duel_index）
                seat, _is_host = parse_type_change(frame.payload)
                self.room_seat = seat
                self._refresh_names()
            elif frame.message_id == Stoc.HS_PLAYER_ENTER:
                name, seat = parse_player_enter(frame.payload)
                self._set_player_name(seat, name)
            elif frame.message_id == Stoc.DUEL_START:
                self.started = True
                self.started_at = time.time()
            elif frame.message_id == Stoc.DUEL_END:
                self.finished = True
                self.ended_at = time.time()
        except Exception:  # noqa: BLE001  单条报文异常不应中断整局记录
            self.unparsed_frames += 1

    @property
    def self_seat(self) -> Optional[int]:
        """统计与局面快照里"我方"那一格的键。

        对局开始后就是 :attr:`duel_index`（对局玩家号）；对局还没开始时退化成入房座位，
        好让「查房」在开局前也能把双方的名字摆在正确的位置上。
        """

        return self.duel_index if self.duel_index in (0, 1) else self.room_seat

    def _room_slot_of(self, duel_index: int) -> Optional[int]:
        """对局玩家号 → 入房座位号（昵称是按入房座位记的）。

        两套编号只可能差一次交换，而且我们知道自己的对应关系（自己的那格照抄，另一格取反），
        所以这个映射是确定的。任一边还不知道时返回 ``None``——宁可显示"座位N"，
        也不能把对手的名字贴到自己头上。
        """

        if self.room_seat not in (0, 1) or self.duel_index not in (0, 1):
            return None
        return self.room_seat if duel_index == self.duel_index else 1 - self.room_seat

    def _refresh_names(self) -> None:
        """把入房座位上的昵称贴到对局玩家号上（自己的名字照抄，对手那个走映射）。"""

        for index in (0, 1):
            slot = self._room_slot_of(index)
            name = self.room_names.get(slot, "") if slot is not None else ""
            if name:
                self.players[index].name = name
            elif index != self.self_seat and self.human_name_hint:
                # 群友的昵称在游戏里可能没送到（进房的昵称可以是空的），用调用方给的提示兜底
                self.players[index].name = self.human_name_hint

    def _set_player_name(self, slot: int, name: str) -> None:
        """登记**入房座位**上的昵称，并重算"哪个名字属于对局里的哪一方"。"""

        if not name:
            return
        self.room_names[slot] = name
        self._refresh_names()

    def _on_game_message(self, frame: Frame) -> None:
        """处理一条游戏消息。"""

        self._dump_frame(frame)
        try:
            event = parse_frame_payload(frame.payload)
        except Exception:  # noqa: BLE001  同上，坏包只计数
            self.unparsed_frames += 1
            return
        if event is None:
            return
        self._apply(event)

    def _dump_frame(self, frame: Frame) -> None:
        """把对局报文原样落盘（**只在设了环境变量 `YGO_DUMP_FRAMES=<文件路径>` 时**）。

        为什么留着这个开关：面板上某个数字不对时（例如"攻守显示的不是实际值"），
        有真报文的十六进制就能对着协议逐字节核对，而不是靠猜——`MSG_UPDATE_CARD` /
        `MSG_UPDATE_DATA` 那两套字段（攻守的当前值）就是这么查出来的。

        格式：每行 `游戏消息号 十六进制payload`。注意游戏消息号取自 **payload 的第一个字节**
        （外层 `STOC_GAME_MSG` 的 message_id 恒为 1，把它写进去等于没有信息）。
        """

        path = os.environ.get(_FRAME_DUMP_ENV, "").strip()
        if not path or not frame.payload:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{frame.payload[0]} {frame.payload[1:].hex()}\n")
        except OSError:
            # 抓帧是排查用的旁路：写不进去（目录没了/盘满了）不该影响对局记录
            return

    def _apply(self, event: DuelEvent) -> None:
        """把事件累加进统计。"""

        # 对局报文里的玩家号**已经是 0/1 的对局编号**，不需要（也不能）再按房间座位换算一次：
        # 两套编号可能整体交换，换算反而会把双方对调（见 duel_index 的说明）。
        if event.kind == "duel_start":
            if event.player in (0, 1):
                self.duel_index = event.player
            self._refresh_names()
            return

        # 实时局面（LP / 场地占用）与统计并行维护，供「查房」随时读取
        self.field_state.apply(event)
        self._note_placement(event)
        # 卡图**自动预热**（用户口径 2026-10-09："预热也做了吧，自动预热"）：卡一有动静就把它的
        # 整卡图排进后台单线程池（`duel/card_images.py`：缓存里有就跳过、没有才联网，重复卡号只排一次），
        # 等群里问 /查房 时缓存里已经在了——出图不用等下载，也不会出现"这格没卡图"。
        # 这里只排"可能出现在场上的卡"（召唤/放置/移动/改表示形式）；查房图也只画这些
        #（里侧画卡背、墓地与除外不画卡），多下几张的代价远小于漏图。
        if event.kind in ("summon", "sp_summon", "flip_summon", "set", "move", "pos_change"):
            request_card_pic(event.card_id, DEFAULT_CACHE_DIR)

        if event.kind == "win":
            self.winner_seat = event.player if event.player in (0, 1) else None
            self.win_reason = event.value
            return

        if event.kind == "new_turn":
            self.turn_count += 1
            if self.first_player_seat is None:
                self.first_player_seat = event.player
            return

        if event.kind == "new_phase":
            # 当前阶段（查房出图要报"主要阶段 2"这类）：原来这条报文没人接，图里就少一栏
            self.phase = PHASE_NAMES.get(event.value, "")
            return

        if event.kind in ("summon", "sp_summon", "flip_summon"):
            stats = self._stats(event.player)
            if stats is not None:
                if event.kind == "sp_summon":
                    stats.sp_summons += 1
                else:
                    stats.normal_summons += 1
            self._count_card(event.player, event.card_id)
            if event.kind == "sp_summon":
                self._add_highlight(f"{self._seat_label(event.player)} 特殊召唤{self._card(event.card_id)}")
            return

        if event.kind == "set":
            # 盖放：怪兽里侧出场与魔陷盖放都走这条（内核不为它们发 summon 报文）。
            # 记进统计与用卡台账——否则"只盖牌"的局会被当成"整局没出牌"
            stats = self._stats(event.player)
            if stats is not None:
                stats.sets += 1
            self._count_card(event.player, event.card_id)
            return

        if event.kind == "chain":
            stats = self._stats(event.player)
            if stats is not None:
                stats.effects += 1
            self._count_card(event.player, event.card_id)
            return

        if event.kind == "draw":
            stats = self._stats(event.player)
            if stats is not None:
                stats.draws += event.value
            # 抽到的卡也记进"到手"台账（起手 5 张走的也是这条报文）
            if _DEBUG_DRAW:
                print(f"[DRAW] 座位{event.player} 张数{event.value} 卡号{list(event.data)}", flush=True)
            for card_id in event.data:
                self._mark_seen(event.player, card_id)
            return

        if event.kind == "attack":
            stats = self._stats(event.player)
            if stats is not None:
                stats.attacks += 1
                if event.value == 0:
                    # 被攻击方所在区域为 0 说明没有目标，即直接攻击
                    stats.direct_attacks += 1
                    self._add_highlight(f"{self._seat_label(event.player)} 直接攻击")
            return

        if event.kind == "damage":
            self._on_damage(event)
            return

        if event.kind == "pay_cost":
            # 支付生命值是自己掏的，不算对手的输出
            stats = self._stats(event.player)
            if stats is not None:
                stats.damage_taken += event.value
            return

        if event.kind == "recover":
            stats = self._stats(event.player)
            if stats is not None:
                stats.lp_recovered += event.value
            return

        if event.kind == "lp_update":
            stats = self._stats(event.player)
            if stats is not None:
                stats.lp_final = event.value
            return

        if event.kind == "move":
            stats = self._stats(event.player)
            if stats is None:
                return
            # 检索/回收进手也算"到手"：能被用上的前提是先到手，不管它是抽来的还是找来的。
            # 只有同一控制者内部的移动才算（`event.player`＝原控制者，`event.data[3]`＝新控制者）——
            # 被对手抢过去进到对方手里不是我们的手牌资源。
            if (
                event.data
                and event.data[3] == event.player
                and event.data[4] == CardLocation.HAND
            ):
                self._mark_seen(event.player, event.card_id)
            if event.extra == CardLocation.GRAVE:
                stats.sent_to_grave += 1
            elif event.extra == CardLocation.REMOVED:
                stats.banished += 1
                self._add_highlight(f"{self._card(event.card_id)}被除外")
            return

    def _on_damage(self, event: DuelEvent) -> None:
        """处理受伤：记在承受方，同时算作对手的输出。

        生命值只在客户端侧可观测，所以「输出伤害」是由对手受到的伤害反推出来的，
        而不是从攻击宣言累加——后者会把被无效化的攻击也算进去。
        """

        stats = self._stats(event.player)
        if stats is None:
            return
        stats.damage_taken += event.value
        if event.value > stats.biggest_hit_taken:
            stats.biggest_hit_taken = event.value
        opponent = self._stats(1 - event.player)
        if opponent is not None:
            opponent.damage_dealt += event.value
        if event.value >= _BIG_HIT_THRESHOLD:
            self._add_highlight(f"{self._seat_label(event.player)} 受到 {event.value} 点伤害")

    def _stats(self, seat: int) -> Optional[PlayerStats]:
        """按座位取统计对象，座位号非法时返回 None。"""

        return self.players.get(seat)

    def _count_card(self, seat: int, card_id: int) -> None:
        """按座位累计单卡出现次数（见 :attr:`PlayerStats.card_usage`）。"""

        stats = self._stats(seat)
        if stats is None or card_id <= 0:
            return
        stats.card_usage[card_id] = stats.card_usage.get(card_id, 0) + 1

    def _mark_seen(self, seat: int, card_id: int) -> None:
        """按座位累计"这张卡进过手牌"（见 :attr:`PlayerStats.cards_seen`）。

        卡号 0 直接丢掉：对手的手牌在客户端侧是隐藏的（那些报文只给 0 占位），
        记进去会把"看不见"当成"没到手"。
        """

        stats = self._stats(seat)
        if stats is None or card_id <= 0:
            return
        stats.cards_seen[card_id] = stats.cards_seen.get(card_id, 0) + 1

    @property
    def card_seen(self) -> Dict[int, int]:
        """**我方**这局进过手牌的卡与次数（对手那份见 :attr:`opponent_card_seen`）。"""

        if self.self_seat not in (0, 1):
            return {}
        return dict(self.players[self.self_seat].cards_seen)

    @property
    def opponent_card_seen(self) -> Dict[int, int]:
        """对手进过手牌的卡——**通常是空的**，对手手牌是隐藏信息（只有被揭示时才带卡号）。"""

        if self.self_seat not in (0, 1):
            return {}
        return dict(self.players[1 - self.self_seat].cards_seen)

    @property
    def card_usage(self) -> Dict[int, int]:
        """**我方**这局用到的卡（播报、落库、`/优化卡组` 的死牌判定都读它）。

        拆座位之前的语义是"双方混在一起"，所以这里保持"我方"才是各方预期的那一份；
        对手的那份见 :attr:`opponent_card_usage`。
        """

        if self.self_seat not in (0, 1):
            return {}
        return dict(self.players[self.self_seat].card_usage)

    @property
    def opponent_card_usage(self) -> Dict[int, int]:
        """**对手**这局用到的卡（复盘"对面到底交了什么"时用；落库到 ``card_usage_opponent``）。"""

        if self.self_seat not in (0, 1):
            return {}
        return dict(self.players[1 - self.self_seat].card_usage)

    def _add_highlight(self, text: str) -> None:
        """记一条亮点，超过上限后丢弃最旧的。"""

        self.highlights.append(text)
        if len(self.highlights) > _HIGHLIGHT_LIMIT:
            del self.highlights[0]

    def _note_placement(self, event: DuelEvent) -> None:
        """有卡落进怪兽区/魔陷区（含场地、灵摆）时记一行"谁在哪一格"（额外怪兽区会点名）。

        **为什么值得单独记**：放置不是出牌脚本也不是 AI 决定的——是 WindBot 的
        ``GameAI.OnSelectPlace`` 按写死的优先级挑的（``z2→z1→z3→z0→z4→z6→z5``，
        主怪兽区优先、额外怪兽区最后），而我们的问题文件里**不含格号**。
        所以"额外卡组出来的大怪有没有进额外怪兽区"这件事，事后只有这份记录说得清
        （用户实测问过"大姐为什么没放额外怪兽区"）。

        **2026-10-06 起也记魔陷区**：验收①的件里有一半是魔陷/场地（如升辉月的「未眠之城」），
        而客户端流里我们的卡有时是 ``UnKnowCard``、`我方` 那条流又是两个 bot 混在一起的闸门视角
        ——"这件第 1 回合摆没摆上去"只有这里可信（见 ``tools/first_turn_accept.py``）。
        """

        if event.kind != "move" or len(event.data) < 6:
            return
        new_location = event.data[4]
        if new_location != int(CardLocation.MONSTER_ZONE) and new_location not in _SPELL_ZONES:
            return
        slot = self.field_state.slot_of(event.card_id)
        if slot is None:
            return
        location, sequence = slot
        label = (zone_label(sequence) if new_location == int(CardLocation.MONSTER_ZONE)
                 else spell_zone_label(location, sequence))
        text = (
            f"第 {self.turn_count} 回合 {self._seat_label(event.player)} "
            f"{label} ← {self._card(event.card_id)}"
        )
        self.placements.append(text)
        if len(self.placements) > _PLACEMENT_LIMIT:
            del self.placements[0]
        if self.logger is not None:
            self.logger.info("落位：%s", text)

    def _card(self, card_id: int) -> str:
        """卡 ID 的可读形式。"""

        if self.card_db is None:
            return f"#{card_id}"
        return self.card_db.describe(card_id)

    def _seat_label(self, seat: int) -> str:
        """座位号的播报称呼，形如「憨憨（我方）」或「打憨憨（对方）」。

        **为什么用「我方/对方」而不是名字或"麦麦"**：双方显示的都是各自客户端里的名字
        （麦麦在游戏内叫"憨憨"，群友是他在 MDPro3 里的昵称），而播报是交给模型写成人话的，
        模型得先弄清"哪个是我"。以前那侧标成「（麦麦）」、另一侧标「（群友）」，
        跟游戏里的名字对不上，看起来就像把两边搞反了（实测被这么误解过）。
        方位是绝对的：带「我方」的那行就是麦麦自己。
        """

        stats = self.players.get(seat)
        base = stats.label() if stats else f"座位{seat}"
        if self.self_seat not in (0, 1):
            return base
        role = "我方" if seat == self.self_seat else "对方"
        return f"{base}（{role}）"

    # ------------------------------------------------------------------ 结果

    def field_snapshot(self) -> List[str]:
        """当前局面：双方的生命值、怪兽数、魔陷数，以及**每只怪在哪一格**。

        给 ``/查房`` 用。数据来自 :class:`~duel.fieldstate.FieldState`——它跟着报文流实时更新，
        所以随时（对局进行中、还没开始时都可以）都能读。返回的每一行是「一方」，
        顺序固定为 bot 自己在前、对手在后（座位号未知时按座位 0/1）。

        格号也报出来，是因为**放置不由 AI 与脚本决定**（WindBot 的 ``GameAI.OnSelectPlace``
        自己挑，优先级是主怪兽区优先、额外怪兽区最后），问题文件里也不含格号——
        "额外卡组的大怪进没进额外怪兽区"只有在能看到格号的地方才判断得了。
        """

        seat_order = [self.self_seat] if self.self_seat in (0, 1) else []
        seat_order += [seat for seat in (0, 1) if seat not in seat_order]
        lines: List[str] = []
        for seat in seat_order:
            field_player = self.field_state.players.get(seat)
            if field_player is None:
                continue
            lines.append(
                f"{self._seat_label(seat)}：{field_player.label_lp()}"
                f"｜怪兽 {field_player.monsters}｜魔陷 {field_player.spells}"
            )
            slots = self.field_state.describe_slots(seat)
            if slots:
                lines.append("　站位：" + "、".join(slots))
        return lines

    @property
    def winner_label(self) -> str:
        """获胜方的播报称呼。"""

        if self.winner_seat is None:
            return ""
        return self._seat_label(self.winner_seat)

    def build_result(self) -> DuelResult:
        """生成结论，包含未解析出胜负的情况。"""

        if self.winner_seat is None:
            return DuelResult(
                winner_seat=None,
                winner_name="",
                reason="",
                resolved=self.finished,
            )
        if self.win_reason < 0:
            reason_text = ""
        elif self.win_reason >= SPECIAL_WIN_REASON_MIN:
            # 0x10 起是「某张卡的效果达成的特殊胜利」，具体是哪张要看内核的 strings.conf；
            # 这里只标注类别，不硬编码卡名（换卡池就过时了）
            reason_text = "特殊胜利"
        else:
            reason_text = WIN_REASONS.get(self.win_reason, "")
        return DuelResult(
            winner_seat=self.winner_seat,
            winner_name=self._seat_label(self.winner_seat),
            reason=reason_text,
            is_draw=False,
        )

    def summary_lines(self) -> List[str]:
        """生成复述行，供群里播报与写入 planner 上下文。"""

        lines: List[str] = []
        result = self.build_result()

        if not self.finished:
            lines.append(f"对局尚未结束，已进行 {self.turn_count} 个回合。")
            self._append_incomplete_note(lines)
            return lines

        if result.winner_seat is None:
            lines.append("对局已结束，但没有观测到胜负报文，结果未知。")
        else:
            reason = f"（{result.reason}）" if result.reason else ""
            lines.append(f"{result.winner_name} 获胜{reason}，共 {self.turn_count} 个回合。")

        if self.first_player_seat is not None:
            lines.append(f"{self._seat_label(self.first_player_seat)} 先攻。")

        for seat in (0, 1):
            stats = self.players[seat]
            if not self._has_activity(stats):
                continue
            # 一律用带身份的标签：同一份播报里有的行带「（麦麦）」有的不带，
            # 读的人（尤其是写播报的模型）就会把两边看混
            lines.append(f"{self._seat_label(seat)}：{self._describe_actions(stats)}")

        top_cards = self.top_cards()
        if top_cards:
            lines.append("最活跃的卡：" + "、".join(top_cards))

        biggest = max(
            (stats.biggest_hit_taken for stats in self.players.values()),
            default=0,
        )
        if biggest > 0:
            lines.append(f"最大单次伤害 {biggest}。")

        if self.highlights:
            lines.append("关键时刻：" + "；".join(self.highlights[-6:]))

        self._append_incomplete_note(lines)
        return lines

    def _append_incomplete_note(self, lines: List[str]) -> None:
        """把「复述可能不完整」如实写在末尾，避免看起来像完整战报。"""

        if self.unparsed_frames:
            lines.append(f"（有 {self.unparsed_frames} 条报文未能解析，复述可能不完整）")

    def top_cards(self, limit: int = _TOP_CARD_LIMIT) -> List[str]:
        """按出现次数给出最活跃的卡。"""

        ranked = sorted(self.card_usage.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [f"{self._card(card_id)}x{count}" for card_id, count in ranked if count > 1]

    def _describe_actions(self, stats: PlayerStats) -> str:
        """把一张统计表压成一句人话，只列出真正发生过的动作。"""

        parts: List[str] = []
        if stats.normal_summons:
            parts.append(f"通常召唤 {stats.normal_summons} 次")
        if stats.sp_summons:
            parts.append(f"特殊召唤 {stats.sp_summons} 次")
        if stats.sets:
            parts.append(f"盖放 {stats.sets} 次")
        if stats.effects:
            parts.append(f"发动效果 {stats.effects} 次")
        if stats.attacks:
            text = f"攻击 {stats.attacks} 次"
            if stats.direct_attacks:
                text += f"（直接攻击 {stats.direct_attacks} 次）"
            parts.append(text)
        if stats.damage_taken:
            parts.append(f"承受伤害 {stats.damage_taken}")
        if stats.lp_recovered:
            parts.append(f"回复生命值 {stats.lp_recovered}")
        if stats.lp_final is not None:
            parts.append(f"终局生命值 {stats.lp_final}")
        return "、".join(parts) if parts else "没有可播报的动作"

    def _has_activity(self, stats: PlayerStats) -> bool:
        """该座位是否有值得播报的动作。"""

        return any(
            (
                stats.normal_summons,
                stats.sp_summons,
                stats.sets,
                stats.effects,
                stats.attacks,
                stats.damage_taken,
            )
        )

    def to_dict(self) -> Dict[str, object]:
        """结构化结果，用于落库与写入 planner 上下文。"""

        result = self.build_result()
        return {
            "group_id": self.group_id,
            "started": self.started,
            "finished": self.finished,
            "turns": self.turn_count,
            "duration_seconds": int(self.ended_at - self.started_at) if self.started_at else 0,
            "winner_seat": self.winner_seat,
            "winner_name": result.winner_name,
            # 谁赢了要给成"是不是自己"这种一眼可判的事实：播报是模型写的，
            # 只给它一个名字时它得自己推断"这个名字是不是我"，实测会推反
            "self_seat": self.self_seat,
            # 两套编号都留下来：出问题时能一眼看出"是不是又碰上服务器重排了"
            "room_seat": self.room_seat,
            "duel_index": self.duel_index,
            "winner_is_self": (
                None
                if self.winner_seat is None or self.self_seat not in (0, 1)
                else self.winner_seat == self.self_seat
            ),
            "reason": result.reason,
            "resolved": result.resolved,
            "players": {
                str(seat): {
                    "name": stats.label(),
                    "normal_summons": stats.normal_summons,
                    "sp_summons": stats.sp_summons,
                    "sets": stats.sets,
                    "effects": stats.effects,
                    "draws": stats.draws,
                    "attacks": stats.attacks,
                    "direct_attacks": stats.direct_attacks,
                    "damage_taken": stats.damage_taken,
                    "damage_dealt": stats.damage_dealt,
                    "biggest_hit_taken": stats.biggest_hit_taken,
                    "lp_recovered": stats.lp_recovered,
                    "lp_final": stats.lp_final,
                    "sent_to_grave": stats.sent_to_grave,
                    "banished": stats.banished,
                    # 进过手牌的卡（只有观测方那一份完整）——擂台按左右映射后落库到
                    # `card_seen` / `card_seen_opponent`，`tools/card_vitality.py` 用它算"到手率"
                    "cards_seen": dict(stats.cards_seen),
                }
                for seat, stats in self.players.items()
            },
            "top_cards": self.top_cards(),
            # 落位记录：额外卡组出来的怪有没有进额外怪兽区，只有这里说得清（放置不由 AI 决定）
            "placements": list(self.placements),
            "unparsed_frames": self.unparsed_frames,
        }
