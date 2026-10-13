# -*- coding: utf-8 -*-
"""反向验证：拆掉画图确定性的两处修复，测试必须变红

和 `_mutcheck_group_field.py` 同一套路：测试全绿说明不了什么，
拆掉修复还全绿才说明它压根没测到东西。

⚠ 本脚本会**真的改写** cacx/graph_analysis.py 再改回去。
  所以最后必须校验 sha256 一致 —— 否则「测试红了」可能只是因为
  文件被改坏了。还原校验和测试结果**都要**看，只看其中一个都不算通过。
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 本文件在（测试）下
TARGET = ROOT / "cacx" / "graph_analysis.py"
TEST = ROOT / "（测试）" / "tests_graph_trunc.py"

orig = TARGET.read_text(encoding="utf-8")
h0 = hashlib.sha256(orig.encode("utf-8")).hexdigest()

MUTANTS = [
    ("拆掉 core.sort 的第二判据",
     "core.sort(key=lambda n: (-d_of[n], n))",
     "core.sort(key=lambda n: -d_of[n])"),
    ("拆掉 edges 的定序",
     '    edges.sort(key=lambda e: (min(e["from"], e["to"]),\n'
     '                              max(e["from"], e["to"])))\n',
     ""),
]

fails = []
bak_dir = Path(tempfile.mkdtemp())
(bak_dir / "graph_analysis.py").write_text(orig, encoding="utf-8")

try:
    for name, old, new in MUTANTS:
        if old not in orig:
            print(f"🔴 {name}：待拆的代码找不到，变异本身失效（不能算通过）")
            fails.append(name)
            continue
        TARGET.write_text(orig.replace(old, new, 1), encoding="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", str(TEST)],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = [l for l in p.stderr.splitlines() if l.startswith(("FAILED", "OK"))]
        if p.returncode != 0:
            print(f"✅ {name} → 红了（rc={p.returncode}, {tail[-1] if tail else '?'}）")
        else:
            print(f"🔴 {name} → 仍然全绿，这条测试测不到它")
            fails.append(name)
finally:
    shutil.copy(str(bak_dir / "graph_analysis.py"), str(TARGET))

h1 = hashlib.sha256(TARGET.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
same = h0 == h1
print(f"\n还原校验 sha256 {'一致' if same else '🔴不一致！'}  {h1[:16]}")
if not same:
    fails.append("sha256 还原不一致")

p = subprocess.run([sys.executable, "-X", "utf8", str(TEST)],
                   cwd=str(ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
print(f"还原后重跑：rc={p.returncode}（应为 0）")
if p.returncode != 0:
    fails.append("还原后测试不通过")

shutil.rmtree(bak_dir, ignore_errors=True)
sys.exit(1 if fails else 0)
