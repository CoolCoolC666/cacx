# -*- coding: utf-8 -*-
"""措施类（低置信门槛整类出局）：默认勾选放行 / 可取消 / **两套口径都钉住**。

背景：`measure` 实体的 confidence 上限恰好等于低置信门槛（0.8），
而踢出规则是 `<= 0.8`，于是这一类在默认口径下**100% 出局** ——
图例恒为 0、复选框还被判成 disabled，用户想看也点不开。

本文件钉住用户拍板的语义（2026-10-14 二次拍板，改为**默认勾选**）：
  **默认勾上**（默认图含措施与 adopt 边；报告/README/演示脚本已同步该口径）
  **仍可取消**（取消即回到门槛口径，门槛过滤依然有效）

⚠⚠ 2026-10-15：真库按**全区间**（2000-09-30 ~ 2026-10-14）抽出结果，
并补抽了 10-12 ~ 10-14 新采的 19 篇之后，「默认口径」是**两套**，必须分开钉
（否则会拿 A 口径的期望去测 B 口径的响应）：

  · **网页首屏口径** —— `index.html:425` 的图谱日期起点 `gp_dfrom` 默认 `2024-01`，
    `gpQuery()` 每次都带上它，所以评审方在浏览器里第一眼看到的图是这一档：
    **1761 节点 / 4211 边 / 126 连通块，画布 300/1482**（全区间重抽 + 补抽后的新基线）。
  · **API 默认口径** —— 直接调 `/api/graph/analysis`（不传 date_from）时是全库：
    **12162 节点 / 29069 边 / 886 连通块，画布 300/2995**（同上新基线）。

⚠ 性能：不带 site/date 的调用要在 **12162 节点全图**上算介数，实测单次 ≈ 268s
（2026-10-15 走接口的 4 类口径实测 327.8s，同量级）；
带 date_from 的调用只在筛选后的小图上算（2024 档实测 3.6s）。
所以本文件所有真库请求都走 `_payload()` **记忆化**（同一组参数只建一次图），
真库请求数固定为：API 口径 2 次（4 类 / 3 类）+ 网页口径若干次（各 ≈ 4s）；
2026-10-15 全量实测 22 用例共 **357s**，远低于 `_regress.py` 的单文件 1800s 上限。
—— 这条修复**只动测试**，不碰 `cacx/web/app.py`（另一路并行在改它）。
"""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
A = importlib.import_module("cacx.web.app")
GA = importlib.import_module("cacx.graph_analysis")

HTML = (Path(__file__).resolve().parent.parent
        / "cacx" / "web" / "static" / "index.html").read_text(encoding="utf-8")

ALL = "org,policy,problem,measure"
WEB = "2024-01-01"          # index.html:425 gp_dfrom 默认值（网页首屏口径）
_C = {}


def _payload(**kw):
    """记忆化：同一组参数只建一次图（不带日期的要在全图上算介数，实测 268s）。"""
    key = tuple(sorted(kw.items()))
    if key not in _C:
        _C[key] = A._graph_payload(min_degree=2, limit=300, min_weight=1.0,
                                   topn=12, max_degree=0, rel_types="",
                                   keyword="", focus="", hop=1, **kw)
    return _C[key]


def _web(**kw):
    """网页首屏口径（日期框默认 2024-01）。"""
    return _payload(date_from=WEB, **kw)


def _api(**kw):
    """API 默认口径（不传日期 = 全库区间）。"""
    return _payload(**kw)


def _deg(d):
    return {r["name"]: r["degree"] for r in (d.get("degree") or [])}


def _bt(d):
    return [(r["name"], r["betweenness"]) for r in (d.get("betweenness") or [])]


def _avail(d):
    return {x["group"]: x["n"] for x in d["graph"]["avail_by_group"]}


def _adopt_edges(d):
    return sum(1 for e in d["graph"]["edges"]
               if "adopt" in (e.get("types") or []))


