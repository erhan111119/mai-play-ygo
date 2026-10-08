"""配置写回与训练新能力（写脚本 / 自动迭代）的测试。

三块：

1. **`configedit`**：面板改配置必须"只碰那一行、保住注释、校验不过不落盘"——
   写坏配置文件等于把用户的环境弄挂，这几条一条都不能松。
2. **卡组操作与对局监控接口**：改随机池 / 删卡组要走插件的入口（真机上是事件循环），
   删除不可逆所以必须显式确认；`/api/rooms` 要能把牌桌摊平（里侧不泄卡号）。
3. **写脚本**：生成器用假的模型回调 + 假的源码树跑通"生成 → 编译失败回喂 → 成功"，
   以及"编译始终失败"时不把坏文件留在源码树里（留了会毒死之后每一轮编译）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import importlib
import json
import sys
import tempfile
import types

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
_HOST_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _HOST_ROOT.parent.parent
for _extra in (
    _HOST_ROOT,
    _INSTALL_ROOT / "python-overrides",
    _INSTALL_ROOT / "python-env" / "Lib" / "site-packages",
):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

_PACKAGE = "mai_play_ygo_config_tests"


def _load(module_name: str):
    """按宿主的方式导入（插件是按包加载的，相对导入要靠这一层）。"""

    package = sys.modules.get(_PACKAGE)
    if package is None:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [str(_PLUGIN_ROOT)]  # type: ignore[attr-defined]
        sys.modules[_PACKAGE] = package
    return importlib.import_module(f"{_PACKAGE}.{module_name}")


def _config_model() -> Any:
    """插件配置模型（校验与渲染都拿它当真相）。"""

    return _load("plugin").MaiPlayYgoConfig()


# ---------------------------------------------------------------------------
# configedit
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = """\
# 麦麦玩游戏王 · 本机配置
#
# 这份的注释必须留着——它是给部署者看的说明书。

[plugin]
enabled = true  # 是否启用插件
config_version = "1.2.0"  # 配置版本

[duel]
bot_name = "憨憨"  # 对局中显示的名字
taunt_lines = []  # 挑衅台词池（留空用内置 20 句）

