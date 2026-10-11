"""AI 中途中断处理：冒烟 + 单元。只跑，不联网。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import db as D                      # noqa: E402
from cacx.ai import batch as B                 # noqa: E402
from cacx.ai.base import AIError, classify_status  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_ai_"))
        self.d = D.open_db({"db": {"path": str(self.tmp / "t.db")}})
        self.assertIn("cacx_ai_", str(self.d.path))
        self.cfg = {"db": {"path": str(self.tmp / "t.db")},
                    "ai": {"provider": "openai_compat",
                           "base_url": "https://example.invalid/v1",
                           "model": "m", "api_key": "sk-TEST",
                           "retry_attempts": 3, "retry_base_delay": 0.01},
                    "search": {"min_fts_chars": 3}}
        self.arts = []
        for i in range(5):
            aid = self.d.conn.execute(
                "INSERT INTO articles (site, column_name, pub_day, pub_date, "
                "title, url, content_text, chars, content_empty, year) "
                "VALUES ('cac','c',?,?,?,?,?,200,0,'2024')",
                (f"2024-01-0{i+1}", f"2024-01-0{i+1}", f"第{i}篇",
                 f"http://a/{i}", "正文" * 100)).lastrowid
            self.arts.append({"id": aid, "title": f"第{i}篇",
                              "url": f"http://a/{i}", "content_text": "正文" * 100,
                              "pub_day": f"2024-01-0{i+1}"})
        self.d.conn.commit()

    def tearDown(self):
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def n(self, sql, *a):
        return self.d.conn.execute(sql, a).fetchone()[0]


class TestClassify(Base):
    def test_retryable_codes(self):
        for c in (408, 429, 500, 502, 503, 504):
            self.assertTrue(classify_status(c), f"{c} 应可重试")

    def test_fatal_codes(self):
        for c in (400, 401, 403, 404, 422):
            self.assertFalse(classify_status(c), f"{c} 不应重试")

    def test_unknown_code_not_retried(self):
        """未知码宁可不重试 —— 对必然失败的请求反复敲门会触发风控"""
        self.assertFalse(classify_status(418))
        self.assertFalse(classify_status(451))

    def test_aierror_carries_retryable(self):
        self.assertTrue(AIError("x", retryable=True).retryable)
        self.assertFalse(AIError("x").retryable)
        self.assertEqual(AIError("x", status_code=429).status_code, 429)


class TestRetry(Base):
    def test_retries_then_succeeds(self):
        import requests
        from cacx.ai import base as AB
        ok = mock.Mock(status_code=200)
        seq = [requests.Timeout("t"), requests.ConnectionError("c"), ok]
        with mock.patch.object(AB.requests, "post",
                               side_effect=seq) as p:
            r = AB.post_with_retry("http://x", timeout=1, attempts=3,
                                   base_delay=0.01)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(p.call_count, 3)

    def test_no_retry_on_fatal_status(self):
        from cacx.ai import base as AB
        bad = mock.Mock(status_code=401, text="unauthorized", headers={})
        with mock.patch.object(AB.requests, "post", return_value=bad) as p:
            with self.assertRaises(AIError) as cm:
                AB.post_with_retry("http://x", timeout=1, attempts=5,
                                   base_delay=0.01)
        self.assertEqual(p.call_count, 1, "鉴权失败不应重试")
        self.assertFalse(cm.exception.retryable)

    def test_retries_on_429(self):
        from cacx.ai import base as AB
        r429 = mock.Mock(status_code=429, text="slow down",
                         headers={"Retry-After": "0"})
        ok = mock.Mock(status_code=200, headers={})
        with mock.patch.object(AB.requests, "post",
                               side_effect=[r429, ok]) as p:
            AB.post_with_retry("http://x", timeout=1, attempts=3,
                               base_delay=0.01)
        self.assertEqual(p.call_count, 2)

    def test_respects_retry_after(self):
        from cacx.ai import base as AB
        self.assertEqual(AB._retry_after_seconds({"Retry-After": "3"}, 1.0), 3.0)
        self.assertEqual(AB._retry_after_seconds({}, 1.0), 1.0)
        self.assertEqual(AB._retry_after_seconds({"Retry-After": "999"}, 1.0),
                         120.0, "不能被服务端的无理值卡死")
        self.assertEqual(AB._retry_after_seconds({"Retry-After": "abc"}, 1.0),
                         1.0)

    def test_attempts_1_disables_retry(self):
        """默认 attempts=1 → 沿用旧行为，不重试（防止无意识多烧配额）"""
        from cacx.ai import base as AB
        bad = mock.Mock(status_code=503, text="busy", headers={})
        with mock.patch.object(AB.requests, "post", return_value=bad) as p:
            with self.assertRaises(AIError):
                AB.post_with_retry("http://x", timeout=1, attempts=1,
                                   base_delay=0.01)
        self.assertEqual(p.call_count, 1)


class TestBatchFailureHandling(Base):
    def _run(self, side_effect, **kw):
        # ⚠ side_effect 传**异常实例**（不是单元素列表）：
        #   列表里的元素用完就 StopIteration，于是 5 篇只跑了 1 篇，
        #   断言「5 篇全失败」自然对不上。传实例则每次调用都抛同一个。
        with mock.patch.object(B.ai_tasks, "summarize",
                               side_effect=side_effect):
            return B.run_batch(self.cfg, self.d, self.arts,
                               dry_run=False, **kw)

    def test_success_persists_per_item(self):
        """每成功一篇立即落库 —— 中途 kill 也不丢已完成"""
        s = self._run([{"summary": "ok"}] * 5)
        self.assertEqual(s["ok"], 5)
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_annotations"), 5)

    def test_retryable_failure_recorded(self):
        s = self._run(AIError("HTTP 429: 限流", retryable=True,
                              status_code=429))
        self.assertEqual(s["fail"], 5)
        self.assertEqual(s["retriable"], 5)
        self.assertTrue(all(f["retryable"] for f in s["failures"]))
        self.assertEqual(
            self.n("SELECT COUNT(*) FROM ai_failures WHERE retryable=1"), 5)

    def test_fatal_failure_marked_not_retryable(self):
        s = self._run(AIError("HTTP 401: key 错", status_code=401))
        self.assertEqual(s["retriable"], 0)
        self.assertFalse(s["failures"][0]["retryable"])
        self.assertIn("api_key", s["failures"][0]["hint"])

    def test_failure_never_writes_annotations(self):
        """⚠ 核心不变式：失败绝不进 ai_annotations。
        否则 get_annotation 非空 → resume 永久跳过 →
        一次瞬时限流失败被固化成永久缺口。"""
        self._run([AIError("HTTP 503", retryable=True, status_code=503)])
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_annotations"), 0)

    def test_resume_skips_success_not_failure(self):
        # 先成功两篇
        with mock.patch.object(B.ai_tasks, "summarize",
                               side_effect=[{"s": 1}, {"s": 2}]):
            B.run_batch(self.cfg, self.d, self.arts[:2], dry_run=False)
        # 再全量跑一次带 resume
        s = self._run([{"s": 3}] * 5, resume=True)
        self.assertEqual(s["skipped"], 2, "已成功的应被跳过")
        self.assertEqual(s["ok"], 3)

    def test_success_clears_stale_failure(self):
        """先造一条失败留痕，再成功 → 留痕必须消失，
        否则界面会一直显示这篇「还没修好」。"""
        self.d.record_ai_failure(self.arts[0]["id"], B.TASK, "boom",
                                 retryable=True)
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_failures"), 1)
        with mock.patch.object(B.ai_tasks, "summarize",
                               return_value={"s": "ok"}):
            B.run_batch(self.cfg, self.d, self.arts[:1], dry_run=False)
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_failures"), 0,
                         "成功后未清除失败留痕")

    def test_stop_on_error_reports_not_run(self):
        """被中断时必须说清「还剩多少没跑」，
        否则「失败 1」会被误读成「跑完了」。"""
        s = self._run([AIError("boom"), AIError("boom"), AIError("boom")],
                      stop_on_error=True, concurrency=1)
        self.assertTrue(s["interrupted"])
        self.assertGreater(s["not_run"], 0)
        self.assertEqual(s["ok"] + s["fail"] + s["cancelled"] +
                         s["not_run"], len(self.arts))

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(B.ai_tasks, "summarize",
                               return_value={"_dry_run": True}) as m:
            s = B.run_batch(self.cfg, self.d, self.arts, dry_run=True)
        self.assertEqual(m.call_count, 1, "dry_run 只应调一次")
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_annotations"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)