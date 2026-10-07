"""对局会话：把房间、闸门、bot 进程与记录器串成一局比赛。

一局的生命周期::

    起内核房间（ygopro.exe，拿到内核端口）
      → 起闸门（对外唯一入口，随机端口 + 口令）
      → 让 bot 带着口令与抽中的卡组连进闸门
      → 把「地址 + 端口 + 口令」交给群里的调用方去播报
      → 等群友进来打（闸门负责口令校验与报文观测）
      → 收到对局结束信号后，用记录器生成复述
      → 无论成败都收摊，确保不留孤儿进程与占用端口

会话只负责「一局」的事情，重复开局、发消息、写 planner 都由插件层决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import asyncio
import logging
import secrets
import time

from .cards import CardDatabase
from .deckpool import StoredDeck
from .gate import ROLE_BOT, DuelGate
from .netutil import format_endpoint
# ⚠ 这里原来还有 `from .playbook import write_playbook_file`：会话开局前把卡组打法数据
# 写成文件交给 WindBot（只有计划感知执行器会读）。打法数据 / AI 打牌整条链路已按
# 2026-10-07 用户口径删除，`duel/playbook.py` 也删了，所以这行没了。
from .recorder import DuelRecorder
from .room import ProcessError, RoomSettings, WindBotProcess, WindBotSettings, YgoProRoom
from .protocol import Frame
from .taunts import TauntPicker


# 会话结束原因
OUTCOME_FINISHED = "finished"
OUTCOME_NO_PLAYER = "no_player"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ABORTED = "aborted"
OUTCOME_ERROR = "error"

# 口令字母表：去掉容易看错的 0/O/1/l/I，方便群友手输
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# 口令长度
_PASSWORD_LENGTH = 6


def generate_password(length: int = _PASSWORD_LENGTH) -> str:
    """生成房间口令。

    这里用 ``secrets`` 而不是 ``random``：口令是对局房间的准入凭据，
    可预测就意味着别人能猜到并占用房间。
    """

    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


@dataclass
class SessionConfig:
    """一局对局需要的全部配置与路径。"""

    ygopro_executable: Path
    ygopro_dir: Path
    windbot_executable: Path
    windbot_dir: Path
    cards_cdb: Optional[Path] = None
    room: RoomSettings = field(default_factory=RoomSettings)
    bot_name: str = "麦麦"
    windbot_deck: str = "DoEveryThing"
    dialog: str = ""
    bot_debug: bool = False
    public_host: str = "127.0.0.1"
    public_port: int = 0
    listen_host: str = "0.0.0.0"
    listen_port: int = 0
    join_timeout: float = 300.0
    max_duration: float = 3600.0
    handshake_timeout: float = 20.0
    on_status: Optional[Callable[[str, Dict[str, object]], None]] = None
    on_frame: Optional[Callable[[str, str, Frame], None]] = None
    in_game_chat: bool = False
    """是否让 WindBot 发它自带的游戏内台词；默认关闭，保持对局干净。"""
    bot_deck_file: Optional[Path] = None
    """bot 侧实际使用的 .ydk。留空则用抽中卡组的存档文件。

    擂台并发跑对局时会为每一局准备独立的卡表副本（见 train/arena.py），
    这样各局可以有不同的牌序而不互相干扰。
    """
    # ⚠ 这里原来还有四个字段：`plan_file`（作战计划）／`playbook` + `playbook_file`
    # （卡组打法数据与它的文件路径）／`brain_file`（逐步问 AI 的问答前缀）。
    # 它们都服务于已删的"计划感知执行器 PlanAware"（AI 教练 / 打法数据 / 逐步问 AI），
    # 2026-10-07 用户口径把整条链路删掉了，写文件的代码（`_playbook_path`）也一起删除。
    taunt_enabled: bool = True
    """是否在局内按概率说挑衅台词。"""
    taunt_chance_per_second: float = 0.03
    """每秒说一句挑衅的概率（0.03 即平均每 33 秒一句）。"""
    taunt_lines: Sequence[str] = ()
    """挑衅台词池（配置里可改）。空 = 用 :data:`duel.taunts.TAUNT_LINES` 那套内置台词。"""


@dataclass
class SessionInfo:
    """开局后要发给群友的信息。

    Attributes:
        port: 闸门在本机实际监听的端口，也就是对局进程与本地客户端要连的端口。
        public_host: 要发给群友的地址。局域网直连时就是本机地址，做内网穿透时是隧道域名。
        public_port: 要发给群友的端口。内网穿透把公网端口映射到别的本地端口时，
            这里与 ``port`` 不同；为 0 表示与 ``port`` 相同。
    """

    host: str
    port: int
    password: str
    deck: Optional[StoredDeck]
    backend_port: int
    public_port: int = 0

    @property
    def advertised_port(self) -> int:
        """实际发给群友的端口。"""

        return self.public_port or self.port

    def connection_hint(self, platform: str = "") -> str:
        """生成加入房间的指引文本。

        不传 ``platform`` 时把两种客户端的步骤都写上：接入信息与协议完全相同，
        分平台只是文案差别，而**为了问清平台而拖住开局更糟**——有的群规禁止提问，
        机器人会卡在「想开房 → 得先问平台 → 不能问」上，房间就一直开不出来。
        """

        lines = [f"服务器 {format_endpoint(self.host, self.advertised_port)}", f"房间密码 {self.password}"]
        if platform == "ygomobile":
            lines.append("YGOMobile：在「编辑服务器」里填上面的地址与端口，加入房间时填密码")
        elif platform == "mdpro3":
            lines.append("MDPro3：在「联机」里填上面的地址与端口，密码框填房间密码")
        else:
            lines.append(
                "MDPro3 在「联机」里填地址端口、密码框填房间密码；"
                "YGOMobile 在「编辑服务器」里填地址端口、加入时填密码"
            )
        return "\n".join(lines)


class DuelSession:
    """一局对局。

    Args:
        config: 会话配置。
        group_id: 发起对局的群号，用于卡组归属与落库。
        human_name_hint: 已知的群友昵称，只用于播报兜底。
        deck: 随机抽中的卡组，None 表示让 bot 用它自带卡组。
        card_db: 卡名数据库，可为空。
        logger: 日志器。
    """

    def __init__(
        self,
        config: SessionConfig,
        *,
        group_id: str,
        human_name_hint: str = "",
        deck: Optional[StoredDeck] = None,
        card_db: Optional[CardDatabase] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._group_id = group_id
        self._deck = deck
        self._logger = logger or logging.getLogger(__name__)
        self._recorder = DuelRecorder(
            card_db=card_db,
            group_id=group_id,
            human_name_hint=human_name_hint,
            start_lp=config.room.start_lp,
            logger=self._logger,
        )
        self._room = YgoProRoom(
            config.ygopro_executable, config.ygopro_dir, config.room, logger=self._logger
        )
        self._gate: Optional[DuelGate] = None
        self._windbot: Optional[WindBotProcess] = None
        self._info: Optional[SessionInfo] = None
        self._finished = asyncio.Event()
        self._started_at = 0.0
        self._taunt_picker = TauntPicker(config.taunt_lines or None)
        self._taunt_task: Optional[asyncio.Task[None]] = None
        self._taunt_count = 0

    @property
    def taunt_count(self) -> int:
        """本局已经说出去的挑衅句数。"""

        return self._taunt_count

    # ⚠ 这里原来有一个 `_playbook_path`：把卡组打法数据（卡组记录里的或配置里给的）
    # 写成文件再交给 WindBot。打法数据已随 AI 打牌整条链路删除（2026-10-07 用户口径），
    # 写文件这条路没了。

    @property
    def info(self) -> Optional[SessionInfo]:
        """开局信息，未启动时为 None。"""

        return self._info

    @property
    def gate(self) -> Optional[DuelGate]:
        """闸门对象，未启动时为 None。"""

        return self._gate

    @property
    def room(self) -> YgoProRoom:
        """房间内核对象，可用于查询进程状态与端口。"""

        return self._room

    @property
    def deck(self) -> Optional[StoredDeck]:
        """本局 bot 使用的卡组。"""

        return self._deck

    async def start(self, *, password: str = "") -> SessionInfo:
        """起房间、起闸门、把 bot 放进去，返回给群友的连接信息。"""

        room_password = password or generate_password()
        bot_password = generate_password(10)
        self._started_at = time.time()

        backend_port = await self._room.start()
        self._logger.info(
            "房间内核就绪：内核端口 %s，规则 %s",
            backend_port,
            self._config.room.to_args(),
        )

        gate = DuelGate(
            backend_host="127.0.0.1",
            backend_port=backend_port,
            public_password=room_password,
            bot_password=bot_password,
            observe_roles=(ROLE_BOT,),
            on_frame=self._on_frame,
            on_status=self._on_gate_status,
            handshake_timeout=self._config.handshake_timeout,
            logger=self._logger,
        )
        self._gate = gate
        gate_port = await gate.start(
            listen_host=self._config.listen_host, listen_port=self._config.listen_port
        )

        windbot_settings = WindBotSettings(
            name=self._config.bot_name,
            # 卡组记录里存了为它挑好的出牌思路就用它（投稿时按卡表相似度算出来的），
            # 没有则退回配置里指定的兜底风格
            deck=(self._deck.windbot_deck if self._deck and self._deck.windbot_deck else self._config.windbot_deck),
            deck_file=self._config.bot_deck_file
            or (self._deck.ydk_path if self._deck else None),
            password=bot_password,
            dialog=self._config.dialog,
            db_path=self._config.cards_cdb,
            debug=self._config.bot_debug,
            chat=self._config.in_game_chat,
        )
        self._windbot = WindBotProcess(
            self._config.windbot_executable,
            self._config.windbot_dir,
            windbot_settings,
            logger=self._logger,
        )
        try:
            await self._windbot.start("127.0.0.1", gate_port)
        except ProcessError:
            await self.stop()
            raise

        self._info = SessionInfo(
            host=self._config.public_host,
            port=gate_port,
            password=room_password,
            deck=self._deck,
            backend_port=backend_port,
            public_port=self._config.public_port,
        )
        await self._warn_if_backend_exposed()
        if self._config.taunt_enabled and self._config.taunt_chance_per_second > 0:
            self._taunt_task = asyncio.create_task(self._taunt_loop())
        return self._info

    async def say(self, text: str) -> bool:
        """以麦麦的身份在游戏内说一句话（走闸门写 ``CTOS_CHAT``）。"""

        if self._gate is None or not text:
            return False
        return await self._gate.send_chat(text, role=ROLE_BOT)

    async def _taunt_loop(self) -> None:
        """对局进行中每秒掷一次骰子，中了就说一句挑衅。

        只在「对局已开始且还没结束」时说话：等人进房间的时候不需要嘴炮，
        而开局前内核那边也还没有可以发言的对局。
        """

        chance = self._config.taunt_chance_per_second
        while True:
            await asyncio.sleep(1.0)
            recorder = self._recorder
            if not recorder.started or recorder.finished:
                continue
            if not self._taunt_picker.should_taunt(chance):
                continue
            line = self._taunt_picker.pick()
            if not line:
                continue
            if await self.say(line):
                self._taunt_count += 1
                if self._logger is not None:
                    self._logger.info("局内挑衅：%s", line)
            elif self._logger is not None:
                self._logger.debug("挑衅没发出去（bot 还没进房间？）：%s", line)

    def field_snapshot(self) -> List[str]:
        """当前局面：双方生命值、怪兽数、魔陷数。"""

        return self._recorder.field_snapshot()

    @property
    def turn_count(self) -> int:
        """已经进行的回合数。"""

        return self._recorder.turn_count

    @property
    def started(self) -> bool:
        """对局是否已经开打。"""

        return self._recorder.started

    @property
    def finished(self) -> bool:
        """对局是否已经结束。"""

        return self._recorder.finished

    async def _warn_if_backend_exposed(self) -> None:
        """若内核也监听在非回环地址上，提示存在绕过闸门的直连风险。"""

        if self._config.public_host in ("", "127.0.0.1", "localhost"):
            return
        exposed = await self._room.is_exposed_beyond_loopback(self._config.public_host)
        if exposed:
            self._logger.warning(
                "内核端口 %s 在 %s 上也可直连，知道该端口的人可以绕过房间口令；"
                "如需严格隔离，请用防火墙只放行闸门端口 %s",
                self._room.port,
                self._config.public_host,
                self._info.port if self._info else "?",
            )

    async def wait_finished(self) -> str:
        """等对局结束，返回结束原因。

        Returns:
            :data:`OUTCOME_FINISHED` / :data:`OUTCOME_NO_PLAYER` /
            :data:`OUTCOME_TIMEOUT` / :data:`OUTCOME_ERROR` 之一。
        """

        loop = asyncio.get_running_loop()
        join_deadline = loop.time() + self._config.join_timeout
        hard_deadline = loop.time() + self._config.max_duration
        human_seen = False

        while True:
            now = loop.time()
            if now >= hard_deadline:
                return OUTCOME_TIMEOUT
            if not human_seen:
                if self._gate is not None and self._gate.status.human_clients:
                    human_seen = True
                    self._logger.info("群友已进入房间，开始等对局结果")
                elif now >= join_deadline:
                    return OUTCOME_NO_PLAYER
            deadline = hard_deadline if human_seen else min(join_deadline, hard_deadline)
            try:
                await asyncio.wait_for(self._finished.wait(), timeout=max(deadline - now, 0.1))
                return OUTCOME_FINISHED
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        """收摊：关 bot、关闸门、关房间。任何一步失败都不影响其余步骤。"""

        # 先停「每秒掷骰子」的任务，再关闸门——否则它可能在闸门关闭过程中还想说话
        if self._taunt_task is not None:
            self._taunt_task.cancel()
            self._taunt_task = None

        for label, action in (
            ("WindBot", self._windbot.stop if self._windbot else None),
            ("闸门", self._gate.stop if self._gate else None),
            ("房间内核", self._room.stop),
        ):
            if action is None:
                continue
            try:
                await action()
            except Exception:  # noqa: BLE001  收摊阶段必须尽力而为，不能因为一步失败就漏掉后续清理
                self._logger.exception("关闭%s时出错", label)

    def kill_now(self) -> None:
        """立刻收摊：同步结束所有子进程与连接，一次都不 await。

        **给插件卸载用**。``stop()`` 是优雅收尾（每个子进程最多等 10 秒退出），
        而宿主给插件卸载的总预算是 5 秒：对局在跑时 WindBot 与内核各等一次就超时，
        宿主判"卸载失败"并重启插件，对局里的连接随之断掉——实测就是这个现象。
        卸载场景只要求"进程与端口立刻断干净"，日志收尾不重要，所以这里直接 kill。
        """

        if self._taunt_task is not None:
            self._taunt_task.cancel()
            self._taunt_task = None
        for kill in (
            self._windbot.kill_now if self._windbot else None,
            self._gate.kill_now if self._gate else None,
            self._room.kill_now,
        ):
            if kill is None:
                continue
            try:
                kill()
            except Exception:  # noqa: BLE001  卸载阶段必须尽力，不能因为一步失败漏掉后续
                self._logger.debug("卸载时强制结束组件失败", exc_info=True)

    # ------------------------------------------------------------------ 结果

    def summary_lines(self) -> List[str]:
        """对局复述。"""

        return self._recorder.summary_lines()

    def result_dict(self) -> Dict[str, object]:
        """结构化结果，用于落库与写入 planner 上下文。"""

        data = self._recorder.to_dict()
        data["duration_seconds"] = int(time.time() - self._started_at) if self._started_at else 0
        data["deck_name"] = self._deck.display_name if self._deck else ""
        data["deck_style"] = self._deck.windbot_deck if self._deck else ""
        return data

    def recorder(self) -> DuelRecorder:
        """暴露记录器，便于插件读取更多细节（例如网关拒绝次数）。"""

        return self._recorder

    # ------------------------------------------------------------------ 回调

    def _on_frame(self, role: str, direction: str, frame: Frame) -> None:
        """闸门观测回调：先喂给记录器，再通知外部观察者（如实验脚本的实时打印）。"""

        self._recorder.feed(direction, frame)
        if self._config.on_frame is not None:
            self._config.on_frame(role, direction, frame)

    def _on_gate_status(self, event: str, payload: Dict[str, object]) -> None:
        """闸门状态回调：对局结束时唤醒等待方，并把事件透传给插件。"""

        if event == "duel_ended":
            self._finished.set()
        elif event == "client_left" and payload.get("role") == ROLE_BOT and self._recorder.started:
            # bot 进程掉了，对局不可能继续，直接收摊。对局已经结束时这只是正常收尾
            # （WindBot 打完就退），训练时一局一条会很吵，所以分级别。
            if self._recorder.finished:
                self._logger.debug("bot 已离场（对局已结束）")
            else:
                self._logger.warning("bot 连接已断开，提前结束本局")
            self._finished.set()
        if self._config.on_status is not None:
            self._config.on_status(event, payload)
