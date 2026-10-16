# -*- coding: utf-8 -*-
"""
只验一件事：runtime/ 下还有多少文件会被通用排除规则排掉。

⚠ 这条豁免（VERBATIM_PREFIXES）以前**只存在于注释里**，从来没在代码里
   落地过，于是 runtime/ 里 636 个第三方文件被当成「我们自己的临时脚本」
   排掉，包内 numpy 直接 import 失败。
   重打包一次要 20 多分钟、还要 8770 服务在线，所以先用这个离线探针
   把「排掉的文件数」打到 0，再去跑完整流程。

判据：跑完必须 printed `仍会被排掉: 0 个`。
     顺便打印几个历史上真实被误伤的路径，确认它们现在**留在**集合里。
"""
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
RT = SRC / "runtime"
SP = RT / "Lib" / "site-packages"

DIR_EXCLUDE_BY_NAME = {"__pycache__", ".pytest_cache", ".ruff_cache",
                       ".mypy_cache", ".git"}
PATTERNS = ("_*.py", "_*.js", "_*.txt", "_*.log", "_*.csv", "_*.jsonl", "*.err")
VERBATIM_PREFIXES = ("runtime/",)


def old_skip(name, rel=""):
    """修复前的规则：runtime/ 没有豁免，与线上出错的那版一致。"""
    if name in DIR_EXCLUDE_BY_NAME:
        return True
    if name.endswith((".err", ".log", ".pyc")):
        return True
    if name.startswith("_") and not name.startswith("__"):
        return any(Path(name).match(p) for p in PATTERNS)
    return False


def new_skip(name, rel=""):
    """修复后的规则，与 _repack.py 的 skip() 保持一致。"""
    if rel.startswith(VERBATIM_PREFIXES):
        return name.endswith(".pyc") or name in DIR_EXCLUDE_BY_NAME
    return old_skip(name, rel)


ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


if not RT.exists():
    print("🔴 runtime/ 不存在，先跑 _build_portable_runtime.py")
    raise SystemExit(2)

old_hit, new_hit, total = [], [], 0
for root, dirs, files in __import__("os").walk(RT):
    for fn in files:
        p = Path(root) / fn
        rel = p.relative_to(SRC).as_posix()
        total += 1
        if old_skip(fn, rel):
            old_hit.append(rel)
        if new_skip(fn, rel):
            new_hit.append(rel)

# ⚠⚠ 断言必须只对「**非 .pyc**」归零，不能写成「一个都不排」。
#   .pyc / __pycache__ 是我们构建运行时时自己跑出来的运行产物，
#   带着走只会让 48 项里的「无 __pycache__ / 无 .pyc」两条闸假红 ——
#   而且 runtime/ 里确实躺着 cpython-313 的 .pyc（早期用 3.13 的 pip
#   装混进来的陈旧缓存），排掉它们本来就是对的。
#   我第一版就是写了「0 个」，跑出 4759 个假 FAIL：断言比现实更严，
#   会把「排对了」误报成「排错了」。
new_hit_non_pyc = [r for r in new_hit
                   if not r.endswith(".pyc") and "/__pycache__/" not in r]

check("runtime/ 文件全部可枚举", total > 5000, f"{total} 个")

# 1. 老规则确实排掉了东西（否则这个探针就证明不了任何事）
check("修复前确实有文件被误排", len(old_hit) >= 600, f"{len(old_hit)} 个")

# 2. 新规则排掉的都是运行产物，**没有任何源码/资源**
check("仍会被排掉的非 .pyc 文件: 0 个", len(new_hit_non_pyc) == 0,
      f"{len(new_hit_non_pyc)} 个：{new_hit_non_pyc[:5]}")
check("排掉的确只是 .pyc", len(new_hit) and all(r.endswith(".pyc") for r in new_hit),
      f"{len(new_hit)} 个全是 .pyc")

# 3. 历史上真实炸过的那些路径，现在必须**保留**
for probe in ("runtime/Lib/site-packages/numpy/_expired_attrs_2_0.py",
              "runtime/Lib/site-packages/IPython/core/_dunder_ops.py",
              "runtime/Lib/site-packages/IPython/utils/_process_common.py"):
    check(f"保留 {probe.rsplit('/', 1)[-1]}", probe not in new_hit and (SRC / probe).exists())

# 4. jedi 类型存根（上一轮被 data/venv/lib 按名字连坐掉的那 482 个）
stubs = list(SP.glob("jedi/third_party/typeshed/**/venv/*.pyi"))
check("jedi venv 存根没被连坐", len(stubs) > 0, f"{len(stubs)} 个")

print()
print(f"通过 {ok} 项，失败 {fail} 项")
raise SystemExit(1 if fail else 0)
