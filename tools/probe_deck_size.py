"""量出"一副牌到底能放多少张"——三条限制各自的上限，以及它们越界时的现象。

**为什么需要它**：卡组张数上有**三条**限制，出自三个地方，混成一条就会得出错误结论
（"赖皮卡组能打"与"一千张灰流丽能打"就是两种完全不同的结论）：

* **对局引擎**：一场对局实际用的主卡组最多 **60 张**。注册多少张它都收，但对局开场只把 60 张
  放进卡组（下面表里那组"注册 250 张、对局内 60 张"就是它）——多出来的牌等于白带，
  WindBot 自己的卡组计数器还会因此一直报 mismatch。
* **内核**（ygopro.exe 服务端模式）：注册的卡组**主卡组+额外最多 250 张**，超了当场回
  ``STOC_ERROR_MSG``（``msg=2`` DECKERROR）并把这个客户端踢出房间；而 WindBot 对这条是
  **静默退出**（只在 DEBUG 构建里打日志）。群里看到的就是"房间开好了但 bot 进不去"。
* **WindBot** 自己的 ``Deck.Load``：主卡组 > 60、额外 > 15、副卡组 > 15 都丢掉整副卡组
  （可用 ``MaxDeckSize=`` 放行 60 那条，但第二、三条摆在那儿，放行了也没意义——
  本项目实测后已经放弃这条路，见 CHANGELOG 1.2.4）。

2026-10-10 用本工具量出的结果（房间规则与插件实际开房一致，"不检查卡组"开着）：

==================  ================================  ==========================================
注册的主+额外张数    对局内报的主卡组张数（见输出）      结果
==================  ================================  ==========================================
75 / 215 / 240 /      75 → 60（截断）                   正常开局（``STOC_DUEL_START`` 到来）
  249 / 250           249 → 60、250 → 60（截断）        对局的卡组只有 60 张，多出来的白带
60（模板无额外）       60                              完整开局，不截断
61                    60（截断）                       同上：多出来的那张不进对局
250 再加副卡组 15      250 → 60                        副卡组 15 张不参与张数限制，也不进主卡组
251                    —                               被拒：``STOC_ERROR_MSG``（msg=2），
                                                        bot 随后退出，附带码（pcode）= 0
265                    —                               同上
==================  ================================  ==========================================

结论写进了 `duel/deckcode`：对局主卡组上限是 ``DUEL_MAIN_MAX = 60``（第 1 条），
开房前由 `DuelSession.start` 用 `describe_room_limits` 拦下主卡组 > 60 与额外/副卡组 > 15
（主+额外 > 250 那条用不着单独查：前两条成立时最多 75 张）。**换内核（换 ygopro 构建）
之后要重跑本工具重新量**——那三个数字都是实测出来的，协议里没写。

做法：起一个真房间（内核 + 闸门），再拉两个 WindBot（探针用按张数现攒的卡表，陪打用
WindBot 自带卡组），从闸门的旁路观测里看探针那份 ``CTOS_UPDATE_DECK`` 的张数、
内核回的 ``STOC_DECK_COUNT``（对局内实际张数）、有没有 ``STOC_ERROR_MSG``、
有没有走到 ``STOC_DUEL_START``。

用法::

    uv run python tools/probe_deck_size.py                       # 默认量 235/236（边界两侧）
    uv run python tools/probe_deck_size.py --size 60 --size 61   # 量对局引擎那条 60
    uv run python tools/probe_deck_size.py --template clients/windbot/Decks/AI_BlueEyes.ydk

``--size`` 填的是**主卡组张数**，额外/副卡组按模板原样保留——报障时看到的张数是"主+额外"，
所以工具会把报文里的实际张数一起打出来。口令在运行时随机生成（源码里不放字面量）；
每个用例都自己起一套进程，互不影响。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

import argparse
import asyncio
import logging
import secrets
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.gate import DuelGate, ROLE_BOT, ROLE_HUMAN  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.protocol import Ctos, Frame, Stoc  # noqa: E402
from duel.room import RoomSettings, WindBotProcess, WindBotSettings, YgoProRoom  # noqa: E402

YGOPRO_DIR = _PLUGIN_ROOT / "clients" / "ygopro"
WIND_BOT_EXE = _PLUGIN_ROOT / "clients" / "windbot" / "WindBot.exe"
CARDS_CDB = YGOPRO_DIR / "cards.cdb"
DEFAULT_TEMPLATE = _PLUGIN_ROOT / "clients" / "windbot" / "Decks" / "AI_BlueEyes.ydk"
# 探针卡表放在插件目录下的 temp/（跑完可以删，不影响池子与真实对局）
DECK_DIR = _PLUGIN_ROOT / "temp"

# 每个用例观测到这些时刻就够下结论：被拒的用例 1 秒内就回错误报文，能开的用例
# 两三秒内也会走到 DUEL_START
_WATCH_POINTS = ((2, 2), (6, 8))


def frame_name(message_id: int, direction: str) -> str:
    """报文号 → 名字（``to_server`` 查 CTOS 表，``to_client`` 查 STOC 表）。"""

    table = Ctos if direction == "to_server" else Stoc
    try:
        return table(message_id).name
    except ValueError:
        return f"?0x{message_id:x}"


def make_deck(target_main: int, template: Path) -> Path:
    """按 ``template`` 攒一副主卡组恰好 ``target_main`` 张的卡表（额外/副卡组原样保留）。

    填充用模板里第一张主卡反复复制：内核判的是**张数**，填哪张无所谓，但**必须是卡库里真有的
    卡号**——WindBot 的 ``Deck.Load`` 会把查不到的卡号直接丢掉，卡组就悄悄少几张；
    张数对不上时量出来的"边界"是假的（第一次探针就踩了这个）。
    """

    main: List[str] = []
    extra: List[str] = []
    side: List[str] = []
    bucket = main
    for line in template.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text == "#extra":
            bucket = extra
            continue
        if text == "!side":
            bucket = side
            continue
        if not text or text.startswith("#"):
            continue
        bucket.append(text)
    if not main:
        raise SystemExit(f"{template} 里没有主卡组卡号")
    while len(main) < target_main:
        main.append(main[0])
    main = main[:target_main]

    path = DECK_DIR / f"probe-main{target_main}.ydk"
    DECK_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(["#created by probe_deck_size", "#main", *main, "#extra", *extra, "!side", *side]) + "\n",
        encoding="utf-8",
    )
    return path


def decode_frame(direction: str, frame: Frame) -> str:
    """把报文里有用的字段解出来：卡组张数与内核的卡组错误码。"""

    if direction == "to_server" and frame.message_id == Ctos.UPDATE_DECK and len(frame.payload) >= 8:
        main_extra = int.from_bytes(frame.payload[0:4], "little")
        side_count = int.from_bytes(frame.payload[4:8], "little")
        return f" → 主+额外={main_extra} 张、副卡组={side_count} 张"
    if direction == "to_client" and frame.message_id == Stoc.DECK_COUNT and len(frame.payload) >= 12:
        # 三个 int32：对局里这副牌实际的主卡组 / 额外 / 副卡组张数（内核对局内用哪副牌以它为准）
        counts = [int.from_bytes(frame.payload[index * 4 : index * 4 + 4], "little") for index in range(3)]
        return f" → 对局内主/额外/副 = {counts[0]}/{counts[1]}/{counts[2]}"
    if direction == "to_client" and frame.message_id == Stoc.ERROR_MSG and len(frame.payload) >= 8:
        msg = frame.payload[0]
        pcode = int.from_bytes(frame.payload[4:8], "little")
        label = "DECKERROR（内核不收这副卡组）" if msg == 2 else f"msg={msg}"
        return f" → {label}，附带码 0x{pcode:08x}"
    return ""


async def run_case(label: str, deck_file: Path, max_deck_size: int, logger: logging.Logger) -> None:
    """跑一个用例：内核 + 闸门 + 探针 bot + 陪打 bot。"""

    frames: List[str] = []
    room_password = secrets.token_urlsafe(9)
    bot_password = secrets.token_urlsafe(9)

    def on_frame(role: str, direction: str, frame: Frame) -> None:
        """闸门的旁路观测回调（同步：闸门不会 await 它）。"""

        decoded = decode_frame(direction, frame)
        if not decoded and "DECK" not in frame_name(frame.message_id, direction):
            # 只留能说明问题的报文：卡组登记、内核回错、以及对局是否开起来
            if frame_name(frame.message_id, direction) not in ("DUEL_START", "DUEL_END"):
                return
        frames.append(
            f"{role} {direction} {frame_name(frame.message_id, direction)} {decoded}".strip()
        )

    logger.info("")
    logger.info("==== 用例 %s：%s｜MaxDeckSize=%s", label, deck_file.name, max_deck_size or "(不传)")
    room = YgoProRoom(YGOPRO_DIR / "ygopro.exe", YGOPRO_DIR, RoomSettings(save_replay=False), logger=logger)
    gate: Optional[DuelGate] = None
    probe: Optional[WindBotProcess] = None
    mate: Optional[WindBotProcess] = None
    try:
        gate = DuelGate(
            backend_host="127.0.0.1",
            backend_port=await room.start(),
            public_password=room_password,
            bot_password=bot_password,
            observe_roles=(ROLE_BOT, ROLE_HUMAN),
            on_frame=on_frame,
            logger=logger,
        )
        gate_port = await gate.start(listen_host="127.0.0.1", listen_port=0)
        probe = WindBotProcess(
            WIND_BOT_EXE,
            WIND_BOT_EXE.parent,
            WindBotSettings(
                name="探针bot",
                deck="Test",
                deck_file=deck_file,
                password=bot_password,
                db_path=CARDS_CDB,
                chat=False,
                max_deck_size=max_deck_size,
            ),
            logger=logger,
        )
        await probe.start("127.0.0.1", gate_port)
        mate = WindBotProcess(
            WIND_BOT_EXE,
            WIND_BOT_EXE.parent,
            WindBotSettings(name="陪打", deck="Blue-Eyes", password=room_password, db_path=CARDS_CDB, chat=False),
            logger=logger,
        )
        await mate.start("127.0.0.1", gate_port)
        for wait, elapsed in _WATCH_POINTS:
            await asyncio.sleep(wait)
            logger.info("   t=%2ss 探针bot存活=%s 陪打存活=%s", elapsed, probe.running, mate.running)
    finally:
        for process in (mate, probe):
            if process is not None:
                await process.stop()
        if gate is not None:
            await gate.stop()
        await room.stop()

    logger.info("   报文：%s", "；".join(dict.fromkeys(frames)) or "（没观测到关键报文）")


async def main() -> int:
    parser = argparse.ArgumentParser(description="量内核收得下的卡组张数上限")
    parser.add_argument("--size", type=int, action="append", dest="sizes", help="要试的主卡组张数（可多次）")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="攒卡表用的模板 .ydk")
    args = parser.parse_args()

    logger = logging.getLogger("probe_deck_size")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    for path in (YGOPRO_DIR / "ygopro.exe", WIND_BOT_EXE, CARDS_CDB, args.template):
        if not path.is_file():
            print(f"❌ 缺少 {path}")
            return 2
    sizes: Sequence[int] = args.sizes or (235, 236)
    for size in sizes:
        # 超过 60 张要给 WindBot 传放行上限，否则它自己就把整副牌丢了，量出来的边界是假的
        await run_case(f"main{size}", make_deck(size, args.template), size if size > 60 else 0, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
