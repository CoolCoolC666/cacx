# -*- coding: utf-8 -*-
"""
前端运行时测试（Node 沙箱真跑 <script>）。
不靠"看着像对的"——把 index.html 的 script 丢进 vm 里真执行，
未捕获的 ReferenceError 会直接让断言失败。
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
HTML = ROOT / "cacx" / "web" / "static" / "index.html"

html = HTML.read_text(encoding="utf-8")
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
assert scripts, "没找到 <script> 块"
code = "\n".join(scripts)

# 桩：记录被调用的方法，避免真发请求
calls = []
HARNESS = r"""
const calls = [];
function mkEl(id){
  const el = {
    id, value:'', textContent:'', innerHTML:'', checked:false, style:{},
    className:'', classList:{add(){},remove(){},toggle(){},contains(){return false}},
    appendChild(){}, remove(){}, click(){}, scrollIntoView(){},
    setAttribute(){}, getAttribute(){return null},
    addEventListener(){}, querySelector(){return mkEl('q')},
    querySelectorAll(){return []},
    dataset:{}, children:[], options:[]
  };
  return el;
}
const _els = {};
// ⚠ 不能叫 $：页面自己的 script 里也定义了 $，重复声明会
//   直接 SyntaxError，连断言区都跑不到。
function stubEl(id){
  if(!_els[id]) _els[id] = mkEl(id);
  return _els[id];
}
globalThis.document = {
  getElementById: stubEl,
  createElement: ()=>mkEl('new'),
  querySelector: ()=>mkEl('q'),
  querySelectorAll: ()=>[],
  body:{appendChild(){},},
  addEventListener(){}
};
globalThis.window = globalThis;
globalThis.location = {href:'http://127.0.0.1:8770/', search:'', pathname:'/'};
globalThis.alert = (m)=>{ calls.push(['alert', m]); };
globalThis.confirm = ()=>true;
globalThis.navigator = {clipboard:{writeText(){}}};
globalThis.localStorage = {getItem(){return null},setItem(){},removeItem(){}};
globalThis.URLSearchParams = URLSearchParams;
globalThis.fetch = async (url, opt) => {
  calls.push(['fetch', String(url)]);
  return { ok:true, json: async () => ({ items:[], columns:[], nodes:[], edges:[] }) };
};
globalThis.URL = { createObjectURL(){return 'blob:x';}, revokeObjectURL(){} };
globalThis.Blob = class { constructor(){} };
"""

with tempfile.TemporaryDirectory() as td:
    js = Path(td) / "t.js"
    js.write_text(HARNESS + "\n" + code + """
// ---- 断言区 ----
const fails = [];
function must(cond, msg){ if(!cond) fails.push(msg); }

function run(name, fn){
  try{ fn(); console.log('  ok   ' + name); }
  catch(e){ fails.push(name + ' → ' + e.message); console.log('  FAIL ' + name + '  ' + e.message); }
}

// 1. 切到样本配置器：应拉栏目选项，且字段名用 columns
run('showTab(sample) 不抛', () => { showTab('sample'); });
run('loadSampleOptions 用 columns 字段', () => {
  calls.length = 0;
  globalThis.fetch = async () => ({ ok:true, json: async () => ({
    columns:[{site:'cac', column_name:'网信要闻', n:741, years:3}]}) });
  return loadSampleOptions().then(()=>{
    must(calls.some(c=>String(c[1]).includes('/api/sample/options')),
         '没请求 /api/sample/options');
  });
});

// 2. spQs 必须带上全部可配参数
run('spQs 带齐 栏目/区间/篇数/策略', () => {
  $('sp_site').value='cac'; $('sp_column').value='网信要闻';
  $('sp_from').value='2024-01-01'; $('sp_to').value='2099-12-31';
  $('sp_limit').value='500'; $('sp_strategy').value='stratified';
  const q = spQs().toString();
  ['site=cac','column=','date_from=2024-01-01','date_to=2099-12-31',
   'limit=500','strategy=stratified'].forEach(k=>
     must(q.includes(k), 'spQs 缺 '+k+' → '+q));
});

// 3. 样本 CSV 必须走后端端点（不能前端拼）
//    ⚠ 下载走 <a href> 而不是 fetch —— 所以要看 href，不是看 fetch 调用。
//    这一点正好符合项目里 dlPanel/dlSnapshot 的既有做法。
run('dlSampleCsv 走 /api/sample/export', () => {
  $('sp_column').value='网信要闻'; $('sp_limit').value='500';
  $('sp_strategy').value='stratified';
  let href = null;
  globalThis.document = Object.assign({}, globalThis.document, {
    createElement: () => { const el = mkEl('a');
      Object.defineProperty(el, 'href', {
        set(v){ href = v; } }); return el; },
    body: { appendChild(){}, }
  });
  dlSampleCsv();
  must(href && href.includes('/api/sample/export'),
       'CSV href 不是后端端点：' + href);
});

// 4. runSample 收到响应后能渲染（含抽样质量警告）
run('runSample 渲染概览与警告', () => {
  globalThis.fetch = async () => ({ ok:true, json: async () => ({
    n:300, n_usable:280, total_pool:1267, limit:300,
    strategy:'recent', years_taken:{2025:300}, years_pool:[], 
    date_min:'2025-01-01', date_max:'2025-12-31',
    items:[{id:1,pub_day:'2025-01-01',title:'测试',url:'http://x',column_name:'网信要闻'}]
  })});
  return runSample().then(()=>{
    must($('sp_summary').innerHTML.includes('2025'),
         '概览没渲染年份');
    must($('sp_summary').innerHTML.includes('2024'),
         '⚠ 单年份的抽样质量警告没出现');
    must($('sp_summary').innerHTML.includes('空正文'),
         '空正文警告没出现');
  });
});

