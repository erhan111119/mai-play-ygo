"""擂台结果库：每局一行，用来算胜率、看「到底有没有在出牌」。

用 SQLite 而不是 CSV，是因为训练跑起来一次几千上万局，边跑边写还要能边查
（比如「某副牌 vs 某个对手的胜率」）。表结构刻意扁平：一行一局，字段都能直接
喂给统计函数，不用连表。

写法约定：**每条 SQL 都内联在 ``execute()`` 调用里、值一律走占位符**。
这样静态扫描与人工审阅都能一眼看清「哪些是语句、哪些是数据」。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS duels (
    duel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    arena TEXT NOT NULL,
    left_name TEXT NOT NULL,
    right_name TEXT NOT NULL,
    left_style TEXT NOT NULL DEFAULT '',
    right_style TEXT NOT NULL DEFAULT '',
    winner TEXT NOT NULL,
    turns INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actions_left INTEGER NOT NULL DEFAULT 0,
    actions_right INTEGER NOT NULL DEFAULT 0,
    lp_left INTEGER NOT NULL DEFAULT 0,
    lp_right INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    round_index INTEGER NOT NULL DEFAULT -1,
    card_usage TEXT NOT NULL DEFAULT '{}',
    card_usage_opponent TEXT NOT NULL DEFAULT '{}',
    card_seen TEXT NOT NULL DEFAULT '{}',
    card_seen_opponent TEXT NOT NULL DEFAULT '{}',
    sets_left INTEGER NOT NULL DEFAULT 0,
    sets_right INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_duels_left ON duels (left_name);
CREATE INDEX IF NOT EXISTS idx_duels_right ON duels (right_name);
"""


