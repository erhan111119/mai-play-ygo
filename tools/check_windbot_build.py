"""核对"对局实际在用的 WindBot.exe"是不是**刚编译出来的那一份**。

**为什么需要它**：改执行器时的常见部署形态是"源码树在一处、运行目录在另一处"——
``paths.windbot_src_dir`` 是源码树，而 ``paths.windbot_executable`` 指向实际跑对局的那份 exe。
两者不同步时，**改了半天执行器，真实对局里一行都没生效**：本项目真踩过——连续几版的手坑禁令、
选卡规则、额外区挑格都没进到房间局，而擂台（用的是编译产物）一切正常，于是"实验室里好的、
群里还是老样子"。

做法：把编译产物（``<windbot_src_dir>/bin/Release/WindBot.exe``）与配置指向的那份做哈希比对，
不一致就明确报出来并给出覆盖命令；``--deploy`` 直接备份并覆盖。

用法::

    uv run python tools/check_windbot_build.py            # 只检查
    uv run python tools/check_windbot_build.py --deploy   # 不一致时备份并覆盖
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import argparse
import hashlib
import shutil
import sys


def _read_paths(config: Path) -> Tuple[str, str]:
    """从 config.toml 里取出 ``windbot_executable`` 与 ``windbot_src_dir``（只解析这两行）。"""

    if not config.is_file():
        raise SystemExit(f"读不到 {config}")
    executable = ""
    source_dir = ""
    for line in config.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("windbot_executable"):
            executable = line.partition("=")[2].strip().strip('"').replace("\\\\", "\\")
        elif line.startswith("windbot_src_dir"):
            source_dir = line.partition("=")[2].strip().strip('"').replace("\\\\", "\\")
    if not executable or not source_dir:
        raise SystemExit(f"{config} 里缺 windbot_executable 或 windbot_src_dir")
    return executable, source_dir


def _digest(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_runtime(executable: Path, source_dir) -> Path:
    """**插件实际会启动哪一份 exe**（与 ``plugin.py:_resolve_windbot_executable`` 同一条规则）。

    规则：配了 ``paths.windbot_src_dir`` 且那份 ``bin/Release/WindBot.exe`` 存在 → **用编译产物**；
    否则用配置里那个 ``windbot_executable``。
    ⚠ 0.21.89 之前这里只比"编译产物 vs 配置副本"，于是漏掉过一次很难发现的错位：
    真人局跑的是 ``bin/Release``，而"部署"却只覆盖了配置副本（两者恰好同哈希时更看不出来）。
    """

    if str(source_dir):
        built = Path(source_dir) / "bin" / "Release" / "WindBot.exe"
        if built.is_file():
            return built
    return executable


def _check_sources_compiled(source_dir, built: Path) -> int:
    """源树里改过、但编译产物没跟上（或上次编译**没落地**）时报警——这两条都会让"改了却没用上"。"""

    if not str(source_dir) or not built.is_file():
        return 0
    source_root = Path(source_dir)
    newest = 0.0
    newest_path: Optional[Path] = None
    for path in (source_root / "Game").rglob("*.cs"):
        mtime = path.stat().st_mtime
        if mtime > newest:
            newest, newest_path = mtime, path
    if newest_path is None:
        return 0
    built_mtime = built.stat().st_mtime
    if newest > built_mtime:
        print(
            "**源码比编译产物新**：{} 改过但没编译——这一版的改动在真实对局里不会生效".format(
                newest_path.relative_to(source_root)
            )
        )
        return 2
    obj_direct = source_root / "obj" / "Release" / "WindBot.exe"
    if obj_direct.is_file() and obj_direct.stat().st_mtime > built_mtime:
        print(
            "**上次编译没落地**：obj/Release 比 bin/Release 新（MSB3027/3021 会静默地只更新 obj，"
            "典型原因是对局还在跑、exe 被锁）——请关掉对局后重编，或把 obj/Release 那份复制成 bin/Release"
        )
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="核对对局实际在用的 WindBot.exe 是否已是最新编译产物")
    parser.add_argument("--plugin-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--deploy", action="store_true", help="不一致时备份并覆盖运行目录那份")
    args = parser.parse_args()

    plugin_root = Path(args.plugin_root)
    executable, source_dir = _read_paths(plugin_root / "config.toml")
    target = Path(executable)
    built = Path(source_dir) / "bin" / "Release" / "WindBot.exe"
    runtime = _resolve_runtime(target, source_dir)

    target_hash = _digest(target)
    built_hash = _digest(built)
    print(f"插件实际会用：{runtime}")
    print(f"编译产物：{built}  {'存在' if built_hash else '**不存在（先编译）**'}")
    print(f"配置副本：{target}  {'存在' if target_hash else '**不存在**'}")
    if built_hash is None:
        return 1

    stale = _check_sources_compiled(source_dir, built)
    if stale:
        return stale

    if runtime == built:
        if target_hash is not None and target_hash != built_hash:
            if args.deploy:
                backup = target.with_suffix(target.suffix + ".bak")
                shutil.copyfile(target, backup)
                shutil.copyfile(built, target)
                print(f"已把编译产物同步到配置副本（备份 {backup.name}）✔")
            else:
                print("（配置副本与编译产物不一致；插件实际用编译产物，所以**不影响对局**——--deploy 可顺手同步）")
        print("一致 ✔ 对局跑的就是最新编译产物")
        return 0

    if built_hash == target_hash:
        print("一致 ✔ 运行中的就是刚编译的那份")
        return 0

    print("**不一致**：对局跑的不是最新编译产物——源码里的改动在真实对局里不会生效")
    print(f"  编译产物 sha256 {built_hash[:16]}")
    print(f"  运行那份 sha256 {target_hash[:16]}")
    if not args.deploy:
        print(f"要同步：uv run python {Path(__file__).name} --deploy")
        return 2

    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copyfile(target, backup)
    shutil.copyfile(built, target)
    print(f"已备份到 {backup.name}，并覆盖为编译产物 ✔（下次开房生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
