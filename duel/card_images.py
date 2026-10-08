"""整卡卡图的本地缓存与补齐（查房出图用）。

**为什么要它**（2026-10-08 用户口径："有的会没有卡图，都加上，不许没有卡图"）：
本机 MDPro3 的 `Picture/Art` 只覆盖 1.3 万多张、且**只有立绘**（624×624），
新卡（例如「闪刀姬=零露」）连立绘都没有，出图就退化成"无卡图"的卡名框。
而插件本来就有在线卡图源——`wiki.py` 的 `CARD_IMAGE_CDN`（萌卡那套 `<卡号>.jpg`，
**整张中文卡图**，`ygo_card_image` 用的就是它）。这里把它接进出图链路：

* **本地优先**：`<缓存目录>/<卡号>.jpg` 有就直接用，不联网；
* **缺了才下**：走 `duel/netguard.guarded_request`（只 https、只公网地址、限体积、拒代理隧道），
  下完**原子写**（先写 `.part` 再 `replace`），半截文件不会留在缓存里；
* **只下这一局场上出现的卡**（调用方给卡号列表），一次 `/查房` 通常 0~10 张、每张 0.1~0.2 秒；
* **不在事件循环里下载**：`warm()` 是 async，内部把下载丢进 `asyncio.to_thread`；
  同步的 `warm_many()` 留给命令行工具（`tools/field_image.py` 预览用）。

⚠ 三条边界（改这个文件时别越过）：
1. 只写**缓存目录**（默认 `<插件>/temp/card_pics/`，gitignore 之列），不接受任意路径参数；
2. 只允许 `https` + 公网地址，且 URL 由固定 CDN 与卡号拼出来——卡号必须是纯数字；
3. 单张上限 `MAX_BYTES`，超了当失败（不落盘），失败只记日志、返回 None（调用方回退立绘/卡名框）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .netguard import UnsafeUrlError, guarded_request

logger = logging.getLogger("mai-play-ygo.card_pics")

#: 在线卡图源：与 `wiki.py` 的 `CARD_IMAGE_CDN` 同一个（萌卡的 ygopro 卡图，整张中文卡面）
CARD_IMAGE_CDN = "https://cdn.233.momobako.com/ygopro/pics"

#: 单张卡图的上限（实测一张 60~120 KB；给到 1 MiB 足够，且防止对面塞超大响应）
MAX_BYTES = 1024 * 1024

#: 一次预热的时间预算与张数上限（超过就放弃剩下的，别让"补图"拖住出图）
WARM_BUDGET_SECONDS = 6.0
WARM_MAX_CARDS = 24

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
#: 默认缓存目录：插件目录下的 `temp/card_pics/`（`temp/` 已 gitignore，属于运行期产物）
DEFAULT_CACHE_DIR = _PLUGIN_ROOT / "temp" / "card_pics"

_DIGITS = re.compile(r"^\d{1,10}$")

#: 关掉联网补齐（环境变量 `MAIPLAYYGO_NO_PIC_FETCH=1`）：测试与离线部署用，
#: 关掉之后只读缓存（出图会退化成"名字 + 星级 + 攻守"的兜底卡面）。
_disabled = os.environ.get("MAIPLAYYGO_NO_PIC_FETCH") == "1"


def cache_dir_for(root: Optional[Path] = None) -> Path:
    """缓存目录（不存在就建出来）；`root` 只在测试里传，正常用默认那份。"""

    target = Path(root) if root is not None else DEFAULT_CACHE_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def cached_pic(card_id: int, cache: Path) -> Optional[Path]:
    """缓存里这张卡的整卡图（没有就 None）。**只读**，不联网。"""

    if card_id <= 0:
        return None
    for suffix in (".jpg", ".png"):
        path = Path(cache) / f"{card_id}{suffix}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def fetch_pic(card_id: int, cache: Path) -> Optional[Path]:
    """下载一张整卡图进缓存（已有就跳过）。失败返回 None——调用方回退立绘或卡名框。"""

    existing = cached_pic(card_id, cache)
    if existing is not None:
        return existing
    if _disabled:
        return None
    key = str(int(card_id))
    if not _DIGITS.match(key):
        logger.warning("卡号不合法，跳过补图：%r", card_id)
        return None
    url = f"{CARD_IMAGE_CDN}/{key}.jpg"
    try:
        data = guarded_request(url, timeout=15, max_bytes=MAX_BYTES)
    except UnsafeUrlError as exc:
        logger.warning("卡图地址被出网护栏拒绝（%s）：%s", key, exc)
        return None
    except Exception as exc:  # noqa: BLE001  网络失败只影响这一张图，不该让出图失败
        logger.info("补图失败（%s）：%s: %s", key, type(exc).__name__, exc)
        return None
    if not data or data[:3] != b"\xff\xd8\xff":       # 只认 JPEG：不然可能把错误页当图存下来
        logger.info("补图失败（%s）：返回的不是 JPEG（%d 字节）", key, len(data or b""))
        return None
    target = Path(cache) / f"{key}.jpg"
    part = target.with_suffix(".jpg.part")
    try:
        part.write_bytes(data)
        os.replace(part, target)                      # 原子替换：不会留下半截文件
    except OSError as exc:
        logger.warning("补图写盘失败（%s）：%s", target, exc)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return target


def missing_ids(card_ids: Iterable[int], cache: Path) -> List[int]:
    """这些卡里缓存还没有的（去重、保序；顺带把非法卡号滤掉）。"""

    out: List[int] = []
    seen = set()
    for card_id in card_ids:
        try:
            key = int(card_id)
        except (TypeError, ValueError):
            continue
        if key <= 0 or key in seen:
            continue
        seen.add(key)
        if cached_pic(key, cache) is None:
            out.append(key)
    return out


def warm_many(card_ids: Sequence[int], cache: Path, *, budget_seconds: float = WARM_BUDGET_SECONDS) -> List[int]:
    """**同步**补齐（命令行预览用）：返回真正下到的卡号。超出预算就不再下剩下的。"""

    started = time.time()
    fetched: List[int] = []
    for card_id in missing_ids(card_ids, cache)[:WARM_MAX_CARDS]:
        if time.time() - started > budget_seconds:
            logger.info("补图超出时间预算（%.0f 秒），剩下的下次再说", budget_seconds)
            break
        if fetch_pic(card_id, cache) is not None:
            fetched.append(card_id)
    return fetched


async def warm(card_ids: Sequence[int], cache: Path, *, budget_seconds: float = WARM_BUDGET_SECONDS) -> List[int]:
    """**异步**补齐（插件里用）：下载在工作线程里跑，不阻塞事件循环。"""

    return await asyncio.to_thread(warm_many, list(card_ids), cache, budget_seconds=budget_seconds)


# ── 后台补齐：渲染器是同步函数（跑在事件循环里），不能在它里面下载； ──────────────
# 所以"缺图"时只把卡号丢进这个单线程池，下次出图就有整卡图了。这样出图链路
# 完全不用改动（`field_image.card_full_uri` 只在缓存里找，找不到就回退立绘/卡名框）。
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_pending: set = set()


def request_async(card_id: int, cache: Path) -> None:
    """把"这张卡缺图"排到后台去下（同一个卡号只排一次）。返回后不保证已下好。"""

    global _executor
    try:
        key = int(card_id)
    except (TypeError, ValueError):
        return
    if _disabled or key <= 0 or cached_pic(key, cache) is not None:
        return
    with _executor_lock:
        if key in _pending:
            return
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="card-pics")
        _pending.add(key)
        future = _executor.submit(fetch_pic, key, cache)
    future.add_done_callback(lambda _f, _key=key: _pending.discard(_key))
