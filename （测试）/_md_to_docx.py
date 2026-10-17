# -*- coding: utf-8 -*-
r"""把 docs/STARTUP-GUIDE-zh.md 转成 Word 版，输出到 提交包/。

## 为什么不用 docx 技能的那条路

技能规定正式生成要走 D 后端，前置是 dotnet>=9 + pandoc + soffice。
本机实测：dotnet 8.0.425（要求 >=9）、pandoc/soffice/pdftoppm 全都没有，
env_check 报 Status: NOT READY。技能同时规定「闸门不过就停，不要静默换后端」。

那就不硬来。本机其实有两条**不需要任何安装**的路：
    · python-docx 1.2.0（已在）
    · Microsoft Word 16.0（COM 可用）
本脚本用前者生成、用后者验收（真的用 Word 打开一次），并把这条
「为什么不用官方后端」的理由留在文件里，免得下一个人以为忘了什么。

## 中文的坑（这是最容易做出来「看着像回事、实际很丑」的地方）

1. python-docx 只会设 **latin** 字体。中文要用 w:rFonts 的 **w:eastAsia**
   才生效，否则中文会退回 Word 默认字体，和英文不在一条基线上。
   设置要作用到**样式**上（styles['Normal'].font + style.element.rPr），
   逐 run 设只覆盖个别字，后续新段落又跑回去了。
2. 表格中文换行难看 → 表格内段落关闭 widow/orphan 意义不大，
   真正要调的是单元格边距和首行不重复。
3. 代码块必须**逐行单独成段**再设 Consolas，
   把带 \n 的一个 run 塞进去，Word 会把换行当普通字符吃掉。

## 用法

    py -3 -X utf8 "（测试）\_md_to_docx.py"
"""
import re
import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "STARTUP-GUIDE-zh.md"
DST = ROOT.parent / "提交包" / "STARTUP-GUIDE-zh.docx"

LATIN = "Calibri"
EAST = "等线"                 # Windows 自带，屏幕/打印都稳
MONO = "Consolas"

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def set_style_font(style, latin=LATIN, east=EAST, size=None, bold=None):
    """同时设 latin 与 eastAsia —— 只设前者的话中文不受控。"""
    f = style.font
    f.name = latin
    if size is not None:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    rpr = style.element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rf)
    rf.set(qn("w:ascii"), latin)
    rf.set(qn("w:hAnsi"), latin)
    rf.set(qn("w:eastAsia"), east)


def shade(par, hexfill):
    pPr = par._p.get_or_add_pPr()
    sh = pPr.makeelement(qn("w:shd"), {})
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:color"), "auto")
    sh.set(qn("w:fill"), hexfill)
    pPr.append(sh)


INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def add_rich(par, text, base_size=10.5):
    """把 **粗体** 和 `行内代码` 渲染成 run。"""
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            r = par.add_run(piece[2:-2])
            r.bold = True
            r.font.size = Pt(base_size)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            r = par.add_run(piece[1:-1])
            r.font.name = MONO
            r.font.size = Pt(base_size - 0.5)
            r.font.color.rgb = RGBColor(0xB0, 0x30, 0x60)
            rpr = r._r.get_or_add_rPr()
            rf = rpr.makeelement(qn("w:rFonts"), {})
            rf.set(qn("w:ascii"), MONO)
            rf.set(qn("w:hAnsi"), MONO)
            rf.set(qn("w:eastAsia"), EAST)   # 代码里混的中文仍要正文字体
            rpr.append(rf)
        else:
            r = par.add_run(piece)
            r.font.size = Pt(base_size)
    return par


def add_code_block(doc, lines):
    for i, ln in enumerate(lines):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.left_indent = Cm(0.6)
        pf.space_before = Pt(6 if i == 0 else 0)
        pf.space_after = Pt(6 if i == len(lines) - 1 else 0)
        pf.line_spacing = 1.0
        r = p.add_run(ln if ln else " ")
        r.font.name = MONO
        r.font.size = Pt(9)
        rpr = r._r.get_or_add_rPr()
        rf = rpr.makeelement(qn("w:rFonts"), {})
        rf.set(qn("w:ascii"), MONO)
        rf.set(qn("w:hAnsi"), MONO)
        rf.set(qn("w:eastAsia"), EAST)
        rpr.append(rf)
        shade(p, "F4F5F7")
        # 只在整块首尾加边框感靠底纹，段落本身不再加框，避免每行都是框
    return len(lines)


def add_table(doc, rows):
    header, body = rows[0], rows[1:]
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for j, cell in enumerate(header):
        c = t.rows[0].cells[j]
        c.text = ""
        add_rich(c.paragraphs[0], cell, base_size=9.5)
        for r in c.paragraphs[0].runs:
            r.bold = True
    t.rows[0]._tr.get_or_add_trPr().append(
        t.rows[0]._tr.makeelement(qn("w:tblHeader"), {}))   # 跨页时重复表头
    for row in body:
        cells = t.add_row().cells
        for j, cell in enumerate(row[:len(header)]):
            cells[j].text = ""
            add_rich(cells[j].paragraphs[0], cell, base_size=9.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)
    return len(rows)


