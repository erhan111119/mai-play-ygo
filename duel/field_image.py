"""把"当前局面"画成一张对局棋盘图（HTML → 宿主的 `render.html2png` 渲染成 PNG）。

**为什么出图**：`/查房` 原来报的是文字（LP / 怪兽数 / 魔陷数），而群友想看的正是"场上到底什么局面"。
这张图按 **MD 局内**的样子排：深色场地 + 圆角格位，双方各 5 怪兽区 + 5 魔陷区 + 场地区，
**额外怪兽区两格在中间共享**（规则上它本来就不属于某一方），两侧角上是 LP 条（名字 + LP + 条），
右侧一个六边形「第 N 回合 / 阶段」徽章，每侧边上还有牌堆（墓地 / 除外 / 额外，带数字）。

**卡面是"标准游戏王卡框"自绘的**（2026-10-08 起）：本机没有整卡图（只有 624×624 的立绘），
所以卡框我们按卡种上色自己画——名字条（中文卡名）、等级/阶级星、立绘框、类型行、
**中文卡文**（`cards.cdb` 的 desc）、右下攻守；怪兽格下面再压一行 MDPro3 那种大号 `2800/2100`。
卡种配色：怪兽土黄、魔法绿、陷阱紫红；融合紫、同调白、超量黑、连接深蓝（按类型行认）。

四条实现约束（改这个文件时别越过）：

1. **里侧一律画卡背**：对手的盖牌、我们的盖牌都只给卡背——画卡面等于替对手公开信息；
2. **缺图要兜底**：新卡/DIY 卡没有卡图时画"卡名框"（按卡种上色），不能留白；
3. **卡图要全部画出来**：`<img>` + 缩到显示尺寸（`ART_MAX_WIDTH`），配合调用方
   `wait_until="networkidle"`。**别改回 CSS 背景图**——那样截屏会拍到还没解码的空白卡面
   （用户 2026-10-07 报的"很多时候没有渲染成功就发出来了"）；
4. **不碰对局**：本模块只读 recorder 的快照与卡图文件；渲染交给宿主的渲染能力，
   失败由调用方退回文字（见 `plugin.py` 的 `/查房`）。

数据来源：`duel/fieldstate.py` 的 `FieldState`（`zones_of(seat)`：每格的卡号 + 表示形式；
`PlayerField.grave/banished/extra`：牌堆计数）、`duel/cards.py` 的 `CardDatabase`
（卡号 → 中文名 / 卡种 / 种族类型 / 攻守 / 等级 / 卡文）。
"""

from __future__ import annotations

from .card_images import DEFAULT_CACHE_DIR, request_async as _request_card_pic
from .cards import CardDatabase  # noqa: F401  类型提示用（卡名/卡种由调用方注入 details_of）

import base64
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("mai-play-ygo.field_image")

#: 棋盘尺寸（宿主按 device_scale_factor=2 渲染，卡片不会糊）
BOARD_WIDTH = 1280
BOARD_HEIGHT = 720

#: 主怪兽区/魔陷区的格数
MAIN_ZONES = 5

#: 格位与卡面的尺寸（卡面比例按真卡 59:86；格子 96×110 = 卡面 64×93 + 下面那行大号攻守）
ZONE_WIDTH = 96
ZONE_HEIGHT = 110
CARD_WIDTH = 64
CARD_HEIGHT = 93
#: 魔陷行/场地区的格位高度（那些卡不显示大号攻守，矮一截，整张图才排得下）
SPELL_ZONE_HEIGHT = 93

#: 区域号（与 `duel/protocol.py` 的 CardLocation 一致）
MONSTER_ZONE = 4
SPELL_ZONE = 8
FIELD_ZONE = 256

#: 卡图默认目录：插件自带的 `clients/art/`（可用配置 `paths.card_art_dir` 指到别处，
#: 例如 MDPro3 的 `Picture/Art`）。这里只给"没人传参"时的默认值（命令行演示用）。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ART_DIR = _PLUGIN_ROOT / "clients" / "art" / "Art"
DEFAULT_ART_FALLBACK = _PLUGIN_ROOT / "clients" / "art" / "Closeup"

#: 卡种 → 卡框配色（MD 里怪兽偏橙、魔法绿、陷阱紫红）。**测试钉着这三个色**，改值要同步改测试。
_KIND_COLORS = {
    "monster": ("#7a4a18", "rgba(255,190,110,.55)"),
    "spell": ("#14503a", "rgba(120,255,200,.5)"),
    "trap": ("#5a1750", "rgba(255,150,235,.5)"),
}

#: 怪兽里更细的卡框（按卡面的"类型行"认）：连接深蓝、超量黑、同调白、融合紫、仪式靛
_FRAME_BY_MARK = (
    ("连接", ("#123a5e", "rgba(150,220,255,.60)")),
    ("超量", ("#1b1d24", "rgba(255,215,130,.62)")),
    ("同调", ("#3d3a2e", "rgba(255,246,210,.65)")),
    ("融合", ("#4a1f44", "rgba(255,170,240,.60)")),
    ("仪式", ("#243a63", "rgba(170,200,255,.60)")),
)

_STATS_RE = re.compile(r"攻(\d+).*?守(\d+)(?:/星(\d+))?")


def _frame_of(card: "CardView") -> Tuple[str, str]:
    """这张卡的卡框配色（先按超量/同调/融合/连接这些"特殊召唤种类"，再按卡种兜底）。"""

    if card.kind == "monster":
        for mark, colors in _FRAME_BY_MARK:
            if mark in card.type_line:
                return colors
    return _KIND_COLORS.get(card.kind, _KIND_COLORS["monster"])


