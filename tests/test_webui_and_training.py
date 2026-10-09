"""面板（`webui.py`）与训练功能（`train/`）的测试。

这一层有两个容易"看着没事、真机上出事"的点，所以用例围着它们写：

1. **鉴权**：面板能读到卡组池、插件配置与日志，密钥错了必须 401、没带必须 401，
   而且 cookie / 请求头 / query 三种带法都要认（否则用户从浏览器登进去也看不到东西）。
2. **停任务**：训练脚本自己会起 ygopro 与 WindBot，只杀顶层进程会留下孤儿内核占着端口
   （下一轮任务就随机失败）。所以既验证"停掉之后子进程真的不再输出"，也验证杀进程树用的
   是带 `/T` 的 taskkill（见 `test_kill_tree_uses_taskkill_with_tree_flag`）。

训练执行器的两条路都要跑一遍，而且**不用真打牌**：combo 推演本来就是纯模型调用；
起子进程那条路用临时目录里的假工具脚本。

面板的请求一律用 `http.client` 直连环回端口（不拼 URL、不经过任何代理配置）：
对被测的这块服务来说，"本机自己刚起的那个端口"就是它唯一该说话的对象。

直接用 ``python tests/test_webui_and_training.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import http.client
import json
import os
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

#: 面板只监听环回；测试连的也是环回。
_LOOPBACK = "127.0.0.1"

#: 插件按包加载（宿主就是这么做的）：把插件目录挂成一个包，
#: 这样 `train/store.py` 里的 `from .analysis import ...` 才能解析到同一个包里的模块。
_PACKAGE = "mai_play_ygo_webui_tests"


def _load(module_name: str):
    """按宿主的方式把插件模块当作包内模块导入。

    不能对每个文件单独 `spec_from_file_location`：那样 `train/runner.py` 会被当成
    "自己就是一个包"，它里面的 `from .analysis import` 会去找
    `...train.runner.analysis` 而不是 `...train.analysis`（实测报的就是这个错）。
    所以这里先把插件目录注册成一个真包，剩下的交给 import 系统。
    """

    import importlib
    import types

    package = sys.modules.get(_PACKAGE)
    if package is None:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [str(_PLUGIN_ROOT)]  # type: ignore[attr-defined]
        sys.modules[_PACKAGE] = package
    return importlib.import_module(f"{_PACKAGE}.{module_name}")


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


@dataclass
class FakeDetail:
    """卡库返回的卡详情替身（只带 `build_deck_digest` 真正读的那几个字段）。"""

    name: str
    type_text: str = "效果怪兽"
    stats: str = "4 星 / 攻 1800 / 守 1200"
    effect: str = "把自己解放才能发动。从卡组抽 1 张。"


class FakeCardDb:
    """卡库替身：认识给定的卡号，别的一律查不到。"""

    def __init__(self, names: Dict[int, str]) -> None:
        self.available = True
        self.path = Path("fake-cards.cdb")
        self._names = dict(names)

    def card_details(self, card_ids: List[int]) -> Dict[int, FakeDetail]:
        """只给认识的卡，其余不返回（模拟"卡库不认识这张卡"）。"""

        return {
            int(card_id): FakeDetail(name=self._names[int(card_id)])
            for card_id in card_ids
            if int(card_id) in self._names
        }


class StubPlugin:
    """面板要的插件替身：只实现 `webui.py` 真正调用的那几个方法。"""

    def __init__(self, data_dir: Path, *, training_model: str = "", workspace: str = "") -> None:
        self._data_dir = data_dir
        self._rooms: Dict[str, Any] = {}
        self._cards_cdb_path = ""
        self._store: Any = None
        self._runner: Any = None
        self._workspace = workspace
        self.started: List[Dict[str, Any]] = []
        self.stopped = 0

        config_module = _load("plugin")
        self.config = config_module.MaiPlayYgoConfig()
        self.config.llm.training_model = training_model
        self.config.training.workspace = workspace
        self.ctx = type("Ctx", (), {"paths": type("P", (), {"data_dir": str(data_dir)})()})()

    # 面板调用的接口
    def training_store(self) -> Any:
        return self._store

    def training_runner(self) -> Any:
        return self._runner

    def _training_workspace(self) -> Path:
        raw = (self._workspace or "").strip()
        return Path(raw) if raw else self._data_dir / "train"

    def schedule_training_start(self, kind: str, params: Dict[str, Any]) -> Any:
        self.started.append({"kind": kind, "params": params})
        store_module = _load("train.store")
        return store_module.TrainingRun(run_id="stub01", kind=kind, title=f"替身任务 {kind}")

    def schedule_training_stop(self) -> bool:
        self.stopped += 1
        return True


# ---------------------------------------------------------------------------
# 面板请求辅助（直连环回端口）
# ---------------------------------------------------------------------------


def _http(
    port: int,
    method: str,
    path: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
):
    """向面板的环回端口发一个请求，返回 ``(状态码, 正文, 响应头)``。

    4xx/5xx 不抛异常（用例要断言状态码）；连接层面失败照抛，由调用方处理。
    """

    connection = http.client.HTTPConnection(_LOOPBACK, port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            response.read().decode("utf-8", errors="replace"),
            dict(response.getheaders()),
        )
    finally:
        connection.close()


def _start_panel(webui: Any, plugin: Any, api_key: str):
    """起一个面板（端口 0＝让系统分配），返回 ``(server, port)``。"""

    server = webui.WebUIServer(
        plugin, host=_LOOPBACK, port=0, api_key=api_key, key_source="测试", logger=None
    )
    assert server.start(), "面板起不来（端口 0 应该总能绑上）"
    return server, server.bound_port


# ---------------------------------------------------------------------------
# 密钥解析
# ---------------------------------------------------------------------------


def test_resolve_api_key_prefers_config_then_env_then_file() -> None:
    """密钥来源顺序：配置 → 环境变量 → 自动生成（生成的那份要落盘并复用）。"""

    webui = _load("webui")
    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory)
        key, source = webui.resolve_api_key("from-config", data_dir)
        assert key == "from-config", key
        assert "配置" in source, source
        assert not (data_dir / webui.KEY_FILE_NAME).exists(), "配置里有密钥时不该再生成一份"

        os.environ["YGO_WEBUI_KEY"] = "from-env"
        try:
            key, source = webui.resolve_api_key("", data_dir)
            assert key == "from-env", key
            assert "环境变量" in source, source
        finally:
            del os.environ["YGO_WEBUI_KEY"]

        key, source = webui.resolve_api_key("", data_dir)
        assert len(key) >= 20, f"自动生成的密钥太短：{key!r}"
        assert "自动生成" in source, source
        written = (data_dir / webui.KEY_FILE_NAME).read_text(encoding="utf-8").strip()
        assert written == key, "自动生成的密钥必须写进密钥文件（登录页要告诉用户去哪找）"
        again, _ = webui.resolve_api_key("", data_dir)
        assert again == key, "重启后必须复用同一份密钥，不能每次启动都换"


# ---------------------------------------------------------------------------
# 面板：鉴权与接口
# ---------------------------------------------------------------------------


def test_webui_requires_key_and_serves_pages() -> None:
    """没带密钥必须 401；带对了才给页面与接口；密钥错了也不能放行。"""

    webui = _load("webui")
    secret = "k" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        server, port = _start_panel(webui, plugin, secret)
        try:
            # 1) 没带密钥：接口 401、页面也 401（面板能看到卡组池与日志，不能默认放行）
            status, _body, _headers = _http(port, "GET", "/api/status")
            assert status == 401, status
            status, _body, _headers = _http(port, "GET", "/")
            assert status == 401, status
            # 2) 密钥错了：401，而且登录页要给"密钥不正确"这句
            status, body, _headers = _http(
                port,
                "POST",
                "/login",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=b"key=wrong",
            )
            assert status == 401, status
            assert "不正确" in body, body[:200]
            # 3) 请求头带对了：接口可用
            headers = {"X-API-Key": secret}
            status, body, _headers = _http(port, "GET", "/api/status", headers=headers)
            assert status == 200, (status, body[:200])
            payload = json.loads(body)
            assert payload["ok"] is True
            assert payload["webui"]["port"] == port
            assert "training" in payload
            # 4) query 带对了：换成 cookie 并跳回首页（用户在浏览器里贴一次密钥就能用）
            status, _body, headers_back = _http(port, "GET", f"/?key={secret}")
            assert status == 302, status
            assert "Set-Cookie" in headers_back, headers_back
            # 5) 各接口都能出数据（卡组池是空的也不能报错）
            # （原来这里还有 `/api/config`：配置页因 bug 太多删掉了，接口一并去掉）
            for path in ("/api/decks", "/api/training", "/api/rooms", "/api/logs"):
                status, body, _headers = _http(port, "GET", path, headers=headers)
                assert status == 200, (path, status, body[:200])
                assert json.loads(body)["ok"] is True, (path, body[:200])
            # 6) 不认识的接口要 404，而不是静默返回首页
            status, _body, _headers = _http(port, "GET", "/api/nope", headers=headers)
            assert status == 404, status
        finally:
            server.stop_now()

        # 停掉之后端口必须真的释放（否则插件一重载就"端口被占用"，只有重启机器能救）
        try:
            status, _body, _headers = _http(port, "GET", "/api/status", headers={"X-API-Key": secret})
        except OSError:
            released = True
        else:
            released = status >= 400
        assert released, "面板停了却还答得上话"


def test_webui_port_conflict_reports_instead_of_raising() -> None:
    """端口被占用时 `start()` 返回 False 并记日志——不能把异常抛进插件加载路径。"""

    webui = _load("webui")
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        first, port = _start_panel(webui, plugin, "a" * 32)
        try:
            second = webui.WebUIServer(
                plugin, host=_LOOPBACK, port=port, api_key="b" * 32, key_source="t"
            )
            assert second.start() is False, "同一个端口应该绑不上"
        finally:
            first.stop_now()


def test_webui_training_endpoints_reach_the_plugin() -> None:
    """训练接口把参数透到插件上（面板只负责收参数，真正的活儿在插件的执行器里）。"""

    webui = _load("webui")
    store_module = _load("train.store")
    secret = "z" * 32
    with tempfile.TemporaryDirectory() as directory:
        plugin = StubPlugin(Path(directory))
        # 挂一个**真的**记录库（不是替身）：列表页与详情页读的就是它，
        # 详情页还要把训练日志的尾巴读出来（这里最容易写错——第一版把"日志目录"
        # 和"日志文件"弄反了，表现是详情页永远显示"（没有输出）"）
        store = store_module.TrainingStore(Path(directory) / "train" / "training.db")
        log_path = Path(directory) / "train" / "logs" / "20261008-120000-script.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("#95 杀调：卡表 36 种\n体检 3 副：0 处需要看的登记问题\n", encoding="utf-8")
        finished = store.create("script", "脚本预校验：95", {"deck_ids": [95]}, log_path)
        store.finish(finished.run_id, store_module.STATUS_DONE, summary={"exit_code": 0})
        plugin._store = store

        server, port = _start_panel(webui, plugin, secret)
        headers = {"X-API-Key": secret, "Content-Type": "application/json"}
        try:
            status, body, _headers = _http(port, "GET", "/api/training", headers=headers)
            assert status == 200, (status, body[:200])
            listing = json.loads(body)
            assert [item["run_id"] for item in listing["runs"]] == [finished.run_id]
            assert listing["runs"][0]["status"] == "done"

            status, body, _headers = _http(
                port, "GET", f"/api/training/run/{finished.run_id}", headers=headers
            )
            assert status == 200, (status, body[:200])
            detail = json.loads(body)["run"]
            assert detail["summary"] == {"exit_code": 0}
            assert any("卡表 36 种" in line for line in detail["tail"]), detail["tail"]

            status, body, _headers = _http(port, "GET", "/api/training/run/nope", headers=headers)
            assert json.loads(body)["ok"] is False, body[:200]

            status, body, _headers = _http(
                port,
                "POST",
                "/api/training/start",
                headers=headers,
                body=json.dumps({"kind": "combo", "deck_id": 7}).encode("utf-8"),
            )
            assert status == 200, (status, body[:200])
            assert json.loads(body)["run"]["kind"] == "combo"
            assert plugin.started == [{"kind": "combo", "params": {"deck_id": 7}}], plugin.started

            status, _body, _headers = _http(port, "POST", "/api/training/stop", headers=headers, body=b"{}")
            assert status == 200, status
            assert plugin.stopped == 1

            # 没带密钥的 POST 不能执行任何动作
            status, _body, _headers = _http(
                port,
                "POST",
                "/api/training/start",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"kind": "combo"}).encode("utf-8"),
            )
            assert status == 401, status
            assert len(plugin.started) == 1, "未授权的请求不该起任务"
        finally:
            server.stop_now()


# ---------------------------------------------------------------------------
# 训练记录库
# ---------------------------------------------------------------------------


def test_training_store_lifecycle() -> None:
    """记录库：建 → 收尾 → 查；重载时把"还写着跑着"的旧记录改成失败。"""

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        store = store_module.TrainingStore(Path(directory) / "training.db")
        run = store.create("combo", "推演测试", {"deck_id": 1}, Path(directory) / "a.log")
        assert run.status == store_module.STATUS_RUNNING
        store.finish(run.run_id, store_module.STATUS_DONE, summary={"lines": 2})
        saved = store.get(run.run_id)
        assert saved is not None and saved.status == store_module.STATUS_DONE
        assert saved.summary == {"lines": 2}
        assert saved.duration_seconds is not None and saved.duration_seconds >= 0
        assert store.counts() == {store_module.STATUS_DONE: 1}
        assert store.newest_running() is None

        # 模拟"插件被重载、库里留着 running"
        stale = store.create("arena", "半截任务", {}, Path(directory) / "b.log")
        assert store.newest_running() is not None
        assert store.get(stale.run_id).status == store_module.STATUS_RUNNING
        changed = store.mark_interrupted()
        assert changed == 1, changed
        revived = store.get(stale.run_id)
        assert revived.status == store_module.STATUS_FAILED
        assert "中断" in revived.error, revived.error
        # 已经收尾的记录不能被改
        assert store.get(run.run_id).status == store_module.STATUS_DONE


def test_kill_tree_uses_taskkill_with_tree_flag() -> None:
    """杀进程树必须带 `/T`（只杀顶层会留下孤儿内核占端口）。

    Windows 上锁死这条命令的形状；其它平台上这个语义做不到，函数**明确返回空列表**
    （调用方据此退化成"只杀顶层"），这一条也要锁住——不然换个平台就悄悄少杀了一批进程。
    """

    runner_module = _load("train.runner")
    command = runner_module.kill_tree_command(4321)
    if sys.platform == "win32":
        assert command == ["taskkill", "/F", "/T", "/PID", "4321"], command
    else:
        assert command == [], command


# ---------------------------------------------------------------------------
# 卡的摘要与推演校验
# ---------------------------------------------------------------------------


def test_research_asks_the_online_model_and_keeps_its_material() -> None:
    """联网查资料：用**配置里那只联网模型**发出去，卡表进提示词，回复原文带回来。

    这条守住的是"写脚本能联网"这件事的两端：**发给谁**（model 参数要原样传下去，别被
    training_model 顶掉）和**问了什么**（卡表必须一起给，不然搜回来的资料与这副牌无关）。
    """

    analysis = _load("train.analysis")
    seen: Dict[str, str] = {}

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        seen["model"] = model
        seen["prompt"] = prompt
        seen["max_tokens"] = str(max_tokens)
        return "- 先手做 500 站场\n- 100 留到后手"

    result = asyncio.run(
        analysis.research_archetype(
            fake_generate,
            deck_name="异解",
            digest="# 卡组：异解\n- 100 测试怪兽｜效果：抽 1 张。",
            model="联网搜索",
            card_names=["测试怪兽", "另一张卡"],
            extra_prompt="重点看先手",
            logger=None,
        )
    )
    assert seen["model"] == "联网搜索", "必须发给配置里那只联网模型"
    assert "异解" in seen["prompt"] and "测试怪兽" in seen["prompt"], "卡表要一起给"
    # 卡组名常常只是社区译名（异解那副按名字问只回"没查到"），所以问句里要**点名卡名**，
    # 让模型在"按卡组名查不到"时改按卡名逐张查
    assert "主要卡片：测试怪兽、另一张卡" in seen["prompt"], seen["prompt"][-260:]
    assert "重点看先手" in seen["prompt"], "作者的要求要带上"
    assert int(seen["max_tokens"]) > 0
    assert result.text.startswith("- 先手做 500"), result.text
    assert result.model == "联网搜索"


def test_empty_research_reply_is_an_error() -> None:
    """联网模型回空串必须报错：调用方靠它区分"没配/查失败"与"查到了"，不能自己编资料。"""

    analysis = _load("train.analysis")

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        del prompt, model, max_tokens
        return ""

    try:
        asyncio.run(
            analysis.research_archetype(
                fake_generate, deck_name="x", digest="d", model="联网搜索", logger=None
            )
        )
    except analysis.AnalysisError as exc:
        assert "空内容" in str(exc), exc
    else:
        raise AssertionError("空回复应该抛 AnalysisError")


def _tool_call(call_id: str, query: str) -> Dict[str, Any]:
    """造一个宿主形状的 tool_call（`generate_with_tools` 返回里的那一截）。"""

    return {
        "id": call_id,
        "function": {"name": "web_search", "arguments": {"query": query}},
        "extra_content": {"tool_call_source": "response"},
    }


def test_search_agent_loops_until_it_has_enough() -> None:
    """检索 agent：模型自己决定查什么、查几次，最后一轮收口拿正文。

    这条锁住三件事：①工具定义真的传下去了（`tools` 非空才算"给了联网工具"）；
    ②检索词按顺序回灌给模型（工具结果里的原文要能被模型看见）；③模型不再调工具时立刻收手。
    """

    analysis = _load("train.analysis")
    turns: List[Dict[str, Any]] = []
    searched: List[str] = []

    async def fake_generate_with_tools(messages: Any, model: str, max_tokens: int, tools: Any) -> Dict[str, Any]:
        turns.append({"model": model, "tools": list(tools), "last": messages[-1], "count": len(messages)})
        if len(turns) == 1:
            return {"success": True, "response": "", "tool_calls": [_tool_call("c1", "码丽丝 先手 combo")]}
        if len(turns) == 2:
            return {"success": True, "response": "", "tool_calls": [_tool_call("c2", "码丽丝 白兔 效果")]}
        return {"success": True, "response": "- 先手做白兔检索\n- 龙王压场", "tool_calls": []}

    async def fake_search(query: str) -> str:
        searched.append(query)
        return f"检索结果：{query} 的要点……"

    result = asyncio.run(
        analysis.research_with_search(
            fake_generate_with_tools,
            fake_search,
            deck_name="码丽丝",
            digest="# 卡组：码丽丝\n- 100 白兔｜效果：检索。",
            model="deepseek-flash",
            card_names=["白兔"],
            max_rounds=4,
            logger=None,
        )
    )
    assert len(turns) == 3, f"应该查两轮、第三轮收口，实际 {len(turns)} 轮"
    assert turns[0]["tools"] and turns[0]["tools"][0]["function"]["name"] == "web_search", "第一轮就要给工具"
    assert turns[2]["tools"], "还没到轮数上限，工具不该被提前收回"
    assert len(turns) == 3, f"模型不再调工具时就该收手（上限 4 轮，实际 {len(turns)} 轮）"
    assert searched == ["码丽丝 先手 combo", "码丽丝 白兔 效果"], searched
    assert result.queries == searched, result.queries
    # 第二轮的消息里要能看到"第一轮查到了什么"（工具结果回灌）
    assert "检索结果：码丽丝 先手 combo" in json.dumps(turns[1]["last"], ensure_ascii=False)
    assert "先手做白兔检索" in result.text
    assert "码丽丝 先手 combo" in result.transcript


def test_search_agent_survives_a_failed_query_and_stops_at_the_round_cap() -> None:
    """单次检索失败只记一条"检索失败"，且轮数用尽时必须收口（不能让写脚本永远挂着）。"""

    analysis = _load("train.analysis")
    rounds: List[int] = []

    async def always_calls_tools(messages: Any, model: str, max_tokens: int, tools: Any) -> Dict[str, Any]:
        rounds.append(len(tools))
        if not tools:
            return {"success": True, "response": "（收口正文）没查到可用资料。", "tool_calls": []}
        return {"success": True, "response": "", "tool_calls": [_tool_call(f"c{len(rounds)}", "随便查点")]}

    async def broken_search(query: str) -> str:
        raise RuntimeError("搜索 provider 欠费")

    result = asyncio.run(
        analysis.research_with_search(
            always_calls_tools,
            broken_search,
            deck_name="x",
            digest="d",
            model="",
            max_rounds=2,
            logger=None,
        )
    )
    assert rounds == [1, 0], f"两轮：第一轮给工具、第二轮收口，实际 {rounds}"
    assert result.text.startswith("（收口正文）"), result.text
    assert "失败" in result.transcript, result.transcript


async def _runner_uses_the_search_agent_only_when_the_host_offers_tools() -> None:
    """runner 的两条路：有 `generate_with_tools` 就走 agent（用写作模型跑）、没有就退回单次问联网模型。"""

    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = _load("train.store").TrainingStore(tmp / "training.db")
        deck_path = tmp / "测试牌.ydk"
        _write_deck(deck_path, [101, 102])
        params = {"deck_id": 0, "deck_name": "测试牌", "deck_file": str(deck_path)}
        calls: List[str] = []

        async def fake_tools(messages: Any, model: str, max_tokens: int, tools: Any) -> Dict[str, Any]:
            calls.append(f"tools:{model}")
            return {"success": True, "response": "- 资料正文", "tool_calls": []}

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            calls.append(f"single:{model}")
            return "- 单次资料正文"

        runner = _make_runner(tmp, store, generate=fake_generate)
        runner.set_search_model("联网搜索")
        runner.set_tools_generate(fake_tools)
        assert "资料正文" in await runner._derive_research_for(params)
        # agent 那一路跑的是**写作模型**（`test-model`），联网由 web_search 背后的联网模型负责
        assert calls == ["tools:test-model"], calls

        # 宿主没给工具出口（旧宿主）→ 退回单次问那只联网模型
        calls.clear()
        runner.set_tools_generate(None)
        assert "单次资料正文" in await runner._derive_research_for(params)
        assert calls == ["single:联网搜索"], calls

        # 关掉检索轮数（0）→ 也走单次那条路
        calls.clear()
        runner.set_tools_generate(fake_tools)
        runner.set_search_rounds(0)
        await runner._derive_research_for(params)
        assert calls == ["single:联网搜索"], calls

        # 完全不联网（search_model 空）→ 一次模型调用都不该发生，且要**清掉**上一次的检索记录
        # （留着会让面板显示"这一版查了 6 次"，其实一次没查——实测被自己误导过）
        calls.clear()
        runner._research_queries = ["上一版的查询"]
        runner.set_search_model("")
        assert await runner._derive_research_for(params) == ""
        assert calls == [], calls
        assert runner._research_queries == [], runner._research_queries


def test_search_agent_keeps_the_raw_results_even_if_the_summary_talks_past_them() -> None:
    """模型"不认"检索结果时，原文也必须留住（本机真实翻车现场）。

    真机现象：agent 查了 6 次、拿到了真 combo（"睡鼠单卡可展开达成双 LINK5+红心+防火龙+07"），
    但它写出来的 2184 字资料开头是"以下内容基于我对该系列的既有知识整理，**未经过本次联网核对**"
    ——全按记忆写。要是资料只取它那段正文，检索到的事实就白丢了，所以原文必须跟着一起给写手。
    """

    analysis = _load("train.analysis")

    async def fake_generate_with_tools(messages: Any, model: str, max_tokens: int, tools: Any) -> Dict[str, Any]:
        if tools:
            return {"success": True, "response": "", "tool_calls": [_tool_call("c1", "码丽丝 睡鼠 单卡 combo")]}
        return {"success": True, "response": "（按记忆写的资料，未经过联网核对）", "tool_calls": []}

    async def fake_search(query: str) -> str:
        del query
        return "- 码丽丝<兵卒>睡鼠单卡可展开达成双 LINK5+红心+防火龙+07，并抽4丢1。"

    result = asyncio.run(
        analysis.research_with_search(
            fake_generate_with_tools,
            fake_search,
            deck_name="码丽丝",
            digest="d",
            model="deepseek-flash",
            max_rounds=2,
            logger=None,
        )
    )
    assert "未经过联网核对" in result.text, result.text[:200]
    assert "【web_search 的原始结果（未经模型改写）】" in result.text, "检索原文必须附在资料里"
    assert "睡鼠单卡可展开达成双 LINK5" in result.text, result.text[-300:]


def test_script_digest_shows_order_missing_hints_and_rule_names() -> None:
    """脚本结构摘要必须把"排查时要看的"排在前面：注册顺序、HintMsg 分支、函数名。

    这条是照 2026-10-09 那次真机排查定的：当时要定位"缺一个 `HintMsg.Target` 分支"和
    "某条闸门因为 desc=-1 成了死代码"，全靠这三样。**头部说明排在最后**——它上千字，
    放前面会把前两样挤出预算（第一版就这么写坏了）。
    """

    review = _load("train.review_material")
    source = (
        "/// <summary>\n"
        + "".join(f"/// 头部说明第 {index} 行：一大段设计意图……\n" for index in range(1, 41))
        + "/// </summary>\n"
        "/// </summary>\n"
        "[Deck(\"Demo\", \"AI_Demo\")]\n"
        "public class DemoExecutor : DoEverythingExecutor\n"
        "{\n"
        "    public new class CardId\n"
        "    {\n"
        "        public const int Alpha = 111;   // 甲卡\n"
        "    }\n"
        "    public DemoExecutor(GameAI ai, Duel duel) : base(ai, duel)\n"
        "    {\n"
        "        AddExecutor(ExecutorType.SummonOrSet, CardId.Alpha, SummonAlpha);\n"
        "        Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, Lethal));\n"
        "    }\n"
        "    public override IList<ClientCard> OnSelectCard(IList<ClientCard> cards, int min, int max, int hint, bool cancelable)\n"
        "    {\n"
        "        switch (hint)\n"
        "        {\n"
        "            case HintMsg.Discard:\n"
        "                return PickDiscard(cards);\n"
        "            case HintMsg.Equip:\n"
        "                return PickEquip(cards);\n"
        "        }\n"
        "        return null;\n"
        "    }\n"
        "    private bool SummonAlpha() { return true; }\n"
        "    private bool Lethal() { return false; }\n"
        "}\n"
    )
    digest = review.script_digest(source)
    for needle in (
        "AddExecutor(ExecutorType.SummonOrSet, CardId.Alpha, SummonAlpha);",
        "Executors.Insert(0, new CardExecutor(ExecutorType.GoToBattlePhase, -1, Lethal));",
        "HintMsg.Discard",
        "HintMsg.Equip",
        "OnSelectCard()",
        "SummonAlpha()",
        "Alpha = 111",
        "[Deck(\"Demo\"",
    ):
        assert needle in digest, f"摘要里缺 {needle}"
    # 头部说明排在结构之后（预算被挤时先丢的是它）
    assert digest.index("注册顺序") < digest.index("头部说明")
    # 预算很小时：前面几段照旧，最后一段（头部说明）**部分截断**并写清少列了几项。
    # 不整段丢：整段丢了模型只能回"这里被截断、无法判断"——真机上就这么吐槽过一次。
    small = review.script_digest(source, max_chars=700)
    assert "AddExecutor(ExecutorType.SummonOrSet" in small, "规则顺序在预算紧张时也不能丢"
    assert "HintMsg.Discard" in small, "HintMsg 分支同理"
    assert "预算所限只列前" in small and "未列出" in small, small[-200:]


def test_decision_log_slice_keeps_actions_inside_the_window_only() -> None:
    """决策日志切片：只留这一局时间窗内的"动作行"，噪声（手牌 dump、血量、洗牌）丢掉。"""

    review = _load("train.review_material")
    raw = "\n".join(
        [
            json.dumps({"timestamp": "2026-10-09T11:52:00.000000Z", "event": "[WindBot] [26-10-09 19:52:00] 执行器：AI_Demo"}),
            json.dumps({"timestamp": "2026-10-09T11:52:01.000000Z", "event": "[WindBot] [26-10-09 19:52:01] (0 's 甲卡 activate effect from MonsterZone)"}),
            json.dumps({"timestamp": "2026-10-09T11:52:01.500000Z", "event": "[WindBot] [26-10-09 19:52:01] *********Bot Hand*********"}),
            json.dumps({"timestamp": "2026-10-09T11:52:02.000000Z", "event": "[WindBot] [26-10-09 19:52:02] (0 got damage , LifePoint left = 7000)"}),
            json.dumps({"timestamp": "2026-10-09T11:52:03.000000Z", "event": "[WindBot] [26-10-09 19:52:03] (Grave 's 乙卡 become target)"}),
            json.dumps({"timestamp": "2026-10-09T12:30:00.000000Z", "event": "[WindBot] [26-10-09 20:30:00] (0 's 丙卡 activate effect from SpellZone)"}),
        ]
    )
    entries = review.parse_host_log(raw)
    # 解析阶段只做"能不能读"，窗口与过滤在切片那一步
    assert len(entries) == 6, entries
    text = review.decision_log_slice(entries, start=entries[0][0] - 1, end=entries[0][0] + 5)
    assert "执行器：AI_Demo" in text
    assert "甲卡 activate effect" in text
    assert "乙卡 become target" in text
    assert "Bot Hand" not in text and "LifePoint" not in text
    assert "丙卡" not in text, "时间窗外的行不该出现"


def test_patch_parsing_and_all_or_nothing_apply() -> None:
    """补丁：解析格式 → 校验（原文必须恰好一处）→ 全有或全无地落地（带备份）。

    这条守的是最危险的一步（复盘会改源码树的 C#）：宁可整批不落地并说清原因，
    也不能只改一半；编译不过时还要能回滚。
    """

    patchwork = _load("train.patchwork")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        decks = root / "Game" / "AI" / "Decks"
        decks.mkdir(parents=True)
        target = decks / "DemoExecutor.cs"
        original = "class Demo\n{\n    private bool A() { return false; }\n}\n"
        target.write_text(original, encoding="utf-8")

        answer = (
            "## 具体问题\n- 甲卡的条件写反了\n\n"
            "<<<PATCH\n"
            "文件: DemoExecutor.cs\n"
            "原因: 甲卡的条件写反了\n"
            "原文:\n"
            "    private bool A() { return false; }\n"
            "改成:\n"
            "    private bool A() { return true; }\n"
            "PATCH>>>\n"
        )
        patch_set = patchwork.parse_patches(answer)
        assert patch_set.ok, patch_set.problems
        assert patch_set.patches[0].file == "DemoExecutor.cs"

        result = patchwork.apply_patches(patch_set, root)
        assert result.ok, result.error
        assert result.applied == 1 and result.backups
        assert "return true" in target.read_text(encoding="utf-8")
        assert "return false" in Path(result.backups[0]).read_text(encoding="utf-8")

        # 原文在文件里出现两次 → 整批不落地（不猜改哪一处）
        twice = decks / "TwiceExecutor.cs"
        twice.write_text("x = 1;\nx = 1;\n", encoding="utf-8")
        bad = patchwork.parse_patches(
            "<<<PATCH\n文件: TwiceExecutor.cs\n原因: 试试\n原文:\nx = 1;\n改成:\nx = 2;\nPATCH>>>"
        )
        assert bad.ok
        failed = patchwork.apply_patches(bad, root)
        assert not failed.ok and "出现 2 次" in failed.error, failed.error
        assert twice.read_text(encoding="utf-8") == "x = 1;\nx = 1;\n"

        # 原文抄不准 → 拒绝，并指出是哪一条
        wrong = patchwork.parse_patches(
            "<<<PATCH\n文件: DemoExecutor.cs\n原因: 抄错了\n原文:\n    private bool A() { return TRUE; }\n改成:\n    private bool A() { return false; }\nPATCH>>>"
        )
        failed2 = patchwork.apply_patches(wrong, root)
        assert not failed2.ok and "一次都没出现" in failed2.error, failed2.error

        # 想改到源码树外面 → 直接拒绝
        outside = patchwork.parse_patches(
            "<<<PATCH\n文件: ../../../evil.cs\n原因: 越界\n原文:\nx\n改成:\ny\nPATCH>>>"
        )
        assert outside.ok
        assert not patchwork.apply_patches(outside, root).ok


async def _review_feeds_the_script_digest_and_that_duel_log() -> None:
    """复盘材料新增两样：脚本结构摘要 + 那一局的决策日志（没勾"落地"时**不动任何文件**）。"""

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")

        # 脚本源码放在"插件留档"那份里（源码树没配时的那条回退路）
        plugin_root = tmp / "plugin"
        (plugin_root / "executors").mkdir(parents=True)
        script_path = plugin_root / "executors" / "DemoExecutor.cs"
        script_path.write_text(
            "\n".join(
                [
                    '[Deck("Demo", "AI_Demo")]',
                    "public class DemoExecutor : DoEverythingExecutor",
                    "{",
                    "    public DemoExecutor(GameAI ai, Duel duel) : base(ai, duel)",
                    "    {",
                    "        AddExecutor(ExecutorType.Activate, CardId.Alpha, AlphaRule);",
                    "    }",
                    "    private bool AlphaRule() { return true; }",
                    "}",
                ]
            ) + "\n",
            encoding="utf-8",
        )

        # 一局对局记录（复盘读它）+ 一份含这一局决策行的宿主日志
        duel = store.create("duel", "房间对局：测试牌", {"deck_id": 7, "deck_name": "测试牌"}, tmp / "duel.log")
        store.finish(
            duel.run_id,
            "done",
            summary={
                "self_seat": 0,
                "winner_is_self": False,
                "turns": 3,
                "duration_seconds": 120,
                "players": {"0": {"name": "憨憨", "normal_summons": 1, "sp_summons": 3,
                                  "effects": 8, "attacks": 0, "damage_taken": 9200}},
                "top_cards": ["「甲卡」x2"],
            },
        )
        started = time.time()
        host_logs = tmp / "logs"
        host_logs.mkdir()

        def _stamp(offset: float) -> str:
            import datetime as _dt

            moment = _dt.datetime.fromtimestamp(started + offset, _dt.timezone.utc)
            return moment.isoformat().replace("+00:00", "Z")

        (host_logs / "app_test.log.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"timestamp": _stamp(1), "event": "[WindBot] [x] (0 's 甲卡 activate effect from Hand)"}),
                    json.dumps({"timestamp": _stamp(2), "event": "[WindBot] [x] (0 's 甲卡 from Hand move to SpellZone)"}),
                    json.dumps({"timestamp": _stamp(9999), "event": "[WindBot] [x] (0 's 别局的卡 activate effect from Hand)"}),
                ]
            ) + "\n",
            encoding="utf-8",
        )

        prompts: List[str] = []

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del model, max_tokens
            prompts.append(prompt)
            return "\n".join(
                [
                    "## 具体问题",
                    "- 甲卡的条件写反了",
                    "",
                    "<<<PATCH",
                    "文件: DemoExecutor.cs",
                    "原因: 甲卡条件写反",
                    "原文:",
                    "    private bool AlphaRule() { return true; }",
                    "改成:",
                    "    private bool AlphaRule() { return false; }",
                    "PATCH>>>",
                ]
            )

        runner = _make_runner(tmp, store, generate=fake_generate)
        # _make_runner 把 plugin_root 指到 tmp/plugin；把源码树关掉，走"插件留档"那条回退路
        runner._windbot_dirs = lambda: (None, None)  # type: ignore[method-assign]
        run = store.create(
            "review", "复盘优化：测试牌", {"deck_id": 7, "deck_name": "测试牌"}, tmp / "review.log"
        )
        await runner._run_review(
            run,
            {"deck_id": 7, "deck_name": "测试牌", "style": "Demo", "latest": 1},
        )
        prompt = prompts[0]
        assert "脚本结构摘要" in prompt, "提示词里没有结构摘要那一段"
        assert "AddExecutor(ExecutorType.Activate, CardId.Alpha, AlphaRule);" in prompt, "摘要没带规则顺序"
        assert "甲卡 activate effect" in prompt, "这一局的决策日志没进材料"
        assert "别局的卡" not in prompt, "时间窗外的日志不该进材料"
        assert "补丁怎么写" in prompt and "<<<PATCH" in prompt, "提示词里没教补丁格式"

        saved = store.get(run.run_id)
        assert saved is not None and saved.status == store_module.STATUS_DONE, saved and saved.error
        assert saved.summary["patches"], saved.summary
        assert not saved.summary["applied"], "没勾「落地」就不该写文件"
        assert saved.summary["apply_requested"] is False
        assert "return true" in script_path.read_text(encoding="utf-8"), "脚本必须原样不动"
        log_text = (tmp / "review.log").read_text(encoding="utf-8")
        assert "脚本结构摘要" in log_text and "补丁与落地结果" in log_text, log_text[-400:]


async def _review_apply_writes_backup_and_rolls_back_when_build_fails() -> None:
    """勾了"落地并编译"：写源码树（带备份）→ 编译；**编译不过就回滚**，别把树留在编不过的状态。

    三条分支都要锁住：
    1. 编译成功 → 文件变了、备份在、summary 记 applied/build_ok；
    2. 编译失败 → 文件**回到改前**、错误里写明"已回滚"；
    3. 有房间在打 → 补丁照样写（用户要的），但**不编译**并说明原因（正在跑的 WindBot 锁着 exe）。
    """

    store_module = _load("train.store")
    runner_module = _load("train.runner")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        source = tmp / "windbot-src"
        decks = source / "Game" / "AI" / "Decks"
        decks.mkdir(parents=True)
        (source / "WindBot.csproj").write_text("<Project />", encoding="utf-8")
        target = decks / "DemoExecutor.cs"
        original = "\n".join(
            [
                '[Deck("Demo", "AI_Demo")]',
                "public class DemoExecutor : DoEverythingExecutor",
                "{",
                "    private bool AlphaRule() { return true; }",
                "}",
            ]
        ) + "\n"
        target.write_text(original, encoding="utf-8")

        duel = store.create("duel", "房间对局：测试牌", {"deck_id": 7, "deck_name": "测试牌"}, tmp / "duel.log")
        store.finish(
            duel.run_id,
            "done",
            summary={"self_seat": 0, "winner_is_self": False, "turns": 3, "duration_seconds": 60,
                     "players": {"0": {"name": "憨憨", "sp_summons": 1}}},
        )

        answer = "\n".join(
            [
                "<<<PATCH",
                "文件: DemoExecutor.cs",
                "原因: 条件写反",
                "原文:",
                "    private bool AlphaRule() { return true; }",
                "改成:",
                "    private bool AlphaRule() { return false; }",
                "PATCH>>>",
            ]
        )

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del prompt, model, max_tokens
            return answer

        calls: List[str] = []

        async def ok_build(source_dir: Path, **kwargs: Any) -> Tuple[bool, str]:
            calls.append("build")
            return True, "Build succeeded"

        async def bad_build(source_dir: Path, **kwargs: Any) -> Tuple[bool, str]:
            calls.append("build")
            return False, "DemoExecutor.cs(4,42): error CS1061: 故意失败"

        original_build = runner_module.build_windbot
        try:
            # 1) 落地 + 编译成功
            runner_module.build_windbot = ok_build  # type: ignore[attr-defined]
            runner = _make_runner(tmp, store, generate=fake_generate)
            runner._windbot_dirs = lambda: (source, tmp / "windbot")  # type: ignore[method-assign]
            run = store.create("review", "复盘优化：测试牌", {"deck_id": 7, "deck_name": "测试牌", "apply": True}, tmp / "r1.log")
            await runner._run_review(
                run,
                {"deck_id": 7, "deck_name": "测试牌", "style": "Demo", "latest": 1, "apply": True},
            )
            saved = store.get(run.run_id)
            assert saved.summary["applied"] == 1, saved.summary
            assert saved.summary["build_ok"] is True, saved.summary
            assert "return false" in target.read_text(encoding="utf-8")
            backup = saved.summary["backups"][0]
            assert "return true" in Path(backup).read_text(encoding="utf-8"), "备份要留改前那份"

            # 2) 编译失败 → 回滚
            target.write_text(original, encoding="utf-8")
            runner_module.build_windbot = bad_build  # type: ignore[attr-defined]
            runner2 = _make_runner(tmp, store, generate=fake_generate)
            runner2._windbot_dirs = lambda: (source, tmp / "windbot")  # type: ignore[method-assign]
            run2 = store.create("review", "复盘优化：测试牌", {"deck_id": 7, "deck_name": "测试牌", "apply": True}, tmp / "r2.log")
            await runner2._run_review(
                run2,
                {"deck_id": 7, "deck_name": "测试牌", "style": "Demo", "latest": 1, "apply": True},
            )
            saved2 = store.get(run2.run_id)
            assert "已把补丁回滚" in saved2.summary["apply_error"], saved2.summary["apply_error"]
            assert target.read_text(encoding="utf-8") == original, "编译不过必须回到改前"

            # 3) 有房间在打 → 写文件但不编译
            target.write_text(original, encoding="utf-8")
            calls.clear()
            runner_module.build_windbot = ok_build  # type: ignore[attr-defined]
            runner3 = _make_runner(tmp, store, generate=fake_generate, rooms=1)
            runner3._windbot_dirs = lambda: (source, tmp / "windbot")  # type: ignore[method-assign]
            run3 = store.create("review", "复盘优化：测试牌", {"deck_id": 7, "deck_name": "测试牌", "apply": True}, tmp / "r3.log")
            await runner3._run_review(
                run3,
                {"deck_id": 7, "deck_name": "测试牌", "style": "Demo", "latest": 1, "apply": True},
            )
            saved3 = store.get(run3.run_id)
            assert saved3.summary["applied"] == 1, saved3.summary
            assert calls == [], "有房间在打时不该去编译"
            assert "没有编译" in saved3.summary["build_tail"], saved3.summary["build_tail"]
        finally:
            runner_module.build_windbot = original_build  # type: ignore[attr-defined]



def _first_line_stuck_in_string(script: str) -> Optional[int]:
    """扫一遍 JS，返回第一处"这一行结束时还停在单/双引号字符串里"的行号。

    模板串（反引号）允许跨行，所以不算；`//` 行注释与 `/* */` 块注释跳过。
    这个检查专治一类**Python 看不出来的错**：页面里的 JS 是被 Python 字符串求值过的，
    JS 想写 `\n` 就得在源码里写两个反斜杠；写成一个会被 Python 先吃成真换行，
    JS 字符串跨行 → 整页脚本语法错误（面板"载入不进去"）。
    """

    in_single = in_double = in_template = in_block_comment = escaped = False
    previous = ""
    for line_no, line in enumerate(script.splitlines(), start=1):
        index = 0
        while index < len(line):
            char = line[index]
            nxt = line[index + 1] if index + 1 < len(line) else ""
            if in_block_comment:
                if char == "*" and nxt == "/":
                    in_block_comment = False
                    index += 2
                    continue
            elif escaped:
                escaped = False
            elif in_single:
                if char == "\\":
                    escaped = True
                elif char == "'":
                    in_single = False
            elif in_double:
                if char == "\\":
                    escaped = True
                elif char == '"':
                    in_double = False
            elif in_template:
                if char == "\\":
                    escaped = True
                elif char == "`":
                    in_template = False
            else:
                if char == "/" and nxt == "/":
                    break                      # 行注释：这一行余下不用看
                if char == "/" and nxt == "*":
                    in_block_comment = True
                    index += 2
                    continue
                if char == "/" and (previous == "" or previous in "(,=:[!&|?{};+-*%~^"):
                    # 正则字面量（`/[&<>"']/g` 这种）：里面的引号不算字符串，跳到它结尾
                    index += 1
                    in_class = False
                    while index < len(line):
                        inner = line[index]
                        if inner == "\\":
                            index += 2
                            continue
                        if inner == "[":
                            in_class = True
                        elif inner == "]":
                            in_class = False
                        elif inner == "/" and not in_class:
                            break
                        index += 1
                    previous = "/"
                    index += 1
                    continue
                if char == "'":
                    in_single = True
                elif char == '"':
                    in_double = True
                elif char == "`":
                    in_template = True
            if not char.isspace():
                previous = char
            index += 1
        if in_single or in_double:
            return line_no
    return None

def test_duel_page_slots_hide_stats_outside_monster_zones_and_show_face_down() -> None:
    """对局页的格子：**只有怪兽区**给攻守；**盖卡必须画出来**（用户报的两条）。

    2026-10-09 用户报："魔法陷阱卡和放在灵摆区的怪兽以及放置在魔陷区的怪兽也有显示攻击力这些数值，
    而且盖卡在这里不显示。"——前者是 `_slot_json` 对所有格位都发 atk/def_；后者是
    `card_id` 为 0（对手盖卡内核不报卡号）被当成"空格"返回了 None。
    """

    webui = _load("webui")
    from duel.fieldstate import ZoneCard  # noqa: PLC0415  测试内导入，避免影响别的用例

    class Detail:
        name = "测试怪兽"
        atk = 1800
        defense = 1300
        level = 4
        type_text = "怪兽 效果"

    details = {999: Detail()}
    names = {999: "测试怪兽"}

    # ① 怪兽区：给攻守（内核当前值优先）
    monster = webui._slot_json(
        ZoneCard(999, 0x1, attack=2600, defense=1300), names=names, details=details, monster_zone=True
    )
    assert monster and monster["atk"] == 2600 and monster["def_"] == 1300 and monster["stats_live"], monster

    # ② 魔陷区（含灵摆区 / 当装备用的怪兽）：不给攻守
    equip = webui._slot_json(
        ZoneCard(999, 0x1, attack=1200, defense=0), names=names, details=details, monster_zone=False
    )
    assert equip and "atk" not in equip and "def_" not in equip, equip
    assert equip["name"] == "测试怪兽", "名字还是要给（贴纸/装备都要看得见）"

    # ③ 盖卡：内核不报卡号（card_id=0）也必须返回一格"里侧"，不能当成空格
    hidden = webui._slot_json(ZoneCard(0, 0x8), names=names, details=details, monster_zone=True)
    assert hidden is not None, "盖卡不能消失"
    assert hidden["face_up"] is False and hidden["id"] == 0 and hidden["name"] == "", hidden
    assert hidden["attack"] is False, "0x8 = 里侧守备"

    # ④ 空格才是 None
    assert webui._slot_json(None, names=names, details=details, monster_zone=True) is None



def test_page_js_has_no_bare_newline_inside_strings() -> None:
    """面板的 JS 不许出现"行尾停在字符串里"——那说明转义被 Python 吃掉了。

    回归测试（2026-10-09 线上炸过）：`join("

")` 在源码里写成单反斜杠 →
    Python 求值成真换行 → JS 字符串跨行 → **整个面板脚本语法错误、载入不进去**。
    Python 自己的语法检查完全看不出这类错，所以这里对**求值后**的页面做一次扫描。
    """

    webui = _load("webui")
    page = webui._app_page()
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, re.S)
    assert scripts, "页面里应该有内联脚本"
    for script in scripts:
        stuck = _first_line_stuck_in_string(script)
        assert stuck is None, f"JS 第 {stuck} 行结束时还停在字符串里（多半是转义被 Python 吃掉了）"
    # 顺带钉住那条具体写法：求值后必须是字面量的「反斜杠 + n」，不是真换行。
    # 锚在 `const reply = [resultText` 那句上：页面里另有一处 `.filter(...).join("　")`，
    # 直接找 join 会抓错地方（第一版就是这么误报的）。
    reply_line = page[page.index("const reply = [resultText") :][:220]
    piece = reply_line[reply_line.index('.join("') + len('.join("') :][:4]
    assert piece == "\\n\\n", f"求值后应当是字面量（反斜杠 + n）两遍，实际是 {piece!r}"

def test_training_tasks_end_with_a_plain_text_result() -> None:
    """四类训练任务结束时都要落一段**明文结果**（用户口径："结束后给我一个明文的结果"）。

    数字从工具输出/记录字段里抄（确定性），模型的结论另存 `conclusion`——模型失败时
    用户也还能看到"打了多少局、谁赢多少"。
    """

    runner = _load("train.runner")
    arena = """
