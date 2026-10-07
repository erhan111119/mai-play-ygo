"""房间进程管理的单元测试。

直接用 ``python tests/test_room.py`` 运行，也可以用 pytest 收集。
测试口令运行时生成，不在源码里留字面量。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import asyncio
import logging
import sys
import uuid

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.room import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    DEFAULT_PROTOCOL_VERSION,
    ProcessError,
    ProcessHandle,
    RoomSettings,
    WindBotSettings,
    describe_room_args,
    read_reported_port,
)


ROOM_PASSWORD = f"room-{uuid.uuid4().hex[:10]}"


def test_room_args_order_and_flags() -> None:
    """ygopro 的 12 个参数必须按固定顺序，且布尔值用 T/F 表示。"""

    settings = RoomSettings(
        lflist=3,
        rule=1,
        mode=0,
        duel_rule="5",
        no_check_deck=True,
        no_shuffle_deck=False,
        start_lp=8000,
        start_hand=5,
        draw_count=1,
        time_limit=180,
        save_replay=True,
    )
    args = settings.to_args(port=0)
    assert args == ["0", "3", "1", "0", "5", "T", "F", "8000", "5", "1", "180", "1"], args
    assert len(args) == 12, "内核要求参数要么不传、要么传满 12 个"


def test_room_args_default_duel_rule_uses_kernel_default() -> None:
    """大师规则留空时传 F，让内核用它自己的默认值。"""

    args = RoomSettings(duel_rule="").to_args()
    assert args[4] == "F"


def test_room_args_replay_switch() -> None:
    """录像开关映射到第 12 个参数。"""

    assert RoomSettings(save_replay=True).to_args()[11] == "1"
    assert RoomSettings(save_replay=False).to_args()[11] == "0"


def test_console_lines_decode_by_actual_encoding() -> None:
    """子进程输出的解码：ygopro 写 UTF-8、WindBot 写 GBK，两种都得读成人话。

    这是实测教训：按 UTF-8 硬解 WindBot 的中文输出，字节会被替换成 U+FFFD，
    日志从此不可读，连"计划已读"这种关键行都搜不到。
    """

    from duel.room import decode_console_line

    text = "计划已读：turn=3 aggression=0.50"
    assert decode_console_line(text.encode("utf-8")) == text
    assert decode_console_line(text.encode("gbk")) == text
    # 纯 ASCII 两边都一致
    assert decode_console_line(b"Decks initialized, 73 found.") == "Decks initialized, 73 found."


def test_windbot_args_are_key_value_pairs() -> None:
    """WindBot 的参数必须全是「键=值」，否则它会直接抛异常退出。"""

    deck_file = Path("/decks/投稿卡组.ydk")
    db_path = Path("/ygopro/cards.cdb")
    settings = WindBotSettings(
        name="麦麦",
        deck="Blue-Eyes",
        deck_file=deck_file,
        password=ROOM_PASSWORD,
        dialog="cirno.zh-CN",
        db_path=db_path,
    )
    args = settings.to_args("127.0.0.1", 7911)
    for arg in args:
        assert "=" in arg, f"参数 {arg!r} 缺少等号，WindBot 会拒绝启动"
    assert "Name=麦麦" in args
    assert "Host=127.0.0.1" in args
    assert "Port=7911" in args
    assert "Deck=Blue-Eyes" in args
    # 路径按 str(Path) 比较，Windows 上分隔符是反斜杠
    assert f"DeckFile={deck_file}" in args
    assert f"DbPath={db_path}" in args
    assert f"HostInfo={ROOM_PASSWORD}" in args
    assert f"Version={DEFAULT_PROTOCOL_VERSION}" in args
    assert "Hand=0" not in args, "默认猜拳不需要显式传"


def test_windbot_args_omit_optional_fields() -> None:
    """没配口令与卡组文件时不该出现空值的键。"""

    args = WindBotSettings(name="MaiBot").to_args("10.0.0.2", 1234)
    assert not any(arg.startswith("HostInfo=") for arg in args)
    assert not any(arg.startswith("DeckFile=") for arg in args)
    assert not any(arg.startswith("Deck=") for arg in args)
    assert not any(arg.startswith("Debug=") for arg in args)


def test_windbot_debug_switch() -> None:
    """打开调试后 WindBot 会把对局过程打到 stdout。"""

    args = WindBotSettings(debug=True).to_args("127.0.0.1", 1)
    assert "Debug=true" in args


async def test_read_reported_port_ok() -> None:
    """正常情况应当读出内核报告的端口。"""

    reader = asyncio.StreamReader()
    reader.feed_data(b"7911\n")
    reader.feed_eof()
    assert await read_reported_port(reader) == 7911


async def test_read_reported_port_zero_is_failure() -> None:
    """内核报告 0 表示监听失败，必须当成错误。"""

    reader = asyncio.StreamReader()
    reader.feed_data(b"0\n")
    reader.feed_eof()
    try:
        await read_reported_port(reader)
    except ProcessError as exc:
        assert "监听失败" in str(exc)
    else:
        raise AssertionError("端口为 0 时本应抛出 ProcessError")


async def test_kernel_output_is_forwarded_after_port() -> None:
    """读过端口之后，内核剩下的输出要继续进日志。

    这条是实测教训的护栏：内核的脚本报错只出现在 stdout（形如
    ``[string "./script/c12375297.lua"]:15: attempt to call a nil value``），
    原先只读走端口那一行、剩下的输出没人管，于是「群友能召唤但发不了效果」
    这种问题在日志里一个字都看不到，只能靠猜。
    """

    collected: List[str] = []

    class _Handler(logging.Handler):
        """收集日志文本。"""

        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record.getMessage())

    logger = logging.getLogger("test.kernel_forward")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(_Handler())

    script = (
        "import sys\n"
        "print('7911', flush=True)\n"
        "print('[string \"./script/c12375297.lua\"]:15: attempt to call a nil value', flush=True)\n"
    )
    handle = ProcessHandle("对局进程", logger)
    process = await handle.spawn(
        Path(sys.executable), ["-c", script], working_dir=_PLUGIN_ROOT, capture_stdout=True
    )
    try:
        assert process.stdout is not None
        port = await read_reported_port(process.stdout)
        assert port == 7911
        handle.forward_remaining_output(process)
        # 给转发任务一点时间把剩下的行读走
        for _ in range(50):
            if any("c12375297" in line for line in collected):
                break
            await asyncio.sleep(0.05)
        joined = "\n".join(collected)
        assert "c12375297" in joined, f"内核的脚本报错必须进日志：{collected}"
    finally:
        await handle.stop()


async def test_read_reported_port_garbage_and_eof() -> None:
    """无法解析的输出与「没输出就退出」都要明确报错。"""

    reader = asyncio.StreamReader()
    reader.feed_data(b"Bad param count.\n")
    reader.feed_eof()
    try:
        await read_reported_port(reader)
    except ProcessError as exc:
        assert "无法解析" in str(exc)
    else:
        raise AssertionError("输出不可解析时本应抛出 ProcessError")

    empty = asyncio.StreamReader()
    empty.feed_eof()
    try:
        await read_reported_port(empty)
    except ProcessError as exc:
        assert "没有报告端口" in str(exc)
    else:
        raise AssertionError("进程没输出时本应抛出 ProcessError")


async def test_read_reported_port_timeout() -> None:
    """内核迟迟不报告端口时要超时失败，而不是无限等待。"""

    reader = asyncio.StreamReader()
    try:
        await read_reported_port(reader, timeout=0.2)
    except ProcessError as exc:
        assert "超时" in str(exc)
    else:
        raise AssertionError("超时时本应抛出 ProcessError")


def test_describe_room_args() -> None:
    """可读摘要应当覆盖所有规则字段，便于启动时打进日志。"""

    described = describe_room_args(RoomSettings(), port=12345)
    assert described["port"] == 12345
    assert described["duel_rule"] == "内核默认"
    for key in ("lflist", "rule", "mode", "no_check_deck", "start_lp", "time_limit", "save_replay"):
        assert key in described, key


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
