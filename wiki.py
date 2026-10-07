"""百科检索（原「游戏王百科检索」插件，已并入「麦麦玩游戏王」）。

三个 LLM 工具：`ygo_card_search`（查卡）／`ygo_deck_analyze`（解析卡组码）／
`ygo_card_image`（发卡图）。参数与返回结构保持原样，只有工具名统一成 `ygo_` 前缀
（本插件所有工具都用这个前缀，避免与别的插件撞名）。

并入之后的三处改进（都是"一体"带来的红利）：

1. **本地卡库优先**：解析卡组码时先用插件自己的 `duel/cards.py` 读本机 `cards.cdb`
   （一次查询拿全、离线可用），只有本机没有的卡才回落到 ygocdb 接口
   —— 原实现是"每张卡一次 HTTP 请求"，一副 40 张的卡组要等十几秒；
2. **卡图本地优先**：优先用本机客户端的卡图（`MDPro3/Picture/Art` → `Closeup`），
   拉了不到才去 CDN 下载；
3. **外网请求一律走 `duel/netguard`**：只允许 https、只连公网地址、限制重定向与响应体积
   —— 插件里的任何出网请求都必须过这道校验，别在这里另写裸请求。

外网依赖：查卡接口（默认 `ygocdb.com/api/v0/`，可在配置 `wiki.endpoint` 换成自建/镜像）
与在线卡图 CDN（本地卡图找不到时才用）。两项都是"拿不到就回落到本地/不附图"，不会让工具报错。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import asyncio
import base64
import json
import logging
import re
import struct
import time

from maibot_sdk import Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .duel.netguard import guarded_request

logger = logging.getLogger("mai-play-ygo.wiki")

#: 在线接口的响应上限（卡图比 JSON 大，单独给一档）
_JSON_BYTES = 2 * 1024 * 1024
_IMAGE_BYTES = 4 * 1024 * 1024

#: 查询地址与超时时间在配置里（`wiki.endpoint` / `wiki.timeout`，用户只配这两项）；
#: 下面这些写死在代码里——它们不是"要调的旋钮"，只是上限与常量。
#: 一次返回几张卡：工具会把结果列给模型看，太多会挤掉对话上下文
MAX_SEARCH_RESULTS = 5
#: 查询缓存时长（秒）：同一张卡反复问时不再打网络；0 表示不缓存
CACHE_TTL_SECONDS = 3600
#: 在线卡图的 CDN（本地卡图找不到时才用它）
CARD_IMAGE_CDN = "https://cdn.233.momobako.com/ygopro/pics"

# ====== 卡牌数据工具函数 ======

# 游戏王卡牌类型映射（位 → 中文）
TYPE_MAP = {
    1: "怪兽", 2: "魔法", 4: "陷阱", 16: "效果", 32: "融合", 64: "仪式", 128: "陷阱怪兽",
    256: "灵魂", 512: "同盟", 1024: "二重", 2048: "调整", 4096: "同调", 8192: "衍生物",
    16384: "速攻", 32768: "永续", 65536: "装备", 131072: "场地", 262144: "反击",
    524288: "翻转", 1048576: "卡通", 2097152: "命运", 4194304: "XYZ", 8388608: "灵摆",
    16777216: "特殊召唤", 33554432: "连接",
}

ATTRIBUTE_MAP = {1: "地", 2: "水", 4: "炎", 8: "风", 16: "光", 32: "暗", 64: "神"}

RACE_MAP = {
    1: "战士", 2: "魔法师", 4: "天使", 8: "恶魔", 16: "不死", 32: "机械", 64: "水",
    128: "炎", 256: "岩石", 512: "鸟兽", 1024: "植物", 2048: "昆虫", 4096: "雷",
    8192: "龙", 16384: "兽", 32768: "鱼", 65536: "海龙", 131072: "爬虫类",
    262144: "恐龙", 524288: "幻神兽", 1048576: "创造神",
}


def decode_type(type_val: int) -> List[str]:
    """解码卡类型位掩码。"""

    return [name for bit, name in TYPE_MAP.items() if type_val & bit]


def decode_attribute(attr_val: int) -> str:
    """解码属性位掩码。"""

    for bit, name in ATTRIBUTE_MAP.items():
        if attr_val & bit:
            return name
    return "未知"


def decode_race(race_val: int) -> str:
    """解码种族位掩码。"""

    for bit, name in RACE_MAP.items():
        if race_val & bit:
            return name
    return "未知"


def format_card_info(card: Dict) -> str:
    """把 ygocdb 的一张卡格式化成可读文本（供模型直接读）。"""

    data = card.get("data", {})
    text = card.get("text", {})
    html = card.get("html", {})

    cn_name = card.get("cn_name") or card.get("sc_name") or "未知"
    types = decode_type(data.get("type", 0))

    lines = [f"【{cn_name}】"]
    sub_names = []
    if card.get("jp_name"):
        sub_names.append(f"JP: {card['jp_name']}")
    if card.get("en_name"):
        sub_names.append(f"EN: {card['en_name']}")
    if sub_names:
        lines.append(" | ".join(sub_names))

    lines.append("─" * 30)

    if "怪兽" in types:
        level = data.get("level", 0)
        atk = data.get("atk", 0)
        def_val = data.get("def", 0)
        race = decode_race(data.get("race", 0))
        attribute = decode_attribute(data.get("attribute", 0))
        lines.append(f"[{'/'.join(types)}] {race}/{attribute}")
        if "连接" in types:
            # 连接怪兽没有守备力，等级字段放的是连接值
            lines.append(f"[LINK-{level}] ATK/{atk}")
        else:
            lines.append(f"[{'★' * level}] ATK/{atk} DEF/{def_val}")
    else:
        lines.append(f"[{'/'.join(types)}]")

    lines.append("─" * 30)

    desc = html.get("desc") or text.get("desc") or ""
    if desc:
        lines.append(desc)

    pdesc = html.get("pdesc") or text.get("pdesc") or ""
    if pdesc:
        lines.extend(["", "【灵摆效果】", pdesc])

    faqcount = card.get("faqcount", 0)
    if faqcount > 0:
        lines.extend(["", f"💡 有 {faqcount} 条 FAQ"])

    return "\n".join(lines)


# ====== 卡组码解析 ======


def parse_ydk(deck_text: str) -> dict:
    """解析 YDK 文本（`#main` / `#extra` / `!side` 分段，每行一个卡号）。"""

    main_deck: List[int] = []
    extra_deck: List[int] = []
    side_deck: List[int] = []
    current_section = "main"

    for raw in deck_text.strip().split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("!"):
            lower = line.lower()
            if "extra" in lower:
                current_section = "extra"
            elif "side" in lower:
                current_section = "side"
            elif "main" in lower:
                current_section = "main"
            continue
        if line.isdigit():
            card_id = int(line)
            if current_section == "main":
                main_deck.append(card_id)
            elif current_section == "extra":
                extra_deck.append(card_id)
            elif current_section == "side":
                side_deck.append(card_id)

    return {"main": main_deck, "extra": extra_deck, "side": side_deck, "format": "ydk"}


def _decode_ydke_part(encoded: str) -> List[int]:
    """YDKE 的一段：URL-safe base64 → 小端 u32 卡号列表。"""

    encoded = encoded.replace("-", "+").replace("_", "/")
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    try:
        raw = base64.b64decode(encoded)
    except Exception:  # noqa: BLE001  坏码只当空段，不抛
        return []
    ids = []
    for i in range(0, len(raw), 4):
        if i + 4 <= len(raw):
            ids.append(struct.unpack("<I", raw[i : i + 4])[0])
    return ids


def parse_ydke(ydke_url: str) -> dict:
    """解析 `ydke://main!extra!side` 链接。"""

    if ydke_url.startswith("ydke://"):
        ydke_url = ydke_url[7:]
    parts = ydke_url.split("!")
    if len(parts) < 2:
        raise ValueError("YDKE 格式错误")
    return {
        "main": _decode_ydke_part(parts[0]),
        "extra": _decode_ydke_part(parts[1]) if len(parts) > 1 else [],
        "side": _decode_ydke_part(parts[2]) if len(parts) > 2 else [],
        "format": "ydke",
    }


def detect_deck_format(deck_str: str) -> str:
    """认一认这段卡组码是什么格式（ydk / ydke / ourygo / unknown）。"""

    deck_str = deck_str.strip()
    if deck_str.startswith("ydke://"):
        return "ydke"
    if "deck.ourygo.top" in deck_str and "d=" in deck_str:
        return "ourygo"
    if "#main" in deck_str or "#extra" in deck_str or "!side" in deck_str:
        return "ydk"
    lines = [line.strip() for line in deck_str.split("\n") if line.strip()]
    if lines and all(line.isdigit() for line in lines[:5]):
        return "ydk"
    if re.match(r"^[A-Za-z0-9_-]+=*$", deck_str) and len(deck_str) > 50:
        return "ourygo"
    return "unknown"


def parse_ourygo(deck_str: str) -> dict:
    """解析萌卡（ourygo）分享链接：每张卡 29 位（2 位张数 + 27 位卡号）。"""

    match = re.search(r"[?&]d=([^&\s]+)", deck_str)
    d_param = match.group(1) if match else deck_str.strip()
    d_param = d_param.replace("-", "+").replace("_", "/")
    padding = 4 - len(d_param) % 4
    if padding != 4:
        d_param += "=" * padding
    try:
        decoded = base64.b64decode(d_param)
    except Exception as error:  # noqa: BLE001
        raise ValueError(f"base64 解码失败: {error}") from error

    bit_str = "".join(format(byte, "08b") for byte in decoded)
    if len(bit_str) < 16:
        raise ValueError("数据太短")

    main_count = int(bit_str[0:8], 2)
    extra_count = int(bit_str[8:12], 2)
    side_count = int(bit_str[12:16], 2)

    pos = 16
    sections: Dict[str, List[int]] = {"main": [], "extra": [], "side": []}
    for key, count in (("main", main_count), ("extra", extra_count), ("side", side_count)):
        for _ in range(count):
            if pos + 29 > len(bit_str):
                break
            bits = bit_str[pos : pos + 29]
            copies = int(bits[0:2], 2)
            code = int(bits[2:29], 2)
            sections[key].extend([code] * copies)
            pos += 29

    return {
        "main": sections["main"],
        "extra": sections["extra"],
        "side": sections["side"],
        "format": "ourygo",
    }


def parse_deck(deck_str: str) -> dict:
    """自动识别格式并解析卡组码。"""

    fmt = detect_deck_format(deck_str)
    if fmt == "ydk":
        return parse_ydk(deck_str)
    if fmt == "ydke":
        return parse_ydke(deck_str)
    if fmt == "ourygo":
        return parse_ourygo(deck_str)
    raise ValueError(f"无法识别的卡组格式: {fmt}")


class YugiohWikiTools:
    """百科检索的三个工具（混入到主插件类里；实例上要有 `ctx` / `config`）。

    为什么做成混入类而不是独立插件：宿主收集组件用的是 `dir(instance)`，会遍历继承链——
    所以混入类的 `@Tool` 一样能被注册，而两个插件的运行数据（卡库、卡图目录、会话）
    可以共用同一份配置与同一批运行时模块。
    """

    # ---- 缓存 ----------------------------------------------------------------

    def _endpoint(self) -> str:
        """查卡接口地址：配置里的 `wiki.endpoint`，末尾补一个斜杠（拼 `?search=` 用）。"""

        return self.config.wiki.endpoint.strip().rstrip("/") + "/"

    def _wiki_store(self) -> Dict[str, Any]:
        """懒建 TTL 缓存（键 → 值）与时间表。"""

        if not hasattr(self, "_wiki_cache_store"):
            self._wiki_cache_store: Dict[str, Any] = {}
            self._wiki_cache_times: Dict[str, float] = {}
        return self._wiki_cache_store

    def _get_cache(self, key: str) -> Optional[Any]:
        """读缓存（过期就删）。"""

        store = self._wiki_store()
        if key not in store:
            return None
        if CACHE_TTL_SECONDS <= 0 or time.time() - self._wiki_cache_times[key] <= CACHE_TTL_SECONDS:
            return store[key]
        del store[key]
        del self._wiki_cache_times[key]
        return None

    def _set_cache(self, key: str, value: Any) -> None:
        """写缓存。"""

        self._wiki_store()[key] = value
        self._wiki_cache_times[key] = time.time()

    @staticmethod
    def _session_from(kwargs: Dict[str, Any]) -> str:
        """从工具调用参数里取会话号（宿主注入 `stream_id`；老版本叫 `session_id`）。

        工具要"顺手把结果发到群里"就得有会话号，取不到就只是返回值——**不要 `del kwargs`**：
        以前 `get_card_image` 开头 `del kwargs`、后面又 `.get("stream_id")`，于是发卡图这条
        永远抛 `UnboundLocalError`（被外层 except 吞掉，群里只看到"取卡图失败"）。
        """

        return str(kwargs.get("stream_id") or kwargs.get("session_id") or "")

    # ---- 本机卡库 / 卡图（离线优先）-----------------------------------------

    def _local_details(self, card_ids: List[int]) -> Dict[int, Any]:
        """用本机 `cards.cdb` 批量取卡（离线、一次查询）——没有卡库就返回空。"""

        database = getattr(self, "_card_db", None)
        if database is None or not card_ids:
            return {}
        try:
            return database.card_details(list(card_ids))
        except Exception:  # noqa: BLE001  本地卡库出问题就回落接口，不影响工具
            logger.warning("本地卡库查询失败，改用在线检索", exc_info=True)
            return {}

    def _local_card_image(self, card_id: int) -> str:
        """本机卡图 → base64（找不到返回空串）。"""

        from .duel.field_image import card_art_uri

        paths = self.config.paths
        uri = card_art_uri(
            card_id,
            art_dir=paths.resolved_card_art_dir(),
            fallback_dir=paths.resolved_card_art_fallback_dir(),
        )
        if not uri:
            return ""
        return uri.split(",", 1)[1] if "," in uri else ""

    # ---- 在线检索（全部走 netguard：https + 公网 + 限体积）------------------

    async def _search_cards(self, keyword: str) -> List[Dict[str, Any]]:
        """按关键词在线搜卡（ygocdb），带 TTL 缓存。"""

        cached = self._get_cache(f"search:{keyword}")
        if cached is not None:
            return cached

        url = f"{self._endpoint()}?search={quote(keyword, safe='')}"

        def _fetch() -> dict:
            body = guarded_request(
                url,
                headers={"User-Agent": "MaiBot-MaiPlayYgo/1.0", "Accept": "application/json"},
                timeout=self.config.wiki.timeout,
                max_bytes=_JSON_BYTES,
            )
            return json.loads(body.decode("utf-8"))

        try:
            data = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except Exception as error:  # noqa: BLE001  网络/校验问题都不该让工具抛异常
            logger.error("搜索卡牌失败: %s", error)
            return []

        results = list(data.get("result", []))[:MAX_SEARCH_RESULTS]
        self._set_cache(f"search:{keyword}", results)
        logger.info("搜索 '%s' 找到 %d 张卡", keyword, len(results))
        return results

    async def _fetch_card_by_id(self, card_id: int) -> Optional[Dict[str, Any]]:
        """按卡号在线取一张卡（带缓存）。"""

        cache_key = f"id:{card_id}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached[0] if cached else None

        url = f"{self._endpoint()}?search={int(card_id)}"

        def _fetch() -> dict:
            body = guarded_request(
                url,
                headers={"User-Agent": "MaiBot-MaiPlayYgo/1.0", "Accept": "application/json"},
                timeout=self.config.wiki.timeout,
                max_bytes=_JSON_BYTES,
            )
            return json.loads(body.decode("utf-8"))

        try:
            result = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except Exception as error:  # noqa: BLE001
            logger.warning("查询卡牌 %s 失败: %s", card_id, error)
            self._set_cache(cache_key, [])
            return None

        matched = next(
            (card for card in result.get("result", []) if str(card.get("id")) == str(card_id)), None
        )
        self._set_cache(cache_key, [matched] if matched else [])
        return matched

    async def _fetch_card_image_online(self, card_id: int) -> str:
        """在线卡图（CDN）→ base64（失败返回空串）。"""

        url = f"{CARD_IMAGE_CDN}/{int(card_id)}.jpg"

        def _download() -> bytes:
            return guarded_request(
                url,
                headers={"User-Agent": "MaiBot-MaiPlayYgo/1.0", "Referer": "https://ygocdb.com/"},
                timeout=self.config.wiki.timeout,
                max_bytes=_IMAGE_BYTES,
            )

        try:
            data = await asyncio.get_running_loop().run_in_executor(None, _download)
        except Exception as error:  # noqa: BLE001
            logger.warning("下载卡图 %s 失败: %s", card_id, error)
            return ""
        return base64.b64encode(data).decode("utf-8")

    # ---- 工具 ----------------------------------------------------------------

    @Tool(
        "ygo_card_search",
        brief_description=(
            "搜索游戏王卡牌信息。当用户询问游戏王卡牌效果、攻击力、防御力、"
            "卡牌类型、规则等相关问题时，调用本工具检索准确的卡牌信息。"
        ),
        detailed_description=(
            "游戏王卡牌查询工具，支持中文、日文、英文卡牌名搜索。"
            "返回卡牌的完整信息，包括：卡名、类型、属性、种族、等级、攻击力、防御力、卡牌效果等。"
            "当对话中涉及游戏王卡牌相关内容，需要准确的卡牌信息时，请调用本工具。"
        ),
        parameters=[
            ToolParameterInfo(
                name="keyword",
                type=ToolParamType.STRING,
                description="要搜索的卡牌名称或关键词，可以是中文、日文、英文",
                required=True,
            ),
        ],
        chat_scope="all",
        visibility="visible",
        timeout_ms=30000,
    )
    async def search_card(self, keyword: str, **kwargs) -> Dict[str, Any]:
        """搜索卡牌信息。"""

        del kwargs
        try:
            cards = await self._search_cards(keyword.strip())
            if not cards:
                return {"success": False, "message": f"未找到与 '{keyword}' 相关的卡牌", "results": []}

            formatted_results = []
            text_parts = []
            for index, card in enumerate(cards[:5]):  # 最多展示 5 张
                info = format_card_info(card)
                formatted_results.append(
                    {
                        "id": card.get("id"),
                        "name": card.get("cn_name") or card.get("sc_name"),
                        "type": card.get("data", {}).get("type", 0),
                        "info": info,
                    }
                )
                text_parts.append(info)
                if index < len(cards) - 1 and index < 4:
                    text_parts.append("")  # 空行分隔

            result_text = "\n".join(text_parts)
            return {
                "success": True,
                "message": f"找到 {len(cards)} 张相关卡牌",
                "content": result_text,
                "count": len(cards),
                "results": formatted_results,
                "full_text": result_text,
            }
        except Exception as error:  # noqa: BLE001
            logger.error("搜索卡牌失败: %s", error)
            return {"success": False, "message": f"搜索失败: {error}", "results": []}

    @Tool(
        "ygo_deck_analyze",
        brief_description=(
            "解析游戏王卡组码并分析卡组。支持 YDK、YDKE 与萌卡分享链接。当用户发送卡组码、卡组链接、"
            "要求分析/查看/评价卡组、或询问某卡组怎么玩时调用。"
            "如果用户只说了卡组名没有发卡组码，可以先用搜索工具找到卡组码再解析。"
        ),
        detailed_description="""
