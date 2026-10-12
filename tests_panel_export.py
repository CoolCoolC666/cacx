# -*- coding: utf-8 -*-
"""
数据面板：站点过滤 + 图表数据导出 的测试
================================================================
覆盖三件事：
  ① 四张图都能按站点筛（trend / facets / columns / events）
  ② 事件按站点过滤时，search/export 被正确排除并**说明原因**
     —— 半口径数字比没有更误导
  ③ /api/panel/export 的 CSV 与面板显示同源（不会「界面对、导出不对」）

铁律：临时库 + TestClient，不碰真实 data/cacx.db。
跑法：py -3 tests_panel_export.py
"""
import csv
import importlib
import io
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import db as D              # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_panel_"))
        cfg = {"db": {"path": str(self.tmp / "t.db")},
               "ai": {"provider": "none"}, "search": {"min_fts_chars": 3}}
        self.d = D.open_db(cfg)
        self.assertIn("cacx_panel_", str(self.d.path))
        self.WA = importlib.import_module("cacx.web.app")
        self.old = self.WA.DB
        self.WA.DB = self.d
        self.c = TestClient(self.WA.app)

        # cac：12 篇跨 3 年；zjwx：4 篇。栏目各 2 个
        self.mk("cac", "A093710", "国·地方网信", [
            ("2024-01-05", 1), ("2024-02-05", 2), ("2024-03-05", 1),
            ("2025-01-05", 3), ("2025-06-05", 2), ("2025-12-05", 1),
        ])
        self.mk("cac", "A093601", "国·时政要闻", [
            ("2024-05-05", 1), ("2025-03-05", 2), ("2026-01-05", 1),
            ("2026-06-05", 1), ("2024-08-05", 1), ("2025-09-05", 1),
        ])
        self.mk("zjwx", "1673574", "浙·清朗网络", [
            ("2024-04-05", 1), ("2025-07-05", 2), ("2026-02-05", 1),
        ])
        self.mk("zjwx", "1673576", "浙·治理监管", [("2025-11-05", 1)])
        self.d.conn.commit()

    def tearDown(self):
        self.WA.DB = self.old
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mk(self, site, code, cname, rows):
        for day, n in rows:
            for i in range(n):
                self.d.conn.execute(
                    "INSERT INTO articles (site,column_code,column_name,pub_day,"
                    "pub_date,year,title,url,content_text,chars,content_empty)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (site, code, cname, day, day, day[:4],
                     f"{site}-{cname}-{day}-{i}",
                     f"http://{site}/{code}/{day}/{i}", "正文" * 50, 100))


# ==================================================================== 趋势
class TestTrendSite(Base):
    def test_按年全部站点(self):
        r = self.c.get("/api/trend?granularity=year").json()
        self.assertEqual(sum(x["n"] for x in r["items"]), 22)

    def test_按年按站点(self):
        r = self.c.get("/api/trend?granularity=year&site=cac").json()
        self.assertEqual(sum(x["n"] for x in r["items"]), 17)
        zj = self.c.get("/api/trend?granularity=year&site=zjwx").json()
        self.assertEqual(sum(x["n"] for x in zj["items"]), 5)

    def test_按月按站点(self):
        r = self.c.get("/api/trend?granularity=month&site=zjwx").json()
        self.assertEqual(sum(x["n"] for x in r["items"]), 5)
        self.assertTrue(all(k.startswith(("2024-", "2025-", "2026-"))
                            for k in (x["k"] for x in r["items"])))

    def test_两站之和等于全部(self):
        a = sum(x["n"] for x in self.c.get(
            "/api/trend?granularity=year").json()["items"])
        b = sum(x["n"] for x in self.c.get(
            "/api/trend?granularity=year&site=cac").json()["items"])
        e = sum(x["n"] for x in self.c.get(
            "/api/trend?granularity=year&site=zjwx").json()["items"])
        self.assertEqual(a, b + e, "两站之和应等于全部，否则按站点看会漏数据")

    def test_不存在的站点返回空而不是报错(self):
        r = self.c.get("/api/trend?granularity=year&site=nope").json()
        self.assertEqual(r["items"], [])


