"""量一次"阻抗决策层提示词"在各候选模型上的单次延迟。

为什么要有这个工具：阻抗决策层的可行性**完全押在单次等待上**——对手回合的等待预算只有
15 秒（WindBot 的 `BrainOppBudgetMs`），而一次"会思考"的答复要十几秒（实测 `ds` 是 7039 字
思考 / 17 秒 / `response` 为空，当年还有 40 局里 5 局因响应窗口等太久被内核判超时输掉）。
所以"哪只模型能用"这件事必须量，不能猜；量完把结果写进插件配置的 `duel.brain_model`。

它做的事：读宿主的模型配置，按 `brain_bridge.py` 里 `disable_target` 问题的同构提示词
（长度、格式、"只回复序号"的口径都一致）各发**一次**请求，报出延迟、是否拿到正文、
以及思考字段的长度（判断是不是"思考型"的关键）。只读配置、只发请求，不改任何文件。

用法（在 MaiBot 根目录跑，用宿主自己的 Python 环境）::

    python plugins/mai-play-ygo/tools/brain_model_probe.py                 # 量内置候选表
    python plugins/mai-play-ygo/tools/brain_model_probe.py ds:2048 火山mini3:1024

``模型名:max_tokens`` 里的额度可省，默认 256（与 `brain_max_tokens` 默认值一致）。
判读口径：
* **能用** → `ok=true`、延迟 ≲ 2 秒、`reasoning_len` 为 0（或极小）；
* **思考型**（不可用）→ `ok=false` 且 `reasoning_len` 很大、`answer` 为空 —— 这类模型
  把额度花在思考上，"把 max_tokens 调大"解决不了（只是让它思考更久）；
* **provider 侧问题** → 1~2 秒就返回"欠费/认证失败/负载过高/超时"，与模型无关。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import argparse
import asyncio
import json
import sys
import time

# 宿主根目录：tools/ -> 插件根 -> plugins/ -> MaiBot 根
_MAIBOT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_INSTALL_ROOT = _MAIBOT_ROOT.parent.parent
for _extra in (
    _MAIBOT_ROOT,
    _INSTALL_ROOT / "python-overrides",
    _INSTALL_ROOT / "python-env" / "Lib" / "site-packages",
):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))


# 与 duel/brain_bridge.py 里 disable_target 问题**同构**的提示词：长度、字段格式、
# "只回复一个序号"的收口都一致——这样量出来的延迟才有代表性。
PROMPT = """你是游戏王对局的决策助手，只负责一件事：我现在要发动一张「无效」卡，
它该指向对手场上的哪一只怪兽。

判断口径：
1. 选「接下来最可能靠自身效果带来优势/压制」的那一只——断掉它收益最大；
2. 卡文里写着「效果不会被无效 / 不受效果影响 / 不能成为效果对象」的那一只不要选,
   选了也是白扔一张牌；
3. 都差不多时，选攻击力更高、或是永续压制类（贴纸）的那一只；
4. 只输出序号（从 1 开始的整数），不要输出任何其它字。

【当前局面】
- 回合数：3
- 是否我方回合：0
- 阶段码：8
- 我方生命值：6000
- 对手生命值：8000
- 我方能打出的总伤害：0
- 我方场上怪兽：（空）
- 我方魔陷区：（空）
- 对手魔陷区：无限泡影(攻0/守0)

【我准备发动的卡】
- 效果遮蒙者（怪兽 调整，攻0/守0）：以对方场上 1 只效果怪兽为对象才能发动。那只怪兽的效果直到回合结束时无效。

【连锁上正在发生的】
- 白龙之落胤（对手，卡号 89900123）
- 白龙之落胤（怪兽 效果）：这张卡在场上发动的场合，以自己墓地 1 只怪兽为对象才能发动。那只怪兽特殊召唤。

【候选（只能从这些里选）】
1. 黑森林的魔女（卡号 78010363，攻1100/守1500）
2. 访问码语者（卡号 86066372，攻3000/守2500）

