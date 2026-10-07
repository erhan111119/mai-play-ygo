"""卡组进化搜索（方案 A）：换牌 → 自动对战 → 只保留统计上真的更强的版本。

**这一版做到哪一步**（说清楚，别把没做的当做了）：单轮、多个候选，各自与固定对手池对战，
按区间判定留下或丢弃，采纳就**新建**一副卡组（版本化命名，绝不覆盖原牌）。
多轮爬山（拿上一轮的赢家继续换）与"独立复测"留给下一次运行——每轮都可复现，重跑即可累加。

**判定口径**（照 ``train/deckopt.py`` 的规矩来，都是实测教训）：

* 每个候选用自己的**牌序种子区间**（``--seed`` + 候选序号）⇒ 候选之间不共用开局，
  避免"背下这几十个开局"的假进步；
* 换进来/换出去的卡先过 ``deck_issues`` ⇒ 缺卡、缺脚本、同名超限的候选**直接丢掉**，
  不去打（否则会重演"35 张装不上、只剩艾克佐迪亚"那种荒唐结果）；
* 只有"95% 区间整体高于基线"才采纳 ⇒ 40 局 62% 那种一律不算赢；
* 一批里"没打起来"的局超过两成就**不比较**（``_BROKEN_SHARE_LIMIT``）：bot 崩溃的那几局
  既没有胜负也没打出来，算成"候选输了"是拿坏数据下结论；
* 区间判定过不了时走**死牌清理**（最近 N 局一次没被动过的卡换成本系列脚本齐全的牌）——
  它明确不声称"变强"，只清理白占卡位的死牌，理由照实写进结果。

用法::

    python tools/optimize_deck.py --deck-id 37 --opponents 38,80 --rounds 50 --candidates 4

退出码：0=有候选被采纳，1=没采纳，2=出错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import argparse
import asyncio
import importlib.util
import json
import random
import sqlite3
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# 一批对局里"没打起来"（bot 报错/超时、没有胜负）的占比超过这个数，就不拿这批数字下结论
_BROKEN_SHARE_LIMIT = 0.2

from duel.deckpool import DeckPool  # noqa: E402  导入顺序受 sys.path 补丁影响
from train.deckopt import (  # noqa: E402
    accept_candidate,
    apply_swap,
    deck_issues,
    missing_scripts,
    propose_swaps,
    prune_for_dead_cards,
    rounds_needed,
)
from train.store import DuelStore  # noqa: E402  导入顺序受 sys.path 补丁影响


def load_cli():
    """按文件路径加载擂台 CLI（复用它的路径解析与选手构造）。"""

    spec = importlib.util.spec_from_file_location(
        "train_arena_cli", str(_PLUGIN_ROOT / "tools" / "train_arena.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="卡组进化搜索：换牌 → 对战 → 按统计采纳")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--deck-id", type=int, required=True, help="要优化的卡组（卡组池编号）")
    parser.add_argument("--opponents", required=True, help="对手卡组编号，逗号分隔（筛选用，别拿它当最终结论）")
    parser.add_argument("--rounds", type=int, default=50, help="每个候选每个对手跑几轮镜像（50 = 100 局）")
    parser.add_argument("--candidates", type=int, default=4, help="这一轮生成几个候选")
    parser.add_argument("--swap-size", type=int, default=2, help="每个候选换几张")
    parser.add_argument("--seed", type=int, default=20260921, help="牌序种子基准（可复现）")
    parser.add_argument("--parallel", type=int, default=4, help="同时跑几局")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--log", default="", help="结果日志（json 行）；默认 temp/opt/<卡组>-<时间>.jsonl")
    parser.add_argument("--no-save", action="store_true", help="只报告，不新建卡组")
    return parser.parse_args()


def plugin_data_dir(plugin_root: Path) -> Path:
    """插件数据目录（宿主约定：``<MaiBot>/data/plugins/<插件 id>/``）。"""

    manifest = plugin_root / "_manifest.json"
    plugin_id = ""
    try:
        plugin_id = str(json.loads(manifest.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        plugin_id = ""
    return plugin_root.parent.parent / "data" / "plugins" / plugin_id if plugin_id else plugin_root / "data"


def read_ydk(deck_path: Path) -> Tuple[List[int], List[str]]:
    """读 .ydk，返回 ``(主卡组卡号, 原始行)``。"""

    lines = deck_path.read_text(encoding="utf-8", errors="replace").splitlines()
    main: List[int] = []
    in_main = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#main"):
            in_main = True
            continue
        if stripped.startswith(("#", "!")):
            in_main = False
            continue
        if in_main and stripped.isdigit():
            main.append(int(stripped))
    return main, lines


def ydk_text_with_main(lines: Sequence[str], main: Sequence[int]) -> str:
    """把主卡组换成新卡表（额外/副卡组原样保留）。"""

    out: List[str] = []
    in_main = False
    written = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#main"):
            out.append("#main")
            in_main = True
            continue
        if stripped.startswith(("#", "!")):
            if in_main and not written:
                out.extend(str(card) for card in main)
                written = True
            in_main = False
            out.append(line)
            continue
        if in_main:
            continue
        out.append(line)
    if in_main and not written:
        out.extend(str(card) for card in main)
    return "\n".join(out) + "\n"


def card_facts(cdb: Path, script_dir: Path, card_ids: Sequence[int]) -> Tuple[set, set]:
    """从卡库与脚本目录算出 ``(已知卡号, 有脚本的卡号)``。

    **脚本判定要跟着别名走**：异画/复刻卡的脚本挂在原卡号上（14558128 灰流丽异画的脚本
    就是 ``c14558127.lua``）。只看自己的卡号会把这类卡一律当白板，实测直接把一整副牌挡在
    门外（码丽丝(OCG) 就因一张异画被判"没有脚本"，工具秒退）。这与体检工具的口径一致。
    """

    connection = sqlite3.connect(f"file:{cdb.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, alias FROM datas WHERE id IN (%s)" % ",".join("?" * len(card_ids)),
            tuple(card_ids),
        ).fetchall()
    finally:
        connection.close()
    known = {int(row[0]) for row in rows}
    aliases = {int(row[0]): int(row[1] or 0) for row in rows}
    scripted: set = set()
    for card in known:
        if (script_dir / f"c{card}.lua").is_file():
            scripted.add(card)
            continue
        alias = aliases.get(card, 0)
        if alias and (script_dir / f"c{alias}.lua").is_file():
            scripted.add(card)
    return known, scripted


OPTIMIZED_GROUP = "__optimized__"
"""优化器产出的卡组放在这个分组下（不是 ``__builtin__``）。

