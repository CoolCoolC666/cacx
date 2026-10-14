# -*- coding: utf-8 -*-
"""反向验证：hl 相关测试真的会红吗？

项目铁律（R4 文档 §4.5）：新增测试必须**反向验证** ——
故意拆掉守卫/改坏实现，确认测试**会红**，然后真还原
（shutil.copy2 备份回去 + sha256 校验），不能只 unlink 备份了事。

变异点：
  M1  graph_analysis.py 的 hl 命中判定 `if hl in keep:` → 永远不命中
      → TestHlHighlight 系列应全红（hl_hits 恒 0）
  M2  app.py 的 pyvis_html 导出不再认 hl（只认 hit）
      → TestHlExportParity.test_export_applies_hl_color 应红

任何一个变异没被抓到，说明测试在假绿，必须停下修正测试本身。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GA = ROOT / "cacx" / "graph_analysis.py"
APP = ROOT / "cacx" / "web" / "app.py"
TEST = ROOT / "（测试）" / "tests_graph_hl.py"
BAK = ROOT / "_tmp_mut_hl_bak"

results = []


def run_test():
    r = subprocess.run([sys.executable, str(TEST)], capture_output=True,
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    out = (r.stdout or b"").decode("utf-8", "replace")
    return r.returncode != 0 or "FAILED" in out or "OK" not in out


def mutate(path, old, new):
    src = path.read_text(encoding="utf-8")
    assert old in src, f"变异点没找到：{old!r}"
    path.write_text(src.replace(old, new, 1), encoding="utf-8")


def restore(path, bak):
    shutil.copy2(bak, path)
    assert hashlib.sha256(bak.read_bytes()).hexdigest() == \
        hashlib.sha256(path.read_bytes()).hexdigest(), \
        f"还原后哈希不一致：{path.name}"


# ---- M1：hl 命中判定被拆 ----
bak1 = BAK.with_suffix(".ga")
shutil.copy2(GA, bak1)
mutate(GA, "if hl in keep:",
       "if False and hl in keep:   # 变异 M1")
red1 = run_test()
restore(GA, bak1)
results.append(("M1 hl 恒不命中", red1))

# ---- M2：导出不再认 hl ----
bak2 = BAK.with_suffix(".app")
shutil.copy2(APP, bak2)
mutate(APP, 'if n.get("hit") or n.get("hl"):',
       'if n.get("hit"):   # 变异 M2')
red2 = run_test()
restore(APP, bak2)
results.append(("M2 导出不认 hl", red2))

print("=" * 60)
for name, red in results:
    print(f"  {'✓ 会红' if red else '✗ 假绿！！'}  {name}")
if all(red for _, red in results):
    print("反向验证通过：所有变异都被测试抓住")
else:
    print("✗✗ 有变异没被抓住 —— 测试在假绿，必须修正测试本身")
    sys.exit(1)
