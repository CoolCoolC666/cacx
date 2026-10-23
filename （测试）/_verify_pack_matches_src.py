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

    ⚠ 这里曾经连栽两次，都是"抽常量"这一步本身太脆：
      ① 硬编码只抽 3 个名字。拆分 DIR_EXCLUDE 之后 skip() 多引用了
         DIR_EXCLUDE_BY_NAME / DIR_EXCLUDE_BY_PATH / VERBATIM_PREFIXES，
         literal_eval 过去之后 exec 照样 NameError ——
         也就是说，改打包规则会让**这个闸自己**先崩，而不是给出结论。
      ② 无脑 literal_eval，遇到 `DIR_EXCLUDE = A | B` 这种集合并集
         （ast.BinOp）直接 ValueError: malformed node。
    所以改成：先抽字面量，再解引用式常量（只认「已知名字的集合并集」），
    最后**检查 skip() 的自由变量是否齐全**，缺一个就在这里报清楚，
    而不是等到 exec 里炸一句看不懂的 NameError。
    """
    tree = ast.parse(REPACK.read_text(encoding="utf-8"))
    ns = {"Path": Path}
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)]

    # 第一轮：纯字面量
    for node in assigns:
        for t in node.targets:
            if getattr(t, "id", None) is None:
                continue
            try:
                ns[t.id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass

    # 第二轮：`X = A | B` 这种只由已知名字组成的集合并集
    # 最多迭代若干轮，容忍定义顺序（PATTERNS 在 FILE_EXCLUDE 后面等）
    for _ in range(len(assigns) + 1):
        progressed = False
        for node in assigns:
            for t in node.targets:
                name = getattr(t, "id", None)
                if name is None or name in ns:
                    continue
                if not isinstance(node.value, ast.BinOp):
                    continue
                try:
                    ns[name] = eval(compile(ast.Expression(node.value), "<c>", "eval"),
                                    {"__builtins__": {}}, dict(ns))
                    progressed = True
                except Exception:
                    continue
        if not progressed:
            break

    skip_node = next((n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "skip"), None)
    if skip_node is None:
        raise SystemExit(f"🔴 {REPACK.name} 里找不到 skip()，本闸必须跟着改")

    # skip() 里引用了哪些**真正的外部名字**？
    #   ⚠ 必须把参数名和局部绑定排除掉，否则 d/name/p/rel 这些
    #     参数与推导式变量会被误判成缺失常量，闸门对着一堆假警报拒绝跑。
    import builtins
    a = skip_node.args
    locals_ = {x.arg for x in (a.args + a.posonlyargs + a.kwonlyargs)}
    if a.vararg:
        locals_.add(a.vararg.arg)
    if a.kwarg:
        locals_.add(a.kwarg.arg)
    locals_ |= {n.id for n in ast.walk(skip_node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    refs = {n.id for n in ast.walk(skip_node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    missing = sorted(refs - locals_ - set(ns) - set(dir(builtins)))
    if missing:
        raise SystemExit(
            f"🔴 skip() 引用了这几个名字，但没从 {REPACK.name} 里抽到：{missing}\n"
            f"   闸门必须与打包规则同口径 —— 缺常量就不能继续比，"
            f"否则本闸会拿一套**过时的**排除规则去核对包，报出假结论。")

    mod = ast.Module(body=[skip_node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), str(REPACK), "exec"), ns)
    return ns["skip"], set(ns.get("DIR_EXCLUDE", ()))


skip, _DIR_EXCLUDE = _load_repack_skip()

cands = sorted(p for p in (ROOT.parent / "网站程序").glob("cacx-分发版-*")
               if p.is_dir())
if not cands:
    print("🔴 网站程序/ 下找不到分发包目录")
    sys.exit(1)

# ⚠ 现在有两种形态的目录并存（完整包 / `-slim`），不能靠 sorted()[-1] 挑。
#   按名字排下来 `-slim` 永远在最后，于是**每次都比的是瘦身包**，
#   而人以为在比完整包 —— 报告里还不会写这件事，它只会报「一致」。
#   形态必须由调用方显式指定，且找不到就报错，不静默换一个。
SLIM = "--slim" in sys.argv
_pool = [p for p in cands if p.stem.endswith("-slim")] if SLIM \
    else [p for p in cands if not p.stem.endswith("-slim")]
if not _pool:
    print(f"🔴 找不到 {'瘦身' if SLIM else '完整'}包目录（--slim 指定了形态，"
          f"但磁盘上没有对应的）")
    sys.exit(1)
# ⚠ 位置参数必须跳过开关。原式是 `Path(sys.argv[1]) if len(sys.argv) > 1`，
#   而 `... --slim` 的 argv[1] 恰恰就是 "--slim" —— 于是 PACK 变成
#   一个叫 "--slim" 的**不存在的目录**，比对对着空气跑，得出
#   「127 个源码文件没进包」这种看着像真问题的假结论。
#   教训：加了命令行开关之后，位置参数不能再靠下标取。
_pos = [a for a in sys.argv[1:] if not a.startswith("-")]
PACK = Path(_pos[0]) if _pos else _pool[-1]
print(f"比对对象：{PACK.name}（{'瘦身包' if SLIM else '完整包'}）")

# ⚠⚠ 必须先确认 PACK **真的存在且是个目录**，否则会报假结论。
#
#   实测踩过（2026-10-23）：从错误的当前目录调用本脚本，
#   位置参数是**相对路径** `..\网站程序\...`，解析到了别的地方 →
#   PACK.rglob("*") 返回空 → 「包内与源码逐字节相同：0」，
#   同时「源码有而包里没有：10414」，看着像**整包没进源码**的大事故，
#   其实是比对对着空气跑。
#
#   与上面 `--slim` 被当位置参数是同一族坑：**静默地比错东西，
#   却输出一份看着很像真问题的报告**。假结论比不验更坏。
if not PACK.exists():
    print(f"🔴 要比对的目录不存在：{PACK}")
    print(f"   （位置参数是相对路径时，当前工作目录必须是项目根；"
          f"现在的工作目录是 {Path.cwd()}）")
    sys.exit(1)
if not PACK.is_dir():
    print(f"🔴 要比对的不是一个目录：{PACK}")
    sys.exit(1)
if not any(PACK.rglob("*")):
    print(f"🔴 要比对的目录是空的：{PACK}")
    sys.exit(1)


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
    # 瘦身包不带 runtime\，这一整棵在"源码有而包里没有"那侧是**预期**的，
    # 不排掉就会报出 1 万多条假差异。这里同样不能塞进 skip()：
    # 那个函数被 AST 抽出来复用，而 SLIM 是打包脚本的全局量。
    if SLIM and rel.parts[0] == "runtime":
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
