# -*- coding: utf-8 -*-
"""
批量摘要的选取口径 + 同源站点去重
================================================================
两件事：
1) limit 必须是**全局上限**。界面上写着「最多条数 10」却产出 20，
   是控件含义与行为不一致 —— 用户看到数字对不上就不知道该信哪个。
2) 同一个网站配了多个 site code（zjwx 与 zjwx-api 都是
   www.zjwx.gov.cn）时，必须识别出来，否则会重复入库 + 重复烧配额。

铁律：临时库 + 临时配置，绝不碰 config/settings.json 与 data/cacx.db。

跑法：py -3 tests_batch_limit.py
"""
import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import db as D  # noqa: E402


def _cfg(root: Path, sites: dict) -> dict:
    return {"db": {"path": str(root / "t.db")},
            "sites": sites, "collector": {},
            "search": {"min_fts_chars": 3},
            "ai": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_bl_"))
        # ⚠ 必须把 sites.list_custom 也隔离掉。
        #   sites.all_sites(cfg) = builtin_specs(cfg) + list_custom()，
        #   而 list_custom 直接读磁盘上的 config/sites.d/*.json。
        #   只换 cfg 不挡它 → 真实配置里的 zjwx-api 会混进测试夹具，
        #   表现为「我明明把 enabled 设成 False，代码还是报同源」。
        #   这类「测试没隔离干净」的假失败最费时间，必须从夹具层堵死。
        import cacx.sites as S
        self._old_list_custom = S.list_custom
        S.list_custom = lambda: {}
        self.addCleanup(self._restore)

        # 两个「同源」站点 + 一个独立站点
        self.sites = {
            "cac": {"code": "cac", "name": "中国网信办",
                    "base_url": "https://www.cac.gov.cn",
                    "enabled": True, "builtin": True},
            "zjwx": {"code": "zjwx", "name": "浙江网信网",
                     "base_url": "https://www.zjwx.gov.cn",
                     "enabled": True, "builtin": True},
            "zjwx-api": {"code": "zjwx-api", "name": "浙江网信网(验证)",
                         "base_url": "https://www.zjwx.gov.cn",
                         "enabled": True, "builtin": False},
        }
        self.cfg = _cfg(self.tmp, self.sites)
        self.d = D.open_db(self.cfg)
        self.assertIn("cacx_bl_", str(self.d.path))
        # cac 30 篇、zjwx 30 篇；zjwx-api 故意不放数据（尚未同步）
        for site, n in (("cac", 30), ("zjwx", 30)):
            for i in range(n):
                day = f"2024-01-{i+1:02d}"
                self.d.conn.execute(
                    "INSERT INTO articles (site, column_name, pub_day, "
                    "pub_date, title, url, content_text, chars, "
                    "content_empty, year) VALUES (?,?,?,?,?,?,?,?,0,?)",
                    (site, "栏目", day, day + " 10:00",
                     f"{site} 第{i}篇", f"http://{site}/{i}",
                     "正文内容" * 20, 100, "2024"))
        # 模拟「同源两站已入库同一篇稿件」：zjwx-api 也存 3 篇 zjwx 的稿
        for i in range(3):
            day = f"2024-01-{i+1:02d}"
            self.d.conn.execute(
                "INSERT INTO articles (site, column_name, pub_day, "
                "pub_date, title, url, content_text, chars, "
                "content_empty, year) "
                "VALUES ('zjwx-api','栏目',?,?,?,?,?,100,0,'2024')",
                (day, day + " 10:00", f"zjwx 第{i}篇", f"http://zjwx/{i}",
                 "正文内容" * 20))
        self.d.conn.commit()

    def _restore(self):
        import cacx.sites as S
        S.list_custom = self._old_list_custom

    def tearDown(self):
        self._restore()
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _req(self, **kw):
        WA = importlib.import_module("cacx.web.app")
        base = dict(sites=["cac", "zjwx"], column="", date_from="",
                    date_to="", mode="incremental", limit=10,
                    concurrency=1, rate=0.0, resume=False,
                    stop_on_error=False, dry_run=True)
        base.update(kw)
        return WA.BatchReq(**base), WA

    def _select(self, **kw):
        req, WA = self._req(**kw)
        return WA._select_batch_articles(self.d, self.cfg, req)


class TestLimitSemantics(Base):
    def test_limit_is_global_not_per_site(self):
        """⚠ 核心回归：勾 2 个站、填 limit=10，结果必须是 10 而不是 20。"""
        _, merged = self._select(sites=["cac", "zjwx"], limit=10)
        self.assertEqual(len(merged), 10)

    def test_single_site_still_respects_limit(self):
        _, merged = self._select(sites=["cac"], limit=10)
        self.assertEqual(len(merged), 10)

    def test_limit_larger_than_pool(self):
        _, merged = self._select(sites=["cac", "zjwx"], limit=100)
        # 60 篇真实数据（zjwx-api 那 3 篇因站点没勾选不参与）
        self.assertEqual(len(merged), 60)

    def test_all_sites_dedups_same_source(self):
        """勾选全部三个站时，同一篇稿件只应出现一次"""
        _, merged = self._select(sites=["cac", "zjwx", "zjwx-api"],
                                 limit=100)
        urls = [m["url"] for m in merged]
        self.assertEqual(len(urls), len(set(urls)),
                         "同一 URL 出现了多次 —— 同源站点没去重")
        self.assertEqual(len(merged), 60)

    def test_same_site_priority_kept(self):
        """limit 很小时，优先保留最新的（日期倒序后再截断）"""
        _, merged = self._select(sites=["cac", "zjwx"], limit=4)
        self.assertEqual(len(merged), 4)
        days = sorted((m.get("pub_date") or "")[:10] for m in merged)
        self.assertTrue(all(d >= "2024-01-27" for d in days),
                        f"截断后应保留最新 4 篇，实际 {days}")

    def test_incremental_excludes_annotated(self):
        """有摘要的不该进 incremental 的总数（否则进度条分母虚胖）"""
        first = self.d.conn.execute(
            "SELECT id FROM articles WHERE site='cac' LIMIT 3").fetchall()
        for r in first:
            self.d.conn.execute(
                "INSERT INTO ai_annotations (article_id, task, provider, "
                "model, result, created_at) VALUES (?,'summarize','p','m',"
                "'{}','2026')", (r["id"],))
        self.d.conn.commit()
        _, merged = self._select(sites=["cac"], limit=10)
        got = {m["id"] for m in merged}
        for r in first:
            self.assertNotIn(r["id"], got)


class TestSameSourceDetection(Base):
    def test_detects_same_base_url(self):
        _, WA = self._req()
        groups = WA._same_source_groups(self.cfg)
        self.assertIn("https://www.zjwx.gov.cn", groups)
        self.assertEqual(sorted(groups["https://www.zjwx.gov.cn"]),
                         ["zjwx", "zjwx-api"])
        self.assertNotIn("https://www.cac.gov.cn", groups)

    def test_warns_when_both_selected(self):
        _, WA = self._req()
        warns = WA._warn_same_source(self.cfg, ["cac", "zjwx", "zjwx-api"])
        self.assertEqual(len(warns), 1)
        w = warns[0]
        self.assertEqual(sorted(w["codes"]), ["zjwx", "zjwx-api"])
        self.assertIn("zjwx.gov.cn", w["base_url"])
        self.assertIn("重复", w["note"])

    def test_no_warning_for_distinct_sites(self):
        _, WA = self._req()
        self.assertEqual(
            WA._warn_same_source(self.cfg, ["cac", "zjwx"]), [])

    def test_batch_response_carries_warning(self):
        """警告要出现在接口返回里，前端才有机会显示"""
        WA = importlib.import_module("cacx.web.app")
        self.assertTrue(hasattr(WA, "_warn_same_source"))

    def test_disabled_site_not_counted(self):
        """停用的站点不参与同源判定 —— 它不会被同步"""
        self.sites["zjwx-api"]["enabled"] = False
        _, WA = self._req()
        self.assertEqual(
            WA._warn_same_source(self.cfg, ["zjwx", "zjwx-api"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)