# -*- coding: utf-8 -*-
"""
批量删除测试
================================================================
铁律：**一律用临时目录建临时库**，绝不碰 config/settings.json 与
data/cacx.db。这不是洁癖 —— 早先一次打桩测试把 6 条假摘要写进了真实库，
还得靠「result 里带双重标记」去精确捞出来清理。删除功能的测试更危险，
写错一行就是真删数据。

跑法：py -3 tests_purge.py
"""
import importlib
import inspect
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import db as D           # noqa: E402
from cacx.purge import (PURGE_TARGETS, PurgeRefused,  # noqa: E402
                        _clean, execute_purge, preview_purge)


def _cfg(root: Path) -> dict:
    # 配置键是 db.path（不是 paths.db）。写错会**静默打开真实库** ——
    # open_db 对无法识别的键回落到 data/cacx.db，于是删除测试可能删掉真数据。
    return {"db": {"path": str(root / "test.db")}}


class PurgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_purge_"))
        self.d = D.open_db(_cfg(self.tmp))
        # 安全断言：本套测试的所有删除都必须发生在临时库上。
        # 万一哪天 _cfg 写错键，后面每个删除用例都会去删用户的真数据。
        self.assertIn("cacx_purge_", str(self.d.path))

        # 造数据：两个站点、跨年份、带摘要、含空正文、含站外条目
        self.ids = []
        rows = [
            ("cac", "通知", "2019-05-01", "cac 2019 甲", "正文甲" * 30, 0),
            ("cac", "通知", "2020-06-01", "cac 2020 乙", "正文乙" * 30, 0),
            ("cac", "评论", "2019-07-02", "cac 2019 丙", "正文丙" * 30, 0),
            ("zjwx", "时政", "2019-08-03", "zjwx 2019 丁", "正文丁" * 30, 0),
            ("zjwx", "时政", "2021-09-04", "zjwx 2021 戊", "正文戊" * 30, 0),
            ("zjwx", "时政", "2018-10-05", "zjwx 2018 己", "", 1),
        ]
        for site, col, day, title, content, empty in rows:
            aid = self.d.conn.execute(
                "INSERT INTO articles (site, column_name, pub_day, title, "
                "url, content_text, chars, content_empty, year, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (site, col, day, title,
                 f"http://x/{site}/{day}/{len(self.ids)}",
                 content, len(content), empty, day[:4],
                 "2026-10-01 10:00:00"),
            ).lastrowid
            self.ids.append(aid)

        # 摘要：加统一标记便于精确核对
        for aid, task, provider in ((self.ids[0], "summarize", "minimax"),
                                    (self.ids[3], "sentiment", "minimax"),
                                    (self.ids[4], "summarize", "minimax")):
            self.d.conn.execute(
                "INSERT INTO ai_annotations (article_id, task, provider, "
                "model, result, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (aid, task, provider, "m",
                 '{"summary":"CACXPURGETEST"}', "2026-10-02 10:00:00"))
        # 事件埋点
        self.d.conn.execute(
            "INSERT INTO events (article_id, kind, created_at) "
            "VALUES (?,?,?)", (self.ids[0], "search", "2026-10-02 10:00:00"))
        # 站外条目
        for kind, own, host in (("timeout", 0, "other.gov"),
                                ("not_found", 0, "third.net")):
            self.d.conn.execute(
                "INSERT INTO external_links (site, column_name, "
                "fail_kind, target_host, title, list_url, is_own_site, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("zjwx", "时政", kind, host, "t",
                 f"http://x/list/{kind}", own, "2026-10-02"))
        self.d.conn.commit()

    def tearDown(self):
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def n(self, sql, *a):
        return self.d.conn.execute(sql, a).fetchone()[0]


