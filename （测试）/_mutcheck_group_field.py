# -*- coding: utf-8 -*-
"""反向验证：把"节点必须带 group"这条守卫拆掉，测试必须变红

跑法：py -3 （测试）/_mutcheck_group_field.py
原理：在**子进程**里改源码再去跑测试 —— 同进程改磁盘文件无效，
因为类已经被 import、代码对象已在内存里。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = Path(__file__).resolve().parent / "tests_graph_nodetype.py"
SRC = ROOT / "cacx" / "graph_analysis.py"

ORIG = SRC.read_text(encoding="utf-8")

# 拆掉守卫：让 to_pyvis 退回"不带 group"的老行为（本次 bug 的原始形态）
CUT = '''        grp = (node_types or {}).get(n) or "org"
        if grp not in ENTITY_TYPES:
            grp = "org"
'''
BROKEN = '''        grp = None          # 反向验证：故意不发 group
'''

RUN = [sys.executable, str(TEST)]
fails = []


def run(tag):
    r = subprocess.run(RUN, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    tail = [x for x in (r.stdout or "").splitlines() if "FAIL" in x or "Ran " in x
            or x.startswith("OK") or x.startswith("FAILED")]
    print(f"  [{tag}] rc={r.returncode}")
    for x in tail[-6:]:
        print("      " + x.strip())
    return r.returncode


def restore():
    SRC.write_text(ORIG, encoding="utf-8")


try:
    print("=== 1. 原样：必须全绿")
    rc = run("原样")
    if rc != 0:
        fails.append("原样应全绿，实际 rc=%s" % rc)

    print("\n=== 2. 拆掉 group 字段 → 必须红")
    assert CUT in ORIG, "锚点失配：守卫代码已变，先核对再改"
    SRC.write_text(ORIG.replace(CUT, BROKEN), encoding="utf-8")
    rc = run("拆守卫")
    if rc == 0:
        fails.append("拆掉 group 后测试仍然通过 → 测试没有真正守住这条")
    else:
        # 打印具体失败的用例名 —— "rc=1"只说明红了，
        # 看不到是哪条在红，下次就不会知道这个守卫到底有没有被真正触发。
        # ⚠ unittest 的失败明细写的是 **stderr**，不是 stdout。
        #   只读 stdout 会打印不出任何用例名，看着像"红了但不知道红在哪"。
        err = subprocess.run(RUN, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", cwd=str(ROOT))
        blob = (err.stderr or "") + "\n" + (err.stdout or "")
        for x in blob.splitlines():
            if x.startswith("FAIL:") or "AssertionError" in x:
                print("      " + x.strip()[:140])

    print("\n=== 3. 还原后：必须重新全绿")
    restore()
    rc = run("还原")
    if rc != 0:
        fails.append("还原后仍失败 → 源码没还原干净")

finally:
    restore()

same = SRC.read_text(encoding="utf-8") == ORIG
print("\n源码逐字节还原:", "是" if same else "否!!")
if not same:
    fails.append("源码未逐字节还原")

print("=" * 60)
print("通过" if not fails else "失败：" + "；".join(fails))
sys.exit(1 if fails else 0)