"""随机池每副卡组各打一局：验证"装上了对的出牌脚本 + 能正常对局"。

**为什么需要它**：WindBot 对"Deck 名字没注册"**不报错**，而是随机挑一个 Normal 档执行器顶上
（`DecksManager` 那条告警是本项目后加的）。于是"脚本装错了"这种故障在对局里看起来完全正常——
打法不对、数字不对，但没有任何报错。所以每副牌都要真机确认一次：
启动日志里的 `执行器：AI_<名字>` 必须**正是我期望的那份**。

它同时是"能打"的体检：对局有没有推进（回合数/动作数）、有没有崩、有没有卡死。

用法::

    python plugins/mai-play-ygo/tools/pool_smoke.py                    # 池子里 in_random 的全打一遍
    python plugins/mai-play-ygo/tools/pool_smoke.py --seconds 150      # 每副最多打多久
    python plugins/mai-play-ygo/tools/pool_smoke.py --deck-id 88 101   # 只打指定的几副

⚠ **每副是"限时对局"**（到点收摊），不是等到分出胜负——真实卡组一副 4~10 分钟，
11 副打完要一两个小时；体检要的是"脚本对不对、有没有在动"，不是胜负。
想要胜负就调大 ``--seconds``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import argparse
import asyncio
import logging
import sys
import tempfile
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MAIBOT_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _MAIBOT_ROOT.parent.parent
for _extra in (_PLUGIN_ROOT, _MAIBOT_ROOT):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from duel.cards import CardDatabase  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.deckpool import DeckPool  # noqa: E402
from duel.room import RoomSettings, WindBotProcess, WindBotSettings  # noqa: E402
from duel.session import DuelSession, SessionConfig  # noqa: E402

#: 对手固定用它：上游自带、可靠、而且它的出牌脚本自带卡表（不用再找 .ydk）。
OPPONENT_STYLE = "Blue-Eyes"


def _resolve_windbot_exe(paths: Path) -> Path:
    """取插件实际会对局用的那份 exe（与另外两个工具同一口径）。"""

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


def _resolve_style(deck) -> str:
    """**与插件同一条口径**决定这副牌用哪份脚本（`plugin._deck_with_style`）。

    顺序：`generated_script` 优先于 `picked_style` 优先于 `windbot_deck`。
    这里必须复刻插件的行为——否则测出来的和真实对局用的可能不是同一份。
    """

    if deck.generated_script:
        return deck.generated_script
    if deck.picked_style:
        return deck.picked_style
    return deck.windbot_deck or ""


def _load_log_name_map(source_root: Optional[Path]) -> Dict[str, str]:
    """从源码里读出 ``注册名 -> 日志名`` 的映射。

    ⚠ **这一步不能省**：`[Deck("A", "B")]` 的第一个参数是 ``Deck=`` 用的**注册名**，
    第二个才是 `DecksManager` 打日志用的**卡表名**，两者**可以合法地不一样**——
    实测踩过：`WitchcraftShop` 注册成 `[Deck("WitchcraftShop", "AI_Witchcraft")]`（沿用上游的卡表名），
    于是日志里出现 `AI_Witchcraft`。我第一版判据拿注册名去比日志名，报了 2 副"脚本装错了"的假警报。
    所以要么查源码建映射，要么改用"有没有那条未注册告警"来判（见 :attr:`SmokeResult.executor_ok`）。
    """

    mapping: Dict[str, str] = {}
    if source_root is None:
        return mapping
    decks_dir = source_root / "Game" / "AI" / "Decks"
    if not decks_dir.is_dir():
        return mapping
    import re

    pattern = re.compile(r'\[Deck\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
    for path in decks_dir.glob("*.cs"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for registered, log_name in pattern.findall(text):
            mapping[registered] = log_name
    return mapping


@dataclass
class SmokeResult:
    """一副牌的体检结果。"""

    deck_id: int
    name: str
    expected_style: str
    expected_log_name: str
    seen_executors: List[str]
    turns: Optional[int]
    seconds: float
    finished: bool
    winner_is_self: Optional[bool] = None
    not_registered_warning: bool = False
    note: str = ""

    @property
    def executor_ok(self) -> bool:
        """对局里真的加载了期望的那份脚本。

        两个条件缺一不可：
        1. **没有那条"没在这份 exe 里注册、改用随机执行器"的告警**——这是唯一能直接证伪的信号；
        2. 日志里的执行器名 == 期望脚本的**日志名**（注册名与日志名可以不同，见 `_load_log_name_map`）。
        """

        if self.not_registered_warning:
            return False
        return self.expected_log_name in self.seen_executors


class _Collector(logging.Handler):
    """收 WindBot 的输出行（`执行器：` 那行只在这里能看见）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def reset(self) -> None:
        self.lines.clear()


