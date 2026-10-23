# -*- coding: utf-8 -*-
"""生成「数据健康顾问 · 提示词」docx，供演示截图/说明用。

⚠ 三样东西全部**从源码现读**，不手抄：
  1. SYS_ADVISORY   —— 系统提示词本身（cacx/ai/tasks.py）
  2. 口径说明块      —— advisory_snapshot() 返回的 _口径说明
  3. 批次字段        —— advisory_snapshot() 返回的 最近同步批次
抄的那份必然漂：改了提示词而这份文档没跟上，演示时就会拿着过期内容讲。

⚠ 为什么不直接 import 跑一遍：
  advisory_snapshot() 需要一个 db 与 cfg 才能算，而算批次要查库。
  这里直接调用它（只读），拿到的是**与线上完全一致**的结构。
"""
import os
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    # 脚本在 <作业>/方向-爬取工具的更新/（测试）/，
    # 演示目录是它的**同级**（<作业>/演示/）—— 从 ROOT 上一级。
    # ⚠ 用相对层级推导路径容易差一级（写错过两次），
    #   所以这里直接按 ROOT 的名字拼：ROOT 的父目录就是作业目录。
    os.path.dirname(ROOT), "演示", "数据健康顾问提示词.docx")

sys.path.insert(0, ROOT)

from cacx.ai.tasks import SYS_ADVISORY, advisory_snapshot  # noqa: E402
from cacx import db as DB  # noqa: E402
from cacx.config import load  # noqa: E402


def _plain(text: str) -> str:
    """去掉源码里给开发者看的 Markdown 记号（** `）。"""
    return (text or "").replace("**", "").replace("`", "")


def set_cell(cell, text, bold=False, size=9.5, color=None, mono=False):
    cell.text = ""
    p = cell.paragraphs[0]
    r = p.add_run(text)
    r.bold = bold
    r.font.size = Pt(size)
    r.font.name = "Consolas" if mono else "Microsoft YaHei"
    if color:
        r.font.color.rgb = color
    return p


