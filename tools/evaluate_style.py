"""评估一个出牌脚本值不值得保留：跑镜像对局，用统计给出「保留 / 丢弃 / 样本不足」。

**为什么需要它**：LLM 改写（或自己手写）的出牌脚本"看起来更聪明"是极不可靠的判断——擂台
实测反复表明，几百局样本里 ±5% 的差别常常只是噪声。所以"能不能留"必须由数字说话：
试验组与对照组各坐一次先攻、镜像配对，**胜率的 95% 置信区间整体高于 50% 才算赢**；
区间压在 50% 以下就是明确更差；压在两边就是样本不够，这时它会算出"要判定 X% 的差别需要多少局"。

**对手不止一个**（默认 ``Test`` 加上这副牌自己的原型脚本）：只对着一个对手刷出来的胜率，
多半只是把那一个对手打舒服了，换个对手就露馅。

**脚本必须真的编译进那份 exe**：WindBot 找不到 ``Deck=`` 请求的名字时不会报错，而是随机挑一个
Normal 档执行器顶上——那样整批数据都是废的。所以这里会先检查 exe 里有没有注册这个名字
（名字以 UTF-16 存在程序集里），没有就直接拒绝开跑。

用法::

    # 评估 LLM 生成的脚本（默认用 <windbot_src_dir>/bin/Release/WindBot.exe）
    python tools/evaluate_style.py --deck Blue-Eyes --style Deck42 --rounds 100

    # 评估计划感知执行器（要带上教练，不然它和通用脚本没差别）
    python tools/evaluate_style.py --deck Blue-Eyes --style PlanAware \\
        --style-exe <src>/bin/PlanAware/WindBot.exe --coach rule --rounds 100

退出码：0=保留，1=丢弃，2=出错，3=样本不足（脚本化迭代时可以直接用）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import argparse
import asyncio
import importlib.util
import json
import logging
import math
import sqlite3
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# 判定的门槛：想判定"至少强 3%"，500 局一批才够（±2.2% @1σ）；判定 5% 需要 200 局左右
TARGET_EFFECT = 0.03
# 95% 置信区间的 z 值（双侧）
_Z = 1.96


def load_train_arena():
    """按文件路径加载擂台 CLI，复用它的配置解析与选手构造（两个都是脚本，不走包导入）。"""

    spec = importlib.util.spec_from_file_location(
        "train_arena_cli", str(_PLUGIN_ROOT / "tools" / "train_arena.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------- 统计


def wilson_interval(wins: int, total: int, *, z: float = _Z) -> Tuple[float, float]:
    """胜率的 Wilson 95% 置信区间。

    小样本时正态近似会给出越界（<0 或 >1）的区间，Wilson 不会，所以判定一律用它。
    """

    if total <= 0:
        return (0.0, 1.0)
    phat = wins / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def verdict(wins: int, total: int) -> Tuple[str, str]:
    """给结论：``keep`` / ``discard`` / ``unclear``，并附一句人话解释。"""

    low, high = wilson_interval(wins, total)
    rate = wins / total if total else 0.0
    if total <= 0:
        return "unclear", "一局都没打成，先看看日志里的报错"
    if low > 0.5:
        return "keep", f"胜率 {rate:.1%}（95% 区间 {low:.1%}~{high:.1%}），整体高于 50%，可以留"
    if high < 0.5:
        return "discard", f"胜率 {rate:.1%}（95% 区间 {low:.1%}~{high:.1%}），明确弱于对照，丢掉"
    return (
        "unclear",
        f"胜率 {rate:.1%}（95% 区间 {low:.1%}~{high:.1%}）还压着 50%，样本不够下结论："
        f"要判定 {TARGET_EFFECT:.0%} 的差别，每组大约需要 {rounds_needed(TARGET_EFFECT)} 局",
    )


def rounds_needed(effect: float, *, confidence: float = 0.95) -> int:
    """要判定 ``effect`` 这么大的胜率差别，每组需要多少局（镜像配对后是 2 倍局数）。"""

    if effect <= 0:
        return 0
    z = _Z if abs(confidence - 0.95) < 1e-9 else 2.576
    return int(math.ceil((z * z * 0.25) / (effect * effect)))


def style_class_name(style: str) -> str:
    """脚本名对应的执行器类名（WindBot 的命名惯例：``<名字>Executor``）。"""

    return f"{style}Executor"


def registered_deck_count(exe: Path, *, timeout: float = 25.0) -> Optional[int]:
    """跑一下这份 exe，读出它注册了多少个出牌脚本（``Decks initialized, N found.``）。

    **为什么不直接在文件里搜名字**：.NET 把字符串存进压缩元数据堆，字节里既可能搜不到，
    也容易被类名（`XxxExecutor`）顶掉——实测两种误判都出现过。而这一行是 WindBot 启动时
    自己报的：和参照 exe 的数字一比就知道这份 exe 多了几个脚本。我们正是靠它发现
    "``Deck=PlanAware`` 其实走的是随机执行器"这个让一整批数据作废的问题。
    """

    import subprocess
    import time as _time

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        process = subprocess.Popen(
            [str(exe), "Name=探针", "Host=127.0.0.1", "Port=1"],
            cwd=str(exe.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except OSError:
        return None
    deadline = _time.monotonic() + timeout
    lines: List[str] = []
    try:
        while _time.monotonic() < deadline:
            line = process.stdout.readline() if process.stdout else b""
            if not line:
                break
            lines.append(line.decode("utf-8", errors="replace").strip())
            if "Decks initialized" in lines[-1]:
                break
    finally:
        process.kill()
        process.wait(timeout=10)
    for line in lines:
        marker = "Decks initialized,"
        if marker in line:
            digits = "".join(ch for ch in line.split(marker, 1)[1] if ch.isdigit())
            if digits:
                return int(digits)
    return None


def style_is_registered(paths: Dict[str, str], style: str, style_exe: Path) -> Tuple[bool, str]:
    """这份 exe 里是否真的注册了 ``style``；返回 ``(是否通过, 说明)``。"""

    reference = Path(paths.get("windbot_exe", ""))
    candidate_count = registered_deck_count(style_exe)
    if candidate_count is None:
        return False, f"读不出这份 exe 注册了多少脚本（它没打印 Decks initialized）：{style_exe}"
    if reference.is_file():
        reference_count = registered_deck_count(reference)
        if reference_count is not None and candidate_count <= reference_count:
            return (
                False,
                f"这份 exe 注册的脚本数（{candidate_count}）不比原版（{reference_count}）多，"
                f"说明 {style} 没编译进去；WindBot 会随机挑一个执行器顶上，跑出来的数字不能用",
            )
    try:
        data = style_exe.read_bytes()
    except OSError as exc:
        return False, f"读不动这份 exe：{exc}"
    if style_class_name(style).encode("utf-8") not in data:
        return False, f"这份 exe 里找不到执行器类 {style_class_name(style)}，脚本可能没编译进去"
    return True, f"已确认注册（这份 exe 共 {candidate_count} 个脚本）"


def default_data_dir(plugin_root: Path) -> Path:
    """插件数据目录（宿主约定：``<MaiBot>/data/plugins/<插件 id>/``）。"""

    manifest = plugin_root / "_manifest.json"
    plugin_id = ""
    try:
        plugin_id = str(json.loads(manifest.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        plugin_id = ""
    if not plugin_id:
        return plugin_root / "data"
    return plugin_root.parent.parent / "data" / "plugins" / plugin_id


def load_submitted_deck(data_dir: Path, deck_id: int) -> Tuple[str, Path, str]:
    """从插件的卡组库里取一副投稿卡组：``(显示名, .ydk 路径, 记录的脚本名)``。

    投稿卡组存在插件数据目录里（``decks/<群>/<uuid>.ydk``），不叫 ``AI_*.ydk``、也不在 WindBot
    目录下——所以评估它们只能走这条查询，手工拼路径很容易指错文件。

    Raises:
        FileNotFoundError: 卡组库或卡表文件不存在，或没有这个编号。
    """

    database = data_dir / "deck_pool.db"
    if not database.is_file():
        raise FileNotFoundError(f"找不到卡组库：{database}（用 --data-dir 指定插件数据目录）")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT display_name, ydk_path, windbot_deck, generated_script, picked_style FROM decks"
            " WHERE deck_id = ?",
            (int(deck_id),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise FileNotFoundError(f"卡组库里没有编号 {deck_id} 的卡组")
    display_name = str(row[0])
    deck_path = Path(str(row[1]))
    if not deck_path.is_file():
        raise FileNotFoundError(f"「{display_name}」的卡表文件不见了：{deck_path}")
    # **与房间同一套优先级**（见 plugin._with_current_style）：实测挑出来的 > 生成的专属脚本 > 映射到的脚本。
    # 顺序写错会让"工具里的实测"和"房间里的实际"用的不是同一份脚本——那 A/B 结论就没意义了
    style = str(row[4] or "") or str(row[3] or "") or str(row[2] or "")
    return display_name, deck_path, style


def filter_opponents(opponents: Sequence[str], style: str) -> Tuple[List[str], List[str]]:
    """去掉与待评估脚本同名的对手，返回 ``(保留的, 被跳过的)``。

    同名时两边跑的是同一个脚本，"胜率"只剩先手优势，测不出脚本强弱。投稿卡组常常映射到
    通用脚本（``Test``），不挡掉的话会白跑一批得到 ~50% 的数字。
    """

    kept = [item for item in opponents if item != style]
    skipped = [item for item in opponents if item == style]
    return kept, skipped


# ---------------------------------------------------------------------- 主流程


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="评估出牌脚本：保留、丢弃还是样本不足")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument(
        "--deck",
        default="",
        help="用哪副牌做实验：内置卡组名（--list-decks 里的）或任意 .ydk 路径",
    )
    parser.add_argument(
        "--deck-id",
        type=int,
        default=0,
        help="直接评估插件卡组库里第 N 副投稿卡组（脚本名、卡表路径都从库里取）",
    )
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument(
        "--style",
        default="",
        help="待评估的脚本名（必须是这一份 exe 里注册的名字）；--deck-id 时留空则用卡组记录的脚本",
    )
    parser.add_argument("--style-exe", default="", help="待评估脚本所在的 WindBot.exe；默认 bin/Release")
    parser.add_argument(
        "--opponents",
        default="",
        help="对手脚本（逗号分隔）；默认 Test 加上这副牌自己的原型脚本",
    )
    parser.add_argument("--rounds", type=int, default=50, help="每个对手跑几轮镜像（每轮 2 局）")
    parser.add_argument("--parallel", type=int, default=6, help="同时跑几局")
    parser.add_argument("--coach", default="none", help="教练：none（默认）/ rule / llm")
    parser.add_argument(
        "--llm-task",
        default="ygo_train",
        help=(
            "用宿主 model_config.toml 里哪个任务配置的模型（默认 ygo_train，专门给训练/评估用）。"
            "这样训练不会去蹭 planner 那种给聊天用的模型；不想让模型参与就 --coach none"
        ),
    )
    parser.add_argument("--llm-model", default="", help="--coach llm 时指定模型别名")
    parser.add_argument("--llm-allow-private", action="store_true", help="允许模型服务在内网/本机")
    parser.add_argument("--ygopro-dir", default="", help="ygopro 工作目录；默认读 config.toml")
    parser.add_argument("--ygopro-exe", default="", help="ygopro.exe；默认读 config.toml")
    parser.add_argument("--windbot-exe", default="", help="对照组的 WindBot.exe；默认读 config.toml")
    parser.add_argument("--windbot-dir", default="", help="WindBot 工作目录；默认读 config.toml")
    parser.add_argument("--cards-cdb", default="", help="cards.cdb；默认取 ygopro 目录下的")
    parser.add_argument("--plan-executable", default="", help="计划感知执行器所在的 exe（--coach 用）")
    parser.add_argument("--arena", default="", help="结果落库用的擂台名；默认 gate-<脚本名>")
    parser.add_argument("--db", default="", help="结果库路径；默认 <插件>/temp/train/arena.db")
    parser.add_argument("--start-lp", type=int, default=4000, help="训练用初始生命值")
    parser.add_argument("--time-limit", type=int, default=30, help="单回合时限（秒）")
    parser.add_argument("--max-duration", type=float, default=180.0, help="单局硬上限（秒）")
    parser.add_argument("--shuffle", action="store_true", help="允许洗切（默认关闭以便镜像配对）")
    parser.add_argument("--verbose-bots", action="store_true", help="把 bot 自己的输出打进日志")
    return parser.parse_args()


def default_style_exe(paths: Dict[str, str]) -> Path:
    """默认的"自己写的执行器"所在 exe：源码树的 Release 产物（手工/agent 写的执行器都编到那里）。"""

    src_dir = paths.get("windbot_src_dir", "").strip()
    if not src_dir:
        return Path("")
    return Path(src_dir) / "bin" / "Release" / "WindBot.exe"


def default_opponents(deck: str, style: str, windbot_dir: Path) -> List[str]:
    """默认对手：通用脚本 + 这副牌自己的原型脚本（能对上就用）。"""

    cli = load_train_arena()
    styles = ["Test"]
    stem = cli.WIND_BOT_DECK_NAMES.get(deck, deck)
    for name in cli.WIND_BOT_DECK_NAMES:
        if cli.WIND_BOT_DECK_NAMES[name] == stem and name not in styles:
            styles.append(name)
            break
    return [item for item in styles if item != style]


def build_coach(args: argparse.Namespace, cli):
    """按参数造教练（与擂台同一套逻辑）。"""

    if args.coach.strip().lower() not in ("llm", "ai", "模型"):
        return cli.make_coach(args.coach)
    from train.llm import ModelClient, ModelError, pick_model

    try:
        target = pick_model(task=args.llm_task, name=args.llm_model)
    except ModelError as exc:
        print(f"[错误] 读不到模型配置：{exc}")
        raise SystemExit(2) from exc
    client = ModelClient(target, allow_private_host=args.llm_allow_private)
    print(f"教练：{target.provider} / {target.model}")
    return cli.make_coach("llm", llm=client)


async def evaluate(args: argparse.Namespace) -> int:
    """跑完所有对手，打印结论并返回退出码。"""

    cli = load_train_arena()
    from train.arena import DuelArena
    from train.store import DuelStore

    if args.deck_id:
        # 投稿卡组走卡组库：脚本名与卡表路径都从记录里取，避免手工拼路径指错文件
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
        try:
            display_name, deck_path, recorded_style = load_submitted_deck(data_dir, args.deck_id)
        except FileNotFoundError as exc:
            print(f"[错误] {exc}")
            return 2
        args.deck = str(deck_path)
        if not args.style:
            args.style = recorded_style
        print(f"投稿卡组 #{args.deck_id}「{display_name}」→ {deck_path.name}（脚本 {args.style or '未记录'}）")
    if not args.deck:
        print("[错误] 要么给 --deck（卡组名或 .ydk 路径），要么给 --deck-id（插件卡组库里的编号）")
        return 2
    if not args.style:
        print("[错误] 没有可评估的脚本名：--deck-id 的卡组还没记录脚本，用 --style 指定")
        return 2

    paths = cli.resolve_paths(args)
    missing = cli.check_paths(paths)
    if missing:
        print("路径没配齐：" + "；".join(missing))
        return 2
    windbot_dir = Path(paths["windbot_dir"])

    style_exe = Path(args.style_exe) if args.style_exe else default_style_exe(paths)
    if not style_exe.is_file():
        print(f"[错误] 找不到待评估脚本所在的 exe：{style_exe}（用 --style-exe 指定）")
        return 2
    ok, detail = style_is_registered(paths, args.style, style_exe)
    if not ok:
        # 这一步是"实验有没有效"的闸门：名字不在 exe 里时 WindBot 会随机挑执行器顶上，
        # 跑出来的胜率与这个脚本毫无关系（我们在这上面浪费过一整批几百局的数据）
        print(f"[错误] {detail}\n  exe：{style_exe}")
        return 2
    print(f"待评估脚本：{args.style}（{detail}）")

    opponents = (
        [item.strip() for item in args.opponents.split(",") if item.strip()]
        if args.opponents
        else default_opponents(args.deck, args.style, windbot_dir)
    )
    # 试验组和对照组用同一个脚本时，这个"对比"只能测出先手胜率，与脚本强弱无关——
    # 投稿卡组常常映射到通用脚本（Test），不挡掉的话会得到一堆没有意义的 50%
    opponents, skipped = filter_opponents(opponents, args.style)
    for item in skipped:
        print(f"跳过对手 {item}：它与待评估脚本同名，比不出任何东西")
    if not opponents:
        print(
            "[错误] 没有可用的对照脚本：待评估脚本与所有对手同名。\n"
            "  投稿卡组若只映射到通用脚本，先让它生成专属脚本（发一次卡组码即可），"
            "或用 --opponents 指定别的对手脚本。"
        )
        return 2
    print(f"对手：{'、'.join(opponents)}；每个 {args.rounds} 轮镜像（{args.rounds * 2} 局）")

    store = DuelStore(Path(args.db) if args.db else _PLUGIN_ROOT / "temp" / "train" / "arena.db")
    logger = logging.getLogger("evaluate_style")
    coach = build_coach(args, cli)
    arena_name = args.arena or f"gate-{args.style}"
    tally: Dict[str, List[int]] = {}
    try:
        config = cli.make_arena_config(args, paths, coach=coach)
        arena = DuelArena(config, logger=logger)
        for opponent in opponents:
            treatment, control = cli.build_ab_fighters(
                args.deck, opponent, windbot_dir, style_exe, treatment_style=args.style
            )
            wins, total = 0, 0
            done = 0

            def record_one(outcome, opponent=opponent) -> None:
                nonlocal wins, total, done
                store.record(arena_name, outcome, left_style=treatment.style, right_style=control.style)
                done += 1
                total += 1
                if outcome.winner == treatment.name:
                    wins += 1
                print(
                    f"  [{done}/{args.rounds * 2}] {treatment.name} vs {control.name}"
                    f" → {outcome.winner}（第 {outcome.round_index} 轮）"
                )

            await arena.play_pair(
                treatment,
                control,
                rounds=args.rounds,
                on_outcome=record_one,
            )
            tally[opponent] = [wins, total]
            print(f"  对 {opponent}：{wins}/{total} = {wins / total:.1%}" if total else "  没有有效对局")
    finally:
        store.close()

    wins = sum(item[0] for item in tally.values())
    total = sum(item[1] for item in tally.values())
    print("\n===== 评估结论 =====")
    for opponent, (w, t) in tally.items():
        if t:
            print(f"  vs {opponent:<14} {w}/{t} = {w / t:.1%}")
    result, reason = verdict(wins, total)
    print(f"\n总体：{wins}/{total}")
    print(f"{'保留' if result == 'keep' else '丢弃' if result == 'discard' else '样本不足'}：{reason}")
    if args.coach != "none":
        print(f"（教练 {args.coach}；结果已落库到擂台 {arena_name}）")
    return {"keep": 0, "discard": 1, "unclear": 3}[result]


def main() -> int:
    """入口。"""

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose_bots else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