用独立分组是为了别跟 WindBot 自带卡组混在一起：内置分组在列表里标「内置」、``/删卡组`` 拒绝删、
启动时的内置登记还会按 ``(分组, 脚本名)`` 去更新它——优化产物落在那里就会被当成 WindBot 自己的牌。
"""

EXTRA_DECK_TYPE_BITS = 0x40 | 0x2000 | 0x800000 | 0x4000000
"""额外卡组的类型位：融合 / 同调 / 超量 / 连接（与 WindBot 的 ``IsExtraCard`` 一致）。

**为什么换入池必须按它过滤**：WindBot 加载卡表时按**卡的类型**（不是 .ydk 的区段）统计主/额外，
额外超过 15 张就 ``Deck.Load`` 返回 null，进房时 ``Deck.Cards`` 抛空引用 ⇒ 机器人交不出卡组、
房间一直显示"不准备"（实测：优化器把「炎凤凰@火灵天星」这种连接怪兽换进了主卡组，整副牌就废了）。
"""


def swap_pool(cdb: Path, deck_ids: Sequence[int], *, limit: int = 300) -> List[int]:
    """换入候选池：与这副牌**同系列**（共享 setcode）、**能放主卡组**（非额外类）且不在卡组里的卡。

    用 setcode 而不是名字前缀：名字的写法千奇百怪（"码丽丝<兵卒>柴郡猫"），
    setcode 是内核里的系列编号，同系列一眼可查。

    额外类（融合/同调/超量/连接）要排除：主卡组换进一张额外怪兽，WindBot 会按类型统计成
    "额外 16 张"而拒绝加载整副牌（见 :data:`EXTRA_DECK_TYPE_BITS`）。
    """

    connection = sqlite3.connect(f"file:{cdb.as_posix()}?mode=ro", uri=True)
    try:
        codes = {
            int(row[0])
            for row in connection.execute(
                "SELECT setcode FROM datas WHERE id IN (%s)" % ",".join("?" * len(deck_ids)),
                tuple(deck_ids),
            )
        }
        pool: List[int] = []
        for code in sorted(codes):
            if code <= 0:
                continue
            rows = connection.execute(
                "SELECT id FROM datas WHERE setcode = ? AND (type & ?) = 0 ORDER BY id LIMIT ?",
                (code, EXTRA_DECK_TYPE_BITS, limit),
            )
            pool.extend(int(row[0]) for row in rows)
    finally:
        connection.close()
    return [card for card in dict.fromkeys(pool) if card not in set(deck_ids)]


def extra_section(lines: Sequence[str]) -> List[int]:
    """取 ``.ydk`` 的 ``#extra`` 区段卡号（用来核对整副牌的合法性）。"""

    cards: List[int] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#extra"):
            inside = True
            continue
        if stripped.startswith(("#", "!")):
            inside = False
            continue
        if inside and stripped.isdigit():
            cards.append(int(stripped))
    return cards