async def _smoke_one(
    *,
    deck,
    style: str,
    log_name: str,
    paths: Path,
    windbot_exe: Path,
    db: CardDatabase,
    logger: logging.Logger,
    collector: _Collector,
    seconds: float,
) -> SmokeResult:
    """给一副牌打一局限时对局，返回体检结果。"""

    ydk = Path(deck.ydk_path) if deck.ydk_path else None
    if ydk is None or not ydk.is_file():
        return SmokeResult(deck.deck_id, deck.display_name, style, log_name, [], None, 0.0, False,
                           note=f"卡表文件不在：{ydk}")

    config = SessionConfig(
        ygopro_executable=paths / "ygopro" / "ygopro.exe",
        ygopro_dir=paths / "ygopro",
        windbot_executable=windbot_exe,
        windbot_dir=paths / "windbot",
        cards_cdb=paths / "ygopro" / "cards.cdb",
        room=RoomSettings(save_replay=False),
        bot_name="体检-bot",
        windbot_deck=style,
        bot_deck_file=ydk,
        bot_debug=True,
        listen_port=0,
        join_timeout=30.0,
        max_duration=seconds,
    )
    session = DuelSession(config, group_id="pool-smoke", card_db=db, logger=logger)
    opponent: Optional[WindBotProcess] = None
    collector.reset()
    started = time.perf_counter()
    finished = False
    turns: Optional[int] = None
    note = ""
    try:
        info = await session.start()
        opponent = WindBotProcess(
            config.windbot_executable,
            config.windbot_dir,
            WindBotSettings(
                name="体检-对手", deck=OPPONENT_STYLE,
                password=info.password, db_path=config.cards_cdb, debug=True,
            ),
            logger=logger,
        )
        await opponent.start("127.0.0.1", info.port)
        try:
            await asyncio.wait_for(session.wait_finished(), timeout=seconds)
            finished = True
        except asyncio.TimeoutError:
            note = f"限时 {seconds:.0f}s 到点收摊（体检不看胜负）"
    except Exception as exc:  # noqa: BLE001  一副出问题不能让整批停
        note = f"{type(exc).__name__}: {exc}"
    finally:
        collector_lines = list(collector.lines)
        data = session.result_dict() if finished else {}
        turns = data.get("turns") if isinstance(data.get("turns"), int) else None
        warning = any("没在这份 exe 里注册" in line for line in collector_lines)
        winner_is_self = data.get("winner_is_self") if finished else None
        if opponent is not None:
            await opponent.stop()
        await session.stop()

    # `执行器：AI_xxx` 是 DecksManager 在本局里真正挑中的那份；两个 bot 各打一行
    executors = [
        line.split("执行器：")[-1].strip()
        for line in collector_lines
        if "执行器：" in line
    ]
    return SmokeResult(
        deck_id=deck.deck_id,
        name=deck.display_name,
        expected_style=style,
        expected_log_name=log_name,
        seen_executors=sorted(set(executors)),
        turns=turns,
        seconds=time.perf_counter() - started,
        finished=finished,
        winner_is_self=winner_is_self if isinstance(winner_is_self, bool) else None,
        not_registered_warning=warning,
        note=note,
    )


