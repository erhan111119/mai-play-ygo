"""网络小工具：探测本机对外地址。

插件与实验脚本都要「告诉别人该连哪个地址」，所以放在这里共用一份实现。
"""

from __future__ import annotations

import socket


def detect_lan_address() -> str:
    """探测本机在局域网/公网侧使用的地址。

    做法是让内核挑一次出口网卡：UDP 套接字 connect 到外部地址只会设置默认路由，
    不会真的发出数据包，所以这不会产生任何网络流量。

    Returns:
        本机地址；探测失败时返回空字符串，由调用方决定是提示用户手填还是回退。
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def format_endpoint(host: str, port: int) -> str:
    """把地址与端口拼成可读形式，IPv6 地址会加方括号。"""

    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"
