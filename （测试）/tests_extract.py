# -*- coding: utf-8 -*-
"""
抽取引擎 + 关联分析的正式回归测试（不碰真库）

重点覆盖两类：
  ① 真库跑出来、自测样本掩盖不了的 bug（并列机构污染、证据基准、
     虚词机构、通用词机构、超长机构、阈值边界）
  ② 关联分析的**可复现性** —— 老师追问「这个数怎么来的」时必须能重跑
"""

import os
import sys
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
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

# ⚠ 真库 data/cacx.db 里 articles.id=3131 的原文（真实公文，非合成中文）。
#   合成句子的断句与机构分布跟真实公文差很远，测不出真实问题 ——
#   本文件里「证据挂到前一句」那个 bug 就是在真语料上才暴露的。
REAL_TITLE = "五部门联合印发《关于促进企业数据资源开发利用的意见》"
REAL_BODY = (
    "为充分释放企业数据资源价值，构建以数据为关键要素的数字经济，近日，"
    "国家数据局联合中央网信办、工业和信息化部、公安部、国务院国资委印发了"
    "《关于促进企业数据资源开发利用的意见》（以下简称《意见》）。"
    "《意见》以习近平新时代中国特色社会主义思想为指导，全面贯彻落实党的"
    "二十大和二十届二中、三中全会精神，完整准确全面贯彻新发展理念，着力"
    "推动高质量发展，统筹数据发展和安全，以深化数据要素市场化配置改革为"
    "主线，以激发企业创新活力为关键，以健全企业数据权益实现机制为重点，"
    "充分发挥企业主体作用，分类推进企业数据资源开发利用，提升企业竞争力，"
    "赋能产业数字化转型，助力提升治理效能和公共服务能力，为培育新质生产力、"
    "推动高质量发展提供有力支撑。"
    "《意见》从健全企业数据权益实现机制、培育企业数字化竞争力、赋能产业"
    "转型升级、服务经济社会高质量发展、营造开放透明可预期的发展环境等"
    "五个方面作出具体部署。"
    "下一步，国家数据局将会同相关部门，加强统筹协调和政策保障，协同各行业"
    "主管部门结合本领域实际，高质量推进企业数据资源开发利用。"
)


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

    def test_句子边界不重叠_归后一句(self):
        """⚠ 真事故：标题行与正文首句在边界处**同时命中**，证据挂错。

        `split_sentences` 返回的句子是 `text[start:i+1]`，**含句尾分隔符**，
        所以标题行是 `'标题\\n'`。而 full = 标题 + "\\n" + 正文，
        正文首句的起点正好等于「标题 + 分隔符」的长度。

        旧实现用闭区间 `start <= pos <= start+len(s)`：
            pos = 27 同时满足标题（0<=27<=27）与正文首句（27<=27<=27+…），
            遍历先撞到标题 → **返回标题**。
        症状：点边回溯，证据句里两个机构名都不出现。
        实测：1311 条关系里 417 条如此（31.8%）。

        修法：半开区间 + 剥掉句尾分隔符后算右端点 → 边界只归后一句。
        """
        title = "五部门联合印发《关于促进企业数据资源开发利用的意见》"
        body = ("为充分释放企业数据资源价值，近日，国家数据局联合中央网信办、"
                "工业和信息化部、公安部、国务院国资委印发了《关于促进企业"
                "数据资源开发利用的意见》。")
        full = title + "\n" + body
        boundary = len(title) + 1            # = 正文首句的起点，也是标题行的右端
        self.assertEqual(len(title) + 1, len(title + "\n"))

        got = extract.sentence_at(full, boundary)
        self.assertIn("国家数据局", got,
                      f"边界处应归**后一句**，实际拿到：{got[:40]}")
        self.assertIn("公安部", got, f"证据句缺主体：{got[:40]}")
        # 标题自身的位置仍要能取到标题
        self.assertIn("五部门", extract.sentence_at(full, 0))

    def test_sentence_at落空返回空串(self):
        """越界位置不能抛异常（下游会把 "" 当无证据处理）。"""
        self.assertEqual(extract.sentence_at("", 0), "")
        self.assertEqual(extract.sentence_at("一句话。", 999), "")

    def test_句间空白归前一句(self):
        """pos 落在两句之间的空白上时，归前一句（不是返回空）。"""
        txt = "第一句话。\n第二句话。"
        nl = txt.index("\n")
        self.assertEqual(extract.sentence_at(txt, nl), "第一句话。")

    def test_所有关系的证据句都含主体名(self):
        """端到端：断言**每一条** co_issue 的证据里都出现两个机构名。

        这条比单点测边界更有价值 —— 它直接钉住「点边看证据」这个
        老师点名要求的功能，而且是全量断言，不是抽样。

        ⚠ 语料用**真库里的原文**（id=3131），不用合成中文：
          合成句子的断句/机构分布跟真实公文差很远，测不出真实问题。
          真库里这条曾经是 417/1311 条证据挂错的重灾区。
        """
        title = "五部门联合印发《关于促进企业数据资源开发利用的意见》"
        body = REAL_BODY
        a = art(title=title, content_text=body)
        self.assertGreater(len(body), extract.MIN_CHARS)
        r = extract.extract_article(a)
        co = [x for x in r["relations"] if x["rel_type"] == "co_issue"]
        self.assertTrue(co, f"真语料里应该抽得出 co_issue，实际 {len(co)} 条")
        bad = [(x["src_name"], x["dst_name"], x["evidence"][:40])
               for x in co
               if x["src_name"] not in x["evidence"]
               or x["dst_name"] not in x["evidence"]]
        self.assertEqual(bad, [], f"证据句缺主体的 co_issue：{bad}")

    def test_边界修复对真语料生效(self):
        """同一篇真语料：正文首句起点处的关系，证据必须是正文句不是标题。"""
        full = "五部门联合印发《关于促进企业数据资源开发利用的意见》\n" \
               + REAL_BODY
        got = extract.sentence_at(full, 27)
        self.assertIn("国家数据局", got)
        self.assertIn("公安部", got)


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