"""面板上的卡组操作、对局监控接口，以及「写脚本」生成器的测试。

两块：

1. **卡组操作与对局监控接口**：改随机池 / 删卡组要走插件的入口（真机上是事件循环），
   删除不可逆所以必须显式确认；`/api/rooms` 要能把牌桌摊平（里侧不泄卡号）。
2. **写脚本**：生成器用假的模型回调 + 假的源码树跑通"生成 → 编译失败回喂 → 成功"，
   以及"编译始终失败"时不把坏文件留在源码树里（留了会毒死之后每一轮编译）。

（这里原来还有一组「面板改配置」的用例。那一页因为 bug 太多按用户口径删掉了，
`configedit.py` 与它的用例一并删除；文件名先留着不改，免得丢掉 git 历史。）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import importlib
import json
import re
import sys
import tempfile
import time
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


# ---------------------------------------------------------------------------
# 卡组操作 / 对局监控接口
# ---------------------------------------------------------------------------


def _config_model() -> Any:
    """插件配置模型（替身拿它当一份像样的配置对象）。"""

    return _load("plugin").MaiPlayYgoConfig()


class StubPlugin:
    """面板要的插件接口替身（卡组操作记下来，对局数据造一份）。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._rooms: Dict[str, Any] = {}
        self._cards_cdb_path = ""
        self.config = _config_model()
        self.ctx = type("Ctx", (), {"paths": type("P", (), {"data_dir": str(data_dir)})()})()
        self.actions: List[Dict[str, Any]] = []
        self.settings: List[Dict[str, Any]] = []
        self.raise_on_action = ""
        self.raise_on_settings = ""

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

    def schedule_deck_settings(
        self, deck_id: int, *, in_random: Optional[bool] = None, brain_scope: Optional[str] = None
    ) -> str:
        self.settings.append({"deck_id": deck_id, "in_random": in_random, "brain_scope": brain_scope})
        if self.raise_on_settings:
            raise RuntimeError(self.raise_on_settings)
        return f"已保存：替身 #{deck_id}"


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


def test_deck_settings_endpoint_writes_random_pool_and_ai_scope() -> None:
    """卡组页每副牌的两个设置（随机池开关 / AI 决策档位）走 `/settings` 到插件。

    这里只验"接线"：面板把两个字段拆开送（只改一个不能顺手覆盖另一个），
    失败原因原样回给用户。档位取值与到决策层的映射由 `test_plugin_lifecycle.py` 的
    `test_deck_ai_scope_overrides_the_global_brain_switches` 盯着。
    """

    webui = _load("webui")
    import http.client

    def call(port: int, path: str, key: str, payload: Any):
        body = json.dumps(payload).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "POST", path, body=body, headers={"X-API-Key": key, "Content-Type": "application/json"}
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    secret = "e" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        server, port = _start_panel(webui, plugin, secret)
        try:
            # 只改随机池：档位一律送 None（插件那边就不动它）
            status, body = call(port, "/api/deck/12/settings", secret, {"in_random": False})
            assert status == 200 and body["ok"] is True, body
            assert plugin.settings[-1] == {"deck_id": 12, "in_random": False, "brain_scope": None}

            # 只改档位
            status, body = call(port, "/api/deck/12/settings", secret, {"brain_scope": "full"})
            assert body["ok"] is True, body
            assert plugin.settings[-1] == {"deck_id": 12, "in_random": None, "brain_scope": "full"}

            # 清空档位（面板上"跟随全局"送的就是空串）
            status, body = call(port, "/api/deck/12/settings", secret, {"brain_scope": ""})
            assert plugin.settings[-1]["brain_scope"] == "", plugin.settings

            # 插件那层拦下错值时要原样显示原因
            plugin.raise_on_settings = "不认识的 AI 决策档位：随便写的"
            status, body = call(port, "/api/deck/12/settings", secret, {"brain_scope": "随便写的"})
            assert body["ok"] is False and "不认识的 AI 决策档位" in body["error"], body

            # 卡组编号不是数字：直接拒掉
            status, body = call(port, "/api/deck/abc/settings", secret, {"brain_scope": "off"})
            assert body["ok"] is False, body
        finally:
            server.stop_now()


