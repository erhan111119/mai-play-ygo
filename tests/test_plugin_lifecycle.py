"""插件生命周期与工具行为测试。

契约测试只验证了「组件注册得上」，这里补上真正跑一遍：

* 按宿主的注入方式设置上下文与配置，调用 ``on_load``，确认它不会抛异常、
  并且真的把卡组池建了出来；
* 逐个调用工具，确认它们在「配置还没填」和「配置已填但路径不存在」这两种情况下
  返回的是可读的提示，而不是抛异常或沉默；
* 调用 ``on_unload``，确认资源被释放（Windows 上不关 sqlite 会让数据目录删不掉）。

宿主源码不在旁边时会自动跳过。

直接用 ``python tests/test_plugin_lifecycle.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import asyncio
import dataclasses
import importlib.util
import sqlite3
import sys
import tempfile
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# 宿主根目录：本文件位于 <MaiBot>/plugins/<plugin>/tests/
_HOST_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _HOST_ROOT.parent.parent
_SDK_CANDIDATES = (
    _INSTALL_ROOT / "python-overrides",
    _INSTALL_ROOT / "python-env" / "Lib" / "site-packages",
)
for extra in (_HOST_ROOT, *_SDK_CANDIDATES):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# 一副合法的测试卡组（40 主卡组 + 3 额外 + 1 副卡组）
MAIN = [89631139, 89631139, 89631139] + [100000000 + index for index in range(37)]
EXTRA = [100200001, 100200002, 100200002]
SIDE = [100300001]


def make_cards_cdb(path: Path, card_ids: List[int]) -> Path:
    """造一个只认识指定卡 ID 的 cards.cdb，用来测「卡库核对」。

    两张表都要有：**生成专属脚本时读的是 ``datas``**（攻防/种族/类型那些要喂给模型），
    只建 ``texts`` 的话那条路径一走就报 "no such table: datas"（实测踩过）。
    """

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT, desc TEXT)")
    connection.execute(
        "CREATE TABLE datas (id INTEGER PRIMARY KEY, ot INTEGER, alias INTEGER, setcode INTEGER,"
        " type INTEGER, atk INTEGER, def INTEGER, level INTEGER, race INTEGER, attribute INTEGER,"
        " category INTEGER)"
    )
    connection.executemany(
        "INSERT OR IGNORE INTO texts VALUES (?, ?, ?)",
        [(card_id, f"测试卡{card_id}", f"测试卡{card_id}的效果。") for card_id in card_ids],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO datas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(card_id, 1, 0, 0, 1, 1000, 1000, 4, 1, 1, 0) for card_id in card_ids],
    )
    connection.commit()
    connection.close()
    return path


def _sdk_available() -> bool:
    """maibot_sdk 是否可导入。"""

    try:
        import maibot_sdk  # noqa: F401
    except Exception:  # noqa: BLE001  缺少宿主依赖时跳过
        return False
    return True


class FakeSend:
    """send 能力替身：记录发出去的消息/图片，不真的发。"""

    def __init__(self) -> None:
        self.messages: List[str] = []
        self.images: List[str] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        """记录一条文本发送。"""

        del stream_id, kwargs
        self.messages.append(text)
        return True

    async def image(self, image_data: str, stream_id: str, **kwargs: Any) -> bool:
        """记录一条图片发送（查房出图走这里）。"""

        del stream_id, kwargs
        self.images.append(image_data)
        return True


class FakeRender:
    """render 能力替身：把收到的 HTML 与参数记下来，返回一张假 PNG（base64）。"""

    def __init__(self) -> None:
        self.pages: List[str] = []
        self.calls: List[Dict[str, Any]] = []
        self.fail = False

    async def html2png(self, html: str, **kwargs: Any) -> Dict[str, Any]:
        """记录 HTML 与渲染参数；按 ``fail`` 决定抛错（查房应当退文本）还是返回图片。"""

        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("模拟渲染失败")
        self.pages.append(html)
        return {"image_base64": "ZmFrZXBuZw==", "mime_type": "image/png", "width": 1280, "height": 720}


class FakeMaisaka:
    """maisaka 能力替身：记录上下文注入与主动唤起，并按宿主约定返回 success。"""

    def __init__(self) -> None:
        self.appended: List[str] = []
        self.triggered: List[Dict[str, Any]] = []
        self.fail_next = False

    async def append_context(self, stream_id: str, segments: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        """记录一次上下文注入。"""

        del stream_id, kwargs
        if self.fail_next:
            self.fail_next = False
            return {"success": False, "error": "模拟宿主拒绝"}
        text = "".join(str(segment.get("data") or "") for segment in segments)
        self.appended.append(text)
        return {"success": True}

    async def trigger_proactive(self, stream_id: str, intent: str, **kwargs: Any) -> Dict[str, Any]:
        """记录一次强制唤起（宿主会据此安排一轮 planner）。"""

        self.triggered.append({"stream_id": stream_id, "intent": intent, **kwargs})
        return {"success": True}


class FakeLlm:
    """llm 能力替身：按预设文本返回，方便断言「总结是先经过模型再发的」。"""

    def __init__(self) -> None:
        self.reply = "这局我赢啦，对面 8 回合就被我打没了。"
        self.fail = False
        self.prompts: List[str] = []
        #: 每次调用收到的完整参数——断言"配置里的模型名真的传到调用点了"要靠它
        self.calls: List[Dict[str, Any]] = []
        #: 让替身"想一会儿"（测超时路径用）
        self.delay = 0.0

    async def generate(self, prompt: str = "", **kwargs: Any) -> Dict[str, Any]:
        """记录提示词与参数并返回预设回复。"""

        self.prompts.append(prompt)
        self.calls.append({"prompt": prompt, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            return {"success": False, "error": "模拟模型不可用"}
        return {"success": True, "response": self.reply}


class FakeChat:
    """chat 能力替身：给出聊天流列表。

    ``/查房`` 跨对话流列房间时要按 stream_id 查群名/私聊名（不能给群友报会话号），
    这里就按宿主的真实返回结构（``session_id`` / ``is_group_session`` / ``group_name`` /
    ``user_nickname``）造替身。
    """

    def __init__(self) -> None:
        self.streams: List[Dict[str, Any]] = []

    async def get_all_streams(self, platform: str = "qq") -> List[Dict[str, Any]]:
        """返回预设的聊天流列表。"""

        del platform
        return list(self.streams)


class FakeContext:
    """插件上下文替身：只提供插件真正用到的部分。"""

    def __init__(self, data_dir: Path) -> None:
        import logging

        self.paths = type("Paths", (), {"data_dir": data_dir, "runtime_dir": data_dir / "runtime"})()
        self.logger = logging.getLogger("test.mai-play-ygo")
        self.send = FakeSend()
        self.maisaka = FakeMaisaka()
        self.llm = FakeLlm()
        self.chat = FakeChat()
        self.render = FakeRender()


def load_plugin_module():
    """按宿主加载器的方式导入 plugin.py 并返回模块。"""

    spec = importlib.util.spec_from_file_location(
        # 模块名跟着现在的插件 id 走（2026-10-07 第五轮评审顺带指出旧名残留）
        "mai_play_ygo_lifecycle",
        str(_PLUGIN_ROOT / "plugin.py"),
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_plugin(data_dir: Path, config: Dict[str, Any] | None = None):
    """造一个已注入上下文与配置的插件实例，返回 ``(实例, 上下文替身)``。"""

    module = load_plugin_module()
    instance = module.create_plugin()
    context = FakeContext(data_dir)
    instance._set_context(context)
    instance.set_plugin_config(config or {})
    return instance, context


def deck_code() -> str:
    """生成一段合法的裸卡组码。"""

    from duel.deckcode import Deck

    return Deck(tuple(MAIN), tuple(EXTRA), tuple(SIDE)).to_deck_code()


async def submit_deck(
    instance: Any,
    context: Any,
    *,
    code: str,
    deck_name: str = "",
    stream_id: str = "s1",
    group_id: str = "111",
    user_id: str = "u1",
    user_nickname: str = "群友A",
) -> str:
    """走指令入口投稿一副卡组，返回插件发到群里的那条回执。

    2026-10-07 用户口径：去掉「模型看到卡组码就自动收录」的工具，**卡组码只能用指令投稿**。
    所以测试也从 `tool_submit_deck` 改成 `/加卡组 <卡组码> [卡组名]`——投稿走的是同一份
    `_store_deck_from_code`（解析、卡库核对、回执都在那里），回执从群里那条消息上取。
    """

    text = f"/加卡组 {code}" + (f" {deck_name}" if deck_name else "")
    await instance.cmd_deck_add(
        text=text,
        stream_id=stream_id,
        group_id=group_id,
        user_id=user_id,
        user_nickname=user_nickname,
    )
    return str(context.send.messages[-1])


def empty_windbot_dir(root: Path) -> Path:
    """造一个「没有任何内置卡表」的 WindBot 目录。

    插件默认的 `paths.windbot_dir` 指向自带的 ``clients/windbot``，那里躺着 WindBot 自带的
    Decks/（本机 72 副）——内置卡组会一起进卡组池，于是「编号 1 就是我投的那副」「池子是空的」
    这类断言全都不成立。测试要的是**自己造的那个小世界**，所以显式把 windbot_dir 指到一个
    空目录（`load_available_decks` 扫不到 .ydk 就返回空，内置卡组自然是 0 副）。
    """

    windbot = root / "windbot"
    (windbot / "Decks").mkdir(parents=True, exist_ok=True)
    return windbot


def make_playing_room(
    *,
    session_info: Any,
    state: Any = None,
    self_seat: int = 0,
    names: Tuple[str, str] = ("憨憨", "打憨憨"),
    lines: Optional[List[str]] = None,
    turn: int = 5,
    phase: str = "主要阶段2",
) -> Any:
    """造一个「打到一半」的会话替身：`/查房` 出图与报文字都只读这几个接口。

    提到模块级是因为三条用例都要它（本流出图 / 跨群出图 / 报 LP 与场面）。以前各写一份，
    `recorder` 从属性改成方法那次就得改三处——漏一处，用例自己先绿，真机上却出不了图。
    """

    class _Stats:
        """记录器那一侧的玩家统计替身（只有名字）。"""

        def __init__(self, name: str) -> None:
            self.name = name

    class _Recorder:
        """记录器替身：出图只读这几个字段。"""

        first_player_seat = 0
        players = {0: _Stats(names[0]), 1: _Stats(names[1])}

        def __init__(self) -> None:
            self.self_seat = self_seat
            self.turn_count = turn
            self.phase = phase
            self.field_state = state

    class _PlayingSession:
        """打到一半的会话替身。"""

        started = True
        finished = False
        turn_count = turn
        info = session_info
        gate = type("Gate", (), {"status": type("S", (), {"bot_connected": True})()})()

        def recorder(self) -> "_Recorder":
            """真实 `session.recorder` 是**方法**（写成属性会拿到函数对象，出图会静默退文本）。"""

            return _Recorder()

        def field_snapshot(self) -> List[str]:
            """退文本那条路要用（渲染失败时的兜底）。"""

            return list(lines or [])

        async def stop(self) -> None:
            """空实现。"""

        def kill_now(self) -> None:
            """收摊替身：用例退出时 on_unload 会调它。"""

    return _PlayingSession()


def test_plugin_reachable_modules_use_relative_imports() -> None:
    """插件能走到的模块里，不许出现「绝对导入 train.* / duel.*」。

    护栏（实测踩了两次，第二次是函数内的延迟导入）：插件是按包加载的，宿主的运行器**不会**
    把插件根目录放进 ``sys.path``，所以 ``from train.x import y`` 会直接
    ``ModuleNotFoundError``——整个插件加载失败，表现就是"没法打牌了"。
    上一次加的护栏只 import 了一下插件，抓不到**函数体里**的导入（不跑就不报），
    所以这里改成**静态走一遍模块图**：从 plugin.py 出发、沿着相对导入把能走到的文件全解析一遍，
    任何层级（含函数内）出现绝对导入都算错。
    """

    import ast

    plugin_root = _PLUGIN_ROOT
    visited = set()
    problems: List[str] = []
    queue = [plugin_root / "plugin.py"]
    sibling_roots = {"train", "duel", "tools", "tests"}

    while queue:
        path = queue.pop()
        if path in visited or not path.is_file():
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and node.module:
                    # 相对导入：解析成文件继续走（.x → 同目录；..x → 上一级）
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    parts = node.module.split(".")
                    candidate = base.joinpath(*parts)
                    queue.append(candidate.with_suffix(".py"))
                    queue.append(candidate / "__init__.py")
                    continue
                if node.level == 0 and node.module:
                    root = node.module.split(".")[0]
                    if root in sibling_roots:
                        problems.append(
                            f"{path.relative_to(plugin_root)}:{node.lineno} 绝对导入了 {node.module}"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in sibling_roots:
                        problems.append(
                            f"{path.relative_to(plugin_root)}:{node.lineno} 绝对导入了 {alias.name}"
                        )

    assert not problems, (
        "插件自己的模块只能用相对导入（插件是按包加载的，绝对导入会 ModuleNotFoundError）：\n  "
        + "\n  ".join(sorted(set(problems)))
    )
    assert len(visited) >= 5, f"模块图走得太少（{len(visited)} 个），这个检查可能没生效"


def test_plugin_imports_without_plugin_root_on_sys_path() -> None:
    """按**宿主运行器的方式**导入插件：只给宿主与 SDK 的路径，不加插件根目录。

    护栏（实测把线上打挂了）：插件自己的模块只能相对导入同包内的东西。宿主的运行器不会把
    插件根目录放进 ``sys.path``，所以 ``from train.plan import ...`` 这种绝对导入会让整个插件
    **加载失败**（日志里是一行 ``加载插件失败 [mai-play-ygo]: No module named 'train'``，
    表现就是"没法打牌了"）。而单元测试全都自己补了 ``sys.path``，所以本地一片绿、线上直接挂——
    这个测试专门用子进程重现宿主的环境，把这类错拦在提交之前。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    import subprocess

    script = (
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        f"plugin_dir = Path(r'{_PLUGIN_ROOT}')\n"
        f"host_root = Path(r'{_HOST_ROOT}')\n"
        f"install_root = Path(r'{_INSTALL_ROOT}')\n"
        "for extra in (host_root, install_root / 'python-overrides',\n"
        "              install_root / 'python-env' / 'Lib' / 'site-packages'):\n"
        "    if extra.is_dir() and str(extra) not in sys.path:\n"
        "        sys.path.insert(0, str(extra))\n"
        "assert str(plugin_dir) not in sys.path, '这个测试的前提是插件根目录不在 sys.path 上'\n"
        "name = 'plugins.mai-play-ygo'\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    name + '.plugin', plugin_dir / 'plugin.py', submodule_search_locations=[str(plugin_dir)])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
        "# 再把「对局」这条链路上实际会用到的模块也按同一个包名导进来：\n"
        "# 它们里面有延迟导入，光 import 插件本身是抓不到的（实测就是这样漏过一次）\n"
        "# （原来这里还导 train.ai_brain / train.plan / train.llm / duel.playbook：\n"
        "# AI 打牌 / 计划 / 打法数据已按 2026-10-07 用户口径删除，那几个包也没了。）\n"
        "import importlib\n"
        "for sub in ('duel.session', 'duel.netguard', 'duel.gate', 'duel.recorder', 'duel.field_image'):\n"
        "    importlib.import_module(name + '.' + sub)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert "ok" in result.stdout, (
        "按宿主的方式加载插件失败（插件自己的模块里是不是写了绝对导入？）：\n"
        + (result.stderr or result.stdout)[-1500:]
    )


