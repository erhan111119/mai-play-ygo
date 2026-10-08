"""在面板里改配置：把改动写回 `config.toml`，**保住原有注释与排版**。

为什么不能整份重写：`config.toml` 里每一行都带中文注释（那是给部署者看的说明书），
用 TOML 库 dump 一遍会把注释全丢掉——用户改一次配置就毁掉一半文档，这不能接受。

所以这里走**行级改写**：只在原文件里替换那一行的值，行尾注释原样留着；
键不存在就在对应配置节的末尾插一行。改动先经过配置模型校验（`validate_assignment`），
**校验不过就一个字都不写**，绝不会出现"文件写坏了、插件起不来"的情况。

写完之后不需要通知谁：宿主用 watchfiles 盯着这份文件，落盘即热更新
（与"在麦麦 WebUI 里保存"走的是同一个入口）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import json
import re

#: 配置节标题，例如 `[duel]`。
_SECTION_PATTERN = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")

#: 值里不允许出现换行：一行一个键是这份文件的排版约定，插进多行会把 TOML 写坏。
_FORBIDDEN_IN_STRING = ("\n", "\r", "\x00")


class ConfigEditError(RuntimeError):
    """改动不合法（值校验不过 / 键不存在 / 字符串里有换行）。"""


def render_value(value: Any) -> str:
    """把 Python 值渲染成 TOML 字面量（只支持配置里实际用到的几种类型）。

    Raises:
        ConfigEditError: 类型不认识，或字符串里带了换行（会破坏"一行一个键"的排版）。
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        if any(mark in value for mark in _FORBIDDEN_IN_STRING):
            raise ConfigEditError("这一项不能包含换行")
        # json.dumps 的字符串字面量与 TOML 基本字符串兼容（转义规则一致）
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    raise ConfigEditError(f"不支持的类型：{type(value).__name__}")


def _split_comment(line: str) -> Tuple[str, str]:
    """把一行拆成 ``(代码, 行尾注释)``。

    引号里的 `#` 不算注释起点（卡组名、提示词里都可能出现 `#`）。
    """

    quote: Optional[str] = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == "\\":
                continue
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            return line[:index], line[index:]
    return line, ""


def _line_key(code: str) -> Optional[str]:
    """一行是不是 ``键 = 值``；是的话返回键名。"""

    if "=" not in code:
        return None
    name = code.split("=", 1)[0].strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return None
    return name


def apply_values(
    config_path: Path,
    payload: Dict[str, Dict[str, Any]],
    model: Any,
) -> List[str]:
    """把 ``{节: {键: 值}}`` 写进配置文件，返回真正改动过的条目（``"节.键=新值"``）。

    两条硬约束：

    * **先校验后落盘**：每个值都赋到配置模型上（pydantic 会校验类型与取值范围），
      任何一项不过就整批放弃——半批写进去比不写更糟，用户看到的是"改了一半"。
    * **只碰涉及的行**：没提到的键、注释、空行、节顺序全部原样保留。

    Raises:
        ConfigEditError: 节/键不认识、值校验不过、类型渲染不了。
    """

    sections = getattr(type(model), "model_fields", None)
    if not sections:
        raise ConfigEditError("配置模型不可用，无法校验改动")
    if not payload:
        raise ConfigEditError("没有要改的内容")

    # 1) 校验：在内存里的配置副本上赋值（`validate_assignment=True` 会当场报错）
    draft = model.model_copy(deep=True)
    for section_name, values in payload.items():
        if section_name not in sections:
            raise ConfigEditError(f"没有 [{section_name}] 这个配置节")
        if not isinstance(values, dict):
            raise ConfigEditError(f"[{section_name}] 的改动应该是一组键值")
        section = getattr(draft, section_name)
        field_names = getattr(type(section), "model_fields", {})
        for key, value in values.items():
            if key not in field_names:
                raise ConfigEditError(f"[{section_name}] 里没有 {key} 这一项")
            try:
                setattr(section, key, value)
            except Exception as exc:  # noqa: BLE001  把 pydantic 的校验信息原样带出去
                raise ConfigEditError(f"{section_name}.{key} 的值不合法：{exc}") from exc

    # 2) 渲染每一行（渲染不了的（带换行的字符串等）在这一步就会失败，仍然不落盘）
    rendered: Dict[str, Dict[str, str]] = {}
    for section_name, values in payload.items():
        section = getattr(draft, section_name)
        rendered[section_name] = {
            key: render_value(getattr(section, key)) for key in values
        }

    # 3) 行级改写
    try:
        original = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigEditError(f"读不了配置文件 {config_path}：{exc}") from exc
    lines = original.splitlines()
    changed: List[str] = []
    for section_name, values in rendered.items():
        for key, text in values.items():
            lines, touched = _replace_or_insert(lines, section_name, key, text)
            if touched:
                changed.append(f"{section_name}.{key}={text}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _replace_or_insert(
    lines: List[str], section: str, key: str, text: str
) -> Tuple[List[str], bool]:
    """在 ``[section]`` 里替换 ``key`` 那一行；没有就插到该节末尾。

    返回 ``(新的行列表, 是否真的动了)``。
    """

    current = ""
    last_of_section = -1          # 该节最后一行非空行的下标
    for index, line in enumerate(lines):
        match = _SECTION_PATTERN.match(line)
        if match:
            current = match.group(1).strip()
            continue
        if current != section:
            continue
        if line.strip():
            last_of_section = index
        code, comment = _split_comment(line)
        if _line_key(code) == key:
            # 找到了：只换值，行尾注释与缩进照旧
            if comment:
                comment = "  " + comment.lstrip() if not comment.startswith("  ") else comment
            lines[index] = f"{key} = {text}{comment}"
            return lines, True

    if last_of_section >= 0:
        lines.insert(last_of_section + 1, f"{key} = {text}")
        return lines, True
    # 连这个节都还没有：自己写一个（放在文件末尾），保证 TOML 仍然合法
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"[{section}]")
    lines.append(f"{key} = {text}")
    return lines, True


def missing_keys(config_path: Path, sections: Sequence[str], model: Any) -> Dict[str, List[str]]:
    """哪些配置项还没写进这份文件（面板据此把"没写过的项"标出来）。

    为什么要这个：配置文件里的键可以缺省（缺省＝用代码里的默认值），但用户看面板时
    分不清"这一项是默认值"还是"我配过"。
    """

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    present: Dict[str, set] = {}
    current = ""
    for line in text.splitlines():
        match = _SECTION_PATTERN.match(line)
        if match:
            current = match.group(1).strip()
            present.setdefault(current, set())
            continue
        name = _line_key(_split_comment(line)[0])
        if name and current:
            present.setdefault(current, set()).add(name)

    result: Dict[str, List[str]] = {}
    for section in sections:
        field_names = getattr(getattr(model, section, None), "model_fields", {})
        have = present.get(section, set())
        result[section] = sorted(name for name in field_names if name not in have)
    return result
