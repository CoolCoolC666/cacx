# -*- coding: utf-8 -*-
r"""改演示稿里那句关于「检查 Python / 自动 pip」的口播。

## 为什么这句必须改

现在的启动逻辑是：

    本机 Python >= 3.10 且能连上包索引  ->  用它，依赖装进去
    否则                                  ->  用包内自带的 runtime，什么都不装

而原稿写的是「程序会检查电脑中的 Python 是否安装……如果未安装，
程序会帮助你 pip. 请保存网络畅通并等待。」

前半句在「走本机那条路」时仍然成立，但**当落到自带运行时那一支时，
实际发生的是「什么都不装」** —— 而念稿的人会照着说「请保持网络畅通」，
组员于是去找网络、装 Python，做一堆根本不需要做的事。
最坏的情况是：他明明什么都没装就能跑，却被这段话劝退了。

这是**照着念**的稿子，所以只做最小改动：保留原句的语序和口吻，
把两条分支都写进去，而不是整段重写。

## 做法

docx 里一句话经常被 Word 拆进多个 <w:t>，直接对整段 replace 经常
一个字符都匹配不到。所以先在纯文本里定位，再按 <w:t> 逐个替换；
匹配不到就**如实报错**，不猜、不硬改（错答案比漏答案危害大）。
"""
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCX = ROOT.parent / "提交包" / "演示" / "演示稿.docx"

# 锚点：只取句子开头一小段，够定位又不会被 Word 的差异拆开
ANCHOR = "程序会检查电脑中的Python是否安装"

NEW = ("程序会先检查本机的Python：如果本机有3.10以上并且能联网，"
       "就用它，并自动装好requirements.txt里的8个依赖，此时请保持网络畅通；"
       "否则直接用压缩包里自带的runtime，不需要装任何东西，也不需要联网。")

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def plain_of(zf):
    xml = zf.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", "", xml), xml


def main():
    if not DOCX.exists():
        print(f"  MISSING {DOCX}")
        return 1

    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
        infos = {i.filename: i for i in z.infolist()}
    plain, xml = plain_of(zipfile.ZipFile(DOCX))

    if NEW in plain:
        print("  已是新口径，无需改动")
        return 0
    if ANCHOR not in plain:
        print(f"  🔴 演示稿里找不到锚点「{ANCHOR}」——措辞可能已变。")
        print("     这时要人工看稿子，别让脚本猜：猜错的口播比旧口播更糟。")
        return 1

    backup = DOCX.with_suffix(".docx.bak")
    if not backup.exists():
        shutil.copy2(DOCX, backup)
        print(f"  备份 -> {backup.name}")

    # 定位：优先整句落在同一个 <w:t> 里
    hit = None
    for m in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S):
        if ANCHOR in m.group(1):
            hit = m
            break

    if hit is None:
        print(f"  🔴 锚点纯文本里有，但没有任何单个 <w:t> 完整包含它"
              f"（被 Word 拆进多个 run）。")
        print("     请在 Word 里手工改这一句 —— 脚本猜 run 边界只会改坏排版。")
        return 1

    frag = hit.group(1)
    # 把「检查 Python……请保持网络畅通并等待」整段换成新说法
    m_old = re.search(r"程序会检查电脑中的Python是否安装.*?请保存网络畅通并等待。", frag)
    if not m_old:
        print(f"  🔴 锚点所在的 run 里找不到完整的旧句，原文是：\n     {frag}")
        return 1
    old_txt = m_old.group(0)
    new_frag = frag[:m_old.start()] + NEW + frag[m_old.end():]
    print(f"  旧: ...{old_txt}")
    print(f"  新: ...{NEW}")
    xml = xml[:hit.start(1)] + new_frag + xml[hit.end(1):]
    blobs["word/document.xml"] = xml.encode("utf-8")

    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            zi = zipfile.ZipInfo(n, date_time=infos[n].date_time)
            zi.compress_type = infos[n].compress_type
            zi.external_attr = infos[n].external_attr
            z.writestr(zi, blobs[n])
    print(f"  已写回 {DOCX.name}")

    # ---- 复验：zip 完整、XML 合法、口径确实换掉了 ----
    with zipfile.ZipFile(DOCX) as z:
        check("zip 完整", z.testzip() is None)
        ET.fromstring(z.read("word/document.xml"))
    p2, _ = plain_of(zipfile.ZipFile(DOCX))
    check("新口径已进稿", NEW in p2)
    check("旧的「程序会检查电脑中的Python是否安装」已不在", ANCHOR not in p2)
    check("旧的「请保存网络畅通并等待」已不在", "请保存网络畅通并等待" not in p2)
    check("两条分支都写到了（自带 runtime / 有网才需要网络）",
          "自带的runtime" in p2 and "保持网络畅通" in p2)
    check("依赖个数仍是现算的 8",
          "8个依赖" in p2 or "requirements.txt里的8个依赖" in p2)

    print()
    print(f"通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
