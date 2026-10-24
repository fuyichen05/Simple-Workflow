"""运行目录隔离与产物落盘。

核心约定：一次运行 = 一个唯一目录 runs/<run_id>/，创建时使用
os.makedirs(exist_ok=False)，从文件系统层面保证不同运行绝不互相覆盖。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import console

# ---------------------------------------------------------------- 常量与限制

FILE_START_RE = re.compile(
    r"^[ \t]*(?:<<<|===)\s*FILE:\s*(?P<path>[^>\r\n=]+?)\s*(?:>>>|===)[ \t]*$",
    re.MULTILINE,
)
# 备用格式：### FILE: path
FILE_HEADING_RE = re.compile(
    r"^[ \t]*#{2,4}[ \t]*FILE:[ \t]*(?P<path>[^\r\n]+?)[ \t]*$",
    re.MULTILINE,
)
FILE_END_RE = re.compile(r"^[ \t]*(?:<<<|===)\s*END(?:\s+FILE)?\s*(?:>>>|===)[ \t]*$", re.MULTILINE)

MAX_FILES = 80
MAX_FILE_BYTES = 512 * 1024

_RUN_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ArtifactError(Exception):
    """产物写入或解析失败。"""


# -------------------------------------------------------------------- 运行目录


@dataclass
class RunContext:
    """单次运行的目录上下文。"""

    run_id: str
    run_dir: Path
    logger: "RunLogger | None" = None

    # -- 路径

    def path(self, *parts: str) -> Path:
        return self.run_dir.joinpath(*parts)

    # -- 写入

    def write_text(self, rel_path: str, text: str) -> int:
        """写入 UTF-8 文本，返回字节数。"""
        target = self._resolve(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        return len(data)

    def write_json(self, rel_path: str, payload: object) -> int:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return self.write_text(rel_path, text)

    def read_json(self, rel_path: str, default=None):
        target = self.path(rel_path)
        if not target.is_file():
            return default
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def append_log(self, text: str) -> None:
        target = self.path("run.log")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    # -- 内部

    def _resolve(self, rel_path: str) -> Path:
        """解析相对路径，阻止目录穿越。"""
        safe = sanitize_relative_path(rel_path)
        target = (self.run_dir / safe).resolve()
        run_dir = self.run_dir.resolve()
        if run_dir != target and run_dir not in target.parents:
            raise ArtifactError(f"路径越界，拒绝写入: {rel_path}")
        return target


def create_run(runs_root: Path, requested_id: str | None = None) -> RunContext:
    """创建一个全新的运行目录，已存在则报错（绝不覆盖）。"""
    runs_root = Path(runs_root).expanduser()
    runs_root.mkdir(parents=True, exist_ok=True)

    if requested_id:
        rid = requested_id.strip()
        if not _RUN_ID_SAFE_RE.match(rid) or ".." in rid:
            raise ArtifactError(
                f"非法的 run-id: {rid!r}，只允许字母、数字、点、下划线、连字符，长度 1-64"
            )
        target = runs_root / rid
        try:
            os.makedirs(target, exist_ok=False)
        except FileExistsError as exc:
            raise ArtifactError(f"运行目录已存在，拒绝覆盖: {target}") from exc
        return RunContext(run_id=rid, run_dir=target)

    for _ in range(16):
        rid = new_run_id()
        target = runs_root / rid
        try:
            os.makedirs(target, exist_ok=False)
        except FileExistsError:
            continue
        return RunContext(run_id=rid, run_dir=target)
    raise ArtifactError("连续 16 次生成 run-id 均冲突，请检查系统时间是否正常")


def new_run_id() -> str:
    """run-id 形如 20261024-001530-9f3a（时间戳 + 4 位随机后缀）。"""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def utc_now_iso() -> str:
    """本地时区的 ISO8601 时间戳（带偏移）。"""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------- 日志


@dataclass
class RunLogger:
    """同时写终端与 run.log 的日志器。并行分支会并发调用，因此写入加锁。"""

    run: RunContext
    quiet: bool = False
    _lock: "threading.Lock" = field(default_factory=threading.Lock, repr=False)

    def _write(self, level: str, message: str) -> None:
        try:
            stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                self.run.append_log(f"{stamp} " + console.line(level, message))
        except OSError:
            pass

    def info(self, message: str) -> None:
        self._write("info", message)
        if not self.quiet:
            console.emit("info", message)

    def ok(self, message: str) -> None:
        self._write("ok", message)
        if not self.quiet:
            console.emit("ok", message)

    def warn(self, message: str) -> None:
        self._write("warn", message)
        if not self.quiet:
            console.emit("warn", message)

    def error(self, message: str) -> None:
        self._write("error", message)
        if not self.quiet:
            console.emit("error", message)

    def step(self, message: str) -> None:
        self._write("step", message)
        if not self.quiet:
            console.emit("step", message)

    def detail(self, message: str) -> None:
        """只写文件，不刷屏。"""
        self._write("info", message)

    def block(self, message: str) -> None:
        """多行内容：整块写入日志，非 quiet 时也打印到终端。"""
        try:
            stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                self.run.append_log(f"{stamp} [INFO] {message}")
        except OSError:
            pass
        if not self.quiet:
            print(console.paint(message, "dim"), flush=True)


# ------------------------------------------------------------------ 路径安全


def sanitize_relative_path(raw: str) -> str:
    """把模型给出的路径规整为安全的相对路径。

    拒绝绝对路径、盘符、.. 上跳、空字节、Windows 保留设备名。
    """
    if not raw:
        raise ArtifactError("文件路径为空")

    path = raw.strip().strip("`\"'").strip()
    path = path.replace("\\", "/")
    if "\x00" in path:
        raise ArtifactError("文件路径含空字节")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ArtifactError(f"不允许绝对路径: {raw}")
    if path.startswith("~"):
        raise ArtifactError(f"不允许用户目录路径: {raw}")

    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts:
        raise ArtifactError(f"文件路径无有效内容: {raw}")
    if any(p == ".." for p in parts):
        raise ArtifactError(f"不允许目录穿越: {raw}")

    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    for part in parts:
        stem = part.split(".")[0].upper()
        if stem in reserved:
            raise ArtifactError(f"路径含系统保留名: {raw}")
        if part != part.strip():
            raise ArtifactError(f"路径含首尾空格: {raw}")

    return "/".join(parts)


# ------------------------------------------------------------------ 代码解析


@dataclass
class ParsedFile:
    path: str
    content: str

    @property
    def size(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass
class ParseReport:
    files: list[ParsedFile] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def parse_code_files(text: str) -> ParseReport:
    """从模型输出中提取 <<<FILE: path>>> ... <<<END>>> 块。

    同时兼容 ```lang 围栏包裹、以及 ### FILE: path 标题形式。
    单个非法文件被跳过并计入 rejected，不影响其它文件。
    """
    report = ParseReport()

    matches = list(FILE_START_RE.finditer(text))
    end_re = FILE_END_RE
    if not matches:
        # 退化为 Markdown 标题形式，此时用下一个标题当边界
        matches = list(FILE_HEADING_RE.finditer(text))
        end_re = None

    if not matches:
        return report

    for index, match in enumerate(matches):
        raw_path = match.group("path")
        start = match.end()
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:stop]

        if end_re is not None:
            end_match = end_re.search(body)
            if end_match:
                body = body[: end_match.start()]

        body = _strip_fence(body)

        try:
            safe_path = sanitize_relative_path(raw_path)
        except ArtifactError as exc:
            report.rejected.append((raw_path.strip(), str(exc)))
            continue

        if not body.strip():
            # __init__.py 本来就常为空，属于合法产物；其它文件为空则视为生成失败
            if safe_path.rsplit("/", 1)[-1] == "__init__.py":
                report.files.append(ParsedFile(path=safe_path, content=""))
                continue
            report.rejected.append((safe_path, "文件内容为空"))
            continue

        if len(body.encode("utf-8")) > MAX_FILE_BYTES:
            report.rejected.append((safe_path, f"文件超过 {MAX_FILE_BYTES // 1024}KB 上限"))
            continue

        if len(report.files) >= MAX_FILES:
            report.truncated = True
            break

        # 同名文件保留第一个，后续覆盖会产生歧义
        if safe_path in report.paths:
            report.rejected.append((safe_path, "重复定义，已忽略后一份"))
            continue

        report.files.append(ParsedFile(path=safe_path, content=body))

    return report


def _strip_fence(body: str) -> str:
    """去掉内容首尾的 Markdown 代码围栏。"""
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[0].lstrip().startswith("```"):
        lines.pop(0)
    if lines and lines[-1].strip() == "```":
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 目录树


def format_tree(paths: list[str]) -> str:
    """把文件路径列表渲染成缩进目录树，用于终端展示。"""
    if not paths:
        return "(无文件)"
    lines: list[str] = []
    seen_dirs: set[str] = set()
    for path in sorted(paths):
        parts = path.split("/")
        for depth in range(len(parts) - 1):
            prefix = "/".join(parts[: depth + 1])
            if prefix in seen_dirs:
                continue
            seen_dirs.add(prefix)
            lines.append("  " * depth + parts[depth] + "/")
        lines.append("  " * (len(parts) - 1) + parts[-1])
    return "\n".join(lines)