@dataclass
class CardView:
    """场上的一张卡（已经翻译成"画得出来"的样子）。"""

    card_id: int
    name: str
    face_up: bool = True
    attack: bool = True
    kind: str = "monster"          # monster / spell / trap（决定卡框配色）
    link: bool = False             # 连接怪：没有守备，角标只报攻击力
    atk: Optional[int] = None      # 表侧怪兽才有
    def_: Optional[int] = None
    level: int = 0                 # 等级 / 阶级 / LINK 数（只在连接怪的攻守角标里用：`2000/LINK-2`）
    type_line: str = ""            # 卡面第二行（"怪兽 超量 效果" 这种，来自 cards.cdb 的类型位）
    effect: str = ""               # 中文卡文（卡面下半段那框小字）
    art: str = ""                  # 立绘的 data URI（自绘卡框用）；空 = 没立绘
    full: str = ""                 # **整卡卡图**的 data URI（`duel/card_images.py` 的缓存/在线补齐）；有就用它

    @property
    def stats_text(self) -> str:
        """攻守角标。**连接怪没有守备**（内核那个"守"字段放的是链接标记位），只显示攻击力。"""

        if self.atk is None:
            return ""
        if self.link or self.def_ is None:
            return f"ATK {self.atk}"
        return f"{self.atk} / {self.def_}"

    @property
    def field_text(self) -> str:
        """格位下方那行大号数字：MDPro3 是 `2800/2100`，连接怪写 `2000/LINK-2`。"""

        if self.atk is None:
            return ""
        if self.link:
            return f"{self.atk}/LINK-{self.level}" if self.level else f"{self.atk}"
        if self.def_ is None:
            return f"{self.atk}"
        return f"{self.atk}/{self.def_}"


@dataclass
class SideView:
    """一侧的场面。"""

    label: str
    lp: int
    deck: str = ""
    is_turn: bool = False
    monsters: List[Optional[CardView]] = field(default_factory=lambda: [None] * MAIN_ZONES)
    spells: List[Optional[CardView]] = field(default_factory=lambda: [None] * MAIN_ZONES)
    extra: Optional[CardView] = None          # 额外怪兽区
    field_zone: Optional[CardView] = None     # 场地区
    #: 牌堆计数（魔法陷阱区外侧那几个小堆；卡组张数内核不给，只有这三样能数）
    grave: int = 0
    banished: int = 0
    extra_count: int = 0


@dataclass
class FieldView:
    """整张图的输入。"""

    title: str
    subtitle: str = ""
    turn: int = 0
    phase: str = ""
    top: Optional[SideView] = None            # 对手（画在上半，卡面朝下）
    bottom: Optional[SideView] = None         # 我们（画在下半）
    footer: str = ""


def card_art_uri(
    card_id: int,
    *,
    art_dir: Path = DEFAULT_ART_DIR,
    fallback_dir: Path = DEFAULT_ART_FALLBACK,
) -> str:
    """卡图 → data URI；两个目录都找不到就返回空串（调用方画卡名框）。

    **先把图缩到显示尺寸再嵌**（`ART_MAX_WIDTH`，一张 88×88 的格子按 device_scale_factor=2
    取 176 像素，留一点余量到 200）：本机卡图是 500×500 上下的原图，一副场十几张就是两兆多的
    base64，Chromium 解码要几百毫秒——实测"图还没画完就截屏"，发到群里的棋盘有卡片是空的。
    缩完一张十几 KB，解码几乎瞬时，出图也就稳了。
    """

    raw, mime = _read_art(card_id, art_dir=art_dir, fallback_dir=fallback_dir)
    if not raw:
        return ""
    payload, out_mime = _shrink_art(raw, mime=mime, max_width=ART_MAX_WIDTH)
    return f"data:{out_mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _read_art(
    card_id: int,
    *,
    art_dir: Path,
    fallback_dir: Path,
) -> Tuple[bytes, str]:
    """读卡图字节；两个目录都没有就返回 ``(b"", "")``。"""

    for directory, suffixes in ((art_dir, (".jpg", ".png")), (fallback_dir, (".png", ".jpg"))):
        for suffix in suffixes:
            path = directory / f"{card_id}{suffix}"
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if raw:
                return raw, ("image/png" if suffix == ".png" else "image/jpeg")
    return b"", ""


#: 卡图嵌进 HTML 前的最大宽度（像素）。格子在棋盘上是 88×88，宿主按 2 倍缩放渲染，200 够用。
ART_MAX_WIDTH = 200
#: 缩放后按 JPEG 存的质量：卡面是照片类内容，82 肉眼看不出差别、体积小一大截
ART_JPEG_QUALITY = 82
#: 缩放结果缓存：键是（路径, 修改时间, 目标宽度），值是 (字节, mime)。棋盘一局内会重画多次。
_ART_CACHE: "Dict[Tuple[str, float, int], Tuple[bytes, str]]" = {}
_ART_CACHE_LIMIT = 600


def _shrink_art(raw: bytes, *, mime: str, max_width: int) -> Tuple[bytes, str]:
    """把卡图缩到 ``max_width`` 以内并转成 JPEG；本来就够小就原样返回。

    缩图用宿主环境自带的 pillow（宿主 pyproject 里就是依赖）。**失败不糊过去**：记一条警告并把
    原图交出去——图大一点仍能看，但"为什么这局出图特别慢"要有线索可查。
    """

    key = (hashlib.sha256(raw).hexdigest(), 0.0, max_width)
    cached = _ART_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from PIL import Image  # 宿主依赖，见 pyproject 的 pillow

        with Image.open(io.BytesIO(raw)) as image:
            if image.width <= max_width:
                result = (raw, mime)
            else:
                # 卡图不需要透明通道，转 RGB 才能存 JPEG
                shrunk = image.convert("RGB")
                shrunk.thumbnail((max_width, max_width * 2))
                buffer = io.BytesIO()
                shrunk.save(buffer, format="JPEG", quality=ART_JPEG_QUALITY, optimize=True)
                result = (buffer.getvalue(), "image/jpeg")
    except Exception:  # noqa: BLE001  缩图失败只该慢一点，不该让整张棋盘出不来
        logger.warning("卡图缩放失败，改用原图（出图会慢一些）", exc_info=True)
        result = (raw, mime)
    if len(_ART_CACHE) >= _ART_CACHE_LIMIT:
        _ART_CACHE.clear()
    _ART_CACHE[key] = result
    return result


