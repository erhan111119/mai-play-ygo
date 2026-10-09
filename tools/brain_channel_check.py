"""阻抗决策层的**真机链路自测**：起一个真实房间，验证"问题真的能走一个来回"。

为什么单测不够：`tests/test_brain_bridge.py` 用的是**手写的问题样本**，它证明不了两端的契约——
WindBot(C#) 写的问题字段名、路径前后缀、答复语法，只要有一处对不上，单测全绿而真机一次都不问。
2026-10-08 就是靠这个工具抓到真 bug 的（闸门问题走 C# 原有的 `AppendContext`，区域行是**五段**，
bridge 按四段解包 → 组装提示词时抛 ValueError → 现象是"问题文件写出来了、模型一次都没被问到"）。

做法：ygopro 内核 + 闸门 + **两个 WindBot**（被测的那个用 `Deck=Test` 通用脚本 + 一手指抗卡组，
另一个拿房间口令当"人类"进来对打），答复端换成**秒答的假模型**——
这样测的是通道本身，**不受本机模型好不好用影响**（本机模型实测全是思考型/不可用，
见 `executors/README.md` §3.5 与 `tools/brain_model_probe.py`）。

用法::

    python plugins/mai-play-ygo/tools/brain_channel_check.py

退出码 0 表示"至少真的问过并且答上了"。它不写录像、不留文件（临时目录里自清理）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import argparse
import asyncio
import logging
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

#: 被测 bot 用通用脚本（`Deck=Test`）：它只有一条 `Activate` 兜底规则 `DefaultDontChainMyself`，
#: 对手连锁时一律说 yes，所以手里有可发动的阻抗卡时几乎每个对手动作都是一个时点。
BOT_DECK_STYLE = "Test"
#: 对手用一副会正常展开的卡组，好让被测 bot 有响应窗口
OPPONENT_DECK_STYLE = "KillerTune"

#: 卡号（写错会变成"这一局没触发"，所以在这里点名）
VEILER_ID = 97268402       # 效果遮蒙者：手卡发动、以对面怪为对象
IMPERM_ID = 10045474       # 无限泡影
BLUE_EYES_ID = 89631139    # 青眼白龙：凑数支撑回合推进


class _Collector(logging.Handler):
    """把日志收进内存，最后统一挑出要看的行。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def grep(self, needle: str) -> List[str]:
        return [line for line in self.lines if needle in line]


def _resolve_windbot_exe(paths: Path) -> Path:
    """取**插件实际会对局用的那份** exe。

    规则与插件一致（`plugin._resolve_windbot_executable`）：配了 `paths.windbot_src_dir`
    且那份 `bin/Release/WindBot.exe` 存在 → 用编译产物；否则用配置里那一条（自带副本）。
    **用错那一份会测到旧版本**——实测踩过：自带副本落后一版，现象是"改了没反应"。

    Raises:
        SystemExit: 两份都找不到时（先把 WindBot 编译出来，见 `executors/README.md`）。
    """

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
    raise SystemExit("找不到 WindBot.exe：请先按 executors/README.md 编译，或检查 paths.windbot_executable")


async def _answer_instantly(prompt: str) -> str:
    """假模型：秒答，且答案一定在合法范围内。

    目标问题回答第 1 项（候选是内核顺序给的，第 1 项一定存在）；闸门问题回答 yes。
    这样一来"通道通不通"与"模型强不强"就分开了。
    """

    await asyncio.sleep(0.01)
    return "yes" if "只回复 yes" in prompt else "1"


