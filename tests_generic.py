# -*- coding: utf-8 -*-
"""
通用采集链路回归测试（离线，不联网、不调 AI）
================================================================
运行：  python tests_generic.py

为什么这些断言是这样写的
  · 分类器最容易犯的错是**误判真文章**（方向错了就是静默丢数据），
    所以每条规则都配了「不能这样判」的负例，而不只是「该这样判」的正例。
  · runner 的分流分支必须用打桩采集器验证：真跑一次同步要几分钟，
    而且依赖源站在线，回归测试不能有这种依赖。
  · 任何会碰用户配置/数据库的测试，一律用**临时目录**，
    绝不碰 config/settings.json 与 data/cacx.db。
"""

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

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


# ---------------------------------------------------------------- 1. 分类器
def test_classify():
    from cacx.collector.classify import (KIND_ARTICLE, KIND_DIRTY, KIND_EXTERNAL,
                                         KIND_INDEX, KIND_WECHAT, classify)
    CAC = "https://www.cac.gov.cn"
    ZJ = "https://www.zjwx.gov.cn"

    # --- 正例：确实不是文章 ---
    for u, want, why in [
        ("", KIND_DIRTY, "空链接"),
        ("javascript:void(0);", KIND_DIRTY, "伪链接"),
        ("mailto:a@b.c", KIND_DIRTY, "邮件"),
        ("https://www.cac.gov.cn/2019zt/419szn/index.htm", KIND_INDEX, "目录索引"),
        ("https://www.cac.gov.cn/ncwx/10.htm", KIND_INDEX, "栏目页码"),
        ("https://www.cac.gov.cn/2018zt/wxgzhyaqpz.htm", KIND_INDEX, "两段无 id"),
        ("https://www.cac.gov.cn/2015fzdm.htm", KIND_INDEX, "单层专题"),
        ("https://www.cac.gov.cn/", KIND_INDEX, "站点根"),
        ("http://theory.people.com.cn/GB/1/index.html", KIND_EXTERNAL, "站外"),
        ("https://mp.weixin.qq.com/s/abc", KIND_WECHAT, "公众号"),
    ]:
        got, reason = classify(u, CAC)
        check(f"分类 {why}", got == want, f"期望 {want} 实际 {got}")

    # 附件要按**同一站点**的基址判，否则先被站外规则截走，测不到附件规则
    got, reason = classify("https://www.cac.gov.cn/a/b.pdf", CAC)
    check("分类 附件下载", got == KIND_INDEX, f"实际 {got}")

    # --- 负例：这些**必须**判成文章，否则会静默丢掉真数据 ---
    for u, why in [
        ("https://www.cac.gov.cn/2019-12/25/c_1578810501771691.htm", "c_ 开头"),
        ("https://www.cac.gov.cn/2015-07/14/c_1115916403.htm", "c_ 开头短 id"),
        # 这条实测被误判过：16 位纯数字文件名是文章 id，不是翻页页码
        ("https://www.cac.gov.cn/yaowen/gdtt/webinfo/2019/12/1578463627194187.htm",
         "16 位数字文章 id"),
    ]:
        got, _ = classify(u, CAC)
        check(f"不得误判 {why}", got == KIND_ARTICLE, f"被判成了 {got}")

    got, _ = classify("https://www.zjwx.gov.cn/art/2025/4/10/art_1673564_58876604.html",
                      ZJ)
    check("浙江文章 URL", got == KIND_ARTICLE, f"实际 {got}")

    # --- 站点自定义专题前缀 ---
    got, reason = classify("https://www.cac.gov.cn/2017zt/a/b.htm", CAC,
                           topic_prefixes=["2017zt"])
    check("站点声明的专题目录生效", got == KIND_INDEX, reason)
    got2, _ = classify("https://www.cac.gov.cn/2017zt/a/b.htm", CAC)
    check("未声明时不误伤", got2 == KIND_ARTICLE, f"实际 {got2}")

    # --- reason 必须非空：跳过要可解释 ---
    _, reason = classify("https://www.cac.gov.cn/2019zt/x/index.htm", CAC)
    check("跳过原因非空", bool(reason))


