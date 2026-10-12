# -*- coding: utf-8 -*-
"""
新词挖掘的取样口径测试
================================================================
背景：界面上「语料篇数」就是个数字，看不出背后挑了哪些文章。
原来的实现是 `SELECT content_text ... LIMIT ?`（**没有 ORDER BY**），
SQLite 按 rowid 取 → 实际等于「取最早入库的 N 篇」。实测 600 篇：
  · 站点：只有 cac（网信办），浙江网信网一篇都没有
  · 年份：只有 2024 年，其余年份全丢
  · 字数：96 万 / 全库 3823 万 = 2.5%
用户以为在挖「网信领域」新词，实际只挖到一家站、一年的用词习惯。
而且无 ORDER BY 就不保证顺序，换查询计划/重建索引结果就变，不可复现。

铁律：
  ① 取样必须确定性（同参数 → 同结果），否则报告里的口径无法复述
  ② 必须覆盖全部站点与全部年份，否则结论只是子集，得在界面上说清
  ③ MCP 与网页**共用同一份取样实现**，不许各自写一遍 SELECT

跑法：py -3 tests_corpus_sample.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import db as D  # noqa: E402


def _mk():
    """造一个小库，站点与年份都有明显缺口 ——
    用真库跑这类断言太慢，而且真库数据会随抓取变化。"""
    tmp = Path(tempfile.mkdtemp(prefix="cacx_corp_"))
    d = D.open_db({"db": {"path": str(tmp / "t.db")}})
    with d._lock:
        rows = []
        # cac 多、zjwx 少；某一年只有 2 篇（极端层），某站某年 0 篇
        for site, year, n in [("cac", "2022", 30), ("cac", "2023", 25),
                              ("cac", "2024", 40), ("zjwx", "2023", 18),
                              ("zjwx", "2024", 12), ("zjwx", "2025", 9)]:
            for i in range(n):
                day = f"{year}-0{i % 9 + 1}"
                body = f"{site}{year} 第{i}篇 正文内容" * 30
                rows.append((site, "C1", f"{site}{year} 第{i}篇",
                             day, year, body, len(body)))
        d.conn.executemany(
            "INSERT INTO articles(site, column_name, title, pub_day, year,"
            " content_text, chars) VALUES(?,?,?,?,?,?,?)", rows)
        d.conn.commit()
    return d, tmp


class TestCorpusSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d, cls.tmp = _mk()

    @classmethod
    def tearDownClass(cls):
        cls.d.close()

    # ------------------------------------------------ 覆盖：这是本轮的核心
    def test_分层取样覆盖全部站点(self):
        s = self.d.sample_content_text(limit=120)
        self.assertEqual(s["cover_sites"], 2,
                         "分层取样漏了站点 —— 实测旧实现只取到 cac")

    def test_分层取样覆盖全部年份(self):
        s = self.d.sample_content_text(limit=120)
        self.assertEqual(s["cover_years"], 4,
                         "分层取样漏了年份 —— 实测旧实现只取到 2024 一年")

    def test_极小层的少量文章也被取到(self):
        """zjwx·2025 只有 9 篇，是最小的层。
        如果按「篇数最多的站优先」配额，它会被完全挤掉 ——
        于是「浙江网信办 2025 年的说法」在语料里彻底消失，且无人察觉。"""
        s = self.d.sample_content_text(limit=120)
        got = {(p["site"], p["year"]): p["n"] for p in s["picked"]}
        self.assertIn(("zjwx", "2025"), got,
                      "最小的层被挤掉了 —— 分层必须给每层保底")
        self.assertEqual(got[("zjwx", "2025")], 9)

    def test_取不够时上限被尊重(self):
        s = self.d.sample_content_text(limit=5000)
        self.assertEqual(s["n"], 134, "库里只有 134 篇，不该凭空多出")

    # ------------------------------------------------ 可复现
    def test_stratified同参数两次结果完全一致(self):
        a = self.d.sample_content_text(limit=100)["texts"]
        b = self.d.sample_content_text(limit=100)["texts"]
        self.assertEqual(a, b, "同一参数两次取样结果不同 → 口径无法复述")

    def test_recent是大limit的前缀(self):
        """recent 是纯 ORDER BY + LIMIT，天然构成前缀：
        把 60 改成 120，前面那 60 篇原样保留，只是往后接。
        所以两种口径的「可加性」不一样，测试要分开断言，不能混着写。"""
        a = self.d.sample_content_text(limit=60, strategy="recent")["texts"]
        b = self.d.sample_content_text(limit=120, strategy="recent")["texts"]
        self.assertEqual(b[:len(a)], a)

    def test_recent取的是时间上最新的一批(self):
        """recent 的语义是「按时间倒序取 N 篇」，**不是**「只覆盖一年」。
        篇数一多跨年是正常的（库里最小的年本来就只有 9 篇）。
        早先在这里写成断言 cover_years==1，是把测试写死了而不是测行为。"""
        s = self.d.sample_content_text(limit=40, strategy="recent")
        yrs = sorted({p["year"] for p in s["picked"]})
        self.assertEqual(yrs, ["2024", "2025"], "应当是最新的连续几年")
        a = self.d.sample_content_text(limit=12, strategy="recent")["texts"]
        # 直接和 SQL 期望值比，别靠「应该是几年」去猜 ——
        # 库里 zjwx·2025 有 9 篇、2024 有 12 篇，top12 自然跨两年，
        # 断言成「只覆盖一年」是我自己写错了，不是行为有问题。
        exp = [r[0] for r in self.d.conn.execute(
            "SELECT content_text FROM articles WHERE content_text IS NOT NULL"
            " AND length(content_text)>200"
            " ORDER BY pub_day DESC, id DESC LIMIT 12").fetchall()]
        self.assertEqual(a, exp, "recent 必须严格等于「按时间倒序取 N 篇」")

    def test_未知策略明确报错而不是静默取前N篇(self):
        with self.assertRaises(ValueError):
            self.d.sample_content_text(limit=50, strategy="随便写")

    # ------------------------------------------------ 口径清单（透明的前提）
    def test_picked清单能还原取样(self):
        s = self.d.sample_content_text(limit=120)
        self.assertEqual(sum(p["n"] for p in s["picked"]), s["n"],
                         "picked 各层相加必须等于实取篇数，否则面板上的"
                         "「每站每层多少篇」是在骗人")

    def test_底账给出分母(self):
        """没有 total，取样对用户就是黑箱：
        「600 篇」只有在「全库 27168 篇」旁边才有意义。"""
        p = self.d.corpus_profile()
        self.assertEqual(p["n"], 134)
        self.assertEqual(len(p["per_site"]), 2)
        self.assertEqual(len(p["per_year"]), 4)
        self.assertEqual(p["date_min"][:4], "2022")
        self.assertEqual(p["date_max"][:4], "2025")


class TestNoDuplicateSamplingLogic(unittest.TestCase):
    """MCP 与网页必须共用一份取样实现。"""

    def test_mcp不再自带SELECT_LIMIT(self):
        src = (Path(__file__).resolve().parent
               / "cacx" / "mcp_server.py").read_text(encoding="utf-8")
        self.assertNotIn("SELECT content_text FROM articles",
                         src,
                         "MCP 又自己写了一遍无 ORDER BY 的取样 —— "
                         "同一个 bug 的第二个副本，且与网页口径漂移")

    def test_网页接口不自带SELECT_LIMIT(self):
        src = (Path(__file__).resolve().parent
               / "cacx" / "web" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('"SELECT content_text FROM articles "',
                         src, "网页端也不该自己写取样 SQL")


if __name__ == "__main__":
    unittest.main(verbosity=2)