#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸：项目根所有 .bat 的正文必须是纯 ASCII。

为什么需要这道闸（不是"顺便加的检查"）：

    2026-10-13 实测事故。cmd.exe 用当前 OEM 代码页（中文机器 = GBK）解码 .bat。
    正文里出现任何一个非 ASCII 字节，整份脚本的解析就会错位——不是"那行显示成
    乱码"，而是后面的行会被拦腰截断，片段被当成命令去执行：

        'tr' is not recognized      <- "Add Python to PATH" 的尾巴
        '..' is not recognized
        n is not recognized

    结果是 python -m cacx serve 根本没执行，前端"无效"；run-demo.bat 同理，
    双击后窗口一闪就没，人根本看不到它在跑。

    三种常见"修法"实测都不成立，别再试了：
        UTF-8 无 BOM + chcp 65001   -> 炸（启动cacx.bat 原状）
        UTF-8 带 BOM               -> 炸得更狠，@echo off 都被 BOM 破坏
        纯 ASCII                    -> 通

    讽刺的是 run-demo.bat 的第 4-7 行白纸黑字写着这条规则，第 48 行就违反了它
    （截图目录名带全角括号）。所以这条规则靠人记不住，必须机器拦。

文件名可以带中文（那是给人双击的），正文不行。

自检：--inject 会在内存里造一份带中文的假 bat，确认本闸真的会响（反向验证）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些是"给人双击看的中文名转发壳"，正文同样必须纯 ASCII，一并检查。
BATS = (
    "dev-clean.bat",
    "run-demo.bat",
    "start-cacx.bat",
    "sync-latest.bat",
    "启动cacx.bat",
    "同步最新文章.bat",
)


def offenders(path: Path) -> list[tuple[int, int]]:
    """返回 [(字节下标, 字节值), ...]，空列表 = 纯 ASCII。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return [(i, b) for i, b in enumerate(raw) if b > 127]


def describe(path: Path, bad: list[tuple[int, int]]) -> str:
    """把违规字节定位到行号，并打印那一行的内容（按 UTF-8 解，乱码也认得出来）。"""
    raw = path.read_bytes()
    lines: list[str] = []
    # 行号 = 违规字节之前有多少个换行
    bad_set = {i for i, _ in bad}
    start = 0
    lineno = 0
    for end in range(len(raw)):
        if raw[end] == 0x0A:
            lineno += 1
            if any(start <= i < end for i in bad_set):
                lines.append((lineno, raw[start:end].decode("utf-8", "replace")))
            start = end + 1
    if start < len(raw) and any(start <= i < len(raw) for i in bad_set):
        lines.append((lineno + 1, raw[start:].decode("utf-8", "replace")))

    out = [f"    {path.name}  非 ASCII 字节 {len(bad)} 个"]
    for n, text in lines:
        out.append(f"      L{n}: {text.strip()}")
    return "\n".join(out)


def self_test() -> int:
    """反向验证：故意造一份含中文的 bat 文本，确认 offenders 能抓住。"""
    cases = [
        ("pure ascii", b"@echo off\r\necho hello\r\n", 0),
        ("one cjk char", "@echo off\r\necho 截图目录\r\n".encode("utf-8"), 1),
        ("fullwidth paren", "@echo off\r\necho in: （测试）\r\n".encode("utf-8"), 1),
        ("utf8 bom only", b"\xef\xbb\xbf@echo off\r\n", 1),
    ]
    bad_count = 0
    for name, data, expect_raised in cases:
        tmp = ROOT / f"_tmp_batenc_{abs(hash(name))}.bin"
        tmp.write_bytes(data)
        try:
            got = len(offenders(tmp))
        finally:
            tmp.unlink(missing_ok=True)
        ok = (got > 0) == bool(expect_raised)
        flag = "PASS" if ok else "FAIL"
        if not ok:
            bad_count += 1
        print(f"  [{flag}] {name:<18} 检出 {got} 个非 ASCII 字节（期望{'检出' if expect_raised else '不检出'}）")
    if bad_count:
        print(f"\n  自检失败：{bad_count} 个用例没检出 —— 这道闸本身坏了，不能用。")
        return 1
    print("  自检通过：探测器能抓到非 ASCII 字节。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", action="store_true", help="只跑反向验证自检")
    args = ap.parse_args()

    print("=" * 70)
    print("闸：.bat 正文纯 ASCII 检查")
    print("=" * 70)

    if args.inject:
        return self_test()

    problems = []
    for name in BATS:
        p = ROOT / name
        if not p.exists():
            print(f"  [SKIP] {name} 不存在")
            continue
        bad = offenders(p)
        if bad:
            problems.append(describe(p, bad))
        else:
            print(f"  [OK]   {name}  纯 ASCII")

    if problems:
        print("\n发现非 ASCII 正文：\n")
        print("\n".join(problems))
        print(
            "\n修法：把中文从 .bat 正文里拿掉。\n"
            "  - 文件名可以留中文（那是给人双击的），正文必须纯 ASCII。\n"
            "  - 别指望 chcp 65001，也别指望加 UTF-8 BOM，实测都炸。\n"
            "  - 要输出中文，让 Python（autodemo.py / cacx）去 print，别让 cmd 去 echo。"
        )
        return 1

    print("\n全部纯 ASCII，合规。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
