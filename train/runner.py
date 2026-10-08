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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time

from .analysis import (
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
    KIND_COMBO,
    KIND_ITERATE,
    KIND_REPLAY,
    KIND_SCRIPT,
    KIND_TITLES,
    KIND_WRITE_SCRIPT,
    KINDS_USING_ENGINE,
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

#: 需要模型写结论的种类（combo / 写脚本 / 自动迭代的模型调用本身就是任务，不在此列）。
KINDS_WITH_CONCLUSION = frozenset({KIND_ARENA, KIND_SCRIPT, KIND_REPLAY})


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

    # ---- 状态 ----

    @property
    def busy(self) -> bool:
        """是否有任务在跑。"""

        return self._task is not None and not self._task.done()

    def active(self) -> Optional[Dict[str, Any]]:
        """当前任务的一行摘要（面板用；没有就是 None）。"""

        if not self._run_id:
            return None
        run = self.store.get(self._run_id)
        if run is None:
            return None
        payload = run.to_dict()
        payload["live"] = self.busy
        if run.log_path:
            payload["tail"] = tail_lines(run.log_path, PANEL_TAIL_LINES)
        return payload

    def describe_kinds(self) -> List[Dict[str, Any]]:
        """五种任务在当前环境里能不能跑（面板据此把按钮置灰/给出原因）。

        「能不能跑」不猜：缺哪个文件就说缺哪个文件——比渲染一个能点但必定失败的按钮好。
        """

        card_db = self._card_db()
        cards_ready = bool(card_db is not None and getattr(card_db, "available", False))
        rooms = self._active_rooms()
        engine_note = ""
        if rooms:
            engine_note = f"现在有 {rooms} 个房间在打，擂台会抢进程与端口，等打完再开"
        source_dir, windbot_dir = self._windbot_dirs()
        script_ready = bool(source_dir) and bool(windbot_dir)
        script_note = "" if script_ready else "要配 paths.windbot_src_dir（写脚本要编译进 WindBot）"
        return [
            {
                "kind": KIND_COMBO,
                "title": KIND_TITLES[KIND_COMBO],
                "ready": cards_ready,
                "note": "" if cards_ready else "卡库不可用：推演要读卡文，请先配好 cards.cdb",
                "needs_engine": False,
                "fields": ["deck", "extra_prompt"],
            },
            {
                "kind": KIND_WRITE_SCRIPT,
                "title": KIND_TITLES[KIND_WRITE_SCRIPT],
                "ready": cards_ready and script_ready,
                "note": script_note or "读卡文+combo 写 C# 并编译（会改动 WindBot 源码树）",
                "needs_engine": False,
                "fields": ["deck", "extra_prompt", "rounds"],
            },
            {
                "kind": KIND_ITERATE,
                "title": KIND_TITLES[KIND_ITERATE],
                "ready": cards_ready and script_ready and not rooms,
                "note": engine_note or "combo → 写脚本 → 擂台 → 回喂下一轮（**会真打牌**，很慢）",
                "needs_engine": True,
                "fields": ["deck", "extra_prompt", "rounds", "duels"],
            },
            {
                "kind": KIND_ARENA,
                "title": KIND_TITLES[KIND_ARENA],
                "ready": not rooms,
                "note": engine_note,
                "needs_engine": True,
                "fields": ["deck", "style_a", "style_b", "duels"],
            },
            {
                "kind": KIND_SCRIPT,
                "title": KIND_TITLES[KIND_SCRIPT],
                "ready": True,
                "note": "静态体检，不打牌",
                "needs_engine": False,
                "fields": ["style", "deck_ids", "group"],
            },
            {
                "kind": KIND_REPLAY,
                "title": KIND_TITLES[KIND_REPLAY],
                "ready": True,
                "note": "只读录像文件，不打牌",
                "needs_engine": False,
                "fields": ["latest", "deck"],
            },
        ]

    # ---- 起任务 ----

    async def start(self, kind: str, params: Dict[str, Any]) -> TrainingRun:
        """起一个训练任务（立刻返回记录，活儿在后台跑）。

        Raises:
            TrainingError: 种类不认识、已有任务在跑、房间占用着对局资源、参数或文件不对。
        """

        if kind not in KIND_TITLES:
            raise TrainingError(f"不认识的训练种类：{kind}")
        if self.busy:
            active = self.store.get(self._run_id)
            label = f"{active.kind_title}（{self._run_id}）" if active else self._run_id
            raise TrainingError(f"已经有一个任务在跑：{label}；等它跑完或先停掉它")
        if kind in KINDS_USING_ENGINE:
            rooms = self._active_rooms()
            if rooms:
                raise TrainingError(
                    f"现在有 {rooms} 个房间在打：擂台会再起一套 ygopro + WindBot，"
                    "抢进程与端口会把真人那局打坏。等这局结束再来"
                )

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

        不需要子进程的种类返回空命令行（combo 推演只问模型）。
        """

        if kind == KIND_COMBO:
            deck_name, ydk_path, deck_id = self._resolve_deck(params)
            resolved = {"deck_id": deck_id, "deck_name": deck_name, "deck_file": str(ydk_path)}
            if str(params.get("extra_prompt") or "").strip():
                resolved["extra_prompt"] = str(params["extra_prompt"]).strip()[:2000]
            return [], f"{KIND_TITLES[kind]}：{deck_name}", resolved

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
            deck_name, ydk_path, deck_id = self._resolve_deck(params)
            self._require_windbot_tree()
            duels = int(params.get("duels") or 20)
            duels = max(2, min(duels, self.max_duels))
            duels -= duels % 2
            rounds = max(1, min(int(params.get("rounds") or 2), 5))
            resolved = {
                "deck_id": deck_id,
                "deck_name": deck_name,
                "deck_file": str(ydk_path),
                "rounds": rounds,
                "duels": duels,
            }
            if str(params.get("extra_prompt") or "").strip():
                resolved["extra_prompt"] = str(params["extra_prompt"]).strip()[:2000]
            return [], f"{KIND_TITLES[kind]}：{deck_name}（{rounds} 轮 × {duels} 局）", resolved

        if kind == KIND_ARENA:
            deck_name, ydk_path, deck_id = self._resolve_deck(params)
            style_a = str(params.get("style_a") or "").strip()
            style_b = str(params.get("style_b") or "").strip()
            if not style_a or not style_b:
                raise TrainingError("擂台要给两个出牌脚本名（style_a / style_b）")
            if style_a == style_b:
                raise TrainingError("两边的脚本名一样，打出来的是同一份脚本，比不出东西")
            duels = int(params.get("duels") or 60)
            # 座位逐局交替：奇数局会让两边坐的次数不一样，取偶数局保证两边坐上/下家一样多
            duels = max(2, min(duels, self.max_duels))
            duels -= duels % 2
            resolved = {
                "deck_id": deck_id,
                "deck_name": deck_name,
                "deck_file": str(ydk_path),
                "style_a": style_a,
                "style_b": style_b,
                "duels": duels,
            }
            argv = [
                str(self.tools_dir / "style_ab.py"),
                "--deck-file",
                str(ydk_path),
                "--style-a",
                style_a,
                "--style-b",
                style_b,
                "--duels",
                str(duels),
            ]
            return argv, f"{KIND_TITLES[kind]}：{style_a} vs {style_b}（{duels} 局）", resolved

        if kind == KIND_SCRIPT:
            style = str(params.get("style") or "").strip()
            group = str(params.get("group") or "").strip()
            deck_ids = _as_int_list(params.get("deck_ids"))
            argv = [str(self.tools_dir / "check_card_coverage.py")]
            resolved: Dict[str, Any] = {}
            if style:
                argv += ["--style", style]
                resolved["style"] = style
            elif deck_ids:
                for deck_id in deck_ids:
                    argv += ["--deck-id", str(deck_id)]
                resolved["deck_ids"] = deck_ids
            elif group:
                argv += ["--group", group]
                resolved["group"] = group
            else:
                raise TrainingError(
                    "脚本预校验要么给出牌脚本名（style），要么给卡组编号（deck_ids），要么给群号（group）"
                )
            argv += ["--data-dir", str(self._data_dir_of_deck_db()), "--cards-cdb", str(self._cards_cdb())]
            label = style or group or "、".join(str(item) for item in deck_ids)
            return argv, f"{KIND_TITLES[kind]}：{label}", resolved

        if kind == KIND_REPLAY:
            latest = max(1, min(int(params.get("latest") or 5), 50))
            argv = [
                str(self.tools_dir / "analyze_replay.py"),
                "--latest",
                str(latest),
                "--export-dir",
                str(self.export_dir),
            ]
            resolved = {"latest": latest}
            if params.get("deck_id"):
                compare = int(params["deck_id"])
                argv += ["--compare", str(compare)]
                resolved["compare"] = compare
            cards_cdb = self._cards_cdb()
            if cards_cdb:
                argv += ["--cards-cdb", cards_cdb]
            return argv, f"{KIND_TITLES[kind]}：最近 {latest} 份", resolved

        raise TrainingError(f"不认识的训练种类：{kind}")

    # ---- 任务的执行 ----

    async def _run(self, run: TrainingRun, argv: List[str], params: Dict[str, Any]) -> None:
        """跑一个任务并把结果写进记录库。"""

        try:
            if run.kind == KIND_COMBO:
                await self._run_combo(run, params)
            elif run.kind == KIND_WRITE_SCRIPT:
                await self._run_write_script(run, params)
            elif run.kind == KIND_ITERATE:
                await self._run_iterate(run, params)
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

    async def _run_combo(self, run: TrainingRun, params: Dict[str, Any]) -> None:
        """combo 推演：读卡表 → 问模型 → 落盘。"""

        ydk_path = Path(str(params["deck_file"]))
        deck = _read_ydk(ydk_path)
        if not deck.main:
            raise TrainingError(f"从 {ydk_path} 里没读到主卡组，请检查这份 .ydk")
        card_db = self._card_db()
        card_ids = deck_card_ids(deck.main, deck.extra, deck.side)
        digest, known_names, warnings = build_deck_digest(
            str(params["deck_name"]),
            card_ids,
            card_db,
            group_counts={"主卡组": len(deck.main), "额外卡组": len(deck.extra)},
        )
        result = await derive_combo(
            self._generate,
            deck_name=str(params["deck_name"]),
            digest=digest,
            known_names=known_names,
            model=str(self._model_name()),
            extra_prompt=str(params.get("extra_prompt") or ""),
            logger=self.logger,
        )
        warnings.extend(result.warnings)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stem = _safe_stem(f"{params.get('deck_id') or 'deck'}-{params['deck_name']}")
        guide_path = self.combo_dir / f"{stamp}-{stem}.txt"
        guide_path.write_text(result.text(), encoding="utf-8")
        (self.combo_dir / f"{stamp}-{stem}.json").write_text(
            _dump_json(result.guide), encoding="utf-8"
        )
        self.store.finish(
            run.run_id,
            STATUS_DONE,
            summary={
                "deck_name": params["deck_name"],
                "cards": len(card_ids),
                "lines": len(result.guide.get("lines") or []),
                "uncertain": len(result.guide.get("uncertain") or []),
                "warnings": warnings,
                "guide_path": str(guide_path),
                "model": result.model or "（宿主给插件配的那只）",
            },
        )
        if self.logger is not None:
            self.logger.info("combo 推演已存档：%s", guide_path)

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
        self, params: Dict[str, Any], *, feedback: str = "", combo_guide: str = ""
    ) -> Any:
        """跑一次「写脚本」：读卡表 + 卡文 → 模型写 C# → dotnet 编译（失败自动重写几轮）。"""

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
        )
        return await generator.generate(request)

    async def _as_prompt_generate(self, prompt: str) -> str:
        """把 `(prompt, model, max_tokens)` 形状的模型回调适配成生成器要的单参数形状。"""

        return await self._generate(prompt, str(self._model_name()), 4096)

    async def _run_write_script(self, run: TrainingRun, params: Dict[str, Any]) -> None:
        """写脚本：模型写 C# → 编译 → 把"这副牌该用哪个脚本"写回卡组池。"""

        guide = self._load_latest_combo(int(params.get("deck_id") or 0), str(params["deck_name"]))
        script = await self._generate_script(params, combo_guide=guide)
        if self._record_script is not None:
            # 不写回池子的话，刚写好的脚本不会被对局用上——那就白写了
            self._record_script(int(params.get("deck_id") or 0), script.style_name)
        exe = Path(script.file_path).parents[2] / "bin" / "Release" / "WindBot.exe"
        summary = {
            "deck_name": params["deck_name"],
            "style_name": script.style_name,
            "file_path": str(script.file_path),
            "attempts": script.attempts,
            "exe": str(exe),
            "exe_mtime": exe.stat().st_mtime if exe.is_file() else 0,
            "combo_used": bool(guide),
        }
        self.store.finish(run.run_id, STATUS_DONE, summary=summary)
        if self.logger is not None:
            self.logger.info(
                "已为「%s」写好出牌脚本 %s（第 %s 轮编译通过）：%s",
                params["deck_name"],
                script.style_name,
                script.attempts,
                script.file_path,
            )

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
        那是循环结束后单独跑擂台的事（面板上「擂台 A/B」就是干这个的）。
        """

        deck_id = int(params.get("deck_id") or 0)
        rounds = int(params.get("rounds") or 2)
        duels = int(params.get("duels") or 20)
        baseline = self._baseline_style(deck_id, params)
        log_path = Path(run.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        history: List[Dict[str, Any]] = []
        feedback = ""

        def note(text: str) -> None:
            """往训练日志里追加一行（面板看进度就是读这个文件）。"""

            stamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {text}\n")
            if self.logger is not None:
                self.logger.info("[自动迭代 %s] %s", run.run_id, text)

        note(f"开始自动迭代：{params['deck_name']}，{rounds} 轮 × {duels} 局/轮，基线脚本 {baseline}")
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

            script = await self._generate_script(params, feedback=feedback, combo_guide=guide_text)
            round_info["style_name"] = script.style_name
            round_info["script_file"] = str(script.file_path)
            round_info["script_attempts"] = script.attempts
            note(f"第 {index} 轮：脚本 {script.style_name} 编译通过（第 {script.attempts} 轮），开擂台…")

            argv = [
                str(self.tools_dir / "style_ab.py"),
                "--deck-file",
                str(params["deck_file"]),
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
            note(f"第 {index} 轮：擂台结束（退出码 {exit_code}），让模型写结论…")
            conclusion = await self._conclude(
                kind=KIND_ARENA,
                params={**params, "style_a": script.style_name, "style_b": baseline},
                output_tail=output_tail,
            )
            if conclusion is None:
                # 结论写不出来不算迭代失败：擂台日志本身就在，下一轮就没有修改方向而已
                round_info["conclusion_error"] = self._last_conclusion_error
                feedback = ""
                note(f"第 {index} 轮：结论没生成（{self._last_conclusion_error}），下一轮不再带修改要求")
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


def _as_int_list(raw: Any) -> List[int]:
    """把 ``"1,2,3"`` / ``[1,2,3]`` 之类的输入读成整数列表（读不出来的项直接跳过并报错）。"""

    if raw in (None, ""):
        return []
    items: List[Any]
    if isinstance(raw, str):
        items = [part for part in raw.replace("，", ",").split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [raw]
    values: List[int] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not text.lstrip("-").isdigit():
            raise TrainingError(f"卡组编号只能是数字，读到的是 {text!r}")
        values.append(int(text))
    return values


def _safe_stem(text: str) -> str:
    """把卡组名转成文件名安全的一段（中文保留，只把 Windows 不认的字符换掉）。"""

    cleaned = "".join("_" if char in '\\/:*?"<>|' else char for char in str(text))
    cleaned = cleaned.strip().strip(".")
    return cleaned[:60] or "deck"


def _dump_json(payload: Any) -> str:
    """按 UTF-8 中文可读的方式导出 JSON。"""

    return json.dumps(payload, ensure_ascii=False, indent=2)
