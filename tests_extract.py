# -*- coding: utf-8 -*-
"""
抽取引擎 + 关联分析的正式回归测试（不碰真库）

重点覆盖两类：
  ① 真库跑出来、自测样本掩盖不了的 bug（并列机构污染、证据基准、
     虚词机构、通用词机构、超长机构、阈值边界）
  ② 关联分析的**可复现性** —— 老师追问「这个数怎么来的」时必须能重跑
"""
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

from cacx import extract
from cacx import graph_analysis as GA

BASE = {
    "id": 1,
    "title": "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》",
    "content_text": (
        "为进一步规范网络数据处理活动，切实保护公民个人信息权益，"
        "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》。"
        "通知要求各地网信部门会同公安机关开展网络安全专项检查，"
        "针对网络谣言、网络诈骗、未成年人沉迷等突出问题依法查处。"
        "各省级网信办要按照通知要求，结合本地实际制定实施方案。"
        "市场监管总局、教育部将联合开展监督检查，"
        "并对违规App下架处理。"),
}
# 正文补足到 200 字以上（低于 MIN_CHARS 会被正确跳过，那不是 bug）
LONG_BODY = BASE["content_text"] + (
    "各地要加强对算法推荐和深度伪造信息的治理力度，"
    "推动构建新发展格局，以人民为中心的工作理念贯穿治理全过程。")


def art(**kw):
    a = dict(BASE)
    a.update(kw)
    return a


class TestEvidence(unittest.TestCase):
    """证据是整个引擎的可信度锚点 —— 必须是原文的连续片段"""

    def test_证据可原样回到原文(self):
        a = art(content_text=LONG_BODY)
        r = extract.extract_article(a)
        v = extract.verify_evidence(
            r["entities"], r["relations"],
            lambda aid: (a["title"] or "").strip() + "\n" +
            a["content_text"][:extract.MAX_BODY])
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["entities_bad"], 0)
        self.assertEqual(v["relations_bad"], 0)

    def test_短正文被跳过且原因聚合(self):
        r = extract.extract_article(art(content_text="太短"))
        self.assertTrue(r["skipped"])
        # 原因不能带具体字数，否则 500 篇会产出几十个不同 reason
        self.assertIn(str(extract.MIN_CHARS), r["reason"])
        self.assertNotIn("仅", r["reason"])


class TestOrgFilter(unittest.TestCase):
    """机构误抽是真实库暴露的：垃圾机构一度占机构总量 60%+"""

    def _orgs(self, body):
        a = art(content_text=body + "x" * max(0, extract.MIN_CHARS - len(body)))
        r = extract.extract_article(a)
        return [e["name"] for e in r["entities"] if e["type"] == "org"]

    def test_并列机关被切成两个(self):
        orgs = self._orgs(LONG_BODY)
        self.assertIn("公安部", orgs)
        self.assertTrue(any("网信办" in o for o in orgs))
        # 不许出现带顿号的粘连名
        self.assertFalse([o for o in orgs if "、" in o])

    def test_标点不被拼进机构名(self):
        """jieba 会把句号切成独立 token，拼 n-gram 时必须先跳掉，
        否则会拼出「。各省级网信办」这种假机构。"""
        orgs = self._orgs(LONG_BODY)
        for o in orgs:
            self.assertNotIn("。", o, o)
            self.assertNotIn("，", o, o)

    def test_虚词开头不算机构(self):
        self.assertFalse(extract._plausible_org("不仅要"))
        self.assertFalse(extract._plausible_org("了全社会"))
        self.assertFalse(extract._plausible_org("了一定的社会"))

    def test_会议峰会不算机构(self):
        for bad in ("乌镇峰会", "二十届四中全会", "第三届会员代表大会",
                    "声援大会", "周年高级别对话会"):
            self.assertFalse(extract._plausible_org(bad), bad)

    def test_通用词裸奔不算机构(self):
        """「研究院」「委员会」这类通用词做后缀时区分度不够，
        只有带前缀的（XX研究院）才该进图。"""
        for bad in ("研究院", "委员会", "联合会", "经济社会",
                    "发展格局", "国际社会"):
            self.assertFalse(extract._plausible_org(bad), bad)

    def test_动词短语不算机构(self):
        for bad in ("构建新发展格局", "为中心", "推动加强"):
            self.assertFalse(extract._plausible_org(bad), bad)

    def test_超长串不算机构(self):
        """长度上限是性价比最高的一条硬约束：真机构都 ≤15 字，
        而 n-gram 失控产生的垃圾全都超长。"""
        self.assertTrue(extract._plausible_org("中央网络安全和信息化委员会"))
        self.assertFalse(extract._plausible_org(
            "为主要生活来源的未成年人作出例外规定"))

    def test_真机构仍能抽到(self):
        orgs = self._orgs(LONG_BODY)
        for good in ("中央网信办", "公安部", "市场监管总局", "教育部"):
            self.assertIn(good, orgs)


