# -*- coding: utf-8 -*-
r"""重建 数据包/ 的四个产物（给使用者的轻量数据集）。

⚠ 上一版生成脚本已被回收站清理，规则丢失。本脚本是**照着旧产物反推复原**的，
   反推过程有据可查，别再凭印象改口径：

  栏目候选清单.csv —— 用「旧 db + 旧 CSV」逐行比对，25/25 行零误差，规则钉死：
    · 2024+篇数      = COUNT(*) GROUP BY site, column_code, column_name
    · 2024-2025篇数  = SUM(pub_day <= '2025-12-31')
    · 2024单年篇数   = SUM(year='2024')
    · 覆盖月份数     = COUNT(DISTINCT month)
    · 落入300-500的窗口 = 24-25 窗口篇数落在 [300,500] 写 '2024-2025'，否则 '无'
    · 推荐理由       = 达标时拼「【窗口】共N/M篇，符合评审方300-500要求；
                        联合发文K篇，机构合作网络有真素材；跨X个月，时间轴够用」；
                       不达标时 月数>=20 写「跨X个月，时间轴够用」，
                       否则「篇数不达标，仅作参考」

  联合发文线索.csv —— 筛选规则也精确复原了：标题命中 (联合|印发|会同|联发|共同)。
    实测旧库全库标题命中 = 222，与旧 CSV 行数**完全相等**，且 222 条自身 100% 命中。

  ⚠ 我一度以为「候选清单的联合列」与「线索表」口径自相矛盾（168 vs 222），
    打算把两者统一 —— **那个判断是错的**。穷举后确认旧包是**有意的两级设计**：
      · 线索表用**宽**口径 (联合|印发|会同|联发|共同)：放宽召回，交人去筛
      · 候选清单的两列联合数用**窄**口径 (联合|印发|会同)：168，是 222 的子集
    窄口径在 7 个栏目 × 2 个时间窗上 **14/14 精确命中**旧 CSV：
      cac/网信要闻 65·39    zjwx/网信要闻 47·30   cac/时政要闻 22·19
      cac/网信发布 11·10    zjwx/治理监管 4·3     cac/地方网信 1·1
      cac/互联网内容管理 2·1
    所以两个口径**各自照原样保留** —— 使用者的投票文档不会因为换了脚本而数字漂移。
    搜索过程见 _tmp_brute_titles.py / _tmp_verify_narrow.py 的记录。

  ⚠ 唯一没能复原的是线索表的「疑似联合机关」那一列。原脚本用的是一套已丢失的
    粗糙正则，产出过 `部`、`部 / 中央网信办` 这种残缺词，还有大量空值。
    改用 cacx.extract._org_mentions()，与主库抽取**同源**，使用者可以自己复算。
    代价：这一列的取值与旧版不同，字段说明.md 已注明口径。

跑法：py -3 （测试）\_export_pack.py [--dry]
"""
import csv
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cacx.extract import _org_mentions   # noqa: E402

PACK = ROOT / "数据包"
SRC_DB = ROOT / "data" / "cacx.db"
OUT_DB = PACK / "cacx-数据集.db"
DRY = "--dry" in sys.argv

# ── 两级口径，各自照旧，别合并（见模块 docstring 的 14/14 验证）──
# 宽口径：筛「联合发文线索.csv」，故意放宽召回交人工去筛
JOINT_WIDE = re.compile(r"(联合|印发|会同|联发|共同)")
# 窄口径：算「栏目候选清单.csv」的 联合发文类 两列
# ⚠ 上一版这里两个地方共用了一个宽口径，于是 A/B 的联合数从旧包的 39/30
#   变成 47/34 —— 使用者手上的投票文档会莫名其妙换数。窄口径才是旧包的真实规则。
JOINT_NARROW = re.compile(r"(联合|印发|会同)")
ORG_SHOW = 3            # 疑似联合机关最多列几个


def to_recycle(p: Path) -> bool:
    """旧产物送回收站（使用者的删除口径是「一切可恢复」），并确认它真消失了。"""
    if not p.exists():
        return True
    verb = "DeleteDirectory" if p.is_dir() else "DeleteFile"
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
          f"[Microsoft.VisualBasic.FileIO.FileSystem]::{verb}("
          f"'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    import time as _t
    for _ in range(60):
        if not p.exists():
            return True
        _t.sleep(0.5)
    return False


print("=" * 70)
print("数据包 · 重建")
print("=" * 70)

