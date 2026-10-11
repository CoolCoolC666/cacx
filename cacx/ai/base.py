# -*- coding: utf-8 -*-
"""
AI 适配层
================================================================
设计原则
    · **云端优先，本地可切**：settings['ai']['provider'] 一处切换；
      也可给单个 task 单独指定 provider/model。
    · **配置可保存**：所有改动写回项目自己的 config/settings.json，
      不动任何第三方工具的原配置。
    · **密钥全程掩码**：show / 日志 / 异常都不回显完整 key。
    · **翻译/生成类调用默认只预览**（dry_run=True），要真发请求得显式
      dry_run=False —— 防止一次误触就烧掉配额。
"""

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests


class AIError(RuntimeError):
    """
    AI 调用失败。

    ⚠ 带 `retryable` 是这次新增的关键字段。
      中途中断处理的第一步不是「重试」，而是**先分清该不该重试**：
        · 可重试（retryable=True）：429 限流、5xx、连接超时、网络抖动
          —— 供应商侧/链路侧的临时问题，等一下再来一次通常就成了。
        · 不可重试（retryable=False）：401/403 鉴权失败、400 参数错、
          配置缺失、模型不存在 —— 重试一万次也是同样结果，
          只会白等并烧限流额度。
      以前两者都被压成一句 `AIError(f"HTTP {code}: ...")`，
      调用方无法判断，于是只能「整批重跑」—— 既浪费又掩盖了真实病因。
    """

    def __init__(self, message: str, retryable: bool = False,
                 status_code: int = 0):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = int(status_code or 0)


# 供应商侧的「等一下就好」信号码。
# 429 限流必须尊重 Retry-After；503/502/504 常见于过载或网关抖动。
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# 鉴权/参数类：重试无意义，且继续打可能被风控。
_FATAL_STATUS = {400, 401, 403, 404, 422}


def classify_status(code: int) -> bool:
    """按 HTTP 码判断是否值得重试。未知码**不重试** —— 宁可漏重试，
    也不要对一个必然失败的请求反复敲门（会触发风控）。"""
    if code in _RETRYABLE_STATUS:
        return True
    if code in _FATAL_STATUS:
        return False
    # 2xx/3xx 在这里不可能出现（调用方已先判 !=200）
    return False


def _retry_after_seconds(headers: Optional[Dict[str, str]], default: float) -> float:
    """优先听服务器的 Retry-After，别自顾自地重试。
    限流场景下，无视服务端指引的立即重试几乎必然再撞一次 429。"""
    if not headers:
        return default
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if not ra:
        return default
    try:
        return max(0.0, min(float(ra), 120.0))
    except (TypeError, ValueError):
        return default


def post_with_retry(url: str, *, headers: Optional[Dict[str, str]] = None,
                    json_body: Optional[Dict[str, Any]] = None,
                    timeout: int = 60, attempts: int = 3,
                    base_delay: float = 1.5, on_retry=None) -> requests.Response:
    """
    带分类与指数退避的 POST。

    attempts=1 即退化为「不重试」，供 dry_run / 单篇调用沿用旧行为。
    on_retry(attempt, delay, reason) 用于把「正在重试」告诉界面 ——
    否则用户只看到进度条卡住十几秒，完全不知道系统在自救。
    """
    last: Optional[Exception] = None
    for i in range(1, max(1, attempts) + 1):
        try:
            r = requests.post(url, headers=headers, json=json_body,
                              timeout=timeout)
        except requests.RequestException as e:
            # 连接层失败（超时/DNS/连接重置）一律可重试：
            # 请求根本没送到服务端，不存在「重复提交」的副作用。
            last = AIError(f"请求失败: {type(e).__name__}", retryable=True)
            if i >= attempts:
                raise last from e
            delay = min(base_delay * (2 ** (i - 1)), 20.0)
            if on_retry:
                on_retry(i, delay, f"{type(e).__name__}")
            time.sleep(delay)
            continue
        if r.status_code == 200:
            return r
        retryable = classify_status(r.status_code)
        err = AIError(f"HTTP {r.status_code}: {r.text[:200]}",
                      retryable=retryable, status_code=r.status_code)
        if not retryable or i >= attempts:
            raise err
        delay = _retry_after_seconds(
            getattr(r, "headers", None),
            min(base_delay * (2 ** (i - 1)), 20.0))
        if on_retry:
            on_retry(i, delay, f"HTTP {r.status_code}")
        time.sleep(delay)
        last = err
    raise last or AIError("请求失败")


def _mask(v: str) -> str:
    if not v:
        return "（未设置）"
    v = str(v)
    return "*" * (len(v) - 4) + v[-4:] if len(v) > 8 else "（未设置）"


