"""百科检索（原「游戏王百科检索」插件，已并入「麦麦玩游戏王」）。

两个 LLM 工具：`ygo_card_search`（查卡）／`ygo_card_image`（发卡图）。
参数与返回结构保持原样，只有工具名统一成 `ygo_` 前缀
（本插件所有工具都用这个前缀，避免与别的插件撞名）。

**原来的第三个工具 `ygo_deck_analyze`（自动识别 YDK / YDKE / 萌卡分享链接并分析卡组）
2026-10-07 按用户口径删掉了**：群里的卡组码现在只能用本插件的 `/加卡组` 指令导入
（走 `duel/deckcode.py` 那条链路，与本文件无关）。它专用的那几个解析函数
（`parse_deck` / `parse_ydk` / `parse_ydke` / `parse_ourygo` 等）与"本地卡库优先"那条路径一并删除。

并入之后的改进（都是"一体"带来的红利）：

1. **卡图本地优先**：优先用本机客户端的卡图（`MDPro3/Picture/Art` → `Closeup`），
   拉了不到才去 CDN 下载；
2. **外网请求一律走 `duel/netguard`**：只允许 https、只连公网地址、限制重定向与响应体积
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


# ⚠ 这里原来是"卡组码解析"一整段：`parse_ydk`（YDK 文本）／`_decode_ydke_part` + `parse_ydke`
# （ydke:// 链接）／`detect_deck_format`／`parse_ourygo`（萌卡分享链接）／`parse_deck`（自动识别）。
# 它们只被已删的 `ygo_deck_analyze` 工具使用（2026-10-07 用户口径），整段删掉。
# 群里的卡组码现在只用 `/加卡组` 指令导入，解析在 `duel/deckcode.py` 里。


class YugiohWikiTools:
    """百科检索的两个工具（混入到主插件类里；实例上要有 `ctx` / `config`）。

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

    # ⚠ 这里原来有一个 `_local_details`（用本机 cards.cdb 批量取卡、离线优先）：
    # 它只服务于已删的 `ygo_deck_analyze`（解析卡组码时把卡号翻成卡名），所以一起去掉了。
    # 本机卡图那条路径（`_local_card_image`）保留——发卡图还在用它。

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

    # ⚠ 这里原来有一个 `_fetch_card_by_id`（按卡号在线取一张卡）：它只被已删的
    # `ygo_deck_analyze` 用来补齐"本机卡库没有的卡"，随那个工具一起去掉了。

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

    # ⚠ 这里原来是第三个工具 `ygo_deck_analyze`（`analyze_deck`）：自动识别群里的卡组码
    # （YDK / YDKE / 萌卡分享链接）并解析成卡表，顺手把结果发到会话里。
    # **2026-10-07 用户口径：去掉卡组码自动识别**——群里贴的卡组码只能用 `/加卡组` 指令导入
    #（那条链路的解析在 `duel/deckcode.py`），所以整个工具与它的解析辅助函数一起删掉了。

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
