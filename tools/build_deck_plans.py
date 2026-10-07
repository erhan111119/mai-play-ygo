"""把「卡牌事实 + 人工整理的系列/卡组知识」合成 ``deck_plans``（每副牌一条、每个系列一条）。

产出就是**给模型查的两类知识**：

* ``deck:<编号>``：机器人自己这副牌怎么打——主轴系列、关键起手件（从事实里挑）、
  手里的阻抗、自肃（会锁死自己的那几张）、额外卡组的终端候选，再加上人工整理的联动/打法说明；
* ``series:<系列码>``：**对手**在打什么体系、它的关键动作与威胁点、怎么拦——
  对手亮出 ≥3 张同系列卡时会被检索到。

自动部分负责"全都有"（82 副牌、106 个系列一条不漏），人工部分（``tools/series_notes.py``）
负责"说得对"，合并时人工说明放最前面。

用法::

    python tools/build_deck_plans.py --check-only
    python tools/build_deck_plans.py            # 写入 knowledge.db
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, Knowledge  # noqa: E402  导入顺序受 sys.path 补丁影响

# 一份知识最多多少字（检索时还会再截断一次，这里先控制住，免得库里全是长文）
MAX_BODY_CHARS = 600
# 每个列表最多写几张卡（提示词里够用即可）
MAX_CARDS_PER_SECTION = 6

# 起手件的判据：事实里出现这些动作的卡，通常是"能动起来"的牌
STARTER_KINDS = ("search", "ss", "to_hand", "draw")


def load_notes():
    """加载人工整理的知识（``tools/series_notes.py``）。"""

    spec = importlib.util.spec_from_file_location(
        "knowledge_notes", str(Path(__file__).resolve().parent / "series_notes.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成卡组/系列知识，写入 knowledge.db")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--check-only", action="store_true", help="只报告要写多少条，不落库")
    return parser.parse_args()


def read_deck_cards(path: Path) -> Dict[str, List[int]]:
    """读 .ydk → ``{"main": [...], "extra": [...], "side": [...]}``。"""

    sections: Dict[str, List[int]] = {"main": [], "extra": [], "side": []}
    current = "main"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return sections
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            lowered = stripped.lower()
            current = "extra" if "extra" in lowered else ("side" if "side" in lowered else "main")
            continue
        if stripped.startswith("!"):
            current = "side"
            continue
        if stripped.isdigit():
            sections[current].append(int(stripped))
    return sections


def series_of(knowledge: Knowledge, card_ids: Sequence[int]) -> Counter:
    """统计这批卡涉及的系列（按出现张数）。"""

    counter: Counter = Counter()
    for card_id in card_ids:
        for code in knowledge._setcodes_of(int(card_id)):  # noqa: SLF001 - 同包内部工具
            counter[code] += 1
    return counter


def card_names(knowledge: Knowledge, card_ids: Sequence[int]) -> List[str]:
    """卡号 → 卡名（去重、保序；知识库里取不到就退回卡号）。"""

    names: List[str] = []
    for card_id in dict.fromkeys(int(cid) for cid in card_ids):
        fact = knowledge.card_facts(card_id)
        names.append(fact.name if fact is not None else f"#{card_id}")
    return names


def pick_starters(knowledge: Knowledge, card_ids: Sequence[int]) -> List[int]:
    """挑"能动起来"的牌：事实里带检索/特召/加手/抽卡、且**不是阻抗**的。

    排序偏好：动作多 > 一回合一次（说明是效果怪而非白板）。手坑不算起手件——
    它虽然也"能特召"（很多手坑有特召条件），但拿它开场等于空过。
    """

    scored: List[Tuple[int, int]] = []
    for card_id in dict.fromkeys(int(cid) for cid in card_ids):
        fact = knowledge.card_facts(card_id)
        if fact is None or fact.is_interaction:
            continue
        kinds = set(fact.kinds) | set(fact.hits)
        score = sum(1 for kind in STARTER_KINDS if kind in kinds)
        if not score:
            continue
        if fact.limit_kind == "once_per_turn":
            score += 1
        scored.append((score, -card_id))
    scored.sort(reverse=True)
    return [-neg for _score, neg in scored[:MAX_CARDS_PER_SECTION]]


def pick_interactions(knowledge: Knowledge, card_ids: Sequence[int]) -> List[int]:
    """挑手里的阻抗（含手坑与盖坑），去重。"""

    found: List[int] = []
    for card_id in dict.fromkeys(int(cid) for cid in card_ids):
        fact = knowledge.card_facts(card_id)
        if fact is not None and fact.is_interaction:
            found.append(card_id)
    return found[:MAX_CARDS_PER_SECTION]


def pick_locks(knowledge: Knowledge, card_ids: Sequence[int]) -> List[Tuple[int, Tuple[str, ...]]]:
    """挑"会给自己上锁"的牌（自肃），去重——这是决策里最容易踩的坑。"""

    found: List[Tuple[int, Tuple[str, ...]]] = []
    for card_id in dict.fromkeys(int(cid) for cid in card_ids):
        fact = knowledge.card_facts(card_id)
        if fact is None or not fact.self_lock:
            continue
        found.append((int(card_id), fact.self_lock))
    return found[:MAX_CARDS_PER_SECTION]


def compose_deck_body(
    knowledge: Knowledge,
    *,
    deck_id: int,
    display_name: str,
    sections: Dict[str, List[int]],
    notes,
) -> str:
    """组装一副牌的说明（人工说明在最前面）。"""

    main, extra, side = sections["main"], sections["extra"], sections["side"]
    lines: List[str] = []
    note = notes.DECK_NOTES.get(int(deck_id))
    if note:
        lines.append("【打法】" + note)

    series = series_of(knowledge, main + extra)
    if series:
        top = series.most_common(4)
        pieces = []
        for code, count in top:
            curated = notes.SERIES_NOTES.get(int(code))
            label = f"0x{code:x}（{count} 张）"
            pieces.append(label)
        lines.append("【主轴系列】" + "、".join(pieces) + f"；主卡 {len(main)} 张、额外 {len(extra)} 张")

    starters = pick_starters(knowledge, main)
    if starters:
        lines.append("【关键起手件】" + "、".join(card_names(knowledge, starters)))

    interactions = pick_interactions(knowledge, main + side)
    if interactions:
        lines.append("【手里的阻抗】" + "、".join(card_names(knowledge, interactions)))

    locks = pick_locks(knowledge, main)
    if locks:
        rendered = [
            f"{card_names(knowledge, [card_id])[0]}（{'/'.join(flags[:2])}）" for card_id, flags in locks
        ]
        lines.append("【自肃/代价】" + "、".join(rendered))

    if extra:
        lines.append("【额外卡组】" + "、".join(card_names(knowledge, extra)[:MAX_CARDS_PER_SECTION]) + "…")

    body = "；".join(lines)
    return body[:MAX_BODY_CHARS]


def compose_series_body(
    knowledge: Knowledge,
    *,
    setcode: int,
    members: Sequence[Tuple[int, int]],
    notes,
    common_handtraps: Sequence[int] = (),
) -> str:
    """组装一个系列的说明（人工说明在最前面）。

    ``members`` 是 ``[(卡号, 在这个卡池里出现的张数), ...]``（已按出现次数排序）。
    ``common_handtraps`` 是本机卡组池里最常见的通用阻抗（给模型一个"对手大概率带什么"的先验）。
    """

    lines: List[str] = []
    note = notes.SERIES_NOTES.get(int(setcode))
    if note:
        lines.append(note)

    card_ids = [card_id for card_id, _count in members]
    lines.append("【成员样本】" + "、".join(card_names(knowledge, card_ids)[:MAX_CARDS_PER_SECTION]))
    starters = pick_starters(knowledge, card_ids)
    if starters:
        lines.append("【起手点】" + "、".join(card_names(knowledge, starters)))
    # 「最该拦的牌」：判断"该不该交坑"必须有**具体目标**——只知道"这套牌有检索"没用。
    # 人工整理了就用人工的（更准），否则退回"它的起手点就是该拦的地方"。
    target_note = notes.SERIES_KEY_TARGETS.get(int(setcode))
    if target_note:
        lines.append("【最该拦的牌】" + target_note)
    elif starters:
        lines.append(
            "【该拦的地方】它的起手点（上面那几张）就是最该交坑的点；"
            "拦不掉时优先拆它的场地/永续，而不是硬拦普通检索"
        )
    interactions = pick_interactions(knowledge, card_ids)
    if interactions:
        # **这条是给"该不该交坑"用的**：判断要不要把灰流丽交出去，先得知道对手这套牌里有几张坑、
        # 是哪些——他手里的坑越少，我这边的动作越安全（反之亦然）。
        lines.append("【这套牌可能有的阻抗（含手坑）】" + "、".join(card_names(knowledge, interactions)))
    if common_handtraps:
        # 本家没坑的卡组也会带通用手坑，所以再给一条"按本机卡组池统计的常见选择"
        lines.append(
            "【通用手坑先验（本机卡组池统计，对手大概率也带）】"
            + "、".join(card_names(knowledge, common_handtraps))
        )
    locks = pick_locks(knowledge, card_ids)
    if locks:
        lines.append("【有自肃的卡】" + "、".join(card_names(knowledge, [cid for cid, _ in locks])))
    body = "；".join(lines)
    return body[:MAX_BODY_CHARS]


def series_title(knowledge: Knowledge, setcode: int, members: Sequence[Tuple[int, int]], notes) -> str:
    """系列标题：人工整理了就用它的名字，否则用"出现最多的那张卡"当线索。"""

    note = notes.SERIES_NOTES.get(int(setcode))
    if note:
        head = note.split("：", 1)[0].split("（", 1)[0].strip()
        if head:
            return head[:24]
    top_name = card_names(knowledge, [members[0][0]])[0] if members else ""
    return f"系列 0x{int(setcode) & 0xffff:x}" + (f"（如「{top_name}」）" if top_name else "")


def compose_auto_interactions(
    knowledge: Knowledge, pool_cards: Sequence[int], notes
) -> List[Tuple[int, str, str, str, str]]:
    """给**池子里每一张阻抗**生成交互行（人工判定过的那些不覆盖）。

    为什么要有这一层：人工只判了 13 张常用坑，而池子里有 500+ 张能当阻抗的卡——
    "这张坑该不该交"如果库里没有它的行，模型就只剩"这张牌能拦什么"这一句事实。
    这里从卡牌事实**推**出结论，规则是保守的、写在明面上的：

    * 拦 **检索 / 特召 / 送墓 / 堆墓 / 加手** 这类"对手做引擎"的动作 → ``hit``（拦这些通常不亏）；
    * 拦 **抽卡 / 除外 / 任意效果 / 场上的卡** → ``neutral``（时机看局面，别乱给"该交"）。

    不写 ``hold``：那需要知道"留着下回合更值"，机器推不出来，人工那 13 张才写。
    理由里也照实标"（自动推断）"，让人一眼看出这不是人工判定的。
    """

    engine_kinds = {"search", "ss", "to_grave", "mill", "to_hand"}
    ambiguous_kinds = {"draw", "banish", "any_effect", "field_card", "monster_effect", "spell_trap_effect"}
    rows: List[Tuple[int, str, str, str, str]] = []
    for card_id in dict.fromkeys(int(cid) for cid in pool_cards):
        if int(card_id) in notes.INTERACTION_NOTES:
            continue  # 人工判定过，别覆盖
        fact = knowledge.card_facts(int(card_id))
        if fact is None or not fact.is_interaction:
            continue
        verdict_kinds: Dict[str, str] = {}
        for tag in fact.hits:
            if tag in engine_kinds:
                verdict_kinds[tag] = "hit"
            elif tag in ambiguous_kinds:
                verdict_kinds.setdefault(tag, "neutral")
        for tag, verdict in list(verdict_kinds.items())[:4]:
            rows.append(
                (
                    int(card_id),
                    tag,
                    verdict,
                    f"（自动推断）{fact.name} 能拦「{notes.KIND_LABELS.get(tag, tag)}」",
                    "auto",
                )
            )
    return rows


def compose_interaction_rows(knowledge: Knowledge, notes) -> List[Tuple[int, str, str, str, str]]:
    """把人工整理的交互知识变成 ``interactions`` 行：``(坑, 动作类别, 结论, 理由, 来源)``。"""

    rows: List[Tuple[int, str, str, str, str]] = []
    for handtrap_id, mapping in notes.INTERACTION_NOTES.items():
        fact = knowledge.card_facts(int(handtrap_id))
        name = fact.name if fact is not None else f"#{handtrap_id}"
        for kind, verdict in mapping.items():
            kind_label = notes.KIND_LABELS.get(kind, kind)
            verdict_label = notes.VERDICT_LABELS.get(verdict, verdict)
            rows.append(
                (
                    int(handtrap_id),
                    kind,
                    verdict,
                    f"{name} 对「{kind_label}」：{verdict_label}",
                    "curated",
                )
            )
    return rows


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    from duel.knowledge import default_data_dir, default_knowledge_db

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    knowledge = Knowledge(
        default_knowledge_db(data_dir),
        plugin_root=Path(args.plugin_root),
    )
    if not knowledge.available:
        print(
            "[错误] 还没有知识库：先跑 `python tools/build_card_facts.py` 把卡牌事实抽出来。\n"
            f"  期望位置：{knowledge.path}"
        )
        return 2
    notes = load_notes()

    pool_path = data_dir / "deck_pool.db"
    if not pool_path.is_file():
        print(f"[错误] 找不到卡组池：{pool_path}")
        return 2
    pool = sqlite3.connect(f"file:{pool_path.as_posix()}?mode=ro", uri=True)
    decks = list(pool.execute("SELECT deck_id, display_name, ydk_path FROM decks ORDER BY deck_id"))

    deck_rows: List[Tuple[str, str, int, str, str, str, float]] = []
    series_members: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    handtrap_decks: Counter = Counter()
    for deck_id, display_name, ydk_path in decks:
        sections = read_deck_cards(Path(ydk_path))
        if not sections["main"] and not sections["extra"]:
            continue
        # 通用手坑统计：本机 82 副牌里有多少副带了它（给"对手大概率带什么"一个先验）
        for card_id in dict.fromkeys(sections["main"] + sections["side"]):
            fact = knowledge.card_facts(int(card_id))
            if fact is not None and fact.is_interaction:
                handtrap_decks[int(card_id)] += 1
        body = compose_deck_body(
            knowledge,
            deck_id=int(deck_id),
            display_name=str(display_name),
            sections=sections,
            notes=notes,
        )
        source = "curated" if int(deck_id) in notes.DECK_NOTES else "auto"
        deck_rows.append(
            (f"deck:{int(deck_id)}", "deck", int(deck_id), str(display_name), body, source, time.time())
        )
        for code, count in series_of(knowledge, sections["main"] + sections["extra"]).items():
            for card_id in sections["main"] + sections["extra"]:
                if code in knowledge._setcodes_of(int(card_id)):  # noqa: SLF001 - 同包内部工具
                    series_members[code].append((count, int(card_id)))

    # 只写"在这批卡组里出现过 ≥3 张"的系列：长尾系列等真遇到时再补（按需生成）
    # 已有"人工整理 / 模型写过"的系列说明就别用自动拼的覆盖掉（模型那份是散文、信息量更大）
    existing_series: Dict[str, str] = {}
    if knowledge.path.is_file():
        connection = sqlite3.connect(f"file:{knowledge.path.as_posix()}?mode=ro", uri=True)
        try:
            existing_series = {
                str(key): str(source)
                for key, source in connection.execute(
                    "SELECT plan_key, source FROM deck_plans WHERE kind = 'series'"
                )
            }
        finally:
            connection.close()

    series_rows: List[Tuple[str, str, int, str, str, str, float]] = []
    for code, members in sorted(series_members.items(), key=lambda item: -len(item[1])):
        unique: Dict[int, int] = {}
        for _slot, card_id in members:
            unique[card_id] = unique.get(card_id, 0) + 1
        if len(unique) < 3:
            continue
        ordered = sorted(unique.items(), key=lambda item: -item[1])
        plan_key = f"series:0x{int(code) & 0xffff:x}"
        # 只保护**模型写的散文**（source='ai'，它信息量比自动拼的大）；人工整理的会重新拼一遍，
        # 这样新加的段落（阻抗/该拦哪张/手坑先验）能跟着一起进库
        if existing_series.get(plan_key) == "ai":
            continue
        body = compose_series_body(
            knowledge,
            setcode=code,
            members=ordered,
            notes=notes,
            common_handtraps=[card_id for card_id, _n in handtrap_decks.most_common(5)],
        )
        source = "curated" if int(code) in notes.SERIES_NOTES else "auto"
        series_rows.append(
            (
                f"series:0x{int(code) & 0xffff:x}",
                "series",
                0,
                series_title(knowledge, code, ordered, notes),
                body,
                source,
                time.time(),
            )
        )

    pool_cards = [
        card_id
        for _deck_id, _name, path in decks
        for card_id in (
            read_deck_cards(Path(path))["main"] + read_deck_cards(Path(path))["side"]
        )
    ]
    interaction_rows = compose_interaction_rows(knowledge, notes)
    auto_rows = compose_auto_interactions(knowledge, pool_cards, notes)
    knowledge.close()

    print(f"卡组知识：{len(deck_rows)} 条（其中人工整理 {sum(1 for r in deck_rows if r[5] == 'curated')} 条）")
    print(f"系列知识：{len(series_rows)} 条（其中人工整理 {sum(1 for r in series_rows if r[5] == 'curated')} 条）")
    print(
        f"交互知识：人工 {len(interaction_rows)} 条（{len(set(r[0] for r in interaction_rows))} 张坑）"
        f"＋自动推断 {len(auto_rows)} 条（{len(set(r[0] for r in auto_rows))} 张）"
    )
    if args.check_only:
        for row in deck_rows[:3]:
            print(f"  样例 {row[0]}：{row[4][:120]}…")
        print("--check-only：没有写库。")
        return 0

    connection = sqlite3.connect(knowledge.path)
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT OR REPLACE INTO deck_plans (plan_key, kind, deck_id, title, body, source, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        deck_rows + series_rows,
    )
    # 人工与自动两层都整体重写（它们都来自这一轮构建，不存在手工维护的增量）
    connection.execute("DELETE FROM interactions WHERE source IN ('curated', 'auto')")
    connection.executemany(
        "INSERT INTO interactions (handtrap_id, target_kind, verdict, why, source, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (row[0], row[1], row[2], row[3], row[4], time.time())
            for row in interaction_rows + auto_rows
        ],
    )
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('deck_plans_built_at', ?)",
        (str(time.time()),),
    )
    connection.commit()
    connection.close()
    print(f"写入完成：{knowledge.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