A = Gen103（日志名 AI_Gen103）｜B = Test（日志名 AI_Test）
[1/40] A 在 bot ｜胜者=A

================ 汇总 ================
局数：40
算进胜率：40
平局：0
没判出胜负：0
Gen103 赢：10
Test 赢：30
Gen103 胜率：25.0%
95%区间：14.2% ~ 40.2%
两侧脚本核对不通过的局数：0
"""
    text = runner.arena_result_text(arena)
    for needle in ("Gen103 10 胜 : Test 30 胜", "胜率 25.0%", "14.2% ~ 40.2%", "共 40 局", "核对通过"):
        assert needle in text, f"{needle} 不在：{text}"
    assert "没解析出统计" in runner.arena_result_text("（工具没输出）")

    write = runner.write_script_result_text(
        {"deck_name": "异解", "style_name": "Gen103", "lines": 1722, "handlers": 39,
         "attempts": 3, "research_model": "联网搜索", "research_chars": 4818, "research_queries": ["a", "b"],
         "warnings": ["这一版是在已有脚本基础上改的"]}
    )
    for needle in ("异解 → Gen103", "1722 行 / 39 个处理函数", "第 3 轮编译通过", "联网资料 4818 字（2 次检索）"):
        assert needle in write, write

    iterate = runner.iterate_result_text(
        [
            {"round": 1, "style_name": "Gen38", "script_lines": 100, "script_handlers": 5, "compared": False},
            {"round": 2, "style_name": "Gen38", "script_lines": 120, "script_handlers": 6, "compared": True,
             "arena_result": "擂台对打｜Gen38 8 胜 : Test 12 胜｜共 20 局"},
        ],
        "Test",
    )
    assert "自动迭代 2 轮（对手脚本 Test）" in iterate and "8 胜 : Test 12 胜" in iterate, iterate
    assert "末轮脚本 Gen38（120 行 / 6 个处理函数）" in iterate, iterate

    review = runner.review_result_text(
        {"duels": 3, "script_chars": 81688, "digest_chars": 9898, "log_lines": 122,
         "patches": ["a：b"], "applied": 1, "build_ok": True}
    )
    for needle in ("读了 3 局", "决策日志 122 行", "给出 1 个补丁", "已落地 1 个并编译通过"):
        assert needle in review, review


def test_deck_digest_and_combo_check_catch_cards_outside_the_deck() -> None:
    """模型提到的卡必须真在卡表里：不在的从结构化字段里剔掉，并留下 warning。"""

    analysis = _load("train.analysis")
    card_db = FakeCardDb({101: "灰流丽", 102: "增殖的G", 103: "墓穴的指名者"})
    digest, names, warnings = analysis.build_deck_digest(
        "测试牌", [101, 102, 103, 999], card_db, group_counts={"主卡组": 40, "额外卡组": 0}
    )
    assert names == {"灰流丽", "增殖的G", "墓穴的指名者"}
    assert "灰流丽" in digest and "效果：" in digest
    assert any("999" in warn for warn in warnings), warnings  # 卡库不认识的卡要报出来

    reply = json.dumps(
        {
            "summary": "手坑压制",
            "lines": [
                {
                    "name": "先手主线",
                    "hand": ["灰流丽", "不存在的卡"],
                    "cards": ["增殖的G", "虚构龙"],
                    "steps": ["通召不存在的卡"],
                }
            ],
            "notes": [],
            "uncertain": [],
        },
        ensure_ascii=False,
    )

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        assert "灰流丽" in prompt, "卡文必须喂给模型（不能让它凭记忆写效果）"
        del prompt, model, max_tokens
        return reply

    result = asyncio.run(
        analysis.derive_combo(
            fake_generate,
            deck_name="测试牌",
            digest=digest,
            known_names=names,
            model="test-model",
            logger=None,
        )
    )
    line = result.guide["lines"][0]
    assert line["hand"] == ["灰流丽"], line["hand"]
    assert line["cards"] == ["增殖的G"], line["cards"]
    assert any("不存在的卡" in warn for warn in result.warnings), result.warnings
    assert any("虚构龙" in warn for warn in result.warnings), result.warnings


def test_combo_tolerates_non_json_reply_but_says_so() -> None:
    """模型没按 JSON 输出时：原文存档 + 明说"没有结构化字段可校验"，不能假装校验过了。"""

    analysis = _load("train.analysis")

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        del prompt, model, max_tokens
        return "先通召灰流丽，然后……（这是一段散文，不是 JSON）"

    result = asyncio.run(
        analysis.derive_combo(
            fake_generate, deck_name="x", digest="d", known_names={"灰流丽"}, model="", logger=None
        )
    )
    assert result.guide["lines"] == []
    assert any("JSON" in warn for warn in result.warnings), result.warnings
    assert "散文" in result.text()


def test_empty_model_reply_is_an_error_not_an_empty_guide() -> None:
    """模型返回空串必须报错（额度被思考吃光是本机踩过的坑），不能生成一份空推演。"""

    analysis = _load("train.analysis")

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        del prompt, model, max_tokens
        return "   "

    try:
        asyncio.run(
            analysis.derive_combo(
                fake_generate, deck_name="x", digest="d", known_names=set(), model="", logger=None
            )
        )
    except analysis.AnalysisError as exc:
        assert "空内容" in str(exc), exc
    else:
        raise AssertionError("空回复应该抛 AnalysisError")


def test_timeout_failure_says_what_to_change() -> None:
    """失败信息要带"该改哪里"：宿主对插件的单次调用有 30 秒硬超时，光看超时不知道怎么办。

    这条是**实测**出来的：真机上跑 combo 推演，第一次就撞上
    `[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`——模型那只（`ds`）会思考，一次要 8000 token
    的答复光"想"就超 30 秒。提示必须指向"换一只不思考的模型"这个动作，否则用户只能干瞪眼。
    """

    analysis = _load("train.analysis")
    hint = analysis.failure_hint("[E_TIMEOUT] 请求 cap.call 超时 (30000ms)")
    assert "30 秒" in hint and "deepseek-flash" in hint, hint
    assert analysis.failure_hint("别的问题") == ""

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        del prompt, model, max_tokens
        raise RuntimeError("[E_TIMEOUT] 请求 cap.call 超时 (30000ms)")

    try:
        asyncio.run(
            analysis.derive_combo(
                fake_generate, deck_name="x", digest="d", known_names=set(), model="", logger=None
            )
        )
    except analysis.AnalysisError as exc:
        assert "30 秒" in str(exc) and "deepseek-flash" in str(exc), exc
    else:
        raise AssertionError("超时应该抛 AnalysisError")


def test_combo_keeps_the_main_lines_when_the_notes_round_fails() -> None:
    """推演分两轮问；第二轮（要点）失败不该把第一轮的主线一起丢掉。

    为什么要分两轮：宿主单次调用 30 秒上限，一轮里要 8000 token 的答复必然超时。
    主线是真正有用的那半，要点是补充——所以第二轮失败只记一条 warning。
    """

    analysis = _load("train.analysis")
    calls: List[str] = []

    async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
        del model, max_tokens
        calls.append(prompt)
        if len(calls) == 1:
            assert "主线" in prompt, prompt[:120]
            return json.dumps(
                {
                    "summary": "以墓地资源滚起来",
                    "lines": [
                        {"name": "先手主线", "hand": ["灰流丽"], "cards": ["灰流丽"], "steps": ["停手"]}
                    ],
                },
                ensure_ascii=False,
            )
        assert "要点" in prompt, prompt[:120]
        raise RuntimeError("[E_TIMEOUT] 请求 cap.call 超时 (30000ms)")

    result = asyncio.run(
        analysis.derive_combo(
            fake_generate,
            deck_name="x",
            digest="d",
            known_names={"灰流丽"},
            model="",
            logger=None,
        )
    )
    assert len(calls) == 2, calls
    assert result.guide["lines"][0]["name"] == "先手主线"
    assert any("要点" in warn for warn in result.warnings), result.warnings
    assert "以墓地资源滚起来" in result.text()


# ---------------------------------------------------------------------------
# 训练执行器
# ---------------------------------------------------------------------------


async def _never_called(prompt: str, model: str, max_tokens: int) -> str:
    """默认的模型替身：不该被调用时被调用了，直接炸出来。"""

    raise AssertionError(f"这个用例不该问模型：{prompt[:60]}")


def _make_runner(tmp: Path, store: Any, *, generate: Any = None, rooms: int = 0) -> Any:
    """造一个跑在临时目录里的执行器（tools 目录由调用方自己摆假脚本）。"""

    runner_module = _load("train.runner")
    plugin_root = tmp / "plugin"
    (plugin_root / "tools").mkdir(parents=True, exist_ok=True)
    card_db = FakeCardDb({101: "灰流丽", 102: "增殖的G"})
    runner = runner_module.TrainingRunner(
        plugin_root=plugin_root,
        workspace=tmp / "train",
        deck_db_path=tmp / "deck_pool.db",
        store=store,
        generate=generate or _never_called,
        card_db=lambda: card_db,
        active_rooms=lambda: rooms,
        logger=None,
        max_duels=60,
        host_log_dir=tmp / "logs",
    )
    runner.set_training_model("test-model")
    return runner


def _write_deck(path: Path, card_ids: List[int]) -> None:
    """写一份最小 .ydk（`!side` 之前算主卡组）。"""

    path.write_text(
        "#created by tests\n#main\n" + "\n".join(str(card_id) for card_id in card_ids) + "\n!side\n",
        encoding="utf-8",
    )


def _seed_two_decks(tmp: Path, *, first_script: str = "GenA", second_script: str = "GenB") -> List[int]:
    """往临时卡组池里登记两副牌（各自带出牌脚本），返回编号。

    擂台与迭代现在选的是"另一副卡组"而不是"另一个脚本名"（2026-10-09 用户口径：
    一个卡组只有一个脚本），所以这两种任务必须有**两副都配了脚本的牌**才起得来。
    """

    deckpool = _load("duel.deckpool")
    pool = deckpool.DeckPool(tmp)
    try:
        deck_ids: List[int] = []
        for index, (name, script) in enumerate((("甲牌", first_script), ("乙牌", second_script))):
            ydk_path = tmp / f"seed{index}.ydk"
            _write_deck(ydk_path, [101 + index])
            stored = pool.add(
                group_id="111",
                display_name=name,
                contributor_id="u",
                contributor_name="群友",
                ydk_text=ydk_path.read_text(encoding="utf-8"),
                deck_code="",
                source_format="ydk",
                main_count=1,
                extra_count=0,
                side_count=0,
            )
            pool.set_generated_script(stored.deck_id, script)
            deck_ids.append(stored.deck_id)
        return deck_ids
    finally:
        # 一定要关：Windows 上没关连接，临时目录清理会报"文件正在使用"
        pool.close()


def _wait_for(predicate: Any, timeout: float = 15.0, interval: float = 0.2) -> bool:
    """等一个条件成立（同步版，用在没跑事件循环的等待里）。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


