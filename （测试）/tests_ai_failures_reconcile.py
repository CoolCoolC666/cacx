# -*- coding: utf-8 -*-
"""
失败清单实时对账 + 清空 + 人工补录的测试
================================================================
覆盖本轮实测到的真实缺陷：
  · 66 条失败记录里 65 条是僵尸（其实已有摘要，因摘要从外部导入而来，
    绕过了 save_annotation 里的自动清除）
  · 界面按 retryable 历史标记计数，与批量面板口径不一致
  · 不可重试错误一律提示「如 api_key、模型名」，把 422 内容审查也误导成配置问题

铁律：临时库 + TestClient，不起真服务、不碰真实 data/cacx.db。
跑法：py -3 tests_ai_failures_reconcile.py
"""
import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import db as D              # noqa: E402

E429 = "AIError: HTTP 429: rate_limit_error 已达到 Token Plan 速率限制"
E422 = ('AIError: HTTP 422: {"type":"error","error":'
        '{"type":"unprocessable_entity_error","message":"input new_sensitive (1026)"}}')


class Base(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_recon_"))
        cfg = {"db": {"path": str(self.tmp / "t.db")},
               "ai": {"provider": "none"}, "search": {"min_fts_chars": 3}}
        self.d = D.open_db(cfg)
        self.assertIn("cacx_recon_", str(self.d.path))
        self.WA = importlib.import_module("cacx.web.app")
        self.old = self.WA.DB
        self.WA.DB = self.d
        self.c = TestClient(self.WA.app)

        # 4 篇有正文的文章，日期都在 2024 起
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

    def tearDown(self):
        self.WA.DB = self.old
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fail_(self, aid, err, retryable=True):
        self.d.record_ai_failure(aid, "summarize", err, retryable=retryable)

    def ann(self, aid, provider="doubao"):
        self.d.save_annotation(aid, "summarize", {"summary": "人工导入"},
                                provider=provider, model="seed2.1")

    def external_ann(self, aid, provider="doubao", at="2026-10-11 20:39:55"):
        """
        **绕过 save_annotation** 直接插摘要 —— 模拟外部导入（本轮实测场景：
        MiniMax 跑挂留下 65 条，随后从豆包导入 65 条摘要，导入不走
        save_annotation，失败记录全成了僵尸）。
        构造僵尸记录必须用这个，save_annotation 自带清除逻辑。
        """
        self.d.conn.execute(
            "INSERT INTO ai_annotations (article_id,task,provider,model,"
            "result,created_at) VALUES (?,?,?,?,?,?)",
            (aid, "summarize", provider, "seed2.1",
             '{"summary": "外部导入"}', at))
        self.d.conn.commit()


# ==================================================================== 分类
class TestClassify(Base):
    def test_429归限流(self):
        self.assertEqual(D.Database.classify_failure(E429, True), "rate_limit")

    def test_422敏感归内容审查(self):
        """回归：原先 422 落在「不可重试」里，被提示成配置问题。"""
        self.assertEqual(D.Database.classify_failure(E422, False), "content_blocked")

    def test_鉴权类(self):
        self.assertEqual(
            D.Database.classify_failure("HTTP 401: invalid api key", False), "auth")

    def test_超时类(self):
        self.assertEqual(D.Database.classify_failure("read timeout", True), "network")

    def test_未配置(self):
        self.assertEqual(
            D.Database.classify_failure("AI 未配置（cacx config set ...）", False),
            "not_configured")

    def test_未知错误看retryable兜底(self):
        self.assertEqual(D.Database.classify_failure("???", True), "retriable")
        self.assertEqual(D.Database.classify_failure("???", False), "other")

    def test_每个类别都有说明(self):
        for k in ("rate_limit", "content_blocked", "auth", "network",
                  "bad_model", "not_configured", "server", "retriable", "other"):
            self.assertIn(k, D.Database.FAILURE_HINTS)
            self.assertTrue(D.Database.FAILURE_HINTS[k].strip())

    def test_内容审查的说明不许提api_key(self):
        """误导的根源就是这句话，得钉死。"""
        self.assertNotIn("api_key", D.Database.FAILURE_HINTS["content_blocked"])


# ============================================================ 实时对账
class TestReconcile(Base):
    def test_只有真缺摘要的才算待补跑(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E429, True)
        self.ann(self.ids[1])                      # 事后补上了
        rows = self.d.list_ai_failures(task="summarize")
        self.assertEqual([r["article_id"] for r in rows], [self.ids[0]])

    def test_僵尸记录默认不出现(self):
        """本轮实测的 66/65 场景。"""
        for aid in self.ids:
            self.fail_(aid, E429, True)
        for aid in self.ids[:3]:
            self.external_ann(aid)
        rows = self.d.list_ai_failures(task="summarize")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["article_id"], self.ids[3])

    def test_摘要从外部导入也算已补上(self):
        """
        关键回归：不清 ai_failures、直接插 ai_annotations（模拟豆包导入），
        对账后也必须认为它已解决 —— 判据是「有没有摘要」，
        不是「有没有走过清除逻辑」。
        """
        self.fail_(self.ids[0], E429, True)
        self.external_ann(self.ids[0])
        self.assertEqual(self.d.list_ai_failures(task="summarize"), [])
        self.assertEqual(self.d.ai_failure_stats()["pending"], 0)

    def test_include_resolved能看到僵尸(self):
        self.fail_(self.ids[0], E429, True)
        self.external_ann(self.ids[0])
        self.assertEqual(len(self.d.list_ai_failures(task="summarize")), 0)
        self.assertEqual(
            len(self.d.list_ai_failures(task="summarize", only_orphans=False)), 1)

    def test_空正文的失败项不计入(self):
        aid = self.d.conn.execute(
            "INSERT INTO articles (site,column_name,pub_day,pub_date,title,"
            "url,content_text,chars,content_empty,year) "
            "VALUES ('cac','c','2024-02-01','2024-02-01','空','http://e',"
            "'',0,1,'2024')").lastrowid
        self.d.conn.commit()
        self.fail_(aid, E429, True)
        self.assertEqual(self.d.list_ai_failures(task="summarize"), [])

    def test_日期口径与批量面板对齐(self):
        """清单与批量面板必须同一套选区，否则数字和动作对不上。"""
        self.fail_(self.ids[0], E429, True)
        self.assertEqual(len(self.d.list_ai_failures(date_from="2024-01-01")), 1)
        self.assertEqual(len(self.d.list_ai_failures(date_from="2025-01-01")), 0)
        self.assertEqual(
            len(self.d.list_ai_failures(date_from="2024-01-01",
                                        date_to="2024-01-01")), 1)

    def test_返回标题与url便于人工处理(self):
        self.fail_(self.ids[0], E429, True)
        r = self.d.list_ai_failures(task="summarize")[0]
        self.assertEqual(r["title"], "第0篇")
        self.assertEqual(r["url"], "http://a/0")
        self.assertEqual(r["pub_date"], "2024-01-01")


