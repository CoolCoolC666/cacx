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


def _flatten(node, prefix="", out=None):
    """把任意嵌套结构摊平成 {点号路径: 叶子值}"""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            _flatten(v, p, out)
    elif isinstance(node, list):
        for i, x in enumerate(node):
            _flatten(x, f"{prefix}[{i}]", out)
    else:
        out[prefix] = node
    return out


# 明显不是 AI 配置的字段名，避免误抓
_NOT_AI = ("proxy", "img", "css", "icon", "favicon", "avatar", "font",
           "path", "file", "dir", "url_encode", "callback")
# URL 值里一眼不是 API 端点的
_NOT_URL_SUFFIX = (".png", ".jpg", ".svg", ".ico", ".css", ".woff", ".ttf")


def _looks_like_endpoint(v: str) -> bool:
    if not v:
        return False
    s = v.strip().lower()
    if not (s.startswith("http://") or s.startswith("https://")):
        return False
    if any(s.endswith(x) for x in _NOT_URL_SUFFIX):
        return False
    # 光一个站点首页不算 API 端点，至少要带一层路径
    tail = s.split("://", 1)[1].split("/", 1)[1] if "/" in s.split("://", 1)[1] else ""
    return bool(tail)


def scan_credentials(src) -> dict:
    """
    从任意一份 JSON 里**找出** AI 相关设置，返回候选清单（不写盘）。

    支持的来源形态（实测的差异不是小事，光认扁平字段名会一个都抓不到）：
      · cacx 自己的 settings.json          { "ai": { "api_key": ... } }
      · 扁平工具配置                         { "api_key": ..., "base_url": ... }
      · MCP 配置                             { "mcpServers": { "x": {
                                            "env": { "MINIMAX_API_KEY": "..." },
                                            "headers": { "Authorization": "Bearer ..." },
                                            "url": "...", "args": [...] } } }
      · 嵌套 provider                       { "llm": { "default": { "api_key": ... } } }

    返回
        {"candidates": {目标配置路径: {"value":…, "from": 来源路径,
                                      "masked": 掩码值}},
         "sources":    [(来源文件/段落, 命中的路径…)],
         "warnings":   [提示…]}
    **value 会原样带回**（调用方要写盘），但绝不进任何日志或错误信息。
    """
    flat = _flatten(src)
    hits: dict = {}

    def take(target, path, value, why):
        if not isinstance(value, str) or not value.strip():
            return
        hits[target] = {"value": value.strip(), "from": path,
                        "masked": mask_secret(value.strip()),
                        "why": why}

    # ---- 1) 显式字段名（后缀匹配，不要求精确相等）----
    # 用 endswith 而不是 == ：MCP/各家工具普遍给密钥变量加前缀，
    # 如 MY_API_KEY、OPENAI_API_KEY、DEEPSEEK_KEY，精确匹配一个都认不出。
    for path, val in flat.items():
        low = path.lower()
        if any(n in low for n in _NOT_AI):
            continue
        leaf = low.rsplit(".", 1)[-1].split("[")[0]
        if leaf.endswith("authorization"):
            v = val.strip()
            if isinstance(v, str) and v.lower().startswith("bearer "):
                take("ai.api_key", path, v[7:], "Authorization: Bearer <token>")
            continue
        if leaf.endswith(("api_key", "apikey", "access_key")) or \
           leaf.endswith(("_token", "secret", "password")):
            # 光叫 "key" / "token" 的太容易误伤（图床、缓存），要求长度像密钥
            if not isinstance(val, str) or len(val.strip()) < 16:
                continue
            if not any(n in leaf for n in ("key", "token", "secret", "password")):
                continue
            take("ai.api_key", path, val, f"字段名 {leaf}")
        elif leaf.endswith(("base_url", "baseurl", "api_base", "endpoint",
                            "api_endpoint", "server_url", "api_host")) or \
                (leaf.endswith("url") and _looks_like_endpoint(val)):
            if isinstance(val, str) and _looks_like_endpoint(val):
                take("ai.base_url", path, val, f"字段名 {leaf}")
        elif leaf.endswith(("model_name", "default_model", "chat_model")) or \
                leaf == "model":
            take("ai.model", path, val, f"字段名 {leaf}")

    # ---- 2) MCP 特有形态：env 里的大写变量名 ----
    #     { "env": { "MINIMAX_API_KEY": "..." } } —— 叶子名是 MINIMAX_API_KEY，
    #     上面那轮认不出（既不等于 api_key 也不等于 key）
    #     顶层 "env" 键也要算 —— MCP 允许 servers.<name>.env 或顶层 env，
    #     只认 ".env." 会漏掉顶层写法。
    for path, val in flat.items():
        low = path.lower()
        parts = low.split(".")
        in_env = "env" in parts[:-1] or low == "env" or ".env." in low
        if not in_env:
            continue
        if not isinstance(val, str) or len(val.strip()) < 16:
            continue
        name = parts[-1]
        if any(k in name for k in ("api_key", "apikey", "token", "secret",
                                   "access_key")):
            if "ai.api_key" not in hits:      # 显式字段优先
                take("ai.api_key", path, val, "env 里的密钥变量")
        elif any(k in name for k in ("api_host", "base_url", "endpoint")):
            if _looks_like_endpoint(val) and "ai.base_url" not in hits:
                take("ai.base_url", path, val, "env 里的地址变量")

    # ---- 3) MCP 的 args 里可能带 --api-key ----
    for path, val in flat.items():
        if "args" not in path.lower() or not isinstance(val, str):
            continue
        for tok in val.split():
            if tok.startswith("--") and "=" in tok:
                k, _, v = tok[2:].partition("=")
                if "key" in k.lower() and len(v) >= 16:
                    take("ai.api_key", path, v, f"命令行参数 --{k}")
                elif "base" in k.lower() or "url" in k.lower():
                    if _looks_like_endpoint(v):
                        take("ai.base_url", path, v, f"命令行参数 --{k}")

    warnings = []
    if not hits:
        warnings.append("没能识别出 API 设置。常见原因："
                        "文件不是 JSON、或字段名不含 key/url/model 之类关键词。")
    if "ai.base_url" not in hits and "ai.api_key" in hits:
        warnings.append("只找到密钥、没有接口地址。填 base_url 才知道往哪发请求。")
    if "ai.api_key" not in hits and "ai.base_url" in hits:
        warnings.append("只找到接口地址、没有密钥。")
    return {"candidates": hits, "warnings": warnings}