class TestRefuse(PurgeBase):
    """该拒的一定要拒 —— 删除功能的正确性主要体现在这里"""

    def test_refuse_illegal_target(self):
        for t in ("meta", "sync_runs", "articles ", "drop", ""):
            with self.subTest(t=t):
                with self.assertRaises(PurgeRefused):
                    preview_purge(self.d, t, site="cac")

    def test_meta_sync_runs_not_in_targets(self):
        """库的血缘不可删 —— 这条要写死在白名单之外"""
        self.assertNotIn("meta", PURGE_TARGETS)
        self.assertNotIn("sync_runs", PURGE_TARGETS)

    def test_refuse_no_filters(self):
        """无条件 = 手滑清库，必须拒绝"""
        with self.assertRaises(PurgeRefused) as cm:
            preview_purge(self.d, "articles")
        self.assertIn("筛选条件", str(cm.exception))

    def test_refuse_falsey_filters(self):
        """⚠ 回归：假条件必须被识别成「没给条件」。

        早先 purge 用 `v not in (None, "", [], {})` 过滤，而
        `False not in (None, "", [], {})` 是 True ——
        empty_content_only=False 被当成有效条件：
          · 绕过「无条件拒绝」的总闸门
          · 但统计时 `if f.get(...)` 又是 False，不拼 WHERE
          · 结果统计的是**全表**，等于给了「删前 200 条」的通路
        删除功能里这种漏洞绝不能有。
        """
        with self.assertRaises(PurgeRefused):
            preview_purge(self.d, "articles", site="", ids=[],
                          empty_content_only=False)

    def test_clean_semantics(self):
        # 空串 / 空列表 / False / None / 空 dict 都要丢
        self.assertEqual(_clean({"site": "", "ids": [], "z": {},
                                 "empty_content_only": False,
                                 "y": None}), {})
        # 0 是有效值（chars_below=0 有意义），不能被当假值丢掉
        self.assertEqual(_clean({"chars_below": 0}), {"chars_below": 0})
        # True 必须保留
        self.assertEqual(_clean({"empty_content_only": True}),
                         {"empty_content_only": True})

    def test_refuse_zero_match(self):
        with self.assertRaises(PurgeRefused) as cm:
            preview_purge(self.d, "articles", site="not-a-site")
        self.assertIn("没有命中", str(cm.exception))

    def test_execute_refuse_without_token(self):
        """核心闸门：不带 token 不许删"""
        with self.assertRaises(PurgeRefused) as cm:
            execute_purge(self.d, "articles", "", site="cac", year="2019")
        self.assertIn("confirm_token", str(cm.exception))

    def test_execute_refuse_wrong_token(self):
        with self.assertRaises(PurgeRefused) as cm:
            execute_purge(self.d, "articles", "deadbeefdeadbeef",
                          site="cac", year="2019")
        self.assertIn("失效", str(cm.exception))

    def test_nothing_deleted_after_all_refusals(self):
        """上面所有拒绝路径跑完，库里必须一条没少"""
        self.assertEqual(self.n("SELECT COUNT(*) FROM articles"), 6)
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_annotations"), 3)


class TestIdsFilter(PurgeBase):
    """--ids 路径：曾因 JOIN 歧义整条坏掉，且当时没有任何测试覆盖"""

    def test_ids_filter_with_cascade_stats(self):
        """⚠ 回归：早先 _stats_articles 的 where 用裸列名（id/site/chars…），
        单独查 articles 没问题，但级联统计那段要拼到
        `ai_annotations x JOIN articles a` 后面 —— 此时裸 `id`
        同时匹配 x 和 a，SQLite 报 ambiguous column name。
        只有 site 侥幸能过（site 只在 articles 上），
        于是「除 site 外的所有条件」在带级联统计时全是坏的，
        而这条路径当时根本没被实测过。
        """
        f = dict(ids=self.ids[:2])
        st = preview_purge(self.d, "articles", **f)
        self.assertEqual(st["matches"], 2)
        self.assertEqual(st["cascade"]["ai_annotations"], 1)

    def test_ids_execute_matches_preview(self):
        """预览说几条就必须删几条（两份条件实现漂移会静默删错）"""
        f = dict(ids=[self.ids[3], self.ids[4]])
        st = preview_purge(self.d, "articles", **f)
        r = execute_purge(self.d, "articles", st["confirm_token"], **f)
        self.assertEqual(r["before"], st["matches"])
        self.assertEqual(r["deleted"]["articles"], st["matches"])
        self.assertEqual(self.n("SELECT COUNT(*) FROM articles"), 4)

    def test_ids_with_site_and_year_combined(self):
        """组合条件在 JOIN 下也必须成立"""
        f = dict(ids=self.ids, site="cac", year="2019")
        st = preview_purge(self.d, "articles", **f)
        self.assertEqual(st["matches"], 2)