[llm]
# 模型：只写模型名
summary_model = ""  # 对局总结用哪只
decision_model = "deepseek-chat"  # 决策层用哪只——"#"号在引号里不算注释
"""


def test_config_edit_replaces_only_touched_lines() -> None:
    """改一项就只动那一行：注释、空行、其它键、节顺序全部原样。"""

    configedit = _load("configedit")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        model = _config_model()
        changed = configedit.apply_values(
            path, {"duel": {"bot_name": "小憨", "taunt_lines": ["别急", "我看着呢"]}}, model
        )
        text = path.read_text(encoding="utf-8")
        assert 'bot_name = "小憨"  # 对局中显示的名字' in text, text
        assert 'taunt_lines = ["别急", "我看着呢"]  # 挑衅台词池（留空用内置 20 句）' in text, text
        assert text.count("# 这份的注释必须留着") == 1, "注释被吃掉了"
        assert 'decision_model = "deepseek-chat"' in text, "没碰的键不该被改写"
        assert sorted(changed) == [
            'duel.bot_name="小憨"',
            'duel.taunt_lines=["别急", "我看着呢"]',
        ], changed


def test_config_edit_rejects_bad_values_without_touching_the_file() -> None:
    """校验不过就**一个字都不写**：写一半比不写更糟（用户看到的是"有些改了有些没改"）。"""

    configedit = _load("configedit")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        model = _config_model()
        before = path.read_text(encoding="utf-8")
        for payload in (
            {"duel": {"bot_name": "x", "brain_timeout_ms": "不是数字"}},
            {"nope": {"bot_name": "x"}},
            {"duel": {"no_such_key": 1}},
            {"duel": {"bot_name": "带\n换行"}},
        ):
            try:
                configedit.apply_values(path, payload, model)
            except configedit.ConfigEditError:
                pass
            else:
                raise AssertionError(f"这批改动不该通过：{payload}")
        assert path.read_text(encoding="utf-8") == before, "校验失败时文件被动了"


def test_config_edit_inserts_keys_that_are_not_written_yet() -> None:
    """文件里没有的键要插进对应的节（而不是新建一个重名的节，那会让 TOML 直接非法）。"""

    configedit = _load("configedit")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        model = _config_model()
        configedit.apply_values(path, {"training": {"enabled": True, "max_duels_per_run": 40}}, model)
        text = path.read_text(encoding="utf-8")
        assert "[training]" in text and "max_duels_per_run = 40" in text, text
        import tomllib

        parsed = tomllib.loads(text)
        assert parsed["training"]["max_duels_per_run"] == 40, parsed["training"]
        assert parsed["duel"]["bot_name"] == "憨憨", "原有内容被破坏"
        assert text.count("[duel]") == 1 and text.count("[llm]") == 1, text


def test_config_edit_reports_missing_keys() -> None:
    """没写进文件的项要能列出来（面板标"默认值"用）。"""

    configedit = _load("configedit")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        model = _config_model()
        missing = configedit.missing_keys(path, ["duel", "training"], model)
        assert "bot_name" not in missing["duel"], missing["duel"]
        assert "listen_port" in missing["duel"], missing["duel"]
        assert "enabled" in missing["training"], missing["training"]


# ---------------------------------------------------------------------------
# 卡组操作 / 对局监控接口
# ---------------------------------------------------------------------------


class StubPlugin:
    """面板要的插件接口替身（卡组操作记下来，对局数据造一份）。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._rooms: Dict[str, Any] = {}
        self._cards_cdb_path = ""
        self.config = _config_model()
        self.ctx = type("Ctx", (), {"paths": type("P", (), {"data_dir": str(data_dir)})()})()
        self.actions: List[Dict[str, Any]] = []
        self.raise_on_action = ""

    def training_store(self) -> Any:
        return None

    def training_runner(self) -> Any:
        return None

    def _training_workspace(self) -> Path:
        return self._data_dir / "train"

    def schedule_deck_action(self, action: str, deck_id: int, **kwargs: Any) -> str:
        self.actions.append({"action": action, "deck_id": deck_id, **kwargs})
        if self.raise_on_action:
            raise RuntimeError(self.raise_on_action)
        return f"替身：{action} #{deck_id}"


def _start_panel(webui: Any, plugin: Any, key: str):
    server = webui.WebUIServer(plugin, host="127.0.0.1", port=0, api_key=key, key_source="t", logger=None)
    assert server.start()
    return server, server.bound_port


