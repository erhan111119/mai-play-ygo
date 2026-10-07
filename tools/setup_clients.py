"""把两个虚拟客户端（ygopro 内核 + WindBot 出牌大脑）装进插件的 `clients/` 目录。

**为什么要它**：插件自带的 `clients/` 是"开箱即用"的那一份，但换机器、升级内核、
或者想用另一套环境（比如群友自己编译的 WindBot）时，需要一个可复现的同步入口——
而不是手工挑文件。这个脚本就是那个入口：按固定清单从源目录拷，拷完逐项校验。

用法::

    python tools/setup_clients.py --check-only                  # 只检查现有 clients/ 齐不齐
    python tools/setup_clients.py --from <你的 ygopro 环境>      # 从某个环境同步过来
    python tools/setup_clients.py --from <环境> --windbot-src <WindBot 源码树>

口径：

* 只**新增/覆盖** `clients/` 里的文件，不动别处；覆盖前逐项校验目标路径在 `clients/` 之内；
* `WindBot.exe` 优先取源码树的 Release 构建（`<windbot-src>/bin/Release/WindBot.exe`）——
  那份才是"带本插件定制执行器"的最新构建；没有源码树时用运行目录里那份；
* 卡图（`clients/art/`）与 WindBot 源码树**不在同步范围内**（太大、且卡图版权归属不明），
  要用就把 `config.toml` 的 `paths.card_art_dir` / `paths.windbot_src_dir` 指过去。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Sequence

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CLIENTS = _PLUGIN_ROOT / "clients"

#: ygopro 内核：`(源文件/目录名, 目标相对 clients/ygopro 的路径)`
YGOPRO_ITEMS: Sequence[str] = (
    "ygopro.exe",
    "cards.cdb",
    "strings.conf",
    "lflist.conf",
    "script",
    "expansions",
)

#: WindBot：运行目录里要带的东西
WINDBOT_ITEMS: Sequence[str] = ("WindBot.exe.config", "Decks", "Dialogs", "x64")

#: 这些是运行期产物，不进包（同步时跳过、检查时忽略）
RUNTIME_JUNK = ("replay", "MaiBotPlans", ".mimosa", "__pycache__")


def check(clients: Path) -> List[str]:
    """列出自带客户端缺少的关键文件（空列表 = 齐了）。"""

    missing: List[str] = []
    for name in ("ygopro.exe", "cards.cdb", "lflist.conf", "strings.conf"):
        if not (clients / "ygopro" / name).is_file():
            missing.append(f"clients/ygopro/{name}")
    if not (clients / "ygopro" / "script").is_dir():
        missing.append("clients/ygopro/script/（卡牌脚本目录）")
    for name in ("WindBot.exe",):
        if not (clients / "windbot" / name).is_file():
            missing.append(f"clients/windbot/{name}")
    if not (clients / "windbot" / "x64" / "sqlite3.dll").is_file():
        missing.append("clients/windbot/x64/sqlite3.dll")
    if not (clients / "windbot" / "Decks").is_dir():
        missing.append("clients/windbot/Decks/（内置卡组）")
    return missing


def _copy_into(src: Path, dest: Path) -> None:
    """把 src 拷到 dest（目录递归合并，文件覆盖）。"""

    if not src.exists():
        raise SystemExit(f"源里没有：{src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)


def sync(source: Path, clients: Path, windbot_src: Path | None) -> List[str]:
    """从 `source` 同步两个客户端；返回做过的事（供打印）。"""

    done: List[str] = []
    ygopro = source / "ygopro"
    windbot = source / "windbot" / "WindBot"

    for name in YGOPRO_ITEMS:
        _copy_into(ygopro / name, clients / "ygopro" / name)
        done.append(f"ygopro/{name}")

    # WindBot.exe：优先源码树的 Release 构建（带本插件的定制执行器）
    built = (windbot_src / "bin" / "Release" / "WindBot.exe") if windbot_src else None
    if built is not None and built.is_file():
        _copy_into(built, clients / "windbot" / "WindBot.exe")
        done.append("windbot/WindBot.exe（取自源码树 Release 构建）")
    else:
        _copy_into(windbot / "WindBot.exe", clients / "windbot" / "WindBot.exe")
        done.append("windbot/WindBot.exe（取自 WindBot 运行目录）")

    for name in WINDBOT_ITEMS:
        _copy_into(windbot / name, clients / "windbot" / name)
        done.append(f"windbot/{name}")

    # MIT 许可要跟着 WindBot 一起走
    license_file = (windbot_src / "LICENSE") if windbot_src else (windbot / "LICENSE")
    if license_file.is_file():
        _copy_into(license_file, clients / "windbot" / "LICENSE")
        done.append("windbot/LICENSE")

    (clients / "ygopro" / "replay").mkdir(parents=True, exist_ok=True)
    return done


def parse_args() -> argparse.Namespace:
    """命令行：--from 源目录 / --check-only / --clients 目标目录。"""

    parser = argparse.ArgumentParser(description="同步插件自带的两个虚拟客户端")
    parser.add_argument("--from", dest="source", type=Path, default=None,
                        help="源环境目录（里面应有 ygopro/ 与 windbot/WindBot/）；"
                             "同步时必须给，--check-only 不用")
    parser.add_argument("--windbot-src", type=Path, default=None,
                        help="WindBot 源码树（有 bin/Release/WindBot.exe 时优先用它）")
    parser.add_argument("--clients", type=Path, default=CLIENTS, help="目标 clients/ 目录")
    parser.add_argument("--check-only", action="store_true", help="只检查，不写任何文件")
    return parser.parse_args()


def main() -> int:
    """入口：检查或同步。"""

    args = parse_args()
    clients: Path = args.clients

    if args.check_only:
        missing = check(clients)
        if missing:
            print("自带客户端缺少这些文件：")
            for item in missing:
                print("  ✘", item)
            print("→ 用 `python tools/setup_clients.py --from <源环境>` 同步一份过来")
            return 1
        total = sum(1 for path in clients.rglob("*") if path.is_file())
        print(f"✔ 自带客户端齐了：{clients}（{total} 个文件）")
        return 0

    if args.source is None:
        # 不猜默认盘位：猜错会从"不存在的目录"拷，报出来像真错误
        raise SystemExit("同步要给 --from <源环境目录>（里面应有 ygopro/ 与 windbot/）；"
                         "只想体检用 --check-only")
    if args.windbot_src is None:
        guess = args.source / "windbot-src"
        args.windbot_src = guess if (guess / "bin" / "Release" / "WindBot.exe").is_file() else None
    done = sync(args.source, clients, args.windbot_src)
    for item in done:
        print("  同步", item)
    missing = check(clients)
    if missing:
        print("同步完仍缺：" + "、".join(missing))
        return 1
    print(f"✔ 两个客户端已就位：{clients}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