src = sqlite3.connect(f"file:{SRC_DB.as_posix()}?mode=ro", uri=True)
src.row_factory = sqlite3.Row
n_2024 = src.execute("SELECT COUNT(*) FROM articles WHERE year>=2024"
                     ).fetchone()[0]
print(f"  源库 {SRC_DB.name}   2024+ 共 {n_2024} 篇")

TARGETS = [
    OUT_DB,
    PACK / "articles.csv",
    PACK / "栏目候选清单.csv",
    PACK / "联合发文线索.csv",
]
if not DRY:
    for t in TARGETS:
        if not to_recycle(t):
            print(f"\n🔴 中止：旧产物没能送进回收站（可能正被占用）：{t}")
            print("   请关掉 Excel / 资源管理器里打开的窗口后重跑。")
            sys.exit(3)
    print(f"  旧产物已送回收站（{len(TARGETS)} 个）")

if DRY:
    print("\n--dry：什么都没生成。")
    sys.exit(0)

# ---------------------------------------------------------------- 1 库
print()
print("=" * 70)
print("1  cacx-数据集.db")
print("=" * 70)
# 用主库的真实 DDL，保证使用者拿 cacx 代码能直接读；
# 但只灌 2024+ 的文章，其余表留空壳（字段说明里写明了这一点）。
ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                  "AND name='articles'").fetchone()[0]
out = sqlite3.connect(str(OUT_DB))
out.execute(ddl)
# 索引：沿用旧包的 4 个（字段说明.md 里写明的那套），不跟着主库新增的走，
# 因为使用者只会按字段说明里那份清单去用。
for ddl_idx in [
        "CREATE INDEX idx_articles_site_pubday ON articles(site, pub_day)",
        "CREATE INDEX idx_articles_column ON articles(column_name)",
        "CREATE INDEX idx_articles_pubday ON articles(pub_day)",
        "CREATE INDEX idx_articles_site_column ON articles(site, column_name)"]:
    out.execute(ddl_idx)
src.execute("ATTACH DATABASE ? AS pack", (str(OUT_DB),))
# ATTACH 之后 pack.articles 已由上面建好；直接从 main 灌数据
src.execute("INSERT INTO pack.articles SELECT * FROM main.articles "
            "WHERE year>=2024")
src.commit()
src.execute("DETACH DATABASE pack")
n_out = out.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
print(f"  articles 灌入 {n_out} 篇（源库 {n_2024}）")
# 空表壳，方便使用者 join 时不报「no such table」
for t in ("ai_annotations", "ai_failures", "events", "external_links", "sync_runs"):
    cols = {r[1] for r in src.execute(f"PRAGMA table_info({t})")}
    if cols:
        out.execute(f"CREATE TABLE {t} ({','.join('`'+c+'`' for c in sorted(cols))})")
out.commit()
ic = out.execute("PRAGMA integrity_check").fetchone()[0]
out.close()
print(f"  integrity_check: {ic}   体积 {OUT_DB.stat().st_size/1e6:.1f} MB")

# ---------------------------------------------------------------- 读数据
# ⚠ 这里必须自己建连接并设 row_factory：上面 src 那条连接关了，
#   而 sqlite3 默认返回 tuple，`dict(tuple)` 会直接抛
#   "cannot convert dictionary update sequence element"。
_csv_src = sqlite3.connect(f"file:{SRC_DB.as_posix()}?mode=ro", uri=True)
_csv_src.row_factory = sqlite3.Row
rows = [dict(r) for r in _csv_src.execute(
    "SELECT id, site, site_name, column_code, column_name, title, pub_date, "
    "pub_day, year, month, chars, source_text, url, content_text "
    "FROM articles WHERE year>=2024 ORDER BY id")]
_csv_src.close()
print(f"  载入 {len(rows)} 篇用于生成 CSV")

# ---------------------------------------------------------------- 2 CSV
print()
print("=" * 70)
print("2  articles.csv")
print("=" * 70)
f = PACK / "articles.csv"
with f.open("w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["id", "站点", "站点名", "栏目", "标题", "发布日期", "年份", "月份",
                "正文字数", "来源字样", "原文链接"])
    for r in rows:
        w.writerow([r["id"], r["site"], r["site_name"], r["column_name"],
                    r["title"], r["pub_day"], r["year"], r["month"],
                    r["chars"], r["source_text"], r["url"]])
