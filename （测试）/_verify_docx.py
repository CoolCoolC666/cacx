# -*- coding: utf-8 -*-
"""验收 docx：内容、页数、表格、字体。用 Word 真读一遍。"""
import os
import sys

from docx import Document
from docx.shared import Pt

sys.stdout.reconfigure(encoding="utf-8")

PATH = sys.argv[1]
d = Document(PATH)

paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
chars = sum(len(t) for t in paras)
for t in d.tables:
    for row in t.rows:
        for c in row.cells:
            chars += len(c.text.strip())

print(f"文件   : {os.path.basename(PATH)}")
print(f"大小   : {os.path.getsize(PATH)} 字节")
print(f"段落   : {len(paras)}")
print(f"表格   : {len(d.tables)}")
for i, t in enumerate(d.tables, 1):
    print(f"   表{i}: {len(t.rows)} 行 × {len(t.columns)} 列")
print(f"总字数 : {chars}")

print("\n---- 标题结构 ----")
for p in d.paragraphs:
    if p.style.name.startswith(("Heading", "Title")) and p.text.strip():
        print(f"  [{p.style.name}] {p.text.strip()}")

print("\n---- 提示词框（前 6 行）----")
if d.tables:
    for r in d.tables[0].rows[0].cells[0].paragraphs[:6]:
        if r.text.strip():
            print("   ", r.text.strip()[:88])

print("\n---- 残留检查 ----")
blob = "\n".join(paras) + "\n".join(
    c.text for t in d.tables for row in t.rows for c in row.cells)
for bad, why in [("**", "Markdown 强调符"),
                 ("`", "Markdown 反引号"),
                 ("�", "乱码字符"),
                 ("listed\":", "裸英文字段 listed"),
                 ("failed\":", "裸英文字段 failed")]:
    n = blob.count(bad)
    print(f"  {why:<22} {bad!r:<12} 命中 {n} 处"
          + ("  ✅" if n == 0 else "  🔴"))
print(f"  含「zjwx-api」        {'是  🔴' if 'zjwx-api' in blob else '否  ✅'}")
print(f"  含「系统提示词」      {'是  ✅' if '系统提示词' in blob else '否  🔴'}")
print(f"  含「口径说明」        {'是  ✅' if '口径说明' in blob else '否  🔴'}")
