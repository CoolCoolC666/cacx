# -*- coding: utf-8 -*-
"""
A1 + A3：facets 接日期筛选 + 抽样上限放宽到 200000
================================================================
A1：facets_by_site 的 columns 两条 SQL 加日期过滤，sites/total 等保持全量。
A3：sample_articles 的 min(limit, 5000) → 200000，路由 Query le 同步。

跑法：py -3 tests_facets_dates.py
"""
import inspect
import sys
import tempfile
import _temppool
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib                     # noqa: E402
A = importlib.import_module("cacx.web.app")
from cacx import db as DB            # noqa: E402


# 4 篇文章：两站 × 两栏目 × 跨年份
ARTICLES = [
    # id, site,  column_code, column_name, pub_day
    (1, "cac",  "c1", "网信要闻",   "2024-01-15"),
    (2, "cac",  "c2", "政策发布",   "2024-06-20"),
    (3, "zjwx", "c3", "浙江动态",   "2024-03-10"),
    (4, "zjwx", "c3", "浙江动态",   "2025-01-05"),
]


def _make_db(path):
    d = DB.Database(path)
    d.conn.executemany(
        "INSERT INTO articles(id,site,column_code,column_name,title,url,"
        "pub_day,year,month,chars,content_empty,content_text) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(a[0], a[1], a[2], a[3], f"标题{a[0]}", f"http://x/{a[0]}",
          a[4], int(a[4][:4]), int(a[4][5:7]), 100, 0, "正文")
         for a in ARTICLES])
    d.conn.commit()
    return d


class TestFacetsDateFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(_temppool.mkdtemp(prefix="cacx_fd_"))
        cls.d = _make_db(cls.tmp / "t.db")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()
        except Exception:
            pass

    def _cols(self, f):
        return {c["column_name"]: c["n"] for c in f["columns"]}

    def test_空日期等于全量(self):
        f = self.d.facets_by_site("")
        cols = self._cols(f)
        self.assertEqual(cols["网信要闻"], 1)
        self.assertEqual(cols["政策发布"], 1)
        self.assertEqual(cols["浙江动态"], 2)

    def test_date_from过滤栏目(self):
        """2024-04-01 起：文章 1（1月，网信要闻）和文章 3（3月，浙江动态）
        都在区间外，只剩文章 2（6月，政策发布）和文章 4（2025-01，浙江动态）。"""
        f = self.d.facets_by_site("", date_from="2024-04-01")
        cols = self._cols(f)
        self.assertNotIn("网信要闻", cols, "网信要闻只在文章 1，文章 1 在区间外")
        self.assertIn("政策发布", cols)
        self.assertEqual(cols["浙江动态"], 1, "只剩文章 4（2025-01）在区间内")

    def test_date_to过滤栏目(self):
        """截止 2024-12-31：文章 4（2025-01，浙江动态）被排除。"""
        f = self.d.facets_by_site("", date_to="2024-12-31")
        cols = self._cols(f)
        self.assertEqual(cols["浙江动态"], 1, "只剩文章 3")
        self.assertIn("政策发布", cols)

    def test_site与日期组合(self):
        """site=cac + date_to=2024-03-31：只剩文章 1（网信要闻）。"""
        f = self.d.facets_by_site("cac", date_to="2024-03-31")
        cols = self._cols(f)
        self.assertSetEqual(set(cols), {"网信要闻"})
        # 单站时 columns 里 site 字段必须是 cac
        self.assertTrue(all(c["site"] == "cac" for c in f["columns"]))

    def test_sites字段不被日期过滤(self):
        """⚠ 铁律：站点列表要全量供切站点，不能跟着日期缩。"""
        f = self.d.facets_by_site("", date_from="2025-01-01")
        site_names = {s["site"] for s in f["sites"]}
        self.assertIn("cac", site_names, "cac 在 2025 无文章，但站点列表仍要列出")
        self.assertIn("zjwx", site_names)

    def test_total不被日期过滤(self):
        """total/chars/days 是库级口径，不能跟着 columns 的日期缩。"""
        f_all = self.d.facets_by_site("")
        f_few = self.d.facets_by_site("", date_from="2025-01-01")
        self.assertEqual(f_all["total"], f_few["total"],
                         "total 跟着日期变了 —— 站点列表/分母口径被误滤")


class TestFacetsRoutePassthrough(unittest.TestCase):
    """路由层：date_from/date_to 空则不传 db。"""

    def setUp(self):
        self.fake = mock.MagicMock()
        self._orig = A._db
        A._db = lambda: self.fake

    def tearDown(self):
        A._db = self._orig

    def test_路由透传site和日期(self):
        A.api_facets(site="cac", date_from="2024-01-01", date_to="2024-12-31")
        _, kw = self.fake.facets_by_site.call_args
        self.assertEqual(kw.get("date_from"), "2024-01-01")
        self.assertEqual(kw.get("date_to"), "2024-12-31")

    def test_路由空日期不传db(self):
        A.api_facets(site="cac", date_from="", date_to="")
        _, kw = self.fake.facets_by_site.call_args
        self.assertNotIn("date_from", kw, "空串 date_from 透传了")
        self.assertNotIn("date_to", kw, "空串 date_to 透传了")


class TestSampleLimitRaised(unittest.TestCase):
    """A3：min(limit, 5000) → min(limit, 200000)。"""

    def test_db层min上限改为200000(self):
        """白盒：源码里 min 的上限必须是 200000，不能还是 5000。"""
        src = inspect.getsource(DB.Database.sample_articles)
        self.assertIn("min(int(limit), 200000)", src,
                      "db 层 min 上限没改成 200000")

    def test_路由Query上限改为200000(self):
        """白盒：两个路由的 Query le 必须是 200000。
        FastAPI Query 的 le/ge 在 p.metadata 里（pydantic Le/Ge 约束对象，
        不同 pydantic 版本导入路径不同，用属性鸭子类型判断）。"""
        for fn in (A.api_sample, A.api_sample_export):
            sig = inspect.signature(fn)
            q = sig.parameters["limit"].default
            les = [getattr(m, "le", None) for m in q.metadata
                   if hasattr(m, "le")]
            self.assertIn(200000, les,
                          f"{fn.__name__} 的 limit le 里没有 200000：{les}")

    def test_mcp_server口径同步(self):
        """mcp_server docstring 的「1~5000」必须改成「1~200000」。"""
        import re
        from cacx import mcp_server as M
        src = inspect.getsource(M)
        self.assertIn("1~200000", src,
                      "mcp_server docstring 的上限口径没同步")
        self.assertNotIn("1~5000", src,
                         "mcp_server 里还残留「1~5000」口径")


if __name__ == "__main__":
    unittest.main(verbosity=2)