def test_training_deck_choices_expose_the_script_that_actually_plays() -> None:
    """训练表单的卡组下拉要拿到**真正上场的那份脚本**（`script`）。

    这条是 bug 的回归测试：接口原来只给 `generated_script` / `picked_style`，
    而前端读的是 `script`，于是每副牌都显示成"还没脚本"——连已经写好 Gen106 的赖皮
    在面板上也是这样（用户在真机上看到的就是这个）。
    """

    webui = _load("webui")
    import http.client

    secret = "f" * 32
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        deckpool = _load("duel.deckpool")
        pool = deckpool.DeckPool(data_dir)
        try:
            with_script = pool.add(
                group_id="111",
                display_name="有脚本的牌",
                contributor_id="u",
                contributor_name="群友",
                ydk_text="#main\n100\n!side\n",
                deck_code="",
                source_format="ydk",
                main_count=1,
                extra_count=0,
                side_count=0,
            )
            pool.set_generated_script(with_script.deck_id, "Gen111")
            pool.add(
                group_id="111",
                display_name="只有风格的牌",
                contributor_id="u",
                contributor_name="群友",
                ydk_text="#main\n101\n!side\n",
                deck_code="",
                source_format="ydk",
                main_count=1,
                extra_count=0,
                side_count=0,
                windbot_deck="RaiseMoon",
            )
        finally:
            pool.close()

        plugin = StubPlugin(data_dir)
        server, port = _start_panel(webui, plugin, secret)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/api/training", headers={"X-API-Key": secret})
            body = json.loads(connection.getresponse().read().decode("utf-8"))
            connection.close()
            choices = {item["name"]: item for item in body["decks"]}
            assert choices["有脚本的牌"]["script"] == "Gen111", choices["有脚本的牌"]
            # 没有生成脚本时退到实测挑的风格 / 自带风格，总之要有个名字
            assert choices["只有风格的牌"]["script"] == "RaiseMoon", choices["只有风格的牌"]
        finally:
            server.stop_now()
            # 面板读卡组库是在自己的线程里跑的，等它收尾再删临时目录（Windows 会报文件占用）
            time.sleep(0.4)


