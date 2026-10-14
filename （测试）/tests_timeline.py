# -*- coding: utf-8 -*-
"""
时间轴（发文量 + 主题随时间）—— PPT 明确要求的第三项
================================================================
这个功能最容易出的不是"算错"，而是"**看着炸**"：
内容都在、控制台无报错、DOM 里有值，但一根柱子高过容器十几倍，
整页被撑爆。所以本文件除了数据口径，还钉**尺寸不变式**。

跑法：py -3 tests_timeline.py
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import db as DB          # noqa: E402

# ⚠ 必须用 importlib 取**模块**：`from cacx.web import app` 拿到的是
#   FastAPI **实例**（cacx/web/__init__.py 导出 app 对象），
#   于是 A._db / A._svg_lines 全都不存在，报
#   "'FastAPI' object has no attribute '_db'" —— 看着像代码缺函数，
#   其实是导入方式错了。
import importlib                     # noqa: E402
A = importlib.import_module("cacx.web.app")

MONTHS = [f"2024-{m:02d}" for m in range(1, 7)]


def _make_db(path):
    d = DB.Database(path)
    rows = [
        # (site, day, title, body, 有无正文)
        ("cac", "2024-01-10", "关于加强数据安全保护的通知", "数据安全 数据安全", 1),
        ("cac", "2024-01-20", "未成年人网络保护专项行动", "未成年人 未成年人", 1),
        ("cac", "2024-03-05", "打击网络诈骗", "网络诈骗", 1),
        ("zjwx", "2024-02-11", "治理网络谣言", "网络谣言", 1),
        ("zjwx", "2024-05-09", "生成式AI治理", "生成式人工智能", 1),
        # 6 月故意空 —— 补零必须把它补出来
        ("cac", "2024-06-15", "", "", 0),
    ]
    ids = []
    for site, day, title, body, ok in rows:
        r = d.conn.execute(
            "INSERT INTO articles (site, site_name, title, pub_date, pub_day, "
            "year, month, url, source_text, chars, content_text, content_empty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (site, site.upper(), title, day, day, int(day[:4]), int(day[5:7]),
             f"http://x/{day}", "s", 10 if ok else 0, body if ok else "",
             0 if ok else 1))
        ids.append(r.lastrowid)
    d.conn.commit()
    return d, ids


class TimelineCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cacx_tl_"))
        cls.d, cls.ids = _make_db(cls.tmp / "t.db")
        # 打两个 AI 主题标注
        cls.d.save_annotation(cls.ids[0], "theme", '["个人信息"]')
        cls.d.save_annotation(cls.ids[1], "theme", '["未成年人"]')
        cls.d.save_annotation(cls.ids[2], "theme", '["网络诈骗"]')

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()
        except Exception:
            pass
        cls.d = None


class TestMonthlyTrend(TimelineCase):
    def test_按月分箱正确(self):
        r = self.d.article_monthly_trend(date_from="2024-01", date_to="2024-06")
        m = {x["month"]: x for x in r}
        self.assertEqual(len(r), 6, "6 个月就该出 6 行")
        self.assertEqual(m["2024-01"]["total"], 2)
        self.assertEqual(m["2024-02"]["total"], 1)
        self.assertEqual(m["2024-06"]["total"], 1, "6 月有文章（正文空也算发文量）")

    def test_空月份必须补零(self):
        """坑：跳过空月 → 前端折线间距被压扁，
        读起来像「发文突然变频繁」。"""
        r = self.d.article_monthly_trend(date_from="2024-01", date_to="2024-06")
        m = {x["month"]: x["total"] for x in r}
        # 造的数据里 6 月有 1 篇；把区间拉到 8 月，7/8 月必须是 0
        r2 = self.d.article_monthly_trend(date_from="2024-01", date_to="2024-08")
        m2 = {x["month"]: x["total"] for x in r2}
        self.assertEqual(m2["2024-07"], 0)
        self.assertEqual(m2["2024-08"], 0)

    def test_补零不能补成无穷长(self):
        """⚠ 真实踩过：date_to 默认 2099-12-31，补零补出 912 个月，
        其中 878 个是 0 —— 折线右边拖一条八百个月的空白。"""
        r = self.d.article_monthly_trend(date_from="2024-01")
        self.assertLessEqual(len(r), 1200)
        # 补零的上界 = 数据实际覆盖到的最后一个月
        last = max(x["month"] for x in r if x["total"] > 0)
        self.assertEqual(r[-1]["month"], last,
                         f"末尾多补了空月（{r[-1]['month']} vs {last}）")

    def test_分站点之和等于总数(self):
        r = self.d.article_monthly_trend(date_from="2024-01", date_to="2024-06")
        for x in r:
            parts = sum(x.get(s, 0) for s in ("cac", "zjwx"))
            self.assertEqual(parts, x["total"], f"{x['month']} 拆分对不上")

    def test_站点筛选生效(self):
        a = self.d.article_monthly_trend(date_from="2024-01", date_to="2024-06")
        c = self.d.article_monthly_trend(site="cac", date_from="2024-01",
                                          date_to="2024-06")
        self.assertLess(sum(x["total"] for x in c),
                        sum(x["total"] for x in a))


class TestProblemMonthly(TimelineCase):
    def test_主题按月分箱(self):
        r = self.d.problem_monthly(date_from="2024-01", date_to="2024-06")
        m = {x["month"]: x["problems"] for x in r}
        self.assertEqual(m["2024-01"]["数据安全"], 1)
        self.assertEqual(m["2024-01"]["未成年人"], 1)
        self.assertEqual(m["2024-02"]["网络谣言"], 1)

    def test_空正文不参与主题统计(self):
        """发文量含正文空的文章，主题不含 —— 两个分母不同，必须真的不同。"""
        vol = self.d.article_monthly_trend(date_from="2024-06", date_to="2024-06")
        th = self.d.problem_monthly(date_from="2024-06", date_to="2024-06")
        self.assertEqual(vol[0]["total"], 1)
        self.assertEqual(th[0]["total"], 0, "正文空的文章不该被主题统计")

    def test_只保留topN类别并列出落选的(self):
        r = self.d.problem_monthly(date_from="2024-01", date_to="2024-06", top=2)
        kept = r[0].get("_kept") or []
        self.assertLessEqual(len(kept), 2)
        for row in r:
            self.assertLessEqual(len(row["problems"]), 2)

    def test_粗筛可重复命中所以总数会超(self):
        """这个「超」不是 bug，界面上写明了 —— 但必须真的会发生，
        否则说明关键词表和实际语料对不上。"""
        r = self.d.problem_monthly(date_from="2024-01", date_to="2024-06")
        hits = sum(sum(x["problems"].values()) for x in r)
        self.assertGreater(hits, 0)


class TestAiThemeMonthly(TimelineCase):
    def test_只统计已标注的文章(self):
        r = self.d.ai_theme_monthly(date_from="2024-01", date_to="2024-06")
        m = {x["month"]: x for x in r}
        self.assertEqual(m["2024-01"]["labeled"], 2)   # ids[0] ids[1]
        self.assertEqual(m["2024-03"]["labeled"], 1)   # ids[2]
        self.assertNotIn("2024-02", m, "没标过的月份不该出现")

    def test_按传入月份对齐(self):
        """横轴必须与发文量图一致，否则并排看像数据打架。"""
        r = self.d.ai_theme_monthly(months=MONTHS,
                                    date_from="2024-01", date_to="2024-06")
        self.assertEqual([x["month"] for x in r], MONTHS)
        m = {x["month"]: x for x in r}
        self.assertEqual(m["2024-02"]["labeled"], 0, "没标过也要补 0 行")

    def test_覆盖率如实报告(self):
        c = self.d.theme_coverage(date_from="2024-01", date_to="2024-06")
        self.assertEqual(c["labeled"], 3)
        self.assertEqual(c["total"], 5, "6 篇里 1 篇正文空，不该计入")
        self.assertEqual(c["pct"], 60.0)

    def test_候选词表受控(self):
        """自由主题名会毁掉按月聚合，也会开提示注入的门。"""
        from cacx.ai import THEME_LABELS
        self.assertIn("个人信息", THEME_LABELS)
        self.assertIn("other", THEME_LABELS, "需要兜底类，否则模型无处安放")


class TestTimelineApi(TimelineCase):
    """接口层：三图横轴对齐 + 口径回显 + AI 默认只预览"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from unittest import mock
        cls._mock = mock.patch.object(A, "_db", lambda: cls.d)
        cls._mock.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._mock.stop()
        except Exception:
            pass
        super().tearDownClass()

    def test_三图横轴完全对齐(self):
        # ⚠ 调 _timeline_impl 而不是路由：路由参数是 Query 对象，
        #   直接调用会 int() 炸（这正是把实现抽出来的原因）。
        d = A._timeline_impl()
        ms = [x["month"] for x in d["months"]]
        self.assertEqual(ms, [x["month"] for x in d["themes"]])
        self.assertEqual(ms, [x["month"] for x in d["ai"]["rows"]])

    def test_回显两个分母(self):
        d = A._timeline_impl()
        s = d["scope"]
        self.assertEqual(s["articles_total"], 6)
        self.assertEqual(s["articles_with_body"], 5)
        self.assertNotEqual(s["articles_total"], s["articles_with_body"],
                            "两个分母应该不同，否则说明白正文过滤没生效")

    def test_站点与日期筛选(self):
        d = A._timeline_impl(site="cac", date_from="2024-01",
                             date_to="2024-03")
        self.assertEqual(d["scope"]["site"], "cac")
        self.assertEqual(d["scope"]["articles_total"], 3)
        self.assertEqual(d["scope"]["months"], 3)

    def test_末月标为未过完(self):
        """末月「未过完」的标注必须跟着末月是不是当月走。

        ⚠ 这里原本断言的是「last_month_partial 恒为 True」，那是**写死行为**
          不是性质：日期筛选（R4）之前末月恒等于当月，写死恰好总对；
          加上 date_from/date_to 后能选历史区间，写死就变成反向误导。
        本测试库的末月是 2024-06 —— 在今天（2026-10）早已过完，
        所以正确行为是**不标**。正反两个场景分别由本项与
        TestLastMonthPartial.test_末月等于当月仍标未过完 覆盖。
        """
        import datetime
        d = A._timeline_impl()
        self.assertEqual(d["last_month"], "2024-06")
        now = datetime.date.today().strftime("%Y-%m")
        self.assertNotEqual(d["last_month"], now,
                            "测试库末月已不再是当月，这条断言的前提要重写")
        self.assertFalse(d["scope"]["last_month_partial"])
        self.assertNotIn("尚未过完", d["note"])
        self.assertNotIn("末月未过完", d["charts"]["volume"])
        # 而「末月确实是当月」时标注必须回来 —— 修到一边倒也不对
        self.assertIn("已过完", d["note"])

    def test_top参数被夹在合理范围(self):
        """Query(6) 是参数对象；越界值也必须被夹住而不是炸掉。"""
        self.assertTrue(A._timeline_impl(top=0)["themes"])
        self.assertTrue(A._timeline_impl(top=99)["themes"])

    def test_ai标主题默认只预览(self):
        """铁律：误触按钮不能烧配额。"""
        r = A.api_panel_ai_themes(A.ThemeReq(limit=10, dry_run=True))
        self.assertTrue(r["dry_run"])
        self.assertIn("plan", r)
        self.assertIn("labels", r)

    def test_preview里的plan不是空话(self):
        r = A.api_panel_ai_themes(A.ThemeReq(limit=10, per_call=12,
                                              dry_run=True))
        self.assertIn("次调用", r["plan"])


