"""训练功能里"要问模型"的那两块。

**一、combo 推演**（:func:`derive_combo`）：读卡表 + 卡文，让模型写出这副牌要做什么、
先手/后手各走哪条线、用到哪些卡。刻意做了两件事：

* **卡文由插件从卡库读出来喂给它**，不让模型凭记忆写效果——本机的实测教训是"卡文与效果的
  存在性必须自己核"（模型会理直气壮地说某张卡有它没有的效果）；
* **输出的卡名要过一遍卡表**：模型说的每张卡都必须在卡表里，不在的就从结构化字段里剔掉
  并记一条 warning。这条检查只能抓"结构化字段"里的卡名（`hand` / `cards`），正文里提到什么
  抓不住——所以 warning 是给人看的，不是"已经干净了"的证明。

**二、给结果写结论**（:func:`summarise_run`）：擂台/体检/复盘跑完是一大堆 stdout，
让模型读尾部若干行写一段"这次结论能信到什么程度"。提示词把两条纪律写死了：
局数不够时只能说"机制没坏"，不许说谁强；以及没跑起来（缺文件、进程崩）要说出来。

**这里的函数只管拼提示词与解析回复**：发请求的活儿由调用方传进来的 `generate` 承担
（超时也在那一层执行），这样 `train/` 不需要依赖插件宿主 SDK。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import json
import logging
import re

#: combo 推演的答复额度。要写先手/后手两条线 + 每步说明 + JSON 外壳，给小了会被截断成坏 JSON。
COMBO_MAX_TOKENS = 8192

#: 写结论的答复额度（一段话，不需要长）。
SUMMARY_MAX_TOKENS = 2048

#: 喂给模型的卡文上限：单张效果截断长度、整副卡表的字符上限。
MAX_EFFECT_CHARS = 400
MAX_DIGEST_CHARS = 24000

#: 写结论时给模型看多少行 stdout。
OUTPUT_TAIL_LINES = 200


class AnalysisError(RuntimeError):
    """需要模型的分析没能完成（模型不可用、回复为空、超时）。"""


@dataclass
class ComboResult:
    """combo 推演的结果。"""

    guide: Dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    digest: str = ""
    warnings: List[str] = field(default_factory=list)
    model: str = ""

    def text(self) -> str:
        """把推演渲染成一段可以直接读/存档的文本。"""

        lines: List[str] = []
        summary = str(self.guide.get("summary") or "").strip()
        if summary:
            lines.append(f"【这副牌想做什么】{summary}")
        for item in self.guide.get("lines") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "（未命名）")
            hand = "、".join(str(x) for x in (item.get("hand") or []))
            cards = "、".join(str(x) for x in (item.get("cards") or []))
            lines.append(f"\n■ {name}")
            if hand:
                lines.append(f"  起手：{hand}")
            if cards:
                lines.append(f"  用到：{cards}")
            for index, step in enumerate(item.get("steps") or [], start=1):
                lines.append(f"  {index}. {step}")
        notes = self.guide.get("notes") or []
        if notes:
            lines.append("\n【要点】")
            lines.extend(f"  - {note}" for note in notes)
        uncertain = self.guide.get("uncertain") or []
        if uncertain:
            lines.append("\n【待人工确认】")
            lines.extend(f"  - {note}" for note in uncertain)
        if self.warnings:
            lines.append("\n【机械校验发现的疑点】")
            lines.extend(f"  - {warn}" for warn in self.warnings)
        return "\n".join(lines).strip() or self.raw


# ---------------------------------------------------------------------------
# 卡表 → 给模型看的摘要
# ---------------------------------------------------------------------------


def deck_card_ids(main: Iterable[int], extra: Iterable[int], side: Iterable[int] = ()) -> List[int]:
    """把三个分区合成一份去重的卡号列表（顺序保持：主 → 额外 → 副）。"""

    seen: Set[int] = set()
    ordered: List[int] = []
    for card_id in list(main) + list(extra) + list(side):
        if card_id in seen:
            continue
        seen.add(card_id)
        ordered.append(card_id)
    return ordered


def build_deck_digest(
    deck_name: str,
    card_ids: Sequence[int],
    card_db: Any,
    *,
    group_counts: Optional[Dict[str, int]] = None,
) -> Tuple[str, Set[str], List[str]]:
    """把一副卡表渲染成"卡名 + 卡文"的清单。

    Args:
        deck_name: 卡组名（只用于开头那行）。
        card_ids: 卡号（去重后）。
        card_db: :class:`duel.cards.CardDatabase` 实例，用来取卡名与卡文。
        group_counts: 每个分区各多少张（有就给，让模型知道"这几张是主卡组"）。

    Returns:
        tuple: ``(摘要文本, 卡表里的卡名集合, 警告列表)``。
        卡库里查不到的卡会用 ``卡号 <id>`` 占位并记一条警告——那种卡模型没法推理，
        必须让人看见。
    """

    warnings: List[str] = []
    details: Dict[int, Any] = {}
    if card_db is not None and getattr(card_db, "available", False):
        try:
            details = card_db.card_details(list(card_ids))
        except Exception as exc:  # noqa: BLE001  卡库坏掉不该让推演整个失败
            warnings.append(f"卡库读取失败（{exc}）：这一轮只能按卡号推演，卡文缺失")
            details = {}

    header = [f"# 卡组：{deck_name}"]
    if group_counts:
        header.append(
            "分区张数："
            + "、".join(f"{zone} {count} 张" for zone, count in group_counts.items())
        )
    header.append(f"卡表里共有 {len(card_ids)} 种卡（去重后）")
    header.append("\n# 卡表与卡文")

    known_names: Set[str] = set()
    unknown_ids: List[int] = []
    blocks: List[str] = []
    used = len("\n".join(header))
    for card_id in card_ids:
        detail = details.get(int(card_id))
        if detail is None:
            unknown_ids.append(int(card_id))
            blocks.append(f"- 卡号 {card_id}（卡库不认识这张卡）")
            continue
        known_names.add(str(detail.name))
        effect = re.sub(r"\s+", " ", str(detail.effect or "")).strip()
        if len(effect) > MAX_EFFECT_CHARS:
            effect = effect[:MAX_EFFECT_CHARS] + "…（卡文已截断）"
        line = f"- {detail.name}｜{detail.type_text}｜{detail.stats}\n  效果：{effect or '（无效果文本）'}"
        if used + len(line) > MAX_DIGEST_CHARS:
            blocks.append(f"（卡文过长，后面 {len(card_ids) - len(blocks)} 种卡没有列出来）")
            break
        used += len(line)
        blocks.append(line)
    if unknown_ids:
        warnings.append(
            "卡库不认识这些卡，模型看不到它们的卡文："
            + "、".join(str(card_id) for card_id in unknown_ids[:20])
            + ("…" if len(unknown_ids) > 20 else "")
        )
    return "\n".join(header + [""] + blocks), known_names, warnings


# ---------------------------------------------------------------------------
# combo 推演
# ---------------------------------------------------------------------------

_COMBO_SYSTEM = """你在为「游戏王」的一副卡组写推演笔记，读者是准备给它写自动出牌脚本的人。

