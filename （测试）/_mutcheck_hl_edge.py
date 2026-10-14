# -*- coding: utf-8 -*-
"""补充反向验证：把 hl 的**边判定**退化成 find 语义（两端都命中），
看 tests_graph_hl.py 抓不抓得住。

为什么要单独一条（而不是并进 _mutcheck_hl.py）：
那一条的两个变异只拆了「节点命中判定」与「导出件认不认 hl」，
**没碰边语义**。而边语义恰恰是 R4 文档 §3.2 点名的那个坑 ——
点单个节点时若按 find 的「两端都命中」来判，邻居都不叫这个名字，
于是边命中数 = 0，图上只有一个黄点、四周全暗，等于点了没反应。

判据：变异必须打在**子进程真正会去读的源码**上，
还原后校验 sha256，不能只 unlink 备份了事。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "cacx" / "graph_analysis.py"
BAK = ROOT / "（测试）" / "_mut_hl_edge.bak"

# 原判定：与中心相连的边（正确语义）
OLD = '                if hl in (e["from"], e["to"]):'
# 退化：find 的「两端都命中」—— 单点场景下必然一条边都命中不了
NEW = '                if e["from"] in (hl,) and e["to"] in (hl,):'

if OLD not in TARGET.read_text(encoding="utf-8"):
    print("✗ 没找到变异点，源码结构与预期不符，先人工核对再改")
    raise SystemExit(2)

BAK.write_bytes(TARGET.read_bytes())
before = hashlib.sha256(TARGET.read_bytes()).hexdigest()
print(f"变异前 sha256: {before[:16]}…")

try:
    TARGET.write_text(TARGET.read_text(encoding="utf-8").replace(OLD, NEW, 1),
                      encoding="utf-8")
    print("已注入变异 M3：边判定 退化为「两端都命中」\n")

    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "（测试）" / "tests_graph_hl.py")],
        capture_output=True, text=True, cwd=str(ROOT), encoding="utf-8",
    )
    out = r.stdout + r.stderr
    red = r.returncode != 0
    print("\n".join([ln for ln in out.splitlines() if ln.strip()][-6:]))
    print()
    if red:
        print("✓ 会红  M3 边判定退化为「两端都命中」—— 测试守住了边语义")
    else:
        print("✗✗ 全绿 —— tests_graph_hl.py **没守住 hl 的边语义**！")
        print("        R4 文档 §3.2 点名的坑正好会溜过去，必须补断言。")
finally:
    shutil.copy2(BAK, TARGET)
    after = hashlib.sha256(TARGET.read_bytes()).hexdigest()
    print(f"\n已还原，sha256: {after[:16]}…  与变异前一致: {before == after}")
    BAK.unlink(missing_ok=True)

raise SystemExit(0 if (red and before == after) else 1)
