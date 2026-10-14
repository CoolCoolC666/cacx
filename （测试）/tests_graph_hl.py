# -*- coding: utf-8 -*-
"""图谱点击聚焦（hl）：精确命中 + 一跳邻居，**只标不改图**

背景（R4 对接文档 §3.2）：点节点**不能**复用 find ——
find 的边命中要求**两端都命中**，点单个节点时它只有一个命中点、
邻居都不叫这个名字，于是边命中数 = 0，图上只有一个黄点、四周全暗，
等于什么都没发生。

hl 的语义：
  · hl=<节点名>：该节点 + **一跳邻居** + 与它相连的全部边
  · hl=<a>|<b> ：恰好这 2 个节点 + 这 1 条边

这里断言的性质：
  ① 加不加 hl，节点数与边数**一模一样**（与 find 一样，只标不改图）
  ② 一跳邻居，**不含二跳**（点击「甲」绝不能把「丙」也点亮）
  ③ 导出 HTML 与屏幕同色 —— 屏幕上的聚焦导不出来 = 白聚焦了
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import networkx as nx

from cacx.graph_analysis import to_pyvis_nodes_edges

HIT_HEX = "#ffd479"


def mk():
    """一张小图：甲—乙—丙—丁 一条链，另挂一个不相关的戊

    特意做出「甲的一跳邻居=乙、戊，二跳=丙」—— 用来验「不含二跳」。
    """
    G = nx.Graph()
    G.add_edge("甲", "乙", weight=3)
    G.add_edge("乙", "丙", weight=3)
    G.add_edge("丙", "丁", weight=3)
    G.add_edge("甲", "戊", weight=3)
    return G


def draw(hl=""):
    return to_pyvis_nodes_edges({}, mk(), min_degree=1, limit=300, hl=hl)


class TestHlHighlight(unittest.TestCase):
    def test_base_has_no_hl(self):
        """不传 hl 时不得有任何 hl 标记 —— 基线被污染，后面全不可信"""
        r = draw("")
        self.assertEqual(r["hl"], "")
        self.assertEqual(r["hl_hits"], 0)
        self.assertEqual(r["hl_edge_hits"], 0)
        self.assertFalse(any(n.get("hl") for n in r["nodes"]))
        self.assertFalse(any(e.get("hl") for e in r["edges"]))

    def test_size_unchanged_by_hl(self):
        """① 高亮不得改变图规模 —— 这条一破，它就退化成筛选了"""
        a, b = draw(""), draw("甲")
        self.assertGreater(len(a["nodes"]), 0)
        self.assertEqual(len(a["nodes"]), len(b["nodes"]))
        self.assertEqual(len(a["edges"]), len(b["edges"]))
        self.assertEqual([n["id"] for n in a["nodes"]],
                         [n["id"] for n in b["nodes"]])
        self.assertEqual(a["scope_nodes"], b["scope_nodes"])

    def test_node_hl_neighbors_and_edges(self):
        """点节点 → 它 + 一跳邻居 + 关联边"""
        r = draw("甲")
        hl_nodes = {n["id"] for n in r["nodes"] if n.get("hl")}
        self.assertEqual(hl_nodes, {"甲", "乙", "戊"})
        hl_edges = [e for e in r["edges"] if e.get("hl")]
        self.assertEqual(len(hl_edges), 2)
        self.assertEqual({frozenset((e["from"], e["to"])) for e in hl_edges},
                         {frozenset(("甲", "乙")), frozenset(("甲", "戊"))})
        self.assertEqual(r["hl_hits"], 3)
        self.assertEqual(r["hl_edge_hits"], 2)

    def test_node_hl_one_hop_only(self):
        """② 一跳邻居，**不含二跳**：点「甲」绝不能把「丙」也点亮"""
        r = draw("甲")
        hl_nodes = {n["id"] for n in r["nodes"] if n.get("hl")}
        self.assertNotIn("丙", hl_nodes)
        self.assertNotIn("丁", hl_nodes)

    def test_pair_hl(self):
        """hl=a|b → 恰好 2 个节点 + 这 1 条边"""
        r = draw("乙|丙")
        hl_nodes = {n["id"] for n in r["nodes"] if n.get("hl")}
        self.assertEqual(hl_nodes, {"乙", "丙"})
        hl_edges = [e for e in r["edges"] if e.get("hl")]
        self.assertEqual(len(hl_edges), 1)
        self.assertEqual({frozenset((e["from"], e["to"])) for e in hl_edges},
                         {frozenset(("乙", "丙"))})
        self.assertEqual(r["hl_hits"], 2)
        self.assertEqual(r["hl_edge_hits"], 1)

    def test_pair_without_direct_edge(self):
        """hl=甲|丙：两点都在图里但没有直接边 → 节点 2、边 0，不崩"""
        r = draw("甲|丙")
        hl_nodes = {n["id"] for n in r["nodes"] if n.get("hl")}
        self.assertEqual(hl_nodes, {"甲", "丙"})
        self.assertEqual(r["hl_edge_hits"], 0)

    def test_hl_not_in_graph(self):
        """hl 目标不在图里（被 min_degree/limit 裁掉）→ 无命中、不崩"""
        r = draw("查无此机构")
        self.assertEqual(r["hl_hits"], 0)
        self.assertEqual(r["hl_edge_hits"], 0)
        self.assertFalse(any(n.get("hl") for n in r["nodes"]))
        self.assertFalse(any(e.get("hl") for e in r["edges"]))


class TestHlExportParity(unittest.TestCase):
    """③ 导出 HTML 必须和屏幕上是同一个功能、同一个颜色

    ⚠ 必须**真调 pyvis_html**（tests_graph_find 同款教训：
       grep 源码是快照，不是性质 —— 那串颜色字面量在不在 HTML 里，
       只有真调才知道）。
    """

    @classmethod
    def setUpClass(cls):
        from cacx.web.app import pyvis_html
        cls.pyvis_html = staticmethod(pyvis_html)
        cls.html = (ROOT / "cacx" / "web" / "static" / "index.html").read_text(
            encoding="utf-8")

    def _nodes(self, hl=False, hit=False):
        return [{"id": "甲", "label": "甲", "group": "org", "size": 16,
                 "title": "", "hl": hl, "hit": hit},
                {"id": "乙", "label": "乙", "group": "org", "size": 16,
                 "title": "", "hl": False, "hit": False}]

    def _edges(self, hl=False, hit=False):
        return [{"from": "甲", "to": "乙", "weight": 3.0,
                 "hl": hl, "hit": hit}]

    def test_export_applies_hl_color(self):
        """hl 命中的节点/边，导出件里必须带高亮色"""
        on = self.pyvis_html(self._nodes(hl=True), self._edges(hl=True),
                             height="400px")
        self.assertIn(HIT_HEX, on, "hl 命中时导出件里没有高亮色")

    def test_export_clean_when_no_hl(self):
        """反向的那一半：没有 hl 时**不能**有高亮色 ——
        少了这条，只把常量改成永远 True 的测试也能过"""
        off = self.pyvis_html(self._nodes(False), self._edges(False),
                              height="400px")
        self.assertNotIn(HIT_HEX, off, "未聚焦却仍带高亮色 —— 底色被污染了")

    def test_export_hl_and_hit_coexist(self):
        """hl 与 find 可并存：两个都命中时同样上色（不互相覆盖）"""
        on = self.pyvis_html(self._nodes(hl=True, hit=True),
                             self._edges(hl=True, hit=True), height="400px")
        self.assertIn(HIT_HEX, on)

    # ---- 前端接线（结构断言：点击聚焦必须走独立参数，且不删原交互）----
    def test_frontend_sends_hl_separately(self):
        """hl 必须**独立**于 find 发 —— 合并会退化成 find 的「两端都命中」"""
        self.assertIn("q.set('hl', GP_HL)", self.html)
        self.assertIn("q.set('find', fd)", self.html)

    def test_frontend_has_hl_state(self):
        self.assertIn("let GP_HL = ''", self.html)
        self.assertIn("GP_DS_NODES", self.html)
        self.assertIn("GP_DS_EDGES", self.html)

    def test_frontend_render_uses_hl(self):
        """渲染分支必须同时认 hit 与 hl —— 只认 hit 的话点击聚焦不显色"""
        self.assertIn("n.hit || n.hl", self.html)
        self.assertIn("e.hit || e.hl", self.html)

    def test_frontend_click_keeps_original_and_adds_focus(self):
        """点击在**原交互里追加**：边→原文证据、节点→focusNode 都不能丢"""
        self.assertIn("showEdgeEvidence(e._from, e._to", self.html)
        self.assertIn("focusNode(p.nodes[0])", self.html)
        self.assertIn("gpTapNode(", self.html)
        self.assertIn("gpTapEdge(e)", self.html)

    def test_frontend_toggle_and_blank(self):
        """再点同一项取消（toggle）；点空白回指引"""
        self.assertIn("GP_HL === id", self.html)
        self.assertIn("GP_HL === pair", self.html)
        self.assertIn("gpTapBlank()", self.html)
        self.assertIn("点击节点或关系可聚焦", self.html)

    def test_frontend_reset_clears_hl(self):
        """清空筛选必须连点击聚焦一起复位 —— 不清的话重置后图还是黄的"""
        m = re.search(r"function gpReset\(\)\{[\s\S]*?\n\}", self.html)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("GP_HL = ''", body)


class TestTimelineDateRange(unittest.TestCase):
    """R4 需求 1：时间轴日期范围筛选

    前端：工具条两个日期控件（tl_from/tl_to，**不能叫 date_from/date_to**，
    后者与图谱页撞名会让 $() 取错元素）+ 筛选/恢复按钮 + loadTimeline 传参。
    后端：_timeline_impl 早已支持 date_from/date_to（app.py:353），
    路由也暴露了 —— 前端传过去就行；scope 回显是筛选后的数字。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "cacx" / "web" / "static" / "index.html").read_text(
            encoding="utf-8")

    def test_date_controls_present(self):
        self.assertIn('id="tl_from"', self.html)
        self.assertIn('id="tl_to"', self.html)

    def test_controls_not_collide_with_graph_page(self):
        """时间轴日期控件必须用 tl_from/tl_to —— date_from/date_to 这两个
        id 已被 241-242 行占用，同文档重名会让 $() 取错元素。
        loadTimeline 读的必须是 tl_*，绝不能读 date_*。"""
        self.assertIn('id="date_from"', self.html)   # 别的功能在用（佐证撞名风险真实存在）
        m = re.search(r"function loadTimeline\(\)\{[\s\S]*?\n\}", self.html)
        body = m.group(0)
        self.assertIn("$('tl_from')", body)
        self.assertIn("$('tl_to')", body)
        self.assertNotIn("$('date_from')", body)
        self.assertNotIn("$('date_to')", body)

    def test_loadTimeline_passes_dates(self):
        m = re.search(r"function loadTimeline\(\)\{[\s\S]*?\n\}", self.html)
        self.assertIsNotNone(m, "没找到 loadTimeline")
        body = m.group(0)
        self.assertIn("q.set('date_from', tdf)", body)
        self.assertIn("q.set('date_to', tdt)", body)
        # 留空 = 不带参数 → 后端走默认全区间，行为与改动前完全一致
        self.assertIn("if(tdf) q.set('date_from', tdf)", body)

    def test_range_buttons_exist(self):
        self.assertIn("function tlApplyRange()", self.html)
        self.assertIn("function tlClearRange()", self.html)
        self.assertIn("onclick=\"tlApplyRange()\"", self.html)
        self.assertIn("onclick=\"tlClearRange()\"", self.html)


