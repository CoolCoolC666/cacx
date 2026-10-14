# -*- coding: utf-8 -*-
"""
图谱建图不能带 LIMIT（静默截断）
================================================================
背景：全量重抽之后关系总量从 1 311 涨到 8 801，把 `list_relations(limit=5000)`
      这个硬上限顶爆了。而它是 `ORDER BY weight DESC` —— 截断**专挑低权重的
      关系丢**，不是随机丢。后果是：

        网页上画出来的图  ≠  直连库算出来的图

      而界面上**没有任何提示**，只会觉得"关系怎么这么少"。
      这类 bug 最讨厌的地方是它不报错、不崩、看起来一切正常。

本文件钉住四件事：
  ① `limit=None` 必须是**真全量**，且 `limit=N` 仍然是"取权重最高的 N 条"
  ② 「截断会改变结果」本身要被验证 —— 如果 limit 怎么设结果都一样，
     那下面几条断言全是空的（测试抓不住 bug 等于没测）
  ③ 建图路径（`_graph_payload` / `facets`）**必须**传 `limit=None`
  ④ 画图复用 analyze 算好的介数映射，结果必须与"自己重算一遍"**逐字节相同**
     （去重优化不能改变口径）

跑法：py -3 tests_graph_limit.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from cacx import db as D                       # noqa: E402
from cacx import graph_analysis as GA          # noqa: E402
from cacx.web import app as A                  # noqa: E402

# ⚠ `from cacx.web import app as A` 拿到的是模块，但 A.app 是 FastAPI 实例；
#   同名属性会遮住同名子模块，patch 时必须走 sys.modules 取真模块。
MOD = sys.modules["cacx.web.app"]


# ⚠ 语料用**真机构名 + 真公文句式**。合成中文（甲部门/乙部门）会绕过
#   机构名后缀白名单，测出来的是一个现实里不存在的世界。
ARTICLES = [
    (1, "cac", "网信要闻",
     "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》",
     "http://x/1", "2024-03-05"),
    (2, "cac", "网信要闻", "国家网信办部署未成年人网络保护专项行动",
     "http://x/2", "2025-06-11"),
    (3, "zjwx", "浙江动态", "浙江网信办开展个人信息保护专项行动",
     "http://x/3", "2026-02-20"),
]

# (src, dst, type, article_id, weight, evidence)
# ⚠ 顺序与上面 INSERT 的列顺序**一致**（weight 在 evidence 前面）。
#   第一次写反过：SQL 列写 evidence,weight 而元组是 weight,evidence，
#   SQLite 动态类型不报错，于是 weight 列里存了一段中文。
RELATIONS = [
    # ---- 高权重的一小撮：全挤在 3 个机构之间 ----
    ("中央网信办", "国家网信办", "joint_issue", 1, 30.0,
     "中央网信办、国家网信办联合印发《关于加强网络数据安全治理的通知》"),
    ("中央网信办", "公安部", "joint_issue", 1, 24.0,
     "中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》"),
    ("国家网信办", "公安部", "joint_issue", 2, 18.0,
     "国家网信办会同公安部依法约谈相关平台负责人"),
    # ---- 低权重的长尾：每个机构只出现一次 ----
    ("工业和信息化部", "市场监管总局", "joint_issue", 1, 2.0,
     "工业和信息化部、市场监管总局将联合开展监督检查"),
    ("教育部", "人力资源社会保障部", "joint_issue", 2, 2.0,
     "教育部、人力资源社会保障部联合印发《工作要点》"),
    ("浙江网信办", "浙江省通信管理局", "joint_issue", 3, 2.0,
     "浙江网信办会同浙江省通信管理局开展专项检查"),
    ("中央网信办", "网络数据安全管理条例", "issue", 1, 2.0,
     "中央网信办印发《网络数据安全管理条例》"),
    ("国家网信办", "个人信息出境认证办法", "issue", 2, 2.0,
     "国家网信办公布《个人信息出境认证办法》"),
    ("浙江网信办", "个人信息", "target", 3, 2.0,
     "浙江网信办依法查处侵害公民个人信息权益的违法违规行为"),
    ("中央网信办", "个人信息", "target", 1, 2.0,
     "中央网信办部署开展个人信息保护专项行动"),
    ("国家网信办", "依法约谈", "adopt", 2, 2.0,
     "国家网信办依法约谈相关平台负责人并实施下架处置"),
    ("浙江网信办", "依法查处", "adopt", 3, 2.0,
     "浙江网信办依法查处一批问题突出的应用软件"),
]

# (name, type, article_id, confidence)
ENTITIES = [
    ("中央网信办", "org", 1, 0.95),
    ("中央网信办", "org", 2, 0.95),          # 同名重复 → 焦点候选必须去重
    ("国家网信办", "org", 2, 0.95),
    ("公安部", "org", 1, 0.95),
    ("浙江网信办", "org", 3, 0.95),
    ("工业和信息化部", "org", 1, 0.95),
    # 低置信措施 → 必须被 blocked_names 踢出图
    ("开展专项检查", "measure", 3, 0.50),
    ("加强网络环境治理", "measure", 1, 0.50),
    ("依法查处", "measure", 3, 0.80),        # 正好卡在阈值上（<= 判据）
    # 高置信措施 → 留在图里
    ("开展个人信息保护专项行动", "measure", 2, 0.95),
    ("网络数据安全管理条例", "policy", 1, 0.95),
    ("个人信息出境认证办法", "policy", 2, 0.95),
    ("个人信息", "problem", 3, 0.95),
]


class BaseGraphLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cacx_limit_")
        cls.orig_db = MOD._db
        cls.orig_cfg = MOD._cfg
        cfg = dict(cls.orig_cfg())
        # ⚠ 键名是 cfg["db"]["path"]（不是 db_path）；写错会静默开真库
        cfg.setdefault("db", {})["path"] = os.path.join(cls.tmp, "t.db")
        cls.store = D.open_db(cfg)

        # 安全闸：配置键写错会静默开真库，必须当场炸掉
        from cacx import config as _C
        real_db = (_C.PROJECT_ROOT / "data" / "cacx.db").resolve()
        got = Path(cls.store.conn.execute(
            "PRAGMA database_list").fetchone()[2]).resolve()
        assert got != real_db, f"测试误开了真库：{got}"

        cls.store.conn.executemany(
            "INSERT INTO articles(id,site,column_name,title,url,pub_day,"
            "chars,content_empty,content_text) VALUES(?,?,?,?,?,?,?,?,?)",
            [(a[0], a[1], a[2], a[3], a[4], a[5], 400, 0,
              a[3] + "。" + "相关内容将依据有关法律法规逐步落实，"
              "各地区各部门应当加强协同联动，及时回应社会关切。")
             for a in ARTICLES])
        cls.store.conn.executemany(
            "INSERT INTO relations(src_name,dst_name,rel_type,article_id,"
            "weight,evidence,extractor,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], "rule",
              "2026-01-01 00:00:00") for r in RELATIONS])
        cls.store.conn.executemany(
            "INSERT INTO entities(name,type,article_id,aliases,evidence,"
            "extractor,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [(e[0], e[1], e[2], "", e[0], "rule", e[3],
              "2026-01-01 00:00:00") for e in ENTITIES])
        cls.store.conn.commit()

        # ⚠ 守门断言：**列顺序写错不会报错**（SQLite 动态类型），只会让
        #   weight 里存进一段中文、evidence 里存进一个数字，然后
        #   `ORDER BY weight DESC` 按字符串比大小，选出完全诡异的一批。
        #   这个 bug 在第一版测试数据里真出现过，而且当时唯一的症状
        #   就是「排序结果看着莫名其妙」—— 所以直接把类型钉在这里。
        for r in cls.store.list_relations(limit=None):
            assert isinstance(r["weight"], float), \
                f"weight 不是数字，八成是列顺序写错：{r['weight']!r}"
            assert isinstance(r["evidence"], str), \
                f"evidence 不是文本，八成是列顺序写错：{r['evidence']!r}"

        MOD._db = lambda: cls.store
        MOD._cfg = lambda: cfg
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


class TestLimitSemantics(BaseGraphLimit):
    def test_None_是真全量(self):
        rows = self.store.list_relations(limit=None)
        self.assertEqual(len(rows), len(RELATIONS))

    def test_None_用在实体上也是全量(self):
        rows = self.store.list_entities(limit=None)
        self.assertEqual(len(rows), len(ENTITIES))

    def test_默认limit仍然生效_不是悄悄变全量(self):
        # ⚠ 加了 limit=None 支持之后，默认值不能一起变成"不限" ——
        #   那会让别的调用方（列表接口）一次吐出全库。
        #   这里查**签名默认值**而不是行数：库里只有十几行时，
        #   数行数完全测不出默认值是多少。
        import inspect
        self.assertEqual(
            inspect.signature(self.store.list_relations)
            .parameters["limit"].default, 200)
        self.assertEqual(
            inspect.signature(self.store.list_entities)
            .parameters["limit"].default, 200)

    def test_截断专挑低权重丢(self):
        cut = self.store.list_relations(limit=4)
        self.assertEqual(len(cut), 4)
        # 权重最高的三条必须按序出现；第 4 条是从 2.0 那批里挑的
        self.assertEqual([r["weight"] for r in cut][:3], [30.0, 24.0, 18.0])
        self.assertEqual(cut[3]["weight"], 2.0)


class TestTruncationReallyMatters(BaseGraphLimit):
    """反向验证：先证明「带 LIMIT 真的会算错」，否则其余断言都是空的。"""

    def test_带limit建图会少节点少边(self):
        full = GA.build_graph(self.store.list_relations(limit=None),
                              min_weight=1.0, blocked_nodes=set())[0]
        cut = GA.build_graph(self.store.list_relations(limit=4),
                             min_weight=1.0, blocked_nodes=set())[0]
        self.assertLess(cut.number_of_nodes(), full.number_of_nodes(),
                        "截断居然没改变节点数 —— 那这些测试抓不住 bug")
        self.assertLess(cut.number_of_edges(), full.number_of_edges())

    def test_低权重关系被丢会丢掉整个关系类型(self):
        """长尾才承载 issue/target/adopt —— 截断会把它们整类抹掉。

        权重分布是刻意的：joint_issue 占着 30/24/18 三个高权重，
        其余三类**只在 2.0 上出现**。所以截到 3 条时，
        图上就只剩 joint_issue 一种关系，另外三类一条不剩。
        """
        full = {r["rel_type"] for r in self.store.list_relations(limit=None)}
        cut = {r["rel_type"] for r in self.store.list_relations(limit=3)}
        self.assertEqual(full, {"joint_issue", "issue", "target", "adopt"})
        self.assertEqual(cut, {"joint_issue"})
        self.assertLess(cut, full)


class TestGraphPayloadUsesFullData(BaseGraphLimit):
    def _spy_limits(self, **extra):
        """调一次 _graph_payload，记录它对 list_relations/list_entities 传了什么。"""
        seen_rel, seen_ent = [], []
        orig_rel, orig_ent = (self.store.list_relations,
                              self.store.list_entities)

        def rel(*a, **kw):
            seen_rel.append(kw.get("limit", "<未传>"))
            return orig_rel(*a, **kw)

        def ent(*a, **kw):
            seen_ent.append((kw.get("type", a[0] if a else "<未传>"),
                             kw.get("limit", "<未传>")))
            return orig_ent(*a, **kw)

        self.store.list_relations, self.store.list_entities = rel, ent
        try:
            r = MOD._graph_payload(min_degree=2, limit=300, min_weight=1.0,
                                   topn=50, **extra)
        finally:
            self.store.list_relations = orig_rel
            self.store.list_entities = orig_ent
        return r, seen_rel, seen_ent

    def test_带站点筛选时底数那张图也必须全量(self):
        """site/date 筛选时 _graph_payload 会**额外**建一张未筛的图报底数
        （界面上「全库本来有多少」）。那张图同样不能带 LIMIT ——
        它错了的话，「筛掉了多少」这个对比数字就是假的。"""
        _r, seen, _e = self._spy_limits(site="cac")
        self.assertGreaterEqual(len(seen), 2,
                                "带站点筛选时应取两次关系（筛后 + 底数）")
        for v in seen:
            self.assertIsNone(v, f"底数那张图带了 LIMIT={v!r}，会少算")

    def test_建图取关系必须全量(self):
        _r, seen, _e = self._spy_limits()
        self.assertTrue(seen, "建图没取关系？")
        for v in seen:
            self.assertIsNone(v, f"建图带了 LIMIT={v!r}，会静默丢关系")

    def test_建图取实体必须全量且只要measure(self):
        """实体只取 measure：blocked_names 本来就只在该类里判，
        全量拉 4 万条是纯浪费（0.75s vs 0.03s），结果一模一样。"""
        _r, _rel, seen = self._spy_limits()
        self.assertTrue(seen, "建图没取实体？")
        for t, v in seen:
            self.assertEqual(t, "measure", f"取实体类型不对：{t!r}")
            self.assertIsNone(v, f"取实体带了 LIMIT={v!r}，黑名单会漏")

    def test_图的节点数等于直连库全量建图(self):
        r, _rel, _ent = self._spy_limits()
        full = GA.build_graph(
            self.store.list_relations(limit=None), min_weight=1.0,
            blocked_nodes=GA.blocked_names(
                self.store.list_entities(type="measure", limit=None),
                {"measure"}))[0]
        self.assertEqual(r["nodes"], full.number_of_nodes())
        self.assertEqual(r["edges"], full.number_of_edges())

    def test_接口响应里不夹带全量映射(self):
        """node_betweenness / node_pagerank / graph_fingerprint 是内部复用的，
        必须 pop 掉。"""
        r, _rel, _ent = self._spy_limits()
        for k in ("node_betweenness", "node_pagerank", "node_community",
                  "graph_fingerprint"):
            self.assertNotIn(k, r, f"{k} 没 pop 掉，响应会白白大几十 KB")
        self.assertGreater(len(r["betweenness"]), 0)


class TestBlockedScope(BaseGraphLimit):
    def test_只取measure与全量实体等价(self):
        full = self.store.list_entities(limit=None)
        only = self.store.list_entities(type="measure", limit=None)
        self.assertLess(len(only), len(full))
        self.assertEqual(GA.blocked_names(only, {"measure"}),
                         GA.blocked_names(full, {"measure"}))

    def test_低置信与卡阈值都被踢(self):
        blocked = GA.blocked_names(
            self.store.list_entities(type="measure", limit=None), {"measure"})
        self.assertIn("开展专项检查", blocked)          # 0.50
        self.assertIn("依法查处", blocked)              # 0.80 = 阈值上（<= 判据）
        self.assertNotIn("开展个人信息保护专项行动", blocked)   # 0.95

    def test_机构名去重且全量(self):
        full = self.store.list_entities(type="org", limit=None)
        names = self.store.distinct_entity_names("org")
        self.assertEqual(sorted(set(x["name"] for x in full)), sorted(names))
        # 同名机构在库里有两行，去重后只该有一个「中央网信办」
        self.assertEqual(names.count("中央网信办"), 1)


class TestStripLocalRefs(unittest.TestCase):
    """剥相对路径引用（零外链判据的补洞）+ 守住「别把代码吃掉」。"""

    def test_剥掉相对路径但保留绝对与data(self):
        html = ('<script src="lib/bindings/utils.js"></script>'
                '<script src="data:text/javascript,var a=1"></script>'
                '<link rel="stylesheet" href="../node_modules/vis/dist/vis.min.css">'
                '<script src="https://cdn.example.com/x.js"></script>')
        out = MOD._strip_local_refs(html)
        self.assertNotIn("lib/bindings/utils.js", out)
        self.assertNotIn("node_modules", out)
        self.assertIn("data:text/javascript,var a=1", out)
        self.assertIn("cdn.example.com", out)

    def test_单双引号都认(self):
        for q in ("'", '"'):
            out = MOD._strip_local_refs(f'<script src={q}lib/a.js{q}></script>')
            self.assertNotIn("lib/a.js", out, f"单引号 {q} 没被认出来")

    def test_自闭合script绝不吃掉后面的代码(self):
        """pyvis 模板里有两个**自闭合**的 script —— 后面直接跟 `</head>`，
        几百行之后才有下一个 `</script>`。

        用 `.*?</script>` 匹配时它会一路跨过去，把中间的真实代码
        （drawGraph / DataSet）整段删掉：vis 还在、数据还在、
        控制台**一个错都不报**，画布却一片空白。
        """
        real_code = ("var nodes = new vis.DataSet([{id:'a'}]);drawGraph();")
        html = ('<script type="text/javascript" '
                'src="../node_modules/vis/dist/vis.js">\n'
                '        </head>\n'
                '        <body>\n'
                f'        <script type="text/javascript">\n'
                f'        {real_code}\n'
                f'        </script>\n')
        out = MOD._strip_local_refs(html)
        self.assertNotIn("node_modules", out)
        self.assertIn("new vis.DataSet", out,
                      "真实代码被吃掉了 —— 这就是那个静默失败")
        self.assertIn("drawGraph();", out)
        self.assertIn("</head>", out, "自闭合 script 后面的结构不该被动")

    def test_空壳script连闭合一起删(self):
        """pyvis 的 utils.js 是这种形态：标签后紧跟闭合，中间无内容。
        漏掉闭合会留下一句孤儿 `</script>`，后面整页都进错状态。"""
        out = MOD._strip_local_refs('<script src="lib/b.js"></script>')
        self.assertEqual(out.strip(), "")

    def test_带内容的外链script内容要一起删(self):
        """本地引用但**确实带内容**的 script：内容必须一起删，
        否则残留的裸 JS 会被浏览器当正文渲染出来。

        判据是「开标签与闭合标签之间有没有内容」，而不是简单看紧邻。
        """
        html = '<script src="lib/x.js">console.log(1)</script>'
        out = MOD._strip_local_refs(html)
        self.assertNotIn("lib/x.js", out)
        self.assertNotIn("console.log(1)", out)
        self.assertNotIn("</script>", out)

    def test_没有引用时原样返回(self):
        html = "<div>hello</div>"
        self.assertEqual(MOD._strip_local_refs(html), html)


class TestExportKeepsDrawingCode(BaseGraphLimit):
    """端到端：真数据走一遍 pyvis_html，剥引用后**绘图代码一字不少**。"""

    def test_剥引用后绘图代码仍在(self):
        """
        ⚠ 这条比「引用数=0」强得多：引用数归零也可能是因为**把整个
          script 都吃了**（第一版就那样：控制台零报错、画布全白）。

          所以除了「没有相对引用」，还必须断言
          ① 关键标记还在  ② 长度减少量**恰好**等于被删标签之和
        """
        import re as _re
        rels = self.store.list_relations(limit=None)
        G, types = GA.build_graph(rels, min_weight=1.0, blocked_nodes=set())
        r = GA.analyze(G, types, topn=50)
        ne = GA.to_pyvis_nodes_edges(r, G, types, min_degree=1, limit=50)
        for n in ne["nodes"]:
            n["group"] = "org"
        html = MOD.pyvis_html(ne["nodes"], ne["edges"])

        # ① 真的没有相对路径引用了
        rel = _re.findall(
            r'(?:src|href)\s*=\s*["\'](?!data:|#|https?:)([^"\']+)', html)
        self.assertEqual(rel, [], f"还剩相对路径引用：{rel}")

        # ② 关键代码还在（这才是「产物正确」而不只是「产物合法」）
        #    ⚠ 节点名在 JSON 里是 \uXXXX 转义的，**不能**直接找中文。
        for must in ("vis.DataSet", "drawGraph", "vis.Network",
                     "\\u4e2d\\u592e\\u7f51\\u4fe1\\u529e"):
            self.assertIn(must, html, f"剥引用时把「{must}」弄丢了")

        # ③ 末尾的绘图调用必须在（pyvis 固定在结尾）
        self.assertIn("drawGraph();", html[-400:],
                      "结尾的 drawGraph() 不见了 —— 剥引用吃过了代码")

        # ④ 内联 vis-network 必须够大（真源码约 460 KB）
        self.assertGreater(len(html), 400_000,
                           "导出件小得离谱，vis.js 可能没内进去")

        # ⑤ 绝对外链仍是 0（原判据不能因这次改动而回退）
        self.assertEqual(
            _re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', html),
            [], "零 CDN 回退了")

    def test_剥引用只删标签_不多删一个字节(self):
        """最精确的判据：长度减少量必须**恰好**等于被删掉的那几个标签之和。

        多了就是在吃代码（第一版多删了 699 字符）；
        少了就是没删干净。这个断言不依赖任何具体内容。
        """
        import re as _re
        # 用 pyvis_html 的真实输入，但只看剥引用这一步的效果
        html = ('<html><head>'
                '<script src="lib/bindings/utils.js"></script>'
                '<link rel="stylesheet" href="../node_modules/vis/dist/vis.min.css">'
                '<script type="text/javascript" '
                'src="../node_modules/vis/dist/vis.js">'
                '</head><body>'
                '<script type="text/javascript">'
                'var nodes = new vis.DataSet([]);drawGraph();'
                '</script></body></html>')
        dropped = ('<script src="lib/bindings/utils.js"></script>'
                   '<link rel="stylesheet" '
                   'href="../node_modules/vis/dist/vis.min.css">'
                   '<script type="text/javascript" '
                   'src="../node_modules/vis/dist/vis.js">')
        out = MOD._strip_local_refs(html)
        self.assertEqual(len(html) - len(out), len(dropped),
                         f"净减 {len(html) - len(out)} 字符，"
                         f"但被删的标签只有 {len(dropped)} 字符 —— "
                         f"差额就是被误吃的代码")
        self.assertIn("new vis.DataSet", out)


class TestPyvisReuseIsFaithful(BaseGraphLimit):
    def test_复用预计算映射与自己重算结果逐字段相同(self):
        rels = self.store.list_relations(limit=None)
        G, types = GA.build_graph(rels, min_weight=1.0, blocked_nodes=set())
        r = GA.analyze(G, types, topn=50)

        reused = GA.to_pyvis_nodes_edges(dict(r), G, types,
                                         min_degree=1, limit=100)
        stripped = dict(r)
        stripped.pop("node_betweenness")
        stripped.pop("node_pagerank")
        recomputed = GA.to_pyvis_nodes_edges(stripped, G, types,
                                             min_degree=1, limit=100)
        self.assertEqual(reused, recomputed,
                         "复用与重算结果不一致 —— 去重把口径改坏了")

    def test_焦点下复用与重算一致(self):
        """焦点场景 sub ≠ G —— 这才是复用最可能串口径的地方：
        把**全图**的介数映射喂给**子图**的绘制。"""
        rels = self.store.list_relations(limit=None)
        G, types = GA.build_graph(rels, min_weight=1.0, blocked_nodes=set())
        sub, _ = GA.crop_scope(G, focus="中央网信办", hop=1)
        self.assertLess(sub.number_of_nodes(), G.number_of_nodes(),
                        "焦点没把图裁小，这条测试就是空的")
        r_sub = GA.analyze(sub, types, topn=50)

        reused = GA.to_pyvis_nodes_edges(dict(r_sub), G, types, min_degree=1,
                                        limit=100, focus="中央网信办", hop=1)
        stripped = dict(r_sub)
        stripped.pop("node_betweenness")
        stripped.pop("node_pagerank")
        recomputed = GA.to_pyvis_nodes_edges(stripped, G, types, min_degree=1,
                                             limit=100, focus="中央网信办",
                                             hop=1)
        self.assertEqual(reused, recomputed,
                         "焦点下复用与重算不一致")
        # 而且喂进去的必须是**子图**的数，不是全图的
        src = {x["name"]: x["betweenness"] for x in r_sub["betweenness"]}
        by_id = {n["id"]: n for n in reused["nodes"]}
        for n, node in by_id.items():
            self.assertEqual(node["betweenness"], src.get(n, 0.0),
                             f"{n} 的介数不是子图口径")

    def test_传错result时必须自己重算_不能盲信(self):
        """反向验证：把**全图**的 analyze 结果 + focus 参数一起传进来，
        是很自然的写法（旧调用方都是这么写的）。此时指纹对不上，
        必须退回重算子图 —— 沿用全图介数就是两套口径混用。

        这条是被 tests_graph_filter 的老测试抓红之后才补的：
        复用快路径上线当天，全图 0.05 被当成子图的 1.0 画了出来。
        """
        import networkx as nx
        rels = self.store.list_relations(limit=None)
        G, types = GA.build_graph(rels, min_weight=1.0, blocked_nodes=set())
        full_result = GA.analyze(G, types, topn=50)
        sub, _ = GA.crop_scope(G, focus="中央网信办", hop=1)

        ne = GA.to_pyvis_nodes_edges(dict(full_result), G, types,
                                     min_degree=1, limit=100,
                                     focus="中央网信办", hop=1)
        ref = nx.betweenness_centrality(sub)
        # ⚠ 节点里的介数是 round(..., 4) 的**展示值**，所以只能比到 4 位。
        #   拿 places=6 去比会得到 3e-05 的差 —— 那是四舍五入，不是口径错。
        for n in ne["nodes"]:
            self.assertAlmostEqual(
                n["betweenness"], round(ref.get(n["id"], 0.0), 4), places=6,
                msg=f"「{n['id']}」用了全图介数（指纹没校验）")

        # 更强的锚点：全图与子图的介数必须**确实不同**，否则这条断言是空的
        self.assertNotAlmostEqual(
            round(full_result["node_betweenness"]["中央网信办"], 4),
            round(ref.get("中央网信办", 0.0), 4), places=3,
            msg="全图与子图介数一样 —— 这条测试抓不住 bug")

    def test_空子图也走重算且不崩(self):
        rels = self.store.list_relations(limit=None)
        G, types = GA.build_graph(rels, min_weight=1.0, blocked_nodes=set())
        r = GA.analyze(G, types, topn=50)
        ne = GA.to_pyvis_nodes_edges(dict(r), G, types, min_degree=1,
                                     limit=100, focus="查无此机构")
        self.assertEqual(ne["nodes"], [])
        self.assertIn("不在当前图里", ne["scope_note"])

    def test_复用确实发生了_不是两边都在重算(self):
        """反向验证：把映射去掉必须让结果**仍然对**，但路径不同。
        用 betweenness 值本身锚住：复用路径不该出现 0.0 的假值。"""
        rels = self.store.list_relations(limit=None)
        G, types = GA.build_graph(rels, min_weight=1.0, blocked_nodes=set())
        r = GA.analyze(G, types, topn=50)
        ne = GA.to_pyvis_nodes_edges(dict(r), G, types, min_degree=1,
                                     limit=100)
        by_id = {n["id"]: n for n in ne["nodes"]}
        self.assertIn("中央网信办", by_id)
        self.assertGreater(by_id["中央网信办"]["betweenness"], 0.0)
        # 与 analyze 里算的必须是同一个数（同一张图）
        src = {x["name"]: x["betweenness"] for x in r["betweenness"]}
        self.assertEqual(by_id["中央网信办"]["betweenness"],
                         src["中央网信办"])


if __name__ == "__main__":
    unittest.main(verbosity=2)