print(f"  {len(rows)+1} 行  {f.stat().st_size/1e6:.2f} MB")

# ---------------------------------------------------------------- 3 栏目清单
print()
print("=" * 70)
print("3  栏目候选清单.csv（投票用）")
print("=" * 70)
groups = {}
for r in rows:
    k = (r["site"], r["site_name"], r["column_code"], r["column_name"])
    groups.setdefault(k, []).append(r)

clist = []
for (site, sname, code, col), items in groups.items():
    n = len(items)
    n2425 = sum(1 for r in items if (r["pub_day"] or "") <= "2025-12-31")
    n24 = sum(1 for r in items if r["year"] == "2024")
    months = len({r["month"] for r in items if r["month"]})
    joint24 = sum(1 for r in items if JOINT_NARROW.search(r["title"] or ""))
    joint2425 = sum(1 for r in items
                    if JOINT_NARROW.search(r["title"] or "")
                    and (r["pub_day"] or "") <= "2025-12-31")
    win = "2024-2025" if 300 <= n2425 <= 500 else "无"
    if win != "无":
        why = (f"【{win}】共{n2425}/{n24}篇，符合评审方300-500要求；"
               f"联合发文{joint2425}篇，机构合作网络有真素材；"
               f"跨{months}个月，时间轴够用")
    elif months >= 20:
        why = f"跨{months}个月，时间轴够用"
    else:
        why = "篇数不达标，仅作参考"
    clist.append([site, sname, col, code, n, n2425, n24, win,
                  joint24, joint2425, months,
                  min(r["pub_day"] for r in items if r["pub_day"]),
                  max(r["pub_day"] for r in items if r["pub_day"]),
                  sum(r["chars"] or 0 for r in items), why])
clist.sort(key=lambda x: (x[0], -x[4]))

f = PACK / "栏目候选清单.csv"
with f.open("w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["站点", "站点名", "栏目", "栏目代码", "2024+篇数", "2024-2025篇数",
                "2024单年篇数", "落入300-500的窗口", "联合发文类(2024+)",
                "联合发文类(2024-2025)", "覆盖月份数", "起", "止", "总字数",
                "推荐理由"])
    w.writerows(clist)
print(f"  {len(clist)+1} 行")
print("  ⚠ 联合发文类口径：标题含 联合/印发/会同（**窄**口径，照旧包）。"
      "下面 联合发文线索.csv 用的是宽口径（多带 联发/共同），"
      "两级设计是有意的：线索表放宽召回交人筛，清单只数强信号")
for c in clist:
    if c[7] != "无":
        print(f"    ✅ 达标窗口 {c[0]}/{c[2]}  {c[7]}  {c[5]} 篇  联合发文 {c[9]} 篇")

# ---------------------------------------------------------------- 4 线索
print()
print("=" * 70)
print("4  联合发文线索.csv")
print("=" * 70)
clue = []
for r in rows:
    t = r["title"] or ""
    if not JOINT_WIDE.search(t):
        continue
    orgs = [n for n, _ in (_org_mentions(r["content_text"] or ""))][:ORG_SHOW]
    clue.append([r["site"], r["site_name"], r["column_name"], r["pub_day"], t,
                 " / ".join(orgs), r["url"],
                 (r["content_text"] or "").replace("\n", " ")[:300]])
f = PACK / "联合发文线索.csv"
with f.open("w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["站点", "站点名", "栏目", "发布日期", "标题", "疑似联合机关",
                "原文链接", "正文开头300字"])
    w.writerows(clue)
print(f"  {len(clue)} 条")
print(f"  其中能抽出 ≥1 个机构的：{sum(1 for c in clue if c[5])} 条")

# ---------------------------------------------------------------- 汇总
print()
print("=" * 70)
print("汇总（请据此同步 字段说明.md）")
print("=" * 70)
body = sum(1 for r in rows if (r["content_text"] or "").strip())
print(f"  2024+ 篇数          {len(rows)}")
print(f"  有正文              {body}   （空正文 {len(rows)-body}）")
print(f"  站点 / 栏目         {len({r['site'] for r in rows})} / {len(groups)}")
print(f"  联合发文线索        {len(clue)}")
for c in clist:
    if c[7] != "无":
        print(f"  达标窗口 {c[0]}/{c[2]} {c[7]}: {c[5]} 篇（2024 单年 {c[6]}）"
              f" 联合发文 {c[9]} 篇 跨 {c[10]} 个月")
