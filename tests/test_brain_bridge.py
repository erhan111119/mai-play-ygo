"""阻抗决策层答复端的单元测试。

直接用 ``python tests/test_brain_bridge.py`` 运行，也可以用 pytest 收集
（用 ``asyncio.run`` 包住异步用例，所以不依赖 pytest-asyncio）。

这里覆盖的是**会静默坏掉**的那几条约定（见 `duel/brain_bridge.py` 文件头）：
答复要带对方的 id、答复要原子写入、答不上来就什么都别写；另外加一条实测踩过的坑——
WindBot 的 id 新一局会从 1 重来，只按 id 判重会把新一局的第 1 问漏掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

import asyncio
import contextlib
import sys
import tempfile
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.brain_bridge import BrainBridge, parse_question  # noqa: E402  导入顺序受 sys.path 补丁影响


# 一份"对面场上有两只怪、我方准备交效果遮蒙者"的问题（字段与 C# 的 BuildDisableQuestion 对齐）
DISABLE_QUESTION = """id={qid}
kind=disable_target
source=97268402;效果遮蒙者
turn=3
my_phase=0
phase=8
my_lp=6000
opp_lp=8000
my_damage=0
mine=（空）
my_spell=（空）
their_spell=83764718;无限泡影;0;0
chain=89631139;青眼白龙;1
option=1;78010363;黑森林的魔女;1100;1500;10000
option=2;86066372;访问码语者;3000;2500;53000
"""

GATE_QUESTION = """id={qid}
kind=negate_gate
source=14558127;灰流丽
turn=4
my_phase=0
phase=8
my_lp=6000
opp_lp=8000
chain=23434538;增殖的G;1
"""

#: 闸门问题的**真实形状**：C# 侧的 `Ask` 走原有的 `AppendContext`，区域行是**五段**
#: （`卡号;卡名;攻;守;表示形式`），还多出 hand/grave/banish/their_seen 这些键。
#: 2026-10-08 真机自测就是栽在这里：bridge 按四段解包 → ValueError → 问题文件写出来了、
#: 模型一次都没被问到（`asked` 不为 0 而 `answered` 为 0，光看计数看不出是解析炸了）。
GATE_QUESTION_FULL = """id={qid}
kind=negate_gate
card=14558127
card_name=灰流丽
turn=4
my_lp=6000
opp_lp=8000
hand=97268402;效果遮蒙者;0;0;2
mine=89631139;青眼白龙;3000;2500;1
my_spell=（空）
theirs=78010363;黑森林的魔女;1100;1500;1
their_spell=10045474;无限泡影;0;0;4
grave_mine=3
grave_theirs=5
banish_mine=0
banish_theirs=1
chain_depth=1
chain=23434538;增殖的G;1
their_seen=23434538;增殖的G
my_phase=0
phase=8
"""


class FakeCardDatabase:
    """只回答卡文的假卡库（真卡库要 cards.cdb，单测不该依赖它）。"""

    def __init__(self, texts: Optional[Dict[int, str]] = None) -> None:
        self._texts = texts or {
            78010363: ("黑森林的魔女", "效果怪兽 攻1100/守1500", "这张卡从场上送去墓地的场合发动。从卡组把 1 只守备力 1500 以下的怪兽加入手卡"),
            86066372: ("访问码语者", "怪兽 连接", "这张卡连接召唤成功的场合，以对方场上 1 张卡为对象才能发动。那张卡破坏"),
            97268402: ("效果遮蒙者", "怪兽 调整", "以对方场上 1 只效果怪兽为对象才能发动。那只怪兽的效果直到回合结束时无效"),
            14558127: ("灰流丽", "怪兽 调整", "包含从卡组把卡加入手卡的效果发动时才能发动。那个发动无效并除外"),
            23434538: ("增殖的G", "怪兽 效果", "对手每次特殊召唤怪兽，自己抽 1 张卡"),
        }

    @property
    def available(self) -> bool:
        return True

    def card_details(self, card_ids: Sequence[int]):
        from duel.cards import CardDetail

        details = {}
        for card_id in card_ids:
            if card_id not in self._texts:
                continue
            name, type_text, effect = self._texts[card_id]
            stats = "攻1100/守1500" if "攻" in type_text else ""
            details[card_id] = CardDetail(
                card_id=card_id,
                name=name,
                type_text=type_text.split()[0],
                stats=stats,
                effect=effect,
            )
        return details


def _write_question(q_path: Path, template: str, qid: int) -> None:
    """把一份问题写进 `q_path`（调用方传的已经是完整的 `.q` 路径）。"""

    q_path.write_text(template.format(qid=qid), encoding="utf-8")


async def _run_bridge_once(bridge: BrainBridge, answer_path: Path, *, wait: float = 2.0) -> Optional[str]:
    """起一次真实轮询循环，等到答复文件出现（或超时）后收摊。"""

    task = asyncio.create_task(bridge.run())
    try:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if answer_path.exists():
                return answer_path.read_text(encoding="utf-8")
            await asyncio.sleep(0.01)
        return None
    finally:
        bridge.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _make_bridge(tmp: str, generate, **kwargs) -> tuple[BrainBridge, Path, Path]:
    prefix = Path(tmp) / "room_test"
    bridge = BrainBridge(
        prefix=prefix,
        generate=generate,
        card_db=FakeCardDatabase(),  # type: ignore[arg-type]
        timeout=kwargs.pop("timeout", 2.0),
        poll_interval=0.01,
        **kwargs,
    )
    return bridge, Path(f"{prefix}.q"), Path(f"{prefix}.a")


# --------------------------------------------------------------------------- 解析


def test_parse_question_requires_id_and_kind() -> None:
    """缺 id 或缺 kind 一律视为"还没写完"，不能当成一个问题受理。"""

    assert parse_question("") is None
    assert parse_question("kind=disable_target\noption=1;1;卡;0;0;0\n") is None
    assert parse_question("id=3\noption=1;1;卡;0;0;0\n") is None
    assert parse_question("id=abc\nkind=disable_target\n") is None


def test_parse_question_keeps_repeated_keys_as_lines() -> None:
    """`option=`/`chain=` 会重复出现，必须按行收好，不能互相覆盖。"""

    question = parse_question(DISABLE_QUESTION.format(qid=7))
    assert question is not None
    assert question["id"] == "7"
    assert question["kind"] == "disable_target"
    assert question["source"] == "97268402;效果遮蒙者"
    assert len(question["option"].splitlines()) == 2
    assert question["chain"].startswith("89631139;")


def test_parse_question_tolerates_half_written_tail() -> None:
    """半写状态（最后一行被截断）也要能解析——重试机制靠这个继续等下一行。"""

    text = "id=1\nkind=disable_target\noption=1;1;卡;0;0;0\nopt"
    question = parse_question(text)
    assert question is not None
    assert question["id"] == "1"


# --------------------------------------------------------------------------- 答复语法


def test_gate_answer_accepts_yes_and_no_with_reason() -> None:
    """yes / no;理由 两种都收；理由是给日志看的，长度要收紧。"""

    question = {"id": "1", "kind": "negate_gate"}
    assert BrainBridge._normalize_answer("negate_gate", "yes", question) == "yes"
    assert BrainBridge._normalize_answer("negate_gate", "  YES  ", question) == "yes"
    assert BrainBridge._normalize_answer("negate_gate", "no;对手不检索", question) == "no;对手不检索"
    # 没写理由也是合法答复（只是日志里会标明"模型没给理由"）
    assert BrainBridge._normalize_answer("negate_gate", "no", question) == "no"


def test_gate_answer_rejects_freeform_text() -> None:
    """自由文本一律拒收——这个回复最终会变成 WindBot 的动作，不能放没校验的输出过去。"""

    question = {"id": "1", "kind": "negate_gate"}
    assert BrainBridge._normalize_answer("negate_gate", "我觉得可以交", question) is None
    assert BrainBridge._normalize_answer("negate_gate", "", question) is None


def test_target_answer_must_be_an_index_inside_the_candidates() -> None:
    """目标答复只接受候选范围内的序号；越界/没数字都不算答复。"""

    question = {"id": "1", "kind": "disable_target", "option": "1;a;卡;0;0;0\n2;b;卡;0;0;0"}
    assert BrainBridge._normalize_answer("disable_target", "2", question) == "2"
    # 模型爱加解释：取第一个数字段即可，后面的话丢掉
    assert BrainBridge._normalize_answer("disable_target", "2（访问码语者）", question) == "2"
    assert BrainBridge._normalize_answer("disable_target", "3", question) is None
    assert BrainBridge._normalize_answer("disable_target", "0", question) is None
    assert BrainBridge._normalize_answer("disable_target", "选第一只", question) is None


# --------------------------------------------------------------------------- 问答往返


def test_bridge_answers_disable_target_with_matching_id() -> None:
    """正常一问一答：答复要带上对方的 id，并且是模型给的那个序号。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                seen.append(prompt)
                return "2"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, DISABLE_QUESTION, 11)
            body = await _run_bridge_once(bridge, a_path)

            assert body is not None, "应当写出答复"
            assert "id=11" in body
            assert "answer=2" in body
            assert bridge.stats.answered == 1
            assert bridge.stats.asked == 1
            # 提示词里要有卡文（这是决策层相对出牌脚本唯一的信息优势）与全部的候选
            assert seen and "从卡组把 1 只守备力 1500 以下的怪兽加入手卡" in seen[0]
            assert "访问码语者" in seen[0]
            assert "效果遮蒙者" in seen[0]
            # 临时文件不能留在原地（C# 只认 `.a`，留个 `.tmp` 会让人以为写坏了）
            assert not Path(f"{a_path}.tmp").exists()

    asyncio.run(scenario())


