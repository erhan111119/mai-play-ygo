"""知识库的**只读**查询层：卡牌事实、卡组线路、系列知识、交互建议，以及对局中的检索供料。

这个模块的定位（见 ``KNOWLEDGE.md``）：**决策仍然由模型做**，它只负责在决策那一刻
把该知道的几条知识取出来喂进提示词。所以这里：

* **只读**（``mode=ro``）——写入只发生在 ``tools/build_*.py`` 里；
* **所有查询参数绑定**，SQL 结构一律是固定字符串（不做拼接、不做动态表名）；
* **库不存在/查不到就返回空**——知识库是"锦上添花"，绝不因为缺数据让一局打不出来。

用法::

    knowledge = Knowledge.from_data_dir(data_dir)
    fact = knowledge.card_facts(14558127)                              # 卡牌事实
    block = knowledge.retrieve(deck_id=88, card_id=... )               # 提示词素材
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# 库文件位置：``<数据目录>/knowledge/knowledge.db``
KNOWLEDGE_SUBDIR = "knowledge"
KNOWLEDGE_FILE = "knowledge.db"

# 检索供料的硬上限（避免把提示词撑爆——每次只喂相关的几条）
MAX_ITEMS = 4
MAX_ITEM_CHARS = 120
"""单条知识最多多少字；提示词里按这个截断（宁可少喂，不可喧宾夺主）。"""
MAX_TOTAL_CHARS = 1500
"""一次检索喂进提示词的总字数上限。"""
MAX_TOTAL_CHARS_FULL_PLAN = 2400
"""**要展开流程**那一问的总字数上限（"这一步做什么"时把整份线路喂进去）。

为什么单独给一档：卡牌事实/对手系列那几条一句话就够，但"我这副牌的展开流程"是**顺序性知识**——
截成两行等于把顺序丢了（"先 A 后 B"变成"A、B"），而那正是模型做不出来的东西。
"""
MAX_PLAN_CHARS = 1200
"""整份展开流程最多多少字（生成时就卡在这个数，见 tools/build_deck_plan_ai.py）。

