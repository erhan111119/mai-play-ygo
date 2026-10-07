"""让模型写"这副牌怎么打"的**数据**（卡组打法清单），校验后存进卡组库。

**为什么不写执行器代码**：代码要编译、会崩、写成空壳还看不出来（实测生成过一张牌都不出的
脚本）。数据没有编译步骤，读不懂的字段直接忽略，坏了最多是"这一步照通用打法做"——
由通用的 ``PlanAware`` 执行器读它执行（见 train/windbot/PlanAwareExecutor.cs）。

用法::

    python tools/generate_playbook.py --deck-id 86              # 只生成，打印出来
    python tools/generate_playbook.py --deck-id 86 --activate   # 校验通过就存进卡组库

退出码：0=成功，1=参数/路径问题，2=模型输出始终不合格式。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import argparse
import asyncio
import json
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.cards import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    CardDatabase,
    collect_card_info,
)
from duel.deckcode import parse_deck_code  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.netguard import guarded_request  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.playbook import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    FIELD_KEYS,
    MAX_ENTRIES,
    Playbook,
    PlaybookError,
    parse_playbook,
    to_text,
)
from train.llm import ModelClient, ModelError, pick_model  # noqa: E402  导入顺序受 sys.path 补丁影响

# 每轮的失败原因都回喂给模型重写；三次还写不对就放弃（省时间也省 token）
MAX_ATTEMPTS = 3


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="让模型写卡组打法数据（key=value），校验后保存")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--deck-id", type=int, default=0, help="卡组池里的编号（/卡组列表 里的序号）")
    parser.add_argument("--deck-file", default="", help=".ydk 路径；与 --deck-id 二选一")
    parser.add_argument("--data-dir", default="", help="插件数据目录；默认按宿主约定推导")
    parser.add_argument("--cards-cdb", default="", help="cards.cdb；默认用 ygopro 目录下的")
    parser.add_argument("--model-task", default="ygo_script", help="用宿主模型配置里的哪个任务")
    parser.add_argument("--model", default="", help="指定模型别名；留空按任务挑")
    parser.add_argument("--allow-private-model", action="store_true", help="允许模型服务在内网/本机")
    parser.add_argument("--max-tokens", type=int, default=2048, help="模型输出上限")
    parser.add_argument("--attempts", type=int, default=MAX_ATTEMPTS, help="最多重写几轮")
    parser.add_argument("--activate", action="store_true", help="校验通过就存进卡组库（等于上线）")
    parser.add_argument("--dump-raw", default="", help="把每轮模型原文写到这个目录（排查用）")
    parser.add_argument("--list-models", action="store_true", help="列出可选模型后退出")
    return parser.parse_args()


def load_config_paths(plugin_root: Path) -> dict:
    """从插件 config.toml 读路径。"""

    import tomllib

    config_path = plugin_root / "config.toml"
    if not config_path.is_file():
        return {}
    with config_path.open("rb") as handle:
        return tomllib.load(handle).get("paths") or {}


def default_data_dir(plugin_root: Path) -> Path:
    """插件数据目录（宿主约定：``<MaiBot>/data/plugins/<插件 id>/``）。"""

    manifest = plugin_root / "_manifest.json"
    plugin_id = ""
    try:
        plugin_id = str(json.loads(manifest.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        plugin_id = ""
    return plugin_root.parent.parent / "data" / "plugins" / plugin_id if plugin_id else plugin_root / "data"


def build_prompt(cards: Sequence[object], deck_name: str, *, previous_error: str = "") -> str:
    """给模型的提示词：只要 ``key=value``，不许写别的。

    为什么把"每张卡的卡号+卡名+效果"逐条列出来：模型凭印象写卡号几乎必错，而写错卡号的后果是
    "这段组合永远找不到那张卡"——静默失效，正是我们最想避免的。列全之后它只需挑顺序。
    """

    lines: List[str] = []
    for info in cards:
        effect = getattr(info, "effect", "") or ""
        lines.append(f"- {info.card_id} {info.name}：{effect[:160]}")
    card_block = "\n".join(lines) if lines else "（这副牌没读到卡片信息）"
    prompt = f"""你是游戏王的卡组分析师。请为下面这副牌写一份"打法清单"，让一个固定的出牌程序按你的清单打。

卡组：{deck_name}

卡表（卡号 卡名：效果）：
{card_block}

请只输出若干行 `键=卡号,卡号,...`，不要写解释、不要写代码块标记。可用键：
- summon_order：优先通常召唤的怪（能站场/能展开的先出）
- activate_order：关键发动顺序（展开件、检索件的先后）
- set_order：优先盖放的魔陷
- search_order：检索/取对象时优先拿的卡（组合的关键件放最前）
- never_activate：需要**谨慎**发动的卡（留着的手坑、容易被骗的坑）。注意这只是提醒：
  决策时会把"这张要谨慎"告诉出牌方，**不会禁止发动**——所以别把关键展开件列进来

