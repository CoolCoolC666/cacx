# -*- coding: utf-8 -*-
"""核对 docs/对接说明/04 里写下的**每一条演示断言**（演示当天照着这份清单点）。

为什么要专门写这个：演示备选边是最容易「文档说得对、现场点不到」的地方 ——
库里查得到 ≠ 页面上点得到（度数不够 / 被低置信规则踢掉的节点根本不画出来），
所以「默认图可点」必须单独验一遍。

⚠ 只读：全程走 HTTP GET，绝不写库。
⚠ 真值只有一个来源：**运行中的服务**。直连 db 算出来的数会和网页对不上。

跑法（服务要先起在 8770）：
    py -3 "（测试）/_verify_demo_docs.py"
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8770"
Q = urllib.parse.quote
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


# ---------------------------------------------------------------- 默认图
# ⚠ /api/graph/vis 只回 {html, nodes, edges, bytes, scope_note}，
#   nodes/edges 是**计数**；节点数组在 /api/graph/analysis 的 graph 里。
an = get("/api/graph/analysis?min_degree=2")
g = an["graph"]
nodes = {n["id"]: n for n in g["nodes"]}
pairs = set()
for e in g["edges"]:
    pairs.add((e["from"], e["to"]))
    pairs.add((e["to"], e["from"]))

print("=" * 72)
print(f"默认图（min_degree={an['filters']['min_degree']}，无其他筛选）"
      f"：{an['nodes']} 节点 / {an['edges']} 边")
print("=" * 72)

print("\n① 文档「实测表」里的度数与可点性")
for a, b, da, db in (("中央网信办", "农业农村部", 243, 34),
                     ("国家网信办", "网络数据安全风险评估办法", 135, 6),
                     ("中央网信办", "工业和信息化部", 243, 126)):
    check(f"{a} ↔ {b} 在默认图上可点", (a, b) in pairs)
    x, y = nodes.get(a, {}).get("degree"), nodes.get(b, {}).get("degree")
    check(f"   度数 {da}/{db}", x == da and y == db, f"实测 {x}/{y}")

print("\n② 备选① 中央网信办 ↔ 农业农村部")
A, B = "中央网信办", "农业农村部"
i1 = get(f"/api/graph/evidence?src={Q(A)}&dst={Q(B)}&limit=50")["items"]
check("证据 24 条", len(i1) == 24, f"实测 {len(i1)}")
check("全部两端点名都在句里",
      all(A in (x.get("evidence") or "") and B in (x.get("evidence") or "")
          for x in i1))
check("全部是 co_issue", all(x.get("rel_type") == "co_issue" for x in i1))
S1 = ("中央网信办、农业农村部、工业和信息化部联合印发"
      "《数字乡村高质量发展行动计划（2026—2030年）》")
h1 = [x for x in i1 if norm(x.get("evidence")) == S1]
check("文档引的那句原文一字不差（50 字）", bool(h1),
      f"实测长度 {[len(norm(x['evidence'])) for x in h1]}")
if h1:
    print(f"        {h1[0].get('pub_day')}  {h1[0].get('article_title')}")
    print(f"        {h1[0].get('url')}")
    sites = {("cac" if "cac.gov.cn" in (x.get("url") or "")
              else "zjwx" if "zjwx.gov.cn" in (x.get("url") or "") else "?")
             for x in i1}
    check("   两个站都有（文档说 cac 与 zjwx 都转载）",
          sites == {"cac", "zjwx"}, f"实测 {sorted(sites)}")
    yrs = {str(x.get("pub_day"))[:4] for x in i1}
    check("   跨 2024/2025/2026 三年（日期怎么筛都点得到）",
          yrs == {"2024", "2025", "2026"}, f"实测 {sorted(yrs)}")

print("\n③ 备选② 国家网信办 ↔ 网络数据安全风险评估办法")
C, D = "国家网信办", "网络数据安全风险评估办法"
i2 = get(f"/api/graph/evidence?src={Q(C)}&dst={Q(D)}&limit=50")["items"]
check("证据 3 条", len(i2) == 3, f"实测 {len(i2)}")
check("全部是 issue（与①的 co_issue 不同类，演示才能讲出两种口径）",
      all(x.get("rel_type") == "issue" for x in i2),
      f"实测 {sorted({x.get('rel_type') for x in i2})}")
check("全部两端点名都在句里",
      all(C in (x.get("evidence") or "") and D in (x.get("evidence") or "")
          for x in i2))
# ⚠ 这句里「（以下简称《办法》）」漏一个字就对不上 ——
#   第一版文档就漏了，逐字核对时才发现。
S2 = ("6月18日，国家网信办、工业和信息化部、公安部联合公布"
      "《网络数据安全风险评估办法》（以下简称《办法》），"
      "自2026年8月20日起施行。")
h2 = [x for x in i2 if norm(x.get("evidence")) == S2]
check("文档引的那句原文一字不差（67 字，含「（以下简称《办法》）」）",
      bool(h2), f"实测长度 {[len(norm(x['evidence'])) for x in i2]}")
if h2:
    print(f"        {h2[0].get('pub_day')}  {h2[0].get('article_title')}")
    print(f"        {h2[0].get('url')}")

print("\n④ 兜底边 中央网信办 ↔ 工业和信息化部（文档写 64 条）")
i3 = get(f"/api/graph/evidence?src={Q('中央网信办')}"
         f"&dst={Q('工业和信息化部')}&limit=200")["items"]
check("证据 64 条", len(i3) == 64, f"实测 {len(i3)}")

print("\n⑤ 兜底话术引的「七部门」长句")
uri = ("file:///" + os.path.join(ROOT, "data", "cacx.db").replace("\\", "/")
       + "?mode=ro")
con = sqlite3.connect(uri, uri=True)
rows = con.execute(
    "SELECT src_name, dst_name, evidence FROM relations "
    "WHERE evidence LIKE '%七部门联合印发%数字化绿色化%'").fetchall()
check("该句确实在库里（文档写 111 条关系共用）", len(rows) == 111,
      f"实测 {len(rows)}")
if rows:
    ev = norm(rows[0][2])
    for nm in ("中央网信办", "国家发展改革委", "工业和信息化部", "生态环境部",
               "住房城乡建设部", "农业农村部", "商务部"):
        check(f"   句中含「{nm}」", nm in ev)
    ends = {x for r in rows for x in (r[0], r[1])}
    check("   端点共 12 个（文档写的数）", len(ends) == 12,
          f"实测 {len(ends)}")
    dirty = [x for x in ends if "联合" in x or x.startswith("会同")]
    check("   没有含连接词的碎片端点", not dirty, f"实测 {dirty}")
con.close()

print("\n⑥ 04 里其余接口契约")
rev = get(f"/api/graph/evidence?src={Q(B)}&dst={Q(A)}&limit=50")["items"]
check("正反向都查（反向也能拿到 24 条）", len(rev) == 24, f"实测 {len(rev)}")
w = {x.get("weight") for x in i1}
check("weight ≥ 2（是命中强度，不是 0~1 的概率）", all(x >= 2 for x in w),
      f"实测 {sorted(w)}")

# ⚠ con 在上面已经 close() 了，这三条断言要自己开只读连接，
# 否则会撞 "Cannot operate on a closed database"。
_w = sqlite3.connect("file:" + urllib.request.pathname2url(
    os.path.join(ROOT, "data", "cacx.db")) + "?mode=ro", uri=True)
_w.row_factory = sqlite3.Row

# ⚠ 2026-10-13 修正：原文把 weight 写成「共现篇数」，是错的。
# 真实语义 = 该文章内这条关系被规则命中的累计强度（每次 +2.0 / +3.0 累加）。
# 这里用**反证**把它钉死：一条只有 1 篇文章支撑的边，weight 却远大于 1，
# 所以它不可能是「跨几篇文章共现」。
single = _w.execute("""
    SELECT src_name, dst_name, rel_type, article_id, weight FROM relations r
    WHERE NOT EXISTS (SELECT 1 FROM relations r2
                      WHERE r2.src_name=r.src_name AND r2.dst_name=r.dst_name
                        AND r2.rel_type=r.rel_type AND r2.article_id<>r.article_id)
    ORDER BY weight DESC LIMIT 1""").fetchone()
check("weight 不是「共现篇数」（单篇边也能 >3）",
      single is not None and single["weight"] > 3,
      f"{single['src_name'][:14]}↔{single['dst_name'][:14]} "
      f"仅 1 篇文章却 weight={single['weight']}" if single else "没找到单篇边")

# 权重应是 2.0/3.0 的整数倍（每次命中的固定增量）
frac = _w.execute("""
    SELECT COUNT(*) FROM relations
    WHERE ABS(weight - ROUND(weight/2.0)*2.0) > 1e-6
       AND ABS(weight - ROUND(weight/3.0)*3.0) > 1e-6""").fetchone()[0]
check("weight 是 2.0/3.0 的整数倍（命中次数 × 固定增量）", frac == 0,
      f"不满足的行数 {frac}")
_w.close()

print("\n" + "=" * 72)
if FAILS:
    print(f"❌ {len(FAILS)} 项不符：")
    for f in FAILS:
        print(f"   - {f}")
else:
    print("✅ 全部通过：04 文档里的演示断言与运行中的服务一致")
print("=" * 72)
sys.exit(0 if not FAILS else 1)