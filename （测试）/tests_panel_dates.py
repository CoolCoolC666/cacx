# -*- coding: utf-8 -*-
"""
T1 后端：面板三张图（problems / orgs / joint）接上日期筛选
================================================================
只验**透传**这一件事：
  1) 路由收到 date_from / date_to 后真的进了 db 方法的 kwargs；
  2) 前端不传（收到 ""）时**绝不**把空串透传给 SQL ——
     `a.pub_day >= ''` 在 SQLite 里恒真（碰巧对），换 PostgreSQL 就错。
  3) orgs 的 limit 参数不能因为加日期就丢。

跑法：py -3 tests_panel_dates.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ⚠ 必须用 importlib 取**模块**：`from cacx.web import app` 拿到的是
#   FastAPI 实例，_db 不在它上面。与 tests_timeline.py 同一约定。
import importlib                     # noqa: E402
A = importlib.import_module("cacx.web.app")


class TestPanelDatePassthrough(unittest.TestCase):
    """打桩 _db，断言 db 方法**收到的 kwargs**。

    真库跑通是 db 层自己的事（problem_distribution / top_orgs /
    joint_issue_trend 本来就有日期参数），这里只验**接线**。
    """

    def setUp(self):
        self.fake = mock.MagicMock()
        self._orig = A._db
        A._db = lambda: self.fake

    def tearDown(self):
        A._db = self._orig

    # ---------- problems ----------
    def test_problems_透传日期(self):
        A.api_panel_problems(site="cac", date_from="2024-01-01",
                             date_to="2024-06-30")
        _, kw = self.fake.problem_distribution.call_args
        self.assertEqual(kw.get("site"), "cac")
        self.assertEqual(kw.get("date_from"), "2024-01-01")
        self.assertEqual(kw.get("date_to"), "2024-06-30")

    def test_problems_空值不传(self):
        """⚠ 铁律：前端不传时收到 ""，绝不能透传给 SQL。"""
        A.api_panel_problems(site="cac", date_from="", date_to="")
        _, kw = self.fake.problem_distribution.call_args
        self.assertNotIn("date_from", kw, "空串 date_from 透传了")
        self.assertNotIn("date_to", kw, "空串 date_to 透传了")

    # ---------- orgs ----------
    def test_orgs_透传日期且保留limit(self):
        A.api_panel_orgs(site="zjwx", limit=20, date_from="2025-01-01",
                         date_to="2025-12-31")
        _, kw = self.fake.top_orgs.call_args
        self.assertEqual(kw.get("site"), "zjwx")
        self.assertEqual(kw.get("date_from"), "2025-01-01")
        self.assertEqual(kw.get("date_to"), "2025-12-31")
        self.assertEqual(kw.get("limit"), 20, "加日期不能把 limit 弄丢")

    def test_orgs_空值不传(self):
        A.api_panel_orgs(site="", limit=12, date_from="", date_to="")
        _, kw = self.fake.top_orgs.call_args
        self.assertNotIn("date_from", kw)
        self.assertNotIn("date_to", kw)

    # ---------- joint ----------
    def test_joint_透传日期(self):
        A.api_panel_joint(site="cac", date_from="2024-07-01",
                          date_to="2024-12-31")
        _, kw = self.fake.joint_issue_trend.call_args
        self.assertEqual(kw.get("site"), "cac")
        self.assertEqual(kw.get("date_from"), "2024-07-01")
        self.assertEqual(kw.get("date_to"), "2024-12-31")

    def test_joint_空值不传(self):
        A.api_panel_joint(site="", date_from="", date_to="")
        _, kw = self.fake.joint_issue_trend.call_args
        self.assertNotIn("date_from", kw)
        self.assertNotIn("date_to", kw)

    # ---------- 边界 ----------
    def test_只传一半日期(self):
        """前端只填起、不填止：只传有的那个，另一个走 db 默认值。"""
        A.api_panel_problems(site="", date_from="2024-01-01", date_to="")
        _, kw = self.fake.problem_distribution.call_args
        self.assertEqual(kw.get("date_from"), "2024-01-01")
        self.assertNotIn("date_to", kw)


class TestPanelDatesRealRun(unittest.TestCase):
    """真跑一次：临时空库上三个路由不炸、返回结构与旧版一致。"""

    @classmethod
    def setUpClass(cls):
        from cacx import db as DB
        cls.tmp = Path(tempfile.mkdtemp(prefix="cacx_pd_"))
        cls.d = DB.Database(cls.tmp / "t.db")
        cls._orig = A._db
        A._db = lambda: cls.d

    @classmethod
    def tearDownClass(cls):
        A._db = cls._orig
        try:
            cls.d.conn.close()
        except Exception:
            pass

    def test_三个路由都能跑且有items键(self):
        for fn in (A.api_panel_problems, A.api_panel_joint):
            r = fn(site="", date_from="2024-01-01", date_to="2099-12-31")
            self.assertIn("items", r)
            self.assertIn("note", r)
        r = A.api_panel_orgs(site="", limit=5, date_from="", date_to="")
        self.assertIn("items", r)
        self.assertIn("review_count", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
