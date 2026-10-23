# -*- coding: utf-8 -*-
"""复现 advisory() 实际喂给模型的那段 JSON，看模型看到什么。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from cacx import db as DB
from cacx.config import load

d = DB.open_db(load())
runs = d.recent_sync_runs(5)

print("== recent_sync_runs(5) 的原样输出 ==")
print(json.dumps(runs, ensure_ascii=False, indent=2))

print("\n== 里面出现的 site ==")
for r in runs:
    print("   ", r["site"], r["started_at"], "listed=", r["listed"],
          "new=", r["new_articles"], "failed=", r["failed"], "note=", r["note"])

print("\n== 结论 ==")
sites = {r["site"] for r in runs}
if "zjwx-api" in sites:
    print("   ⚠ 快照里含功能开发残余接口 zjwx-api，"
          "模型会把它当成第三个站点解读")
else:
    print("   ✅ 快照里没有残余接口")
d.close()
