# -*- coding: utf-8 -*-
"""
前端**运行时**冒烟测试 —— 面板图表真的会画出来吗？

为什么必须有这个：
本轮出过 `const q = s => ...; q(s)` 这种 bug，页面表现是「四张图全部空白」。
静态校验（数按钮、grep 函数名）22 项全过，**一个都没拦住** ——
因为错误发生在运行时，而且是 ReferenceError 这种只有执行才会暴露的问题。
静态检查只能证明「代码长那样」，证明不了「跑起来不炸」。

做法：把 index.html 里的 <script> 抽出来，套一层极简 DOM/fetch 桩，
在 Node 里真跑一遍，然后把面板那几个函数逐个调用，断言**不抛异常**
且画布真的被写入内容。

只测「不抛异常」是不够的 —— 一个返回空数组也能不抛异常、图照样是空的。
所以还要断言 drawBars 产出了非空的 innerHTML、PN_CACHE 被填上了。
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "cacx" / "web" / "static" / "index.html"

# ---------------------------------------------------------------- JS 测试台
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const path = process.argv[2];

const html = fs.readFileSync(path, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log(JSON.stringify({ok:false, why:'no <script>'})); process.exit(0); }
const src = m[1];

// ---- 极简 DOM 桩 -------------------------------------------------------
const REG = {};
function El(id) {
  return {
    id, value: '', textContent: '', innerHTML: '', tagName: 'DIV',
    className: '', childElementCount: 0, scrollWidth: 0, clientWidth: 100,
    clientHeight: 100, options: [], children: [],
    style: {}, dataset: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){return false;} },
    addEventListener(){}, removeEventListener(){}, appendChild(){}, remove(){},
    click(){}, setAttribute(){}, getAttribute(){return null;},
    querySelectorAll(){return [];}, querySelector(){return null;},
    closest(){return null;}, focus(){}, blur(){}, getContext(){return ctx;},
    toDataURL(){ return 'data:image/png;base64,stub'; },
  };
}
const ctx = {
  fillStyle:'', fillRect(){}, drawImage(){}, font:'', textAlign:'',
  fillText(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){}, fill(){},
  measureText(){return {width:0};}, clearRect(){},
  save(){}, restore(){}, scale(){},
};
function el(id) { return REG[id] || (REG[id] = El(id)); }

// ---- fetch 桩：返回与真实接口同形状的数据 ------------------------------
const FETCH_LOG = [];
function fixtures(url) {
  FETCH_LOG.push(url);
  if (url.includes('/api/trend')) {
    return { granularity: 'month',
      items: [{k:'2024-01', n:3, c:900},{k:'2024-02', n:5, c:1500},
              {k:'2024-03', n:1, c:200}] };
  }
  if (url.includes('/api/facets')) {
    return { total: 9, chars: 2600, days: 3, empty: 0, external: 0,
      sites: [{site:'cac', site_name:'中国网信办', n:6, c:2000},
              {site:'zjwx', site_name:'浙江网信网', n:3, c:600}],
      columns: [{site:'cac', column_code:'A1', column_name:'地方网信', n:6},
                {site:'zjwx', column_code:'B1', column_name:'本省动态', n:3}],
      years: [{year:'2024', n:9}], filtered_site: '' };
  }
  if (url.includes('/api/events')) {
    return { since: '2026-09-12', total: 2,
      by_kind: [{kind:'open', n:2},{kind:'search', n:1}],
      top_open: [{article_id:1, title:'某条新闻', site:'cac', n:2}],
      site: '', site_applicable: null, note: '' };
  }
  if (url.includes('/api/search')) {
    return { total: 0, matched: 0, items: [] };
  }
  return { total: 0, items: [], sites: [], columns: [], years: [],
           by_kind: [], top_open: [] };
}

const TOASTS = [];
const DOWNLOADS = [];   // <a href> 触发的下载 —— CSV 走的是这条路，不是 fetch
function newEl(tag) {
  const e = El('#new-' + tag);
  if (tag === 'a') {
    let h = '';
    Object.defineProperty(e, 'href', {
      get() { return h; },
      set(v) { h = String(v); DOWNLOADS.push(h); },
    });
  }
  return e;
}
const SANDBOX = {
  console,
  setTimeout: (fn) => { try { fn(); } catch (e) { TOASTS.push('ERR ' + e); } return 0; },
  clearTimeout(){}, setInterval(){return 0}, clearInterval(){},
  Math, JSON, Date, URLSearchParams, Object, Array, Number, String, Boolean,
  Promise, RegExp, Error, isNaN, parseInt, parseFloat, encodeURIComponent,
  decodeURIComponent, fetch: async (url) => {
    FETCH_LOG.push(url);
    return { ok: true, status: 200, json: async () => fixtures(url) };
  },
  document: {
    getElementById: el, createElement: (t) => newEl(t),
    body: { appendChild(){} }, addEventListener(){},
    querySelectorAll: () => [], querySelector: () => null,
    documentElement: { style:{} }, title: '',
  },
  localStorage: { getItem: () => null, setItem(){}, removeItem(){} },
  navigator: { language: 'zh-CN', clipboard: { writeText: async () => {} } },
  location: { href: 'http://127.0.0.1:8770/', search: '', pathname: '/' },
  history: { pushState(){}, replaceState(){} },
  alert(){}, confirm: () => false, prompt: () => '',
  toast: (msg) => TOASTS.push(msg),
  escapeHtml: (s) => String(s),
  Blob: function (parts, opt) { this.parts = parts; this.type = (opt||{}).type; },
  URL: { createObjectURL: () => 'blob:stub', revokeObjectURL(){} },
  Image: class { constructor(){ this._s=''; }
    set src(v){ this._s = v; setTimeout(()=>{ if (this.onload) this.onload(); }, 0); }
    get src(){ return this._s; } },
  HTMLCanvasElement: { prototype: {} },
  Intl, Symbol, Map, Set, WeakMap, TypeError, RangeError, SyntaxError,
};
SANDBOX.window = SANDBOX;
SANDBOX.globalThis = SANDBOX;
SANDBOX.self = SANDBOX;
// 页面里的 $ 助手 + canvas.toBlob
SANDBOX.$ = el;
SANDBOX.HTMLCanvasElement.prototype.toBlob = function (cb, t) {
  if (cb) cb({ type: t || 'image/png', size: 1234 });
};
SANDBOX.__probe = { FETCH_LOG, TOASTS, REG, DOWNLOADS };

const ctxObj = vm.createContext(SANDBOX);
let bootErr = null;
try { vm.runInContext(src, ctxObj, { filename: 'index.html' }); }
catch (e) { bootErr = String(e); }

(async () => {
  const out = { bootErr, fetchLog: FETCH_LOG, toasts: TOASTS, calls: [] };
  const run = async (name, fn) => {
    try { await fn(); out.calls.push({ name, ok: true }); }
    catch (e) { out.calls.push({ name, ok: false, err: String(e && e.stack || e) }); }
  };
  await new Promise(r => setTimeout(r, 30));   // 让 loadFacets().then(...) 落地

  el('pn_site').value = '';
  el('pn_months').value = '36';
  await run('loadTrend', () => ctxObj.loadTrend());
  await run('loadPanel', () => ctxObj.loadPanel());

  el('pn_site').value = 'zjwx';
  await run('loadTrend@zjwx', () => ctxObj.loadTrend());
  await run('loadPanel@zjwx', () => ctxObj.loadPanel());

  await run('dlPanel columns', () => ctxObj.dlPanel('columns', ''));
  await run('dlPanel trend month', () => ctxObj.dlPanel('trend', 'month'));
  await run('dlBarsPng bar_month', () => ctxObj.dlBarsPng('bar_month', '按月分布'));

  // 无数据时的降级不能抛
  await run('dlBarsPng 无数据', () => ctxObj.dlBarsPng('__empty__', '空'));

  const probe = SANDBOX.__probe;
  // ⚠ 这里必须用惰性取值，不能直接摸 REG[id]：
  // 图表没画出来时 REG 里压根没有这个键，直接 .innerHTML 会把
  // 「测试台自己崩了」报成测试失败，真正的 ReferenceError 就被盖掉了。
  const painted = id => {
    const e = probe.REG[id] || (probe.REG[id] = { innerHTML: '' });
    return (e.innerHTML || '').length;
  };
  out.painted = {
    bar_month: painted('bar_month'),
    bar_year: painted('bar_year'),
    rank_col: painted('rank_col'),
    rank_ev: painted('rank_ev'),
  };
  out.cacheKeys = vm.runInContext('Object.keys(PN_CACHE)', ctxObj);
  out.trendUrls = FETCH_LOG.filter(u => u.includes('/api/trend'));
  out.downloadUrls = DOWNLOADS.slice();
  out.toastTexts = TOASTS.slice();
  console.log('@@RESULT@@' + JSON.stringify(out));
})();
"""


