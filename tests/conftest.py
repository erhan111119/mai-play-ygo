"""测试环境准备：把宿主（含 SDK）与插件根目录放进 `sys.path`，并按依赖可用性跳过。

两条现实约束（都是实测踩出来的）：

1. **有些用例要加载插件本体**（`plugin.py`），而它 `import maibot_sdk` —— 那是宿主提供的 SDK；
   用例自己是没法 `pip install` 的，所以这里照宿主的目录布局把路径补上
   （`<MaiBot>/python-overrides`、`<MaiBot>/python-env/Lib/site-packages`）。
2. **插件被单独拷到别处跑时**（例如只把插件目录复制到 `D:\\mai-play-ygo`），宿主根本不在旁边，
   上面这些路径都不存在——这时不该报"收集失败"，而是**明确跳过**那些必须宿主的用例：
   否则 `pytest` 一上来就 `IndexError`/`ModuleNotFoundError`，看起来像插件坏了。
"""

from __future__ import annotations

from pathlib import Path
import importlib.util
import os
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_HOST_ROOT = _PLUGIN_ROOT.parent.parent
_INSTALL_ROOT = _HOST_ROOT.parent.parent

for _extra in (
    _HOST_ROOT,
    _INSTALL_ROOT / "python-overrides",
    _INSTALL_ROOT / "python-env" / "Lib" / "site-packages",
):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

#: 需要宿主 SDK 的用例文件：单独拷贝出去跑时跳过它们，其余用例（纯逻辑）照跑
_SDK_ONLY = ("test_manifest.py", "test_plugin_lifecycle.py")

collect_ignore: list[str] = (
    [] if importlib.util.find_spec("maibot_sdk") is not None else list(_SDK_ONLY)
)

# 测试里**不要联网补卡图**：`duel/recorder.py` 会在卡出现在场上时把整卡图排进后台线程
# （自动预热，见 `duel/card_images.py`），而用例里用的多是假卡号——联网只会拖慢测试、
# 顺便往 `temp/card_pics/` 塞垃圾文件。关掉之后只读缓存（出图退化成兜底卡面）。
os.environ.setdefault("MAIPLAYYGO_NO_PIC_FETCH", "1")
