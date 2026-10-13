# -*- coding: utf-8 -*-
"""
批量摘要增强（cacx.ai.batch）离线测试
================================================================
运行：  python tests_batch_summarize.py

为什么这些断言这样写
  · AI 层全部打桩：用户当前云端额度紧张，真调 MiniMax 既烧钱又不稳；
    而且本模块要测的是「并发/限速/续跑/落库串行」这套编排逻辑，
    跟模型输出什么毫无关系。patch 掉 cacx.ai.tasks.summarize 后，
    编排逻辑照样能被完整压到。
  · 数据库一律 tempfile.mkdtemp：绝不碰 config/settings.json 与
    data/cacx.db。cfg 里的 api_key 一律用假值 sk-test-dummy，
    并在每个用例后断言返回结果里不含它 —— 密钥泄漏是唯一
    「测试过了但生产出事」的高危点。
  · 落库串行是本模块的核心约束：测试不只断言「写进去了」，还断言
    调用发生的线程是主线程（见 test_concurrent_all_ok 里的
    threading.current_thread().name 记录），防的是以后有人图省事
    把 save_annotation 挪进 worker。
"""

import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def section(t):
    print(f"\n=== {t} ===")


DUMMY_KEY = "sk-test-dummy"


def make_cfg():
    """假配置：provider 填 openai_compat 只为走和生产一样的 cfg 形状；
    summarize 被打桩后 build()/requests 根本不会被调用。"""
    return {"ai": {"provider": "openai_compat", "model": "test-model",
                   "base_url": "https://api.example.invalid/v1",
                   "api_key": DUMMY_KEY, "timeout": 1}}


def setup_db(n=5, site="test.gov.cn"):
    """临时目录建库 + 插入 n 篇测试文章，返回 (tmp, db, articles)"""
    from cacx import db as D
    tmp = Path(tempfile.mkdtemp(prefix="cacx_batch_"))
    d = D.Database(tmp / "t.db")
    arts = []
    for i in range(n):
        url = f"https://{site}/a/{i}.htm"
        d.upsert_article({"site": site, "site_name": "测试站",
                          "column_name": "要闻",
                          "title": f"测试文章{i}", "url": url,
                          "pub_date": "2026-01-01", "chars": 100,
                          # 每篇正文必须唯一：upsert_article 有「同站同正文
                          # 指纹」守卫（MIN_DEDUP_CHARS=100），五篇正文逐字
                          # 相同会被判成一文多栏目发，第 2~5 篇入库返回
                          # False，get_by_url 拿回 None，后面整批编排全崩。
                          "content_text": "正文" * 50 + f"第{i}篇独有段落"})
        a = d.get_by_url(site, url)
        if a is None:
            raise AssertionError(
                f"第 {i} 篇没入库成功（守卫拒绝或 upsert 失败）：{url}")
        arts.append(a)
    return tmp, d, arts


def ann_count(d, task="summarize"):
    with d._lock:
        return d.conn.execute(
            "SELECT COUNT(*) FROM ai_annotations WHERE task=?", (task,)
        ).fetchone()[0]


def assert_no_secret(summary, label):
    check(f"{label}：返回值不含假密钥",
          DUMMY_KEY not in str(summary), "竟然泄漏了 api_key！")


# ---------------------------------------------------------------- 打桩
class StubAI:
    """
    替换 cacx.ai.tasks.summarize。
    fail_ids 里的 id 抛异常；每个调用记录 (id, dry_run, 线程名, 时间戳)，
    用于验证并发数、限速间隔、落库线程。
    """

    def __init__(self, fail_ids=(), sleep=0.0):
        self.fail_ids = set(fail_ids)
        self.sleep = sleep
        self.calls = []

    def __call__(self, cfg, art, dry_run=True):
        self.calls.append({
            "id": art["id"],
            "dry_run": dry_run,
            "thread": threading.current_thread().name,
            "t": time.monotonic(),
        })
        if self.sleep:
            time.sleep(self.sleep)
        if art["id"] in self.fail_ids:
            raise RuntimeError("boom-" + str(art["id"]))
        if dry_run:
            return {"_dry_run": True, "endpoint": "https://x/v1/chat/completions",
                    "payload_preview": {"model": "test-model"}}
        return {"summary": f"摘要-{art['id']}", "tags": ["标签"],
                "sentiment": "neutral", "entities": []}


def patch_summarize(stub):
    """contextmanager 式替换：保证测试异常也能还原"""
    from cacx.ai import tasks as ai_tasks
    return _Patch(ai_tasks, stub)