# ---------------------------------------------------------------- API
import sys as _sys
from fastapi.testclient import TestClient
from cacx.web import app as _A
MOD = _sys.modules["cacx.web.app"]


class _BaseTmpDB(unittest.TestCase):
    """临时库（绝不碰 data/cacx.db），照 tests_graph_api.py 的守门方式"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cacx_hl_")
        from cacx import db as D
        cls.orig_db = MOD._db
        cls.orig_cfg = MOD._cfg
        cfg = dict(cls.orig_cfg())
        cfg.setdefault("db", {})["path"] = os.path.join(cls.tmp, "t.db")
        cls.store = D.open_db(cfg)
        from cacx import config as _C
        real_db = (_C.PROJECT_ROOT / "data" / "cacx.db").resolve()
        got = Path(cls.store.conn.execute(
            "PRAGMA database_list").fetchone()[2]).resolve()
        assert got != real_db, f"测试误开了真库：{got}"
        MOD._db = lambda: cls.store
        MOD._cfg = lambda: cfg
        MOD.DB = cls.store
        cls.client = TestClient(MOD.app)
        cls.conn = cls.store.conn

    @classmethod
    def tearDownClass(cls):
        MOD._db = cls.orig_db
        MOD._cfg = cls.orig_cfg
        try:
            cls.store.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _seed_articles(cls):
        PAD = ("相关内容将依据有关法律法规逐步落实，各地区各部门应当加强"
               "协同联动，及时回应社会关切，持续完善相关工作机制，"
               "确保各项治理措施落到实处见到成效。")
        rows = [
            (1, "cac", "网信要闻",
             "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》",
             "http://x/1", "2024-03-05", 400, 0,
             "为进一步规范网络数据处理活动，切实保护公民个人信息权益，"
             "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》。"
             "通知要求各地网信部门会同公安机关开展网络安全专项检查，"
             "针对网络谣言、网络诈骗、未成年人沉迷等突出问题依法查处。"
             "各省级网信办要按照通知要求，结合本地实际制定实施方案。"
             "市场监管总局、教育部将联合开展监督检查，并对违规App下架处理。"
             "通知还要求加强对算法推荐和深度伪造信息的治理力度。" + PAD),
            (2, "cac", "网信要闻", "国家网信办部署未成年人网络保护专项行动",
             "http://x/2", "2025-06-11", 320, 0,
             "国家网信办有关部门近日部署未成年人网络保护专项行动，"
             "要求各地网信部门会同公安机关开展专项检查，"
             "针对未成年人沉迷等突出问题依法查处，"
             "并会同市场监管总局、教育部加强网络环境治理。"
             "专项行动重点整治网络暴力、网络诈骗等涉未成年人网络乱象，"
             "对违规App下架处理，并对相关平台负责人依法约谈整改。" + PAD),
            (3, "cac", "网信要闻", "中央网信办、公安部联合开展网络谣言整治",
             "http://x/3", "2026-02-20", 300, 0,
             "中央网信办、公安部联合开展网络谣言整治专项行动，"
             "针对网络谣言、网络暴力等社会问题依法查处，"
             "各地网信部门应当按照统一部署推进治理工作。"
             "有关部门还组织开展了水军有偿删帖专项治理，" + PAD +
             "对造谣账号依法封禁处置，并发布了典型案例通报。"),
        ]
        cols = "id,site,column_name,title,url,pub_day,chars,content_empty,content_text"
        ph = ",".join("?" * len(rows[0]))
        cls.conn.executemany(f"INSERT INTO articles({cols}) VALUES({ph})", rows)
        cls.conn.commit()


class TestTimelineApi(_BaseTmpDB):
    """时间轴接口：date_from/date_to 真的会过滤月份（临时库三篇跨年）"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed_articles()

    def _months(self, date_from="", date_to=""):
        q = "?include_ai=false&months=0&top=6"
        if date_from:
            q += "&date_from=" + date_from
        if date_to:
            q += "&date_to=" + date_to
        r = self.client.get("/api/panel/timeline" + q)
        self.assertEqual(r.status_code, 200, r.text[:400])
        d = r.json()
        self.assertTrue(d["months"], "时间轴空了，种子数据没进来")
        return [m["month"] for m in d["months"]]

    def test_full_range_has_all_years(self):
        ms = self._months()
        joined = " ".join(ms)
        self.assertIn("2024", joined)
        self.assertIn("2025", joined)
        self.assertIn("2026", joined)

    def test_date_range_filters_months(self):
        """选 2025 全年 → 只剩 2025 的月份，2024/2026 都不该出现"""
        ms = self._months("2025-01-01", "2025-12-31")
        self.assertTrue(ms, "筛选后时间轴不应为空")
        for m in ms:
            self.assertTrue(m.startswith("2025-"),
                            f"范围外月份混进来了：{m}")

    def test_scope_reflects_filtered(self):
        """tl_note 回显（scope）必须是筛选后的数字"""
        r = self.client.get(
            "/api/panel/timeline?include_ai=false&months=0"
            "&date_from=2025-01-01&date_to=2025-12-31")
        d = r.json()
        self.assertIn("2025-01-01", d["scope"]["range"])
        # 全文 3 篇里只有 1 篇在 2025 —— 合计不能是 3
        self.assertLess(d["scope"]["articles_total"], 3)
        self.assertGreaterEqual(d["scope"]["articles_total"], 1)


