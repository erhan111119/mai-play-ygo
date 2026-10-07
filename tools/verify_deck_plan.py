"""抽检一条展开流程好不好用：**打若干局，看它能不能真的展开**，差的自动重写。

判据不是胜率（胜率在几十局的样本里什么都说明不了），而是**展开指标**：
每局特殊召唤几次、有多少局"一次特召都没有"（那种局就是"根本没动起来"）。

流程（一轮）：
1. 让擂台跑 N 轮镜像（默认 10 轮 = 20 局），左方是本副牌 + AI 出牌 + 知识库（含这份流程）；
2. 从结果库读展开指标；
3. 达标就留档并记进 ``meta``；不达标就把失败数据当**反馈**再生成一版（``--feedback``），
   最多重来 ``--attempts`` 轮，最后留**表现最好的那一版**。

**不复用子进程**：直接按模块调用 ``brain_eval.run`` 与 ``build_deck_plan_ai.main_async``——
既少一层进程，也不用把外部输入拼进命令行（那正是命令注入的老套路）。

用法::

    python tools/verify_deck_plan.py --deck-id 89                 # 抽检 20 局
    python tools/verify_deck_plan.py --deck-id 89 --rounds 20      # 抽检 40 局（更稳）
    python tools/verify_deck_plan.py --all-ai-plans                # 把所有 AI 生成的流程都过一遍
    python tools/verify_deck_plan.py --deck-id 89 --no-rewrite     # 只看数字，不重写
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, Knowledge, default_data_dir, default_knowledge_db  # noqa: E402

# 达标线（可用命令行覆盖）：这些数字来自实测的"动起来/没动起来"分界——
# 只有脚本时每局特召 1.3 次、30% 的局一次都没有；AI 出牌后是 3.9 次、0%。
DEFAULT_MIN_SP = 2.0
DEFAULT_MAX_DRY_RATIO = 0.2


def load_tool(name: str):
    """按文件路径加载同目录的工具（tools/ 不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(
        f"tool_{name}", str(Path(__file__).resolve().parent / f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抽检展开流程：打几局看它能不能展开，差的自动重写")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--deck-id", type=int, default=0, help="要抽检的卡组编号")
    parser.add_argument("--all-ai-plans", action="store_true", help="抽检所有 AI 生成的展开流程")
    parser.add_argument(
        "--all-decks",
        action="store_true",
        help="抽检卡组池里**所有**卡组（含内置卡组）；配合 --generate-missing 就能让每副牌都有流程",
    )
    parser.add_argument(
        "--generate-missing",
        action="store_true",
        help="遇到没有展开流程的卡组先生成一份（按卡表，几十秒），再抽检",
    )
    parser.add_argument("--opponent", default="Albaz", help="陪练的内置卡组（默认 Albaz）")
    parser.add_argument("--rounds", type=int, default=10, help="镜像轮数（每轮 2 局）")
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--time-limit", type=int, default=180)
    parser.add_argument("--max-duration", type=float, default=300.0)
    parser.add_argument("--min-sp", type=float, default=DEFAULT_MIN_SP, help="每局特召次数合格线")
    parser.add_argument("--min-actions", type=float, default=6.0, help="每局动作数下限（判断「动起来了吗」）")
    parser.add_argument("--min-effects", type=float, default=1.0, help="每局效果发动次数下限")
    parser.add_argument(
        "--max-dry-ratio", type=float, default=DEFAULT_MAX_DRY_RATIO,
        help="允许的「一次特召都没有」比例",
    )
    parser.add_argument("--attempts", type=int, default=2, help="最多重写几轮（含首版）")
    parser.add_argument("--no-rewrite", action="store_true", help="只看数字，不重写")
    parser.add_argument("--db", default="", help="结果库；默认 <插件>/temp/train/arena.db")
    return parser.parse_args()


def ai_plan_deck_ids(database: Path) -> List[int]:
    """库里所有 ``source='ai'`` 的卡组编号（按需生成的那些）。"""

    if not database.is_file():
        return []
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT deck_id FROM deck_plans WHERE kind = 'deck' AND source = 'ai' AND deck_id > 0"
            " ORDER BY deck_id"
        ).fetchall()
    finally:
        connection.close()
    return [int(row[0]) for row in rows]


def pool_deck_ids(data_dir: Path) -> List[int]:
    """卡组池里所有卡组的编号（含内置卡组——它们平时靠自带脚本，但 AI 开着时也吃展开流程）。"""

    database = data_dir / "deck_pool.db"
    if not database.is_file():
        return []
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT deck_id FROM decks ORDER BY deck_id").fetchall()
    finally:
        connection.close()
    return [int(row[0]) for row in rows]


