# -*- coding: utf-8 -*-
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect("file:data/cacx.db?mode=ro", uri=True)
for t in ("articles", "relations", "entities"):
    cols = [(r[1], r[2]) for r in con.execute(f"PRAGMA table_info({t})")]
    print(f"--- {t}")
    for n, ty in cols:
        print(f"    {n:<22} {ty}")
con.close()
