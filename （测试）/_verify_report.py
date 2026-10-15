# -*- coding: utf-8 -*-
"""核对报告里每个数字。只读真库 + 只打运行中的服务，口径 = 报告口径。

⚠⚠ 2026-10-15：真库已按**全区间**（2000-09-30 ~ 2026-10-10）重跑落库
（用户 09:08 在界面上点的「抽取并写入」）。于是**每个数字都必须写明分母口径**，
报告现在是「一份 2024 年以来的深描 + 全库底数」的双口径结构：

  · 报告的**分析窗** = 2024 年以来（4435 篇 / 4433 篇有正文）。
    图谱、证据、主题占比、发文量、浙江对照都属于这一档 —— 未变（重跑只新增了
    2024 年之前的关系，2024+ 窗口的抽取结果逐条一致）。
  · 报告的**全库底数** = 28919 篇。抽了多少、覆盖多少、跨度多长，必须按全库说，
    否则会出现「6887 篇 / 4433 篇 = 156%」这种分子分母不同口径的自相矛盾。
  · ⚠ 下面这些**篇数**是「库快照」：真库仍在持续采集（10:42 服务重启后两站又新采
    14 篇，全库 28905→28919、分析窗 4421→4435，而关系表一分未变，因为新文章还没
    抽取）。数变了就照实测改，不算断言写错；改的时候报告 md / 演示脚本 /
    `_verify_demo_docs.py` / `06-R5` / `autodemo.py` 要一起改。
  · 图谱接口有两套默认：`/api/graph/analysis` 不传日期 = 全库（12160 节点，慢）；
    网页图谱页日期框默认 2024-01，所以**屏幕上**是 1757 节点那档。
    本文件两档都核（全库那档只在 §⑤ 打一次，实测 ~270s，请耐心）。
"""
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