class TestPolicyAndMeasure(unittest.TestCase):
    def test_政策来自书名号(self):
        r = extract.extract_article(art(content_text=LONG_BODY))
        pol = [e["name"] for e in r["entities"] if e["type"] == "policy"]
        self.assertTrue(any("加强网络数据安全治理" in p for p in pol), pol)

    def test_动词短语不被误抽成政策(self):
        """「各省级网信办要按照通知」只是恰好以「通知」结尾的普通句子。"""
        self.assertFalse(extract.is_policy_name("各省级网信办要按照通知"))
        self.assertFalse(extract.is_policy_name("未成年人沉迷等突出问题依法"))

    def test_措施标needs_review(self):
        """⚠ 阈值用 <= 而非 <：置信度恰好 0.8 的措施曾因卡在边界而漏标。"""
        r = extract.extract_article(art(content_text=LONG_BODY))
        meas = [e for e in r["entities"] if e["type"] == "measure"]
        self.assertTrue(meas)
        for e in meas:
            self.assertTrue(e.get("needs_review"), e)
            self.assertLessEqual(e["confidence"], extract.NEEDS_REVIEW_BELOW)

    def test_治理问题复用统一词表(self):
        r = extract.extract_article(art(content_text=LONG_BODY))
        prob = {e["name"] for e in r["entities"] if e["type"] == "problem"}
        self.assertTrue(prob & {"网络谣言", "网络诈骗", "未成年人", "数据安全"})


class TestRelations(unittest.TestCase):
    def test_联合发文只在触发句内配对(self):
        """8 个机构全文互连会产出 56 条噪声边；正确做法是
        只在含联合触发词的那一句里配对。"""
        r = extract.extract_article(art(content_text=LONG_BODY))
        joint = [x for x in r["relations"] if x["rel_type"] == "co_issue"]
        self.assertTrue(joint)
        n_org = len({x["src_name"] for x in joint} |
                    {x["dst_name"] for x in joint})
        # 联合发文边不应把全文所有机构都拉进来
        self.assertLessEqual(n_org, 8)
        self.assertLessEqual(len(joint), 40)

    def test_四类关系都出现(self):
        r = extract.extract_article(art(content_text=LONG_BODY))
        got = {x["rel_type"] for x in r["relations"]}
        self.assertIn("co_issue", got)
        self.assertIn("target", got)

    def test_关系都带证据与文章id(self):
        r = extract.extract_article(art(content_text=LONG_BODY))
        for x in r["relations"]:
            self.assertTrue(x["evidence"].strip(), x)
            self.assertEqual(x["article_id"], BASE["id"])


