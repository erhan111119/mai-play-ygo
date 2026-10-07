"""给一副卡组生成**展开流程**（combo line），存进知识库的 ``deck:<编号>``。

与 ``generate_playbook.py`` 的分工：
* ``generate_playbook.py`` 写的是 **key=value 清单**（先出谁、检索什么、哪些留着）——给执行器当规则用；
* 本工具写的是 **一步一步的展开流程**（起手 → 步骤 → 终场 → 自肃 → 被断后怎么走）——给**问 AI 时当资料**用。

为什么要单独做它：卡表里每张卡的效果模型都看得到，但它不知道**这副牌的正确顺序与终场**——
实测就是"连基础展开都做不出来"（用户报的"脚本根本无法正常展开"）。市面上没有覆盖所有系列的
combo 库，所以这里做成**按需生成 + 缓存**：某副牌第一次要用时生成一次，之后一直读缓存。

校验（写在提示词里、也在代码里复查）：
1. 卡号/卡名只能来自这副牌的卡表——写错的直接判失败并让它重写（宁可没有，也不能喂错资料）；
2. 长度上限（知识库检索有硬上限，写太长会被截断）；
3. 必须包含"起手/步骤/终场"三段，否则要求重写。

用法::

    python tools/build_deck_plan_ai.py --deck-id 89              # 只生成并打印，不写库
    python tools/build_deck_plan_ai.py --deck-id 89 --activate    # 写进 knowledge.db
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, default_data_dir, default_knowledge_db  # noqa: E402

# 展开流程的长度上限：知识库检索时有硬上限，写长了会被截断，所以生成时就卡住
MAX_PLAN_CHARS = 1200
"""展开流程最多多少字（知识库那边的同名字段要一致）。

1200 是因为**要按起手张数分档写**：一份通用流程只能覆盖"手牌恰好凑齐"的情况，
而实战里 1 卡起手与 2 卡起手要走的线完全不同（这正是"看起来会打、实际展开不出来"的原因）。
"""
# 一次给模型看多少张卡的效果（主+额外；太长的效果文本会被截断）
MAX_CARDS_IN_PROMPT = 80
EFFECT_CHARS = 150


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="给一副卡组生成展开流程，存进知识库")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--deck-id", type=int, required=True, help="卡组池里的编号")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--activate", action="store_true", help="写进 knowledge.db（默认只打印）")
    parser.add_argument("--model", default="", help="覆盖模型名（默认用 brain_model.toml）")
    parser.add_argument("--attempts", type=int, default=2, help="校验不过时最多重写几次")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--allow-private-model", action="store_true", help="允许模型服务在内网")
    parser.add_argument(
        "--feedback",
        default="",
        help=(
            "上一版在实测里的问题（例如「平均特召 1.2 次/局、60% 的局一次都没特召」），"
            "会写进提示词要求重写"
        ),
    )
    return parser.parse_args()


def read_deck(data_dir: Path, deck_id: int) -> Tuple[str, Dict[str, List[int]]]:
    """从卡组池读一副牌：``(显示名, {main/extra/side: [卡号…]})``。"""

    database = data_dir / "deck_pool.db"
    if not database.is_file():
        raise FileNotFoundError(f"找不到卡组池：{database}")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT display_name, ydk_path FROM decks WHERE deck_id = ?", (int(deck_id),)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise FileNotFoundError(f"卡组池里没有编号 {deck_id} 的卡组")
    display_name, ydk_path = str(row[0]), Path(str(row[1]))
    sections: Dict[str, List[int]] = {"main": [], "extra": [], "side": []}
    current = "main"
    for line in ydk_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            lowered = stripped.lower()
            current = "extra" if "extra" in lowered else ("side" if "side" in lowered else "main")
            continue
        if stripped.startswith("!"):
            current = "side"
            continue
        if stripped.isdigit():
            sections[current].append(int(stripped))
    return display_name, sections


def card_texts(cards_db: Path, card_ids: Sequence[int]) -> List[Tuple[int, str, str]]:
    """取 ``[(卡号, 卡名, 效果文本)]``（只读、参数绑定）。"""

    connection = sqlite3.connect(f"file:{cards_db.as_posix()}?mode=ro", uri=True)
    result: List[Tuple[int, str, str]] = []
    try:
        for card_id in list(dict.fromkeys(int(cid) for cid in card_ids))[:MAX_CARDS_IN_PROMPT]:
            row = connection.execute(
                "SELECT name, desc FROM texts WHERE id = ?", (card_id,)
            ).fetchone()
            if row is None:
                result.append((card_id, f"#{card_id}", ""))
            else:
                result.append((card_id, str(row[0]), str(row[1] or "").replace("\r", " ")))
    finally:
        connection.close()
    return result


def build_prompt(deck_name: str, main: Sequence[Tuple[int, str, str]], extra: Sequence[Tuple[int, str, str]],
                 *, previous_error: str = "", feedback: str = "") -> str:
    """给模型的提示词：要一份**按起手张数分档**的展开流程，卡号只能来自卡表。"""

    def block(title: str, cards: Sequence[Tuple[int, str, str]]) -> str:
        rows = [f"- {cid} {name}：{effect[:EFFECT_CHARS]}" for cid, name, effect in cards]
        return f"{title}：\n" + ("\n".join(rows) if rows else "（空）")

    prompt = f"""你是游戏王 OCG 的卡组教练。请为下面这副牌写一份**展开流程**，给一个对局中的 AI 选手当资料用。

