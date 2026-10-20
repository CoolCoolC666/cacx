# -*- coding: utf-8 -*-
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect("file:data/cacx.db?mode=ro", uri=True)
c = con.cursor()
HAS = "content_text IS NOT NULL AND TRIM(content_text)<>''"
print("总文章", c.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
print("有正文", c.execute(f"SELECT COUNT(*) FROM articles WHERE {HAS}").fetchone()[0])
print("总字数", c.execute("SELECT SUM(chars) FROM articles").fetchone()[0])
print("分站", c.execute("SELECT site,COUNT(*) FROM articles GROUP BY site").fetchall())
print("摘要条数", c.execute("SELECT COUNT(*) FROM ai_annotations WHERE kind='summarize'").fetchone()[0])
print("站外链接", c.execute("SELECT COUNT(*) FROM external_links").fetchone()[0])
print("占位", c.execute("SELECT COUNT(*) FROM articles WHERE content_empty=1").fetchone()[0])
con.close()
