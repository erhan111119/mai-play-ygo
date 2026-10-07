"""知识库的测试：只读查询、缺库退化、检索裁剪、事实抽取、线路组装。

这些是"查库决策"能不能信的前提：
* **缺库必须等于"没有知识"**，绝不能让对局因为缺数据出错；
* **检索必须有硬上限**（提示词被撑爆比没有知识更糟）；
* **事实抽取要认得出自肃与"能拦什么"**（决策里最容易踩的就是这两个）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import importlib.util
import sqlite3
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.knowledge import SCHEMA, Knowledge  # noqa: E402  导入顺序受 sys.path 补丁影响


def load_tool(name: str):
    """按文件路径加载 tools/ 下的脚本。"""

    spec = importlib.util.spec_from_file_location(
        f"tool_{name}", str(_PLUGIN_ROOT / "tools" / f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_knowledge_db(root: Path, *, cards_cdb: bool = True) -> Path:
    """造一个迷你知识库（外加一份迷你 cards.cdb，用来验证系列推断）。"""

    database = root / "knowledge" / "knowledge.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO card_facts (card_id, name, kinds, hits, events, timing, limit_kind,"
        " self_lock, negate_what, is_interaction, from_zones, confidence, evidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1001, "测试坑", "disable", "search,ss", "连锁中", "quick", "once_per_turn",
         "", "effect", 1, "hand", "script", "脚本，动作在第 3 行"),
    )
    connection.execute(
        "INSERT INTO card_facts (card_id, name, kinds, hits, events, timing, limit_kind,"
        " self_lock, negate_what, is_interaction, from_zones, confidence, evidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1002, "测试展开件", "ss,draw", "", "自由时点", "ignition,quick", "once_per_turn",
         "no_search", "", 0, "hand", "script", "脚本，动作在第 5 行"),
    )
    # 卡组里会出现的卡（2001～2004）：让"卡组线路"的组装测试能拿到真名字
    for card_id, name, kinds, extra in (
        (2001, "本家起手件", "ss,draw", ("once_per_turn", "", 0)),
        (2002, "本家补充件", "search", ("none", "", 0)),
        (2003, "本家终端", "negate", ("once_per_turn", "", 0)),
        (2004, "普通打手", "", ("none", "", 0)),
    ):
        limit_kind, self_lock, is_interaction = extra
        connection.execute(
            "INSERT INTO card_facts (card_id, name, kinds, hits, events, timing, limit_kind,"
            " self_lock, negate_what, is_interaction, from_zones, confidence, evidence)"
            " VALUES (?, ?, ?, '', '', 'ignition', ?, ?, '', ?, '', 'script', '脚本')",
            (card_id, name, kinds, limit_kind, self_lock, is_interaction),
        )
    connection.execute(
        "INSERT INTO deck_plans (plan_key, kind, deck_id, title, body, source, updated_at)"
        " VALUES ('deck:7', 'deck', 7, '测试卡组', '先出 A 再出 B', 'curated', 0)"
    )
    connection.execute(
        "INSERT INTO deck_plans (plan_key, kind, deck_id, title, body, source, updated_at)"
        " VALUES ('series:0x64', 'series', 0, '测试系列', '对手主轴：检索特召一体，拦检索点', 'curated', 0)"
    )
    connection.execute(
        "INSERT INTO interactions (handtrap_id, target_kind, verdict, why, source)"
        " VALUES (1001, 'search', 'hit', '测试坑 对「检索」：该交', 'curated')"
    )
    connection.commit()
    connection.close()

    if cards_cdb:
        cdb = root / "cards.cdb"
        connection = sqlite3.connect(cdb)
        connection.execute("CREATE TABLE datas (id INTEGER PRIMARY KEY, setcode INTEGER)")
        connection.executemany(
            "INSERT INTO datas (id, setcode) VALUES (?, ?)",
            [(2001, 100), (2002, 100), (2003, 100), (2004, 0)],
        )
        connection.commit()
        connection.close()
    return database


def test_missing_database_means_no_knowledge() -> None:
    """没有知识库时一切查询都是"空"，不能抛异常。"""

    with tempfile.TemporaryDirectory() as directory:
        knowledge = Knowledge(Path(directory) / "nope.db")
        assert knowledge.available is False
        assert knowledge.card_facts(1001) is None
        assert knowledge.plan("deck:7") is None
        assert knowledge.interactions(1001) == []
        assert knowledge.counts() == {}
        assert knowledge.retrieve(deck_id=7, card_id=1001, opponent_cards=[2001]) == []


def test_queries_use_binding_and_tolerate_weird_input() -> None:
    """查询走参数绑定：SQL 元字符只会"查不到"，不会改结构、不会报错。"""

    with tempfile.TemporaryDirectory() as directory:
        database = make_knowledge_db(Path(directory))
        knowledge = Knowledge(database, cards_db=Path(directory) / "cards.cdb")
        try:
            assert knowledge.available is True
            assert knowledge.plan("deck:7' OR '1'='1") is None
            assert knowledge.deck_plan(7) is not None
            assert knowledge.series_plan(0x64).title == "测试系列"
            facts = knowledge.card_facts(1001)
            assert facts is not None and facts.is_interaction and "search" in facts.hits
            assert "能拦：" in facts.describe()
            rows = knowledge.interactions(1001)
            assert rows and rows[0][1] == "hit" and "该交" in rows[0][2], rows
            # "能做"与"能拦"完全重合时不再重复啰嗦（灰流丽不是检索卡）
            from duel.knowledge import CardFact

            only_hits = CardFact(
                card_id=1, name="纯阻抗", kinds=("search",), hits=("search",), events=(),
                timing=("quick",), limit_kind="none", self_lock=(), is_interaction=True,
                from_zones=("hand",), confidence="script", evidence="",
            )
            assert "能做" not in only_hits.describe()
        finally:
            knowledge.close()


def test_retrieve_is_capped_and_prioritised() -> None:
    """检索喂料必须**有上限**，而且按"这张牌 → 我的线路 → 对手系列"的顺序给。"""

    with tempfile.TemporaryDirectory() as directory:
        database = make_knowledge_db(Path(directory))
        knowledge = Knowledge(database, cards_db=Path(directory) / "cards.cdb")
        try:
            items = knowledge.retrieve(
                deck_id=7, card_id=1001, opponent_cards=[2001, 2002, 2003, 2004]
            )
            assert items and items[0].startswith("【这张牌的事实】")
            assert any("我这副牌的线路" in item for item in items)
            assert any("对手系列" in item for item in items)
            # 上限：条数与总字数
            many = knowledge.retrieve(
                deck_id=7, card_id=1001, opponent_cards=[2001, 2002, 2003], limit=2
            )
            assert len(many) <= 2
            long_body = "字" * 5000
            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT OR REPLACE INTO deck_plans"
                " (plan_key, kind, deck_id, title, body, source, updated_at)"
                " VALUES ('deck:8', 'deck', 8, '超长', ?, 'auto', 0)",
                (long_body,),
            )
            connection.commit()
            connection.close()
            trimmed = knowledge.retrieve(deck_id=8, card_id=0, limit=4)
            assert trimmed and len(trimmed[0]) <= 300, len(trimmed[0])
        finally:
            knowledge.close()


def test_series_inference_needs_three_cards() -> None:
    """对手主轴要 ≥3 张同系列才算——一张卡说明不了它在打什么体系。"""

    with tempfile.TemporaryDirectory() as directory:
        database = make_knowledge_db(Path(directory))
        knowledge = Knowledge(database, cards_db=Path(directory) / "cards.cdb")
        try:
            assert knowledge._series_from_cards([2001, 2002, 2003, 2004]) == [100]
            assert knowledge._series_from_cards([2001, 2002, 2004]) == []
        finally:
            knowledge.close()


def test_extract_facts_reads_script_and_text() -> None:
    """事实抽取：动作、时机、自肃、"能拦什么"都要认出来；没有脚本时退回文本。"""

    tool = load_tool("build_card_facts")
    script = "\n".join(
        [
            "function c1.initial_effect(c)",
            "  local e1=Effect.CreateEffect(c)",
            "  e1:SetCategory(CATEGORY_DISABLE)",
            "  e1:SetType(EFFECT_TYPE_QUICK_O)",
            "  e1:SetCode(EVENT_CHAINING)",
            "  e1:SetRange(LOCATION_HAND)",
            "  e1:SetCountLimit(1,1)",
            "  e1:SetCondition(c1.con)",
            "  c:RegisterEffect(e1)",
            "end",
            "function c1.con(e,tp,eg,ep,ev,re,r,rp)",
            "  local ex2=Duel.GetOperationInfo(ev,CATEGORY_SEARCH)",
            "  return ex2 and Duel.IsChainDisablable(ev)",
            "end",
        ]
    )
    fact = tool.extract_facts(1, "测试坑", script, "把手卡丢弃才能发动。这个回合，不能用抽卡以外的方法拿牌。")
    assert fact.is_interaction == 1, fact
    assert "disable" in fact.kinds
    assert "search" in fact.hits, fact.hits
    assert "quick" in fact.timing
    assert fact.limit_kind == "once_per_turn"
    assert "no_search" in fact.self_lock, fact.self_lock
    assert "连锁中" in fact.events
    assert fact.confidence == "script"

    # 没有脚本：只从文本认动作，且标成低置信度
    text_fact = tool.extract_facts(2, "白板", None, "从卡组把 1 张卡加入手卡。")
    assert text_fact.confidence == "text"
    assert "search" in text_fact.kinds and "to_hand" in text_fact.kinds

    # 别名（异画）：脚本沿用别人的，证据里要说清楚
    alias_fact = tool.extract_facts(3, "异画", script, "丢弃手卡", alias_note="别名 1")
    assert "别名 1" in alias_fact.evidence


def test_deck_plan_composition_is_deduped_and_capped() -> None:
    """线路组装：去重、只写前几张、总数有上限。"""

    building = load_tool("build_deck_plans")
    notes = load_tool("series_notes")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = make_knowledge_db(root)
        knowledge = Knowledge(database, cards_db=root / "cards.cdb")
        notes.DECK_NOTES[7] = "先出 A 再出 B（测试用的卡组说明）"
        sections = {"main": [2001, 2001, 2001, 2002, 2002], "extra": [2003], "side": []}
        try:
            body = building.compose_deck_body(
                knowledge, deck_id=7, display_name="测试卡组", sections=sections, notes=notes
            )
            assert "先出 A 再出 B" in body, body
            assert body.count("本家起手件") <= 1, body        # 同名重复要去掉
            assert "本家起手件" in body and "本家终端" in body, body
            assert len(body) <= building.MAX_BODY_CHARS
        finally:
            knowledge.close()
        ydk = root / "deck.ydk"
        ydk.write_text("#main\n2001\n2001\n#extra\n2003\n!side\n2002\n", encoding="utf-8")
        parsed = building.read_deck_cards(ydk)
        assert parsed["main"] == [2001, 2001] and parsed["extra"] == [2003]
        assert parsed["side"] == [2002], parsed


def test_prompt_includes_retrieved_knowledge() -> None:
    """检索到的知识要真的进提示词，并且明确标注"可能过时、以局面为准"。"""

    from train.ai_brain import KIND_ACTIVATE, Question, build_prompt

    question = Question(question_id=1, kind=KIND_ACTIVATE, card_id=1001, card_name="测试坑", raw={})
    prompt = build_prompt(
        question,
        None,
        deck_name="测试卡组",
        playbook="summon_order=1",
        combo_guide="先展开再压制",
        retrieved=["【这张牌的事实】测试坑｜能拦：search/ss｜可用作阻抗"],
    )
    assert "【知识库" in prompt
    assert "测试坑｜能拦" in prompt
    assert "以局面为准" in prompt


def test_wrong_knowledge_does_not_break_the_decision() -> None:
    """知识库出错（或者对象给错）时，问模型这一步必须照常跑完。"""

    from train.ai_brain import KIND_ACTIVATE, Question, make_model_decider

    class ExplodingKnowledge:
        def retrieve(self, **_kwargs):
            raise RuntimeError("模拟知识库炸了")

    captured: List[str] = []

    def fake_request(url, *, data, headers, timeout):  # noqa: ARG001
        import json as _json

        body = _json.loads(data.decode("utf-8"))
        captured.append(body["messages"][0]["content"])
        return _json.dumps({"choices": [{"message": {"content": "决定：发动"}, "finish_reason": "stop"}]}).encode()

    decider = make_model_decider(
        _settings(),
        request=fake_request,
        knowledge=ExplodingKnowledge(),
        deck_id=7,
    )
    answer = decider(Question(question_id=1, kind=KIND_ACTIVATE, card_id=1001, card_name="测试坑", raw={}))
    assert answer == "yes", answer
    assert captured and "【知识库" not in captured[0], captured


def _settings():
    """造一份模型设置（测试里不会真的发请求）。"""

    from train.ai_brain import BrainModelSettings

    return BrainModelSettings(
        base_url="https://example.invalid", api_key="k", model="m", max_tokens=32, timeout=5
    )


def test_retrieve_works_from_a_worker_thread() -> None:
    """检索必须能在**别的线程**里跑通。

    答复任务跑在 ``asyncio.to_thread`` 的线程里，而 SQLite 连接不能跨线程使用——
    实测踩过：不按线程开连接时每次都抛 "SQLite objects created in a thread ..."，
    异常被上层吞掉、表面看只是"没有知识"，整条对照腿白跑（数字看起来完全正常）。
    """

    import threading

    with tempfile.TemporaryDirectory() as directory:
        database = make_knowledge_db(Path(directory))
        knowledge = Knowledge(database, cards_db=Path(directory) / "cards.cdb")
        # 先在主线程查一次（建立主线程的连接），再去子线程查——两条路都要能用
        assert knowledge.card_facts(1001) is not None
        result: List[object] = []
        errors: List[BaseException] = []

        def worker() -> None:
            try:
                result.append(
                    knowledge.retrieve(deck_id=7, card_id=1001, opponent_cards=[2001, 2002, 2003])
                )
            except BaseException as exc:  # noqa: BLE001  线程里要把异常带出来
                errors.append(exc)
            finally:
                # 每个线程各有一条连接，临时目录回收前要把它关掉（否则 Windows 上删不掉文件）
                knowledge.close()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        assert not errors, errors
        assert result and result[0], "子线程里检索不到东西"
        knowledge.close()


def test_full_deck_plan_is_fed_whole_when_asked_for() -> None:
    """"这一步做什么"要把**整份**展开流程喂进去；其它问题只喂两行摘要。

    顺序性知识截短了就没用了（"先 A 后 B"变成"A、B"），而那正是模型做不出来的东西；
    但"要不要发动某张牌"只需要一两句事实，喂整篇反而淹掉局面。
    """

    with tempfile.TemporaryDirectory() as directory:
        database = make_knowledge_db(Path(directory))
        knowledge = Knowledge(database, cards_db=Path(directory) / "cards.cdb")
        long_plan = "起手：A→检索B；步骤：1) 召唤A 2) 发动B；终场：C" + "补" * 600
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT OR REPLACE INTO deck_plans (plan_key, kind, deck_id, title, body, source, updated_at)"
            " VALUES ('deck:9', 'deck', 9, '长流程', ?, 'ai', 0)",
            (long_plan,),
        )
        connection.commit()
        connection.close()
        try:
            short = knowledge.retrieve(deck_id=9, card_id=0, limit=1)
            assert short and "…" in short[0], short          # 摘要会被截断
            full = knowledge.retrieve(deck_id=9, card_id=0, limit=1, full_deck_plan=True)
            assert full and "…" not in full[0], full          # 整份不截断
            assert len(full[0]) > len(short[0]) * 2
        finally:
            knowledge.close()


def test_deck_plan_tool_validates_card_ids_and_sections() -> None:
    """展开流程生成器的校验：缺段落、编造卡号、超长都要被拒（宁可没有也不喂错资料）。"""

    tool = load_tool("build_deck_plan_ai")
    allowed = [1001, 1002, 2001]
    # 格式要求是"按起手张数分档"：单卡起手 / 两卡起手 / 终场 / 自肃 / 被断后
    ok, error = tool.validate_plan(
        "单卡起手：1001→检索2001，做出2001\n"
        "两卡起手：1001+1002→先召唤再检索\n"
        "终场：2001站场\n自肃：无\n被断后：改走1002",
        allowed,
    )
    assert ok is not None and not error, error
    bad, error = tool.validate_plan("单卡起手：1001→检索\n自肃：无", allowed)
    assert bad is None and "终场" in error, error
    cheated, error = tool.validate_plan(
        "单卡起手：1001→发动99999999\n终场：2001", allowed
    )
    assert cheated is None and "99999999" in error, error
    huge, error = tool.validate_plan(
        "单卡起手：1001\n终场：" + "字" * 4000, allowed
    )
    assert huge is None and "太长" in error, error
    # 紧贴中文的卡号也要能被查出来（实测踩过：Python 的 \b 把中文当单词字符，漏检）
    tight, error = tool.validate_plan("单卡起手：发动99999999\n终场：2001", allowed)
    assert tight is None and "99999999" in error, error


def test_extract_facts_classifies_traps_and_punish_handtraps() -> None:
    """陷阱/速攻魔法也要算"能在对手回合动手"；惩罚型手坑的"能拦什么"来自它响应的事件。

    实测教训：只看脚本里的 ``EFFECT_TYPE_QUICK_O`` 会漏掉泡影、指名者这类**陷阱/速攻魔法**
    （它们的脚本用 ``EFFECT_TYPE_ACTIVATE``）——1,161 张阻抗里漏了一半以上，
    而"能拦什么"是"该不该交坑"这条线唯一的原料。
    """

    tool = load_tool("build_card_facts")
    trap_script = "\n".join([
        "function c1.initial_effect(c)",
        "  local e1=Effect.CreateEffect(c)",
        "  e1:SetCategory(CATEGORY_DISABLE)",
        "  e1:SetType(EFFECT_TYPE_ACTIVATE)",      # 陷阱/速攻魔法都长这样
        "  e1:SetCode(EVENT_FREE_CHAIN)",
        "  c:RegisterEffect(e1)",
        "end",
    ])
    # 陷阱（type & 0x4）→ 算阻抗，"能拦"补成泛化标签
    trap = tool.extract_facts(1, "测试陷阱", trap_script, "无效场上的卡的效果。", card_type=0x4)
    assert trap.is_interaction == 1, trap
    assert trap.hits, "阻抗的能拦不能是空的"
    assert "any_effect" in trap.hits or "field_card" in trap.hits, trap.hits

    # 惩罚型手坑（增殖的G 那种）：只响应事件、不无效任何东西
    punish_script = "\n".join([
        "function c2.initial_effect(c)",
        "  local e1=Effect.CreateEffect(c)",
        "  e1:SetCategory(CATEGORY_DRAW)",
        "  e1:SetType(EFFECT_TYPE_QUICK_O)",
        "  e1:SetCode(EVENT_SPSUMMON_SUCCESS)",
        "  c:RegisterEffect(e1)",
        "end",
    ])
    punish = tool.extract_facts(2, "惩罚手坑", punish_script, "对手特殊召唤时抽 1。", card_type=0x21)
    assert punish.is_interaction == 1, punish
    assert "特召" in punish.hits, punish.hits

    # 普通通常怪兽 + 无 quick 效果：不算阻抗
    plain = tool.extract_facts(3, "白板", None, "通常怪兽。", card_type=0x1)
    assert plain.is_interaction == 0


def test_decision_log_backfills_outcome_per_duel() -> None:
    """决策日志要能回填"这一局的结果"，否则日志里只有"问了什么"、看不出"后来怎样"。

    护栏来源：第一次统计时 ``outcome`` 列 4,442 行**全空**——AI 的干预到底帮没帮上忙，
    事后从数据里根本判不出来，只能每次再烧几百局做对照实验。
    """

    from duel.knowledge import DecisionLog, duel_outcome_text

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "knowledge" / "knowledge.db"
        first = DecisionLog(database, arena="room:abc", deck_key="89", duel_key="duel-1")
        first.add(kind="activate", card_id=111, answer="no", cost_ms=1500)
        first.add(kind="idle_action", card_id=0, answer="3", cost_ms=9000)
        assert first.finish_duel(duel_outcome_text(result="win", turns=5, lp_self=8000)) == 2
        # 重复回填不覆盖第一次的结论
        assert first.finish_duel(duel_outcome_text(result="loss")) == 0
        first.close()

        # 另一局（另一个实例）不该被上一局的结果污染
        second = DecisionLog(database, arena="room:abc", deck_key="89", duel_key="duel-2")
        second.add(kind="activate", card_id=222, answer="yes", cost_ms=800)
        second.close()

        connection = sqlite3.connect(database)
        try:
            rows = dict(connection.execute("SELECT duel_key, outcome FROM decisions").fetchall())
        finally:
            connection.close()
        assert rows["duel-1"] == "result=win;turns=5;lp=8000:0", rows
        assert rows["duel-2"] == "", "没回填的那一局应当还是空"


def test_last_duel_reads_trace_and_slowest_questions() -> None:
    """``/复盘`` 要的两样东西：这一局的概况（含胜负）与最费时间的几问。"""

    from duel.knowledge import DecisionLog, duel_outcome_text

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "knowledge" / "knowledge.db"
        log = DecisionLog(database, arena="room:xyz", deck_key="86", duel_key="duel-a")
        log.add(kind="activate", card_id=11, answer="no", cost_ms=1200)
        log.add(kind="activate", card_id=12, answer="no", cost_ms=1100)
        log.add(kind="activate", card_id=13, answer="yes", cost_ms=900)
        log.add(kind="idle_action", card_id=0, answer="2", cost_ms=21000)
        log.finish_duel(duel_outcome_text(result="loss", turns=3, lp_self=0, lp_other=8000))
        log.close()

        knowledge = Knowledge(database)
        try:
            trace = knowledge.last_duel("room:xyz")
            assert trace is not None
            assert trace.result == "loss", trace.outcome
            assert trace.asks == 4 and trace.kinds == {"activate": 3, "idle_action": 1}, trace.kinds
            # 否决率只按 activate 那一类算：3 问里 2 个 no
            assert trace.vetos == 2 and trace.activate_asks == 3, trace.describe()
            assert "否决发动 2/3 = 67%" in trace.describe(), trace.describe()
            assert trace.slowest_ms == 21000
            slowest = knowledge.duel_decisions(trace.duel_key, limit=2)
            assert slowest[0][0] == "idle_action" and slowest[0][3] == 21000, slowest
            # 别的来源查不到东西（房间之间互不串）
            assert knowledge.last_duel("room:other") is None
        finally:
            knowledge.close()


def test_old_knowledge_db_gets_duel_key_column() -> None:
    """老库（``decisions`` 还没有 ``duel_key`` 列）要能自动补列，读取侧也不能炸。

    ``CREATE TABLE IF NOT EXISTS`` 不会改动已有表——补列发生在写入侧，
    而 ``/复盘`` 与 review_decisions 是只读打开的，所以读之前必须先问一声有没有这一列。
    """

    from duel.knowledge import DecisionLog

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "knowledge" / "knowledge.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        # 造一张"老"表：结构与 0.19.0 之前一致（有 opponent_key，没有 duel_key）
        legacy = sqlite3.connect(database)
        legacy.execute(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY, created_at REAL, arena TEXT,"
            " deck_key TEXT, opponent_key TEXT, kind TEXT, card_id INTEGER, board_json TEXT,"
            " retrieved_json TEXT, answer TEXT, audit TEXT, cost_ms INTEGER, outcome TEXT)"
        )
        legacy.execute(
            "INSERT INTO decisions (created_at, arena, deck_key, kind, answer, cost_ms, outcome)"
            " VALUES (1.0, 'room:old', '89', 'activate', 'no', 500, '')"
        )
        legacy.commit()
        legacy.close()

        # 老数据读不出来也不算错（没有局标识就没法聚）
        knowledge = Knowledge(database)
        try:
            assert knowledge.last_duel("room:old") is None
        finally:
            knowledge.close()

        # 写入侧一碰就会补上列，之后新写的决策就能被复盘读到
        log = DecisionLog(database, arena="room:old", deck_key="89", duel_key="duel-new")
        try:
            log.add(kind="activate", card_id=1, answer="no", cost_ms=600)
        finally:
            log.close()
        knowledge = Knowledge(database)
        try:
            trace = knowledge.last_duel("room:old")
            assert trace is not None and trace.duel_key == "duel-new", trace
        finally:
            knowledge.close()


def test_review_decisions_groups_veto_rate_by_result() -> None:
    """「否决与胜负」要按局分组算否决率，而不是拿单条决策当样本。

    护栏来源：日志里同一局有几十条决策，按条平均会把长局放大好几倍；而且"否决对不对"
    只能跟**这一局的胜负**放在一起看（相关关系，不是因果）。
    """

    import contextlib
    import io

    from duel.knowledge import DecisionLog, duel_outcome_text

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "knowledge" / "knowledge.db"
        # 赢的局：问 2 次、否决 0 次；输的局：问 2 次、否决 2 次
        for key, result, answers in (
            ("win-1", "win", ("yes", "yes")),
            ("loss-1", "loss", ("no", "no")),
        ):
            log = DecisionLog(database, arena="room:x", deck_key="89", duel_key=key)
            try:
                for index, answer in enumerate(answers):
                    log.add(kind="activate", card_id=index + 1, answer=answer, cost_ms=100)
                log.finish_duel(duel_outcome_text(result=result, turns=2))
            finally:
                log.close()

        connection = sqlite3.connect(database)
        try:
            duels = tool_review().fetch_by_duel(connection)
        finally:
            connection.close()
        assert len(duels) == 2, duels

        tool = tool_review()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            tool.report_outcomes(tool.filter_duels(duels, 0, "room:x"))
        text = buffer.getvalue()
        assert "赢的局" in text and "输的局" in text, text
        assert "100%" in text and "0%" in text, text
        assert "更爱拦车" in text, text
        # 按来源/卡组筛：不匹配就什么都不显示
        assert tool.filter_duels(duels, 86, "") == []
        assert tool.filter_duels(duels, 0, "room:nope") == []


def tool_review():
    """按文件路径加载复盘工具（它的聚合逻辑要单独测）。"""

    return load_tool("review_decisions")


def main() -> int:
    """逐个执行测试函数。"""

    tests = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
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
