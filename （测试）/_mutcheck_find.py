# -*- coding: utf-8 -*-
"""反向验证：把高亮的三处守卫生意弄坏，tests_graph_find 必须变红。

不这么做就不知道测试是不是真的在守 —— 全绿的测试可能只是
一个恒为真的断言。这里对**运行测试的那个进程所读的源码**做变异：

  M1  后端：边命中改成「只亮一端」（e.get("hit")）
  M2  后端：pyvis 导出忽略 hit（颜色写死）
  M3  前端：把 find 并进 keyword（等于退化成筛选）

每个变异：改 → 跑测试 → 必须 FAIL → 还原 → 校验 sha256 与原始一致。
"""
import hashlib
import subprocess
import sys
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "（测试）" / "tests_graph_find.py"
APP = ROOT / "cacx" / "web" / "app.py"
HTML = ROOT / "cacx" / "web" / "static" / "index.html"
GA = ROOT / "cacx" / "graph_analysis.py"

MUTANTS = [
    ("M1 边命中改成只亮一端", GA,
     'e["hit"] = e["from"] in hits and e["to"] in hits',
     'e["hit"] = e["from"] in hits or e["to"] in hits'),
    ("M2 导出忽略 hit", APP,
     'hit = bool(e.get("hit"))',
     'hit = False'),
    ("M3 find 并进 keyword", HTML,
     "if(fd) q.set('find', fd);",
     "if(fd) q.set('keyword', fd);"),
]

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def run():
    r = subprocess.run([sys.executable, "-X", "utf8", str(TEST)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT))
    return r.returncode, (r.stdout or "") + (r.stderr or "")

print("=== 反向验证：每个变异都必须让测试变红 ===")
print("基线（未变异）：")
rc, out = run()
print("  returncode=%d %s" % (rc, "OK" if rc == 0 else "FAIL"))
if rc != 0:
    print(out[-2500:])
    print("基线就不绿，后面的红绿没有意义 —— 先修测试。")
    sys.exit(2)

bad = []
for name, path, old, new in MUTANTS:
    before = sha(path)
    bak = path.with_name(path.name + ".bak")
    shutil.copy2(path, bak)
    try:
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print("  SKIP  %s —— 待替换串不存在（代码已改？）" % name)
            bad.append(name + "（锚点不存在）")
            continue
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
        rc, out = run()
        red = rc != 0
        print("  %s  %s" % ("RED " if red else "!!! ", name))
        if not red:
            bad.append(name)
            print("        测试仍然全绿 —— 这个守卫生意没有测试覆盖")
    finally:
        # ⚠⚠ 必须**还原**，不能只删备份。
        #   上一版这里写的是 unlink(bak) —— 备份被删掉、变异留在原地，
        #   源文件从此一直是坏的（实测 app.py 的边命中被改成 e.get("hit")、
        #   index.html 的 find 被并进 keyword，直到人工发现才修回来）。
        #   而且下一个变异会因此找不到锚点、被误报成"代码已改"。
        shutil.copy2(bak, path)
        bak.unlink()
    after = sha(path)
    if after != before:
        print("        !! 还原后 sha256 不一致：", after)
        bad.append(name + "（未还原干净）")

print()
if bad:
    print("未达成：%d 项" % len(bad))
    for b in bad:
        print("  - " + b)
    sys.exit(1)
print("反向验证通过：3 个变异全部变红，且文件已还原。")