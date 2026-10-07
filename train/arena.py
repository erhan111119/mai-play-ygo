"""训练擂台：批量跑「脚本 A vs 脚本 B」的对局，供训练与基线测量使用。

和线上对局的区别：

* **不需要真人**：两边都是 WindBot（一个用 bot 口令、一个用群友口令连进闸门），
  所以一局能全自动打完；
* **配对 + 降噪**：同一对脚本按「镜像」跑两局（各坐一次先攻），配合
  ``no_shuffle_deck``（牌序固定）把洗牌噪声压掉——不然胜率差别全被运气盖住；
* **可并行**：每局一个内核进程 + 一个闸门 + 两个 WindBot，用信号量限并发；
* **一切落库**：每局一行，含胜负、回合、双方动作数与 LP，方便回头算胜率与
  「它到底有没有在出牌」。

闸门必须在链路里：内核不会把胜负写进日志，WindBot 的 release 构建也不打结果，
**唯一能观测到胜负的途径就是闸门旁路出来的报文**（`DuelRecorder`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import asyncio
import logging
import random
import time
import uuid

# 与 duel/* 一致：按顶层包导入（插件根目录需在 sys.path 上，tools/ 与 tests/ 都这么干）
# DuelOutcome 定义在 duel/duelrecord.py：**插件也要用它**，而插件只能相对导入 duel/**，
# 不能碰到这个文件里的绝对导入（护栏测试会抓）。
from duel.duelrecord import DuelOutcome
from duel.knowledge import duel_outcome_text
from duel.netguard import guarded_request
from duel.playbook import write_playbook_file
from duel.room import ProcessError, RoomSettings, WindBotSettings, forward_process_output
from duel.session import OUTCOME_FINISHED, DuelSession, SessionConfig

from .coach import Coach, TurnContext
from .ai_brain import (
    SCOPE_ALL,
    BrainServer,
    build_prompt,
    decide_with_rule,
    normalise_answer,
)
from .plan import PlanLog, write_plan


@dataclass(frozen=True)
class Fighter:
    """擂台上的一方：一副卡表 + 驱动它的出牌脚本风格。

    Args:
        name: 展示名（一般用卡组名或训练代号）。
        deck_file: .ydk 路径。
        style: WindBot 注册的出牌脚本名（``Deck=`` 参数）；写错不会报错，
            WindBot 会静默换随机卡组，所以这里的取值必须来自已验证的名单。
        executable: 这一方用哪个 WindBot.exe。

            **为什么必须能分开指定**：出牌脚本是编译进 exe 的（WindBot 用反射扫
            ``[Deck(...)]`` 注册），计划感知执行器只存在于自己编译的那份里。
            拿原版 exe 跑 ``Deck=PlanAware`` 不会报错——``DecksManager`` 会**随机挑一个
            Normal 档执行器**顶上（见 windbot-src/Game/AI/DecksManager.cs），
            于是计划文件根本没人读，实验测出来的是"随机脚本"而不是计划感知。
            留空表示用配置里那个 exe。
    """

    name: str
    deck_file: Path
    style: str
    executable: Optional[Path] = None
    playbook: str = ""
    """这一方的卡组打法数据（``duel/playbook.py`` 格式的文本）。

    只有通用执行器 ``PlanAware`` 会读它。擂台上用它做对照实验："同一副牌、同一个执行器，
    带打法数据 vs 不带"——这样才能说清"模型写的这份数据到底有没有用"。
    """
    brain: str = ""
    """逐步问 AI 的策略：``""``（不问）/ ``rule``（固定策略，对照与联调用）/ ``llm``（问模型）。

    这是"第一步实测"：只把"要不要发动"和"打谁"两件事交给 AI，其余照脚本；
    每次决策的执行器会等一份答复文件（超时就按脚本自己判断），答复由 :class:`train.ai_brain.BrainServer` 写。"""

    brain_scope: str = ""
    """问 AI 的范围：``""`` / ``all``（每一问都问，历史行为）/ ``high_stakes``（只问高压决策）。

    ``high_stakes`` 下"本家卡要不要发动"交回脚本，只留"该不该交坑 / 打谁 / 这回合走哪条线"——
    实测 AI 的发动否决率 41%~86%、开 AI 反而少打 16%（见 :func:`train.ai_brain.needs_model`）。
    """

    deck_id: int = 0
    """这副牌在卡组池里的编号；用来取它的**攻略要点**（``<数据目录>/combos/<编号>.txt``）
    与**知识库线路**（``deck:<编号>``）。

    为什么要在这里带上：没有攻略要点时模型会凭常识"省牌"，把展开链上的关键卡省下来不发
    （用户实测"连基础展开都断了"）。房间那边一直是带攻略问的，擂台若不带，量到的就不是
    线上那套东西了——那样 A/B 结论再漂亮也没意义。0 表示"这副牌没有攻略"。"""

    knowledge: bool = True
    """这一方问 AI 时是否**按局面检索知识库**（``duel/knowledge.py``）。

    True＝临场检索（这张牌的事实 / 我这副牌的线路 / 对手轴系的威胁），False＝只用静态素材
    （攻略要点 + 打法数据）。两者都要能在擂台上量到，所以做成每一方各自的开关。
    """


@dataclass
class ArenaConfig:
    """擂台设置。

    Args:
        ygopro_executable: 内核路径（训练建议用引擎目录的副本，别和线上抢）。
        ygopro_dir: 内核工作目录。
        windbot_executable: WindBot 路径。
        windbot_dir: WindBot 工作目录。
        cards_cdb: 卡表数据库，供 WindBot 与记录器查卡名。
        parallel: 同时跑几局（每个逻辑核心一局左右都行，16 核建议 6-8）。
        start_lp: 初始生命值；训练时调低能显著缩短单局。
        no_shuffle_deck: 是否关闭洗切；开启后牌序固定，镜像配对才能成对比较。
        time_limit: 单回合时限（秒），调小可以避免卡住的脚本拖垮整个擂台。
        max_duration: 单局硬上限（秒）。
        save_replay: 是否留录像；训练时建议关掉，免得 replay 目录堆满。
    """

    ygopro_executable: Path
    ygopro_dir: Path
    windbot_executable: Path
    windbot_dir: Path
    cards_cdb: Optional[Path] = None
    parallel: int = 6
    start_lp: int = 4000
    no_shuffle_deck: bool = True
    time_limit: int = 30
    max_duration: float = 120.0
    """单局硬上限（秒）。日志里的实测单局在 3–21 秒，120 秒足够，
    而在某个客户端崩掉时能少浪费一个并发位。"""
    save_replay: bool = False
    coach: Optional[Coach] = None
    """教练：每回合给计划感知执行器写一份作战计划；None 表示不下计划（纯通用打法）。

    计划写在 WindBot 的工作目录下（执行器按相对路径读），所以两边共用同一个工作目录时，
    只有"用计划感知执行器"的那一方会去读它——对照组不受影响。
    """
    shuffle_decks: bool = True
    """每轮是否用固定种子重洗主卡组：关洗切时让各轮的起手不同（且像真实对局），N 轮才是 N 个样本。"""
    plan_style: str = "PlanAware"
    """计划感知执行器的注册名（对应 C# 里的 [Deck(...)]）。"""
    plan_executable: Optional[Path] = None
    """计划感知执行器所在的 WindBot.exe（A/B 里"实验组"这一方用它）。

    必须指到**编译进了 PlanAwareExecutor 的那份**。出牌脚本是编译进 exe 的，
    WindBot 找不到 ``Deck=`` 请求的名字时不会报错，而是随机挑一个 Normal 档执行器顶上
    （windbot-src/Game/AI/DecksManager.cs）——计划文件于是根本没人读，
    实验安静地测成了"随机脚本互打"。所以 A/B 实验缺这个路径时直接报错，不给兜底。
    """
    verbose_bots: bool = False
    """是否把两个 bot 自己的输出（含逐回合决策）转发到日志。

    "某一方整局没动作"这类问题只能从 bot 自己的输出里看出原因，默认这些行是被丢掉的。
    """
    shuffle_seed_base: int = 0
    """牌序种子基准：第 N 轮的实际种子是 ``base + N``（见 :func:`shuffle_deck`）。

    为什么要能改：卡组进化搜索里每个候选必须用**不同**的牌序集合，否则候选会"背下"
    这几十个开局，赢的只是过拟合（关洗切时牌序就是文件顺序，这件事在训练里尤其致命）。

    ⚠ **只在"内核不洗切"时才管手牌**（``no_shuffle_deck=True``）。内核洗切开着时
    （真实房间、以及 ``--shuffle`` 那一档）开局由**内核自己再洗一遍**，而我们没有任何
    种子能传给它（``RoomSettings.to_args`` 的 12 个参数里没有种子）——实测同 base 重跑，
    两遍的开局手牌一张都不重合。**单局复现只能在"不洗切"模式下做。**
    """


