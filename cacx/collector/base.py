# -*- coding: utf-8 -*-
"""
采集器公共基类
================================================================
抽取两站共用的：HTTP 会话、重试、限速、正文抽取、HTML→纯文本/Markdown。
子类只需实现 `list_articles()` 与 `fetch_article()`。
"""

import html as html_mod
import re
import time
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import Dict, List, Optional

import requests


class Fetcher:
    """带重试与限速的 HTTP 客户端"""

    def __init__(self, ua: str, timeout: int = 25, retry: int = 3,
                 interval: float = 0.35):
        self.timeout = timeout
        self.retry = retry
        self.interval = interval
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ua,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._last = 0.0

    def _throttle(self):
        dt = time.time() - self._last
        if dt < self.interval:
            time.sleep(self.interval - dt)
        self._last = time.time()

    def get(self, url: str, referer: str = "", headers=None,
            **kw) -> Optional[requests.Response]:
        # referer 与 headers 要**合并**，不能一个拼好一个再传下去：
        # 否则调用方同时给 referer 和 headers 时，
        # session.request 会撞上「headers 重复传」的 TypeError。
        # json 模式的列就正好同时传这两个（jpaas 接口校验 Referer），
        # 之前被上层 except TypeError 吞成裸请求，表现为「列表一条都抓不到」。
        h = dict(headers or {})
        if referer:
            h.setdefault("Referer", referer)
        return self._do("GET", url, headers=h, **kw)

    def post(self, url: str, data=None, headers=None, **kw) -> Optional[requests.Response]:
        return self._do("POST", url, data=data, headers=headers, **kw)

    def _do(self, method, url, **kw):
        for i in range(self.retry):
            self._throttle()
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
                if r.status_code == 200:
                    return r
            except requests.RequestException:
                pass
            time.sleep(self.interval * (i + 1) * 2)
        return None


# ---------------------------------------------------------------- 正文→纯文本
class TextCollector(HTMLParser):
    """HTML 正文 → 干净纯文本。段落间换行，图片记为 [图:URL]。"""

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.buf = []
        self.skip = 0

    def _abs(self, src):
        if not src:
            return ""
        src = src.strip()
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/") and self.base:
            return self.base.rstrip("/") + src
        return src

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.buf.append("\n")
        elif tag == "img":
            d = dict(attrs)
            self.buf.append(f" [图:{self._abs(d.get('src',''))}] ")
        elif tag == "td":
            self.buf.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in ("p", "div", "tr", "li", "h1", "h2", "h3",
                                      "h4", "h5", "h6"):
            self.buf.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def text(self) -> str:
        raw = "".join(self.buf).replace("\xa0", " ").replace("　", " ")
        lines = [re.sub(r"[ \t]+", " ", l).strip() for l in raw.split("\n")]
        out, blank = [], 0
        for l in lines:
            if not l:
                blank += 1
                if blank:
                    continue
            else:
                blank = 0
            out.append(l)
        return "\n".join(out).strip()


def html_to_text(content_html: str, base_url: str = "") -> str:
    c = TextCollector(base_url)
    try:
        c.feed(content_html)
        c.close()
    except Exception:
        pass
    return c.text()


def absolutize(src: str, base_url: str) -> str:
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return base_url.rstrip("/") + src
    return src


# requests 对 `Content-Type: text/html`（没有 charset）会**擅自**把
# r.encoding 设成 ISO-8859-1，于是每一个中文字符都变成「Ã¥â»¨」
# 这种乱码。国内政务站有大量这种不声明 charset 的页面，
# 而这里不会报任何错 —— 乱码会一路写进数据库，直到人真去读才发现。
# 所以编码判定必须自己来。
_DEFAULT_ENCODINGS = {"iso-8859-1", "latin-1", "latin1", "ascii"}


def decode_response(r) -> str:
    """拿到响应正文（str），编码判定顺序：
        1) 响应头里**真的**声明了 charset（不是 requests 的默认猜测）→ 用它
        2) 内容能严格按 UTF-8 解码 → utf-8
        3) 能严格按 gb18030 解码 → gb18030（GBK/GB2312 的超集）
        4) 兜底用 requests 的 apparent_encoding
    """
    enc = (r.encoding or "").lower().strip()
    if enc and enc not in _DEFAULT_ENCODINGS:
        try:
            return r.content.decode(enc, errors="replace")
        except (LookupError, TypeError):
            pass
    for cand in ("utf-8", "gb18030"):
        try:
            text = r.content.decode(cand)          # strict，失败就换下一个
        except (UnicodeDecodeError, LookupError):
            continue
        # 全是 ASCII 时两种编码结果一样，不必再试
        return text
    return r.text


def charset_note(r) -> str:
    """一句话说明这个响应的编码是怎么定的，给试抓报告用（排查乱码时最有用）"""
    declared = (r.encoding or "").lower().strip()
    if declared and declared not in _DEFAULT_ENCODINGS:
        return f"响应头声明 {declared}"
    for cand in ("utf-8", "gb18030"):
        try:
            r.content.decode(cand)
            return ("按内容严格解码判定为 " + cand +
                    "（响应头未声明 charset）")
        except (UnicodeDecodeError, LookupError):
            continue
    return "编码无法确定，已按 apparent_encoding 尽力解码"


def clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(
        re.sub(r"<[^>]*>", "", t or ""))).strip()


# ---------------------------------------------------------------- 站点协议
class SiteCollector(ABC):
    """站点采集器协议"""

    code: str = ""          # cac | zjwx
    name: str = ""          # 中国网信办
    base_url: str = ""

    def __init__(self, cfg_site: dict, cfg_collector: dict, verbose: bool = True):
        self.cfg_site = cfg_site
        self.verbose = verbose
        self.fetcher = Fetcher(
            ua=cfg_collector.get("user_agent", ""),
            timeout=cfg_collector.get("timeout", 25),
            retry=cfg_collector.get("retry", 3),
            interval=cfg_collector.get("interval", 0.35),
        )
        self.date_start = cfg_site.get("date_start", "2024-01-01")
        self.date_end = cfg_site.get("date_end", "2026-12-31")

    @abstractmethod
    def columns(self) -> List[Dict[str, str]]:
        """[{code, name}]"""

    @abstractmethod
    def list_articles(self, column_code: str) -> List[Dict]:
        """返回 [{url, title, date, column_code, column_name, ...}]"""

    @abstractmethod
    def fetch_article(self, url: str, fallback: Dict) -> Optional[Dict]:
        """返回 {title, pub_date, source_text, content_html} 或 None"""

    # -------- 通用工具
    def in_window(self, day: str) -> bool:
        return bool(day) and self.date_start <= day <= self.date_end

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def health_check(self) -> Dict:
        """doctor 用：探一个接口，看返回结构是否符合预期"""
        raise NotImplementedError