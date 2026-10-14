# -*- coding: utf-8 -*-
"""图谱检索高亮：**高亮 ≠ 筛选**

背景（备忘录 26.10.14 第 1 条）：图谱节点多，找一个机构只能靠眼睛扫。
最自然的做法是复用已有的「名称含」输入框 —— 但那是**筛选**：
它会把不匹配的节点从图上裁掉，连带指标（度、中心性）全部重算。
于是「搜不到」有两种完全相反的含义：

    · 该机构不存在         → 数据问题，要去查库
    · 该机构存在但被度门槛挡掉了 → 只是被裁了，放宽条件就有

用户分不清，于是只会反复调条件 —— 而这恰好是「搜不到」最该避免的事。

所以高亮必须是**独立的一路参数** `find`：
只打标、不裁剪、不动任何指标。图永远还是那张图。

这里断言的三条性质：
  ① 加不加 find，节点数与边数**一模一样**
  ② 边命中 = **两端都命中**（只点亮一端的话，搜泛词等于点亮整张图）
  ③ 导出 HTML 与屏幕同色 —— 否则「导出后高亮去哪了」
"""

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import networkx as nx

from cacx.graph_analysis import to_pyvis_nodes_edges

# 高亮色三处必须同值：后端常量 / 前端 JS 常量 / CSS 变量 --hl
HIT_HEX = "#ffd479"


def mk():
    """一张小图：甲网信办—乙网信办—丙宣传部—丁宣传部，另挂一个不相关的戊

    特意做出「命中节点只连着一个不命中节点」（甲—戊），
    用来验「边命中必须两端都命中」。
    """
    G = nx.Graph()
    G.add_edge("甲网信办", "乙网信办", weight=3)
    G.add_edge("乙网信办", "丙宣传部", weight=3)
    G.add_edge("丙宣传部", "丁宣传部", weight=3)
    G.add_edge("甲网信办", "戊", weight=3)
    return G


def draw(find=""):
    return to_pyvis_nodes_edges({}, mk(), min_degree=1, limit=300, find=find)


class TestFindHighlight(unittest.TestCase):
    def test_base_has_no_hit(self):
        """不传 find 时不得有任何 hit 标记 —— 基线被污染，后面全不可信"""
        r = draw("")
        self.assertEqual(r["find"], "")
        self.assertEqual(r["find_hits"], 0)
        self.assertFalse(any(n.get("hit") for n in r["nodes"]))
        self.assertFalse(any(e.get("hit") for e in r["edges"]))

    def test_size_unchanged_by_find(self):
        """① 高亮不得改变图规模 —— 这条一破，它就退化成筛选了"""
        a, b = draw(""), draw("网信")
        self.assertGreater(len(a["nodes"]), 0)
        self.assertEqual(len(a["nodes"]), len(b["nodes"]))
        self.assertEqual(len(a["edges"]), len(b["edges"]))
        self.assertEqual([n["id"] for n in a["nodes"]],
                         [n["id"] for n in b["nodes"]])

    def test_hit_marking(self):
        r = draw("网信")
        hits = {n["id"] for n in r["nodes"] if n.get("hit")}
        self.assertEqual(hits, {"甲网信办", "乙网信办"})
        self.assertEqual(r["find_hits"], 2)

    def test_edge_hit_requires_both_ends(self):
        """② 边命中 = 两端都命中。
        只点亮一端的话，「网信」这种泛词会点亮几乎所有边 = 等于没筛。"""
        r = draw("网信")
        eh = [e for e in r["edges"] if e.get("hit")]
        self.assertEqual(len(eh), 1)                      # 甲—乙
        self.assertEqual({e["from"] for e in eh} |
                         {e["to"] for e in eh}, {"甲网信办", "乙网信办"})
        self.assertEqual(r["find_edge_hits"], 1)
        # 甲—戊：甲命中、戊不命中 → 不得点亮
        cross = [e for e in r["edges"]
                 if {e["from"], e["to"]} == {"甲网信办", "戊"}]
        self.assertEqual(len(cross), 1)
        self.assertFalse(cross[0].get("hit"))

    def test_no_match(self):
        r = draw("查无此词")
        self.assertEqual(r["find_hits"], 0)
        self.assertEqual(r["find_edge_hits"], 0)
        self.assertFalse(any(n.get("hit") for n in r["nodes"]))


