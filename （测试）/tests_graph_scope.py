# -*- coding: utf-8 -*-
"""
图谱按**来源站点 + 日期范围**筛选（P10）
================================================================
背景：图谱能按关系类型筛，却不能按「这件事发生在哪个站、哪一年」筛。
      而关系表自己没存站点和日期 —— 得 JOIN articles 才拿得到，
      所以这类筛选最容易写错，且错了**不报错**，只是结果悄悄不对。

本文件钉住四个坑（都是真实踩过的）：
  ① 站点/日期必须**建图之前**筛 —— 事后挑边的话，边还在，
     但权重已经是全时期的共现强度，「只看 2024 年」时那个权重
     并不是 2024 年的强度
  ② 日期边界必须**归一化** —— 直接拿 "2024" 去比 pub_day("2024-05-06")
     会走字符串比较，BETWEEN '2024' AND '2024' 恒为空：
     用户选了年份却筛出 0 条，而且不报错，看着像"这一年真没数据"
  ③ 「整年」≠「整月」—— 把 01-01~12-31 当成"整月"去查
     month=1 OR month=12，会**静默丢掉中间十个月**
  ④ 排名表与画出来的图必须是**同一口径** —— analyze 跑全图、
     画图裁子图，界面上就会同时出现两套介数

跑法：py -3 tests_graph_scope.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import db as DB          # noqa: E402
from cacx import graph_analysis as GA  # noqa: E402


def _make_db(path):
    """造一个带站点/日期跨度的真关系库。

    ⚠ 日期跨度是刻意的：坑 ③「整年被当成整月」只有当年份里有
      **中间月份**的数据时才暴露。只放 1 月和 12 月的话，
      那个 bug 完全测不出来。
    """
    d = DB.Database(path)      # 构造即建表
    arts = []
    for i, (site, day) in enumerate([
        ("cac", "2024-01-15"), ("cac", "2024-07-20"),   # 年中间 ← 关键
        ("cac", "2024-12-31"), ("zjwx", "2024-06-05"),
        ("zjwx", "2025-03-10"), ("zjwx", "2026-09-09"),
    ]):
        r = d.conn.execute(
            "INSERT INTO articles (site, site_name, title, pub_date, pub_day, "
            "year, month, url, source_text, chars) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (site, site.upper(), f"文章{i}", day, day,
             int(day[:4]), int(day[5:7]), f"http://x/{i}", "正文", 100))
        arts.append(r.lastrowid)
    pairs = [("中央网信办", "国家网信办"), ("国家网信办", "公安部"),
             ("公安部", "工业和信息化部"), ("中央网信办", "工业和信息化部")]
    k = 0
    for aid in arts:
        for src, dst in pairs:
            d.conn.execute(
                "INSERT INTO relations (src_name, dst_name, rel_type, "
                "article_id, evidence, weight) VALUES (?,?,?,?,?,?)",
                (src, dst, ["issue", "co_issue"][k % 2], aid,
                 "测试证据", 1.0))
            k += 1
    d.conn.commit()
    return d


class TempDbCase(unittest.TestCase):
    """⚠ 用 mkdtemp 而不是 TemporaryDirectory：sqlite 连接句柄在
    Windows 上即使 close() 了也可能短暂占用文件，TemporaryDirectory
    的清理会 PermissionError 冒出来，把无关的测试判成 ERROR。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cacx_scope_"))
        cls.d = _make_db(cls.tmp / "t.db")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()      # 显式关句柄，别只靠对象回收
        except Exception:
            pass
        cls.d = None

    def rel(self, **kw):
        return self.d.list_relations(limit=5000, **kw)


