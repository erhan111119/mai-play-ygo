"""端到端测试用的替身内核。

它模仿 ``ygopro.exe`` 服务端模式的两个关键行为：

1. **把真实监听端口打印到 stdout 的第一行**（内核就是这么报端口的，房间管理要靠它）；
2. 对每个连进来的客户端回放一段固定的对局报文流，让闸门与记录器有东西可解析。

刻意不含任何算牌逻辑——本测试要验证的是「进程 → 端口 → 闸门 → 透传 → 观测」这条接线，
真实内核的牌局规则不在这条链路的验证范围内。

输出目录一律取脚本自身所在位置，不从外部传入，避免路径被外部改写。
"""

from __future__ import annotations

from typing import Final, List

import os
import socket
import struct
import threading
import time


# STOC 消息号
STOC_GAME_MSG: Final = 0x01
STOC_TYPE_CHANGE: Final = 0x13
STOC_DUEL_START: Final = 0x15
STOC_DUEL_END: Final = 0x16
STOC_HS_PLAYER_ENTER: Final = 0x20

# 游戏内部消息号
MSG_START: Final = 4
MSG_WIN: Final = 5
MSG_NEW_TURN: Final = 40
MSG_SPSUMMONING: Final = 62
MSG_DAMAGE: Final = 91

# 脚本所在目录，就是测试建的临时目录
HERE: Final = os.path.dirname(os.path.abspath(__file__))


def encode_frame(message_id: int, payload: bytes = b"") -> bytes:
    """按协议打包一条帧。

    长度字段的值是「消息号 1 字节 + payload」的字节数，也就是 ``len(payload) + 1``。
    这个 +1 很容易漏掉，漏掉会让整条报文流错位——协议层那边有单测钉住这个语义。
    """

    return struct.pack("<HB", len(payload) + 1, message_id) + payload


def encode_wide(text: str) -> bytes:
    """编码成 20 个 UTF-16 字符的定长字段。"""

    return text.encode("utf-16-le").ljust(40, b"\x00")


def game_message(message_id: int, payload: bytes = b"") -> bytes:
    """打包一条 ``STOC_GAME_MSG``，其负载首字节是游戏内部消息号。"""

    return encode_frame(STOC_GAME_MSG, bytes([message_id]) + payload)


def build_canned_stream() -> bytes:
    """回放用的固定对局：座位 1（群友A）打脸 2400 并取胜。"""

    frames: List[bytes] = [
        encode_frame(STOC_TYPE_CHANGE, bytes([0x10])),
        encode_frame(STOC_HS_PLAYER_ENTER, encode_wide("MaiBot") + bytes([0])),
        encode_frame(STOC_HS_PLAYER_ENTER, encode_wide("群友A") + bytes([1])),
        encode_frame(STOC_DUEL_START),
        # MSG_START 的首字节低 4 位是「我在对局里的玩家号」：这里是 0，即 bot 自己是对局 0 号
        game_message(MSG_START, bytes([0])),
        game_message(MSG_NEW_TURN, bytes([0])),
        # u32 卡 ID、u8 控制者、u8 位置、i8 序号、i8 表示形式
        game_message(MSG_SPSUMMONING, struct.pack("<IBBbb", 89631139, 0, 4, 0, 1)),
        game_message(MSG_DAMAGE, struct.pack("<Bi", 0, 2400)),
        game_message(MSG_WIN, bytes([1, 2])),
        encode_frame(STOC_DUEL_END),
    ]
    return b"".join(frames)


CANNED_STREAM: Final = build_canned_stream()


def watch_stop_flag() -> None:
    """看到停止标记就退出进程；另外**最多活 10 分钟**。

    护栏（实测泄漏过）：停止标记是写在临时目录里的，而测试清理时是先写标记、等几秒、
    再把目录整个删掉——替身要是错过那几百毫秒的轮询窗口，标记就随目录一起没了，于是它
    永远活着。本机上这样累积过 8 个替身内核（最老的活了一天多）。给个上限，孤儿最多
    10 分钟就自己走；正常测试（整个套件 ~30 秒）远在这个上限之内。
    """

    flag_path = os.path.join(HERE, "stop.flag")
    deadline = time.time() + 600
    while True:
        if os.path.exists(flag_path) or time.time() > deadline:
            os._exit(0)
        time.sleep(0.2)


def serve_connection(conn: socket.socket) -> None:
    """连接建立后回放报文流，再挂一会儿让客户端有时间读走。"""

    try:
        conn.sendall(CANNED_STREAM)
        time.sleep(3)
    except OSError:
        pass
    finally:
        conn.close()


def main() -> None:
    """入口：忽略全部命令行参数（真实内核会收到 12 个房间参数），只报端口。"""

    threading.Thread(target=watch_stop_flag, daemon=True).start()

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    # 内核报端口的方式：单独一行、立刻 flush
    print(server.getsockname()[1], flush=True)

    while True:
        try:
            conn, _address = server.accept()
        except OSError:
            return
        threading.Thread(target=serve_connection, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
