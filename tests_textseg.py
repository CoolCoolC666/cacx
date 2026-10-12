# -*- coding: utf-8 -*-
"""
中文分词与机构候选词挖掘。

这两件事有没有用，只能靠真实标题跑出来看，
所以每条断言都对着**具体词**写，不写「分词结果应包含某类词」这种废话。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import textseg                # noqa: E402


class TestDict(unittest.TestCase):
    """内置词典：机构名不能被切碎，否则下游抽出来全是垃圾"""

    def test_机构名不被切碎(self):
        for phrase, must in [
            ("中央网信办部署开展清朗网络专项行动", "中央网信办"),
            ("公安部、市场监管总局联合发布", "公安部"),
            ("省委网信办召开网络安全工作会议", "省委网信办"),
            ("工业和信息化部关于印发通知", "工业和信息化部"),
        ]:
            toks = textseg.cut(phrase)
            self.assertIn(must, toks,
                          f"「{must}」被切碎了：{toks}")

    def test_政策名不被切碎(self):
        toks = textseg.cut("深入贯彻《个人信息保护法》")
        self.assertIn("个人信息保护法", toks, toks)

    def test_治理问题词被识别(self):
        toks = textseg.cut("依法整治网络谣言与网络暴力")
        self.assertIn("网络谣言", toks, toks)

    def test_没装jieba也不崩(self):
        """jieba 是可选依赖。没它全文检索要退化，但程序不能起不来。"""
        saved, textseg._jieba = textseg._jieba, None
        textseg._load_failed = True
        try:
            self.assertFalse(textseg.available())
            self.assertTrue(textseg.cut("中央网信办部署"))   # 不抛异常即可
            self.assertEqual(textseg.mine_orgs(["中央网信办发文"]), [])
        finally:
            textseg._load_failed = False
            textseg._jieba = saved


class TestKeywords(unittest.TestCase):
    def test_丢单字与停用词(self):
        kw = textseg.keywords("关于印发《通知》的通知", topk=10)
        self.assertNotIn("的", kw)
        self.assertTrue(all(len(w) >= 2 for w in kw))

    def test_高频词排前面(self):
        kw = textseg.keywords("网信办 网信办 网信办 清朗 谣言 谣言", topk=5)
        self.assertEqual(kw[0], "网信办")


class TestMineOrgs(unittest.TestCase):
    TITLES = [
        "中央网信办部署开展2024年清朗系列专项行动",
        "中央网信办网络局召开座谈会",
        "中央网信办举报中心通报情况",
        "公安部、市场监管总局联合发布典型案例",
        "市场监管总局发布合规指引",
        "省委网信办传达学习相关精神",
        "省委网信办召开全省网络安全工作会议",
        "教育部回应关切",
    ]

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_挖到核心机构(self):
        # 用真实语料验证过：中央网信办 233 次、省网信办 53 次、
        # 公安部 52 次、市场监管总局 18 次。
        # 这里只断言「能从这批标题里找到这几个主体」，
        # 因为候选词的长短取决于 jieba 怎么切，不该断言字面完全相等。
        got = {o["org"] for o in textseg.mine_orgs(self.TITLES, min_count=1)}
        for must in ("中央网信办", "公安部", "市场监管总局", "省委网信办"):
            self.assertTrue(any(must in g for g in got),
                            f"没挖到「{must}」，挖到的是：{sorted(got)}")

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_附了出现次数和例句(self):
        orgs = {o["org"]: o for o in textseg.mine_orgs(
            self.TITLES, min_count=2)}
        k = next((k for k in orgs if "中央网信办" in k), None)
        self.assertIsNotNone(k)
        self.assertGreaterEqual(orgs[k]["count"], 2)
        self.assertIn("中央网信办", orgs[k]["example_title"])

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_高频门槛有效(self):
        low = textseg.mine_orgs(self.TITLES, min_count=99)
        self.assertEqual(low, [])


class TestNormalize(unittest.TestCase):
    """归并：互为子串的机构名必须合成一个，否则合作网络会裂成孤岛"""

    def test_子串归并到主名(self):
        raw = [
            {"org": "中央网信办", "count": 100, "example_title": "a"},
            {"org": "中央网信办网络局", "count": 20, "example_title": "b"},
            {"org": "中央网信办举报中心", "count": 9, "example_title": "c"},
        ]
        got = textseg.normalize_orgs(raw)
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["count"], 100, "频次应取全组最大")
        v = got[0]["variants"]
        for m in ("中央网信办", "中央网信办网络局", "中央网信办举报中心"):
            self.assertTrue(any(m in x for x in v),
                            f"「{m}」没被收进变体：{v}")

    def test_兄弟节点也能归并(self):
        """⚠ 这条是回归：曾经只按「互为子串」判断，
        中央网信办网络局 与 中央网信办举报中心 互不包含，
        结果各自成了孤岛。必须按最长公共前缀归并。"""
        raw = [
            {"org": "中央网信办网络局", "count": 20, "example_title": "a"},
            {"org": "中央网信办举报中心", "count": 9, "example_title": "b"},
        ]
        got = textseg.normalize_orgs(raw)
        self.assertEqual(len(got), 1, f"兄弟节点没合并：{got}")
        self.assertEqual(len(got[0]["variants"]), 2, got[0]["variants"])

    def test_简称全称归并(self):
        raw = [
            {"org": "市场监管总局", "count": 50, "example_title": "a"},
            {"org": "国家市场监督管理总局", "count": 30, "example_title": "b"},
        ]
        got = textseg.normalize_orgs(raw)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(got[0]["variants"]), 2)

    def test_无关机构不合并(self):
        raw = [
            {"org": "公安部", "count": 50, "example_title": "a"},
            {"org": "教育部", "count": 40, "example_title": "b"},
            {"org": "市场监管总局", "count": 30, "example_title": "c"},
        ]
        self.assertEqual(len(textseg.normalize_orgs(raw)), 3)

    def test_主名取出现最多的(self):
        raw = [
            {"org": "网信办", "count": 100, "example_title": "a"},
            {"org": "省网信办网络管理处", "count": 5, "example_title": "b"},
        ]
        got = textseg.normalize_orgs(raw)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["count"], 100, "频次应取两者的较大值")
        self.assertEqual(got[0]["org"], "省网信办网络管理处", "主名取更具体的")

    def test_空输入(self):
        self.assertEqual(textseg.normalize_orgs([]), [])

    def test_标点前缀不污染主名(self):
        """⚠ 这条是回归：标题里 n-gram 会把相邻标点吞进来，
        出现「、公安部」「公安部、」「……公安部」三种形态。
        原样入库 → 归并时它们是三个不相等的 key，主流名甚至被
        「、公安部」（更长）抢走，报告里就出现带标点的假机构名。"""
        raw = [
            {"org": "、公安部", "count": 20, "example_title": "a"},
            {"org": "公安部、", "count": 20, "example_title": "b"},
            {"org": "公安部", "count": 30, "example_title": "c"},
        ]
        got = textseg.normalize_orgs(raw)
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["org"], "公安部", got[0]["org"])
        self.assertEqual(got[0]["count"], 30, "频次应取全组最大")

    def test_动词短语不算机构(self):
        """以「办」「委」「部」结尾不代表是机构。
        「成功举办」「在四川成都举办」「？法院」都曾混进候选。"""
        for bad in ("成功举办", "在四川成都举办", "召开会议研究部署",
                    "慰问基层干部", "出席二十国集团"):
            self.assertFalse(textseg.org_like(bad), f"「{bad}」不该被当机构")
        for good in ("中央网信办", "公安部", "工业和信息化部", "清华大学"):
            self.assertTrue(textseg.org_like(good), f"「{good}」该被认成机构")


class TestQuery(unittest.TestCase):
    def test_长查询切词(self):
        got = textseg.tokenize_query("算法推荐管理")
        self.assertGreater(len(got), 1, got)
        self.assertIn("算法推荐", got)

    def test_成词查询原样保留(self):
        got = textseg.tokenize_query("未成年人网络保护")
        self.assertIn("未成年人网络保护", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)