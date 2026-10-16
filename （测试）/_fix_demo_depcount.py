# -*- coding: utf-8 -*-
r"""把演示稿里「8 个依赖」改成「7 个依赖」。

背景：mcp 已从 requirements.txt 拆到 requirements-mcp.txt（可选），
核心依赖变成 7 个。演示稿第 ① 段照着念的是旧数字。

做法上有个坑：docx 里一句话经常被 Word 拆进多个 <w:t>，
直接对整段字符串 replace 经常一个字符都匹配不到。
所以先定位「8个依赖」在 XML 里的真实分布，再决定改哪一段文本。

只改这一处，其余字节原样写回。
"""
import re
import shutil
import sys
import zipfile
from pathlib import Path

DOCX = Path(__file__).resolve().parent.parent.parent / "提交包" / "演示" / "演示稿.docx"

OLD = "8个依赖"
NEW = "7个依赖"


def main():
    if not DOCX.exists():
        print(f"  MISSING {DOCX}")
        return 1
    backup = DOCX.with_suffix(".docx.bak")
    if not backup.exists():
        shutil.copy2(DOCX, backup)
        print(f"  备份 -> {backup.name}")

    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
        infos = {i.filename: i for i in z.infolist()}

    xml = blobs["word/document.xml"].decode("utf-8")
    # 去标签后的纯文本，用它判断「这句话在不在」
    plain = re.sub(r"<[^>]+>", "", xml)
    if OLD not in plain:
        print(f"  SKIP  纯文本里找不到「{OLD}」，可能已经改过或措辞不同")
        return 0
    print(f"  命中「{OLD}」，开始定位所在 run")

    # 找出包含该片段的 <w:t> 节点
    hits = []
    for m in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S):
        if OLD in m.group(1):
            hits.append(m)
    if not hits:
        print(f"  WARN  纯文本里有，但没有任何单个 <w:t> 完整包含它"
              f"（被 Word 拆进多个 run），需人工在 Word 里改。")
        return 1

    for m in hits:
        frag = m.group(1)
        new_frag = frag.replace(OLD, NEW)
        print(f"  改前: ...{frag[max(0, frag.find(OLD) - 30):frag.find(OLD) + 20]}...")
        print(f"  改后: ...{new_frag[max(0, new_frag.find(NEW) - 30):new_frag.find(NEW) + 20]}...")
        xml = xml[:m.start(1)] + new_frag + xml[m.end(1):]

    blobs["word/document.xml"] = xml.encode("utf-8")

    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            zi = zipfile.ZipInfo(n, date_time=infos[n].date_time)
            zi.compress_type = infos[n].compress_type
            zi.external_attr = infos[n].external_attr
            z.writestr(zi, blobs[n])
    print(f"  已写回 {DOCX.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