解析游戏王卡组码，自动识别 YDK / YDKE / 萌卡（ourygo）三种格式，返回主卡组、额外卡组、副卡组的完整卡牌列表。

支持的格式：
1. YDK：文本格式，每行一个卡牌 ID，用 #main #extra !side 分隔
2. YDKE：ydke:// 开头的 URL 格式，EDOPro 等软件使用
3. 萌卡分享链接：http://deck.ourygo.top?name=...&d=...

解析完卡组后，可以结合卡组构筑知识给出：卡组类型判断、核心展开点与关键卡、常见 combo 路线、
对局思路与注意事项、可以优化的地方。

不要凭记忆回答卡组内容，务必调用工具解析。
        """,
        parameters=[
            ToolParameterInfo(
                name="deck_code",
                param_type=ToolParamType.STRING,
                description="卡组码（YDK 文本 / YDKE URL / 萌卡分享链接）",
                required=True,
            ),
        ],
        chat_scope="all",
        visibility="visible",
        timeout_ms=60000,
    )
    async def analyze_deck(self, deck_code: str, **kwargs) -> Dict[str, Any]:
        """解析卡组码并返回卡组信息。"""

        try:            deck = parse_deck(deck_code.strip())
        except Exception as error:  # noqa: BLE001
            return {"success": False, "message": f"卡组解析失败: {error}"}

        main_ids, extra_ids, side_ids = deck["main"], deck["extra"], deck["side"]
        fmt = deck["format"]
        main_count, extra_count, side_count = len(main_ids), len(extra_ids), len(side_ids)
        total = main_count + extra_count + side_count

        unique_ids = sorted({*main_ids, *extra_ids, *side_ids})

        # ① 本机卡库先来一遍（一次查询、离线、快）
        local = self._local_details(unique_ids)
        names: Dict[int, str] = {
            card_id: str(getattr(detail, "name", "") or f"ID:{card_id}")
            for card_id, detail in local.items()
        }

        # ② 本机没有的（新卡/DIY）才逐张问在线接口
        for card_id in [cid for cid in unique_ids if cid not in local]:
            card = await self._fetch_card_by_id(card_id)
            if card:
                names[card_id] = card.get("cn_name") or card.get("sc_name") or f"ID:{card_id}"
            else:
                names.setdefault(card_id, f"未知卡(ID:{card_id})")

        def format_section(card_ids: List[int], title: str) -> str:
            """一段卡组（同名合并计数）。"""

            if not card_ids:
                return f"【{title}】无"
            counts: Dict[str, int] = {}
            for card_id in card_ids:
                label = names.get(card_id) or f"未知卡(ID:{card_id})"
                counts[label] = counts.get(label, 0) + 1
            lines = [f"【{title}】({len(card_ids)}张)"]
            for label, count in sorted(counts.items()):
                lines.append(f"  {label} ×{count}" if count > 1 else f"  {label}")
            return "\n".join(lines)

        full_text = f"卡组解析成功（{fmt.upper()}格式）\n\n"
        full_text += f"总计 {total} 张（主 {main_count} / 额外 {extra_count} / 副 {side_count}）\n\n"
        full_text += format_section(main_ids, "主卡组") + "\n\n"
        full_text += format_section(extra_ids, "额外卡组")
        if side_count > 0:
            full_text += "\n\n" + format_section(side_ids, "副卡组")

        # 顺手把结果发到会话里（拿得到 stream_id 才发）
        stream_id = self._session_from(kwargs)
        if stream_id:
            try:
                await self.ctx.send.text(full_text, stream_id)
            except Exception as error:  # noqa: BLE001  发不出去不影响工具返回
                logger.warning("发送卡组解析结果失败: %s", error)

        def deck_rows(card_ids: List[int]) -> List[Dict[str, Any]]:
            return [{"id": cid, "name": names.get(cid) or f"ID:{cid}"} for cid in card_ids]

        return {
            "success": True,
            "message": f"卡组解析成功，共 {total} 张卡",
            "content": full_text,
            "format": fmt,
            "main_count": main_count,
            "extra_count": extra_count,
            "side_count": side_count,
            "total_count": total,
            "main_deck": deck_rows(main_ids),
            "extra_deck": deck_rows(extra_ids),
            "side_deck": deck_rows(side_ids),
            "full_text": full_text,
        }

    @Tool(
        "ygo_card_image",
        brief_description="获取游戏王卡牌图片。当用户想看某张卡的卡图、要求发卡牌图片、或者需要展示卡牌外观时调用。",
        detailed_description="""
