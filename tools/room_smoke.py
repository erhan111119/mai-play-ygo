"""走**插件真实开房链路**打一局：不开聊天，只验"群里那条路现在还通不通"。

为什么需要它：群里打不了对局时，从聊天层排查很绕（工具调用在模型手里、房间在别的进程里）。
这个脚本把插件自己的那条链原样跑一遍——**用的是插件自己的代码与当前生效的配置**：

    MaiPlayYgoConfig（读本机 config.toml，含 [llm]）
      → _build_session_config(stream_id, brain_prefix)   # 与开房工具里同一句
      → await _start_room_brain(stream_id)               # 起决策层答复任务 + 预热（等它走完）
      → DuelSession.start() → 对手进房 → 打完
      → _stop_room_brain(stream_id)                      # 收摊（含计数日志）

所以别人改了插件、改了配置、改了 exe 之后，这一条跑通就说明"群里那局能打"；
跑不通的话它会把失败点（配置校验 / 路径解析 / 开房 / 决策层 / 收摊）直接报出来。

用法::

    python plugins/mai-play-ygo/tools/room_smoke.py                # 用池子里的随机一副，对手固定青眼
    python plugins/mai-play-ygo/tools/room_smoke.py --deck-id 101  # 指定用哪副（池子编号）
    python plugins/mai-play-ygo/tools/room_smoke.py --seconds 180  # 一局最多打多久
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import argparse
import asyncio
import importlib.util
import logging
import sys
import time
import tomllib

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MAIBOT_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _MAIBOT_ROOT.parent.parent
for _extra in (_MAIBOT_ROOT, _INSTALL_ROOT / "python-overrides", _INSTALL_ROOT / "python-env" / "Lib" / "site-packages"):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

OPPONENT_STYLE = "Blue-Eyes"


def _plugin_submodules(module: Any) -> Dict[str, Any]:
    """取插件里那几个子模块（与插件用的是同一份模块对象）。"""

    import importlib

    names = ("duel.cards", "duel.deckpool", "duel.session", "duel.room", "duel.brain_bridge")
    out: Dict[str, Any] = {}
    for name in names:
        out[name] = importlib.import_module(f"{module.__name__}.{name}")
    return out


def _load_plugin_module():
    """按宿主的加载姿势导入 plugin.py。

    ⚠ 关键在 `submodule_search_locations=[插件目录]`：插件里的相对导入（`from .duel.session import`）
    只有在"这个模块同时被当作包"时才成立——少了这一项会报
    `ModuleNotFoundError: No module named 'mai_play_ygo_under_test.duel'; ... is not a package`。
    `tests/test_manifest.py` 里也是这么加载的，两处口径保持一致。
    """

    spec = importlib.util.spec_from_file_location(
        "mai_play_ygo_smoke",
        str(_PLUGIN_ROOT / "plugin.py"),
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"加载不了插件模块：{_PLUGIN_ROOT / 'plugin.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLlm:
    """把 `ctx.llm.generate` 接到宿主的 LLM 服务上——**与插件在生产里走的是同一条路**。"""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self.calls = 0

    async def generate(self, *, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        from src.services import llm_service

        self.calls += 1
        result = await llm_service.generate(
            llm_service.LLMServiceRequest(
                task_name="utils",
                request_type="plugin.mai-play-ygo.smoke",
                prompt=prompt,
                model_name=str(kwargs.get("model_name") or ""),
                temperature=kwargs.get("temperature"),
                max_tokens=kwargs.get("max_tokens"),
            )
        )
        return result.to_capability_payload() if hasattr(result, "to_capability_payload") else {}


class _FakePaths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _FakeCtx:
    """只补插件这条链真正用到的那几项（数据目录 + 模型）。"""

    def __init__(self, data_dir: Path, logger: logging.Logger) -> None:
        self.paths = _FakePaths(data_dir)
        self.llm = _FakeLlm(logger)


async def run(args: argparse.Namespace) -> int:
    logger = logging.getLogger("room_smoke")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    module = _load_plugin_module()
    data_dir = _MAIBOT_ROOT / "data" / "plugins" / "mai-play-ygo"

    # ① 配置：读本机 config.toml（含 [llm]）并交给插件自己的配置模型校验
    config_path = _PLUGIN_ROOT / "config.toml"
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        validated = module.MaiPlayYgoConfig(**raw)
    except Exception as exc:  # noqa: BLE001  配置写坏时这一条就该当场说清楚
        print(f"❌ 配置校验失败（{config_path}）：{type(exc).__name__}: {exc}")
        return 2
    del validated  # 真正的注入走下面的 set_plugin_config（同一套校验），这里只做一次"能不能读懂"的预检
    print("✔ 配置能读懂：%s" % config_path)

    # ② 插件实例：用 SDK 的**正规注入入口**塞配置与上下文（`config`/`ctx` 都是只读属性，
    #    直接赋值会报 "property ... has no setter"；`set_plugin_config` 还会顺带跑一遍配置校验，
    #    与生产里宿主注入的方式一致）
    instance = module.MaiPlayYgo()
    instance.set_plugin_config(raw)
    instance._set_context(_FakeCtx(data_dir, logger))
    instance._logger = logger
    # ⚠ 插件的子模块要从**已加载的那个包**里取，不能另外 `import duel.xxx`——
    # 那会加载出第二份同名模块（`mai_play_ygo_smoke.duel.session` 与 `duel.session` 是两个对象），
    # 于是 isinstance 校验、模块级缓存全都会错开，故障还很难看（这条踩过）
    sub = _plugin_submodules(module)
    config = instance.config
    print("✔ 配置注入成功：脚本默认 %s｜决策层 目标=%s 闸门=%s｜模型=%s"
          % (config.duel.windbot_deck or "(auto)",
             "开" if config.duel.brain_enabled else "关",
             "开" if config.duel.brain_negate_gate else "关",
             config.llm.decision_model.strip() or "(宿主默认)"))
    CardDatabase = sub["duel.cards"].CardDatabase

    cards_cdb = instance._resolve_cards_cdb()
    instance._card_db = CardDatabase(cards_cdb)
    if not cards_cdb or not Path(cards_cdb).is_file():
        print(f"❌ 卡库不可用：{cards_cdb}")
        return 2
    print(f"✔ 卡库：{cards_cdb}")

    # ③ 挑一副牌：默认从随机池里拿，和群里随机到的那副是同一批
    DeckPool = sub["duel.deckpool"].DeckPool

    pool = DeckPool(data_dir, default_windbot_deck="Blue-Eyes")
    deck = None
    deck_style = config.duel.windbot_deck or "Blue-Eyes"
    if args.deck_id:
        candidates = [d for d in pool.list_decks(args.group_id) if d.deck_id in set(args.deck_id)]
        if candidates:
            deck = candidates[0]
        else:
            print(f"⚠ 池子里没有编号 {args.deck_id} 的卡组，用配置里的兜底风格")
    if deck is not None:
        deck_style = deck.generated_script or deck.picked_style or deck.windbot_deck or deck_style
    print(f"✔ 这一局用：{'#' + str(deck.deck_id) + ' ' + deck.display_name if deck else '（配置兜底）'}"
          f"｜脚本 {deck_style}")

    # ④ 走插件自己的两句：起决策层 → 拼会话配置
    stream_id = "room-smoke"
    brain_prefix = await instance._start_room_brain(stream_id)  # 内部会先把预热走完
    print(f"✔ 决策层：{'前缀 ' + str(brain_prefix) if brain_prefix else '未启动（两个开关都关？）'}")
    session_config = instance._build_session_config(stream_id, brain_prefix)
    # 自测不写录像：否则会往 `clients/ygopro/replay/` 里塞自测对局，而 `tests/test_replay.py`
    # 会扫真实录像目录（**这条踩过两次**：先污染发现、再清掉）。
    session_config.room.save_replay = False
    print(f"✔ 会话配置：ygopro={session_config.ygopro_executable}")
    print(f"            windbot={session_config.windbot_executable}")
    if not Path(session_config.ygopro_executable).is_file():
        print("❌ ygopro 可执行文件不在——这就是群里开不了房的直接原因")
        instance._stop_room_brain(stream_id)
        return 2
    if not Path(session_config.windbot_executable).is_file():
        print("❌ WindBot 可执行文件不在——这就是群里开不了房的直接原因")
        instance._stop_room_brain(stream_id)
        return 2

    # ⑤ 真开一局：对手拿房间口令从"人类位"进来
    WindBotProcess = sub["duel.room"].WindBotProcess
    WindBotSettings = sub["duel.room"].WindBotSettings
    DuelSession = sub["duel.session"].DuelSession

    session = DuelSession(
        session_config, group_id=args.group_id, deck=deck,
        card_db=instance._card_db, logger=logger,
    )
    opponent: Optional[WindBotProcess] = None
    outcome = "未开始"
    ok = True
    try:
        info = await session.start()
        print(f"✔ 房间已开：端口 {info.port}｜口令 {info.password}｜"
              f"发给群友 {info.host}:{info.advertised_port}")
        opponent = WindBotProcess(
            session_config.windbot_executable,
            session_config.windbot_dir,
            WindBotSettings(name="smoke-对手", deck=OPPONENT_STYLE, password=info.password,
                            db_path=session_config.cards_cdb, debug=session_config.bot_debug),
            logger=logger,
        )
        await opponent.start("127.0.0.1", info.port)
        print("✔ 对手已进房，等对局结束……")
        try:
            outcome = await asyncio.wait_for(session.wait_finished(), timeout=args.seconds)
        except asyncio.TimeoutError:
            outcome = f"限时 {args.seconds:.0f}s 收摊"
    except Exception as exc:  # noqa: BLE001  开房失败要原样报出来
        print(f"❌ 打不了：{type(exc).__name__}: {exc}")
        outcome = "失败"
    finally:
        data = session.result_dict()
        if opponent is not None:
            await opponent.stop()
        await session.stop()
        instance._stop_room_brain(stream_id)
        instance._card_db.close()
        pool.close()

    print("\n================ 结果 ================")
    print(f"结局：{outcome}｜回合数：{data.get('turns')}｜机器人赢：{data.get('winner_is_self')}")
    print(f"模型被调用次数（含预热）：{instance.ctx.llm.calls}")
    print("（决策层的问答计数在上面的收摊日志里：问了/答了/缓存/超时/失败）")

    # 收尾那一步也走一遍：群里"这局没成"有时其实是对**打完那一步**炸了（总结调模型失败），
    # 而它看起来就像"对局有问题"。这里直接调插件自己的 `_write_summary`。
    if args.check_summary:
        try:
            started = time.monotonic()
            summary_text = await instance._write_summary("\n".join(session.summary_lines()), data)
            seconds = time.monotonic() - started
            if summary_text:
                print(f"✔ 对局总结（{seconds:.1f}s）：{summary_text[:120]}")
            else:
                print(f"❌ 对局总结返回空（{seconds:.1f}s）——模型名/额度/提示词哪一处有问题，"
                      f"日志里会有「生成对局总结返回了空内容」或 provider 报错")
                ok = False
        except Exception as exc:  # noqa: BLE001  总结失败不该把整条自测变成崩溃
            print(f"❌ 对局总结抛异常：{type(exc).__name__}: {exc}")
            ok = False
    return 0 if ok and outcome and outcome != "失败" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="走插件真实开房链路打一局（不开聊天）")
    parser.add_argument("--group-id", default="27dc88f32327", help="按哪个群的口径取随机池")
    parser.add_argument("--deck-id", type=int, nargs="*", help="指定用池子里哪几副（默认不指定=兜底风格）")
    parser.add_argument("--seconds", type=float, default=180.0, help="一局最多打多少秒")
    parser.add_argument("--no-check-summary", dest="check_summary", action="store_false",
                        help="跳过收尾那一步的检查（默认会跑一遍 _write_summary）")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
