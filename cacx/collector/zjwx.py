# -*- coding: utf-8 -*-
"""
浙江网信网 www.zjwx.gov.cn 采集器
================================================================
列表接口（jpaas CMS 隐藏端点）：
    GET /api-gateway/jpaas-publish-server/front/page/build/unit
        ?parseType=bulidstatic&webId=3238
        &tplSetId=N1veJDb5WxxasyYgczpyA&pageType=column
        &tagId=当前栏目list&editType=null&pageId=<ColId>
        &paramJson={"pageNo":N,"pageSize":15}
    返回 data.html（内嵌列表 + <div class="pagination" count="N">）

文章页：meta PubDate/Author/ContentSource/ColumnName；正文 div.art_con
    ⚠ 部分条目（转载占位）的 art_con 源站即为空，属正常。

栏目名以**站点自称**为准（2026-10-06 订正）：
    原配置「法治浙江 / 之江净网 / 网上辟谣」是上一轮人工标签，
    站点实际为「治理监管 / 清朗网络 / 安全动态」。
"""

import html as html_mod
import json
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .base import SiteCollector, absolutize, clean_title
from .classify import classify

# (ColId, 站点自称栏目名)
COLUMNS = [
    ("1228998798", "通知公告"),
    ("1673565",    "网信要闻"),
    ("1694583",    "工作动态"),
    ("1694818",    "数字浙江"),
    ("1694595",    "社会治理"),
    ("1694598",    "网络文化"),
    ("1694817",    "网络强省"),
    ("1694597",    "网络安全"),
    ("1694596",    "传播前沿"),
    ("1694600",    "世界互联网大会"),
    ("1673576",    "治理监管"),
    ("1673574",    "清朗网络"),
    ("1673575",    "安全动态"),
]

META_RE = re.compile(
    r'<meta name="(PubDate|Author|ContentSource|ColumnName|ColId)" content="([^"]*)"')
TITLE_RE = re.compile(r'<div class="w760 cf f26 mb10"[^>]*>(.*?)</div>', re.S)

# 列表项：两种 URL 形式都要认
LIST_ITEM_RE = re.compile(
    r'<a class="fl" href="(?P<url>(?:/col/col\d+/art/\d+/art_[0-9a-f]{32}\.html'
    r'|/art/\d+/\d+/\d+/art_\d+_\d+\.html))"[^>]*>'
    r'(?:<i></i>)?(?P<title>.*?)</a>\s*<span class="fr">(?P<date>[\d-]+)</span>',
    re.S)