# ---------------------------------------------------------------- 2. 日期归一化
def test_dates():
    from cacx.collector.generic import normalize_date as nd
    cases = [
        ("2026-10-10", "", "2026-10-10"),
        ("2026/10/10", "", "2026-10-10"),
        ("2026年10月10日", "", "2026-10-10"),
        ("2026.10.10", "", "2026-10-10"),
        ("发布时间：2026-10-10 14:30", "", "2026-10-10 14:30"),
        ("10-10", "2026", "2026-10-10"),
        ("10月10日", "2026", "2026-10-10"),
        ("1760000000000", "", "2025-10-09"),      # epoch 毫秒
        ("1760000000", "", "2025-10-09"),         # epoch 秒
        ("", "", ""),
        ("暂无", "", ""),
        ("2026-13-45", "", ""),                   # 非法日期不猜
    ]
    for raw, yr, want in cases:
        got = nd(raw, yr)
        check(f"日期 {raw[:18] or '(空)'}→{want}", got == want, f"实际 {got!r}")


# ---------------------------------------------------------------- 3. JSON 路径
def test_dig():
    from cacx.collector.generic import dig
    data = {"data": {"list": [{"u": 1}, {"u": 2}], "total": 2}}
    check("dig 两级", dig(data, "data.list") == data["data"]["list"])
    check("dig 缺失", dig(data, "data.nope") is None)
    check("dig 越界", dig(data, "data.list[9]", "x") == "x")
    check("dig 下标", dig(data, "data.list[1].u") == 2)
    check("dig 空路径", dig(data, "") is None)


# ---------------------------------------------------------------- 4. 站点注册表
def test_sites_registry():
    from cacx import sites as S
    tmp = tempfile.mkdtemp(prefix="cacx_sites_")
    old = S.CUSTOM_DIR
    try:
        S.CUSTOM_DIR = Path(tmp)
        check("空目录读出空表", S.list_custom() == {})

        spec = {"code": "sdwx", "name": "山东", "base_url": "https://x.gov.cn",
                "kind": "html",
                "columns": [{"code": "main", "name": "要闻",
                             "list_url": "https://x.gov.cn/a/",
                             "item": "ul li", "title": "a", "date": "span"}],
                "article": {"content": ".content"}}
        S.save_custom(spec)
        got = S.list_custom()
        check("写入后可读回", "sdwx" in got, str(got))
        check("名称保留", got["sdwx"]["name"] == "山东")
        check("自动补 updated_at", bool(got["sdwx"].get("updated_at")))
        check("校验通过", S.validate(spec) == [], str(S.validate(spec)))

        # 坏文件不能让整个列表崩
        (Path(tmp) / "broken.json").write_text("{不是合法 json",
                                              encoding="utf-8")
        got = S.list_custom()
        check("坏文件被标记而非抛异常", got["broken"].get("_broken") is True)
        check("好文件仍可读", "sdwx" in got and not got["sdwx"].get("_broken"))

        check("删除", S.delete_custom("sdwx") is True)
        check("删除不存在返回 False", S.delete_custom("sdwx") is False)

        # 非法 code 必须拒绝，不能拼出越界路径
        for bad in ("../evil", "a/b", "中文"):
            try:
                S.save_custom({"code": bad, "name": "x"})
                check(f"拒绝非法 code {bad}", False, "竟然写进去了")
            except ValueError:
                check(f"拒绝非法 code {bad}", True)
            except Exception as e:
                check(f"拒绝非法 code {bad}", False, f"抛了 {type(e).__name__}")

        # 校验应挑出真问题
        bad_spec = {"code": "t", "base_url": "", "kind": "html", "columns": [],
                    "article": {}}
        issues = S.validate(bad_spec)
        check("校验能挑出缺 base_url", any("base_url" in i for i in issues))
        check("校验能挑出缺栏目", any("栏目" in i for i in issues))
        bad2 = {"code": "t", "base_url": "https://a.cn", "kind": "json",
                "columns": [{"code": "c", "name": "n", "api_url": ""}],
                "article": {}}
        issues = S.validate(bad2)
        check("json 模式校验 api_url", any("api_url" in i for i in issues))
    finally:
        S.CUSTOM_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5. runner 分流
