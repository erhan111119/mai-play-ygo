"""作战计划：AI 每回合给麦麦下的"战术指令"，以及让它落到 WindBot 手里的文件协议。

**为什么要这样一个协议**：WindBot 是独立进程，插件/AI 没法直接调它的函数。能用的通道
只有文件——所以约定一个极简的文本格式，由"教练"（AI 或规则）写、由计划感知执行器
（``train/windbot/PlanAwareExecutor.cs``）在每次决策前读。选 ``key=value`` 而不是 JSON，
是因为 C# 那边读起来零依赖（WindBot 只引用了 System.*，没有 JSON 库），Python 侧写也就
几行；字段少的时候这个格式反而更不容易出错。

**写入必须原子**：执行器随时可能读这个文件，如果它读到只写了一半的内容，整局行为都可能
跑偏。所以先写临时文件再 ``os.replace`` 顶替（同分区上是原子的）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import os
import time

# 计划文件名：放在 WindBot 工作目录下，执行器按相对路径读
# （卡组打法数据是另一份文件，见 duel/playbook.py：它不该过期，所以不跟计划混在一起）
PLAN_FILE_NAME = "MaiBotPlan.txt"

# 替换被读端挡住时的重试次数与间隔（Windows 上 .NET 读文件会短暂禁止替换）
_REPLACE_RETRIES = 10
_REPLACE_RETRY_DELAY = 0.02

# 允许的键（执行器只认这几个；多写的会被忽略，便于以后加字段不破坏兼容）
_KEY_AGGRESSION = "aggression"
_KEY_HOLD_HANDTRAPS = "hold_handtraps"
_KEY_PREFER_DIRECT = "prefer_direct"
_KEY_TURN = "turn"
_KEY_NOTES = "notes"


@dataclass
class DuelPlan:
    """一份作战计划。

    Attributes:
        aggression: 进攻倾向 0~1。越高越倾向于用更多怪兽攻击、越愿意打对拼；
            0 表示能不打就不打（保场）。
        hold_handtraps: 是否留着阻止类卡片（手坑）不用。等对面关键展开再交。
        prefer_direct: 是否优先直接攻击（打脸）而不是清场。
        turn: 这份计划是给第几回合用的（回合数，从 1 开始；0 表示不限）。
        notes: 给日志看的备注（执行器不用它做决策）。
    """

    aggression: float = 0.5
    hold_handtraps: bool = False
    prefer_direct: bool = False
    turn: int = 0
    notes: str = ""

    def clamped(self) -> "DuelPlan":
        """把数值收进合理范围，避免手写配置或模型输出越界。"""

        return DuelPlan(
            aggression=min(1.0, max(0.0, float(self.aggression))),
            hold_handtraps=bool(self.hold_handtraps),
            prefer_direct=bool(self.prefer_direct),
            turn=max(0, int(self.turn)),
            notes=" ".join(str(self.notes).split())[:200],
        )

    def to_text(self) -> str:
        """序列化成执行器能读的 ``key=value`` 文本。"""

        plan = self.clamped()
        lines = [
            f"{_KEY_AGGRESSION}={plan.aggression:.2f}",
            f"{_KEY_HOLD_HANDTRAPS}={1 if plan.hold_handtraps else 0}",
            f"{_KEY_PREFER_DIRECT}={1 if plan.prefer_direct else 0}",
            f"{_KEY_TURN}={plan.turn}",
            f"{_KEY_NOTES}={plan.notes}",
        ]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_text(cls, text: str) -> "DuelPlan":
        """解析 ``key=value`` 文本；无法识别的行直接忽略。"""

        values: Dict[str, str] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, _, raw = line.partition("=")
            values[key.strip().lower()] = raw.strip()
        return cls(
            aggression=float(values.get(_KEY_AGGRESSION, 0.5) or 0.5),
            hold_handtraps=values.get(_KEY_HOLD_HANDTRAPS, "0") not in ("0", "", "false", "False"),
            prefer_direct=values.get(_KEY_PREFER_DIRECT, "0") not in ("0", "", "false", "False"),
            turn=int(values.get(_KEY_TURN, 0) or 0),
            notes=values.get(_KEY_NOTES, ""),
        )


def plan_path(windbot_dir: Path) -> Path:
    """计划文件的位置（执行器按这个名字在它的工作目录里找）。"""

    return Path(windbot_dir) / PLAN_FILE_NAME


def write_plan(target: Path, plan: DuelPlan) -> Path:
    """原子地写入计划，返回写入路径。

    先写 ``.tmp`` 再 ``os.replace``：执行器可能正好在读这个文件，
    半截内容比"旧计划"糟糕得多。

    Windows 上还有一层麻烦：.NET 的 ``File.ReadAllLines`` 打开文件时不允许被替换，
    执行器每几次决策就会读一次，撞上时 ``os.replace`` 抛 ``PermissionError``。
    所以这里短暂重试；重试完仍失败就抛出来——写不进去必须让人看见，
    不能悄悄丢掉，否则"教练下了计划但没生效"会变成无迹可循的怪事。

    Args:
        target: **文件路径**（擂台给每个对局一个独立文件；固定名字用 :func:`plan_path`）。
    """

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(plan.to_text(), encoding="utf-8")
    last_error: Optional[PermissionError] = None
    for _attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, target)
            return target
        except PermissionError as exc:
            last_error = exc
            time.sleep(_REPLACE_RETRY_DELAY)
    raise RuntimeError(
        f"写计划失败：{target} 被占用（已重试 {_REPLACE_RETRIES} 次）"
    ) from last_error


def read_plan(target: Path) -> Optional[DuelPlan]:
    """读回计划；文件不存在或读不动时返回 None。"""

    try:
        return DuelPlan.from_text(Path(target).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def clear_plan(target: Path) -> None:
    """删掉计划文件（下一局开始前调用，避免上一局的战术串场）。"""

    Path(target).unlink(missing_ok=True)


@dataclass
class PlanLog:
    """记录一局里写过的计划，便于复盘「AI 到底下了什么指令」。"""

    entries: list = field(default_factory=list)

    def add(self, plan: DuelPlan, *, source: str) -> None:
        """记一条。"""

        self.entries.append(
            {"turn": plan.turn, "aggression": plan.aggression,
             "hold_handtraps": plan.hold_handtraps, "prefer_direct": plan.prefer_direct,
             "notes": plan.notes, "source": source, "at": time.time()}
        )

    def summary(self) -> str:
        """一行一行的可读摘要。"""

        if not self.entries:
            return "（这局没有下过计划）"
        return "\n".join(
            f"  第 {item['turn']} 回合：aggression={item['aggression']:.2f}"
            f" hold_handtraps={item['hold_handtraps']} prefer_direct={item['prefer_direct']}"
            f"（{item['source']}）{('｜' + item['notes']) if item['notes'] else ''}"
            for item in self.entries
        )
