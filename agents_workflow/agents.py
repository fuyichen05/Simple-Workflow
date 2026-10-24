"""五个 Agent 的实现。

统一契约：Agent 只负责"给定上下文 -> 产出文本"，
不读磁盘、不写状态、不感知流程走向。
上下文由引擎从共享状态与消息中取好传进来，产物由引擎负责落盘与广播。
因此每个 Agent 都可以单独测试或替换。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from . import prompts
from .llm import LLMResponse, Usage


class ChatClient(Protocol):
    """Agent 依赖的最小客户端接口，真实实现与 Mock 都满足它。"""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tag: str = "call",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


@dataclass
class AgentResult:
    """单个 Agent 的执行结果。"""

    stage: str
    title: str
    output: str
    output_name: str
    elapsed: float = 0.0
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1
    finish_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.output)

    def as_meta(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "title": self.title,
            "output": self.output_name,
            "status": "ok",
            "elapsed_sec": round(self.elapsed, 2),
            "chars": self.chars,
            "usage": self.usage.as_dict(),
            "attempts": self.attempts,
            "finish_reason": self.finish_reason,
            "warnings": self.warnings,
        }


class BaseAgent:
    """Agent 基类：负责计时、用量统计与截断检测。"""

    stage: str = "agent"
    title: str = "Agent"
    output_name: str = "output.md"
    temperature: float | None = None

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    # 子类实现：构造 messages
    def build_messages(self, *args, **kwargs) -> list[dict[str, str]]:
        raise NotImplementedError

    def run(self, *args, **kwargs) -> AgentResult:
        messages = self.build_messages(*args, **kwargs)
        started = time.monotonic()
        response = self.client.chat(
            messages,
            tag=self.stage,
            temperature=self.temperature,
        )
        elapsed = time.monotonic() - started

        warnings: list[str] = []
        if response.truncated:
            warnings.append(
                "输出因 max_tokens 上限被截断，产物可能不完整；"
                "可调大 DEEPSEEK_MAX_TOKENS 后重跑"
            )

        return AgentResult(
            stage=self.stage,
            title=self.title,
            output=response.text,
            output_name=self.output_name,
            elapsed=elapsed,
            usage=response.usage,
            attempts=response.attempts,
            finish_reason=response.finish_reason,
            warnings=warnings,
        )


class RequirementAgent(BaseAgent):
    """阶段 1：把原始需求整理成结构化规格说明书。"""

    stage = "requirement"
    title = "需求分析 Agent"
    output_name = "01_requirement.md"
    temperature = 0.3

    def build_messages(self, requirement: str) -> list[dict[str, str]]:
        return prompts.build_requirement_messages(requirement)


class DesignAgent(BaseAgent):
    """阶段 2：基于需求规格输出技术设计说明书。"""

    stage = "design"
    title = "架构设计 Agent"
    output_name = "02_design.md"
    temperature = 0.25

    def build_messages(self, requirement_doc: str) -> list[dict[str, str]]:
        return prompts.build_design_messages(requirement_doc)


class CodeAgent(BaseAgent):
    """阶段 3：基于需求与设计生成完整代码文件。

    文件较多时会自动分批生成：单次调用的 max_tokens 装不下十几个文件，
    硬塞的结果就是尾部文件被截断。分批后每批只负责一小部分文件，
    合并后仍是完整项目。
    """

    stage = "code"
    title = "编码实现 Agent"
    output_name = "03_code"
    temperature = 0.15
    # 每批最多生成的文件数；<= 该值时不分批
    files_per_batch = 4

    def build_messages(
        self,
        requirement: str,
        requirement_doc: str,
        design_doc: str,
        feedback: str = "",
    ) -> list[dict[str, str]]:
        expected = count_expected_files(design_doc)
        return prompts.build_code_messages(
            requirement=requirement,
            requirement_doc=requirement_doc,
            design_doc=design_doc,
            expected_files=expected,
            feedback=feedback,
        )

    def run(
        self,
        requirement: str,
        requirement_doc: str,
        design_doc: str,
        feedback: str = "",
    ) -> AgentResult:
        files = extract_expected_files(design_doc)
        batches = self.plan_batches(files)
        if len(batches) <= 1:
            return super().run(requirement, requirement_doc, design_doc, feedback)
        return self._run_batched(requirement, requirement_doc, design_doc, feedback, batches)

    def plan_batches(self, files: list[str]) -> list[list[str]]:
        """按文件数切批；清单解析不出来时退回单批（交给模型自己决定）。"""
        if not files or len(files) <= self.files_per_batch:
            return [files]
        return [
            files[i: i + self.files_per_batch]
            for i in range(0, len(files), self.files_per_batch)
        ]

    def _run_batched(
        self,
        requirement: str,
        requirement_doc: str,
        design_doc: str,
        feedback: str,
        batches: list[list[str]],
    ) -> AgentResult:
        started = time.monotonic()
        parts: list[str] = []
        warnings: list[str] = []
        usage = Usage()
        attempts = 0
        total = len(batches)

        for index, batch in enumerate(batches, 1):
            messages = prompts.build_code_messages(
                requirement=requirement,
                requirement_doc=requirement_doc,
                design_doc=design_doc,
                expected_files=len(batch),
                feedback=feedback,
                batch=batch,
                batch_index=index,
                batch_total=total,
            )
            response = self.client.chat(
                messages,
                tag=f"code_b{index}",
                temperature=self.temperature,
            )
            usage = usage + response.usage
            attempts += response.attempts
            if response.truncated:
                warnings.append(
                    f"第 {index}/{total} 批输出被截断（{', '.join(batch)}），"
                    "该批尾部文件可能不完整"
                )
            parts.append(response.text)

        elapsed = time.monotonic() - started
        return AgentResult(
            stage=self.stage,
            title=f"{self.title}（{total} 批）",
            output="\n\n".join(parts),
            output_name=self.output_name,
            elapsed=elapsed,
            usage=usage,
            attempts=attempts,
            finish_reason="stop" if not warnings else "length",
            warnings=warnings,
        )


class TestAgent(BaseAgent):
    """阶段 4：与编码并行，只依据设计产出黑盒测试。"""

    stage = "test"
    title = "测试生成 Agent"
    output_name = "03_code/tests"
    temperature = 0.2

    def build_messages(
        self,
        requirement_doc: str,
        design_doc: str,
        feedback: str = "",
    ) -> list[dict[str, str]]:
        return prompts.build_test_messages(requirement_doc, design_doc, feedback)


class ReviewAgent(BaseAgent):
    """阶段 5：质量门禁。结论为 PASS 才放行，FAIL 触发返工循环。"""

    stage = "review"
    title = "代码审查 Agent"
    output_name = "04_review.md"
    temperature = 0.1

    def build_messages(
        self,
        requirement: str,
        requirement_doc: str,
        design_doc: str,
        static_report: str,
        code_block: str,
        previous_issues: str = "",
    ) -> list[dict[str, str]]:
        return prompts.build_review_messages(
            requirement=requirement,
            requirement_doc=requirement_doc,
            design_doc=design_doc,
            static_report=static_report,
            code_block=code_block,
            previous_issues=previous_issues,
        )


# ------------------------------------------------------------- 文件数量估算

_TABLE_ROW_RE = re.compile(
    r"^\|[\s`*]*([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9]{1,10})[\s`*]*\|",
    re.MULTILINE,
)


def count_expected_files(design_doc: str) -> int | None:
    """从设计文档的表格里估算应交付的文件数，用于给编码 Agent 一个明确目标。

    只做尽力而为的估算：解析失败返回 None，不阻断流程。
    """
    paths = set(extract_expected_files(design_doc))
    if not paths:
        return None
    return min(len(paths), 40)


def extract_expected_files(design_doc: str) -> list[str]:
    """从设计文档第 11 节的文件清单表格里提取应交付路径。"""
    found: list[str] = []
    for match in _TABLE_ROW_RE.finditer(design_doc):
        path = match.group(1).replace("\\", "/").lstrip("./").strip()
        if path.lower().startswith(("http", "www")):
            continue
        if path not in found:
            found.append(path)
    return found


# ------------------------------------------------------------- 审查结论解析


@dataclass
class ReviewVerdict:
    """审查 Agent 输出的结构化结论。"""

    passed: bool
    p0: int = 0
    p1: int = 0
    p2: int = 0
    issues: list[str] = field(default_factory=list)
    conclusion: str = ""

    def as_summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        counts = f"P0={self.p0} P1={self.p1} P2={self.p2}"
        return f"{verdict} ({counts})"

    def as_feedback(self) -> str:
        """转成给编码 Agent 的返工清单。"""
        if not self.issues:
            return "上一轮审查未通过，但没有给出具体问题，请整体自查需求覆盖与可运行性。"
        return "\n".join(f"- {item}" for item in self.issues)


_CONCLUSION_RE = re.compile(
    r"审查结论[^\n]*\n{1,4}[^\w\n]{0,6}\**\s*(PASS|FAIL)\b", re.IGNORECASE
)
_FALLBACK_RE = re.compile(r"\b(PASS|FAIL)\b")
_ISSUE_ROW_RE = re.compile(
    r"^\|\s*(\d{1,2})\s*\|\s*(P[0-2])\s*\|\s*([^|\n]*?)\s*\|\s*([^|\n]*?)\s*\|\s*([^|\n]*?)\s*\|",
    re.MULTILINE,
)


def parse_review(text: str) -> ReviewVerdict:
    """解析审查报告：结论 + 问题清单。

    优先级：显式「审查结论」段 > 全文首个 PASS/FAIL > 默认 FAIL（宁可严格）。
    """
    verdict = ReviewVerdict(passed=False, conclusion=text.strip())

    match = _CONCLUSION_RE.search(text)
    if match:
        verdict.passed = match.group(1).upper() == "PASS"
    else:
        fallback = _FALLBACK_RE.search(text)
        if fallback:
            verdict.passed = fallback.group(1).upper() == "PASS"

    for row in _ISSUE_ROW_RE.finditer(text):
        level = row.group(2).upper()
        file_name = (row.group(3) or "").strip("` ")
        problem = (row.group(4) or "").strip()
        advice = (row.group(5) or "").strip()
        if level == "P0":
            verdict.p0 += 1
        elif level == "P1":
            verdict.p1 += 1
        elif level == "P2":
            verdict.p2 += 1
        detail = f"{level} {file_name}: {problem}".strip()
        if advice:
            detail += f" —— 建议：{advice}"
        verdict.issues.append(detail)

    # 有 P0 却判 PASS，视为模型自相矛盾，按 FAIL 处理
    if verdict.p0 > 0:
        verdict.passed = False

    return verdict