1200 而不是 900：流程要**按起手张数分档**（单卡起手/两卡起手各写一条），
只写一份"通用流程"在实战里对不上手牌。
"""

# 动作/命中标签 → 人话（**渲染在 prompts 里的是这份表**，所以它决定模型看到什么）
# 键是 card_facts 里存的那套英文标签，值是给模型看的中文。
ACTION_LABELS: Dict[str, str] = {
    "search": "检索",
    "to_hand": "加手",
    "ss": "特召",
    "destroy": "破坏",
    "negate": "无效",
    "disable": "无效效果",
    "to_grave": "送墓",
    "to_deck": "回卡组",
    "banish": "除外",
    "draw": "抽卡",
    "mill": "堆墓",
    "recover": "回收",
    "bounce": "回手",
    "damage": "烧血",
    "release": "解放",
    "control": "抢控制权",
    "any_effect": "任意发动中的效果",
    "field_card": "场上的卡",
    "gy_card": "墓地的卡",
    "hand_card": "手牌",
    "monster_effect": "怪兽效果",
    "spell_trap_effect": "魔陷的发动/效果",
}


def label_of(tag: str) -> str:
    """把内部标签翻成人话；不认识的标签原样返回（不吞掉信息）。"""

    return ACTION_LABELS.get(tag, tag)


def _migrate_decisions(connection: sqlite3.Connection) -> None:
    """给已存在的知识库补上 ``decisions`` 表后加的列。

    ``CREATE TABLE IF NOT EXISTS`` 不会改动已有表，新增字段必须自己迁移——和
    ``duel/deckpool.py`` 的 ``_migrate_schema`` 一个做法：按列名逐个检查，缺什么补什么。
    补列语句在这里**写死**（``ALTER TABLE`` 不支持参数占位），外部输入一律不参与。

    ``duel_key`` 的索引也在这里建：放进 :data:`SCHEMA` 会在老库上先于补列执行、
    直接报 ``no such column``——那样连接都建不起来，迁移根本没机会跑（实测踩过）。
    """

    columns = {row[1] for row in connection.execute("PRAGMA table_info(decisions)")}
    if "duel_key" not in columns:
        # 这一列把"同一局的决策"聚起来，回填结果与复盘都靠它（见 DecisionLog.finish_duel）
        connection.execute("ALTER TABLE decisions ADD COLUMN duel_key TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_dec_duel ON decisions(duel_key)")
    connection.commit()


def duel_outcome_text(*, result: str, turns: int = 0, lp_self: int = 0, lp_other: int = 0) -> str:
    """把一局的结果写成回填用的紧凑串（``result=loss;turns=2;lp=0:4000``）。

    ``result`` 取 ``win`` / ``loss`` / ``draw``（站在**开了问 AI 的那一方**看）——
    ``tools/review_decisions.py`` 按这个字段把局分组，所以取值要固定、不要写中文长句。
    """

    return f"result={result};turns={int(turns)};lp={int(lp_self)}:{int(lp_other)}"


class DecisionLog:
    """把每次"问 AI"落到 ``decisions`` 表（**写入路径**；读取在 :class:`Knowledge`）。

    为什么要真的落库：复盘时要能回答"哪些决策点它答得差"——例如"被它否决的发动里，
    有多少后来被证明是错的"。没有这份数据，改提示词就只能靠感觉。

    写入策略：连接**按线程私有**（决策发生在答复线程里），每条一次 ``commit``——
    单条几毫秒、不影响对局；任何失败都由调用方吞掉（决策日志绝不能拖垮一局）。

    **一个实例 = 一局**（``duel_key`` 见 :meth:`finish_duel`）：擂台与房间都是每局新建一个，
    打完由调用方回填结果。少了这一步，日志里就只有"问了什么、答了什么"，
    没有"后来怎样"——那等于无法判断干预的对错。
    """

    def __init__(
        self, database: Path, *, arena: str = "", deck_key: str = "", duel_key: str = ""
    ) -> None:
        self._path = Path(database)
        self._arena = arena
        self._deck_key = deck_key
        # 这一局的标识（留空就现生成一个）：回填结果与复盘都靠它把同一局的决策聚起来
        self._duel_key = duel_key or uuid.uuid4().hex[:12]
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []

    @property
    def duel_key(self) -> str:
        """这一局的标识。"""

        return self._duel_key

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.executescript(SCHEMA)
        _migrate_decisions(connection)
        self._local.connection = connection
        self._connections.append(connection)
        return connection

    def add(
        self,
        *,
        kind: str = "",
        card_id: int = 0,
        board: str = "",
        retrieved: str = "",
        answer: str = "",
        audit: str = "",
        cost_ms: int = 0,
    ) -> None:
        """记一次决策（参数绑定；失败往上抛，由调用方决定要不要吞）。"""

        connection = self._connect()
        connection.execute(
            "INSERT INTO decisions (created_at, arena, deck_key, opponent_key, kind, card_id,"
            " board_json, retrieved_json, answer, audit, cost_ms, duel_key, outcome)"
            " VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, '')",
            (
                time.time(),
                str(self._arena),
                str(self._deck_key),
                str(kind),
                int(card_id),
                str(board)[:500],
                str(retrieved)[:800],
                str(answer)[:40],
                str(audit)[:200],
                int(cost_ms),
                str(self._duel_key),
            ),
        )
        connection.commit()

    def finish_duel(self, outcome: str) -> int:
        """这一局打完了：给这一局的所有决策行补上结果，返回补了多少行。

        ``outcome`` 用紧凑的键值串（见 :meth:`duel_outcome_text`），例如
        ``result=loss;turns=2;lp=0:4000``。

        **为什么必须回填**：决策日志原先只记"问了什么、答了什么"，于是"AI 的干预到底帮没帮上忙"
        只能靠每次几百局的对照实验，事后从数据里看不出来（第一次统计时 ``outcome`` 列 4,442 行全空）。
        回填之后，:mod:`tools.review_decisions` 可以直接按"输了的那几局它否决得更多吗"来算。
        这里只补 ``outcome`` 还是空的行，重复调用不会覆盖第一次写的结论。
        """

        connection = self._connect()
        cursor = connection.execute(
            "UPDATE decisions SET outcome = ? WHERE duel_key = ? AND outcome = ''",
            (str(outcome)[:80], str(self._duel_key)),
        )
        connection.commit()
        return int(cursor.rowcount)

    def close(self) -> None:
        """关闭所有线程的连接。"""

        for connection in self._connections:
            try:
                connection.close()
            except sqlite3.Error:
                continue
        self._connections.clear()
        self._local = threading.local()


# 数行数用的固定语句（表名写死在 SQL 里，不做动态拼接）
_COUNT_STATEMENTS: Tuple[Tuple[str, str], ...] = (
    ("card_facts", "SELECT COUNT(*) FROM card_facts"),
    ("deck_plans", "SELECT COUNT(*) FROM deck_plans"),
    ("interactions", "SELECT COUNT(*) FROM interactions"),
    ("decisions", "SELECT COUNT(*) FROM decisions"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT
);

-- ① 卡牌事实（tools/build_card_facts.py 自动抽；全卡覆盖）
CREATE TABLE IF NOT EXISTS card_facts(
  card_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  kinds TEXT NOT NULL DEFAULT '',
  hits TEXT NOT NULL DEFAULT '',
  events TEXT NOT NULL DEFAULT '',
  target_scope TEXT NOT NULL DEFAULT '',
  timing TEXT NOT NULL DEFAULT '',
  limit_kind TEXT NOT NULL DEFAULT 'none',
  self_lock TEXT NOT NULL DEFAULT '',
  negate_what TEXT NOT NULL DEFAULT '',
  is_interaction INTEGER NOT NULL DEFAULT 0,
  from_zones TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT 'text',
  evidence TEXT NOT NULL DEFAULT '',
  source_mtime REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);

-- ② 卡组/系列线路（tools/build_deck_plans.py 生成 + 人工整理合并）
CREATE TABLE IF NOT EXISTS deck_plans(
  plan_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'deck',
  deck_id INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0
);

-- ③ 交互建议（坑 × 对手动作类别）
CREATE TABLE IF NOT EXISTS interactions(
  id INTEGER PRIMARY KEY,
  handtrap_id INTEGER NOT NULL,
  target_kind TEXT NOT NULL,
  verdict TEXT NOT NULL DEFAULT 'neutral',
  why TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'auto',
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inter ON interactions(handtrap_id, target_kind);

-- ⑤ 每张卡的"怎么用/该不该拦/威胁多大"（tools/build_card_notes.py 推导 + AI 精写）
CREATE TABLE IF NOT EXISTS card_notes(
  card_id INTEGER PRIMARY KEY,
  usage TEXT NOT NULL DEFAULT '',        -- 怎么用（干扰牌/引擎件/解场…）
  block_advice TEXT NOT NULL DEFAULT '', -- 该不该拦、什么时候拦
  threat TEXT NOT NULL DEFAULT '',       -- 威胁度（带理由，可复核）
  source TEXT NOT NULL DEFAULT 'derived',-- derived（从事实推） / ai（模型精写）
  updated_at REAL NOT NULL DEFAULT 0
);

-- ④ 决策日志（对局中每次问答；复盘用，只追加）
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY,
  created_at REAL NOT NULL DEFAULT 0,
  arena TEXT NOT NULL DEFAULT '',
  deck_key TEXT NOT NULL DEFAULT '',
  opponent_key TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  card_id INTEGER NOT NULL DEFAULT 0,
  board_json TEXT NOT NULL DEFAULT '',
  retrieved_json TEXT NOT NULL DEFAULT '',
  answer TEXT NOT NULL DEFAULT '',
  audit TEXT NOT NULL DEFAULT '',
  cost_ms INTEGER NOT NULL DEFAULT 0,
  duel_key TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dec ON decisions(arena, created_at);
"""