class TestMeasureGatedDefault(unittest.TestCase):
    """默认口径：措施**默认勾选放行**（用户 2026-10-14 拍板）。下面两个口径都要对。

    ⚠ 报告/演示脚本引的是**网页首屏口径**（日期框 2024-01）；
      API 默认（全库）那套是重抽后的新基线，单独钉住，防止它悄悄回到旧值。
    """

    def test_两套口径的图规模数字(self):
        api = _api(node_types=ALL)
        self.assertEqual(api["graph"]["scope_nodes"], 12162)
        self.assertEqual(api["graph"]["scope_edges"], 29069)
        self.assertEqual(api.get("components"), 886)
        self.assertEqual(len(api["graph"]["nodes"]), 300)
        self.assertEqual(len(api["graph"]["edges"]), 2995)
        self.assertEqual(api["graph"].get("hidden_nodes"), 11862)

        web = _web(node_types=ALL)
        self.assertEqual(web["graph"]["scope_nodes"], 1761)
        self.assertEqual(web["graph"]["scope_edges"], 4211)
        self.assertEqual(web.get("components"), 126)
        self.assertEqual(web.get("largest_component"), 1410)
        self.assertEqual(len(web["graph"]["nodes"]), 300)
        self.assertEqual(len(web["graph"]["edges"]), 1482)
        self.assertEqual(web["graph"].get("hidden_nodes"), 1461)

    def test_默认图里措施可见且adopt边在(self):
        for tag, d in (("API", _api(node_types=ALL)), ("网页", _web(node_types=ALL))):
            self.assertGreater(_avail(d)["measure"], 0, f"{tag}：措施默认就该可用")
            self.assertGreater(_adopt_edges(d), 0, f"{tag}：默认就该看得到 adopt 边")
        self.assertEqual(_adopt_edges(_web(node_types=ALL)), 54)

    def test_两套口径的机构度数与介数(self):
        api = _api(node_types=ALL)
        deg = _deg(api)
        self.assertEqual(deg.get("网信办"), 1447)
        self.assertEqual(deg.get("中央网信办"), 814)
        self.assertEqual(deg.get("工业和信息化部"), 456)
        # 全区间口径下第一名是泛称「网信办」（早期文章里未按层级归一，
        # 见报告第四章「机构别名归并过宽」），中央网信办居第二。
        bt = _bt(api)
        self.assertEqual(bt[0][0], "网信办")
        self.assertAlmostEqual(bt[0][1], 0.1892, places=3)
        self.assertEqual(bt[1][0], "中央网信办")
        self.assertAlmostEqual(bt[1][1], 0.0899, places=3)

        web = _web(node_types=ALL)
        wdeg = _deg(web)
        self.assertEqual(wdeg.get("中央网信办"), 285)
        self.assertEqual(wdeg.get("工业和信息化部"), 142)
        self.assertEqual(wdeg.get("公安部"), 115)
        wbt = _bt(web)
        self.assertEqual(wbt[0][0], "中央网信办")
        self.assertAlmostEqual(wbt[0][1], 0.2109, places=3)
        self.assertEqual(wbt[1][0], "网信办")
        self.assertAlmostEqual(wbt[1][1], 0.1201, places=3)
        self.assertAlmostEqual(wbt[0][1] / wbt[1][1], 1.76, places=2)

    def test_默认放行数等于门槛挡住的量(self):
        for tag, d in (("API", _api(node_types=ALL)), ("网页", _web(node_types=ALL))):
            self.assertEqual(d["blocked_kept_out"], d["blocked_count"], tag)
            self.assertEqual(d["blocked_count"], 1375, tag)
            self.assertEqual(d["blocked_by_type"]["measure"], 1375, tag)


