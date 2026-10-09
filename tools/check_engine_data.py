"""引擎数据自检：确认 cards.cdb / strings.conf / script 三者是配套的。

**为什么需要这个**：这套对局环境的数据有三个来源——内核（ygopro.exe）、卡库
（cards.cdb + strings.conf）、卡牌脚本（script/*.lua）。三者**必须同源**，否则会
出现最难查的一类故障：

* 脚本比内核新（脚本用了内核没有的 Lua 接口，例如 `Synchro.AddProcedure`、
  `IsOriginalType`）→ 脚本一加载就报错，卡片表现为「能召唤、能攻击，但发不了效果」；
* 卡库比脚本新 → 新卡的脚本还不存在，同样是「发不了效果」，而且一个字都不报。

这两种情况都**不会**打断对局，只会让效果默默失效，所以必须在开打前查。

用法::

    # 离线体检（不启动任何进程）；不填 --ygopro-dir 就用插件自带的 clients/ygopro
    python tools/check_engine_data.py --ygopro-dir <你的 ygopro 目录>

    # 再自己打一局（内核 + 两个 WindBot），捕获内核的 Lua 报错
    python tools/check_engine_data.py --ygopro-dir ... \\
        --windbot-exe .../WindBot.exe --windbot-dir .../WindBot --selfplay

    # 顺便体检某副 .ydk
    python tools/check_engine_data.py --ygopro-dir ... --deck 某人.ydk
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import argparse
import asyncio
import re
import sqlite3
import sys
import time

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.cards import CardDatabase  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.room import WindBotSettings  # noqa: E402  导入顺序受 sys.path 补丁影响
from duel.session import OUTCOME_NO_PLAYER, DuelSession, SessionConfig  # noqa: E402

# 卡类型位（ocgcore 的 TYPE_*，与 cards.cdb 的 datas.type 一致）
TYPE_MONSTER = 0x1
TYPE_SPELL = 0x2
TYPE_TRAP = 0x4
TYPE_NORMAL = 0x10
TYPE_TOKEN = 0x4000

# 内核里出现的 Lua 报错行；这是「效果静默失效」最直接的证据
LUA_ERROR_PATTERN = re.compile(r"\[string \".*?/(c\d+\.lua)\"\]:(\d+): (.+)")

# 这套内核（mycard/moecube 血统）没有的接口：官方脚本改造版用不到，官方最新脚本会用到。
# 出现这些说明脚本集与内核不是一套，效果会成片失效。
MODERN_ONLY_APIS: Tuple[str, ...] = (
    "TIMINGS_CHECK_MONSTER_E",
    "IsOriginalType",
    "IsSynchroMonster",
    "IsLinkMonster",
    "SelectMZone",
    "Synchro.AddProcedure",
    "aux.addContinuousLizardCheck",
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="检查游戏王引擎数据（卡库 / 脚本 / 内核）是否配套")
    parser.add_argument("--ygopro-dir", required=True, help="ygopro 工作目录（含 cards.cdb 与 script/）")
    parser.add_argument("--ygopro-exe", default="", help="ygopro.exe 路径；自检对局时需要")
    parser.add_argument("--cards-cdb", default="", help="cards.cdb 路径，默认取工作目录下的")
    parser.add_argument("--windbot-exe", default="", help="WindBot.exe 路径；自检对局时需要")
    parser.add_argument("--windbot-dir", default="", help="WindBot 工作目录")
    parser.add_argument(
        "--deck", default=[], action="append", help="额外体检某副 .ydk（可重复）"
    )
    parser.add_argument("--selfplay", action="store_true", help="真起一局（内核 + 两个 WindBot）抓内核报错")
    parser.add_argument(
        "--verbose", action="store_true", help="自检对局时把内核输出与 WindBot 对局日志也报出来"
    )
    parser.add_argument("--selfplay-seconds", type=float, default=45.0, help="自检对局跑多少秒")
    parser.add_argument(
        "--modern-api-sample",
        type=int,
        default=400,
        help="抽样多少个脚本检查是否用到内核不支持的接口（0 表示不查）",
    )
    return parser.parse_args()


def load_scripts(script_dir: Path) -> Set[str]:
    """返回脚本目录里所有卡脚本的「卡号字符串」集合。"""

    return {path.stem[1:] for path in script_dir.glob("c*.lua")}


def load_cards(cdb: Path) -> List[Tuple[int, str, int, int]]:
    """读出 ``[(卡号, 卡名, 类型位, 别名)]``。"""

    connection = sqlite3.connect(str(cdb))
    try:
        rows = connection.execute(
            "SELECT t.id, t.name, d.type, d.alias FROM texts t LEFT JOIN datas d ON d.id = t.id"
        ).fetchall()
    finally:
        connection.close()
    return [(int(row[0]), str(row[1]), int(row[2] or 0), int(row[3] or 0)) for row in rows]


def has_script(card_id: int, alias: int, scripts: Set[str]) -> bool:
    """这张卡有没有脚本（异画卡可以靠别名指向本体的脚本，内核就是这么找的）。"""

    return str(card_id) in scripts or (alias and str(alias) in scripts)


def check_card_coverage(cards: List[Tuple[int, str, int, int]], scripts: Set[str]) -> List[Tuple[int, str, str]]:
    """列出「本该有脚本却没有」的卡。

    通常怪兽、衍生物本来就没有脚本，不算问题；真正的问题是有类型的怪兽 / 魔法 / 陷阱
    找不到脚本——它们在游戏里会变成一张没有效果的牌。
    """

    missing: List[Tuple[int, str, str]] = []
    for card_id, name, card_type, alias in cards:
        if not needs_script(card_type):
            continue
        if not has_script(card_id, alias, scripts):
            missing.append((card_id, name, hex(card_type)))
    return missing


def needs_script(card_type: int) -> bool:
    """这张卡是否本该有脚本（通常怪兽与衍生物本来就没有效果）。"""

    if card_type & TYPE_TOKEN or card_type & TYPE_NORMAL:
        return False
    return bool(card_type & (TYPE_MONSTER | TYPE_SPELL | TYPE_TRAP))


def _strip_lua_comments(text: str) -> str:
    """去掉 Lua 注释，只在代码里找引用。

    不然卡名注释会把检查带偏：``--F.A.シティGP``、``--U.A.リベロスパイカー`` 这类名字
    看起来正好是"某个大写命名空间的字段访问"，实测凭空造出 A/B/C/... 一堆假缺失。
    """

    text = re.sub(r"--\[\[.*?\]\]", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def check_library_globals(script_dir: Path) -> Tuple[List[str], Dict[str, int]]:
    """找出"卡脚本引用了、但公共库里没有定义"的全局辅助对象。

    **为什么要专门查这个**：这套脚本是混装的（官方 CardScripts + 内核同源脚本），
    卡脚本与公共库（``constant.lua`` / ``utility.lua`` / ``procedure.lua``）来自不同快照时，
    卡脚本会引用库里的新辅助（例如 ``FusionSpell``）——库里没有，于是这张卡**一加载就报
    Lua 错误、整局直接崩**，玩家看到的是"卡不能载入"。实测 185 张卡就这么废了。

    Returns:
        ``(缺失的名字列表, {名字: 引用它的卡脚本数})``。
    """

    # 库里定义/挂在哪些顶层名字上：`Auxiliary.X = ...`、`FusionSpell = {}`、`aux.f = ...`
    defined: Set[str] = set()
    library_files = [
        path
        for path in script_dir.glob("*.lua")
        if not re.match(r"^c\d", path.name)
    ]
    for path in library_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        defined |= set(re.findall(r"^\s*([A-Za-z_]\w*)\s*[.=]", text, re.M))

    # 卡脚本引用到的顶层名字：必须是大写开头的命名空间或已知小写辅助（避免把局部变量当引用）
    allowed_lower = {"aux", "Auxiliary"}
    used: Dict[str, int] = {}
    for path in script_dir.glob("c*.lua"):
        text = _strip_lua_comments(path.read_text(encoding="utf-8", errors="replace"))
        # 同一个文件里声明过的 local 名字都不算"引用了全局"：
        # 卡脚本常写 `local A,B,C=...` 或把 local 写在函数体里，所以按行扫、并按逗号拆开
        locals_here: Set[str] = set()
        for decl in re.findall(r"\blocal\s+([\w\s,]+)", text):
            locals_here |= {part.strip() for part in decl.split(",") if part.strip().isidentifier()}
        candidates = set(re.findall(r"\b([A-Z][A-Za-z_]*)\s*\.", text))
        candidates |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\.", text)) & allowed_lower
        for name in candidates - locals_here:
            used[name] = used.get(name, 0) + 1
    # 内核与标准库提供的全局（不是卡脚本自己要用的辅助），不算缺失
    builtin = {
        "Duel", "Card", "Effect", "Group", "Debug", "Util", "Coroutine", "Lang", "OCG",
        "Bit", "Math", "Table", "String", "Os", "Io", "Aux", "EFFECT", "TYPE", "LOCATION",
        "ATTRIBUTE", "RACE", "PHASE", "POS", "REASON", "TIMING", "SUMMON", "RESET", "EVENT",
        "CATEGORY", "OPCODE", "PLAYER", "LINK", "DUEL", "CHAININFO", "EFFECT_FLAG", "SET",
        "HINT", "Link",
    }
    missing = sorted(
        name for name in used if name not in defined and name not in builtin
    )
    affected = {name: used[name] for name in missing}
    return missing, affected


def check_modern_apis(script_dir: Path, sample: int) -> Dict[str, List[str]]:
    """抽样检查脚本是否用到这套内核没有的接口。

    Returns:
        ``{接口名: [文件名…]}``，空字典表示抽样范围内没发现不配套的脚本。
    """

    hits: Dict[str, List[str]] = {}
    files = sorted(script_dir.glob("c*.lua"))
    if sample and len(files) > sample:
        step = max(1, len(files) // sample)
        files = files[::step]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for api in MODERN_ONLY_APIS:
            if api in text:
                hits.setdefault(api, []).append(path.name)
    return hits


def check_deck(path: Path, cards: Dict[int, Tuple[str, int, int]], scripts: Set[str]) -> List[Tuple[int, str]]:
    """体检一副 .ydk，返回「本该有脚本却没有」的卡。"""

    ids: List[int] = []
    section = "main"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("!"):
            section = "extra" if "extra" in line.lower() else "main"
            continue
        if section == "side":
            continue
        if line.isdigit():
            ids.append(int(line))
    bad: List[Tuple[int, str]] = []
    for card_id in ids:
        name, card_type, alias = cards.get(card_id, (f"#{card_id}", 0, 0))
        if not needs_script(card_type):
            continue
        if not has_script(card_id, alias, scripts):
            bad.append((card_id, name))
    return bad


async def run_selfplay(args: argparse.Namespace, report) -> int:
    """真打一局：内核 + 闸门 + 两个代打，把内核的 Lua 报错和这局的动作统计都抓出来。

    走的是插件自己那条链路（``DuelSession``），所以顺带验证了闸门与记录器；第二个代打
    用**群友口令**连闸门，对闸门来说它就是玩家，于是两边都能自动出牌、打完整一局。
    """

    if not args.ygopro_exe or not args.windbot_exe or not args.windbot_dir:
        report("自检对局需要 --ygopro-exe / --windbot-exe / --windbot-dir")
        return 2
    ygopro_dir = Path(args.ygopro_dir)
    windbot_dir = Path(args.windbot_dir)
    decks_dir = windbot_dir / "Decks"
    decks = sorted(decks_dir.glob("AI_*.ydk")) if decks_dir.is_dir() else []
    if not decks:
        report(f"WindBot 目录里找不到卡表：{decks_dir}")
        return 2
    # 想体检某副牌就用 --deck 指定的第一副，否则用 WindBot 自带第一副。
    #
    # **必须转成绝对路径**：WindBot 是以自己的工作目录为基准找 `DeckFile=` 的，相对路径会
    # 指向 WindBot 目录下的同名文件；找不到文件时它不会报错，而是静默丢掉整副卡组，
    # 表现为"房间开好了但一个回合都打不起来"（实测被这个坑绕了十几分钟）。
    human_deck = (Path(args.deck[0]) if args.deck else decks[0]).resolve()
    if not human_deck.is_file():
        report(f"找不到要体检的卡表：{human_deck}")
        return 2

    errors: List[str] = []
    noise: List[str] = []
    logger = _collector(errors, noise)
    config = SessionConfig(
        ygopro_executable=Path(args.ygopro_exe),
        ygopro_dir=ygopro_dir,
        windbot_executable=Path(args.windbot_exe),
        windbot_dir=windbot_dir,
        cards_cdb=Path(args.cards_cdb) if args.cards_cdb else ygopro_dir / "cards.cdb",
        bot_name="自检甲",
        # 出牌风格必须挑「卡表文件真实存在」的卡组：Test / Lucky 都指向 AI_Test.ydk，
        # 而发行版里没有这个文件，WindBot 会在开局读卡表时抛 NullReferenceException。
        windbot_deck="Blue-Eyes",
        public_host="127.0.0.1",
        listen_host="127.0.0.1",
        listen_port=0,
        join_timeout=args.selfplay_seconds,
        max_duration=args.selfplay_seconds * 2,
    )
    session = DuelSession(
        config,
        group_id="selfplay",
        logger=logger,
        # 带上卡名库：不然战报里所有卡都显示成 #卡号，看不出 bot 到底认不认得这些卡——
        # 而这恰恰是"补卡数据有没有生效"最直接的证据（实测在这上面误判过一次）
        card_db=CardDatabase(config.cards_cdb) if config.cards_cdb else None,
    )
    info = await session.start()
    report(f"房间已开好：闸门端口 {info.port}（内核端口 {info.backend_port}），这就让两个代打进去")
    opponent = await _start_windbot(
        Path(args.windbot_exe), windbot_dir, info.port, "自检乙", human_deck,
        password=info.password, db_path=config.cards_cdb,
    )
    try:
        outcome = await session.wait_finished()
    finally:
        await opponent.stop()
        await session.stop()

    summary = session.summary_lines()
    report(f"对局结束原因：{outcome}，时长 {session.result_dict().get('duration_seconds')} 秒")
    if summary:
        report("这局的动作统计：")
        for line in summary:
            report("  " + line)
    if args.verbose and noise:
        report(f"内核输出 {len(noise)} 行，最后 5 行：")
        for line in noise[-5:]:
            report("  " + line)
    if errors:
        report(f"内核报了 {len(errors)} 条脚本错误：")
        for line in errors[:20]:
            report("  " + line)
        return 1
    if outcome == OUTCOME_NO_PLAYER:
        report("两个代打没能在时限内开局，本次自检不算数（把 --selfplay-seconds 调大再试）")
        return 2
    turns = int(session.result_dict().get("turns") or 0)
    if turns == 0:
        # 一个回合都没打起来，最常见的原因是牌组根本没被装上——这不能算"体检通过"，
        # 不然工具会给出虚假的安心感（实测踩过：DeckFile 用了相对路径，WindBot 静默丢牌）
        report("对局一个回合都没打起来，本次自检不算数。常见原因：")
        report(f"  1. 卡表路径不可达（WindBot 以工作目录为基准找 DeckFile=，找不到就静默丢牌）：{human_deck}")
        report("  2. 牌组不合规（主卡组不足 40 张、额外超 15 张、卡号库里没有）")
        report("  3. 时限太短（把 --selfplay-seconds 调大）")
        return 2
    report("内核没有报任何脚本错误。")
    return 0


class _Spawned:
    """已经起来的 WindBot 进程，收摊时结束它。"""

    def __init__(self, process) -> None:
        self._process = process

    async def stop(self) -> None:
        """结束进程。"""

        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()


async def _start_windbot(
    exe: Path,
    working_dir: Path,
    port: int,
    name: str,
    deck: Path,
    *,
    password: str = "",
    db_path: Optional[Path] = None,
) -> "_Spawned":
    """起一个 WindBot 占座。"""

    settings = WindBotSettings(
        name=name, deck="Test", deck_file=deck, version=0x1362, password=password, db_path=db_path
    )
    process = await asyncio.create_subprocess_exec(
        str(exe), *settings.to_args("127.0.0.1", port), cwd=str(working_dir),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return _Spawned(process)


def _collector(error_sink: List[str], line_sink: Optional[List[str]] = None):
    """造一个把内核输出收进列表的 logger：报错单独收一份，其余按需收。"""

    import logging

    logger = logging.getLogger("check_engine_data")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    class _Handler(logging.Handler):
        """从内核输出里挑出 Lua 报错。"""

        def emit(self, record: logging.LogRecord) -> None:
            text = record.getMessage()
            if line_sink is not None:
                line_sink.append(text)
            match = LUA_ERROR_PATTERN.search(text)
            if match:
                error_sink.append(f"{match.group(1)}:{match.group(2)} {match.group(3)}")

    logger.addHandler(_Handler())
    return logger


def main() -> int:
    """离线体检 +（可选）自检对局。"""

    args = parse_args()
    ygopro_dir = Path(args.ygopro_dir)
    cdb = Path(args.cards_cdb) if args.cards_cdb else ygopro_dir / "cards.cdb"
    script_dir = ygopro_dir / "script"
    exit_code = 0

    def report(line: str) -> None:
        print(line, flush=True)

    report(f"工作目录 {ygopro_dir}")
    if not cdb.is_file():
        report(f"  [错误] 找不到 cards.cdb：{cdb}")
        return 2
    if not script_dir.is_dir():
        report(f"  [错误] 找不到脚本目录：{script_dir}")
        return 2
    cards = load_cards(cdb)
    scripts = load_scripts(script_dir)
    report(f"  cards.cdb 卡数 {len(cards)}，脚本 {len(scripts)} 个")

    missing = check_card_coverage(cards, scripts)
    if missing:
        exit_code = 1
        report(f"  [警告] {len(missing)} 张卡既没有脚本也没有别名脚本（在游戏里会没有效果）：")
        for card_id, name, card_type in missing[:10]:
            report(f"        {card_id} {name} ({card_type})")
        if len(missing) > 10:
            report(f"        …还有 {len(missing) - 10} 张")
    else:
        report("  卡片脚本覆盖完整。")

    hits = check_modern_apis(script_dir, args.modern_api_sample)
    if hits:
        exit_code = 1
        report("  [警告] 有脚本用到这套内核没有的接口，说明脚本集与内核不配套：")
        for api, files in hits.items():
            report(f"        {api}: {len(files)} 个脚本，例如 {files[:3]}")
    else:
        report("  抽样脚本没有使用内核不支持的接口。")

    missing_libs, affected = check_library_globals(script_dir)
    if missing_libs:
        exit_code = 1
        report(
            "  [警告] 卡脚本引用了公共库里没有的辅助对象（这些卡一加载就会报 Lua 错误、"
            "对局直接崩，表现为「卡不能载入」）："
        )
        for name in missing_libs:
            report(f"        {name}: 被 {affected.get(name, 0)} 个卡脚本引用，但没有任何库文件定义它")
    else:
        report("  卡脚本引用的公共辅助对象都有定义。")

    cards_by_id = {card_id: (name, card_type, alias) for card_id, name, card_type, alias in cards}
    for deck in args.deck:
        deck_path = Path(deck)
        bad = check_deck(deck_path, cards_by_id, scripts)
        if bad:
            exit_code = 1
            report(f"  [警告] 卡组 {deck_path.name} 里有 {len(bad)} 张卡没有脚本：")
            for card_id, name in bad:
                report(f"        {card_id} {name}")
        else:
            report(f"  卡组 {deck_path.name} 的卡都有脚本。")

    if args.selfplay:
        # 自检对局要真起进程，先关掉可能干扰的日志噪音
        logging.getLogger().setLevel(logging.WARNING)
        started = time.monotonic()
        code = asyncio.run(run_selfplay(args, report))
        exit_code = max(exit_code, code)
        report(f"  自检对局用时 {time.monotonic() - started:.1f} 秒")

    report("体检结束。" if exit_code == 0 else "体检发现问题（见上）。")
    return exit_code


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
