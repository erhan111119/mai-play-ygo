"""对外请求的安全边界：插件里所有出网请求都从这里走。

**谁在用**：`wiki.py` 的两处——查卡接口（ygocdb）与在线卡图 CDN（`cdn.233.momobako.com`，
见 `CARD_IMAGE_CDN`）。中文卡名/卡号会发给前者，卡号会发给后者；聊天记录、用户 ID、群 ID、
密钥一概不出网。

**威胁模型**：不是"群友注入"（查询串整体转义），而是**配置页管理员 ≠ 系统管理员**——
能被改配置的人不该因此让插件去读本机或内网服务（云环境元数据接口 169.254.169.254、
本机管理端口），再把响应原样喂给模型。所以地址来自配置/常量时也要校验。

**处理办法**：只允许 http/https，且默认只允许公网地址（内网/回环/链路本地/保留/组播/未指定
一律拒绝）。插件里所有出网调用方（`wiki.py`）都走默认值——**没有配置项能放开内网**：
`wiki.endpoint` 就算填成内网/本机地址一样会被拒。签名里的 ``allow_private_host`` 是给测试
与将来可能的内网部署留的开关，**当前生产代码没有任何调用方传 ``True``**（2026-10-07 评审指出
旧文案把它写成了"自建服务时的常规做法"，那是一个并不存在的门）。

除了 IP 边界，这里还守住三件事：

* **重定向**：只跟最多 3 跳，而且每一跳都重新过一遍同样的校验（否则一次
  ``302 → http://169.254.169.254/`` 就绕过了第一道检查）；
* **响应体积**：默认最多读 512 KiB（:data:`MAX_BYTES`），调用方可按需要收紧/放宽——
  `wiki.py` 查卡用 2 MiB、取卡图用 4 MiB；
* **代理隧道一律拒绝**：走代理时目标地址由代理解析，这一步校验不到，宁可不连。

**解析与连接之间的窗口**（2026-10-07 评审指出）：`validate_url` 先解析一次域名、之后再连接时
标准库会**再解析一次**，两次结果可以不同——DNS 轮询或 rebinding 就能让"校验过的公网地址"
在真正连接的一刻变成内网地址。所以这里不用标准库默认的连接方式，而是自己实现
:func:`_pinned_connect`：**连之前解析、校验的就是马上要连的那个 IP**，一次解析一次使用，
窗口就没了（TLS 的 SNI 与证书校验仍按原主机名，所以正常 https 站不受影响）。
"""

from __future__ import annotations

from typing import Any, Optional

import http.client
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