class Provider(ABC):
    """供应商适配协议"""

    name = "base"

    def __init__(self, cfg_ai: Dict):
        self.cfg = cfg_ai
        self.base_url = (cfg_ai.get("base_url") or "").rstrip("/")
        self.model = cfg_ai.get("model") or ""
        self.api_key = cfg_ai.get("api_key") or ""
        self.timeout = int(cfg_ai.get("timeout", 60))
        self.max_tokens = int(cfg_ai.get("max_tokens", 2000))
        # 重试次数与退避基数。与 config.DEFAULTS['ai'] 保持一致：**默认开启 3 次**。
        # 这里的兜底值只在调用方递进来一份没跟 DEFAULTS 合并过的 cfg 时才会生效
        # （正常路径 C.load() 已经合过了），但两处默认值必须一致 ——
        # 否则同一份配置在「走服务」和「直接构造 client」两条路上行为不同。
        # 想关掉设成 1（0 也按 1 处理：没有「重试 0 次」这种语义）。
        self.retry_attempts = max(1, int(cfg_ai.get("retry_attempts", 3)))
        self.retry_base_delay = float(cfg_ai.get("retry_base_delay", 1.5))
        # 最近一次重试的原因，供调用方呈现给用户
        self.last_retry: Optional[Dict[str, Any]] = None

    def _on_retry(self, attempt: int, delay: float, reason: str):
        self.last_retry = {"attempt": attempt, "delay": round(delay, 2),
                           "reason": reason}

    @abstractmethod
    def chat(self, system: str, user: str, dry_run: bool = False) -> str:
        ...

    def info(self) -> Dict[str, str]:
        return {"provider": self.name, "base_url": self.base_url,
                "model": self.model, "api_key": _mask(self.api_key)}


class OpenAICompat(Provider):
    """
    任何 OpenAI 兼容的云端接口（MiniMax / DeepSeek / 智谱 / OpenRouter / vLLM…）。
    base_url 填到 /v1 为止，路径自动拼 /chat/completions。
    """

    name = "openai_compat"

    def chat(self, system: str, user: str, dry_run: bool = False) -> str:
        if not self.base_url:
            raise AIError("未配置 ai.base_url")
        if not self.api_key:
            raise AIError("未配置 ai.api_key")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
        }
        if dry_run:
            return json.dumps({"_dry_run": True, "endpoint":
                               self.base_url + "/chat/completions",
                               "payload_preview": payload}, ensure_ascii=False, indent=2)
        url = self.base_url + "/chat/completions"
        r = post_with_retry(
            url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json_body=payload, timeout=self.timeout,
            attempts=self.retry_attempts, base_delay=self.retry_base_delay,
            on_retry=self._on_retry)
        try:
            d = r.json()
        except Exception as e:
            raise AIError(f"响应非 JSON: {r.text[:200]}") from e
        # 部分厂商把思维内容塞进 content 而不是 reasoning_content，
        # 这里顺手剥掉 <think>…</think> 残留，避免污染业务字段。
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return strip_think(content or "")


class Ollama(Provider):
    """本地 Ollama。base_url 形如 http://127.0.0.1:11434"""

    name = "ollama"

    def chat(self, system: str, user: str, dry_run: bool = False) -> str:
        if not self.model:
            raise AIError("未配置 ai.model（本地模型名）")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": self.max_tokens},
        }
        if dry_run:
            return json.dumps({"_dry_run": True,
                               "endpoint": self.base_url + "/api/chat",
                               "payload_preview": payload}, ensure_ascii=False, indent=2)
        url = self.base_url + "/api/chat"
        r = post_with_retry(
            url, json_body=payload, timeout=self.timeout,
            attempts=self.retry_attempts, base_delay=self.retry_base_delay,
            on_retry=self._on_retry)
        try:
            d = r.json()
        except Exception as e:
            raise AIError(f"响应非 JSON: {r.text[:200]}") from e
        return strip_think(d.get("message", {}).get("content", "") or "")


def strip_think(text: str) -> str:
    """
    剥离推理模型的思维内容。
    注意必须用 re.DOTALL 且不能加 ^ —— 有些厂商把思维塞在 content 里，
    而且不一定出现在字符串最开头（前面可能有换行或空格）。
    """
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


PROVIDERS = {"openai_compat": OpenAICompat, "ollama": Ollama, "none": None}


def build(cfg: Dict, task: Optional[str] = None) -> Optional[Provider]:
    """按配置构造 provider；task 可单独覆盖"""
    ai = cfg.get("ai", {})
    name = ai.get("provider", "none")
    model = ai.get("model", "")
    base_url = ai.get("base_url", "")
    if task:
        t = (ai.get("tasks") or {}).get(task) or {}
        name = t.get("provider") or name
        model = t.get("model") or model
    cls = PROVIDERS.get(name)
    if cls is None:
        return None
    sub = dict(ai)
    if task:
        sub["model"] = model
    if base_url:
        sub["base_url"] = base_url
    return cls(sub)


def status(cfg: Dict) -> Dict[str, Any]:
    ai = cfg.get("ai", {})
    p = build(cfg)
    return {
        "enabled": ai.get("enabled", False),
        "provider": ai.get("provider", "none"),
        "model": ai.get("model", ""),
        "base_url": ai.get("base_url", ""),
        "api_key": _mask(ai.get("api_key", "")),
        "ready": bool(p) and bool(ai.get("base_url")),
        "tasks": ai.get("tasks", {}),
    }