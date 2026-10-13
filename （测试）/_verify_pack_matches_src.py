# -*- coding: utf-8 -*-
"""分发包 == 当前源码？逐文件比对哈希

补的是一道一直没人验的闸：
  · `_repack.py` 里的检查验的是「包自己完整吗」（漏了什么、有没有密钥）
  · `_accept_dist_pack.py` 验的是「解包后服务跑起来对吗」
  · **没有任何一处验「包里那份代码 == 我刚提交的那份代码」**
以前就出过一次「说明写 4416、库里还是 4515」的文档与数据各说各话 ——
性质一样：发出去的东西和当时的真相不是一份。

⚠⚠⚠ 排除规则**不用 ast 抄、也不用自己判** —— 直接把 `_repack.py` 里那个
   `skip()` 函数原样抽出来执行。抄一份必然对不上，而这里已经栽了两次：
     ① 第一版漏了 `data` 与 `config/sites.d/` → 把「快照库本来就和线上库不同」
        「站点配置是有意排除的」报成漂移，**假警报**；
     ② 第二版自己写 `name.startswith("_")` → 把 `__init__.py` 当本地脚本跳过，
        改了源码它也不报红，**假阴性**。而 `_repack.py` 早就为这条写过注释：
        「双下划线是 Python 语法，必须保留，上次就因为没区分把 5 个 __init__.py
        全排掉了」—— 同一个坑，同一天栽两遍，根子都是「另写一份口径」。
   假警报和假阴性都远比不验更坏：前者让人怀疑闸坏了，后者让人以为验过了。
"""
import ast
import hashlib
import sys
from pathlib import Path

TESTDIR = Path(__file__).resolve().parent
ROOT = TESTDIR.parent
REPACK = TESTDIR / "_repack.py"


def _load_repack_skip():
    """把 _repack.py 的常量与 skip() 函数抽出来，在本地命名空间里执行。

    这样本闸的排除口径**按定义等于**打包脚本的口径 ——
    打包规则改了（哪怕只是把某个 `__` 开头的特判删掉），本闸自动跟着变。
    """
    tree = ast.parse(REPACK.read_text(encoding="utf-8"))
    ns = {"Path": Path}
    want = ("DIR_EXCLUDE", "FILE_EXCLUDE", "PATTERNS")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in want:
                    ns[t.id] = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == "skip":
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(ast.fix_missing_locations(mod), str(REPACK), "exec"), ns)
    missing = [k for k in want if k not in ns]
    if missing or "skip" not in ns:
        raise SystemExit(f"🔴 从 {REPACK.name} 里抽不出 {missing or ['skip']}，"
                         f"打包规则改名了 —— 本闸必须跟着改")
    return ns["skip"], set(ns["DIR_EXCLUDE"])


skip, _DIR_EXCLUDE = _load_repack_skip()

cands = sorted(p for p in (ROOT.parent / "网站程序").glob("cacx-分发版-*")
               if p.is_dir())
if not cands:
    print("🔴 网站程序/ 下找不到分发包目录")
    sys.exit(1)
PACK = Path(sys.argv[1]) if len(sys.argv) > 1 else cands[-1]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def norm(b):
    """换行符归一化后再比。

    ⚠ 为什么必须归一：本仓 core.autocrlf 开着，**工作区是 CRLF、git blob
      与分发包是 LF**。拿工作区当基准逐字节比，autocrlf 一开就必然误报
      「包里的代码和源码不一样」—— 而其实只是换行符。
      实测踩过：cacx/__init__.py 报不一致，diff 出来只有 18 个 \\r。
    归一化不是放水：raw 不同但 norm 相同的，会单独列成「仅换行符不同」，
    不藏。
    """
    return b.replace(b"\r\n", b"\n")


def excluded(rel):
    """rel：相对项目根的 Path。

    ⚠ `skip()` 是**遍历目录时逐个路径分量**调用的：它判 `name in DIR_EXCLUDE`，
      拿的是**本名**。直接问它 `skip("cacx.db", "data/cacx.db")` 判不出来 ——
      目录级排除（data / .git / __pycache__ / （测试））得自己沿祖先查一遍。
      第一版就这么漏的，于是 .git 里 243 个文件全被报成「源码有而包里没有」。
    """
    if skip(rel.name, str(rel).replace("\\", "/")):
        return True
    return any(anc.name in _DIR_EXCLUDE for anc in rel.parents
               if anc.name and anc != Path("."))


same, eol_only, diff, only_pack, missing = 0, [], [], [], []

for f in sorted(PACK.rglob("*")):
    if not f.is_file():
        continue
    rel = f.relative_to(PACK)
    if excluded(rel):
        continue
    src = ROOT / rel
    if not src.exists():
        only_pack.append(str(rel).replace("\\", "/"))
        continue
    a, b = f.read_bytes(), src.read_bytes()
    if a == b:
        same += 1
    elif norm(a) == norm(b):
        eol_only.append(str(rel).replace("\\", "/"))
    else:
        diff.append(str(rel).replace("\\", "/"))

for f in sorted(ROOT.rglob("*")):
    if not f.is_file():
        continue
    rel = f.relative_to(ROOT)
    if excluded(rel) or "网站程序" in rel.parts:
        continue
    if not (PACK / rel).exists():
        missing.append(str(rel).replace("\\", "/"))

print(f"比对目录：{PACK}")
print(f"包内与源码逐字节相同  ：{same}")
print(f"仅换行符不同（CRLF/LF）：{len(eol_only)}  {eol_only[:6]}")
print(f"内容不一致            ：{len(diff)}  {diff if diff else ''}")
print(f"包内独有（打包时生成） ：{len(only_pack)}  {only_pack}")
print(f"源码有而包里没有      ：{len(missing)}  {missing[:10]}")

ok = not diff and not missing
print()
print("✅ 包内代码与当前源码完全一致" if ok
      else "🔴 存在漂移 —— 发出去的东西和刚提交的不是一个东西")
sys.exit(0 if ok else 1)
