"""插件自带的 WebUI：一块只读面板 + 后续「训练功能」的入口。

为什么自带一个 HTTP 面板而不是往群里加命令：

* 训练类操作（推演 combo、复盘录像、生成脚本、跑擂台）都是"长任务 + 大量结构化结果"，
  塞进群聊既刷屏又难看；面板能一次看全，还能翻历史。
* 群里的命令面保持现状不动——按用户口径，新功能只在面板里出现。

设计约束（与插件其余部分一致）：

* **零第三方依赖**：只用标准库 ``http.server`` + ``threading``；插件本身只依赖 ``maibot_sdk``。
* **同步生命周期**：:meth:`WebUIServer.start` 起一个守护线程，:meth:`WebUIServer.stop_now`
  同步关闭。宿主给插件卸载的预算只有 5 秒，卸载路径里不能 await（对齐 ``duel/gate.py``
  的 ``kill_now`` 约定）。
* **只监听回环**：默认 ``127.0.0.1``。面板能看到卡组池、配置与日志，不对外网开放；
  要手机看就自己改成 ``0.0.0.0`` 并想清楚密钥强度。
* **密钥来源有序**：配置 ``webui.api_key`` → 环境变量 ``YGO_WEBUI_KEY`` → 自动生成。
  源码与配置模板里**不留任何凭据字面量**；自动生成的密钥明文写进
  ``<data_dir>/webui_key.txt``，面板登录页会告诉用户去哪里找。
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time

from .duel.card_images import cache_dir_for
from .duel.deckpool import list_numbering_key
from .duel.field_image import _archetype_guess
from .duel.fieldstate import MONSTER_ZONE, SPELL_ZONES
from .train.analysis import tail_lines

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 登录成功后下发的 cookie 名。
COOKIE_NAME = "ygo_webui_key"

#: 自动生成密钥时写入的文件名（位于插件数据目录）。
KEY_FILE_NAME = "webui_key.txt"

#: 自动生成密钥的字节数（token_urlsafe 后约 32 字符）。
_KEY_BYTES = 24

#: 日志接口默认/最大行数。
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 2000

#: 卡组池列名（与 ``duel/deckpool.py`` 的 ``_DECK_COLUMNS`` 对齐；老库可能缺列，
#: 所以这里按"存在即取"的方式读，缺列给空值）。
#: ⚠ 这份列表漏一列不会报错、只会让那一列在前端永远是空的（`brain_scope` 就这么丢过一次：
#: 卡组页的「AI 决策」永远显示"跟随全局"，库里明明存了值）。加列时两边一起加。
_DECK_COLUMNS = (
    "deck_id",
    "group_id",
    "display_name",
    "contributor_id",
    "contributor_name",
    "ydk_path",
    "deck_code",
    "source_format",
    "main_count",
    "extra_count",
    "side_count",
    "windbot_deck",
    "generated_script",
    "picked_style",
    "in_random",
    "created_at",
    "brain_scope",
)

#: 推演笔记在详情页里给多少行（整份推演可能很长，详情页只要够读个大概）。
_GUIDE_PREVIEW_LINES = 200

#: 日志里的 ANSI 颜色码（训练日志来自命令行工具，带颜色码时面板里会花屏）。
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: 卡图接口能给的图片后缀（只认这两种；卡号必须是纯数字，见 `_send_art`）。
_ART_SUFFIXES = (".jpg", ".png")

#: 卡图在浏览器里的缓存时长（秒）。卡图不会变，给一天；换图靠卡号本身的缓存失效。
_ART_CACHE_SECONDS = 86400

#: 「头牌」缓存：``(ydk 路径, mtime) → 卡号``。列卡组时要给每副牌配一张缩略图，
#: 96 副牌每次都重读 .ydk 太浪费（虽然小，但没必要）——按 mtime 失效就够。
_HEAD_CARD_CACHE: Dict[Tuple[str, float], int] = {}
#: 卡的「头牌」缓存上限。
_HEAD_CARD_CACHE_MAX = 512

#: 是否把**每一条**面板访问日志都写进插件日志（默认只写非 200 的）。
#: 对局监控页每 2 秒拉一次接口，全记下来会把日志灌满；排查时设 `webui_verbose=1`。
_VERBOSE_ACCESS_LOG = os.environ.get("webui_verbose") == "1"

#: 挑「有图的头牌」时最多往下试多少张。给 30 是因为额外卡组通常 15 张、
#: 主卡组里常见的卡也就那么几种；再多就是浪费 disk stat。
_HEAD_ART_TRIES = 30


def _json_body(raw: str) -> Dict[str, Any]:
    """把 POST 的 body 读成字典（JSON 优先，退回表单编码）。"""

    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    # 表单提交（`a=1&b=2`）也认：面板自己是发 JSON 的，这么写是为了能用 curl 手测
    return {key: value[0] for key, value in parse_qs(text).items() if value}


def _strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码（训练日志来自命令行工具，带颜色码时面板里会花屏）。"""

    return _ANSI_PATTERN.sub("", text)


def _safe_counts(panel: "WebUIServer") -> Dict[str, int]:
    """训练记录各状态条数；拿不到就返回空（面板不该因为库读不动整页报错）。"""

    store = panel.plugin.training_store()
    if store is None:
        return {}
    try:
        return store.counts()
    except Exception:  # noqa: BLE001
        return {}


def resolve_api_key(configured: str, data_dir: Path) -> Tuple[str, str]:
    """按"配置 → 环境变量 → 自动生成"的顺序解析面板密钥。

    Args:
        configured: 配置里的 ``webui.api_key``（可空）。
        data_dir: 插件数据目录，自动生成的密钥写在这里。

    Returns:
        tuple[str, str]: ``(密钥, 来源说明)``；来源说明用于日志，不包含密钥本身。
    """

    text = str(configured or "").strip()
    if text:
        return text, "配置 webui.api_key"
    env_key = str(os.environ.get("YGO_WEBUI_KEY") or "").strip()
    if env_key:
        return env_key, "环境变量 YGO_WEBUI_KEY"
    key_file = Path(data_dir) / KEY_FILE_NAME
    try:
        if key_file.exists():
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing, f"自动生成（{key_file.name}，首次启动时写入）"
    except OSError:
        pass
    generated = secrets.token_urlsafe(_KEY_BYTES)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(generated, encoding="utf-8")
    except OSError:
        return generated, "自动生成（本次运行有效，写入密钥文件失败）"
    return generated, f"自动生成（已写入 {key_file}）"


# ---------------------------------------------------------------------------
# 数据读取辅助（全部只读，直接查 SQLite / 文件，不碰插件的运行时状态）
# ---------------------------------------------------------------------------