def test_rooms_endpoint_lays_out_the_board_without_leaking_face_down_cards() -> None:
    """对局监控接口：**整张牌桌**都要给（额外怪兽区 / 场地 / 灵摆 / 三堆计数），里侧不泄卡号。

    这条同时是那个 `TypeError` 的回归测试：记录器里存的是 `ZoneCard` 对象（带 `position`），
    不是卡号——第一版把它当卡号 `int(card)`，真机对局中整页报
    `int() argument must be a string ... not 'ZoneCard'`。所以这里**用真的 `ZoneCard` 造数据**。
    """

    webui = _load("webui")
    fieldstate = _load("duel.fieldstate")
    zone_card = fieldstate.ZoneCard
    import http.client

    class Player:
        def __init__(self, lp: int, grave: int) -> None:
            self.lp = lp
            self.grave = grave
            self.banished = 1
            self.extra = 2

    # 位号与内核一致：怪兽区 5/6 是额外怪兽区、魔陷区第 5 号位是场地魔法、灵摆区＝魔陷区最左/最右
    # 攻守分三种情形各来一张：内核给过当前值（100）、只有卡面数值（400）、里侧（200/300）
    table = {
        (0, fieldstate.MONSTER_ZONE, 0): zone_card(100, 0x1, attack=3100, defense=2600),
        (0, fieldstate.MONSTER_ZONE, 5): zone_card(400, 0x1),          # 额外怪兽区（内核还没说过攻守）
        (1, fieldstate.MONSTER_ZONE, 2): zone_card(200, 0x8),          # 对手：里侧守备
        (0, fieldstate.SPELL_ZONES[0], 1): zone_card(300, 0x2),        # 盖放的魔陷（里侧）
        (0, fieldstate.SPELL_ZONES[0], 5): zone_card(500, 0x4),        # 场地魔法（表侧守备位＝表侧）
        (0, fieldstate.SPELL_ZONES[1], 0): zone_card(600, 0x4),        # 灵摆区左
    }
    state = types.SimpleNamespace(
        zones=dict(table),
        players={0: Player(6800, 4), 1: Player(7200, 1)},
        zones_of=lambda seat: {
            (location, sequence): card
            for (controller, location, sequence), card in table.items()
            if controller == seat
        },
    )

    class Detail:
        """卡库查出来的卡面信息（面板画卡名 / 攻守角标要用）。"""

        def __init__(
            self, name: str, type_text: str, atk: Any = None, defense: Any = None, level: int = 0
        ) -> None:
            self.name = name
            self.type_text = type_text
            self.atk = atk
            self.defense = defense
            self.level = level
            self.stats = f"攻{atk}/守{defense}/星{level}" if atk is not None else ""
            self.effect = ""

    class CardDb:
        """只认识场上这几张卡。"""

        available = True

        def card_details(self, card_ids: List[int]) -> Dict[int, Any]:
            known = {
                100: Detail("混源龙", "怪兽 效果", 3000, 2500, 8),
                400: Detail("渊兽", "怪兽 融合", 2500, 2000, 8),
                500: Detail("王家的神殿", "魔法 场地"),
                600: Detail("时读之魔术师", "怪兽 灵摆 效果", 1200, 800, 4),
            }
            return {int(cid): known[int(cid)] for cid in card_ids if int(cid) in known}

    class Recorder:
        self_seat = 0
        turn_count = 3
        phase = "主要阶段1"
        field_state = state
        players = {
            0: types.SimpleNamespace(name="憨憨", normal_summons=2, sp_summons=3, effects=4, attacks=1),
            1: types.SimpleNamespace(name="群友", normal_summons=1),
        }

    class Session:
        started = True
        finished = False

        def recorder(self) -> Any:
            return Recorder()

    secret = "r" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        plugin._card_db = CardDb()
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
            assert not room["error"], room["error"]
            ours = [side for side in room["sides"] if side["is_self"]][0]
            theirs = [side for side in room["sides"] if not side["is_self"]][0]
            assert ours["lp"] == 6800 and theirs["lp"] == 7200, room["sides"]
            assert ours["name"] == "憨憨" and theirs["name"] == "群友", room["sides"]
            assert ours["deck"] == "升辉月", ours["deck"]

            # 完整牌桌：5 主怪兽 + 2 额外怪兽 + 5 魔陷（最左/最右＝灵摆）+ 场地 + 三堆
            assert len(ours["monsters"]) == 5 and len(ours["spells"]) == 5, ours
            assert len(ours["extra_monsters"]) == 2, ours
            assert ours["spell_pendulum"] == [True, False, False, False, True], ours["spell_pendulum"]
            assert ours["piles"] == {"grave": 4, "banished": 1, "extra": 2}, ours["piles"]
            # 表侧怪：报卡号 + 表示形式
            assert ours["monsters"][0]["id"] == 100 and ours["monsters"][0]["attack"] is True, ours["monsters"][0]
            assert ours["monsters"][1] is None, "空格要显式给 null，前端才画得出空场"
            # 额外怪兽区 / 场地各自归位（不能都塞进主怪兽区与魔陷区）
            assert ours["extra_monsters"][0]["id"] == 400, ours["extra_monsters"]
            assert ours["field_zone"]["id"] == 500, ours["field_zone"]
            # ⚠ 灵摆区就是魔陷区最左那格：内核把这儿的牌报成 PENDULUM_ZONE，也要画在魔陷区 1
            assert ours["spells"][0] and ours["spells"][0]["id"] == 600, ours["spells"][0]
            # 里侧：只报"有卡 + 里侧 + 攻/守表示"，一个卡号都不能给（否则面板比对手本人知道得更多）
            back = theirs["monsters"][2]
            assert back is not None and back["face_up"] is False, back
            assert back.get("id") in (0, None) and not back.get("name"), back
            assert back["attack"] is False, f"里侧守备也要看得出是在守备：{back}"

            # 攻守：内核给过当前值就用内核的（stats_live=True）；只有卡面数值时也照样给，并标明来源
            front = ours["monsters"][0]
            assert front["name"] == "混源龙" and front["kind"] == "monster", front
            assert (front["atk"], front["def_"]) == (3100, 2600), front
            assert front["stats_live"] is True, front
            extra = ours["extra_monsters"][0]
            assert (extra["atk"], extra["def_"]) == (2500, 2000), extra
            assert extra["stats_live"] is False, "内核还没说过攻守，就要标明这是卡面数值"
            # 盖着的魔陷：面板也要画得出来（只有"里侧 + 攻/守表示"，没有卡号）
            hidden = ours["spells"][1]
            assert hidden is not None and hidden["face_up"] is False, hidden
            assert hidden["attack"] is True, f"里侧攻击位（0x2）的盖牌：{hidden}"
            # 表侧魔陷没有攻守角标（不是怪兽）
            assert ours["field_zone"].get("atk") is None, ours["field_zone"]
            # 台账（记录器数出来的东西）也要带上
            assert ours["stats"]["normal_summons"] == 2 and ours["stats"]["sp_summons"] == 3, ours["stats"]
        finally:
            server.stop_now()


