# -*- coding: utf-8 -*-
"""
中国网信办 www.cac.gov.cn 采集器
================================================================
接口（2026-10-06 实测，详见 ../../日志/2026-10-06_c_*.md）：
    POST https://www.cac.gov.cn/cms/JsonList
    body: channelCode / perPage / pageno / condition / fuhao / value
    返回: {"totalRec": N, "list":[{topic,infourl,pubtime,filltime,coverurl,description}]}
    · 返回是**扁平**结构，不是 data.list
    · perPage 实测可开到 200
    · Referer 不校验，无 Cookie / 验证码
文章页：
    <h1 class="title"> · <span id="pubtime"> · <span id="source"> · <DIV id=BodyLabel>
    注意 DIV 大写、属性无引号；正文尾部混有 <div id="网站群管理"> 需剥除
"""

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .base import SiteCollector, clean_title

# 14 个栏目。A0937「网信政务总」是聚合导航页(totalRec 15394)，
# 其内容已被下列 12 个子栏目完全覆盖(合计 16318)，故不采。
CHANNELS = [
    ("A093601",   "时政要闻",        "/yaowen/szyw/A093601index_1.htm"),
    ("A093602",   "网信要闻",        "/yaowen/wxyw/A093602index_1.htm"),
    ("A09370101", "党的建设",        "/wxzw/djjs/A09370101index_1.htm"),
    ("A09370102", "办领导动态",      "/wxzw/bgs/blddt/A09370102index_1.htm"),
    ("A093702",   "网信发布",        "/wxzw/wxfb/A093702index_1.htm"),
    ("A093703",   "政策法规",        "/wxzw/zcfg/A093703index_1.htm"),
    ("A093704",   "互联网内容管理",  "/wxzw/hlwnrgl/A093704index_1.htm"),
    ("A093705",   "网络安全",        "/wxzw/wlaq/A093705index_1.htm"),
    ("A093706",   "数据安全",        "/wxzw/sjaq/A093706index_1.htm"),
    ("A093707",   "个人信息保护",    "/wxzw/grxxbh/A093707index_1.htm"),
    ("A093708",   "信息化",          "/wxzw/xxh/A093708index_1.htm"),
    ("A09370801", "信息化·数据出境", "/wxzw/xxh/A09370801index_1.htm"),
    ("A093709",   "网络信息管理",    "/wxzw/wlxxgl/A093709index_1.htm"),
    ("A093710",   "地方网信",        "/wxzw/dfwx/A093710index_1.htm"),
]