要求：
1. 卡号只能从上面卡表里挑，不许编造；
2. 每个键最多 {MAX_ENTRIES} 个卡号，按优先级从高到低；
3. 不确定的键就不要写这一行（留空比写错好）；
4. 只写键=值这几种行，其它内容一律不要。
"""
    if previous_error:
        prompt += f"\n上一次的输出不能用，原因是：{previous_error}\n请只按上面的格式重新输出一遍。\n"
    return prompt


def extract_playbook(text: str, allowed: Sequence[int]) -> Tuple[Optional[Playbook], str]:
    """从模型输出里抽出打法清单；返回 ``(清单, 错误说明)``。"""

    cleaned: List[str] = []
    for line in text.splitlines():
        stripped = line.strip().strip("`").strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped and not stripped.startswith(("{", "[")):
            cleaned.append(stripped.split("）")[0] if False else stripped)
    if not cleaned:
        return None, "模型没有输出任何 `键=卡号` 形式的行"
    try:
        book = parse_playbook("\n".join(cleaned), allowed_cards=allowed)
    except PlaybookError as exc:
        return None, str(exc)
    if book.is_empty():
        return None, "输出里没有一个认识的键"
    return book, ""


async def main_async(args: argparse.Namespace) -> int:
    """跑一次生成；返回退出码。"""

    if args.list_models:
        from train.llm import available_models

        for name in available_models():
            print(name)
        return 0

    plugin_root = Path(args.plugin_root)
    paths = load_config_paths(plugin_root)
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(plugin_root)
    cdb = Path(args.cards_cdb) if args.cards_cdb else Path(str(paths.get("ygopro_dir") or "")) / "cards.cdb"

    deck_id = 0
    if args.deck_id:
        deck_id = args.deck_id
        database = data_dir / "deck_pool.db"
        if not database.is_file():
            print(f"[错误] 找不到卡组库：{database}")
            return 1
        import sqlite3

        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT display_name, ydk_path FROM decks ORDER BY deck_id"
            ).fetchall()
        finally:
            connection.close()
        if not 1 <= deck_id <= len(row):
            print(f"[错误] 卡组池里没有编号 {deck_id} 的卡组（共 {len(row)} 副）")
            return 1
        deck_name, deck_path = str(row[deck_id - 1][0]), Path(str(row[deck_id - 1][1]))
    elif args.deck_file:
        deck_path = Path(args.deck_file)
        deck_name = deck_path.stem
    else:
        print("[错误] 要指定 --deck-id 或 --deck-file")
        return 1
    if not deck_path.is_file():
        print(f"[错误] 找不到卡表：{deck_path}")
        return 1
    if not cdb.is_file():
        print(f"[错误] 找不到卡库：{cdb}（用 --cards-cdb 指定）")
        return 1

    parsed = parse_deck_code(deck_path.read_text(encoding="utf-8", errors="replace"))
    card_db = CardDatabase(cdb)
    cards = collect_card_info(card_db, parsed.main, parsed.extra)
    allowed = sorted({int(card) for card in list(parsed.main) + list(parsed.extra)})
    print(f"卡组「{deck_name}」：主 {len(parsed.main)} / 额外 {len(parsed.extra)}，共 {len(cards)} 种卡")
    if not cards:
        print("[错误] 这副牌一张卡都没认出来（卡库对不上？）")
        return 1

    # 模型来源：**优先插件自带的 brain_model.toml**（就是我们给"问 AI"配的那个），
    # 没有它才退回宿主模型注册表里的任务。宿主的 ygo_script 任务常常没配——实测直接
    # 报"没有配置模型"，而插件自带的那份是用户已经配好的（也就不用动宿主配置）。
    from train.ai_brain import chat_with_settings, load_brain_model_settings

    local_settings = load_brain_model_settings(data_dir)
    client = None
    if local_settings is not None:
        print(f"模型：{local_settings.model}（插件自带配置 {local_settings.base_url}）")
    else:
        try:
            target = pick_model(task=args.model_task, name=args.model)
        except ModelError as exc:
            print(f"[错误] 读不到模型配置：{exc}")
            return 1
        client = ModelClient(target, timeout=120, allow_private_host=args.allow_private_model)
        print(f"模型：{target.provider} / {target.model}（上限 {args.max_tokens} tokens）")

    async def ask(prompt: str) -> str:
        """问一次模型（走上面选定的那一个）。"""

        if local_settings is not None:
            return await asyncio.to_thread(
                chat_with_settings,
                local_settings,
                prompt,
                request=guarded_request,
                max_tokens=args.max_tokens,
            )
        assert client is not None
        return await asyncio.to_thread(
            client.chat, prompt, max_tokens=args.max_tokens, temperature=0.2
        )

    # 提示词里带"上一次错在哪"，让重写有的放矢；报错要原样给人看，别吞
    error = ""
    book: Optional[Playbook] = None
    for attempt in range(1, max(1, args.attempts) + 1):
        prompt = build_prompt(cards, deck_name, previous_error=error)
        try:
            raw = await ask(prompt)
        except (ModelError, RuntimeError) as exc:
            print(f"[失败] 调模型时报错：{exc}")
            return 2
        if args.dump_raw:
            target_dir = Path(args.dump_raw)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"playbook-attempt-{attempt}.txt").write_text(raw, encoding="utf-8")
        book, error = extract_playbook(raw, allowed)
        if book is not None:
            print(f"[成功] 第 {attempt} 轮拿到可用清单："
                  + "、".join(f"{key} {len(book.get(key))} 张" for key in FIELD_KEYS if book.get(key)))
            break
        print(f"[第 {attempt} 轮不通过] {error}")
    if book is None:
        print("[失败] 模型始终没有按格式输出，没有改动卡组库。")
        return 2

    text = to_text(book)
    print("--- 打法数据 ---")
    print(text.strip())
    if not args.activate or not deck_id:
        print("（没有写进卡组库：加 --activate 才会生效）")
        return 0
    pool_path = data_dir / "deck_pool.db"
    import sqlite3

    connection = sqlite3.connect(str(pool_path))
    try:
        connection.execute("UPDATE decks SET playbook = ? WHERE deck_id = ?", (text, deck_id))
        connection.commit()
    finally:
        connection.close()
    print(f"已存进卡组库（编号 {deck_id}）；用「PlanAware」执行器的那一局会按它出牌")
    return 0


def main() -> int:
    """入口。"""

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
