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
from typing import Any, Dict, List, Optional

import asyncio
import http.client
import json
import os
import sys
import tempfile
import time

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
    assert "30 秒" in hint and "deepseek-chat" in hint, hint
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
        assert "30 秒" in str(exc) and "deepseek-chat" in str(exc), exc
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
        test_kill_tree_uses_taskkill_with_tree_flag,
        test_deck_digest_and_combo_check_catch_cards_outside_the_deck,
        test_combo_tolerates_non_json_reply_but_says_so,
        test_empty_model_reply_is_an_error_not_an_empty_guide,
        test_timeout_failure_says_what_to_change,
        test_combo_keeps_the_main_lines_when_the_notes_round_fails,
        _combo_derivation_archives_the_guide,
        _unknown_deck_is_refused_before_a_record_exists,
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