def test_bridge_writes_nothing_when_model_is_too_slow() -> None:
    """等模型超时就什么都别写：WindBot 会按出牌脚本继续，塞个瞎猜的答复更坏。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:

            async def generate(prompt: str) -> Optional[str]:
                del prompt
                await asyncio.sleep(1.0)
                return "2"

            bridge, q_path, a_path = _make_bridge(tmp, generate, timeout=0.2)
            _write_question(q_path, DISABLE_QUESTION, 12)
            body = await _run_bridge_once(bridge, a_path, wait=0.6)

            assert body is None
            assert bridge.stats.timed_out == 1
            assert bridge.stats.answered == 0

    asyncio.run(scenario())


def test_bridge_writes_nothing_when_model_raises_or_returns_garbage() -> None:
    """模型抛异常、返回空、返回不合语法的答复——三种都不许写答复文件。"""

    async def scenario() -> None:
        cases = ("raise", "empty", "garbage")
        for case in cases:
            with tempfile.TemporaryDirectory() as tmp:

                async def generate(prompt: str, case: str = case) -> Optional[str]:
                    del prompt
                    if case == "raise":
                        raise RuntimeError("网关挂了")
                    if case == "empty":
                        return ""
                    return "我选访问码语者"

                bridge, q_path, a_path = _make_bridge(tmp, generate)
                _write_question(q_path, DISABLE_QUESTION, 13)
                body = await _run_bridge_once(bridge, a_path)

                assert body is None, case
                assert bridge.stats.answered == 0, case
                assert bridge.stats.failed == 1, case

    asyncio.run(scenario())


def test_bridge_ignores_unsupported_question_kinds() -> None:
    """不支持的问题类型不答复，但要留一条日志线索（否则"开了没反应"没法排查）。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                calls.append(prompt)
                return "1"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, "id={qid}\nkind=idle_action\n", 14)
            body = await _run_bridge_once(bridge, a_path, wait=0.3)

            assert body is None
            assert calls == []
            assert bridge.stats.unsupported == 1

    asyncio.run(scenario())