@dataclasses.dataclass(frozen=True)
class CardFact:
    """一张卡的事实（只读视图）。"""

    card_id: int
    name: str
    kinds: Tuple[str, ...]
    hits: Tuple[str, ...]
    events: Tuple[str, ...]
    timing: Tuple[str, ...]
    limit_kind: str
    self_lock: Tuple[str, ...]
    is_interaction: bool
    from_zones: Tuple[str, ...]
    confidence: str
    evidence: str

    def describe(self) -> str:
        """一句话描述，直接进提示词。

        ``hits`` 与 ``kinds`` 是两件事：前者是"它能拦住什么"（手坑的用法），
        后者是"它自己能做什么"。混在一起会让模型把灰流丽当成一张检索卡。
        **渲染成中文**：模型读"能拦：特召/检索"比读"ss/search"靠谱得多（实测英文键它要猜）。
        """

        parts = [f"{self.name}（#{self.card_id}）"]
        if self.hits:
            parts.append("能拦：" + "/".join(label_of(tag) for tag in self.hits[:5]))
        # "能做"在它只是"拦这些东西"时是误导（灰流丽不是检索卡），所以两者重合就不重复说
        if self.kinds and not set(self.kinds) <= set(self.hits):
            parts.append("能做：" + "/".join(label_of(tag) for tag in self.kinds[:5]))
        if self.events:
            parts.append("响应：" + "/".join(label_of(tag) for tag in self.events[:4]))
        if self.self_lock:
            parts.append("自肃：" + "/".join(self.self_lock[:3]))
        if self.is_interaction:
            parts.append("可用作阻抗")
        return "｜".join(parts)