def apply_candidates(cands: dict, fields=("ai.api_key", "ai.base_url",
                                          "ai.model")) -> tuple:
    """把扫描结果写进 settings.json。返回 (改了哪些字段, 错误信息)"""
    cfg = load()
    changed = []
    for f in fields:
        if f in cands:
            old = get_path(cfg, f)
            new = cands[f]["value"]
            if old != new:
                set_path(cfg, f, new)
                changed.append(f)
    if changed:
        save(cfg)
    return changed, None


def import_from(external: Path) -> dict:
    """
    从外部配置文件导入（命令行入口，保持原有返回签名）。
    网页端请走 POST /api/config/import，能先预览再决定要不要写。
    """
    external = Path(external)
    if not external.exists():
        raise FileNotFoundError(f"配置文件不存在: {external}")
    src = json.loads(external.read_text(encoding="utf-8-sig"))
    cfg = load()

    # 情形 1：本身即 cacx 配置 → 整体合并
    if isinstance(src, dict) and "ai" in src:
        cfg = _deep_merge(cfg, src)
        save(cfg)
        return SETTINGS_FILE, ["识别为 cacx 配置，已整体合并"]

    res = scan_credentials(src)
    changed, _ = apply_candidates(res["candidates"])
    notes = [f'{f}  <-  {res["candidates"][f]["from"]}'
             f'（{res["candidates"][f]["why"]}）' for f in changed]
    notes += res["warnings"]
    return SETTINGS_FILE, notes


IMPORT_TEMPLATE = {
    "_说明": "把这段模板填好另存为 xxx.json，再在网页设置页粘贴其内容或导入文件。"
             "字段名可以照抄，也可以用你自己的（MCP 的 env/headers 结构同样认得）。",
    "provider": "openai_compat",
    "base_url": "https://api.example.com/v1",
    "model": "your-model-name",
    "api_key": "sk-xxxxxxxxxxxxxxxx",

    "_MCP写法示例": {
        "mcpServers": {
            "your-server": {
                "command": "npx",
                "env": {
                    "PROVIDER_API_KEY": "sk-xxxxxxxxxxxxxxxx",
                    "PROVIDER_BASE_URL": "https://api.example.com/v1"
                }
            }
        }
    },
    "_headers写法示例": {
        "servers": {
            "your-server": {
                "url": "https://api.example.com/v1",
                "headers": {"Authorization": "Bearer sk-xxxxxxxxxxxxxxxx"}
            }
        }
    },
}


def describe(cfg: dict) -> str:
    """人类可读的当前配置（密钥掩码）"""
    m = mask_config(cfg)
    return json.dumps(m, ensure_ascii=False, indent=2)