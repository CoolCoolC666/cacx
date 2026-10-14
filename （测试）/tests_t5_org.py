# -*- coding: utf-8 -*-
"""
T5：机构识别漏词修复 —— 固定建制名白名单。

背景（见 docs/对接说明/06 §6.2）：
  「中共中央」「中国人民银行」「中国人民政治协商会议」此前在构词法就被否，
  库里 0 次。根因不是数据，是规则过宽：
    · 尾字「央」「行」不在 _ORG_SUFFIX；
    · 尾巴「会议」撞上 _BAD_TAIL / _NOT_ORG_TAIL。
  修法是**只对固定建制名加精确白名单**，绝不整类放开「会议/大会」
  （那会把新闻发布会、高峰论坛、乌镇峰会全放回图谱）。

本测试三条硬断言：
  ① 四个建制名改后 org_like 与 _plausible_org 双双放行；
  ② 普通活动名（XX大会/XX会议）仍被双重闸门挡住，不得误放；
  ③ 端到端 extract_article 真能从公文里抽出「中共中央」等机构。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from cacx import textseg          # noqa: E402
from cacx import extract           # noqa: E402

#: T5 要求放行的固定建制名
WHITELISTED = [
    "中共中央",
    "中国人民银行",
    "中国人民政治协商会议",
    "全国人民代表大会",
]

#: 普通活动/会议名 —— 绝不能被当成机构放进图谱。
#: 判定闸门 = org_like 且 _plausible_org 同时通过（见 extract._org_mentions）。
ACTIVITY_NAMES = [
    "新闻发布会",
    "高峰论坛",
    "乌镇峰会",
    "第三届会员代表大会",
    "四中全会",
    "全国教育大会",
    "中央经济工作会议",
    "欢迎宴会",
]


class TestWhitelistPositive(unittest.TestCase):
    def test_白名单建制名构词法放行(self):
        for w in WHITELISTED:
            self.assertTrue(textseg.org_like(w),
                            f"org_like 该放行「{w}」却被否")

    def test_白名单建制名抽取层放行(self):
        for w in WHITELISTED:
            self.assertTrue(extract._plausible_org(w),
                            f"_plausible_org 该放行「{w}」却被否")


class TestActivityNegative(unittest.TestCase):
    def test_活动名不得双闸门放行(self):
        """负例：org_like 与 _plausible_org 不能同时为 True。
        （图谱里一个机构只有两道都过才会入库。）"""
        for w in ACTIVITY_NAMES:
            both = textseg.org_like(w) and extract._plausible_org(w)
            self.assertFalse(
                both,
                f"「{w}」是活动名，却被当成机构放进图谱 "
                f"(org_like={textseg.org_like(w)}, plausible={extract._plausible_org(w)})")


class TestExistingOrgsStillWork(unittest.TestCase):
    def test_既有核心机构不受影响(self):
        for w in ["中央网信办", "公安部", "工业和信息化部", "国务院",
                  "国家发展改革委", "全国人大常委会"]:
            self.assertTrue(textseg.org_like(w), f"「{w}」被误伤")


class TestEndToEnd(unittest.TestCase):
    def test_从公文抽出中共中央等建制名(self):
        art = {
            "id": 999,
            "title": "中共中央、中国人民银行联合印发指导意见",
            "content_text": (
                "为深入贯彻落实党中央、国务院决策部署，中共中央、中国人民银行"
                "联合印发《关于金融支持网络强国建设的指导意见》。"
                "意见明确了下一步工作要求，各有关方面要认真抓好落实。"
                "中国人民政治协商会议召开专题会议，全国人民代表大会相关"
                "专门委员会听取专项工作报告。"
            ) * 6,
        }
        r = extract.extract_article(art)
        orgs = {e["name"] for e in r["entities"] if e["type"] == "org"}
        for must in ("中共中央", "中国人民银行", "中国人民政治协商会议"):
            self.assertIn(must, orgs,
                          f"端到端没抽出「{must}」，实际机构：{sorted(orgs)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
