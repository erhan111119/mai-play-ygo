"""卡牌数据库（cards.cdb）读取。

ygopro 生态的 cards.cdb 是 SQLite 数据库：``texts`` 表放卡名与效果文本，``datas`` 表放
类型、攻防、等级这些数值。这里提供两件事：把卡 ID 翻成卡名（播报用），以及取出卡牌详情
（给模型写出牌脚本时当素材用，效果文本直接从本地库读，不必上网找）。

数据库路径由插件配置提供（通常就是 ygopro 目录下的 cards.cdb）。文件缺失或表结构
不符合预期时会明确报错，而不是返回空名字把问题掩盖成「未知卡」。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

import sqlite3
import threading


# 卡类型位掩码，取自 WindBot 的 YGOSharp.OCGWrapper.Enums/CardType
CARD_TYPE_MONSTER = 0x1
CARD_TYPE_SPELL = 0x2
CARD_TYPE_TRAP = 0x4

# 位 -> 中文名，按「先大类、再细分」的顺序拼
_TYPE_LABELS = (
    (0x1, "怪兽"),
    (0x2, "魔法"),
    (0x4, "陷阱"),
    (0x10, "通常"),
    (0x20, "效果"),
    (0x40, "融合"),
    (0x80, "仪式"),
    (0x100, "陷阱怪兽"),
    (0x200, "灵魂"),
    (0x400, "同盟"),
    (0x800, "二重"),
    (0x1000, "调整"),
    (0x2000, "同调"),
    (0x4000, "衍生物"),
    (0x10000, "速攻"),
    (0x20000, "永续"),
    (0x40000, "装备"),
    (0x80000, "场地"),
    (0x100000, "反击"),
    (0x200000, "反转"),
    (0x400000, "卡通"),
    (0x800000, "超量"),
    (0x1000000, "灵摆"),
    (0x4000000, "连接"),
)


@dataclass(frozen=True)
class CardDetail:
    """一张卡的详情，供生成出牌脚本时当素材。"""

    card_id: int
    name: str
    type_text: str
    stats: str
    effect: str
    atk: Optional[int] = None
    """**卡面**攻击力（怪兽才有，其余是 None）。

    与 :attr:`stats` 分开留着：`stats` 是给人看的一行字（"攻1800/守1200/星4"），
    要拿数值的地方（对局页的攻守角标）不该去解析那行字——原来面板读的是
    `detail.atk`，而这个字段压根不存在，于是场上永远没有攻守。
    """

    defense: Optional[int] = None
    """**卡面**守备力（怪兽才有）。"""

    level: int = 0
    """星级/阶级（魔陷为 0）。"""

    @property
    def is_monster(self) -> bool:
        """是否是怪兽卡（按类型文本判断）。"""

        return "怪兽" in self.type_text


def decode_card_type(card_type: int) -> str:
    """把卡类型位掩码翻成中文，例如 ``怪兽 效果 同调``。"""

    if not card_type:
        return ""
    labels = [label for bit, label in _TYPE_LABELS if card_type & bit]
    return " ".join(labels)


def _clean_effect_text(desc: str, name: str) -> str:
    """清掉效果文本里的冗余内容，压成一行便于放进提示词。

    cdb 里的 desc 以卡名开头，后面才是效果；再往后是「卡片密码」之类的附加行，都是噪音。
    """

    text = desc.strip()
    if text.startswith(name):
        text = text[len(name) :]
    for marker in ("卡片密码", "卡包", "罕贵度", "Card Password"):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
    return " ".join(text.split())


class CardDatabaseError(RuntimeError):
    """卡牌数据库不可用时抛出。"""


class CardDatabase:
    """cards.cdb 的只读封装，带进程内缓存。

    **连接按线程私有**：这个对象是全局共享的（插件一个、对局记录器也持有），而查询发生在
    不同线程里——对局记录器的报文回调跑在**闸门自己的线程**里（每条连接一个），插件主线程也会查。
    SQLite 连接不能跨线程用，所以每个线程各开一条只读连接；不共享就不需要加锁。

    为什么把这段写这么清楚：实测踩过——``sqlite3.ProgrammingError: SQLite objects created in a
    thread can only be used in that same thread``，表现是**每次"某张卡被除外/被破坏"的记录都抛异常**
    （日志里刷满「报文观测回调失败」），而对局本身还在继续，外面看起来就像"突然卡住"。
    """

    def __init__(self, cdb_path: Optional[Union[str, Path]]) -> None:
        self._path = Path(cdb_path) if cdb_path else None
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []
        """开过的连接（每线程一条），关闭时统一收尾。

        不这样做的话，Windows 上 cards.cdb 会被一直占着（连删除都不行）。
        """
        self._cache: Dict[int, str] = {}

    @property
    def available(self) -> bool:
        """数据库是否可用。配置里没给路径时视为不可用。"""

        return self._path is not None

    @property
    def path(self) -> Optional[Path]:
        """数据库文件路径。"""

        return self._path

    def _ensure_connection(self) -> sqlite3.Connection:
        """取**本线程**的只读连接（第一次用到时建立，并校验表结构）。"""

        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        if self._path is None:
            raise CardDatabaseError("配置里没有指定 cards.cdb 路径")
        if not self._path.is_file():
            raise CardDatabaseError(f"cards.cdb 不存在：{self._path}")

        # 用 URI 打开成只读，避免误改用户客户端自带的数据库
        connection = sqlite3.connect(
            f"file:{self._path.as_posix()}?mode=ro", uri=True, check_same_thread=False
        )
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise CardDatabaseError(f"cards.cdb 不是有效的 SQLite 数据库：{exc}") from exc

        if "texts" not in tables:
            connection.close()
            raise CardDatabaseError(
                f"cards.cdb 里没有 texts 表，实际包含的表：{sorted(tables)}"
            )
        self._local.connection = connection
        self._connections.append(connection)
        return connection

    def name(self, card_id: int) -> Optional[str]:
        """查询卡名，查不到返回 None。

        Args:
            card_id: 卡牌 ID（卡组码与报文里的编号）。
        """

        if card_id in self._cache:
            return self._cache[card_id]
        if not self.available:
            return None

        connection = self._ensure_connection()
        row = connection.execute("SELECT name FROM texts WHERE id = ?", (card_id,)).fetchone()
        name = row[0] if row and row[0] else None
        if name:
            self._cache[card_id] = name
        return name

    def describe(self, card_id: int) -> str:
        """给出「卡名」；查不到时退化成带编号的占位文本，方便排查。"""

        if card_id <= 0:
            return ""
        name = self.name(card_id)
        if name:
            return f"「{name}」"
        return f"未知卡({card_id})"

    def warm_up(self, card_ids: List[int]) -> int:
        """批量预热缓存，返回成功查到名字的数量。"""

        found = 0
        for card_id in card_ids:
            if self.name(card_id):
                found += 1
        return found

    def card_details(self, card_ids: List[int]) -> Dict[int, CardDetail]:
        """批量取出卡牌详情（卡名、类型、攻防、效果文本）。

        生成出牌脚本时要靠这些信息写 CardId 常量与 combo；``datas`` 表缺行时该卡会被跳过，
        由调用方决定怎么处理（例如提示"本地卡库不认识这些卡"）。
        """

        if not self.available:
            return {}
        connection = self._ensure_connection()
        details: Dict[int, CardDetail] = {}
        for card_id in dict.fromkeys(card_ids):
            row = connection.execute(
                "SELECT t.name, t.desc, d.type, d.atk, d.def, d.level "
                "FROM texts AS t LEFT JOIN datas AS d ON t.id = d.id WHERE t.id = ?",
                (card_id,),
            ).fetchone()
            if row is None or not row[0]:
                continue
            name, desc, card_type, atk, defence, level = row
            type_text = decode_card_type(int(card_type or 0))
            stats = ""
            is_monster = type_text.startswith("怪兽")
            if is_monster:
                stats = f"攻{atk or 0}/守{defence or 0}/星{level or 0}"
            elif type_text.startswith(("魔法", "陷阱")) and level:
                # 灵摆魔法的等级字段是刻度，这里不展示，免得和怪兽星级混淆
                stats = ""
            effect = _clean_effect_text(str(desc or ""), name)
            details[card_id] = CardDetail(
                card_id=card_id,
                name=name,
                type_text=type_text,
                stats=stats,
                effect=effect,
                # 数值另存一份：面板画攻守角标要用数字，不能去解析上面那行给人看的字
                atk=int(atk) if is_monster and atk is not None else None,
                defense=int(defence) if is_monster and defence is not None else None,
                level=int(level or 0) if is_monster else 0,
            )
        return details

    def unknown_ids(self, card_ids: List[int]) -> List[int]:
        """找出本地卡库里查不到的卡 ID（去重、保持出现顺序）。

        这个检查是「投稿卡组能不能打」的关键：WindBot 靠 cards.cdb 理解卡牌，
        卡库里没有的卡它完全不认识，会表现为**每回合空过、一张都不出**。
        数据库不可用时返回空列表（不阻断流程，由调用方决定怎么提示）。
        """

        if not self.available:
            return []
        unknown: List[int] = []
        seen = set()
        for card_id in card_ids:
            if card_id in seen:
                continue
            seen.add(card_id)
            if self.name(card_id) is None:
                unknown.append(card_id)
        return unknown

    def close(self) -> None:
        """关闭**所有线程**开过的连接，可重复调用。

        Windows 上只要连接没关，cards.cdb 就会一直被占用（连删除都不行），
        所以插件卸载时必须调用到这里，或直接把它当作上下文管理器使用。
        """

        for connection in self._connections:
            try:
                connection.close()
            except sqlite3.Error:
                continue
        self._connections.clear()
        self._local = threading.local()

    def __enter__(self) -> "CardDatabase":
        """支持 ``with CardDatabase(path) as db:`` 写法，退出时自动关闭。"""

        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """退出上下文时关闭连接。"""

        self.close()