async def run(args: argparse.Namespace) -> int:
    logger = logging.getLogger("pool_smoke")
    logger.setLevel(logging.INFO)
    collector = _Collector()
    logger.addHandler(collector)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    data_dir = _MAIBOT_ROOT / "data" / "plugins" / "mai-play-ygo"
    pool = DeckPool(data_dir, default_windbot_deck="Blue-Eyes")
    decks = [d for d in pool.list_decks(args.group_id) if d.in_random]
    if args.deck_id:
        wanted = set(args.deck_id)
        decks = [d for d in decks if d.deck_id in wanted]
    decks.sort(key=lambda d: d.deck_id)

    paths = _PLUGIN_ROOT / "clients"
    windbot_exe = _resolve_windbot_exe(paths)
    db = CardDatabase(paths / "ygopro" / "cards.cdb")
    # 源码树在 `<src>/bin/Release/WindBot.exe` 时，`<src>` 就是往上三级
    from_src = windbot_exe.parent.parent.parent if windbot_exe.parent.name == "Release" else None
    log_map = _load_log_name_map(from_src)
    print(f"被测 exe：{windbot_exe}")
    print(f"注册名→日志名映射：{len(log_map)} 条"
          + ("（读不到源码树时退化成'注册名即日志名'，可能误报）" if not log_map else ""))
    print(f"随机池：{len(decks)} 副｜每副限时 {args.seconds:.0f}s｜对手固定 {OPPONENT_STYLE}\n")

    results: List[SmokeResult] = []
    for index, deck in enumerate(decks, start=1):
        style = _resolve_style(deck)
        print(f"--- [{index}/{len(decks)}] #{deck.deck_id} {deck.display_name}（期望脚本 {style}）")
        result = await _smoke_one(
            deck=deck, style=style, log_name=log_map.get(style, style), paths=paths,
            windbot_exe=windbot_exe, db=db, logger=logger, collector=collector,
            seconds=args.seconds,
        )
        results.append(result)
        mark = "✔" if result.executor_ok else "✘"
        print(f"    {mark} 实际加载：{result.seen_executors or '（没看到执行器行）'}"
              f"｜回合={result.turns}｜{result.seconds:.0f}s｜{'打完' if result.finished else '限时收摊'}"
              f"{'｜' + result.note if result.note else ''}")
        if args.pause:
            await asyncio.sleep(args.pause)

    db.close()
    pool.close()

    print("\n================ 体检汇总 ================")
    print(f"{'副':>4} {'名字':<20} {'期望脚本':<22} {'实际加载':<40} {'回合':>4}  判定")
    bad = []
    for r in results:
        actual = "、".join(r.seen_executors) or "（无）"
        verdict = "✔" if r.executor_ok else "✘ 脚本不对/没加载"
        if not r.executor_ok:
            bad.append(r)
        outcome = "—" if r.winner_is_self is None else ("bot赢" if r.winner_is_self else "bot输")
        print(f"{r.deck_id:>4} {r.name[:18]:<18} {r.expected_style:<20} {actual[:34]:<34} "
              f"{str(r.turns):>4} {outcome:>6}  {verdict}")
    print()
    if bad:
        print(f"❌ 有 {len(bad)} 副没加载到期望的脚本："
              + "、".join(f"#{r.deck_id} {r.name}" for r in bad))
        print("   去 executors/README.md 看'怎么装'——脚本没编进 exe 时 WindBot 不报错、"
              "会随机挑一个顶上（对局照打，但打出来的不是这副牌的脚本）。")
    else:
        print(f"✅ {len(results)} 副全部加载到期望的脚本。")
    wins = sum(1 for r in results if r.winner_is_self is True)
    losses = sum(1 for r in results if r.winner_is_self is False)
    if wins + losses:
        print(f"顺带看一眼胜负（对手固定 {OPPONENT_STYLE} 脚本）：池子这边 {wins} 胜 {losses} 负"
              f"——每副只打 1 局，**不是胜率**，只看「有没有人能打」。")
    played = [r for r in results if r.turns]
    if played:
        print(f"对局推进：{len(played)}/{len(results)} 副报出了回合数，"
              f"平均 {sum(r.turns or 0 for r in played) / len(played):.1f} 回合，"
              f"限时收摊 {sum(1 for r in results if not r.finished)} 副（体检不等胜负，属正常）。")
    return 0 if not bad else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="随机池逐副体检：脚本装对了没有 + 能不能打")
    parser.add_argument("--group-id", default="27dc88f32327", help="按哪个群的口径取随机池")
    parser.add_argument("--deck-id", type=int, nargs="*", help="只体检这几副（默认全池）")
    parser.add_argument("--seconds", type=float, default=120.0, help="每副最长打多少秒")
    parser.add_argument("--pause", type=float, default=3.0, help="两副之间的间隔秒数（让端口/进程收干净）")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