def test_runner_diversion():
    """用打桩采集器验证：非文章条目被登记、且不计入 failed"""
    from cacx import db as D
    from cacx.collector import runner as R

    tmp = Path(tempfile.mkdtemp(prefix="cacx_db_"))
    d = D.Database(tmp / "t.db")

    class Stub:
        code, name, base_url = "stub", "桩站", "https://stub.gov.cn"
        date_start, date_end = "2000-01-01", "2099-12-31"

        def columns(self):
            return [{"code": "main", "name": "要闻"}]

        def list_articles(self, cc):
            return [
                {"url": "https://stub.gov.cn/2026/01/01/c_1.htm",
                 "title": "真文章", "date": "2026-01-01", "kind": "article"},
                {"url": "https://stub.gov.cn/zt/index.htm",
                 "title": "专题页", "date": "2026-01-02", "kind": "index_page",
                 "skip_reason": "目录索引页"},
                {"url": "https://other.gov.cn/a.htm", "title": "站外",
                 "date": "2026-01-03", "kind": "external",
                 "skip_reason": "域名不属于本站"},
                {"url": "https://mp.weixin.qq.com/s/x", "title": "公众号",
                 "date": "2026-01-04", "kind": "wechat",
                 "skip_reason": "微信公众号"},
                {"url": "", "title": "脏数据", "date": "2026-01-05",
                 "kind": "dirty", "skip_reason": "链接为空"},
            ]

        def fetch_article(self, url, fb):
            return {"title": "真文章", "pub_date": "2026-01-01",
                    "source_text": "", "content_html": "<p>" + "内容" * 200 + "</p>"}

        def in_window(self, day):
            return True

    logs = []
    orig_get = R.get_collector
    orig_codes = R.site_codes
    R.get_collector = lambda cfg, site, verbose=True: Stub()
    R.site_codes = lambda cfg, only_enabled=True: ["stub"]
    try:
        cfg = {"db": {"path": str(tmp / "t.db")}, "sites": {}, "collector": {}}
        R.sync(cfg, mode="incremental", verbose=True,
               on_progress=lambda ev: logs.append(ev))
    finally:
        R.get_collector, R.site_codes = orig_get, orig_codes

    done = [e for e in logs if e.get("kind") == "site_done"]
    check("产生 site_done 事件", len(done) == 1, str(logs[-3:]))
    if done:
        ev = done[0]
        check("failed 只算真失败", ev["failed"] == 0, f"failed={ev['failed']}")
        check("skipped 计数正确", ev["skipped"] == 4, f"skipped={ev['skipped']}")
        kinds = ev.get("skip_kinds", {})
        check("专题页计入", kinds.get("index_page") == 1, str(kinds))
        check("站外计入", kinds.get("external") == 1, str(kinds))
        check("微信计入", kinds.get("wechat") == 1, str(kinds))
        check("脏数据计入", kinds.get("dirty") == 1, str(kinds))

    with d._lock:
        arts = d.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        rows = [dict(r) for r in d.conn.execute(
            "SELECT fail_kind, is_own_site, list_url FROM external_links")]
    check("只入库真文章", arts == 1, f"articles={arts}")
    got = {r["fail_kind"] for r in rows}
    check("四类都登记了", got == {"index_page", "external", "wechat", "dirty"},
          str(got))
    dirty = [r for r in rows if r["fail_kind"] == "dirty"]
    check("脏数据有合成键（不丢）", dirty and dirty[0]["list_url"], str(dirty))
    idx = [r for r in rows if r["fail_kind"] == "index_page"]
    check("专题页标记为本站", idx and idx[0]["is_own_site"] == 1, str(idx))
    ext = [r for r in rows if r["fail_kind"] == "external"]
    check("站外标记为非本站", ext and ext[0]["is_own_site"] == 0, str(ext))
    d.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5b. 翻页早停
