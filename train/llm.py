"""训练侧的模型客户端：读宿主自己的模型配置来说话。

**为什么读宿主的配置**：插件在宿主进程里可以用 ``ctx.llm.generate`` 这个官方能力，
而训练擂台是独立进程，拿不到那个上下文。它又是"AI 定战术"的实验场，必须能调模型。
所以这里直接读宿主那份 ``config/model_config.toml``（服务商地址 + 密钥 + 任务到模型的映射），
用同一个模型、同一份配额说话——不新增第二套凭据，也不改宿主配置（只读）。

请求本身走 :func:`duel.netguard.guarded_request`：只允许 http/https、默认只连公网地址
（自建的内网模型服务用 ``allow_private_host`` 显式打开）、限制跳数与响应体积。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import json
import logging
import time
import tomllib

from duel.netguard import UnsafeUrlError, guarded_request

# 插件位于 <宿主根>/plugins/<插件名>/train/llm.py，所以宿主根是上溯三层
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOST_ROOT = PLUGIN_ROOT.parent.parent

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_TOKENS = 200
# 模型响应最多读这么多，避免异常响应把内存吃满
MAX_RESPONSE_BYTES = 1024 * 1024
# 限流与临时故障的重试：擂台会并发好几局，每局每回合都问一次模型，
# 实测并发 6 时被服务商限流（HTTP 429）打回上百次，退回规则教练等于白花这批数据
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.8


class ModelError(RuntimeError):
    """调用模型失败（配置缺失、网络错误、返回无法解析）时抛出。"""


@dataclass(frozen=True)
class ModelTarget:
    """一次调用需要的全部信息：地址、密钥、模型标识符。"""

    provider: str
    base_url: str
    api_key: str
    model: str
    client_type: str = "openai"


def load_model_config(config_path: Optional[Path] = None) -> Dict:
    """读宿主的 model_config.toml；找不到时抛 :class:`ModelError`。"""

    path = Path(config_path) if config_path else HOST_ROOT / "config" / "model_config.toml"
    if not path.is_file():
        raise ModelError(f"找不到宿主模型配置：{path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModelError(f"读模型配置失败：{exc}") from exc


def available_models(config: Optional[Dict] = None) -> List[str]:
    """列出配置里可用的模型别名（``models[].name``）。"""

    data = config if config is not None else load_model_config()
    return [str(item.get("name") or "") for item in data.get("models", []) if item.get("name")]


def pick_model(
    *,
    task: str = "utils",
    name: str = "",
    config: Optional[Dict] = None,
) -> ModelTarget:
    """挑一个模型：显式给了 ``name`` 就用它，否则用任务配置里 ``model_list`` 的第一个。

    Args:
        task: 任务名（对应 ``model_task_config`` 里的键，例如 utils / planner）。
        name: 模型别名（``models[].name``）；留空则按任务配置挑。
    """

    data = config if config is not None else load_model_config()
    models = {str(item.get("name")): item for item in data.get("models", []) if item.get("name")}
    providers = {str(item.get("name")): item for item in data.get("api_providers", [])}

    if not name:
        task_conf = (data.get("model_task_config") or {}).get(task) or {}
        candidates = task_conf.get("model_list") or []
        if not candidates:
            raise ModelError(f"任务 {task} 没有配置模型（model_list 为空）")
        name = str(candidates[0])

    model = models.get(name)
    if model is None:
        raise ModelError(f"配置里没有名为 {name} 的模型；可用：{sorted(models)}")
    provider_name = str(model.get("api_provider") or "")
    provider = providers.get(provider_name)
    if provider is None:
        raise ModelError(f"模型 {name} 指向的服务商 {provider_name} 不存在")
    api_key = str(provider.get("api_key") or "")
    if not api_key:
        raise ModelError(f"服务商 {provider_name} 没配 api_key")
    return ModelTarget(
        provider=provider_name,
        base_url=str(provider.get("base_url") or "").rstrip("/"),
        api_key=api_key,
        model=str(model.get("model_identifier") or name),
        client_type=str(provider.get("client_type") or "openai"),
    )


class ModelClient:
    """极简的 OpenAI 兼容聊天客户端（只支持 openai 类型的服务商）。

    Args:
        target: 调用目标。
        timeout: 单次请求超时（秒）。
        allow_private_host: 是否允许服务商地址在内网/本机（自建模型服务）。
        logger: 日志器。
    """

    def __init__(
        self,
        target: ModelTarget,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        allow_private_host: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._target = target
        self._timeout = timeout
        self._allow_private_host = allow_private_host
        self._logger = logger or logging.getLogger(__name__)

    @property
    def target(self) -> ModelTarget:
        """当前调用目标（日志用）。"""

        return self._target

    def chat(
        self,
        prompt: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.3,
    ) -> str:
        """发一条消息，返回模型输出文本。

        限流（429）与 5xx 会短暂重试：并发跑对局时每个回合都要问一次模型，
        被限流打回就整回合退回规则教练，那批实验数据就不能算在模型头上了。

        Raises:
            ModelError: 配置/网络/解析出问题时抛出（重试次数用尽也算）。
        """

        if self._target.client_type != "openai":
            raise ModelError(f"暂不支持 client_type={self._target.client_type} 的服务商")
        payload = {
            "model": self._target.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body = self._request_with_retry(json.dumps(payload).encode("utf-8"))

        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"返回内容无法解析：{body[:200]!r}") from exc
        if not content or not str(content).strip():
            # 空回必须说清"服务端怎么解释这次空"：finish_reason=content_filter 是内容被拦，
            # length 是额度用尽，其它则多是服务端抽风。只报"空内容"会让排查方向全靠猜
            raise ModelError(
                f"模型返回空内容（finish_reason={choice.get('finish_reason')!r}，"
                f"usage={data.get('usage')!r}）"
            )
        return str(content)

    def _request_with_retry(self, payload: bytes) -> bytes:
        """发请求，遇到限流/临时故障就退避重试。"""

        last_error: Optional[ModelError] = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return guarded_request(
                    f"{self._target.base_url}/chat/completions",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._target.api_key}",
                    },
                    allow_private_host=self._allow_private_host,
                    timeout=self._timeout,
                    max_bytes=MAX_RESPONSE_BYTES,
                )
            except UnsafeUrlError as exc:
                raise ModelError(f"服务商地址不合规：{exc}") from exc
            except OSError as exc:
                last_error = ModelError(f"请求失败：{exc}")
                if not self._retryable(exc) or attempt == RETRY_ATTEMPTS - 1:
                    raise last_error
            # 指数退避：0.8s、1.6s……最多再等两秒多，不至于把对局拖垮
            time.sleep(RETRY_BASE_DELAY * (2**attempt))
        raise last_error or ModelError("请求失败")

    @staticmethod
    def _retryable(exc: OSError) -> bool:
        """这个错误值不值得重试：限流与 5xx 值得，其它（如参数错）立刻暴露。"""

        status = getattr(exc, "code", None)
        return isinstance(status, int) and status in RETRY_STATUSES
