# -*- coding: utf-8 -*-
r"""反向验证：证明 tests_mcp / tests_batch_summarize 修好后是真的"守卫生效→红"，
不是碰巧绿。

**背景**：给 articles 加了「同站同正文指纹」去重守卫之后，这两个文件突然红了 ——
因为它们是全仓库仅有的、用 `upsert_article()` 循环造**多篇正文逐字相同**的测试。
守卫把第 2 篇起全拒了，`get_by_url()` 回 None，一路传到下游炸成
`AttributeError: 'NoneType' object has no attribute 'get'`。
修法是让样本正文每篇唯一（守卫是对的，生产语义上那确实是同一篇），
并当场 assert 入库结果，别把 False 带进下游。

这个脚本负责证明：那处修法不是"碰巧变绿"。
**守卫生效 → 必须红；还原 → 必须绿**。

做法（子进程，必须子进程 —— 同进程改磁盘文件对已 import 的模块无效）：
  1. 备份两个测试文件
  2. 去掉正文里的「每篇唯一后缀」，让样本退回逐字相同 → 守卫重新生效
  3. 记录 rc，以及报错里有没有人可读的「入库」提示
  4. 还原 → 再跑一次，必须 rc=0
  5. 断言 3 的 rc≠0、还原后 rc=0、文件逐字节还原

运行：py -3 （测试）\_mutcheck_batch_dedup.py
      py -3 （测试）\_mutcheck_batch_dedup.py --inject   （手动注入，不自动还原）
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "（测试）" / "tests_mcp.py",
           ROOT / "（测试）" / "tests_batch_summarize.py"]
# 正文唯一后缀（正则自动定位，不手抄 —— 手抄带转义的锚点已失败过多次）
SUFFIXES = [' + f"第{i}篇独有段落"', ', f"第{i}篇独有段落"']


def run(p):
    return subprocess.run([sys.executable, "-X", "utf8", str(p)],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def strip_suffix(text):
    hits = 0
    for s in SUFFIXES:
        if s in text:
            text = text.replace(s, "")
            hits += 1
    return text, hits


def main():
    if "--inject" in sys.argv:
        for t in TARGETS:
            txt = t.read_text(encoding="utf-8")
            txt2, h = strip_suffix(txt)
            t.write_text(txt2, encoding="utf-8")
            print(f"inject {t.name}: removed {h} suffix(es)")
        return 0

    backups = {}
    total_hits = 0
    for t in TARGETS:
        orig = t.read_text(encoding="utf-8")
        backups[t] = orig
        txt, h = strip_suffix(orig)
        total_hits += h
        t.write_text(txt, encoding="utf-8")

    rc_mut, msg_mut, restored_ok = {}, {}, True
    try:
        for t in TARGETS:
            r = run(t)
            rc_mut[t.name] = r.returncode
            msg_mut[t.name] = (r.stdout or "") + (r.stderr or "")
    finally:
        for t, orig in backups.items():
            t.write_text(orig, encoding="utf-8")

    for t, orig in backups.items():
        if t.read_text(encoding="utf-8") != orig:
            restored_ok = False

    print("=" * 62)
    print(f"注入：去掉 {total_hits} 处正文唯一后缀（守卫重新生效）")
    for t in TARGETS:
        m = msg_mut[t.name]
        print(f"  {t.name}: rc={rc_mut[t.name]}")
        print(f"    有人可读的『入库』提示: {'是' if '入库' in m else '否'}")
        print(f"    退回 NoneType 崩溃:     {'是' if 'NoneType' in m else '否'}")
    print(f"  文件已逐字节还原: {'是' if restored_ok else '否'}")

    print("\n=== 还原后再跑（必须全绿）===")
    rc_ok = True
    for t in TARGETS:
        r = run(t)
        print(f"  {t.name}: rc={r.returncode}")
        if r.returncode != 0:
            rc_ok = False

    bad = []
    if total_hits < 2:
        bad.append(f"只注入了 {total_hits} 处，变异没作用到被测代码")
    for t in TARGETS:
        if rc_mut[t.name] == 0:
            bad.append(f"{t.name} 变异后仍全绿 —— 测试对守卫不敏感")
    if not rc_ok:
        bad.append("还原后未全绿 —— 改动本身有问题")
    if not restored_ok:
        bad.append("测试文件没还原干净")

    print("\n" + "=" * 62)
    if bad:
        print("反向验证失败：")
        for b in bad:
            print("  -", b)
        return 1
    print("反向验证通过：守卫生效时红、还原时绿，且改动可逆")
    return 0


if __name__ == "__main__":
    sys.exit(main())