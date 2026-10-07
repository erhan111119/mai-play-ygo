"""把「超先行卡」（未发售 / 先行收录）补进引擎卡库与卡脚本。

**为什么需要这个工具**：机器人是靠引擎的 ``cards.cdb`` 认识卡的——库里没有的卡，它看到的就是
一张白板（症状是"整局不认识、不出牌、效果全空"，我们踩过一次：群友 55 张的投稿卡里有 35 张
本地不认得）。而先行卡由社区项目单独维护（官方发行包只带已发售的卡），每出新卡就得补一次；
这个脚本把这件事做成一条命令。

数据源：``ElderLich/TransSuperpre``（MDPro3 客户端用的那份超先行包，社区用 PR 持续更新，
``version.txt`` 是数据时间戳）。下载走 :func:`duel.netguard.guarded_urlopen`：只允许 https、
只连公网地址、限制响应体积——**不因为"这是个熟名字的源"就额外信任它**。

四条安全边界（改这个文件时别越过）：

1. 只写 ``paths.ygopro_dir`` / ``paths.windbot_dir`` 里的 ``cards.cdb`` 与 ``script/``，
   写入路径全部由配置与固定文件名拼出来，不接受"写到任意路径"的参数；
2. 写入前一定备份（``cards.cdb.bak-<时间戳>``），合并只做 ``INSERT OR IGNORE`` 与显式 ``UPDATE``，
   绝不删除任何行；
3. 解包**只挑需要的条目**（根目录的 ``*.cdb`` 与 ``script/c<卡号>.lua``），逐个校验落点，
   拒绝绝对路径与 ``../``（zip slip）；卡图、pack 列表一概不落盘；
4. SQL 一律内联在 ``execute()`` 调用里、值只走占位符（与 ``train/store.py`` 同一约定），
   表结构只用于核对，不合就报错。

用法::

    # 只看差多少（会联网取最新包，但不动数据库）
    python tools/update_card_data.py --check-only

    # 下载最新包并补齐（引擎库 + WindBot 目录下能找到的副本）
    python tools/update_card_data.py

    # 用本地已有的包补齐（不联网）
    python tools/update_card_data.py --package D:/下载/ygopro-super-pre.ypk

补卡是幂等的：库里已有的卡不会被覆盖（只有上次由本工具补进去的先行卡会随上游更新而更新）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
import zipfile

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from duel.netguard import UnsafeUrlError, guarded_urlopen  # noqa: E402  导入顺序受 sys.path 补丁影响

# 超先行包的下载地址（同一个目录下还有 version.txt 记录数据时间戳）
DEFAULT_BASE_URL = (
    "https://raw.githubusercontent.com/ElderLich/TransSuperpre/main/ZH-TW/ygopro-super-pre.ypk"
)
# 包只有十几 MB；给足余量但别让异常响应把内存吃满
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_VERSION_BYTES = 4096
# 卡库结构（mycard 系 cards.cdb 的固定两张表）：只用来核对结构，SQL 本身内联在调用处
_DATAS_COLUMNS = (
    "id",
    "ot",
    "alias",
    "setcode",
    "type",
    "atk",
    "def",
    "level",
    "race",
    "attribute",
    "category",
)
_TEXTS_COLUMNS = (
    "id",
    "name",
    "desc",
    "str1",
    "str2",
    "str3",
    "str4",
    "str5",
    "str6",
    "str7",
    "str8",
    "str9",
    "str10",
    "str11",
    "str12",
    "str13",
    "str14",
    "str15",
    "str16",
)
# 卡脚本的文件名形状：script/c<卡号>.lua
_SCRIPT_DIR = "script"
_SCRIPT_NAME = re.compile(r"^c\d+\.lua$")


class UpdateError(RuntimeError):
    """更新流程失败（网络、包结构、数据库读写）。"""


# ---------------------------------------------------------------------- 路径与配置


def load_config_paths(plugin_root: Path) -> Dict[str, str]:
    """从插件 config.toml 里读路径（与其它工具一致）。"""

    import tomllib

    config_path = plugin_root / "config.toml"
    if not config_path.is_file():
        return {}
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return {key: str(value) for key, value in (data.get("paths") or {}).items()}


def cache_dir(plugin_root: Path) -> Path:
    """下载缓存目录（复用同一份包，方便离线再跑）。"""

    path = plugin_root / "temp" / "card_data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def card_db_targets(paths: Dict[str, str]) -> List[Path]:
    """列出要同步的 cards.cdb：引擎那份是主目标，WindBot 目录下的副本顺手一起补。

    为什么连副本也补：WindBot 启动时先看自己目录下的 ``cards.cdb``（``DbPath=`` 是后面才生效的），
    目录里那份太旧会让"手工跑的 WindBot"认不出新卡，排查时非常容易误判。
    """

    targets: List[Path] = []
    ygopro_dir = paths.get("ygopro_dir", "").strip()
    if ygopro_dir:
        targets.append(Path(ygopro_dir) / "cards.cdb")
    windbot_dir = paths.get("windbot_dir", "").strip()
    if windbot_dir:
        targets.append(Path(windbot_dir) / "cards.cdb")
    src_dir = paths.get("windbot_src_dir", "").strip()
    if src_dir:
        # 编译产物目录（bin/*）里若有 cards.cdb，也一并补上
        for candidate in sorted(Path(src_dir).glob("bin/*/cards.cdb")):
            targets.append(candidate)
    seen: set = set()
    unique: List[Path] = []
    for path in targets:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


# ---------------------------------------------------------------------- 下载与解包


def fetch_package(url: str, target: Path, *, timeout: int = 120) -> Tuple[Path, str]:
    """下载超先行包与它的版本戳。

    Raises:
        UpdateError: 地址不合规、网络失败或响应过大。
    """

    try:
        payload = guarded_urlopen(url, timeout=timeout, max_bytes=MAX_PACKAGE_BYTES)
    except UnsafeUrlError as exc:
        raise UpdateError(f"下载地址不合规：{exc}") from exc
    except OSError as exc:
        raise UpdateError(f"下载失败：{exc}") from exc
    target.write_bytes(payload)

    version = ""
    version_url = url.rsplit("/", 1)[0] + "/version.txt"
    try:
        raw = guarded_urlopen(version_url, timeout=timeout, max_bytes=MAX_VERSION_BYTES)
        version = raw.decode("utf-8", errors="replace").strip()
    except (UnsafeUrlError, OSError) as exc:
        # 版本戳只是展示用，取不到不影响补卡，但要说一声
        print(f"[提示] 没取到 version.txt：{exc}")
    return target, version


def _pick_members(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """挑出要解压的条目：根目录的 ``*.cdb`` 与 ``script/c<卡号>.lua``。

    只解这两类，是为了把"落盘的东西"限制在看得见的范围内：卡图、pack 列表、说明文件都不需要。
    """

    picked: List[zipfile.ZipInfo] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if "/" not in name:
            if name.lower().endswith(".cdb"):
                picked.append(info)
            continue
        parts = name.split("/")
        if len(parts) == 2 and parts[0] == _SCRIPT_DIR and _SCRIPT_NAME.match(parts[1]):
            picked.append(info)
    return picked


def _reject_unsafe_names(archive: zipfile.ZipFile) -> None:
    """扫描包里所有条目名：出现绝对路径或 ``../`` 就直接报错。

    为什么"不挑它"还不够：白名单只解 ``*.cdb`` 与 ``script/c*.lua``，越界条目本来就选不上，
    但**包里出现越界路径本身就是可疑信号**（要么包被改过，要么打包工具出了问题），
    静默忽略会让人以为"这个源是干净的"。宁可停下来说清楚。

    Raises:
        UpdateError: 有条目名越界。
    """

    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts:
            continue
        if name.startswith("/") or ":" in parts[0]:
            raise UpdateError(f"包里有绝对路径条目，拒绝解压：{name}")
        if ".." in parts:
            raise UpdateError(f"包里有越界条目（../），拒绝解压：{name}")


def extract_package(package: Path, staging: Path) -> Path:
    """把包里需要的条目解到暂存目录，返回该目录。

    先整体扫一遍条目名（见 :func:`_reject_unsafe_names`），再逐个校验落点并要求确实落在
    暂存目录下；只有固定形状的条目会落盘。

    Raises:
        UpdateError: 包损坏、条目越界或写盘失败。
    """

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    root = staging.resolve()
    try:
        with zipfile.ZipFile(package) as archive:
            _reject_unsafe_names(archive)
            for info in _pick_members(archive):
                name = info.filename.replace("\\", "/")
                target = (root / name).resolve()
                if target != root and root not in target.parents:
                    raise UpdateError(f"包里有越界条目，拒绝解压：{name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"解包失败：{exc}") from exc
    return staging


def overlay_databases(staging: Path) -> List[Path]:
    """包里带的卡库（``test-release.cdb`` / ``test-update.cdb``）。"""

    found = sorted(path for path in staging.glob("*.cdb") if path.is_file())
    if not found:
        raise UpdateError(f"包里没有找到 .cdb：{staging}")
    return found


def overlay_scripts(staging: Path) -> Dict[int, Path]:
    """包里带的卡脚本：``{卡号: 脚本路径}``。"""

    result: Dict[int, Path] = {}
    script_dir = staging / _SCRIPT_DIR
    if not script_dir.is_dir():
        return result
    for path in script_dir.glob("c*.lua"):
        if not _SCRIPT_NAME.match(path.name):
            continue
        result[int(path.stem[1:])] = path
    return result


# ---------------------------------------------------------------------- 卡库合并


def check_schema(connection: sqlite3.Connection) -> None:
    """核对表结构：和写死的列名不一致就报错，绝不硬着头皮写。

    Raises:
        UpdateError: 缺少表或列名不同。
    """

    datas = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(datas)"))
    if not datas:
        raise UpdateError("卡库里没有 datas 表")
    if datas != _DATAS_COLUMNS:
        raise UpdateError(f"卡库结构不匹配：datas 的列是 {datas}")
    texts = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(texts)"))
    if not texts:
        raise UpdateError("卡库里没有 texts 表")
    if texts != _TEXTS_COLUMNS:
        raise UpdateError(f"卡库结构不匹配：texts 的列是 {texts}")


def existing_ids(connection: sqlite3.Connection) -> set:
    """库里已有的卡号。"""

    return {int(row[0]) for row in connection.execute("SELECT id FROM datas")}


def read_overlay(overlay: Path) -> Dict[int, Dict[str, Tuple[object, ...]]]:
    """把包里的卡库读成 ``{卡号: {"datas": 整行, "texts": 整行}}``。"""

    rows: Dict[int, Dict[str, Tuple[object, ...]]] = {}
    connection = sqlite3.connect(str(overlay))
    try:
        check_schema(connection)
        for row in connection.execute(
            "SELECT id, ot, alias, setcode, type, atk, def, level, race, attribute, category"
            " FROM datas"
        ):
            rows.setdefault(int(row[0]), {})["datas"] = tuple(row)
        for row in connection.execute(
            "SELECT id, name, desc, str1, str2, str3, str4, str5, str6, str7, str8, str9, str10,"
            " str11, str12, str13, str14, str15, str16 FROM texts"
        ):
            rows.setdefault(int(row[0]), {})["texts"] = tuple(row)
    finally:
        connection.close()
    return rows


def insert_card(
    connection: sqlite3.Connection, datas_row: Sequence[object], texts_row: Sequence[object]
) -> None:
    """插入一张新卡（两张表各一行）。"""

    connection.execute(
        "INSERT OR IGNORE INTO datas (id, ot, alias, setcode, type, atk, def, level, race,"
        " attribute, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        datas_row,
    )
    connection.execute(
        "INSERT OR IGNORE INTO texts (id, name, desc, str1, str2, str3, str4, str5, str6, str7,"
        " str8, str9, str10, str11, str12, str13, str14, str15, str16)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        texts_row,
    )


def update_card(
    connection: sqlite3.Connection, datas_row: Sequence[object], texts_row: Sequence[object]
) -> None:
    """更新一张"本工具补进去过"的先行卡（上游会改先行卡的数据）。"""

    connection.execute(
        "UPDATE datas SET ot = ?, alias = ?, setcode = ?, type = ?, atk = ?, def = ?, level = ?,"
        " race = ?, attribute = ?, category = ? WHERE id = ?",
        tuple(datas_row[1:]) + (datas_row[0],),
    )
    connection.execute(
        "UPDATE texts SET name = ?, desc = ?, str1 = ?, str2 = ?, str3 = ?, str4 = ?, str5 = ?,"
        " str6 = ?, str7 = ?, str8 = ?, str9 = ?, str10 = ?, str11 = ?, str12 = ?, str13 = ?,"
        " str14 = ?, str15 = ?, str16 = ? WHERE id = ?",
        tuple(texts_row[1:]) + (texts_row[0],),
    )


def merge_into(
    target: Path,
    overlay_rows: Dict[int, Dict[str, Tuple[object, ...]]],
    *,
    installed_before: set,
) -> Tuple[int, int]:
    """把包里的卡合并进目标卡库；返回 ``(新增, 更新)``。

    规则：库里没有的卡直接插入；**上次由本工具补进去的先行卡会被更新**（上游会改先行卡的数据），
    其余已有卡一律不动——已发售卡的数据由主卡库负责，不能被这份包降级覆盖。
    """

    added = 0
    updated = 0
    connection = sqlite3.connect(str(target))
    try:
        check_schema(connection)
        have = existing_ids(connection)
        for card_id, tables in overlay_rows.items():
            datas_row = tables.get("datas")
            texts_row = tables.get("texts")
            if datas_row is None or texts_row is None:
                continue
            if card_id not in have:
                insert_card(connection, datas_row, texts_row)
                added += 1
            elif card_id in installed_before:
                update_card(connection, datas_row, texts_row)
                updated += 1
        connection.commit()
    finally:
        connection.close()
    return (added, updated)


def install_scripts(
    scripts: Dict[int, Path], *, ygopro_dir: Path, installed_before: set
) -> Tuple[int, int]:
    """把包里的卡脚本补进引擎 ``script/``；返回 ``(新增, 覆盖)``。

    引擎的其它脚本来自官方脚本库（对已发售卡更权威），所以这里**只补缺口**，外加覆盖
    "本来就是我们补进去的先行卡"那几个脚本——不然先行卡的脚本更新永远进不来。
    """

    target_dir = ygopro_dir / _SCRIPT_DIR
    if not target_dir.is_dir():
        raise UpdateError(f"找不到脚本目录：{target_dir}")
    added = 0
    replaced = 0
    for card_id, source in sorted(scripts.items()):
        if not _SCRIPT_NAME.match(source.name):
            raise UpdateError(f"脚本文件名不合规：{source.name}")
        destination = target_dir / f"c{card_id}.lua"
        if destination.is_file():
            if card_id in installed_before:
                shutil.copyfile(source, destination)
                replaced += 1
            continue
        shutil.copyfile(source, destination)
        added += 1
    return (added, replaced)


# ---------------------------------------------------------------------- 清单与备份


def load_installed(path: Path) -> set:
    """读"上次补进去的卡号"清单（文件不存在或坏了就当空集）。"""

    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {int(item) for item in data.get("ids", [])}


def save_installed(path: Path, ids: Sequence[int]) -> None:
    """写清单（排序后写，方便人看差异）。"""

    path.write_text(
        json.dumps({"ids": sorted(int(item) for item in ids)}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def backup(target: Path) -> Path:
    """合并前备份卡库。"""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copyfile(target, copy)
    return copy


def read_card_names(staging: Path, card_ids: Sequence[int]) -> Dict[int, str]:
    """从包里的卡库读卡名（报告用）。"""

    names: Dict[int, str] = {}
    wanted = set(card_ids)
    for overlay in sorted(staging.glob("*.cdb")):
        connection = sqlite3.connect(str(overlay))
        try:
            for card_id, name in connection.execute("SELECT id, name FROM texts"):
                if int(card_id) in wanted:
                    names[int(card_id)] = str(name)
        except sqlite3.DatabaseError:
            continue
        finally:
            connection.close()
    return names


# ---------------------------------------------------------------------- 主流程


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="把超先行卡补进引擎卡库与卡脚本")
    parser.add_argument("--plugin-root", default=str(_PLUGIN_ROOT), help="插件根目录（读 config.toml）")
    parser.add_argument("--ygopro-dir", default="", help="ygopro 工作目录；默认读 config.toml")
    parser.add_argument("--windbot-dir", default="", help="WindBot 工作目录；默认读 config.toml")
    parser.add_argument(
        "--source-url", default=DEFAULT_BASE_URL, help="超先行包地址（只允许 https 公网地址）"
    )
    parser.add_argument("--package", default="", help="用本地已有的 .ypk，不联网")
    parser.add_argument("--check-only", action="store_true", help="只报告差多少张，不改任何文件")
    parser.add_argument("--timeout", type=int, default=120, help="下载超时（秒）")
    return parser.parse_args()


def resolve_package(args: argparse.Namespace, plugin_root: Path) -> Tuple[Path, str]:
    """拿到要用的包：本地指定的、或下载最新的。"""

    if args.package:
        package = Path(args.package).resolve()
        if not package.is_file():
            raise UpdateError(f"找不到本地包：{package}")
        print(f"使用本地包：{package}")
        return package, ""
    package = cache_dir(plugin_root) / "ygopro-super-pre.ypk"
    print(f"下载超先行包：{args.source_url}")
    package, version = fetch_package(args.source_url, package, timeout=args.timeout)
    print(f"下载完成：{package}（{package.stat().st_size // 1024} KB）")
    return package, version


def main() -> int:
    """入口。"""

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = parse_args()
    plugin_root = Path(args.plugin_root)
    paths = load_config_paths(plugin_root)
    if args.ygopro_dir:
        paths["ygopro_dir"] = args.ygopro_dir
    if args.windbot_dir:
        paths["windbot_dir"] = args.windbot_dir
    ygopro_dir = Path(paths.get("ygopro_dir", "") or "")
    if not ygopro_dir.is_dir():
        print(f"[错误] 找不到 ygopro 工作目录：{ygopro_dir or '（未配置）'}")
        return 2

    manifest = cache_dir(plugin_root) / "installed.json"
    staging = cache_dir(plugin_root) / "staging"
    try:
        package, version = resolve_package(args, plugin_root)
        if version:
            readable = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(int(version)))
                if version.isdigit()
                else version
            )
            print(f"上游数据时间：{version}（{readable}）")
        staging = extract_package(package, staging)
        overlays = overlay_databases(staging)
        scripts = overlay_scripts(staging)
    except UpdateError as exc:
        print(f"[错误] {exc}")
        return 2

    overlay_rows: Dict[int, Dict[str, Tuple[object, ...]]] = {}
    for overlay in overlays:
        overlay_rows.update(read_overlay(overlay))
    overlay_ids = set(overlay_rows)
    print(f"包内卡库：{', '.join(path.name for path in overlays)}（合计 {len(overlay_ids)} 张）")
    print(f"包内脚本：{len(scripts)} 个")

    targets = card_db_targets(paths)
    main_target = ygopro_dir / "cards.cdb"
    if not main_target.is_file():
        print(f"[错误] 找不到卡库：{main_target}")
        return 2

    connection = sqlite3.connect(str(main_target))
    try:
        check_schema(connection)
        have = existing_ids(connection)
    except UpdateError as exc:
        print(f"[错误] {exc}")
        return 2
    finally:
        connection.close()

    installed_before = load_installed(manifest)
    missing = overlay_ids - have
    print(f"引擎卡库现有 {len(have)} 张，其中缺 {len(missing)} 张先行卡")
    names = read_card_names(staging, sorted(missing)[:8])
    for card_id in sorted(missing)[:8]:
        print(f"  缺：{card_id} {names.get(card_id, '')}")

    if args.check_only:
        print("\n--check-only：未改动任何文件")
        return 0

    print()
    total_added = 0
    total_updated = 0
    for target in targets:
        if not target.is_file():
            print(f"跳过（不存在）：{target}")
            continue
        print(f"已备份：{backup(target).name}")
        added, updated = merge_into(target, overlay_rows, installed_before=installed_before)
        total_added += added
        total_updated += updated
        print(f"  已补：{target}（新增 {added} 张，更新 {updated} 张）")

    script_added, script_replaced = install_scripts(
        scripts, ygopro_dir=ygopro_dir, installed_before=installed_before
    )
    print(f"脚本：新增 {script_added} 个，覆盖先行卡脚本 {script_replaced} 个")

    connection = sqlite3.connect(str(main_target))
    try:
        after = existing_ids(connection)
    finally:
        connection.close()
    still_missing = overlay_ids - after
    if still_missing:
        print(f"[警告] 仍有 {len(still_missing)} 张没补进去（包里的卡库可能不完整）")
    save_installed(manifest, sorted(installed_before | overlay_ids))
    print(f"清单已更新：{manifest}")
    print(f"\n完成：引擎卡库 {len(have)} → {len(after)} 张（新增 {total_added}，更新 {total_updated}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
