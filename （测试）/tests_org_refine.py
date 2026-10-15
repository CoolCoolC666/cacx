# -*- coding: utf-8 -*-
"""
T8：**通用形态闸门** + 粗选/精选两档 + 缺省说明。

背景：T7 用 `ORG_BANNED_TERMS` / `ORG_BANNED_TAILS` 逐条封堵了 7 个点名活动名，
但同族的**未点名**变体照样进榜 —— 实测（默认口径 date_from=2023-10-01，
4825 篇标题）粗选 Top12 里仍有「协会召开第二届理事会 45」
「第十一次全国代表大会 11」，Top50 里还有「周年文艺晚会 / IPv6创新发展大会 /
全球数字经济大会 / 第九届亚洲冬季运动会」。逐条拉黑是打地鼠。

所以改成判**形态**：`textseg.eventish_org(name) -> bool`。
两档口径：
  · 粗选 coarse = 只走 `org_like` 粗筛（**默认，与历史行为逐字一致**）
  · 精选 refine = 再过一道 `eventish_org`，并给出「滤掉了多少、哪些组缺省」

本文件覆盖：
  ① `eventish_org` 的**必命中**名单（活动/会议片段）
  ② `eventish_org` 的**必保护**名单（真建制；含真库 Top50 内的真机构）
  ③ 粗选/精选**同源**：`orgs + filtered == 粗选全部候选`（同一个集合的一刀两半）
  ④ `db.top_orgs_refine` 的 refine 字段结构（临时库，绝不碰真库）
  ⑤ `/api/panel/orgs` 两档响应结构（coarse 不带 refine 字段）
  ⑥ 图谱三个通道的 mode（/api/graph、/api/graph/analysis、/api/graph/facets）

⚠ 全部用**临时库**，绝不读写 data/cacx.db。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient                      # noqa: E402

from cacx import textseg                                       # noqa: E402
from cacx import db as D                                       # noqa: E402
from cacx.web import app as A                                  # noqa: E402

# ⚠ `from cacx.web import app as A` 拿到的是包属性（FastAPI 实例可能遮住同名
#   子模块），patch 内部函数必须走 sys.modules 取真模块。
MOD = sys.modules["cacx.web.app"]

# ---------------------------------------------------------------- 名单

#: 现场实测「必须命中（True = 精选模式滤掉）」—— 含用户点名的与真库 Top50 残留
MUST_FILTER = [
    "协会召开第二届理事会",          # 粗选 Top12 第 5 名（45）
    "第十一次全国代表大会",          # 粗选 Top12 第 12 名（11）
    "第九届亚洲冬季运动会",
    "全球数字经济大会",
    "IPv6创新发展大会",
    "周年文艺晚会",
    "第二届中国网络文明大会",
    "2024年世界互联网大会乌镇峰会",
    "的二十届四中全会",
    "人工智能发展论坛",
    "网络安全博览会",
    # 同族变体（拉黑主名后会顶上来的那些）
    "全国政协新年茶话会", "春节团拜会", "开幕会", "闭幕会",
    "60周年庆祝大会", "民族团结进步表彰大会", "发表70周年纪念大会",
    "世界人工智能大会", "中国新电商大会", "中国网络安全年会",
    "第十届世界互联网大会", "第七届数字中国建设峰会",
    # ⚠ 只有**条件 b（活动动词上下文）**才拦得住的两个：
    #   尾缀「理事会」被排除在「只看尾巴就定案」那批之外（保欧洲理事会），
    #   又没有序数、没有「的」—— 少了 b 它们就会漏。
    #   反证（拆掉 b）时红的就是这两条。
    "协会召开理事会", "知名企业出席理事会",
]

#: 现场实测「必须保护（False = 保留）」—— 用户点名的防误伤名单
MUST_KEEP = [
    "中共中央", "中央委员会", "第二十届中央委员会", "全国人民代表大会",
    "中国人民政治协商会议", "中国人民银行", "中央网信办举报中心",
    "中央网络安全和信息化委员会", "公安部网安局", "国家市场监督管理总局",
    "国务院办公厅", "农业农村部", "全国人大常委会", "最高人民法院",
    "最高人民检察院", "中央军委", "国家发展改革委", "福建省委网信办",
    "世界互联网大会组委会",
]

#: 真库 Top50 内的真机构 + 归并探针撞出来的真建制（同一批，都不许动）
MUST_KEEP_REAL_DB = [
    "欧洲理事会", "欧盟理事会", "亚奥理事会",       # 尾「理事会」的真建制
    "杭州市临安区检察院", "杭州互联网法院", "新疆生产建设兵团党委",
    "国家互联网应急中心", "浙江省人民政府办公厅",
    "十四届全国人大常委会", "政协第十四届全国委员会",
    "中央网信办", "国家网信办", "工业和信息化部", "教育部", "民政部",
    "市场监管总局", "中国互联网发展基金会", "中国网络空间安全协会",
    "国务院", "党委网信办", "省委网信办", "市委网信办", "国家铁路局",
    "人力资源社会保障部", "财政部", "中国证监会", "广电总局",
]

#: ⚠ 已知边界（**如实记录，不断言「必须滤」**）：这些是**主题残片**或
#:   被建制词保护的串，不是活动形态，闸门判 False、仍在精选榜上。
#:   本文件只断言它们「不崩」，不把它们算成闸门的能力。
KNOWN_GAPS = [
    "安全治理新格局", "智能社会", "经济社会", "基层干部", "建设书香社会",
    "制定国民经济和社会", "大会人工智能专委会", "世界互联网大会理事会",
]

BAD_TAIL_WORDS = ("峰会", "博览会", "研讨会", "交流会", "恳谈会", "推进会",
                  "发布会", "开幕式", "闭幕式", "周年大会", "中全会",
                  "互联网大会", "文明大会", "院士大会", "奖励大会",
                  "分享会", "对话会", "观摩会", "报告会", "招待会", "宴会")


class TestEventishOrgs(unittest.TestCase):
    """① 必命中名单：一个都不许漏。"""

    def test_活动名必须命中(self):
        for w in MUST_FILTER:
            with self.subTest(w=w):
                self.assertTrue(textseg.eventish_org(w),
                                f"形态闸门漏了「{w}」—— 它仍会挤占精选榜")

    def test_活动形态尾缀表是模块级常量且含关键尾巴(self):
        self.assertIsInstance(textseg.ORG_EVENT_TAILS, tuple)
        for t in BAD_TAIL_WORDS:
            self.assertIn(t, textseg.ORG_EVENT_TAILS, f"尾缀表少了「{t}」")

    def test_裸会_委员会_法院_检察院_政府不在活动形态表内(self):
        """防误杀：裸尾巴进表就是整类误杀（R5 §9 的老坑）。"""
        for t in ("会", "委员会", "法院", "检察院", "政府", "理事会即真建制"):
            self.assertNotIn(t, textseg.ORG_EVENT_TAILS, f"「{t}」不该在活动尾缀表")
        # 「理事会」在表内但**必须**从「只看尾巴就定案」那批里排除
        self.assertNotIn("理事会", textseg._EVENT_TAILS_PLAIN,
                         "「理事会」不能只凭尾巴定案（欧洲理事会/亚奥理事会）")

    def test_空串与标点不崩(self):
        for w in ("", None, "   ", "、", "（）", "第", "第届"):
            self.assertFalse(textseg.eventish_org(w), f"「{w!r}」不该判 True")


class TestRealOrgsNotHurt(unittest.TestCase):
    """② 必保护名单：真建制一律不许被闸门滤掉。"""

    def test_点名防误伤名单(self):
        for w in MUST_KEEP:
            with self.subTest(w=w):
                self.assertFalse(textseg.eventish_org(w),
                                 f"闸门误杀真建制「{w}」")

    def test_真库真机构不误杀(self):
        for w in MUST_KEEP_REAL_DB:
            with self.subTest(w=w):
                self.assertFalse(textseg.eventish_org(w),
                                 f"闸门误杀真机构「{w}」")

    def test_第二十届中央委员会不因序数被滤(self):
        """条件 c 的序数规则**不得**把「第…届」+「委员会」当真活动。"""
        self.assertFalse(textseg.eventish_org("第二十届中央委员会"))
        for w in ("第十四届全国委员会", "政协第十四届全国委员会"):
            self.assertFalse(textseg.eventish_org(w), f"误杀「{w}」")

    def test_已知边界只断言不崩(self):
        """这些闸门判不出（主题残片/建制词保护），如实记录而非假装覆盖。"""
        for w in KNOWN_GAPS:
            self.assertIn(textseg.eventish_org(w), (True, False))


class TestMineOrgsSameSource(unittest.TestCase):
    """③ 粗选/精选**同源**：同一段挖掘循环的两个投影。"""

    TITLES = [
        "协会召开第二届理事会第一次会议在京举行",
        "中国网络空间安全协会召开第二届理事会",
        "协会召开第二届理事会审议通过工作报告",
        "中央网信办、公安部联合部署网络生态治理专项行动",
        "中央网信办部署开展清朗系列专项行动",
        "国家网信办会同公安部开展专项检查",
        "全国人大常委会审议通过网络安全相关法律",
        "全国人大常委会举行专题讲座",
        "全球数字经济大会在北京开幕",
    ]

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_coarse含活动名_refine滤掉(self):
        coarse = {o["org"]: o["count"] for o in
                  textseg.mine_orgs(self.TITLES, min_count=1)}
        orgs, filtered = textseg.mine_orgs_refine(self.TITLES, min_count=1)
        fmap = {o["org"]: o["count"] for o in filtered}
        omap = {o["org"]: o["count"] for o in orgs}
        # 活动名：粗选在、精选不在
        hit = [k for k in fmap if "召开第二届理事会" in k]
        self.assertTrue(hit, f"精选没滤掉活动名，filtered={sorted(fmap)}")
        for k in hit:
            self.assertIn(k, coarse, "粗选口径不该被闸门影响")
            self.assertNotIn(k, omap, f"精选里仍有活动名「{k}」")
        # 真机构：两档都在
        for must in ("中央网信办", "全国人大常委会"):
            self.assertTrue(any(must in k for k in coarse), sorted(coarse))
            self.assertTrue(any(must in k for k in omap), sorted(omap))
            self.assertFalse(any(must in k for k in fmap), sorted(fmap))

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_两档同源_并集等于粗选全部候选(self):
        """()`orgs + filtered` 必须**恰好**等于粗选的全部候选：
        数量、计数逐项相同。不同源就说明两档挖的不是同一批东西。"""
        coarse = textseg.mine_orgs(self.TITLES, min_count=1, topn=400)
        orgs, filtered = textseg.mine_orgs_refine(self.TITLES, min_count=1,
                                                  topn=400)
        self.assertEqual(sorted(o["org"] for o in orgs + filtered),
                         sorted(o["org"] for o in coarse))
        cm = {o["org"]: o["count"] for o in coarse}
        for o in orgs + filtered:
            self.assertEqual(o["count"], cm[o["org"]],
                             f"「{o['org']}」两档计数不一致")
        # 交集必须为空（一刀两半，不是各留一份）
        self.assertFalse({o["org"] for o in orgs} & {o["org"] for o in filtered})

    @unittest.skipUnless(textseg.available(), "需要 jieba")
    def test_filtered带example_title且取首次出现(self):
        orgs, filtered = textseg.mine_orgs_refine(self.TITLES, min_count=1)
        self.assertTrue(filtered)
        for o in filtered:
            self.assertEqual(set(o), {"org", "count", "example_title"})
            self.assertIn(o["example_title"], self.TITLES)
            # 与 mine_orgs 的 surface 同一套逻辑：**第一次出现**该串的标题
            first = next(t for t in self.TITLES if o["org"] in t)
            self.assertEqual(o["example_title"], first)

    def test_没有jieba时不炸(self):
        """`_get()` 返回 None（无 jieba）时两档都返回空，而不是抛异常。"""
        orig = textseg._get
        textseg._get = lambda: None
        try:
            self.assertEqual(textseg.mine_orgs(["中央网信办"]), [])
            self.assertEqual(textseg.mine_orgs_refine(["中央网信办"]), ([], []))
        finally:
            textseg._get = orig


class BaseTmp(unittest.TestCase):
    """临时库 + TestClient（真库 data/cacx.db 绝不参与）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cacx_orgrefine_")
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
        cls.real_db = real_db

        # ⚠ 标题是**唯一**的输入：机构榜只读 articles.title
        rows = [
            (1, "cac", "网信要闻", "协会召开第二届理事会第一次会议在京举行",
             "http://x/1", "2025-03-05", 300, 0, "正文占位" * 60),
            (2, "cac", "网信要闻", "中国网络空间安全协会召开第二届理事会",
             "http://x/2", "2025-04-11", 300, 0, "正文占位" * 60),
            (3, "zjwx", "要闻", "协会召开第二届理事会审议通过工作报告",
             "http://x/3", "2025-05-20", 300, 0, "正文占位" * 60),
            (4, "cac", "网信要闻", "中央网信办、公安部联合部署网络生态治理",
             "http://x/4", "2025-06-01", 300, 0, "正文占位" * 60),
            (5, "cac", "网信要闻", "中央网信办部署开展清朗系列专项行动",
             "http://x/5", "2025-06-02", 300, 0, "正文占位" * 60),
            (6, "cac", "网信要闻", "国家网信办会同公安部开展专项检查",
             "http://x/6", "2025-06-03", 300, 0, "正文占位" * 60),
            (7, "cac", "网信要闻", "全国人大常委会审议通过网络安全相关法律",
             "http://x/7", "2025-06-04", 300, 0, "正文占位" * 60),
            (8, "cac", "网信要闻", "全球数字经济大会在北京开幕",
             "http://x/8", "2025-07-04", 300, 0, "正文占位" * 60),
        ]
        cols = ("id,site,column_name,title,url,pub_day,chars,"
                "content_empty,content_text")
        ph = ",".join("?" * len(rows[0]))
        cls.store.conn.executemany(
            f"INSERT INTO articles({cols}) VALUES({ph})", rows)
        cls.store.conn.commit()

        MOD._db = lambda: cls.store
        MOD._cfg = lambda: cfg
        MOD.DB = cls.store
        cls.client = TestClient(MOD.app)
        cls.conn = cls.store.conn

        # 实体/关系：让图谱三通道有东西可筛
        #   中央网信办 —— 真机构，必须留
        #   全球数字经济大会 —— 活动名，精选档必须剔
        cls.store.conn.executemany(
            "INSERT OR IGNORE INTO entities(name,type,article_id) VALUES(?,?,?)",
            [("中央网信办", "org", 1), ("中央网信办", "org", 2),
             ("中央网信办", "org", 3), ("全球数字经济大会", "org", 1),
             ("全球数字经济大会", "org", 2), ("全球数字经济大会", "org", 3),
             ("数据安全治理", "problem", 1), ("数据安全治理", "problem", 2),
             ("数据安全治理", "problem", 3)])
        cls.store.conn.executemany(
            "INSERT OR IGNORE INTO relations(src_name,dst_name,rel_type,"
            "article_id,weight,evidence) VALUES(?,?,?,?,?,'测试证据')",
            [("中央网信办", "数据安全治理", "issue", 1, 1.0),
             ("中央网信办", "数据安全治理", "issue", 2, 1.0),
             ("全球数字经济大会", "数据安全治理", "issue", 3, 1.0),
             ("全球数字经济大会", "中央网信办", "joint_issue", 4, 1.0)])
        cls.store.conn.commit()

    @classmethod
    def tearDownClass(cls):
        MOD._db = cls.orig_db
        MOD._cfg = cls.orig_cfg
        try:
            cls.store.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestTopOrgsRefineShape(BaseTmp):
    """④ db.top_orgs_refine 的返结构（临时库）。"""

    def test_refine字段结构(self):
        r = self.store.top_orgs_refine(site="", date_from="2020-01-01",
                                       date_to="2099-12-31", limit=12)
        self.assertEqual(set(r), {"items", "refine"})
        rf = r["refine"]
        self.assertEqual(rf["mode"], "refine")
        self.assertIsInstance(rf["filtered_total"], int)
        self.assertIsInstance(rf["filtered_n"], int)
        self.assertGreater(rf["filtered_total"], 0)
        self.assertLessEqual(len(rf["filtered_groups"]), 5,
                             "缺省说明的组最多 5 个（前端按 5 判是否截断）")
        for g in rf["filtered_groups"]:
            self.assertEqual(set(g), {"org", "count"})
            self.assertIsInstance(g["count"], int)
        # 活动名必须出现在被滤组里，且不在 items 里
        names = [g["org"] for g in rf["filtered_groups"]]
        self.assertTrue(any("召开第二届理事会" in n or "数字经济大会" in n
                            for n in names), names)
        for it in r["items"]:
            self.assertFalse(textseg.eventish_org(it["org"]),
                             f"精选榜里混进了活动名「{it['org']}」")

    def test_top_orgs的mode_refine等于refine_items(self):
        a = self.store.top_orgs(site="", date_from="2020-01-01",
                               date_to="2099-12-31", limit=12)
        b = self.store.top_orgs(site="", date_from="2020-01-01",
                                date_to="2099-12-31", limit=12, mode="refine")
        c = self.store.top_orgs_refine(site="", date_from="2020-01-01",
                                       date_to="2099-12-31", limit=12)["items"]
        self.assertEqual([x["org"] for x in b], [x["org"] for x in c])
        # 粗选口径含活动名（默认档行为不能被闸门改掉）
        self.assertTrue(any(textseg.eventish_org(x["org"]) for x in a)
                        or any("召开第二届理事会" in x["org"] for x in a),
                        f"粗选榜应仍含活动名：{[x['org'] for x in a]}")