class TestSvgGeom(unittest.TestCase):
    """⚠ 尺寸不变式。这是本轮真踩的坑：内容都在、控制台无报错，
    但一根柱子 2208px 高、容器只有 142px，整页被撑爆。
    所以生成器必须**自我钳制**，不能指望"公式一定对"。"""

    def test_超大数据仍画得出来且被钳制(self):
        """喂 10 万级的高度：钳制失效时坐标会跑出画布**上下两侧**。

        ⚠ 只查上界是不够的 —— 去掉钳制后 y 变成**负值**，
          max() 反而更小，"max<=h" 照样通过，测试是假的。
          所以这里必须同时查下界。
        """
        rows = [{"month": f"2024-{m:02d}",
                 "problems": {"a": 100000, "b": 88888}} for m in range(1, 5)]
        svg = A._svg_stack(rows, ["a", "b"], {"a": "#111", "b": "#222"},
                           w=900, h=320)
        self.assertTrue(svg.startswith("<svg"), "没生成 SVG")
        self.assertIn("</svg>", svg)
        ys = _path_ys(svg)
        self.assertTrue(ys, "没解析到 path 坐标")
        self.assertGreaterEqual(min(ys), -1.0,
                                f"坐标跑到画布上方（钳制失效）：{min(ys)}")
        self.assertLessEqual(max(ys), 320.0,
                             f"坐标跑到画布下方（钳制失效）：{max(ys)}")

    def test_钳制在上限算错时仍然兜得住(self):
        """**这条才是钳制真正的用处**，也是它唯一可被观测的场景。

        常规数据下 mx ≥ 每个值，钳制数学上是冗余的 —— 所以
        「删掉 min() 测试会不会红」这个问题，正常数据永远答不出。
        它防的是**上游把 mx 算错**：这里直接把 _nice_max 打桩成
        返回 1（远小于真实值），模拟「Y 轴上限被低估」，
        此时不钳制的话整块面积会糊到画布上方。
        """
        rows = [{"month": f"2024-{m:02d}",
                 "problems": {"a": 5000, "b": 4000}} for m in range(1, 5)]
        real = A._nice_max
        A._nice_max = lambda v: 1              # ← 故意低估上限
        try:
            svg = A._svg_stack(rows, ["a", "b"], {"a": "#111", "b": "#222"},
                               w=900, h=320)
        finally:
            A._nice_max = real
        ys = _path_ys(svg)
        self.assertTrue(ys)
        self.assertGreaterEqual(min(ys), -1.0,
                                f"mx 被低估后越出画布（钳制没兜住）：{min(ys)}")
        self.assertLessEqual(max(ys), 320.0)

    def test_末月未过完有标记(self):
        rows = [{"month": f"2024-{m:02d}", "problems": {"a": 5}}
                for m in range(1, 5)]
        self.assertIn("末月未过完",
                      A._svg_stack(rows, ["a"], {"a": "#111"}, partial_last=True))
        self.assertNotIn("末月未过完",
                         A._svg_stack(rows, ["a"], {"a": "#111"}))

    def test_坐标轴带单位(self):
        rows = [{"month": "2024-01", "problems": {"a": 3}}]
        self.assertIn("篇", A._svg_stack(rows, ["a"], {"a": "#111"}, unit=" 篇"))

    def test_空数据不出错(self):
        self.assertIn("empty", A._svg_stack([], ["x"], {}))
        self.assertIn("empty", A._svg_stack([{"month": "2024-01",
                                               "problems": {"x": 1}}], [], {}))

    def test_viewBox写明尺寸(self):
        svg = A._svg_stack([{"month": "2024-01", "problems": {"x": 1}}],
                           ["x"], {})
        self.assertIn('viewBox="0 0 900 320"', svg)

    def test_末尾刻度不与前一个叠在一起(self):
        """34 个月、每 4 格一标时，最后两个标签会糊成「2026056-10」。"""
        n, every = 34, 34 // 8
        idxs = A._tick_idx(n, every)
        self.assertEqual(idxs[-1], n - 1, "末尾那个月必须标出来")
        gaps = [b - a for a, b in zip(idxs, idxs[1:])]
        self.assertTrue(all(g >= every / 2 for g in gaps),
                        f"有刻度间距过小，会叠字：{list(zip(idxs, gaps))}")

    def test_刻度下标不越界(self):
        for n in (1, 2, 5, 13, 34, 120):
            idxs = A._tick_idx(n, max(1, n // 8))
            self.assertTrue(all(0 <= i < n for i in idxs), f"n={n}: {idxs}")
        self.assertEqual(A._tick_idx(0, 4), [])


class TestScreenEqualsExport(TimelineCase):
    """「屏幕 = 导出」必须是**构造出来的**，不是两处人工同步。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠ 必须指向临时库：_timeline_impl / snapshot 都走 _db()，
        #   而 _db() 读的是 config/settings.json 里的真库路径。
        #   测试绝不能碰 data/cacx.db。
        cls._orig_db = A._db
        A._db = lambda: cls.d

    @classmethod
    def tearDownClass(cls):
        A._db = cls._orig_db
        super().tearDownClass()

    def test_快照里的时间轴图与接口逐字符相同(self):
        d = A._timeline_impl(top=A._TL_TOP, include_ai=True, months_n=6)
        snap = _snapshot_html(months=6)
        svgs = re.findall(r"<svg\b.*?</svg>", snap, re.S)
        self.assertTrue(svgs, "快照里一张 SVG 都没有")
        for key in ("volume", "theme_kw"):
            want = re.search(r"<svg\b.*?</svg>", d["charts"][key], re.S)
            self.assertIsNotNone(want, f"{key} 没生成 SVG")
            self.assertIn(want.group(0), svgs,
                          f"{key} 在快照里找不到逐字符相同的一段 —— "
                          f"网页和导出口径分叉了")

    def test_top口径三处同源(self):
        """网页、CSV 导出、快照导出显示的主题类数必须一致。"""
        q = A.api_panel_timeline.__defaults__[1]
        self.assertEqual(getattr(q, "default", q), A._TL_TOP,
                         "路由的 top 默认值已经和 _TL_TOP 分叉")
        self.assertIn("top=_TL_TOP", _src_of_snapshot_call())

    def test_图例也进快照(self):
        d = A._timeline_impl(top=A._TL_TOP, include_ai=True, months_n=6)
        snap = _snapshot_html(months=6)
        for k in d["kept"]:
            self.assertIn(f'background:{d["palette"][k]}"', snap)

    def test_只截尾部N个月且在服务端切(self):
        d6 = A._timeline_impl(top=A._TL_TOP, include_ai=True, months_n=3)
        self.assertEqual([r["month"] for r in d6["months"]],
                         ["2024-04", "2024-05", "2024-06"])
        self.assertEqual(d6["scope"]["months"], 3)
        self.assertEqual(len(d6["themes"]), 3, "主题图必须同步截断，横轴才对得上")
        self.assertEqual(len(d6["ai"]["rows"]), 3)
        # 截断后 _sites 不能丢（它原本只挂在第一行上）
        self.assertEqual(d6["sites"], ["cac", "zjwx"])

    def test_取色按名字固定不随排名变(self):
        """换个筛选后，「未成年人」必须还是那个颜色。

        ⚠ 必须用**不同的排名顺序**去比。早先的版本拿两次相同顺序的
          输入去比 —— 按名字取色和按排名取色都会通过，是假测试。
        """
        self.assertEqual(A._tl_palette(["a", "b", "c"]),
                         A._tl_palette(["c", "a", "b"]),
                         "同一批主题换个顺序就换了色，读者会当成两个东西")
        self.assertEqual(A._tl_color("未成年人"), A._tl_color("未成年人"))
        d = A._timeline_impl(top=A._TL_TOP, include_ai=True, months_n=6)
        ai_keys = (d.get("ai") or {}).get("kept") or []
        self.assertEqual(set(d["palette"]),
                         set(d["sites"]) | set(d["kept"]) | set(ai_keys))


def _snapshot_html(months: int = 6, site: str = "") -> str:
    r = A.api_panel_snapshot(site=site, months=months, days=30, top=12)
    # 路由返回的是 Response（内部调用的 FastAPI 装饰器会包一层），
    # 跟走 HTTP 拿到的 body 文本不是一回事，这里取 .body。
    body = getattr(r, "body", None)
    return body.decode("utf-8") if body is not None else str(r)


def _src_of_snapshot_call() -> str:
    import inspect
    src = inspect.getsource(A.api_panel_snapshot)
    return src


class TestLastMonthPartial(unittest.TestCase):
    """末月「未过完」标注必须**随区间开关**，不能写死。

    ⚠ 为什么曾是写死的、且写死还总是对的：
      日期筛选（R4）之前末月恒等于当月，所以 `partial_last=True`
      恰好永远正确，bug 露不出来。
      加了 date_from/date_to 之后能选到**历史区间**，末月是个完整的月 ——
      这时还标「末月未过完」是**反向误导**：读者会以为末月偏低是
      没采集完，其实它是完整的月，恰好把真实跌幅掩盖掉。
    """

    MONTHS = [f"2024-{m:02d}" for m in range(1, 7)]

    def _r(self, date_from="", date_to="", months_n=0):
        d = A._timeline_impl(site="", top=6, date_from=date_from,
                             date_to=date_to, include_ai=False,
                             months_n=months_n)
        sc = d["scope"]
        vol = d["charts"]["volume"]
        kw = d["charts"]["theme_kw"]
        return d, sc, vol, kw

    def test_历史区间不标未过完(self):
        """2024 全年：末月 2024-12 早已过完，图上不该有那条虚线。"""
        d, sc, vol, kw = self._r("2024-01-01", "2024-12-31")
        self.assertEqual(d["last_month"], "2024-12")
        self.assertFalse(sc["last_month_partial"])
        self.assertNotIn("末月未过完", vol)
        self.assertNotIn("末月未过完", kw)
        self.assertIn("已过完", d["note"])

    def test_末月等于当月仍标未过完(self):
        """末月就是当前自然月时，虚线与文案必须照旧存在（别修过头）。"""
        import datetime
        now = datetime.date.today()
        y, m = now.year, now.month
        d, sc, vol, kw = self._r(f"{y:04d}-{m:02d}-01",
                                 f"{y:04d}-{m:02d}-28")
        self.assertEqual(d["last_month"], f"{y:04d}-{m:02d}")
        self.assertTrue(sc["last_month_partial"])
        self.assertIn("末月未过完", vol)
        self.assertIn("尚未过完", d["note"])

    def test_三处必须一致(self):
        """scope 标记 / 发文量图 / 主题图 —— 三处不能各说各话。"""
        for f, t in [("2024-01-01", "2024-12-31"), ("", "")]:
            d, sc, vol, kw = self._r(f, t)
            want = sc["last_month_partial"]
            self.assertEqual("末月未过完" in vol, want,
                             f"发文量图与 scope 不一致：{f}~{t}")
            self.assertEqual("末月未过完" in kw, want,
                             f"主题图与 scope 不一致：{f}~{t}")
            self.assertEqual("尚未过完" in d["note"], want)

    def test_默认区间末月不早于当月(self):
        """默认区间（不截断）下末月应是当月，虚线必须画 —— 回归护栏。"""
        import datetime
        now = datetime.date.today()
        _, sc, vol, _ = self._r()
        self.assertTrue(sc["last_month_partial"])
        self.assertIn("末月未过完", vol)
        self.assertGreaterEqual(sc["range"][-7:], f"{now.year:04d}")


def _path_ys(svg):
    """取出所有 path/line/circle 的 y 坐标（取第一个数值对里的第二个）。"""
    import re
    ys = []
    for m in re.finditer(r'\sd="([^"]+)"', svg):
        for p in re.findall(r'(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)', m.group(1)):
            ys.append(float(p[1]))
    for m in re.finditer(r'y[12]="(-?\d+(?:\.\d+)?)"', svg):
        ys.append(float(m.group(1)))
    for m in re.finditer(r'cy="(-?\d+(?:\.\d+)?)"', svg):
        ys.append(float(m.group(1)))
    return ys


if __name__ == "__main__":
    unittest.main(verbosity=2)