卡组：{deck_name}

{block("主卡组（卡号 卡名：效果）", main)}

{block("额外卡组", extra)}

**最关键的要求：按起手张数分档写**——实战里 1 张卡起手和 2 张卡起手走的线完全不同，
只写一份"通用流程"等于没法用。请严格按下面五段输出（每段一行标题 + 内容，不要写别的、不要代码块）：

单卡起手：每张**能单独启动**的卡各写一条，格式 `卡号→这条线怎么走（到哪只终端）`；一张卡只能做出小场就如实写"小场：出X"
两卡起手：常见的两卡组合各写一条，格式 `卡号+卡号→怎么走（到哪只终端）`；**并且标出关键件缺一张时改走哪条**（写"缺X时→…"）
终场：做完之后场上应该有什么（尽量点名额外卡组的怪兽；分别写"理想场"和"退而求其次"）
自肃：发动过程中会限制自己的地方（不能检索 / 只能出某类怪 / 一回合一次 / 只能用某方法特召…）
被断后：关键步骤被无效或被破坏时改走哪条线（点明"哪个点被断→改走哪条"）

要求：
1. 只能提上面卡表里出现过的卡（写卡号或卡名都行，**卡号必须来自卡表**）；
2. 全篇不超过 {MAX_PLAN_CHARS} 字，句子短、动词开头（"召唤…""发动…""检索…"）；
3. 不确定的地方写"视情况"，不要编造卡表里没有的卡；
4. 如果这副牌明显不是展开卡组（纯控制/削血/beat），就如实写它的取胜方式与关键张，不要硬编 combo。
"""
    if feedback:
        prompt += (
            "\n**上一版在实测里表现不好**：" + feedback.strip() + "\n"
            "请针对它重写：把「第一步该出什么、按什么顺序」写得更明确，"
            "并确保 1 卡起手也有可执行的线。\n"
        )
    if previous_error:
        prompt += f"\n上一次的输出不能用，原因是：{previous_error}\n请按上面的五段格式重写一遍。\n"
    return prompt


def validate_plan(text: str, allowed_cards: Sequence[int]) -> Tuple[Optional[str], str]:
    """校验模型写的展开流程；返回 ``(正文, 错误说明)``。"""

    body = (text or "").strip()
    if not body:
        return None, "模型没有返回内容"
    needed = ("单卡起手", "终场")
    missing = [part for part in needed if part not in body]
    if missing:
        return None, "缺少必须的段落：" + "、".join(missing)
    if len(body) > MAX_PLAN_CHARS * 2:
        return None, f"写太长了（{len(body)} 字，上限 {MAX_PLAN_CHARS * 2}）"
    # 卡号必须都在这副牌里：模型最容易犯的错就是编卡（那种资料喂进去比没有更糟）。
    # 注意**不能用 ``\b``**：Python 的 ``\w`` 把中文也算作单词字符，所以"发动100267016"里
    # 数字两侧没有单词边界、``\b\d{5,8}\b`` 一个都匹配不到（实测踩过）；改用"前后不是数字"。
    allowed = {int(cid) for cid in allowed_cards}
    unknown = sorted({int(token) for token in re.findall(r"(?<!\d)(\d{5,9})(?!\d)", body)} - allowed)
    if unknown:
        return None, "提到了卡表里没有的卡号：" + "、".join(str(cid) for cid in unknown[:5])
    if len(body) > MAX_PLAN_CHARS:
        body = body[: MAX_PLAN_CHARS - 1] + "…"
    return body, ""


def store_plan(database: Path, deck_id: int, title: str, body: str) -> None:
    """写进 ``deck_plans``（``deck:<编号>``）。"""

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO deck_plans"
            " (plan_key, kind, deck_id, title, body, source, updated_at)"
            " VALUES (?, 'deck', ?, ?, ?, 'ai', ?)",
            (f"deck:{int(deck_id)}", int(deck_id), title, body, __import__("time").time()),
        )
        connection.commit()
    finally:
        connection.close()


async def main_async(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    database = default_knowledge_db(data_dir)
    try:
        deck_name, sections = read_deck(data_dir, args.deck_id)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 2

    import tomllib

    config = Path(args.plugin_root) / "config.toml"
    with config.open("rb") as handle:
        paths = tomllib.load(handle).get("paths") or {}
    cards_cdb = Path(str(paths.get("cards_cdb") or "")) if paths.get("cards_cdb") else (
        Path(str(paths.get("ygopro_dir") or "")) / "cards.cdb"
    )
    if not cards_cdb.is_file():
        print(f"[错误] 找不到卡片数据库：{cards_cdb}")
        return 2

    main_cards = card_texts(cards_cdb, sections["main"])
    extra_cards = card_texts(cards_cdb, sections["extra"])
    print(f"卡组 #{args.deck_id}「{deck_name}」：主卡 {len(sections['main'])} 张、额外 {len(sections['extra'])} 张")

    from duel.netguard import guarded_request
    from train.ai_brain import chat_with_settings, load_brain_model_settings

    settings = load_brain_model_settings(data_dir)
    if settings is None:
        print(f"[错误] 读不到 {data_dir / 'brain_model.toml'}（问 AI 用的那份模型配置）")
        return 2
    if args.model:
        settings = dataclasses_replace(settings, model=args.model)
    print(f"模型：{settings.model}（{settings.base_url}）")

    allowed = list(sections["main"]) + list(sections["extra"])
    error = ""
    for attempt in range(1, max(1, args.attempts) + 1):
        prompt = build_prompt(
            deck_name, main_cards, extra_cards, previous_error=error, feedback=args.feedback
        )
        try:
            raw = await asyncio.to_thread(
                chat_with_settings,
                settings,
                prompt,
                request=guarded_request,
                max_tokens=args.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001  模型出错要如实报出来
            print(f"[错误] 第 {attempt} 轮模型调用失败：{exc}")
            return 1
        body, error = validate_plan(raw, allowed)
        if body is not None:
            print("=" * 72)
            print(body)
            print("=" * 72)
            if args.activate:
                store_plan(database, args.deck_id, deck_name, body)
                print(f"已写入知识库：{database}（deck:{args.deck_id}）")
            else:
                print("（没有 --activate，未写库）")
            return 0
        print(f"第 {attempt} 轮输出不能用：{error}")
    print(f"[错误] {args.attempts} 轮都没通过校验：{error}")
    return 1


def dataclasses_replace(settings, **changes):
    """替换 ``BrainModelSettings`` 里的字段（避免为一个字段 import dataclasses）。"""

    import dataclasses

    return dataclasses.replace(settings, **changes)


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