def _number_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每行卡组补上 `number` ＝ **列表编号**（群里 `/卡组列表` 显示的那个号）。

    ⚠ 面板只读卡组池的数据库（跨线程用插件的 sqlite 连接是未定义行为），所以这里自己排一遍，
    排序键必须与 `duel/deckpool.py::list_numbering_key` **完全一致**（内置在前、其余按加入顺序）；
    否则面板上的 `#N` 会和群里看到的编号对不上——这正是 2026-10-09 用户报的
    "卡组编号到底是多少，一切都统一到卡组列表里面的编号，包括 webui 里面的"。
    数据库里的 `deck_id` 只是内部主键，只留在接口参数里（值本身不显示给用户）。
    """

    ordered = sorted(
        rows,
        key=lambda row: list_numbering_key(str(row.get("group_id") or ""), int(row.get("deck_id") or 0)),
    )
    return [dict(row, number=index) for index, row in enumerate(ordered, start=1)]


def _read_deck_rows(db_path: Path) -> List[Dict[str, Any]]:
    """读卡组池全部卡组（跨群）；**编号要另外算**（见 :func:`_number_rows`）。"""

    if not db_path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute("SELECT * FROM decks")
        for row in cursor.fetchall():
            record: Dict[str, Any] = {}
            keys = set(row.keys())
            for column in _DECK_COLUMNS:
                record[column] = row[column] if column in keys else ""
            rows.append(record)
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    rows.sort(key=lambda item: (str(item.get("group_id") or ""), -float(item.get("created_at") or 0)))
    return rows


def _read_ydk_summary(path: Path) -> Dict[str, Any]:
    """读一份 .ydk，返回分区计数与卡号列表（不查卡名，卡名交给卡牌库可选补充）。

    分区按**标记行**切：``#main`` / ``#extra`` / ``!side`` 三种写法都见过
    （不同工具的首字符是 ``#`` 还是 ``!`` 不一定），所以去掉前导的 ``#`` / ``!`` 之后按名字认。

    ⚠ 这里踩过一次：``!side`` 的判断写在"以 ``#`` 开头即注释"那一支**里面**——它不以 ``#`` 开头，
    于是永远认不出来，副卡组的卡全被算进额外卡组。面板上就是「额外卡组 30 张 / 副卡组 0 张」，
    两个区混在一起（用户报的"额外卡组和副卡组应该分开"）。
    """

    summary: Dict[str, Any] = {"main": [], "extra": [], "side": [], "error": ""}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        summary["error"] = f"读取失败：{exc}"
        return summary
    zone = "main"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line[0] in "#!":
            marker = line.lstrip("#!").strip().lower()
            if marker in ("main", "extra", "side"):
                zone = marker
            continue
        if line.isdigit():
            summary[zone].append(line)
    return summary


def _group_cards(card_ids: List[str], card_db: Any) -> List[Dict[str, Any]]:
    """把一列卡号按「同一张卡」聚合起来：``[{"id", "name", "count"}]``，张数多的在前。

    为什么要聚合：.ydk 里一张卡出现 3 次就写 3 行，直接铺成 40 行列表既长又难扫；
    面板是给人看的，应该一眼看出"这副牌的主力是哪几张"。
    """

    counts: Dict[int, int] = {}
    for raw in card_ids:
        text = str(raw)
        if not text.isdigit():
            continue
        card_id = int(text)
        counts[card_id] = counts.get(card_id, 0) + 1
    names = _card_names(card_db, list(counts))
    entries = [
        {"id": card_id, "name": names.get(card_id, str(card_id)), "count": count}
        for card_id, count in counts.items()
    ]
    # 张数多的在前；张数一样时按卡号升序（顺序稳定、可预期，别让列表每次刷新都在跳）
    entries.sort(key=lambda item: (-int(item["count"]), int(item["id"])))
    return entries


def _head_card_of(ydk_path: Path, has_art: Optional[Callable[[int], bool]] = None) -> int:
    """这副牌的「头牌」卡号（拿它当缩略图）：优先额外卡组的第一张，否则主卡组里张数最多的。

    为什么这么挑：额外卡组才是这副牌的牌面（终端大哥），主卡组第一张往往只是手坑。
    给了 `has_art` 时**优先挑本地真的有图的那张**（顺着候选往下试有限次）——不这么做，
    一半卡组的缩略图都会是占位卡背，整页看起来就像图挂了。
    读不到就返回 0（前端画占位图标，不留空白）。
    """

    try:
        stat = ydk_path.stat()
    except OSError:
        return 0
    cache_key = (str(ydk_path), stat.st_mtime)
    cached = _HEAD_CARD_CACHE.get(cache_key)
    if cached is not None:
        return cached

    summary = _read_ydk_summary(ydk_path)
    candidates: List[int] = []
    for item in summary.get("extra", []):
        if str(item).isdigit():
            candidates.append(int(item))
    counts: Dict[int, int] = {}
    for item in summary.get("main", []):
        if str(item).isdigit():
            card_id = int(item)
            counts[card_id] = counts.get(card_id, 0) + 1
    candidates.extend(card_id for card_id, _ in sorted(counts.items(), key=lambda pair: -pair[1]))

    head = candidates[0] if candidates else 0
    if has_art is not None:
        for card_id in candidates[:_HEAD_ART_TRIES]:
            if has_art(card_id):
                head = card_id
                break
    if len(_HEAD_CARD_CACHE) >= _HEAD_CARD_CACHE_MAX:
        _HEAD_CARD_CACHE.clear()
    _HEAD_CARD_CACHE[cache_key] = head
    return head


def _find_deck_row(db_path: Path, deck_id: int) -> Optional[Dict[str, Any]]:
    """按编号找一条卡组记录（只读打开卡组池，找不到返回 None）。"""

    for row in _read_deck_rows(db_path):
        if str(row.get("deck_id") or "") == str(int(deck_id)):
            return row
    return None


def _clock(stamp: float) -> str:
    """时间戳 → `YYYY-mm-dd HH:MM:SS`（给面板显示用）。"""

    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else ""


def _combo_label(path: Path) -> str:
    """推演存档的显示名：把 `20261008-193012-95-杀调.txt` 显示成 `10-08 19:30　杀调`。"""

    match = re.match(r"^(\d{8})-(\d{6})-(.+)$", path.stem)
    if not match:
        return path.stem
    date, clock, rest = match.groups()
    rest = re.sub(r"^-?\d+-", "", rest)          # 去掉中间那个卡组编号
    stamp = f"{date[4:6]}-{date[6:8]} {clock[0:2]}:{clock[2:4]}"
    return f"{stamp}　{rest}" if rest else stamp


def _read_head(path: Path, lines: int) -> str:
    """读文件前若干行（读不到就空串）。"""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[: max(1, int(lines))])


def _tail_log_lines(host_root: Path, lines: int, keyword: str) -> List[str]:
    """取宿主最新日志的尾部若干行，可按关键词过滤（空关键词 = 不过滤）。"""

    log_dir = host_root / "logs"
    if not log_dir.is_dir():
        return []
    candidates = sorted(log_dir.glob("app_*.log.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return []
    text = candidates[0].read_text(encoding="utf-8", errors="replace")
    picked = text.splitlines()[-max(1, min(lines, _LOG_TAIL_MAX)):]
    needle = keyword.strip().lower()
    if needle:
        picked = [line for line in picked if needle in line.lower()]
    return picked


def _pretty_log_line(raw: str) -> str:
    """把一行 jsonl 日志压成"时间 级别 事件"的可读形式。"""

    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    stamps = str(record.get("timestamp") or record.get("@timestamp") or "")
    level = str(record.get("level") or "").upper()
    event = str(record.get("event") or "")
    logger_name = str(record.get("logger_name") or record.get("logger") or "")
    match = re.search(r"(\d{2}:\d{2}:\d{2})", stamps)
    clock = match.group(1) if match else stamps
    prefix = f"{clock} [{level}]"
    if logger_name:
        prefix += f" {logger_name}"
    return f"{prefix} {event}".strip()


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class _PanelHandler(BaseHTTPRequestHandler):
    """面板的请求处理器：密钥鉴权 + JSON 接口 + 单页界面。"""

    server_version = "MaiPlayYgoWebUI/1.0"

    # 默认只记"值得看"的请求（非 200）。对局监控页每 2 秒拉一次 /api/rooms，
    # 全部记下来会把宿主日志灌满、也把那点有用的信息淹掉；要排查时把
    # `webui_verbose` 环境变量设成 1 就能看到每一条。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        if panel.logger is None:
            return
        status = args[1] if len(args) > 1 else ""
        if _VERBOSE_ACCESS_LOG or str(status) != "200":
            panel.logger.debug("WebUI: " + fmt, *args)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # noqa: D102
        # 交给 `log_message` 统一判断：标准库这一层会把成功请求也写出来
        self.log_message('"%s" %s %s', self.requestline, str(code), str(size))

    # ---- 鉴权 ----

    def _provided_key(self) -> str:
        """从 cookie / 请求头 / query 里取密钥。"""

        cookie_header = self.headers.get("Cookie") or ""
        if cookie_header:
            jar = SimpleCookie()
            try:
                jar.load(cookie_header)
            except Exception:  # noqa: BLE001  cookie 解析失败按"没带"处理
                jar = SimpleCookie()
            morsel = jar.get(COOKIE_NAME)
            if morsel is not None and morsel.value:
                return morsel.value
        header_key = str(self.headers.get("X-API-Key") or "").strip()
        if header_key:
            return header_key
        query = parse_qs(urlparse(self.path).query)
        return str((query.get("key") or [""])[0]).strip()

    def _authorized(self) -> bool:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        provided = self._provided_key()
        if not provided:
            return False
        return secrets.compare_digest(provided, panel.api_key)

    def _set_cookie(self, value: str) -> None:
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={7 * 24 * 3600}",
        )

    # ---- 输出辅助 ----

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, *, max_age: int = 0) -> None:
        """发一段二进制（卡图走这里）。"""

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"private, max-age={int(max_age)}" if max_age else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_text: str, status: int = 200) -> None:
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802  标准库命名
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/login":
            self._send_html(_login_page(error=""))
            return
        if path == "/api/logout":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0")
            self.end_headers()
            return
        if not self._authorized():
            # 密钥不对时明说"不正确"；压根没带就只给登录页（不透露磁盘路径、端口这些细节）
            if path.startswith("/api/"):
                self._send_json({"ok": False, "error": "未授权：请先在面板首页输入密钥"}, status=401)
                return
            self._send_html(
                _login_page(error="密钥不正确" if self._provided_key() else ""), status=401
            )
            return
        # 用 `?key=...` 进首页的人：先换成 cookie 再跳到干净的地址。
        # 为什么非跳不可：密钥留在地址栏里，就会进浏览历史、进 Referer 头、被截图带出去。
        if path == "/" and (parse_qs(parsed.query).get("key") or [""])[0]:
            self.send_response(302)
            self.send_header("Location", "/")
            self._set_cookie(self.server.panel.api_key)  # type: ignore[attr-defined]
            self.end_headers()
            return

        try:
            if path == "/":
                self._send_html(_app_page())
                return
            if path == "/api/status":
                self._send_json(self._api_status())
                return
            if path == "/api/decks":
                self._send_json(self._api_decks())
                return
            if path.startswith("/api/art/"):
                self._send_art(path[len("/api/art/"):])
                return
            # ⚠ 顺序要紧：`/workspace` 比"卡组详情"更具体，必须排在它前面——否则会被
            # 详情那条路由吃掉（`deck_id` 变成 "97/workspace"，报"找不到卡组"）。
            if path.startswith("/api/deck/") and path.endswith("/workspace"):
                deck_id = path[len("/api/deck/"):-len("/workspace")].rstrip("/")
                self._send_json(self._api_deck_workspace(deck_id))
                return
            if path.startswith("/api/deck/"):
                deck_id = path[len("/api/deck/"):]
                self._send_json(self._api_deck_detail(deck_id, parse_qs(parsed.query)))
                return
            if path == "/api/rooms":
                self._send_json(self._api_rooms())
                return
            if path == "/api/training":
                self._send_json(self._api_training())
                return
            if path.startswith("/api/training/run/"):
                run_id = path[len("/api/training/run/"):]
                self._send_json(self._api_training_run(run_id))
                return
            if path == "/api/logs":
                query = parse_qs(parsed.query)
                lines = int((query.get("lines") or [_LOG_TAIL_DEFAULT])[0] or _LOG_TAIL_DEFAULT)
                keyword = str((query.get("filter") or [""])[0])
                self._send_json(self._api_logs(lines, keyword))
                return
        except Exception as exc:  # noqa: BLE001  面板出错只回 500，绝不把异常抛回插件
            self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, status=500)
            return

        self._send_json({"ok": False, "error": f"未知接口：{path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802  标准库命名
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

        if path == "/login":
            self._post_login(raw)
            return
        if not self._authorized():
            self._send_json({"ok": False, "error": "未授权：请先在面板首页输入密钥"}, status=401)
            return
        try:
            if path == "/api/training/start":
                self._send_json(self._post_training_start(_json_body(raw)))
                return
            if path == "/api/training/stop":
                self._send_json(self._post_training_stop(_json_body(raw)))
                return
            if path.startswith("/api/deck/"):
                self._send_json(self._post_deck(path, _json_body(raw)))
                return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, status=500)
            return
        self._send_json({"ok": False, "error": f"未知接口：{path}"}, status=404)

    def _post_login(self, raw: str) -> None:
        """登录：表单提交，对了就下发 cookie。"""

        form = parse_qs(raw)
        candidate = str((form.get("key") or [""])[0]).strip()
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        if candidate and secrets.compare_digest(candidate, panel.api_key):
            self.send_response(302)
            self.send_header("Location", "/")
            self._set_cookie(panel.api_key)
            self.end_headers()
            return
        self._send_html(_login_page(error="密钥不正确"), status=401)

    def _post_training_start(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """起一个训练任务（参数按种类取，多余的一律忽略）。"""

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        kind = str(body.get("kind") or "").strip()
        params: Dict[str, Any] = {key: value for key, value in body.items() if key != "kind"}
        try:
            run = panel.plugin.schedule_training_start(kind, params)
        except Exception as exc:  # noqa: BLE001  起不来的原因要原样告诉用户
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "run": run.to_dict()}

    def _post_training_stop(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """停掉正在跑的训练任务。"""

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        del body
        try:
            stopped = panel.plugin.schedule_training_stop()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "stopped": bool(stopped)}

    def _post_deck(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """卡组操作：加入/移出随机池、删除。

        走插件的公开入口（`plugin.schedule_deck_action`），因为卡组池的 sqlite 连接
        是在插件的事件循环里建的——**面板线程不能直接碰它**（跨线程用同一个连接是未定义行为）。
        """

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        tail = path[len("/api/deck/"):]
        if "/" not in tail:
            return {"ok": False, "error": f"未知接口：{path}"}
        deck_id, action = tail.split("/", 1)
        if not deck_id.isdigit():
            return {"ok": False, "error": "卡组编号必须是数字"}
        action = action.strip("/")
        group_id = str(body.get("group_id") or "")
        if action == "settings":
            scope = body.get("brain_scope")
            try:
                message = panel.plugin.schedule_deck_settings(
                    int(deck_id),
                    in_random=body.get("in_random") if "in_random" in body else None,
                    brain_scope=None if scope is None else str(scope),
                )
            except Exception as exc:  # noqa: BLE001  失败原因原样给用户看
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "message": message}
        try:
            if action == "random":
                message = panel.plugin.schedule_deck_action(
                    "random", int(deck_id), in_random=bool(body.get("in_random")), group_id=group_id
                )
            elif action == "delete":
                if not body.get("confirm"):
                    return {"ok": False, "error": "删除卡组要带 confirm: true（这个动作不可撤销）"}
                message = panel.plugin.schedule_deck_action("delete", int(deck_id), group_id=group_id)
            else:
                return {"ok": False, "error": f"未知的卡组操作：{action}"}
        except Exception as exc:  # noqa: BLE001  失败原因（内置卡组不能删等）要原样给用户看
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "message": str(message)}

    # ---- 各接口实现 ----

    def _api_status(self) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        plugin = panel.plugin
        decks = _read_deck_rows(panel.deck_db_path)
        rooms = []
        try:
            for stream_id, room in dict(getattr(plugin, "_rooms", {})).items():
                rooms.append(
                    {
                        "stream_id": stream_id,
                        "group_id": getattr(room, "group_id", ""),
                        "deck_name": getattr(room, "deck_name", ""),
                    }
                )
        except Exception:  # noqa: BLE001  面板只读展示，拿不到就算了
            rooms = []
        key_file = Path(panel.data_dir) / KEY_FILE_NAME
        return {
            "ok": True,
            "plugin": {
                "id": "mai-play-ygo",
                "data_dir": str(panel.data_dir),
                "log_dir": str(panel.host_root / "logs"),
            },
            "webui": {
                "host": panel.host,
                # 报**实际在听的端口**：配置里写 0（随机分配）或写错端口时，
                # 用户要看的是"现在到底在哪个端口上"，不是配置里那个数
                "port": panel.bound_port or panel.port,
                "url": panel.url,
                "started_at": panel.started_at,
                "key_source": panel.key_source,
                "key_file": str(key_file) if key_file.exists() else "",
            },
            "decks": {
                "total": len(decks),
                "groups": len({str(row.get("group_id") or "") for row in decks}),
                "in_random": sum(1 for row in decks if row.get("in_random")),
                "with_script": sum(1 for row in decks if str(row.get("generated_script") or "").strip()),
                "with_style": sum(1 for row in decks if str(row.get("picked_style") or "").strip()),
            },
            "rooms": rooms,
            "training": {
                "enabled": bool(plugin.config.training.enabled),
                "workspace": str(panel.training_workspace()),
                "model": str(plugin.config.llm.training_model or "").strip() or "（宿主给插件配的那只）",
                "max_duels_per_run": int(plugin.config.training.max_duels_per_run),
                "counts": _safe_counts(panel),
            },
            "platform": {
                "frozen": True,
                "cards_cdb": str(getattr(plugin, "_cards_cdb_path", "") or ""),
            },
        }

    # ---- 训练功能 ----

    def _api_training(self) -> Dict[str, Any]:
        """训练页的全部数据：能跑什么、在跑什么、最近跑过什么。"""

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        plugin = panel.plugin
        runner = plugin.training_runner()
        store = plugin.training_store()
        runs: List[Dict[str, Any]] = []
        if store is not None:
            try:
                runs = [item.to_dict() for item in store.list_recent(limit=40)]
            except Exception:  # noqa: BLE001  库读不动就只给"跑了什么"，不整页报错
                runs = []
        kinds = runner.describe_kinds() if runner is not None else []
        active = runner.active() if runner is not None else None
        return {
            "ok": True,
            "enabled": bool(plugin.config.training.enabled),
            "workspace": str(panel.training_workspace()),
            "model": str(plugin.config.llm.training_model or "").strip() or "（宿主给插件配的那只）",
            "max_duels_per_run": int(plugin.config.training.max_duels_per_run),
            "bridge_ready": runner is not None,
            "kinds": kinds,
            "active": active,
            "runs": runs,
            # 卡组列表跟"卡组"页共用一份数据：训练表单要让人挑一副牌
            "decks": self._deck_choices(),
        }

    def _deck_choices(self) -> List[Dict[str, Any]]:
        """给训练表单用的卡组选项（编号、群、名字、现有脚本名）。

        `script` 是**真正上场的那份**（生成脚本优先，其次实测挑的，最后是自带风格名）：
        前端两个下拉都读这个键——早先只给了 `generated_script` / `picked_style`，
        于是前端读不到、每副牌都显示成"还没脚本"（连有 Gen106 的赖皮也一样）。
        """

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        choices: List[Dict[str, Any]] = []
        for row in _read_deck_rows(panel.deck_db_path):
            generated = str(row.get("generated_script") or "")
            picked = str(row.get("picked_style") or "")
            choices.append(
                {
                    "deck_id": row.get("deck_id") or "",
                    "group_id": row.get("group_id") or "",
                    "name": row.get("display_name") or "",
                    "generated_script": generated,
                    "picked_style": picked,
                    "windbot_deck": str(row.get("windbot_deck") or ""),
                    "script": generated or picked or str(row.get("windbot_deck") or ""),
                    "is_builtin": str(row.get("group_id") or "") == "__builtin__",
                }
            )
        return choices

    def _api_training_run(self, run_id: str) -> Dict[str, Any]:
        """单条训练记录的详情（含输出尾巴——任务还在跑时就是实时进度）。"""

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        store = panel.plugin.training_store()
        if store is None:
            return {"ok": False, "error": "训练功能没启用（training.enabled = false）"}
        run = store.get(run_id)
        if run is None:
            return {"ok": False, "error": f"没有编号 {run_id} 的训练记录"}
        payload = run.to_dict()
        # 训练日志是**单个文件**（`train/logs/<时刻>-<种类>.log`），读它的尾巴就行——
        # 不能拿主机日志那套"目录 + app_*.jsonl"的读法去套（第一版写错了，
        # 表现是详情页永远显示"（没有输出）"，而任务其实跑得好好的）
        payload["tail"] = [_strip_ansi(line) for line in tail_lines(run.log_path, _LOG_TAIL_DEFAULT).splitlines()]
        # combo 推演没有子进程输出，成果是那份推演笔记——把它读进来，
        # 否则这一类任务的成功记录点开是一页空白（"没有输出"），等于没留下东西
        guide_path = str(payload.get("summary", {}).get("guide_path") or "")
        if guide_path:
            payload["summary"]["guide_text"] = tail_lines(guide_path, _GUIDE_PREVIEW_LINES)
        return {"ok": True, "run": payload}

    def _api_rooms(self) -> Dict[str, Any]:
        """实时对局监控：每个进行中的房间一份**完整**局面快照。

        给的是牌桌上的全部位置（与内核的位号一一对应，和 `/查房` 出图同一套）：

        * 怪兽区 7 格（0~4 主怪兽区、5~6 **额外怪兽区**）
        * 魔陷区 5 格 + **场地魔法区**（SPELL_ZONE 的第 5 号位）+ **灵摆区 2 格**
        * 墓地 / 除外 / 额外卡组 三堆的**当前张数**
        * 每张表侧卡的卡名、攻守、表示形式；里侧的只报"有卡"（不公开卡号）
        * 双方 LP、回合数、阶段、现在轮到谁，以及记录器那份"台账"（召唤/特召/发动/盖放/攻击/伤害…）

        ⚠ 里侧卡在记录器里是 ``ZoneCard`` 对象（带 ``position``），**不是卡号**——
        第一版按卡号写的 `int(card)` 就是那个 `TypeError` 的来源；这里统一走
        ``ZoneCard.card_id`` 与 ``.face_up``。

        面板线程读这些字段是**只读**的、且都在同一进程里，某一帧读到不一致可以接受
        （每 2 秒刷新一次，下一帧就对上了）——要做成严格一致就得让出牌循环给面板让路，
        那会拖慢对局。
        """

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        plugin = panel.plugin
        card_db = getattr(plugin, "_card_db", None)
        rooms: List[Dict[str, Any]] = []
        for stream_id, room in list(dict(getattr(plugin, "_rooms", {})).items()):
            entry: Dict[str, Any] = {
                "stream_id": stream_id,
                "group_id": str(getattr(room, "group_id", "") or ""),
                "deck_name": str(getattr(room, "deck_name", "") or ""),
                "started": False,
                "finished": False,
                "turn": 0,
                "phase": "",
                "current_seat": None,
                "sides": [],
                "error": "",
            }
            try:
                session = room.session
                entry["started"] = bool(getattr(session, "started", False))
                entry["finished"] = bool(getattr(session, "finished", False))
                recorder = session.recorder()
                entry["turn"] = int(getattr(recorder, "turn_count", 0) or 0)
                entry["phase"] = str(getattr(recorder, "phase", "") or "")
                # 现在轮到谁：内核每回合发 MSG_NEW_TURN，记录器把当前回合方存在 players 里
                state = getattr(recorder, "field_state", None)
                entry["current_seat"] = _current_seat(recorder, state)
                names: Dict[int, str] = {}
                stats_of: Dict[int, Any] = {}
                lp_hint: Dict[int, int] = {}
                for seat, stats in dict(getattr(recorder, "players", {}) or {}).items():
                    names[int(seat)] = str(getattr(stats, "name", "") or "")
                    stats_of[int(seat)] = stats
                    final = getattr(stats, "lp_final", None)
                    if final:
                        lp_hint[int(seat)] = int(final)
                entry["sides"] = _board_sides(
                    state,
                    names=names,
                    stats_of=stats_of,
                    self_seat=int(getattr(recorder, "self_seat", 0) or 0),
                    lp_hint=lp_hint,
                    start_lp=int(getattr(plugin.config.duel, "start_lp", 8000) or 8000),
                    our_deck=str(getattr(room, "deck_name", "") or ""),
                    card_db=card_db,
                    current_seat=entry["current_seat"],
                )
            except Exception as exc:  # noqa: BLE001  单个房间读失败不该让整页没数据
                entry["error"] = f"{type(exc).__name__}: {exc}"
            rooms.append(entry)
        return {
            "ok": True,
            "rooms": rooms,
            "note": "只列本插件开的房间；里侧的卡不公开卡号（与出图同一口径）",
            "unknown": [
                "卡组剩余张数、手牌张数：内核不下发这两个数（它只在「抽牌」时报动作），所以这里给不了",
            ],
        }

    def _api_deck_workspace(self, raw_deck_id: str) -> Dict[str, Any]:
        """这副牌已有的家底：当前出牌脚本、combo 推演存档、最近一次训练结论。

        为什么要这个：训练台是"针对某副牌干活"的地方，动手之前该先看见它已经有什么——
        DSH 那个游戏王插件就是这路子（把复盘结论按"卡组指纹"绑成可复用的 skill，能 list/get/activate）。
        这里做到够用为止：**脚本与推演按卡组编号绑**（脚本名记在卡组池的 `generated_script` 上，
        推演存档按 `*-<编号>-*.txt` 落在 `train/combos/`），再顺手把最近一次针对这副牌的
        训练结论捞出来。
        """

        if not raw_deck_id.isdigit():
            return {"ok": False, "error": "卡组编号必须是数字"}
        deck_id = int(raw_deck_id)
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        row = _find_deck_row(panel.deck_db_path, deck_id)
        if row is None:
            return {"ok": False, "error": f"卡组池里没有编号 {deck_id} 的卡组"}

        combo_dir = panel.training_workspace() / "combos"
        archives: List[Dict[str, Any]] = []
        if combo_dir.is_dir():
            for path in sorted(combo_dir.glob(f"*-{deck_id}-*.txt"), key=lambda item: item.stat().st_mtime):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                archives.append(
                    {"name": path.name, "label": _combo_label(path), "mtime": _clock(stat.st_mtime)}
                )
        archives.reverse()

        latest: Dict[str, Any] = {}
        store = panel.plugin.training_store()
        if store is not None:
            try:
                for run in store.list_recent(limit=40):
                    if int(run.params.get("deck_id") or 0) != deck_id:
                        continue
                    latest = {
                        "run_id": run.run_id,
                        "kind_title": run.kind_title,
                        "title": run.title,
                        "status": run.status,
                        "started_at": _clock(run.started_at),
                        "conclusion": str(run.summary.get("conclusion") or "")[:600],
                    }
                    break
            except Exception:  # noqa: BLE001  拿不到就算了，不该让训练台整页报错
                latest = {}

        return {
            "ok": True,
            "deck": {
                "deck_id": deck_id,
                "name": str(row["display_name"] or ""),
                "group_id": str(row["group_id"] or ""),
                "script": str(row["generated_script"] or ""),
                "picked_style": str(row["picked_style"] or ""),
                "windbot_deck": str(row["windbot_deck"] or ""),
                "in_random": bool(row["in_random"]),
                "head_card": panel.pick_head_card(Path(str(row["ydk_path"])))
                if Path(str(row["ydk_path"])).is_file()
                else 0,
            },
            "combos": archives,
            "combo_preview": _read_head(combo_dir / archives[0]["name"], 24) if archives else "",
            "latest": latest,
        }

    def _api_decks(self) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        rows = _number_rows(_read_deck_rows(panel.deck_db_path))
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            group_id = str(row.get("group_id") or "") or "(未分组)"
            ydk_path = Path(str(row.get("ydk_path") or ""))
            generated = str(row.get("generated_script") or "")
            picked = str(row.get("picked_style") or "")
            groups.setdefault(group_id, []).append(
                {
                    # `number` ＝ 群友在 `/卡组列表` 里看到的编号（前端显示它；`deck_id` 只用来调接口）
                    "number": row.get("number") or "",
                    "deck_id": row.get("deck_id") or "",
                    "name": row.get("display_name") or "",
                    "contributor": row.get("contributor_name") or "",
                    "main": row.get("main_count") or 0,
                    "extra": row.get("extra_count") or 0,
                    "side": row.get("side_count") or 0,
                    "in_random": bool(row.get("in_random")),
                    "generated_script": generated,
                    "picked_style": picked,
                    # 面板上的出牌脚本名：真正上场的是 generated_script（它优先），
                    # 挑样式只是"备选/历史"，两个都给前端，由前端决定怎么显示
                    "style_now": generated or picked or str(row.get("windbot_deck") or ""),
                    # 每副牌自己的 AI 决策档位（卡组页那个设置）：空串＝跟随全局
                    "brain_scope": str(row.get("brain_scope") or ""),
                    "created_at": row.get("created_at") or 0,
                    "is_builtin": group_id == "__builtin__",
                    # 缩略图用：卡表里的「头牌」（优先额外卡组里本地真有图的那张）
                    "head_card": panel.pick_head_card(ydk_path) if ydk_path.is_file() else 0,
                    # 卡表文件不存在时前端要能直接说"文件没了"，而不是画一张空图
                    "file_missing": not ydk_path.is_file(),
                }
            )
        return {
            "ok": True,
            "groups": [
                {"group_id": key, "decks": value} for key, value in sorted(groups.items())
            ],
        }

    def _send_art(self, raw_id: str) -> None:
        """发一张卡图（卡号 → 本地图片字节）。

        **只认纯数字卡号**，文件名由卡号拼出来：这样"路径穿越"这类问题从形状上就不成立
        （用户输入进不了路径，只能是数字）。取图顺序与出图那条链路一致：
        本地缓存（`temp/card_pics/`）→ 配置的卡图目录 → 备用目录；都没有就 404，
        前端画一个卡背占位——**不在这里联网**（面板打开要快，缺图交给查房那条链路去补）。
        """

        card_id = raw_id.strip()
        if not card_id.isdigit() or len(card_id) > 12:
            self._send_json({"ok": False, "error": "卡号必须是数字"}, status=400)
            return
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        for path in panel.art_candidates(int(card_id)):
            try:
                body = path.read_bytes()
            except OSError:
                continue
            if body:
                mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                self._send_bytes(body, mime, max_age=_ART_CACHE_SECONDS)
                return
        self._send_json({"ok": False, "error": "这张卡本地没有图"}, status=404)

    def _api_deck_detail(self, deck_id: str, query: Dict[str, List[str]]) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        group_id = str((query.get("group") or [""])[0])
        for row in _number_rows(_read_deck_rows(panel.deck_db_path)):
            if str(row.get("deck_id") or "") != deck_id:
                continue
            if group_id and str(row.get("group_id") or "") != group_id:
                continue
            ydk_path = Path(str(row.get("ydk_path") or ""))
            summary = (
                _read_ydk_summary(ydk_path)
                if ydk_path.exists()
                else {"main": [], "extra": [], "side": [], "error": f"找不到卡表文件：{ydk_path}"}
            )
            names: Dict[str, List[str]] = {}
            entries: Dict[str, List[Dict[str, Any]]] = {}
            card_db = getattr(panel.plugin, "_card_db", None)
            for zone in ("main", "extra", "side"):
                ids = [int(item) for item in summary.get(zone, []) if str(item).isdigit()]
                zone_names = _card_names(card_db, ids)
                # 卡名列表（按 .ydk 原始顺序，一张卡 3 张就出现 3 次）
                names[zone] = [zone_names.get(card_id, str(card_id)) for card_id in ids]
                # 聚合后的清单（新字段）：面板按"卡图 + 卡名 + ×张数"渲染
                entries[zone] = _group_cards([str(item) for item in summary.get(zone, [])], card_db)
            return {
                "ok": True,
                "deck": {
                    # 详情页也显示**列表编号**（和列表卡片、和群里 `/卡组详情 <编号>` 是同一个号）
                    "number": row.get("number") or "",
                    "deck_id": row.get("deck_id") or "",
                    "group_id": row.get("group_id") or "",
                    "name": row.get("display_name") or "",
                    "contributor": row.get("contributor_name") or "",
                    "ydk_path": str(ydk_path),
                    "deck_code": row.get("deck_code") or "",
                    "counts": {
                        "main": len(summary.get("main", [])),
                        "extra": len(summary.get("extra", [])),
                        "side": len(summary.get("side", [])),
                    },
                    "cards": names,
                    "entries": entries,
                    "head_card": panel.pick_head_card(ydk_path) if ydk_path.is_file() else 0,
                    "generated_script": str(row.get("generated_script") or ""),
                    "picked_style": str(row.get("picked_style") or ""),
                    "in_random": bool(row.get("in_random")),
                    "brain_scope": str(row.get("brain_scope") or ""),
                    "error": summary.get("error", ""),
                },
            }
        return {"ok": False, "error": f"找不到卡组：{deck_id}"}

    def _api_logs(self, lines: int, keyword: str) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        raw_lines = _tail_log_lines(panel.host_root, lines, keyword)
        return {
            "ok": True,
            "count": len(raw_lines),
            "lines": [_pretty_log_line(line) for line in raw_lines],
        }


def _card_name_or_id(card_db: Any, passcode: str) -> str:
    """单个卡号 → 卡名；查不到就原样返回卡号（前端的兜底也是这个口径）。"""

    if not str(passcode).isdigit():
        return str(passcode)
    return _card_names(card_db, [int(passcode)]).get(int(passcode), str(passcode))


def _card_names(card_db: Any, card_ids: List[int]) -> Dict[int, str]:
    """批量取卡名：尽量一次查询拿全，查不到的卡就不放进结果（调用方自己回落成卡号）。

    ⚠ 用的是 `CardDatabase.card_details`（批量）而不是逐张 `name()`：一副牌 40~55 张，
    逐张查就是 55 次 SQL；顺手也把"卡库不认识的卡"自然地区分出来了。
    """

    names: Dict[int, str] = {}
    wanted = [int(card_id) for card_id in card_ids]
    if card_db is None or not wanted:
        return names
    try:
        details = card_db.card_details(wanted)
    except Exception:  # noqa: BLE001  卡库接口差异不该让面板整页报错
        details = {}
    for card_id, detail in (details or {}).items():
        name = str(getattr(detail, "name", "") or "")
        if name:
            names[int(card_id)] = name
    for card_id in wanted:
        if card_id in names:
            continue
        # 批量查询漏掉的（或整批失败时的）个别卡：再单独问一次
        try:
            name = card_db.name(card_id)
        except Exception:  # noqa: BLE001
            name = None
        if name:
            names[card_id] = str(name)
    return names


def _current_seat(recorder: Any, state: Any) -> Optional[int]:
    """现在轮到谁下（拿不到就 None）。

    三个来源依次试：记录器的当前回合方、局面对象里的同一字段。**不猜**：都没有就返回 None，
    面板上不显示"轮到谁"，而不是标错一方。
    """

    for holder in (state, recorder):
        for attr in ("current_seat", "turn_player", "turn_seat"):
            value = getattr(holder, attr, None)
            if isinstance(value, int):
                return value
    return None


def _board_sides(
    state: Any,
    *,
    names: Dict[int, str],
    stats_of: Dict[int, Any],
    self_seat: int,
    lp_hint: Optional[Dict[int, int]] = None,
    start_lp: int = 0,
    our_deck: str = "",
    card_db: Any = None,
    current_seat: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """把 `FieldState` 摊成面板画**整张牌桌**要的形状。

    位置与内核的位号一一对应（与 `/查房` 出图同一套，见 `duel/fieldstate.py`）：

    * 怪兽区 7 格：0~4 主怪兽区、5~6 **额外怪兽区**
    * 魔陷区 5 格 + **场地魔法区**（SPELL_ZONE 第 5 号位）+ **灵摆区 2 格**
    * 墓地 / 除外 / 额外卡组 三堆的当前张数
    * 每张卡：表侧的给卡号（前端自己去取卡图与卡名）、里侧的**只报"有卡"**

    **还没有局面时也要给两边**（房刚开好、人还没进来时）：以前这种情况直接返回空列表，
    面板上只剩一个标题、看着像坏了——现在照样给两边的名字与初始 LP，牌区空着。
    """

    zones_of = getattr(state, "zones_of", None)
    players = dict(getattr(state, "players", {}) or {}) if state is not None else {}
    hints = dict(lp_hint or {})
    sides: List[Dict[str, Any]] = []
    for seat in (self_seat, 1 - self_seat):
        seat_zones: Dict[Any, Any] = {}
        if callable(zones_of):
            try:
                seat_zones = dict(zones_of(seat) or {})
            except Exception:  # noqa: BLE001  某个座位读失败就当空场，别让整页报错
                seat_zones = {}
        # 卡名与攻守：一次批量查完（逐张查一副场要几十次 SQL）
        wanted = [
            card.card_id
            for card in seat_zones.values()
            if getattr(card, "card_id", 0) and getattr(card, "face_up", False)
        ]
        details = _card_names(card_db, wanted) if wanted else {}
        detail_map: Dict[int, Any] = {}
        if card_db is not None and wanted:
            try:
                detail_map = card_db.card_details(sorted(set(wanted)))
            except Exception:  # noqa: BLE001
                detail_map = {}

        def slot(location: int, sequence: int) -> Optional[Dict[str, Any]]:
            """一格 → 面板要的信息（``None`` 表示空格）。"""

            card = seat_zones.get((location, sequence))
            return _slot_json(card, names=details, details=detail_map)

        def spell_slot(sequence: int) -> Optional[Dict[str, Any]]:
            """魔陷区的一格。

            ⚠ **灵摆区就是魔陷区最左（0）与最右（4）这两格**（大师规则 4 之后不再单独占地）：
            内核有时把牌报在 ``SPELL_ZONE``、有时报在 ``PENDULUM_ZONE``，两处都要看，
            否则摆上灵摆的牌会在面板上凭空消失（第一版把灵摆单列成两张独立卡位，是错的）。
            """

            card = seat_zones.get((SPELL_ZONES[0], sequence))
            if card is None and sequence in (0, 4):
                card = seat_zones.get((SPELL_ZONES[1], 1 if sequence == 4 else 0))
            return _slot_json(card, names=details, details=detail_map)

        player = players.get(seat)
        stats = stats_of.get(seat)
        lp = getattr(player, "lp", None)
        if lp is None or not int(lp):
            # 还没收到任何 LP 报文（等人进房时就是这样）：先显示初始 LP，别显示 0
            lp = hints.get(seat, start_lp)
        sides.append(
            {
                "seat": seat,
                "is_self": seat == self_seat,
                "name": names.get(seat, "") or ("我方" if seat == self_seat else "对手"),
                "deck": _side_deck_label(state, seat, names, self_seat, our_deck, card_db),
                "lp": int(lp or 0),
                "is_turn": current_seat == seat,
                # 主怪兽区 5 + 额外怪兽区 2
                "monsters": [slot(MONSTER_ZONE, seq) for seq in range(5)],
                "extra_monsters": [slot(MONSTER_ZONE, seq) for seq in (5, 6)],
                # 魔陷区 5 格（**最左/最右就是灵摆区**）+ 场地区
                "spells": [spell_slot(seq) for seq in range(5)],
                "spell_pendulum": [seq in (0, 4) for seq in range(5)],
                "field_zone": slot(SPELL_ZONES[0], 5),
                "piles": {
                    "grave": int(getattr(player, "grave", 0) or 0),
                    "banished": int(getattr(player, "banished", 0) or 0),
                    "extra": int(getattr(player, "extra", 0) or 0),
                },
                "stats": _stats_json(stats),
            }
        )
    return sides


def _slot_json(card: Any, *, names: Dict[int, str], details: Dict[int, Any]) -> Optional[Dict[str, Any]]:
    """场上一格 → 面板要的信息（``None`` 表示空格）。

    ⚠ 记录器里存的是 `ZoneCard`（带 ``card_id`` / ``position`` / 攻守），**不是卡号**：
    第一版按卡号写（`int(card)`）在对局中就炸了 `TypeError: int() argument must be ... not 'ZoneCard'`。
    里侧的卡**不报卡号**（只报"有卡 + 里侧"），与出图同一口径。

    攻守取"内核说过的那份"（`ZoneCard.attack/defense`，来自 `MSG_UPDATE_DATA`），内核没说过时
    退回**卡面数值**（卡库里的），并在 `stats_live` 里说明是哪一种——装备/场地加成之后两者会不一样。
    """

    if card is None:
        return None
    card_id = int(getattr(card, "card_id", 0) or 0)
    if not card_id:
        return None
    face_up = bool(getattr(card, "face_up", True))
    attack_position = bool(getattr(card, "attack_position", False))
    if not face_up:
        # 里侧的卡：只给"有卡 + 攻/守表示"，卡号卡名一律不出去（与出图同一口径）
        return {"id": 0, "face_up": False, "attack": attack_position, "name": "", "kind": ""}
    detail = details.get(card_id)
    name = ""
    if detail is not None:
        name = str(getattr(detail, "name", "") or "")
    live_atk = int(getattr(card, "attack", -1))
    live_def = int(getattr(card, "defense", -1))
    base_atk = getattr(detail, "atk", None) if detail is not None else None
    base_def = getattr(detail, "defense", None) if detail is not None else None
    atk = live_atk if live_atk >= 0 else base_atk
    def_ = live_def if live_def >= 0 else base_def
    return {
        "id": card_id,
        "face_up": True,
        "attack": attack_position,
        "name": name or names.get(card_id, ""),
        "atk": atk,
        "def_": def_,
        # 攻守是哪来的：内核当前值 / 卡面数值（面板上要能分辨，别把卡面当成现在的）
        "stats_live": live_atk >= 0 or live_def >= 0,
        "level": int(getattr(detail, "level", 0) or 0) if detail is not None else 0,
        "kind": _card_kind_translate(detail) if detail is not None else "",
    }


def _card_kind_translate(detail: Any) -> str:
    """卡的种类（怪兽/魔法/陷阱）——面板用它决定卡框颜色。"""

    type_text = str(getattr(detail, "type_text", "") or "")
    if "魔法" in type_text:
        return "spell"
    if "陷阱" in type_text:
        return "trap"
    return "monster" if type_text else ""


def _side_deck_label(
    state: Any,
    seat: int,
    names: Dict[int, str],
    self_seat: int,
    our_deck: str,
    card_db: Any,
) -> str:
    """这一侧用的什么牌：我方用房间记录里的卡组名；对面内核不给，只能**按见过的卡名猜**。"""

    if seat == self_seat:
        return our_deck
    seen_of = getattr(state, "seen_ids", None)
    if not callable(seen_of) or card_db is None:
        return ""
    try:
        seen = [int(card_id) for card_id in seen_of(seat)]
    except Exception:  # noqa: BLE001
        return ""
    if not seen:
        return ""
    name_map = _card_names(card_db, seen)
    guessed = _archetype_guess([name_map[card_id] for card_id in seen if card_id in name_map])
    label = f"{guessed}（看牌猜）" if guessed else f"已见 {len(seen)} 张"
    del names
    return label


def _stats_json(stats: Any) -> Dict[str, Any]:
    """记录器那份台账 → 面板要的字段（拿不到就给空字典）。"""

    if stats is None:
        return {}
    fields = (
        "normal_summons",
        "sp_summons",
        "effects",
        "sets",
        "draws",
        "attacks",
        "direct_attacks",
        "damage_dealt",
        "damage_taken",
        "biggest_hit_taken",
        "lp_recovered",
        "sent_to_grave",
        "banished",
    )
    result: Dict[str, Any] = {}
    for field_name in fields:
        value = getattr(stats, field_name, None)
        if value is not None:
            result[field_name] = int(value or 0)
    return result


class _ThreadingPanelServer(ThreadingHTTPServer):
    """带插件引用的 ThreadingHTTPServer。"""

    daemon_threads = True
    # 允许复用地址：这样插件重载（关掉旧的、马上起新的）不会因为上个连接还在 TIME_WAIT
    # 而绑不上。⚠ 但 Windows 上 SO_REUSEADDR 的语义比 Linux 宽——它允许**抢别人已经占着的
    # 端口**，绑定时不报错。所以"端口被占用"这件事不能靠这次 bind 发现，得先用
    # `_port_available()` 独占探测一次（见 `WebUIServer.start`）。
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], panel: "WebUIServer") -> None:
        self.panel = panel
        super().__init__(address, _PanelHandler)


def _port_available(host: str, port: int) -> bool:
    """端口能不能独占绑定（探测用；绑定失败＝端口已被别的程序占着）。

    探测必须用**独占语义**：`SO_EXCLUSIVEADDRUSE` 在 Windows 上才是"这块地址归我，
    别人已经占着就失败"。不加它的话，探测自己也会被 `allow_reuse_address` 那套语义骗过去
    （实测就是这样：第二个面板"成功"绑上了第一个面板已经在听的端口）。
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class WebUIServer:
    """插件自带的面板服务：起一个守护线程，卸载时同步关闭。"""

    def __init__(
        self,
        plugin: Any,
        *,
        host: str,
        port: int,
        api_key: str,
        key_source: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.plugin = plugin
        self.host = host
        self.port = int(port)
        self.api_key = api_key
        self.key_source = key_source
        self.logger = logger

        self._server: Optional[_ThreadingPanelServer] = None
        self._thread: Optional[threading.Thread] = None
        self.started_at = ""
        self.bound_port = 0

        data_dir = Path(plugin.ctx.paths.data_dir)
        self.data_dir = data_dir
        # data_dir = <项目根>/data/plugins/<插件 id> → parents[2] 就是项目根
        self.host_root = data_dir.parents[2] if len(data_dir.parents) >= 3 else data_dir
        self.deck_db_path = data_dir / "deck_pool.db"

    # ---- 只读访问器（面板线程用，全部走插件自己的公开方法读，不碰它的私有状态）----

    def training_workspace(self) -> Path:
        """训练工作目录（与插件用的是同一个：都在 `_training_workspace` 里算）。"""

        return self.plugin._training_workspace()

    def art_candidates(self, card_id: int) -> List[Path]:
        """这张卡**本地**可能有图的几个位置，按优先级：缓存 → 立绘目录 → 备用目录。

        与出图那条链路（`duel/field_image.py` 的 `_read_art` + `duel/card_images.py` 的缓存）
        保持同一个顺序，免得"面板上有图、发到群里没有"这种对不上的情况。
        只读配置里的目录，不联网、不建目录。
        """

        suffixes = _ART_SUFFIXES
        directories: List[Tuple[Path, Tuple[str, ...]]] = []
        cache = cache_dir_for()
        directories.append((cache, suffixes))
        paths = getattr(self.plugin.config, "paths", None)
        if paths is not None:
            directories.append((paths.resolved_card_art_dir(), suffixes))
            directories.append((paths.resolved_card_art_fallback_dir(), suffixes))
        candidates: List[Path] = []
        for directory, order in directories:
            for suffix in order:
                candidates.append(Path(directory) / f"{int(card_id)}{suffix}")
        return candidates

    def _wait_port_free(self, seconds: float = 6.0, step: float = 0.5) -> bool:
        """等端口空出来（最多几秒）。

        **为什么必须重试**：插件每次重载都会先关旧面板再起新的，而旧那个的监听线程不一定
        已经退干净——一次探测就放弃的话，面板会在这一次重载里彻底起不来（用户看到的
        "面板又打不开了"，日志里是"已经被别的程序占着"）。实测日志里这种时序占了大多数，
        所以给它几秒钟；真的是被别的程序长期占着时，等满这几秒后照样如实报错、绝不抢端口。
        """

        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if _port_available(self.host, self.port):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)

    def has_art(self, card_id: int) -> bool:
        """本地有没有这张卡的图（只 stat，不读字节）。"""

        for path in self.art_candidates(card_id):
            try:
                if path.is_file():
                    return True
            except OSError:
                continue
        return False

    def pick_head_card(self, ydk_path: Path) -> int:
        """给一副牌挑缩略图用的卡号（优先挑本地**真的有图**的那张）。"""

        return _head_card_of(ydk_path, self.has_art)

    # ---- 生命周期 ----

    @property
    def url(self) -> str:
        """面板地址（密钥不在 URL 里，登录页会引导填）。"""

        display_host = self.host if self.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        return f"http://{display_host}:{self.bound_port or self.port}/"

    def start(self) -> bool:
        """启动面板；端口被占用等失败一律返回 False 并记日志，绝不抛给插件加载路径。"""

        if self.port and not self._wait_port_free():
            # 这一步是为了"端口被占就明说"：直接 bind 在 Windows 上不一定报错（SO_REUSEADDR
            # 允许抢端口），抢过来之后两个程序都以为自己在这个端口上服务，排查起来极难。
            if self.logger is not None:
                self.logger.warning(
                    "WebUI 启动失败：%s:%s 已经被别的程序占着（等了几秒也没等到它释放；改 webui.port 换一个）",
                    self.host,
                    self.port,
                )
            return False
        try:
            server = _ThreadingPanelServer((self.host, self.port), self)
        except OSError as exc:
            if self.logger is not None:
                self.logger.warning(
                    "WebUI 启动失败（%s:%s）：%s；改 webui.port 或关掉 webui.enabled",
                    self.host,
                    self.port,
                    exc,
                )
            return False
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.warning("WebUI 启动失败：%s", exc)
            return False

        self._server = server
        self.bound_port = int(server.server_address[1])
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._thread = threading.Thread(target=server.serve_forever, name="mai-play-ygo-webui", daemon=True)
        self._thread.start()
        if self.logger is not None:
            self.logger.info("WebUI 已启动：%s（密钥来源：%s）", self.url, self.key_source)
        return True

    def stop_now(self) -> None:
        """同步关闭面板：卸载路径只有 5 秒预算，这里不 await、不等优雅收尾。"""

        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception as exc:  # noqa: BLE001  卸载阶段必须尽力收尾
            if self.logger is not None:
                self.logger.exception("WebUI 关闭失败：%s", exc)
        finally:
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
            if self.logger is not None:
                self.logger.info("WebUI 已停止")


