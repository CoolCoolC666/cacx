# -*- coding: utf-8 -*-
"""证明浏览器是在**端口能连上之后**才被打开的。

## 为什么这道闸必须存在

原来的代码是

    webbrowser.open(url)          # ← 端口还没人监听
    uvicorn.run(app, ...)         # ← 端口在这行才开始监听

实测那个窗口是 0.92 秒（`_measure_bind_race.py`）。浏览器在这段时间里拿到
「连接被拒绝」，而外面还套着 `except: pass`，所以用户看到的现象是
**窗口显示一切正常，但网站打不开** —— 没有任何报错可以指认原因。

肉眼验证不了这个顺序（浏览器弹出来的时机由系统决定，而且本机不一定装了
默认浏览器），所以只能把 `webbrowser.open` 换掉，自己记一笔账：
它被调用时，端口到底通没通。
"""
import contextlib
import importlib
import io
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ⚠ `import cacx.web.app as APP` 拿到的是 **FastAPI 实例**
#   （cacx/web/__init__.py 把它 re-export 了），不是模块 ——
#   于是 APP._open_browser_when_listening 会报
#   "'FastAPI' object has no attribute ..."。这个坑本轮之前就踩过。
#   要拿模块只能用 importlib.import_module。
APP = importlib.import_module("cacx.web.app")

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def listening(port, timeout=0.3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


PORT = 8797
call = {}


def fake_open(url):
    call["url"] = url
    call["port_listening"] = listening(PORT)
    call["at"] = time.time()


APP.webbrowser = type("W", (), {"open": staticmethod(fake_open)})

print("=" * 66)
print("浏览器自动打开时机")
print("=" * 66)

# 端口一开始没人听，1.2 秒后才 bind —— 模拟真实的 0.92 秒窗口
# ⚠ 不要"先绑一下再关掉"来制造"没人监听"的状态：我第一版那么写了，
#   结果 bind_later 线程 bind 时报 address already in use，
#   **线程里的异常没人看**，于是探针一直等、闸门超时，
#   报出来的是"浏览器没被打开"—— 一个把"测试自己坏了"说成
#   "产品坏了"的假结论。端口本来就是空的，直接断言就行。
stop = threading.Event()
timeline = []


def sampler():
    """独立采样端口状态。这样"打开那一刻通没通"就有旁证，
    而不是靠闸门自己在同一线程里连问两次。"""
    while not stop.is_set():
        timeline.append((time.time() - t0, listening(PORT)))
        time.sleep(0.05)


def bind_later():
    time.sleep(1.2)
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", PORT))
        # ⚠⚠ backlog 必须给够，**而且要真的 accept**。
        #   这个坑我踩了一轮：原来的夹具 `listen(1)` + 从不 accept，
        #   而"探一下端口通不通"本身就是一次 connect —— 连接握手完成后
        #   会留在 accept 队列里，直到服务端 accept 才被取走。
        #   于是**探针自己把唯一的槽位占满**，之后每一次探都被拒，
        #   闸门就一直等、等不到，最后报「浏览器没被打开」。
        #   症状极其误导：那明明是夹具坏了，产品代码是好的。
        #   （产品代码同样会消耗一个槽位，但 uvicorn 的 backlog 是 2048，
        #     探一次无关紧要；这里给 64 并起 accept 循环是为了夹具本身可靠。）
        s.listen(64)
        s.settimeout(0.5)

        def _drain():
            while not stop.is_set():
                try:
                    c, _ = s.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                c.close()

        threading.Thread(target=_drain, daemon=True).start()
        stop.wait(20)
    except OSError as e:                 # 线程里的异常必须自己喊出来
        print(f"        [测试夹具出错] bind_later 失败：{e}")
    finally:
        try:
            s.close()
        except OSError:
            pass


threading.Thread(target=bind_later, daemon=True).start()
check("一开始端口确实没人监听", not listening(PORT))
t0 = time.time()
threading.Thread(target=sampler, daemon=True).start()

APP._open_browser_when_listening(f"http://127.0.0.1:{PORT}/", "127.0.0.1", PORT,
                                 timeout=30)
for _ in range(150):                    # 最多等 15 秒
    if "at" in call:
        break
    time.sleep(0.1)

check("浏览器最终被打开了", "at" in call, f"{time.time() - t0:.1f} 秒后")
if "at" in call:
    sampled = [on for ts, on in timeline if ts <= call["at"]]
    print(f"        采样点 {len(timeline)} 个，打开前最后一个采样 = "
          f"{sampled[-1] if sampled else '无'}")
    print(f"        open() 时自测 = {call['port_listening']}")
    check("打开时端口已经在监听（旁证：采样时间线）",
          bool(sampled) and sampled[-1] is True)
    check("打开时端口已经在监听（自测）", call["port_listening"] is True)
    check("不是一开始就开的（说明确实等过）", call["at"] - t0 >= 1.0,
          f"等了 {call['at'] - t0:.2f} 秒")
    check("打开的是对的地址", call["url"] == f"http://127.0.0.1:{PORT}/")

stop.set()

# 开不了也要吭声：把 open 换成会抛异常的
APP.webbrowser = type("W", (), {"open": staticmethod(
    lambda u: (_ for _ in ()).throw(RuntimeError("no browser here")))})
srv2 = socket.socket()
srv2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv2.bind(("127.0.0.1", 8796))
srv2.listen(64)
stop2 = threading.Event()


def _drain2():
    # 收尾也要干净：线程里冒出来的 traceback 会印在闸门输出末尾，
    # 哪怕前面全 PASS，读的人也会以为哪里坏了 —— 那是噪音，不是结论。
    srv2.settimeout(0.3)
    while not stop2.is_set():
        try:
            c, _ = srv2.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        c.close()


threading.Thread(target=_drain2, daemon=True).start()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    APP._open_browser_when_listening("http://127.0.0.1:8796/", "127.0.0.1", 8796,
                                     timeout=10)
    time.sleep(1.0)
stop2.set()
time.sleep(0.5)
srv2.close()
seen = buf.getvalue()
check("浏览器打不开时会打印提示（不再静默吞掉）", "请手动访问" in seen, seen.strip()[-80:])

print()
print(f"通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
