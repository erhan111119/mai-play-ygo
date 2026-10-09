"""麦麦玩游戏王（Mai Play YGO）。

一个插件把"打牌"和"查牌"两件事都包了——原「游戏王对局管家」与「游戏王百科检索」
两个插件合并而来，v1.0.0 起对外只有这一个插件：

**对局侧**：群里有人说想打牌 → LLM 调用本插件的工具 → 插件自己起一个对局房间 →
把服务器地址与房间密码发到群里 → 群友用 MDPro3 / YGOMobile 连进来和机器人打一局 →
打完后把结果与过程复述发回群里，并写进 Maisaka 上下文让机器人后续聊天时知道刚才发生了什么。

**百科侧**（`wiki.py` 的 `YugiohWikiTools` 混入）：两个 LLM 工具——查卡、发卡图；
卡图优先用本机客户端的图。（原来还有第三个"自动识别群里的卡组码"的工具，
2026-10-07 用户口径精简掉了：卡组码只走本插件的 `/加卡组` 指令导入。）

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
from typing import Any, Callable, Dict, List, Optional, Tuple

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

import asyncio
import concurrent.futures
import dataclasses
import logging
import sys
import time
import uuid

from .duel.brain_bridge import BrainBridge
from .duel.builtin_decks import builtin_decks
from .duel.cards import CardDatabase, CardDatabaseError
# ⚠ 这里原来还有 `from .duel.knowledge import ...`（AI 打牌的知识库/决策日志）。
# 2026-10-07 用户口径：AI 打牌整条链路去掉，`duel/knowledge.py` 也删了，所以这行没了。
from .duel.deckcode import Deck, DeckCodeError, deck_summary, describe_issues, parse_deck_code
# ⚠ 这里原来还导入 `BUILTIN_GROUP`（"按内置卡组解析某副牌"的伪群号）：
# 它的消费者是已删的 `/优化卡组` `/挑脚本` `/写打法` `/出牌模式`，所以一并去掉。
from .duel.deckpool import DeckPool, DeckPoolError, StoredDeck
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
from .train.runner import TrainingError, TrainingRunner
from .train.store import TrainingStore
from .webui import WebUIServer, resolve_api_key
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

#: 决策层预热最多等多久（秒）。预热只是"把冷启动开销提前付掉"，不该让开局被它拖住；
#: 超时就放弃，正式提问有自己的超时与熔断兜底。
_WARMUP_LIMIT_SECONDS = 8.0

# ⚠ 这里原来有三样与"AI 自动打牌/常驻房"有关的东西，2026-10-07 用户口径精简掉了：
# 常驻房（`persist_room` / `persist_room_port` 配置 + `PERSIST_STREAM` / `PERSIST_GROUP`
# / `PERSIST_RETRY_SECONDS` 三个常量）、计划感知执行器名 `PLAN_AWARE_STYLE`
# （配合 AI 教练/展开流程用的 `PlanAware`）。
# 现在只有"群里有人要打才开房"这一条路径（见 `/开房` 与 `ygo_duel_start`）。

# 对局总结（`duel.summarize_with_ai`）让模型润色时给的输出额度。
# 值给得大是有实测原因的：宿主那只用于该任务的模型会先"想"一大段，额度给小了
# 思考就把它吃光、正文空着回来，看起来像"模型不回话"（2026-10-06 群里就是这个问题）。
SUMMARY_MAX_TOKENS = 16384

# 面板线程等插件事件循环响应的上限（秒）。只用来等"参数校验 + 建记录 + 起后台任务"
# 这种微秒级的活儿，所以给得宽是给人看的：真等超时说明事件循环被卡住了（看门狗会记日志）。
_PANEL_LOOP_TIMEOUT_SECONDS = 10.0

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

# ⚠ 这里原来有两块「问 AI 档位」的数据表：`BRAIN_MODES`（档位名 → 群里的说法/给模型的 scope）
# 与 `BRAIN_MODE_ALIASES`（中文别名）。它们只服务于 `/出牌模式` 与逐步问 AI 的决策通道，
# 2026-10-07 用户口径把 AI 打牌整条链路去掉后就没有消费者了，连同两张表一起删掉。

# 插件根目录（配置里的相对路径按它解析）
_PLUGIN_ROOT = Path(__file__).resolve().parent

# ⚠ 这里原来还有 `DEFAULT_TRAINING_ROUNDS` / `MAX_TRAINING_ROUNDS`（`/训练` 的轮数上下限）：
# 训练/调优整条链路已按 2026-10-07 用户口径删除，这两个常量随之去掉。


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.4.0", description="配置版本")
    # 版本号提升是有意的：1.2.0 起新增 `[llm]`（三个用途的模型与超时）、`[training]`（训练功能）、
    # `[webui]`（插件自带面板）三节，并把 `duel.brain_model` / `duel.brain_timeout_ms`
    # 移成 `llm.decision_model` / `llm.decision_timeout_ms`（老键会被静默忽略）。
    # 1.3.0 起 `[llm]` 多一个 `training_script_max_tokens`（写脚本每批的输出上限），
    # 老配置没有这个键时按默认值 4096 走。
    # 1.4.0 起 `llm.decision_model` 的默认值从 `deepseek-chat` 改成空串（跟宿主的 utils 任务走）：
    # 把一个厂商模型名当出厂默认，在别的机器上那个名字不存在，决策层一上来就必然失败。


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


class LlmConfig(PluginConfigBase):
    """模型配置：这一节**只写模型名**——填宿主 `config/model_config.toml` 里那个模型的 `name`
    （`[[models]]` 里可以随意命名的那个字段），**不是 `model_identifier`**：
    后者是发给供应商的标识符；两者常常不一样（`name` 是你自己起的名字，identifier 是发给供应商的那个串）。
    写错会在调用时被宿主拒掉（`未找到名为 'xxx' 的模型`），插件把原因原样记进日志与训练记录。

    三个用途各管一件事，互不借用：

    * **总结**——打完一局把机读复述写成一段人话（`duel.summarize_with_ai` 打开时才用）；
    * **决策**——阻抗决策层在对手回合里问「这张无效卡该指哪只怪」；
    * **训练**——训练功能里的 combo 推演、写脚本、复盘与写结论。

    模型名一律留空＝跟宿主的默认插件任务走：宿主把插件的默认任务定为 `utils`，
    也就是 `[model_task_config.utils]` 那一组模型（不是"专门给插件配的一只"）。
    超时时间由插件自己执行（宿主那侧没有超时参数），超时只影响这一次调用，不影响对局本身。
    """

    __ui_label__ = "模型"
    __ui_icon__ = "sparkles"
    __ui_order__ = 3

    summary_model: str = Field(
        default="",
        description=(
            "【对局总结】用哪只模型。留空＝跟宿主的 utils 任务走。"
            "这活儿是「把字段列表写成一段人话」，对智力要求低——胜负那一行由插件自己写在"
            "播报最前面，模型只负责润色，所以哪只都行"
        ),
    )
    summary_timeout_ms: int = Field(
        default=60000,
        description=(
            "【对局总结】等模型的上限（毫秒，默认 60000）。"
            "为什么给这么宽：总结的额度是 16384 token，思考型模型会先想一大段，"
            "给短了会稳定超时——超时的后果是播报退化成原始复述，群里看起来就是「没有 AI 总结」"
        ),
    )
    decision_model: str = Field(
        default="",
        description=(
            "【AI 决策】用哪只模型。**推荐小体量、不思考的那种**（本机实测 `deepseek-chat` 关掉思考后 0.58~0.93 秒）。"
            "实测对手回合的等待预算一共只有 15 秒，一次会思考的答复要 13 秒；"
            "而 `deepseek-chat` 关掉思考后是 0.58~0.93 秒、零思考 token、答复就是干净的序号。"
            "⚠ 换模型要同时看两件事：模型名，以及 `model_config.toml` 里那条"
            "`extra_params = {thinking = {type = \"disabled\"}}`——思考型模型会把额度全花在"
            "思考上、`response` 是空串（拉高额度解不了）。换完用 `tools/brain_model_probe.py` 量一遍"
        ),
    )
    decision_timeout_ms: int = Field(
        default=2500,
        description=(
            "【AI 决策】WindBot 等答复的上限（毫秒，默认 2500）。"
            "Python 侧的等模型上限会自动取「这个值再减 400 毫秒」，好让超时由 Python 先发现并记账。"
            "实测模型答复 0.6~0.9 秒，所以 2500 是「够用且不会拖住对局」的余量；"
            "调大之前想清楚：对手回合的等待预算一共只有 15 秒"
        ),
    )
    decision_style: str = Field(
        default="aggressive",
        description=(
            "【AI 决策·档位】控制模型交阻抗的**幅度**，三档：`conservative`（保守：有疑问就不交、"
            "留着以后）/ `normal`（平常：值得拦才拦）/ `aggressive`（激进：能拦就拦、宁可早一点）。"
            "中英文都收（也认「保守 / 平常 / 激进」），写别的按 normal 处理并在日志里说一声。"
            "**只改提示词，不用重编译 exe**——候选卡仍由内核给出，再激进也不会做出非法动作。"
            "⚠ 一正一负的实测：同一份保守口径在「手坑泛滥、没有展开要保护」的合成牌组上是 +13 个点，"
            "在真实卡组「全盛俱舍」上是 -5.6/-9.4 个点（该断就得断，保守就是亏的）"
            "——档位要按牌组调，用 `tools/brain_ab.py` 各档各量一遍再定"
        ),
    )
    training_model: str = Field(
        default="",
        description=(
            "【训练功能】用哪只模型。训练要它读卡文推 combo、复盘录像、写结论——"
            "**建议用一只聪明点的**（和上面「决策」那只相反，那只要求快而不要求聪明）。"
            "留空＝跟宿主的 utils 任务走"
        ),
    )
    training_timeout_ms: int = Field(
        default=25000,
        description=(
            "【训练功能】单次等模型的上限（毫秒，默认 25000）。"
            "**给不大**：宿主对插件的单次模型调用有 30 秒硬超时（`cap.call`，插件改不了，"
            "实测原文是 `[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`），写比它大没有意义。"
            "插件把上限设在 25 秒，是为了让失败由插件先发现，并在记录里写清"
            "「是模型太慢」以及该换哪只模型；训练任务是后台任务，失败一条不影响对局"
        ),
    )
    training_script_max_tokens: int = Field(
        default=4096,
        description=(
            "【写脚本】每次让模型写多少 token（默认 4096）。脚本是**分批**写的："
            "每批 8 张卡的处理函数、一次模型调用写一批，所以这个数决定的是"
            "「一批能写多细」，不是整份脚本的长度上限（脚本总长＝批数 × 每批）。"
            "想让它写得更细可以调大，但**别超 8000**：宿主对插件的单次调用有 30 秒硬超时，"
            "写不完就整批白写（宁可多分几批）。这个额度用满时输出约 200~300 行 C#"
        ),
    )


class TrainingConfig(PluginConfigBase):
    """训练功能（插件里的「研究台」）：面板上就四件事——卡组互打、编写脚本、卡组迭代、复盘优化。

    这一节只放**开关与放东西的地方**——用哪只模型在上面的「模型」节里（`training_model`）。
    """

    __ui_label__ = "训练功能"
    __ui_icon__ = "flask-conical"
    __ui_order__ = 5

    enabled: bool = Field(
        default=True,
        description=(
            "是否启用训练功能。关掉后面板里的训练页只展示历史记录，不受理新任务"
            "（对局侧完全不受影响）"
        ),
    )
    workspace: str = Field(
        default="",
        description=(
            "训练工作目录：擂台数据库、训练日志、推演结果都放这儿。"
            "留空＝插件数据目录下的 `train/`。想接着用旧插件的擂台数据，就把它指到那个目录"
        ),
    )
    max_duels_per_run: int = Field(
        default=60,
        description=(
            "一次擂台最多打多少局（逐局交替座位，最多 200）。"
            "⚠ 这个数字是「一次任务的上限」，不是「判定强弱要多少局」——"
            "实测同一套构筑重测会出现 0/60 与 8/20 并存的噪声，所以判强弱要 ≥80 局/腿，"
            "局数不够时训练报告里只会说「机制有没有坏」，不会说谁强"
        ),
    )


class WebUiConfig(PluginConfigBase):
    """插件自带的控制面板（`webui.py`）。

    面板是独立于麦麦 WebUI 的一个小 HTTP 服务，只为了把「群里说不清的东西」摊开看：
    卡组池、日志、训练任务与结果。**新功能不往群里加命令**，都在这儿。
    """

    __ui_label__ = "面板"
    __ui_icon__ = "monitor"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="是否随插件一起启动面板")
    host: str = Field(
        default="127.0.0.1",
        description=(
            "监听地址。默认只监听本机（面板能看到卡组池与日志，不该默认对外）。"
            "想让手机或别的机器访问再改成 `0.0.0.0`——改之前先确认密钥足够长，并且"
            "端口别直接暴露在公网上"
        ),
    )
    port: int = Field(
        default=17911,
        description=(
            "监听端口。被别的程序占用时面板启动失败并记一条 warning，对局功能不受影响；"
            "换一个没被占用的端口即可"
        ),
    )
    api_key: str = Field(
        default="",
        description=(
            "面板密钥。留空＝按「环境变量 `YGO_WEBUI_KEY` → 自动生成」的顺序取："
            "自动生成的密钥会写到插件数据目录的 `webui_key.txt`，登录页会告诉你去看那个文件。"
            "**别把密钥提交进版本库**"
        ),
    )


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
    # ⚠ 这里原来有两项：`persist_room`（插件一启动就拉一副 ygopro + WindBot 并一直保持）
    # 与 `persist_room_port`（常驻房的固定端口）。2026-10-07 用户口径精简掉了常驻房：
    # 现在只有"群里有人说要打才开房"这一条路径（见 `/开房` 与 `ygo_duel_start`）。
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
    # ⚠ 这里原来有一整片"AI 打牌 / 自动调优"的配置：`invite_enabled`（空闲主动约战）+
    # `invite_min_minutes` / `invite_max_minutes` / `invite_text`、`ai_plan_coach`（AI 教练）、
    # `ai_brain`（逐步问 AI）、`brain_knowledge`（临场检索知识库）、`brain_scope`（问 AI 范围）、
    # `ai_deck_plan`（导入卡组后写展开流程）、`train_model`（训练用哪个模型）。
    # **2026-10-07 用户口径：只留核心（开房打牌 / 导卡组 / 随机池 / 查房 / 查卡发图）**，全删了。
    # （更早还删过 `script_max_tokens` / `script_model`：对局总结的额度仍写死在
    # `_write_summary` 里（`SUMMARY_MAX_TOKENS`），但**模型与超时已在 2026-10-08 移到 `[llm]` 节**：
    # `llm.summary_model` / `llm.summary_timeout_ms`。）

    # ---- 阻抗决策层（2026-10-08）--------------------------------------------------------
    #
    # 这一片不是上面那套"逐步问 AI"的回归，范围完全不同，**只在阻抗时点问**：
    #   * 展开期一步都不问——C# 侧的入口按"对手回合 + 这张是阻抗卡表里的卡"卡死
    #     （`NegateDecision.IsNegateCard`），是结构性保证，不靠提示词里写"展开时别管"；
    #   * 只用**不思考的快模型**：实测对手回合的等待预算只有 15 秒，而一次"会思考"的答复
    #     要 13 秒（当年 40 局里有 5 局是响应窗口等太久被内核判超时输掉的）；
    #   * 答不上来就什么都不写，WindBot 按出牌脚本继续（连续 3 次熔断本局的问答）。
    #
    # 一局会问多少次：实测 8 局真实对局，**"对手回合里我方发动的连锁"平均 10.8 次/局**
    # （范围 3~21），这是决策层调用次数的上界。所以"快"这件事是有余量的——
    # 真正要小心的是单次等待，不是总次数。
    brain_enabled: bool = Field(
        default=True,
        description=(
            "【阻抗决策层】是否让模型决定「这一张无效卡该指向对面哪只怪」。**默认开**。"
            "风险最低的一半：它只在「脚本本来就要交这张无效卡」的前提下改目标，不会让脚本少交一张牌；"
            "模型在这里有真信息优势（脚本只认卡号，模型认识卡文）。"
            "2026-10-08 实测：链路与模型都验过（`deepseek-chat` 关思考 0.58~0.93 秒、答复干净、"
            "零思考 token），一局只问十次左右，等待挤得进对手回合的 15 秒预算。"
            "关掉＝完全按出牌脚本自己的判据选目标（改动前的老口径）"
        ),
    )
    brain_negate_gate: bool = Field(
        default=True,
        description=(
            "【阻抗决策层·决策本身】是否让模型决定「对面发动效果时，我方能发的这几张里发哪张 / "
            "都不发」。`chain_choice` 那一问同时回答了「要不要发」与「该发谁的效果」；"
            "选中的若是无效系，还会再问一次「该去针对谁」（那一问由 `brain_enabled` 控制）。"
            "关掉＝完全按出牌脚本的注册顺序决定谁先上（改动前的老口径：第一条说 yes 的规则获胜）。"
            "⚠ **默认关，是量出来的**（2026-10-08 镜像 A/B）：合成手坑牌组上 300 局 62.3%"
            "（区间 56.7~67.6），但**真实卡组「全盛俱舍」上 160 局只有 44.4%**"
            "（区间 36.9~52.1；把它真动过手的那 97 局单看是 39.2%）——合成那副牌是"
            "「20 手坑 + 20 白板大怪」，「留着不交」在那里天然占便宜，不代表真实卡组。"
            "要开就按牌组分别量（`tools/brain_ab.py`），只在对它有利的牌组上打开"
        ),
    )
    # ⚠ 这里原来有 `brain_model` / `brain_timeout_ms` 两项（决策层用哪只模型、等多久）。
    # 2026-10-08 用户口径：**模型与超时统一收进 `[llm]` 节**——决策层那两项现在是
    # `llm.decision_model` / `llm.decision_timeout_ms`。老配置文件里残留的这两个键会被
    # 静默忽略（`PluginConfigBase` 是 `extra="ignore"`），不会报错。
    brain_max_tokens: int = Field(
        default=256,
        description=(
            "【阻抗决策层】答复的额度上限。答复只有「一个序号」或「yes / no;理由」两种，"
            "**给小一点反而更稳**——额度大了会诱使模型先想半天（实测思考型模型会把 256 全花在思考上、"
            "`response` 留空；关掉思考的模型只花 1 个 token）"
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
    """插件配置总表（麦麦玩游戏王）。

    节的顺序就是配置页里的顺序（各节自己还带 `__ui_order__`）：
    插件 → 运行环境 → 对局 → 模型 → 百科检索 → 训练功能 → 面板。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    duel: DuelConfig = Field(default_factory=DuelConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    webui: WebUiConfig = Field(default_factory=WebUiConfig)


class ActiveRoom:
    """一个进行中的房间及其完成通知任务。"""

    def __init__(
        self,
        session: DuelSession,
        stream_id: str,
        group_id: str,
        task: asyncio.Task,
        deck_name: str = "",
        deck_id: int = 0,
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
        self.deck_id = deck_id
        """这一局用的是卡组池里哪一副（0＝没对上池子，用的是自带卡组）。

        对局结束后要按它写训练记录：「复盘优化」是按**这一副牌**取最近几局的，
        光有卡组名匹配不可靠（池子里真有重名的牌，比如三副"码丽丝111"）。
        """


class MaiPlayYgo(YugiohWikiTools, MaiBotPlugin):
    """麦麦玩游戏王：对局管家（开房/播报/查房）+ 百科检索（查卡/发卡图）。

    继承顺序有意为之：`YugiohWikiTools` 在前（它的 `@Tool` 方法与本体一起被
    `dir(instance)` 采集到），`MaiBotPlugin` 在后（提供 `ctx` / `config` / 生命周期）。
    """

    config_model = MaiPlayYgoConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        self._deck_pool: Optional[DeckPool] = None
        self._card_db: Optional[CardDatabase] = None
        self._retired_card_dbs: List[CardDatabase] = []
        """配置热更新换下来、但**还有房间在用**的旧卡库（等房间空了再关）。

        房间的 `DuelSession` 与决策层 `BrainBridge` 都是直接引用实例（不是回调），
        当场 close 会让它们的下一次查卡名/查卡文抛
        `sqlite3.ProgrammingError: Cannot operate on a closed database`。
        """
        self._rooms: Dict[str, ActiveRoom] = {}
        # ⚠ 这里原来有一批"重活"与 AI 打牌的句柄：`_knowledge_cache`（知识库缓存）、
        # `_background_tasks`（导入卡组后写展开流程的后台任务）、`_training_task` / `_optimize_task`
        # / `_pick_task` / `_playbook_task`（训练·优化·挑脚本·写打法四条批任务的互斥锁）、
        # `_brain_tasks`（房间的问 AI 答复任务）。2026-10-07 用户口径只留核心功能，全部删掉。
        # 2026-10-08 的**阻抗决策层**是另一回事，所以这里重新有了一份句柄：每个房间一条
        # `(bridge, task)`，房间收摊时一起停（`_stop_room_brain`）。
        self._brains: Dict[str, Tuple[BrainBridge, "asyncio.Task[None]"]] = {}
        # 面板与训练功能（2026-10-08 新增）：面板是本插件自己的 HTTP 服务，
        # 训练功能是"把 tools/ 下那批命令行脚本接进面板"的那一层，两个都在 on_load 起、
        # on_unload 同步关（卸载只有 5 秒预算，见 on_unload 的说明）。
        self._webui: Optional[WebUIServer] = None
        #: 当前面板是按哪份 webui 配置起的（配置热更新时用来判断"要不要重启面板"）
        self._webui_settings: Dict[str, Any] = {}
        self._train_store: Optional[TrainingStore] = None
        self._train_runner: Optional[TrainingRunner] = None
        # 面板的 HTTP 线程要把"起任务/停任务"送回插件自己的事件循环执行，所以这里记下它。
        # 在 on_load 里取——那时拿到的就是宿主跑插件协程的那条循环。
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._logger: Optional[logging.Logger] = None

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """准备卡组池与卡牌数据库，并检查外部程序路径是否配好。"""

        self._logger = self.ctx.logger
        self._loop = asyncio.get_running_loop()
        self._deck_pool = DeckPool(
            Path(self.ctx.paths.data_dir),
            default_windbot_deck=self.config.duel.windbot_deck or DEFAULT_WINDBOT_DECK,
        )
        self._card_db = CardDatabase(self._resolve_cards_cdb())
        self._seed_builtin_decks()
        # ⚠ 这里原来还会起两个常驻循环：`_invite_loop`（空闲主动约战）与 `_persist_room_loop`
        # （常驻房，插件一启动就拉一副 ygopro + WindBot 一直等着）。2026-10-07 用户口径把
        # 约战与常驻房都去掉了，现在只在群里有人要打时才开房。
        self._logger.info("游戏王对局管家已加载，数据目录 %s", self.ctx.paths.data_dir)
        if sys.platform != "win32":
            # 非 Windows 上只有百科那半边能用：这里先明说，别让部署者等到有人开房才发现
            #（`_check_ready` 也会拦，但那时是群里看到报错，日志里看不到原因）
            self._logger.warning(
                "当前平台 %s 不是 Windows：对局功能（ygopro.exe / WindBot.exe）不可用，"
                "开房会被拒绝；查卡与发卡图不受影响。",
                sys.platform,
            )
        # 把生效中的关键配置打进日志：内网穿透时"闸门端口是否真的固定了"全靠这一行确认
        self._log_effective_config()
        # 自定义的对局总结提示词先试渲染一次：占位符写错要现在就说，别等打完一局才发现没总结
        self._check_summary_prompt()
        missing = self._missing_paths()
        if missing:
            self._logger.warning(
                "以下路径尚未配置，对局功能不可用，请在插件配置页补全：%s", "；".join(missing)
            )
        # 面板与训练功能：放在最后起——它们都只读"已经准备好"的东西（卡组池、卡库），
        # 而且起不来也不该拦住对局那半边（起失败的细节各自记日志）。
        self._restart_train_runner()
        self._restart_webui()

    # ------------------------------------------------------------------ 面板与训练功能

    def training_store(self) -> Optional[TrainingStore]:
        """训练记录库（没启用训练功能时是 None；面板据此决定显示什么）。"""

        return self._train_store

    def training_runner(self) -> Optional[TrainingRunner]:
        """训练执行器（没启用训练功能时是 None）。"""

        return self._train_runner

    def schedule_training_start(self, kind: str, params: Dict[str, Any]) -> Any:
        """面板线程起任务用的入口：把请求送回插件的事件循环，等参数校验做完再返回。

        为什么必须回事件循环：任务的执行体是协程（要 `asyncio.create_task` 起子进程、
        要 await 模型），而面板的 HTTP 处理器跑在自己的线程里。跨线程直接用
        `run_coroutine_threadsafe` 是标准做法，唯一的注意点是**别在这里等太久**——
        被等的活儿只有"参数校验 + 建记录 + create_task"，真正的对局在后台跑，
        所以这里给 10 秒已经非常宽裕（等不到就报错，绝不假装任务起来了）。
        """

        runner = self._train_runner
        if runner is None:
            raise TrainingError(
                "训练功能没启用：检查配置 training.enabled，以及插件日志里「训练功能已就绪」那行"
            )
        return self._run_on_plugin_loop(runner.start(kind, params))

    def schedule_training_stop(self) -> bool:
        """面板线程停任务用的入口（杀进程树最长几秒，所以超时给得比启动宽）。"""

        runner = self._train_runner
        if runner is None:
            return False
        return bool(self._run_on_plugin_loop(runner.stop()))

    def schedule_deck_settings(
        self, deck_id: int, *, in_random: Optional[bool] = None, brain_scope: Optional[str] = None
    ) -> str:
        """面板上改一副牌的设置（随机池开/关、AI 决策档位）。

        和卡组删除/随机池一样：卡组池的 sqlite 连接建在插件的事件循环上，
        **面板线程不能直接碰**，所以绕回循环里执行并同步等结果。
        """

        return str(
            self._run_on_plugin_loop(
                self._deck_settings(deck_id, in_random=in_random, brain_scope=brain_scope)
            )
        )

    async def _deck_settings(
        self, deck_id: int, *, in_random: Optional[bool], brain_scope: Optional[str]
    ) -> str:
        """真正改设置的那一步（在插件循环里跑）。"""

        pool = self._deck_pool
        if pool is None:
            raise TrainingError("卡组池没准备好（插件正在重载？稍后再试）")
        changed: List[str] = []
        if in_random is not None:
            if not pool.set_in_random(deck_id, in_random):
                raise TrainingError(f"卡组池里没有编号 {deck_id} 的卡组")
            changed.append(f"随机池{'开' if in_random else '关'}")
        if brain_scope is not None:
            # 档位在写入时就校验：读的时候认不出的值会当作"跟随全局"（老库里的历史值照旧能读），
            # 但面板写进来的错值必须当场报出来，否则用户点了设置却什么都没改，还看不出来
            scope = str(brain_scope).strip().lower()
            if scope and scope not in self._BRAIN_SCOPE_SWITCHES:
                options = "、".join(("跟随全局", *self._BRAIN_SCOPE_SWITCHES))
                raise TrainingError(f"不认识的 AI 决策档位：{brain_scope}（可选：{options}）")
            if not pool.set_brain_scope(deck_id, scope):
                raise TrainingError(f"卡组池里没有编号 {deck_id} 的卡组")
            changed.append(f"AI 决策 {scope or '跟随全局'}")
        if self._logger is not None:
            self._logger.info("面板改了卡组 #%s：%s", deck_id, "、".join(changed) or "（没改什么）")
        return "已保存：" + "、".join(changed)

    def schedule_deck_action(
        self, action: str, deck_id: int, *, in_random: bool = False, group_id: str = ""
    ) -> Any:
        """面板线程改卡组用的入口（加入/移出随机池、删除）。

        为什么要绕回事件循环：卡组池的 sqlite 连接是 `on_load` 里在插件循环上建的，
        **面板线程不能直接用它**（跨线程共用一个 sqlite 连接是未定义行为，Windows 上
        表现为随机报错或直接崩）。所以这里包成协程丢回去执行，并同步等结果。
        """

        return self._run_on_plugin_loop(
            self._deck_action(action, deck_id, in_random=in_random, group_id=group_id)
        )

    async def _deck_action(
        self, action: str, deck_id: int, *, in_random: bool = False, group_id: str = ""
    ) -> str:
        """在插件循环里执行一次卡组操作，返回给用户看的一句话。"""

        pool = self._deck_pool
        if pool is None:
            raise TrainingError("卡组池没准备好（插件正在重载？稍后再试）")
        if action == "random":
            if not pool.set_in_random(deck_id, in_random):
                raise TrainingError(f"卡组池里没有编号 {deck_id} 的卡组")
            if self._logger is not None:
                self._logger.info("面板把 #%s %s了随机池", deck_id, "加入" if in_random else "移出")
            return f"#{deck_id} 已{'加入' if in_random else '移出'}随机池"
        if action == "delete":
            try:
                removed = pool.remove(group_id, deck_id)
            except DeckPoolError as exc:
                # 内置卡组的 .ydk 是 WindBot 自己的文件，删不得——把原因原样抛给面板
                raise TrainingError(str(exc)) from exc
            if not removed:
                raise TrainingError(f"卡组池里没有编号 {deck_id} 的卡组")
            if self._logger is not None:
                self._logger.info("面板删除了卡组 #%s", deck_id)
            return f"已删除 #{deck_id}"
        raise TrainingError(f"不认识的卡组操作：{action}")

    def _run_on_plugin_loop(self, coro: Any) -> Any:
        """把协程送回插件的事件循环执行并等结果（面板线程调用）。"""

        loop = self._loop
        if loop is None or loop.is_closed():
            raise TrainingError("插件的事件循环不可用（插件正在重载？稍后再试）")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=_PANEL_LOOP_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TrainingError(
                f"等插件的事件循环响应超过 {_PANEL_LOOP_TIMEOUT_SECONDS:.0f} 秒："
                "这一步没有完成，看插件日志里有没有「事件循环卡顿」那行"
            ) from exc

    def _training_workspace(self) -> Path:
        """训练工作目录（配置留空＝数据目录下的 `train/`）。"""

        raw = (self.config.training.workspace or "").strip()
        if not raw:
            return Path(self.ctx.paths.data_dir) / "train"
        path = Path(raw)
        return path if path.is_absolute() else (Path(self.ctx.paths.data_dir) / path)

    def _windbot_dirs_for_training(self) -> Tuple[Optional[Path], Optional[Path]]:
        """写脚本要用的两个目录：``(WindBot 源码树, WindBot 运行目录)``。

        源码树没配就返回 ``(None, None)``——训练层会据此把"写脚本"标成不可用并说清原因，
        而不是等用户点了才报一个看不懂的错。
        """

        paths = self.config.paths
        return paths.resolved_windbot_src_dir(), paths.resolved_windbot_dir()

    def _record_generated_script(self, deck_id: int, style_name: str) -> None:
        """把"这副牌该用哪个出牌脚本"写回卡组池。

        这一步不能省：写好并编译通过的脚本，只有在卡组记录里被记成 `generated_script`
        才会真的被对局用上（`generated_script` 优先于 `picked_style` 与 `windbot_deck`）。
        不写回池子，就是"写了一份谁都不用的脚本"。
        """

        if self._deck_pool is None or not deck_id:
            return
        self._deck_pool.set_generated_script(deck_id, style_name)
        if self._logger is not None:
            self._logger.info("卡组 #%s 的出牌脚本已设为 %s", deck_id, style_name)

    async def _training_generate(self, prompt: str, model: str, max_tokens: int) -> str:
        """训练功能要用模型时的统一出口：带上配置里的超时，返回纯文本。

        超时**由这里执行**（宿主那侧没有超时参数）：训练是后台任务，模型卡住时必须能变成
        一条失败的记录，而不是让任务永远挂在"跑着"的状态上。失败一律抛异常——
        训练报告是给人看的，"模型没答上来"必须写在记录里，不能拿一段编的结论糊过去。
        """

        llm = self.config.llm
        result = await asyncio.wait_for(
            self.ctx.llm.generate(
                prompt=prompt,
                model=model.strip(),
                temperature=0.2,
                max_tokens=int(max_tokens),
            ),
            timeout=max(float(llm.training_timeout_ms) / 1000.0, 1.0),
        )
        if not isinstance(result, dict) or not result.get("success", False):
            raise RuntimeError(f"模型请求被拒绝：{result}")
        return str(result.get("response") or "").strip()

    def _restart_train_runner(self) -> None:
        """（重）建训练功能那一层：记录库 + 执行器。

        配置热更新时也走这里，但**只有"真的动到执行器"的改动才重建**：工作目录、
        单次局数上限、启用/关闭。其余配置（改个总结模型、改面板端口、改挑衅台词……）
        只把模型名推过去、**保留正在跑的任务**——擂台一跑就是几十分钟，
        为一次无关的配置保存把它掐掉，用户会以为"这功能自己崩了"（实测踩过）。
        工作目录变了必须重建：换了目录还往旧目录写结果才是更坏的结果。
        """

        workspace = self._training_workspace()
        max_duels = int(self.config.training.max_duels_per_run)
        current = self._train_runner
        if current is not None and self.config.training.enabled:
            if current.workspace == workspace and current.max_duels == max_duels:
                current.set_training_model(self.config.llm.training_model)
                # 写脚本的额度也是"改了就生效"的一项：它不改变任务的身份，没必要重建执行器
                current.set_script_max_tokens(self.config.llm.training_script_max_tokens)
                if self._logger is not None:
                    self._logger.debug("训练功能的配置没动到执行器，保留正在跑的任务")
                return
        if current is not None:
            current.stop_now()
            self._train_runner = None
        if not self.config.training.enabled:
            self._train_store = None
            self._logger.info("训练功能已在配置里关闭：面板只读历史，不受理新任务")
            return
        self._train_store = TrainingStore(workspace / "training.db")
        interrupted = self._train_store.mark_interrupted()
        if interrupted:
            # 上一次卸载（或插件重载）砍掉的运行中任务：库里还写着"跑着"，其实早没了。
            self._logger.warning("有 %s 条训练任务在上次关闭时被打断，已标记为失败", interrupted)
        self._train_runner = TrainingRunner(
            plugin_root=Path(__file__).resolve().parent,
            workspace=workspace,
            deck_db_path=Path(self.ctx.paths.data_dir) / "deck_pool.db",
            store=self._train_store,
            generate=self._training_generate,
            # 卡库实例会被配置热更新换掉，所以给回调而不是给实例
            card_db=lambda: self._card_db,
            active_rooms=lambda: len(self._rooms),
            logger=self._logger,
            max_duels=int(self.config.training.max_duels_per_run),
            windbot_dirs=self._windbot_dirs_for_training,
            record_script=self._record_generated_script,
        )
        self._train_runner.set_training_model(self.config.llm.training_model)
        self._train_runner.set_script_max_tokens(self.config.llm.training_script_max_tokens)
        self._logger.info(
            "训练功能已就绪：工作目录 %s｜训练模型 %s｜写脚本额度 %s token/批｜单次擂台上限 %s 局",
            workspace,
            self.config.llm.training_model.strip() or "（跟宿主 utils 任务）",
            self.config.llm.training_script_max_tokens,
            self.config.training.max_duels_per_run,
        )

    def _restart_webui(self) -> None:
        """（重）起面板。端口被占用只在日志里说一声，不影响插件其余部分。

        **只在面板自己的配置变了才重建**（host / port / 密钥 / 开关）：现在配置可以在
        面板里改，而宿主每次保存配置都会推一次热更新——每改一项就把面板重启一次，
        用户那边看到的就是"点保存 → 页面打不开一下"，很难受。别的配置（模型、挑衅台词……）
        对面板没影响，原样留着就行。
        """

        webui = self.config.webui
        wanted = {
            "enabled": bool(webui.enabled),
            "host": str(webui.host or "127.0.0.1").strip(),
            "port": int(webui.port),
            "api_key": str(webui.api_key or "").strip(),
        }
        if self._webui is not None and self._webui_settings == wanted:
            if self._logger is not None:
                self._logger.debug("面板配置没变，保留正在服务的那一个")
            return
        if self._webui is not None:
            self._webui.stop_now()
            self._webui = None
        if not webui.enabled:
            self._webui_settings = wanted
            self._logger.info("面板已在配置里关闭（webui.enabled = false）")
            return
        api_key, key_source = resolve_api_key(webui.api_key, Path(self.ctx.paths.data_dir))
        server = WebUIServer(
            self,
            host=wanted["host"],
            port=wanted["port"],
            api_key=api_key,
            key_source=key_source,
            logger=self._logger,
        )
        if server.start():
            self._webui = server
        self._webui_settings = wanted

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

        # ⚠ 这里原来还要收尾一批后台东西：`_background_tasks`（写展开流程的任务）、
        # `_training_task` / `_optimize_task`（训练·优化子进程）、`_invite_task`（约战循环）、
        # `_persist_room_task`（常驻房循环）。这些功能已按 2026-10-07 用户口径删除，
        # 现在只剩"停掉进行中的房间 + 关库"。
        #
        # 计时是为了下次再有人问"为什么记了 plugin.shutdown 超时"时**有数可查**：
        # 宿主那只 5 秒的预算是按整条 on_unload 算的，日志里没有耗时就只能靠猜。
        started = time.monotonic()
        for stream_id in list(self._rooms):
            room = self._rooms.pop(stream_id, None)
            if room is None:
                continue
            room.task.cancel()
            # 决策层的答复任务也要收掉：它是插件自己的 asyncio 任务，卸载时不撤会留着
            # 轮询一个已经不存在的房间目录（宿主给插件卸载的预算只有 5 秒，不能拖）
            self._stop_room_brain(stream_id)
            try:
                room.session.kill_now()
            except Exception:  # noqa: BLE001  卸载阶段必须尽力清理，不能中断后续步骤
                if self._logger is not None:
                    self._logger.exception("卸载时关闭房间失败：会话 %s", stream_id)
        if self._deck_pool is not None:
            self._deck_pool.close()
            self._deck_pool = None
        # 卸载时房间已经停了（见上面），所以顺手把配置热更新换下来的旧卡库也一起关掉
        self._close_retired_card_dbs()
        if self._card_db is not None:
            self._card_db.close()
            self._card_db = None
        # 面板与训练任务也要收：训练那层会起子进程（擂台跑起来是一整套内核 + 两个 WindBot），
        # 卸载时不杀就会留下孤儿进程占着端口；两边都走"同步、不 await"的路径，
        # 因为卸载给的总预算只有 5 秒。
        if self._train_runner is not None:
            self._train_runner.stop_now()
            self._train_runner = None
        self._train_store = None
        if self._webui is not None:
            self._webui.stop_now()
            self._webui = None
        if self._logger is not None:
            self._logger.info(
                "游戏王对局管家已卸载（用了 %.2f 秒；宿主给的预算是 5 秒）",
                time.monotonic() - started,
            )

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新后重建卡牌数据库、卡组池、面板与训练层。"""

        del config_data
        del version
        if scope != "self":
            return
        # 卡库换实例时**房间在打就不能关旧的那个**（见 `_swap_card_db`）
        self._swap_card_db(CardDatabase(self._resolve_cards_cdb()))
        if self._deck_pool is not None:
            self._deck_pool.close()
            self._deck_pool = DeckPool(
                Path(self.ctx.paths.data_dir),
                default_windbot_deck=self.config.duel.windbot_deck or DEFAULT_WINDBOT_DECK,
            )
            self._seed_builtin_decks()
        # 端口/密钥/工作目录/模型都可能刚被改过：整层重建（正在跑的擂台会被停掉，
        # 记录留成"被手动停止"——换了工作目录还往旧目录写才是更坏的结果）
        self._restart_train_runner()
        self._restart_webui()
        self._log_effective_config()

    def _swap_card_db(self, fresh: CardDatabase) -> None:
        """换掉卡库实例（配置热更新走这里）。

        ⚠ **还有房间在打时不能当场关掉旧实例**：`DuelSession`（记录器/查房）与决策层
        `BrainBridge` 都直接引用着那个对象，不是回调。关掉之后它们下一次查卡名/卡文会抛
        `sqlite3.ProgrammingError: Cannot operate on a closed database`——
        用户看到的现象是"改了配置之后这局的 AI 决策就不动了"。
        所以旧的先记下来，等房间都空了再关（`_close_retired_card_dbs`）。
        """

        previous, self._card_db = self._card_db, fresh
        if previous is None:
            return
        if self._rooms:
            self._retired_card_dbs.append(previous)
            if self._logger is not None:
                self._logger.info(
                    "配置热更新：还有 %s 个房间在打，旧卡库先留着（这局打完再关）", len(self._rooms)
                )
            return
        previous.close()

    def _close_retired_card_dbs(self) -> None:
        """房间都空了就把换下来的旧卡库关掉（每局收摊与卸载时各调一次）。"""

        if self._rooms or not self._retired_card_dbs:
            return
        for database in self._retired_card_dbs:
            try:
                database.close()
            except Exception:  # noqa: BLE001  关不上只是回收没做干净，不该影响收尾
                continue
        self._retired_card_dbs.clear()

    # ------------------------------------------------------------------ 工具

    @Tool(
        TOOL_START,
        brief_description=(
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
            # 阻抗决策层：**起在 session.start() 之前**——问答前缀是 WindBot 的启动参数，
            # 起来之后再补就晚了（WindBot 一进房就开始打）。
            # 老口径的"逐步问 AI"删在这条之前（2026-10-07）；现在这条只服务阻抗时点，
            # 展开期一步都不问，见 `duel/brain_bridge.py` 与 `Game/AI/NegateDecision.cs`。
            brain_prefix = await self._start_room_brain(stream_id, deck)
            config = self._build_session_config(stream_id, brain_prefix, deck)
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
                # 房间没起来就把决策层收掉，否则那条任务会挂着空转到插件卸载
                self._stop_room_brain(stream_id)
                await session.stop()
                return self._tool_result(
                    TOOL_START,
                    f"房间开不起来：{exc}。请检查插件配置里的 ygopro 路径与工作目录。",
                )

            deck_label = deck.display_name if deck is not None else DEFAULT_WINDBOT_DECK
            deck_id = deck.deck_id if deck is not None else 0
            task = asyncio.create_task(self._run_room(session, stream_id, group_id, deck_id=deck_id))
            # ⚠ 这里原来会记住"最近一次开过房的群"（`_invite_stream_id`），供主动约战与常驻房
            # 把消息发到真实聊天流；这两项已按 2026-10-07 用户口径删除，所以这行没了。
            # 卡组名一并记进房间：`/查房` 跨群列房间时要能说清每个房间是哪副牌
            # （没找到投稿卡组时用的是机器人自带卡组，与上面的日志同一口径）
            deck_label = deck.display_name if deck is not None else DEFAULT_WINDBOT_DECK
            self._rooms[stream_id] = ActiveRoom(
                session, stream_id, group_id, task, deck_name=deck_label, deck_id=deck_id
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

        ``stream_id`` 原来只用来给"展开流程写好了"的后续播报定位会话；那条链路已删除，
        参数保留只是不打乱现有调用方的签名。
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
            # ⚠ 别再写"本群现在有 N 副"：`count()` 数的是**全局投稿**（卡组池共享，见 DeckPool.count），
            # 那样写会让人以为数的是本群（2026-10-07 评审指出）。
            f"卡组池现在有 {self._deck_pool.count(group_id)} 副投稿（共享池，含别群的），"
            f"可选 {len(self._deck_pool.list_decks(group_id))} 副（含 WindBot 内置卡组）",
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
        # ⚠ 这里原来还有一个"导入卡组后让模型写一份展开流程存进知识库"的分支
        # （`ai_deck_plan` + `ai_brain` 都开着时才走）。2026-10-07 用户口径把
        # 展开流程 / 知识库 / 逐步问 AI 整条链路去掉了，投稿回执到这里就结束。
        return "\n".join(lines)

    # ⚠ 这里原来有一个 `ygo_deck_submit` 工具（让模型在群里看到卡组码就自动收进卡组池）。
    # **2026-10-07 用户口径：去掉自动导入，卡组码只能用指令投稿**——所以整块删掉了。
    # 投稿走 `/加卡组 <卡组码>`（内部复用 `_store_deck_from_code`，解析/校验/卡库核对都在那一份）。

    @Tool(
        TOOL_LIST_DECKS,
        brief_description=(
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
        brief_description="查看当前游戏王对局的状态：房间地址、机器人是否就位、群友是否进来了、打到哪一步。",
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
        brief_description=(
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
        self._stop_room_brain(stream_id)
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

    # ⚠ 这里原来是"卡组训练/调优与 AI 打牌"的一整片指令，2026-10-07 用户口径全删：
    # `/优化卡组`（＋ `_optimize_command` / `_optimize_summary` / `_run_optimize` / `_optimize_milestone`）、
    # `/挑脚本`（cmd_pick_style）、`/写打法`（cmd_write_playbook）、`/训练`（＋
    # `_parse_training_rounds` / `_training_command` / `_run_training` / `_training_summary`），
    # 以及只给它们用的 `_run_tool` / `_style_tool_command` 两个跑独立进程的辅助方法。
    # 留下的只有 `_list_position`（`/卡组列表`、`/卡组详情`、`/固定卡组` 要用）。

    def _list_position(self, deck: StoredDeck) -> int:
        """卡组在 ``/卡组列表`` 里的位置号（命令行工具按这个号取卡组，见 tools 的约定）。"""

        if self._deck_pool is None:
            return 0
        for index, item in enumerate(self._deck_pool.list_decks(""), start=1):
            if item.deck_id == deck.deck_id:
                return index
        return 0

    # ⚠ 这里原来是 `_run_tool` / `_style_tool_command` 两个方法：在独立进程里跑
    # tools/ 下的"挑脚本 / 写打法"工具，边跑边把里程碑播到群里。它们只被下面那几条
    # 已删的指令使用（`/挑脚本` 与 `/写打法`），2026-10-07 用户口径一并删掉。

    # ⚠ 这里原来是三条"训练/调优"指令：`/挑脚本`（cmd_pick_style，实测几个现成脚本挑一个）、
    # `/写打法`（cmd_write_playbook，让模型写打法数据）、`/训练`（cmd_train，两副牌互相对打 +
    # `_parse_training_rounds` / `_training_command` / `_run_training` / `_training_summary`）。
    # **2026-10-07 用户口径：卡组训练与调优整块去掉**——想给某副牌写专属执行器改用 AI agent
    # 按 executors/README.md 多轮迭代着写，插件自己不再做这些。

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

    # ⚠ 这里原来有一条 `/复盘` 指令（cmd_replay）与它的三个私有方法 `_replay_lines` /
    # `_card_label` / `_write_replay_commentary`：读这一局的决策日志，报"问 AI 多少次、
    # 拦下多少、时间花在哪"，再让模型讲成人话。**2026-10-07 用户口径：复盘与 AI 打牌一起去掉**
    #（决策日志本身也存在已删的 duel/knowledge.py 里），所以整块删掉了。

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
        description="删除一副投稿卡组（卡组池全局共享，删除范围不限本群；需管理员）",
        pattern=r"^/删卡组\s+\S",
        permission="operator",
    )
    async def cmd_deck_delete(
        self, text: str = "", stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """按编号或名字删除一副投稿。

        ⚠ **删除范围是全局的**（不限本群）：卡组池共享，列表里看得到别群的投稿，也就删得掉。
        所以这里要管理员权限（2026-10-07 评审两次指出：没有门槛时任何群友都能删掉别群投的牌），
        回执里也会写明这副牌是谁投的。
        """

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
        # 投稿来自别的群时说明一句：删的是共享池里的牌，不是"本群那副"。
        # ⚠ 比较前要把调用方的群号**归一**（库里存的是归一后的值，见 `DeckPool.canonical_group`）：
        # 群号里带 `/`、`:`、`..` 这类字符时，不归一就会把本群的投稿说成"来自别的群"。
        own_group = self._deck_pool.canonical_group(group_key)
        source = "" if deck.group_id == own_group else f"（投稿人 {deck.contributor_name}，来自别的群）"
        await self.ctx.send.text(
            f"已删除「{deck.display_name}」{source}。"
            f"卡组池现在还有 {self._deck_pool.count(group_key)} 副投稿（共享池，含别群的）。",
            stream_id,
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
            # ⚠ 这里原来还有一行"问 AI：<档位>"（/出牌模式 改的就是它）。AI 打牌整条链路
            # 已按 2026-10-07 用户口径删除，卡组详情不再报这一项。
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

    # ⚠ 这里原来有一条 `/出牌模式` 指令（cmd_brain_mode）：按卡组切换"问 AI"的档位
    # （不问 / 每问都问 / 只问高压 / 只应对不掌舵 / 跟随全局），改的是卡组库里的 brain_scope。
    # AI 打牌整条链路已按 2026-10-07 用户口径删除，这条指令连同档位表一起删掉了。

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
        description="清空**本群**投稿的卡组（需管理员；别群的投稿与内置卡组不受影响）",
        pattern=r"^/(?:清空卡组|删除所有卡组)\s*$",
        permission="operator",
    )
    async def cmd_deck_clear(
        self, stream_id: str = "", group_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """清空本群投稿。

        回执里点明"本群"与"卡组池是共享的"：以前这里文件按全池清、记录只删本群，
        别群的投稿会变成悬空文件引用（2026-10-07 评审指出）；现在两边都只动本群。
        """

        del kwargs
        if self._deck_pool is None:
            return True, "卡组池还没准备好", 1
        group_key = group_id or stream_id
        count = self._deck_pool.delete_all(group_key)
        await self.ctx.send.text(
            f"已清空本群投稿的 {count} 副卡组。"
            "（卡组池是全局共享的：别群的投稿和 WindBot 自带卡组都没动）",
            stream_id,
        )
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
        不会报错，只会静默换一个随机脚本 —— 具体分两种情况：

        1. 配了源码树且编译过：用编译产物（里面含自己写/agent 写的执行器）；
        2. 其余情况：用配置里那个原版 exe。
        """

        configured = self.config.paths.resolved_windbot_executable() or Path(
            self.config.paths.windbot_executable
        )
        src_dir = self.config.paths.resolved_windbot_src_dir()
        if src_dir is None:
            return configured
        # ⚠ 这里原来还有一条分支："开了 AI 教练就必须用 bin/PlanAware 那份计划感知执行器"。
        # AI 教练已按 2026-10-07 用户口径删除（连带 `_plan_coach_requested`），现在只按源码树
        # 编译产物优先、否则用配置的 exe 这条简单逻辑走。
        built = src_dir / "bin" / "Release" / "WindBot.exe"
        return built if built.is_file() else configured

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
    # `_with_current_style()` 照旧优先用它。（原来那两支"写入这两个字段"的命令行工具
    # ——`tools/pick_style.py` / `tools/optimize_deck.py`——也随训练调优一起删了，
    # 所以这两个字段现在由 agent 手工写库/由旧数据带着走。）

    # ⚠ 这里原来是 `_make_deck_plan_handler` / `_generate_deck_plan` 两块：导入卡组后排一个
    # 后台任务，调 tools/build_deck_plan_ai.py 让模型按卡表写一份展开流程存进知识库，
    # 问 AI 时整份喂给模型。展开流程 / 知识库 / 问 AI 已按 2026-10-07 用户口径整条删除
    # （连带 `_background_tasks` 这个"后台任务集合"与 `_knowledge_cache` 失效逻辑）。

    # ⚠ 这里原来还有三块：`_script_command`（拼 `tools/generate_deck_script.py` 的命令行）、
    # `_parse_generated_style`（从子进程输出里抠脚本类名）、`_collect_card_info`
    # （把卡组整理成含效果文本的清单喂给模型）。自动写脚本整条链路已按 2026-10-07 用户口径删除。
    # 卡牌清单那份口径本身是通用工具，留在 `duel/cards.py` 的 `collect_card_info()`
    # （原来那两支 CLI——写打法数据 / 写展开流程——也一起删了，所以现在没有人调它）。

    def _record_room_duel(
        self, stream_id: str, group_id: str, result_data: Dict[str, object], deck_id: int = 0
    ) -> None:
        """把这一局房间对局写进训练记录（`kind=duel`），给「复盘优化」当素材。

        为什么不解析录像：`.yrp` 里**只有玩家的应答，没有内核的提问**，逐动作复盘根本做不出来
        （`tools/analyze_replay.py` 的模块头写着这条）。而这边的记录器是**活着看到全部报文**的，
        回合数、双方动作数、召唤/特召/发动/盖放/攻击、伤害、用过的卡都在手里——
        这比录像强得多，所以对局一结束就把它落库。

        ``deck_id`` 必须带上：「复盘优化」是按这副牌的编号找最近几局的（`_recent_duels`），
        只记卡组名的话那份记录谁也匹配不上，用户看到的永远是"还没有对局记录"。

        写失败只记日志：这是训练功能的素材，不该影响对局播报。
        """

        store = self._train_store
        if store is None:
            return
        try:
            title = f"房间对局：{result_data.get('deck_name') or '未知卡组'}"
            log_dir = self._training_workspace() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-duel.log"
            record = store.create(
                "duel",
                title,
                {"stream_id": stream_id, "group_id": group_id, "deck_id": int(deck_id)},
                log_path,
            )
            store.finish(record.run_id, "done", summary=dict(result_data))
        except Exception:  # noqa: BLE001  记录失败不该影响播报
            if self._logger is not None:
                self._logger.exception("写对局记录失败（不影响播报）")

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

    async def _run_room(
        self, session: DuelSession, stream_id: str, group_id: str, *, deck_id: int = 0
    ) -> None:
        """在后台等一局打完，然后播报结果并写进机器人上下文。"""

        outcome = OUTCOME_ABORTED
        summary: List[str] = []
        result_data: Dict[str, object] = {}
        try:
            outcome = await session.wait_finished()
            summary = session.summary_lines()
            result_data = session.result_dict()
            # ⚠ 这里原来还有两步收尾：`_finish_room_brain`（把这一局的胜负回填进决策日志，
            # 供 /复盘 与 review_decisions 判断干预对错）与 `_record_room_duel`（把真实房间对局
            # 写进擂台那张结果库，arena="room"）。AI 打牌 / 复盘 / 训练调优整条链路已按
            # 2026-10-07 用户口径删除，这两步连同 duel/duelrecord.py 与 train/store.py 一起去掉了。
            # 打完就记一条：复盘优化要有"这一局到底发生了什么"才能说问题。
            # 只有真的打完（不是没人来/超时收摊）才记。
            if outcome == OUTCOME_FINISHED:
                self._record_room_duel(stream_id, group_id, result_data, deck_id=deck_id)
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
            # 阻抗决策层跟着房间一起收摊：先撤答复任务（它还在轮询问答文件），再关房间。
            # 收摊时会打一行计数（问了/答了/超时/失败各几次）——这是下一局复盘"决策层到底
            # 有没有在工作"的凭据，别删。
            self._stop_room_brain(stream_id)
            await session.stop()
            # 房间空了：配置热更新期间换下来的旧卡库现在可以关了
            self._close_retired_card_dbs()
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
        llm = self.config.llm
        try:
            # ⚠ **必须显式给 max_tokens**：省略会落到宿主给该任务配的默认额度，
            # 而那个模型"先想半天"——短请求的额度会被思考吃光、回复是**空串**
            #（2026-10-06 群里"打完没有 AI 总结"就是这个：配置 `summarize_with_ai` 明明是 true，
            # 日志里只留一条 `生成对局总结返回了空内容`）。
            # 播报只要求 120 字，但思考会先花掉额度；模型与超时都读 `[llm]` 那节。
            #
            # 超时由插件自己执行（宿主那侧没有超时参数）：没超时的话，一次卡住的请求会让
            # 播报无限期挂着——对局早结束了，群里却什么都看不到。
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=prompt,
                    model=llm.summary_model.strip(),
                    max_tokens=SUMMARY_MAX_TOKENS,
                ),
                timeout=max(float(llm.summary_timeout_ms) / 1000.0, 1.0),
            )
        except asyncio.TimeoutError:
            if self._logger is not None:
                self._logger.warning(
                    "生成对局总结超时（%s 毫秒；可调 llm.summary_timeout_ms）", llm.summary_timeout_ms
                )
            return ""
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
                # 标签用合并后的插件名（2026-10-07 第五轮评审指出旧名残留）：
                # 它会被写进机器人上下文的来源标记，对不上现在注册的插件 id 会让追溯变难
                source_kind="plugin:mai-play-ygo",
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

    # ⚠ 这里原来是 AI 打牌的核心几块：`_brain_scope_for`（这一局的问 AI 档位）、
    # `_start_room_brain`（起"逐步问 AI"的答复任务：模型走 <数据目录>/brain_model.toml，
    # 把打法数据 + 攻略要点 + 知识库检索结果一起喂给模型，再把问答文件交给 WindBot）、
    # `_finish_room_brain`（打完把胜负回填决策日志）、`_record_room_duel`（真实对局写进结果库）、
    # `_stop_room_brain`（收摊撤任务）。**2026-10-07 用户口径：AI 打牌整条链路去掉**——
    # 出牌交回各卡组自带的 WindBot 脚本（见 `_with_current_style`），这几块连同
    # train/ 整包、duel/knowledge.py、duel/duelrecord.py 一起删了。

    # ⚠ 这里原来还有 `_data_dir`（插件数据目录，只有已删的 AI 链路在用）与
    # `_knowledge`（懒加载 knowledge.db 的知识库缓存）。2026-10-07 用户口径去掉 AI 打牌后
    # 两个方法都没有消费者了：`duel/knowledge.py` 已删，这里一并删掉。

    def _build_session_config(
        self,
        stream_id: str,
        brain_prefix: Optional[Path] = None,
        deck: Optional[StoredDeck] = None,
    ) -> SessionConfig:
        """把插件配置映射成会话配置。

        Args:
            stream_id: 这一局属于哪个聊天流（房间按它登记）。
            brain_prefix: 阻抗决策层的问答前缀；``None`` 表示这一局不起决策层
                （总开关关掉，或者房间还没走到起决策层那一步）。
        """

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
            brain_file=brain_prefix,
            # 决策层开哪半跟着**卡组**走（卡组页那个档位），没设过就跟随全局配置
            brain_target_choice=self._brain_switches_for(deck)[0],
            brain_negate_gate=self._brain_switches_for(deck)[1],
            brain_timeout_ms=int(self.config.llm.decision_timeout_ms),
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

    # ------------------------------------------------------------------ 阻抗决策层
    #
    # WindBot 侧在阻抗时点把问题写进 `<prefix>.q`、轮询 `<prefix>.a` 等答复
    # （协议见 `duel/brain_bridge.py` 的文件头与 WindBot 的 `Game/AI/MaiBotBrain.cs`）。
    # 这里负责：**每个房间一份前缀**（互不干扰，也便于按房间排查）、起停答复任务、
    # 以及把问题交给宿主模型。

    def _brain_prefix(self, stream_id: str) -> Path:
        """给这一局生成问答前缀（数据目录下，每局一个独立名字）。

        为什么不用 stream_id 直接当文件名：聊天流 ID 里带平台前缀与井号，直接落盘既不好看
        也不安全（Windows 上 `#`、`:` 之类都可能出问题）。所以用短随机后缀，并把路径打进日志，
        要排查时按日志里的路径去数据目录找即可。
        """

        directory = Path(self.ctx.paths.data_dir) / "brain"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"room_{uuid.uuid4().hex[:12]}"

    #: 卡组页那个「AI 决策档位」的取值 → 这一局开哪半决策层。
    #: 空串（或认不出的旧值）＝跟随全局配置，行为与从前一致。
    _BRAIN_SCOPE_SWITCHES: Dict[str, Tuple[bool, bool]] = {
        "off": (False, False),           # 不问（只用脚本）
        "target_only": (True, False),    # 只问"该指哪只怪"
        "full": (True, True),            # 目标 + 要不要交，都问
    }

    def _brain_switches_for(self, deck: Optional[StoredDeck]) -> Tuple[bool, bool]:
        """这一局的阻抗决策层开哪半：``(问目标, 问要不要交)``。

        顺序是"全局配置 → 卡组自己的档位覆盖"：档位记在卡组上（面板卡组页每副牌一个设置），
        因为实测"有的牌脚本就够、有的牌非要 AI 才动得起来"，一个全局开关满足不了两种牌。
        """

        enabled = bool(self.config.duel.brain_enabled)
        gate = bool(self.config.duel.brain_negate_gate)
        if deck is None:
            return enabled, gate
        scope = deck.brain_scope.strip().lower()
        return self._BRAIN_SCOPE_SWITCHES.get(scope, (enabled, gate))

    async def _start_room_brain(self, stream_id: str, deck: Optional[StoredDeck] = None) -> Optional[Path]:
        """起这一局的答复任务，返回问答前缀；不起时返回 None（调用方据此不起决策层）。

        **是 async 的、而且会在开房之前把预热走完**：预热与第一次提问撞在一起时，那条提问会
        被在途的预热拖过等待上限（实测：走插件真实链路的一局里唯一一次提问就是这个原因超时的）。
        预热本身最多等 ``_WARMUP_LIMIT_SECONDS``，超时就放弃（正式提问有熔断兜底）。
        """

        wants_target, wants_gate = self._brain_switches_for(deck)
        if not (wants_target or wants_gate):
            return None
        existing = self._brains.get(stream_id)
        if existing is not None:
            self._stop_room_brain(stream_id)

        prefix = self._brain_prefix(stream_id)
        # ⚠ Python 侧的等模型上限要比 WindBot 那边**短 400 毫秒**：超时必须由这一侧先发现
        # 并记账（`timed_out` 计数是排查"决策层是不是在拖时间"的唯一凭据），否则 WindBot
        # 已经放弃、这边还在等，日志里什么都看不见。
        timeout = max(float(self.config.llm.decision_timeout_ms) / 1000.0 - 0.4, 0.5)
        bridge = BrainBridge(
            prefix=prefix,
            generate=self._brain_generate,
            card_db=self._card_db,
            logger=self._logger,
            timeout=timeout,
            style=self.config.llm.decision_style,
        )
        task = asyncio.create_task(bridge.run(), name=f"mai-play-ygo-brain-{stream_id}")
        self._brains[stream_id] = (bridge, task)
        # 预热一次并**等它走完再开房**：首次调用要多付约 1.2 秒（宿主懒加载模型配置 + 建连），
        # 而 WindBot 一进房就开始打——不预热则一局的第一次提问最容易白等。
        # ⚠ 但也不能把它挂成"后台任务"就算了：那样它会和第一次正式提问**在途撞车**，
        # 反而把那次提问拖过等待上限（走插件真实链路的一局里就是这样超时掉的）。
        # 所以这里 await，并且加一个上限——预热本身不值得让开局等太久。
        try:
            await asyncio.wait_for(self._warmup_brain(stream_id), timeout=_WARMUP_LIMIT_SECONDS)
        except asyncio.TimeoutError:
            if self._logger is not None:
                self._logger.warning(
                    "决策层预热超过 %.0f 秒仍未返回，不再等它（正式提问有自己的超时与熔断）",
                    _WARMUP_LIMIT_SECONDS,
                )
        if self._logger is not None:
            self._logger.info(
                "阻抗决策层已启动：前缀 %s｜目标选择 %s｜闸门 %s｜模型 %s｜档位 %s｜等答复上限 %.1fs",
                prefix,
                "开" if wants_target else "关",
                "开" if wants_gate else "关",
                self.config.llm.decision_model.strip() or "（跟宿主 utils 任务）",
                bridge.style,
                timeout,
            )
        return prefix

    async def _warmup_brain(self, stream_id: str) -> None:
        """开局前把模型这条路"走通一次"，免得第一次真正的提问撞上冷启动。

        为什么不省这一步：实测首次调用要比常态多约 1.2 秒（懒加载配置 + 建连），
        单次 2.03 秒 vs 常态 0.83~0.93 秒——而 WindBot 的等待上限是 2.5 秒、
        Python 侧更短，于是**一局的第一次提问最可能白等**（真机自测里就是这样）。
        开局到有人进房通常有十几秒，用掉这笔开销正好。
        """

        started = time.monotonic()
        try:
            await self.ctx.llm.generate(
                prompt="回复两个字：就绪",
                model=self.config.llm.decision_model.strip(),
                temperature=0.0,
                max_tokens=8,
            )
        except Exception:  # noqa: BLE001  预热失败不该影响开局：正式提问会自己超时熔断
            if self._logger is not None:
                self._logger.warning("决策层预热失败（不影响开局，正式提问会自己超时熔断）", exc_info=True)
            return
        if self._logger is not None:
            self._logger.info(
                "决策层预热完成：%.2fs（这一步是为了不让一局的第一次提问撞上冷启动）",
                time.monotonic() - started,
            )

    def _stop_room_brain(self, stream_id: str) -> None:
        """停掉这一局的答复任务并收走问答文件（房间收摊时调用）。"""

        entry = self._brains.pop(stream_id, None)
        if entry is None:
            return
        bridge, task = entry
        bridge.stop()
        task.cancel()
        stats = bridge.stats.as_dict()
        if self._logger is not None:
            self._logger.info("阻抗决策层已收摊：%s", stats)

    async def _brain_generate(self, prompt: str) -> Optional[str]:
        """把决策层的问题交给宿主模型，返回答复文本。

        只做三件事：发问、把"宿主返回结构"翻译成纯文本、失败时明确返回 None。
        **不做任何兜底措辞**：答不上来就让 WindBot 按出牌脚本打——塞一句编出来的答复
        回去比不答更坏（它会被当成决策层的主张）。
        """

        model = self.config.llm.decision_model.strip()
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=model,
                temperature=0.0,
                max_tokens=int(self.config.duel.brain_max_tokens),
            )
        except Exception:  # noqa: BLE001  模型不可用不该让对局出问题
            if self._logger is not None:
                self._logger.exception("决策层调用模型失败")
            return None
        if not isinstance(result, dict) or not result.get("success", False):
            if self._logger is not None:
                self._logger.warning("决策层的模型请求被拒绝：%s", result)
            return None
        text = str(result.get("response") or "").strip()
        if not text:
            # 这句日志与"对局总结返回了空内容"是同一类坑：额度被思考吃光时答复就是空的
            if self._logger is not None:
                self._logger.warning(
                    "决策层模型返回空内容（模型 %s；若是思考型模型请换一只）", model or "（宿主默认）"
                )
            return None
        return text


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

        # 对局内核与出牌引擎都是 Windows 可执行文件（2026-10-07 评审提醒：这是个 Windows-only 插件）。
        # 非 Windows 上不假装能开，直接说清楚——查卡与发卡图那半边不受影响，仍然可用。
        if sys.platform != "win32":
            return (
                f"本插件只在 Windows 上能开房打牌（当前平台 {sys.platform}）：对局内核 ygopro.exe 与"
                "出牌引擎 WindBot.exe 都是 Windows 程序。查卡与发卡图不受影响。"
            )
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

    # ⚠ 这里原来是三个常驻循环/辅助：`_invite_loop`（空闲时按随机间隔主动冒泡约战）、
    # `_compose_invite_text`（该不该发、发什么）、`_persist_room_loop`（常驻房：插件一启动就
    # 拉起一副 ygopro + WindBot 一直等人连，固定端口、打完自动重开、崩了 5 秒后自愈）。
    # **2026-10-07 用户口径：常驻房与空闲约战都去掉**，现在只有群里有人要打（`/开房` 或
    # `ygo_duel_start`）才开房。

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
