# -*- coding: utf-8 -*-
"""报告重写取数：2024+ 分析窗下「独立 / 联合 / 未做」三类支撑数据。只读连接。"""
import sqlite3
from collections import Counter

W = "a.pub_date >= '2024-01-01'"
c = sqlite3.connect("file:data/cacx.db?mode=ro", uri=True)

def q1(sql, *a):
    return c.execute(sql, a).fetchall()

print("=== [0] 窗口底数 ===")
print(q1(f"select count(*), sum(case when content_empty=0 then 1 else 0 end), "
        f"count(distinct site), count(distinct column_code) from articles a where {W}")[0])
print("按站:", [tuple(r) for r in q1(f"select site, count(*) from articles a where {W} group by site")])

print("\n=== [1] 2024+ 关系类型分布 ===")
for r in q1(f"select r.rel_type, count(*) from relations r join articles a on a.id=r.article_id "
            f"where {W} group by r.rel_type order by 2 desc"):
    print(" ", tuple(r))

CENTRAL = ("中央网信办", "国家互联网信息办公室", "国家网信办")
ZJ = ("浙江省委网信办", "网信浙江", "浙江省互联网信息办公室")
# 泛称，单独统计
ZJGENERIC = ("省委网信办", "党委网信办", "市委网信办", "网信办")

def stats(names, label):
    ph = ",".join("?" * len(names))
    print(f"\n=== [{label}] ===")
    tot = q1(f"select count(*) from relations r join articles a on a.id=r.article_id "
             f"where {W} and (r.src_name in ({ph}) or r.dst_name in ({ph}))", *names, *names)[0][0]
    print("  涉及其名的关系总数:", tot)
    by = q1(f"select r.rel_type, count(*) from relations r join articles a on a.id=r.article_id "
           f"where {W} and (r.src_name in ({ph}) or r.dst_name in ({ph})) "
           f"group by r.rel_type order by 2 desc", *names, *names)
    for r in by:
        print("   ", tuple(r))
    print("  joint_issue 搭档 Top15:")
    for r in q1(f"""select case when r.src_name in ({ph}) then r.dst_name else r.src_name end o,
                    count(*) n
                    from relations r join articles a on a.id=r.article_id
                    where {W} and r.rel_type='joint_issue' and (r.src_name in ({ph}) or r.dst_name in ({ph}))
                    group by o order by n desc limit 15""", *names, *names, *names):
        print("   ", tuple(r))
    print("  独立发文/施策（另一端为政策/问题实体，非机构）Top10:")
    for r in q1(f"""select case when r.src_name in ({ph}) then r.dst_name else r.src_name end o,
                    r.rel_type, count(*) n
                    from relations r join articles a on a.id=r.article_id
                    where {W} and r.rel_type in ('issue','adopt') and (r.src_name in ({ph}) or r.dst_name in ({ph}))
                    group by o, r.rel_type order by n desc limit 10""", *names, *names, *names):
        print("   ", tuple(r))

stats(CENTRAL, "2 中央网信办/国家互联网信息办公室")
stats(ZJ, "3 浙江省委网信办（zjwx 站本体）")
stats(ZJGENERIC, "3b 泛称「省/市委/网信办」（含中央站引用地方）")

print("\n=== [4] 中央 vs 任何省网信办 的直接联合发文（2024+）===")
CIN = ",".join("?" * len(CENTRAL))
anyprov = q1("select count(*) from relations r join articles a on a.id=r.article_id "
             "where " + W + " and r.rel_type='joint_issue' "
             "and (r.src_name in (" + CIN + ") or r.dst_name in (" + CIN + ")) "
             "and ((r.src_name like '%省委网信办%' or r.dst_name like '%省委网信办%') "
             "or (r.src_name like '%网信浙江%' or r.dst_name like '%网信浙江%') "
             "or (r.src_name like '浙江省委%' or r.dst_name like '浙江省委%'))", *CENTRAL, *CENTRAL)
print("  中央 × 省级（含泛称）joint_issue 条数:", anyprov[0][0])
print("  逐条证据：")
for r in q1("select r.src_name, r.dst_name, a.title, substr(r.evidence,1,90) "
            "from relations r join articles a on a.id=r.article_id "
            "where " + W + " and r.rel_type='joint_issue' "
            "and (r.src_name in (" + CIN + ") or r.dst_name in (" + CIN + ")) "
            "and ((r.src_name like '%省委%' or r.dst_name like '%省委%') "
            "  or (r.src_name like '%网信办%' and r.dst_name like '%网信办%'))",
            *CENTRAL, *CENTRAL):
    print("   ", tuple(r))
allsame = q1(f"""select count(*) from relations r join articles a on a.id=r.article_id
    where {W} and r.rel_type='joint_issue'
      and r.src_name like '%网信办%' and r.dst_name like '%网信办%'""")
print("  任意两个「网信办」之间的 joint_issue（含泛称）:", allsame[0][0])

print("\n=== [4b] zjwx 站文章里出现的机构名 Top30（核对浙江侧真实写法）===")
for r in q1(f"""select e.name, count(*) n from entities e join articles a on a.id=e.article_id
    where {W} and a.site='zjwx' and e.type='org' group by e.name order by n desc limit 30"""):
    print("  ", tuple(r))
