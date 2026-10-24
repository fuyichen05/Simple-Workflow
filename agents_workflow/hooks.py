"""节点钩子：为特定节点准备上下文，或生成最终交付物。

图定义（workflow.py）只声明"谁连谁"，具体某个节点需要什么额外上下文，
统一放在这里，避免引擎里堆业务分支。

三个钩子：
    prepare_code    注入原始需求 + 上一轮审查反馈
    prepare_review  跑本地静态检查 + 拼装代码上下文
    finalize_delivery 生成交付清单
"""

from __future__ import annotations

from pathlib import Path

from .artifacts import RunContext, format_tree
from .state import WorkflowState
from .tools import build_code_context, static_check


# ------------------------------------------------------------------ 需求节点


def prepare_requirement(state: WorkflowState, run: RunContext) -> dict[str, object]:
    """把用户原始需求注入需求分析 Agent。

    原始需求存在共享状态里，任何节点都可以按需取用，不必逐级传参。
    """
    return {"requirement": str(state.get("requirement") or "")}


# ------------------------------------------------------------------ 编码节点


def prepare_code(state: WorkflowState, run: RunContext) -> dict[str, object]:
    """返工时把上一轮审查意见带进 Prompt，这是循环能收敛的关键。"""
    feedback = str(state.get("pending_feedback") or "")
    return {
        "requirement": str(state.get("requirement") or ""),
        "feedback": feedback,
    }


# ------------------------------------------------------------------ 审查节点


def prepare_review(state: WorkflowState, run: RunContext) -> dict[str, object]:
    """先跑确定性的本地检查，再让 LLM 做判断性审查。"""
    code_files = state.files("code_files")
    test_files = state.files("test_files")
    base = run.path("03_code")

    expected = list(state.get("expected_files") or [])
    result = static_check(base, code_files + test_files, expected)

    context, truncated = build_code_context(base, code_files + test_files)

    state.set("static_result", result.as_dict())
    state.set("static_report", result.as_report())
    state.set("code_truncated", truncated)

    return {
        "requirement": str(state.get("requirement") or ""),
        "static_report": result.as_report(),
        "code_block": context,
        "previous_issues": str(state.get("pending_feedback") or ""),
    }


# ------------------------------------------------------------------ 交付节点


def finalize_delivery(state: WorkflowState, run: RunContext, spec) -> str:
    """汇总全流程产出，生成一页交付说明。"""
    code_files = state.files("code_files")
    test_files = state.files("test_files")
    review = state.get("review", {}) or {}
    passed = bool(review.get("passed"))
    rounds = int(review.get("rounds") or 0)

    lines: list[str] = [
        f"# 交付说明 · {run.run_id}",
        "",
        f"- 流程：`{spec.name}` —— {spec.description}",
        f"- 审查结论：**{'PASS' if passed else 'FAIL'}**（共 {rounds} 轮）",
        "",
        "## 原始需求",
        "",
        "```text",
        str(state.get("requirement") or "").strip(),
        "```",
        "",
        "## 节点执行情况",
        "",
        "| 节点 | 状态 | 耗时 | 轮次 | 产物 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in state.summary_rows():
        elapsed = row.get("elapsed")
        elapsed_text = f"{elapsed}s" if isinstance(elapsed, (int, float)) else "-"
        status = row["status"]
        # 生成本文件时 finalize 节点自身仍是 running，展示时按即将完成处理
        if row["node"] == "finalize" and status == "running":
            status = "done"
        lines.append(
            f"| {row['node']} | {status} | {elapsed_text} | "
            f"{row.get('round', '-')} | {row.get('output', '')} |"
        )

    if code_files:
        lines += ["", "## 代码文件", "", "```text", format_tree(code_files), "```"]
    if test_files:
        lines += ["", "## 测试文件", "", "```text", format_tree(test_files), "```"]

    static = state.get("static_result") or {}
    if static:
        lines += [
            "",
            "## 本地静态检查",
            "",
            f"- 语法检查：{static.get('checked', 0)} 个文件，"
            + ("全部通过" if static.get("ok") else "存在问题"),
            f"- 缺失文件：{', '.join(static.get('missing') or []) or '无'}",
        ]

    issues = review.get("issues") or []
    if issues:
        lines += ["", "## 审查遗留问题", ""]
        lines += [f"- {item}" for item in issues]

    if not passed:
        lines += [
            "",
            "> 注意：本次交付未通过审查，产物仅供人工复核，不建议直接用于生产。",
        ]

    lines += [
        "",
        "## 如何运行生成的代码",
        "",
        "```bash",
        f"cd {run.run_dir / '03_code'}",
        "python <入口文件>.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def review_summary(state: WorkflowState) -> str:
    """审查结论的一行摘要，供终端展示。"""
    review = state.get("review", {}) or {}
    verdict = "PASS" if review.get("passed") else "FAIL"
    return f"{verdict}  P0={review.get('p0', 0)} P1={review.get('p1', 0)}"


def cleanup_code_dir(base: Path, previous: list[str]) -> list[str]:
    """返工前清理上一轮的代码文件（保留 tests/，测试不随实现重写）。"""
    removed: list[str] = []
    for rel in previous:
        if rel.startswith("tests/"):
            continue
        target = base / rel
        try:
            if target.is_file():
                target.unlink()
                removed.append(rel)
        except OSError:
            continue
    return removed
