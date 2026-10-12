# -*- coding: utf-8 -*-
"""实体 / 关系表（MCP 写入能力的落地）"""
import shutil
import tempfile
import unittest
from pathlib import Path

from cacx.db import Database


def _mk(n=3):
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    db = Database(str(tmp))
    db.conn.execute("""CREATE TABLE IF NOT EXISTS articles(
      id INTEGER PRIMARY KEY, site TEXT, site_name TEXT, column_name TEXT,
      column_code TEXT, title TEXT, pub_date TEXT, pub_day TEXT, url TEXT,
      source_text TEXT, chars INTEGER, content_text TEXT, year TEXT,
      content_empty INTEGER DEFAULT 0, first_seen TEXT)""")
    titles = ["中央网信办 公安部 联合部署清朗行动",
              "中央网信办 公安部 联合发布通报",
              "国家网信办 部署算法推荐治理"]
    for i in range(1, n + 1):
        t = titles[i - 1]
        db.conn.execute(
            "INSERT INTO articles(id,site,site_name,column_name,column_code,"
            "title,pub_date,pub_day,url,source_text,chars,content_text,year)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "cac", "中国网信网", "网信要闻", "xx", t, "2025-01-01",
             "2025-01-01", "http://u%d" % i, "新华社", 100, t, "2025"))
    db.conn.commit()
    return db, tmp.parent


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _mk()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_两表都建好了(self):
        for t in ("entities", "relations"):
            r = self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            self.assertIsNotNone(r, f"{t} 没建出来")

    def test_实体四种类型(self):
        self.assertEqual(self.db.ENTITY_TYPES,
                         ("org", "policy", "problem", "measure"))


