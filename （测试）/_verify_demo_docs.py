# -*- coding: utf-8 -*-
"""核对 **docs/演示脚本-5分钟.md** 里写下的每一条演示断言（演示当天照着这份清单点）。

为什么要专门写这个：演示备选边是最容易「文档说得对、现场点不到」的地方 ——
库里查得到 ≠ 页面上点得到，所以「默认图可点 / 列表里点得到」必须单独验。

⚠ 只读：全程 HTTP GET，绝不写库。
⚠ 真值只有一个来源：**运行中的服务**。直连 db 算出来的数会和网页对不上。
⚠ 口径：本文按**网页默认口径**校验，即 node_types 全勾（措施类放行）。
   不带 node_types 会拿到旧口径 1633/3921 —— 那不是网页上看到的数。

跑法（服务要先起在 8770）：
    py -3 -X utf8 "（测试）/_verify_demo_docs.py"

历史：本文件原先校验的是 docs/对接说明/04（旧口径 1633/3921），
那批断言已随「措施类默认放行」作废。04 是历史对接文档，保留不追改；
演示脚本改用新口径后，守门人换成这份。
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8770"
Q = urllib.parse.quote
# ⚠ 网页默认口径：措施类放行。少这一段整套断言会全部红在旧口径上。
NODE_TYPES = "org,policy,problem,measure"
FAILS = []


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def check(tag, cond, detail=""):
    if not cond:
        FAILS.append(tag)
    print(f"  [{'PASS' if cond else 'FAIL'}] {tag}"
          + (f"  {detail}" if detail else ""))


def norm(s):
    return " ".join((s or "").split())


def evidence(src, dst, rel="", limit=0):
    # ⚠ 别自作主张传 limit：接口对 limit 有上限（le=），传超了直接 422，
    #   而且是不带 limit 才是**接口默认的全量**口径 —— 演示脚本念的
    #   「N 条证据」用的就是全量，少传/多传都会和现场对不上。
    kw = {"src": src, "dst": dst, "rel_type": rel}
    if limit:
        kw["limit"] = limit
    return get("/api/graph/evidence?" + urllib.parse.urlencode(kw)).get("items") or []


# ---------------------------------------------------------------- ① 默认图口径
print("=" * 74)
an = get(f"/api/graph/analysis?min_degree=2&node_types={NODE_TYPES}")
g = an["graph"]
nodes = {n["id"]: n for n in g["nodes"]}
pairs = set()
for e in g["edges"]:
    pairs.add((e["from"], e["to"]))
    pairs.add((e["to"], e["from"]))

print(f"网页默认口径（措施放行）：{an['nodes']} 节点 / {an['edges']} 边 / "
      f"{an['components']} 连通块")
print(f"画布：{len(g['nodes'])} 节点 / {len(g['edges'])} 边，"
      f"折叠 {g.get('hidden_nodes')}")
print("=" * 74)

print("\n① 演示脚本 §B1 的口径数字")
check("子图 1759 节点", an["nodes"] == 1759, f"实测 {an['nodes']}")
check("子图 4154 边", an["edges"] == 4154, f"实测 {an['edges']}")
check("126 个连通块", an["components"] == 126, f"实测 {an['components']}")
check("画布 300 节点", len(g["nodes"]) == 300, f"实测 {len(g['nodes'])}")
check("画布 1449 边", len(g["edges"]) == 1449, f"实测 {len(g['edges'])}")
check("折叠 1459", g.get("hidden_nodes") == 1459, f"实测 {g.get('hidden_nodes')}")

b = an.get("betweenness") or []
check("介数 Top1 中央网信办 0.2104",
      bool(b) and b[0]["name"] == "中央网信办" and abs(b[0]["betweenness"] - 0.2104) < 1e-4,
      f"实测 {b[0]['name']} {b[0]['betweenness']}" if b else "无")
check("介数 Top2 网信办 0.1175",
      len(b) > 1 and b[1]["name"] == "网信办" and abs(b[1]["betweenness"] - 0.1175) < 1e-4,
      f"实测 {b[1]['name']} {b[1]['betweenness']}" if len(b) > 1 else "无")

fo = get(f"/api/graph/analysis?min_degree=2&focus={Q('中央网信办')}"
         f"&hop=1&node_types={NODE_TYPES}")
fb = fo.get("betweenness") or []
fg = fo["graph"]
check("一跳子图 286 节点", fg.get("scope_nodes") == 286, f"实测 {fg.get('scope_nodes')}")
check("一跳子图 1116 边", fg.get("scope_edges") == 1116, f"实测 {fg.get('scope_edges')}")
check("一跳介数 0.85 / 0.0305",
      len(fb) > 1 and abs(fb[0]["betweenness"] - 0.85) < 1e-3
      and abs(fb[1]["betweenness"] - 0.0305) < 1e-3,
      f"实测 {[x['betweenness'] for x in fb[:2]]}")

# ---------------------------------------------------------------- ② 四条演示边
print("\n② 演示脚本 §B2–B4 的四条边（都要在**边列表前 300 行**里）")
lst = get("/api/graph?limit=500").get("edges") or []
print(f"   边列表取数 {len(lst)} 条（界面只渲染前 300 行）")


def in_list(src, dst, rel):
    for i, e in enumerate(lst):
        if e["src_name"] == src and e["dst_name"] == dst and e["rel_type"] == rel:
            return i + 1
    return None


DEMO = [
    ("中央网信办", "农业农村部", "joint_issue", 24, "联合发文"),
    ("公安部", "个人信息保护法", "issue", 22, "发布"),
    ("公安部", "个人信息", "target", 32, "针对"),
    ("中央网信办", "开展2025年个人信息保护系列专项行动", "adopt", 18, "采取"),
]
for src, dst, rel, exp, cn in DEMO:
    row = in_list(src, dst, rel)
    check(f"{cn} {src} → {dst} 在列表第 {row} 行（<300 才能点到）",
          row is not None and row <= 300, f"实测第 {row} 行" if row else "列表里没有")
    items = evidence(src, dst, rel)
    check(f"   {cn} 证据 {exp} 条", len(items) == exp, f"实测 {len(items)}")
    check(f"   {cn} 全部两端点名都在句里",
          all(src in (x.get("evidence") or "") and dst in (x.get("evidence") or "")
              for x in items))

# ---------------------------------------------------------------- ③ 边①细节
print("\n③ 边① 中央网信办 ↔ 农业农村部（脚本里逐条念的）")
A, B = "中央网信办", "农业农村部"
i1 = evidence(A, B, "joint_issue")
days = sorted({(x.get("pub_day") or "")[:10] for x in i1})
check("首条是 2025-03-07 那句", (i1[0].get("pub_day") or "").startswith("2025-03-07"),
      f"实测首条 {i1[0].get('pub_day')}")
check("首条含「联合开展数字乡村强农惠农富农专项行动」",
      "联合开展数字乡村强农惠农富农专项行动" in (i1[0].get("evidence") or ""))
check("日期跨度 2024-05-15 ~ 2026-09-14",
      days[0] == "2024-05-15" and days[-1] == "2026-09-14",
      f"实测 {days[0]} ~ {days[-1]}")
check("跨 2024/2025/2026 三年",
      {x[:4] for x in days} == {"2024", "2025", "2026"}, f"实测 {sorted({x[:4] for x in days})}")
sites = defaultdict(int)
for x in i1:
    u = x.get("url") or ""
    sites["cac" if "cac.gov.cn" in u else ("zjwx" if "zjwx.gov.cn" in u else "?")] += 1
check("两站都有（cac 17 / zjwx 7）", sites.get("cac") == 17 and sites.get("zjwx") == 7,
      f"实测 {dict(sites)}")
S1 = "中央网信办、农业农村部、工业和信息化部联合印发《数字乡村高质量发展行动计划（2026—2030年）》"
h1 = [x for x in i1 if norm(x.get("evidence")) == norm(S1)]
check("第 3 条是《数字乡村…行动计划》那句（逐字一致）", bool(h1))

# ---------------------------------------------------------------- ④ 兜底话术
print("\n④ 脚本 §Q2 兜底：中央网信办 ↔ 工业和信息化部")
# ⚠ 这条边同时考两个口径，差的就是「界面上到底写几」：
#   库里 64 条关系（**全是 joint_issue**），而界面 fetch 不传 limit，
#   接口默认只返回前 50 条 —— 页面上写的是「50 条原文证据」。
#   演示念 64 而屏幕显示 50，当场就被看穿，所以两条都得钉死。
E = evidence("中央网信办", "工业和信息化部", "", limit=200)
J = evidence("中央网信办", "工业和信息化部", "joint_issue", limit=200)
Ei = evidence("中央网信办", "工业和信息化部", "")   # 界面口径：不传 limit
check("库里 64 条关系（limit=200）", len(E) == 64, f"实测 {len(E)}")
check("64 条全是 joint_issue", len(J) == 64, f"实测 {len(J)}")
check("界面默认只显示 50 条（接口 limit 默认值）", len(Ei) == 50, f"实测 {len(Ei)}")
check("首条就是「七部门联合印发」那句",
      "七部门联合印发" in (J[0].get("evidence") or ""), f"实测首条 {(J[0].get('evidence') or '')[:40]}")

# ---------------------------------------------------------------- ⑤ 七部门长句
print("\n⑤ 脚本 §Q2 的「111 条关系共用 / 12 个端点」")
uri = ("file:///" + os.path.join(ROOT, "data", "cacx.db").replace("\\", "/")
       + "?mode=ro")
con = sqlite3.connect(uri, uri=True)
rows = con.execute(
    "SELECT src_name, dst_name, evidence FROM relations "
    "WHERE evidence LIKE '%七部门联合印发%数字化绿色化%'").fetchall()
check("111 条关系共用", len(rows) == 111, f"实测 {len(rows)}")
if rows:
    ev = norm(rows[0][2])
    for nm in ("中央网信办", "国家发展改革委", "工业和信息化部", "生态环境部",
               "住房城乡建设部", "农业农村部", "商务部"):
        if nm not in ev:
            check(f"   句中含「{nm}」", False)
    check("   七个部门名全在句里", True)
    ends = {x for r in rows for x in (r[0], r[1])}
    check("   端点共 12 个", len(ends) == 12, f"实测 {len(ends)}")
    dirty = [x for x in ends if "联合" in x or x.startswith("会同")]
    check("   没有含连接词的碎片端点", not dirty, f"实测 {dirty}")
con.close()

# ---------------------------------------------------------------- ⑥ 时间轴数字
print("\n⑥ 脚本 §C 的时间轴与主题占比")
tl = get("/api/panel/timeline?top=10")
th = tl["themes"]
check("34 个月", len(th) == 34, f"实测 {len(th)}")
by_year = defaultdict(lambda: defaultdict(int))
tot = defaultdict(int)
top_gen = []
for m in th:
    y = m["month"][:4]
    tot[y] += m["total"]
    for k, v in m["problems"].items():
        by_year[y][k] += v
    best = max(m["problems"].items(), key=lambda kv: kv[1])
    top_gen.append((m["month"], best[0], best[1], m["total"]))

check("2024 发文 1494", tot["2024"] == 1494, f"实测 {tot['2024']}")
check("2025 发文 1541", tot["2025"] == 1541, f"实测 {tot['2025']}")
check("2026 发文 1380（仅 10 个月）", tot["2026"] == 1380, f"实测 {tot['2026']}")
check("2026 只有 10 个月（不满 12，脚本靠这条防呆）",
      len([m for m in th if m["month"].startswith("2026")]) == 10)

for y, exp in (("2024", 4.5), ("2025", 9.8), ("2026", 14.7)):
    g_ = by_year[y]["生成式AI"]
    pct = g_ / tot[y] * 100
    check(f"{y} 生成式AI 占比 {exp}%", abs(pct - exp) < 0.06,
          f"实测 {g_}/{tot[y]} = {pct:.1f}%")

gen_top = [mo for mo, b_, _, _ in top_gen if b_ == "生成式AI"]
check("生成式AI 当月第一恰好 2 个月", len(gen_top) == 2, f"实测 {gen_top}")
check("   且是 2025-08 / 2026-09", gen_top == ["2025-08", "2026-09"], f"实测 {gen_top}")

for y, exp in (("2024", 179), ("2025", 193), ("2026", 191)):
    top = max((m["total"] for m in th if m["month"].startswith(y)))
    check(f"{y} 年度峰值 9 月 {exp} 篇", top == exp, f"实测 {top}")
check("末月 2026-10 标为未过完",
      tl.get("scope", {}).get("last_month_partial") is True
      and tl.get("last_month") == "2026-10",
      f"实测 {tl.get('last_month')} partial={tl.get('scope',{}).get('last_month_partial')}")

# ---------------------------------------------------------------- ⑦ 边②细节
print("\n⑦ 边② 公安部 ↔ 个人信息保护法（脚本 §B3 念的那句）")
i2 = evidence("公安部", "个人信息保护法", "issue")
check("22 条", len(i2) == 22, f"实测 {len(i2)}")
check("首条含《小型个人信息处理者个人信息保护简化措施规定》",
      "小型个人信息处理者个人信息保护简化措施规定" in (i2[0].get("evidence") or ""),
      f"实测 {(i2[0].get('evidence') or '')[:50]}")
check("首条日期 2026-07-24", (i2[0].get("pub_day") or "").startswith("2026-07-24"),
      f"实测 {i2[0].get('pub_day')}")

print("\n" + "=" * 74)
if FAILS:
    print(f"❌ {len(FAILS)} 项不符：")
    for f in FAILS:
        print(f"   - {f}")
    print("\n   ⚠ 不要直接改脚本里的数字 —— 先确认是不是数据重抽了，")
    print("     再把**实测值连分母**一起写回演示脚本。")
else:
    print("✅ 全部通过：演示脚本里的断言与运行中的服务一致")
print("=" * 74)
sys.exit(0 if not FAILS else 1)
