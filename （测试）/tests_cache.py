# -*- coding: utf-8 -*-
"""
慢接口结果缓存 的测试
================================================================
被缓存的是「先前成功的一次结果」。这里守六件事：

  ① 同参数二次请求命中：第一次真算、第二次不再算，两次响应逐字段一致，
     且第二次显著更快（用一个故意变慢的真算函数量出来，不靠猜）。
  ② 参数任一变化 → 重算：date_from / mode / limit / hl 各自都要分开；
     另有**结构性守门**（ast 解析 app.py）：每条接缓存的路由，键里的参数
     集合必须与它签名的参数集合**逐个相同** —— 漏一个参数是这套缓存
     唯一能算错的方式，所以拿测试钉死，而不是靠人肉复核。
  ③ 写路径失效：抽取落库（write=true）、清库（clear_first）、
     POST /api/entities、POST /api/relations、同步结束、批量摘要结束 ——
     都要把缓存整表清空；而**只预览不落库**（write=false）不该清。
  ④ 失败不缓存：{"error": ...} 与抛异常都不入缓存（否则一次失败会被
     钉成永久失败）。
  ⑤ 深拷贝：调用方就地改响应，改不到缓存里那份，下一次请求仍然干净。
  ⑥ 容量 64 / LRU 淘汰 / 命中后挪到最新 / 并发不炸 / stats 与 clear 端点。

铁律：临时库 + TestClient，不碰真实 data/cacx.db，不写 config/settings.json。
跑法：py -3 "（测试）\tests_cache.py"（cwd = 项目根）
"""
import ast
import importlib
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from cacx import db as D              # noqa: E402

APP_PY = _HERE.parent / "cacx" / "web" / "app.py"


