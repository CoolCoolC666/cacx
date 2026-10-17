# -*- coding: utf-8 -*-
"""读真库，只读，拿几个「文档里会引用」的口径的当前值。

⚠ 为什么写脚本而不是 py -3 -c：内联 Python 里嵌套引号会被 PowerShell 吃掉
   （本轮已经见过 `count(distinct ...)` 被解析成 cmdlet）。复杂查询一律落文件。

⚠ 只读连接：绝不 `sqlite3.connect("data/cacx.db")` —— 那会创建 WAL/SHM
   边车文件、改变主库 mtime，把「本次验收没有写库」的证据链毁掉。
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "cacx.db"

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
q = lambda s, *a: c.execute(s, a).fetchone()[0]

print(f"库：{DB.name}")
print(f"  articles         = {q('select count(*) from articles')}")
print(f"  2024+ 发文        = {q('select count(*) from articles where pub_date >= ?', '2024-01-01')}")
print(f"  ai_annotations   = {q('select count(*) from ai_annotations')}")
print(f"  2024+ 有摘要      = "
      f"{q('select count(distinct a.article_id) from ai_annotations a join articles ar on ar.id=a.article_id where ar.pub_date >= ?', '2024-01-01')}")
print(f"  entities         = {q('select count(*) from entities')}")
print(f"  relations        = {q('select count(*) from relations')}")
# 这几个是查「数字为什么在漂」的：摘要数会不会自己往上跑，
# 决定了文档里那个数是历史数字（钉死）还是活口径（得现算）。
print(f"  摘要最近写入      = {q('select max(created_at) from ai_annotations')}")
print(f"  今天新增摘要      = {q('select count(*) from ai_annotations where created_at >= ?', '2026-10-16')}")
print(f"  最近同步          = {q('select max(synced_at) from articles')}")
c.close()