def decks_with_plan(database: Path) -> set:
    """已经有展开流程的卡组编号。"""

    if not database.is_file():
        return set()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT deck_id FROM deck_plans WHERE kind = 'deck' AND deck_id > 0"
        ).fetchall()
    finally:
        connection.close()
    return {int(row[0]) for row in rows}


def parse_tool_args(module, argv: Sequence[str]):
    """借工具自己的 ``parse_args`` 造参数对象（把 argv 临时换成我们要传的那些）。"""

    saved = sys.argv
    sys.argv = list(argv)
    try:
        return module.parse_args()
    finally:
        sys.argv = saved


async def run_arena(args: argparse.Namespace, deck_id: int, arena_name: str) -> bool:
    """跑一批对局：复用 brain_eval（它已经是"同副牌 + AI + 知识库"那条路）。"""

    brain_eval = load_tool("brain_eval")
    argv = [
        "brain_eval",
        "--plugin-root",
        str(args.plugin_root),
        "--deck-id",
        str(int(deck_id)),
        "--opponent",
        str(args.opponent),
        "--left-brain",
        "llm",
        "--rounds",
        str(int(args.rounds)),
        "--parallel",
        str(int(args.parallel)),
        "--time-limit",
        str(int(args.time_limit)),
        "--max-duration",
        str(float(args.max_duration)),
        "--arena",
        str(arena_name),
    ]
    if args.data_dir:
        argv += ["--data-dir", str(args.data_dir)]
    if args.db:
        argv += ["--db", str(args.db)]
    namespace = parse_tool_args(brain_eval, argv)
    code = await brain_eval.run(namespace)
    return code in (0, 3)  # 3 = 与基线分不开（这里不关心胜率）


def read_metrics(db: Path, arena_name: str) -> Dict[str, float]:
    """从结果库读这一批的展开指标。"""

    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COUNT(*), AVG(sp_summons_left), AVG(effects_left), AVG(actions_left),"
            " SUM(CASE WHEN sp_summons_left = 0 THEN 1 ELSE 0 END)"
            " FROM duels WHERE arena = ?",
            (arena_name,),
        ).fetchone()
    finally:
        connection.close()
    total = int(row[0] or 0)
    if not total:
        return {"games": 0.0, "sp": 0.0, "effects": 0.0, "actions": 0.0, "dry_ratio": 1.0}
    return {
        "games": float(total),
        "sp": float(row[1] or 0.0),
        "effects": float(row[2] or 0.0),
        "actions": float(row[3] or 0.0),
        "dry_ratio": float(row[4] or 0) / total,
    }


def verdict(metrics: Dict[str, float], args: argparse.Namespace) -> Tuple[bool, str, str]:
    """这批数字怎么判；返回 ``(是否要重写, 结论行, 说明)``。

    **两档判据，只有第一档会触发重写**（实测教训）：

    1. **动起来了吗**（硬判据）：每局动作数够不够、有没有发过效果。
       不合格 = 这份流程/这套配置真的卡住了 → 值得带着反馈重写。
    2. **展开到线了吗**（软判据）：每局特召次数、「一次特召都没有」的比例。
       **只提示、不重写**——因为**控制/削血/beat 这类卡组本来特召就少**：
       实测一副 13 回合、双方 400 LP 的慢速牌，被"每局特召 ≥2"判成不合格并重写，
       第二版反而更差（0.8 → 0.6）。用一把尺子量所有卡组，只会把好流程改坏。
    """

    if metrics["games"] <= 0:
        return False, "一局都没打成（看上面的输出）", "没有有效对局"
    moving = metrics["actions"] >= args.min_actions and metrics["effects"] >= args.min_effects
    combo_line = metrics["sp"] >= args.min_sp and metrics["dry_ratio"] <= args.max_dry_ratio
    detail = (
        f"每局动作 {metrics['actions']:.1f}（线 {args.min_actions}）、效果 {metrics['effects']:.1f} 次"
        f"（线 {args.min_effects}）｜展开：特召 {metrics['sp']:.1f} 次（线 {args.min_sp}）、"
        f"「一次特召都没有」{metrics['dry_ratio']:.0%}（线 {args.max_dry_ratio:.0%}）"
    )
    if not moving:
        return True, f"{detail} → 没动起来", "这套配置下它几乎不出牌，值得重写流程"
    if combo_line:
        return False, f"{detail} → 动起来了，展开也到线", "合格"
    return (
        False,
        f"{detail} → 动起来了（展开没到线；若这副牌是展开卡组才需要重写）",
        "动起来就算合格：慢速/控制卡组的特召次数天然就低",
    )