# ---------------------------------------------------------------------------
# 界面（单页，内嵌；不引用任何外网资源——离线也要能打开）
# ---------------------------------------------------------------------------

_STYLE = """
/* 设计基调：深色「牌桌」——近黑的蓝灰底 + 一层层抬起来的卡面，紫色为主动作色，
   状态色只用在状态上。全部内联，不引用任何外网资源（离线也要能打开）。*/
:root {
  color-scheme: dark;
  --bg:#0a0d14;
  --bg-soft:#0f131d;
  --panel:#151b27;
  --panel-2:#1a2130;
  --line:#232c3d;
  --line-soft:#1b2231;
  --text:#e8ecf6;
  --muted:#8a97b0;
  --faint:#5d6a83;
  --brand:#7c6cf6;
  --brand-2:#a78bfa;
  --cyan:#4cc9f0;
  --ok:#3ddc97;
  --warn:#ffb454;
  --danger:#ff6b81;
  --r-lg:16px; --r-md:12px; --r-sm:9px;
  --shadow:0 10px 30px -12px rgba(0,0,0,.75);
  --tap: cubic-bezier(.22,.61,.36,1);
}
* { box-sizing:border-box; }
html, body { height:100%; }
body {
  margin:0; background:
    radial-gradient(1100px 620px at 12% -8%, rgba(124,108,246,.16), transparent 62%),
    radial-gradient(900px 560px at 100% 0%, rgba(76,201,240,.10), transparent 58%),
    var(--bg);
  color:var(--text); font-size:14px; line-height:1.55;
  font-family:-apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  -webkit-font-smoothing:antialiased;
}
a { color:var(--brand-2); text-decoration:none; }
a:hover { color:#c4b5fd; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:#26304a; border-radius:20px; border:3px solid transparent; background-clip:content-box; }
::-webkit-scrollbar-thumb:hover { background:#33405f; background-clip:content-box; }
::-webkit-scrollbar-track { background:transparent; }
svg.i { width:18px; height:18px; flex:none; fill:none; stroke:currentColor; stroke-width:1.7;
        stroke-linecap:round; stroke-linejoin:round; }

/* ---- 布局：左侧栏 + 主区 ---- */
.shell { display:flex; min-height:100vh; }
.side {
  width:236px; flex:none; padding:18px 14px; display:flex; flex-direction:column; gap:8px;
  background:linear-gradient(180deg, rgba(21,27,39,.92), rgba(15,19,29,.92));
  border-right:1px solid var(--line); backdrop-filter:blur(6px);
  position:sticky; top:0; height:100vh;
}
.brand { display:flex; align-items:center; gap:10px; padding:6px 8px 14px; }
.brand .mark { width:34px; height:34px; border-radius:11px; flex:none;
  background:linear-gradient(145deg, var(--brand), #4f46e5 62%, var(--cyan));
  display:grid; place-items:center; color:#fff; box-shadow:0 6px 18px -8px rgba(124,108,246,.9); }
.brand .mark svg.i { width:19px; height:19px; stroke-width:1.9; }
.brand b { font-size:15px; letter-spacing:.2px; display:block; }
.brand span { font-size:11.5px; color:var(--faint); }
.side nav { display:flex; flex-direction:column; gap:4px; margin-top:6px; }
.navlink { display:flex; align-items:center; gap:11px; padding:10px 12px; border-radius:var(--r-md);
  color:var(--muted); cursor:pointer; border:1px solid transparent; font-size:13.5px;
  transition:background .16s var(--tap), color .16s var(--tap), transform .16s var(--tap); }
.navlink:hover { background:rgba(124,108,246,.08); color:var(--text); }
.navlink.active { color:#fff; background:linear-gradient(100deg, rgba(124,108,246,.22), rgba(76,201,240,.09));
  border-color:rgba(124,108,246,.34); box-shadow:inset 0 1px 0 rgba(255,255,255,.05); }
.navlink .n { margin-left:auto; font-size:11px; color:var(--faint); }
.side .foot { margin-top:auto; padding:10px 8px 2px; font-size:11.5px; color:var(--faint); }
.side .foot code { color:var(--muted); word-break:break-all; font-size:11px; }
.main { flex:1; min-width:0; display:flex; flex-direction:column; }
.topbar { display:flex; align-items:center; gap:12px; padding:16px 26px 12px; }
.topbar h1 { font-size:19px; margin:0; font-weight:650; letter-spacing:.2px; }
.topbar .sub { font-size:12.5px; color:var(--faint); }
.topbar .sp { flex:1; }
.wrap { padding:0 26px 40px; max-width:1500px; width:100%; }
section.view { display:none; animation:fade .22s var(--tap); }
section.view.active { display:block; }
@keyframes fade { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }

/* ---- 通用元件 ---- */
.panel { background:linear-gradient(180deg, var(--panel), var(--panel-2));
  border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow); }
.panel > .hd { display:flex; align-items:center; gap:10px; padding:14px 18px; border-bottom:1px solid var(--line-soft); }
.panel > .hd h3 { margin:0; font-size:14px; font-weight:600; }
.panel > .hd .sp { flex:1; }
.panel > .bd { padding:16px 18px; }
.grid { display:grid; gap:14px; }
.cols-2 { grid-template-columns:minmax(0,1fr) minmax(0,1fr); }
.cols-main { grid-template-columns:minmax(0,1.55fr) minmax(0,1fr); align-items:start; }
.muted { color:var(--muted); }
.faint { color:var(--faint); }
.mono { font-family:ui-monospace, "Cascadia Mono", Consolas, monospace; font-size:12px; }
.chip { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; padding:3px 9px;
  border-radius:999px; border:1px solid var(--line); color:var(--muted); background:rgba(255,255,255,.02); white-space:nowrap; }
.chip svg.i { width:13px; height:13px; }
.chip.ok { border-color:rgba(61,220,151,.4); color:#7ff0bb; background:rgba(61,220,151,.09); }
.chip.run { border-color:rgba(124,108,246,.45); color:#c7bdfd; background:rgba(124,108,246,.12); }
.chip.warn { border-color:rgba(255,180,84,.4); color:#ffd39a; background:rgba(255,180,84,.1); }
.chip.err { border-color:rgba(255,107,129,.42); color:#ffb3bd; background:rgba(255,107,129,.1); }
.chip.dim { border-color:var(--line); color:var(--faint); }
.chip.brand { border-color:rgba(124,108,246,.4); color:#cfc7ff; background:rgba(124,108,246,.12); }
.btn { display:inline-flex; align-items:center; justify-content:center; gap:7px; cursor:pointer;
  font-size:13px; font-weight:550; padding:9px 14px; border-radius:var(--r-sm);
  border:1px solid var(--line); background:var(--panel-2); color:var(--text);
  transition:transform .14s var(--tap), border-color .16s var(--tap), background .16s var(--tap), opacity .16s; }
.btn:hover { border-color:#33405f; background:#1f2839; transform:translateY(-1px); }
.btn:active { transform:none; }
.btn.primary { background:linear-gradient(100deg, var(--brand), #6d5ce7); border-color:transparent; color:#fff;
  box-shadow:0 8px 22px -12px rgba(124,108,246,1); }
.btn.primary:hover { background:linear-gradient(100deg, #8a7bff, #7a68f0); }
.btn.danger { border-color:rgba(255,107,129,.4); color:#ffb3bd; background:rgba(255,107,129,.08); }
.btn.danger:hover { background:rgba(255,107,129,.16); border-color:var(--danger); }
.btn[disabled] { opacity:.45; cursor:not-allowed; transform:none; }
.btn.sm { padding:6px 11px; font-size:12.5px; }
input, select, textarea { font-family:inherit; font-size:13.5px; padding:9px 11px; border-radius:var(--r-sm);
  border:1px solid var(--line); background:#101623; color:var(--text); width:100%;
  transition:border-color .16s var(--tap), box-shadow .16s var(--tap); }
input:focus, select:focus, textarea:focus { outline:none; border-color:rgba(124,108,246,.6);
  box-shadow:0 0 0 3px rgba(124,108,246,.16); }
label.field { display:flex; flex-direction:column; gap:5px; font-size:11.5px; color:var(--faint); letter-spacing:.2px; }
label.field > span { display:flex; align-items:center; gap:6px; }
label.field > span.info { color:var(--faint); font-size:11px; }
.toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:14px; }
.toolbar .grow { flex:1; min-width:200px; }
.switch { display:inline-flex; align-items:center; gap:8px; font-size:12.5px; color:var(--muted);
  cursor:pointer; user-select:none; }
.switch input { width:auto; }
/* 开关做成滑块：配置页里一眼能看出"开/关"，比一个默认的方框勾选好认 */
.switch input[type=checkbox] { appearance:none; -webkit-appearance:none; width:38px; height:21px;
  border-radius:999px; background:#232c3d; border:1px solid var(--line); position:relative;
  cursor:pointer; transition:background .18s var(--tap), border-color .18s var(--tap); flex:none; }
.switch input[type=checkbox]::after { content:""; position:absolute; top:2px; left:2px; width:15px; height:15px;
  border-radius:50%; background:#8a97b0; transition:transform .18s var(--tap), background .18s var(--tap); }
.switch input[type=checkbox]:checked { background:rgba(124,108,246,.45); border-color:var(--brand); }
.switch input[type=checkbox]:checked::after { transform:translateX(17px); background:#fff; }

/* ---- 概览 ---- */
.stats { display:grid; grid-template-columns:repeat(auto-fit, minmax(158px, 1fr)); gap:12px; }
.stat { position:relative; overflow:hidden; padding:15px 16px; border-radius:var(--r-lg);
  background:linear-gradient(180deg, var(--panel), var(--panel-2)); border:1px solid var(--line); }
.stat::after { content:""; position:absolute; inset:0 0 auto 0; height:2px;
  background:linear-gradient(90deg, var(--brand), var(--cyan)); opacity:.55; }
.stat .k { display:flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); }
.stat .v { font-size:28px; font-weight:680; letter-spacing:.5px; margin-top:7px; line-height:1.05;
  font-variant-numeric:tabular-nums; }
.stat .s { font-size:11.5px; color:var(--faint); margin-top:3px; }
.kv { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:9px 18px; font-size:13px; }
.kv dt { color:var(--faint); }
.kv dd { margin:0; word-break:break-all; }
.rooms { display:flex; flex-direction:column; gap:8px; }
.room { display:flex; align-items:center; gap:10px; padding:10px 12px; border-radius:var(--r-md);
  background:#111826; border:1px solid var(--line-soft); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--ok); box-shadow:0 0 0 4px rgba(61,220,151,.14); }

/* ---- 卡组：卡片栅格 + 抽屉 ---- */
.grp { margin-bottom:22px; }
.grp > h3 { display:flex; align-items:center; gap:9px; margin:0 0 10px; font-size:14px; font-weight:600; }
.grp > h3 .n { font-size:11.5px; color:var(--faint); font-weight:500; }
.deckgrid { display:grid; grid-template-columns:repeat(auto-fill, minmax(228px, 1fr)); gap:13px; }
.deck { position:relative; display:flex; gap:12px; align-items:center; padding:11px 12px; cursor:pointer;
  border-radius:var(--r-lg); border:1px solid var(--line); background:linear-gradient(180deg, var(--panel), var(--panel-2));
  transition:transform .16s var(--tap), border-color .16s var(--tap), box-shadow .16s var(--tap); }
.deck:hover { transform:translateY(-3px); border-color:rgba(124,108,246,.5);
  box-shadow:0 16px 34px -18px rgba(124,108,246,.85); }
.deck .art { width:56px; height:56px; flex:none; border-radius:10px; overflow:hidden; background:#0b101a;
  border:1px solid var(--line); display:grid; place-items:center; color:var(--faint); }
.deck .art img { width:100%; height:100%; object-fit:cover; display:block; }
.deck .art.zoom img { object-fit:contain; }
.deck .meta { min-width:0; flex:1; }
.deck .nm { font-weight:600; font-size:13.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.deck .ln { display:flex; gap:6px; flex-wrap:wrap; margin-top:5px; }
.deck .cnt { font-size:11px; color:var(--faint); margin-top:5px; font-variant-numeric:tabular-nums; }
.deckctl { display:flex; gap:10px; align-items:center; margin-top:7px; flex-wrap:wrap; }
.switch.sm { font-size:11.5px; gap:6px; }
.switch.sm input[type=checkbox] { width:30px; height:17px; }
.switch.sm input[type=checkbox]::after { width:11px; height:11px; }
.switch.sm input[type=checkbox]:checked::after { transform:translateX(13px); }
.field.mini { font-size:11px; gap:2px; }
.field.mini select { min-width:106px; font-size:11.5px; padding:3px 6px; }
.empty { display:flex; flex-direction:column; align-items:center; gap:10px; padding:48px 20px; color:var(--faint); }
.empty svg.i { width:34px; height:34px; opacity:.55; }
.sheet-mask { position:fixed; inset:0; background:rgba(4,6,11,.62); backdrop-filter:blur(3px);
  opacity:0; pointer-events:none; transition:opacity .2s var(--tap); z-index:40; }
.sheet-mask.on { opacity:1; pointer-events:auto; }
.sheet { position:fixed; top:0; right:0; height:100vh; width:min(620px, 94vw); z-index:41;
  background:linear-gradient(180deg, #131926, #0d1119); border-left:1px solid var(--line);
  transform:translateX(102%); transition:transform .26s var(--tap); display:flex; flex-direction:column;
  box-shadow:-24px 0 60px -30px #000; }
.sheet.on { transform:none; }
.sheet .hd { display:flex; align-items:flex-start; gap:12px; padding:18px 20px 14px; border-bottom:1px solid var(--line-soft); }
.sheet .hd h3 { margin:0; font-size:17px; }
.sheet .bd { overflow:auto; padding:16px 20px 30px; }
.sheet .hero { display:flex; gap:14px; align-items:center; margin-bottom:14px; }
.sheet .hero .art { width:92px; height:92px; border-radius:12px; overflow:hidden; border:1px solid var(--line);
  background:#0b101a; display:grid; place-items:center; flex:none; }
.sheet .hero .art img { width:100%; height:100%; object-fit:cover; }
.ell { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%; }
.zone { margin-top:16px; }
.zone > h4 { display:flex; align-items:center; gap:8px; margin:0 0 9px; font-size:13px; }
.zone > h4 .n { color:var(--faint); font-weight:500; font-size:11.5px; }
.cardrow { display:grid; grid-template-columns:repeat(auto-fill, minmax(178px, 1fr)); gap:8px; }
.crow { display:flex; align-items:center; gap:9px; padding:7px 9px; border-radius:10px;
  background:#111826; border:1px solid var(--line-soft); transition:border-color .16s var(--tap), transform .16s var(--tap); }
.crow:hover { border-color:#2f3a52; transform:translateY(-1px); }
.crow .thumb { width:34px; height:34px; flex:none; border-radius:7px; overflow:hidden; background:#0b101a;
  border:1px solid var(--line-soft); display:grid; place-items:center; color:var(--faint); }
.crow .thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.crow .nm { font-size:12.5px; min-width:0; flex:1; overflow:hidden; display:-webkit-box;
  -webkit-line-clamp:2; -webkit-box-orient:vertical; line-height:1.35; }.crow .ct { font-size:11.5px; color:var(--brand-2); font-variant-numeric:tabular-nums; }

/* ---- 训练 ---- */
.kind { border:1px solid var(--line); border-radius:var(--r-lg); padding:15px 16px;
  background:linear-gradient(180deg, var(--panel), var(--panel-2)); }
.kind + .kind { margin-top:12px; }
.kind h4 { margin:0 0 3px; font-size:13.5px; display:flex; align-items:center; gap:8px; }
.kind .note { font-size:12px; color:var(--faint); margin:0 0 12px; }
.kind .fields { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:11px; align-items:end; }
.kind .go { display:flex; justify-content:flex-end; margin-top:12px; }
.runlist { display:flex; flex-direction:column; gap:9px; }
.run { display:flex; align-items:center; gap:12px; padding:11px 13px; border-radius:var(--r-md);
  border:1px solid var(--line-soft); background:#111826; cursor:pointer;
  transition:border-color .16s var(--tap), background .16s var(--tap), transform .16s var(--tap); }
.run:hover { border-color:#33405f; background:#141c2b; transform:translateX(2px); }
.run .rid { font-size:11px; color:var(--faint); }
.run .ttl { font-weight:550; font-size:13px; }
.run .sp { flex:1; min-width:0; }
.run .meta { font-size:11.5px; color:var(--faint); margin-top:3px; }
.live { border-radius:var(--r-lg); border:1px solid rgba(124,108,246,.36);
  background:linear-gradient(180deg, rgba(124,108,246,.10), rgba(124,108,246,.03)); padding:15px 16px; margin-bottom:14px; }
.live .hd { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.round { border:1px solid var(--line-soft); border-radius:var(--r-md); padding:10px 12px; background:#111826; }
.round + .round { margin-top:8px; }
.round .rh { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:13px; }
.spin { width:14px; height:14px; border-radius:50%; border:2px solid rgba(199,189,253,.35);
  border-top-color:#c7bdfd; animation:spin 1s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
pre.log { background:#0b0f19; border:1px solid var(--line); border-radius:var(--r-md); padding:13px 14px;
  overflow:auto; max-height:56vh; font-size:12px; line-height:1.6; margin:0; color:#c9d3e6;
  font-family:ui-monospace, "Cascadia Mono", Consolas, monospace; white-space:pre-wrap; word-break:break-word; }
pre.log .lv-error { color:#ff9aa8; } pre.log .lv-warning { color:#ffd39a; }
pre.log .lv-debug { color:#6f7d95; } pre.log .lv-info { color:#9fe8c8; }
.banner { display:flex; gap:10px; align-items:flex-start; padding:11px 13px; border-radius:var(--r-md);
  margin-bottom:12px; font-size:13px; border:1px solid var(--line); background:#111826; }
.banner svg.i { margin-top:2px; }
.banner.warn { border-color:rgba(255,180,84,.35); background:rgba(255,180,84,.08); color:#ffd9a6; }
.banner.err { border-color:rgba(255,107,129,.35); background:rgba(255,107,129,.08); color:#ffbcc4; }
.toasts { position:fixed; right:22px; bottom:22px; z-index:60; display:flex; flex-direction:column; gap:9px; }
.toast { min-width:240px; max-width:400px; padding:11px 13px; border-radius:var(--r-md); font-size:13px;
  background:#161d2b; border:1px solid var(--line); box-shadow:var(--shadow); animation:fade .2s var(--tap); }
.toast.ok { border-color:rgba(61,220,151,.4); } .toast.err { border-color:rgba(255,107,129,.42); }
.seg { display:inline-flex; padding:3px; gap:3px; border-radius:999px; border:1px solid var(--line); background:#101623; }
.seg button { border:none; background:transparent; color:var(--muted); font-size:12.5px; padding:6px 12px;
  border-radius:999px; cursor:pointer; font-family:inherit; transition:background .16s var(--tap), color .16s var(--tap); }
.seg button.on { background:rgba(124,108,246,.22); color:#fff; }

/* ---- 对局监控：整张牌桌 ---- */
.board-card { margin-bottom:14px; }
/* 宽屏把两边**并排**：整场对局一次看完；窄屏再上下堆叠（那是手机的形状） */
.boards { display:grid; grid-template-columns:minmax(0,1fr); gap:10px; }
@media (min-width: 1180px) { .boards { grid-template-columns:minmax(0,1fr) minmax(0,1fr); } }
.board-card .bd { display:flex; flex-direction:column; gap:8px; }
.board-mid { text-align:center; font-size:11px; letter-spacing:3px; color:var(--faint); }
.side-board { border:1px solid var(--line-soft); border-radius:var(--r-md); padding:8px 10px;
  background:radial-gradient(600px 200px at 50% -40%, rgba(124,108,246,.10), transparent 70%), #101623;
  max-width:700px; margin:0 auto; width:100%; }
.side-board.theirs { background:radial-gradient(600px 200px at 50% -40%, rgba(255,107,129,.10), transparent 70%), #101623; }
.side-board.turn { border-color:rgba(61,220,151,.45); box-shadow:0 0 0 1px rgba(61,220,151,.18) inset; }
.side-hd { display:flex; align-items:center; gap:10px; margin-bottom:8px; font-size:12.5px; flex-wrap:wrap; }
.side-hd .who { color:var(--muted); }
.side-hd .lp { display:inline-flex; align-items:center; gap:5px; font-weight:650; font-variant-numeric:tabular-nums;
  color:#ffd9a6; font-size:14px; }
.side-hd .lp svg.i { width:14px; height:14px; color:var(--danger); }
/* 一行牌区：**固定格宽**（不是铺满）——两边牌桌要能同屏看，格子上限 112px，
   再大就成"一次只看得到半边"，监控页失去意义。 */
.boardrow { display:grid; grid-template-columns:repeat(5, minmax(0, 92px)); gap:4px;
  justify-content:center; margin-bottom:5px; }
.boardrow.ex { grid-template-columns:repeat(2, minmax(0, 92px)); }
.boardrow.low { grid-template-columns:repeat(4, minmax(0, 92px)); }
.slot { position:relative; aspect-ratio:59/86; max-height:132px; border-radius:8px; overflow:hidden;
  border:1px dashed #2a3346; background:rgba(255,255,255,.015); display:grid; place-items:center; }
.slot img { width:100%; height:100%; object-fit:cover; display:block; }
.slot.up { border-style:solid; border-color:#33405f; box-shadow:0 6px 16px -12px #000; }
.slot.up.spell { border-color:#2f5a4a; } .slot.up.trap { border-color:#5a4a2f; }
.slot.back { border-style:solid; border-color:#2f3a52; background:linear-gradient(150deg,#1b2233,#141a27); }
.slot.back .backface { color:#4a5670; } .slot.back svg.i { width:22px; height:22px; }
.slot.noart::after { content:"无图"; font-size:10px; color:var(--faint); }
/* 格位名（空位也写出来，让人看清这一格是干什么的）+ 卡名 + 攻守角标 */
.slot .zl { position:absolute; left:0; right:0; bottom:0; text-align:center; font-size:9px;
  color:var(--faint); background:rgba(8,11,17,.72); padding:1px 0; letter-spacing:.2px; }
.slot.empty .zl { position:static; background:none; }
.slot .nm2 { position:absolute; left:0; right:0; top:0; font-size:9.5px; line-height:1.25;
  color:#eaf0ff; background:rgba(8,11,17,.82); padding:1px 2px; max-height:26px; overflow:hidden; }
.slot .badge2 { position:absolute; right:2px; bottom:12px; font-size:9.5px; padding:0 3px; border-radius:4px;
  background:rgba(8,11,17,.85); border:1px solid #33405f; font-variant-numeric:tabular-nums; }
.slot .badge2.atk { color:#ffd9a6; } .slot .badge2.def { color:#9fc8ff; }
/* 内核下发的当前攻守（实心一点）与只有卡面数值时（写着"·卡面"）在视觉上分开 */
.slot .badge2.live { border-color:#4a6ea8; background:rgba(10,18,32,.95); }
.pile { border:1px solid var(--line-soft); border-radius:8px; background:#0d131f;
  display:flex; flex-direction:column; align-items:center; justify-content:center; gap:2px; }
.pile .k { font-size:10px; color:var(--faint); }
.pile b { font-size:17px; font-variant-numeric:tabular-nums; }
/* 台账：召唤/特召/发动/盖放/攻击/伤害… */
.ledger { display:flex; flex-wrap:wrap; gap:4px; margin-top:6px; padding-top:6px; border-top:1px solid var(--line-soft); }
.ledger .cell { display:inline-flex; align-items:center; gap:4px; font-size:11px; font-variant-numeric:tabular-nums;
  border:1px solid var(--line-soft); border-radius:999px; padding:1px 8px; color:#dbe3f5; }
.ledger .cell .k { color:var(--faint); }
.side-ft { display:flex; gap:6px; flex-wrap:wrap; }

/* ---- 训练台：对话流 + 吸底输入区（"游戏王专用的小 dsh"）---- */
.console { display:flex; flex-direction:column; gap:12px; }
.stream { display:flex; flex-direction:column; gap:14px; padding:16px 18px; overflow:auto;
  max-height:calc(100vh - 370px); min-height:200px; border-radius:var(--r-lg);
  border:1px solid var(--line); background:linear-gradient(180deg, var(--panel), var(--panel-2)); }
.msg { display:flex; flex-direction:column; gap:8px; }
.bubble { border-radius:14px; padding:10px 13px; font-size:13px; max-width:min(860px, 92%);
  border:1px solid var(--line); }
.bubble.me { align-self:flex-end; background:rgba(124,108,246,.14); border-color:rgba(124,108,246,.32); }
.bubble.me .txt { margin-top:6px; }
.bubble.bot { align-self:flex-start; background:#111826; }
.bubble.bot.failed { border-color:rgba(255,107,129,.4); }
.bubble.bot.done { border-color:rgba(61,220,151,.32); }
.bubble.bot.running { border-color:rgba(124,108,246,.45); background:rgba(124,108,246,.07); }
.bubble .rh { display:flex; align-items:center; gap:9px; }
.bubble .rh svg.i { width:15px; height:15px; color:var(--muted); }
.bubble .txt { margin-top:6px; white-space:pre-wrap; line-height:1.6; }
.bubble .at { font-size:11px; color:var(--faint); margin-top:5px; }
.bubble pre.log { margin-top:8px; max-height:220px; }
.deckworks { border:1px solid var(--line-soft); border-radius:var(--r-md); background:#111826; }
.worksrow { display:flex; align-items:center; gap:12px; padding:9px 12px; }
.worksrow .art.sm { width:44px; height:44px; flex:none; border-radius:9px; overflow:hidden;
  border:1px solid var(--line); background:#0b101a; display:grid; place-items:center; color:var(--faint); }
.worksrow .art.sm img { width:100%; height:100%; object-fit:cover; display:block; }
.worksrow .winfo { flex:1; min-width:0; }
.worksrow .nm { font-size:13px; font-weight:600; }
.worksrow .ln { display:flex; gap:6px; flex-wrap:wrap; margin-top:5px; }
.composer { border:1px solid var(--line); border-radius:var(--r-lg); padding:12px 14px 10px;
  background:linear-gradient(180deg, var(--panel-2), var(--panel)); box-shadow:var(--shadow); }
.bubble details.long { margin-top:6px; }
.bubble details.long > summary { cursor:pointer; color:var(--brand-2); font-size:12px; }
.bubble details.long .txt { margin-top:6px; color:var(--muted); }
.composer .crow2 { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
.composer .crow2 > .field, .composer .extra-inline > .field { flex:0 0 auto; min-width:170px; }
/* 下拉别被最长的卡组名撑宽：撑宽就换行，把「对手卡组」挤到第二行、落到视口外
   （实测被撑到 427px → 四五个字段排不下；用户看到的就是「对手卡组没有显示」）。 */
.composer .crow2 select, .composer .extra-inline select { min-width:190px; max-width:260px; }
.composer .crow2 .btn { padding:9px 18px; }
.composer .cinput { margin-top:10px; }
.composer .cinput textarea { resize:vertical; }
/* 随种类出现的字段（对手卡组 / 局数 / 轮数…）：**并排在同一行**，跟在「要写的东西」后面。
   ⚠ 原来它在 textarea 下面单独一行，真机上正好被挤到视口最底边（top=999px / 视口 1000px）——
   用户看到的就是"对手卡组没有显示"（其实渲染了 97 项，只是看不见）。 */
.composer .extra-inline { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
.composer .extra-inline:empty { display:none; }
.composer .faint { margin-top:8px; line-height:1.7; }

/* ---- 登录 ---- */
.login { min-height:100vh; display:grid; place-items:center; padding:24px; }
.login .box { width:100%; max-width:392px; padding:26px 26px 22px; border-radius:20px;
  background:linear-gradient(180deg, #141a27, #0e131d); border:1px solid var(--line); box-shadow:var(--shadow); }
.login .mark { width:46px; height:46px; border-radius:14px; margin-bottom:14px;
  background:linear-gradient(145deg, var(--brand), #4f46e5 62%, var(--cyan)); display:grid; place-items:center;
  color:#fff; box-shadow:0 10px 26px -12px rgba(124,108,246,1); }
.login .mark svg.i { width:24px; height:24px; stroke-width:1.8; }
.login h1 { font-size:19px; margin:0 0 6px; }
.login p { font-size:12.5px; color:var(--muted); margin:0 0 18px; }
.login .err { display:flex; gap:8px; align-items:center; font-size:12.5px; color:#ffbcc4;
  background:rgba(255,107,129,.1); border:1px solid rgba(255,107,129,.35); padding:9px 11px;
  border-radius:var(--r-sm); margin-bottom:14px; }
.login .hint { font-size:11.5px; color:var(--faint); margin-top:14px; line-height:1.6; }
.login .hint code { color:var(--muted); }

@media (max-width: 900px) {
  .shell { flex-direction:column; }
  .side { width:100%; height:auto; position:static; flex-direction:row; align-items:center;
    overflow-x:auto; padding:10px 12px; gap:6px; flex-wrap:nowrap; }
  .brand { padding:0 8px 0 0; gap:0; }
  /* 窄屏只留图标：标题字被挤成"一个字一行"比不显示更难认 */
  .brand > div { display:none; }
  .side nav { flex-direction:row; margin:0; gap:4px; flex-wrap:nowrap; }
  .navlink { white-space:nowrap; padding:8px 10px; }
  .navlink .n { display:none; }
  .side .foot { display:none; }
  .topbar, .wrap { padding-left:14px; padding-right:14px; }
  .cols-main, .cols-2 { grid-template-columns:minmax(0,1fr); }
  .deckgrid { grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); }
  .sheet { width:100vw; }
}
"""