class DuelArena:
    """并行跑对局的擂台。"""

    def __init__(self, config: ArenaConfig, *, logger: Optional[logging.Logger] = None) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._semaphore = asyncio.Semaphore(max(1, config.parallel))

    async def play_pair(
        self,
        left: Fighter,
        right: Fighter,
        *,
        rounds: int = 1,
        on_outcome: Optional[Callable[["DuelOutcome"], None]] = None,
        shared_deck: bool = False,
    ) -> List[DuelOutcome]:
        """跑一对脚本的镜像对局：每轮两局（各坐一次先攻），各轮**并行**发起。

        两点值得说明：

        * **每轮一份独立卡表副本**：关洗切时"牌序"就是文件顺序，所以每轮要先洗出这一轮的牌序；
          若各轮共用同一个文件，并行时就会互相踩（这一轮刚洗完被那一轮覆盖）。
          副本跑完就删，免得目录里越堆越多。
        * **真的并行**：早先的实现是逐局 ``await``，信号量根本没用上——实测 56 局跑了 13 分钟，
          一直是串行。现在把各局的 task 一次性发出去，由信号量控并发。

        Args:
            left: 先以座位 0 出场的脚本。
            right: 先以座位 1 出场的脚本。
            rounds: 跑几轮镜像（每轮 2 局）。
            on_outcome: 每局**打完立刻**回调一次（结果落库、打印进度用）。

                为什么需要它：400 局一批时如果等全部跑完再落库，中途只能干看着——
                既不知道跑到哪了，也分不清"慢"和"卡死"。回调让结果边打边入库。
            shared_deck: 双方是否**共用左方那副牌**。默认 False＝各打各的牌；
                只有"同一副牌、比较两个出牌脚本"（`--pair-styles`）才该传 True。

                这里踩过一次：共用牌表被用在"两副牌对战"上，右边那方于是被迫打左边的牌，
                它的执行器不认识那些卡 ⇒ 整局一步不走，外面看到的是"对手 0 动作、
                一路空过拖到 72 回合"。更早那些"胜率"因此测的其实是**脚本**而不是卡组。

        Returns:
            每局一个 :class:`DuelOutcome`（顺序按轮次与座位）。
        """

        plans_dir = Path(self._config.windbot_dir) / "MaiBotPlans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        tasks: List[asyncio.Task] = []
        left_decks: List[Path] = []
        right_decks: List[Path] = []
        for index in range(max(1, rounds)):
            # 这一轮各自的卡表副本（洗牌后的牌序）；要共用就都以左方为准
            left_deck = self._copy_round_deck(left, index, shared_from=left, plans_dir=plans_dir)
            right_deck = (
                left_deck
                if shared_deck
                else self._copy_round_deck(right, index, shared_from=right, plans_dir=plans_dir)
            )
            left_decks.append(left_deck)
            right_decks.append(right_deck)
            for left_seat in (0, 1):
                task = asyncio.create_task(
                    self.play_duel(
                        left,
                        right,
                        left_seat=left_seat,
                        left_deck_file=left_deck,
                        right_deck_file=right_deck,
                        round_index=index,
                    )
                )
                if on_outcome is not None:
                    task.add_done_callback(_outcome_callback(on_outcome))
                tasks.append(task)
        outcomes = await asyncio.gather(*tasks)
        for path in {*left_decks, *right_decks}:
            path.unlink(missing_ok=True)
        return list(outcomes)

    def _copy_round_deck(
        self, fighter: Fighter, index: int, *, shared_from: Fighter, plans_dir: Path
    ) -> Path:
        """给这一轮复制一份卡表（并按轮次种子洗牌），返回副本路径。

        第 0 轮也保留原始牌序：这样"同一配置重跑一遍"能得到完全一样的对局（可复现）。
        读不到文件时退回原文件——不因为读文件失败中断整轮（但那样双方会共用同一个文件，
        并行时可能互相踩，所以只在真的读不到时才会走到）。
        """

        target = plans_dir / f"deck_{fighter.name[:12]}_r{index}_{uuid.uuid4().hex[:6]}.ydk"
        try:
            base_text = Path(shared_from.deck_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return shared_from.deck_file
        target.write_text(base_text, encoding="utf-8")
        if self._config.shuffle_decks and index:
            shuffle_deck(target, seed=self._config.shuffle_seed_base + index)
        return target

    async def play_duel(
        self,
        left: Fighter,
        right: Fighter,
        *,
        left_seat: int = 0,
        left_deck_file: Optional[Path] = None,
        right_deck_file: Optional[Path] = None,
        round_index: int = -1,
    ) -> DuelOutcome:
        """打一局，``left`` 坐 ``left_seat`` 号位（0 号位先攻）。

        Args:
            left: 结果里记为 ``left`` 的一方。
            right: 记为 ``right`` 的一方。
            left_seat: 0 或 1，决定 ``left`` 坐哪个座位；胜负按名字记账，
                所以镜像两局能记在同一个口径下。
            left_deck_file: 左方这一局用的卡表副本（默认用 ``left.deck_file``）。
            right_deck_file: 右方这一局用的卡表副本（默认用 ``right.deck_file``）。
                两侧分开传是必须的：两副不同的牌对战时要各打各的牌，只传一侧会让
                另一方被迫打对手的牌（它的执行器不认识那些卡 ⇒ 一步不走）。
            round_index: 第几轮镜像，写进结果里便于复现。

        Returns:
            这一局的结果；出错时在 ``error`` 里写明原因，不抛异常——
            一局失败不该中断整轮训练。
        """

        seat_zero, seat_one = (left, right) if left_seat == 0 else (right, left)
        async with self._semaphore:
            try:
                return await self._play(
                    seat_zero,
                    seat_one,
                    left=left,
                    right=right,
                    left_seat=left_seat,
                    left_deck_file=left_deck_file,
                    right_deck_file=right_deck_file,
                    round_index=round_index,
                )
            except Exception as exc:  # noqa: BLE001  单局失败不该中断训练
                if self._logger is not None:
                    self._logger.warning("对局失败：%s vs %s：%s", left.name, right.name, exc)
                return DuelOutcome(
                    left=left.name,
                    right=right.name,
                    left_seat=left_seat,
                    winner="unknown",
                    turns=0,
                    duration_seconds=0,
                    reason="",
                    actions_left=0,
                    actions_right=0,
                    lp_left=self._config.start_lp,
                    lp_right=self._config.start_lp,
                    error=f"{type(exc).__name__}: {exc}",
                    round_index=round_index,
                )

    async def _play(
        self,
        seat_zero: Fighter,
        seat_one: Fighter,
        *,
        left: Fighter,
        right: Fighter,
        left_seat: int = 0,
        left_deck_file: Optional[Path] = None,
        right_deck_file: Optional[Path] = None,
        round_index: int = -1,
    ) -> DuelOutcome:
        """真正起一局：内核 + 闸门 + 两个 WindBot，等打完收摊。

        ``seat_zero``/``seat_one`` 只决定谁坐哪、谁先攻；``left``/``right`` 只用于记账。
        ``left_seat`` 是**镜像配对安排的那个座位**（记进结果里，读结果时口径不变）；
        内核实际给双方分的对局玩家号要等 ``MSG_START`` 才知道，两者不一定一致。
        ``left_deck_file``/``right_deck_file`` 跟着**人**走，镜像换座位时各自仍打自己的牌。
        """

        # 每个对局一个独立的计划文件：擂台并发跑对局时，共用一个路径会互相撞
        # （Windows 上表现为文件占用报错，整批对局都被判失败）
        plan_path = self._plan_path_for_duel()
        # 各方的卡表副本（并行的各轮各有各的牌序，互不干扰）；按"人"取，再按座位派
        zero_deck = (left_deck_file or left.deck_file) if seat_zero is left else (right_deck_file or right.deck_file)
        one_deck = (right_deck_file or right.deck_file) if seat_one is right else (left_deck_file or left.deck_file)
        # 逐步问 AI：**哪一侧要问，就只给那一侧起服务**（另一侧照脚本打，这才有对照）。
        # 这里起的是会话自己那台 WindBot（seat_zero）的服务；seat_one 那台在下面单独起。
        # **两边要各传自己的 fighter**：以前两次都传了同一个 brain_fighter，于是同一个座位
        # 起了两份服务、两份决策日志——真正在答复的那份写日志，而收摊回填用的是没收到提问的那份，
        # 结果只有"要问 AI 的那方坐在 seat_one"的那一半对局能被回填（实测 6 局里 3 局），
        # 另外还白留一个永远没人取消的答复任务。
        zero_prefix, zero_task, zero_log = self._start_brain(seat_zero if seat_zero.brain else None)
        config = SessionConfig(
            plan_file=plan_path,
            bot_deck_file=zero_deck,
            # bot 那一侧的打法数据由会话写成文件（session._playbook_path）
            playbook=seat_zero.playbook,
            brain_file=zero_prefix,
            ygopro_executable=self._config.ygopro_executable,
            ygopro_dir=self._config.ygopro_dir,
            # 每一方可以用各自的 WindBot.exe：计划感知执行器只存在于自己编译的那份里，
            # 拿原版 exe 跑 Deck=PlanAware 会被随机执行器静默顶替（见 Fighter.executable）
            windbot_executable=seat_zero.executable or self._config.windbot_executable,
            windbot_dir=self._config.windbot_dir,
            cards_cdb=self._config.cards_cdb,
            room=RoomSettings(
                start_lp=self._config.start_lp,
                no_shuffle_deck=self._config.no_shuffle_deck,
                time_limit=self._config.time_limit,
                save_replay=self._config.save_replay,
            ),
            bot_name=seat_zero.name,
            windbot_deck=seat_zero.style,
            # 排查「整局没动作」时要看 bot 自己的决策日志
            bot_debug=self._config.verbose_bots,
            public_host="127.0.0.1",
            listen_host="127.0.0.1",
            listen_port=0,
            # 两边都是脚本，不存在「等群友进来」，所以把等待时间压到最短
            join_timeout=30.0,
            max_duration=self._config.max_duration,
            taunt_enabled=False,
        )
        # seat_one 那台（用口令连进闸门的对手）要问 AI 的话，服务在这里单独起
        one_prefix, one_task, one_log = self._start_brain(seat_one if seat_one.brain else None)
        session = DuelSession(config, group_id="arena", logger=self._logger)
        started_at = time.monotonic()
        plan_log = PlanLog()
        info = await session.start()        # bot 座位由 DuelSession 起好了（带 DeckFile），这里起第二个作为对手，
        # 用群友口令连进闸门；它的卡表与脚本风格单独指定
        opponent = await self._spawn_opponent(
            seat_one,
            info.port,
            info.password,
            plan_file=plan_path,
            deck_file=one_deck,
            # 对手那一侧没有会话帮忙写文件，这里自己写一份它自己的打法数据
            playbook_file=self._write_playbook(seat_one),
            # 逐步问 AI：只有这一方要问（另一侧照脚本打），所以前缀只给它
            brain_file=one_prefix,
        )
        # 排查时才读它的输出：谁没出牌、为什么没出牌，只有它自己的日志说得清
        opponent_log_tasks: List[asyncio.Task] = []
        if self._config.verbose_bots and self._logger is not None:
            opponent_log_tasks = forward_process_output(opponent, seat_one.name, self._logger)
        coach_task: Optional[asyncio.Task[None]] = None
        if self._config.coach is not None:
            coach_task = asyncio.create_task(
                self._coach_loop(session, self._config.coach, plan_log, plan_path)
            )
        try:
            outcome = await session.wait_finished()
        finally:
            if coach_task is not None:
                coach_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(coach_task), timeout=5)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    # 教练任务自己出错不该把整局判成失败（曾经就是这样，
                    # 5 个并发对局共用一个计划文件互相撞，结果整批对局全废）
                    if self._logger is not None:
                        self._logger.warning("教练任务异常：%s", exc)
                except asyncio.TimeoutError:
                    if self._logger is not None:
                        self._logger.warning("教练任务没能在 5 秒内收摊，已放弃等待")
            if opponent.returncode is None:
                opponent.terminate()
                try:
                    await asyncio.wait_for(opponent.wait(), timeout=10)
                except asyncio.TimeoutError:
                    opponent.kill()
            for task in opponent_log_tasks:
                task.cancel()
            await session.stop()
            plan_path.unlink(missing_ok=True)
            # 两个座位各有一份答复服务与决策日志（只有一个会真的收到提问，另一个是空的）
            for prefix, task in ((zero_prefix, zero_task), (one_prefix, one_task)):
                if task is not None:
                    task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=5)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    except Exception as exc:  # noqa: BLE001  答复任务出错不该影响这一局的结论
                        if self._logger is not None:
                            self._logger.warning("问 AI 任务异常：%s", exc)
                if prefix is not None:
                    for suffix in (".q", ".a"):
                        Path(str(prefix) + suffix).unlink(missing_ok=True)

        data = session.result_dict()
        players = data.get("players") or {}
        recorder = session.recorder()
        # 统计快照是按**对局玩家号**分桶的（记录器观测的是 DuelSession 起的那一方，
        # 它的对局玩家号在 MSG_START 里，可能是 0 也可能是 1——服务器会为先后手重排，
        # 实测 bot 先入房却拿到对局号 1）。所以不能假设 seat_zero 就是对局里的 0 号。
        observer_seat = recorder.self_seat
        observer_stats = players.get(str(observer_seat)) or {}
        other_stats = players.get(str(1 - observer_seat)) if observer_seat in (0, 1) else {}
        winner_seat = data.get("winner_seat")
        if outcome != OUTCOME_FINISHED or winner_seat is None:
            winner = "draw" if outcome == OUTCOME_FINISHED else "unknown"
        elif observer_seat in (0, 1):
            # seat_zero 是 DuelSession 起的那一方，也就是记录器观测的那条连接
            winner = seat_zero.name if int(winner_seat) == observer_seat else seat_one.name
        else:
            winner = "unknown"

        def actions(stats: Dict[str, object]) -> int:
            """把「有没有在出牌」压成一个数：召唤 + 盖放 + 效果 + 攻击。

            **盖放要算进来**（2026-10-06 起）：内核给盖放单发 ``MSG_SET``、不给 summon 报文，
            原来这条报文整条没人接，于是一副"整局只盖陷阱"的牌（虫惑魔、闪刀的后手局）
            在台账里看起来就是"整局没出牌"——那是量具的漏，不是脚本空转。
            """

            return sum(
                int(stats.get(key) or 0)
                for key in ("normal_summons", "sp_summons", "sets", "effects", "attacks")
            )

        # 谁被记录器观测到，谁的统计就在 observer 那一份里
        left_is_seat_zero = left is seat_zero
        left_stats = observer_stats if left_is_seat_zero else other_stats
        right_stats = other_stats if left_is_seat_zero else observer_stats
        # 用卡台账按**左右**取（落库要的是"被评估的那副牌用了哪些卡"）。
        # 记录器挂在 **seat_zero 那台 bot** 上（见上面 `bot_name=seat_zero.name`），而服务器会给
        # 先后手重排一次"对局玩家号"，所以左边那副的对局座位是：
        #   `left_seat == 0`（左边就是 seat_zero）→ 照抄记录器的 duel_index；否则两边互换。
        # ⚠ 不要按昵称找：记录器只看到 bot 那一侧的握手昵称，另一侧的 name 是空的（实测一半的局找不到）。
        recorder = session.recorder()
        duel_index = recorder.duel_index
        if duel_index in (0, 1):
            left_deck_seat = duel_index if left_seat == 0 else 1 - duel_index
            left_usage = dict(recorder.players[left_deck_seat].card_usage)
            right_usage = dict(recorder.players[1 - left_deck_seat].card_usage)
            # 「进过手牌」同样按左右取（只有 recorder 观测的那一侧是完整的，
            # 另一侧基本是空的——见 DuelOutcome.card_seen_opponent 的说明）
            left_seen = dict(recorder.players[left_deck_seat].cards_seen)
            right_seen = dict(recorder.players[1 - left_deck_seat].cards_seen)
        else:
            if self._logger is not None:
                self._logger.warning("用卡台账：这局没拿到对局玩家号（MSG_START 没看到？），两侧都记成空")
            left_usage, right_usage = {}, {}
            left_seen, right_seen = {}, {}
        # 生命值取「实时局面」那份：记录器的 lp_final 依赖 MSG_LPUPDATE，
        # 而这套内核不为掉血发它（见 duel/fieldstate.py），也就会一直是 0
        field_state = recorder.field_state
        observer_lp = (
            field_state.players[observer_seat].lp if observer_seat in (0, 1) else self._config.start_lp
        )
        other_lp = (
            field_state.players[1 - observer_seat].lp
            if observer_seat in (0, 1)
            else self._config.start_lp
        )

        result = DuelOutcome(
            left=left.name,
            right=right.name,
            left_seat=left_seat,
            winner=winner,
            turns=int(data.get("turns") or 0),
            duration_seconds=int(data.get("duration_seconds") or 0)
            or int(time.monotonic() - started_at),
            reason=str(data.get("reason") or outcome),
            actions_left=actions(left_stats),
            actions_right=actions(right_stats),
            sp_summons_left=int(left_stats.get("sp_summons") or 0),
            sp_summons_right=int(right_stats.get("sp_summons") or 0),
            effects_left=int(left_stats.get("effects") or 0),
            effects_right=int(right_stats.get("effects") or 0),
            sets_left=int(left_stats.get("sets") or 0),
            sets_right=int(right_stats.get("sets") or 0),
            lp_left=observer_lp if left_is_seat_zero else other_lp,
            lp_right=other_lp if left_is_seat_zero else observer_lp,
            plans=plan_log.summary(),
            round_index=round_index,
            # 每张卡被用到的次数：卡组进化搜索靠它找"整局没被用过"的死牌。
            # ⚠ 2026-10-07 起按**左右**落库（不是"观测方"）：`left` 才是这一轮被评估的那副牌，
            # 而记录器观测的可能是任意一边（实测同一批里两种都有）——不映射就会把对手的用卡
            # 记到被评估的那一副头上。用卡台账本身已按座位分开记（`duel/recorder.py`）。
            card_usage=dict(left_usage),
            card_usage_opponent=dict(right_usage),
            card_seen=dict(left_seen),
            card_seen_opponent=dict(right_seen),
        )
        # 回填决策日志：**要问 AI 的那一方在哪一边，就用它在的那一侧的日志**
        # （两份日志里只有收到提问的那份有内容，见 _start_brain 的说明）
        for log, fighter in ((zero_log, seat_zero), (one_log, seat_one)):
            if fighter.brain:
                self._finish_brain_log(log, fighter, result)
        return result

    def _finish_brain_log(
        self, log: Optional[object], fighter: Optional[Fighter], outcome: DuelOutcome
    ) -> None:
        """给这一局的决策日志回填结果（失败只记日志，绝不影响对局的结论）。

        回填之后 ``tools/review_decisions.py`` 才能按"输了的那几局它否决得更多吗"来算——
        少了这一步，日志里只有"问了什么、答了什么"，没有"后来怎样"。
        """

        if log is None or fighter is None:
            return
        if outcome.winner == fighter.name:
            result = "win"
        elif outcome.winner == "draw":
            result = "draw"
        else:
            result = "loss" if outcome.winner != "unknown" else "unknown"
        is_left = outcome.left == fighter.name
        text = duel_outcome_text(
            result=result,
            turns=outcome.turns,
            lp_self=outcome.lp_left if is_left else outcome.lp_right,
            lp_other=outcome.lp_right if is_left else outcome.lp_left,
        )
        try:
            log.finish_duel(text)
        except Exception as exc:  # noqa: BLE001  决策日志绝不能把一局的结论搅坏
            if self._logger is not None:
                self._logger.warning("决策日志回填结果失败：%s", exc)

    async def _coach_loop(
        self,
        session: DuelSession,
        coach: Coach,
        plan_log: PlanLog,
        plan_path: Path,
    ) -> None:
        """每回合请教练出一份计划，写进 WindBot 工作目录供执行器读取。

        为什么按回合而不是按决策：一次模型调用的代价是秒级，而一局有几十次决策；
        按回合出计划（"这回合该凶还是该稳"）既省钱又正好是模型擅长的那一层判断。
        执行器每 8 次决策重读一次计划文件，所以两个节奏自然错开。
        """

        recorder = session.recorder()
        last_turn = -1
        while True:
            await asyncio.sleep(0.5)
            turn = recorder.turn_count
            if turn <= last_turn or not recorder.started:
                continue
            last_turn = turn
            context = self._turn_context(recorder)
            if context is None:
                continue
            plan = await coach(context)
            if plan is None:
                continue
            write_plan(plan_path, plan)
            plan_log.add(plan, source="coach")
            if self._logger is not None:
                # 记一行时序：排查"计划没被读到"时要看的是它到底写没写、什么时候写的
                self._logger.info("写下计划：第 %d 回合 → %s", plan.turn, plan_path.name)

    def _turn_context(self, recorder) -> Optional[TurnContext]:
        """从记录器里读局面，组装成教练要的上下文。

        自己的座位由记录器维护（它观测的是 bot 那条连接），拿不到时就返回 None —— 
        宁可这回合不下计划，也不要猜错座位、把"给对手的计划"写进去。
        """

        seat = recorder.self_seat
        if seat not in (0, 1):
            return None
        mine = recorder.field_state.players.get(seat)
        theirs = recorder.field_state.players.get(1 - seat)
        if mine is None or theirs is None:
            return None
        return TurnContext(
            turn=recorder.turn_count,
            my_lp=mine.lp,
            opponent_lp=theirs.lp,
            my_monsters=mine.monsters,
            my_spells=mine.spells,
            opponent_monsters=theirs.monsters,
            opponent_spells=theirs.spells,
        )

    def _plan_path_for_duel(self) -> Path:
        """给这一局分配一个独立的计划文件路径。"""

        plans_dir = Path(self._config.windbot_dir) / "MaiBotPlans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        return plans_dir / f"plan_{uuid.uuid4().hex[:12]}.txt"

    def _write_playbook(self, fighter: Fighter) -> Optional[Path]:
        """把这一方的打法数据写成文件（没有就返回 None）。

        对手那一侧是直接 spawn 的 WindBot，没有 DuelSession 帮忙写文件，所以这里自己写一份；
        bot 那一侧由会话按 ``playbook`` 文本写（只有一条写文件的路径，见 session._playbook_path）。
        """

        text = fighter.playbook.strip()
        if not text:
            return None
        plans_dir = Path(self._config.windbot_dir) / "MaiBotPlans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        target = plans_dir / f"playbook_{fighter.name[:10]}_{uuid.uuid4().hex[:6]}.txt"
        return write_playbook_file(target, text)

    def _start_brain(
        self, fighter: Optional[Fighter]
    ) -> Tuple[Optional[Path], Optional[asyncio.Task], Optional[object]]:
        """给这一方起一个"答复 AI 提问"的任务；返回 ``(问答前缀, 任务, 决策日志)``。

        策略（``Fighter.brain``）：
          * ``rule``：固定答复（发动一律"不发动"、攻击一律"不攻击"）——只用来验证通道真的
            改变了行为（差异巨大、肉眼可见），不参与"有没有用"的结论；
          * ``llm``：每次问模型（提示词带卡名与效果文本，见 ``train/ai_brain.py``）。

        只有这一方的 WindBot 会拿到前缀，另一侧照脚本打——对照才有意义。

        第三项是这一局的 :class:`~duel.knowledge.DecisionLog`（一个实例 = 一局），
        调用方打完要用它回填胜负，否则日志里就没有"后来怎样"（见 ``_finish_brain_log``）。
        """

        if fighter is None or not fighter.brain:
            return None, None, None
        plans_dir = Path(self._config.windbot_dir) / "MaiBotPlans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        prefix = plans_dir / f"brain_{fighter.name[:10]}_{uuid.uuid4().hex[:6]}.txt"

        if fighter.brain == "rule":
            decide = lambda question: decide_with_rule(question, answer="no")  # noqa: E731
            log = None
        else:
            decide, log = self._make_llm_decider(fighter, duel_key=prefix.name)
            if decide is None:
                # 模型配不上就**不要**开这条通道：留着它只会让每次决策白等 20 秒，
                # 而房间的每回合时限是有限的——实测被自己的等待拖到几乎没时间出牌
                # （动作数从 12~18 掉到 1~6，一局从 7 秒变成 31~99 秒）。
                if self._logger is not None:
                    self._logger.warning("问 AI 用不了（模型不可用），这一方照脚本打（不开问答通道）")
                return None, None, None
        server = self._make_brain_server(prefix, decide)
        task = asyncio.create_task(server.serve_forever())
        if self._logger is not None:
            self._logger.info("逐步问 AI 已启用：%s（策略 %s）", fighter.name, fighter.brain)
        return prefix, task, log

    def _make_brain_server(self, prefix: Path, decide: Callable[[object], str]) -> BrainServer:
        """造一个答复服务。

        单独抽出来是为了让调用方**能拿到这些服务**：报告里"这一局问了多少次、答了多少次"
        要读 ``BrainServer.answered``，而服务是在这里造的。子类可以覆写它来收集计数。
        """

        return BrainServer(prefix, decide, logger=self._logger)

    def _make_llm_decider(self, fighter: Fighter, *, duel_key: str = ""):
        """造一个"把问题交给模型"的答复函数；模型不可用时返回 ``(None, None)``（调用方就不开通道）。

        **优先用插件自带的模型配置**（``<数据目录>/brain_model.toml``）：密钥不进仓库、
        也不动宿主的模型配置（这是选定的方案 B）。没有那份文件时，退回宿主模型注册表里
        ``config.toml`` 指名的模型——两条路都走不通就不开通道。

        预算不够会自动重问（见 :func:`train.ai_brain.make_model_decider`）：推理模型会把
        max_tokens 全花在思考上、回来是空内容（实测 deepseek-flash 2048 都不够）。

        返回的第二个值是这一局的决策日志（``duel_key`` 由调用方给，一局一个）。
        """

        from duel.cards import CardDatabase  # 局部导入：只有开 AI 时才需要卡库
        from duel.knowledge import DecisionLog, Knowledge
        from train.ai_brain import load_brain_models, load_combo_guide, make_model_decider

        card_db = None
        if self._config.cards_cdb is not None and Path(self._config.cards_cdb).is_file():
            card_db = CardDatabase(Path(self._config.cards_cdb))

        # 知识库：本机 SQLite，毫秒级、不花钱。没有那个库时 retrieve 返回空，等于没开
        knowledge = None
        decision_log = None
        if fighter.knowledge:
            knowledge = Knowledge.from_data_dir(
                self._data_dir(), cards_db=self._config.cards_cdb
            )
            if not knowledge.available:
                knowledge = None
            else:
                # 决策日志落到同一个库（复盘用：哪些决策点它答得差、输了的那几局它否决得更多吗）
                decision_log = DecisionLog(
                    knowledge.path,
                    arena=str(self._config.windbot_dir.name),
                    deck_key=str(fighter.deck_id),
                    duel_key=duel_key,
                )

        settings, strong = load_brain_models(self._data_dir())
        if settings is not None:
            guide = load_combo_guide(self._data_dir(), fighter.deck_id)
            if self._logger is not None:
                self._logger.info(
                    "问 AI 用插件自带的模型配置：%s（%s）%s；素材：打法数据 %s 字、攻略要点 %s 字、知识库 %s",
                    settings.model,
                    settings.base_url,
                    f"＋强模型 {strong.model}（只给「这一步做什么」）" if strong is not None else "",
                    len(fighter.playbook.strip()),
                    len(guide),
                    "开" if knowledge is not None else "关",
                )
            # 第二个返回值是这一局的决策日志：调用方打完要用它回填胜负
            return (
                make_model_decider(
                    settings,
                    request=guarded_request,
                    card_db=card_db,
                    logger=self._logger,
                    playbook=fighter.playbook,
                    deck_name=fighter.name,
                    combo_guide=guide,
                    knowledge=knowledge,
                    deck_id=fighter.deck_id,
                    decision_log=decision_log,
                    strong_settings=strong,
                    scope=fighter.brain_scope or SCOPE_ALL,
                ),
                decision_log,
            )

        # 退回宿主模型注册表（方案 A 的走法）：按 config.toml 里指名的模型取
        from train.llm import ModelClient, ModelError, pick_model

        name = ""
        config_path = Path(__file__).resolve().parent.parent / "config.toml"
        try:
            import tomllib

            with config_path.open("rb") as handle:
                name = str((tomllib.load(handle).get("duel") or {}).get("script_model") or "").strip()
        except (OSError, ValueError):
            name = ""
        try:
            target = pick_model(task="ygo_script", name=name)
        except ModelError as exc:
            if self._logger is not None:
                self._logger.warning("问 AI 用不了（既没有 brain_model.toml，也读不到宿主模型配置）：%s", exc)
            return None, None
        client = ModelClient(target, timeout=20)
        if self._logger is not None:
            self._logger.info("问 AI 用宿主模型：%s / %s", target.provider, target.model)

        def decide(question) -> str:
            prompt = build_prompt(question, card_db)
            if not prompt:
                return ""
            # **预算要给足**：推理模型的"思考"也算在 max_tokens 里，给 16 会被思考吃光、
            # 回来是空内容（finish_reason='length'、reasoning_tokens=16）——脚本生成那边
            # 踩过同一个坑，这里同样按"思考 + 一个词"给（512 够用，代价是每次多一两秒）
            raw = client.chat(prompt, max_tokens=512, temperature=0.0)
            return normalise_answer(question, raw)

        # 这条退路上没有知识库与决策日志（它只把问题直接发给模型）。
        # 前面那条路（brain_model.toml）才写决策日志，`/复盘` 读的就是那些。
        return decide, None

    def _data_dir(self) -> Path:
        """插件数据目录（从 config.toml 的位置推出来：``<宿主>/data/plugins/<插件 id>/``）。"""

        plugin_root = Path(__file__).resolve().parent.parent
        manifest = plugin_root / "_manifest.json"
        plugin_id = ""
        try:
            import json as _json

            plugin_id = str(_json.loads(manifest.read_text(encoding="utf-8")).get("id") or "")
        except (OSError, ValueError):
            plugin_id = ""
        if plugin_id:
            return plugin_root.parent.parent / "data" / "plugins" / plugin_id
        return plugin_root / "data"

    async def _spawn_opponent(
        self,
        fighter: Fighter,
        port: int,
        password: str,
        *,
        plan_file=None,
        deck_file=None,
        playbook_file=None,
        brain_file=None,
    ):
        """起对手那一侧的 WindBot（用群友口令连闸门，闸门就当它是玩家）。"""

        settings = WindBotSettings(
            name=fighter.name,
            deck=fighter.style,
            deck_file=deck_file or fighter.deck_file,
            password=password,
            db_path=self._config.cards_cdb,
            plan_file=plan_file,
            playbook_file=playbook_file,
            brain_file=brain_file,
            debug=self._config.verbose_bots,
        )
        executable = fighter.executable or self._config.windbot_executable
        # 只有排查时才接管道：正常跑对局不该为没人看的输出付读管道的代价
        verbose = self._config.verbose_bots
        try:
            return await asyncio.create_subprocess_exec(
                str(executable),
                *settings.to_args("127.0.0.1", port),
                cwd=str(self._config.windbot_dir),
                stdout=asyncio.subprocess.PIPE if verbose else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if verbose else asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ProcessError(f"起对手 WindBot 失败：{exc}") from exc


def _outcome_callback(on_outcome: Callable[["DuelOutcome"], None]):
    """把「task 打完」转成「交出一局结果」的回调。

    ``play_duel`` 自己吞掉所有异常（一局失败不该中断整轮），所以这里只需防住
    被取消的 task —— 直接取 ``result()`` 会在取消时抛。
    """

    def callback(task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        on_outcome(task.result())

    return callback


def shuffle_deck(deck_file: Path, seed: int) -> None:
    """用固定种子把主卡组顺序打乱（同样的种子得到同样的顺序）。

    **为什么不是"循环移位"**：训练规则关掉了洗切（为了让镜像配对能成对比较），于是"牌序"
    就是 .ydk 文件里的顺序。而 AI 卡表是按类型分组写的——实测把主卡组循环移位取起手，
    某些轮次拿到的是「3×银龙的轰咆 + 青眼白龙」这种一张都打不出去的烂手牌，
    于是那一局看起来就是"整局空过"。那不是执行器的毛病，是我们的取样方式不真实。
    改成"固定种子随机洗牌"后：每轮的起手像真实对局，且仍然可复现、可配对比较。

    ⚠ **可复现的前提是内核不洗切**（``no_shuffle_deck=True``，即不传 ``--shuffle``）：
    这一层只是把**文件**顺序洗好，内核若还开着洗切（真实房间 / ``--shuffle``），
    它会在这份顺序上再随机洗一遍，种子无从控制 → 手牌每局都不同。
    **"不洗切"模式下的镜像两局是同一副牌序**（实测两局动作数 46:17 对 46:17），
    配对最强；``--shuffle`` 模式下 20 局是 20 个独立样本，配对只剩"同卡表"。
    """

    deck = Path(deck_file)
    try:
        lines = deck.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    start = None
    end = None
    for index, line in enumerate(lines):
        lowered = line.strip().lower()
        if start is None and lowered.startswith("#main"):
            start = index + 1
        elif start is not None and lowered.startswith(("#extra", "!side")):
            end = index
            break
    if start is None:
        return
    if end is None:
        end = len(lines)
    block = [line for line in lines[start:end] if line.strip()]
    if len(block) < 2:
        return
    shuffled = list(block)
    random.Random(seed).shuffle(shuffled)
    deck.write_text("\n".join(lines[:start] + shuffled + lines[end:]) + "\n", encoding="utf-8")


def make_fighters(pairs: Sequence[Tuple[str, Path, str]]) -> List[Fighter]:
    """``[(名字, .ydk 路径, 脚本风格)]`` → :class:`Fighter` 列表。"""

    return [Fighter(name=name, deck_file=Path(path), style=style) for name, path, style in pairs]
