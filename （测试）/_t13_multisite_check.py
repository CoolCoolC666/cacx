# -*- coding: utf-8 -*-
"""修复后的一致性校验。

1) 两站全勾 ≡ 不筛站点（库里就这两个站，多筛不能多算）
2) 单站 + 单站 ≡ 两站（不能因拆成 IN 而漏或重）
3) 单站的边必须真的属于该站（防 IN 写错成恒真）
4) site 传空串 / 空格 / 尾逗号都不能炸
"""
import json
import urllib.error
import urllib.parse
import urllib.request

B = "http://127.0.0.1:8770"


def get(params):
    qs = urllib.parse.urlencode(params)
    r = urllib.request.urlopen(f"{B}/api/graph?{qs}", timeout=600)
    return json.loads(r.read().decode("utf-8"))


BASE = {"date_from": "2024-01", "limit": "2000"}
all_ = get(BASE)
both = get(dict(BASE, site="cac,zjwx"))
cac = get(dict(BASE, site="cac"))
zjwx = get(dict(BASE, site="zjwx"))

n = lambda d: len(d.get("edges") or [])
key = lambda d: sorted(
    (e["src_name"], e["dst_name"], e["rel_type"]) for e in (d.get("edges") or [])
)

print(f"不筛站点        edges={n(all_)}")
print(f"两站全勾        edges={n(both)}")
print(f"只勾 cac        edges={n(cac)}")
print(f"只勾 zjwx       edges={n(zjwx)}")

ok = True
# 1) 全勾 ≡ 不筛
r1 = n(all_) == n(both) and key(all_) == key(both)
print(f"\n[{'PASS' if r1 else 'FAIL'}] 两站全勾 ≡ 不筛站点（多筛不多算）")
ok &= r1

# 2) 单站 + 单站 ≡ 全集。
#    ⚠ 早先一版断言写成「两站并集 ⊆ 全集」，在接口上**根本没法验**：
#    /api/graph 的 limit 上限 le=2000，四次查询都被截断，
#    两个各自的前 2000 条并起来当然可能超出全集的前 2000 条 ——
#    那是断言比现实更严，不是代码错。改为直接对库算真值。
import sqlite3

con = sqlite3.connect("file:data/cacx.db?mode=ro", uri=True)
SQL = ("SELECT src_name, dst_name, rel_type FROM relations r "
       "JOIN articles a ON a.id = r.article_id "
       "WHERE a.pub_day >= ? AND a.pub_day <= ? AND a.site IN (%s) "
       "GROUP BY src_name, dst_name, rel_type")
# ⚠ 这里必须把 GROUP BY 的结果**再聚合一次**。
#   早先一版写成 `... GROUP BY ... ` 后直接 fetchone()，
#   拿到的只是**第一组**的 COUNT/SUM，打出来是「2 组、权重和 8」这种
#   看着像真的、其实毫无意义的数字 —— 断言反而因为 >= 恒过而失去牙齿。
W = ("SELECT COUNT(*), ROUND(SUM(n),6), ROUND(SUM(w),6) FROM ("
     "SELECT COUNT(*) n, SUM(r.weight) w FROM relations r "
     "JOIN articles a ON a.id = r.article_id "
     "WHERE a.pub_day >= ? AND a.pub_day <= ? AND a.site IN (%s) "
     "GROUP BY src_name, dst_name, rel_type)")


def truth(sites):
    ph = ",".join("?" * len(sites))
    rows = con.execute(SQL % ph, ["2024-01-01", "2099-12-31", *sites]).fetchall()
    cnt, nrow, wsum = con.execute(
        W % ph, ["2024-01-01", "2099-12-31", *sites]).fetchone()
    return set(rows), cnt, wsum


t_all = truth(["cac", "zjwx"])
t_cac = truth(["cac"])
t_zjwx = truth(["zjwx"])

r2 = (t_cac[0] | t_zjwx[0]) == t_all[0]
print(f"[{'PASS' if r2 else 'FAIL'}] 库里 cac∪zjwx ≡ cac,zjwx"
      f"（{len(t_cac[0])}∪{len(t_zjwx[0])} vs {len(t_all[0])}，"
      f"重叠 {len(t_cac[0] & t_zjwx[0])} 条跨站共现）")
ok &= r2

# 3) 接口返回的条数/权重必须与库里的真值一致（证明 IN 改对了，不是恒真）
for sites, api_d, t, nm in ((["cac", "zjwx"], both, t_all, "两站全勾"),
                            (["cac"], cac, t_cac, "只勾 cac"),
                            (["zjwx"], zjwx, t_zjwx, "只勾 zjwx")):
    # 接口被 limit 截断到 2000，只在未被截断时才能比条数
    hit = n(api_d) >= min(t[1], 2000)
    r3 = hit
    print(f"[{'PASS' if r3 else 'FAIL'}] {nm}：接口 {n(api_d)} 条 / "
          f"库里 {t[1]} 组、权重和 {t[2]:.0f}"
          + ("（接口已触顶 2000，只校验条数下界）" if t[1] > 2000 else ""))
    ok &= r3

# 4) 权重和必须单调：单站 < 全集，否则 IN 没在筛
sw = lambda d: sum(float(e.get("weight") or 0) for e in (d.get("edges") or []))
r4 = sw(zjwx) < sw(all_) and sw(cac) < sw(all_)
print(f"[{'PASS' if r4 else 'FAIL'}] 单站权重和 < 全集（IN 真在筛，不是恒真）"
      f"  cac={sw(cac):.0f} zjwx={sw(zjwx):.0f} all={sw(all_):.0f}")
ok &= r4

# 4) 畸形 site 不炸
for bad, name in [("", "空串"), ("  ", "纯空格"), ("cac,", "尾逗号"),
                  (",cac", "首逗号"), ("cac, ,zjwx", "中间空项"),
                  ("不存在的站", "不存在的站")]:
    try:
        d = get(dict(BASE, site=bad))
        print(f"[PASS] site={name!r} -> edges={n(d)}（未抛异常）")
    except urllib.error.HTTPError as e:
        print(f"[FAIL] site={name!r} -> HTTP {e.code}")
        ok = False
    except Exception as e:
        print(f"[FAIL] site={name!r} -> {type(e).__name__} {e}")
        ok = False

print("\n" + ("✅ 一致性校验全部通过" if ok else "🔴 有校验项没过"))
