"""麦麦玩游戏王（Mai Play YGO）。

一个插件把"打牌"和"查牌"两件事都包了——原「游戏王对局管家」与「游戏王百科检索」
两个插件合并而来，v1.0.0 起对外只有这一个插件：

**对局侧**：群里有人说想打牌 → LLM 调用本插件的工具 → 插件自己起一个对局房间 →
把服务器地址与房间密码发到群里 → 群友用 MDPro3 / YGOMobile 连进来和机器人打一局 →
打完后把结果与过程复述发回群里，并写进 Maisaka 上下文让机器人后续聊天时知道刚才发生了什么。

**百科侧**（`wiki.py` 的 `YugiohWikiTools` 混入）：三个 LLM 工具——查卡、解析卡组码、
发卡图；解析卡组码优先用本机 `cards.cdb`（离线、快），卡图优先用本机客户端的图。

**两个虚拟客户端是一体的**：对局内核 `ygopro.exe` 与出牌大脑 `WindBot.exe`
都放在插件自己的 `clients/` 目录里（默认配置直接指向它们），拷到哪台机器都能跑。

架构上分四层，都在 ``duel/`` 目录里：

* :mod:`duel.room` —— 起 ``ygopro.exe``（一进程一房间）与 ``WindBot.exe``（bot 的出牌大脑）；
* :mod:`duel.gate` —— 对外唯一入口：校验房间口令、双向透传、旁路观测报文；
* :mod:`duel.recorder` —— 从报文流里统计胜负与过程要点；
* :mod:`duel.session` —— 把上面三者与卡组串成一局。

**关于「发送房间信息」的一个取舍**：房间地址与口令由插件直接发到群里，而不是交给 LLM 转述。
口令是精确字符串，经过一次模型转述就有被写错的风险，而写错口令会让群友连不上房间。
工具的返回值里会附带同样的信息，供模型叙述时引用。

**关于 bot 棋力的诚实说明**：bot 由 WindBot 代打。WindBot 的棋力来自它为特定卡组准备的
出牌脚本，而群友投稿的卡组通常没有对应脚本，所以它会用配置里指定的「风格卡组」思路去操作
投稿的卡表，水平可能明显不如原卡组。这一点在投稿回复与开局回复里都会如实说明。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

import asyncio
import dataclasses
import logging
import random
import re
import sys
import time

from .duel.builtin_decks import builtin_decks
from .duel.cards import CardDatabase, CardDatabaseError
from .duel.knowledge import DecisionLog, DuelTrace, Knowledge, duel_outcome_text
from .duel.deckcode import Deck, DeckCodeError, deck_summary, describe_issues, parse_deck_code
from .duel.deckpool import BUILTIN_GROUP, DeckPool, DeckPoolError, StoredDeck
from .duel.field_image import (
    BOARD_HEIGHT,
    BOARD_WIDTH,
    build_html,
    view_from_state,
)
from .duel.netutil import detect_lan_address, format_endpoint
from .duel.room import ProcessError, RoomSettings
from .duel.windbot_decks import (
    AUTO_DECK_STYLE,
    GENERIC_DECK_STYLE,
    GENERIC_STYLE_NAME,
    load_available_decks,
    pick_best_match,
)
from .wiki import YugiohWikiTools
from .duel.session import (
    OUTCOME_ABORTED,
    OUTCOME_FINISHED,
    OUTCOME_NO_PLAYER,
    OUTCOME_TIMEOUT,
    DuelSession,
    SessionConfig,
    SessionInfo,
)


# 工具名统一前缀，避免与其它插件的工具重名（宿主只保留同名组件的第一个）
TOOL_START = "ygo_duel_start"
TOOL_LIST_DECKS = "ygo_deck_list"
TOOL_STATUS = "ygo_duel_status"
TOOL_STOP = "ygo_duel_stop"

# 支持的客户端平台
PLATFORM_MDPRO3 = "mdpro3"
PLATFORM_YGOMOBILE = "ygomobile"

# 插件私有设置里，「游戏内名字」覆盖值用的键名
SETTING_BOT_NAME = "in_game_bot_name"

# 默认出牌风格卡组。必须是 WindBot 注册表里的名字（即各 Executor 上的 [Deck("名字", ...)]），
# 写错不会有任何报错，WindBot 会静默换成随机卡组——所以这里用已验证存在的名字。
DEFAULT_WINDBOT_DECK = "Blue-Eyes"

# 常驻房（duel.persist_room）：插件启动就把「ygopro + WindBot」拉起来并保持。
# 用一个不会和真实群/聊天流撞车的假 stream_id / group_id 走现有的房间与落库链路
#（落在 `arena="room"`，和"群里那局"同一张表，但名字一眼能认出来）。
PERSIST_STREAM = "__persist_room__"
PERSIST_GROUP = "__persist_room__"
# 一局打完（或起崩）之后隔多久再开下一副。给收尾留一点时间，别把端口抢在同一秒。
PERSIST_RETRY_SECONDS = 5.0

# 计划感知执行器：通用打法 + 会读"作战计划 / 卡组打法数据 / 逐步问 AI"三份文件的那一个。
# 开了"逐步问 AI"就必须用它——别的脚本不读问答文件。
PLAN_AWARE_STYLE = "PlanAware"

# 对局总结（`duel.summarize_with_ai`）让模型润色时给的输出额度。
# 值给得大是有实测原因的：宿主那只用于该任务的模型会先"想"一大段，额度给小了
# 思考就把它吃光、正文空着回来，看起来像"模型不回话"（2026-10-06 群里就是这个问题）。
SUMMARY_MAX_TOKENS = 16384

# 百科检索的默认查卡接口（ygocdb 的搜索接口；换自建/镜像时改配置 `wiki.endpoint`）
WIKI_DEFAULT_ENDPOINT = "https://ygocdb.com/api/v0/"

# 对局总结的内置提示词（`duel.summary_prompt` 留空时用它；配置里写了就整套换成用户那份）。
# 可用占位符：self_name / report / verdict / winner / turns。
#
# 这几句约束不是装饰，都是踩出来的：
# * 「保留这些事实」「不要复述字段名」——原始复述是「回合数 4 / 动作 12 比 9」那种字段列表，
#   直接发群里很生硬，这活就是让模型把它写成一句人话；
# * 「胜负以这句为准，不要自己推断」——模型推反过胜负，所以胜负那一行由插件写在最前面，
#   这里再压一遍，避免它复述时写反；
# * 「（我方）/（对方）」的说明——记录里是这两个词，模型不知道"我方"指谁，会写错人称。
DEFAULT_SUMMARY_PROMPT = (
    "你是群里的游戏王玩家「{self_name}」，刚和群友打完一局。请把下面这份对局记录写成"
    "**一条**要发到群里的播报，用你自己的语气，口语化、有点小得意或不服气都可以，"
    "但必须保留这些事实：谁赢了、怎么赢的、总共几个回合、双方谁更活跃。"
    "不要用列表或标题，不要复述原文的字段名，控制在 120 字以内。\n\n"
    "对局记录：\n{report}\n\n"
    "（胜负：{verdict}，回合数：{turns}）"
    "\n注意：记录里带「（我方）」的就是你自己（{self_name}），带「（对方）」的是对手，"
    "胜负以上面这句为准，不要自己推断。"
)

# 按卡组的问 AI 档位：键是写进卡组库的值，值是（给群里看的名字, 给模型的 scope）。
# 空串＝跟随全局配置（`duel.brain_scope`）——老库补列后默认就是空，行为不变。
BRAIN_MODES: Dict[str, Tuple[str, str]] = {
    "": ("跟随全局", ""),
    "off": ("不问 AI（只用脚本）", "off"),
    "high_stakes": ("只问高压（交坑/打谁/这局走哪条线）", "high_stakes"),
    "interrupt_only": ("只应对不掌舵（连每一步做什么也交回脚本）", "interrupt_only"),
    "all": ("每一问都问", "all"),
}
"""按卡组的问 AI 档位（``/出牌模式`` 改的就是它）。

**为什么要有**：实测"脚本本来就能打"的牌（升辉月跑自带的 ``Lucky``）开着问 AI 反而少打动作
（16 局：特召 4.4 → 3.6、还多出空过局），而"脚本一步都走不出来"的牌全靠 AI 才动得起来。
一个全局开关满足不了两种牌，所以档位记在卡组上；空串＝跟随全局（默认，行为与以前一致）。
"""

BRAIN_MODE_ALIASES: Dict[str, str] = {
    "不问": "off",
    "关": "off",
    "关闭": "off",
    "只用脚本": "off",
    "问": "all",
    "开": "all",
    "每问都问": "all",
    "全问": "all",
    "高压": "high_stakes",
    "只看高压": "high_stakes",
    "只应对": "interrupt_only",
    "只应对不掌舵": "interrupt_only",
    "跟随": "",
    "跟随全局": "",
    "默认": "",
    "全局": "",
}

# 插件根目录：/训练 要按绝对路径拉起 tools/train_arena.py（它是独立进程）
_PLUGIN_ROOT = Path(__file__).resolve().parent

# 「/训练」的轮数：每轮 2 局镜像（两边各坐一次先攻），所以 20 轮 = 40 局
DEFAULT_TRAINING_ROUNDS = 20
MAX_TRAINING_ROUNDS = 100


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class PathsConfig(PluginConfigBase):
    """外部程序与数据文件路径。

    **所有路径都支持"相对于插件目录"的写法**（推荐）：默认值就是插件自带的
    `clients/ygopro` 与 `clients/windbot`——插件把两个虚拟客户端（对局内核 ygopro +
    出牌大脑 WindBot）放在自己目录里，拷到哪台机器都能跑，不用再改绝对路径。
    填绝对路径也照旧认（老配置直接能用）。
    """

    __ui_label__ = "运行环境"
    __ui_icon__ = "folder"
    __ui_order__ = 1

    ygopro_executable: str = Field(
        default="clients/ygopro/ygopro.exe",
        description="ygopro.exe 路径（插件自带的在 clients/ygopro/；必须是用服务端模式构建的版本）",
    )
    ygopro_dir: str = Field(
        default="clients/ygopro",
        description="ygopro 工作目录，需要包含 cards.cdb、lflist.conf、script/ 与 expansions/",
    )
    windbot_executable: str = Field(
        default="clients/windbot/WindBot.exe",
        description="WindBot.exe 路径（出牌大脑；插件自带的在 clients/windbot/）",
    )
    windbot_dir: str = Field(
        default="clients/windbot",
        description="WindBot 工作目录，需要包含 Decks/ 与 x64/sqlite3.dll（插件运行时会往里写卡表与计划文件）",
    )
    cards_cdb: str = Field(
        default="clients/ygopro/cards.cdb",
        description="cards.cdb 路径；留空则用 ygopro 工作目录下的同名文件",
    )
    card_art_dir: str = Field(
        default="clients/art/Art",
        description="卡图目录（查房出图与发卡图用；缺图会退到 card_art_fallback_dir，再缺就画卡名框）",
    )
    card_art_fallback_dir: str = Field(
        default="clients/art/Closeup",
        description="备用卡图目录（大图；Art 里没有的卡常在这里能找到）",
    )
    windbot_src_dir: str = Field(
        default="",
        description=(
            "WindBot **源码树**目录（含 WindBot.csproj）。**可选**：填了它插件才能为投稿卡组生成"
            "专属出牌脚本并编译（出牌脚本是编译进 exe 的）；不填则投稿卡组走通用脚本"
        ),
    )

    # ---- 路径解析（相对路径 = 相对插件目录）--------------------------------

    @staticmethod
    def _resolve(raw: str) -> Optional[Path]:
        """把配置里的路径解析成绝对路径（相对路径一律相对插件目录）。

        空值返回 ``None``——**不能返回 `Path("")`**：那是 `Path(".")`（当前工作目录），
        会把"没配"悄悄当成"插件目录/宿主进程目录"，排查起来极难。
        """

        text = (raw or "").strip()
        if not text:
            return None
        path = Path(text)
        return path if path.is_absolute() else (_PLUGIN_ROOT / path)

    def resolved_ygopro_executable(self) -> Optional[Path]:
        """ygopro.exe 的绝对路径（没配就是 None）。"""

        return self._resolve(self.ygopro_executable)

    def resolved_ygopro_dir(self) -> Optional[Path]:
        """ygopro 工作目录的绝对路径（没配就是 None）。"""

        return self._resolve(self.ygopro_dir)

    def resolved_windbot_executable(self) -> Optional[Path]:
        """WindBot.exe 的绝对路径（没配就是 None）。"""

        return self._resolve(self.windbot_executable)

    def resolved_windbot_dir(self) -> Optional[Path]:
        """WindBot 工作目录的绝对路径（没配就是 None）。"""

        return self._resolve(self.windbot_dir)

    def resolved_cards_cdb(self) -> Optional[Path]:
        """cards.cdb 的绝对路径（留空则用 ygopro 目录下的同名文件；都没有就是 None）。"""

        if (self.cards_cdb or "").strip():
            return self._resolve(self.cards_cdb)
        ygopro_dir = self.resolved_ygopro_dir()
        return (ygopro_dir / "cards.cdb") if ygopro_dir is not None else None

    def resolved_windbot_src_dir(self) -> Optional[Path]:
        """WindBot 源码树的绝对路径（没配就是 None）。"""

        return self._resolve(self.windbot_src_dir)

    def resolved_card_art_dir(self) -> Path:
        """主卡图目录（没配就用插件自带的 `clients/art/Art`）。"""

        return self._resolve(self.card_art_dir) or (_PLUGIN_ROOT / "clients" / "art" / "Art")

    def resolved_card_art_fallback_dir(self) -> Path:
        """备用卡图目录（没配就用插件自带的 `clients/art/Closeup`）。"""

        return self._resolve(self.card_art_fallback_dir) or (
            _PLUGIN_ROOT / "clients" / "art" / "Closeup"
        )


# ---------------------------------------------------------------------------
# 对局配置的"可见面"（2026-10-07 用户口径）见下方 `DuelConfig` 定义之后。
# ---------------------------------------------------------------------------


class WikiConfig(PluginConfigBase):
    """百科检索（原「游戏王百科检索」插件）。

    **只有两项**（2026-10-07 用户口径：百科侧只要"查询地址 + 超时时间"）：
    地址换自建/镜像站点时改 `endpoint`，网络慢时改 `timeout`。
    其余都写死在代码里——结果条数上限 5（`wiki.py` 的 `MAX_SEARCH_RESULTS`）、
    查询缓存 1 小时（`CACHE_TTL_SECONDS`）、卡图默认发（本地优先，缺了才走 CDN）。
    """

    __ui_label__ = "百科检索"
    __ui_icon__ = "search"
    __ui_order__ = 4

    endpoint: str = Field(
        default=WIKI_DEFAULT_ENDPOINT,
        description=(
            "查卡接口地址（ygocdb 兼容的 `/api/v0/?search=<关键词或卡号>`）。"
            "换成自建或镜像站点时改这里；地址要和官方的一致，末尾带斜杠"
        ),
    )
    timeout: int = Field(default=15, description="在线接口超时时间（秒）：查卡、取卡图都用它")


class DuelConfig(PluginConfigBase):
    """对局与网络配置。"""

    __ui_label__ = "对局"
    __ui_icon__ = "swords"
    __ui_order__ = 2

    bot_name: str = Field(default="麦麦", description="bot 在对局中显示的名字")
    windbot_deck: str = Field(
        default=GENERIC_DECK_STYLE,
        description=(
            "bot 的出牌思路。generic（默认）用 WindBot 的通用脚本，喂任意卡表都能打；"
            "auto 表示按卡表相似度挑一套原型脚本——实测原型脚本按自己的卡表写死 combo，"
            "换成群友投稿的卡表会整局一张都不出，所以不建议对投稿卡组用它；"
            "也可以写死一个名字（如 Blue-Eyes / Albaz / Kashtira / Labrynth），必须是 WindBot "
            "注册表里的名字——写错不报错，但 bot 会静默换成随机卡组"
        ),
    )
    in_game_chat: bool = Field(
        default=False,
        description="机器人是否在游戏内说话（WindBot 自带的固化台词）。默认关闭，保持对局干净",
    )
    # 这里原来有三项：`auto_generate_script` / `search_endpoint` / `search_allow_private_host`
    # （导入卡组后让模型写 C# 出牌脚本、以及为此查打法要点用的搜索服务）。
    # **2026-10-07 用户口径：插件不再自动写脚本**——新导入的卡组一律用通用脚本，
    # 想要专属脚本改用 AI agent 按 executors/README.md 多轮迭代着写。
    # 三个字段连同它们唯一的消费者（`DeckScriptGenerator` / `_web_search`）一起去掉了。
    deck_unknown_tolerance: float = Field(
        default=0.25,
        description=(
            "投稿卡组里「本地卡库不认识的卡」占比超过这个值时拒绝收录（0~1，默认 0.25）。"
            "WindBot 靠 cards.cdb 理解卡牌，卡库不认识的卡它完全不会用，超过这个比例就会出现"
            "「机器人整局一张都不出」的情况——与其收下来打不动，不如当场提示更新卡库"
        ),
    )
    dialog: str = Field(default="", description="WindBot 台词包名称，留空用默认")
    bot_debug: bool = Field(default=False, description="是否把 WindBot 的对局日志打到插件日志里")
    public_host: str = Field(
        default="",
        description=(
            "发给群友的服务器地址，留空则自动探测本机局域网地址。"
            "做内网穿透时填隧道域名或公网地址"
        ),
    )
    public_port: int = Field(
        default=0,
        description=(
            "发给群友的端口，0 表示直接用闸门监听的端口。"
            "内网穿透把公网端口映射到不同本地端口时填这里（例如隧道公网端口 30123 "
            "转发到本地闸门端口 64400，就填 30123）"
        ),
    )
    listen_host: str = Field(default="0.0.0.0", description="闸门监听地址，默认监听所有网卡")
    listen_port: int = Field(default=0, description="闸门监听端口，0 表示随机分配一个空闲端口")
    persist_room: bool = Field(
        default=False,
        description=(
            "常驻房（**默认关闭**）：开启后插件一启动就拉起一副「ygopro + WindBot」并一直保持"
            "（一局打完自动再开一副、崩了自动重启），端口固定。"
            "关着的时候就是原来的行为：群里有人要打才开房。"
        ),
    )
    persist_room_port: int = Field(
        default=7801,
        description="常驻房固定端口（固定＝重启后地址不变，方便直接连；0 表示随机分配）",
    )
    join_timeout_seconds: int = Field(default=300, description="等群友进房间的秒数，超时自动收摊")
    max_duration_seconds: int = Field(default=3600, description="单局最长秒数，超时自动收摊")
    max_concurrent_rooms: int = Field(default=1, description="同时允许存在的房间数上限")
    time_limit: int = Field(default=180, description="传给内核的单回合思考时限（秒）")
    start_lp: int = Field(default=8000, description="初始生命值")
    start_hand: int = Field(default=5, description="初始手牌数")
    draw_count: int = Field(default=1, description="每回合抽卡数")
    no_check_deck: bool = Field(
        default=True,
        description="是否关闭卡组合法性检查。开启后投稿卡组不会被禁卡表挡在门外，适合娱乐局",
    )
    no_shuffle_deck: bool = Field(default=False, description="是否关闭开局洗切卡组")
    duel_rule: str = Field(default="", description="大师规则编号，留空使用内核默认值")
    save_replay: bool = Field(
        default=True,
        description="是否把录像写到 ygopro 目录的 replay/ 下，方便群友回看",
    )
    announce_result: bool = Field(
        default=True,
        description="打完之后是否把对局总结发到群里（默认开）",
    )
    summarize_with_ai: bool = Field(
        default=True,
        description=(
            "对局总结是否先交给模型写成一段人话再发。默认开：发出去的是麦麦口吻的一段话，"
            "而不是「回合数 / 双方统计」那样的字段列表。关掉或模型不可用时发原始复述"
        ),
    )
    inject_to_planner: bool = Field(
        default=True,
        description=(
            "是否把对局复述写进机器人上下文，让它后续聊天时知道这一局。"
            "注意这只影响「记忆」，播报本身由插件直接发出，不再经过 planner"
        ),
    )
    taunt_enabled: bool = Field(
        default=False,
        description=(
            "局内是否按概率说挑衅台词（**具体说什么**见下面的 `taunt_lines`）。"
            "默认关闭（用户 2026-10-07 要求：开房把地址与口令发出去之后就不再讲话）"
        ),
    )
    taunt_lines: List[str] = Field(
        default_factory=list,
        description=(
            "挑衅台词池（一条一句）。留空＝用内置的 20 句（`duel/taunts.py` 的 TAUNT_LINES）；"
            "填了就只用你写的这些（同样的台词不会连着说两次）。"
            "只在 `taunt_enabled = true` 时才用得上"
        ),
    )
    summary_prompt: str = Field(
        default="",
        description=(
            "【对局总结的提示词】留空＝用内置那份（要求 120 字、只许润色不许改胜负，见 README）。"
            "自己写可以用五个占位符（花括号包起来的英文名，完整清单与含义见 README）："
            "self_name 是 bot 在群里的名字、report 是对局记录原文、verdict 是机器判定的胜负、"
            "winner 是胜者名字、turns 是回合数。"
            "注意：胜负那一行由插件自己写在播报最前面，提示词改成什么都不影响它"
        ),
    )
    taunt_chance_per_second: float = Field(
        default=0.03,
        description=(
            "每秒说一句挑衅的概率：0.03 表示平均每 33 秒说一句。"
            "只在「对局已经开始且还没结束」时掷骰子，等人进房间时不会嘴炮"
        ),
    )
    invite_enabled: bool = Field(
        default=False,
        description=(
            "是否让机器人在空闲时**主动冒泡约战**：间隔到了就在最近一次开过房的群里问一句"
            "「要不要打一局」。默认关闭；开着才会按下面的间隔随机发问"
        ),
    )
    invite_min_minutes: int = Field(default=30, description="主动约战的最短间隔（分钟）")
    invite_max_minutes: int = Field(
        default=90, description="主动约战的最长间隔（分钟）；与最短值之间取随机数"
    )
    invite_text: str = Field(
        default="",
        description="主动约战的台词；留空用内置台词（「〈名字〉在，有人想打一局吗？说一声就开房」）",
    )
    ai_plan_coach: str = Field(
        default="off",
        description=(
            "让模型每回合给麦麦定一次战术：off（默认，用各卡组自带的出牌脚本）/ "
            "rule（规则教练）/ llm（模型教练）。"
            "开启后这一局改用「计划感知执行器」——它在通用脚本上加了一层战术偏置；"
            "实验阶段默认关闭：先在擂台测出它不弱于自带脚本，再考虑打开"
        ),
    )
    ai_brain: bool = Field(
        default=True,
        description=(
            "让模型参与出牌决策（AI 打牌）：除了回答「要不要发动/连锁」「打谁」，"
            "还会在主要阶段**从全部合法动作里挑这一步做什么**（召唤/特召/发动/盖放/进战斗/结束）——"
            "脚本写不出的新卡组（新系列、投稿卡组）就是靠这个动起来的"
            "（脚本层只能否决它自己想做的事，碰上它不认识的卡组会一步都走不出来）。"
            "问答通道挂在 WindBot 基类上，**任何卡组都能用**，不会因此换掉卡组自己的出牌脚本。"
            "模型走插件自带的 <数据目录>/brain_model.toml（密钥不进插件仓库，也不动宿主模型配置）。"
            "**代价**：每次决策一次模型调用（实测 deepseek-chat 约 0.6~0.9 秒），一局会因此慢几倍；"
            "每回合的等待总预算默认 25 秒（用完这一回合照脚本打）、每回合最多主动问 6 次。"
            "拿不到模型时不会开这条通道（免得不作答还拖慢每一局）"
        ),
    )
    brain_knowledge: bool = Field(
        default=True,
        description=(
            "问 AI 时是否**按当前局面检索知识库**（卡牌事实 / 我这副牌的线路 / 对手轴系的威胁）。"
            "知识库是本机 SQLite（<数据目录>/knowledge/knowledge.db，由 tools/build_card_facts.py "
            "与 tools/build_deck_plans.py 生成），检索是毫秒级、不花钱，所以默认开。"
            "关掉它就退回「只用攻略要点 + 打法数据」那套静态素材——擂台上的 A/B 就是这两档"
        ),
    )
    brain_scope: str = Field(
        default="all",
        description=(
            "问 AI 的范围：`all` = 每一问都问（历史行为）；`high_stakes` = 只问高压决策——"
            "**我的回合**（执行器写的 `my_phase=1`）不把「本家卡要不要发动」拿去问模型，交回出牌脚本，"
            "对手的回合照问（那才是「该不该交这个坑」），「这一步做什么」「打谁」不受影响；"
            "`interrupt_only` = 连「这一步做什么」也交回脚本，只留对手回合的应对与「打谁」。"
            "实测（80 局/腿）：不问 23.1 动作/局、每问都问 20.7、只问高压 21.0——"
            "只问高压确实拦下了 47% 的发动提问，但动作数没回来，所以嫌疑落在「这一步做什么」那个菜单上，"
            "`interrupt_only` 就是用来把两者分开的。"
            "翻默认值之前要按项目纪律跑 ≥80 局/腿（tools/brain_eval.py --brain-scope …）"
        ),
    )
    ai_deck_plan: bool = Field(
        default=True,
        description=(
            "导入卡组后**让模型按卡表写一份展开流程**（起手→步骤→终场→自肃→被断后），"
            "存进知识库；问 AI 的「这一步做什么」会把整份流程喂给模型。"
            "这就是「让新卡组一导入就会展开」的那一步：市场上没有覆盖所有系列的 combo 库，"
            "所以做成**按需生成 + 缓存**（一副牌只生成一次，几十秒，之后一直读缓存）。"
            "写失败不影响开局，最多少一份资料；卡号写错会被校验拦住并让它重写"
        ),
    )
    # 这里原来有两项：`script_max_tokens` / `script_model`（生成专属脚本的额度与模型）。
    # 脚本生成整条链路去掉了，但「对局总结交给模型润色」那一处**还要那只模型与足额 token**：
    # 现在直接写在 `_summarize_with_ai` 里（额度 = `SUMMARY_MAX_TOKENS`，模型留空 = 宿主给该任务配的），
    # 不再占一个配置项。
    train_model: str = Field(
        default="",
        description=(
            "【训练/评估用哪个模型】填宿主 model_config.toml 里的模型别名。"
            "训练默认不下计划（--coach none，跑的是卡组自带的出牌思路）；"
            "只有填了这个才让模型参与出牌决策（--coach llm --llm-model <别名>），"
            "所以训练速度与结果都由你选的模型决定。留空 = 纯脚本对打，不调模型"
        ),
    )


# ---------------------------------------------------------------------------
# 对局配置的"可见面"（2026-10-07 用户口径）
#
# 群主要配的就是下面这十项（开房地址与等人超时 / bot 名字 / 总结开关与提示词 /
# 挑衅开关与台词池）。其余字段**保留在模型里**（老配置文件里的键照旧能读、
# 代码里 `duel.xxx` 照旧可读），但在 WebUI 与配置模板里**隐藏**——把配置面收窄，
# 同时不给"删字段"引入迁移风险。
#
# 中文标签写在这里而不是每个 Field 上：一眼能看出"群主看到的配置长什么样"，
# 改可见面时只动这一处。
# ---------------------------------------------------------------------------
_VISIBLE_DUEL_FIELDS: Dict[str, str] = {
    "bot_name": "bot 名字",
    "public_host": "服务器地址",
    "public_port": "服务器端口",
    "listen_port": "本地端口",
    "join_timeout_seconds": "开房等人超时（秒）",
    "announce_result": "打完发总结",
    "summarize_with_ai": "总结交给 AI 润色",
    "summary_prompt": "总结的提示词",
    "taunt_enabled": "局内挑衅",
    "taunt_lines": "挑衅台词池",
}


def _apply_duel_config_surface() -> None:
    """给可见字段配中文标签，把其余字段标成 hidden（WebUI 与配置模板不再显示）。"""

    for name, field_info in DuelConfig.model_fields.items():
        extra = dict(field_info.json_schema_extra or {})
        label = _VISIBLE_DUEL_FIELDS.get(name)
        if label is None:
            extra["hidden"] = True
        else:
            extra["label"] = label
        field_info.json_schema_extra = extra


_apply_duel_config_surface()


class MaiPlayYgoConfig(PluginConfigBase):
    """插件配置总表（麦麦玩游戏王）。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    duel: DuelConfig = Field(default_factory=DuelConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)


