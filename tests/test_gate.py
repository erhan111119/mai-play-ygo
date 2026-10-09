"""闸门集成测试：真起 TCP 服务，用假后端与假客户端验证行为。

直接用 ``python tests/test_gate.py`` 运行，也可以用 pytest 收集。

测试口令一律运行时随机生成，不放字面量——一来避免源码里出现看起来像凭据的字符串，
二来能顺带证明闸门是按实际收到的口令匹配，而不是撞上了某个固定值。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import asyncio
import struct
import sys
import uuid

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.gate import ROLE_BOT, ROLE_HUMAN, DuelGate  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.protocol import (  # noqa: E402
    Ctos,
    Frame,
    Msg,
    Stoc,
    encode_frame,
)


# 每轮测试随机生成的口令
PUBLIC_PASSWORD = f"room-{uuid.uuid4().hex[:10]}"
BOT_PASSWORD = f"bot-{uuid.uuid4().hex[:10]}"


def build_handshake(name: str, password: str) -> bytes:
    """拼出一个客户端加入房间时实际会发的握手字节串。

    顺序与真实客户端一致：先报昵称，再发加入房间报文。
    """

    player_info = encode_frame(Ctos.PLAYER_INFO, name.encode("utf-16-le").ljust(40, b"\x00"))
    join_payload = (
        struct.pack("<H", 0x1362)  # version
        + b"\x00\x00"  # padding
        + struct.pack("<I", 0)  # gameid
        + password.encode("utf-16-le").ljust(40, b"\x00")  # pass[20]
    )
    return player_info + encode_frame(Ctos.JOIN_GAME, join_payload)


class FakeBackend:
    """冒充 ygopro.exe 的后端进程，只做收发计数。"""

    def __init__(self) -> None:
        self.server: Optional[asyncio.AbstractServer] = None
        self.port = 0
        self.connection_count = 0
        self.received = bytearray()
        self.outgoing: List[bytes] = []
        self._writers: List[asyncio.StreamWriter] = []

    async def start(self) -> None:
        """在随机端口上监听。"""

        self.server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        sockets = self.server.sockets or ()
        self.port = sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """连接建立后先把预设的帧发出去，然后一直记录收到的字节。"""

        self.connection_count += 1
        self._writers.append(writer)
        try:
            for frame in self.outgoing:
                writer.write(frame)
            await writer.drain()
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                self.received.extend(chunk)
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            return

    async def stop(self) -> None:
        """关闭所有连接与监听。"""

        for writer in self._writers:
            writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def read_with_timeout(reader: asyncio.StreamReader, timeout: float = 2.0) -> bytes:
    """带超时地读取一次数据，超时返回空字节串。"""

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


async def test_wrong_password_is_rejected() -> None:
    """口令不对的连接必须被断开，且不会连到后端。"""

    backend = FakeBackend()
    await backend.start()
    statuses: List[Tuple[str, dict]] = []
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password=PUBLIC_PASSWORD,
        bot_password=BOT_PASSWORD,
        on_status=lambda event, payload: statuses.append((event, payload)),
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        writer.write(build_handshake("路人", PUBLIC_PASSWORD + "-猜错"))
        await writer.drain()

        # 闸门应当直接断开，客户端读到 EOF
        assert await read_with_timeout(reader) == b""
        assert gate.status.rejected_count == 1
        assert backend.connection_count == 0, "被拒绝的连接不该连到后端"
        assert any(event == "client_rejected" for event, _ in statuses)
        writer.close()
    finally:
        await gate.stop()
        await backend.stop()


async def test_bot_and_human_roles_and_observation() -> None:
    """bot 口令识别角色、玩家口令放行，且只有 bot 的连接被观测。"""

    backend = FakeBackend()
    await backend.start()
    # 后端在连接建立时就推一条 NEW_TURN，模拟对局开始
    backend.outgoing.append(encode_frame(Stoc.GAME_MSG, bytes([int(Msg.NEW_TURN), 0])))

    observed: List[Tuple[str, str, Frame]] = []
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password=PUBLIC_PASSWORD,
        bot_password=BOT_PASSWORD,
        on_frame=lambda role, direction, frame: observed.append((role, direction, frame)),
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        # bot 先连进来
        bot_reader, bot_writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        bot_writer.write(build_handshake("WindBot", BOT_PASSWORD))
        await bot_writer.drain()
        assert await read_with_timeout(bot_reader), "bot 应当收到后端推来的帧"

        # 玩家用公开口令连进来
        human_reader, human_writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        human_writer.write(build_handshake("群友A", PUBLIC_PASSWORD))
        await human_writer.drain()
        assert await read_with_timeout(human_reader), "玩家应当收到后端推来的帧"

        await asyncio.sleep(0.1)
        clients = gate.status.clients
        assert len(clients) == 2
        roles = sorted(client.role for client in clients)
        assert roles == [ROLE_BOT, ROLE_HUMAN], roles
        names = {client.role: client.name for client in clients}
        assert names[ROLE_BOT] == "WindBot"
        assert names[ROLE_HUMAN] == "群友A"

        # 观测只落在 bot 的连接上，避免同一局事件被记两遍
        assert observed, "应当观测到后端发来的帧"
        assert {role for role, _, _ in observed} == {ROLE_BOT}
        assert all(direction == "to_client" for _, direction, _ in observed)
        assert observed[0][2].message_id == Stoc.GAME_MSG

        # 客户端发起的字节要原样到达后端（含握手缓冲）
        bot_writer.write(encode_frame(Ctos.HS_READY))
        await bot_writer.drain()
        await asyncio.sleep(0.15)
        assert encode_frame(Ctos.HS_READY) in bytes(backend.received)
        assert "WindBot".encode("utf-16-le") in bytes(backend.received), "握手缓冲必须原样转发"

        bot_writer.close()
        human_writer.close()
    finally:
        await gate.stop()
        await backend.stop()


async def test_kernel_deck_error_is_reported() -> None:
    """内核拒收卡组的报文要被记下来并上报（否则"bot 进不去"完全没有线索）。

    实测场景：卡组张数超过内核上限时，内核回 ``STOC_ERROR_MSG``（msg=2 DECKERROR）后断开
    连接，WindBot 那边是静默退出——插件能看到的只有这条报文。
    """

    backend = FakeBackend()
    await backend.start()
    backend.outgoing.append(encode_frame(Stoc.ERROR_MSG, bytes([2]) + b"\x00\x00\x00" + struct.pack("<i", 0)))
    events: List[Tuple[str, dict]] = []
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password="",
        bot_password=BOT_PASSWORD,
        on_status=lambda event, payload: events.append((event, payload)),
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        writer.write(build_handshake("WindBot", BOT_PASSWORD))
        await writer.drain()
        await asyncio.sleep(0.2)

        deck_errors = [payload for event, payload in events if event == "deck_error"]
        assert len(deck_errors) == 1, events
        assert deck_errors[0]["role"] == ROLE_BOT
        assert deck_errors[0]["pcode"] == 0
        writer.close()
    finally:
        await gate.stop()
        await backend.stop()


async def test_open_room_accepts_any_password() -> None:
    """没配公开口令时按玩家放行，方便本机做可行性实验。"""

    backend = FakeBackend()
    await backend.start()
    gate = DuelGate(backend_host="127.0.0.1", backend_port=backend.port, public_password="")
    await gate.start(listen_host="127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        writer.write(build_handshake("谁都能进", uuid.uuid4().hex[:6]))
        await writer.drain()
        await asyncio.sleep(0.15)
        status = gate.status
        assert len(status.clients) == 1, "空口令配置下应当放行"
        assert status.clients[0].role == ROLE_HUMAN
        writer.close()
    finally:
        await gate.stop()
        await backend.stop()


async def test_handshake_timeout() -> None:
    """连上却不发握手报文的连接要超时断开，不能一直占着槽位。"""

    backend = FakeBackend()
    await backend.start()
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password="",
        handshake_timeout=0.4,
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        # 故意什么都不发
        assert await read_with_timeout(reader, timeout=2.0) == b""
        assert backend.connection_count == 0
        writer.close()
    finally:
        await gate.stop()
        await backend.stop()


async def test_client_chat_is_not_mistaken_for_duel_end() -> None:
    """客户端发来的聊天帧不能被当成对局结束。

    ``CTOS_CHAT`` 与 ``STOC_DUEL_END`` 都是 0x16，``CTOS_LEAVE_GAME`` 与
    ``STOC_TYPE_CHANGE`` 都是 0x13——CTOS 与 STOC 号段大量重叠，闸门只能按方向区分语义。
    真实的 WindBot 一进房间就会发聊天，所以这条必须钉住。
    """

    backend = FakeBackend()
    await backend.start()
    backend.outgoing.append(encode_frame(Stoc.DUEL_START, b""))
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password="",
        bot_password=BOT_PASSWORD,
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        writer.write(build_handshake("WindBot", BOT_PASSWORD))
        await writer.drain()
        await read_with_timeout(reader)
        await asyncio.sleep(0.15)
        assert gate.status.duel_started, "应当已识别到对局开始"
        assert not gate.status.duel_finished

        # 发一条聊天：数值上与 STOC_DUEL_END 相同，方向相反
        writer.write(encode_frame(Ctos.CHAT, b"hello from bot"))
        await writer.drain()
        await asyncio.sleep(0.2)

        assert not gate.status.duel_finished, "客户端聊天被误判成了对局结束"
        assert gate.status.duel_started, "对局状态不该被客户端帧改写"
        await close_stream(writer)
    finally:
        await gate.stop()
        await backend.stop()


async def test_send_chat_reaches_backend() -> None:
    """闸门能以 bot 身份在游戏内发话，且负载格式要对。

    这是「让模型生成台词」的传输基础：台词不是 WindBot 发的，而是闸门直接往
    bot 那条连接的内核侧写入一条 CTOS_CHAT。
    """

    backend = FakeBackend()
    await backend.start()
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password="",
        bot_password=BOT_PASSWORD,
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        # 没有连接时发不出去
        assert await gate.send_chat("还没人") is False

        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        writer.write(build_handshake("WindBot", BOT_PASSWORD))
        await writer.drain()
        await read_with_timeout(reader)
        await asyncio.sleep(0.15)
        assert gate.status.bot_connected

        assert await gate.send_chat("召唤词测试") is True
        await asyncio.sleep(0.2)
        received = bytes(backend.received)
        assert "召唤词测试".encode("utf-16-le") in received, "台词没有以 UTF-16LE 到达内核"

        # 帧头语义：长度 = payload + 1，消息号是 CTOS_CHAT(0x16)
        marker = "召唤词测试".encode("utf-16-le")
        offset = received.index(marker)
        (length_field,) = struct.unpack_from("<H", received, offset - 3)
        assert received[offset - 1] == int(Ctos.CHAT)
        # 负载 = 文本 + NUL 终止符（内核要求，缺了会被静默丢弃）
        assert length_field == len(marker) + 2 + 1

        # 以不在房间里的角色发话应当失败，而不是静默发成 bot 的
        assert await gate.send_chat("冒充玩家", role="human") is False
        # 超长台词要被拦下
        assert await gate.send_chat("长" * 300) is False
        await close_stream(writer)
    finally:
        await gate.stop()
        await backend.stop()


async def test_duel_lifecycle_and_seat_tracking() -> None:
    """对局开始/结束与座位号应当由闸门自己从报文里维护。"""

    backend = FakeBackend()
    await backend.start()
    backend.outgoing.extend(
        [
            encode_frame(Stoc.TYPE_CHANGE, bytes([0x10])),  # 座位 0 且是房主
            encode_frame(
                Stoc.HS_PLAYER_ENTER, "群友A".encode("utf-16-le").ljust(40, b"\x00") + bytes([0])
            ),
            encode_frame(Stoc.DUEL_START, b""),
            encode_frame(Stoc.GAME_MSG, bytes([int(Msg.WIN), 0, 0])),
            encode_frame(Stoc.DUEL_END, b""),
        ]
    )
    statuses: List[str] = []
    gate = DuelGate(
        backend_host="127.0.0.1",
        backend_port=backend.port,
        public_password="",
        bot_password=BOT_PASSWORD,
        on_status=lambda event, payload: statuses.append(event),
    )
    await gate.start(listen_host="127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gate.listening_port)
        writer.write(build_handshake("WindBot", BOT_PASSWORD))
        await writer.drain()
        await read_with_timeout(reader)
        await asyncio.sleep(0.2)

        status = gate.status
        assert status.duel_started, "应当识别到对局开始"
        assert status.duel_finished, "应当识别到对局结束"
        assert len(status.clients) == 1
        client = status.clients[0]
        assert client.seat == 0, client.seat
        assert client.is_host, "高 4 位非零表示房主"
        assert "duel_started" in statuses
        assert "duel_ended" in statuses
        writer.close()
    finally:
        await gate.stop()
        await backend.stop()


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
