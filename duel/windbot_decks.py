"""按卡表相似度挑选 WindBot 的出牌脚本。

群友投稿的卡组只会替换 WindBot 的**卡表**，不会替换它的**出牌思路**——出牌思路来自
``Deck=`` 指定的那套 Executor。原来不管投稿什么卡组都套同一套思路（例如白龙），
就会出现「用白龙的思路去打码丽丝」这种明显不会玩的情况。

这里改成：把投稿卡组和 WindBot 自带的每个卡组做卡表比对，挑最接近的那套当出牌思路。
比对时**先看额外卡组**——额外卡组基本是卡组的身份标识，而主卡组的泛用手坑（灰流丽、
增殖的 G 之类）人人都有，只看主卡组会把所有卡组拉得一样近。

名字与卡表文件的对应关系来自 WindBot 主线 ``Game/AI/Decks/*.cs`` 上的
``[Deck("名字", "卡表文件", "强度档")]`` 属性。名字与文件名并不一致
（``AI_BlueEyes.ydk`` 对应的名字是 ``Blue-Eyes``），而且 WindBot 对不认识的名字
**不报错、静默换成随机卡组**，所以这里只会挑出「卡表文件确实存在」的名字。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import logging
import re


# WindBot 注册的出牌脚本：名字 -> 卡表文件名（不含扩展名）。
# 取自 WindBot 主线 Game/AI/Decks/*.cs 的 [Deck(...)] 属性，实测于 2026-09，共 72 个。
WIND_BOT_DECK_NAMES: Dict[str, str] = {
    "Albaz": "AI_Albaz",
    "Altergeist": "AI_Altergeist",
    "Apophis": "AI_Apophis",
    "Archfiend": "AI_Archfiend",
    "BE2025": "AI_BE2025",
    "Blackwing": "AI_Blackwing",
    "Blue-Eyes": "AI_BlueEyes",
    "BlueEyesMaxDragon": "AI_BlueEyesMaxDragon",
    "Brave": "AI_Brave",
    "Burn": "AI_Burn",
    "ChainBurn": "AI_ChainBurn",
    "Chaos408": "AI_Chaos408",
    "ChaosRitual": "AI_ChaosRitual",
    "CyberDragon": "AI_CyberDragon",
    "DarkMagician": "AI_DarkMagician",
    "Dogmatika": "AI_Dogmatika",
    "Dragun": "AI_Dragun",
    "Dragunity": "AI_Dragunity",
    "Enneacraft": "AI_Enneacraft",
    "Evilswarm": "AI_Evilswarm",
    "Exosister": "AI_Exosister",
    "FamiliarPossessed": "AI_FamiliarPossessed",
    "Frog": "AI_Frog",
    "Gravekeeper": "AI_Gravekeeper",
    "Graydle": "AI_Graydle",
    "GrenMajuThunderBoarder": "AI_GrenMajuThunderBoarder",
    "HeroBeat1103": "AI_HeroBeat1103",
    "Horus": "AI_Horus",
    "Kashtira": "AI_Kashtira",
    "Labrynth": "AI_Labrynth",
    "Level VIII": "AI_Level8",
    "Lightsworn": "AI_Lightsworn",
    "LightswornShaddoldinosour": "AI_LightswornShaddoldinosour",
    "Lucky": "AI_Test",
    "Maliss": "AI_Maliss",
    "MalissOCG": "AI_MalissOCG",
    "MathMech": "AI_Mathmech",
    "MokeyMokey": "AI_MokeyMokey",
    "MokeyMokeyKing": "AI_MokeyMokeyKing",
    "Monarch506": "AI_Monarch506",
    "Neko": "AI_Neko",
    "Nekroz": "AI_Nekroz",
    "OldSchool": "AI_OldSchool",
    "Orcust": "AI_Orcust",
    "Phantasm": "AI_Phantasm",
    "Pumpking": "AI_Pumpking",
    "PureWinds": "AI_PureWinds",
    "Qliphort": "AI_Qliphort",
    "RadiantTyphoon": "AI_RadiantTyphoon",
    "Rainbow": "AI_Rainbow",
    "Rank V": "AI_Rank5",
    "Rank8": "AI_Rank8",
    "Ryzeal": "AI_Ryzeal",
    "ST1732": "AI_ST1732",
    "SacredBeast": "AI_SacredBeast",
    "Salamangreat": "AI_Salamangreat",
    "SkyStriker": "AI_SkyStriker",
    "SuperheavySamurai": "AI_SuperheavySamurai",
    "Swordsoul": "AI_Swordsoul",
    "Tearlaments": "AI_Tearlaments",
    "Test": "AI_Test",
    "ThunderDragon": "AI_ThunderDragon",
    "TimeThief": "AI_Timethief",
    "Toadally Awesome": "AI_ToadallyAwesome",
    "Trickstar": "AI_Trickstar",
    "Voiceless": "AI_Voiceless",
    "Witchcraft": "AI_Witchcraft",
    "Yosenju": "AI_Yosenju",
    "Yubel": "AI_Yubel",
    "Zefra": "AI_Zefra",
    "Zexal Weapons": "AI_ZexalWeapons",
    "Zoodiac": "AI_Zoodiac",
}

# 配置里表示「按卡表相似度自动挑」的哨兵值
AUTO_DECK_STYLE = "auto"

# 配置里表示「用 WindBot 的通用兜底脚本」的哨兵值
GENERIC_DECK_STYLE = "generic"

# WindBot 的通用兜底出牌脚本：DoEverythingExecutor，注册名是 "Test"（强度档也是 "Test"）。
# 它不是为某个原型写的 combo，而是「看着场面做点合理的事」，所以喂任意卡表都能打。
# 实测（2026-09-20，同一副 Enneacraft 投稿卡组，与同一台 Blue-Eyes WindBot 对打）：
#   用原型脚本 Enneacraft：0 召唤、0 效果、0 攻击，整局空过并落败；
#   用通用脚本 Test：     通常召唤 3、特殊召唤 9、发动效果 18、攻击 6，对手掉 8100 血。
# 原因是原型脚本按它自己那份卡表的 combo 流程写死，卡表稍有不同就一步都走不出来。
GENERIC_STYLE_NAME = "Test"

# 额外卡组与主卡组在相似度里的权重：额外卡组更能代表一套牌的身份
_EXTRA_WEIGHT = 0.6
_MAIN_WEIGHT = 0.4

_CARD_LINE = re.compile(r"^\d+$")


def parse_ydk_cards(path: Path) -> Tuple[Set[int], Set[int]]:
    """读出一个 ``.ydk`` 文件里的主卡组与额外卡组卡 ID 集合。

    分区按标记行切（``#main`` / ``#extra`` / ``!side``，首字符是 ``#`` 还是 ``!`` 不一定）；
    文件缺失或读不动时返回两个空集合。
    """

    main: Set[int] = set()
    extra: Set[int] = set()
    section = "main"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return main, extra
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("#") or lowered.startswith("!"):
            # ⚠ `side` 要单独认出来：副卡组的卡既不属于主卡组也不属于额外卡组
            # （WindBot 自己的 `Deck.Load` 也把它们单独收进 SideCards，整局用不上）。
            # 原来只说"不是 main/extra 就保持原区"，副卡组的卡于是被算进了额外卡组——
            # 拿这份集合去比"哪份自带卡表最像"会偏（自带卡表里副卡组普遍有 5~15 张）。
            if "side" in lowered:
                section = "side"
            elif "extra" in lowered:
                section = "extra"
            elif "main" in lowered:
                section = "main"
            continue
        if not _CARD_LINE.match(stripped):
            continue
        if section == "side":
            continue
        card_id = int(stripped)
        if section == "extra":
            extra.add(card_id)
        else:
            main.add(card_id)
    return main, extra


def load_available_decks(windbot_dir: Path, *, logger: Optional[logging.Logger] = None) -> Dict[str, Tuple[Set[int], Set[int]]]:
    """扫描 WindBot 的 ``Decks/`` 目录，返回「脚本名 -> (主卡组, 额外卡组)」。

    只有卡表文件真的存在的脚本才会被返回：这样上层挑出来的名字一定能被 WindBot 认出来，
    不会出现「名字写错 → WindBot 静默换随机卡组」的情况。
    """

    decks_dir = Path(windbot_dir) / "Decks"
    if not decks_dir.is_dir():
        if logger is not None:
            logger.warning("找不到 WindBot 的 Decks 目录：%s，无法按卡表挑选出牌脚本", decks_dir)
        return {}
    available: Dict[str, Tuple[Set[int], Set[int]]] = {}
    for name, file_stem in WIND_BOT_DECK_NAMES.items():
        path = decks_dir / f"{file_stem}.ydk"
        if not path.is_file():
            continue
        main, extra = parse_ydk_cards(path)
        if main:
            available[name] = (main, extra)
    if logger is not None:
        logger.info("可用的出牌脚本 %s 个（共登记 %s 个名字）", len(available), len(WIND_BOT_DECK_NAMES))
    return available


def _jaccard(left: Set[int], right: Set[int]) -> float:
    """两个集合的 Jaccard 相似度；任一为空时返回 0。"""

    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def similarity(
    submitted_main: Iterable[int],
    submitted_extra: Iterable[int],
    candidate_main: Set[int],
    candidate_extra: Set[int],
) -> float:
    """算出投稿卡组与某个出牌脚本卡表的相似度（0~1）。

    有额外卡组时以额外卡组为主、主卡组为辅；没有额外卡组时只看主卡组。
    """

    own_main, own_extra = set(submitted_main), set(submitted_extra)
    if own_extra:
        return _EXTRA_WEIGHT * _jaccard(own_extra, candidate_extra) + _MAIN_WEIGHT * _jaccard(
            own_main, candidate_main
        )
    return _jaccard(own_main, candidate_main)


def pick_best_match(
    submitted_main: Sequence[int],
    submitted_extra: Sequence[int],
    available: Dict[str, Tuple[Set[int], Set[int]]],
    *,
    minimum_score: float = 0.05,
) -> Optional[Tuple[str, float]]:
    """挑出最接近的出牌脚本，返回 ``(名字, 相似度)``。

    相似度低于 ``minimum_score`` 时返回 None——那说明这套牌和 WindBot 自带的所有卡组都不像，
    随便挑一个反而更糟；此时上层会退回配置里指定的兜底风格卡组。
    """

    best_name: Optional[str] = None
    best_score = 0.0
    for name, (candidate_main, candidate_extra) in available.items():
        score = similarity(submitted_main, submitted_extra, candidate_main, candidate_extra)
        if score > best_score:
            best_name, best_score = name, score
    if best_name is None or best_score < minimum_score:
        return None
    return best_name, best_score


def list_normal_style_names() -> List[str]:
    """列出可用的风格名（按字母序），供配置说明与报错提示引用。"""

    return sorted(WIND_BOT_DECK_NAMES)
