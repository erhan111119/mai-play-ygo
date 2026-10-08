"""对局进程管理：ygopro.exe（房间内核）与 WindBot.exe（bot 的出牌大脑）。

ygopro 服务端模式的参数格式来自 ``gframe/gframe.cpp`` 的 ``servermain``：它要求
**要么不传参数、要么把 12 个参数传满**（``argc > 1 && argc < 13`` 直接报错退出），
顺序为::

    <端口> <禁卡表编号> <卡片允许范围> <决斗模式> <大师规则> <不检查卡组> <不洗切卡组>
    <初始LP> <初始手牌> <每回合抽卡> <回合时限> <录像选项> [base64 种子...]

端口传 0 表示由内核选一个空闲端口，真实端口会**打印到 stdout 的第一行**（失败时打印 ``0``），
所以起房间时要读子进程输出，而不是自己猜端口。

WindBot 的参数是一串 ``键=值``（``Config.cs`` 的 ``LoadArgs`` 遇到不含 ``=`` 的参数会直接抛异常），
可用键为 Name / Deck / DeckFile / Dialog / Host / Port / HostInfo / Version / Hand / Debug /
Chat / DbPath / ServerMode / ServerPort。其中 bot 加入房间时用的口令就是 ``HostInfo``，
它会被写进 ``CTOS_JOIN_GAME`` 的 pass 字段，正好是闸门校验的那个字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import asyncio
import ctypes
import logging
import subprocess
import sys


# WindBot 的协议版本默认值，与服务端 gframe/config.h 的 PRO_VERSION = 0x1362 一致
DEFAULT_PROTOCOL_VERSION = 0x1362

# 等待 ygopro 报出端口的最长时间
_PORT_READ_TIMEOUT = 30.0

# 关闭进程时先 terminate 等待的秒数，超时再 kill
_TERMINATE_GRACE_SECONDS = 5.0


class ProcessError(RuntimeError):
    """进程启动或退出异常时抛出。"""


@dataclass
class RoomSettings:
    """房间规则参数（对应 ygopro 的 12 个命令行参数）。

    Args:
        lflist: 禁卡表编号，0 表示使用默认。
        rule: 卡片允许范围。
        mode: 决斗模式，0 单打 / 1 双打 / 2 组队，超过 2 会被内核当作 0。
        duel_rule: 大师规则，留空表示用内核默认值。
        no_check_deck: 不检查卡组。默认开启，这样群友投稿的卡组不会被禁卡表挡在门外。
        no_shuffle_deck: 不洗切卡组。
        save_replay: 是否在对局结束后把录像写进 ``<ygopro目录>/replay/``。
    """

    lflist: int = 0
    rule: int = 0
    mode: int = 0
    duel_rule: str = ""
    no_check_deck: bool = True
    no_shuffle_deck: bool = False
    start_lp: int = 8000
    start_hand: int = 5
    draw_count: int = 1
    time_limit: int = 180
    save_replay: bool = True

    def to_args(self, port: int = 0) -> List[str]:
        """生成 ygopro 的命令行参数（不含可执行文件本身）。"""

        return [
            str(port),
            str(self.lflist),
            str(self.rule),
            str(self.mode),
            self.duel_rule if self.duel_rule else "F",
            "T" if self.no_check_deck else "F",
            "T" if self.no_shuffle_deck else "F",
            str(self.start_lp),
            str(self.start_hand),
            str(self.draw_count),
            str(self.time_limit),
            "1" if self.save_replay else "0",
        ]


@dataclass
class WindBotSettings:
    """WindBot 的启动参数。

    Args:
        name: 对局里显示的昵称。
        deck: WindBot 已注册卡组名，决定它用哪套出牌策略（必须填它认识的卡组）。
        deck_file: 实际使用的 .ydk 路径，会覆盖 ``deck`` 对应的卡表，但不会改变出牌策略。
        password: 加入房间的口令，会被写进 HostInfo。
        dialog: 台词包名。
        debug: 打开后 WindBot 会把对局过程打到 stdout，排查问题时很有用。
        chat: 是否让 WindBot 发它自带的固化台词。``False`` 表示闭嘴，改由插件用模型生成台词；
            ``None`` 表示不传这个参数、用 WindBot 自己的默认值。
        brain_file: **阻抗决策层**的问答前缀（不含扩展名）。给了它 WindBot 才会在阻抗时点
            写下问题并等答复（见 `duel/brain_bridge.py` 与 WindBot 的 `Game/AI/MaiBotBrain.cs`）；
            ``None`` 表示不起决策层，出牌完全由脚本决定。
        brain_target_choice: 决策层是否回答"无效哪只怪"（默认开；风险最低的一半）。
        brain_negate_gate: 决策层是否回答"要不要交这张阻抗"（默认关；要先跑镜像 A/B）。
        brain_timeout_ms: WindBot 等答复的上限（毫秒）。**必须大于 Python 侧的等模型上限**，
            否则"超时"只会由 WindBot 发现，Python 那边还在傻等。
    """

    name: str = "MaiBot"
    deck: str = ""
    deck_file: Optional[Path] = None
    password: str = ""
    dialog: str = ""
    db_path: Optional[Path] = None
    version: int = DEFAULT_PROTOCOL_VERSION
    debug: bool = False
    hand: int = 0
    chat: Optional[bool] = None
    # ⚠ 这里原来还有两个路径参数：`plan_file`（作战计划）／`playbook_file`（卡组打法数据）
    # ／以及老口径的 `brain_file`（逐步问 AI）——它们只有计划感知执行器 ``PlanAware`` 会读。
    # AI 教练 / 打法数据 / 逐步问 AI 已按 2026-10-07 用户口径删除（连带 `PlanAware`）。
    # 2026-10-08 的**阻抗决策层**重新用了 ``BrainFile=`` 这个名字，但范围完全不同：
    # 只在"对手回合 + 这张是阻抗卡"时问，展开期一步都不问（见 `Game/AI/NegateDecision.cs`）。
    brain_file: Optional[Path] = None
    brain_target_choice: bool = True
    brain_negate_gate: bool = False
    brain_timeout_ms: int = 2500

    def to_args(self, host: str, port: int) -> List[str]:
        """生成 WindBot 的命令行参数（不含可执行文件本身）。

        每个参数都必须是 ``键=值`` 形式，否则 WindBot 会直接抛异常退出。
        """

        args = [f"Name={self.name}", f"Host={host}", f"Port={port}", f"Version={self.version}"]
        if self.deck:
            args.append(f"Deck={self.deck}")
        if self.deck_file is not None:
            args.append(f"DeckFile={self.deck_file}")
        if self.password:
            args.append(f"HostInfo={self.password}")
        if self.dialog:
            args.append(f"Dialog={self.dialog}")
        if self.db_path is not None:
            args.append(f"DbPath={self.db_path}")
        if self.hand:
            args.append(f"Hand={self.hand}")
        if self.debug:
            args.append("Debug=true")
        if self.chat is not None:
            args.append(f"Chat={'true' if self.chat else 'false'}")
        if self.brain_file is not None:
            args.append(f"BrainFile={self.brain_file}")
            args.append(f"BrainTargetChoice={'true' if self.brain_target_choice else 'false'}")
            args.append(f"BrainNegateGate={'true' if self.brain_negate_gate else 'false'}")
            args.append(f"BrainTimeoutMs={int(self.brain_timeout_ms)}")
        return args


def _creation_flags() -> int:
    """Windows 下不弹出控制台窗口；其它平台无此概念。"""

    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


async def read_reported_port(stream: asyncio.StreamReader, timeout: float = _PORT_READ_TIMEOUT) -> int:
    """从进程输出里读出 ygopro 报告的监听端口。

    内核启动成功后会把真实端口单独打印一行；失败时打印 ``0``。两种情况都在这里判别，
    不完整的输出会被当作错误抛出，而不是让调用方拿到一个猜出来的端口。

    Raises:
        ProcessError: 超时、没有输出、输出不可解析或端口为 0 时抛出。
    """

    try:
        line = await asyncio.wait_for(stream.readline(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ProcessError(f"等待对局进程报告端口超时（{timeout:.0f} 秒）") from exc

    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        raise ProcessError("对局进程没有报告端口就退出了，请检查 ygopro 目录是否完整")
    try:
        port = int(text)
    except ValueError as exc:
        raise ProcessError(f"对局进程输出了无法解析的端口行：{text!r}") from exc
    if port == 0:
        raise ProcessError("对局进程报告端口为 0，说明监听失败（端口被占用或权限不足）")
    return port


def _console_codepage() -> str:
    """Windows 控制台当前的 OEM 代码页，例如 ``cp936``（简体中文）。"""

    if sys.platform != "win32":
        return "utf-8"
    codepage = ctypes.windll.kernel32.GetConsoleOutputCP()
    return f"cp{codepage}" if codepage else "utf-8"


def decode_console_line(raw: bytes) -> str:
    """按实际编码解一行子进程输出。

    两个子进程写的编码不一样：ygopro 写 UTF-8，WindBot 是 .NET 控制台程序、
    在中文 Windows 上按 GBK 写 stdout。所以要先按 UTF-8 严格解，解不动再按系统
    代码页 —— 顺序不能反，反了会把中文日志解成乱码，而乱码是不可逆的
    （字节一旦被替换成 U+FFFD，日志就再也读不出来了）。
    """

    for encoding in ("utf-8", _console_codepage()):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 两种都不行：按 latin-1 收下（它不会失败），至少让这行可读、不丢字节
    return raw.decode("latin-1")


async def _forward_stream(
    stream: Optional[asyncio.StreamReader], tag: str, logger: logging.Logger
) -> None:
    """把一个进程输出流逐行转发进日志。

    出错行用 warning 级别，其余用 info：这样日志里既能看到它在做什么，
    也能一眼看到 "Can't find cards database file" 之类的问题。
    """

    if stream is None:
        return
    while True:
        try:
            raw = await stream.readline()
        except (OSError, ValueError):
            return
        if not raw:
            return
        line = decode_console_line(raw).strip()
        if not line:
            continue
        lowered = line.lower()
        if any(word in lowered for word in ("error", "exception", "can't", "cannot", "fail")):
            logger.warning("%s%s", tag, line)
        else:
            logger.info("%s%s", tag, line)


def forward_process_output(
    process: asyncio.subprocess.Process, label: str, logger: logging.Logger
) -> List[asyncio.Task]:
    """转发子进程的 stdout/stderr，返回读取任务（调用方可取消）。

    需要在启动时给两个流都接了管道才有效（``stdout=PIPE, stderr=PIPE``）。
    """

    return [
        asyncio.create_task(_forward_stream(process.stdout, f"[{label}] ", logger)),
        asyncio.create_task(_forward_stream(process.stderr, f"[{label}:err] ", logger)),
    ]


class ProcessHandle:
    """对子进程的薄封装，负责启动与稳妥关闭。"""

    def __init__(self, label: str, logger: Optional[logging.Logger] = None) -> None:
        self._label = label
        self._logger = logger or logging.getLogger(__name__)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._output_tasks: set = set()

    @property
    def running(self) -> bool:
        """进程是否仍在运行。"""

        return self._process is not None and self._process.returncode is None

    @property
    def process(self) -> Optional[asyncio.subprocess.Process]:
        """底层进程对象，未启动时为 None。"""

        return self._process

    async def spawn(
        self,
        executable: Path,
        args: List[str],
        *,
        working_dir: Path,
        capture_stdout: bool = False,
        forward_output: bool = False,
    ) -> asyncio.subprocess.Process:
        """启动进程。

        Args:
            executable: 可执行文件路径。
            args: 命令行参数。
            working_dir: 工作目录，ygopro 要靠它找 cards.cdb 与 script/。
            capture_stdout: 是否需要读取子进程输出（ygopro 要用它报端口）。
            forward_output: 是否把子进程输出转发进日志。WindBot 出错或卡住时只能从这里看出来，
                丢掉它的输出会让「bot 不动」这类问题完全无从排查。
        """

        if not executable.is_file():
            raise ProcessError(f"找不到可执行文件：{executable}")
        if not working_dir.is_dir():
            raise ProcessError(f"工作目录不存在：{working_dir}")
        if self.running:
            raise ProcessError(f"{self._label} 已经在运行")

        pipe = capture_stdout or forward_output
        self._process = await asyncio.create_subprocess_exec(
            str(executable),
            *args,
            cwd=str(working_dir),
            stdout=asyncio.subprocess.PIPE if pipe else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE if pipe else asyncio.subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        self._logger.info("%s 已启动（pid=%s）", self._label, self._process.pid)
        if forward_output and not capture_stdout:
            # 交给调用方读端口时，输出由调用方自己消费，不重复开读取任务
            self._start_output_forwarder(self._process)
        return self._process

    def forward_remaining_output(self, process: asyncio.subprocess.Process) -> None:
        """接管已经被读过一部分的输出流，把剩下的转发进日志。

        给「先读一行端口、再继续读日志」的场景用：不接管的话，读端停在那里，
        内核写满管道缓冲后会自己阻塞，表现为对局卡死。
        """

        self._start_output_forwarder(process)

    def _start_output_forwarder(self, process: asyncio.subprocess.Process) -> None:
        """把子进程的 stdout/stderr 逐行转发到日志。"""

        self._output_tasks.update(forward_process_output(process, self._label, self._logger))

    async def stop(self) -> None:
        """先 terminate，超时再 kill，确保不留下孤儿进程。"""

        for task in list(self._output_tasks):
            task.cancel()
        self._output_tasks.clear()

        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            return
        self._logger.info("正在关闭 %s（pid=%s）", self._label, process.pid)
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            self._process = None
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            self._logger.warning("%s 未响应终止信号，强制结束", self._label)
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
            await process.wait()
        self._process = None

    def kill_now(self) -> None:
        """立刻结束进程：同步、不等退出、不写日志。

        给插件卸载路径用：``stop()`` 会等进程自己退出（最多 ``_TERMINATE_GRACE_SECONDS`` 秒），
        而宿主给插件卸载的总预算只有 5 秒——对局在跑时 WindBot 与内核各等一次就超时，
        宿主会判"卸载失败"并重启插件，对局里的连接随之断掉（实测就是这个现象）。
        卸载场景只关心"进程必须死"，不需要日志收尾，所以这里直接 kill。
        """

        for task in list(self._output_tasks):
            task.cancel()
        self._output_tasks.clear()

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


class YgoProRoom:
    """一个 ygopro 房间进程。

    ygopro 服务端模式本身就是「一进程一房间」，所以「创建房间」在这里就等价于
    「起一个进程并拿到它报告的端口」。房间名与口令由闸门负责，内核不参与。
    """

    def __init__(
        self,
        executable: Path,
        working_dir: Path,
        settings: RoomSettings,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._executable = executable
        self._working_dir = working_dir
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._handle = ProcessHandle("对局进程", self._logger)
        self._port = 0

    @property
    def port(self) -> int:
        """内核实际监听的端口，未启动时为 0。"""

        return self._port

    @property
    def running(self) -> bool:
        """房间进程是否仍在运行。"""

        return self._handle.running

    @property
    def working_dir(self) -> Path:
        """工作目录，录像与卡牌数据库都在这里找。"""

        return self._working_dir

    async def start(self) -> int:
        """启动房间并返回端口。"""

        args = self._settings.to_args(port=0)
        process = await self._handle.spawn(
            self._executable, args, working_dir=self._working_dir, capture_stdout=True
        )
        assert process.stdout is not None  # 已要求捕获 stdout
        try:
            self._port = await read_reported_port(process.stdout)
        except ProcessError:
            await self._handle.stop()
            raise

        # 端口读到之后，继续把内核剩下的输出转发进日志。内核的 Lua 报错只出现在这里
        # （形如 [string "./script/c<id>.lua"]:36: attempt to …），而它恰恰是
        # 「能召唤但不能发效果」这类问题的唯一线索——丢掉这些输出就只能靠猜。
        # 顺带把管道读空，避免内核输出写满管道后被阻塞。
        self._handle.forward_remaining_output(process)

        self._logger.info("房间已就绪，内核端口 %s", self._port)
        # 若进程随即退出，这里能第一时间发现，避免把死房间当成好房间交给群友
        await asyncio.sleep(0)
        if process.returncode is not None:
            self._port = 0
            raise ProcessError(f"对局进程启动后立即退出（退出码 {process.returncode}）")
        return self._port

    async def stop(self) -> None:
        """关闭房间进程。"""

        await self._handle.stop()
        self._port = 0

    def kill_now(self) -> None:
        """立刻结束内核进程（插件卸载用，见 :meth:`ProcessHandle.kill_now`）。"""

        self._handle.kill_now()
        self._port = 0

    async def is_exposed_beyond_loopback(self, host: str) -> bool:
        """检查内核是否也监听在非回环地址上。

        内核的监听地址取决于编译开关：定义了 ``SERVER_PRO2_SUPPORT`` 时只监听回环，
        否则监听 ``INADDR_ANY``。后者意味着知道内核端口的人可以绕过闸门的口令校验直连。

        Args:
            host: 本机在局域网/公网上的地址，用它来试探是否能连上内核端口。
        """

        if not self._port:
            return False
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, self._port), timeout=3.0
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        return True


class WindBotProcess:
    """bot 侧的出牌进程。"""

    def __init__(
        self,
        executable: Path,
        working_dir: Path,
        settings: WindBotSettings,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._executable = executable
        self._working_dir = working_dir
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._handle = ProcessHandle("WindBot", self._logger)

    @property
    def running(self) -> bool:
        """进程是否仍在运行。"""

        return self._handle.running

    async def start(self, host: str, port: int) -> None:
        """让 bot 连进指定房间。"""

        args = self._settings.to_args(host, port)
        # 转发它的输出：WindBot 报错或卡住时只能从它的日志看出来
        await self._handle.spawn(
            self._executable, args, working_dir=self._working_dir, forward_output=True
        )

    async def stop(self) -> None:
        """关闭 bot 进程。对局中它没有优雅投降的入口，只能终止。"""

        await self._handle.stop()

    def kill_now(self) -> None:
        """立刻结束 bot 进程（插件卸载用，见 :meth:`ProcessHandle.kill_now`）。"""

        self._handle.kill_now()


def describe_room_args(settings: RoomSettings, port: int = 0) -> Dict[str, object]:
    """把房间参数整理成可读字典，用于日志与启动提示。"""

    return {
        "port": port,
        "lflist": settings.lflist,
        "rule": settings.rule,
        "mode": settings.mode,
        "duel_rule": settings.duel_rule or "内核默认",
        "no_check_deck": settings.no_check_deck,
        "no_shuffle_deck": settings.no_shuffle_deck,
        "start_lp": settings.start_lp,
        "start_hand": settings.start_hand,
        "draw_count": settings.draw_count,
        "time_limit": settings.time_limit,
        "save_replay": settings.save_replay,
    }