# ⚠ 这里原来有一条 `test_plugin_commands_are_accepted_by_their_tools`：拿 `tools/optimize_deck.py`
# 与 `tools/train_arena.py` 自己的 argparse 去解析 `/优化卡组`、`/训练` 拼出来的命令行
# （护栏：``--opponents`` 漏传时 argparse 直接退出、报错被说成"没有候选通过判定"）。
# 2026-10-07 用户口径把训练调优整条链路删掉后，插件已经没有"拼命令行给 tools/ 子进程"的地方，
# 这条用例没有被测对象了，整条删掉。

async def test_on_load_creates_deck_pool() -> None:
    """on_load 必须能跑通，并把卡组池建出来。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            # 卡组池建库成功
            assert (Path(directory) / "deck_pool.db").is_file(), "on_load 没有建出卡组池"
            assert (Path(directory) / "decks").is_dir()
        finally:
            await instance.on_unload()


async def test_on_load_survives_empty_paths() -> None:
    """路径没填时 on_load 不能失败——用户是先启用插件、再填配置的。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        # 默认配置里路径全为空字符串，这里就是这个场景
        await instance.on_load()
        await instance.on_unload()


async def test_duel_start_reports_missing_config() -> None:
    """配置没填时开局要给可读提示，而不是抛异常。

    **注意要显式把路径清空**：插件现在自带两个虚拟客户端（``clients/``），
    默认配置指向它们，所以"什么都不配"其实是配好的——只有把 ``paths`` 里的
    这几项显式写成空串，才复现得出"没配"这条路径。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(
            Path(directory),
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {
                    "ygopro_executable": "",
                    "ygopro_dir": "",
                    "windbot_executable": "",
                    "windbot_dir": "",
                },
            },
        )
        await instance.on_load()
        try:
            result = await instance.tool_start_duel(
                platform="mdpro3", stream_id="stream-1", group_id="111"
            )
            content = str(result["content"])
            assert "没配好" in content or "未填写" in content, content
            assert context.send.messages == [], "配置不全时不该往群里发任何东西"

            # 不传 platform 也必须能开局：群里禁止提问时，模型没法先问客户端，
            # 原先 platform 必填会让它卡在「不敢问也不敢猜」上（实测：只说去建房却没建）
            fallback = await instance.tool_start_duel(stream_id="stream-1", group_id="111")
            assert "没配好" in str(fallback["content"]) or "未填写" in str(fallback["content"])

            # 模型有时会把「不用指定」写成这些字样，也要当成没填而不是报参数错
            for filler in ("空", "未指定", "无"):
                result = await instance.tool_start_duel(
                    platform=filler, stream_id="stream-1", group_id="111"
                )
                assert "没配好" in str(result["content"]) or "未填写" in str(result["content"])
        finally:
            await instance.on_unload()


async def test_duel_start_reports_bad_paths() -> None:
    """路径填了但不存在时也要给出可读原因。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, _context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {
                    "ygopro_executable": str(root / "不存在" / "ygopro.exe"),
                    "ygopro_dir": str(root / "不存在"),
                    "windbot_executable": str(root / "不存在" / "WindBot.exe"),
                    "windbot_dir": str(root / "不存在"),
                },
                "duel": {"public_host": "127.0.0.1"},
            },
        )
        await instance.on_load()
        try:
            result = await instance.tool_start_duel(
                platform="ygomobile", stream_id="stream-2", group_id="111"
            )
            content = str(result["content"])
            assert "路径不存在" in content, content
        finally:
            await instance.on_unload()


async def test_deck_submit_and_list_roundtrip() -> None:
    """投稿卡组后能在卡组池里查到，且不合法的卡组会被拒绝并说明原因。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # 卡库要自己造一份：插件自带的是真 cards.cdb，认不出测试卡号，投稿会被"卡库核对"拦下
        cdb = make_cards_cdb(root / "cards.cdb", sorted(set(MAIN) | set(EXTRA) | set(SIDE)))
        instance, context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {"cards_cdb": str(cdb)},
            },
        )
        await instance.on_load()
        try:
            content = await submit_deck(
                instance,
                context,
                code=deck_code(),
                deck_name="测试卡组",
                stream_id="stream-3",
                group_id="111",
            )
            assert "已收下「测试卡组」" in content, content

            listed = await instance.tool_list_decks(stream_id="stream-3", group_id="111")
            listed_text = str(listed["content"])
            assert "测试卡组" in listed_text, listed_text
            # 投稿默认不在随机池里，工具回话也要说清楚
            assert "不在随机池" in listed_text, listed_text
            # 卡组池是共享的：换个群号（或私聊）也该看到同一副，不再按群隔离
            other = await instance.tool_list_decks(stream_id="stream-9", group_id="999")
            assert "测试卡组" in str(other["content"]), other

            # 换成另一个群应当查不到
            other = await instance.tool_list_decks(stream_id="stream-4", group_id="222")
            # （原来这里断言"换个群号卡组池是空的"，即按群隔离；现在共享池，该断言已随需求删除）

            # 卡组码读不懂时要给出可读原因
            broken = await submit_deck(
                instance, context, code="这不是卡组码", stream_id="stream-3", group_id="111"
            )
            assert "没读懂" in broken, broken

            # 主卡组不够 40 张要被拦下（WindBot 会静默丢弃这种卡组）
            from duel.deckcode import Deck

            too_small = Deck(tuple(MAIN[:20]), (), ()).to_deck_code()
            rejected = await submit_deck(
                instance, context, code=too_small, stream_id="stream-3", group_id="111"
            )
            assert "主卡组" in rejected, rejected
        finally:
            await instance.on_unload()


async def test_status_and_stop_without_room() -> None:
    """没有对局时查状态与收摊都要给出明确回答。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            status = await instance.tool_status(stream_id="stream-5", group_id="111")
            assert "没有进行中的对局" in str(status["content"])

            stopped = await instance.tool_stop_duel(stream_id="stream-5", group_id="111")
            assert "没有进行中的对局" in str(stopped["content"])
        finally:
            await instance.on_unload()


