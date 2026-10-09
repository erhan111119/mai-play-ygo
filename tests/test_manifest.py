"""契约测试：用宿主真实的校验器检查清单与配置模型。

这类错误很隐蔽——``_manifest.json`` 里某个 URL 留空、能力名写错一个字母，
插件会在发现阶段被静默丢弃或在调用时被拒绝，日志里看不出原因。所以这里直接调用
宿主的校验代码，把问题挡在提交之前。

宿主源码不在旁边时（插件仓库单独克隆的情况）这些检查会自动跳过。

直接用 ``python tests/test_manifest.py`` 运行，也可以用 pytest 收集。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import json
import re
import sys
import tomllib

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# 宿主根目录：本文件位于 <MaiBot>/plugins/<plugin>/tests/
_HOST_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _HOST_ROOT.parent.parent
# maibot_sdk 有两个可能的位置：一键包的 overrides 目录，或宿主自己的 site-packages
_SDK_CANDIDATES = (
    _INSTALL_ROOT / "python-overrides",
    _INSTALL_ROOT / "python-env" / "Lib" / "site-packages",
)
for extra in (_HOST_ROOT, *_SDK_CANDIDATES):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _host_available() -> bool:
    """宿主校验代码是否可用。"""

    try:
        import src.plugin_runtime.runner.manifest_validator  # noqa: F401
    except Exception:  # noqa: BLE001  缺少宿主依赖时跳过契约检查
        return False
    return True


def test_manifest_passes_host_validator() -> None:
    """清单必须通过宿主真实校验器。"""

    if not _host_available():
        print("      （跳过：未找到宿主源码）")
        return

    from src.plugin_runtime.runner.manifest_validator import PluginManifest

    raw = json.loads((_PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    manifest = PluginManifest.model_validate(raw)

    assert manifest.manifest_version == 2
    assert manifest.id == "mai-play-ygo"
    assert manifest.name == "麦麦玩游戏王"
    assert manifest.author.url.strip(), "author.url 留空会导致插件被静默丢弃"
    assert manifest.urls.repository.strip(), "urls.repository 留空会导致插件被静默丢弃"
    assert manifest.capabilities, "至少要声明一个能力"


def test_declared_capabilities_cover_code_usage() -> None:
    """代码里用到的能力都必须写进清单，否则运行时会 E_CAPABILITY_DENIED。"""

    raw = json.loads((_PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    declared = set(raw["capabilities"])

    source = (_PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
    # ctx.send.text(...) -> send.text；ctx.maisaka.append_context(...) -> maisaka.context.append
    used = set()
    if "self.ctx.send.text(" in source:
        used.add("send.text")
    if "self.ctx.maisaka.append_context(" in source:
        used.add("maisaka.context.append")
    if "self.ctx.maisaka.trigger_proactive(" in source:
        used.add("maisaka.proactive.trigger")
    if "self.ctx.llm.generate" in source:
        used.add("llm.generate")
    if "self.ctx.chat.get_all_streams(" in source:
        used.add("chat.get_all_streams")

    missing = used - declared
    assert not missing, f"代码用到但清单未声明：{sorted(missing)}"


def test_no_capability_used_but_never_declared_by_hallucination() -> None:
    """清单里不该出现代码没用到的能力（多余声明会误导使用者）。"""

    raw = json.loads((_PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    declared = set(raw["capabilities"])
    assert declared <= {
        "send.text",
        "llm.generate",
        "maisaka.context.append",
        "maisaka.proactive.trigger",
        "chat.get_all_streams",
        # 查房出图（2026-10-07）：宿主渲染 HTML → PNG，再把图发到群里
        "render.html2png",
        "send.image",
    }, (
        f"清单声明了意料之外的能力：{sorted(declared)}，请确认代码真的会用"
    )


def _load_plugin_module():
    """按宿主加载器的方式导入 plugin.py。

    宿主用 ``spec_from_file_location(..., submodule_search_locations=[插件目录])`` 加载入口，
    这样插件里的相对导入（``from .duel.session import ...``）才成立。这里复刻同样的方式，
    顺带验证插件能在宿主的加载姿势下正常导入。
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        # 模块名跟着现在的插件 id 走（2026-10-07 第五轮评审顺带指出旧名残留）
        "mai_play_ygo_under_test",
        str(_PLUGIN_ROOT / "plugin.py"),
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_config_model_builds_and_matches_toml() -> None:
    """配置模型必须能给每个字段生成默认值，且与模板 ``config.toml.example`` 的节保持一致。

    ⚠ 校验的是**模板**而不是本机的 `config.toml`：仓库里只放 `config.toml.example`，
    真正在跑的那份是本地文件（已 gitignore，可能钉了内部参数、还带隧道地址），
    拿它当"模板校验"会把本机配置误判成模板写错。
    """

    plugin_module = _load_plugin_module()
    config_model = plugin_module.MaiPlayYgoConfig

    defaults = config_model()
    # 自包含：两个虚拟客户端的默认值指向插件自带的 clients/（相对路径，按插件目录解析）
    assert defaults.paths.ygopro_executable == "clients/ygopro/ygopro.exe"
    assert defaults.paths.windbot_executable == "clients/windbot/WindBot.exe"
    assert defaults.paths.resolved_ygopro_dir() == _PLUGIN_ROOT / "clients" / "ygopro"
    assert defaults.duel.bot_name, "bot 昵称要有默认值"
    assert defaults.duel.join_timeout_seconds > 0
    # 版本号提升是有意的：0.21.27 新增了 duel.invite_*（空闲主动约战）；
    # 2026-10-07 起 1.10.0：`taunt_enabled` / `invite_enabled` 默认关闭（用户要求"开房发完地址就不再讲话"）。
    # 2026-10-08 起 1.2.0：新增 [llm]（三个用途的模型与超时）/ [training]（训练功能）/ [webui]（插件面板），
    # 并把 duel.brain_model + duel.brain_timeout_ms 移成 llm.decision_model + llm.decision_timeout_ms。
    # 2026-10-09 起 1.3.0：`[llm]` 多一个 training_script_max_tokens（写脚本每批的输出上限）。
    # 1.5.0 起 `llm.decision_timeout_ms` 默认 4000（决策层推荐用 2 秒档的关思考模型）。
    # 同日起 1.4.0：`llm.decision_model` 默认改成空串（跟宿主的 utils 任务走）——
    # 出厂默认写一个厂商模型名，在别的机器上一上来就会"未找到名为 xxx 的模型"。
    assert defaults.plugin.config_version == "1.5.0"

    with (_PLUGIN_ROOT / "config.toml.example").open("rb") as handle:
        toml_data = tomllib.load(handle)
    assert set(toml_data) >= {"plugin", "paths", "duel", "llm", "training", "webui", "wiki"}, (
        "配置模板缺少必要的配置节"
    )
    assert toml_data["plugin"]["config_version"] == defaults.plugin.config_version, (
        "配置模板的 config_version 与代码默认值不一致，会导致每次启动都触发配置迁移"
    )

    # 模板里出现的键必须在模型里有对应字段，否则是写错了名字
    model_fields = set(config_model.model_fields)
    assert set(toml_data) <= model_fields, f"配置模板存在模型里没有的节：{set(toml_data) - model_fields}"
    for section in ("paths", "duel", "llm", "training", "webui", "wiki"):
        section_model = config_model.model_fields[section].annotation
        assert section_model is not None
        assert set(toml_data[section]) <= set(section_model.model_fields), (
            f"config.toml 的 [{section}] 存在未知字段："
            f"{set(toml_data[section]) - set(section_model.model_fields)}"
        )


