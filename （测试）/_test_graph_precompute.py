# -*- coding: utf-8 -*-
"""图谱预计算（后台任务 + 阶段进度）的闸门。

守的是三件事，每一件都对应本轮真实踩到的或真实想坏掉的：

1) **不阻塞**：`wait=false` 必须在毫秒级返回，而不是等 460 秒。
   这条守的是「整站卡死」那个真 bug —— 原来没有后台线程，
   同步算图期间连 /api/health 都"连接被拒绝"。
   ⚠ 用的是 6 节点小图 + 假 calc，所以**不需要真跑全图**：
     断言的是"有没有立刻返回"这个性质，与图多大无关。

2) **阶段真的推进 + 真的上报**：analyze(on_stage=) 要按顺序把
   degree → betweenness → pagerank → components → communities 报一遍。
   ⚠ 为什么要单独守：networkx **不给**进度回调，阶段名是我们自己写的。
   改错一个字、改漏一个阶段，界面就一直停在"读图与度数"，
   而**服务不报错、测试也不会红** —— 这是最典型的静默失效。

3) **算完真的进缓存，且不破坏同步路径**：
   · done 之后重发同一个请求 → 200 且是**结果体**，不是 202
   · `wait=true`（默认）的行为**逐字节不变** —— 验收脚本、
     演示脚本、文档里的 curl 全走这条，它变了就是最难的回归
   · 连点两次只排一份任务（幂等），不会排出两份几百秒的计算

另外单独守一条「观测代码不许弄坏被观测代码」：
on_stage 自己抛异常时，analyze 必须照常返回结果。
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import networkx as nx  # noqa: E402

import cacx.graph_analysis as GA  # noqa: E402

_RESULTS: List[Dict[str, Any]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"   [{detail}]" if detail else ""))
    return bool(ok)


def tiny_graph(n: int = 6):
    G = nx.Graph()
    for i in range(n):
        G.add_node(f"机构{i}", type="org")
    for i in range(n):
        G.add_edge(f"机构{i}", f"机构{(i + 1) % n}", weight=1)
    G.add_edge("机构0", "机构3", weight=2)
    return G


# ---------------------------------------------------------------- 2) 阶段上报

def test_stages_reported_in_order():
    seen: List[str] = []
    G = tiny_graph()
    r = GA.analyze(G, {"机构0": "org"}, topn=3, on_stage=seen.append)
    want = ["degree", "betweenness", "pagerank", "components", "communities"]
    check("阶段按顺序全部上报", seen == want, f"实到 {seen}")
    check("on_stage 传了就真的出结果（不是空壳）", len(r.get("betweenness", [])) > 0,
          f"betweenness {len(r.get('betweenness', []))} 条")
    check("五个阶段都有中文说明", all(s in GA.STAGE_LABELS for s in want),
          f"缺 {[s for s in want if s not in GA.STAGE_LABELS]}")
    total = sum(w for _, w in GA.STAGE_LABELS.values())
    check("阶段权重加起来是 100", total == 100, f"实际 {total}")


def test_no_callback_when_omitted():
    """默认 on_stage=None 时一次都不该调 —— 这是向后兼容的底线。"""
    G = tiny_graph()
    r1 = GA.analyze(G, {"机构0": "org"}, topn=3)
    r2 = GA.analyze(G, {"机构0": "org"}, topn=3)
    a = [(k, r1[k]) for k in ("components", "summary")]
    b = [(k, r2[k]) for k in ("components", "summary")]
    check("不传 on_stage 也能正常算完（老调用方不受影响）", r1["nodes"] == G.number_of_nodes(),
          f"nodes={r1['nodes']}")
    check("两次结果一致（阶段回调没有引入随机性）", a == b)


def test_callback_error_does_not_break_analysis():
    """观测代码抛异常，被观测的代码必须照常跑完。"""
    def bad(_s: str) -> None:
        raise RuntimeError("观测炸了")

    G = tiny_graph()
    try:
        r = GA.analyze(G, {"机构0": "org"}, topn=3, on_stage=bad)
        check("on_stage 抛异常时 analyze 仍返回结果",
              r.get("nodes") == G.number_of_nodes(), f"nodes={r.get('nodes')}")
    except Exception as e:
        check("on_stage 抛异常时 analyze 仍返回结果", False,
              f"{type(e).__name__}: {e}")


def test_empty_graph_shortcircuit():
    """空图要在**任何阶段上报之前**就返回，否则界面会停在 degree。"""
    seen: List[str] = []
    r = GA.analyze(nx.Graph(), topn=3, on_stage=seen.append)
    check("空图直接返回且不上报阶段", r["nodes"] == 0 and seen == [],
          f"nodes={r['nodes']} stages={seen}")


# ---------------------------------------------------------------- 1)(3) HTTP

def test_http_layer():
    """起一个真 TestClient 打到路由上，而不是直接调内部函数。

    ⚠ 路由函数的参数是 Query 对象，直接调会炸（TypeError）——
      这个坑项目里已经踩过一次，所以必须从 HTTP 进。
    """
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        check("TestClient 可用", False, f"{type(e).__name__}: {e}")
        return

    appmod = importlib.import_module("cacx.web.app")
    client = TestClient(appmod.app)

    # -- 状态端点在没有任何任务时不该 500 --
    r = client.get("/api/graph/analysis/status", params={"task": "deadbeefdeadbeef"})
    check("查一个不存在的任务不报错", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        check("不存在的任务如实说 found=false", r.json().get("found") is False,
              str(r.json())[:80])

    # -- 异步路径：换成假 calc，测「立刻返回」这个性质 --
    appmod._GRAPH_TASKS.clear()
    appmod._RESP_CACHE.clear()
    calls: List[str] = []

    def slow_calc(on_stage=None):
        calls.append("x")
        if on_stage:
            on_stage("degree")
            time.sleep(0.45)          # 模拟"很贵"，但不至于真的跑 460 秒
            on_stage("betweenness")
            on_stage("pagerank")
        return {"nodes": 6, "edges": 6, "summary": "假图",
                "components": 1, "by_type": []}

    # monkeypatch _graph_payload：只换**被测的那一段**，
    # 路由、缓存、任务调度全都走真代码。
    orig_payload = appmod._graph_payload
    appmod._graph_payload = lambda *a, **kw: slow_calc(kw.get("on_stage"))
    try:
        t0 = time.time()
        r = client.get("/api/graph/analysis", params={
            "limit": 11, "topn": 5, "rel_types": "issue,target",
            "node_types": "org", "date_from": "2024-01", "wait": "false"})
        dt = time.time() - t0
        check("wait=false 立刻返回 202", r.status_code == 202, f"{r.status_code}")
        check("wait=false 不等计算完成（毫秒级）", dt < 1.5, f"{dt:.2f}s")
        body = r.json() if r.status_code == 202 else {}
        tid = body.get("task_id", "")
        check("202 里带回了 task_id", bool(tid), str(body)[:120])
        check("202 里给了轮询地址", "status?task=" in body.get("poll", ""),
              body.get("poll", ""))

        # -- 幂等：同参数再发一次，不该排第二份任务 --
        r2 = client.get("/api/graph/analysis", params={
            "limit": 11, "topn": 5, "rel_types": "issue,target",
            "node_types": "org", "date_from": "2024-01", "wait": "false"})
        tid2 = r2.json().get("task_id", "") if r2.status_code == 202 else ""
        check("同参数重发拿到同一个 task_id（幂等）", tid == tid2 and bool(tid),
              f"{tid} vs {tid2}")
        time.sleep(0.15)
        check("连点两次只算了一份", len(calls) == 1, f"calc 被调 {len(calls)} 次")

        # -- 阶段在推进 --
        stages_seen = set()
        deadline = time.time() + 12
        last = {}
        while time.time() < deadline:
            s = client.get("/api/graph/analysis/status",
                           params={"task": tid}).json()
            last = s
            stages_seen.add(s.get("stage", ""))
            if s.get("status") in ("done", "error"):
                break
            time.sleep(0.12)
        check("任务最终跑到 done", last.get("status") == "done",
              f"status={last.get('status')} stage={last.get('stage')} "
              f"err={last.get('error')}")
        check("状态里能看到阶段名", bool(stages_seen & {"degree", "betweenness",
                                                        "pagerank", "done"}),
              f"见过 {sorted(stages_seen)}")
        check("状态里有中文阶段说明", bool(last.get("stage_label")),
              str(last.get("stage_label")))

        # -- done 之后重发原请求：必须是 200 结果体 --
        r3 = client.get("/api/graph/analysis", params={
            "limit": 11, "topn": 5, "rel_types": "issue,target",
            "node_types": "org", "date_from": "2024-01", "wait": "false"})
        check("算完再取：回 200 而不是再排一个任务", r3.status_code == 200,
              f"{r3.status_code}")
        if r3.status_code == 200:
            check("算完再取：拿到的是结果体", r3.json().get("summary") == "假图",
                  str(r3.json())[:80])

        # -- 同步路径**行为不变** --
        r4 = client.get("/api/graph/analysis", params={
            "limit": 11, "topn": 5, "rel_types": "issue,target",
            "node_types": "org", "date_from": "2024-01"})
        check("不传 wait 时走同步路径（默认行为不变）", r4.status_code == 200,
              f"{r4.status_code}")
        check("同步路径直接给结果体（脚本调用方不受影响）",
              r4.json().get("summary") == "假图", str(r4.json())[:80])
    finally:
        appmod._graph_payload = orig_payload


def test_frontend_contract():
    """前端约定 —— 这些是**只能靠约定维持**的东西，静态查。

    ⚠ 为什么不靠"点一遍页面"验收：那要真跑 460 秒的全库口径，
      而且"页面看起来在转圈"根本证明不了它走的是新路径。
      下面这几条一旦被改回去，后果都是**静默**的：

      · analysis 不带 wait=false → 又变成同步等，整站卡死，且**不报错**
      · 又用 Promise.all 包 analysis → 慢的那个把快的 stats 一起拖住，
        界面上一行统计都不出来，看起来像"整个图谱页坏了"
      · 没有 found=false 的处理 → 任务丢了以后前端死等，
        永远转圈，用户只能杀浏览器
    """
    path = os.path.join(ROOT, "cacx", "web", "static", "index.html")
    if not os.path.exists(path):
        check("index.html 存在", False, path)
        return
    src = open(path, "rb").read().decode("utf-8")

    check("前端走 wait=false（不在事件循环里干等）",
          "qs.set('wait','false')" in src, "")
    check("认得 202（pending 响应）",
          "res.status !== 202" in src and "body.pending" in src, "")
    check("轮询完重发同一个 URL 取结果",
          "res = await fetch(url);" in src, "")
    check("显示中文阶段说明", "s.stage_label" in src, "")
    check("显示已耗时秒数", "secs()" in src, "")
    check("任务丢失会告诉用户而不是死等", "s.found === false" in src, "")
    check("出错会显示原因", "s.status === 'error'" in src, "")
    check("没把 analysis 又塞回 Promise.all",
          "Promise.all" not in src or "/api/graph/analysis" not in
          src.split("Promise.all")[1].split(")")[0], "")
    check("注释说清了为什么不报百分比",
          "故意不报百分比" in src, "")


def test_heavy_mutex_between_sync_and_async():
    """同步路径和后台任务**必须共用一把互斥锁**。

    ⚠ 这条是本轮真跑撞出来的，不是想出来的：
      执行器只串行化后台任务，而同步请求（wait=true）是在事件循环里
      直接算的、**完全绕开执行器**。实测「后台全库任务 + 一个脚本的
      同步全库调用」并发时，/api/health **连续 6 次全部超时**；
      而只跑后台任务时它是 1.2~2.5 秒正常响应。
      两个 1.1 万节点的 networkx 图同时驻留内存（单个 550 MB 量级）
      就是元凶。少这一把锁 = 把 OOM 风险换成了无响应。
    """
    appmod = importlib.import_module("cacx.web.app")
    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)

    appmod._GRAPH_TASKS.clear()
    appmod._RESP_CACHE.clear()
    held = threading.Event()

    def slow_calc(on_stage=None):
        held.set()
        time.sleep(0.6)
        if on_stage:
            on_stage("degree")
        return {"nodes": 6, "edges": 6, "summary": "假图",
                "components": 1, "by_type": []}

    orig = appmod._graph_payload
    appmod._graph_payload = lambda *a, **kw: slow_calc(kw.get("on_stage"))
    try:
        # 手动把重计算锁按住，模拟"已经有别的东西在算"
        appmod._GRAPH_HEAVY_LOCK.acquire()
        try:
            r = client.get("/api/graph/analysis", params={"limit": 77})
            check("同步请求撞上重计算锁：明确报 409 而不是照算",
                  r.status_code == 409, f"{r.status_code}")
            if r.status_code == 409:
                detail = str(r.json().get("detail", ""))
                check("409 带的是一句人话，不是空白",
                      "wait=false" in detail or "计算" in detail, detail[:80])

            r2 = client.get("/api/graph/analysis", params={
                "limit": 77, "wait": "false"})
            check("锁被占时异步请求仍然受理（它会排队等锁）",
                  r2.status_code == 202, f"{r2.status_code}")
        finally:
            appmod._GRAPH_HEAVY_LOCK.release()

        time.sleep(1.2)     # 等那个后台任务把锁还回来
        r3 = client.get("/api/graph/analysis", params={"limit": 88})
        check("锁释放后同步请求恢复正常（不是永久卡死）",
              r3.status_code == 200, f"{r3.status_code}")
    finally:
        appmod._graph_payload = orig


def main() -> int:
    print("=" * 70)
    print("图谱预计算（后台任务 + 阶段进度）")
    print("=" * 70)
    for fn in (test_stages_reported_in_order,
               test_no_callback_when_omitted,
               test_callback_error_does_not_break_analysis,
               test_empty_graph_shortcircuit,
               test_http_layer,
               test_heavy_mutex_between_sync_and_async,
               test_frontend_contract):
        print()
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"{type(e).__name__}: {e}")
            traceback.print_exc()
    ok = sum(1 for r in _RESULTS if r["ok"])
    bad = [r for r in _RESULTS if not r["ok"]]
    print()
    print("=" * 70)
    print(f"通过 {ok} 项，失败 {len(bad)} 项")
    for r in bad:
        print(f"  FAIL  {r['name']}  [{r['detail']}]")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