def test_bridge_reuses_cache_for_identical_question() -> None:
    """同一个局面反复问同一件事时直接复用答复：省一次网络往返就是省一段等待。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                calls.append(prompt)
                return "1"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, DISABLE_QUESTION, 20)
            assert await _run_bridge_once(bridge, a_path) is not None

            # 模拟 C# 读完就删掉答复文件，然后同一局面又来一问（只有 id 不同）
            a_path.unlink()
            _write_question(q_path, DISABLE_QUESTION, 21)
            body = await _run_bridge_once(bridge, a_path)

            assert body is not None and "id=21" in body
            assert len(calls) == 1, "第二次应当命中缓存，不再调模型"
            assert bridge.stats.cached == 1
            assert bridge.stats.answered == 2

    asyncio.run(scenario())


def test_bridge_handles_new_duel_restarting_ids_at_one() -> None:
    """⚠ 实测踩过的坑：WindBot 的 id 是新一局从 1 重来的，只按 id 判重会漏掉新一局的第 1 问。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:

            async def generate(prompt: str) -> Optional[str]:
                # 闸门问题只要 yes/no，目标问题只要序号——按问题里那句话分派
                return "yes" if "只回复 yes" in prompt else "1"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, DISABLE_QUESTION, 1)
            assert await _run_bridge_once(bridge, a_path) is not None
            assert bridge.stats.answered == 1

            # 新一局：id 又回到 1，内容也换了一份（常驻房打完一局会再开一副、进程重启）
            a_path.unlink()
            _write_question(q_path, GATE_QUESTION, 1)
            body = await _run_bridge_once(bridge, a_path)

            assert body is not None, "新一局的第 1 问不能被当成「已经答过」而漏掉"
            assert "id=1" in body
            assert bridge.stats.answered == 2
            assert bridge.stats.by_kind.get("negate_gate") == 1

    asyncio.run(scenario())