def test_rooms_endpoint_still_draws_the_table_before_the_duel_starts() -> None:
    """房刚开好、人还没进来时也要给出两边的牌桌（这一条是用户报"监控用不了"的正因）。

    当时的行为：记录器里还没有局面（`field_state` 为空），`_sides_from_state` 直接返回空列表，
    面板上只剩一个标题、牌桌整块空白——看着像坏了，其实只是"还没开始"。
    现在照样给两边的名字与**初始 LP**，牌区留空让人看出"空场"。
    """

    webui = _load("webui")
    import http.client

    class Recorder:
        self_seat = 0
        turn_count = 0
        phase = ""
        field_state = None          # 还没收到 MSG_START，什么都没有
        players: Dict[int, Any] = {}

    class Session:
        started = False
        finished = False

        def recorder(self) -> Any:
            return Recorder()

    secret = "w" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        plugin.config.duel.start_lp = 6000      # 用非默认值，确认真的读的是配置
        plugin._rooms = {
            "qq:group:9": types.SimpleNamespace(group_id="9", deck_name="刻魔异响鸣", session=Session())
        }
        server, port = _start_panel(webui, plugin, secret)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/api/rooms", headers={"X-API-Key": secret})
            payload = json.loads(connection.getresponse().read().decode("utf-8"))
            connection.close()
            room = payload["rooms"][0]
            assert room["started"] is False, room
            assert len(room["sides"]) == 2, room["sides"]
            assert [side["lp"] for side in room["sides"]] == [6000, 6000], room["sides"]
            assert all(zone is None for side in room["sides"] for zone in side["monsters"]), room["sides"]
            assert [side["is_self"] for side in room["sides"]] == [True, False], room["sides"]
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


def handler_reply(prompt: str) -> str:
    """按提示词点名要的那几个函数，交一份占位处理函数。

    现在的流程是"骨架由代码拼、模型只写处理函数"，所以假模型也得照这个契约回话：
    从提示词里读出要哪些 `Card<卡号>Handler`（**只认这个形状**——速查表里还有
    `SomeCardHandler` 那种示例名，不按卡号筛会把示例也当成要求）。
    """

    names = sorted(set(re.findall(r"private bool (Card\d+Handler)\(\)", prompt)), key=len)
    body: List[str] = []
    for name in names:
        body += [
            f"        private bool {name}()",
            "        {",
            "            return false;",
            "        }",
            "",
        ]
    return "```csharp\n" + "\n".join(body).rstrip() + "\n```"


def _make_tree(root: Path) -> tuple:
    """造一份最小的"WindBot 源码树 + 运行目录"。"""

    source = root / "windbot-src"
    (source / "Game" / "AI" / "Decks").mkdir(parents=True, exist_ok=True)
    (source / "WindBot.csproj").write_text("<Project />", encoding="utf-8")
    windbot = root / "windbot"
    (windbot / "Decks").mkdir(parents=True, exist_ok=True)
    return source, windbot


def test_deck_detail_splits_main_extra_and_side() -> None:
    """卡组详情要把主卡组 / 额外卡组 / 副卡组**分开**（用户报"额外和副卡组应该分开"）。

    回归测试：分区判断原来把 `!side` 写在"以 `#` 开头即注释"那一支里面——`!side` 不以 `#` 开头，
    永远认不出来，副卡组的卡全被算进额外卡组。真机上表现为「额外卡组 30 张 / 副卡组 0 张」
    （那副牌其实是 主 54 / 额 15 / 副 15）。这里连 `!extra` 这类别的写法一起钉住。
    """

    webui = _load("webui")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "分区测试.ydk"
        # 三种标记写法都写一遍：#main / #extra / !side（用 chr(10) 拼行，少一层转义）
        path.write_text(
            chr(10).join(
                ["#created by mai-play-ygo - 测试", "#main", "1", "2", "3",
                 "#extra", "9", "9", "!side", "5", ""]
            ),
            encoding="utf-8",
        )
        summary = webui._read_ydk_summary(path)
        assert {key: len(value) for key, value in summary.items() if key != "error"} == {
            "main": 3, "extra": 2, "side": 1
        }, summary

        # 没有额外卡组标记的那种（老工具只写 #main + !side）：额外区应当是空的，不能把副卡组算进去
        legacy = Path(directory) / "老格式.ydk"
        legacy.write_text(chr(10).join(["#main", "1", "2", "!side", "5", "6", ""]), encoding="utf-8")
        legacy_summary = webui._read_ydk_summary(legacy)
        assert {key: len(value) for key, value in legacy_summary.items() if key != "error"} == {
            "main": 2, "extra": 0, "side": 2
        }, legacy_summary