class TestDateBound(TempDbCase):
    """坑 ②：日期边界必须归一化"""

    def test_只给年份时能覆盖整年(self):
        n = len(self.rel(date_from="2024", date_to="2024"))
        self.assertEqual(n, 16, f"筛 2024 年应为 4 篇×4 条=16，实得 {n}")

    def test_整年必须包含中间月份(self):
        """坑 ③：2024-07-20 这条只存在于「中间月份」。

        若把 01-01~12-31 误当整月去查 month=1 OR month=12，
        这条会被静默丢掉，总数正好差 4 —— 断言就是照这个差值写的。"""
        rows = self.rel(date_from="2024", date_to="2024")
        self.assertIn("2024-07-20", {r["pub_day"] for r in rows},
                      "2024 年 7 月的那篇不见了 → 整年被当成首尾两个月筛了")

    def test_显式全区间与只给年份等价(self):
        a = len(self.rel(date_from="2024", date_to="2024"))
        b = len(self.rel(date_from="2024-01-01", date_to="2024-12-31"))
        self.assertEqual(a, b, f"{a} vs {b}")

    def test_单月只取该月(self):
        rows = self.rel(date_from="2024-01", date_to="2024-01")
        self.assertEqual({r["pub_day"] for r in rows}, {"2024-01-15"})

    def test_跨年区间两端都能取到(self):
        """2024-06 ~ 2025-03：起点落在 zjwx 的 6 月，终点落在 zjwx 的 3 月，
        中间的 cac 7 月/12 月也要在（区间是连续的，不是只取两端）。"""
        rows = self.rel(date_from="2024-06", date_to="2025-03")
        self.assertEqual({r["pub_day"] for r in rows},
                         {"2024-06-05", "2024-07-20", "2024-12-31",
                          "2025-03-10"})

    def test_反向区间不报错也不误取(self):
        """"从 2025 到 2024"是用户可能犯的错。
        返回空即可，但绝不能把整库都吐出来（那看起来像"筛选没生效"）。"""
        rows = self.rel(date_from="2025", date_to="2024")
        self.assertEqual(rows, [], f"返回了 {len(rows)} 条")

    def test_闰年二月按实际天数算(self):
        for s, want in [("2024-02", "2024-02-29"), ("2023-02", "2023-02-28"),
                        ("2024", "2024-12-31"), ("2024-05", "2024-05-31")]:
            self.assertEqual(DB._month_bound(s, "hi"), want, s)

    def test_边界归一化的基本性质(self):
        self.assertEqual(DB._month_bound("", "lo"), "0000-01-01")
        self.assertEqual(DB._month_bound("", "hi"), "9999-12-31")
        for s in ["2024", "2024-05", "2024-02-29"]:
            self.assertLessEqual(DB._month_bound(s, "lo"), DB._month_bound(s, "hi"))

    def test_不填日期等于不限(self):
        self.assertEqual(len(self.rel()), len(self.rel(date_from="", date_to="")))


class TestSiteFilter(TempDbCase):
    def test_单站筛出该站全部关系(self):
        rows = self.rel(site="cac")
        self.assertEqual({r["site"] for r in rows}, {"cac"})
        self.assertEqual(len(rows), 12, "cac 有 3 篇文章 × 4 条")

    def test_多站是各站之和(self):
        a, b = len(self.rel(site="cac")), len(self.rel(site="zjwx"))
        self.assertEqual(len(self.rel(site="cac,zjwx")), a + b)

    def test_站点与日期是叠加不是覆盖(self):
        """cac 的 3 篇文章都在 2024 年（1 月/7 月/12 月），每篇 4 条关系
        → 叠加筛出 12 条。若两个筛子互相覆盖，只筛站点也是 12，
        只筛日期是 16，**三者不等**才说明是真叠加。"""
        by_site = len(self.rel(site="cac"))
        by_date = len(self.rel(date_from="2024", date_to="2024"))
        both = len(self.rel(site="cac", date_from="2024", date_to="2024"))
        self.assertEqual((by_site, by_date, both), (12, 16, 12))
        self.assertLess(both, by_date, "叠加结果比只筛日期还多 → 站点筛选没生效")

    def test_逗号分隔容忍空格(self):
        self.assertEqual(len(self.rel(site="cac, zjwx ")), len(self.rel()))

    def test_不存在的站点返回空而不是全量(self):
        self.assertEqual(self.rel(site="查无此站"), [])

    def test_返回里带得上站点字段(self):
        """前端的口径面板要显示站点分布，字段必须查出来。"""
        self.assertTrue(all("site" in r for r in self.rel(site="cac")))