# ---------------------------------------------------------------- 夹具
class Base(unittest.TestCase):
    """临时库 + 两站三栏目（跨 2024~2026）。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_cache_"))
        cfg = {"db": {"path": str(self.tmp / "t.db")},
               "ai": {"provider": "none"}, "search": {"min_fts_chars": 3}}
        self.d = D.open_db(cfg)
        self.assertIn("cacx_cache_", str(self.d.path))
        self.WA = importlib.import_module("cacx.web.app")
        self.old = self.WA.DB
        self.WA.DB = self.d
        self.c = TestClient(self.WA.app)
        self.reset_cache()

        self.mk("cac", "A093710", "国·地方网信", [
            ("2024-01-05", 1), ("2024-02-05", 2), ("2025-01-05", 3),
            ("2025-06-05", 2), ("2026-01-05", 1), ("2026-06-05", 1),
        ])
        self.mk("cac", "A093601", "国·时政要闻", [
            ("2024-05-05", 1), ("2025-03-05", 2), ("2026-01-05", 1),
        ])
        self.mk("zjwx", "1673574", "浙·清朗网络", [
            ("2024-04-05", 1), ("2025-07-05", 2), ("2026-02-05", 1),
        ])
        self.d.conn.commit()
        # 人工补录接口要真实存在的 article_id（EntityIn/RelationIn 必填）
        self.aid = self.d.conn.execute(
            "SELECT MIN(id) FROM articles").fetchone()[0]

    def tearDown(self):
        self.reset_cache()
        self.WA.DB = self.old
        try:
            self.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reset_cache(self):
        self.WA._CACHE_CLEAR()
        for k in ("hit", "miss", "put", "skip", "evict", "clear"):
            self.WA._CACHE_STATS[k] = 0

    def mk(self, site, code, cname, rows):
        for day, n in rows:
            for i in range(n):
                self.d.conn.execute(
                    "INSERT INTO articles (site,column_code,column_name,pub_day,"
                    "pub_date,year,title,url,content_text,chars,content_empty)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (site, code, cname, day, day, day[:4],
                     f"{site}-{cname}-{day}-{i}",
                     f"http://{site}/{code}/{day}/{i}", "正文" * 50, 100))

    def entries(self) -> int:
        return self.c.get("/api/cache/stats").json()["entries"]

    def slow(self, name, delay=0.20):
        """把 DB 上的某个方法换成『先睡再算』的版本，返回调用计数。

        用真算函数来量耗时差，而不是断言一个凭感觉的毫秒数。
        """
        calls = []
        real = getattr(self.d, name)

        def wrapper(*a, **kw):
            calls.append(kw)
            time.sleep(delay)
            return real(*a, **kw)

        setattr(self.d, name, wrapper)
        return calls


# ---------------------------------------------------------------- ① 命中
class TestHit(Base):
    def test_同参数二次请求命中_结果逐字段一致_且明显更快(self):
        calls = self.slow("facets_by_site")
        url = "/api/facets?date_from=2025-01-01"
        t0 = time.time(); r1 = self.c.get(url); miss_ms = time.time() - t0
        t0 = time.time(); r2 = self.c.get(url); hit_ms = time.time() - t0
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json(), r2.json())          # 逐字段一致
        self.assertEqual(len(calls), 1)                 # 只真算了一次
        self.assertLess(hit_ms, 0.05)                   # 命中不走计算
        self.assertLess(hit_ms * 3, miss_ms)            # 且差得明显
        st = self.c.get("/api/cache/stats").json()
        self.assertEqual(st["hit"], 1)
        self.assertEqual(st["miss"], 1)

    def test_没传参数与传默认值是同一个键(self):
        calls = self.slow("top_orgs")
        a = self.c.get("/api/panel/orgs").json()
        b = self.c.get("/api/panel/orgs?limit=12&mode=coarse").json()
        self.assertEqual(a, b)
        self.assertEqual(len(calls), 1)

    def test_直接调函数与走HTTP是同一个键(self):
        """导出/快照内部是**直接调函数**（不传 mode → 拿到 Query 实例）。

        如果键上不把 Query 实例归一化成它的默认值，屏幕缓存一份、
        导出缓存另一份，「屏幕=导出」就白做了。
        """
        calls = self.slow("top_orgs")
        http = self.c.get("/api/panel/orgs?site=&limit=12&mode=coarse").json()
        direct = self.WA.api_panel_orgs(site="", limit=12)   # mode 不传
        self.assertEqual(http, direct)
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------- ② 不误命中
class TestParamSeparation(Base):
    def test_date_from变了要重算(self):
        calls = self.slow("facets_by_site")
        a = self.c.get("/api/facets?date_from=2025-01-01").json()
        b = self.c.get("/api/facets?date_from=2026-01-01").json()
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(a["columns"], b["columns"])
        self.assertEqual(self.entries(), 2)

    def test_mode变了不串键(self):
        coarse = self.slow("top_orgs", delay=0)
        refine = self.slow("top_orgs_refine", delay=0)
        a = self.c.get("/api/panel/orgs?limit=5&mode=coarse").json()
        b = self.c.get("/api/panel/orgs?limit=5&mode=refine").json()
        self.assertNotIn("refine", a)          # 粗选档不带 refine 字段
        self.assertIn("refine", b)             # 精选档带
        self.assertEqual(len(coarse), 1)
        self.assertEqual(len(refine), 1)
        self.assertEqual(self.entries(), 2)

    def test_limit变了要重算(self):
        calls = self.slow("top_orgs", delay=0)
        self.c.get("/api/panel/orgs?limit=3")
        self.c.get("/api/panel/orgs?limit=7")
        self.assertEqual(len(calls), 2)

    def test_graph_analysis的高亮参数也进键(self):
        seen = []
        real = self.WA._graph_payload

        def spy(*a, **kw):
            seen.append(kw)
            return {"nodes": [], "edges": [], "filters": {"n": len(seen)}}

        self.WA._graph_payload = spy
        try:
            self.c.get("/api/graph/analysis?hl=a")
            self.c.get("/api/graph/analysis?hl=b")     # 只改了 hl
            self.c.get("/api/graph/analysis?hl=b")     # 与上一条同参
        finally:
            self.WA._graph_payload = real
        self.assertEqual(len(seen), 2)                 # 第三次命中
        self.assertEqual(seen[0]["hl"], "a")
        self.assertEqual(seen[1]["hl"], "b")

    def test_每条接入路由的键与签名参数逐个相同(self):
        """结构性守门：漏一个参数 = 两个口径撞同一个键（唯一会算错的方式）。

        直接读源码：找出每个含 `_cached(...)` 的函数，比较
        「第 2 个实参那个字典的键集合」与「该函数签名的参数集合」。
        """
        tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
        checked = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            sig = {a.arg for a in fn.args.args if a.arg != "self"}
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_cached"):
                    self.assertGreaterEqual(len(node.args), 2, fn.name)
                    params = node.args[1]
                    self.assertIsInstance(params, ast.Dict, fn.name)
                    keys = {k.value for k in params.keys}
                    self.assertEqual(
                        keys, sig,
                        f"{fn.name} 的缓存键与签名参数不一致："
                        f"漏 {sorted(sig - keys)}，多 {sorted(keys - sig)}")
                    checked.append(fn.name)
        self.assertGreaterEqual(len(checked), 10, f"接入点太少：{checked}")
        self.assertIn("api_graph_analysis", checked)
        self.assertIn("api_facets", checked)


# ---------------------------------------------------------------- ②b 换库
class TestDbSwap(Base):
    """缓存属于「当时那个库」。

    回归自一次真事故：缓存是模块级的、跨用例存活，而本仓库的测试夹具**每个
    用例换一个新临时库**（`WA.DB = self.d`）。于是在用例 B 里，参数一字不差
    命中用例 A 那份（另一个库的）响应 —— tests_panel_snapshot 的
    「栏目名被转义不会破版」就是这样拿到旧库的 HTML 而变红的。
    """

    def test_换了库之后同参数不命中(self):
        # ⚠ 这里不能拿 slow() 计数：它挂的是**旧库对象**的方法，换库之后
        #   请求走的是新库的方法，数不到 —— 用缓存统计当标记才准。
        url = "/api/facets?date_from=2025-01-01"
        first = self.c.get(url).json()
        miss0 = self.WA._CACHE_STATS["miss"]

        tmp2 = Path(tempfile.mkdtemp(prefix="cacx_cache2_"))
        self.addCleanup(shutil.rmtree, tmp2, ignore_errors=True)
        d2 = D.open_db({"db": {"path": str(tmp2 / "t2.db")},
                        "ai": {"provider": "none"},
                        "search": {"min_fts_chars": 3}})
        self.addCleanup(d2.conn.close)
        d2.conn.execute(
            "INSERT INTO articles (site,column_code,column_name,pub_day,"
            "pub_date,year,title,url,content_text,chars,content_empty) "
            "VALUES ('cac','A093710','国·地方网信','2025-06-01','2025-06-01',"
            "'2025','另一个库的唯一一篇','http://x/1','正文',2,0)")
        d2.conn.commit()

        self.WA.DB = d2                       # ← 换库（夹具就是这么干的）
        second = self.c.get(url).json()
        self.assertEqual(self.WA._CACHE_STATS["miss"], miss0 + 1,
                         "换了库还命中 —— 等于把旧库的数字端给新库")
        self.assertNotEqual(first, second)
        self.assertEqual([c["n"] for c in second["columns"]], [1])

        third = self.c.get(url).json()        # 同一库再问一次：该命中了
        self.assertEqual(self.WA._CACHE_STATS["miss"], miss0 + 1)
        self.assertEqual(third, second)

    def test_库没换时照常命中(self):
        """反向：没人换库就该命中 —— 别把这条防线的代价付成「永远不命中」。"""
        calls = self.slow("facets_by_site", delay=0)
        url = "/api/facets?date_from=2025-01-01"
        self.c.get(url)
        self.c.get(url)
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------- ③ 失效
class TestWriteInvalidation(Base):
    def warm(self, url="/api/facets?date_from=2025-01-01"):
        self.c.get(url)
        self.assertGreater(self.entries(), 0, "预热失败：缓存里应有东西")

    def test_写实体清缓存(self):
        self.warm()
        r = self.c.post("/api/entities", json=[
            {"name": "测试机构", "type": "org", "article_id": self.aid}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.entries(), 0)

    def test_写关系清缓存(self):
        self.warm()
        r = self.c.post("/api/relations", json=[
            {"src_name": "A", "dst_name": "B", "rel_type": "issue",
             "article_id": self.aid}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.entries(), 0)

    def test_抽取落库清缓存(self):
        self.warm()
        r = self.c.post("/api/extract/run",
                        json={"site": "cac", "limit": 5, "mode": "full",
                              "write": True})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json().get("written"))
        self.assertEqual(self.entries(), 0)
        self.assertGreater(self.WA._CACHE_STATS["clear"], 0)

    def test_只预览不落库_不清缓存(self):
        """write=false 什么都没写，清掉只是白白重算 —— 这条钉住那个判断。"""
        self.warm()
        r = self.c.post("/api/extract/run",
                        json={"site": "cac", "limit": 5, "mode": "full",
                              "write": False})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json().get("written"))
        self.assertGreater(self.entries(), 0)

    def test_清库清缓存(self):
        self.warm()
        r = self.c.post("/api/extract/run",
                        json={"clear_first": True, "confirm_clear": True,
                              "site": "cac", "limit": 2, "mode": "full",
                              "write": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.entries(), 0)

    def test_失效之后要重算(self):
        calls = self.slow("facets_by_site", delay=0)
        url = "/api/facets?date_from=2025-01-01"
        self.c.get(url)
        self.c.get(url)                       # 命中
        r = self.c.post("/api/entities", json=[
            {"name": "X", "type": "org", "article_id": self.aid}])
        self.assertEqual(r.status_code, 200)
        self.c.get(url)                       # 失效后重算
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.WA._CACHE_STATS["clear"], 1)
        self.assertEqual(self.WA._CACHE_STATS["hit"], 1)


# ---------------------------------------------------------------- ④ 失败不缓存
class TestNoFailCaching(Base):
    """④ 只缓存成功。

    ⚠ 写这几条时踩过的坑：`lambda: n.append(1) and {...}` 返回的是 None
      （append 返回 None），于是「没入缓存」是因为返回值不是 dict，
      而不是因为 error 字段 —— 用例会绿着骗人。必须让 calc 真的返回 dict。
    """

    @staticmethod
    def _calc(log, payload):
        def f():
            log.append(payload)
            return payload
        return f

    def test_error字典不入缓存(self):
        n = []
        err = {"error": "口径下没有文章"}
        for _ in range(2):
            self.WA._cached("unit", {"k": 1}, self._calc(n, err))
        self.assertEqual(len(n), 2, "error 响应被缓存了")
        self.assertEqual(self.entries(), 0)
        self.assertEqual(self.WA._CACHE_STATS["skip"], 2)

    def test_正常结果入缓存(self):
        n = []
        a = self.WA._cached("unit2", {"k": 1}, self._calc(n, {"v": 1}))
        b = self.WA._cached("unit2", {"k": 1}, self._calc(n, {"v": 2}))
        self.assertEqual(len(n), 1)
        self.assertEqual(a, {"v": 1})
        self.assertEqual(b, {"v": 1})         # 第二次是缓存那份

    def test_异常不入缓存且照抛(self):
        n = []

        def boom():
            n.append(1)
            raise RuntimeError("炸了")

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.WA._cached("unit3", {"k": 1}, boom)
        self.assertEqual(len(n), 2)
        self.assertEqual(self.entries(), 0)

    def test_路由抛异常不入缓存(self):
        """500 之后第二次必须真算，而不是把失败钉住。"""
        from fastapi.testclient import TestClient
        raw = TestClient(self.WA.app, raise_server_exceptions=False)
        real = self.d.facets_by_site
        boom = {"left": 1}

        def flaky(*a, **kw):
            if boom["left"]:
                boom["left"] -= 1
                raise RuntimeError("第一次就炸")
            return real(*a, **kw)

        self.d.facets_by_site = flaky
        url = "/api/facets?date_from=2025-01-01"
        self.assertEqual(raw.get(url).status_code, 500)
        self.assertEqual(self.entries(), 0)
        self.assertEqual(raw.get(url).status_code, 200)   # 第二次真算、成功
        self.assertEqual(self.entries(), 1)


# ---------------------------------------------------------------- ⑤ 深拷贝
class TestDeepCopy(Base):
    def test_就地改命中的响应不污染缓存(self):
        url = "/api/facets?date_from=2025-01-01"
        first = self.c.get(url).json()          # miss：返回的是刚算出来的那份
        second = self.c.get(url).json()         # hit
        second["total"] = -1
        (second.get("columns") or []).clear()
        third = self.c.get(url).json()
        self.assertEqual(third["total"], first["total"])
        self.assertEqual(third["columns"], first["columns"])

    def test_就地改未命中的响应也不污染缓存(self):
        url = "/api/facets?date_from=2025-01-01"
        first = self.c.get(url).json()          # miss
        first["total"] = -1
        again = self.c.get(url).json()          # 从缓存拿
        self.assertNotEqual(again["total"], -1)


# ---------------------------------------------------------------- ⑥ 容量/并发/端点
class TestCapacityAndEndpoints(Base):
    def test_容量上限64_超出淘汰最旧的(self):
        cap = self.WA._CACHE_CAP
        self.assertEqual(cap, 64)
        for i in range(cap + 3):
            self.WA._cached("unit", {"i": i}, lambda i=i: {"i": i})
        self.assertEqual(self.entries(), cap)
        self.assertEqual(self.WA._CACHE_STATS["evict"], 3)

    def test_命中的会挪到最新_淘汰的是第二旧(self):
        cap = self.WA._CACHE_CAP
        for i in range(cap):
            self.WA._cached("unit", {"i": i}, lambda i=i: {"i": i})
        self.WA._cached("unit", {"i": 0}, lambda: {"i": 0})     # 命中第 0 条
        self.WA._cached("unit", {"i": 999}, lambda: {"i": 999})  # 挤掉一条
        params = [x["params"] for x in
                  self.c.get("/api/cache/stats").json()["items"]]
        self.assertIn("i=0", params)          # 刚被访问过 → 留着
        self.assertNotIn("i=1", params)       # 最久没用 → 被淘汰
        self.assertIn("i=999", params)

    def test_并发同键不炸且结果一致(self):
        errs = []

        def work():
            try:
                for _ in range(20):
                    v = self.WA._cached("thr", {"k": 1},
                                        lambda: {"v": [1, 2, 3], "s": "x"})
                    assert v == {"v": [1, 2, 3], "s": "x"}, v
            except Exception as e:                # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(self.entries(), 1)

    def test_stats端点的形状(self):
        self.c.get("/api/facets?date_from=2025-01-01")
        st = self.c.get("/api/cache/stats").json()
        for k in ("entries", "cap", "version", "bytes", "bytes_mb", "hit",
                  "miss", "hit_rate", "put", "skip", "evict", "clear",
                  "items", "note"):
            self.assertIn(k, st)
        self.assertEqual(st["cap"], 64)
        self.assertEqual(st["entries"], 1)
        self.assertEqual(st["miss"], 1)
        self.assertEqual(st["hit"], 0)
        self.assertGreater(st["bytes"], 0)
        self.assertTrue(st["items"][0]["params"],
                        "items 里要能看出缓存的是什么参数组合")
        self.assertIn("date_from=2025-01-01", st["items"][0]["params"])

    def test_clear端点(self):
        self.c.get("/api/facets?date_from=2025-01-01")
        self.assertGreater(self.entries(), 0)
        r = self.c.post("/api/cache/clear").json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["cleared"], 1)
        self.assertEqual(self.entries(), 0)
        self.assertEqual(r["version"], self.WA._CACHE_VER)

    def test_版本号随清空自增(self):
        v0 = self.WA._CACHE_VER
        self.WA._CACHE_CLEAR("test")
        self.assertEqual(self.WA._CACHE_VER, v0 + 1)
        self.assertEqual(self.c.get("/api/cache/stats").json()["version"],
                         v0 + 1)

    def test_缓存不改响应结构(self):
        """缓存只是加速：命中与未命中的响应必须一模一样。"""
        urls = ["/api/facets?date_from=2025-01-01",
                "/api/panel/orgs?site=cac&limit=5",
                "/api/panel/problems?site=cac",
                "/api/panel/joint?date_from=2025-01-01",
                "/api/panel/timeline?top=3&months=12",
                "/api/graph?limit=50",
                "/api/sample/options"]
        for u in urls:
            a = self.c.get(u)
            b = self.c.get(u)
            self.assertEqual(a.status_code, 200, u)
            self.assertEqual(
                json.dumps(a.json(), sort_keys=True, ensure_ascii=False),
                json.dumps(b.json(), sort_keys=True, ensure_ascii=False), u)


if __name__ == "__main__":
    unittest.main(verbosity=2)
