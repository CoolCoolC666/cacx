# -*- coding: utf-8 -*-
"""组员数据包 · 交付前核对闸

为什么单独有这道闸
  ================================================================
  数据包是**给组员的最终交付物**，而它由 _export_pack.py 现算生成。
  生成脚本对了不等于产物对了：上一版就出现过
    · 「疑似联合机关」整列是 `部` 这种残缺词和大量空值
    · 字段说明.md 写着 4416，实际库还是 4515 —— **文档与数据各说各话**
  格式漂亮、不报错、但内容是空的/旧的，这类问题只有拿产物本身对着声明核才抓得住。

  跑法：py -3 （测试）\_verify_datapack.py
  改完 _export_pack.py 必须重跑这个；两个一起过才算改完。
"""
import csv
import pathlib
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
# ⚠ 本文件在 （测试）/ 里，所以是 parent.parent。
#   写成 parent 会指到 （测试）/ 上去，那儿没有 组员数据包/ ——
#   而 sqlite 的 file: URI 模式会直接报 unable to open database file，
#   不会给你一个"看起来像空库"的假象，这一点比 Database() 好。
ROOT = pathlib.Path(__file__).resolve().parent.parent
PACK = ROOT / "组员数据包"

ok = bad = 0


def chk(label, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {label}" + (f"   {detail}" if detail else ""))
    else:
        bad += 1
        print(f"  FAIL  {label}" + (f"   {detail}" if detail else ""))


print("=" * 70)
print("组员数据包 · 交付前核对")
print("=" * 70)

# ---- db ----
p = PACK / "cacx-作业数据集.db"
c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True)
cols = [r[1] for r in c.execute("PRAGMA table_info(articles)")]
chk("articles 有 body_hash 列（字段说明已写）", "body_hash" in cols, str(cols[-2:]))
n = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
chk("articles = 4433（与说明一致）", n == 4433, f"实际 {n}")
n24 = c.execute("SELECT COUNT(*) FROM articles WHERE year>=2024").fetchone()[0]
chk("库内全部是 2024+", n == n24, f"2024+ {n24}/{n}")
body = c.execute("SELECT COUNT(*) FROM articles WHERE content_text IS NOT NULL "
                 "AND LENGTH(content_text)>0").fetchone()[0]
chk("有正文 = 4432", body == 4432, f"实际 {body}，空正文 {n-body}")
chk("content_empty=1 与「无正文」数一致",
    c.execute("SELECT COUNT(*) FROM articles WHERE COALESCE(content_empty,0)=1"
              ).fetchone()[0] == n - body)

# ⚠ r[0] 是索引名、r[1] 是建表 SQL —— 上一版这里取了 r[1]，
#   于是 idx 里装的是 SQL 全文，按名字永远匹配不上，
#   四条明明存在的索引全被判 FAIL。**核对脚本自己也会错**，
#   detail 里把实际值打出来才能一眼看出是判错还是数据错。
idx = {r[0] for r in c.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='articles'"
    " AND sql IS NOT NULL")}
for want, label in [("idx_articles_site_pubday", "site+pub_day"),
                    ("idx_articles_column", "column_name"),
                    ("idx_articles_pubday", "pub_day"),
                    ("idx_articles_site_column", "site+column_name")]:
    chk(f"索引 {label}", want in idx, str(sorted(idx)))

