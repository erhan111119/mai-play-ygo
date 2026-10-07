"""六副牌「无干扰最大场」测量：读各自对空白的日志，汇总"我方第 1 回合结束"的场面。

**口径**：每副牌的验收件写在下面的 :data:`DECKS`（依据 ``docs/acceptance.md`` 与本轮教程），
件数由 ``tools/plan_accept.py`` **在进程内**算（不重写它的解析逻辑，免得两边漂移）——
现在直接调它的 :func:`analyze_log` 拿结构化结果，不再解析它打印的文本。

    # 先自己跑日志（每副一行，6 副共 6 条）：
    python tools/brain_eval.py --deck-id 88 --left-style RaiseMoon \\
      --opponent <空白卡表> --right-style Test --rounds 15 --parallel 1 --verbose-bots \\
      --shuffle-seed-base 1500 > temp/train/maxboard-88.log 2>&1

    # 再汇总（读 temp/train/maxboard-<编号>.log）：
    python tools/max_board.py

    # 迭代轮次里每份日志只有 2 局（--rounds 1 的镜像），用 --logs-dir/--pattern 指过去：
    python tools/max_board.py --logs-dir temp/rounds/r1 --pattern "b{deck_id}.log"

⚠ **这个口径是"下界"**（2026-10-06 实测）：场面是按动作回放出来的，而日志里 `我方` 那条流是插件的
闸门视角（两个 bot 的动作混在一起）、对方客户端的流里我们的卡有时是 `UnKnowCard` —— 实测升辉月
"达标 2/28" 而插件 recorder 的落位行显示"大姐第 1 回合站进额外怪兽区 27/30 局"。**要判"第 1 回合
做不做得出件"，用 `tools/first_turn_accept.py`（读落位行）**，本工具只当"变化方向"看。

输出：**最大场达标**＝口径里所有件同时在场的局数（"无干扰下做出来了"）、
**平均件数**＝平均到几件（看"差多远"）、**空场局**＝首回合结束场上一个怪都没有的局。

⚠ **每份日志里的局数要按"bot 流"数，不是按进度行数**：`--rounds 1` 的日志里有镜像 2 局、
而且是并发跑的，进度行是每局打完之后才打的（详见 ``tools/plan_accept.py`` 的 docstring）。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Dict, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# 卡表在宿主的插件数据目录里（插件在 <MaiBot>/plugins/<id>，数据在 <MaiBot>/data/plugins/<id>）。
# 宿主按群哈希分子目录存 .ydk，所以下面用 _deck_path() 在根目录与一级子目录里找；
# 找不到会明确报错并提示用 --decks-dir 指定，不静默跳过。
_HOST_ROOT = _PLUGIN_ROOT.parents[1] if len(_PLUGIN_ROOT.parents) > 1 else _PLUGIN_ROOT.parent
_DATA_DIR = _HOST_ROOT / "data" / "plugins" / "mai-play-ygo"
_DECKS_DIR = _DATA_DIR / "decks"
_CARDS_CDB = _PLUGIN_ROOT / "clients" / "ygopro" / "cards.cdb"


def _deck_path(decks_dir: Path, name: str) -> Path:
    """在卡组目录里找到名为 ``name`` 的卡表（先根目录，再一级子目录=群目录）。"""

    direct = decks_dir / name
    if direct.is_file():
        return direct
    for found in sorted(decks_dir.glob(f"*/{name}")):
        return found
    raise SystemExit(
        f"找不到卡表 {name}（找过 {decks_dir} 与它的一级子目录）。"
        "用 --decks-dir 指到宿主的卡组目录（<MaiBot>/data/plugins/<插件id>/decks）。"
    )

#: 每副牌：deck_id → (脚本名, 卡表, marker, 验收件)
#: ``marker`` 是认"我方进程/座位"用的卡名片段（座位靠"谁更像我方卡组"判，认不出时才用它救场）。
DECKS: Dict[str, Tuple[str, str, str, Dict[str, Sequence[str]]]] = {
    "88": ("RaiseMoon", "77ebc397f862.ydk", "盈彩月夜", {
        "monster": ("盈彩月夜之天 西艾萝-一掷乾坤",),
        "spell": ("未眠之城的『盈彩月夜』",),
    }),
    "93": ("WitchcraftShop", "ca2f4b1d6cf6.ydk", "魔女术", {
        "monster": ("魔女术代理师傅", "魔女术学童组合", "魔女术工匠·服装女巫", "魔女术师傅·玻璃女巫"),
        "spell": ("魔女术的歪曲",),
    }),
    "94": ("ToonShop", "6a28d318c589.ydk", "卡通", {
        "monster": ("漫画猫", "邪魔箱", "青眼卡通究极龙"),
        "spell": ("完美世界 卡通世界", "卡通恐怖", "看透心灵之眼"),
    }),
    "95": ("KillerTune", "d3864774f449.ydk", "杀手级", {
        "monster": ("杀手级调整曲·再混音手", "杀手级调整曲·唱片师"),
        "spell": ("杀手级调整曲同调",),
    }),
    "96": ("SkyStrikerShop", "c4b388c14dc8.ydk", "闪刀", {
        "monster": ("闪刀姬=零露", "闪刀姬-雫空"),
        "spell": ("闪刀亚式-双纽闪门",),
    }),
    "98": ("TraptrixRagnaraika", "48b08ce6cacf.ydk", "虫惑魔", {
        "monster": ("蕾祸之大王鬼牙", "塞拉之虫惑魔"),
        "spell": ("蕾祸大轮首狩舞",),
    }),
    # 2026-10-06 补齐到十副牌（新四副的终场取自各自教程的"终场"段）
    "99": ("Yaosheng", "d9f04bf8b01c.ydk", "耀圣", {
        "monster": ("水晶翼同调龙", "鲜花女男爵"),
        "spell": ("耀圣之诗～回乡之平行体～", "耀圣之诗～狂奏之狂想曲～"),
    }),
    "100": ("KezmoYixiangming", "7de3f02378ef.ydk", "刻魔异响鸣", {
        "monster": ("梦幻崩影·狮鹫", "梦幻崩影·哥布林", "DDD 怒涛大王 决策凯撒", "雷火沸动死旋爆震机"),
    }),
    "101": ("Kashtira", "fad70da2a497.ydk", "俱舍", {
        "monster": ("俱舍怒威族的香格里拉茧", "俱舍怒威族·阿莱斯哈特"),
    }),
    "102": ("Tearlaments", "84b2827c787c.ydk", "珠泪", {
        "monster": ("珠泪哀歌族·水仙女人鱼", "珠泪哀歌族·鲁莎卡人鱼"),
    }),
}


def _load_accept():
    """按文件路径加载同目录的 ``plan_accept``（tools 下的脚本不是包的一部分）。"""

    spec = importlib.util.spec_from_file_location(
        "tool_plan_accept", str(_PLUGIN_ROOT / "tools" / "plan_accept.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # **必须先进 sys.modules 再 exec**：plan_accept 里的 @dataclass 会去
    # ``sys.modules[cls.__module__]`` 找这个模块，不在注册表里会直接抛
    # AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _summarize(report) -> Dict[str, object]:
    """把 :class:`plan_accept.LogReport` 汇总成表里要的三个口径。

    **按局统计，不是按次数**：`full` 数"所有件同时在场的局"、`empty` 数"场上一个怪都没有的局"。
    （`avg`/`need` 交给调用方除局数——表里要显示成 ``x/3``。）
    """

    games = len(report.games)
    return {
        "games": games,
        "streams": report.stream_count,
        "full": sum(1 for stats in report.games if stats.full),
        "empty": sum(1 for stats in report.games if stats.empty_board),
        "avg": sum(stats.hits for stats in report.games),
        "need": sum(stats.required for stats in report.games),
    }


def _analyze(deck_id: str, log: Path, decks_dir: Path) -> Dict[str, object]:
    """用 plan_accept 在进程内算一遍（直接拿结构化结果）。"""

    accept = _load_accept()
    style, ydk, marker, filters = DECKS[deck_id]
    our_cards = accept.read_deck_cards(_deck_path(decks_dir, ydk), Path(_CARDS_CDB))
    lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
    report = accept.analyze_log(
        lines, our_cards, marker=marker,
        monster=filters.get("monster", ()), spell=filters.get("spell", ()),
        grave=filters.get("grave", ()),
    )
    return _summarize(report)


def main() -> int:
    parser = argparse.ArgumentParser(description="六副牌无干扰最大场测量（读日志汇总）")
    parser.add_argument("--logs-dir", default=str(_PLUGIN_ROOT / "temp" / "train"),
                        help="日志目录；读 <目录>/maxboard-<编号>.log")
    parser.add_argument("--pattern", default="maxboard-{deck_id}.log",
                        help="日志文件名模板（迭代轮次里是 temp/rounds/rN/b{deck_id}.log）")
    parser.add_argument("--decks-dir", default=str(_DECKS_DIR),
                        help="宿主的卡组目录（<MaiBot>/data/plugins/<插件id>/decks）")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    decks_dir = Path(args.decks_dir)
    print("=== 无干扰（对空白）首回合终场 ===")
    print(f"{'编号/脚本':<26}{'最大场达标':>12}{'平均件数':>13}{'空场局':>10}")
    seen = 0
    for deck_id, (style, _, _, _) in DECKS.items():
        log = logs_dir / args.pattern.format(deck_id=deck_id)
        if not log.is_file():
            print(f"{deck_id} {style[:16]:<20}{'（没有日志）':>12}")
            continue
        stats = _analyze(deck_id, log, decks_dir)
        games = int(stats["games"])
        if games == 0:
            print(f"{deck_id} {style[:16]:<20}{'（认不出我方进程？）':>12}")
            continue
        seen += 1
        note = ""
        if int(stats["streams"]) != games:
            # 跳过的局要说出来：以前这些局会直接消失，表格看起来"只有 1 局"却没人发现
            note = f"（另有 {int(stats['streams']) - games} 局跳过）"
        print(f"{deck_id} {style[:16]:<20}{stats['full']:>7}/{games:<4}"
              f"{stats['avg'] / games:>8.1f}/{int(stats['need']) / games:<3}{stats['empty']:>8}/{games:<3}{note}")
    print()
    print("（'最大场达标'＝该副牌验收件全部同时在场的局；'平均件数'＝口径里的件平均到几件；")
    print("  '空场局'＝首回合结束时场上一个怪都没有的局）")
    return 0 if seen else 1


if __name__ == "__main__":
    sys.exit(main())
