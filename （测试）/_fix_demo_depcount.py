# -*- coding: utf-8 -*-
r"""把演示稿里的依赖个数改成**从 requirements.txt 现算**的那个数。

背景（一轮来回）：
  · mcp 拆成可选依赖 → 核心依赖 8 → 7，演示稿照着念的「8 个依赖」过时了；
  · 随后查出 networkx 的 pagerank() 无条件要 scipy，而 networkx 自己
    不声明它 → scipy 补进 requirements.txt → 核心依赖回到 **8**。
数字绕回起点，但**理由已经完全不同**。这是活口径，不是历史数字。

所以这里不能写死 OLD/NEW，必须每次现算：
  n = requirements.txt 里非注释非空行的条数
否则下一次加减依赖，这个演示稿又会悄悄说错 —— 而它是**照着念**的。

做法上的坑（上次就踩过）：docx 里一句话经常被 Word 拆进多个 <w:t>，
直接对整段 replace 经常一个字符都匹配不到，所以先在纯文本里定位、
再按 <w:t> 逐个替换。

只改这一处，其余字节原样写回；写回后验 zip 完整性与 XML 合法性。
"""
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCX = ROOT.parent / "提交包" / "演示" / "演示稿.docx"
REQ = ROOT / "requirements.txt"

PAT = re.compile(r"(\d+)个依赖")


def core_dep_count() -> int:
    """核心依赖个数 = requirements.txt 里非注释、非空的行数。"""
    n = 0
    for ln in REQ.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            n += 1
    return n


def main() -> int:
    if not DOCX.exists():
        print(f"  MISSING {DOCX}")
        return 1
    want = core_dep_count()
    print(f"  requirements.txt 现算核心依赖：{want} 个")

    backup = DOCX.with_suffix(".docx.bak")
    if not backup.exists():
        shutil.copy2(DOCX, backup)
        print(f"  备份 -> {backup.name}")

    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
        infos = {i.filename: i for i in z.infolist()}

    xml = blobs["word/document.xml"].decode("utf-8")
    plain = re.sub(r"<[^>]+>", "", xml)
    found = PAT.findall(plain)
    if not found:
        print(f"  SKIP  演示稿里没有「N个依赖」这种写法，措辞可能不同，"
              f"请人工核对（别当成「不需要改」）")
        return 0
    print(f"  文中现有说法：{sorted(set(found))}")

    changed = 0
    for m in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S):
        frag = m.group(1)
        if not PAT.search(frag):
            continue
        new_frag = PAT.sub(f"{want}个依赖", frag)
        if new_frag != frag:
            print(f"  改: ...{frag}...")
            print(f"  为: ...{new_frag}...")
            xml = xml[:m.start(1)] + new_frag + xml[m.end(1):]
            changed += 1

    if changed == 0:
        print(f"  已一致（{want}），无需改动")
        return 0

    blobs["word/document.xml"] = xml.encode("utf-8")
    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            zi = zipfile.ZipInfo(n, date_time=infos[n].date_time)
            zi.compress_type = infos[n].compress_type
            zi.external_attr = infos[n].external_attr
            z.writestr(zi, blobs[n])
    print(f"  已写回 {DOCX.name}")

    # 写回后必须验：docx 本质是 zip，XML 坏了 Word 会直接打不开。
    with zipfile.ZipFile(DOCX) as z:
        bad = z.testzip()
        if bad:
            print(f"  🔴 zip 损坏于 {bad}")
            return 1
        ET.fromstring(z.read("word/document.xml"))
    plain2 = re.sub(r"<[^>]+>", "", zipfile.ZipFile(DOCX).read("word/document.xml").decode("utf-8"))
    got = sorted(set(PAT.findall(plain2)))
    print(f"  复验：zip 完整、XML 合法，文中现为 {got}")
    if got != [str(want)]:
        print(f"  🔴 期望只有 {want}，实际 {got}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
