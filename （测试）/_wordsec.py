# -*- coding: utf-8 -*-
"""按段落统计中文字符，找出报告的重量分布，用于定点精简。"""
import re
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/报告-网信办角色与权责边界.md")
t = p.read_text(encoding="utf-8")
cjk = lambda s: len(re.findall(r"[\u4e00-\u9fff]", s))
total = cjk(t)
print(f"{p.name}  总中文字符 {total}\n")
for blk in re.split(r"\n(?=#{1,3} )", t):
    head = blk.splitlines()[0][:34]
    n = cjk(blk)
    print(f"{n:5d}  {100*n/max(1,total):4.1f}%  {head}")