class TestStats(Base):
    def test_四个数字口径(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E429, True)
        self.external_ann(self.ids[1])
        self.fail_(self.ids[2], E422, False)
        s = self.d.ai_failure_stats()
        self.assertEqual(s["total_rows"], 3)     # 表里有 3 行
        self.assertEqual(s["pending"], 2)        # 其中 2 篇仍缺摘要
        self.assertEqual(s["retryable"], 1)      # 429 那 1 篇可重试
        self.assertEqual(s["stale"], 1)          # 1 条已过时（已补摘要）

    def test_按类别分组(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E422, False)
        k = self.d.ai_failure_stats()["kinds"]
        self.assertEqual(k.get("rate_limit"), 1)
        self.assertEqual(k.get("content_blocked"), 1)

    def test_类别只统计待补跑的(self):
        self.fail_(self.ids[0], E422, False)
        self.external_ann(self.ids[0])
        self.assertEqual(self.d.ai_failure_stats()["kinds"], {})


# ================================================================ 清空
class TestClear(Base):
    def test_默认只清已补上摘要的(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E422, False)
        self.external_ann(self.ids[0])
        n = self.d.clear_ai_failures(task="summarize", only_stale=True)
        self.assertEqual(n, 1)
        left = self.d.list_ai_failures(task="summarize", only_orphans=False)
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["article_id"], self.ids[1])

    def test_全清会连真正待补跑的也清掉(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E422, False)
        n = self.d.clear_ai_failures(task="summarize", only_stale=False)
        self.assertEqual(n, 2)
        self.assertEqual(self.d.ai_failure_stats()["pending"], 0)

    def test_清空不动任何摘要(self):
        """清的是记账，不是数据。"""
        self.fail_(self.ids[0], E429, True)
        self.ann(self.ids[0])
        before = self.d.conn.execute(
            "SELECT COUNT(*) FROM ai_annotations").fetchone()[0]
        self.d.clear_ai_failures(task="summarize", only_stale=False)
        after = self.d.conn.execute(
            "SELECT COUNT(*) FROM ai_annotations").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(before, 1)

    def test_按ids精确清(self):
        for aid in self.ids:
            self.fail_(aid, E429, True)
        n = self.d.clear_ai_failures(task="summarize", only_stale=False,
                                     article_ids=[self.ids[0], self.ids[2]])
        self.assertEqual(n, 2)
        left = self.d.list_ai_failures(task="summarize")
        self.assertEqual([r["article_id"] for r in left], [self.ids[1], self.ids[3]])

    def test_清空返回条数不是静默的(self):
        self.fail_(self.ids[0], E429, True)
        self.external_ann(self.ids[0])
        self.assertEqual(self.d.clear_ai_failures(task="summarize"), 1)

    def test_清空无匹配返回0(self):
        self.assertEqual(self.d.clear_ai_failures(task="summarize"), 0)


