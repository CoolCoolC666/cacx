# -*- coding: utf-8 -*-
r"""确定 cacx 在旧 Python 上的真实下限，而不是靠猜。

背景：启动器原先硬拦 3.10，因为 requirements.txt 里有 mcp>=2.3。
现在要把 MCP 拆成可选，那么"核心功能到底需要多新"就必须重新测出来，
否则就是把一个拍脑袋的版本号换成另一个拍脑袋的版本号。

本脚本查两件事，都是静态的、可复现的：

1. 代码里有没有 3.9/3.10 才有的语法或标准库 API
   （match 语句、X|Y 运行时联合类型、list[int] 注解在运行期求值、
     str.removeprefix/removesuffix、dict | merge、match 上下文管理器…）。
   注意：`from __future__ import annotations` 会让**注解**变字符串，
   3.8 上 `def f(x: list[int])` 不报错 —— 但**默认值**和**基类**不行。

2. 核心依赖各自的 Requires-Python 区间，用来决定该把 pip 的解析下限
   钉在哪。networkx 是唯一的风险项：3.4+ 要求 3.10，pip 会自动降级选
   3.1，而我们要确认代码没用到 3.1 之后才有的东西。

只读。不写任何文件。
用法：py -3 -X utf8 （测试）\_pyfloor_probe.py
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 3.9 / 3.10 才有的标准库 API。key=最低版本元组
# ⚠ 版本号一律用 **tuple**，不能用 float。
#   float("3.9") >= 3.10 在 Python 里是 3.9 >= 3.1 —— 恒真。
#   我第一版就踩了这个，把一处 3.9 的 removeprefix 报成了"3.10 硬门槛"。
#   版本比较和排序一样，比较器比数字类型重要。
NEW_APIS = {
    (3, 9): [r"\.removeprefix\(", r"\.removesuffix\(", r"graphlib\.",
             r"zoneinfo\.", r"\bfunctools\.cache\b"],
    (3, 10): [r"\bmatch\b\s+.*:\s*$", r"^\s*case\s+\w+.*:\s*$",
              r"\bexcept\*\b", r"itertools\.pairwise"],
}

# 用到的 networkx API，逐个标注引入版本
NETWORKX_USAGE = [
    r"nx\.\w+",
    r"networkx\.\w+",
]


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def scan_syntax():
    hits = []
    files = sorted(ROOT.rglob("*.py"))
    # 跳过分发包副本与测试目录里的临时探针（它们不是发布产物）
    files = [f for f in files
             if "分发版" not in str(f) and ".git" not in str(f)]
    for f in files:
        # utf-8-sig: 目录里有带 BOM 的遗留脚本（_chk.py），用 utf-8 读会
        # 直接 SyntaxError 崩掉整个扫描。扫描器的输入不该比被扫描物更脆。
        try:
            src = f.read_text(encoding="utf-8-sig")
        except Exception as e:  # noqa: BLE001
            hits.append((rel(f), 0, f"读取失败: {e}"))
            continue
        tree = ast.parse(src, filename=str(f))
        # 1) match 语句：AST 层面最可靠
        for node in ast.walk(tree):
            if isinstance(node, ast.Match):
                hits.append((rel(f), (3, 10), f"match 语句 (line {node.lineno})"))
        # 2) 运行期会求值的 X | Y 类型注解 —— 只在**没有**
        #    from __future__ import annotations 时才算数
        has_future = "from __future__ import annotations" in src
        if not has_future:
            for node in ast.walk(tree):
                ann = None
                if isinstance(node, (ast.AnnAssign, ast.arg)):
                    ann = node.annotation
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ann = node.returns
                if ann is None:
                    continue
                for sub in ast.walk(ann):
                    if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                        hits.append((rel(f), (3, 10),
                                     f"运行期 | 类型注解 (line {getattr(sub, 'lineno', '?')})"))
                        break
        # 3) 新标准库 API，纯文本
        for ver, pats in NEW_APIS.items():
            for pat in pats:
                for m in re.finditer(pat, src, re.M):
                    hits.append((rel(f), ver,
                                 f"{pat} @line {src[:m.start()].count(chr(10)) + 1}"))
    return hits


def scan_networkx():
    used = {}
    for f in sorted(ROOT.rglob("*.py")):
        if "分发版" in str(f):
            continue
        try:
            src = f.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for pat in NETWORKX_USAGE:
            for m in re.finditer(pat, src):
                used.setdefault(m.group(0), set()).add(rel(f))
    return used


def main():
    print("=" * 70)
    print("1) 3.9/3.10 专有语法与标准库 API 扫描")
    print("=" * 70)
    hits = scan_syntax()
    # 发布产物（cacx/、*.py 顶层脚本）与 测试/工具 目录要分开算：
    # 只有前者会被打包给使用者，后者的门槛跟"使用者能不能跑"无关。
    shipped = [h for h in hits if not h[0].startswith("（测试）")]
    tooling = [h for h in hits if h[0].startswith("（测试）")]
    if not shipped:
        print("  PASS  发布代码里没有任何 3.9/3.10 专有构造。")
    else:
        for path, ver, why in sorted(shipped, key=lambda x: (x[1], x[0])):
            print(f"  HIT   需要>={'.'.join(map(str, ver))}  {path}  {why}")
    if tooling:
        print(f"  note  测试/工具目录另有 {len(tooling)} 处（不随包发布，不影响使用者）")
    print()
    print("=" * 70)
    print("2) networkx API 使用面（决定能不能降到 3.1）")
    print("=" * 70)
    used = scan_networkx()
    if not used:
        print("  （没有用到 networkx）")
    for api, files in sorted(used.items()):
        print(f"  {api:34s} {', '.join(sorted(files))}")
    print()
    print("  提示：networkx 3.4+ 要求 Python>=3.10，3.2/3.3 要求>=3.9，")
    print("        3.0/3.1 支持 3.8。上面这些 API 若全是 2.x 就有的，")
    print("        降级解析是安全的。")

    print()
    print("=" * 70)
    print("3) 结论依据")
    print("=" * 70)
    hard = [h for h in shipped if h[1] >= (3, 10)]
    soft = [h for h in shipped if h[1] < (3, 10)]
    if not hard:
        print("  发布代码本身可在 3.8 上运行（无 3.10 硬门槛）。")
    else:
        print(f"  发布代码里有 {len(hard)} 处 3.10 硬门槛，必须 >=3.10：")
        for h in hard:
            print(f"    - {h[0]}  {h[2]}")
    if soft:
        print(f"  另有 {len(soft)} 处 3.9 门槛（3.8 跑不了，3.9 可）：")
        for h in soft:
            print(f"    - {h[0]}  {h[2]}")
    return 0


# ---- 4) 依赖的 Requires-Python 实查 ----
# 这一段是本脚本存在的核心理由：**版本下限不能靠记忆写**。
# 我对 networkx 各版本的支持区间有印象，但印象不是证据 ——
# 一旦记错，pip 会在 3.8 机器上解析出一个跑不通的版本，
# 而错误信息会指向别处，比没有下限更难查。
# 所以直接读 PyPI 上每个版本的 requires_python 元数据。
DEPS = ["requests", "fastapi", "uvicorn", "beautifulsoup4",
        "jieba", "pyvis", "networkx", "mcp"]


def probe_pypi():
    import json
    import urllib.request
    print("=" * 70)
    print("4) 依赖的 Requires-Python（实查 PyPI，不靠记忆）")
    print("=" * 70)
    for name in DEPS:
        url = f"https://pypi.org/pypi/{name}/json"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  {name:18s} 查询失败: {e}")
            continue
        # 取最新若干个版本，按发布时间从新到旧，标注各自 requires_python
        rows = []
        for ver, files in data.get("releases", {}).items():
            if not files:
                continue
            rp = {f.get("requires_python") for f in files}
            rows.append((files[0].get("upload_time", ""), ver,
                         sorted(x for x in rp if x)))
        rows.sort(reverse=True)
        # 只打印最近 8 个 + 最新的稳定版
        print(f"  {name}  (latest = {data['info']['version']}, "
              f"requires_python = {data['info'].get('requires_python')})")
        for _, ver, rp in rows[:8]:
            print(f"      {ver:12s} {rp}")
        print()


if __name__ == "__main__":
    rc = main()
    try:
        probe_pypi()
    except Exception as e:  # noqa: BLE001
        print(f"  （PyPI 段跳过：{e}）")
    sys.exit(rc)
