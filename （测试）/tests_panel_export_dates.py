# -*- coding: utf-8 -*-
"""
「屏幕=导出」：面板的时间口径必须一路透传到 **导出 CSV / 快照 HTML**
================================================================
缺口背景（本轮要补的那个）：
前端面板已经做到「日期优先于月份」—— pn_from/pn_to 有值就按日期筛，
两个都留空才由 pn_months 换算成 date_from（`monthsToFrom`）。
但 /api/panel/export 与 /api/panel/snapshot 两条链当时只接站点、不接日期：
屏幕上按「近 36 个月」筛过的四张图，导出的还是**全量**数字。
屏幕一个口径、导出另一个口径，正是「屏幕=导出」这条铁律要拦的事。

本文件验三件事：
  ① 接线：export / snapshot 收到 date_from/date_to 后**原样**交给
     api_facets / api_panel_problems / api_panel_orgs / api_panel_joint；
     没收到（""）时也**原样传空串**，由那几个路由按 T1 的规矩自己丢弃 ——
     导出层绝不替下游编一个区间出来。
  ② 数字：同一段区间下，导出 CSV 与屏幕走的那条接口逐行一致。
  ③ 前端真的带了这个参数（前端不带，后端透传就没人触发，等于没改）——
     在 Node 里跑 index.html 的 dlPanel / dlSnapshot，看生成的下载地址。

铁律照旧：临时库 + TestClient，不碰真实 data/cacx.db、不写 config。
跑法：py -3 tests_panel_export_dates.py
"""
import csv
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib                      # noqa: E402
A = importlib.import_module("cacx.web.app")

# 复用面板测试的临时库夹具（铁律同上：不碰真实库）
from tests_panel_export import Base   # noqa: E402

HTML = Path(__file__).resolve().parent.parent / "cacx" / "web" / "static" / "index.html"

# 夹具里的数据：cac 17 篇 / zjwx 5 篇。
# ⚠ 只给 date_from 是**开区间**（到 2099 才算止），2026 年的那几篇也会被算进来；
#   要卡住两年之间的窗口，date_from 与 date_to 必须一起给。
D = "2025-01-01"
D2 = "2025-12-31"
CAC_IN_WINDOW = 9        # 2025 全年：地方网信 6 + 时政要闻 3
CAC_ALL = 17


class _Spy(Base):
    """把四个下游路由换成 spy，其余照常跑真库。

    ⚠ 只 spy 路由函数，不 mock _db：这样验的是**接线**，
      SQL 层的日期语义是那四个路由自己的事（见 tests_panel_dates.py）。
    """
    NAMES = ("api_facets", "api_panel_problems", "api_panel_orgs",
             "api_panel_joint")

    def setUp(self):
        super().setUp()
        self.seen = {}
        self._orig = {n: getattr(A, n) for n in self.NAMES}
        for n, real in self._orig.items():
            setattr(A, n, self._spy(n, real))

    def _spy(self, name, real):
        seen = self.seen

        def f(*a, **kw):
            seen[name] = kw
            return real(*a, **kw)
        return f

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(A, n, f)
        super().tearDown()