def _escape(text: object) -> str:
    """HTML 转义（卡名里有 & < > " 时别把页面搞坏）。"""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _kind_of(type_text: str) -> str:
    """`CardDetail.type_text` → 卡框配色用的卡种。"""

    if "陷阱" in type_text:
        return "trap"
    if "魔法" in type_text:
        return "spell"
    return "monster"


#: 名字条可用宽度（卡面 64 − 卡框左右各 3 − 名字条左右各 4）
_NAME_BOX_WIDTH = CARD_WIDTH - 6 - 8#: 名字字号：基准 / 单行允许的最小值 / 多行允许的最小值
_NAME_FONT_BASE = 9.5
_NAME_FONT_MIN_SINGLE = 7.0
_NAME_FONT_MIN_ANY = 6.0


def _archetype_guess(names: Sequence[str], *, min_cards: int = 3) -> str:
    """按"见过的卡名"猜这副牌的主题词（查房图上给对面标个卡组名用）。

    **为什么要猜**（2026-10-09 用户："没有读取对面的卡组名"）：对面的构筑是隐藏信息，内核只在卡
    露出来之后才把卡号给我们，所以"卡组名"这个东西根本拿不到；但同一副牌的主题词会反复出现在卡名里
    （「卡通目录 / 完美世界 卡通世界 / 卡通黑魔术师」→「卡通」；「闪刀姬=零露 / 闪刀起动-连刀」→「闪刀」）。
    这里统计 2~4 字的片段，取"出现在 ≥`min_cards` 张**不同**卡里、且（命中张数, 片段长度）最大"的那个。

    猜不出来（同名词太少）返回空串——图上只写"已见 N 张"，不硬编一个名字。
    """

    hits: Dict[str, set] = {}
    for name in names:
        text = str(name)
        for size in (2, 3, 4):
            for start in range(0, max(0, len(text) - size + 1)):
                token = text[start : start + size]
                if token.strip() != token or not token.isprintable():
                    continue
                hits.setdefault(token, set()).add(text)
    best, best_key = "", (0, 0)
    for token, cards in hits.items():
        if len(cards) < min_cards:
            continue
        if (len(cards), len(token)) > best_key:
            best, best_key = token, (len(cards), len(token))
    return best.rstrip("△▲·・-— 　")     # 主题词尾巴上的符号（「异解△」这种）顺手剪掉


def _name_box(name: str) -> Tuple[float, int, int]:
    """卡名要多大字号、几行才能**完整**显示 → `(字号, 行数, 名字条高度)`。

    **为什么要算**（2026-10-09 用户口径："卡名有的是两行有的是一行，有的还没显示完全"）：
    原来名字条固定一行 + `overflow:hidden`，长名字直接被截掉；而"缺卡图"那版又允许换成两行，
    于是同一张图上出现三种样子。这里按**中文全角 1 个字宽、西文按 0.55** 估宽，
    先试一行、不行退两行、再不行三行，取"行数最少且字号最大"的那个组合——名字始终完整。
    """

    units = 0.0
    for char in str(name):
        units += 1.0 if ord(char) > 0x2E80 else 0.55
    units = max(units, 1.0)
    for lines, minimum in ((1, _NAME_FONT_MIN_SINGLE), (2, _NAME_FONT_MIN_ANY), (3, _NAME_FONT_MIN_ANY)):
        size = min(_NAME_FONT_BASE, _NAME_BOX_WIDTH * lines / units)
        if size >= minimum or lines == 3:
            # ⚠ 向下取整到 0.1px：四舍五入会让人为算出的字号比"装得下"的临界值大一点点，
            # 长名字就又会被切掉一个字（实测 8.0 × 12.55 字宽 = 100.4 > 可用 100）。
            size = max(int(size * 10) / 10.0, _NAME_FONT_MIN_ANY)
            return size, lines, int(round(size * 1.28)) * lines
    return _NAME_FONT_MIN_ANY, 3, int(round(_NAME_FONT_MIN_ANY * 1.28)) * 3


def card_full_uri(card_id: int, *, pic_dir: Optional[Path] = DEFAULT_CACHE_DIR) -> str:
    """**整卡卡图**的 data URI（本机缓存里那张 `<卡号>.jpg`）；没有就返回空串。

    图和立绘走同一套缩放（`_shrink_art`）：整卡图按卡面宽度缩到显示尺寸，一张十几 KB。
    缓存里没有时**顺手排进后台补齐**（`duel/card_images.py` 的单线程池，不联网阻塞出图）——
    下一次 `/查房` 就有整卡图了；这一次先用自绘卡框（立绘 + 中文卡名/卡文）。
    """

    if pic_dir is None or card_id <= 0:
        return ""
    for suffix in (".jpg", ".png"):
        path = Path(pic_dir) / f"{card_id}{suffix}"
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        if not raw:
            return ""
        mime = "image/jpeg" if suffix == ".jpg" else "image/png"
        payload, out_mime = _shrink_art(raw, mime=mime, max_width=ART_MAX_WIDTH)
        if not payload:
            return ""
        return f"data:{out_mime};base64,{base64.b64encode(payload).decode('ascii')}"
    _request_card_pic(card_id, Path(pic_dir))    # 缺图：排进后台补齐，下一次出图就有整卡图
    return ""


