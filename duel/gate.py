"""对局闸门：房间里唯一的对外入口。

它做三件事，全部在一条 TCP 连接上完成：

1. **口令校验**：客户端连进来后先发握手报文，其中 ``CTOS_JOIN_GAME`` 的 pass 字段就是
   玩家在「加入房间」界面密码框里填的内容。闸门在放行之前先核对它，不匹配就直接断开，
   这样「房间密码」才真正有意义（ygopro.exe 自身的单房间模式并不校验口令）。
2. **双向透传**：校验通过后把字节原样转接到后端的 ``ygopro.exe``，不做任何改写，
   所以客户端不会察觉到中间有一层。
3. **旁路观测**：透传的同时按协议分帧解析报文，把对局过程交给外部回调（记录器）。
   观测默认只挂在 bot 自己那条连接上，否则同一局的事件会被记两遍。

状态（谁进来了、对局是否开始/结束、座位号）由闸门自己在观测路径上维护，不需要调用方
另外接线，避免出现「忘了更新状态」这类不一致。

口令字段的偏移依据是服务端 ``gframe/network.h`` 的 ``CTOS_JoinGame`` 结构体与 WindBot
发送端代码的双重核对，见 :func:`duel.protocol.parse_join_game_password`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import asyncio
import contextlib
import logging
import time

from .protocol import (
    Ctos,
    Frame,
    FrameDecoder,
    ProtocolError,
    Stoc,
    encode_chat_payload,
    encode_frame,
    parse_join_game_password,
    parse_join_game_version,
    parse_player_enter,
    parse_player_info_name,
    parse_stoc_error,
    parse_type_change,
)


# 连接角色
ROLE_BOT = "bot"
ROLE_HUMAN = "human"

# ``STOC_ERROR_MSG`` 的消息号：内核不收这副卡组（WindBot 的 ``OnErrorMsg`` 里写的
# ``ERRMSG_DECKERROR``）。实测张数超过内核上限时就是这个，随后内核断开这条连接、
# WindBot 静默退出——所以这条报文是"房间起不来"的唯一线索，必须记下来。
_ERRMSG_DECKERROR = 2

# 透传时单次读取的字节数
_READ_CHUNK = 65536

# 握手阶段允许积压的最大字节数，避免有人连上后狂发数据把内存撑爆
_MAX_HANDSHAKE_BYTES = 64 * 1024

# 关闭闸门时等待连接任务收尾的上限秒数（宿主只给插件约 1.5 秒做卸载）
_SHUTDOWN_GRACE_SECONDS = 1.0

# 关闭单条连接时等待其真正断开的上限秒数
_CLOSE_GRACE_SECONDS = 1.0


class GateError(RuntimeError):
    """闸门层错误。"""


@dataclass
class GateClient:
    """一条已接入或正在接入的客户端连接。"""

    index: int
    role: str
    peer: str
    connected_at: float
    name: str = ""
    seat: Optional[int] = None
    is_host: bool = False
    accepted: bool = True
    rejected_reason: str = ""

    def describe(self) -> str:
        """人类可读的连接描述。"""

        label = {ROLE_BOT: "机器人", ROLE_HUMAN: "玩家"}.get(self.role, "未知")
        name = self.name or "未报名"
        if self.seat is not None:
            return f"{label} {name}（座位{self.seat}）"
        return f"{label} {name}"


@dataclass
class GateStatus:
    """闸门的对外状态快照。"""

    listening_port: int = 0
    clients: List[GateClient] = field(default_factory=list)
    duel_started: bool = False
    duel_finished: bool = False
    rejected_count: int = 0

    @property
    def human_clients(self) -> List[GateClient]:
        """已接入的玩家连接。"""

        return [client for client in self.clients if client.role == ROLE_HUMAN and client.accepted]

    @property
    def bot_connected(self) -> bool:
        """bot 的连接是否已就位。"""

        return any(client.role == ROLE_BOT and client.accepted for client in self.clients)


class DuelGate:
    """房间闸门。

    Args:
        backend_host: 后端 ``ygopro.exe`` 的地址。
        backend_port: 后端 ``ygopro.exe`` 的端口。
        public_password: 发给群友的房间密码；留空表示不校验口令，任何口令都按玩家放行
            （此时「房间密码」失去了保护作用，只做入口说明）。
        bot_password: bot 自己用的口令，用于把 bot 的连接与群友区分开。
        observe_roles: 需要旁路观测的连接角色。
        on_frame: 观测回调，参数为 ``(角色, 方向, 帧)``，方向取值 ``"to_client"`` / ``"to_server"``。
        on_status: 状态回调，参数为 ``(事件名, 数据字典)``。
        handshake_timeout: 客户端连上后必须在多少秒内完成握手。
        logger: 可选的日志器。
    """

    def __init__(
        self,
        *,
        backend_host: str,
        backend_port: int,
        public_password: str = "",
        bot_password: str = "",
        observe_roles: Sequence[str] = (ROLE_BOT,),
        on_frame: Optional[Callable[[str, str, Frame], None]] = None,
        on_status: Optional[Callable[[str, Dict[str, object]], None]] = None,
        handshake_timeout: float = 20.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._backend_host = backend_host
        self._backend_port = backend_port
        self._public_password = public_password
        self._bot_password = bot_password
        self._observe_roles = tuple(observe_roles)
        self._on_frame = on_frame
        self._on_status = on_status
        self._handshake_timeout = handshake_timeout
        self._logger = logger or logging.getLogger(__name__)

        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: set = set()
        self._clients: Dict[int, GateClient] = {}
        # 各客户端到后端内核的连接，用于以某个玩家的身份主动发话（游戏内台词）
        self._backend_writers: Dict[int, asyncio.StreamWriter] = {}
        self._next_index = 0
        self._listening_port = 0
        self._duel_started = False
        self._duel_finished = False
        self._rejected_count = 0

    @property
    def listening_port(self) -> int:
        """实际监听端口，``start`` 之后才有意义。"""

        return self._listening_port

    @property
    def status(self) -> GateStatus:
        """当前状态快照。"""

        return GateStatus(
            listening_port=self._listening_port,
            clients=list(self._clients.values()),
            duel_started=self._duel_started,
            duel_finished=self._duel_finished,
            rejected_count=self._rejected_count,
        )

    async def start(self, *, listen_host: str = "0.0.0.0", listen_port: int = 0) -> int:
        """开始监听，返回实际使用的端口。

        Args:
            listen_host: 监听地址，默认所有网卡（群友要从其它机器连进来）。
            listen_port: 监听端口，传 0 表示由系统分配空闲端口。
        """

        if self._server is not None:
            raise GateError("闸门已经启动")
        self._server = await asyncio.start_server(
            self._handle_client, host=listen_host, port=listen_port
        )
        sockets = self._server.sockets or ()
        if sockets:
            self._listening_port = sockets[0].getsockname()[1]
        self._logger.info("闸门已启动，监听端口 %s", self._listening_port)
        self._emit("gate_listening", {"port": self._listening_port})
        return self._listening_port

    async def stop(self) -> None:
        """关闭监听并断开所有转发连接。

        注意顺序：必须**先取消**连接处理任务，再关监听、再等 ``wait_closed``。
        Python 3.12 的 ``Server.wait_closed()`` 会等到所有连接处理函数返回为止，
        如果先等它、而某个客户端还连着，这里就会永久挂住——插件卸载时只给 1.5 秒，
        那样会把整个插件进程拖死。
        """

        pending = list(self._tasks)
        for task in pending:
            task.cancel()

        if self._server is not None:
            self._server.close()
            self._server = None

        if pending:
            # 处理函数在自己的 finally 里关闭 socket，正常都能立刻结束；
            # 留一个上限只是防止外部库把取消吞掉，超时则如实记录。
            done, still_pending = await asyncio.wait(pending, timeout=_SHUTDOWN_GRACE_SECONDS)
            if still_pending:
                self._logger.warning("闸门关闭时有 %s 个连接任务未在限时内结束", len(still_pending))
            self._tasks.clear()

        self._clients.clear()
        self._logger.info("闸门已关闭")

    def kill_now(self) -> None:
        """立刻关掉监听与所有连接：同步、不等收尾、不写日志。

        给插件卸载用——``stop()`` 会等连接任务结束（有上限，但叠加 WindBot 与内核的等待
        仍会超出宿主给卸载的 5 秒预算，超时会被判"卸载失败"并重启插件）。
        卸载时只要求"端口与连接立刻断干净"，不需要收尾日志，所以这里直接关。
        """

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        self._clients.clear()

    async def send_chat(self, text: str, *, role: str = ROLE_BOT) -> bool:
        """以某个玩家的身份在游戏内说一句话。

        做法是往「该客户端 → 后端内核」那条连接里写入一条 ``CTOS_CHAT``，效果与这个玩家自己
        在客户端里打字完全一致，内核会把它广播给房间里其他人。这样麦麦的台词就不必依赖
        WindBot 自带的固化台词包，可以由模型按当前局势生成。

        Args:
            text: 要说的内容。
            role: 以谁的身份发话，默认是 bot 自己。

        Returns:
            是否成功发出（目标角色不在房间里时返回 False）。
        """

        writer: Optional[asyncio.StreamWriter] = None
        for index, client in self._clients.items():
            if client.role == role and client.accepted:
                writer = self._backend_writers.get(index)
                break
        if writer is None:
            return False
        try:
            payload = encode_chat_payload(text)
        except ProtocolError as exc:
            self._logger.warning("台词未能发出：%s", exc)
            return False
        try:
            writer.write(encode_frame(Ctos.CHAT, payload))
            await writer.drain()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            return False
        return True

    # ---------------------------------------------------------------- 内部实现

    def _emit(self, event: str, payload: Dict[str, object]) -> None:
        """触发状态回调；回调属于外部代码，异常只记日志、不影响转发。"""

        if self._on_status is None:
            return
        try:
            self._on_status(event, payload)
        except Exception:  # noqa: BLE001  外部回调不应带崩闸门
            self._logger.exception("闸门状态回调失败：%s", event)

    def _observe(self, client: GateClient, direction: str, frame: Frame) -> None:
        """观测一条报文：先更新闸门自身状态，再交给外部回调。

        **只有「服务器 → 客户端」方向的帧才按 STOC 解释。** CTOS 与 STOC 的号段大量重叠
        （CTOS_CHAT 与 STOC_DUEL_END 都是 0x16、CTOS_LEAVE_GAME 与 STOC_TYPE_CHANGE 都是
        0x13、CTOS_TIME_CONFIRM 与 STOC_DUEL_START 都是 0x15），不加方向判断就会把客户端
        发来的聊天当成对局结束。真实 WindBot 一进房间就会发聊天，这个坑一定会踩到。
        """

        if client.role not in self._observe_roles:
            return
        if direction == "to_client":
            self._update_state(client, frame)
        if self._on_frame is None:
            return
        try:
            self._on_frame(client.role, direction, frame)
        except Exception:  # noqa: BLE001  同上
            self._logger.exception("报文观测回调失败：消息号=%s", frame.message_id)

    def _update_state(self, client: GateClient, frame: Frame) -> None:
        """从观测到的报文里更新连接状态与对局阶段。"""

        message_id = frame.message_id
        try:
            if message_id == Stoc.TYPE_CHANGE:
                seat, is_host = parse_type_change(frame.payload)
                client.seat = seat
                client.is_host = is_host
                self._emit("client_seated", {"client": client.describe(), "seat": seat})
            elif message_id == Stoc.HS_PLAYER_ENTER:
                name, seat = parse_player_enter(frame.payload)
                # 该报文描述的是任意座位的玩家，只有座位号能对上时才算自己的名字
                if client.seat == seat and not client.name:
                    client.name = name
                self._emit("player_entered", {"seat": seat, "name": name})
            elif message_id == Stoc.DUEL_START:
                if not self._duel_started:
                    self._duel_started = True
                    self._emit("duel_started", {"port": self._listening_port})
            elif message_id == Stoc.DUEL_END:
                if not self._duel_finished:
                    self._duel_finished = True
                    self._emit("duel_ended", {"port": self._listening_port})
            elif message_id == Stoc.ERROR_MSG:
                msg, pcode = parse_stoc_error(frame.payload)
                if msg == _ERRMSG_DECKERROR:
                    # 内核不收这个客户端注册的卡组：它接着就会断开这条连接（bot 那侧是静默退出，
                    # 日志里什么都看不到），所以这里必须自己记一条，会话那边才能说出原因。
                    self._logger.warning(
                        "内核拒收了 %s 的卡组（DECKERROR，附带码 0x%08x）", client.describe(), pcode
                    )
                    self._emit(
                        "deck_error",
                        {"client": client.describe(), "role": client.role, "pcode": pcode},
                    )
        except ProtocolError as exc:
            self._logger.warning("状态报文解析失败：%s", exc)

    def _resolve_role(self, password: str) -> Optional[str]:
        """根据 pass 字段判断连接角色，返回 None 表示口令错误。"""

        if self._bot_password and password == self._bot_password:
            return ROLE_BOT
        if not self._public_password:
            return ROLE_HUMAN
        if password == self._public_password:
            return ROLE_HUMAN
        return None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """处理一条客户端连接：先握手校验，再转接后端。"""

        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        client = GateClient(
            index=self._next_index,
            role="unknown",
            peer=self._format_peer(writer),
            connected_at=time.time(),
        )
        self._next_index += 1
        accepted = False
        try:
            accepted = await self._serve_client(client, reader, writer)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001  单条连接的意外不能影响其它连接，但必须留痕
            self._logger.exception("处理客户端连接时出现未预期错误：%s", client.peer)
        finally:
            self._clients.pop(client.index, None)
            await self._close_writer(writer)
            if task is not None:
                self._tasks.discard(task)
            if accepted:
                self._emit("client_left", {"client": client.describe(), "role": client.role})

    @staticmethod
    def _format_peer(writer: asyncio.StreamWriter) -> str:
        """取对端地址用于日志展示。"""

        peer = writer.get_extra_info("peername")
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return str(peer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        """关闭一个 writer 并等它真正关掉。

        只调 ``close()`` 不等的话，transport 会一直挂到垃圾回收时才释放，
        进程退出时就会刷一堆 unclosed transport 警告，长跑时也会攒下句柄。
        """

        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=_CLOSE_GRACE_SECONDS)

    async def _serve_client(
        self, client: GateClient, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """握手校验通过后建立到后端的转接。

        Returns:
            连接是否通过校验并被正常服务过。
        """

        handshake = await self._read_handshake(client, reader)
        if handshake is None:
            return False
        role, buffered = handshake

        client.role = role
        self._clients[client.index] = client
        self._emit(
            "client_joined",
            {"client": client.describe(), "role": role, "peer": client.peer, "name": client.name},
        )

        try:
            backend_reader, backend_writer = await asyncio.open_connection(
                self._backend_host, self._backend_port
            )
        except OSError as exc:
            self._emit("gate_error", {"error": f"连接后端对局进程失败：{exc}"})
            await self._close_writer(writer)
            return True

        try:
            self._backend_writers[client.index] = backend_writer
            # 把握手阶段缓冲的原始字节原样送进后端，避免重新编码引入差异
            if buffered:
                backend_writer.write(buffered)
                await backend_writer.drain()
            await asyncio.gather(
                self._pump(client, reader, backend_writer, direction="to_server"),
                self._pump(client, backend_reader, writer, direction="to_client"),
            )
        finally:
            self._backend_writers.pop(client.index, None)
            await self._close_writer(backend_writer)
        return True

    async def _read_handshake(
        self, client: GateClient, reader: asyncio.StreamReader
    ) -> Optional[Tuple[str, bytes]]:
        """读取并校验握手报文。

        握手期间收到的原始字节会被完整保留，校验通过后原样转发给后端，
        所以客户端不会因为中间隔了一层而看到不同的数据。

        Returns:
            ``(角色, 已缓冲的原始字节)``；被拒绝或客户端提前断开时返回 None。
        """

        decoder = FrameDecoder()
        buffered = bytearray()
        deadline = time.monotonic() + self._handshake_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._reject(client, "握手超时")
                return None
            try:
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK), timeout=remaining)
            except asyncio.TimeoutError:
                self._reject(client, "握手超时")
                return None
            if not chunk:
                # 还没发完 JOIN_GAME 就断开，不算被拒绝
                return None

            buffered.extend(chunk)
            if len(buffered) > _MAX_HANDSHAKE_BYTES:
                self._reject(client, "握手数据过大")
                return None

            try:
                frames = decoder.feed(chunk)
            except ProtocolError as exc:
                self._reject(client, f"协议数据异常：{exc}")
                return None

            for frame in frames:
                if frame.message_id == Ctos.PLAYER_INFO:
                    try:
                        client.name = parse_player_info_name(frame.payload)
                    except ProtocolError:
                        client.name = ""
                elif frame.message_id == Ctos.JOIN_GAME:
                    try:
                        password = parse_join_game_password(frame.payload)
                        version = parse_join_game_version(frame.payload)
                    except ProtocolError as exc:
                        self._reject(client, f"加入房间报文异常：{exc}")
                        return None
                    role = self._resolve_role(password)
                    if role is None:
                        self._reject(client, "房间密码不正确")
                        return None
                    self._emit(
                        "client_handshake",
                        {
                            "client": client.describe(),
                            "role": role,
                            "name": client.name,
                            "version": version,
                        },
                    )
                    return role, bytes(buffered)

    def _reject(self, client: GateClient, reason: str) -> None:
        """记录一条被拒绝的连接。"""

        client.accepted = False
        client.rejected_reason = reason
        self._rejected_count += 1
        self._logger.warning("拒绝连接 %s：%s", client.peer, reason)
        self._emit(
            "client_rejected",
            {"peer": client.peer, "reason": reason, "rejected_count": self._rejected_count},
        )

    async def _pump(
        self,
        client: GateClient,
        source: asyncio.StreamReader,
        target: asyncio.StreamWriter,
        *,
        direction: str,
    ) -> None:
        """单向透传，顺带按协议分帧做旁路解析。"""

        decoder = FrameDecoder()
        observing = client.role in self._observe_roles
        while True:
            try:
                chunk = await source.read(_READ_CHUNK)
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                return
            if not chunk:
                return
            try:
                target.write(chunk)
                await target.drain()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                return

            if not observing:
                continue
            try:
                frames = decoder.feed(chunk)
            except ProtocolError as exc:
                # 帧同步已丢失：停掉本连接的观测但继续转发，并把原因写进日志
                self._logger.warning("停止观测 %s（%s 方向）：%s", client.peer, direction, exc)
                observing = False
                continue
            for frame in frames:
                self._observe(client, direction, frame)
