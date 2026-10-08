"""`duel/field_image.py` 的回归测试：**图里画了什么、绝不能画什么**。

这个模块会把局面发到群里，所以最容易出错、也最要紧的是三件事：

1. **里侧的卡只画卡背**（画卡面＝替对手公开信息）；
2. 卡图找不到时**不留白**（画卡名框，按卡种上色）；
3. 格位映射对得上（5 个怪兽区按序号排、额外怪兽区单独一格、场地区在魔陷行最前）。

另外顺带钉住"连接怪不显示守备"（内核那个"守"字段放的是链接标记位，不是数值）。

直接用 ``python tests/test_field_image.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.fieldstate import FieldState, ZoneCard  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.field_image import (  # noqa: E402
    build_html,
    CardView,
    FieldView,
    SideView,
    view_from_state,
)


class _Detail:
    """`CardDatabase.card_details` 那一份的最小替身。"""

    def __init__(self, name: str, type_text: str = "怪兽 效果", stats: str = "攻1700/守400/星4") -> None:
        self.name = name
        self.type_text = type_text
        self.stats = stats


def _details(mapping: dict) -> "object":
    """卡号 → 详情（查不到返回 None）。"""

    def lookup(card_id: int) -> Optional[_Detail]:
        return mapping.get(card_id)

    return lookup


def test_face_down_card_never_reveals_art_or_name() -> None:
    """**里侧只画卡背**：不能出现卡名，也不能带卡图（图会发到群里）。"""

    hidden = CardView(card_id=96205925, name="异解△福音", face_up=False, art="data:image/jpeg;base64,AAAA")
    html = build_html(
        FieldView(
            title="t",
            turn=3,
            top=SideView(label="对手", lp=3000, monsters=[None, hidden, None, None, None]),
            bottom=SideView(label="我方", lp=8000),
        )
    )
    assert "卡背" in html, "里侧的卡必须画成卡背"
    assert "异解△福音" not in html, "里侧的卡不能把卡名画出来"
    assert "AAAA" not in html, "里侧的卡不能带上卡图（即使视图对象里塞了图）"


def test_missing_art_falls_back_to_name_frame() -> None:
    """没有卡图时画卡名框（`noart`），并且带上卡种配色。"""

    html = build_html(
        FieldView(
            title="t",
            turn=1,
            bottom=SideView(
                label="我方",
                lp=8000,
                monsters=[CardView(1, "无图怪", art="")],
                spells=[CardView(2, "无图魔法", kind="spell", art="")],
            ),
        )
    )
    assert "noart" in html and "无图怪" in html and "无图魔法" in html
    assert "--frame:#14503a" in html, "魔法卡要用绿色卡框"


def test_link_monster_shows_attack_only() -> None:
    """连接怪没有守备：角标只报攻击力（内核的"守"字段是链接标记位）。"""

    link = CardView(1, "闪刀姬=零露", link=True, atk=2000, def_=5)
    normal = CardView(2, "绯之异解△奈落迦", link=False, atk=1700, def_=400)
    assert link.stats_text == "ATK 2000", link.stats_text
    assert normal.stats_text == "1700 / 400", normal.stats_text


def test_zones_are_mapped_to_slots() -> None:
    """格位映射：怪兽按序号排、额外怪兽区单独一格、场地区进魔陷行。"""

    state = FieldState(start_lp=8000)
    state.players[0].lp = 6200
    state.players[1].lp = 3100
    state.turn_count = 7
    state.zones[(0, 4, 2)] = ZoneCard(76072561, 0x1)          # 我方：主怪兽区3
    state.zones[(0, 4, 6)] = ZoneCard(63288573, 0x1)          # 我方：额外怪兽区
    state.zones[(0, 8, 0)] = ZoneCard(9726840, 0x5)           # 我方：魔法陷阱区1
    state.zones[(0, 256, 0)] = ZoneCard(33700664, 0x5)        # 我方：场地区
    state.zones[(1, 4, 0)] = ZoneCard(96205925, 0xA)          # 对手：里侧的怪
    details = _details(
        {
            76072561: _Detail("闪刀姬=零露", "怪兽 效果 连接", "攻2000/守5/星2"),
            63288573: _Detail("闪刀姬-燎里", "怪兽 效果 连接", "攻1500/守3/星2"),
            9726840: _Detail("闪刀起动-连刀", "魔法 速攻", ""),
            33700664: _Detail("异解△领域-瓦尔涡罗斯", "魔法 场地", ""),
            96205925: _Detail("异解△福音", "魔法 永续", ""),
        }
    )

    view = view_from_state(
        state,
        seat=0,
        opponent_seat=1,
        our_label="憨憨",
        their_label="嘻嘻$aN9sW",
        our_deck="闪刀姬",
        details_of=details,
        current_seat=0,
    )

    assert view.turn == 7 and view.bottom is not None and view.top is not None
    assert view.bottom.monsters[2] is not None and view.bottom.monsters[2].name == "闪刀姬=零露"
    assert view.bottom.monsters[0] is None, "没放卡的主怪兽区要留空位"
    assert view.bottom.extra is not None and view.bottom.extra.name == "闪刀姬-燎里"
    assert view.bottom.spells[0] is not None and view.bottom.spells[0].kind == "spell"
    assert view.bottom.field_zone is not None and view.bottom.field_zone.name.startswith("异解△领域")
    assert view.bottom.is_turn and not view.top.is_turn
    # 对手那张是里侧的：视图里不许带卡名以外的图，而且 face_up 为假
    assert view.top.monsters[0] is not None and not view.top.monsters[0].face_up
    assert view.bottom.lp == 6200 and view.top.lp == 3100


def test_card_face_shows_chinese_name_type_and_effect() -> None:
    """**卡面是自绘的标准卡框**：中文卡名、类型行、卡文都要画出来（卡图缺了也一样画）。"""

    html = build_html(
        FieldView(
            title="t",
            turn=2,
            bottom=SideView(
                label="我方",
                lp=8000,
                monsters=[
                    CardView(
                        1,
                        "救援少女·卡尔麦尔",
                        atk=2600,
                        def_=1800,
                        level=4,
                        type_line="怪兽/超量/效果",
                        effect="①：这张卡超量召唤的场合才能发动。",
                        art="",
                    )
                ],
            ),
        )
    )
    for needle in ("救援少女·卡尔麦尔", "怪兽/超量/效果", "①：这张卡超量召唤的场合才能发动。", "2600 / 1800"):
        assert needle in html, needle
    assert "rank" in html, "超量的星星要按阶级星画（黑底金星）"


def test_stack_counts_are_rendered() -> None:
    """堆叠计数（墓地/除外/额外）要画在牌堆上——数据来自 fieldstate 的三个计数。"""

    html = build_html(
        FieldView(
            title="t",
            turn=5,
            top=SideView(label="对手", lp=3000, grave=7, banished=2, extra_count=1),
            bottom=SideView(label="我方", lp=8000, grave=4, banished=1, extra_count=2),
        )
    )
    for label in ("墓地", "除外", "额外"):
        assert label in html, label
    for count in ("7", "2", "1", "4"):
        assert f'<div class="pnum">{count}</div>' in html, count


def test_full_card_image_wins_over_drawn_frame() -> None:
    """有**整卡卡图**（缓存里那张 `<卡号>.jpg`）时直接铺它，不再自绘名字条/卡文。

    整卡图来自 `duel/card_images.py` 的缓存（本机缺的卡由它从萌卡那套 CDN 补齐）——
    用户口径："有的会没有卡图，都加上，不许没有卡图"。
    """

    import tempfile

    from duel.field_image import to_card_view
    from duel.fieldstate import ZoneCard

    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory)
        (cache / "12345.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)   # 一张假 JPEG
        card = to_card_view(ZoneCard(12345, 0x1), None, Path("nope"), Path("nope"), cache)
        assert card is not None and card.full.startswith("data:image/jpeg;base64,"), card
        html = build_html(
            FieldView(
                title="t",
                turn=1,
                bottom=SideView(label="我方", lp=8000, monsters=[card]),
            )
        )
        assert 'class="card fullcard"' in html and 'class="cardimg"' in html, "整卡图要铺满卡位"
        assert 'class="cname"' not in html, "有整卡图就不该再叠自绘的名字条"


def test_long_card_name_shrinks_instead_of_being_cut() -> None:
    """卡名**必须完整显示**：长名字自动缩小字号/换行，不能像以前那样一行截断。

    用户口径（2026-10-09）："卡名有的是两行有的是一行，而且有的还是没显示完全"——
    原来名字条固定一行 + overflow:hidden，长名字直接被切掉。
    """

    from duel.field_image import _NAME_FONT_BASE, _name_box

    short_size, short_lines, short_h = _name_box("无限泡影")
    long_size, long_lines, long_h = _name_box("超魔导龙骑士-真红眼龙骑士")
    longer_size, longer_lines, _ = _name_box("真红眼暗钢龙-真红眼黑龙剑士·究极形态")
    assert short_lines == 1 and abs(short_size - _NAME_FONT_BASE) < 0.01, (short_size, short_lines)
    assert long_lines >= 2 and long_size < short_size, (long_size, long_lines)
    assert long_h >= long_lines * long_size, (long_h, long_size, long_lines)
    assert longer_lines >= long_lines and longer_size <= long_size, (longer_size, longer_lines)
    # 端到端：长名字原样出现在 HTML 里，并带上算出来的字号
    html = build_html(
        FieldView(
            title="t",
            turn=1,
            bottom=SideView(
                label="我方", lp=8000, monsters=[CardView(1, "超魔导龙骑士-真红眼龙骑士", art="")]
            ),
        )
    )
    assert "超魔导龙骑士-真红眼龙骑士" in html
    assert f"font-size:{long_size}px" in html, long_size


def test_html_contains_lp_turn_and_phase() -> None:
    """整张图上要有双方 LP、回合与阶段（查房图自带这些信息，群里不用再看文字）。"""

    html = build_html(
        FieldView(
            title="游戏王·当前局面",
            subtitle="群「测试」",
            turn=7,
            phase="主要阶段2",
            top=SideView(label="对手", lp=3100),
            bottom=SideView(label="憨憨", lp=6200, deck="闪刀姬", is_turn=True),
            footer="页脚",
        )
    )
    for needle in ("3100", "6200", "第 7 回合", "主要阶段2", "闪刀姬", "行动中", "页脚"):
        assert needle in html, needle


def _run_all() -> int:
    """不装 pytest 时的自跑入口（与其它测试文件一致）。"""

    tests: List = [
        test_face_down_card_never_reveals_art_or_name,
        test_missing_art_falls_back_to_name_frame,
        test_link_monster_shows_attack_only,
        test_zones_are_mapped_to_slots,
        test_card_face_shows_chinese_name_type_and_effect,
        test_stack_counts_are_rendered,
        test_full_card_image_wins_over_drawn_frame,
        test_long_card_name_shrinks_instead_of_being_cut,
        test_html_contains_lp_turn_and_phase,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✔ {test.__name__}")
        except AssertionError as error:
            failed += 1
            print(f"✘ {test.__name__}: {error}")
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
