"""训练任务的执行器。

**同一时刻只跑一个任务**，理由是硬的：擂台与体检都会真的起 ygopro + WindBot，
两个任务并行就是两套内核抢端口、抢 CPU，还会让「谁的成绩是谁的」变得说不清；
而 `duel/` 侧的统计口径本来就依赖"一台机器一次只有一局"。要并行请另找机器。

**任务怎么跑**：每个种类翻译成一条命令行（复用插件 `tools/` 下已经验证过的脚本），
用 `python -u` 起子进程，stdout/stderr 直接写进 `<工作目录>/logs/<时刻>-<种类>.log`——
面板看进度就是读这个文件的尾巴，跟人在命令行里跑是同一份输出。

**停任务必须杀整棵树**：训练脚本自己会起 `ygopro.exe` 与两个 `WindBot.exe`，
只杀脚本进程会留下一堆孤儿内核占着端口（下一轮任务就随机失败）。Windows 上必须
`taskkill /F /T`（`/T` 就是"连子孙一起"），POSIX 上没有对应语义，退回杀顶层进程并如实记账。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import time

from .analysis import (
    SUMMARY_MAX_TOKENS,
    AnalysisError,
    build_deck_digest,
    deck_card_ids,
    derive_combo,
    summarise_run,
    tail_lines,
)
from .scriptgen import (
    DeckScriptGenerator,
    DeckScriptRequest,
    ScriptGenerationError,
    collect_card_info,
)
from .store import (
    KIND_ARENA,
    KIND_ITERATE,
    KIND_REVIEW,
    KIND_TITLES,
    KIND_WRITE_SCRIPT,
    KINDS_NEEDING_QUIET,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    TrainingRun,
    TrainingStore,
)
from ..duel.deckcode import DeckCodeError, parse_deck_code
from ..duel.windbot_decks import GENERIC_STYLE_NAME

#: 子进程写日志时用的创建标志（Windows 下别弹控制台窗口）。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: 正常停止（异步路径）杀进程树的等待上限（秒）。这条路走的是"用户点了停止"，多等一会儿没关系。
_TASKKILL_TIMEOUT = 5.0

#: **卸载路径**杀进程树的等待上限（秒）。宿主给插件卸载的总预算是 5 秒，而卸载还要
#: 停房间（`kill_now`，同步、瞬时）、停面板（关 TCP 监听 + join 线程，约 0.6 秒），
#: 所以这里最多只能给 2 秒——给 5 秒的话"训练在跑时卸载"必然超预算，
#: 宿主会记 `plugin.shutdown 超时` 并把整个插件重启（对局里的连接跟着断）。
_UNLOAD_KILL_TIMEOUT = 2.0

#: 面板里展示给用户看的输出尾巴有多少行。
PANEL_TAIL_LINES = 80

#: 需要模型写结论的种类（写脚本 / 自动迭代的模型调用本身就是任务，不在此列）。
KINDS_WITH_CONCLUSION = frozenset({KIND_ARENA})

#: 写脚本时**每次模型调用**的输出上限（token）。可以在配置里改（`llm.training_script_max_tokens`）：
#: 脚本是分批写的（每批 `scriptgen.HANDLERS_PER_CALL` 张卡），所以这个数只决定"一批能写多细"。
#: 给太大没用——宿主对插件的单次调用有 30 秒硬超时，写不完一样是白写。
DEFAULT_SCRIPT_MAX_TOKENS = 4096

#: 自动迭代的"下一轮改什么"结论给多少额度。比普通结论长：它要列出能落到代码上的修改清单。
_ITERATE_CONCLUSION_TOKENS = 2000

#: 迭代结论里放多少字符的脚本源码（整份可能上千行，够模型对上处理函数就行）。
_ITERATE_CODE_CHARS = 12000


class TrainingError(RuntimeError):
    """任务起不来（参数不对、环境不对、已有任务在跑、房间里有人在打）。"""


class TrainingRunner:
    """训练任务的起停与记账。"""

    def __init__(
        self,
        *,
        plugin_root: Path,
        workspace: Path,
        deck_db_path: Path,
        store: TrainingStore,
        generate: Callable[[str, str, int], Awaitable[str]],
        card_db: Callable[[], Any],
        active_rooms: Callable[[], int],
        logger: Optional[logging.Logger] = None,
        max_duels: int = 60,
        windbot_dirs: Optional[Callable[[], Tuple[Optional[Path], Optional[Path]]]] = None,
        record_script: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        """
        Args:
            plugin_root: 插件目录（`tools/*.py` 在这里）。
            workspace: 训练工作目录（日志、推演结果、卡表导出都写这儿）。
            deck_db_path: `deck_pool.db`（读卡组用；只读打开）。
            store: 任务记录库。
            generate: 发模型请求的协程，签名 ``generate(prompt, model, max_tokens) -> str``，
                超时由它自己执行。
            card_db: 返回**当前**的 CardDatabase（配置热更新会换掉实例，所以要用回调取）。
            active_rooms: 返回当前进行中的房间数（擂台要据此拒绝启动）。
            max_duels: 一次擂台最多多少局。
            windbot_dirs: 返回 ``(WindBot 源码树, WindBot 运行目录)``，写脚本时要用；
                没配源码树就返回 None（那时"写脚本"会明确报错，不做假成功）。
            record_script: 把"这副牌该用哪个出牌脚本"写回卡组池（`set_generated_script`），
                否则刚写好的脚本不会被对局用上——那就白写了。
        """

        self.plugin_root = Path(plugin_root)
        self.tools_dir = self.plugin_root / "tools"
        self.workspace = Path(workspace)
        self.deck_db_path = Path(deck_db_path)
        self.store = store
        self._generate = generate
        self._card_db = card_db
        self._active_rooms = active_rooms
        self.logger = logger
        self.max_duels = max(2, int(max_duels))
        self._windbot_dirs = windbot_dirs or (lambda: (None, None))
        self._record_script = record_script

        self.log_dir = self.workspace / "logs"
        self.combo_dir = self.workspace / "combos"
        self.export_dir = self.workspace / "decks"
        for directory in (self.workspace, self.log_dir, self.combo_dir, self.export_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._task: Optional["asyncio.Task[None]"] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._run_id = ""
        self._stopping = False
        #: 训练用哪只模型（空串＝宿主给插件配的那只）；由插件在起 runner 与配置热更新时推过来。
        self._training_model = ""
        #: 写脚本时每次模型调用的输出上限；同样由插件按配置推过来（见 `set_script_max_tokens`）。
        self._script_max_tokens = DEFAULT_SCRIPT_MAX_TOKENS

    # ---- 状态 ----

    @property
    def busy(self) -> bool:
        """是否有任务在跑。"""

        return self._task is not None and not self._task.done()

    def active(self) -> Optional[Dict[str, Any]]:
        """**正在跑**的那个任务的一行摘要（面板用；没有就是 None）。

        ⚠ 这里必须同时判 `busy`：面板拿 `active` 决定"开始按钮能不能点"与要不要继续轮询，
        而 `_run_id` 在正常跑完之后**不会**被清掉（只有 stop/stop_now 清）。只看 `_run_id`
        的话，跑完的任务会一直占着这个位置——面板上那条"正在跑"的气泡与「有任务在跑」的
        禁用状态永远挂着，用户下一件事就点不动了。跑完的记录照旧在历史列表里。
        """

        if not self._run_id or not self.busy:
            return None
        run = self.store.get(self._run_id)
        if run is None:
            return None
        payload = run.to_dict()
        payload["live"] = True
        if run.log_path:
            payload["tail"] = tail_lines(run.log_path, PANEL_TAIL_LINES)
        return payload

    #: 面板上给用户挑的任务（2026-10-09 用户口径：只留这四项）。
    #: 其余种类（combo / script / replay）降级成**内部步骤**——由这几项自己调用，
    #: 不再让用户先想"我该跑哪个"。
    PANEL_KINDS = (KIND_ARENA, KIND_WRITE_SCRIPT, KIND_ITERATE, KIND_REVIEW)

    def describe_kinds(self) -> List[Dict[str, Any]]:
        """五种任务在当前环境里能不能跑（面板据此把按钮置灰/给出原因）。

        「能不能跑」不猜：缺哪个文件就说缺哪个文件——比渲染一个能点但必定失败的按钮好。
        """

        card_db = self._card_db()
        cards_ready = bool(card_db is not None and getattr(card_db, "available", False))
        rooms = self._active_rooms()
        engine_note = ""
        quiet_note = ""
        if rooms:
            engine_note = f"现在有 {rooms} 个房间在打，打牌类的任务会抢进程与端口，等打完再开"
            quiet_note = f"现在有 {rooms} 个房间在打，它的 WindBot 占着要重编的那个 exe，等打完再写"
        source_dir, windbot_dir = self._windbot_dirs()
        script_ready = bool(source_dir) and bool(windbot_dir)
        script_note = "" if script_ready else "要配 paths.windbot_src_dir（写脚本要编译进 WindBot）"
        no_decks = len(self._deck_choices()) < 2
        return [
            {
                "kind": KIND_ARENA,
                "title": KIND_TITLES[KIND_ARENA],
                "ready": not rooms,
                "note": engine_note or "两副牌各自用自己的脚本对打，逐局交替座位",
                "needs_engine": True,
                "fields": ["opponent_deck", "duels"],
            },
            {
                "kind": KIND_WRITE_SCRIPT,
                "title": KIND_TITLES[KIND_WRITE_SCRIPT],
                "ready": cards_ready and script_ready and not rooms,
                "note": quiet_note or script_note or "读卡文写真脚本并编译，会自动先推一遍 combo",
                "needs_engine": False,
                "fields": ["rounds"],
            },
            {
                "kind": KIND_ITERATE,
                "title": KIND_TITLES[KIND_ITERATE],
                "ready": cards_ready and script_ready and not rooms and not no_decks,
                "note": engine_note or script_note or "推演 → 写脚本 → 跟另一副牌打 → 按结果再改一轮",
                "needs_engine": True,
                "fields": ["opponent_deck", "rounds", "duels"],
            },
            {
                "kind": KIND_REVIEW,
                "title": KIND_TITLES[KIND_REVIEW],
                "ready": True,
                "note": "读这副牌最近打过的对局记录，指出具体该改哪里（不动文件）",
                "needs_engine": False,
                "fields": ["latest"],
            },
        ]

    def _deck_choices(self) -> List[Dict[str, Any]]:
        """面板上"选卡组/选对手"要的那份清单（编号 + 名字 + 它自己的脚本）。"""

        if not self.deck_db_path.exists():
            return []
        import sqlite3 as _sqlite3

        connection = _sqlite3.connect(f"file:{self.deck_db_path}?mode=ro", uri=True)
        try:
            connection.row_factory = _sqlite3.Row
            rows = connection.execute(
                "SELECT deck_id, group_id, display_name, generated_script, picked_style,"
                " brain_scope, in_random FROM decks ORDER BY deck_id"
            ).fetchall()
        except _sqlite3.Error:
            return []
        finally:
            connection.close()
        result: List[Dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "deck_id": int(row["deck_id"]),
                    "group_id": str(row["group_id"] or ""),
                    "name": str(row["display_name"] or ""),
                    "script": str(row["generated_script"] or row["picked_style"] or ""),
                    "brain_scope": str(row["brain_scope"] or ""),
                    "in_random": bool(row["in_random"]),
                    "is_builtin": str(row["group_id"] or "") == "__builtin__",
                }
            )
        return result

    def _resolve_matchup(self, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """解析"哪两副牌对打"：A 是被迭代/主看的那副，B 是对手。

        两副牌各自带**自己那份脚本**（一个卡组只有一个脚本，这是用户 2026-10-09 的口径）：
        所以面板上选的是"另一副卡组"，不是"另一个脚本名"。
        """

        deck_a = self._deck_info(params.get("deck_id"), params.get("deck_file"))
        opponent_id = params.get("opponent_deck_id")
        if opponent_id in (None, ""):
            raise TrainingError("要选一副**对手卡组**（跟谁打）")
        deck_b = self._deck_info(opponent_id, None)
        if deck_a["deck_id"] and deck_a["deck_id"] == deck_b["deck_id"]:
            raise TrainingError("两边选的是同一副卡组：那样比的是运气，不是牌组强弱")
        for label, deck in (("主卡组", deck_a), ("对手卡组", deck_b)):
            if not deck["style"]:
                raise TrainingError(
                    f"{label}「{deck['name']}」还没有出牌脚本：先给它写一份（训练台的「编写脚本」），"
                    "或者把它指定给 WindBot 自带的风格名"
                )
        return deck_a, deck_b

    def _deck_info(self, deck_id: Any, deck_file: Any) -> Dict[str, Any]:
        """取一副牌的"能开打"信息：编号、名字、卡表路径、它自己的脚本名。"""

        if deck_file:
            path = Path(str(deck_file))
            if not path.is_file():
                raise TrainingError(f"找不到卡表文件：{path}")
            return {"deck_id": 0, "name": path.stem, "ydk_path": path, "style": GENERIC_STYLE_NAME}
        if deck_id in (None, ""):
            raise TrainingError("要选一副卡组")
        row = self._deck_row(int(deck_id))
        if row is None:
            raise TrainingError(f"卡组池里没有编号 {deck_id} 的卡组")
        keys = set(row.keys())
        script = ""
        for key in ("generated_script", "picked_style", "windbot_deck"):
            if key in keys and str(row[key] or "").strip():
                script = str(row[key]).strip()
                break
        path = Path(str(row["ydk_path"]))
        if not path.is_file():
            raise TrainingError(f"卡组 {deck_id} 的卡表文件不见了：{path}")
        return {
            "deck_id": int(deck_id),
            "name": str(row["display_name"] or path.stem),
            "ydk_path": path,
            "style": script,
        }

    # ---- 起任务 ----

    async def start(self, kind: str, params: Dict[str, Any]) -> TrainingRun:
        """起一个训练任务（立刻返回记录，活儿在后台跑）。

        Raises:
            TrainingError: 种类不认识、已有任务在跑、房间占用着对局资源、参数或文件不对。
        """

        def busy_note(target: str, rooms: int) -> str:
            """房间占用时拒绝启动的说明。两种占用原因不一样，得说清是哪一种。"""

            if target == KIND_WRITE_SCRIPT:
                return (
                    f"现在有 {rooms} 个房间在打：写脚本要 `dotnet build` 重编 WindBot.exe，"
                    "而那一局正占着这个文件（Windows 上覆盖不了），等这局打完再写"
                )
            return (
                f"现在有 {rooms} 个房间在打：{KIND_TITLES.get(target, target)}会再起一套 "
                "ygopro + WindBot，抢进程与端口会把真人那局打坏。等这局结束再来"
            )

        if kind not in self.PANEL_KINDS:
            raise TrainingError(
                f"这个训练种类不对外开放：{kind}"
                f"（面板上能起的是：{'、'.join(KIND_TITLES[k] for k in self.PANEL_KINDS)}）"
            )
        if self.busy:
            active = self.store.get(self._run_id)
            label = f"{active.kind_title}（{self._run_id}）" if active else self._run_id
            raise TrainingError(f"已经有一个任务在跑：{label}；等它跑完或先停掉它")
        if kind in KINDS_NEEDING_QUIET:
            rooms = self._active_rooms()
            if rooms:
                raise TrainingError(busy_note(kind, rooms))

        # 先做参数校验与卡表解析（同步、很快），这一步抛错不会留下"半个任务"的记录
        argv, title, resolved = self._plan(kind, params)
        log_path = self.log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.log"
        run = self.store.create(kind, title, resolved, log_path)
        self._run_id = run.run_id
        self._stopping = False
        self._task = asyncio.create_task(self._run(run, argv, resolved), name=f"mai-play-ygo-train-{kind}")
        if self.logger is not None:
            self.logger.info(
                "训练任务已起：%s（%s）｜%s", title, run.run_id, " ".join(argv) if argv else "只问模型"
            )
        return run

    async def stop(self) -> bool:
        """停掉当前任务（异步路径：正常收尾用）。返回是否真的停了什么。"""

        task, self._task = self._task, None
        if task is None or task.done():
            self._run_id = ""
            return False
        self._stopping = True
        await self._kill_tree_async()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001  停下这件事本身不该再抛给调用方
            pass
        if self.logger is not None:
            self.logger.info("训练任务已停止：%s", self._run_id)
        return True

    def stop_now(self) -> None:
        """卸载路径专用的同步停止：不 await、不写日志，只保证进程树死掉。

        宿主给插件卸载的预算是 5 秒（超时会记 `plugin.shutdown 超时` 并重启插件）。
        这里取消任务后**没有机会**再跑 `_run` 的收尾代码，所以记录会留在 `running` 状态，
        由下次加载时的 :meth:`train.store.TrainingStore.mark_interrupted` 统一改成失败——
        与其在卸载路径里抢时间写库，不如让"下一次启动"来收这个尾。

        杀进程树的等待上限用 :data:`_UNLOAD_KILL_TIMEOUT`（2 秒）而不是正常停止那条路的
        5 秒：卸载还要停房间与面板，三条加起来不能超过 5 秒的总额。
        """

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        self._stopping = True
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            _kill_tree_sync(process.pid, self.logger, timeout=_UNLOAD_KILL_TIMEOUT)
        self._run_id = ""

    # ---- 任务怎么翻译成命令 ----

    def _plan(self, kind: str, params: Dict[str, Any]) -> Tuple[List[str], str, Dict[str, Any]]:
        """把 ``(种类, 参数)`` 翻译成 ``(命令行, 标题, 落到记录里的参数)``。

        不需要子进程的种类返回空命令行（写脚本与迭代都是"只问模型 + 写文件"）。
        """

        if kind == KIND_WRITE_SCRIPT:
            deck_name, ydk_path, deck_id = self._resolve_deck(params)
            self._require_windbot_tree()
            resolved = {"deck_id": deck_id, "deck_name": deck_name, "deck_file": str(ydk_path)}
            if str(params.get("extra_prompt") or "").strip():
                resolved["extra_prompt"] = str(params["extra_prompt"]).strip()[:2000]
            attempts = max(1, min(int(params.get("rounds") or 3), 6))
            resolved["rounds"] = attempts
            return [], f"{KIND_TITLES[kind]}：{deck_name}", resolved

        if kind == KIND_ITERATE:
            deck = self._deck_info(params.get("deck_id"), params.get("deck_file"))
            opponent = self._deck_info(params.get("opponent_deck_id"), None)
            if deck["deck_id"] and deck["deck_id"] == opponent["deck_id"]:
                raise TrainingError("主卡组与对手卡组是同一副：迭代要有对手才能量出强弱")
            self._require_windbot_tree()
            duels = int(params.get("duels") or 20)
            duels = max(2, min(duels, self.max_duels))
            duels -= duels % 2
            rounds = max(1, min(int(params.get("rounds") or 2), 5))
            resolved = {
                "deck_id": deck["deck_id"],
                "deck_name": deck["name"],
                "deck_file": str(deck["ydk_path"]),
                "opponent_deck_id": opponent["deck_id"],
                "opponent_name": opponent["name"],
                "opponent_deck_file": str(opponent["ydk_path"]),
                "opponent_style": opponent["style"],
                "rounds": rounds,
                "duels": duels,
            }
            if str(params.get("extra_prompt") or "").strip():
                resolved["extra_prompt"] = str(params["extra_prompt"]).strip()[:2000]
            title = f"{KIND_TITLES[kind]}：{deck['name']}（{rounds} 轮 × {duels} 局 vs {opponent['name']}）"
            return [], title, resolved

        if kind == KIND_REVIEW:
            deck = self._deck_info(params.get("deck_id"), params.get("deck_file"))
            self._require_script_deck(deck)
            latest = max(1, min(int(params.get("latest") or 3), 20))
            resolved = {
                "deck_id": deck["deck_id"],
                "deck_name": deck["name"],
                "deck_file": str(deck["ydk_path"]),
                "style": deck["style"],
                "latest": latest,
            }
            if str(params.get("extra_prompt") or "").strip():
                resolved["extra_prompt"] = str(params["extra_prompt"]).strip()[:2000]
            return [], f"{KIND_TITLES[kind]}：{deck['name']}（最近 {latest} 局）", resolved

        if kind == KIND_ARENA:
            deck_a, deck_b = self._resolve_matchup(params)
            duels = int(params.get("duels") or 60)
            # 座位逐局交替：奇数局会让两边坐的次数不一样，取偶数局保证两边坐上/下家一样多
            duels = max(2, min(duels, self.max_duels))
            duels -= duels % 2
            resolved = {
                "deck_id": deck_a["deck_id"],
                "deck_name": deck_a["name"],
                "deck_file": str(deck_a["ydk_path"]),
                "opponent_deck_id": deck_b["deck_id"],
                "opponent_name": deck_b["name"],
                "opponent_deck_file": str(deck_b["ydk_path"]),
                "style_a": deck_a["style"],
                "style_b": deck_b["style"],
                "duels": duels,
            }
            argv = [
                str(self.tools_dir / "style_ab.py"),
                "--deck-file",
                str(deck_a["ydk_path"]),
                "--deck-file-b",
                str(deck_b["ydk_path"]),
                "--style-a",
                deck_a["style"],
                "--style-b",
                deck_b["style"],
                "--duels",
                str(duels),
            ]
            title = f"{KIND_TITLES[kind]}：{deck_a['name']} vs {deck_b['name']}（{duels} 局）"
            return argv, title, resolved

        raise TrainingError(f"不认识的训练种类：{kind}")

    # ---- 任务的执行 ----

    async def _run(self, run: TrainingRun, argv: List[str], params: Dict[str, Any]) -> None:
        """跑一个任务并把结果写进记录库。"""

        try:
            if run.kind == KIND_WRITE_SCRIPT:
                await self._run_write_script(run, params)
            elif run.kind == KIND_ITERATE:
                await self._run_iterate(run, params)
            elif run.kind == KIND_REVIEW:
                await self._run_review(run, params)
            elif argv:
                await self._run_process(run, argv, params)
            else:
                raise TrainingError(f"{run.kind} 没有可执行的步骤")
        except asyncio.CancelledError:
            # 被 stop()/stop_now() 取消：这不是失败，如实记下来
            self.store.finish(run.run_id, STATUS_CANCELLED, summary={"reason": "被手动停止"})
            self._cleanup_process()
            raise
        except (AnalysisError, TrainingError, ScriptGenerationError) as exc:
            self.store.finish(run.run_id, STATUS_FAILED, error=str(exc))
        except Exception as exc:  # noqa: BLE001  任何意外都要落成一条失败的记录，不能只进日志
            if self.logger is not None:
                self.logger.exception("训练任务异常：%s", run.run_id)
            self.store.finish(run.run_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")

    async def _run_process(self, run: TrainingRun, argv: List[str], params: Dict[str, Any]) -> None:
        """起子进程跑一个工具，然后把结论交给模型写。"""

        tool = Path(argv[0])
        if not tool.is_file():
            raise TrainingError(f"找不到工具脚本：{tool}")

        log_path = Path(run.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-u", *argv]
        exit_code, output_tail = await self._spawn(command, log_path)

        if self._stopping:
            self.store.finish(run.run_id, STATUS_CANCELLED, summary={"reason": "被手动停止"})
            return

        summary: Dict[str, Any] = {
            "exit_code": exit_code,
            "command": " ".join(command[1:]),
            "log_path": str(log_path),
        }
        if run.kind in KINDS_WITH_CONCLUSION:
            conclusion = await self._conclude(
                kind=run.kind, params=params, output_tail=output_tail
            )
            if conclusion is None:
                summary["conclusion_error"] = self._last_conclusion_error
            else:
                summary["conclusion"] = conclusion
        status = STATUS_DONE if exit_code == 0 else STATUS_FAILED
        error = "" if exit_code == 0 else f"工具以退出码 {exit_code} 结束（看日志尾巴定位）"
        self.store.finish(run.run_id, status, summary=summary, error=error, exit_code=exit_code)

    # ---------------------------------------------------------------- 写脚本 / 自动迭代

    @staticmethod
    def _require_script_deck(deck: Dict[str, Any]) -> None:
        """这副牌得有自己的脚本，否则"优化"无从谈起。"""

        if not deck.get("style"):
            raise TrainingError(
                f"「{deck['name']}」还没有出牌脚本：先用「编写脚本」写一份再来复盘"
            )

    def _require_windbot_tree(self) -> Tuple[Path, Path]:
        """取 WindBot 源码树与运行目录；没配就明确报错（不做假成功）。"""

        source_dir, windbot_dir = self._windbot_dirs()
        if not source_dir or not windbot_dir:
            raise TrainingError(
                "写脚本要配 paths.windbot_src_dir（WindBot 源码树）：脚本是编译进 WindBot 的，"
                "没有源码树就编译不了。也可以在配置页里填好再来"
            )
        return Path(source_dir), Path(windbot_dir)

    async def _generate_script(
        self, params: Dict[str, Any], *, feedback: str = "", combo_guide: str = "", previous_code: str = ""
    ) -> Any:
        """跑一次「写脚本」：读卡表 + 卡文 → 模型写 C# → 编译（失败自动重写几轮）。

        `previous_code` 是上一版的源码：迭代时把它传进来才是"改"而不是"从零重写"。
        不传的话，生成器自己会去找这副牌已有的脚本当基座（"再写一次"＝改进）。
        """

        source_dir, windbot_dir = self._require_windbot_tree()
        ydk_path = Path(str(params["deck_file"]))
        deck = _read_ydk(ydk_path)
        card_db = self._card_db()
        cards = collect_card_info(card_db, deck.main, deck.extra)
        if not cards:
            raise TrainingError("卡库里查不到这副牌的卡（卡文是写脚本的依据，不能没有）")
        style_name = DeckScriptGenerator.style_name_for(int(params.get("deck_id") or 0) or 0)
        generator = DeckScriptGenerator(
            self._as_prompt_generate,
            source_dir=source_dir,
            windbot_dir=windbot_dir,
            style_name=style_name,
            max_attempts=int(params.get("rounds") or 3),
            logger=self.logger,
        )
        request = DeckScriptRequest(
            deck_id=int(params.get("deck_id") or 0),
            deck_name=str(params["deck_name"]),
            cards=cards,
            combo_guide=combo_guide,
            extra_prompt=str(params.get("extra_prompt") or ""),
            feedback=feedback,
            previous_code=previous_code,
        )
        return await generator.generate(request)

    async def _as_prompt_generate(self, prompt: str) -> str:
        """把 `(prompt, model, max_tokens)` 形状的模型回调适配成生成器要的单参数形状。

        额度取配置里的 `llm.training_script_max_tokens`：脚本是分批写的，这个数只影响
        "一批能写多细"，不是整份脚本的长度上限。
        """

        return await self._generate(prompt, str(self._model_name()), int(self._script_max_tokens))

    async def _run_write_script(self, run: TrainingRun, params: Dict[str, Any]) -> None:
        """写脚本：**先推一遍 combo**，再让模型照它写 C# → 编译 → 把脚本名写回卡组池。

        推演放在这一步里面（而不是让用户先跑一次"推演 combo"）：用户口径是"一件事一句话"，
        而推演只是写脚本的中间材料——它自己会存档，想看得去 `train/combos/`，
        不必先在面板上跑一次。
        """

        guide = await self._derive_combo_for(params)
        script = await self._generate_script(params, combo_guide=guide)
        if self._record_script is not None:
            # 不写回池子的话，刚写好的脚本不会被对局用上——那就白写了
            self._record_script(int(params.get("deck_id") or 0), script.style_name)
        # 编译产物在**源码树根**下的 bin/Release/：脚本文件躺在 <根>/Game/AI/Decks/ 里，
        # 所以得往上数四级才是根（数少一级就永远拿不到 mtime，面板上"exe 什么时候编的"一直是 0）
        exe = Path(script.file_path).parents[3] / "bin" / "Release" / "WindBot.exe"
        summary = {
            "deck_name": params["deck_name"],
            "style_name": script.style_name,
            "file_path": str(script.file_path),
            "attempts": script.attempts,
            # 这一次写了多少：面板上要能一眼看出"是不是又只写了十来行"
            "lines": len(str(script.code).splitlines()),
            "handlers": DeckScriptGenerator.count_handlers(script.code),
            "warnings": list(script.warnings),
            "exe": str(exe),
            "exe_mtime": exe.stat().st_mtime if exe.is_file() else 0,
            "combo_used": bool(guide),
        }
        self.store.finish(run.run_id, STATUS_DONE, summary=summary)
        if self.logger is not None:
            self.logger.info(
                "已为「%s」写好出牌脚本 %s：%s 行、%s 个处理函数（第 %s 轮编译通过）：%s",
                params["deck_name"],
                script.style_name,
                summary["lines"],
                summary["handlers"],
                script.attempts,
                script.file_path,
            )

    async def _derive_combo_for(self, params: Dict[str, Any]) -> str:
        """给这副牌推一遍 combo，存档并返回正文（写脚本 / 迭代共用）。"""

        result = await derive_combo(
            self._generate,
            deck_name=str(params["deck_name"]),
            digest=self._deck_digest(params),
            known_names=self._known_names(params),
            model=str(self._model_name()),
            extra_prompt=str(params.get("extra_prompt") or ""),
            logger=self.logger,
        )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        deck_id = int(params.get("deck_id") or 0)
        path = self.combo_dir / f"{stamp}-{deck_id}-{_safe_stem(str(params['deck_name']))}.txt"
        path.write_text(result.text(), encoding="utf-8")
        if self.logger is not None:
            self.logger.info("combo 推演已存档：%s", path)
        return result.text()

    def _load_latest_combo(self, deck_id: int, deck_name: str) -> str:
        """取这副牌最近一份 combo 推演（没有就空串：脚本退化成"只按卡文写"）。"""

        if not self.combo_dir.is_dir():
            return ""
        candidates = sorted(self.combo_dir.glob(f"*-{deck_id}-*.txt"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            candidates = sorted(self.combo_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            return ""
        try:
            return candidates[-1].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    async def _run_iterate(self, run: TrainingRun, params: Dict[str, Any]) -> None:
        """自动迭代：combo → 写脚本 → 擂台实测 → 把结论回喂下一轮。

        每一轮做的事（都写进日志与记录，面板上能逐轮看）：

        1. 读卡表 + 卡文推 combo（带作者的额外要求）；
        2. 按 combo 写脚本并编译；
        3. 拿新脚本与**基线脚本**打一轮擂台（默认 20 局、逐局交替座位）；
        4. 让模型读擂台输出写结论——**下一轮就是拿这段结论当修改要求**。
           这就是"复盘找问题再优化"的自动化版本。

        局数刻意给得小（默认 20）：这个循环的价值在于"快速试错"，判定强弱要 ≥80 局/腿，
        那是循环结束后单独跑擂台的事（面板上「卡组互打」就是干这个的）。
        """

        deck_id = int(params.get("deck_id") or 0)
        rounds = int(params.get("rounds") or 2)
        duels = int(params.get("duels") or 20)
        opponent_file = str(params.get("opponent_deck_file") or "")
        opponent_name = str(params.get("opponent_name") or "对手")
        baseline = str(params.get("opponent_style") or "") or GENERIC_STYLE_NAME
        log_path = Path(run.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        history: List[Dict[str, Any]] = []
        feedback = ""
        last_code = ""
        """上一轮写出来的脚本源码：下一轮在它基础上改（迭代要越改越好，不能每轮从零重写）。"""

        def note(text: str) -> None:
            """往训练日志里追加一行（面板看进度就是读这个文件）。"""

            stamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {text}\n")
            if self.logger is not None:
                self.logger.info("[自动迭代 %s] %s", run.run_id, text)

        note(
            f"开始迭代：{params['deck_name']}，{rounds} 轮 × {duels} 局/轮，"
            f"对手「{opponent_name}」（脚本 {baseline}）"
        )
        for index in range(1, rounds + 1):
            if self._stopping:
                break
            round_info: Dict[str, Any] = {"round": index}
            note(f"第 {index} 轮：推 combo…")
            combo = await derive_combo(
                self._generate,
                deck_name=str(params["deck_name"]),
                digest=self._deck_digest(params),
                known_names=self._known_names(params),
                model=str(self._model_name()),
                extra_prompt=str(params.get("extra_prompt") or ""),
                logger=self.logger,
            )
            guide_text = combo.text()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            guide_path = self.combo_dir / f"{stamp}-{deck_id}-iter{index}.txt"
            guide_path.write_text(guide_text, encoding="utf-8")
            round_info["combo_lines"] = len(combo.guide.get("lines") or [])
            round_info["combo_warnings"] = combo.warnings
            round_info["guide_path"] = str(guide_path)
            note(f"第 {index} 轮：combo 完成（{round_info['combo_lines']} 条主线），开始写脚本…")

            script = await self._generate_script(
                params, feedback=feedback, combo_guide=guide_text, previous_code=last_code
            )
            last_code = str(script.code)
            round_info["style_name"] = script.style_name
            round_info["script_file"] = str(script.file_path)
            round_info["script_attempts"] = script.attempts
            round_info["script_lines"] = len(last_code.splitlines())
            round_info["script_handlers"] = DeckScriptGenerator.count_handlers(last_code)
            round_info["script_warnings"] = list(script.warnings)
            note(
                f"第 {index} 轮：脚本 {script.style_name} 编译通过"
                f"（{round_info['script_lines']} 行、{round_info['script_handlers']} 个处理函数，"
                f"第 {script.attempts} 轮），开擂台…"
            )

            argv = [
                str(self.tools_dir / "style_ab.py"),
                "--deck-file",
                str(params["deck_file"]),
                "--deck-file-b",
                opponent_file or str(params["deck_file"]),
                "--style-a",
                script.style_name,
                "--style-b",
                baseline,
                "--duels",
                str(duels),
            ]
            if script.style_name == baseline:
                # 基线就是它自己（第一次写脚本）：这一轮只验证"能不能打起来"，
                # 不该拿自己和自己对打去算胜率——如实说，别编一个 50% 出来
                note("这一轮没有可比基线（新脚本就是基线本身），只验证它能被加载")
                round_info["compared"] = False
                round_info["conclusion"] = "本轮没有可比基线：脚本已写入并编译通过，下一轮起才与旧脚本对打。"
                history.append(round_info)
                feedback = ""
                continue
            round_info["compared"] = True
            arena_log = self.log_dir / f"{log_path.stem}-round{index}-arena.log"
            exit_code, output_tail = await self._spawn(
                [sys.executable, "-u", *argv], arena_log
            )
            round_info["arena_exit_code"] = exit_code
            round_info["arena_log"] = str(arena_log)
            note(f"第 {index} 轮：擂台结束（退出码 {exit_code}），让模型读战况 + 脚本写下一版改什么…")
            conclusion, conclusion_error = await self._iteration_conclusion(
                params=params,
                script=script,
                baseline=baseline,
                log_path=arena_log,
                history=history,
            )
            if conclusion is None:
                # 结论写不出来不算迭代失败：擂台日志本身就在，下一轮就没有修改方向而已
                round_info["conclusion_error"] = conclusion_error
                feedback = ""
                note(f"第 {index} 轮：结论没生成（{conclusion_error}），下一轮不再带修改要求")
            else:
                round_info["conclusion"] = conclusion
                feedback = conclusion
                note(f"第 {index} 轮结论：{conclusion.replace(chr(10), ' ')[:160]}")
            history.append(round_info)

        if self._record_script is not None and history:
            last_style = str(history[-1].get("style_name") or "")
            if last_style:
                self._record_script(deck_id, last_style)
        self.store.finish(
            run.run_id,
            STATUS_DONE,
            summary={
                "deck_name": params["deck_name"],
                "baseline": baseline,
                "rounds": len(history),
                "history": history,
                "log_path": str(log_path),
            },
        )

    async def _run_review(self, run: TrainingRun, params: Dict[str, Any]) -> None:
        """复盘优化：读这副牌**最近打过的几局**，让模型指出具体该改哪里。

        素材是插件自己在每局结束时写下的记录（`kind=duel`）——那里面有记录器活着看到的
        回合数、双方动作数、召唤/特召/发动/盖放/攻击、伤害、用过的卡。**不用录像**：
        `.yrp` 只有玩家的应答、没有内核的提问，逐动作复盘做不出来（`tools/analyze_replay.py`
        的模块头写着这条限制）。

        产出是一份"改哪儿"的清单，**不动任何文件**：改脚本是「编写脚本」那一步的事，
        让复盘自己去改代码就等于把"看"和"改"混在一起，出了问题说不清是哪一步坏的。
        """

        deck_id = int(params.get("deck_id") or 0)
        latest = int(params.get("latest") or 3)
        records = self._recent_duels(deck_id, latest)
        if not records:
            raise TrainingError(
                f"还没有「{params['deck_name']}」的对局记录：先在群里跟它打几局（每局打完会自动记下来），再来复盘"
            )
        material = "\n\n".join(_duel_brief(index + 1, record) for index, record in enumerate(records))
        prompt = _REVIEW_PROMPT.format(
            deck=params["deck_name"],
            style=params.get("style") or "（未设置）",
            count=len(records),
            material=material[:12000],
            extra=str(params.get("extra_prompt") or "").strip()[:1500],
        )
        try:
            text = await self._generate(prompt, str(self._model_name()), SUMMARY_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001  模型那层失败就是这次复盘失败，如实报
            raise AnalysisError(f"调用模型失败：{exc}") from exc
        answer = str(text or "").strip()
        if not answer:
            raise AnalysisError("模型返回了空内容（额度被思考吃光，或那只模型不能用）")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.workspace / "reviews"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{stamp}-{deck_id}-{_safe_stem(params['deck_name'])}.txt"
        head = (
            f"# {params['deck_name']}（脚本 {params.get('style') or '未设置'}）复盘\n"
            f"# 依据：最近 {len(records)} 局房间对局记录\n\n"
        )
        path.write_text(head + answer + "\n", encoding="utf-8")
        self.store.finish(
            run.run_id,
            STATUS_DONE,
            summary={
                "deck_name": params["deck_name"],
                "style_name": params.get("style") or "",
                "duels": len(records),
                "conclusion": answer[:4000],
                "review_path": str(path),
            },
        )
        if self.logger is not None:
            self.logger.info("复盘优化已存档：%s", path)

    def _recent_duels(self, deck_id: int, latest: int) -> List[Any]:
        """这副牌最近的房间对局记录（新的在前）。"""

        try:
            runs = self.store.list_recent(limit=200, kind="duel")
        except Exception:  # noqa: BLE001  库读不动就当没有记录
            return []
        picked: List[Any] = []
        for record in runs:
            params = record.params or {}
            if str(params.get("deck_id") or "") != str(deck_id):
                continue
            picked.append(record)
            if len(picked) >= latest:
                break
        return picked

    def _baseline_style(self, deck_id: int, params: Dict[str, Any]) -> str:
        """这一轮迭代的对照脚本：卡组池里现在记着的那份（没有就用通用脚本）。"""

        row = self._deck_row(deck_id)
        if row is not None:
            for key in ("generated_script", "picked_style", "windbot_deck"):
                try:
                    value = str(row[key] or "").strip()
                except (IndexError, KeyError):
                    value = ""
                if value:
                    return value
        return GENERIC_STYLE_NAME

    def _deck_digest(self, params: Dict[str, Any]) -> str:
        """准备推演用的卡表摘要（与 combo 任务同一份口径）。"""

        deck = _read_ydk(Path(str(params["deck_file"])))
        digest, _names, _warnings = build_deck_digest(
            str(params["deck_name"]),
            deck_card_ids(deck.main, deck.extra, deck.side),
            self._card_db(),
            group_counts={"主卡组": len(deck.main), "额外卡组": len(deck.extra)},
        )
        return digest

    def _known_names(self, params: Dict[str, Any]) -> set:
        """卡表里的卡名集合（推演的卡名核对用）。"""

        deck = _read_ydk(Path(str(params["deck_file"])))
        _digest, names, _warnings = build_deck_digest(
            str(params["deck_name"]), deck_card_ids(deck.main, deck.extra, deck.side), self._card_db()
        )
        return names

    async def _spawn(self, command: List[str], log_path: Path) -> Tuple[int, str]:
        """起一个子进程，输出直接写日志文件，返回 ``(退出码, 输出尾巴)``。"""

        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        if self.logger is not None:
            self.logger.info("训练任务子进程启动：%s", " ".join(command))
        with open(log_path, "ab", buffering=0) as handle:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.plugin_root),
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                env=environment,
                creationflags=_NO_WINDOW,
            )
        process = self._process
        exit_code = await process.wait()
        self._process = None
        return exit_code, tail_lines(log_path, 200)

    _last_conclusion_error = ""

    async def _conclude(
        self, *, kind: str, params: Dict[str, Any], output_tail: str
    ) -> Optional[str]:
        """让模型读输出的尾巴写一段结论；写不出来返回 None（原因放在 `_last_conclusion_error`）。"""

        try:
            return await summarise_run(
                self._generate,
                kind_title=KIND_TITLES.get(kind, kind),
                params=params,
                output_tail=output_tail,
                model=str(self._model_name()),
                logger=self.logger,
            )
        except AnalysisError as exc:
            # 结论写不出来**不算任务失败**：报告本身（日志）已经落盘了，
            # 这里把原因记进摘要，面板上会显示"结论没生成"以及为什么
            self._last_conclusion_error = str(exc)
            return None


    async def _iteration_conclusion(
        self,
        *,
        params: Dict[str, Any],
        script: Any,
        baseline: str,
        log_path: Path,
        history: Sequence[Dict[str, Any]],
    ) -> Tuple[Optional[str], str]:
        """写"下一轮要改什么"：给模型看**这一版脚本的源码** + 这一轮的逐局战况。

        和单次擂台的结论不一样：那个只要说清"能不能信、下一步做什么"，迭代要的是**能落到
        代码上的修改清单**。所以提示词里必须带源码——只说"胜率 40%"没法指导改哪个处理函数，
        而"XX 的阈值太宽 / 该用 SelectCard 挑目标"才是真的改得动的东西。

        Returns:
            ``(结论, 失败原因)``；结论写不出来时前者为 None（原因给用户看）。
        """

        material = self._arena_evidence(log_path)
        earlier = "\n".join(
            f"- 第 {item['round']} 轮的修改要求：{str(item.get('conclusion') or '（没生成）')[:200]}"
            for item in history
        )
        prompt = _ITERATE_PROMPT.format(
            deck=params["deck_name"],
            opponent=params.get("opponent_name") or "对手",
            baseline=baseline,
            style=script.style_name,
            lines=len(str(script.code).splitlines()),
            handlers=DeckScriptGenerator.count_handlers(script.code),
            warnings="；".join(script.warnings) or "（无）",
            earlier=earlier or "（这是第一轮）",
            code=("```csharp\n" + str(script.code)[:_ITERATE_CODE_CHARS] + "\n```"),
            material=material,
        )
        try:
            raw = await self._generate(prompt, str(self._model_name()), _ITERATE_CONCLUSION_TOKENS)
        except Exception as exc:  # noqa: BLE001  模型那层的问题要如实说
            return None, f"调用模型失败：{exc}"
        text = str(raw or "").strip()
        if not text:
            return None, "模型返回了空内容（额度被思考吃光，或那只模型不能用）"
        if self.logger is not None:
            self.logger.info("迭代结论已生成：%s", text.replace("\n", " ")[:120])
        return text, ""

    @staticmethod
    def _arena_evidence(log_path: Path, max_duels: int = 40) -> str:
        """从擂台日志里取"逐局战况 + 汇总"两段。

        只给汇总（一个胜率）是不够的：指出问题要靠"第 3 局 6 回合就输了""这一局 20 秒就结束"
        这类具体现象。逐局行是 `style_ab.py` 自己打的，所以这里按行首的 `[` 挑。
        """

        try:
            rows = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "（读不到擂台日志）"
        duels = [row for row in rows if row.startswith("[")]
        split = next((index for index, row in enumerate(rows) if "汇总" in row), None)
        summary = rows[split:] if split is not None else []
        parts: List[str] = []
        if duels:
            parts.append(
                f"逐局（共 {len(duels)} 局，这里列前 {min(len(duels), max_duels)} 局）：\n"
                + "\n".join(duels[:max_duels])
            )
        if summary:
            parts.append("汇总：\n" + "\n".join(summary))
        if not parts:
            parts.append("（擂台日志里没有逐局记录；尾巴：\n" + "\n".join(rows[-12:]) + "）")
        return "\n\n".join(parts)

    def _cleanup_process(self) -> None:
        """进程对象在取消路径上的收尾（不 await，只保证不再被引用）。"""

        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            _kill_tree_sync(process.pid, self.logger)

    # ---- 辅助 ----

    def _model_name(self) -> str:
        """训练用哪只模型：空串在插件那层会被换成"宿主给插件配的那只"。"""

        model = getattr(self, "_training_model", "")
        return str(model or "")

    def set_training_model(self, model: str) -> None:
        """配置热更新后由插件把新模型名推给 runner。"""

        self._training_model = str(model or "")

    def set_script_max_tokens(self, value: Any) -> None:
        """配置热更新后由插件把"写脚本每次调用的输出上限"推给 runner。

        上限<=0（或没配）时用默认值：写脚本必须有个额度，给 0 会让模型每次都返回空内容。
        """

        tokens = int(value or 0)
        self._script_max_tokens = tokens if tokens > 0 else DEFAULT_SCRIPT_MAX_TOKENS

    def _cards_cdb(self) -> str:
        """卡库路径（拿不到就给空串，脚本自己会报"卡库不存在"）。"""

        card_db = self._card_db()
        path = getattr(card_db, "path", None)
        return str(path) if path else ""

    def _data_dir_of_deck_db(self) -> Path:
        """`tools/check_card_coverage.py` 要的是数据目录，不是库文件本身。"""

        return self.deck_db_path.parent

    def _resolve_deck(self, params: Dict[str, Any]) -> Tuple[str, Path, int]:
        """从参数里取出 ``(卡组名, .ydk 路径, 编号)``。

        两种给法：`deck_id`（从卡组池里挑）或 `deck_file`（直接给 .ydk 路径）。
        两条路都必须真找到文件——找不到就抛 :class:`TrainingError`，不做"猜一个类似名字"的兜底。
        """

        deck_file = str(params.get("deck_file") or "").strip()
        if deck_file:
            path = Path(deck_file)
            if not path.is_file():
                raise TrainingError(f"找不到卡表文件：{path}")
            return path.stem, path, 0
        deck_id = params.get("deck_id")
        if deck_id in (None, ""):
            raise TrainingError("要选一副卡组（deck_id）或给一个 .ydk 路径（deck_file）")
        row = self._deck_row(int(deck_id))
        if row is None:
            raise TrainingError(f"卡组池里没有编号 {deck_id} 的卡组")
        path = Path(str(row["ydk_path"]))
        if not path.is_file():
            raise TrainingError(f"卡组 {deck_id} 的卡表文件不见了：{path}")
        return str(row["display_name"] or path.stem), path, int(deck_id)

    def _deck_row(self, deck_id: int) -> Optional[sqlite3.Row]:
        """只读查一条卡组记录（不做写入，也不碰插件的卡组池实例）。"""

        if not self.deck_db_path.exists():
            return None
        connection = sqlite3.connect(f"file:{self.deck_db_path}?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM decks WHERE deck_id = ?", (deck_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()

    async def _kill_tree_async(self) -> None:
        """异步杀进程树（正常停止路径）。"""

        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        command = kill_tree_command(process.pid)
        if command:
            try:
                killer = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=_NO_WINDOW,
                )
                await asyncio.wait_for(killer.wait(), timeout=_TASKKILL_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                if self.logger is not None:
                    self.logger.warning("taskkill 失败（%s），改杀顶层进程", exc)
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


# ---------------------------------------------------------------------------
# 模块级小工具
# ---------------------------------------------------------------------------


#: 复盘优化的提示词。**要求它说"改哪一行/哪条规则"**，不接受"多练习"这种废话；
#: 也不许它编卡（素材里出现了哪些卡就是哪些卡）。
_ITERATE_PROMPT = """你在主持「游戏王出牌脚本」的自动迭代：读完这一版脚本与它刚打完的这一轮擂台，
写出**下一版脚本要改什么**。你的输出会原样交给写脚本的模型当修改要求，所以要对着代码说话。

