# -*- coding: utf-8 -*-
"""全量回归：逐个跑 tests_*.py，汇总判定。
判据三条全为「无」才算过：
  ① ^FAILED            ② Traceback            ③ 「失败 N」里 N≠0
只把 stderr 全丢掉会漏掉「测试自己 print 了异常」的情况，所以全文都收。

⚠ 测试文件在「（测试）」子目录，但**工作目录必须是项目根** ——
  一部分测试要读 config/settings.json、data/cacx.db、cacx/web/static，
  cwd 一变就全找不到。所以：cwd = 根，被测文件用绝对路径。
"""
import glob
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # （测试）目录
ROOT = HERE.parent
os.chdir(ROOT)                                   # cwd 必须是项目根
env = dict(os.environ, PYTHONIOENCODING="utf-8")

sys.path.insert(0, str(HERE))
import _temppool  # noqa: E402

# `--check-leaks-only`：只查临时目录残留，不跑用例。
# 日常体检用（全量要十几分钟）；反向验证也靠它 —— 新闸门必须先证明它会红。
if "--check-leaks-only" in sys.argv:
    left = list(_temppool.ROOT.iterdir()) if _temppool.ROOT.exists() else []
    total = sum(f.stat().st_size for p in left
                for f in p.rglob("*") if f.is_file())
    print(f"临时根 {_temppool.ROOT}")
    if not left:
        print("临时目录：0 残留")
        sys.exit(0)
    print(f"✗ 临时目录泄漏：残留 {len(left)} 项，合计 {total / 1048576:.1f} MB")
    for p in left[:20]:
        print(f"    {p.name}")
    if len(left) > 20:
        print(f"    …… 另有 {len(left) - 20} 项")
    sys.exit(1)

files = sorted(glob.glob(str(HERE / "tests_*.py")))
bad, total_run, skipped = [], 0, []

# ⚠⚠ 一个测试文件都没找到时**必须报错退出**，不能报「全部通过」。
# 真的踩过：把 HERE 写错一层，glob 匹配到 0 个文件，
# 于是「失败文件 0 个」→ 打印「>>> 全部通过」。
# 一个没跑的测试套件，比跑挂的测试套件危险得多 ——
# 跑挂了至少是红的，没跑是**绿的**，会把没验证的东西当成验证过了。
if not files:
    print(f"✗ 没找到任何 tests_*.py（找的是 {HERE}）")
    print("  这不是「通过」，是「一个都没跑」。检查 HERE 指向。")
    sys.exit(2)
if len(files) < 30:
    print(f"✗ 只找到 {len(files)} 个测试文件，明显不对（应约 34 个）")
    sys.exit(2)

# ---------------------------------------------------------- 临时目录泄漏闸门
# 起因：测试反复跑，%TEMP% 攒出 1814 个 cacx_* 目录 / 4.30 GB。
# 现在临时库统一落在 _temppool.ROOT，**这一维度必须进回归判据** ——
# 「用例全绿但临时目录越攒越多」和「用例红了」一样是坏了，只是坏得慢。
#
# 先扫一遍再跑：把上一轮遗留清掉，这样结束时量到的是**本次**的残留，
# 而不是历史累积（否则第一次跑新闸门就会因为旧垃圾直接红）。
print(f"临时根 {_temppool.ROOT}")
print(f"开跑前清扫：清掉 {_temppool.sweep()} 项遗留")

for f in files:
    name = Path(f).name
    p = subprocess.run([sys.executable, f], capture_output=True, env=env,
                       timeout=1800)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    all_ = out + "\n" + err
    m = re.search(r"Ran (\d+) test", all_)
    n = int(m.group(1)) if m else 0
    total_run += n

    # ⚠ **不要**把 n==0 当失败：tests_mcp / tests_generic / tests_import
    #   这些是自定义脚本式套件（自己 print PASS/FAIL），本来就没有
    #   unittest 用例，n 一直是 0。把 0 当失败会误报一片。
    #   真正该拦的是上面那句「一个文件都没找到」。
    reasons = []
    if p.returncode != 0:
        reasons.append(f"exit={p.returncode}")
    if re.search(r"(?m)^FAILED", all_):
        reasons.append("FAILED")
    if "Traceback (most recent call last)" in all_:
        reasons.append("Traceback")
    fm = re.search(r"失败\s*([1-9]\d*)", all_)
    if fm:
        reasons.append(f"失败={fm.group(1)}")
    if re.search(r"^ERROR:", all_, re.M):
        reasons.append("ERROR:")
    # 单个文件自己报的泄漏（tearDown 没删掉 / atexit 兜底也没删掉）
    if "CACX_TEMP_LEAK" in all_:
        reasons.append("临时目录泄漏")

    if reasons:
        bad.append((name, reasons, n, all_[-2500:]))
        print(f"  FAIL  {name:36} n={n:<4} {'/'.join(reasons)}")
    else:
        print(f"  ok    {name:36} n={n}")
    sys.stdout.flush()

print("\n" + "=" * 68)

# 跑完再点一次名：即使各文件都没自报（进程被强杀之类），只要根目录非空就是漏了。
left = list(_temppool.ROOT.iterdir()) if _temppool.ROOT.exists() else []
if left:
    total = 0
    for p in left:
        total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    print(f"✗ 临时目录泄漏：本次跑完仍残留 {len(left)} 项，合计 "
          f"{total / 1048576:.1f} MB")
    for p in left[:20]:
        print(f"    {p.name}")
    if len(left) > 20:
        print(f"    …… 另有 {len(left) - 20} 项")
else:
    print(f"临时目录：0 残留（临时根 {_temppool.ROOT}）")

print(f"测试文件 {len(files)} 个，用例 {total_run} 项，失败文件 {len(bad)} 个")
if bad:
    print("=" * 68)
    for f, reasons, n, tail in bad:
        print(f"\n### {f}  ({'/'.join(reasons)}, n={n})")
        print(tail)
    sys.exit(1)
if left:
    print("=" * 68)
    print("用例本身全过，但临时目录在累积 —— 这正是当初攒到 4.30 GB 的那条路。")
    sys.exit(1)
print(">>> 全部通过")