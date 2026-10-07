"""给"库里有名字、却没有脚本"的卡补上脚本。

**为什么需要它**：社区包的卡号和我们一直在用的 DIY 卡号是**两套**（同一个卡组的同名卡
往往有两个号，一套繁体、一套简体，同名但字符串不同）。引擎加载脚本是按卡号找
``script/c<卡号>.lua`` 的——库里没有那张卡的脚本时，它在对局里就是"加载即报错"：

    [对局进程:err] "CallCardFunction"(c73090586.initial_effect): attempt to call an error function

也就是**卡在场上但效果永远不发动**（群里遇到的"明明发出来了却没反应"）。而这类卡的脚本
其实存在——只是挂在它的"孪生卡号"上。

**匹配规则（两条都要满足，宁可漏不可错）**：

1. ``datas`` 完全一致（类型/攻守/星级/种族/属性），2. 名字的字符重合度 ≥ 阈值
   （默认 0.7；简繁混排时同名卡的字符仍高度重合，"西艾蘿/西艾萝"能对上）。

**只新增、绝不覆盖**：目标文件已存在就跳过（不会动官方脚本库的版本）。

用法::

    uv run python tools/fix_missing_card_scripts.py            # 直接补
    uv run python tools/fix_missing_card_scripts.py --check-only
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import argparse
import re
import shutil
import sqlite3
import sys

# 卡脚本的文件名形状：script/c<卡号>.lua
_SCRIPT_NAME = re.compile(r"^c\d+\.lua$")
_SCRIPT_DIR = "script"
_DEFAULT_THRESHOLD = 0.7


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_paths(plugin_root: Path) -> Path:
    """从 config.toml 里读引擎目录（只解析 ``ygopro_dir`` 一行，不引入完整配置层）。"""

    config = plugin_root / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"读不到 {config}：{exc}") from exc
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ygopro_dir"):
            _, _, value = line.partition("=")
            path = value.strip().strip('"').strip("'").replace("\\\\", "\\")
            if path:
                return Path(path)
    raise SystemExit(f"{config} 里没有 ygopro_dir")


def similarity(left: str, right: str) -> float:
    """名字的字符重合度（多重集交集 / 左侧字符数）。"""

    a, b = Counter(left), Counter(right)
    total = sum(a.values())
    if total == 0:
        return 0.0
    return sum((a & b).values()) / total


def plan_fixes(
    cdb: Path, script_dir: Path, threshold: float
) -> Tuple[List[Tuple[int, str, int, float]], Dict[int, str]]:
    """算出"该给哪些卡号补脚本"；返回 ``(待补清单, 卡号→名字)``。"""

    connection = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True)
    data: Dict[int, str] = {
        int(card_id): str(signature)
        for card_id, signature in connection.execute(
            "select id, type||'/'||atk||'/'||def||'/'||level||'/'||race||'/'||attribute from datas"
        )
    }
    names: Dict[int, str] = {
        int(card_id): str(name) for card_id, name in connection.execute("select id, name from texts")
    }
    connection.close()

    have = {
        int(path.name[1:-4])
        for path in script_dir.iterdir()
        if path.is_file() and _SCRIPT_NAME.match(path.name)
    }

    pending: List[Tuple[int, str, int, float]] = []
    for card_id, name in names.items():
        if card_id in have:
            continue
        best_id = 0
        best_score = 0.0
        for twin in have:
            same_name = names.get(twin, "") == name
            # 同名＝同一张卡（异画/再版号之间 `datas` 可能有细微差别，例如只有 setcode 不同），
            # 这种情况直接认；不同名时才要求 datas 完全一致。
            if not same_name and data.get(twin) != data.get(card_id):
                continue
            score = similarity(name, names.get(twin, ""))
            if score > best_score:
                best_id, best_score = twin, score
        if best_id and best_score >= threshold:
            pending.append((card_id, name, best_id, round(best_score, 2)))
    pending.sort()
    return pending, names


def main() -> int:
    parser = argparse.ArgumentParser(description="给缺脚本的卡按孪生卡号补脚本（只新增，不覆盖）")
    parser.add_argument("--plugin-root", default=str(_plugin_root()), help="插件根目录（读 config.toml）")
    parser.add_argument("--ygopro-dir", default="", help="引擎目录；默认读 config.toml 的 ygopro_dir")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_THRESHOLD, help="名字重合度阈值")
    parser.add_argument("--check-only", action="store_true", help="只列要补哪些，不写文件")
    args = parser.parse_args()

    ygopro_dir = Path(args.ygopro_dir) if args.ygopro_dir else _read_paths(Path(args.plugin_root))
    cdb = ygopro_dir / "cards.cdb"
    script_dir = ygopro_dir / _SCRIPT_DIR
    if not cdb.is_file() or not script_dir.is_dir():
        raise SystemExit(f"引擎目录不完整：{cdb} / {script_dir}")

    pending, _ = plan_fixes(cdb, script_dir, args.threshold)
    print(f"待补脚本：{len(pending)} 个（阈值 {args.threshold}）")
    for card_id, name, twin, score in pending[:10]:
        print(f"  {card_id} {name} <- {twin}（重合 {score}）")
    if args.check_only:
        print("--check-only：没有写任何文件")
        return 0

    added = 0
    for card_id, _name, twin, _score in pending:
        destination = script_dir / f"c{card_id}.lua"
        if destination.exists():
            continue
        shutil.copyfile(script_dir / f"c{twin}.lua", destination)
        added += 1
    print(f"完成：新增 {added} 个脚本（已存在的跳过，绝不覆盖）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