def get(p, timeout=900):
    # ⚠ 默认超时给足 900s：§⑤ 那次**全库**建图实测 237–385s（关系表涨到 44342 条后
    #   已经超过原来的 300s 硬超时，会直接 TimeoutError 把整脚本打断）。
    #   服务端有响应缓存（c1e33b4），第一次算完后面基本秒回；超时只是客户端耐心，
    #   不影响任何断言口径。
    with urllib.request.urlopen(B + p, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


db = sqlite3.connect("file:" + urllib.request.pathname2url(
    str(ROOT / "data" / "cacx.db")) + "?mode=ro", uri=True)
q = lambda s: db.execute(s).fetchone()[0]

print("=" * 70)
print("① 语料规模")
print("=" * 70)
# ⚠ 这些数是**库快照**（2026-10-15 上午实测）。真库一直在采集，
#   每采一批新文章这两行的数就会变 —— 那是数据变了，不是断言写错，
#   照实测值改这两行即可（演示脚本 §6 的教训同样适用于这里）。
n24 = q("SELECT COUNT(*) FROM articles WHERE year>=2024")
n24_txt = q("SELECT COUNT(*) FROM articles WHERE year>=2024 AND content_text "
            "IS NOT NULL AND LENGTH(content_text)>0")
check("2024 年以来 4435 篇", n24 == 4435, str(n24))
check("有正文 4433 篇", n24_txt == 4433, str(n24_txt))
check("栏目 25 个",
      q("SELECT COUNT(DISTINCT column_code) FROM articles WHERE year>=2024") == 25,
      str(q("SELECT COUNT(DISTINCT column_code) FROM articles WHERE year>=2024")))
zh = q("SELECT SUM(LENGTH(content_text)) FROM articles WHERE year>=2024")
check("正文 653 万字", 6_500_000 <= zh <= 6_550_000, f"{zh/1e4:.0f} 万字")

# 全库底数（报告 §六 的规模句用这一套；分母必须与分子同一档）
n_all = q("SELECT COUNT(*) FROM articles")
check("全库 28919 篇", n_all == 28919, str(n_all))
n_all_txt = q("SELECT COUNT(*) FROM articles WHERE content_text IS NOT NULL "
              "AND LENGTH(content_text)>0")
check("全库有正文 28850 篇", n_all_txt == 28850, str(n_all_txt))
check("全库栏目 26 个",
      q("SELECT COUNT(DISTINCT column_code) FROM articles") == 26,
      str(q("SELECT COUNT(DISTINCT column_code) FROM articles")))
zh_all = q("SELECT SUM(LENGTH(content_text)) FROM articles")
check("全库正文 3827 万字", 38_200_000 <= zh_all <= 38_400_000,
      f"{zh_all/1e4:.0f} 万字")
check("全库两站 23291 + 5628 = 28919",
      dict(db.execute("SELECT site, COUNT(*) FROM articles GROUP BY site")
           .fetchall()).get("cac") == 23291, "cac 应 23291")

print()
print("=" * 70)
print("② 抽取规模（报告 §一/§三 = 2024 年以来窗口；另核全库）")
print("=" * 70)
# ⚠ 这两条 SQL 必须带 year 过滤：报告写的是「2024 年以来的 4433 篇里抽出了…」，
#   不带过滤就是全库的数（114807 / 44342），分子分母一换口径结论全错。
W_E = "entities e JOIN articles a ON a.id=e.article_id WHERE a.year>=2024"
W_R = "relations r JOIN articles a ON a.id=r.article_id WHERE a.year>=2024"
ew = q("SELECT COUNT(DISTINCT e.name) FROM " + W_E)
check("窗口去重实体 16507", ew == 16507, str(ew))
rw = q("SELECT COUNT(*) FROM " + W_R)
check("窗口关系 8499", rw == 8499, str(rw))
ear = q("SELECT COUNT(DISTINCT e.article_id) FROM " + W_E)
check("窗口实体来源 3815 篇", ear == 3815, str(ear))
check("窗口实体行数 43927",
      q("SELECT COUNT(*) FROM " + W_E) == 43927,
      str(q("SELECT COUNT(*) FROM " + W_E)))
check("窗口关系去重对 4191",
      q("SELECT COUNT(DISTINCT r.src_name || '#' || r.dst_name) FROM " + W_R) == 4191,
      str(q("SELECT COUNT(DISTINCT r.src_name || '#' || r.dst_name) FROM " + W_R)))

# 全库（报告 §六 的「全库底数」）
ea = q("SELECT COUNT(DISTINCT name) FROM entities")
check("全库去重实体 114807", ea == 114807, str(ea))
check("全库实体行数 277355",
      q("SELECT COUNT(*) FROM entities") == 277355,
      str(q("SELECT COUNT(*) FROM entities")))
ra = q("SELECT COUNT(*) FROM relations")
check("全库关系 44342", ra == 44342, str(ra))
check("全库关系去重对 29049",
      q("SELECT COUNT(DISTINCT src_name || '#' || dst_name) FROM relations") == 29049,
      str(q("SELECT COUNT(DISTINCT src_name || '#' || dst_name) FROM relations")))
check("全库关系来源 6887 篇",
      q("SELECT COUNT(DISTINCT article_id) FROM relations") == 6887,
      str(q("SELECT COUNT(DISTINCT article_id) FROM relations")))

print()
print("=" * 70)
print("③ 关系类型分布（窗口 5367/2082/561/489；全库 29724/9724/2725/2169）")
print("=" * 70)
for rt, want in [("joint_issue", 5367), ("issue", 2082), ("target", 561), ("adopt", 489)]:
    got = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='" + rt + "'")
    check(f"窗口 {rt} = {want}", got == want, str(got))
ci = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue'")
check("窗口 joint_issue 占 63.1%", abs(100 * ci / rw - 63.1) < 0.15,
      f"实测 {100*ci/rw:.1f}%")
check("无证据关系为 0",
      q("SELECT COUNT(*) FROM relations WHERE evidence IS NULL OR evidence=''") == 0)
# 全库
for rt, want in [("joint_issue", 29724), ("issue", 9724), ("adopt", 2725),
                 ("target", 2169)]:
    got = q("SELECT COUNT(*) FROM relations WHERE rel_type='" + rt + "'")
    check(f"全库 {rt} = {want}", got == want, str(got))
ci_a = q("SELECT COUNT(*) FROM relations WHERE rel_type='joint_issue'")
check("全库 joint_issue 占 67.0%", abs(100 * ci_a / ra - 67.0) < 0.15,
      f"实测 {100*ci_a/ra:.1f}%")
check("全库 joint_issue 含「联合印发」1638 条",
      q("SELECT COUNT(*) FROM relations WHERE rel_type='joint_issue' "
        "AND evidence LIKE '%联合印发%'") == 1638,
      str(q("SELECT COUNT(*) FROM relations WHERE rel_type='joint_issue' "
            "AND evidence LIKE '%联合印发%'")))

print()
print("=" * 70)
print("④ 覆盖率（窗口 1195/4435 = 26.9%（对 4433 篇有正文为 27.0%）；全库 6887/28919 = 23.8%）")
print("=" * 70)
cov = q("SELECT COUNT(DISTINCT r.article_id) FROM " + W_R)
check("窗口关系覆盖 1195 篇 / 26.9%（分母 2024+ 全部 4435 篇；有正文 27.0%）",
      cov == 1195 and abs(100 * cov / n24 - 26.9) < 0.15,
      f"{cov} 篇 = {100*cov/n24:.1f}%")
# ⚠ 全库的分母必须是 28919（全库文章数）。
#   曾经出现过「6887 篇 / 4433 篇 = 156%」—— 分子是全库、分母是窗口，自相矛盾。
cov_a = q("SELECT COUNT(DISTINCT article_id) FROM relations")
check("全库关系覆盖 6887 篇 / 23.8%（分母 28919）",
      cov_a == 6887 and abs(100 * cov_a / n_all - 23.8) < 0.15,
      f"{cov_a} 篇 = {100*cov_a/n_all:.1f}%")
have = q("SELECT COUNT(DISTINCT id) FROM articles WHERE "
         "id IN (SELECT article_id FROM entities) "
         "OR id IN (SELECT article_id FROM relations)")
check("全库有抽取结果 26080 篇 / 90.2%",
      have == 26080 and abs(100 * have / n_all - 90.2) < 0.2,
      f"{have} 篇 = {100*have/n_all:.1f}%")
check("窗口有抽取结果 3815 篇 / 86.0%",
      abs(100 * 3815 / n24 - 86.0) < 0.1, f"{100*3815/n24:.1f}%")
# 时间跨度：全库 vs 窗口（报告 §六 与 §一 各引一档）
_sp = db.execute("SELECT MIN(a.pub_day), MAX(a.pub_day) FROM relations r "
                 "JOIN articles a ON a.id=r.article_id").fetchone()
check("全库关系跨度 2000-09-30 ~ 2026-10-10",
      tuple(_sp) == ("2000-09-30", "2026-10-10"), str(tuple(_sp)))
_sp2 = db.execute("SELECT MIN(a.pub_day), MAX(a.pub_day) FROM relations r "
                  "JOIN articles a ON a.id=r.article_id WHERE a.year>=2024").fetchone()
check("窗口关系跨度 2024-01-02 ~ 2026-10-10",
      tuple(_sp2) == ("2024-01-02", "2026-10-10"), str(tuple(_sp2)))

print()
print("=" * 70)
print("⑤ 图谱口径与度数（网页首屏 2024-01 档 + 全库档）")
print("=" * 70)
# ⚠ 两件事都要显式说明，否则这个闸会红/会慢：
#   1) node_types 必须全勾：不带时服务端按「措施被低置信门槛整类挡在图外」
#      的旧口径返回（实测 1631/3958/115），而报告的「默认分析口径」是措施放行的那套。
#   2) date_from 必须显式带 2024-01-01：报告/演示脚本/网页首屏都是这一档
#      （index.html 的 gp_dfrom 默认值）。不带 = 全库 12160 节点，
#      而且要在全图上算介数（实测 268s，本文件会等）。
NT = "org,policy,problem,measure"
WEB = "&date_from=2024-01-01"
an = get("/api/graph/analysis?min_degree=2&node_types=" + NT + WEB)
nodes = {n["id"]: n for n in an["graph"]["nodes"]}
check("窗口 1757 节点 / 4191 边 / 126 连通块",
      (an["nodes"], an["edges"], an["components"]) == (1757, 4191, 126),
      f'{an["nodes"]}/{an["edges"]}/{an["components"]}')
check("窗口最大连通块 1406（80.0%）",
      an.get("largest_component") == 1406,
      f'{an.get("largest_component")} = '
      f'{100*(an.get("largest_component") or 0)/max(1, an["nodes"]):.1f}%')
# ⚠ 下面的度数是**窗口默认口径**（措施默认勾选放行）下的值。
#   旧值 243/127/103 属于「措施被低置信门槛整类挡在图外」那套口径。
for nm, want in [("中央网信办", 285), ("工业和信息化部", 142), ("公安部", 115)]:
    got = nodes.get(nm, {}).get("degree")
    check(f"窗口 {nm} 度 {want}", got == want, f"实测 {got}")

# 全库档（不传日期）—— 报告 §六 的规模句。⚠ 这一发实测 ~270s。
an_all = get("/api/graph/analysis?min_degree=2&node_types=" + NT)
nodes_all = {n["id"]: n for n in an_all["graph"]["nodes"]}
check("全库 12160 节点 / 29049 边 / 887 连通块",
      (an_all["nodes"], an_all["edges"], an_all["components"]) == (12160, 29049, 887),
      f'{an_all["nodes"]}/{an_all["edges"]}/{an_all["components"]}')
check("全库画布 300 节点 / 2993 边",
      (len(an_all["graph"]["nodes"]), len(an_all["graph"]["edges"])) == (300, 2993),
      f'{len(an_all["graph"]["nodes"])}/{len(an_all["graph"]["edges"])}')
# ⚠ 全库口径下度/介数第一是**泛称「网信办」**（3377 条实体记录），不是中央网信办 ——
#   早期文章里大量只写「网信办」。报告第四章把这列为「别名归并过窄」的已知局限，
#   这里钉住实测值，避免哪天写成「中央网信办第一」却没人发现。
check("全库度数第一 网信办 1446",
      nodes_all.get("网信办", {}).get("degree") == 1446,
      f'实测 {nodes_all.get("网信办", {}).get("degree")}')
check("全库度数次席 中央网信办 814",
      nodes_all.get("中央网信办", {}).get("degree") == 814,
      f'实测 {nodes_all.get("中央网信办", {}).get("degree")}')
check("全库工信部 456 / 公安部 464",
      (nodes_all.get("工业和信息化部", {}).get("degree"),
       nodes_all.get("公安部", {}).get("degree")) == (456, 464),
      f'{nodes_all.get("工业和信息化部", {}).get("degree")}/'
      f'{nodes_all.get("公安部", {}).get("degree")}')

print()
print("=" * 70)
print("⑥ 介数（窗口 0.2109/0.1196 + 聚焦 0.8487/0.0306；全库 0.1891/0.0900）")
print("=" * 70)
bt = sorted(((n["id"], n.get("betweenness", 0)) for n in an["graph"]["nodes"]),
            key=lambda kv: -kv[1])
check("窗口全图第一 = 中央网信办 0.2109",
      bt[0][0] == "中央网信办" and abs(bt[0][1] - 0.2109) < 0.001,
      f'{bt[0][0]} = {bt[0][1]}')
check("窗口全图第二 = 网信办 0.1196", bt[1][0] == "网信办" and abs(bt[1][1] - 0.1196) < 0.001,
      f'{bt[1][0]} = {bt[1][1]}')
r = bt[0][1] / bt[1][1]
check("窗口两者相差 1.76 倍", abs(r - 1.76) < 0.05, f"实测 {r:.2f}")

bt_a = sorted(((n["id"], n.get("betweenness", 0)) for n in an_all["graph"]["nodes"]),
              key=lambda kv: -kv[1])
check("全库全图第一 = 网信办 0.1891",
      bt_a[0][0] == "网信办" and abs(bt_a[0][1] - 0.1891) < 0.001,
      f'{bt_a[0][0]} = {bt_a[0][1]}')
check("全库全图第二 = 中央网信办 0.0900",
      bt_a[1][0] == "中央网信办" and abs(bt_a[1][1] - 0.0900) < 0.001,
      f'{bt_a[1][0]} = {bt_a[1][1]}')
check("全库两者相差 2.10 倍", abs(bt_a[0][1] / bt_a[1][1] - 2.10) < 0.05,
      f"实测 {bt_a[0][1]/bt_a[1][1]:.2f}")

g1 = get("/api/graph/analysis?focus=" + urllib.parse.quote("中央网信办")
         + "&hops=1&node_types=" + NT + WEB)
b1 = sorted(((n["id"], n.get("betweenness", 0)) for n in g1["graph"]["nodes"]),
            key=lambda kv: -kv[1])
check("聚焦 286 节点 / 1115 边", (g1["nodes"], g1["edges"]) == (286, 1115),
      f'{g1["nodes"]}/{g1["edges"]}')
check("聚焦第一 = 中央网信办 0.8487",
      b1[0][0] == "中央网信办" and abs(b1[0][1] - 0.8487) < 0.001,
      f'{b1[0][0]} = {b1[0][1]}')
check("聚焦第二 0.0306", abs(b1[1][1] - 0.0306) < 0.001, f'{b1[1][0]} = {b1[1][1]}')
check("聚焦两者相差 28 倍", 24 < b1[0][1] / b1[1][1] < 32, f"实测 {b1[0][1]/b1[1][1]:.0f} 倍")

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
    "2024": {"生成式AI": 11.2, "个人信息": 24.6, "未成年人": 20.8, "数据安全": 17.6},
    "2025": {"生成式AI": 15.1, "个人信息": 30.6, "未成年人": 14.5, "数据安全": 22.3},
    "2026": {"生成式AI": 17.4, "个人信息": 27.0, "未成年人": 19.0, "数据安全": 17.1},
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
print("⑧ 发文量（报告：1497 / 1541 / 1397，2025-09 峰值 193）")
print("=" * 70)
yr = Counter()
for x in tl["months"]:
    yr[x["month"][:4]] += x["total"]
for y, w in [("2024", 1497), ("2025", 1541), ("2026", 1397)]:
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
check("中央网信办 2965 + 浙江 1470 = 4435",
      d.get("cac", 0) + d.get("zjwx", 0) == 4435, str(d))
check("浙江网信网 1470 篇", d.get("zjwx") == 1470, str(d))
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
# ⚠ 证据接口**没有站点/日期参数**（app.py:2474 只有 src/dst/rel_type/limit），
#   所以它列的是**全库**这条边的全部证据；而边列表（/api/graph）默认按
#   db.graph 的 date_from="2024-01-01" 只数窗口内的。两个数不同是对的，
#   报告必须分别写清（全库 42 条 / 其中 2024 年以来 24 条）。
e1_n24 = q("SELECT COUNT(*) FROM relations r JOIN articles a "
           "ON a.id=r.article_id WHERE a.year>=2024 "
           "AND r.src_name='中央网信办' AND r.dst_name='农业农村部'")
e1_nall = q("SELECT COUNT(*) FROM relations WHERE src_name='中央网信办' "
            "AND dst_name='农业农村部'")
check("备选① 全库 42 条 / 窗口 24 条",
      len(e1) == 42 and e1_nall == 42 and e1_n24 == 24,
      f"接口 {len(e1)} / 全库 {e1_nall} / 窗口 {e1_n24}")
_ds = sorted(x["pub_day"] for x in e1 if x.get("pub_day"))
check("备选① 跨度 2019-05-17 ~ 2026-09-14",
      (_ds[0], _ds[-1]) == ("2019-05-17", "2026-09-14"), f"{_ds[0]} ~ {_ds[-1]}")
check("备选① 首条为 2025-03-07（列表按文章序，非日期序）",
      (e1[0].get("pub_day") or "") == "2025-03-07", str(e1[0].get("pub_day")))
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

# ⚠ 端点数原来**没人守**，报告里「12 个端点」就这么一直错着。
#   现在补上，并且口径必须与上面那条一致：**只算这一句**。
#   （按「含全部七个部门名的所有句子」去算是 10，
#    按「提到该方案的任意句子」去算是 12 —— 都是别的口径，不是这一句。）
_row = db.execute("""SELECT evidence FROM relations
                     WHERE evidence LIKE '%国家发展改革委%'
                       AND evidence LIKE '%生态环境部%'
                       AND evidence LIKE '%住房城乡建设部%'
                       AND evidence LIKE '%商务部%'
                     GROUP BY evidence ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
_seen, _n_ep = set(), 0
if _row:
    for _s, _d in db.execute("SELECT src_name, dst_name FROM relations WHERE evidence=?",
                             (_row[0],)):
        _seen.add(_s); _seen.add(_d)
check("七部门长句涉及 8 个端点", len(_seen) == 8, str(len(_seen)))

# 口径声明那段曾经把 co_issue 说成「同句共现」，与实测不符（证据句含联合类触发词）。
# ⚠ 全区间重抽后这条要分两档看：**分析窗 0 例外，全库 7 例外**。
#   7 条全部出在 2024 年前的两篇稿件（2016 年 1 条、2020 年 6 条）——
#   机构词典把「研发中心」「创新中心」「将其区域总部」当机构名后按同句并列建边
#   （例：文章 33811「将其区域总部、研发中心、创新中心等设立在合肥高新区」6 条、
#    文章 26164「中国科学院第十七次院士大会、中国工程院第十二次院士大会」1 条）。
#   报告 §一 与本文件同口径写明，报告 §五.8 列为已知局限。
_j = db.execute("""SELECT COUNT(*) FROM relations
                    WHERE rel_type='joint_issue'
                      AND evidence IS NOT NULL AND evidence<>''""").fetchone()[0]
_W = """rel_type='joint_issue' AND evidence IS NOT NULL AND evidence<>''
        AND evidence NOT LIKE '%联合%' AND evidence NOT LIKE '%会同%'
        AND evidence NOT LIKE '%共同%'"""
_t = db.execute("SELECT COUNT(*) FROM relations WHERE " + _W).fetchone()[0]
_t24 = db.execute(
    """SELECT COUNT(*) FROM relations r JOIN articles a ON a.id=r.article_id
        WHERE a.year>=2024 AND """
    + _W.replace("rel_type", "r.rel_type").replace("evidence", "r.evidence")).fetchone()[0]
check("窗口 joint_issue 证据句 100% 含联合类触发词", _t24 == 0, f"窗口 2024+ 里 {_t24} 条")
check("全库同类例外 7 条（2024 年前两篇稿件的垃圾实体，报告 §五.8 已写明）", _t == 7,
      f"{_j} 条中不含触发词 {_t} 条")

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
# ⚠ 这里**不能写死文件名**。报告 docx 已经被用户改名成「…(10.13).docx」，
#   写死的闸当场就红 —— 而且报错说的是「缺文件」，
#   看起来像生成步骤漏了，其实是我这里过期了。
#   改成按模式取：多个匹配时按修改时间取最新的那个，
#   这样用户加日期后缀、改版本名都不会再把闸弄坏。
_docx_cands = sorted((ROOT / "docs").glob("报告-网信办角色与权责边界*.docx"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
docx_path = _docx_cands[0] if _docx_cands else (
    ROOT / "docs" / "报告-网信办角色与权责边界.docx")
if not docx_path.exists():
    check("docx 已生成", False, "docs/ 下找不到 报告-网信办角色与权责边界*.docx，"
                                 "跑 py -3 md2docx.py")
else:
    if len(_docx_cands) > 1:
        print(f"  NOTE  docs/ 下有 {_docx_cands} 份报告 docx，"
              f"本闸只验最新的：{docx_path.name}")
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
    # （2026-10-15 补：真库全区间重跑后，报告多了「全库底数」这一档，
    #   这些数同样必须转进 docx；"24 条" 保留，因为报告还写了「其中 2024 年以来 24 条」）
    MUST = ["4433", "4435", "653 万字", "16507", "8499", "1195", "27.0%",
            "285", "142", "115", "0.2109", "0.1196", "0.8487", "0.0306",
            "1757", "4191", "126", "286", "1115",
            "5367", "2082", "561", "489", "63.1%",
            "1497", "1541", "1397", "193",
            "2965", "1470", "24 条", "3 条", "21 条", "8 个端点",
            "11.2%", "15.1%", "17.4%", "19.0%", "17.1%", "24.6%",
            "30.6%", "27.0%",
            # —— 全库底数（2000-2026 全区间）——
            "28919", "28850", "3827", "277355", "114807", "44342", "29049",
            "6887", "23.8%", "26080", "12160", "29724", "67.0%", "42 条",
            "2019-05-17", "3377", "0.1891", "0.0900", "1446", "814"]

    missing = [m for m in MUST if m not in full]
    check(f"关键数字 {len(MUST)} 个全部保留",
          not missing, f"缺 {missing}" if missing else "无缺失")
    # 口径措辞守卫：joint_issue 的判定有触发词门槛，
    # 报告若再写成「同句共现」就是与实测相反（证据句 100% 含联合类触发词）。
    check("报告未把 joint_issue 误称为同句共现",
          "同句共现" not in full.replace("不是任意同句共现", ""),
          "出现了未经否定的「同句共现」")

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