async def test_on_unload_releases_data_dir() -> None:
    """on_unload 之后数据目录要能删掉（说明 sqlite 连接真的关了）。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory) / "data"
        instance, _context = make_plugin(data_dir)
        await instance.on_load()
        await instance.on_unload()
        (data_dir / "deck_pool.db").unlink()
        (data_dir / "decks").rmdir()
        # 2026-10-08 加的两样东西也在这个目录里，逐个删干净才算"句柄都放开了"：
        # 训练记录库（`train/training.db`，同样是一次连接都不能留着）与自动生成的面板密钥。
        (data_dir / "train" / "training.db").unlink()
        for sub in ("logs", "combos", "decks"):
            (data_dir / "train" / sub).rmdir()
        (data_dir / "train").rmdir()
        (data_dir / "webui_key.txt").unlink()
        data_dir.rmdir()


async def test_card_commands_roundtrip() -> None:
    """卡组指令走一遍：加、列表、随机池增删、详情、固定、删、清空，以及改游戏内名字。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # 卡库也自己造：插件自带的是真 cards.cdb，认不出测试卡号，投稿会被"卡库核对"拦下
        cdb = make_cards_cdb(root / "cards.cdb", sorted(set(MAIN) | set(EXTRA) | set(SIDE)))
        # 内置卡组清零（插件默认的 windbot_dir 指向自带的 clients/windbot，那里有 72 副）：
        # 卡组池只装这条用例自己投的牌，编号、随机池、清空这些断言才可复现
        instance, context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {
                    "cards_cdb": str(cdb),
                    "windbot_dir": str(empty_windbot_dir(root)),
                },
            },
        )
        await instance.on_load()
        try:
            group = "111"

            # 空池时的列表要给出引导，而不是空消息
            await instance.cmd_deck_list(stream_id="s1", group_id=group)
            assert "卡组池是空的" in context.send.messages[-1], context.send.messages[-1]

            # /加卡组 <卡组码> <名字>
            added = await instance.cmd_deck_add(
                text=f"/加卡组 {deck_code()} 测试卡组",
                stream_id="s1",
                group_id=group,
                user_id="u1",
                user_nickname="群友A",
            )
            assert added[0] is True and added[2] == 1, added
            assert "已收下「测试卡组」" in context.send.messages[-1], context.send.messages[-1]
            # 投稿默认不进随机池，回执要告诉群友怎么加进去
            assert "/加入随机 1" in context.send.messages[-1], context.send.messages[-1]

            # 列表要能列出编号与随机池状态
            await instance.cmd_deck_list(stream_id="s1", group_id=group)
            listing = context.send.messages[-1]
            assert "1. [✗随机] 测试卡组" in listing, listing
            assert "/固定卡组" in listing

            # /卡组详情 1
            await instance.cmd_deck_detail(text="/卡组详情 1", stream_id="s1", group_id=group)
            detail = context.send.messages[-1]
            assert "测试卡组" in detail and "随机池：不在" in detail, detail

            # /加入随机 1 → 随机池里就该有它，列表里的标记也要跟着变
            joined = await instance.cmd_random_add(text="/加入随机 1", stream_id="s1", group_id=group)
            assert joined[0] is True, joined
            assert "已把「测试卡组」加入随机池" in context.send.messages[-1], context.send.messages[-1]
            await instance.cmd_random_list(stream_id="s1", group_id=group)
            assert "测试卡组" in context.send.messages[-1], context.send.messages[-1]
            await instance.cmd_deck_list(stream_id="s1", group_id=group)
            assert "1. [✓随机] 测试卡组" in context.send.messages[-1], context.send.messages[-1]

            # 重复加入不必报错，但要说清状态没变
            again = await instance.cmd_random_add(text="/加入随机 1", stream_id="s1", group_id=group)
            assert again[0] is True and "已经在" in context.send.messages[-1], context.send.messages[-1]

            # /移出随机 1
            await instance.cmd_random_remove(text="/移出随机 1", stream_id="s1", group_id=group)
            assert "已把「测试卡组」移出随机池" in context.send.messages[-1], context.send.messages[-1]
            await instance.cmd_random_list(stream_id="s1", group_id=group)
            assert "随机池是空的" in context.send.messages[-1], context.send.messages[-1]

            # /加入随机 全部：一次把池子里所有卡组放进去
            await instance.cmd_random_add(text="/加入随机 全部", stream_id="s1", group_id=group)
            assert "1 副" in context.send.messages[-1], context.send.messages[-1]

            # 编号越界要给出可读提示
            out_of_range = await instance.cmd_random_add(text="/加入随机 9", stream_id="s1", group_id=group)
            assert out_of_range[0] is True and "没找到" in context.send.messages[-1], context.send.messages[-1]

            # /固定卡组 1 → 列表里要标注固定
            fixed = await instance.cmd_deck_fix(text="/固定卡组 1", stream_id="s1", group_id=group)
            assert fixed[0] is True
            await instance.cmd_deck_list(stream_id="s1", group_id=group)
            assert "★固定" in context.send.messages[-1], context.send.messages[-1]

            # 固定卡组被删掉时要连带取消固定，不能留下悬空引用
            await instance.cmd_deck_delete(text="/删卡组 1", stream_id="s1", group_id=group)
            assert "已删除「测试卡组」" in context.send.messages[-1]
            assert instance._deck_pool is not None
            assert instance._deck_pool.fixed_deck() is None, "删掉固定卡组后应当自动取消固定"

            # 再投两副后清空
            for name in ("甲", "乙"):
                await instance.cmd_deck_add(
                    text=f"/加卡组 {deck_code()} {name}", stream_id="s1", group_id=group
                )
            cleared = await instance.cmd_deck_clear(stream_id="s1", group_id=group)
            assert cleared[0] is True
            assert "2 副" in context.send.messages[-1], context.send.messages[-1]
            assert instance._deck_pool.count(group) == 0

            # 删不存在的编号要给出可读提示
            missing = await instance.cmd_deck_delete(text="/删卡组 9", stream_id="s1", group_id=group)
            assert "没找到" in context.send.messages[-1], context.send.messages[-1]
            assert missing[0] is True

            # /对局名字：设置、查询、恢复默认
            await instance.cmd_bot_name(text="/对局名字 憨憨", stream_id="s1")
            assert instance._resolve_bot_name() == "憨憨"
            await instance.cmd_bot_name(text="/对局名字", stream_id="s1")
            assert "憨憨" in context.send.messages[-1]
            await instance.cmd_bot_name(text="/对局名字 默认", stream_id="s1")
            assert instance._resolve_bot_name() == instance.config.duel.bot_name
            # 超长名字要被拦下（协议里名字字段是 20 个 UTF-16 字符）
            too_long = await instance.cmd_bot_name(text="/对局名字 " + "长" * 25, stream_id="s1")
            assert too_long[0] is True and "太长" in context.send.messages[-1]
            assert instance._resolve_bot_name() == instance.config.duel.bot_name
        finally:
            await instance.on_unload()


async def test_deck_rejected_when_cards_unknown_to_local_db() -> None:
    """卡库不认识的卡太多时要当场拒绝投稿。

    这是实测踩出来的：群友投了一副 55 张里 35 张本地卡库没有的卡组，收下之后
    WindBot 一个卡都不认识，整局只发动了 1 次效果、没召唤也没攻击，
    表现为「机器人一张牌都不出」。所以要在投稿时就拦住并说清原因。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # 卡库里只有青眼白龙，测试卡组里其余 40+ 张都不认识
        cdb = make_cards_cdb(root / "cards.cdb", [89631139])
        instance, context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {"cards_cdb": str(cdb)},
            },
        )
        await instance.on_load()
        try:
            content = await submit_deck(
                instance, context, code=deck_code(), deck_name="新卡卡组", stream_id="s1", group_id="111"
            )
            assert "本地卡库不认识" in content, content
            assert "更新本地卡库" in content, content
            assert instance._deck_pool is not None
            assert instance._deck_pool.count("111") == 0, "被拒绝的卡组不该进池子"
        finally:
            await instance.on_unload()


# ⚠ 这里原有两条测试：`test_submit_deck_launches_script_generation`
# （投稿后要真的把"现写专属脚本"那步启动起来）与 `test_script_generation_is_serialised`
# （连投几副时生成要串行，免得并发编译同一份源码树互相踩）。
# **2026-10-07 用户口径：插件不再自动写脚本**——新导入的卡组一律用通用脚本，
# 专属出牌脚本改由 agent 按 executors/README.md 多轮迭代着写，所以这两条连同被测功能一起删。


async def test_unload_is_fast_and_kills_rooms() -> None:
    """卸载必须在宿主的预算内返回，并且真的把房间进程杀掉。

    这条是回归测试：宿主给插件卸载的预算是 5 秒，而我们原先在 ``on_unload`` 里
    ``await session.stop()``（优雅收尾要对 WindBot 与内核各等最多 10 秒）——对局在跑时
    必然超时，宿主记 ``plugin.shutdown 超时`` 并重启插件，对局里的连接就断了
    （用户看到的现象是"用 /查房 会断掉连接"，其实跟哪条指令无关）。
    所以卸载只做"立刻杀进程、关端口"，一次都不等。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, _context = make_plugin(
            root / "data", {"plugin": {"enabled": True, "config_version": "1.0.0"}}
        )
        module = load_plugin_module()
        await instance.on_load()

        class HangingSession:
            """优雅收尾会卡住的会话替身：卸载如果去 await 它，就会超时。"""

            def __init__(self) -> None:
                self.killed = False
                self.stopped = False

            async def stop(self) -> None:  # pragma: no cover - 正常路径不该走到
                self.stopped = True
                await asyncio.sleep(30)

            def kill_now(self) -> None:
                self.killed = True

        session = HangingSession()
        instance._rooms["s1"] = module.ActiveRoom(
            session, "s1", "111", asyncio.create_task(asyncio.sleep(0))
        )

        started = asyncio.get_running_loop().time()
        await instance.on_unload()
        elapsed = asyncio.get_running_loop().time() - started

        assert session.killed is True, "卸载必须立刻结束房间进程"
        assert session.stopped is False, "卸载不该走会卡住的优雅收尾"
        assert elapsed < 1.0, f"卸载用了 {elapsed:.2f} 秒，超出宿主给卸载的预算"