这副牌：{deck}（对手：{opponent}，对照脚本：{baseline}）
这一版脚本：{style}（{lines} 行、{handlers} 个处理函数）
写脚本时已知的问题：{warnings}

之前的修改要求：
{earlier}

这一版脚本源码：
{code}

这一轮擂台的实际战况：
{material}

要求：
1. **对着上面的处理函数说话**：写清"哪个函数（`Card<卡号>Handler`）现在的判断是什么、该改成什么"
   ——例如"Card12345678Handler 只要对手场上有 1 只怪就发，改成对手有 2 只以上且自己场面落后时再发"。
   只说"要加强先手展开"这种话没有用，写脚本的模型照着它改不动。
2. **每条都要给依据**：引用战况里的具体现象（第几局、几回合、脚本没加载的告警、耗时异常）。
   战况里看不出来的事就不要写。
3. **样本不够就直说**：本机实测同一套牌重测会出现完全相反的胜率，所以局数少于 80 局时
   **不许**说"这版更强/更弱"，只能说"机制有没有跑坏"以及从逐局现象里看到的具体问题。
4. 如果战况里有"脚本没加载 / 执行器没注册 / 进程崩了 / 超时"这类痕迹，**第一条就写它**，
   并且这轮不要拿胜率说事。
5. 最后给一句"下一版重点改哪两三个函数"，不超过 40 字。
6. 中文小标题 + 短句，不要长篇大论；不要编造卡号与卡文。
"""

_REVIEW_PROMPT = """你在帮一副游戏王卡组做复盘。下面是它最近打过的对局记录（记录器在对局里
实时看到的：回合、双方动作数、召唤/特召/发动/盖放/攻击、伤害、双方用过的卡）。