print("  zjwx 站 joint_issue 端点 Top20:")
for r in q1(f"""select r.src_name, r.dst_name, count(*) n from relations r
    join articles a on a.id=r.article_id where {W} and a.site='zjwx' and r.rel_type='joint_issue'
    group by 1,2 order by n desc limit 20"""):
    print("  ", tuple(r))

print("\n=== [5] 2023 职责划出后的「文本里没做什么」双证 ===")
for kw in ("数字中国", "智慧城市", "数据局", "公共数据", "数据要素", "信息化"):
    body = q1(f"select count(*) from articles a where {W} and a.content_text like ?", f"%{kw}%")[0][0]
    withcent = q1(f"""select count(*) from articles a where {W} and a.content_text like ?
                      and a.content_text like '%中央网信办%'""", f"%{kw}%")[0][0]
    wdata = q1(f"select count(*) from articles a where {W} and a.content_text like ?"
               " and a.content_text like '%国家数据局%'", f"%{kw}%")[0][0]
    print(f"  {kw}: 正文命中 {body} 篇；其中含「中央网信办」{withcent} 篇；含「国家数据局」{wdata} 篇")

print("\n=== [5b] 脏/非机构实体名实证（2024+ 关系端点）===")
ORG = ("部", "局", "委员会", "政府", "党委", "法院", "检察院", "协会", "联合会", "大学", "学院", "公司")
bad = q1(f"""select r.src_name, count(*) n from relations r join articles a on a.id=r.article_id
             where {W} and r.rel_type='joint_issue' and r.src_name in
               ('活动由中央网信办','根据中央网信办','展播活动由中央网信办','系列活动是中央网信办',
                '会议由中央网信办','移交省通信管理局','有限公司','中国社会组织联合会','社会组织联合会',
                '中国网络社会组织联合会','联合主办','网络传播局','秘书局','信息化委员会','网络社会',
                '中国网络社会','法学院','主任')
             group by 1 order by n desc""")
tot_bad = sum(r[1] for r in bad)
print(f"  典型脏名合计 {tot_bad} 条：")
for r in bad:
    print("   ", tuple(r))
allend = q1(f"""select count(*) from relations r join articles a on a.id=r.article_id
                 where {W} and r.rel_type='joint_issue'""")[0][0]
short = q1(f"""select count(*) from relations r join articles a on a.id=r.article_id
    where {W} and r.rel_type='joint_issue'
      and (length(r.src_name)>10 or r.src_name like '%由%' or r.src_name like '%根据%'
           or r.src_name like '%联合主办%' or r.src_name like '%的活动%')""")[0][0]
print(f"  joint_issue 端点含动词/长串的可疑条数 {short}/{allend} = {short/allend*100:.1f}%")

print("\n=== [5c] 中央网信办「列名居首」还是被列名 ===")
lead = q1(f"""select count(*) from relations r join articles a on a.id=r.article_id
    where {W} and r.rel_type='joint_issue' and r.src_name='中央网信办'""")[0][0]
tail = q1(f"""select count(*) from relations r join articles a on a.id=r.article_id
    where {W} and r.rel_type='joint_issue' and r.dst_name='中央网信办'""")[0][0]
print(f"  src=中央网信办 {lead} 条 / dst=中央网信办 {tail} 条")

print("\n=== [6] 执法/处罚主体：中央网信办与执法机关的关系 ===")
for r in q1(f"""select r.rel_type, r.src_name, r.dst_name, count(*) n from relations r
                join articles a on a.id=r.article_id where {W}
                and (r.src_name in ({','.join('?'*len(CENTRAL))}) or r.dst_name in ({','.join('?'*len(CENTRAL))}))
                and (r.src_name like '%公安部%' or r.dst_name like '%公安部%'
                  or r.src_name like '%网安局%' or r.dst_name like '%网安局%'
                  or r.src_name like '%市场监管%' or r.dst_name like '%市场监管%')
                group by 1,2,3 order by n desc limit 20""", *CENTRAL, *CENTRAL):
    print("  ", tuple(r))

print("\n=== [7] 2024+ 中央网信办标题里的合作部委（标题共现）===")
for r in q1(f"""select count(*) from articles a where {W} and a.title like '%中央网信办%'""")[0:1]:
    print("  标题含「中央网信办」:", r[0])
co = Counter()
for (t,) in q1(f"select title from articles a where {W} and a.title like '%中央网信办%'"):
    for m in ("工业和信息化部", "公安部", "国家发展改革委", "国家数据局", "市场监管总局",
              "教育部", "中央宣传部", "国家保密局", "中央网信办"):
        if m in t:
            co[m] += 1
for k, v in co.most_common():
    print(f"   {k}: {v}")

print("\n=== [8] 分析窗关系覆盖与实体（2024+）===")
print("  有关系文章:", q1(f"select count(distinct r.article_id) from relations r join articles a on a.id=r.article_id where {W}")[0][0])
print("  有实体或关系文章:", q1(f"""select count(*) from articles a where {W} and (
    id in (select article_id from relations) or id in (select article_id from entities))""")[0][0])
print("  去重实体名:", q1(f"""select count(distinct e.name) from entities e join articles a on a.id=e.article_id where {W}""")[0][0])
print("  实体按类型:", [tuple(r) for r in q1(f"""select e.type, count(distinct e.name) from entities e
      join articles a on a.id=e.article_id where {W} group by e.type order by 2 desc""")])
