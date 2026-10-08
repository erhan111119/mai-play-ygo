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


def test_fallback_card_shows_name_and_stats_only() -> None:
    """暂时没有整卡图（正在后台补 / 离线）时的兜底：**只画名字 + 星级 + 攻守**。

    用户口径（2026-10-09）："不用你做的立绘加卡框了……不用写效果这类"——
    所以兜底里不该出现立绘框、类型行或效果文案。
    """

    html = build_html(
        FieldView(
            title="t",
            turn=1,
            bottom=SideView(
                label="我方",
                lp=8000,
                monsters=[CardView(1, "无图怪", atk=1700, def_=400, level=4, art="",
                                   type_line="怪兽/效果", effect="①：这段效果不该被画出来。")],
                spells=[CardView(2, "无图魔法", kind="spell", art="", type_line="魔法/速攻")],
            ),
        )
    )
    assert "无图怪" in html and "1700/400" in html, "兜底要写名字与攻守"
    assert "这段效果不该被画出来" not in html, "兜底不该画效果文案"
    assert "怪兽/效果" not in html, "兜底不该画类型行"
    assert "--frame:#14503a" in html, "魔法卡兜底要用绿色卡框"


def test_full_image_card_gets_stats_overlay_and_defence_rotation() -> None:
    """**完整卡图** + 下面的攻守；守备表示整张卡转 90°；**不叠星级、不写效果**。

    用户口径（2026-10-09）："卡图就直接用完整卡图……下面直接写攻击力守备力就行，
    另外注意一下朝向问题就可以了，不用写效果这类"、"去掉星级"。
    """

    attack_card = CardView(1, "闪刀姬-燎里", full="data:image/jpeg;base64,AAAA",
                           atk=1500, def_=1000, level=4, type_line="怪兽/效果",
                           effect="①：这段效果不该被画出来。")
    defend_card = CardView(2, "守备怪", full="data:image/jpeg;base64,BBBB",
                           atk=2000, def_=2100, level=8, attack=False)
    html = build_html(
        FieldView(title="t", turn=1, bottom=SideView(label="我方", lp=8000,
                                                     monsters=[attack_card, defend_card]))
    )
    assert html.count('class="cardimg"') == 2, "两张都要用整卡图"
    assert "stars" not in html and "★" not in html, "星级要去掉（卡图里本来就有）"
    assert 'class="card fullcard def"' in html, "守备表示要旋转（朝向问题）"
    assert "1500/1000" in html and "2000/2100" in html, "下面要写攻守"
    assert 'class="cname"' not in html, "有整卡图就不该再叠自绘的名字条"
    assert "这段效果不该被画出来" not in html, "不写效果文案"


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


def test_fallback_keeps_link_rating_readable() -> None:
    """兜底卡面（没有整卡图）只画**名字 + 攻守**；连接怪的攻守角标仍要写清 LINK 数。

    用户口径（2026-10-09）："去掉星级"——星级不再由我们画（完整卡图里本来就有）。
    """

    html = build_html(
        FieldView(
            title="t",
            turn=2,
            bottom=SideView(
                label="我方",
                lp=8000,
                monsters=[
                    CardView(1, "救援少女·卡尔麦尔", atk=2600, def_=1800, level=4),
                    CardView(2, "闪刀姬=零露", atk=2000, level=2, link=True),
                ],
            ),
        )
    )
    assert "救援少女·卡尔麦尔" in html and "闪刀姬=零露" in html
    assert "★" not in html and "stars" not in html, "星级已经去掉，不该再画"
    assert "LINK-2" in html, "连接怪要写 LINK 数"
    assert "2600/1800" in html and "2000/LINK-2" in html, "下面写攻守"


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

    from duel.field_image import _NAME_BOX_WIDTH, _NAME_FONT_BASE, _name_box

    def units(name: str) -> float:
        return sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in name)

    short_size, short_lines, _ = _name_box("无限泡影")
    assert short_lines == 1 and abs(short_size - _NAME_FONT_BASE) < 0.01, (short_size, short_lines)

    # 不变量：**算出来的字号与行数一定装得下这个名字**（每行放得下 size×字宽 个字符）
    for name in ("闪刀姬=零露", "异解△领域-瓦尔涡罗斯", "超魔导龙骑士-真红眼龙骑士",
                 "杀手级调整曲·红印鉴唱片师", "真红眼暗钢龙-真红眼黑龙剑士·究极形态",
                 "Kozmo Dark Destroyer", "D/D/D 超死伟王 白地狱终末神"):
        size, lines, height = _name_box(name)
        assert size * units(name) <= lines * _NAME_BOX_WIDTH + 0.01, (name, size, lines)
        assert height >= lines * size, (name, height, size, lines)
        assert 1 <= lines <= 3, (name, lines)

    # 长名字会退到多行、字号也随之变小（不再是"一行截断"）
    long_size, long_lines, _ = _name_box("超魔导龙骑士-真红眼龙骑士")
    assert long_lines >= 2 and long_size < _NAME_FONT_BASE, (long_size, long_lines)
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
        # 不装 pytest 时的自跑入口：与上面定义的用例保持一致（新增用例记得加到这里）
        test_face_down_card_never_reveals_art_or_name,
        test_fallback_card_shows_name_and_stats_only,
        test_link_monster_shows_attack_only,
        test_zones_are_mapped_to_slots,
        test_fallback_keeps_link_rating_readable,
        test_full_image_card_gets_stats_overlay_and_defence_rotation,
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
