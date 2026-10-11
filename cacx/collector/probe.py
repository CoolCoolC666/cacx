# -*- coding: utf-8 -*-
"""
试抓验证（probe）
================================================================
这是**保存站点配置之前的闸门**，存在理由很实际：

    规则推断是猜的。猜错了直接保存、直接同步，库里就会多出一批
    「有标题、日期对、正文是导航栏」的脏数据。这类数据在检索页看着
    很正常，要等人真去读那篇文章才发现不对 —— 那时清理成本已经很高了。

所以流程固定为：

    输网址 → 推断规则 → **真抓一次给你看** → 你认可 → 才写配置

本模块全程只读不写数据库，绝不碰 config/settings.json。
"""

from typing import Any, Dict, List, Optional

from .base import Fetcher, charset_note, decode_response, html_to_text
from .generic import build as build_collector
from . import rules as R


# ---------------------------------------------------------------- 造草稿
def draft_from_url(cfg: Dict[str, Any], url: str, name: str = "",
                   use_ai: bool = True,
                   date_start: str = "2000-01-01",
                   date_end: str = "2099-12-31") -> Dict[str, Any]:
    """
    输一个网址，产出一份**站点配置草稿**（还没保存）。
    返回 {"draft": {...}, "evidence": [...], "issues": [...], "ai": {...}}
    """
    issues: List[str] = []
    evidence: List[str] = []
    ai_info: Dict[str, Any] = {}

    base = R  # 便于下面少写几个字符
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not url:
        return {"draft": {}, "evidence": [], "issues": ["请填写网址"], "ai": ai_info}

    f = Fetcher(ua=cfg.get("collector", {}).get("user_agent", ""),
                timeout=cfg.get("collector", {}).get("timeout", 25),
                retry=cfg.get("collector", {}).get("retry", 3),
                interval=cfg.get("collector", {}).get("interval", 0.35))

    r = f.get(url)
    if r is None:
        return {"draft": {}, "evidence": [],
                "issues": ["打不开这个网址（连接失败、超时，或被源站拒绝）"],
                "ai": ai_info}
    html = decode_response(r)
    evidence.append(f"已打开 {url}（HTTP {r.status_code}，{len(html)} 字符，"
                    f"编码判定 {charset_note(r)}）")

    title = ""
    try:
        from bs4 import BeautifulSoup
        title = BeautifulSoup(html, "html.parser").title.get_text(strip=True) \
            if BeautifulSoup(html, "html.parser").title else ""
    except Exception:
        pass
    site_name = name.strip() or re_split_title(title)
    if name.strip():
        evidence.append(f"站点名称用你填的：{name.strip()}")
    elif site_name:
        evidence.append(f"站点名称从网页标题推断：{site_name}")

    # ---- 列表规则 ----
    lr = R.infer_list_rule(html, url)
    kind = "html"
    if lr.get("ok") and not lr.get("issues"):
        evidence.append(f"自动探测到列表：{lr['item']}（命中 {lr.get('hit_count')} 条）")
    else:
        issues += lr.get("issues", ["列表规则自动探测失败"])
        best = lr.get("best") or {}
        if best.get("item"):
            issues.append(f"已给出一个低置信度候选：{best['item']}，"
                          f"试抓验证后再决定用不用")
            lr = {**lr, **best}

    # ---- HTML 路径走不通时，改推 JSON 接口模式 ----
    # 浙江、江苏等多省网信办跑在同一套 jpaas 建站平台上，
    # 主列表是前端调接口拿的，HTML 里压根没有（江苏网信整页只有 17 个
    # 含日期的零散块）。这时硬套 CSS 选择器只会浪费时间，
    # 正确的动作是换个采集模式。
    if not lr.get("ok"):
        api = R.find_api_candidates(html, url)
        if api:
            kind = "json"
            evidence.append(
                "这个页面像是 JS 加载列表的，已自动切换到 JSON 接口模式")
            for a in api[:3]:
                evidence.append(f"  接口候选：{a['url']}")
            issues.append("已按 JSON 模式生成草稿，但 list_path / fields 需要你确认："
                          "打开接口看返回的 JSON 结构，列表在哪个字段、文章字段叫什么。")
            draft_json = _json_draft(url, site_name, api[0]["url"])
            return _finish(draft_json, evidence, issues, ai_info, url)

    col = {
        "code": "main",
        "name": site_name or "主栏目",
        "list_url": url,
        "item": lr.get("item", ""),
        "title": lr.get("title", "a"),
        "link": lr.get("link", "a"),
        "date": lr.get("date", ""),
        "pagination": {"mode": "none"},
    }

    # ---- 详情页规则 ----
    article_rule: Dict[str, Any] = {}
    sample_url = (lr.get("sample") or {}).get("url", "")
    if sample_url:
        from .base import absolutize
        sample_url = absolutize(sample_url, url)
        ar = f.get(sample_url)
        if ar is not None:
            ar_html = decode_response(ar)
            art_rule = R.infer_article_rule(ar_html)
            article_rule = {k: v for k, v in art_rule.items()
                            if k in ("title", "pub_date", "content", "source")}
            article_rule["content_chars"] = art_rule.get("content_chars", 0)
            if art_rule.get("content"):
                evidence.append(f"自动探测到正文容器：{article_rule['content']}"
                                f"（约 {art_rule['content_chars']} 字）")
            else:
                issues += art_rule.get("issues", [])
            ai = None
            if use_ai and not art_rule.get("ok"):
                ai = R.ai_infer(cfg, ar_html, "article", sample_url)
                ai_info["article"] = {k: v for k, v in ai.items() if k != "result"}
                if ai.get("ok") and isinstance(ai.get("result"), dict):
                    res = ai["result"].get("article") or ai["result"]
                    article_rule.update({k: v for k, v in res.items()
                                         if v and k in ("title", "pub_date",
                                                        "content", "source")})
                    evidence.append("AI 补全了正文选择器")
                else:
                    issues += ai.get("issues", [])
            # 站名补全：无论走没走 AI 分支都要判一次。
            # 早前把这段放在 `if use_ai` 块外面却引用 ai，
            # 启发式成功（ai 从未赋值）时直接 UnboundLocalError，
            # 而启发式成功恰恰是最常见的路径 —— 等于主流程一走就崩。
            if (ai and ai.get("ok") and isinstance(ai.get("result"), dict)
                    and not site_name):
                nm = ai["result"].get("site_name")
                if nm:
                    site_name = nm
                    col["name"] = nm
        else:
            issues.append("详情页打不开，无法自动推断正文规则")

    draft = {
        "name": site_name or url,
        "base_url": _origin(url),
        "kind": "html",
        "enabled": True,
        "date_start": date_start,
        "date_end": date_end,
        "columns": [col],
        "article": article_rule or {"content": ""},
        "_inferred_from": url,
    }
    return {"draft": draft, "evidence": evidence,
            "issues": issues, "ai": ai_info}


