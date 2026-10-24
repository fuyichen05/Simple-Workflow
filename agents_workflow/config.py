"""配置加载。

优先级：命令行参数 > 进程环境变量 > 项目根目录 .env > 内置默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_RUNS_DIR = "runs"

# 项目根目录（agents_workflow 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

TRUTHY = {"1", "true", "yes", "on", "y"}
FALSY = {"0", "false", "no", "off", "n"}


class ConfigError(Exception):
    """配置非法。"""


@dataclass
class Config:
    """运行期配置。"""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    max_tokens: int = 8192
    timeout: float = 180.0
    max_retries: int = 3
    runs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / DEFAULT_RUNS_DIR)
    mock: bool = False
    quiet: bool = False
    # 是否允许并行组并发执行（--no-parallel 可关闭，便于调试）
    parallel: bool = True
    # 非致命的配置提示，启动时统一打印
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 属性

    @property
    def normalized_base_url(self) -> str:
        """补全 /v1 并去掉尾部斜杠。

        DeepSeek 官方既支持 https://api.deepseek.com 也支持
        https://api.deepseek.com/v1，两种写法都统一到 /v1。
        """
        url = self.base_url.strip().rstrip("/")
        if not url:
            url = DEFAULT_BASE_URL
        if url.endswith("/chat/completions"):
            return url
        if not url.endswith("/v1"):
            url += "/v1"
        return url

    @property
    def endpoint(self) -> str:
        """对话补全接口完整地址。"""
        return f"{self.normalized_base_url}/chat/completions"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Config":
        """从环境变量与 .env 构造配置。"""
        root = root or PROJECT_ROOT
        env = load_env(root)
        cfg = cls()
        notes: list[str] = []

        api_key = env.get("DEEPSEEK_API_KEY") or env.get("LLM_API_KEY") or ""
        cfg.api_key = api_key.strip()

        base_url = (env.get("DEEPSEEK_BASE_URL") or "").strip()
        if base_url:
            cfg.base_url = base_url

        model = (env.get("DEEPSEEK_MODEL") or "").strip()
        if model:
            cfg.model = model

        cfg.temperature = _read_float(env, "DEEPSEEK_TEMPERATURE", cfg.temperature, notes)
        cfg.max_tokens = _read_int(env, "DEEPSEEK_MAX_TOKENS", cfg.max_tokens, notes)
        cfg.timeout = _read_float(env, "DEEPSEEK_TIMEOUT", cfg.timeout, notes)
        cfg.max_retries = _read_int(env, "DEEPSEEK_MAX_RETRIES", cfg.max_retries, notes)

        runs_dir = (env.get("WORKFLOW_RUNS_DIR") or "").strip()
        if runs_dir:
            cfg.runs_dir = _abs_path(runs_dir, root)

        mock_flag = (env.get("WORKFLOW_MOCK") or "").strip().lower()
        if mock_flag in TRUTHY:
            cfg.mock = True
        elif mock_flag in FALSY:
            cfg.mock = False

        parallel_flag = (env.get("WORKFLOW_PARALLEL") or "").strip().lower()
        if parallel_flag in FALSY:
            cfg.parallel = False

        # 合理性校验
        if not 0.0 <= cfg.temperature <= 2.0:
            notes.append(f"temperature={cfg.temperature} 超出 [0, 2]，已重置为 0.3")
            cfg.temperature = 0.3
        if cfg.max_tokens <= 0:
            notes.append(f"max_tokens={cfg.max_tokens} 非法，已重置为 8192")
            cfg.max_tokens = 8192
        if cfg.timeout <= 0:
            notes.append(f"timeout={cfg.timeout} 非法，已重置为 180")
            cfg.timeout = 180.0
        if cfg.max_retries < 0:
            cfg.max_retries = 0

        cfg.notes = notes
        return cfg

    def describe(self) -> dict[str, object]:
        """用于 meta.json 的可序列化快照，绝不包含密钥明文。"""
        return {
            "base_url": self.normalized_base_url,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_sec": self.timeout,
            "max_retries": self.max_retries,
            "api_key_present": self.has_api_key,
        }


# ---------------------------------------------------------------------- 工具


def load_env(root: Path | None = None) -> dict[str, str]:
    """合并 .env 与进程环境变量，进程环境变量优先。"""
    root = root or PROJECT_ROOT
    merged = parse_env_file(root / ".env")
    for key, value in os.environ.items():
        merged[key] = value
    return merged


def parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 注释，可选的 export 前缀与引号。"""
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            data[key] = value
    return data


def _abs_path(raw: str, root: Path) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (root / p).resolve()


def _read_float(env: dict[str, str], key: str, default: float, notes: list[str]) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        notes.append(f"{key}={raw!r} 不是数字，已使用默认值 {default}")
        return default


def _read_int(env: dict[str, str], key: str, default: int, notes: list[str]) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        notes.append(f"{key}={raw!r} 不是整数，已使用默认值 {default}")
        return default