# ==================================================== ① 接线：导出 CSV
class TestExportPassthrough(_Spy):
    def test_栏目导出把日期交给facets(self):
        self.c.get("/api/panel/export?which=columns&site=cac"
                   "&date_from=%s&date_to=2025-06-30" % D)
        kw = self.seen["api_facets"]
        self.assertEqual(kw.get("date_from"), D)
        self.assertEqual(kw.get("date_to"), "2025-06-30")
        self.assertEqual(kw.get("site"), "cac", "加日期不能把站点弄丢")

    def test_治理问题导出把日期交给problems路由(self):
        self.c.get("/api/panel/export?which=problems&date_from=%s" % D)
        kw = self.seen["api_panel_problems"]
        self.assertEqual(kw.get("date_from"), D)
        self.assertEqual(kw.get("date_to"), "")

    def test_机构导出把日期交给orgs路由且不丢limit(self):
        self.c.get("/api/panel/export?which=orgs&date_to=2025-06-30&limit=9")
        kw = self.seen["api_panel_orgs"]
        self.assertEqual(kw.get("date_to"), "2025-06-30")
        self.assertEqual(kw.get("date_from"), "")
        self.assertEqual(kw.get("limit"), 9, "加日期不能把 limit 弄丢")

    def test_联合发文导出把日期交给joint路由(self):
        self.c.get("/api/panel/export?which=joint&date_from=%s" % D)
        self.assertEqual(self.seen["api_panel_joint"].get("date_from"), D)

    def test_不带日期时传空串而不是自己编一个区间(self):
        """⚠ 导出层不是「默认区间的定义者」。

        默认区间（2024-01-01 ~ 2099-12-31）属于 db / 路由那层的口径，
        导出层只负责原样传。这里自作主张填一个默认值，就会出现
        「导出用 A 区间、屏幕用 B 区间」这种最难查的分叉。
        空串怎么处理：见 tests_facets_dates.py / tests_panel_dates.py。
        """
        self.c.get("/api/panel/export?which=problems")
        kw = self.seen["api_panel_problems"]
        self.assertEqual(kw.get("date_from"), "")
        self.assertEqual(kw.get("date_to"), "")

    def test_趋势导出不假装有日期口径(self):
        """trend 分支不吃 date_from（它的截断走月份那套），

        所以导出层也**不该**给它塞日期参数：URL 上带着一个不生效的
        date_from，看的人会以为这份 CSV 是筛过的 —— 比不写更坏。
        """
        self.c.get("/api/panel/export?which=trend&granularity=year"
                   "&date_from=%s" % D)
        self.assertEqual(self.seen, {},
                         "trend 分支不该去调这四个路由")


# ==================================================== ① 接线：快照 HTML
class TestSnapshotPassthrough(_Spy):
    def test_快照把日期交给四张图(self):
        self.c.get("/api/panel/snapshot?months=36&site=cac&date_from=%s" % D)
        for n in self.NAMES:
            self.assertIn(n, self.seen, "%s 没被调到" % n)
            self.assertEqual(self.seen[n].get("date_from"), D, n)
            self.assertEqual(self.seen[n].get("date_to"), "", n)

    def test_快照不带日期时传空串(self):
        self.c.get("/api/panel/snapshot?months=36")
        for n in self.NAMES:
            self.assertEqual(self.seen[n].get("date_from"), "", n)
            self.assertEqual(self.seen[n].get("date_to"), "", n)

    def test_快照副标题写的是真实区间(self):
        """副标题是这份 HTML 上唯一写明口径的地方。
        给了日期还印「近 36 个月」，交上去就是**假口径** —— 读的人
        会拿别的区间去理解这些数字。"""
        h = self.c.get("/api/panel/snapshot?months=36&date_from=%s"
                       "&date_to=2025-06-30" % D).text
        self.assertIn(D, h)
        self.assertIn("2025-06-30", h)
        self.assertNotIn("近 36 个月", h)
        # 不给日期才走月份说法
        h2 = self.c.get("/api/panel/snapshot?months=36").text
        self.assertIn("近 36 个月", h2)


