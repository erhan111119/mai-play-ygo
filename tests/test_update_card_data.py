"""超先行卡更新工具的单元测试（不需要联网、不碰真实卡库）。

这一段的价值在"别把卡库写坏"上：合并只会插入/更新**本工具补进去的那批卡**，
已发售卡的数据一行都不能动；解包只挑固定形状的条目，包里塞了越界路径要拒绝。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import sqlite3
import sys
import tempfile
import zipfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


def load_tool():
    """按文件路径加载工具模块（``tools/`` 不在包路径里）。"""

    spec = importlib.util.spec_from_file_location(
        "update_card_data", str(_PLUGIN_ROOT / "tools" / "update_card_data.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db(path: Path, cards: List[tuple]) -> None:
    """造一个最小的 cards.cdb（结构与本机一致）。"""

    tool = load_tool()
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE datas ("
        + ", ".join(f"{name}" for name in tool._DATAS_COLUMNS)
        + ")"
    )
    connection.execute(
        "CREATE TABLE texts (" + ", ".join(name for name in tool._TEXTS_COLUMNS) + ")"
    )
    for card_id, name in cards:
        connection.execute(
            "INSERT INTO datas VALUES (" + ", ".join("?" for _ in tool._DATAS_COLUMNS) + ")",
            (card_id,) + tuple([0] * (len(tool._DATAS_COLUMNS) - 1)),
        )
        values = [card_id, name] + [""] * (len(tool._TEXTS_COLUMNS) - 2)
        connection.execute(
            "INSERT INTO texts VALUES (" + ", ".join("?" for _ in tool._TEXTS_COLUMNS) + ")",
            tuple(values),
        )
    connection.commit()
    connection.close()


def test_schema_check_rejects_unknown_layout() -> None:
    """卡库结构对不上时必须报错——绝不硬着头皮往一个陌生结构里写。"""

    tool = load_tool()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "weird.cdb"
        connection = sqlite3.connect(str(path))
        connection.execute("CREATE TABLE datas (id INTEGER, name TEXT)")
        connection.commit()
        try:
            tool.check_schema(connection)
        except tool.UpdateError as exc:
            assert "结构不匹配" in str(exc)
        else:
            raise AssertionError("结构不一样本该报错")
        finally:
            connection.close()


def test_merge_inserts_missing_and_updates_only_installed() -> None:
    """合并规则：没见过的卡插入；本工具补过的更新；其余已发售卡一律不动。"""

    tool = load_tool()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "cards.cdb"
        make_db(target, [(100, "旧卡"), (200, "本工具补过的先行卡"), (300, "已发售卡")])

        overlay_rows = {
            100: {"datas": (100,) + (0,) * 10, "texts": tuple([100, "被包改过的旧卡"] + [""] * 17)},
            200: {"datas": (200,) + (0,) * 10, "texts": tuple([200, "更新后的先行卡"] + [""] * 17)},
            300: {"datas": (300,) + (0,) * 10, "texts": tuple([300, "被包改过的已发售卡"] + [""] * 17)},
            400: {"datas": (400,) + (0,) * 10, "texts": tuple([400, "新先行卡"] + [""] * 17)},
        }
        added, updated = tool.merge_into(target, overlay_rows, installed_before={200})

        assert (added, updated) == (1, 1), (added, updated)
        connection = sqlite3.connect(str(target))
        names = dict(connection.execute("SELECT id, name FROM texts"))
        connection.close()
        assert names[400] == "新先行卡", "库里没有的卡要插进来"
        assert names[200] == "更新后的先行卡", "本工具补过的卡要跟随上游更新"
        assert names[100] == "旧卡", "已发售卡不能被这份包覆盖"
        assert names[300] == "已发售卡", "已发售卡不能被这份包覆盖"


def test_merge_is_idempotent() -> None:
    """重复合并不该重复插入（第二次数出来是 0 张新增）。"""

    tool = load_tool()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "cards.cdb"
        make_db(target, [(100, "旧卡")])
        overlay_rows = {
            400: {"datas": (400,) + (0,) * 10, "texts": tuple([400, "新卡"] + [""] * 17)}
        }
        first = tool.merge_into(target, overlay_rows, installed_before=set())
        second = tool.merge_into(target, overlay_rows, installed_before={400})
        assert first == (1, 0), first
        assert second == (0, 1), second
        connection = sqlite3.connect(str(target))
        count = connection.execute("SELECT COUNT(*) FROM datas").fetchone()[0]
        connection.close()
        assert count == 2, count


def test_pick_members_only_takes_known_shapes() -> None:
    """解包只挑根目录的 .cdb 与 script/c<卡号>.lua，卡图与 pack 列表一概不要。"""

    tool = load_tool()
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "p.ypk"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("test-release.cdb", b"x")
            archive.writestr("script/c100200274.lua", b"--")
            archive.writestr("script/readme.txt", b"x")
            archive.writestr("pics/100200274.jpg", b"x")
            archive.writestr("pack/some.ydk", b"x")
        with zipfile.ZipFile(package) as archive:
            picked = sorted(info.filename for info in tool._pick_members(archive))
        assert picked == ["script/c100200274.lua", "test-release.cdb"], picked


def test_extract_rejects_zip_slip() -> None:
    """包里带 ../ 的条目要拒绝解压（zip slip），不能写到暂存目录外面。"""

    tool = load_tool()
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "evil.ypk"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../evil.cdb", b"x")
        staging = Path(directory) / "staging"
        try:
            tool.extract_package(package, staging)
        except tool.UpdateError as exc:
            assert "越界" in str(exc) or "拒绝" in str(exc)
        else:
            raise AssertionError("带 ../ 的条目本该被拒绝")
        assert not (Path(directory) / "evil.cdb").exists(), "绝不能写到暂存目录外面"


def test_script_name_shape() -> None:
    """脚本文件名只认 script/c<卡号>.lua。"""

    tool = load_tool()
    assert tool._SCRIPT_NAME.match("c100200274.lua")
    assert not tool._SCRIPT_NAME.match("c100200274.txt")
    assert not tool._SCRIPT_NAME.match("readme.lua")
    assert not tool._SCRIPT_NAME.match("cc1.lua")


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
