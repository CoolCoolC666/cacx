# -*- coding: utf-8 -*-
"""量报告字数：分别按「中文字符」「全部非空白字符」两种口径，供人工判读 1500–2500。"""
import re
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/报告-网信办角色与权责边界.md")
t = p.read_text(encoding="utf-8")
cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
nows = len(re.sub(r"\s", "", t))
words = len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", t))
print(f"{p.name}")
print(f"  中文字符        {cjk}")
print(f"  中文+英文数字词 {words}")
print(f"  全部非空白字符  {nows}")