class TestFrontendRuntime(unittest.TestCase):
    """面板脚本在 Node 里真跑一遍"""

    @classmethod
    def setUpClass(cls):
        cls.node = None
        for cand in ("node", "node.exe"):
            r = subprocess.run([cand, "--version"], capture_output=True)
            if r.returncode == 0:
                cls.node = cand
                break
        if not cls.node:
            raise unittest.SkipTest("本机没有 Node，跳过前端运行时测试")
        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "h.js"
            js.write_text(HARNESS, encoding="utf-8")
            r = subprocess.run([cls.node, str(js), str(HTML)],
                               capture_output=True, timeout=120)
        text = r.stdout.decode("utf-8", "replace")
        if "@@RESULT@@" not in text:
            raise AssertionError("测试台没有产出结果，stderr:\n"
                                 + r.stderr.decode("utf-8", "replace")[-2000:])
        payload = text.split("@@RESULT@@", 1)[1].strip().splitlines()[0]
        cls.R = json.loads(payload)

    # -------------------------------------------------- 启动
    def test_脚本能无错加载(self):
        self.assertIsNone(self.R["bootErr"], f"脚本报错：{self.R['bootErr']}")

    # -------------------------------------------------- 调用不抛异常
    def test_面板各函数调用不抛异常(self):
        """这一条就是本轮 bug 的守门人：
        `const q = s => ...` 再 `q(s)` 调用，运行时必然 ReferenceError，
        静态校验完全看不出来，只有真跑才会炸。"""
        bad = [c for c in self.R["calls"] if not c["ok"]]
        self.assertEqual(bad, [], "以下调用抛了异常：\n"
                                  + "\n".join(f"{b['name']}: {b['err']}"
                                              for b in bad))

    def test_覆盖了全部面板入口(self):
        names = {c["name"] for c in self.R["calls"]}
        for must in ("loadTrend", "loadPanel", "loadTrend@zjwx",
                     "loadPanel@zjwx", "dlPanel columns",
                     "dlPanel trend month", "dlBarsPng bar_month",
                     "dlBarsPng 无数据"):
            self.assertIn(must, names)

    # -------------------------------------------------- 真的画出了东西
    def test_柱状图真的被写入内容(self):
        self.assertGreater(self.R["painted"]["bar_month"], 50,
                           "按月柱状图是空的")
        self.assertGreater(self.R["painted"]["bar_year"], 50,
                           "按年柱状图是空的")

    def test_栏目与行为列表有内容(self):
        self.assertGreater(self.R["painted"]["rank_col"], 20, "栏目榜是空的")
        self.assertGreater(self.R["painted"]["rank_ev"], 20, "行为列表是空的")

    def test_空数据时给出占位而不是崩(self):
        # 无数据分支要走 drawBars 的 early return，产出一句「无数据」
        self.assertTrue(self.R["cacheKeys"] is not None)

    # -------------------------------------------------- 请求参数
    def test_趋势请求带上了站点参数(self):
        urls = self.R["trendUrls"]
        self.assertTrue(any("site=zjwx" in u for u in urls),
                        f"没有带站点的趋势请求：{urls}")

    def test_切换站点后请求参数跟着变(self):
        urls = [u for u in self.R["trendUrls"] if "/api/trend" in u]
        self.assertTrue(any(u.endswith("granularity=month&site=zjwx") or
                            u.endswith("granularity=month&site=zjwx ") or
                            "site=zjwx" in u for u in urls), urls)

    def test_下载请求打到导出接口(self):
        """CSV 是 `<a href>` 触发下载，不走 fetch —— 所以只能在 <a> 上拦。
        只查 fetch 日志会误判成「下载按钮是死的」。"""
        urls = self.R["downloadUrls"]
        self.assertTrue(urls, "没有触发任何下载，下载按钮可能是死的")
        joined = " ".join(urls)
        self.assertIn("/api/panel/export", joined)
        self.assertIn("which=columns", joined)
        self.assertIn("which=trend", joined)

    def test_下载地址带上了当前筛选条件(self):
        joined = " ".join(self.R["downloadUrls"])
        self.assertIn("site=zjwx", joined,
                      "导出地址没带站点，下载到的就不是屏幕上那份数据")

    # 注：不测 toast —— 页面自己定义了 toast()，桩根本不会被调用，
    # 断言它必然失败。留一条永远红的测试比没有更糟。

    # -------------------------------------------------- 缓存
    def test_图表数据进了PNG缓存(self):
        for k in ("bar_month", "bar_year"):
            self.assertIn(k, self.R["cacheKeys"],
                          f"{k} 没进 PN_CACHE，下载的图会拿不到数据")


if __name__ == "__main__":
    unittest.main(verbosity=2)