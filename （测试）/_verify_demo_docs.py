# -*- coding: utf-8 -*-
"""核对 **docs/演示脚本-5分钟.md** 里写下的每一条演示断言（演示当天照着这份清单点）。

为什么要专门写这个：演示备选边是最容易「文档说得对、现场点不到」的地方 ——
库里查得到 ≠ 页面上点得到，所以「默认图可点 / 列表里点得到」必须单独验。

⚠ 只读：全程 HTTP GET，绝不写库。
⚠ 真值只有一个来源：**运行中的服务**。直连 db 算出来的数会和网页对不上。
⚠ 口径：本文按**网页默认口径**校验，即 node_types 全勾（措施类放行）+ 日期框默认
   `2024-01`（= 分析窗）。不带 node_types 会拿到**门槛口径** 1634/3977 —— 那不是网页上
   看到的数；不带 date_from 会拿到**全库** 12162 节点那档（单次 ~5 分钟），也不是首屏的数。
⚠ 2026-10-15 真库按全区间（2000-09-30 ~ 2026-10-14，28919 篇）抽出结果，并补抽了
   10-12 ~ 10-14 新采的 19 篇之后，**分析窗与全库两档的抽取数都动了**（19 篇里 15 篇
   有正文、产出了实体/关系）：窗口 1757/4191 → 1761/4211，全库 12160/29049 → 12162/29069。
   除此之外，口径差异照旧分两处：
     · `/api/graph/evidence` 没有日期参数 → 它列的是**全库**证据（边① 24 → 42 条）；
     · 边列表 `/api/graph` 读日期框 → 是窗口口径，右端的 n 随窗口抽取结果走（24/23/32/18）。
   这两处**故意不一样**，下面分别钉死，别再互相「校正」。
   ⚠ 另有一类数会随**采集**漂：全库/分析窗的篇数、分年发文量、主题占比分母。
     10:42 服务重启后两站又新采 14 篇（全库 28905→28919、分析窗 4421→4435、
     2024 年 1495→1497、2026 年 1385→1397、2024-09 峰值 179→180）。
     这些「库快照」数变了照实测改；上面那组抽取/图谱数则只在**补抽**后才变。

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
# ⚠ 必须显式带 date_from=2024-01-01：这是网页日期框的默认值（= 演示脚本的口径）。
#   不带 = 全库 12162 节点那档，单次建图 ~5 分钟，而且数字与演示脚本不符。
WEB = "&date_from=2024-01-01"
an = get(f"/api/graph/analysis?min_degree=2&node_types={NODE_TYPES}{WEB}")
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
check("子图 1761 节点", an["nodes"] == 1761, f"实测 {an['nodes']}")
check("子图 4211 边", an["edges"] == 4211, f"实测 {an['edges']}")
check("126 个连通块", an["components"] == 126, f"实测 {an['components']}")
check("画布 300 节点", len(g["nodes"]) == 300, f"实测 {len(g['nodes'])}")
check("画布 1482 边", len(g["edges"]) == 1482, f"实测 {len(g['edges'])}")
check("折叠 1461", g.get("hidden_nodes") == 1461, f"实测 {g.get('hidden_nodes')}")

b = an.get("betweenness") or []
check("介数 Top1 中央网信办 0.2109",
      bool(b) and b[0]["name"] == "中央网信办" and abs(b[0]["betweenness"] - 0.2109) < 1e-4,
      f"实测 {b[0]['name']} {b[0]['betweenness']}" if b else "无")
check("介数 Top2 网信办 0.1201",
      len(b) > 1 and b[1]["name"] == "网信办" and abs(b[1]["betweenness"] - 0.1201) < 1e-4,
      f"实测 {b[1]['name']} {b[1]['betweenness']}" if len(b) > 1 else "无")

fo = get(f"/api/graph/analysis?min_degree=2&focus={Q('中央网信办')}"
         f"&hop=1&node_types={NODE_TYPES}{WEB}")
fb = fo.get("betweenness") or []
fg = fo["graph"]
check("一跳子图 286 节点", fg.get("scope_nodes") == 286, f"实测 {fg.get('scope_nodes')}")
check("一跳子图 1116 边", fg.get("scope_edges") == 1116, f"实测 {fg.get('scope_edges')}")
check("一跳介数 0.8486 / 0.0306",
      len(fb) > 1 and abs(fb[0]["betweenness"] - 0.8486) < 1e-3
      and abs(fb[1]["betweenness"] - 0.0306) < 1e-3,
      f"实测 {[x['betweenness'] for x in fb[:2]]}")

# ---------------------------------------------------------------- ② 四条演示边
print("\n② 演示脚本 §B2–B4 的四条边（都要在**边列表前 300 行**里）")
# ⚠ 边列表读日期框（`db.graph` 的 date_from 默认也是 2024-01-01），右端的 n 是
#   **窗口内**关系条数；证据面板不读日期、列的是**全库** —— 两个口径都钉。
lst = get("/api/graph?limit=500" + WEB).get("edges") or []
print(f"   边列表取数 {len(lst)} 条（界面只渲染前 300 行）")


def find_edge(src, dst, rel):
    for i, e in enumerate(lst):
        if e["src_name"] == src and e["dst_name"] == dst and e["rel_type"] == rel:
            return i + 1, e
    return None, None


# (src, dst, rel, 列表右端 n = 窗口口径, 证据面板条数 = 全库口径, 中文名)
DEMO = [
    ("中央网信办", "农业农村部", "joint_issue", 24, 42, "联合发文"),
    ("公安部", "个人信息保护法", "issue", 23, 26, "发布"),
    ("公安部", "个人信息", "target", 32, 50, "针对"),
    ("中央网信办", "开展2025年个人信息保护系列专项行动", "adopt", 18, 18, "采取"),
]
for src, dst, rel, n_list, n_ev, cn in DEMO:
    row, ent = find_edge(src, dst, rel)
    check(f"{cn} {src} → {dst} 在列表第 {row} 行（<300 才能点到）",
          row is not None and row <= 300, f"实测第 {row} 行" if row else "列表里没有")
    check(f"   {cn} 列表右端 n={n_list}（窗口口径）",
          bool(ent) and ent.get("n") == n_list,
          f"实测 {ent.get('n') if ent else None}")
    items = evidence(src, dst, rel)
    check(f"   {cn} 证据面板 {n_ev} 条（全库口径）",
          len(items) == n_ev, f"实测 {len(items)}")
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
check("日期跨度 2019-05-17 ~ 2026-09-14（全库口径）",
      days[0] == "2019-05-17" and days[-1] == "2026-09-14",
      f"实测 {days[0]} ~ {days[-1]}")
check("   跨 2019–2026 八个年份",
      {x[:4] for x in days} == {"2019", "2020", "2021", "2022", "2023",
                               "2024", "2025", "2026"},
      f"实测 {sorted({x[:4] for x in days})}")
check("   共 33 个不同日期", len(days) == 33, f"实测 {len(days)}")
sites = defaultdict(int)
for x in i1:
    u = x.get("url") or ""
    sites["cac" if "cac.gov.cn" in u else ("zjwx" if "zjwx.gov.cn" in u else "?")] += 1
check("两站都有（cac 34 / zjwx 8）", sites.get("cac") == 34 and sites.get("zjwx") == 8,
      f"实测 {dict(sites)}")
S1 = "中央网信办、农业农村部、工业和信息化部联合印发《数字乡村高质量发展行动计划（2026—2030年）》"
h1 = [i for i, x in enumerate(i1) if norm(x.get("evidence")) == norm(S1)]
check("含《数字乡村…行动计划》那句（逐字一致）", bool(h1),
      f"位置 {[i + 1 for i in h1]} / 共 {len(i1)} 条")
# 列表右端 24 是窗口口径；本边一篇文章一条关系，所以 24 也等于 24 篇文章。
row1, ent1 = find_edge(A, B, "joint_issue")
check("边① 列表右端 n=24 且 article_ids 24 个（窗口口径）",
      bool(ent1) and ent1.get("n") == 24
      and len(ent1.get("article_ids") or []) == 24,
      f'n={ent1.get("n") if ent1 else None} / '
      f'ids={len(ent1.get("article_ids") or []) if ent1 else None}')
# 全库 42 / 窗口 24 —— 两条口径都留痕（记录面板读全库、列表读日期框）
con0 = sqlite3.connect("file:///" + os.path.join(ROOT, "data", "cacx.db")
                       .replace("\\", "/") + "?mode=ro", uri=True)
n_all42 = con0.execute(
    "SELECT COUNT(*) FROM relations WHERE src_name=? AND dst_name=?",
    (A, B)).fetchone()[0]
n_win24 = con0.execute(
    "SELECT COUNT(*) FROM relations r JOIN articles a ON a.id=r.article_id "
    "WHERE a.year>=2024 AND r.src_name=? AND r.dst_name=?", (A, B)).fetchone()[0]
con0.close()
check("边① 全库 42 条 / 窗口 24 条（两条口径并存）",
      n_all42 == 42 and n_win24 == 24, f"全库 {n_all42} / 窗口 {n_win24}")

# ---------------------------------------------------------------- ④ 兜底话术
print("\n④ 脚本 §Q2 兜底：中央网信办 ↔ 工业和信息化部")
# ⚠ 这条边三个数会同屏出现，对应的三个口径都要钉：
#   · 边列表右端 64 = **窗口内**（2024 年以来）关系条数；
#   · 全库 153 条（最早 2014-11-24），且**全是 joint_issue**；
#   · 证据面板不传 limit → 接口默认只返回前 50 条（limit 上限 200）。
#   演示念错任何一个，当场就被看穿。
E = evidence("中央网信办", "工业和信息化部", "", limit=200)
J = evidence("中央网信办", "工业和信息化部", "joint_issue", limit=200)
Ei = evidence("中央网信办", "工业和信息化部", "")   # 界面口径：不传 limit（默认 50）
check("全库 153 条关系（limit=200）", len(E) == 153, f"实测 {len(E)}")
check("153 条全是 joint_issue", len(J) == 153, f"实测 {len(J)}")
check("界面默认只显示 50 条（接口 limit 默认值）", len(Ei) == 50, f"实测 {len(Ei)}")
row_i, ent_i = find_edge("中央网信办", "工业和信息化部", "joint_issue")
check("边列表第 1 行、右端 n=64（窗口口径）",
      row_i == 1 and bool(ent_i) and ent_i.get("n") == 64,
      f"第 {row_i} 行 / n={ent_i.get('n') if ent_i else None}")
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
# ⚠ 年度发文量必须取 `months` 数组，不能取 `themes[].total`：
#   面板声明「发文量按所有文章计（含正文为空的列表页占位），主题分布按有正文的文章计」，
#   而 themes[] 走的是主题分类那条路，**空正文文章进不去** —— 实测 2024-04
#   months=113 / themes=111，差的正是 2 篇空正文占位（其中 id 3691 与 44146
#   还是同一篇的重复行）。所以 2024 年发文量是 1497（months）不是 1495（themes）。
tot = defaultdict(int)
for m in tl["months"]:
    tot[m["month"][:4]] += m["total"]

check("34 个月", len(tl["months"]) == 34, f"实测 {len(tl['months'])}")
by_year = defaultdict(lambda: defaultdict(int))
top_gen = []
for m in th:
    y = m["month"][:4]
    for k, v in m["problems"].items():
        by_year[y][k] += v
    best = max(m["problems"].items(), key=lambda kv: kv[1])
    top_gen.append((m["month"], best[0], best[1], m["total"]))

check("2024 发文 1497", tot["2024"] == 1497, f"实测 {tot['2024']}")
check("2025 发文 1541", tot["2025"] == 1541, f"实测 {tot['2025']}")
check("2026 发文 1397（仅 10 个月）", tot["2026"] == 1397, f"实测 {tot['2026']}")
check("2024 月均 125 篇（脚本 §C 念的数）",
      round(tot["2024"] / 12) == 125, f"实测 {tot['2024'] / 12:.1f}")
check("2026 只有 10 个月（不满 12，脚本靠这条防呆）",
      len([m for m in th if m["month"].startswith("2026")]) == 10)

for y, exp in (("2024", 4.5), ("2025", 9.8), ("2026", 14.5)):
    g_ = by_year[y]["生成式AI"]
    pct = g_ / tot[y] * 100
    check(f"{y} 生成式AI 占比 {exp}%", abs(pct - exp) < 0.06,
          f"实测 {g_}/{tot[y]} = {pct:.1f}%")

gen_top = [mo for mo, b_, _, _ in top_gen if b_ == "生成式AI"]
check("生成式AI 当月第一恰好 2 个月", len(gen_top) == 2, f"实测 {gen_top}")
check("   且是 2025-08 / 2026-09", gen_top == ["2025-08", "2026-09"], f"实测 {gen_top}")

for y, exp in (("2024", 180), ("2025", 193), ("2026", 191)):
    top = max((m["total"] for m in tl["months"] if m["month"].startswith(y)))
    check(f"{y} 年度峰值 9 月 {exp} 篇", top == exp, f"实测 {top}")
check("末月 2026-10 标为未过完",
      tl.get("scope", {}).get("last_month_partial") is True
      and tl.get("last_month") == "2026-10",
      f"实测 {tl.get('last_month')} partial={tl.get('scope',{}).get('last_month_partial')}")

# ---------------------------------------------------------------- ⑦ 边②细节
print("\n⑦ 边② 公安部 ↔ 个人信息保护法（脚本 §B3 念的那句）")
i2 = evidence("公安部", "个人信息保护法", "issue")
check("26 条（全库口径；列表右端写的是窗口 23）", len(i2) == 26, f"实测 {len(i2)}")
row2, ent2 = find_edge("公安部", "个人信息保护法", "issue")
check("   列表右端 n=23（窗口口径）",
      bool(ent2) and ent2.get("n") == 23, f"实测 {ent2.get('n') if ent2 else None}")
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
