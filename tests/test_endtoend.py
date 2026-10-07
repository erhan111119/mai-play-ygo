"""端到端链路测试：验证「起内核 → 起闸门 → 拉起 bot → 观测对局 → 复述 → 收摊」。

不依赖真实的 ygopro.exe / WindBot.exe，而是用 ``tests/fakes/`` 下的两个替身脚本扮演它们：
替身内核像真实内核一样把端口打印到 stdout 并回放一段固定对局报文；替身 bot 按 ``键=值``
参数连上房间并用 ``HostInfo`` 当口令握手。

于是本测试覆盖了除「真实内核算牌」之外的全部接线：进程启动、端口发现、闸门口令校验、
双向透传、旁路观测、胜负与过程提取、以及收摊时进程是否真的退出并释放端口。

Windows 上用 ``.cmd`` 包裹脚本，其它平台用 ``sh``。

直接用 ``python tests/test_endtoend.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List

import asyncio
import shutil
import sqlite3
import struct
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.cards import CardDatabase  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.session import (  # noqa: E402
    OUTCOME_FINISHED,
    DuelSession,
    SessionConfig,
    generate_password,
)

# 替身程序所在目录（就是测试临时目录里的那份拷贝）
_FAKES_DIR = Path(__file__).resolve().parent / "fakes"


def write_wrapper(root: Path, name: str, target: Path) -> Path:
    """按平台生成可执行包裹脚本，返回可执行文件路径。"""

    if sys.platform == "win32":
        wrapper = root / f"{name}.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{target}" %*\r\n', encoding="utf-8")
    else:
        wrapper = root / f"{name}.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{target}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def build_fake_environment(root: Path) -> None:
    """把替身脚本与内核需要的数据文件准备到临时目录。"""

    for name in ("fake_ygopro.py", "fake_windbot.py"):
        (root / name).write_text((_FAKES_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")

    (root / "script").mkdir(exist_ok=True)
    for index in range(110):
        (root / "script" / f"c{index}.lua").write_text("-- fake card script\n", encoding="utf-8")
    (root / "lflist.conf").write_text("!2026.1\n89631139 1\n", encoding="utf-8")

    connection = sqlite3.connect(root / "cards.cdb")
    connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO texts VALUES (89631139, '青眼白龙')")
    connection.commit()
    connection.close()


async def wait_for(predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    """轮询等待条件成立。"""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def build_handshake(name: str, password: str) -> bytes:
    """构造一条客户端加入房间时的握手字节串。"""

    player_info = struct.pack("<HB", 41, 0x10) + name.encode("utf-16-le").ljust(40, b"\x00")
    join_payload = (
        struct.pack("<H", 0x1362)
        + b"\x00\x00"
        + struct.pack("<I", 0)
        + password.encode("utf-16-le").ljust(40, b"\x00")
    )
    return player_info + struct.pack("<HB", len(join_payload) + 1, 0x12) + join_payload


async def read_with_timeout(reader: asyncio.StreamReader, timeout: float = 3.0) -> bytes:
    """带超时读一次，超时返回空字节串。"""

    try:
        return await asyncio.wait_for(reader.read(4096), timeout=timeout)
    except asyncio.TimeoutError:
        return b""


async def close_stream(writer: asyncio.StreamWriter) -> None:
    """关闭连接并等它真正关掉，避免退出时刷 unclosed transport 警告。"""

    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except (asyncio.TimeoutError, OSError):
        pass


async def shutdown_fakes(root: Path) -> None:
    """通知替身进程退出，等它们走完再清理目录。

    替身脚本是通过 .cmd/.sh 包裹启动的，terminate 只杀掉包裹进程本身，
    所以再放一个停止标记让真正的 Python 子进程自己退出。
    """

    (root / "stop.flag").write_text("stop", encoding="utf-8")
    for _ in range(20):
        await asyncio.sleep(0.15)
        if not any(root.iterdir()):
            return
    shutil.rmtree(root, ignore_errors=True)


def make_temp_root() -> Path:
    """建一个由测试自己负责清理的临时目录。"""

    return Path(tempfile.mkdtemp(prefix="duel-e2e-"))


def build_session(root: Path, **overrides: object) -> DuelSession:
    """用临时目录里的替身程序构造一个会话。"""

    cards_cdb = root / "cards.cdb"
    config = SessionConfig(
        ygopro_executable=write_wrapper(root, "ygopro", root / "fake_ygopro.py"),
        ygopro_dir=root,
        windbot_executable=write_wrapper(root, "windbot", root / "fake_windbot.py"),
        windbot_dir=root,
        cards_cdb=cards_cdb,
        bot_name="MaiBot",
        windbot_deck="Blue-Eyes",
        listen_host="127.0.0.1",
        join_timeout=6.0,
        max_duration=20.0,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return DuelSession(config, group_id="e2e", card_db=CardDatabase(cards_cdb))


async def test_full_chain() -> None:
    """整条外部链路的端到端验证。"""

    room_password = generate_password(8)
    root = make_temp_root()
    build_fake_environment(root)
    session = build_session(root, human_name_hint="群友A")
    try:
        info = await session.start(password=room_password)

        # 内核端口必须是从它输出里真实读到的那一个
        assert info.backend_port > 0, "没有从内核输出里读到端口"
        assert info.port > 0 and info.port != info.backend_port

        gate = session.gate
        assert gate is not None
        # bot 被识别为 bot 角色，说明它连的是闸门端口、且 HostInfo 与内部口令一致
        assert await wait_for(lambda: gate.status.bot_connected), "bot 没能通过闸门"

        # 口令不对的连接要被拒绝
        bad_reader, bad_writer = await asyncio.open_connection("127.0.0.1", info.port)
        bad_writer.write(build_handshake("路人", room_password + "-错"))
        await bad_writer.drain()
        assert await read_with_timeout(bad_reader) == b"", "口令错误应当被直接断开"
        assert gate.status.rejected_count == 1
        await close_stream(bad_writer)

        # 口令正确的群友进来，应当收到内核回放的报文
        reader, writer = await asyncio.open_connection("127.0.0.1", info.port)
        writer.write(build_handshake("群友A", room_password))
        await writer.drain()
        assert await read_with_timeout(reader, timeout=5.0), "群友应当收到房间报文"
        assert await wait_for(lambda: len(gate.status.human_clients) == 1), "群友没有进到房间列表"

        # 替身内核回放的对局以 MSG_WIN + DUEL_END 结束，会话应当就此收场
        assert await session.wait_finished() == OUTCOME_FINISHED

        summary = "\n".join(session.summary_lines())
        assert "群友A（对方） 获胜" in summary, summary
        assert "青眼白龙" in summary, "卡名应当被翻译出来：" + summary
        # 原因文案照内核 strings.conf 的 !victory 编号：替身发的是 0x2 → 「没有卡可抽」
        assert "没有卡可抽" in summary, "结束原因应当被翻译出来：" + summary
        assert "2400" in summary, "伤害数字应当出现在复述里：" + summary

        result = session.result_dict()
        assert result["winner_seat"] == 1
        assert result["resolved"] is True
        assert result["players"]["0"]["sp_summons"] == 1
        assert result["unparsed_frames"] == 0, "回放的报文应当全部可解析"

        await close_stream(writer)
    finally:
        await session.stop()
        await shutdown_fakes(root)


async def test_stop_releases_room_process() -> None:
    """收摊后房间进程必须真的退出，不能留下孤儿进程占着端口。"""

    root = make_temp_root()
    build_fake_environment(root)
    session = build_session(root, join_timeout=30.0, max_duration=30.0)
    try:
        info = await session.start(password=generate_password())
        assert session.room is not None and session.room.running
        await session.stop()
        assert not session.room.running, "收摊后房间进程仍在运行"
        # 端口必须已经释放：能重新绑定同一个端口
        probe = await asyncio.start_server(lambda _r, _w: None, host="127.0.0.1", port=info.port)
        probe.close()
        await probe.wait_closed()
    finally:
        await session.stop()
        await shutdown_fakes(root)


def main() -> int:
    """逐个执行测试；协程测试用 asyncio.run 驱动。"""

    tests = [(name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures: List[str] = []
    for name, func in tests:
        try:
            result = func()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