# ==================================================== ② 数字：屏幕=导出
class TestScreenEqualsExport(Base):
    """同一段区间，屏幕那条接口与导出/快照的数字必须一样。

    ⚠ 这条才是真的判据。只断言「参数传过去了」是接线测试；
      「传过去之后数字还一致」才是铁律本身。
    """

    def _csv(self, qs):
        r = self.c.get("/api/panel/export?" + qs)
        self.assertEqual(r.status_code, 200, r.text[:200])
        return list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))

    def _rank_of_snapshot(self, qs):
        """从快照里抠出「栏目 Top」那张数据表。"""
        r = self.c.get("/api/panel/snapshot?" + qs)
        self.assertEqual(r.status_code, 200, r.text[:200])
        m = re.search(r"<h2>栏目 Top \d+</h2>(.*?)</details>", r.text, re.S)
        self.assertIsNotNone(m, "快照里没有栏目 Top 的数据表")
        return [(k, int(n)) for k, n in
                re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td></tr>",
                           m.group(1))]

    def test_栏目导出与屏幕接口逐项一致(self):
        scr = self.c.get("/api/facets?site=cac&date_from=%s" % D).json()
        want = sorted((c["column_name"], c["n"]) for c in scr["columns"])
        rows = self._csv("which=columns&site=cac&date_from=%s" % D)
        got = sorted((r[2], int(r[3])) for r in rows[1:])
        self.assertEqual(got, want,
                         "导出的栏目数字与屏幕接口不是同一份")

    def test_带日期比不带日期的数字真的少(self):
        """反向自证：这个筛选真的在起作用，不是每次都返回全量。
        只验「都能跑通」的话，把日期丢掉的 bug 照样是绿的。"""
        with_d = sum(int(r[3]) for r in self._csv(
            "which=columns&site=cac&date_from=%s&date_to=%s" % (D, D2))[1:])
        without = sum(int(r[3]) for r in
                      self._csv("which=columns&site=cac")[1:])
        self.assertEqual(with_d, CAC_IN_WINDOW)
        self.assertEqual(without, CAC_ALL)
        self.assertLess(with_d, without, "日期没让数字收窄，等于没接上")

    def test_快照栏目榜与屏幕接口逐项一致(self):
        """栏目标签与屏幕同一条规则（站点 tag + “·” + 栏目名），
        所以这里必须**照屏幕的拼法**比，不能拿裸栏目名去比 ——
        那样比出来的差异是标签，不是数据。"""
        scr = self.c.get("/api/facets?site=cac&date_from=%s" % D).json()
        want = sorted(("国·" + c["column_name"], c["n"]) for c in scr["columns"])
        got = sorted(self._rank_of_snapshot("months=36&site=cac&date_from=%s"
                                            % D))
        self.assertEqual(got, want,
                         "快照的栏目榜与屏幕不是同一份数据")

    def test_快照不带日期时仍是全量(self):
        got = self._rank_of_snapshot("months=36&site=cac")
        self.assertEqual(sum(n for _, n in got), CAC_ALL)

    def test_治理问题导出与屏幕接口逐项一致(self):
        scr = self.c.get("/api/panel/problems?date_from=%s" % D).json()
        want = [(x["problem"], x["articles"]) for x in scr["items"]]
        rows = self._csv("which=problems&date_from=%s" % D)
        got = [(r[0], int(r[1])) for r in rows[1:]]
        self.assertEqual(got, want)


# ==================================================== ③ 前端：真的带了参数
_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const path = process.argv[2];
const html = fs.readFileSync(path, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log('@@RESULT@@' + JSON.stringify({err:'no <script>'})); process.exit(0); }
const src = m[1];

