# -*- coding: utf-8 -*-
r"""同步剩余文档里的 Python 版本口径。

为什么用脚本文件而不是内联 python -c：
  内联命令行里的引号会被 PowerShell 吃掉，`**` 和中文引号混在一起
  还会触发 SyntaxWarning / SyntaxError。这条已经踩过，不重复。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EDITS = [
    ("数据包/字段说明.md",
     "需要 Python 3.10+。原始库有",
     "需要 Python 3.8+（推荐 3.12；仅可选的 MCP 功能需要 3.10+）。原始库有"),
    ("任务指引/README-任务拆分总方案.md",
     '2. 装 Python 3.10+（安装时**务必勾选 "Add Python to PATH"**）',
     '2. 装 Python 3.8+（推荐 3.12 / 3.13；安装时**务必勾选 '
     '"Add Python to PATH"**。只有可选的 MCP 功能需要 3.10+，'
     '不装不影响其他任何功能）'),
]


def main():
    rc = 0
    for rel, old, new in EDITS:
        p = ROOT / rel
        t = p.read_text(encoding="utf-8")
        if new in t:
            print(f"  SKIP  {rel}  （已经是新口径）")
            continue
        if old not in t:
            print(f"  MISS  {rel}  锚点没找到，需人工核对")
            rc = 1
            continue
        p.write_text(t.replace(old, new), encoding="utf-8")
        print(f"  OK    {rel}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
