# -*- coding: utf-8 -*-
"""
API 接口检测工具（cacx.diagnose.probe_api）离线回归测试
================================================================
运行：  python tests_diagnose.py

为什么这些断言这么写（照 tests_generic.py 的思路）：
  · probe_api 是「对着真实接口随便打」的诊断器，最容易犯两个错：
      a) 把不是接口的东西（静态 js / 导航页）判成可采集 —— 用户照配置填，
         只会抓到脚本源码或导航；所以必须配「不得误判」的负例。
      b) 把业务失败（success:false、9 字错误页）判成成功 —— 这种静默
         假阳性最害人，所以 5 个实测坑每个都有正例钉死。
  · 全部离线：用 unittest.mock 打桩 requests.Session.request，绝不真联网、
    绝不真调任何 API。
  · 安全：config/settings.json 含真实 MiniMax api_key。本测试只对它做
    sha256（绝不打印内容），用来证明 probe_api 跑完后它原封不动。
"""

import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def section(t):
    print(f"\n=== {t} ===")


# ---------------------------------------------------------------- 打桩
class FakeResp:
    """模拟 requests.Response：只提供 decode_response / r.json() 用到的属性"""

    def __init__(self, body, status=200, content_type="application/json",
                 encoding="utf-8"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.encoding = encoding
        self.apparent_encoding = "utf-8"
        self._content = body if isinstance(body, bytes) else body.encode("utf-8")

    @property
    def content(self):
        return self._content

    @property
    def text(self):
        return self._content.decode(self.encoding or "utf-8", "replace")

    def json(self):
        return json.loads(self._content.decode("utf-8"))


def _json_resp(obj, **kw):
    return FakeResp(json.dumps(obj, ensure_ascii=False), **kw)


# jpaas 平台实测形态（取自对接文档 §8 与 tests_generic.py 的离线复现）：
# 整页 HTML 里导航推荐位 + 真列表 ul 混在一起，真列表 ul 带 count= 属性。
JPAAS_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<nav><ul><li><a href="/col/col1673565/art/x.html">推荐位A</a></li>
  <li><a href="/col/col1673565/art/y.html">推荐位B</a></li></ul></nav>
<ul class="pagination" count="331" pageNo="1">
  <li><a class="bt-left" href="/col/col1694583/art/2026/art_a1.html">工作动态一</a>
      <span class="bt-right">2026-10-10</span></li>
  <li><a class="bt-left" href="/col/col1694583/art/2026/art_a2.html">工作动态二</a>
      <span class="bt-right">2026-09-30</span></li>
  <li><a class="bt-left" href="/col/col1694583/art/2026/art_a3.html">工作动态三</a>
      <span class="bt-right">2026-09-21</span></li>
</ul></body></html>"""

API = "https://www.zjwx.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit"
REFERER = "https://www.zjwx.gov.cn/col/col1694583/index.html"


def _run_probe(provider, **kw):
    """
    打桩 requests.Session.request 后调 probe_api。
    provider(method, url, request_kw) -> FakeResp；request_kw['headers']
    里能拿到 Referer，用来模拟「带/不带 Referer 两种响应」。
    """
    import cacx.diagnose as D

    with mock.patch.object(D.requests.Session, "request") as m:
        def side(method, url, **reqkw):
            return provider(method, url, reqkw)
        m.side_effect = side
        return D.probe_api(API, **kw)


def _has_referer(reqkw):
    return bool((reqkw.get("headers") or {}).get("Referer"))


# ---------------------------------------------------------------- 坑 #1 Referer 依赖
def test_referer_required():
    """带 Referer → success:true + 真列表；不带 → success:false"""

    def provider(method, url, reqkw):
        if _has_referer(reqkw):
            return _json_resp({"success": True,
                                "data": {"html": JPAAS_PAGE}})
        return _json_resp({"success": False, "data": None})

    r = _run_probe(provider, referer=REFERER)
    check("Referer 依赖被识别", r["referer_required"] is True, str(r["issues"]))
    check("带 Referer 时整体可用", r["ok"] is True, str(r["issues"]))
    check("报告里提示了 Referer",
          any("Referer" in i for i in r["issues"]), str(r["issues"]))


# ---------------------------------------------------------------- 坑 #2 success:false @200
def test_success_false_at_200():
    """HTTP 200 + 业务 success:false，不能因为状态码 200 就报可用"""

    def provider(method, url, reqkw):
        return _json_resp({"success": False,
                           "errorMsg": "referer校验失败", "data": None})

    r = _run_probe(provider, referer=REFERER)
    check("success:false 时 ok=False", r["ok"] is False, str(r))
    check("issues 点明 success=false",
          any("success=false" in i for i in r["issues"]), str(r["issues"]))
    check("状态码仍是 200（诊断要能看到它）", r["http_status"] == 200, str(r))


# ---------------------------------------------------------------- 坑 #3 9 字错误页伪装成功
def test_error_page_masquerade():
    """success:true 但 data.html 是 9 字错误文案，必须识破"""

    def provider(method, url, reqkw):
        return _json_resp({"success": True,
                           "data": {"html": "模版内容不能为空！"}})

    r = _run_probe(provider, referer=REFERER)
    check("错误伪装页 ok=False", r["ok"] is False, str(r))
    check("issues 识破是错误文案",
          any(("模版" in i or "错误" in i or "伪装" in i) for i in r["issues"]),
          str(r["issues"]))
    check("不能把错误文案当 HTML 列表", r["count"] == 0, str(r))


# ---------------------------------------------------------------- 坑 #4 + #5 JSON 装 HTML / count 属性
def test_json_wraps_html_and_count_attr():
    """data.html 是 HTML 片段：建议用 CSS 选择器；总条数从 ul count= 挖"""

    def provider(method, url, reqkw):
        return _json_resp({"success": True, "data": {"html": JPAAS_PAGE}})

    r = _run_probe(provider)          # 不带 referer，只打一次
    check("识别为 JSON", r["is_json"] is True, str(r))
    check("列表路径猜中 data.html", r["list_path_guess"] == "data.html",
          r["list_path_guess"])
    check("总条数从 count 属性挖出（坑#5）", r["count"] == 331, str(r["count"]))
    check("item 带父级上下文，没选宽",
          r["suggested"].get("item") == "ul.pagination li",
          str(r["suggested"]))
    check("title 选择器", r["suggested"].get("title") == "a.bt-left",
          str(r["suggested"]))
    check("date 选择器", r["suggested"].get("date") == "span.bt-right",
          str(r["suggested"]))
    check("建议用 CSS 选择器而非 JSON fields",
          any("HTML 片段" in i or "CSS" in i for i in r["issues"]),
          str(r["issues"]))
    check("可用", r["ok"] is True, str(r["issues"]))


# ---------------------------------------------------------------- 正例：普通对象数组
def test_plain_json_array():
    """非 jpaas 的普通 JSON：直接猜 data.list 这种数组路径"""

    def provider(method, url, reqkw):
        return _json_resp({
            "success": True, "code": 0,
            "data": {"list": [
                {"title": "标题一", "url": "https://x.gov.cn/a1.htm",
                 "date": "2026-10-10"},
                {"title": "标题二", "url": "https://x.gov.cn/a2.htm",
                 "date": "2026-10-09"},
            ], "total": 2}})

    r = _run_probe(provider)
    check("数组路径猜中 data.list", r["list_path_guess"] == "data.list",
          r["list_path_guess"])
    check("count = 数组长度", r["count"] == 2, str(r["count"]))
    check("可用", r["ok"] is True, str(r["issues"]))
    check("suggested 带 api_url/method/params",
          r["suggested"]["api_url"] == API and r["suggested"]["method"] == "GET",
          str(r["suggested"]))


# ---------------------------------------------------------------- 负例 #1 静态 js
def test_negative_static_js():
    """Content-Type: application/javascript 绝不能判成可采集接口"""

    def provider(method, url, reqkw):
        return FakeResp("var app={list:[]};", content_type="application/javascript")

    r = _run_probe(provider)
    check("静态 js 不报可用", r["ok"] is False, str(r))
    check("issues 点明是静态资源",
          any("静态" in i for i in r["issues"]), str(r["issues"]))


# ---------------------------------------------------------------- 负例 #2 导航路径
def test_negative_nav_page():
    """/fwhd/ 这类栏目导航页：有链接但没有文章列表结构，不得判成接口"""

    nav_html = ("<html><body><nav>"
                "<a href='/fwhd/'>服务活动</a>"
                "<a href='/about/'>关于我们</a>"
                "<a href='/fwhd/tzgg/'>通知公告</a>"
                "</nav></body></html>")

    def provider(method, url, reqkw):
        return FakeResp(nav_html, content_type="text/html; charset=utf-8")

    import cacx.diagnose as D
    with mock.patch.object(D.requests.Session, "request") as m:
        m.side_effect = lambda method, url, **kw: provider(method, url, kw)
        r = D.probe_api("https://www.zjwx.gov.cn/fwhd/")
    check("导航页不报可用", r["ok"] is False, str(r))
    check("issues 指向「无列表/导航」",
          any(("列表" in i or "导航" in i) for i in r["issues"]),
          str(r["issues"]))


# ---------------------------------------------------------------- 负例 #3 无列表 HTML
def test_negative_plain_content():
    """返回 HTML 但没有 <ul>/<li>/<a> 列表模式（纯正文页），不得报可采集"""

    page = ("<html><head><title>某篇文章</title></head><body>"
            "<h1>文章标题</h1><p>这是正文段落，没有任何列表结构。"
            "没有 ul，没有 li，也没有文章链接列表。</p></body></html>")

    def provider(method, url, reqkw):
        return FakeResp(page, content_type="text/html; charset=utf-8")

    import cacx.diagnose as D
    with mock.patch.object(D.requests.Session, "request") as m:
        m.side_effect = lambda method, url, **kw: provider(method, url, kw)
        r = D.probe_api("https://www.zjwx.gov.cn/art/2026/10/10/a.htm")
    check("纯正文页不报列表可采集", r["ok"] is False, str(r))
    check("count 为 0", r["count"] == 0, str(r["count"]))


# ---------------------------------------------------------------- 安全：不写库/不写配置
def test_no_side_effect():
    """probe_api 跑完后：真实 config/settings.json 与 data/cacx.db 原封不动"""
    root = Path(__file__).resolve().parent
    settings = root / "config" / "settings.json"
    db = root / "data" / "cacx.db"

    def snap(p):
        if not p.exists():
            return None
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return (h, p.stat().st_mtime_ns)

    before_cfg = snap(settings)
    before_db = snap(db)

    # 打桩跑一次完整诊断（带 referer，会发两次请求）
    def provider(method, url, reqkw):
        if _has_referer(reqkw):
            return _json_resp({"success": True, "data": {"html": JPAAS_PAGE}})
        return _json_resp({"success": False, "data": None})

    r = _run_probe(provider, referer=REFERER)
    check("诊断本身跑通", isinstance(r, dict) and r.get("ok") is not None,
          "probe_api 没返回 dict")

    after_cfg = snap(settings)
    after_db = snap(db)
    check("settings.json 内容+mtime 不变（不回显密钥，只比哈希）",
          before_cfg == after_cfg, f"before={before_cfg} after={after_cfg}")
    check("cacx.db 未被改动", before_db == after_db,
          f"before={before_db} after={after_db}")


# ---------------------------------------------------------------- main
def main():
    print("=" * 62)
    print("API 接口检测工具（probe_api）离线回归测试")
    print("=" * 62)
    for fn in (test_referer_required, test_success_false_at_200,
               test_error_page_masquerade,
               test_json_wraps_html_and_count_attr, test_plain_json_array,
               test_negative_static_js, test_negative_nav_page,
               test_negative_plain_content, test_no_side_effect):
        try:
            fn()
        except Exception:
            FAIL.append(fn.__name__)
            print(f"  !! {fn.__name__} 抛异常")
            traceback.print_exc()
    print("\n" + "=" * 62)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
