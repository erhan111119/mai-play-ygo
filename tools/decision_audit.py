"""决策审计：把一份 verbose 对局日志扫成"一屏可疑决策"，不用再人工翻日志。

**为什么要有它**：前几轮里所有真问题（手坑空放、大怪干站、选项登记错、关键件被当费用）都是
人工逐局读日志找出来的——一份 30 局的日志一两万行，翻一次半小时，而且只能抽着看。
这份工具把"翻日志"变成"看一屏"：按局定位、给出日志行号、按规则分类。

判据（保守：只在**看得见证据**时才报，宁可少报也不要制造噪声）：

1. ``手坑空放``：反应型手坑（灰流丽/幽鬼兔/遮蒙者/屋敷童/锁鸟）在自己回合交、或在对手**还没动作**
   的回合里交（连锁不上任何东西）。⚠ 主动型手坑（增殖的G、欢聚友伴）不适用这一条——它们
   "发动后按对手之后的动作抽卡"，**对手回合里越早交越好**，只有在自己回合交才算浪费
   （`_RESPONSE_HAND_TRAPS` / `_PREEMPTIVE_HAND_TRAPS`）。
2. ``大怪没进战阶``：我方回合里有 ≥2000 打点的怪在场，而这一回合**没进战阶**。
3. ``关键件被弃``：本家的怪从**手牌直接进墓地**（被当费用丢掉）——逐类计数，看是不是有价值的动点。
4. ``额外卡组出场``：整个日志里每张额外卡组怪"上过场"的**局数**——0 的就是一次都没打出来
   （对空白/对打都适用，是"关键终端没出场"那类问题的量具）。
5. ``选项没命中``：``[探针]`` 行里"想要"的选项值**不在内核给的选项表里**（＝这条登记写错了时机，
   或者这次发动被无效/错过时点）——按卡统计，一眼看出哪条登记在空转。
6. ``整局 0 额外召唤``：整局一只额外卡组怪都没上过场（真人局"输了但看不出为什么没做出来"那条：
   先用它把"根本没展开"的局挑出来，再配合 ``Bot Hand`` 段判卡手还是脚本问题）。
7. ``我方回合 ≤1 个动作``：我方某个回合只做了 ≤1 个动作（那一回合基本没动），报回合号与动作数。
   动作口径＝召唤/盖放/效果发动/攻击宣言（与结果库的「动作数」同口径）。
8. ``欢聚友伴提前交``：那两张手坑在"对手这一回合还没召唤"时就交了。**按卡文这是合法的**
   （发动条件只有"自己场上没有卡"＋对手回合，抽卡是"发动后对手每次召唤抽 1"的整回合持续效果），
   但用户 P2 的口径是"等对手真的开始出怪再交"，所以单独报出来，
   并配第五节 ``对手回合里我方抽卡``（＝这两张的实际收益）做 A/B 对比——
   收紧闸门会少掉"先交先抽"的那一两张，值不值要看这两个读数。

用法::

    python tools/decision_audit.py --log temp/train/mb-after-88.log --deck-id 88
    python tools/decision_audit.py --log temp/rounds/r17/p88-94.log --deck-id 88 --limit 20
    python tools/decision_audit.py --log <日志> --ydk <卡表.ydk> --cards-cdb <cards.cdb>

⚠ **口径提醒（2026-10-06 实测）**：日志里有两类"一局一条流"——
* ``我方`` 前缀是**插件自己的筛选日志**（只打它认为重要的事件：落位/选项/抽卡…），**不是完整客户端日志**：
  实测有局里"大姐已经直击打了 1900"，但这条流里**没有它的入场行**，所以场面账目会漏（见下）；
* ``对手(X)`` 前缀是对面客户端的完整日志，但我们这边的卡在那台客户端上有时显示为 ``UnKnowCard``，
  按卡名过滤同样会漏。
所以本工具报出来的都是**下界**（少报不会多报）："没报"不等于"没问题"，但"报了"一定是日志里真有那件事。

⚠ **"（我方回合）"要人工核一遍再动手**（2026-10-07 实测）：回合归属是从 ``(N draw 1 card)`` 推的
（``plan_accept._turns``），**第 1 回合没有抽牌行、只能用第 2 回合反推**；串行日志里基本可靠，
但并发（``--parallel >1``）日志会把两局混成一条流，归属就可能错——先把那一行原文前后 20 行翻出来看
（`grep -n "卡名 activate"` 然后看上下文），确认"这真的是我方回合、对面真的什么都没做"再改脚本。
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# 默认路径按宿主布局推（插件在 <MaiBot>/plugins/<id>，数据在 <MaiBot>/data/plugins/<id>），
# 卡库用插件自带客户端里那份；老插件的历史数据用 --data-dir 指过去。
# 夹逼是为了"插件不在宿主里"（单独放在 D:\mai-play-ygo）时不炸，见 check_card_coverage.py 的说明。
_HOST_ROOT = _PLUGIN_ROOT.parents[1] if len(_PLUGIN_ROOT.parents) > 1 else _PLUGIN_ROOT.parent
_DATA_DIR = _HOST_ROOT / "data" / "plugins" / "mai-play-ygo"
_CARDS_CDB = _PLUGIN_ROOT / "clients" / "ygopro" / "cards.cdb"

#: 手坑（日志里是卡名，不用卡号）——与 ``RaiseMoonExecutor.cs`` 的 HandTraps 同源
_HAND_TRAPS: Tuple[str, ...] = (
    "灰流丽", "增殖的G", "朔夜时雨", "幽鬼兔", "效果遮蒙者", "小丑与锁鸟",
    "抖抖海月水母", "茸茸长尾山雀",
)

#: **反应型**手坑：对着"对手某一次动作"交（灰流丽挡检索/特召、幽鬼兔炸表侧、遮蒙者无效怪、
#: 屋敷童挡墓地、锁鸟挡检索）。在自己回合交、或对手这一回合还没动作就交＝空放。
_RESPONSE_HAND_TRAPS: Tuple[str, ...] = (
    "灰流丽", "幽鬼兔", "效果遮蒙者", "屋敷童", "小丑与锁鸟",
)

#: **主动型**手坑：发动后按**对手之后的动作**抽卡（增殖的G＝对方特召抽 1；欢聚友伴＝对方召唤抽 1），
#: 所以正确玩法是**在对手回合里尽早交**——"对手这一回合还没动作就先交"是它们的正常时机，不算空放。
#: 只有**在我们自己的回合**交，才基本是白扔（对面多半不会在我们回合特召）。
_PREEMPTIVE_HAND_TRAPS: Tuple[str, ...] = ("增殖的G", "欢聚友伴")

#: 「大怪」的门槛打点（低于它的怪站着不打不算问题：这副牌的下级本来就该守备）
_BIG_ATTACK = 2000

#: ``[探针]`` 行：``[探针] 选项：正在结算=卡名(卡号) options=[值,…] 想要=值 命中=下标（按值/基类随机）``
_PROBE_RE = re.compile(
    r"\[探针\] 选项：正在结算=(?P<card>[^ ]*?) options=\[(?P<options>[^\]]*)\]"
    r"\s+想要=(?P<wanted>\S+)\s+命中=(?P<hit>-?\d+)（(?P<how>[^）]*)）"
)

#: 另一种探针写法（刻魔/杀调那两份执行器用的）：
#: ``[探针] 选项：正在结算=卡名 options=[…] 登记值=[…] 命中=N/未命中``
#: ——"登记值"里可能同时列出优先值表的多个值，只要**有一个**在 options 里就算命中。
_PROBE_B_RE = re.compile(
    r"\[探针\] 选项：正在结算=(?P<card>.+?) options=\[(?P<options>[^\]]*)\]"
    r"\s+登记值=\[(?P<wanted>[^\]]*)\]\s+命中=(?P<hit>\S+)"
)

_HAND_TRAP_ACTIVATE_RE = re.compile(r"\(([01]) 's (.+?) activate effect from [A-Za-z]*\)")
_ENTER_BATTLE_RE = re.compile(r"^\((Go to BattleStart|Go to Battle)\)$")
#: 攻击宣言（内核只在 ``Debug=true`` 时打，见 `GameBehavior.OnAttack`）：
#: 直接攻击 ``(青眼白龙 direct attack!!)``（**没有玩家号**，只能按回合归属）、
#: 打怪 ``(0 's 青眼白龙 attack  1 's 打憨憨)``（注意中间是两个空格）。
_ATTACK_RE = re.compile(r"\(([01]) 's (.+?) attack  ([01]) 's (.+?)\)")
_DIRECT_ATTACK_RE = re.compile(r"^\((.+?) direct attack!!\)$")


def _load_accept():
    """按文件路径加载同目录的 ``plan_accept``（tools 下的脚本不是包的一部分）。

    复用它的日志切分（``_streams`` / ``_turns`` / ``_seat``）与三段正则，
    免得两个工具对"什么算一局、什么算一条移动"各有一套理解。
    """

    spec = importlib.util.spec_from_file_location(
        "tool_plan_accept", str(_PLUGIN_ROOT / "tools" / "plan_accept.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先进 sys.modules 再 exec：plan_accept 里的 @dataclass 会去 sys.modules 找自己的模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def deck_of(deck_id: int, data_dir: Path) -> Tuple[str, Path]:
    """从卡组库取（显示名, ydk 路径）。"""

    connection = sqlite3.connect(f"file:{data_dir / 'deck_pool.db'}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT display_name, ydk_path FROM decks WHERE deck_id = ?", (deck_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"卡组库里没有 #{deck_id}")
    return str(row[0]), Path(str(row[1]))


def attack_of(cards_cdb: Path, names: Sequence[str]) -> Dict[str, int]:
    """卡名 → 攻击力（只查我们这副牌的卡；查不到的按 0 处理）。"""

    if not names:
        return {}
    connection = sqlite3.connect(f"file:{cards_cdb}?mode=ro", uri=True)
    try:
        result: Dict[str, int] = {}
        for name in names:
            row = connection.execute(
                "SELECT atk FROM datas WHERE id IN (SELECT id FROM texts WHERE name = ?)",
                (name,),
            ).fetchone()
            result[name] = int(row[0] or 0) if row else 0
        return result
    finally:
        connection.close()


def is_ours(name: str, our_names: Sequence[str]) -> bool:
    """日志里的卡名是不是我们牌组里的卡（日志用卡名，不用卡号）。"""

    return any(card in name for card in our_names)


def is_hand_trap(name: str) -> bool:
    return any(trap in name for trap in _HAND_TRAPS)


def is_response_hand_trap(name: str) -> bool:
    """反应型手坑（对着对手某一次动作交）。"""

    return any(trap in name for trap in _RESPONSE_HAND_TRAPS)


def is_preemptive_hand_trap(name: str) -> bool:
    """主动型手坑（发动后按对手之后的动作抽卡，对手回合里越早交越好）。"""

    return any(trap in name for trap in _PREEMPTIVE_HAND_TRAPS)


def audit_stream(stream, seat: int, our_names: Sequence[str], attacks: Dict[str, int],
                 limit: int, game_no: int) -> Tuple[List[str], Dict[str, int], Dict[str, int]]:
    """扫一条流（一局），返回（异常清单, 额外卡组出场, 本家资源去向计数）。

    ``seat``：我方在这条流里的座位（0＝这条流那只 bot 就是我方）。

    报出来的都是**下界**（见模块 docstring）：流里看不见的行不会报，所以一条异常＝日志里确实有这件事。
    两个规则都按**局**汇总，不按回合刷屏：``大怪没进战阶``要攒够 2 个回合才报，
    ``手坑空放``一局只报一行（列出每一次）。
    """

    accept = _load_accept()
    turns = accept._turns(stream.lines)
    findings: List[str] = []
    extra_plays: Dict[str, int] = {}
    resource: Dict[str, int] = {}

    field: Dict[str, int] = {}       # 我方此刻在场的怪（卡名 → 张数）
    idle_turns = 0                   # 有大怪、却没进战阶的**我方回合**数
    idle_monsters: List[str] = []
    wasted_traps: List[str] = []
    thin_turns: List[Tuple[int, int]] = []   # （回合号, 该回合我方的动作数）——动作 ≤1 的
    our_total_actions = 0            # 我方整局动作数（"整局 0 额外召唤"那条用它说明"动了几次"）
    # 「欢聚友伴」的**兑现**（P2 的 A/B 指标）：发动时机 + 对手回合里我方的额外抽卡。
    # 为什么用"对手回合里我方抽到的卡"当指标：正常抽牌只发生在自己的抽牌阶段，
    # 对手回合里我方每多抽 1 张几乎都是这两张手坑的收益（卡文：发动后对手每次召唤抽 1）。
    mulcharmy_after = mulcharmy_early = 0
    extra_draws = 0

    def leave(name: str) -> None:
        if field.get(name):
            field[name] -= 1

    for turn in turns:
        window = stream.lines[turn.start:turn.end]
        our_turn = turn.player == seat
        opponent_actions = 0
        our_actions = 0              # 这一回合我方做了几个动作（召唤/盖放/效果/攻击）
        opponent_summoned = False    # 这一回合对手召唤/特召过没有（"欢聚友伴"的时机判据）
        entered_battle = False

        for offset, text in enumerate(window):
            line_no = stream.first_line + turn.start + offset

            if our_turn and _ENTER_BATTLE_RE.match(text):
                entered_battle = True
                continue

            # 对手回合里我方抽到的卡（超过抽牌阶段那一张的都算）＝对手回合的额外抽卡
            if not our_turn:
                draw = accept._DRAW_RE.match(text.strip())
                if draw and int(draw.group(1)) == seat:
                    extra_draws += int(draw.group(2))

            # 攻击宣言也算一个动作（"动作数"口径＝召唤+盖放+效果+攻击）：直接攻击那行没有玩家号，
            # 按"谁回合谁打"归属——不数它会出现"三拳打死的斩杀回合被报成 0 个动作"这种假阳性。
            attack = _ATTACK_RE.search(text)
            if attack:
                if int(attack.group(1)) == seat:
                    our_actions += 1
                else:
                    opponent_actions += 1
                continue
            if _DIRECT_ATTACK_RE.match(text):
                if our_turn:
                    our_actions += 1
                else:
                    opponent_actions += 1
                continue

            move = accept._MOVE_RE.search(text)
            if move:
                who, name, src, dst = int(move.group(1)), move.group(2), move.group(3), move.group(4)
                if who == seat:
                    our_actions += 1
                    if is_ours(name, our_names):
                        if dst == "MonsterZone" and src != "MonsterZone":
                            field[name] = field.get(name, 0) + 1
                        elif src == "MonsterZone" and dst != "MonsterZone":
                            leave(name)
                        if src == "Hand" and dst == "Grave":
                            resource["本家被弃（手牌→墓地）"] = \
                                resource.get("本家被弃（手牌→墓地）", 0) + 1
                        elif src in ("SpellZone", "FieldZone") and dst == "Grave":
                            resource["魔陷/场地进墓地"] = resource.get("魔陷/场地进墓地", 0) + 1
                        if dst == "MonsterZone" and src == "Extra":
                            extra_plays[name] = extra_plays.get(name, 0) + 1
                else:
                    opponent_actions += 1
                    if dst == "MonsterZone" and src in (
                        "Hand", "Deck", "Extra", "Grave", "Removed", "SpellZone"
                    ):
                        opponent_summoned = True
                continue

            appear = accept._APPEAR_RE.search(text)
            if appear:
                who, name, zone = int(appear.group(1)), appear.group(2), appear.group(3)
                if who == seat:
                    our_actions += 1
                    if zone == "MonsterZone" and is_ours(name, our_names):
                        field[name] = field.get(name, 0) + 1
                else:
                    opponent_actions += 1
                    if zone == "MonsterZone":
                        opponent_summoned = True
                continue

            overlay = accept._OVERLAY_RE.search(text)
            if overlay:
                if int(overlay.group(1)) == seat:
                    our_actions += 1
                    material = overlay.group(3)
                    if is_ours(material, our_names):
                        leave(material)     # 素材离开原地（多数是怪兽区）
                        resource["本家被当素材（overlay）"] = \
                            resource.get("本家被当素材（overlay）", 0) + 1
                else:
                    opponent_actions += 1
                continue

            hand_trap = _HAND_TRAP_ACTIVATE_RE.search(text)
            if hand_trap and int(hand_trap.group(1)) == seat:
                our_actions += 1
                name = hand_trap.group(2)
                if "欢聚友伴" in name:
                    # P2 的 A/B 指标：这一手是"对手已经召唤过之后才交"还是"提前交"（见模块 docstring 第 8 条）
                    if opponent_summoned:
                        mulcharmy_after += 1
                    else:
                        mulcharmy_early += 1
                if is_hand_trap(name) and not name.startswith("朔夜时雨"):
                    if is_preemptive_hand_trap(name):
                        # 主动型（增殖的G / 欢聚友伴）：对手回合里"还没动作就先交"是**正常时机**，
                        # 只有在自己回合交才是浪费（对面多半不会在我们回合特召）。
                        if our_turn:
                            wasted_traps.append(
                                f"第{turn.order}回合（我方回合）{name}（主动型：对手回合早点交才对）"
                                f"｜行 {line_no}"
                            )
                    elif our_turn:
                        # 反应型（灰流丽/幽鬼兔/遮蒙者/屋敷童/锁鸟）只有"对面做事"时才值得交：
                        # ① 我们自己的回合交：一定是空放（对面的动作发生在它自己的回合）；
                        # ② 对面的回合、但这一回合对面到此刻 0 个动作：连锁不上任何东西。
                        wasted_traps.append(f"第{turn.order}回合（我方回合）{name}｜行 {line_no}")
                    elif opponent_actions == 0:
                        wasted_traps.append(f"第{turn.order}回合（对面 0 动作）{name}｜行 {line_no}")
                continue
            if accept._ACTIVATE_RE.search(text):
                if int(accept._ACTIVATE_RE.search(text).group(1)) != seat:
                    opponent_actions += 1
                else:
                    our_actions += 1
                continue

        if our_turn:
            our_total_actions += our_actions
            if our_actions <= 1:
                thin_turns.append((turn.order, our_actions))
            if not entered_battle:
                bigs = [name for name, count in field.items()
                        if count > 0 and attacks.get(name, 0) >= _BIG_ATTACK]
                if bigs:
                    idle_turns += 1
                    for name in bigs:
                        if name not in idle_monsters:
                            idle_monsters.append(name)

    if idle_turns >= 2:
        findings.append(
            f"局{game_no} 大怪干站：{idle_turns} 个我方回合里有 ≥{_BIG_ATTACK} 打点的怪、"
            f"却没进战阶（{'、'.join(idle_monsters)}）｜行 {stream.first_line + 1}"
        )
    for text in wasted_traps[:limit]:
        findings.append(f"局{game_no} 手坑空放：{text}")

    # 「整局 0 额外召唤」：额外卡组怪一只都没上过场。真人局里"输了但看不出为什么没做出来"那条，
    # 先用它把"根本没展开"的局挑出来（再看起手判卡手/脚本问题）。
    if not extra_plays:
        findings.append(
            f"局{game_no} 整局 0 额外召唤：额外卡组怪一次都没上过场"
            f"（我方整局动作 {our_total_actions}｜行 {stream.first_line + 1}）"
        )
    # 「我方回合 ≤1 个动作」：那一回合基本没动（偶发一次是手感，连着出现才是问题——用序号读）
    if thin_turns:
        detail = "、".join(f"第{order}回合 {count} 个" for order, count in thin_turns[:limit])
        findings.append(f"局{game_no} 我方回合动作 ≤1：{detail}")

    # 「欢聚友伴提前交」：这一手在"对手还没出怪"时就交了——按卡文是合法的（发动条件只有"自己场上没有卡"
    # ＋对手回合），但用户 P2 的口径是"等对手真的开始出怪再交"，所以单独报出来供 A/B 对比。
    if mulcharmy_early:
        findings.append(
            f"局{game_no} 欢聚友伴提前交：{mulcharmy_early} 次（对手这一回合还没召唤就交了）"
        )

    timing = {
        "欢聚友伴·对手召唤后交": mulcharmy_after,
        "欢聚友伴·提前交": mulcharmy_early,
        "对手回合里我方抽卡": extra_draws,
    }
    return findings[:limit], extra_plays, resource, timing


def scan_probe(lines: Sequence[str]) -> Tuple[int, int, int, Dict[str, int]]:
    """扫 ``[探针]`` 行（两种写法都认）：返回（提问数, 命中, 想要不在选项表, 按卡的落空次数）。

    ⚠ **"内核只给一个选项"的题不算落空**：那种题没得选（探针会写"想要=X 命中=0（基类随机）"，
    其实是照发那唯一一项）。实测升辉月对空白 20 局里"未眠之城登记落空"32 次**全是这一类**
    （`options=[1167648609]` 只有一个 k=1），把它们算成落空会让这一节虚高、掩盖真的落空。
    """

    asked = hit = missed = 0
    miss_by_card: Dict[str, int] = {}
    for line in lines:
        match = _PROBE_RE.search(line)
        if match:
            asked += 1
            wanted = match.group("wanted")
            options = [item for item in match.group("options").split(",") if item]
            if match.group("how") == "按值":
                hit += 1
            elif len(options) > 1 and wanted != "—" and wanted not in options:
                missed += 1
                card = match.group("card")
                miss_by_card[card] = miss_by_card.get(card, 0) + 1
            continue
        match = _PROBE_B_RE.search(line)
        if match:
            asked += 1
            options = [item for item in match.group("options").split(",") if item]
            wanted = [item for item in match.group("wanted").split(",") if item]
            if match.group("hit").isdigit():
                hit += 1
            elif len(options) > 1 and wanted and not any(value in options for value in wanted):
                missed += 1
                card = match.group("card")
                miss_by_card[card] = miss_by_card.get(card, 0) + 1
    return asked, hit, missed, miss_by_card


def main() -> int:
    parser = argparse.ArgumentParser(description="决策审计：把 verbose 日志扫成可疑决策清单")
    parser.add_argument("--log", required=True, help="verbose 对局日志（brain_eval --verbose-bots 产出的）")
    parser.add_argument("--deck-id", type=int, help="卡组编号（用它读卡表；与 --ydk 二选一）")
    parser.add_argument("--ydk", help="直接给 .ydk 路径")
    parser.add_argument("--data-dir", default=str(_DATA_DIR), help="插件数据目录（--deck-id 时用）")
    parser.add_argument("--cards-cdb", default=str(_CARDS_CDB), help="cards.cdb")
    parser.add_argument("--limit", type=int, default=12, help="每条规则的报数上限（默认 12）")
    parser.add_argument(
        "--label",
        default="",
        help=(
            "只看某个日志前缀的流；不给就自动认（默认跳过 ``WindBot``，那是并发镜像里两台 bot 共用的名字）。"
            "**真人房间日志只有 ``WindBot`` 一个前缀**，审计它必须显式点名：--label WindBot"
        ),
    )
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.is_file():
        raise SystemExit(f"没有这个日志：{log_path}")
    if args.ydk:
        deck_name, ydk = "(命令行给的卡表)", Path(args.ydk)
    elif args.deck_id:
        deck_name, ydk = deck_of(args.deck_id, Path(args.data_dir))
    else:
        raise SystemExit("要么给 --deck-id，要么给 --ydk")

    cards_cdb = Path(args.cards_cdb)
    accept = _load_accept()
    our_names = accept.read_deck_cards(ydk, cards_cdb)
    attacks = attack_of(cards_cdb, our_names)

    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    games = 0
    skipped = 0
    findings: List[str] = []
    extra_total: Dict[str, int] = {}
    resource: Dict[str, int] = {}
    timing: Dict[str, int] = {}
    for stream in accept._streams(lines, only_label=args.label or None):
        seat = accept._seat(stream.lines, our_names)
        if seat is None:
            skipped += 1
            continue
        games += 1
        found, extra, res, tm = audit_stream(stream, seat, our_names, attacks, args.limit, games)
        findings.extend(found)
        for name in extra:
            extra_total[name] = extra_total.get(name, 0) + 1
        for key, count in res.items():
            resource[key] = resource.get(key, 0) + count
        for key, count in tm.items():
            timing[key] = timing.get(key, 0) + count

    print(f"=== 决策审计：{log_path.name}（{deck_name}）===")
    print(f"扫描 {games} 局（跳过 {skipped} 局：认不出我方座位）；报出来的都是**下界**（口径见模块 docstring）")
    print()
    print("一、异常清单")
    if not findings:
        print("  ✔ 没有命中任何规则（下界：没报不等于没问题）")
    for line in findings:
        print(f"  {line}")
    print()
    print("二、额外卡组出场（每张卡「上过场」的局数 / 扫描局数）")
    if not extra_total:
        print("  一局都没有额外卡组怪上过场（或这段日志里我们的卡全成了 UnKnowCard）")
    for name, count in sorted(extra_total.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {name} {count}/{games}")
    print()
    print("三、本家资源去向")
    if not resource:
        print("  ✔ 没有被弃/当素材/魔陷进墓地的记录")
    for key, count in sorted(resource.items(), key=lambda item: -item[1]):
        print(f"  {key} {count} 次")
    print()
    asked, by_value, missed, miss_by_card = scan_probe(lines)
    print("四、选项登记命中（[探针] 行）")
    print(f"  提问 {asked} 次：命中 {by_value}、想要不在选项表 {missed}"
          "（⚠ 后一项对「登记值=[…]」那种探针是**上界**：待办登记被别的卡的提问先看到也会算进来；"
          "单选项的题已排除）")
    for card, count in sorted(miss_by_card.items(), key=lambda item: -item[1]):
        print(f"    ⚠ {card} 的登记没命中 {count} 次（发动被无效/错过时点，或登记的值本身写错了）")
    print()
    print("五、「欢聚友伴」兑现（P2 口径：等对手出怪再交；对手回合里我方抽到的卡＝它的收益）")
    if not timing:
        print("  （这段日志里没有可判的局）")
    for key in ("欢聚友伴·对手召唤后交", "欢聚友伴·提前交", "对手回合里我方抽卡"):
        print(f"  {key} {timing.get(key, 0)} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
