# -*- coding: utf-8 -*-
"""
分析样本抽样（stratified / recent）。

守门的是一条很容易被忽略的约束：
老师要求「发文量与主题随时间的变化」，
所以样本**必须覆盖整个时间窗**，而不是只覆盖最近那一段。
「取最近 N 篇」看起来最自然，但它恰恰会丢掉早期年份。
"""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import db as D              # noqa: E402


class T(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_sample_"))
        self.d = D.open_db({"db": {"path": str(self.tmp / "t.db")},
                            "ai": {"provider": "none"}})
        self.WA = importlib.import_module("cacx.web.app")
        self.old = self.WA.DB
        self.WA.DB = self.d
        self.c = TestClient(self.WA.app)
        self.assertIn("cacx_sample_", str(self.d.path))
        # cac·网信要闻：2024=186 / 2025=289 / 2026=266，共 741
        self.seed("cac", "网信要闻", 186, "2024")
        self.seed("cac", "网信要闻", 289, "2025")
        self.seed("cac", "网信要闻", 266, "2026")
        # cac·时政要闻：只有一个年份，用来验证「分层不适用时」
        self.seed("cac", "时政要闻", 300, "2025")

    def tearDown(self):
        self.WA.DB = self.old

    def seed(self, site, col, n, year):
        # url 是 (site,url) 唯一约束的一部分，必须每条都不一样
        for i in range(n):
            d = f"{year}-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
            self.d.conn.execute(
                "INSERT INTO articles(site,column_code,column_name,title,"
                "pub_date,pub_day,year,month,url,chars,content_text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (site, "X", col, f"{col}标题{i}", d, d, year, d[5:7],
                 f"http://e/{site}/{col}/{year}/{i}", 500, "正文" * 50))
        self.d.conn.commit()

    # ------------------------------------------------ 核心约束
    def test_分层抽样覆盖全部年份(self):
        """这条是整个功能成立的前提。取最近 500 篇会丢掉 2024。"""
        r = self.d.sample_articles("cac", "网信要闻", limit=500)
        self.assertEqual(r["n"], 500, r["years_taken"])
        self.assertEqual(set(r["years_taken"]),
                         {"2024", "2025", "2026"},
                         f"分层抽样必须每年都取到：{r['years_taken']}")

    def test_对比_取最近N篇会丢年份(self):
        """把反面证据也钉住，说明为什么默认不是 recent。"""
        recent = self.d.sample_articles("cac", "网信要闻", limit=500,
                                        strategy="recent")
        strat = self.d.sample_articles("cac", "网信要闻", limit=500)
        self.assertEqual(recent["n"], 500)
        self.assertGreater(recent["date_min"], strat["date_min"],
                           "recent 取最新N篇，最早一篇应当更靠后")
        self.assertTrue(recent["date_min"] >= "2025-01-01",
                        f"取最近500篇竟然覆盖到了早期：{recent['date_min']}")

    def test_每年配额大致均等(self):
        r = self.d.sample_articles("cac", "网信要闻", limit=300)
        per = list(r["years_taken"].values())
        self.assertLessEqual(max(per) - min(per), 3, r["years_taken"])

    # ------------------------------------------------ 基本约束
    def test_不超过limit(self):
        r = self.d.sample_articles("cac", "网信要闻", limit=100)
        self.assertEqual(r["n"], 100)

    def test_池子小就返回全量(self):
        r = self.d.sample_articles("cac", "时政要闻", limit=500)
        self.assertEqual(r["n"], 300)
        self.assertEqual(r["limit"], 500)

    def test_池子更小时也返回全量(self):
        r = self.d.sample_articles("cac", "时政要闻", limit=5000)
        self.assertEqual(r["n"], 300)

    def test_按栏目过滤(self):
        r = self.d.sample_articles("cac", "时政要闻", limit=500)
        self.assertEqual({i["column_name"] for i in r["items"]}, {"时政要闻"})

    def test_按站点过滤(self):
        r = self.d.sample_articles(site="cac", limit=500)
        self.assertEqual({i["site"] for i in r["items"]}, {"cac"})

    def test_区间可调(self):
        r = self.d.sample_articles("cac", "网信要闻", limit=1000,
                                   date_from="2025-01-01")
        self.assertTrue(all(i["pub_day"] >= "2025-01-01" for i in r["items"]))
        self.assertNotIn("2024", r["years_taken"])

    def test_空结果不炸(self):
        r = self.d.sample_articles(column="根本没有这个栏目", limit=500)
        self.assertEqual(r["n"], 0)
        self.assertEqual(r["items"], [])
        self.assertEqual(r["date_min"], "")

    def test_非法策略被拒(self):
        with self.assertRaises(ValueError):
            self.d.sample_articles(limit=10, strategy="随便")

    def test_limit被夹到安全范围(self):
        self.assertEqual(
            self.d.sample_articles("cac", limit=0)["limit"], 1)
        # A3：上限从 5000 放宽到 200000；99999 在新上限内不再被夹
        self.assertEqual(
            self.d.sample_articles("cac", limit=99999)["limit"], 99999)
        self.assertEqual(
            self.d.sample_articles("cac", limit=999999)["limit"], 200000)

    # ------------------------------------------------ 可用性
    def test_只回元数据不带正文(self):
        """样本清单几百条，不能把正文一起吐出来，那是另一个接口的事。"""
        r = self.d.sample_articles("cac", limit=50)
        self.assertNotIn("content_text", r["items"][0])

    def test_正文为空的单独统计(self):
        self.d.conn.execute(
            "UPDATE articles SET content_empty=1 WHERE year='2024'")
        self.d.conn.commit()
        r = self.d.sample_articles("cac", "网信要闻", limit=500)
        self.assertEqual(r["n"], 500)
        self.assertLess(r["n_usable"], 500, "空正文的篇数应被单独报出来")

    # ------------------------------------------------ 接口
    def test_接口默认不吐items(self):
        r = self.c.get("/api/sample?site=cac&column=网信要闻&limit=500").json()
        self.assertNotIn("items", r)
        self.assertEqual(r["items_total"], 500)
        self.assertLessEqual(len(r["items_sample"]), 20)

    def test_接口可要全量items(self):
        r = self.c.get(
            "/api/sample?site=cac&limit=500&with_items=true").json()
        self.assertEqual(len(r["items"]), 500)

    def test_接口非法人参400(self):
        self.assertEqual(
            self.c.get("/api/sample?strategy=nope").status_code, 400)

    def test_候选清单接口(self):
        r = self.c.get("/api/sample/options").json()
        names = {(x["site"], x["column_name"]) for x in r["columns"]}
        self.assertIn(("cac", "网信要闻"), names)
        self.assertIn("stratified", r["strategies"])

    def test_逐年统计接口(self):
        r = self.c.get("/api/sample/years?site=cac&column=网信要闻").json()
        self.assertEqual([x["y"] for x in r["years"]],
                         ["2024", "2025", "2026"])
        self.assertEqual(sum(x["n"] for x in r["years"]), 741)

    def test_抽样结果每条都能回到文章(self):
        ids = [i["id"] for i in
               self.d.sample_articles("cac", limit=100)["items"]]
        got = self.d.conn.execute(
            f"SELECT COUNT(*) FROM articles WHERE id IN "
            f"({','.join('?' * len(ids))})", ids).fetchone()[0]
        self.assertEqual(got, len(ids))


if __name__ == "__main__":
    unittest.main(verbosity=2)