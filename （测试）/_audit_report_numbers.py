# -*- coding: utf-8 -*-
"""只读核对报告关键数字，找出内部不一致。"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

con = sqlite3.connect("file:data/cacx.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()

HAS_BODY = "content_text IS NOT NULL AND TRIM(content_text)<>''"

def q(sql, *a):
    return c.execute(sql, a).fetchall()

def one(sql, *a):
    r = c.execute(sql, a).fetchone()
    return r[0] if r else None

print("== 分析窗：有正文 且 year>=2024 ==")
tot = one(f"SELECT COUNT(*) FROM articles WHERE {HAS_BODY} AND year>='2024'")
print("  total =", tot)

print("\n== 年度分布 ==")
s = 0
for r in q(f"SELECT year, COUNT(*) n FROM articles WHERE {HAS_BODY} AND year>='2024' GROUP BY year ORDER BY year"):
    print(f"    {r['year']}  {r['n']}")
    s += r["n"]
print("    合计 =", s, " 与 total 差 =", s - tot)

print("\n== 分站 ==")
s2 = 0
for r in q(f"SELECT site, site_name, COUNT(*) n FROM articles WHERE {HAS_BODY} AND year>='2024' GROUP BY site ORDER BY n DESC"):
    print(f"    {r['site']:<12} {r['site_name'] or '':<16} {r['n']}")
    s2 += r["n"]
print("    合计 =", s2, " 与 total 差 =", s2 - tot)

print("\n== 年份字段缺失但有正文的（可能是差额来源）==")
print("  year IS NULL/'' =", one(f"SELECT COUNT(*) FROM articles WHERE {HAS_BODY} AND (year IS NULL OR year='')"))
print("  year<2024 但正文在 =", one(f"SELECT COUNT(*) FROM articles WHERE {HAS_BODY} AND (year IS NULL OR year='' OR year<'2024')"))

print("\n== 关系总表（去重 vs 不去重）==")
print("  全部关系行数 =", one("SELECT COUNT(*) FROM relations"))
print("  分析窗内 =", one(f"""SELECT COUNT(*) FROM relations r JOIN articles a ON a.id=r.article_id
                             WHERE a.{HAS_BODY} AND a.year>='2024'"""))
for r in q(f"""SELECT r.rel_type, COUNT(*) n FROM relations r JOIN articles a ON a.id=r.article_id
                WHERE a.{HAS_BODY} AND a.year>='2024' GROUP BY r.rel_type ORDER BY n DESC"""):
    print(f"    {r['rel_type']:<14} {r['n']}")

print("\n== 含机构间关系的文章数 ==")
n = one(f"""SELECT COUNT(DISTINCT r.article_id) FROM relations r JOIN articles a ON a.id=r.article_id
            WHERE a.{HAS_BODY} AND a.year>='2024'""")
print(f"  {n} / {tot} = {n/tot*100:.1f}%")

con.close()
