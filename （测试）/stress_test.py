# -*- coding: utf-8 -*-
"""并发压力测试：多线程读 + 混合写，模拟 FastAPI 线程池下的真实负载"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cacx import config as C, db as D

cfg = C.load()
d = D.open_db(cfg)
errors = []
RESULTS = {}


def reader(n):
    try:
        for _ in range(n):
            r = d.search(q="网信办", limit=5)
            assert r["total"] >= 0
            r2 = d.search(q="清朗", limit=5)     # LIKE 分支
            assert r2["mode"] == "like"
            d.facets()
    except Exception as e:
        errors.append(f"reader: {type(e).__name__} {e}")


def writer(n, tag):
    try:
        for i in range(n):
            d.upsert_article({
                "site": "cac", "site_name": "中国网信办",
                "column_code": "TEST", "column_name": "并发测试",
                "title": f"并发测试 {tag}-{i}",
                "pub_date": "2026-10-10 00:00:00",
                "url": f"https://example.invalid/test/{tag}-{i}",
                "source_text": "t", "chars": 10, "content_text": "测试内容",
                "content_html": "<p>测试</p>",
            })
            d.save_annotation(1, "test", {"ok": True})
    except Exception as e:
        errors.append(f"writer{tag}: {type(e).__name__} {e}")


print("=" * 60)
print("并发压测：6 读线程 + 3 写线程")
print("=" * 60)
before = d.facets()["total"]

ts = ([threading.Thread(target=reader, args=(25,), name=f"r{i}") for i in range(6)]
      + [threading.Thread(target=writer, args=(20, f"w{i}"), name=f"w{i}")
         for i in range(3)])

t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
el = time.time() - t0

after = d.facets()["total"]
print(f"  耗时 {el:.2f}s")
print(f"  文章数 {before} → {after}  (应 +60)")
print(f"  错误 {len(errors)}")
for e in errors[:8]:
    print("   ", e)

ok = (not errors) and (after - before == 60)
print()
print("结果:", "PASS —— 共享连接在并发读写下稳定" if ok else "FAIL")

# 清理测试数据（只删本测试写入的 URL 前缀）
n = d.conn.execute("DELETE FROM articles WHERE url LIKE 'https://example.invalid/test/%'").rowcount
d.conn.execute("DELETE FROM ai_annotations WHERE task='test'")
d.conn.commit()
d.rebuild_fts()
print(f"已清理测试数据 {n} 条，剩余 {d.facets()['total']} 篇")
d.close()
sys.exit(0 if ok else 1)