class TestHlApi(_BaseTmpDB):
    """hl 的 HTTP 层：/api/graph/analysis 与 /api/graph/vis 都接 hl"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed_articles()
        # 先抽取，才有关系和机构节点可点
        cls.client.post("/api/extract/run", json={
            "site": "cac", "column": "网信要闻", "limit": 10,
            "strategy": "stratified", "write": True})

    def test_analysis_accepts_hl(self):
        row = self.conn.execute(
            "SELECT src_name FROM relations LIMIT 1").fetchone()
        if not row:
            self.skipTest("临时库没抽出关系")
        name = row[0]
        r = self.client.get(f"/api/graph/analysis?min_degree=1&hl={name}")
        self.assertEqual(r.status_code, 200, r.text[:400])
        g = r.json()["graph"]
        self.assertIn("hl", g)
        self.assertIn("hl_hits", g)
        self.assertIn("hl_edge_hits", g)
        # 目标节点必然被点亮（它在图上）
        hit_ids = {n["id"] for n in g["nodes"] if n.get("hl")}
        self.assertIn(name, hit_ids)
        self.assertGreaterEqual(g["hl_hits"], 1)

    def test_analysis_hl_unknown_no_crash(self):
        r = self.client.get("/api/graph/analysis?min_degree=1&hl=查无此机构")
        self.assertEqual(r.status_code, 200, r.text[:400])
        self.assertEqual(r.json()["graph"]["hl_hits"], 0)

    def test_vis_accepts_hl_and_colors(self):
        """导出件必须带聚焦色（「屏幕 = 导出」的硬保证）"""
        row = self.conn.execute(
            "SELECT src_name FROM relations LIMIT 1").fetchone()
        if not row:
            self.skipTest("临时库没抽出关系")
        name = row[0]
        r = self.client.get(f"/api/graph/vis?min_degree=1&limit=200&hl={name}")
        self.assertEqual(r.status_code, 200, r.text[:300])
        d = r.json()
        if "error" in d:
            self.skipTest(d["error"])
        self.assertIn(HIT_HEX, d["html"],
                      "hl 聚焦后导出的 HTML 里没有高亮色")

    def test_vis_param_alignment(self):
        """/api/graph/vis 参数必须与 /api/graph/analysis 对齐 ——
        少接一个筛选参数，导出件就和屏幕对不上"""
        import inspect
        from cacx.web.app import api_graph_vis, api_graph_analysis
        sig_vis = inspect.signature(api_graph_vis).parameters
        sig_an = inspect.signature(api_graph_analysis).parameters
        for name in ("hl", "find", "focus", "hop", "keyword",
                     "site", "date_from", "date_to", "node_types"):
            self.assertIn(name, sig_vis, f"vis 缺参数 {name}")
            self.assertIn(name, sig_an, f"analysis 缺参数 {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
