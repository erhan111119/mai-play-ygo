"""卡组进化搜索的纯逻辑：候选换牌、上线前体检、接受判定、牌序排期。

**为什么单独一层**：搜索本身要跑几百上千局，没法靠"跑一遍看看"来验证；而"能不能信这批
结果"恰恰取决于这里的判定口径（用什么标准算"变强了"、每个候选拿到哪些牌序、换进来的卡
本机认不认得）。所以把决定性逻辑抽出来，让它能单测。

三条硬规矩（都是实测教训）：

1. **每个候选必须换一套牌序**：训练默认关洗切（牌序＝文件顺序），如果所有候选共用同一批
   牌序，卡组会"背下这几个开局"，赢的是过拟合而不是实力；
2. **换进来的卡必须先体检**：本机卡库缺卡、脚本缺失、同名超过 3 张，都会让候选"看起来变强"
   其实是"根本没打起来"（实锤过：35 张先行卡装不上，只剩艾克佐迪亚直接特殊胜利）；
3. **接受判定用区间不用百分比大小**：40 局的 62% 只是 1.6σ，什么也证明不了。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import math
import random


@dataclass(frozen=True)
class SwapProposal:
    """一次换牌：换出这些卡、换进这些卡（各若干张）。"""

    remove: Tuple[int, ...]
    add: Tuple[int, ...]
    reason: str = ""

    def describe(self) -> str:
        """给日志/群消息用的一行描述。"""

        out = "、".join(str(card) for card in self.remove) or "（无）"
        into = "、".join(str(card) for card in self.add) or "（无）"
        tail = f"（{self.reason}）" if self.reason else ""
        return f"换出 {out} → 换入 {into}{tail}"


def seed_schedule(base_seed: int, candidate_index: int, rounds: int) -> List[int]:
    """给第 N 个候选排一批**互不相同**的洗牌种子。

    避开"所有候选共用同一批牌序"的过拟合：同一个候选内部各轮也不同，跨候选也不重叠
    （用候选序号做偏移，简单且可复现）。
    """

    offset = candidate_index * 1000
    return [base_seed + offset + index for index in range(max(1, rounds))]


def missing_scripts(
    deck: Sequence[int], *, known_cards: Sequence[int], scripted_cards: Sequence[int]
) -> List[int]:
    """挑出"卡库里有、但本机查不到脚本"的卡号。

    没脚本的卡在游戏里既不会崩、也没有效果，等于白占卡位。这条规则被两个地方用：候选卡表
    体检（硬性，直接毙掉）、主体卡组体检（只警告——牌子是群友投的，不该因为几张白板就不让它
    被优化）。
    """

    known = set(known_cards)
    scripted = set(scripted_cards)
    return sorted({card for card in deck if card in known and card not in scripted})


def deck_issues(
    deck: Sequence[int],
    *,
    known_cards: Sequence[int],
    scripted_cards: Sequence[int],
    max_copies: int = 3,
    min_main: int = 40,
    skip_script_check: bool = False,
) -> List[str]:
    """上线前体检：返回问题清单（空列表＝这副牌可以拿去打）。

    Args:
        deck: 主卡组卡号（可重复，重复表示同名的多张）。
        known_cards: 本机卡库里有的卡号。
        scripted_cards: 本机有卡片脚本的卡号（没脚本＝在游戏里没效果）。
        max_copies: 同名卡上限（默认 3）。
        min_main: 主卡组张数下限（默认 40）。
        skip_script_check: 跳过"缺脚本"这一条（主体卡组用，缺脚本只警告不拦）。
    """

    known = set(known_cards)
    scripted = set(scripted_cards)
    issues: List[str] = []
    if len(deck) < min_main:
        issues.append(f"主卡组只有 {len(deck)} 张（少于 {min_main}）")
    unknown = sorted({card for card in deck if card not in known})
    if unknown:
        issues.append(f"卡库里没有这些卡：{unknown[:6]}")
    if not skip_script_check:
        absent = missing_scripts(deck, known_cards=known, scripted_cards=scripted)
        if absent:
            # 没脚本的卡在游戏里是白板：不会崩，但等于把卡位白白送掉
            issues.append(f"这些卡没有卡片脚本（会变成白板）：{absent[:6]}")
    counts: Dict[int, int] = {}
    for card in deck:
        counts[card] = counts.get(card, 0) + 1
    too_many = sorted(card for card, count in counts.items() if count > max_copies)
    if too_many:
        issues.append(f"同名超过 {max_copies} 张：{too_many[:6]}")
    return issues


def wilson_interval(wins: int, total: int, *, z: float = 1.96) -> Tuple[float, float]:
    """胜率的 Wilson 95% 置信区间（小样本时不会越界，判定一律用它）。"""

    if total <= 0:
        return (0.0, 1.0)
    phat = wins / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def rounds_needed(effect: float, *, z: float = 1.96) -> int:
    """要判定 ``effect`` 这么大的胜率差别，每组需要多少局。"""

    if effect <= 0:
        return 0
    return int(math.ceil((z * z * 0.25) / (effect * effect)))


def accept_candidate(
    baseline_wins: int,
    baseline_total: int,
    candidate_wins: int,
    candidate_total: int,
) -> Tuple[bool, str]:
    """候选值不值得留下：只有"95% 区间整体高于基线"才算赢。

    **不能比两个百分比的大小**：40 局的 62% 与 50% 的差别在噪声里（1.6σ），
    照那样挑出来的"改进版"只是恰好运气好，下一批复测就掉回去。

    Returns:
        ``(是否接受, 说明)``。
    """

    if baseline_total <= 0 or candidate_total <= 0:
        return False, "局数为 0，没法判定"
    base_low, base_high = wilson_interval(baseline_wins, baseline_total)
    cand_low, cand_high = wilson_interval(candidate_wins, candidate_total)
    base_rate = baseline_wins / baseline_total
    cand_rate = candidate_wins / candidate_total
    if cand_low > base_high:
        return True, (
            f"候选 {cand_rate:.1%}（{cand_low:.1%}~{cand_high:.1%}）整体高于"
            f"基线 {base_rate:.1%}（{base_low:.1%}~{base_high:.1%}），接受"
        )
    return False, (
        f"候选 {cand_rate:.1%}（{cand_low:.1%}~{cand_high:.1%}）与"
        f"基线 {base_rate:.1%}（{base_low:.1%}~{base_high:.1%}）分不开，不采纳。"
        f"要判定 5% 的差别，每组约需 {rounds_needed(0.05)} 局"
    )


def _order_pool(pool: Sequence[int], preferred: Sequence[int]) -> List[int]:
    """把换入池排成"优先那些最近对局里真的会被打出来的牌"，其余保持原顺序。

    **为什么要有这个偏好**：实测把一张从没被打出来的牌换成另一张同样从没被打出来的牌，
    结果是"换了等于没换"——牌表动了，机器人打出来的东西一点没变，看起来就像优化没做事。
    会出场的牌至少让改动可观测（也是后续用胜率判定的前提）。

    这是**偏好不是硬条件**：preferred 为空的池子原样返回，池里没出现过的牌仍然排在后面可选
    （老牌可能只是还没被抽到过）。
    """

    if not preferred:
        return list(pool)
    rank = {card: index for index, card in enumerate(preferred)}
    order = {card: index for index, card in enumerate(pool)}
    return sorted(pool, key=lambda card: (rank.get(card, len(rank)), order[card]))


def propose_swaps(
    deck: Sequence[int],
    *,
    unused: Sequence[int],
    candidates: Sequence[int],
    count: int,
    rng: random.Random,
    swap_size: int = 2,
    max_copies: int = 3,
    preferred: Sequence[int] = (),
) -> List[SwapProposal]:
    """生成 ``count`` 个换牌候选：优先换掉"整局没用过"的卡，换入候选池里的卡。

    Args:
        deck: 当前主卡组（可重复）。
        unused: 数据驱动信号——整局下来从没被用过的卡号（最该换掉的一批）。
        candidates: 换入候选池（同系列但不在卡组里的卡；由调用方查卡库给出）。
        count: 生成几个候选。
        rng: 随机数发生器（固定种子可复现）。
        swap_size: 每个候选换几张。
        max_copies: 同名上限（换入时不能超过）。
        preferred: 优先换入的卡号（最近对局里真的会出场的牌），见 :func:`_order_pool`。
    """

    pool = _order_pool(
        [card for card in dict.fromkeys(candidates) if card not in set(deck)], preferred
    )
    if not pool:
        return []
    # 换出优先级：没用过的卡靠前；不够就补上"张数最多"的卡（同名 3 张里拿掉 1 张）
    removable: List[int] = [card for card in dict.fromkeys(unused) if card in set(deck)]
    counts: Dict[int, int] = {}
    for card in deck:
        counts[card] = counts.get(card, 0) + 1
    for card in sorted(counts, key=lambda item: (-counts[item], item)):
        if len(removable) >= count * swap_size:
            break
        if card not in removable:
            removable.append(card)
    if not removable:
        return []

    unused_cards = [card for card in dict.fromkeys(unused) if card in set(deck)]
    proposals: List[SwapProposal] = []
    for index in range(max(1, count)):
        size = min(swap_size, len(removable), len(pool))
        # 每个候选**至少换掉一张"整局没用过"的卡**：这是数据给的最强信号，
        # 不能靠均匀随机抽把它稀释掉（那和不看数据没区别）
        forced = unused_cards[index % len(unused_cards)] if unused_cards else removable[0]
        rest = [card for card in removable if card != forced]
        remove = {forced}
        if size > 1 and rest:
            remove |= set(rng.sample(rest, min(size - 1, len(rest))))
        remove = tuple(sorted(remove))
        add_pool = [card for card in pool if card not in remove]
        add = tuple(sorted(rng.sample(add_pool, min(len(remove), len(add_pool)))))
        if not add:
            continue
        reason = "换掉没被用过的卡" if forced in unused_cards else "换掉占比最高的卡"
        proposals.append(SwapProposal(remove=remove, add=add, reason=reason))
    return proposals


def prune_for_dead_cards(
    deck: Sequence[int],
    unused: Sequence[int],
    candidates: Sequence[int],
    *,
    games: int,
    min_games: int = 20,
    max_copies: int = 3,
    preferred: Sequence[int] = (),
) -> Optional[SwapProposal]:
    """按"整局从没被用过"判死牌：证据够就把占用卡位最多的那张整组换掉。

    **为什么这条不用胜率判定**：要证明 5% 的差别得每组几百局（实测 40 局的 62% 只有 1.6σ），
    所以按区间判定的话，几十局的搜索永远只会回答"证据不足"，等于什么都不做。而"这张卡在 N
    局里一次都没被用过"是**直接观察**、不是统计推断——把白占卡位的牌换成同系列里能用的牌，不
    需要先赢一局来证明。代价是它只清理死牌，不能证明"变强了"，所以理由里必须写明这一点。

    Args:
        deck: 当前主卡组（可重复）。
        unused: 整局没被用过的卡号。
        candidates: 换入候选池（同系列、不在卡组里的卡）。
        games: ``unused`` 背后的样本量（局数）。
        min_games: 样本量下限，不够就不动（2 局的"没用过"什么也说明不了）。
        max_copies: 同名卡上限。
        preferred: 优先换入的卡号（最近对局里真的会出场的牌），见 :func:`_order_pool`。

    Returns:
        换牌提案；证据不足、没有死牌、或没卡可换入时返回 ``None``。
    """

    if games < min_games:
        return None
    pool = _order_pool(
        [card for card in dict.fromkeys(candidates) if card not in set(deck)], preferred
    )
    dead = [card for card in dict.fromkeys(unused) if card in set(deck)]
    if not dead or not pool:
        return None
    counts: Dict[int, int] = {}
    for card in deck:
        counts[card] = counts.get(card, 0) + 1
    # 先清占用卡位最多的那张死牌（换出的位子最多），同张数时取卡号小的，保证可复现
    victim = sorted(dead, key=lambda card: (-counts.get(card, 0), card))[0]
    copies = min(counts.get(victim, 0), len(pool), max_copies)
    if copies <= 0:
        return None
    # 换入的是不是"机器人真会打出来"的牌，要在理由里说清：不然读者会以为只是把牌换了个名字
    swapped_in = pool[:copies]
    note = "，换入的是最近对局里真会出场的牌" if any(card in set(preferred) for card in swapped_in) else ""
    return SwapProposal(
        remove=tuple([victim] * copies),
        add=tuple(swapped_in),
        reason=f"最近 {games} 局一次没动，按死牌清理换掉 {copies} 张{note}",
    )


def apply_swap(deck: Sequence[int], proposal: SwapProposal, *, max_copies: int = 3) -> List[int]:
    """把一次换牌应用到卡表上（换出各一张、换进各一张，保持张数不变）。"""

    result = list(deck)
    for card in proposal.remove:
        if card in result:
            result.remove(card)
    counts: Dict[int, int] = {}
    for card in result:
        counts[card] = counts.get(card, 0) + 1
    for card in proposal.add:
        if counts.get(card, 0) >= max_copies:
            continue
        result.append(card)
        counts[card] = counts.get(card, 0) + 1
    return sorted(result)
