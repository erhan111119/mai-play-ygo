"""查房出图的命令行外壳：把 `duel/field_image.py` 的渲染器直接跑起来看效果。

用法::

    python tools/field_image.py --demo --render          # 造一副假局面，出 HTML + PNG（用本机 Edge）
    python tools/field_image.py --demo --out temp/x.html  # 只出 HTML

渲染逻辑在 `duel/field_image.py`（插件运行时也要 import 它）；这里只负责造演示局面与预览，
真正的查房走 `plugin.py` 的 `/查房`（宿主的 `render.html2png` + `send.image`）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.card_images import DEFAULT_CACHE_DIR, cache_dir_for, warm_many  # noqa: E402
from duel.cards import CardDatabase  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.field_image import (  # noqa: E402
    BOARD_HEIGHT,
    BOARD_WIDTH,
    CardView,
    DEFAULT_ART_DIR,
    DEFAULT_ART_FALLBACK,
    FieldView,
    SideView,
    _kind_of,
    _STATS_RE,
    build_html,
    card_art_uri,
    card_full_uri,
)

DEFAULT_CDB = _PLUGIN_ROOT / "clients" / "ygopro" / "cards.cdb"


def parse_args() -> argparse.Namespace:
    """命令行：造一张演示图（不需要房间）。"""

    parser = argparse.ArgumentParser(description="把局面画成棋盘图（HTML/PNG）")
    parser.add_argument("--demo", action="store_true", help="用一副假局面出图")
    parser.add_argument("--out", type=Path, default=Path("temp/field-demo.html"), help="HTML 输出路径")
    parser.add_argument("--render", action="store_true", help="顺手用本机 Edge 渲染成 PNG（预览用）")
    parser.add_argument("--art-dir", type=Path, default=DEFAULT_ART_DIR, help="卡图目录（Art）")
    parser.add_argument("--art-fallback", type=Path, default=DEFAULT_ART_FALLBACK, help="备用卡图目录")
    parser.add_argument("--pic-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="整卡卡图缓存目录（缺图会从这里/在线补齐）")
    parser.add_argument("--no-fetch", action="store_true", help="预览时不联网补整卡图")
    parser.add_argument("--cdb", type=Path, default=DEFAULT_CDB, help="cards.cdb（拿卡名/卡种/攻守）")
    return parser.parse_args()


def demo_view(
    details_of: Optional[Callable[[int], object]],
    art_dir: Path,
    art_fallback: Path,
    pic_dir: Optional[Path] = None,
) -> FieldView:
    """一张演示局面：尽量铺满（各种召唤种类的怪 + 里侧 + 空位 + 牌堆计数），检查排版与兜底。

    ⚠ 演示的卡**按卡种自动摆到该去的行**（见下面的 `put`）：真人局里位置来自 recorder 的报文，
    不会摆错；但演示是我们手写的卡号，写错就会出现"羽毛扫站在怪兽区"这种假象
    （2026-10-08 用户就是被这个骗到了）——所以这里用卡种判一次，摆错直接抛 AssertionError。
    """

    def card(card_id: int, *, face_up: bool = True, attack: bool = True) -> CardView:
        detail = details_of(card_id) if details_of else None
        type_text = str(getattr(detail, "type_text", ""))
        match = _STATS_RE.search(str(getattr(detail, "stats", "")))
        return CardView(
            card_id=card_id,
            name=str(getattr(detail, "name", "") or card_id),
            face_up=face_up,
            attack=attack,
            kind=_kind_of(type_text),
            link="连接" in type_text,
            level=int(match.group(3)) if match and match.group(3) and face_up else 0,
            type_line=type_text.replace(" ", "/"),
            effect=str(getattr(detail, "effect", "") or "") if face_up else "",
            atk=int(match.group(1)) if match and face_up else None,
            def_=int(match.group(2)) if match and face_up else None,
            art=card_art_uri(card_id, art_dir=art_dir, fallback_dir=art_fallback) if face_up else "",
            # 整卡图（本地缓存 → 缺了就排后台下载）：预览里先同步补齐一次，图里就能看到真卡图
            full=card_full_uri(card_id, pic_dir=pic_dir) if face_up else "",
        )

    sides = {
        "me": SideView(label="憨憨", lp=6200, deck="闪刀姬", is_turn=True, grave=4, banished=1, extra_count=2),
        "them": SideView(label="嘻嘻$aN9sW", lp=3100, deck="异解", grave=7, banished=2, extra_count=1),
    }

    def put(who: str, card_id: int, slot: int, *, place: str = "auto", face_up: bool = True,
            attack: bool = True) -> None:
        """把一张卡放好：`place="auto"` 时按卡种决定进怪兽行还是魔陷行（场地魔法进场地格）。"""

        view = sides[who]
        detail = details_of(card_id) if details_of else None
        kind = _kind_of(str(getattr(detail, "type_text", "")))
        is_field_spell = kind == "spell" and "场地" in str(getattr(detail, "type_text", ""))
        where = place
        if place == "auto":
            where = "field" if is_field_spell else ("monster" if kind == "monster" else "spell")
        assert where != "monster" or kind == "monster", f"{card_id} 不是怪兽，别放进怪兽区"
        assert where != "spell" or kind in ("spell", "trap"), f"{card_id} 不是魔陷，别放进魔陷行"
        assert where != "field" or is_field_spell, f"{card_id} 不是场地魔法，别放进场地格"
        if where == "monster":
            view.monsters[slot] = card(card_id, face_up=face_up, attack=attack)
        elif where == "spell":
            view.spells[slot] = card(card_id, face_up=face_up, attack=attack)
        elif where == "field":
            view.field_zone = card(card_id, face_up=face_up, attack=attack)
        else:
            view.extra = card(card_id, face_up=face_up, attack=attack)

    # 我方（下半场）：怪兽行 + 魔陷行 + 场地；额外怪兽区在中间那一格
    put("me", 76072561, 0)                      # 闪刀姬=零露（连接）
    put("me", 63288573, 1, attack=False)         # 闪刀姬-燎里（守备）
    put("me", 9753964, 2)                        # 琰魔龙 红莲魔·渊（同调）
    put("me", 15665977, 3)                       # 杀手级调整曲·红印鉴唱片师（长名字，验证名字条）
    put("me", 34433770, 1)                       # 闪刀亚式-双纽闪门（魔法）→ 魔陷行
    put("me", 10045474, 3)                       # 无限泡影（陷阱）→ 魔陷行
    put("me", 33700664, 0)                       # 异解△领域-瓦尔涡罗斯（场地魔法）→ 场地格
    put("me", 84815190, 0, place="extra")        # 鲜花女男爵 → 我方额外怪兽区

    # 对手（上半场）
    put("them", 49036338, 0)                     # PSY骨架驱动者
    put("them", 37818795, 2)                     # 超魔导龙骑士-真红眼龙骑士（更长的名字）
    put("them", 34645790, 3, attack=False)        # 绯之异解△奈落迦（守备）
    put("them", 0, 4, face_up=False)             # 里侧：只画卡背（卡号 0 = 只有里侧信息）
    put("them", 98806751, 1)                     # 执爱之化卢普（怪兽）
    put("them", 96205925, 2)                     # 异解△福音（永续魔法）→ 魔陷行
    put("them", 14442329, 0)                     # 点唱机酒吧（场地魔法）→ 场地格
    put("them", 50588353, 0, place="extra")      # 水晶机巧-继承玻纤 → 对手额外怪兽区

    return FieldView(
        title="游戏王·当前局面",
        subtitle="群「游戏王测试群」",
        turn=7,
        phase="主要阶段 2",
        top=sides["them"],
        bottom=sides["me"],
        footer="里侧的卡只画卡背（不公开卡面）｜卡面＝完整卡图 + 下面的攻守角标",
    )


def main() -> int:
    """命令行入口：出 HTML（可选地用本机 Edge 渲染成 PNG 预览）。"""

    args = parse_args()
    if not args.demo:
        raise SystemExit("现在只支持 --demo（插件里由 /查房 直接渲染）")

    database = CardDatabase(args.cdb) if args.cdb.is_file() else None
    cache: Dict[int, object] = {}

    def lookup_card(card_id: int) -> Optional[object]:
        """查一张卡的详情（演示局面里同一种卡可能出现在多格，缓存一下）。"""

        if card_id not in cache:
            cache[card_id] = database.card_details([card_id]).get(card_id) if database else None
        return cache[card_id]

    details_of: Optional[Callable[[int], object]] = lookup_card if database is not None else None

    # 演示局面里的卡先补一次整卡图（同步、走 netguard）：图里就能看到"真·卡图"的效果
    pic_dir = cache_dir_for(args.pic_dir)
    demo_ids = [76072561, 63288573, 9753964, 15665977, 34433770, 10045474, 33700664, 84815190,
                49036338, 34645790, 37818795, 98806751, 96205925, 14442329, 50588353]
    if not args.no_fetch:
        fetched = warm_many(demo_ids, pic_dir)
        print(f"整卡图补齐：新下 {len(fetched)} 张（缓存目录 {pic_dir}）")

    html = build_html(demo_view(details_of, args.art_dir, args.art_fallback, pic_dir))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(f"HTML 已写出：{args.out}（{len(html)} 字符）")

    if args.render:
        from playwright.sync_api import sync_playwright

        png = args.out.with_suffix(".png")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(
                viewport={"width": BOARD_WIDTH, "height": BOARD_HEIGHT}, device_scale_factor=2
            )
            # 与 /查房 走同一套等待口径（见 plugin.py 的 _send_field_image）：
            # networkidle + 一点额外时间，卡图才保证画完再截屏
            page.set_content(html, wait_until="networkidle")
            page.wait_for_timeout(300)
            page.screenshot(path=str(png))
            browser.close()
        print(f"PNG 已写出：{png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
