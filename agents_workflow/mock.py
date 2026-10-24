"""离线演示模式（Mock）。

用途：没有 API Key、或需要验证 Workflow 编排与产物隔离时，
不发起任何网络请求，直接返回一套完整、可运行的三阶段样例产物。

注意：Mock 产物与用户真实需求无关，仅供流程验证；meta.json 会标记 mode=mock。
"""

from __future__ import annotations

import time

from .llm import LLMResponse, Usage

_BANNER = "> 本文件由**离线演示模式（Mock）**生成，内容为固定样例，不反映真实需求。\n"

REQUIREMENT_MOCK = f"""\
{_BANNER}
## 1. 背景与目标

个人用户需要一个命令行待办清单工具，用于快速记录、查看和勾选当天要做的事。
目标是把"想到一件事"到"记下来"的路径压缩到一条命令，数据保存在本地，不依赖网络。
成功标准：单条命令完成增删改查，进程启动时间低于 100ms，数据文件损坏时能给出明确提示。

## 2. 目标用户与核心场景

- **个人开发者**：在终端里写代码时，想到待办随手 `todo add "修复登录超时"`，不切换窗口。
- **学生**：晚上复盘时 `todo list` 查看未完成项，逐条 `todo done <id>` 勾选。

## 3. 功能需求

- FR-1（P0）：新增待办。输入一段文本，生成递增 ID 并持久化，返回新 ID。
- FR-2（P0）：列出待办。默认只显示未完成项，`--all` 显示全部，输出含 ID/状态/文本。
- FR-3（P0）：标记完成。按 ID 把待办置为已完成，ID 不存在时提示并返回非零退出码。
- FR-4（P0）：删除待办。按 ID 删除，需在输出中回显被删内容。
- FR-5（P1）：数据持久化到 JSON 文件，写入使用临时文件 + 重命名，避免半截文件。
- FR-6（P2）：空文本、超长文本（>200 字符）拒绝写入并给出提示。

## 4. 非功能需求

- **性能**：本地 1000 条待办时，`list` 命令耗时 < 50ms。
- **可靠性**：JSON 损坏时不静默覆盖，保留原文件并报错退出，退出码 2。
- **安全**：不执行任何外部命令，不把用户输入拼进 shell；文件路径固定为用户目录下 `.todo.json`。
- **可维护性**：存储层与命令层分离，存储层不依赖 argparse。
- **兼容性**：Python 3.10+，仅使用标准库，Windows / macOS / Linux 均可运行。

## 5. 验收标准

- Given 空仓库，When 执行 `todo add "买菜"`，Then 文件新增一条 done=false 的记录且输出 `added #1`。
- Given 一条未完成待办，When 执行 `todo done 1`，Then `todo list` 不再显示该条，`--all` 显示为已完成。
- Given 一个不存在的 ID，When 执行 `todo done 99`，Then 输出错误提示且退出码为 1。
- Given 内容损坏的 JSON 文件，When 执行任意命令，Then 输出"数据文件损坏"并退出码 2，原文件保持不变。

## 6. 范围外

- 不做多用户、不做云同步。
- 不做截止日期、提醒、重复任务。
- 不做 TUI / GUI 界面。
- 不做导入导出（CSV / ICS）。
- 不做标签与全文搜索。

## 7. 约束与假设

- 仅使用 Python 标准库，不引入 click / rich 等依赖。
- 数据文件位置固定为 `~/.todo.json`，允许通过环境变量 `TODO_FILE` 覆盖（便于测试）。
- 假设单进程使用，不做文件锁。

## 8. 待澄清问题

- Q-1：是否需要优先级字段？—— 默认决策：本期不做，仅保留 done 状态。
- Q-2：是否需要软删除？—— 默认决策：直接删除，不保留历史。
"""

