# -*- coding: utf-8 -*-
"""静态查「用了但没导入 / 拼错的名字」——这一类只有运行到才炸。

起因是本轮真实踩了一次：我加了 `return JSONResponse(...)`，
`py -c "import cacx.web.app"` 照样成功、FastAPI 照样把路由注册好、
`app.routes` 里也照样看得到 `/api/graph/analysis` ——
**所有"导入自检"全绿**，而那个分支一跑到就 NameError。
根因：路由函数体里的名字是**运行时**才解析的，模块级导入检查不到。

⚠ 为什么不用 flake8/ruff 就完事：本机不一定装了，而装依赖本身
  是这个项目反复踩的坑（requirements 装不上就起不来）。
  这里只用标准库 ast，一件依赖都不加 —— 闸门自己不能成为
  "跑不起来"的新来源。

查两样：
  ① 模块级作用域里被**使用**但**没绑定**的全局名
     （排除函数局部名、参数名、内置名、import 进来的名）
  ② 只在本模块 import 了、却从未使用的名字（顺带发现过期导入）
"""
from __future__ import annotations

import ast
import builtins
import os
import sys
from typing import Dict, List, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TARGETS = [
    os.path.join(ROOT, "cacx", "web", "app.py"),
    os.path.join(ROOT, "cacx", "graph_analysis.py"),
]

#: 这些名字在模块里是靠 exec/globals 注入或框架注册的，
#: 静态查不出来也不该报。
_ALLOW = {"__file__", "__name__", "__doc__", "__package__", "__builtins__",
          "app", "cfg", "cfg_snapshot", "self", "cls"}


class ScopeCollector(ast.NodeVisitor):
    """收集模块级**绑定**的名字（导入、赋值、def/class、for 目标…）。"""

    def __init__(self) -> None:
        self.bound: Set[str] = set()
        self.used: List[Tuple[str, int]] = []

    # -- 绑定 --
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.bound.add((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            self.bound.add(a.asname or a.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    self.bound.add(n.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.bound.add(node.target.id)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.bound.add(node.target.id)

    def visit_For(self, node: ast.For) -> None:
        for n in ast.walk(node.target):
            if isinstance(n, ast.Name):
                self.bound.add(n.id)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                for n in ast.walk(item.optional_vars):
                    if isinstance(n, ast.Name):
                        self.bound.add(n.id)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.bound.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.bound.update(node.names)

    # -- 使用 --
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.used.append((node.id, node.lineno))


def function_scopes(tree: ast.AST) -> Set[str]:
    """所有函数/类内部绑定过的名字 —— 它们在模块级不算"未定义"。"""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda)):
            a = node.args
            for arg in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs):
                names.add(arg.arg)
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                names.add(al.asname or al.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
    return names


def check_file(path: str) -> Tuple[int, int, List[str]]:
    src = open(path, "rb").read().decode("utf-8")
    tree = ast.parse(src, filename=path)

    c = ScopeCollector()
    c.visit(tree)
    local = function_scopes(tree)
    known = c.bound | local | set(dir(builtins)) | _ALLOW

    undefined: Dict[str, int] = {}
    for name, line in c.used:
        if name not in known:
            undefined.setdefault(name, line)

    unused = sorted(n for n in c.bound
                    if n not in local and not n.startswith("_")
                    and sum(1 for u, _ in c.used if u == n) == 0
                    and n not in _ALLOW)

    lines = [f"{os.path.relpath(path, ROOT)}"]
    if undefined:
        for n, ln in sorted(undefined.items(), key=lambda x: x[1]):
            lines.append(f"    第 {ln} 行用了未绑定的名字：{n}")
    if unused:
        lines.append(f"    导入/定义后从未使用：{', '.join(unused)}")
    if not undefined and not unused:
        lines.append("    （干净）")
    return len(undefined), len(unused), lines


def main() -> int:
    print("=" * 70)
    print("静态查「用了没导入 / 过期导入」")
    print("=" * 70)
    total_bad = 0
    for p in TARGETS:
        if not os.path.exists(p):
            print(f"  跳过（不存在）：{p}")
            continue
        u, n, lines = check_file(p)
        for ln in lines:
            print(ln)
        total_bad += u + n
    print()
    print("=" * 70)
    if total_bad == 0:
        print("✅ 没有未绑定名字，也没有过期导入")
        return 0
    print(f"❌ 共 {total_bad} 处要修")
    return 1


if __name__ == "__main__":
    sys.exit(main())