def test_deck_operations_go_through_the_plugin_and_need_confirmation() -> None:
    """卡组操作必须走插件入口（真机上是事件循环），删除要显式确认，失败原因原样回给用户。"""

    webui = _load("webui")
    import http.client

    def call(port: int, method: str, path: str, key: str, payload: Any = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"X-API-Key": key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    secret = "d" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        server, port = _start_panel(webui, plugin, secret)
        try:
            status, body = call(port, "POST", "/api/deck/12/random", secret, {"in_random": True, "group_id": "111"})
            assert status == 200 and body["ok"] is True, body
            assert plugin.actions[-1] == {
                "action": "random", "deck_id": 12, "in_random": True, "group_id": "111"
            }, plugin.actions

            # 删除没带 confirm：不执行任何动作
            status, body = call(port, "POST", "/api/deck/12/delete", secret, {})
            assert body["ok"] is False and "confirm" in body["error"], body
            assert len(plugin.actions) == 1, plugin.actions

            status, body = call(port, "POST", "/api/deck/12/delete", secret, {"confirm": True})
            assert body["ok"] is True, body
            assert plugin.actions[-1]["action"] == "delete"

            # 卡号不是数字：直接拒掉，不拼进任何东西
            status, body = call(port, "POST", "/api/deck/abc/delete", secret, {"confirm": True})
            assert body["ok"] is False, body

            # 插件那层报错（内置卡组不能删）要原样显示给用户
            plugin.raise_on_action = "内置卡组不能删除，只能把它移出随机池"
            status, body = call(port, "POST", "/api/deck/9/delete", secret, {"confirm": True})
            assert body["ok"] is False and "内置卡组" in body["error"], body
        finally:
            server.stop_now()


def test_rooms_endpoint_lays_out_the_board_without_leaking_face_down_cards() -> None:
    """对局监控接口：双方路区/魔陷区按 1~5 号位铺开，里侧的卡只报"有卡"不报卡号。"""

    webui = _load("webui")
    fieldstate = _load("duel.fieldstate")
    import http.client

    class Player:
        def __init__(self, lp: int, grave: int) -> None:
            self.lp = lp
            self.grave = grave
            self.banished = 0
            self.extra = 2

    # 卡号用正数表示表侧、负数表示里侧（`_zone_card` 就是这么判的：
    # 记录器把里侧的卡存成负卡号，避免面板/出图泄露对手的盖牌）
    state = types.SimpleNamespace(
        zones={
            (0, fieldstate.MONSTER_ZONE, 1): 100,
            (1, fieldstate.MONSTER_ZONE, 3): -200,
            (0, fieldstate.SPELL_ZONES[0], 2): 300,
        },
        players={0: Player(6800, 4), 1: Player(7200, 1)},
    )

    class Recorder:
        self_seat = 0
        turn_count = 3
        phase = "主要阶段1"
        field_state = state
        players = {0: types.SimpleNamespace(name="憨憨"), 1: types.SimpleNamespace(name="群友")}

    class Session:
        started = True
        finished = False

        def recorder(self) -> Any:
            return Recorder()

    secret = "r" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        plugin._rooms = {
            "qq:group:1": types.SimpleNamespace(
                group_id="1", deck_name="升辉月", session=Session()
            )
        }
        server, port = _start_panel(webui, plugin, secret)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/api/rooms", headers={"X-API-Key": secret})
            payload = json.loads(connection.getresponse().read().decode("utf-8"))
            connection.close()
            assert payload["ok"] is True, payload
            room = payload["rooms"][0]
            assert room["turn"] == 3 and room["phase"] == "主要阶段1", room
            ours = [side for side in room["sides"] if side["is_self"]][0]
            theirs = [side for side in room["sides"] if not side["is_self"]][0]
            assert ours["lp"] == 6800 and theirs["lp"] == 7200, room["sides"]
            assert len(ours["monsters"]) == 5 and len(ours["spells"]) == 5
            assert ours["monsters"][0] == {"id": 100, "face_up": True, "art": 100}, ours["monsters"][0]
            assert ours["monsters"][1] is None, "空格要显式给 null，前端才画得出空场"
            # 里侧：只报"有卡"，卡号取绝对值会泄露对手盖的是什么——所以只给 face_up=False
            back = theirs["monsters"][2]
            assert back is not None and back["face_up"] is False, back
            assert ours["graveyard"] == 4 and theirs["graveyard"] == 1, room["sides"]
        finally:
            server.stop_now()


# ---------------------------------------------------------------------------
# 写脚本
# ---------------------------------------------------------------------------

#: 假模型给的脚本。**用占位符替换而不是 str.format**：C# 代码里全是花括号，
#: format 会把它们当占位符炸掉（这条在提示词那边也踩过一次）。
FAKE_CODE = """```csharp
using YGOSharp.OCGWrapper.Enums;
using WindBot.Game.AI;

namespace WindBot.Game.AI.Decks
{
    [Deck("__STYLE__", "AI___STYLE__", "Normal")]
    public class __CLS__ : DefaultExecutor
    {
        public __CLS__(GameAI ai, Duel duel) : base(ai, duel)
        {
            AddExecutor(ExecutorType.SummonOrSet);
            AddExecutor(ExecutorType.Repair);
        }
    }
}
```"""


def fake_script(style: str, cls: str) -> str:
    """把占位符换成真名字（避开 str.format 与 C# 花括号的冲突）。"""

    return FAKE_CODE.replace("__CLS__", cls).replace("__STYLE__", style)


def _make_tree(root: Path) -> tuple:
    """造一份最小的"WindBot 源码树 + 运行目录"。"""

    source = root / "windbot-src"
    (source / "Game" / "AI" / "Decks").mkdir(parents=True, exist_ok=True)
    (source / "WindBot.csproj").write_text("<Project />", encoding="utf-8")
    windbot = root / "windbot"
    (windbot / "Decks").mkdir(parents=True, exist_ok=True)
    return source, windbot


def test_script_generator_writes_compiles_and_reports_attempts() -> None:
    """生成 → 编译成功：文件写进源码树、卡表写进 Decks/、轮数如实记下来。"""

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return fake_script("Gen42", "Gen42Executor")

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen42", max_attempts=3
        )

        async def fake_build() -> tuple:
            return True, "Build succeeded"

        generator._build = fake_build  # type: ignore[method-assign]
        cards = [
            scriptgen.CardInfo(card_id=100, name="测试怪兽", zone="主卡组", effect="抽 1 张。"),
            scriptgen.CardInfo(card_id=500, name="测试额外", zone="额外卡组"),
        ]
        request = scriptgen.DeckScriptRequest(
            deck_id=42, deck_name="测试牌", cards=cards, combo_guide="先通召 100，再做 500。",
            extra_prompt="先手优先做阻抗", feedback="上一轮太爱盖牌",
        )
        result = asyncio.run(generator.generate(request))

        assert result.style_name == "Gen42" and result.attempts == 1, result
        assert result.file_path.is_file(), result.file_path
        assert "class Gen42Executor" in result.file_path.read_text(encoding="utf-8")
        assert (windbot / "Decks" / "AI_Gen42.ydk").is_file(), "卡表也要写一份（脚本自身要自洽）"
        # 提示词里该有的东西：卡文、combo、作者要求、上一轮反馈、约定的名字
        prompt = prompts[0]
        for needle in ("测试怪兽", "抽 1 张", "先通召 100", "先手优先做阻抗", "上一轮太爱盖牌", "Gen42Executor"):
            assert needle in prompt, f"提示词里缺少 {needle}"


