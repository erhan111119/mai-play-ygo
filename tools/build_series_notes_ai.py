"""让模型给**每个系列**写一段"这牌怎么打、怎么拦"，补上没有人工整理的那些。

为什么要做这个：池子里 106 个系列，人工整理的只有 30 个；其余是自动拼的"画像"
（成员/起手点/阻抗清单），**没有"它想干什么、我该怎么拦"的人话**——而那正是对手侧最难的部分。
人工整理长尾不现实，所以按需生成 + 缓存，和卡组展开流程一个思路。

判据与来源：系列的成员直接从引擎卡库里按 setcode 找（池子里出现过的），
效果文本从本地卡库读；卡号校验沿用"只能提这个系列里有的卡"。

用法::

    python tools/build_series_notes_ai.py --setcode 0x1bf          # 单个系列
    python tools/build_series_notes_ai.py --all-missing            # 池子里所有没有人工整理的
    python tools/build_series_notes_ai.py --all-missing --limit 20  # 先做 20 个
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, default_data_dir, default_knowledge_db  # noqa: E402

# 系列说明的长度上限（进提示词时还会再截断；宁短勿长）
MAX_NOTE_CHARS = 420
# 一次给模型看多少张系列成员
MAX_MEMBERS = 26
EFFECT_CHARS = 120


def all_db_setcodes(cards_cdb: Path, *, min_members: int = 3) -> List[int]:
    """**整个卡库**里成员数够的系列码（按成员数从多到少）。

    为什么要有它：池子只有 83 副牌、涉及 100 多个系列，而全库有 500+ 个——
    对手是真人时随时可能掏出池子里没有的系列，那时库里没说明就只能靠模型现猜。
    一次把全库补上（几百个系列，跑一两个小时，之后只增量补新卡包）。
    """

    connection = sqlite3.connect(f"file:{cards_cdb.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT setcode & 65535 AS code, COUNT(*) AS n FROM datas"
            " WHERE (setcode & 65535) != 0 GROUP BY code HAVING n >= ? ORDER BY n DESC",
            (int(min_members),),
        ).fetchall()
    finally:
        connection.close()
    return [int(code) for code, _count in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="给系列生成「怎么打/怎么拦」的说明")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--setcode", default="", help="系列码，十六进制（0x1bf）或十进制")
    parser.add_argument("--all-missing", action="store_true", help="池子里所有没有说明的系列")
    parser.add_argument(
        "--all-db",
        action="store_true",
        help="**整个卡库**里成员 ≥3 张的所有系列（几百个，跑得久；对手是真人时用得上）",
    )
    parser.add_argument("--min-members", type=int, default=3, help="--all-db 时的最少成员数")
    parser.add_argument("--limit", type=int, default=0, help="最多做几个（0 = 不限）")
    parser.add_argument("--activate", action="store_true", help="写进知识库（默认只打印）")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=1024)
    return parser.parse_args()


def parse_setcode(raw: str) -> int:
    text = raw.strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text)


def series_members(cards_cdb: Path, setcode: int) -> List[Tuple[int, str, str]]:
    """这套系列的成员卡：``[(卡号, 卡名, 效果)]``（按 setcode 低 16 位匹配）。"""

    connection = sqlite3.connect(f"file:{cards_cdb.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT d.id, t.name, t.desc FROM datas d JOIN texts t ON t.id = d.id"
            " WHERE (d.setcode & 65535) = ? ORDER BY d.id",
            (int(setcode) & 0xFFFF,),
        ).fetchall()
    finally:
        connection.close()
    return [(int(cid), str(name), str(desc or "").replace("\r", " ")) for cid, name, desc in rows]


def pool_setcodes(data_dir: Path) -> List[int]:
    """池子里出现过的系列码（按出现次数排序）——只做真的会遇到的那些。"""

    import tomllib

    config = _PLUGIN_ROOT / "config.toml"
    paths = tomllib.loads(config.read_text(encoding="utf-8")).get("paths") or {}
    cards_cdb = Path(str(paths.get("cards_cdb") or "")) if paths.get("cards_cdb") else (
        Path(str(paths.get("ygopro_dir") or "")) / "cards.cdb"
    )
    pool = sqlite3.connect(f"file:{(data_dir / 'deck_pool.db').as_posix()}?mode=ro", uri=True)
    deck_cards: List[int] = []
    for (path,) in pool.execute("SELECT ydk_path FROM decks"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                deck_cards.append(int(stripped))
    pool.close()

    cards = sqlite3.connect(f"file:{cards_cdb.as_posix()}?mode=ro", uri=True)
    counter: Dict[int, int] = {}
    for card_id in deck_cards:
        row = cards.execute("SELECT setcode FROM datas WHERE id = ?", (card_id,)).fetchone()
        if not row or not row[0]:
            continue
        code = int(row[0]) & 0xFFFF
        counter[code] = counter.get(code, 0) + 1
    cards.close()
    return [code for code, count in sorted(counter.items(), key=lambda item: -item[1]) if count >= 3]


def existing_sources(database: Path) -> Dict[str, str]:
    """库里已有的系列说明及其来源（``series:0x..`` → source）。"""

    if not database.is_file():
        return {}
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return {
            str(key): str(source)
            for key, source in connection.execute(
                "SELECT plan_key, source FROM deck_plans WHERE kind = 'series'"
            )
        }
    finally:
        connection.close()


def build_prompt(setcode: int, members: Sequence[Tuple[int, str, str]], *, previous_error: str = "") -> str:
    """要一段"对手视角"的说明：它想干什么、关键动作、我该怎么拦。"""

    rows = [f"- {cid} {name}：{effect[:EFFECT_CHARS]}" for cid, name, effect in members[:MAX_MEMBERS]]
    block = "\n".join(rows) if rows else "（没有读到成员卡）"
    prompt = f"""你是游戏王 OCG 的卡组分析师。下面是一个系列（系列码 0x{setcode:x}）的卡表与效果。