# ==================================================================== 栏目
class TestColumnsSite(Base):
    def test_facets带站点(self):
        allc = self.c.get("/api/facets").json()
        cac = self.c.get("/api/facets?site=cac").json()
        zj = self.c.get("/api/facets?site=zjwx").json()
        self.assertEqual(sum(x["n"] for x in cac["columns"]), 17)
        self.assertEqual(sum(x["n"] for x in zj["columns"]), 5)
        self.assertEqual(
            sum(x["n"] for x in allc["columns"]),
            sum(x["n"] for x in cac["columns"]) + sum(x["n"] for x in zj["columns"]))

    def test_按站点时sites仍返回全部但标明筛选态(self):
        """站点数组必须保持全量 —— 前端的站点切换下拉靠它渲染，
        筛成单站的话下拉里就只剩一个选项、切不回去了。
        所以「筛的是栏目」，站点列表不动，用 filtered_site 标明当前筛选。"""
        cac = self.c.get("/api/facets?site=cac").json()
        self.assertEqual([s["site"] for s in cac["sites"]], ["cac", "zjwx"])
        self.assertEqual(cac["filtered_site"], "cac")
        self.assertEqual({c["site"] for c in cac["columns"]}, {"cac"})


# ==================================================================== 事件
class TestEventsSite(Base):
    def setUp(self):
        super().setUp()
        cac_ids = [r[0] for r in self.d.conn.execute(
            "SELECT id FROM articles WHERE site='cac' LIMIT 3")]
        zj_ids = [r[0] for r in self.d.conn.execute(
            "SELECT id FROM articles WHERE site='zjwx' LIMIT 2")]
        self.d.record_event("open", cac_ids[0])
        self.d.record_event("open", cac_ids[1])
        self.d.record_event("open", zj_ids[0])
        self.d.record_event("open", zj_ids[1])
        self.d.record_event("summarize", cac_ids[2])
        # 跨站的 search / export：没有 article_id，天然不可归属
        self.d.record_event("search", 0, detail="清朗")
        self.d.record_event("export", 0, detail="全部")

    def test_不筛站点时统计全部(self):
        r = self.c.get("/api/events?days=30").json()
        kinds = {x["kind"]: x["n"] for x in r["by_kind"]}
        self.assertEqual(kinds["open"], 4)
        self.assertEqual(kinds["search"], 1)
        self.assertEqual(kinds["export"], 1)
        self.assertIsNone(r["site_applicable"])

    def test_按站点过滤排除不可归属的类别(self):
        """
        ⚠ 核心判据：search / export 跨站，不可归属。
        混进来报会让人以为「浙江的检索是 0 次」，实际是不可归属。
        """
        r = self.c.get("/api/events?days=30&site=cac").json()
        kinds = {x["kind"] for x in r["by_kind"]}
        self.assertNotIn("search", kinds)
        self.assertNotIn("export", kinds)
        self.assertIn("open", kinds)
        self.assertIn("summarize", kinds)
        self.assertEqual(r["site_applicable"], ["open", "summarize"])
        self.assertIn("不可归属", r["note"])

    def test_按站点的open数量正确(self):
        r = self.c.get("/api/events?days=30&site=cac").json()
        kinds = {x["kind"]: x["n"] for x in r["by_kind"]}
        self.assertEqual(kinds["open"], 2)      # 3 个 open 里 2 个属 cac
        zj = self.c.get("/api/events?days=30&site=zjwx").json()
        self.assertEqual({x["kind"]: x["n"] for x in zj["by_kind"]}["open"], 2)

    def test_两站open之和等于总数(self):
        a = {x["kind"]: x["n"] for x in self.c.get(
            "/api/events?days=30").json()["by_kind"]}
        b = {x["kind"]: x["n"] for x in self.c.get(
            "/api/events?days=30&site=cac").json()["by_kind"]}
        d = {x["kind"]: x["n"] for x in self.c.get(
            "/api/events?days=30&site=zjwx").json()["by_kind"]}
        self.assertEqual(a["open"], b["open"] + d["open"])

    def test_top_open按站点过滤(self):
        r = self.c.get("/api/events?days=30&site=zjwx").json()
        for t in r["top_open"]:
            self.assertEqual(t["site"], "zjwx", "top_open 不该混进别的站")