// 按名字抠出函数体（数括号，不靠正则 —— 正则配不平嵌套）
function grab(name) {
  let i = src.indexOf('function ' + name + '(');
  if (i < 0) return '';
  // ⚠ async 不能丢：抠出来的是字符串，`function f(){ await g() }` 是**语法错误**，
  //   一个 async 函数就能让整个沙箱脚本解析失败。症状是 bootErr（不是 ReferenceError），
  //   很难联想到「少抄了 6 个字符」。
  const am = src.slice(Math.max(0, i - 8), i).match(/async\s+$/);
  if (am) i -= am[0].length;
  let k = src.indexOf('{', i), depth = 0;
  for (; k < src.length; k++) {
    const c = src[k];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(i, k + 1);
}
// 抠哪些函数进沙箱 —— 这是**闭包**，不是一份清单：
//   dlPanel / dlSnapshot 本轮新增了对 pnOrgMode() 的调用，只抠原来那 4 个
//   就是「沙箱里少一个函数」→ ReferenceError（本类里那两条红的根因）。
//   凡入口函数直接/间接会调到的，一个都不能漏。
//   后半段是本轮图谱侧同批新增的函数：本测试台暂时不调它们，
//   一起抠进来是为了下次补图谱导出断言时不再踩同一个坑。
const need = ['monthsToFrom', 'panelDateQs', 'pnOrgMode', 'dlPanel', 'dlSnapshot',
              'panelScopeQs', 'panelOrgsQs', 'pnRefineNote', 'renderPanelOrgs',
              'loadPanelOrgs', 'gpMode', 'gpFacetsQs', 'gpRebuildChecks',
              'gpRenderFacets', 'gpModeChange'];
const body = need.map(grab);
const missing = need.filter((n, i) => !body[i]);

const DL = [];
const REG = {};
// 本轮新增的两个「档位」下拉框：真实页面上默认就是粗选。
// 桩里必须同值 —— 否则沙箱里的默认档位和屏幕不是一个状态，测了等于没测
// （value 留空串时 pnOrgMode() 虽然也返回 'coarse'，但那是「恰好」，不是「同状态」）。
const DEF = { pn_orgmode: 'coarse', gp_mode: 'coarse' };
function el(id) {
  return REG[id] || (REG[id] = {id: id, value: DEF[id] || '', innerHTML: ''});
}
const SB = {
  console, Math, Date, URLSearchParams, parseInt, String, Object, Array, JSON,
  $: el,
  toast: function(){},
  document: {
    createElement: function(t){
      const e = {tagName: t, download: '', _h: '', style: {},
                 click(){}, remove(){}, setAttribute(){}};
      Object.defineProperty(e, 'href', {
        get(){ return this._h; },
        set(v){ this._h = String(v); DL.push(String(v)); },
      });
      return e;
    },
    body: {appendChild(){}},
  },
};
SB.window = SB; SB.globalThis = SB; SB.self = SB;
vm.createContext(SB);
let bootErr = null;
try { vm.runInContext(body.join('\n'), SB, {filename: 'panel-export.js'}); }
catch (e) { bootErr = String(e); }

el('pn_site').value = 'zjwx';
el('pn_months').value = '36';
const autoFrom = (typeof SB.monthsToFrom === 'function') ? SB.monthsToFrom(36) : null;

const calls = [];
function rec(label, fn){
  const n = DL.length;
  try { fn(); } catch (e) { calls.push({label: label, err: String(e)}); return; }
  calls.push({label: label, urls: DL.slice(n)});
}
el('pn_from').value = ''; el('pn_to').value = '';
rec('columns', () => SB.dlPanel('columns', ''));
rec('problems', () => SB.dlPanel('problems', ''));
rec('orgs', () => SB.dlPanel('orgs', ''));
rec('joint', () => SB.dlPanel('joint', ''));
rec('trend', () => SB.dlPanel('trend', 'month'));
rec('events', () => SB.dlPanel('events', ''));
rec('snapshot', () => SB.dlSnapshot());
el('pn_from').value = '2025-01-01'; el('pn_to').value = '2025-06-30';
rec('columns@dates', () => SB.dlPanel('columns', ''));
rec('snapshot@dates', () => SB.dlSnapshot());
console.log('@@RESULT@@' + JSON.stringify(
  {bootErr: bootErr, missing: missing, autoFrom: autoFrom, calls: calls}));
"""


class TestFrontendSendsDates(unittest.TestCase):
    """前端不带参数，后端透传就是死代码。

    ⚠ 这一条必须**真跑 JS**：静态看代码「字符串里有 date_from」说明不了
      它出现在最终 URL 上（拼接顺序、条件分支都可能把它吞掉）。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = None
        for cand in ("node", "node.exe"):
            r = subprocess.run([cand, "--version"], capture_output=True)
            if r.returncode == 0:
                cls.node = cand
                break
        if not cls.node:
            raise unittest.SkipTest("本机没有 Node，跳过前端参数测试")
        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "h.js"
            js.write_text(_HARNESS, encoding="utf-8")
            r = subprocess.run([cls.node, str(js), str(HTML)],
                               capture_output=True, timeout=120)
        text = r.stdout.decode("utf-8", "replace")
        if "@@RESULT@@" not in text:
            raise AssertionError("测试台没产出结果，stderr:\n"
                                 + r.stderr.decode("utf-8", "replace")[-2000:])
        cls.R = json.loads(text.split("@@RESULT@@", 1)[1].strip().splitlines()[0])

    def _qs(self, label):
        c = [x for x in self.R["calls"] if x["label"] == label]
        self.assertTrue(c, "没跑到 %s" % label)
        self.assertNotIn("err", c[0], "%s 抛异常：%s" % (label, c[0].get("err")))
        self.assertTrue(c[0]["urls"], "%s 一次下载都没触发" % label)
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(c[0]["urls"][0]).query)

    def test_入口函数及其依赖都抠出来了(self):
        # 名字不再叫「四个函数」：本轮 entry 的闭包从 4 个长到 15 个，
        # 留着旧名会让下一个人以为这里只盯那 4 个。
        self.assertEqual(self.R["missing"], [],
                         "index.html 里少了函数：%s" % self.R["missing"])
        self.assertIsNone(self.R["bootErr"], "脚本报错：%s" % self.R["bootErr"])

    def test_月份模式导出带date_from(self):
        """pn_from/pn_to 都留空 → 用 pn_months 换算，与屏幕同一套规则。"""
        self.assertTrue(self.R["autoFrom"], "前端没有 monthsToFrom")
        for w in ("columns", "problems", "orgs", "joint"):
            q = self._qs(w)
            self.assertEqual(q.get("date_from"), [self.R["autoFrom"]],
                             "%s 导出没带换算出来的 date_from" % w)
            self.assertNotIn("date_to", q, "%s 月份模式不该传 date_to" % w)

    def test_日期模式导出带同一天的起止(self):
        q = self._qs("columns@dates")
        self.assertEqual(q.get("date_from"), ["2025-01-01"])
        self.assertEqual(q.get("date_to"), ["2025-06-30"])

    def test_快照同样带日期(self):
        q = self._qs("snapshot")
        self.assertEqual(q.get("date_from"), [self.R["autoFrom"]])
        self.assertEqual(q.get("months"), ["36"])
        q2 = self._qs("snapshot@dates")
        self.assertEqual(q2.get("date_from"), ["2025-01-01"])
        self.assertEqual(q2.get("date_to"), ["2025-06-30"])

    def test_导出地址仍带站点与图表名(self):
        """补日期不能把原有的筛选条件挤掉。"""
        q = self._qs("problems")
        self.assertEqual(q.get("which"), ["problems"])
        self.assertEqual(q.get("site"), ["zjwx"])

    def test_趋势导出不带不生效的日期参数(self):
        """trend 分支不消费 date_from，带上它就是个假口径。"""
        q = self._qs("trend")
        self.assertNotIn("date_from", q)
        self.assertEqual(q.get("granularity"), ["month"])

    def test_前端换算规则与后端默认一致(self):
        """自证 autoFrom 是「距今 N 个月的月初」而不是随手一个值 ——
        否则上面几条断言可能在比一个错误的值。"""
        from datetime import date
        y, mo = date.today().year, date.today().month
        for _ in range(36):
            mo -= 1
            if mo == 0:
                mo = 12
                y -= 1
        self.assertEqual(self.R["autoFrom"], "%04d-%02d-01" % (y, mo))


if __name__ == "__main__":
    unittest.main(verbosity=2)
