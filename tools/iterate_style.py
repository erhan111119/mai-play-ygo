"""魔女术脚本的**自对弈迭代脚手架**：一条命令跑两条腿、出指标、给判定。

设计（每一步都是这几轮踩出来的经验）：

* **腿 1（镜像质量腿）**：同一副牌、同一个脚本两边互打。胜率天然五五开、没有信号，
  但**质量指标**（特召/局、效果/局、里程碑命中率、"一次特召都没有的局"）是对称的，
  跨迭代可比——用来回答"脚本有没有变得更会展开"。
* **腿 2（外部腿）**：同一副牌对 Albaz。牌序种子固定（`--shuffle-seed-base 0`），
  所以**不同迭代之间是配对可比的**——用来回答"变强没有，还是只是自己跟自己打得好看"。
* **里程碑**从 verbose 日志里数（本家关键落位 + 从墓地跳出来的次数），不靠肉眼。

判定（默认阈值，可用参数改）：

* 外部腿胜率**不掉**（不低于基线 - 8 个百分点）；
* 且质量指标里至少一项**明显上升**（特召/局 +0.3 以上）；
* 两条都满足 = 「接受」，否则「保留」/「回退」由人看一眼（脚本不替人做决定，只给数据）。

用法::

    uv run python tools/iterate_style.py --deck <#93 卡表.ydk> --style WitchcraftShop \\
        --note "结束阶段/费用：优先丢重复牌，不丢杰妮与商标女巫"

跑完会在 ``temp/train/iterations.jsonl`` 追加一行（迭代账本）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import argparse
import json
import re
import subprocess
import sys
import time

# 本副牌的里程碑卡号（要不要算命中率）
_MILESTONES = {
    50277355: "交织绵羊",
    69964858: "学童组合",
    33475154: "大魔女 桑德里永",
    9603252: "魔女术代理师傅",
}
# 「从墓地跳出来」也算里程碑（Combo 2 的商标女巫复活线）
_REVIVE_HINT = "from Grave move to MonsterZone"
# 汇总行里要抓的数字（brain_eval 的中文汇总）
_SUMMARY_PATTERNS = {
    "wins_left": r"胜 (\d+)",
    "actions_left": r"动作数：(\S+)｜(\S+)",
    "sp_left": r"特殊召唤 (\S+) 次",
    "effects_left": r"发动效果 (\S+) 次",
    "no_sp_games": r"一次特召都没有的局：(\d+)/(\d+)",
}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_brain_eval(
    *,
    plugin_root: Path,
    deck: str,
    left_style: str,
    left_name: str,
    right_style: str,
    right_deck: str,
    right_name: str,
    rounds: int,
    parallel: int,
    arena: str,
    verbose_log: Path,
) -> Tuple[str, str]:
    """跑一条腿，返回 (汇总文本, 日志文本)。

    ``right_style`` 只在**同一副牌**的镜像腿里才传（那时两边都该用被测脚本）。
    对**别的卡组**（外部腿）**绝不能传**——曾经在这里给 Albaz 也传了被测脚本名，
    结果那副牌被我们的脚本驱动、等于对手被削弱，"外部腿数字"全部作废。
    """

    command = [
        sys.executable,
        str(plugin_root / "tools" / "brain_eval.py"),
        "--deck",
        deck,
        "--left-style",
        left_style,
        "--left-name",
        left_name,
        "--opponent",
        right_deck,
        "--right-name",
        right_name,
        "--rounds",
        str(rounds),
        "--parallel",
        str(parallel),
        "--start-lp",
        "8000",
        "--max-duration",
        "600",          # ← 不放大这个值会把长局掐断，结论会反过来（本项目真踩过）
        "--arena",
        arena,
        "--verbose-bots",
    ]
    if right_style:
        command += ["--right-style", right_style]
    print(f"[跑] {' '.join(command[2:])}", flush=True)
    result = subprocess.run(
        command, cwd=str(plugin_root), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    verbose_log.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    return result.stdout, result.stdout + result.stderr


def _parse_summary(text: str) -> Dict[str, float]:
    """从 summarize 里抠出要看的数字。"""

    numbers: Dict[str, float] = {}
    match = re.search(_SUMMARY_PATTERNS["wins_left"], text)
    if match:
        numbers["wins_left"] = float(match.group(1))
    match = re.search(_SUMMARY_PATTERNS["actions_left"], text)
    if match:
        numbers["actions_left"] = float(match.group(1))
    match = re.search(_SUMMARY_PATTERNS["sp_left"], text)
    if match:
        numbers["sp_left"] = float(match.group(1))
    match = re.search(_SUMMARY_PATTERNS["effects_left"], text)
    if match:
        numbers["effects_left"] = float(match.group(1))
    match = re.search(_SUMMARY_PATTERNS["no_sp_games"], text)
    if match:
        numbers["no_sp_games"] = float(match.group(1))
        numbers["games"] = float(match.group(2))
    return numbers


def _count_milestones(log_text: str) -> Dict[str, int]:
    """数里程碑：关键卡的落位次数 + 从墓地跳出来的次数。"""

    hits: Dict[str, int] = {}
    for card_id, name in _MILESTONES.items():
        hits[name] = len(re.findall(rf"← #{card_id}\b", log_text))
    hits["墓地起跳"] = log_text.count(_REVIVE_HINT)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="魔女术脚本的自对弈迭代脚手架（两腿 + 判定 + 记账）")
    parser.add_argument("--deck", required=True, help="卡表 .ydk 路径")
    parser.add_argument("--style", default="WitchcraftShop", help="被迭代的出牌脚本名")
    parser.add_argument("--opponent", default="Albaz", help="外部腿的对手卡组")
    parser.add_argument("--rounds", type=int, default=10, help="每条腿的镜像轮数（每轮 2 局）")
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--baseline-wins", type=float, default=-1.0, help="外部腿基线胜局数；-1 表示这是首次测量")
    parser.add_argument("--baseline-sp", type=float, default=-1.0, help="质量腿基线特召/局；-1 表示首次")
    parser.add_argument("--note", default="", help="本次改了什么（写进迭代账本）")
    args = parser.parse_args()

    plugin_root = _plugin_root()
    log_dir = plugin_root / "temp" / "train"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")

    mirror_summary, mirror_log = _run_brain_eval(
        plugin_root=plugin_root,
        deck=args.deck,
        left_style=args.style,
        left_name="候选",
        right_style=args.style,          # 镜像腿：两边同一副牌，都该用被测脚本
        right_deck=args.deck,
        right_name="镜像",
        rounds=args.rounds,
        parallel=args.parallel,
        arena=f"iter-mirror-{stamp}",
        verbose_log=log_dir / f"iter-mirror-{stamp}.log",
    )
    external_summary, external_log = _run_brain_eval(
        plugin_root=plugin_root,
        deck=args.deck,
        left_style=args.style,
        left_name="候选",
        right_style="",                  # 外部腿：**不传**右边脚本，让对手用它自己的
        right_deck=args.opponent,
        right_name=args.opponent,
        rounds=args.rounds,
        parallel=args.parallel,
        arena=f"iter-external-{stamp}",
        verbose_log=log_dir / f"iter-external-{stamp}.log",
    )

    mirror_numbers = _parse_summary(mirror_summary)
    external_numbers = _parse_summary(external_summary)
    milestones = _count_milestones(mirror_log)

    print("\n==================== 迭代报告 ====================")
    print(f"改动：{args.note or '（未填写）'}")
    print(f"镜像腿：局数 {mirror_numbers.get('games')}｜特召/局 {mirror_numbers.get('sp_left')}｜"
          f"效果/局 {mirror_numbers.get('effects_left')}｜动作/局 {mirror_numbers.get('actions_left')}｜"
          f"零特召局 {mirror_numbers.get('no_sp_games')}")
    print(f"外部腿：对手 {args.opponent}｜胜 {external_numbers.get('wins_left')}"
          f"/{external_numbers.get('games')}｜特召/局 {external_numbers.get('sp_left')}")
    print(f"里程碑（镜像腿）：{milestones}")

    verdict = "首次测量（无基线可比）"
    if args.baseline_wins >= 0 and args.baseline_sp >= 0:
        wins_ok = external_numbers.get("wins_left", 0) >= args.baseline_wins - 8 * (20 / 100)
        sp_ok = (mirror_numbers.get("sp_left", 0) - args.baseline_sp) >= 0.3
        if wins_ok and sp_ok:
            verdict = "接受"
        elif not wins_ok:
            verdict = "回退（外部腿掉太多）"
        else:
            verdict = "保留（质量没明显提升）"
    print(f"判定：{verdict}")
    print("=================================================\n")

    record = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": args.note,
        "style": args.style,
        "rounds": args.rounds,
        "mirror": mirror_numbers,
        "external": external_numbers,
        "milestones": milestones,
        "verdict": verdict,
    }
    ledger = log_dir / "iterations.jsonl"
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"已记入账本：{ledger}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
