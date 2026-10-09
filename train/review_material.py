"""复盘要喂给模型的两样"看得见代码/决策"的材料（纯函数，好测）。

为什么需要它：复盘原来只有记录器的统计（回合数、动作数、用过的卡、落位），
于是它只能看出"哪里不对"却说不出"该改哪一行"。2026-10-09 那次真机排查证明了要定位到
"缺一个 `HintMsg.Target` 分支""某个闸门因为 `ActivateDescription == -1` 成了死代码"，
手里必须有两样东西：

1. **脚本的结构摘要**：规则注册顺序（`AddExecutor(...)` 的先后就是优先级）、
   `OnSelectCard` 里**已经处理了哪些 `HintMsg`**（缺哪一类一眼可见）、每个规则函数的签名；
2. **那一局的 WindBot 决策日志**：内核问了什么、脚本选了谁（`become target` / `move to` /
   `activate effect` 这些行），它就在宿主日志里（`duel.bot_debug = true` 时）。

两样都只做"取与压"，不做判断——判断留给模型，落不落地由 `train/patchwork.py` 说了算。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

import json
import re

#: 脚本摘要的长度上限（给模型的"结构"部分）。
#: ⚠ 6000 实测**不够**：真机上 64 条注册 + 23 个 HintMsg 分支就把预算用光，
#: 结果 "HintMsg 列表被截断"——而那正是排查"缺分支"时唯一要看的东西（复盘自己都吐槽过这点）。
#: 摘要长一点没关系：它比源码原文短一个数量级，而源码那一段本来就占大头。
DIGEST_CHARS = 10000

#: 脚本源码原文的长度上限（整份 Kezmo 是 100KB+，全塞进去既慢又没必要）。
SOURCE_CHARS = 14000

#: 决策日志切片的长度上限。
LOG_CHARS = 7000

#: 摘要里最多列多少个卡号常量 / 规则函数。
MAX_CONSTS = 60
MAX_METHODS = 60

#: 决策日志里"值得看"的行：内核的提问与结果、脚本自己的诊断输出、阶段推进。
_LOG_KEEP = (
    "activate effect",
    "become target",
    "move to",
    "Go to ",
    "attack",
    "overlay",
    "deattach",
    "change position",
    "Summon",
    "summon",
    "执行器",
    "诊断",
    "[刻魔异响鸣]",
    "[升辉月]",
    "[耀圣]",
    "negate",
    "无效",
    "destroy",
    "破坏",
    "除外",
    "灵摆",
    "Pendulum",
)

#: 明显是噪声的行（卡牌清单 dump、血量变化、洗牌……）。
_LOG_DROP = (
    "Bot Hand",
    "Bot Spell",
    "Bot Monster",
    "Bot Grave",
    "*********",
    "LifePoint",
    "shuffle",
    "Decks initialized",
    "starting",
    "阻抗决策层",
    "Tick Error",
    "draw ",
    "Draw)",
)


def script_digest(text: str, *, max_chars: int = DIGEST_CHARS) -> str:
    """把一份执行器源码压成"结构摘要"。

    刻意**不评好坏**，只把结构性事实抄出来：规则注册顺序、`HintMsg` 分支清单、override 与
    规则函数签名、卡号常量，最后才是作者写的头部说明。

    ⚠ 顺序是刻意排的（2026-10-09 踩过）：头部文档常常上千字，放前面会把"注册顺序"
    和"HintMsg 分支"挤出预算——而**那两样才是排查"顺序不对/缺分支"时唯一要看的东西**。
    所以按价值从高到低追加，预算用光就停（被丢的是最不关键的那几段）。
    """

    source = str(text or "").lstrip("\ufeff")
    lines = source.splitlines()
    deck_line = next((line.strip() for line in lines if "[Deck(" in line), "")
    class_line = next((line.strip() for line in lines if re.search(r"class\s+\w+Executor", line)), "")

    consts: List[str] = []
    for line in lines:
        match = re.search(r"public const int (\w+)\s*=\s*(\d+);\s*(?://\s*(.*))?", line)
        if match:
            consts.append(f"  {match.group(1)} = {match.group(2)}" + (f"  // {match.group(3).strip()}" if match.group(3) else ""))
        if len(consts) >= MAX_CONSTS:
            break

    registrations: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("AddExecutor(") or stripped.startswith("Executors.Add(") or stripped.startswith("Executors.Insert("):
            # 行尾注释压到 40 字：64 条注册的注释加起来能吃掉一半预算，而
            # "哪条规则排在第几位"才是这一段要传达的事。
            code, _, comment = stripped.partition("//")
            code = code.rstrip()
            comment = comment.strip()[:40]
            registrations.append("  " + code + (f"    // {comment}" if comment else ""))

    hints: List[str] = []
    for line in lines:
        match = re.search(r"case\s+(HintMsg\.\w+)", line)
        if match and match.group(1) not in hints:
            hints.append(match.group(1))

    overrides: List[str] = []
    for line in lines:
        match = re.search(r"public override [\w<>\[\],\s]+\s+(\w+)\(", line)
        if match:
            overrides.append("  " + match.group(1) + "()")

    methods: List[str] = []
    for line in lines:
        match = re.search(r"private bool (\w+)\(", line)
        if match:
            methods.append("  " + match.group(1) + "()")
        if len(methods) >= MAX_METHODS:
            break

    head_doc: List[str] = []
    for line in lines[:160]:
        stripped = line.strip()
        if stripped.startswith("///") or stripped.startswith("//"):
            head_doc.append(stripped)
        elif head_doc:
            break

    # 按"排查时最有用"的顺序追加；预算用光就停。
    sections = (
        ("注册与基类", [deck_line, class_line]),
        ("规则注册顺序（先后＝优先级）", registrations),
        ("OnSelectCard 已处理的 HintMsg（**没列的＝会掉进 default**）", hints),
        ("override 的方法", overrides),
        ("规则函数", methods),
        ("卡号常量", consts),
        ("文件头部说明（作者写的设计意图）", head_doc[:40]),
    )
    digest = ""
    for title, body in sections:
        body = [item for item in body if item]
        if not body:
            continue
        block = f"\n### {title}（{len(body)} 项）\n" + "\n".join(body) + "\n"
        if len(digest) + len(block) <= max_chars:
            digest += block
            continue
        room = max_chars - len(digest)
        if room < 200:
            break
        # 放不下就**只截这一段**（不整段丢）：整段丢了等于没给，模型只能回"这里被截断、
        # 无法判断"——第一版就是这样，HintMsg 列表整段被吞掉，而它正是排查"缺分支"要看的东西。
        kept: List[str] = []
        used = len(f"\n### {title}（{len(body)} 项）\n")
        for item in body:
            if used + len(item) + 1 > room - 80:
                break
            kept.append(item)
            used += len(item) + 1
        if kept:
            digest += (
                f"\n### {title}（共 {len(body)} 项，预算所限只列前 {len(kept)} 项）\n"
                + "\n".join(kept)
                + f"\n（…本段余下 {len(body) - len(kept)} 项未列出）\n"
            )
        break
    return digest.strip()


def parse_host_log(raw_text: str) -> List[Tuple[float, str]]:
    """把宿主 `logs/app_*.log.jsonl` 解析成 ``[(时间戳, 事件正文)]``。

    只保留带 ``timestamp`` 与 ``event`` 的行；解析不动的直接跳过（宿主日志里混着别的格式）。
    """

    entries: List[Tuple[float, str]] = []
    for line in str(raw_text or "").splitlines():
        line = line.strip()
        if not line or "[WindBot" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = record.get("event")
        stamp = record.get("timestamp") or record.get("@timestamp")
        if not isinstance(event, str) or not stamp:
            continue
        seconds = _epoch(stamp)
        if seconds is None:
            continue
        entries.append((seconds, event))
    return entries


def _epoch(stamp: str) -> float:
    """ISO 时间戳 → epoch 秒（容错：解析失败返回 None）。"""

    from datetime import datetime, timezone

    text = str(stamp).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def interesting(line: str) -> bool:
    """这一行值不值得给模型看。"""

    text = str(line or "")
    if any(noise in text for noise in _LOG_DROP):
        return False
    return any(keep in text for keep in _LOG_KEEP)


def decision_log_slice(
    entries: Iterable[Tuple[float, str]],
    *,
    start: float,
    end: float,
    max_chars: int = LOG_CHARS,
) -> str:
    """截出某一局时间窗内的决策日志（过滤噪声 + 限长）。

    超长时保留**前 2/3 与后 1/3**：中线通常断在中后段，尾巴一定要看到。
    """

    picked: List[str] = []
    for seconds, event in entries:
        if start and seconds < start:
            continue
        if end and seconds > end:
            continue
        text = re.sub(r"^\[WindBot\]\s*", "", event).strip()
        if interesting(text):
            picked.append(text)
    if not picked:
        return ""
    joined = "\n".join(picked)
    if len(joined) <= max_chars:
        return joined
    head = joined[: max_chars * 2 // 3]
    tail = joined[-max_chars // 3 :]
    return head + "\n…（中间省略）…\n" + tail
