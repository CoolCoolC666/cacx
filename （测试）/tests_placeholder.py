# -*- coding: utf-8 -*-
"""
占位条目（正文未入库）测试
================================================================
同样铁律：临时库，绝不碰 data/cacx.db。
这不只是谨慎 —— 本轮真的发生了：tests_purge 的临时库配置键写错
（paths.db 而不是 db.path），open_db 静默回落到真实库，
6 篇夹具文章 + 1 条测试摘要进了生产语料。
「测试污染真实数据」在这个项目里已经发生过一次，所以每套测试
都要自带「我确实在临时库上」的断言。

跑法：py -3 tests_placeholder.py
"""
import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import db as D                       # noqa: E402
from cacx import placeholder as P              # noqa: E402


def _cfg(root: Path) -> dict:
    return {"db": {"path": str(root / "t.db")}}


class PhBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_ph_"))
        self.d = D.open_db(_cfg(self.tmp))
        self.assertIn("cacx_ph_", str(self.d.path),
                      "测试跑到了真实库上 —— 配置键写错了")

        # 3 篇有正文、2 篇占位（打标）、1 篇半成品（chars=0 但未打标）
        self.full, self.ph, self.ghost = [], [], []
        spec = [
            ("cac", "有正文一", "正文内容AAA", 0),
            ("cac", "有正文二", "正文内容BBB", 0),
            ("zjwx", "占位一", "", 1),
            ("zjwx", "占位二", "", 1),
            ("zjwx", "半成品", "", 0),
        ]
        for i, (site, title, text, empty) in enumerate(spec):
            aid = self.d.conn.execute(
                "INSERT INTO articles (site, column_name, pub_day, title, "
                "url, content_text, chars, content_empty, year, first_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (site, "栏目", f"202{i}-01-0{i+1}", title,
                 f"http://t/{i}", text, len(text), empty,
                 f"202{i}", f"2026-01-0{i+1} 00:00:00")).lastrowid
            (self.full if not empty and text else
             self.ghost if not empty else self.ph).append(aid)

        # 导航/站外条目 —— 它们在 external_links，不在 articles
        for kind, host in (("index_page", "www.cac.gov.cn"),
                           ("external", "mp.weixin.qq.com"),
                           ("wechat", "mp.weixin.qq.com")):
            self.d.conn.execute(
                "INSERT INTO external_links (site, column_name, fail_kind, "
                "target_host, title, list_url, is_own_site, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("cac", "栏目", kind, host, "导航页",
                 f"http://x/{kind}", 0, "2026-01-01"))
        # 给占位一篇挂摘要，验证级联统计
        self.d.conn.execute(
            "INSERT INTO ai_annotations (article_id, task, provider, model, "
            "result, created_at) VALUES (?,?,?,?,?,?)",
            (self.ph[0], "summarize", "p", "m", "{}", "2026-01-02"))
        self.d.conn.commit()

    def tearDown(self):
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def n(self, sql, *a):
        return self.d.conn.execute(sql, a).fetchone()[0]


class TestList(PhBase):
    def test_lists_only_marked_empty(self):
        r = P.list_placeholders(self.d)
        self.assertEqual(r["total"], 2)
        titles = sorted(i["title"] for i in r["items"])
        self.assertEqual(titles, ["占位一", "占位二"])

    def test_ghost_excluded(self):
        """⚠ 半成品（chars=0 但未打 content_empty 标）不算占位。
        它是抓取中途崩掉的残骸 = 脏数据，不是「已知缺口」。
        混进来会让缺口数字失真（多一条假信号）。"""
        r = P.list_placeholders(self.d)
        self.assertNotIn("半成品", [i["title"] for i in r["items"]])

    def test_navigation_and_external_not_included(self):
        """⚠ 用户特别强调过：不能被导航一类的失败干扰。
        导航页/站外/公众号被 classify 判成 SKIP_KINDS，根本不进
        articles，它们在 external_links 里 —— 占位视图必须与它们无关。"""
        r = P.list_placeholders(self.d)
        titles = [i["title"] for i in r["items"]]
        for bad in ("导航页", "专题", "栏目首页"):
            self.assertNotIn(bad, titles)
        # external_links 里有 3 条，但占位数不因此 +3
        self.assertEqual(self.n("SELECT COUNT(*) FROM external_links"), 3)
        self.assertEqual(r["total"], 2)

    def test_reports_exclusions(self):
        """边界必须写进返回值，让前端能如实告知用户"""
        r = P.list_placeholders(self.d)
        joined = " ".join(r["excludes"])
        self.assertIn("index_page", joined)
        self.assertIn("external", joined)

    def test_title_searchable(self):
        """占位条目没有正文 → 不在 FTS 里，只能按标题搜。
        走 FTS 必然 0 结果，用户会误以为功能坏了。"""
        r = P.list_placeholders(self.d, q="占位一")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["mode"], "like")

    def test_body_word_does_not_match(self):
        """搜正文里的词不该命中占位 —— 这是正确行为，不是 bug"""
        r = P.list_placeholders(self.d, q="正文内容AAA")
        self.assertEqual(r["total"], 0)

    def test_sort_retry_first(self):
        """按重抓优先级：最早入库的排前面"""
        r = P.list_placeholders(self.d)
        days = [i["id"] for i in r["items"]]
        self.assertEqual(days, sorted(days))

    def test_has_url_for_jump_back(self):
        """占位也要能跳回原文 —— 否则占位毫无用处"""
        r = P.list_placeholders(self.d)
        for it in r["items"]:
            self.assertTrue(it["url"].startswith("http://"))

    def test_by_site_counts(self):
        r = P.list_placeholders(self.d)
        self.assertEqual(r["by_site"], {"zjwx": 2})