def deck_legality(
    main: Sequence[int], extra: Sequence[int], *, extra_cards: Sequence[int]
) -> List[str]:
    """按 **WindBot 的加载规则**核对整副牌，返回问题清单（空表示它能被加载）。

    规则取自 WindBot 的 ``Deck.Load``：主卡组按类型统计后不得超过 60 张、额外不得超过 15 张
    （``extra_cards`` 里是"额外类型的卡号"，由卡库给出）。越界时 WindBot 不报错，只是返回 null，
    然后在进房时抛空引用——表现出来就是"机器人不准备"，所以这里必须拦在保存之前。
    """

    extra_set = set(extra_cards)
    issues: List[str] = []
    # 主卡组里混进额外怪兽：WindBot 会把它算进额外，于是"额外超过 15 张"整副牌加载失败
    strays = sorted({card for card in main if card in extra_set})
    if strays:
        issues.append(f"主卡组里有额外卡组的卡（会被算进额外名额）：{strays[:6]}")
    main_count = len([card for card in main if card not in extra_set])
    extra_count = len([card for card in main if card in extra_set]) + len(extra)
    if main_count > 60:
        issues.append(f"主卡组 {main_count} 张（超过 WindBot 的上限 60）")
    if extra_count > 15:
        issues.append(f"额外卡组 {extra_count} 张（超过 WindBot 的上限 15）")
    return issues


