"""卡表 × 执行器登记体检：找出"卡表里有、执行器却没登记"的卡，以及只登记了一半的印刷号/异画号。

**为什么需要它**（这轮迭代的真实教训）：两处真问题都是"卡在卡表里，但执行器完全不认识它"——
* 「珠泪哀歌族型俱舍怒威族」在 WindBot 自带的 `TearlamentsExecutor` 里**一次都没出现** → 每局 3 张白板；
* 「神影依·米德拉什」在卡表里是异画号 94977270（alias 94977269），执行器 12 处写成 `card.Id == 94977269`
  → 判卡永远不成立，整条线判死。
这两种错**不会报错、也不会让 AI 卡住**，只会静默少打牌——靠翻日志很难发现，靠这个工具一屏就出来。

用法::

    python tools/check_card_coverage.py                 # 体检随机池里所有卡组
    python tools/check_card_coverage.py --deck-id 100   # 只看某一副
    python tools/check_card_coverage.py --style Yaosheng

判据（保守，宁可少报也不要误报）：
1. **没登记**：卡号（ydk 里的印刷号 **和** `cards.cdb` 的 alias 原卡号）在执行器源码里**一次都没出现**。
2. **只登记一半**：印刷号与 alias 号只出现其中一个，且源码里没有 `IsCode(`/`IsOriginalCode(` 这类写法
   —— 那意味着另一印刷号上场时判卡会落空（异画/复刻卡很容易这样）。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# 默认路径按**宿主布局**推，不写死某台机器的绝对路径：
# 插件在 <MaiBot>/plugins/<插件id>，宿主给它的数据目录是 <MaiBot>/data/plugins/<插件id>。
# 卡库与执行器源码都用插件自带的那份（clients/ygopro 与 executors/），换机器不用改。
# 老插件（yugioh.duel-arena）留下的历史数据用 --data-dir 指过去即可。
# `len(parents) > 1` 的夹逼是为了"插件不在宿主里"（例如单独放在 D:\mai-play-ygo）时也不炸：
# 那时候按 <上级>/data/plugins/<id> 推，推得不对就用 --data-dir 覆盖。
_HOST_ROOT = _PLUGIN_ROOT.parents[1] if len(_PLUGIN_ROOT.parents) > 1 else _PLUGIN_ROOT.parent
_DATA_DIR = _HOST_ROOT / "data" / "plugins" / "mai-play-ygo"
_CARDS_CDB = _PLUGIN_ROOT / "clients" / "ygopro" / "cards.cdb"
_SOURCE_DECKS = _PLUGIN_ROOT / "executors"

#: 「判卡会不会认别的印刷号」的写法特征：出现这些就认为源码考虑了别名
_ALIAS_AWARE = ("IsCode(", "IsOriginalCode(", "IsCodeOrListed", "alias")


def style_of_ydk(connection: sqlite3.Connection, deck_id: int) -> Tuple[str, str, str]:
    """取（显示名, 脚本名, ydk 路径）。脚本名优先 picked_style，退回 windbot_deck。"""

    row = connection.execute(
        "SELECT display_name, windbot_deck, picked_style, ydk_path FROM decks WHERE deck_id = ?",
        (deck_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"卡组库里没有 #{deck_id}")
    name, windbot_deck, picked, ydk = row
    return str(name), str(picked or windbot_deck or ""), str(ydk)


def load_deck_cards(ydk_path: Path) -> List[int]:
    """读 .ydk 里的主卡组 + 额外卡组卡号（保留顺序、去重交给调用方）。"""

    cards: List[int] = []
    section = ""
    for line in ydk_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if text.startswith("#main") or text.startswith("#extra"):
            section = "main"
            continue
        if text.startswith("!side"):
            section = "side"
            continue
        if text.startswith("#") or text.startswith("!") or not text:
            continue
        if section in ("main", "") and text.isdigit():
            cards.append(int(text))
    return cards


def alias_of(connection: sqlite3.Connection, card_id: int) -> int:
    """这张卡的原始卡号（cards.cdb 的 datas.alias；0 表示就是原卡）。"""

    row = connection.execute("SELECT alias FROM datas WHERE id = ?", (card_id,)).fetchone()
    return int(row[0] or 0) if row else 0


def card_name(connection: sqlite3.Connection, card_id: int) -> str:
    """卡名；查不到就退回 ``#卡号``。"""

    row = connection.execute("SELECT name FROM texts WHERE id = ?", (card_id,)).fetchone()
    return str(row[0]) if row and row[0] else f"#{card_id}"


