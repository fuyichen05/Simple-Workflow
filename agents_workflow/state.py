"""Workflow 共享状态。

所有 Agent 共享同一份状态对象，Agent 之间以此交换"确定的事实"：
当前轮次、各节点执行结果、产物位置、审查结论。

与消息的区别：
    Message  一次性的、有方向的通信事件（A 告诉 B 什么）
    State    持续存在的、全局可读的事实快照（现在是什么情况）

状态每变更一次即落盘 state.json（原子写），因此进程被 kill 后可以 --resume 续跑。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .artifacts import RunContext

STATE_FILE = "state.json"
STATE_VERSION = 1

# 节点状态机
NODE_PENDING = "pending"
NODE_RUNNING = "running"
NODE_DONE = "done"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"


class StateError(Exception):
    """状态读写失败。"""


class WorkflowState:
    """可持久化的共享状态。"""

    def __init__(
        self,
        run: RunContext,
        *,
        logger=None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.run = run
        self.logger = logger
        self._lock = threading.RLock()
        self.data: dict[str, Any] = data or self._blank()

    # ------------------------------------------------------------------ 构造

    def _blank(self) -> dict[str, Any]:
        now = _now()
        return {
            "version": STATE_VERSION,
            "run_id": self.run.run_id,
            "workflow": "",
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "requirement": "",
            "requirement_chars": 0,
            "nodes": {},
            "artifacts": {},
            "counters": {},
            "review": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "next_node": None,
            "error": None,
            "history": [],
        }

    @classmethod
    def load(cls, run: RunContext, *, logger=None) -> "WorkflowState | None":
        """从磁盘恢复状态；不存在或损坏返回 None。"""
        path = run.path(STATE_FILE)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return None
        state = cls(run, logger=logger, data=data)
        state.data.setdefault("history", [])
        return state

    # ------------------------------------------------------------------ 读写

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value

    def bump(self, counter: str, delta: int = 1) -> int:
        """自增计数器并返回新值（用于循环轮次）。"""
        with self._lock:
            counters = self.data.setdefault("counters", {})
            counters[counter] = int(counters.get(counter, 0)) + delta
            return counters[counter]

    def counter(self, name: str) -> int:
        return int(self.data.get("counters", {}).get(name, 0))

    # ------------------------------------------------------------------ 节点

    def node(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.data.get("nodes", {}).get(name, {}))

    def mark_node(self, name: str, status: str, **extra: Any) -> None:
        """记录节点状态，并追加一条历史。"""
        with self._lock:
            nodes = self.data.setdefault("nodes", {})
            record = nodes.setdefault(name, {})
            record["status"] = status
            record["updated_at"] = _now()
            record.update(extra)
            if status in (NODE_DONE, NODE_FAILED, NODE_SKIPPED):
                self.data.setdefault("history", []).append(
                    {"node": name, "status": status, "at": record["updated_at"]}
                )
        self.save()

    def is_done(self, name: str) -> bool:
        return self.node(name).get("status") == NODE_DONE

    def reset_nodes(self, names: list[str] | tuple[str, ...], reason: str = "") -> None:
        """把节点打回 pending（循环回边重跑时用）。"""
        with self._lock:
            nodes = self.data.setdefault("nodes", {})
            for name in names:
                record = nodes.setdefault(name, {})
                record["status"] = NODE_PENDING
                record["reset_reason"] = reason
                record["updated_at"] = _now()
        self.save()

    # ------------------------------------------------------------------ 产物

    def put_text(self, key: str, rel_path: str, text: str) -> dict[str, Any]:
        """把文本产物落盘并登记到状态。"""
        size = self.run.write_text(rel_path, text)
        entry = {
            "type": "text",
            "path": rel_path,
            "chars": len(text),
            "bytes": size,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        }
        with self._lock:
            self.data.setdefault("artifacts", {})[key] = entry
        self.save()
        return entry

    def put_files(self, key: str, paths: list[str]) -> dict[str, Any]:
        """登记一组文件产物（代码、测试）。"""
        entry = {"type": "files", "paths": list(paths), "count": len(paths)}
        with self._lock:
            self.data.setdefault("artifacts", {})[key] = entry
        self.save()
        return entry

    def artifact(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self.data.get("artifacts", {}).get(key)
            return dict(entry) if entry else None

    def text(self, key: str, default: str = "") -> str:
        """读取文本产物内容（不存在返回 default）。"""
        entry = self.artifact(key)
        if not entry or entry.get("type") != "text":
            return default
        path = self.run.path(str(entry.get("path", "")))
        if not path.is_file():
            return default
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return default

    def files(self, key: str) -> list[str]:
        entry = self.artifact(key)
        if not entry or entry.get("type") != "files":
            return []
        return list(entry.get("paths") or [])

    # ------------------------------------------------------------------ 用量

    def add_usage(self, usage: dict[str, int]) -> None:
        with self._lock:
            total = self.data.setdefault(
                "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            )
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total[field_name] = int(total.get(field_name, 0)) + int(usage.get(field_name, 0) or 0)

    # ------------------------------------------------------------------ 持久化

    def save(self) -> None:
        """原子写入 state.json。整个写入过程持锁，避免并行分支互相覆盖。"""
        with self._lock:
            self.data["updated_at"] = _now()
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            target = self.run.path(STATE_FILE)
            tmp_name = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=str(target.parent), delete=False, suffix=".tmp"
                ) as fh:
                    tmp_name = fh.name
                    fh.write(payload)
                    fh.flush()
                os.replace(tmp_name, target)
            except OSError as exc:
                if tmp_name and os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                raise StateError(f"state.json 写入失败: {exc}") from exc

    # ------------------------------------------------------------------ 展示

    def summary_rows(self) -> list[dict[str, Any]]:
        """节点状态表，用于终端与 SUMMARY 展示。"""
        rows: list[dict[str, Any]] = []
        for name, record in self.data.get("nodes", {}).items():
            rows.append(
                {
                    "node": name,
                    "status": record.get("status", NODE_PENDING),
                    "elapsed": record.get("elapsed_sec"),
                    "attempts": record.get("attempts", 1),
                    "output": record.get("output", ""),
                }
            )
        return rows


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_state_file(run_dir: Path) -> dict[str, Any] | None:
    """不构造对象、直接读 state.json（CLI 列表用）。"""
    path = Path(run_dir) / STATE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