def test_deck_workspace_endpoint_lists_script_and_combo_archives() -> None:
    """训练台要先看见"这副牌已经有什么"：当前脚本、推演存档、最近的结论。

    这里顺带锁住路由顺序：`/api/deck/<id>/workspace` 比"卡组详情"更具体，
    必须排在它前面——第一版把顺序写反了，`deck_id` 变成 `97/workspace`，
    面板上显示的是"找不到卡组：97/workspace"。
    """

    webui = _load("webui")
    import http.client

    def get(port: int, path: str, key: str):
        """对面板发一个 GET，返回 ``(状态码, 解析后的 JSON)``。"""

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", path, headers={"X-API-Key": key})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    secret = "k" * 32
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        deck_text = "\n".join(
            ["#main"] + [str(1000 + i) for i in range(3)] + ["#extra", "5001", "!side", ""]
        )
        deckpool = _load("duel.deckpool")
        pool = deckpool.DeckPool(data_dir)
        try:
            stored = pool.add(
                group_id="111",
                display_name="测试牌",
                contributor_id="u",
                contributor_name="群友",
                ydk_text=deck_text,
                deck_code="",
                source_format="ydk",
                main_count=3,
                extra_count=1,
                side_count=0,
            )
            pool.set_generated_script(stored.deck_id, "Gen111")
        finally:
            pool.close()

        # 造一份推演存档（训练台的"看最近推演"读的就是它）
        workspace = data_dir / "train"
        (workspace / "combos").mkdir(parents=True, exist_ok=True)
        (workspace / "combos" / f"20261008-193012-{stored.deck_id}-测试牌.txt").write_text(
            "【这副牌想做什么】先手做阻抗。\n", encoding="utf-8"
        )

        plugin = StubPlugin(data_dir)
        server, port = _start_panel(webui, plugin, secret)
        try:
            status, payload = get(port, f"/api/deck/{stored.deck_id}/workspace", secret)
            assert status == 200, (status, payload)
            assert payload["ok"] is True, payload
            assert payload["deck"]["script"] == "Gen111", payload["deck"]
            assert [item["label"] for item in payload["combos"]], payload["combos"]
            assert "10-08 19:30" in payload["combos"][0]["label"], payload["combos"][0]
            assert "先手做阻抗" in payload["combo_preview"], payload["combo_preview"]

            # 路由顺序：详情那条不能被 workspace 抢走，workspace 也不能被详情吃掉
            status, payload = get(port, f"/api/deck/{stored.deck_id}?group=111", secret)
            assert payload["ok"] is True, payload

            status, payload = get(port, "/api/deck/abc/workspace", secret)
            assert payload["ok"] is False, payload
        finally:
            server.stop_now()
            time.sleep(0.4)


