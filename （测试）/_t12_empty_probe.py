# -*- coding: utf-8 -*-
"""找出会让 /api/graph 返回**空 edges** 的那组参数。

已知：用户在浏览器里刷新后看到的是新文案「当前筛选条件下没有关系」，
说明请求**成功**了、但 edges 是空数组（真空，不是失败）。
而我早前用 `date_from=2024-01&limit=300` 实测拿到 300 条。
差异只能来自 gpQuery() 拼出来的别的参数，这里逐个组合试。

参数来源（index.html gpQuery()）：
  min_degree=2            最小度数默认 2
  date_from=2024-01       日期起默认 2024-01（month 格式！）
  rel_types=...           关系类型勾选（不勾=不传）
  site=...                站点勾选（不勾=不传）
  node_types=...          图例勾选
  limit=300 / topn=12     固定
"""
import json
import urllib.error
import urllib.parse
import urllib.request

B = "http://127.0.0.1:8770"
REL = "joint_issue,issue,target,adopt"
TYPES = "org,policy,measure,problem"

VARIANTS = [
    ("真实前端形状（不传 site）",
     {"min_degree": "2", "date_from": "2024-01", "rel_types": REL,
      "node_types": TYPES, "limit": "300", "topn": "12"}),
    ("+site=cac,zjwx（站点复选框全勾的默认态）",
     {"min_degree": "2", "date_from": "2024-01", "rel_types": REL,
      "node_types": TYPES, "limit": "300", "topn": "12", "site": "cac,zjwx"}),
    ("+site=cac（只勾中国网信办）",
     {"min_degree": "2", "date_from": "2024-01", "rel_types": REL,
      "node_types": TYPES, "limit": "300", "topn": "12", "site": "cac"}),
    ("+site=zjwx（只勾浙江）",
     {"min_degree": "2", "date_from": "2024-01", "rel_types": REL,
      "node_types": TYPES, "limit": "300", "topn": "12", "site": "zjwx"}),
    ("+date_to=2099-12（带上限）",
     {"min_degree": "2", "date_from": "2024-01", "date_to": "2099-12",
      "rel_types": REL, "node_types": TYPES, "limit": "300", "topn": "12"}),
    ("site 全勾 + date_to（最接近用户态）",
     {"min_degree": "2", "date_from": "2024-01", "date_to": "2099-12",
      "rel_types": REL, "node_types": TYPES, "limit": "300", "topn": "12",
      "site": "cac,zjwx"}),
]

for name, params in VARIANTS:
    qs = urllib.parse.urlencode(params)
    url = f"{B}/api/graph?{qs}"
    try:
        r = urllib.request.urlopen(url, timeout=300)
        d = json.loads(r.read().decode("utf-8"))
        n_e = len(d.get("edges") or [])
        n_n = len(d.get("nodes") or [])
        flag = "  <<< 空！" if n_e == 0 else ""
        print(f"[{r.status}] edges={n_e:<5} nodes={n_n:<6} {name}{flag}")
    except urllib.error.HTTPError as e:
        print(f"[{e.code}] {name}: {e.read().decode('utf-8','replace')[:120]}")
    except Exception as e:
        print(f"[ERR] {name}: {type(e).__name__} {e}")