这副牌：{deck}
它当前的出牌脚本：{style}
依据：最近 {count} 局房间对局记录
{extra}

要求：
1. **只根据上面的事实说话**，不要猜没写出来的东西；
2. 指出**具体**问题：例如"第 2 回合有 3 张手牌却只盖了 1 张""整局没发动过 XX""对手只剩 800 血
   时没有进战阶"；说不清就别写；
3. 每条问题要给出**可执行**的改法：改哪张卡的规则、加什么前提、优先做哪一步；
4. 最后给一句"下一版脚本的重点"，不超过 30 字；
5. 输出用中文小标题 + 短句，不要长篇大论。

对局记录：
{material}
"""


def _duel_brief(index: int, record: Any) -> str:
    """把一条对局记录压成几行事实（给复盘提示词用）。"""

    summary = dict(getattr(record, "summary", {}) or {})
    params = dict(getattr(record, "params", {}) or {})
    lines = [
        f"第 {index} 局（{getattr(record, 'started_at', '') or ''}）："
        f"{'我方赢' if summary.get('winner_is_self') else '我方输' if summary.get('winner_is_self') is False else '未判定'}"
        f"｜回合 {summary.get('turns', '?')}"
    ]
    for label, key in (("我方", "self"), ("对手", "opponent")):
        stats = summary.get(key)
        if isinstance(stats, dict):
            lines.append(
                f"  {label}：召唤 {stats.get('normal_summons', 0)}"
                f"｜特召 {stats.get('sp_summons', 0)}｜发动 {stats.get('effects', 0)}"
                f"｜盖放 {stats.get('sets', 0)}｜攻击 {stats.get('attacks', 0)}"
                f"｜造成伤害 {stats.get('damage_dealt', 0)}｜受到伤害 {stats.get('damage_taken', 0)}"
                f"｜剩 LP {stats.get('lp_final', '?')}"
            )
    usage = summary.get("card_usage") or {}
    if isinstance(usage, dict) and usage:
        top = sorted(usage.items(), key=lambda pair: -int(pair[1] or 0))[:8]
        lines.append("  用过的卡：" + "、".join(f"{card}×{count}" for card, count in top))
    if params.get("deck_name"):
        lines.append(f"  （这一局机器人用的是「{params['deck_name']}」）")
    return "\n".join(lines)


def kill_tree_command(pid: int) -> List[str]:
    """返回"连子孙一起杀"的命令；平台没有这个语义时返回空列表。

    Windows 上必须是 `taskkill /F /T`——`/T` 就是 kill tree。为什么不能只杀顶层：
    训练脚本自己会起 `ygopro.exe` 与两个 `WindBot.exe`，只杀脚本进程会留下一堆孤儿内核
    占着端口，下一轮任务就随机失败（而且现象是"有时候起不来"，极难排查）。
    POSIX 上 `kill` 没有对应语义（要 `killpg` 才等价，而那要求起进程时就用新进程组），
    所以这里**明确返回空列表**表示"做不到"，让调用方按"只杀顶层"处理并如实记账，
    而不是假装成功。
    """

    if sys.platform == "win32":
        return ["taskkill", "/F", "/T", "/PID", str(int(pid))]
    return []


def _kill_tree_sync(
    pid: int,
    logger: Optional[logging.Logger] = None,
    *,
    timeout: float = _TASKKILL_TIMEOUT,
) -> None:
    """同步杀进程树（卸载路径用；有超时上限，不会拖住卸载预算）。"""

    if not pid:
        return
    command = kill_tree_command(pid)
    if command:
        try:
            subprocess.run(
                command,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            return
        except (OSError, subprocess.TimeoutExpired) as exc:
            if logger is not None:
                logger.warning("taskkill 失败（%s），改杀顶层进程", exc)
    try:
        os.kill(pid, 9)
    except (OSError, ValueError):
        pass


def _read_ydk(path: Path) -> Any:
    """读一份 .ydk，返回 :class:`duel.deckcode.Deck`（保留张数、三个分区都在）。

    复用 `/加卡组` 那条链路的解析器，不在训练侧另写一个——两份解析器迟早会不一致，
    而"训练时看到的卡表"和"开房时用的卡表"必须是同一副。

    Raises:
        TrainingError: 文件读不动或不是能识别的卡组格式。
    """

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TrainingError(f"读不了卡表文件 {path}：{exc}") from exc
    try:
        return parse_deck_code(text)
    except DeckCodeError as exc:
        raise TrainingError(f"{path} 不是能识别的卡组格式：{exc}") from exc


def _safe_stem(text: str) -> str:
    """把卡组名转成文件名安全的一段（中文保留，只把 Windows 不认的字符换掉）。"""

    cleaned = "".join("_" if char in '\\/:*?"<>|' else char for char in str(text))
    cleaned = cleaned.strip().strip(".")
    return cleaned[:60] or "deck"