class TestPanelOrgsApi(BaseTmp):
    """⑤ /api/panel/orgs 两档响应结构。"""

    # ⚠ 参数串**不带问号**，由各条测试自己拼第一个 ? —— 拼成
    #   「?which=orgs?date_from=…」时第二个问号会被当成 which 的一部分，
    #   后端只会回一句「不支持的图表」，测试却看起来只是状态码不对。
    QPARAMS = "date_from=2020-01-01&date_to=2099-12-31&limit=12"

    def test_coarse不带refine字段(self):
        d = self.client.get("/api/panel/orgs?" + self.QPARAMS).json()
        self.assertNotIn("refine", d, "粗选档不该有 refine 字段（前端靠它判档）")
        self.assertIn("items", d)
        self.assertIn("note", d)

    def test_refine带缺省说明(self):
        d = self.client.get("/api/panel/orgs?" + self.QPARAMS +
                            "&mode=refine").json()
        rf = d.get("refine")
        self.assertIsNotNone(rf, "精选档必须给缺省说明")
        self.assertEqual(rf["mode"], "refine")
        self.assertGreater(rf["filtered_total"], 0)
        self.assertIn("org", rf["filtered_groups"][0])
        self.assertIn("count", rf["filtered_groups"][0])
        self.assertIn("note", rf)
        self.assertIn("精选", rf["note"])
        # 精选榜里不得再有活动名
        for it in d["items"]:
            self.assertFalse(textseg.eventish_org(it["org"]), it["org"])

    def test_直接调函数_默认mode是Query对象也不崩(self):
        """⚠ 回归：默认值是 FastAPI 的 `Query` 实例，直接调函数时
        `(mode or '').strip()` 会 AttributeError（既有测试就是这么调的）。"""
        r = MOD.api_panel_orgs(site="", limit=12, date_from="2020-01-01",
                               date_to="2099-12-31")
        self.assertNotIn("refine", r)
        from fastapi import Query
        r2 = MOD.api_panel_orgs(site="", limit=12, date_from="2020-01-01",
                                date_to="2099-12-31", mode=Query("coarse"))
        self.assertNotIn("refine", r2)
        self.assertTrue(MOD._refine_mode_on("refine"))
        self.assertFalse(MOD._refine_mode_on("Coarse"))
        self.assertFalse(MOD._refine_mode_on(Query("coarse")))

    def test_导出与快照同样吃mode(self):
        """「屏幕=导出」：精选档下导出的机构榜必须也是精选那份。"""
        base = "/api/panel/export?which=orgs&" + self.QPARAMS
        c = self.client.get(base)
        r = self.client.get(base + "&mode=refine")
        self.assertEqual(c.status_code, 200, c.text[:300])
        self.assertEqual(r.status_code, 200, r.text[:300])
        crows = c.text.strip().splitlines()[1:]
        rrows = r.text.strip().splitlines()[1:]
        self.assertNotEqual(crows, rrows,
                            "导出没吃 mode：粗选与精选导出同一份")
        self.assertTrue(any("召开第二届理事会" in x for x in crows), crows)
        for line in rrows:
            self.assertFalse("召开第二届理事会" in line, line)
        snap = self.client.get("/api/panel/snapshot?top=12&" + self.QPARAMS +
                              "&mode=refine")
        self.assertEqual(snap.status_code, 200, snap.text[:300])