# ⚠ 这里原来有三条用例：`test_training_helpers`（`/训练` 的轮数解析与结果整理）、
# `test_optimize_summary_reports_child_errors_verbatim`（`/优化卡组` 的子进程序言报错不能被
# 说成"没有候选通过"）、`test_optimize_milestone_picks_only_key_lines`（优化进度里程碑）。
# 卡组训练与调优已按 2026-10-07 用户口径整块删除（指令与私有方法都没了），被测对象不存在，
# 三条用例一并删掉。


# ⚠ 这里原来有一条 `test_room_brain_starts_and_stops`：开了 ai_brain 时房间要起"逐步问 AI"的
# 答复任务、把问答前缀交给 WindBot，收摊时清掉问答文件并取消任务。AI 打牌整条链路
# （`_start_room_brain` / `_stop_room_brain` / `_brain_tasks` / brain_model.toml）已按
# 2026-10-07 用户口径删除，这条用例一并删掉。

# （`test_script_generation_is_serialised` 也在这条口径下去掉了：它测的是"连投几副时生成要排队"，
# 而投稿已经不再触发任何生成——见上面那条注释。）

async def test_deck_ai_scope_overrides_the_global_brain_switches() -> None:
    """卡组页里给每副牌设的「AI 决策档位」要真的改这一局决策层开哪半。

    这条是回归测试：档位原来用 `getattr(deck, "brain_scope", "")` 读，而 `StoredDeck`
    **根本没有这个字段** —— 于是 getattr 永远回落到全局配置，面板上改了档位、
    开房时却按全局走，用户看到的是"设置了没用"。所以这里既测"档位能写进去、读得回来"
    （新库也要有这一列），也测它真的映射到 `(问目标, 问要不要交)` 两个开关上。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    # 从插件模块里拿异常类型：`train.runner` 里有 `from ..duel...` 这种跨层相对导入，
    # 单独 `import train.runner` 会报"attempted relative import beyond top-level package"
    # （插件只有以包的形式加载时那些相对导入才成立，宿主就是这么加载它的）
    TrainingError = load_plugin_module().TrainingError

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, _context = make_plugin(root / "data")
        await instance.on_load()
        try:
            pool = instance._deck_pool
            assert pool is not None, "on_load 该把卡组池建出来"

            def deck_row() -> Any:
                """读回库里这一行（池子没有"按编号取一副"的接口，从列表里挑）。"""

                return next(item for item in pool.list_decks("123456") if item.deck_id == stored.deck_id)

            stored = pool.add(
                group_id="123456",
                display_name="测试牌",
                contributor_id="u",
                contributor_name="群友",
                ydk_text="#main\n100\n!side\n",
                deck_code="",
                source_format="ydk",
                main_count=1,
                extra_count=0,
                side_count=0,
            )
            # 默认：这副牌没设档位 → 跟随全局（全局是"问目标、不问要不要交"）
            instance.config.duel.brain_enabled = True
            instance.config.duel.brain_negate_gate = False
            assert instance._brain_switches_for(None) == (True, False)
            assert instance._brain_switches_for(deck_row()) == (True, False)

            # 换成"目标 + 要不要交"：这一副牌自己覆盖全局
            message = await instance._deck_settings(stored.deck_id, in_random=True, brain_scope="full")
            assert "AI 决策 full" in message, message
            assert "随机池开" in message, message
            assert deck_row().brain_scope == "full"
            assert deck_row().in_random is True
            assert instance._brain_switches_for(deck_row()) == (True, True)

            # 两支决策层全关
            await instance._deck_settings(stored.deck_id, in_random=None, brain_scope="off")
            assert instance._brain_switches_for(deck_row()) == (False, False)

            # 只问目标
            await instance._deck_settings(stored.deck_id, in_random=None, brain_scope="target_only")
            assert instance._brain_switches_for(deck_row()) == (True, False)

            # 清空（面板上"跟随全局"那个选项送的就是空串，不是它的中文标签）
            message = await instance._deck_settings(stored.deck_id, in_random=None, brain_scope="")
            assert "跟随全局" in message, message
            assert deck_row().brain_scope == "", "空串＝跟随全局"

            # 认不出的档位：当场报错，而且**不能**把库里那行改坏
            try:
                await instance._deck_settings(stored.deck_id, in_random=None, brain_scope="随便写的")
            except TrainingError as exc:
                assert "不认识的 AI 决策档位" in str(exc), exc
            else:
                raise AssertionError("认不出的档位不该被写进库")
            assert deck_row().brain_scope == "", "写失败不该改动已有设置"
        finally:
            await instance.on_unload()


async def test_generated_script_wins_over_deck_style() -> None:
    """卡组库里的 `generated_script` 要优先于按卡表重猜出来的风格。

    （这条用例原来叫 `test_ai_brain_keeps_the_deck_script`，测的是"开逐步问 AI 不该把卡组自己的
    出牌脚本换成 PlanAware"。AI 打牌已按 2026-10-07 用户口径删除、`PLAN_AWARE_STYLE` 也没了，
    留下的是**保下来**的这条优先级：`_with_current_style` 里 `picked_style` / `generated_script`
    先于「按卡表相似度挑脚本」——那是 agent 手工写的执行器，不能被自动匹配顶掉。）
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, _context = make_plugin(root / "data")
        from duel.deckpool import StoredDeck

        deck = StoredDeck(
            deck_id=88,
            group_id="__builtin__",  # 被 is_builtin 认成内置卡组，但下面会替换掉这个字段
            display_name="升辉月",
            contributor_id="",
            contributor_name="",
            ydk_path=root / "deck.ydk",
            source_format="ourygo",
            main_count=40,
            extra_count=15,
            side_count=15,
            windbot_deck="Test",
            generated_script="Gen88",
        )
        deck = dataclasses.replace(deck, group_id="123456")  # 投稿卡组（非内置）
        resolved = instance._with_current_style(deck)
        assert resolved.windbot_deck == "Gen88", resolved.windbot_deck


async def test_field_command_reads_live_session() -> None:
    """``/查房`` 要能在对局进行中报出局面，且不碰坏会话。

    这里用的是**真实的 DuelSession**（不是替身）：这条指令读的是会话上的
    ``started`` / ``turn_count`` / ``field_snapshot``——它们必须是会话真的提供的接口，
    替身很容易把接口不匹配糊过去（上报"断掉连接"就是这个位置出的问题）。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, context = make_plugin(
            root / "data", {"plugin": {"enabled": True, "config_version": "1.0.0"}}
        )
        module = load_plugin_module()
        await instance.on_load()
        try:
            from duel.protocol import DuelEvent
            from duel.session import DuelSession, SessionConfig

            config = SessionConfig(
                ygopro_executable=Path("ygopro.exe"),
                ygopro_dir=root,
                windbot_executable=Path("WindBot.exe"),
                windbot_dir=root,
                listen_port=0,
            )
            session = DuelSession(config, group_id="111")
            recorder = session.recorder()
            recorder._set_player_name(0, "麦麦")
            recorder._set_player_name(1, "群友A")
            recorder.room_seat = 0
            recorder.duel_index = 0
            recorder._refresh_names()
            recorder._apply(DuelEvent("lp_update", player=0, value=5200))
            recorder._apply(DuelEvent("lp_update", player=1, value=3100))
            recorder._apply(DuelEvent("new_turn", player=0))
            recorder.started = True
            instance._rooms["s1"] = module.ActiveRoom(
                session, "s1", "111", asyncio.create_task(asyncio.sleep(0))
            )

            # 开打之后本流房间发的是**图**（2026-10-07 起）：图里带上双方名字与 LP
            handled, summary, _priority = await instance.cmd_field(stream_id="s1", group_id="111")
            assert handled is True, summary
            assert context.send.images, "本流房间应当发棋盘图"
            page = context.render.pages[-1]
            for needle in ("麦麦", "5200", "群友A", "3100"):
                assert needle in page, (needle, page[:400])
            # 会话本身不受影响：还能继续用（真正的实现里 kill_now 才是收摊）
            assert session.kill_now is not None
            # 渲染挂掉时必须退回文字（查房不能因为出图失败就不报局面）
            context.render.fail = True
            recorder.finished = True
            before = len(context.send.messages)
            await instance.cmd_field(stream_id="s1", group_id="111")
            assert len(context.send.messages) > before, "渲染失败要退文本"
            assert "已经打完" in context.send.messages[-1]
        finally:
            await instance.on_unload()


async def test_field_command_without_room() -> None:
    """没有对局时 ``/查房`` 要给出可读提示，而不是抛异常或沉默。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(
            Path(directory) / "data", {"plugin": {"enabled": True, "config_version": "1.0.0"}}
        )
        await instance.on_load()
        try:
            handled, _summary, _priority = await instance.cmd_field(stream_id="s9")
            assert handled is True
            assert "没有进行中的对局" in context.send.messages[-1]
        finally:
            await instance.on_unload()