def test_script_generator_writes_compiles_and_reports_attempts() -> None:
    """生成 → 编译成功：骨架 + 分批写出来的处理函数都进源码树，卡表也写一份。

    这套流程的重点是"**第一次就写足量**"：`CardId` 常量表、登记表、通用兜底由代码拼（不花模型
    额度、不可能写错），处理函数分批问模型——每批 `HANDLERS_PER_CALL` 张、一次调用一批。
    脚本长度因此是"批数 × 每批长度"，而不是"一次回答能写多长"（宿主的单次模型调用只有 30 秒）。
    """

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return handler_reply(prompt)

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen42", max_attempts=3
        )

        async def fake_build() -> tuple:
            return True, "Build succeeded"

        generator._build = fake_build  # type: ignore[method-assign]
        # 9 张有效果的卡 + 1 张白板：白板没有可判断的东西，不写处理函数
        cards = [
            scriptgen.CardInfo(card_id=100 + index, name=f"测试怪兽{index}", zone="主卡组", effect="抽 1 张。")
            for index in range(9)
        ]
        cards.append(scriptgen.CardInfo(card_id=500, name="测试白板", zone="主卡组", type_text="怪兽 通常"))
        request = scriptgen.DeckScriptRequest(
            deck_id=42,
            deck_name="测试牌",
            cards=cards,
            combo_guide="先通召 100，再做 500。",
            extra_prompt="先手优先做阻抗",
            feedback="上一轮太爱盖牌",
        )
        result = asyncio.run(generator.generate(request))

        assert result.attempts == 1, result
        assert len(prompts) == 2, f"9 张卡该分成 2 批（每批 8 张），实际问了 {len(prompts)} 次"
        code = result.file_path.read_text(encoding="utf-8")
        assert "class Gen42Executor" in code
        assert scriptgen.DeckScriptGenerator.count_handlers(code) == 9, "9 张有效果的卡都要有处理函数"
        for card_id in [100 + index for index in range(9)]:
            assert f"AddExecutor(ExecutorType.Activate, CardId.Card{card_id}, Card{card_id}Handler);" in code
        # 白板只登记"能被拍出来"，不登记发动
        assert "AddExecutor(ExecutorType.SummonOrSet, CardId.Card500);" in code
        assert "AddExecutor(ExecutorType.Activate, CardId.Card500" not in code
        assert "AddExecutor(ExecutorType.Repos, DefaultMonsterRepos);" in code
        assert (windbot / "Decks" / "AI_Gen42.ydk").is_file(), "卡表也要写一份（脚本自身要自洽）"
        # 提示词里该有的东西：本批卡文、要写的函数名、combo、作者要求、上一轮反馈
        prompt = prompts[0]
        for needle in ("抽 1 张", "Card100Handler", "先通召 100", "先手优先做阻抗", "上一轮太爱盖牌"):
            assert needle in prompt, f"提示词里缺少 {needle}"
        # 两个真机上栽过的 API 名要写在速查表里（模型最容易自己编名字的两处）：
        # 墓区是 CardLocation.Grave（不是 Graveyard）、"这次问的是哪种动作"是 Type（不是 ExecutorType）
        for needle in ("CardLocation.Grave", "CardLocation.Graveyard", "Type == ExecutorType"):
            assert needle in prompt, f"速查表里缺少 {needle}"


def test_script_generator_registers_what_it_got_and_warns_about_the_rest() -> None:
    """某一批最终没写成时：**不登记它的发动**（没判断就"能发就发"更糟），并在结果里说清少了哪些卡。"""

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)

        async def generate(prompt: str) -> str:
            # 只写第一张卡的处理函数，其余一律不写（模拟"这一批怎么问都写不出来"）
            return handler_reply(prompt).split("        private bool Card")[0] + (
                "        private bool Card100Handler()\n        {\n            return true;\n        }\n```"
            )

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen43", max_attempts=1
        )

        async def ok_build() -> tuple:
            return True, "Build succeeded"

        generator._build = ok_build  # type: ignore[method-assign]
        cards = [
            scriptgen.CardInfo(card_id=100, name="第一张", zone="主卡组", effect="抽 1 张。"),
            scriptgen.CardInfo(card_id=101, name="第二张", zone="主卡组", effect="破坏一张。"),
        ]
        result = asyncio.run(
            generator.generate(scriptgen.DeckScriptRequest(deck_id=43, deck_name="x", cards=cards))
        )
        code = result.file_path.read_text(encoding="utf-8")
        assert "Card100Handler);" in code, code
        assert "Card101Handler" not in code, "没写成的卡不能引用一个不存在的函数名"
        assert "AddExecutor(ExecutorType.Activate, CardId.Card101" not in code, "没判断就不登记发动"
        assert any("第二张" in note for note in result.warnings), result.warnings
        assert scriptgen.DeckScriptGenerator.count_handlers(code) == 1, code


def test_script_generator_retries_only_the_broken_batch_and_cleans_up_failures() -> None:
    """编译报错落在哪一批就只重问那一批；几轮都不成时**不能把坏文件留在源码树里**。

    留着的后果很具体：csproj 会把 `Game/AI/Decks` 下所有 .cs 都编进去，
    这份失败的尝试就成了同一个类的第二份定义，之后每轮编译都报 CS0101/CS0579
    （实测把三轮重试全毒死过）。
    """

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        target = source / "Game" / "AI" / "Decks" / "Gen7Executor.cs"
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return handler_reply(prompt)

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen7", max_attempts=2
        )

        async def failing_build() -> tuple:
            # 报错行取自真实文件、指向处理函数那一行——这样才测得到"按行号定位到哪一批"
            rows = target.read_text(encoding="utf-8").splitlines()
            line = next(index for index, text in enumerate(rows, start=1) if "Card7Handler()" in text)
            return False, f"Decks/Gen7Executor.cs({line},26): error CS0117: 没有这个成员"

        generator._build = failing_build  # type: ignore[method-assign]
        request = scriptgen.DeckScriptRequest(
            deck_id=7, deck_name="测试牌", cards=[scriptgen.CardInfo(card_id=7, name="卡", effect="抽 1 张。")]
        )
        try:
            asyncio.run(generator.generate(request))
        except scriptgen.ScriptGenerationError as exc:
            assert "CS0117" in str(exc), exc
        else:
            raise AssertionError("编译一直失败时应该抛 ScriptGenerationError")

        assert len(prompts) == 2, f"1 批 × 2 轮＝2 次调用，实际 {len(prompts)} 次"
        assert "CS0117" in prompts[1], "编译器输出要回喂给模型，它才知道改哪儿"
        assert not target.exists(), (
            "失败的脚本必须从源码树里清掉，否则它会毒死之后每一轮编译"
        )
        failed = windbot / "FailedScripts" / "Gen7Executor.failed.cs"
        assert failed.is_file(), "最后一次尝试要留一份给人看"


