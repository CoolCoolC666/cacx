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


def _classify_failure(e: BaseException) -> tuple:
    """
    把异常归类为 (是否值得重试, 给人看的说明)。

    为什么要分：以前所有失败都被压成 `type(e).__name__: e`，
    界面上「失败 12」看起来都一样，用户既不知道是网络抖了（该重跑）
    还是 api_key 错了（重跑一万次也没用），只能整批重来 —— 配额白烧。

    判据：
      · AIError 自带 retryable —— 429/5xx/超时 为 True，
        401/403/400/404 等为 False（重试无意义，还可能撞风控）
      · 其它异常默认**不可重试**：来源不明时宁可不重试，
        盲目重试未知异常可能反复触发同一个确定性失败
    """
    from .base import AIError
    if isinstance(e, AIError):
        if e.retryable:
            return True, "可重试（限流/超时/服务端临时故障）"
        if e.status_code in (401, 403):
            return False, "鉴权失败 —— 检查 api_key，重试无用"
        if e.status_code in (400, 422):
            return False, "请求被拒（参数/模型名问题）—— 检查模型配置"
        if e.status_code == 404:
            return False, "接口地址不存在 —— 检查 base_url"
        return False, "配置或请求问题，重试无用"
    return False, f"{type(e).__name__}（原因不明，建议先手动试一篇）"


def run_batch(cfg: Dict[str, Any], d, articles: List[Dict[str, Any]],
              dry_run: bool = True,
              concurrency: int = 3,
              rate: float = 0.0,
              resume: bool = False,
              stop_on_error: bool = False,
              batch_id: str = "",
              on_progress: Optional[Callable[[Dict[str, Any]], None]] = None
              ) -> Dict[str, Any]:
    """
    批量跑文章摘要。

    返回 {"total","ok","fail","skipped","dry_run","failures":[…],"retriable"}
    dry_run 时额外带 "preview"。

    中途中断语义（本次重点）
    ------------------------------------
    · **每成功一篇立即落库**（save_annotation 在主线程、逐个调用）。
      进程被 kill 最多丢 concurrency 篇，其余都在库里；
      重跑走 incremental/resume 会被跳过 —— 不重复烧配额。
    · **失败单独留痕**（record_ai_failure）并标注该不该重试。
      失败绝不写进 ai_annotations —— 否则 resume 会把它当已完成跳过，
      一个瞬时失败会被固化成永久缺口。
    · 自动重试交给 AI 调用层（指数退避 + 尊重 Retry-After），
      这里只负责把「正在重试第 N 次」如实报给界面。
    """
    total = len(articles or [])
    summary: Dict[str, Any] = {"total": total, "ok": 0, "fail": 0,
                               "skipped": 0, "cancelled": 0, "dry_run": dry_run,
                               "failures": [], "retriable": 0,
                               "interrupted": False, "not_run": 0}
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
                summary["cancelled"] += 1
                continue  # 主动取消的：没调 AI，不计 ok 也不计 fail
            except Exception as e:
                retryable, hint = _classify_failure(e)
                msg = f"{type(e).__name__}: {e}"
                summary["fail"] += 1
                if retryable:
                    summary["retriable"] += 1
                summary["failures"].append({
                    "id": art.get("id"), "title": art.get("title"),
                    "url": art.get("url"),
                    "error": msg, "retryable": retryable, "hint": hint})
                # 失败留痕：写进独立的 ai_failures（不碰 ai_annotations）。
                # 这样进程重启后仍能查到「哪几篇没成、该不该重试」。
                try:
                    d.record_ai_failure(art.get("id"), TASK, msg,
                                        retryable=retryable,
                                        attempts=(cfg.get("ai", {})
                                                  .get("retry_attempts", 1)),
                                        batch_id=batch_id)
                except Exception:
                    pass  # 留痕失败不能反过来把整批搞崩
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
                summary["interrupted"] = True
                break  # 已在飞的任务让它跑完（shutdown 会等），但不再处理新结果

    # 被 stop_on_error 打断时，剩下的 pending 还没跑 —— 必须显式交代，
    # 否则界面只显示「失败 1」，用户以为剩下的 9 篇也跑过了。
    # 用计数而非 id 差集：id 可能重复出现在 failures 与成功里，
    # 靠集合相减得到的是「看起来像」的数，不是真的没跑数。
    handled = summary["ok"] + summary["fail"] + summary["cancelled"]
    summary["not_run"] = max(0, len(pending) - handled)
    if summary["not_run"] and not summary["interrupted"]:
        summary["interrupted"] = True
    return summary