class TestSearchView(PhBase):
    def test_default_view_unchanged(self):
        r = self.d.search(q="", limit=10)
        self.assertEqual(r["total"], 3)   # 3 篇有正文
        self.assertFalse(r["only_empty"])

    def test_only_empty_view(self):
        r = self.d.search(q="", skip_empty=False, only_empty=True, limit=10)
        self.assertEqual(r["total"], 2)
        self.assertTrue(r["only_empty"])

    def test_skip_empty_and_only_empty_both_true(self):
        """两者都传时以 only_empty 为准，否则占位视图会被自己的
        过滤器清空（skip_empty 会要求 content_empty=0）"""
        r = self.d.search(q="", skip_empty=True, only_empty=True, limit=10)
        self.assertEqual(r["total"], 2)

    def test_normal_search_still_hides_empty(self):
        r = self.d.search(q="占位", limit=10)
        self.assertEqual(r["total"], 0)


class TestDiagnose(PhBase):
    def test_video_like_title(self):
        msg = P.diagnose_one({"title": "微视频：团结奋斗再出发",
                              "url": "http://a/1.htm"})
        self.assertIn("视频", msg)

    def test_attachment(self):
        msg = P.diagnose_one({"title": "附件", "url": "http://a/x.pdf"})
        self.assertIn("附件", msg)

    def test_generic_fallback(self):
        msg = P.diagnose_one({"title": "普通标题", "url": "http://a/1.htm"})
        self.assertTrue(msg)

    def test_diagnose_makes_no_network_call(self):
        """诊断只能基于已有字段推断 —— 它自己不该变成另一种副作用"""
        import inspect
        src = inspect.getsource(P.diagnose_one)
        for banned in ("requests", "urlopen", "fetch_article",
                       "http.get", ".get(url"):
            self.assertNotIn(banned, src)


class TestBackfill(PhBase):
    def setUp(self):
        super().setUp()
        self.cfg = {"db": {"path": str(self.tmp / "t.db")},
                    "sites": {}, "collector": {}, "ai": {}}

    def test_dry_run_no_write(self):
        r = P.backfill(self.cfg, self.d, self.ph, dry_run=True)
        self.assertEqual(r["todo"], 2)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r.get("filled"), None)
        self.assertEqual(self.n(
            "SELECT COUNT(*) FROM articles WHERE COALESCE(content_empty,0)=1"), 2)

    def test_refuses_articles_with_content(self):
        """⚠ 不能给「已有正文的文章」开覆盖重写的后门，
        否则这个接口就成了绕过正常流程的万能写入入口。"""
        r = P.backfill(self.cfg, self.d, self.full, dry_run=True)
        self.assertEqual(r["todo"], 0)
        self.assertEqual(sorted(r["skipped_has_content"]), sorted(self.full))

    def test_reports_unknown_ids(self):
        r = P.backfill(self.cfg, self.d, [999999], dry_run=True)
        self.assertEqual(r["not_found"], [999999])

    def test_empty_ids_rejected(self):
        r = P.backfill(self.cfg, self.d, [], dry_run=True)
        self.assertIn("error", r)

    def test_dry_run_reports_would_fetch(self):
        r = P.backfill(self.cfg, self.d, self.ph, dry_run=True)
        for x in r["results"]:
            self.assertTrue(x["would_fetch"].startswith("http://"))


class TestWebApi(PhBase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        WA = importlib.import_module("cacx.web.app")
        self.WA = WA
        self.old = WA.DB
        WA.DB = self.d
        self.c = TestClient(WA.app)

    def tearDown(self):
        self.WA.DB = self.old
        super().tearDown()

    def test_placeholders_endpoint(self):
        r = self.c.get("/api/placeholders")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 2)

    def test_search_only_empty(self):
        r = self.c.get("/api/search?only_empty=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 2)

    def test_search_default_unchanged(self):
        self.assertEqual(self.c.get("/api/search").json()["total"], 3)

    def test_backfill_dry_run_default(self):
        r = self.c.post("/api/backfill", json={"article_ids": self.ph})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dry_run"])
        self.assertEqual(r.json()["todo"], 2)

    def test_backfill_rejects_too_many(self):
        r = self.c.post("/api/backfill",
                        json={"article_ids": list(range(1, 60))})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)