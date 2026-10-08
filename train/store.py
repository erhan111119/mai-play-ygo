"""训练任务的记录库（`<工作目录>/training.db`）。

**每次调用现开一个连接**（和面板读卡组池、`duel/deckpool.py` 一样）：任务的写入发生在
插件的事件循环里，读取发生在面板的 HTTP 线程里，共用一条连接就得自己处理线程归属；
训练任务的条数很少（一天几条到几十条），省下的那点开销远不如"谁都能读"重要。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import json
import sqlite3
import time
import uuid

# ---- 任务种类 ---------------------------------------------------------------

KIND_COMBO = "combo"
"""combo 推演：读卡表+卡文推演展开流程（只问模型，不开对局）。
**只剩历史记录**：2026-10-09 起推演降级成「编写脚本 / 卡组迭代」的内部步骤，不再单独起任务。"""

KIND_ARENA = "arena"
"""两副卡组各带自己的出牌脚本对打（`tools/style_ab.py`，**会真打牌**）。"""

KIND_SCRIPT = "script"
"""卡表 × 执行器登记的静态体检（`tools/check_card_coverage.py`，不打牌）。**只剩历史记录**。"""

KIND_WRITE_SCRIPT = "write_script"
"""给一副卡组**写**出牌脚本：模型读卡文 + combo 写 C#，再 `dotnet build`（会写源码树）。"""

KIND_ITERATE = "iterate"
"""自动迭代：combo → 写脚本 → 编译 → 擂台实测 → 把结论回喂下一轮（**会真打牌**）。"""

KIND_REPLAY = "replay"
"""录像复盘（`tools/analyze_replay.py`：卡表、双方差异、导出 .ydk，不打牌）。**只剩历史记录**。"""

KIND_REVIEW = "review"
"""复盘优化：读这副牌最近打过的对局记录，让模型指出具体该改哪里（不自动改文件）。"""

KIND_DUEL = "duel"
"""房间对局记录（插件在每局打完时自动写一条）：复盘优化的素材，不是用户起的任务。"""

#: 记录里显示的名字。前四项就是面板上的四个选项，两边用词保持一致；
#: 另外三个标着"只剩历史记录"的种类不会再产生新记录，留着只为旧记录还能显示标题。
KIND_TITLES: Dict[str, str] = {
    KIND_COMBO: "combo 推演",
    KIND_WRITE_SCRIPT: "编写脚本",
    KIND_ITERATE: "卡组迭代",
    KIND_ARENA: "卡组互打",
    KIND_SCRIPT: "脚本预校验",
    KIND_REPLAY: "录像复盘",
    KIND_REVIEW: "复盘优化",
    KIND_DUEL: "房间对局",
}

#: 会真的开对局的种类——房间里有人时不许起（见 `train.runner`）。
KINDS_USING_ENGINE = frozenset({KIND_ARENA, KIND_ITERATE})

#: **要独占 WindBot 可执行文件/进程**的种类：除了打牌的，还有"写脚本"——它要
#: `dotnet build` 重编 `bin/Release/WindBot.exe`，而正在打的那局 WindBot 占着这个文件，
#: Windows 上根本覆盖不了（实测编译会以"文件正被另一个进程使用"失败）。
#: 同一类约束，所以用同一个门：房间有人在打就拒绝启动。
KINDS_NEEDING_QUIET = frozenset({KIND_ARENA, KIND_ITERATE, KIND_WRITE_SCRIPT})

# ---- 任务状态 ---------------------------------------------------------------

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS training_runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    params       TEXT NOT NULL DEFAULT '{}',
    summary      TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    exit_code    INTEGER,
    log_path     TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_training_runs_created ON training_runs(created_at DESC);