要求：
1. **只能用下面卡表里出现的卡**。你需要哪些卡就写哪些，但一张都不能是卡表之外的——
   包括"通常召唤出来的怪兽"也必须来自卡表。
2. **不要凭记忆写效果**：卡文已经给你了，只能按卡文写；卡文截断或缺失时，把这一点写进
   `uncertain`，不要猜。
3. 先手与后手分开写（对手先手时会把手坑/无效系留给你，链路不一样）。
4. 每一步要说清「用哪张卡、做什么、场上/墓地变成什么样」，不要写"展开一套"这种空话。
5. 输出**只有 JSON**，不要解释、不要 markdown 代码块围栏。

JSON 结构：
{
  "summary": "一句话：这副牌的核心目标",
  "lines": [
    {
      "name": "先手主线",
      "hand": ["需要起手的卡名"],
      "cards": ["这条线用到的所有卡名"],
      "steps": ["第 1 步…", "第 2 步…"]
    }
  ],
  "notes": ["通用要点，例如某张卡要留到什么时候"],
  "uncertain": ["卡文不足或你不确定的地方"]
}
"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型回复里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""

    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except ValueError:
        # 前后带解释的情况很常见：退一步取第一个 `{` 到最后一个 `}` 之间
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _check_card_names(guide: Dict[str, Any], known_names: Set[str]) -> List[str]:
    """把 `hand` / `cards` 里不属于卡表的卡名剔掉，返回警告。

    只查结构化字段：`steps` 是自由文本，正文里提到的卡名抓不准，硬查会误报。
    """

    warnings: List[str] = []
    if not known_names:
        return ["卡库里一张卡都没查到，卡名无法校验（只看卡文的话请人工核对）"]
    for item in guide.get("lines") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or "未命名的线")
        for key in ("hand", "cards"):
            raw = item.get(key)
            if not isinstance(raw, list):
                continue
            kept: List[str] = []
            dropped: List[str] = []
            for name in raw:
                text = str(name).strip()
                if not text:
                    continue
                if text in known_names:
                    kept.append(text)
                else:
                    dropped.append(text)
            if dropped:
                warnings.append(f"{label}的 {key} 提到卡表之外的卡：{'、'.join(dropped)}（已剔除）")
            item[key] = kept
    return warnings


