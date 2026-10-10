# -*- coding: utf-8 -*-
"""
配置持久化
================================================================
设计要点
    · 配置存在项目根的 config/settings.json（gitignored），
      另附 config/settings.example.json 作为可提交模板。
    · **不动工具原配置**：AI 供应商、密钥都只写本项目的 settings.json。
    · 密钥全程掩码：`show` 子命令与任何打印都不回显完整 key。
    · 支持从别处 `import` 一次填好，避免手抄。
"""

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Dict

PKG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_DIR.parent

CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
EXAMPLE_FILE = CONFIG_DIR / "settings.example.json"
ENV_PREFIX = "CACX_"

# 需要掩码显示的字段（按路径前缀匹配）
SECRET_KEYS = ("api_key", "apikey", "token", "secret", "password")

DEFAULTS: Dict[str, Any] = {
    "app": {
        "host": "127.0.0.1",
        "port": 8770,
        "open_browser": True,
        "page_size": 20,
    },
    "db": {
        "path": "data/cacx.db",
    },
    "sites": {
        "cac": {
            "enabled": True,
            "name": "中国网信办",
            "base_url": "https://www.cac.gov.cn",
            "api_url": "https://www.cac.gov.cn/cms/JsonList",
            "date_start": "2024-01-01",
            "date_end": "2026-12-31",
            "per_page": 200,
        },
        "zjwx": {
            "enabled": True,
            "name": "浙江网信网",
            "base_url": "https://www.zjwx.gov.cn",
            "api_url": ("https://www.zjwx.gov.cn/api-gateway/"
                         "jpaas-publish-server/front/page/build/unit"),
            "date_start": "2024-01-01",
            "date_end": "2026-12-31",
            "page_size": 15,
        },
    },
    "collector": {
        "interval": 0.35,
        "timeout": 25,
        "retry": 3,
        "concurrency": 1,
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    },
    "ai": {
        "enabled": False,
        "provider": "none",          # none | openai_compat | ollama
        "base_url": "",
        "model": "",
        "api_key": "",
        "timeout": 60,
        "max_tokens": 2000,
        # 各任务可单独覆盖 provider/model，留空则用上面的默认
        "tasks": {
            "summarize": {"enabled": False, "provider": "", "model": ""},
            "tag":       {"enabled": False, "provider": "", "model": ""},
            "doctor":    {"enabled": False, "provider": "", "model": ""},
        },
    },
    "search": {
        "min_fts_chars": 3,          # 少于该长度走 LIKE 兜底（trigram 限制）
        "default_sort": "date_desc",
    },
    "corpus_root": "",              # 既有 json 语料目录，留空则用项目同级推断
}


# ---------------------------------------------------------------- 工具
def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict:
    """环境变量覆盖：CACX_AI__API_KEY=xxx → settings['ai']['api_key']"""
    out: Dict[str, Any] = {}
    for env_key, val in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = env_key[len(ENV_PREFIX):].lower().split("__")
        cur = out
        for p in path[:-1]:
            cur = cur.setdefault(p, {})
        cur[path[-1]] = val
    return out


def mask_secret(value: str, keep: int = 4) -> str:
    """密钥掩码：只留末 keep 位"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def mask_config(cfg: dict) -> dict:
    """深拷贝并把所有敏感字段替换为掩码"""
    out = copy.deepcopy(cfg)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and any(s in k.lower() for s in SECRET_KEYS):
                    if isinstance(v, str):
                        node[k] = mask_secret(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)
    walk(out)
    return out


# ---------------------------------------------------------------- 读写
def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)


def load(create: bool = False) -> dict:
    """读配置；不存在时若 create=True 则写出默认配置"""
    ensure_dirs()
    raw = {}
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    else:
        if create:
            save(DEFAULTS)
            raw = copy.deepcopy(DEFAULTS)
            EXAMPLE_FILE.write_text(
                json.dumps(mask_config(DEFAULTS), ensure_ascii=False, indent=2),
                encoding="utf-8")
    return _deep_merge(DEFAULTS, _deep_merge(raw, _env_overrides()))


def save(cfg: dict) -> Path:
    ensure_dirs()
    merged = _deep_merge(DEFAULTS, cfg)
    merged.setdefault("_meta", {})
    merged["_meta"]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    SETTINGS_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return SETTINGS_FILE


def get_path(cfg: dict, dotted: str, default=None):
    """按 'ai.api_key' 取值"""
    cur: Any = cfg
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def set_path(cfg: dict, dotted: str, value: Any):
    parts = dotted.split(".")
    cur = cfg
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def import_from(external: Path) -> dict:
    """
    从外部配置文件导入（一次性填好，不要求手抄）。
    支持两类来源：
      1) cacx 自身格式的 settings.json
      2) 其他工具的原配置（自动挑出 api_key / base_url / model 等字段）
    返回 (导入后的配置, 导入摘要)
    """
    external = Path(external)
    if not external.exists():
        raise FileNotFoundError(f"配置文件不存在: {external}")
    src = json.loads(external.read_text(encoding="utf-8-sig"))

    cfg = load()
    notes = []

    # 情形 1：本身即 cacx 配置
    if isinstance(src, dict) and "ai" in src:
        cfg = _deep_merge(cfg, src)
        notes.append("识别为 cacx 配置，已整体合并")
        return save(cfg), notes

    # 情形 2：当作外部工具原配置，挑字段
    flat = {}

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{prefix}.{k}" if prefix else k
                walk(v, p)
        elif isinstance(node, list):
            for i, x in enumerate(node):
                walk(x, f"{prefix}[{i}]")
        else:
            flat[prefix] = node

    walk(src)
    mapping = [
        (("api_key", "apikey", "key"), "ai.api_key"),
        (("base_url", "baseurl", "url", "endpoint"), "ai.base_url"),
        (("model", "model_name"), "ai.model"),
    ]
    for needles, target in mapping:
        for k, v in flat.items():
            low = k.lower()
            if isinstance(v, str) and v and any(n in low for n in needles) \
                    and not any(s in low for s in ("proxy", "img", "css")):
                set_path(cfg, target, v)
                notes.append(f"{k}  ->  {target}")
                break
    save(cfg)
    return SETTINGS_FILE, notes


def describe(cfg: dict) -> str:
    """人类可读的当前配置（密钥掩码）"""
    m = mask_config(cfg)
    return json.dumps(m, ensure_ascii=False, indent=2)