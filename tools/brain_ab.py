"""镜像 A/B：同一副牌对打，比较"用阻抗决策层 / 不用"的胜率。

**为什么要专门写这个工具，而不是随手跑几局看谁赢**：
1. **只认镜像腿**——两边的牌组、出牌脚本、exe 完全一样，唯一变量是"某一边有没有开决策层"；
   否则赢了也说不清是决策层的功劳还是牌组/脚本的差距。
2. **必须能证伪**——每一局都要确认"开了的那一边真的问了模型"（`asks>0`），
   否则测出来的可能是"决策层其实没触发"的假零差分（实测：只开目标选择那一半时，
   真机里一次都不会触发，整个 A/B 会得到一个毫无意义的 0 差分）。
3. **座位要来回换**——决策层这一边在"闸门位"与"人类位"之间交替，避免把座位/先后手
   的偏差算进结论。

**它不解决的问题**：样本量。本机这类镜像对局的噪声很大（同一构筑重测都能出现
0/60 与 8/20 并存），`n=10~20/腿` 只能当**飞行员批次**看趋势，**不足以下结论**；
项目自己的纪律是 ≥80 局/腿，而且要在**同一次**运行里跑完（中间别换 exe、别换模型）。
工具会在结尾把置信区间一并打出来，别只看那个百分比。

用法::

    python plugins/mai-play-ygo/tools/brain_ab.py --duels 8 --deck-style Test --gate
    python plugins/mai-play-ygo/tools/brain_ab.py --duels 80 --deck-style TraptrixRagnaraika

``--gate`` 打开"要不要交"那一半（默认关）。**不打开它，决策层在实战里几乎不动作**
（目标选择那一半只在"链上没有可无效的怪物"时才问，真机实测一次都没触发），
所以做 A/B 必须带 ``--gate``，否则量的是自己的空转。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import argparse
import asyncio
import logging
import math
import sys
import tempfile
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MAIBOT_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _MAIBOT_ROOT.parent.parent
for _extra in (
    _PLUGIN_ROOT,
    _MAIBOT_ROOT,
    _INSTALL_ROOT / "python-overrides",
    _INSTALL_ROOT / "python-env" / "Lib" / "site-packages",
):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from duel.brain_bridge import BrainBridge  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.cards import CardDatabase  # noqa: E402
from duel.room import RoomSettings, WindBotProcess, WindBotSettings  # noqa: E402
from duel.session import DuelSession, SessionConfig  # noqa: E402

#: 合成卡组：一手阻抗 + 一部分大怪撑起回合推进。用它是因为**它短**——
#: 真实卡组一副要 4~10 分钟（历史日志里俱舍 239s、闪刀 629s），一局太慢就永远攒不到样本；
#: 这副 20 张青眼白龙能两三回合结束，而 15 张效果遮蒙者保证每局都有大量阻抗时点。
VEILER_ID = 97268402
IMPERM_ID = 10045474
BLUE_EYES_ID = 89631139


def _write_deck(path: Path, veiler: int = 15, imperm: int = 5, beater: int = 20) -> Path:
    """写一份确定性的合成卡组（`no_check_deck=true`，重复张数不受限）。"""

    main = [VEILER_ID] * veiler + [IMPERM_ID] * imperm + [BLUE_EYES_ID] * beater
    path.write_text("#main\n" + "\n".join(str(c) for c in main) + "\n#extra\n!side\n", encoding="utf-8")
    return path


def _resolve_windbot_exe(paths: Path) -> Path:
    """取插件实际会对局用的那份 exe（与 `brain_channel_check` 同一口径）。"""

    import tomllib

    configured = ""
    config_path = _PLUGIN_ROOT / "config.toml"
    if config_path.is_file():
        with config_path.open("rb") as handle:
            configured = str(tomllib.load(handle).get("paths", {}).get("windbot_src_dir", "") or "")
    if configured:
        source_root = Path(configured)
        if not source_root.is_absolute():
            source_root = (_PLUGIN_ROOT / source_root).resolve()
        built = source_root / "bin" / "Release" / "WindBot.exe"
        if built.is_file():
            return built
    bundled = paths / "windbot" / "WindBot.exe"
    if bundled.is_file():
        return bundled
    raise SystemExit("找不到 WindBot.exe：请先按 executors/README.md 编译")


@dataclass
class DuelOutcome:
    """一局的结论。**每个字段都是后面算结论时要用的，缺一个都会让数字说不清。**"""

    index: int
    brain_seat: str
    """决策层在哪一边（`bot` 或 `opponent`）。"""
    winner: str
    """`brain` / `control` / `draw` / `unknown`。"""
    turns: Optional[int] = None
    seconds: float = 0.0
    asks: int = 0
    answered: int = 0
    cached: int = 0
    failed: int = 0
    timed_out: int = 0
    kinds: str = ""
    brain_effects: Optional[int] = None
    """决策层那一侧的"发动过几次效果"（`effects`）——**机制读数**：
    如果决策层真在否决，这一侧的交牌次数应当明显少于脚本侧。"""
    control_effects: Optional[int] = None
    brain_side_name: str = ""
    control_side_name: str = ""
    """两边的昵称——**用来独立核对"哪一边是决策层"**。

    不能只信 `winner_is_self`：那个字段依赖记录器把"我方座位"认对（内核会重排座位，
    记录器为此专门留了两套编号）。给某一边起名 `A/B-bot`、另一边 `A/B-opponent` 之后，
    就能从结果里读出"决策层这一侧的名字是不是我以为的那个"——**这个错一旦发生，
    胜率会整体翻转**（62.7% 会变成 37.3%），是这套 A/B 最危险的单点。"""

    @property
    def mapping_ok(self) -> bool:
        """座位映射核对通过：决策层那一侧的昵称与我指定的一致。"""

        return self.brain_side_name == ("A/B-bot" if self.brain_seat == "bot" else "A/B-opponent")

    @property
    def valid(self) -> bool:
        """这一局能不能算进胜率（胜负没判出来的不算）。"""

        return self.winner in {"brain", "control", "draw"}

    @property
    def differential_exercised(self) -> bool:
        """这一局的"差分"有没有真的被跑出来（决策层至少问过一次模型）。

        没跑出来的局**不能算进结论**：那等于"开了但没动作"，测的是空转——
        实测踩过：只开目标选择那一半时，真机一局都不会触发，整批会得到一个假的 0 差分。
        """

        return self.asks > 0


@dataclass
class Tally:
    """汇总。分开记"决策层那一侧"与"脚本那一侧"，因为座位会交替。"""

    outcomes: List[DuelOutcome] = field(default_factory=list)

    def add(self, outcome: DuelOutcome) -> None:
        self.outcomes.append(outcome)

    def summary(self) -> dict:
        """算出两边胜负与置信区间。"""

        brain = sum(1 for o in self.outcomes if o.winner == "brain")
        control = sum(1 for o in self.outcomes if o.winner == "control")
        draws = sum(1 for o in self.outcomes if o.winner == "draw")
        decided = brain + control
        rate = (brain / decided) if decided else float("nan")
        low, high = self._wilson(brain, decided)
        exercised = sum(1 for o in self.outcomes if o.differential_exercised)
        mapping_bad = sum(1 for o in self.outcomes if o.brain_side_name and not o.mapping_ok)
        brain_effects = [o.brain_effects for o in self.outcomes if o.brain_effects is not None]
        control_effects = [o.control_effects for o in self.outcomes if o.control_effects is not None]
        return {
            "局数": len(self.outcomes),
            "算进胜率": decided,
            "平局": draws,
            "决策层赢": brain,
            "脚本赢": control,
            "决策层胜率": rate,
            "95%区间": (low, high),
            "**座位映射核对**": f"{len(self.outcomes) - mapping_bad}/{len(self.outcomes)} 局一致"
            + ("（✅ 可信）" if mapping_bad == 0 else "（❌ 有反的局，胜率数字不可用）"),
            "差分真的跑出来的局数": f"{exercised}/{len(self.outcomes)}",
            "决策层那侧平均发动次数": (sum(brain_effects) / len(brain_effects)) if brain_effects else None,
            "脚本那侧平均发动次数": (sum(control_effects) / len(control_effects)) if control_effects else None,
            "总提问": sum(o.asks for o in self.outcomes),
            "总被答复": sum(o.answered for o in self.outcomes),
            "缓存命中": sum(o.cached for o in self.outcomes),
            "失败": sum(o.failed for o in self.outcomes),
            "超时": sum(o.timed_out for o in self.outcomes),
        }

    @staticmethod
    def _wilson(successes: int, total: int) -> tuple:
        """Wilson 得分区间（95%）。

        为什么不用"正态近似 ±1.96·√(p(1-p)/n)"：**极端比例下它直接失效**——
        p=0 或 p=1 时它给出"0.0% ~ 0.0%"这种零宽度区间，看起来像"结论非常确定"，
        实际是"样本太少、什么都说明不了"。Wilson 在 p=0/1 时仍给出真实的宽度。
        """

        if total <= 0:
            return (float("nan"), float("nan"))
        z = 1.96
        p = successes / total
        denom = 1 + z * z / total
        center = (p + z * z / (2 * total)) / denom
        half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
        return (max(0.0, center - half), min(1.0, center + half))


async def _run_one(
    *,
    index: int,
    brain_seat: str,
    deck_style: str,
    deck_path: Path,
    paths: Path,
    windbot_exe: Path,
    db: CardDatabase,
    logger: logging.Logger,
    real_model: str,
    gate: bool,
    baseline: bool,
    style: str,
    max_duration: float,
    join_timeout: float,
) -> DuelOutcome:
    """跑一局：`brain_seat` 决定决策层给哪一边。"""

    tmp = Path(tempfile.mkdtemp(prefix=f"brain_ab_{index:03d}_"))
    prefix = tmp / "room"
    timings: List[float] = []
    async def generate(prompt: str) -> Optional[str]:
        from src.services import llm_service

        started = time.perf_counter()
        try:
            result = await llm_service.generate(
                llm_service.LLMServiceRequest(
                    task_name="utils", request_type="brain.ab", prompt=prompt,
                    model_name=real_model, temperature=0.0, max_tokens=256,
                )
            )
        finally:
            timings.append(time.perf_counter() - started)
        payload = result.to_capability_payload() if hasattr(result, "to_capability_payload") else dict(result)
        if not payload.get("success"):
            return None
        text = str(payload.get("response") or "").strip()
        return text or None

    bridge = BrainBridge(
        prefix=prefix, generate=generate, card_db=db, logger=logger, timeout=2.1,
        style=style,   # 决策档位（保守/平常/激进）——A/B 时这是**自变量**，不是提示词细节
    )
    bridge_task = asyncio.create_task(bridge.run())
    brain_on_bot = brain_seat == "bot"
    # baseline 模式：**两边都不给 BrainFile**（连问答文件都不存在），量的是"这套镜像对局自身的
    # 空分布"——如果基线不是 ~50%，那说明座位/先后手本身带偏，前面那个胜率就不能直接读。
    bot_brain = None if baseline else (prefix if brain_on_bot else None)
    opponent_brain = None if baseline else (prefix if not brain_on_bot else None)

    config = SessionConfig(
        ygopro_executable=paths / "ygopro" / "ygopro.exe",
        ygopro_dir=paths / "ygopro",
        windbot_executable=windbot_exe,
        windbot_dir=paths / "windbot",
        cards_cdb=paths / "ygopro" / "cards.cdb",
        room=RoomSettings(save_replay=False),
        bot_name="A/B-bot",
        windbot_deck=deck_style,
        bot_deck_file=deck_path,
        brain_file=bot_brain,
        brain_target_choice=True,
        brain_negate_gate=gate,
        brain_timeout_ms=2500,
        listen_port=0,
        join_timeout=join_timeout,
        max_duration=max_duration,
    )
    session = DuelSession(config, group_id="brain-ab", card_db=db, logger=logger)
    opponent: Optional[WindBotProcess] = None
    starts = time.perf_counter()
    winner = "unknown"
    turns: Optional[int] = None
    outcome = "未开始"
    try:
        info = await session.start()
        opponent = WindBotProcess(
            config.windbot_executable,
            config.windbot_dir,
            WindBotSettings(
                name="A/B-opponent",
                deck=deck_style,
                deck_file=deck_path,
                password=info.password,
                db_path=config.cards_cdb,
                brain_file=opponent_brain,
                brain_target_choice=True,
                brain_negate_gate=gate,
                brain_timeout_ms=2500,
            ),
            logger=logger,
        )
        await opponent.start("127.0.0.1", info.port)
        outcome = await asyncio.wait_for(session.wait_finished(), timeout=max_duration + 20)
    except asyncio.TimeoutError:
        outcome = "超时收摊"
    except Exception as exc:  # noqa: BLE001  单局出错不能让整批停下
        outcome = f"出错：{type(exc).__name__}: {exc}"
    finally:
        if opponent is not None:
            await opponent.stop()
        data = session.result_dict() if outcome != "未开始" else {}
        winner_is_self = data.get("winner_is_self")
        turns = data.get("turns") if isinstance(data.get("turns"), int) else None
        if winner_is_self is True:
            winner = "brain" if brain_on_bot else "control"
        elif winner_is_self is False:
            winner = "control" if brain_on_bot else "brain"
        elif outcome == "finished":
            winner = "draw"
        # 机制读数：决策层那一侧 vs 脚本那一侧的"发动次数"。
        # 决策层若真在否决，这一侧的交牌次数就会明显更少——这是"它到底动没动手"的直接证据，
        # 比胜率可靠得多（胜率被噪声淹，这个是每次否决都会留下的痕迹）。
        brain_effects = control_effects = None
        brain_side_name = control_side_name = ""
        players = data.get("players") if isinstance(data.get("players"), dict) else None
        self_seat = data.get("self_seat")
        if players and self_seat in (0, 1):
            brain_seat_index = self_seat if brain_on_bot else 1 - self_seat
            control_seat_index = 1 - brain_seat_index
            brain_side = players.get(str(brain_seat_index)) or {}
            control_side = players.get(str(control_seat_index)) or {}
            brain_effects = brain_side.get("effects")
            control_effects = control_side.get("effects")
            brain_side_name = str(brain_side.get("name") or "")
            control_side_name = str(control_side.get("name") or "")
        await session.stop()
        bridge.stop()
        bridge_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass

    kinds = ", ".join(f"{k}×{v}" for k, v in sorted(bridge.stats.by_kind.items())) or "（没问过）"
    return DuelOutcome(
        index=index,
        brain_seat=brain_seat,
        winner=winner,
        turns=turns,
        seconds=time.perf_counter() - starts,
        asks=bridge.stats.asked,
        answered=bridge.stats.answered,
        cached=bridge.stats.cached,
        failed=bridge.stats.failed,
        timed_out=bridge.stats.timed_out,
        kinds=kinds,
        brain_effects=brain_effects,
        control_effects=control_effects,
        brain_side_name=brain_side_name,
        control_side_name=control_side_name,
    )


async def run(args: argparse.Namespace) -> int:
    logger = logging.getLogger("brain_ab")
    logger.setLevel(logging.WARNING)  # 每局的 WindBot 输出太吵，只看汇总
    logger.addHandler(logging.StreamHandler(sys.stdout))

    paths = _PLUGIN_ROOT / "clients"
    windbot_exe = _resolve_windbot_exe(paths)
    db = CardDatabase(paths / "ygopro" / "cards.cdb")
    tmp_root = Path(tempfile.mkdtemp(prefix="brain_ab_deck_"))
    if args.deck_file:
        # 同上：`DeckFile=` 按 WindBot 的工作目录解析，相对路径会让 Deck 加载不出来
        deck_path = Path(args.deck_file).resolve()
    else:
        deck_path = _write_deck(tmp_root / "mirror.ydk", veiler=args.veiler, imperm=args.imperm)
    print(f"被测 exe：{windbot_exe}")
    print(f"镜像卡组：{deck_path}（脚本 {args.deck_style}，两边完全一样）")
    if args.baseline:
        print("模式：**基线**（两边都不开决策层，量这套镜像对局的空分布，期望 ≈50%）")
    else:
        print(f"决策层那一侧：闸门={'开' if args.gate else '关'}｜模型={args.real_model}｜档位={args.style}")
    print(f"计划 {args.duels} 局，逐局交替座位……\n")

    # **先预热**：首次调用要多付约 1.2 秒（宿主懒加载配置 + 建连），而 A/B 的每一局都很短，
    # 冷启动会把那一局的第一次（常常也是唯一一次）决策直接撞掉——校准时就撞掉过。
    # baseline 模式不调模型，跳过。
    try:
        if args.baseline:
            raise RuntimeError("baseline 模式不预热（两边都不开决策层）")
        from src.services import llm_service

        warm_started = time.perf_counter()
        await llm_service.generate(
            llm_service.LLMServiceRequest(
                task_name="utils", request_type="brain.ab.warmup",
                prompt="回复两个字：就绪", model_name=args.real_model,
                temperature=0.0, max_tokens=8,
            )
        )
        print(f"决策层预热完成：{time.perf_counter() - warm_started:.2f}s\n")
    except Exception as exc:  # noqa: BLE001  预热失败就照跑，正式提问会自己熔断
        print(f"（预热跳过/失败：{exc}）\n")

    tally = Tally()
    deadline = time.monotonic() + args.max_minutes * 60
    for index in range(args.duels):
        if time.monotonic() > deadline:
            print(f"（到时间上限 {args.max_minutes} 分钟，提前收工；已跑 {len(tally.outcomes)} 局）")
            break
        brain_seat = "bot" if index % 2 == 0 else "opponent"
        outcome = await _run_one(
            index=index, brain_seat=brain_seat, deck_style=args.deck_style, deck_path=deck_path,
            paths=paths, windbot_exe=windbot_exe, db=db, logger=logger,
            real_model=args.real_model, gate=args.gate, baseline=args.baseline, style=args.style,
            max_duration=args.max_duel_seconds, join_timeout=args.join_timeout,
        )
        tally.add(outcome)
        print(
            f"[{index + 1}/{args.duels}] 决策层在 {outcome.brain_seat:8s}｜"
            f"胜者={outcome.winner:7s}｜回合={outcome.turns}｜{outcome.seconds:5.0f}s｜"
            f"发动 决策层{outcome.brain_effects}/脚本{outcome.control_effects}｜"
            f"问 {outcome.asks}（答 {outcome.answered}，缓存 {outcome.cached}，"
            f"失败 {outcome.failed}，超时 {outcome.timed_out}）"
        )

    db.close()
    summary = tally.summary()
    print("\n================ 汇总 ================")
    for key, value in summary.items():
        if key == "决策层胜率" and isinstance(value, float) and not math.isnan(value):
            print(f"{key}：{value:.1%}")
        elif key == "95%区间" and isinstance(value, tuple) and not math.isnan(value[0]):
            print(f"{key}：{value[0]:.1%} ~ {value[1]:.1%}")
        elif isinstance(value, float):
            print(f"{key}：{value:.2f}")
        else:
            print(f"{key}：{value}")

    decided = summary["算进胜率"]
    low, high = summary["95%区间"]
    if decided and not (low <= 0.5 <= high):
        print(f"\n→ 这一批的 95% 区间（{low:.1%}~{high:.1%}）**没有跨过 50%**，"
              f"方向上偏向一边；但仍然要看下面的样本量提醒。")
    elif decided:
        print(f"\n→ 95% 区间（{low:.1%}~{high:.1%}）跨过 50%：**这一批分不出高下**。")
    if decided < 80:
        print(f"⚠ 只有 {decided} 局有效样本——**这是飞行员批次，不是结论**：本机镜像对局噪声很大"
              f"（同构筑重测都能出现 0/60 与 8/20 并存），项目纪律是 ≥80 局/腿，"
              f"且要在同一次运行里跑完（别中途换 exe、换模型）。")
    exercised = summary["差分真的跑出来的局数"]
    if args.baseline:
        print("（baseline：两边都不开决策层，'差分'本来就该是 0——这一批只看座位/先手有没有带偏："
              "胜率应当落在 50% 附近。）")
    elif exercised.startswith("0/") or exercised.startswith("1/"):
        print(f"⚠ 差分只在 {exercised} 局里真的跑出来过——决策层基本没动过手，"
              f"这个 0 差分是假的：先确认 `--gate` 与模型可用。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="阻抗决策层镜像 A/B")
    parser.add_argument("--duels", type=int, default=8, help="总对局数（座位逐局交替）")
    parser.add_argument("--deck-style", default="Test", help="两边共用的 WindBot 脚本名")
    parser.add_argument("--deck-file", default="", help="两边共用的 .ydk；留空用合成卡组")
    parser.add_argument("--veiler", type=int, default=15, help="合成卡组里效果遮蒙者的张数")
    parser.add_argument("--imperm", type=int, default=5, help="合成卡组里无限泡影的张数")
    parser.add_argument("--gate", action="store_true", help="打开'要不要交'那一半（A/B 必开）")
    parser.add_argument(
        "--style", default="normal",
        help="决策档位：conservative / normal / aggressive（也认 保守/平常/激进）。"
             "同一份口径在不同牌组上一正一负，所以档位是 A/B 的自变量之一",
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="基线：两边都不开决策层。**先跑它**——基线不是 ~50% 就说明座位/先手带偏，胜率不能直接读",
    )
    parser.add_argument("--real-model", default="deepseek-flash", help="决策层用的模型（要关思考的那种）")
    parser.add_argument("--max-duel-seconds", type=float, default=240.0, help="单局最长秒数")
    parser.add_argument("--join-timeout", type=float, default=30.0, help="等对手进房的秒数")
    parser.add_argument("--max-minutes", type=float, default=50.0, help="整批的时间上限")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