def _card_html(card: Optional[CardView], *, flat: bool = False, owner: str = "", flipped: bool = False) -> str:
    """一格：空位 / 卡背 / **完整卡图**（下面写攻守）→ 没有图时画名字框。

    `flat=True` 用在魔陷行/场地区：那些格位不显示下面那行大号攻守，所以矮一截，整张图才排得下。
    `owner`（`opp` / `me`）只给**中央额外怪兽区**用——那两格在上下半场中间，要标清是谁的。
    `flipped=True` 用在**对手半场**：整张卡转 180°（和我们这半场朝向相反，像真实桌面那样，
    一眼就看得出是谁的卡；用户口径 2026-10-09："对面的卡应该朝向和我方的卡朝向相反"）。

    用户口径（2026-10-09）："卡图就直接用完整卡图，不用你做的立绘加卡框了，下面直接写攻击力守备力
    就行，另外注意一下朝向问题就可以了，不用写效果这类"、"去掉星级"。
    """

    plate = "plate flat" if flat else "plate"
    if owner:
        plate += f" own-{owner}"
    if card is None:
        return f'<div class="{plate}"><div class="hole"></div></div>'
    if not card.face_up:
        # 里侧：只画卡背（硬约束，别改成画卡面）
        return f'<div class="{plate}"><div class="card back"><span>卡背</span></div></div>'
    numbers = (
        f'<div class="fnum">{_escape(card.field_text)}</div>'
        if card.kind == "monster" and card.field_text
        else ""
    )
    # 朝向：对手整张卡转 180°，守备表示再各自转 90°（合起来就是 180+90 / 90）
    posture = (" flip" if flipped else "") + ("" if card.attack else " def")
    if card.full:
        return (
            f'<div class="{plate}"><div class="card fullcard{posture}">'
            f'<img class="cardimg" src="{card.full}" alt="">'
            f"</div>{numbers}</div>"
        )
    # 兜底（缓存里暂时没有：正在后台补 / 离线）：只画 名字 + 攻守——**不画立绘、不写效果**
    name_size, _lines, name_height = _name_box(card.name or str(card.card_id))
    name_style = f"font-size:{name_size}px;line-height:{round(name_size * 1.28, 1)}px;max-height:{name_height}px"
    frame, _line = _frame_of(card)
    return (
        f'<div class="{plate}"><div class="card bare{posture}" style="--frame:{frame}">'
        f'<div class="cname" style="{name_style}">{_escape(card.name or card.card_id)}</div>'
        f'<div class="barestat">{_escape(card.field_text)}</div>'
        f"</div>{numbers}</div>"
    )
def _piles_html(side: SideView) -> str:
    """一侧的牌堆（墓地 / 除外 / 额外的数字，MD 图右边那几个小堆）。

    ⚠ 卡组张数内核不告诉我们（对手的构筑是隐藏信息），所以**不画卡组堆的数字**，只画能数出来的三个。
    """

    def pile(label: str, count: int, *, kind: str) -> str:
        return (
            f'<div class="pile {kind}"><div class="pnum">{count}</div>'
            f'<div class="plabel">{label}</div></div>'
        )

    return (
        '<div class="piles">'
        + pile("墓地", side.grave, kind="grave")
        + pile("除外", side.banished, kind="out")
        + pile("额外", side.extra_count, kind="extra")
        + "</div>"
    )


def _lp_html(side: SideView, *, side_tag: str) -> str:
    """LP 条：头像位（画个圆，用名字首字）＋ 名字 ＋ LP 数字 ＋ 血条（相对 8000）。"""

    ratio = max(0.0, min(1.0, side.lp / 8000.0))
    deck = f'<div class="deck">{_escape(side.deck)}</div>' if side.deck else ""
    turn = '<div class="turnchip">行动中</div>' if side.is_turn else ""
    return (
        f'<div class="lpbar {side_tag}">'
        f'<div class="avatar">{_escape(side.label[:1])}</div>'
        f'<div class="lpinfo">'
        f'<div class="who"><span class="name">{_escape(side.label)}</span>{deck}{turn}</div>'
        f'<div class="lpnum">LP {side.lp}</div>'
        f'<div class="lptrack"><div class="lpfill" style="width:{ratio * 100:.1f}%"></div></div>'
        f"</div></div>"
    )


def _row_html(cards: List[Optional[CardView]], *, flat: bool = False) -> str:
    """一行格位（5 格）。"""

    return '<div class="row">' + "".join(_card_html(card, flat=flat) for card in cards) + "</div>"


def _arena_html(view: FieldView) -> str:
    """场地主体：**上半＝对手**（魔陷行/怪兽行）→ **分界缝** → **下半＝我方**（怪兽行/魔陷行）。

    额外怪兽区（两格）放在分界缝的上下两侧——规则上它是双方共用的，所以那两格**按归属上色**
    （对手的用红紫、我方的用蓝），免得看着像"谁的怪都一样"。
    """

    top = view.top
    bottom = view.bottom
    empty_row = [None] * MAIN_ZONES

    def side_row(side: Optional[SideView], *, spells: bool, mirrored: bool) -> str:
        cards = empty_row if side is None else (side.spells if spells else side.monsters)
        # 对手那半场（mirrored）的卡整张转 180°：朝向与我们相反，一眼分得清归属
        cells = "".join(
            _card_html(card, flat=spells, flipped=mirrored)
            for card in (reversed(cards) if mirrored else cards)
        )
        # 场地区贴在魔陷行的**右端**（双方都一样，和客户端里那格的位置一致）
        zone_html = (
            '<div class="zone-tag">'
            + _card_html(side.field_zone, flat=True, flipped=mirrored)
            + '<div class="zlabel">场地</div></div>'
            if side is not None and spells
            else ""
        )
        return f'<div class="row{" toprow" if mirrored else ""}">{cells}{zone_html}</div>'

    def who(side: Optional[SideView]) -> str:
        """这一侧的名字（没给定就空着，别写成 None）。"""

        return _escape(side.label) if side is not None else ""
    return (
        '<div class="arena">'
        '<div class="half opp">'
        f'<div class="sidelabel">对手 · {who(top)}</div>'
        + side_row(top, spells=True, mirrored=True)
        + side_row(top, spells=False, mirrored=True)
        + "</div>"
        # 中间那一行＝双方的额外怪兽区（规则上双方共用，MD 局内也是并排摆在中缝上）；
        # 对手那一格也按他们的朝向转 180°
        + '<div class="row emz">'
        + _card_html(top.extra if top else None, owner="opp", flipped=True)
        + _card_html(bottom.extra if bottom else None, owner="me")
        + "</div>"
        + '<div class="half me">'
        + side_row(bottom, spells=False, mirrored=False)
        + side_row(bottom, spells=True, mirrored=False)
        + f'<div class="sidelabel">我方 · {who(bottom)}</div>'
        + "</div>"
        "</div>"
    )