async def test_deck_accepted_and_noted_when_cards_known() -> None:
    """卡库全认识时要收下，并在回执里说明核对通过。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cdb = make_cards_cdb(root / "cards.cdb", sorted(set(MAIN) | set(EXTRA) | set(SIDE)))
        instance, context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {"cards_cdb": str(cdb)},
            },
        )
        await instance.on_load()
        try:
            content = await submit_deck(
                instance, context, code=deck_code(), deck_name="全认识", stream_id="s1", group_id="111"
            )
            assert "卡库核对通过" in content, content
            assert instance._deck_pool is not None
            assert instance._deck_pool.count("111") == 1

            # 只有少量卡不认识时收下但提示，而不是一刀切拒绝
            partial = make_cards_cdb(
                root / "partial.cdb", sorted(set(MAIN) | set(EXTRA) | set(SIDE))[:30]
            )
            previous_db = instance._card_db
            instance._card_db = type(previous_db)(partial) if previous_db is not None else None
            if previous_db is not None:
                # 不关掉旧连接的话，Windows 上临时目录会删不掉
                previous_db.close()
            content = await submit_deck(
                instance, context, code=deck_code(), deck_name="部分认识", stream_id="s1", group_id="111"
            )
            assert "已收下「部分认识」" in content, content
            assert "张本地卡库不认识" in content, content
        finally:
            await instance.on_unload()


class _StubSession:
    """`_run_room` 需要的会话替身：只回放一份结果，不真起进程。"""

    def __init__(self, *, outcome: str, summary: List[str], result: Dict[str, Any]) -> None:
        self._outcome = outcome
        self._summary = summary
        self._result = result
        self.stopped = False

    async def wait_finished(self) -> str:
        """返回预设的结束原因。"""

        return self._outcome

    def summary_lines(self) -> List[str]:
        """返回预设的过程复述。"""

        return list(self._summary)

    def result_dict(self) -> Dict[str, Any]:
        """返回预设的结构化结果。"""

        return dict(self._result)

    async def stop(self) -> None:
        """记录收摊。"""

        self.stopped = True


# ⚠ 这里原来还有两个替身 `_StubSeat` / `_StubRecorder`（模拟记录器：我方座位、双方实时生命值、
# 双方用卡台账）。它们只服务于"打完把胜负回填决策日志"与"真实对局落库"这两条用例——
# 那两条链路（`_finish_room_brain` / `_record_room_duel` + train/store.py）已按
# 2026-10-07 用户口径删除，替身也一并删掉。`_StubSession` 还在用（下面测收尾播报那条）。


# ⚠ 这里原来有三条用例：`test_room_brain_backfills_duel_outcome`（打完把胜负回填决策日志）、
# `test_brain_mode_command_sets_per_deck_scope`（`/出牌模式` 按卡组切问 AI 档位）、
# `test_finished_room_duel_goes_into_the_store`（真实对局写进结果库，arena="room"）。
# 决策日志 / 知识库 / 问 AI 档位 / 对局落库随 AI 打牌与训练调优整条链路一起删了
#（2026-10-07 用户口径），三条用例连同它们的记录器替身一并删掉。


def test_open_message_hides_the_deck_but_keeps_pool_hint() -> None:
    """开房播报**不再说用哪副牌**（用户要求：每次都提醒一次太吵），但"挑不出卡组"的说明要留着。

    护栏来源：群友玩的是同一副升辉月，播报「这局机器人用随机抽到的「升辉月」」纯属噪音；
    而"随机池是空的、发 /加入随机 放牌进来"是**可操作**的提示，删了群友就不知道怎么加牌。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        module = load_plugin_module()
        info = module.SessionInfo(
            host="1.2.3.4",
            port=1234,
            password="pw",
            deck=None,
            backend_port=2234,
        )
        plain = instance._compose_open_message(info, "mdpro3")
        assert "房间开好了" in plain, plain
        assert "1.2.3.4:1234" in plain, plain
        # 正常抽到牌时不再播报用哪副（这里模拟"挑到了牌"→ hint 传空）
        assert "这局机器人用" not in plain, plain
        # 挑不出卡组时的说明要留着
        hint = "本群的随机池是空的，这局机器人用它自带的卡组「Test」。发 /加入随机 全部 放进随机池。"
        with_hint = instance._compose_open_message(info, "mdpro3", hint)
        assert "/加入随机" in with_hint, with_hint
        # 本机地址要给出那行提醒（这条与卡组无关，不能一起删掉）
        local = instance._compose_open_message(
            module.SessionInfo(host="127.0.0.1", port=1234, password="pw", deck=None, backend_port=1),
            "mdpro3",
        )
        assert "只有和机器人在同一台机器上" in local, local


async def test_open_message_calls_the_bot_by_its_name() -> None:
    """开房播报里要写出机器人叫什么（用户要求："改成叫憨憨"）。

    名字来源与对局一致：卡组池里的 ``in_game_bot_name`` 优先（``/对局名`` 改的就是它），
    没有才回落到配置里的 ``duel.bot_name``。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        module = load_plugin_module()
        await instance.on_load()
        try:
            info = module.SessionInfo(
                host="1.2.3.4", port=1234, password="pw", deck=None, backend_port=2234
            )
            instance._deck_pool.set_setting(module.SETTING_BOT_NAME, "憨憨")
            plain = instance._compose_open_message(info, "mdpro3")
            assert "憨憨" in plain, plain
            assert "房间开好了" in plain, plain
            # 清掉设置后回落到配置里的默认名
            instance._deck_pool.set_setting(module.SETTING_BOT_NAME, None)
            fallback = instance._compose_open_message(info, "mdpro3")
            assert instance.config.duel.bot_name in fallback, fallback
        finally:
            await instance.on_unload()


# ⚠ 这里原来有一条 `test_invite_text_judges_when_to_speak`：空闲主动约战的"该不该发、发什么"
# （关着 / 正在打 / 没开过房三种情况都不发）。`invite_*` 配置、`_invite_stream_id` 与
# `_compose_invite_text` 已按 2026-10-07 用户口径删除，这条用例一并删掉。


async def test_finished_duel_summary_goes_through_the_model() -> None:
    """打完的总结先交给模型写成一段人话，再由插件直接发到群里（不经过 planner）。

    这是群友的实际反馈：原先发的是「回合数 / 双方统计」那样的字段列表。改成走模型之后，
    群里收到的是一条口语化播报；planner 那条路也去掉了——那一轮会被其它插件的规则拦掉，
    而收尾播报不该受别的插件影响。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            session = _StubSession(
                outcome="finished",
                summary=["麦麦 获胜（对手 LP 归零）", "共 4 回合", "麦麦：召唤 3 次、攻击 2 次"],
                result={"winner_name": "麦麦", "turns": 4, "reason": "lp_zero", "deck_name": "青眼白龙"},
            )
            await instance._run_room(session, "stream-1", "111")

            assert session.stopped is True
            assert len(context.send.messages) == 1, context.send.messages
            assert context.send.messages[0] == context.llm.reply, "发出去的应当是模型写的那段话"
            assert context.llm.prompts, "要真把复述交给模型"
            assert "麦麦 获胜" in context.llm.prompts[0], "复述事实必须进提示词"
            assert "4" in context.llm.prompts[0], "回合数也要给模型"
            # 事实照样写进上下文，麦麦之后聊天时记得这一局
            assert len(context.maisaka.appended) == 1
            assert context.maisaka.appended[0] == context.llm.reply
            assert context.maisaka.triggered == [], "不该再强制唤起 planner 了"

            # 模型不可用时的降级（用户 2026-10-07 要求）：只留胜负 + 回合数 + 卡组名，
            # 不再把「回合数 / 双方统计」那种字段列表整段发出去
            context.llm.fail = True
            context.send.messages.clear()
            await instance._run_room(
                _StubSession(
                    outcome="finished",
                    summary=["麦麦 获胜（对手 LP 归零）", "共 6 回合", "麦麦：召唤 3 次、攻击 2 次"],
                    result={
                        "winner_name": "麦麦",
                        "winner_is_self": True,
                        "turns": 6,
                        "deck_name": "青眼白龙",
                    },
                ),
                "stream-2",
                "111",
            )
            fallback = context.send.messages[0]
            assert "赢了" in fallback, fallback
            assert "6" in fallback and "青眼白龙" in fallback, fallback
            assert "召唤" not in fallback, f"润色失败时不该再发字段列表：{fallback}"

            # 关掉模型润色时直接发原始复述
            instance.config.duel.summarize_with_ai = False
            context.llm.fail = False
            context.send.messages.clear()
            await instance._run_room(
                _StubSession(outcome="finished", summary=["麦麦 获胜"], result={"winner_name": "麦麦"}),
                "stream-3",
                "111",
            )
            assert "麦麦 获胜" in context.send.messages[0], context.send.messages[0]

            # 没人进来属于收摊通知，要可靠直发（否则群里什么都看不到）
            context.send.messages.clear()
            await instance._run_room(
                _StubSession(outcome="no_player", summary=[], result={}), "stream-4", "111"
            )
            assert context.send.messages, "收摊通知不能只靠模型转述"
            assert "没人进来" in context.send.messages[0]
        finally:
            await instance.on_unload()


async def test_config_update_keeps_the_card_db_alive_while_a_room_runs() -> None:
    """配置热更新**不能**把正在打的那局手里的卡库关掉。

    房间的 `DuelSession`（记录器/查房）与决策层 `BrainBridge` 都是**直接引用卡库实例**
    （不是回调），`on_config_update` 当场 `close()` 的话，它们下一次查卡名/卡文会抛
    `sqlite3.ProgrammingError: Cannot operate on a closed database`——
    用户看到的现象是"改了配置之后这局的 AI 决策就不动了"。

    所以换下来但还有房间在用的旧卡库要留着，等房间空了（`_close_retired_card_dbs`）再关。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            old_db = instance._card_db
            assert old_db is not None and old_db.available, "on_load 该把卡库建出来"
            # 假装有一局正在打（会话替身够用：这里只关心"房间占用"这件事）
            instance._rooms["s1"] = load_plugin_module().ActiveRoom(
                session=_StubSession(outcome="finished", summary=[], result={}),
                stream_id="s1",
                group_id="111",
                task=asyncio.create_task(asyncio.sleep(5)),
                deck_name="升鹏月",
            )

            await instance.on_config_update("self", {}, "1.3.0")
            assert instance._card_db is not old_db, "配置热更新要换上新的卡库实例"
            assert instance._retired_card_dbs == [old_db], "旧卡库要挂起来，而不是当场关掉"

            # 关键断言：旧实例还能用（真查一次库；close 过的连接到这里会抛 ProgrammingError）
            sample = old_db.card_details([89631139])
            assert sample, "房间还握着的那份卡库必须仍然可查"

            # 房间空了：现在才关
            instance._rooms.clear()
            instance._close_retired_card_dbs()
            assert instance._retired_card_dbs == [], "房间空了就该把旧卡库收掉"
        finally:
            for room in list(instance._rooms.values()):
                room.task.cancel()
            await instance.on_unload()


async def test_finished_room_duel_lands_in_the_review_material() -> None:
    """每局打完要落一条 `kind=duel` 记录，而且**带着卡组编号**——复盘优化靠它取最近几局。

    这条是回归测试：第一版只把 `stream_id` / `group_id` 写进参数，而「复盘优化」是按
    这副牌的编号筛的（`_recent_duels`），于是那份记录谁也匹配不上，
    用户点"复盘优化"永远得到"还没有对局记录"。所以这里既查记录存在，也查能按编号取回来。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            store = instance.training_store()
            assert store is not None, "训练开着时该有记录库"

            await instance._run_room(
                _StubSession(
                    outcome="finished",
                    summary=["麦麦 获胜（对手 LP 归零）"],
                    result={"winner_name": "麦麦", "turns": 5, "deck_name": "青眼白龙"},
                ),
                "stream-1",
                "111",
                deck_id=88,
            )
            duels = store.list_recent(limit=10, kind="duel")
            assert len(duels) == 1, [record.title for record in duels]
            assert "青眼白龙" in duels[0].title, duels[0].title
            assert duels[0].params["deck_id"] == 88, duels[0].params
            assert duels[0].summary["turns"] == 5, duels[0].summary

            runner = instance.training_runner()
            assert runner is not None, "训练开着时该有执行器"
            assert [record.run_id for record in runner._recent_duels(88, 3)] == [duels[0].run_id]
            assert runner._recent_duels(99, 3) == [], "别的牌的对局不能算进这副牌"

            # 没人进来 / 中途收摊不算素材（只记真的打完的）
            await instance._run_room(
                _StubSession(outcome="no_player", summary=[], result={}), "stream-2", "111", deck_id=88
            )
            assert len(store.list_recent(limit=10, kind="duel")) == 1
        finally:
            await instance.on_unload()