DESIGN_MOCK = '''\
## 1. 设计概述与关键取舍

采用"薄命令层 + 独立存储层"的两层结构：`todo.py` 只负责参数解析与输出，
`storage.py` 负责 JSON 读写与原子落盘。
关键取舍一：用单文件 JSON 而非 SQLite——数据量小、便于人工查看，代价是并发写不安全（已列入范围外）。
关键取舍二：ID 用"当前最大 ID + 1"而非 uuid——符合命令行用户逐条引用的习惯。

## 2. 技术栈与依赖

- Python 3.10+
- 仅使用标准库：`argparse`、`json`、`os`、`sys`、`tempfile`、`pathlib`、`datetime`
- 无第三方依赖，无降级方案需求

## 3. 系统架构

- 入口层 `todo.py`
  - 解析子命令 add / list / done / rm
  - 捕获 StorageError，转成用户可读提示与退出码
- 存储层 `storage.py`
  - `load_items()` 读取并校验结构
  - `save_items()` 原子写入
- 数据层
  - `~/.todo.json`，结构为 `{"version": 1, "next_id": 2, "items": [...]}`

数据流：argv -> argparse -> storage.load_items() -> 变更内存列表 -> storage.save_items() -> 打印结果

## 4. 模块与文件结构

```
todo-demo/
├── todo.py          # 命令入口：参数解析、子命令分发、输出与退出码
├── storage.py       # 存储层：JSON 读写、结构校验、原子落盘
├── test_todo.py     # 单元测试：覆盖增删改查与损坏文件场景
└── README.md        # 使用说明
```

## 5. 核心数据结构

| 字段 | 类型 | 含义 | 必填 |
| --- | --- | --- | --- |
| items | list[dict] | 全部待办记录 | 是 |
| items[].id | int | 自增主键，从 1 开始 | 是 |
| items[].text | str | 待办内容，1-200 字符 | 是 |
| items[].done | bool | 是否完成 | 是 |
| items[].created | str | 创建时间，ISO8601 | 是 |
| next_id | int | 下一条待办使用的 ID | 是 |

## 6. 关键流程

1. 解析命令行，确定子命令与参数。
2. 调用 `load_items()`；文件不存在则返回空集合，文件损坏则抛 `StorageError` -> 打印提示，退出码 2。
3. 执行子命令：
   - add：校验文本 1-200 字符（非法 -> 提示，退出码 1），追加记录，`next_id += 1`。
   - list：按 `--all` 过滤，格式化输出；空集合提示"暂无待办"。
   - done：按 ID 查找（找不到 -> 提示，退出码 1），置 `done=True`。
   - rm：按 ID 查找（找不到 -> 提示，退出码 1），移除。
4. 需要变更时调用 `save_items()`（先写临时文件，`os.replace` 原子替换）。
5. 返回退出码 0。

## 7. 接口设计

```python
class StorageError(Exception):
    """数据文件读写或结构错误。"""

def resolve_path() -> Path:
    """返回数据文件路径，优先取环境变量 TODO_FILE。"""

def load_items() -> list[dict]:
    """读取全部待办。文件不存在返回 []；结构非法抛 StorageError。"""

def save_items(items: list[dict]) -> None:
    """原子写入全部待办。IO 失败抛 StorageError。"""

def main(argv: list[str] | None = None) -> int:
    """命令入口，返回进程退出码。"""
```

## 8. 错误处理与日志策略

- 用户输入错误（空文本、ID 非法、ID 不存在）：打印 `error: ...`，退出码 1，不写文件。
- 外部依赖失败（读不到文件、磁盘不可写）：`StorageError` -> 打印原因，退出码 2。
- 内部逻辑错误：不吞异常，让 traceback 暴露（便于排查）。
- 不引入 logging，命令行工具直接用 stderr 输出错误。

## 9. 安全与边界

- 文本长度上限 200 字符（对应 FR-6）。
- 路径固定，不接受用户传入任意路径，规避路径穿越。
- 写入前落临时文件，`os.replace` 保证原子性，避免断电产生半截 JSON。
- 不读取环境变量以外的外部配置，不发起网络请求。

## 10. 测试策略

- add 正常写入并返回 ID（FR-1）
- add 空文本被拒绝，退出码 1（FR-6）
- list 默认过滤已完成（FR-2）
- list --all 显示全部（FR-2）
- done 不存在的 ID 返回 1（FR-3）
- rm 后文件条目减少（FR-4）
- 损坏 JSON 触发 StorageError 且原文件不变（FR-5）
- 1000 条数据下 list 输出正确（性能边界）

## 11. 交付文件清单

| 路径 | 类型 | 说明 |
| --- | --- | --- |
| todo.py | 入口 | 命令行解析与子命令实现 |
| storage.py | 模块 | JSON 原子读写与校验 |
| test_todo.py | 测试 | 单元测试 |
| README.md | 文档 | 用法说明 |
'''

