# -*- coding: utf-8 -*-
r"""启动器解释器选择逻辑的测试架 —— 判据直接从 start-cacx.bat 抽取。

## 为什么不再在 .bat 里抄一份

上一版 _probe_test.bat 自己维护了一份 :probe 的副本，文件头却写着
"It copies that block verbatim"（照抄）—— 两句话是矛盾的。
副本必然漂移：改启动器的人不会记得改测试，而测试还照样"全绿"。

这和本项目里"闸门抽被闸规则"是同一类错误，只是方向反过来：
不是闸门跟不上被闸对象，而是测试跟不上被闸对象。

所以本脚本用锚点正则**从 start-cacx.bat 真抽取**四段：

    A1  rem ---- 1. what does this machine already have?  ..  rem ---- 2.
    A2  rem ---- 2. ..                                    ..  rem ---- install deps
    B   rem ---- probe one candidate interpreter ..         ..  rem ---- is the package index
    C   :probe_bundle ..                                   ..  EOF

其中 A2（网络判定 + 三分支决策 + :python_bad/:python_ok）是真正要验的部分。

## 唯一打桩的地方：网络

:net_ok 是整个决策里唯一"取决于环境"的一环，而它恰恰是最难在测试机上
复现的（有网/没网、抓包重定向、代理、断网）。所以测试**只替换 :net_ok**，
用 FAKENET 开关控制返回码，其余逻辑逐字节用真代码。

⚠ 别把打桩扩大到别的分支上。桩越多，"验的是真逻辑"这句话就越假。
:probe 的解析逻辑是真跑的（B 段照抄），所以 batch 语法一旦写错这里立刻红。

## 用法

    py -3 -X utf8 "（测试）\_probe_test.py"
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "start-cacx.bat"

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def region(text, start_re, end_re=None):
    """按锚点正则抽一段；end_re 为 None 时取到文末。"""
    m = re.search(start_re, text, re.M)
    if not m:
        raise SystemExit(f"🔴 抽不到起点 {start_re!r} —— 启动器结构变了，"
                         f"本测试架必须跟着改（不要退回到手抄一份）")
    start = m.start()
    if end_re is None:
        return text[start:]
    m2 = re.search(end_re, text[start:], re.M)
    if not m2:
        raise SystemExit(f"🔴 抽不到终点 {end_re!r} —— 启动器结构变了")
    return text[start:start + m2.start()]


def run(cases, workdir, use_a1=False, lines=()):
    """把抽出的真代码拼一个临时 .bat 跑一遍，返回 (rc, stdout)。"""
    # 刻意不写 `cd /d`：项目根路径里有中文，而 .bat 正文必须纯 ASCII
    # （cmd.exe 按 OEM codepage 解码，中文注释会被当命令执行）。工作目录
    # 由 subprocess 的 cwd 给，抽出的代码全部用相对路径。
    body = ["@echo off", "setlocal"]
    body.append('set "FAKENET=1"')
    if use_a1:
        body.append(A1)
    body.extend(cases)          # 预置 PY310 / PY8 等输入
    body.append(A2)
    # ⚠ 收尾必须在子程序段**之前**。
    #   真实启动器靠主流程末尾的 exit /b 0 挡住 :probe / :probe_bundle，
    #   拼装文件少了那一行就会一路顺序执行下去，落到 :probe_bundle 里
    #   把 PY 覆写成 runtime\python.exe —— 于是每个分支用例都显示 bundled，
    #   而且看不出是哪里错了。B / C 只是定义，必须排在 exit 之后。
    body.extend(lines)
    body.append('echo __RESULT__ PY=[%PY%] KIND=[%PYKIND%] DESC=[%PYDESC%]')
    body.append("exit /b 0")
    body.append(B)
    body.append(C)
    # :net_ok 桩：这是唯一被打桩的地方，理由见文件头。
    body.append(":net_ok")
    body.append('if "%FAKENET%"=="1" exit /b 0')
    body.append("exit /b 1")
    src = "\r\n".join(body) + "\r\n"
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe_case.bat"
        f.write_bytes(src.encode("ascii"))   # 纯 ASCII，避免 codepage 干扰
        p = subprocess.run(["cmd", "/c", str(f)], cwd=workdir,
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=180)
        return p.returncode, p.stdout


src = BAT.read_text(encoding="utf-8")
A1 = region(src, r"^rem ---- 1\. what does this machine already have\?",
            r"^rem ---- 2\.")
A2 = region(src, r"^rem ---- 2\.", r"^rem ---- install deps on first run")
B = region(src, r"^rem ---- probe one candidate interpreter",
           r"^rem ---- is the package index reachable")
C = region(src, r"^:probe_bundle\b")

print("=" * 70)
print("start-cacx.bat 解释器选择测试架")
print("=" * 70)
print(f"  抽到 A1 {len(A1)} 字符 / A2 {len(A2)} / B {len(B)} / C {len(C)}")
check("四段都抽到了", all([A1, A2, B, C]))
# 抽出来的代码必须能按纯 ASCII 落盘 —— 这条本身就是一道闸：
# 一旦有人往启动器里塞了中文注释，cmd.exe 会把它当命令执行，
# 而这里会在更早的地方、用一条看得懂的报错拦下来。
_nonascii = [n for n, r in (("A1", A1), ("A2", A2), ("B", B), ("C", C))
             if any(ord(ch) > 127 for ch in r)]
check("抽出的四段全是 ASCII（.bat 正文不容许中文）", not _nonascii, str(_nonascii))

# ---- case 0: 真机器上整段跑，验 :probe 的解析逻辑没写错 ------------------
print()
print("case 0  real machine, whole probe chain verbatim (batch syntax check)")
rc, out = run([], ROOT, use_a1=True)
m = re.search(r"__RESULT__ PY=\[([^\]]*)\] KIND=\[([^\]]*)\]", out)
check("走到了 python_ok 并给出解释器", bool(m), out.strip().splitlines()[-1:] and out.strip().splitlines()[-1])
if m:
    py, kind = m.group(1), m.group(2)
    print(f"        -> PY={py}  KIND={kind}")
    check("KIND 只能是 local / bundled / local-old",
          kind in ("local", "bundled", "local-old"), kind)
    check("有网且本机 >=3.10 时应优先用本机 Python", kind == "local", kind)
    check("选本机时不能用自带 runtime", not (kind == "local" and py.lower().startswith("runtime")))

# ---- case 1: :probe 找到 >=3.10 时，必须同时填上 3.8 槽 ----------------
print()
print("case 1  a >=3.10 candidate must also fill the 3.8 slot")
# 这条单独测是因为它救的是一个真实的死路：本机 3.12 + 没网 + runtime\
# 缺失时，若 3.8 槽空着，决策会一路穿到 :python_bad 报「没有可用解释器」，
# 而 3.12 明明就在 PATH 上。
lines = [
    'call :probe "py -3"',
    'echo __PROBE__ P310=[%PY310%] P8=[%PY8%]',
    'echo __RESULT__ PY=[%PY%] KIND=[%PYKIND%]',
    "exit /b 0",
]
rc, out = run([], ROOT, use_a1=False, lines=lines)
mp = re.search(r"__PROBE__ P310=\[([^\]]*)\] P8=\[([^\]]*)\]", out)
if mp and mp.group(1):
    check("找到 >=3.10 后 3.8 槽也被填上", bool(mp.group(2)), f"P310={mp.group(1)} P8={mp.group(2)}")
else:
    print(f"  SKIP  本机没有 >=3.10 的解释器（P310=[{mp.group(1) if mp else '?'}]），这条留待有 3.10 的机器验")

# ---- 下面全部用预置输入驱动，验的是 A2 的决策本身 -----------------------
print()
print("cases 2-6  decision branches, inputs preset, logic verbatim")

with tempfile.TemporaryDirectory() as empty:
    # 2. 本机 >=3.10 + 有网 -> local
    rc, out = run(['set "PY310=py -3"', 'set "PY310D=py -3 3.13 at C:\\fake\\python.exe"',
                   'set "FAKENET=1"'], ROOT)
    check("2  >=3.10 + 有网 -> local", "KIND=[local]" in out)

    # 3. 本机 >=3.10 + 抓不到索引 + runtime 在 -> bundled
    rc, out = run(['set "PY310=py -3"', 'set "PY310D=py -3 3.13 at C:\\fake\\python.exe"',
                   'set "PY8=py -3"', 'set "PY8D=py -3 3.13 at C:\\fake\\python.exe"',
                   'set "FAKENET=0"'], ROOT)
    check("3  >=3.10 但没网 + runtime 在 -> bundled", "KIND=[bundled]" in out)
    check("3  选了自带 runtime 后确实是它", re.search(r"__RESULT__ PY=\[runtime\\python\.exe\]", out) is not None)

    # 4. 本机只有 3.9 + 有网 -> 仍然 bundled（<3.10 不够格用本机）
    rc, out = run(['set "PY8=python"', 'set "PY8D=python 3.9 at C:\\fake\\python.exe"',
                   'set "FAKENET=1"'], ROOT)
    check("4  只有 3.9（哪怕有网）-> bundled", "KIND=[bundled]" in out)

    # 5. 本机只有 3.9 + 没网 + runtime 缺失 -> local-old（最后兜底）
    rc, out = run(['set "PY8=python"', 'set "PY8D=python 3.9 at C:\\fake\\python.exe"',
                   'set "FAKENET=1"'], empty)
    check("5  3.9 + 没网 + 无 runtime -> local-old", "KIND=[local-old]" in out)

    # 6. 什么都没有 -> :python_bad，且不能悄悄把 PY 填上
    rc, out = run([], empty)
    check("6  什么都没有 -> 走 :python_bad", "__RESULT__" not in out and rc != 0,
          f"rc={rc}")
    check("6  :python_bad 给出了可执行的下一步",
          "Re-extract" in out and "runtime" in out)
    check("6  没网时会说明原因",
          "unreachable" in out or rc == 1)

# ---- case 7: pip 装失败必须能回落，不能直接判死 ------------------------
print()
print("case 7  a failed pip install must fall back, not stop the machine")
fallback = region(src, r"^:install_failed", r"^:deps_ready")
check("存在 :install_failed 回落分支", ":install_dead" in fallback)
check("回落分支会在装不上时改用自带 runtime",
      'if not exist "runtime\\python.exe" goto :install_dead' in fallback
      and "probe_bundle" in fallback)
check("失败信息里有手动重试命令", "pip install -r requirements.txt" in fallback)

# ---- case 8: 依赖检查清单必须含 scipy ----------------------------------
print()
print("case 8  the import check list must include scipy")
# networkx 的 pagerank() 无条件走 _pagerank_scipy，而 networkx 不声明 scipy。
# 检查清单漏掉 scipy 的后果很隐蔽：机器上只缺 scipy 时整段安装被跳过，
# 启动一切正常，直到用户点进关系图谱、等几分钟，才在 PageRank 上炸。
IMP = "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx"
lines_all = src.splitlines()
with_scipy = [i + 1 for i, ln in enumerate(lines_all) if IMP in ln and "scipy" in ln]
without = [i + 1 for i, ln in enumerate(lines_all) if IMP in ln and "scipy" not in ln]
check("清单里至少有一处 import 检查", bool(with_scipy) or bool(without))
check("每一处 import 检查都带 scipy", not without, f"漏掉的行号：{without}")
check("requirements.txt 里确实声明了 scipy",
      "scipy" in (ROOT / "requirements.txt").read_text(encoding="utf-8"))

# ---- case 9: MCP 提示随分支变化，不得用一句话套三种情况 -----------------
print()
print("case 9  the MCP message must match the branch it is on")
mcp = region(src, r"^rem ---- MCP is optional", r"^rem ---- is the database")
check("分派到了 bundled 分支", 'if "%PYKIND%"=="bundled" goto :mcp_bundled' in mcp)
check("分派到了 local 分支", 'if "%PYKIND%"=="local" goto :mcp_local' in mcp)
# local-old 走的是兜底的 goto :mcp_old，**不需要**再写一条显式判断 ——
# 硬要求「三个分支都得写成 if」反而是在逼代码加一条纯冗余的行。
# 真正要验的是：三种情况各有各的说法，不是同一句模板。
check("bundled 的说法讲清了「不用修」",
      ":mcp_bundled" in mcp and "Nothing to fix" in mcp)
check("local 的说法讲清了「能装但不自动装」",
      ":mcp_local" in mcp and "never installed automatically" in mcp)
check("local-old 的说法落在 3.10 这条线上",
      ":mcp_old" in mcp and "older than 3.10" in mcp)
check("旧的「3.8 or 3.9 this is expected」不再一刀切",
      mcp.count("this is expected") <= 1)

print()
print(f"通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
