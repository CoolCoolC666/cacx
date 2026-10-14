# -*- coding: utf-8 -*-
"""措施类（低置信门槛整类出局）：默认不勾 / 勾了放行。

背景：`measure` 实体的 confidence 上限恰好等于低置信门槛（0.8），
而踢出规则是 `<= 0.8`，于是这一类在默认口径下**100% 出局** ——
图例恒为 0、复选框还被判成 disabled，用户想看也点不开。

本文件钉住用户拍板的语义：
  **默认不勾**（默认图口径与报告都建立在「措施被挡」这套口径上）
  **勾了放行**（不能是假开关）
"""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
A = importlib.import_module("cacx.web.app")
GA = importlib.import_module("cacx.graph_analysis")

HTML = (Path(__file__).resolve().parent.parent
        / "cacx" / "web" / "static" / "index.html").read_text(encoding="utf-8")


class TestMeasureGatedDefault(unittest.TestCase):
    """默认口径：措施被挡，图例为 0。**这些数字是报告引的，不能变**。"""

    def test_默认不传node_types时措施被挡(self):
        d = A._graph_payload(min_degree=2, limit=300, min_weight=1.0,
                             topn=12, max_degree=0, rel_types="",
                             keyword="", focus="", hop=1)
        avail = {x["group"]: x["n"] for x in d["graph"]["avail_by_group"]}
        self.assertEqual(avail["measure"], 0,
                         "默认口径下措施必须为 0 —— 报告与基线都按这个口径")
        self.assertGreater(d["blocked_count"], 0)
        self.assertEqual(d["blocked_kept_out"], 0,
                         "默认时不该有放行")
        self.assertEqual(d["graph"]["scope_nodes"], 1633)
        self.assertEqual(d["graph"]["scope_edges"], 3921)

    def test_默认画布基线不变(self):
        d = A._graph_payload(min_degree=2, limit=300, min_weight=1.0,
                             topn=12, max_degree=0, rel_types="",
                             keyword="", focus="", hop=1)
        self.assertEqual(len(d["graph"]["nodes"]), 300)
        self.assertEqual(len(d["graph"]["edges"]), 1410)

    def test_blocked按类型报出(self):
        """只给总数答不了用户的问题 —— 必须点名是措施被挡。"""
        d = A._graph_payload(min_degree=2, limit=300, min_weight=1.0,
                             topn=12, max_degree=0, rel_types="",
                             keyword="", focus="", hop=1)
        self.assertIn("measure", d["blocked_by_type"])
        self.assertGreater(d["blocked_by_type"]["measure"], 0)


class TestMeasureGatedOptIn(unittest.TestCase):
    """勾选措施 → 解除过滤 → 措施与 adopt 进图。"""

    def _with(self, node_types):
        return A._graph_payload(min_degree=2, limit=300, min_weight=1.0,
                                topn=12, max_degree=0, rel_types="",
                                keyword="", focus="", hop=1,
                                node_types=node_types)

    def test_勾选措施后进图(self):
        d = self._with("org,policy,problem,measure")
        avail = {x["group"]: x["n"] for x in d["graph"]["avail_by_group"]}
        self.assertGreater(avail["measure"], 0, "勾了措施它就该可用")
        self.assertGreater(d["blocked_kept_out"], 0,
                           "必须说清放行了多少")
        self.assertEqual(d["blocked_count"],
                         d["blocked_by_type"]["measure"])

    def test_勾选后图规模变大(self):
        base = self._with("")
        on = self._with("org,policy,problem,measure")
        self.assertGreater(on["graph"]["scope_edges"],
                           base["graph"]["scope_edges"],
                           "adopt 边应该回来了")
        self.assertGreater(on["graph"]["scope_nodes"],
                           base["graph"]["scope_nodes"])

    def test_adopt边真的进图(self):
        d = self._with("org,policy,problem,measure")
        n = sum(1 for e in d["graph"]["edges"] if "adopt" in (e.get("types") or []))
        self.assertGreater(n, 0, "勾了措施就该看得到 adopt 边")

    def test_只勾措施不含机构时得到空图(self):
        """关系全是 org→X 单向：只勾措施 → 机构被裁掉 → 措施节点失去全部邻居、
        度归零 → 被 min_degree 滤光 → 空图。这与「只勾政策」是同一既有语义，
        前端本来就弹窗提示「请同时勾选机构」。此处钉住它不会悄悄变样。"""
        d = self._with("measure")
        self.assertEqual(len(d["graph"]["nodes"]), 0)
        self.assertEqual(len(d["graph"]["edges"]), 0)

    def test_未勾措施时放行数为零(self):
        """只勾其它三类 —— 措施仍在门槛外。"""
        d = self._with("org,policy,problem")
        avail = {x["group"]: x["n"] for x in d["graph"]["avail_by_group"]}
        self.assertEqual(avail["measure"], 0)
        self.assertEqual(d["blocked_kept_out"], 0)


class TestBlockedTypesHelper(unittest.TestCase):
    def test_allow_types只放行指定类(self):
        class _D:
            def list_entities(self, type="", limit=None):
                return [{"name": "下架", "type": "measure", "confidence": 0.7}]

        self.assertEqual(len(A._blocked_for_graph(_D())), 1)
        self.assertEqual(
            len(A._blocked_for_graph(_D(), allow_types={"measure"})), 0,
            "勾了措施就该解除对它的过滤")
        self.assertEqual(
            len(A._blocked_for_graph(_D(), allow_types={"org"})), 1,
            "勾机构不该顺带放行措施")


class TestFrontendOptIn(unittest.TestCase):
    """前端：可勾、默认不勾、勾了要真传参。"""

    def test_措施复选框不得禁用(self):
        self.assertIn("const off = avail[k] === 0 && bkd === 0;", HTML,
                      "avail=0 但被门槛挡过 ≠ 不存在，不能禁用")

    def test_默认勾选排除被门槛挡的类(self):
        self.assertIn("function gpDefaultTypes()", HTML)
        self.assertIn("GP_LEGEND_ON = new Set(gpDefaultTypes())", HTML)

    def test_初始为null等后端告知(self):
        """「哪些类被挡着」要等后端响应 —— 初始瞎猜会把措施勾上、默认口径就变了。"""
        self.assertIn("let GP_LEGEND_ON = null;", HTML)

    def test_全勾时也必须传node_types(self):
        """⚠ 这是踩过的坑：全勾时旧逻辑不传 node_types，
        后端把空读成「什么都没勾」→ 措施照旧被挡 → 勾选成了假开关。"""
        self.assertIn("needsGatedType", HTML)
        self.assertIn("(lgOn.length < lgAll.length || needsGatedType)", HTML)

    def test_说明不得给无效操作(self):
        """实测调小最小度数并不会让措施变多（画布 300 上限截断）——
        说过头就是假提示，比不说更糟。"""
        self.assertNotIn("想看全可把", HTML)
        self.assertIn("调小「最小度数」不会让它们变多", HTML)

    def test_放行文案要写明画布只显示一部分(self):
        self.assertIn("图上只看得到一部分", HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
