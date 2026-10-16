# -*- coding: utf-8 -*-
r"""直接调 _graph_payload，把 500 的真 traceback 拿出来。

为什么不用 HTTP 去打：
  服务端 stderr 是 0 字节 —— 500 被应用的异常处理吞掉了，
  uvicorn 根本没打出 traceback。只看 HTTP 只能得到一个 "500"，
  没有任何定位价值。直接调同一个函数才有 traceback。

为什么要在两个解释器下各跑一次：
  同样的代码 + 同样的库版本，在系统 3.13 下 14:56 那次打包成功，
  在便携 3.12 下 500。若两边都失败，那是代码/数据问题；
  只在 3.12 失败，才是运行时选型的问题 —— 这两种结论要修的地方完全不同。

只读。不写 config/settings.json，不写 data/cacx.db。
"""
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_DEGREE = 2
NODE_TYPES = "org,policy,problem,measure"


def main():
    print(f"解释器: {sys.executable}")
    print(f"版本  : {sys.version.split()[0]}")
    try:
        import networkx
        print(f"networkx: {networkx.__version__}")
    except Exception:
        pass
    # ⚠ 不能写 `from cacx.web import app as A`：
    #   cacx/web/__init__.py 把 FastAPI 实例 re-export 成了 `app`，
    #   于是 `from ... import app` 拿到的是**实例**不是模块，
    #   报 'FastAPI' object has no attribute '_graph_payload'。
    #   名字被同名变量抢走了，只能用 importlib 按模块路径拿。
    import importlib
    A = importlib.import_module("cacx.web.app")
    from cacx import graph_analysis
    print(f"graph_analysis 来自: {graph_analysis.__file__}")

    t0 = time.time()
    print(f"调用 _graph_payload(min_degree={MIN_DEGREE}, "
          f"node_types={NODE_TYPES}) …")
    try:
        r = A._graph_payload(MIN_DEGREE, 300, 1.0, 20, 0, "", "", "",
                             1, "", "", "", node_types=NODE_TYPES)
        print(f"✅ 成功 {time.time() - t0:.1f}s  "
              f"nodes={len(r.get('nodes', []))} edges={len(r.get('edges', []))} "
              f"components={r.get('components')}")
        return 0
    except BaseException:
        print(f"❌ 失败 {time.time() - t0:.1f}s，traceback：")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
