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
#: combo 推演**每次**答复的额度。分两次问（先写主线、再写要点），每次都给小额度——
#: 这不是省 token，是被宿主的硬超时逼出来的：插件调模型走的是 `cap.call`，
#: **单次调用 30 秒就断**（实测跑出来的原文：`[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`）。
#: 一次要 8192 token 的答复，思考型模型光想就超 30 秒；拆小之后才有机会落在窗口内。
COMBO_MAX_TOKENS = 1600

#: 写结论的答复额度（一段话，不需要长）。
SUMMARY_MAX_TOKENS = 1200

#: 喂给模型的卡文上限：单张效果截断长度、整副卡表的字符上限。
#: 卡表给太长同样是"读得久"的问题：输入越长，同一只模型回得越慢。
MAX_EFFECT_CHARS = 300
MAX_DIGEST_CHARS = 12000

#: 写结论时给模型看多少行 stdout。
OUTPUT_TAIL_LINES = 120

#: 调用失败时的排查提示（按错误内容挑一条贴上去）。
#: 为什么值得专门写：这两条都是实测踩出来的，光看 `[E_TIMEOUT] 请求 cap.call 超时`
#: 完全不知道该改什么——而它们各自只需要改一行配置。
_FAILURE_HINTS: Tuple[Tuple[str, str], ...] = (
    (
        "cap.call",
        "宿主对插件的单次模型调用有 30 秒硬超时（插件改不了）："
        "训练模型请填一只**不思考**的（本机是 `deepseek-flash`；`ds` / `deepseekV4.1flash` 这类会思考的会撞上）",
    ),
    (
        "空内容",
        "答复是空的通常说明额度被思考吃光了：换一只关掉思考的模型，"
        "或在 model_config.toml 里给它加 `extra_params = {thinking = {type = \"disabled\"}}`",
    ),
)


def failure_hint(error: str) -> str:
    """给失败原因配一句"该改哪里"（没有对应提示就返回空串）。"""

    text = str(error or "")
    for needle, hint in _FAILURE_HINTS:
        if needle in text:
            return hint
    return ""


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

_COMBO_MAIN_SYSTEM = """你在为「游戏王」的一副卡组写推演笔记，读者是准备给它写自动出牌脚本的人。

这一轮**只写主线**：先手与后手各一条，把最想做的事情按顺序写清楚。

要求：
1. **只能用下面卡表里出现的卡**。你需要哪些卡就写哪些，但一张都不能是卡表之外的——
   包括"通常召唤出来的怪兽"也必须来自卡表。
2. **不要凭记忆写效果**：卡文已经给你了，只能按卡文写；卡文截断或缺失的地方不要猜。
3. 先手与后手分开写（对手先手时会把手坑/无效系留给你，链路不一样）。
4. 每一步要说清「用哪张卡、做什么、场上/墓地变成什么样」，不要写"展开一套"这种空话。
5. 主线控制在 2~3 条、每条不超过 8 步——这一轮只要骨架，细节下一轮再说。
6. 输出**只有 JSON**，不要解释、不要 markdown 代码块围栏。

JSON 结构：
{
  "summary": "一句话：这副牌的核心目标",
  "lines": [
    {"name": "先手主线", "hand": ["需要起手的卡名"], "cards": ["这条线用到的卡名"],
     "steps": ["第 1 步…", "第 2 步…"]}
  ]
}
"""