def is_public_address(address: str) -> bool:
    """单个 IP 是否是可连的公网地址（内网/回环/链路本地/保留/组播/未指定都不算）。"""

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_public_host(host: str) -> bool:
    """域名解析出来的所有地址是否都是公网地址。

    解析失败（域名不存在、DNS 不可用）也算不安全：宁可跳过这次请求，
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
    return all(is_public_address(address) for address in addresses)


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
        # ⚠ 文案里**不要**再提"去打开某个配置开关"：那个开关（`search_allow_private_host`）是自动写脚本
        # 那条链路时代的东西，早就随功能删掉了，照着提示去配置页只会更懵（2026-10-07 评审指出）。
        raise UnsafeUrlError(
            f"{parts.hostname} 解析到内网/本机地址，已拒绝这次请求（出网只允许公网地址）"
        )
    return url


def _pinned_addresses(host: str, port: int, *, allow_private_host: bool) -> list:
    """解析主机并按需校验，返回**这次真正要连**的地址列表（``getaddrinfo`` 的原样元组）。

    校验就发生在这里、紧接着用于 connect：查到的每个地址先过 :func:`is_public_address`，
    不合格直接抛错，绝不"先放行再换地址"。
    """

    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if not infos:
        raise UnsafeUrlError(f"无法解析 {host}")
    if not allow_private_host:
        for _family, _socktype, _proto, _canon, sockaddr in infos:
            if not is_public_address(str(sockaddr[0])):
                raise UnsafeUrlError(
                    f"{host} 要连的地址 {sockaddr[0]} 是内网/本机地址，已拒绝连接"
                )
    return list(infos)


def _pinned_connect(connection: Any, *, allow_private_host: bool) -> None:
    """把"解析 → 校验 → 连接"合成一步，结果写进 ``connection.sock``。

    Args:
        connection: ``http.client.HTTPConnection``（或它的子类）实例。
        allow_private_host: 是否允许内网/本机地址。
    """

    if getattr(connection, "_tunnel_host", None):
        # 走代理隧道时目标地址由代理解析，这里校验不到——不糊过去，直接拒绝
        raise UnsafeUrlError("不支持通过代理访问（无法在连接时校验目标地址）")
    last_error: Optional[OSError] = None
    for family, socktype, proto, _canon, sockaddr in _pinned_addresses(
        connection.host, connection.port, allow_private_host=allow_private_host
    ):
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(connection.timeout)
            sock.connect(sockaddr)
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
            continue
        connection.sock = sock
        return
    raise last_error if last_error is not None else OSError("没有可用的地址")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接时校验目标 IP 的 HTTP 连接（见 :func:`_pinned_connect`）。"""

    def __init__(self, *args: Any, allow_private_host: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._allow_private_host = allow_private_host

    def connect(self) -> None:
        """解析→校验→连接（不再交给标准库自己再解析一次）。"""

        _pinned_connect(self, allow_private_host=self._allow_private_host)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """同上，额外把 TCP 连接包成 TLS（SNI 与证书校验仍按原主机名）。"""

    def __init__(self, *args: Any, allow_private_host: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._allow_private_host = allow_private_host

    def connect(self) -> None:
        """解析→校验→连 TCP→按主机名建立 TLS。"""

        _pinned_connect(self, allow_private_host=self._allow_private_host)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _connection_factory(base: type, *, allow_private_host: bool):
    """给 urllib 的连接类工厂：把 ``allow_private_host`` 带到每次连接里。"""

    def build(host: str, **kwargs: Any):
        return base(host, allow_private_host=allow_private_host, **kwargs)

    return build


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """用 :class:`_PinnedHTTPConnection` 发 http 请求。"""

    def __init__(self, *, allow_private_host: bool) -> None:
        super().__init__()
        self._allow_private_host = allow_private_host

    def http_open(self, req):  # noqa: ANN001, ANN201
        """打开连接（连接时才解析并校验目标 IP）。"""

        return self.do_open(
            _connection_factory(_PinnedHTTPConnection, allow_private_host=self._allow_private_host),
            req,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """用 :class:`_PinnedHTTPSConnection` 发 https 请求。"""

    def __init__(self, *, allow_private_host: bool) -> None:
        super().__init__()
        self._allow_private_host = allow_private_host

    def https_open(self, req):  # noqa: ANN001, ANN201
        """打开连接（连接时才解析并校验目标 IP）。"""

        return self.do_open(
            _connection_factory(_PinnedHTTPSConnection, allow_private_host=self._allow_private_host),
            req,
            context=self._context,
        )


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
    连接走 :class:`_PinnedHTTPConnection` / :class:`_PinnedHTTPSConnection`：
    解析与校验发生在真正连接的那一步，避免"校验时是公网、连接时换成内网"的窗口。
    """

    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method or ("POST" if data else "GET")
    )
    validate_url(request.full_url, allow_private_host=allow_private_host)
    opener = urllib.request.build_opener(
        _PinnedHTTPHandler(allow_private_host=allow_private_host),
        _PinnedHTTPSHandler(allow_private_host=allow_private_host),
        _GuardedRedirectHandler(allow_private_host=allow_private_host),
    )
    with opener.open(request, timeout=timeout) as response:  # noqa: S310  地址已校验
        return response.read(max_bytes)
