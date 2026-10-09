"""同一副卡表、两个出牌脚本对打：看哪个更厉害。

和 `brain_ab.py` 是同一套纪律（镜像腿、座位逐局交替、Wilson 区间、座位映射核对），
只是变量从"开不开决策层"换成"用哪份脚本"。

**为什么要专门一个工具**：`Deck=` 的名字与"脚本强弱"是两件事。本机就有这种情况——
升辉月那副的库字段里 `picked_style=RaiseMoon` 与 `generated_script=Gen88` 同时存在，
而插件解析时后者优先，于是**手工调的那份根本没上过场**。要比出高下，就得让两份脚本
在同一副卡表上正面对打。

**两边各挂一个独立日志收集器**：`执行器：AI_xxx` 那行两个 bot 都会打，混在一个 logger 里
分不出谁是谁；分开挂之后，每一侧实际加载了哪份脚本是可以逐局核对的（没注册时 WindBot
会静默换随机执行器，只看胜负根本发现不了）。

用法::

    python plugins/mai-play-ygo/tools/style_ab.py \\
        --deck-file <升辉月的 .ydk> --style-a RaiseMoon --style-b Gen88 --duels 120
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import argparse
import asyncio
import logging
import math
import re
import sys
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MAIBOT_ROOT = _PLUGIN_ROOT.parent.parent
for _extra in (_PLUGIN_ROOT, _MAIBOT_ROOT):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from duel.cards import CardDatabase  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.room import RoomSettings, WindBotProcess, WindBotSettings  # noqa: E402
from duel.session import DuelSession, SessionConfig  # noqa: E402


def _resolve_windbot_exe(paths: Path) -> Path:
    """取插件实际会对局用的那份 exe。"""

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


def _load_log_name_map(source_root: Optional[Path]) -> Dict[str, str]:
    """源码里的 ``注册名 -> 日志名``（`[Deck("注册名", "日志名")]`；两者可以不同）。"""

    mapping: Dict[str, str] = {}
    if source_root is None:
        return mapping
    decks_dir = source_root / "Game" / "AI" / "Decks"
    if not decks_dir.is_dir():
        return mapping
    pattern = re.compile(r'\[Deck\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
    for path in decks_dir.glob("*.cs"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for registered, log_name in pattern.findall(text):
            mapping[registered] = log_name
    return mapping


class _Collector(logging.Handler):
    """按侧收集 WindBot 输出（两侧各一个，才能分清谁加载了哪份脚本）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def reset(self) -> None:
        self.lines.clear()

    def executors(self) -> List[str]:
        """这一侧实际加载的执行器名（`执行器：AI_xxx`）。"""

        return [
            line.split("执行器：")[-1].strip()
            for line in self.lines
            if "执行器：" in line
        ]

    def not_registered(self) -> bool:
        """有没有出现"名字没注册、改用随机执行器"的告警（脚本没装对时唯一会吼的一行）。"""

        return any("没在这份 exe 里注册" in line for line in self.lines)


@dataclass
class DuelOutcome:
    index: int
    seat_a: str
    """A 脚本在哪一侧（`bot` / `opponent`）。"""
    winner: str
    """`A` / `B` / `draw` / `unknown`。"""
    turns: Optional[int] = None
    seconds: float = 0.0
    a_loaded: List[str] = field(default_factory=list)
    b_loaded: List[str] = field(default_factory=list)
    a_warned: bool = False
    b_warned: bool = False
    note: str = ""

    def loaded(self, side: str) -> List[str]:
        """某一侧实际加载的执行器名（`side` 取 ``"A"`` / ``"B"``）。"""

        return self.a_loaded if side == "A" else self.b_loaded

    def warned(self, side: str) -> bool:
        """某一侧有没有出现"名字没注册、改用随机执行器"的告警。"""

        return self.a_warned if side == "A" else self.b_warned

    def side_ok(self, side: str, log_name: str) -> bool:
        """`side` 这一侧真的加载了期望的脚本。

        ⚠ 这个签名是修出来的：第一版写成 `side_ok(log_name)`、内部按"A 在不在 bot 侧"取列表，
        于是**问 B 的时候也在查 A 的列表**，一半的核对必然失败（假警报）。核对要显式说清问的是哪一侧。
        """

        return log_name in self.loaded(side)


