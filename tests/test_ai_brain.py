"""逐步问 AI 通道的单元测试（纯逻辑 + 假模型，不真打对局）。

这块出错的后果很隐蔽：答复认错、id 对不上、答非所问被硬猜——都是"看起来在互动、其实在瞎指挥"，
所以口径必须钉死。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import asyncio
import sys
import tempfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from train.ai_brain import (  # noqa: E402  导入顺序受 sys.path 补丁影响
    KIND_ACTIVATE,
    KIND_ATTACK_TARGET,
    KIND_IDLE,
    SCOPE_ALL,
    SCOPE_HIGH_STAKES,
    BrainServer,
    Question,
    build_prompt,
    condense_combo_guide,
    decide_with_rule,
    needs_model,
    normalise_answer,
    parse_question,
)

ACTIVATE_QUESTION = """id=7
kind=activate
turn=3
my_lp=6200
opp_lp=7100
card=14558127
card_name=灰流丽
hand=14558127;灰流丽;0;0;1
mine=30118811;备份员@火灵天星;1200;800;1
theirs=89631139;青眼白龙;3000;2500;1
"""

ATTACK_QUESTION = """id=8
kind=attack_target
turn=5
my_lp=5200
opp_lp=6100
card=30118811
card_name=备份员@火灵天星
hand=14558127;灰流丽;0;0;1
mine=30118811;备份员@火灵天星;1200;800;1
theirs=89631139;青眼白龙;3000;2500;1
theirs=46986414;黑魔导;2500;2100;4
choice_ids=89631139,46986414
choice_names=青眼白龙,黑魔导
grave_mine=3
grave_theirs=1
"""


def test_parse_question() -> None:
    """问题解析：认得出 id/kind/卡号与候选；缺 id 或缺 kind 时不猜。"""

    question = parse_question(ACTIVATE_QUESTION)
    assert question is not None
    assert question.question_id == 7 and question.kind == KIND_ACTIVATE, question
    assert question.card_id == 14558127 and question.turn == 3, question
    assert question.my_lp == 6200 and question.opp_lp == 7100, question

    attack = parse_question(ATTACK_QUESTION)
    assert attack is not None and attack.choice_ids == [89631139, 46986414], attack
    assert attack.choice_names == ["青眼白龙", "黑魔导"], attack
    # 场面：每张卡一行、分号分隔（卡名里可能有逗号，所以不能用逗号拼一行）
    theirs = attack.zone("theirs")
    assert [(card.name, card.attack, card.defense) for card in theirs] == [
        ("青眼白龙", 3000, 2500),
        ("黑魔导", 2500, 2100),
    ], theirs
    assert attack.zone("hand")[0].name == "灰流丽", attack.zone("hand")
    assert attack.raw["grave_mine"] == "3", attack.raw

    assert parse_question("kind=activate\n") is None, "缺 id 不该猜"
    assert parse_question("id=3\n") is None, "缺 kind 不该猜"
    assert parse_question("随便一段文字") is None


def test_prompt_carries_board_and_stats() -> None:
    """提示词要带上**攻守数值与双方场面**（实测不给数值时它会拿小怪撞大怪）。

    也要带上"判断顺序"和固定输出格式——推理模型被开放问题带跑时会给不出答案。
    """

    class Detail:
        name = "灰流丽"
        effect = "把手牌里的这张卡丢弃才能发动"

    class FakeDb:
        def card_details(self, card_ids):
            return {card_ids[0]: Detail()}

    question = parse_question(ACTIVATE_QUESTION)
    assert question is not None
    prompt = build_prompt(question, FakeDb(), playbook="summon_order=30118811", deck_name="耀圣加速均")
    assert "灰流丽" in prompt and "丢弃" in prompt, prompt
    assert "攻3000/守2500" in prompt, "对手的攻守必须写进提示词：" + prompt
    assert "我的手牌" in prompt and "对方怪兽区" in prompt, prompt
    assert "决定：" in prompt and "理由：" in prompt, "要有固定输出格式：" + prompt
    assert "summon_order=30118811" in prompt, "打法数据要带上（不然它会把展开件省下来）：" + prompt

    attack = parse_question(ATTACK_QUESTION)
    assert attack is not None
    attack_prompt = build_prompt(attack)
    # 候选要把"对方那只怪 + 它的攻守"写清；攻击者也要带攻守
    assert "1＝青眼白龙（表侧攻击，攻3000/守2500" in attack_prompt, attack_prompt
    assert "攻1200/守800" in attack_prompt, attack_prompt
    assert "打不过就别攻击" in attack_prompt, attack_prompt


def test_normalise_answer_is_strict() -> None:
    """答复收敛：只认明确的答复，含糊的一律当没答（猜错比不答更糟）。"""

    question = parse_question(ACTIVATE_QUESTION)
    assert question is not None
    # 新格式是两行（决定 + 理由），理由不进决策
    assert normalise_answer(question, "决定：发动\n理由：这是展开件") == "yes"
    assert normalise_answer(question, "决定：不发动\n理由：留着更值") == "no"
    assert normalise_answer(question, "发动") == "yes"
    assert normalise_answer(question, "yes") == "yes"
    assert normalise_answer(question, "不发动") == "no"
    assert normalise_answer(question, "NO") == "no"
    assert normalise_answer(question, "看情况吧") == "", "含糊的答复不该硬猜"
    assert normalise_answer(question, "") == ""

    attack = parse_question(ATTACK_QUESTION)
    assert attack is not None and attack.kind == KIND_ATTACK_TARGET, attack
    assert normalise_answer(attack, "决定：2\n理由：打黑魔导打得过") == "2"
    assert normalise_answer(attack, "2") == "2"
    assert normalise_answer(attack, "打第 2 只") == "2"
    assert normalise_answer(attack, "决定：0\n理由：打不过") == "0"
    assert normalise_answer(attack, "不攻击") == "0"
    assert normalise_answer(attack, "9") == "", "候选只有两个，9 是瞎答"
    assert normalise_answer(attack, "随便") == ""

    # 固定策略（联调用）：发动一律不发动、攻击一律不攻击
    assert decide_with_rule(question) == "no"
    assert decide_with_rule(attack) == "0"


def test_server_answers_with_matching_id_only() -> None:
    """服务端：答复必须带对 id（执行器会丢掉对不上的），模型答非所问时**不写答复**。"""

    with tempfile.TemporaryDirectory() as directory:
        prefix = Path(directory) / "brain.txt"

        good = BrainServer(prefix, lambda question: "no")
        prefix.with_name(prefix.name + ".q").write_text(ACTIVATE_QUESTION, encoding="utf-8")
        assert asyncio.run(good.serve_once()) is True
        answer = Path(str(prefix) + ".a").read_text(encoding="utf-8")
        assert "id=7" in answer and "answer=no" in answer, answer
        assert good.answered == 1

        # 同一个问题不重复答复（执行器没换问题就不该再写一份）
        assert asyncio.run(good.serve_once()) is False

        # 模型答非所问 → 不写答复（执行器超时后按脚本自己的判断走）
        silent = BrainServer(Path(directory) / "silent.txt", lambda question: "")
        Path(str(silent.answer_path)).unlink(missing_ok=True)
        Path(str(silent.question_path)).write_text(ATTACK_QUESTION, encoding="utf-8")
        assert asyncio.run(silent.serve_once()) is False
        assert not silent.answer_path.exists(), "答不出来就不该写答复"


def test_server_survives_bad_decider() -> None:
    """答复函数抛异常时不能把对局带崩：只是这次不答复。"""

    with tempfile.TemporaryDirectory() as directory:
        prefix = Path(directory) / "brain.txt"

        def explode(question):
            raise RuntimeError("模拟模型炸了")

        server = BrainServer(prefix, explode)
        Path(str(server.question_path)).write_text(ACTIVATE_QUESTION, encoding="utf-8")
        assert asyncio.run(server.serve_once()) is False
        assert not server.answer_path.exists()


def test_needs_model_keeps_only_high_stakes() -> None:
    """``scope=high_stakes`` 只留高压决策：**我的回合**不要把"要不要发动"拿去问模型。

    护栏来源（两轮实测）：第一版按"这张卡像不像阻抗"（知识库 ``is_interaction``）筛，
    只覆盖到 13% 的提问、一点没救回来——因为问得最多的恰恰是本家引擎件，它们因为有速攻类效果
    被标成了"阻抗"（实测 `盈彩月夜之朔` 问 23 次、83% 被否）。真正能分清的是执行器早就写进
    问题里的 ``my_phase``：我的回合问的是本家引擎件（脚本主场）、对手回合问的才是该不该交坑。
    """

    def question(*, kind: str, my_phase: str = "", card_id: int = 0) -> Question:
        """造一个问题（``my_phase`` 空 = 旧版执行器没写这一行）。"""

        raw = {} if my_phase == "" else {"my_phase": my_phase}
        return Question(question_id=1, kind=kind, card_id=card_id, raw=raw)

    def high_stakes(item: Question) -> bool:
        """高压口径下要不要问（可读一点）。"""

        return needs_model(item, None, scope=SCOPE_HIGH_STAKES)[0]

    idle = question(kind=KIND_IDLE, my_phase="1")
    attack = question(kind=KIND_ATTACK_TARGET, my_phase="0")
    my_turn_engine = question(kind=KIND_ACTIVATE, my_phase="1", card_id=100267037)
    their_turn_engine = question(kind=KIND_ACTIVATE, my_phase="0", card_id=100267021)
    no_phase = question(kind=KIND_ACTIVATE, card_id=1001)

    # 默认口径：每一问都问（历史行为不变）
    for item in (idle, attack, my_turn_engine, their_turn_engine, no_phase):
        assert needs_model(item, None, scope=SCOPE_ALL) == (True, "")

    # 高压口径：我的回合不问发动（交回脚本），对手的回合照问
    wanted, why = needs_model(my_turn_engine, None, scope=SCOPE_HIGH_STAKES)
    assert wanted is False and "交回脚本" in why, (wanted, why)
    assert high_stakes(their_turn_engine) is True
    # 「这一步做什么」「打谁」不受范围影响
    assert high_stakes(idle) is True and high_stakes(attack) is True
    # 旧版执行器没写 my_phase：不敢少问
    assert high_stakes(no_phase) is True


def test_is_my_turn_reads_my_phase() -> None:
    """``my_phase`` 的解析：1 = 我的回合、0 = 对手的回合、缺字段 = 不知道（None）。"""

    assert Question(question_id=1, kind=KIND_ACTIVATE, raw={"my_phase": "1"}).is_my_turn is True
    assert Question(question_id=1, kind=KIND_ACTIVATE, raw={"my_phase": "0"}).is_my_turn is False
    assert Question(question_id=1, kind=KIND_ACTIVATE, raw={}).is_my_turn is None
    assert Question(question_id=1, kind=KIND_ACTIVATE, raw={"my_phase": ""}).is_my_turn is None


def test_condense_combo_guide_keeps_only_the_checklist() -> None:
    """单点问题（要不要发动/打谁）只喂"决策清单"那一段；认不出标记就原样返回。

    护栏来源：整份攻略三千多字，原先**每一问都塞进提示词**，而多数问题是"这张手坑现在交不交"
    ——通篇牌组战略对它多半是噪音，还白等模型读完。实测「升辉月」的攻略 3738 字，
    压成清单后 932 字（省 75%），"这一步做什么"仍喂全篇。
    """

    text = (
        "★★★ 决策清单 ★★★\n"
        "1. 先看这个\n"
        "2. 再看这个\n"
        "3. 还有这个\n"
        "4. 最后这个\n"
        "\n■ 详细部分（三千字的流程与自肃）\n…很长很长…"
    )
    short = condense_combo_guide(text)
    assert "决策清单" in short and "详细部分" not in short, short
    # 没有清单标记的纯文本：原样返回（宁可多喂，也不悄悄把资料删掉）
    plain = "没有标记的一篇攻略\n■ 详细部分\n…"
    assert condense_combo_guide(plain) == plain


def test_idle_menu_gets_playbook_priority_tags() -> None:
    """「这一步做什么」的菜单要按打法数据标出"该优先召唤/盖放"。

    护栏来源：按卡号的标注只对"要不要发动/打谁"生效（那类问题里有 ``card_id``），
    而 AI 真正掌舵的菜单里 ``card_id`` 是 0——于是最硬的那条信号从来没在菜单里出现过。
    菜单选项文本带卡名（执行器写的 ``召唤 卡名（攻/守）``），所以按卡名匹配。
    """

    class FakeCardDb:
        """只提供卡名的替身。"""

        NAMES = {100267017: "盈彩月夜之雅 卡蘿爾", 100267026: "盈彩月夜之輝煌"}

        def name(self, card_id: int) -> str:
            return self.NAMES.get(int(card_id), "")

        def describe(self, card_id: int) -> str:
            return self.NAMES.get(int(card_id), str(card_id))

    playbook = "summon_order=100267017\nset_order=100267026\n"
    question = Question(
        question_id=1,
        kind=KIND_IDLE,
        options=["召唤 盈彩月夜之雅 卡蘿爾（攻700/守2000）", "盖放 盈彩月夜之輝煌", "结束主要阶段"],
    )
    prompt = build_prompt(question, FakeCardDb(), deck_name="升辉月", playbook=playbook)
    assert "（打法数据：该优先召唤）" in prompt, prompt
    assert "（打法数据：该优先盖放）" in prompt, prompt
    # 没被点名的选项不加标注
    assert "结束主要阶段（打法数据" not in prompt, prompt


def test_set_order_tags_activate_questions() -> None:
    """``set_order`` 也要能标到"要不要盖放"上（原来只有 activate/search/summon 三个键）。"""

    class FakeCardDb:
        """只提供卡名的替身。"""

        def name(self, card_id: int) -> str:
            return "盈彩月夜之輝煌" if int(card_id) == 100267026 else ""

        def describe(self, card_id: int) -> str:
            return self.name(card_id) or str(card_id)

    question = Question(question_id=1, kind=KIND_ACTIVATE, card_id=100267026)
    prompt = build_prompt(question, FakeCardDb(), playbook="set_order=100267026\n")
    assert "该优先盖放" in prompt, prompt


def test_never_summon_is_tagged_and_ruled_out() -> None:
    """``never_summon``（别拿手坑当怪兽用）要标到菜单上，并在规则里明说不要选。

    护栏来源（用户报"通召手坑、效果乱发"后的实测证据）：决策日志里模型真选过
    「里侧盖放 幽鬼兔」、还在**自己回合发动了增殖的G**（G 是等对手特召时丢的手坑）。
    执行器的菜单本来就会把这些卡列成"召唤/盖放"，所以必须在这里给它打上标注 + 规则说清。
    """

    class FakeCardDb:
        """只提供卡名的替身。"""

        def name(self, card_id: int) -> str:
            return {23434539: "增殖的G", 59438931: "幽鬼兔"}.get(int(card_id), "")

        def describe(self, card_id: int) -> str:
            return self.name(card_id) or str(card_id)

    playbook = "never_summon=23434539,59438931\n"
    question = Question(
        question_id=1,
        kind=KIND_IDLE,
        options=["召唤 幽鬼兔（攻0/守1800）", "发动 增殖的G（攻500/守200）", "结束主要阶段"],
    )
    prompt = build_prompt(question, FakeCardDb(), playbook=playbook)
    assert "别拿它当怪兽用" in prompt, prompt
    # 规则里也要明说（标注只是提示，规则才是"不要选"）
    assert "手坑不要当怪兽用" in prompt, prompt
    assert "手坑也别在自己回合" in prompt, prompt
    assert "会炸到自己场面的效果" in prompt, prompt
    # 按卡号的定位标注同样认这个键（发动/盖放类问题）
    activate = Question(question_id=2, kind=KIND_ACTIVATE, card_id=59438931)
    activate_prompt = build_prompt(activate, FakeCardDb(), playbook=playbook)
    assert "别拿它当怪兽用" in activate_prompt, activate_prompt


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