async def test_taunt_lines_and_summary_prompt_config_reach_consumers() -> None:
    """台词池与总结提示词这两个配置项要真的送到用它们的地方（2026-10-07 用户要求可配）。

    两处都容易"配了没生效"：台词池要经过 `SessionConfig` 才进 `TauntPicker`；
    总结提示词要经过占位符渲染才拼成给模型的提示词——所以两头都断言。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(
            Path(directory),
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "duel": {
                    "taunt_lines": ["这局的剧本我写好了", "轮到你了，别发呆"],
                    "summary_prompt": "一句话总结：{report}｜{verdict}｜{turns}｜{winner}｜{self_name}",
                },
            },
        )
        await instance.on_load()
        try:
            # ① 台词池：配置 → SessionConfig → 挑台词的人
            session_config = instance._build_session_config("s1")
            assert tuple(session_config.taunt_lines) == (
                "这局的剧本我写好了",
                "轮到你了，别发呆",
            ), session_config.taunt_lines

            # ② 总结提示词：五个占位符都换上真值
            rendered = instance._render_summary_prompt(
                report="（对局记录）", verdict="我方赢了", winner="麦麦", turns=7, self_name="麦麦"
            )
            assert rendered == "一句话总结：（对局记录）｜我方赢了｜7｜麦麦｜麦麦", rendered

            # ③ 回合数未知时占位符给"未知"，不能是 "None"
            assert "未知" in instance._render_summary_prompt(
                report="r", verdict="v", winner="", turns=None, self_name="麦麦"
            )

            # ④ 占位符写坏（写了个不存在的名字）时不能拿半截提示词去问模型
            instance.config.duel.summary_prompt = "坏占位符 {nope}"
            assert (
                instance._render_summary_prompt(
                    report="r", verdict="v", winner="w", turns=1, self_name="麦麦"
                )
                == ""
            )
        finally:
            await instance.on_unload()


async def test_wiki_endpoint_config_is_normalised() -> None:
    """百科的查询地址可配，且末尾斜杠怎么填都能拼出正确的搜索 URL。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            instance.config.wiki.endpoint = "https://example.invalid/api/v0"
            assert instance._endpoint() == "https://example.invalid/api/v0/"
            instance.config.wiki.endpoint = "https://example.invalid/api/v0/"
            assert instance._endpoint() == "https://example.invalid/api/v0/"
        finally:
            await instance.on_unload()


async def test_field_command_sends_board_image() -> None:
    """本流房间开打后 `/查房` 发**棋盘图**；渲染失败时退回纯文本（2026-10-07 加）。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            from duel.fieldstate import FieldState, ZoneCard  # noqa: PLC0415  测试里按需引入

            state = FieldState(start_lp=8000)
            state.players[0].lp = 6200
            state.players[1].lp = 3400
            state.turn_count = 5
            state.zones[(0, 4, 2)] = ZoneCard(76072561, 0x1)     # 我方表侧怪
            state.zones[(1, 8, 1)] = ZoneCard(96205925, 0xA)     # 对手里侧的魔陷（只能画卡背）

            active_room_cls = load_plugin_module().ActiveRoom
            info = type("Info", (), {"host": "127.0.0.1", "port": 7911, "advertised_port": 7911})()
            task = asyncio.create_task(asyncio.sleep(3600))
            instance._rooms["s9"] = active_room_cls(
                make_playing_room(
                    session_info=info,
                    state=state,
                    lines=["憨憨：LP 6200｜怪兽 1｜魔陷 0", "打憨憨：LP 3400｜怪兽 0｜魔陷 1"],
                ),
                "s9",
                "111",
                task,
            )
            try:
                # ① 正常路径：发的是图，而且图里画了卡背（对手那张是里侧的）
                await instance.cmd_field(stream_id="s9", group_id="111")
                assert context.send.images, "查房应当发图"
                assert context.send.images[-1] == "ZmFrZXBuZw==", context.send.images[-1]
                page = context.render.pages[-1]
                assert "卡背" in page, "里侧的卡只画卡背"
                assert "异解△福音" not in page, "里侧的卡不能把卡名画出来"
                assert "6200" in page and "3400" in page, "双方 LP 要在图上"
                # 出图要带上"等图片画完"的渲染参数：卡图没解码完就截屏是实测踩过的坑
                assert context.render.calls[-1]["wait_until"] == "networkidle"
                assert context.render.calls[-1]["wait_for_timeout_ms"] > 0

                # ② 渲染失败：退回纯文本，不能什么都不发
                context.render.fail = True
                before = len(context.send.messages)
                await instance.cmd_field(stream_id="s9", group_id="111")
                assert len(context.send.messages) > before, "渲染失败要退文本"
                assert "第 5 回合" in context.send.messages[-1], context.send.messages[-1]
            finally:
                task.cancel()
        finally:
            await instance.on_unload()


async def test_field_command_renders_image_for_another_group() -> None:
    """在**别的群**问 `/查房` 也要出棋盘图（2026-10-07 用户报"其他群不能渲染查房的图片"）。

    原先只认本流：本流没有房间就直接走文字，哪怕别处正打得热闹。现在本流没开打时画
    **最近开打的那个房间**，并在图上写清是谁家的棋盘（群名来自 `chat.get_all_streams`）。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            context.chat.streams = [
                {
                    "session_id": "s9",
                    "is_group_session": True,
                    "group_name": "游戏王测试群",
                    "user_nickname": "",
                },
                {
                    "session_id": "s7",
                    "is_group_session": True,
                    "group_name": "隔壁群",
                    "user_nickname": "",
                },
            ]
            from duel.fieldstate import FieldState, ZoneCard  # noqa: PLC0415  测试里按需引入

            state = FieldState(start_lp=8000)
            state.players[0].lp = 5100
            state.players[1].lp = 7300
            state.turn_count = 3
            state.zones[(0, 4, 1)] = ZoneCard(89631139, 0x1)

            active_room_cls = load_plugin_module().ActiveRoom
            info = type("Info", (), {"host": "127.0.0.1", "port": 7911, "advertised_port": 7911})()
            task = asyncio.create_task(asyncio.sleep(3600))
            # 房间开在 s9，问的人在 s7：本流一个房间都没有
            instance._rooms["s9"] = active_room_cls(
                make_playing_room(session_info=info, state=state, names=("麦麦", "群友")), "s9", "111", task
            )
            try:
                await instance.cmd_field(stream_id="s7", group_id="222")
                assert context.send.images, "在别的群问 /查房 也要出图"
                page = context.render.pages[-1]
                assert "游戏王测试群" in page, "图上要写清这是哪个群的棋盘"
                assert "5100" in page and "7300" in page, "双方 LP 要在图上"
            finally:
                task.cancel()
        finally:
            await instance.on_unload()