class ActiveRoom:
    """一个进行中的房间及其完成通知任务。"""

    def __init__(
        self,
        session: DuelSession,
        stream_id: str,
        group_id: str,
        task: asyncio.Task,
        deck_name: str = "",
    ) -> None:
        self.session = session
        self.stream_id = stream_id
        self.group_id = group_id
        self.task = task
        self.deck_name = deck_name
        """这一局机器人用的卡组名。

        ``/查房`` 要跨所有对话流列房间，光报"某处有个房间"没有意义——
        群友问的是"谁在打、用的哪副牌"，所以创建房间时就把卡组名记在这里。
        """


class MaiPlayYgo(YugiohWikiTools, MaiBotPlugin):
    """麦麦玩游戏王：对局管家（开房/播报/查房/AI 教练）+ 百科检索（查卡/卡组码/卡图）。

    继承顺序有意为之：`YugiohWikiTools` 在前（它的 `@Tool` 方法与本体一起被
    `dir(instance)` 采集到），`MaiBotPlugin` 在后（提供 `ctx` / `config` / 生命周期）。
    """

    config_model = MaiPlayYgoConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        self._deck_pool: Optional[DeckPool] = None
        self._card_db: Optional[CardDatabase] = None
        self._knowledge_cache: Optional[Knowledge] = None
        """知识库缓存（懒加载；库不存在时保持 None）——见 :meth:`_knowledge`。"""
        self._rooms: Dict[str, ActiveRoom] = {}
        self._background_tasks: set = set()
        """插件自己起的后台任务（目前只有"导入卡组后写展开流程"这一种）。

        统一收在集合里是为了 shutdown 时能撤干净：任务里可能正在跑一支子进程。
        """
        self._training_task: Optional[asyncio.Task] = None
        """正在跑的「/训练」批（一次只允许一批：它要吃满 CPU）。"""
        self._optimize_task: Optional[asyncio.Task] = None
        """正在跑的「/优化卡组」批（同样吃满 CPU，与 /训练 互斥）。"""
        self._pick_task: Optional[asyncio.Task] = None
        """正在跑的「/挑脚本」批（要真的打对局，所以同样与训练/优化互斥）。"""
        self._playbook_task: Optional[asyncio.Task] = None
        """正在跑的「/写打法」批（只调模型，不占对局资源）。"""
        self._brain_tasks: Dict[str, Any] = {}
        """房间的"问 AI"答复任务：``{stream_id: (问答前缀, 任务, 服务端)}``，收摊时撤掉。"""
        self._logger: Optional[logging.Logger] = None

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """准备卡组池与卡牌数据库，并检查外部程序路径是否配好。"""

        self._logger = self.ctx.logger
        self._deck_pool = DeckPool(
            Path(self.ctx.paths.data_dir),
            default_windbot_deck=self.config.duel.windbot_deck or DEFAULT_WINDBOT_DECK,
        )
        self._card_db = CardDatabase(self._resolve_cards_cdb())
        self._seed_builtin_decks()
        # 主动约战：记住"最近一次开过房的群"，并起一个常驻循环（间隔与开关在循环里读，
        # 这样配置热更新后不用重启插件就能生效）
        self._invite_stream_id = ""
        self._invite_task: Optional[asyncio.Task] = asyncio.create_task(self._invite_loop())
        # 常驻房：见 `_persist_room_loop`。开关与端口都在循环里读配置，热更新即可生效。
        self._persist_room_task: Optional[asyncio.Task] = asyncio.create_task(
            self._persist_room_loop()
        )
        self._logger.info("游戏王对局管家已加载，数据目录 %s", self.ctx.paths.data_dir)
        # 把生效中的关键配置打进日志：内网穿透时"闸门端口是否真的固定了"全靠这一行确认
        self._log_effective_config()
        # 自定义的对局总结提示词先试渲染一次：占位符写错要现在就说，别等打完一局才发现没总结
        self._check_summary_prompt()
        missing = self._missing_paths()
        if missing:
            self._logger.warning(
                "以下路径尚未配置，对局功能不可用，请在插件配置页补全：%s", "；".join(missing)
            )

    def _log_effective_config(self) -> None:
        """把生效中的关键配置写进日志。

        配置改动要经过宿主推送或插件运行时重启才会生效，光看 config.toml 不能确定插件用的是哪份值，
        所以启动时把真正生效的值打出来，排查「改了配置没反应」时直接看这行。
        """

        if self._logger is None:
            return
        duel = self.config.duel
        listen_port = duel.listen_port or "随机分配"
        advertised_host = duel.public_host.strip() or "自动探测本机地址"
        advertised_port = duel.public_port or "同闸门端口"
        # auto 时要说清「挑不出相似卡组时用哪套」，否则日志只写 auto 看不出机器人实际会用什么
        style = duel.windbot_deck or "未设置"
        if style == AUTO_DECK_STYLE:
            style = f"{AUTO_DECK_STYLE}（按卡表相似度自动挑，挑不出时用 {self._fallback_deck_style()}）"
        self._logger.info(
            "对局配置：闸门监听 %s:%s；发给群友 %s:%s；出牌思路 %s；等群友 %s 秒",
            duel.listen_host,
            listen_port,
            advertised_host,
            advertised_port,
            style,
            duel.join_timeout_seconds,
        )
        self._logger.info(
            "对局玩法：游戏内名字 %s；游戏内说话 %s；总结交给模型 %s；局内挑衅 %s",
            self._resolve_bot_name(),
            "开" if duel.in_game_chat else "关",
            "开" if duel.summarize_with_ai else "关",
            f"开（每秒 {duel.taunt_chance_per_second:.0%}）" if duel.taunt_enabled else "关",
        )
        # 两处"可配置的文字"要说清用的是哪一份：配了没生效（或写坏了）时，看这行就知道
        self._logger.info(
            "文案：总结提示词 %s；挑衅台词池 %s",
            "自定义" if duel.summary_prompt.strip() else "内置",
            f"自定义（{len(duel.taunt_lines)} 句）" if duel.taunt_lines else "内置 20 句",
        )

    async def on_unload(self) -> None:
        """收摊：停掉所有房间，关闭数据库。

        **卸载必须快**：宿主给插件卸载的预算是 5 秒（实测超时会被记为
        ``plugin.shutdown 超时``，随后整个插件被重启，对局里的连接跟着断掉）。
        而优雅收尾要对 WindBot 与内核各等最多 10 秒，必然超时——所以卸载路径只做
        "立刻杀掉进程、关掉端口"这一件事（``kill_now`` 全程同步、不 await）。
        对局正常结束那条路仍然走 :meth:`DuelSession.stop` 的优雅收尾。
        """

        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

        if self._training_task is not None and not self._training_task.done():
            # 训练是独立进程，取消这个 task 不会杀掉子进程；显式 kill 一遍，别留下孤儿
            self._training_task.cancel()
            self._training_task = None
        if self._optimize_task is not None and not self._optimize_task.done():
            self._optimize_task.cancel()
            self._optimize_task = None
        if self._invite_task is not None and not self._invite_task.done():
            self._invite_task.cancel()
            self._invite_task = None
        if self._persist_room_task is not None and not self._persist_room_task.done():
            self._persist_room_task.cancel()
            self._persist_room_task = None

        for stream_id in list(self._rooms):
            room = self._rooms.pop(stream_id, None)
            if room is None:
                continue
            room.task.cancel()
            try:
                room.session.kill_now()
            except Exception:  # noqa: BLE001  卸载阶段必须尽力清理，不能中断后续步骤
                if self._logger is not None:
                    self._logger.exception("卸载时关闭房间失败：会话 %s", stream_id)
        if self._deck_pool is not None:
            self._deck_pool.close()
            self._deck_pool = None
        if self._card_db is not None:
            self._card_db.close()
            self._card_db = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新后重建卡牌数据库（路径可能变了）。"""

        del config_data
        del version
        if scope != "self":
            return
        if self._card_db is not None:
            self._card_db.close()
        self._card_db = CardDatabase(self._resolve_cards_cdb())
        if self._deck_pool is not None:
            self._deck_pool.close()
            self._deck_pool = DeckPool(
                Path(self.ctx.paths.data_dir),
                default_windbot_deck=self.config.duel.windbot_deck or DEFAULT_WINDBOT_DECK,
            )
            self._seed_builtin_decks()

    # ------------------------------------------------------------------ 工具

    @Tool(
        TOOL_START,
        description=(
            "开一局游戏王对战：先把房间建好，再把服务器地址与房间密码发到群里，"
            "群友用 MDPro3 或 YGOMobile 连进来和机器人打一局。"
            "当群聊里有人表示想打牌、想决斗、问有没有人来一局、想和机器人比一场时调用。"
            "**直接调用即可，不要先问对方用哪个客户端**——两种客户端的接入信息完全一样，"
            "开好房后的指引里会同时写明两者的加入步骤。"
            "如果对方已经说了用哪个客户端，就把它填进 platform；没说就留空。"
            "也不要在调用之前先回一句「我这就去建房间」——直接把房间建好再回话。"
            "有人表示想打就直接开房，**不要反问「要不要打」「你确定吗」这类确认问题**——"
            "房间建好、地址发出去之后再回话即可。"
        ),
        parameters=[
            ToolParameterInfo(
                name="platform",
                param_type=ToolParamType.STRING,
                description="对方明确说出的客户端；没说就留空，留空时指引里两种客户端都会写上",
                required=False,
                enum_values=[PLATFORM_MDPRO3, PLATFORM_YGOMOBILE],
            ),
            ToolParameterInfo(
                name="deck_name",
                param_type=ToolParamType.STRING,
                description="指定机器人用哪副卡组（内置卡组或群友投稿都行），留空表示从随机池抽一副",
                required=False,
            ),
        ],
        visibility="visible",
        chat_scope="all",
        timeout_ms=60000,
    )
    async def tool_start_duel(
        self, platform: str = "", deck_name: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """开一局对战。"""

        try:
            stream_id = str(kwargs.get("stream_id") or "")
            group_id = str(kwargs.get("group_id") or stream_id)
            if not stream_id:
                return self._tool_result(TOOL_START, "拿不到当前聊天流，无法开局。")
            # 模型有时会把「留空」写成字符串 "空"/"未指定"，一律按没填处理
            platform = platform.strip().lower()
            if platform not in (PLATFORM_MDPRO3, PLATFORM_YGOMOBILE):
                platform = ""

            problem = self._check_ready()
            if problem:
                return self._tool_result(TOOL_START, problem)

            if stream_id in self._rooms:
                return self._tool_result(
                    TOOL_START, "这个群已经有一局在进行了，先打完或者用取消工具收摊。"
                )
            if len(self._rooms) >= max(self.config.duel.max_concurrent_rooms, 1):
                return self._tool_result(
                    TOOL_START, f"当前已有 {len(self._rooms)} 个房间在跑，达到上限，等一局结束再来。"
                )

            # **固定卡组优先于模型填的 `deck_name`**：用户实测"我固定的是卡通，打出来却是升辉月"——
            # `deck_name` 这个工具参数是**模型按上下文自己填的**（描述里写着"指定机器人用哪副卡组"），
            # 它会顺手上把上一轮聊过的卡组填进来，而 `_pick_deck` 里"传了名字就用名字"把固定设置静默盖掉。
            # 固定是用户用 `/固定卡组 <编号>` 明确设的（对所有群生效），要换就再发一次那条命令（`/固定卡组 随机` 回随机池），
            # 所以这里以固定为准；只在**没有固定**时才让 `deck_name` 生效（保留"这局用某副"的用法）。
            if deck_name.strip() and self._deck_pool is not None:
                pinned = self._deck_pool.fixed_deck()
                if pinned is not None:
                    if self._logger is not None:
                        self._logger.info(
                            "已固定「%s」，忽略模型传入的 deck_name=%s（要换发 /固定卡组 <编号>）",
                            pinned.display_name,
                            deck_name,
                        )
                    deck_name = ""

            deck, deck_note = self._pick_deck(group_id, deck_name)
            # 这一行是排查"bot 进房不准备/一步不走"的唯一线索：出错时只会看到 WindBot 抛异常，
            # 而"它用了哪副牌、哪个脚本、哪份 exe"决定了能不能复现（实测排查花了很久）
            if self._logger is not None:
                exe = self._resolve_windbot_executable()
                if deck is None:
                    self._logger.info(
                        "本局机器人用默认卡组：脚本 %s｜exe %s", DEFAULT_WINDBOT_DECK, exe
                    )
                else:
                    self._logger.info(
                        "本局机器人用「%s」：脚本 %s｜卡表 %s｜exe %s",
                        deck.display_name,
                        deck.windbot_deck,
                        deck.ydk_path,
                        exe,
                    )
            # 逐步问 AI（可选）：起一个"答复"任务，并把问答前缀交给这一局的 WindBot
            # （把这副牌一起传进去：提示词里要带上它的打法数据）
            brain_prefix, brain_task = self._start_room_brain(stream_id, deck)
            config = self._build_session_config(stream_id)
            if brain_prefix is not None:
                config = dataclasses.replace(config, brain_file=brain_prefix)
            session = DuelSession(
                config,
                group_id=group_id,
                human_name_hint=str(kwargs.get("user_nickname") or ""),
                deck=deck,
                card_db=self._card_db,
                logger=self._logger,
            )
            try:
                info = await session.start()
            except ProcessError as exc:
                await session.stop()
                return self._tool_result(
                    TOOL_START,
                    f"房间开不起来：{exc}。请检查插件配置里的 ygopro 路径与工作目录。",
                )

            task = asyncio.create_task(self._run_room(session, stream_id, group_id))
            # 记住这个群：主动约战只在"开过房、拿得到聊天流"的群里说话
            self._invite_stream_id = stream_id
            # 卡组名一并记进房间：`/查房` 跨群列房间时要能说清每个房间是哪副牌
            # （没找到投稿卡组时用的是机器人自带卡组，与上面的日志同一口径）
            deck_label = deck.display_name if deck is not None else DEFAULT_WINDBOT_DECK
            self._rooms[stream_id] = ActiveRoom(
                session, stream_id, group_id, task, deck_name=deck_label
            )

            announcement = self._compose_open_message(
                info, platform, deck_note if deck is None else ""
            )
            # 房间信息由插件直接发出，避免口令经过模型转述被写错
            await self.ctx.send.text(announcement, stream_id)
            # 用哪副牌**不再播报**（每次开房都提示一次太吵，牌在 /卡组详情 里能看）；
            # 但"随机池是空的 / 没找到叫 X 的卡组"这类说明要留着——它告诉群友怎么把牌放进池子
            return self._tool_result(
                TOOL_START,
                f"房间已经开好，地址与口令已经发到群里了：\n{announcement}\n"
                "请提醒群友按上面的信息连进来，打完我会播报结果。",
            )
        except Exception as exc:  # noqa: BLE001  工具层必须把失败原因交回给模型
            if self._logger is not None:
                self._logger.exception("开局失败")
            return self._tool_result(TOOL_START, f"开局时出错：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 收卡组的共用实现

    def _store_deck_from_code(
        self,
        *,
        group_id: str,
        deck_code: str,
        deck_name: str,
        user_id: str,
        user_nickname: str,
        stream_id: str = "",
    ) -> str:
        """解析并收录一副投稿卡组，返回给群里的回执文本。

        工具与 ``/加卡组`` 指令共用这里，避免两条入口的行为漂移。
        （原来还有一条入口：模型看到群里贴卡组码就调 `ygo_deck_submit` 自动收录——已按
        2026-10-07 用户口径去掉，现在**只有指令能投稿**。）

        ``stream_id`` 只用来给"展开流程写好了"的后续播报定位会话；指令调用里它可能与
        ``group_id`` 是同一个值，也可能为空（那就在调用处退到 ``group_id``）。
        """

        if self._deck_pool is None:
            return "卡组池还没准备好，请稍后再试。"
        if not deck_code.strip():
            return "没有拿到卡组码内容。"
        try:
            deck = parse_deck_code(deck_code)
        except DeckCodeError as exc:
            return f"这段卡组码没读懂：{exc}"

        issues = describe_issues(deck)
        if issues:
            return "这副卡组不能直接用于对战：" + "；".join(issues) + "。请确认卡组码是否完整。"

        # 卡库核对：本地认不出的卡，WindBot 也认不出，会表现为「机器人一张都不出」。
        # 与其收下来打不动作，不如在这里就说清楚。
        unknown_all = self._unknown_cards(deck)
        if unknown_all is None:
            unknown_note = "（本地卡库不可用，没能核对这副牌是否被卡库收录）"
        else:
            total = len(set(deck.main) | set(deck.extra))
            ratio = len(unknown_all) / total if total else 0.0
            tolerance = float(self.config.duel.deck_unknown_tolerance)
            if ratio > tolerance:
                sample = "、".join(self._lookup_card_name(cid) or f"#{cid}" for cid in unknown_all[:6])
                return (
                    f"这副卡组有 {len(unknown_all)}/{total} 张卡本地卡库不认识（例如 {sample}…），"
                    "机器人会认不出这些卡、整局都出不了手。"
                    "请先更新本地卡库（cards.cdb）与卡牌脚本再投稿，或者换一副本地认得的卡组。"
                )
            unknown_note = (
                f"（其中 {len(unknown_all)} 张本地卡库不认识，这些卡的效果可能不生效）"
                if unknown_all
                else "（卡库核对通过，全部卡都能识别）"
            )

        display_name = deck_name.strip() or self._auto_deck_name(deck)
        style, style_note = self._deck_style_for(deck.main, deck.extra)
        stored = self._deck_pool.add(
            group_id=group_id,
            display_name=display_name,
            contributor_id=user_id,
            contributor_name=user_nickname or "群友",
            ydk_text=deck.to_ydk(display_name),
            deck_code=deck_code.strip(),
            source_format=deck.source_format,
            main_count=len(deck.main),
            extra_count=len(deck.extra),
            side_count=len(deck.side),
            windbot_deck=style,
        )
        lines = [
            f"已收下「{stored.display_name}」：{deck_summary(deck, card_lookup=self._lookup_card_name)}，"
            f"投稿人 {stored.contributor_name}",
            style_note,
            unknown_note,
            f"本群现在有 {self._deck_pool.count(group_id)} 副投稿卡组，"
            f"卡组池里还有 {len(self._deck_pool.list_decks(group_id))} 副可选（含 WindBot 内置卡组）",
        ]
        if deck.ambiguous:
            lines.append("（这段码是按萌卡格式解析的；如果不对，请把完整分享链接再发一次）")
        index = self._deck_index(group_id, stored.deck_id)
        lines.append(
            f"默认开局只用内置卡组；想让机器人随机用到这副，发 /加入随机 {index}。"
            "想看全部牌发 /卡组列表，想固定用某副发 /固定卡组 <编号>"
        )
        # **不再自动写脚本**（2026-10-07 用户口径）：新导入的卡组一律用 WindBot 的通用脚本。
        # 想让它打得更强，用 AI agent 给这副牌写一个专属执行器（多轮迭代找问题、改、再测），
        # 装法见 executors/README.md——插件自己不动手写 C#，也不编译。
        lines.append(
            "这副牌会先用 WindBot 的通用脚本打；想让它更强可以按 executors/README.md "
            "让 agent 写一个专属出牌脚本（需要多轮迭代）。"
        )
        # 展开流程（combo 线）：只是让模型按卡表写一份人话资料存进知识库，问 AI 时会喂给它；
        # 与"写出牌脚本"无关（那个已经不再由插件做了）。失败也不影响开局，最多少一份资料。
        if self.config.duel.ai_deck_plan and self.config.duel.ai_brain:
            self._make_deck_plan_handler(stream_id or group_id, group_id)(stored)
            lines.append("顺便给它写一份展开流程（问 AI 时会喂给模型），好了我告诉你。")
        return "\n".join(lines)

    # ⚠ 这里原来有一个 `ygo_deck_submit` 工具（让模型在群里看到卡组码就自动收进卡组池）。
    # **2026-10-07 用户口径：去掉自动导入，卡组码只能用指令投稿**——所以整块删掉了。
    # 投稿走 `/加卡组 <卡组码>`（内部复用 `_store_deck_from_code`，解析/校验/卡库核对都在那一份）。

    @Tool(
        TOOL_LIST_DECKS,
        description=(
            "查看本群可用的卡组：WindBot 自带的内置卡组与群友投稿，并标出哪些在随机池里、"
            "哪副是当前固定使用的。"
        ),
        parameters=[],
        visibility="visible",
        chat_scope="all",
    )
    async def tool_list_decks(self, **kwargs: Any) -> Dict[str, Any]:
        """列出本群卡组池。"""

        if self._deck_pool is None:
            return self._tool_result(TOOL_LIST_DECKS, "卡组池还没准备好。")
        stream_id = str(kwargs.get("stream_id") or "")
        group_id = str(kwargs.get("group_id") or stream_id)
        decks = self._deck_pool.list_decks(group_id)
        if not decks:
            return self._tool_result(
                TOOL_LIST_DECKS,
                "卡组池是空的——通常是没配 WindBot 工作目录，或那个目录里没有 Decks/。"
                "群友也可以发卡组码，发 /加卡组 <卡组码> 就能收下。",
            )
        fixed = self._deck_pool.fixed_deck()
        in_random = self._deck_pool.count_in_random(group_id)
        lines = []
        for index, deck in enumerate(decks):
            marks = []
            if deck.in_random:
                marks.append("随机池")
            if fixed is not None and fixed.deck_id == deck.deck_id:
                marks.append("当前固定")
            if deck.is_builtin:
                marks.append("内置")
            suffix = f"（{'、'.join(marks)}）" if marks else "（不在随机池）"
            lines.append(f"{index + 1}. {deck.describe()}{suffix}")
        return self._tool_result(
            TOOL_LIST_DECKS,
            f"卡组池共 {len(decks)} 副，其中 {in_random} 副在随机池：\n" + "\n".join(lines),
        )

    @Tool(
        TOOL_STATUS,
        description="查看当前游戏王对局的状态：房间地址、机器人是否就位、群友是否进来了、打到哪一步。",
        parameters=[],
        visibility="visible",
        chat_scope="all",
    )
    async def tool_status(self, **kwargs: Any) -> Dict[str, Any]:
        """汇报当前对局状态。"""

        stream_id = str(kwargs.get("stream_id") or "")
        room = self._rooms.get(stream_id)
        if room is None:
            return self._tool_result(TOOL_STATUS, "现在没有进行中的对局。")
        info = room.session.info
        gate = room.session.gate
        if info is None or gate is None:
            return self._tool_result(TOOL_STATUS, "房间正在启动中，稍等一下。")

        status = gate.status
        endpoint = format_endpoint(info.host, info.advertised_port)
        if info.advertised_port != info.port:
            # 内网穿透场景：对外端口与本地闸门端口不同，两个都要说清楚
            parts = [f"房间（对外）：{endpoint}", f"本机闸门端口：{info.port}"]
        else:
            parts = [f"房间：{endpoint}"]
        parts.append(f"机器人已就位：{'是' if status.bot_connected else '否'}")
        humans = status.human_clients
        if humans:
            parts.append("已进入的群友：" + "、".join(client.describe() for client in humans))
        else:
            parts.append("还没有群友进来")
        parts.append(f"对局已开始：{'是' if status.duel_started else '否'}")
        parts.append(f"对局已结束：{'是' if status.duel_finished else '否'}")
        if status.rejected_count:
            parts.append(f"口令错误的连接：{status.rejected_count} 次")
        return self._tool_result(TOOL_STATUS, "；".join(parts))

    @Tool(
        TOOL_STOP,
        description=(
            "结束并关闭当前房间（群友不来了、要取消这一局、或者房间卡住时用）。"
            "当群里有人说算了不打了、取消对局、或者要重开一局时调用。"
        ),
        parameters=[],
        visibility="visible",
        chat_scope="all",
    )
    async def tool_stop_duel(self, **kwargs: Any) -> Dict[str, Any]:
        """收摊当前房间。"""

        stream_id = str(kwargs.get("stream_id") or "")
        room = self._rooms.pop(stream_id, None)
        if room is None:
            return self._tool_result(TOOL_STOP, "现在没有进行中的对局。")
        room.task.cancel()
        try:
            await room.session.stop()
        except Exception as exc:  # noqa: BLE001
            if self._logger is not None:
                self._logger.exception("收摊失败")
            return self._tool_result(TOOL_STOP, f"收摊时出错：{type(exc).__name__}: {exc}")
        return self._tool_result(TOOL_STOP, "房间已经关掉了。想再打随时说一声。")

    # ------------------------------------------------------------------ 指令

    # ⚠ 这里原来有一条 `/重生成脚本` 指令（让插件重新给某副牌生成专属 C# 执行器并编译）。
    # **2026-10-07 用户口径：插件不再自动写脚本**——新卡组一律用通用脚本，想更强就用 AI agent
    # 按 executors/README.md 写（多轮迭代），所以这条指令整块删掉了。

    @Command(
        "ygo_cmd_optimize",
        description="自动优化一副卡组：换牌 → 自动对战 → 只保留统计上更强的版本",
        pattern=r"^/(?:优化卡组|卡组优化)\s+\S+",
    )
    async def cmd_optimize(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """跑一轮卡组进化搜索（换牌 → 对战 → 按区间判定）。"""

        del kwargs
        if self._deck_pool is None:
            await self.ctx.send.text("卡组池还没准备好。", stream_id)
            return True, "卡组池不可用", 1
        for task, label in ((self._training_task, "训练"), (self._optimize_task, "优化")):
            if task is not None and not task.done():
                await self.ctx.send.text(f"已经有{label}在跑了，等它结束再来。", stream_id)
                return True, "已有重活在跑", 1
        parts = text.strip().split()
        # **只取第一个参数**当卡组引用：解析器是按"整行指令"取参数的，把整行喂进去的话，
        # 后面跟着的局数会被当成卡组名的一部分（"码丽丝 25"）⇒ 永远找不到（实测踩了两次）
        reference = f"{parts[0]} {parts[1]}" if len(parts) > 1 else parts[0]
        deck = self._deck_by_argument(group_id, reference)
        if deck is None:
            # 私聊里没有"本群卡组池"，上面必然找不到。退一步按**内置卡组**解析：
            # 私聊里说"优化某副牌"时，想优化的几乎总是内置卡组或编号卡组（实测用户就在私聊里用）
            deck = self._deck_by_argument(BUILTIN_GROUP, reference)
        if deck is None:
            await self.ctx.send.text(
                "用法：/优化卡组 <编号或名字> [每个对手的局数]\n"
                "会用同群的其它卡组当对手，自动试换牌并只保留统计上更强的版本（新建一副，不动原牌）。",
                stream_id,
            )
            return True, "缺卡组参数", 1
        rounds = 25
        if len(parts) > 2 and parts[2].isdigit() and 1 <= int(parts[2]) <= 200:
            rounds = int(parts[2])
        opponents = [
            item
            for item in self._deck_pool.list_decks(group_id or deck.group_id)
            if item.deck_id != deck.deck_id
        ][:2]
        if not opponents:
            await self.ctx.send.text(
                f"卡组池里除了「{deck.display_name}」没有别的卡组了，优化需要至少一副对手牌。\n"
                "先让群友投一副（发卡组码 + /加卡组），或者用 /卡组列表 看看池子里有什么。",
                stream_id,
            )
            return True, "没有对手卡组", 1
        expected = (rounds * 2) * len(opponents) * 2 if len(opponents) == 2 else rounds * 2
        await self.ctx.send.text(
            f"开始优化「{deck.display_name}」，对手：{'、'.join(item.display_name for item in opponents)}。\n"
            f"每个候选约打 {expected} 局，跑到几十局的批次通常要十几到几十分钟"
            "（WindBot 偶发错误会拖慢）；基线打完、每个候选打完我都会说一声，跑完发结果。",
            stream_id,
        )
        self._optimize_task = asyncio.create_task(
            self._run_optimize(deck, rounds, opponents, stream_id, group_id)
        )
        return True, "优化已开始", 1

    def _optimize_command(
        self, deck: StoredDeck, rounds: int, opponents: Sequence[StoredDeck]
    ) -> List[str]:
        """拼出优化搜索的命令行（独立进程，见 tools/optimize_deck.py）。

        ``--opponents`` 是那个工具的**必填**参数：漏掉的话 argparse 直接退出，输出里只有一段
        usage、连一行 ``[错误]`` 都没有，群里就会被说成"这一轮没有候选通过判定"（实测踩过）。
        优化器自己不用模型（只用卡库与对局数据），所以**不要**给它传 ``--model``。
        """

        command = [
            sys.executable,
            str(_PLUGIN_ROOT / "tools" / "optimize_deck.py"),
            "--plugin-root",
            str(_PLUGIN_ROOT),
            "--deck-id",
            str(deck.deck_id),
            "--opponents",
            ",".join(str(item.deck_id) for item in opponents),
            "--rounds",
            str(rounds),
            # 候选越多，局数线性增长（基线 100 局 + 每个候选 100 局，4 并发下要几十分钟）。
            # 群里跑两副就够了：区间判定本来就不会在几十局规模下通过，真正落地的是死牌清理。
            "--candidates",
            "2",
        ]
        return command

    @staticmethod
    def _optimize_summary(output: str, returncode: int = 0) -> str:
        """从优化进程的输出里整理一条群消息（单独抽出来是为了能测）。

        ``returncode`` 用来兜住"输出里没有可识别的错误、但进程就是没跑完"的情况
        （argparse 用法错误、没捕获的异常）：那时候只报"没有候选通过判定"会把真因吞掉。
        """

        lines = output.strip().splitlines()
        # 认"基线（原卡表）"这个前缀，别只认"基线"：进度行是「    [基线] 已打 10/20 局…」，
        # 它排在结果行前面，只认两个字的话群里看到的就是进度而不是结果（实测踩过）
        base = next((line for line in lines if line.strip().startswith("基线（原卡表）")), "")
        accepted = [line for line in lines if "采纳：" in line or "已新建卡组" in line]
        verdict = "这一轮没有候选通过判定，原卡组没动。"
        for line in lines:
            if "已新建卡组" in line:
                verdict = line.strip()
                break

        # 子进程要是根本没跑起来（参数错、卡表体检不过、引擎缺数据），输出里是一行 [错误]。
        # 这时候报"没有候选通过判定"会把真正的原因吞掉——实测就是这样把"这副牌有白板卡"
        # 说成了"候选都不合格"，后来又有一次是 argparse 的 usage（连 [错误] 都没有），
        # 所以这里连 usage/异常一起认。
        problems = [
            line.strip()
            for line in lines
            if line.strip().startswith(("[错误]", "usage:", "Traceback"))
            or "本身的" in line
            or "没能" in line
            or line.strip().startswith("optimize_deck.py: error:")
        ]
        if problems and not accepted:
            return "【卡组优化】没能跑完：\n" + "\n".join(problems[:4])
        if returncode not in (0, 1) and not accepted and not base:
            tail = "\n".join(line.strip() for line in lines[-3:] if line.strip())
            return f"【卡组优化】没能跑完（退出码 {returncode}）：\n{tail or '（子进程没有输出）'}"

        # 体检给的是警告（比如"主体卡组里有查不到脚本的卡"）：不拦优化，但得让群里知道
        # ——那几张卡在游戏里是白板，打不出来不是打法问题
        warnings = [line.strip() for line in lines if line.strip().startswith("[警告]")]

        parts = ["【卡组优化】"]
        if base:
            parts.append(base.strip())
        if accepted:
            parts.extend(line.strip() for line in accepted[:3])
        else:
            parts.append(verdict)
            note = next((line for line in lines if "每组约需" in line), "")
            if note:
                parts.append(note.strip())
        parts.extend(warnings[:2])
        return "\n".join(parts)

    async def _run_optimize(
        self,
        deck: StoredDeck,
        rounds: int,
        opponents: Sequence[StoredDeck],
        stream_id: str,
        group_id: str,
    ) -> None:
        """跑一轮优化，结束后把结果发到群里（独立进程，别占住事件循环）。

        整批要跑几十分钟，所以**边跑边报里程碑**（基线打完、每个候选打完、出错）：只在结束时
        吭一声的话，群里看着就像卡死了——实测用户就是这么以为的。
        """

        target = stream_id or group_id
        command = self._optimize_command(deck, rounds, opponents)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(_PLUGIN_ROOT),
            )
        except Exception as exc:  # noqa: BLE001  优化失败不该影响插件其余功能
            if self._logger is not None:
                self._logger.exception("优化进程启动失败")
            await self._announce(target, f"优化没能跑起来：{type(exc).__name__}: {exc}")
            return
        lines: List[str] = []
        try:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                milestone = self._optimize_milestone(line)
                if milestone:
                    await self._announce(target, milestone)
            await process.wait()
        except Exception as exc:  # noqa: BLE001  读进程输出失败也要有个交代
            if self._logger is not None:
                self._logger.exception("优化进程输出读取失败")
            process.kill()
            await self._announce(target, f"优化中途断了：{type(exc).__name__}: {exc}")
            return
        await self._announce(
            target, self._optimize_summary("\n".join(lines), returncode=process.returncode or 0)
        )

    @staticmethod
    def _optimize_milestone(line: str) -> str:
        """把优化进程的关键输出翻成一条群消息（不是关键行就返回空串）。"""

        text = line.strip()
        if text.startswith("[错误]"):
            return f"【卡组优化】{text}"
        if text.startswith("基线（原卡表）"):
            return f"【卡组优化】基线跑完了，{text}"
        if text.startswith("候选 ") and "→" in text:
            return f"【卡组优化】{text}"
        return ""

    def _list_position(self, deck: StoredDeck) -> int:
        """卡组在 ``/卡组列表`` 里的位置号（命令行工具按这个号取卡组，见 tools 的约定）。"""

        if self._deck_pool is None:
            return 0
        for index, item in enumerate(self._deck_pool.list_decks(""), start=1):
            if item.deck_id == deck.deck_id:
                return index
        return 0

    async def _run_tool(
        self, label: str, command: List[str], target: str, *, milestones: Tuple[str, ...] = ()
    ) -> None:
        """在独立进程里跑一个工具，把关键行播到群里，最后把结论整理成一条消息。

        与优化那边同一套做法：**边跑边报**（工具要跑几分钟），结束时再给一条带结论的汇报。
        工具报错一律原样贴出来——这一路上被"报错被说成别的东西"坑过好几次。
        """

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(_PLUGIN_ROOT),
            )
        except Exception as exc:  # noqa: BLE001  工具起不来不该影响插件其余功能
            if self._logger is not None:
                self._logger.exception("%s 进程启动失败", label)
            await self._announce(target, f"{label}没能跑起来：{type(exc).__name__}: {exc}")
            return
        lines: List[str] = []
        try:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                text = line.strip()
                if text.startswith("[错误]"):
                    await self._announce(target, f"{label}：{text}")
                elif any(text.startswith(mark) for mark in milestones):
                    await self._announce(target, f"{label}｜{text}")
            await process.wait()
        except Exception as exc:  # noqa: BLE001  读输出失败也要有个交代
            if self._logger is not None:
                self._logger.exception("%s 输出读取失败", label)
            process.kill()
            await self._announce(target, f"{label}中途断了：{type(exc).__name__}: {exc}")
            return
        tail = [line.strip() for line in lines if line.strip()][-12:]
        report = "\n".join(tail) if tail else "（工具没有任何输出）"
        if process.returncode not in (0, 1):
            report = f"退出码 {process.returncode}：\n{report}"
        await self._announce(target, f"{label}\n{report}")

    def _style_tool_command(self, tool: str, deck: StoredDeck, extra: Sequence[str]) -> List[str]:
        """拼"给某副牌挑脚本/写打法"的命令行（卡组用 ``/卡组列表`` 的位置号传）。"""

        return [
            sys.executable,
            str(_PLUGIN_ROOT / "tools" / tool),
            "--plugin-root",
            str(_PLUGIN_ROOT),
            "--deck-id",
            str(self._list_position(deck)),
            *extra,
        ]

    @Command(
        "ygo_cmd_pick_style",
        description="实测挑出牌脚本：让这副牌用几个现成脚本各打几局，挑会出牌、每局中位动作最多的那个",
        pattern=r"^/(?:挑脚本|选脚本)\s+\S+",
    )
    async def cmd_pick_style(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """按实测排名给这副牌挑一个出牌脚本。"""

        del kwargs
        if self._deck_pool is None:
            await self.ctx.send.text("卡组池还没准备好。", stream_id)
            return True, "卡组池不可用", 1
        for task, label in ((self._pick_task, "挑脚本"), (self._training_task, "训练"), (self._optimize_task, "优化")):
            if task is not None and not task.done():
                await self.ctx.send.text(f"已经有{label}在跑了，等它结束再来。", stream_id)
                return True, "已有重活在跑", 1
        parts = text.strip().split()
        reference = f"{parts[0]} {parts[1]}" if len(parts) > 1 else parts[0]
        deck = self._deck_by_argument(group_id, reference) or self._deck_by_argument(
            BUILTIN_GROUP, reference
        )
        if deck is None:
            await self.ctx.send.text(
                "用法：/挑脚本 <编号或名字>\n"
                "会让这副牌用几个现成的出牌脚本各打几局（对手固定，候选里含它现在用的那个），"
                "按「会不会出牌 + 每局中位动作数」排名（均值与胜率只用来裁决平手），"
                "然后把它记下来供对局使用。",
                stream_id,
            )
            return True, "缺卡组参数", 1
        position = self._list_position(deck)
        if position <= 0:
            await self.ctx.send.text(f"「{deck.display_name}」不在卡组列表里，没法挑脚本。", stream_id)
            return True, "卡组不在列表里", 1
        await self.ctx.send.text(
            f"开始给「{deck.display_name}」实测挑脚本：几个候选各打几局，大概几分钟；跑完发排名。",
            stream_id,
        )
        self._pick_task = asyncio.create_task(
            self._run_tool(
                "【挑脚本】",
                self._style_tool_command("pick_style.py", deck, ["--rounds", "3"]),
                stream_id or group_id,
                milestones=("[结果]",),
            )
        )
        return True, "挑脚本已开始", 1

    @Command(
        "ygo_cmd_write_playbook",
        description="让模型写这副牌的打法数据（先出谁、检索什么、哪些牌别发动），由通用执行器执行",
        pattern=r"^/(?:写打法|打法数据|生成打法)\s+\S+",
    )
    async def cmd_write_playbook(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """生成并保存卡组打法数据。"""

        del kwargs
        if self._deck_pool is None:
            await self.ctx.send.text("卡组池还没准备好。", stream_id)
            return True, "卡组池不可用", 1
        if self._playbook_task is not None and not self._playbook_task.done():
            await self.ctx.send.text("已经有一份打法数据在写了，等它结束再来。", stream_id)
            return True, "已有重活在跑", 1
        parts = text.strip().split()
        reference = f"{parts[0]} {parts[1]}" if len(parts) > 1 else parts[0]
        deck = self._deck_by_argument(group_id, reference) or self._deck_by_argument(
            BUILTIN_GROUP, reference
        )
        if deck is None:
            await self.ctx.send.text(
                "用法：/写打法 <编号或名字>\n"
                "会让模型写成一份「这副牌怎么打」的清单（先出谁、检索什么、哪些牌留着别发动），"
                "校验通过后保存；用通用执行器出牌的那些对局会按它打。",
                stream_id,
            )
            return True, "缺卡组参数", 1
        position = self._list_position(deck)
        if position <= 0:
            await self.ctx.send.text(f"「{deck.display_name}」不在卡组列表里。", stream_id)
            return True, "卡组不在列表里", 1
        await self.ctx.send.text(
            f"开始给「{deck.display_name}」写打法数据（模型写、我们校验卡号），大概一两分钟。",
            stream_id,
        )
        self._playbook_task = asyncio.create_task(
            self._run_tool(
                "【写打法】",
                self._style_tool_command("generate_playbook.py", deck, ["--activate"]),
                stream_id or group_id,
                milestones=("[成功]",),
            )
        )
        return True, "写打法已开始", 1

    @Command(
        "ygo_cmd_train",
        description="让两副卡组互相对打若干局（镜像配对），打完报胜率",
        pattern=r"^/(?:训练|对战训练)\s+\S+",
    )
    async def cmd_train(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """挑两副卡组互相对打，训练/评估它们的强弱并汇报。"""

        del kwargs
        parts = text.strip().split()
        if len(parts) < 3:
            await self.ctx.send.text(
                "用法：/训练 <卡组A> <卡组B> [轮数]\n"
                "两边用同一副牌各坐一次先攻（镜像配对），轮数默认 20（每轮 2 局）。\n"
                "卡组可以用 /卡组列表 里的编号或名字；不填轮数就按默认来。",
                stream_id,
            )
            return True, "用法提示", 1
        if self._deck_pool is None:
            await self.ctx.send.text("卡组池还没准备好。", stream_id)
            return True, "卡组池不可用", 1
        if self._training_task is not None and not self._training_task.done():
            await self.ctx.send.text("已经有一批训练在跑了，等它结束再来。", stream_id)
            return True, "已有训练在跑", 1

        # 注意：_deck_by_argument 期望收到**整行指令**（它内部再 split 取参数词），
        # 直接喂单个参数会让它取到空串、永远"找不到卡组"（实测踩过）
        left = self._deck_by_argument(group_id, f"{parts[0]} {parts[1]}")
        right = self._deck_by_argument(group_id, f"{parts[0]} {parts[2]}")
        if left is None or right is None:
            missing = parts[1] if left is None else parts[2]
            await self.ctx.send.text(f"找不到卡组「{missing}」，发 /卡组列表 看看编号。", stream_id)
            return True, "卡组不存在", 1
        if left.deck_id == right.deck_id:
            await self.ctx.send.text("两边是同一副牌，换一副再来。", stream_id)
            return True, "两边相同", 1

        rounds = self._parse_training_rounds(parts[3] if len(parts) > 3 else "")
        if rounds is None:
            await self.ctx.send.text("轮数要写成 1~100 之间的整数。", stream_id)
            return True, "轮数不合法", 1

        arena = f"train-g{group_id}-{int(time.time())}"
        await self.ctx.send.text(
            f"开始训练：「{left.display_name}」vs「{right.display_name}」，"
            f"{rounds} 轮镜像（共 {rounds * 2} 局）。\n"
            "训练会占满 CPU，期间对局可能变慢；打完了我把结果发上来。",
            stream_id,
        )
        self._training_task = asyncio.create_task(
            self._run_training(arena, left, right, rounds, stream_id, group_id)
        )
        return True, "训练已开始", 1

    @staticmethod
    def _parse_training_rounds(raw: str) -> Optional[int]:
        """把轮数参数读成 1~100；留空用默认值，写错返回 None。"""

        if not raw.strip():
            return DEFAULT_TRAINING_ROUNDS
        try:
            value = int(raw.strip())
        except ValueError:
            return None
        return value if 1 <= value <= MAX_TRAINING_ROUNDS else None

    def _training_command(self, arena: str, left: StoredDeck, right: StoredDeck, rounds: int) -> List[str]:
        """拼出擂台命令行的参数（单独抽出来是为了能测）。"""

        command = [
            sys.executable,
            str(_PLUGIN_ROOT / "tools" / "train_arena.py"),
            "--plugin-root",
            str(_PLUGIN_ROOT),
            "--arena",
            arena,
            "--pairs",
            # 脚本名=卡表路径：按这两副牌**实际在用的打法**训练（生成的专属脚本也在这里生效）
            f"{left.windbot_deck}={left.ydk_path},{right.windbot_deck}={right.ydk_path}",
            "--rounds",
            str(rounds),
            "--parallel",
            "4",
        ]
        # 训练默认不下计划（跑卡组自带的出牌思路）；只有配了 train_model 才让模型参与决策，
        # 而且是"用你指定的那个模型"，不碰 planner
        model = self.config.duel.train_model.strip()
        if model:
            command += ["--coach", "llm", "--llm-model", model]
        else:
            command += ["--coach", "none"]
        return command

    async def _run_training(
        self,
        arena: str,
        left: StoredDeck,
        right: StoredDeck,
        rounds: int,
        stream_id: str,
        group_id: str,
    ) -> None:
        """跑一批训练，结束后把胜率发到群里。

        训练是独立进程（``tools/train_arena.py``）：它要起内核与 WindBot、吃满 CPU，
        放在插件进程里会把事件循环拖死。
        """

        command = self._training_command(arena, left, right, rounds)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(_PLUGIN_ROOT),
            )
            stdout, _ = await process.communicate()
        except Exception as exc:  # noqa: BLE001  训练失败不该影响插件其余功能
            if self._logger is not None:
                self._logger.exception("训练进程启动失败")
            await self._announce(stream_id or group_id, f"训练没能跑起来：{type(exc).__name__}: {exc}")
            return
        output = (stdout or b"").decode("utf-8", errors="replace")
        report = self._training_summary(left, right, rounds, output)
        await self._announce(stream_id or group_id, report)

    @staticmethod
    def _training_summary(left: StoredDeck, right: StoredDeck, rounds: int, output: str) -> str:
        """把擂台输出整理成一条群消息（单独抽出来是为了能测）。"""

        rates: Dict[str, Tuple[int, int]] = {}
        for line in output.splitlines():
            match = re.match(r"^(\S.*?)\s+(\d+)\s+(\d+)\s+([\d.]+)%\s*$", line.strip())
            if match:
                rates[match.group(1).strip()] = (int(match.group(2)), int(match.group(3)))
        head = f"训练完成：「{left.display_name}」vs「{right.display_name}」（{rounds} 轮镜像）"
        if not rates:
            return head + "\n没能从训练输出里读到胜率，原始输出的最后几行：\n" + "\n".join(
                output.strip().splitlines()[-4:]
            )
        left_stat = rates.get(left.windbot_deck) or rates.get(left.display_name)
        right_stat = rates.get(right.windbot_deck) or rates.get(right.display_name)
        lines = [head]
        if left_stat and right_stat:
            left_wins, left_total = left_stat
            right_wins, right_total = right_stat
            lines.append(
                f"「{left.display_name}」{left_wins}/{left_total} = {left_wins / max(left_total, 1):.0%}｜"
                f"「{right.display_name}」{right_wins}/{right_total} = {right_wins / max(right_total, 1):.0%}"
            )
            if left_wins == right_wins:
                lines.append("打平：这两副牌在这个对手池里看不出强弱差别。")
            else:
                better = left if left_wins > right_wins else right
                lines.append(f"这一批里「{better.display_name}」更占上风。")
            silent = [line for line in output.splitlines() if "动作数 < 3" in line]
            if silent:
                lines.append("（有整局空过的对局，胜负参考价值有限，可以再跑一批看看）")
        else:
            for name, (wins, total) in rates.items():
                lines.append(f"{name}：{wins}/{total} = {wins / max(total, 1):.0%}")
        return "\n".join(lines)

    @Command(
        "ygo_cmd_field",
        description="查房：看所有对话流里开着的房间（各自的卡组、回合与场面）",
        pattern=r"^/(?:查房|战况|局面)\s*$",
    )
    async def cmd_field(
        self, stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """报出所有进行中房间的局面：能给图就给图，其余房间各报一段文字。

        原先只看本流：别的群里开着房间时这里会说"没有进行中的对局"，
        而群友问 `/查房` 想知道的正是"谁在打、打到哪了"——所以改成遍历 ``self._rooms``。
        每个房间必须能指名道姓（群名/私聊名 + 卡组名）：stream_id 对群友没有意义。

        **出图的那个房间**（`duel/field_image.py` + 宿主的 `render.html2png`）：
        本流有进行中的房间就画它，否则画**最近开的那一个**——用户 2026-10-07 报的
        "在其他群不能渲染查房的图片"就是这里：以前只认本流，在别的群问 `/查房` 永远只有文字，
        而图里带的群名已经说清了这是谁家的棋盘。
        出图任何一步失败都**退回纯文本**，不能因为渲染挂了就什么都不报。
        """

        del kwargs
        if not self._rooms:
            await self.ctx.send.text("现在没有进行中的对局。发 /开房 可以开一局。", stream_id)
            return True, "没有进行中的对局", 1

        names = await self._stream_display_names()
        own = self._rooms.get(stream_id)
        # 本流排最前（问的人最关心自己这边），其余按开房顺序排后面
        ordered = ([own] if own is not None else []) + [
            room for key, room in self._rooms.items() if key != stream_id
        ]
        # 画谁：本流那个（已开打）优先；没有就画最近开打的那个（dict 保序，最后一个是最近的）
        started = [room for room in ordered if room.session.started]
        picture_room = own if (own is not None and own.session.started) else (started[-1] if started else None)
        if picture_room is not None:
            if await self._send_field_image(
                picture_room, names.get(picture_room.stream_id, ""), stream_id
            ):
                others = [room for room in ordered if room is not picture_room]
                if others:
                    lines = ["【游戏王】别处还有进行中的对局："]
                    for room in others:
                        lines.extend(self._room_field_lines(room, names, self_stream=False))
                    await self.ctx.send.text("\n".join(lines), stream_id)
                return True, "已发出棋盘图", 1
        lines = [f"【游戏王】当前局面（进行中 {len(self._rooms)} 个房间）"]
        for room in ordered:
            lines.extend(self._room_field_lines(room, names, self_stream=room.stream_id == stream_id))
        if own is None:
            lines.append("（这个对话流里没有房间，上面是别处的对局）")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"已报出 {len(self._rooms)} 个房间的局面", 1

    async def _send_field_image(self, room: "ActiveRoom", stream_name: str, stream_id: str) -> bool:
        """给这个房间出一张棋盘图并发出去；任何一步失败都返回 False（由调用方退文本）。

        **里侧的卡只画卡背**（`duel/field_image.py` 的硬约束）：图会发到群里，画卡面等于替对手公开信息。
        整个过程包在 try 里：出图是"锦上添花"，任何环节（拿快照 / 渲染 / 发送）出问题都只记一条日志、
        退回文字——查房绝不能因为图挂了就不报局面。
        """

        try:
            # ⚠ `session.recorder` 是**方法**不是属性（2026-10-07 上线后第一次被 /查房 打到时踩的：
            # 按属性取会拿到函数对象，`recorder.self_seat` 直接 AttributeError → 出图静默退文本）
            recorder = room.session.recorder()
            seat = recorder.self_seat
            if seat not in (0, 1):
                return False
            their_seat = 1 - seat
            our_name = recorder.players[seat].name or self.config.duel.bot_name or "我方"
            their_name = recorder.players[their_seat].name or "对手"
            current_seat = None
            if recorder.first_player_seat in (0, 1) and recorder.turn_count:
                current_seat = (
                    recorder.first_player_seat
                    if recorder.turn_count % 2 == 1
                    else 1 - recorder.first_player_seat
                )

            # 卡名/卡种/攻守由卡库提供；没有卡库（或库不可用）就传 None，图里画卡号
            card_cache: Dict[int, Any] = {}

            def lookup_card(card_id: int) -> Optional[Any]:
                """查一张卡的详情（同一张牌只查一次，一局里反复出现的是同一批怪）。"""

                if card_id not in card_cache:
                    card_cache[card_id] = self._card_db.card_details([card_id]).get(card_id)
                return card_cache[card_id]

            details_of: Optional[Callable[[int], Optional[Any]]] = (
                lookup_card if self._card_db is not None else None
            )

            paths = self.config.paths
            view = view_from_state(
                recorder.field_state,
                seat=seat,
                opponent_seat=their_seat,
                our_label=our_name,
                their_label=their_name,
                our_deck=room.deck_name or "",
                title="游戏王·当前局面",
                subtitle=f"群「{stream_name}」" if stream_name else "",
                phase=recorder.phase,
                art_dir=paths.resolved_card_art_dir(),
                art_fallback_dir=paths.resolved_card_art_fallback_dir(),
                details_of=details_of,
                current_seat=current_seat,
            )
            html = build_html(view)
            # ⚠ 渲染参数是"卡图必须画全"的关键（用户 2026-10-07 报"很多时候没渲染成功就发出来"）：
            # `wait_until="networkidle"` 等页面彻底静下来（`load` 之后还有图片解码与绘制），
            # `wait_for_timeout_ms` 再留一点时间给合成；卡图本身也已缩到 200px 宽（见 field_image），
            # 解码几乎瞬时。少了这两项 + 原尺寸卡图，截屏就会拍到还没画好的格子。
            rendered = await self.ctx.render.html2png(
                html,
                viewport={"width": BOARD_WIDTH, "height": BOARD_HEIGHT},
                device_scale_factor=2,
                wait_until="networkidle",
                wait_for_timeout_ms=300,
                render_timeout_ms=20000,
            )
            image = ""
            if isinstance(rendered, dict):
                image = str(rendered.get("image_base64") or "")
            else:
                image = str(getattr(rendered, "image_base64", "") or "")
            if not image:
                return False
            await self.ctx.send.image(image, stream_id)
            if self._logger is not None:
                # 成功也留一行：出图是"看日志才知道发没发出去"的功能（失败那条走 except）
                self._logger.info(
                    "查房出图已发送：%s×%s，base64 %d 字节",
                    1280, 720, len(image),
                )
            return True
        except Exception:  # noqa: BLE001  出图/发图失败一律退文本（见 docstring）
            if self._logger is not None:
                self._logger.warning("查房出图失败，改为发文字", exc_info=True)
            return False

    async def _stream_display_names(self) -> Dict[str, str]:
        """``stream_id → 群名/私聊名``，供跨群查房指名道姓。

        名字从宿主的聊天流列表里取，不拿 id 硬拼——群友看的是"哪个群在打"。
        取名失败（宿主拒绝、没有该能力）只记一条日志并按会话号报，不牵连查房本身。
        """

        names: Dict[str, str] = {}
        try:
            streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
        except Exception:  # noqa: BLE001  名字取不到不该让查房整条失败
            if self._logger is not None:
                self._logger.warning("查房时获取聊天流列表失败，只能按会话号报房间", exc_info=True)
            return names
        if not isinstance(streams, list):
            return names
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if not session_id:
                continue
            if stream.get("is_group_session"):
                group_name = str(stream.get("group_name") or "").strip()
                names[session_id] = f"{group_name}（群）" if group_name else "未命名群聊"
            else:
                nickname = str(stream.get("user_nickname") or "").strip()
                names[session_id] = f"{nickname} 的私聊" if nickname else "私聊"
        return names

    def _room_field_lines(
        self, room: ActiveRoom, names: Dict[str, str], *, self_stream: bool
    ) -> List[str]:
        """一个房间的查房文本块：先报是哪个流、哪副牌，再报回合与场面。"""

        label = "本群" if self_stream else (names.get(room.stream_id) or f"会话 {room.stream_id[:12]}")
        session = room.session
        lines = [f"· {label}｜卡组：{room.deck_name or '未知'}"]
        if not session.started:
            lines.append("  房间开着，但还没开打（在等群友进来）。")
            return lines
        lines.append(f"  第 {session.turn_count} 回合")
        lines.extend(f"  {line}" for line in session.field_snapshot())
        if session.finished:
            lines.append("  （这局已经打完了，以上是结束时的局面）")
        return lines

    @Command(
        "ygo_cmd_replay",
        description="复盘刚打完的那局：问 AI 问了多少次、拦下多少、时间花在哪，并说清问题出在哪",
        pattern=r"^/(?:复盘|上把复盘)\s*$",
    )
    async def cmd_replay(
        self, stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """把这一局的决策日志读出来，回答"这把为什么打得菜"。

        机读事实（问了多少次、否决率、最慢几问）**永远照发**，模型只负责把它讲成人话——
        和播报同一个口径：数字不能被模型改写，解释才由它写。
        """

        del kwargs
        knowledge = self._knowledge()
        if knowledge is None:
            await self.ctx.send.text("这台机器上还没有知识库（决策日志也记在里面）。", stream_id)
            return True, "没有知识库", 1
        trace = knowledge.last_duel(f"room:{stream_id[:12]}")
        if trace is None:
            await self.ctx.send.text(
                "还没有可复盘的局：开着问 AI 打完一局之后再来（每局的问答与胜负都会落到决策日志里）。",
                stream_id,
            )
            return True, "没有可复盘的局", 1

        lines = self._replay_lines(trace, knowledge)
        comment = await self._write_replay_commentary(trace, lines)
        text = "\n".join(["【复盘】" + lines[0]] + lines[1:] + ([comment] if comment else []))
        await self.ctx.send.text(text, stream_id)
        return True, "已复盘", 1

    def _replay_lines(self, trace: DuelTrace, knowledge: Knowledge) -> List[str]:
        """复盘要发的那几行机读事实（模型不许改这些数字）。"""

        verdict = {"win": "我方赢了", "loss": "我方输了", "draw": "平局"}.get(
            trace.result, "没分出胜负"
        )
        parts = [f"最近一局（{verdict}）：问 AI {trace.asks} 次"]
        if trace.activate_asks:
            parts.append(
                f"其中「要不要发动」{trace.activate_asks} 次、被拦下 {trace.vetos} 次"
                f"（{trace.vetos / trace.activate_asks:.0%}）"
            )
        lines = ["｜".join(parts)]
        if trace.kinds:
            lines.append(
                "问法分布：" + "、".join(f"{kind}×{count}" for kind, count in sorted(trace.kinds.items()))
            )
        slowest = [item for item in knowledge.duel_decisions(trace.duel_key, limit=4) if item[3]]
        if slowest:
            lines.append(
                "最费时间的几问："
                + "；".join(
                    f"{self._card_label(knowledge, card_id)} {cost / 1000:.1f} 秒"
                    for _kind, card_id, _answer, cost in slowest
                )
            )
        if trace.result == "loss":
            lines.append("（这一局的完整问答都在决策日志里，/复盘 每次读的就是它）")
        return lines

    def _card_label(self, knowledge: Knowledge, card_id: int) -> str:
        """卡号 → 卡名（查不到就用卡号；别为了个名字去查两次库）。"""

        if not card_id:
            return "「这一步做什么」"
        fact = knowledge.card_facts(int(card_id))
        return fact.name if fact is not None and fact.name else f"卡 {card_id}"

    async def _write_replay_commentary(self, trace: DuelTrace, lines: List[str]) -> str:
        """请模型把复盘事实讲成人话：问题出在哪、下次怎么改。

        失败（模型不可用、返回空、被拒）返回空串，由调用方只发机读事实——
        不编一段"看起来像解释"的话盖住失败。
        """

        self_name = self._resolve_bot_name()
        prompt = (
            f"你是群里的游戏王玩家「{self_name}」，刚打完一局。看下面这份**这一局的实际数据**，"
            "用两三句话说明：这一局为什么打成这样、下次该怎么改。要求：\n"
            "* 只说数据支持得住的话——否决率高就说它在拦自己的牌，问得慢就说时间被吃掉了；\n"
            "* 数据不足以判断原因时，直接说「看不出」，不要编；\n"
            "* 口语化，别用列表，控制在 100 字以内，不要复述字段名。\n\n"
            f"这一局的数据：\n" + "\n".join(lines)
        )
        try:
            result = await self.ctx.llm.generate(prompt=prompt)
        except Exception:  # noqa: BLE001  模型不可用不该让复盘整条消失
            if self._logger is not None:
                self._logger.exception("生成复盘点评失败")
            return ""
        if not isinstance(result, dict) or not result.get("success", False):
            if self._logger is not None:
                self._logger.warning("生成复盘点评被拒绝：%s", result)
            return ""
        written = str(result.get("response") or result.get("text") or "").strip()
        return written

    @Command(
        "ygo_cmd_start",
        description="直接开一局（不依赖模型的判断）",
        pattern=r"^/(?:开房|开局)\s*\S*",
    )
    async def cmd_start(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """手动开局：走的是和开局工具完全一样的流程，只是不用等模型决定。

        存在的理由：是不是要开房由模型判断，而模型可能判断错、或者（在禁止提问的群里）
        卡住不调用工具，群里就只看到一句空承诺。这条指令把模型从关键路径上拿掉。
        """

        del kwargs
        parts = text.strip().split(maxsplit=1)
        target = parts[1].strip().lower() if len(parts) > 1 else ""
        platform = target if target in (PLATFORM_MDPRO3, PLATFORM_YGOMOBILE) else ""
        result = await self.tool_start_duel(platform=platform, stream_id=stream_id, group_id=group_id)
        note = str(result.get("content") or "")
        if stream_id not in self._rooms:
            # 没开成：原因要发到群里。开成了的话房间信息由开局流程自己发过了
            await self.ctx.send.text(note or "开局失败，原因不明。", stream_id)
        return True, note.splitlines()[0] if note else "已处理", 1

    @Command(
        "ygo_cmd_deck_list",
        description="查看卡组池：内置卡组与群友投稿，以及随机池状态",
        pattern=r"^/(?:卡组列表|卡组)\s*$",
    )
    async def cmd_deck_list(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """列出本群卡组池。"""

        del text, kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        group_key = group_id or stream_id
        decks = self._deck_pool.list_decks(group_key)
        if not decks:
            await self.ctx.send.text(
                "卡组池是空的——通常是没配 WindBot 工作目录、或那个目录里没有 Decks/。"
                "群友也可以发卡组码投稿，发 /加卡组 <卡组码> 就能收下。",
                stream_id,
            )
            return True, "卡组池为空", 1

        fixed = self._deck_pool.fixed_deck()
        in_random = self._deck_pool.count_in_random(group_key)
        lines = [f"【游戏王】卡组池：{len(decks)} 副，其中 {in_random} 副在随机池"]
        for index, deck in enumerate(decks, start=1):
            marks = ["✓随机" if deck.in_random else "✗随机"]
            if fixed is not None and fixed.deck_id == deck.deck_id:
                marks.append("★固定")
            if deck.is_builtin:
                marks.append("内置")
            lines.append(f"{index}. [{ ' '.join(marks) }] {deck.display_name}")
        lines.append(
            "指令：/卡组详情 <编号>、/加入随机 <编号|全部>、/移出随机 <编号|全部>、"
            "/固定卡组 <编号|随机>、/随机池"
        )
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"列出 {len(decks)} 副卡组", 1

    @Command(
        "ygo_cmd_deck_add",
        description="用卡组码投稿一副卡组",
        pattern=r"^/加卡组\s+\S",
    )
    async def cmd_deck_add(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """把群友贴的卡组码收进本群卡组池。"""

        nickname = str(kwargs.get("user_nickname") or "")
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2:
            await self.ctx.send.text("用法：/加卡组 <卡组码> [卡组名]", stream_id)
            return True, "缺少卡组码", 1
        deck_code = parts[1]
        deck_name = parts[2] if len(parts) > 2 else ""
        receipt = self._store_deck_from_code(
            group_id=group_id or stream_id,
            stream_id=stream_id,
            deck_code=deck_code,
            deck_name=deck_name,
            user_id=user_id,
            user_nickname=nickname,
        )
        await self.ctx.send.text(receipt, stream_id)
        return True, receipt.splitlines()[0] if receipt else "已处理", 1

    @Command(
        "ygo_cmd_deck_delete",
        description="删除本群的一副投稿卡组",
        pattern=r"^/删卡组\s+\S",
    )
    async def cmd_deck_delete(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """按编号或名字删除一副投稿。"""

        del kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        group_key = group_id or stream_id
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await self.ctx.send.text("用法：/删卡组 <编号或卡组名>（编号看 /卡组列表）", stream_id)
            return True, "缺少参数", 1
        target = parts[1].strip()

        deck = None
        if target.isdigit():
            decks = self._deck_pool.list_decks(group_key)
            index = int(target)
            if 1 <= index <= len(decks):
                deck = decks[index - 1]
        else:
            deck = self._find_deck(group_key, target)
        if deck is None:
            await self.ctx.send.text(f"没找到「{target}」这副卡组，先发 /卡组列表 看看编号。", stream_id)
            return True, "未找到卡组", 1

        if deck.is_builtin:
            await self.ctx.send.text(
                f"「{deck.display_name}」是 WindBot 自带卡组，不能删；"
                "不想让它被抽到就发 /移出随机 <编号>。",
                stream_id,
            )
            return True, "内置卡组不可删除", 1
        try:
            removed = self._deck_pool.remove(group_key, deck.deck_id)
        except DeckPoolError as exc:
            await self.ctx.send.text(f"删除失败：{exc}", stream_id)
            return True, "删除失败", 1
        if not removed:
            await self.ctx.send.text("删除失败，这副卡组可能已经被删掉了。", stream_id)
            return True, "删除失败", 1
        await self.ctx.send.text(
            f"已删除「{deck.display_name}」。本群还剩 {self._deck_pool.count(group_key)} 副。", stream_id
        )
        return True, f"已删除 {deck.display_name}", 1

    @Command(
        "ygo_cmd_deck_detail",
        description="看某一副卡组的详情",
        pattern=r"^/卡组详情\s+\S",
    )
    async def cmd_deck_detail(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """看编号对应卡组的名字、张数、状态与出牌思路。"""

        del kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        deck = self._deck_by_argument(group_id or stream_id, text)
        if deck is None:
            await self.ctx.send.text("没找到这副卡组，先发 /卡组列表 看看编号。", stream_id)
            return True, "未找到卡组", 1
        lines = [
            f"【{deck.display_name}】{'WindBot 自带卡组' if deck.is_builtin else '群友投稿'}",
            f"随机池：{'在' if deck.in_random else '不在'}｜出牌思路：{deck.windbot_deck or '未设置'}",
            # 问 AI 档位（/出牌模式 改的就是它）：空串＝跟随全局
            "问 AI：" + BRAIN_MODES.get(deck.brain_scope, ("跟随全局（库里的值认不出）", ""))[0],
        ]
        if deck.is_builtin:
            lines.append("内置卡组配着它自己的出牌脚本，牌力最稳")
        else:
            lines.append(
                f"主{deck.main_count}/额外{deck.extra_count}/副{deck.side_count}，投稿人 {deck.contributor_name}"
            )
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"详情：{deck.display_name}", 1

    @Command(
        "ygo_cmd_random_add",
        description="把卡组加入随机池",
        pattern=r"^/加入随机\s+\S",
    )
    async def cmd_random_add(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """把一副卡组（或全部）加入随机池。"""

        del kwargs
        return await self._change_random(text, stream_id, group_id or stream_id, in_random=True)

    @Command(
        "ygo_cmd_random_remove",
        description="把卡组移出随机池",
        pattern=r"^/移出随机\s+\S",
    )
    async def cmd_random_remove(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """把一副卡组（或全部）移出随机池。"""

        del kwargs
        return await self._change_random(text, stream_id, group_id or stream_id, in_random=False)

    @Command(
        "ygo_cmd_random_list",
        description="查看当前随机池里的卡组",
        pattern=r"^/随机池\s*$",
    )
    async def cmd_random_list(
        self, stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """列出当前会参与随机抽取的卡组。"""

        del kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        group_key = group_id or stream_id
        pool = self._deck_pool.random_pool(group_key)
        if not pool:
            await self.ctx.send.text(
                "随机池是空的。发 /加入随机 <编号> 或 /加入随机 全部 来放卡组进去。", stream_id
            )
            return True, "随机池为空", 1
        names = "、".join(deck.display_name for deck in pool)
        text_out = f"【游戏王】随机池 {len(pool)} 副：{names}"
        fixed = self._deck_pool.fixed_deck()
        if fixed is not None:
            text_out += "\n" + f"注意：现在固定用「{fixed.display_name}」，发 /固定卡组 随机 才会走随机池。"
        await self.ctx.send.text(text_out, stream_id)
        return True, f"随机池 {len(pool)} 副", 1

    @Command(
        "ygo_cmd_brain_mode",
        description="按卡组切换问 AI 的档位（不问 / 每问都问 / 只问高压 / 只应对不掌舵 / 跟随全局）",
        pattern=r"^/(?:出牌模式|模式|ai模式|AI模式)\s*\S*",
    )
    async def cmd_brain_mode(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """``/出牌模式 <编号或名字> [档位]``：不带档位就看当前，带了就改。

        为什么要按牌分别设：实测"脚本本来就能打"的牌（升辉月跑自带的 Lucky）开着问 AI 反而少打动作
        （16 局：特召 4.4 → 3.6、还多出空过局），而"脚本一步都走不出来"的牌全靠 AI 才动得起来。
        一个全局开关满足不了两种牌，所以档位记在卡组上。
        """

        del kwargs
        if self._deck_pool is None:
            await self.ctx.send.text("卡组池还没准备好。", stream_id)
            return True, "卡组池不可用", 1
        group_key = group_id or stream_id
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 2 or not parts[1].strip():
            await self.ctx.send.text(
                "用法：/出牌模式 <编号或名字> [档位]\n"
                "档位：不问 / 每问都问 / 只问高压 / 只应对 / 跟随全局（默认）。\n"
                "不带档位就看这副牌现在是什么档。",
                stream_id,
            )
            return True, "缺卡组参数", 1

        # _deck_by_argument 收的是"命令 + 一个参数"这种文本（它自己取第二个词），而这条命令
        # 有两个参数（编号 + 档位）——所以先规整成它能认的两段式，别把"1 只问高压"整串当名字
        lookup = f"/模式 {parts[1].strip()}"
        deck = self._deck_by_argument(group_key, lookup) or self._deck_by_argument(BUILTIN_GROUP, lookup)
        if deck is None:
            await self.ctx.send.text(f"没找到「{parts[1].strip()}」这副牌。", stream_id)
            return True, "缺卡组参数", 1

        if len(parts) < 3 or not parts[2].strip():
            current = BRAIN_MODES.get(deck.brain_scope, ("跟随全局（库里的值认不出）", ""))[0]
            global_mode = self._brain_scope_for(None)
            shown = {"off": "不问 AI", "all": "每一问都问"}.get(global_mode, global_mode)
            await self.ctx.send.text(
                f"「{deck.display_name}」当前的问 AI 档位：{current}。\n"
                f"（全局配置是「{shown}」；发 /出牌模式 {parts[1].strip()} 不问 可以改）",
                stream_id,
            )
            return True, "已报出档位", 1

        wanted = parts[2].strip()
        if wanted in BRAIN_MODE_ALIASES:
            scope = BRAIN_MODE_ALIASES[wanted]
        elif wanted in BRAIN_MODES:
            scope = wanted
        else:
            await self.ctx.send.text(
                f"「{wanted}」这个档位不认识。可用：不问 / 每问都问 / 只问高压 / 只应对 / 跟随全局。",
                stream_id,
            )
            return True, "档位不认识", 1

        self._deck_pool.set_brain_scope(deck.deck_id, scope)
        label = BRAIN_MODES[scope][0]
        await self.ctx.send.text(
            f"好了：「{deck.display_name}」之后每局**{label}**（下一局生效）。",
            stream_id,
        )
        return True, f"已设为 {label}", 1

    @Command(
        "ygo_cmd_deck_fix",
        description="固定/取消固定麦麦使用的卡组（对所有群生效）",
        pattern=r"^/固定卡组\s*\S*",
    )
    async def cmd_deck_fix(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """固定一副卡组，或恢复随机抽取。固定是对所有群生效的（与 /对局名字 一致）。"""

        del kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        group_key = group_id or stream_id
        parts = text.strip().split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""

        if not target:
            fixed = self._deck_pool.fixed_deck()
            answer = (
                f"当前固定使用「{fixed.display_name}」（对所有群生效）。"
                "发 /固定卡组 随机 可以恢复随机抽取。"
                if fixed is not None
                else "当前是随机抽取。发 /固定卡组 <编号> 可以固定一副。"
            )
            await self.ctx.send.text(answer, stream_id)
            return True, answer, 1

        if target in ("随机", "取消", "关"):
            self._deck_pool.set_fixed_deck(None)
            await self.ctx.send.text("好了，之后每局从随机池里抽一副。", stream_id)
            return True, "已取消固定", 1

        deck = None
        if target.isdigit():
            decks = self._deck_pool.list_decks(group_key)
            index = int(target)
            if 1 <= index <= len(decks):
                deck = decks[index - 1]
        else:
            deck = self._find_deck(group_key, target)
        if deck is None:
            await self.ctx.send.text(f"没找到「{target}」这副卡组，先发 /卡组列表 看看编号。", stream_id)
            return True, "未找到卡组", 1

        self._deck_pool.set_fixed_deck(deck.deck_id)
        await self.ctx.send.text(
            f"好了，之后每局都用「{deck.display_name}」（对所有群生效）。"
            "发 /固定卡组 随机 可以恢复随机抽取。",
            stream_id,
        )
        return True, f"已固定 {deck.display_name}", 1

    @Command(
        "ygo_cmd_deck_clear",
        description="清空本群的全部投稿卡组（需管理员）",
        pattern=r"^/(?:清空卡组|删除所有卡组)\s*$",
        permission="operator",
    )
    async def cmd_deck_clear(
        self, stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """清空本群卡组池。"""

        del kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        group_key = group_id or stream_id
        count = self._deck_pool.delete_all(group_key)
        await self.ctx.send.text(f"已清空本群的 {count} 副投稿卡组。", stream_id)
        return True, f"已清空 {count} 副卡组", 1

    @Command(
        "ygo_cmd_bot_name",
        description="设置机器人在游戏内显示的名字（需管理员）",
        pattern=r"^/(?:对局名字|游戏内名字)\s*\S*",
        permission="operator",
    )
    async def cmd_bot_name(
        self, text: str = "", stream_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """设置游戏内名字；对所有群生效，从下一局开始。"""

        del kwargs
        parts = text.strip().split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            await self.ctx.send.text(
                f"现在游戏内名字是「{self._resolve_bot_name()}」。"
                "发 /对局名字 新名字 可以改，发 /对局名字 默认 恢复配置里的值。",
                stream_id,
            )
            return True, "查询游戏内名字", 1

        if self._deck_pool is None:
            return True, "存储还没准备好", 1
        if target in ("默认", "重置"):
            self._deck_pool.set_setting(SETTING_BOT_NAME, None)
            await self.ctx.send.text(
                f"已恢复为配置里的名字「{self.config.duel.bot_name}」，下一局生效。", stream_id
            )
            return True, "已恢复默认名字", 1

        # 协议里名字字段是 20 个 UTF-16 字符，超了会被截断，这里先拦住
        if len(target) > 20:
            await self.ctx.send.text("名字太长了，游戏内名字最多 20 个字符。", stream_id)
            return True, "名字过长", 1
        self._deck_pool.set_setting(SETTING_BOT_NAME, target)
        await self.ctx.send.text(
            f"好了，下一局开始机器人就叫「{target}」了（对所有群生效）。", stream_id
        )
        return True, f"游戏内名字已设为 {target}", 1

    # ------------------------------------------------------------------ 卡组池辅助

    def _deck_index(self, group_id: str, deck_id: int) -> int:
        """查一副卡组在 ``/卡组列表`` 里的编号（从 1 开始）；找不到时返回 0。"""

        if self._deck_pool is None:
            return 0
        for index, deck in enumerate(self._deck_pool.list_decks(group_id), start=1):
            if deck.deck_id == deck_id:
                return index
        return 0

    def _deck_by_argument(self, group_id: str, text: str) -> Optional[StoredDeck]:
        """按「编号或名字」取一副卡组；编号与 /卡组列表 显示的一致。"""

        if self._deck_pool is None:
            return None
        parts = text.strip().split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            return None
        if target.isdigit():
            decks = self._deck_pool.list_decks(group_id)
            index = int(target)
            if 1 <= index <= len(decks):
                return decks[index - 1]
            return None
        return self._find_deck(group_id, target)

    async def _change_random(
        self, text: str, stream_id: str, group_id: str, *, in_random: bool
    ) -> Tuple[bool, str, int]:
        """把一副卡组或全部卡组加入/移出随机池。"""

        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        parts = text.strip().split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        action = "加入" if in_random else "移出"

        if target in ("全部", "所有", "all"):
            decks = self._deck_pool.list_decks(group_id)
            for deck in decks:
                self._deck_pool.set_in_random(deck.deck_id, in_random)
            await self.ctx.send.text(
                f"已把 {len(decks)} 副卡组{action}随机池；现在随机池里共 "
                f"{self._deck_pool.count_in_random(group_id)} 副。",
                stream_id,
            )
            return True, f"{action}随机池 {len(decks)} 副", 1

        deck = self._deck_by_argument(group_id, text)
        if deck is None:
            await self.ctx.send.text(
                f"没找到这副卡组。用法：/{action}随机 <编号|全部>（编号看 /卡组列表）", stream_id
            )
            return True, "未找到卡组", 1
        if deck.in_random == in_random:
            state = "已经在" if in_random else "本来就不在"
            await self.ctx.send.text(f"「{deck.display_name}」{state}随机池里。", stream_id)
            return True, f"{deck.display_name} 状态未变", 1
        self._deck_pool.set_in_random(deck.deck_id, in_random)
        await self.ctx.send.text(
            f"已把「{deck.display_name}」{action}随机池；现在随机池里共 "
            f"{self._deck_pool.count_in_random(group_id)} 副。",
            stream_id,
        )
        return True, f"{action}随机池：{deck.display_name}", 1

    # ------------------------------------------------------------------ WindBot 可执行文件

    def _resolve_windbot_executable(self) -> Path:
        """决定用哪个 WindBot 可执行文件。

        出牌脚本是编译进 exe 的（WindBot 用反射扫 ``[Deck(...)]`` 注册），所以选错 exe
        不会报错，只会静默换一个随机脚本 —— 具体分三种情况：

        1. 开了 AI 教练：必须用编译进 ``PlanAwareExecutor`` 的那份（``bin/PlanAware``），
           否则教练写的计划根本没人读；
        2. 没开教练但配了源码树且编译过：用编译产物（里面含自己写/agent 写的执行器）；
        3. 其余情况：用配置里那个原版 exe。
        """

        configured = self.config.paths.resolved_windbot_executable() or Path(
            self.config.paths.windbot_executable
        )
        src_dir = self.config.paths.resolved_windbot_src_dir()
        if src_dir is None:
            return configured
        if self._plan_coach_requested():
            plan_built = src_dir / "bin" / "PlanAware" / "WindBot.exe"
            if plan_built.is_file():
                return plan_built
            # 计划感知执行器没编译出来。这里不能用兜底糊过去：用原版 exe 时
            # Deck=PlanAware 会被随机执行器顶替，计划无人读取而日志里看不出异常
            self._logger.error(
                "配置开了 AI 教练（duel.ai_plan_coach=%s），但找不到计划感知执行器：%s；"
                "这一局会退回各卡组自带的出牌脚本（教练写的计划不会生效）。"
                "需要先按 README 用 msbuild 把 windbot-src 编译到 bin/PlanAware",
                self.config.duel.ai_plan_coach,
                plan_built,
            )
            return configured
        built = src_dir / "bin" / "Release" / "WindBot.exe"
        return built if built.is_file() else configured

    def _plan_coach_requested(self) -> bool:
        """配置里是否要求了 AI 教练（``duel.ai_plan_coach``）。"""

        return self.config.duel.ai_plan_coach.strip().lower() not in ("", "off", "none", "无", "关")

    # ⚠ 这里原来有四段"让模型现写 C# 出牌脚本"的代码：
    #   `_make_script_generator`（造 DeckScriptGenerator）／`_llm_generate`（调宿主模型）
    #   ／`_web_search`（查打法要点，只为喂给那个模型）／以及后面的 `_make_script_handler`
    #   `_generate_deck_script(_locked)` `_script_command` `_parse_generated_style`。
    # **2026-10-07 用户口径：插件不再自动写脚本**——新导入的卡组一律用 WindBot 的通用脚本，
    # 想给某副牌写专属执行器就用 AI agent 按 `executors/README.md` 多轮迭代着写（推荐做法）。
    # 因此这一整条链路连同 `duel/script_gen.py`、`tools/generate_deck_script.py` 与
    # `auto_generate_script` / `script_model` / `script_max_tokens` / `search_endpoint`
    # / `search_allow_private_host` 五个配置项一起去掉了。

    # ⚠ 这里原来还有三块（`_make_script_handler` / `_generate_deck_script` /
    # `_generate_deck_script_locked`）：导入卡组后排一个后台任务，调 `tools/generate_deck_script.py`
    # 让模型写 C# 执行器、编译、再把脚本名写回卡组库。**2026-10-07 用户口径：不再自动写脚本**，
    # 所以整块删掉——投稿回执里只说明"先用通用脚本打，想更强就让 agent 写专属执行器"。
    # 手工编译出来的执行器依旧生效：卡组库里的 `generated_script` / `picked_style` 字段还在，
    # `_with_current_style()` 照旧优先用它（`set_generated_script` 现在只由命令行工具调用）。

    def _make_deck_plan_handler(self, stream_id: str, group_id: str) -> Callable[[StoredDeck], None]:
        """造一个「导入卡组后写展开流程」的回调（后台任务，不阻塞投稿回执）。

        只做一件事：让模型按卡表写一份人话的展开流程，存进知识库的 ``deck:<编号>``；
        问 AI 时（"这一步做什么"那一问）会整份喂给模型。
        所以它失败也不影响开局——最多少一份资料。
        """

        def handler(deck: StoredDeck) -> None:
            task = asyncio.create_task(self._generate_deck_plan(stream_id, group_id, deck))
            self._background_tasks.add(task)

            def done(finished: asyncio.Task) -> None:
                self._background_tasks.discard(finished)
                if finished.cancelled():
                    return
                error = finished.exception()
                if error is not None and self._logger is not None:
                    self._logger.error(
                        "写展开流程的后台任务异常退出：%s: %s", type(error).__name__, error
                    )

            task.add_done_callback(done)

        return handler

    async def _generate_deck_plan(self, stream_id: str, group_id: str, deck: StoredDeck) -> None:
        """让模型按卡表写一份展开流程，存进知识库（独立进程，几十秒）。"""

        del group_id
        command = [
            sys.executable,
            str(_PLUGIN_ROOT / "tools" / "build_deck_plan_ai.py"),
            "--plugin-root",
            str(_PLUGIN_ROOT),
            "--deck-id",
            str(deck.deck_id),
            "--activate",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(_PLUGIN_ROOT),
            )
            stdout, _ = await process.communicate()
        except Exception:  # noqa: BLE001  后台任务必须自己吞异常
            if self._logger is not None:
                self._logger.exception("启动「写展开流程」进程失败：卡组 %s", deck.deck_id)
            return
        output = (stdout or b"").decode("utf-8", errors="replace")
        # 缓存要失效：这条进程刚往 knowledge.db 里写了数据
        self._knowledge_cache = None
        if process.returncode != 0:
            tail = "\n".join(output.strip().splitlines()[-4:]) or "（没有任何输出）"
            if self._logger is not None:
                self._logger.warning("「%s」的展开流程没写成功：%s", deck.display_name, tail)
            return
        if self._logger is not None:
            self._logger.info("「%s」的展开流程已写进知识库", deck.display_name)

    # ⚠ 这里原来还有三块：`_script_command`（拼 `tools/generate_deck_script.py` 的命令行）、
    # `_parse_generated_style`（从子进程输出里抠脚本类名）、`_collect_card_info`
    # （把卡组整理成含效果文本的清单喂给模型）。自动写脚本整条链路已按 2026-10-07 用户口径删除。
    # 卡牌清单这份口径本身还有用（写打法数据、展开流程的两支 CLI 都要），
    # 已经挪到 `duel/cards.py` 的 `collect_card_info()`。

    async def _announce(self, stream_id: str, text: str) -> None:
        """往群里发一条消息；发不出去也只记日志。"""

        try:
            await self.ctx.send.text(text, stream_id)
        except Exception:  # noqa: BLE001  播报失败不该影响后台任务收尾
            if self._logger is not None:
                self._logger.exception("发送消息失败")

    # ------------------------------------------------------------------ 游戏内名字

    def _resolve_bot_name(self) -> str:
        """取 bot 的游戏内名字：优先用指令改过的值，其次用配置。"""

        if self._deck_pool is not None:
            override = self._deck_pool.get_setting(SETTING_BOT_NAME)
            if override:
                return override
        return self.config.duel.bot_name

    # ------------------------------------------------------------------ 房间完成处理

    async def _run_room(self, session: DuelSession, stream_id: str, group_id: str) -> None:
        """在后台等一局打完，然后播报结果并写进机器人上下文。"""

        outcome = OUTCOME_ABORTED
        summary: List[str] = []
        result_data: Dict[str, object] = {}
        try:
            outcome = await session.wait_finished()
            summary = session.summary_lines()
            result_data = session.result_dict()
            # 这一局的决策日志补上结果（`/复盘` 与 review_decisions 靠它判断干预的对错）
            self._finish_room_brain(stream_id, session, result_data, outcome)
            # 打完的真实对局也进结果库（与擂台同一个库，arena="room" 区分）——
            # 不落库的话，"改动有没有效果"就只能在擂台模拟里看，真实对局一点数据都不留
            self._record_room_duel(session, result_data, outcome)
            report = self._compose_result_message(outcome, summary)
            if outcome == OUTCOME_FINISHED:
                # 打完的总结交给模型写成一段人话，再由插件直接发到群里。
                # 不走 planner 是有意的：planner 那一轮可能被其它插件的规则拦掉
                # （实测被拦过），而这条播报是这一局的收尾，不该受别的插件影响。
                # 胜负由**机器写在第一行**（见 _verdict_line）：模型的语气可以有起伏，
                # 但"谁赢了"这件事不能再由它复述——实测它写反过不止一次。
                verdict = self._verdict_line(result_data)
                text = report
                if self.config.duel.summarize_with_ai:
                    written = await self._write_summary(report, result_data)
                    if written:
                        text = written
                    else:
                        # 润色失败时的降级（用户 2026-10-07 要求）：**只报回合数与机器人用的卡组名**
                        # （胜负行由外层加），不再把"回合数/双方统计"那种字段列表整段发出去。
                        # ⚠ 这里必须从 `result_data` 取回合数：早先这行误用了不存在的局部变量，
                        # 抛 NameError 被外层 except 吞掉，表现是"打完了群里什么都没收到"。
                        turns = result_data.get("turns")
                        deck = str(result_data.get("deck_name") or "未知")
                        text = (
                            f"回合数：{turns if turns is not None else '未知'}"
                            f"｜{self._resolve_bot_name()}用的卡组：{deck}"
                        )
                if verdict:
                    text = f"【游戏王】{verdict}\n{text}"
                if self.config.duel.announce_result:
                    await self.ctx.send.text(text, stream_id)
                if self.config.duel.inject_to_planner:
                    # 事实照样写进上下文，麦麦之后聊天时记得这一局
                    await self._append_to_planner(stream_id, text, group_id)
            else:
                # 没人进来 / 超时 / 取消：这类是收摊通知，不是对局数据，必须可靠送到，
                # 所以不受 announce_result 影响——否则群里会看到房间莫名其妙消失
                await self.ctx.send.text(report, stream_id)
                if self.config.duel.inject_to_planner:
                    await self._append_to_planner(stream_id, report, group_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  后台任务里必须自己吞掉异常，否则会静默死掉
            if self._logger is not None:
                self._logger.exception("播报对局结果失败：群 %s", group_id)
        finally:
            self._rooms.pop(stream_id, None)
            self._stop_room_brain(stream_id)
            await session.stop()
            if self._logger is not None:
                self._logger.info("房间已收摊：群 %s，结束原因 %s", group_id, outcome)

    def _summary_prompt_template(self) -> str:
        """总结提示词模板：配置里写了就用配置那份，留空用内置那份。"""

        return self.config.duel.summary_prompt.strip() or DEFAULT_SUMMARY_PROMPT

    def _check_summary_prompt(self) -> None:
        """启动时校验一次自定义提示词：占位符写错要当场说清楚。

        为什么要有这一步：提示词是配置项，写错一个花括号（例如漏了 ``}``）在真正打第一局之前
        谁也看不见；等到播报时才失败，现象是"打完没有 AI 总结"，很容易被当成模型的问题。
        这里只做一次 `format` 试跑，把名字对不上的占位符直接列进日志。
        """

        template = self.config.duel.summary_prompt.strip()
        if not template or self._logger is None:
            return
        try:
            template.format(
                self_name="麦麦", report="（记录）", verdict="（胜负）", winner="（胜者）", turns="1"
            )
        except (IndexError, KeyError, ValueError) as exc:
            self._logger.error(
                "配置里的对局总结提示词（duel.summary_prompt）没法用：%s。"
                "可用占位符只有 self_name / report / verdict / winner / turns（花括号包起来），"
                "字面量花括号要写成双花括号。改好之前，打完之后不会发 AI 润色那段。",
                exc,
            )

    def _render_summary_prompt(
        self,
        *,
        report: str,
        verdict: str,
        winner: str,
        turns: object,
        self_name: str,
    ) -> str:
        """把模板渲染成真正发给模型的提示词；占位符写错时返回空串并记日志。"""

        template = self._summary_prompt_template()
        try:
            return template.format(
                self_name=self_name,
                report=report,
                verdict=verdict,
                winner=winner,
                turns=str(turns) if turns is not None else "未知",
            )
        except (IndexError, KeyError, ValueError) as exc:
            if self._logger is not None:
                self._logger.error(
                    "对局总结提示词渲染失败（duel.summary_prompt）：%s；这一局不发 AI 润色那段",
                    exc,
                )
            return ""

    async def _write_summary(self, report: str, result_data: Dict[str, object]) -> str:
        """请模型把机读的对局复述写成一段可以直接发群里的人话。

        提示词可配置（`duel.summary_prompt`，占位符见 README），留空用内置那份。
        失败（模型不可用、返回空、被宿主拒绝、提示词写坏）时返回空串，
        由调用方决定怎么交代——这里不做兜底文风，盖住失败只会让人以为播报正常。

        Returns:
            写好的播报文本；没写成时返回空串。
        """

        winner = str(result_data.get("winner_name") or "")
        winner_is_self = result_data.get("winner_is_self")
        self_name = self._resolve_bot_name()
        if winner_is_self is True:
            verdict = f"我方（{self_name}，也就是你）赢了"
        elif winner_is_self is False:
            verdict = f"对方赢了，我方（{self_name}）输了"
        else:
            verdict = "未解析出胜负"
        turns = result_data.get("turns")
        prompt = self._render_summary_prompt(
            report=report, verdict=verdict, winner=winner, turns=turns, self_name=self_name
        )
        if not prompt:
            return ""
        try:
            # ⚠ **必须显式给 max_tokens**：省略会落到宿主给该任务配的默认额度，
            # 而那个模型"先想半天"——短请求的额度会被思考吃光、回复是**空串**
            #（2026-10-06 群里"打完没有 AI 总结"就是这个：配置 `summarize_with_ai` 明明是 true，
            # 日志里只留一条 `生成对局总结返回了空内容`）。
            # 播报只要求 120 字，但思考会先花掉额度；模型不指定，用宿主给该任务配的那只。
            result = await self.ctx.llm.generate(
                prompt=prompt,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
        except Exception:  # noqa: BLE001  模型不可用不该让整条播报消失
            if self._logger is not None:
                self._logger.exception("生成对局总结失败")
            return ""
        if not isinstance(result, dict) or not result.get("success", False):
            if self._logger is not None:
                self._logger.warning("生成对局总结被拒绝：%s", result)
            return ""
        text = str(result.get("response") or "").strip()
        if not text:
            if self._logger is not None:
                self._logger.warning("生成对局总结返回了空内容")
        return text

    def _verdict_line(self, result_data: Dict[str, object]) -> str:
        """机器写的一行胜负判定（放在播报最前面）。

        为什么不靠模型复述：播报交给模型润色，语气、细节都可以由它发挥，
        但"谁赢了"是唯一不能让模型自己推断的事实——实测它推反过不止一次。
        这里只读记录器算出来的 ``winner_is_self``（已按协议语义换算成绝对座位），
        判定不出来就返回空串、不写这一行（宁可不说，也不说反）。
        """

        winner_is_self = result_data.get("winner_is_self")
        if winner_is_self is None:
            return ""
        self_name = self._resolve_bot_name()
        if winner_is_self:
            return f"{self_name} 赢了这一局。"
        return f"{self_name} 输掉了这一局。"

    async def _append_to_planner(self, stream_id: str, text: str, group_id: str) -> None:
        """把对局复述写进 Maisaka 上下文，让机器人后续聊天时记得这一局。"""

        try:
            result = await self.ctx.maisaka.append_context(
                stream_id,
                [{"type": "text", "data": text}],
                visible_text=text,
                source_kind="plugin:yugioh-duel-arena",
            )
        except Exception:  # noqa: BLE001  注入失败不应影响已经发出的播报
            if self._logger is not None:
                self._logger.exception("写入机器人上下文失败：群 %s", group_id)
            return
        # 宿主在参数不合法时返回 success=False 而不是抛异常，这里必须检查，否则会静默丢上下文
        if isinstance(result, dict) and not result.get("success", True):
            if self._logger is not None:
                self._logger.warning("写入机器人上下文被拒绝：%s", result.get("error"))

    # ------------------------------------------------------------------ 辅助

    def _brain_scope_for(self, deck: Optional[StoredDeck]) -> str:
        """这一局实际的问 AI 档位：``deck.brain_scope`` 优先，空串跟随全局。

        返回 ``"off"`` 表示不问（调用方据此不开通道），其余值直接当模型的 ``scope``。
        """

        per_deck = (deck.brain_scope if deck is not None else "").strip()
        if per_deck:
            # 库里的值可能是老版本/手改的，认不出就跟随全局（不拿未知值去当 scope）
            return per_deck if per_deck in BRAIN_MODES else ""
        if not self.config.duel.ai_brain:
            return "off"
        return self.config.duel.brain_scope.strip() or "all"

    def _start_room_brain(
        self, stream_id: str, deck: Optional[StoredDeck] = None
    ) -> Tuple[Optional[Path], Optional[asyncio.Task]]:
        """按配置决定这一局要不要"逐步问 AI"，要就起一个答复任务。

        模型走**插件自带的配置**（``<数据目录>/brain_model.toml``：密钥不进仓库、也不动宿主配置）。
        拿不到模型就**不开这条通道**——否则每次决策都会白等执行器的超时，
        把房间的每回合时限吃光（实测动作数从 12~18 掉到 1~6）。

        要不要问、问到什么程度，**先看这副牌自己的档位**（``/出牌模式`` 设的
        ``deck.brain_scope``），空串才跟随全局配置 ``duel.brain_scope``：
        "脚本本来就能打"的牌不问反而更好，而"脚本走不出来"的牌非问不可。
        """

        scope = self._brain_scope_for(deck)
        if scope == "off":
            if self._logger is not None:
                self._logger.info("这副牌设了「不问 AI」（/出牌模式），本局不开问答通道")
            return None, None
        # 注意：这里必须**相对导入**——插件是按包加载的，绝对导入 train.* / duel.* 会直接崩
        # （实测：ModuleNotFoundError: No module named 'train'）
        from .duel.netguard import guarded_request
        from .train.ai_brain import (
            BrainServer,
            load_brain_model_settings,
            load_combo_guide,
            make_model_decider,
        )

        data_dir = self._data_dir()
        settings = load_brain_model_settings(data_dir)
        if settings is None:
            if self._logger is not None:
                self._logger.warning(
                    "配置开了 ai_brain，但没读到 %s，这一局照脚本打",
                    data_dir / "brain_model.toml",
                )
            return None, None
        card_db = self._card_db
        # 把这副牌的打法数据 + **攻略要点**（展开流程）一起塞进提示词：不然它会把展开链上的
        # 关键卡"省下来"不发（用户实测"连基础展开都断了"就是这么来的）
        deck_playbook = deck.playbook if deck is not None else ""
        deck_name = deck.display_name if deck is not None else ""
        guide = load_combo_guide(data_dir, deck.deck_id) if deck is not None else ""
        # 知识库：按"这一问的局面"临场检索几条（本机 SQLite，毫秒级、不花钱）
        knowledge = None
        if self.config.duel.brain_knowledge:
            knowledge = self._knowledge()
        if deck is not None and self._logger is not None:
            self._logger.info(
                "本局问 AI 的素材：打法数据 %s 字、攻略要点 %s 字、知识库 %s",
                len(deck_playbook),
                len(guide),
                "开" if knowledge is not None else "关",
            )
        # 决策日志：一个实例 = 一局（毫秒级本地写入）。打完由 `_finish_room_brain` 回填胜负——
        # 没有胜负的日志只能看"问了什么"，看不出"这一步到底帮没帮上忙"
        decision_log = (
            DecisionLog(
                knowledge.path,
                arena=f"room:{stream_id[:12]}",
                deck_key=str(deck.deck_id),
                duel_key=f"{stream_id[:12]}-{int(time.time())}",
            )
            if knowledge is not None and deck is not None
            else None
        )
        decide = make_model_decider(
            settings,
            request=guarded_request,
            card_db=card_db,
            logger=self._logger,
            playbook=deck_playbook,
            deck_name=deck_name,
            combo_guide=guide,
            knowledge=knowledge,
            deck_id=deck.deck_id if deck is not None else 0,
            decision_log=decision_log,
            scope=scope,
        )
        prefix = data_dir / "temp" / "brain" / f"room_{stream_id[:12]}_{int(time.time())}.txt"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        server = BrainServer(prefix, decide, logger=self._logger)
        task = asyncio.create_task(server.serve_forever())
        self._brain_tasks[stream_id] = (prefix, task, server, decision_log)
        if self._logger is not None:
            self._logger.info("本局开启逐步问 AI：模型 %s（%s）", settings.model, settings.base_url)
        return prefix, task

    def _finish_room_brain(
        self,
        stream_id: str,
        session: DuelSession,
        result_data: Dict[str, object],
        outcome: str,
    ) -> None:
        """给这一局的决策日志回填胜负——`/复盘` 与 review_decisions 靠它判断干预的对错。

        生命值取**实时局面**那份：记录器的 ``lp_final`` 依赖 ``MSG_LPUPDATE``，
        而本机内核不为掉血发它（会一直是 0），擂台那边踩过同一个坑。
        """

        entry = self._brain_tasks.get(stream_id)
        if entry is None:
            return
        log = entry[3]
        if log is None:
            return
        try:
            winner_is_self = result_data.get("winner_is_self")
            if outcome != OUTCOME_FINISHED or winner_is_self is None:
                result = "unknown"
            else:
                result = "win" if winner_is_self else "loss"
            lp_self = 0
            lp_other = 0
            recorder = session.recorder()
            seat = recorder.self_seat
            if seat in (0, 1):
                field_state = recorder.field_state
                lp_self = int(field_state.players[seat].lp)
                lp_other = int(field_state.players[1 - seat].lp)
            text = duel_outcome_text(
                result=result,
                turns=int(result_data.get("turns") or 0),
                lp_self=lp_self,
                lp_other=lp_other,
            )
            log.finish_duel(text)
        except Exception as exc:  # noqa: BLE001  决策日志绝不能把这一局的播报搅坏
            if self._logger is not None:
                self._logger.warning("回填决策日志结果失败：%s", exc)

    def _record_room_duel(
        self,
        session: DuelSession,
        result_data: Dict[str, object],
        outcome: str,
        *,
        store_path: Optional[Path] = None,
    ) -> None:
        """把真实房间对局也写进结果库（``arena="room"``，与擂台同一个库）。

        **为什么必须落库**：以前只有擂台局进库，真实对局只留下决策日志里的问答——
        于是"改了有没有变好"只能在模拟里看，真实对局没有任何可统计的东西。
        字段口径与擂台**完全一致**（动作数＝召唤＋特召＋效果＋攻击，生命值取实时局面那份），
        这样两边能直接放一起比。

        只记**真打完**的局（中途收摊/没人进来那种不算对局）；任何失败都只记日志——
        写库绝不能把这一局的播报搅坏。
        """

        if outcome != OUTCOME_FINISHED:
            return
        try:
            # 相对导入（见 _start_room_brain 的说明：插件按包加载，绝对导入 train.* 会崩）。
            # DuelOutcome 放在 duel/ 下就是为了这里能安全导入（train/arena.py 用绝对导入，进不来）
            from .duel.duelrecord import DuelOutcome
            from .train.store import DuelStore

            recorder = session.recorder()
            seat = recorder.self_seat
            if seat not in (0, 1):
                return
            players = result_data.get("players") or {}
            self_stats = players.get(str(seat)) or {}
            other_stats = players.get(str(1 - seat)) or {}

            def actions(stats: Dict[str, object]) -> int:
                """与擂台同一个口径：召唤 + 盖放 + 效果 + 攻击。

                **盖放要算进来**（2026-10-06 起，与 ``train/arena.py`` 的 ``actions()`` 保持同步）：
                内核给盖放单发 ``MSG_SET``、不给 summon 报文；漏掉它，"整局只盖牌"的局
                在台账里看起来就像"整局没出牌"。
                """

                return sum(
                    int(stats.get(key) or 0)
                    for key in ("normal_summons", "sp_summons", "sets", "effects", "attacks")
                )

            # 双方都取**游戏内的名字**（记录器按座位给的），别一边用配置昵称、一边用游戏内名——
            # 那样库里的左右两列不是同一套称呼，事后对不上（实测麦麦在游戏内叫「憨憨」）
            self_name = str(self_stats.get("name") or self._resolve_bot_name())
            other_name = str(other_stats.get("name") or "群友")
            winner_is_self = result_data.get("winner_is_self")
            if winner_is_self is True:
                winner = self_name
            elif winner_is_self is False:
                winner = other_name
            else:
                winner = "unknown"
            field_state = recorder.field_state
            left_lp = int(field_state.players[seat].lp)
            right_lp = int(field_state.players[1 - seat].lp)
            deck_style = str(result_data.get("deck_style") or "")
            stored = DuelOutcome(
                left=self_name,
                right=other_name,
                left_seat=seat,
                winner=winner,
                turns=int(result_data.get("turns") or 0),
                duration_seconds=int(result_data.get("duration_seconds") or 0),
                reason=str(result_data.get("reason") or ""),
                actions_left=actions(self_stats),
                actions_right=actions(other_stats),
                lp_left=left_lp,
                lp_right=right_lp,
                sp_summons_left=int(self_stats.get("sp_summons") or 0),
                sp_summons_right=int(other_stats.get("sp_summons") or 0),
                effects_left=int(self_stats.get("effects") or 0),
                effects_right=int(other_stats.get("effects") or 0),
                sets_left=int(self_stats.get("sets") or 0),
                sets_right=int(other_stats.get("sets") or 0),
                # 用卡台账按座位分开（2026-10-07）：`card_usage` 只含我方，对手那份单独落一列
                card_usage=dict(recorder.card_usage),
                card_usage_opponent=dict(recorder.opponent_card_usage),
            )
            store = DuelStore(store_path or (_PLUGIN_ROOT / "temp" / "train" / "arena.db"))
            try:
                store.record(
                    "room",
                    stored,
                    left_style=deck_style or "未记录",
                    right_style="真人",
                )
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001  落库失败绝不能影响播报
            if self._logger is not None:
                self._logger.warning("房间对局落库失败：%s", exc)

    def _stop_room_brain(self, stream_id: str) -> None:
        """房间收摊时停掉答复任务并清掉问答文件。"""

        entry = self._brain_tasks.pop(stream_id, None)
        if entry is None:
            return
        prefix, task, server, log = entry
        task.cancel()
        if log is not None:
            log.close()
        for suffix in (".q", ".a"):
            try:
                Path(str(prefix) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
        if self._logger is not None:
            self._logger.info("逐步问 AI 结束：这一局共答复 %s 次", server.answered)

    def _data_dir(self) -> Path:
        """插件数据目录（宿主约定：``<MaiBot>/data/plugins/<插件 id>/``）。"""

        return Path(self.ctx.paths.data_dir)

    def _knowledge(self) -> Optional[Knowledge]:
        """知识库（只读；库不存在时返回 None，问 AI 就退回"只用静态素材"）。

        懒加载 + 缓存：本机 SQLite 查询是毫秒级，但每局都开一次连接没必要。
        """

        if self._knowledge_cache is None:
            cards_db = self._resolve_cards_cdb()
            knowledge = Knowledge.from_data_dir(
                self._data_dir(), cards_db=cards_db, plugin_root=_PLUGIN_ROOT
            )
            self._knowledge_cache = knowledge if knowledge.available else None
            if self._knowledge_cache is None and self._logger is not None:
                self._logger.info(
                    "还没有知识库（%s）——问 AI 只用攻略要点与打法数据；"
                    "要建的话跑 tools/build_card_facts.py 与 tools/build_deck_plans.py",
                    knowledge.path,
                )
        return self._knowledge_cache

    def _build_session_config(self, stream_id: str) -> SessionConfig:
        """把插件配置映射成会话配置。"""

        paths = self.config.paths
        duel = self.config.duel
        public_host = duel.public_host.strip() or detect_lan_address()
        return SessionConfig(
            ygopro_executable=paths.resolved_ygopro_executable() or Path(paths.ygopro_executable),
            ygopro_dir=paths.resolved_ygopro_dir() or Path(paths.ygopro_dir),
            windbot_executable=self._resolve_windbot_executable(),
            windbot_dir=paths.resolved_windbot_dir() or Path(paths.windbot_dir),
            cards_cdb=self._resolve_cards_cdb(),
            room=RoomSettings(
                no_check_deck=duel.no_check_deck,
                no_shuffle_deck=duel.no_shuffle_deck,
                start_lp=duel.start_lp,
                start_hand=duel.start_hand,
                draw_count=duel.draw_count,
                time_limit=duel.time_limit,
                duel_rule=duel.duel_rule,
                save_replay=duel.save_replay,
            ),
            bot_name=self._resolve_bot_name(),
            windbot_deck=self._fallback_deck_style(),
            in_game_chat=duel.in_game_chat,
            dialog=duel.dialog,
            bot_debug=duel.bot_debug,
            taunt_enabled=duel.taunt_enabled,
            taunt_chance_per_second=float(duel.taunt_chance_per_second),
            taunt_lines=tuple(duel.taunt_lines),
            public_host=public_host or "127.0.0.1",
            public_port=duel.public_port,
            listen_host=duel.listen_host,
            listen_port=duel.listen_port,
            join_timeout=float(duel.join_timeout_seconds),
            max_duration=float(duel.max_duration_seconds),
            on_status=self._on_room_event,
        )

    def _on_room_event(self, event: str, payload: Dict[str, object]) -> None:
        """把房间事件记进日志（不往群里刷消息，避免打扰）。"""

        if self._logger is None:
            return
        if event == "client_joined":
            self._logger.info("有客户端进入房间：%s", payload.get("client"))
        elif event == "client_rejected":
            self._logger.warning("有连接被拒绝：%s（%s）", payload.get("peer"), payload.get("reason"))
        elif event in ("duel_started", "duel_ended"):
            self._logger.info("对局阶段变化：%s", event)

    def _unknown_cards(self, deck: Deck) -> Optional[List[int]]:
        """列出这副卡组里本地卡库查不到的卡；卡库不可用时返回 None。"""

        if self._card_db is None or not self._card_db.available:
            return None
        try:
            return self._card_db.unknown_ids(list(deck.main) + list(deck.extra))
        except CardDatabaseError as exc:
            if self._logger is not None:
                self._logger.warning("核对卡库失败：%s", exc)
            return None

    def _seed_builtin_decks(self) -> None:
        """把 WindBot 自带卡组登记进卡组池（幂等，每次启动都对齐一次）。

        这批卡组本来就配着自己的出牌脚本，是牌力最稳的一档；池子里放上它们，
        群友直接挑或者让机器人随机抽都行。
        """

        if self._deck_pool is None:
            return
        windbot_dir = self.config.paths.resolved_windbot_dir()
        if windbot_dir is None:
            return
        entries = builtin_decks(windbot_dir, logger=self._logger)
        if not entries:
            if self._logger is not None:
                self._logger.warning("没在 %s/Decks 里找到 WindBot 自带卡组，池子里只有投稿卡组", windbot_dir)
            return
        changed, removed = self._deck_pool.seed_builtin_decks(entries)
        if self._logger is not None:
            self._logger.info(
                "内置卡组已登记 %s 副（新增/更新 %s，清理 %s）", len(entries), changed, removed
            )

    def _fallback_deck_style(self) -> str:
        """auto 模式下挑不出相似卡组时的兜底风格。"""

        configured = self.config.duel.windbot_deck.strip()
        if not configured or configured == GENERIC_DECK_STYLE:
            # 投稿卡组用通用脚本；没投稿时机器人用它自带的卡组，那副对应的是默认风格
            return DEFAULT_WINDBOT_DECK
        if configured == AUTO_DECK_STYLE:
            return DEFAULT_WINDBOT_DECK
        return configured

    def _deck_style_for(self, main: Tuple[int, ...], extra: Tuple[int, ...]) -> Tuple[str, str]:
        """给一副投稿卡组挑出牌思路，返回 ``(风格名, 说明文本)``。

        ``auto`` 模式下按卡表相似度从 WindBot 自带卡组里挑最接近的一套——
        投稿卡组没有专属打法，用相近卡组的思路去操作它会明显好于固定套一套无关的思路。
        """

        configured = self.config.duel.windbot_deck.strip()
        if configured and configured not in (AUTO_DECK_STYLE, GENERIC_DECK_STYLE):
            return configured, f"出牌思路按配置固定为「{configured}」"
        if not configured or configured == GENERIC_DECK_STYLE:
            return GENERIC_STYLE_NAME, (
                f"出牌思路用 WindBot 的通用脚本「{GENERIC_STYLE_NAME}」"
                "（原型脚本按自己的卡表写死 combo，换成别的卡表会整局不打，所以投稿卡组默认用通用的）"
            )

        windbot_dir = self.config.paths.resolved_windbot_dir()
        if windbot_dir is None:
            return DEFAULT_WINDBOT_DECK, "没配 WindBot 目录，出牌思路用默认值"
        available = load_available_decks(windbot_dir, logger=self._logger)
        if not available:
            return DEFAULT_WINDBOT_DECK, "没在 WindBot 目录里找到自带卡组，出牌思路用默认值"
        picked = pick_best_match(main, extra, available)
        if picked is None:
            return (
                DEFAULT_WINDBOT_DECK,
                "没找到相似的出牌思路（这副牌和 WindBot 自带的都不像），用默认风格打",
            )
        name, score = picked
        return name, f"出牌思路自动匹配到「{name}」（卡表相似度 {score:.0%}）"

    def _resolve_cards_cdb(self) -> Optional[Path]:
        """定位 cards.cdb：优先用显式配置，其次用 ygopro 目录下的同名文件。"""

        return self.config.paths.resolved_cards_cdb()

    def _missing_paths(self) -> List[str]:
        """列出尚未配置或不存在的关键路径。"""

        missing: List[str] = []
        checks = (
            ("ygopro.exe", self.config.paths.ygopro_executable, self.config.paths.resolved_ygopro_executable()),
            ("ygopro 工作目录", self.config.paths.ygopro_dir, self.config.paths.resolved_ygopro_dir()),
            ("WindBot.exe", self.config.paths.windbot_executable, self.config.paths.resolved_windbot_executable()),
            ("WindBot 工作目录", self.config.paths.windbot_dir, self.config.paths.resolved_windbot_dir()),
        )
        for label, raw, resolved in checks:
            if resolved is None:
                missing.append(f"{label}（未填写）")
            elif not resolved.exists():
                missing.append(f"{label}（路径不存在：{resolved}）")
        return missing

    def _check_ready(self) -> str:
        """开局前的配置自检，返回空字符串表示可以开局。"""

        if self._deck_pool is None:
            return "插件还没加载完，稍后再试。"
        missing = self._missing_paths()
        if missing:
            return "对局功能还没配好：" + "；".join(missing) + "。请在插件配置页补全后重试。"
        public_host = self.config.duel.public_host.strip() or detect_lan_address()
        if not public_host:
            return "探测不到本机对外的地址，请在插件配置里手填「发给群友的服务器地址」。"
        return ""

    def _pick_deck(self, group_id: str, deck_name: str) -> Tuple[Optional[StoredDeck], str]:
        """选一副卡组给 bot 用，返回 ``(卡组, 说明文本)``。

        随机抽取只看随机池：内置卡组登记时就在池子里，投稿卡组要群友显式
        ``/加入随机`` 才参与。随机池空了才退回 WindBot 自带的那副默认卡组。
        """

        if self._deck_pool is None:
            return None, "卡组池还没准备好，这局机器人用它自带的卡组。"

        if deck_name.strip():
            matched = self._find_deck(group_id, deck_name.strip())
            if matched is None:
                return None, f"没找到叫「{deck_name}」的卡组，这局机器人用它自带的卡组。"
            return matched, self._describe_deck_choice(matched, fixed=True)

        fixed = self._deck_pool.fixed_deck()
        if fixed is not None:
            fixed = self._with_current_style(fixed)
            return fixed, self._describe_deck_choice(fixed, fixed=True)

        picked = self._deck_pool.random_pick(group_id)
        if picked is None:
            return None, (
                f"本群的随机池是空的，这局机器人用它自带的卡组「{DEFAULT_WINDBOT_DECK}」。"
                "发 /卡组列表 看有哪些牌，发 /加入随机 <编号> 或 /加入随机 全部 放进随机池。"
            )
        picked = self._with_current_style(picked)
        return picked, self._describe_deck_choice(picked, fixed=False)

    def _with_current_style(self, deck: StoredDeck) -> StoredDeck:
        """按当前配置重新算出牌思路。

        投稿时记录的那份只是当时的快照；出牌思路应该在开局时按当前配置决定，
        这样改了配置之后不必让群友重新投稿。
        """

        # 注意：**开"逐步问 AI"不再需要换脚本**。问答钩子挂在 WindBot 基类上
        # （Game/AI/MaiBotBrain.cs：Executor.AddExecutor + GameAI.OnSelectBattle），
        # 任何出牌脚本都答得到问题。以前这里强制换成 PlanAware，代价很大——实测同一副牌同一对手，
        # 专属脚本 13% 胜 / 16.0 动作，通用脚本 0% / 8.7 动作，等于为了开 AI 白扔掉卡组的脚本。
        # ⚠ 占位风格：入库时没定风格的那批牌写的是 **`windbot_deck='Test'`**（92 副里 17 副，
        # 含用户投稿区的「升辉月」——它的 `picked_style='RaiseMoon'` 才是真风格）。
        # 下面几条"原样返回"的分支（内置牌 / auto-optimize 产物）会把占位值一路带进对局，
        # 于是"用 升辉月 的卡表 + 通用 Test 脚本"去打（实测观感就是"问题很大"）。
        # 所以占位值一律按"还没定风格"处理，强制走 picked_style / generated_script 的解析。
        placeholder_style = deck.windbot_deck.strip() in ("", "Test")
        if deck.is_builtin and not placeholder_style:
            # 内置卡组和它的出牌脚本是配套的，换成别的思路反而打得差
            return deck
        if deck.picked_style:
            # **实测挑出来的**优先：那是真打若干局量出来的（会不会出牌 + 胜率），
            # 比"模型现写、只保证能编译且会出牌"的生成脚本可信度高一个档次
            return dataclasses.replace(deck, windbot_deck=deck.picked_style)
        if deck.generated_script:
            # 已经为这副牌生成并编译过专属脚本，直接用它
            return dataclasses.replace(deck, windbot_deck=deck.generated_script)
        if deck.source_format == "auto-optimize" and not placeholder_style:
            # 优化产物：它记着来源卡组的出牌脚本，按卡表相似度重猜反而会换掉那个已验证的组合
            return deck
        main, extra = self._stored_deck_cards(deck)
        style, _note = self._deck_style_for(main, extra)
        if style == deck.windbot_deck:
            return deck
        return dataclasses.replace(deck, windbot_deck=style)

    def _stored_deck_cards(self, deck: StoredDeck) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
        """从存档的 .ydk 里读回主卡组与额外卡组。

        只用于重新算出牌思路（``auto`` 模式要按卡表比对），读不出来时返回空元组，
        由 :meth:`_deck_style_for` 退回兜底风格。
        """

        try:
            parsed = parse_deck_code(deck.ydk_path.read_text(encoding="utf-8"))
        except (DeckCodeError, OSError) as exc:
            if self._logger is not None:
                self._logger.warning("读取卡组文件失败（%s），出牌思路退回兜底：%s", deck.ydk_path, exc)
            return (), ()
        return parsed.main, parsed.extra

    def _describe_deck_choice(self, deck: StoredDeck, *, fixed: bool) -> str:
        """说明这局用了哪副卡组、以及它的出牌思路来自哪里。"""

        prefix = "按固定设置" if fixed else "随机抽到"
        if deck.is_builtin:
            # 内置卡组的打法就是它自己那套，写「出牌思路：<英文名>」群里只会看糊涂
            style = "，是 WindBot 自带的卡组，打法配套"
        elif deck.windbot_deck == GENERIC_STYLE_NAME:
            # 脚本内部名是 Test，照原样写出来群里看不懂
            style = "，出牌思路：WindBot 通用脚本"
        elif deck.windbot_deck:
            style = f"，出牌思路：{deck.windbot_deck}"
        else:
            style = ""
        return f"这局机器人用{prefix}的「{deck.display_name}」{style}。"

    def _find_deck(self, group_id: str, name: str) -> Optional[StoredDeck]:
        """按名字找卡组（内置卡组与投稿都在池子里）：先精确匹配，再退化为包含匹配。"""

        if self._deck_pool is None:
            return None
        decks = self._deck_pool.list_decks(group_id)
        for deck in decks:
            if deck.display_name == name:
                return deck
        for deck in decks:
            if name in deck.display_name:
                return deck
        return None

    def _auto_deck_name(self, deck: Deck) -> str:
        """没有给名字时，用主力卡名当卡组名。"""

        counts: Dict[int, int] = {}
        for card_id in list(deck.main) + list(deck.extra):
            counts[card_id] = counts.get(card_id, 0) + 1
        if not counts:
            return "未命名卡组"
        top_id = max(counts.items(), key=lambda item: item[1])[0]
        name = self._lookup_card_name(top_id)
        return name or f"卡组{top_id}"

    def _lookup_card_name(self, card_id: int) -> str:
        """查卡名，查不到时返回空字符串。"""

        if self._card_db is None or not self._card_db.available:
            return ""
        try:
            return self._card_db.name(card_id) or ""
        except Exception:  # noqa: BLE001  卡名只是锦上添花，读不到不该影响主流程
            return ""

    def _bot_display_name(self) -> str:
        """机器人当下的显示名：卡组池里的设置优先（``/对局名`` 改的就是它），否则用配置里的。"""

        name = ""
        if self._deck_pool is not None:
            name = (self._deck_pool.get_setting(SETTING_BOT_NAME) or "").strip()
        return name or self.config.duel.bot_name

    async def _invite_loop(self) -> None:
        """空闲时主动冒泡约战（配置 ``duel.invite_enabled`` 打开才发）。

        间隔在 ``invite_min_minutes``~``invite_max_minutes`` 之间随机；下面两种情况直接跳过：

        * 已经有房间在跑（不打扰正在打的局）；
        * 还没在哪个群开过房（``_invite_stream_id`` 为空）——只有调用过本插件工具的群
          才拿得到可用的聊天流，凭空猜一个 stream_id 会发到不存在的地方。
        """

        while True:
            duel = self.config.duel
            low = max(1, min(duel.invite_min_minutes, duel.invite_max_minutes))
            high = max(low, duel.invite_max_minutes)
            # 这里的随机只是"让间隔别太机械"，不是安全用途（不需要密码学随机源）
            await asyncio.sleep(random.uniform(low, high) * 60.0)
            text = self._compose_invite_text()
            if text is None:
                continue
            try:
                await self.ctx.send.text(text, self._invite_stream_id)
                if self._logger is not None:
                    self._logger.info("主动约战：已向 %s 发出邀请", self._invite_stream_id)
            except Exception:  # noqa: BLE001  约战失败不能牵连对局与其它功能
                if self._logger is not None:
                    self._logger.exception("主动约战发送失败")

    def _compose_invite_text(self) -> Optional[str]:
        """该不该主动约战、说什么：**关着、正在打、或还没在哪个群开过房**时返回 None。

        把"该不该发"抽出来是为了能测——循环本身要 sleep 几十分钟，测试等不起。
        """

        duel = self.config.duel
        if not duel.invite_enabled or self._rooms or not self._invite_stream_id:
            return None
        name = self._bot_display_name()
        return (duel.invite_text or "").strip() or f"{name}在，有人想和{name}打一局吗？说一声就开房。"

    async def _persist_room_loop(self) -> None:
        """常驻房：插件在跑，就一直有一副「ygopro + WindBot」等着人连。

        和"群里喊一句才开房"的区别：

        * **端口固定**（配置 ``duel.persist_room_port``）——重启电脑/重启插件后地址不变，能直接连；
        * **一局打完自动再开一副**，进程起崩了 5 秒后重来（所以它是"自启动 + 自愈"，不用人工盯着）；
        * 不登记进 ``self._rooms``：那是"群里这一局"的账本，常驻房进去会占掉
          ``max_concurrent_rooms`` 的额度、也会被 ``/查房`` 当成当前对局——两码事。

        开关（``duel.persist_room``）与端口都在循环里读配置，所以热更新即时生效，不用重启插件。
        """

        while True:
            duel = self.config.duel
            if not duel.persist_room:
                return
            session: Optional[DuelSession] = None
            try:
                deck, _note = self._pick_deck(PERSIST_GROUP, "")
                config = self._build_session_config(PERSIST_STREAM)
                if duel.persist_room_port:
                    config = dataclasses.replace(config, listen_port=duel.persist_room_port)
                session = DuelSession(
                    config,
                    group_id=PERSIST_GROUP,
                    deck=deck,
                    card_db=self._card_db,
                    logger=self._logger,
                )
                info = await session.start()
                message = self._compose_open_message(info, "mdpro3", "（常驻房）")
                # 地址只发到"最近开过房的群"：没开过房就没有可用的聊天流，凭空猜 stream_id 会发错地方。
                # 那种情况下写进插件日志——重启电脑后没有群消息可看时，这是唯一能找到端口与口令的地方。
                if self._invite_stream_id:
                    await self.ctx.send.text(message, self._invite_stream_id)
                elif self._logger is not None:
                    self._logger.info(
                        "常驻房已就绪（还没有可发的群，信息记在这里）：\n%s", message
                    )
                # 等这一局打完（内部会播报结果、落库、写进机器人上下文）
                # ⚠ **结果必须发到真实的聊天流**：`PERSIST_STREAM` 只是"这一局在账本里的名字"，
                # 拿它当 stream_id 发消息等于发到不存在的地方——实测症状就是"打完换房间了、
                # 但总结没发出来"。有开过房的群就发给那个群；没有就只记日志、别假装发出去。
                report_stream = self._invite_stream_id or PERSIST_STREAM
                if report_stream == PERSIST_STREAM and self._logger is not None:
                    self._logger.warning("常驻房打完但还没有可发的群，这一局的结果只写进日志")
                await self._run_room(session, report_stream, PERSIST_GROUP)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  常驻房不能因为一次异常就停掉
                if self._logger is not None:
                    self._logger.exception("常驻房异常，5 秒后重开")
            finally:
                if session is not None:
                    try:
                        await session.stop()
                    except Exception:  # noqa: BLE001  收摊失败不阻塞重开
                        if self._logger is not None:
                            self._logger.warning("常驻房收摊失败，忽略后重开")
            await asyncio.sleep(PERSIST_RETRY_SECONDS)

    def _compose_open_message(self, info: SessionInfo, platform: str, hint: str = "") -> str:
        """开局后发到群里的连接信息。
        ``hint`` 只在**没挑到卡组**时才有内容（"随机池是空的，发 /加入随机 放牌进来"这类）。
        正常抽到一副牌时**不播报用的是哪副**——用户要求别再每次开房提醒一次；
        想知道哪副牌发 ``/卡组详情``。
        """

        lines = [f"【游戏王】{self._bot_display_name()}把房间开好了", info.connection_hint(platform)]
        if hint:
            lines.append(hint)
        host = info.host
        if host in ("127.0.0.1", "localhost"):
            lines.append("注意：当前地址是本机地址，只有和机器人在同一台机器上才能连进来。")
        return "\n".join(lines)

    def _compose_result_message(self, outcome: str, summary: List[str]) -> str:
        """对局结束后发到群里的复述。"""

        body = "\n".join(summary)
        if outcome == OUTCOME_FINISHED:
            return "【游戏王】这局打完了\n" + body
        if outcome == OUTCOME_NO_PLAYER:
            return "【游戏王】房间开好了但一直没人进来，先收摊了。想打随时说一声。"
        if outcome == OUTCOME_TIMEOUT:
            return "【游戏王】这局超时了，先收摊。\n" + body
        return "【游戏王】对局已取消。"

    @staticmethod
    def _tool_result(name: str, content: str) -> Dict[str, Any]:
        """构造工具返回值。"""

        return {"name": name, "content": content}


def create_plugin() -> MaiPlayYgo:
    """创建插件实例。"""

    return MaiPlayYgo()