tables = {r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("ai_annotations", "ai_failures", "events", "external_links", "sync_runs"):
    chk(f"空壳表 {t} 存在（字段说明承诺了方便 join）", t in tables)
chk("无 entities/relations（与旧包一致，说明里写明只有 articles 有数据）",
    "entities" not in tables and "relations" not in tables)

# 成片的空值扫描：说明里承诺的字段不能整列为空
for colname in ("url", "title", "pub_day", "column_name", "content_text",
                "body_hash", "chars"):
    nulls = c.execute(f'SELECT COUNT(*) FROM articles '
                      f'WHERE "{colname}" IS NULL OR "{colname}"=\'\'').fetchone()[0]
    ratio = nulls / n
    chk(f"{colname} 没有大面积空缺", ratio < 0.10,
        f"空 {nulls} 条（{ratio*100:.1f}%）")
# 2024+ 的重复必须已清完（口径与清理脚本一致：同站+同标题+同 pub_day+同正文）
chk("包内无「同站+同标题+同日+同正文」的重复组", c.execute(
    "SELECT COUNT(*) FROM (SELECT site, title, pub_day FROM articles "
    "WHERE pub_day>='2024-01-01' AND title IS NOT NULL AND title<>'' "
    "GROUP BY site, title, pub_day HAVING COUNT(*)>1 "
    "AND COUNT(DISTINCT body_hash)=1)").fetchone()[0] == 0)
c.close()

# ---- articles.csv ----
f = PACK / "articles.csv"
lines = f.read_text(encoding="utf-8-sig").splitlines()
chk("articles.csv 行数 = 篇数 + 表头", len(lines) == n + 1, f"{len(lines)} vs {n}+1")
chk("articles.csv 表头未变", lines[0] == "id,站点,站点名,栏目,标题,发布日期,年份,"
    "月份,正文字数,来源字样,原文链接", lines[0][:60])

# ---- 栏目候选清单 ----
cl = list(csv.DictReader(
    (PACK / "栏目候选清单.csv").read_text(encoding="utf-8-sig").splitlines()))
chk("栏目候选清单 = 25 行", len(cl) == 25, f"{len(cl)}")
chk("各栏目篇数之和 == 库内篇数", sum(int(r["2024+篇数"]) for r in cl) == n,
    f"{sum(int(r['2024+篇数']) for r in cl)}")
win = [r for r in cl if r["落入300-500的窗口"] != "无"]
chk("达标窗口恰好 2 个", len(win) == 2,
    str([(r["站点"], r["栏目"], r["2024-2025篇数"]) for r in win]))

# ---- 联合发文线索 ----
cu = list(csv.DictReader(
    (PACK / "联合发文线索.csv").read_text(encoding="utf-8-sig").splitlines()))
chk("联合发文线索 = 221 条", len(cu) == 221, f"{len(cu)}")
WIDE = re.compile(r"(联合|印发|会同|联发|共同)")
chk("线索表每条标题都命中宽口径筛选词", all(WIDE.search(r["标题"] or "") for r in cu))
noorg = sum(1 for r in cu if not (r["疑似联合机关"] or "").strip())
chk("机关列不是成片空（旧版大量空值的问题）", noorg < len(cu) * 0.2,
    f"空 {noorg}/{len(cu)}")

# ---- 两级口径：候选清单的联合列必须是**窄**口径 ----
# 这条守的是一个我差点改坏的地方：旧包候选清单的联合列 (65/47) 比线索表 (73/51)
# 少，我一度当成"旧包自相矛盾"想统一成宽口径 —— 那会让组员的投票文档
# 从 39/30 静默变成 47/34。穷举后确认是**有意的两级设计**，14/14 精确命中旧 CSV。
NARROW = re.compile(r"(联合|印发|会同)")
db2 = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True)
db2.row_factory = sqlite3.Row
mis = 0
for r in cl:
    its = db2.execute("SELECT title, pub_day FROM articles WHERE site=? AND column_name=?",
                      (r["站点"], r["栏目"])).fetchall()
    w24 = sum(1 for x in its if NARROW.search(x["title"] or ""))
    w2425 = sum(1 for x in its if NARROW.search(x["title"] or "")
                and (x["pub_day"] or "") <= "2025-12-31")
    if w24 != int(r["联合发文类(2024+)"]) or w2425 != int(r["联合发文类(2024-2025)"]):
        mis += 1
        if mis <= 3:
            print(f"      ✗ {r['站点']}/{r['栏目']} 清单 {r['联合发文类(2024+)']}/"
                  f"{r['联合发文类(2024-2025)']} vs 窄口径重算 {w24}/{w2425}")
db2.close()
chk("候选清单联合列 == 窄口径(联合|印发|会同) 重算，25 行全对", mis == 0, f"{mis} 行不符")
chk("线索表条数 > 清单联合列合计（宽窄两级设计成立）",
    len(cu) > sum(int(r["联合发文类(2024+)"]) for r in cl),
    f"{len(cu)} vs {sum(int(r['联合发文类(2024+)']) for r in cl)}")

# ---- 字段说明.md 的承诺数字 ----
d = (PACK / "字段说明.md").read_text(encoding="utf-8")
for want in ("4433", "4432", "221", "39", "30", "28917", "body_hash"):
    chk(f"字段说明.md 含 {want}", want in d)
for stale in ("222 条", "28999", "46 MB", "47 篇", "34 篇", "4519 篇", "4517"):
    chk(f"字段说明.md 无过期数字 {stale}", stale not in d)

print(f"\n通过 {ok} 项，失败 {bad} 项")
sys.exit(1 if bad else 0)
