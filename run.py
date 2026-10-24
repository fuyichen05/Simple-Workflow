#!/usr/bin/env python3
"""三 Agent 顺序 Workflow 入口：需求 -> 设计 -> 代码。

常用用法：
    python run.py -r "做一个命令行待办工具"
    python run.py -f 需求.txt
    python run.py                 # 交互式粘贴需求
    python run.py --mock -r "..."  # 离线演示，不调用真实模型
    python run.py --list           # 查看历史运行
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents_workflow import __version__, console  # noqa: E402
from agents_workflow.artifacts import ArtifactError, RunContext, create_run  # noqa: E402
from agents_workflow.config import Config  # noqa: E402
from agents_workflow.pipeline import Engine, EngineError  # noqa: E402
from agents_workflow.state import read_state_file  # noqa: E402
from agents_workflow.workflow import mini_se_workflow  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# 流程跑完但质量门禁未通过：产物可用，但需要人工复核
EXIT_QUALITY = 3

HINT_END_TOKENS = {":end", ":done", "EOF", "eof"}


# ---------------------------------------------------------------------- 参数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="需求 -> 设计 -> 代码 三 Agent 顺序 Workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py -r \"做一个命令行待办工具\"\n"
            "  python run.py -f 需求.txt --model deepseek-reasoner\n"
            "  python run.py --mock -r \"随便写点什么\"\n"
            "  python run.py --list\n"
        ),
    )
    source = parser.add_argument_group("需求来源（三选一，都不给则交互输入）")
    source.add_argument("-r", "--requirement", metavar="TEXT", help="直接传入需求文本")
    source.add_argument("-f", "--requirement-file", metavar="PATH", help="从文件读取需求")

    model = parser.add_argument_group("模型与接口")
    model.add_argument("--model", help="模型名，默认取 DEEPSEEK_MODEL 或 deepseek-chat")
    model.add_argument("--base-url", help="接口地址，默认取 DEEPSEEK_BASE_URL 或官方地址")
    model.add_argument("--temperature", type=float, help="全局温度覆盖（各阶段默认 0.3/0.25/0.15）")
    model.add_argument("--max-tokens", type=int, help="单次输出上限")
    model.add_argument("--timeout", type=float, help="单次请求超时秒数")
    model.add_argument("--retries", type=int, help="可重试错误的最大重试次数")
    model.add_argument("--mock", action="store_true", help="离线演示模式，不发起网络请求")
    model.add_argument("--live", action="store_true", help="强制真实调用，缺少 Key 时直接报错")

    run = parser.add_argument_group("运行控制")
    run.add_argument("--runs-dir", metavar="DIR", help="运行产物根目录，默认 ./runs")
    run.add_argument("--run-id", metavar="ID", help="自定义本次运行 ID（已存在则拒绝执行）")
    run.add_argument("--resume", metavar="RUN_ID", help="从指定历史运行的断点继续执行")
    run.add_argument("--no-parallel", action="store_true", help="关闭并行，按串行执行并行组")
    run.add_argument("--show-graph", action="store_true", help="打印 Workflow 流程图后退出")
    run.add_argument("-q", "--quiet", action="store_true", help="只输出关键信息，完整日志仍写入文件")
    run.add_argument("--list", action="store_true", help="列出历史运行记录后退出")
    run.add_argument("--version", action="version", version=f"agents-workflow {__version__}")
    return parser


# ---------------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    console.configure_stdout()
    args = build_parser().parse_args(argv)

    cfg = Config.from_env()
    _apply_overrides(cfg, args)

    if args.list:
        return list_runs(cfg.runs_dir)
    if args.show_graph:
        return show_graph()

    for note in cfg.notes:
        console.emit("warn", note)

    print_title()

    if args.resume:
        return resume_run(cfg, args)

    # 交互式启动：在终端里运行时，先问用不用真实 API
    interactive = sys.stdin.isatty() and not args.mock and not args.live
    try:
        if interactive:
            if not setup_interactive(cfg):
                return EXIT_USAGE
        elif args.mock:
            cfg.mock = True
        elif not cfg.has_api_key:
            if args.live:
                console.emit(
                    "error",
                    "指定了 --live 但没有检测到 DEEPSEEK_API_KEY，"
                    "请设置环境变量或在项目根目录 .env 中配置",
                    stream=sys.stderr,
                )
                return EXIT_USAGE
            cfg.mock = True
        else:
            cfg.mock = False

        requirement = resolve_requirement(args)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        console.emit("error", str(exc), stream=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print()
        console.emit("warn", "已取消，未创建任何运行目录", stream=sys.stderr)
        return 130

    if not requirement.strip():
        console.emit("error", "需求内容为空，已退出", stream=sys.stderr)
        return EXIT_USAGE

    try:
        run = create_run(cfg.runs_dir, requested_id=args.run_id)
    except ArtifactError as exc:
        console.emit("error", str(exc), stream=sys.stderr)
        return EXIT_USAGE

    print_run_info(cfg, run, mock_fallback=cfg.mock and not args.mock and not interactive)

    return _execute(cfg, run, requirement)


def _execute(cfg: Config, run, requirement: str = "", *, resume: bool = False) -> int:
    """构造引擎并执行，统一处理异常与退出码。"""
    try:
        engine = Engine(cfg, run, resume=resume)
        result = engine.execute(requirement)
    except EngineError as exc:
        console.emit("error", str(exc), stream=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.emit(
            "warn",
            f"已中断。产物保留在 {run.run_dir}\n"
            f"       续跑命令：python run.py --resume {run.run_id}",
            stream=sys.stderr,
        )
        return 130

    print_footer(result.ok, run, result, result.error)
    if not result.ok:
        return EXIT_FAILED
    if result.review_rounds and not result.review_passed:
        return EXIT_QUALITY
    return EXIT_OK


def resume_run(cfg: Config, args: argparse.Namespace) -> int:
    """从断点续跑：读 state.json 恢复模式与进度。"""
    run_dir = cfg.runs_dir / args.resume
    if not run_dir.is_dir():
        console.emit("error", f"运行目录不存在: {run_dir}", stream=sys.stderr)
        return EXIT_USAGE

    state = read_state_file(run_dir)
    if not state:
        console.emit("error", "该目录缺少可用的 state.json，无法续跑", stream=sys.stderr)
        return EXIT_USAGE

    status = str(state.get("status") or "unknown")
    if status == "completed":
        console.emit("info", f"{args.resume} 已执行完成，无需续跑")
        return EXIT_OK

    if state.get("mode") == "mock":
        cfg.mock = True
    elif not cfg.has_api_key:
        console.emit(
            "error",
            "该运行使用真实 API，但当前没有可用的 DEEPSEEK_API_KEY，无法续跑",
            stream=sys.stderr,
        )
        return EXIT_USAGE
    else:
        cfg.mock = False

    run = RunContext(run_id=args.resume, run_dir=run_dir)
    console.rule()
    print(f"  续跑 ID   : {console.paint(args.resume, 'bold')}")
    print(f"  产物目录  : {run_dir}")
    print(f"  上次状态  : {status}")
    print(f"  下一节点  : {state.get('next_node') or '(起始节点)'}")
    print(f"  模式      : {'离线演示 Mock' if cfg.mock else '真实调用 DeepSeek'}")
    console.rule()

    return _execute(cfg, run, resume=True)


def show_graph() -> int:
    """打印流程定义，便于讲解与检查。"""
    spec = mini_se_workflow()
    print(spec.ascii_diagram())
    problems = spec.validate()
    if problems:
        console.emit("warn", "流程定义存在问题：")
        for item in problems:
            print(f"  - {item}")
        return EXIT_USAGE
    console.emit("ok", "流程定义校验通过（节点、边、并行组出口一致）")
    print()
    print(console.paint("Mermaid（可直接贴进 Markdown）:", "dim"))
    print(spec.mermaid())
    return EXIT_OK


# ------------------------------------------------------------------ 交互引导


def setup_interactive(cfg: Config) -> bool:
    """终端交互：选择运行方式，必要时录入 API Key。

    返回 False 表示用户放弃，调用方应以退出码 2 结束。
    """
    use_api = ask_mode(cfg)
    if use_api is None:
        console.emit("warn", "未选择运行方式，已退出", stream=sys.stderr)
        return False

    if not use_api:
        cfg.mock = True
        console.emit("info", "已选择离线演示模式（不发起任何网络请求）")
        return True

    key = ask_api_key(cfg)
    if not key:
        console.emit("error", "未获取到 API Key，已退出", stream=sys.stderr)
        return False

    cfg.api_key = key
    cfg.mock = False
    console.emit("ok", f"已就绪，使用 Key {mask_key(key)}")
    return True


def ask_mode(cfg: Config) -> bool | None:
    """询问是否调用真实 API。True=用 API，False=离线，None=放弃。"""
    api_line = "调用 DeepSeek API    —— 真实生成，需要 API Key"
    if cfg.has_api_key:
        api_line += "（已检测到配置）"

    print()
    print("  " + console.paint("请选择运行方式：", "bold"))
    print(f"    {console.paint('[1]', 'green')} {api_line}")
    print(f"    {console.paint('[2]', 'yellow')} 离线演示模式        —— 不联网，使用内置样例")
    print()

    for attempt in range(3):
        try:
            raw = input("  请输入 1 或 2，直接回车默认 1：").strip()
        except EOFError:
            return None

        if raw in ("", "1"):
            return True
        if raw == "2":
            return False
        console.emit("warn", f"只能输入 1 或 2（当前输入：{raw!r}），请重试 {attempt + 1}/3")
    return None


def ask_api_key(cfg: Config) -> str:
    """要求录入 API Key。已配置时可直接回车沿用。"""
    existing = cfg.api_key.strip()
    print()
    if existing:
        prompt = f"  已检测到 Key {mask_key(existing)}，回车沿用，或粘贴新 Key："
    else:
        prompt = "  请粘贴 DeepSeek API Key（输入不回显，仅存于本次运行的进程内存）："

    for _ in range(3):
        try:
            raw = _read_secret(prompt)
        except EOFError:
            return ""
        raw = raw.strip()
        if raw:
            return raw
        if existing:
            return existing
        console.emit("warn", "API Key 不能为空，请重新输入")
        prompt = "  请粘贴 DeepSeek API Key："
    return ""


def _read_secret(prompt: str) -> str:
    """隐藏回显读取敏感输入；终端不支持时退回普通输入。"""
    import getpass

    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        return input(prompt)


def mask_key(key: str) -> str:
    """脱敏展示 Key，日志与终端都不出现明文。"""
    key = key.strip()
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


# ---------------------------------------------------------------------- 辅助


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    """命令行参数覆盖配置。"""
    if args.model:
        cfg.model = args.model
    if args.base_url:
        cfg.base_url = args.base_url
    if args.temperature is not None:
        cfg.temperature = max(0.0, min(2.0, args.temperature))
    if args.max_tokens is not None and args.max_tokens > 0:
        cfg.max_tokens = args.max_tokens
    if args.timeout is not None and args.timeout > 0:
        cfg.timeout = args.timeout
    if args.retries is not None and args.retries >= 0:
        cfg.max_retries = args.retries
    if args.runs_dir:
        p = Path(args.runs_dir).expanduser()
        cfg.runs_dir = p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    if args.no_parallel:
        cfg.parallel = False
    cfg.quiet = args.quiet


def resolve_requirement(args: argparse.Namespace) -> str:
    """按 -r / -f / stdin 的优先级取需求文本。"""
    if args.requirement:
        return args.requirement
    if args.requirement_file:
        path = Path(args.requirement_file).expanduser()
        if not path.is_file():
            raise ValueError(f"需求文件不存在: {path}")
        return path.read_text(encoding="utf-8")

    if sys.stdin.isatty():
        console.rule()
        print("未通过 -r / -f 提供需求，请直接粘贴需求描述。")
        print(f"输入结束后，另起一行输入 {console.paint(':end', 'bold')} 回车（或 Ctrl+Z 回车）。")
        console.rule()
    return _read_stdin()


def _read_stdin() -> str:
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() in HINT_END_TOKENS:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def print_title() -> None:
    """标题横幅。放在交互询问之前，让用户知道自己在启动什么。"""
    console.banner(
        "  多 Agent 协作 Workflow   需求 → 设计 → 代码 → 审查",
        f"  v{__version__}",
    )


def print_run_info(cfg: Config, run, *, mock_fallback: bool) -> None:
    """运行信息：确认本次运行落在哪个目录、用哪种模式。"""
    spec = mini_se_workflow()
    console.rule()
    print(f"  运行 ID   : {console.paint(run.run_id, 'bold')}")
    print(f"  产物目录  : {run.run_dir}")
    print(f"  流程      : {spec.name} —— {spec.description}")
    print(f"  并行      : {'开启' if cfg.parallel else '关闭'}（--no-parallel 可关闭）")
    print(f"  模型      : {cfg.model}")
    mode = "离线演示 Mock" if cfg.mock else "真实调用 DeepSeek"
    print(f"  模式      : {console.paint(mode, 'yellow' if cfg.mock else 'green')}")
    print(f"  接口      : {cfg.endpoint if not cfg.mock else '(不发起网络请求)'}")

    if mock_fallback:
        print()
        console.emit(
            "warn",
            "未检测到 DEEPSEEK_API_KEY，已自动进入离线演示模式（产物为固定样例）。",
        )
        print("       配置方式：在项目根目录创建 .env 写入 DEEPSEEK_API_KEY=sk-xxx")
        print("       或执行：set DEEPSEEK_API_KEY=sk-xxx（PowerShell: $env:DEEPSEEK_API_KEY=\"sk-xxx\"）")


def print_footer(ok: bool, run, result, error: str | None) -> None:
    console.rule()
    review_node = next((n for n in result.nodes if n.get("node") == "review"), None)
    quality_failed = bool(review_node) and not result.review_passed

    if not ok:
        label = "Workflow 中断" if result.status == "interrupted" else "Workflow 失败"
        console.emit("error", label)
        if error:
            print(f"       原因：{error}")
    elif quality_failed:
        console.emit("warn", "Workflow 跑完，但代码未通过审查（产物仅供人工复核）")
    else:
        console.emit("ok", "Workflow 完成")

    total_nodes = len(result.nodes)
    print(f"  运行 ID   : {run.run_id}")
    print(f"  产物目录  : {run.run_dir}")
    print(f"  总耗时    : {result.duration:.1f}s")
    print(f"  Token     : {result.usage.total_tokens:,}")
    print(f"  节点      : {result.done_count}/{total_nodes} 完成")

    if review_node:
        verdict = "PASS" if result.review_passed else "FAIL"
        color = "green" if result.review_passed else "yellow"
        print(
            f"  审查      : {console.paint(verdict, color)}"
            f"（{result.review_rounds} 轮）"
        )
    print(f"  消息数    : {result.messages}")
    print(f"  已完成产出: {_describe_outputs(result)}")

    if ok:
        console.rule()
        print(f"  下一步：{console.paint(str(run.run_dir / 'SUMMARY.md'), 'bold')}")
        print(f"          cd {run.run_dir / '03_code'}")
        if quality_failed:
            print(f"  遗留问题：{run.run_dir / '05_delivery.md'}")
    else:
        print(f"  排查：{run.run_dir / 'run.log'}")
        print(f"  续跑：{console.paint(f'python run.py --resume {run.run_id}', 'bold')}")
    console.rule()


def list_runs(runs_dir: Path) -> int:
    """列出历史运行，直观展示每次运行的隔离目录。"""
    if not runs_dir.is_dir():
        console.emit("info", f"尚无运行记录（目录不存在：{runs_dir}）")
        return EXIT_OK

    entries = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    if not entries:
        console.emit("info", f"尚无运行记录：{runs_dir}")
        return EXIT_OK

    console.banner("  历史运行记录", f"  目录：{runs_dir}")
    header = (
        f"{'RUN ID':<24} {_pad('状态', 10)} {_pad('审查', 5)} {_rpad('耗时', 6)} "
        f"{_rpad('文件', 4)}  {_pad('时间', 16)} 需求"
    )
    print(console.paint(header, "dim"))
    console.rule()

    for entry in entries:
        meta = _read_meta(entry)
        status = str(meta.get("status") or "unknown")
        duration = meta.get("duration_sec")
        duration_text = f"{duration}s" if isinstance(duration, (int, float)) else "-"
        files = len(meta.get("code_files") or []) + len(meta.get("test_files") or [])
        started = str(meta.get("started_at") or "")[:16].replace("T", " ")
        summary = _clip(str(meta.get("requirement") or entry.name), 34)

        review = meta.get("review") or {}
        if review:
            review_text = "PASS" if review.get("passed") else "FAIL"
            review_color = "green" if review.get("passed") else "yellow"
        else:
            review_text, review_color = "-", "dim"

        colors = {"completed": "green", "failed": "red", "interrupted": "yellow"}
        color = colors.get(status, "yellow")
        status_cell = console.paint(_pad(status, 10), color)
        review_cell = console.paint(_pad(review_text, 5), review_color)
        print(
            f"{entry.name:<24} {status_cell} {review_cell} "
            f"{_rpad(duration_text, 6)} {_rpad(str(files), 4)}  "
            f"{_pad(started, 16)} {summary}"
        )

    console.rule()
    console.emit("info", f"共 {len(entries)} 次运行，目录彼此隔离，互不覆盖")
    return EXIT_OK


def _read_meta(run_dir: Path) -> dict:
    import json

    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _display_width(text: str) -> int:
    """按终端显示宽度计算字符串长度（中文占 2 列）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """左对齐补空格，兼容中文字符宽度。"""
    return text + " " * max(0, width - _display_width(text))


def _rpad(text: str, width: int) -> str:
    """右对齐补空格，兼容中文字符宽度。"""
    return " " * max(0, width - _display_width(text)) + text


def _describe_outputs(result) -> str:
    """按实际完成的节点描述产物，失败时不留误导。"""
    if not result.nodes:
        return "无（首个节点即失败）"

    placeholder = "03_code/"
    items: list[str] = []
    code_total = 0
    for node in result.nodes:
        if node.get("status") != "done":
            continue
        output = str(node.get("output") or "")
        if output.startswith(placeholder):
            match = re.search(r"(\d+) 个文件", output)
            code_total += int(match.group(1)) if match else 0
            if placeholder not in items:
                items.append(placeholder)
        elif output:
            items.append(output)

    if not items:
        return "无"
    return ", ".join(
        f"03_code/（{code_total} 个文件）" if item == placeholder else item for item in items
    )


if __name__ == "__main__":
    sys.exit(main())
