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


def pick_config_file(plugin_root: Path) -> Path:
    """挑出要读的配置文件：本机的 ``config.toml``，没复制过就用仓库里的模板。

    仓库里只放 `config.toml.example`（真正在跑的 config.toml 是本地文件、已 gitignore），
    所以新克隆的仓库上直接跑工具时按模板的默认值来（路径指向自带的 `clients/`），
    并且**说一声**用的是哪份，免得"我明明改了路径却没生效"。
    """

    config = plugin_root / "config.toml"
    if config.is_file():
        return config
    example = plugin_root / "config.toml.example"
    if example.is_file():
        print(f"（没找到 {config.name}，按模板 {example.name} 的默认值来；要改路径就复制一份 config.toml）")
        return example
    raise SystemExit(f"既没有 {config} 也没有 {example}：请从仓库里复制 config.toml.example 成 config.toml")


def _config_value(raw: str) -> str:
    """取 ``key = "值"  # 注释`` 里的那个值（**行尾注释必须丢掉**）。

    配置模板每一行都带行内注释，而"只解析这两行"的朴素写法会把注释一起当成路径：
    实测（2026-10-07）`clients/windbot/WindBot.exe"  # WindBot.exe 路径（…）` 被判成
    "文件不存在"，于是每次都报"编译产物还没同步"。
    """

    text = raw.strip()
    for quote in ('"', "'"):
        if text.startswith(quote) and text.count(quote) >= 2:
            return text[1:text.rindex(quote)]
    return text.split("#", 1)[0].strip()


def _read_paths(config: Path) -> Tuple[str, str]:
    """从配置文件里取出 ``windbot_executable`` 与 ``windbot_src_dir``（只解析这两行）。"""

    if not config.is_file():
        raise SystemExit(f"读不到 {config}")
    executable = ""
    source_dir = ""
    for line in config.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("windbot_executable"):
            executable = _config_value(line.partition("=")[2]).replace("\\\\", "\\")
        elif line.startswith("windbot_src_dir"):
            source_dir = _config_value(line.partition("=")[2]).replace("\\\\", "\\")
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
    executable, source_dir = _read_paths(pick_config_file(plugin_root))
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
