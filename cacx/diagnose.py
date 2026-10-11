# -*- coding: utf-8 -*-
"""
API 接口检测工具（probe_api）
================================================================
为什么单独做这个模块：
    现有的 `site probe` / `doctor` 都要先把 URL 包成一份站点配置才能跑，
    而对接一个新站时最常见的问题恰恰是「这个地址到底能不能用、返回了什么」。
    本模块给一个任意 URL 就给出一份只读诊断报告，定位新站接口时不用先造配置。

设计铁律（与对接文档 §3.2 / §7 对齐）：
    · **全程只读**：不写数据库、不写任何配置文件。probe_api 里没有任何
      open(..., 'w') / sqlite / 站点注册表写路径 —— 这是「可以对着生产接口
      随便打」的前提，测试里也专门钉死了这一点。
    · **能看到非 200 与 success:false@200**：现成的 Fetcher._do 只在
      status==200 时才返回响应，其余一律吞成 None。而 jpaas 平台失败时
      恰恰是 HTTP 200 + {"success":false}，靠 Fetcher 根本诊断不到，
      所以这里自己持有 requests.Session，原样返回任意状态码的响应。
    · 编码判定复用 base.decode_response：requests 对无 charset 的响应
      擅自按 ISO-8859-1 解码，中文会静默变乱码（§6.3），不能用 r.text。
"""

import re
import time
from typing import Any, Dict, Optional

import requests

# bs4 在本项目是 generic.py / rules.py 的间接依赖。这里单独再做一次
# try/except 降级：诊断工具在没装 bs4 的干净环境里也应能跑出「能连通、
# 但选择器推断不可用」的报告，而不是直接 ImportError 崩掉。
try:
    from bs4 import BeautifulSoup
except ImportError:                               # pragma: no cover
    BeautifulSoup = None  # type: ignore

from .collector.base import decode_response

# 与 config/settings.example.json 里的采集器 UA 保持一致：政务站会按
# UA 拦截明显的脚本客户端，探测时用浏览器 UA 才贴近真实访问。
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")

# requests 把这些当「猜测出来的默认编码」，不是服务器真正声明的，
# 判定 charset_used 时不能把它当真（见 base._DEFAULT_ENCODINGS 的注释）。
_DEFAULT_ENCODINGS = {"iso-8859-1", "latin-1", "latin1", "ascii"}

# 静态资源后缀：命中即说明这不是数据接口，不能往下走列表推断。
_STATIC_RE = re.compile(
    r"\.(js|css|png|jpe?g|gif|ico|svg|woff2?|ttf|map|mp4)(\?|$)", re.I)

# 「结构像成功、内容其实是错误文案」的关键词。不能只靠单个关键词硬匹配，
# 所以下面的判断是双向的：先看正文是不是短到反常，再叠关键词佐证。
_ERROR_MARKERS = ("不能为空", "模版", "模板", "参数错误", "请求错误", "系统错误",
                  "页面不存在", "无权限", "未登录", "非法请求", "操作失败",
                  "请稍后", "暂无数据")

# 一段字符串「看起来是 HTML 片段」而不是普通文本/JSON 字段。
_HTML_TAG_RE = re.compile(r"<\s*(ul|ol|li|div|table|tr|p|a)[\s>]", re.I)


# ---------------------------------------------------------------- 网络
def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _DEFAULT_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 探测目标多半是 JSON 列表接口，Accept 显式带上，避免部分站按
        # 浏览器导航返回整页 HTML 而掩盖了真实接口形态。
        "Accept": "application/json, text/html, */*;q=0.8",
    })
    return s


def _do(session, method, url, headers, params, body, referer, timeout=25):
    """
    打一次请求。referer 与 headers 必须合并：照搬 base.Fetcher.get 踩过的坑，
    同时给 referer 和 headers 时若分别透传会撞 TypeError，被上层吞成裸请求。
    异常返回 None（和 Fetcher 语义一致），由调用方记成 issues，不抛出去。
    """
    h = dict(headers or {})
    if referer:
        h.setdefault("Referer", referer)
    kw: Dict[str, Any] = {"headers": h, "timeout": timeout}
    if params:
        kw["params"] = params
    m = (method or "GET").upper()
    try:
        if m == "POST":
            if body is not None:
                # dict/list 走 json 体，其余（字符串等）原样当 data
                if isinstance(body, (dict, list)):
                    kw["json"] = body
                else:
                    kw["data"] = body
            return session.request("POST", url, **kw)
        return session.request("GET", url, **kw)
    except requests.RequestException:
        return None


