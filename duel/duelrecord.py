"""一局对局的结果（写进结果库、也用于播报）。

**为什么单独放一个文件**：它是纯数据、没有任何依赖，而**插件**与**工具**都要用它——
插件只能在包内相对导入（``from .duel.duelrecord import DuelOutcome``），
工具则用绝对导入（``from duel.duelrecord import DuelOutcome``）。
原来它定义在 ``train/arena.py`` 里，那个文件用的是绝对导入（``from duel.knowledge import …``），
插件一旦 import 它就会按包加载失败（护栏测试抓过：``插件自己的模块只能用相对导入``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class DuelOutcome:
    """一局的结果，准备写进库里。"""

    left: str
    right: str
    left_seat: int
    winner: str
    """胜者名字（``left``/``right``/``draw``/``unknown``）。"""
    turns: int
    duration_seconds: int
    reason: str
    actions_left: int
    actions_right: int
    lp_left: int
    lp_right: int
    error: str = ""
    sp_summons_left: int = 0
    """左方这局**特殊召唤**了几次。"""
    sp_summons_right: int = 0
    """右方这局特殊召唤了几次。

    为什么要单独记它：**"能不能展开"靠胜率是看不出来的**——用户报的是"脚本根本无法正常展开"
    （一张额外怪都没做出来），而胜率只告诉你输赢。特召次数与效果次数才是"有没有动起来"的直接指标。
    """
    effects_left: int = 0
    """左方这局发动了几次效果。"""
    effects_right: int = 0
    plans: str = ""
    """这局下过的计划（一行一条）；没有教练或没写成功时为空。"""
    round_index: int = -1
    """第几轮镜像（= 洗牌种子，见 :func:`train.arena.shuffle_deck`）；-1 表示不是擂台跑的。

    记下来是为了便于对照。⚠ **"同一轮就能原样重跑"只在不洗切时才成立**：内核的洗切
    （真实房间 / ``--shuffle``）不受我们的种子控制，实测同 base 重跑两遍开局手牌一张都不重合
    （见 :attr:`train.arena.ArenaConfig.shuffle_seed_base`）。
    """
    sets_left: int = 0
    """左方这局盖放了几次（怪兽里侧 + 魔陷）。

    为什么要拆出来记：**动作总数里已经含盖放**（``train/arena.py`` 的 ``actions()``），
    只有拆开才分得清"这局 2 个动作"是"盖了 2 张牌"还是"召唤 1 只 + 发 1 个效果"。
    """
    sets_right: int = 0
    card_usage: Dict[int, int] = field(default_factory=dict)
    """这局**观测方（我方）**各张卡被用到的次数（召唤/特殊召唤/反转召唤/发动效果/盖放），
    卡组进化搜索的输入。

    搜索"该换掉哪张卡"最可靠的信号就是它：卡在卡组里但整局从没被用过 ⇒ 大概率是死牌。
    ⚠ 2026-10-07 起只含我方——以前是双方混记的一张表，"对手打过的同一张卡"会被算成
    我们也用过（实测 r24~r27 里 15 局是"只有对手带这张卡却进了台账"）。
    """
    card_usage_opponent: Dict[int, int] = field(default_factory=dict)
    """**对手**这局各张卡被用到的次数（复盘"对面到底交了什么"时用）。"""

    card_seen: Dict[int, int] = field(default_factory=dict)
    """**左方**这局各张卡**进过手牌**的次数（抽到 + 被检索/回收进手）。

    它是"该不该加第几张"的分母：把使用率拆成"到手就能用"与"到手了也用不上"。
    ⚠ 只有 recorder 观测的那一方是完整的（对手手牌是隐藏信息，那些报文的卡号是 0），
    擂台按左右映射落库，所以另一侧的那份基本是空的——对手那份见下。
    """

    card_seen_opponent: Dict[int, int] = field(default_factory=dict)
    """**右方**各张卡进过手牌的次数（通常为空：对手手牌在客户端侧看不见）。"""

    @property
    def decided(self) -> bool:
        """这一局是否分出了胜负。"""

        return self.winner in (self.left, self.right)