class _ArtCon(HTMLParser):
    """按 class=art_con 提取正文（支持嵌套 div）"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_art = False
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            cls = dict(attrs).get("class", "") or ""
            if not self.in_art and "art_con" in cls.split():
                self.in_art = True
                self.depth = 1
                return
            if self.in_art:
                self.depth += 1
        if self.in_art:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self.in_art:
            return
        if tag == "div":
            if self.depth <= 1:
                self.depth -= 1
                self.in_art = False
                return
            self.depth -= 1
        self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        if self.in_art:
            self.parts.append(self.get_starttag_text())

    def handle_data(self, data):
        if self.in_art:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.in_art:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.in_art:
            self.parts.append(f"&#{name};")

    def result(self):
        return "".join(self.parts)


class ZjwxCollector(SiteCollector):
    code = "zjwx"
    name = "浙江网信网"
    base_url = "https://www.zjwx.gov.cn"

    def __init__(self, cfg_site, cfg_collector, verbose=True):
        super().__init__(cfg_site, cfg_collector, verbose)
        self.api_url = cfg_site.get("api_url", self.base_url + "/cms/JsonList")
        self.page_size = int(cfg_site.get("page_size", 15))
        self._ids = {c: n for c, n in COLUMNS}

    def columns(self):
        return [{"code": c, "name": n} for c, n in COLUMNS]

    def _list_page(self, col_id: str, page_no: int):
        params = {
            "parseType": "bulidstatic", "webId": "3238",
            "tplSetId": "N1veJDb5WxxasyYgczpyA", "pageType": "column",
            "tagId": "当前栏目list", "editType": "null", "pageId": col_id,
            "paramJson": json.dumps({"pageNo": page_no, "pageSize": self.page_size},
                                    ensure_ascii=False),
        }
        r = self.fetcher.get(self.api_url, params=params,
                              referer=f"{self.base_url}/col/col{col_id}/index.html")
        if r is None:
            return None, 0
        try:
            d = r.json()
        except Exception:
            return None, 0
        if not d.get("success"):
            return None, 0
        html_text = d.get("data", {}).get("html", "")
        m = re.search(r'count="(\d+)"\s+pageNo="(\d+)"', html_text)
        count = int(m.group(1)) if m else 0
        return html_text, count

    def list_articles(self, column_code: str) -> List[Dict]:
        name = self._ids.get(column_code, column_code)
        out, page, count, miss = [], 1, 0, 0
        while page <= 400:
            html_text, count = self._list_page(column_code, page)
            if html_text is None:
                self.log(f"    [warn] {name} 第{page}页请求失败，停止该栏目")
                break
            if not html_text:
                break
            items = list(LIST_ITEM_RE.finditer(html_text))
            if not items:
                miss += 1
                if miss >= 2:
                    break
                page += 1
                continue
            miss = 0

            in_page = 0
            for m in items:
                day = m.group("date").strip()
                if self.date_start <= day <= self.date_end:
                    in_page += 1
                    u = self.base_url + m.group("url")
                    kind, reason = classify(u, self.base_url,
                                            self.cfg_site.get("topic_prefixes", ()))
                    out.append({
                        "url": u,
                        "title": clean_title(m.group("title")),
                        "date": day,
                        "column_code": column_code,
                        "column_name": name,
                        "kind": kind,
                        "skip_reason": reason,
                    })
            # 列表按日期倒序：整页没有一条落在窗口内，且存在早于起始日的条目，
            # 说明后面的只会更旧 —— 安全收工，不必翻完整个存量。
            # （整页判断而非遇到即停，避免跨年同日被误切）
            oldest = min(m.group("date").strip() for m in items)
            if in_page == 0 and oldest < self.date_start:
                break

            if count:
                total_pages = (count + self.page_size - 1) // self.page_size
                if page >= total_pages:
                    break
            page += 1
        return out

    def fetch_article(self, url: str, fallback: Dict) -> Optional[Dict]:
        r = self.fetcher.get(url, referer=self.base_url + "/")
        if r is None:
            return None
        t = r.text
        metas = dict(META_RE.findall(t))
        tm = TITLE_RE.search(t)
        title = clean_title(tm.group(1)) if tm else fallback.get("title", "")

        ex = _ArtCon()
        try:
            ex.feed(t)
            ex.close()
        except Exception:
            pass
        body = ex.result().strip()
        body = re.sub(r'<img\s+src="([^"]*)"(?:\s+alt="([^"]*)")?',
                      lambda m: f'<img src="{absolutize(m.group(1), self.base_url)}"'
                                + (f' alt="{m.group(2)}"' if m.group(2) else "") + ">",
                      body)
        return {"title": title, "pub_date": metas.get("PubDate", ""),
                "source_text": metas.get("ContentSource", ""),
                "author": metas.get("Author", ""),
                "column_name": metas.get("ColumnName", "") or fallback.get("column_name", ""),
                "content_html": body}   # body 可能为空 = 源站即为空，属正常

    def health_check(self) -> Dict:
        issues = []
        html_text, count = self._list_page("1673565", 1)
        if html_text is None:
            return {"site": self.code, "ok": False,
                    "issues": ["列表接口无响应或 success!=true"]}
        if not LIST_ITEM_RE.search(html_text):
            issues.append("列表页未匹配到文章链接，LIST_ITEM_RE 可能已失效")
        if not re.search(r'count="\d+"', html_text):
            issues.append("分页 count 标记消失，翻页终止条件需改写")
        links = LIST_ITEM_RE.findall(html_text)
        if links:
            u = self.base_url + links[0][0]
            if not self.fetcher.get(u, referer=self.base_url + "/"):
                issues.append("正文页请求失败")
        return {"site": self.code, "ok": not issues, "issues": issues,
                "count": count, "checked_columns": len(COLUMNS)}