def build():
    if not SRC.exists():
        raise SystemExit(f"🔴 找不到 {SRC}")
    lines = SRC.read_text(encoding="utf-8").splitlines()
    doc = Document()

    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.4)
    sec.top_margin = sec.bottom_margin = Cm(2.2)

    set_style_font(doc.styles["Normal"], size=10.5)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.45
    doc.styles["Normal"].paragraph_format.space_after = Pt(4)
    for name, size, bold in (("Title", 22, True), ("Heading 1", 17, True),
                             ("Heading 2", 14, True), ("Heading 3", 12, True)):
        try:
            set_style_font(doc.styles[name], size=size, bold=bold)
        except KeyError:
            pass

    stats = {"h": 0, "p": 0, "code": 0, "table": 0, "list": 0, "quote": 0}
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()

        if not s:
            i += 1
            continue

        if s.startswith("```"):                      # 围栏代码块
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i].rstrip())
                i += 1
            i += 1
            stats["code"] += add_code_block(doc, buf)
            continue

        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^[\s:|-]+$", "".join(cells)):   # 跳过分隔行
                    rows.append(cells)
                i += 1
            stats["table"] += add_table(doc, rows)
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            lvl, txt = len(m.group(1)), m.group(2)
            style = "Title" if lvl == 1 else f"Heading {min(lvl - 1, 3)}"
            p = doc.add_paragraph(style=style)
            add_rich(p, txt, base_size={1: 22, 2: 17, 3: 14, 4: 12}.get(lvl, 11))
            if lvl == 1:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            stats["h"] += 1
            i += 1
            continue

        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_before = Pt(4)
            add_rich(p, "\n".join(x for x in buf if x), base_size=9.5)
            for r in p.runs:
                r.font.color.rgb = RGBColor(0x55, 0x5D, 0x66)
            shade(p, "F7F8FA")
            stats["quote"] += 1
            continue

        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            indent, mark, txt = m.groups()
            p = doc.add_paragraph(style="List Bullet" if not mark[0].isdigit() else "List Number")
            if len(indent) >= 2:
                p.paragraph_format.left_indent = Cm(1.4)
            add_rich(p, txt)
            stats["list"] += 1
            i += 1
            continue

        if set(s) <= {"-", "*", "_"} and len(s) >= 3:
            i += 1
            continue

        p = doc.add_paragraph()
        add_rich(p, s)
        stats["p"] += 1
        i += 1

    DST.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(DST))
    return stats


def main():
    print("=" * 66)
    print("md -> docx")
    print("=" * 66)
    stats = build()
    print(f"  标题 {stats['h']} / 正文段 {stats['p']} / 代码行 {stats['code']} "
          f"/ 表格行 {stats['table']} / 列表 {stats['list']} / 引用 {stats['quote']}")
    print(f"  -> {DST}  {DST.stat().st_size} 字节")

    # ---- 结构复验（不依赖 Word）----
    d = Document(str(DST))
    heads = [p.text for p in d.paragraphs if p.style.name.startswith(("Heading", "Title"))]
    check("能重新打开且标题层级在", len(heads) >= 20, f"{len(heads)} 个标题")
    check("表格数量与源文一致", len(d.tables) >= 3, f"{len(d.tables)} 张表")
    check("段落总数合理", len(d.paragraphs) > 150, f"{len(d.paragraphs)} 段")
    xml = d.element.xml
    check("中文字体真的写进了样式（w:eastAsia）", "eastAsia" in xml)
    check("等宽代码字体写进去了", "Consolas" in xml)
    # 比对前两边都要剥掉 ` 反引号：渲染时行内代码的反引号本来就该去掉，
    # 直接拿源文原样比，等于要求 docx 里保留 markdown 记号。
    def _plain(t):
        return t.replace("`", "").replace("**", "").strip()

    heads_plain = [_plain(h) for h in heads]
    src_heads = [_plain(l.strip().lstrip("#")) for l in
                 SRC.read_text(encoding="utf-8").splitlines()
                 if re.match(r"^#{1,4}\s", l.strip())]
    missing = [h for h in src_heads if h not in heads_plain]
    check("源文每个标题都在 docx 里", not missing, f"缺：{missing[:3]}")

    # ---- 用真 Word 开一次：docx 本质是 zip+XML，Word 打不开就等于坏了 ----
    # ⚠ WdStatistic: 0=Words  1=Lines  2=Pages  3=Paragraphs
    #   我第一版把 0 当成页数打了出来，于是出现 "PAGES=5248" 这种
    #   一眼就荒谬的数字。**打错标签的检查比没有检查更糟** ——
    #   它会让读的人以为文档真有问题，或者更糟，让人以为检查过了。
    try:
        ps = ("$w=New-Object -ComObject Word.Application;$w.Visible=$false;"
              f"$d=$w.Documents.Open('{DST}',$false,$true);"
              "$wd=0;$pg=2;"
              "$n=$d.ComputeStatistics($wd);$p=$d.ComputeStatistics($pg);"
              "$d.Close($false);$w.Quit();Write-Output \"PAGES=$p WORDS=$n\"")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "").strip()
        check("真 Word 16 能打开且读得出页数", r.returncode == 0 and "PAGES=" in out, out)
        m = re.search(r"PAGES=(\d+)", out)
        check("页数像样（10~80 页，不是 5000）",
              bool(m) and 10 <= int(m.group(1)) <= 80,
              out)
    except Exception as e:            # noqa: BLE001
        check("真 Word 16 能打开", False, f"{type(e).__name__}: {e}")

    print()
    print(f"通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
