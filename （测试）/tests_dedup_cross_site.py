# -*- coding: utf-8 -*-
"""跨站重复入库的回归测试
================================================================
背景（2026-10-13 真实事故）：新增了一个通用采集器验证源 `zjwx-api`，
它抓的 10 篇文章 URL 与已有的 `zjwx` **完全相同**，
但 articles 表的唯一键是 (site, url) —— 所以库��默默多出 10 行。

后果不是「多了 10 篇」这么简单：
  1. 它们被一起抽了关系，凭空多出 62 条关系（8801 → 8863）；
     而关系总数是报告的核心数字之一。
  2. 覆盖率 27.9% 的分母也跟着动，看上去像「数据更新了」，
     实际增长全部来自重复。
  3. 事后要靠人工比对 URL 才能发现，因为站内去重是正常的、不会报错。

修法（两层）：
  - db.upsert_article 写库前先问 get_url_owner(url, exclude_site=site)；
  - collector/runner.py 在**抓正文之前**就早退，省掉带 3 次退避重试的请求。

跑法：py -3 tests_dedup_cross_site.py
反向验证：py -3 tests_dedup_cross_site.py --mutate
           （临时把守卫改坏，证明这套断言真的会红）
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx.db import Database                              # noqa: E402

MUTATE = "--mutate" in sys.argv
URL_A = "https://www.zjwx.gov.cn/col/col1673565/art/2026/art_abc.html"
URL_B = "https://www.cac.gov.cn/2026-10/13/c_999.html"


def rec(site, url, title="标题"):
    return {"site": site, "site_name": site, "column_code": "c",
            "column_name": "栏目", "title": title, "pub_date": "2026-10-13",
            "url": url, "source_text": "", "content_text": "正文内容",
            "content_html": "<p>正文内容</p>", "chars": 4,
            "content_empty": False}


class TestCrossSiteDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.d.close()
        self.tmp.cleanup()

    def n_rows(self, url):
        return self.d.conn.execute(
            "SELECT COUNT(*) FROM articles WHERE url=?", (url,)).fetchone()[0]

    # ---------------------------------------------------------- 核心
    def test_same_url_other_site_is_rejected(self):
        """同 URL 换站再采 —— 不能多出一行。"""
        self.assertTrue(self.d.upsert_article(rec("zjwx", URL_A)))
        self.assertFalse(self.d.upsert_article(rec("zjwx-api", URL_A)))
        self.assertEqual(self.n_rows(URL_A), 1,
                         "同一 URL 被两个站各存一行了")

    def test_rejected_write_is_reported_not_silent(self):
        """静默跳过最坏：用户以为这站没新东西。
        所以必须计数。"""
        self.d.upsert_article(rec("zjwx", URL_A))
        self.d.upsert_article(rec("zjwx-api", URL_A))
        self.assertEqual(self.d.dup_cross_site, 1)
        self.assertTrue(self.d.dup_cross_site_samples)

    def test_same_site_reupsert_still_idempotent(self):
        """老行为不能被破坏：同站重复导入要能修正字段，不是报错。"""
        self.assertTrue(self.d.upsert_article(rec("zjwx", URL_A)))
        self.assertFalse(self.d.upsert_article(
            rec("zjwx", URL_A, title="修正后的标题")))
        self.assertEqual(self.n_rows(URL_A), 1)
        row = self.d.conn.execute(
            "SELECT title FROM articles WHERE url=?", (URL_A,)).fetchone()
        self.assertEqual(row["title"], "修正后的标题")

    def test_different_url_other_site_is_allowed(self):
        """不能误伤：不同 URL 当然照收。"""
        self.assertTrue(self.d.upsert_article(rec("zjwx", URL_A)))
        self.assertTrue(self.d.upsert_article(rec("zjwx-api", URL_B)))
        self.assertEqual(self.n_rows(URL_A), 1)
        self.assertEqual(self.n_rows(URL_B), 1)

    def test_get_url_owner_excludes_own_site(self):
        self.d.upsert_article(rec("zjwx", URL_A))
        self.assertIsNone(self.d.get_url_owner(URL_A, exclude_site="zjwx"))
        self.assertIsNotNone(self.d.get_url_owner(URL_A, exclude_site="cac"))
        self.assertIsNone(self.d.get_url_owner("https://nope.example/x.html"))

    def test_runner_source_has_early_exit(self):
        """静态检查：runner 必须在**抓正文之前**判跨站重复。

        理由：正文抓取带 3 次退避重试，很贵。只在 upsert_article 里拦，
        网络请求照发不误 —— 功能对了，白烧的请求和限流风险还在。
        """
        src = (Path(__file__).resolve().parent.parent /
               "cacx" / "collector" / "runner.py").read_text(encoding="utf-8")
        i_guard = src.find("get_url_owner")
        i_fetch = src.find("fetch_article")
        self.assertNotEqual(i_guard, -1, "runner 里没有跨站去重判断")
        self.assertNotEqual(i_fetch, -1, "runner 结构变了，找不到 fetch_article")
        self.assertLess(i_guard, i_fetch,
                        "跨站判断在抓正文之后 —— 拦晚了，网络请求白烧")


if __name__ == "__main__":
    if MUTATE:
        # 反向验证：把 db.py 里的守卫临时拆掉，证明上面这些断言真的会红。
        #
        # ⚠ 第一版踩过的坑：用 unittest.main(exit=False) 在**当前进程**里跑，
        # 结果 0 个失败 —— 因为 Database 类早就 import 进内存了，
        # 改磁盘上的 .py 对已加载的类毫无影响。变异根本没生效，
        # "测试没红"是假阴性。**变异必须在子进程里跑**，重新 import 才算数。
        # 这跟「破坏被观测的事实」是同一条纪律的另一面：
        # 变异必须真的作用到被运行的那份代码上，否则测的是空气。
        import subprocess
        db_py = Path(__file__).resolve().parent.parent / "cacx" / "db.py"
        src = db_py.read_text(encoding="utf-8")
        anchor = '        owner = self.get_url_owner(rec.get("url", ""), exclude_site=site)'
        if anchor not in src:
            print("[MUTATE] 找不到守卫锚点，db.py 结构变了")
            sys.exit(2)
        db_py.write_text(src.replace(
            anchor, "        owner = None  # MUTATED", 1), encoding="utf-8")
        print("[MUTATE] 已拆掉 db.upsert_article 的跨站守卫，在**子进程**里跑…\n")
        try:
            p = subprocess.run(
                [sys.executable, "-m", "unittest", "-v",
                 "tests_dedup_cross_site"],
                cwd=str(Path(__file__).resolve().parent), capture_output=True,
                text=True, encoding="utf-8", errors="replace")
        finally:
            db_py.write_text(src, encoding="utf-8")
            print("\n[MUTATE] 已还原 db.py")
        tail = (p.stdout or "").strip().splitlines()[-4:]
        print("\n".join(tail))
        if p.returncode == 0:
            print("\n✗ 变异后测试仍然全绿 —— 这套测试抓不住这个 bug，不能用。")
            sys.exit(3)
        print(f"\n✓ 变异后子进程退出码 {p.returncode}（非 0），"
              f"测试确实抓得住这次回归。")
        sys.exit(0)
    unittest.main(verbosity=2)