async def _combo_derivation_archives_the_guide() -> None:
    """combo 推演：读卡表 → 问模型 → 落盘。

    推演现在**不是**面板上的一个任务（2026-10-09 用户口径：只留卡组互打 / 编写脚本 /
    卡组迭代 / 复盘优化），它是「编写脚本」与「卡组迭代」的中间材料。所以这里直接走它
    真正被调用的那个入口（`_derive_combo_for`），测的还是那条链：卡表 → 卡文 → 模型 → 存档。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        deck_path = tmp / "测试牌.ydk"
        _write_deck(deck_path, [101, 101, 102])

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del max_tokens
            assert model == "test-model", model
            assert "增殖的G" in prompt
            return json.dumps(
                {
                    "summary": "先攻压制",
                    "lines": [
                        {"name": "先手", "hand": ["灰流丽"], "cards": ["灰流丽"], "steps": ["留手"]}
                    ],
                    "notes": [],
                    "uncertain": [],
                },
                ensure_ascii=False,
            )

        runner = _make_runner(tmp, store, generate=fake_generate)
        guide_text = await runner._derive_combo_for(
            {"deck_id": 0, "deck_name": "测试牌", "deck_file": str(deck_path)}
        )
        assert "先攻压制" in guide_text, guide_text[:200]
        archived = list(runner.combo_dir.glob("*.txt"))
        assert len(archived) == 1, archived
        assert "先攻压制" in archived[0].read_text(encoding="utf-8")
        assert not runner.busy


async def _research_goes_to_the_search_model_and_never_blocks_the_run() -> None:
    """联网查资料这条链的三件事：没配就**不问**、配了就**用那只模型**问并落盘、问失败**只记账不炸**。

    第三点是刻意的：搜索 provider 欠费/超时不该把整次写脚本拖没（资料只是参考），
    但也不许悄悄咽掉——失败原因要留在 `_research_error` 里给面板看。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        deck_path = tmp / "测试牌.ydk"
        _write_deck(deck_path, [101, 102])
        params = {"deck_id": 0, "deck_name": "测试牌", "deck_file": str(deck_path)}

        # 1) 没配联网模型：一次模型调用都不该发生（`_never_called` 会炸）
        lazy = _make_runner(tmp, store)
        assert await lazy._derive_research_for(params) == ""
        assert lazy.research_dir.is_dir(), "研究目录要在起 runner 时就建好"

        # 2) 配了：用**那只**模型问，资料落盘
        seen: List[str] = []

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del max_tokens
            seen.append(model)
            assert "增殖的G" in prompt, prompt[:120]
            return "- 先把 101 拍上去\n- 102 留着应对"

        runner = _make_runner(tmp, store, generate=fake_generate)
        runner.set_search_model("联网搜索")
        text = await runner._derive_research_for(params)
        assert seen == ["联网搜索"], seen
        assert "102 留着应对" in text
        assert runner._research_error == ""
        archived = list(runner.research_dir.glob("*-0-*.txt"))
        assert len(archived) == 1, archived
        assert "102 留着应对" in archived[0].read_text(encoding="utf-8")

        # 3) 联网那一侧挂了：返回空串（不炸）但把原因记下来
        async def failing_generate(prompt: str, model: str, max_tokens: int) -> str:
            del prompt, model, max_tokens
            raise RuntimeError("[E_TIMEOUT] 请求 cap.call 超时 (30000ms)")

        broken = _make_runner(tmp, store, generate=failing_generate)
        broken.set_search_model("联网搜索")
        assert await broken._derive_research_for(params) == ""
        assert "30 秒" in broken._research_error, broken._research_error


