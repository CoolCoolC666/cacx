# -*- coding: utf-8 -*-
"""
T3 后端：边列表 graph() 接 articles JOIN 后吃 site/date 筛选
================================================================
背景：/api/graph/analysis（画布）早就吃 site/date，边列表 /api/graph
却只有 limit —— 画布筛了、边列表不筛，两个数字打架。

本文件钉三件事：
  1) db.graph() 两条 SQL JOIN articles 后，site/date 真的筛到文章；
  2) site="" 不筛（SQL 里 `? = '' OR a.site = ?` 短路）；
  3) 路由层空 date_from/date_to 不传 db（SQLite 碰巧对、PostgreSQL 会错）。

跑法：py -3 tests_graph_filter_join.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib                     # noqa: E402
A = importlib.import_module("cacx.web.app")
from cacx import db as DB            # noqa: E402


# 4 篇文章：两站 × 两年份，实体/关系分别挂在不同文章上
ARTICLES = [
    # id, site,    pub_day
    (1, "cac",   "2024-01-15"),
    (2, "cac",   "2024-06-20"),
    (3, "zjwx",  "2024-03-10"),
    (4, "zjwx",  "2025-01-05"),
]

# entities: (name, type, article_id)
ENTITIES = [
    ("X", "org",     1),
    ("Y", "org",     1),
    ("Y", "org",     2),
    ("Z", "policy",  2),
    ("Z", "policy",  3),
    ("W", "problem", 3),
    ("W", "problem", 4),
    ("V", "org",     4),
]

# relations: (src, dst, rel_type, article_id, weight)
RELATIONS = [
    ("X", "Y", "joint_issue", 1, 1.0),
    ("Y", "Z", "issue",       2, 2.0),
    ("Z", "W", "target",      3, 1.5),
    ("W", "V", "adopt",       4, 1.0),
]


def _make_db(path):
    d = DB.Database(path)
    d.conn.executemany(
        "INSERT INTO articles(id,site,column_name,title,url,pub_day,"
        "chars,content_empty,content_text) VALUES(?,?,?,?,?,?,?,?,?)",
        [(a[0], a[1], "col", f"标题{a[0]}", f"http://x/{a[0]}", a[2],
          100, 0, "正文") for a in ARTICLES])
    d.conn.executemany(
        "INSERT INTO entities(name,type,article_id,aliases,evidence,"
        "extractor,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
        [(e[0], e[1], e[2], "", e[0], "rule", 0.95, "2026-01-01")
         for e in ENTITIES])
    d.conn.executemany(
        "INSERT INTO relations(src_name,dst_name,rel_type,article_id,"
        "weight,evidence,extractor,created_at) VALUES(?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], r[4], r[0] + r[1], "rule",
          "2026-01-01") for r in RELATIONS])
    d.conn.commit()
    return d


class TestGraphJoinFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cacx_gj_"))
        cls.d = _make_db(cls.tmp / "t.db")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()
        except Exception:
            pass
        cls.d = None

    def _names(self, g):
        return {n["name"]: n["n"] for n in g["nodes"]}

    def _edges(self, g):
        return {(e["src_name"], e["dst_name"]) for e in g["edges"]}

    def test_不筛时全量(self):
        g = self.d.graph()
        names = self._names(g)
        self.assertSetEqual(set(names), {"X", "Y", "Z", "W", "V"})
        self.assertEqual(names["Y"], 2, "Y 在文章 1/2 各出现一次")
        self.assertEqual(names["Z"], 2)
        self.assertSetEqual(self._edges(g),
                            {("X", "Y"), ("Y", "Z"), ("Z", "W"), ("W", "V")})

    def test_site_cac只出cac文章里的(self):
        g = self.d.graph(site="cac")
        names = self._names(g)
        self.assertSetEqual(set(names), {"X", "Y", "Z"})
        self.assertNotIn("W", names)
        self.assertNotIn("V", names)
        self.assertSetEqual(self._edges(g), {("X", "Y"), ("Y", "Z")})

    def test_site_zjwx只出zjwx文章里的(self):
        g = self.d.graph(site="zjwx")
        names = self._names(g)
        self.assertSetEqual(set(names), {"Z", "W", "V"})
        self.assertNotIn("X", names)
        self.assertNotIn("Y", names)
        self.assertSetEqual(self._edges(g), {("Z", "W"), ("W", "V")})

    def test_空site不筛(self):
        g = self.d.graph(site="")
        self.assertSetEqual(set(self._names(g)), {"X", "Y", "Z", "W", "V"})
        self.assertEqual(len(g["edges"]), 4)

    def test_date_from筛掉早于区间的文章(self):
        """2024-04-01 起：文章 1（1月）被排除，剩下 2/3/4。"""
        g = self.d.graph(date_from="2024-04-01")
        names = self._names(g)
        self.assertNotIn("X", names, "X 只在文章 1，文章 1 在区间外")
        self.assertSetEqual(set(names), {"Y", "Z", "W", "V"})
        self.assertNotIn(("X", "Y"), self._edges(g))
        self.assertIn(("Y", "Z"), self._edges(g))

    def test_date_to筛掉晚于区间的文章(self):
        """截止 2024-12-31：文章 4（2025-01）被排除。"""
        g = self.d.graph(date_to="2024-12-31")
        names = self._names(g)
        self.assertNotIn("V", names, "V 只在文章 4，文章 4 在区间外")
        self.assertSetEqual(self._edges(g),
                            {("X", "Y"), ("Y", "Z"), ("Z", "W")})

    def test_site与日期组合(self):
        """site=zjwx + 2024 全年：只剩文章 3（2024-03），文章 4 是 2025 被排。"""
        g = self.d.graph(site="zjwx", date_from="2024-01-01",
                         date_to="2024-12-31")
        names = self._names(g)
        self.assertSetEqual(set(names), {"Z", "W"})
        self.assertNotIn("V", names)
        self.assertSetEqual(self._edges(g), {("Z", "W")})

    def test_limit生效(self):
        g = self.d.graph(limit=2)
        self.assertLessEqual(len(g["edges"]), 2)
        self.assertLessEqual(len(g["nodes"]), 2)

    def test_article_ids回溯仍可用(self):
        """边列表带 article_id 是「点行出原文」的命脉，任何时候都不能坏。"""
        g = self.d.graph(site="cac")
        for e in g["edges"]:
            self.assertTrue(e["article_ids"], f"边 {e} 没带回溯文章 id")


class TestGraphRoutePassthrough(unittest.TestCase):
    """路由层：site/limit 总传，date_from/date_to 空则不传 db。"""

    def setUp(self):
        self.fake = mock.MagicMock()
        self._orig = A._db
        A._db = lambda: self.fake

    def tearDown(self):
        A._db = self._orig

    def test_路由透传site和limit(self):
        A.api_graph(site="cac", limit=50)
        _, kw = self.fake.graph.call_args
        self.assertEqual(kw.get("site"), "cac")
        self.assertEqual(kw.get("limit"), 50)

    def test_路由空日期不传db(self):
        """⚠ 前端不传时收到 ""，绝不能把空串透传给 SQL。"""
        A.api_graph(site="cac", date_from="", date_to="")
        _, kw = self.fake.graph.call_args
        self.assertNotIn("date_from", kw, "空串 date_from 透传了")
        self.assertNotIn("date_to", kw, "空串 date_to 透传了")

    def test_路由有日期就传(self):
        A.api_graph(site="zjwx", date_from="2024-01-01", date_to="2024-12-31")
        _, kw = self.fake.graph.call_args
        self.assertEqual(kw.get("date_from"), "2024-01-01")
        self.assertEqual(kw.get("date_to"), "2024-12-31")


if __name__ == "__main__":
    unittest.main(verbosity=2)