def test_duel_config_exposes_only_group_owner_items() -> None:
    """对局配置的**可见面**只有群主要用的那几项（2026-10-07 用户口径）。

    开房地址与端口 / bot 名字 / 等人超时 / 总结开关与提示词 / 挑衅开关与台词池。
    其余字段都标了 `hidden`：字段还在模型里（老配置写过的键照旧能读、代码照旧读得到），
    但插件配置页与 `config.toml` 模板上不再出现。这条护栏卡住"以后又把内部参数露出来"。
    （原来列在这里的 `ai_brain` / `brain_scope` / `persist_room` / `invite_*` / `train_model`
    等"内部参数"已随 AI 打牌 / 常驻房 / 约战 / 训练调优的删除**从模型里彻底去掉**，
    不再是"隐藏字段"。）
    """

    plugin_module = _load_plugin_module()
    duel_model = plugin_module.MaiPlayYgoConfig.model_fields["duel"].annotation
    assert duel_model is not None
    visible = sorted(
        name
        for name, field in duel_model.model_fields.items()
        if not (field.json_schema_extra or {}).get("hidden")
    )
    assert visible == [
        "announce_result",
        "bot_name",
        "join_timeout_seconds",
        "listen_port",
        "public_host",
        "public_port",
        "summarize_with_ai",
        "summary_prompt",
        "taunt_enabled",
        "taunt_lines",
    ], visible
    # 可见字段要有中文标签：配置页直接显示字段名（英文）就太难用了
    for name in visible:
        label = (duel_model.model_fields[name].json_schema_extra or {}).get("label")
        assert label and not label.isascii(), f"{name} 缺中文标签：{label!r}"

    # 配置页看到的十项都要在配置里出现；反过来配置里**不许有拼错的键**
    # （以前这里断言"模板里只有这十项"，但本机在跑的 config.toml 是同一份文件：
    #  开发机可以合法地额外钉几个内部参数（bot_debug / no_check_deck 之类），
    #  那条"严格相等"会把"本机配置"当成"模板写错了"。可见面由上面模型级的断言守着。）
    #
    # ⚠ 校验对象：**模板一份必查，本机 `config.toml` 存在时再加查一份**（2026-10-07 第五轮评审指出）：
    # `config.toml` 恰恰是不入库的那份，原来无条件 open 会让新克隆的仓库与 CI 直接 FileNotFoundError
    # ——"配置不入库"这条实践不能被自己的测试绊倒。四个 tools 用的是同一套"没有就回落模板"的口径。
    config_paths = [_PLUGIN_ROOT / "config.toml.example"]
    local_config = _PLUGIN_ROOT / "config.toml"
    if local_config.is_file():
        config_paths.insert(0, local_config)
    checked: List[str] = []
    for config_path in config_paths:
        with config_path.open("rb") as handle:
            toml_data = tomllib.load(handle)
        assert set(visible) <= set(toml_data["duel"]), (
            f"{config_path.name} 的 [duel] 缺少可见项：{sorted(set(visible) - set(toml_data['duel']))}"
        )
        unknown = set(toml_data["duel"]) - set(duel_model.model_fields)
        assert not unknown, f"{config_path.name} 的 [duel] 有模型里不存在的键：{sorted(unknown)}"
        checked.append(config_path.name)
    print(f"      （已核对的配置：{'、'.join(checked)}）")