async def _unknown_deck_is_refused_before_a_record_exists() -> None:
    """卡组找不到时必须起不来（在"建记录"之前就拦掉，不留下半截任务）。"""

    store_module = _load("train.store")
    runner_module = _load("train.runner")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        runner = _make_runner(tmp, store)
        try:
            await runner.start("write_script", {"deck_id": 999})
        except runner_module.TrainingError as exc:
            assert "999" in str(exc), exc
        else:
            raise AssertionError("卡组不存在时不该起得来")
        assert store.list_recent() == [], "起不来的任务不该在库里留下记录"


async def _script_tokens_and_iteration_prompts_come_from_config() -> None:
    """写脚本的输出额度取自配置；迭代的结论必须带上**脚本源码**与逐局战况。

    两件都是"配了/写了但没生效"的高发区：额度原来写死在 `runner` 里（4096），
    迭代结论原来只给擂台输出的尾巴（一个胜率），模型没法对着处理函数说该改哪一行。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        seen: List[int] = []
        prompts: List[str] = []

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del model
            seen.append(max_tokens)
            prompts.append(prompt)
            return "结论：Card100Handler 的阈值太宽。"

        runner = _make_runner(tmp, store, generate=fake_generate)
        # 1) 默认额度
        assert await runner._as_prompt_generate("hello") == "结论：Card100Handler 的阈值太宽。"
        assert seen[-1] == 4096, seen
        # 2) 配置推过来之后按配置走；给 0 退回默认（额度为 0 会让模型每次都返回空内容）
        runner.set_script_max_tokens(7000)
        await runner._as_prompt_generate("hello")
        assert seen[-1] == 7000, seen
        runner.set_script_max_tokens(0)
        await runner._as_prompt_generate("hello")
        assert seen[-1] == 4096, seen

        # 3) 迭代结论的素材：脚本源码 + 逐局战况都要进提示词
        arena_log = tmp / "arena.log"
        arena_log.write_text(
            "被测 exe：x\n[1/2] A 在 bot     ｜胜者=B      ｜回合=6｜   7s｜✔ A侧['AI_Gen9']\n"
            "[2/2] A 在 opponent｜胜者=A      ｜回合=4｜   7s｜✔ A侧['AI_Gen9']\n"
            "\n================ 汇总 ================\nGen9 胜率：50.0%\n",
            encoding="utf-8",
        )
        script = types.SimpleNamespace(
            style_name="Gen9",
            code="class Gen9Executor\n{\n    private bool Card100Handler() { return true; }\n}",
            warnings=[],
        )
        conclusion, error = await runner._iteration_conclusion(
            params={"deck_name": "测试牌", "opponent_name": "对手牌"},
            script=script,
            baseline="Test",
            log_path=arena_log,
            history=[{"round": 1, "conclusion": "上一轮：把阈值收紧"}],
        )
        assert error == "" and conclusion, (conclusion, error)
        prompt = prompts[-1]
        for needle in ("Card100Handler", "[1/2] A 在 bot", "Gen9 胜率", "上一轮：把阈值收紧"):
            assert needle in prompt, f"迭代结论的提示词里缺少 {needle}"

        # 读不到的日志要如实说，而不是拼出一份假战况
        assert "读不到" in runner._arena_evidence(tmp / "不存在的.log")


async def _write_script_record_says_how_much_it_wrote() -> None:
    """「编写脚本」的记录里要写清"写了多少"：行数、处理函数个数、缺了哪些卡。

    用户看面板就是看这几个数（150 行的脚本和 600 行的脚本，一眼能分出来）。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        runner = _make_runner(tmp, store)
        run = store.create(
            "write_script", "编写脚本：测试牌", {"deck_id": 1, "deck_name": "测试牌"}, tmp / "a.log"
        )

        async def fake_combo(params: Dict[str, Any]) -> str:
            del params
            return "推演讲义"

        async def fake_generate_script(params: Dict[str, Any], **kwargs: Any) -> Any:
            del params, kwargs
            code = "class Gen1Executor\n{\n" + "\n".join(
                f"    private bool Card{index}Handler() {{ return false; }}" for index in range(3)
            ) + "\n}"
            return types.SimpleNamespace(
                style_name="Gen1",
                file_path=tmp / "Gen1Executor.cs",
                code=code,
                attempts=1,
                warnings=["这些卡没写出处理函数，只登记了通用行为：某卡（9）"],
            )

        runner._derive_combo_for = fake_combo  # type: ignore[method-assign]
        runner._generate_script = fake_generate_script  # type: ignore[method-assign]
        await runner._run_write_script(
            run, {"deck_id": 1, "deck_name": "测试牌", "deck_file": str(tmp / "牌.ydk")}
        )
        saved = store.get(run.run_id)
        assert saved is not None and saved.status == store_module.STATUS_DONE, saved and saved.error
        assert saved.summary["handlers"] == 3, saved.summary
        assert saved.summary["lines"] >= 5, saved.summary
        assert saved.summary["warnings"] == ["这些卡没写出处理函数，只登记了通用行为：某卡（9）"]


