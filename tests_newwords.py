# -*- coding: utf-8 -*-
"""新词自学习（分词实验室后端）。

测的是**每条判据各自的职责**，不是「整体跑通」——
凝固度 / 邻字熵 / 可切分性 / 领域加权里，任何一条单独失效
症状都一样（候选表里混进碎片），不分开测就永远定位不到是哪一条坏。

语料全部手写，**不读真库、不碰 data/cacx.db 与 config/settings.json**。
"""
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cacx import newwords as NW  # noqa: E402


def _corpus():
    """一份**有变化**的合成语料。

    ⚠ 别用「同一句话重复 N 遍」：那样单字分布过于均匀，
      _splittable 的兜底判据几乎恒成立，所有候选都会被判成短语，
      结果恒为 [] —— 测出来的不是引擎对不对，是语料有没有代表性。
    """
    subj = ["中央网信办", "省委网信办", "国家网信办", "工业和信息化部",
            "公安部", "市场监管总局", "国家发改委", "中央宣传部"]
    verb = ["部署", "推进", "开展", "印发", "组织", "召开", "强调", "要求"]
    obj = ["清朗网络专项行动", "网络安全宣传周活动", "个人信息保护专项执法",
           "网络谣言专项治理", "未成年人网络保护工作", "数据安全风险排查"]
    tail = ["取得阶段成效", "召开专题会议", "出台相关举措", "强化行业自律",
            "压实主体责任", "畅通举报渠道", "加大宣传力度", "健全长效机制"]
    return ["%s%s%s，%s。" % (a, b, c, d)
            for a in subj for b in verb for c in obj for d in tail]


class TestEntropy(unittest.TestCase):
    """邻字熵：左右能接的字越杂，越可能是真词"""

    def test_空分布熵为0(self):
        # ⚠ 0 是**真实值不是错误**，界面必须能把它和「算错了」区分开
        self.assertEqual(NW._entropy(Counter()), 0.0)

    def test_邻字越杂熵越大(self):
        self.assertEqual(NW._entropy(Counter("甲")), 0.0)
        self.assertGreater(NW._entropy(Counter("甲乙丙丁戊己庚辛")), 0.0)

    def test_邻字全相同熵为0(self):
        """⚠ 这是熵 0 的第二种成因，跟「在句子首/末」不同：
        候选总出现在同一个固定上下文里，更像短语片段。
        界面说明必须两种都写，只写一种就是不完整的。"""
        self.assertEqual(NW._entropy(Counter("啦啦啦啦啦")), 0.0)


class TestSplittable(unittest.TestCase):
    """可切分性：能切成两个已成立的词，就是短语不是新词

    ⚠ 这是整个引擎最关键的一条。实测凝固度和熵**单独使用**会把
      「实现发展」「推进落实」全部放行，而且这些短语的熵比真词还高。
    """

    def test_词典认的两段词算可切(self):
        uni = Counter("实现发展推进落实网络谣言")
        total = 1000
        self.assertTrue(NW._splittable("实现发展", uni, total))

    def test_两字词没有合法二段切法(self):
        self.assertFalse(NW._splittable("谣言", Counter("谣言"), 10))

    def test_切点落在虚字上的不算数(self):
        """「发展了」能切成「发展|了」，但「了」不算词，
        这种切法没有意义 —— 必须返回 False，否则碎片全被误杀/误留。"""
        uni = Counter("发展了的和")
        self.assertFalse(NW._splittable("发展了", uni, 10))


class TestDomainWeight(unittest.TestCase):
    """领域加权：排序用。提示与排序必须同判据，否则界面自相矛盾"""

    def test_含领域词上浮(self):
        self.assertGreater(NW.domain_weight("深度伪造治理"), 0)
        self.assertEqual(NW.domain_weight("卢旺达"), 0.0)

    def test_单字后缀的碎片不得加权(self):
        """回归用例：县各族干部 曾排到候选表第一。

        ⚠ 它同时踩中 _ORG_LEAD 的「县」和 _ORG_SUFFIX 的「部」，
          但它根本不是机构，是「县各族」+「干部」拼出来的。
          这类不能靠黑名单枚举（干部/教干/片干列不完），
          只能改判据：单字后缀要求整体 ≥6 字。
        """
        self.assertEqual(NW.domain_weight("县各族干部"), 0.0)
        self.assertIsNone(NW.domain_hint("县各族干部"))

    def test_多字后缀照常加权(self):
        # 「中心」是 2 字后缀，本身带语义，门槛仍是 ≥4
        self.assertGreater(NW.domain_weight("国家网络安全中心"), 0)

    def test_单字后缀够长才加权(self):
        self.assertGreater(NW.domain_weight("国家药品监督管理局"), 0)
        self.assertEqual(NW.domain_weight("工信部"), 0.0)

    def test_提示与加权结论一致(self):
        """⚠ 两者共用 _org_boostable()：界面写「以机构后缀结尾」
          就必须真的加权，否则用户会当成 bug。"""
        for w in ["县各族干部", "国家网络安全中心", "工信部",
                  "国家药品监督管理局", "深度伪造治理"]:
            hint = NW.domain_hint(w)
            boosted = NW.domain_weight(w) > 0
            self.assertEqual(bool(hint), boosted,
                             f"「{w}」提示={hint!r} 加权={boosted}")


