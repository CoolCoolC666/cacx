# -*- coding: utf-8 -*-
"""
AI 中断恢复的 Web 接口测试
================================================================
覆盖「跨重启可见」与「只重试失败项」这两条主线。

铁律：临时库 + TestClient，不起真服务、不碰真实 data/cacx.db。
跑法：py -3 tests_ai_resume_api.py
"""
import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import db as D              # noqa: E402
from cacx.ai.base import AIError       # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_aiapi_"))
        cfg = {"db": {"path": str(self.tmp / "t.db")},
               "ai": {"provider": "none"}, "search": {"min_fts_chars": 3}}
        self.d = D.open_db(cfg)
        self.assertIn("cacx_aiapi_", str(self.d.path))
        self.ids = []
        for i in range(4):
            aid = self.d.conn.execute(
                "INSERT INTO articles (site, column_name, pub_day, pub_date,"
                " title, url, content_text, chars, content_empty, year) "
                "VALUES ('cac','c',?,?,?,?,?,200,0,'2024')",
                (f"2024-01-0{i+1}", f"2024-01-0{i+1}", f"第{i}篇",
                 f"http://a/{i}", "正文" * 100)).lastrowid
            self.ids.append(aid)
        self.d.conn.commit()

        WA = importlib.import_module("cacx.web.app")
        self.WA = WA
        self.old = WA.DB
        WA.DB = self.d
        self.c = TestClient(WA.app)

    def tearDown(self):
        self.WA.DB = self.old
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def n(self, sql, *a):
        return self.d.conn.execute(sql, a).fetchone()[0]


class TestFailuresApi(Base):
    def test_empty_initially(self):
        r = self.c.get("/api/ai/failures")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_lists_recorded_failures(self):
        self.d.record_ai_failure(self.ids[0], "summarize", "HTTP 429",
                                 retryable=True)
        self.d.record_ai_failure(self.ids[1], "summarize", "HTTP 401",
                                 retryable=False)
        r = self.c.get("/api/ai/failures").json()
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["retryable"], 1)

    def test_retryable_only_filter(self):
        self.d.record_ai_failure(self.ids[0], "summarize", "429", retryable=True)
        self.d.record_ai_failure(self.ids[1], "summarize", "401", retryable=False)
        r = self.c.get("/api/ai/failures?retryable_only=true").json()
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["article_id"], self.ids[0])

    def test_includes_title_for_display(self):
        """前端要显示标题，光给 id 用户看不出是哪篇"""
        self.d.record_ai_failure(self.ids[0], "summarize", "boom")
        it = self.c.get("/api/ai/failures").json()["items"][0]
        self.assertEqual(it["title"], "第0篇")
        self.assertEqual(it["site"], "cac")

    def test_retry_endpoint_returns_ids(self):
        self.d.record_ai_failure(self.ids[0], "summarize", "429", retryable=True)
        self.d.record_ai_failure(self.ids[1], "summarize", "401", retryable=False)
        r = self.c.post("/api/ai/retry-failed",
                        json={"dry_run": True, "retryable_only": True})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["selected"], 1)
        self.assertEqual(j["article_ids"], [self.ids[0]])

    def test_retry_endpoint_skips_fatal(self):
        """鉴权类失败不该被塞进重试清单 —— 重试一万次还是 401。

        selected=0 时接口按设计抛 400（没有可重试项就别假装成功），
        所以这里验证底层筛选：失败清单里确实只有 retryable 的那一条。
        """
        self.d.record_ai_failure(self.ids[0], "summarize", "429",
                                 retryable=True)
        self.d.record_ai_failure(self.ids[1], "summarize", "401",
                                 retryable=False)
        rows = self.d.list_ai_failures(task="summarize", retryable_only=True)
        self.assertEqual([r["article_id"] for r in rows], [self.ids[0]])
        # 没有可重试项时接口明确拒绝，而不是回一个 selected=0 的"成功"
        self.d.clear_ai_failure(self.ids[0], "summarize")
        r = self.c.post("/api/ai/retry-failed", json={"retryable_only": True})
        self.assertEqual(r.status_code, 400)

    def test_retry_endpoint_refuses_when_empty(self):
        r = self.c.post("/api/ai/retry-failed", json={"retryable_only": True})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["detail"].get("refused"))

    def test_success_clears_from_api(self):
        """摘要写成功后，失败清单里不该再有它"""
        self.d.record_ai_failure(self.ids[0], "summarize", "boom")
        self.d.save_annotation(self.ids[0], "summarize", {"s": "ok"})
        self.assertEqual(self.c.get("/api/ai/failures").json()["count"], 0)


class TestSelectByIds(Base):
    """「只重试失败项」靠 ids 参数接上，必须验证它真的只选这些"""

    def _select(self, **kw):
        base = dict(sites=["cac"], column="", date_from="", date_to="",
                    mode="incremental", limit=10, concurrency=1, rate=0.0,
                    resume=False, stop_on_error=False, dry_run=True,
                    ids=None)
        base.update(kw)
        return self.WA._select_batch_articles(
            self.d, {"search": {"min_fts_chars": 3}}, self.WA.BatchReq(**base))

    def test_ids_selects_only_those(self):
        _, arts = self._select(ids=[self.ids[0], self.ids[2]])
        self.assertEqual([a["id"] for a in arts],
                         [self.ids[0], self.ids[2]])

    def test_ids_ignores_limit(self):
        """limit=1 也应跑完点名的那几篇 —— 否则「只重试失败项」
        会被 limit 截断成只跑 1 篇，用户以为重试了其实没轮到。"""
        _, arts = self._select(ids=self.ids, limit=1)
        self.assertEqual(len(arts), 4)

    def test_ids_respects_incremental(self):
        self.d.save_annotation(self.ids[0], "summarize", {"s": "ok"})
        _, arts = self._select(ids=self.ids)
        self.assertNotIn(self.ids[0], [a["id"] for a in arts])

    def test_unknown_ids_dropped(self):
        _, arts = self._select(ids=[self.ids[0], 999999])
        self.assertEqual([a["id"] for a in arts], [self.ids[0]])

    def test_ids_takes_priority_over_sites(self):
        _, arts = self._select(ids=[self.ids[1]], sites=["cac"])
        self.assertEqual([a["id"] for a in arts], [self.ids[1]])


if __name__ == "__main__":
    unittest.main(verbosity=2)