# ---------------------------------------------------------------- 编码
def _charset_split(r) -> tuple:
    """
    返回 (declared, used)。
    declared = 服务器真声明的字符集（Content-Type 里的 charset=，或非默认
               的 r.encoding）；没声明就是空串。
    used     = 实际解码用的那个：优先真声明，否则按内容严格试 utf-8/gb18030。
    """
    ct = r.headers.get("Content-Type", "") or ""
    m = re.search(r"charset=([^\s;]+)", ct, re.I)
    declared = (m.group(1).strip().strip('"').lower() if m else "")
    if not declared:
        enc = (r.encoding or "").lower().strip()
        if enc and enc not in _DEFAULT_ENCODINGS:
            declared = enc
    # 实际用哪个编码解的，照 base.decode_response 的判定顺序走一遍
    if declared:
        return declared, declared
    for cand in ("utf-8", "gb18030"):
        try:
            r.content.decode(cand)
            return declared, cand
        except (UnicodeDecodeError, LookupError):
            continue
    return declared, getattr(r, "apparent_encoding", "") or "unknown"


# ---------------------------------------------------------------- JSON 结构探测
def _find_first_list(obj: Any, prefix: str = "") -> tuple:
    """
    递归找 JSON 里第一个「list 类型的值」，返回 (点号路径, 那个列表)。
    DFS 即可：对象数组型接口的列表通常只在一个字段里，找到第一个就够。
    """
    if isinstance(obj, list):
        return prefix, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            found_path, found = _find_first_list(v, p)
            if isinstance(found, list):
                return found_path, found
    return "", None


