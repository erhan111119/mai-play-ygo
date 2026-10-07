"""测试入口：跑完 tests/ 下所有测试文件。

每个测试文件都能单独运行（``python tests/test_gate.py``），也都能被 pytest 收集；
这个脚本只是省去逐个敲命令的麻烦。

用了就::

    python tools/run_tests.py
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import subprocess
import sys


# 测试文件所在目录
_TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"


def discover_tests() -> List[Path]:
    """按文件名排序列出所有测试文件。"""

    return sorted(_TESTS_DIR.glob("test_*.py"))


def main() -> int:
    """逐个运行测试文件，汇总结果。"""

    tests = discover_tests()
    if not tests:
        print(f"没有在 {_TESTS_DIR} 找到测试文件")
        return 1

    failed: List[str] = []
    for test_file in tests:
        print(f"\n===== {test_file.name} =====")
        result = subprocess.run(
            [sys.executable, str(test_file)],
            cwd=str(_TESTS_DIR.parent),
            capture_output=False,
        )
        if result.returncode != 0:
            failed.append(test_file.name)

    print("\n" + "=" * 40)
    if failed:
        print(f"{len(tests) - len(failed)}/{len(tests)} 个测试文件通过，失败：{'、'.join(failed)}")
        return 1
    print(f"全部 {len(tests)} 个测试文件通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