def remember_result(database: Path, deck_id: int, metrics: Dict[str, float], arena_name: str) -> None:
    """把抽检结果记进 ``meta``（一行一个卡组，便于"哪些还没抽检过"）。"""

    connection = sqlite3.connect(database)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (
                f"verify:deck:{int(deck_id)}",
                json.dumps(
                    {
                        "sp": round(metrics["sp"], 2),
                        "dry_ratio": round(metrics["dry_ratio"], 3),
                        "effects": round(metrics["effects"], 2),
                        "games": int(metrics["games"]),
                        "arena": arena_name,
                        "at": time.time(),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


async def regenerate(args: argparse.Namespace, deck_id: int, feedback: str) -> bool:
    """把失败数据当反馈，让模型重写一版展开流程。"""

    plan_tool = load_tool("build_deck_plan_ai")
    argv = [
        "build_deck_plan_ai",
        "--plugin-root",
        str(args.plugin_root),
        "--deck-id",
        str(int(deck_id)),
        "--activate",
        "--feedback",
        str(feedback),
    ]
    if args.data_dir:
        argv += ["--data-dir", str(args.data_dir)]
    namespace = parse_tool_args(plan_tool, argv)
    print("  重写展开流程（把实测问题当反馈喂回去）")
    return await plan_tool.main_async(namespace) == 0


def current_plan_body(database: Path, deck_id: int) -> str:
    """读当前那版展开流程的正文（重写到更差时要能放回去）。"""

    knowledge = Knowledge(database, plugin_root=_PLUGIN_ROOT)
    entry = knowledge.deck_plan(int(deck_id))
    knowledge.close()
    return entry.body if entry is not None else ""


async def verify_one(args: argparse.Namespace, deck_id: int, database: Path, db: Path) -> bool:
    """抽检一副牌；返回是否达标。"""

    print(f"\n===== 卡组 #{deck_id} =====")
    best: Optional[Dict[str, float]] = None

    for attempt in range(1, max(1, args.attempts) + 1):
        arena_name = f"verify-{deck_id}-v{attempt}"
        print(f"  第 {attempt} 轮：")
        if not await run_arena(args, deck_id, arena_name):
            print("  （这一轮对局没跑完，跳过）")
            return False
        metrics = read_metrics(db, arena_name)
        needs_rewrite, detail, why = verdict(metrics, args)
        print(f"  结果：{detail}")
        if why:
            print(f"        （{why}）")
        if best is None or metrics["sp"] > best["sp"]:
            best = metrics
        if not needs_rewrite:
            remember_result(database, deck_id, metrics, arena_name)
            return True
        if args.no_rewrite or attempt >= args.attempts:
            break
        feedback = (
            f"实测 {int(metrics['games'])} 局：每局只特殊召唤 {metrics['sp']:.1f} 次、"
            f"{metrics['dry_ratio']:.0%} 的局一次特召都没有、每局发动效果 {metrics['effects']:.1f} 次。"
            "说明这份流程没被执行出来——请把「第一回合先出什么、按什么顺序」写得更明确，"
            "特别是 1 卡起手能做出的最小场。"
        )
        await regenerate(args, deck_id, feedback)

    if best is not None:
        remember_result(database, deck_id, best, f"verify-{deck_id}-best")
        best_body = current_plan_body(database, deck_id)
        print(
            f"  抽检结束：留最后这版（每局特召 {best['sp']:.1f} 次、没有特召的局 {best['dry_ratio']:.0%}，"
            f"正文 {len(best_body)} 字）"
        )
    return False


async def main_async(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    database = default_knowledge_db(data_dir)
    if not database.is_file():
        print(f"[错误] 还没有知识库：{database}（先跑 tools/build_card_facts.py）")
        return 2
    db = Path(args.db) if args.db else _PLUGIN_ROOT / "temp" / "train" / "arena.db"

    if args.all_ai_plans or args.all_decks:
        deck_ids: List[int] = pool_deck_ids(data_dir) if args.all_decks else ai_plan_deck_ids(database)
        if args.generate_missing:
            have = decks_with_plan(database)
            missing = [deck_id for deck_id in deck_ids if deck_id not in have]
            if missing:
                print(f"这些卡组还没有展开流程，先生成：{missing}")
                for deck_id in missing:
                    print(f"\n--- 生成 #{deck_id} ---")
                    await regenerate(args, deck_id, "")
                deck_ids = [deck_id for deck_id in deck_ids if deck_id in decks_with_plan(database)]
        if not deck_ids:
            print("没有要抽检的卡组（先用 tools/build_deck_plan_ai.py 生成一份）")
            return 0
        print(f"要抽检的卡组（{len(deck_ids)} 副）：{deck_ids}")
    elif args.deck_id:
        deck_ids = [int(args.deck_id)]
    else:
        print("[错误] 要么给 --deck-id，要么给 --all-ai-plans / --all-decks")
        return 2

    passed = 0
    for deck_id in deck_ids:
        if await verify_one(args, int(deck_id), database, db):
            passed += 1
    print(f"\n抽检完成：{passed}/{len(deck_ids)} 副达标")
    return 0 if passed == len(deck_ids) else 1


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    import asyncio

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