class TestGatedStillReachable(unittest.TestCase):
    """取消勾选措施 = 回到门槛口径（门槛仍然生效，只是不默认生效）。"""

    def test_取消勾选措施回到门槛口径(self):
        web = _web(node_types="org,policy,problem")
        self.assertEqual(_avail(web)["measure"], 0)
        self.assertEqual(web["blocked_kept_out"], 0)
        self.assertEqual(web["graph"]["scope_nodes"], 1634)
        self.assertEqual(web["graph"]["scope_edges"], 3977)
        self.assertEqual(web.get("components"), 115)
        self.assertEqual(_deg(web).get("中央网信办"), 243)
        self.assertAlmostEqual(_bt(web)[0][1], 0.1942, places=3)

        api = _api(node_types="org,policy,problem")
        self.assertEqual(_avail(api)["measure"], 0)
        self.assertEqual(api["blocked_kept_out"], 0)
        self.assertEqual(api["graph"]["scope_nodes"], 11291)
        self.assertEqual(api["graph"]["scope_edges"], 27328)
        self.assertEqual(api.get("components"), 817)

    def test_不传node_types也回到门槛口径(self):
        """「没勾任何特殊类」= 门槛生效。后端按「空=不筛」读，
        所以空串与不传必须与「只勾三类」完全一致。"""
        for tag, d in (("空串", _web(node_types="")), ("不传", _web())):
            self.assertEqual(_avail(d)["measure"], 0, tag)
            self.assertEqual(d["blocked_kept_out"], 0, tag)
            self.assertEqual(d["graph"]["scope_nodes"], 1634, tag)
            self.assertEqual(d["graph"]["scope_edges"], 3977, tag)


class TestMeasureGatedOptIn(unittest.TestCase):
    """勾选措施 → 解除过滤 → 措施与 adopt 进图。"""

    def test_勾选措施后进图(self):
        d = _api(node_types=ALL)
        self.assertGreater(_avail(d)["measure"], 0, "勾了措施它就该可用")
        self.assertGreater(d["blocked_kept_out"], 0, "必须说清放行了多少")
        self.assertEqual(d["blocked_count"], d["blocked_by_type"]["measure"])

    def test_勾选后图规模变大(self):
        base = _web(node_types="")
        on = _web(node_types=ALL)
        self.assertGreater(on["graph"]["scope_edges"],
                           base["graph"]["scope_edges"], "adopt 边应该回来了")
        self.assertGreater(on["graph"]["scope_nodes"],
                           base["graph"]["scope_nodes"])

    def test_adopt边真的进图(self):
        self.assertGreater(_adopt_edges(_api(node_types=ALL)), 0,
                           "勾了措施就该看得到 adopt 边")

    def test_只勾措施不含机构时得到空图(self):
        """关系全是 org→X 单向：只勾措施 → 机构被裁掉 → 措施节点失去全部邻居、
        度归零 → 被 min_degree 滤光 → 空图。这与「只勾政策」是同一既有语义，
        前端本来就弹窗提示「请同时勾选机构」。此处钉住它不会悄悄变样。"""
        d = _web(node_types="measure")
        self.assertEqual(len(d["graph"]["nodes"]), 0)
        self.assertEqual(len(d["graph"]["edges"]), 0)

    def test_未勾措施时放行数为零(self):
        """只勾其它三类 —— 措施仍在门槛外。"""
        d = _web(node_types="org,policy,problem")
        self.assertEqual(_avail(d)["measure"], 0)
        self.assertEqual(d["blocked_kept_out"], 0)


class TestBlockedTypesHelper(unittest.TestCase):
    def test_allow_types只放行指定类(self):
        class _D:
            def list_entities(self, type="", limit=None):
                return [{"name": "下架", "type": "measure", "confidence": 0.7}]

        self.assertEqual(len(A._blocked_for_graph(_D())), 1)
        self.assertEqual(
            len(A._blocked_for_graph(_D(), allow_types={"measure"})), 0,
            "勾了措施就该解除对它的过滤")
        self.assertEqual(
            len(A._blocked_for_graph(_D(), allow_types={"org"})), 1,
            "勾机构不该顺带放行措施")