TITLE_RE = re.compile(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>', re.S | re.I)
PUB_RE = re.compile(r'<span[^>]*id=["\']?pubtime["\']?[^>]*>(.*?)</span>', re.S | re.I)
SRC_RE = re.compile(r'<span[^>]*id=["\']?source["\']?[^>]*>(.*?)</span>', re.S | re.I)
JUNK_RE = re.compile(r'<div[^>]*id=["\']?网站群管理.*?</div>', re.S | re.I)


class _BodyExtractor(HTMLParser):
    """提取 <DIV id=BodyLabel> 内部原始 HTML（支持嵌套与大写标签）"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_body = False
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        if tl == "div" and not self.in_body:
            if any((k or "").lower() == "id" and (v or "") == "BodyLabel"
                   for k, v in attrs):
                self.in_body = True
                self.depth = 1
                return
        if self.in_body:
            if tl == "div":
                self.depth += 1
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self.in_body:
            return
        tl = tag.lower()
        if tl == "div":
            if self.depth <= 1:
                self.depth -= 1
                self.in_body = False
                return
            self.depth -= 1
        self.parts.append(f"</{tl}>")

    def handle_startendtag(self, tag, attrs):
        if self.in_body:
            self.parts.append(self.get_starttag_text())

    def handle_data(self, data):
        if self.in_body:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.in_body:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.in_body:
            self.parts.append(f"&#{name};")

    def result(self):
        return "".join(self.parts)


class CacCollector(SiteCollector):
    code = "cac"
    name = "中国网信办"
    base_url = "https://www.cac.gov.cn"

    def __init__(self, cfg_site, cfg_collector, verbose=True):
        super().__init__(cfg_site, cfg_collector, verbose)
        self.api_url = cfg_site.get("api_url", self.base_url + "/cms/JsonList")
        self.per_page = int(cfg_site.get("per_page", 200))
        self._paths = {c: p for c, _n, p in CHANNELS}

    def columns(self):
        return [{"code": c, "name": n} for c, n, _p in CHANNELS]

    def _list_page(self, ch_code: str, page_no: int):
        referer = self.base_url + self._paths.get(ch_code, "/")
        data = {"channelCode": ch_code, "perPage": self.per_page,
                "pageno": page_no, "condition": 0, "fuhao": "=", "value": ""}
        headers = {"X-Requested-With": "XMLHttpRequest",
                   "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                   "Referer": referer}
        r = self.fetcher.post(self.api_url, data=data, headers=headers)
        if r is None:
            return None, 0
        try:
            d = r.json()
        except Exception:
            return None, 0
        try:
            total = int(d.get("totalRec") or 0)   # 实测有时是字符串
        except (TypeError, ValueError):
            total = 0
        return d.get("list") or [], total

    def list_articles(self, column_code: str) -> List[Dict]:
        name = next((n for c, n, _ in CHANNELS if c == column_code), column_code)
        out, page, got, total = [], 1, 0, 0
        while page <= 400:
            lst, total = self._list_page(column_code, page)
            if lst is None:
                self.log(f"    [warn] {name} 第{page}页请求失败，停止该栏目")
                break
            if not lst:
                break
            # 列表按 pubtime 严格倒序 —— 一旦本页出现早于起始年的条目，
            # 后面的只会更旧，可以直接早停，不必翻完整个存量
            # （CAC 有 2.5 万条历史回填稿，限定年份时全翻极慢）
            hit_old = False
            for it in lst:
                pt = (it.get("pubtime") or "")[:10]
                if not pt:
                    continue
                if pt < self.date_start:
                    hit_old = True
                    break
                if pt > self.date_end:
                    continue
                u = (it.get("infourl") or "").strip()
                if u.startswith("//"):
                    u = "https:" + u
                elif u.startswith("/"):
                    u = self.base_url + u
                out.append({
                    "url": u,
                    "title": (it.get("topic") or "").strip(),
                    "date": pt,
                    "column_code": column_code,
                    "column_name": name,
                    "description": (it.get("description") or "").strip(),
                    "coverurl": (it.get("coverurl") or "").strip(),
                })
            got += len(lst)
            if hit_old:
                break
            if total and got >= total:
                break
            if not total and len(lst) < self.per_page:
                break
            page += 1
        return out

    def fetch_article(self, url: str, fallback: Dict) -> Optional[Dict]:
        r = self.fetcher.get(url, referer=self.base_url + "/")
        if r is None:
            return None
        t = r.text

        tm = TITLE_RE.search(t)
        title = clean_title(tm.group(1)) if tm else fallback.get("title", "")

        pub = ""
        pm = PUB_RE.search(t)
        if pm:
            raw = clean_title(pm.group(1))
            dm = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", raw)
            if dm:
                y, mo, dd = (int(x) for x in dm.groups())
                pub = f"{y:04d}-{mo:02d}-{dd:02d}"
                hm = re.search(r"(\d{1,2}):(\d{2})", raw)
                if hm:
                    pub += f" {int(hm.group(1)):02d}:{hm.group(2)}"

        src = ""
        sm = SRC_RE.search(t)
        if sm:
            src = re.sub(r"^来源[:：]\s*", "", clean_title(sm.group(1))).strip()

        ex = _BodyExtractor()
        try:
            ex.feed(t)
            ex.close()
        except Exception:
            pass
        body = JUNK_RE.sub("", ex.result()).strip()
        if not body:
            return None
        body = re.sub(r'<img\s+src="([^"]*)"',
                      lambda m: f'<img src="{_abs_img(m.group(1), self.base_url)}"',
                      body)
        return {"title": title, "pub_date": pub, "source_text": src,
                "content_html": body}

    def health_check(self) -> Dict:
        """检查列表接口字段是否还是预期结构（站点改版时 doctor 会用到）"""
        issues = []
        lst, total = self._list_page("A093602", 1)
        if lst is None:
            return {"site": self.code, "ok": False,
                    "issues": ["列表接口无响应或返回非 JSON"]}
        if not isinstance(lst, list) or not lst:
            return {"site": self.code, "ok": False,
                    "issues": [f"list 字段异常，拿到 {type(lst).__name__}"]}
        need = {"topic", "infourl", "pubtime"}
        miss = need - set(lst[0].keys())
        if miss:
            issues.append(f"列表条目缺字段: {sorted(miss)}")
        dates = [(x.get("pubtime") or "")[:10] for x in lst if x.get("pubtime")]
        if dates and any(dates[i] < dates[i + 1] for i in range(len(dates) - 1)):
            issues.append("列表不再是按 pubtime 倒序，日期筛选逻辑需复核")
        # 探一篇正文
        u = lst[0].get("infourl", "")
        if u.startswith("//"):
            u = "https:" + u
        art = self.fetch_article(u, {"title": ""}) if u else None
        if not art:
            issues.append("正文页解析失败（BodyLabel 选择器可能已失效）")
        elif not TITLE_RE.search(self.fetcher.get(u, self.base_url + "/").text):
            issues.append("正文页 h1.title 选择器可能已失效")
        return {"site": self.code, "ok": not issues, "issues": issues,
                "total_rec": total, "checked_columns": len(CHANNELS)}


def _abs_img(src: str, base: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return base.rstrip("/") + src
    return src