只回复一个序号（1-2）。"""

#: 默认候选：本机 model_config.toml 里那些"看起来该快"的（2026-10-08 实测几乎全不可用，
#: 详见 executors/README.md §3.5；这份表留着是为了换了 provider 之后能一键再量一遍）。
DEFAULT_CANDIDATES = [
    "ds",                 # ark-code-latest @ 火山（插件的 utils 任务就是它）
    "glm5.3flash",        # glm-5.3-flash @ 火山
    "glm-4.7-flash",      # @ ZhipuAI
    "deepseekV4.1flash",  # deepseek-flash @ DeepSeek
    "火山mini3",           # minimax-m3 @ 火山
    "DMXglm1",            # spark-lite-free @ DMX1
]

DEFAULT_TIMEOUT_S = 40.0


async def probe(spec: str, timeout: float) -> Dict[str, Any]:
    """量一只模型：返回延迟、答复、以及思考字段长度。"""

    from src.services import llm_service

    model, _, tokens_text = spec.partition(":")
    max_tokens = int(tokens_text) if tokens_text else 256

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            llm_service.generate(
                llm_service.LLMServiceRequest(
                    task_name="utils",  # 与插件默认走的任务一致
                    request_type="probe.brain",
                    prompt=PROMPT,
                    model_name=model,
                    temperature=0.0,
                    max_tokens=max_tokens,
                )
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"model": spec, "seconds": None, "ok": False, "note": f"本地超时 {timeout:g}s"}
    except Exception as exc:  # noqa: BLE001  探针要把失败也如实报出来
        return {"model": spec, "seconds": None, "ok": False, "note": f"{type(exc).__name__}: {exc}"}

    elapsed = time.perf_counter() - started
    if hasattr(result, "to_capability_payload"):
        payload = result.to_capability_payload()
    elif hasattr(result, "model_dump"):
        payload = result.model_dump()
    else:
        payload = {"success": getattr(result, "success", False), "response": getattr(result, "response", "")}
    text = str(payload.get("response") or "").strip()
    success = bool(payload.get("success", False))
    return {
        "model": spec,
        "seconds": round(elapsed, 2),
        "ok": success and bool(text),
        "answer": text[:60],
        "reasoning_len": len(str(payload.get("reasoning") or "").strip()),
        "note": "" if success else str(payload.get("error") or "")[:120],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="量阻抗决策层提示词在各模型上的单次延迟")
    parser.add_argument("models", nargs="*", help="模型名 或 模型名:max_tokens；留空用内置候选表")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="单次本地超时（秒）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON（给脚本用）")
    args = parser.parse_args()

    specs: List[str] = args.models or list(DEFAULT_CANDIDATES)
    rows: List[Dict[str, Any]] = []
    for spec in specs:
        row = await probe(spec, args.timeout)
        rows.append(row)
        if not args.json:
            seconds = f"{row['seconds']:6.2f}s" if row.get("seconds") is not None else "   --   "
            print(
                f"{spec:22s} {seconds}  ok={str(row['ok']):5s} "
                f"思考={row['reasoning_len']:>5d}字  {row.get('answer') or row.get('note', '')}"
            )

    usable = [row["model"] for row in rows if row.get("ok") and row.get("seconds", 99) <= 3.0]
    if not args.json:
        print()
        if usable:
            print(f"可用（延迟 ≤3s 且有正文）：{'、'.join(usable)}")
            print("→ 把它写进插件配置 [duel] 的 brain_model，再打开 brain_enabled")
        else:
            print("没有任何模型可用：阻抗决策层先保持关闭（brain_enabled 默认就是关）。")
            print("判读：ok=false 且「思考=N字」很大 → 思考型；1~2 秒就报错 → provider 侧问题。")
        print("\n汇总（JSON）：")
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(rows, ensure_ascii=False))
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