async def _active_reports_only_a_running_task() -> None:
    """面板靠 `active()` 判断"有没有任务在跑"：**跑完的任务不能一直占着它**。

    这条是 bug 的回归测试：`active()` 原来只看 `_run_id`，而正常跑完不会清它（只有 stop 才清），
    于是跑完之后面板上那条"正在跑"的气泡与「有任务在跑」的禁用状态永远挂着——
    用户跑完一件事就再也点不动下一件。跑完的记录必须照旧留在历史里。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        runner = _make_runner(tmp, store)
        run = store.create("arena", "测试任务", {}, tmp / "a.log")
        runner._run_id = run.run_id
        assert runner.active() is None, "没有任务在跑时不该报 active"

        running = asyncio.create_task(asyncio.sleep(0.2))
        runner._task = running
        payload = runner.active()
        assert payload is not None and payload["live"] is True, payload
        await running
        assert runner.active() is None, "跑完的任务不该继续占着 active（面板会一直禁用开始按钮）"
        assert store.get(run.run_id) is not None, "记录照旧在历史里"


async def _review_writes_its_material_into_the_run_log() -> None:
    """复盘优化要把"依据的这几局"与结论写进任务日志——面板的「详情」读的就是它。

    回归测试：它以前根本不写日志，面板上是"（没有输出）"，用户想看"它到底依据了什么"无从查起
    （用户报"复盘优化不能看到该对局的日志吗"）。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        runner = _make_runner(tmp, store)

        # 造一条"打完的房间对局"记录（复盘就是读它）
        duel = store.create(
            "duel",
            "房间对局：测试牌",
            {"deck_id": 5, "group_id": "111"},
            tmp / "duel.log",
        )
        # 按**记录的真实结构**造：players 是按对局座位号做键的字符串统计
        # （以前复盘读的是 self/opponent/card_usage 这些不存在的键，于是什么都看不到）
        store.finish(
            duel.run_id,
            "done",
            summary={
                "winner_is_self": False,
                "turns": 6,
                "duration_seconds": 123,
                "reason": "基本分变成0",
                "self_seat": 0,
                "deck_name": "测试牌",
                "deck_style": "Gen5",
                "players": {
                    "0": {"name": "憨憨", "normal_summons": 1, "sp_summons": 4, "effects": 3,
                          "sets": 2, "draws": 5, "attacks": 2, "direct_attacks": 0,
                          "damage_dealt": 1200, "damage_taken": 3000, "biggest_hit_taken": 2500},
                    "1": {"name": "群友", "normal_summons": 3, "sp_summons": 9, "effects": 11,
                          "sets": 4, "draws": 7, "attacks": 4, "direct_attacks": 2,
                          "damage_dealt": 3000, "damage_taken": 1200, "biggest_hit_taken": 1200},
                },
                "top_cards": ["「测试白板A」x4", "「测试白板B」x3"],
                "placements": ["第 1 回合 憨憨（我方） 主怪兽区1 ← 「测试白板A」"],
            },
        )

        seen_prompts: List[str] = []

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del model, max_tokens
            seen_prompts.append(prompt)
            return "先把 XX 的阈值收紧。"

        runner = _make_runner(tmp, store, generate=fake_generate)
        run = store.create(
            "review", "复盘优化：测试牌", {"deck_id": 5, "deck_name": "测试牌"}, tmp / "review.log"
        )
        await runner._run_review(
            run,
            {"deck_id": 5, "deck_name": "测试牌", "deck_file": str(tmp / "牌.ydk"), "style": "Gen5",
             "latest": 1},
        )
        saved = store.get(run.run_id)
        assert saved is not None and saved.status == store_module.STATUS_DONE, saved and saved.error
        log_text = (tmp / "review.log").read_text(encoding="utf-8")
        assert "这几局的事实" in log_text and "模型的复盘结论" in log_text, log_text[:300]
        assert "先把 XX 的阈值收紧。" in log_text, log_text[-200:]
        assert saved.summary["log_path"].endswith("review.log"), saved.summary
        # 事实要真的进提示词/日志：回合数、双方动作数、用过的卡、落位、胜负原因
        prompt = seen_prompts[0]
        for needle in ("回合 6", "时长 123 秒", "胜负原因 基本分变成0", "我方（憨憨）", "召唤 1", "发动 3",
                       "对手（群友）", "直接攻击 2", "最大一击 2500", "用过的卡", "测试白板A", "主怪兽区1"):
            assert needle in prompt, f"复盘提示词里缺少 {needle}"
            assert needle in log_text, f"复盘的日志里缺少 {needle}"