def test_bridge_does_not_answer_twice_for_one_question() -> None:
    """轮询很快，同一个问题不能被反复回答（否则会在对局里刷出好几条答复）。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                calls.append(prompt)
                return "1"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, DISABLE_QUESTION, 30)
            assert await _run_bridge_once(bridge, a_path) is not None

            # 答复文件还在（C# 还没来得及读），再跑一会儿循环，不该再答一次
            task = asyncio.create_task(bridge.run())
            await asyncio.sleep(0.08)
            bridge.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            assert len(calls) == 1
            assert bridge.stats.answered == 1

    asyncio.run(scenario())


#: 链上选择问题（"该发谁的效果"）的真实形状：C# 的 `BuildChainQuestion`。
#: ⚠ `option=` 行首那个数字是**内核候选下标 + 1**，不保证从 1 连续（这里就是 2 和 4），
#: 所以答复校验必须按"行首数字的集合"来判，不能拿候选个数当上限。
CHAIN_QUESTION = """id={qid}
kind=chain_choice
turn=3
my_phase=1
phase=256
my_lp=7000
opp_lp=8000
chain=14558127;灰流丽;1
mine=89631139;青眼白龙;3000;2500;1
my_spell=（空）
theirs=78010363;黑森林的魔女;1100;1500;1
their_spell=（空）
option=2;97268402;效果遮蒙者;0;0;desc=-1
option=4;10045474;无限泡影;0;0;desc=-1
"""


def test_chain_answer_accepts_option_number_or_no() -> None:
    """链上选择：序号必须**正是候选行首那个数字**（内核下标+1，不连续），no 表示都不发。"""

    question = {
        "id": "1",
        "kind": "chain_choice",
        "option": "2;a;卡;0;0;desc=-1\n4;b;卡;0;0;desc=-1",
    }
    assert BrainBridge._normalize_answer("chain_choice", "2", question) == "2"
    assert BrainBridge._normalize_answer("chain_choice", "4;对手在检索", question) == "4;对手在检索"
    assert BrainBridge._normalize_answer("chain_choice", "no;只是铺场", question) == "no;只是铺场"
    # 1、3 都不在候选里（内核下标+1 不连续）→ 不能当合法答复
    assert BrainBridge._normalize_answer("chain_choice", "1", question) is None
    assert BrainBridge._normalize_answer("chain_choice", "3", question) is None
    assert BrainBridge._normalize_answer("chain_choice", "灰流丽", question) is None


def test_bridge_answers_chain_choice_and_includes_card_text() -> None:
    """链上选择这一问要能答上，而且提示词里要有"对面刚发动了什么"与各候选的卡文。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                seen.append(prompt)
                return "4;对手在检索，用泡影"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, CHAIN_QUESTION, 60)
            body = await _run_bridge_once(bridge, a_path)

            assert body is not None
            assert "id=60" in body
            assert "answer=4;对手在检索，用泡影" in body
            assert bridge.stats.answered == 1
            assert bridge.stats.by_kind.get("chain_choice") == 1
            prompt = seen[0]
            # 对面刚发动的是什么、候选各是谁、卡文都在
            assert "灰流丽" in prompt
            assert "效果遮蒙者" in prompt and "无限泡影" in prompt
            # 链上那张（灰流丽）的卡文必须在——判断"这张能不能拦得住"全靠它
            assert "包含从卡组把卡加入手卡的效果发动时才能发动" in prompt
            assert "只回复一个序号" in prompt


    asyncio.run(scenario())


def test_bridge_handles_five_field_zone_rows_from_original_context() -> None:
    """闸门问题走的是 C# 原有的 `AppendContext`（区域行五段），bridge 必须照样能答。

    这条是**真机自测踩出来的回归**：bridge 原来按四段元组解包，多一个"表示形式"就抛
    ValueError，而异常发生在组装提示词阶段——现场表现是"问题文件写出来了、模型一次都没被问到"，
    光看 `asked/answered` 计数很难判断是解析炸了。
    """

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                seen.append(prompt)
                return "no;对面只是抽牌"

            bridge, q_path, a_path = _make_bridge(tmp, generate)
            _write_question(q_path, GATE_QUESTION_FULL, 50)
            body = await _run_bridge_once(bridge, a_path)

            assert body is not None, "五段格式的问题也必须能答上"
            assert "id=50" in body
            assert "answer=no;对面只是抽牌" in body
            assert bridge.stats.answered == 1
            assert bridge.stats.failed == 0, "不该有解析异常"
            # 局面摘要要把五段行里的卡名与攻守挑出来，多余的"表示形式"要忽略
            assert seen and "黑森林的魔女" in seen[0] and "攻1100/守1500" in seen[0]
            # 对手已露过的卡也要进提示词（判断"这张坑现在交值不值"要用它）
            assert "对手已露过的卡" in seen[0]

    asyncio.run(scenario())