class TestLimit(PurgeBase):
    """条件分级上限：宁可拒绝，也不给「一键清空」"""

    def test_coarse_limit_smaller(self):
        from cacx.purge import _COARSE_LIMIT, _FINE_LIMIT, _limit_for
        self.assertEqual(_limit_for({"site": "zjwx"}), _COARSE_LIMIT)
        self.assertEqual(_limit_for({"site": "zjwx", "year": "2019"}),
                         _FINE_LIMIT)
        self.assertLess(_COARSE_LIMIT, _FINE_LIMIT)

    def test_coarse_over_limit_refused(self):
        """数据量大到超过粗上限时拒绝，并提示要加精确条件"""
        for i in range(205):
            self.d.conn.execute(
                "INSERT INTO articles (site, column_name, pub_day, title, "
                "url, content_text, chars, content_empty, year) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("big", "c", "2020-01-01", f"t{i}", f"http://b/{i}",
                 "x" * 100, 100, 0, "2020"))
        self.d.conn.commit()
        with self.assertRaises(PurgeRefused) as cm:
            preview_purge(self.d, "articles", site="big")
        self.assertIn("上限", str(cm.exception))


class TestPreview(PurgeBase):
    def test_preview_articles(self):
        st = preview_purge(self.d, "articles", site="zjwx")
        self.assertEqual(st["target"], "articles")
        self.assertEqual(st["matches"], 3)
        self.assertTrue(st["confirm_token"])
        self.assertEqual(len(st["sample"]), 3)
        self.assertEqual(st["by_site"][0]["site"], "zjwx")

    def test_preview_reports_cascade(self):
        """删文章要连带删摘要 —— 预览里必须先说清楚"""
        st = preview_purge(self.d, "articles", site="zjwx", year="2019")
        self.assertEqual(st["cascade"]["ai_annotations"], 1)

    def test_preview_warnings(self):
        st = preview_purge(self.d, "articles", site="zjwx")
        joined = " ".join(st["warnings"])
        self.assertIn("不可逆", joined)
        # 没给 date_to 必须提醒
        self.assertIn("date_to", joined)

    def test_preview_annotations(self):
        st = preview_purge(self.d, "annotations", task="summarize")
        self.assertEqual(st["matches"], 2)
        self.assertEqual(st["by_task"][0]["task"], "summarize")
        self.assertEqual(st["by_task"][0]["n"], 2)

    def test_preview_annotations_by_site(self):
        st = preview_purge(self.d, "annotations", site="zjwx")
        self.assertEqual(st["matches"], 2)

    def test_preview_external(self):
        st = preview_purge(self.d, "external_links", fail_kind="timeout")
        self.assertEqual(st["matches"], 1)
        self.assertEqual(st["by_kind"][0]["fail_kind"], "timeout")

    def test_preview_by_date_range(self):
        st = preview_purge(self.d, "articles",
                           date_from="2019-01-01", date_to="2019-12-31")
        self.assertEqual(st["matches"], 3)

    def test_preview_empty_content(self):
        st = preview_purge(self.d, "articles", empty_content_only=True)
        self.assertEqual(st["matches"], 1)
        self.assertEqual(st["sample"][0]["site"], "zjwx")

    def test_token_stable_across_calls(self):
        """⚠ 回归：token 不能掺时间戳。
        早先 _fingerprint 里混了 time.time()，导致同一份预览算两次
        token 就不同 → 二次确认闸门把自己锁死，功能完全不可用。"""
        a = preview_purge(self.d, "articles", site="zjwx", year="2019")
        b = preview_purge(self.d, "articles", site="zjwx", year="2019")
        self.assertEqual(a["confirm_token"], b["confirm_token"])


