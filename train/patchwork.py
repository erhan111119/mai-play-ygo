"""复盘给的"补丁"：解析 → 校验 → 落盘（**全有或全无**）。

为什么要单独一个模块：这一步会**改源码树里的 C#**，是最容易造成"说不清哪一步坏了"的地方，
所以把它做成纯函数 + 明确的失败原因，测试直接对着它写。

模型输出格式（提示词里强制，解析器只认这一种）::

    <<<PATCH
    文件: KezmoYixiangmingExecutor.cs
    原因: 一句话说清为什么改
    原文:
    <要被替换的片段，必须与文件里**一模一样**、且只出现一次>
    改成:
    <替换后的片段>
    PATCH>>>

不这么写的地方（缺字段、片段对不上、片段出现两次）一律**整批不落地**——
按 AGENTS.md 的纪律：宁可什么都不改并说清原因，也不要留一半改动在文件里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import re
import time

#: 一次复盘最多接受几个补丁（多了说明模型在瞎改，宁可让它分几次）。
MAX_PATCHES = 6

#: 单个补丁的片段长度上限（防止模型把整份文件塞进来"替换"）。
MAX_SNIPPET_CHARS = 8000

_BLOCK = re.compile(r"<<<PATCH\s*(.*?)\s*PATCH>>>", re.S)


@dataclass
class Patch:
    """一个待落地的改动。"""

    file: str
    reason: str
    old: str
    new: str

    def describe(self) -> str:
        """面板/日志里的一行摘要。"""

        head = self.reason.strip().splitlines()[0] if self.reason.strip() else "（没说原因）"
        return f"{self.file}：{head[:80]}"


@dataclass
class PatchSet:
    """解析结果：补丁 + 解析阶段发现的问题。"""

    patches: List[Patch] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """有没有至少一个补丁、且没有解析问题。"""

        return bool(self.patches) and not self.problems


@dataclass
class ApplyResult:
    """落地结果（全有或全无）。"""

    applied: int = 0
    files: List[str] = field(default_factory=list)
    backups: List[str] = field(default_factory=list)
    error: str = ""
    preview: str = ""

    @property
    def ok(self) -> bool:
        """是否真的落地了。"""

        return self.applied > 0 and not self.error


def _field(body: str, label: str) -> str:
    """取 `标签:` 到下一个标签之间的内容（多行）。"""

    pattern = re.compile(
        rf"^\s*{label}\s*:\s*(.*?)(?=^\s*(?:文件|原因|原文|改成)\s*:|\Z)",
        re.S | re.M,
    )
    match = pattern.search(body)
    return match.group(1).strip("\n").rstrip() if match else ""


def parse_patches(answer: str) -> PatchSet:
    """从复盘正文里抠出补丁块。

    Returns:
        PatchSet: 解析到的补丁与问题列表（问题非空时调用方**不要**落地）。
    """

    result = PatchSet()
    raw_blocks = _BLOCK.findall(str(answer or ""))
    if not raw_blocks:
        result.problems.append("没有找到 `<<<PATCH ... PATCH>>>` 块（这次只给了建议，没有给可落地的改动）")
        return result
    if len(raw_blocks) > MAX_PATCHES:
        result.problems.append(f"补丁太多（{len(raw_blocks)} 个，上限 {MAX_PATCHES}）——请分成几次复盘")
        return result
    for index, body in enumerate(raw_blocks, start=1):
        name = _field(body, "文件").strip().strip("`")
        reason = _field(body, "原因").strip()
        old = _field(body, "原文")
        new = _field(body, "改成")
        if not name:
            result.problems.append(f"补丁 {index}：缺 `文件:`")
            continue
        if not old:
            result.problems.append(f"补丁 {index}：缺 `原文:`（没有要替换的片段就无法落地）")
            continue
        if len(old) > MAX_SNIPPET_CHARS or len(new) > MAX_SNIPPET_CHARS:
            result.problems.append(f"补丁 {index}：片段太长（超过 {MAX_SNIPPET_CHARS} 字）")
            continue
        if old.strip() == new.strip():
            result.problems.append(f"补丁 {index}：原文与改成一样，等于没改")
            continue
        result.patches.append(Patch(file=name, reason=reason, old=old, new=new))
    return result


def locate_target(root: Path, name: str) -> Optional[Path]:
    """把模型写的文件名解析成源码树里的真实路径。

    只认"文件名"（`XxxExecutor.cs`）与"相对 Game/AI/Decks 的路径"两种写法，
    并且**必须落在 root 之下**——不给"顺手改到别处"的机会。
    """

    cleaned = str(name or "").strip().strip("`").replace("\\", "/")
    if not cleaned:
        return None
    candidate = (root / cleaned).resolve() if cleaned.startswith("Game/") else (root / "Game" / "AI" / "Decks" / cleaned).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def check(patch_set: PatchSet, root: Path) -> Tuple[List[Tuple[Path, Patch]], List[str]]:
    """逐个校验"文件存在 + 原文恰好出现一次"。

    Returns:
        tuple: ``(可直接落地的 (路径, 补丁) 列表, 问题列表)``；问题非空＝整批不落地。
    """

    problems: List[str] = []
    ready: List[Tuple[Path, Patch]] = []
    for index, patch in enumerate(patch_set.patches, start=1):
        path = locate_target(root, patch.file)
        if path is None:
            problems.append(f"补丁 {index}：找不到文件 `{patch.file}`（只认 Game/AI/Decks 下的 .cs）")
            continue
        text = path.read_text(encoding="utf-8-sig")
        count = text.count(patch.old)
        if count == 0:
            problems.append(f"补丁 {index}：`原文` 在 {path.name} 里一次都没出现（模型没抄准，或这处已经改过）")
            continue
        if count > 1:
            problems.append(f"补丁 {index}：`原文` 在 {path.name} 里出现 {count} 次，改哪一处不明确")
            continue
        ready.append((path, patch))
    return ready, problems


def _read_text(path: Path) -> Tuple[str, bool]:
    """读源码文本，并记下**这个文件原来有没有 BOM**。

    ⚠ 不能直接 `write_text(encoding="utf-8")` 写回：那样会把 BOM 吃掉——实测落地一次之后
    「留档副本 vs 源码树」的哈希就对不上了，而那个对照正是判断"部署的是哪一版"的依据；
    顺手还可能把 CRLF 改成 LF。所以读写都按字节来，只替换补丁那一段。
    """

    raw = path.read_bytes()
    return raw.decode("utf-8-sig"), raw.startswith(b"\xef\xbb\xbf")


def _write_text(path: Path, text: str, had_bom: bool) -> None:
    """按原来的编码写回（BOM 与换行都不动）。"""

    path.write_bytes((b"\xef\xbb\xbf" if had_bom else b"") + text.encode("utf-8"))


def apply_patches(patch_set: PatchSet, root: Path) -> ApplyResult:
    """落地（全有或全无）：先校验全部，再备份 + 写入。

    Raises:
        无——失败都写进 :class:`ApplyResult`，调用方按它如实汇报。
    """

    result = ApplyResult()
    if not patch_set.patches:
        result.error = "没有可落地的补丁"
        return result
    ready, problems = check(patch_set, root)
    if problems:
        result.error = "；".join(problems)
        return result

    # 同一份文件可能被改多处：先按文件把替换串起来，避免"后一个补丁看不到前一个的结果"。
    by_file: dict = {}
    for path, patch in ready:
        by_file.setdefault(path, []).append(patch)
    changed: dict = {}
    boms: dict = {}
    for path, patches in by_file.items():
        text, had_bom = _read_text(path)
        boms[path] = had_bom
        for patch in patches:
            if text.count(patch.old) != 1:
                result.error = f"应用期间 {path.name} 的 `原文` 匹配数变了（同一文件里有互相重叠的补丁）"
                return result
            text = text.replace(patch.old, patch.new, 1)
        changed[path] = text

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path, text in changed.items():
        backup = path.with_suffix(path.suffix + f".bak-{stamp}")
        # 备份也逐字节照抄：还原时要能回到**一模一样**的那份
        backup.write_bytes(path.read_bytes())
        _write_text(path, text, boms[path])
        result.files.append(str(path))
        result.backups.append(str(backup))
        result.preview += f"=== {path.name} ===\n" + "\n".join(
            f"- {patch.describe()}" for patch in by_file[path]
        ) + "\n"
    result.applied = len(ready)
    return result


def restore_backups(backups: List[str]) -> str:
    """把备份还原回去（编译不过时用），返回一句人话说明。

    ⚠ 只在"补丁已写入、但编译失败"时调用：让源码树回到改动前，而不是停在编译不过的状态
    （那会让下一局连 exe 都编不出来，比"没改"更糟）。
    """

    done: List[str] = []
    for raw in backups:
        path = Path(raw)
        if not path.is_file():
            continue
        target = Path(str(path).split(".bak-")[0])
        try:
            # 逐字节照抄：BOM 与换行都要跟改前一模一样（否则"还原了"其实还是变了）
            target.write_bytes(path.read_bytes())
        except OSError as exc:  # noqa: PERF203  逐个报，不因为一个失败就放弃其余
            done.append(f"{target.name} 还原失败（{exc}）")
            continue
        done.append(target.name)
    return "、".join(done) if done else "没有可还原的备份"
