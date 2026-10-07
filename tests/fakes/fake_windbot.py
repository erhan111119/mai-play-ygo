"""端到端测试用的替身 bot。

它模仿 ``WindBot.exe`` 在命令行模式下的行为：

1. 参数必须是 ``键=值`` 形式（真实 WindBot 遇到不含等号的参数会直接抛异常退出）；
2. 按 ``Host``/``Port`` 连上房间，用 ``HostInfo`` 作为口令发加入房间报文。

参数构造本身由 ``test_room.py`` 覆盖；而「插件把 bot 指向了哪个端口、给了什么口令」由
测试断言闸门侧的状态来验证——bot 能通过闸门的口令校验并被识别为 bot 角色，
就说明它连的是闸门端口而不是内核端口。所以这里不需要额外落盘任何文件。
"""

from __future__ import annotations

from typing import Any, Dict, Final, List

import socket
import struct
import sys


# 帧头与字段格式与 ygopro 协议一致
CTOS_PLAYER_INFO: Final = 0x10
CTOS_JOIN_GAME: Final = 0x12
CTOS_CHAT: Final = 0x16

# 与服务端 gframe/config.h 的 PRO_VERSION 一致
PROTOCOL_VERSION: Final = 0x1362


def encode_frame(message_id: int, payload: bytes = b"") -> bytes:
    """按协议打包一条帧。"""

    return struct.pack("<HB", len(payload) + 1, message_id) + payload


def encode_wide(text: str) -> bytes:
    """编码成 20 个 UTF-16 字符的定长字段。"""

    return text.encode("utf-16-le").ljust(40, b"\x00")


def parse_arguments(argv: List[str]) -> Dict[str, str]:
    """解析 ``键=值`` 形式的参数，键统一小写。"""

    fields: Dict[str, str] = {}
    for argument in argv:
        if "=" not in argument:
            raise SystemExit(f"替身 bot 只接受 键=值 参数，收到：{argument}")
        key, value = argument.split("=", 1)
        fields[key.strip().lower()] = value
    return fields


def main() -> None:
    """连上房间并保持连接。"""

    fields: Dict[str, Any] = parse_arguments(sys.argv[1:])
    host = fields.get("host", "127.0.0.1")
    port = int(fields.get("port", "0"))
    password = fields.get("hostinfo", "")

    sock = socket.create_connection((host, port), timeout=10)
    join_payload = (
        struct.pack("<H", PROTOCOL_VERSION)
        + b"\x00\x00"
        + struct.pack("<I", 0)
        + encode_wide(password)
    )
    sock.sendall(
        encode_frame(CTOS_PLAYER_INFO, encode_wide(fields.get("name", "WindBot")))
        + encode_frame(CTOS_JOIN_GAME, join_payload)
    )
    # 真实 WindBot 进房间后会发聊天（CTOS_CHAT=0x16，与 STOC_DUEL_END 同号）。
    # 这里照做，专门用来钉住「闸门不会把客户端聊天误判成对局结束」。
    sock.sendall(encode_frame(CTOS_CHAT, "进房间了".encode("utf-8")))

    # 一直读到连接被房间关掉为止：房间收摊时这条连接会断开，进程随即退出，
    # 这样测试结束后不会留下孤儿进程（真实 WindBot 也是在对局结束后自行断开退出）
    sock.settimeout(60)
    try:
        while sock.recv(4096):
            pass
    except (OSError, socket.timeout):
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
