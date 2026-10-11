# -*- coding: utf-8 -*-
"""
批量摘要增强（并发 / 限速 / 断点续跑 / 汇总）核心逻辑
================================================================
为什么把它从 CLI 的 for 循环里抽出来
    原先 cmd_ai 里那段串行循环（cli/main.py 490-506）既管选文章又管
    落库，Web 端想要进度条就得整段抄一遍 —— 两份计数口径迟早漂移。
    这里只做「拿一批文章，跑完摘要，交回一份汇总」，CLI 与 Web 两端
    都只是它的表现层。

线程模型（整个模块的命根子，见 docs/工程留痕.md §3.3 压测结论）
    SQLite 是**单连接 + RLock，而且读也持锁**：共享连接下一旦有写线程
    提交事务，别的线程正在用的游标就会失效（InterfaceError）。所以：
      · 工作线程只做一件事 —— 等 AI 网络响应，**绝不碰 d**；
      · resume 的 get_annotation、成功后的 save_annotation 全部收回
        主线程串行做。跨线程写库在压测里是必炸，不是偶发。

限速
    rate = 每秒最多多少次 AI 调用。一把全局锁 +「下一个可放行时刻」
    时间戳：拿到锁的线程算好自己的出发时刻登记后立刻释放，真正的
    请求在锁外并发。锁内 sleep 看似阻塞，其实是刻意为之 —— 把放行
    节奏串行化，跨线程的总速率才压得住。rate=0 表示不限速，锁直接退化成空操作。

dry_run（预览铁律）
    不真调就绝不写库、绝不并发。只对第一篇取一次请求体给人看一眼。
    这条沿用 CLI 现有行为：预览模式下误触也烧不掉配额。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from . import tasks as ai_tasks

TASK = "summarize"


class _BatchCancelled(Exception):
    """内部信号：本篇因 stop_on_error 被取消，根本没调 AI，不算失败。

    单独成异常而不是返回哨兵，是为了让 as_completed 的 fut.result()
    路径统一 —— 取消和真失败都走 except，再按类型分流计数。
    """


class _RateLimiter:
    """全局节流：把跨线程的 AI 调用平均速率压到 rate 次/秒以内。"""

    def __init__(self, rate: float):
        self.min_interval = (1.0 / rate) if rate and rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0  # monotonic 时间戳：下一次放行最早可在何时

    def acquire(self):
        if self.min_interval <= 0:
            return
        # 锁内 sleep 是刻意的：放行节奏必须在同一把锁里排先后，
        # 否则两个线程同时算出「现在就能发」，瞬间就把速率顶穿了。
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = now + self.min_interval


def _emit(on_progress, ev: Dict[str, Any]):
    """进度回调只是给人看的，它崩了不能连累整批任务。"""
    if on_progress is None:
        return
    try:
        on_progress(ev)
    except Exception:
        pass


def run_batch(cfg: Dict[str, Any], d, articles: List[Dict[str, Any]],
              dry_run: bool = True,
              concurrency: int = 3,
              rate: float = 0.0,
              resume: bool = False,
              stop_on_error: bool = False,
              on_progress: Optional[Callable[[Dict[str, Any]], None]] = None
              ) -> Dict[str, Any]:
    """
    批量跑文章摘要。

    返回 {"total","ok","fail","skipped","dry_run","failments":[{"id","title","url","error"}]}
    dry_run 时额外带 "preview"。
    """
    total = len(articles or [])
    summary: Dict[str, Any] = {"total": total, "ok": 0, "fail": 0,
                               "skipped": 0, "dry_run": dry_run,
                               "failures": []}
    if not articles:
        return summary

    ai = cfg.get("ai") or {}
    provider = ai.get("provider", "")
    model = ai.get("model", "")

    # -------------------------------------------------- dry_run：只取第一篇预览
    if dry_run:
        art0 = articles[0]
        try:
            preview = ai_tasks.summarize(cfg, art0, dry_run=True)
        except Exception as e:
            # 预览失败也要显式交代去向，不能让调用方以为「没事」
            summary["fail"] = 1
            summary["failures"].append({
                "id": art0.get("id"), "title": art0.get("title"),
                "url": art0.get("url"),
                "error": f"预览失败: {type(e).__name__}: {e}"})
            _emit(on_progress, {"done": 1, "ok": 0, "fail": 1,
                                "skipped": 0, "total": total,
                                "title": art0.get("title")})
            return summary
        summary["preview"] = preview
        _emit(on_progress, {"done": 1, "ok": 0, "fail": 0,
                            "skipped": 0, "total": total,
                            "title": art0.get("title")})
        return summary

    # --------------------------------------- resume 判断必须在主线程做
    # get_annotation 读的是共享连接，丢进工作线程和主线程的写并发就是
    # 压测里复现过的 InterfaceError。趁还没提交线程池，先在主线程筛一遍。
    pending: List[Dict[str, Any]] = []
    for art in articles:
        if resume and d.get_annotation(art["id"], TASK) is not None:
            summary["skipped"] += 1
            _emit(on_progress, {
                "done": summary["ok"] + summary["fail"] + summary["skipped"],
                "ok": summary["ok"], "fail": summary["fail"],
                "skipped": summary["skipped"], "total": total,
                "title": art.get("title")})
        else:
            pending.append(art)

    if not pending:
        return summary

    limiter = _RateLimiter(rate)
    stop = threading.Event()  # stop_on_error 时让尚未开跑的任务自行了断

    def worker(art: Dict[str, Any]):
        # 工作线程里唯一允许碰的全局对象就是限速器；d 一个字节都不许碰
        limiter.acquire()
        if stop.is_set():
            raise _BatchCancelled()
        try:
            res = ai_tasks.summarize(cfg, art, dry_run=False)
        except Exception:
            # 出错的 worker 自己当场拉闸，而不是等主线程被唤醒再处理
            # fut.result()：单 worker 池里，worker 抛完异常会立刻从队列
            # 拽走下一篇，主线程的 stop.set() 永远慢这一步 —— 实测放过
            # 了一篇。异常路径上的取消必须在异常发生的那一刻生效。
            if stop_on_error:
                stop.set()
            raise
        return art, res

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(worker, art): art for art in pending}
        for fut in as_completed(futs):
            art = futs[fut]
            try:
                art_done, res = fut.result()
            except _BatchCancelled:
                continue  # 主动取消的：没调 AI，不计 ok 也不计 fail
            except Exception as e:
                summary["fail"] += 1
                summary["failures"].append({
                    "id": art.get("id"), "title": art.get("title"),
                    "url": art.get("url"),
                    "error": f"{type(e).__name__}: {e}"})
                if stop_on_error:
                    stop.set()
                    for f in futs:
                        f.cancel()  # 没开跑的直接取消；已在飞的由 worker 里的 stop 检查兜底
            else:
                # 落库收回主线程串行调用：共享连接跨线程写必炸
                d.save_annotation(art_done["id"], TASK, res,
                                  provider=provider, model=model)
                summary["ok"] += 1
            _emit(on_progress, {
                "done": summary["ok"] + summary["fail"] + summary["skipped"],
                "ok": summary["ok"], "fail": summary["fail"],
                "skipped": summary["skipped"], "total": total,
                "title": art.get("title")})
            if stop_on_error and stop.is_set():
                break  # 已在飞的任务让它跑完（shutdown 会等），但不再处理新结果

    return summary