def test_script_generator_feeds_build_errors_back_and_cleans_up_failures() -> None:
    """编译失败要把编译器输出回喂重写；几轮都不成时**不能把坏文件留在源码树里**。

    留着的后果很具体：csproj 会把 `Game/AI/Decks` 下所有 .cs 都编进去，
    这份失败的尝试就成了同一个类的第二份定义，之后每轮编译都报 CS0101/CS0579
    （实测把三轮重试全毒死过）。
    """

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return fake_script("Gen7", "Gen7Executor")

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen7", max_attempts=2
        )
        builds = {"n": 0}

        async def failing_build() -> tuple:
            builds["n"] += 1
            return False, "Decks/Gen7Executor.cs(9,26): error CS0117: 没有这个成员"

        generator._build = failing_build  # type: ignore[method-assign]
        request = scriptgen.DeckScriptRequest(
            deck_id=7, deck_name="测试牌", cards=[scriptgen.CardInfo(card_id=1, name="卡")]
        )
        try:
            asyncio.run(generator.generate(request))
        except scriptgen.ScriptGenerationError as exc:
            assert "CS0117" in str(exc), exc
        else:
            raise AssertionError("编译一直失败时应该抛 ScriptGenerationError")

        assert builds["n"] == 2, builds
        assert len(prompts) == 2, prompts
        assert "CS0117" in prompts[1], "编译器输出要回喂给模型，它才知道改哪儿"
        assert not (source / "Game" / "AI" / "Decks" / "Gen7Executor.cs").exists(), (
            "失败的脚本必须从源码树里清掉，否则它会毒死之后每一轮编译"
        )
        failed = windbot / "FailedScripts" / "Gen7Executor.failed.cs"
        assert failed.is_file(), "最后一次尝试要留一份给人看"


def test_script_generator_reports_missing_tree_instead_of_pretending() -> None:
    """没配源码树就说清楚缺什么（不做"假装写好了"的兜底）。"""

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        generator = scriptgen.DeckScriptGenerator(
            lambda prompt: None,  # type: ignore[arg-type]
            source_dir=root / "nowhere",
            windbot_dir=root / "nowhere-windbot",
            style_name="Gen1",
        )
        try:
            asyncio.run(
                generator.generate(
                    scriptgen.DeckScriptRequest(
                        deck_id=1, deck_name="x", cards=[scriptgen.CardInfo(card_id=1, name="卡")]
                    )
                )
            )
        except scriptgen.ScriptGenerationError as exc:
            assert "WindBot.csproj" in str(exc), exc
        else:
            raise AssertionError("没有源码树时不该「成功」")