CODE_MOCK = '''\
<<<FILE: storage.py>>>
\"\"\"待办数据的持久化层：JSON 读取、校验与原子写入。\"\"\"

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULT_PATH = Path.home() / ".todo.json"


class StorageError(Exception):
    \"\"\"数据文件读写或结构错误。\"\"\"


def resolve_path() -> Path:
    \"\"\"返回数据文件路径，优先使用环境变量 TODO_FILE。\"\"\"
    override = os.environ.get("TODO_FILE")
    if override:
        return Path(override).expanduser()
    return DEFAULT_PATH


def load() -> dict:
    \"\"\"读取数据文件内容。

    文件不存在时返回空结构；结构非法时抛 StorageError，且绝不改写原文件。
    \"\"\"
    path = resolve_path()
    if not path.is_file():
        return {"version": 1, "next_id": 1, "items": []}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"无法读取 {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorageError(f"数据文件损坏（{path} 第 {exc.lineno} 行）") from exc

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise StorageError(f"数据文件结构不符合预期: {path}")

    data.setdefault("version", 1)
    data.setdefault("next_id", _max_id(data["items"]) + 1)
    return data


def save(data: dict) -> None:
    \"\"\"原子写入：先写同目录临时文件，再 os.replace 覆盖。\"\"\"
    path = resolve_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"无法创建目录 {path.parent}: {exc}") from exc

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
        ) as fh:
            tmp_name = fh.name
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except OSError as exc:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise StorageError(f"无法写入 {path}: {exc}") from exc


def _max_id(items: list) -> int:
    ids = [item.get("id", 0) for item in items if isinstance(item, dict)]
    return max([i for i in ids if isinstance(i, int)], default=0)
<<<END>>>

<<<FILE: todo.py>>>
\"\"\"命令行待办清单：add / list / done / rm。

用法：
    python todo.py add "写周报"
    python todo.py list --all
    python todo.py done 1
    python todo.py rm 1
\"\"\"

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from storage import StorageError, load, save

MAX_TEXT = 200


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo", description="一个极简的命令行待办工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="新增待办")
    p_add.add_argument("text", nargs="+", help="待办内容")

    p_list = sub.add_parser("list", help="列出待办")
    p_list.add_argument("--all", action="store_true", help="包含已完成项")

    p_done = sub.add_parser("done", help="标记完成")
    p_done.add_argument("id", type=int, help="待办 ID")

    p_rm = sub.add_parser("rm", help="删除待办")
    p_rm.add_argument("id", type=int, help="待办 ID")

    return parser


def _find(items: list, item_id: int):
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def cmd_add(args, data: dict) -> int:
    text = " ".join(args.text).strip()
    if not text:
        return _fail("内容不能为空")
    if len(text) > MAX_TEXT:
        return _fail(f"内容过长（{len(text)} > {MAX_TEXT} 字符）")

    item = {
        "id": data["next_id"],
        "text": text,
        "done": False,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    data["items"].append(item)
    data["next_id"] += 1
    save(data)
    print(f"added #{item['id']}: {text}")
    return 0


def cmd_list(args, data: dict) -> int:
    items = data["items"] if args.all else [i for i in data["items"] if not i.get("done")]
    if not items:
        print("暂无待办")
        return 0
    for item in items:
        mark = "x" if item.get("done") else " "
        print(f"[{mark}] #{item.get('id', '?'):<3} {item.get('text', '')}")
    return 0


def cmd_done(args, data: dict) -> int:
    item = _find(data["items"], args.id)
    if item is None:
        return _fail(f"找不到 ID {args.id}")
    if item.get("done"):
        print(f"#{args.id} 已经是完成状态")
        return 0
    item["done"] = True
    save(data)
    print(f"done #{args.id}: {item.get('text', '')}")
    return 0


def cmd_rm(args, data: dict) -> int:
    item = _find(data["items"], args.id)
    if item is None:
        return _fail(f"找不到 ID {args.id}")
    data["items"] = [i for i in data["items"] if i.get("id") != args.id]
    save(data)
    print(f"removed #{args.id}: {item.get('text', '')}")
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


HANDLERS = {"add": cmd_add, "list": cmd_list, "done": cmd_done, "rm": cmd_rm}


def main(argv: list[str] | None = None) -> int:
    \"\"\"命令入口，返回进程退出码。\"\"\"
    args = _build_parser().parse_args(argv)
    try:
        data = load()
        return HANDLERS[args.command](args, data)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
<<<END>>>

<<<FILE: test_todo.py>>>
\"\"\"todo.py 与 storage.py 的单元测试。\"\"\"

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import storage
import todo


class TodoTestCase(unittest.TestCase):
    \"\"\"每个用例使用独立的临时数据文件，避免互相污染。\"\"\"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_file = Path(self.tmpdir.name) / "todo.json"
        os.environ["TODO_FILE"] = str(self.data_file)

    def tearDown(self) -> None:
        os.environ.pop("TODO_FILE", None)

    def test_add_and_list(self) -> None:
        self.assertEqual(todo.main(["add", "买菜"]), 0)
        self.assertTrue(self.data_file.is_file())
        data = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(data["items"][0]["text"], "买菜")
        self.assertFalse(data["items"][0]["done"])

    def test_add_empty_rejected(self) -> None:
        self.assertEqual(todo.main(["add", "   "]), 1)

    def test_add_too_long_rejected(self) -> None:
        self.assertEqual(todo.main(["add", "x" * (todo.MAX_TEXT + 1)]), 1)

    def test_done_missing_id(self) -> None:
        self.assertEqual(todo.main(["done", "99"]), 1)

    def test_rm_removes_item(self) -> None:
        todo.main(["add", "a"])
        todo.main(["add", "b"])
        self.assertEqual(todo.main(["rm", "1"]), 0)
        data = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual([i["id"] for i in data["items"]], [2])

    def test_corrupted_file_kept(self) -> None:
        self.data_file.write_text("{ not json", encoding="utf-8")
        self.assertEqual(todo.main(["list"]), 2)
        self.assertEqual(self.data_file.read_text(encoding="utf-8"), "{ not json")

    def test_save_is_atomic(self) -> None:
        data = {"version": 1, "next_id": 2, "items": [{"id": 1, "text": "x", "done": False}]}
        storage.save(data)
        self.assertEqual(storage.load()["items"][0]["text"], "x")


if __name__ == "__main__":
    unittest.main()
<<<END>>>

<<<FILE: README.md>>>
# todo-demo

极简命令行待办工具，仅使用 Python 标准库。

## 用法

```bash
python todo.py add "写周报"
python todo.py list
python todo.py list --all
python todo.py done 1
python todo.py rm 1
```

数据保存在 `~/.todo.json`，可用环境变量 `TODO_FILE` 指定其它路径。

## 退出码

- 0 成功
- 1 用户输入错误（空内容、ID 不存在）
- 2 数据文件读写或解析失败

## 测试

```bash
python test_todo.py
```
<<<END>>>
'''


