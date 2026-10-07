"""对局监视（只读）：盯着插件日志，把对局里出现的问题**记录下来**，不修、不打扰游玩。

用法::

    python tools/room_watch.py --follow            # 常驻看门狗（默认：从"现在"开始，不补历史）
    python tools/room_watch.py --once              # 扫一遍就退出（给定时任务用；同一份 state 接着跑）
    python tools/room_watch.py --once --from-start # 把已有历史日志也扫一遍（补录用）

**为什么要有它**：群友在房间里遇到的问题（效果不发动、卡在载入、进房开不了、整局不出牌）
大多只在**日志**里留痕，而那行字往往一闪而过。这个工具把日志当成数据源，持续把可疑的行
抽出来落成两份产物，等"要修了"的时候一次性交给修复：

* ``temp/room-watch/findings.jsonl``：一条一个 JSON（时间、类别、严重度、去重键、证据行）；
* ``temp/room-watch/report.md``：给人看的汇总（按类别分组 + 每局一行摘要）。

**零打扰约束**（改这个文件时别越过）：
1. **只读**日志：只 ``open(..., "r")``，不写、不锁、不删任何日志；
2. **不碰对局**：不起房间、不发消息、不调插件接口，只读 ``logs/`` 下的日志文件；
3. **低开销**：默认 30 秒轮询一次、只读上次偏移之后新增的字节，空闲时几乎不占 CPU；
4. **不自动修**：发现问题只记录（用户口径：攒着一起修）。

判据（类别 → 严重度）：
* ``内核脚本报错``（high）——``attempt to call an error function`` / ``[对局进程:err]`` /
  ``[string "./script/…``。这一类＝某张卡的脚本加载失败，那张卡在对局里是白板
  （2026-10-07 异解那局就是这么表现的：能召唤、能送墓，效果全空）。
* ``插件报错``（high）／``插件警告``（medium）——本插件自己打出的 error/警告（日志名 `plugin.mai-play-ygo`，
  见 :data:`PLUGIN_LOGGER_PREFIXES`）；"被拒绝"这类大模型拒答只记 info。
* ``进房未开打``（medium）——玩家进了房间却迟迟没有 ``duel_started``（版本不一致/卡握手）。
* ``开局未结束``（medium）——``duel_started`` 之后迟迟没有 ``duel_ended``。
* ``闸门/房间异常``（medium）——闸门、房间启动/收摊报错。
* ``整局无动作``（medium）——一局打完（或超过 3 回合）时，某一侧**一件卡都没落到场上**
  （按 recorder 的落位行判；这是"整局不出牌"最可信的代理指标）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = _PLUGIN_ROOT.parent.parent / "logs"
DEFAULT_STATE = Path("temp/room-watch/state.json")
DEFAULT_FINDINGS = Path("temp/room-watch/findings.jsonl")
DEFAULT_REPORT = Path("temp/room-watch/report.md")

SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}

#: 内核脚本加载失败的判据（卡在对局里等于白板）
KERNEL_SCRIPT_PATTERNS = (
    "attempt to call an error function",
    '[string "./script/',
    "[对局进程:err]",
)
#: 插件警告里"像真问题"的关键词（其余警告只当 info，避免刷屏）
PLUGIN_PROBLEM_WORDS = ("失败", "错误", "异常", "超时", "占用", "崩溃")
#: 本插件的日志名：宿主按 ``plugin.<插件 id>`` 命名，插件自己的模块日志也都挂在这个名字下
#: （实测 2026-10-08 的日志里就是 ``plugin.mai-play-ygo``）。
#: ⚠ 这里原来写的是合并前的 ``plugin.yugioh.duel-arena`` —— 对不上真实日志名，于是
#: "插件报错 / 插件警告"三类**永远匹配不到**，看门狗会安静地漏掉真问题（2026-10-07 第五轮评审指出）。
#: 插件改名时这里要跟着改；`tests/test_room_watch.py` 会拿 `_manifest.json` 的 id 对一遍。
PLUGIN_LOGGER_PREFIXES = ("plugin.mai-play-ygo",)
#: 进房后多久还没开打就记一笔（秒）
JOIN_GRACE_SECONDS = 180
#: 开局后多久还没结束就记一笔（秒）：房间每股时钟 180 秒，正常局 3~8 分钟
DUEL_GRACE_SECONDS = 20 * 60
#: 一局的回合数达到这个值，才用"整局无动作"去判某一侧
MIN_TURNS_FOR_IDLE = 3

_PLACEMENT_RE = re.compile(r"落位：第 (\d+) 回合 (.+?)（(?:我方|对方)）")
_JOIN_RE = re.compile(r"有客户端进入房间：(玩家|机器人) (.+)$")
_CARD_ID_RE = re.compile(r"c(\d{6,9})\.lua|\(c(\d{6,9})\.")


def severity_of(category: str, level: str, text: str) -> str:
    """一条命中的严重度。"""

    if category in ("内核脚本报错", "插件报错", "闸门/房间异常"):
        return "high" if category != "闸门/房间异常" else "medium"
    if category == "插件警告":
        if "被拒绝" in text:
            return "info"
        return "medium" if any(word in text for word in PLUGIN_PROBLEM_WORDS) else "info"
    return "medium"


def classify(logger: str, level: str, event: str) -> Optional[str]:
    """把一行日志归到一个类别；不属于任何类别时返回 None。"""

    if any(pattern in event for pattern in KERNEL_SCRIPT_PATTERNS):
        return "内核脚本报错"
    if logger.startswith(PLUGIN_LOGGER_PREFIXES):
        if level == "error":
            return "插件报错"
        if level == "warning":
            return "插件警告"
        if any(word in event for word in PLUGIN_PROBLEM_WORDS) and "失败" in event:
            return "闸门/房间异常"
    return None


def card_ids_in(text: str) -> List[str]:
    """从一行里抽出卡号（``c12345.lua`` 或 ``(c12345.`` 两种写法）。"""

    found: List[str] = []
    for match in _CARD_ID_RE.finditer(text):
        card_id = match.group(1) or match.group(2)
        if card_id and card_id not in found:
            found.append(card_id)
    return found


def line_key(source: str, offset: int, text: str) -> str:
    """去重键：同一个文件同一位置的同一行只记一次。"""

    raw = f"{source}:{offset}:{text[:200]}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


class WatchState:
    """跨进程共享的进度：每个日志文件读到哪、当前局在谁手上。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offsets: Dict[str, int] = {}          # 日志文件 → 已读字节
        self.seen: Dict[str, float] = {}           # 去重键 → 首次出现时间
        self.seen_events: Dict[str, float] = {}    # 「时间戳+正文」→ 首次出现（同一条事件去重）
        self.pending_join: Dict[str, float] = {}    # 玩家名 → 进房时间（等 duel_started）
        self.roster: List[str] = []                 # 本房间的对局名册（进房行攒出来的）
        self.duel_started_at: Optional[float] = None
        self.duel_players: List[str] = []           # 这一局的落位行看到的双方
        self.duel_placements: Dict[str, int] = {}   # 玩家名 → 落位件数
        self.duel_max_turn: int = 0
        self.duels: int = 0                         # 统计：已完成的局数

    @classmethod
    def load(cls, path: Path) -> "WatchState":
        """读状态；没有（或坏了）就返回空状态。"""

        state = cls(path)
        if not path.is_file():
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        state.offsets = {str(k): int(v) for k, v in (data.get("offsets") or {}).items()}
        state.seen = {str(k): float(v) for k, v in (data.get("seen") or {}).items()}
        state.seen_events = {str(k): float(v) for k, v in (data.get("seen_events") or {}).items()}
        state.pending_join = {str(k): float(v) for k, v in (data.get("pending_join") or {}).items()}
        state.roster = [str(v) for v in (data.get("roster") or [])]
        state.duel_started_at = data.get("duel_started_at")
        state.duel_players = list(data.get("duel_players") or [])
        state.duel_placements = {str(k): int(v) for k, v in (data.get("duel_placements") or {}).items()}
        state.duel_max_turn = int(data.get("duel_max_turn") or 0)
        state.duels = int(data.get("duels") or 0)
        return state

    def save(self) -> None:
        """落盘（原子写：先写临时文件再替换，避免读到的状态半截）。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "offsets": self.offsets,
            "seen": self.seen,
            "seen_events": self.seen_events,
            "pending_join": self.pending_join,
            "roster": self.roster,
            "duel_started_at": self.duel_started_at,
            "duel_players": self.duel_players,
            "duel_placements": self.duel_placements,
            "duel_max_turn": self.duel_max_turn,
            "duels": self.duels,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class Watcher:
    """把新增的日志行读进来、判类别、攒成一局的摘要。"""

    def __init__(self, log_dir: Path, state: WatchState, findings: Path, report: Path) -> None:
        self.log_dir = log_dir
        self.state = state
        self.findings = findings
        self.report = report
        self.new_findings: List[dict] = []
        self.duel_lines: List[str] = []      # 当前局的原始行（写摘要用）

    def _log_files(self) -> List[Path]:
        """要盯的日志：宿主 ``logs/app_*.log.jsonl``（插件与内核 stdout 都在里面）。"""

        return sorted(self.log_dir.glob("app_*.log.jsonl"))

    def poll(self, *, now: Optional[float] = None) -> int:
        """扫一遍所有日志的新增部分；返回这次新增的命中数。"""

        now = time.time() if now is None else now
        for path in self._log_files():
            self._read_new(path, now)
        self._check_pending(now)
        self._flush()
        return len(self.new_findings)

    def _read_new(self, path: Path, now: float) -> None:
        """读一个文件里上次偏移之后的新行（二进制读，避免半行解码问题）。"""

        key = str(path)
        offset = self.state.offsets.get(key, 0)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < offset:      # 日志被轮转/截断：从头再来
            offset = 0
        if size == offset:
            self.state.offsets[key] = offset
            return
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(size - offset)
            self.state.offsets[key] = handle.tell()
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            self._consume(key, offset, raw, now)
            offset += len(raw) + 1

    def _consume(self, source: str, offset: int, raw: bytes, now: float) -> None:
        """处理一行：结构化日志能解就解，解不开（内核 stdout 的裸行）也照样判类别。"""

        text = raw.decode("utf-8", "replace")
        logger, level, event = "", "", text
        ts = ""
        if text.lstrip().startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                logger = str(data.get("logger") or "")
                level = str(data.get("level") or "")
                event = str(data.get("event") or "")
                ts = str(data.get("timestamp") or "")
        # **同一条事件在日志里会出现多份**（实测：同一秒的 `duel_ended` 在 2~3 个 app_*.log.jsonl
        # 里各有一份，甚至同一文件里重复）——按"时间戳 + 正文"去重，否则一局会被算成好几局、
        # 同一条问题会被记好几次。不带的裸行（内核 stdout）正文里一般自带时间戳，也能去重。
        dup_key = hashlib.sha256(f"{ts}|{event.strip()[:400]}".encode("utf-8", "replace")).hexdigest()[:16]
        if dup_key in self.state.seen_events:
            return
        self.state.seen_events[dup_key] = now
        # 一局的摘要素材：进房/开局/收局 + 落位行（两个来源都要，裸行里也有）
        join = _JOIN_RE.search(event)
        if join:
            name = join.group(2).strip()
            # 名册要从**进房行**攒：只放落位行的话，"一件都没放"的那一侧根本不会出现在名册里，
            # "整局无动作"这条规则就永远触发不了（这个 bug 是 2026-10-07 的用例抓出来的）
            if name and name not in self.state.roster:
                self.state.roster.append(name)
            if join.group(1) == "玩家":
                # 玩家进房先挂起来：等他真的开打（`duel_started`）就算正常
                self.state.pending_join[name] = now
        self._track_duel(event, now)
        if "落位：" in event:
            self._track_placement(event)
        category = classify(logger, level, event)
        if category is None:
            return
        self._record(category, logger, level, event, source, offset, now)

    def _track_duel(self, event: str, now: float) -> None:
        """开局/收局：维护"当前局"，收局时产出一条摘要。"""

        if "duel_started" in event:
            self.state.duel_started_at = now
            self.state.duel_players = []
            self.state.duel_placements = {}
            self.state.duel_max_turn = 0
            self.state.pending_join.clear()      # 已经开打了，"进房未开打"不再成立
            self.duel_lines = []
            return
        if "duel_ended" in event and self.state.duel_started_at is not None:
            started = self.state.duel_started_at
            self.state.duel_started_at = None
            self.state.duels += 1
            self._finish_duel(started, now)

    def _track_placement(self, event: str) -> None:
        """落位行：记下双方各落了几件、打到第几回合（"整局无动作"的判据）。"""

        match = _PLACEMENT_RE.search(event)
        if not match:
            return
        turn = int(match.group(1))
        who = match.group(2).strip()
        self.state.duel_max_turn = max(self.state.duel_max_turn, turn)
        if who not in self.state.duel_players:
            self.state.duel_players.append(who)
        self.state.duel_placements[who] = self.state.duel_placements.get(who, 0) + 1

    def _finish_duel(self, started: float, now: float) -> None:
        """一局结束：写一行摘要，并按需报"整局无动作"。"""

        minutes = int((now - started) // 60)
        summary = (
            f"局 #{self.state.duels}：约 {minutes} 分钟"
            f"，{self.state.duel_max_turn} 回合"
            f"，落位 "
            + ("、".join(f"{who} {n}" for who, n in self.state.duel_placements.items()) or "无")
        )
        self.new_findings.append(
            {
                "time": _stamp(now),
                "category": "对局摘要",
                "severity": "info",
                "key": line_key("duel", self.state.duels, summary),
                "evidence": summary,
            }
        )
        roster = list(dict.fromkeys(self.state.roster))            # 进房行里的名字（大厅名）
        labels = list(dict.fromkeys(self.state.duel_players))      # 落位行里的名字（游戏内名/座位N）
        # **同名/换名/多余名字**：一局只有两侧，所以只要**两侧都落过牌**，就不可能有人"整局无动作"——
        # 名册里多出来的名字要么是同一个人换的名字（实测：群友客户端用默认名进房是「未报名」、
        # 记录器那侧又标成「座位N」；另一次是同一个人换了昵称），要么是进来没打的旁观者。
        # 只有"落位标签不足 2 个"时才需要去名册里找那个没出牌的人（2026-10-07 两轮实测）。
        idle = [name for name in roster if not self.state.duel_placements.get(name)]
        suspicious = idle if len(labels) < 2 else []
        self.state.roster = []
        if self.state.duel_max_turn >= MIN_TURNS_FOR_IDLE:
            for who in suspicious:
                if not self.state.duel_placements.get(who):
                    self.new_findings.append(
                        {
                            "time": _stamp(now),
                            "category": "整局无动作",
                            "severity": "medium",
                            "key": line_key("idle", self.state.duels, who),
                            "evidence": f"{summary}；**{who} 一件卡都没落到场上**",
                        }
                    )

    def _check_pending(self, now: float) -> None:
        """超时检查：进房没开打、开局没结束。"""

        for who, joined in list(self.state.pending_join.items()):
            if now - joined > JOIN_GRACE_SECONDS:
                self.new_findings.append(
                    {
                        "time": _stamp(now),
                        "category": "进房未开打",
                        "severity": "medium",
                        "key": line_key("join", who, str(int(joined))),
                        "evidence": f"「{who}」进房 {int((now - joined) // 60)} 分钟仍未开始对局",
                    }
                )
                del self.state.pending_join[who]
        started = self.state.duel_started_at
        if started is not None and now - started > DUEL_GRACE_SECONDS:
            self.new_findings.append(
                {
                    "time": _stamp(now),
                    "category": "开局未结束",
                    "severity": "medium",
                    "key": line_key("duel-open", str(int(started)), ""),
                    "evidence": f"这局已经打了 {int((now - started) // 60)} 分钟还没结束（可能卡住/超时）",
                }
            )
            self.state.duel_started_at = None      # 只报一次

    def _record(
        self, category: str, logger: str, level: str, event: str, source: str, offset: int, now: float
    ) -> None:
        """记一条命中（同位置只记一次）。"""

        key = line_key(source, offset, event)
        if key in self.state.seen:
            return
        self.state.seen[key] = now
        severity = severity_of(category, level, event)
        if category == "插件警告" and severity == "info":
            return                                  # 大模型拒答之类的噪声，不落账
        finding = {
            "time": _stamp(now),
            "category": category,
            "severity": severity,
            "key": key,
            "logger": logger,
            "evidence": event.strip()[:400],
        }
        cards = card_ids_in(event)
        if cards:
            finding["cards"] = cards
        self.new_findings.append(finding)

    def _flush(self) -> None:
        """把新的命中追加到 jsonl，并重画 report.md。"""

        if not self.new_findings:
            return
        self.findings.parent.mkdir(parents=True, exist_ok=True)
        with self.findings.open("a", encoding="utf-8") as handle:
            for finding in self.new_findings:
                handle.write(json.dumps(finding, ensure_ascii=False) + "\n")
        self.new_findings = []
        write_report(self.findings, self.report, duels=self.state.duels)


def _stamp(now: float) -> str:
    """本地时间戳（日志里是 UTC，这里统一成本地，方便对群里的时间）。"""

    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))


def write_report(findings_path: Path, report_path: Path, *, duels: int = 0) -> None:
    """把 jsonl 汇总成给人看的一页：类别计数 + 最近的条目。"""

    rows: List[dict] = []
    if findings_path.is_file():
        for line in findings_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    counts: Dict[str, int] = {}
    for row in rows:
        if row.get("category") != "对局摘要":
            counts[row["category"]] = counts.get(row["category"], 0) + 1
    entries = sum(counts.values())          # "记录条目"只数非摘要的那些
    lines = [
        "# 对局监视记录（`tools/room_watch.py`）",
        "",
        "> 只读日志、只记录、不修。要修的时候按类别一次性处理；"
        "**这里是原始记录，不是结论**——每条都要先看证据行。",
        "",
        f"- 已观察对局：**{duels}** 局",
        f"- 记录条目：**{entries}** 条（不含对局摘要）",
        "- 按类别：" + ("、".join(f"{name} {n}" for name, n in sorted(counts.items())) or "无"),
        "",
        "## 最近 40 条（新的在前）",
        "",
    ]
    for row in reversed(rows[-40:]):
        tag = f"[{row.get('severity', '?')}]"
        cards = ("｜卡号 " + "、".join(row.get("cards") or [])) if row.get("cards") else ""
        lines.append(f"- `{row.get('time', '')}` {tag} **{row.get('category', '')}** {row.get('evidence', '')}{cards}")
    lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """命令行：跑法（常驻/单次）、从哪开始、目录。"""

    parser = argparse.ArgumentParser(description="对局监视（只读日志、只记录、不修）")
    parser.add_argument("--follow", action="store_true", help="常驻：每 --interval 秒扫一次")
    parser.add_argument("--once", action="store_true", help="扫一遍就退出（给定时任务用）")
    parser.add_argument("--interval", type=int, default=30, help="常驻模式的轮询间隔（秒）")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="宿主日志目录")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="进度文件")
    parser.add_argument("--out", type=Path, default=DEFAULT_FINDINGS, help="记录（jsonl）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="汇总（md）")
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="把已有日志从头扫一遍（默认从「现在」开始：监视的是「从现在起」的对局）",
    )
    return parser.parse_args()


def initialize_offsets(state: WatchState, log_dir: Path) -> None:
    """从"现在"开始：把已有日志的偏移设到文件末尾，只监视此后新增的内容。"""

    for path in sorted(log_dir.glob("app_*.log.jsonl")):
        try:
            state.offsets[str(path)] = path.stat().st_size
        except OSError:
            continue


def main() -> int:
    """入口：单次扫描或常驻循环。"""

    args = parse_args()
    if not args.once and not args.follow:
        args.follow = True
    state = WatchState.load(args.state)
    if not args.from_start and not state.offsets:
        initialize_offsets(state, args.log_dir)
        state.save()
    watcher = Watcher(args.log_dir, state, args.out, args.report)
    if args.once:
        hits = watcher.poll()
        state.save()
        print(f"本次扫描：新增命中 {hits} 条（累计观察 {state.duels} 局）")
        return 0
    print(f"开始监视 {args.log_dir}（每 {args.interval} 秒扫一次；只读、不修）", flush=True)
    while True:
        try:
            hits = watcher.poll()
            state.save()
            if hits:
                print(f"[{_stamp(time.time())}] 新增命中 {hits} 条", flush=True)
        except Exception as error:  # noqa: BLE001  看门狗绝不能因为单次异常退出
            print(f"[{_stamp(time.time())}] 扫描出错（继续）：{type(error).__name__}: {error}", flush=True)
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
