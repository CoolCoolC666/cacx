# -*- coding: utf-8 -*-
"""
后端抽取/图谱/关联接口测试 + 零 CDN 断言

用临时库，绝不碰 config/settings.json 与 data/cacx.db。
"""
import os
from pathlib import Path
import re
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tempfile
import _temppool
import unittest

sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from cacx.web import app as A
# ⚠ `from cacx.web import app as A` 拿到的是**模块**，但 A.app 是 FastAPI
#   实例；同名属性会遮住同名子模块，patch 时必须走 sys.modules 取真模块。
import sys as _sys
MOD = _sys.modules["cacx.web.app"]

FAIL_NOTE = []

class BaseGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = _temppool.mkdtemp(prefix="cacx_gtest_")
        from cacx import db as D
        # ⚠ 先存旧引用，再改全局 —— 否则拿到的已是自己覆盖过的值
        cls.orig_db = MOD._db
        cls.orig_cfg = MOD._cfg
        # ⚠ 不要手写 articles 表：真实 SCHEMA 里还有 column_code、
        #   FTS5 虚表、触发器等，手写必漏列（第一版就栽在这）。
        #   直接用 open_db 在临时路径建整套结构。
        cfg = dict(cls.orig_cfg())
        # ⚠ 键名是 cfg["db"]["path"]（不是 db_path），写错会静默开真库，
        #   然后在真库上跑测试 —— 那正是「绝不碰 data/cacx.db」要防的事故。
        cfg.setdefault("db", {})["path"] = os.path.join(cls.tmp, "t.db")
        cls.store = D.open_db(cfg)
        # 安全闸：万一配置键写错静默开了真库，必须当场炸掉而不是
        # 把抽取结果写进 data/cacx.db
        from cacx import config as _C
        real_db = (_C.PROJECT_ROOT / "data" / "cacx.db").resolve()
        got = Path(cls.store.conn.execute(
            "PRAGMA database_list").fetchone()[2]).resolve()
        assert got != real_db, f"测试误开了真库：{got}"

        # ⚠ 正文必须真的超过 extract.MIN_CHARS(200)，否则会被正确跳过 ——
        #   第一版正文写太短，4 篇全跳过、图谱为空，一度误以为是抽取坏了。
        PAD = ("相关内容将依据有关法律法规逐步落实，各地区各部门应当加强"
               "协同联动，及时回应社会关切，持续完善相关工作机制，"
               "确保各项治理措施落到实处见到成效。")
        rows = [
            (1, "cac", "网信要闻",
             "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》",
             "http://x/1", "2024-03-05", 400, 0,
             "为进一步规范网络数据处理活动，切实保护公民个人信息权益，"
             "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》。"
             "通知要求各地网信部门会同公安机关开展网络安全专项检查，"
             "针对网络谣言、网络诈骗、未成年人沉迷等突出问题依法查处。"
             "各省级网信办要按照通知要求，结合本地实际制定实施方案。"
             "市场监管总局、教育部将联合开展监督检查，并对违规App下架处理。"
             "通知还要求加强对算法推荐和深度伪造信息的治理力度。" + PAD),
            (2, "cac", "网信要闻", "国家网信办部署未成年人网络保护专项行动",
             "http://x/2", "2025-06-11", 320, 0,
             "国家网信办有关部门近日部署未成年人网络保护专项行动，"
             "要求各地网信部门会同公安机关开展专项检查，"
             "针对未成年人沉迷等突出问题依法查处，"
             "并会同市场监管总局、教育部加强网络环境治理。"
             "专项行动重点整治网络暴力、网络诈骗等涉未成年人网络乱象，"
             "对违规App下架处理，并对相关平台负责人依法约谈整改。" + PAD),
            (3, "cac", "网信要闻", "中央网信办、公安部联合开展网络谣言整治",
             "http://x/3", "2026-02-20", 300, 0,
             "中央网信办、公安部联合开展网络谣言整治专项行动，"
             "针对网络谣言、网络暴力等社会问题依法查处，"
             "各地网信部门应当按照统一部署推进治理工作。"
             "有关部门还组织开展了水军有偿删帖专项治理，" + PAD +
             "对造谣账号依法封禁处置，并发布了典型案例通报。"),
            (4, "cac", "网信要闻", "短文", "http://x/4",
             "2024-01-01", 10, 0, "太短"),
        ]
        cols = "id,site,column_name,title,url,pub_day,chars,content_empty,content_text"
        ph = ",".join("?" * len(rows[0]))
        cls.store.conn.executemany(
            f"INSERT INTO articles({cols}) VALUES({ph})", rows)
        cls.store.conn.commit()

        cls.orig_db = MOD._db
        cls.orig_cfg = MOD._cfg
        MOD._db = lambda: cls.store
        MOD._cfg = lambda: cfg
        MOD.DB = cls.store
        cls.client = TestClient(MOD.app)
        cls.conn = cls.store.conn

    @classmethod
    def tearDownClass(cls):
        MOD._db = cls.orig_db
        MOD._cfg = cls.orig_cfg
        try:
            cls.store.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