# ========================================================== 人工补录
class TestManual(Base):
    def test_补录后这篇不再算待补跑(self):
        self.fail_(self.ids[0], E422, False)
        self.assertEqual(self.d.ai_failure_stats()["pending"], 1)
        self.d.manual_annotation(self.ids[0], "summarize",
                                 {"summary": "手写摘要"}, "manual", "human")
        self.assertEqual(self.d.ai_failure_stats()["pending"], 0)
        self.assertEqual(self.d.list_ai_failures(task="summarize"), [])

    def test_补录会被失败留痕清掉(self):
        self.fail_(self.ids[0], E422, False)
        self.d.manual_annotation(self.ids[0], "summarize",
                                 {"summary": "x"}, "manual", "human")
        self.assertEqual(
            self.d.conn.execute("SELECT COUNT(*) FROM ai_failures").fetchone()[0], 0)

    def test_补录标记为manual可与模型区分(self):
        self.d.manual_annotation(self.ids[0], "summarize",
                                 {"summary": "x"}, "manual", "human")
        r = self.d.get_annotation(self.ids[0], "summarize")
        self.assertEqual(r["provider"], "manual")
        self.assertEqual(r["model"], "human")

    def test_重复补录是覆盖不是新增(self):
        for s in ("第一次", "第二次"):
            self.d.manual_annotation(self.ids[0], "summarize",
                                     {"summary": s}, "manual", "human")
        n = self.d.conn.execute(
            "SELECT COUNT(*) FROM ai_annotations").fetchone()[0]
        self.assertEqual(n, 1)
        import json
        r = self.d.get_annotation(self.ids[0], "summarize")
        self.assertEqual(json.loads(r["result"])["summary"], "第二次")


