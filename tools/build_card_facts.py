"""从引擎的卡牌脚本（Lua）抽取「卡牌事实」，写进 ``knowledge.db``。

**为什么从脚本抽而不是从网页抄**：这 13,766 个 Lua 脚本是引擎**真正执行**的规则——比任何资料都准，
而且新卡包来了重跑一次就好（按 mtime 增量，幂等）。每条事实都带 ``evidence``（脚本文件:行号 +
文本片段），出了问题能追溯到具体一行。

抽出来的东西回答一个问题：**这张卡能干什么、什么时候能干、有什么代价**——
动作类别（检索/特召/破坏/无效/抽卡/墓地/除外…）、作用对象、发动时机、次数限制、
**自肃**（"只能出光属性超量"这类，是决策里最容易踩的坑）、是不是阻抗。

用法::

    python tools/build_card_facts.py --check-only     # 只看差多少，不写库
    python tools/build_card_facts.py                  # 增量写入 knowledge.db
    python tools/build_card_facts.py --force          # 全量重抽
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA  # noqa: E402  导入顺序受 sys.path 补丁影响

# CATEGORY_* → 我们的动作类别（只收"决策时看得懂"的那些）
CATEGORY_KINDS: Dict[str, str] = {
    "SEARCH": "search",          # 从卡组拿牌
    "TOHAND": "to_hand",         # 从墓地/场上回手
    "SPECIAL_SUMMON": "ss",
    "DESTROY": "destroy",
    "NEGATE": "negate",
    "DISABLE": "disable",        # 无效效果
    "TOGRAVE": "to_grave",
    "TODECK": "to_deck",
    "REMOVE": "banish",
    "DRAW": "draw",
    "RECOVER": "recover",        # 从除外回手
    "DAMAGE": "damage",
    "DECKDES": "mill",
    "TOKEN": "token",
    "EQUIP": "equip",
    "POSITION": "position",
    "ATKCHANGE": "atk_change",
    "CONTROL": "control",
    "RELEASE": "release",
    "RETURN": "bounce",
}

# 事件 → 人话（模型看到的"它在响应什么"）
EVENT_LABELS: Dict[str, str] = {
    "SPECIAL_SUMMON": "特召",
    "SPSUMMON_SUCCESS": "特召",
    "SPSUMMON": "特召",
    "CHAINING": "连锁中",
    "CHAIN_SOLVING": "连锁处理中",
    "ACTIVATE": "卡发动",
    "SUMMON": "召唤",
    "SET": "盖放",
    "TO_GRAVE": "送墓",
    "TO_HAND": "加手",
    "SEARCH": "检索",
    "DRAW": "抽卡",
    "DESTROY": "破坏",
    "REMOVE": "除外",
    "ATTACK": "攻击",
    "BATTLE": "战斗",
    "FLIP": "翻转",
    "FREE_CHAIN": "自由时点",
    "END_PHASE": "结束阶段",
    "PHASE": "阶段",
    "BE_BATTLE_DESTROYED": "被战破",
    "BE_EFFECT_DESTROYED": "被效果破坏",
    "CONTROL_CHANGED": "控制权转移",
}

# 泛化的"能拦什么"：脚本里没写"检查对方效果类别"时，按**卡自己的能力**推一个通用的
# 这类阻抗（泡影/遮蒙者/神字/破坏系）本来就是"对手发动什么就管什么"，写清楚比留空有用得多
GENERIC_HITS: Tuple[Tuple[str, str], ...] = (
    # (判据：kinds 里出现这个动作, 命中标签)
    ("disable", "any_effect"),      # 无效效果 → 拦任意发动中的效果
    ("negate", "any_effect"),
    ("destroy", "field_card"),      # 破坏 → 拦场上的卡
    ("banish", "field_card"),       # 除外 → 拦场上的卡（或墓地）
    ("bounce", "field_card"),       # 回手
    ("to_deck", "field_card"),
    ("to_grave", "any_effect"),
)

# 事件里"能当成拦截目标"的那些（其余如自由时点/阶段/战斗只说明时机，不是目标）
HITTABLE_EVENTS = {"特召", "连锁中", "连锁处理中", "卡发动", "召唤", "盖放", "送墓", "检索", "抽卡", "加手", "破坏", "除外"}

# 命中标签 → 人话（写进提示词）
HIT_LABELS: Dict[str, str] = {
    "search": "从卡组检索",
    "ss": "特殊召唤",
    "to_grave": "送墓",
    "draw": "抽卡",
    "mill": "堆墓",
    "to_hand": "加入手牌",
    "recover": "从除外回收",
    "any_effect": "任意发动中的效果",
    "field_card": "场上的卡",
    "gy_card": "墓地的卡",
    "hand_card": "手牌",
    "monster_effect": "怪兽的效果",
    "spell_trap_effect": "魔陷的发动/效果",
}

# 自肃关键词：效果文本里出现就是"发动这张卡会给自己上锁"
SELF_LOCK_RULES: Tuple[Tuple[str, str], ...] = (
    ("不能用抽卡以外的方法", "no_search"),
    ("不能用抽牌以外的方法", "no_search"),
    ("不能把卡加入手牌", "no_search"),
    ("不能从卡组把卡加入", "no_search"),
    ("只能特殊召唤", "ed_lock"),
    ("不能特殊召唤", "ed_lock"),
    ("只能用", "ed_lock"),
    ("不能把怪兽特殊召唤", "ss_lock"),
    ("不能进行特殊召唤", "ss_lock"),
    ("不能攻击", "no_attack"),
    ("不能作为", "material_lock"),
)

_TIMING_MAP: Tuple[Tuple[str, str], ...] = (
    ("EFFECT_TYPE_QUICK_O", "quick"),
    ("EFFECT_TYPE_QUICK_F", "quick"),
    ("EFFECT_TYPE_IGNITION", "ignition"),
    ("EFFECT_TYPE_TRIGGER_O", "trigger"),
    ("EFFECT_TYPE_TRIGGER_F", "trigger"),
    ("EFFECT_TYPE_ACTIVATE", "activate"),
    ("EFFECT_TYPE_CONTINUOUS", "continuous"),
    ("EFFECT_TYPE_FIELD", "field"),
    ("EFFECT_TYPE_SINGLE", "single"),
)

_RANGE_LOCATIONS: Tuple[Tuple[str, str], ...] = (
    ("LOCATION_HAND", "hand"),
    ("LOCATION_GRAVE", "grave"),
    ("LOCATION_REMOVED", "banished"),
    ("LOCATION_DECK", "deck"),
    ("LOCATION_EXTRA", "extra"),
    ("LOCATION_MZONE", "field"),
    ("LOCATION_SZONE", "field"),
)

# 文本兜底（没有 Lua 脚本的卡）：从效果描述里认动作
TEXT_KINDS: Tuple[Tuple[str, str], ...] = (
    ("从卡组把", "search"),
    ("加入手卡", "to_hand"),
    ("加入手牌", "to_hand"),
    ("特殊召唤", "ss"),
    ("破坏", "destroy"),
    ("无效", "negate"),
    ("抽", "draw"),
    ("送去墓地", "to_grave"),
    ("回到卡组", "to_deck"),
    ("除外", "banish"),
    ("解放", "release"),
    ("获得控制权", "control"),
    ("从墓地", "recover"),
)


@dataclasses.dataclass
class CardFact:
    """一张卡的事实（写进 ``card_facts``）。"""

    card_id: int
    name: str
    kinds: str = ""
    hits: str = ""
    events: str = ""
    target_scope: str = ""
    timing: str = ""
    limit_kind: str = "none"
    self_lock: str = ""
    negate_what: str = ""
    is_interaction: int = 0
    from_zones: str = ""
    confidence: str = "text"
    evidence: str = ""
    source_mtime: float = 0.0
    """卡牌脚本的 mtime（增量重抽的判据；没有脚本时为 0）。"""

    def to_row(self) -> Tuple[object, ...]:
        """转成 SQLite 行（顺序与 :data:`duel.knowledge.SCHEMA` 的列一致）。"""

        return (
            self.card_id,
            self.name,
            self.kinds,
            self.hits,
            self.events,
            self.target_scope,
            self.timing,
            self.limit_kind,
            self.self_lock,
            self.negate_what,
            int(self.is_interaction),
            self.from_zones,
            self.confidence,
            self.evidence,
            float(self.source_mtime),
            time.time(),
        )


def _first_line(text: str, needle: str) -> int:
    """``needle`` 第一次出现的行号（1 起，找不到返回 0）——记证据用。"""

    index = text.find(needle)
    if index < 0:
        return 0
    return text.count("\n", 0, index) + 1


def extract_facts(
    card_id: int,
    name: str,
    script: Optional[str],
    desc: str,
    *,
    alias_note: str = "",
    card_type: int = 0,
) -> CardFact:
    """从脚本（优先）与效果文本抽一张卡的事实。

    Args:
        card_id: 卡号。
        name: 卡名（写库便于人眼核对）。
        script: Lua 脚本原文；``None`` 表示这张卡没有自己的脚本。
        desc: 效果文本（卡库里的 ``desc``）。
        alias_note: 脚本来自别名时填上（例如 ``别名 14558127``），写进证据。
        card_type: 卡牌类型位掩码（来自 ``cards.cdb`` 的 ``datas.type``）。
            **判"能不能在对手回合动手"必须看它**：陷阱（0x4）与速攻魔法（0x10000）
            的脚本用的是 ``EFFECT_TYPE_ACTIVATE``，只看 "QUICK_O" 会把泡影、指名者这类
            全漏掉（实测：1,161 张里漏了大半）。
    """

    fact = CardFact(card_id=card_id, name=name)
    if script is None:
        # 没有脚本：从效果文本认动作，标成低置信度（它只是给模型看的线索，不参与胜负判定）。
        # 但**"能不能当阻抗"仍然要判**：陷阱/速攻魔法里有很多是文本能认出来的干扰牌。
        kinds = [kind for word, kind in TEXT_KINDS if word in (desc or "")]
        fact.kinds = ",".join(dict.fromkeys(kinds))
        fact.self_lock = ",".join(
            dict.fromkeys(flag for word, flag in SELF_LOCK_RULES if word in (desc or ""))
        )
        card_is_trap = bool(card_type & 0x4)
        card_is_quick_play = bool(card_type & 0x10000)
        interfering = any(
            kind in fact.kinds.split(",")
            for kind in ("negate", "disable", "destroy", "banish", "bounce")
        )
        fact.is_interaction = 1 if ((card_is_trap or card_is_quick_play) and interfering) else 0
        if fact.is_interaction:
            hit_kinds = [
                label for kind, label in GENERIC_HITS if kind in fact.kinds.split(",")
            ]
            fact.hits = ",".join(dict.fromkeys(hit_kinds)) or "any_effect"
        fact.evidence = "效果文本（没有卡牌脚本，通常是通常怪兽或异画）"
        return fact

    kinds: List[str] = []
    timing_set: List[str] = []
    zones: List[str] = []
    for raw, kind in CATEGORY_KINDS.items():
        if f"CATEGORY_{raw}" in script:
            kinds.append(kind)
    for raw, label in _TIMING_MAP:
        if raw in script:
            timing_set.append(label)
    for raw, label in _RANGE_LOCATIONS:
        if raw in script:
            zones.append(label)

    # "能拦什么"与"能做什么"要分开：灰流丽的条件里出现 CATEGORY_SEARCH 是说它**拦检索**，
    # 不是它自己会检索。判据是"这个 CATEGORY 出现在检查别人效果的调用附近"：
    # GetOperationInfo / IsHasCategory / GetChainInfo。
    hit_kinds: List[str] = []
    for match in re.finditer(r"CATEGORY_([A-Z_]+)", script):
        kind = CATEGORY_KINDS.get(match.group(1))
        if kind is None:
            continue
        window = script[max(0, match.start() - 60) : match.start()]
        if any(checker in window for checker in ("GetOperationInfo", "IsHasCategory", "GetChainInfo")):
            hit_kinds.append(kind)
    events = [
        label
        for raw, label in EVENT_LABELS.items()
        if f"EVENT_{raw}" in script
    ]

    fact.kinds = ",".join(dict.fromkeys(kinds))
    fact.hits = ",".join(dict.fromkeys(hit_kinds))
    fact.events = ",".join(dict.fromkeys(events))[:120]
    fact.timing = ",".join(dict.fromkeys(timing_set))
    fact.from_zones = ",".join(dict.fromkeys(zones))
    fact.limit_kind = "once_per_turn" if "SetCountLimit(1" in script else "none"
    fact.self_lock = ",".join(
        dict.fromkeys(flag for word, flag in SELF_LOCK_RULES if word in (desc or ""))
    )
    # 自肃还有脚本里的一处硬信号：EFFECT_FLAG_OATH 就是"上锁"标记
    if "EFFECT_FLAG_OATH" in script and "oath" not in fact.self_lock:
        fact.self_lock = (fact.self_lock + ",oath").strip(",")

    if "CATEGORY_NEGATE" in script or "CATEGORY_DISABLE" in script:
        fact.negate_what = "effect"
    elif "CATEGORY_SEARCH" in script:
        fact.negate_what = "search"

    # 阻抗判定：**能在对手回合动手**（速攻怪效 / 陷阱 / 速攻魔法），且做的事是干扰或惩罚。
    # 三条都要看：只看脚本里的 QUICK_O 会把陷阱与速攻魔法全部漏掉（实测漏了一半以上）。
    card_is_trap = bool(card_type & 0x4)
    card_is_quick_play = bool(card_type & 0x10000)
    quick = (
        "EFFECT_TYPE_QUICK_O" in script
        or "EFFECT_TYPE_QUICK_F" in script
        or card_is_trap
        or card_is_quick_play
    )
    interfering = {
        kind
        for kind in fact.kinds.split(",")
        if kind in ("negate", "disable", "destroy", "banish", "bounce", "release", "control")
    }
    hit_kinds_now = set(fact.hits.split(",")) | interfering
    # "惩罚型"手坑（增殖的G、小丑与锁鸟）什么都不无效，而是**用手牌里的自己**响应对手
    # （脚本里就是"速攻效果 + 从手牌发动"）。**只有这一类**才用事件当"能拦什么"——
    # 否则速攻魔法里的展开件（例如"平行瞬间移动"）也会被算成阻抗（实测抽样抓到的误判）。
    handtrap_like = (
        ("EFFECT_TYPE_QUICK_O" in script or "EFFECT_TYPE_QUICK_F" in script)
        and "LOCATION_HAND" in script
    )
    punish = bool(events) and handtrap_like
    fact.is_interaction = 1 if (quick and (interfering or punish or hit_kinds_now)) else 0

    # 判为阻抗但"检查对方效果类别"那一套没命中时，补**泛化的能拦什么**：
    # 实测 1,161 张阻抗里 1,123 张的 hits 是空的——等于"该不该交坑"这条线几乎没有资料。
    # 这类卡（泡影/遮蒙者/神字/破坏系）本来就是"对手发动什么就管什么"，
    # 按它自己的能力推一个通用标签，比留空有用得多；能区分的再细分到怪兽/魔陷。
    if fact.is_interaction and not hit_kinds:
        for kind, label in GENERIC_HITS:
            if kind in kinds:
                hit_kinds.append(label)
        if any(label in hit_kinds for label in ("any_effect", "field_card")):
            if "LOCATION_MZONE" in script:
                hit_kinds.append("monster_effect")
            if "LOCATION_SZONE" in script:
                hit_kinds.append("spell_trap_effect")
    if fact.is_interaction and not hit_kinds and events:
        # 惩罚型手坑：它拦的就是它响应的事件（"对手特召时…""对手把手牌加入时…"）。
        # **只收"对手的动作"那几种**：自由时点/阶段/战斗不是能拦的目标——
        # 写进去会出现"拦「自由时点」"这种废话（实测抽样里就抓到了）。
        hit_kinds.extend([label for label in events if label in HITTABLE_EVENTS][:3])
    if fact.is_interaction and not hit_kinds:
        hit_kinds.append("any_effect")
    fact.hits = ",".join(dict.fromkeys(hit_kinds))
    fact.confidence = "script"
    evidence_bits = ["脚本" + (f"（{alias_note}）" if alias_note else "")]
    if kinds:
        evidence_bits.append(f"动作在第 {_first_line(script, 'CATEGORY_')} 行")
    if events:
        evidence_bits.append(f"事件在第 {_first_line(script, 'EVENT_')} 行")
    fact.evidence = "，".join(evidence_bits)
    return fact


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从卡牌脚本抽取卡牌事实，写入 knowledge.db")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--ygopro-dir", default="", help="ygopro 工作目录（含 cards.cdb 与 script/）")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--check-only", action="store_true", help="只统计要抽多少，不写库")
    parser.add_argument("--force", action="store_true", help="全量重抽（默认按脚本 mtime 增量）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 张（调试用）")
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    """返回 ``(script 目录, knowledge.db 路径)``。"""

    from duel.knowledge import default_data_dir, default_knowledge_db

    ygopro_dir = Path(args.ygopro_dir) if args.ygopro_dir else None
    if ygopro_dir is None:
        import tomllib

        config = Path(args.plugin_root) / "config.toml"
        with config.open("rb") as handle:
            paths = (tomllib.load(handle).get("paths") or {})
        ygopro_dir = Path(str(paths.get("ygopro_dir") or ""))
    if not (ygopro_dir / "script").is_dir():
        raise SystemExit(f"[错误] 找不到卡牌脚本目录：{ygopro_dir / 'script'}")
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    return ygopro_dir / "script", default_knowledge_db(data_dir)


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    script_dir, db_path = resolve_paths(args)

    # 卡名与效果文本：从引擎卡库读（只读，绝不写它）
    cdb = script_dir.parent / "cards.cdb"
    if not cdb.is_file():
        print(f"[错误] 找不到卡库：{cdb}")
        return 2
    cards_db = sqlite3.connect(f"file:{cdb.as_posix()}?mode=ro", uri=True)
    names = {int(cid): str(name) for cid, name in cards_db.execute("SELECT id, name FROM texts")}
    descs = {int(cid): str(desc or "") for cid, desc in cards_db.execute("SELECT id, desc FROM texts")}
    # 异画/别名：引擎在没有 ``c<id>.lua`` 时会去加载 ``c<alias>.lua``
    # （实测：灰流丽异画 14558128 → 14558127、天霆号 90448282 → 90448279），所以抽事实也要跟着走
    aliases = {
        int(cid): int(alias or 0)
        for cid, alias in cards_db.execute("SELECT id, alias FROM datas")
    }
    # 卡牌类型位掩码：判"陷阱/速攻魔法"要用（见 extract_facts 的说明）
    types = {int(cid): int(value or 0) for cid, value in cards_db.execute("SELECT id, type FROM datas")}
    cards_db.close()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    known: Dict[int, float] = {}
    if not args.force:
        for cid, mtime in connection.execute("SELECT card_id, source_mtime FROM card_facts"):
            known[int(cid)] = float(mtime or 0.0)

    todo: List[int] = []
    for cid in sorted(names):
        script_file = script_dir / f"c{cid}.lua"
        mtime = script_file.stat().st_mtime if script_file.is_file() else 0.0
        if not args.force and cid in known and known[cid] >= mtime:
            continue
        todo.append(cid)
        if args.limit and len(todo) >= args.limit:
            break

    total = len(names)
    print(f"卡库共 {total} 张；需要处理 {len(todo)} 张（其余是已抽过且脚本没变的）")
    if args.check_only:
        print("--check-only：没有写库。")
        return 0
    if not todo:
        print("都抽过了，没有要更新的。")
        return 0

    rows = []
    started = time.monotonic()
    alias_hits = 0
    for index, cid in enumerate(todo, 1):
        script_file = script_dir / f"c{cid}.lua"
        script = script_file.read_text(encoding="utf-8", errors="replace") if script_file.is_file() else None
        alias_note = ""
        if script is None:
            # 顺着别名找脚本（最多 3 跳，防止数据里出现环）
            hop, seen = int(cid), {int(cid)}
            for _ in range(3):
                hop = int(aliases.get(hop, 0))
                if not hop or hop in seen:
                    break
                seen.add(hop)
                candidate = script_dir / f"c{hop}.lua"
                if candidate.is_file():
                    script = candidate.read_text(encoding="utf-8", errors="replace")
                    alias_note = f"别名 {hop}"
                    alias_hits += 1
                    break
        fact = extract_facts(
            cid,
            names.get(cid, str(cid)),
            script,
            descs.get(cid, ""),
            alias_note=alias_note,
            card_type=types.get(cid, 0),
        )
        fact.source_mtime = script_file.stat().st_mtime if script_file.is_file() else 0.0
        rows.append(fact.to_row())
        if index % 2000 == 0:
            print(f"  已抽 {index}/{len(todo)}（{time.monotonic() - started:.0f}s）")

    connection.executemany(
        "INSERT OR REPLACE INTO card_facts (card_id, name, kinds, hits, events, target_scope,"
        " timing, limit_kind, self_lock, negate_what, is_interaction, from_zones, confidence,"
        " evidence, source_mtime, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    count = connection.execute("SELECT COUNT(*) FROM card_facts").fetchone()[0]
    with_script = connection.execute(
        "SELECT COUNT(*) FROM card_facts WHERE confidence = 'script'"
    ).fetchone()[0]
    interactions = connection.execute(
        "SELECT COUNT(*) FROM card_facts WHERE is_interaction = 1"
    ).fetchone()[0]
    connection.close()
    print(
        f"写入完成：库内共 {count} 张（有脚本 {with_script} 张、判为阻抗 {interactions} 张）；"
        f"本次 {len(rows)} 张（其中 {alias_hits} 张沿用了别名/异画脚本），"
        f"用时 {time.monotonic() - started:.0f}s\n库：{db_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
