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
    parser.add_argument("--cdb", type=Path, default=DEFAULT_CDB, help="cards.cdb（拿卡名/卡种/攻守）")
    return parser.parse_args()


def demo_view(
    details_of: Optional[Callable[[int], object]], art_dir: Path, art_fallback: Path
) -> FieldView:
    """一张演示局面：尽量铺满（各种召唤种类的怪 + 里侧 + 空位 + 牌堆计数），检查排版与兜底。"""

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
            rank="超量" in type_text,
            level=int(match.group(3)) if match and match.group(3) and face_up else 0,
            type_line=type_text.replace(" ", "/"),
            effect=str(getattr(detail, "effect", "") or "") if face_up else "",
            atk=int(match.group(1)) if match and face_up else None,
            def_=int(match.group(2)) if match and face_up else None,
            art=card_art_uri(card_id, art_dir=art_dir, fallback_dir=art_fallback) if face_up else "",
        )

    us = SideView(label="憨憨", lp=6200, deck="闪刀姬", is_turn=True, grave=4, banished=1, extra_count=2)
    us.monsters[0] = card(76072561)                  # 连接怪
    us.monsters[1] = card(63288573, attack=False)    # 守备怪
    us.monsters[2] = card(9753964)                   # 超量怪（阶级星）
    us.monsters[3] = card(18144506)                  # 同调怪
    us.spells[1] = card(9726840)
    us.spells[3] = card(34433770)
    us.field_zone = card(33700664)
    us.extra = card(84815190)                        # 我方额外怪兽区

    them = SideView(label="嘻嘻$aN9sW", lp=3100, deck="异解", grave=7, banished=2, extra_count=1)
    them.monsters[0] = card(0, face_up=False)        # 里侧：只画卡背
    them.monsters[3] = card(34645790, attack=False)
    them.monsters[4] = card(49036338)
    them.spells[2] = card(96205925)
    them.field_zone = card(18716735)
    them.extra = card(50588353)                      # 对手额外怪兽区
    return FieldView(
        title="游戏王·当前局面",
        subtitle="群「游戏王测试群」",
        turn=7,
        phase="主要阶段 2",
        top=them,
        bottom=us,
        footer="里侧的卡只画卡背（不公开卡面）｜卡面＝标准卡框自绘 + 本机立绘 + 中文卡名/卡文",
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

    html = build_html(demo_view(details_of, args.art_dir, args.art_fallback))
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