def test_script_generator_handles_truncated_and_renamed_output() -> None:
    """两类"看着像成功"的坏回答都要拦住：写一半的、自己另起名字的。

    * 截断：代码取出来也编译不过，先要求它写短，省一轮无效编译；
    * 改名：能编译、能注册，但对局时按约定名找不到脚本，WindBot 会随机挑一个顶上——
      "用了错的脚本还看不出来"，所以必须在生成阶段就挡掉。
    """

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        replies = [
            "```csharp\nclass Gen9Executor : DefaultExecutor {\n  // 写一半就被截断了",
            fake_script("OtherName", "OtherNameExecutor"),
            fake_script("Gen9", "Gen9Executor"),
        ]
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return replies.pop(0)

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen9", max_attempts=4
        )

        async def ok_build() -> tuple:
            return True, "Build succeeded"

        generator._build = ok_build  # type: ignore[method-assign]
        result = asyncio.run(
            generator.generate(
                scriptgen.DeckScriptRequest(
                    deck_id=9, deck_name="x", cards=[scriptgen.CardInfo(card_id=1, name="卡")]
                )
            )
        )
        assert result.attempts == 3, result.attempts
        assert "写短" in prompts[1] or "截断" in prompts[1], prompts[1][-300:]
        assert "Gen9Executor" in prompts[2] and "名字" in prompts[2], prompts[2][-300:]


def test_runner_offers_the_new_kinds_only_when_the_tree_is_configured() -> None:
    """面板要能看出"写脚本/自动迭代"现在为什么不能用（缺源码树 vs 房间占着）。"""

    runner_module = _load("train.runner")
    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")

        def make(rooms: int, dirs: Any) -> Any:
            return runner_module.TrainingRunner(
                plugin_root=tmp / "plugin",
                workspace=tmp / "train",
                deck_db_path=tmp / "pool.db",
                store=store,
                generate=lambda *a, **k: None,  # type: ignore[arg-type]
                card_db=lambda: None,
                active_rooms=lambda: rooms,
                logger=None,
                max_duels=60,
                windbot_dirs=dirs,
            )

        kinds = {item["kind"]: item for item in make(0, lambda: (None, None)).describe_kinds()}
        assert kinds["write_script"]["ready"] is False
        assert "windbot_src_dir" in kinds["write_script"]["note"], kinds["write_script"]
        assert kinds["iterate"]["ready"] is False

        ready_dirs = lambda: (tmp / "src", tmp / "windbot")  # noqa: E731  测试里的小 lambda
        kinds = {item["kind"]: item for item in make(0, ready_dirs).describe_kinds()}
        assert kinds["write_script"]["ready"] is False, "卡库不可用时也不能写脚本（要卡文）"

        kinds = {item["kind"]: item for item in make(1, ready_dirs).describe_kinds()}
        assert kinds["arena"]["ready"] is False and kinds["iterate"]["ready"] is False
        assert "房间" in kinds["iterate"]["note"], kinds["iterate"]["note"]
        assert kinds["combo"]["needs_engine"] is False and kinds["iterate"]["needs_engine"] is True


def main() -> int:
    """逐个执行测试；协程测试用 asyncio.run 驱动。"""

    tests = [
        test_config_edit_replaces_only_touched_lines,
        test_config_edit_rejects_bad_values_without_touching_the_file,
        test_config_edit_inserts_keys_that_are_not_written_yet,
        test_config_edit_reports_missing_keys,
        test_deck_operations_go_through_the_plugin_and_need_confirmation,
        test_rooms_endpoint_lays_out_the_board_without_leaking_face_down_cards,
        test_script_generator_writes_compiles_and_reports_attempts,
        test_script_generator_feeds_build_errors_back_and_cleans_up_failures,
        test_script_generator_reports_missing_tree_instead_of_pretending,
        test_script_generator_handles_truncated_and_renamed_output,
        test_runner_offers_the_new_kinds_only_when_the_tree_is_configured,
    ]
    failures: List[str] = []
    for func in tests:
        name = func.__name__
        try:
            result = func()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures.append(name)
            import traceback

            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
