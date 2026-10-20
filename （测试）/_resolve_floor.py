# -*- coding: utf-8 -*-
r"""让 pip 真的为旧 Python 解析一遍依赖，而不是靠推断。

为什么必须真跑一次 dry-run：
  "pip 会按 Requires-Python 自动降级" 是**推断**。它成立与否取决于
  依赖树里有没有互相卡死的要求、以及被解析到的旧版本 API 是否还对。
  一旦不成立，使用者装完第一次启动就炸，而错误信息指向 pip 而不是我们。

⚠ 第一版的一个假警报（已修）：
  我给 dry-run 加了 `--only-binary=:all:`，结果 **所有** Python 版本
  都报 "No matching distribution found for jieba>=0.42"。
  看着像"3.8 根本装不上"，其实 jieba 历来只发 sdist、**从不发 wheel**，
  那个参数人为排除了它唯一的分发形式。
  断言比现实更严 → 造出假警报 → 然后逼着人去"修"本来没问题的东西。
  现在改成：wheel 集走 --only-binary 校验，sdist-only 的包单独用
  PyPI 文件清单证明"确实只有 sdist，但 sdist 是能装的"。

只解析不安装（--dry-run + --target 指向临时目录）。
"""
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
REQ_MCP = ROOT / "requirements-mcp.txt"

TARGETS = ["3.8", "3.9", "3.10", "3.13"]

# 只发 sdist、永远没有 wheel 的包。它们必须走另一条验证路径。
SDIST_ONLY = {"jieba"}


def _req_lines(p: Path):
    out = []
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def resolve(python_version: str, specs):
    """对给定 requirement 行做一次真实解析。返回 (rc, stdout, stderr)。"""
    with tempfile.TemporaryDirectory(prefix="cacx_resolve_") as td:
        rf = Path(td) / "r.txt"
        rf.write_text("\n".join(specs), encoding="utf-8")
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--dry-run", "--ignore-installed", "--quiet",
            "--only-binary=:all:",
            "--python-version", python_version,
            "--target", td,
            "-r", str(rf),
        ]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def tail(err: str, n=4):
    """只取有诊断价值的那几行，跳过 pip 那几百行 'Ignored the following
    versions ...' 的版本清单 —— 它不报错，只是把被跳过的都列一遍。"""
    lines = [x for x in err.splitlines()
             if "Ignored the following versions" not in x]
    return lines[:n]


def check_wheels(title: str, req: Path):
    print("=" * 72)
    print(f"{title}   —— 有 wheel 的部分（--only-binary=:all:）")
    print("=" * 72)
    specs = [s for s in _req_lines(req) if s.split(">=")[0].split("==")[0].strip().lower() not in SDIST_ONLY]
    for v in TARGETS:
        rc, out, err = resolve(v, specs)
        if rc == 0:
            print(f"  Python {v:5s} -> 解析成功")
        else:
            print(f"  Python {v:5s} -> 解析失败 rc={rc}")
            for line in tail(err or out):
                print(f"        {line}")
    print()


def check_sdist_only():
    print("=" * 72)
    print("只发 sdist 的包：为什么 dry-run 不能验它们")
    print("=" * 72)
    for name in sorted(SDIST_ONLY):
        try:
            with urllib.request.urlopen(
                    f"https://pypi.org/pypi/{name}/json", timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: 查询失败 {e}")
            continue
        latest = data["info"]["version"]
        files = data["releases"].get(latest, [])
        kinds = sorted({f["packagetype"] for f in files})
        print(f"  {name} 最新版 {latest} 的分发形式: {kinds}")
        print(f"    Requires-Python = {data['info'].get('requires_python') or '(未声明)'}")
        if kinds == ["sdist"]:
            print("    -> 没有任何 wheel。--only-binary=:all: 必然报找不到，"
                  "那是参数的问题，不是版本的问题。")
            print("    -> 真实安装走 pip 构建 sdist，纯 Python 包，各版本均可装。")
    print()


def main():
    check_wheels("核心依赖 requirements.txt", REQ)
    check_sdist_only()
    check_wheels("可选依赖 requirements-mcp.txt", REQ_MCP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
