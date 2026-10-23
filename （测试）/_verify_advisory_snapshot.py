# -*- coding: utf-8 -*-
"""验证 advisory() 改后喂给模型的快照：残余接口是否已剔除、字段是否已中文化。

不调 AI（省钱且不外发），只复现快照构造。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from cacx import db as DB
from cacx.config import load
from cacx import sites as S

cfg = load()
d = DB.open_db(cfg)
f = d.facets()

allsrc = S.all_sites(cfg) or {}
enabled_codes = [c for c, s in allsrc.items()
                 if (s or {}).get("enabled", True)]
in_db = {s.get("site") for s in f.get("sites", []) if s.get("site")}
want = [c for c in enabled_codes if not in_db or c in in_db] or enabled_codes

print("== 启用中站点 ==", enabled_codes)
print("== 库里有文章的站点 ==", sorted(in_db))
print("== 送给 recent_sync_runs 的过滤码 ==", want)

raw_runs = d.recent_sync_runs(5, sites=want)
print("\n== 过滤后的批次 ==")
for r in raw_runs:
    print("   ", r["site"], r["started_at"], "listed=", r["listed"],
          "new=", r["new_articles"], "failed=", r["failed"])

site_names = {c: (allsrc.get(c) or {}).get("name") or c
              for c in enabled_codes}
batches = []
for r in raw_runs:
    code = r.get("site") or ""
    batches.append({
        "站点": site_names.get(code, code),
        "站点代码": code,
        "接口模式": "增量同步" if r.get("mode") == "incremental" else "全量重抓",
        "开始时间": r.get("started_at"),
        "结束时间": r.get("finished_at"),
        "列表接口扫到条数": r.get("listed"),
        "本次新增条数": r.get("new_articles"),
        "抓取失败条数": r.get("failed"),
    })

print("\n== 改后模型看到的「最近同步批次」==")
print(json.dumps(batches, ensure_ascii=False, indent=2))

blob = json.dumps(batches, ensure_ascii=False)
print("\n== 校验 ==")
print("   含 zjwx-api :", "zjwx-api" in blob)
print("   含裸英文 listed/failed :",
      ('"listed"' in blob) or ('"failed"' in blob))
print("   批次数 :", len(batches))

d.close()