def _duel_brief_reads_the_real_record_shape() -> None:
    """复盘事实按记录**真实的结构**读（`players` 按座位 / `top_cards` / `placements`）。

    回归测试：键名想当然写成 `self` / `opponent` / `card_usage` 时，每条记录只剩一行
    "第 N 局：我方输｜回合 4"，模型只能回"记录里没有给出动作数、无法指出具体问题"——
    用户看到的就是"复盘优化给不出有用建议"。
    """

    runner_module = _load("train.runner")
    record = types.SimpleNamespace(
        started_at=1791511637.0,
        params={"deck_id": 5, "deck_name": "测试牌"},
        summary={
            "winner_is_self": False,
            "turns": 4,
            "duration_seconds": 376,
            "reason": "基本分变成0",
            "self_seat": 0,
            "deck_style": "Gen5",
            "players": {
                "0": {"name": "憨憨", "normal_summons": 0, "sp_summons": 7, "effects": 7, "sets": 0,
                      "draws": 6, "attacks": 1, "direct_attacks": 1, "damage_dealt": 2900,
                      "damage_taken": 9900, "biggest_hit_taken": 5100, "lp_recovered": 0},
                "1": {"name": "二憨", "normal_summons": 2, "sp_summons": 17, "effects": 26, "sets": 4,
                      "draws": 9, "attacks": 3, "direct_attacks": 1, "damage_dealt": 9900,
                      "damage_taken": 6200, "biggest_hit_taken": 2900, "lp_recovered": 2300},
            },
            "top_cards": ["「机巧蛇-丛云远吕智」x4", "「碧之异解△屠奥内拉」x3"],
            "placements": ["第 1 回合 憨憨（我方） 主怪兽区3 ← 「机巧蛇-丛云远吕智」"],
        },
    )
    brief = runner_module._duel_brief(1, record)
    for needle in ("我方输", "回合 4", "时长 376 秒", "基本分变成0",
                   "我方（憨憨）", "特召 7", "发动 7", "直接攻击 1", "受到伤害 9900",
                   "对手（二憨）", "特召 17", "发动 26", "回复 2300",
                   "用过的卡", "机巧蛇", "落位", "主怪兽区3", "脚本 Gen5"):
        assert needle in brief, "复盘事实里缺少 " + needle + "：" + chr(10) + brief


