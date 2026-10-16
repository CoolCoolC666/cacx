# -*- coding: utf-8 -*-
r"""小图冒烟：在毫秒级内抓住「依赖装得上、但代码跑不了」。

这个脚本存在的理由是一笔真实代价：
  `_resolve_floor.py` 只验了 **pip 解析成功**，`_build_portable_runtime.py`
  的 `import cacx` 也只验了**能 import** —— 两道都绿，
  可 `/api/graph/analysis` 一调就
    `ModuleNotFoundError: No module named 'numpy'`
  因为 networkx 的 `pagerank()` **无条件**派发到 `_pagerank_scipy`，
  而 networkx 自己不声明 numpy/scipy 依赖。
  真跑到那一步要 **348 秒**（冷启动全图），等一次错 6 分钟。

  所以这里不跑全库，只构造一张 6 节点的小图，
  把 `analyze()` 里用到的每一条 networkx 路径都走一遍
  （度/加权度/介数/PageRank/Louvain/连通块）：
      · 毫秒级
      · 与数据规模无关
      · 抓的正是「依赖声明了但用不了」这一类

  ⚠ 关键设计：跑的是**真实的 analyze()**，不是「import 一下 networkx」。
     `import networkx` 永远成功，`nx.pagerank(G)` 才会在
     缺 scipy 时炸 —— 断言必须打在会被用到的那一刻。

用法（必须用目标解释器跑，不能用开发机的）：
    runtime\python.exe （测试）\_smoke_analyze.py     # 构建/验收时
    python -m cacx.serve 之外的任何入口都不算
只读：不碰 config/settings.json 与 data/cacx.db。
"""
import os
import sys
import traceback
from pathlib import Path


def _find_root():
    """定位 cacx 包所在的项目根。

    为什么不在这里写死 `parent.parent`：
      这个脚本要被**三方**用，位置各不相同 ——
        · 开发/构建：在 （测试）/ 下跑，根是 parent.parent
        · 验收：会被**复制进 zip 解压出来的临时目录**再跑，
          那时它是 <stage>/_smoke_analyze.py，parent.parent 反而指到
          stage 的上一级去了，于是 import cacx 失败 —— 而失败原因
          会被误读成"分发包坏了"，其实只是我拷错了位置。
    所以优先认环境变量，其次认「当前工作目录里有没有 cacx/」。
    """
    env = os.environ.get("CACX_SMOKE_ROOT")
    if env and (Path(env) / "cacx" / "__init__.py").exists():
        return Path(env)
    cwd = Path.cwd()
    if (cwd / "cacx" / "__init__.py").exists():
        return cwd
    return Path(__file__).resolve().parent.parent


def main():
    ROOT = _find_root()
    sys.path.insert(0, str(ROOT))
    print(f"root: {ROOT}")

    import networkx as nx
    from cacx import graph_analysis as GA

    # 6 个节点、7 条边的小图：足以触发介数/PageRank/Louvain/连通块，
    # 又快到可以放进每次构建里。刻意包含一个度数 1 的悬挂节点，
    # 因为 PageRank 的 dangling 节点处理是最容易缺依赖/出边界错的地方。
    G = nx.Graph()
    G.add_edges_from([
        ("A", "B"), ("A", "C"), ("B", "C"), ("C", "D"),
        ("D", "E"), ("E", "F"), ("F", "D"),
    ])
    G.add_node("Z")          # 孤立节点：连通块 / PageRank 都要处理

    print(f"解释器 {sys.executable}  networkx {nx.__version__}")
    try:
        r = GA.analyze(G)
    except BaseException:
        print("SMOKE-FAIL")
        traceback.print_exc()
        return 1

    # 不只看"没抛异常"，还要看**真的出数了**。
    # 只 assert 不抛异常的闸，会被"返回空 dict"这类假成功骗过去。
    need = ["degree", "strength", "betweenness", "pagerank", "communities"]
    missing = [k for k in need if not r.get(k)]
    if missing:
        print(f"SMOKE-FAIL  analyze() 没返回这些键: {missing}")
        print(f"  实际返回的键: {sorted(r.keys())}")
        return 1

    print(f"SMOKE-OK  degree={len(r['degree'])} "
          f"betweenness={len(r['betweenness'])} "
          f"pagerank={len(r['pagerank'])} "
          f"communities={len(r['communities'])} "
          f"components={r.get('components')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