async def test_field_command_reports_lp_and_board() -> None:
    """`/查房` 要报出双方生命值、怪兽数与魔陷数；没有对局时给出可读提示。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            # 没有房间时要引导怎么开局
            await instance.cmd_field(stream_id="s1", group_id="111")
            assert "没有进行中的对局" in context.send.messages[-1], context.send.messages[-1]

            # 造一个「房间开着但还没开打」的房间
            class _WaitingSession:
                """还没开打的会话替身。"""

                started = False
                finished = False
                turn_count = 0
                info = type("Info", (), {"host": "127.0.0.1", "port": 7911, "advertised_port": 7911})()
                gate = type("Gate", (), {"status": type("S", (), {"bot_connected": True})()})()

                async def stop(self) -> None:
                    """空实现。"""

            # 用宿主加载器那份模块里的类：它和插件实例采用的是同一份实现
            active_room_cls = load_plugin_module().ActiveRoom

            task = asyncio.create_task(asyncio.sleep(3600))
            instance._rooms["s2"] = active_room_cls(_WaitingSession(), "s2", "111", task)
            try:
                await instance.cmd_field(stream_id="s2", group_id="111")
                assert "还没开打" in context.send.messages[-1], context.send.messages[-1]
            finally:
                task.cancel()

            # 造一个打到一半的局面：自己 LP 6200 两只怪兽一张魔陷，对手 LP 3400 一只怪兽
            from duel.fieldstate import FieldState  # noqa: PLC0415  测试里按需引入
            from duel.protocol import CardLocation, DuelEvent  # noqa: PLC0415

            field = FieldState(start_lp=8000)
            field.apply(DuelEvent("lp_update", player=1, value=6200))
            field.apply(DuelEvent("lp_update", player=0, value=3400))
            field.apply(
                DuelEvent(
                    "move",
                    player=1,
                    card_id=89631139,
                    extra=int(CardLocation.MONSTER_ZONE),
                    data=(1, int(CardLocation.HAND), 0, 1, int(CardLocation.MONSTER_ZONE), 0),
                )
            )
            field.apply(
                DuelEvent(
                    "move",
                    player=1,
                    card_id=10000,
                    extra=int(CardLocation.MONSTER_ZONE),
                    data=(1, int(CardLocation.HAND), 1, 1, int(CardLocation.MONSTER_ZONE), 1),
                )
            )
            field.apply(
                DuelEvent(
                    "move",
                    player=1,
                    card_id=44095762,
                    extra=int(CardLocation.SPELL_ZONE),
                    data=(1, int(CardLocation.HAND), 2, 1, int(CardLocation.SPELL_ZONE), 0),
                )
            )
            field.apply(
                DuelEvent(
                    "move",
                    player=0,
                    card_id=12345,
                    extra=int(CardLocation.MONSTER_ZONE),
                    data=(0, int(CardLocation.HAND), 0, 0, int(CardLocation.MONSTER_ZONE), 0),
                )
            )

            class _PlayingSession:
                """打到一半的会话替身：只暴露查房要用的接口。"""

                started = True
                finished = False
                turn_count = 5
                info = type("Info", (), {"host": "127.0.0.1", "port": 7911, "advertised_port": 7911})()
                gate = type("Gate", (), {"status": type("S", (), {"bot_connected": True})()})()

                def field_snapshot(self) -> List[str]:
                    """返回预先造好的局面。"""

                    return ["憨憨：LP 6200｜怪兽 2｜魔陷 1", "打憨憨：LP 3400｜怪兽 1｜魔陷 0"]

                async def stop(self) -> None:
                    """空实现。"""

            task = asyncio.create_task(asyncio.sleep(3600))
            instance._rooms["s3"] = active_room_cls(_PlayingSession(), "s3", "111", task)
            try:
                await instance.cmd_field(stream_id="s3", group_id="111")
                report = context.send.messages[-1]
                assert "第 5 回合" in report, report
                assert "憨憨：LP 6200｜怪兽 2｜魔陷 1" in report, report
                assert "打憨憨：LP 3400｜怪兽 1｜魔陷 0" in report, report
            finally:
                task.cancel()
        finally:
            await instance.on_unload()


async def test_field_command_lists_rooms_across_streams() -> None:
    """``/查房`` 要报出**所有**对话流的房间：群名 + 卡组名 + 回合 + 场面。

    这是群友提的需求（2026-10-07）：只报本流时，别的群开着的房间成了盲区——
    群里问"还有谁在打"没人答得上来。名字必须从宿主聊天流列表里取（群名 / "xx 的私聊"），
    给群友报会话号没有意义。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            active_room_cls = load_plugin_module().ActiveRoom
            context.chat.streams = [
                {
                    "session_id": "s-own",
                    "is_group_session": True,
                    "group_name": "一号群",
                    "user_nickname": "",
                },
                {
                    "session_id": "s-other",
                    "is_group_session": True,
                    "group_name": "隔壁测试群",
                    "user_nickname": "",
                },
                {
                    "session_id": "s-private",
                    "is_group_session": False,
                    "group_name": "",
                    "user_nickname": "群友B",
                },
            ]

            class _Session:
                """查房要用的最小会话替身：开打中、给出回合数与场面。"""

                def __init__(self, turn: int, snapshot: List[str]) -> None:
                    self.started = True
                    self.finished = False
                    self.turn_count = turn
                    self._snapshot = snapshot

                def field_snapshot(self) -> List[str]:
                    """返回造好的场面。"""

                    return list(self._snapshot)

                async def stop(self) -> None:
                    """空实现。"""

            tasks = [asyncio.create_task(asyncio.sleep(3600)) for _ in range(3)]
            instance._rooms["s-own"] = active_room_cls(
                _Session(3, ["憨憨：LP 8000｜怪兽 1｜魔陷 0"]),
                "s-own",
                "111",
                tasks[0],
                deck_name="青眼白龙",
            )
            instance._rooms["s-other"] = active_room_cls(
                _Session(7, ["憨憨：LP 4200｜怪兽 2｜魔陷 1", "群友A：LP 1900｜怪兽 0｜魔陷 0"]),
                "s-other",
                "222",
                tasks[1],
                deck_name="卡通",
            )
            instance._rooms["s-private"] = active_room_cls(
                _Session(1, ["憨憨：LP 8000｜怪兽 0｜魔陷 0"]),
                "s-private",
                "333",
                tasks[2],
                deck_name="闪刀",
            )
            try:
                handled, summary, _priority = await instance.cmd_field(
                    stream_id="s-own", group_id="111"
                )
                report = context.send.messages[-1]
                assert handled is True, summary
                assert "3 个房间" in summary, summary
                assert "· 本群｜卡组：青眼白龙" in report, report
                assert "· 隔壁测试群（群）｜卡组：卡通" in report, report
                assert "· 群友B 的私聊｜卡组：闪刀" in report, report
                # 每个房间都要带回合数与场面，且不能把会话号报给群友
                assert "第 7 回合" in report and "群友A：LP 1900" in report, report
                assert "s-other" not in report, report

                # 从没有房间的流查：也要报出全部房间，并说明本流没有房间
                context.send.messages.clear()
                await instance.cmd_field(stream_id="s-none", group_id="999")
                report = context.send.messages[-1]
                assert "隔壁测试群（群）" in report, report
                assert "这个对话流里没有房间" in report, report
            finally:
                for task in tasks:
                    task.cancel()
        finally:
            await instance.on_unload()


def _make_fake_windbot(root: Path) -> Path:
    """造一个带 Decks/ 的假 WindBot 目录，返回该目录。"""

    decks_dir = root / "windbot" / "Decks"
    decks_dir.mkdir(parents=True)
    for stem, card_ids in (
        ("AI_BlueEyes", MAIN),
        ("AI_Kashtira", [100000000 + index for index in range(40)]),
    ):
        lines = ["#main"] + [str(c) for c in card_ids] + ["#extra"] + [str(c) for c in EXTRA] + ["!side"]
        (decks_dir / f"{stem}.ydk").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root / "windbot"


async def test_start_command_reports_problem_instead_of_silence() -> None:
    """`/开房` 要能不走模型直接开局；配置不全时把原因发到群里，而不是静默失败。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(
            Path(directory),
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                # 显式清空路径：插件自带 clients/，不清空就是"配好的"，走不到报错那条路
                "paths": {
                    "ygopro_executable": "",
                    "ygopro_dir": "",
                    "windbot_executable": "",
                    "windbot_dir": "",
                },
            },
        )
        await instance.on_load()
        try:
            # 没配路径：指令要把「没配好」的原因发出去
            handled, reason, _level = await instance.cmd_start(stream_id="s1", group_id="111")
            assert handled is True
            assert context.send.messages, "失败原因必须发到群里，否则群里只看到命令被吃掉"
            assert "没配好" in context.send.messages[-1] or "未填写" in context.send.messages[-1]
            assert "没配好" in reason or "未填写" in reason, reason

            # 平台参数是可选的说法微调，写错/不写都不该报错
            for text in ("/开房", "/开房 ygomobile", "/开房 随便写的", "/开局"):
                handled, _reason, _level = await instance.cmd_start(
                    text=text, stream_id="s1", group_id="111"
                )
                assert handled is True, text
        finally:
            await instance.on_unload()


async def test_builtin_decks_seeded_into_pool() -> None:
    """启动时把 WindBot 自带卡组登记进池子：中文名、默认进随机池、不能删。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        windbot_dir = _make_fake_windbot(root)
        instance, context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "paths": {"windbot_dir": str(windbot_dir)},
            },
        )
        await instance.on_load()
        try:
            group = "111"
            assert instance._deck_pool is not None
            decks = instance._deck_pool.list_decks(group)
            assert [deck.display_name for deck in decks] == ["青眼白龙", "怒刹帝利"], decks
            assert all(deck.is_builtin for deck in decks)
            assert instance._deck_pool.count(group) == 0, "内置卡组不算群友投稿"

            # 列表里要标出内置，且默认就在随机池里
            await instance.cmd_deck_list(stream_id="s1", group_id=group)
            listing = context.send.messages[-1]
            assert "1. [✓随机 内置] 青眼白龙" in listing, listing
            assert "2 副在随机池" in listing, listing

            # 开局抽牌抽到的就是内置卡组，说明里不用报英文脚本名
            picked, note = instance._pick_deck(group, "")
            assert picked is not None and picked.is_builtin, picked
            assert picked.windbot_deck in ("Blue-Eyes", "Kashtira"), picked.windbot_deck
            assert "WindBot 自带" in note, note

            # /固定卡组 1（内置卡组）要立刻生效：开局就认这一副，且对所有群生效
            fixed_cmd = await instance.cmd_deck_fix(text="/固定卡组 1", stream_id="s1", group_id=group)
            assert fixed_cmd[0] is True
            assert "对所有群生效" in context.send.messages[-1], context.send.messages[-1]
            assert instance._deck_pool.fixed_deck().display_name == "青眼白龙"
            pinned, pinned_note = instance._pick_deck("999", "")
            assert pinned is not None and pinned.display_name == "青眼白龙", pinned
            assert "青眼白龙" in pinned_note, pinned_note
            await instance.cmd_deck_fix(text="/固定卡组 随机", stream_id="s1", group_id=group)
            assert instance._deck_pool.fixed_deck() is None

            # 内置卡组不能删，但可以移出随机池
            refused = await instance.cmd_deck_delete(text="/删卡组 1", stream_id="s1", group_id=group)
            assert refused[0] is True and "不能删" in context.send.messages[-1], context.send.messages[-1]
            assert len(instance._deck_pool.list_decks(group)) == 2, "拒绝删除后卡组要还在"

            for deck in instance._deck_pool.list_decks(group):
                await instance.cmd_random_remove(
                    text=f"/移出随机 {instance._deck_index(group, deck.deck_id)}",
                    stream_id="s1",
                    group_id=group,
                )
            assert instance._deck_pool.count_in_random(group) == 0
            # 随机池空了就退回 WindBot 自带的那副默认卡组，并告诉群友怎么放牌进去
            empty_pick, empty_note = instance._pick_deck(group, "")
            assert empty_pick is None
            assert "随机池是空的" in empty_note and "/加入随机" in empty_note, empty_note
        finally:
            await instance.on_unload()