def _finish(draft, evidence, issues, ai_info, url):
    draft["_inferred_from"] = url
    return {"draft": draft, "evidence": evidence,
            "issues": issues, "ai": ai_info}


def _json_draft(url, site_name, api_url):
    """JSON 模式的草稿骨架。
    list_path / fields 留成占位并显式标注 —— 这里不能瞎猜，
    猜错了用户会以为系统能自己填，直接保存就会抓回一堆空。"""
    return {
        "name": site_name or url,
        "base_url": _origin(url),
        "kind": "json",
        "enabled": True,
        "date_start": "2000-01-01",
        "date_end": "2099-12-31",
        "columns": [{
            "code": "main",
            "name": site_name or "主栏目",
            "api_url": api_url,
            "method": "GET",
            "list_path": "TODO:列表在哪个字段，如 data.list",
            "total_path": "",
            "url_prefix": "",
            "fields": {"title": "TODO:标题字段", "url": "TODO:链接字段",
                       "date": "TODO:日期字段"},
            "pagination": {"mode": "params", "size": 20, "max_pages": 20,
                           "params": {"page": "{page}", "size": "{size}"}},
            "_need_fill": ["list_path", "fields.title", "fields.url", "fields.date"],
        }],
        "article": {"title": "h1", "pub_date": "", "content": "TODO:正文选择器",
                    "_need_fill": ["article.content"]},
    }