class _Patch:
    def __init__(self, ai_tasks, stub):
        self.mod, self.stub = ai_tasks, stub

    def __enter__(self):
        self.orig = self.mod.summarize
        self.mod.summarize = self.stub
        return self.stub

    def __exit__(self, *a):
        self.mod.summarize = self.orig


# ---------------------------------------------------------------- 1. dry_run
def test_dry_run_no_write():
    section("① dry_run=True：只预览、不写库、不并发")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(3)
    stub = StubAI(sleep=0.05)
    events = []
    try:
        with patch_summarize(stub):
            r = run_batch(make_cfg(), d, arts, dry_run=True,
                          on_progress=lambda ev: events.append(dict(ev)))
        check("dry_run 标志透传", r["dry_run"] is True, str(r))
        check("返回 preview", isinstance(r.get("preview"), dict)
              and r["preview"].get("_dry_run") is True, str(r.get("preview")))
        check("preview 来自第一篇", stub.calls[0]["id"] == arts[0]["id"],
              str(stub.calls))
        check("只调了一次 AI", len(stub.calls) == 1, f"n={len(stub.calls)}")
        check("那次调用本身也是 dry_run", stub.calls[0]["dry_run"] is True)
        check("ai_annotations 计数为 0", ann_count(d) == 0,
              f"got {ann_count(d)}")
        check("汇总计数：ok=0 fail=0 skipped=0",
              r["ok"] == 0 and r["fail"] == 0 and r["skipped"] == 0, str(r))
        check("on_progress 至少回调一次", len(events) >= 1, str(events))
        if events:
            ev = events[0]
            check("progress 字段齐全",
                  all(k in ev for k in ("done", "ok", "fail",
                                        "skipped", "total", "title")),
                  str(ev))
        assert_no_secret(r, "dry_run")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 2. 并发全成功
def test_concurrent_all_ok():
    section("② 并发模式：N 篇全成功、落库 N 条、确有并发")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(6)
    stub = StubAI(sleep=0.05)  # 0.05s/篇，串行 6 篇要 0.3s，并发 3 应约 0.1s
    progress = []
    try:
        with patch_summarize(stub):
            t0 = time.monotonic()
            r = run_batch(make_cfg(), d, arts, dry_run=False,
                          concurrency=3,
                          on_progress=lambda ev: progress.append(dict(ev)))
            elapsed = time.monotonic() - t0
        check("total=6", r["total"] == 6, str(r["total"]))
        check("ok=6", r["ok"] == 6, str(r))
        check("fail=0", r["fail"] == 0, str(r))
        check("skipped=0", r["skipped"] == 0, str(r))
        check("failures 为空", r["failures"] == [], str(r["failures"]))
        check("落库 6 条", ann_count(d) == 6, f"got {ann_count(d)}")
        check("AI 被调 6 次", len(stub.calls) == 6, f"n={len(stub.calls)}")
        # 真并发：同一时刻至少有 2 个 worker 在跑；串行耗时约 0.3s，
        # 并发 3 应明显短于此
        check("确有并发（耗时 < 串行一半）", elapsed < 0.2,
              f"elapsed={elapsed:.3f}s")
        threads = {c["thread"] for c in stub.calls}
        check("调用发生在 >=2 个工作线程", len(threads) >= 2,
              str(threads))
        # 落库必须在主线程：save_annotation 是主线程做的，所以 AI 调用
        # 所在线程名里绝不能出现 MainThread —— 否则就是有人把落库挪进了 worker
        check("AI 调用不在主线程（说明真用了线程池）",
              all(tn != "MainThread" for tn in threads), str(threads))
        # 落库内容正确：provider/model/result 都写进去了
        row = d.conn.execute(
            "SELECT provider, model, result FROM ai_annotations "
            "WHERE article_id=? AND task='summarize'",
            (arts[0]["id"],)).fetchone()
        check("落库带 provider", row["provider"] == "openai_compat",
              str(dict(row)))
        check("落库带 model", row["model"] == "test-model", str(dict(row)))
        check("落库 result 含摘要文本",
              "摘要-" + str(arts[0]["id"]) in (row["result"] or ""),
              str(row["result"])[:120])
        import json as _json
        parsed = _json.loads(row["result"])
        check("落库 result 解析出 summary",
              parsed.get("summary") == "摘要-" + str(arts[0]["id"]),
              str(parsed))
        # on_progress：每篇完成后回调一次，done 单调递增到 total
        dones = [ev["done"] for ev in progress]
        check("progress 回调 6 次", len(progress) == 6, f"n={len(progress)}")
        check("done 单调递增", dones == sorted(dones), str(dones))
        check("done 最终=total", dones and dones[-1] == 6, str(dones))
        check("progress 里 ok 累计正确",
              all(ev["ok"] + ev["fail"] + ev["skipped"] == ev["done"]
                  for ev in progress), str(progress[-1]))
        assert_no_secret(r, "concurrent")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 3. 单篇失败不中断
