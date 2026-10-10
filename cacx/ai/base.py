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
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests


class AIError(RuntimeError):
    pass


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
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise AIError(f"请求失败: {type(e).__name__}") from e
        if r.status_code != 200:
            raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
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
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise AIError(f"请求失败: {type(e).__name__}（本地服务是否已启动？）") from e
        if r.status_code != 200:
            raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
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