async def derive_combo(
    generate: Callable[..., Any],
    *,
    deck_name: str,
    digest: str,
    known_names: Set[str],
    model: str,
    logger: Optional[logging.Logger] = None,
) -> ComboResult:
    """让模型读卡表写推演笔记。

    Args:
        generate: 发请求的协程，签名 ``generate(prompt, model, max_tokens) -> str``；
            超时由它自己执行（插件那边读 `llm.training_timeout_ms`）。
        deck_name / digest / known_names: :func:`build_deck_digest` 的产物。
        model: 模型名（空串＝宿主给插件配的那只）。
        logger: 日志器。

    Raises:
        AnalysisError: 模型不可用、被拒绝、或回复为空。
    """

    prompt = f"{_COMBO_SYSTEM}\n\n{digest}\n\n请按上面的 JSON 结构输出这副卡组的推演笔记。"
    try:
        raw = await generate(prompt, model, COMBO_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001  统一成 AnalysisError，让调用方把它记成一次失败的任务
        raise AnalysisError(f"调用模型失败：{exc}") from exc
    text = str(raw or "").strip()
    if not text:
        raise AnalysisError("模型返回了空内容（额度被思考吃光，或那只模型不能用）")
    guide = _extract_json(text)
    warnings: List[str] = []
    if guide is None:
        warnings.append("模型没按 JSON 输出，原文已存档，但没有结构化字段可校验")
        result = ComboResult(
            guide={"summary": text, "lines": [], "notes": [], "uncertain": []},
            raw=text,
            digest=digest,
            warnings=warnings,
            model=model,
        )
    else:
        warnings.extend(_check_card_names(guide, known_names))
        result = ComboResult(guide=guide, raw=text, digest=digest, warnings=warnings, model=model)
    if logger is not None:
        logger.info(
            "combo 推演完成：%s 行主线、%s 条待确认、%s 条校验疑点",
            len(result.guide.get("lines") or []),
            len(result.guide.get("uncertain") or []),
            len(result.warnings),
        )
    return result


# ---------------------------------------------------------------------------
# 给跑完的输出写结论
# ---------------------------------------------------------------------------

_VERDICT_SYSTEM = """你在为「游戏王」自动出牌脚本的实验写结论，读者是脚本作者。

硬性要求：
1. **不许夸大**：对局数不够（单腿少于 80 局）时只能说「机制有没有跑坏」，不能说谁更强——
   本机的实测教训是同一套构筑重测会出现 0/60 与 8/20 并存的噪声。
2. 如果日志里有「进程崩了、文件没找到、执行器没注册、模型返回空、超时」这类迹象，
   必须写在最前面，这一轮的胜率不能当结论用。
3. 结论落成 3~5 句话，说清：这次实际跑了什么、能不能信、下一步该做什么。
   不要复述统计数字的原始行，也不要写客套话。
"""


async def summarise_run(
    generate: Callable[..., Any],
    *,
    kind_title: str,
    params: Dict[str, Any],
    output_tail: str,
    model: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """让模型读输出的尾部若干行，写一段结论。

    Raises:
        AnalysisError: 模型不可用、被拒绝、或回复为空。
    """

    param_text = "\n".join(f"- {key}：{value}" for key, value in params.items())
    prompt = (
        f"{_VERDICT_SYSTEM}\n\n"
        f"任务类型：{kind_title}\n参数：\n{param_text}\n\n"
        f"输出尾部：\n{output_tail}\n\n请写结论。"
    )
    try:
        raw = await generate(prompt, model, SUMMARY_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001
        raise AnalysisError(f"调用模型失败：{exc}") from exc
    text = str(raw or "").strip()
    if not text:
        raise AnalysisError("模型返回了空内容（额度被思考吃光，或那只模型不能用）")
    if logger is not None:
        logger.info("训练结论已生成：%s", text.replace("\n", " ")[:80])
    return text


def tail_lines(path: Any, limit: int = OUTPUT_TAIL_LINES) -> str:
    """取日志文件的尾部若干行（文件不存在就返回空串）。"""

    target = Path(path)
    if not target.exists():
        return ""
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max(1, int(limit)):])
