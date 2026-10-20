# -*- coding: utf-8 -*-
r"""清理「一文多栏目发」造成的重复行（保守口径）。

只删**确定**是同一篇的：同站 + 同标题 + 同发布日期 + 规范化正文完全相同。
每组**优先保留有正文的那篇**（2026-10-15 起；此前是「保留 id 最小」），
id 最小退化为同优先级下的次序；整组都无正文时才退回 id 最小。

⚠ 为什么口径这么窄：同正文还可能来自别的情形（图集 vs 正文、错别字版），
  那些不一定该合并。用 body_hash 单独分组能认出 597 组，但其中相当一部分
  标题或日期对不上，属于「疑似」而非「确定」，本脚本一律不动。

⚠ 写的是**真库**，跑前自动 SQLite 备份。
  删 articles 会**级联删掉它的 relations**（外键 ON DELETE CASCADE），
  所以删完必须重跑抽取引擎，关系数不会自己回落。

跑法：py -3 （测试）\_cleanup_multicolumn.py --dry   # 只预览
      py -3 （测试）\_cleanup_multicolumn.py          # 真删
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
from cacx.db import Database   # noqa: E402

# 本文件在 （测试）/ 里，所以是 parent.parent。
# ⚠ 写成 parent 会指到 （测试）/ 上去 —— Database() 会顺手在那儿建一个
#   **空库**（它有 mkdir + CREATE TABLE），于是查询全返回 0，
#   看起来像"库里没有重复"，实际是连错了库。
#   真踩过：脚本报"0 组"，而同一个 SQL 用原生 sqlite3 查是 112 组。
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "cacx.db"
DRY = "--dry" in sys.argv

d = Database(str(DB))

# ---- 连错库防护 ------------------------------------------------
# 空库和新库在这个脚本里长得一模一样：都是"0 组、无需处理"。
# 所以在任何提前退出之前，先把「我连的是哪个文件、多少篇文章」打出来，
# 让"连错库"和"真的干净了"在输出上一眼可分。
_n_all = d.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
_n_2024 = d.conn.execute(
    "SELECT COUNT(*) FROM articles WHERE year>=2024").fetchone()[0]
if _n_all == 0:
    print(f"[ABORT] {DB} 里一篇文章都没有 —— 连错库了，拒绝继续。")
    print("        Database() 会自动 mkdir + 建表，指错目录不会报错，只会给你一个空库。")
    d.close()
    sys.exit(2)
print("=" * 68)
print(f"目标库 {DB}")
print(f"  articles { _n_all }   其中 2024+ { _n_2024 }")
print("=" * 68)

groups = d.conn.execute("""
    SELECT site, title, pub_day, COUNT(*) c, GROUP_CONCAT(id) ids,
           COUNT(DISTINCT body_hash) bh_n
    FROM articles
    WHERE pub_day >= '2024-01-01' AND title IS NOT NULL AND title<>''
    GROUP BY site, title, pub_day
    HAVING COUNT(*) > 1
    ORDER BY site, pub_day""").fetchall()


def _pick_keep(conn, ids):
    """同组里保留哪一条。

    原规则是「保留 id 最小的」（先入库的那篇），2026-10-15 改为
    **优先留有正文的**，id 最小退化为同优先级下的次序。

    为什么改：id 最小 ≠ 更好。有正文那篇才是使用者真正用得上的；
    留一个空正文占位，等于把「有正文 N 篇」这个口径永远钉死成 N-1，
    还制造出一道需要额外解释的例外。实测就踩中了：浙江站 2024-04-03
    「打『国家』旗号的涉诈App」那组，id 最小的 #3691 恰好 content_empty=1，
    等于删掉了有正文的、留下了没正文的。

    ⚠ 正文的判据用 content_empty 标记而不是 length(content_text)>0：
      后者会把「抓到 1 个空白字符」也当成有正文，而 content_empty
      是入库时定的标记，口径唯一。
    """
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, content_empty FROM articles WHERE id IN ({ph}) ORDER BY id",
        ids).fetchall()
    with_body = [r["id"] for r in rows if not r["content_empty"]]
    pool = with_body or [r["id"] for r in rows]
    return pool[0]


keep_ids, drop_ids = [], []
for g in groups:
    ids = sorted(int(x) for x in g["ids"].split(","))
    if g["bh_n"] != 1:
        continue                       # 正文不止一种指纹 -> 存疑，不动
    k = _pick_keep(d.conn, ids)
    keep_ids.append(k)
    drop_ids.extend(i for i in ids if i != k)

print("=" * 68)
print("一文多栏目发 · 重复行清理")
print("=" * 68)
print(f"  2024+ 同站同标题同日的多行组：{len(groups)}")
print(f"  其中正文指纹唯一（可确定同一篇）：{len(keep_ids)}")
print(f"  将删除的行：{len(drop_ids)}   保留：{len(keep_ids)}")

if not drop_ids:
    print("\n没有可确定的重复，无需处理。")
    d.close()
    sys.exit(0)

ph = ",".join("?" * len(drop_ids))
print(f"  占 2024+ 篇数的 {100 * len(drop_ids) / _n_2024:.2f}%")

print("\n" + "=" * 68)
print("影响面")
print("=" * 68)
q = lambda s: d.conn.execute(s).fetchone()[0]
body_now = q("SELECT COUNT(*) FROM articles WHERE year>=2024 AND content_text IS NOT NULL AND LENGTH(content_text)>0")
cov_now = q("SELECT COUNT(DISTINCT article_id) FROM relations")
rel_now = q("SELECT COUNT(*) FROM relations")
print(f"  篇数(2024+)  {_n_2024}  ->  {_n_2024 - len(drop_ids)}")
print(f"  有正文       {body_now}  ->  {body_now - len(drop_ids)}")
print(f"  关系覆盖篇数 {cov_now}  ->  {cov_now - len(drop_ids)}（级联删）")
rel_cascade = d.conn.execute(
    "SELECT COUNT(*) FROM relations WHERE article_id IN (" + ph + ")",
    drop_ids).fetchone()[0]
print(f"  关系行数     {rel_now}  ->  {rel_now - rel_cascade}（级联删）")

print("\n" + "=" * 68)
print("样本（前 8 组）")
print("=" * 68)
shown = 0
for g in groups:
    if g["bh_n"] != 1:
        continue
    ids = sorted(int(x) for x in g["ids"].split(","))
    rows = d.conn.execute(
        f"SELECT id, column_name, content_empty FROM articles "
        f"WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
        ids).fetchall()
    cols = " / ".join(f"{r['column_name'] or '?'}{'' if not r['content_empty'] else '(空)'}"
                      for r in rows)
    print(f"  [{g['site']}] {g['title'][:30]}")
    print(f"      栏目 {cols}  ->  保留 #{_pick_keep(d.conn, ids)}")
    shown += 1
    if shown >= 8:
        break

if DRY:
    print("\n--dry：什么都没删。")
    d.close()
    sys.exit(0)

print("\n" + "=" * 68)
print("备份")
print("=" * 68)
bak = DB.with_name(f"cacx.db.bak-{datetime.now():%Y%m%d-%H%M%S}")
s = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
t = sqlite3.connect(str(bak))
with t:
    s.backup(t)
t.close()
s.close()
print(f"  已备份 → {bak.name}  ({bak.stat().st_size / 1e6:.1f} MB)")

print("\n" + "=" * 68)
print("删除")
print("=" * 68)
d.conn.execute(f"DELETE FROM articles WHERE id IN ({ph})", drop_ids)
d.conn.commit()
left = q("SELECT COUNT(*) FROM articles")
print(f"  已删除 {len(drop_ids)} 行，articles 现存 {left}")
print("  ⚠ 关系已随之级联删除，**必须重跑抽取引擎**（关系数不会自己回落）。")
d.close()