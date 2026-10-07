"""WindBot 自带卡组的登记表：出牌思路名 -> 中文卡组名。

WindBot 自带的每副卡组都配了它自己的出牌脚本，**卡表与脚本是一套的**，所以用它自带的卡组
时牌力最好（这也是不再依赖群友投稿卡组的原因）。

英文名是 WindBot 的注册名（``Deck=`` 参数用的就是它），中文名只用于群里展示。
个别拿不准译名的系列保留英文原名——与其编一个可能不对的译名，不如原样显示；这张表
就在这个文件里，想改直接改。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import logging

from .windbot_decks import WIND_BOT_DECK_NAMES, load_available_decks


# 出牌思路名（WindBot 注册名）-> 群里展示用的中文名
BUILTIN_DECK_NAMES: Dict[str, str] = {
    "Albaz": "烙印",
    "Altergeist": "幻变骚灵",
    "Apophis": "Apophis",
    "Archfiend": "恶魔",
    "BE2025": "青眼2025",
    "Blackwing": "黑羽",
    "Blue-Eyes": "青眼白龙",
    "BlueEyesMaxDragon": "青眼究极龙",
    "Brave": "勇者",
    "Burn": "削血",
    "ChainBurn": "连锁削血",
    "Chaos408": "混沌408",
    "ChaosRitual": "混沌仪式",
    "CyberDragon": "电子龙",
    "DarkMagician": "黑魔导",
    "Dogmatika": "教导",
    "Dragun": "龙骑兵",
    "Dragunity": "驭龙团",
    "Enneacraft": "Enneacraft",
    "Evilswarm": "邪蚀",
    "Exosister": "驱魔修女",
    "FamiliarPossessed": "使魔附身者",
    "Frog": "青蛙",
    "Gravekeeper": "守墓",
    "Graydle": "古雷德尔",
    "GrenMajuThunderBoarder": "巨兽加雷法混搭",
    "HeroBeat1103": "英雄BEAT",
    "Horus": "荷鲁斯",
    "Kashtira": "怒刹帝利",
    "Labrynth": "拉比林斯",
    "Level VIII": "八星",
    "Lightsworn": "光道",
    "LightswornShaddoldinosour": "光道影依恐龙",
    "Maliss": "码丽丝",
    "MalissOCG": "码丽丝(OCG)",
    "MathMech": "数学群",
    "MokeyMokey": "摩奇摩奇",
    "MokeyMokeyKing": "摩奇摩奇王",
    "Monarch506": "帝王",
    "Neko": "猫",
    "Nekroz": "影灵衣",
    "OldSchool": "老派",
    "Orcust": "苍奏",
    "Phantasm": "Phantasm",
    "Pumpking": "南瓜王",
    "PureWinds": "纯风",
    "Qliphort": "影依",
    "RadiantTyphoon": "光台风",
    "Rainbow": "彩虹",
    "Rank V": "五星",
    "Rank8": "八阶",
    "Ryzeal": "雷热",
    "ST1732": "ST1732",
    "SacredBeast": "三幻魔",
    "Salamangreat": "电子界",
    "SkyStriker": "闪刀姬",
    "SuperheavySamurai": "超重武者",
    "Swordsoul": "天威相剑",
    "Tearlaments": "泪冠哀歌",
    "ThunderDragon": "雷龙",
    "TimeThief": "时劫者",
    "Toadally Awesome": "饼蛙",
    "Trickstar": "淘气仙星",
    "Voiceless": "静寂",
    "Witchcraft": "魔女工艺",
    "Yosenju": "妖仙兽",
    "Yubel": "尤贝尔",
    "Zefra": "索菲拉",
    "Zexal Weapons": "ZEXAL兵器",
    "Zoodiac": "十二兽",
}


def builtin_decks(windbot_dir: Path, *, logger: Optional[logging.Logger] = None) -> List[Tuple[str, str, Path]]:
    """列出本机实际可用的内置卡组。

    Returns:
        ``[(出牌思路名, 中文名, .ydk 路径)]``；只有卡表文件真的存在的才会返回，
        所以登记表里的名字永远不会指向一副不存在的卡组。
    """

    available = load_available_decks(windbot_dir, logger=logger)
    decks_dir = Path(windbot_dir) / "Decks"
    result: List[Tuple[str, str, Path]] = []
    for style in sorted(available):
        # 名字表里存的就是卡表文件名（形如 AI_BlueEyes），不要再补 AI_ 前缀
        ydk = decks_dir / f"{_ydk_stem(style)}.ydk"
        if not ydk.is_file():
            continue
        result.append((style, BUILTIN_DECK_NAMES.get(style, style), ydk))
    return result


def _ydk_stem(style: str) -> str:
    """由出牌思路名查出对应的卡表文件名（不含扩展名）。"""

    return WIND_BOT_DECK_NAMES.get(style, style)