async def _iterate_does_not_borrow_its_round_count_as_compile_retries() -> None:
    """迭代的"轮数"是迭代几轮，不能拿去当"生成 → 编译"的重试次数。

    真机上踩过：迭代填 5 轮 → 写脚本那一步白重试 5 遍，失败信息也写成"生成 5 轮都没能编译通过"，
    把真正的原因（模型用了不存在的 API 名）盖住了。写脚本任务的"生成→编译轮数"仍按面板那一栏走。
    """

    runner_module = _load("train.runner")
    scriptgen = _load("train.scriptgen")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = _load("train.store").TrainingStore(tmp / "training.db")
        runner = _make_runner(tmp, store)
        source, windbot = tmp / "windbot-src", tmp / "windbot"

        seen: List[int] = []

        class RecordingGenerator:
            """只记下 max_attempts 就被构造出来的替身（不真的写文件、不编译）。"""

            def __init__(self, generate: Any, **kwargs: Any) -> None:
                seen.append(int(kwargs["max_attempts"]))
                raise scriptgen.ScriptGenerationError("替身：到此为止")

            @staticmethod
            def style_name_for(deck_id: int) -> str:
                return f"Gen{deck_id}"

        original = runner_module.DeckScriptGenerator
        runner_module.DeckScriptGenerator = RecordingGenerator  # type: ignore[attr-defined]
        try:
            runner._windbot_dirs = lambda: (source, windbot)  # type: ignore[method-assign]
            (source / "Game" / "AI" / "Decks").mkdir(parents=True, exist_ok=True)
            (source / "WindBot.csproj").write_text("<Project />", encoding="utf-8")
            (windbot / "Decks").mkdir(parents=True, exist_ok=True)
            deck_path = tmp / "牌.ydk"
            _write_deck(deck_path, [101])
            params = {"deck_id": 5, "deck_name": "测试牌", "deck_file": str(deck_path), "rounds": 5}
            try:
                await runner._generate_script(params)
            except Exception:  # noqa: BLE001  替身故意抛错，这里只关心传进去的重试次数
                pass
            assert seen and seen[-1] == runner_module.DEFAULT_SCRIPT_ATTEMPTS, seen
            # 写脚本那条路仍旧按面板上的"生成→编译轮数"走
            try:
                await runner._generate_script(params, attempts=5)
            except Exception:  # noqa: BLE001  同上
                pass
            assert seen[-1] == 5, seen
        finally:
            runner_module.DeckScriptGenerator = original  # type: ignore[attr-defined]


async def _arena_refused_while_room_is_active() -> None:
    """房间里有人在打时不许起擂台（会再拉一套内核与两个 WindBot，把真人那局打坏）。"""

    store_module = _load("train.store")
    runner_module = _load("train.runner")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        deck_path = tmp / "a.ydk"
        _write_deck(deck_path, [101])
        runner = _make_runner(tmp, store, rooms=1)
        try:
            await runner.start(
                "arena",
                {"deck_file": str(deck_path), "style_a": "A", "style_b": "B", "duels": 10},
            )
        except runner_module.TrainingError as exc:
            assert "房间" in str(exc), exc
        else:
            raise AssertionError("有房间在跑时不该起擂台")


async def _arena_needs_two_different_styles() -> None:
    """两边脚本名一样等于自己打自己，必须在起任务前拦住。"""

    store_module = _load("train.store")
    runner_module = _load("train.runner")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        deck_path = tmp / "a.ydk"
        _write_deck(deck_path, [101])
        runner = _make_runner(tmp, store)
        for style_a, style_b in (("A", "A"), ("", "B")):
            try:
                await runner.start(
                    "arena",
                    {
                        "deck_file": str(deck_path),
                        "style_a": style_a,
                        "style_b": style_b,
                        "duels": 10,
                    },
                )
            except runner_module.TrainingError:
                pass
            else:
                raise AssertionError(f"这组脚本名不该起得来：{style_a!r} / {style_b!r}")