TEST_MOCK = '''\
<<<FILE: tests/test_cli.py>>>
"""针对设计接口的黑盒测试（离线演示样例）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage  # noqa: E402
import todo  # noqa: E402


class CliTest(unittest.TestCase):
    """每个用例使用独立临时数据文件。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_file = Path(self.tmp.name) / "todo.json"
        os.environ["TODO_FILE"] = str(self.data_file)

    def tearDown(self) -> None:
        os.environ.pop("TODO_FILE", None)

    def test_add_creates_item(self) -> None:
        self.assertEqual(todo.main(["add", "写周报"]), 0)
        data = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["items"]), 1)
        self.assertFalse(data["items"][0]["done"])

    def test_add_empty_rejected(self) -> None:
        self.assertEqual(todo.main(["add", "   "]), 1)

    def test_done_missing_id_returns_1(self) -> None:
        self.assertEqual(todo.main(["done", "42"]), 1)

    def test_done_marks_item(self) -> None:
        todo.main(["add", "买菜"])
        self.assertEqual(todo.main(["done", "1"]), 0)
        data = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertTrue(data["items"][0]["done"])

    def test_corrupted_file_kept(self) -> None:
        self.data_file.write_text("{ broken", encoding="utf-8")
        self.assertEqual(todo.main(["list"]), 2)
        self.assertEqual(self.data_file.read_text(encoding="utf-8"), "{ broken")

    def test_storage_roundtrip(self) -> None:
        payload = {"version": 1, "next_id": 2, "items": [{"id": 1, "text": "x", "done": False}]}
        storage.save(payload)
        self.assertEqual(storage.load()["items"][0]["text"], "x")


if __name__ == "__main__":
    unittest.main()
<<<END>>>
'''

