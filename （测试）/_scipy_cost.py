# -*- coding: utf-8 -*-
r"""量 scipy/numpy 加进依赖的确切代价（拿真实 wheel 大小，不靠估算）。

背景：networkx 的 pagerank() 无条件走 _pagerank_scipy，
而 networkx 自己**不声明** numpy/scipy 依赖。于是 requirements.txt
只写 networkx>=3.0 就会装出一个"装得上、pagerank 一调就炸"的环境。

本脚本回答两件事：
  1. cp312/win_amd64 的 wheel 各多大（决定分发包涨多少）
  2. 只装 scipy 时 numpy 会不会被带进来（避免重复声明）
只读。
"""
import json
import sys
import urllib.request

PKGS = ["scipy", "numpy"]
TAG = "cp312"


def wheels(name):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json",
                                timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    ver = data["info"]["version"]
    out = []
    for f in data["urls"]:
        fn = f["filename"]
        if "win_amd64" in fn and TAG in fn and "cp312" in fn:
            out.append((fn, f["size"]))
    return ver, data["info"].get("requires_python"), data["info"].get("requires_dist"), out


def main():
    total = 0
    for p in PKGS:
        ver, rp, rd, ws = wheels(p)
        print(f"{p} {ver}  Requires-Python={rp}")
        for fn, sz in ws:
            print(f"    {fn:<62} {sz/1e6:6.1f} MB")
        best = max((sz for _, sz in ws), default=0)
        total += best
        print(f"    -> 取最大者 {best/1e6:.1f} MB")
        if p == "scipy" and rd:
            deps = [d for d in rd if "numpy" in d.lower()]
            print(f"    -> scipy 声明的依赖里含 numpy: {deps}")
        print()
    print(f"cp312/win_amd64 两个 wheel 合计约 {total/1e6:.1f} MB（压缩后）")
    print("参考：当前分发包 409.9 MiB；加便携运行时后约 470 MiB。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
