"""阶段 0 实验：不开 MaiBot，直接在本机验证「房间 + 闸门 + bot」这条外部链路。

它做的事情和插件运行时完全一样（复用同一套 ``duel/`` 模块），只是把过程直接打在终端上，
方便在接进 MaiBot 之前先确认部署前提是否成立：

1. 检查 ygopro.exe、工作目录、cards.cdb、script/ 脚本是否齐全；
2. 起房间内核，确认它能报出监听端口（这一步同时验证了内核是服务端模式构建的）；
3. 起闸门并打印地址与口令；
4. 拉起 WindBot 占座；
5. 打印「在 MDPro3 里要填什么」，然后实时显示观测到的对局事件；
6. 对局结束后打印复述，最后收摊。

用法::

    python tools/experiment_room.py \\
        --ygopro-exe D:/ygopro/ygopro.exe --ygopro-dir D:/ygopro \\
        --windbot-exe D:/windbot/WindBot.exe --windbot-dir D:/windbot

只想检查部署前提、不起房间时加 ``--check-only``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import argparse
import asyncio
import logging
import sys
import tempfile
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.cards import CardDatabase, CardDatabaseError  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.deckcode import DeckCodeError, describe_issues, parse_deck_code  # noqa: E402
from duel.deckpool import DeckPool, StoredDeck  # noqa: E402
from duel.netutil import detect_lan_address, format_endpoint  # noqa: E402
from duel.protocol import Frame, Msg, Stoc  # noqa: E402
from duel.room import ProcessError  # noqa: E402
from duel.session import (  # noqa: E402
    OUTCOME_NO_PLAYER,
    DuelSession,
    SessionConfig,
    generate_password,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="验证游戏王对局链路（房间 + 闸门 + bot）")
    parser.add_argument("--ygopro-exe", required=True, help="ygopro.exe 路径（服务端模式构建）")
    parser.add_argument("--ygopro-dir", required=True, help="ygopro 工作目录")
    parser.add_argument("--windbot-exe", required=True, help="WindBot.exe 路径")
    parser.add_argument("--windbot-dir", required=True, help="WindBot 工作目录")
    parser.add_argument("--cards-cdb", default="", help="cards.cdb 路径，默认取 ygopro 目录下的")
    parser.add_argument("--deck", default="", help="给 bot 用的 .ydk 路径，留空则用风格卡组自带卡表")
    parser.add_argument("--deck-style", default="DoEveryThing", help="WindBot 风格卡组名")
    parser.add_argument("--bot-name", default="MaiBot", help="bot 在对局里显示的名字")
    parser.add_argument("--public-host", default="", help="发给群友的地址，留空自动探测")
    parser.add_argument("--listen-port", type=int, default=0, help="闸门端口，0 表示随机")
    parser.add_argument("--timeout", type=float, default=600.0, help="整场实验的最长秒数")
    parser.add_argument("--join-timeout", type=float, default=300.0, help="等人类进来的秒数")
    parser.add_argument("--bot-debug", action="store_true", help="打开 WindBot 的对局日志")
    parser.add_argument("--check-only", action="store_true", help="只检查部署前提，不起房间")
    return parser.parse_args()


def check_environment(args: argparse.Namespace) -> List[str]:
    """检查部署前提，返回问题列表（空列表表示全部通过）。"""

    problems: List[str] = []
    ygopro_exe = Path(args.ygopro_exe)
    ygopro_dir = Path(args.ygopro_dir)
    windbot_exe = Path(args.windbot_exe)
    windbot_dir = Path(args.windbot_dir)

    print("== 部署前提检查 ==")
    for label, path in (
        ("ygopro.exe", ygopro_exe),
        ("ygopro 工作目录", ygopro_dir),
        ("WindBot.exe", windbot_exe),
        ("WindBot 工作目录", windbot_dir),
    ):
        exists = path.exists()
        print(f"  [{'OK ' if exists else '缺失'}] {label}: {path}")
        if not exists:
            problems.append(f"{label} 不存在：{path}")

    if not ygopro_dir.is_dir():
        return problems

    for name in ("cards.cdb", "lflist.conf"):
        candidate = ygopro_dir / name
        exists = candidate.is_file()
        print(f"  [{'OK ' if exists else '缺失'}] {name}")
        if not exists:
            problems.append(f"ygopro 工作目录缺少 {name}（客户端分发包里有）")

    script_dir = ygopro_dir / "script"
    if script_dir.is_dir():
        lua_files = list(script_dir.glob("*.lua"))
        print(f"  [OK ] script/ 目录，{len(lua_files)} 个 Lua 脚本")
        if len(lua_files) < 100:
            problems.append("script/ 里的 Lua 脚本太少，请确认卡片脚本已完整解压")
    else:
        print("  [缺失] script/ 目录（卡片效果脚本）")
        problems.append("ygopro 工作目录缺少 script/（来自 ProjectIgnis/CardScripts）")

    cdb_path = Path(args.cards_cdb) if args.cards_cdb else ygopro_dir / "cards.cdb"
    if cdb_path.is_file():
        try:
            with CardDatabase(cdb_path) as database:
                sample_name = database.name(89631139)
            print(f"  [OK ] cards.cdb 可读，样例卡名：{sample_name}")
            if not sample_name:
                problems.append("cards.cdb 里查不到样例卡 89631139，可能是空库")
        except CardDatabaseError as exc:
            print(f"  [坏 ] cards.cdb 读取失败：{exc}")
            problems.append(str(exc))
    return problems


def load_deck(pool: DeckPool, deck_path: Path) -> StoredDeck:
    """把指定的 .ydk 装进一个临时卡组池，返回可交给 bot 使用的卡组记录。

    Raises:
        DeckCodeError: 卡组文件无法解析或不满足对战条件时抛出。
        OSError: 卡组文件读不出来时抛出。
    """

    parsed = parse_deck_code(deck_path.read_text(encoding="utf-8"))
    issues = describe_issues(parsed)
    if issues:
        raise DeckCodeError("；".join(issues))
    return pool.add(
        group_id="experiment",
        display_name=deck_path.stem,
        contributor_id="experiment",
        contributor_name="实验脚本",
        ydk_text=parsed.to_ydk(deck_path.stem),
        deck_code="",
        source_format=parsed.source_format,
        main_count=len(parsed.main),
        extra_count=len(parsed.extra),
        side_count=len(parsed.side),
    )


async def run_experiment(args: argparse.Namespace) -> int:
    """执行一次完整的链路验证。"""

    public_host = args.public_host.strip() or detect_lan_address() or "127.0.0.1"
    cards_cdb = Path(args.cards_cdb) if args.cards_cdb else Path(args.ygopro_dir) / "cards.cdb"
    # 实验用的卡组池放在系统临时目录，不往插件源码目录里写东西
    work_dir = Path(tempfile.mkdtemp(prefix="yugioh-duel-experiment-"))
    deck_pool = DeckPool(work_dir / "decks")
    started_at = time.time()

    def on_status(event: str, payload: Dict[str, object]) -> None:
        """把闸门事件实时打在终端上。"""

        stamp = time.strftime("%H:%M:%S")
        detail = payload.get("client") or payload.get("peer") or payload.get("name") or ""
        reason = payload.get("reason") or ""
        print(f"  [{stamp}] {event} {detail} {reason}".rstrip())

    def on_frame(role: str, direction: str, frame: Frame) -> None:
        """把关键报文打出来，用于确认解析链路正常。"""

        del role
        if direction != "to_client":
            return
        if frame.message_id == Stoc.GAME_MSG and frame.payload and frame.payload[0] == Msg.WIN:
            print(f"  [{time.strftime('%H:%M:%S')}] 观测到胜负报文 MSG_WIN")
        elif frame.message_id == Stoc.DUEL_START:
            print(f"  [{time.strftime('%H:%M:%S')}] 观测到对局开始")
        elif frame.message_id == Stoc.DUEL_END:
            print(f"  [{time.strftime('%H:%M:%S')}] 观测到对局结束")

    deck: Optional[StoredDeck] = None
    if args.deck:
        deck_path = Path(args.deck)
        if not deck_path.is_file():
            print(f"指定的卡组文件不存在：{deck_path}")
            deck_pool.close()
            return 2
        try:
            deck = load_deck(deck_pool, deck_path)
        except (DeckCodeError, OSError) as exc:
            print(f"卡组装载失败：{exc}")
            deck_pool.close()
            return 2
        print(f"== 已装载卡组：{deck.describe()}")

    session = DuelSession(
        SessionConfig(
            ygopro_executable=Path(args.ygopro_exe),
            ygopro_dir=Path(args.ygopro_dir),
            windbot_executable=Path(args.windbot_exe),
            windbot_dir=Path(args.windbot_dir),
            cards_cdb=cards_cdb,
            bot_name=args.bot_name,
            windbot_deck=args.deck_style,
            bot_debug=args.bot_debug,
            public_host=public_host,
            listen_port=args.listen_port,
            join_timeout=args.join_timeout,
            max_duration=args.timeout,
            on_status=on_status,
            on_frame=on_frame,
        ),
        group_id="experiment",
        human_name_hint="实验员",
        deck=deck,
        card_db=CardDatabase(cards_cdb),
        logger=logging.getLogger("experiment"),
    )

    print("\n== 启动 ==")
    try:
        info = await session.start(password=generate_password())
    except ProcessError as exc:
        print(f"启动失败：{exc}")
        await session.stop()
        deck_pool.close()
        return 3

    print(f"  内核端口（内部，不用填）：{info.backend_port}")
    print("\n" + "=" * 60)
    print("  在客户端里这样填：")
    print(f"    主机地址 / 服务器：{info.host}")
    print(f"    端口：{info.port}")
    print(f"    房间密码：{info.password}")
    print(f"  （等价写法：{format_endpoint(info.host, info.port)}）")
    print("  然后加入房间，和 bot 打一局。")
    print("=" * 60 + "\n")
    print("== 事件流（Ctrl+C 可提前收摊）==")

    outcome = "aborted"
    try:
        outcome = await session.wait_finished()
    except KeyboardInterrupt:
        print("\n已手动中断，正在收摊…")
    finally:
        print("\n== 结果 ==")
        for line in session.summary_lines():
            print(f"  · {line}")
        print(f"  结束原因：{outcome}，耗时 {int(time.time() - started_at)} 秒")
        if outcome != OUTCOME_NO_PLAYER:
            print(f"  结构化数据：{session.result_dict()}")
        await session.stop()
        deck_pool.close()

    return 0 if outcome else 1


def main() -> int:
    """入口：先检查前提，再决定是否起房间。"""

    args = parse_args()
    problems = check_environment(args)
    if problems:
        print("\n== 前提检查未通过 ==")
        for problem in problems:
            print(f"  - {problem}")
        print("\n先把上面这些问题解决，再跑一次。")
        return 1

    print("\n前提检查全部通过。")
    if args.check_only:
        print("（--check-only 模式，不启动房间）")
        return 0

    try:
        return asyncio.run(run_experiment(args))
    except KeyboardInterrupt:
        print("\n已手动中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