def test_script_generator_says_exe_is_locked_instead_of_dumping_msbuild() -> None:
    r"""exe 被占用（有人在打）时的编译失败要给一句人话，而不是一堆 MSBuild 警告。

    实测（2026-10-08）：有人正在跟机器人打的时候跑「写脚本」，`dotnet build` 在
    "复制 obj\Release\WindBot.exe 到 bin\Release\WindBot.exe" 这一步失败（MSB3026）——
    那一局正跑着这个 exe。原始输出看不懂的人只会以为"生成功能坏了"。
    """

    scriptgen = _load("train.scriptgen")
    output = "\n".join(
        [
            "  无可执行操作。指定的项目均不包含要还原的包。",
            r"C:\Program Files\dotnet\sdk\9.0.304\Microsoft.Common.CurrentVersion.targets(4916,5): "
            r"warning MSB3026: 无法将“obj\Release\WindBot.exe”复制到“bin\Release\WindBot.exe”",
        ]
    )
    try:
        scriptgen.DeckScriptGenerator._raise_if_exe_locked(output)
    except scriptgen.ScriptGenerationError as exc:
        assert "等这局打完" in str(exc), exc
        assert "MSB3026" in str(exc), exc
    else:
        raise AssertionError("exe 被占用时应该抛 ScriptGenerationError")

    # 别的编译错误（真的写错了代码）不该被翻译成"有人在打"
    scriptgen.DeckScriptGenerator._raise_if_exe_locked(
        "Decks/Gen1Executor.cs(9,26): error CS0117: 没有这个成员"
    )


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


def test_script_generator_restores_the_previous_script_when_it_fails() -> None:
    """改脚本失败时**要把上一版原样放回去**：池子里记的还是这个名字，源码树里没有它，
    对局按 `Deck=<名字>` 找不到，WindBot 会静默换一个随机执行器顶上。

    同时要把报错那几行的代码原文回喂给模型——只给"第 N 行类型不匹配"，它会照着原样重写一遍
    （真机上实测连着两轮报同一个错）。
    """

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        target = source / "Game" / "AI" / "Decks" / "Gen5Executor.cs"
        target.write_text("// 上一版：能用的脚本\n", encoding="utf-8")
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return handler_reply(prompt)

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen5", max_attempts=2
        )

        async def failing_build() -> tuple:
            rows = target.read_text(encoding="utf-8").splitlines()
            line = next(index for index, text in enumerate(rows, start=1) if "Card5Handler()" in text)
            return False, f"Decks/Gen5Executor.cs({line},39): error CS0029: 无法将类型 int 转换为 ClientCard"

        generator._build = failing_build  # type: ignore[method-assign]
        request = scriptgen.DeckScriptRequest(
            deck_id=5, deck_name="测试牌", cards=[scriptgen.CardInfo(card_id=5, name="卡", effect="抽 1 张。")]
        )
        try:
            asyncio.run(generator.generate(request))
        except scriptgen.ScriptGenerationError as exc:
            assert "上一版脚本原样放回" in str(exc), exc
        else:
            raise AssertionError("一直编译不过时应该抛 ScriptGenerationError")

        assert target.read_text(encoding="utf-8") == "// 上一版：能用的脚本\n", "上一版必须回到原位"
        assert "Card5Handler" in prompts[1], "回喂里要有报错处的代码原文"
        assert "←" in prompts[1], "报错行要标出来，模型才知道改哪句"