def _side_panel_html(side: Optional[SideView], *, tag: str) -> str:
    """一侧的边栏（LP 条 + 牌堆）；空的那侧只留位置，别让版面塌掉。"""

    if side is None:
        return f'<div class="panel {tag}"></div>'
    return f'<div class="panel {tag}">{_lp_html(side, side_tag=tag)}{_piles_html(side)}</div>'


_CSS = """
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    width: {width}px; height: {height}px; overflow: hidden; color: #f2f6ff;
    font-family: "Microsoft YaHei", "Segoe UI", "SimHei", sans-serif;
    background:
      radial-gradient(760px 360px at 50% 50%, rgba(120,175,255,.16) 0%, rgba(8,10,22,0) 72%),
      linear-gradient(180deg, #0d1738 0%, #0a1027 45%, #080c1c 100%);
  }}
  /* 场地："石板"底纹（斜向菱形格），加一层中心柔光 */
  .wrap {{ display: flex; flex-direction: column; height: 100%; padding: 6px 14px 8px; gap: 4px; }}
  header {{ display: flex; align-items: baseline; gap: 10px; }}
  .title {{ font-size: 17px; font-weight: 700; letter-spacing: .5px;
    text-shadow: 0 0 12px rgba(120,170,255,.45); }}
  .subtitle {{ font-size: 12px; opacity: .75; }}
  .tourn {{ margin-left: auto; font-size: 12px; opacity: .8; }}

  .stage {{ flex: 1; display: flex; align-items: center; gap: 10px; min-height: 0; }}
  .board {{ flex: 1; height: 100%; border-radius: 16px; padding: 7px 10px; position: relative;
    background:
      repeating-linear-gradient(45deg, rgba(255,255,255,.022) 0 10px, rgba(0,0,0,0) 10px 20px),
      repeating-linear-gradient(-45deg, rgba(255,255,255,.018) 0 10px, rgba(0,0,0,0) 10px 20px),
      radial-gradient(600px 300px at 50% 50%, rgba(90,140,255,.12) 0%, rgba(6,9,20,0) 75%),
      linear-gradient(180deg, #101a3d 0%, #0c1330 60%, #0a1028 100%);
    border: 1px solid rgba(150,190,255,.18);
    box-shadow: inset 0 0 60px rgba(0,0,0,.55), 0 10px 30px rgba(0,0,0,.45); }}
  .arena {{ height: 100%; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 3px; }}
  /* 上下半场各染一层色（对手红紫、我方蓝），中间再压一道分界缝：一眼看出谁是谁的 */
  .half {{ width: 100%; border-radius: 12px; padding: 1px 6px; display: flex;
    flex-direction: column; align-items: center; gap: 3px; position: relative; }}
  .half.opp {{ background: linear-gradient(180deg, rgba(255,90,140,.11), rgba(255,90,140,0));
    box-shadow: inset 0 0 0 1px rgba(255,120,160,.16); }}
  .half.me {{ background: linear-gradient(0deg, rgba(90,160,255,.13), rgba(90,160,255,0));
    box-shadow: inset 0 0 0 1px rgba(120,180,255,.18); }}
  .half .sidelabel {{ position: absolute; left: 6px; top: 50%; transform: translateY(-50%);
    writing-mode: vertical-rl; font-size: 10px; letter-spacing: 2px; opacity: .55; }}
  .seam {{ width: 92%; height: 0; border-top: 1px solid rgba(255,255,255,.18);
    box-shadow: 0 0 10px rgba(160,200,255,.28); }}

  .row {{ display: flex; align-items: flex-start; gap: 6px; }}
  /* 中间那一行＝双方的额外怪兽区：拉开距离，行中间压一道分界缝（上下半场的分界就在这） */
  .row.emz {{ gap: 150px; position: relative; align-items: flex-start;
    padding: 0 6px; }}
  .row.emz::before {{ content: ""; position: absolute; left: 0; right: 0; top: 50%;
    border-top: 1px solid rgba(255,255,255,.16); box-shadow: 0 0 10px rgba(160,200,255,.22);
    pointer-events: none; }}
  .row.emz .plate {{ background: rgba(8,12,26,.55); border-radius: 12px; }}
  .zone-tag {{ display: flex; flex-direction: column; align-items: center; gap: 2px;
    margin: 0 8px; }}
  .zlabel {{ font-size: 10px; opacity: .45; }}
  .plate {{ width: {zone_w}px; height: {zone_h}px; position: relative; display: flex;
    align-items: flex-start; justify-content: center; }}
  .plate.flat {{ height: {spell_h}px; }}      /* 魔陷行/场地区：矮一截（没有大号攻守那行） */
  /* 空格位：凹槽（MD 那种场地刻线，八角形） */
  .plate .hole {{ width: 100%; height: {card_h}px; border-radius: 10px;
    background: linear-gradient(180deg, rgba(150,190,255,.05), rgba(10,16,36,.35));
    border: 1px solid rgba(150,190,255,.16);
    box-shadow: inset 0 2px 10px rgba(0,0,0,.45); }}
  .plate .hole::after {{ content: ""; display: block; margin: 9px auto 0; width: 60%; height: 60%;
    border: 1px dashed rgba(150,190,255,.18); border-radius: 8px; }}
  /* 额外怪兽区：按归属上色（对手红紫 / 我方蓝），那两格在中间，必须看得出是谁的 */
  .plate.own-opp .hole {{ border-color: rgba(255,120,160,.45);
    background: linear-gradient(180deg, rgba(255,90,140,.10), rgba(10,16,36,.35)); }}
  .plate.own-me .hole {{ border-color: rgba(120,180,255,.45);
    background: linear-gradient(180deg, rgba(90,160,255,.12), rgba(10,16,36,.35)); }}
  .plate.own-opp .card {{ box-shadow: 0 4px 10px rgba(0,0,0,.55), 0 0 0 1px rgba(255,120,160,.45); }}
  .plate.own-me .card {{ box-shadow: 0 4px 10px rgba(0,0,0,.55), 0 0 0 1px rgba(120,180,255,.45); }}

  /* ── 卡面：自绘的标准卡框 ───────────────────────────────── */
  .card {{ position: relative; width: {card_w}px; height: {card_h}px; border-radius: 5px;
    overflow: hidden; padding: 3px 3px 0;
    background: linear-gradient(180deg, var(--frame, #7a4a18), #1b1206 92%);
    border: 1px solid var(--line, rgba(255,220,160,.6));
    box-shadow: 0 4px 10px rgba(0,0,0,.55); }}
  .card .cname {{ height: auto; overflow: hidden; color: #241505; font-weight: 700;
    padding: 0 4px; border-radius: 2px; word-break: break-word; text-align: center;
    background: linear-gradient(180deg, #f6e6c2, #d9c294); }}
  .card .artbox {{ height: 38px; margin: 0 1px; overflow: hidden; border: 1px solid rgba(0,0,0,.6);
    background: linear-gradient(160deg, #2b2417, #0c0a06); }}
  .card .art {{ width: 100%; height: 100%; object-fit: cover; object-position: 50% 18%; }}
  /* 完整卡图：直接铺满卡位（就是真卡面）；朝向按半场来 */
  .card.fullcard {{ padding: 0; background: #0b0e18; }}
  .card.fullcard .cardimg {{ width: 100%; height: 100%; object-fit: fill; display: block; }}
  /* 朝向：**对手半场整张卡转 180°**（和我们朝向相反），守备表示再各自转 90° */
  .card.flip {{ transform: rotate(180deg); }}
  .card.def {{ transform: rotate(90deg) scale(.94); }}
  .card.flip.def {{ transform: rotate(270deg) scale(.94); }}
  /* 兜底（暂时没整卡图）：只画 名字 + 攻守，不画立绘/效果/星级（用户口径 2026-10-09："去掉星级"） */
  .card.bare {{ display: flex; flex-direction: column; align-items: stretch; justify-content: center;
    gap: 2px; background:
      repeating-linear-gradient(45deg, rgba(255,255,255,.06) 0 5px, rgba(0,0,0,0) 5px 10px),
      linear-gradient(180deg, var(--frame, #7a4a18), #140e05 92%); }}
  .card.bare .cname {{ background: none; color: #ffeccd; padding: 0 3px; }}
  .card.bare .barestat {{ font-size: 10px; font-weight: 800; color: #ffe9bd; text-align: center;
    text-shadow: 0 1px 2px #000; }}
  .card .tline {{ margin-top: 2px; font-size: 7px; line-height: 10px; height: 10px; overflow: hidden;
    color: #ffe9bd; text-align: left; padding: 0 3px; background: rgba(0,0,0,.3);
    white-space: nowrap; }}
  .card .text {{ margin: 2px 1px; height: 18px; overflow: hidden; font-size: 6.2px; line-height: 7.6px;
    padding: 1px 3px; text-align: left;
    background: rgba(246,240,224,.94); color: #1d1608; border-radius: 1px; }}
  .card .stat {{ position: absolute; right: 4px; bottom: 3px; font-size: 7.5px; font-weight: 700;
    color: #ffe9bd; text-shadow: 0 1px 2px #000; }}
  .card .pos {{ position: absolute; left: 4px; bottom: 3px; font-size: 7.5px; color: #cfe9ff;
    text-shadow: 0 1px 2px #000; }}
  /* 缺卡图：只画卡名框（不留白）——卡位里给一层斜纹 + 类型行，不能是个黑块 */
  .card.noart {{ display: flex; flex-direction: column; align-items: center; justify-content: center;
    background:
      repeating-linear-gradient(45deg, rgba(255,255,255,.07) 0 5px, rgba(0,0,0,0) 5px 10px),
      linear-gradient(180deg, var(--frame, #7a4a18), #140e05 92%); }}
  .card.noart .cname {{ width: 100%; background: none; color: #ffeccd; padding: 2px 3px; }}
  .card.noart .artbox {{ width: 100%; display: flex; align-items: center;
    justify-content: center; border-color: rgba(255,220,160,.35); }}
  .card.noart .artbox::after {{ content: "无卡图"; font-size: 7px; color: rgba(255,235,200,.55); }}
  .card.noart .tline {{ width: 100%; text-align: center; }}
  /* 里侧：只画卡背 */
  .card.back {{ display: flex; align-items: center; justify-content: center;
    background:
      radial-gradient(60% 55% at 50% 45%, rgba(255,214,140,.22), rgba(0,0,0,0) 70%),
      repeating-conic-gradient(from 0deg, rgba(255,205,120,.16) 0deg 12deg, rgba(0,0,0,0) 12deg 24deg),
      linear-gradient(160deg, #3a2a12, #14100a 80%);
    border-color: rgba(255,210,140,.5); }}
  .card.back span {{ font-size: 8px; letter-spacing: 2px; color: rgba(255,235,200,.72); }}
  /* 格位下方的大号攻守（MDPro3 那种）*/
  .plate .fnum {{ position: absolute; bottom: 2px; left: 0; right: 0; text-align: center;
    font-size: 15px; font-weight: 800; letter-spacing: .3px; color: #fff; white-space: nowrap;
    text-shadow: 0 2px 3px #000, 0 0 6px rgba(0,0,0,.9); }}

  /* ── 边栏：LP 条 + 牌堆 ───────────────────────────────── */
  .panel {{ width: 158px; display: flex; flex-direction: column; gap: 8px; }}
  .panel.top {{ order: 2; }}                    /* 对手在右、我们在左（MD 那种左右分栏） */
  .panel.bottom {{ order: 0; }}
  .lpbar {{ display: flex; align-items: center; gap: 7px; padding: 6px 8px; border-radius: 12px;
    background: linear-gradient(180deg, rgba(20,30,64,.92), rgba(10,15,34,.92));
    border: 1px solid rgba(150,190,255,.28); box-shadow: 0 4px 14px rgba(0,0,0,.45); }}
  .avatar {{ width: 30px; height: 30px; border-radius: 50%; flex: none; display: flex;
    align-items: center; justify-content: center; font-size: 13px; font-weight: 700;
    color: #08101f; background: linear-gradient(160deg, #ffe6a8, #e0a94c);
    border: 2px solid rgba(255,235,180,.75); }}
  .lpinfo {{ flex: 1; min-width: 0; }}
  .who {{ display: flex; align-items: center; gap: 5px; font-size: 11px; }}
  .who .name {{ font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .deck {{ font-size: 9px; opacity: .62; white-space: nowrap; }}
  .lpnum {{ font-size: 19px; font-weight: 800; color: #ffe89a; line-height: 1.05;
    text-shadow: 0 0 10px rgba(255,205,90,.4); }}
  .lptrack {{ height: 5px; border-radius: 999px; background: rgba(255,255,255,.14); overflow: hidden; }}
  .lpfill {{ height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, #7ce0b0, #ffe08a); }}
  .turnchip {{ font-size: 9px; padding: 0 5px; border-radius: 999px; white-space: nowrap;
    background: rgba(120,255,190,.16); border: 1px solid rgba(120,255,190,.55); color: #b6ffdd; }}
  .piles {{ display: flex; gap: 6px; justify-content: space-between; }}
  .pile {{ flex: 1; border-radius: 8px; padding: 4px 2px 3px; text-align: center;
    background: rgba(255,255,255,.04); border: 1px solid rgba(150,190,255,.18); }}
  .pile .pnum {{ font-size: 14px; font-weight: 800; color: #dce9ff; line-height: 1.05; }}
  .pile .plabel {{ font-size: 8px; opacity: .6; }}
  .pile.grave {{ background: radial-gradient(circle at 50% 30%, rgba(160,120,255,.28), rgba(0,0,0,.4)); }}
  .pile.out {{ background: radial-gradient(circle at 50% 30%, rgba(255,180,120,.22), rgba(0,0,0,.4)); }}
  .pile.extra {{ background: radial-gradient(circle at 50% 30%, rgba(120,220,255,.22), rgba(0,0,0,.4)); }}

  /* 回合 / 阶段：右侧六边形徽章（MD 局内那个 Turn/Main 牌） */
  .turnbadge {{ width: 62px; flex: none; align-self: center; text-align: center; padding: 9px 4px;
    font-size: 10px; font-weight: 700; line-height: 1.25; color: #fff6dd;
    background: linear-gradient(180deg, #7d1f2a, #4a0f18);
    border: 2px solid rgba(255,210,150,.75);
    clip-path: polygon(50% 0, 100% 25%, 100% 75%, 50% 100%, 0 75%, 0 25%);
    text-shadow: 0 1px 2px #000; }}
  .footer {{ font-size: 10px; opacity: .55; text-align: center; }}
"""


