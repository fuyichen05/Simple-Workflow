"""Workflow 图定义：节点、条件边、并行组。

把"流程长什么样"从"怎么执行"里彻底剥出来：
    workflow.py  声明有哪些节点、谁能到谁、什么条件下走哪条边
    pipeline.py  读取声明并按图调度，不含任何业务细节

新增一个 Agent = 在 nodes 里加一条 + 在 edges 里接线，引擎代码零改动。
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Callable

from . import hooks
from .state import WorkflowState

# 返工循环上限：审查不通过最多回炉 2 次
MAX_REVIEW_ROUNDS = 2

# 组前缀：边的目标写成 "group:generate" 表示并行展开该组
GROUP_PREFIX = "group:"

# 节点处理器类型
HANDLER_AGENT = "agent"
HANDLER_FINALIZE = "finalize"


@dataclass(frozen=True)
class NodeSpec:
    """一个 Workflow 节点。

    name         节点标识，同时是消息里的 sender/receiver 名
    title        人类可读名称
    agent        注册表中的 Agent key；内置节点可为 None
    inputs       执行前需要从共享状态读取的产物 key
    output_key   本节点产物在共享状态中的 key
    output_path  产物落盘路径，支持 {round} 占位（审查轮次）
    group        并行组名，同组节点会并发执行
    handler      处理器类型：agent / finalize
    prepare      节点专属的上下文准备钩子，返回额外参数
    """

    name: str
    title: str
    agent: str | None = None
    inputs: tuple[str, ...] = ()
    output_key: str | None = None
    output_path: str | None = None
    group: str | None = None
    handler: str = HANDLER_AGENT
    prepare: Callable[[WorkflowState, object], dict] | None = None

    @property
    def output(self) -> str:
        return self.output_key or self.name


@dataclass(frozen=True)
class Edge:
    """有向边。when 为 None 表示无条件，否则按顺序匹配第一个为真的边。

    rerun=True 表示这是一条循环回边：目标节点即使已完成也要重置重跑。
    """

    dst: str
    when: Callable[[WorkflowState], bool] | None = None
    label: str = ""
    rerun: bool = False


@dataclass
class WorkflowSpec:
    """完整流程定义。"""

    name: str
    start: str
    nodes: dict[str, NodeSpec]
    edges: dict[str, list[Edge]]
    max_steps: int = 32
    description: str = ""

    # ------------------------------------------------------------------ 查询

    def node(self, name: str) -> NodeSpec:
        try:
            return self.nodes[name]
        except KeyError as exc:
            raise KeyError(f"流程 {self.name} 中不存在节点: {name}") from exc

    def group_members(self, group: str) -> list[str]:
        return [n.name for n in self.nodes.values() if n.group == group]

    def resolve(self, dst: str) -> list[str]:
        """把边的目标解析成一批要执行的节点。"""
        if dst.startswith(GROUP_PREFIX):
            group = dst[len(GROUP_PREFIX):]
            members = self.group_members(group)
            if not members:
                raise KeyError(f"流程 {self.name} 中不存在并行组: {group}")
            return members
        return [dst]

    def expand(self, name: str) -> list[str]:
        """把任意节点名展开成实际要执行的批次。

        - "group:generate" -> 组内全部节点
        - 组内某节点（如 "code"）-> 该组全部节点，保证并行组不会只跑一半
        - 普通节点 -> 自身
        """
        if name.startswith(GROUP_PREFIX):
            return self.resolve(name)
        node = self.nodes.get(name)
        if node is not None and node.group:
            return self.group_members(node.group)
        return [name]

    # ------------------------------------------------------------------ 校验

    def validate(self) -> list[str]:
        """静态检查流程定义，返回问题列表（供 --show-graph 与单测使用）。"""
        problems: list[str] = []
        if self.start not in self.nodes:
            problems.append(f"start 节点不存在: {self.start}")

        for name in self.nodes:
            if name not in self.edges:
                problems.append(f"节点 {name} 没有定义出口（终止节点请写空列表）")
        for src, edge_list in self.edges.items():
            if src not in self.nodes:
                problems.append(f"边来源未定义: {src}")
            # 空列表表示终止节点，合法
            for i, edge in enumerate(edge_list):
                if edge.when is None and i != len(edge_list) - 1:
                    problems.append(f"节点 {src} 的无条件边必须是最后一条")
                try:
                    for dst in self.resolve(edge.dst):
                        if dst not in self.nodes:
                            problems.append(f"边目标未定义: {src} -> {edge.dst}")
                except KeyError as exc:
                    problems.append(str(exc))

        # 组内节点出口必须一致，否则并行分支无法合并
        groups: dict[str, set[str]] = {}
        for node in self.nodes.values():
            if node.group:
                keys = tuple(sorted(f"{e.dst}:{e.label}" for e in self.edges.get(node.name, [])))
                groups.setdefault(node.group, set()).add(str(keys))
        for group, signatures in groups.items():
            if len(signatures) > 1:
                problems.append(f"并行组 {group} 内节点出口不一致，无法合并")

        return problems

    # ------------------------------------------------------------------ 展示

    def ascii_diagram(self) -> str:
        """生成终端可读的流程图（不依赖图形库）。"""
        lines: list[str] = []
        width = 64
        lines.append("=" * width)
        lines.append(f" 流程: {self.name}   {self.description}")
        lines.append("=" * width)

        for name, node in self.nodes.items():
            mark = f"[{name}]"
            if node.group:
                mark += f" (并行组:{node.group})"
            lines.append(f"  {_pad(mark, 34)}{node.title}")
            for edge in self.edges.get(name, []):
                label = f"--{edge.label or ''}-->"
                extra = "   ← 返工循环" if edge.rerun else ""
                lines.append(f"      {_pad(label, 26)}{edge.dst}{extra}")
        lines.append("=" * width)
        lines.append(f" 步数上限 {self.max_steps}，返工循环上限 {MAX_REVIEW_ROUNDS} 次")
        return "\n".join(lines)

    def mermaid(self) -> str:
        """Mermaid 流程图源码，可直接贴进 Markdown 文档。"""
        rows = ["flowchart TD"]
        groups: dict[str, list[str]] = {}
        for name, node in self.nodes.items():
            if node.group:
                groups.setdefault(node.group, []).append(name)
            else:
                rows.append(f'    {name}["{node.title}"]')

        for group, members in groups.items():
            rows.append(f'    subgraph {group}["并行组：{group}"]')
            for member in members:
                rows.append(f'        {member}["{self.nodes[member].title}"]')
            rows.append("    end")

        drawn: set[tuple[str, str, str]] = set()
        for src, edge_list in self.edges.items():
            for edge in edge_list:
                label = edge.label or ""
                if edge.dst.startswith(GROUP_PREFIX):
                    members = self.group_members(edge.dst[len(GROUP_PREFIX):])
                else:
                    members = [edge.dst]
                for dst in members:
                    key = (src, dst, label)
                    if key in drawn:
                        continue
                    drawn.add(key)
                    arrow = f"-->|{label}|" if label else "-->"
                    rows.append(f"    {src} {arrow} {dst}")
        return "\n".join(rows)


# --------------------------------------------------------------------- 条件


def review_passed(state: WorkflowState) -> bool:
    """审查通过。"""
    return bool(state.get("review", {}).get("passed"))


def _review_rounds(state: WorkflowState) -> int:
    return int(state.node("review").get("round") or 0)


def review_retryable(state: WorkflowState) -> bool:
    """审查未通过，且还有返工次数。"""
    if review_passed(state):
        return False
    return _review_rounds(state) < MAX_REVIEW_ROUNDS


def review_exhausted(state: WorkflowState) -> bool:
    """审查未通过且次数用尽 —— 带着遗留问题进入交付。"""
    if review_passed(state):
        return False
    return _review_rounds(state) >= MAX_REVIEW_ROUNDS


# --------------------------------------------------------------------- 定义


def mini_se_workflow() -> WorkflowSpec:
    """Mini SE 流程：需求 -> 设计 -> (代码 ∥ 测试) -> 审查 -(不通过)-> 返工 -> 交付。"""
    nodes = {
        "requirement": NodeSpec(
            name="requirement",
            title="需求分析 Agent",
            agent="requirement",
            output_key="requirement_doc",
            output_path="01_requirement.md",
            prepare=hooks.prepare_requirement,
        ),
        "design": NodeSpec(
            name="design",
            title="架构设计 Agent",
            agent="design",
            inputs=("requirement_doc",),
            output_key="design_doc",
            output_path="02_design.md",
        ),
        "code": NodeSpec(
            name="code",
            title="编码实现 Agent",
            agent="code",
            inputs=("requirement_doc", "design_doc"),
            output_key="code_files",
            output_path="03_code",
            group="generate",
            prepare=hooks.prepare_code,
        ),
        "test": NodeSpec(
            name="test",
            title="测试生成 Agent",
            agent="test",
            inputs=("requirement_doc", "design_doc"),
            output_key="test_files",
            output_path="03_code",
            group="generate",
        ),
        "review": NodeSpec(
            name="review",
            title="代码审查 Agent",
            agent="review",
            inputs=("requirement_doc", "design_doc", "code_files", "test_files"),
            output_key="review_doc",
            output_path="04_review_r{round}.md",
            prepare=hooks.prepare_review,
        ),
        "finalize": NodeSpec(
            name="finalize",
            title="交付汇总",
            handler=HANDLER_FINALIZE,
            inputs=("requirement_doc", "design_doc", "review_doc"),
            output_key="delivery",
            output_path="05_delivery.md",
        ),
    }

    edges = {
        "requirement": [Edge(dst="design", label="规格说明书")],
        "design": [Edge(dst=f"{GROUP_PREFIX}generate", label="设计与接口")],
        "code": [Edge(dst="review", label="代码 + 测试")],
        "test": [Edge(dst="review", label="代码 + 测试")],
        "review": [
            Edge(dst="finalize", when=review_passed, label="PASS"),
            Edge(dst="finalize", when=review_exhausted, label="FAIL(轮次用尽)"),
            Edge(dst="code", when=review_retryable, label="FAIL", rerun=True),
        ],
        "finalize": [],
    }

    return WorkflowSpec(
        name="mini-se",
        start="requirement",
        nodes=nodes,
        edges=edges,
        description="需求 → 设计 → 代码∥测试 → 审查 → 返工循环 → 交付",
    )


def _pad(text: str, width: int) -> str:
    """按终端显示宽度左对齐补空格（中文占 2 列）。"""
    import unicodedata

    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - shown)