"""


@dataclass
class TrainingRun:
    """一条训练任务记录。"""

    run_id: str
    kind: str
    title: str = ""
    status: str = STATUS_RUNNING
    params: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    exit_code: Optional[int] = None
    log_path: str = ""
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def kind_title(self) -> str:
        """这个种类的中文名（面板里显示用）。"""

        return KIND_TITLES.get(self.kind, self.kind)

    @property
    def duration_seconds(self) -> Optional[float]:
        """跑了多久（没跑完就是 None；跑完了没记时间也返回 None）。"""

        if self.started_at is None or self.finished_at is None:
            return None
        return max(self.finished_at - self.started_at, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        """给面板用的字典（时间转成可读字符串，免得前端各自算时区）。"""

        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "kind_title": self.kind_title,
            "title": self.title,
            "status": self.status,
            "params": self.params,
            "summary": self.summary,
            "error": self.error,
            "exit_code": self.exit_code,
            "log_path": self.log_path,
            "created_at": _clock(self.created_at),
            "started_at": _clock(self.started_at),
            "finished_at": _clock(self.finished_at),
            "duration_seconds": (
                round(self.duration_seconds, 1) if self.duration_seconds is not None else None
            ),
        }


def _clock(stamp: Optional[float]) -> str:
    """把时间戳转成 `YYYY-mm-dd HH:MM:SS`（空值原样返回空串）。"""

    if not stamp:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


class TrainingStore:
    """训练记录的读写。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """一次调用的连接：进来开、出去**一定关**（异常也关）。

        必须显式 close：`with sqlite3.connect(...)` 只管事务（提交/回滚），**不关连接**，
        句柄会一直挂着——Windows 上后果就是数据目录删不掉（插件更新时"文件被占用"），
        而且每查一次泄漏一个。
        """

        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:  # 事务：块内抛异常就回滚
                yield connection
        finally:
            connection.close()

    # ---- 写 ----

    def create(self, kind: str, title: str, params: Dict[str, Any], log_path: Path) -> TrainingRun:
        """登记一条新任务（状态 `running`：起任务的人负责随后改成 done/failed）。"""

        run = TrainingRun(
            run_id=uuid.uuid4().hex[:12],
            kind=kind,
            title=title,
            status=STATUS_RUNNING,
            params=dict(params),
            log_path=str(log_path),
            created_at=time.time(),
            started_at=time.time(),
        )
        with self._session() as connection:
            connection.execute(
                "INSERT INTO training_runs"
                " (run_id, kind, title, status, params, summary, error, log_path, created_at, started_at)"
                " VALUES (?, ?, ?, ?, ?, '{}', '', ?, ?, ?)",
                (
                    run.run_id,
                    run.kind,
                    run.title,
                    run.status,
                    json.dumps(run.params, ensure_ascii=False),
                    run.log_path,
                    run.created_at,
                    run.started_at,
                ),
            )
        return run

    def finish(
        self,
        run_id: str,
        status: str,
        *,
        summary: Optional[Dict[str, Any]] = None,
        error: str = "",
        exit_code: Optional[int] = None,
    ) -> None:
        """收尾一条任务（状态、结论、错误、退出码、结束时间一次性写进去）。"""

        with self._session() as connection:
            connection.execute(
                "UPDATE training_runs SET status = ?, summary = ?, error = ?, exit_code = ?,"
                " finished_at = ? WHERE run_id = ?",
                (
                    status,
                    json.dumps(summary or {}, ensure_ascii=False),
                    error,
                    exit_code,
                    time.time(),
                    run_id,
                ),
            )

    def mark_interrupted(self) -> int:
        """把"库里还写着 running、其实已经没人跑"的任务标成失败。

        插件重启（或上次卸载没来得及收尾）会留下这种记录。不处理的话面板会永远显示
        "有一个任务在跑"，而且 `train.runner` 的互斥判断也会一直拦着新任务。
        返回改掉的条数。
        """

        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE training_runs SET status = ?, error = ?, finished_at = ?"
                " WHERE status = ?",
                (
                    STATUS_FAILED,
                    "插件重载或上次卸载时中断（这一条不是任务本身的失败原因）",
                    time.time(),
                    STATUS_RUNNING,
                ),
            )
            return int(cursor.rowcount or 0)

    # ---- 读 ----

    def get(self, run_id: str) -> Optional[TrainingRun]:
        """按编号取一条。"""

        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM training_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row) if row is not None else None

    def newest_running(self) -> Optional[TrainingRun]:
        """取最新一条还在跑的任务（理论上最多一条，runner 保证互斥）。"""

        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM training_runs WHERE status = ? ORDER BY created_at DESC LIMIT 1",
                (STATUS_RUNNING,),
            ).fetchone()
        return _row_to_run(row) if row is not None else None

    def list_recent(self, limit: int = 30, kind: str = "") -> List[TrainingRun]:
        """按时间倒序列出最近的任务。"""

        sql = "SELECT * FROM training_runs"
        args: List[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._session() as connection:
            rows = connection.execute(sql, tuple(args)).fetchall()
        return [_row_to_run(row) for row in rows]

    def counts(self) -> Dict[str, int]:
        """各状态各有多少条（面板概览用）。"""

        with self._session() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM training_runs GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}


def _row_to_run(row: sqlite3.Row) -> TrainingRun:
    """把一行记录读成 :class:`TrainingRun`（坏 JSON 就当成空，不让面板崩）。"""

    return TrainingRun(
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        status=str(row["status"]),
        params=_loads(row["params"]),
        summary=_loads(row["summary"]),
        error=str(row["error"]),
        exit_code=row["exit_code"],
        log_path=str(row["log_path"]),
        created_at=float(row["created_at"] or 0.0),
        started_at=float(row["started_at"]) if row["started_at"] else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] else None,
    )


def _loads(raw: Any) -> Dict[str, Any]:
    """读 JSON 字段，读不出来就返回空字典。"""

    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