async def test_submitted_deck_style_follows_config() -> None:
    """投稿卡组默认用通用出牌脚本；切到 auto 时改用卡表匹配，且开局时按当前配置重算。

    这条是实测教训的护栏：原型脚本按自己的卡表写死 combo，喂群友投稿的卡表会整局空过，
    所以默认必须是通用脚本。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cdb = make_cards_cdb(root / "cards.cdb", sorted(set(MAIN) | set(EXTRA) | set(SIDE)))
        # 造一个假的 WindBot 目录，让 auto 模式有卡表可比对
        decks_dir = root / "windbot" / "Decks"
        decks_dir.mkdir(parents=True)
        for stem, card_ids in (
            ("AI_BlueEyes", MAIN),
            ("AI_Kashtira", [100000000 + index for index in range(40)]),
        ):
            lines = ["#main"] + [str(c) for c in card_ids] + ["#extra"] + [str(c) for c in EXTRA] + ["!side"]
            (decks_dir / f"{stem}.ydk").write_text("\n".join(lines) + "\n", encoding="utf-8")

        base_config = {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "paths": {"cards_cdb": str(cdb), "windbot_dir": str(root / "windbot")},
        }

        # 默认（generic）：投稿回执要说明用的通用脚本；投稿卡组在开局时也按通用脚本解析
        instance, context = make_plugin(root / "data", base_config)
        await instance.on_load()
        try:
            receipt = await submit_deck(
                instance, context, code=deck_code(), deck_name="默认模式", stream_id="s1", group_id="111"
            )
            assert "通用脚本" in receipt, receipt
            # 内置卡组登记时就在随机池里，投稿要自己加进去，所以默认抽到的是内置卡组
            picked, _note = instance._pick_deck("111", "")
            assert picked is not None and picked.is_builtin, picked
            # 真要机器人打投稿那副（群友 /加入随机 或 /固定卡组）时，出牌思路必须是通用脚本
            assert instance._deck_pool is not None
            own = instance._deck_pool.own_decks("111")[0]
            resolved = instance._with_current_style(own)
            assert resolved.windbot_deck == "Test", resolved.windbot_deck
            assert "通用脚本" in instance._describe_deck_choice(resolved, fixed=True)
        finally:
            await instance.on_unload()

        # 切到 auto：开局时按卡表比对，应命中 Blue-Eyes（卡表一致）
        auto_config = dict(base_config)
        auto_config["duel"] = {"windbot_deck": "auto"}
        instance, context = make_plugin(root / "data2", auto_config)
        await instance.on_load()
        try:
            receipt = await submit_deck(
                instance, context, code=deck_code(), deck_name="匹配模式", stream_id="s1", group_id="222"
            )
            # 投稿回执要说明匹配依据（相似度），开局说明只报脚本名即可
            assert "相似度" in receipt, receipt
            assert instance._deck_pool is not None
            own = instance._deck_pool.own_decks("222")[0]
            resolved = instance._with_current_style(own)
            assert resolved.windbot_deck == "Blue-Eyes", resolved.windbot_deck
            assert "Blue-Eyes" in instance._describe_deck_choice(resolved, fixed=True)
        finally:
            await instance.on_unload()


def test_llm_section_owns_the_models_and_the_decision_layer_reads_it() -> None:
    """模型配置收在 `[llm]` 一节里，老配置里的 `duel.brain_model` 只被忽略、不会炸。

    这条护栏针对的是"配置搬家"这类改动：字段挪了地方，老配置文件还在用户机器上，
    搬完之后必须**读得进去、跑得起来**，而不是抛验证错误让插件加载失败。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    module = load_plugin_module()
    config = module.MaiPlayYgoConfig()
    # 三处用途各有自己的模型与超时
    assert hasattr(config.llm, "summary_model") and hasattr(config.llm, "summary_timeout_ms")
    assert config.llm.decision_model == "deepseek-chat", config.llm.decision_model
    assert config.llm.decision_timeout_ms == 2500, config.llm.decision_timeout_ms
    assert hasattr(config.llm, "training_model") and hasattr(config.llm, "training_timeout_ms")
    # 决策层的两项已经搬走：模型里不该再有旧键（否则就有两份真相）
    assert "brain_model" not in module.DuelConfig.model_fields
    assert "brain_timeout_ms" not in module.DuelConfig.model_fields
    # 面板与训练两节的默认值
    assert config.webui.enabled is True and config.webui.port == 17911
    assert config.webui.host == "127.0.0.1", "面板默认不该对外监听"
    assert config.training.enabled is True

    # 老配置文件（带 duel.brain_model / duel.brain_timeout_ms）要能照常加载
    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(
            Path(directory),
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "duel": {"brain_model": "old-model", "brain_timeout_ms": 3333, "brain_enabled": True},
            },
        )
        assert instance.config.duel.brain_enabled is True
        assert instance.config.llm.decision_model == "deepseek-chat", "老键不该盖住新节的默认值"


async def test_llm_section_reaches_summary_and_decision_calls() -> None:
    """配置里的模型名要真的传到调用点：总结用 `summary_model`，决策用 `decision_model`。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            instance.config.llm.summary_model = "总结专用"
            instance.config.llm.decision_model = "决策专用"
            instance.config.llm.decision_timeout_ms = 2000

            summary = await instance._write_summary("回合数 3｜动作 5 比 4", {"winner_is_self": True, "turns": 3})
            assert summary == context.llm.reply, summary
            assert context.llm.calls[-1]["model"] == "总结专用", context.llm.calls[-1]

            answer = await instance._brain_generate("要无效哪只？")
            assert answer == context.llm.reply, answer
            assert context.llm.calls[-1]["model"] == "决策专用", context.llm.calls[-1]
            assert context.llm.calls[-1]["max_tokens"] == instance.config.duel.brain_max_tokens
        finally:
            await instance.on_unload()


async def test_summary_times_out_instead_of_hanging_the_announcement() -> None:
    """总结卡住时要按 `llm.summary_timeout_ms` 收手（返回空串让播报退化），不能一直挂着。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        instance, context = make_plugin(Path(directory))
        await instance.on_load()
        try:
            instance.config.llm.summary_timeout_ms = 2000  # 毫秒：替身要睡 5 秒，必定超时
            context.llm.delay = 5.0
            started = time.monotonic()
            summary = await instance._write_summary("复述", {"winner_is_self": False, "turns": 2})
            elapsed = time.monotonic() - started
            assert summary == "", summary
            assert elapsed < 3.5, f"超时没生效，等了 {elapsed:.2f} 秒"
        finally:
            await instance.on_unload()


async def test_panel_starts_with_the_plugin_and_stops_on_unload() -> None:
    """面板随插件起、随插件停（且端口真的释放）。

    这里用配置里的 `port = 0` 让系统分配：用例不该赌某个端口是空的。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    import http.client

    with tempfile.TemporaryDirectory() as directory:
        instance, _context = make_plugin(
            Path(directory),
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "webui": {"enabled": True, "host": "127.0.0.1", "port": 0, "api_key": "k" * 32},
            },
        )
        await instance.on_load()
        server = instance._webui
        assert server is not None, "面板没有随插件启动"
        port = server.bound_port
        assert port > 0, port

        def get(path: str) -> int:
            """连本机面板的环回端口发一个请求，返回状态码。"""

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("GET", path, headers={"X-API-Key": "k" * 32})
                return connection.getresponse().status
            finally:
                connection.close()

        assert get("/api/status") == 200
        assert get("/api/training") == 200

        await instance.on_unload()
        assert instance._webui is None, "卸载后面板句柄没清掉"
        released = False
        try:
            get("/api/status")
        except OSError:
            released = True
        assert released, "卸载后面板还在答话"


def test_train_runner_is_built_with_the_configured_workspace() -> None:
    """训练执行器要按配置建在工作目录上，模型名也要推给它（不是硬编码）。"""

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    import asyncio as asyncio_module

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, _context = make_plugin(
            root / "data",
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "llm": {"training_model": "训练专用"},
                "training": {"max_duels_per_run": 12},
            },
        )

        async def run() -> None:
            await instance.on_load()
            try:
                runner = instance.training_runner()
                assert runner is not None, "训练执行器没建起来"
                assert runner.workspace == root / "data" / "train", runner.workspace
                assert runner.max_duels == 12, runner.max_duels
                assert runner._model_name() == "训练专用", runner._model_name()
                # 配了工作目录时要用配置那个
                instance.config.training.workspace = str(root / "elsewhere")
                assert instance._training_workspace() == root / "elsewhere"
            finally:
                await instance.on_unload()

        asyncio_module.run(run())


async def test_config_save_keeps_a_running_training_task() -> None:
    """配置热更新只在"动到执行器"时才重建训练层，别的改动不能把在跑的任务掐掉。

    这条是实测踩出来的：擂台一跑几十分钟，而宿主**每次保存配置都会推一次热更新**——
    如果每次都重建 runner（`stop_now` 会连子进程树一起杀），用户改个总结模型就会看到
    "任务失败：插件重载或上次卸载时中断"。所以只有
    工作目录 / 局数上限 / 启用开关变了才重建，其余只把模型名推过去。
    """

    if not _sdk_available():
        print("      （跳过：未找到 maibot_sdk）")
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        instance, _context = make_plugin(
            root / "data", {"plugin": {"enabled": True, "config_version": "1.0.0"}}
        )
        await instance.on_load()
        try:
            runner = instance.training_runner()
            assert runner is not None
            stopped: List[str] = []
            runner.stop_now = lambda: stopped.append("stop")  # type: ignore[method-assign]

            # 1) 只改模型：不重建、不打断
            instance.config.llm.training_model = "另一只"
            await instance.on_config_update("self", {}, "1.2.0")
            assert not stopped, "改模型不该把在跑的任务掐掉"
            assert instance.training_runner() is runner, "不该重建执行器"
            assert runner._model_name() == "另一只", "模型名要推过去"

            # 2) 改工作目录：必须重建（换了目录还往旧目录写结果才是灾难）
            instance.config.training.workspace = str(root / "another")
            await instance.on_config_update("self", {}, "1.2.0")
            assert stopped, "换工作目录必须停掉旧执行器"

            # 3) 关掉训练功能：执行器与记录库都收掉
            instance.config.training.enabled = False
            await instance.on_config_update("self", {}, "1.2.0")
            assert instance.training_runner() is None
            assert instance.training_store() is None
        finally:
            await instance.on_unload()


def main() -> int:
    """逐个执行测试；协程测试用 asyncio.run 驱动。"""

    tests = [(name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures: List[str] = []
    for name, func in tests:
        try:
            result = func()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception as exc:  # noqa: BLE001  测试脚本需要打印任意异常
            failures.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ ok ] {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