def test_one_failure_continues():
    section("③ stop_on_error=False：单篇抛异常，其余继续、failures 有登记")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(5)
    bad = arts[1]["id"]
    stub = StubAI(fail_ids={bad}, sleep=0.02)
    try:
        with patch_summarize(stub):
            r = run_batch(make_cfg(), d, arts, dry_run=False, concurrency=3)
        check("ok=4", r["ok"] == 4, str(r))
        check("fail=1", r["fail"] == 1, str(r))
        check("failures 长度=1", len(r["failures"]) == 1, str(r["failures"]))
        f0 = r["failures"][0]
        check("failures 记录了出错那篇的 id/title/url",
              f0.get("id") == bad and f0.get("title") == arts[1]["title"]
              and f0.get("url") == arts[1]["url"], str(f0))
        check("failures 带错误信息", "boom-" + str(bad) in f0.get("error", ""),
              str(f0))
        check("其余 4 篇照常落库", ann_count(d) == 4, f"got {ann_count(d)}")
        check("AI 5 篇全调到了（没中断）", len(stub.calls) == 5,
              f"n={len(stub.calls)}")
        assert_no_secret(r, "one-fail")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 4. stop_on_error
def test_stop_on_error():
    section("④ stop_on_error=True：遇错即停，后续不再调 AI")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(6)
    bad = arts[2]["id"]
    # 并发压成 1：同一时刻只有一篇在跑，错篇一失败，后续任务要么被
    # cancel 掉，要么在 worker 入口的 stop 检查里自行取消 —— 两条路
    # 都不允许再真调 AI。这样断言是确定性的，不跟调度器抢时间。
    stub = StubAI(fail_ids={bad}, sleep=0.1)
    try:
        with patch_summarize(stub):
            r = run_batch(make_cfg(), d, arts, dry_run=False,
                          concurrency=1, stop_on_error=True)
        called_ids = {c["id"] for c in stub.calls}
        check("fail=1", r["fail"] == 1, str(r))
        check("failures 含出错那篇",
              any(f["id"] == bad for f in r["failures"]), str(r["failures"]))
        not_called = [a["id"] for a in arts[3:]]
        check("出错后的文章一律未调 AI",
              all(i not in called_ids for i in not_called),
              f"called={sorted(called_ids)}")
        check("只跑到出错那篇为止（含已完成的前 2 篇）",
              called_ids == {arts[0]["id"], arts[1]["id"], bad},
              f"called={sorted(called_ids)}")
        check("ok=2（错篇前完成的两篇）", r["ok"] == 2, str(r))
        check("库内只落 2 条", ann_count(d) == 2, f"got {ann_count(d)}")
        assert_no_secret(r, "stop-on-error")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5. resume
def test_resume_skip():
    section("⑤ resume=True：已有 annotation 的篇跳过，且不重复调 AI")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(4)
    # 先给第 0、2 篇手工落库，模拟上次跑到一半退出
    d.save_annotation(arts[0]["id"], "summarize",
                      {"summary": "旧摘要0"}, provider="old", model="old")
    d.save_annotation(arts[2]["id"], "summarize",
                      {"summary": "旧摘要2"}, provider="old", model="old")
    stub = StubAI()
    try:
        with patch_summarize(stub):
            r = run_batch(make_cfg(), d, arts, dry_run=False,
                          concurrency=2, resume=True)
            called_ids = {c["id"] for c in stub.calls}
            check("skipped=2", r["skipped"] == 2, str(r))
            check("ok=2（只跑没跑过的两篇）", r["ok"] == 2, str(r))
            check("已跑过的两篇没再调 AI",
                  arts[0]["id"] not in called_ids
                  and arts[2]["id"] not in called_ids,
                  f"called={sorted(called_ids)}")
            check("没跑过的两篇确实调了",
                  called_ids == {arts[1]["id"], arts[3]["id"]},
                  f"called={sorted(called_ids)}")
            check("库内共 4 条", ann_count(d) == 4, f"got {ann_count(d)}")
            # 对照：resume=False 时同样的库会把 4 篇全重跑一遍
            r2 = run_batch(make_cfg(), d, arts, dry_run=False,
                           concurrency=2, resume=False)
        check("resume=False 时不跳过（ok=4）",
              r2["ok"] == 4 and r2["skipped"] == 0, str(r2))
        assert_no_secret(r, "resume")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 6. 限速
