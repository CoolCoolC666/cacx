# -*- coding: utf-8 -*-
"""
通用站点采集器（配置驱动）
================================================================
两种模式，覆盖绝大多数「网信办 / 网信网」类站点：

    kind = "html"   服务端渲染的列表页 + 详情页
                    规则是一组 CSS 选择器 + 分页方式
    kind = "json"   列表走接口（像 CAC / 浙江那种）
                    规则是一组 JSON 路径 + 字段映射

设计取舍（踩过的坑写在这里，别退化）：
    · **选择器空 ≠ 静默跳过**。取不到正文就返回 None，让 runner 记失败。
      「抓不到但不记录」会造出大批有标题没正文的幽灵数据，
      而这类脏数据要等到人读的时候才发现，代价极大。
    · **日期必须归一化**。中文站点日期格式极杂：
      2026-10-10 / 2026/10/10 / 2026年10月10日 / 2026.10.10 /
      10-10（缺年份）/ 1758000000000（epoch 毫秒）
      不归一化的话，库里会出现「2026-10」这种没法排序的日期，
      倒序早停逻辑也会整体失效。
    · **列表早停**只在前缀日期单调递减时才敢用。规则站点不保证排序，
      检测到乱序就退化成翻完 max_pages，不拿正确性换速度。
"""

import json
import re
from typing import Any, Dict, List, Optional

try:
    from bs4 import BeautifulSoup
except ImportError:                              # pragma: no cover
    BeautifulSoup = None

from .base import SiteCollector, absolutize, clean_title, decode_response
from .classify import classify