class TestWriteEntities(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _mk()
        self.tid = len(self.db.ENTITY_TYPES)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_正常写入(self):
        r = self.db.upsert_entities([
            {"name": "中央网信办", "type": "org", "article_id": 1},
            {"name": "清朗行动", "type": "measure", "article_id": 1},
            {"name": "个人信息保护法", "type": "policy", "article_id": 3},
        ])
        self.assertEqual(r["written"], 3)
        self.assertEqual(r["skipped"], 0)
        self.assertEqual(len(self.db.list_entities()), 3)

    def test_重复提交幂等(self):
        """AI 重跑一遍不能把行数撑成双倍"""
        row = {"name": "中央网信办", "type": "org", "article_id": 1}
        self.db.upsert_entities([row])
        self.db.upsert_entities([row])
        self.assertEqual(len(self.db.list_entities()), 1)

    def test_重复提交会更新证据(self):
        self.db.upsert_entities([
            {"name": "中央网信办", "type": "org", "article_id": 1,
             "evidence": "旧证据"}])
        self.db.upsert_entities([
            {"name": "中央网信办", "type": "org", "article_id": 1,
             "evidence": "新证据"}])
        got = self.db.list_entities()[0]["evidence"]
        self.assertEqual(got, "新证据")

    def test_同实体不同文章各留一行(self):
        """同一个机构在三篇里出现 → 三条记录，才能统计共现强度"""
        for i in (1, 2, 3):
            self.db.upsert_entities([
                {"name": "中央网信办", "type": "org", "article_id": i}])
        self.assertEqual(len(self.db.list_entities()), 3)

    def test_未知类型被拒且给出可选值(self):
        r = self.db.upsert_entities([
            {"name": "某个人", "type": "person", "article_id": 1}])
        self.assertEqual(r["written"], 0)
        self.assertEqual(r["skipped"], 1)
        self.assertIn("org", r["errors"][0])

    def test_文章不存在被拒(self):
        r = self.db.upsert_entities([
            {"name": "某机构", "type": "org", "article_id": 999}])
        self.assertEqual(r["written"], 0)
        self.assertIn("999", r["errors"][0])

    def test_缺字段被拒(self):
        r = self.db.upsert_entities([{"type": "org", "article_id": 1}])
        self.assertEqual(r["written"], 0)

    def test_部分失败不影响其他行(self):
        r = self.db.upsert_entities([
            {"name": "好机构", "type": "org", "article_id": 1},
            {"name": "坏机构", "type": "person", "article_id": 1},
            {"name": "好机构2", "type": "org", "article_id": 2},
        ])
        self.assertEqual(r["written"], 2, "一行坏的不该拖垮整批")
        self.assertEqual(r["skipped"], 1)

    def test_别名可存可读(self):
        self.db.upsert_entities([
            {"name": "国家市场监督管理总局", "type": "org", "article_id": 1,
             "aliases": ["市场监管总局"]}])
        import json
        got = json.loads(self.db.list_entities()[0]["aliases"])
        self.assertIn("市场监管总局", got)

    def test_带原文标题可回溯(self):
        """答辩要「点实体回溯原文」，列表里必须带标题和 url"""
        self.db.upsert_entities([
            {"name": "中央网信办", "type": "org", "article_id": 1}])
        e = self.db.list_entities()[0]
        self.assertTrue(e["article_title"])
        self.assertTrue(e["url"].startswith("http"))


class TestWriteRelations(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _mk()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_正常写入(self):
        r = self.db.upsert_relations([
            {"src_name": "中央网信办", "dst_name": "公安部",
             "rel_type": "co_issue", "article_id": 1}])
        self.assertEqual(r["written"], 1)

    def test_自环被拒(self):
        r = self.db.upsert_relations([
            {"src_name": "网信办", "dst_name": "网信办",
             "rel_type": "co_issue", "article_id": 1}])
        self.assertEqual(r["written"], 0)
        self.assertIn("自环", r["errors"][0])

    def test_跨文章共现权重累加(self):
        for i in (1, 2, 3):
            self.db.upsert_relations([
                {"src_name": "中央网信办", "dst_name": "公安部",
                 "rel_type": "co_issue", "article_id": i}])
        g = self.db.graph()
        self.assertEqual(len(g["edges"]), 1, "应聚合成一条边")
        self.assertEqual(g["edges"][0]["n"], 3)
        self.assertEqual(g["edges"][0]["weight"], 3.0)

    def test_同一篇重复提交权重累加但行数不增(self):
        rel = {"src_name": "中央网信办", "dst_name": "公安部",
               "rel_type": "co_issue", "article_id": 1}
        self.db.upsert_relations([rel])
        self.db.upsert_relations([rel])
        self.assertEqual(len(self.db.list_relations()), 1)
        self.assertEqual(self.db.graph()["edges"][0]["weight"], 2.0)

    def test_证据文章不被压成一条(self):
        """⚠ 关键：同一对机构在 3 篇里联合发文，3 条证据必须都留着。
        若对 (src,dst,type) 建唯一约束，最该保留的共现强度反而被抹掉。"""
        for i in (1, 2, 3):
            self.db.upsert_relations([
                {"src_name": "甲", "dst_name": "乙",
                 "rel_type": "co_issue", "article_id": i,
                 "evidence": f"证据{i}"}])
        self.assertEqual(len(self.db.list_relations()), 3)
        self.assertEqual(len(self.db.graph()["edges"][0]["article_ids"]), 3)

    def test_不同关系类型互不覆盖(self):
        self.db.upsert_relations([
            {"src_name": "甲", "dst_name": "乙", "rel_type": "co_issue",
             "article_id": 1}])
        self.db.upsert_relations([
            {"src_name": "甲", "dst_name": "乙", "rel_type": "regulate",
             "article_id": 1}])
        self.assertEqual(len(self.db.list_relations()), 2)

    def test_缺字段被拒(self):
        r = self.db.upsert_relations([
            {"src_name": "甲", "article_id": 1}])
        self.assertEqual(r["written"], 0)

    def test_按实体查关系包含入边出边(self):
        self.db.upsert_relations([
            {"src_name": "甲", "dst_name": "乙", "rel_type": "co_issue",
             "article_id": 1},
            {"src_name": "丙", "dst_name": "甲", "rel_type": "co_issue",
             "article_id": 1}])
        got = self.db.list_relations(name="甲")
        self.assertEqual(len(got), 2, got)


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _mk()
        self.db.upsert_entities([
            {"name": "中央网信办", "type": "org", "article_id": 1},
            {"name": "公安部", "type": "org", "article_id": 1},
            {"name": "清朗行动", "type": "measure", "article_id": 1},
        ])
        for i in (1, 2):
            self.db.upsert_relations([
                {"src_name": "中央网信办", "dst_name": "公安部",
                 "rel_type": "co_issue", "article_id": i,
                 "evidence": "联合发文"}])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_节点边都在(self):
        g = self.db.graph()
        self.assertEqual(len(g["nodes"]), 3)
        self.assertEqual(len(g["edges"]), 1)

    def test_节点按去重后的名字计数(self):
        self.db.upsert_entities([
            {"name": "中央网信办", "type": "org", "article_id": 3}])
        names = [n["name"] for n in self.db.graph()["nodes"]]
        self.assertEqual(names.count("中央网信办"), 1, names)

    def test_概览数字(self):
        s = self.db.graph_stats()
        # setUp 写了 3 条：网信办@1、公安部@1、清朗行动@1
        self.assertEqual(s["entity_total"], 3)
        # 但「中央网信办」跨 2 篇各留一行，所以去重后的机构是 2 个
        self.assertEqual(s["entity_orgs"], 2)
        self.assertEqual(s["relation_total"], 2)   # 两篇文章各一条证据
        self.assertEqual(s["rel_pairs"], 1)        # 但聚合成一条边
        self.assertEqual(s["no_evidence"], 0)

    def test_无证据关系可被统计出来(self):
        """答辩时无证据的关系是硬伤，必须能一眼看出有多少"""
        self.db.upsert_relations([
            {"src_name": "甲", "dst_name": "乙", "rel_type": "co_issue",
             "article_id": 1}])
        self.assertEqual(self.db.graph_stats()["no_evidence"], 1)

    def test_按类型过滤实体(self):
        orgs = self.db.list_entities(type="org")
        self.assertEqual(len(orgs), 2)
        self.assertTrue(all(e["type"] == "org" for e in orgs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