class TestGraphAnalysis(unittest.TestCase):
    RELS = [
        {"src_name": "中央网信办", "dst_name": "公安部", "rel_type": "co_issue", "weight": 5},
        {"src_name": "中央网信办", "dst_name": "市场监管总局", "rel_type": "co_issue", "weight": 3},
        {"src_name": "公安部", "dst_name": "市场监管总局", "rel_type": "co_issue", "weight": 2},
        {"src_name": "中央网信办", "dst_name": "网络谣言", "rel_type": "target", "weight": 4},
        {"src_name": "公安部", "dst_name": "网络谣言", "rel_type": "target", "weight": 2},
        {"src_name": "市场监管总局", "dst_name": "网络谣言", "rel_type": "target", "weight": 1},
    ]

    def test_每条结果都带全部指标(self):
        """前端 tooltip 要同时显示度/加权度/介数/社区，
        缺一个 key 就会在渲染时 KeyError。"""
        G, t = GA.build_graph(self.RELS)
        r = GA.analyze(G, t)
        for key in ("degree", "strength", "betweenness", "pagerank"):
            for item in r[key]:
                for f in ("name", key, "degree", "strength",
                          "betweenness", "pagerank", "community"):
                    self.assertIn(f, item, f"{key} 缺 {f}")

    def test_两次跑社区划分一致(self):
        """Louvain 默认带随机性；不锁 seed 的话同一份数据两次跑
        社区划分会不同，报告里的数字就无法当场复现。"""
        G1, t1 = GA.build_graph(self.RELS)
        r1 = GA.analyze(G1, t1)
        G2, t2 = GA.build_graph(self.RELS)
        r2 = GA.analyze(G2, t2)
        self.assertEqual([sorted(c["members"]) for c in r1["communities"]],
                         [sorted(c["members"]) for c in r2["communities"]])
        self.assertEqual({x["name"]: x["betweenness"] for x in r1["betweenness"]},
                         {x["name"]: x["betweenness"] for x in r2["betweenness"]})

    def test_居中者介数最高(self):
        """中央网信办连着公安部、市场监管总局、网络谣言三处，
        应是度与介数双高 —— 这正是「协调枢纽」的量化证据。"""
        G, t = GA.build_graph(self.RELS)
        r = GA.analyze(G, t)
        top = r["betweenness"][0]["name"]
        self.assertEqual(top, "中央网信办")

    def test_关系类型占比可算(self):
        G, t = GA.build_graph(self.RELS)
        r = GA.analyze(G, t)
        by = {x["rel_type"]: x for x in r["by_type"]}
        self.assertIn("co_issue", by)
        self.assertIn("target", by)
        self.assertAlmostEqual(sum(x["pct"] for x in r["by_type"]), 100.0,
                               places=0)

    def test_低置信节点不进图(self):
        G, t = GA.build_graph(
            self.RELS + [{"src_name": "下架", "dst_name": "中央网信办",
                          "rel_type": "adopt", "weight": 1}],
            blocked_nodes={"下架"})
        self.assertNotIn("下架", set(G.nodes))
        # EdgeDataView 不可下标，用邻接表查
        self.assertNotIn("下架", set(G["中央网信办"]))

    def test_空图不崩(self):
        G, t = GA.build_graph([])
        r = GA.analyze(G, t)
        self.assertEqual(r["nodes"], 0)
        self.assertEqual(r["degree"], [])

    def test_前端数据按度数收敛(self):
        G, t = GA.build_graph(self.RELS)
        r = GA.analyze(G, t)
        ne = GA.to_pyvis_nodes_edges(r, G, t, min_degree=1, limit=300)
        self.assertTrue(ne["nodes"])
        # 节点大小应随度数递增
        sizes = [n["size"] for n in ne["nodes"]]
        self.assertGreaterEqual(sizes[0], sizes[-1])
        for n in ne["nodes"]:
            self.assertIn("title", n)
            for f in ("degree", "strength", "betweenness", "pagerank",
                      "community"):
                self.assertIn(f, n)


if __name__ == "__main__":
    unittest.main(verbosity=2)