def _looks_html_string(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    return len(s) >= 5 and bool(_HTML_TAG_RE.search(s))


def _find_html_string(obj: Any, prefix: str = "") -> tuple:
    """
    递归找「值是 HTML 片段的字符串字段」，返回 (点号路径, 字符串)。
    jpaas 的形态是 {"data":{"html":"<ul>..."}} —— 字段名可能不叫 html，
    所以靠「字符串里有 HTML 标签」判断，而不是写死字段名。
    """
    if isinstance(obj, str) and _looks_html_string(obj):
        return prefix, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            found_path, found = _find_html_string(v, p)
            if found:
                return found_path, found
    return "", None


def _find_short_string(obj: Any, prefix: str = "") -> tuple:
    """
    递归找「短字符串叶子」——用来兜底识别坑 #3 的错误文案。
    它不是 HTML（没有标签），分支 B 抓不到；但 success:true 时 data 里
    若躺着一个 <=60 字的短串，很可能就是「模版内容不能为空！」这类占位。
    判不判成错误由上层双向规则决定，这里只负责把候选找出来。
    """
    if isinstance(obj, str):
        if 0 < len(obj.strip()) <= 60:
            return prefix, obj
        return "", None
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            fp, fs = _find_short_string(v, p)
            if fs is not None:
                return fp, fs
    return "", None


# ---------------------------------------------------------------- HTML 片段
def _count_from_html(fragment: str) -> int:
    """
    jpaas 把总条数塞在 ul 属性里：<ul count="331" pageNo="1">。
    正则与 generic.GenericCollector._count_from_html 保持一致，
    否则同一个站在两处算出不同总数会很困惑。
    """
    m = re.search(r'count\s*=\s*"?(\d+)', fragment or "")
    return int(m.group(1)) if m else 0


def _looks_error_fragment(html_str: str) -> tuple:
    """
    识别「结构对但内容是错误文案」（实测坑 #3）。

    为什么是双向判断而不是单关键词：
      · 单关键词「不能为空」太脆 —— 万一真列表页里某条标题恰好带这几个字，
        就被误判成错误页，整条接口被否掉。
      · 只看长度也不够 —— 有的错误页文案稍长（「参数错误，请重试」）。
    所以要求「**正文短到反常**」叠「含错误关键词」才判错；
    9 字的「模版内容不能为空！」在第一档（<=15 字）就直接命中。
    """
    text = re.sub(r"<[^>]+>", "", html_str or "")
    text = re.sub(r"\s+", "", text)
    if not text:
        return True, "空内容"
    if len(text) <= 15:
        return True, f"正文仅 {len(text)} 字（真列表页不可能这么短）"
    if len(text) <= 60:
        hit = next((m for m in _ERROR_MARKERS if m in text), "")
        if hit:
            return True, f"含错误关键词「{hit}」：{text[:30]}"
    return False, ""


def _rel_el_sel(el) -> str:
    """条目内部元素的相对选择器（带 class 才精确，否则退回裸标签）。"""
    if el is None:
        return ""
    tag = el.name or ""
    cls = [c for c in (el.get("class") or [])
           if isinstance(c, str) and re.match(r"^[A-Za-z][\w-]*$", c)]
    return f"{tag}.{cls[0]}" if cls else tag


def _container_sel(el) -> str:
    """
    列表容器的选择器，必须带父级上下文（§6.4）：
      · 裸 `ul li` 在浙江整页会匹配 25 条，其中 15 条是导航推荐位，
        目标栏目只有 10 条 —— 过宽等于把推荐位也当成文章。
      · 所以优先用容器自己的 id/class 钉住它：`ul.pagination li`。
      · 实在没有 class/id 才退回 `ul li`，并在 issues 里提示置信度低。
    """
    tag = el.name or "ul"
    eid = el.get("id")
    if eid and re.match(r"^[A-Za-z][\w-]*$", str(eid)):
        return f"{tag}#{eid} li"
    cls = [c for c in (el.get("class") or [])
           if isinstance(c, str) and re.match(r"^[A-Za-z][\w-]*$", str(c))]
    if cls:
        return f"{tag}.{cls[0]} li"
    return f"{tag} li"


def _pick_list_container(soup):
    """
    从一段 HTML（含 JSON 里解出来的片段）里挑出「主列表容器」。
    返回 (item选择器, title选择器, date选择器, 命中条数)。
    """
    # 导航/页脚/侧栏里的 ul 是推荐位，不是主列表。浙江实测：整页 ul li
    # 25 条里 15 条是导航推荐 —— 先把这些区域剥掉，避免选错容器。
    for bad in soup.select("nav, header, footer, aside"):
        bad.decompose()

    # 优先带 count 属性的容器：jpaas 用它标总条数，它几乎一定是主列表。
    containers = soup.find_all(attrs={"count": True})
    if not containers:
        containers = soup.find_all(["ul", "ol"])

    def _real_li(c):
        n = 0
        for li in c.find_all("li"):
            a = li.find("a", href=True)
            href = (a.get("href") or "").strip() if a else ""
            if a and href.startswith(("/", "http")) and not href.startswith("#"):
                n += 1
        return n

    best, best_n = None, 0
    for c in containers:
        n = _real_li(c)
        if n > best_n:
            best, best_n = c, n

    if best is None or best_n < 2:
        return "", "", "", 0

    item_sel = _container_sel(best)

    # 以第一条真条目为模板，反推 title / date 的相对选择器
    first_li = None
    for li in best.find_all("li"):
        a = li.find("a", href=True)
        href = (a.get("href") or "").strip() if a else ""
        if a and href.startswith(("/", "http")) and not href.startswith("#"):
            first_li = li
            break
    title_sel = _rel_el_sel(first_li.find("a")) if first_li else "a"
    date_sel = ""
    if first_li is not None:
        for el in first_li.find_all(["span", "div", "em", "p", "time"]):
            t = el.get_text(" ", strip=True)
            if t and re.search(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}", t):
                date_sel = _rel_el_sel(el)
                break
    return item_sel, title_sel, date_sel, best_n


# ---------------------------------------------------------------- 业务成败指纹
def _business_failed(r) -> Optional[bool]:
    """
    抽一次响应的业务成败：True=业务失败，False=业务成功，None=无法判断
    （不是 JSON / 没有 success 字段）。这是「去掉 Referer 前后对比」的关键，
    因为 jpaas 不带 Referer 时返回的是 HTTP 200 + success:false，
    只比状态码永远一样，必须比业务标记。
    """
    try:
        d = r.json()
    except Exception:
        return None
    if isinstance(d, dict) and "success" in d:
        return d.get("success") is False
    return None


# ---------------------------------------------------------------- 主入口
def probe_api(url: str, method: str = "GET",
              headers: Optional[dict] = None,
              params: Optional[dict] = None,
              body: Any = None,
              referer: str = "") -> Dict[str, Any]:
    """
    对任意接口做只读诊断。返回字段见对接文档 §3.2。
    全程不写数据库、不写任何配置文件。
    """
    result: Dict[str, Any] = {
        "ok": False,
        "http_status": 0,
        "content_type": "",
        "charset_declared": "",
        "charset_used": "",
        "elapsed_ms": 0,
        "is_json": False,
        "json_top_keys": [],
        "list_path_guess": "",
        "count": 0,
        "referer_required": False,
        "sample": "",
        "issues": [],
        "suggested": {
            "api_url": url,
            "method": (method or "GET").upper(),
            "referer": referer,
            "list_path": "",
            "params": dict(params or {}),
        },
    }
    issues = result["issues"]

    url = (url or "").strip()
    if not url:
        issues.append("URL 为空")
        return result
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
        result["suggested"]["api_url"] = url

    session = _make_session()

    # ---- 第一打：带给定 referer（若有） ----
    t0 = time.perf_counter()
    r1 = _do(session, method, url, headers, params, body, referer)
    result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

    if r1 is None:
        issues.append("请求失败（连接错误 / 超时 / 被拒），未拿到响应")
        return result

    result["http_status"] = r1.status_code
    ct = r1.headers.get("Content-Type", "") or ""
    result["content_type"] = ct
    result["charset_declared"], result["charset_used"] = _charset_split(r1)

    # 负例 #1：静态资源（.js/.css/图片）绝不能被当成数据接口。
    # jpaas 页面里会内嵌 /static/js/app.js 这类地址，若把它当接口推荐，
    # 用户照填后只会抓到一段脚本源码。
    ct_l = ct.lower()
    if (_STATIC_RE.search(url)
            or "javascript" in ct_l or "css" in ct_l
            or "image/" in ct_l or "octet-stream" in ct_l):
        issues.append("这是静态资源（JS/CSS/图片），不是数据接口，不能当列表采集")
        return result

    if r1.status_code != 200:
        issues.append(f"HTTP {r1.status_code}（非 200），接口不可用，"
                      "先确认地址与鉴权，而不是套选择器")
        return result

    text = decode_response(r1)
    result["sample"] = text[:300]

    # ---- 判定是 JSON 还是 HTML ----
    data = None
    try:
        data = r1.json()
        result["is_json"] = True
    except Exception:
        result["is_json"] = False

    usable = False
    if result["is_json"]:
        if isinstance(data, dict):
            result["json_top_keys"] = [str(k) for k in data.keys()]
        usable = _analyze_json(result, data, issues)
    else:
        usable = _analyze_html(result, text, url, issues)

    # ---- 坑 #1：Referer 依赖。只在调用方给了 referer 时才做对比 ——
    # 因为要「先带给定 referer 打一次、再不带打一次」，没给 referer 就没有
    # 对照组，无法判定。对比的是**业务成败**而非状态码（坑 #2 同源）。
    if referer:
        r2 = _do(session, method, url, headers, params, body, "")
        if r2 is not None:
            f1, f2 = _business_failed(r1), _business_failed(r2)
            diff_biz = (f1 is False) != (f2 is False)
            # 业务标记判不出来时，退一步比正文长度/哈希，避免漏判
            diff_body = False
            if not diff_biz:
                try:
                    diff_body = decode_response(r2)[:200] != text[:200]
                except Exception:
                    diff_body = r2.status_code != r1.status_code
            if diff_biz or diff_body:
                result["referer_required"] = True
                issues.append(
                    "去掉 Referer 后响应不同（业务标记或内容变了）——该接口按栏目"
                    "校验 Referer（jpaas 常见），站点配置里必须带 referer")

    result["ok"] = (result["http_status"] == 200
                    and usable
                    and not any("success" in i for i in issues))
    return result


def _analyze_json(result: Dict[str, Any], data, issues) -> bool:
    """返回是否「可用」（找到了可采集的列表）。"""
    suggested = result["suggested"]

    # 坑 #2：HTTP 200 + success:false。只看状态码会以为接口正常。
    if isinstance(data, dict) and data.get("success") is False:
        issues.append("HTTP 200 但业务标记 success=false —— 不是网络问题，"
                      "是参数不对或缺 Referer（jpaas 平台常见）")
        return False
    if isinstance(data, dict) and data.get("success") is True:
        # 带了 success:true 也别高兴太早，继续往下看 data 里到底装了什么
        pass

    # 分支 A：真正的对象数组
    path, arr = _find_first_list(data)
    if path and isinstance(arr, list):
        result["list_path_guess"] = path
        result["count"] = len(arr)
        suggested["list_path"] = path
        if arr and not all(isinstance(x, dict) for x in arr):
            issues.append(f"列表 {path} 里不全是对象，字段映射需手工确认")
        if len(arr) == 0:
            issues.append(f"列表 {path} 是空数组 —— 可能是翻页/参数不对")
            return False
        return True

    # 分支 B：JSON 里装的是 HTML 片段（坑 #4）
    html_path, html_str = _find_html_string(data)
    if html_path:
        result["list_path_guess"] = html_path
        suggested["list_path"] = html_path
        return _analyze_html_fragment(result, html_str, html_path, issues)

    # 分支 C：success:true 但主字段装的是**错误文案字符串**（坑 #3）。
    # 它没有 HTML 标签，所以分支 B 找不到；但它短得反常 / 带错误关键词。
    # 双向判断避免误伤：必须「含错误关键词」，或「字段名像 page/html 但
    # 内容 <=15 字」——像 {"success":true,"msg":"ok"} 这种正常短串不会被误判。
    err_path, err_str = _find_short_string(data)
    if err_path:
        plain = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", err_str or ""))
        has_marker = any(m in (err_str or "") for m in _ERROR_MARKERS)
        looks_container = any(k in err_path.lower()
                              for k in ("html", "content", "data", "page"))
        if has_marker or (len(plain) <= 15 and looks_container):
            result["list_path_guess"] = err_path
            issues.append(
                f"业务错误伪装成成功：{err_path} 的内容是「{plain[:30]}」"
                "（jpaas 带了多余/错误参数，如 tagId 时会返回『模版内容不能为空！』）")
            return False

    # 顶层直接就是数组
    if isinstance(data, list):
        result["count"] = len(data)
        return len(data) > 0

    issues.append("JSON 里既没找到对象数组，也没找到 HTML 片段字段，"
                  "结构需人工确认（list_path 留空，不要照抄）")
    return False


def _analyze_html_fragment(result: Dict[str, Any], html_str: str,
                           source: str, issues) -> bool:
    """分析 JSON 里解出来的 HTML 片段（jpaas 形态）。"""
    suggested = result["suggested"]

    # 坑 #3：结构对但内容是错误文案（9 字「模版内容不能为空！」）。
    # 必须在选选择器之前判掉，否则会把错误文案当 HTML 片段去配 item。
    is_err, why = _looks_error_fragment(html_str)
    if is_err:
        issues.append(f"业务错误伪装成成功：{source} 的内容是「{why}」"
                      "（jpaas 带了多余/错误参数，如 tagId 时会返回这种）")
        return False

    # 坑 #5：总条数藏在 HTML 属性里
    result["count"] = _count_from_html(html_str)

    if BeautifulSoup is None:
        issues.append("未安装 beautifulsoup4，无法从 HTML 片段反推 item/title/date"
                      "（pip install beautifulsoup4 后重试）")
        return result["count"] > 0

    soup = BeautifulSoup(html_str, "html.parser")
    item_sel, title_sel, date_sel, n = _pick_list_container(soup)
    if n < 2:
        issues.append(f"{source} 里没找到 >=2 条带链接的列表项，"
                      "要么不是列表片段，要么选择器需手工写")
        return False

    # 坑 #4 的核心建议：字段是 HTML，必须用 CSS 选择器，不能用 JSON 字段映射
    issues.append(
        f"{source} 装的是 HTML 片段而非对象数组 —— 用 CSS 选择器解析"
        f"（item={item_sel}, title={title_sel}, date={date_sel or '（待补）'}），"
        "不要用 JSON fields 映射")
    suggested["item"] = item_sel
    suggested["title"] = title_sel
    suggested["link"] = title_sel
    suggested["date"] = date_sel
    return True


def _analyze_html(result: Dict[str, Any], text: str, url: str, issues) -> bool:
    """分析非 JSON 的整页 HTML（导航页/正文页/真列表页都走这里）。"""
    # 负例 #2/#3：导航路径（如 /fwhd/）与无列表结构的页面，不得报告「可采集」。
    # 复用 rules.infer_list_rule —— 它已经把「裸 li 把导航选进来」
    # 「命中太多=选宽了」这些坑做成了打分门槛（>=5 条、每条一个文章链接、
    # >=60% 带日期）。整页 HTML 直接用它，比自己再写一套门槛可靠。
    if BeautifulSoup is None:
        issues.append("未安装 beautifulsoup4，无法做 HTML 列表结构判断")
        return False

    from .collector import rules as R
    rule = R.infer_list_rule(text, url)
    if rule.get("ok"):
        result["count"] = rule.get("hit_count", 0)
        result["suggested"].update({
            "list_path": "",            # HTML 模式不用 list_path
            "item": rule.get("item", ""),
            "title": rule.get("title", "a"),
            "link": rule.get("link", "a"),
            "date": rule.get("date", ""),
        })
        if rule.get("issues"):
            issues.extend(rule["issues"])
        return True

    issues.append("页面没有可识别的文章列表（可能是导航页/栏目首页/正文页，"
                  "或列表由 JS 异步加载）—— 不应直接当列表采集；"
                  "若这是 JS 加载的站，请改找它的 JSON 接口")
    return False