class TestScopeConsistency(TempDbCase):
    """坑 ①④：筛选在建图之前发生，且排名与画图同口径"""

    def _graph(self, **kw):
        rows = self.rel(**kw)
        G, types = GA.build_graph(rows)
        return G, types

    def test_筛站点后建出的图不含别站的关系(self):
        G, _ = self._graph(site="cac")
        # 建图用的是筛选后的行，所以图规模必须小于全量
        G_all, _ = self._graph()
        self.assertLessEqual(G.number_of_edges(), G_all.number_of_edges())

    def test_关系集合确实被筛过而非事后挑边(self):
        """若只挑边不换来源，2024 年那张图的边权应与全图一致；
        实际应当不同（同一条边在全时期共现次数更多）。"""
        G_all, _ = self._graph()
        G_24, _ = self._graph(date_from="2024", date_to="2024")
        self.assertTrue(G_24.number_of_nodes() > 0)
        # 2024 只覆盖 4/6 篇文章 → 权重必然变小
        w_all = sum(d.get("weight", 0) for _, _, d in G_all.edges(data=True))
        w_24 = sum(d.get("weight", 0) for _, _, d in G_24.edges(data=True))
        self.assertLess(w_24, w_all, f"{w_24} vs {w_all}")

    def test_crop_scope与分析共用同一张图(self):
        G, types = self._graph(site="cac")
        res = GA.analyze(G, types, topn=10)
        sub, note = GA.crop_scope(G, focus="中央网信办", hop=1)
        self.assertGreater(sub.number_of_nodes(), 0)
        self.assertIn("中央网信办", note)
        # 排名表必须等于在子图上算的结果
        r_sub = GA.analyze(sub, types, topn=10)
        self.assertEqual([n["name"] for n in r_sub["degree"]][:2],
                         [n["name"] for n in r_sub["degree"]][:2])
        self.assertNotEqual(
            [n["betweenness"] for n in r_sub["betweenness"]],
            [n["betweenness"] for n in res["betweenness"]],
            "子图介数与全图完全一致 → 重算没生效")

    def test_空子图不静默回退成全图(self):
        G, _ = self._graph(site="cac")
        sub, note = GA.crop_scope(G, focus="查无此机构")
        self.assertEqual(sub.number_of_nodes(), 0)
        self.assertIn("查无此机构", note)


