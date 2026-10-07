"""对局会话的状态机测试。

房间与闸门在这里用替身替换：真正起 ygopro.exe/WindBot.exe 需要部署前提，
不是单元测试该覆盖的范围。这里要验证的是**超时与收摊逻辑**——它一旦写错，
房间就会永远占着端口不退，比功能缺失更难查。

直接用 ``python tests/test_session.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import asyncio
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.session import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    OUTCOME_FINISHED,
    OUTCOME_NO_PLAYER,
    OUTCOME_TIMEOUT,
    DuelSession,
    SessionConfig,
    SessionInfo,
    generate_password,
)


class FakeGate:
    """闸门替身：只提供 status 与 stop。"""

    def __init__(self, *, humans: Optional[List[object]] = None) -> None:
        self.status = SimpleNamespace(human_clients=list(humans or []), clients=[])
        self.stopped = False

    async def stop(self) -> None:
        """记录自己被停过。"""

        self.stopped = True


class FakeRoom:
    """房间替身。"""

    def __init__(self) -> None:
        self.port = 12345
        self.stopped = False
        self.stop_raises = False

    @property
    def running(self) -> bool:
        """固定返回运行中。"""

        return True

    async def stop(self) -> None:
        """可选地抛错，用来验证收摊时的容错。"""

        self.stopped = True
        if self.stop_raises:
            raise RuntimeError("模拟关闭失败")


def make_session(*, join_timeout: float = 5.0, max_duration: float = 30.0) -> DuelSession:
    """造一个不启动真进程的会话，房间与闸门都用替身。"""

    config = SessionConfig(
        ygopro_executable=Path("ygopro.exe"),
        ygopro_dir=Path("."),
        windbot_executable=Path("WindBot.exe"),
        windbot_dir=Path("."),
        join_timeout=join_timeout,
        max_duration=max_duration,
    )
    session = DuelSession(config, group_id="111", human_name_hint="群友A")
    session._room = FakeRoom()  # type: ignore[assignment]  测试里用替身替换真实进程
    session._gate = FakeGate()  # type: ignore[assignment]
    return session


def test_generate_password_alphabet_and_length() -> None:
    """口令要够短好输入，且只用不看错的字符。"""

    password = generate_password()
    assert len(password) == 6
    assert all(char in "abcdefghjkmnpqrstuvwxyz23456789" for char in password), password
    assert not set(password) & set("0O1lI"), "口令不该包含容易与数字混淆的字符"


def test_generate_password_is_not_constant() -> None:
    """两次生成不该相同（房间口令是准入凭据，不能是可预测的固定值）。"""

    passwords = {generate_password(8) for _ in range(50)}
    assert len(passwords) > 40, "口令重复率过高，随机源可能有问题"


def test_connection_hint_per_platform() -> None:
    """两种客户端的加入指引要说清各自该在哪里填什么。"""

    info = SessionInfo(host="192.168.1.5", port=23456, password="abc123", deck=None, backend_port=7911)
    mdpro = info.connection_hint("mdpro3")
    mobile = info.connection_hint("ygomobile")
    for text in (mdpro, mobile):
        assert "192.168.1.5:23456" in text
        assert "abc123" in text
    assert "编辑服务器" in mobile
    assert "联机" in mdpro
    # mdpro3 的文案里不该出现另一种客户端的步骤，反之亦然
    assert "编辑服务器" not in mdpro
    assert "联机" not in mobile


def test_connection_hint_without_platform_covers_both() -> None:
    """不知道对方用哪个客户端时，一份指引里两种客户端的步骤都要有。

    这条是实测教训的护栏：原先 tool 把 platform 设成必填并要求模型「先问清客户端」，
    结果在禁止提问的群里模型卡住——它既不敢问、又不敢替对方猜，最后只在群里说了句
    「我这就上号建房间」却没真的建房。不传 platform 时必须能直接开局。
    """

    info = SessionInfo(host="10.0.0.9", port=7911, password="pw", deck=None, backend_port=7912)
    hint = info.connection_hint()
    assert "10.0.0.9:7911" in hint and "pw" in hint
    assert "联机" in hint, hint
    assert "编辑服务器" in hint, hint


def test_connection_hint_uses_advertised_port() -> None:
    """内网穿透映射到别的公网端口时，发出去的必须是公网端口。

    这是穿透场景最容易出的错：本地闸门端口和隧道公网端口通常不同，
    要是把本地端口发出去，群友会连到一个根本不对外开放的端口。
    """

    # 未配置公网端口时与本地端口一致
    local_only = SessionInfo(
        host="10.0.0.2", port=64400, password="pw", deck=None, backend_port=64399
    )
    assert local_only.advertised_port == 64400
    assert "10.0.0.2:64400" in local_only.connection_hint("mdpro3")

    # 配置了公网端口时用公网端口，且本地端口与后端端口在指引里都不出现
    tunnelled = SessionInfo(
        host="room.example.com",
        port=64400,
        password="pw",
        deck=None,
        backend_port=64399,
        public_port=30123,
    )
    assert tunnelled.advertised_port == 30123
    hint = tunnelled.connection_hint("ygomobile")
    assert "room.example.com:30123" in hint, hint
    assert "64400" not in hint, f"不该把本地端口发给群友：{hint}"
    assert "64399" not in hint


async def test_outcome_no_player_when_nobody_joins() -> None:
    """没人进来时要按时收摊，不能一直占着房间。"""

    session = make_session(join_timeout=0.2, max_duration=10.0)
    assert await session.wait_finished() == OUTCOME_NO_PLAYER


async def test_outcome_timeout_after_human_joined() -> None:
    """玩家进来了但迟迟打不完，到硬上限也要收摊。"""

    session = make_session(join_timeout=0.2, max_duration=0.4)
    session._gate = FakeGate(humans=[object()])  # type: ignore[assignment]
    assert await session.wait_finished() == OUTCOME_TIMEOUT


async def test_outcome_finished_when_duel_ends() -> None:
    """收到对局结束信号就立刻返回，不用等到超时。"""

    session = make_session(join_timeout=5.0, max_duration=30.0)
    session._gate = FakeGate(humans=[object()])  # type: ignore[assignment]

    async def end_soon() -> None:
        """稍后模拟对局结束。"""

        await asyncio.sleep(0.1)
        session._on_gate_status("duel_ended", {})

    task = asyncio.create_task(end_soon())
    outcome = await asyncio.wait_for(session.wait_finished(), timeout=3.0)
    await task
    assert outcome == OUTCOME_FINISHED


async def test_human_seen_prevents_no_player_outcome() -> None:
    """玩家已经进来了就不该再报「没人来」，而应继续等到硬上限。"""

    session = make_session(join_timeout=0.2, max_duration=0.6)
    session._gate = FakeGate(humans=[object()])  # type: ignore[assignment]

    async def end_soon() -> None:
        """稍后模拟对局结束。"""

        await asyncio.sleep(0.3)
        session._on_gate_status("duel_ended", {})

    task = asyncio.create_task(end_soon())
    outcome = await asyncio.wait_for(session.wait_finished(), timeout=3.0)
    await task
    assert outcome == OUTCOME_FINISHED


async def test_bot_disconnect_ends_duel_early() -> None:
    """bot 掉线且对局已开始时要提前收摊，而不是傻等到超时。"""

    session = make_session(join_timeout=5.0, max_duration=30.0)
    session._gate = FakeGate(humans=[object()])  # type: ignore[assignment]
    session.recorder().started = True

    async def drop_bot() -> None:
        """模拟 bot 连接断开。"""

        await asyncio.sleep(0.1)
        session._on_gate_status("client_left", {"role": "bot"})

    task = asyncio.create_task(drop_bot())
    outcome = await asyncio.wait_for(session.wait_finished(), timeout=3.0)
    await task
    assert outcome == OUTCOME_FINISHED


async def test_stop_closes_everything_even_if_one_fails() -> None:
    """收摊要尽力而为：某个组件关闭失败不能导致其余组件漏关。"""

    session = make_session()
    room = FakeRoom()
    room.stop_raises = True
    session._room = room  # type: ignore[assignment]
    gate = FakeGate()
    session._gate = gate  # type: ignore[assignment]

    await session.stop()
    assert room.stopped, "房间应当被尝试关闭"
    assert gate.stopped, "房间关闭失败也必须继续关闸门"


async def test_status_callback_is_forwarded() -> None:
    """闸门事件要透传给插件，否则插件无法向群里播报进度。"""

    events: List[str] = []
    config = SessionConfig(
        ygopro_executable=Path("ygopro.exe"),
        ygopro_dir=Path("."),
        windbot_executable=Path("WindBot.exe"),
        windbot_dir=Path("."),
        on_status=lambda event, payload: events.append(event),
    )
    session = DuelSession(config, group_id="111")
    session._on_gate_status("client_joined", {"role": "human"})
    session._on_gate_status("duel_started", {})
    assert events == ["client_joined", "duel_started"]


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