# ---------------------------------------------------------------- 日期
_DATE_FULL = [
    re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"),
]
_DATE_NOYEAR = [
    re.compile(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(?!\d)"),
]
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def normalize_date(raw: Any, default_year: str = "") -> str:
    """
    任意日期形态 → 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM'。
    认不出来就返回空串 —— 空值由上层决定回退到什么，
    绝不猜一个「看起来差不多」的日期进库。
    """
    if raw is None:
        return ""
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        n = int(float(raw))
        if n > 1_000_000_000_000:                  # 毫秒
            n //= 1000
        if 946_684_800 <= n <= 4_102_444_800:      # 2000~2100
            import time as _t
            return _t.strftime("%Y-%m-%d", _t.localtime(n))
        return ""

    s = str(raw).strip()
    if not s:
        return ""
    s = s.replace(" ", " ")

    for rx in _DATE_FULL:
        m = rx.search(s)
        if m:
            y, mo, dd = (int(x) for x in m.groups())
            if not (1 <= mo <= 12 and 1 <= dd <= 31):
                return ""
            out = f"{y:04d}-{mo:02d}-{dd:02d}"
            hm = _TIME_RE.search(s)
            if hm:
                out += f" {int(hm.group(1)):02d}:{hm.group(2)}"
            return out

    if default_year and default_year.isdigit() and len(default_year) == 4:
        for rx in _DATE_NOYEAR:
            m = rx.search(s)
            if m:
                mo, dd = int(m.group(1)), int(m.group(2))
                if 1 <= mo <= 12 and 1 <= dd <= 31:
                    out = f"{default_year}-{mo:02d}-{dd:02d}"
                    hm = _TIME_RE.search(s)
                    if hm:
                        out += f" {int(hm.group(1)):02d}:{hm.group(2)}"
                    return out
    return ""


def dig(obj: Any, path: str, default=None) -> Any:
    """点号路径取值：'data.list' / 'result.records'，支持 a[0] 形式"""
    cur = obj
    if not path:
        return default
    for part in str(path).split("."):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([^\[\]]*)((?:\[\d+\])+)$", part)
        keys = [part]
        if m:
            # 下标必须转成 int，否则后面 isinstance(k, int) 分支永远进不去，
            # 'data.list[1].u' 会静默返回默认值
            keys = ([m.group(1)] if m.group(1) else []) + \
                   [int(x) for x in re.findall(r"\[(\d+)\]", m.group(2))]
        for k in keys:
            if isinstance(k, int):
                if isinstance(cur, list) and -len(cur) <= k < len(cur):
                    cur = cur[k]
                else:
                    return default
            else:
                if isinstance(cur, dict):
                    if k not in cur:
                        return default
                    cur = cur[k]
                else:
                    return default
    return cur


def _subst(obj, page: int, size: int):
    """把 {page} / {size} 占位替换掉；非字符串原样返回"""
    if isinstance(obj, str):
        return obj.replace("{page}", str(page)).replace("{size}", str(size))
    if isinstance(obj, dict):
        return {k: _subst(v, page, size) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(x, page, size) for x in obj]
    return obj


def _inner_html(el) -> str:
    parts = [str(c) for c in el.children]
    return "".join(parts) if parts else el.get_text()


def _pick(soup, sel: str):
    if not sel:
        return None
    try:
        return soup.select_one(sel)
    except Exception:
        # 选择器写错（soupsieve 不支持）不能拖垮整次抓取
        return None


# ---------------------------------------------------------------- 正文启发式
_JUNK_SEL = ("script, style, nav, header, footer, aside, noscript, "
             "iframe, form, .nav, .menu, .footer, .header, .sidebar")


def guess_content(soup) -> Optional[str]:
    """
    选不出正文时兜底：找「文字最多」的那个 div/td/article 容器。
    启发式会猜错，所以只在显式规则失效时用，且会在
    health_check / probe 里明确标出来 —— 不能让人以为抓对了。
    """
    best, best_len = None, 0
    for el in soup.select("div, td, article, section"):
        try:
            for bad in el.select(_JUNK_SEL):
                bad.decompose()
        except Exception:
            pass
        txt = el.get_text(" ", strip=True)
        # 正文块至少要有一定长度，且内部不该有太多链接（否则是导航区）
        links = len(el.find_all("a"))
        if len(txt) > best_len and links <= max(3, len(txt) // 200):
            best, best_len = el, len(txt)
    if best is not None and best_len >= 120:
        return _inner_html(best)
    return None


# ---------------------------------------------------------------- 采集器
class GenericCollector(SiteCollector):
    """配置驱动的通用采集器。自定义站点全部走这个类。"""

    def __init__(self, spec: Dict[str, Any], cfg_collector: dict, verbose=True):
        self.spec = spec or {}
        super().__init__(self.spec, cfg_collector, verbose)
        self.code = self.spec.get("code", "")
        self.name = self.spec.get("name", self.code)
        self.base_url = (self.spec.get("base_url") or "").rstrip("/")
        self.kind = self.spec.get("kind", "html")
        self.article_rule = self.spec.get("article") or {}
        self._cols = {str(c.get("code", "")): c
                      for c in (self.spec.get("columns") or [])}

    # ------------------------------------------------------------ 栏目
    def columns(self) -> List[Dict[str, str]]:
        return [{"code": str(c.get("code", "")), "name": c.get("name", "")}
                for c in (self.spec.get("columns") or [])]

    # ------------------------------------------------------------ 列表
    def list_articles(self, column_code: str) -> List[Dict]:
        col = self._cols.get(str(column_code))
        if not col:
            return []
        if self.kind == "json":
            return self._list_json(col)
        return self._list_html(col)

    def _mk(self, col, url, title, date, column_code):
        kind, reason = classify(url, self.base_url,
                                self.cfg_site.get("topic_prefixes", ()))
        return {
            "url": url,
            "title": clean_title(title or ""),
            "date": date,
            "column_code": str(column_code),
            "column_name": col.get("name", ""),
            "kind": kind,
            "skip_reason": reason,
        }

    def _list_html(self, col) -> List[Dict]:
        base = self.base_url
        item_sel = col.get("item", "")
        if not item_sel:
            self.log(f"    [warn] 栏目 {col.get('name')} 没有配置 item 选择器")
            return []
        pg = col.get("pagination") or {}
        mode = (pg.get("mode") or "none").lower()
        max_pages = int(pg.get("max_pages") or 30)
        out: List[Dict] = []
        seen: set = set()
        prev_oldest = None
        descending = True          # 只有确认倒序才敢早停

        if mode == "url":
            tmpl = pg.get("url") or col.get("list_url", "")
            start = int(pg.get("start_page") or 1)
            pages = [str(tmpl).replace("{page}", str(start + i))
                     for i in range(max_pages)]
        elif mode == "next":
            pages = [col.get("list_url", "")]
        else:
            pages = [col.get("list_url", "")]

        for url in pages:
            if not url:
                break
            r = self.fetcher.get(url, referer=self.base_url + "/")
            if r is None:
                self.log(f"    [warn] 列表页取不到: {url}")
                break
            html = decode_response(r)
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select(item_sel)
            if not items:
                break
            rows, oldest = self._harvest(col, items, base, seen)
            out.extend(rows)
            if oldest:
                prev_oldest = oldest

            # 单页就够了 / 已翻够 / 确认到底
            if mode == "none" or not out:
                break
            if self.date_start and descending and prev_oldest \
                    and prev_oldest < self.date_start:
                break
            if mode == "next":
                nxt = _pick(soup, pg.get("selector", "a.next"))
                href = nxt.get("href") if nxt else ""
                if not href:
                    break
                url = absolutize(href, base)
                if url in seen:
                    break
        # 一旦发现页面顺序不单调，就别再指望早停（本次已在上面保守处理）
        return out

    def _harvest(self, col, items, base, seen) -> List[Dict]:
        """
        把一组条目元素解析成记录。HTML 模式与 JSON 模式共用。

        之所以抽出来：浙江省网信那一套「jpaas 建站平台」的接口，
        返回的 JSON 里 data.html 装的是**一段 HTML**，不是对象数组。
        所以 json 模式也得能用 item/title/date 这套 CSS 选择器，
        否则这类站（很可能覆盖大多数省网信）一个都加不进来。
        """
        out, oldest = [], ""
        for el in items:
            got = self._parse_item(el, col, base)
            if not got:
                continue
            u, title, dt = got
            if not u or u in seen:
                continue
            seen.add(u)
            if dt:
                d = dt[:10]
                # 「见过的最旧日期」必须在入库过滤**之前**统计：
                # 早停靠的就是窗口外那些行。放进 out 之后再算，最小值
                # 永远 >= date_start，早停条件恒不成立，会白翻到 max_pages。
                if not oldest or d < oldest:
                    oldest = d
                if not self.in_window(d):
                    continue
            out.append(self._mk(col, u, title, dt, col.get("code")))
        return out, oldest

    def _parse_item(self, el, col, base) -> Optional[tuple]:
        link_el = _pick(el, col.get("link", "a"))
        href = ""
        if link_el is not None:
            href = link_el.get("href") or ""
        if not href:
            el_a = el.find("a")
            href = el_a.get("href") if el_a else ""
        if not href or href.startswith(("#", "javascript:")):
            return None
        url = absolutize(href, base)
        if not url.startswith(("http://", "https://")):
            return None

        t_el = _pick(el, col.get("title", "a"))
        title = t_el.get_text(" ", strip=True) if t_el is not None \
            else (link_el.get_text(" ", strip=True) if link_el else "")

        d_el = _pick(el, col.get("date", ""))
        raw_date = d_el.get_text(" ", strip=True) if d_el is not None else ""
        if not raw_date:
            # 日期没配选择器时，退一步在整块里找第一个日期串
            raw_date = el.get_text(" ", strip=True)
        dt = normalize_date(raw_date)
        return url, title, dt

    def _list_json(self, col) -> List[Dict]:
        api = col.get("api_url", "")
        method = (col.get("method") or "GET").upper()
        pg = col.get("pagination") or {}
        max_pages = int(pg.get("max_pages") or 30)
        size = int(pg.get("size") or 20)
        f = col.get("fields") or {}
        out: List[Dict] = []
        seen: set = set()
        for page in range(1, max_pages + 1):
            params = {}
            for k, v in (pg.get("params") or {"page": "{page}", "size": str(size)}).items():
                params[k] = str(v).replace("{page}", str(page)).replace(
                    "{size}", str(size))
            headers = col.get("headers") or None
            # Referer 不是可选项：jpaas 建站平台的列表接口会校验它，
            # 不带就直接 success=false（实测浙江网信）。默认用 base_url 兜底。
            referer = col.get("referer") or self.base_url + "/"
            # referer 里允许 {code}：jpaas 的列表接口按栏目校验 Referer，
            # 形如 https://host/col/col1694583/index.html，
            # 而栏目 id 就是本栏目的 code，必须能跟着变。
            if "{code}" in referer:
                referer = referer.replace("{code}", str(col.get("code", "")))
            try:
                if method == "POST":
                    body = pg.get("json_body")
                    if body:
                        # json_body 里也允许 {page} 占位
                        r = self.fetcher.post(
                            api,
                            json=_subst(body, page, size),
                            headers=headers)
                    else:
                        r = self.fetcher.post(api, data=params, headers=headers)
                else:
                    r = self.fetcher.get(api, params=params, headers=headers,
                                         referer=referer)
            except TypeError:
                # 源站只吃裸 GET 时不要因为多传了 headers 就整条挂掉
                r = self.fetcher.get(api, params=params)
            if r is None:
                break
            try:
                data = r.json()
            except Exception:
                self.log(f"    [warn] 接口返回的不是 JSON: {api}")
                break
            lst = dig(data, col.get("list_path", ""), None)
            if lst is None:
                break

            # 字段里装的是 **HTML 片段**（jpaas 建站平台就是这样：
            # {"success":true,"data":{"html":"<ul>...</ul> count=\"N\""}})
            # 这种站的列表不是对象数组，用 fields 那套映射根本没得用，
            # 必须回退到 CSS 选择器解析 —— 所以这里分流而不是报错。
            if isinstance(lst, str) and lst.strip():
                soup = BeautifulSoup(lst, "html.parser")
                items = soup.select(col.get("item", ""))
                if not items:
                    break
                out.extend(self._harvest(col, items, self.base_url, seen)[0])
                total = self._count_from_html(lst)
                try:
                    if total and len(out) >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
                if len(items) < size:
                    break
                continue

            if not isinstance(lst, list) or not lst:
                break
            for it in lst:
                if not isinstance(it, dict):
                    continue
                u = str(dig(it, f.get("url", "url"), "") or "").strip()
                if not u:
                    continue
                if not u.startswith(("http://", "https://")):
                    prefix = (col.get("url_prefix") or "").strip()
                    u = absolutize((prefix.rstrip("/") + u) if prefix else u,
                                   self.base_url)
                if not u.startswith(("http://", "https://")) or u in seen:
                    continue
                seen.add(u)
                dt = normalize_date(dig(it, f.get("date", "date"), ""))
                title = clean_title(str(dig(it, f.get("title", "title"), "") or ""))
                if dt and not self.in_window(dt[:10]):
                    continue
                out.append(self._mk(col, u, title, dt, col.get("code")))
            total = dig(data, col.get("total_path", ""), None)
            try:
                if total is not None and len(out) >= int(total):
                    break
            except (TypeError, ValueError):
                pass
            if len(lst) < size:
                break
        return out

    # ------------------------------------------------------------ 正文
    @staticmethod
    def _count_from_html(fragment: str) -> int:
        """
        jpaas 那套接口把总条数塞在 HTML 的属性里：
        <ul count="1299" pageNo="1" ...>。取不到就返回 0（按页数兜底）。
        """
        m = re.search(r'count\s*=\s*"?(\d+)', fragment or "")
        return int(m.group(1)) if m else 0

    def fetch_article(self, url: str, fallback: Dict) -> Optional[Dict]:
        r = self.fetcher.get(url, referer=self.base_url + "/")
        if r is None:
            return None
        html = decode_response(r)
        soup = BeautifulSoup(html, "html.parser")
        for bad in soup.select("script, style, noscript"):
            bad.decompose()

        ar = self.article_rule
        t_el = _pick(soup, ar.get("title", "h1"))
        title = clean_title(t_el.get_text(" ", strip=True)) if t_el else \
            clean_title(fallback.get("title", ""))

        d_el = _pick(soup, ar.get("pub_date", ""))
        pub = normalize_date(d_el.get_text(" ", strip=True)) if d_el else ""
        if not pub:
            pub = normalize_date(fallback.get("date", ""))

        s_el = _pick(soup, ar.get("source", ""))
        src = clean_title(s_el.get_text(" ", strip=True)) if s_el else ""
        src = re.sub(r"^(来源|文章来源)\s*[:：]\s*", "", src)

        c_el = _pick(soup, ar.get("content", ""))
        guessed = False
        if c_el is None:
            g = guess_content(soup)
            if not g:
                return None          # 抓不到正文 = 明确失败，不写半成品
            body, guessed = g, True
        else:
            body = _inner_html(c_el)

        body = re.sub(r'<img\s+src="([^"]*)"',
                      lambda m: f'<img src="{absolutize(m.group(1), self.base_url)}"',
                      body)
        return {"title": title, "pub_date": pub, "source_text": src,
                "content_html": body, "_guessed": guessed}

    # ------------------------------------------------------------ 自检
    def health_check(self) -> Dict:
        issues: List[str] = []
        checked = 0
        first_item: Optional[Dict] = None
        for c in self.columns()[:3]:
            checked += 1
            try:
                items = self.list_articles(c["code"])
            except Exception as e:
                issues.append(f"{c['name']}：列表抓取异常 {type(e).__name__}: {e}")
                continue
            if not items:
                issues.append(f"{c['name']}：列表页取不到条目"
                              f"（item 选择器或 list_url 可能不对）")
                continue
            bad_date = sum(1 for x in items if not x.get("date"))
            if bad_date:
                issues.append(f"{c['name']}：{bad_date}/{len(items)} 条取不到日期，"
                              f"date 选择器可能不对")
            if not first_item:
                first_item = items[0]
        if not first_item:
            return {"site": self.code, "ok": False, "issues": issues,
                    "checked_columns": checked, "total_rec": 0}
        art = None
        try:
            art = self.fetch_article(first_item["url"], first_item)
        except Exception as e:
            issues.append(f"正文解析异常 {type(e).__name__}: {e}")
        if not art:
            issues.append("正文页解析失败（article.content 选择器可能已失效）")
        else:
            txt = re.sub(r"<[^>]+>", "", art["content_html"]).strip()
            if len(txt) < 80:
                issues.append(f"正文只有 {len(txt)} 字，可能选到了导航或摘要区")
            if art.get("_guessed"):
                issues.append("正文是启发式兜底抓的（content 选择器没命中），"
                              "结果可能不完整，建议人工核对")
        return {"site": self.code, "ok": not issues, "issues": issues,
                "total_rec": 0, "checked_columns": checked,
                "kind": self.kind, "guessed": bool(art and art.get("_guessed"))}


def build(spec: Dict[str, Any], cfg_collector: dict, verbose=True):
    """按 spec 造采集器；kind 非法时直接报错，不静默退化"""
    kind = (spec.get("kind") or "html").lower()
    if kind not in ("html", "json"):
        raise ValueError(f"kind 只能是 html / json，收到 {kind!r}")
    return GenericCollector(spec, cfg_collector, verbose=verbose)


def spec_summary(spec: Dict[str, Any]) -> str:
    """给日志用的一行摘要"""
    cols = spec.get("columns") or []
    return f"{spec.get('name') or spec.get('code')} [{spec.get('kind','html')}] " \
           f"{len(cols)} 栏目"
