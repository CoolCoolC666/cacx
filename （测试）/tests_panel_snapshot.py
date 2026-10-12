# -*- coding: utf-8 -*-
"""
面板 HTML 快照导出。

「交作业」这个场景要的是一个**双击就能打开、断网也能看、不需要装任何东西**的
HTML 文件。所以这里最要紧的不是内容对不对，而是——
  · 零外部依赖（任何 CDN 外链都会让教室没网时只剩白页）
  · HTML 转义到位（少一个 & 转义，标题里一个「<」就能破版）
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 复用面板测试的临时库夹具 —— 铁律同样是：不碰真实 data/cacx.db
from tests_panel_export import Base                # noqa: E402


class TestSnapshot(Base):
    def setUp(self):
        super().setUp()
        self.r = self.c.get("/api/panel/snapshot?months=12")
        self.html = self.r.text

    # -------------------------------------------------- 基本
    def test_返回HTML而不是404(self):
        self.assertEqual(self.r.status_code, 200, self.r.text[:200])
        self.assertIn("text/html", self.r.headers["content-type"])

    def test_是完整HTML文档(self):
        self.assertTrue(self.html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", self.html)

    # -------------------------------------------------- ⚠ 零外部依赖
    def test_没有任何外部引用(self):
        """这是本功能成立的前提。一条 CDN 外链 = 教室没网就白屏。"""
        bad = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', self.html)
        self.assertEqual(bad, [], f"出现外部引用，断网会失效：{bad}")
        self.assertNotIn("//cdn", self.html)
        self.assertNotIn("<script", self.html, "不该有脚本，静态快照不需要")

    # -------------------------------------------------- 转义
    def test_栏目名被转义不会破版(self):
        """栏目名是会原样出现在页面上的（rank 列表 + 数据表），
        而栏目名来自源站，理论上什么字符都可能有。少一个转义就破版。"""
        self.d.conn.execute(
            "UPDATE articles SET column_name=? WHERE site='cac'",
            ('危<script>alert(1)</script> & "房"',))
        self.d.conn.commit()
        h = self.c.get("/api/panel/snapshot?site=cac").text
        self.assertNotIn("<script>alert", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("&amp;", h)
        self.assertIn("&quot;", h)

    # -------------------------------------------------- 图表与数据
    def test_有SVG图表(self):
        self.assertGreaterEqual(self.html.count("<svg"), 2, "至少两张 SVG 图")
        self.assertGreaterEqual(self.html.count("<rect"), 10, "柱子数量太少")

    def test_四块内容都在(self):
        for t in ("按月分布", "按年分布", "栏目 Top", "检索行为"):
            self.assertIn(t, self.html, f"缺少区块：{t}")

    def test_每张图都带数据表(self):
        self.assertGreaterEqual(self.html.count("<details"), 4)
        self.assertGreaterEqual(self.html.count("<th>"), 10)

    # ------------------------------------------- 三张作业相关的图
    def test_三张作业图都在导出里(self):
        """⚠ 必须与网页面板同步。用户拍板：面板 HTML 是要交上去的，
        屏幕上更新了而导出版还是旧图，答辩时两边数字对不上最难看。"""
        for t in ("治理问题分布", "高频机构", "联合发文类文章月度趋势"):
            self.assertIn(t, self.html, f"导出缺区块：{t}")

    def test_作业图带口径说明(self):
        """三张都是粗筛/机器挖掘，不写口径会被当成精确统计引用进报告。"""
        self.assertIn("关键词粗筛", self.html)
        self.assertIn("人工复核", self.html)
        self.assertIn("非精确统计", self.html)

    def test_机构需复核标记进导出(self):
        """needs_review 的 ⚠ 必须原样带到 HTML —— 这是给 reviewer 的信号，
        丢了就等于让可疑的归并结果冒充可信数据。"""
        self.d.conn.execute("DELETE FROM articles")
        self.d.conn.execute(
            "INSERT INTO articles(site,site_name,column_name,column_code,"
            "title,pub_date,pub_day,url,chars,content_text,year) "
            "VALUES('cac','中国网信网','网信要闻','c1',?,"
            "'2025-01-01','2025-01-01','http://x1',10,'正文','2025')",
            ("中央网信办 联合 公安部 部署专项行动 " * 12,))
        self.d.conn.commit()
        h = self.c.get("/api/panel/snapshot?months=12").text
        self.assertIn("需复核", h, "导出里没有复核列/标记")

    # -------------------------------------------------- 筛选
    def test_按站点导出(self):
        h = self.c.get("/api/panel/snapshot?site=cac").text
        # 标题与副标题里的站点名
        self.assertIn("<b>中国网信办</b>", h)
        # 栏目榜里只该有国信的栏目（浙· 开头的是浙江）
        self.assertNotIn("浙·", h)
        self.assertIn("国·", h)

    def test_按月数截断(self):
        h12 = self.c.get("/api/panel/snapshot?months=6").text
        h36 = self.c.get("/api/panel/snapshot?months=36").text
        self.assertLess(len(h12), len(h36), "6 个月应当比 36 个月短")

    # -------------------------------------------------- 交付属性
    def test_文件名带站点与时间(self):
        import urllib.parse
        cd = self.r.headers.get("Content-Disposition", "")
        self.assertIn("filename*=UTF-8''", cd)
        # 中文文件名是 URL 编码的（RFC5987），断言前必须先解码
        raw = cd.split("UTF-8''", 1)[1]
        name = urllib.parse.unquote(raw)
        self.assertIn("数据面板", name)
        self.assertIn("12月", name)
        self.assertTrue(name.endswith(".html"), name)

    def test_空数据时不出现未替换的占位符(self):
        """f-string 漏写一个 f，页面就会把 `{days}` 原样打出来。
        这条专门防这个 —— 它出现过一次。"""
        self.assertNotIn("{days}", self.html)
        self.assertNotIn("{", self.html.split("<style>")[0])

    def test_含使用声明(self):
        self.assertIn("著作权", self.html)
        self.assertIn("学习研究", self.html)

    def test_被记为一次导出行为(self):
        self.assertEqual(
            self.d.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='export' "
                "AND detail='panel:html'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)