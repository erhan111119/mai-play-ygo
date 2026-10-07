"""给**重要卡**逐张写"怎么用 / 该不该拦 / 威胁多大"（模型精写，覆盖推导层）。

推导层（``tools/build_card_notes.py``）已经给全 15,205 张卡写了一行，而且是零成本、可复算的；
但它只能从事实里"拼"结论，说不出**具体怎么拦、拦在哪一步**。这里让模型逐张写——
为什么不一次写全卡：15,205 次调用要二十多个小时，而**大部分卡是白板/没人带**。
所以只写"重要"的那些：威胁=高或中、或者进过卡组、或者是常用干扰牌（默认约 1,400 张）。

用法::

    python tools/build_card_notes_ai.py --important            # 推出来的"高/中"那些
    python tools/build_card_notes_ai.py --deck-cards 83        # 某副牌的全部卡
    python tools/build_card_notes_ai.py --important --limit 50 # 先做 50 张
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, default_data_dir, default_knowledge_db  # noqa: E402

MAX_NOTE_CHARS = 220
"""单卡说明的长度上限（进提示词的素材要短，长了会挤掉局面本身）。"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="给重要卡逐张写用法/拦法/威胁")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--important", action="store_true", help="推导出的威胁=高或中的那些")
    parser.add_argument("--deck-cards", type=int, default=0, help="只做这副牌（卡组编号）的卡")
    parser.add_argument("--limit", type=int, default=0, help="最多做几张（0 = 不限）")
    parser.add_argument("--activate", action="store_true", help="写进知识库")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=512)
    return parser.parse_args()


def pick_cards(connection: sqlite3.Connection, args: argparse.Namespace) -> List[int]:
    """挑要写的卡：按威胁度与"有没有人带"排序（重要的先来）。"""

    if args.deck_cards:
        import tomllib

        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
        paths = tomllib.loads((Path(args.plugin_root) / "config.toml").read_text(encoding="utf-8")).get("paths") or {}
        pool = sqlite3.connect(f"file:{(data_dir / 'deck_pool.db').as_posix()}?mode=ro", uri=True)
        row = pool.execute("SELECT ydk_path FROM decks WHERE deck_id = ?", (int(args.deck_cards),)).fetchone()
        pool.close()
        if row is None:
            return []
        cards: List[int] = []
        for line in Path(str(row[0])).read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                cards.append(int(stripped))
        return list(dict.fromkeys(cards))

    rows = connection.execute(
        "SELECT card_id FROM card_notes WHERE source = 'derived'"
        " AND (threat LIKE '高%' OR threat LIKE '中%') ORDER BY card_id"
    ).fetchall()
    cards = [int(row[0]) for row in rows]
    already = {
        int(row[0]) for row in connection.execute("SELECT card_id FROM card_notes WHERE source = 'ai'")
    }
    return [card_id for card_id in cards if card_id not in already]


def card_texts(cards_cdb: Path, card_id: int) -> Tuple[str, str]:
    connection = sqlite3.connect(f"file:{cards_cdb.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT name, desc FROM texts WHERE id = ?", (int(card_id),)
        ).fetchone()
    finally:
        connection.close()
    return (str(row[0]), str(row[1] or "").replace("\r", " ")) if row else (f"#{card_id}", "")


def build_prompt(name: str, effect: str, derived_threat: str) -> str:
    """要三行：怎么用 / 该不该拦 / 威胁。"""

    return f"""你是游戏王 OCG 的卡牌分析师。就下面这一张卡，写三行结论（不要写别的、不要代码块）：

卡名：{name}
效果：{effect[:400]}
（库里已有的推导结论，可以修正它：{derived_threat}）

请输出三行，每行 ≤60 字：
怎么用：这张卡在什么局面下出、配合什么
该不该拦：对手用它的时候，我该不该交坑、交给哪一步（不是引擎就直说优先级别低）
威胁：高／中／低 + 一句话理由

要求：不确定就写"视情况"；不要编造这张卡没有的效果；不要写卡组之外的卡名。
"""


def validate(text: str) -> Tuple[Optional[str], str]:
    body = (text or "").strip()
    if not body:
        return None, "模型没有返回内容"
    if "该不该拦" not in body and "拦" not in body:
        return None, "没写「该不该拦」"
    if "威胁" not in body:
        return None, "没写「威胁」"
    return body[: MAX_NOTE_CHARS * 2], ""


def store_note(database: Path, card_id: int, body: str, *, usage: str, threat: str) -> None:
    """写进 ``card_notes``（source='ai'；推导层不动）。"""

    connection = sqlite3.connect(database)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO card_notes (card_id, usage, block_advice, threat, source, updated_at)"
            " VALUES (?, ?, ?, ?, 'ai', ?)",
            (int(card_id), usage, body, threat, time.time()),
        )
        connection.commit()
    finally:
        connection.close()


async def main_async(args: argparse.Namespace) -> int:
    import tomllib

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(Path(args.plugin_root))
    database = default_knowledge_db(data_dir)
    if not database.is_file():
        print(f"[错误] 还没有知识库：{database}")
        return 2
    paths = tomllib.loads((Path(args.plugin_root) / "config.toml").read_text(encoding="utf-8")).get("paths") or {}
    cards_cdb = Path(str(paths.get("cards_cdb") or "")) if paths.get("cards_cdb") else (
        Path(str(paths.get("ygopro_dir") or "")) / "cards.cdb"
    )

    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        targets = pick_cards(connection, args)
        derived: Dict[int, str] = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT card_id, threat FROM card_notes WHERE source='derived'")
        }
    finally:
        connection.close()
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("没有要写的卡（都已经写过了，或筛选条件没命中）")
        return 0

    from duel.netguard import guarded_request
    from train.ai_brain import chat_with_settings, load_brain_model_settings

    settings = load_brain_model_settings(data_dir)
    if settings is None:
        print(f"[错误] 读不到 {data_dir / 'brain_model.toml'}")
        return 2
    print(f"要写的卡 {len(targets)} 张（模型 {settings.model}）")

    done = 0
    for index, card_id in enumerate(targets, 1):
        name, effect = card_texts(cards_cdb, card_id)
        error = ""
        for attempt in range(1, max(1, args.attempts) + 1):
            prompt = build_prompt(name, effect, derived.get(card_id, ""))
            if error:
                prompt += f"\n上一次的输出不能用：{error}\n请按三行格式重写。\n"
            try:
                raw = await asyncio.to_thread(
                    chat_with_settings,
                    settings,
                    prompt,
                    request=guarded_request,
                    max_tokens=args.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001  模型出错要如实报出来
                print(f"  [错误] {name} 第 {attempt} 轮调用失败：{exc}")
                break
            body, error = validate(raw)
            if body is None:
                continue
            usage = ""
            threat = derived.get(card_id, "")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("怎么用"):
                    usage = stripped.split("：", 1)[-1].split(":", 1)[-1].strip()
                elif stripped.startswith("威胁"):
                    threat = stripped.split("：", 1)[-1].split(":", 1)[-1].strip()
            if args.activate:
                store_note(database, card_id, body, usage=usage, threat=threat)
            done += 1
            if index % 25 == 0 or index == len(targets):
                print(f"  [{index}/{len(targets)}] {name}：{usage or body[:40]}")
            break
        else:
            print(f"  [失败] {name}：{error}")

    print(f"\n完成 {done}/{len(targets)} 张" + ("（已写库）" if args.activate else "（未写库）"))
    return 0


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
