"""给一副牌**实测**挑出牌脚本：现成执行器各打几局，按"会不会出牌 + 每局中位动作数"排名。

**为什么要有这个工具**：原先投稿卡组默认让模型现写一个 C# 执行器（要编译、会崩、可能写成空壳），
而 WindBot 自带的那批执行器是长期维护的代码。实测同一副牌：生成脚本一局动作 26 次甚至 0 次，
通用脚本却有 51 次——那还不如让**实测**告诉我们哪个现成执行器最合适。

排名口径（都在同一副牌上跑，只有"用哪个执行器"这一个变量）：

* 对手固定（``--opponents``，默认一副内置卡组），所以所有候选面对的是同一把尺子；
* 候选里**一定有"当前真正在用的那个脚本"**（``picked_style`` > ``generated_script`` > 映射到的脚本），
  哪怕它是生成的专属脚本、根本不在 WindBot 卡组目录里——不然"换个风格不能比现在更差"就没有基准；
* **先看"会不会出牌"**：平均每局动作数（召唤+发动+攻击）低于 3 次的一律判为不可用
  （实测有脚本编译通过但一张牌都不出）；
* 再按**中位动作数**排，并同时看"能动局占比"（动作数 ≥ 3 的局占比）；
* 均值与胜率只用来收尾：中位数差不到 1 次/局才算平手，平手时才比均值（"打得起来的时候能爆多高"），
  再平才比胜率。为什么不是均值优先：同一副牌同一个候选的**逐局动作数标准差能到 20 以上**
  （有的局 1 次、有的局 91 次），均值会被几局爆发拖走——实测「改922」上 ``Maliss`` 有 27% 的局
  只打 1~2 次，均值却靠几局 55~67 次冲到 24.4，和"每局基本都能打"的 ``MalissOCG`` 打平；
  按中位数看则是 20.0 对 8.0，一眼分明。12 局样本的胜率同样分不清 2/12 与 3/12，所以它排在最后；
* 牌里带了打法数据（``duel/playbook.py``）时，会**额外**跑一条"通用执行器 + 打法数据"的腿，
  这样"模型写的数据到底有没有用"是量出来的，不是我们说了算。

用法::

    python tools/pick_style.py --deck-id 86 --rounds 2
    python tools/pick_style.py --deck-file D:/decks/x.ydk --candidates 4 --no-save
    python tools/pick_style.py --deck-id 81 --styles Maliss,MalissOCG --rounds 15

退出码：0=挑到了并保存，1=没有可用候选，2=出错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.deckcode import parse_deck_code  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.deckpool import DeckPool, StoredDeck  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.windbot_decks import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    GENERIC_STYLE_NAME,
    load_available_decks,
    similarity,
)
from train.store import DuelStore  # noqa: E402  导入顺序受 sys.path 补丁影响

# 平均动作数低于这个值就判"打不出牌"（与擂台的「整局空过」阈值一致）
MIN_ACTIONS_PER_GAME = 3.0

# 中位动作数差不到这个值就算"平手"，此时才看均值与胜率。
# 只是防"差 0.1 次也算赢"，不是给其它指标更高的权重——理由见模块文档。
MEDIAN_TIE_BAND = 1.0

# 通用执行器：它继承 WindBot 的通用打法，并且是唯一会读"卡组打法数据"的那一个
GENERIC_PLAN_STYLE = "PlanAware"


def describe_match(
    main: Sequence[int],
    extra: Sequence[int],
    available: Dict[str, Tuple[Sequence[int], Sequence[int]]],
    style: str,
) -> str:
    """这个执行器"认识"这副牌吗（卡表重合度）。

    **这是"换脚本能不能解决问题"的前置判断**：实测「升辉月」与它最好的执行器只重合 0.06
    ——WindBot 那 72 个自带执行器里没有这个系列，换脚本已经到头了，该写打法数据或改卡表；
    而「码丽丝(OCG)·改7」与 ``MalissOCG`` 是 0.93，脚本本身还能继续调。
    目录里没有的执行器（通用 ``PlanAware``、生成的 ``GenXX``）不按卡表打分：它们本来就不靠卡表。
    """

    if style not in available:
        return f"{style}：不在 WindBot 卡组目录里（通用/生成的执行器，不按卡表打分）"
    score = similarity(main, extra, *available[style])
    if score < 0.2:
        hint = "（几乎不重合：执行器不认识这副牌，换脚本解决不了，该写打法数据或改卡表）"
    elif score < 0.6:
        hint = "（只认一半：卡表里有执行器不认识的轴）"
    else:
        hint = "（认得这副牌）"
    return f"{style} 与这副牌的卡表重合度 {score:.2f}{hint}"


def load_cli():
    """按文件路径加载擂台 CLI（复用它的路径解析与选手构造）。"""

    spec = importlib.util.spec_from_file_location(
        "train_arena_cli", str(_PLUGIN_ROOT / "tools" / "train_arena.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tool(name: str):
    """按文件路径加载同目录的工具脚本（tools 下的脚本不是包的一部分）。"""

    spec = importlib.util.spec_from_file_location(
        f"tool_{name}", str(_PLUGIN_ROOT / "tools" / f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def style_registered(style: str, paths: Dict[str, str]) -> Tuple[bool, str]:
    """这个脚本名在 ``windbot_exe`` 里真的注册过吗。

    判定直接复用 :func:`brain_eval.style_registered`（内置名查表、``GenXX`` 这类查执行器类），
    免得两处各判一套、日后漂移。**这事必须查**：WindBot 遇到没注册的 ``Deck=`` 名字不报错，
    它会随便挑一个 Normal 档执行器顶上，跑出来的数字与这个脚本毫无关系。
    """

    return load_tool("brain_eval").style_registered(
        style, Path(paths["windbot_exe"]), load_cli().WIND_BOT_DECK_NAMES
    )


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="实测挑出牌脚本：同一副牌，几个执行器各打几局")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument(
        "--deck-id", type=int, default=0, help="卡组在 /卡组列表 里的位置号（不是数据库编号）"
    )
    parser.add_argument("--deck-file", default="", help=".ydk 路径；与 --deck-id 二选一")
    parser.add_argument("--candidates", type=int, default=4, help="最多测几个候选执行器（按卡表相似度挑）")
    parser.add_argument(
        "--styles",
        default="",
        help="只测这几个执行器（逗号分隔），跳过自动挑候选；用于把两个候选打更多局再定论",
    )
    parser.add_argument(
        "--opponents", default="", help="对手，形如 脚本名 或 脚本=卡表路径；默认挑一副内置"
    )
    parser.add_argument("--rounds", type=int, default=2, help="每个候选跑几轮镜像（每轮 2 局）")
    parser.add_argument("--parallel", type=int, default=4, help="同时跑几局")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--no-save", action="store_true", help="只排名，不写回卡组库")
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


def rank_candidates(
    main: Sequence[int],
    extra: Sequence[int],
    available: Dict[str, Tuple[Sequence[int], Sequence[int]]],
    *,
    current_style: str = "",
    limit: int = 4,
) -> List[str]:
    """按卡表相似度挑出要实测的执行器名单（相似度最高的几个 + 通用执行器 + 当前风格）。

    **只按相似度挑候选、不按相似度定胜负**：相似度只是"少测几个"的省事办法，
    真正谁合适要靠实测（见模块文档）。所以一定要把通用执行器和当前风格也带上——
    前者是"什么都不会时最稳的那个"，后者是"换个风格至少不能比现在更差"的基准。

    ``current_style`` **不要求在 ``available`` 里**：生成的专属脚本（``Gen89`` 这类）编译进
    WindBot 的执行器表里，却不在卡组目录里，以前正是被这个条件筛掉了基准——
    实测「里除出大哥」时四个候选全打不出牌、工具报"不改动现状"，而它自己的 ``Gen89`` 其实能打。
    调用方负责确认这个名字真的注册过（见 :func:`style_registered_gate`），否则别传进来。
    """

    scored: List[Tuple[float, str]] = []
    for style, (deck_main, deck_extra) in available.items():
        if not deck_main and not deck_extra:
            continue
        scored.append((similarity(main, extra, deck_main, deck_extra), style))
    scored.sort(key=lambda item: (-item[0], item[1]))

    picked: List[str] = []
    for style in (GENERIC_PLAN_STYLE, current_style):
        if style and style not in picked:
            picked.append(style)
    for _score, style in scored:
        if len(picked) >= max(1, limit):
            break
        if style not in picked:
            picked.append(style)
    return picked


class ArmResult:
    """一个候选（或对照腿）的实测结果。"""

    def __init__(self, label: str, style: str, actions: Sequence[int], wins: int, total: int) -> None:
        self.label = label
        self.style = style
        self.actions = list(actions)
        self.wins = wins
        self.total = total

    @property
    def average_actions(self) -> float:
        """平均每局动作数（召唤+发动+攻击）。"""

        return sum(self.actions) / len(self.actions) if self.actions else 0.0

    @property
    def median_actions(self) -> float:
        """中位动作数：排在中间那一局打了多少（比均值抗几局爆发的干扰）。"""

        return statistics.median(self.actions) if self.actions else 0.0

    @property
    def alive_rate(self) -> float:
        """能动局占比：动作数达到 ``MIN_ACTIONS_PER_GAME`` 的局占多少。"""

        if not self.actions:
            return 0.0
        alive = sum(1 for value in self.actions if value >= MIN_ACTIONS_PER_GAME)
        return alive / len(self.actions)

    @property
    def win_rate(self) -> float:
        """胜率（0~1）。"""

        return self.wins / self.total if self.total else 0.0

    @property
    def usable(self) -> bool:
        """能不能用：**先看会不会出牌**，打不出牌的一律不可用。"""

        return bool(self.actions) and self.average_actions >= MIN_ACTIONS_PER_GAME

    def describe(self) -> str:
        """一行结论。"""

        detail = (
            f"中位 {self.median_actions:.1f}｜均值 {self.average_actions:.1f}"
            f"｜能动 {self.alive_rate:.0%}｜胜率 {self.wins}/{self.total} = {self.win_rate:.0%}"
        )
        return detail if self.usable else detail + "（打不出牌，不可用）"


def pick_best(results: Sequence[ArmResult]) -> Optional[ArmResult]:
    """在实测过的候选里选一个：**先看会不会出牌，再看每局中位动作数，均值和胜率只收尾**。

    为什么不是"均值优先"：逐局动作数的标准差能到 20 以上（同一候选有的局 1 次、有的局 91 次），
    均值会被几局爆发拖走。实测「改922」上 ``Maliss`` 有 27% 的局只打 1~2 次、均值 24.4，
    而每局基本都能打的 ``MalissOCG`` 均值 24.6——均值看着打平，中位数是 20.0 对 8.0。
    为什么胜率排最后：12 局样本分不清 2/12 与 3/12（95% 区间有 ±0.26 宽）。
    打不出牌的候选（``usable`` 为假）无条件出局，不管它均值多好看。
    """

    usable = [item for item in results if item.usable]
    if not usable:
        return None
    best_median = max(item.median_actions for item in usable)
    tied = [item for item in usable if item.median_actions >= best_median - MEDIAN_TIE_BAND]
    return max(tied, key=lambda item: (item.average_actions, item.win_rate, item.label))


async def run(args: argparse.Namespace) -> int:
    """跑一轮实测排名；返回退出码。"""

    cli = load_cli()
    from train.arena import DuelArena, Fighter

    plugin_root = Path(args.plugin_root)
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
    windbot_dir = Path(paths["windbot_dir"])

    # 1) 找准要测的那副牌
    data_dir = Path(args.data_dir) if args.data_dir else plugin_data_dir(plugin_root)
    pool: Optional[DeckPool] = None
    deck: Optional[StoredDeck] = None
    if args.deck_id:
        pool = DeckPool(data_dir, default_windbot_deck=GENERIC_STYLE_NAME)
        decks = pool.list_decks("")
        if not 1 <= args.deck_id <= len(decks):
            print(f"[错误] 卡组池里没有编号 {args.deck_id} 的卡组（共 {len(decks)} 副）")
            pool.close()
            return 2
        deck = decks[args.deck_id - 1]
        deck_path = Path(deck.ydk_path)
        deck_name = deck.display_name
    elif args.deck_file:
        deck_path = Path(args.deck_file)
        deck_name = deck_path.stem
    else:
        print("[错误] 要指定 --deck-id 或 --deck-file")
        return 2
    if not deck_path.is_file():
        print(f"[错误] 找不到卡表：{deck_path}")
        if pool is not None:
            pool.close()
        return 2

    # 2) 候选名单：相似度挑几个 + 通用执行器 + **当前真正在用的那个**
    parsed = parse_deck_code(deck_path.read_text(encoding="utf-8", errors="replace"))
    available = load_available_decks(windbot_dir) if windbot_dir.is_dir() else {}
    # 优先级必须与房间一致（见 plugin._with_current_style）：实测挑出来的 > 生成的专属脚本 > 映射到的脚本。
    # **漏掉 generated_script 是个真错**：那样"当前脚本"被当成通用脚本，实测里量不到它，
    # 工具还可能把一副牌从专属脚本换到更差的通用脚本上（升辉月那次候选里就没有 Gen88）。
    current = ""
    if deck is not None:
        current = deck.picked_style or deck.generated_script or deck.windbot_deck
    if current:
        registered, detail = style_registered(current, paths)
        if not registered:
            # 名字没注册时**不能**把它放进候选：那等于量一个随机执行器、却挂着这个脚本的名。
            # 但必须说出来——房间那边同样会退化成随机执行器，这副牌现在打出来的样子是假的。
            print(f"[警告] 当前脚本 {current} 不能当基准：{detail}")
            current = ""
    if args.styles:
        # 指定候选：把两个候选打更多局再定论时用（初筛 12 局的方差足以让同一个候选差 8 次动作/局）。
        # 点名要测的同样要过注册校验——不查的话量到的是 WindBot 随手顶上的那个执行器。
        candidates = []
        for style in (item.strip() for item in args.styles.split(",")):
            if not style:
                continue
            registered, detail = style_registered(style, paths)
            if not registered:
                print(f"[警告] 跳过 {style}：{detail}")
                continue
            candidates.append(style)
        print("（--styles 指定候选，已跳过自动挑候选）")
    else:
        candidates = rank_candidates(
            parsed.main, parsed.extra, available, current_style=current, limit=args.candidates
        )
    if not candidates:
        print("[错误] 一个候选执行器都没有（WindBot 目录里没找到自带卡组？）")
        if pool is not None:
            pool.close()
        return 2

    # 3) 对手：默认挑一副内置卡组（所有候选面对同一把尺子）
    opponent_style, opponent_deck_path = resolve_opponent(args.opponents, available, windbot_dir)
    if opponent_style is None:
        print("[错误] 没找到可用的对手，用 --opponents 脚本名[=卡表路径] 指定一个")
        if pool is not None:
            pool.close()
        return 2
    print(f"要测的牌：{deck_name}（{deck_path.name}）")
    print(f"对手：{opponent_style}（{opponent_deck_path.name}）｜每个候选 {args.rounds * 2} 局")
    print(f"候选：{'、'.join(candidates)}")
    # 换脚本能不能解决问题，先看"执行器认识这副牌吗"（不认识就只能改卡表/写打法数据）
    if current:
        print("当前：" + describe_match(parsed.main, parsed.extra, available, current))
    if deck is not None and deck.playbook:
        print("这副牌带打法数据：会额外跑一条「通用执行器 + 打法数据」的腿做对照")

    # 4) 一个候选一个候选地打（每局都落库，便于事后核对）
    store = DuelStore(plugin_root / "temp" / "train" / "arena.db")
    config = cli.make_arena_config(args, paths)
    results: List[ArmResult] = []
    try:
        arms: List[Tuple[str, str, str]] = [(style, style, "") for style in candidates]
        if deck is not None and deck.playbook and GENERIC_PLAN_STYLE in candidates:
            # 对照腿：同一个通用执行器，只多了一份打法数据
            arms.append((f"{GENERIC_PLAN_STYLE}+打法数据", GENERIC_PLAN_STYLE, deck.playbook))
        for label, style, playbook in arms:
            subject = Fighter(name=label, deck_file=deck_path, style=style, playbook=playbook)
            opponent = Fighter(
                name=f"对手({opponent_style})", deck_file=opponent_deck_path, style=opponent_style
            )
            outcomes = await DuelArena(config, logger=None).play_pair(
                subject,
                opponent,
                rounds=args.rounds,
                on_outcome=lambda outcome, tag=label: store.record(
                    f"pick-{tag}", outcome, left_style=tag, right_style=opponent_style
                ),
            )
            actions = [
                item.actions_left if item.left == label else item.actions_right for item in outcomes
            ]
            wins = sum(1 for item in outcomes if item.winner == label)
            result = ArmResult(label, style, actions, wins, len(outcomes))
            results.append(result)
            print(f"  {label:<18} {result.describe()}")
        winner = pick_best(results)
        if winner is None:
            print("\n[结果] 没有一个候选打得出牌，不改动现状。")
            return 1
        print(f"\n[结果] 最佳：{winner.label}（{winner.describe()}）")
        print("（口径：中位动作数优先，差不到 1 次/局才算平手、平手时才比均值与胜率）")
        print("（选中的：" + describe_match(parsed.main, parsed.extra, available, winner.style) + "）")
        print(
            f"（样本量：每个候选 {args.rounds * 2} 局，只够初筛——"
            "要拿它当结论就加 --rounds 多跑几轮）"
        )
        if args.no_save:
            print("--no-save：只排名，没有写回卡组库。")
            return 0
        if deck is None:
            print("（用 --deck-file 时没有卡组记录可写，只排名）")
            return 0
        pool.set_picked_style(deck.deck_id, winner.style)
        print(f"已写回卡组库：「{deck.display_name}」之后用「{winner.style}」出牌")
        return 0
    finally:
        store.close()
        if pool is not None:
            pool.close()


def resolve_opponent(
    spec: str, available: Dict[str, Tuple[Sequence[int], Sequence[int]]], windbot_dir: Path
) -> Tuple[Optional[str], Optional[Path]]:
    """解析对手：``脚本名`` 或 ``脚本名=卡表路径``；留空时挑一副内置卡组。

    内置卡组的卡表按 WindBot 的存放规则找（``Decks/AI_<名>.ydk``，名字里的短横线去掉），
    找不到就明确报错——擂台上"卡表找不到"会让那一侧一步不走，看起来像"脚本不行"。
    """

    text = spec.strip()
    if text:
        style, _, deck_path = text.partition("=")
        style = style.strip()
        path = Path(deck_path.strip()) if deck_path.strip() else default_deck_file(style, windbot_dir)
        if not style or not path.is_file():
            return None, None
        return style, path
    for style in sorted(available):
        cards_main, cards_extra = available[style]
        if not cards_main and not cards_extra:
            continue
        path = default_deck_file(style, windbot_dir)
        if path.is_file():
            return style, path
    return None, None


def default_deck_file(style: str, windbot_dir: Path) -> Path:
    """内置卡组的卡表路径（WindBot 的 ``Decks/AI_<名>.ydk`` 约定）。"""

    return Path(windbot_dir) / "Decks" / f"AI_{style.replace('-', '')}.ydk"


def main() -> int:
    """入口。"""

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
