# -*- coding: utf-8 -*-
"""核对报告里每个数字。只读真库 + 只打运行中的服务，口径 = 报告口径。

⚠⚠ 2026-10-16 报告已按新要求重写，结构变了：
  · 四块 = 角色画像 / 权责边界（独立做·联合谁·文本里没做什么·中央与地方关系）/
    问题与措施观察 / 已知局限（数据层面）。术语统一为「中央网信办」，
    地方一律「浙江省委网信办」。
  · **删掉了全库底数那一整套**（28917 篇 / 12162 节点 / 泛称「网信办」1447 度等）。
    原因：课程要求「使用 2024 年之后的数据」，把 2000-2023 的底数搬进正文
    既超字数上限，又会让「中央网信办 vs 泛称网信办」的别名问题盖过主线。
    旧报告靠一个双口径结构解决这件事，新报告直接不用那批数据 —— 更干净。
  · 本文件随之改了三处断言组：⑬ 的 MUST 数字表、表格结构（只剩 1 张 6x3）、
    章节名；措辞守卫的豁免短语由「不是任意同句共现」改为「触发词门槛」；
    并**新增 ⑭**，把本轮新写的每个数字（独立/联合/执法/央地/脏实体）钉到真库。

⚠⚠ 数字口径（与旧版一致，未变）：
  · 报告的**分析窗** = 2024 年以来（4433 篇 / 4432 篇有正文 / 25 栏目 / 653 万字）。
    图谱、证据、主题占比、发文量、中央—地方对照都属于这一档。
  · 真库仍在持续采集，**篇数是快照**：数变了照实测改，不算断言写错；
    改的时候报告 md/docx、演示脚本、`_verify_demo_docs.py` 要一起改。
  · 图谱接口有两套默认：`/api/graph/analysis` 不传日期 = 全库（慢）；
    报告引用的都是 `date_from=2024-01-01` 那一档。全库那一档默认不跑
    （--full 才跑），理由见 FULL 变量处的说明。
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

# ⚠⚠ 2026-10-16 新增：默认**跳过**全库建图那组断言（⑤ 后半 + ⑥ 后半）。
#   起因是实测：本轮跑闸门时，这一组在 node_types 四类全勾下连续算了
#   **20 分钟仍未返回**（服务进程 CPU 打满 1493 秒、常驻内存涨到 1.04 GB），
#   而早先记的「270 秒」很可能是一次缓存命中的读数，不是真实冷启动耗时。
#
#   为什么可以默认跳过：重写后的报告**只用 2024+ 分析窗口径**，
#   全库底数那一整套（12162/29069/886、泛称「网信办」1447、介数 0.1892…）
#   已从正文删除。所以对「报告」而言这些断言不再守任何东西，
#   却让每次改报告都要先付 20 分钟 —— 最容易出的事就是有人等不及跳过整份闸门。
#
#   它们**没有删**：README、启动说明、演示脚本仍在引用全库视图的数，
#   得有人守。跑 `py -3 -X utf8 "（测试）\_verify_report.py" --full` 才包含。
#   跳过的条数会显式打出来，不让人以为「全跑过了」。
FULL = "--full" in sys.argv
if not FULL:
    print("⚠ 本次**不含**全库建图那组断言（--full 才跑）；跳过条数见下方标注。")


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
# ⚠ q 必须**接受绑定参数**。原来只写成 `lambda s:`，于是 2026-10-16 新增的 ⑭ 节
#   （带 IN (?,?,?) 的机构名查询）一调用就抛
#   「Incorrect number of bindings supplied. The current statement uses 6, and there are 0 supplied」
#   —— 而异常发生在 ⑬ **之后**，所以屏幕上看「前面全绿、最后崩」，
#   很容易误以为是 ⑬ 里的 docx 校验出了问题。绑定参数不是可选项。
q = lambda s, *p: db.execute(s, p).fetchone()[0]

print("=" * 70)
print("① 语料规模")
print("=" * 70)
# ⚠ 这些数是**库快照**（2026-10-15 上午实测）。真库一直在采集，
#   每采一批新文章这两行的数就会变 —— 那是数据变了，不是断言写错，
#   照实测值改这两行即可（演示脚本 §6 的教训同样适用于这里）。
n24 = q("SELECT COUNT(*) FROM articles WHERE year>=2024")
n24_txt = q("SELECT COUNT(*) FROM articles WHERE year>=2024 AND content_text "
            "IS NOT NULL AND LENGTH(content_text)>0")
check("2024 年以来 4433 篇", n24 == 4433, str(n24))
check("有正文 4432 篇", n24_txt == 4432, str(n24_txt))
check("栏目 25 个",
      q("SELECT COUNT(DISTINCT column_code) FROM articles WHERE year>=2024") == 25,
      str(q("SELECT COUNT(DISTINCT column_code) FROM articles WHERE year>=2024")))
zh = q("SELECT SUM(LENGTH(content_text)) FROM articles WHERE year>=2024")
check("正文 653 万字", 6_500_000 <= zh <= 6_550_000, f"{zh/1e4:.0f} 万字")

# 全库底数（报告 §六 的规模句用这一套；分母必须与分子同一档）
n_all = q("SELECT COUNT(*) FROM articles")
check("全库 28917 篇", n_all == 28917, str(n_all))
n_all_txt = q("SELECT COUNT(*) FROM articles WHERE content_text IS NOT NULL "
              "AND LENGTH(content_text)>0")
check("全库有正文 28849 篇", n_all_txt == 28849, str(n_all_txt))
check("全库栏目 26 个",
      q("SELECT COUNT(DISTINCT column_code) FROM articles") == 26,
      str(q("SELECT COUNT(DISTINCT column_code) FROM articles")))
zh_all = q("SELECT SUM(LENGTH(content_text)) FROM articles")
check("全库正文 3827 万字", 38_200_000 <= zh_all <= 38_400_000,
      f"{zh_all/1e4:.0f} 万字")
check("全库两站 23291 + 5626 = 28917",
      dict(db.execute("SELECT site, COUNT(*) FROM articles GROUP BY site")
           .fetchall()).get("cac") == 23291, "cac 应 23291")

print()
print("=" * 70)
print("② 抽取规模（报告 §一/§三 = 2024 年以来窗口；另核全库）")
print("=" * 70)
# ⚠ 这两条 SQL 必须带 year 过滤：报告写的是「2024 年以来的 4432 篇里抽出了…」，
#   不带过滤就是全库的数（114931 / 44420），分子分母一换口径结论全错。
W_E = "entities e JOIN articles a ON a.id=e.article_id WHERE a.year>=2024"
W_R = "relations r JOIN articles a ON a.id=r.article_id WHERE a.year>=2024"
ew = q("SELECT COUNT(DISTINCT e.name) FROM " + W_E)
check("窗口去重实体 16648", ew == 16648, str(ew))
rw = q("SELECT COUNT(*) FROM " + W_R)
check("窗口关系 8577", rw == 8577, str(rw))
ear = q("SELECT COUNT(DISTINCT e.article_id) FROM " + W_E)
check("窗口实体来源 3829 篇", ear == 3829, str(ear))
check("窗口实体行数 44223",
      q("SELECT COUNT(*) FROM " + W_E) == 44223,
      str(q("SELECT COUNT(*) FROM " + W_E)))
check("窗口关系去重对 4211",
      q("SELECT COUNT(DISTINCT r.src_name || '#' || r.dst_name) FROM " + W_R) == 4211,
      str(q("SELECT COUNT(DISTINCT r.src_name || '#' || r.dst_name) FROM " + W_R)))

# 全库（报告 §六 的「全库底数」）
ea = q("SELECT COUNT(DISTINCT name) FROM entities")
check("全库去重实体 114931", ea == 114931, str(ea))
check("全库实体行数 277651",
      q("SELECT COUNT(*) FROM entities") == 277651,
      str(q("SELECT COUNT(*) FROM entities")))
ra = q("SELECT COUNT(*) FROM relations")
check("全库关系 44420", ra == 44420, str(ra))
check("全库关系去重对 29069",
      q("SELECT COUNT(DISTINCT src_name || '#' || dst_name) FROM relations") == 29069,
      str(q("SELECT COUNT(DISTINCT src_name || '#' || dst_name) FROM relations")))
check("全库关系来源 6893 篇",
      q("SELECT COUNT(DISTINCT article_id) FROM relations") == 6893,
      str(q("SELECT COUNT(DISTINCT article_id) FROM relations")))

print()
print("=" * 70)
print("③ 关系类型分布（窗口 5425/2098/561/493；全库 29782/9740/2729/2169）")
print("=" * 70)
for rt, want in [("joint_issue", 5425), ("issue", 2098), ("target", 561), ("adopt", 493)]:
    got = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='" + rt + "'")
    check(f"窗口 {rt} = {want}", got == want, str(got))
ci = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue'")
check("窗口 joint_issue 占 63.3%", abs(100 * ci / rw - 63.3) < 0.15,
      f"实测 {100*ci/rw:.1f}%")
check("无证据关系为 0",
      q("SELECT COUNT(*) FROM relations WHERE evidence IS NULL OR evidence=''") == 0)
# 全库
for rt, want in [("joint_issue", 29782), ("issue", 9740), ("adopt", 2729),
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
print("④ 覆盖率（窗口 1201/4433 = 27.1%（对 4432 篇有正文为 27.1%）；全库 6893/28917 = 23.8%）")
print("=" * 70)
cov = q("SELECT COUNT(DISTINCT r.article_id) FROM " + W_R)
check("窗口关系覆盖 1201 篇 / 27.1%（分母 2024+ 全部 4433 篇；有正文 27.1%）",
      cov == 1201 and abs(100 * cov / n24 - 27.1) < 0.15,
      f"{cov} 篇 = {100*cov/n24:.1f}%")
# ⚠ 全库的分母必须是 28917（全库文章数）。
#   曾经出现过「6887 篇 / 4432 篇 = 156%」—— 分子是全库、分母是窗口，自相矛盾。
cov_a = q("SELECT COUNT(DISTINCT article_id) FROM relations")
check("全库关系覆盖 6893 篇 / 23.8%（分母 28917）",
      cov_a == 6893 and abs(100 * cov_a / n_all - 23.8) < 0.15,
      f"{cov_a} 篇 = {100*cov_a/n_all:.1f}%")
have = q("SELECT COUNT(DISTINCT id) FROM articles WHERE "
         "id IN (SELECT article_id FROM entities) "
         "OR id IN (SELECT article_id FROM relations)")
check("全库有抽取结果 26094 篇 / 90.2%",
      have == 26094 and abs(100 * have / n_all - 90.2) < 0.2,
      f"{have} 篇 = {100*have/n_all:.1f}%")
check("窗口有抽取结果 3829 篇 / 86.4%",
      abs(100 * 3829 / n24 - 86.3) < 0.1, f"{100*3829/n24:.1f}%")
# 时间跨度：全库 vs 窗口（报告 §六 与 §一 各引一档）
_sp = db.execute("SELECT MIN(a.pub_day), MAX(a.pub_day) FROM relations r "
                 "JOIN articles a ON a.id=r.article_id").fetchone()
check("全库关系跨度 2000-09-30 ~ 2026-10-14",
      tuple(_sp) == ("2000-09-30", "2026-10-14"), str(tuple(_sp)))
_sp2 = db.execute("SELECT MIN(a.pub_day), MAX(a.pub_day) FROM relations r "
                  "JOIN articles a ON a.id=r.article_id WHERE a.year>=2024").fetchone()
check("窗口关系跨度 2024-01-02 ~ 2026-10-14",
      tuple(_sp2) == ("2024-01-02", "2026-10-14"), str(tuple(_sp2)))

print()
print("=" * 70)
print("⑤ 图谱口径与度数（网页首屏 2024-01 档 + 全库档）")
print("=" * 70)
# ⚠ 两件事都要显式说明，否则这个闸会红/会慢：
#   1) node_types 必须全勾：不带时服务端按「措施被低置信门槛整类挡在图外」
#      的旧口径返回（实测 1631/3958/115），而报告的「默认分析口径」是措施放行的那套。
#   2) date_from 必须显式带 2024-01-01：报告/演示脚本/网页首屏都是这一档
#      （index.html 的 gp_dfrom 默认值）。不带 = 全库口径（1 万+ 节点、极慢，
#      且默认不跑，见 FULL 说明）。
#      而且要在全图上算介数（实测 268s，本文件会等）。
NT = "org,policy,problem,measure"
WEB = "&date_from=2024-01-01"
an = get("/api/graph/analysis?min_degree=2&node_types=" + NT + WEB)
nodes = {n["id"]: n for n in an["graph"]["nodes"]}
check("窗口 1761 节点 / 4211 边 / 126 连通块",
      (an["nodes"], an["edges"], an["components"]) == (1761, 4211, 126),
      f'{an["nodes"]}/{an["edges"]}/{an["components"]}')
check("窗口最大连通块 1410（80.1%）",
      an.get("largest_component") == 1410,
      f'{an.get("largest_component")} = '
      f'{100*(an.get("largest_component") or 0)/max(1, an["nodes"]):.1f}%')
# ⚠ 下面的度数是**窗口默认口径**（措施默认勾选放行）下的值。
#   旧值 243/127/103 属于「措施被低置信门槛整类挡在图外」那套口径。
for nm, want in [("中央网信办", 285), ("工业和信息化部", 142), ("公安部", 115)]:
    got = nodes.get(nm, {}).get("degree")
    check(f"窗口 {nm} 度 {want}", got == want, f"实测 {got}")

# 全库档（不传日期）—— 守 README / 启动说明 / 演示脚本引用的那批数。
# ⚠ 这一发实测远超预期：四类 node_types 全勾时连续跑 20 分钟未返回。
#   所以默认跳过，要跑显式加 --full。
# 报告正文已不用全库口径（见文件头说明），跳过不损失对报告的覆盖。
if not FULL:
    print("  SKIP  全库建图组（③④⑥ 的全库部分）：本次未跑，"
          "加 --full 才包含（实测冷启动 > 20 分钟）")
else:
    an_all = get("/api/graph/analysis?min_degree=2&node_types=" + NT)
    nodes_all = {n["id"]: n for n in an_all["graph"]["nodes"]}
    check("全库 12162 节点 / 29069 边 / 886 连通块",
          (an_all["nodes"], an_all["edges"], an_all["components"]) == (12162, 29069, 886),
          f'{an_all["nodes"]}/{an_all["edges"]}/{an_all["components"]}')
    check("全库画布 300 节点 / 2995 边",
          (len(an_all["graph"]["nodes"]), len(an_all["graph"]["edges"])) == (300, 2995),
          f'{len(an_all["graph"]["nodes"])}/{len(an_all["graph"]["edges"])}')
    # ⚠ 全库口径下度/介数第一是**泛称「网信办」**（3381 条实体记录），不是中央网信办 ——
    #   早期文章里大量只写「网信办」。README 与演示脚本都据此说明「两个名字的度与介数
    #   要分开看、不能相加」，这里钉住实测值。
    check("全库度数第一 网信办 1447",
          nodes_all.get("网信办", {}).get("degree") == 1447,
          f'实测 {nodes_all.get("网信办", {}).get("degree")}')
    check("全库度数次席 中央网信办 814",
          nodes_all.get("中央网信办", {}).get("degree") == 814,
          f'实测 {nodes_all.get("中央网信办", {}).get("degree")}')
    check("全库工信部 456 / 公安部 464",
          (nodes_all.get("工业和信息化部", {}).get("degree"),
           nodes_all.get("公安部", {}).get("degree")) == (456, 464),
          f'{nodes_all.get("工业和信息化部", {}).get("degree")}/'
          f'{nodes_all.get("公安部", {}).get("degree")}')

    bt_a = sorted(((n["id"], n.get("betweenness", 0)) for n in an_all["graph"]["nodes"]),
                  key=lambda kv: -kv[1])
    check("全库全图第一 = 网信办 0.1892",
          bt_a[0][0] == "网信办" and abs(bt_a[0][1] - 0.1892) < 0.001,
          f'{bt_a[0][0]} = {bt_a[0][1]}')
    check("全库全图第二 = 中央网信办 0.0899",
          bt_a[1][0] == "中央网信办" and abs(bt_a[1][1] - 0.0899) < 0.001,
          f'{bt_a[1][0]} = {bt_a[1][1]}')
    check("全库两者相差 2.10 倍", abs(bt_a[0][1] / bt_a[1][1] - 2.10) < 0.05,
          f"实测 {bt_a[0][1]/bt_a[1][1]:.2f}")

print()
print("=" * 70)
print("⑥ 介数（窗口 0.2109/0.1201 + 聚焦 0.8486/0.0306）")
print("=" * 70)
bt = sorted(((n["id"], n.get("betweenness", 0)) for n in an["graph"]["nodes"]),
            key=lambda kv: -kv[1])
check("窗口全图第一 = 中央网信办 0.2109",
      bt[0][0] == "中央网信办" and abs(bt[0][1] - 0.2109) < 0.001,
      f'{bt[0][0]} = {bt[0][1]}')
check("窗口全图第二 = 网信办 0.1201", bt[1][0] == "网信办" and abs(bt[1][1] - 0.1201) < 0.001,
      f'{bt[1][0]} = {bt[1][1]}')
r = bt[0][1] / bt[1][1]
check("窗口两者相差 1.76 倍", abs(r - 1.76) < 0.05, f"实测 {r:.2f}")

g1 = get("/api/graph/analysis?focus=" + urllib.parse.quote("中央网信办")
         + "&hops=1&node_types=" + NT + WEB)
b1 = sorted(((n["id"], n.get("betweenness", 0)) for n in g1["graph"]["nodes"]),
            key=lambda kv: -kv[1])
check("聚焦 286 节点 / 1116 边", (g1["nodes"], g1["edges"]) == (286, 1116),
      f'{g1["nodes"]}/{g1["edges"]}')
check("聚焦第一 = 中央网信办 0.8486",
      b1[0][0] == "中央网信办" and abs(b1[0][1] - 0.8486) < 0.001,
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
    "2024": {"生成式AI": 11.2, "个人信息": 24.5, "未成年人": 20.8, "数据安全": 17.6},
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
print("⑧ 发文量（报告：1495 / 1541 / 1397，2025-09 峰值 193）")
print("=" * 70)
yr = Counter()
for x in tl["months"]:
    yr[x["month"][:4]] += x["total"]
for y, w in [("2024", 1495), ("2025", 1541), ("2026", 1397)]:
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
check("中央网信办 2965 + 浙江 1468 = 4433",
      d.get("cac", 0) + d.get("zjwx", 0) == 4433, str(d))
check("浙江网信网 1468 篇", d.get("zjwx") == 1468, str(d))
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

    # ---------------------------------------------------------- ⑬-b 漂移
    # ⚠⚠ 本文件此前的 103 项**全部只读 docx**，没有任何一项读 md。
    #   也就是说：改完 md 忘了重新生成 docx，交付的 Word 版会静默停在
    #   旧内容，而这 103 项照样全绿。实测就是这样：改 md 注入错误，
    #   闸门毫无反应 —— 因为它压根没看 md。
    #   报告以 md 为主稿、docx 由 md2docx.py 生成，这个漂移方向是常态。
    #
    # 做法：拿 md 现场重新生成一份到临时文件，与现有 docx **比文本**。
    # 不用时间戳（转档器重跑一次 mtime 就变了，比不出内容差异），
    # 不用手工维护的哈希（忘了更新就恒真）。比对内容是唯一诚实的判据。
    _md_path = ROOT / "docs" / "报告-网信办角色与权责边界.md"
    _md_cands = sorted(ROOT.glob("docs/报告-网信办角色与权责边界*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if _md_cands:
        _md_path = _md_cands[0]
    _drift = None
    if not _md_path.exists():
        check("md↔docx 同源（无漂移）", False,
              f"找不到 {_md_path.name}，无法比对")
    else:
        import subprocess as _sp
        import tempfile as _tf
        # ⚠⚠ **必须显式给 -o**。md2docx.py 不给 -o 时走
        #   `out = src.with_suffix(".docx")` —— 也就是**就地覆盖 docs/ 下
        #   那份真 docx**。校验脚本为了「看一眼」把原件改掉，
        #   等于把要比对的两边一起污染，比出来永远一致。
        #   这个错一旦犯，闸门不但没用，还会主动销毁证据。
        _tmpdir = Path(_tf.mkdtemp(prefix="cacx_md2docx_"))
        _fresh = _tmpdir / "fresh.docx"
        _r = _sp.run([sys.executable, "-X", "utf8",
                      str(ROOT / "md2docx.py"),
                      str(_md_path), "-o", str(_fresh)],
                     cwd=str(ROOT), capture_output=True, timeout=300)
        # ⚠ **不要用 text=True**。子进程按 Windows 默认编码（GBK/cp936）
        #   写 stdout，而 text=True 走 UTF-8 解码 —— 转档一旦真失败，
        #   拿到的不是真实报错，而是一个 UnicodeDecodeError：
        #   故障原因被解码错误顶替，等于在最需要诊断的时候瞎了。
        #   这里收字节自己按 utf-8 解，解不了就替换，绝不抛。
        _out = (_r.stdout or b"").decode("utf-8", "replace")
        _err = (_r.stderr or b"").decode("utf-8", "replace")
        if _r.returncode != 0 or not _fresh.exists():
            _drift = (f"重新生成失败 rc={_r.returncode} "
                      f"{( _err.strip() or _out.strip())[-200:]}")
        else:
            _d2 = _docx.Document(str(_fresh))
            # ⚠ 别把列表名和单元格循环变量取成同一个 —— 这里踩过：
            #   _c2 既是 chunks 列表又是 cell，一 append 就变成
            #   「_Cell.append」，整段炸掉，⑬ 之后所有检查全都不跑，
            #   报错还指向一个跟真实原因无关的行。
            _chunks2 = [p.text for p in _d2.paragraphs]
            for _t2 in _d2.tables:
                for _row2 in _t2.rows:
                    for _cell2 in _row2.cells:
                        _chunks2.append(_cell2.text)
            _fresh_text = "\n".join(_chunks2)
            if _fresh_text.strip() == full.strip():
                _drift = "同源"
            else:
                import difflib as _dl
                _a, _b = full.splitlines(), _fresh_text.splitlines()
                _sm = _dl.SequenceMatcher(None, _a, _b)
                _ops = [o for o in _sm.get_opcodes() if o[0] != "equal"]
                _drift = (f"有 {len(_ops)} 处不一致 "
                          f"(md 重新生成 vs 现有 docx)，例如：")
                for o in _ops[:3]:
                    _tag = {"replace": "内容不同", "delete": "仅 docx 有",
                            "insert": "仅 md 有"}.get(o[0], o[0])
                    _ex_b = _b[o[3]] if o[3] < o[4] else (
                        _b[o[4] - 1] if o[4] else "")
                    _drift += f"\n         - {_tag}：{( _ex_b or '')[:50]}"
        # 临时目录走项目内临时池，便于统一回收
        try:
            import shutil as _sh
            _sh.rmtree(_tmpdir, ignore_errors=True)
        except Exception:
            pass
    check("md↔docx 同源（无漂移）", _drift == "同源",
          _drift if _drift != "同源" else "重新生成后逐段一致")

    # 关键数字一个都不能少 —— Word 版最常见的失真就是转档时漏句/漏行
    # （2026-10-15 补：真库全区间重跑 + 补抽 10-12~10-14 新采的 19 篇后，
    #   窗口/全库两套数都动了，这些数同样必须转进 docx；
    #   "24 条" 保留，因为报告还写了「其中 2024 年以来 24 条」）
    # ⚠ 2026-10-16 报告按新要求重写（四块：角色画像／权责边界／问题与措施观察／
    #   已知局限；统称「中央网信办」，新增「文本里没做什么」与中央—地方关系专节）。
    #   下面的 MUST、表格结构、章节名都随之换过一轮 —— 数字全部来自
    #   （测试）/_t10_report_stats.py 的 2024+ 只读取数（口径 = 分析窗）。
    MUST = ["4432", "653 万字", "2965", "1468", "25 个栏目",
            "16648", "8577", "1201", "27.1%", "561 条",
            "1761", "4211", "126", "285", "0.2109", "0.1201",
            "286", "1116", "0.8486", "0.0306",
            "5425", "63.3%", "1518", "348", "137", "601", "87", "74",
            "21 条", "8 个端点", "15 条", "17 条", "11 条", "18 条",
            "91 条", "45 条", "39 条",
            "1495", "1541", "1397", "193",
            "11.2%", "15.1%", "17.4%", "30.6%", "27.0%",
            "14 条", "26 条", "20 条", "328 次", "35 次",
            "57 篇", "37 篇", "67 篇", "59 篇",
            "359/5425", "6.6%", "12140", "4317", "211", "10 个"]

    missing = [m for m in MUST if m not in full]
    check(f"关键数字 {len(MUST)} 个全部保留",
          not missing, f"缺 {missing}" if missing else "无缺失")
    # 口径措辞守卫：joint_issue 的判定有触发词门槛，
    # 报告若再写成「同句共现」就是与实测相反（证据句 100% 含联合类触发词）。
    #
    # ⚠ 说明文字必须**跟着结论变**，不能红绿都印同一句失败措辞 ——
    #   那样一行 PASS 会读成「出了这个问题」，看的人很快就学会忽略 detail，
    #   闸门也就跟着失效了（失败措辞印在 PASS 上 = 误报，误报会杀死闸门）。
    #
    # ⚠ 豁免短语改为「触发词门槛」——重写后的报告不再写「不是任意同句共现」，
    #   而是正面写明「设触发词门槛」。若连这个正面表述也一并删掉，
    #   断言就退化成「什么都没检查」，那种 PASS 不构成证据，故显式报出来。
    _EX = "触发词门槛"
    _n_ex = full.count(_EX)
    _left = full.replace(_EX, "")
    _bad_joint = "同句共现" in _left
    check("报告未把 joint_issue 误称为同句共现，且写明了触发词门槛",
          (not _bad_joint) and _n_ex > 0,
          (f"出现了未经否定的「同句共现」" if _bad_joint
           else (f"已豁免 {_n_ex} 处门槛表述，无残留误称" if _n_ex
                 else "⚠ 报告里没有「触发词门槛」，断言空跑 —— 检查是否被改写")))
    # 术语统一：正文不得把中央与地方网信办混称。新报告两处「浙江省委网信办」
    # 是地方样本的正确写法，这里只守「浙江网信办」这一易混写法不出现。
    _zjmix = full.count("浙江网信办")
    check("未出现易混写法「浙江网信办」（地方一律「浙江省委网信办」）",
          _zjmix == 0, f"出现 {_zjmix} 次" if _zjmix else "0 次")

    # markdown 残留：转换器失手时正文会露出星号或反引号
    residue = [tok for tok in ("**", "`") if tok in full]
    check("无 markdown 标记残留", not residue, f"残留 {residue}" if residue else "")

    # 表格结构：问题与措施观察表 = 表头 + 5 行观察 = 6 行 x 3 列
    shapes = [(len(t.rows), len(t.columns)) for t in d.tables]
    check("问题与措施观察表 6x3（表头+5 类观察）", (6, 3) in shapes, f"实际表格 {shapes}")

    # 六个一级章节标题都在
    heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
    for need in ("一、方法与口径", "二、角色画像", "三、权责边界",
                 "四、问题与措施观察", "五、已知局限", "六、结论"):
        check(f"章节「{need}」", any(need in h for h in heads))

print()
print("=" * 70)
print("⑭ 权责边界新增断言（§3.1 / §3.2 / §3.3 / §3.4 / §五 的每个新数字）")
print("=" * 70)
# ⚠ 这一节是 2026-10-16 重写报告时新增的。旧报告没有「独立/联合/文本里没做什么」
#   这套分法，这些数字当时没有任何断言守着 —— 重写后必须逐条钉住，
#   否则下次改报告又会悄悄漂。
_C = ("中央网信办", "国家互联网信息办公室", "国家网信办")
_CIN = ",".join("?" * len(_C))


def qc(s):
    """跑一条带 _CIN 占位符的 SQL。

    ⚠ 本节每条查询都是 **两个** IN 子句（src_name IN / dst_name IN），
    所以要传两遍名字（6 个绑定，不是 3 个）。写成只传一遍就会得到
    「uses 6, and there are 0/3 supplied」—— 而且它发生在 ⑬ 全绿之后，
    屏幕上看像是 docx 校验崩了，实际是这儿的绑定数量不对。
    """
    return db.execute(s, _C + _C).fetchone()[0]


_c_all = qc("SELECT COUNT(*) FROM " + W_R + " AND (r.src_name IN (" + _CIN +
            ") OR r.dst_name IN (" + _CIN + "))")
check("§3.1 中央网信办相关关系 1518 条", _c_all == 1518, str(_c_all))
for _rt, _want in [("issue", 348), ("adopt", 137)]:
    _g = qc("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='" + _rt +
            "' AND (r.src_name IN (" + _CIN + ") OR r.dst_name IN (" + _CIN + "))")
    check(f"§3.1 中央 {_rt} = {_want}", _g == _want, str(_g))
_jc = qc("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue' AND (r.src_name IN (" +
         _CIN + ") OR r.dst_name IN (" + _CIN + "))")
check("§3.2 中央 joint_issue 959 条", _jc == 959, str(_jc))
_src = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue' AND r.src_name='中央网信办'")
_dst = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue' AND r.dst_name='中央网信办'")
check("§3.2 源端 601 / 目标端 87",
      (_src, _dst) == (601, 87) and abs(100 * _src / (_src + _dst) - 87) < 1.0,
      f"{_src}/{_dst} = {100*_src/(_src+_dst):.1f}%")

# §3.3 其一：与执法机关的 74 条关系**全部**是 joint_issue，target/adopt 各 0 条。
#   这条是「文本里没做什么」的全部证据，所以要两头都钉：
#   既钉总数 74，也钉「执法机关那一侧没有一条非 joint_issue」。
_enf_all = qc("SELECT COUNT(*) FROM " + W_R + " AND (r.src_name IN (" + _CIN +
              ") OR r.dst_name IN (" + _CIN + ")) AND ((r.src_name LIKE '%公安部%' "
              "OR r.dst_name LIKE '%公安部%' OR r.src_name LIKE '%市场监管%' "
              "OR r.dst_name LIKE '%市场监管%'))")
check("§3.3 中央 × 执法机关 74 条", _enf_all == 74, str(_enf_all))
_enf_other = qc("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type<>'joint_issue' "
                "AND (r.src_name IN (" + _CIN + ") OR r.dst_name IN (" + _CIN + ")) "
                "AND ((r.src_name LIKE '%公安部%' OR r.dst_name LIKE '%公安部%' "
                "OR r.src_name LIKE '%市场监管%' OR r.dst_name LIKE '%市场监管%'))")
check("§3.3 其中 target/adopt 确为 0 条", _enf_other == 0, str(_enf_other))

# §3.3 其二：2023 职责划出后的文本侧证据。
for kw, n_all, n_cent, n_data in [("数字中国", 57, 17, None),
                                  ("智慧城市", 37, 14, None),
                                  ("数据局", 67, 40, 59)]:
    _b = q("SELECT COUNT(*) FROM articles a WHERE a.year>=2024 AND a.content_text LIKE ?",
           f"%{kw}%")
    _c = q("SELECT COUNT(*) FROM articles a WHERE a.year>=2024 AND a.content_text LIKE ? "
           "AND a.content_text LIKE '%中央网信办%'", f"%{kw}%")
    check(f"§3.3 「{kw}」正文命中 {_b} 篇、含中央网信办 {_c} 篇",
          _b == n_all and _c == n_cent, f"{_b}/{_c}")
    if n_data is not None:
        _d = q("SELECT COUNT(*) FROM articles a WHERE a.year>=2024 AND a.content_text LIKE ? "
               "AND a.content_text LIKE '%国家数据局%'", f"%{kw}%")
        check(f"§3.3 「{kw}」含国家数据局 {n_data} 篇", _d == n_data, str(_d))

# §3.4 中央 × 省级
_prov = qc("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue' "
           "AND (r.src_name IN (" + _CIN + ") OR r.dst_name IN (" + _CIN + ")) "
           "AND ((r.src_name LIKE '%省委网信办%' OR r.dst_name LIKE '%省委网信办%') "
           "OR (r.src_name LIKE '%网信浙江%' OR r.dst_name LIKE '%网信浙江%') "
           "OR (r.src_name LIKE '浙江省委%' OR r.dst_name LIKE '浙江省委%'))")
check("§3.4 中央 × 省级网信办 joint_issue 14 条", _prov == 14, str(_prov))
_zj = q("SELECT COUNT(*) FROM " + W_R + " AND (r.src_name='浙江省委网信办' OR r.dst_name='浙江省委网信办')")
check("§3.4 浙江省委网信办本体 26 条关系", _zj == 26, str(_zj))
_zjc = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue' AND "
         "(r.src_name='浙江省委网信办' OR r.dst_name='浙江省委网信办')")
check("§3.4 其中 joint_issue 20 条", _zjc == 20, str(_zjc))
for nm, want in (("中央网信办", 328), ("浙江省委网信办", 35)):
    _e = q("SELECT COUNT(*) FROM entities e JOIN articles a ON a.id=e.article_id "
           "WHERE a.year>=2024 AND a.site='zjwx' AND e.type='org' AND e.name='" + nm + "'")
    check(f"§3.4 zjwx 站机构「{nm}」{want} 条", _e == want, str(_e))

# §五 局限第 4/5 条
_bad = q("SELECT COUNT(*) FROM " + W_R + " AND r.rel_type='joint_issue' AND "
         "(LENGTH(r.src_name)>10 OR r.src_name LIKE '%由%' OR r.src_name LIKE '%根据%' "
         "OR r.src_name LIKE '%联合主办%' OR r.src_name LIKE '%的活动%')")
check("§五 可疑端点 359 条 / 6.6%", _bad == 359 and abs(100 * _bad / ci - 6.6) < 0.1,
      f"{_bad} 条 = {100*_bad/ci:.1f}%")
for _ty, _want in (("org", 12140), ("policy", 4317), ("measure", 211), ("problem", 10)):
    _n = q("SELECT COUNT(DISTINCT e.name) FROM " + W_E + " AND e.type='" + _ty + "'")
    check(f"§五 去重实体 {_ty} = {_want}", _n == _want, str(_n))

db.close()
print()
print("=" * 70)
print(f"通过 {ok} 项，失败 {fail} 项")
print("=" * 70)
sys.exit(1 if fail else 0)