_COMBO_NOTES_SYSTEM = """你在为「游戏王」的一副卡组写推演笔记。主线已经写好了（见下），
这一轮**只补充要点与不确定的地方**，不要重写主线。

要求：
1. 卡名只能用卡表里有的（卡表已给你）。
2. `notes` 写 3~6 条通用要点（例如某张卡要留到什么时候、哪一步最怕被打断）。
3. `uncertain` 写卡文不足、或你不确定的地方（没有就写空数组，不要编）。
4. 输出**只有 JSON**，不要解释、不要 markdown 代码块围栏。

JSON 结构：
{"notes": ["…"], "uncertain": ["…"]}
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
    extra_prompt: str = "",
    logger: Optional[logging.Logger] = None,
) -> ComboResult:
    """让模型读卡表写推演笔记（**分两轮问**，每轮都小）。

    为什么要分开：插件调模型走 `cap.call`，宿主对单次调用有 30 秒硬超时。
    一轮里要 8000 token 的答复，思考型模型光"想"就超时（实测就是这么失败的），
    所以拆成"先写主线，再补要点"两次小请求。第二次失败不算整个任务失败：
    主线已经拿到了，把缺的那半如实记进 warning。

    Args:
        generate: 发请求的协程，签名 ``generate(prompt, model, max_tokens) -> str``；
            超时由它自己执行（插件那边读 `llm.training_timeout_ms`）。
        deck_name / digest / known_names: :func:`build_deck_digest` 的产物。
        model: 模型名（空串＝宿主给插件配的那只）。
        logger: 日志器。

    Raises:
        AnalysisError: 主线这一轮就失败（模型不可用、被拒绝、回复为空）。
    """

    warnings: List[str] = []
    extra = str(extra_prompt or "").strip()
    extra_block = f"\n\n作者的额外要求（必须体现在推演里）：\n{extra[:2000]}" if extra else ""
    main_prompt = (
        f"{_COMBO_MAIN_SYSTEM}\n\n{digest}{extra_block}\n\n请按上面的 JSON 结构输出这副卡组的主线。"
    )
    try:
        raw = await generate(main_prompt, model, COMBO_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001  统一成 AnalysisError，让调用方把它记成一次失败的任务
        raise AnalysisError(_with_hint(f"调用模型失败：{exc}")) from exc
    text = str(raw or "").strip()
    if not text:
        raise AnalysisError(
            _with_hint("模型返回了空内容（额度被思考吃光，或那只模型不能用）")
        )
    guide = _extract_json(text)
    if guide is None:
        warnings.append("模型没按 JSON 输出，原文已存档，但没有结构化字段可校验")
        guide = {"summary": text, "lines": [], "notes": [], "uncertain": []}
    else:
        warnings.extend(_check_card_names(guide, known_names))

    notes = await _derive_notes(
        generate,
        digest=digest,
        summary=str(guide.get("summary") or ""),
        lines=guide.get("lines") or [],
        model=model,
        extra_prompt=extra,
    )
    if isinstance(notes, str):
        warnings.append(notes)
    else:
        guide["notes"] = notes.get("notes") or []
        guide["uncertain"] = notes.get("uncertain") or []
    result = ComboResult(
        guide=guide, raw=text, digest=digest, warnings=warnings, model=model
    )
    if logger is not None:
        logger.info(
            "combo 推演完成：%s 行主线、%s 条待确认、%s 条校验疑点",
            len(result.guide.get("lines") or []),
            len(result.guide.get("uncertain") or []),
            len(result.warnings),
        )
    return result


async def _derive_notes(
    generate: Callable[..., Any],
    *,
    digest: str,
    summary: str,
    lines: Sequence[Any],
    model: str,
    extra_prompt: str = "",
) -> Any:
    """第二轮：补要点与待确认项。

    返回解析出来的 dict；这一轮失败时返回**一句说明**（字符串），由调用方记进 warning——
    主线已经拿到了，不该因为补料失败把整份推演丢掉。
    """

    outline = json.dumps({"summary": summary, "lines": lines}, ensure_ascii=False)[:4000]
    extra = str(extra_prompt or "").strip()
    extra_block = f"\n\n作者的额外要求（要点里也要体现）：\n{extra[:1000]}" if extra else ""
    prompt = (
        f"{_COMBO_NOTES_SYSTEM}\n\n{digest}{extra_block}\n\n已写好的主线：\n{outline}"
        "\n\n请补充要点与待确认项。"
    )
    try:
        raw = await generate(prompt, model, SUMMARY_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001  第二轮失败只影响"要点"，不影响主线
        return _with_hint(f"要点这一轮没写成：{exc}（主线已存档）")
    text = str(raw or "").strip()
    if not text:
        return "要点这一轮没写成：模型返回了空内容（主线已存档）"
    parsed = _extract_json(text)
    if parsed is None:
        return "要点这一轮没按 JSON 输出，已跳过（主线已存档）"
    return parsed


def _with_hint(message: str) -> str:
    """给错误信息补一句"该改哪里"（没有对应提示就原样返回）。"""

    hint = failure_hint(message)
    return f"{message}｜提示：{hint}" if hint else message


# ---------------------------------------------------------------------------
# 联网资料查证
# ---------------------------------------------------------------------------

#: 联网查资料**每次**答复的额度。这类带检索的模型会把检索到的原文一起带回来，给少了只剩半句。
#: 写脚本是一次性调用（不是对局内逐次决策），几十秒的等待可以接受。
RESEARCH_MAX_TOKENS = 1800


@dataclass
class ResearchResult:
    """联网资料查证的结果。"""

    text: str = ""
    model: str = ""


_RESEARCH_SYSTEM = """你在为「游戏王」的一副卡组做资料搜集，读者是准备给它写自动出牌脚本的人。
请**联网检索**这副牌在现实中的主流打法，然后整理成给脚本作者看的资料。

