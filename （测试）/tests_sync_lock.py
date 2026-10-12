# -*- coding: utf-8 -*-
"""
同步接口锁的自锁回归测试
================================================================
背景：点「开始同步」一点反应都没有，服务却没崩、别的接口也照常返回。

根因：api_sync_start 里 `with SYNC_LOCK:` 罩住了整段初始化，
其中调用了同样要加锁的 _sync_log()。threading.Lock 不可重入，
同线程二次加锁**永久阻塞**，而且从外面看完全无异常：
  - POST /api/sync 不返回 → 前端 await 一直挂着，按钮没反应
  - 之后所有 GET /api/sync/status 一起卡住 → 日志/状态/栏目进度全空白
  - /api/doctor 不碰这把锁 → 依然 200，探活会误判为「服务正常」
这类自锁不会抛异常、不会打日志、不会让进程退出，只在「真的走那条
默认分支」时才发作（站点留空 + 存在同源重复站组），因此必须用测试钉死。

跑法：py -3 tests_sync_lock.py
"""
import ast
import importlib
import inspect
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ⚠ 不能写 `from cacx.web import app` —— 那个名字是 FastAPI 实例
# （web/__init__.py 里 `from .app import app`），不是模块。
# 拿到实例就找不到 SYNC_LOCK 了，测试会报「对象没有这个属性」，
# 看起来像 bug 其实是我拿错东西了。
A = importlib.import_module("cacx.web.app")

TIMEOUT = 5.0        # 秒。自锁是永久阻塞，所以只能「限时等」而不是「等它结束」


def _run_limited(fn, timeout=TIMEOUT):
    """在独立线程里跑 fn；超时判定为自锁，返回 ('ok', 值) / ('blocked', None)。
    ⚠ 线程是 daemon：自锁时它会一直挂着，但不会挡住测试进程退出。"""
    box = {}

    def _wrap():
        try:
            box["r"] = fn()
        except BaseException as e:            # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_wrap, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return "blocked", None
    if "e" in box:
        raise box["e"]
    return "ok", box.get("r")


class TestSyncLock(unittest.TestCase):
    def test_锁必须可重入(self):
        """持锁时再调 _sync_log 不能卡住 —— 这是整个 bug 的判据。
        用真实的 SYNC_LOCK，而不是新建一个 RLock 自证。"""
        def _inside_lock():
            with A.SYNC_LOCK:
                A._sync_log("自检：临界区内写日志")
            return len(A.SYNC_STATE["logs"])

        st, _ = _run_limited(_inside_lock)
        self.assertEqual(st, "ok",
                         "SYNC_LOCK 上锁后无法再次获取 —— 自锁已复现，"
                         "换回 threading.Lock 会让同步接口永久卡死")

    def test_status端点在持锁写日志时仍可返回(self):
        """模拟真实故障：后台线程在跑、日志不断追加，
        此时前端轮询的 status 必须照样返回，不能被卡住。"""
        stop = threading.Event()

        def _writer():
            while not stop.is_set():
                with A.SYNC_LOCK:
                    A._sync_log("模拟后台日志")
                threading.Event().wait(0.01)

        w = threading.Thread(target=_writer, daemon=True)
        w.start()
        try:
            st, v = _run_limited(lambda: A.api_sync_status(since=0))
        finally:
            stop.set()
            w.join(2)
        self.assertEqual(st, "ok", "后台持续写日志时 status 端点被卡死")
        self.assertIn("running", v)
        self.assertIn("logs", v)

    def test_start端点临界区内不调加锁函数(self):
        """第二道防线：即使有人日后把锁换回普通 Lock，
        只要临界区内不出现 _sync_log 调用，也就不会自锁。
        用 AST 静态查，不靠跑（跑的话自锁只会让测试超时而非失败）。"""
        src = inspect.getsource(A.api_sync_start)
        tree = ast.parse(inspect.cleandoc(src))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            # 认准 SYNC_LOCK 这一把锁，别把别的 with 一并算进来
            if not any("SYNC_LOCK" in ast.dump(item.context_expr)
                       for item in node.items):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id in ("_sync_log", "api_sync_status")):
                    offenders.append(
                        f"{sub.func.id}() 出现在第 {sub.lineno} 行的 "
                        f"SYNC_LOCK 临界区内")
        self.assertEqual(offenders, [],
                         "临界区内调用了要加锁的函数：" + "；".join(offenders))

    def test_全局没有裸threading_Lock(self):
        """把 SYNC_LOCK 换回 threading.Lock 的改动，
        应该在这条测试上先炸，而不是等用户在界面上发现。"""
        self.assertTrue(isinstance(A.SYNC_LOCK, type(threading.RLock())),
                        "SYNC_LOCK 必须用 threading.RLock()（可重入）")


if __name__ == "__main__":
    unittest.main(verbosity=2)