"""十副牌的"购物单"：在一轮台账上把每副牌的**该加 / 该减候选**挑出来。

用法::

    python tools/vitality_table.py --arenas s8          # 十副牌一次扫完（推荐）
    python tools/vitality_table.py --arenas s8 --verbose   # 连数值一起打

**它回答什么**：`card_usage` 只能说"用过没有"，`card_seen`（0.21.110 起）才说"到没到手"。
有了分母，一张卡就能落进三类之一：

* **该加**：只带 1~2 张、**到手就用得上**（转化率 ≥ 80%）、但**很难到手**（到手率 ≤ 35%）
  ⇒ 多一张就是把"到手率"抬上去，这是"加第 3 张"唯一站得住的理由；
* **该减**：**常常到手却很少用上**（到手率 ≥ 50% 且转化率 ≤ 40%）
  ⇒ 它更可能是在吃卡位（是"局面件"还是"脚本不会用"要另看）；
* **多余拷贝**：带 3 张、但用上它的局数不到 25%。

⚠ **这只是候选，不是结论**（2026-10-07 刻魔实测）：把引擎件从 4 张加厚到 7 张，
出件率肉眼可见地变好（赫赫君王 T≤2 0/20 → 3/20），**180 局胜率却 27.2% → 25.0%（不显著）**。
所以从这里挑出来的每一条，都要先写成影子卡组（`temp/test-decks/`）跑同协议两臂再定——
协议见 `docs/deck-audit.md` §6.9/§6.10。

口径提醒：到手率的分母是**被记录器观测到的局**（擂台镜像里约一半——见
`tools/card_vitality.py` 的 docstring），所以 `--arenas` 只能用加了 `card_seen` 之后录的轮次。
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# 默认路径按宿主布局推（插件在 <MaiBot>/plugins/<id>，数据在 <MaiBot>/data/plugins/<id>），
# 卡库用插件自带客户端里那份；老插件的历史数据用 --pool/--cdb 指过去。
# 夹逼是为了"插件不在宿主里"（单独放在 D:\mai-play-ygo）时不炸，见 check_card_coverage.py 的说明。
_HOST_ROOT = _PLUGIN_ROOT.parents[1] if len(_PLUGIN_ROOT.parents) > 1 else _PLUGIN_ROOT.parent
_DATA_DIR = _HOST_ROOT / "data" / "plugins" / "mai-play-ygo"
DEFAULT_DB = Path("temp/train/rounds.db")
DEFAULT_CDB = _PLUGIN_ROOT / "clients" / "ygopro" / "cards.cdb"

#: "该加"的门槛：到手就用得上（转化率 ≥）且很难到手（到手率 ≤）
ADD_CONVERSION = 0.80
ADD_ARRIVAL = 0.35
#: "该减"的门槛：常常到手（到手率 ≥）却很少用上（转化率 ≤）
CUT_ARRIVAL = 0.50
CUT_CONVERSION = 0.40
#: "多余拷贝"：带 3 张以上、用上它的局数不到这个比例
EXTRA_USAGE = 0.25
#: 两张表都要求"到手过这么多局"才下结论——1~2 局的样本算出来的比率全是噪声，
#: 而且能把"这副牌根本不靠抽它"的卡（从卡组/墓地直接出场的，如珠泪「宣告者的神巫」）
#: 误当成"该加"。
MIN_ARRIVED = 5


def load_vitality():
    """按路径加载 `tools/card_vitality.py`（tools 不是包，宿主环境里也没有它）。"""

    spec = importlib.util.spec_from_file_location(
        "tools_card_vitality", _PLUGIN_ROOT / "tools" / "card_vitality.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def pool_decks(pool: Path) -> List[Tuple[int, str, Path, str]]:
    """从卡组库取（编号、显示名、ydk 路径、出牌脚本名）——只取有专属脚本的那些。"""

    if not pool.exists():
        raise SystemExit(f"卡组库不存在：{pool}")
    connection = sqlite3.connect(f"file:{pool}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT deck_id, display_name, ydk_path, picked_style FROM decks "
            "WHERE picked_style <> '' ORDER BY deck_id"
        ).fetchall()
    finally:
        connection.close()
    return [(int(r[0]), str(r[1]), Path(str(r[2])), str(r[3])) for r in rows]


def classify(
    deck: Sequence[int],
    names: Dict[int, str],
    events: Dict[int, int],
    games_used: Dict[int, int],
    games_seen: Dict[int, int],
    games_seen_used: Dict[int, int],
    games: int,
    games_observed: int,
) -> Tuple[List[tuple], List[tuple], List[tuple]]:
    """把主卡分成三类候选：该加 / 该减 / 多余拷贝。

    两个率都按**被记录器观测到的那些局**算，而且转化率的分子只认"**又到手又用上**"的局
    （从卡组/墓地直接特召的卡"用过但从没到手"，算进去会得出 >100% 的转化率——
    见 `tools/card_vitality.py` 里 `load_usage` 的两条口径说明）。
    返回的每一项是 ``(卡号, 张数, 到手率, 使用率, 转化率)``。
    """

    copies: Dict[int, int] = {}
    for card_id in deck:
        copies[card_id] = copies.get(card_id, 0) + 1

    add: List[tuple] = []
    cut: List[tuple] = []
    extra: List[tuple] = []
    for card_id, count in copies.items():
        arrived = games_seen.get(card_id, 0)
        if arrived < MIN_ARRIVED or not games_observed:
            # 到手局数太少：比率全是噪声；也把"这副牌不靠抽它"的卡挡在表外（见 MIN_ARRIVED）
            continue
        both = games_seen_used.get(card_id, 0)
        arrival = arrived / games_observed
        usage = games_used.get(card_id, 0) / games
        conversion = both / arrived
        row = (card_id, count, arrival, usage, conversion)
        if count <= 2 and conversion >= ADD_CONVERSION and arrival <= ADD_ARRIVAL:
            add.append(row)
        if arrival >= CUT_ARRIVAL and conversion <= CUT_CONVERSION:
            cut.append(row)
        if count >= 3 and usage <= EXTRA_USAGE:
            extra.append(row)
    # 该加：最"到手就用"的排前面；该减：最"到手不用"的排前面；多余拷贝：用得最少的排前面
    add.sort(key=lambda r: (-r[4], r[2]))
    cut.sort(key=lambda r: (r[4], -r[2]))
    extra.sort(key=lambda r: r[3])
    return add, cut, extra


def label(card_id: int, names: Dict[int, str]) -> str:
    """卡名（查不到就只给卡号）。"""

    return names.get(card_id, str(card_id))


def render(pairs: List[tuple], names: Dict[int, str], verbose: bool, limit: int = 3) -> str:
    """把一类候选渲染成一行文本。"""

    if not pairs:
        return "无"
    shown = pairs if verbose else pairs[:limit]
    parts = []
    for card_id, count, arrival, used, conversion in shown:
        if verbose:
            parts.append(f"{label(card_id, names)}{count}张(到{arrival:.0%}用{used:.0%}转{conversion:.0%})")
        else:
            parts.append(f"{label(card_id, names)}({count}张 转{conversion:.0%})")
    text = "、".join(parts)
    if len(pairs) > len(shown):
        text += f" …共 {len(pairs)} 条"
    return text


def parse_args() -> argparse.Namespace:
    """命令行：轮次必填，库/卡组库/卡库可覆盖。"""

    parser = argparse.ArgumentParser(description="十副牌的该加/该减候选")
    parser.add_argument("--arenas", required=True, help="逗号分隔的轮次（必须含 card_seen，如 s8）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"结果库（默认 {DEFAULT_DB}）")
    parser.add_argument("--pool", type=Path, default=_DATA_DIR / "deck_pool.db", help="卡组库")
    parser.add_argument("--cdb", type=Path, default=DEFAULT_CDB, help="cards.cdb 路径")
    parser.add_argument("--verbose", action="store_true", help="打出到手率/使用率/转化率")
    return parser.parse_args()


def main() -> None:
    """逐副牌算三类候选并打印。"""

    args = parse_args()
    arenas = tuple(a.strip() for a in args.arenas.split(",") if a.strip())
    vitality = load_vitality()

    print(f"轮次：{'、'.join(arenas)}（门槛：该加＝转化≥{ADD_CONVERSION:.0%}且到手≤{ADD_ARRIVAL:.0%}；"
          f"该减＝到手≥{CUT_ARRIVAL:.0%}且转化≤{CUT_CONVERSION:.0%}；"
          f"两类都要求到手过 ≥{MIN_ARRIVED} 局）")
    skipped: List[str] = []
    for deck_id, display, ydk, style in pool_decks(args.pool):
        try:
            deck = vitality.load_deck(ydk)
            names = vitality.load_names(args.cdb, sorted(set(deck)))
            events, games_used, games_seen, games_seen_used, games, observed = vitality.load_usage(
                args.db, style, arenas
            )
        except SystemExit as error:  # 缺表/缺文件都不该让整张表停下来
            skipped.append(f"#{deck_id} {display}（{error}）")
            continue
        # 卡组库里有 30+ 副牌，只有真在这几轮里打过的那十副才有数据——没数据的静默跳过
        if not games:
            continue
        add, cut, extra = classify(
            deck, names, events, games_used, games_seen, games_seen_used, games, observed
        )
        print(f"\n### #{deck_id} {display}（{style}）：{games} 局，其中 {observed} 局读得到手牌")
        print(f"  该加：{render(add, names, args.verbose)}")
        print(f"  该减：{render(cut, names, args.verbose)}")
        print(f"  多余拷贝：{render(extra, names, args.verbose)}")
    if skipped:
        print("\n跳过（读不了台账）：" + "；".join(skipped))


if __name__ == "__main__":
    main()
