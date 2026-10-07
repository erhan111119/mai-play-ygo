"""对外请求护栏的单元测试（``duel/netguard.py``）。

这里全部用 IP 字面量与 ``localhost``，不依赖外网，也不真的发请求——
真发请求的路径没法在回归里跑（会被网络环境影响），所以只钉住判定逻辑本身。

直接用 ``python tests/test_netguard.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.netguard import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    MAX_REDIRECTS,
    UnsafeUrlError,
    _pinned_connect,
    is_public_address,
    is_public_host,
    validate_url,
)


def expect_unsafe(url: str, *, allow_private_host: bool = False) -> str:
    """期望校验失败，返回错误信息。"""

    try:
        validate_url(url, allow_private_host=allow_private_host)
    except UnsafeUrlError as exc:
        return str(exc)
    raise AssertionError(f"这个地址本该被拒绝：{url}")


def test_scheme_restricted_to_http() -> None:
    """只允许 http/https：file:// 能读本地文件，绝不能放它过去。"""

    assert "只允许 http/https" in expect_unsafe("file:///C:/Windows/win.ini")
    assert "只允许 http/https" in expect_unsafe("ftp://example.com/x")
    assert "只允许 http/https" in expect_unsafe("//example.com/x")


def test_missing_host_rejected() -> None:
    """没有主机名的地址不该被当成合法地址。"""

    assert "没有主机名" in expect_unsafe("http:///search")
    assert "没有主机名" in expect_unsafe("https://")


def test_private_and_loopback_blocked_by_default() -> None:
    """默认不允许内网/回环/链路本地地址（含云元数据那种 169.254 地址）。"""

    for url in (
        "http://127.0.0.1:8888",
        "http://localhost:8888",
        "http://10.0.0.5:8888",
        "http://192.168.1.10:8888",
        "http://172.16.3.4:8888",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8888",
    ):
        message = expect_unsafe(url)
        assert "内网/本机" in message, (url, message)

    assert not is_public_host("127.0.0.1")
    assert not is_public_host("localhost")
    assert not is_public_host("不存在的域名.invalid")


def test_private_host_allowed_when_operator_opts_in() -> None:
    """自建搜索服务时，运维显式打开开关后要能通过（否则这个功能就用不了）。"""

    assert validate_url("http://127.0.0.1:8888", allow_private_host=True) == "http://127.0.0.1:8888"
    assert validate_url("http://localhost:8888/", allow_private_host=True)


def test_public_addresses_pass() -> None:
    """公网地址正常通过；即使不做 DNS 查询也要能判定 IP 字面量。"""

    assert is_public_host("8.8.8.8")
    assert is_public_host("1.1.1.1")
    assert validate_url("https://example.com/search") == "https://example.com/search"


def test_redirect_limit_is_small() -> None:
    """重定向跳数要压得很小：每多一跳都是一次新的、可能被引导到内网的请求。"""

    assert 0 < MAX_REDIRECTS <= 5, MAX_REDIRECTS


def test_connect_time_check_blocks_private_target() -> None:
    """连接那一刻再校验一次目标 IP：解析与连接之间的换址窗口要堵上（2026-10-07 评审指出）。

    这里不真的连网：造一个只有几个属性的假连接对象喂给 `_pinned_connect`，
    它应当在校验阶段就抛错，压根不走到 socket.connect。
    """

    class _FakeConnection:
        """`_pinned_connect` 需要的最小接口。"""

        def __init__(self, host: str, port: int = 80) -> None:
            self.host = host
            self.port = port
            self.timeout = 3
            self.sock = None
            self._tunnel_host = None

    for host in ("localhost", "127.0.0.1", "169.254.169.254", "192.168.1.1", "[::1]"):
        try:
            _pinned_connect(_FakeConnection(host), allow_private_host=False)
        except UnsafeUrlError:
            continue
        raise AssertionError(f"{host} 应当在连接前被拦下")

    # 走代理隧道时目标地址校验不到，必须明确拒绝而不是照常连
    tunnel = _FakeConnection("127.0.0.1")
    tunnel._tunnel_host = "proxy.internal"
    try:
        _pinned_connect(tunnel, allow_private_host=False)
    except UnsafeUrlError:
        pass
    else:
        raise AssertionError("隧道连接应当被拒绝")


def test_public_address_predicate() -> None:
    """`is_public_address` 的边界：内网/回环/链路本地/保留/组播都不算公网。"""

    assert is_public_address("8.8.8.8") and is_public_address("206.237.31.210")
    for bad in (
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
        "not-an-ip",
    ):
        assert not is_public_address(bad), bad


def main() -> int:
    """逐个执行测试函数。"""

    tests = [(name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures: List[str] = []
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
