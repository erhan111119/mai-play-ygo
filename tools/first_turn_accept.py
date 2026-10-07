"""口径①（**我方第 1 个回合**的终场）的稳健量具：只用两个**可信**来源。

**为什么另起一个**（2026-10-06 实测）：`max_board.py` / `plan_accept.py` 的场面账目是"按动作回放"
出来的，而日志里
* ``我方`` 那条流是**插件的闸门视角**——两个 bot 的动作都在里面（同一段里 ``(0 draw 5 card)`` 与
  ``(1 draw 5 card)`` 各出现两次、两套 ``Bot Hand`` 交错打印），"第 k 个 dump 是谁的场面"分不清；
* 对面客户端的流里，我们的卡**有时显示成 ``UnKnowCard``**，按卡名过滤会漏。

于是同一份日志里出现过"最大场达标 2/28"与"大姐落进额外怪兽区 15 次/30 轮"对不上的事。这个工具换两个来源：

1. **落位件（主来源）**：插件 recorder 直接写的 ``落位：第 N 回合 <谁>（<视角>） <格位> ← #<卡号>``。
   它带**绝对回合号 + 格位 + 卡号**，不依赖卡名、也不受"谁在看"影响。同一件事两个视角各记一份
   （实测同一张卡同一回合同一个格出现两行），按"同一个键在邻近行内再次出现"去重。
   ``2026-10-06`` 起 recorder **也记魔陷区/场地区**（``duel/fieldstate.py`` 的 ``spell_zone_label``），
   所以场地件（如「未眠之城」）现在也有同一份可信来源；**早于这次改动跑出来的老日志没有魔陷落位**，
   那时工具会退回第 2 条。
2. **魔陷件（老日志的退路）**：从**完整客户端流**（对面客户端那条，未合并）的动作行里数
   ``我方 <卡名> from Hand move to SpellZone``，按"这一流里我方第 1 个回合"的窗口取
   （回合归属用 ``plan_accept._turns`` 的抽牌行口径）。这条依赖卡名，会漏（``UnKnowCard``）。
3. **每局件数（补充）**：``--db`` 给擂台库时读 ``duels.card_usage``——那是 recorder 按**局**记的卡使用，
   可以回答"这一局有没有用上某张件"（不等于"第 1 回合结束时还在场上"，但它是按局、按卡、可复现的）。
   ``2026-10-06`` 起它也记**盖放**的卡（``duel/recorder.py`` 的 ``sets``）——盖着的件不再算"没用过"，
   所以跨那一版的前后两轮，这一项的数字不可直接比（新口径只会更全/更高）。

口径：一轮镜像两局，先攻那局我方第 1 回合＝绝对回合 1、后攻那局＝绝对回合 2，所以 **T ≤ 2 的落位
就覆盖了两局的"我方第 1 回合"**；报出来的次数以"轮"为分母（``13/30 轮`` ≈ 30 轮里 13 次达成）。

用法::

    python tools/first_turn_accept.py --log temp/train/mb-final-88.log --deck-id 88
    python tools/first_turn_accept.py --log temp/rounds/r16/b88.log --ydk <卡表.ydk> \
        --rounds 1 --monster "盈彩月夜之天 西艾萝-一掷乾坤" --spell "未眠之城的『盈彩月夜』"
    python tools/first_turn_accept.py --log <日志> --deck-id 88 --db temp/train/arena.db
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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

#: ``落位：第 3 回合 我方（我方） 额外怪兽区 ← #73090586`` / ``… ← 盈彩月夜之天 西艾萝-一掷乾坤``
_PLACEMENT_RE = re.compile(r"落位：第 (\d+) 回合 (\S+?)（(我方|对方)） (\S+) ← (.+)$")
_MOVE_TO_SPELL_RE = re.compile(r"\(([01]) 's (.+?) from (Hand|Deck) move to (SpellZone|FieldZone)\)")
_ROUND_LINE_RE = re.compile(r"\[\s*\d+/\d+\]\s*第\s*(\d+)\s*轮")

#: 落位行里的格位分两类：怪兽区（进 "怪件"）与魔陷区（2026-10-06 起 recorder 也记）
_MONSTER_ZONES = ("主怪兽区", "额外怪兽区")
_SPELL_ZONES = ("魔法陷阱区", "场地区", "灵摆区")


def _load_accept():
    """按路径加载 ``plan_accept``（复用它切流与切回合的口径）。"""

    spec = importlib.util.spec_from_file_location(
        "tool_plan_accept", str(_PLUGIN_ROOT / "tools" / "plan_accept.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
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


def read_ydk(ydk: Path) -> Tuple[Dict[int, str], List[int]]:
    """读 .ydk → （额外卡组卡号集合, 全部卡号列表）。卡名另外查。"""

    extra: List[int] = []
    every: List[int] = []
    section = ""
    for raw in ydk.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("#main"):
            section = "main"
            continue
        if line.startswith("#extra"):
            section = "extra"
            continue
        if line.startswith("!side"):
            section = "side"
            continue
        if not line.isdigit():
            continue
        card_id = int(line)
        every.append(card_id)
        if section == "extra":
            extra.append(card_id)
    return set(extra), every


def names_of(cards_cdb: Path, ids: Sequence[int]) -> Dict[int, str]:
    """卡号 → 卡名。"""

    unique = tuple({card_id for card_id in ids})
    if not unique:
        return {}
    connection = sqlite3.connect(f"file:{cards_cdb}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in unique)
        return {card_id: name for card_id, name in connection.execute(
            f"SELECT id, name FROM texts WHERE id IN ({placeholders})", unique
        )}
    finally:
        connection.close()


def count_rounds(lines: Sequence[str], fallback: int) -> int:
    """日志里的轮数：brain_eval 的进度行 ``[30/30] 第 14 轮`` 取最大值；没有就用命令行给的。"""

    rounds = [int(match.group(1)) for line in lines
              for match in [_ROUND_LINE_RE.search(line)] if match]
    return max(rounds) + 1 if rounds else fallback


def monster_placements(lines: Sequence[str], our_ids: Sequence[int], turn_limit: int
                       ) -> Tuple[Dict[int, List[Tuple[int, str]]], int]:
    """怪兽件：``落位`` 行 → （卡号 → [(回合, 格位)] 去重后的列表, 总条数）。

    **去重**：同一件事两个视角各记一份（实测每张卡每条都出现两次），所以同一个
    ``(回合, 格位, 卡号)`` 在**邻近行内**再出现一次就算重复。**不能只在整份日志里去重**——
    落位行没有局号，两局各自"第 1 回合把大姐放进额外怪兽区"会是同一个键
    （早先按整份日志去重，把 30 局读成了 2 次，正好少算一个数量级）。所以绑定"行号距离"：
    两个视角的那两份总是前后脚打印，跨局的两份则隔着成百上千行。
    """

    known = set(our_ids)
    result: Dict[int, List[Tuple[int, str]]] = {}
    total = 0
    last_seen: Dict[Tuple[int, str, int], int] = {}
    neighbor = 80        # 同一个事件的两份落位行之间：隔得比这个近才算同一件事
    for index, line in enumerate(lines):
        match = _PLACEMENT_RE.search(line)
        if not match:
            continue
        turn, placer, _viewer, zone, token = match.groups()
        if placer != "我方":
            continue
        card = re.search(r"#(\d+)", token)
        if card is None:
            continue
        card_id = int(card.group(1))
        if card_id not in known or int(turn) > turn_limit:
            continue
        key = (int(turn), zone, card_id)
        previous = last_seen.get(key)
        if previous is not None and index - previous <= neighbor:
            last_seen[key] = index
            continue        # 同一件事的另一个视角
        last_seen[key] = index
        result.setdefault(card_id, []).append((int(turn), zone))
        total += 1
    return result, total


def spell_placements(lines: Sequence[str], our_names: Sequence[str], our_ids: Sequence[int],
                     turn_limit: int) -> Dict[str, List[int]]:
    """魔陷件：从**完整客户端流**里数"我方 <卡名> 从手牌进魔陷/场地区"，按我方第 1 个回合取窗。

    用对面客户端那条流（未合并、回合归属可靠）。``--piece`` 点名才报，避免刷屏。
    """

    if not our_names:
        return {}
    accept = _load_accept()
    wanted = {name: [] for name in our_names}
    for stream in accept._streams(lines):
        seat = accept._seat(stream.lines, our_names)
        if seat is None:
            continue
        turns = accept._turns(stream.lines)
        first = next((turn for turn in turns if turn.player == seat), None)
        if first is None:
            continue
        for text in stream.lines[first.start:first.end]:
            move = _MOVE_TO_SPELL_RE.match(text)
            if move is None or int(move.group(1)) != seat:
                continue
            name = move.group(2)
            for want in our_names:
                if want in name:
                    wanted[want].append(first.order)
    return wanted


def usage_by_game(db: Path, our_ids: Sequence[int], arena: str = "") -> Tuple[int, Dict[int, int], int]:
    """擂台库 ``duels.card_usage``：每局里我们这副牌**用上过**哪些卡。

    返回 （局数, 卡号→出现的局数, 两件同时出现的局数由调用方按件算）。``arena`` 非空时只看该批。
    """

    if not db.is_file():
        return 0, {}, 0
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        known = set(our_ids)
        rows = connection.execute(
            "SELECT card_usage FROM duels" + (" WHERE arena = ?" if arena else ""),
            (arena,) if arena else (),
        ).fetchall()
    finally:
        connection.close()
    games = 0
    per_card: Dict[int, int] = {}
    for (usage,) in rows:
        if not usage:
            continue
        try:
            data = json.loads(usage)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        used = {int(key) for key, value in data.items() if value and str(key).isdigit()}
        if not used & known:
            continue        # 这一局里看不到我们这副牌的卡（不是这一副的对局）
        games += 1
        for card_id in used & known:
            per_card[card_id] = per_card.get(card_id, 0) + 1
    return games, per_card, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="口径①稳健读数（落位行 + 擂台库）")
    parser.add_argument("--log", required=True, help="verbose 对局日志")
    parser.add_argument("--deck-id", type=int, help="卡组编号（与 --ydk 二选一）")
    parser.add_argument("--ydk", help="直接给 .ydk 路径")
    parser.add_argument("--data-dir", default=str(_DATA_DIR), help="插件数据目录")
    parser.add_argument("--cards-cdb", default=str(_CARDS_CDB), help="cards.cdb")
    parser.add_argument("--rounds", type=int, default=1, help="日志的轮数（日志里没有进度行时用）")
    parser.add_argument("--turn-limit", type=int, default=2,
                        help="算到第几个绝对回合为止（镜像两局＝2：先攻那局 T1、后攻那局 T2）")
    parser.add_argument("--monster", nargs="*", default=[], help="验收的怪件卡名（可多选）")
    parser.add_argument("--spell", nargs="*", default=[], help="验收的魔陷件卡名（从客户端流数）")
    parser.add_argument("--db", help="擂台库（temp/train/arena.db）：补一份按局的使用率")
    parser.add_argument("--arena", default="", help="只看擂台库里某个 arena 名（配合 --db）")
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
    extra_ids, every_ids = read_ydk(ydk)
    names = names_of(cards_cdb, every_ids)
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    rounds = count_rounds(lines, args.rounds)

    print(f"=== 口径①稳健读数：{log_path.name}（{deck_name}） ===")
    print(f"轮数 {rounds}（每轮镜像 2 局；我方第 1 回合＝绝对回合 1 或 2，所以统计到 T≤{args.turn_limit}）")

    placements, total = monster_placements(lines, every_ids, args.turn_limit)
    monster_spots = {card_id: [spot for spot in spots if spot[1].startswith(_MONSTER_ZONES)]
                     for card_id, spots in placements.items()}
    spell_spots = {card_id: [spot for spot in spots if spot[1].startswith(_SPELL_ZONES)]
                   for card_id, spots in placements.items()}
    monster_spots = {card_id: spots for card_id, spots in monster_spots.items() if spots}
    spell_spots = {card_id: spots for card_id, spots in spell_spots.items() if spots}
    print()
    print(f"一、怪件落位（插件 recorder 的落位行，T≤{args.turn_limit} 去重后共 {total} 条）")
    if not placements:
        print("  一条都没有（要么这局没出怪，要么这份日志的落位行被截了）")
    for card_id, spots in sorted(monster_spots.items(), key=lambda item: -len(item[1])):
        zones: Dict[str, int] = {}
        for _turn, zone in spots:
            zones[zone] = zones.get(zone, 0) + 1
        label = zones.get("额外怪兽区", 0)
        zone_text = "、".join(f"{zone} {count}" for zone, count in
                             sorted(zones.items(), key=lambda item: -item[1]))
        extra = " ← 验收件" if card_id in extra_ids else ""
        print(f"  {names.get(card_id, f'#{card_id}')}：{len(spots)} 次 / {rounds * 2} 局｜{zone_text}"
              f"（额外怪兽区 {label}）{extra}")

    print()
    if spell_spots:
        print("二、魔陷件落位（同一份落位行，格位＝魔陷区/场地区）")
        for card_id, spots in sorted(spell_spots.items(), key=lambda item: -len(item[1])):
            zones: Dict[str, int] = {}
            for _turn, zone in spots:
                zones[zone] = zones.get(zone, 0) + 1
            zone_text = "、".join(f"{zone} {count}" for zone, count in
                                 sorted(zones.items(), key=lambda item: -item[1]))
            print(f"  {names.get(card_id, f'#{card_id}')}：{len(spots)} 次 / {rounds * 2} 局｜{zone_text}")
    elif args.spell:
        print("二、魔陷件落位（这份日志没有魔陷落位行→退回客户端流数，会漏：UnKnowCard）")
        wanted = spell_placements(lines, list(args.spell), every_ids, args.turn_limit)
        for name, turns in wanted.items():
            print(f"  {name}：{len(turns)} 次 / {rounds * 2} 局"
                  + (f"（来自第 {turns} 回合）" if turns else "（一次都没有）"))

    if args.monster:
        print()
        print("三、验收件点名核对")
        for name in args.monster:
            hit = [card_id for card_id, _ in placements.items() if name in names.get(card_id, "")]
            count = max((len(placements[card_id]) for card_id in hit), default=0)
            print(f"  {name}：T≤{args.turn_limit} 落位 {count} 次 / {rounds * 2} 局")

    if args.db:
        games, per_card, _ = usage_by_game(Path(args.db), every_ids, args.arena)
        print()
        print(f"四、按局使用率（擂台库 duels.card_usage，共 {games} 局）")
        if not games:
            print("  （库里没有我们这副牌的局：--arena 指名对不对？）")
        for name in list(args.monster) + list(args.spell):
            hit = [card_id for card_id, text in names.items() if name in text]
            count = max((per_card.get(card_id, 0) for card_id in hit), default=0)
            print(f"  {name}：{count}/{games} 局（这一局里用上过，不等于第 1 回合结束时还在场）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