class TestGraphModes(BaseTmp):
    """⑥ 图谱三通道的 mode。"""

    def test_graph_coarse带活动名_无refined_count(self):
        d = self.client.get("/api/graph?limit=300").json()
        names = [n["name"] for n in d["nodes"]]
        self.assertIn("全球数字经济大会", names)
        self.assertNotIn("refined_count", d, "粗选档不该给 refined_count")

    def test_graph_refine剔活动名节点与边(self):
        c = self.client.get("/api/graph?limit=300").json()
        d = self.client.get("/api/graph?limit=300&mode=refine").json()
        names = [n["name"] for n in d["nodes"]]
        self.assertNotIn("全球数字经济大会", names)
        self.assertIn("中央网信办", names)
        for e in d["edges"]:
            self.assertNotIn("全球数字经济大会",
                             (e["src_name"], e["dst_name"]))
        self.assertIn("refined_count", d)          # refine 档 0 也带
        self.assertGreater(d["refined_count"], 0)
        self.assertEqual(len(d["nodes"]), len(c["nodes"]) - 1)
        self.assertLess(len(d["edges"]), len(c["edges"]))

    def test_analysis_refine并入blocked且计数同口径(self):
        q = "?min_degree=1&limit=300&topn=20&min_weight=1.0"
        c = self.client.get("/api/graph/analysis" + q).json()
        r = self.client.get("/api/graph/analysis" + q + "&mode=refine").json()
        cn = {n["id"] for n in c["graph"]["nodes"]}
        rn = {n["id"] for n in r["graph"]["nodes"]}
        self.assertIn("全球数字经济大会", cn)
        self.assertNotIn("全球数字经济大会", rn)
        self.assertNotIn("refined_count", c["filters"], "粗选不该带")
        self.assertEqual(r["filters"]["refined_count"], 1)
        # 图/数字同口径：typed 计数必须等于实际节点数
        self.assertEqual(r["filters"]["graph_nodes_typed"], len(rn))
        self.assertEqual(r["filters"]["graph_edges_typed"],
                         len(r["graph"]["edges"]))
        # 图例 by_group 里 org 计数也要跟着少
        cg = {x["group"]: x["n"] for x in c["graph"]["by_group"]}
        rg = {x["group"]: x["n"] for x in r["graph"]["by_group"]}
        self.assertEqual(rg.get("org"), cg.get("org") - 1)

    def test_facets_refine滤掉焦点候选(self):
        c = self.client.get("/api/graph/facets?limit=60").json()
        r = self.client.get("/api/graph/facets?limit=60&mode=refine").json()
        cn = [x["name"] for x in c["focus_candidates"]]
        rn = [x["name"] for x in r["focus_candidates"]]
        self.assertIn("全球数字经济大会", cn)
        self.assertNotIn("全球数字经济大会", rn)
        self.assertIn("中央网信办", rn)
        # 筛子的候选说明口径（rel_types）不跟着收紧
        self.assertEqual(c.get("rel_types"), r.get("rel_types"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