class TestVisAndAnalysisAlign(TempDbCase):
    """屏幕 = 导出：两个接口的筛选参数必须完全一致"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import inspect
        import importlib
        A = importlib.import_module("cacx.web.app")
        vis = set(inspect.signature(A.api_graph_vis).parameters)
        ana = set(inspect.signature(A.api_graph_analysis).parameters)
        cls.vis, cls.ana = vis, ana

    def test_两个接口都有站点与日期参数(self):
        for p in ("site", "date_from", "date_to"):
            self.assertIn(p, self.vis, f"/api/graph/vis 缺 {p} —— "
                                       f"页面筛完导出却是全图")
            self.assertIn(p, self.ana, f"/api/graph/analysis 缺 {p}")

    def test_前端gpQuery带上了站点与日期(self):
        html = (Path(__file__).resolve().parent.parent
                / "cacx" / "web" / "static" / "index.html").read_text(
                    encoding="utf-8")
        i = html.find("function gpQuery()")
        self.assertGreater(i, 0, "找不到 gpQuery")
        seg = html[i:i + 1200]
        for k in ("'site'", "'date_from'", "'date_to'", "gp_sites"):
            self.assertIn(k, seg, f"gpQuery 没带 {k}")

    def test_前端有站点与日期的输入控件(self):
        html = (Path(__file__).resolve().parent.parent
                / "cacx" / "web" / "static" / "index.html").read_text(
                    encoding="utf-8")
        for eid in ("gp_sites", "gp_dfrom", "gp_dto"):
            self.assertIn(f'id="{eid}"', html, f"页面上没有 {eid} 控件")

    def test_facets给出站点与年份候选项(self):
        """前端不许硬编码站点名 —— 站点改名就会静默筛空。"""
        import inspect
        import importlib
        A = importlib.import_module("cacx.web.app")
        src = inspect.getsource(A.api_graph_facets)
        self.assertIn("sites", src)
        self.assertIn("years", src)

    def test_清空筛选会把站点和日期一并复位(self):
        html = (Path(__file__).resolve().parent.parent
                / "cacx" / "web" / "static" / "index.html").read_text(
                    encoding="utf-8")
        i = html.find("function gpReset()")
        self.assertGreater(i, 0)
        seg = html[i:i + 700]
        for k in ("gp_dfrom", "gp_dto", "gp_sites"):
            self.assertIn(k, seg, f"gpReset 没复位 {k} —— 清不干净")


class TestPayloadIntegration(TempDbCase):
    """⚠ 这一层才是真正出 bug 的地方。

    坑④⑤都发生在 **集成层**（_graph_payload 决定 analyze 跑在哪张图上），
    而 crop_scope 本身没问题 —— 只测底层函数的话，上面两处退化一律测不出来。
    所以这里把 app._db 换成临时库，直接调 _graph_payload。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib
        from unittest import mock
        cls.app = importlib.import_module("cacx.web.app")
        cls._mock = mock.patch.object(cls.app, "_db", lambda: cls.d)
        cls._mock.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._mock.stop()
        except Exception:
            pass
        super().tearDownClass()

    def pay(self, **kw):
        p = dict(min_degree=1, limit=100, min_weight=1.0, topn=10)
        p.update(kw)
        return self.app._graph_payload(**p)

    def _btw(self, d):
        return {n["name"]: n["betweenness"] for n in d["betweenness"]}

    def test_聚焦时排名跑在子图上而非全图(self):
        """坑④。界面口径面板写着「介数只在这张子图里算」，
        排名表就必须是子图的数 —— 否则是骗用户。"""
        allp = self.pay()
        sub = self.pay(focus="中央网信办", hop=1)
        self.assertGreater(sub["filters"]["scope_nodes"], 0)
        self.assertNotEqual(self._btw(sub), self._btw(allp),
                            "聚焦后排名与全图完全一致 → analyze 仍跑全图")
        # 与手工在子图上算的结果对齐（不能只是"不一样"）
        G, types = GA.build_graph(self.rel())
        s, _ = GA.crop_scope(G, focus="中央网信办", hop=1)
        ref = {n["name"]: n["betweenness"]
               for n in GA.analyze(s, types, topn=10)["betweenness"]}
        for k, v in self._btw(sub).items():
            self.assertAlmostEqual(v, ref[k], places=6, msg=f"「{k}」对不上")

    def test_排名与画出来的节点同口径(self):
        """同一个节点在排名表和图里必须是同一个介数，
        否则界面上两张表自相矛盾。"""
        p = self.pay(focus="中央网信办", hop=1, site="cac")
        rank = self._btw(p)
        drawn = {n["id"]: n["betweenness"] for n in p["graph"]["nodes"]}
        shared = [k for k in drawn if k in rank]
        self.assertTrue(shared, "没有可比对的节点")
        for k in shared:
            self.assertAlmostEqual(drawn[k], rank[k], places=4, msg=f"「{k}」两处不一致")

    def test_空子图不给全图排名冒充(self):
        """坑⑤。焦点查无此机构时，宁可列表为空，
        也不能把全库数字摆在「子图口径」的位置上。"""
        p = self.pay(focus="查无此机构")
        self.assertEqual(self._btw(p), {}, "空子图却返回了排名")
        for k in ("degree", "betweenness", "pagerank", "strength"):
            self.assertEqual(p.get(k), [], f"{k} 该清空")
        self.assertEqual(p["graph"]["nodes"], [])

    def test_站点筛选反映在关系条数上(self):
        allp, onec = self.pay(), self.pay(site="cac")
        self.assertEqual(onec["filters"]["rel_rows"], 12)
        self.assertEqual(onec["filters"]["rel_rows_all"], 24)
        self.assertEqual(allp["filters"]["rel_rows"], 24)

    def test_日期筛选反映在关系条数上(self):
        p = self.pay(date_from="2024", date_to="2024")
        self.assertEqual(p["filters"]["rel_rows"], 16)
        self.assertEqual(p["filters"]["rel_rows_all"], 24)

    def test_筛站点后图里不含别站的节点(self):
        """关系筛了，图必须跟着筛 —— 图只从筛选后的关系建。"""
        only = self.pay(site="zjwx", date_from="2026", date_to="2026")
        self.assertEqual(only["filters"]["rel_rows"], 4)
        self.assertEqual(only["filters"]["graph_edges_typed"],
                         len(only["graph"]["edges"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
