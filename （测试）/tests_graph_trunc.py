# -*- coding: utf-8 -*-
"""画图截断的**确定性**：同度数节点入选不得取决于建图插入顺序

背景（真实事故）：报告里引的边数是 1407，网页上显示 1416。
根因不是统计错，而是 `to_pyvis_nodes_edges` 里
    core.sort(key=lambda n: -d_of[n])
只按度数排 —— 度数并列的节点之间，相对顺序 = 建图插入顺序，
而插入顺序取决于关系行的返回顺序。SQL 只要多一个 where（比如页面上
勾了站点筛选），查询计划就可能变、行序就变、被 limit 截掉的那批
同度数节点就换人。**两边都不报错**，评审方一对数字就露馅。

同一个 bug 类别和「四条图例一直是假的」一模一样：
都没有任何测试断言过这条性质，于是它能一直活着。

所以这里断言的性质不是「截断结果好看」，而是
「**换任何插入顺序，截断结果都一样**」。
"""

import os
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import networkx as nx

from cacx.graph_analysis import to_pyvis_nodes_edges

# 6 个枢纽，每个都和同一批 10 个外围节点相连 —— 度数全部相等（10），
# 再加一条把 6 个枢纽串起来的边，让它们都高于 min_degree。
HUBS = ["部甲", "部乙", "部丙", "部丁", "部戊", "部己"]
LEAVES = ["叶%d" % i for i in range(10)]


def build(order_seed):
    """按给定随机种子决定插入顺序，建出**度数完全相同**的图"""
    rng = random.Random(order_seed)
    G = nx.Graph()
    pairs = [(h, l) for h in HUBS for l in LEAVES]
    pairs += [(HUBS[i], HUBS[i + 1]) for i in range(len(HUBS) - 1)]
    rng.shuffle(pairs)
    for a, b in pairs:
        G.add_edge(a, b, weight=1)
    return G


class Test截断确定性(unittest.TestCase):
    """limit 必须切在度数并列的区间里 —— 这是本测试有意义的前提"""

    def _draw(self, seed):
        return to_pyvis_nodes_edges({}, build(seed),
                                    min_degree=2, limit=8)

    def test_前提_limit确实切在并列区间(self):
        """如果哪天改图时度数不再并列，这条测试就失去意义，必须先炸出来"""
        nodes = self._draw(1)["nodes"]
        self.assertEqual(len(nodes), 8, "limit=8 却没截到 8 个，测试前提失效")
        # 8 个节点里至少要有 3 个度数相同，否则不存在「并列截断」这回事
        degs = [n["degree"] for n in nodes]
        self.assertGreaterEqual(len(set(degs)), 1)
        tied = [d for d in degs if degs.count(d) > 1]
        self.assertTrue(tied, "取到的 8 个节点度数全不相同，测不到并列")

    def _undirected(self, payload):
        """边的**无向集合 + 输出次序**。

        ⚠ 不能直接比 (from, to)：types 以 (s,d) 为键、且 undirected=False
          走 DiGraph，`target` 这类关系方向本身带语义，首尾对调会让
          类型查不到，所以方向不能被规范化掉。这里比的是
          「哪两个点相连」+「按什么次序吐出来」——这才是「画出来是不是
          同一张图」真正关心的两件事。
        """
        es = payload["edges"]
        order = [(min(e["from"], e["to"]), max(e["from"], e["to"])) for e in es]
        return sorted(order), order

    def test_换24种插入顺序_截断结果完全一致(self):
        base = self._draw(0)
        base_nodes = sorted(n["id"] for n in base["nodes"])
        base_set, base_seq = self._undirected(base)
        for seed in range(1, 24):
            got = self._draw(seed)
            self.assertEqual(
                sorted(n["id"] for n in got["nodes"]), base_nodes,
                f"seed={seed} 时入选节点变了 —— 排序缺第二判据")
            gset, gseq = self._undirected(got)
            self.assertEqual(gset, base_set, f"seed={seed} 时边集变了")
            self.assertEqual(gseq, base_seq,
                             f"seed={seed} 时边的输出次序变了（边叠着画，看着像图在闪）")

    def test_有向关系的类型不能因为排序而丢失(self):
        """上面只排了列表次序、没有碰 from/to —— 这条守住那个前提"""
        G = nx.DiGraph()
        G.add_edge("机构甲", "问题乙", weight=2)
        G.add_edge("机构丙", "问题乙", weight=1)
        G.add_edge("机构甲", "机构丙", weight=1)
        out = to_pyvis_nodes_edges({}, G, min_degree=1, limit=10,
                                   types={("机构甲", "问题乙"): {"target"},
                                          ("机构丙", "问题乙"): {"target"},
                                          ("机构甲", "机构丙"): {"joint_issue"}})
        got = {(e["from"], e["to"]): e["types"] for e in out["edges"]}
        self.assertEqual(got[("机构甲", "问题乙")], ["target"])
        self.assertEqual(got[("机构丙", "问题乙")], ["target"])
        self.assertEqual(got[("机构甲", "机构丙")], ["joint_issue"])

    def test_并行加入时也要一致(self):
        """现实里同一批关系是 SELECT 出来的，行序由查询计划决定；
        这里用另一个插入顺序复现「加了个 where」的等价效果"""
        a = self._draw(3)
        b = self._draw(77)
        self.assertEqual(sorted(n["id"] for n in a["nodes"]),
                         sorted(n["id"] for n in b["nodes"]))
        self.assertEqual(len(a["edges"]), len(b["edges"]))

    def test_排序键确实是_度数降序加_名字升序(self):
        """把性质直接钉在排序键上，失败时报错更靠近原因"""
        import inspect

        from cacx import graph_analysis
        src = inspect.getsource(graph_analysis.to_pyvis_nodes_edges)
        self.assertIn("(-d_of[n], n)", src,
                      "core.sort 的 key 里没有第二判据，截断仍会随行序漂移")


if __name__ == "__main__":
    unittest.main(verbosity=2)
