# -*- coding: utf-8 -*-
"""
规则推断：给一个网址，推断出「怎么抓」
================================================================
两条腿走路，且**启发式在前**：

    1. 启发式探测（不花钱、不依赖网络服务、结果可复现）
    2. AI 补全（启发式抓不准时兜底）

为什么不让 AI 打头：AI 有 token 成本、有偶发幻觉、同一页面两次结果可能不同，
而「选一个 item 选择器」这类事，结构化探测的准确率通常更高也更稳。
AI 的价值在于启发式**完全找不到**时（比如页面结构诡异），以及
帮我们推断出站名、栏目语义、分页规律这些**语义**部分。

所以 AI 只在明确要求或启发式失败时才调用，且它的产出**永远要过 probe 验证**
才允许保存 —— 模型说「这是正文区」不算数，抓出来的字数才算数。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from .base import Fetcher, decode_response
from .generic import normalize_date

DATE_RX = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?")

# 看起来像「文章详情页」的链接特征
ARTICLE_HINT = re.compile(
    r"(/art(icle)?/|/content|/detail|/info|/nry|/show|/post|/(\d{4})[-_/]?\d{2}"
    r"|[/\-_\d]{6,}\.(s?html?|shtml)$)", re.I)
# 导航里的噪声词，不是栏目
NAV_JUNK = ("首页", "更多", "登录", "注册", "English", "无障碍", "长者",
            "简体", "繁體", "返回", "上一页", "下一页", "网站地图", "联系我们",
            "版权", "关于我们", "打印", "分享", "微信", "微博", "RSS")


# ---------------------------------------------------------------- 小工具
def _soup(html: str):
    s = BeautifulSoup(html or "", "html.parser")
    for bad in s.select("script, style, noscript, iframe"):
        bad.decompose()
    return s


def css_of(el, max_depth: int = 4) -> str:
    """给一个节点生成尽量稳定的 CSS 选择器"""
    parts, cur, depth = [], el, 0
    while cur is not None and getattr(cur, "name", None) and depth < max_depth:
        seg = cur.name
        eid = cur.get("id")
        if eid and re.match(r"^[A-Za-z][\w-]*$", str(eid)):
            parts.append(f"#{eid}")
            break
        cls = [c for c in (cur.get("class") or []) if isinstance(c, str)
               and re.match(r"^[A-Za-z][\w-]*$", c)]
        if cls:
            seg += "".join("." + c for c in cls[:2])
        parent = cur.parent
        if parent is not None and getattr(parent, "name", None):
            sibs = [c for c in parent.find_all(cur.name, recursive=False)
                    if c is not cur]
            if sibs:
                seg += f":nth-of-type({sibs.index(cur) + 1})"
        parts.append(seg)
        cur = parent
        depth += 1
    return " > ".join(reversed(parts))


def _looks_article(href: str) -> bool:
    if not href:
        return False
    h = href.strip()
    if h.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    return bool(ARTICLE_HINT.search(h))


def _text(el, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True))[:limit]


# ---------------------------------------------------------------- 列表规则
def infer_list_rule(html: str, base_url: str = "") -> Dict[str, Any]:
    """
    从一份列表页 HTML 里推断 item / title / link / date 选择器。

    判据（为什么是这几条，都是被假阳性坑出来的）：
      · 命中数 >= 5      —— 少于 5 条的区块更可能是「推荐位」不是主列表
      · 每块恰好 1 个文章链接 —— 多链接的块往往是「标题+更多」或卡片组
      · >=60% 的块里能读出日期 —— 有日期的列表才做得了时间窗筛选与早停
      · 文本长度在 15~200 字 —— 太短是图标/按钮，太长是整段摘要页
    """
    soup = _soup(html)

    # 候选：(tag, class元组)
    cands: Dict[Tuple[str, Tuple[str, ...]], None] = {}
    for a in soup.find_all("a"):
        if not _looks_article(a.get("href") or ""):
            continue
        cur = a
        for _ in range(5):
            cur = cur.parent
            if cur is None or not getattr(cur, "name", None) or cur.name == "body":
                break
            key = (cur.name, tuple(cur.get("class") or []))
            cands[key] = None
            if cur.find_parent(["body", "html"]) is None:
                break

    best, best_score = None, 0.0
    for (tag, cls) in cands:
        sel = tag + "".join("." + c for c in cls if re.match(r"^[A-Za-z][\w-]*$", c))
        try:
            els = soup.select(sel)
        except Exception:
            continue
        if not (5 <= len(els) <= 200):
            continue
        ok_link = ok_date = 0
        texts = []
        for el in els:
            links = [a for a in el.find_all("a") if _looks_article(a.get("href") or "")]
            if len(links) == 1:
                ok_link += 1
            t = _text(el)
            if DATE_RX.search(t):
                ok_date += 1
            if 15 <= len(t) <= 400:
                texts.append(t)
        if not texts:
            continue
        ratio_link = ok_link / len(els)
        ratio_date = ok_date / len(els)
        if ratio_link < 0.8 or ratio_date < 0.6:
            continue
        # 打分：结构一致性权重最高，其次日期，其次「命中别太多」
        # （命中 200 条往往是把整个 ul 也选进去了）
        score = ratio_link * 3 + ratio_date * 2.5 + min(len(els), 40) / 40.0
        joined = " ".join(cls).lower()
        if any(k in joined for k in ("list", "news", "item", "row", "content", "article")):
            score += 0.6
        if score > best_score:
            best_score, best = score, sel
    if not best:
        return {"ok": False, "issues": [
            "没找到像文章列表的区块。可能：①列表是 JS 渲染的（这种站要 Playwright，"
            "本版不支持）②页面确实没有超过 5 条的列表 ③需要手工指定 item 选择器"]}

    els = soup.select(best)
    item = els[0]

    # 在条目里找标题 / 链接 / 日期
    title_el = link_el = date_el = None
    for a in item.find_all("a"):
        if _looks_article(a.get("href") or ""):
            link_el = a
            if len(a.get_text(strip=True)) >= 6:
                title_el = a
                break
    if title_el is None and link_el is not None:
        title_el = link_el

    for el in item.select("span, div, p, td, em, i, time"):
        t = el.get_text(" ", strip=True)
        if len(t) <= 40 and normalize_date(t):
            date_el = el
            break
    if date_el is None and DATE_RX.search(_text(item)):
        date_el = item

    rule = {
        "ok": True,
        "item": best,
        "link": css_of(link_el).split(" > ")[-1] if link_el else "a",
        "title": css_of(title_el).split(" > ")[-1] if title_el else "a",
        "date": (css_of(date_el).split(" > ")[-1] if date_el and date_el is not item
                 else ""),
        "hit_count": len(els),
        "score": round(best_score, 2),
    }
    # 抽样验证：真按规则解析一条，日期和链接都得有值
    probe_item = None
    for el in els[:5]:
        u = link_el and link_el.get("href")
        if u:
            probe_item = {"url": u, "title": title_el.get_text(strip=True) if title_el else "",
                          "date": normalize_date(_text(date_el)) if date_el else ""}
            break
    if probe_item is None:
        rule["ok"] = False
        rule["issues"] = ["定位到列表区块，但没能从中解析出文章链接，请手工指定 link 选择器"]
    else:
        rule["sample"] = probe_item
        if not probe_item["date"]:
            rule["issues"] = ["列表里读不出日期，时间窗筛选与早停会失效，请手工指定 date 选择器"]
    return rule


def infer_article_rule(html: str) -> Dict[str, Any]:
    """从详情页 HTML 推断 title / pub_date / source / content"""
    soup = _soup(html)
    out: Dict[str, Any] = {"ok": False, "issues": []}

    h1 = soup.find("h1")
    if h1 and len(h1.get_text(strip=True)) >= 4:
        out["title"] = css_of(h1).split(" > ")[-1]
    else:
        for sel in (".title", ".article-title", "h2.title", ".tit", ".titMain"):
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) >= 4:
                out["title"] = css_of(el).split(" > ")[-1]
                break
    if not out.get("title"):
        out["issues"].append("找不到标题元素，详情页标题将退回列表页标题")

    # 日期：优先 class/name 语义，其次页面上第一个能归一化的短文本
    date_el = None
    for el in soup.select("[class*=date], [class*=time], [class*=pub], "
                          "[name*=date], span, em, div"):
        t = el.get_text(" ", strip=True)
        if 4 <= len(t) <= 40 and normalize_date(t):
            date_el = el
            break
    if date_el is not None:
        out["pub_date"] = css_of(date_el).split(" > ")[-1]

    src_el = None
    for el in soup.select("[class*=source], [class*=from], [class*=zuizhi]"):
        t = el.get_text(" ", strip=True)
        if t and len(t) <= 60:
            src_el = el
            break
    if src_el is not None:
        out["source"] = css_of(src_el).split(" > ")[-1]

    # 正文：文字最多、链接最少的块
    from .generic import guess_content
    body = guess_content(soup)
    if body:
        # 反查是哪个节点
        for el in soup.select("div, td, article, section"):
            if _text(el, 10_000) and str(el).find(body[:60]) >= 0:
                out["content"] = css_of(el).split(" > ")[-1]
                break
    if not out.get("content"):
        out["issues"].append("定位不到正文块，必须手工指定 article.content 选择器，"
                             "否则每篇正文都会判为失败")
    else:
        plain = re.sub(r"<[^>]+>", "", body).strip()
        out["content_chars"] = len(plain)
        if len(plain) < 120:
            out["issues"].append(f"疑似正文只有 {len(plain)} 字，可能选到了摘要区")
    out["ok"] = bool(out.get("content"))
    return out


# ---------------------------------------------------------------- 栏目发现
def discover_columns(base_url: str, fetcher: Fetcher,
                     limit: int = 20) -> List[Dict[str, str]]:
    """
    从首页导航里挑出可能的栏目页。
    只做**候选**，不保证都是列表页 —— 真假由 probe 判定。
    """
    from urllib.parse import urljoin, urlparse
    r = fetcher.get(base_url)
    if r is None:
        return []
    html = decode_response(r)
    soup = _soup(html)
    host = urlparse(base_url).netloc
    seen, out = set(), []

    zones = soup.select("nav a, .nav a, .menu a, .menu li a, header a, "
                        "ul li a, .column a, .navBox a")
    if not zones:
        zones = soup.find_all("a")
    for a in zones:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        if p.netloc != host or p.scheme not in ("http", "https"):
            continue
        path = p.path.strip("/")
        if not path or path.count("/") > 2:          # 太深的多半是详情页
            continue
        if full.rstrip("/") == base_url.rstrip("/"):
            continue
        txt = re.sub(r"\s+", "", a.get_text(" ", strip=True))
        if not (2 <= len(txt) <= 14) or any(j in txt for j in NAV_JUNK):
            continue
        key = full.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"code": re.sub(r"[^a-zA-Z0-9_-]+", "", p.path.strip("/")
                                   .replace("/", "_")) or f"c{len(out)+1}",
                    "name": txt, "list_url": key})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- AI 补全
_AI_SYSTEM = (
    "你是网页采集规则助手。根据给定的 HTML 片段，判断页面上「文章列表」和"
    "「文章正文」的 CSS 选择器。只输出 JSON，不要解释。"
)

_AI_SHAPE = {
    "list": {"item": "选中每一条列表项的 CSS 选择器",
             "title": "条目内标题元素选择器", "date": "条目内日期元素选择器，可为空串",
             "link": "条目内链接元素选择器"},
    "article": {"title": "详情页标题选择器", "pub_date": "发布时间选择器",
                "content": "正文容器选择器", "source": "来源选择器，可为空串"},
    "site_name": "网站中文名称",
    "columns": [{"name": "栏目名", "list_url": "栏目列表页地址"}],
}


def ai_infer(cfg: Dict[str, Any], html: str, what: str = "list",
             base_url: str = "") -> Dict[str, Any]:
    """
    交给 AI 推断规则。失败不抛异常 —— 返回 ok=False + 原因，
    启发式结果仍然可用，别让一次模型抖动废掉整条流程。
    """
    from ..ai import base as AIB

    p = AIB.build(cfg)
    if p is None:
        return {"ok": False, "issues": ["AI 未配置（provider=none 或缺 base_url）"]}
    snippet = re.sub(r"\n\s*\n", "\n", html or "")[:12000]
    user = (f"目标站点：{base_url}\n要判断的部分：{what}\n"
            f"字段含义：{json.dumps(_AI_SHAPE[what], ensure_ascii=False)}\n"
            f"只输出如下结构的 JSON："
            f"{json.dumps(_AI_SHAPE[what], ensure_ascii=False)}\n\n"
            f"HTML 片段：\n{snippet}")
    try:
        raw = p.chat(_AI_SYSTEM, user, dry_run=False)
    except Exception as e:
        return {"ok": False, "issues": [f"AI 调用失败: {type(e).__name__}: {e}"]}
    try:
        d = json.loads(_first_json(raw))
    except Exception as e:
        return {"ok": False, "issues": [f"AI 返回的不是合法 JSON: {type(e).__name__}",
                                        raw[:200]]}
    return {"ok": True, "result": d, "source": "ai"}


def _first_json(text: str) -> str:
    """模型常在 JSON 外面裹 ```json 或加一句人话，取第一段完整对象"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return t[i:j + 1]
    return t