REVIEW_MOCK_FAIL = """\
> 离线演示模式：本轮结论为模拟，用于展示「审查未通过 → 返工」的循环分支。

## 审查结论

FAIL

## 问题清单

| 编号 | 级别 | 文件 | 问题 | 修改建议 |
| --- | --- | --- | --- | --- |
| 1 | P0 | todo.py | [演示] 模拟一个 P0 问题以触发返工循环，真实模式下这里是实际缺陷 | 按问题清单逐条修复后重新生成 |
| 2 | P2 | README.md | 未说明退出码含义 | 补充退出码表格 |

## 亮点

- 存储层与命令层分离清晰，写入使用临时文件 + os.replace 保证原子性。

## 下一轮重点

1. 修复上表 P0 问题。
"""

REVIEW_MOCK_PASS = """\
> 离线演示模式：本轮结论为模拟。

## 审查结论

PASS

## 问题清单

| 编号 | 级别 | 文件 | 问题 | 修改建议 |
| --- | --- | --- | --- | --- |
| 1 | P2 | README.md | 未说明退出码含义 | 补充退出码表格 |

## 亮点

- 需求 FR-1 ~ FR-4 全部实现，读写路径做了输入校验与异常处理。
- 测试覆盖了正常与异常分支。

## 下一轮重点

无
"""


class MockClient:
    """满足 ChatClient 协议的离线实现。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self._review_calls = 0

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tag: str = "call",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if self.delay > 0:
            time.sleep(self.delay)

        if tag == "requirement":
            text = REQUIREMENT_MOCK
        elif tag == "design":
            text = DESIGN_MOCK
        elif tag == "code":
            text = CODE_MOCK
        elif tag == "test":
            text = TEST_MOCK
        elif tag == "review":
            # 第一轮故意判 FAIL，第二轮 PASS：用于演示返工循环分支
            self._review_calls += 1
            text = REVIEW_MOCK_PASS if self._review_calls >= 2 else REVIEW_MOCK_FAIL
        else:
            text = f"> Mock 响应：未识别的阶段 {tag}\n"

        return LLMResponse(
            text=text,
            usage=Usage(
                len(str(messages)) // 4,
                len(text) // 4,
                len(str(messages)) // 4 + len(text) // 4,
            ),
            finish_reason="stop",
            elapsed=self.delay,
            attempts=1,
            model="mock-model",
            request_id=f"mock-{tag}",
        )