def build_html(view: FieldView) -> str:
    """整张图的 HTML（纯函数，方便单测）。"""

    turn = f"第 {view.turn} 回合" if view.turn else "—"
    phase = f"{_escape(view.phase)}" if view.phase else "—"
    subtitle = f'<span class="subtitle">{_escape(view.subtitle)}</span>' if view.subtitle else ""
    footer = f'<div class="footer">{_escape(view.footer)}</div>' if view.footer else ""
    css = _CSS.format(
        width=BOARD_WIDTH,
        height=BOARD_HEIGHT,
        zone_w=ZONE_WIDTH,
        zone_h=ZONE_HEIGHT,
        spell_h=SPELL_ZONE_HEIGHT,
        card_w=CARD_WIDTH,
        card_h=CARD_HEIGHT,
    )
    badge = f'<div class="turnbadge">Turn {view.turn or "?"}<br>{phase}</div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body><div class="wrap">
  <header>
    <div class="title">{_escape(view.title)}</div>
    {subtitle}
    <div class="tourn">第 {turn} · {phase}</div>
  </header>
  <div class="stage">
    {_side_panel_html(view.bottom, tag="bottom")}
    <div class="board">{_arena_html(view)}</div>
    {badge}
    {_side_panel_html(view.top, tag="top")}
  </div>  {footer}