class TestExecuteArticles(PurgeBase):
    def test_execute_articles_cascade(self):
        f = dict(site="zjwx", year="2019")
        tok = preview_purge(self.d, "articles", **f)["confirm_token"]
        r = execute_purge(self.d, "articles", tok, **f)
        self.assertEqual(r["before"], 1)
        self.assertEqual(r["deleted"]["articles"], 1)
        self.assertEqual(r["deleted"]["ai_annotations"], 1)
        self.assertEqual(r["total"], 1)
        # 孤儿检查
        self.assertEqual(self.n(
            "SELECT COUNT(*) FROM ai_annotations x LEFT JOIN articles a "
            "ON a.id=x.article_id WHERE a.id IS NULL"), 0)
        self.assertEqual(self.n(
            "SELECT COUNT(*) FROM events x LEFT JOIN articles a "
            "ON a.id=x.article_id WHERE a.id IS NULL"), 0)

    def test_fts_stays_consistent(self):
        """FTS 由触发器维护，删完索引里不该还留着已删文章"""
        f = dict(site="zjwx", year="2019")
        tok = preview_purge(self.d, "articles", **f)["confirm_token"]
        gone = execute_purge(self.d, "articles", tok, **f)["deleted"]["articles"]
        self.assertEqual(gone, 1)
        orphan = self.n(
            "SELECT COUNT(*) FROM articles_fts WHERE rowid NOT IN "
            "(SELECT id FROM articles)")
        self.assertEqual(orphan, 0)

    def test_token_invalid_after_data_change(self):
        """核心防呆：预览之后库变了 → token 必须失效"""
        f = dict(site="zjwx")
        tok = preview_purge(self.d, "articles", **f)["confirm_token"]
        self.d.conn.execute(
            "INSERT INTO articles (site, column_name, pub_day, title, url, "
            "content_text, chars, content_empty, year) "
            "VALUES ('zjwx','时政','2022-01-01','新增','http://x/n','',0,1,'2022')")
        self.d.conn.commit()
        with self.assertRaises(PurgeRefused) as cm:
            execute_purge(self.d, "articles", tok, **f)
        self.assertIn("失效", str(cm.exception))
        # 关键：一条都不能少删
        self.assertEqual(self.n("SELECT COUNT(*) FROM articles"), 7)

    def test_execute_reports_each_item(self):
        """绝不静默删除：逐项回报"""
        f = dict(site="zjwx", year="2021")
        tok = preview_purge(self.d, "articles", **f)["confirm_token"]
        r = execute_purge(self.d, "articles", tok, **f)
        for k in ("articles", "ai_annotations", "events", "fts_optimized"):
            self.assertIn(k, r["deleted"])


class TestExecuteAnnotations(PurgeBase):
    def test_execute_annotations(self):
        f = dict(task="summarize")
        tok = preview_purge(self.d, "annotations", **f)["confirm_token"]
        r = execute_purge(self.d, "annotations", tok, **f)
        self.assertEqual(r["deleted"]["annotations"], 2)
        self.assertEqual(self.n("SELECT COUNT(*) FROM ai_annotations"), 1)

    def test_articles_kept_when_deleting_annotations(self):
        """删摘要不动正文 —— 这是用户最在意的区分"""
        f = dict(task="summarize")
        tok = preview_purge(self.d, "annotations", **f)["confirm_token"]
        execute_purge(self.d, "annotations", tok, **f)
        self.assertEqual(self.n("SELECT COUNT(*) FROM articles"), 6)

    def test_delete_by_result_like(self):
        f = dict(result_like="CACXPURGETEST")
        tok = preview_purge(self.d, "annotations", **f)["confirm_token"]
        r = execute_purge(self.d, "annotations", tok, **f)
        self.assertEqual(r["deleted"]["annotations"], 3)

    def test_delete_by_ids(self):
        f = dict(ids=[self.ids[0], self.ids[1]])
        tok = preview_purge(self.d, "annotations", **f)["confirm_token"]
        r = execute_purge(self.d, "annotations", tok, **f)
        self.assertEqual(r["deleted"]["annotations"], 1)