class TestFrontendOptIn(unittest.TestCase):
    """前端：可勾、默认不勾、勾了要真传参。"""

    def test_措施复选框不得禁用(self):
        self.assertIn("const off = avail[k] === 0 && bkd === 0;", HTML,
                      "avail=0 但被门槛挡过 ≠ 不存在，不能禁用")

    def test_默认勾选排除被门槛挡的类(self):
        self.assertIn("function gpDefaultTypes()", HTML)
        self.assertIn("GP_LEGEND_ON = new Set(gpDefaultTypes())", HTML)

    def test_初始为null等后端告知(self):
        """「哪些类被挡着」要等后端响应 —— 初始瞎猜会把措施勾上、默认口径就变了。"""
        self.assertIn("let GP_LEGEND_ON = null;", HTML)

    def test_全勾时也必须传node_types(self):
        """⚠ 这是踩过的坑：全勾时旧逻辑不传 node_types，
        后端把空读成「什么都没勾」→ 措施照旧被挡 → 勾选成了假开关。
        默认改为全勾之后，这个条件更关键 —— 少了它首屏就会是门槛口径。"""
        self.assertIn("needsGatedType", HTML)
        self.assertIn("(lgSend.length < lgAll.length || needsGatedType || notInit)",
                      HTML)

    def test_首屏图例未渲染时也要兜底(self):
        """⚠ 2026-10-14 实测踩的第二个坑：首屏那次请求里
        **图例一个 checkbox 都还没渲染**（loadGraph 先跑、renderLegend 在它之后），
        于是 lgOn.length === 0，`if(lgSend.length && ...)` 整个短路 ——
        上一版加的 notInit 挂在第二个条件上，根本没机会被读到，
        首屏照样按门槛口径建图。

        闸门是 lgSend，所以必须**先按默认类型兜底**，再谈传不传。"""
        self.assertIn("const legendNotReady = lgAll.length === 0;", HTML)
        self.assertIn("const lgSend = legendNotReady ? gpDefaultTypes() : lgOn;", HTML)
        self.assertIn("|| legendNotReady", HTML)

    def test_图例未渲染与用户全取消必须分开(self):
        """两者在 lgOn 上都是空的，但不分开的话用户「四类全取消」
        会被兜底成全勾 —— 那就是个假开关（用户取消勾选，图却没变）。"""
        self.assertIn("const notInit = Object.keys(GP_BLOCKED_BY_TYPE).length === 0 || legendNotReady;",
                      HTML)

    def test_实体类型变量不得叫types(self):
        """⚠ 上面「关系类型」那节已经 const 过一次 types，
        同名再声明一次是 SyntaxError → **整个 script 不执行** →
        页面永远停在「加载中…」，图谱请求一个都不发。
        这类错要往下滑控制台才看得到，现场极难定位。"""
        src = HTML
        self.assertIn("const lgSend =", src)
        # gpQuery 函数体内不得再出现第二个 types 声明
        body = src[src.index("function gpQuery("):src.index("function gpReset(")]
        self.assertNotIn("const types =", body.replace("const types = [...document", "OK"))

    def test_加载完要自动铺满(self):
        """⚠ vis 默认 scale=1 且从不 fit：300 节点物理布局只占画布
        左下角一小块（实测 scale≈0.27，中央网信办与农业农村部两节点
        相距仅 19px），投屏时那团图缩成一坨、标签全糊，
        「点边」在几何上就点不中。fit 必须在**物理收敛之后**做。"""
        self.assertIn("stabilizationIterationsDone", HTML)
        self.assertIn("GP_NET.fit(", HTML)
        src = HTML
        i_net = src.index("GP_NET = new vis.Network")
        i_fit = src.index("GP_NET.fit(")
        self.assertGreater(i_fit, i_net, "fit 要在建图之后")
        self.assertNotIn("GP_NET.fit();", HTML.split("stabilizationIterationsDone")[0],
                         "不能在物理收敛前就 fit（对着一张还在动的图取景）")

    def test_说明不得给无效操作(self):
        """实测调小最小度数并不会让措施变多（画布 300 上限截断）——
        说过头就是假提示，比不说更糟。"""
        self.assertNotIn("想看全可把", HTML)
        self.assertIn("调小「最小度数」不会让它们变多", HTML)

    def test_放行文案要写明画布只显示一部分(self):
        self.assertIn("图上只看得到一部分", HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
