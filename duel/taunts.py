"""局内挑衅台词：对局进行中按概率挑一句话，用麦麦的身份发进游戏里。

台词池在这里 :data:`TAUNT_LINES`（内置 20 句），也可以用插件配置里的
``duel.taunt_lines`` 覆盖（**集合类型**，一条一句；留空＝用内置这套）——
2026-10-07 用户要求"具体说什么"要能自己改，所以它从"只能改代码"变成了配置项。

发送走的是闸门：往「bot → 内核」那条连接里写一条 ``CTOS_CHAT``，效果与麦麦自己在客户端
里打字一样，内核会广播给房间里其他人（详见 :meth:`duel.gate.DuelGate.send_chat`）。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import random

# 挑衅台词池。保持「嘴硬但不脏」的调子：这是打牌时的垃圾话，不是人身攻击。
TAUNT_LINES: List[str] = [
    "就这？我还没热身呢",
    "你的回合能不能快点，我快睡着了",
    "这一手我早就看穿了",
    "嗯…有点意思，但也只是有点",
    "抽卡运不错嘛，可惜对手是我",
    "别急，我还没认真",
    "这波操作，我给你的勇气点个赞",
    "我的回合会更精彩的，你等着",
    "手牌是不是卡住了？我这个外行都看出来了",
    "场上是有点危险，不过危险的是你",
    "你确定要这么打吗？我可不提醒第二次",
    "再来一局也是一样的结果哦",
    "哎呀，是不是算错了？",
    "我这边全是好牌，你要不要认输",
    "节奏在我手里，你慢慢想",
    "打得不错，可惜赢不了",
    "你这套牌我见过，结果没变过",
    "别紧张，深呼吸，反正结局都一样",
    "下一张抽什么不重要，重要的是我更强",
    "再拖下去天都要黑了，快点出招",
]


class TauntPicker:
    """挑挑衅台词：随机但不连着重复上一句。

    刻意用普通的 ``random`` 而不是 ``secrets``：这是游戏嘴炮，要的是「看着随机」，
    不涉及任何凭据或不可预测性要求；``rng`` 参数只为测试注入固定种子。
    """

    def __init__(self, lines: Optional[Sequence[str]] = None, *, rng: Optional[random.Random] = None) -> None:
        self._lines: List[str] = list(lines if lines is not None else TAUNT_LINES)
        self._rng = rng or random.Random()
        self._last = ""

    def pick(self) -> str:
        """挑一句台词；池子为空时返回空串。"""

        if not self._lines:
            return ""
        if len(self._lines) == 1:
            self._last = self._lines[0]
            return self._last
        # 连着说同一句最出戏，这里最多重试几次避开上一句
        for _ in range(8):
            candidate = self._rng.choice(self._lines)
            if candidate != self._last:
                self._last = candidate
                return candidate
        return self._last

    def should_taunt(self, chance_per_second: float) -> bool:
        """按「每秒概率」掷一次骰子。"""

        if chance_per_second <= 0:
            return False
        if chance_per_second >= 1:
            return True
        return self._rng.random() < chance_per_second
