"""WindBot 源码树 ↔ 插件留档目录 的同步 / 校验工具。

**为什么需要它**：出牌脚本是 C#（编译进 `WindBot.exe`），源码在 WindBot 源码树里，
插件要能独立地说明"这副牌用的是哪一版脚本"。原来 `train/windbot/` 里是**手工照抄**的副本，
结果两天内就过期了（缺魔女术/卡通，另外三份停在 10-03），而看目录的人不会知道。

现在改成：`train/windbot/sources.json` 记**文件 → sha256**；本工具负责

    python tools/check_windbot_sources.py            # 只校验（默认，读源码树路径）
    python tools/check_windbot_sources.py --sync     # 用源码树刷新留档副本 + 更新哈希

校验口径：留档副本与源码树的同名文件哈希一致，且 `sources.json` 里记的就是源码树的当前哈希。
不一致时打印"哪一侧旧了"，而不是猜。

用法（路径默认从插件 config.toml 推导，可用 --windbot-src 覆盖）：

    python tools/check_windbot_sources.py --windbot-src <WindBot 源码树>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_ARCHIVE_DIR = _PLUGIN_ROOT / "train" / "windbot"
_MANIFEST = _ARCHIVE_DIR / "sources.json"

#: 留档的文件（相对 WindBot 源码树）。改执行器只改源码树，然后用 --sync 刷新这里。
FILES: Tuple[str, ...] = (
    "Game/AI/Decks/RaiseMoonExecutor.cs",
    "Game/AI/Decks/KillerTuneExecutor.cs",
    "Game/AI/Decks/SkyStrikerShopExecutor.cs",
    "Game/AI/Decks/WitchcraftShopExecutor.cs",
    "Game/AI/Decks/ToonShopExecutor.cs",
    "Game/AI/Decks/TraptrixRagnaraikaExecutor.cs",
    "Game/AI/Decks/PlanAwareExecutor.cs",
    "Game/AI/MaiBotBrain.cs",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_source() -> Path:
    """源码树路径：优先环境变量 ``MAIBOT_WINDBOT_SRC`` / 插件配置里的 `paths.windbot_src_dir`。

    两者都没有时**明确报错**（不猜某个盘位——猜错会去校验一个不存在的源码树，
    报出来的"文件缺失"看着像真问题）。
    """
    import os

    from_env = os.environ.get("MAIBOT_WINDBOT_SRC")
    if from_env:
        return Path(from_env)
    configured = _configured_source()
    if configured is not None:
        return configured
    raise SystemExit(
        "没拿到 WindBot 源码树路径：用 --windbot-src 指定，或设环境变量 MAIBOT_WINDBOT_SRC，"
        "或在插件 config.toml 里填 paths.windbot_src_dir"
    )


def _configured_source() -> Optional[Path]:
    """插件 config.toml 里的 `paths.windbot_src_dir`（相对路径按插件目录解析）。"""

    if not _PLUGIN_ROOT.is_dir():
        return None
    try:
        import tomllib

        data = tomllib.loads((_PLUGIN_ROOT / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = str((data.get("paths") or {}).get("windbot_src_dir") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else (_PLUGIN_ROOT / path)


def _load_manifest() -> Dict[str, str]:
    if not _MANIFEST.is_file():
        return {}
    return json.loads(_MANIFEST.read_text(encoding="utf-8")).get("files", {})


def _compare(source: Path) -> Tuple[List[str], List[str], List[str]]:
    """返回 (缺文件, 留档与源码不一致, 清单与源码不一致)。"""

    manifest = _load_manifest()
    missing: List[str] = []
    stale_copy: List[str] = []
    stale_manifest: List[str] = []
    for rel in FILES:
        src = source / rel
        archived = _ARCHIVE_DIR / Path(rel).name
        if not src.is_file():
            missing.append(rel)
            continue
        src_hash = _sha256(src)
        if archived.is_file() and _sha256(archived) != src_hash:
            stale_copy.append(rel)
        if manifest.get(rel) != src_hash:
            stale_manifest.append(rel)
    return missing, stale_copy, stale_manifest


def _sync(source: Path) -> int:
    manifest: Dict[str, str] = {}
    for rel in FILES:
        src = source / rel
        if not src.is_file():
            print(f"[错误] 源码树里没有 {rel}（{src}）")
            return 2
        shutil.copy2(src, _ARCHIVE_DIR / Path(rel).name)
        manifest[rel] = _sha256(src)
        print(f"  刷新 {Path(rel).name}")
    _MANIFEST.write_text(
        json.dumps({"source": str(source), "files": manifest}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"清单已更新：{_MANIFEST}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WindBot 源码树 ↔ 插件留档目录 同步/校验")
    parser.add_argument("--windbot-src", default="", help="WindBot 源码树根目录")
    parser.add_argument("--sync", action="store_true", help="用源码树刷新留档副本与哈希清单")
    args = parser.parse_args()

    source = Path(args.windbot_src) if args.windbot_src else _default_source()
    if not source.is_dir():
        print(f"[错误] 找不到源码树：{source}（用 --windbot-src 指定）")
        return 2
    if args.sync:
        return _sync(source)

    missing, stale_copy, stale_manifest = _compare(source)
    print(f"源码树：{source}")
    print(f"留档目录：{_ARCHIVE_DIR}")
    if missing:
        print("**源码树里缺文件**：" + "；".join(missing))
    if stale_copy:
        print("**留档副本过期**（与源码树不一致）：" + "；".join(stale_copy))
    if stale_manifest:
        print("**哈希清单与源码树不一致**：" + "；".join(stale_manifest))
    if not (missing or stale_copy or stale_manifest):
        print(f"一致 ✔ {len(FILES)} 个文件（留档 = 源码树 = 清单）")
        return 0
    print("跑 `python tools/check_windbot_sources.py --sync` 刷新留档")
    return 1


if __name__ == "__main__":
    sys.exit(main())