_SCRIPT = """
const Key = { value: "" };
const State = { tab:"overview", decks:null, training:null, logTimer:null, trainTimer:null, logFilter:"", logLevel:"all" };
function $(id){ return document.getElementById(id); }
function esc(text){ return String(text==null?"":text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(text, kind){
  const box = $("toasts"); if (!box) return;
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 4200);
}
async function api(path){
  const res = await fetch(path, { headers: Key.value ? { "X-API-Key": Key.value } : {} });
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  return await res.json();
}
async function postApi(path, payload){
  const headers = { "Content-Type": "application/json" };
  if (Key.value) headers["X-API-Key"] = Key.value;
  const res = await fetch(path, { method:"POST", headers, body: JSON.stringify(payload || {}) });
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  return await res.json();
}
function art(cardId, cls){
  if (!cardId) return `<div class="${cls}">${ICON.back}</div>`;
  return `<div class="${cls}"><img loading="lazy" src="/api/art/${esc(cardId)}" alt=""
     onerror="this.parentNode.innerHTML='${ICON.back.replace(/'/g, "&#39;")}'"></div>`;
}
const ICON = {
  back: '<svg class="i" viewBox="0 0 24 24"><rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M8 8h8v8H8z"/></svg>',
  deck: '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="12" height="15" rx="2.2"/><path d="M8 3h10a2 2 0 0 1 2 2v13"/><path d="M7.5 11h4"/></svg>',
  train: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v3M6.5 5.5l2 2M17.5 5.5l-2 2"/><rect x="4" y="10" width="16" height="10" rx="3"/><path d="M9 15h6"/></svg>',
  logs: '<svg class="i" viewBox="0 0 24 24"><path d="M5 4h14v16H5z"/><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4"/></svg>',
  gear: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2.2M12 18.8V21M4.2 7.5l1.9 1.1M17.9 15.4l1.9 1.1M4.2 16.5l1.9-1.1M17.9 8.6l1.9-1.1"/></svg>',
  home: '<svg class="i" viewBox="0 0 24 24"><path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M10 20v-5h4v5"/></svg>',
  play: '<svg class="i" viewBox="0 0 24 24"><path d="M7 5l12 7-12 7z"/></svg>',
  stop: '<svg class="i" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  refresh: '<svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v4h-4"/></svg>',
  search: '<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="M15.5 15.5 20 20"/></svg>',
  warn: '<svg class="i" viewBox="0 0 24 24"><path d="M12 4.5 20.5 19h-17z"/><path d="M12 10v4M12 16.5v.01"/></svg>',
  info: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.01"/></svg>',
  chart: '<svg class="i" viewBox="0 0 24 24"><path d="M5 19V9M12 19V5M19 19v-7"/></svg>',
  spark: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/></svg>',
  key: '<svg class="i" viewBox="0 0 24 24"><circle cx="8.5" cy="14.5" r="3.5"/><path d="M11 12 20 3M17 6l2.5 2.5"/></svg>',
  check: '<svg class="i" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
};
function statusLabel(status){ return ({running:"跑着", done:"成功", failed:"失败", cancelled:"已停止"})[status] || status; }
function statusClass(status){ return ({running:"run", done:"ok", failed:"err", cancelled:"warn"})[status] || "dim"; }
function statusChip(status){ return `<span class="chip ${statusClass(status)}">${esc(statusLabel(status))}</span>`; }
function shortTime(stamp){ return String(stamp || "").slice(5); }

function tab(name){
  State.tab = name;
  document.querySelectorAll(".navlink").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("section.view").forEach(s => s.classList.toggle("active", s.id === "view-" + name));
  const titles = {overview:["总览","卡组池、房间与训练状态"],decks:["卡组","群友投稿与内置卡组"],
                  duel:["对局监控","进行中的牌桌（每 2 秒刷新）"],
                  training:["训练台","跟它说一句要做什么：卡组互打 / 编写脚本 / 卡组迭代 / 复盘优化"],
                  logs:["日志","宿主日志（可按关键词过滤）"]};
  const pair = titles[name] || ["面板",""];
  $("page-title").textContent = pair[0]; $("page-sub").textContent = pair[1];
  if (name === "overview") loadOverview();
  if (name === "decks") loadDecks();
  if (name === "duel") { loadDuel(); startDuelLive(); }
  if (name === "training") loadTraining();
  if (name === "logs") loadLogs();
  if (name !== "duel" && DUEL_TIMER) { clearInterval(DUEL_TIMER); DUEL_TIMER = null; }
}
function stopTimers(){
  if (State.logTimer) { clearInterval(State.logTimer); State.logTimer = null; }
  if (State.trainTimer) { clearInterval(State.trainTimer); State.trainTimer = null; }
}

/* ------------------------------ 总览 ------------------------------ */
async function loadOverview(){
  const d = await api("/api/status");
  if (!d.ok) return;
  const counts = d.training.counts || {};
  const running = counts.running || 0;
  const trainSub = [counts.done ? `成功 ${counts.done}` : "", counts.failed ? `失败 ${counts.failed}` : ""]
    .filter(Boolean).join("　") || "还没有记录";
  $("ov-stats").innerHTML = [
    ["已注册卡组", d.decks.total, ICON.deck, d.decks.groups + " 个来源"],
    ["随机池", d.decks.in_random, ICON.spark, "能随机抽到"],
    ["专属脚本", d.decks.with_script, ICON.gear, "自写执行器"],
    ["进行中房间", (d.rooms||[]).length, ICON.play, (d.rooms||[]).length ? "正在打" : "空闲"],
    ["训练任务", running ? running : (counts.done || 0), ICON.train, running ? "正在跑" : trainSub],
  ].map(([k,v,ic,sub]) => `<div class="stat"><div class="k">${ic}${esc(k)}</div>
      <div class="v">${esc(v)}</div><div class="s">${esc(sub)}</div></div>`).join("");

  const rooms = d.rooms || [];
  $("ov-rooms").innerHTML = rooms.length
    ? `<div class="rooms">${rooms.map(r => `<div class="room"><span class="dot"></span>
        <b>${esc(r.deck_name || "未记录卡组")}</b>
        <span class="faint">群 ${esc(r.group_id || "-")}</span>
        <span class="sp" style="flex:1"></span>
        <span class="mono faint">${esc(r.stream_id)}</span></div>`).join("")}</div>`
    : `<div class="empty">${ICON.play}<div>现在没有进行中的对局</div></div>`;

  const trainRows = Object.keys(counts).length
    ? Object.entries(counts).map(([k,v]) => `<span class="chip ${statusClass(k)}">${esc(statusLabel(k))} ${esc(v)}</span>`).join(" ")
    : `<span class="faint">还没有训练记录</span>`;
  $("ov-training").innerHTML = `<dl class="kv">
      <dt>状态</dt><dd>${d.training.enabled ? '<span class="chip ok">已启用</span>' : '<span class="chip dim">已关闭</span>'}</dd>
      <dt>记录</dt><dd style="display:flex;gap:6px;flex-wrap:wrap">${trainRows}</dd>
      <dt>训练模型</dt><dd><span class="chip brand">${esc(d.training.model)}</span></dd>
      <dt>单次擂台</dt><dd>最多 ${esc(d.training.max_duels_per_run)} 局</dd>
      <dt>工作目录</dt><dd class="mono faint">${esc(d.training.workspace)}</dd>
    </dl>`;

  $("ov-webui").innerHTML = `<dl class="kv">
      <dt>面板地址</dt><dd><a id="panel-url" href="${esc(d.webui.url)}" target="_blank" rel="noreferrer">${esc(d.webui.url)}</a></dd>
      <dt>监听</dt><dd class="mono">${esc(d.webui.host)}:${esc(d.webui.port)}</dd>
      <dt>密钥来源</dt><dd>${esc(d.webui.key_source)}</dd>
      <dt>密钥文件</dt><dd class="mono faint">${esc(d.webui.key_file || "（由配置或环境变量提供）")}</dd>
      <dt>启动于</dt><dd>${esc(d.webui.started_at)}</dd>
      <dt>数据目录</dt><dd class="mono faint">${esc(d.plugin.data_dir)}</dd>
    </dl>`;
  // 侧栏底部也写上面板地址：端口改了以后，用户第一眼要看的就是"现在到底在哪个端口"
  const side = $("side-url");
  if (side) { side.textContent = d.webui.url; side.href = d.webui.url; }
  loadRecentRuns();
}

async function loadRecentRuns(){
  const box = $("ov-runs");
  if (!box) return;
  const d = await api("/api/training");
  if (!d.ok) { box.innerHTML = `<div class="faint">读不到训练记录</div>`; return; }
  const runs = (d.runs || []).slice(0, 4);
  box.innerHTML = runs.length ? `<div class="runlist">${runs.map(r => `
      <div class="run" onclick="showRun('${esc(r.run_id)}')">
        <div class="sp"><div class="ttl">${esc(r.title)}</div>
          <div class="meta">${esc(r.started_at)}${r.duration_seconds==null?"":"　"+esc(r.duration_seconds)+"s"}</div></div>
        ${statusChip(r.status)}</div>`).join("")}</div>`
    : `<div class="faint">还没有训练记录——去训练页起一个 combo 推演试试。</div>`;
}

/* ------------------------------ 卡组 ------------------------------ */
async function loadDecks(){
  const d = await api("/api/decks");
  if (!d.ok) return;
  State.decks = d.groups || [];
  renderDecks("");
  $("deck-search").oninput = (event) => renderDecks(event.target.value.trim().toLowerCase());
}
function renderDecks(needle){
  const groups = State.decks || [];
  const hit = (deck) => !needle || String(deck.name).toLowerCase().includes(needle)
      || String(deck.deck_id) === needle || String(deck.style_now || "").toLowerCase().includes(needle);
  const html = groups.map(g => {
    const decks = (g.decks || []).filter(hit);
    if (!decks.length) return "";
    const title = g.group_id === "__builtin__" ? "内置卡组" : (g.group_id === "__optimized__" ? "调优产物" : "群 " + g.group_id);
    return `<div class="grp"><h3>${ICON.deck}${esc(title)}<span class="n">${decks.length} 副</span></h3>
      <div class="deckgrid">${decks.map(deckCard).join("")}</div></div>`;
  }).join("");
  $("decks-body").innerHTML = html || `<div class="empty">${ICON.search}<div>没有匹配的卡组</div></div>`;
}
const BRAIN_SCOPE_LABEL = {"": "跟随全局", off: "不问 AI", target_only: "只问目标", full: "目标+要不要交"};
function deckCard(deck){
  const chips = [];
  if (deck.generated_script) chips.push(`<span class="chip brand">脚本 ${esc(deck.generated_script)}</span>`);
  else if (deck.picked_style) chips.push(`<span class="chip">样式 ${esc(deck.picked_style)}</span>`);
  else if (deck.style_now) chips.push(`<span class="chip dim">${esc(deck.style_now)}</span>`);
  if (deck.file_missing) chips.push('<span class="chip err">卡表文件丢了</span>');
  const scope = deck.brain_scope || "";
  // 整张卡点开详情（一直如此，卡片本身的 cursor 就是 pointer）；下面那两个控件
  // 必须自己把点击吞掉——不吞的话点开关/下拉也会顺带打开详情抽屉。
  return `<div class="deck" onclick="showDeck('${esc(deck.deck_id)}','${esc(deck.group_id)}')">
    ${art(deck.head_card, "art")}
    <div class="meta">
      <div class="nm" title="${esc(deck.name)}">${esc(deck.name)}</div>
      <div class="ln">${chips.join("")}</div>
      <div class="cnt" title="编号与群里 /卡组列表 的编号一致">#${esc(deck.number)}　主 ${esc(deck.main)}·额 ${esc(deck.extra)}·副 ${esc(deck.side)}${deck.contributor ? "　by " + esc(deck.contributor) : ""}</div>
      <div class="deckctl" onclick="event.stopPropagation()">
        <label class="switch sm" title="加入/移出随机池">
          <input type="checkbox" ${deck.in_random ? "checked" : ""}
            onchange="deckToggle('${esc(deck.deck_id)}','${esc(deck.group_id)}','random', this.checked)">
          <span class="sw-text">随机池</span></label>
        <label class="field mini"><span class="muted">AI 决策</span>
          <select onchange="deckToggle('${esc(deck.deck_id)}','${esc(deck.group_id)}','brain', this.value)">
            ${Object.entries(BRAIN_SCOPE_LABEL).map(([value, text]) =>
              `<option value="${esc(value)}" ${scope === value ? "selected" : ""}>${esc(text)}</option>`).join("")}
          </select></label>
      </div>
    </div></div>`;
}
async function deckToggle(deckId, group, what, value){
  const payload = { group_id: group };
  if (what === "random") payload.in_random = !!value;
  else payload.brain_scope = String(value);
  const d = await postApi(`/api/deck/${deckId}/settings`, payload);
  if (!d.ok) { toast(d.error || "改不了", "err"); loadDecks(); return; }
  toast(d.message, "ok");
  loadDecks();
}
function openSheet(){ $("deck-sheet").classList.add("on"); $("sheet-mask").classList.add("on"); }
function closeSheet(){ $("deck-sheet").classList.remove("on"); $("sheet-mask").classList.remove("on"); }
async function showDeck(deckId, group){
  $("sheet-title").textContent = "载入中…";
  $("sheet-body").innerHTML = "";
  openSheet();
  const d = await api(`/api/deck/${encodeURIComponent(deckId)}?group=${encodeURIComponent(group)}`);
  if (!d.ok) { $("sheet-title").textContent = "打不开"; $("sheet-body").innerHTML = `<div class="banner err">${ICON.warn}${esc(d.error)}</div>`; return; }
  const deck = d.deck;
  const chips = [
    deck.in_random ? '<span class="chip ok">随机池</span>' : '<span class="chip dim">不进随机</span>',
    deck.generated_script ? `<span class="chip brand">脚本 ${esc(deck.generated_script)}</span>` : "",
    deck.picked_style ? `<span class="chip">挑样式 ${esc(deck.picked_style)}</span>` : "",
    deck.contributor ? `<span class="chip">投稿 ${esc(deck.contributor)}</span>` : "",
  ].join(" ");
  const zone = (label, key) => {
    const rows = (deck.entries && deck.entries[key]) || [];
    if (!rows.length) return "";
    const total = (deck.counts && deck.counts[key]) || rows.length;
    return `<div class="zone"><h4>${esc(label)}<span class="n">${esc(total)} 张 / ${rows.length} 种</span></h4>
      <div class="cardrow">${rows.map(c => `<div class="crow">
        ${art(c.id, "thumb")}<div class="nm" title="${esc(c.name)}">${esc(c.name)}</div>
        <div class="ct">×${esc(c.count)}</div></div>`).join("")}</div></div>`;
  };
  $("sheet-title").textContent = deck.name || `卡组 #${deck.number}`;
  $("sheet-body").innerHTML = `
    <div class="hero">${art(deck.head_card, "art")}
      <div style="min-width:0"><div class="ln" style="display:flex;gap:6px;flex-wrap:wrap">${chips}</div>
        <div class="faint mono ell" style="margin-top:8px;font-size:11.5px" title="${esc(deck.ydk_path)}">#${esc(deck.number)}　${esc(deck.ydk_path)}</div></div></div>
    <div class="toolbar" style="margin-top:4px">
      <button class="btn sm" onclick="deckRandom('${esc(deck.deck_id)}','${esc(deck.group_id)}',${deck.in_random ? "false" : "true"})">
        ${deck.in_random ? "移出随机池" : "加入随机池"}</button>
      <label class="field mini"><span class="muted">AI 决策</span>
        <select onchange="deckToggle('${esc(deck.deck_id)}','${esc(deck.group_id)}','brain', this.value)">
          ${Object.entries(BRAIN_SCOPE_LABEL).map(([value, text]) =>
            `<option value="${esc(value)}" ${(deck.brain_scope || "") === value ? "selected" : ""}>${esc(text)}</option>`).join("")}
        </select></label>
      <button class="btn danger sm" onclick="deckDelete('${esc(deck.deck_id)}','${esc(deck.group_id)}','${esc(deck.name)}')">删除卡组</button>
    </div>
    ${deck.error ? `<div class="banner warn">${ICON.warn}${esc(deck.error)}</div>` : ""}
    ${zone("主卡组", "main")}${zone("额外卡组", "extra")}${zone("副卡组", "side")}`;
}
async function deckRandom(deckId, group, want){
  const d = await postApi(`/api/deck/${deckId}/random`, { in_random: want, group_id: group });
  if (!d.ok) { toast(d.error || "操作失败", "err"); return; }
  toast(d.message, "ok");
  loadDecks();
  showDeck(deckId, group);
}
async function deckDelete(deckId, group, name){
  if (!confirm(`删除卡组「${name}」#${deckId}？\\n\\n它的 .ydk 文件会一起删掉，这个动作不可撤销。\\n（内置卡组删不掉，只能移出随机池）`)) return;
  const d = await postApi(`/api/deck/${deckId}/delete`, { confirm: true, group_id: group });
  if (!d.ok) { toast(d.error || "删除失败", "err"); return; }
  toast(d.message, "ok");
  closeSheet();
  loadDecks();
}

/* ------------------------------ 对局监控 ------------------------------ */
let DUEL_TIMER = null;
function startDuelLive(){
  const box = $("duel-live");
  if (DUEL_TIMER) { clearInterval(DUEL_TIMER); DUEL_TIMER = null; }
  if (box && box.checked) DUEL_TIMER = setInterval(() => { if (State.tab === "duel") loadDuel(); }, 2000);
}
async function loadDuel(){
  const d = await api("/api/rooms");
  if (!d.ok) { $("duel-body").innerHTML = `<div class="banner err">${ICON.warn}${esc(d.error)}</div>`; return; }
  const rooms = d.rooms || [];
  if (!rooms.length) {
    $("duel-body").innerHTML = `<div class="empty">${ICON.play}<div>现在没有本插件开的房间</div>
      <div class="faint" style="font-size:12px;max-width:520px;text-align:center;line-height:1.8">
        这一页只显示**群里开给群友打的那种房间**（有人在里面跟机器人对局）。<br>
        擂台、A/B 测试、体检跑起来的那些 ygopro / WindBot 进程不在这一页
        （它们不走房间口令、也没有播报）——那些去看<a href="#" onclick="tab('training');return false;">训练页</a>的任务记录。<br>
        群里有人说想打牌（或 <span class="mono">/开房</span>）之后，这里会实时显示牌桌。</div></div>`;
    return;
  }
  const notes = (d.unknown || []).map(item => `<li>${esc(item)}</li>`).join("");
  $("duel-body").innerHTML = rooms.map(roomBoard).join("")
    + (notes ? `<div class="faint" style="font-size:11.5px;line-height:1.8">
        <b>内核不给、这里也没有的：</b><ul style="margin:4px 0 0;padding-left:20px">${notes}</ul></div>` : "");
}
function cardSlot(card, label){
  /* 一格：表侧给卡图 + 卡名 + 攻守角标；里侧只画卡背（不公开卡号），但**要看得见**。
     label 是格位名（主怪兽区3 / 额外怪兽区 / 场地区 / 灵摆区左…），空位也标出来，
     这样"这一格是干什么的"一眼就能看见，不会以为是排版歪了。 */
  if (!card) return `<div class="slot empty"><span class="zl">${esc(label)}</span></div>`;
  if (!card.face_up) {
    return `<div class="slot back" title="${esc(label)}：里侧表示（不公开卡号）">
      <div class="backface">${ICON.back}</div>
      <span class="badge2 ${card.attack ? "atk" : "def"}">里侧 · ${card.attack ? "攻" : "守"}</span>
      <span class="zl">${esc(label)}</span></div>`;
  }
  const known = (card.atk != null || card.def_ != null);
  // 攻守角标：内核给过当前值就打实心（`live`），只有卡面数值时标一下"卡面"——
  // 装备/场地加成之后这两个数会不一样，不写清楚会让人以为面板算错了
  const stats = known
    ? `<span class="badge2 ${card.attack ? "atk" : "def"}${card.stats_live ? " live" : ""}"
        title="${card.stats_live ? "内核下发的当前数值" : "卡面数值（内核还没下发当前值）"}">
        ${card.attack ? "攻" : "守"} ${esc(card.atk ?? "?")}${card.def_ != null ? "/" + esc(card.def_) : ""}${
        card.stats_live ? "" : "·卡面"}</span>`
    : "";
  return `<div class="slot up ${esc(card.kind || "")}" title="${esc(card.name || ("卡号 " + card.id))}（${esc(label)}）｜卡号 ${esc(card.id)}">
    <img loading="lazy" src="/api/art/${esc(card.id)}" alt=""
      onerror="this.parentNode.classList.add('noart');this.remove()">
    <span class="nm2">${esc(card.name || ("#" + card.id))}</span>${stats}<span class="zl">${esc(label)}</span></div>`;
}
function sideBoard(side){
  /* 一侧的**完整**牌桌：额外怪兽区 2 + 主怪兽区 5 / 魔陷区 5 / 场地区 / 灵摆区 2 + 三堆计数。
     位置与内核的位号一一对应（与 /查房 出图同一套）。 */
  const piles = side.piles || {};
  return `<div class="side-board ${side.is_self ? "ours" : "theirs"} ${side.is_turn ? "turn" : ""}">
    <div class="side-hd">
      <span class="who">${side.is_self ? "我方" : "对手"}${side.name ? " · " + esc(side.name) : ""}</span>
      ${side.deck ? `<span class="chip dim">${esc(side.deck)}</span>` : ""}
      ${side.is_turn ? '<span class="chip ok">该它动</span>' : ""}
      <span style="flex:1"></span>
      <span class="lp"><svg class="i" viewBox="0 0 24 24"><path d="M12 20s-7-4.4-7-9.3A4 4 0 0 1 12 8a4 4 0 0 1 7 2.7C19 15.6 12 20 12 20z"/></svg>${esc(side.lp)}</span>
    </div>
    <div class="boardrow ex">${(side.extra_monsters || []).map((c, i) => cardSlot(c, "额外怪兽区" + (i + 1))).join("")}</div>
    <div class="boardrow">${(side.monsters || []).map((c, i) => cardSlot(c, "怪兽区" + (i + 1))).join("")}</div>
    <div class="boardrow">${(side.spells || []).map((c, i) =>
      cardSlot(c, (side.spell_pendulum && side.spell_pendulum[i] ? "魔陷区" + (i + 1) + "·灵摆" : "魔陷区" + (i + 1)))).join("")}</div>
    <div class="boardrow low">
      ${cardSlot(side.field_zone, "场地魔法")}
      <div class="pile"><span class="k">墓地</span><b>${esc(piles.grave ?? 0)}</b></div>
      <div class="pile"><span class="k">除外</span><b>${esc(piles.banished ?? 0)}</b></div>
      <div class="pile"><span class="k">额外</span><b>${esc(piles.extra ?? 0)}</b></div>
    </div>
    ${ledgerRow(side.stats)}
  </div>`;
}
function ledgerRow(stats){
  /* 这一局的台账（记录器数出来的）：召唤/特召/发动/盖放/攻击/伤害…，没有就不显示 */
  if (!stats || !Object.keys(stats).length) return "";
  const items = [
    ["召唤", stats.normal_summons], ["特召", stats.sp_summons], ["发动", stats.effects],
    ["盖放", stats.sets], ["抽牌", stats.draws], ["攻击", stats.attacks],
    ["直击", stats.direct_attacks], ["打伤", stats.damage_dealt], ["挨打", stats.damage_taken],
    ["最大一击", stats.biggest_hit_taken], ["回复", stats.lp_recovered],
    ["送墓", stats.sent_to_grave], ["除外", stats.banished],
  ];
  return `<div class="ledger">${items.map(([k, v]) => `<span class="cell"><span class="k">${esc(k)}</span>${esc(v ?? 0)}</span>`).join("")}</div>`;
}
function roomBoard(room){
  const state = room.started ? (room.finished ? '<span class="chip warn">已结束</span>' : '<span class="chip ok">对局中</span>')
    : '<span class="chip run">等人进房</span>';
  const sides = room.sides || [];
  const ours = sides.find(s => s.is_self) || sides[0];
  const theirs = sides.find(s => !s.is_self) || sides[1];
  return `<div class="panel board-card"><div class="hd">
      <h3>${esc(room.deck_name || "未记录卡组")}</h3>
      <span class="chip dim">群 ${esc(room.group_id || "-")}</span>
      ${state}
      <span class="chip">第 ${esc(room.turn || 0)} 回合${room.phase ? " · " + esc(room.phase) : ""}</span>
      <span style="flex:1"></span>
      <span class="faint mono" style="font-size:11px">${esc(room.stream_id)}</span>
    </div>
    <div class="bd">
      ${room.error ? `<div class="banner warn">${ICON.warn}${esc(room.error)}</div>` : ""}
      <div class="boards">
        ${theirs ? sideBoard(theirs) : ""}
        ${ours ? sideBoard(ours) : ""}
      </div>
    </div></div>`;
}

/* ------------------------------ 训练 ------------------------------ */
/* 训练台：做成一个"游戏王专用的小 dsh"——上面是对话流（每条 = 一次任务：我要求的 + 它的结果），
   下面是吸底的输入区（选卡组 → 选要写的东西 → 补充要求 → 开始）。 */
/* 面板上只有这四件事（2026-10-09 用户口径）。推演 combo、脚本体检、读录像都降级成
   这些任务内部的步骤，不再让用户先想"我该跑哪一个"。 */
const KIND_HINT = {
  arena: "两副牌各自用自己那份脚本对打，逐局交替座位——看谁的牌组+脚本更硬。",
  write_script: "读卡文给这副牌写一份 C# 出牌脚本并编译：**每张卡一个处理函数**，分批写（一批 8 张，一次调用一批），所以第一次就会写足量。已有脚本时是在它基础上改。会改动 WindBot 源码树。",
  iterate: "推演 → 写脚本 → 跟另一副牌打 → 让模型对着**脚本源码 + 逐局战况**说下一版改哪几个函数 → 再改一轮。**会真打牌，慢**。",
  review: "读这副牌最近打过的对局记录，指出具体该改哪里（只看不改，不动任何文件）。",
};
const KIND_LABEL = {
  arena: "卡组互打", write_script: "编写脚本", iterate: "卡组迭代", review: "复盘优化",
};
let KIND_META = {};

async function loadTraining(){
  const d = await api("/api/training");
  if (!d.ok) { $("training-stream").innerHTML = `<div class="banner err">${ICON.warn}${esc(d.error||"读不到训练数据")}</div>`; return; }
  const banners = [];
  if (!d.enabled) banners.push(`<div class="banner warn">${ICON.info}训练功能在配置里关着（<span class="mono">training.enabled = false</span>）：这一页只能看历史记录。</div>`);
  if (d.enabled && !d.bridge_ready) banners.push(`<div class="banner err">${ICON.warn}训练执行器没建起来——看插件日志里"训练功能"那一行找原因。</div>`);
  $("training-banners").innerHTML = banners.join("");
  KIND_META = {};
  (d.kinds || []).forEach(k => { KIND_META[k.kind] = k; });

  const active = d.active;
  ensureComposer(d, active);

  const runs = (d.runs || []).slice().reverse();   // 对话流：旧的在上、新的在下
  const stream = $("training-stream");
  const stick = nearBottom(stream);
  const blocks = runs.map(runExchange);
  if (active) blocks.push(activeExchange(active));
  stream.innerHTML = blocks.length ? blocks.join("")
    : `<div class="empty">${ICON.train}<div>还没有任务</div>
       <div class="faint" style="font-size:12px">选一副卡组、挑一件要做的事，然后在下面写一句要求就能开始</div></div>`;
  if (stick) stream.scrollTop = stream.scrollHeight;

  if (active && !State.trainTimer) State.trainTimer = setInterval(() => { if (State.tab === "training") loadTraining(); }, 4000);
  if (!active && State.trainTimer) { clearInterval(State.trainTimer); State.trainTimer = null; }
}
function nearBottom(el){ return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }
function longText(text){
  /* 长文本折叠：训练台里经常有人贴一整段打法说明，直接铺开会把对话流刷得没法看 */
  const body = esc(text);
  if (String(text).length <= 320) return `<div class="txt">${body}</div>`;
  return `<details class="long"><summary>展开我写的要求（${esc(String(text).length)} 字）</summary>
      <div class="txt">${body}</div></details>`;
}
function runExchange(run){
  const summary = run.summary || {};
  const params = run.params || {};
  const ask = [
    `<span class="chip brand">${esc(KIND_LABEL[run.kind] || run.kind_title)}</span>`,
    params.deck_name ? `<span class="chip">${esc(params.deck_name)}</span>` : "",
    params.rounds ? `<span class="chip dim">${esc(params.rounds)} 轮</span>` : "",
    params.duels ? `<span class="chip dim">${esc(params.duels)} 局</span>` : "",
  ].filter(Boolean).join(" ");
  const reply = summary.conclusion ? esc(String(summary.conclusion))
    : summary.guide_path ? "推演已存档，点开看全文。"
    : summary.style_name ? `出牌脚本 ${esc(summary.style_name)} 已编译通过（第 ${esc(summary.attempts || "?")} 轮）。`
    : summary.exit_code === 0 ? "跑完了，没有结论（点开看输出尾巴）。"
    : (run.error ? esc(String(run.error)) : "（没有输出）");
  // 失败原因（编译器输出、日志尾巴）经常很长：对话流里折起来，详情抽屉里看全文
  const replyHtml = reply.length > 400
    ? `<details class="long"><summary>展开详情（${esc(reply.length)} 字）</summary><div class="txt">${reply}</div></details>`
    : `<div class="txt">${reply}</div>`;
  return `<div class="msg">
      <div class="bubble me">${ask}${params.extra_prompt ? longText(params.extra_prompt) : ""}
        <div class="at">${esc(run.started_at || "")}${run.duration_seconds==null?"":"　"+esc(run.duration_seconds)+"s"}</div></div>
      <div class="bubble bot ${run.status}">
        <div class="rh">${KIND_MARK(run.status)}<b>${esc(run.title)}</b>${statusChip(run.status)}
          <span style="flex:1"></span>
          <a href="#" onclick="showRun('${esc(run.run_id)}');return false;">详情</a></div>
        ${replyHtml}</div>
    </div>`;
}
function activeExchange(run){
  return `<div class="msg">
      <div class="bubble me">${esc(KIND_LABEL[run.kind] || run.kind_title)}　<span class="at">${esc(run.started_at)}</span></div>
      <div class="bubble bot running">
        <div class="rh"><span class="spin"></span><b>${esc(run.title)}</b>${statusChip(run.status)}
          <span style="flex:1"></span>
          <button class="btn danger sm" onclick="stopTraining()">${ICON.stop}停止</button></div>
        <pre class="log" id="live-log">${esc((run.tail||[]).join("\\n")) || "（还没有输出）"}</pre></div>
    </div>`;
}
function KIND_MARK(status){
  return status === "done" ? ICON.check : status === "failed" ? ICON.warn : ICON.info;
}
function ensureComposer(d, active){
  const deckSel = $("c-deck");
  const kindSel = $("c-kind");
  if (deckSel.options.length !== (d.decks || []).length) {
    const keep = deckSel.value;
    State.decks = d.decks || [];
    deckSel.innerHTML = State.decks.map(x =>
      `<option value="${esc(x.deck_id)}">${x.is_builtin?"[内置] ":""}${esc(x.name)}（#${esc(x.number)}）${x.script ? "｜脚本 " + esc(x.script) : "｜还没脚本"}</option>`).join("");
    if (keep) deckSel.value = keep;
  }
  if (!kindSel.options.length) {
    kindSel.innerHTML = (d.kinds||[]).map(k => `<option value="${esc(k.kind)}">${esc(k.title)}</option>`).join("");
  }
  kindSel.disabled = !!active || !d.bridge_ready;
  $("btn-send").disabled = !!active || !d.bridge_ready;
  $("btn-send").textContent = active ? "有任务在跑" : "开始";
  deckSel.onchange = () => { loadDeckWorks(); renderExtraFields(); };
  onKindChange();
  renderExtraFields(active);
  loadDeckWorks();
}
function onKindChange(){
  const kind = $("c-kind").value;
  const meta = KIND_META[kind] || {};
  const hint = KIND_HINT[kind] || meta.note || "";
  const blocked = meta.ready === false;
  $("c-hint").innerHTML = (blocked ? `<span style="color:#ffd39a">现在不能跑：${esc(meta.note || "")}</span><br>` : "")
    + esc(hint) + (meta.needs_engine ? '　<span class="chip warn">会起对局</span>' : "");
  renderExtraFields();
}
function renderExtraFields(active){
  const kind = $("c-kind").value;
  const box = $("c-extra");
  const needOpponent = kind === "arena" || kind === "iterate";
  const mine = $("c-deck").value;
  const options = (State.decks || [])
    .filter(x => String(x.deck_id) !== String(mine))
    .map(x => `<option value="${esc(x.deck_id)}">${x.is_builtin ? "[内置] " : ""}${esc(x.name)}（#${esc(x.number)}）${x.script ? "｜脚本 " + esc(x.script) : "｜还没脚本"}</option>`)
    .join("");
  let html = "";
  if (needOpponent) {
    html += `<label class="field"><span>对手卡组（用它自己的脚本打）</span>
      <select id="c-opponent">${options || '<option value="">（池子里没有别的卡组）</option>'}</select></label>`;
  }
  if (kind === "arena") {
    html += `<label class="field"><span>局数（逐局交替座位）</span>
      <input id="c-duels" type="number" value="60" min="2"></label>`;
  }
  if (kind === "iterate") {
    html += `<label class="field"><span>迭代轮数</span><input id="c-rounds" type="number" value="2" min="1" max="5"></label>`;
    html += `<label class="field"><span>每轮局数</span><input id="c-duels" type="number" value="20" min="2"></label>`;
  }
  if (kind === "write_script") {
    html += `<label class="field"><span>生成→编译轮数</span><input id="c-rounds" type="number" value="3" min="1" max="6"></label>`;
  }
  if (kind === "review") {
    html += `<label class="field"><span>复盘最近几局</span><input id="c-latest" type="number" value="3" min="1" max="20"></label>`;
  }
  // 直接放进那一行（外层 .crow2 已经是 flex，这里再套一层会让宽度规则失效）
  box.innerHTML = html;
}
async function loadDeckWorks(){
  const deckId = $("c-deck").value;
  const box = $("deck-works");
  if (!deckId) { box.innerHTML = ""; return; }
  const d = await api(`/api/deck/${encodeURIComponent(deckId)}/workspace`);
  if (!d.ok) { box.innerHTML = `<div class="banner warn">${ICON.warn}${esc(d.error || "读不到这副牌的情况")}</div>`; return; }
  const deck = d.deck || {};
  const combos = d.combos || [];
  box.innerHTML = `<div class="worksrow">
      ${art(deck.head_card, "art sm")}
      <div class="winfo">
        <div class="nm">${esc(deck.name)} <span class="faint mono" style="font-size:11px">#${esc(deck.number)}</span></div>
        <div class="ln">
          ${deck.script ? `<span class="chip brand">脚本 ${esc(deck.script)}</span>` : '<span class="chip dim">还没写过脚本</span>'}
          ${deck.picked_style ? `<span class="chip">挑样式 ${esc(deck.picked_style)}</span>` : ""}
          ${deck.in_random ? '<span class="chip ok">随机池</span>' : ""}
          ${combos.length ? `<span class="chip">推演 ${combos.length} 份</span>` : ""}
        </div>
        ${d.latest && d.latest.conclusion ? `<div class="faint" style="font-size:11.5px;margin-top:5px">上次${esc(d.latest.kind_title)}：${esc(String(d.latest.conclusion).slice(0, 120))}</div>` : ""}
      </div>
      ${combos.length ? `<a class="btn sm" href="#" onclick="showCombo('${encodeURIComponent(combos[0].name)}');return false;">看最近推演</a>` : ""}
    </div>`;
}
async function showCombo(fileName){
  const deckId = $("c-deck").value;
  const d = await api(`/api/deck/${encodeURIComponent(deckId)}/workspace`);
  if (!d.ok) { toast(d.error || "读不到推演", "err"); return; }
  $("sheet-title").innerHTML = `${esc(d.deck.name)}　<span class="chip brand">推演存档</span>`;
  $("sheet-body").innerHTML = `<pre class="log">${esc(d.combo_preview || "（还没有推演存档）")}</pre>
    <p class="faint" style="font-size:11.5px">这份推演的完整文件：<span class="mono">${esc((d.combos[0]||{}).name || "")}</span></p>`;
  openSheet();
}
async function sendTask(){
  const kind = $("c-kind").value;
  const payload = { kind: kind, deck_id: Number($("c-deck").value) };
  const text = $("c-text").value.trim();
  if (text) payload.extra_prompt = text;
  const num = (id) => { const el = $(id); return el && el.value !== "" ? Number(el.value) : undefined; };
  const opponent = $("c-opponent") ? Number($("c-opponent").value) : 0;
  if (opponent) payload.opponent_deck_id = opponent;
  if (kind === "arena") payload.duels = num("c-duels") ?? 60;
  if (kind === "iterate") { payload.rounds = num("c-rounds") ?? 2; payload.duels = num("c-duels") ?? 20; }
  if (kind === "write_script") payload.rounds = num("c-rounds") ?? 3;
  if (kind === "review") payload.latest = num("c-latest") ?? 3;

  const d = await postApi("/api/training/start", payload);
  if (!d.ok) { toast(d.error || "起不来", "err"); return; }
  $("c-text").value = "";
  toast(`已开始：${d.run.title}`, "ok");
  loadTraining();
}
async function stopTraining(){
  const d = await postApi("/api/training/stop", {});
  if (!d.ok) { toast(d.error || "停不下来", "err"); return; }
  toast(d.stopped ? "已发出停止信号" : "没有在跑的任务", "ok");
  loadTraining();
}
async function showRun(runId){
  const d = await api(`/api/training/run/${encodeURIComponent(runId)}`);
  if (!d.ok) { toast(d.error || "打不开这条记录", "err"); return; }
  const run = d.run, summary = run.summary || {};
  const parts = [];
  if (run.error) parts.push(`<div class="banner err">${ICON.warn}${esc(run.error)}</div>`);
  if (summary.style_name) parts.push(`<dl class="kv" style="margin-bottom:10px">
      <dt>出牌脚本</dt><dd><span class="chip brand">${esc(summary.style_name)}</span>
        ${summary.attempts ? `<span class="chip dim">第 ${esc(summary.attempts)} 轮编译通过</span>` : ""}</dd>
      <dt>源文件</dt><dd class="faint mono ell" title="${esc(summary.file_path || "")}">${esc(summary.file_path || "")}</dd>
      ${summary.exe ? `<dt>编译产物</dt><dd class="faint mono ell" title="${esc(summary.exe)}">${esc(summary.exe)}</dd>` : ""}
      <dt>用了 combo</dt><dd>${summary.combo_used ? "是（按推演的顺序写脚本）" : "没有（直接按卡文写）"}</dd>
    </dl>`);
  if (summary.baseline) parts.push(`<p class="faint" style="font-size:12px">对照脚本：<span class="mono">${esc(summary.baseline)}</span>　共 ${esc(summary.rounds || 0)} 轮</p>`);
  const history = summary.history || [];
  if (history.length) {
    parts.push(`<h4 style="margin:8px 0 6px;font-size:13.5px">逐轮记录</h4>`);
    parts.push(history.map(item => `<div class="round">
        <div class="rh"><b>第 ${esc(item.round)} 轮</b>
          ${item.style_name ? `<span class="chip brand">${esc(item.style_name)}</span>` : ""}
          ${item.arena_exit_code != null ? `<span class="chip ${item.arena_exit_code === 0 ? "ok" : "err"}">擂台退出码 ${esc(item.arena_exit_code)}</span>` : ""}
          ${item.compared === false ? '<span class="chip dim">本轮无对照</span>' : ""}
        </div>
        <div class="faint" style="font-size:11.5px">combo 主线 ${esc(item.combo_lines ?? 0)} 条
          ${item.guide_path ? `　推演 ${esc(String(item.guide_path).split("\\\\").pop())}` : ""}
          ${item.arena_log ? `　擂台日志 ${esc(String(item.arena_log).split("\\\\").pop())}` : ""}</div>
        ${item.conclusion ? `<p style="margin:6px 0 0;font-size:12.5px">${esc(item.conclusion)}</p>` : ""}
        ${item.conclusion_error ? `<div class="banner warn" style="margin:6px 0 0">${ICON.info}结论没生成：${esc(item.conclusion_error)}</div>` : ""}
        ${(item.combo_warnings || []).length ? `<ul style="margin:6px 0 0;padding-left:20px;font-size:12px">${item.combo_warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      </div>`).join(""));
  }
  if (summary.conclusion) parts.push(`<h4 style="margin:14px 0 6px;font-size:13.5px">结论</h4><p style="margin:0">${esc(summary.conclusion)}</p>`);
  if (summary.conclusion_error) parts.push(`<div class="banner warn">${ICON.info}结论没生成：${esc(summary.conclusion_error)}</div>`);
  const warnings = summary.warnings || [];
  if (warnings.length) parts.push(`<h4 style="margin:14px 0 6px;font-size:13.5px">校验疑点</h4>
      <ul style="margin:0;padding-left:20px;font-size:12.5px">${warnings.map(w=>`<li>${esc(w)}</li>`).join("")}</ul>`);
  if (summary.guide_path) parts.push(`<p class="faint mono ell" style="font-size:11.5px" title="${esc(summary.guide_path)}">推演存档：${esc(summary.guide_path)}</p>`);
  if (summary.guide_text) parts.push(`<h4 style="margin:14px 0 6px;font-size:13.5px">推演笔记</h4>
      <pre class="log">${esc(summary.guide_text)}</pre>`);
  parts.push(`<h4 style="margin:14px 0 6px;font-size:13.5px">输出尾巴</h4>
      <pre class="log">${esc((run.tail||[]).join("\\n")) || "（这类任务没有子进程输出——看上面的推演笔记/结论）"}</pre>`);
  $("sheet-title").innerHTML = `${esc(run.title)} ${statusChip(run.status)}`;
  $("sheet-body").innerHTML = `<div class="faint mono" style="font-size:11.5px;margin-bottom:10px">${esc(JSON.stringify(run.params))}　起 ${esc(run.started_at)}${run.duration_seconds==null?"":"　耗时 "+esc(run.duration_seconds)+"s"}</div>${parts.join("")}`;
  openSheet();
}
async function stopTraining(){
  const d = await postApi("/api/training/stop", {});
  if (!d.ok) { toast(d.error || "停不下来", "err"); return; }
  toast(d.stopped ? "已发出停止信号" : "没有在跑的任务", "ok");
  loadTraining();
}

/* ------------------------------ 日志 ------------------------------ */
function levelOf(line){
  const m = line.match(/\\[(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\\]/);
  if (!m) return "info";
  const lv = m[1];
  if (lv === "ERROR" || lv === "CRITICAL") return "error";
  if (lv === "WARNING" || lv === "WARN") return "warning";
  if (lv === "DEBUG" || lv === "TRACE") return "debug";
  return "info";
}
function paintLogs(lines){
  const keep = [];
  for (const line of lines) {
    const lv = levelOf(line);
    if (State.logLevel === "error" && lv !== "error") continue;
    if (State.logLevel === "warning" && lv !== "error" && lv !== "warning") continue;
    keep.push(`<span class="lv-${lv}">${esc(line)}</span>`);
  }
  $("log-pre").innerHTML = keep.length ? keep.join("\\n") : "（没有匹配的日志）";
}
async function loadLogs(){
  const lines = $("log-lines").value || 200;
  const keyword = $("log-filter").value || "";
  const d = await api(`/api/logs?lines=${encodeURIComponent(lines)}&filter=${encodeURIComponent(keyword)}`);
  if (!d.ok) { $("log-pre").textContent = d.error; return; }
  paintLogs(d.lines || []);
}
function setLevel(level){
  State.logLevel = level;
  document.querySelectorAll("#log-levels button").forEach(b => b.classList.toggle("on", b.dataset.level === level));
  loadLogs();
}

/* ------------------------------ 启动 ------------------------------ */
document.addEventListener("DOMContentLoaded", async () => {
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("key");
  if (fromQuery) Key.value = fromQuery;
  document.querySelectorAll(".navlink").forEach(b => b.onclick = () => tab(b.dataset.tab));
  if ($("btn-logs-refresh")) $("btn-logs-refresh").onclick = loadLogs;
  if ($("btn-logs-live")) $("btn-logs-live").onchange = (event) => {
    if (State.logTimer) { clearInterval(State.logTimer); State.logTimer = null; }
    if (event.target.checked) State.logTimer = setInterval(() => { if (State.tab === "logs") loadLogs(); }, 4000);
  };
  if ($("btn-duel-refresh")) $("btn-duel-refresh").onclick = loadDuel;
  if ($("duel-live")) $("duel-live").onchange = startDuelLive;
  if ($("btn-overview-refresh")) $("btn-overview-refresh").onclick = loadOverview;
  if ($("log-filter")) $("log-filter").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLogs(); });
  if ($("deck-search")) { /* 卡组页载入后再挂，见 loadDecks */ }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
  tab("overview");
});
"""

