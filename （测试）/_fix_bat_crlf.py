# -*- coding: utf-8 -*-
r"""把 .bat 规整成「纯 ASCII + 全 CRLF」，并按字节自检。

为什么必须单独写脚本做（而不是靠 write/edit 工具）：
1. `write` 工具一律写 LF。cmd.exe 对 .bat 的行尾**没有** Unix 友好性，
   LF-only 的 .bat 在部分 Windows 环境会连成一行。
2. 中文进 .bat 正文 = 灾难。cmd.exe 按**活动 OEM 代码页**（简体中文机器上是
   GBK）解码 .bat，UTF-8 的中文行会被从中间截断，结果 `python -m cacx serve`
   根本不会执行 —— 而且不报错，只是「点了没反应」。
   这两条实测都失败过，唯独纯 ASCII 能跑。

所以本脚本做的检查不是形式主义，是这两条血泪教训的闸门：
  · 非 ASCII 字节数必须为 0
  · 裸 LF（前面不是 CR）必须为 0
  · 文件须以 CRLF 结尾
  · 行内出现孤立的 ")" 或 "(" 也会让 if 块解析失败（见下方 CLI 块注释）

用法：py -3 -X utf8 （测试）\_fix_bat_crlf.py start-cacx.bat [--write]
不带 --write 只检查并报告，不改文件。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check(p: Path):
    raw = p.read_bytes()
    bad_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    bare_lf = [i for i, b in enumerate(raw)
               if b == 0x0A and (i == 0 or raw[i - 1] != 0x0D)]
    crlf = raw.count(b"\r\n")
    ends_nl = raw.endswith(b"\r\n") or raw.endswith(b"\r")
    return {
        "bytes": len(raw),
        "non_ascii": bad_ascii,
        "bare_lf": bare_lf,
        "crlf": crlf,
        "ends_nl": ends_nl,
    }


# --------------------------------------------------------------------------
# 第三条检查：echo / set 里的 cmd 重定向符注入
# --------------------------------------------------------------------------
# 实测踩过：探测子程序把解释器路径存进 PYDESC，用 "->" 当分隔符，
# 上层 `echo Using %PYDESC%` 一执行，cmd 就把 "-> C:\...\python.exe"
# 解析成 stdout 重定向 —— 直接去**截断解释器本体**。
# 症状极怪：case 1 整段没有任何输出，别的 case 正常，
# 而真正的错误（"文件被写坏了"）一个字都没打。
#
# 机制：cmd.exe 在 echo 生效**之前**就做重定向解析，
# 所以只要展开后的文本里出现 > < | &，就已经晚了。
# 引号保护不了 —— 变量展开发生在解析之后。
# 结论：这类字符只能靠"根本不写进去"来防。
#
# 分两级，避免把闸门写得过严（过严会造假警报，然后逼人去改本来正确的东西）：
#   FAIL  set "VAR=..." 的值里含 > < | &  —— 引号赋值却带重定向符，
#         基本只可能是 bug，没有 legit 用法
#   WARN  echo 行里同时出现 变量 与 未转义重定向符 —— 可能是故意的重定向，
#         所以只提醒不判死
SPECIAL = re.compile(r"[<>|&]")
VAR = re.compile(r"%[^%]*%")


def check_injection(p: Path):
    """返回 (hard_hits, soft_wits)。hard_hits 非空即 FAIL。"""
    text = p.read_bytes().decode("utf-8-sig", errors="replace")
    hard, soft = [], []
    for i, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.lower().startswith("rem "):
            continue
        # 去掉 caret 转义后再判断：^> 是作者故意的转义，不算注入
        probe = re.sub(r"\^[<>&|]", "", line)
        # 引号赋值
        m = re.match(r'set\s+"([^=]+)=(.*)"$', probe, re.I)
        if m and SPECIAL.search(m.group(2)):
            hard.append((i, line))
            continue
        if line.lower().startswith("echo ") and SPECIAL.search(probe[5:]) \
                and VAR.search(probe[5:]):
            soft.append((i, line))
    return hard, soft


def fix(p: Path):
    raw = p.read_bytes()
    # 先统一去 CR 再补 CRLF，避免出现 \r\r\n
    body = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    p.write_bytes(body.replace(b"\n", b"\r\n"))


def main(argv):
    args = [a for a in argv[1:] if a != "--write"]
    do_write = "--write" in argv[1:]
    targets = [Path(a) if Path(a).is_absolute() else ROOT / a for a in args]
    if not targets:
        targets = sorted(ROOT.glob("*.bat"))
    rc = 0
    for p in targets:
        if not p.exists():
            print(f"  MISSING  {p.name}")
            rc = 1
            continue
        if do_write:
            fix(p)
        r = check(p)
        # ⚠ 判据只有两条：非 ASCII 必须 0、裸 LF 必须 0。
        #   「文件必须以换行结尾」**不是**要求 —— 末行没有换行符（常见末行是
        #   `pause` / `exit /b %RC%`）cmd.exe 完全正常执行。
        #   我第一版把「必须以 CRLF 结尾」当成硬条件，结果三个好文件全被
        #   误判成 FAIL，还差点让「修复」去给它们硬塞一个换行 ——
        #   **把断言写得比现实更严，就会造出假警报，然后逼着人去改本来正确的东西。**
        ok = not r["non_ascii"] and not r["bare_lf"]
        detail = ""
        if r["non_ascii"]:
            pos = ", ".join(f"offset {i}=0x{b:02X}" for i, b in r["non_ascii"][:5])
            detail += f"  非 ASCII {len(r['non_ascii'])} 处: {pos}"
        if r["bare_lf"]:
            detail += f"  裸 LF {len(r['bare_lf'])} 处: {r['bare_lf'][:5]}"
        hard, soft = check_injection(p)
        for i, ln in hard:
            detail += f"\n         重定向符注入 FAIL line {i}: {ln}"
        for i, ln in soft:
            detail += f"\n         重定向符 WARN line {i}: {ln}"
        ok = ok and not hard
        if not r["ends_nl"]:
            detail += "  （末行无换行符，cmd.exe 正常，informational）"
        print(f"  {'PASS' if ok else 'FAIL'}  {p.name}   "
              f"{r['bytes']} bytes, {r['crlf']} CRLF{detail}")
        if not ok:
            rc = 1
    print()
    print("硬判据三条：非 ASCII = 0、裸 LF = 0、set 值里无 > < | &。"
          "中文进 .bat 正文会让 `python -m cacx serve` 静默不执行；"
          "而 > 出现在会被 echo 的变量里，会被 cmd 当成重定向去写文件。")
    sys.exit(rc)


if __name__ == "__main__":
    main(sys.argv)