def re_split_title(title: str) -> str:
    """从 <title> 里取站点名：去掉「_site - 后缀」这类尾巴"""
    t = (title or "").strip()
    for sep in ("_", " - ", "|", "－", "—", "·"):
        if sep in t:
            head = t.split(sep)[0].strip()
            if 2 <= len(head) <= 20:
                return head
    return t[:20]


def _origin(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------- 试抓
def probe_site(cfg: Dict[str, Any], spec: Dict[str, Any],
               max_columns: int = 5, n_samples: int = 5,
               preview_chars: int = 400) -> Dict[str, Any]:
    """
    用一份（可能是刚推断出来的）配置**真抓一次**。
    全程不写数据库、不写配置文件。

    返回给界面看的报告：每个栏目抓到几条、样例长什么样、
    以及第一篇文章的正文预览和字数。
    """
    from .. import sites as S

    issues = list(S.validate(spec))
    report: Dict[str, Any] = {
        "ok": False, "issues": issues, "columns": [],
        "article": None, "written_to_db": False,
    }
    if issues:
        return report

    # 试抓一律用宽时间窗，否则会因为「样本日期在窗口外」误报成 0 条
    spec = {**spec, "date_start": "2000-01-01", "date_end": "2099-12-31"}
    spec.setdefault("code", "probe")
    try:
        col = build_collector(spec, cfg.get("collector", {}), verbose=False)
    except Exception as e:
        report["issues"] = [f"配置无法使用: {type(e).__name__}: {e}"]
        return report

    cols = col.columns()[:max_columns]
    if not cols:
        report["issues"].append("没有栏目")
        return report

    total_listed = 0
    for c in cols:
        try:
            items = col.list_articles(c["code"])
        except Exception as e:
            report["columns"].append({**c, "listed": 0, "error":
                                      f"{type(e).__name__}: {e}", "samples": []})
            report["issues"].append(f"{c['name']}：抓取异常 {type(e).__name__}")
            continue
        total_listed += len(items)
        entry = {
            **c, "listed": len(items), "samples": [],
            "no_date": sum(1 for x in items if not x.get("date")),
        }
        for it in items[:n_samples]:
            entry["samples"].append({"title": it["title"], "date": it["date"],
                                     "url": it["url"]})
        report["columns"].append(entry)

    if total_listed == 0:
        report["issues"].append("所有栏目都没抓到条目 —— 选择器多半不对")
        return report

    # 挑一篇最长的做正文预览：短的可能是「通知公告」这类占位页
    all_items = []
    for entry in report["columns"]:
        for s in entry["samples"]:
            all_items.append(s)
    if not all_items:
        report["issues"].append("没有样例条目可用来验证正文")
        return report
    target = all_items[0]
    try:
        art = col.fetch_article(target["url"], target)
    except Exception as e:
        report["issues"].append(f"正文抓取异常 {type(e).__name__}: {e}")
        art = None
    if not art:
        report["issues"].append("正文抓不到（article.content 选择器需要手工修正）")
    else:
        text = html_to_text(art["content_html"], col.base_url)
        report["article"] = {
            "url": target["url"],
            "title": art.get("title") or "",
            "pub_date": art.get("pub_date") or "",
            "source_text": art.get("source_text") or "",
            "chars": len(text.strip()),
            "guessed": bool(art.get("_guessed")),
            "preview": text.strip()[:preview_chars],
        }
        if art.get("_guessed"):
            report["issues"].append(
                "正文是启发式兜底抓的（content 选择器没命中），"
                "内容可能不完整，建议手工补一个正文选择器")
        if len(text.strip()) < 80:
            report["issues"].append(f"正文只有 {len(text.strip())} 字，很可能选错了容器")

    report["ok"] = not report["issues"]
    return report


def fill_from_probe(draft: Dict[str, Any], probe: Dict[str, Any]) -> Dict[str, Any]:
    """把试抓结果里发现的日期缺失误写回草稿（显式登记，不静默）"""
    draft = {**draft}
    cols = []
    for c in draft.get("columns", []):
        hit = next((e for e in probe.get("columns", [])
                    if e.get("code") == c.get("code")), {})
        c = {**c}
        if hit.get("no_date") and not c.get("date"):
            c["date"] = ""
            c["_warn"] = "列表里有条目取不到日期"
        cols.append(c)
    draft["columns"] = cols
    return draft