def test_plugin_exposes_expected_tools() -> None:
    """插件实例应当注册出预期的工具，且工具名带统一前缀避免撞名。"""

    if not _host_available():
        print("      （跳过：未找到宿主源码）")
        return

    plugin_module = _load_plugin_module()
    instance = plugin_module.create_plugin()
    components = instance.get_components()
    # get_components() 返回的是声明字典，type 用大写的组件类型枚举值
    tool_names = sorted(item["name"] for item in components if item.get("type") == "TOOL")
    assert tool_names, "插件没有注册任何工具"
    for name in tool_names:
        assert name.startswith("ygo_"), f"工具 {name} 缺少 ygo_ 前缀，容易与其它插件撞名"
    for expected in (
        "ygo_duel_start", "ygo_duel_status", "ygo_duel_stop",
        # 百科检索（原 yugioh-wiki 插件）并进来后的两个工具
        "ygo_card_search", "ygo_card_image",
    ):
        assert expected in tool_names, f"缺少工具 {expected}，实际有 {tool_names}"
    # **卡组码只能用指令投稿**（2026-10-07 用户口径）：原来那个「模型看到卡组码就自动收录」的
    # `ygo_deck_submit` 工具已删除，模型手里不该再有导入卡组的入口；
    # 同一个口径下，「自动识别群里的卡组码」的 `ygo_deck_analyze` 也删掉了
    for removed_tool in ("ygo_deck_submit", "ygo_deck_analyze"):
        assert removed_tool not in tool_names, tool_names

    # 卡组管理的指令必须注册出来，且破坏性操作要声明管理员权限
    commands = {item["name"]: item for item in components if item.get("type") == "COMMAND"}
    for expected in ("ygo_cmd_deck_list", "ygo_cmd_deck_add", "ygo_cmd_deck_delete", "ygo_cmd_deck_fix"):
        assert expected in commands, f"缺少指令 {expected}，实际有 {sorted(commands)}"
    # 只留核心功能（2026-10-07 用户口径）：训练调优 / AI 打牌 / 复盘这几条指令已经删掉，
    # 不该再被注册出来（模型或群友都不该看到入口）
    for removed_command in (
        "ygo_cmd_optimize",
        "ygo_cmd_pick_style",
        "ygo_cmd_write_playbook",
        "ygo_cmd_train",
        "ygo_cmd_replay",
        "ygo_cmd_brain_mode",
    ):
        assert removed_command not in commands, f"{removed_command} 应当已删除，实际有 {sorted(commands)}"
    # 破坏性/全局性的指令要声明管理员权限：清空与改名会动全局数据，
    # 删卡组是**跨群**删除（卡组池共享，列表里看得见别群的投稿）
    for guarded in ("ygo_cmd_deck_clear", "ygo_cmd_bot_name", "ygo_cmd_deck_delete"):
        assert guarded in commands, f"缺少指令 {guarded}"
        assert commands[guarded]["metadata"].get("permission") == "operator", (
            f"指令 {guarded} 会改动全局数据，应当声明 permission=operator"
        )

    # 指令名与工具名不能相同：宿主按「插件ID.组件名」做扁平索引，撞名会互相覆盖
    overlap = set(commands) & set(tool_names)
    assert not overlap, f"指令与工具重名，会互相覆盖：{sorted(overlap)}"

    # 工具是给 LLM 判断「有人想打牌」用的，必须让模型直接看得到，而不是藏在 tool_search 后面
    for item in components:
        if item.get("type") != "TOOL":
            continue
        assert item["metadata"].get("visibility") == "visible", (
            f"工具 {item['name']} 的 visibility 不是 visible，模型需要额外搜索才能发现它"
        )


