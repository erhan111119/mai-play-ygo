"""把"当前局面"画成一张对局棋盘图（HTML → 宿主的 `render.html2png` 渲染成 PNG）。

**为什么要出图**：`/查房` 原来报的是文字（LP / 怪兽数 / 魔陷数），而群友想看的正是"场上到底什么局面"。
这张图按 MD 那种棋盘排版：双方各 5 个怪兽区 + 5 个魔陷区 + 场地区 + 额外怪兽区，卡面用**本机客户端的
卡图**（默认插件自带的 `clients/art/`，可用配置 `paths.card_art_dir` 指到 MDPro3 的
`Picture/Art/<卡号>.jpg`，缺了退 `Closeup/<卡号>.png`），表侧怪带攻守数值。

四条实现约束（改这个文件时别越过）：

1. **里侧一律画卡背**：对手的盖牌、我们的盖牌都只给卡背——画卡面等于替对手公开信息；
2. **缺图要兜底**：新卡/DIY 卡没有卡图时画"卡名框"（按卡种上色），不能留白；
3. **卡图要全部画出来**：`<img>` + 缩到显示尺寸（`ART_MAX_WIDTH`），配合调用方
   `wait_until="networkidle"`。**别改回 CSS 背景图**——那样截屏会拍到还没解码的空白卡面
   （用户 2026-10-07 报的"很多时候没有渲染成功就发出来了"）；
4. **不碰对局**：本模块只读 recorder 的快照与卡图文件；渲染交给宿主的渲染能力，
   失败由调用方退回文字（见 `plugin.py` 的 `/查房`）。

数据来源：`duel/fieldstate.py` 的 `FieldState`（`zones_of(seat)`：每格的卡号 + 表示形式）、
`duel/cards.py` 的 `CardDatabase`（卡号 → 中文名 / 卡种 / 攻守）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("mai-play-ygo.field_image")

#: 棋盘尺寸（宿主按 device_scale_factor=2 渲染，卡片不会糊）
BOARD_WIDTH = 1280
BOARD_HEIGHT = 720

#: 主怪兽区/魔陷区的格数
MAIN_ZONES = 5

#: 区域号（与 `duel/protocol.py` 的 CardLocation 一致）
MONSTER_ZONE = 4
SPELL_ZONE = 8
FIELD_ZONE = 256

#: 卡图默认目录：插件自带的 `clients/art/`（可用配置 `paths.card_art_dir` 指到别处，
#: 例如 MDPro3 的 `Picture/Art`）。这里只给"没人传参"时的默认值（命令行演示用）。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ART_DIR = _PLUGIN_ROOT / "clients" / "art" / "Art"
DEFAULT_ART_FALLBACK = _PLUGIN_ROOT / "clients" / "art" / "Closeup"

#: 卡种 → 卡框配色（MD 里怪兽偏橙、魔法绿、陷阱紫红）
_KIND_COLORS = {
    "monster": ("#7a4a18", "rgba(255,190,110,.55)"),
    "spell": ("#14503a", "rgba(120,255,200,.5)"),
    "trap": ("#5a1750", "rgba(255,150,235,.5)"),
}

_STATS_RE = re.compile(r"攻(\d+).*?守(\d+)")


@dataclass
class CardView:
    """场上的一张卡（已经翻译成"画得出来"的样子）。"""

    card_id: int
    name: str
    face_up: bool = True
    attack: bool = True
    kind: str = "monster"          # monster / spell / trap（决定卡框配色）
    link: bool = False             # 连接怪：没有守备，攻守角标只报攻击力
    atk: Optional[int] = None      # 表侧怪兽才有
    def_: Optional[int] = None
    art: str = ""                  # 卡图的 data URI；空 = 没有卡图，画卡名框

    @property
    def stats_text(self) -> str:
        """攻守角标。**连接怪没有守备**（内核那个"守"字段放的是链接标记位），只显示攻击力。"""

        if self.atk is None:
            return ""
        if self.link or self.def_ is None:
            return f"ATK {self.atk}"
        return f"{self.atk} / {self.def_}"


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


def _card_html(card: Optional[CardView]) -> str:
    """一格：空位 / 卡背 / 表侧卡。"""

    if card is None:
        return '<div class="slot"></div>'
    if not card.face_up:
        # 里侧：只画卡背（硬约束，别改成画卡面）
        return '<div class="slot"><div class="card back"><span>卡背</span></div></div>'
    bg, border = _KIND_COLORS.get(card.kind, _KIND_COLORS["monster"])
    # ⚠ 卡面用真 `<img>` 而不是 CSS 背景图：宿主渲染时等的是页面的 load 事件，
    # 背景图**不保证**在截屏前已经解码画好（实测发到群里的棋盘常有空卡面），
    # `<img>` 的加载与解码是 load 的一部分，配 contract 里那个 wait_until="networkidle" 才稳。
    art = (
        f'<img class="art{" turned" if not card.attack else ""}" '
        f'src="{card.art}" alt="">'
        if card.art
        else ""
    )
    stats = (
        f'<div class="stats">{_escape(card.stats_text)}</div>'
        if card.stats_text and card.kind == "monster"
        else ""
    )
    position = "" if card.attack else '<div class="pos">守</div>'
    return (
        f'<div class="slot"><div class="card face{" noart" if not card.art else ""}" '
        f'style="--frame:{bg};--line:{border}">{art}{position}'
        f'<div class="cname">{_escape(card.name or card.card_id)}</div>{stats}</div></div>'
    )


def _side_html(side: Optional[SideView], *, mirrored: bool) -> str:
    """一侧的整块。

    排版与客户端一致：**怪兽行 = 5 个怪兽区 + 额外怪兽区**（额外在最外端），
    魔陷行 = **场地区 + 5 个魔陷区**；对手镜像朝上（它的魔陷行在上、怪兽行在下）。
    """

    if side is None:
        return ""
    monsters = "".join(_card_html(card) for card in side.monsters)
    spells = "".join(_card_html(card) for card in side.spells)
    extra = f'<div class="zone-tag">{_card_html(side.extra)}<div class="zlabel">额外</div></div>'
    field_zone = f'<div class="zone-tag">{_card_html(side.field_zone)}<div class="zlabel">场地</div></div>'
    monster_row = f'<div class="row">{monsters}{extra}</div>'
    spell_row = f'<div class="row">{field_zone}{spells}</div>'
    rows = f"{spell_row}{monster_row}" if mirrored else f"{monster_row}{spell_row}"
    sidebar = (
        f'<div class="sidebar"><div class="lp">{side.lp}</div>'
        f'<div class="who">{_escape(side.label)}</div>'
        + (f'<div class="deck">{_escape(side.deck)}</div>' if side.deck else "")
        + ('<div class="turnchip">行动中</div>' if side.is_turn else "")
        + "</div>"
    )
    board = f'<div class="board"><div class="rows">{rows}</div></div>'
    # 对手的 LP 面板放右边、我们的放左边（镜像，和客户端里的相对位置一致）
    inner = board + sidebar if mirrored else sidebar + board
    return f'<div class="side {"top" if mirrored else "bottom"}">{inner}</div>'


_CSS = """
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    width: {width}px; height: {height}px; overflow: hidden;
    font-family: "Microsoft YaHei", "SimHei", sans-serif; color: #eef3ff;
    background:
      radial-gradient(900px 420px at 50% 46%, rgba(90,140,255,.20) 0%, rgba(10,13,30,0) 70%),
      linear-gradient(180deg, #121a3e 0%, #0b1024 55%, #0a0d1e 100%);
  }}
  .wrap {{ display: flex; flex-direction: column; height: 100%; padding: 8px 18px 6px;
    justify-content: center; gap: 14px; }}
  header {{ display: flex; align-items: center; gap: 10px; }}
  .title {{ font-size: 20px; font-weight: 700; letter-spacing: .4px;
    text-shadow: 0 0 12px rgba(120,170,255,.5); }}
  .subtitle {{ font-size: 13px; opacity: .78; }}
  header .right {{ margin-left: auto; display: flex; gap: 8px; align-items: center; }}
  .chip {{ font-size: 12px; padding: 3px 10px; border-radius: 999px;
    background: rgba(120,170,255,.16); border: 1px solid rgba(150,190,255,.45); }}
  .chip.phase {{ background: rgba(255,208,120,.16); border-color: rgba(255,208,120,.5); }}
  .side {{ display: flex; align-items: center; gap: 14px; height: 246px; }}
  .sidebar {{ width: 132px; display: flex; flex-direction: column; gap: 3px; }}
  .side.top .sidebar {{ align-items: flex-end; text-align: right; }}
  .lp {{ font-size: 34px; font-weight: 800; color: #ffe89a; line-height: 1;
    text-shadow: 0 0 16px rgba(255,210,90,.45); }}
  .who {{ font-size: 14px; opacity: .92; }}
  .deck {{ font-size: 12px; opacity: .68; }}
  .turnchip {{ margin-top: 3px; font-size: 11px; padding: 1px 8px; border-radius: 999px;
    background: rgba(120,255,190,.16); border: 1px solid rgba(120,255,190,.55); color: #b6ffdd; }}
  .board {{ flex: 1; display: flex; align-items: center; gap: 10px; }}
  .rows {{ display: flex; flex-direction: column; gap: 8px; }}
  .row {{ display: flex; justify-content: center; }}
  .zone-tag {{ display: flex; flex-direction: column; align-items: center; gap: 2px;
    margin-left: 10px; }}
  .row:last-child .zone-tag:first-child {{ margin-left: 0; margin-right: 10px; }}
  .zlabel {{ font-size: 10px; opacity: .48; }}
  .slot {{ width: 88px; height: 88px; border-radius: 9px;
    background: linear-gradient(180deg, rgba(140,180,255,.09), rgba(18,26,60,.42));
    border: 1px solid rgba(140,180,255,.20); display: flex; align-items: center;
    justify-content: center; box-shadow: inset 0 0 16px rgba(90,140,255,.10); }}
  .card {{ position: relative; width: 88px; height: 88px; border-radius: 9px; overflow: hidden;
    background: linear-gradient(170deg, var(--frame, #2b3a6e), #131a38 78%);
    border: 1px solid var(--line, rgba(200,225,255,.55));
    box-shadow: 0 3px 10px rgba(0,0,0,.5); }}
  .card .art {{ position: absolute; inset: 0 0 18px 0; width: 100%; height: calc(100% - 18px);
    object-fit: cover; object-position: 50% 12%; }}
  .card .art.turned {{ transform: rotate(90deg) scale(.74); }}
  .card .cname {{ position: absolute; left: 0; right: 0; bottom: 0; font-size: 10px; line-height: 1.2;
    padding: 2px 3px 3px; text-align: center; max-height: 34px; overflow: hidden;
    background: linear-gradient(180deg, rgba(6,10,24,0), rgba(6,10,24,.94) 50%); }}
  .card .stats {{ position: absolute; top: 3px; right: 3px; font-size: 10px; font-weight: 700;
    padding: 0 5px; border-radius: 6px; background: rgba(6,10,24,.72);
    border: 1px solid rgba(255,255,255,.22); }}
  .card .pos {{ position: absolute; top: 3px; left: 3px; font-size: 10px; padding: 0 5px;
    border-radius: 6px; background: rgba(6,10,24,.72); border: 1px solid rgba(150,190,255,.45); }}
  .card.noart {{ display: flex; align-items: center; justify-content: center; }}
  .card.noart .cname {{ position: static; background: none; font-size: 12px; max-height: none;
    padding: 4px 5px; }}
  .card.back {{ background:
      repeating-linear-gradient(45deg, rgba(255,255,255,.05) 0 7px, rgba(0,0,0,0) 7px 14px),
      linear-gradient(160deg, #26325f, #101736); display: flex; align-items: center;
    justify-content: center; }}
  .card.back span {{ font-size: 11px; opacity: .55; letter-spacing: 3px; }}
  .footer {{ font-size: 11px; opacity: .6; text-align: center; padding-top: 2px; }}
"""


def build_html(view: FieldView) -> str:
    """整张图的 HTML（纯函数，方便单测）。"""

    turn = f'<span class="chip">第 {view.turn} 回合</span>' if view.turn else ""
    phase = f'<span class="chip phase">{_escape(view.phase)}</span>' if view.phase else ""
    subtitle = f'<span class="subtitle">{_escape(view.subtitle)}</span>' if view.subtitle else ""
    footer = f'<div class="footer">{_escape(view.footer)}</div>' if view.footer else ""
    css = _CSS.format(width=BOARD_WIDTH, height=BOARD_HEIGHT)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body><div class="wrap">
  <header>
    <div class="title">{_escape(view.title)}</div>
    {subtitle}
    <div class="right">{turn}{phase}</div>
  </header>
  {_side_html(view.top, mirrored=True)}
  {_side_html(view.bottom, mirrored=False)}
  {footer}
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
) -> FieldView:
    """把记录器的 `FieldState` 翻译成一张图的输入。

    `details_of(card_id)` 返回带 `name` / `type_text` / `stats` 的对象（通常是
    `CardDatabase.card_details` 的逐张版本）；不传就只显示卡号。`current_seat` 是**现在轮到谁**。
    """

    zones_of = field_state.zones_of          # type: ignore[attr-defined]

    def side_of(target_seat: int, label: str, deck: str) -> SideView:
        cards = zones_of(target_seat)
        player = field_state.players.get(target_seat)   # type: ignore[attr-defined]
        view = SideView(
            label=label,
            lp=int(getattr(player, "lp", 0) or 0),
            deck=deck,
            is_turn=current_seat == target_seat,
        )
        for index in range(MAIN_ZONES):
            view.monsters[index] = to_card_view(
                cards.get((MONSTER_ZONE, index)), details_of, art_dir, art_fallback_dir
            )
            view.spells[index] = to_card_view(
                cards.get((SPELL_ZONE, index)), details_of, art_dir, art_fallback_dir
            )
        for sequence in (5, 6):          # 额外怪兽区（本座位那一格）
            card = to_card_view(
                cards.get((MONSTER_ZONE, sequence)), details_of, art_dir, art_fallback_dir
            )
            if card is not None:
                view.extra = card
                break
        view.field_zone = to_card_view(
            cards.get((FIELD_ZONE, 0)), details_of, art_dir, art_fallback_dir
        )
        return view

    return FieldView(
        title=title or "游戏王·当前局面",
        subtitle=subtitle,
        turn=int(getattr(field_state, "turn_count", 0) or 0),
        phase=phase,
        top=side_of(opponent_seat, their_label, their_deck),
        bottom=side_of(seat, our_label, our_deck),
    )


def to_card_view(
    card,
    details_of: Optional[Callable[[int], object]] = None,
    art_dir: Path = DEFAULT_ART_DIR,
    art_fallback_dir: Path = DEFAULT_ART_FALLBACK,
) -> Optional[CardView]:
    """`ZoneCard` → `CardView`（里侧的卡不读卡图：反正要画卡背）。"""

    if card is None:
        return None
    card_id = int(getattr(card, "card_id", 0) or 0)
    face_up = bool(getattr(card, "face_up", True))
    name, kind, atk, def_, link = str(card_id), "monster", None, None, False
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
            match = _STATS_RE.search(str(getattr(detail, "stats", "")))
            if match:
                atk, def_ = int(match.group(1)), int(match.group(2))
    return CardView(
        card_id=card_id,
        name=name,
        face_up=face_up,
        attack=bool(getattr(card, "attack_position", True)),
        kind=kind,
        link=link,
        atk=atk if face_up else None,
        def_=def_ if face_up else None,
        art=card_art_uri(card_id, art_dir=art_dir, fallback_dir=art_fallback_dir) if face_up else "",
    )