def test_early_stop():
    """
    早停靠的是「见到的最旧日期」，而不是「入库行的最旧日期」。

    这两者差一个窗口过滤：一旦只统计入库行，最小值必然 >= date_start，
    早停条件恒不成立，翻页会一路走到 max_pages。不漏数据，但白翻几十页
    请求，对源站不友好、跑起来也慢。这个曾经真实退化过一次，故钉死。
    """
    from cacx.collector.generic import GenericCollector

    col = {"code": "main", "name": "要闻",
           "item": "ul li", "title": "a", "link": "a", "date": "span"}

    # URL 必须带页码：翻页的语义就是每页给一批**没出现过**的条目，
    # 若各页 URL 相同，第二页会被 seen 去重挡在日期统计之前，
    # 测的就不是早停而是去重了。
    def page(day, n=3, pg=1):
        items = "".join(
            f'<li><a href="/a/{pg}_{i}.htm">标题{pg}-{i}</a>'
            f'<span>{day}</span></li>' for i in range(n))
        return f"<html><body><ul>{items}</ul></body></html>"

    # 第 1 页全在窗口内，第 2 页已越过窗口下界 -> 应在第 2 页停下
    PAGES = {"https://x.gov.cn/list_1.htm": page("2026-06-01", 3, 1),
             "https://x.gov.cn/list_2.htm": page("2026-01-01", 3, 2)}
    for i in range(3, 9):
        PAGES[f"https://x.gov.cn/list_{i}.htm"] = page("2025-01-01", 3, i)

    class FakeResp:
        def __init__(self, t):
            self.text, self.content, self.encoding = t, t.encode(), "utf-8"
            self.status_code = 200
            self.apparent_encoding = "utf-8"
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

    class FakeFetcher:
        def __init__(self):
            self.hits = []

        def get(self, url, **kw):
            self.hits.append(url)
            return FakeResp(PAGES[url]) if url in PAGES else None

    gc = GenericCollector.__new__(GenericCollector)
    gc.base_url = "https://x.gov.cn"
    gc.fetcher = FakeFetcher()
    gc.log = lambda *a, **k: None
    gc.cfg_site = {"code": "x", "name": "桩站", "topic_prefixes": ()}
    gc.date_start, gc.date_end = "2026-01-15", "2099-12-31"
    gc.in_window = lambda d: bool(d) and gc.date_start <= d <= gc.date_end

    col = dict(col, list_url="https://x.gov.cn/list_1.htm",
               pagination={"mode": "url", "url": "https://x.gov.cn/list_{page}.htm",
                           "start_page": 1, "max_pages": 8})
    rows = gc._list_html(col)

    check("窗口内条目全部入库", len(rows) == 3, f"rows={len(rows)}")
    check("越界后即停，不翻满 8 页",
          len(gc.fetcher.hits) == 2, f"hits={len(gc.fetcher.hits)}")
    check("窗口外的不入库",
          all(r["date"] >= "2026-01-15" for r in rows),
          str([r["date"] for r in rows]))

    # 反向：窗口内行与窗口外行同页时，最旧日期应取到窗口外那个
    PAGES["https://x.gov.cn/list_1.htm"] = (
        '<html><body><ul>'
        '<li><a href="/a/1.htm">新</a><span>2026-06-01</span></li>'
        '<li><a href="/a/2.htm">旧</a><span>2020-01-01</span></li>'
        '</ul></body></html>')
    PAGES["https://x.gov.cn/list_2.htm"] = page("2026-01-01", 3, 2)
    gc.fetcher.hits.clear()
    rows = gc._list_html(col)
    check("同页混排：只收窗口内 1 条", len(rows) == 1, f"rows={len(rows)}")
    check("同页混排：见到旧条目后当场停，不多翻一页",
          len(gc.fetcher.hits) == 1, f"hits={len(gc.fetcher.hits)}")


# ---------------------------------------------------------------- 6. 解码
def test_decode():
    """requests 对无 charset 的响应会按 ISO-8859-1 解码，中文会静默变乱码"""
    from cacx.collector.base import decode_response

    class FakeResp:
        def __init__(self, content, encoding=None):
            self.content, self.encoding = content, encoding
            self.apparent_encoding = "utf-8"
        @property
        def text(self):
            return self.content.decode(self.encoding or "utf-8", "replace")

    zh = "紫金e评 网信办".encode("utf-8")
    check("utf-8 无 charset 时按内容判", decode_response(FakeResp(zh, "ISO-8859-1")) == "紫金e评 网信办")
    check("utf-8 声明正确", decode_response(FakeResp(zh, "utf-8")) == "紫金e评 网信办")
    check("ISO-8859-1 声明不被信任",
          decode_response(FakeResp(zh, "ISO-8859-1")) == "紫金e评 网信办",
          "requests 的 ISO-8859-1 是默认猜测，不能当真实声明用")
    gb = "紫金e评 网信办".encode("gb18030")
    check("gb18030 兜底", decode_response(FakeResp(gb, None)) == "紫金e评 网信办")