def test_script_generator_handles_truncated_and_renamed_output() -> None:
    """两类"看着像成功"的坏回答都要拦住：写一半的、函数名自己另起的。

    * 截断：这一批没写完 → 回喂"写完整"重问（分批之后每批本来就不长，
      所以要求的是**写完这一批**，不再是"砍掉几张卡写短一点"）；
    * 改名：函数名与登记表对不上就等于这张卡没写 → 点名要回来；
    两次都不成时，下一轮还会再补这一批（而不是整份任务就此失败）。
    """

    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, windbot = _make_tree(root)
        replies = [
            "```csharp\n        private bool Card9Handler()\n        {\n            // 写一半就被截断了",
            "```csharp\n        private bool Foo()\n        {\n            return false;\n        }\n```",
        ]
        prompts: List[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            if replies:
                return replies.pop(0)
            return handler_reply(prompt)

        generator = scriptgen.DeckScriptGenerator(
            generate, source_dir=source, windbot_dir=windbot, style_name="Gen9", max_attempts=4
        )

        async def ok_build() -> tuple:
            return True, "Build succeeded"

        generator._build = ok_build  # type: ignore[method-assign]
        result = asyncio.run(
            generator.generate(
                scriptgen.DeckScriptRequest(
                    deck_id=9, deck_name="x", cards=[scriptgen.CardInfo(card_id=9, name="卡", effect="抽 1 张。")]
                )
            )
        )
        assert result.attempts == 2, f"第一轮这一批没写成，第二轮补上：{result.attempts}"
        assert "截断" in prompts[1], prompts[1][-400:]
        assert "Card9Handler" in prompts[2] and "没有写出来" in prompts[2], prompts[2][-400:]
        assert scriptgen.DeckScriptGenerator.count_handlers(result.code) == 1, result.code


def test_runner_offers_only_the_four_panel_kinds() -> None:
    """面板上只该看到四项（用户 2026-10-09 口径）：卡组互打 / 编写脚本 / 卡组迭代 / 复盘优化。

    推演 combo、脚本体检、录像复盘降级成**内部步骤**（由这四项自己调用），
    不再出现在面板上——用户不该先想"我该跑哪一项"。同时还要能看出"现在为什么不能用"
    （缺源码树 / 房间占着）。"""

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
        assert set(kinds) == {"arena", "write_script", "iterate", "review"}, sorted(kinds)
        assert kinds["write_script"]["ready"] is False
        assert "windbot_src_dir" in kinds["write_script"]["note"], kinds["write_script"]
        assert kinds["iterate"]["ready"] is False
        # 打牌类的四项里只有两项需要引擎，且都要能"选一副对手卡组"
        assert kinds["arena"]["fields"][0] == "opponent_deck", kinds["arena"]["fields"]
        assert kinds["iterate"]["fields"][0] == "opponent_deck", kinds["iterate"]["fields"]

        ready_dirs = lambda: (tmp / "src", tmp / "windbot")  # noqa: E731  测试里的小 lambda
        kinds = {item["kind"]: item for item in make(0, ready_dirs).describe_kinds()}
        assert kinds["write_script"]["ready"] is False, "卡库不可用时也不能写脚本（要卡文）"

        kinds = {item["kind"]: item for item in make(1, ready_dirs).describe_kinds()}
        assert kinds["arena"]["ready"] is False and kinds["iterate"]["ready"] is False
        assert "房间" in kinds["iterate"]["note"], kinds["iterate"]["note"]
        assert kinds["review"]["needs_engine"] is False and kinds["iterate"]["needs_engine"] is True


def main() -> int:
    """逐个执行测试；协程测试用 asyncio.run 驱动。"""

    tests = [
        test_deck_operations_go_through_the_plugin_and_need_confirmation,
        test_deck_settings_endpoint_writes_random_pool_and_ai_scope,
        test_deck_detail_splits_main_extra_and_side,
        test_training_deck_choices_expose_the_script_that_actually_plays,
        test_deck_workspace_endpoint_lists_script_and_combo_archives,
        test_rooms_endpoint_lays_out_the_board_without_leaking_face_down_cards,
        test_rooms_endpoint_still_draws_the_table_before_the_duel_starts,
        test_script_generator_writes_compiles_and_reports_attempts,
        test_script_generator_registers_what_it_got_and_warns_about_the_rest,
        test_script_generator_retries_only_the_broken_batch_and_cleans_up_failures,
        test_script_generator_says_exe_is_locked_instead_of_dumping_msbuild,
        test_script_generator_reports_missing_tree_instead_of_pretending,
        test_script_generator_handles_truncated_and_renamed_output,
        test_script_generator_restores_the_previous_script_when_it_fails,
        test_runner_offers_only_the_four_panel_kinds,
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
