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
)

#: 日志里的 ANSI 颜色码（训练日志来自命令行工具，带颜色码时面板里会花屏）。
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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


def _read_deck_rows(db_path: Path) -> List[Dict[str, Any]]:
    """读卡组池全部卡组（跨群），按群号与创建时间排序。"""

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
    """读一份 .ydk，返回分区计数与卡号列表（不查卡名，卡名交给卡牌库可选补充）。"""

    summary: Dict[str, Any] = {"main": [], "extra": [], "side": [], "error": ""}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        summary["error"] = f"读取失败：{exc}"
        return summary
    zone = "main"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if line.startswith("!side"):
                zone = "side"
            elif line.startswith("!extra"):
                zone = "extra"
            elif not line.startswith("#main") and line.startswith("#extra"):
                zone = "extra"
            continue
        if line.isdigit():
            summary[zone].append(line)
    return summary


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

    # 关掉 BaseHTTPRequestHandler 默认往 stderr 打每条访问日志的行为，
    # 统一走插件的 logger（否则控制台会被刷屏）。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        if panel.logger is not None:
            panel.logger.debug("WebUI: " + fmt, *args)

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
            if path.startswith("/api/deck/"):
                deck_id = path[len("/api/deck/"):]
                self._send_json(self._api_deck_detail(deck_id, parse_qs(parsed.query)))
                return
            if path == "/api/config":
                self._send_json(self._api_config())
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
        """给训练表单用的卡组选项（编号、群、名字、现有脚本名）。"""

        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        choices: List[Dict[str, Any]] = []
        for row in _read_deck_rows(panel.deck_db_path):
            choices.append(
                {
                    "deck_id": row.get("deck_id") or "",
                    "group_id": row.get("group_id") or "",
                    "name": row.get("display_name") or "",
                    "generated_script": str(row.get("generated_script") or ""),
                    "picked_style": str(row.get("picked_style") or ""),
                    "windbot_deck": str(row.get("windbot_deck") or ""),
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
        return {"ok": True, "run": payload}

    def _api_decks(self) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        rows = _read_deck_rows(panel.deck_db_path)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            group_id = str(row.get("group_id") or "") or "(未分组)"
            groups.setdefault(group_id, []).append(
                {
                    "deck_id": row.get("deck_id") or "",
                    "name": row.get("display_name") or "",
                    "contributor": row.get("contributor_name") or "",
                    "main": row.get("main_count") or 0,
                    "extra": row.get("extra_count") or 0,
                    "side": row.get("side_count") or 0,
                    "in_random": bool(row.get("in_random")),
                    "generated_script": str(row.get("generated_script") or ""),
                    "picked_style": str(row.get("picked_style") or ""),
                    "created_at": row.get("created_at") or 0,
                    "is_builtin": group_id == "__builtin__",
                }
            )
        return {"ok": True, "groups": [{"group_id": key, "decks": value} for key, value in sorted(groups.items())]}

    def _api_deck_detail(self, deck_id: str, query: Dict[str, List[str]]) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        group_id = str((query.get("group") or [""])[0])
        for row in _read_deck_rows(panel.deck_db_path):
            if str(row.get("deck_id") or "") != deck_id:
                continue
            if group_id and str(row.get("group_id") or "") != group_id:
                continue
            ydk_path = Path(str(row.get("ydk_path") or ""))
            summary = _read_ydk_summary(ydk_path) if ydk_path.exists() else {"error": f"找不到卡表文件：{ydk_path}"}
            names = {}
            card_db = getattr(panel.plugin, "_card_db", None)
            if card_db is not None:
                for zone in ("main", "extra", "side"):
                    names[zone] = [
                        _card_name_or_id(card_db, passcode) for passcode in summary.get(zone, [])
                    ]
            return {
                "ok": True,
                "deck": {
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
                    "generated_script": str(row.get("generated_script") or ""),
                    "picked_style": str(row.get("picked_style") or ""),
                    "in_random": bool(row.get("in_random")),
                    "error": summary.get("error", ""),
                },
            }
        return {"ok": False, "error": f"找不到卡组：{deck_id}"}

    def _api_config(self) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        plugin = panel.plugin
        try:
            raw = plugin.config.model_dump()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取配置失败：{exc}"}
        if isinstance(raw.get("webui"), dict):
            raw["webui"]["api_key"] = "（已隐去）" if raw["webui"].get("api_key") else ""
        return {"ok": True, "config": raw}

    def _api_logs(self, lines: int, keyword: str) -> Dict[str, Any]:
        panel: "WebUIServer" = self.server.panel  # type: ignore[attr-defined]
        raw_lines = _tail_log_lines(panel.host_root, lines, keyword)
        return {
            "ok": True,
            "count": len(raw_lines),
            "lines": [_pretty_log_line(line) for line in raw_lines],
        }


def _card_name_or_id(card_db: Any, passcode: str) -> str:
    """尽量把卡号翻成中文名；查不到就原样返回卡号。"""

    try:
        name = card_db.get_name(passcode)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001  卡牌库接口差异不该让面板报错
        name = ""
    return str(name or passcode)


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

    # ---- 生命周期 ----

    @property
    def url(self) -> str:
        """面板地址（密钥不在 URL 里，登录页会引导填）。"""

        display_host = self.host if self.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        return f"http://{display_host}:{self.bound_port or self.port}/"

    def start(self) -> bool:
        """启动面板；端口被占用等失败一律返回 False 并记日志，绝不抛给插件加载路径。"""

        if self.port and not _port_available(self.host, self.port):
            # 这一步是为了"端口被占就明说"：直接 bind 在 Windows 上不一定报错（SO_REUSEADDR
            # 允许抢端口），抢过来之后两个程序都以为自己在这个端口上服务，排查起来极难。
            if self.logger is not None:
                self.logger.warning(
                    "WebUI 启动失败：%s:%s 已经被别的程序占着（改 webui.port 换一个）",
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
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       background:#14161a; color:#e6e8ee; font-size:14px; }
a { color:#7db4ff; text-decoration:none; }
header { display:flex; align-items:center; gap:14px; padding:14px 20px; background:#1b1e24;
         border-bottom:1px solid #2a2f38; position:sticky; top:0; }
header h1 { font-size:16px; margin:0; font-weight:600; }
header .tag { font-size:12px; color:#8b93a3; }
nav { display:flex; gap:6px; }
nav button { background:#22262e; color:#c8cddb; border:1px solid #303643; border-radius:6px;
             padding:6px 12px; cursor:pointer; font-size:13px; }
  nav button.active { background:#2d5bd7; border-color:#2d5bd7; color:#fff; }
main { padding:20px; max-width:1200px; margin:0 auto; }
section { display:none; }
section.active { display:block; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
.card { background:#1b1e24; border:1px solid #2a2f38; border-radius:10px; padding:14px; }
.card .k { font-size:12px; color:#8b93a3; margin-bottom:6px; }
.card .v { font-size:20px; font-weight:600; word-break:break-all; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid #262b34; font-size:13px; }
th { color:#8b93a3; font-weight:500; }
tr:hover td { background:#1a1d23; }
.muted { color:#8b93a3; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size:12px; }
pre { background:#161a20; border:1px solid #262b34; border-radius:8px; padding:12px;
      overflow:auto; max-height:60vh; font-size:12px; line-height:1.5; }
input, select, button.primary { font-size:14px; padding:9px 12px; border-radius:8px;
      border:1px solid #303643; background:#1b1e24; color:#e6e8ee; }
button.primary { background:#2d5bd7; border-color:#2d5bd7; color:#fff; cursor:pointer; }
button.primary:disabled { background:#2a2f38; border-color:#303643; color:#6b7280; cursor:not-allowed; }
button.ghost { background:#22262e; color:#c8cddb; border:1px solid #303643; border-radius:8px;
               padding:8px 12px; cursor:pointer; font-size:13px; }
.badge { display:inline-block; font-size:11px; padding:2px 7px; border-radius:999px;
         border:1px solid #3a4150; color:#9aa3b4; }
.badge.done { border-color:#2f7d4f; color:#7ee0a2; }
.badge.running { border-color:#2d5bd7; color:#8fb8ff; }
.badge.failed { border-color:#7d2f2f; color:#ff9a9a; }
.badge.cancelled { border-color:#4a4258; color:#b9a6e0; }
.login { max-width:380px; margin:12vh auto; }
.login .card { padding:22px; }
.row { display:flex; gap:10px; margin-bottom:14px; flex-wrap:wrap; align-items:center; }
.kind { border:1px solid #2a2f38; border-radius:10px; padding:14px; margin-bottom:12px; background:#1b1e24; }
.kind h4 { margin:0 0 4px; font-size:14px; }
.kind .note { font-size:12px; color:#8b93a3; margin:0 0 10px; }
.field { display:flex; flex-direction:column; gap:4px; font-size:12px; color:#8b93a3; }
.field input, .field select { min-width:150px; }
.banner { padding:10px 12px; border-radius:8px; margin-bottom:14px; font-size:13px; }
.banner.warn { background:#2b2417; border:1px solid #5c4a1e; color:#f0d090; }
.banner.err { background:#2b1b1b; border:1px solid #5c2a2a; color:#ffa8a8; }
"""

_SCRIPT = """
const Key = { value: "" };
function $(id){ return document.getElementById(id); }
function esc(text){ return String(text==null?"":text).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
async function api(path){
  const res = await fetch(path, { headers: Key.value ? { "X-API-Key": Key.value } : {} });
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  return await res.json();
}
async function postApi(path, payload){
  const headers = { "Content-Type": "application/json" };
  if (Key.value) headers["X-API-Key"] = Key.value;
  const res = await fetch(path, { method: "POST", headers: headers, body: JSON.stringify(payload || {}) });
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  return await res.json();
}
function tab(name){
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("section").forEach(s => s.classList.toggle("active", s.id === "tab-" + name));
  if (name === "overview") loadOverview();
  if (name === "decks") loadDecks();
  if (name === "training") loadTraining();
  if (name === "logs") loadLogs();
  if (name === "config") loadConfig();
}
async function loadOverview(){
  const d = await api("/api/status");
  if (!d.ok) return;
  $("ov-cards").innerHTML = [
    ["已注册卡组", d.decks.total],
    ["随机池", d.decks.in_random],
    ["有专属脚本", d.decks.with_script],
    ["已挑样式", d.decks.with_style],
    ["进行中房间", (d.rooms||[]).length],
  ].map(([k,v]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");
  const counts = d.training.counts || {};
  const statusText = Object.keys(counts).length
    ? Object.entries(counts).map(([k,v]) => `${statusLabel(k)} ${v}`).join("　")
    : "还没有训练记录";
  $("ov-training").innerHTML = `<table><tbody>
     <tr><td class="muted">训练功能</td><td>${d.training.enabled ? "已启用" : "已关闭（training.enabled = false）"}</td></tr>
     <tr><td class="muted">记录</td><td>${esc(statusText)}</td></tr>
     <tr><td class="muted">训练模型</td><td>${esc(d.training.model)}</td></tr>
     <tr><td class="muted">工作目录</td><td class="mono">${esc(d.training.workspace)}</td></tr>
     <tr><td class="muted">单次擂台上限</td><td>${esc(d.training.max_duels_per_run)} 局</td></tr>
     </tbody></table>`;
  $("ov-webui").innerHTML = `<table><tbody>
     <tr><td class="muted">面板地址</td><td class="mono">${esc(d.webui.url)}</td></tr>
     <tr><td class="muted">监听</td><td class="mono">${esc(d.webui.host)}:${esc(d.webui.port)}</td></tr>
     <tr><td class="muted">密钥来源</td><td>${esc(d.webui.key_source)}</td></tr>
     <tr><td class="muted">密钥文件</td><td class="mono">${esc(d.webui.key_file || "（由配置或环境变量提供）")}</td></tr>
     <tr><td class="muted">启动时间</td><td>${esc(d.webui.started_at)}</td></tr>
     <tr><td class="muted">数据目录</td><td class="mono">${esc(d.plugin.data_dir)}</td></tr>
     </tbody></table>`;
}
function statusLabel(status){
  return ({running:"跑着", done:"成功", failed:"失败", cancelled:"已停止"})[status] || status;
}
function statusBadge(status){
  return `<span class="badge ${esc(status)}">${esc(statusLabel(status))}</span>`;
}
async function loadDecks(){
  const d = await api("/api/decks");
  if (!d.ok) return;
  if (!d.groups.length) { $("decks-body").innerHTML = `<p class="muted">卡组池是空的。</p>`; return; }
  $("decks-body").innerHTML = d.groups.map(g => `
    <h3>${g.group_id === "__builtin__" ? "内置卡组" : "群 " + esc(g.group_id)} <span class="muted">（${g.decks.length} 副）</span></h3>
    <table><thead><tr><th>编号</th><th>名称</th><th>投稿人</th><th>主/额外/副</th><th>随机池</th><th>专属脚本</th><th>挑样式</th><th></th></tr></thead>
    <tbody>${g.decks.map(x => `<tr>
      <td class="mono">${esc(x.deck_id)}</td>
      <td>${esc(x.name)}</td><td class="muted">${esc(x.contributor)}</td>
      <td class="mono">${x.main}/${x.extra}/${x.side}</td>
      <td>${x.in_random ? "是" : "否"}</td>
      <td class="mono">${esc(x.generated_script || "-")}</td>
      <td class="mono">${esc(x.picked_style || "-")}</td>
      <td><a href="#" onclick="showDeck('${esc(x.deck_id)}','${esc(g.group_id)}');return false;">详情</a></td>
    </tr>`).join("")}</tbody></table>`).join("");
  $("deck-detail").innerHTML = "";
}
async function showDeck(deckId, group){
  const d = await api(`/api/deck/${encodeURIComponent(deckId)}?group=${encodeURIComponent(group)}`);
  if (!d.ok) { $("deck-detail").innerHTML = `<p class="muted">${esc(d.error)}</p>`; return; }
  const deck = d.deck;
  const zone = (title, items) => items && items.length
      ? `<h4>${title}（${items.length}）</h4><div class="mono muted">${items.map(esc).join("、")}</div>` : "";
  $("deck-detail").innerHTML = `<div class="card">
    <h3>${esc(deck.name)}</h3>
    <p class="muted mono">${esc(deck.ydk_path)}</p>
    ${deck.error ? `<p class="muted">${esc(deck.error)}</p>` : ""}
    ${zone("主卡组", deck.cards.main)}
    ${zone("额外卡组", deck.cards.extra)}
    ${zone("副卡组", deck.cards.side)}
  </div>`;
}
let trainingTimer = null;
async function loadTraining(){
  const d = await api("/api/training");
  if (!d.ok) { $("training-body").innerHTML = `<p class="muted">${esc(d.error||"读不到训练数据")}</p>`; return; }
  const banners = [];
  if (!d.enabled) banners.push(`<div class="banner warn">训练功能在配置里关着（training.enabled = false）：这一页只能看历史记录。</div>`);
  if (d.enabled && !d.bridge_ready) banners.push(`<div class="banner err">训练执行器没建起来——看插件日志里“训练功能”那一行找原因。</div>`);
  $("training-banners").innerHTML = banners.join("");

  const active = d.active;
  if (active) {
    $("training-active").innerHTML = `<div class="kind">
      <h4>正在跑：${esc(active.title)} ${statusBadge(active.status)}</h4>
      <p class="note">${esc(active.kind_title)}｜起了 ${esc(active.started_at)}｜${active.duration_seconds==null?"":esc(active.duration_seconds)+" 秒"}｜编号 ${esc(active.run_id)}</p>
      <button class="ghost" onclick="stopTraining()">停掉</button>
      <pre>${esc((active.tail||[]).join("\\n")) || "（还没有输出）"}</pre>
    </div>`;
  } else {
    $("training-active").innerHTML = `<p class="muted">现在没有任务在跑。</p>`;
  }

  const deckOptions = (d.decks||[]).map(x =>
    `<option value="${esc(x.deck_id)}">${x.is_builtin?"[内置] ":""}${esc(x.name)}（#${esc(x.deck_id)}）</option>`).join("");
  $("training-forms").innerHTML = (d.kinds||[]).map(k => {
    const disabled = (!k.ready || active || !k.bridge_ready) ? "disabled" : "";
    const rows = [];
    if (k.fields.includes("deck")) {
      rows.push(`<label class="field">卡组<select id="f-${k.kind}-deck">${deckOptions}</select></label>`);
    }
    if (k.fields.includes("duels")) {
      rows.push(`<label class="field">局数<input id="f-${k.kind}-duels" type="number" value="${d.max_duels_per_run}" min="2" max="${d.max_duels_per_run}"></label>`);
    }
    if (k.fields.includes("style_a")) {
      rows.push(`<label class="field">脚本 A<input id="f-${k.kind}-style-a" placeholder="如 RaiseMoon"></label>`);
      rows.push(`<label class="field">脚本 B<input id="f-${k.kind}-style-b" placeholder="如 Gen88"></label>`);
    }
    if (k.fields.includes("style")) {
      rows.push(`<label class="field">自写执行器名<input id="f-${k.kind}-style" placeholder="如 KillerTune；留空则按卡组编号"><span class="muted">查的是插件 executors/ 里那份源码；WindBot 自带的那些没有源码文件，查不到</span></label>`);
      rows.push(`<label class="field">卡组编号<input id="f-${k.kind}-deck-ids" placeholder="如 95,99,88"></label>`);
      rows.push(`<label class="field">群号<input id="f-${k.kind}-group" placeholder="按某群的随机池体检"></label>`);
    }
    if (k.fields.includes("latest")) {
      rows.push(`<label class="field">取最近几份<input id="f-${k.kind}-latest" type="number" value="5" min="1" max="50"></label>`);
      rows.push(`<label class="field">对比卡组编号<input id="f-${k.kind}-deck" placeholder="可留空"></label>`);
    }
    return `<div class="kind">
      <h4>${esc(k.title)}</h4>
      <p class="note">${esc(k.note || "")}${k.needs_engine ? "｜会真的起对局，房间里有人时不能跑" : ""}</p>
      <div class="row">${rows.join("")}<button class="primary" ${disabled} onclick="startTraining('${k.kind}')">开始</button></div>
    </div>`;
  }).join("");

  const runs = d.runs || [];
  $("training-runs").innerHTML = runs.length ? `<table>
      <thead><tr><th>编号</th><th>种类</th><th>标题</th><th>状态</th><th>开始</th><th>耗时</th><th>结论</th></tr></thead>
      <tbody>${runs.map(r => `<tr>
        <td class="mono">${esc(r.run_id)}</td>
        <td>${esc(r.kind_title)}</td>
        <td>${esc(r.title)}</td>
        <td>${statusBadge(r.status)}</td>
        <td class="mono">${esc(r.started_at)}</td>
        <td class="mono">${r.duration_seconds==null?"-":esc(r.duration_seconds)+"s"}</td>
        <td><a href="#" onclick="showRun('${esc(r.run_id)}');return false;">详情</a></td>
      </tr>`).join("")}</tbody></table>` : `<p class="muted">还没有训练记录。</p>`;
  $("training-run-detail").innerHTML = "";

  if (active && !trainingTimer) trainingTimer = setInterval(loadTraining, 5000);
  if (!active && trainingTimer) { clearInterval(trainingTimer); trainingTimer = null; }
}
async function showRun(runId){
  const d = await api(`/api/training/run/${encodeURIComponent(runId)}`);
  if (!d.ok) { $("training-run-detail").innerHTML = `<p class="muted">${esc(d.error)}</p>`; return; }
  const run = d.run;
  const summary = run.summary || {};
  const warnings = summary.warnings || [];
  const parts = [];
  if (run.error) parts.push(`<div class="banner err">${esc(run.error)}</div>`);
  if (summary.conclusion) parts.push(`<h4>结论</h4><p>${esc(summary.conclusion)}</p>`);
  if (summary.conclusion_error) parts.push(`<div class="banner warn">结论没生成：${esc(summary.conclusion_error)}</div>`);
  if (warnings.length) parts.push(`<h4>校验疑点</h4><ul>${warnings.map(w=>`<li>${esc(w)}</li>`).join("")}</ul>`);
  if (summary.guide_path) parts.push(`<p class="muted mono">推演已存档：${esc(summary.guide_path)}</p>`);
  parts.push(`<h4>输出尾巴</h4><pre>${esc((run.tail||[]).join("\\n")) || "（没有输出）"}</pre>`);
  $("training-run-detail").innerHTML = `<div class="kind"><h4>${esc(run.title)} ${statusBadge(run.status)}</h4>
    <p class="note mono">${esc(JSON.stringify(run.params))}</p>${parts.join("")}</div>`;
}
function trainingField(kind, name){
  const el = $(`f-${kind}-${name}`);
  return el ? el.value.trim() : "";
}
async function startTraining(kind){
  const payload = { kind: kind };
  const deck = trainingField(kind, "deck");
  if (deck) payload.deck_id = Number(deck);
  if (kind === "arena") {
    payload.duels = Number(trainingField(kind, "duels") || 60);
    payload.style_a = trainingField(kind, "style-a");
    payload.style_b = trainingField(kind, "style-b");
  }
  if (kind === "script") {
    payload.style = trainingField(kind, "style");
    payload.deck_ids = trainingField(kind, "deck-ids");
    payload.group = trainingField(kind, "group");
  }
  if (kind === "replay") {
    payload.latest = Number(trainingField(kind, "latest") || 5);
    const compare = trainingField(kind, "deck");
    if (compare) payload.deck_id = Number(compare);
  }
  const d = await postApi("/api/training/start", payload);
  if (!d.ok) { $("training-banners").innerHTML = `<div class="banner err">${esc(d.error)}</div>`; return; }
  loadTraining();
}
async function stopTraining(){
  const d = await postApi("/api/training/stop", {});
  if (!d.ok) { $("training-banners").innerHTML = `<div class="banner err">${esc(d.error)}</div>`; return; }
  loadTraining();
}
async function loadLogs(){
  const lines = $("log-lines").value || 200;
  const filter = $("log-filter").value || "";
  const d = await api(`/api/logs?lines=${encodeURIComponent(lines)}&filter=${encodeURIComponent(filter)}`);
  $("log-pre").textContent = d.ok ? (d.lines.join("\\n") || "（没有匹配的日志）") : d.error;
}
async function loadConfig(){
  const d = await api("/api/config");
  $("config-pre").textContent = d.ok ? JSON.stringify(d.config, null, 2) : d.error;
}
document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("key");
  if (fromQuery) { Key.value = fromQuery; }
  document.querySelectorAll("nav button").forEach(b => b.onclick = () => tab(b.dataset.tab));
  if ($("btn-logs")) $("btn-logs").onclick = loadLogs;
  tab("overview");
});
"""


def _login_page(error: str) -> str:
    """登录页：只问密钥，不透露其它信息。"""

    message = f'<p style="color:#ff8a8a">{error}</p>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>麦麦玩游戏王 · 面板登录</title><style>{_STYLE}</style></head>
<body><div class="login"><div class="card">
<h1 style="margin-top:0;font-size:17px">麦麦玩游戏王 · 控制面板</h1>
<p class="muted">请输入面板密钥（在插件配置 <span class="mono">webui.api_key</span>，
或数据目录的 <span class="mono">{KEY_FILE_NAME}</span>，或环境变量
<span class="mono">YGO_WEBUI_KEY</span>）。</p>
{message}
<form method="post" action="/login">
<input type="password" name="key" placeholder="面板密钥" style="width:100%;margin-bottom:10px" autofocus>
<button class="primary" type="submit" style="width:100%">进入</button>
</form>
</div></div></body></html>"""


def _app_page() -> str:
    """主体单页：概览 / 卡组 / 日志 / 设置。"""

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>麦麦玩游戏王 · 控制面板</title><style>{_STYLE}</style></head>
<body>
<header>
  <h1>麦麦玩游戏王</h1>
  <span class="tag">控制面板</span>
  <nav>
    <button data-tab="overview" class="active">概览</button>
    <button data-tab="decks">卡组</button>
    <button data-tab="training">训练</button>
    <button data-tab="logs">日志</button>
    <button data-tab="config">配置</button>
  </nav>
  <span style="flex:1"></span>
  <a href="/api/logout" class="muted">退出</a>
</header>
<main>
  <section id="tab-overview" class="active">
    <div class="cards" id="ov-cards"></div>
    <h3>训练功能</h3><div id="ov-training"></div>
    <h3>面板与运行环境</h3><div id="ov-webui"></div>
  </section>
  <section id="tab-decks">
    <div id="decks-body"><p class="muted">加载中…</p></div>
    <div id="deck-detail"></div>
  </section>
  <section id="tab-training">
    <div id="training-banners"></div>
    <div id="training-active"><p class="muted">加载中…</p></div>
    <h3>起一个任务</h3>
    <div id="training-forms"></div>
    <h3>最近的任务</h3>
    <div id="training-runs"></div>
    <div id="training-run-detail"></div>
  </section>
  <section id="tab-logs">
    <div class="row">
      <input id="log-lines" type="number" value="200" min="20" max="2000" style="width:110px">
      <input id="log-filter" placeholder="关键词过滤（如 WebUI、房间、错误）" style="flex:1;min-width:220px">
      <button class="primary" id="btn-logs">刷新</button>
    </div>
    <pre id="log-pre">加载中…</pre>
  </section>
  <section id="tab-config">
    <p class="muted">当前生效配置（密钥已隐去；本页只读，要改配置请到麦麦的插件配置页）。</p>
    <pre id="config-pre">加载中…</pre>
  </section>
</main>
<script>{_SCRIPT}</script>
</body></html>"""