@dataclass
class Tally:
    outcomes: List[DuelOutcome] = field(default_factory=list)
    log_a: str = ""
    log_b: str = ""
    name_a: str = "A"
    name_b: str = "B"

    def add(self, outcome: DuelOutcome) -> None:
        self.outcomes.append(outcome)

    @staticmethod
    def _wilson(successes: int, total: int) -> tuple:
        """Wilson 95% 区间（极端比例下也有效；正态近似在 p=0/1 会给出零宽度假象）。"""

        if total <= 0:
            return (float("nan"), float("nan"))
        z = 1.96
        p = successes / total
        denom = 1 + z * z / total
        center = (p + z * z / (2 * total)) / denom
        half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
        return (max(0.0, center - half), min(1.0, center + half))

    def summary(self) -> dict:
        a = sum(1 for o in self.outcomes if o.winner == "A")
        b = sum(1 for o in self.outcomes if o.winner == "B")
        draws = sum(1 for o in self.outcomes if o.winner == "draw")
        decided = a + b
        rate = (a / decided) if decided else float("nan")
        low, high = self._wilson(a, decided)
        side_bad = sum(
            1
            for o in self.outcomes
            if o.warned("A") or o.warned("B")
            or not o.side_ok("A", self.log_a) or not o.side_ok("B", self.log_b)
        )
        return {
            "局数": len(self.outcomes),
            "算进胜率": decided,
            "平局": draws,
            "没判出胜负": len(self.outcomes) - decided - draws,
            f"{self.name_a} 赢": a,
            f"{self.name_b} 赢": b,
            f"{self.name_a} 胜率": rate,
            "95%区间": (low, high),
            "两侧脚本核对不通过的局数": side_bad,
        }


async def _run_one(
    *,
    index: int,
    seat_a: str,
    deck_file: Path,
    deck_file_b: Path,
    style_a: str,
    style_b: str,
    paths: Path,
    windbot_exe: Path,
    db: CardDatabase,
    session_logger: logging.Logger,
    opponent_logger: logging.Logger,
    bot_collector: _Collector,
    opp_collector: _Collector,
    is_a_on_bot: bool,
    max_duel_seconds: float,
    join_timeout: float,
) -> DuelOutcome:
    """打一局：`seat_a` 侧用 A 那副牌与它的脚本，另一侧用 B 的。

    **牌与脚本必须一起换**：座位每局交替，换座位时如果只换脚本不换卡表，
    就变成"A 的脚本拿 B 的卡表在打"——那不是比强弱，是在比谁的脚本更抗错配。
    """

    bot_style = style_a if is_a_on_bot else style_b
    opponent_style = style_b if is_a_on_bot else style_a
    bot_deck = deck_file if is_a_on_bot else deck_file_b
    opponent_deck = deck_file_b if is_a_on_bot else deck_file

    config = SessionConfig(
        ygopro_executable=paths / "ygopro" / "ygopro.exe",
        ygopro_dir=paths / "ygopro",
        windbot_executable=windbot_exe,
        windbot_dir=paths / "windbot",
        cards_cdb=paths / "ygopro" / "cards.cdb",
        room=RoomSettings(save_replay=False),
        bot_name="A/B-bot",
        windbot_deck=bot_style,
        bot_deck_file=bot_deck,
        listen_port=0,
        join_timeout=join_timeout,
        max_duration=max_duel_seconds,
    )
    session = DuelSession(config, group_id="style-ab", card_db=db, logger=session_logger)
    opponent: Optional[WindBotProcess] = None
    bot_collector.reset()
    opp_collector.reset()
    started = time.perf_counter()
    finished = False
    note = ""
    try:
        info = await session.start()
        opponent = WindBotProcess(
            config.windbot_executable,
            config.windbot_dir,
            WindBotSettings(
                name="A/B-opponent", deck=opponent_style, deck_file=opponent_deck,
                password=info.password, db_path=config.cards_cdb, debug=True,
            ),
            logger=opponent_logger,
        )
        await opponent.start("127.0.0.1", info.port)
        try:
            await asyncio.wait_for(session.wait_finished(), timeout=max_duel_seconds + 20)
            finished = True
        except asyncio.TimeoutError:
            note = f"限时 {max_duel_seconds:.0f}s 收摊"
    except Exception as exc:  # noqa: BLE001  单局出错不停批
        note = f"{type(exc).__name__}: {exc}"
    finally:
        data = session.result_dict() if finished else {}
        turns = data.get("turns") if isinstance(data.get("turns"), int) else None
        winner_is_self = data.get("winner_is_self")
        # 收集器是按**侧**固定挂的：bot 那个必然是被测会话，opponent 那个必然是另一个进程
        bot_loaded, opp_loaded = bot_collector.executors(), opp_collector.executors()
        bot_warned, opp_warned = bot_collector.not_registered(), opp_collector.not_registered()
        if opponent is not None:
            await opponent.stop()
        await session.stop()

    winner = "unknown"
    if winner_is_self is True:
        winner = "A" if is_a_on_bot else "B"
    elif winner_is_self is False:
        winner = "B" if is_a_on_bot else "A"
    elif finished:
        winner = "draw"
    # 对局根本没推进时（回合 0 / 没判出胜负），WindBot 的报错只在收集器里——带出来，
    # 否则只能看到"限时收摊"，完全不知道是卡表坏了、脚本崩了还是压根没进房
    if not note and (turns in (None, 0) or winner == "unknown"):
        bot_tail = bot_collector.lines[-12:]
        opp_tail = opp_collector.lines[-6:]
        note = (
            "对局未推进｜bot 侧尾部：" + " ⏎ ".join(line[:160] for line in bot_tail)
            + "｜对手侧尾部：" + " ⏎ ".join(line[:160] for line in opp_tail)
        )
    return DuelOutcome(
        index=index, seat_a=seat_a, winner=winner, turns=turns,
        seconds=time.perf_counter() - started,
        a_loaded=sorted(set(bot_loaded if is_a_on_bot else opp_loaded)),
        b_loaded=sorted(set(opp_loaded if is_a_on_bot else bot_loaded)),
        a_warned=bot_warned if is_a_on_bot else opp_warned,
        b_warned=opp_warned if is_a_on_bot else bot_warned,
        note=note,
    )


