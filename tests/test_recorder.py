"""对局记录器与卡牌数据库的单元测试。

直接用 ``python tests/test_recorder.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import sqlite3
import struct
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.cards import CardDatabase, CardDatabaseError  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.protocol import (  # noqa: E402
    CardLocation,
    Ctos,
    Frame,
    Msg,
    Stoc,
)
from duel.recorder import DuelRecorder  # noqa: E402
from duel.protocol import SPECIAL_WIN_REASON_MIN, WIN_REASONS  # noqa: E402

# 测试里用到的两张卡
BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414


def make_card_db(directory: str) -> CardDatabase:
    """在临时目录里造一个最小可用的 cards.cdb。"""

    path = Path(directory) / "cards.cdb"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT)")
    connection.executemany(
        "INSERT INTO texts (id, name) VALUES (?, ?)",
        [(BLUE_EYES, "青眼白龙"), (DARK_MAGICIAN, "黑魔导")],
    )
    connection.commit()
    connection.close()
    return CardDatabase(path)


def game_msg(message_id: int, payload: bytes = b"") -> Frame:
    """构造一条服务器发来的游戏报文。"""

    return Frame(Stoc.GAME_MSG, bytes([int(message_id)]) + payload)


def stoc(message_id: int, payload: bytes = b"") -> Frame:
    """构造一条服务器发来的非游戏报文。"""

    return Frame(message_id, payload)


def feed_all(recorder: DuelRecorder, frames: List[Frame]) -> None:
    """把一串报文按「服务器发给客户端」的方向喂进记录器。"""

    for frame in frames:
        recorder.feed("to_client", frame)


def build_full_duel_frames() -> List[Frame]:
    """造一局结构完整的小对局。"""

    return [
        stoc(Stoc.TYPE_CHANGE, bytes([0x10])),
        stoc(Stoc.HS_PLAYER_ENTER, "机器人".encode("utf-16-le").ljust(40, b"\x00") + bytes([0])),
        stoc(Stoc.HS_PLAYER_ENTER, "群友A".encode("utf-16-le").ljust(40, b"\x00") + bytes([1])),
        stoc(Stoc.DUEL_START),
        # 开打：MSG_START 说明"我在对局里是 0 号玩家"。两套编号一致时名字照抄；
        # 不一致（服务器为先后手重排）的情况见 test_fieldstate 里的用例
        game_msg(Msg.START, bytes([0])),
        # 第 1 回合：座位 0 先攻
        game_msg(Msg.NEW_TURN, bytes([0])),
        game_msg(Msg.NEW_PHASE, struct.pack("<h", 1)),
        game_msg(Msg.DRAW, bytes([0, 1]) + struct.pack("<I", BLUE_EYES)),
        game_msg(Msg.SPSUMMONING, struct.pack("<IBBbb", BLUE_EYES, 0, 4, 0, 1)),
        game_msg(Msg.CHAINING, struct.pack("<IBBbbBhi", DARK_MAGICIAN, 1, 8, 0, 0, 1, 0, 100)),
        game_msg(Msg.CHAIN_END),
        # 第 2 回合：座位 1 反击并打脸
        game_msg(Msg.NEW_TURN, bytes([1])),
        game_msg(Msg.ATTACK, bytes([1, int(CardLocation.MONSTER_ZONE), 0, 1, 0, 0, 0, 0])),
        game_msg(Msg.DAMAGE, struct.pack("<Bi", 0, 2400)),
        game_msg(Msg.LPUPDATE, struct.pack("<Bi", 0, 5600)),
        game_msg(Msg.MOVE, struct.pack("<IBBbBBBbBI", BLUE_EYES, 0, 4, 0, 1, 0, 16, 0, 1, 0x40)),
        # 座位 0 投降
        game_msg(Msg.WIN, bytes([1, 0])),
        stoc(Stoc.DUEL_END),
    ]


def test_full_duel_summary() -> None:
    """完整一局应当播报出胜负、回合数、先攻方与各自统计。"""

    with tempfile.TemporaryDirectory() as directory:
        with make_card_db(directory) as card_db:
            recorder = DuelRecorder(card_db=card_db, group_id="12345")
            feed_all(recorder, build_full_duel_frames())

            assert recorder.started and recorder.finished
            assert recorder.turn_count == 2
            assert recorder.first_player_seat == 0
            assert recorder.winner_seat == 1
            assert recorder.self_seat == 0

            summary = "\n".join(recorder.summary_lines())
            assert "群友A（对方） 获胜" in summary, summary
            assert "投降" in summary
            assert "共 2 个回合" in summary
            assert "机器人（我方） 先攻" in summary
            assert "青眼白龙" in summary, "应当把卡 ID 翻成卡名"
            assert "2400" in summary

            data = recorder.to_dict()
            assert data["winner_seat"] == 1
            assert data["turns"] == 2
            players = data["players"]
            assert players["1"]["attacks"] == 1
            assert players["1"]["direct_attacks"] == 1
            assert players["1"]["damage_dealt"] == 2400, "输出伤害应记在攻击方身上"
            assert players["0"]["damage_taken"] == 2400
            assert players["0"]["lp_final"] == 5600
            assert players["0"]["sp_summons"] == 1
            assert players["1"]["effects"] == 1
            assert players["0"]["sent_to_grave"] == 1
            assert data["unparsed_frames"] == 0


def test_missing_win_is_reported_honestly() -> None:
    """没观测到胜负报文时要如实说结果未知，而不是猜一个。"""

    with tempfile.TemporaryDirectory() as directory:
        with make_card_db(directory) as card_db:
            recorder = DuelRecorder(card_db=card_db)
            frames = [
                frame for frame in build_full_duel_frames() if frame.payload[:1] != bytes([Msg.WIN])
            ]
            feed_all(recorder, frames)

            result = recorder.build_result()
            assert result.winner_seat is None
            assert result.resolved, "对局本身是结束了的，只是结果未知"
            summary = "\n".join(recorder.summary_lines())
            assert "结果未知" in summary, summary
            assert "获胜" not in summary


def test_unfinished_duel() -> None:
    """对局没结束时不播报胜负。"""

    recorder = DuelRecorder()
    feed_all(
        recorder,
        [
            stoc(Stoc.DUEL_START),
            game_msg(Msg.NEW_TURN, bytes([0])),
            game_msg(Msg.NEW_TURN, bytes([1])),
        ],
    )
    lines = recorder.summary_lines()
    assert len(lines) == 1
    assert "尚未结束" in lines[0]
    assert "2 个回合" in lines[0]


def test_set_is_counted_as_play() -> None:
    """盖放要记进统计与用卡台账（内核为它单发 MSG_SET，原来整条报文没人接）。"""

    with tempfile.TemporaryDirectory() as directory:
        with make_card_db(directory) as card_db:
            recorder = DuelRecorder(card_db=card_db)
            feed_all(
                recorder,
                [
                    stoc(Stoc.DUEL_START),
                    game_msg(Msg.START, bytes([0])),
                    game_msg(Msg.NEW_TURN, bytes([0])),
                    # 怪兽里侧盖放：卡号已知（自己盖的）
                    game_msg(Msg.SET, struct.pack("<IBBbb", BLUE_EYES, 0, int(CardLocation.MONSTER_ZONE), 1, 8)),
                    # 魔陷盖放：对面盖的，内核只给卡号 0（隐藏）
                    game_msg(Msg.SET, struct.pack("<IBBbb", 0, 1, int(CardLocation.SPELL_ZONE), 0, 8)),
                    game_msg(Msg.WIN, bytes([0, 0])),
                    stoc(Stoc.DUEL_END),
                ],
            )

            assert recorder.players[0].sets == 1
            assert recorder.players[1].sets == 1
            assert recorder.players[0].normal_summons == 0, "盖放不是召唤，两栏要分开记"
            assert recorder.card_usage.get(BLUE_EYES) == 1, "自己盖的卡要进用卡台账"
            assert 0 not in recorder.card_usage, "隐藏卡号（0）不该进台账"

            data = recorder.to_dict()
            assert data["players"]["0"]["sets"] == 1
            summary = "\n".join(recorder.summary_lines())
            assert "盖放 1 次" in summary, summary


def test_card_usage_splits_by_seat() -> None:
    """用卡台账按座位分开记：``card_usage`` 只含我方，对手的走 ``opponent_card_usage``。

    为什么要拆（2026-10-07）：以前是一张**双方混记**的表，于是 `/优化卡组` 的"死牌"判定与
    `first_turn_accept.py` 会把"对手打过的同一张卡"算成我们也用过（实测 r24~r27 里有 15 局是
    "只有对手带这张卡却进了台账"）。播报的"最活跃的卡"同样会混进对手的卡。
    """

    with tempfile.TemporaryDirectory() as directory:
        with make_card_db(directory) as card_db:
            recorder = DuelRecorder(card_db=card_db)
            feed_all(
                recorder,
                [
                    stoc(Stoc.DUEL_START),
                    game_msg(Msg.START, bytes([0])),      # 我方＝座位 0
                    game_msg(Msg.NEW_TURN, bytes([0])),
                    # 我方召唤青眼白龙、对面召唤黑魔导
                    game_msg(Msg.SUMMONING, struct.pack("<IBBbb", BLUE_EYES, 0, int(CardLocation.HAND), 0, 8)),
                    game_msg(Msg.SUMMONING, struct.pack("<IBBbb", DARK_MAGICIAN, 1, int(CardLocation.HAND), 0, 8)),
                    game_msg(Msg.WIN, bytes([0, 0])),
                    stoc(Stoc.DUEL_END),
                ],
            )

            assert recorder.card_usage == {BLUE_EYES: 1}, recorder.card_usage
            assert recorder.opponent_card_usage == {DARK_MAGICIAN: 1}, recorder.opponent_card_usage
            assert DARK_MAGICIAN not in recorder.card_usage, "对手的卡不能进我方台账"


def test_cards_seen_counts_draw_and_search() -> None:
    """「进过手牌」的做法：抽到的卡与被检索/回收进手的卡都要记，对手的隐藏牌不记。

    为什么需要这一份（2026-10-07）：`card_usage` 只回答"用过没有"，回答不了"到没到手"——
    而"该不该加第几张"要的正是分母（使用率 ÷ 到手率，见 `tools/card_vitality.py`）。
    """

    recorder = DuelRecorder()
    feed_all(
        recorder,
        [
            stoc(Stoc.DUEL_START),
            game_msg(Msg.START, bytes([0])),
            game_msg(Msg.NEW_TURN, bytes([0])),
            # 起手：抽到青眼白龙（有卡号）
            game_msg(Msg.DRAW, bytes([0, 1]) + struct.pack("<I", BLUE_EYES)),
            # 对手抽牌：客户端只看到 0 占位 ⇒ 不能算他"到手"了
            game_msg(Msg.DRAW, bytes([1, 1]) + struct.pack("<I", 0)),
            # 检索：deck → hand 也进手
            game_msg(
                Msg.MOVE,
                struct.pack(
                    "<IBBbBBBbBI", DARK_MAGICIAN, 0, int(CardLocation.DECK), 0, 1, 0,
                    int(CardLocation.HAND), 0, 1, 0x02,
                ),
            ),
            # 进墓（同一控制者）不该算进手
            game_msg(
                Msg.MOVE,
                struct.pack(
                    "<IBBbBBBbBI", BLUE_EYES, 0, int(CardLocation.MONSTER_ZONE), 0, 1, 0,
                    int(CardLocation.GRAVE), 0, 1, 0x40,
                ),
            ),
            # 被对手拿走进到**对手**手里：不算我们的手牌资源
            game_msg(
                Msg.MOVE,
                struct.pack(
                    "<IBBbBBBbBI", BLUE_EYES, 0, int(CardLocation.DECK), 0, 1, 1,
                    int(CardLocation.HAND), 0, 1, 0x02,
                ),
            ),
        ],
    )

    assert recorder.card_seen == {BLUE_EYES: 1, DARK_MAGICIAN: 1}, recorder.card_seen
    assert recorder.opponent_card_seen == {}, "对手的隐藏手牌不该被算作他的'到手'"


def test_big_hit_recorded_as_highlight() -> None:
    """达到阈值的单次伤害要进亮点列表。"""

    recorder = DuelRecorder()
    recorder.feed("to_client", stoc(Stoc.DUEL_START))
    recorder.feed("to_client", game_msg(Msg.DAMAGE, struct.pack("<Bi", 1, 3000)))
    recorder.feed("to_client", game_msg(Msg.DAMAGE, struct.pack("<Bi", 1, 500)))

    assert recorder.players[1].biggest_hit_taken == 3000
    assert recorder.players[0].damage_dealt == 3500
    assert any("3000" in item for item in recorder.highlights), recorder.highlights
    assert not any("500" in item for item in recorder.highlights), "低于阈值不该记亮点"


def test_malformed_frame_counted_not_fatal() -> None:
    """坏包只计数，不能中断整局记录。"""

    recorder = DuelRecorder()
    recorder.feed("to_client", stoc(Stoc.DUEL_START))
    recorder.feed("to_client", game_msg(Msg.NEW_PHASE, b"\x01"))  # 长度不足
    recorder.feed("to_client", game_msg(Msg.NEW_TURN, bytes([1])))

    assert recorder.unparsed_frames == 1
    assert recorder.turn_count == 1, "坏包之后仍应继续统计"
    assert any("未能解析" in line for line in recorder.summary_lines())


def test_to_server_frames_ignored() -> None:
    """客户端发往服务器的报文不应影响统计。"""

    recorder = DuelRecorder()
    recorder.feed("to_server", game_msg(Msg.NEW_TURN, bytes([0])))
    recorder.feed("to_server", Frame(Ctos.SURRENDER, b""))
    assert recorder.turn_count == 0
    assert not recorder.finished


def test_record_without_card_db() -> None:
    """没有卡牌数据库时退化成编号，而不是报错。"""

    recorder = DuelRecorder(card_db=None)
    feed_all(
        recorder,
        [
            stoc(Stoc.DUEL_START),
            game_msg(Msg.NEW_TURN, bytes([0])),
            game_msg(Msg.SPSUMMONING, struct.pack("<IBBbb", BLUE_EYES, 0, 4, 0, 1)),
            game_msg(Msg.WIN, bytes([0, 1])),
            stoc(Stoc.DUEL_END),
        ],
    )
    summary = "\n".join(recorder.summary_lines())
    assert f"#{BLUE_EYES}" in summary, summary
    assert "机器人" not in summary, "没有昵称时不该编一个出来"


def test_card_database_lookup() -> None:
    """卡名查询、未知卡退化与缓存都要正确。"""

    with tempfile.TemporaryDirectory() as directory:
        database = make_card_db(directory)
        assert database.available
        assert database.name(BLUE_EYES) == "青眼白龙"
        assert database.name(999999999) is None
        assert database.describe(BLUE_EYES) == "「青眼白龙」"
        assert database.describe(999999999) == "未知卡(999999999)"
        assert database.warm_up([BLUE_EYES, DARK_MAGICIAN, 999999999]) == 2
        database.close()


def test_card_database_errors_are_explicit() -> None:
    """路径缺失、文件不存在、表结构不对，都要明确报错而不是静默返回空名。"""

    assert CardDatabase(None).available is False

    with tempfile.TemporaryDirectory() as directory:
        missing = CardDatabase(Path(directory) / "不存在.cdb")
        try:
            missing.name(BLUE_EYES)
        except CardDatabaseError as exc:
            assert "不存在" in str(exc)
        else:
            raise AssertionError("文件缺失时本应抛出 CardDatabaseError")

        wrong_schema = Path(directory) / "wrong.cdb"
        connection = sqlite3.connect(wrong_schema)
        connection.execute("CREATE TABLE other (id INTEGER)")
        connection.commit()
        connection.close()
        bad = CardDatabase(wrong_schema)
        try:
            bad.name(BLUE_EYES)
        except CardDatabaseError as exc:
            assert "texts" in str(exc)
        else:
            raise AssertionError("表结构不对时本应抛出 CardDatabaseError")


def test_win_reason_labels_follow_kernel_strings_conf() -> None:
    """胜负原因文案要跟内核自带 strings.conf 的 !victory 对齐。

    这里曾经写错过（把 0x1 当成"特殊胜利"、0x2 当成"生命值或卡组耗尽"），
    结果训练日志里一堆普通打空血的局被标成「特殊胜利条件达成」，战报与数据都被带偏。
    """

    assert WIN_REASONS[0x0] == "投降"
    assert WIN_REASONS[0x1] == "基本分变成0"
    assert WIN_REASONS[0x2] == "没有卡可抽"
    assert WIN_REASONS[0x3] == "超时"
    assert 0x10 not in WIN_REASONS, "0x10 起是逐卡的特殊胜利，不该硬编码进这张表"
    assert SPECIAL_WIN_REASON_MIN == 0x10

    # 记录器要把「基本分变成0」如实翻出来，而不是笼统说特殊胜利
    recorder = DuelRecorder()
    recorder.winner_seat = 0
    recorder.win_reason = 0x1
    assert recorder.build_result().reason == "基本分变成0"
    recorder.win_reason = 0x10
    assert recorder.build_result().reason == "特殊胜利"


def test_card_database_works_from_any_thread() -> None:
    """卡库查询必须能在**别的线程**里跑通。

    实测踩过（用户报"突然卡住"）：记录器的报文回调跑在**闸门自己的线程**里，而卡库连接是在
    插件主线程建立的——SQLite 连接不能跨线程用，于是每次"某张卡被除外/被破坏"的记录都抛
    ``sqlite3.ProgrammingError: SQLite objects created in a thread ...``，
    日志里刷满「报文观测回调失败」，对局还在继续但记录与播报缺数据。
    """

    import threading

    with tempfile.TemporaryDirectory() as directory:
        cdb = Path(directory) / "cards.cdb"
        connection = sqlite3.connect(cdb)
        connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT, desc TEXT)")
        connection.execute("INSERT INTO texts (id, name) VALUES (987654, '跨线测试卡')")
        connection.commit()
        connection.close()

        cards = CardDatabase(cdb)
        # 主线程先查一次（建立主线程的连接），再去子线程查
        assert cards.name(987654) == "跨线测试卡"
        cards._cache.clear()  # 逼子线程真的去查库，而不是命中缓存
        errors: List[BaseException] = []
        result: List[object] = []

        def worker() -> None:
            try:
                result.append(cards.describe(987654))
            except BaseException as exc:  # noqa: BLE001  线程里要把异常带出来
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        assert not errors, errors
        assert result and result[0] == "「跨线测试卡」", result
        cards.close()


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
