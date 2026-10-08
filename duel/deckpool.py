"""卡组池：保存 bot 能用的卡组，并在开局时随机抽一副。

池子有两类卡组：

* **内置卡组**（``BUILTIN_GROUP``）：WindBot 自带的那批卡组，随插件启动登记进来，
  自带配套的 Executor，牌力最稳，是 bot 的默认牌库。
* **投稿卡组**：群友在群里发卡组码，插件解析成 .ydk 落到数据目录，并按群隔离地记账。

开局时从「随机池」里抽一副给 bot 用——随机池是池子的子集，默认放全部内置卡组，
投稿卡组要显式 ``/加入随机`` 才参与抽取，这样默认行为是「只用内置卡组」。

卡组文件写在插件的持久数据目录（``ctx.paths.data_dir``）下，不写卡组码原文以外的任何东西；
数据库只存元数据与 .ydk 路径，便于群友查看、替换与删除自己的投稿。

**关于出牌策略的诚实说明**：WindBot 的棋力来自它为每个卡组准备的 Executor，而投稿卡组
基本不会有对应的 Executor。所以记录里同时保存一个「风格卡组名」（``windbot_deck``），
由配置决定；bot 会用它那套出牌思路去操作投稿的卡表，水平可能明显不如原卡组。这一点在
投稿与开局的回复里都会如实告知，不假装 bot 会玩这套卡组。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import logging
import random
import re
import sqlite3
import time
import uuid


_LOGGER = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decks (
    deck_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    contributor_id TEXT NOT NULL,
    contributor_name TEXT NOT NULL,
    ydk_path TEXT NOT NULL,
    deck_code TEXT NOT NULL,
    source_format TEXT NOT NULL,
    main_count INTEGER NOT NULL,
    extra_count INTEGER NOT NULL,
    side_count INTEGER NOT NULL,
    windbot_deck TEXT NOT NULL,
    generated_script TEXT NOT NULL DEFAULT '',
    picked_style TEXT NOT NULL DEFAULT '',
    in_random INTEGER NOT NULL DEFAULT 1,
    brain_scope TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decks_group ON decks (group_id);
CREATE TABLE IF NOT EXISTS group_settings (
    group_id TEXT PRIMARY KEY,
    fixed_deck_id INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# 读取时必须显式列出列名：老库是用 ALTER TABLE 补的列，物理列序与新库不同，
# 用 SELECT * 按位置取值会读串（这个坑被迁移测试抓到过）。
# ⚠ 这份顺序必须与 `_row_to_deck` 的下标一一对应（brain_scope 在最后）。
_DECK_COLUMNS = (
    "deck_id, group_id, display_name, contributor_id, contributor_name, ydk_path, "
    "source_format, main_count, extra_count, side_count, windbot_deck, generated_script, "
    "picked_style, in_random, created_at, brain_scope"
)

# 内置卡组统一挂在这个保留群号下，对每个群都可见
BUILTIN_GROUP = "__builtin__"

# 固定卡组存在 settings 表里的键名（全局一份，见 DeckPool.set_fixed_deck）
_FIXED_DECK_KEY = "fixed_deck_id"

#: 群号当目录名时的白名单：字母/数字/下划线/横线（本机见过的群号都是十六进制哈希或纯数字）
_GROUP_DIR_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _group_dir_name(group_id: str) -> str:
    """把群号变成安全的目录名（白名单之外的字符一律换成下划线）。

    ⚠ 别直接拿群号拼路径：`group_id` 来自平台消息，理论上可能带 `/`、`\\` 或 `..`，
    拼进 ``decks/<group_id>/`` 就能把投稿的 .ydk（连同它的卡表内容）写到数据目录外面去。
    """

    text = str(group_id)
    if _GROUP_DIR_OK.match(text):
        return text
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", text)[:64]
    return cleaned or "group"


class DeckPoolError(RuntimeError):
    """卡组池操作失败时抛出。"""


@dataclass(frozen=True)
class StoredDeck:
    """池子里的一副卡组。"""

    deck_id: int
    group_id: str
    display_name: str
    contributor_id: str
    contributor_name: str
    ydk_path: Path
    source_format: str
    main_count: int
    extra_count: int
    side_count: int
    windbot_deck: str
    generated_script: str
    picked_style: str = ""
    """给这副牌挑定的出牌脚本名（自带的十份执行器就是靠它与卡表对上）。

    与 :attr:`generated_script` 分开存：一个是我们用**现成执行器**量出来的，一个是别人
    现写并编译进去的，可信度不一样；两个都空就按卡表相似度挑、挑不到用通用脚本。
    """
    in_random: bool = True
    created_at: float = 0.0
    brain_scope: str = ""
    """这副牌的 AI 决策档位：``""`` 跟随全局配置，``off`` / ``target_only`` / ``full`` 覆盖它。

    档位挂在卡组上（卡组页每副牌一个设置），取值到"开哪半决策层"的映射在
    ``plugin.py`` 的 ``_BRAIN_SCOPE_SWITCHES``。
    """

    @property
    def is_builtin(self) -> bool:
        """是否是 WindBot 自带卡组。"""

        return self.group_id == BUILTIN_GROUP

    def describe(self) -> str:
        """人类可读的一行描述。"""

        return (
            f"{self.display_name}（主{self.main_count}/额外{self.extra_count}/副{self.side_count}，"
            f"投稿人 {self.contributor_name}）"
        )


class DeckPool:
    """按群隔离的卡组池。

    Args:
        data_dir: 插件的持久数据目录。
        default_windbot_deck: 未指定时使用的 WindBot 风格卡组名。
    """

    def __init__(self, data_dir: Path, *, default_windbot_deck: str = "DoEveryThing") -> None:
        self._data_dir = Path(data_dir)
        self._decks_dir = self._data_dir / "decks"
        self._decks_dir.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._data_dir / "deck_pool.db")
        self._connection.executescript(_SCHEMA)
        self._migrate_schema()
        self._migrate_fixed_deck()
        self._connection.commit()
        self._default_windbot_deck = default_windbot_deck

    @staticmethod
    def canonical_group(group_id: str) -> str:
        """把群号归一到**库里存的那份**（＝目录名用的白名单形式，见 :func:`_group_dir_name`）。

        ⚠ 库里 `decks.group_id` 存的是归一后的值（`add` 写的就是它），所以任何"拿调用方的群号
        去跟库里的行比"的地方都必须先过一次这里。不过这一关的话：群号里带 `/`、`:`、`..`
        这类字符时，`/清空卡组` 会一副都匹配不到（静默报"已清空 0 副"，看着像没人投过稿），
        `/删卡组` 的回执会把本群的投稿说成"来自别的群"（2026-10-07 评审指出）。
        删文件那侧只会少删、不会多删，但两边的口径必须一致。
        """

        return _group_dir_name(group_id)

    def add(
        self,
        *,
        group_id: str,
        display_name: str,
        contributor_id: str,
        contributor_name: str,
        ydk_text: str,
        deck_code: str,
        source_format: str,
        main_count: int,
        extra_count: int,
        side_count: int,
        windbot_deck: str = "",
    ) -> StoredDeck:
        """把一副卡组写进池子。

        Args:
            ydk_text: 已渲染好的 .ydk 文本。
            deck_code: 群友提供的原始卡组码，保存下来便于核对。
            windbot_deck: 风格卡组名，留空则用配置里的默认值。

        新收下的投稿不进随机池：默认只让 bot 用内置卡组，投稿要群友自己
        ``/加入随机 <编号>`` 才会被抽到。
        """

        group_key = _group_dir_name(group_id)
        target_dir = self._decks_dir / group_key
        target_dir.mkdir(parents=True, exist_ok=True)
        # 文件名带上随机后缀，避免同名投稿互相覆盖
        ydk_path = target_dir / f"{uuid.uuid4().hex[:12]}.ydk"
        ydk_path.write_text(ydk_text, encoding="utf-8")

        cursor = self._connection.execute(
            """
            INSERT INTO decks (
                group_id, display_name, contributor_id, contributor_name, ydk_path,
                deck_code, source_format, main_count, extra_count, side_count,
                windbot_deck, in_random, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                group_key,
                display_name,
                contributor_id,
                contributor_name,
                str(ydk_path),
                deck_code,
                source_format,
                main_count,
                extra_count,
                side_count,
                windbot_deck or self._default_windbot_deck,
                time.time(),
            ),
        )
        self._connection.commit()
        deck_id = int(cursor.lastrowid or 0)
        return StoredDeck(
            deck_id=deck_id,
            group_id=group_key,
            display_name=display_name,
            contributor_id=contributor_id,
            contributor_name=contributor_name,
            ydk_path=ydk_path,
            source_format=source_format,
            main_count=main_count,
            extra_count=extra_count,
            side_count=side_count,
            windbot_deck=windbot_deck or self._default_windbot_deck,
            generated_script="",
            in_random=False,
            created_at=time.time(),
            brain_scope="",
        )

    def list_decks(self, group_id: str) -> List[StoredDeck]:
        """列出某个群可见的全部卡组：内置卡组在前，本群投稿在后。

        固定卡组可能不是本群的（例如在私聊里固定的内置卡组），那也照样列在最后，
        否则群友在 ``/卡组列表`` 里看不到自己固定的是哪一副。列表编号按这个顺序排，
        所以「追加」不会打乱已有编号。
        """

        decks = self._visible_decks(group_id)
        fixed = self.fixed_deck()
        if fixed is not None and all(deck.deck_id != fixed.deck_id for deck in decks):
            decks = decks + [fixed]
        return decks

    def own_decks(self, group_id: str) -> List[StoredDeck]:
        """取"非内置"的卡组（不含内置）。

        ``group_id`` 只为兼容调用方保留、不再参与过滤：卡组池是**全局共享**的（见
        :meth:`_visible_decks` 的说明），投稿属于哪一群只作来源记录。
        """

        del group_id
        rows = self._connection.execute(
            f"SELECT {_DECK_COLUMNS} FROM decks WHERE group_id != ? ORDER BY deck_id",
            (BUILTIN_GROUP,),
        ).fetchall()
        return [self._row_to_deck(row) for row in rows]

    def random_pool(self, group_id: str) -> List[StoredDeck]:
        """取当前进入随机池的卡组（内置卡组默认都在，投稿要自己加进来）。

        只看本群可见的那些：固定卡组若属于别的群，不会因为被固定就混进本群的随机池。
        """

        return [deck for deck in self._visible_decks(group_id) if deck.in_random]

    def _visible_decks(self, group_id: str) -> List[StoredDeck]:
        """所有可用的卡组：全部内置 + 全部投稿，内置在前。

        **卡组池不再按群隔离**（用户的明确要求）：一台机器上的机器人只有一个牌库，
        谁投稿的都该能被抽到、能在任何群和私聊里选用；按群隔离只会带来"私聊里找不到
        卡组""别的群投稿的用不了"这类麻烦。来源群号仍然记着（列表里能看到），
        但不再参与可见性判断。
        """

        del group_id
        rows = self._connection.execute(
            f"SELECT {_DECK_COLUMNS} FROM decks "
            "ORDER BY CASE group_id WHEN ? THEN 0 ELSE 1 END, deck_id",
            (BUILTIN_GROUP,),
        ).fetchall()
        return [self._row_to_deck(row) for row in rows]

    def set_in_random(self, deck_id: int, in_random: bool) -> bool:
        """把一副卡组加入/移出随机池，返回是否真的改到了东西。"""

        cursor = self._connection.execute(
            "UPDATE decks SET in_random = ? WHERE deck_id = ?",
            (1 if in_random else 0, int(deck_id)),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def set_brain_scope(self, deck_id: int, scope: Optional[str]) -> bool:
        """按卡组写「AI 决策档位」，返回是否真的改到了东西。

        2026-10-09 用户口径：档位记在**卡组**上（面板卡组页每副牌一个设置）——
        一个全局开关满足不了"有的牌脚本就够、有的牌非要 AI 才动得起来"。

        取值（与面板上那两个控件一一对应）：``""`` 跟随全局（默认）、``off`` 不问、
        ``target_only`` 只问"该指哪只怪"（阻抗层的目标选择）、``full`` 目标 + 要不要交都问。
        认不出的值**原样存进去、对局时按"跟随全局"处理**：老库里可能存着当年那套
        `all` / `high_stakes` / `interrupt_only` 的旧口径，不该因为它让插件报错。
        """

        cursor = self._connection.execute(
            "UPDATE decks SET brain_scope = ? WHERE deck_id = ?",
            (str(scope or ""), int(deck_id)),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def seed_builtin_decks(self, entries: List[Tuple[str, str, Path]]) -> Tuple[int, int]:
        """把 WindBot 自带卡组登记进池子（幂等）。

        Args:
            entries: ``[(出牌思路名, 中文名, .ydk 路径)]``，通常来自
                :func:`duel.builtin_decks.builtin_decks`。

        Returns:
            ``(新增/更新的数量, 清理掉的数量)``——清理的是已不在本机的内置卡组
            （例如换了 WindBot 版本后消失的那些）。

        内置卡组登记时默认就在随机池里（它们配着自带 Executor，牌力最稳）；
        已有的记录只在名字或 .ydk 路径变了的时候才动，群友用 ``/移出随机``
        做过的取舍不会被下次启动覆盖掉。
        """

        kept_styles = set()
        changed = 0
        now = time.time()
        for style, display_name, ydk_path in entries:
            kept_styles.add(style)
            existing = self._connection.execute(
                "SELECT deck_id, display_name, ydk_path FROM decks WHERE group_id = ? AND windbot_deck = ?",
                (BUILTIN_GROUP, style),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO decks (
                        group_id, display_name, contributor_id, contributor_name, ydk_path,
                        deck_code, source_format, main_count, extra_count, side_count,
                        windbot_deck, generated_script, in_random, created_at
                    ) VALUES (?, ?, '', 'WindBot 自带', ?, '', 'builtin', 0, 0, 0, ?, '', 1, ?)
                    """,
                    (BUILTIN_GROUP, display_name, str(ydk_path), style, now),
                )
                changed += 1
            # 名字表会随版本更新，.ydk 路径也会随 WindBot 目录变化，所以按实际值对齐
            elif existing[1] != display_name or existing[2] != str(ydk_path):
                self._connection.execute(
                    "UPDATE decks SET display_name = ?, ydk_path = ? WHERE deck_id = ?",
                    (display_name, str(ydk_path), int(existing[0])),
                )
                changed += 1

        removed = 0
        stale = self._connection.execute(
            "SELECT deck_id, windbot_deck FROM decks WHERE group_id = ?", (BUILTIN_GROUP,)
        ).fetchall()
        for deck_id, style in stale:
            if style not in kept_styles:
                self._connection.execute("DELETE FROM decks WHERE deck_id = ?", (int(deck_id),))
                removed += 1
        self._connection.commit()
        return changed, removed

    def count(self, group_id: str) -> int:
        """投稿数量（全局，不再按群分——卡组池是共享的）。"""

        (total,) = self._connection.execute(
            "SELECT COUNT(*) FROM decks WHERE group_id != ?", (BUILTIN_GROUP,)
        ).fetchone()
        return int(total)

    def count_all(self, group_id: str) -> int:
        """本群可见的卡组总数（内置 + 本群投稿）。"""

        return len(self.list_decks(group_id))

    def count_in_random(self, group_id: str) -> int:
        """随机池里的卡组数量。"""

        return len(self.random_pool(group_id))

    def random_pick(self, group_id: str, *, rng: Optional[random.Random] = None) -> Optional[StoredDeck]:
        """从某个群的池子里随机抽一副，池子为空时返回 None。

        这里刻意使用普通的 ``random`` 而不是 ``secrets``：抽卡组是游戏行为，需要的是
        「每次不一样」，不涉及任何凭据或不可预测性要求。``rng`` 参数只为测试注入固定种子。
        """

        decks = self.random_pool(group_id)
        if not decks:
            return None
        chooser = rng or random.Random()
        return chooser.choice(decks)

    def _migrate_schema(self) -> None:
        """给已存在的库补上后加的列。

        ``CREATE TABLE IF NOT EXISTS`` 不会改动已有表，所以新增字段必须自己迁移；
        这里按列名逐个检查，缺什么补什么。
        """

        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(decks)")}
        if "generated_script" not in columns:
            self._connection.execute(
                "ALTER TABLE decks ADD COLUMN generated_script TEXT NOT NULL DEFAULT ''"
            )
        if "picked_style" not in columns:
            # 实测挑出来的出牌脚本（见 tools/pick_style.py）。与"生成脚本"分开存：
            # 一个是我们用现成执行器量出来的，一个是模型现写的，可信度不一样。
            self._connection.execute(
                "ALTER TABLE decks ADD COLUMN picked_style TEXT NOT NULL DEFAULT ''"
            )
        if "in_random" not in columns:
            self._connection.execute(
                "ALTER TABLE decks ADD COLUMN in_random INTEGER NOT NULL DEFAULT 1"
            )
        if "brain_scope" not in columns:
            # 这副牌开不开 AI 决策、开到哪一档（见 plugin.py 的 `_BRAIN_SCOPE_SWITCHES`）。
            # ⚠ 这一列必须保证存在：卡组面板与训练面板都要读它，而读的时候写的是
            # `SELECT ... brain_scope ...`——列不存在会抛 sqlite3.Error，
            # 上层按"查询失败就当没有卡组"处理，于是面板静默变成一副牌都没有。
            self._connection.execute(
                "ALTER TABLE decks ADD COLUMN brain_scope TEXT NOT NULL DEFAULT ''"
            )
        # ⚠ 老库里可能还留着 `playbook` 列（打法数据已按 2026-10-07 用户口径删除）。
        # 列留着不影响读：读取时是显式列名，多的列没人碰；新库不建这一列。

    def _migrate_fixed_deck(self) -> None:
        """把老库里的「按群固定卡组」搬成全局固定值。

        早期版本把固定卡组存在 ``group_settings`` 里、按群生效；现在改成全局一份。
        只有一个群设过的时候直接沿用那个值，省得群友重新固定一次；设过好几个不同值的
        情况无法一一保留，就留空让群友重新固定。``group_settings`` 表保留不动
        （老库里有数据，删表属于不必要的破坏），只是不再读了。
        """

        if self.get_setting(_FIXED_DECK_KEY) is not None:
            return
        rows = [
            int(row[0])
            for row in self._connection.execute(
                "SELECT DISTINCT fixed_deck_id FROM group_settings WHERE fixed_deck_id IS NOT NULL"
            )
        ]
        if len(rows) == 1:
            self.set_setting(_FIXED_DECK_KEY, str(rows[0]))

    def set_generated_script(self, deck_id: int, style_name: Optional[str]) -> None:
        """记录/清除某副卡组的专属脚本名（由模型生成并编译出来的那个）。"""

        self._connection.execute(
            "UPDATE decks SET generated_script = ? WHERE deck_id = ?",
            (style_name or "", int(deck_id)),
        )
        self._connection.commit()

    def set_picked_style(self, deck_id: int, style_name: Optional[str]) -> None:
        """记录/清除"实测挑出来的出牌脚本"（见 ``tools/pick_style.py``）。"""

        self._connection.execute(
            "UPDATE decks SET picked_style = ? WHERE deck_id = ?",
            (style_name or "", int(deck_id)),
        )
        self._connection.commit()

    # ⚠ 这里原来有 `set_playbook`（按卡组写"打法数据"，给计划感知执行器读）。
    # 打法数据 / AI 打牌链路已按 2026-10-07 用户口径删除，没有调用方了，方法删掉；
    # 表里的 `playbook` 列与 `StoredDeck.playbook` 字段**照旧保留**（不动表结构、不做迁移）。

    def pick_for_duel(self, group_id: str, *, rng: Optional[random.Random] = None) -> Optional[StoredDeck]:
        """开局选一副卡组：设了固定卡组就用它（全局生效），否则从本群随机池里抽。"""

        fixed = self.fixed_deck()
        if fixed is not None:
            return fixed
        return self.random_pick(group_id, rng=rng)

    def set_fixed_deck(self, deck_id: Optional[int]) -> None:
        """设定/取消固定卡组。

        Args:
            deck_id: 卡组编号；传 None 表示恢复随机抽取。

        固定卡组是**全局一份**（存 ``settings`` 表），理由有两条：内置卡组本来就是
        所有群共享的，「固定」这个说法在群友心里也是「让麦麦一直用这副」；而且在私聊里
        发过 ``/固定卡组`` 之后，群里开局也该认——按群各存一份时这条不成立，实测踩过。
        """

        self.set_setting(_FIXED_DECK_KEY, None if deck_id is None else str(int(deck_id)))

    def fixed_deck(self) -> Optional[StoredDeck]:
        """取当前固定的卡组；没设或那副已被删除时返回 None。"""

        raw = self.get_setting(_FIXED_DECK_KEY)
        if raw is None:
            return None
        try:
            deck_id = int(raw)
        except ValueError:
            # 值被手改坏了：直接清掉，别让它一直选不出卡组
            self.set_setting(_FIXED_DECK_KEY, None)
            return None
        # 不按群过滤：固定卡组可以是内置卡组（挂在保留群号下），也可以是别群的投稿
        deck_row = self._connection.execute(
            f"SELECT {_DECK_COLUMNS} FROM decks WHERE deck_id = ?", (deck_id,)
        ).fetchone()
        if deck_row is None:
            # 固定卡组被删掉了：顺手清掉这个悬空设置，避免一直选不到卡组
            self.set_setting(_FIXED_DECK_KEY, None)
            return None
        return self._row_to_deck(deck_row)

    def delete_all(self, group_id: str) -> int:
        """清空**本群**的投稿（不动内置卡组、也不动别群的投稿），返回删掉的副数。

        ⚠ 这里以前是"文件按全池清、SQL 只删本群"：`own_decks()` 返回的是**全池**投稿（卡组池是
        全局共享的，见 :meth:`own_decks`），于是别群的 .ydk 文件被 unlink 了、数据库里的行却还在，
        变成指向已删文件的悬空记录——之后随机抽到那副牌，机器人一张都出不了牌，而且**不报错**
        （2026-10-07 评审指出）。现在文件与记录按同一个范围删，两边数量必然对得上。

        语义上选"只清本群"而不是"清空整个池子"：清空是破坏性操作，别群辛苦投的牌不该被
        不相干的管理员一键清掉；卡组池共享只影响"看得到、用得上"，删除范围仍然按来源群。

        ⚠ 群号要先过 :meth:`canonical_group`：库里存的是归一后的值，拿原始群号去比会一副都匹配不到
        （群号带 `/`、`:`、`..` 时），于是静默返回 0——看着像"本群没人投过稿"（2026-10-07 评审指出）。

        ⚠ 顺序是**先删记录、再清文件**（2026-10-07 评审指出）：反过来的话，unlink 到一半失败
        （Windows 上文件被别的进程占着就会 PermissionError）会让一部分 .ydk 已经消失、记录却还在，
        又回到上面那个悬空引用的坑。现在最坏情况只是留下几个没人引用的孤儿 .ydk。
        """

        group_key = self.canonical_group(group_id)
        decks = [deck for deck in self.own_decks(group_key) if deck.group_id == group_key]
        if not decks:
            return 0
        removed_ids = tuple(deck.deck_id for deck in decks)
        placeholders = ", ".join("?" for _ in removed_ids)
        self._connection.execute(
            f"DELETE FROM decks WHERE deck_id IN ({placeholders})", removed_ids
        )
        self._connection.commit()
        for deck in decks:
            try:
                deck.ydk_path.unlink(missing_ok=True)
            except OSError as exc:
                # 记录已经删了，删不掉的文件只是孤儿，不影响任何对局，记一条日志就够了
                _LOGGER.warning("清空投稿：%s 删不掉，留下孤儿文件：%s", deck.ydk_path, exc)
        # 清掉的投稿里可能有当前固定的那副，交给 fixed_deck() 自己发现悬空并清设置
        return len(decks)

    def get_setting(self, key: str) -> Optional[str]:
        """读取一个全局设置。"""

        row = self._connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    def set_setting(self, key: str, value: Optional[str]) -> None:
        """写入一个全局设置；value 传 None 表示删除。"""

        if value is None:
            self._connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self._connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self._connection.commit()

    def remove(self, group_id: str, deck_id: int) -> bool:
        """删除一副投稿（连 .ydk 一起清掉），返回是否删到了东西。

        内置卡组不属于任何群，也不能删——真删了会把它的 .ydk 文件一起清掉，
        那是 WindBot 自己的文件。

        ``group_id`` 只为兼容调用方保留、不再参与判断：卡组池是共享的，
        在哪个群/私聊里删同一副牌都是一回事。
        """

        del group_id
        row = self._connection.execute(
            "SELECT group_id FROM decks WHERE deck_id = ?", (int(deck_id),)
        ).fetchone()
        if row is not None and str(row[0]) == BUILTIN_GROUP:
            raise DeckPoolError("内置卡组不能删除，只能把它移出随机池")

        row = self._connection.execute(
            "SELECT ydk_path FROM decks WHERE deck_id = ?", (int(deck_id),)
        ).fetchone()
        if row is None:
            return False
        self._connection.execute("DELETE FROM decks WHERE deck_id = ?", (int(deck_id),))
        self._connection.commit()
        try:
            Path(row[0]).unlink(missing_ok=True)
        except OSError as exc:
            raise DeckPoolError(f"卡组记录已删除，但 .ydk 文件清理失败：{exc}") from exc
        # 删掉的正好是当前的固定卡组时，同步取消固定，避免留下悬空引用
        if self.get_setting(_FIXED_DECK_KEY) == str(int(deck_id)):
            self.set_setting(_FIXED_DECK_KEY, None)
        return True

    def close(self) -> None:
        """关闭数据库连接。Windows 上不关会导致数据目录无法清理。"""

        self._connection.close()

    @staticmethod
    def _row_to_deck(row: Sequence[object]) -> StoredDeck:
        """把一行记录转成 :class:`StoredDeck`。"""

        return StoredDeck(
            deck_id=int(row[0]),  # type: ignore[arg-type]
            group_id=str(row[1]),
            display_name=str(row[2]),
            contributor_id=str(row[3]),
            contributor_name=str(row[4]),
            ydk_path=Path(str(row[5])),
            source_format=str(row[6]),
            main_count=int(row[7]),  # type: ignore[arg-type]
            extra_count=int(row[8]),  # type: ignore[arg-type]
            side_count=int(row[9]),  # type: ignore[arg-type]
            windbot_deck=str(row[10]),
            generated_script=str(row[11] or ""),
            picked_style=str(row[12] or ""),
            in_random=bool(row[13]),
            created_at=float(row[14]),  # type: ignore[arg-type]
            brain_scope=str(row[15] or ""),
        )