@dataclasses.dataclass(frozen=True)
class PlanEntry:
    """一条线路/系列知识。"""

    plan_key: str
    kind: str
    title: str
    body: str
    source: str

    def describe(self, *, limit: int = MAX_ITEM_CHARS) -> str:
        """带标题的一行（超长截断）。"""

        text = " ".join(self.body.split())
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return f"{self.title}：{text}" if self.title else text


@dataclasses.dataclass(frozen=True)
class DuelTrace:
    """一局里的"问 AI"概况（``/复盘`` 与工具用）。

    有了 ``outcome`` 才谈得上判断干预的对错：同样是"否决了 30 次发动"，
    出现在赢的局里和输的局里含义完全不同。
    """

    duel_key: str
    outcome: str
    result: str
    """从 ``outcome`` 里解出来的胜负（``win`` / ``loss`` / ``draw`` / ``""`` 未知）。"""

    asks: int
    """这一局问了多少次。"""

    vetos: int
    """其中答 "no"（否决）的次数（只统计 ``activate`` 那一类问题）。"""

    activate_asks: int
    slowest_ms: int
    kinds: Dict[str, int]
    """问法 → 次数（``activate`` / ``idle_action`` / ``attack_target``）。"""

    def describe(self) -> str:
        """一行结论（给消息与报告复用）。"""

        parts = [f"问 {self.asks} 次"]
        if self.activate_asks:
            rate = self.vetos / self.activate_asks
            parts.append(f"否决发动 {self.vetos}/{self.activate_asks} = {rate:.0%}")
        parts.append(f"最慢一问 {self.slowest_ms / 1000:.1f} 秒")
        return "｜".join(parts)


def result_of_outcome(outcome: str) -> str:
    """从回填的 ``outcome`` 串里取胜负（``result=win;turns=3;...`` → ``win``）。"""

    for chunk in str(outcome).split(";"):
        name, _, value = chunk.partition("=")
        if name.strip() == "result":
            return value.strip()
    return ""


