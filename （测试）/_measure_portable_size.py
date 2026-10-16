# -*- coding: utf-8 -*-
r"""量一下「随包自带运行时」到底要付出多少体积。

只报数字，不做决策。但决策必须有数字支撑：
如果便携运行时方案要 +400 MB，那就得明说，而不是先斩后奏。
"""
import importlib.util
import os
import sys
from pathlib import Path

CORE = ["fastapi", "uvicorn", "requests", "bs4", "pyvis", "jieba",
        "networkx", "certifi", "pip", "setuptools"]


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def main():
    base = Path(sys.prefix)
    print(f"当前解释器: {sys.executable}")
    print(f"site-packages: {base}")
    total = 0
    rows = []
    for name in CORE:
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.submodule_search_locations:
            rows.append((name, None))
            continue
        d = Path(list(spec.submodule_search_locations)[0])
        sz = dir_size(d)
        total += sz
        rows.append((name, sz))
    for name, sz in rows:
        if sz is None:
            print(f"  {name:14s}  未安装")
        else:
            print(f"  {name:14s} {sz/1e6:8.1f} MB")
    print()
    print(f"核心依赖合计（含 pip/setuptools/certifi）: {total/1e6:.1f} MB")
    print(f"若去掉 pip/setuptools（便携包不必自带）: "
          f"{total - sum(s for n, s in rows if n in ('pip', 'setuptools')):.1f} MB")
    print()
    print("参考：embeddable Python 本体 10.6 MB（实测 HEAD 得到）")
    print(f"→ 便携运行时总增项约 "
          f"{(total - sum(s for n, s in rows if n in ('pip', 'setuptools')))/1e6 + 10.6:.0f} MB")


if __name__ == "__main__":
    main()