// 5. 分层抽样多年份 → 不该报「2024 没覆盖」
run('runSample 分层多年份不误报', () => {
  globalThis.fetch = async () => ({ ok:true, json: async () => ({
    n:300, n_usable:300, total_pool:1267, limit:300,
    strategy:'stratified', years_taken:{2024:100,2025:100,2026:100},
    years_pool:[], date_min:'2024-01-01', date_max:'2026-10-10',
    items:[]
  })});
  return runSample().then(()=>{
    must(!$('sp_summary').innerHTML.includes('2024 年没有被覆盖'),
         '分层抽样被误报为缺 2024');
    must($('sp_summary').innerHTML.includes('覆盖了区间内的全部年份'),
         '正向确认没出现');
  });
});

// 6. 图谱：点关系要能列出证据
run('gpDetail 列出原文证据', () => {
  GP_CACHE = [{src_name:'中央网信办', dst_name:'公安部', rel_type:'co_issue',
               n:2, weight:2, article_ids:[11,12]}];
  globalThis.fetch = async () => ({ ok:true, json: async () => ({ items:[
    {src_name:'中央网信办', dst_name:'公安部', rel_type:'co_issue',
     article_id:11, article_title:'联合部署清朗', pub_day:'2024-05-01',
     evidence:'中央网信办 公安部 联合部署'},
    {src_name:'中央网信办', dst_name:'公安部', rel_type:'co_issue',
     article_id:12, article_title:'联合发布通报', pub_day:'2025-06-01',
     evidence:''}
  ]})});
  gpDetail(0);
  return new Promise(res=>setTimeout(()=>{
    must($('gp_detail').innerHTML.includes('中央网信办'), '详情没渲染');
    must($('gp_detail').innerHTML.includes('→'), '缺箭头');
    res();
  }, 60));
});

// 7. 不许残留已改名的函数调用
run('不残留 openArt 调用', () => {
  const src = globalThis.__code || '';
  must(!src.includes('openArt('), '还有 openArt( 调用，应为 detail(');
});

// 8. 后端少给字段时不能整页崩
//    ⚠ 回归：for...of 一个 undefined 抛 TypeError，Promise 无人 catch，
//    页面会一直停在「加载中…」，看起来像服务挂了。
run('loadFacets 缺 sites/years 不崩', () => {
  globalThis.fetch = async (u) => ({ ok:true, json: async () =>
    String(u).includes('/api/facets') ? { total:10, empty:1 } : {} });
  return loadFacets().then(()=> must(true,''));
});

// 9. 检索主路径同样要兜底
run('go() 响应缺 total 不崩', () => {
  globalThis.fetch = async () => ({ ok:true, json: async () => ({}) });
  return go(1).then(()=>{
    must(TOTAL === 0, 'TOTAL 应兜底为 0，实际 '+TOTAL);
    must($('meta').innerHTML.includes('0'), 'meta 没渲染兜底数字');
  });
});

// 11. ⚠ 回归：浏览器实测撞到的 —— 后端 /api/sample 默认只回
//      items_sample（前 20 条预览），完整清单在 items_total。
//      不带 with_items=1 的话 r.items 是 undefined，文章列表一片空白，
//      而且**没有任何报错** —— 光看概览数字一切正常。
run('runSample 显式请求完整清单', () => {
  let asked = '';
  globalThis.fetch = async (u) => { asked = String(u);
    return { ok:true, json: async () => ({
      n:2, n_usable:2, total_pool:100, limit:2, strategy:'stratified',
      years_taken:{2024:1,2025:1}, years_pool:[], date_min:'2024-01-01',
      date_max:'2025-01-01',
      items:[{id:1,pub_day:'2024-01-01',title:'甲',url:'http://x',column_name:'网信要闻'},
             {id:2,pub_day:'2025-01-01',title:'乙',url:'http://y',column_name:'网信要闻'}]
    })}; };
  return runSample().then(()=>{
    must(asked.includes('with_items=1'), '没带 with_items=1：'+asked);
    must($('sp_list').innerHTML.includes('甲'), '列表没渲染文章');
    must($('sp_list').innerHTML.includes('乙'), '列表只渲染了一部分');
  });
});

// 12. 退回 items_sample 也不该崩（后端改了默认行为时的兜底）
run('runSample 只有 items_sample 也不崩', () => {
  globalThis.fetch = async () => ({ ok:true, json: async () => ({
    n:1, n_usable:1, total_pool:100, limit:1, strategy:'stratified',
    years_taken:{2024:1}, years_pool:[], date_min:'2024-01-01', date_max:'2024-01-01',
    items_total:1,
    items_sample:[{id:9,pub_day:'2024-01-01',title:'预览',url:'http://z',column_name:'x'}]
  })});
  return runSample().then(()=> must(true,''));
});
// 10. ⚠ 回归：响应用 {items:[]} 时 runSample 会抛 TypeError
run('runSample 响应缺 items 不崩', () => {
  globalThis.fetch = async () => ({ ok:true, json: async () => ({
    n:0, n_usable:0, total_pool:0, limit:500, strategy:'stratified',
    years_taken:{}, years_pool:[], date_min:'', date_max:''
  })});
  return runSample().then(()=> must(true,''));
});

if(fails.length){
  console.log('\\n失败 '+fails.length+' 项:');
  fails.forEach(f=>console.log('  - '+f));
  process.exit(1);
}
console.log('\\n全部通过');
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    print(r.stdout)
    if r.stderr.strip():
        print("--- stderr ---")
        print(r.stderr[-3000:])
    sys.exit(r.returncode)