def _make_real_generate(model_name: str, timings: List[float]):
    """造一个**真的** generate：走宿主的 LLM 服务（与插件运行时同一条路）。

    为什么要留这个入口：假模型只能证明"通道通"，证不了"配上真模型之后一局要等多久"。
    实测 `deepseek-flash`（关思考）在决策层提示词下约 2 秒，这里量的是**含文件往返**的端到端延迟。
    """

    async def generate(prompt: str) -> Optional[str]:
        from src.services import llm_service

        started = time.perf_counter()
        try:
            result = await llm_service.generate(
                llm_service.LLMServiceRequest(
                    task_name="utils",
                    request_type="brain.channel-check",
                    prompt=prompt,
                    model_name=model_name,
                    temperature=0.0,
                    max_tokens=256,
                )
            )
        finally:
            timings.append(time.perf_counter() - started)
        payload = result.to_capability_payload() if hasattr(result, "to_capability_payload") else dict(result)
        if not payload.get("success"):
            return None
        text = str(payload.get("response") or "").strip()
        return text or None

    return generate


def _write_probe_deck(path: Path) -> Path:
    """写一份"一手阻抗"的确定性卡组（`no_check_deck=true`，重复张数不受限）。"""

    main = [VEILER_ID] * 15 + [IMPERM_ID] * 5 + [BLUE_EYES_ID] * 20
    path.write_text("#main\n" + "\n".join(str(card) for card in main) + "\n#extra\n!side\n", encoding="utf-8")
    return path


