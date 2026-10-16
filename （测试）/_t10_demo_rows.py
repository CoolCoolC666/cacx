# -*- coding: utf-8 -*-
"""为演示脚本定稿：边列表行号 + 画布口径 + 两条候选边的证据首条。"""
import json
import urllib.parse
import urllib.request

B = "http://127.0.0.1:8770"


def get(p):
    with urllib.request.urlopen(B + p, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


g = get("/api/graph")
es = g["edges"]
print(f"边列表默认返回 {len(es)} 条，按 n 降序，前 5 个 n={[e['n'] for e in es[:5]]}")

WANT = [("中央网信办", "山西省委网信办"), ("公安部", "个人信息保护法"),
        ("中央网信办", "农业农村部"), ("公安部", "个人信息"),
        ("中央网信办", "开展2025年个人信息保护系列专项行动")]
print("\n行号（1 起，按界面顺序）：")
for s, d in WANT:
    row = next((i + 1 for i, e in enumerate(es)
                if {e["src_name"], e["dst_name"]} == {s, d}), None)
    rt = next((e["rel_type"] for e in es if {e["src_name"], e["dst_name"]} == {s, d}), None)
    nn = next((e["n"] for e in es if {e["src_name"], e["dst_name"]} == {s, d}), None)
    print(f"  {'第 %d 行' % row if row else '❌ 不在列表':<10} {s} → {d}  {rt}  n={nn}")

print("\n画布口径（date_from=2024-01-01, min_degree=2, 四类全勾）：")
an = get("/api/graph/analysis?min_degree=2&node_types=org,policy,problem,measure"
         "&date_from=2024-01-01")
gr = an["graph"]
print(f"  全图 {an['nodes']} 节点 / {an['edges']} 边 / {an['components']} 连通块"
      f"；最大连通块 {an.get('largest_component')}")
print(f"  画布实画 {len(gr['nodes'])} 节点 / {len(gr['edges'])} 边"
      f"（折叠 {an['nodes'] - len(gr['nodes'])} 个低度数节点）")
for n in ("中央网信办", "山西省委网信办", "公安部", "个人信息保护法"):
    nd = next((x for x in gr["nodes"] if x["id"] == n), None)
    print(f"  画布上「{n}」: {'在，度 ' + str(nd.get('degree')) if nd else '不在画布（被折叠）'}")

print("\n两条演示边的证据首条（证据接口无日期参数 = 全库口径）：")
for s, d in (("中央网信办", "山西省委网信办"), ("公安部", "个人信息保护法")):
    r = get("/api/graph/evidence?src=" + urllib.parse.quote(s)
            + "&dst=" + urllib.parse.quote(d))
    its = r.get("items", [])
    print(f"  {s} ↔ {d}: 全库 {len(its)} 条")
    for it in its[:2]:
        print(f"     [{it.get('pub_day')}] {it.get('title')}")
        print(f"        {(it.get('evidence') or '')[:110]}")
