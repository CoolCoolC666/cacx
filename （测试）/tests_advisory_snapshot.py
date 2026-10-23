# -*- coding: utf-8 -*-
"""AI 顾问快照的回归闸：残余站点与裸英文字段都不能进模型输入。

背景（2026-10-23）：数据健康顾问把「最近同步批次」原样喂给模型，
而 sync_runs 存的是**历史** —— 2026-10-13 做通用采集器验证时临时加的
`zjwx-api`（只抓 20 条、2 个栏目）留了一行历史，模型会把它当成
**第三个站点**解读，进而得出「来源构成失衡」之类的错误结论。
同时批次字段是裸英文（listed/failed），模型只能靠猜。

这两条都是**静默劣化**：不报错，只是结论慢慢变歪。所以必须有闸。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from cacx import db as DB
from cacx import sites as S
from cacx.config import load


class TestAdvisorySnapshot(unittest.TestCase):
    """顾问快照的三条硬约束"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = load()
        cls.db = DB.open_db(cls.cfg)
        cls.allsrc = S.all_sites(cls.cfg) or {}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def _enabled(self):
        return [c for c, s in self.allsrc.items()
                if (s or {}).get("enabled", True)]

    def test_停用站点的历史批次不会进快照(self):
        """
        已停用/已删除站点的 sync_runs 历史行必须被挡住。

        判据用「站点是否在启用列表里」而不是「库里有几篇」——
        停用的站不该进快照，但它可能有历史数据。
        """
        enabled = set(self._enabled())
        batches = self._advisory_snapshot()["最近同步批次"]
        for b in batches:
            self.assertIn(b["站点代码"], enabled,
                          f"快照里混进了未启用站点的批次：{b}")

    def test_残余接口不得出现在批次里(self):
        """
        zjwx-api 是 2026-10-13 的开发验证源，已停用。
        它在 sync_runs 里留着一行历史 —— 出现在快照里就会被当成第三个站点。
        """
        batches = self._advisory_snapshot()["最近同步批次"]
        for b in batches:
            self.assertNotEqual(b["站点代码"], "zjwx-api",
                                "开发残余接口 zjwx-api 进了顾问快照")

    def test_批次字段是中文化过的(self):
        """
        裸英文 listed/failed 会让模型去猜语义 —— 必须换成中文名。

        ⚠ 中文化发生在 **advisory_snapshot() 层**，不在 db 层：
        db.recent_sync_runs 返回的是库层原始行（英文是对的，
        DB 层不该负责给 AI 喂的字段改名）。所以测的是
        advisory_snapshot 真正构造出来的那段 JSON ——
        **直接调它，不复制逻辑**（抄一份口径必然对不上）。
        """
        snap = self._advisory_snapshot()
        batches = snap["最近同步批次"]
        self.assertTrue(batches, "快照里没有批次")
        for b in batches:
            for bad in ("listed", "failed", "new_articles",
                        "started_at", "finished_at", "site", "mode"):
                self.assertNotIn(bad, b,
                                 f"批次里仍有裸英文键 {bad}：{b}")
            for good in ("列表接口扫到条数", "本次新增条数",
                         "抓取失败条数", "站点", "开始时间"):
                self.assertIn(good, b,
                              f"批次缺少中文字段 {good}：{b}")

    def _advisory_snapshot(self):
        """直接调生产代码里的快照构造函数。"""
        from cacx.ai.tasks import advisory_snapshot
        return advisory_snapshot(self.cfg, self.db, [], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
