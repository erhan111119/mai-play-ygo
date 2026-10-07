"""卡组池的单元测试。

直接用 ``python tests/test_deckpool.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import random
import sqlite3
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.deckcode import Deck  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.deckpool import DeckPool, DeckPoolError  # noqa: E402

MAIN: List[int] = [89631139, 89631139, 89631139] + [100000000 + index for index in range(37)]
EXTRA: List[int] = [100200001, 100200002, 100200002]
SIDE: List[int] = [100300001]


def submit(pool: DeckPool, group_id: str, name: str, contributor: str = "群友A"):
    """往池子里投一副卡组，返回记录。"""

    deck = Deck(tuple(MAIN), tuple(EXTRA), tuple(SIDE))
    return pool.add(
        group_id=group_id,
        display_name=name,
        contributor_id=f"uid-{contributor}",
        contributor_name=contributor,
        ydk_text=deck.to_ydk(name),
        deck_code=deck.to_deck_code(),
        source_format=deck.source_format,
        main_count=len(deck.main),
        extra_count=len(deck.extra),
        side_count=len(deck.side),
    )


def test_add_and_list() -> None:
    """投稿后能查到，且 .ydk 真的落盘了；新投稿默认不进随机池。"""

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            stored = submit(pool, "111", "青眼白龙")
            assert stored.deck_id > 0
            assert stored.ydk_path.is_file(), "投稿应当生成 .ydk 文件"
            assert stored.main_count == 40
            assert stored.extra_count == 3
            assert stored.side_count == 1
            assert stored.in_random is False, "投稿要先自己 /加入随机 才会被抽到"

            decks = pool.list_decks("111")
            assert len(decks) == 1
            assert decks[0].display_name == "青眼白龙"
            assert pool.count("111") == 1

            # .ydk 内容要能被重新解析回同一副卡组
            from duel.deckcode import parse_deck_code

            reparsed = parse_deck_code(stored.ydk_path.read_text(encoding="utf-8"))
            assert reparsed.main == tuple(MAIN)
            assert reparsed.extra == tuple(EXTRA)
        finally:
            pool.close()


def test_pool_is_shared_across_groups() -> None:
    """卡组池**全局共享**：谁投稿的都可见，不再按群隔离。

    按要求改过：原先 A 群的投稿在 B 群看不到，结果是"私聊里找不到卡组、别的群投稿的用不了"。
    来源群号仍记着（列表里能看出来），但不再参与可见性判断。
    """

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            submit(pool, "111", "群一的卡组")
            submit(pool, "222", "群二的卡组")
            assert pool.count("111") == 2, "投稿数量是全局的"
            assert pool.count("222") == 2
            names = [deck.display_name for deck in pool.list_decks("333")]
            assert names == ["群一的卡组", "群二的卡组"], names
            assert [deck.display_name for deck in pool.own_decks("333")] == [
                "群一的卡组",
                "群二的卡组",
            ], "私聊（任意群号）都该看到全部投稿"
        finally:
            pool.close()


def test_random_pick() -> None:
    """随机抽取只看随机池：投稿默认不在池里，加进来之后才可能被抽到。"""

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            assert pool.random_pick("111") is None, "空池子应当返回 None"

            first = submit(pool, "111", "卡组甲")
            submit(pool, "111", "卡组乙")
            assert pool.random_pick("111") is None, "投稿默认不进随机池"
            assert pool.count_in_random("111") == 0

            pool.set_in_random(first.deck_id, True)
            picked = {pool.random_pick("111").display_name for _ in range(10)}
            assert picked == {"卡组甲"}, f"只有加进随机池的那副能抽到，实际 {picked}"
            assert pool.count_in_random("111") == 1

            pool.set_in_random(first.deck_id, False)
            assert pool.random_pick("111") is None, "移出随机池后又该抽不到了"
        finally:
            pool.close()


def test_random_pick_spreads_over_pool() -> None:
    """随机池里有多副时，多抽几次应当都能出现（注入固定种子保证可复现）。"""

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            submit(pool, "111", "卡组甲")
            submit(pool, "111", "卡组乙")
            for deck in pool.list_decks("111"):
                pool.set_in_random(deck.deck_id, True)

            chooser = random.Random(20260920)
            picked = {pool.random_pick("111", rng=chooser).display_name for _ in range(30)}
            assert picked == {"卡组甲", "卡组乙"}, f"两副都该有可能抽到，实际只抽到 {picked}"
        finally:
            pool.close()


def test_seed_builtin_decks() -> None:
    """内置卡组登记：进池、默认进随机池、幂等、名字/路径变化时对齐、消失时清理。"""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        decks_dir = root / "Decks"
        decks_dir.mkdir()
        blue = decks_dir / "AI_BlueEyes.ydk"
        blue.write_text("#main\n89631139\n#extra\n!side\n", encoding="utf-8")
        kashtira = decks_dir / "AI_Kashtira.ydk"
        kashtira.write_text("#main\n900000001\n#extra\n!side\n", encoding="utf-8")

        pool = DeckPool(root / "data")
        try:
            entries = [("Blue-Eyes", "青眼白龙", blue), ("Kashtira", "怒刹帝利", kashtira)]
            changed, removed = pool.seed_builtin_decks(entries)
            assert (changed, removed) == (2, 0), (changed, removed)

            builtin = [deck for deck in pool.list_decks("111") if deck.is_builtin]
            assert [deck.display_name for deck in builtin] == ["青眼白龙", "怒刹帝利"]
            assert all(deck.in_random for deck in builtin), "内置卡组登记时就在随机池里"
            assert pool.count("111") == 0, "内置卡组不算群友投稿"
            assert pool.count_all("111") == 2

            # 重复登记不该反复写库；只有名字/路径变化才算改动
            assert pool.seed_builtin_decks(entries) == (0, 0)
            renamed = [("Blue-Eyes", "青眼白龙（改）", blue), ("Kashtira", "怒刹帝利", kashtira)]
            assert pool.seed_builtin_decks(renamed) == (1, 0)

            # 群友把某副移出随机池后，下次启动不该被登记流程重新加回去
            target = [deck for deck in pool.list_decks("111") if deck.display_name.startswith("青眼白龙")][0]
            pool.set_in_random(target.deck_id, False)
            pool.seed_builtin_decks(renamed)
            after = [deck for deck in pool.list_decks("111") if deck.deck_id == target.deck_id][0]
            assert after.in_random is False, "登记流程不该覆盖群友做过的取舍"

            # 本机不再有的内置卡组要从池子里清掉（换 WindBot 版本后的情况）
            changed, removed = pool.seed_builtin_decks([("Blue-Eyes", "青眼白龙（改）", blue)])
            assert (changed, removed) == (0, 1), (changed, removed)
            assert [deck.display_name for deck in pool.list_decks("111")] == ["青眼白龙（改）"]
        finally:
            pool.close()


def test_builtin_deck_is_protected() -> None:
    """内置卡组不能删（会连 WindBot 自己的 .ydk 一起删掉），清空投稿也不该动它。"""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        decks_dir = root / "Decks"
        decks_dir.mkdir()
        blue = decks_dir / "AI_BlueEyes.ydk"
        blue.write_text("#main\n89631139\n#extra\n!side\n", encoding="utf-8")

        pool = DeckPool(root / "data")
        try:
            pool.seed_builtin_decks([("Blue-Eyes", "青眼白龙", blue)])
            builtin = pool.list_decks("111")[0]
            try:
                pool.remove("111", builtin.deck_id)
            except DeckPoolError as exc:
                assert "内置" in str(exc)
            else:
                raise AssertionError("删内置卡组应当报错")
            assert blue.is_file(), "内置卡组的 .ydk 不该被动到"

            submit(pool, "111", "群友投稿")
            assert pool.delete_all("111") == 1, "清空只清投稿"
            assert [deck.display_name for deck in pool.list_decks("111")] == ["青眼白龙"]
        finally:
            pool.close()


def test_fixed_deck_works_for_builtin_and_across_groups() -> None:
    """固定卡组要能固定内置卡组，并且对所有群生效。

    这条是实测踩出来的两个坑：内置卡组挂在保留群号 ``__builtin__`` 下，而老实现按
    「本群 + deck_id」去查，固定内置卡组时根本查不到 → 静默失效还顺手清掉了设置；
    另外固定值原先按群各存一份，在私聊里固定过的卡组到了群里就不认了。
    """

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        decks_dir = root / "Decks"
        decks_dir.mkdir()
        blue = decks_dir / "AI_BlueEyes.ydk"
        blue.write_text("#main\n89631139\n#extra\n!side\n", encoding="utf-8")

        pool = DeckPool(root / "data")
        try:
            pool.seed_builtin_decks([("Blue-Eyes", "青眼白龙", blue)])
            builtin = pool.list_decks("111")[0]
            assert builtin.is_builtin

            pool.set_fixed_deck(builtin.deck_id)
            fixed = pool.fixed_deck()
            assert fixed is not None, "固定内置卡组必须能取回来"
            assert fixed.deck_id == builtin.deck_id
            # 换一个群（模拟另一个群开局）也该认这个固定值
            picked = pool.pick_for_duel("999")
            assert picked is not None and picked.deck_id == builtin.deck_id, picked
            # 固定的是内置卡组时，列表里要能标出来
            assert any(deck.deck_id == builtin.deck_id for deck in pool.list_decks("999"))

            # 取消固定后回到随机池抽取
            pool.set_fixed_deck(None)
            assert pool.fixed_deck() is None
            assert pool.pick_for_duel("999") is not None, "取消固定后应当从随机池里抽（内置卡组在池里）"

            # 固定别群的投稿：本群列表里也要能看到（否则群友不知道自己固定的是哪副）
            other = submit(pool, "222", "别群的卡组")
            pool.set_fixed_deck(other.deck_id)
            assert pool.fixed_deck().deck_id == other.deck_id
            assert other.deck_id in [deck.deck_id for deck in pool.list_decks("111")]
            assert other.deck_id not in [deck.deck_id for deck in pool.random_pool("111")], (
                "别群投稿不该因为被固定就混进本群的随机池"
            )

            # 固定卡组被删掉时，固定设置要跟着清掉，不能留下悬空引用
            pool.remove("222", other.deck_id)
            assert pool.fixed_deck() is None
        finally:
            pool.close()


def test_legacy_group_fixed_deck_migrates_to_global() -> None:
    """老库里按群存的固定卡组要搬成全局值，群友不用重新固定一次。"""

    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory)
        first = DeckPool(data_dir)
        try:
            stored = submit(first, "111", "老卡组")
            # 造一份「老版本」留下的按群固定记录
            first._connection.execute(
                "INSERT INTO group_settings (group_id, fixed_deck_id) VALUES (?, ?)",
                ("111", stored.deck_id),
            )
            first._connection.execute("DELETE FROM settings WHERE key = 'fixed_deck_id'")
            first._connection.commit()
        finally:
            first.close()

        second = DeckPool(data_dir)
        try:
            fixed = second.fixed_deck()
            assert fixed is not None, "重开库时要沿用老库里的固定设置"
            assert fixed.deck_id == stored.deck_id
        finally:
            second.close()


def test_remove_cleans_file() -> None:
    """删除投稿要连 .ydk 一起清掉，避免数据目录里留垃圾。"""

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            stored = submit(pool, "111", "待删除")
            assert stored.ydk_path.is_file()
            assert pool.remove("111", stored.deck_id) is True
            assert not stored.ydk_path.exists(), ".ydk 应当被删除"
            assert pool.count("111") == 0
            assert pool.remove("111", stored.deck_id) is False, "重复删除应当返回 False"
        finally:
            pool.close()


def test_remove_works_from_any_group() -> None:
    """共享池里从任何群/私聊都能删同一副投稿；内置卡组仍然删不掉。"""

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            stored = submit(pool, "111", "群一的卡组")
            assert pool.remove("222", stored.deck_id) is True, "共享池不该有群边界"
            assert pool.count("111") == 0
        finally:
            pool.close()


def test_delete_all_only_touches_the_calling_group() -> None:
    """`/清空卡组` 只清本群投稿：别群的行与 .ydk 都要留着。

    这条是回归测试（2026-10-07 评审指出的坑）：以前 `delete_all` 按"全池投稿"删文件、
    却只 `DELETE ... WHERE group_id = ?` 删本群的行——别群的记录会变成指向已删文件的悬空引用，
    之后随机抽到那副牌，机器人一张都出不了牌，而且不报错。
    """

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            mine_a = submit(pool, "111", "本群甲")
            mine_b = submit(pool, "111", "本群乙")
            other = submit(pool, "222", "别群的")
            assert pool.delete_all("111") == 2
            assert not mine_a.ydk_path.exists() and not mine_b.ydk_path.exists()
            # 别群的牌：行还在、文件也还在（否则就是悬空引用）
            assert other.ydk_path.is_file(), "别群的 .ydk 被误删了"
            remaining = pool.own_decks("222")
            assert [deck.deck_id for deck in remaining] == [other.deck_id]
            assert all(deck.ydk_path.is_file() for deck in pool.own_decks("222"))
            assert pool.delete_all("111") == 0, "再清一次应返回 0"
        finally:
            pool.close()


def test_group_dir_name_is_sanitised() -> None:
    """群号当目录名要过白名单：`/`、`\\`、`..` 之类不能拼进数据目录。"""

    from duel.deckpool import _group_dir_name

    assert _group_dir_name("27dc88f323272580afdcce4edf26b5be") == "27dc88f323272580afdcce4edf26b5be"
    assert _group_dir_name("100907480") == "100907480"
    assert _group_dir_name("__builtin__") == "__builtin__"
    for bad in ("../evil", "a/b", "a\\b", "..", "", "x" * 200):
        cleaned = _group_dir_name(bad)
        assert "/" not in cleaned and "\\" not in cleaned, (bad, cleaned)
        assert ".." not in cleaned, (bad, cleaned)
        assert 0 < len(cleaned) <= 64, (bad, cleaned)

    # 端到端：恶意群号投稿后，文件必须落在数据目录里面
    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory))
        try:
            stored = submit(pool, "../evil", "越界测试")
            data_dir = Path(directory).resolve()
            assert data_dir in stored.ydk_path.resolve().parents, stored.ydk_path
            assert stored.ydk_path.is_file()
        finally:
            pool.close()


def test_default_windbot_deck_applied() -> None:
    """未显式指定风格卡组时应当套用配置里的默认值。"""

    with tempfile.TemporaryDirectory() as directory:
        pool = DeckPool(Path(directory), default_windbot_deck="Blue-Eyes")
        try:
            stored = submit(pool, "111", "随便一副")
            assert stored.windbot_deck == "Blue-Eyes"
            explicit = pool.add(
                group_id="111",
                display_name="指定风格",
                contributor_id="uid-x",
                contributor_name="群友B",
                ydk_text=Deck(tuple(MAIN), (), ()).to_ydk(),
                deck_code="x",
                source_format="code",
                main_count=40,
                extra_count=0,
                side_count=0,
                windbot_deck="Kashtira",
            )
            assert explicit.windbot_deck == "Kashtira"
        finally:
            pool.close()


def test_persistence_across_reopen() -> None:
    """重开数据库后投稿仍在（插件重启不丢卡组）。"""

    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory)
        first = DeckPool(data_dir)
        try:
            submit(first, "111", "持久化验证")
        finally:
            first.close()

        second = DeckPool(data_dir)
        try:
            decks = second.list_decks("111")
            assert len(decks) == 1
            assert decks[0].display_name == "持久化验证"
        finally:
            second.close()


def test_generated_script_field_and_migration() -> None:
    """专属脚本字段可写可清；老库（没有这一列）打开时要自动补上。"""

    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory)
        # 先造一个「老结构」的库：decks 表里没有 generated_script 列
        legacy = sqlite3.connect(data_dir / "deck_pool.db")
        legacy.execute(
            """
            CREATE TABLE decks (
                deck_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL, display_name TEXT NOT NULL,
                contributor_id TEXT NOT NULL, contributor_name TEXT NOT NULL,
                ydk_path TEXT NOT NULL, deck_code TEXT NOT NULL, source_format TEXT NOT NULL,
                main_count INTEGER NOT NULL, extra_count INTEGER NOT NULL, side_count INTEGER NOT NULL,
                windbot_deck TEXT NOT NULL, created_at REAL NOT NULL
            )
            """
        )
        legacy.execute(
            "INSERT INTO decks VALUES (1, '111', '老卡组', 'u', '群友', 'x.ydk', 'code', 'code', 40, 3, 1, 'Test', 0)"
        )
        legacy.commit()
        legacy.close()

        pool = DeckPool(data_dir)
        try:
            decks = pool.list_decks("111")
            assert len(decks) == 1, "迁移后老数据要还在"
            assert decks[0].generated_script == "", "老库补列后默认值应为空"

            pool.set_generated_script(decks[0].deck_id, "Gen9")
            assert pool.list_decks("111")[0].generated_script == "Gen9"
            pool.set_generated_script(decks[0].deck_id, None)
            assert pool.list_decks("111")[0].generated_script == ""

            # 后加的列也要在同一个迁移里补齐：产出脚本名（`generated_script`）与挑定的脚本名
            assert decks[0].picked_style == "", "老库补列后默认值应为空"
            pool.set_picked_style(decks[0].deck_id, "PlanAware")
            again = pool.list_decks("111")[0]
            assert again.picked_style == "PlanAware", again.picked_style
            # 列序读串的话这几个字段会互相污染，所以顺手确认一下没串
            assert again.generated_script == "", again.generated_script
        finally:
            pool.close()


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