# ---------------------------------------------------------------- 7. 列表规则推断
LIST_HTML = """
<html><body>
<nav><ul>
  <li><a href="/index.html">首页</a></li>
  <li><a href="/about.html">关于我们</a></li>
</ul></nav>
<div class="main"><ul id="loadingInfoPage">
  <li><h5><a href="//w.gov.cn/2026-09/30/c_1792520315672675.htm">第一条标题</a></h5>
      <div class="times">2026-09-30</div></li>
  <li><h5><a href="//w.gov.cn/2026-09/29/c_1792520315672676.htm">第二条标题</a></h5>
      <div class="times">2026-09-29</div></li>
  <li><h5><a href="//w.gov.cn/2026-09/28/c_1792520315672677.htm">第三条标题</a></h5>
      <div class="times">2026-09-28</div></li>
  <li><h5><a href="//w.gov.cn/2026-09/27/c_1792520315672678.htm">第四条标题</a></h5>
      <div class="times">2026-09-27</div></li>
  <li><h5><a href="//w.gov.cn/2026-09/26/c_1792520315672679.htm">第五条标题</a></h5>
      <div class="times">2026-09-26</div></li>
</ul></div></body></html>
"""


def test_list_inference():
    """这条结构取自 CAC 时政要闻列表页的真实 HTML（简化）"""
    from cacx.collector import rules as R
    r = R.infer_list_rule(LIST_HTML, "https://www.w.gov.cn")
    check("推断出列表条目选择器", r.get("ok") is True, str(r.get("issues")))
    if not r.get("ok"):
        return
    check("item 带父级上下文",
          "li" in r["item"] and ">" in r["item"], f"item={r['item']}")
    check("item 不是裸 li（裸 li 会把导航栏选进来）",
          r["item"].strip() != "li", f"item={r['item']}")
    check("item 不带 :nth-of-type（会把每条拆成唯一选择器）",
          "nth-of-type" not in r["item"], f"item={r['item']}")
    check("标题选择器", r["title"] == "a", f"title={r['title']}")
    check("链接选择器", r["link"] == "a", f"link={r['link']}")
    check("日期选择器", r["date"] == "div.times", f"date={r['date']}")
    s = r.get("sample") or {}
    check("样本能解析出日期", s.get("date") == "2026-09-30", str(s))
    check("样本能解析出标题", s.get("title", "").startswith("第一条"), str(s))

    # 条目内部选择器必须是**相对**的：通用采集器在条目里 select_one
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(LIST_HTML, "html.parser")
    item = soup.select_one(r["item"])
    check("item 选择器可定位到元素", item is not None)
    if item is not None:
        check("标题选择器在条目内可定位",
              item.select_one(r["title"]) is not None, r["title"])
        check("日期选择器在条目内可定位",
              item.select_one(r["date"]) is not None, r["date"])


def test_api_candidates():
    """接口探测不能把普通导航路径当接口推荐出去"""
    from cacx.collector.rules import find_api_candidates
    html = """
    <script>var a="/fwhd/tzgg/",b="/about/index.html",
      c="https://x.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit",
      d="/static/js/app.js";</script>"""
    got = [c["url"] for c in find_api_candidates(html, "https://x.gov.cn")]
    check("抓到 jpaas 接口", any("jpaas" in u for u in got), str(got))
    check("普通导航路径不被当成接口",
          not any("/fwhd/" in u for u in got), str(got))
    check("静态 js 不算接口", not any(u.endswith(".js") for u in got), str(got))
    check("无接口关键词时返回空",
          find_api_candidates('<a href="/a/b/c.html">x</a>', "https://x.gov.cn") == [])


# ---------------------------------------------------------------- 8. 通用采集器配置校验
def test_generic_build():
    from cacx.collector.generic import build as build_col
    spec = {"code": "x", "name": "X", "base_url": "https://a.cn", "kind": "html",
            "columns": [{"code": "m", "name": "M", "list_url": "https://a.cn/l/",
                         "item": "ul li", "title": "a", "date": "span"}],
            "article": {"content": ".c"}}
    col = build_col(spec, {})
    check("html 采集器可构造", col.kind == "html")
    check("栏目读得出", col.columns() == [{"code": "m", "name": "M"}])
    try:
        build_col({**spec, "kind": "xml"}, {})
        check("非法 kind 被拒", False, "竟然构造成功了")
    except ValueError:
        check("非法 kind 被拒", True)


# ---------------------------------------------------------------- main
def main():
    print("=" * 62)
    print("通用采集链路回归测试")
    print("=" * 62)
    for fn in (test_classify, test_dates, test_dig, test_sites_registry,
               test_runner_diversion, test_early_stop, test_decode,
               test_list_inference, test_api_candidates, test_generic_build):
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