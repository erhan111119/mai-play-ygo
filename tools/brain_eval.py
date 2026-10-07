"""量一量「某个出牌脚本 + 逐步问 AI」到底有没有用。

**为什么要单独有一个工具**：房间那边的 AI 开关是给真实对局用的，量不出"值不值"——
只有同一副牌、同一个对手、镜像配对跑几十局，才知道多花的那些模型调用换来了什么。
之前这类实验是临时脚本跑的，跑完就散了，结论里的数字谁也复现不了。

用法（左边是要评估的一方，右边是对手）：

```bash
python tools/brain_eval.py --deck-id 83 --left-brain llm --opponent Albaz \
    --rounds 15 --parallel 6 --arena ai-maliss --baseline 4/30
```

* ``--deck-id``：从插件卡组库取卡表、脚本名，并自动带上这副牌的**攻略要点**
  （``<数据目录>/combos/<编号>.txt``）——房间那边一直是带着攻略问的，量的时候也必须带，
  否则量到的不是线上那套东西。
* ``--baseline 4/30``：上一次同配置的胜局/局数；工具会用 95% 区间判断这次有没有真的变化
  （**不比两个百分比的大小**，40 局的差别大多在噪声里）。
* 报告里会打出**问了多少次、答了多少次**：AI 的代价就是这些调用，动作数一起看才判断得清
  "是 AI 帮忙了，还是它只是把出牌时间吃掉了"。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import Knowledge  # noqa: E402  导入顺序受 sys.path 补丁影响


def load_tool(name: str):
    """按文件路径加载同目录的工具脚本（tools 下的脚本不是包的一部分）。"""

    spec = importlib.util.spec_from_file_location(
        f"tool_{name}", str(Path(__file__).resolve().parent / f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="量「脚本 + 逐步问 AI」的效果")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--deck", default="", help="左方卡组：内置脚本名或 .ydk 路径")
    parser.add_argument(
        "--deck-id", type=int, default=0, help="左方卡组的**数据库编号**（不是 /卡组列表 的位置号）"
    )
    parser.add_argument("--left-style", default="", help="左方出牌脚本；默认取卡组记录里的那份")
    parser.add_argument(
        "--left-brain",
        default="",
        choices=["", "rule", "llm"],
        help="左方的问 AI 策略：不填＝不问，rule＝固定答复（只验通道），llm＝真问模型",
    )
    parser.add_argument("--left-playbook", default="", help="左方打法数据文本文件（量它有没有用）")
    parser.add_argument(
        "--brain-scope",
        default="",
        choices=["", "all", "high_stakes", "interrupt_only"],
        help=(
            "问 AI 的范围：all＝每一问都问（默认）；high_stakes＝我的回合不回答「要不要发动」"
            "（交回脚本），对手回合照问，其余两问不变；interrupt_only＝连「这一步做什么」也交回脚本。"
            "实测（80 局/腿）：不问 23.1 动作/局、每问都问 20.7、只问高压 21.0"
            "——只问高压拦下了四成多的发动提问，可动作数没回来"
        ),
    )
    parser.add_argument(
        "--no-knowledge",
        action="store_true",
        help="左方不做知识库检索（只用静态素材）——A/B/C 里的 B 腿",
    )
    parser.add_argument("--opponent", required=True, help="右方卡组：内置脚本名或 .ydk 路径")
    parser.add_argument("--right-style", default="", help="右方出牌脚本；默认与 --opponent 同名")
    parser.add_argument("--right-brain", default="", choices=["", "rule", "llm"], help="右方的问 AI 策略")
    parser.add_argument("--left-name", default="我方", help="左方在报告/库里的名字")
    parser.add_argument("--right-name", default="", help="右方名字；默认「对手(<脚本名>)」")
    parser.add_argument("--rounds", type=int, default=15, help="镜像轮数（每轮 2 局）")
    parser.add_argument("--parallel", type=int, default=6, help="同时跑几局")
    parser.add_argument("--start-lp", type=int, default=4000, help="训练用初始生命值")
    parser.add_argument(
        "--time-limit",
        type=int,
        default=180,
        help="单回合时限（秒）；**默认跟房间一致（180）**，调小会让「问 AI」那几腿被时钟判负",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=180.0,
        help=(
            "单局硬上限（秒）。**跑镜像对局（两边同一副牌）时务必放大到 600**："
            "两边同强时对局本来就长，再叠加「问 AI」那侧的思考时间，会成片撞上这个上限被掐断——"
            "掐断的局既判不出胜负，动作数也失真（实测 40 局里 17 局正好卡在 180 秒，"
            "「问 AI」那侧看起来少打三成，其实是测量假象）"
        ),
    )
    parser.add_argument("--shuffle-seed-base", type=int, default=0, help="牌序种子基准（换一批牌序用）")
    parser.add_argument("--shuffle", action="store_true", help="允许内核洗切（默认关，便于镜像配对）")
    parser.add_argument("--arena", default="", help="结果落库用的擂台名")
    parser.add_argument("--db", default="", help="结果库路径；默认 <插件>/temp/train/arena.db")
    parser.add_argument("--baseline", default="", help="基线，形如 4/30；给了就做区间比较")
    parser.add_argument("--verbose-bots", action="store_true", help="把 bot 自己的输出打进日志")
    parser.add_argument("--ygopro-exe", default="")
    parser.add_argument("--ygopro-dir", default="")
    parser.add_argument("--windbot-exe", default="")
    parser.add_argument("--windbot-dir", default="")
    parser.add_argument("--cards-cdb", default="")
    parser.add_argument("--plan-executable", default="", help="计划感知执行器所在的 exe（这里只用来解析路径）")
    return parser.parse_args()


def parse_baseline(raw: str) -> Optional[Tuple[int, int]]:
    """把 ``4/30`` 解析成 ``(4, 30)``；空串返回 None，写错了直接报错。"""

    text = (raw or "").strip()
    if not text:
        return None
    wins, _, total = text.partition("/")
    if not (wins.strip().isdigit() and total.strip().isdigit()):
        raise ValueError(f"--baseline 要写成「胜局/局数」，拿到的是 {raw!r}")
    return int(wins), int(total)


def style_for(name: str, styles) -> str:
    """把"卡组名/路径"转成出牌脚本名。

    **脚本名就是卡组名**（``Albaz``、``MalissOCG``），而 ``WIND_BOT_DECK_NAMES`` 那张表是
    "脚本名 → ``AI_*.ydk`` 文件名"。把文件名当脚本名传（``Deck=AI_Albaz``）不会报错——
    WindBot 会**静默换成随机执行器**，而那个执行器不认识这副牌，整局一步不动
    （实测：对手 28 回合 0 动作，看起来像"AI 一侧大胜"，其实是量错了东西）。
    """

    if name.lower().endswith(".ydk") or Path(name).is_file():
        return "Test"  # 任意 .ydk 没有对应的专属脚本名，用通用脚本
    return name if name in styles else "Test"


def style_registered(style: str, exe: Path, styles) -> Tuple[bool, str]:
    """这个脚本名在这份 exe 里注册过吗。

    内置脚本查 ``WIND_BOT_DECK_NAMES`` 这张表（它由插件自己的测试对着 exe 的注册表核过）；
    投稿卡组生成的脚本（``Gen86`` 这类）走 :func:`evaluate_style.style_is_registered` 的类名检查。

    **这是"实验有没有效"的闸门**：WindBot 找不到 ``Deck=`` 请求的名字时不报错，它会随机挑一个
    Normal 档执行器顶上（Game/AI/DecksManager.cs）。名字对不上时跑出来的胜率与这个脚本毫无关系
    ——我们在这上面浪费过一整批几百局的数据。
    """

    if style in styles:
        return True, f"内置脚本（{styles[style]}）"
    # 生成脚本（Gen88 这类）：直接看 exe 里有没有那个执行器类。
    # **不能拿它跟"原版 exe 注册了几个脚本"比大小**——我们两边指的常常是同一份源码树产物，
    # 于是"候选 ≤ 原版"永远成立，会把编译好的脚本判成没编译进去（实测就是这么误报过）。
    gate = load_tool("evaluate_style")
    class_name = gate.style_class_name(style)
    try:
        data = exe.read_bytes()
    except OSError as exc:
        return False, f"读不动这份 exe：{exc}"
    if class_name.encode("utf-8") in data:
        return True, f"生成脚本（类 {class_name} 在这份 exe 里）"
    return False, f"这份 exe 里找不到执行器类 {class_name}（生成的脚本没编译进去）"


def build_arena_class():
    """造一个能收集"答复服务"的擂台类（报告要读它数问了多少次）。"""

    from train.arena import DuelArena

    class ObservingArena(DuelArena):
        """记住这一局所有的答复服务，好在报告里说"问了多少次、答了多少次"。"""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.servers: List[object] = []

        def _make_brain_server(self, prefix, decide):
            server = super()._make_brain_server(prefix, decide)
            self.servers.append(server)
            return server

    return ObservingArena


def describe_brains(args: argparse.Namespace, data_dir: Path) -> List[str]:
    """把"这一局问 AI 的素材与模型"打出来——不打印的话，事后没人知道问的是哪个模型。"""

    lines: List[str] = []
    for side, brain in (("左", args.left_brain), ("右", args.right_brain)):
        if not brain:
            continue
        if brain == "llm":
            from train.ai_brain import load_brain_model_settings

            settings = load_brain_model_settings(data_dir)
            if settings is None:
                lines.append(
                    f"{side}方问 AI：策略 llm，但没读到 {data_dir / 'brain_model.toml'}"
                    "（这一方会照脚本打，不开通道）"
                )
            else:
                lines.append(f"{side}方问 AI：策略 llm，模型 {settings.model}（{settings.base_url}）")
        else:
            lines.append(f"{side}方问 AI：策略 {brain}")
    if args.brain_scope and args.left_brain == "llm":
        # 报告里必须留下"这一批问的是什么范围"——否则两条腿的数字没法比
        scope_names = {
            "all": "每一问都问",
            "high_stakes": "只问高压决策（我的回合不回答发动、交回脚本）",
            "interrupt_only": "只应对不掌舵（连「这一步做什么」也交回脚本）",
        }
        lines.append("左方问 AI 范围：" + scope_names.get(args.brain_scope, args.brain_scope))
    return lines


def report(
    outcomes: List[object],
    *,
    left_name: str,
    right_name: str,
    answered: int,
    answers: Optional[Dict[str, int]] = None,
    baseline: Optional[Tuple[int, int]],
) -> int:
    """打印结论；返回退出码（基线比较"没变化"时返回 3，便于脚本判断）。

    **为什么不比百分比大小**：40 局的 62% 与 50% 的差别在噪声里（1.6σ），照那样挑出来的
    "改进"只是恰好运气好。这里一律用 95% 区间：只有整体高于基线才算变了。
    """

    from train.deckopt import accept_candidate, wilson_interval

    total = len(outcomes)
    if total == 0:
        print("[错误] 一局都没打成，看日志里的报错")
        return 2
    wins = sum(1 for item in outcomes if item.winner == left_name)
    losses = sum(1 for item in outcomes if item.winner == right_name)
    undecided = total - wins - losses
    low, high = wilson_interval(wins, total)
    actions_left = sum(item.actions_left for item in outcomes) / total
    actions_right = sum(item.actions_right for item in outcomes) / total
    sp_left = sum(getattr(item, "sp_summons_left", 0) for item in outcomes) / total
    sp_right = sum(getattr(item, "sp_summons_right", 0) for item in outcomes) / total
    effects_left = sum(getattr(item, "effects_left", 0) for item in outcomes) / total
    seconds = sum(item.duration_seconds for item in outcomes) / total
    turns = sum(item.turns for item in outcomes) / total
    timeouts = sum(1 for item in outcomes if item.reason == "超时")
    # "能不能展开"的直接指标：整局一次额外怪都没做出来的比例（用户报的就是这个）
    dry = sum(1 for item in outcomes if getattr(item, "sp_summons_left", 0) == 0)

    print("\n===== 结果 =====")
    print(f"  局数 {total}：{left_name} 胜 {wins}、{right_name} 胜 {losses}、未分胜负 {undecided}")
    print(f"  {left_name} 胜率 {wins / total:.1%}（95% 区间 {low:.1%}~{high:.1%}）")
    print(f"  平均动作数：{left_name} {actions_left:.1f}｜{right_name} {actions_right:.1f}")
    print(
        f"  **展开指标**：{left_name} 每局特殊召唤 {sp_left:.1f} 次、发动效果 {effects_left:.1f} 次｜"
        f"对手特召 {sp_right:.1f} 次；{left_name} **一次特召都没有的局：{dry}/{total}**"
    )
    print(f"  平均 {turns:.1f} 回合、{seconds:.0f} 秒；累计答复 {answered} 次"
          + (f"，其中超时判负 {timeouts} 局" if timeouts else ""))
    if answers:
        counted = "、".join(f"{key}×{value}" for key, value in sorted(answers.items(), key=lambda kv: -kv[1]))
        vetoes = sum(value for key, value in answers.items() if key in ("no", "0"))
        print(f"  答复分布：{counted}（推算否决 {vetoes} 次）")
    if baseline is not None:
        base_wins, base_total = baseline
        accepted, detail = accept_candidate(base_wins, base_total, wins, total)
        print(f"  与基线 {base_wins}/{base_total} 比较：{detail}")
        return 0 if accepted else 3
    return 0


async def run(args: argparse.Namespace) -> int:
    cli = load_tool("train_arena")
    evaluate = load_tool("evaluate_style")
    from train.arena import Fighter
    from train.store import DuelStore

    data_dir = Path(args.data_dir) if args.data_dir else evaluate.default_data_dir(Path(args.plugin_root))

    deck = args.deck
    left_style = args.left_style
    deck_id = int(args.deck_id)
    if deck_id:
        # 投稿/优化过的卡组走卡组库：卡表路径、脚本名都从记录里取，避免手工拼路径指错文件
        try:
            display_name, deck_path, recorded_style = evaluate.load_submitted_deck(data_dir, deck_id)
        except FileNotFoundError as exc:
            print(f"[错误] {exc}")
            return 2
        deck = str(deck_path)
        left_style = left_style or recorded_style
        print(f"卡组 #{deck_id}「{display_name}」→ {deck_path.name}（脚本 {left_style or '未记录'}）")
        guide = data_dir / "combos" / f"{deck_id}.txt"
        print(f"攻略要点：{guide if guide.is_file() else '（没有，展开链只能靠模型自己猜）'}")
        knowledge = Knowledge.from_data_dir(data_dir, plugin_root=Path(args.plugin_root))
        if not knowledge.available:
            print(f"知识库：没有（{knowledge.path}）——这一轮等于只喂静态素材")
        else:
            counts = knowledge.counts()
            print(
                f"知识库：{knowledge.path.name}｜卡牌事实 {counts['card_facts']} 张、"
                f"线路 {counts['deck_plans']} 条、交互 {counts['interactions']} 条"
                f"{'｜**本轮左方关闭检索（--no-knowledge）**' if args.no_knowledge else ''}"
            )
    if not deck:
        print("[错误] 要么给 --deck（卡组名或 .ydk 路径），要么给 --deck-id（卡组库编号）")
        return 2

    paths = cli.resolve_paths(args)
    missing = cli.check_paths(paths)
    if missing:
        print("路径没配齐：" + "；".join(missing))
        return 2
    windbot_dir = Path(paths["windbot_dir"])
    windbot_exe = Path(paths["windbot_exe"])
    if not windbot_exe.is_file():
        print(f"[错误] 找不到 WindBot.exe：{windbot_exe}")
        return 2

    deck_path = cli.resolve_deck_source(deck, windbot_dir)
    if not deck_path.is_file():
        print(f"[错误] 找不到卡表：{deck_path}")
        return 2
    styles = cli.WIND_BOT_DECK_NAMES
    left_style = left_style or style_for(deck, styles)
    right_style = args.right_style or style_for(args.opponent, styles)
    right_deck = cli.resolve_deck_source(args.opponent, windbot_dir)
    if not right_deck.is_file():
        print(f"[错误] 找不到对手卡表：{right_deck}")
        return 2

    for style, label in ((left_style, "左方"), (right_style, "右方")):
        ok, detail = style_registered(style, windbot_exe, styles)
        if not ok:
            print(
                f"[错误] {label}脚本 {style} 不在这个 exe 里：{windbot_exe}\n  {detail}\n"
                "  WindBot 找不到脚本名时会随机挑一个执行器顶上，跑出来的数字与它无关——"
                "内置脚本名用卡组名（如 Albaz），生成的脚本名用卡组记录里那个。"
            )
            return 2

    playbook = ""
    if args.left_playbook:
        playbook = Path(args.left_playbook).read_text(encoding="utf-8", errors="replace")
    left = Fighter(
        name=args.left_name,
        deck_file=deck_path,
        style=left_style,
        playbook=playbook,
        brain=args.left_brain,
        brain_scope=args.brain_scope,
        deck_id=deck_id,
        knowledge=not args.no_knowledge,
    )
    right = Fighter(
        name=args.right_name or f"对手({right_style})",
        deck_file=right_deck,
        style=right_style,
        brain=args.right_brain,
    )

    print(f"左：{left.name}｜脚本 {left.style}｜卡表 {deck_path.name}｜问 AI {left.brain or '不问'}")
    print(f"右：{right.name}｜脚本 {right.style}｜卡表 {right_deck.name}｜问 AI {right.brain or '不问'}")
    print(f"exe：{windbot_exe}")
    for line in describe_brains(args, data_dir):
        print(line)
    if playbook:
        print(f"左方打法数据：{len(playbook)} 字（{args.left_playbook}）")
    print(f"每轮 {args.rounds} 轮镜像（{args.rounds * 2} 局），并发 {args.parallel}")
    if (args.left_brain == "llm" or args.right_brain == "llm") and args.time_limit < 90:
        # 问 AI 的每一问都要真花时间（实测每题平均 1.2~2.7 秒、最强的那问 20 秒以上），
        # 单回合时限给到 30 秒时整局会被时钟判负：实测 12 局里 10 局超时、只打了 2.1 回合，
        # 于是量出来的是"谁先超时"而不是"谁打得好"。房间的时限是 180 秒，测量要对齐它。
        print(
            f"[警告] 单回合时限只有 {args.time_limit} 秒，而房间用的是 180 秒："
            "问 AI 那几腿会被时钟判负，这一批数字不能用来判断打得好不好。"
        )

    store = DuelStore(Path(args.db) if args.db else _PLUGIN_ROOT / "temp" / "train" / "arena.db")
    logger = logging.getLogger("brain_eval")
    arena_name = args.arena or "brain"
    outcomes: List[object] = []
    try:
        config = cli.make_arena_config(args, paths)
        # 牌序种子在共享的构造函数里没有参数（那是给所有工具用的），造好后单独设——
        # 与 tools/optimize_deck.py 的做法一致。批量复测要换一批牌序时靠它。
        config.shuffle_seed_base = args.shuffle_seed_base
        arena = build_arena_class()(config, logger=logger)
        done = 0

        def record_one(outcome) -> None:
            nonlocal done
            store.record(arena_name, outcome, left_style=left.style, right_style=right.style)
            outcomes.append(outcome)
            done += 1
            print(
                f"  [{done}/{args.rounds * 2}] 第 {outcome.round_index} 轮 → {outcome.winner}"
                f"（{outcome.turns} 回合，动作 {outcome.actions_left}:{outcome.actions_right}）"
            )

        await arena.play_pair(left, right, rounds=args.rounds, on_outcome=record_one)
        answered = sum(int(getattr(server, "answered", 0)) for server in arena.servers)
        answers: Dict[str, int] = {}
        for server in arena.servers:
            for key, value in dict(getattr(server, "answers", {})).items():
                answers[key] = answers.get(key, 0) + int(value)
    finally:
        store.close()

    code = report(
        outcomes,
        left_name=left.name,
        right_name=right.name,
        answered=answered,
        answers=answers,
        baseline=parse_baseline(args.baseline),
    )
    print(f"\n结果库：{store.path}（擂台 {arena_name}）")
    return code


def main() -> int:
    """入口。"""

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose_bots else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