class TestExecuteExternal(PurgeBase):
    def test_execute_external(self):
        f = dict(fail_kind="timeout")
        tok = preview_purge(self.d, "external_links", **f)["confirm_token"]
        r = execute_purge(self.d, "external_links", tok, **f)
        self.assertEqual(r["deleted"]["external_links"], 1)
        self.assertEqual(self.n("SELECT COUNT(*) FROM external_links"), 1)


class TestWebApi(PurgeBase):
    """Web 接口的闸门 —— 用 TestClient，不起真服务"""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        # ⚠ 必须 import **模块**，不能 from cacx.web import app ——
        # cacx.web 包里的 app 是 FastAPI 实例，DB 挂在模块上，
        # 拿实例去取 WA.DB 会 AttributeError。
        WA = importlib.import_module("cacx.web.app")
        self.WA = WA
        self.old_db = WA.DB
        WA.DB = self.d
        self.c = TestClient(WA.app)

    def tearDown(self):
        self.WA.DB = self.old_db
        super().tearDown()

    def test_preview_endpoint(self):
        r = self.c.post("/api/purge/preview",
                        json={"target": "articles", "site": "zjwx"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["matches"], 3)
        self.assertTrue(r.json()["confirm_token"])

    def test_execute_without_yes_refused(self):
        r = self.c.post("/api/purge/execute",
                        json={"target": "articles", "site": "zjwx"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("未确认", r.json()["detail"]["message"])

    def test_execute_without_token_refused(self):
        r = self.c.post("/api/purge/execute",
                        json={"target": "articles", "site": "zjwx",
                              "yes": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("confirm_token", r.json()["detail"]["message"])

    def test_execute_full_flow(self):
        f = {"target": "articles", "site": "zjwx", "year": "2019"}
        tok = self.c.post("/api/purge/preview", json=f).json()["confirm_token"]
        r = self.c.post("/api/purge/execute", json={**f, "yes": True,
                                                     "confirm_token": tok})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"]["articles"], 1)

    def test_refusal_is_400_not_500(self):
        """按规则拒绝属于正常业务结果，前端要当可读提示展示"""
        r = self.c.post("/api/purge/preview", json={"target": "articles"})
        self.assertEqual(r.status_code, 400)
        # FastAPI 会把 HTTPException 的 dict 载荷包进 detail 一层
        body = r.json()["detail"]
        self.assertTrue(body.get("refused"))
        self.assertIn("筛选条件", body["message"])


class TestMcpTool(PurgeBase):
    def setUp(self):
        super().setUp()
        self.M = importlib.import_module("cacx.mcp_server")

    def test_tool_registered(self):
        fns = [f for f in dir(self.M) if f.startswith("cacx_purge")]
        self.assertEqual(fns, ["cacx_purge"])

    def test_no_filters_refused(self):
        r = self.M.cacx_purge(target="articles")
        self.assertTrue(r.get("refused"))

    def test_dry_run_default_true(self):
        sig = inspect.signature(self.M.cacx_purge)
        self.assertIs(sig.parameters["dry_run"].default, True)

    def test_confirm_token_required(self):
        r = self.M.cacx_purge(target="articles", site="zjwx", dry_run=False)
        self.assertTrue(r.get("refused"))
        self.assertIn("confirm_token", r.get("message", ""))
        self.assertEqual(self.n("SELECT COUNT(*) FROM articles"), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)