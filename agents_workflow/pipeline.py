"""Workflow 执行引擎。

职责边界：
    workflow.py  声明流程长什么样（节点、边、条件）
    hooks.py     节点专属的上下文准备
    pipeline.py  按图调度：顺序、并行、条件分支、返工循环、断点恢复

引擎不含任何业务分支判断，所有"走哪条路"都由 workflow.py 里的条件函数决定。
"""

from __future__ import annotations

import inspect
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import console, hooks
from .agents import (
    AgentResult,
    CodeAgent,
    DesignAgent,
    RequirementAgent,
    ReviewAgent,
    TestAgent,
    extract_expected_files,
    parse_review,
)
from .artifacts import (
    ArtifactError,
    RunContext,
    RunLogger,
    format_tree,
    parse_code_files,
    utc_now_iso,
)
from .config import Config
from .llm import DeepSeekClient, LLMError, Usage, transport_name
from .message import (
    KIND_ARTIFACT,
    KIND_CONTROL,
    KIND_ERROR,
    KIND_FEEDBACK,
    KIND_STATUS,
    MessageBus,
    format_timeline,
)
from .mock import MockClient
from .state import (
    NODE_DONE,
    NODE_FAILED,
    NODE_RUNNING,
    StateError,
    WorkflowState,
)
from .tools import static_check
from .workflow import HANDLER_FINALIZE, WorkflowSpec, mini_se_workflow

MAX_REQUIREMENT_CHARS = 20_000
CODE_DIR = "03_code"


class EngineError(Exception):
    """流程执行错误（配置或调度层面）。"""


@dataclass
class PipelineResult:
    """一次运行的最终结果。"""

    run_id: str
    run_dir: Path
    status: str = "running"
    nodes: list[dict] = field(default_factory=list)
    code_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    duration: float = 0.0
    review_passed: bool = False
    review_rounds: int = 0
    messages: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @property
    def stages(self) -> list[dict]:
        """兼容旧调用方：节点执行记录。"""
        return self.nodes

    @property
    def done_count(self) -> int:
        return sum(1 for n in self.nodes if n.get("status") == NODE_DONE)