请在 {MAX_NOTE_CHARS} 字以内写一段说明，**站在对手的角度**，严格按三段写（不要写别的、不要代码块）：
它想干什么：一句话点出这套牌的取胜方式与节奏（快攻/展开/控制）
关键动作：它最依赖的 1~3 张卡或动作（写卡名），为什么
怎么拦：我该把灰流丽/无效系/除外系交给哪一步，以及什么手段对它特别有效

卡表：
{block}

要求：
1. 只能提上面出现过的卡（写卡名或卡号都行，卡号必须来自上面）；
2. 全篇不超过 {MAX_NOTE_CHARS} 字，短句、动词开头；
3. 说不准就写"视情况"，不要编造这套牌没有的卡；
4. 如果这个系列只是泛用卡（没有成体系的打法），如实写"这是泛用卡，按单卡应对"。
"""
    if previous_error:
        prompt += f"\n上一次的输出不能用，原因是：{previous_error}\n请按三段格式重写。\n"
    return prompt


def validate_note(text: str, allowed: Sequence[int], setcode: int) -> Tuple[Optional[str], str]:
    """校验模型写的系列说明。"""

    body = (text or "").strip()
    if not body:
        return None, "模型没有返回内容"
    missing = [part for part in ("关键动作", "怎么拦") if part not in body]
    if missing:
        return None, "缺少必须的段落：" + "、".join(missing)
    allowed_set = {int(cid) for cid in allowed}
    unknown = sorted({int(t) for t in re.findall(r"(?<!\d)(\d{5,9})(?!\d)", body)} - allowed_set)
    if unknown:
        return None, "提到了这个系列没有的卡号：" + "、".join(str(cid) for cid in unknown[:5])
    if len(body) > MAX_NOTE_CHARS * 2:
        return None, f"写太长了（{len(body)} 字）"
    return body[:MAX_NOTE_CHARS], ""


def store_note(database: Path, setcode: int, body: str) -> None:
    """写进 ``deck_plans``（``series:0x..``，source='ai'）。"""

    connection = sqlite3.connect(database)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO deck_plans (plan_key, kind, deck_id, title, body, source, updated_at)"
            " VALUES (?, 'series', 0, ?, ?, 'ai', ?)",
            (f"series:0x{int(setcode) & 0xffff:x}", f"系列 0x{setcode:x}", body, time.time()),
        )
        connection.commit()
    finally:
        connection.close()


async def main_async(args: argparse.Namespace) -> int:
    import tomllib

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    database = default_knowledge_db(data_dir)
    paths = tomllib.loads((Path(args.plugin_root) / "config.toml").read_text(encoding="utf-8")).get("paths") or {}
    cards_cdb = Path(str(paths.get("cards_cdb") or "")) if paths.get("cards_cdb") else (
        Path(str(paths.get("ygopro_dir") or "")) / "cards.cdb"
    )
    if not cards_cdb.is_file():
        print(f"[错误] 找不到卡片数据库：{cards_cdb}")
        return 2

    if args.all_db or args.all_missing:
        existing = existing_sources(database)
        pool = pool_setcodes(data_dir)
        codes = all_db_setcodes(cards_cdb, min_members=args.min_members) if args.all_db else pool
        # **人工整理过的（curated）绝不能被模型覆盖**：那份是人写的，信息量与准确度都更高
        targets = [
            code
            for code in codes
            if existing.get(f"series:0x{code & 0xffff:x}") not in ("ai", "curated")
            and len(series_members(cards_cdb, code)) >= max(3, args.min_members)
        ]
        # 池子里的排前面：先保证"自己会遇到的那些"有说明，再补长尾
        in_pool = [code for code in targets if code in set(pool)]
        targets = in_pool + [code for code in targets if code not in set(pool)]
        if args.limit:
            targets = targets[: args.limit]
    elif args.setcode:
        targets = [parse_setcode(args.setcode)]
    else:
        print("[错误] 要么给 --setcode，要么给 --all-missing")
        return 2

    from duel.netguard import guarded_request
    from train.ai_brain import chat_with_settings, load_brain_model_settings

    settings = load_brain_model_settings(data_dir)
    if settings is None:
        print(f"[错误] 读不到 {data_dir / 'brain_model.toml'}")
        return 2
    print(f"要写的系列：{['0x%x' % code for code in targets]}（模型 {settings.model}）")

    done = 0
    for index, code in enumerate(targets, 1):
        members = series_members(cards_cdb, code)
        if len(members) < 3:
            print(f"  [跳过] 0x{code:x}：卡库里只有 {len(members)} 张成员")
            continue
        allowed = [cid for cid, _name, _effect in members]
        error = ""
        for attempt in range(1, max(1, args.attempts) + 1):
            prompt = build_prompt(code, members, previous_error=error)
            try:
                raw = await asyncio.to_thread(
                    chat_with_settings,
                    settings,
                    prompt,
                    request=guarded_request,
                    max_tokens=args.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001  模型出错要如实报出来
                print(f"  [错误] 0x{code:x} 第 {attempt} 轮调用失败：{exc}")
                break
            body, error = validate_note(raw, allowed, code)
            if body is None:
                continue
            print(f"\n[{index}/{len(targets)}] 0x{code:x}（{members[0][1]}…，{len(members)} 张成员）")
            print("  " + body.replace("\n", "\n  "))
            if args.activate:
                store_note(database, code, body)
            done += 1
            break
        else:
            print(f"  [失败] 0x{code:x}：{error}")

    print(f"\n完成 {done}/{len(targets)} 个系列" + ("（已写库）" if args.activate else "（未写库，加 --activate）"))
    return 0


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