def _strip_comments(block: str) -> List[str]:
    """去掉整行注释与行尾注释，只留真正的代码行（否则注释里提到 `type=` 会被误判）。"""

    lines: List[str] = []
    for line in block.splitlines():
        code = line.split("#", 1)[0].rstrip()
        if code.strip():
            lines.append(code)
    return lines


def test_sdk_calls_use_the_current_keyword_forms() -> None:
    """组件声明要用 SDK 现行的关键字：`brief_description` / `param_type`。

    为什么值得一条护栏（2026-10-07 评审指出）：两代写法在 2.5.0 上都能"跑起来"，
    但**旧写法是静默失效**的——`ToolParameterInfo(param_type=...)` 才是字段名，写成 `type=`
    pydantic 会当多余关键字丢掉、参数类型悄悄回落到 STRING；`@Tool(description=...)` 只是兼容别名，
    现行字段是 `brief_description` / `detailed_description`。写错了没有任何报错，
    只会在别处表现出"为什么模型不按我写的类型传参"。
    """

    problems: List[str] = []
    for path in (_PLUGIN_ROOT / "plugin.py", _PLUGIN_ROOT / "wiki.py"):
        text = path.read_text(encoding="utf-8")

        # ① @Tool 的**顶层**关键字（与第一个参数同缩进）不许是裸 description=
        for match in re.finditer(r"@Tool\(\n(.*?)\n(\s*)\)\n", text, re.S):
            body, args = match.group(1), _strip_comments(match.group(1))
            if not args:
                continue
            top_indent = len(args[0]) - len(args[0].lstrip())
            for line in args:
                indent = len(line) - len(line.lstrip())
                if indent == top_indent and re.match(r"\s*description\s*=", line):
                    problems.append(f"{path.name}: @Tool 顶层用了 description=（应改成 brief_description）")
                    break
            del body

        # ② ToolParameterInfo 里不许有裸 type=（字段名是 param_type）
        for match in re.finditer(r"ToolParameterInfo\((.*?)\)", text, re.S):
            for line in _strip_comments(match.group(1)):
                if re.search(r"(?<!param_)\btype\s*=", line):
                    problems.append(f"{path.name}: ToolParameterInfo 用了 type=（应改成 param_type）")
                    break
    assert not problems, "；".join(problems)


def test_windows_only_is_declared_in_manifest_and_readme() -> None:
    """这是个 Windows-only 插件，清单与 README 都要写明（2026-10-07 评审要求）。

    对局内核 ygopro 与出牌引擎 WindBot 都是 Windows 可执行文件，Linux / macOS 上开房必然失败；
    不写清楚的话，非 Windows 的部署者只会看到"麦麦一声不响"。
    """

    raw = json.loads((_PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert "Windows" in raw["description"], "清单描述里要写明只支持 Windows"
    readme = (_PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "只支持 Windows" in readme, "README 开头要写明只支持 Windows"


def test_packaged_binaries_have_a_licensing_stance() -> None:
    """随包二进制/卡库的许可要有明确结论，不能只写"请自行判断"（2026-10-07 评审要求）。"""

    readme = (_PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "发行结论" in readme, "README 要写明随包内容的发行结论"
    assert "cards.cdb" in readme and "GPL-2.0" in readme and "MIT" in readme
    clients_readme = (_PLUGIN_ROOT / "clients" / "README.md").read_text(encoding="utf-8")
    assert "cards.cdb" in clients_readme


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
