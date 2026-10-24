"""DeepSeek 对话补全客户端。

设计要点：
1. 传输层双后端：装了 requests 就用 requests，否则用标准库 urllib。
   两条路径对外行为完全一致，保证 `python run.py` 在任何环境都能跑。
2. 可重试错误（网络、429、5xx）按指数退避重试；配置类错误（400/401/403）
   立即失败，避免无意义等待。
3. 每次调用的请求与响应原文落盘到 runs/<id>/llm/，便于事后审计。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

try:  # 可选依赖
    import requests
except ImportError:  # pragma: no cover - 取决于环境
    requests = None  # type: ignore[assignment]

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
BACKOFF_BASE = 1.6
BACKOFF_CAP = 30.0

USER_AGENT = "agents-workflow/1.0 (+python)"


class LLMError(Exception):
    """LLM 调用失败。retryable 表示是否属于可重试错误。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class LLMResponse:
    text: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    elapsed: float = 0.0
    attempts: int = 1
    model: str = ""
    request_id: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def transport_name() -> str:
    """当前使用的 HTTP 后端名称。"""
    return "requests" if requests is not None else "urllib"


class DeepSeekClient:
    """OpenAI 兼容的对话补全客户端。"""

    def __init__(
        self,
        cfg: Config,
        dump_dir: Path | None = None,
        logger=None,
    ) -> None:
        self.cfg = cfg
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self.logger = logger
        self._seq = 0
        if self.dump_dir is not None:
            self.dump_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 公开

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tag: str = "call",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """发起一次对话补全，失败时按策略重试。"""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        # deepseek-reasoner 不接受 temperature/top_p 等采样参数，传了会报 400
        if "reasoner" not in self.cfg.model.lower():
            payload["temperature"] = self.cfg.temperature if temperature is None else temperature
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        url = self.cfg.endpoint

        attempts = 0
        last_error: LLMError | None = None
        started = time.monotonic()

        while attempts <= self.cfg.max_retries:
            attempts += 1
            try:
                status, raw_text = self._post(url, payload, headers)
                if status >= 400:
                    raise _error_from_response(status, raw_text)
                response = self._parse_success(
                    raw_text,
                    elapsed=time.monotonic() - started,
                    attempts=attempts,
                )
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempts > self.cfg.max_retries:
                    break
                self._sleep_before_retry(attempts, exc)
                continue

            self._dump(tag, payload, raw_text, response)
            return response

        elapsed = time.monotonic() - started
        self._dump_error(tag, payload, last_error, attempts, elapsed)
        assert last_error is not None
        raise LLMError(
            f"{last_error}（重试 {attempts - 1} 次后仍失败，耗时 {elapsed:.1f}s）",
            status=last_error.status,
            retryable=False,
        )

    # ------------------------------------------------------------------ 传输

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        """返回 (status_code, response_text)。网络层异常统一转 LLMError。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timeout = self.cfg.timeout

        if requests is not None:
            try:
                resp = requests.post(url, data=body, headers=headers, timeout=(15, timeout))
                return resp.status_code, resp.text
            except requests.exceptions.Timeout as exc:  # type: ignore[union-attr]
                raise LLMError(f"请求超时（{timeout:.0f}s）: {exc}", retryable=True) from exc
            except requests.exceptions.ConnectionError as exc:  # type: ignore[union-attr]
                raise LLMError(f"网络连接失败: {exc}", retryable=True) from exc
            except requests.exceptions.RequestException as exc:  # type: ignore[union-attr]
                raise LLMError(f"HTTP 请求异常: {exc}", retryable=True) from exc

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return int(exc.code), text
        except socket.timeout as exc:
            raise LLMError(f"请求超时（{timeout:.0f}s）: {exc}", retryable=True) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"网络连接失败: {exc.reason}", retryable=True) from exc
        except OSError as exc:
            raise LLMError(f"网络 IO 异常: {exc}", retryable=True) from exc

    # ------------------------------------------------------------------ 解析

    def _parse_success(self, raw_text: str, *, elapsed: float, attempts: int) -> LLMResponse:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            # 网关返回 HTML 错误页时也会走到这里，允许重试一次
            raise LLMError(
                f"响应不是合法 JSON（前 200 字符）: {raw_text[:200]!r}", retryable=True
            ) from exc

        if not isinstance(data, dict):
            raise LLMError(f"响应结构异常: {type(data).__name__}", retryable=True)

        if "error" in data and data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError(f"接口返回错误: {message}", retryable=False)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("响应缺少 choices 字段", retryable=True)

        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if not isinstance(text, str) or not text.strip():
            raise LLMError("模型返回空内容", retryable=True)

        return LLMResponse(
            text=text,
            usage=_extract_usage(data),
            finish_reason=str(choice.get("finish_reason") or ""),
            elapsed=elapsed,
            attempts=attempts,
            model=str(data.get("model") or self.cfg.model),
            request_id=str(data.get("id") or ""),
        )

    # ------------------------------------------------------------------ 落盘

    def _dump(self, tag: str, payload: dict[str, Any], raw_text: str, response: LLMResponse) -> None:
        if self.dump_dir is None:
            return
        self._seq += 1
        record = {
            "seq": self._seq,
            "tag": tag,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "transport": transport_name(),
            "endpoint": self.cfg.endpoint,
            "request": {
                "model": payload.get("model"),
                "temperature": payload.get("temperature"),
                "max_tokens": payload.get("max_tokens"),
                "messages": payload.get("messages"),
            },
            "response": {
                "finish_reason": response.finish_reason,
                "usage": response.usage.as_dict(),
                "elapsed_sec": round(response.elapsed, 3),
                "attempts": response.attempts,
                "request_id": response.request_id,
                "content": response.text,
            },
        }
        self._write_dump(tag, record)

    def _dump_error(
        self,
        tag: str,
        payload: dict[str, Any],
        error: LLMError | None,
        attempts: int,
        elapsed: float,
    ) -> None:
        if self.dump_dir is None:
            return
        self._seq += 1
        record = {
            "seq": self._seq,
            "tag": tag,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "transport": transport_name(),
            "endpoint": self.cfg.endpoint,
            "request": {
                "model": payload.get("model"),
                "temperature": payload.get("temperature"),
                "max_tokens": payload.get("max_tokens"),
                "messages": payload.get("messages"),
            },
            "error": {
                "message": str(error) if error else "unknown",
                "status": error.status if error else None,
                "attempts": attempts,
                "elapsed_sec": round(elapsed, 3),
            },
        }
        self._write_dump(f"{tag}_error", record)

    def _write_dump(self, tag: str, record: dict[str, Any]) -> None:
        assert self.dump_dir is not None
        safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)
        name = f"{self._seq:02d}_{safe_tag}.json"
        try:
            (self.dump_dir / name).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 落盘失败不应影响主流程

    # ------------------------------------------------------------------ 重试

    def _sleep_before_retry(self, attempts: int, error: LLMError) -> None:
        delay = min(BACKOFF_CAP, BACKOFF_BASE ** attempts)
        # 加抖动，避免多进程同时重试形成尖峰
        delay += (attempts * 0.137) % 0.5
        if self.logger is not None:
            self.logger.warn(f"第 {attempts} 次调用失败（{error}），{delay:.1f}s 后重试")
        time.sleep(delay)


# ---------------------------------------------------------------------- 工具


def _extract_usage(data: dict[str, Any]) -> Usage:
    raw = data.get("usage") or {}
    if not isinstance(raw, dict):
        return Usage()
    prompt = _as_int(raw.get("prompt_tokens"))
    completion = _as_int(raw.get("completion_tokens"))
    total = _as_int(raw.get("total_tokens")) or (prompt + completion)
    return Usage(prompt, completion, total)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _error_from_response(status: int, raw_text: str) -> LLMError:
    """把 HTTP 错误响应转成可读的 LLMError，并判定是否可重试。"""
    detail = _extract_error_message(raw_text)
    retryable = status in RETRYABLE_STATUS

    hints = {
        400: "请求参数不合法（常见原因：max_tokens 超限或 messages 为空）",
        401: "API Key 无效或未生效，请检查 DEEPSEEK_API_KEY",
        402: "账户余额不足，请到 DeepSeek 控制台充值",
        403: "无访问权限，请确认 Key 的权限范围",
        404: "接口地址不存在，请检查 DEEPSEEK_BASE_URL",
        422: "请求实体无法处理，请检查参数",
        429: "触发限流，请降低频率后重试",
        500: "服务端内部错误",
        502: "网关或上游服务不可用",
        503: "服务端繁忙",
        504: "网关超时",
    }
    hint = hints.get(status, "接口返回异常状态码")
    message = f"HTTP {status} - {hint}"
    if detail:
        message += f"：{detail}"
    return LLMError(message, status=status, retryable=retryable)


def _extract_error_message(raw_text: str) -> str:
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        snippet = (raw_text or "").strip().replace("\n", " ")
        return snippet[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:300]
        if isinstance(err, str):
            return err[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return ""