def test_bridge_prompt_degrades_without_card_database() -> None:
    """卡库不可用时仍然要能问（只是少了卡文），不能因为读不出卡文就整条链路停掉。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: List[str] = []

            async def generate(prompt: str) -> Optional[str]:
                seen.append(prompt)
                return "1"

            prefix = Path(tmp) / "room_nocdb"
            bridge = BrainBridge(
                prefix=prefix,
                generate=generate,
                card_db=None,
                timeout=2.0,
                poll_interval=0.01,
            )
            q_path = Path(f"{prefix}.q")
            a_path = Path(f"{prefix}.a")
            _write_question(q_path, DISABLE_QUESTION, 40)
            body = await _run_bridge_once(bridge, a_path)

            assert body is not None and "answer=1" in body
            # 没有卡文时提示词里仍然要有候选卡名，模型至少能按名字判断
            assert seen and "黑森林的魔女" in seen[0]
            assert "从卡组把 1 只守备力 1500 以下的怪兽加入手卡" not in seen[0]

    asyncio.run(scenario())


def test_decision_style_changes_policy_text_and_accepts_aliases() -> None:
    """档位（保守/平常/激进）**只换"判断口径"那段**，而且中英别名都认、写错回落到 normal。

    这条护的是用户口径：决策层的"幅度"要能按牌组调（同一份保守口径在合成牌组 +13 点、
    在真实俱舍 -5.6/-9.4 点），所以档位必须真的改变提示词，而不是只是配置里好看。
    """

    from duel.brain_bridge import BrainBridge, normalize_decision_style

    assert normalize_decision_style("保守") == "conservative"
    assert normalize_decision_style("激进") == "aggressive"
    assert normalize_decision_style("AGGRESSIVE") == "aggressive"
    assert normalize_decision_style("") == "normal"
    assert normalize_decision_style("乱写的") == "normal"  # 认不出来回落，不报错

    question = parse_question(CHAIN_QUESTION.format(qid=1))
    assert question is not None
    texts = {}
    for style in ("conservative", "normal", "aggressive"):
        bridge = BrainBridge(
            prefix=Path("x"), generate=None, card_db=FakeCardDatabase(), style=style
        )  # type: ignore[arg-type]
        assert bridge.style == style
        prompt = bridge._build_chain_prompt(question) or ""
        texts[style] = prompt.split("判断口径：")[1].split("⚠ 只按我给的卡文")[0]
    # 三档必须互不相同，而且激进档要明确写"能拦就拦"、保守档要写"有疑问就不交"
    assert len(set(texts.values())) == 3
    assert "能拦就拦" in texts["aggressive"]
    assert "有疑问就不交" in texts["conservative"]
    assert "有疑问就不交" not in texts["aggressive"]


def test_chain_answer_tolerates_how_the_model_writes_reasons() -> None:
    """**理由怎么写都要认**：``4;理由`` / ``4 理由：…`` / 换行加理由 / ``都不发：…`` 全都收。

    这是实测踩出来的：原来只按分号切第一段，换一档提示词后模型改成写"序号 + 换行 + 理由："，
    于是 88/566 = 15.6% 的答复被整条丢弃（那一腿的测量因此不干净）。
    答复格式会随提示词措辞变，解析器不能假设分隔符。
    """

    question = {
        "id": "1",
        "kind": "chain_choice",
        "option": "1;a;卡;0;0;desc=-1\n4;b;卡;0;0;desc=-1",
    }
    cases = {
        "4;对手在检索": "4;对手在检索",
        "4 理由：对手在检索": "4;理由：对手在检索",
        "4\n\n理由：对手在检索": "4;理由：对手在检索",
        "4。": "4",
        "no;只是铺场": "no;只是铺场",
        "no\n理由：只是铺场": "no;理由：只是铺场",
        "都不发：只是铺场": "no;只是铺场",
        "不发": "no",
    }
    for raw, expected in cases.items():
        got = BrainBridge._normalize_answer("chain_choice", raw, question)
        assert got == expected, f"{raw!r} → {got!r}（期望 {expected!r}）"
    # 候选里没有的序号仍然要拒（内核下标+1，不保证从 1 连续）
    assert BrainBridge._normalize_answer("chain_choice", "2", question) is None


def main() -> int:
    """无 pytest 环境下逐个执行测试函数。"""

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
