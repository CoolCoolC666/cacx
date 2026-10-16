# -*- coding: utf-8 -*-
r"""查 networkx 哪个版本起 pagerank 变成"必须有 scipy"。

背景：networkx>=3.6 的 pagerank 直接派发到 _pagerank_scipy，
而 networkx 自己**不声明** numpy/scipy 依赖（它们是可选的），
于是 requirements.txt 只写 networkx>=3.0 就装出一个
"装得上、但 pagerank 一调就 ModuleNotFoundError" 的环境。

两条修法：
  A. 把 numpy + scipy 明写进依赖（+148 MB 未压缩）
  B. 把 networkx 钉在还带纯 Python 回退的版本（不加包）

先查清 B 到底行不行 —— 别凭印象写版本号，那正是这次翻车的根源。
做法是把候选版本下载到临时目录**读源码**，看 pagerank 有没有
"没有 scipy 就走 _pagerank_python" 的分支。只读，不安装。
"""
import io
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

CANDIDATES = ["3.4.2", "3.5", "3.6", "3.6.1", "3.7"]
PR = "pagerank_alg.py"


def fetch(version):
    url = f"https://pypi.org/pypi/networkx/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as r:
        import json
        data = json.loads(r.read().decode("utf-8"))
    for f in data["urls"]:
        if f["packagetype"] == "bdist_wheel" and "win_amd64" in f["filename"]:
            return f["url"], data["info"]["requires_python"]
    for f in data["urls"]:
        if f["packagetype"] == "bdist_wheel":
            return f["url"], data["info"]["requires_python"]
    return None, data["info"]["requires_python"]


def main():
    print(f"{'版本':<8} {'Requires-Python':<18} {'派发':<34} 纯 Python 回退")
    print("-" * 88)
    for v in CANDIDATES:
        try:
            url, rp = fetch(v)
            if not url:
                print(f"{v:<8} {str(rp):<18} （没拿到 wheel）")
                continue
            with urllib.request.urlopen(url, timeout=120) as r:
                blob = r.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                name = next(n for n in z.namelist() if n.endswith(PR))
                src = z.read(name).decode("utf-8")
            # 派发处：pagerank() 里第一次分岔
            dispatch = "?"
            for line in src.splitlines():
                s = line.strip()
                if s.startswith("return _pagerank_"):
                    dispatch = s.replace("return ", "")
                    break
                if s.startswith("return _nx_scipy") or s.startswith("return _pagerank"):
                    dispatch = s.replace("return ", "")
                    break
            has_py = "_pagerank_python" in src
            has_py_def = "def _pagerank_python" in src
            print(f"{v:<8} {str(rp):<18} {dispatch:<34} "
                  f"{'有' if has_py_def else '**无**'}"
                  f"{'' if has_py_def else '   <- 没有纯 Python 实现'}")
            if has_py_def:
                # 回退分支是否真的在 scipy 缺失时可用
                br = [ln.strip() for ln in src.splitlines()
                      if "_pagerank_python" in ln and "def " not in ln]
                if br:
                    print(f"         回退分支: {br[0][:70]}")
        except Exception as e:  # noqa: BLE001
            print(f"{v:<8} 查询失败: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
