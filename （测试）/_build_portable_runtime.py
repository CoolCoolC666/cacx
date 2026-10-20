# -*- coding: utf-8 -*-
r"""构建随包携带的便携 Python 运行时（方案 A）。

目标：使用者**什么都不用装**，解压后双击就能跑 —— 不需要管理员权限、
不需要改 PATH、不需要联网、不需要"安装"这个动作。

四个已知坑，处理方式都在下面注释里，不处理就是埋雷：

1) embeddable 版默认在 python312._pth 里写死路径，且注释掉 import site。
   不改的话 site-packages 根本不在 sys.path 上，import 一律 ModuleNotFoundError。
2) embeddable 版我没有"确认"不带证书根 —— 我一开始**以为**它不带，还把
   「同步最新文章会静默失效」写进了注释里。实测推翻了：Windows 的
   embeddable Python 会枚举 Windows 根证书存储（实测 91 张），
   requests 更是自带 certifi 根本不查系统证书库。
   cacert.pem 仍随包带着，但定位是**冗余保险**，注释已按实测改写。
   **别把保险说成修复** —— 假的因果比没有注释更贵。
3) requirements.txt 里只有 7 个**顶层**包，pyvis 会拖进 jinja2 等传递依赖。
   直接按文件装会漏，所以这里让 pip 自己解析（--target 会把传递依赖一并装上）。
4) 便携运行时里没有 pip，"想启用 MCP 就装一下"那句话就会变成一句废话。
   所以把 pip 也装进去（+10.6 MB），让启动器打印的命令真的能执行。
5) 跨版本装二进制依赖必须显式指定 --python-version 3.12 --platform win_amd64，
   否则 pip 会按构建机的 3.13 去挑轮子，装进来的 .pyd 在 3.12 里根本
   加载不了 —— 报错还不带任何 ABI 线索。详见下面第 3 步的注释。

复用：脚本可重复跑（幂等 —— 每次从干净目录重建），产物不入库，
只有本脚本入库。原则同 _export_pack.py：能重建交付物的脚本必须入库。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
REQ = ROOT / "requirements.txt"

PY_VER = "3.12.10"
PY_TAG = "312"
EMBED_URL = (f"https://www.python.org/ftp/python/{PY_VER}/"
             f"python-{PY_VER}-embed-amd64.zip")

# embeddable 的 _pth 内容。顺序即 sys.path 顺序：
#   python312.zip  —— 标准库（官方就是压缩包形式）
#   .              —— 运行时的 DLL 与 exe 所在目录（runtime\）
#   Lib\site-packages —— 我们 --target 装进去的依赖
#   ..             —— **项目根**，cacx\ 包在这里！
#   import site    —— **必须打开**，否则 .pth 文件不生效、site 不注册
#
# ⚠ `..` 这一行是踩出来的，不加会报 `No module named cacx`：
#   带 ._pth 的 Python 处于**隔离模式** —— sys.path 完全由 _pth 决定，
#   既不自动包含当前工作目录，PYTHONPATH 也被无视。
#   所以从项目根跑 `python -m cacx serve` 时，cacx\ 根本不在 sys.path 上。
#   而且它**只影响启动器那一条命令**，第三方依赖照常 import 得通 ——
#   我第一版的自检只 import 了 fastapi/requests 之类，全部 PASS，
#   一路绿灯直到真去起服务才炸。静态检查 + 依赖 import 都发现不了这一条。
#   `..` 是相对 python.exe 所在目录解析的，即 runtime\ 的上一层 = 项目根。
PTH = (
    "python{tag}.zip\n"
    ".\n"
    "Lib\\site-packages\n"
    "..\n"
    "import site\n"
).format(tag=PY_TAG)

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def recycle_dir(p: Path) -> bool:
    """把旧运行时目录送进系统回收站。

    ⚠ 为什么不用 shutil.rmtree：
      1. **违反本项目口径** —— 一切删除走回收站，可恢复。rmtree 是永久删除。
      2. 报错毫无信息量。上一次它炸在
         `PermissionError: ... cd.cp312-win_amd64.pyd`，
         看着像文件损坏，实际是**上一次用便携运行时起着的服务还开着**，
         .pyd 被进程加载着锁住了。从 traceback 里根本看不出该去关服务。
      3. 159 MB / 10872 个文件，rmtree 一次要很久，中途失败会留下半残目录。
    所以改成走回收站：失败时明确告诉使用者「先把服务关掉」。
    """
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
          "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
          f"'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    return r.returncode == 0 and not p.exists()


def main():
    print("=" * 68)
    print(f"构建便携运行时  Python {PY_VER} embed-amd64")
    print("=" * 68)

    # ---------- 0 干净重建 ----------
    if RUNTIME.exists():
        if not recycle_dir(RUNTIME):
            print(f"  🔴 旧的 runtime\\ 没能删除（多半是上一次用它起着的服务还开着，"
                  f".pyd 被锁住了）。")
            print(f"     请先关掉 cacx 窗口 / 结束占用 8770 的 python 进程，再重跑。")
            print(f"     目录：{RUNTIME}")
            return 2
        print(f"  旧的 runtime\\ 已送回收站：{RUNTIME.name}")
    RUNTIME.mkdir(parents=True)

    # ---------- 1 下载并解压 ----------
    with tempfile.TemporaryDirectory(prefix="cacx_embed_") as td:
        zp = Path(td) / "embed.zip"
        print(f"  下载 {EMBED_URL}")
        with urllib.request.urlopen(EMBED_URL, timeout=300) as r, open(zp, "wb") as f:
            shutil.copyfileobj(r, f)
        print(f"  {zp.stat().st_size/1e6:.1f} MB")
        with zipfile.ZipFile(zp) as z:
            z.extractall(RUNTIME)
    check("python.exe 已就位", (RUNTIME / "python.exe").exists())

    # ---------- 2 修 _pth ----------
    # 见文件头坑 1：不改这四行，下面装什么都没用。
    (RUNTIME / f"python{PY_TAG}._pth").write_text(PTH, encoding="utf-8")
    txt = (RUNTIME / f"python{PY_TAG}._pth").read_text(encoding="utf-8")
    check("_pth 打开了 import site", "import site" in txt)
    check("_pth 含 Lib\\site-packages", "Lib\\site-packages" in txt)

    # ---------- 3 装依赖 ----------
    # ⚠ 必须显式告诉 pip「目标平台是 win_amd64 / Python 3.12」。
    #   否则 pip 用**当前解释器**（构建机上是 3.13）去挑轮子，
    #   装进来的是 cp313 的 .pyd，塞进 3.12 运行时里 import 就炸：
    #     ImportError: cannot import name 'ArgsKwargs' from
    #     'pydantic_core._pydantic_core' (unknown location)
    #   这个错**极难定位** —— 报错里没有 "abi" / "cp313" 字样，
    #   看着像 pydantic 装坏了或版本不兼容，实际是二进制标签对不上。
    #   所以除了真 import 一遍，下面还加一条按文件名查 cp313 的闸，
    #   让下次同类问题一眼看出是哪来的。
    sp = RUNTIME / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)

    # ⚠ jieba 历来**只发 sdist、从不发 wheel**，加 --only-binary=:all: 会
    #   直接报"找不到该版本" —— 那是参数的问题不是它不兼容。
    #   所以把它摘出去，第二阶段单独装（纯 Python、零依赖、无二进制）。
    SDIST_ONLY = {"jieba"}
    req_lines = [ln.strip() for ln in REQ.read_text(encoding="utf-8-sig").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
    binary_reqs = [ln for ln in req_lines
                   if ln.split(">=")[0].split("==")[0].strip().lower() not in SDIST_ONLY]
    sdist_reqs = [ln for ln in req_lines if ln not in binary_reqs]

    def run_pip(extra, label):
        cmd = [sys.executable, "-m", "pip", "install",
               "--target", str(sp), "--upgrade"] + extra
        print(f"  [{label}] pip install --target ... {' '.join(extra)}")
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            print(p.stdout[-2500:])
            print(p.stderr[-2500:])
        return p.returncode

    rc1 = run_pip(["--only-binary=:all:", "--python-version", "3.12",
                   "--platform", "win_amd64", *binary_reqs, "pip"],
                  "binary for cp312")
    check("二进制依赖安装成功（cp312/win_amd64）", rc1 == 0, f"rc={rc1}")

    if sdist_reqs:
        # --no-deps：jieba 没有依赖，且这样装出来的就是纯 .py，
        # 天然与解释器版本无关，不需要再指定 python-version。
        rc2 = run_pip(["--no-deps", *sdist_reqs], "sdist (no-deps)")
        check(f"纯源码依赖安装成功 {sdist_reqs}", rc2 == 0, f"rc={rc2}")

    # ABI 闸：把"装错二进制"这个最贵的错提前暴露在名字上，
    # 而不是等它变成一句没有线索的 ImportError。
    stray = sorted({f.name for f in sp.rglob("*")
                    if f.is_file() and "cp313" in f.name})
    check("site-packages 里没有 cp313 的二进制", not stray,
          f"发现 {len(stray)} 个：{stray[:4]}")

    # 反向确认：确实装了 cp312
    got312 = sorted({f.name for f in sp.rglob("*.pyd") if "cp312" in f.name})
    check("确实装的是 cp312 二进制", bool(got312), f"{len(got312)} 个 .pyd")

    # ---------- 4 证书（冗余保险，不是修复）----------
    # ⚠ 我第一版在这里写的是「embeddable 不带证书根 → https 会挂」。
    #   **那是错的**，实测推翻了它：
    #     · Windows 的 embeddable Python 在没有 CA 文件时会去枚举
    #       **Windows 根证书存储**（ssl -> EnumCertificates），
    #       实测 create_default_context() 拿到 91 张根证书；
    #     · requests 压根不看系统证书库，它用 site-packages 里的 certifi。
    #   所以 cacert.pem 现在只是**冗余保险**（机器读不到 Windows 根存储时兜底），
    #   不是「不搬就挂」的必需品。注释如实写，别把保险说成修复。
    cacert = sp / "certifi" / "cacert.pem"
    check("certifi 的 cacert.pem 存在", cacert.exists())
    if cacert.exists():
        shutil.copy2(cacert, RUNTIME / "cacert.pem")

    # ---------- 5 体积与文件数 ----------
    n = sum(1 for f in RUNTIME.rglob("*") if f.is_file())
    total = sum(f.stat().st_size for f in RUNTIME.rglob("*") if f.is_file())
    print()
    print(f"  运行时 {total/1e6:.1f} MB / {n} 个文件")

    # ---------- 6 真跑一次 ----------
    # 静态检查过了不算数，必须让便携解释器自己 import 一遍。
    exe = str(RUNTIME / "python.exe")
    probe = ("import fastapi, uvicorn, requests, bs4, pyvis, jieba, "
             "networkx, sqlite3, ssl; print('IMPORTS-OK')")
    r = subprocess.run([exe, "-c", probe], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("便携解释器能 import 全部核心依赖",
          "IMPORTS-OK" in (r.stdout or ""),
          (r.stderr or "").strip().splitlines()[-1] if r.returncode else "")

    # 证书是否真的生效 —— 用一个不依赖外网的判断：
    # ssl.create_default_context() 会去读系统根证书，embeddable 没有就会炸。
    r2 = subprocess.run(
        [exe, "-c",
         "import os,ssl;"
         "os.environ.setdefault('SSL_CERT_FILE', r'%s');"
         "c=ssl.create_default_context();print('SSL-OK', len(c.get_ca_certs()))"
         % (RUNTIME / "cacert.pem")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("SSL 上下文能建立（证书根可用）",
          "SSL-OK" in (r2.stdout or ""),
          (r2.stderr or "").strip().splitlines()[-1] if r2.returncode else "")

    # 顺带看看 mcp 是不是"恰好没装"（便携运行时里应当没有）
    r3 = subprocess.run([exe, "-c", "import mcp"],
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check("mcp 未随包安装（可选件，应缺）", r3.returncode != 0)

    # ⚠ 必须从**项目根**试 import cacx —— 上一版只试了第三方依赖，
    #   全绿，然后启动器一跑就是 `No module named cacx`。
    #   依赖能不能 import 和**自己的包能不能被找到**是两回事，
    #   隔离模式下后者要单独验（见 _pth 里 `..` 那段注释）。
    r4 = subprocess.run([exe, "-c", "import cacx, cacx.web.app; print('CACX-OK')"],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check("从项目根能 import cacx（_pth 的 .. 生效）",
          "CACX-OK" in (r4.stdout or ""),
          (r4.stderr or "").strip().splitlines()[-1] if r4.returncode else "")

    # 再进一步：CLI 入口能不能列出命令（等价于 `python -m cacx --help`）
    r5 = subprocess.run([exe, "-m", "cacx", "--help"], cwd=str(ROOT),
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check("便携解释器能跑 `python -m cacx`", r5.returncode == 0,
          (r5.stderr or "").strip().splitlines()[-1] if r5.returncode else "")

    # ⚠⚠ 最关键的一道：**真调一次 analyze()**，不是 import 一下。
    # networkx>=3.6 的 pagerank() 无条件要 scipy，而 networkx 不声明它。
    # `import cacx` 和 `import networkx` 永远成功，只有 nx.pagerank(G)
    # 才会在缺 scipy 时炸 —— 断言必须打在会被用到的那一刻。
    # 用 6 节点小图，毫秒级；等全图冷启动报同一个错要 348 秒。
    r6 = subprocess.run([exe, str(ROOT / "（测试）" / "_smoke_analyze.py")],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    tail = (r6.stdout or "").strip().splitlines()
    check("小图 analyze() 出数（scipy 到位）",
          "SMOKE-OK" in (r6.stdout or ""),
          tail[-1] if tail else (r6.stderr or "").strip().splitlines()[-1:])

    print()
    print(f"通过 {ok} 项，失败 {fail} 项")
    print(f"运行时目录：{RUNTIME}")
    if fail:
        print("\n🔴 运行时不可用，不要打包。先修上面的 FAIL。")
        return 1
    print("\n下一步：改 start-cacx.bat 让它优先用 runtime\\python.exe，"
          "并设 SSL_CERT_FILE；然后 _repack.py 会自动把它带进分发包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