def default_data_dir(plugin_root: Path) -> Path:
    """插件数据目录（宿主约定：``<MaiBot>/data/plugins/<插件 id>/``）。"""

    import json

    manifest = Path(plugin_root) / "_manifest.json"
    plugin_id = ""
    try:
        plugin_id = str(json.loads(manifest.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        plugin_id = ""
    if not plugin_id:
        return Path(plugin_root) / "data"
    return Path(plugin_root).parent.parent / "data" / "plugins" / plugin_id


def default_knowledge_db(data_dir: Path) -> Path:
    """知识库文件路径。"""

    return Path(data_dir) / KNOWLEDGE_SUBDIR / KNOWLEDGE_FILE


@dataclasses.dataclass(frozen=True)
class KnowledgePaths:
    """给工具用的路径集合（避免每个工具各写一份推导逻辑）。"""

    data_dir: Path
    database: Path


class Knowledge:
    """知识库的只读查询。库不在时所有查询都返回空（不影响对局）。"""

    def __init__(
        self,
        database: Path,
        *,
        cards_db: Optional[Path] = None,
        plugin_root: Optional[Path] = None,
    ) -> None:
        self._path = Path(database)
        self._cards_db = Path(cards_db) if cards_db else None
        self._plugin_root = Path(plugin_root) if plugin_root else None
        self._connection: Optional[sqlite3.Connection] = None
        self._cards_connection: Optional[sqlite3.Connection] = None
        # **连接按线程私有**：答复任务是 ``asyncio.to_thread`` 里跑的（房间还可能多个房共用
        # 这一份 Knowledge），而 SQLite 连接不能跨线程用——实测踩过：不这么做时每次检索都抛
        # "SQLite objects created in a thread ..."，异常被上层吞掉、表面看只是"没有知识"，
        # 整条对照腿白跑。线程私有连接不需要锁，也不会互相阻塞。
        self._local = threading.local()
        self._setcode_cache: Dict[int, Tuple[int, ...]] = {}

    @classmethod
    def from_data_dir(
        cls, data_dir: Path, *, cards_db: Optional[Path] = None, plugin_root: Optional[Path] = None
    ) -> "Knowledge":
        """按数据目录打开（插件与擂台都走这条）。"""

        return cls(
            default_knowledge_db(Path(data_dir)), cards_db=cards_db, plugin_root=plugin_root
        )

    @classmethod
    def from_plugin_root(cls, plugin_root: Path, *, data_dir: Optional[Path] = None) -> "Knowledge":
        """按插件根目录打开（工具里的常见入口；卡库位置从 config.toml 读）。"""

        root = Path(plugin_root)
        return cls(default_knowledge_db(data_dir or default_data_dir(root)), plugin_root=root)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def available(self) -> bool:
        """库存在且能开——不存在时调用方应当走"没有知识"的分支。"""

        return self._connect() is not None

    def _connect(self) -> Optional[sqlite3.Connection]:
        """取**本线程**的知识库连接（库不存在时返回 None）。

        线程私有是有意的：SQLite 连接不能跨线程使用，而检索发生在答复线程里；
        每个线程各开一条只读连接既安全又不需要加锁。
        """

        connection = getattr(self._local, "knowledge", None)
        if connection is not None:
            return connection
        if not self._path.is_file():
            return None
        try:
            # 只读打开：这个模块永远不写库
            connection = sqlite3.connect(
                f"file:{self._path.as_posix()}?mode=ro", uri=True, check_same_thread=False
            )
        except sqlite3.Error:
            return None
        self._local.knowledge = connection
        self._connection = connection
        return connection

    def close(self) -> None:
        """关掉**本线程**的连接（工具里用完记得调；其它线程的连接随进程结束回收）。"""

        for attribute in ("knowledge", "cards"):
            connection = getattr(self._local, attribute, None)
            if connection is not None:
                connection.close()
                setattr(self._local, attribute, None)
        self._connection = None
        self._cards_connection = None

    # ------------------------------------------------------------------ 查询

    def _has_duel_key(self, connection: sqlite3.Connection) -> bool:
        """老库可能还没有 ``duel_key`` 列（补列发生在写入侧）——读取侧要先问一声。"""

        return any(row[1] == "duel_key" for row in connection.execute("PRAGMA table_info(decisions)"))

    def last_duel(self, arena: str) -> Optional[DuelTrace]:
        """这个来源（房间是 ``room:<群标识>``）最近一局的决策概况；没有就 ``None``。

        只统计**有决策记录**的局：没开问 AI 的局本来就没有可复盘的决策。
        """

        connection = self._connect()
        if connection is None or not self._has_duel_key(connection):
            return None
        row = connection.execute(
            "SELECT duel_key FROM decisions WHERE arena = ? AND duel_key <> ''"
            " ORDER BY id DESC LIMIT 1",
            (str(arena),),
        ).fetchone()
        if row is None:
            return None
        duel_key = str(row[0])
        kinds: Dict[str, int] = {}
        for kind, count in connection.execute(
            "SELECT kind, COUNT(*) FROM decisions WHERE duel_key = ? GROUP BY kind", (duel_key,)
        ):
            kinds[str(kind)] = int(count)
        summary = connection.execute(
            "SELECT MAX(outcome), COUNT(*),"
            " SUM(CASE WHEN kind = 'activate' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN kind = 'activate' AND answer = 'no' THEN 1 ELSE 0 END),"
            " MAX(cost_ms) FROM decisions WHERE duel_key = ?",
            (duel_key,),
        ).fetchone()
        if summary is None:
            return None
        outcome = str(summary[0] or "")
        return DuelTrace(
            duel_key=duel_key,
            outcome=outcome,
            result=result_of_outcome(outcome),
            asks=int(summary[1] or 0),
            activate_asks=int(summary[2] or 0),
            vetos=int(summary[3] or 0),
            slowest_ms=int(summary[4] or 0),
            kinds=kinds,
        )

    def duel_decisions(self, duel_key: str, limit: int = 40) -> List[Tuple[str, int, str, int]]:
        """一局的决策明细（``(问法, 卡号, 答复, 耗时毫秒)``，按耗时倒序取前 ``limit`` 条）。

        按耗时倒序而不是按时间：复盘时最想问的是"哪几问最费时间"（时间被吃光就是少打牌的直接原因）。
        """

        connection = self._connect()
        if connection is None or not self._has_duel_key(connection):
            return []
        rows = connection.execute(
            "SELECT kind, card_id, answer, cost_ms FROM decisions WHERE duel_key = ?"
            " ORDER BY cost_ms DESC LIMIT ?",
            (str(duel_key), max(1, int(limit))),
        ).fetchall()
        return [(str(row[0]), int(row[1] or 0), str(row[2]), int(row[3] or 0)) for row in rows]

    def card_facts(self, card_id: int) -> Optional[CardFact]:
        """取一张卡的事实；没有这条记录返回 ``None``。"""

        connection = self._connect()
        if connection is None:
            return None
        row = connection.execute(
            "SELECT card_id, name, kinds, hits, events, timing, limit_kind, self_lock,"
            " is_interaction, from_zones, confidence, evidence FROM card_facts WHERE card_id = ?",
            (int(card_id),),
        ).fetchone()
        if row is None:
            return None
        return CardFact(
            card_id=int(row[0]),
            name=str(row[1] or ""),
            kinds=tuple(part for part in str(row[2] or "").split(",") if part),
            hits=tuple(part for part in str(row[3] or "").split(",") if part),
            events=tuple(part for part in str(row[4] or "").split(",") if part),
            timing=tuple(part for part in str(row[5] or "").split(",") if part),
            limit_kind=str(row[6] or "none"),
            self_lock=tuple(part for part in str(row[7] or "").split(",") if part),
            is_interaction=bool(row[8]),
            from_zones=tuple(part for part in str(row[9] or "").split(",") if part),
            confidence=str(row[10] or "text"),
            evidence=str(row[11] or ""),
        )

    def card_notes(self, card_id: int) -> Optional[Tuple[str, str, str, str]]:
        """取一张卡的 ``(怎么用, 该不该拦, 威胁度, 来源)``；没有这条返回 ``None``。"""

        connection = self._connect()
        if connection is None:
            return None
        row = connection.execute(
            "SELECT usage, block_advice, threat, source FROM card_notes WHERE card_id = ?",
            (int(card_id),),
        ).fetchone()
        if row is None:
            return None
        return (str(row[0]), str(row[1]), str(row[2]), str(row[3]))

    def plan(self, plan_key: str) -> Optional[PlanEntry]:
        """按 key 取线路/系列知识（``deck:88`` / ``series:0x2ed``）。"""

        connection = self._connect()
        if connection is None:
            return None
        row = connection.execute(
            "SELECT plan_key, kind, title, body, source FROM deck_plans WHERE plan_key = ?",
            (str(plan_key),),
        ).fetchone()
        if row is None:
            return None
        return PlanEntry(
            plan_key=str(row[0]),
            kind=str(row[1]),
            title=str(row[2]),
            body=str(row[3]),
            source=str(row[4]),
        )

    def deck_plan(self, deck_id: int) -> Optional[PlanEntry]:
        """取某副牌的线路。"""

        return self.plan(f"deck:{int(deck_id)}")

    def series_plan(self, setcode: int) -> Optional[PlanEntry]:
        """取某个系列的知识（``setcode`` 是低 16 位的系列码）。"""

        return self.plan(f"series:0x{int(setcode) & 0xffff:x}")

    def interactions(self, handtrap_id: int) -> List[Tuple[str, str, str, str]]:
        """取某张坑的交互建议：``[(动作类别, verdict, 理由, 来源), ...]``。

        来源 ``curated`` 是人工判定的时机（更准），``auto`` 是从卡牌事实推的（"它能拦什么"），
        取用时**人工的优先**——见 :meth:`retrieve`。
        """

        connection = self._connect()
        if connection is None:
            return []
        rows = connection.execute(
            "SELECT target_kind, verdict, why, source FROM interactions WHERE handtrap_id = ?",
            (int(handtrap_id),),
        ).fetchall()
        return [(str(a), str(b), str(c), str(d)) for a, b, c, d in rows]

    def counts(self) -> Dict[str, int]:
        """各表的行数（体检报告用）。语句固定，表名不参与拼接。"""

        connection = self._connect()
        if connection is None:
            return {}
        result: Dict[str, int] = {}
        for name, statement in _COUNT_STATEMENTS:
            try:
                result[name] = int(connection.execute(statement).fetchone()[0])
            except sqlite3.Error:
                result[name] = -1
        return result

    # -------------------------------------------------------------- 检索供料

    def retrieve(
        self,
        *,
        deck_id: int = 0,
        card_id: int = 0,
        opponent_cards: Sequence[int] = (),
        limit: int = MAX_ITEMS,
        full_deck_plan: bool = False,
    ) -> List[str]:
        """给这次决策取"该知道的几条"，返回可直接拼进提示词的文本行。

        只挑**与当前这一问相关**的：我要判定的这张卡的事实、我这副牌的线路、
        对手主轴（按对手场上/用过的卡推断系列）、以及这张牌作为阻抗时的用法。
        没有知识库时返回空列表，调用方照原来的路子走。

        Args:
            full_deck_plan: 是否**整份**喂"我这副牌的展开流程"（不截成两行）。
                只有"这一步做什么"那种决策该开——顺序性知识截短了就没用了；
                而"要不要发动某张牌"只需要一两句事实，喂整篇反而淹掉局面。
        """

        if not self.available:
            return []
        items: List[str] = []
        plan_index = -1

        if card_id:
            fact = self.card_facts(int(card_id))
            if fact is not None:
                line = f"【这张牌的事实】{fact.describe()}"
                if fact.confidence == "text":
                    line += "（无卡牌脚本，事实来自效果文本，仅供参考）"
                items.append(line)
            # 每张卡的"怎么用/该拦不该拦/威胁多大"（推导服务全卡，模型精写覆盖它）
            note = self.card_notes(int(card_id))
            if note is not None:
                usage, block, threat, source = note
                tag = "" if source == "ai" else "（自动推导）"
                items.append(f"【这张牌怎么用/威胁】{usage}｜拦不拦：{block}｜威胁：{threat}{tag}")
            # 这张牌当阻抗怎么用：人工判定优先，其次"该交"的那些，最后"留着"的——
            # 一条"对除外：留着"单独出现毫无意义，得让模型看到"能拦什么该交、什么该留"
            rows = self.interactions(int(card_id))
            if rows:
                order = {"hit": 1, "neutral": 2, "hold": 3}
                rows = sorted(
                    rows,
                    key=lambda row: (0 if row[3] == "curated" else 1, order.get(row[1], 4)),
                )
                rendered = "；".join(
                    f"对「{label_of(row[0])}」{row[1]}" + (f"（{row[2]}）" if row[2] else "")
                    for row in rows[:2]
                )
                items.append(f"【这张牌当阻抗用】{rendered}")

        if deck_id:
            plan = self.deck_plan(int(deck_id))
            if plan is not None:
                plan_index = len(items)
                if full_deck_plan and plan.body.strip():
                    items.append("【我这副牌的展开流程】" + " ".join(plan.body.split()))
                else:
                    items.append(f"【我这副牌的线路】{plan.describe(limit=MAX_ITEM_CHARS * 2)}")

        for setcode in self._series_from_cards(opponent_cards)[:2]:
            plan = self.series_plan(setcode)
            if plan is None:
                continue
            items.append(f"【对手系列】{plan.describe(limit=MAX_ITEM_CHARS * 2)}")

        # 统一裁剪：单条截断（整份线路那一条例外）+ 总量上限
        trimmed: List[str] = []
        total = 0
        budget = MAX_TOTAL_CHARS_FULL_PLAN if full_deck_plan else MAX_TOTAL_CHARS
        for index, item in enumerate(items[: max(1, limit)]):
            allowed = MAX_PLAN_CHARS + 20 if index == plan_index else MAX_ITEM_CHARS * 2
            text = item if len(item) <= allowed else item[: allowed - 1] + "…"
            if total + len(text) > budget and trimmed:
                break
            trimmed.append(text)
            total += len(text)
        return trimmed

    def _series_from_cards(self, card_ids: Iterable[int]) -> List[int]:
        """从对手的卡推断它的主轴系列（出现 ≥3 张的系列才算主轴）。

        为什么给模型"体系"而不是"一张张卡"：对手在打什么体系，比它场上那几张卡的名字
        更能决定"我现在该不该交阻抗"。
        """

        ids = [int(cid) for cid in card_ids if int(cid) > 0]
        if not ids:
            return []
        counts: Dict[int, int] = {}
        for cid in ids:
            for code in self._setcodes_of(cid):
                counts[code] = counts.get(code, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: -item[1])
        return [code for code, count in ranked if count >= 3][:2]

    def _setcodes_of(self, card_id: int) -> Tuple[int, ...]:
        """卡号 → 系列码（低 16 位）。系列码只在引擎卡库里，所以按需查一次并缓存。

        **逐卡一条固定语句**（``WHERE id = ?``），不做 ``IN (?,?,…)`` 那种动态占位符拼接——
        参数化要覆盖结构，不只是值。
        """

        cached = self._setcode_cache.get(card_id)
        if cached is not None:
            return cached
        result: Tuple[int, ...] = ()
        connection = self._open_cards_db()
        if connection is not None:
            row = connection.execute("SELECT setcode FROM datas WHERE id = ?", (int(card_id),)).fetchone()
            if row is not None and int(row[0] or 0):
                result = (int(row[0]) & 0xFFFF,)
        self._setcode_cache[card_id] = result
        return result

    def _open_cards_db(self) -> Optional[sqlite3.Connection]:
        """打开引擎卡库（只读、同样按线程私有）——系列码只在它里面。"""

        connection = getattr(self._local, "cards", None)
        if connection is not None:
            return connection
        for candidate in self._cards_db_candidates():
            if not candidate.is_file():
                continue
            try:
                connection = sqlite3.connect(
                    f"file:{candidate.as_posix()}?mode=ro", uri=True, check_same_thread=False
                )
            except sqlite3.Error:
                return None
            self._local.cards = connection
            self._cards_connection = connection
            return connection
        return None

    def _cards_db_candidates(self) -> List[Path]:
        """可能的 ``cards.cdb`` 位置。

        顺序：显式传入 > 数据目录旁的副本 > 环境变量 > **插件 config.toml 里配的卡库/引擎目录**
        （最后这条才是常态：引擎卡库在 ``paths.cards_cdb`` 或 ``paths.ygopro_dir`` 下）。
        """

        import os

        candidates: List[Path] = []
        if self._cards_db:
            candidates.append(Path(self._cards_db))
        for parent in (self._path.parent, self._path.parent.parent):
            candidates.append(parent / "cards.cdb")
        configured_env = os.environ.get("YGO_CARDS_CDB", "").strip()
        if configured_env:
            candidates.append(Path(configured_env))
        candidates.extend(self._configured_cards_db())
        return candidates

    def _configured_cards_db(self) -> List[Path]:
        """从插件 ``config.toml`` 读卡库位置（工具与插件都走这条）。"""

        import tomllib

        plugin_root = self._plugin_root or Path(__file__).resolve().parent.parent
        config = Path(plugin_root) / "config.toml"
        try:
            with config.open("rb") as handle:
                paths = tomllib.load(handle).get("paths") or {}
        except (OSError, ValueError):
            return []
        result: List[Path] = []
        cards_cdb = str(paths.get("cards_cdb") or "").strip()
        if cards_cdb:
            result.append(Path(cards_cdb))
        ygopro_dir = str(paths.get("ygopro_dir") or "").strip()
        if ygopro_dir:
            result.append(Path(ygopro_dir) / "cards.cdb")
        return result
