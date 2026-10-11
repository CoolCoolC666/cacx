# -*- coding: utf-8 -*-
"""
站点注册表：内置站点 + 用户自定义站点
================================================================
这个模块存在的理由是**隔离**，不是方便：

    自定义站点一律不写进 config/settings.json。
    那个文件里存着用户的 AI 密钥，历史上被反复读写，并出过两次
    「自检把用户真实密钥洗成空」的事故（详见 docs/工程留痕.md）。
    新增站点是高频操作，不该每次都去碰那个文件 ——
    万一哪次写坏了，用户丢的是密钥，不是站点列表。

所以：每个自定义站点一个独立文件 config/sites.d/<code>.json。

    · 单站写坏只影响它自己，不拖累别的站点
    · 复制/分享一个站点配置 = 复制一个文件，不用从大 JSON 里挑
    · 删站点 = 删一个文件，不存在「从大 JSON 里摘干净」的残留风险
    · gitignore 掉整个 sites.d/，别人的密钥/站点配置不会进仓库

内置站点（CAC / 浙江）标 builtin=True，**不**放进 sites.d ——
它们的采集逻辑是代码里的类，不是配置能表达的，配置文件管不了它们。
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as C

CUSTOM_DIR = C.CONFIG_DIR / "sites.d"

# 站点 code 的合法字符：字母数字下划线连字符，不允许中文与路径分隔符
_CODE_BAD = re.compile(r"[^a-z0-9_-]+")


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def make_code(url: str, name: str = "", fallback: str = "site") -> str:
    """
    由网址/名称生成一个安全的站点代码。
    优先用主机名（最稳定，也最可读），冲突时加序号。
    """
    from urllib.parse import urlparse
    host = ""
    try:
        host = urlparse(_norm_url(url)).netloc
    except Exception:
        host = ""
    base = _CODE_BAD.sub("", (host or name or fallback).lower())[:32]
    if not base or base[0].isdigit():
        base = "s_" + base if base else "site"
    return base


def unique_code(url: str, name: str = "") -> str:
    code = make_code(url, name)
    taken = set(list_custom().keys())
    if code not in taken:
        return code
    for i in range(2, 100):
        c2 = f"{code}{i}"
        if c2 not in taken:
            return c2
    return f"{code}_{int(time.time())}"


# ---------------------------------------------------------------- 读写
def ensure_dir() -> Path:
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    return CUSTOM_DIR


def site_file(code: str) -> Path:
    return CUSTOM_DIR / f"{code}.json"


def list_custom() -> Dict[str, Dict[str, Any]]:
    """
    读全部自定义站点配置。
    单个文件坏了不能让整个列表读不出来 —— 坏的记进 _errors，
    界面能看到「这个站点配置坏了」而不是「所有站点都没了」。
    """
    if not CUSTOM_DIR.exists():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for p in sorted(CUSTOM_DIR.glob("*.json")):
        try:
            spec = json.loads(p.read_text(encoding="utf-8-sig"))
            if not isinstance(spec, dict):
                raise ValueError("站点配置必须是对象")
            spec.setdefault("code", p.stem)
            spec["_file"] = str(p)
            out[spec["code"]] = spec
        except Exception as e:
            out[p.stem] = {"code": p.stem, "_broken": True,
                           "_error": f"{type(e).__name__}: {e}",
                           "_file": str(p)}
    return out


def get(code: str) -> Optional[Dict[str, Any]]:
    return list_custom().get(code)


def save_custom(spec: Dict[str, Any]) -> Path:
    """
    写一个自定义站点配置（原子替换）。
    只写 sites.d/<code>.json，**不碰 settings.json**。
    """
    code = str(spec.get("code") or "").strip()
    # 正向白名单，而不是拿「非法字符类」去反着判。
    # 两次踩坑都栽在双重否定上：
    #   · fullmatch(code) 配非法字符类 → 任何正常代码都存不进去
    #   · 换成「有非法字符才匹配」→ '../evil' 这种混合串反而匹配不上，
    #     结果路径穿越照样写进去了（更糟：写到目录外去了）
    # 白名单一次说清：只允许小写字母、数字、下划线、连字符。
    if not re.fullmatch(r"[a-z0-9_-]+", code):
        raise ValueError(f"站点代码不合法: {code!r}"
                         f"（只允许小写字母、数字、下划线、连字符）")
    ensure_dir()
    spec = dict(spec)
    spec.pop("_file", None)
    spec.pop("_broken", None)
    spec.pop("_error", None)
    spec.setdefault("enabled", True)
    spec["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = site_file(code)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)          # 同分区 rename 是原子的，避免半截文件
    return p


def delete_custom(code: str) -> bool:
    p = site_file(code)
    if p.exists():
        p.unlink()
        return True
    return False


def set_enabled(code: str, enabled: bool) -> bool:
    spec = get(code)
    if not spec or spec.get("_broken"):
        return False
    spec["enabled"] = bool(enabled)
    save_custom(spec)
    return True


# ---------------------------------------------------------------- 汇总
def builtin_specs(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """内置站点：从 settings.json 的 sites 段读，只读不写"""
    out = {}
    for code, v in (cfg.get("sites") or {}).items():
        if not isinstance(v, dict):
            continue
        out[code] = {**v, "code": code, "builtin": True,
                     "collector": "builtin"}
    return out


def all_sites(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """内置 + 自定义，后加的自定义不覆盖内置同名项"""
    out = builtin_specs(cfg)
    for code, spec in list_custom().items():
        if code in out:            # 同名：保留内置，自定义改名再添加
            spec = {**spec, "_shadowed": True}
        out[code] = {**spec, "builtin": False, "collector": "generic"}
    return out


def enabled_codes(cfg: Dict[str, Any]) -> List[str]:
    return [c for c, s in all_sites(cfg).items()
            if s.get("enabled", True) and not s.get("_broken")]


def describe(cfg: Dict[str, Any],
             counts: Optional[Dict[str, dict]] = None) -> List[Dict[str, Any]]:
    """
    给界面用的站点清单。
    counts: {site: {n, chars, columns}} 来自数据库，缺就不给。
    """
    items = []
    for code, s in all_sites(cfg).items():
        d = {
            "code": code,
            "name": s.get("name") or code,
            "base_url": s.get("base_url", ""),
            "kind": s.get("kind", ""),
            "enabled": bool(s.get("enabled", True)),
            "builtin": bool(s.get("builtin")),
            "broken": bool(s.get("_broken")),
            "error": s.get("_error", ""),
            "file": s.get("_file", ""),
            "date_start": s.get("date_start", ""),
            "date_end": s.get("date_end", ""),
            "n_columns": len(s.get("columns") or []),
        }
        if not s.get("builtin"):
            d["article"] = s.get("article", {}) or {}
            d["columns"] = [
                {"code": c.get("code", ""), "name": c.get("name", "")}
                for c in (s.get("columns") or [])
            ]
        if counts and code in counts:
            d.update(counts[code])
        items.append(d)
    items.sort(key=lambda x: (not x["builtin"], x["name"]))
    return items


# ---------------------------------------------------------------- 校验
REQUIRED_HTML = ["columns"]
REQUIRED_JSON = ["columns"]


def validate(spec: Dict[str, Any]) -> List[str]:
    """
    保存前的静态校验。返回问题清单，空列表=可保存。
    只查结构，不联网 —— 能不能真抓到由 probe 负责。
    """
    issues: List[str] = []
    if not spec.get("base_url"):
        issues.append("缺少 base_url")
    kind = spec.get("kind") or "html"
    if kind not in ("html", "json"):
        issues.append(f"kind 只能是 html 或 json，收到 {kind!r}")
    cols = spec.get("columns") or []
    if not cols:
        issues.append("至少要有一个栏目")
    for i, c in enumerate(cols):
        tag = f"栏目[{i + 1}] {c.get('name') or c.get('code') or '?'}"
        if not c.get("name"):
            issues.append(f"{tag}：缺 name")
        if not c.get("code"):
            issues.append(f"{tag}：缺 code")
        if kind == "html" and not (c.get("list_url") or "").strip():
            issues.append(f"{tag}：缺 list_url")
        if kind == "json":
            if not (c.get("api_url") or "").strip():
                issues.append(f"{tag}：缺 api_url")
            if not (c.get("list_path") or "").strip():
                issues.append(f"{tag}：缺 list_path（JSON 里列表所在路径）")
            # 两种 json 栏目形态，校验要求不同：
            #   ① 对象数组 -> 用 fields.{title,url,date} 映射
            #   ② 字段里装 **HTML 片段** -> 用 CSS 选择器 item/title/date
            #      （jpaas 建站平台就是这种，返回 {"data":{"html":"<ul>…"}}）
            # 早先不分形态，一律要求 fields，结果 ② 这类站**永远过不了
            # 校验闸门**——而它们的抓取路径是好的，白白挡住。
            uses_fields = not (c.get("item") or "").strip()
            f = c.get("fields") or {}
            if uses_fields:
                for need in ("title", "url", "date"):
                    if not f.get(need):
                        issues.append(f"{tag}：fields.{need} 没填")
            else:
                if not (c.get("title") or "").strip():
                    issues.append(f"{tag}：配了 item 就必须有 title 选择器")
                if not (c.get("date") or "").strip():
                    issues.append(f"{tag}：配了 item 就必须有 date 选择器")
    art = spec.get("article") or {}
    if kind == "html" and not (art.get("content") or "").strip():
        issues.append("article.content 没填 —— 正文选择器为空会抓出一堆空正文")
    return issues