async def run(args: argparse.Namespace) -> int:
    # ⚠ 卡表路径**必须解析成绝对路径**再交给 WindBot：`DeckFile=` 是按 **WindBot 的工作目录**
    # （`clients/windbot`）解析的，而命令行里写的通常是"相对 MaiBot 根目录"的路径。
    # 传相对路径的后果不是"报错说找不到卡表"，而是 Deck 根本没加载 → 两个 bot 一收到 JoinGame
    # 就 `System.NullReferenceException`（GameBehavior.OnJoinGame 里读 `Deck.Cards`），
    # 每一局空转到 max-duel-seconds 才收摊——2026-10-09 就是这样白跑了 7 局 × 2 分钟。
    deck_file = Path(args.deck_file).resolve()
    if not deck_file.is_file():
        print(f"卡表不在：{deck_file}")
        return 2
    # 第二副牌（可选）：给了就是"两副牌互打"（各自用自己的脚本），
    # 不给就是老口径"同一副牌换脚本"，两边行为完全兼容
    deck_file_b = Path(args.deck_file_b).resolve() if args.deck_file_b else deck_file
    if not deck_file_b.is_file():
        print(f"B 侧卡表不在：{deck_file_b}")
        return 2
    paths = _PLUGIN_ROOT / "clients"
    windbot_exe = _resolve_windbot_exe(paths)
    db = CardDatabase(paths / "ygopro" / "cards.cdb")
    from_src = windbot_exe.parent.parent.parent if windbot_exe.parent.name == "Release" else None
    log_map = _load_log_name_map(from_src)

    session_logger = logging.getLogger("style_ab.bot")
    opponent_logger = logging.getLogger("style_ab.opponent")
    bot_collector, opp_collector = _Collector(), _Collector()
    for lg, collector in ((session_logger, bot_collector), (opponent_logger, opp_collector)):
        lg.setLevel(logging.INFO)     # WindBot 的输出行走 INFO，收下来才能核对执行器
        lg.addHandler(collector)
        lg.propagate = False          # 别让对局输出淹了汇总（两个 logger 各自收各自的）

    tally = Tally(log_a=log_map.get(args.style_a, args.style_a),
                  log_b=log_map.get(args.style_b, args.style_b),
                  name_a=args.style_a, name_b=args.style_b)
    print(f"被测 exe：{windbot_exe}")
    if deck_file_b == deck_file:
        print(f"卡表（两边一样）：{deck_file}")
    else:
        print(f"A 的卡表：{deck_file}\nB 的卡表：{deck_file_b}")
    print(f"A = {args.style_a}（日志名 {tally.log_a}）｜B = {args.style_b}（日志名 {tally.log_b}）")
    print(f"计划 {args.duels} 局，逐局交替座位（两边都**不开**决策层，只比脚本）\n")

    deadline = time.monotonic() + args.max_minutes * 60
    for index in range(args.duels):
        if time.monotonic() > deadline:
            print(f"（到时间上限 {args.max_minutes} 分钟，提前收工；已跑 {len(tally.outcomes)} 局）")
            break
        is_a_on_bot = index % 2 == 0
        seat_a = "bot" if is_a_on_bot else "opponent"
        outcome = await _run_one(
            index=index, seat_a=seat_a, deck_file=deck_file, deck_file_b=deck_file_b,
            style_a=args.style_a, style_b=args.style_b,
            paths=paths, windbot_exe=windbot_exe, db=db,
            session_logger=session_logger, opponent_logger=opponent_logger,
            bot_collector=bot_collector, opp_collector=opp_collector,
            is_a_on_bot=is_a_on_bot,
            max_duel_seconds=args.max_duel_seconds, join_timeout=args.join_timeout,
        )
        tally.add(outcome)
        mark = "✔" if (outcome.side_ok("A", tally.log_a) and outcome.side_ok("B", tally.log_b)) else "✘"
        print(
            f"[{index + 1}/{args.duels}] A 在 {outcome.seat_a:8s}｜胜者={outcome.winner:7s}｜"
            f"回合={outcome.turns}｜{outcome.seconds:4.0f}s｜{mark} A侧{outcome.a_loaded} "
            f"B侧{outcome.b_loaded}{'｜' + outcome.note if outcome.note else ''}",
            # 长批次要能边跑边看：重定向到文件时 stdout 是块缓冲，不 flush 的话整批跑完之前
            # 日志一行都看不见（实测卡了 9 分钟、以为它死了，其实在正常跑）
            flush=True,
        )
        if args.pause:
            await asyncio.sleep(args.pause)

    db.close()
    summary = tally.summary()
    print("\n================ 汇总 ================")
    for key, value in summary.items():
        if key.endswith("胜率") and isinstance(value, float) and not math.isnan(value):
            print(f"{key}：{value:.1%}")
        elif key == "95%区间" and isinstance(value, tuple) and not math.isnan(value[0]):
            print(f"{key}：{value[0]:.1%} ~ {value[1]:.1%}")
        else:
            print(f"{key}：{value}")
    decided = summary["算进胜率"]
    low, high = summary["95%区间"]
    if decided and not (low <= 0.5 <= high):
        print(f"\n→ 区间（{low:.1%}~{high:.1%}）**没有跨过 50%**：这一个批次里 A、B 强弱有差。")
    elif decided:
        print(f"\n→ 区间（{low:.1%}~{high:.1%}）跨过 50%：**这一批分不出高下**。")
    if summary["两侧脚本核对不通过的局数"]:
        print(f"⚠ 有 {summary['两侧脚本核对不通过的局数']} 局两侧加载的脚本不对（没注册会静默换随机执行器）"
              f"——那些局的胜负不能算进「哪个脚本更强」。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同一副卡表、两个出牌脚本对打")
    parser.add_argument("--deck-file", required=True, help="A 侧的 .ydk")
    parser.add_argument("--deck-file-b", default="", help="B 侧的 .ydk（留空＝两边同一副牌，只换脚本）")
    parser.add_argument("--style-a", required=True, help="A 脚本（Deck= 的名字）")
    parser.add_argument("--style-b", required=True, help="B 脚本")
    parser.add_argument("--duels", type=int, default=60, help="总对局数（座位逐局交替）")
    parser.add_argument("--max-duel-seconds", type=float, default=120.0, help="单局最长秒数")
    parser.add_argument("--join-timeout", type=float, default=30.0, help="等对手进房的秒数")
    parser.add_argument("--pause", type=float, default=1.0, help="两局之间的间隔秒数")
    parser.add_argument("--max-minutes", type=float, default=50.0, help="整批时间上限")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
