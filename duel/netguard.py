"""对外请求的安全边界：给「联网搜索」这类功能套一层护栏。

**为什么需要它**：插件里唯一会主动对外发起请求的地方是"搜打法要点"（SearXNG 兼容接口），
地址来自插件配置。威胁模型不是"群友注入"（地址由运维填、查询串会整体转义），而是
**配置页管理员 ≠ 系统管理员**：能被改配置的人不该因此让插件去读本机或内网服务
（例如云环境的元数据接口 169.254.169.254、本机的管理端口），再把响应原样喂给模型。

**处理办法**：默认只允许公网地址；确实想把搜索服务自建在内网/本机时，运维显式打开
``duel.search_allow_private_host``。这样既堵住默认路径，又不把常用场景一刀切死——
"封内网"对自建 SearXNG 是致命的，所以做成开关而不是硬禁。

除了 IP 边界，这里还顺手守住两件常被忽略的事：

* **重定向**：只跟最多 3 跳，而且每一跳都重新过一遍同样的校验（否则一次
  ``302 → http://169.254.169.254/`` 就绕过了第一道检查）；
* **响应体积**：最多读 512 KiB，免得对面用超大响应把内存吃满。
"""

from __future__ import annotations

from typing import Optional

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

# 请求超时（秒）
TIMEOUT = 15
# 最多读多少字节的响应体
MAX_BYTES = 512 * 1024
# 最多跟几跳重定向
MAX_REDIRECTS = 3


class UnsafeUrlError(ValueError):
    """地址不满足安全约束时抛出（协议、主机或解析后的 IP 不合规）。"""


def is_public_host(host: str) -> bool:
    """域名解析出来的所有地址是否都是公网地址。

    解析失败（域名不存在、DNS 不可用）也算不安全：宁可跳过这次搜索，
    也不要让请求落在一个我们判断不了的目标上。
    """

    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def validate_url(url: str, *, allow_private_host: bool = False) -> str:
    """校验一个 http/https 地址，返回原地址；不合规时抛出 :class:`UnsafeUrlError`。

    Args:
        url: 待校验的地址。
        allow_private_host: 是否允许内网/本机地址（自建搜索服务的场景）。
    """

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"只允许 http/https，收到 {parts.scheme or '(空)'}")
    if not parts.hostname:
        raise UnsafeUrlError("地址里没有主机名")
    if not allow_private_host and not is_public_host(parts.hostname):
        raise UnsafeUrlError(
            f"{parts.hostname} 解析到内网/本机地址；确实要连自建服务，"
            "请在配置里打开 search_allow_private_host"
        )
    return url


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """限制跳数、并且每一跳都重新校验的安全重定向处理器。"""

    max_redirections = MAX_REDIRECTS

    def __init__(self, *, allow_private_host: bool) -> None:
        self._allow_private_host = allow_private_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        """跟随重定向前先校验新地址。"""

        validate_url(newurl, allow_private_host=self._allow_private_host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_urlopen(
    url: str,
    *,
    allow_private_host: bool = False,
    timeout: int = TIMEOUT,
    max_bytes: int = MAX_BYTES,
):
    """按安全约束发起 GET，返回响应正文（bytes）。

    Raises:
        UnsafeUrlError: 地址不合规（协议/主机/IP 边界）。
    """

    return guarded_request(
        url, allow_private_host=allow_private_host, timeout=timeout, max_bytes=max_bytes
    )


def guarded_request(
    url: str,
    *,
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    method: Optional[str] = None,
    allow_private_host: bool = False,
    timeout: int = TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    """按同一套约束发起请求（GET/POST 都行），返回响应正文。

    与 :func:`guarded_urlopen` 共用校验、重定向限制与响应体积上限；
    训练侧的模型调用需要 POST，所以单独开这个入口，而不是在那边另写一份 urllib。
    """

    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method or ("POST" if data else "GET")
    )
    validate_url(request.full_url, allow_private_host=allow_private_host)
    opener = urllib.request.build_opener(
        _GuardedRedirectHandler(allow_private_host=allow_private_host)
    )
    with opener.open(request, timeout=timeout) as response:  # noqa: S310  地址已校验
        return response.read(max_bytes)