获取游戏王卡牌图片并发送到当前会话。
支持通过卡牌名称或卡牌 ID 获取图片。

使用场景：用户说"给我看一下青眼白龙的卡图"、"发一张 XXX 的图片"、"这张卡长什么样"。

注意：先搜索找到正确的卡牌，再获取图片；如果有多张同名卡，返回最相关的那张。
本机有卡图（客户端 `Picture/Art` / `Closeup`）时**直接用本机的**，没有再走在线 CDN。
        """,
        parameters=[
            ToolParameterInfo(
                name="card_name",
                param_type=ToolParamType.STRING,
                description="卡牌名称（中文/日文/英文均可），或者卡牌 ID",
                required=True,
            ),
        ],
        chat_scope="all",
        visibility="visible",
        timeout_ms=30000,
    )
    async def get_card_image(self, card_name: str, **kwargs) -> Dict[str, Any]:
        """获取卡牌图片并发送。"""

        try:
            cards = await self._search_cards(card_name.strip())
            if not cards:
                return {"success": False, "message": f"未找到与 '{card_name}' 相关的卡牌"}

            card = cards[0]
            card_id = card.get("id")
            display_name = card.get("cn_name") or card.get("sc_name") or card_name
            if not card_id:
                return {"success": False, "message": "卡牌 ID 无效"}

            # ① 本机卡图优先（离线、也不用等 CDN）
            image_b64 = self._local_card_image(int(card_id))
            source = "本机卡图"
            if not image_b64:
                image_b64 = await self._fetch_card_image_online(int(card_id))
                source = "在线 CDN"
            if not image_b64:
                return {
                    "success": False,
                    "message": f"找到【{display_name}】但没有取到卡图（本机没有、在线也失败）",
                    "card_name": display_name,
                    "card_id": card_id,
                }

            stream_id = self._session_from(kwargs)
            if not stream_id:
                return {
                    "success": True,
                    "message": f"找到【{display_name}】的卡图，但无法发送（缺少会话信息）",
                    "card_name": display_name,
                    "card_id": card_id,
                }
            await self.ctx.send.image(image_b64, stream_id)
            logger.info("已发送卡牌图片: %s（%s）", display_name, source)
            return {
                "success": True,
                "message": f"已发送【{display_name}】的卡图",
                "card_name": display_name,
                "card_id": card_id,
                "source": source,
            }
        except Exception as error:  # noqa: BLE001
            logger.error("获取卡牌图片失败: %s", error)
            return {"success": False, "message": f"获取卡牌图片失败: {error}"}
