# -*- coding: utf-8 -*-
"""
关系图谱的筛选
================================================================
背景：界面上明明**显示着**各关系类型占比（joint_issue 71%）却**筛不了**，
筛选器只有一个「最小度数」。加上筛选之后最容易犯的错有三个，
本文件把它们都钉住：
  ① 关系类型必须**在建图阶段**过滤 —— 事后挑边的话权重是两种关系
     共现强度之和，「只看 issue」时那个权重并不是 issue 的强度
  ② 介数/PageRank 必须在**裁剪后的子图**上重算 —— 沿用全图指标等于
     让用户拿两个口径的数字比大小
  ③ 导出 HTML 的筛选参数必须与页面**完全对齐** —— 否则「屏幕看到的」
     和「导出的」不是同一张图

跑法：py -3 tests_graph_filter.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import graph_analysis as GA  # noqa: E402


def rel(src, dst, rt, w=1.0):
    return {"src_name": src, "dst_name": dst, "rel_type": rt, "weight": w}


# A-B 既有 joint_issue(权重 1) 又有 issue(权重 9)。
# 若先建全图再挑边，A-B 的权重会变成 10 —— 那是两种关系混在一起的值，
# 「只看 issue」时就不该是 10。这组数据专门用来抓这个错误。
SAMPLE = [
    rel("A", "B", "joint_issue", 1.0),
    rel("A", "B", "issue", 9.0),
    rel("B", "C", "joint_issue", 2.0),
    rel("C", "D", "issue", 3.0),
    rel("D", "A", "target", 1.0),
    rel("E", "F", "issue", 5.0),      # 独立连通块
]


class TestRelTypeFilter(unittest.TestCase):
    def test_只看一种类型时权重不被其他类型污染(self):
        G, types = GA.build_graph(SAMPLE, rel_types={"issue"})
        self.assertIn(("A", "B"), G.edges(), "A-B 有 issue 关系，不该被滤掉")
        self.assertAlmostEqual(G["A"]["B"]["weight"], 9.0, places=6,
            msg="A-B 的权重被 joint_issue 的 1.0 累加了 —— "
                "说明类型过滤发生得太晚（建完图才挑边）")

    def test_不指定类型时权重照常累加(self):
        G, _ = GA.build_graph(SAMPLE)
        self.assertAlmostEqual(G["A"]["B"]["weight"], 10.0, places=6)

    def test_过滤后节点数变少(self):
        G, _ = GA.build_graph(SAMPLE)
        Gt, _ = GA.build_graph(SAMPLE, rel_types={"target"})
        self.assertLess(Gt.number_of_nodes(), G.number_of_nodes())
        self.assertEqual(Gt.number_of_edges(), 1)

    def test_空的类型集合等于不过滤(self):
        """前端「没勾任何类型」传的是空串 → None。
        若传空集合却被当成「什么都不要」，整张图会空掉。"""
        G1, _ = GA.build_graph(SAMPLE)
        G2, _ = GA.build_graph(SAMPLE, rel_types=set())
        G3, _ = GA.build_graph(SAMPLE, rel_types=None)
        self.assertEqual(G1.number_of_nodes(), G2.number_of_nodes())
        self.assertEqual(G1.number_of_nodes(), G3.number_of_nodes())

    def test_类型名写错不会静默画空图(self):
        """不存在的类型名必须表现为「筛完为空」+ 有据可查，
        不能回退成全图 —— 回退等于用户以为筛了其实没筛。"""
        G, _ = GA.build_graph(SAMPLE, rel_types={"不存在的类型"})
        self.assertEqual(G.number_of_edges(), 0)


class TestDegreeAndScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.G, cls.types = GA.build_graph(SAMPLE)
        cls.result = GA.analyze(cls.G, cls.types, topn=10)

    def _ne(self, **kw):
        return GA.to_pyvis_nodes_edges(self.result, self.G, self.types,
                                       limit=50, **kw)

    def test_最小度数生效(self):
        ne = self._ne(min_degree=2)
        for n in ne["nodes"]:
            self.assertGreaterEqual(n["degree"], 2)

    def test_最大度数生效(self):
        ne = self._ne(min_degree=1, max_degree=2)
        for n in ne["nodes"]:
            self.assertLessEqual(n["degree"], 2)
        self.assertTrue(ne["nodes"], "上限不该把节点全筛没")

    def test_最大度数为0表示不限(self):
        a = self._ne(min_degree=1, max_degree=0)
        b = self._ne(min_degree=1)
        self.assertEqual(len(a["nodes"]), len(b["nodes"]))

    def test_关键词只留名称含它的节点(self):
        """⚠ 关键词是**纯子集筛选**：不匹配的节点被移除，它们的连边自然也没了。
          所以用它去找「中央网信办和谁协作」是找不到协作方的 ——
          那是「聚焦」那个控件的活。想保留协作方请用 focus。
          这里用中文机构名构造，否则单字母样本会把节点全滤成孤立点
          （孤立点度数 0，被 min_degree 顺带筛掉，测出来是「一个都不剩」，
          看着像 bug 其实不是）。"""
        rows = [rel("中央网信办", "国家网信办", "issue", 2.0),
                rel("中央网信办", "公安部", "joint_issue", 1.0),
                rel("国家网信办", "公安部", "issue", 1.0),
                rel("工业和信息化部", "公安部", "issue", 3.0)]
        G, ty = GA.build_graph(rows)
        res = GA.analyze(G, ty, topn=10)
        ne = GA.to_pyvis_nodes_edges(res, G, ty, min_degree=1, limit=50,
                                     keyword="网信")
        ids = {n["id"] for n in ne["nodes"]}
        self.assertEqual(ids, {"中央网信办", "国家网信办"})
        self.assertIn("网信", ne["scope_note"])

    def test_关键词为空等于不限(self):
        a = self._ne(min_degree=1, keyword="")
        b = self._ne(min_degree=1)
        self.assertEqual(len(a["nodes"]), len(b["nodes"]))


class TestFocusNeighborhood(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.G, cls.types = GA.build_graph(SAMPLE)
        cls.result = GA.analyze(cls.G, cls.types, topn=10)

    def _ne(self, **kw):
        kw.setdefault("min_degree", 1)
        return GA.to_pyvis_nodes_edges(self.result, self.G, self.types,
                                       limit=50, **kw)

    def test_1跳邻域只含自己和邻居(self):
        ne = self._ne(min_degree=1, focus="B", hop=1)
        ids = {n["id"] for n in ne["nodes"]}
        # B 的邻居是 A、C；E/F 在另一个连通块里
        self.assertEqual(ids, {"A", "B", "C"})

    def test_2跳能到更远(self):
        ne = self._ne(min_degree=1, focus="B", hop=2)
        ids = {n["id"] for n in ne["nodes"]}
        self.assertIn("D", ids, "2 跳应该够到 D")

    def test_边也被限制在邻域内(self):
        ne = self._ne(min_degree=1, focus="B", hop=1)
        ends = set()
        for e in ne["edges"]:
            ends.add(e["from"]); ends.add(e["to"])
        self.assertTrue(ends <= {"A", "B", "C"},
                        f"出现了邻域外的边端点：{ends}")

    def test_介数在子图上重算而非沿用全图(self):
        """B 在全图 {A,B,C,D} 里只占 0.05（E/F 是另一个连通块，拉低了归一化分母）；
        聚焦 B 的 1 跳邻域 {A,B,C} 后，B 成了 A↔C 的唯一中介 → 1.0。
        两者必须不同。若界面沿用全图介数，用户就会拿两个口径的数字比大小。"""
        import networkx as nx

        full = self._ne(min_degree=1)
        full_btw = {n["id"]: n["betweenness"] for n in full["nodes"]}
        sub = self._ne(min_degree=1, focus="B", hop=1)
        sub_btw = {n["id"]: n["betweenness"] for n in sub["nodes"]}
        self.assertEqual(set(sub_btw), {"A", "B", "C"})

        # 交叉验证：与直接在子图上跑 networkx 的结果对齐，
        # 而不是只断言「不等于全图」（那也可能是算错了，只是错得不一样）。
        H = self.G.subgraph(["A", "B", "C"])
        ref = nx.betweenness_centrality(H)
        for w, v in sub_btw.items():
            self.assertAlmostEqual(v, ref[w], places=6,
                msg=f"「{w}」的介数与在子图上独立算的结果不一致")
            self.assertNotAlmostEqual(
                v, full_btw[w], places=6,
                msg=f"「{w}」的介数与全图完全相同 → 子图重算没生效")

    def test_焦点不存在时给出说明而不是静默画全图(self):
        ne = self._ne(min_degree=1, focus="查无此机构")
        self.assertEqual(len(ne["nodes"]), 0,
                         "焦点不在图里却画出了节点 —— 等于骗用户说筛生效了")
        self.assertIn("查无此机构", ne["scope_note"],
                      "必须说清为什么是空的")


class TestVisAlignsWithScreen(unittest.TestCase):
    """导出的图必须和页面上看到的一致（用户铁律：屏幕看到的 = 导出的）。"""

    @classmethod
    def setUpClass(cls):
        src = Path(__file__).resolve().parent.parent / "cacx" / "web" / "app.py"
        cls.src = src.read_text(encoding="utf-8")

    def test_vis接口的筛选参数与分析接口一致(self):
        import inspect
        import importlib
        A = importlib.import_module("cacx.web.app")
        vis = {p for p in inspect.signature(A.api_graph_vis).parameters}
        ana = {p for p in inspect.signature(A.api_graph_analysis).parameters}
        # 分析接口另有 min_weight/topn，vis 不需要
        must = {"min_degree", "limit", "max_degree", "rel_types",
                "keyword", "focus", "hop"}
        missing = must - vis
        self.assertEqual(missing, set(),
                         f"/api/graph/vis 缺筛选参数 {missing} —— "
                         f"页面筛完导出却是全图，交付物与演示对不上")
        self.assertTrue(must <= ana, f"分析接口缺 {must - ana}")

    def test_前端导出带上当前筛选(self):
        html = (Path(__file__).resolve().parent.parent
                / "cacx" / "web" / "static" / "index.html"
                ).read_text(encoding="utf-8")
        i = html.find("async function dlGraphHtml")
        self.assertGreater(i, 0, "找不到 dlGraphHtml")
        seg = html[i:i + 900]
        self.assertIn("gpQuery()", seg,
                      "导出没有带上当前筛选条件")
        self.assertNotIn("gp_mindeg')?.value", seg,
                         "导出还在自己读最小度数，与筛选条件脱节了")


if __name__ == "__main__":
    unittest.main(verbosity=2)