def _icon_sprite() -> str:
    """把 JS 里那套图标定义搬进页面（`ICON` 是 JS 常量，登录页只用得到其中两个）。

    图标全部内联成 SVG，**不引任何外网资源**：面板在断网的机器上也要能正常打开。
    """

    return (
        '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="12" height="15" rx="2.2"/>'
        '<path d="M8 3h10a2 2 0 0 1 2 2v13"/><path d="M7.5 11h4"/></svg>'
    )


def _login_page(error: str) -> str:
    """登录页：只问密钥，不透露其它信息。"""

    message = (
        f'<div class="err"><svg class="i" viewBox="0 0 24 24"><path d="M12 4.5 20.5 19h-17z"/>'
        f'<path d="M12 10v4M12 16.5v.01"/></svg>{error}</div>'
        if error
        else ""
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦玩游戏王 · 登录</title><style>{_STYLE}</style></head>
<body><div class="login"><div class="box">
  <div class="mark">{_icon_sprite()}</div>
  <h1>麦麦玩游戏王</h1>
  <p>输入面板密钥就能进来。面板只在本机监听，看的是卡组池、对局与训练任务。</p>
  {message}
  <form method="post" action="/login">
    <label class="field"><span>面板密钥</span>
      <input type="password" name="key" placeholder="粘贴密钥" autofocus autocomplete="current-password"></label>
    <button class="btn primary" type="submit" style="width:100%;margin-top:14px">进入面板</button>
  </form>
  <div class="hint">密钥在插件配置 <code>webui.api_key</code>、数据目录的
    <code>{KEY_FILE_NAME}</code>，或环境变量 <code>YGO_WEBUI_KEY</code> 里。</div>
</div></div></body></html>"""


def _app_page() -> str:
    """主体单页：侧栏 + 五个视图（总览 / 卡组 / 训练 / 日志 / 配置）。"""

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦玩游戏王 · 控制面板</title><style>{_STYLE}</style></head>
<body>
<div class="shell">
  <aside class="side">
    <div class="brand">
      <div class="mark">{_icon_sprite()}</div>
      <div><b>麦麦玩游戏王</b><span>控制面板</span></div>
    </div>
    <nav>
      <div class="navlink active" data-tab="overview"><svg class="i" viewBox="0 0 24 24"><path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M10 20v-5h4v5"/></svg>总览</div>
      <div class="navlink" data-tab="decks"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="12" height="15" rx="2.2"/><path d="M8 3h10a2 2 0 0 1 2 2v13"/><path d="M7.5 11h4"/></svg>卡组</div>
      <div class="navlink" data-tab="duel"><svg class="i" viewBox="0 0 24 24"><path d="M4 6h16M4 18h16"/><rect x="5" y="8" width="6" height="8" rx="1.5"/><rect x="13" y="8" width="6" height="8" rx="1.5"/></svg>对局</div>
      <div class="navlink" data-tab="training"><svg class="i" viewBox="0 0 24 24"><path d="M12 3v3M6.5 5.5l2 2M17.5 5.5l-2 2"/><rect x="4" y="10" width="16" height="10" rx="3"/><path d="M9 15h6"/></svg>训练台</div>
      <div class="navlink" data-tab="logs"><svg class="i" viewBox="0 0 24 24"><path d="M5 4h14v16H5z"/><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4"/></svg>日志</div>
    </nav>
    <div class="foot">面板只监听本机。<br><a id="side-url" href="http://127.0.0.1:17911">载入中…</a></div>
  </aside>

  <div class="main">
    <div class="topbar">
      <div><h1 id="page-title">总览</h1><div class="sub" id="page-sub"></div></div>
      <span class="sp"></span>
      <a class="btn sm" href="/api/logout">退出</a>
    </div>

    <div class="wrap">
      <section class="view active" id="view-overview">
        <div class="stats" id="ov-stats"></div>
        <div class="grid cols-main" style="margin-top:14px">
          <div>
            <div class="panel"><div class="hd"><h3>进行中的对局</h3><span class="sp"></span>
                <button class="btn sm" id="btn-overview-refresh">刷新</button></div>
              <div class="bd" id="ov-rooms"></div></div>
            <div class="panel" style="margin-top:14px"><div class="hd"><h3>面板与运行环境</h3></div>
              <div class="bd" id="ov-webui"></div></div>
          </div>
          <div class="panel"><div class="hd"><h3>训练功能</h3></div>
            <div class="bd" id="ov-training"></div></div>
          <div class="panel" style="margin-top:14px"><div class="hd"><h3>最近的任务</h3><span class="sp"></span>
              <a class="btn sm" href="#" onclick="tab('training');return false;">去训练页</a></div>
            <div class="bd" id="ov-runs"></div></div>
        </div>
      </section>

      <section class="view" id="view-decks">
        <div class="toolbar">
          <div class="grow"><label class="field"><span>搜索</span>
            <input id="deck-search" placeholder="按卡组名 / 编号 / 脚本名过滤"></label></div>
        </div>
        <div id="decks-body"><div class="empty">加载中…</div></div>
      </section>

      <section class="view" id="view-duel">
        <div class="toolbar">
          <label class="switch"><input type="checkbox" id="duel-live" checked>每 2 秒刷新</label>
          <span class="sp" style="flex:1"></span>
          <button class="btn sm" id="btn-duel-refresh">刷新</button>
        </div>
        <div id="duel-body"><div class="empty">加载中…</div></div>
      </section>

      <section class="view" id="view-training">
        <div id="training-banners"></div>
        <div class="console">
          <div class="stream" id="training-stream">
            <div class="empty">加载中…</div>
          </div>
          <div class="deckworks" id="deck-works"></div>
          <div class="composer">
            <div class="crow2">
              <label class="field"><span>卡组</span><select id="c-deck"></select></label>
              <label class="field"><span>要写的东西</span><select id="c-kind" onchange="onKindChange()"></select></label>
              <div class="extra-inline" id="c-extra"></div>
              <button class="btn primary" id="btn-send" onclick="sendTask()">开始</button>
            </div>
            <div class="cinput">
              <textarea id="c-text" rows="2"
                placeholder="想强调的打法、额外的要求（可以不填）。例如：先手优先做鲜花女男爵；别去踩对面的神宣"></textarea>
            </div>
            <div class="faint" id="c-hint" style="font-size:11.5px"></div>
          </div>
        </div>
        <div id="training-run-detail"></div>
      </section>

      <section class="view" id="view-logs">
        <div class="toolbar">
          <div class="seg" id="log-levels">
            <button class="on" data-level="all" onclick="setLevel('all')">全部</button>
            <button data-level="warning" onclick="setLevel('warning')">警告以上</button>
            <button data-level="error" onclick="setLevel('error')">只看错误</button>
          </div>
          <div class="grow"><input id="log-filter" placeholder="关键词过滤（如 WebUI、房间、训练）"></div>
          <label class="field" style="width:110px"><span>行数</span>
            <input id="log-lines" type="number" value="200" min="20" max="2000"></label>
          <button class="btn" id="btn-logs-refresh">刷新</button>
          <label class="switch"><input type="checkbox" id="btn-logs-live">自动刷新</label>
        </div>
        <pre class="log" id="log-pre">加载中…</pre>
      </section>

    </div>
  </div>
</div>

<div class="sheet-mask" id="sheet-mask" onclick="closeSheet()"></div>
<aside class="sheet" id="deck-sheet">
  <div class="hd"><h3 id="sheet-title">详情</h3><span style="flex:1"></span>
    <button class="btn sm" onclick="closeSheet()">关闭</button></div>
  <div class="bd" id="sheet-body"></div>
</aside>
<div class="toasts" id="toasts"></div>
<script>{_SCRIPT}</script>
</body></html>"""