</div></body></html>"""


def view_from_state(
    field_state: object,
    *,
    seat: int,
    opponent_seat: int,
    our_label: str,
    their_label: str,
    our_deck: str = "",
    their_deck: str = "",
    title: str = "",
    subtitle: str = "",
    phase: str = "",
    art_dir: Path = DEFAULT_ART_DIR,
    art_fallback_dir: Path = DEFAULT_ART_FALLBACK,
    details_of: Optional[Callable[[int], object]] = None,
    current_seat: Optional[int] = None,
    # 整卡图缓存：**默认就用自动预热那份**（`duel/card_images.py`）——调用方不传也能出真卡图
    pic_dir: Optional[Path] = DEFAULT_CACHE_DIR,
) -> FieldView:
    """把记录器的 `FieldState` 翻译成一张图的输入。

    `details_of(card_id)` 返回带 `name` / `type_text` / `stats` / `effect` 的对象（通常是
    `CardDatabase.card_details` 的逐张版本）；不传就只显示卡号。`current_seat` 是**现在轮到谁**。
    `their_deck` 留空时会**按"看牌猜卡组"**填一个（`FieldState.seen_ids()` + `details_of`）。
    """

    zones_of = field_state.zones_of          # type: ignore[attr-defined]

    def deck_label(target_seat: int, given: str) -> str:
        """这一侧显示的卡组名：**给定了就用给定的**（我们这侧是 `room.deck_name`）；
        没给定（对面就是这种情况——内核不给对面卡组名）就按见过的卡名**看牌猜一个**。"""

        if given.strip():
            return given
        seen_of = getattr(field_state, "seen_ids", None)
        if seen_of is None or details_of is None:
            return ""
        names: List[str] = []
        for card_id in seen_of(target_seat):
            try:
                detail = details_of(card_id)
            except Exception:  # noqa: BLE001  猜个名字而已，查不到就跳过
                detail = None
            name = str(getattr(detail, "name", "") or "")
            if name:
                names.append(name)
        if not names:
            return ""
        guess = _archetype_guess(names)
        if guess:
            return f"{guess}（看牌猜，已见 {len(names)} 张）"
        return f"已见 {len(names)} 张"

    def side_of(target_seat: int, label: str, deck: str) -> SideView:
        cards = zones_of(target_seat)
        player = field_state.players.get(target_seat)   # type: ignore[attr-defined]
        view = SideView(
            label=label,
            lp=int(getattr(player, "lp", 0) or 0),
            deck=deck,
            is_turn=current_seat == target_seat,
            # 牌堆计数（墓地/除外/额外）：`fieldstate` 逐次移动维护的那三个数
            grave=int(getattr(player, "grave", 0) or 0),
            banished=int(getattr(player, "banished", 0) or 0),
            extra_count=int(getattr(player, "extra", 0) or 0),
        )
        for index in range(MAIN_ZONES):
            # 怪兽区：给攻守（内核当前值优先，见 `to_card_view`）
            view.monsters[index] = to_card_view(
                cards.get((MONSTER_ZONE, index)), details_of, art_dir, art_fallback_dir, pic_dir,
                monster_zone=True,
            )
            # 魔陷行（含灵摆区最左/最右两格）：不给攻守
            view.spells[index] = to_card_view(
                cards.get((SPELL_ZONE, index)), details_of, art_dir, art_fallback_dir, pic_dir
            )
        for sequence in (5, 6):          # 额外怪兽区（本座位那一格）
            card = to_card_view(
                cards.get((MONSTER_ZONE, sequence)), details_of, art_dir, art_fallback_dir, pic_dir,
                monster_zone=True,
            )
            if card is not None:
                view.extra = card
                break
        # ⚠ 场地区**不是** `(FIELD_ZONE, 0)`：内核把它报在魔陷区的第 6 格（`(SPELL_ZONE, 5)`）——
        # 这正是"查房的图里场地魔法永远不显示"的原因（`fieldstate` 也只收 MONSTER_ZONE/SPELL_ZONES）。
        view.field_zone = to_card_view(
            cards.get((SPELL_ZONE, 5)), details_of, art_dir, art_fallback_dir, pic_dir
        )
        return view

    return FieldView(
        title=title or "游戏王·当前局面",
        subtitle=subtitle,
        turn=int(getattr(field_state, "turn_count", 0) or 0),
        phase=phase,
        top=side_of(opponent_seat, their_label, deck_label(opponent_seat, their_deck)),
        bottom=side_of(seat, our_label, deck_label(seat, our_deck)),
    )


def to_card_view(
    card,
    details_of: Optional[Callable[[int], object]] = None,
    art_dir: Path = DEFAULT_ART_DIR,
    art_fallback_dir: Path = DEFAULT_ART_FALLBACK,
    # 整卡图缓存：**默认就用自动预热那份**（`duel/card_images.py` 的 DEFAULT_CACHE_DIR）。
    # ⚠ 别再改回 None：调用方（`plugin.py` 的 `/查房`）没传这个参数时，None 会让整张图
    # 全部退化成"名字 + 攻守"的兜底卡面（2026-10-09 线上就是这么表现的："卡图没有正确渲染"）。
    pic_dir: Optional[Path] = DEFAULT_CACHE_DIR,
    monster_zone: bool = False,
) -> Optional[CardView]:
    """`ZoneCard` → `CardView`（里侧的卡不读任何卡图：反正要画卡背）。

    ⚠ 攻守的取值顺序（2026-10-09 用户报"查房图里攻击力是原始数值"）：
    **内核下发过的当前值优先**（`ZoneCard.attack/defense`，来自 `MSG_UPDATE_CARD`），
    内核没说过时才退回卡库的卡面数值。原来只读卡库那份，于是装备/场地加成、指示物、
    减攻效果之后图上的数字全是错的。

    `monster_zone=True` 才给攻守：魔陷区/灵摆区/场地区里的卡（含"当装备用的怪兽"）
    **没有攻守可显示**——用户报过"魔陷区的卡也显示攻击力"。
    """

    if card is None:
        return None
    card_id = int(getattr(card, "card_id", 0) or 0)
    face_up = bool(getattr(card, "face_up", True))
    name, kind, atk, def_, link = str(card_id), "monster", None, None, False
    level, type_line, effect = 0, "", ""
    if details_of is not None:
        try:
            detail = details_of(card_id)
        except Exception:  # noqa: BLE001  卡名/卡种查不到不该让整张图崩掉
            detail = None
        if detail is not None:
            type_text = str(getattr(detail, "type_text", ""))
            name = str(getattr(detail, "name", "") or name)
            kind = _kind_of(type_text)
            link = "连接" in type_text
            type_line = type_text.replace(" ", "/")
            match = _STATS_RE.search(str(getattr(detail, "stats", "")))
            if match:
                atk, def_, level = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
            # 卡文只给表侧的卡（里侧的不读，免得哪天顺手画出来）
            if face_up:
                effect = str(getattr(detail, "effect", "") or "")
    if face_up and monster_zone:
        # 当前值优先（内核没下发的字段保持卡面值）；连接怪没有守备力，别把 0 写成守备
        live_atk = int(getattr(card, "attack", -1) or -1)
        live_def = int(getattr(card, "defense", -1) or -1)
        if live_atk >= 0:
            atk = live_atk
        if live_def >= 0 and not link:
            def_ = live_def
    elif not monster_zone:
        # 非怪兽区：攻守一律不显示（场地区/魔陷区/灵摆区都是"没有这些数值"的卡）
        atk, def_ = None, None
    return CardView(
        card_id=card_id,
        name=name,
        face_up=face_up,
        attack=bool(getattr(card, "attack_position", True)),
        kind=kind,
        link=link,
        atk=atk if face_up else None,
        def_=def_ if face_up else None,
        level=level if face_up else 0,
        type_line=type_line,
        effect=effect if face_up else "",
        art=card_art_uri(card_id, art_dir=art_dir, fallback_dir=art_fallback_dir) if face_up else "",
        # 整卡图优先（本机缓存里那份，通常是萌卡的中文卡面）；里侧一律不读
        full=card_full_uri(card_id, pic_dir=pic_dir) if face_up else "",
    )