class TestDiscover(unittest.TestCase):
    """端到端：造语料 → 出候选"""

    def test_纯噪声语料挖不出东西(self):
        # 语料太短、候选频次不够 → 空列表，不能瞎凑
        self.assertEqual(NW.discover_new_words(["啊"], min_count=5), [])

    def test_空输入不崩(self):
        self.assertEqual(NW.discover_new_words([], min_count=5), [])

    def test_挖出候选且不含虚字首尾(self):
        got = NW.discover_new_words(_corpus(), min_count=8, topn=30)
        self.assertTrue(got, "这份语料应该能挖出候选")
        for c in got:
            self.assertNotIn(c["word"][0], NW._DROP_CHARS, c["word"])
            self.assertNotIn(c["word"][-1], NW._DROP_CHARS, c["word"])

    def test_候选词长与频次在界内(self):
        for c in NW.discover_new_words(_corpus(), min_count=8, topn=30):
            self.assertGreaterEqual(len(c["word"]), NW.MIN_LEN, c)
            self.assertLessEqual(len(c["word"]), NW.MAX_LEN, c)
            self.assertGreaterEqual(c["count"], 8, c)

    def test_topn_生效(self):
        self.assertLessEqual(len(NW.discover_new_words(
            _corpus(), min_count=8, topn=3)), 3)

    def test_min_count_越大候选越少(self):
        few = NW.discover_new_words(_corpus(), min_count=8, topn=300)
        many = NW.discover_new_words(_corpus(), min_count=200, topn=300)
        self.assertLess(len(many), len(few))

    def test_已知词不再报为新词(self):
        """回归：曾经把「中央网信办」当新词报给用户 ——
        已知词过滤必须覆盖 内置词表 + jieba词典 + 已回流词 三份来源。"""
        known = {"中央网信办", "省委网信办", "国家网信办", "工业和信息化部",
                 "公安部", "市场监管总局", "国家发改委", "中央宣传部",
                 "网络安全", "专项行动", "数据安全", "个人信息"}
        got = NW.discover_new_words(_corpus(), min_count=8, topn=300,
                                    known=known)
        self.assertTrue(got)
        self.assertNotIn("中央网信办", [c["word"] for c in got])

    def test_按score降序(self):
        got = NW.discover_new_words(_corpus(), min_count=8, topn=50)
        scores = [c["score"] for c in got]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestNoDeadDefinition(unittest.TestCase):
    """⚠ 回归：newwords.py 一度存在**两份** discover_new_words，
    前一份只统计不返回（隐式 None），被后一份覆盖。
    症状极隐蔽：接口一切正常，只有读代码/改顺序的人会发现。
    判据：模块里这个名字只能有一个函数定义。"""

    def test_只有一个定义(self):
        import re
        src = Path(NW.__file__).read_text(encoding="utf-8")
        hits = re.findall(r"^def discover_new_words\(", src, re.M)
        self.assertEqual(len(hits), 1, "discover_new_words 出现了 %d 次定义" % len(hits))


class TestUserdict(unittest.TestCase):
    """词典回流：追加而非覆盖，格式是 jieba 认的三列"""

    def test_读写往返(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "userdict.txt"
            NW.write_userdict(p, ["清朗专项行动", "网络谣言"])
            self.assertEqual(NW.read_userdict(p), ["清朗专项行动", "网络谣言"])

    def test_追加不覆盖手工词(self):
        """⚠ 用户词典是他们的劳动成果，再点一次采纳不能把它冲掉。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "userdict.txt"
            p.write_text("手工词 100000 n\n", encoding="utf-8")
            NW.write_userdict(p, ["深度伪造"])
            self.assertIn("手工词", NW.read_userdict(p))

    def test_重复采纳去重(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "userdict.txt"
            NW.write_userdict(p, ["网络谣言"])
            NW.write_userdict(p, ["网络谣言", "网络谣言"])
            self.assertEqual(NW.read_userdict(p), ["网络谣言"])

    def test_读不存在的文件不崩(self):
        """词典是可选的：没有 / 读不了都只是「还没有用户词」，
        不能让整个检索功能挂掉。"""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(NW.read_userdict(Path(td) / "没有这个.txt"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