def history_evidence(
    db: Path, deck_style: str, deck_ids: Sequence[int]
) -> Tuple[int, List[int]]:
    """从对局库里统计 ``(看了多少局, 这些局里从没被用过的卡号)``。

    "整局没被用过"是卡组搜索里最可靠的信号，但**要带上样本量**：只有 2 局的话"没用过"什么
    也说明不了，所以把局数一起返回，让调用方自己决定够不够（见 ``prune_for_dead_cards``）。

    ⚠ 2026-10-07 起 ``card_usage`` **只含我方**（`duel/recorder.py` 按座位分开记了）：
    以前它是双方混记的一张表，"对手打过的同一张卡"会被算成我们"用过"，信号因此偏保守
    （只会漏死牌、不会误杀）；现在读的就是观测方那一边。对手用到的卡另存一列
    （``card_usage_opponent``），复盘"对面到底交了什么"时用。
    """

    if not db.is_file():
        return 0, []
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT card_usage FROM duels WHERE (left_style = ? OR right_style = ?)"
            " ORDER BY created_at DESC LIMIT 200",
            (deck_style, deck_style),
        ).fetchall()
    except sqlite3.DatabaseError:
        return 0, []
    finally:
        connection.close()
    if not rows:
        return 0, []
    seen: set = set()
    for (payload,) in rows:
        try:
            seen |= {int(card) for card in (json.loads(payload or "{}") or {})}
        except (ValueError, TypeError):
            continue
    return len(rows), [card for card in dict.fromkeys(deck_ids) if card not in seen]


def played_cards(db: Path, *, limit: int = 200) -> Dict[int, int]:
    """最近若干局里**真的被打出来过**的卡 → 出场局数。

    用途：换入的候选要**优先**从这里挑。依据是实测的：把一张从没被打出来的牌换成另一张
    同样从没被打出来的牌，结果是"换了等于没换"——码丽丝那副牌被清掉的「灵王的波动」在
    4090 局里只出场 2 次，而换进来的 4 张 @火灵天星在 4090 局里出场 **0 次**：牌表动了，
    机器人打出来的东西一点没变，看起来就像"优化没做任何事"。会出场的牌至少让改动**可观测**，
    这也是后续"用胜率判定"能成立的前提。

    注意这是**偏好**不是硬条件：池子里没出现过的新牌仍然可选（老牌可能只是没被抽到过）。
    """

    if not db.is_file():
        return {}
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT card_usage FROM duels ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    finally:
        connection.close()
    counts: Dict[int, int] = {}
    for (payload,) in rows:
        try:
            cards = {int(card) for card in (json.loads(payload or "{}") or {})}
        except (ValueError, TypeError):
            continue
        for card in cards:
            counts[card] = counts.get(card, 0) + 1
    return counts


def extra_card_ids(cdb: Path) -> List[int]:
    """卡库里所有**额外卡组类型**（融合/同调/超量/连接）的卡号。

    只为了把"主卡组里混进了额外怪兽"查出来：WindBot 按卡类型统计主/额外，混进去一张就会
    让额外变成 16 张而拒绝加载整副牌（表现出来是机器人进房后"不准备"）。
    """

    connection = sqlite3.connect(f"file:{cdb.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, type FROM datas WHERE (type & ?) != 0", (EXTRA_DECK_TYPE_BITS,)
        ).fetchall()
    finally:
        connection.close()
    return sorted(int(row[0]) for row in rows)


def list_position(pool: DeckPool, group_id: str, deck_id: int) -> int:
    """卡组在 ``/卡组列表`` 里的位置号（1 起；找不到时返回 0）。

    群里的指令按**位置**解析（``plugin._deck_by_argument`` 取 ``decks[index-1]``），而
    ``StoredDeck.deck_id`` 是数据库自增 ID。池子里有内置卡组打底时两者并不相等，所以任何
    要报给用户的"编号"都必须换成位置号，不然照报的数字去敲指令会操作到另一副牌（实测踩过）。
    """

    for index, deck in enumerate(pool.list_decks(group_id), start=1):
        if deck.deck_id == deck_id:
            return index
    return 0


def group_of_deck(db: Path, deck_id: int) -> str:
    """查一副卡组属于哪个群：卡组池是**按群**列出的，不知道群就取不到它。"""

    if not db.is_file():
        return ""
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT group_id FROM decks WHERE deck_id = ?", (int(deck_id),)
        ).fetchone()
    except sqlite3.DatabaseError:
        return ""
    finally:
        connection.close()
    return str(row[0]) if row else ""


