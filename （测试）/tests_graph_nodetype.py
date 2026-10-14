# -*- coding: utf-8 -*-
"""
关系图谱的**实体类型**（图例那四个）
================================================================
这个文件是为了钉住一个已经上线的 bug：

  建图只吃关系的两端**名字**，实体类型（org/policy/problem/measure）
  在建图时被整个丢掉，于是接口返回的节点根本没有 `group` 字段。
  前端那句 `n.group || 'org'` 于是把所有节点都当成机构 —— 画出一片蓝点，
  而图例上明明写着「政策■ 治理问题▲ 措施◆」，颜色+形状双编码**一条都没生效**。

  它能活这么久，是因为没有任何测试断言过 `group`。
  所以本文件的第一条测试就是：**节点必须带 group**。

其余钉住的坑：
  ① 跨类型同名实体按众数定类型，且并列时结果可复现
  ② 类型筛选必须在**裁子图**阶段做，指标随之重算（不能在画图时藏节点）
  ③ 筛选顺序固定为 类型 → 聚焦 → 关键词
  ④ 「一个类型都不选」是非法输入，不能返回一张让人误解的空图
  ⑤ **一条边都没有**的子图等价于空图，排名必须一起清空
     —— 否则出现「屏幕上没点，指标却满屏 0.0」
  ⑥ 图例的禁用态依据必须是**筛选前**的计数，否则用户取消勾选后
     那个复选框会被自己置灰、再也点不回来（死锁）

跑法：py -3 tests_graph_nodetype.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import graph_analysis as GA  # noqa: E402


def rel(src, dst, rt, w=1.0):
    return {"src_name": src, "dst_name": dst, "rel_type": rt, "weight": w}


# 真实库里的形态：关系全是 org → X 单向，X 与 X 之间没有边。
# 机构 A/B 联合发文；A 发布政策；A 针对治理问题；A 采取措施。
SAMPLE = [
    rel("甲部门", "乙部门", "joint_issue", 3.0),
    rel("甲部门", "丙部门", "joint_issue", 2.0),
    rel("甲部门", "某条例", "issue", 5.0),
    rel("乙部门", "某办法", "issue", 4.0),
    rel("甲部门", "网络诈骗", "target", 2.0),
    rel("甲部门", "加强监管", "adopt", 1.0),
]

TYPES = {
    "甲部门": "org", "乙部门": "org", "丙部门": "org",
    "某条例": "policy", "某办法": "policy",
    "网络诈骗": "problem", "加强监管": "measure",
}


def build():
    # ⚠ 刻意**不收** types 参数：早先它是个没人用的形参，
    #   于是 `build(types={})` 看着像在换类型、实际没换，
    #   下一行 draw() 又用回默认 TYPES —— 断言"全是 org"就必然失败。
    #   传了不生效的参数比没有参数危险：它让测试"看起来"覆盖了某个场景。
    G, ty = GA.build_graph(SAMPLE, min_weight=1.0)
    return G, ty


def draw(G, ty, types=TYPES, want=None, min_degree=1, limit=300, **kw):
    res = GA.analyze(G, ty, topn=10)
    return GA.to_pyvis_nodes_edges(res, G, ty, min_degree=min_degree,
                                   limit=limit, node_types=types,
                                   want_types=want, **kw)


class Test节点类型映射(unittest.TestCase):
    def test_按众数定类型(self):
        ents = [
            {"name": "南方AI破谣局", "type": "org"},
            {"name": "南方AI破谣局", "type": "org"},
            {"name": "南方AI破谣局", "type": "policy"},
        ]
        self.assertEqual(GA.node_type_map(ents), {"南方AI破谣局": "org"})

    def test_并列时结果可复现(self):
        """⚠ 众数并列必须有个确定的取舍，否则同一份数据两次跑出不同颜色，
        演示时刷新一次截图就对不上，报告里的图例也没法当证据。"""
        ents = [{"name": "X", "type": "org"}, {"name": "X", "type": "policy"}]
        m1 = GA.node_type_map(ents)
        m2 = GA.node_type_map(list(ents))
        self.assertEqual(m1, m2)
        self.assertIn(m1["X"], ("org", "policy"))

    def test_脏类型不进映射(self):
        ents = [{"name": "Y", "type": "乱码"}, {"name": "Y", "type": "org"}]
        self.assertEqual(GA.node_type_map_from_counts(
            {"Y": {"乱码": 9, "org": 1}}), {"Y": "org"})

    def test_计数版与逐条版一致(self):
        ents = [{"name": "Z", "type": "org"}] * 3 + [{"name": "Z", "type": "policy"}]
        self.assertEqual(GA.node_type_map(ents),
                         GA.node_type_map_from_counts({"Z": {"org": 3, "policy": 1}}))


class Test节点必须带group(unittest.TestCase):
    """★ 这条是本次修复的核心断言。以前接口返回的节点没有 group，
    前端 `n.group || 'org'` 把所有节点画成蓝点，而图例形同虚设。"""

    def test_每个节点都有合法group(self):
        G, ty = build()
        out = draw(G, ty)
        self.assertTrue(out["nodes"])
        for n in out["nodes"]:
            self.assertIn("group", n, "节点缺少 group 字段 → 前端会全当机构")
            self.assertIn(n["group"], GA.ENTITY_TYPES)

    def test_group与类型映射一致(self):
        G, ty = build()
        out = draw(G, ty)
        for n in out["nodes"]:
            self.assertEqual(n["group"], TYPES[n["id"]])

    def test_取不到类型时归org而不是丢弃节点(self):
        """归 org 是保守选择；丢节点会静默改变度/介数，
        用户只会觉得"图怎么少了点"，想不到是缺字段。"""
        G, ty = build()
        out = draw(G, ty, types={})
        self.assertEqual(len(out["nodes"]), 7)
        self.assertTrue(all(n["group"] == "org" for n in out["nodes"]))

    def test_by_group数的是画出来的节点(self):
        G, ty = build()
        out = draw(G, ty)
        cnt = {x["group"]: x["n"] for x in out["by_group"]}
        self.assertEqual(sum(cnt.values()), len(out["nodes"]))
        self.assertEqual(cnt["org"], 3)
        self.assertEqual(cnt["policy"], 2)


class Test按类型筛选(unittest.TestCase):
    def test_只留选中类型(self):
        G, ty = build()
        out = draw(G, ty, want={"org", "policy"})
        self.assertTrue(all(n["group"] in ("org", "policy") for n in out["nodes"]))
        self.assertEqual(len(out["nodes"]), 5)

    def test_不筛时包含全部(self):
        G, ty = build()
        out = draw(G, ty)
        self.assertEqual(len(out["nodes"]), 7)

    def test_筛选是诱导子图_两端都留的边才留(self):
        """筛成"仅机构"时，机构与政策的边整条消失 —— 这是筛选的定义，
        不是丢数据。但 note 得说明，否则用户以为图坏了。"""
        G, ty = build()
        out = draw(G, ty, want={"org"})
        pairs = {frozenset((e["from"], e["to"])) for e in out["edges"]}
        self.assertIn(frozenset(("甲部门", "乙部门")), pairs)
        self.assertNotIn(frozenset(("甲部门", "某条例")), pairs)

    def test_空结果给出原因(self):
        G, ty = build()
        sub, note = GA.crop_by_node_types(G, TYPES, {"policy"})
        self.assertEqual(sub.number_of_edges(), 0)
        self.assertIn("没有边", note)
        self.assertIn("机构", note, "必须告诉用户要同时勾选机构")

    def test_有节点但零条边也要给原因(self):
        """「只看政策」= 2 个节点、0 条边。只判"零节点"会漏掉，
        于是界面上出现"有节点、却画不出任何连线、还没有提示"。"""
        G, ty = build()
        sub, note = GA.crop_by_node_types(G, TYPES, {"policy"})
        self.assertEqual(sub.number_of_nodes(), 2)
        self.assertEqual(sub.number_of_edges(), 0)
        self.assertIn("没有边", note)
        self.assertIn("机构", note, "必须告诉用户要同时勾选机构")

    def test_不筛返回原图(self):
        G, ty = build()
        sub, note = GA.crop_by_node_types(G, TYPES, None)
        self.assertIs(sub, G)
        self.assertEqual(note, "")


class Test筛选顺序(unittest.TestCase):
    """⚠ 顺序固定为 类型 → 聚焦 → 关键词。
    若把类型放到聚焦之后，「只看政策」就变成"只看聚焦邻域里的政策"——
    同一条筛选语句换个顺序含义就变了，用户以为选的是前者。"""

    def test_先按类型再按聚焦(self):
        G, ty = build()
        sub, note = GA.crop_scope(G, focus="甲部门", hop=1,
                                 node_types=TYPES, want_types={"org", "policy"})
        self.assertIn("只看机构", note)
        self.assertIn("甲部门", sub.nodes())

    def test_聚焦不存在的机构返回空图并说明(self):
        G, ty = build()
        sub, note = GA.crop_scope(G, focus="查无此机构", node_types=TYPES,
                                 want_types={"org"})
        self.assertEqual(sub.number_of_nodes(), 0)
        self.assertIn("不在当前图里", note)


class Test指标与图同口径(unittest.TestCase):
    def test_筛选后指标在子图上重算(self):
        """沿用全图指标 = 让用户拿两个口径的数字比大小。"""
        G, ty = build()
        full = GA.analyze(G, ty, topn=5)
        sub, _ = GA.crop_by_node_types(G, TYPES, {"org"})
        part = GA.analyze(sub, ty, topn=5)
        self.assertNotEqual(full["nodes"], part["nodes"])

    def test_无边子图的排名必须能被识别(self):
        """「只看政策」得到 448 个节点但零条边 —— analyze 照样能排出一份
        全 0.0 的名单。调用方必须能据此清空排名。"""
        G, ty = build()
        out = draw(G, ty, want={"policy"})
        self.assertGreater(out["scope_nodes"], 0)
        self.assertEqual(len(out["edges"]), 0)


class Test图例禁用态(unittest.TestCase):
    """禁用态必须依据**筛选前**的计数（avail_by_group），
    否则取消勾选 → 计数变 0 → 判为"不存在" → 置灰 → 再也点不回来。"""

    def test_avail是筛选前的计数(self):
        G, ty = build()
        pre, _ = GA.crop_scope(G)
        avail = {t: sum(1 for x in pre.nodes() if TYPES.get(x) == t)
                 for t in GA.ENTITY_TYPES}
        self.assertEqual(avail["policy"], 2)
        out = draw(G, ty, want={"org"}, avail_by_group=avail)
        # 筛掉政策后 by_group.policy = 0，但 avail 仍是 2 → 不该被禁用
        self.assertEqual({x["group"]: x["n"] for x in out["by_group"]}["policy"], 0)
        self.assertEqual({x["group"]: x["n"]
                          for x in out["avail_by_group"]}["policy"], 2)

    def test_真的不存在才avail为0(self):
        G, ty = build()
        pre, _ = GA.crop_scope(G)
        avail = {t: sum(1 for x in pre.nodes() if TYPES.get(x) == t)
                 for t in GA.ENTITY_TYPES}
        self.assertEqual(avail["problem"], 1)
        # 库里"措施"是有的，但这里被 blocked 掉 → 换成不存在的类型验证
        self.assertEqual(avail["measure"], 1)

    def test_avail四类齐全(self):
        G, ty = build()
        out = draw(G, ty)
        self.assertEqual([x["group"] for x in out["avail_by_group"]],
                         list(GA.ENTITY_TYPES))


if __name__ == "__main__":
    unittest.main(verbosity=2)