def main():
    cfg = load()
    d = DB.open_db(cfg)
    try:
        snap = advisory_snapshot(cfg, d, [], [])
    finally:
        d.close()

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Microsoft YaHei"
    st.font.size = Pt(10.5)

    h = doc.add_heading("cacx · 数据健康顾问提示词", level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("模型：MiniMax-M3.1-Flash-Preview　|　"
                  "入口：数据同步页 →「数据健康顾问」")
    r.font.size = Pt(10)
    r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    # ---- 1. 系统提示词原文
    doc.add_heading("一、系统提示词（原文）", level=2)
    note = doc.add_paragraph()
    nr = note.add_run("以下为实际发给模型的 SYS_ADVISORY，"
                      "由 cacx/ai/tasks.py 现读，不是手抄。")
    nr.font.size = Pt(9)
    nr.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    box = doc.add_table(rows=1, cols=1)
    box.style = "Light Grid Accent 1"
    c = box.rows[0].cells[0]
    c.text = ""
    for i, line in enumerate(SYS_ADVISORY.split("\n")):
        para = c.paragraphs[0] if i == 0 else c.add_paragraph()
        run = para.add_run(_plain(line))
        run.font.size = Pt(9)
        run.font.name = "Consolas"

    # ---- 2. 字段口径说明
    doc.add_heading("二、字段口径说明（随快照一起发给模型）", level=2)
    note2 = doc.add_paragraph()
    n2 = note2.add_run("模型看不到原始字段名，只能按这份说明理解。"
                       "少了它，模型会把不同口径的数当成「口径不一致」。")
    n2.font.size = Pt(9)
    n2.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    tbl = doc.add_table(rows=1, cols=2)
    tbl.style = "Light Grid Accent 1"
    for cc, name in zip(tbl.rows[0].cells, ("字段", "口径")):
        set_cell(cc, name, bold=True, size=9.5)
    for k, v in (snap.get("_口径说明") or {}).items():
        row = tbl.add_row()
        set_cell(row.cells[0], k, size=8.5)
        set_cell(row.cells[1], _plain(v), size=8.5)

    # ---- 3. 批次字段对照
    doc.add_heading("三、同步批次字段对照", level=2)
    note3 = doc.add_paragraph()
    n3 = note3.add_run("喂给模型的是中文名；库里原本是英文字段，"
                       "由 advisory_snapshot() 逐个改名。")
    n3.font.size = Pt(9)
    n3.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    batches = snap.get("最近同步批次") or []
    # ⚠ 对照表要**如实**反映「哪个中文键来自库里哪个字段」。
    #   「站点」与「站点代码」都来自 site（一个给人看、一个是 code），
    #   这里如实标出同源，而不是把它们排成两行不同的字段 ——
    #   排错了会让人以为库里有两个站点字段。
    MAP = [
        ("站点", "site（取站点中文名，如「中国网信办」）"),
        ("站点代码", "site（取 code，如「cac」）"),
        ("接口模式", "mode（incremental→增量同步 / 其余→全量重抓）"),
        ("开始时间", "started_at"),
        ("结束时间", "finished_at"),
        ("列表接口扫到条数", "listed"),
        ("本次新增条数", "new_articles"),
        ("抓取失败条数", "failed"),
    ]
    tbl2 = doc.add_table(rows=1, cols=2)
    tbl2.style = "Light Grid Accent 1"
    for cc, name in zip(tbl2.rows[0].cells, ("发给模型的中文字段", "库里的英文字段")):
        set_cell(cc, name, bold=True, size=9.5)
    for zh, en in MAP:
        row = tbl2.add_row()
        set_cell(row.cells[0], zh, size=8.5)
        set_cell(row.cells[1], en, size=8.5, mono=True)

    doc.add_paragraph()
    warn = doc.add_paragraph()
    wr = warn.add_run(_plain(
        "⚠ 只取「启用中」站点的批次。历史表里留着一个已停用的"
        "开发验证源（只抓了 20 条、2 个栏目），若原样喂给模型，"
        "它会被当成第三个站点解读。"))
    wr.font.size = Pt(9)
    wr.font.color.rgb = RGBColor(0xA0, 0x50, 0x00)

    # ---- 3b. 实际批次（模型真正看到的）
    doc.add_heading("四、实际喂给模型的最近批次", level=2)
    note3b = doc.add_paragraph()
    n3b = note3b.add_run(f"取自当前库，共 {len(batches)} 条 —— "
                          "只有两个在用站点的记录。")
    n3b.font.size = Pt(9)
    n3b.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    if batches:
        cols = ["站点", "接口模式", "开始时间", "列表接口扫到条数",
                "本次新增条数", "抓取失败条数"]
        tbl2b = doc.add_table(rows=1, cols=len(cols))
        tbl2b.style = "Light Grid Accent 1"
        for cc, name in zip(tbl2b.rows[0].cells, cols):
            set_cell(cc, name, bold=True, size=8.5)
        for b in batches:
            row = tbl2b.add_row()
            set_cell(row.cells[0], b.get("站点", ""), size=8)
            set_cell(row.cells[1], b.get("接口模式", ""), size=8)
            set_cell(row.cells[2], b.get("开始时间", ""), size=8)
            set_cell(row.cells[3], str(b.get("列表接口扫到条数", "")), size=8)
            set_cell(row.cells[4], str(b.get("本次新增条数", "")), size=8)
            set_cell(row.cells[5], str(b.get("抓取失败条数", "")), size=8)

    # ---- 4. 输出格式
    doc.add_heading("五、模型输出格式", level=2)
    tbl3 = doc.add_table(rows=1, cols=2)
    tbl3.style = "Light Grid Accent 1"
    for cc, name in zip(tbl3.rows[0].cells, ("字段", "含义")):
        set_cell(cc, name, bold=True, size=9.5)
    for k, v in [("summary", "一段话结论"),
                 ("quality_score", "0–100 整数，按「数据可用于何种分析」打分"),
                 ("data_quality", "数据质量观察，逐条"),
                 ("risks", "潜在风险，只写影响结论可信度的"),
                 ("suggestions", "后续建议，具体可执行"),
                 ("need_human", "true/false，是否需人工确认")]:
        row = tbl3.add_row()
        set_cell(row.cells[0], k, size=8.5, mono=True)
        set_cell(row.cells[1], v, size=8.5)

    foot = doc.add_paragraph()
    fr = foot.add_run("本文件由 cacx/ai/tasks.py 现读生成，随代码同步。")
    fr.font.size = Pt(8.5)
    fr.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    doc.save(OUT)
    print(f"已生成：{os.path.abspath(OUT)}")
    print(f"提示词 {len(SYS_ADVISORY)} 字符　|　"
          f"口径说明 {len(snap.get('_口径说明') or {})} 条　|　"
          f"批次 {len(batches)} 条　|　"
          f"涉及站点 {sorted({b['站点代码'] for b in batches})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