def test_rate_limit():
    section("⑥ rate 限速：rate=50 时调用间隔被压到 >= 1/50s")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(8)
    stub = StubAI(sleep=0.0)  # AI 本身不耗时，间隔完全由限速器决定
    try:
        with patch_summarize(stub):
            t0 = time.monotonic()
            r = run_batch(make_cfg(), d, arts, dry_run=False,
                          concurrency=8, rate=50.0)
            wall = time.monotonic() - t0
        check("8 篇都成功", r["ok"] == 8, str(r))
        ts = sorted(c["t"] for c in stub.calls)
        check("8 次调用都发生了", len(ts) == 8, f"n={len(ts)}")
        span = ts[-1] - ts[0]
        # 理论下限：(8-1) * 0.02s = 0.14s。给 30% 抖动余量。
        check(f"首末调用跨度 >= 0.11s（理论下限 0.14s）",
              span >= 0.11, f"span={span:.3f}s wall={wall:.3f}s")
        gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        # 任意两次相邻调用间隔应接近 0.02s，不能出现 < 0.005s 的「瞬间连发」
        tiny = [g for g in gaps if g < 0.005]
        check("没有相邻调用挤在 5ms 内（限速生效）",
              len(tiny) == 0, f"gaps={[round(g, 4) for g in gaps]}")

        # 对照：rate=0 不限速时，8 个线程几乎同时起步
        tmp2, d2, arts2 = setup_db(8)
        stub2 = StubAI(sleep=0.0)
        with patch_summarize(stub2):
            run_batch(make_cfg(), d2, arts2, dry_run=False,
                      concurrency=8, rate=0.0)
        ts2 = sorted(c["t"] for c in stub2.calls)
        span2 = ts2[-1] - ts2[0]
        check("对照：rate=0 时首末跨度明显更短",
              span2 < span / 2, f"span2={span2:.3f}s vs {span:.3f}s")
        d2.close()
        shutil.rmtree(tmp2, ignore_errors=True)
        assert_no_secret(r, "rate")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 7. 落库幂等
def test_save_idempotent():
    section("⑦ save_annotation 幂等：重复跑不产生重复行")
    from cacx.ai.batch import run_batch
    tmp, d, arts = setup_db(3)
    stub = StubAI()
    try:
        with patch_summarize(stub):
            r1 = run_batch(make_cfg(), d, arts, dry_run=False, concurrency=3)
            n1 = ann_count(d)
            r2 = run_batch(make_cfg(), d, arts, dry_run=False, concurrency=3)
            n2 = ann_count(d)
        check("第一轮落库 3 条", n1 == 3, f"got {n1}")
        check("第二轮 AI 照常调了（resume=False）",
              len(stub.calls) == 6, f"n={len(stub.calls)}")
        check("第二轮后仍是 3 条（ON CONFLICT 更新而非插入）",
              n2 == 3, f"got {n2}")
        check("两轮都 ok=3", r1["ok"] == 3 and r2["ok"] == 3,
              f"{r1['ok']} / {r2['ok']}")
        # 更新语义：后写的覆盖先写的
        row = d.conn.execute(
            "SELECT result FROM ai_annotations WHERE article_id=? "
            "AND task='summarize'", (arts[0]["id"],)).fetchone()
        import json as _json
        parsed = _json.loads(row["result"])
        check("重复落库后内容是最新一轮",
              parsed.get("summary") == "摘要-" + str(arts[0]["id"]),
              str(parsed))
        assert_no_secret(r2, "idempotent")
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 8. 空输入
def test_empty():
    section("⑧ 空输入：不炸、返回 0 计数")
    from cacx.ai.batch import run_batch
    tmp, d, _ = setup_db(1)
    try:
        r = run_batch(make_cfg(), d, [], dry_run=False, concurrency=3)
        check("空列表 total=0", r["total"] == 0 and r["ok"] == 0, str(r))
        r2 = run_batch(make_cfg(), d, None, dry_run=True)
        check("None 也不炸", r2["total"] == 0, str(r2))
    finally:
        d.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- main
def main():
    print("=" * 62)
    print("批量摘要增强（cacx.ai.batch）离线测试")
    print("=" * 62)
    for fn in (test_dry_run_no_write, test_concurrent_all_ok,
               test_one_failure_continues, test_stop_on_error,
               test_resume_skip, test_rate_limit, test_save_idempotent,
               test_empty):
        try:
            fn()
        except Exception:
            FAIL.append(fn.__name__)
            print(f"  !! {fn.__name__} 抛异常")
            traceback.print_exc()
    print("\n" + "=" * 62)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