# ==================================================================== 导出
class TestPanelExport(Base):
    def csv_of(self, qs):
        r = self.c.get(f"/api/panel/export?{qs}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/csv", r.headers.get("content-type", ""))
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        txt = r.content.decode("utf-8-sig")
        return list(csv.reader(io.StringIO(txt)))

    def test_按年导出(self):
        rows = self.csv_of("which=trend&granularity=year")
        self.assertEqual(rows[0], ["年份", "文章数", "字数"])
        self.assertEqual(sum(int(r[1]) for r in rows[1:]), 22)

    def test_按年按站点导出与接口一致(self):
        rows = self.csv_of("which=trend&granularity=year&site=cac")
        self.assertEqual(sum(int(r[1]) for r in rows[1:]), 17)
        api = self.c.get("/api/trend?granularity=year&site=cac").json()
        self.assertEqual(len(rows) - 1, len(api["items"]),
                         "导出行数应与接口条目数一致")

    def test_按月导出(self):
        rows = self.csv_of("which=trend&granularity=month&site=zjwx")
        self.assertEqual(rows[0], ["月份", "文章数", "字数"])
        self.assertEqual(sum(int(r[1]) for r in rows[1:]), 5)

    def test_栏目导出(self):
        rows = self.csv_of("which=columns")
        self.assertEqual(rows[0], ["站点", "站点名", "栏目", "文章数"])
        self.assertEqual(sum(int(r[3]) for r in rows[1:]), 22)

    def test_栏目按站点导出(self):
        rows = self.csv_of("which=columns&site=zjwx")
        self.assertTrue(all(r[0] == "zjwx" for r in rows[1:]))

    def test_事件导出(self):
        rows = self.csv_of("which=events")
        self.assertEqual(rows[0], ["行为类型", "次数"])

    def test_文件名带站点名与时间戳(self):
        r = self.c.get("/api/panel/export?which=trend&granularity=year&site=cac")
        cd = r.headers.get("content-disposition", "")
        self.assertIn("cacx_", cd)
        self.assertIn("cac", cd)

    def test_不支持的图表被拒(self):
        r = self.c.get("/api/panel/export?which=nope")
        self.assertEqual(r.status_code, 400)

    def test_中文文件名按RFC5987编码(self):
        """中文站名进 Content-Disposition 必须用 filename*=UTF-8''，
        否则部分浏览器会拿到乱码文件名。"""
        r = self.c.get("/api/panel/export?which=trend&granularity=year&site=cac")
        cd = r.headers.get("content-disposition", "")
        self.assertIn("filename*=UTF-8''", cd)

    def test_导出动作被记为事件(self):
        before = self.c.get("/api/events?days=30").json()["total"]
        self.c.get("/api/panel/export?which=trend&granularity=year")
        after = self.c.get("/api/events?days=30").json()["total"]
        self.assertEqual(after, before + 1)

    def test_导出内容有BOM供Excel识别中文(self):
        r = self.c.get("/api/panel/export?which=trend&granularity=year")
        self.assertTrue(r.content.startswith(b"\xef\xbb\xbf"),
                        "Excel 打开无 BOM 的 UTF-8 CSV 会乱码")


if __name__ == "__main__":
    unittest.main(verbosity=2)