class Engine:
    """顺序 + 并行 + 条件分支 + 循环 的 Workflow 执行器。"""

    def __init__(
        self,
        cfg: Config,
        run: RunContext,
        *,
        spec: WorkflowSpec | None = None,
        resume: bool = False,
    ) -> None:
        self.cfg = cfg
        self.run = run
        self.spec = spec or mini_se_workflow()
        self.logger = RunLogger(run, quiet=cfg.quiet)
        self.bus = MessageBus(run.path("messages.jsonl"), logger=self.logger)

        problems = self.spec.validate()
        if problems:
            raise EngineError("流程定义有问题:\n  - " + "\n  - ".join(problems))

        if resume:
            restored = WorkflowState.load(run, logger=self.logger)
            self.state = restored or WorkflowState(run, logger=self.logger)
            self.bus.replay(run.path("messages.jsonl"))
            self.requirement = str(self.state.get("requirement") or "")
        else:
            self.state = WorkflowState(run, logger=self.logger)
            self.requirement = ""

        self.client = self._build_client()
        self.agents = {
            "requirement": RequirementAgent(self.client),
            "design": DesignAgent(self.client),
            "code": CodeAgent(self.client),
            "test": TestAgent(self.client),
            "review": ReviewAgent(self.client),
        }
        self.result = PipelineResult(run_id=run.run_id, run_dir=run.run_dir)
        self._started_at = utc_now_iso()

    # ------------------------------------------------------------------ 入口

    def execute(self, requirement: str = "") -> PipelineResult:
        """执行流程。requirement 为空时表示续跑，从 state 恢复。"""
        try:
            self._begin(requirement)
            self._loop()
            self.result.status = "completed"
        except (LLMError, ArtifactError, StateError, EngineError) as exc:
            self._fail(str(exc))
        except (KeyboardInterrupt, BrokenPipeError):
            # 终端被关闭或管道提前结束，等同于中断，保留产物以便 --resume
            self._mark_interrupted()
            raise
        except Exception as exc:  # 兜底：保证 state/meta 一定被写出
            self._fail(f"未预期错误: {exc.__class__.__name__}: {exc}")
            self.logger.detail(traceback.format_exc())
        finally:
            self._finalize()
        return self.result

    # ------------------------------------------------------------------ 主循环

    def _loop(self) -> None:
        next_batch = [self.state.get("next_node") or self.spec.start]
        steps = 0

        while next_batch:
            steps += 1
            if steps > self.spec.max_steps:
                raise EngineError(
                    f"超过最大步数 {self.spec.max_steps}，疑似流程存在无法收敛的循环"
                )
            nodes = self.spec.expand(next_batch[0])
            self._run_batch(nodes)
            next_batch = self._route(nodes)

    def _run_batch(self, names: list[str]) -> None:
        """执行一批节点：单个串行，多个并行。"""
        targets = [n for n in names if not self.state.is_done(n)]
        if not targets:
            self.logger.detail(f"跳过已完成节点: {', '.join(names)}")
            return

        if len(targets) > 1 and self.cfg.parallel:
            titles = " ∥ ".join(self.spec.node(n).title for n in targets)
            self.logger.step(f"并行执行：{titles}")
            with ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="node") as pool:
                futures = {pool.submit(self._run_node, self.spec.node(n)): n for n in targets}
                for future in as_completed(futures):
                    future.result()  # 任一分支失败即向上抛
        else:
            for name in targets:
                self._run_node(self.spec.node(name))

    def _route(self, executed: list[str]) -> list[str]:
        """根据条件边决定下一批节点。"""
        edges = []
        for name in executed:
            edge = self._pick_edge(name)
            if edge is not None:
                edges.append(edge)

        if not edges:
            return []

        chosen = edges[0]
        for other in edges[1:]:
            if other.dst != chosen.dst:
                raise EngineError(
                    f"并行分支出口不一致：{executed} 分别指向 {chosen.dst} 和 {other.dst}"
                )

        if chosen.rerun:
            targets = list(self.spec.resolve(chosen.dst))
            node = self.spec.node(chosen.dst)
            issues = self.state.get("review", {}).get("issues", [])
            # 审查问题落在测试文件上时，只重跑实现是修不好的，必须连同测试一起重来
            if node.group and any("tests/" in str(item) for item in issues):
                targets = self.spec.group_members(node.group)
                self.logger.info("审查问题涉及测试文件，测试节点一并重跑")
            invalidated = self._invalidate_from(targets)
            rounds = self.state.node("review").get("round") or 0
            self.bus.emit(
                "engine",
                targets[0],
                KIND_CONTROL,
                f"返工重置 {', '.join(invalidated)}（第 {rounds} 轮审查未通过）",
            )
            self.logger.warn(f"审查未通过 → 回到 {', '.join(targets)} 返工")
            self.logger.detail(f"失效节点: {', '.join(invalidated)}")

        self.state.set("next_node", chosen.dst)
        self.state.save()
        return [chosen.dst]

    def _invalidate_from(self, starts: list[str]) -> list[str]:
        """回边触发时，把目标节点及其所有下游节点打回 pending。

        只重置"从目标出发可达"的节点，因此并行组里的兄弟节点（如 test）
        不会被牵连重跑。
        """
        seen: set[str] = set()
        queue = list(starts)
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            for edge in self.spec.edges.get(name, []):
                for dst in self.spec.resolve(edge.dst):
                    if dst not in seen:
                        queue.append(dst)
        invalidated = sorted(seen)
        self.state.reset_nodes(invalidated, reason=f"返工：{starts[0]}")
        return invalidated

    def _pick_edge(self, node_name: str):
        edges = self.spec.edges.get(node_name, [])
        for edge in edges:
            if edge.when is None or edge.when(self.state):
                return edge
        if edges:
            raise EngineError(f"节点 {node_name} 没有任何出口边条件成立，流程无法继续")
        return None

    # ------------------------------------------------------------------ 节点

    def _run_node(self, spec) -> None:
        if spec.handler == HANDLER_FINALIZE:
            self._run_finalize(spec)
            return

        round_no = 0
        if spec.name == "review":
            round_no = int(self.state.node("review").get("round") or 0) + 1

        self.logger.step(f"{spec.title} 运行中 ...")
        self.state.mark_node(spec.name, NODE_RUNNING, round=round_no or None)
        started = time.monotonic()

        # 代码类节点：落盘后立刻做语法自检，不合格就地重试一次，
        # 不必等到审查环节才发现"文件根本编译不过"。
        extra_feedback = ""
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self._call_agent(spec, extra_feedback)
                output = self._store(spec, result, round_no)
            except (LLMError, ArtifactError) as exc:
                self.state.mark_node(spec.name, NODE_FAILED, error=str(exc))
                self.bus.emit(spec.name, "engine", KIND_ERROR, str(exc)[:180])
                raise

            errors = self._syntax_errors(spec)
            if not errors:
                break
            if attempt >= 2:
                self.logger.warn(
                    f"语法自检仍有 {len(errors)} 处错误，交由审查环节判定"
                )
                for item in errors[:3]:
                    print(console.paint(f"        - {item}", "dim"))
                break
            self.logger.warn(f"语法自检未通过（{len(errors)} 处），带错误信息重试一次")
            for item in errors[:3]:
                print(console.paint(f"        - {item}", "dim"))
            extra_feedback = "【本地语法检查未通过，以下文件无法编译，必须修复】\n" + "\n".join(
                f"- {item}" for item in errors
            )

        elapsed = time.monotonic() - started

        self.state.add_usage(result.usage.as_dict())
        self.state.mark_node(
            spec.name,
            NODE_DONE,
            elapsed_sec=round(elapsed, 2),
            output=output,
            attempts=result.attempts,
            round=round_no or None,
            chars=result.chars,
            self_retried=attempt - 1 or None,
        )
        self.bus.emit(
            spec.name,
            "*",
            KIND_ARTIFACT,
            f"{spec.title} 完成 -> {output}（{result.chars} 字符）",
            payload={"output": output, "chars": result.chars, "usage": result.usage.as_dict()},
        )

        self.logger.ok(
            f"{elapsed:.1f}s · {result.usage.total_tokens} tokens · "
            f"{result.chars} 字符 -> {output}"
        )
        for warning in result.warnings:
            self.logger.warn(warning)

    def _call_agent(self, spec, extra_feedback: str = "") -> AgentResult:
        agent = self.agents.get(spec.agent or "")
        if agent is None:
            raise EngineError(f"节点 {spec.name} 引用了未注册的 Agent: {spec.agent}")
        args = self._collect_args(spec, agent)
        if extra_feedback and "feedback" in inspect.signature(agent.build_messages).parameters:
            existing = str(args.get("feedback") or "")
            args["feedback"] = (extra_feedback + ("\n\n" + existing if existing else "")).strip()
        return agent.run(**args)

    def _syntax_errors(self, spec) -> list[str]:
        """对刚落盘的 Python 文件做语法检查，返回可读的错误列表。"""
        if spec.agent not in ("code", "test") or not spec.output_key:
            return []
        files = self.state.files(spec.output_key)
        if not files:
            return []
        checked = static_check(self.run.path(CODE_DIR), files)
        return [
            f"{err['file']}:{err['line']} {err['msg']}" for err in checked.errors
        ]

    def _collect_args(self, spec, agent) -> dict[str, object]:
        """从共享状态取产物，再叠加节点专属钩子参数。"""
        args: dict[str, object] = {}
        for key in spec.inputs:
            entry = self.state.artifact(key)
            if not entry:
                continue
            if entry.get("type") == "text":
                args[key] = self.state.text(key)
            else:
                args[key] = self.state.files(key)

        if spec.prepare is not None:
            args.update(spec.prepare(self.state, self.run))

        # 只传 Agent 真正接受的参数，多余的直接丢弃
        allowed = set(inspect.signature(agent.build_messages).parameters)
        return {k: v for k, v in args.items() if k in allowed}

    # ------------------------------------------------------------------ 落盘

    def _store(self, spec, result: AgentResult, round_no: int) -> str:
        if spec.agent in ("code", "test"):
            return self._store_files(spec, result)
        path = self._output_path(spec, round_no)
        self.state.put_text(spec.output_key or spec.name, path, result.output)
        if spec.name == "review":
            self._record_review(result, round_no)
        return path

    def _store_files(self, spec, result: AgentResult) -> str:
        # 原始输出留档：解析出问题时不用重跑就能查
        self.run.write_text(f"{spec.name}_raw.txt", result.output)

        report = parse_code_files(result.output)
        if report.rejected:
            for path, reason in report.rejected:
                result.warnings.append(f"跳过文件 {path}: {reason}")
                self.logger.warn(f"跳过文件 {path}：{reason}")
        if not report.files:
            raise ArtifactError(
                f"{spec.title} 的输出中没有解析到任何文件块"
                f"（原文已存 {spec.name}_raw.txt）"
            )

        if spec.name == "code":
            previous = self.state.files("code_files")
            removed = hooks.cleanup_code_dir(self.run.path(CODE_DIR), previous)
            if removed:
                self.logger.detail(f"返工清理旧文件 {len(removed)} 个")

        written: list[str] = []
        for parsed in report.files:
            rel = parsed.path
            if spec.name == "test" and not rel.startswith("tests/") and rel.endswith(".py"):
                rel = f"tests/{rel}"
            self.run.write_text(f"{CODE_DIR}/{rel}", parsed.content)
            written.append(rel)

        # 测试目录补一个 __init__.py，让 `python -m unittest discover` 可用
        if spec.name == "test" and any(w.startswith("tests/") for w in written):
            init_file = self.run.path(f"{CODE_DIR}/tests/__init__.py")
            if not init_file.is_file():
                init_file.parent.mkdir(parents=True, exist_ok=True)
                init_file.write_text("", encoding="utf-8")
                written.append("tests/__init__.py")

        written.sort()
        self.state.put_files(spec.output_key or spec.name, written)

        if spec.name == "code":
            self.result.code_files = written
            self.state.set("expected_files", extract_expected_files(self.state.text("design_doc")))
        else:
            self.result.test_files = written

        print(console.paint(format_tree(written), "dim"))
        return f"{CODE_DIR}/（{len(written)} 个文件）"

    def _record_review(self, result: AgentResult, round_no: int) -> None:
        verdict = parse_review(result.output)
        self.state.set(
            "review",
            {
                "passed": verdict.passed,
                "p0": verdict.p0,
                "p1": verdict.p1,
                "p2": verdict.p2,
                "issues": verdict.issues,
                "rounds": round_no,
                "summary": verdict.as_summary(),
            },
        )
        self.state.set("pending_feedback", "" if verdict.passed else verdict.as_feedback())
        self.result.review_passed = verdict.passed
        self.result.review_rounds = round_no

        if verdict.passed:
            self.logger.ok(f"审查通过（{verdict.as_summary()}），进入交付")
        else:
            self.logger.warn(f"审查未通过（{verdict.as_summary()}），问题：")
            for item in verdict.issues[:6]:
                print(console.paint(f"        - {item}", "dim"))
            self.bus.emit(
                "review",
                "code",
                KIND_FEEDBACK,
                f"审查未通过，回传 {len(verdict.issues)} 条问题",
                content=verdict.as_feedback(),
            )

    def _run_finalize(self, spec) -> None:
        self.logger.step(f"{spec.title} 运行中 ...")
        self.state.mark_node(spec.name, NODE_RUNNING)
        text = hooks.finalize_delivery(self.state, self.run, self.spec)
        path = self._output_path(spec, 0)
        self.state.put_text(spec.output_key or spec.name, path, text)
        self.state.mark_node(spec.name, NODE_DONE, output=path)
        self.bus.emit("finalize", "*", KIND_ARTIFACT, f"交付说明 -> {path}")
        self.logger.ok(f"交付说明 -> {path}")

    def _output_path(self, spec, round_no: int) -> str:
        template = spec.output_path or f"{spec.name}.md"
        try:
            return template.format(round=round_no or 1)
        except (KeyError, IndexError):
            return template

    # ------------------------------------------------------------------ 收尾

    def _begin(self, requirement: str) -> None:
        text = (requirement or "").strip()
        if not self.requirement:
            if not text:
                raise EngineError("需求内容为空，无法开始")
            if len(text) > MAX_REQUIREMENT_CHARS:
                raise EngineError(
                    f"需求过长（{len(text)} 字符 > 上限 {MAX_REQUIREMENT_CHARS}），请精简后再试"
                )
            self.requirement = text
            self.state.set("requirement", text)
            self.state.set("requirement_chars", len(text))
            self.run.write_text("requirement_input.txt", text + "\n")

        self.state.set("workflow", self.spec.name)
        self.state.set("status", "running")
        self.state.set("mode", "mock" if self.cfg.mock else "live")
        self.state.save()

        self.logger.info(f"流程 {self.spec.name} 启动，节点数 {len(self.spec.nodes)}")
        self.bus.emit("engine", "*", KIND_STATUS, f"workflow {self.spec.name} started")

    def _fail(self, message: str) -> None:
        self.result.status = "failed"
        self.result.error = message
        self.state.set("status", "failed")
        self.state.set("error", message)
        self.state.save()
        self.bus.emit("engine", "*", KIND_ERROR, message[:200])
        self.logger.error(message)

    def _mark_interrupted(self) -> None:
        self.result.status = "interrupted"
        self.state.set("status", "interrupted")
        self.state.save()
        self.logger.warn("已中断，可用 --resume 续跑")

    def _finalize(self) -> None:
        duration = _seconds(self._started_at)
        self.result.duration = duration
        self.result.nodes = self.state.summary_rows()
        self.result.messages = len(self.bus)
        self.result.code_files = self.state.files("code_files")
        self.result.test_files = self.state.files("test_files")
        review = self.state.get("review", {}) or {}
        self.result.review_passed = bool(review.get("passed"))
        self.result.review_rounds = int(review.get("rounds") or 0)

        usage = self.state.get("usage", {}) or {}
        self.result.usage = Usage(
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(usage.get("total_tokens", 0)),
        )

        if self.result.status == "running":
            self.result.status = "incomplete"

        self.state.set("status", self.result.status)
        self.state.set("duration_sec", round(duration, 2))
        self.state.save()
        self._write_meta(duration)
        self._write_summary(duration)

    def _write_meta(self, duration: float) -> None:
        meta = {
            "run_id": self.run.run_id,
            "status": self.result.status,
            "mode": "mock" if self.cfg.mock else "live",
            "workflow": self.spec.name,
            "started_at": self._started_at,
            "finished_at": utc_now_iso(),
            "duration_sec": round(duration, 2),
            "requirement": self.requirement,
            "requirement_chars": len(self.requirement),
            "config": self.cfg.describe(),
            "transport": "mock" if self.cfg.mock else transport_name(),
            "nodes": self.result.nodes,
            "code_files": self.result.code_files,
            "test_files": self.result.test_files,
            "usage_total": self.result.usage.as_dict(),
            "review": self.state.get("review", {}),
            "messages": self.result.messages,
            "error": self.result.error,
        }
        try:
            self.run.write_json("meta.json", meta)
        except (OSError, ArtifactError) as exc:
            self.logger.detail(f"meta.json 写入失败: {exc}")

    def _write_summary(self, duration: float) -> None:
        mode = "离线演示（Mock）" if self.cfg.mock else "真实调用"
        review = self.state.get("review", {}) or {}
        lines = [
            f"# 运行摘要 · {self.run.run_id}",
            "",
            f"- 状态：**{self.result.status}**",
            f"- 流程：`{self.spec.name}` —— {self.spec.description}",
            f"- 模式：{mode}",
            f"- 模型：{self.cfg.model}",
            f"- 总耗时：{duration:.1f}s",
            f"- Token 合计：{self.result.usage.total_tokens}",
            f"- 审查：**{'PASS' if review.get('passed') else 'FAIL'}**"
            f"（{review.get('rounds', 0)} 轮，P0={review.get('p0', 0)}）",
            f"- 消息数：{self.result.messages}",
            "",
            "## 原始需求",
            "",
            "```text",
            self.requirement,
            "```",
            "",
            "## 节点执行",
            "",
            "| 节点 | 状态 | 耗时 | 轮次 | 产物 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in self.result.nodes:
            elapsed = row.get("elapsed")
            elapsed_text = f"{elapsed}s" if isinstance(elapsed, (int, float)) else "-"
            lines.append(
                f"| {row['node']} | {row['status']} | {elapsed_text} | "
                f"{row.get('round', '-')} | {row.get('output', '')} |"
            )

        if self.result.code_files:
            lines += ["", "## 代码文件", "", "```text", format_tree(self.result.code_files), "```"]
        if self.result.test_files:
            lines += ["", "## 测试文件", "", "```text", format_tree(self.result.test_files), "```"]

        timeline = format_timeline(self.bus.all(), limit=25)
        if timeline:
            lines += ["", "## Agent 消息时间线", "", "```text", timeline, "```"]

        if self.result.error:
            lines += ["", "## 失败原因", "", "```text", self.result.error, "```"]

        lines += [
            "",
            "## 目录说明",
            "",
            "| 文件 | 说明 |",
            "| --- | --- |",
            "| state.json | 共享状态：节点状态、产物索引、审查结论 |",
            "| messages.jsonl | 全部 Agent 消息，可回放协作过程 |",
            "| 01_requirement.md | 阶段 1 需求规格说明书 |",
            "| 02_design.md | 阶段 2 技术设计说明书 |",
            "| 03_code/ | 阶段 3 生成的代码与 tests/ |",
            "| 04_review_rN.md | 阶段 4 第 N 轮审查报告 |",
            "| 05_delivery.md | 交付说明 |",
            "| llm/ | 每次调用的请求与响应原文 |",
            "| run.log | 完整运行日志 |",
            "| meta.json | 机器可读的运行元数据 |",
            "",
        ]
        try:
            self.run.write_text("SUMMARY.md", "\n".join(lines))
        except (OSError, ArtifactError):
            pass

    # ------------------------------------------------------------------ 其它

    def _build_client(self):
        if self.cfg.mock:
            return MockClient()
        return DeepSeekClient(self.cfg, dump_dir=self.run.path("llm"), logger=self.logger)


def _seconds(start_iso: str) -> float:
    import datetime as dt

    try:
        start = dt.datetime.fromisoformat(start_iso)
        end = dt.datetime.now().astimezone()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (end - start).total_seconds())


# 兼容旧名字
Pipeline = Engine