要求：
1. 只写与卡表里**实际有的卡**有关的战术。资料里出现的卡如果不在卡表里，就不要写进结论；
   宁愿少写，也不要为了凑数把别的构筑的卡混进来。
2. **卡组名可能只是社区译名或群友自取的名字，按它查不到很正常**：那就改用上面的**卡名**
   去查——一张卡一张卡地查"这张卡的效果怎么用、和卡表里谁配合、该在什么时点发动"，
   再拼出这套牌的打法。卡名比卡组名可靠。
3. 重点回答这五件事：
   * 典型展开路线：起手有哪几张 → 做到什么终场（按"先手"和"后手"分开写）；
   * 卡的用法时机：哪些是启动点、哪些要留着应对、同一个效果的多个用法里哪个优先；
   * **同一张卡有多条效果时，哪一条先发动、哪一条要留着**（脚本作者最缺的就是这一层）；
   * 常见失误：打得不好的人通常错在哪一步、错在哪张卡上；
   * 这套牌怕什么（手坑/除去/特定压制），以及怎么躲。
4. 不确定的地方**直接写"不确定"**，查不到就写"没查到"——不要用记忆里的旧信息把空补上；
   逐卡查也查不到的卡，就列出卡名说明"这几张没查到用法"。
5. 最后单独一段列出来源（站点名或标题即可；一条都没拿到就别写这段）。
6. 输出纯文本条目（每条 `- ` 开头），不要 JSON、不要表格、不要代码块。"""


async def research_archetype(
    generate: Callable[..., Any],
    *,
    deck_name: str,
    digest: str,
    model: str,
    card_names: Sequence[str] = (),
    extra_prompt: str = "",
    logger: Optional[logging.Logger] = None,
) -> ResearchResult:
    """让**联网模型**查一遍这副牌在现实里怎么打，返回给写脚本用的资料文本。

    与 :func:`derive_combo` 的分工：combo 推演是"按卡文自己推出来的"，这一份是"网上的人怎么打"。
    两份都会喂给写脚本的模型，而**卡文永远优先**：网上资料可能过时、可能是别的构筑的说法
    （本机实测：同一个问题问两只联网模型，卡表明细就对不上），所以调用方在提示词里必须把它
    标成"仅供参考、与卡文冲突时以卡文为准"。

    Args:
        generate: 发请求的协程，签名 ``generate(prompt, model, max_tokens) -> str``。
        deck_name / digest: 卡组名与 :func:`build_deck_digest` 的产物。
        model: 联网模型名（空串＝没配，调用方应该干脆不要调这个函数）。
        card_names: 卡表里的卡名（会在问句里点名，让模型在"卡组名查不到"时改按卡名逐张查）。
            本机踩过：异解那副按卡组名问，联网模型只回了"没查到"（32 字节），换成卡名就有东西可查。
        extra_prompt: 作者额外要求（特别想弄清楚什么就写在这）。
        logger: 日志器。

    Raises:
        AnalysisError: 调用失败或回复为空（**不吞**：要不要继续由调用方决定——
            写脚本时"少一份参考"和"整个任务失败"是两种不同的处理）。
    """

    extra = str(extra_prompt or "").strip()
    extra_block = f"\n\n作者特别想弄清楚的问题：\n{extra[:1000]}" if extra else ""
    names = [str(name).strip() for name in card_names if str(name).strip()]
    names_block = ""
    if names:
        shown = names[:14]
        names_block = "\n（这套牌的主要卡片：" + "、".join(shown) + ("…）" if len(names) > len(shown) else "）")
    prompt = (
        f"{_RESEARCH_SYSTEM}\n\n{digest}{extra_block}\n\n"
        f"请检索「{deck_name}」这套牌的打法{names_block}，整理成上面要求的资料。"
    )
    try:
        raw = await generate(prompt, model, RESEARCH_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001  统一成 AnalysisError，让调用方把它记成一次失败
        raise AnalysisError(_with_hint(f"联网查资料失败：{exc}")) from exc
    text = str(raw or "").strip()
    if not text:
        raise AnalysisError(
            _with_hint("联网模型返回了空内容（额度被思考吃光，或这只模型其实不支持对话接口）")
        )
    if logger is not None:
        logger.info("联网资料已取回：%s 字（模型 %s）", len(text), model or "宿主默认")
    return ResearchResult(text=text, model=model)


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
