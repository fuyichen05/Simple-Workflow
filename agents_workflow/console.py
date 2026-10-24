"""终端输出：ANSI 着色 + 结构化日志行。

在 Windows 下自动启用 VT 序列；不支持时自动退化为无色输出。
设置环境变量 NO_COLOR=1 可强制无色。
"""

from __future__ import annotations

import os
import sys

_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "blue": "\033[34m",
}


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        # Windows 10+ 控制台默认支持 VT，尝试打开标记
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


_COLOR_ENABLED = _supports_color()

# 各种日志级别 -> (标签, 颜色)
_LEVELS = {
    "info": ("INFO", "cyan"),
    "ok": (" OK ", "green"),
    "warn": ("WARN", "yellow"),
    "error": ("FAIL", "red"),
    "step": ("STEP", "magenta"),
    "tip": ("TIP ", "blue"),
}


def paint(text: str, color: str) -> str:
    """给文本上色；不支持颜色时原样返回。"""
    if not _COLOR_ENABLED:
        return text
    code = _COLORS.get(color)
    if not code:
        return text
    return f"{code}{text}{_COLORS['reset']}"


def configure_stdout() -> None:
    """统一 stdout/stderr 为 UTF-8，避免 Windows 下中文乱码。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def line(level: str, message: str) -> str:
    """构造一行带级别标签的日志文本（无色，用于写文件）。"""
    tag, _ = _LEVELS.get(level, ("INFO", "cyan"))
    return f"[{tag}] {message}"


def emit(level: str, message: str, *, stream=None) -> None:
    """打印一行彩色日志。"""
    tag, color = _LEVELS.get(level, ("INFO", "cyan"))
    target = stream or sys.stdout
    print(f"{paint('[' + tag + ']', color)} {message}", file=target, flush=True)


def banner(title: str, subtitle: str = "", width: int = 68) -> None:
    """打印启动横幅。"""
    bar = "=" * width
    print(paint(bar, "dim"))
    print(paint(title, "bold"))
    if subtitle:
        print(paint(subtitle, "dim"))
    print(paint(bar, "dim"))


def rule(width: int = 68) -> None:
    print(paint("-" * width, "dim"))
