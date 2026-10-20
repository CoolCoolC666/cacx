# -*- coding: utf-8 -*-
"""精确匹配口径复核中央网信办关系数。"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect("file:data/cacx.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()

WIN = ("JOIN articles a ON a.id=r.article_id "
       "WHERE a.content_text IS NOT NULL AND TRIM(a.content_text)<>'' AND a.year>='2024'")
N = "中央网信办"

print("== 端点名里包含「中央网信办」但不相等的样本（看口径差在哪）==")
for r in c.execute(f"""SELECT DISTINCT src_name s FROM relations r {WIN}
                       AND src_name LIKE '%{N}%' AND src_name<>'{N}' LIMIT 20"""):
    print("    src:", r["s"])
for r in c.execute(f"""SELECT DISTINCT dst_name d FROM relations r {WIN}
                       AND dst_name LIKE '%{N}%' AND dst_name<>'{N}' LIMIT 20"""):
    print("    dst:", r["d"])

print("\n== 精确匹配 src_name = 报告口径 ==")
for r in c.execute(f"""SELECT r.rel_type, COUNT(*) n FROM relations r {WIN}
                       AND r.src_name='{N}' GROUP BY r.rel_type ORDER BY n DESC"""):
    print(f"    {r['rel_type']:<14} {r['n']}")

print("\n== 精确匹配 dst_name ==")
for r in c.execute(f"""SELECT r.rel_type, COUNT(*) n FROM relations r {WIN}
                       AND r.dst_name='{N}' GROUP BY r.rel_type ORDER BY n DESC"""):
    print(f"    {r['rel_type']:<14} {r['n']}")

print("\n== 精确匹配 joint_issue 源端/目标端 ==")
src = c.execute(f"""SELECT COUNT(*) FROM relations r {WIN}
    AND r.rel_type='joint_issue' AND r.src_name='{N}'""").fetchone()[0]
dst = c.execute(f"""SELECT COUNT(*) FROM relations r {WIN}
    AND r.rel_type='joint_issue' AND r.dst_name='{N}'""").fetchone()[0]
print(f"    源端 {src} / 目标端 {dst}  源端占比 {src/(src+dst)*100:.1f}%  (报告 601/87, 87%)")

print("\n== 联合对象 Top（源端=中央网信办, joint_issue）==")
for r in c.execute(f"""SELECT r.dst_name d, COUNT(*) n FROM relations r {WIN}
                       WHERE r.rel_type='joint_issue' AND r.src_name='{N}'
                       GROUP BY d ORDER BY n DESC LIMIT 8"""):
    print(f"    {r['n']:>4}  {r['d']}")

print("\n== 与公安部/市场监管总局（精确）==")
for r in c.execute(f"""SELECT r.rel_type, COUNT(*) n FROM relations r {WIN}
    WHERE (r.src_name='{N}' AND (r.dst_name LIKE '%公安部%' OR r.dst_name LIKE '%市场监管总局%'))
       OR (r.dst_name='{N}' AND (r.src_name LIKE '%公安部%' OR r.src_name LIKE '%市场监管总局%'))
    GROUP BY r.rel_type"""):
    print(f"    {r['rel_type']:<14} {r['n']}")
con.close()