def find_executor(style: str) -> Optional[Path]:
    """按风格名找执行器文件：搜 ``[Deck("风格名"``。"""

    if not style:
        return None
    pattern = re.compile(r'\[Deck\(\s*"' + re.escape(style) + r'"')
    for path in sorted(_SOURCE_DECKS.glob("*.cs")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            return path
    return None


def audit_deck(deck_id: int, connection: sqlite3.Connection, cards_db: sqlite3.Connection) -> List[str]:
    """体检一副牌，返回要打印的行（空列表＝没问题）。"""

    name, style, ydk_path = style_of_ydk(connection, deck_id)
    executor = find_executor(style)
    if executor is None:
        return [f"#{deck_id} {name}（脚本 {style or '(空)'}）：找不到执行器源码——"
                f"要么是内置/通用脚本，要么风格名写错，跳过"]
    source = executor.read_text(encoding="utf-8", errors="ignore")
    alias_aware = any(marker in source for marker in _ALIAS_AWARE)

    missing: List[str] = []
    half: List[str] = []
    for card_id in dict.fromkeys(load_deck_cards(Path(ydk_path))):
        original = alias_of(cards_db, card_id)
        mentions_self = f" {card_id}" in source or f"({card_id}" in source or f"={card_id}" in source
        mentions_original = bool(original) and (
            f" {original}" in source or f"({original}" in source or f"={original}" in source
        )
        label = f"{card_name(cards_db, card_id)}({card_id}" + (f"/alias {original}" if original else "") + ")"
        if not mentions_self and not mentions_original:
            missing.append(label)
        elif not alias_aware and original and (mentions_self != mentions_original):
            only = "印刷号" if mentions_self else "原卡号"
            half.append(f"{label}（源码里只有{only}，且没写 IsCode/IsOriginalCode）")

    lines = [f"#{deck_id} {name}（脚本 {style}，{executor.name}）："
             f"卡表 {len(set(load_deck_cards(Path(ydk_path))))} 种"]
    if missing:
        lines.append(f"  ❌ 完全没登记 {len(missing)} 张：{'、'.join(missing[:10])}"
                     + ("（更多见下）" if len(missing) > 10 else ""))
        for extra in missing[10:]:
            lines.append(f"     - {extra}")
    if half:
        lines.append(f"  ⚠ 只登记一半的印刷号 {len(half)} 张：{'、'.join(half[:6])}")
    if not missing and not half:
        lines.append("  ✔ 卡表里每张卡都登记了，且判卡认别名")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="卡表 × 执行器登记体检")
    parser.add_argument("--deck-id", type=int, action="append", help="只体检这些编号（可重复）")
    parser.add_argument("--style", help="只体检这个风格名对应的执行器")
    parser.add_argument("--data-dir", default=str(_DATA_DIR), help="插件数据目录")
    parser.add_argument("--cards-cdb", default=str(_CARDS_CDB), help="cards.cdb")
    parser.add_argument("--group", default="", help="只体检这个群的随机池（不填则要求给 --deck-id 或 --style）")
    args = parser.parse_args()

    pool = sqlite3.connect(f"file:{Path(args.data_dir) / 'deck_pool.db'}?mode=ro", uri=True)
    cards_db = sqlite3.connect(f"file:{Path(args.cards_cdb)}?mode=ro", uri=True)

    if args.deck_id:
        deck_ids = list(dict.fromkeys(args.deck_id))
    elif args.style:
        executor = find_executor(args.style)
        if executor is None:
            raise SystemExit(f"找不到风格 {args.style} 的执行器")
        deck_ids = [int(row[0]) for row in pool.execute(
            "SELECT deck_id FROM decks WHERE picked_style = ? OR windbot_deck = ? ORDER BY deck_id",
            (args.style, args.style),
        )]
    else:
        if not args.group:
            # 不猜"主力群"：库里的群号是宿主算出来的哈希，猜错会静默体检 0 副
            groups = [str(row[0]) for row in pool.execute(
                "SELECT DISTINCT group_id FROM decks ORDER BY group_id"
            )]
            raise SystemExit(
                "要么给 --deck-id（或 --style），要么给 --group。"
                f"这份卡组库里的群：{'、'.join(groups) if groups else '（一个都没有）'}"
            )
        deck_ids = [int(row[0]) for row in pool.execute(
            "SELECT deck_id FROM decks WHERE group_id = ? AND in_random = 1 ORDER BY deck_id",
            (args.group,),
        )]

    problems = 0
    for deck_id in deck_ids:
        for line in audit_deck(deck_id, pool, cards_db):
            print(line)
            problems += 1 if line.startswith("  ❌") or line.startswith("  ⚠") else 0
    print(f"\n体检 {len(deck_ids)} 副：{problems} 处需要看的登记问题")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
