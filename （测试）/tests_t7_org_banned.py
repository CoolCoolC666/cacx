# -*- coding: utf-8 -*-
"""
T7：活动/会议名挤占「高频机构 Top」—— 抽取层精确排除。

背景（现场截图，近 50 个月全量口径的高频机构榜）：
  「互联网大会乌镇峰会」153、「大数据产业博览会」68、「中国网络文明大会」41、
  「组织成员国元首理事会」39、「重要思想理论研讨会」30、「回归祖国25周年大会」23
  挤占机构榜；「的二十届四中全会」24 缺「中共」前缀；「全国人大常委会国务院」90
  是拼接残片。

根因：机构候选挖掘 `mine_orgs` **只过 `org_like` 这一道粗筛**（不走
`_plausible_org`），而 `org_like` 的 `_BAD_TAIL` 只拦「会议/论坛/座谈」，
拦不住「峰会/博览会/研讨会/理事会/全会/大会」——它们的尾巴是「会/院」，
落在 `_ORG_SUFFIX` 里，粗筛直接放行。

修法（R5 §9：`_NOT_ORG_TAIL` / `_ORG_VERB` 整体框架不动，只加精确例外）：
  · `textseg.ORG_BANNED_TERMS` —— 活动/会议名的**完整全名**（整串命中即否）；
  · `textseg.ORG_BANNED_TAILS` —— **无歧义活动尾巴**（后缀命中即否）。
  两张表同时进 `org_like()` 与 `extract._plausible_org()`。
  ⚠ 绝不整类放开「会议/大会」——它们和人大常委会/人民代表大会/政协会议撞尾。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from cacx import textseg          # noqa: E402
from cacx import extract          # noqa: E402

#: 现场点名的坏例（完整全名）
NAMED_BAD = [
    "互联网大会乌镇峰会",
    "大数据产业博览会",
    "中国网络文明大会",
    "组织成员国元首理事会",
    "重要思想理论研讨会",
    "回归祖国25周年大会",
    "的二十届四中全会",
]
#: 同族的 n-gram 变体（拉黑主名后会立刻顶上来的那些）
VARIANT_BAD = [
    "年世界互联网大会", "世界互联网大会", "大会乌镇峰会", "乌镇峰会",
    "2024年乌镇峰会", "的二十届三中全会", "两院院士大会",
    "国家科学技术奖励大会", "传播精品分享会",
]
#: 拼接残片（两个真机构连写成一个节点）
CONCAT_BAD = ["全国人大常委会国务院", "中共中央国务院", "国务院和中央军委"]
#: 真机构 —— 一律不得误伤
GOOD_ORGS = [
    "中共中央", "中国人民银行", "中国人民政治协商会议", "全国人民代表大会",
    "中央网信办", "公安部", "工业和信息化部", "国务院", "国家发展改革委",
    "国家市场监督管理总局", "中央网络安全和信息化委员会", "农业农村部",
    "生态环境部", "教育部", "国家网信办",
]


class TestNamedOffendersBanned(unittest.TestCase):
    def test_点名坏例双闸门为零(self):
        """活动名/拼接残片在两道闸门上都应为 False（图谱里一个机构要两道都过）。"""
        for w in NAMED_BAD + VARIANT_BAD + CONCAT_BAD:
            self.assertFalse(textseg.org_like(w), f"org_like 该否「{w}」")
            self.assertFalse(extract._plausible_org(w), f"_plausible_org 该否「{w}」")


class TestRealOrgsNotHurt(unittest.TestCase):
    def test_真机构不被误伤(self):
        for w in GOOD_ORGS:
            self.assertTrue(textseg.org_like(w), f"org_like 误伤「{w}」")

    def test_会议大会不整类(self):
        """R5 §9：不得整类否「会议/大会」。这些真机构必须仍然放行：
        · 全国人民代表大会     尾「代表大会」（T5 白名单）
        · 中国人民政治协商会议 尾「会议」    （T5 白名单）
        · 全国人大常委会       尾「常委会」
        · 中央网络安全和信息化委员会 尾「委员会」（13 字，也别被长度上限误杀）"""
        for w in ("全国人民代表大会", "中国人民政治协商会议",
                  "全国人大常委会", "中央网络安全和信息化委员会"):
            self.assertTrue(textseg.org_like(w), f"「{w}」被整类误伤")
        self.assertTrue(extract._plausible_org("中央网络安全和信息化委员会"))


class TestMiningPath(unittest.TestCase):
    """端到端：挖掘候选时不放活动名，但真机构照挖。"""
    TITLES = [
        "2024年世界互联网大会乌镇峰会在浙江乌镇举行",
        "大数据产业博览会在贵阳开幕",
        "中央网信办、公安部联合部署清朗行动",
        "中国网络文明大会发布典型案例",
        "中央网信办部署开展清朗系列专项行动",
    ]

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_活动名不入候选而真机构入候选(self):
        got = {o["org"] for o in textseg.mine_orgs(self.TITLES, min_count=1)}
        self.assertTrue(any("中央网信办" in g for g in got), sorted(got))
        for bad in ("乌镇峰会", "互联网大会", "产业博览会", "网络文明大会"):
            self.assertFalse(any(bad in g for g in got),
                             f"活动名「{bad}」混进了候选：{sorted(got)}")


class TestEndToEndExtract(unittest.TestCase):
    def test_端到端不抽活动名只抽机构(self):
        art = {
            "id": 1001,
            "title": "中央网信办、公安部联合部署，2024年世界互联网大会乌镇峰会举行",
            "content_text": (
                "中央网信办、公安部近日联合印发通知，部署开展专项行动。"
                "2024年世界互联网大会乌镇峰会在浙江乌镇举行，"
                "大数据产业博览会同期开幕。会议指出，要深化网络生态治理。"
            ) * 8,
        }
        r = extract.extract_article(art)
        orgs = {e["name"] for e in r["entities"] if e["type"] == "org"}
        self.assertTrue(any("中央网信办" in o for o in orgs), sorted(orgs))
        self.assertTrue(any("公安部" in o for o in orgs), sorted(orgs))
        for bad in ("乌镇峰会", "互联网大会", "大数据产业博览会"):
            self.assertFalse(any(bad in o for o in orgs),
                             f"活动名「{bad}」被当成机构抽进来了：{sorted(orgs)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
