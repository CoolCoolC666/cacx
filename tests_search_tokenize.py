# -*- coding: utf-8 -*-
"""分词检索（tokenize / op）"""
import shutil
import tempfile
import unittest
from pathlib import Path

from cacx.db import Database, _split_query, _tokenize_for_search


def _mk(rows):
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    db = Database(str(tmp))
    db.conn.execute("""CREATE TABLE IF NOT EXISTS articles(
      id INTEGER PRIMARY KEY, site TEXT, site_name TEXT, column_name TEXT,
      column_code TEXT, title TEXT, pub_date TEXT, pub_day TEXT, url TEXT,
      source_text TEXT, chars INTEGER, content_text TEXT, year TEXT,
      content_empty INTEGER DEFAULT 0, first_seen TEXT)""")
    for i, (t, c) in enumerate(rows, 1):
        db.conn.execute(
            "INSERT INTO articles(id,site,site_name,column_name,column_code,"
            "title,pub_date,pub_day,url,source_text,chars,content_text,year)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "cac", "中国网信网", "网信要闻", "xx", t, "2025-01-01",
             "2025-01-01", "u%d" % i, "新华社", len(c), c, "2025"))
    db.conn.commit()
    return db, tmp.parent


ROWS = [
    ("清朗网络谣言专项行动部署", "关于开展清朗网络谣言专项行动的通知"),
    ("算法推荐管理规定发布", "关于算法推荐管理规定的情况"),
    ("未成年人网络保护条例", "未成年人网络保护工作部署"),
    ("公安部部署打击网络水军", "公安部有关网络水军专项行动"),
    ("完全无关的一篇", "某地举办马拉松赛事"),
]


class TestSplit(unittest.TestCase):
    def test_显式分隔符(self):
        self.assertEqual(_split_query("中央网信办 网络安全，净网"),
                         ["中央网信办", "网络安全", "净网"])

    def test_空串不炸(self):
        self.assertEqual(_split_query(""), [])
        self.assertEqual(_split_query(None), [])

    def test_默认不分词(self):
        self.assertEqual(_tokenize_for_search("清朗网络谣言", False),
                         ["清朗网络谣言"])

    def test_分词后去重且保序(self):
        got = _tokenize_for_search("清朗 清朗 网络谣言", True)
        self.assertEqual(len(got), len(set(got)), got)
        self.assertIn("清朗", got)
        self.assertIn("网络谣言", got)


class TestFtsQuote(unittest.TestCase):
    def test_引号注入被转义(self):
        q = Database._fts_quote('he"llo', False)
        self.assertEqual(q, '"he""llo"')

    def test_or_用_OR_连接(self):
        self.assertEqual(Database._fts_quote("a b", False, "OR"),
                         '"a" OR "b"')

    def test_and_是默认(self):
        self.assertEqual(Database._fts_quote("a b"), '"a" AND "b"')

    def test_空查询返回空串(self):
        self.assertEqual(Database._fts_quote(""), "")


class TestSearchTokenize(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _mk(ROWS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_精确检索整串命中(self):
        r = self.db.search(q="算法推荐管理规定", limit=10)
        self.assertGreaterEqual(r["total"], 1)

    def test_整串搜不到时_分词能捞回(self):
        """⚠ 这是本功能的立论：中文没有空格，FTS/trigram 整串匹配
        「清朗网络谣言」经常一篇都搜不到，切成 词组 后 OR 才有召回。"""
        exact = self.db.search(q="清朗网络谣言专项行动部署的通知", limit=10)
        seg = self.db.search(q="清朗网络谣言专项行动部署的通知",
                             tokenize=True, op="OR", limit=50)
        self.assertGreaterEqual(seg["total"], exact["total"])

    def test_or_召回不少于_and(self):
        """多条 WHERE 是 AND，OR 语义会静默失效 —— 必须钉住"""
        q = "网络 水军"
        a = self.db.search(q=q, tokenize=True, op="AND", limit=50)
        o = self.db.search(q=q, tokenize=True, op="OR", limit=50)
        self.assertGreaterEqual(o["total"], a["total"],
                                f"OR={o['total']} < AND={a['total']}")

    def test_like_路径也遵守_or(self):
        """强制走 LIKE（min_fts_chars 拉满）时，OR 不能又变回 AND"""
        q = "网络 水军"
        a = self.db.search(q=q, op="AND", min_fts_chars=99, limit=50)
        o = self.db.search(q=q, tokenize=True, op="OR", min_fts_chars=99,
                           limit=50)
        self.assertEqual(a["mode"], "like")
        self.assertEqual(o["mode"], "like")
        self.assertGreaterEqual(o["total"], a["total"])

    def test_默认行为未变(self):
        """不传 tokenize 时必须与改动前完全一致（AND + 不切词）"""
        r = self.db.search(q="中央网信办 网络安全", limit=5)
        self.assertEqual(r["mode"] in ("fts", "like"), True)
        r2 = self.db.search(q="中央网信办 网络安全", op="AND", limit=5)
        self.assertEqual(r["total"], r2["total"])

    def test_响应带上用了什么模式(self):
        r = self.db.search(q="清朗网络谣言", tokenize=True, op="OR", limit=5)
        self.assertIn("mode", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