# ==================================================================== API
class TestApi(Base):
    def test_清单接口带类别与说明(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E422, False)
        r = self.c.get("/api/ai/failures").json()
        self.assertEqual(r["count"], 2)
        kinds = {i["kind"] for i in r["items"]}
        self.assertEqual(kinds, {"rate_limit", "content_blocked"})
        for i in r["items"]:
            self.assertTrue(i["hint"].strip())

    def test_清单接口默认不含僵尸(self):
        self.fail_(self.ids[0], E429, True)
        self.external_ann(self.ids[0])
        self.assertEqual(self.c.get("/api/ai/failures").json()["count"], 0)
        self.assertEqual(
            self.c.get("/api/ai/failures?include_resolved=true").json()["count"], 1)

    def test_清单接口带stats(self):
        self.fail_(self.ids[0], E429, True)
        self.external_ann(self.ids[0])
        s = self.c.get("/api/ai/failures").json()["stats"]
        self.assertEqual(s["stale"], 1)
        self.assertEqual(s["pending"], 0)

    def test_清空接口默认档(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E422, False)
        self.external_ann(self.ids[0])
        r = self.c.post("/api/ai/failures/clear", json={}).json()
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(r["scope"], "resolved_only")
        self.assertEqual(self.c.get("/api/ai/failures").json()["count"], 1)

    def test_清空接口全清需显式all(self):
        self.fail_(self.ids[0], E429, True)
        self.fail_(self.ids[1], E422, False)
        r = self.c.post("/api/ai/failures/clear", json={"all": True}).json()
        self.assertEqual(r["deleted"], 2)
        self.assertEqual(r["scope"], "all")
        self.assertEqual(self.c.get("/api/ai/failures").json()["count"], 0)

    def test_清空接口不动摘要(self):
        self.fail_(self.ids[0], E429, True)
        self.ann(self.ids[0])
        before = self.c.get("/api/ai/failures?include_resolved=true").json()["count"]
        self.c.post("/api/ai/failures/clear", json={})
        # 摘要还在，所以 solved 关系不变
        self.assertEqual(
            self.d.conn.execute("SELECT COUNT(*) FROM ai_annotations").fetchone()[0], 1)

    def test_人工补录接口(self):
        self.fail_(self.ids[0], E422, False)
        r = self.c.post("/api/ai/annotations/manual",
                        json={"article_id": self.ids[0],
                              "summary": "手动补的一条摘要",
                              "tags": ["领导人"]}).json()
        self.assertTrue(r["saved"])
        self.assertEqual(self.c.get("/api/ai/failures").json()["count"], 0)

    def test_人工补录拒绝空摘要(self):
        st = self.c.post("/api/ai/annotations/manual",
                         json={"article_id": self.ids[0], "summary": "  "}).status_code
        self.assertEqual(st, 400)

    def test_人工补录拒绝不存在的文章(self):
        st = self.c.post("/api/ai/annotations/manual",
                         json={"article_id": 999999, "summary": "x"}).status_code
        self.assertEqual(st, 404)

    def test_人工补录不调模型(self):
        """
        绝不能因为「补录」偷偷去请求供应商。

        钩子挂在 cacx.ai.tasks.build 上：它是所有模型调用的唯一出口，
        拦在这里就等于拦住了全部出网路径。比挂某个具体函数可靠 ——
        app.py 里的 ai_summarize 是模块名，将来重构改法就失效了。
        """
        from cacx.ai import tasks as T
        calls = []
        orig = T.build
        T.build = lambda *a, **k: calls.append(1)
        try:
            r = self.c.post("/api/ai/annotations/manual",
                            json={"article_id": self.ids[0], "summary": "手写"})
            self.assertEqual(r.status_code, 200)
        finally:
            T.build = orig
        self.assertEqual(calls, [], "人工补录路径里出现了模型调用")


if __name__ == "__main__":
    unittest.main(verbosity=2)

