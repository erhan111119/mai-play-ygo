"""内置卡组登记表的单元测试。

这两件事出错都不会报错、只会静默退化，所以值得钉住：

* 名字表里写错一个出牌脚本名 → 那副卡组在群里显示成英文名（甚至根本不出现）；
* 卡表文件不在 → 不能把它登记进池子，否则开局时 WindBot 会静默换成随机卡组。

直接用 ``python tests/test_builtin_decks.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.builtin_decks import BUILTIN_DECK_NAMES, builtin_decks  # noqa: E402
from duel.windbot_decks import WIND_BOT_DECK_NAMES  # noqa: E402

MAIN: List[int] = [89631139, 89631139, 89631139] + [100000000 + index for index in range(37)]
EXTRA: List[int] = [100200001, 100200002, 100200002]

# WindBot 注册名 Test 与 Lucky 都指向 AI_Test：那是通用兜底脚本（DoEverythingExecutor），
# 不是真卡组，发行版里也没有 AI_Test.ydk，所以不该进卡组池。
GENERIC_SCRIPT_STEMS = {"AI_Test"}


def write_deck(path: Path) -> None:
    """写一副最小的合法 .ydk。"""

    lines = ["#main"] + [str(card) for card in MAIN] + ["#extra"] + [str(card) for card in EXTRA] + ["!side"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_name_table_matches_windbot_registry() -> None:
    """名字表的键必须是 WindBot 的注册名，且每个真卡组都要有中文名。"""

    unknown = sorted(set(BUILTIN_DECK_NAMES) - set(WIND_BOT_DECK_NAMES))
    assert not unknown, f"这些名字在 WindBot 的注册表里不存在：{unknown}"

    real_decks = {
        style for style, stem in WIND_BOT_DECK_NAMES.items() if stem not in GENERIC_SCRIPT_STEMS
    }
    missing = sorted(real_decks - set(BUILTIN_DECK_NAMES))
    assert not missing, f"这些内置卡组漏了中文名，会在群里显示英文：{missing}"
    assert not (GENERIC_SCRIPT_STEMS & {WIND_BOT_DECK_NAMES.get(s, "") for s in BUILTIN_DECK_NAMES})
    assert all(name.strip() for name in BUILTIN_DECK_NAMES.values()), "中文名不能为空"


def test_builtin_decks_use_chinese_names() -> None:
    """登记出来的卡组要带中文名与真实存在的 .ydk 路径。"""

    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory) / "windbot"
        decks_dir = windbot_dir / "Decks"
        decks_dir.mkdir(parents=True)
        write_deck(decks_dir / "AI_BlueEyes.ydk")
        write_deck(decks_dir / "AI_Kashtira.ydk")

        entries = builtin_decks(windbot_dir)
        assert [(style, name) for style, name, _path in entries] == [
            ("Blue-Eyes", "青眼白龙"),
            ("Kashtira", "怒刹帝利"),
        ], entries
        assert all(path.is_file() for _style, _name, path in entries)


def test_missing_deck_file_is_skipped() -> None:
    """卡表文件不存在的卡组不能被登记：名字写错时 WindBot 会静默换随机卡组。"""

    with tempfile.TemporaryDirectory() as directory:
        windbot_dir = Path(directory) / "windbot"
        decks_dir = windbot_dir / "Decks"
        decks_dir.mkdir(parents=True)
        write_deck(decks_dir / "AI_BlueEyes.ydk")
        # 名字表里有 Kashtira，但没有对应的 .ydk 文件
        entries = builtin_decks(windbot_dir)
        assert [style for style, _name, _path in entries] == ["Blue-Eyes"], entries


def test_empty_windbot_dir() -> None:
    """没有 Decks 目录时返回空表（由调用方决定怎么提示），不该抛异常。"""

    with tempfile.TemporaryDirectory() as directory:
        assert builtin_decks(Path(directory) / "nonexistent") == []


def main() -> int:
    """逐个执行测试函数。"""

    tests = [(name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures: List[str] = []
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