async def _subprocess_run_writes_log_and_stops() -> None:
    """起子进程那条路：输出直接落进日志；停任务后子进程必须真的不再输出。

    假工具不认识任何参数，只管打印 + 写心跳——测的是"接线"（日志、停任务、记录状态），
    不是工具本身的逻辑。走的种类是**卡组互打**：四个面板任务里只有它（和迭代）会起子进程，
    而它现在要两副各自带脚本的卡组。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        runner = _make_runner(tmp, store)
        deck_a, deck_b = _seed_two_decks(tmp)
        heartbeat = tmp / "heartbeat.txt"
        tool = runner.tools_dir / "style_ab.py"
        tool.write_text(
            "import pathlib, sys, time\n"
            "print('假工具开始跑', flush=True)\n"
            f"target = pathlib.Path(r'{heartbeat}')\n"
            "deadline = time.time() + 120\n"
            "while time.time() < deadline:\n"
            "    with target.open('a', encoding='utf-8') as handle:\n"
            "        handle.write('tick\\n')\n"
            "    time.sleep(0.1)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        async def fake_generate(prompt: str, model: str, max_tokens: int) -> str:
            del prompt, max_tokens
            assert model == "test-model", model
            return "这一轮只是机制检查。"

        runner = _make_runner(tmp, store, generate=fake_generate)
        params = {"deck_id": deck_a, "opponent_deck_id": deck_b, "duels": 2}
        run = await runner.start("arena", params)
        assert runner.busy, "起完之后应该在跑"
        # 起第二个任务必须被拦住（同一台机器上两套内核会互相抢资源）
        try:
            await runner.start("arena", params)
        except _load("train.runner").TrainingError as exc:
            assert "已经在跑" in str(exc) or "在跑" in str(exc), exc
        else:
            raise AssertionError("同一时刻只允许一个训练任务")

        ok = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _wait_for(lambda: "假工具开始跑" in _read(run.log_path))
        )
        assert ok, f"子进程的输出没有进日志：{_read(run.log_path)!r}"
        ok = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _wait_for(heartbeat.exists)
        )
        assert ok, "心跳文件没出现"

        assert await runner.stop() is True
        stopped = store.get(run.run_id)
        assert stopped.status == store_module.STATUS_CANCELLED, stopped.status
        ticks_at_stop = heartbeat.read_text(encoding="utf-8").count("\n")
        await asyncio.sleep(1.5)
        assert heartbeat.read_text(encoding="utf-8").count("\n") == ticks_at_stop, (
            "停任务之后子进程还在跑：真机上就是孤儿内核占着端口"
        )
        assert not runner.busy


def _read(path: Any) -> str:
    """读一个可能还不存在的文件（读不到就当空）。"""

    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


async def _stop_now_kills_the_tree_within_unload_budget() -> None:
    """**卸载路径**（`stop_now`，同步）也要杀掉训练进程，而且必须留在 5 秒预算内。

    这条是回归测试：宿主给插件卸载的预算是 5 秒，而卸载要连着做三件事——停房间、
    停面板、停训练。杀进程树的等待上限如果按正常停止那条路给 5 秒，
    "训练在跑的时候卸载"就必然超预算，宿主会记 `plugin.shutdown 超时` 并把整个插件重启
    （对局里的连接跟着断，用户看到的是"莫名其妙掉线"）。
    """

    store_module = _load("train.store")
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        store = store_module.TrainingStore(tmp / "training.db")
        deck_a, deck_b = _seed_two_decks(tmp)
        heartbeat = tmp / "heartbeat.txt"
        tool = tmp / "plugin" / "tools" / "style_ab.py"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(
            "import pathlib, time\n"
            "print('假工具开始跑', flush=True)\n"
            f"target = pathlib.Path(r'{heartbeat}')\n"
            "deadline = time.time() + 120\n"
            "while time.time() < deadline:\n"
            "    with target.open('a', encoding='utf-8') as handle:\n"
            "        handle.write('tick\\n')\n"
            "    time.sleep(0.1)\n",
            encoding="utf-8",
        )
        runner = _make_runner(tmp, store)
        run = await runner.start(
            "arena", {"deck_id": deck_a, "opponent_deck_id": deck_b, "duels": 2}
        )
        ok = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _wait_for(heartbeat.exists)
        )
        assert ok, "假工具没起来"

        started = time.monotonic()
        runner.stop_now()
        elapsed = time.monotonic() - started
        assert elapsed < 2.6, f"卸载路径停训练用了 {elapsed:.2f} 秒（预算 2 秒）"
        assert not runner.busy

        # 记录会留在 running（卸载路径没机会写库），由下次加载时统一改成失败
        assert store.get(run.run_id).status == store_module.STATUS_RUNNING
        assert store.mark_interrupted() == 1
        assert store.get(run.run_id).status == store_module.STATUS_FAILED

        ticks_at_stop = heartbeat.read_text(encoding="utf-8").count("\n")
        await asyncio.sleep(1.2)
        assert heartbeat.read_text(encoding="utf-8").count("\n") == ticks_at_stop, (
            "卸载时没杀掉训练进程：真机上就是孤儿内核占着端口"
        )


def test_webui_art_endpoint_serves_local_art_only_for_numeric_ids() -> None:
    """卡图接口：只认数字卡号、只发本地有的图、没有就 404（**不联网**）。

    这条护栏针对的是"用户输入进路径"这类问题：卡号必须是纯数字、文件名由卡号拼出来，
    所以 `../` 那类输入从形状上就不成立。另外面板要快——缺图不在这里联网补，
    交给查房那条链路（`duel/card_images.py`）慢慢补。
    """

    webui = _load("webui")
    secret = "a" * 32
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        art_dir = root / "Art"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "12345.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        (art_dir / "22222.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
        (art_dir / "33333.txt").write_text("不是图", encoding="utf-8")

        plugin = StubPlugin(root / "data")
        plugin.config.paths.card_art_dir = str(art_dir)
        plugin.config.paths.card_art_fallback_dir = str(art_dir)
        server, port = _start_panel(webui, plugin, secret)
        headers = {"X-API-Key": secret}
        try:
            status, _body, reply = _http(port, "GET", "/api/art/12345", headers=headers)
            assert status == 200, status
            assert reply.get("Content-Type") == "image/jpeg", reply
            assert "max-age" in reply.get("Cache-Control", ""), reply

            status, _body, reply = _http(port, "GET", "/api/art/22222", headers=headers)
            assert status == 200 and reply.get("Content-Type") == "image/png", reply

            # 本地没有这张卡的图 → 404（前端画占位卡背）
            status, _body, _reply = _http(port, "GET", "/api/art/99999", headers=headers)
            assert status == 404, status
            # 本地有同名 txt 也不该被当成图发出去
            status, _body, _reply = _http(port, "GET", "/api/art/33333", headers=headers)
            assert status == 404, status
            # 非数字（含路径穿越的样子）一律 400/404，绝不拼进文件名
            for bad in ("..%2F..%2Fetc", "abc", "12345.jpg"):
                status, _body, _reply = _http(port, "GET", f"/api/art/{bad}", headers=headers)
                assert status in (400, 404), (bad, status)
            # 卡图接口同样要密钥
            status, _body, _reply = _http(port, "GET", "/api/art/12345")
            assert status == 401, status
        finally:
            server.stop_now()


def test_deck_names_and_head_cards_come_from_the_card_db() -> None:
    """卡组接口给的是**卡名**而不是卡号；头牌优先挑本地有图的那张。

    两个都踩过：卡名那处调了不存在的 `card_db.get_name()`（异常被吞掉 → 整页显示卡号），
    头牌那处固定取额外卡组第一张（结果一半卡组的缩略图是占位卡背，看着像图全挂了）。
    """

    webui = _load("webui")
    secret = "b" * 32
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        art_dir = root / "Art"
        art_dir.mkdir(parents=True, exist_ok=True)
        # 只给"第二张额外卡"准备图：头牌应该因此跳过第一张
        (art_dir / "5002.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")

        deck_text = "#main\n" + "\n".join(str(1000 + i) for i in range(3)) + "\n#extra\n5001\n5002\n!side\n"
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
                extra_count=2,
                side_count=0,
            )
            # 卡组页那个「AI 决策档位」：面板是直接读卡组库这一列的。
            # ⚠ 顺带锁住"新库也要有 brain_scope 列"——列不存在时接口会静默变成"一副牌都没有"。
            pool.set_brain_scope(stored.deck_id, "target_only")
        finally:
            # 一定要关：Windows 上没关连接，临时目录清理会报"文件正在使用"，
            # 而那个报错会把真正的失败原因（比如参数写错）盖掉
            pool.close()

        plugin = StubPlugin(data_dir)
        plugin.config.paths.card_art_dir = str(art_dir)
        plugin.config.paths.card_art_fallback_dir = str(art_dir)
        plugin._card_db = FakeCardDb(
            {1000 + i: f"主卡{1000 + i}" for i in range(3)} | {5001: "额额外一", 5002: "额额外二"}
        )

        server, port = _start_panel(webui, plugin, secret)
        headers = {"X-API-Key": secret}
        try:
            status, body, _reply = _http(port, "GET", "/api/decks", headers=headers)
            deck = json.loads(body)["groups"][0]["decks"][0]
            assert deck["head_card"] == 5002, f"头牌该挑本地有图的那张：{deck['head_card']}"
            assert deck["brain_scope"] == "target_only", deck["brain_scope"]

            status, body, _reply = _http(
                port, "GET", f"/api/deck/{deck['deck_id']}?group=111", headers=headers
            )
            detail = json.loads(body)["deck"]
            names = [entry["name"] for entry in detail["entries"]["main"]]
            assert all(name.startswith("主卡") for name in names), names
            assert [entry["name"] for entry in detail["entries"]["extra"]] == ["额额外一", "额额外二"]
            assert detail["entries"]["main"][0]["count"] == 1
            assert detail["head_card"] == 5002
            assert detail["brain_scope"] == "target_only", detail["brain_scope"]
        finally:
            server.stop_now()
            # 面板的请求是在**自己的线程**里跑的（daemon 线程，停服务时不会被 join），
            # 刚发出去的那次请求可能还在读 deck_pool.db。等它收尾再删临时目录，
            # 否则 Windows 上会报"文件正在使用"——那不是被测代码泄漏句柄，是测试自己的时序。
            time.sleep(0.4)


def test_panel_has_no_external_asset_references() -> None:
    """面板的页面不引任何外网资源（断网也要能打开）。

    这条锁的是"顺手挂个 CDN 图标/字体"这种改动：面板跑在国内的机器上，
    外网字体一挂就是几秒白屏，而它本来只是看卡组与训练状态的小工具。
    只允许 `/api/...` 这种同源地址与内联的 svg/data URI。
    """

    webui = _load("webui")
    for html in (webui._login_page(""), webui._app_page()):
        assert "//cdn" not in html, html[:200]
        assert "http://" not in html.split("</style>")[0] or "127.0.0.1" in html, "样式里不该有外部地址"
        for marker in ("fonts.googleapis", "unpkg", "jsdelivr", "cdnjs"):
            assert marker not in html, f"页面里出现了外部资源：{marker}"
    assert "<svg" in webui._app_page(), "图标应该是内联 svg"


def test_training_composer_keeps_the_kind_fields_on_screen() -> None:
    """随种类出现的字段（对手卡组 / 局数 / 轮数）要在**同一行**里，不能排在文本框之后。

    回归测试：它原来排在 textarea 后面，真机上整块被挤出视口（量下来 top=999px、视口 1000px）——
    用户看到的就是"对手卡组没有显示"（其实渲染了 97 项，只是看不见）。
    另外下拉不能被最长的卡组名撑宽：一撑宽这一行就换行，"对手卡组"又掉到第二行去。
    """

    webui = _load("webui")
    page = webui._app_page()
    assert '<div class="extra-inline" id="c-extra"></div>' in page, "字段要放进「卡组 / 要写的东西」那一行"
    assert page.index('id="c-extra"') < page.index('id="c-text"'), "字段必须在额外要求文本框之前"
    assert "max-width:260px" in page, "下拉要限宽，否则被长卡组名撑宽后这一行会换行"


def test_deck_card_opens_its_detail_from_the_whole_card() -> None:
    """卡组卡片**整张**都能点开详情，而卡上的两个控件（随机池 / AI 决策）不能顺带打开抽屉。

    回归测试：给卡片加内联控件时把整卡的 `onclick` 挪到了卡名那一行——卡片本身还留着
    `cursor:pointer`，于是"看着能点、点了一点反应没有"（用户报的正是"卡组界面不能查看卡组详情了"）。
    """

    webui = _load("webui")
    page = webui._app_page()
    assert 'class="deck" onclick="showDeck(' in page, "整张卡组卡要能点开详情"
    assert 'class="deckctl" onclick="event.stopPropagation()"' in page, (
        "卡上的开关/下拉要自己吞掉点击，否则点控件会顺带打开详情抽屉"
    )


def main() -> int:
    """逐个执行测试；协程测试用 asyncio.run 驱动。"""

    tests: List[Any] = [
        test_resolve_api_key_prefers_config_then_env_then_file,
        test_webui_requires_key_and_serves_pages,
        test_webui_port_conflict_reports_instead_of_raising,
        test_webui_training_endpoints_reach_the_plugin,
        test_training_store_lifecycle,
        test_webui_art_endpoint_serves_local_art_only_for_numeric_ids,
        test_deck_names_and_head_cards_come_from_the_card_db,
        test_panel_has_no_external_asset_references,
        test_deck_card_opens_its_detail_from_the_whole_card,
        test_training_composer_keeps_the_kind_fields_on_screen,
        test_kill_tree_uses_taskkill_with_tree_flag,
        test_duel_page_slots_hide_stats_outside_monster_zones_and_show_face_down,
        test_page_js_has_no_bare_newline_inside_strings,
        test_training_tasks_end_with_a_plain_text_result,
        test_deck_digest_and_combo_check_catch_cards_outside_the_deck,
        test_combo_tolerates_non_json_reply_but_says_so,
        test_empty_model_reply_is_an_error_not_an_empty_guide,
        test_timeout_failure_says_what_to_change,
        test_combo_keeps_the_main_lines_when_the_notes_round_fails,
        _combo_derivation_archives_the_guide,
        _research_goes_to_the_search_model_and_never_blocks_the_run,
        test_search_agent_loops_until_it_has_enough,
        test_script_digest_shows_order_missing_hints_and_rule_names,
        test_decision_log_slice_keeps_actions_inside_the_window_only,
        test_patch_parsing_and_all_or_nothing_apply,
        _review_feeds_the_script_digest_and_that_duel_log,
        _review_apply_writes_backup_and_rolls_back_when_build_fails,
        test_search_agent_keeps_the_raw_results_even_if_the_summary_talks_past_them,
        test_search_agent_survives_a_failed_query_and_stops_at_the_round_cap,
        _runner_uses_the_search_agent_only_when_the_host_offers_tools,
        _script_tokens_and_iteration_prompts_come_from_config,
        _write_script_record_says_how_much_it_wrote,
        _unknown_deck_is_refused_before_a_record_exists,
        _active_reports_only_a_running_task,
        _duel_brief_reads_the_real_record_shape,
        _review_writes_its_material_into_the_run_log,
        _iterate_does_not_borrow_its_round_count_as_compile_retries,
        _arena_refused_while_room_is_active,
        _arena_needs_two_different_styles,
        _subprocess_run_writes_log_and_stops,
        _stop_now_kills_the_tree_within_unload_budget,
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