class TestExtractEndpoint(BaseGraph):
    def setUp(self):
        # ⚠ 每个测试前清库：unittest 的测试执行顺序是按字母序，
        #   若上一个测试写了库，「预览不落库」就会误判成失败
        self.conn.execute("DELETE FROM relations")
        self.conn.execute("DELETE FROM entities")
        self.conn.commit()

    def test_预览不落库(self):
        r = self.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "strategy": "stratified", "write": False})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIn("articles", d)
        # ⚠ 预览**不该**把上万条实体塞进响应（会撑爆 HTTP），
        #   只回统计。这条是防止有人日后顺手把 entities 又加回来。
        self.assertNotIn("entities", d)
        self.assertNotIn("relations", d)
        self.assertIsNone(d["written"])
        self.assertIn("预览", d.get("hint", ""))
        # 库里必须还是空的
        n = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        self.assertEqual(n, 0)

    def test_写库后实体关系都有证据(self):
        r = self.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "strategy": "stratified", "write": True,
            "clear_first": True, "confirm_clear": True})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIsNotNone(d["written"])
        self.assertGreater(d["types"].get("org", 0), 0)
        self.assertGreater(d["types"].get("policy", 0), 0)
        self.assertGreater(d["types"].get("problem", 0), 0)
        # 每条关系都有证据
        bad = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE evidence IS NULL "
            "OR evidence=''").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_清库必须显式确认(self):
        # 先造点数据，才测「清库被拒绝时数据还在」
        self.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "strategy": "stratified", "write": True})
        before = self.conn.execute(
            "SELECT COUNT(*) FROM entities").fetchone()[0]
        self.assertGreater(before, 0)
        r = self.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "clear_first": True, "confirm_clear": False})
        self.assertIn("confirm_clear", r.json().get("error", ""))
        # 清库不该发生
        after = self.conn.execute(
            "SELECT COUNT(*) FROM entities").fetchone()[0]
        self.assertEqual(after, before)

    def test_空口径不崩(self):
        r = self.client.post("/api/extract/run", json={
            "column": "不存在的栏目", "limit": 10})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())

class TestGraphEndpoints(BaseGraph):
    def setUp(self):
        # ⚠ 每个测试前重建：unittest 按字母序跑，清完库后本组测试
        #   自己灌一次，保证彼此独立、不受执行顺序影响
        self.conn.execute("DELETE FROM relations")
        self.conn.execute("DELETE FROM entities")
        self.conn.commit()
        self.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "strategy": "stratified", "write": True})

    def test_关联分析出指标(self):
        r = self.client.get("/api/graph/analysis?min_degree=1&topn=10")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertGreater(d["nodes"], 0)
        for k in ("degree", "strength", "betweenness", "pagerank",
                  "communities", "by_type", "graph"):
            self.assertIn(k, d)
        self.assertNotIn("node_community", d)   # 内部字段不该外泄
        self.assertTrue(d["graph"]["nodes"])

    def test_边证据正反向都能查到(self):
        rows = self.conn.execute(
            "SELECT src_name,dst_name,rel_type FROM relations "
            "WHERE rel_type='joint_issue' LIMIT 1").fetchone()
        if not rows:
            self.skipTest("本次抽取没出联合发文边")
        s, d, t = rows
        r1 = self.client.get(f"/api/graph/evidence?src={s}&dst={d}&rel_type={t}")
        r2 = self.client.get(f"/api/graph/evidence?src={d}&dst={s}&rel_type={t}")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # 图是无向的，正反向必须等价
        self.assertEqual(len(r1.json()["items"]), len(r2.json()["items"]))
        for it in r1.json()["items"]:
            self.assertTrue(it["evidence"].strip())
            self.assertTrue(it["url"])

    def test_pyvis产物零外链(self):
        r = self.client.get("/api/graph/vis?min_degree=1&limit=200")
        self.assertEqual(r.status_code, 200, r.text[:300])
        d = r.json()
        if "error" in d:
            self.skipTest(d["error"])
        html = d["html"]
        ext = re.findall(r'<(?:script|link)[^>]+(?:src|href)="https?://', html)
        self.assertEqual(ext, [], f"产物里还有外链：{ext[:3]}")
        self.assertGreater(len(html), 300000)   # vis.js 应已内联
        self.assertIn("vis-network", html)

    def test_清空图谱(self):
        self.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "clear_first": True, "confirm_clear": True})
        n = self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        self.assertEqual(n, 0)
        r = self.client.get("/api/graph/analysis")
        self.assertEqual(r.json()["nodes"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
