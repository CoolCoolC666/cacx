# -*- coding: utf-8 -*-
"""量一件事：从进程启动到端口真正能连上，中间有多久。

为什么要量：`web/app.py` 里 `webbrowser.open(url)` 排在 `uvicorn.run(...)`
**之前**，也就是说浏览器打开的那一刻端口还没人监听。这个时间差越大，
用户越容易看到一个"打不开"的页面 —— 而异常还被 `except: pass` 吞了，
窗口里什么都不显示。

量出来是几百毫秒还是几十秒，决定了这算"偶发"还是"必现"。
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799


def listening(port, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


print("启动 cacx serve（--no-browser --port %d）…" % PORT)
t0 = time.time()
p = subprocess.Popen(
    [sys.executable, "-m", "cacx", "serve", "--no-browser", "--port", str(PORT)],
    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace", env={**__import__("os").environ},
)
try:
    while time.time() - t0 < 180:
        if listening(PORT):
            print(f"端口 {PORT} 开始监听：{time.time() - t0:.2f} 秒")
            break
        if p.poll() is not None:
            print("进程提前退出，输出：")
            print((p.stdout.read() or "")[-1500:])
            break
        time.sleep(0.05)
    else:
        print("180 秒内没监听起来")
finally:
    p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()

print()
print("这段时间就是 webbrowser.open() 早于服务器就绪的窗口：")
print("  浏览器在这段时间内打开 -> 连接被拒绝 -> 用户以为坏了")