async def run(
    *, max_duration: float = 240.0, join_timeout: float = 60.0, real_model: str = "",
    brain_timeout_ms: int = 2500,
) -> int:
    """跑一局自测；返回进程退出码。

    Args:
        max_duration: 一局最长秒数。
        join_timeout: 等"人类"对手进房的秒数。
        real_model: 给了就用**真的模型**（走宿主 LLM 服务）而不是秒答的假模型——
            这样量出来的是含文件往返的端到端延迟。
        brain_timeout_ms: WindBot 等答复的上限。工具默认 2500ms（**紧张窗口**，专门用来
            暴露"答复太慢"）；插件实际用的是 `llm.decision_timeout_ms`——本机把决策模型换成
            `deepseek-flash`（关思考）之后单次实测约 2 秒，那一档已经放宽到 4000。
            要按插件的口径量，用 `--brain-timeout-ms` 传成配置里的值。
    """

    logger = logging.getLogger("brain_channel")
    logger.setLevel(logging.INFO)
    collector = _Collector()
    logger.addHandler(collector)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    tmp = Path(tempfile.mkdtemp(prefix="brain_channel_"))
    prefix = tmp / "room"
    paths = _PLUGIN_ROOT / "clients"
    windbot_exe = _resolve_windbot_exe(paths)
    logger.info("被测 exe：%s", windbot_exe)
    deck_path = _write_probe_deck(tmp / "bot.ydk")
    db = CardDatabase(paths / "ygopro" / "cards.cdb")

    prompts: List[str] = []
    timings: List[float] = []

    async def stub_generate(prompt: str) -> str:
        prompts.append(prompt)
        return await _answer_instantly(prompt)

    if real_model:
        logger.info("答复端用**真模型**：%s", real_model)
        inner = _make_real_generate(real_model, timings)

        async def generate(prompt: str) -> Optional[str]:
            prompts.append(prompt)
            return await inner(prompt)

        # 预热一次：首次调用要多付约 1.2 秒（宿主懒加载配置 + 建连），不预热的话
        # 一局的第一次提问必然撞上 WindBot 的 2.5 秒上限——那测出来的是冷启动，不是常态。
        # 插件运行时也是这么做的（`_warmup_brain`），所以这里对齐它，量的才是同一件事。
        warm = await inner("回复两个字：就绪")
        logger.info(
            "预热完成：%.2fs（正式提问的延迟从下一行开始计）｜预热答复=%r", timings[-1], warm
        )
    else:
        generate = stub_generate  # type: ignore[assignment]

    bridge = BrainBridge(prefix=prefix, generate=generate, card_db=db, logger=logger, timeout=2.0)
    bridge_task = asyncio.create_task(bridge.run())

    config = SessionConfig(
        ygopro_executable=paths / "ygopro" / "ygopro.exe",
        ygopro_dir=paths / "ygopro",
        windbot_executable=windbot_exe,
        windbot_dir=paths / "windbot",
        cards_cdb=paths / "ygopro" / "cards.cdb",
        room=RoomSettings(save_replay=False),   # 自测不写录像（否则会污染真实录像目录）
        bot_name="决策层自测",
        windbot_deck=BOT_DECK_STYLE,
        bot_deck_file=deck_path,
        bot_debug=True,
        brain_file=prefix,
        brain_target_choice=True,
        brain_negate_gate=True,                 # 两半都开，尽量多产生一次问答
        brain_timeout_ms=2500,
        listen_port=0,
        join_timeout=join_timeout,
        max_duration=max_duration,
    )
    session = DuelSession(config, group_id="channel-check", card_db=db, logger=logger)
    opponent: Optional[WindBotProcess] = None
    outcome = "未开始"
    try:
        info = await session.start()
        logger.info("房间已开：端口 %s", info.port)
        opponent = WindBotProcess(
            config.windbot_executable,
            config.windbot_dir,
            WindBotSettings(
                name="对手",
                deck=OPPONENT_DECK_STYLE,
                deck_file=_PLUGIN_ROOT / "decks" / f"{OPPONENT_DECK_STYLE}.ydk",
                password=info.password,
                db_path=config.cards_cdb,
                debug=True,
            ),
            logger=logger,
        )
        await opponent.start("127.0.0.1", info.port)
        logger.info("对手已进房，等对局结束……")
        outcome = await asyncio.wait_for(session.wait_finished(), timeout=max_duration + 30)
    except asyncio.TimeoutError:
        outcome = "超时收摊"
    finally:
        if opponent is not None:
            await opponent.stop()
        await session.stop()
        bridge.stop()
        bridge_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass
        db.close()

    print("\n================ 链路自测结果 ================")
    print(f"对局结局：{outcome}")
    print(f"答复端：{'真模型 ' + real_model if real_model else '秒答假模型'}")
    print(f"bridge 计数：{bridge.stats.as_dict()}")
    if timings:
        ordered = sorted(timings)
        print(
            f"模型单次延迟（含文件往返）：最快 {ordered[0]:.2f}s｜"
            f"中位 {ordered[len(ordered) // 2]:.2f}s｜最慢 {ordered[-1]:.2f}s（共 {len(ordered)} 次）"
        )
    print(f"临时目录里的问题文件：{sorted(p.name for p in tmp.iterdir())}")
    print(f"模型收到的问题数：{len(prompts)}")
    if prompts:
        print("---- 第一份问题（截断 800 字）----")
        print(prompts[0][:800])
    decisions = collector.grep("[决策]")
    print(f"WindBot 侧的决策日志（{len(decisions)} 行）：")
    for line in decisions[:10]:
        print("   ", line[:200])
    for line in (collector.grep("等 AI 答复超时") + collector.grep("AI 决策层连续"))[:5]:
        print("   ", line[:200])

    ok = bool(prompts) and bridge.stats.answered > 0 and bridge.stats.failed == 0
    print("\n通道结论：", "通 ✔（真的问过并且答上了）" if ok else "没通过 ✘（见上面的计数与日志）")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="阻抗决策层真机链路自测")
    parser.add_argument("--max-duration", type=float, default=120.0, help="一局最长秒数")
    parser.add_argument("--brain-timeout-ms", type=int, default=2500,
                        help="WindBot 等答复的上限（默认 2500＝紧张窗口；插件实际用 llm.decision_timeout_ms）")
    parser.add_argument("--join-timeout", type=float, default=45.0, help="等对手进房的秒数")
    parser.add_argument(
        "--real-model",
        default="",
        help="用真模型而不是假模型（例如 deepseek-flash）；量端到端延迟时用它",
    )
    args = parser.parse_args()
    return asyncio.run(
        run(max_duration=args.max_duration, join_timeout=args.join_timeout,
            real_model=args.real_model, brain_timeout_ms=args.brain_timeout_ms)
    )


if __name__ == "__main__":
    raise SystemExit(main())
