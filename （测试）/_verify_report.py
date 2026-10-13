# -*- coding: utf-8 -*-
"""核对报告里每个数字。只读真库 + 只打运行中的服务，口径 = 报告口径。"""
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
B = "http://127.0.0.1:8770"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}" + (f"   {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL  {name}" + (f"   {detail}" if detail else ""))


def get(p):
    with urllib.request.urlopen(B + p, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


db = sqlite3.connect("file:" + urllib.request.pathname2url(
    str(ROOT / "data" / "cacx.db")) + "?mode=ro", uri=True)
q = lambda s: db.execute(s).fetchone()[0]

print("=" * 70)
print("① 语料规模")
print("=" * 70)
check("2024 年以来 4416 篇",
      q("SELECT COUNT(*) FROM articles WHERE year>=2024") == 4416,
      str(q("SELECT COUNT(*) FROM articles WHERE year>=2024")))
check("有正文 4415 篇",
      q("SELECT COUNT(*) FROM articles WHERE year>=2024 AND content_text IS NOT NULL AND LENGTH(content_text)>0") == 4415,
      str(q("SELECT COUNT(*) FROM articles WHERE year>=2024 AND content_text IS NOT NULL AND LENGTH(content_text)>0")))
check("栏目 25 个",
      q("SELECT COUNT(DISTINCT column_code) FROM articles WHERE year>=2024") == 25,
      str(q("SELECT COUNT(DISTINCT column_code) FROM articles WHERE year>=2024")))
zh = q("SELECT SUM(LENGTH(content_text)) FROM articles WHERE year>=2024")
check("正文 650 万字", 6_450_000 <= zh <= 6_550_000, f"{zh/1e4:.0f} 万字")

print()
print("=" * 70)
print("② 抽取规模")
print("=" * 70)
check("去重实体 16561",
      q("SELECT COUNT(DISTINCT name) FROM entities") == 16561,
      str(q("SELECT COUNT(DISTINCT name) FROM entities")))
check("关系 8419", q("SELECT COUNT(*) FROM relations") == 8419,
      str(q("SELECT COUNT(*) FROM relations")))

print()
print("=" * 70)
print("③ 关系类型分布（报告：5313/2060/557/555）")
print("=" * 70)
for rt, want in [("co_issue", 5313), ("issue", 2060), ("target", 557), ("adopt", 489)]:
    got = q(f"SELECT COUNT(*) FROM relations WHERE rel_type='{rt}'")
    check(f"{rt} = {want}", got == want, str(got))
ci = q("SELECT COUNT(*) FROM relations WHERE rel_type='co_issue'")
tot = q("SELECT COUNT(*) FROM relations")
check("co_issue 占 63.1%", abs(100 * ci / tot - 63.1) < 0.15,
      f"实测 {100*ci/tot:.1f}%")
check("无证据关系为 0", q("SELECT COUNT(*) FROM relations WHERE evidence IS NULL OR evidence=''") == 0)

print()
print("=" * 70)
print("④ 覆盖率 27.1%")
print("=" * 70)
cov = q("SELECT COUNT(DISTINCT article_id) FROM relations")
check("关系覆盖 1196 篇 / 27.1%",
      cov == 1196 and abs(100 * cov / 4415 - 27.1) < 0.15,
      f"{cov} 篇 = {100*cov/4415:.1f}%")

print()
print("=" * 70)
print("⑤ 图谱口径与度数")
print("=" * 70)
an = get("/api/graph/analysis?min_degree=2")
nodes = {n["id"]: n for n in an["graph"]["nodes"]}
check("1633 节点 / 3921 边 / 115 连通块",
      (an["nodes"], an["edges"], an["components"]) == (1633, 3921, 115),
      f'{an["nodes"]}/{an["edges"]}/{an["components"]}')
for nm, want in [("中央网信办", 243), ("工业和信息化部", 126), ("公安部", 102)]:
    got = nodes.get(nm, {}).get("degree")
    check(f"{nm} 度 {want}", got == want, f"实测 {got}")

print()
print("=" * 70)
print("⑥ 介数（报告：全图 0.194/0.1173，聚焦 0.8282/0.0272）")
print("=" * 70)
bt = sorted(((n["id"], n.get("betweenness", 0)) for n in an["graph"]["nodes"]),
            key=lambda kv: -kv[1])
check("全图第一 = 中央网信办 0.194",
      bt[0][0] == "中央网信办" and abs(bt[0][1] - 0.194) < 0.001,
      f'{bt[0][0]} = {bt[0][1]}')
check("全图第二 = 网信办 0.1173", bt[1][0] == "网信办" and abs(bt[1][1] - 0.1173) < 0.001,
      f'{bt[1][0]} = {bt[1][1]}')
r = bt[0][1] / bt[1][1]
check("两者相差 1.65 倍", abs(r - 1.65) < 0.05, f"实测 {r:.2f}")

g1 = get("/api/graph/analysis?focus=" + urllib.parse.quote("中央网信办") + "&hops=1")
b1 = sorted(((n["id"], n.get("betweenness", 0)) for n in g1["graph"]["nodes"]),
            key=lambda kv: -kv[1])
check("聚焦 244 节点 / 1015 边", (g1["nodes"], g1["edges"]) == (244, 1015),
      f'{g1["nodes"]}/{g1["edges"]}')
check("聚焦第一 = 中央网信办 0.8282",
      b1[0][0] == "中央网信办" and abs(b1[0][1] - 0.8282) < 0.001,
      f'{b1[0][0]} = {b1[0][1]}')
check("聚焦第二 0.0272", abs(b1[1][1] - 0.0272) < 0.001, f'{b1[1][0]} = {b1[1][1]}')
check("聚焦两者相差 30 倍", 25 < b1[0][1] / b1[1][1] < 35, f"实测 {b1[0][1]/b1[1][1]:.0f} 倍")

print()
print("=" * 70)
print("⑦ 主题逐年占比（报告表格）")
print("=" * 70)
tl = get("/api/panel/timeline")
agg = {}
for x in tl["themes"]:
    agg.setdefault(x["month"][:4], Counter())
    agg[x["month"][:4]].update(x["problems"])
want = {
    "2024": {"生成式AI": 11.2, "个人信息": 24.5, "未成年人": 20.8, "数据安全": 17.6},
    "2025": {"生成式AI": 15.1, "个人信息": 30.6, "未成年人": 14.5, "数据安全": 22.3},
    "2026": {"生成式AI": 17.5, "个人信息": 27.0, "未成年人": 18.7, "数据安全": 17.2},
}
for yr, items in want.items():
    c = agg[yr]
    t = sum(c.values())
    for k, v in items.items():
        got = round(100 * c[k] / t, 1)
        check(f"{yr} {k} = {v}%", abs(got - v) < 0.1, f"实测 {got}%")

first = []
for x in tl["themes"]:
    c = x.get("problems") or {}
    if c:
        best = max(c.items(), key=lambda kv: kv[1])
        if best[0] == "生成式AI":
            first.append(x["month"])
check("生成式 AI 当月第一仅 2 次", len(first) == 2, str(first))
check("两次都在最近 14 个月", first == ["2025-08", "2026-09"], str(first))

print()
print("=" * 70)
print("⑧ 发文量（报告：1495 / 1541 / 1380，2025-09 峰值 193）")
print("=" * 70)
yr = Counter()
for x in tl["months"]:
    yr[x["month"][:4]] += x["total"]
for y, w in [("2024", 1495), ("2025", 1541), ("2026", 1380)]:
    check(f"{y} = {w}", yr[y] == w, str(yr[y]))
pk = max(tl["months"], key=lambda x: x["total"])
check("单月峰值 193（2025-09）",
      pk["month"] == "2025-09" and pk["total"] == 193, f'{pk["month"]}={pk["total"]}')

print()
print("=" * 70)
print("⑨ 浙江对照")
print("=" * 70)
rows = db.execute("SELECT site, COUNT(*) n FROM articles WHERE year>=2024 GROUP BY site").fetchall()
d = dict(rows)
check("中央网信办 2960 + 浙江 1456 = 4416",
      d.get("cac", 0) + d.get("zjwx", 0) == 4416, str(d))
check("浙江网信网 1456 篇", d.get("zjwx") == 1456, str(d))
# 两省是否真有直接联合发文
cross = q("""SELECT COUNT(*) FROM relations WHERE year>=2024
             AND site='zjwx' AND (src_name LIKE '%浙江%' OR dst_name LIKE '%浙江%')
             AND (src_name LIKE '%中央%' OR dst_name LIKE '%中央%')""") if False else None

print()
print("=" * 70)
print("⑩ 报告里点名的两条证据")
print("=" * 70)


def evid(a, b):
    r = get("/api/graph/evidence?src=" + urllib.parse.quote(a) +
            "&dst=" + urllib.parse.quote(b))
    return r["items"]


e1 = evid("中央网信办", "农业农村部")
check("备选① 24 条证据", len(e1) == 24, str(len(e1)))
s1 = "中央网信办、农业农村部、工业和信息化部联合印发《数字乡村高质量发展行动计划（2026—2030年）》"
check("备选① 原文逐字一致", any(s1 in (i["evidence"] or "") for i in e1))

e2 = evid("国家网信办", "网络数据安全风险评估办法")
check("备选② 3 条证据", len(e2) == 3, str(len(e2)))
check("备选② 含《办法》",
      any("（以下简称《办法》）" in (i["evidence"] or "") for i in e2))

# 七部门长句：按**同一句原文**分组计数（宽匹配会把多句不同起首词的长句混进来，
# 得到偏大的数 —— 报告里的数必须对应「这一句」）
n111 = db.execute("""SELECT COALESCE(MAX(c), 0) FROM (
                        SELECT COUNT(*) c FROM relations
                        WHERE evidence LIKE '%国家发展改革委%'
                          AND evidence LIKE '%生态环境部%'
                          AND evidence LIKE '%住房城乡建设部%'
                          AND evidence LIKE '%商务部%'
                        GROUP BY evidence)""").fetchone()[0]
check("七部门长句支撑 21 条关系", n111 == 21, str(n111))

print()
print("=" * 70)
print("⑪ AI 主题标注（报告说 0 篇）")
print("=" * 70)
ai = get("/api/panel/timeline")["ai"]
check("AI 主题已标注 0 篇", ai["coverage"]["labeled"] == 0,
      str(ai["coverage"]["labeled"]))

print()
print("=" * 70)
print("⑫ 污染名确实进不了图（报告第四章的立论依据）")
print("=" * 70)
for nm in ("吉林省网信办", "浙江省通信管理局", "中共浙江省委"):
    hit = nm in nodes
    check(f"{nm} 不在默认图中", not hit,
          f"在图中，度={nodes[nm]['degree']}" if hit else "已正确拦截")

print()
print("=" * 70)
print("⑬ Word 版（.docx）与 md 版是否同源、没漂移")
print("=" * 70)
docx_path = ROOT / "docs" / "报告-网信办角色与权责边界.docx"
if not docx_path.exists():
    check("docx 已生成", False, f"缺 {docx_path.name}，跑 py -3 md2docx.py")
else:
    import docx as _docx
    d = _docx.Document(str(docx_path))

    # 把段落 + 表格单元格拼成一个全文字符串
    chunks = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            for c in row.cells:
                chunks.append(c.text)
    full = "\n".join(chunks)

    check("docx 已生成", True, f"{docx_path.stat().st_size:,} 字节")

    # 关键数字一个都不能少 —— Word 版最常见的失真就是转档时漏句/漏行
    MUST = ["4415", "4416", "650 万字", "16561", "8419", "1196", "27.1%",
            "243", "126", "102", "0.194", "0.1173", "0.8282", "0.0272",
            "1633", "3921", "115", "244", "1015",
            "5313", "2060", "557", "489", "63.1%",
            "1495", "1541", "1380", "193",
            "2960", "1456", "24 条", "3 条", "21 条", "12 个端点",
            "11.2%", "15.1%", "17.5%", "18.7%", "30.6%", "27.0%"]
    missing = [m for m in MUST if m not in full]
    check(f"关键数字 {len(MUST)} 个全部保留",
          not missing, f"缺 {missing}" if missing else "无缺失")

    # markdown 残留：转换器失手时正文会露出星号或反引号
    residue = [tok for tok in ("**", "`") if tok in full]
    check("无 markdown 标记残留", not residue, f"残留 {residue}" if residue else "")

    # 表格结构：主题占比表 表头+3 年 = 4 行 x 5 列；
    # 问题应对表 表头+6 类问题（2026-10 新增「跨站重复入库」一行）= 7 行 x 3 列
    shapes = [(len(t.rows), len(t.columns)) for t in d.tables]
    check("主题表 4x5（表头+2024/2025/2026）", (4, 5) in shapes, f"实际表格 {shapes}")
    check("问题应对表 7x3（表头+6 类问题）", (7, 3) in shapes, f"实际表格 {shapes}")

    # 六个一级章节标题都在
    heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
    for need in ("一、方法与口径", "二、角色画像", "三、权责边界",
                 "四、发现的问题与应对措施", "五、已知局限", "六、结论"):
        check(f"章节「{need}」", any(need in h for h in heads))

db.close()
print()
print("=" * 70)
print(f"通过 {ok} 项，失败 {fail} 项")
print("=" * 70)
sys.exit(1 if fail else 0)