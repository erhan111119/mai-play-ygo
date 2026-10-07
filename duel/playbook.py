"""卡组打法数据（playbook）：由模型写、由通用执行器读的"这副牌该怎么打"。

**为什么要这个东西**：原先"让模型为每副牌现写一个 C# 执行器"有三个硬伤——要编译
（编译不过就白跑）、会崩（一个空引用就把整局打歪）、写到空壳也看不出来（实测生成过
一张牌都不出的脚本）。改成**写数据**之后：没有编译步骤、读不懂的字段就忽略（退回通用打法）、
坏了最多是"这一步不做"，不会整局不动。

格式沿用计划文件那套 ``key=value``（C# 侧零依赖解析，Python 侧几行就能校验），键的含义：

* ``summon_order``：通常召唤的优先级（想先站场的怪，按顺序）。执行器会**尽量通召**这些卡；
* ``activate_order``：⚠ **只列"只要能用就该发动"的卡**——执行器把它们装成"能发就发"（AlwaysPlay），
  不分场合。实测把看时机的卡（解场、抽滤、按局势的交坑）列进去会**打得更差**
  （同一副牌镜像 30 局：乱列的一侧 7 胜 23 负、动作还更少）。想表达"关键展开件"请用
  ``search_order``（选卡时优先取）与攻略要点，别塞这里；
* ``set_order``：盖放的优先级（陷阱、速攻）；
* ``search_order``：**单选时**优先取哪些卡（检索、选卡；**组合牌最吃这一项**）；
* ``never_activate``：**问 AI 时**提醒"留着"的卡（陷阱/手坑这类容易被骗掉的；
  执行器不读它——只在提示词侧加一行 ``caution=1``，让模型知道要谨慎）；
* ``never_summon``：**别拿它当怪兽用**的卡（手坑这类留在手里才有用的）。
  执行器菜单会把它们列成"召唤/盖放 XXX"，实测模型真会挑（日志里选过「里侧盖放 幽鬼兔」、
  甚至自己回合「发动 增殖的G」）；列在这里，提示词会给那些选项打上标注。同样**只在提示词侧用**。

数值项（``aggression`` / ``hold_handtraps`` / ``prefer_direct``）仍然走 :mod:`train.plan`，
这里只管"卡与卡之间的取舍"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import os
import time

# 打法数据文件名（与 C# 执行器的默认值一致；实际用哪个路径由 ``PlaybookFile=`` 决定）
PLAYBOOK_FILE_NAME = "MaiBotPlaybook.txt"

# 替换被读端挡住时的重试次数与间隔（Windows 上 .NET 读文件会短暂禁止替换）
_REPLACE_RETRIES = 10
_REPLACE_RETRY_DELAY = 0.02

# 允许的键（执行器只认这几个；多写的会被忽略，便于以后加字段不破坏兼容）
FIELD_KEYS: Tuple[str, ...] = (
    "summon_order",
    "activate_order",
    "set_order",
    "search_order",
    "never_activate",
    # "别拿它当怪兽用"：手坑这类**留在手里才有用**的卡。执行器的菜单会把它们列成
    # "召唤/盖放 XXX"，实测模型真会挑（日志里选过「里侧盖放 幽鬼兔」，
    # 甚至自己回合「发动 增殖的G」）。列在这里，提示词会给那些选项打上
    # "别拿它当怪兽用"的标注。**执行器（C#）不读这个键**，只在提示词侧用。
    "never_summon",
)

# 每个列表最多几张：太长说明模型在瞎填，执行器也不会用这么多
MAX_ENTRIES = 16


def write_playbook_file(target: Path, text: str) -> Path:
    """原子地写入打法数据文件，返回写入路径。

    先写 ``.tmp`` 再 ``os.replace``：执行器随时可能读这个文件，半截内容比"旧数据"糟糕得多。
    Windows 上 .NET 的 ``File.ReadAllLines`` 打开文件时不允许被替换，撞上时短暂重试；
    重试完仍失败就抛出来——写不进去必须让人看见。

    **放在这个包里（而不是 tools/ 或 train/）**：插件自己的模块只能相对导入同包内的东西，
    宿主的运行器不会把插件根目录加进 ``sys.path``，导 ``train.*`` 会让整个插件加载失败
    （实测踩过：`加载插件失败 … No module named 'train'`）。
    """

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    last_error: Optional[PermissionError] = None
    for _attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, target)
            return target
        except PermissionError as exc:
            last_error = exc
            time.sleep(_REPLACE_RETRY_DELAY)
    raise RuntimeError(
        f"写打法数据失败：{target} 被占用（已重试 {_REPLACE_RETRIES} 次）"
    ) from last_error


class PlaybookError(ValueError):
    """打法数据不合法（键名、卡号、条数）。"""


@dataclass
class Playbook:
    """一副牌的打法数据。

    Attributes:
        lists: ``{键: [卡号, ...]}``，键取自 :data:`FIELD_KEYS`。
        source: 出处（``model`` / ``hand`` / ...），只用于日志与排查。
    """

    lists: Dict[str, List[int]] = field(default_factory=dict)
    source: str = ""

    def get(self, key: str) -> List[int]:
        """取某个键的列表（没有就是空列表）。"""

        return list(self.lists.get(key, ()))

    def is_empty(self) -> bool:
        """一个字段都没有时视为"没有打法数据"。"""

        return not any(self.lists.get(key) for key in FIELD_KEYS)


def parse_playbook(text: str, *, allowed_cards: Sequence[int] = ()) -> Playbook:
    """解析 ``key=value`` 文本；不认识的键忽略，值里的非法卡号直接报错。

    **为什么非法卡号要报错而不是忽略**：卡号写错（比如把 ``14558127`` 写成 ``1455812``）
    在执行器那边表现为"这张牌永远找不到"，整段组合静默失效——那正是我们要避免的
    "看起来做了、其实没做"。所以宁可在这里拒绝整份数据。

    Args:
        text: 文件内容或模型输出。
        allowed_cards: 允许出现的卡号（一般是这副牌的卡表 + 卡库）；空表示不校验。
    """

    allowed = {int(card) for card in allowed_cards}
    lists: Dict[str, List[int]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise PlaybookError(f"这一行不是 key=value：{stripped}")
        key, raw = stripped.split("=", 1)
        key = key.strip().lower()
        if key not in FIELD_KEYS:
            continue  # 多写的键直接忽略：以后加字段不会让旧执行器报错
        cards: List[int] = []
        for item in raw.replace("，", ",").split(","):
            item = item.strip()
            if not item:
                continue
            if not item.lstrip("-").isdigit():
                raise PlaybookError(f"{key} 里不是卡号：{item}")
            card = int(item)
            if card <= 0:
                raise PlaybookError(f"{key} 里的卡号非法：{card}")
            if allowed and card not in allowed:
                raise PlaybookError(f"{key} 里的卡号 {card} 不在卡库里（写错了卡号）")
            if card not in cards:
                cards.append(card)
        if len(cards) > MAX_ENTRIES:
            raise PlaybookError(f"{key} 写了 {len(cards)} 张，超过上限 {MAX_ENTRIES}")
        if cards:
            lists[key] = cards
    return Playbook(lists=lists)


def to_text(playbook: Playbook) -> str:
    """反向写回 ``key=value`` 文本（保存与人工查看用）。"""

    lines: List[str] = []
    for key in FIELD_KEYS:
        cards = playbook.lists.get(key)
        if cards:
            lines.append(f"{key}=" + ",".join(str(card) for card in cards))
    return "\n".join(lines) + ("\n" if lines else "")


def missing_keys(playbook: Playbook, required: Iterable[str]) -> List[str]:
    """哪些要求的字段是空的（用于给模型的提示、以及日志里如实说明）。"""

    return [key for key in required if not playbook.lists.get(key)]
