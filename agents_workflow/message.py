"""Agent 间消息传递。

设计原则：Agent 之间**不直接调用、也不共享内存对象**，
所有协作都通过 MessageBus 投递 Message 完成。

三段式协作：
    Agent A ──publish(artifact)──► MessageBus ──inbox("B")──► Agent B
                                        │
                                        └─ messages.jsonl（可完整回放）

一条消息 = 谁发的 + 给谁的 + 什么类型 + 一句话摘要 + 结构化负载 + 正文（可截断）。
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 消息类型
KIND_ARTIFACT = "artifact"   # 交付产物（需求文档、设计文档、代码清单）
KIND_FEEDBACK = "feedback"   # 审查意见回传，驱动返工循环
KIND_STATUS = "status"       # 节点状态变更
KIND_CONTROL = "control"     # 流程控制指令（如 retry / abort）
KIND_ERROR = "error"         # 异常上报

KINDS = (KIND_ARTIFACT, KIND_FEEDBACK, KIND_STATUS, KIND_CONTROL, KIND_ERROR)

# 正文落盘上限：完整内容在产物文件里，消息里只留摘要，避免 jsonl 爆炸
MAX_CONTENT_CHARS = 4000


@dataclass
class Message:
    """一条 Agent 间消息。"""

    sender: str
    receiver: str
    kind: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: str = field(
        default_factory=lambda: _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "ts": self.ts,
            "from": self.sender,
            "to": self.receiver,
            "kind": self.kind,
            "summary": self.summary,
            "payload": self.payload,
        }
        if self.content:
            data["content"] = self.content[:MAX_CONTENT_CHARS]
            if len(self.content) > MAX_CONTENT_CHARS:
                data["truncated"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            sender=str(data.get("from", "?")),
            receiver=str(data.get("to", "?")),
            kind=str(data.get("kind", KIND_STATUS)),
            summary=str(data.get("summary", "")),
            payload=dict(data.get("payload") or {}),
            content=str(data.get("content", "")),
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            ts=str(data.get("ts") or ""),
        )

    def line(self) -> str:
        """一行式展示：from -> to [kind] summary"""
        return f"{self.sender} -> {self.receiver} [{self.kind}] {self.summary}"


class MessageBus:
    """内存队列 + JSONL 持久化的消息总线。

    线程安全：并行分支会并发 publish，内部用锁保护写入与索引。
    """

    def __init__(self, path: Path | None = None, logger=None, echo: bool = False) -> None:
        self.path = Path(path) if path else None
        self.logger = logger
        self.echo = echo
        self._lock = threading.Lock()
        self._messages: list[Message] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)

    # ------------------------------------------------------------------ 写

    def publish(self, msg: Message) -> Message:
        """投递一条消息：入内存、追加 jsonl、（可选）打日志。"""
        if msg.kind not in KINDS:
            raise ValueError(f"未知消息类型: {msg.kind}")
        with self._lock:
            self._messages.append(msg)
            if self.path is not None:
                try:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
                except OSError:
                    pass
        if self.logger is not None:
            self.logger.detail(f"MSG {msg.line()}")
        if self.echo:
            print(f"      msg {msg.line()}")
        return msg

    def emit(self, sender: str, receiver: str, kind: str, summary: str, **kwargs) -> Message:
        """便捷构造 + 投递。"""
        return self.publish(Message(sender=sender, receiver=receiver, kind=kind, summary=summary, **kwargs))

    # ------------------------------------------------------------------ 读

    def all(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def inbox(self, receiver: str, kind: str | None = None) -> list[Message]:
        """某 Agent 收到的消息，按投递顺序。"""
        with self._lock:
            return [
                m
                for m in self._messages
                if (m.receiver in (receiver, "*"))
                and (kind is None or m.kind == kind)
            ]

    def latest(self, receiver: str, kind: str | None = None) -> Message | None:
        items = self.inbox(receiver, kind)
        return items[-1] if items else None

    def replay(self, path: Path) -> list[Message]:
        """从 jsonl 载入历史消息（用于 resume 时恢复上下文）。"""
        if not path.is_file():
            return []
        loaded: list[Message] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                loaded.append(Message.from_dict(json.loads(raw)))
            except (json.JSONDecodeError, TypeError):
                continue
        with self._lock:
            self._messages = loaded
        return loaded

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)


def format_timeline(messages: Iterable[Message], limit: int = 40) -> str:
    """把消息列表渲染成缩进时间线，用于终端展示。"""
    lines: list[str] = []
    for msg in list(messages)[-limit:]:
        clock = msg.ts[11:19] if len(msg.ts) >= 19 else msg.ts
        lines.append(f"{clock}  {msg.line()}")
    return "\n".join(lines)