class TestExportParity(unittest.TestCase):
    """③ 导出 HTML 必须和屏幕上是同一个功能、同一个颜色

    ⚠ 这里**必须真调 pyvis_html**。
      早先写成「grep 源码里有没有那串颜色表达式」，结果反向验证当场抓出：
      把 `hit = bool(e.get("hit"))` 改成 `hit = False`，那串字面量纹丝不动，
      测试照样全绿 —— 而导出件已经彻底不标黄了。
      **grep 源码是快照，不是性质**：它只能证明「曾经写过这行」，
      证明不了「这行还在被使用」。
    """

    @classmethod
    def setUpClass(cls):
        from cacx.web.app import pyvis_html
        # ⚠ 必须 staticmethod：裸函数存进类属性会变成方法，
        #   调用时 self 被当成第一个位置参数（nodes），于是报
        #   「got multiple values for argument 'height'」——
        #   报错指向 height，真正的原因在绑定方式上，很容易查偏。
        cls.pyvis_html = staticmethod(pyvis_html)
        src = (ROOT / "cacx" / "web" / "app.py").read_text(encoding="utf-8")
        cls.src = src
        cls.html = (ROOT / "cacx" / "web" / "static" / "index.html").read_text(
            encoding="utf-8")
        m = re.search(r'^_GRAPH_HIT_COLOR\s*=\s*"(#[0-9a-fA-F]{6})"', src, re.M)
        cls.backend_hex = m.group(1) if m else None

    def _nodes(self, hit):
        return [{"id": "甲网信办", "label": "甲网信办", "group": "org",
                 "size": 16, "title": "", "hit": hit},
                {"id": "丙宣传部", "label": "丙宣传部", "group": "org",
                 "size": 16, "title": "", "hit": False}]

    def _edges(self, hit):
        return [{"from": "甲网信办", "to": "丙宣传部", "weight": 3.0,
                 "hit": hit}]

    def test_export_applies_hit_color(self):
        on = self.pyvis_html(self._nodes(True), self._edges(True), height="400px")
        self.assertIn(HIT_HEX, on, "命中时导出件里没有高亮色")

    def test_export_clean_when_no_hit(self):
        """反向的那一半：没有命中时**不能**有高亮色。
        少了这条，只把常量改成永远 True 的测试也能过。"""
        off = self.pyvis_html(self._nodes(False), self._edges(False),
                              height="400px")
        self.assertNotIn(HIT_HEX, off, "未命中却仍带高亮色 —— 底色被污染了")

    def test_backend_color_exists(self):
        self.assertIsNotNone(self.backend_hex,
                             "后端缺 _GRAPH_HIT_COLOR 常量")

    def test_three_sources_same_hex(self):
        """三处同色：后端常量 / 前端 GP_HIT_COLOR / CSS --hl"""
        self.assertEqual(self.backend_hex.lower(), HIT_HEX)
        m = re.search(r"GP_HIT_COLOR\s*=\s*'(#[0-9a-fA-F]{6})'", self.html)
        self.assertIsNotNone(m, "前端缺 GP_HIT_COLOR")
        self.assertEqual(m.group(1).lower(), HIT_HEX)
        self.assertRegex(self.html, r"--hl\s*:\s*#ffd479")

    def test_frontend_sends_find_separately(self):
        """find 必须**独立**于 keyword 发 —— 合并成一个参数就退化成筛选"""
        m = re.search(r"q\.set\('find',\s*fd\)", self.html)
        self.assertIsNotNone(m, "前端未把 find 传给后端")
        self.assertIn("q.set('keyword', k)", self.html)

    def test_find_cleared_on_reset(self):
        self.assertRegex(self.html, r"\$\('gp_find'\)\.value = ''")


if __name__ == "__main__":
    unittest.main(verbosity=2)