class DuelStore:
    """对局结果库。

    Args:
        path: SQLite 文件路径；父目录会自动创建。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._migrate()

    def _migrate(self) -> None:
        """给早先建的库补上新列。

        ``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列，所以新增字段必须单独补，
        否则旧库一写就报"没有这一列"。
        """

        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(duels)")}
        if "round_index" not in columns:
            self._connection.execute(
                "ALTER TABLE duels ADD COLUMN round_index INTEGER NOT NULL DEFAULT -1"
            )
            self._connection.commit()
        if "card_usage" not in columns:
            self._connection.execute(
                "ALTER TABLE duels ADD COLUMN card_usage TEXT NOT NULL DEFAULT '{}'"
            )
            self._connection.commit()
        # 对手用到的卡（2026-10-07 拆出来）：``card_usage`` 以前是"双方混记"的一张表，
        # 于是 `/优化卡组` 的死牌判定与 `first_turn_accept.py` 会把"对手打过的同一张卡"
        # 算成我们也用过（实测 r24~r27 里有 15 局是"只有对手带这张卡却进了台账"）。
        if "card_usage_opponent" not in columns:
            self._connection.execute(
                "ALTER TABLE duels ADD COLUMN card_usage_opponent TEXT NOT NULL DEFAULT '{}'"
            )
            self._connection.commit()
        # 「进过手牌」的卡（2026-10-07 加）：`card_usage` 只回答"用过没有"，
        # 回答不了"这张卡到底有没有到手"——而后者才是"该不该加第几张"的分母
        # （见 `tools/card_vitality.py`）。对手那一份通常为空（手牌是隐藏信息）。
        for name in ("card_seen", "card_seen_opponent"):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE duels ADD COLUMN {name} TEXT NOT NULL DEFAULT '{{}}'"  # noqa: S608 - 列名来自这里写死的白名单
                )
                self._connection.commit()
        # 展开指标：特召次数与效果发动次数。**为什么值得单独存**——"能不能展开"用胜率看不出来，
        # 而"每次抽检生成的展开流程好不好用"靠的就是这两列（见 tools/verify_deck_plan.py）。
        for name in ("sp_summons_left", "sp_summons_right", "effects_left", "effects_right"):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE duels ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"  # noqa: S608 - 列名来自这里写死的白名单
                )
                self._connection.commit()
        # 盖放次数。**为什么单独存**：「动作总数」里已经含盖放（2026-10-06 起），但只有拆开才回答得了
        # "这一局的 2 个动作是 2 次盖放，还是 1 次召唤 + 1 个效果"——异常行里的差别就靠它。
        for name in ("sets_left", "sets_right"):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE duels ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"  # noqa: S608 - 同上
                )
                self._connection.commit()

    @property
    def path(self) -> Path:
        """库文件路径。"""

        return self._path

    def record(
        self,
        arena: str,
        outcome,
        *,
        left_style: str = "",
        right_style: str = "",
    ) -> None:
        """记一局。

        Args:
            arena: 擂台名（例如 ``baseline`` 或 ``gen-3``），方便按批次查。
            outcome: :class:`train.arena.DuelOutcome`。
            left_style: 左方用的出牌脚本名（便于分清同副牌的两种脚本）。
            right_style: 右方的出牌脚本名。
        """

        payload = asdict(outcome)
        self._connection.execute(
            "INSERT INTO duels ("
            "  arena, left_name, right_name, left_style, right_style, winner, turns,"
            "  duration_seconds, reason, actions_left, actions_right, lp_left, lp_right,"
            "  error, round_index, card_usage, card_usage_opponent,"
            "  card_seen, card_seen_opponent,"
            "  sp_summons_left, sp_summons_right, effects_left, effects_right,"
            "  sets_left, sets_right, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                arena,
                payload["left"],
                payload["right"],
                left_style,
                right_style,
                payload["winner"],
                payload["turns"],
                payload["duration_seconds"],
                payload["reason"],
                payload["actions_left"],
                payload["actions_right"],
                payload["lp_left"],
                payload["lp_right"],
                payload["error"],
                payload["round_index"],
                json.dumps(payload.get("card_usage") or {}, ensure_ascii=False),
                json.dumps(payload.get("card_usage_opponent") or {}, ensure_ascii=False),
                json.dumps(payload.get("card_seen") or {}, ensure_ascii=False),
                json.dumps(payload.get("card_seen_opponent") or {}, ensure_ascii=False),
                int(payload.get("sp_summons_left") or 0),
                int(payload.get("sp_summons_right") or 0),
                int(payload.get("effects_left") or 0),
                int(payload.get("effects_right") or 0),
                int(payload.get("sets_left") or 0),
                int(payload.get("sets_right") or 0),
                time.time(),
            ),
        )
        self._connection.commit()

    def count(self, arena: Optional[str] = None) -> int:
        """总局数（可按擂台筛）。"""

        if arena is None:
            row = self._connection.execute("SELECT COUNT(*) FROM duels").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM duels WHERE arena = ?", (arena,)
            ).fetchone()
        return int(row[0]) if row else 0

    def win_rates(self, arena: Optional[str] = None) -> List[Tuple[str, int, int, float]]:
        """按脚本统计 ``[(名字, 胜, 总, 胜率)]``，按胜率降序。

        只统计分出胜负的局；左右两侧各查一次，所以同一副牌无论坐哪边都会被算进去。
        """

        rows: List[Tuple[str, int, int]] = []
        # 左方视角
        rows.extend(
            (str(name), int(wins or 0), int(total or 0))
            for name, wins, total in self._connection.execute(
                "SELECT left_name,"
                "  SUM(CASE WHEN winner = left_name THEN 1 ELSE 0 END),"
                "  SUM(CASE WHEN winner IN (left_name, right_name) THEN 1 ELSE 0 END)"
                " FROM duels WHERE (? IS NULL OR arena = ?) GROUP BY left_name",
                (arena, arena),
            )
        )
        # 右方视角
        rows.extend(
            (str(name), int(wins or 0), int(total or 0))
            for name, wins, total in self._connection.execute(
                "SELECT right_name,"
                "  SUM(CASE WHEN winner = right_name THEN 1 ELSE 0 END),"
                "  SUM(CASE WHEN winner IN (left_name, right_name) THEN 1 ELSE 0 END)"
                " FROM duels WHERE (? IS NULL OR arena = ?) GROUP BY right_name",
                (arena, arena),
            )
        )

        merged: Dict[str, Tuple[int, int]] = {}
        for name, wins, total in rows:
            wins_sum, total_sum = merged.get(name, (0, 0))
            merged[name] = (wins_sum + wins, total_sum + total)
        table = [
            (name, wins, total, (wins / total) if total else 0.0)
            for name, (wins, total) in merged.items()
        ]
        return sorted(table, key=lambda item: (-item[3], item[0]))

    def silent_scripts(
        self, arena: Optional[str] = None, *, threshold: int = 3
    ) -> List[Tuple[str, int]]:
        """列出「一局下来几乎没动作」的记录：``[(名字, 局数)]``。

        这是上次实测踩过的坑：出牌脚本与卡表不匹配时会整局空过，胜负看起来只是
        「打不过」，其实根本没在打。训练前先看这个列表，能省很多瞎猜。
        """

        cursor = self._connection.execute(
            "SELECT name, COUNT(*) FROM ("
            "  SELECT left_name AS name, actions_left AS actions FROM duels"
            "    WHERE actions_left < ? AND (? IS NULL OR arena = ?)"
            "  UNION ALL"
            "  SELECT right_name AS name, actions_right AS actions FROM duels"
            "    WHERE actions_right < ? AND (? IS NULL OR arena = ?)"
            ") GROUP BY name ORDER BY COUNT(*) DESC",
            (threshold, arena, arena, threshold, arena, arena),
        )
        return [(str(name), int(count)) for name, count in cursor.fetchall()]

    def close(self) -> None:
        """关闭连接。"""

        self._connection.close()
