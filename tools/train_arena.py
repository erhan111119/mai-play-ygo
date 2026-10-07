"""擂台命令行：跑基准线或指定对战组合，并把胜率表打出来。

**阶段 0 的用途**：先把「尺子」立起来——内置出牌脚本互相打一遍，测出各副牌/各脚本的
基准胜率。以后不管是训练出来的参数、模型改写的执行器，还是"AI 定战术"的新打法，
都拿这张表来对比，才知道有没有真的变强。

用法::

    # 基准线：内置卡组里挑 8 副做巡回赛（每对镜像 2 局），全程落库
    python tools/train_arena.py --arena baseline --decks 8 --parallel 6

    # 只打指定的几副（名字按 Decks/AI_*.ydk 的文件名，不带前缀与扩展名）
    python tools/train_arena.py --arena spot --pairs Albaz,Kashtira Blue-Eyes,Maliss

    # 看已有结果
    python tools/train_arena.py --report-only

引擎与 WindBot 路径默认从插件 config.toml 读；训练建议把 ``--ygopro-dir`` 指向引擎副本，
别和线上对局抢同一份目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import argparse
import asyncio
import itertools
import logging
import sys
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.windbot_decks import WIND_BOT_DECK_NAMES  # noqa: E402  导入顺序受 sys.path 补丁影响
from train.arena import ArenaConfig, DuelArena, DuelOutcome, Fighter  # noqa: E402
from train.coach import LlmCoach, make_coach  # noqa: E402
from train.llm import ModelClient, ModelError, pick_model  # noqa: E402
from train.store import DuelStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="训练擂台：批量跑对局并统计胜率")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--ygopro-exe", default="", help="ygopro.exe；默认读 config.toml")
    parser.add_argument("--ygopro-dir", default="", help="ygopro 工作目录；训练建议用副本")
    parser.add_argument("--windbot-exe", default="", help="WindBot.exe；默认读 config.toml")
    parser.add_argument("--windbot-dir", default="", help="WindBot 工作目录；默认读 config.toml")
    parser.add_argument("--cards-cdb", default="", help="cards.cdb；默认取 ygopro 目录下的")
    parser.add_argument("--arena", default="baseline", help="擂台名（按批次查结果用）")
    parser.add_argument("--db", default="", help="结果库路径；默认 <插件>/temp/train/arena.db")
    parser.add_argument("--decks", type=int, default=0, help="基准线：从内置卡组里挑几副（0 表示不跑巡回赛）")
    parser.add_argument(
        "--deck-names",
        default="",
        help="指定基准线用哪几副，逗号分隔（AI_ 前缀与 .ydk 后缀可省略）；留空则按出场顺序取前 N 副",
    )
    parser.add_argument(
        "--pairs",
        nargs="*",
        default=[],
        help="指定对战组合，形如 Blue-Eyes,Kashtira（两边都用各自卡组的出牌脚本）",
    )
    parser.add_argument("--parallel", type=int, default=6, help="同时跑几局")
    parser.add_argument("--rounds", type=int, default=1, help="每对跑几轮镜像（每轮 2 局）")
    parser.add_argument("--start-lp", type=int, default=4000, help="训练用初始生命值（调低能缩短单局）")
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="允许洗切（默认关闭：牌序固定，镜像配对才能成对比较）",
    )
    parser.add_argument("--time-limit", type=int, default=30, help="单回合时限（秒）")
    parser.add_argument("--max-duration", type=float, default=180.0, help="单局硬上限（秒）")
    parser.add_argument("--report-only", action="store_true", help="只打印已有库里的结果，不跑对局")
    parser.add_argument(
        "--silent-threshold", type=int, default=3, help="动作数低于多少算「整局空过」（报告里单列）"
    )
    parser.add_argument("--list-decks", action="store_true", help="列出可用的内置卡组后退出")
    parser.add_argument(
        "--ab-plan",
        default="",
        help="A/B 实验：同一副牌，一侧用计划感知执行器（PlanAware + 教练），另一侧用 --control-style",
    )
    parser.add_argument(
        "--control-style",
        default="Test",
        help="A/B 里对照组的出牌脚本名（默认 Test，即 WindBot 自带的通用脚本）",
    )
    parser.add_argument(
        "--plan-executable",
        default="",
        help=(
            "计划感知执行器所在的 WindBot.exe（A/B 的试验组用它）；"
            "留空则用 <windbot_src_dir>/bin/PlanAware/WindBot.exe"
        ),
    )
    parser.add_argument(
        "--pair-styles",
        default="",
        help=(
            "用 --ab-plan 指定的那副牌，让两个出厂脚本对打，形如 Test,Blue-Eyes。"
            "用来给脚本排名：先测「通用脚本 vs 原型脚本」，才知道 A/B 里那点差距是战术层"
            "带来的，还是脚本本身就有强弱"
        ),
    )
    parser.add_argument(
        "--verbose-bots",
        action="store_true",
        help="把两个 bot 自己的输出（含逐回合决策）打进日志；排查「整局没动作」时必须开",
    )
    parser.add_argument(
        "--coach",
        default="rule",
        help="教练：rule（规则，默认）/ llm（模型）/ none（不下计划）",
    )
    parser.add_argument(
        "--llm-task",
        default="utils",
        help="用宿主 model_config.toml 里哪个任务的模型（默认 utils，便宜且够用）",
    )
    parser.add_argument("--llm-model", default="", help="指定模型别名；留空按 --llm-task 挑")
    parser.add_argument(
        "--llm-allow-private",
        action="store_true",
        help="允许模型服务在内网/本机（自建推理服务时用）",
    )
    return parser.parse_args()


def load_config_paths(plugin_root: Path) -> Dict[str, str]:
    """从插件 config.toml 里读出路径，省得每次手敲。"""

    import tomllib

    config_path = plugin_root / "config.toml"
    if not config_path.is_file():
        return {}
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    paths = data.get("paths") or {}
    return {key: str(value) for key, value in paths.items()}


def deck_file(windbot_dir: Path, name: str) -> Path:
    """把 ``Blue-Eyes`` 这样的脚本名/卡组名转成 ``Decks/AI_*.ydk`` 路径。"""

    stem = WIND_BOT_DECK_NAMES.get(name, name)
    return Path(windbot_dir) / "Decks" / f"{stem}.ydk"


def resolve_deck_source(deck: str, windbot_dir: Path) -> Path:
    """把 ``--deck`` 的取值解析成卡表路径：内置卡组名，或任意 ``.ydk`` 路径。

    支持路径是为了投稿卡组：它们存在插件数据目录里（``data/plugins/<id>/decks/<群>/<uuid>.ydk``），
    不叫 ``AI_*.ydk``、也不在 WindBot 目录下，只能用路径指。
    """

    candidate = Path(deck)
    if deck.lower().endswith(".ydk") or candidate.is_file():
        return candidate
    return deck_file(windbot_dir, deck)


def all_builtin_decks(windbot_dir: Path) -> List[Tuple[str, Path]]:
    """列出本机可用的内置卡组：``[(脚本名, .ydk 路径)]``，按脚本名排序。"""

    decks_dir = Path(windbot_dir) / "Decks"
    result: List[Tuple[str, Path]] = []
    for style, stem in sorted(WIND_BOT_DECK_NAMES.items()):
        path = decks_dir / f"{stem}.ydk"
        if path.is_file():
            result.append((style, path))
    return result


def make_fighter(style: str, path: Path) -> Fighter:
    """造一个擂台选手；名字就用脚本名，报告里一眼能对上。"""

    return Fighter(name=style, deck_file=path, style=style)


def resolve_paths(args: argparse.Namespace) -> Dict[str, str]:
    """把命令行与 config.toml 合并成最终路径。

    **可执行文件的取舍**：源码树编译过（``bin/Release/WindBot.exe`` 存在）就用它，而不是配置里
    那份原版 exe——出牌脚本是编译进 exe 的，原版里**没有**为投稿卡组生成的专属脚本
    （``Deck=Gen86`` 这种）。名字找不到时 WindBot 不报错，它会静默换一个随机执行器，遇到不匹配
    的卡表就整局一步不走 ⇒ 测出来的是"随机脚本"而不是这副牌的打法（实测踩过）。
    编译产物是原版的超集（同一份源码树编出来的），所以统一用它既安全又和线上房间一致。
    """

    from_config = load_config_paths(Path(args.plugin_root))
    resolved = {
        "ygopro_exe": args.ygopro_exe or from_config.get("ygopro_executable", ""),
        "ygopro_dir": args.ygopro_dir or from_config.get("ygopro_dir", ""),
        "windbot_exe": args.windbot_exe or from_config.get("windbot_executable", ""),
        "windbot_dir": args.windbot_dir or from_config.get("windbot_dir", ""),
        "windbot_src_dir": from_config.get("windbot_src_dir", ""),
        "cards_cdb": args.cards_cdb or from_config.get("cards_cdb", ""),
        "plan_exe": args.plan_executable,
    }
    if not args.windbot_exe:
        src_dir = from_config.get("windbot_src_dir", "")
        built = Path(src_dir) / "bin" / "Release" / "WindBot.exe" if src_dir else None
        if built is not None and built.is_file():
            resolved["windbot_exe"] = str(built)
    if not resolved["cards_cdb"] and resolved["ygopro_dir"]:
        resolved["cards_cdb"] = str(Path(resolved["ygopro_dir"]) / "cards.cdb")
    if not resolved["plan_exe"]:
        # 计划感知执行器是和主干一起编译到 bin/PlanAware 的（见 windbot-src 的编译脚本）
        src_dir = from_config.get("windbot_src_dir", "")
        if src_dir:
            resolved["plan_exe"] = str(Path(src_dir) / "bin" / "PlanAware" / "WindBot.exe")
    return resolved


def check_paths(paths: Dict[str, str]) -> List[str]:
    """检查关键路径，返回缺失项。"""

    missing: List[str] = []
    for label, key in (
        ("ygopro.exe", "ygopro_exe"),
        ("ygopro 工作目录", "ygopro_dir"),
        ("WindBot.exe", "windbot_exe"),
        ("WindBot 工作目录", "windbot_dir"),
    ):
        value = paths.get(key, "")
        if not value:
            missing.append(f"{label}（未配置）")
        elif not Path(value).exists():
            missing.append(f"{label}（不存在：{value}）")
    return missing


def build_coach(args: argparse.Namespace):
    """按参数造教练；``llm`` 模式先把模型客户端建起来（读宿主模型配置）。"""

    if args.coach.strip().lower() not in ("llm", "ai", "模型"):
        return make_coach(args.coach)
    try:
        target = pick_model(task=args.llm_task, name=args.llm_model)
    except ModelError as exc:
        print(f"[错误] 读不到模型配置：{exc}")
        raise SystemExit(2) from exc
    client = ModelClient(target, allow_private_host=args.llm_allow_private)
    print(f"模型教练：{target.provider} / {target.model}")
    return make_coach("llm", llm=client)


def build_ab_fighters(
    deck: str,
    control_style: str,
    windbot_dir: Path,
    treatment_exe: Path,
    *,
    treatment_style: str = "PlanAware",
) -> Tuple[Fighter, Fighter]:
    """A/B 实验的两个选手：同一副牌，一侧用待评估的脚本、一侧对照。

    两边都用同一份卡表副本（``Decks/AI_PlanAware.ydk``），所以唯一的差别就是出牌脚本 ——
    这才是干净的对照。

    试验组必须用**真的注册了这个脚本的那份 exe**：出牌脚本是编译进 exe 的，
    WindBot 找不到 ``Deck=`` 请求的名字时不会报错，而是随机挑一个 Normal 档执行器顶上，
    计划文件/生成的脚本于是没人用，实验会安静地测成"随机脚本互打"。所以缺文件就直接报错。

    Args:
        deck: 卡组名（``--list-decks`` 里的名字）。
        control_style: 对照组的脚本名（例如 ``Test``）。
        windbot_dir: WindBot 工作目录（卡表放在它的 ``Decks/`` 下）。
        treatment_exe: 试验组用的 WindBot.exe（含待评估脚本的那份）。
        treatment_style: 试验组的脚本名；默认 ``PlanAware``（计划感知执行器）。
    """

    decks_dir = Path(windbot_dir) / "Decks"
    source = resolve_deck_source(deck, windbot_dir)
    if not source.is_file():
        raise FileNotFoundError(f"找不到卡表：{source}（--ab-plan 传卡组名或 .ydk 路径，见 --list-decks）")
    if not treatment_exe.is_file():
        raise FileNotFoundError(
            f"找不到试验组要用的 WindBot.exe：{treatment_exe}\n"
            f"脚本 {treatment_style} 必须真的编译进这一份里，否则 WindBot 会随机挑别的执行器顶上，"
            "整批对局的结果都不能用来下结论（用 --plan-executable 指定，或先编译 windbot-src）。"
        )
    target = decks_dir / "AI_PlanAware.ydk"
    target.write_bytes(source.read_bytes())
    treatment = Fighter(
        name=treatment_style, deck_file=target, style=treatment_style, executable=treatment_exe
    )
    control = Fighter(name=f"对照({control_style})", deck_file=target, style=control_style)
    return treatment, control


def build_pair_styles(deck: str, styles: str, windbot_dir: Path) -> Tuple[Fighter, Fighter]:
    """同一副牌、两个出厂脚本对打：给脚本排名用（两边都用配置里那个 exe）。

    为什么要专门测这个：A/B 里"实验组比对照组强/弱"有两种可能原因 —— 战术层起了作用，
    或者单纯是"通用脚本本来就比原型脚本强"。不先把脚本本身排个序，就分不清是哪一种。
    """

    names = [item.strip() for item in styles.split(",") if item.strip()]
    if len(names) != 2:
        raise ValueError(f"--pair-styles 要写两个脚本名，形如 Test,Blue-Eyes；收到：{styles!r}")
    source = deck_file(Path(windbot_dir), deck)
    if not source.is_file():
        raise FileNotFoundError(f"找不到卡表：{source}（--ab-plan 传卡组名，见 --list-decks）")
    target = Path(windbot_dir) / "Decks" / "AI_MirrorDeck.ydk"
    target.write_bytes(source.read_bytes())
    left = Fighter(name=names[0], deck_file=target, style=names[0])
    right = Fighter(name=names[1], deck_file=target, style=names[1])
    return left, right


def select_matchups(
    args: argparse.Namespace, windbot_dir: Path, plan_executable: Path
) -> List[Tuple[Fighter, Fighter]]:
    """按参数决定这一批要打哪些组合。

    抽成函数是为了能测：``--pair-styles`` 与 ``--ab-plan`` 都得用到卡组名参数，
    所以两者的判断顺序有讲究——顺序写反会让脚本排名那一批**安静地**变成 A/B 实验，
    跑完还看不出哪里不对（实测踩过一次）。
    """

    if args.pair_styles:
        return [build_pair_styles(args.ab_plan, args.pair_styles, windbot_dir)]
    if args.ab_plan:
        return [build_ab_fighters(args.ab_plan, args.control_style, windbot_dir, plan_executable)]
    return build_matchups(args, windbot_dir)


def build_matchups(args: argparse.Namespace, windbot_dir: Path) -> List[Tuple[Fighter, Fighter]]:
    """按参数算出要打的组合。

    * ``--pairs A,B``：打指定的这一组（可给多组，按传入顺序两两成对）；
    * ``--deck-names X,Y,Z`` / ``--decks N``：这些牌之间做巡回赛（任意两副都打）。
    """

    if args.pairs:
        fighters: List[Fighter] = []
        for pair in args.pairs:
            names = [item.strip() for item in pair.split(",") if item.strip()]
            if len(names) != 2:
                print(f"[警告] --pairs 的每一项要写成 A,B 两副：{pair}")
                continue
            resolved: List[Fighter] = []
            for name in names:
                # 支持三种写法：
                #   Blue-Eyes                 内置卡组名（脚本名同名）
                #   D:/.../x.ydk              只看卡表，脚本用通用脚本 Test
                #   Gen80=D:/.../x.ydk        卡表 + 指定出牌脚本（投稿卡组的"实际打法"走这条）
                #
                # 为什么必须能指定脚本：投稿卡组存在插件数据目录里，WindBot 的 Deck= 只认
                # 编译进 exe 的注册名；拿路径去当脚本名会静默落进"随机执行器"，训练出来的
                # 结果与这副牌实际怎么打毫无关系。
                style = ""
                deck_ref = name
                if "=" in name:
                    style, _, deck_ref = name.partition("=")
                    style = style.strip()
                    deck_ref = deck_ref.strip()
                path = resolve_deck_source(deck_ref, windbot_dir)
                if not path.is_file():
                    print(f"[警告] 找不到卡表：{path}")
                    break
                if not style:
                    style = deck_ref if deck_ref in WIND_BOT_DECK_NAMES else (
                        WIND_BOT_DECK_NAMES.get(deck_ref) or "Test"
                    )
                # 展示名用文件名（路径太长了，报告里读不出来）
                resolved.append(Fighter(name=path.stem or style, deck_file=path, style=style))
            else:
                fighters.extend(resolved)
        return [(fighters[index], fighters[index + 1]) for index in range(0, len(fighters) - 1, 2)]

    available = all_builtin_decks(windbot_dir)
    by_style = {style: path for style, path in available}
    picked: List[Fighter] = []
    if args.deck_names:
        for name in (item.strip() for item in args.deck_names.split(",")):
            if not name:
                continue
            style = name if name in by_style else WIND_BOT_DECK_NAMES.get(name, name)
            path = by_style.get(style)
            if path is None:
                print(f"[警告] 找不到卡组：{name}（可用名字见 --list-decks）")
                continue
            picked.append(make_fighter(style, path))
    elif args.decks > 0:
        picked = [make_fighter(style, path) for style, path in available[: args.decks]]

    return list(itertools.combinations(picked, 2))


def warn_if_lopsided(left: Fighter, right: Fighter, outcomes: Sequence[DuelOutcome]) -> None:
    """一组结果「一边全赢」时提醒：这多半不是强弱，而是有一边根本没打起来。

    实测教训：某副牌的脚本引用了公共库里缺失的辅助对象，卡一加载就报 Lua 错误，
    整批 40 局一边全胜、还有 7 局整局空过——看着像"强弱悬殊"，其实是"一边装不上牌"。
    这条提醒让人在采信数字之前先看一眼。
    """

    decided = [item for item in outcomes if item.decided]
    if len(decided) < 8:
        return
    left_wins = sum(1 for item in decided if item.winner == left.name)
    right_wins = len(decided) - left_wins
    if left_wins and right_wins:
        return
    winner, loser = (left, right) if left_wins else (right, left)
    silent = sum(1 for item in outcomes if item.actions_left < 3 or item.actions_right < 3)
    print(
        f"  [警告] 这 {len(decided)} 局全是「{winner.name}」赢，另一边一局没赢——"
        f"这通常不是强弱，而是「{loser.name}」根本没打起来："
        "出牌脚本名没注册（会被随机执行器顶替）/ 卡表读不出来 / 卡库里缺卡都会这样。"
        f"（整局空过 {silent} 局；要定位原因加 --verbose-bots 看 bot 自己的日志）"
    )


def make_arena_config(args: argparse.Namespace, paths: Dict[str, str], *, coach=None) -> ArenaConfig:
    """按命令行与路径造擂台配置（其它工具也复用，省得各写一份）。"""

    return ArenaConfig(
        ygopro_executable=Path(paths["ygopro_exe"]),
        ygopro_dir=Path(paths["ygopro_dir"]),
        windbot_executable=Path(paths["windbot_exe"]),
        windbot_dir=Path(paths["windbot_dir"]),
        cards_cdb=Path(paths["cards_cdb"]) if paths.get("cards_cdb") else None,
        parallel=args.parallel,
        start_lp=args.start_lp,
        no_shuffle_deck=not args.shuffle,
        time_limit=args.time_limit,
        max_duration=args.max_duration,
        coach=coach,
        plan_executable=Path(paths["plan_exe"]) if paths.get("plan_exe") else None,
        verbose_bots=args.verbose_bots,
    )


async def run_matchups(
    args: argparse.Namespace,
    matchups: Sequence[Tuple[Fighter, Fighter]],
    paths: Dict[str, str],
    store: DuelStore,
    logger: logging.Logger,
    coach=None,
) -> None:
    """把组合按顺序跑完，边跑边落库。"""

    config = make_arena_config(args, paths, coach=coach)
    arena = DuelArena(config, logger=logger)

    total_pairs = len(matchups)
    total_duels = total_pairs * args.rounds * 2
    print(f"共 {total_pairs} 组对手、{total_duels} 局；并发 {args.parallel}")
    started = time.monotonic()
    done = 0

    def record_one(left: Fighter, right: Fighter, outcome) -> None:
        """每局打完立刻落库并打印（不等整批跑完）。

        400 局一批要跑十几分钟，等全部跑完再输出的话，中途既看不到进度，
        也分不清"慢"和"卡死"——实测在这上面白等过二十分钟。
        """

        nonlocal done
        store.record(args.arena, outcome, left_style=left.style, right_style=right.style)
        done += 1
        mark = "✓" if outcome.decided else "…"
        detail = outcome.error or f"{outcome.turns} 回合 / {outcome.duration_seconds}s"
        print(
            f"  [{done}/{total_duels}] {mark} {left.name} vs {right.name}"
            f" → {outcome.winner}（第 {outcome.round_index} 轮，{detail}）"
        )
        if args.coach != "none" and outcome.plans:
            # 把教练下过的计划打出来：管线有没有通，看这里最直接
            print("     教练记录：" + outcome.plans.replace(chr(10), chr(10) + "     "))

    for index, (left, right) in enumerate(matchups, start=1):
        outcomes = await arena.play_pair(
            left,
            right,
            rounds=args.rounds,
            on_outcome=lambda outcome, left=left, right=right: record_one(left, right, outcome),
            # 只有"同一副牌、比两个脚本"才共用卡表：`--pairs A,B` 是**两副牌对战**，
            # 共用会让右边那方去打左边的牌（执行器不认识 ⇒ 一步不走、一路空过）
            shared_deck=bool(args.pair_styles),
        )
        assert len(outcomes) == args.rounds * 2, (len(outcomes), args.rounds)
        elapsed = time.monotonic() - started
        print(f"  第 {index}/{total_pairs} 组完成，用时 {elapsed:.0f}s")
        warn_if_lopsided(left, right, outcomes)

    if isinstance(coach, LlmCoach):
        # 模型教练抽风时会退回规则教练，那些回合测的其实是兜底逻辑；
        # 失败率不低的话这次 A/B 的结论就不能算在模型头上，所以必须打出来
        rate = coach.failures / coach.calls if coach.calls else 0.0
        print(
            f"\n模型教练调用：{coach.calls} 次，失败 {coach.failures} 次"
            f"（{rate:.0%}，失败即改用规则教练）"
        )


def print_report(store: DuelStore, arena: str, silent_threshold: int) -> None:
    """打印胜率表与「整局空过」名单。"""

    total = store.count(arena)
    print(f"\n===== 擂台 {arena}：{total} 局 =====")
    rows = store.win_rates(arena)
    if not rows:
        print("（还没有结果）")
        return
    print(f"{'脚本':<24}{'胜':>5}{'总':>6}{'胜率':>9}")
    for name, wins, total_games, rate in rows:
        print(f"{name:<24}{wins:>5}{total_games:>6}{rate:>8.1%}")

    silent = store.silent_scripts(arena, threshold=silent_threshold)
    if silent:
        print(f"\n动作数 < {silent_threshold} 的「整局空过」记录（这些局的胜负没有参考价值）：")
        for name, count in silent:
            print(f"  {name}: {count} 局")


def main() -> int:
    """入口。"""

    # 训练要跑几分钟到几小时，输出重定向到文件时默认是块缓冲，看不到进度，
    # 所以这里强制行缓冲
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = parse_args()
    # bot 自己的输出是 info 级；排查「整局没动作」时只有它能说明原因
    logging.basicConfig(
        level=logging.DEBUG if args.verbose_bots else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    paths = resolve_paths(args)
    windbot_dir = Path(paths["windbot_dir"]) if paths.get("windbot_dir") else Path(".")

    if args.list_decks:
        for style, path in all_builtin_decks(windbot_dir):
            print(f"{style:<24}{path}")
        return 0

    db_path = Path(args.db) if args.db else _PLUGIN_ROOT / "temp" / "train" / "arena.db"
    store = DuelStore(db_path)
    try:
        if args.report_only:
            print_report(store, args.arena, args.silent_threshold)
            return 0

        missing = check_paths(paths)
        if missing:
            print("路径没配齐：" + "；".join(missing))
            return 2
        plan_exe = Path(paths["plan_exe"]) if paths.get("plan_exe") else Path("")
        try:
            matchups = select_matchups(args, windbot_dir, plan_exe)
        except (FileNotFoundError, ValueError) as exc:
            # 组合算不出来时直接报错：A/B 缺了计划感知执行器还硬跑，等于测随机脚本
            print(f"[错误] {exc}")
            return 2
        if not matchups:
            print("没有要打的对手组合：用 --decks N 或 --pairs A,B 指定")
            return 2

        if args.ab_plan and not args.pair_styles:
            print(
                f"A/B 实验：同一副牌（{args.ab_plan}），"
                f"一侧 PlanAware + {args.coach} 教练，一侧 {args.control_style}；"
                f"每轮 2 局镜像，共 {args.rounds} 轮"
            )
            # 把两侧各自的 exe 打出来：出牌脚本是编译进 exe 的，用错 exe 会静默换随机脚本
            for fighter in matchups[0]:
                origin = fighter.executable or Path(paths["windbot_exe"])
                print(f"  {fighter.name:<16} 脚本={fighter.style:<10} exe={origin}")
        if args.pair_styles:
            print(
                f"脚本排名：同一副牌（{args.ab_plan}），"
                f"{matchups[0][0].style} vs {matchups[0][1].style}；"
                f"每轮 2 局镜像，共 {args.rounds} 轮"
            )
        logger = logging.getLogger("train_arena")
        coach = build_coach(args)
        asyncio.run(run_matchups(args, matchups, paths, store, logger, coach))
        print_report(store, args.arena, args.silent_threshold)
        print(f"\n结果库：{store.path}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