async def run(args: argparse.Namespace) -> int:
    """跑一轮搜索；返回退出码。"""

    cli = load_cli()
    from train.arena import DuelArena

    plugin_root = Path(args.plugin_root)
    # 复用的擂台 CLI 要那一套路径/规则参数名；这里没暴露它们，就补上**带类型**的默认值。
    # 注意别一律补空串：数值字段补成 "" 会让 make_arena_config 里 float + str 直接崩（实测）
    defaults: dict = {
        "ygopro_exe": "",
        "ygopro_dir": "",
        "windbot_exe": "",
        "windbot_dir": "",
        "cards_cdb": "",
        "plan_executable": "",
        "verbose_bots": False,
        "shuffle": False,
        "start_lp": 4000,
        "time_limit": 30,
        "max_duration": 120.0,
    }
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    paths = cli.resolve_paths(args)
    missing = cli.check_paths(paths)
    if missing:
        print("路径没配齐：" + "；".join(missing))
        return 2
    cdb = Path(paths["cards_cdb"]) if paths.get("cards_cdb") else Path(paths["ygopro_dir"]) / "cards.cdb"
    scripts = Path(paths["ygopro_dir"]) / "script"
    data_dir = Path(args.data_dir) if args.data_dir else plugin_data_dir(plugin_root)

    pool = DeckPool(data_dir, default_windbot_deck="Test")
    group_id = group_of_deck(data_dir / "deck_pool.db", args.deck_id)
    if not group_id:
        print(f"[错误] 卡组池里没有编号 {args.deck_id} 的卡组")
        return 2
    decks = {deck.deck_id: deck for deck in pool.list_decks(group_id)}
    subject = decks.get(args.deck_id)
    if subject is None:
        print(f"[错误] 卡组池里没有编号 {args.deck_id} 的卡组")
        return 2
    opponents = []
    for raw in args.opponents.split(","):
        item_id = raw.strip()
        if not item_id.isdigit():
            print(f"[错误] 对手要写卡组池编号，收到 {raw!r}")
            return 2
        # 每个对手按**它自己的群**去查：内置卡组归 __builtin__、投稿归各自群，
        # 用主角的群去查会找不到（"投稿牌打内置牌"是最常见的用法）
        deck_group = group_of_deck(data_dir / "deck_pool.db", int(item_id))
        if not deck_group:
            print(f"[错误] 找不到对手卡组编号 {item_id}")
            return 2
        found = next(
            (deck for deck in pool.list_decks(deck_group) if deck.deck_id == int(item_id)), None
        )
        if found is None:
            print(f"[错误] 找不到对手卡组编号 {item_id}")
            return 2
        opponents.append(found)
    print(f"优化对象：「{subject.display_name}」（{subject.windbot_deck}）")
    print(f"对手池：{'、'.join(item.display_name for item in opponents)}")

    main_ids, lines = read_ydk(subject.ydk_path)
    extra_ids = extra_section(lines)
    candidates_pool = swap_pool(cdb, main_ids)
    # 体检的"已知卡"必须**包含换入候选**：只查原卡表的话，换进来的新卡会被一律判成
    # "卡库里没有"，候选全被误杀（实测就是这么把两个候选都毙掉的）
    known, scripted = card_facts(cdb, scripts, list(main_ids) + list(candidates_pool))
    # 合法性单独查一次：WindBot 加载不了整副牌时不会报错，只会在进房时"不准备"，
    # 所以这种问题必须在开打之前就拦住（见 deck_legality）
    extra_set = set(extra_card_ids(cdb))
    legality = deck_legality(main_ids, extra_ids, extra_cards=sorted(extra_set))
    if legality:
        print("[错误] 这副牌 WindBot 加载不了，先解决它：" + "；".join(legality))
        return 2
    # 主体卡组的"缺脚本"只警告不拦：牌子是群友投的，几张白板不该让整副牌失去被优化的机会
    # （实测就因此秒退，群里只看到一句"没能跑完"）。候选卡表的同名检查仍然是硬性的。
    issues = deck_issues(
        main_ids,
        known_cards=sorted(known),
        scripted_cards=sorted(scripted),
        skip_script_check=True,
    )
    if issues:
        print("[错误] 这副牌本身就有问题，先解决它：" + "；".join(issues))
        return 2
    blanks = missing_scripts(main_ids, known_cards=sorted(known), scripted_cards=sorted(scripted))
    if blanks:
        print(
            f"[警告] 主体卡组里有 {len(blanks)} 种卡查不到脚本（在游戏里是白板，白占卡位）："
            f"{blanks[:6]}"
        )
    # 换入池只留"本机有脚本"的卡：脚本缺失的卡在游戏里是白板，换进去等于把卡位白送掉。
    # 在**生成候选之前**就筛掉，否则每个踩到白板的候选都要白跑一整批对局才被体检毙掉（实测）。
    usable_pool = [card for card in candidates_pool if card in scripted]
    dropped = len(candidates_pool) - len(usable_pool)
    print(
        f"卡表 {len(main_ids)} 张；同系列可换入 {len(usable_pool)} 张"
        + (f"（另有 {dropped} 张没有卡片脚本，已排除）" if dropped else "")
    )

    # 擂台配置提前构造一次：写错的话要在开打之前报出来，别等第一批局跑到一半才崩
    cli.make_arena_config(args, paths)
    opt_dir = plugin_root / "temp" / "opt"
    opt_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log) if args.log else opt_dir / f"opt-{args.deck_id}-{args.seed}.jsonl"
    # 优化的每一局都落进**训练用的同一个库**：一是"哪些卡整局没被用过"要靠它才有数据
    # （不落库的话 unused_from_history 永远是 0 张，换牌只能瞎挑），二是证据能跨轮累积
    # ——每次 /优化卡组 都从头再打一遍的话，那点样本量永远到不了能判定的程度。
    store = DuelStore(plugin_root / "temp" / "train" / "arena.db")

    async def evaluate(
        deck_file: Path, opponent, *, seed_base: int, label: str = "进度"
    ) -> Tuple[int, int]:
        """让 ``deck_file`` 与某个对手打一批镜像局，返回 ``(我方胜, 总局)``。

        **边打边报进度**：一批 100 局要跑好几分钟（WindBot 偶发错误会让单局拖到超时），
        如果只在打完后输出一行，外面看着就像卡死了——实测就是这么白等 37 分钟。
        """

        from train.arena import Fighter

        left = Fighter(name=subject.display_name, deck_file=deck_file, style=subject.windbot_deck)
        right = Fighter(name=opponent.display_name, deck_file=opponent.ydk_path, style=opponent.windbot_deck)
        config = cli.make_arena_config(args, paths)
        config.shuffle_seed_base = seed_base
        local_arena = DuelArena(config, logger=None)
        expected = args.rounds * 2
        tally = {"wins": 0, "total": 0, "undecided": 0}

        def on_outcome(outcome) -> None:
            """每局打完更新一次计数、落库，并按每 10 局打一行进度。"""

            tally["total"] += 1
            if outcome.winner == left.name:
                tally["wins"] += 1
            if not outcome.decided:
                tally["undecided"] += 1
            store.record(
                f"opt-{args.deck_id}-{label}", outcome, left_style=left.style, right_style=right.style
            )
            if tally["total"] % 10 == 0 or tally["total"] == expected:
                rate = tally["wins"] / tally["total"]
                print(
                    f"    [{label}] 已打 {tally['total']}/{expected} 局，"
                    f"当前 {tally['wins']}/{tally['total']} = {rate:.0%}",
                    flush=True,
                )

        outcomes = await local_arena.play_pair(
            left, right, rounds=args.rounds, on_outcome=on_outcome
        )
        wins = sum(1 for item in outcomes if item.winner == left.name)
        return wins, len(outcomes), tally["undecided"]

    try:
        baseline_wins = baseline_total = baseline_undecided = 0
        for index, opponent in enumerate(opponents):
            wins, total, undecided = await evaluate(
                subject.ydk_path,
                opponent,
                seed_base=args.seed + 900_000 + index * 10_000,
                label="基线",
            )
            baseline_wins += wins
            baseline_total += total
            baseline_undecided += undecided
        print(f"基线（原卡表）：{baseline_wins}/{baseline_total} = {baseline_wins / baseline_total:.1%}")
        if baseline_undecided:
            print(f"    （其中 {baseline_undecided} 局没打起来，基线数字偏保守）")

        # "哪些卡整局没被用过"要**在基线打完之后**统计：刚打完的这批已经落库，所以第一轮
        # 优化就有当轮数据（第一次跑不再退化成一堆 0），而历史轮次也一并算进来。
        seen_games, unused = history_evidence(store.path, subject.windbot_deck, main_ids)
        print(f"最近 {seen_games} 局里没被用过的卡：{len(unused)} 张")
        # 换入优先挑"最近对局里真的会被打出来"的牌：实测把死牌换成同样不会出场的牌，
        # 结果是牌表动了、机器人打出来的东西一点没变（见 played_cards 的说明）
        played = played_cards(store.path)
        preferred = [card for card in usable_pool if card in played]
        print(
            f"换入池里最近对局真的会出场的：{len(preferred)}/{len(usable_pool)} 张"
            "（换入优先从这些里挑）"
        )
        # 这里的随机数是**刻意可复现**的洗牌种子（同一个 --seed 给出同一批候选），
        # 不是安全用途，所以不用 secrets
        proposals = propose_swaps(
            main_ids,
            unused=unused,
            candidates=usable_pool,
            count=args.candidates,
            rng=random.Random(args.seed),
            swap_size=args.swap_size,
            preferred=preferred,
        )
        if not proposals:
            print("[错误] 生成不出候选（同系列可换入的卡太少了）")
            return 2

        accepted: List[Tuple[object, List[int], str]] = []
        for index, proposal in enumerate(proposals):
            candidate_deck = apply_swap(main_ids, proposal)
            candidate_issues = deck_issues(
                candidate_deck, known_cards=sorted(known), scripted_cards=sorted(scripted)
            )
            if candidate_issues:
                print(f"  候选 {index + 1} 不合格，跳过：{'；'.join(candidate_issues)}")
                continue
            candidate_file = opt_dir / f"cand-{args.deck_id}-{index}.ydk"
            candidate_file.write_text(ydk_text_with_main(lines, candidate_deck), encoding="utf-8")
            wins = total = undecided = 0
            for sub, opponent in enumerate(opponents):
                got, count, broke = await evaluate(
                    candidate_file,
                    opponent,
                    # 每个候选一套**不同**的牌序区间：共用牌序会让"赢家"只是背下了开局
                    seed_base=args.seed + 1_000 + index * 1000 + sub * 100,
                    label=f"候选{index + 1}",
                )
                wins += got
                total += count
                undecided += broke
            if total and undecided / total >= _BROKEN_SHARE_LIMIT:
                # bot 崩溃/超时的那几局既没有胜负也没打出来：把它们算成"候选输了"是在拿
                # 坏数据下结论（实测就有候选 0/4，其中一半是 Tick Error）。直接不比较。
                print(
                    f"  候选 {index + 1} 跳过比较：{total} 局里有 {undecided} 局没打起来"
                    f"（bot 报错或超时），这个数字不能用来判断强弱"
                )
                continue
            ok, reason = accept_candidate(baseline_wins, baseline_total, wins, total)
            print(f"  候选 {index + 1}：{proposal.describe()} → {wins}/{total}")
            if undecided:
                print(f"      （其中 {undecided} 局没打起来，数字偏保守）")
            print(f"      {reason}")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "deck_id": args.deck_id,
                            "proposal": proposal.describe(),
                            "remove": list(proposal.remove),
                            "add": list(proposal.add),
                            "wins": wins,
                            "total": total,
                            "baseline_wins": baseline_wins,
                            "baseline_total": baseline_total,
                            "accepted": ok,
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            if ok:
                accepted.append((proposal, candidate_deck, reason))

        if not accepted:
            # 区间判定什么都没通过时，退一步做**死牌清理**：这不是"证明变强了"，而是"把最近
            # N 局一次都没动过的卡换成同系列的牌"——直接观察，不需要几百局的样本量。
            # 样本量不够（比如只打了几局）就什么都不动，理由里也照实写。
            prune = prune_for_dead_cards(
                main_ids, unused, usable_pool, games=seen_games, preferred=preferred
            )
            if prune is None:
                print(
                    "\n这一轮没有候选通过判定，不动原卡组。\n"
                    f"（要判定 5% 的差别，每组约需 {rounds_needed(0.05)} 局；"
                    "可以加 --rounds 再跑一轮）"
                )
                return 1
            pruned = apply_swap(main_ids, prune)
            pruned_issues = deck_issues(
                pruned, known_cards=sorted(known), scripted_cards=sorted(scripted)
            )
            if pruned_issues:
                print(f"\n[错误] 死牌清理后的卡表没通过体检：{'；'.join(pruned_issues)}")
                return 2
            print(f"\n{prune.describe()}\n  {prune.reason}")
            if args.no_save:
                print("--no-save：只报告不新建卡组。")
                return 0
            accepted.append((prune, pruned, prune.reason))
        if args.no_save:
            print(f"\n有 {len(accepted)} 个候选通过，但 --no-save：只报告不新建卡组。")
            return 0

        proposal, best_deck, reason = accepted[-1]
        # 保存前最后核一次"WindBot 能不能加载这副牌"：它在加载失败时不报错，只会在进房时
        # 抛空引用、房间一直"不准备"（实测就这么交出过两副废牌：主卡组里混进了连接怪兽）
        final_issues = deck_legality(best_deck, extra_ids, extra_cards=extra_set)
        if final_issues:
            print(f"\n[错误] 这版卡表 WindBot 加载不了，**没有保存**：{'；'.join(final_issues)}")
            return 2
        display = f"{subject.display_name}·改{args.seed % 1000}"
        stored = pool.add(
            # 存进**自己的分组**，不要跟着源卡组的 `__builtin__`：那样会被当成 WindBot 自带卡组
            # （列表里标「内置」、/删卡组 拒绝删、启动时的内置登记还会去碰它）
            group_id=OPTIMIZED_GROUP,
            display_name=display,
            contributor_id="",
            contributor_name="自动优化",
            ydk_text=ydk_text_with_main(lines, best_deck),
            deck_code="",
            source_format="auto-optimize",
            main_count=len(best_deck),
            extra_count=0,
            side_count=0,
            windbot_deck=subject.windbot_deck,
        )
        # 报给群里的"编号"必须是**/卡组列表 里的位置号**：群里的指令都是按位置解析的
        # （见 plugin._deck_by_argument），而这里的 stored.deck_id 是数据库自增 ID——
        # 两者在池子里有内置卡组打底时并不相等（实测把"编号 83"报给了用户，而列表里它是 73，
        # 照报的数字去敲指令会操作到另一副牌）。
        position = list_position(pool, subject.group_id, stored.deck_id)
        print(
            f"\n采纳：{proposal.describe()}\n  {reason}\n"
            f"  已新建卡组「{display}」（/卡组列表 里的编号 {position or stored.deck_id}，"
            f"未覆盖原卡组）\n"
            f"  日志：{log_path}"
        )
        return 0
    finally:
        store.close()
        pool.close()


def main() -> int:
    """入口。"""

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
