"""本地工具：不消耗 token 的确定性检查。

Agent 负责"判断"，工具负责"事实"。把这些检查放在 LLM 之外有三个好处：
1. 结果确定，不受模型波动影响；
2. 免费、秒级；
3. 审查报告里的事实依据可追溯。

包含：
    static_check      语法检查（ast.parse）
    coverage_check    设计文件清单 vs 实际交付的覆盖率
    build_code_context 把代码拼成审查上下文（带预算截断）
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

MAX_TOTAL_CHARS = 30_000
MAX_FILE_CHARS = 6_000


@dataclass
class StaticResult:
    """静态检查结果。"""

    checked: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    expected: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing

    def as_report(self) -> str:
        """渲染成给审查 Agent 看的事实清单。"""
        lines = [
            f"- 语法检查：{self.checked} 个 Python 文件，"
            + ("全部通过" if not self.errors else f"{len(self.errors)} 个存在错误"),
            f"- 文件清单：设计期望 {self.expected} 个，实际交付 {self.checked + len(self.extra)} 个",
        ]
        if self.missing:
            lines.append(f"- 缺失文件：{', '.join(self.missing)}")
        if self.extra:
            lines.append(f"- 清单外文件：{', '.join(self.extra)}")
        for err in self.errors:
            lines.append(f"- 语法错误 {err['file']}:{err['line']} — {err['msg']}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "expected": self.expected,
            "errors": self.errors,
            "missing": self.missing,
            "extra": self.extra,
        }


def static_check(base_dir: Path, paths: list[str], expected: list[str] | None = None) -> StaticResult:
    """对生成的 Python 文件做语法检查，并核对文件清单覆盖率。"""
    result = StaticResult()
    expected = expected or []
    result.expected = len(expected)

    actual_set = set(paths)
    for path in expected:
        if path not in actual_set:
            result.missing.append(path)

    expected_set = set(expected)
    for path in paths:
        if expected and path not in expected_set and not path.startswith("tests/"):
            result.extra.append(path)

    for rel in paths:
        if not rel.endswith(".py"):
            continue
        target = base_dir / rel
        try:
            source = target.read_text(encoding="utf-8")
        except OSError as exc:
            result.errors.append({"file": rel, "line": "0", "msg": f"无法读取: {exc}"})
            continue
        result.checked += 1
        try:
            ast.parse(source, filename=rel)
        except SyntaxError as exc:
            result.errors.append(
                {"file": rel, "line": str(exc.lineno or 0), "msg": str(exc.msg or "语法错误")}
            )
        except ValueError as exc:  # 例如源码含空字节
            result.errors.append({"file": rel, "line": "0", "msg": str(exc)})

    return result


def build_code_context(
    base_dir: Path,
    paths: list[str],
    *,
    total_budget: int = MAX_TOTAL_CHARS,
    per_file: int = MAX_FILE_CHARS,
) -> tuple[str, list[str]]:
    """把代码文件拼成审查上下文，超预算时截断。

    返回 (文本, 被截断的文件列表)。截断信息会一并写进上下文，
    让审查 Agent 知道自己看到的是不完整内容，避免误判。
    """
    blocks: list[str] = []
    truncated: list[str] = []
    used = 0

    for rel in paths:
        if used >= total_budget:
            truncated.append(rel)
            continue
        target = base_dir / rel
        try:
            source = target.read_text(encoding="utf-8")
        except OSError:
            blocks.append(f"【文件: {rel}】\n(无法读取)")
            continue

        remaining = min(per_file, total_budget - used)
        body = source
        if len(body) > remaining:
            body = body[:remaining]
            truncated.append(rel)
            note = f"\n... (已截断，原文 {len(source)} 字符，此处仅显示前 {remaining} 字符)"
        else:
            note = ""

        used += len(body)
        lang = _lang_of(rel)
        blocks.append(f"【文件: {rel}】\n```{lang}\n{body}{note}\n```")

    text = "\n\n".join(blocks) if blocks else "(没有可审查的文件)"
    if truncated:
        text += "\n\n> 注意：以下文件内容被截断，审查时不要据此判断缺文件：" + ", ".join(truncated)
    return text, truncated


def _lang_of(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".py"):
        return "python"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".md"):
        return "markdown"
    if lower.endswith((".yaml", ".yml")):
        return "yaml"
    return ""
