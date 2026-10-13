# -*- coding: utf-8 -*-
"""
反向验证：故意把 autodemo.py 改坏，看它**会不会真的报错**。

为什么要做这个：
  自动演示最危险的失败模式不是「报错」，
  而是**看起来跑完了、其实关键那步没发生** ——
  比如证据面板打开着但是空的，截图拍了但是糊的，
  字幕写着 24 条证据而面板上其实 0 条。

  所以每个变异都必须能触发 **AssertionError / DemoError**，
  而不是 SyntaxError、NameError 这种「压根没跑起来」的假通过。
  只认前者。

跑法：先确保服务在 8770 上，然后
    py -3 （测试）\_rev_autodemo.py
"""
import ast
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "autodemo.py"

# 这些是**不该算抓住**的错误：说明脚本没跑起来，等于没测到东西
FAKE = ("SyntaxError", "IndentationError", "NameError: name 'cap'",
        "ModuleNotFoundError", "TypeError: 'NoneType'")


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(headless=True):
    args = [sys.executable, str(TARGET), "--slow", "0.12"]
    if headless:
        args.append("--headless")
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT),
                       timeout=900)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- 变异表
# 每个变异 = 一处**单行**替换。
# ⚠ 必须是单行：多行替换会因缩进回退判错而连带吃掉周围代码，
#   于是「脚本挂了」被误当成「变异被抓住」，测了个寂寞。
#
# ⚠ 变异点都在 `book.mark(...)` / 判定表达式上，**不在 if 上**了 ——
#   第一版把变异打在 `if "打开原文" not in det:` 上，结果 8/10 没抓住：
#   因为数据正常时那个条件本来就为假，删掉它脚本照样跑完 rc=0。
#   「装饰性断言」拦不住任何东西，只有账本 + 结尾核账才是真保险丝。
# ---------------------------------------------------------------- 变异表
# 每个变异 = 一处替换（尽量单行；确需多行时每行都是完整语句）。
#
# ==========================================================================
# ⚠⚠ 变异方向踩过两次坑 —— 这是血泪换来的规矩，务必先读
# ==========================================================================
# 第一版打在 `if "打开原文" not in det:` 上            ->  0/10 全没抓住
# 第二版打在 `book.mark(..., "打开原文" in det,)` 上  ->  2/11 只抓住 2 个
#
# **两次都错在同一个地方**：改的是「判定表达式」，把它改成永远为真。
# 而在数据正常时，判定**本来就该为真** ——
# 所以核账照样全绿、脚本照样 rc=0，一个 bug 都拦不住。
#
# 把判定改坏 = 把探测器拆了，那当然什么也测不到。
#
# ✅ 正确方向：**破坏被观测的事实，判定逻辑一个字不动。**
#    · 让事实真的出错（证据真的没了 / 字幕真的不渲染 / 图真的滚出屏幕）
#    · 判定与核账应当**发现**问题并拦住
#    · 跑不过 = 判定失灵，这才是真 bug
# ==========================================================================
# 这些变异**实测抓不住**，且已查明原因 —— 属于「本来就不是保险丝」，
# 不算缺陷。列出来是为了不让它们污染 FAIL 统计，也不让人以为漏改了。
# 名称需与 MUTANTS 里的第一项完全一致。
KNOWN_UNCATCHABLE = {
    "不等证据就往下走":
        "那处 wait_for_function 是速度优化不是保险丝；"
        "真正兜底的是账本里的「备选一有原文链接 / 含两端主体」",
    "健康检查放水":
        "服务正常时健康检查本来就通过，改成只打印也照样跑完 —— "
        "与「装饰性 if」同一性质：它只在服务真挂了时才起作用，"
        "而那时它确实会中止（见 run() 里后续接口全失败）。",
}

MUTANTS = [
    # 1 证据面板真的被清空 —— 原文链接、主体全没了，判定应发现
    ("证据面板真的空",
     "        det = pg.inner_text(\"#gp_detail\")",
     "        pg.evaluate(\"()=>{document.getElementById('gp_detail')"
     ".innerHTML='<div>暂无</div>'}\")\n        det = pg.inner_text(\"#gp_detail\")"),

    # 2 备选① 的证据真的取不到（接口指向不存在的边 -> 静默 0 条）
    ("备选① 真的取不到证据",
     '    return jget(f"/api/graph/evidence?src={q(src)}&dst={q(dst)}")["items"]',
     '    return jget(f"/api/graph/evidence?src={q(src)}&dst=__NO_SUCH__")["items"]'),

    # 3 字幕真的不渲染（观众看不到讲稿）
    ("字幕真的不渲染",
     "               el.querySelector('.say').innerHTML = say;",
     "               el.querySelector('.say').innerHTML = '';"),

    # 4 字幕真的落到底部，真的盖住右下角的证据面板
    ("字幕真的放底部遮挡",
     "#demo-cap{position:fixed;left:0;right:0;top:0;",
     "#demo-cap{position:fixed;left:0;right:0;top:auto;bottom:0;"),

    # 5 时间轴真的没滚 —— 讲发现时图在屏幕外
    ("时间轴真的没滚",
     "                 const t = document.getElementById('tl_vol');",
     "                 window.scrollTo(0, 0); const t = null;"),

    # 6 不等证据面板加载就往下走 —— 面板还停在「正在取原文证据…」
    # ⚠ 锚点必须与 autodemo.py **逐字节相同**（源文件里单引号全是转义的）。
    # 【已知抓不住 · 实测结论，不是缺陷】
    #   把它换成 `() => true` 后脚本照样 rc=0 —— 因为证据面板实际加载
    #   很快，不等也能拿到正确内容。
    #   说明那处 wait_for_function 是**速度优化，不是保险丝**；
    #   真正兜住「证据没来」的是账本里的「备选一有原文链接 / 含两端主体」。
    #   留在这里是为了记录这个结论：别指望那处等待能拦住任何东西。
    ("不等证据就往下走",
     '        pg.wait_for_function(\n            """() => /条原文证据/.test(\n                   document.getElementById(\'gp_detail\')?.textContent||\'\')""",\n            timeout=60000)',
     '        pg.wait_for_function("() => true", timeout=60000)',
    ),

    # 7 中文 URL 真的抄错（%E8%82%A2 而非 %E5%86%9C）-> 接口静默返回 0 条
    ("URL 编码真的手写错",
     '    q = urllib.parse.quote\n'
     '    return jget(f"/api/graph/evidence?src={q(src)}&dst={q(dst)}")["items"]',
     '    return jget("/api/graph/evidence?src=%E4%B8%AD%E5%A4%AE%E7%BD%91%E4%BF%A1%E5%8A%9E'
     '&dst=%E8%82%A2%E4%B8%9A%E5%86%9C%E6%9D%91%E9%83%A8")["items"]'),

    # 8 结尾核账真的被拆掉 —— 事故已发生却无人拦（最关键的一个）
    ("结尾核账被拆",
     "        bad = []\n        for key, why in required:",
     "        bad = []\n        return  # 被拆掉了\n        for key, why in required:"),

    # 9 服务健康检查真的失效：出错了只是打印，然后接着演
    ("健康检查放水",
     '        raise DemoError(\n'
     '            f"服务没起来（{e}）。\\n"\n'
     '            f"       先双击 start-cacx.bat，等约 30 秒后再跑本脚本。")',
     '        say("服务没起来，但先继续")'),

    # 10 点不存在的标签页
    ("点不存在的标签",
     'pg.click("#tab_panel")',
     'pg.click("#tab_NOT_EXIST")'),
]


def main():
    if not TARGET.exists():
        print("找不到 autodemo.py")
        return 1

    # ---- 先验锚点，几秒就能出结果 --------------------------------------
    # ⚠ 顺序很要紧：锚点失效时每个变异都会白跑一次演示（30 秒 × N），
    #   才发现「锚点匹配 0 次」。这一段是纯文本比对，不启动浏览器。
    #   本次就因为锚点错，连着浪费了三轮 5 分钟。
    src = TARGET.read_text(encoding="utf-8")
    bad_anchor = []
    for name, old, _new in MUTANTS:
        n = src.count(old)
        if n != 1:
            bad_anchor.append((name, n))
    if bad_anchor:
        print("=" * 70)
        print("🔴 锚点不合格，先修锚点再跑（否则每个变异都要白跑一次演示）")
        print("=" * 70)
        for name, n in bad_anchor:
            print(f"  {name}：在 autodemo.py 里命中 {n} 次（应为 1）")
        print()
        print("  锚点必须与源文件**逐字节相同**。若目标代码里有转义（\\' 之类），")
        print("  手抄必错 —— 改用「从 autodemo.py 自动抽取」的方式生成。")
        return 3
    print(f"  锚点自检：{len(MUTANTS)}/{len(MUTANTS)} 全部命中")
    print()

    # 基线：原版必须跑通，否则谈不上验证
    print("=" * 70)
    print("基线：未改动的 autodemo.py 必须跑通")
    print("=" * 70)
    base = TARGET.read_text(encoding="utf-8")
    bak = TARGET.with_suffix(".py.revbak")
    shutil.copy2(TARGET, bak)
    t = time.time()
    rc, out = run()
    dt = time.time() - t
    if rc != 0:
        print("🔴 基线就失败了，变异验证无从谈起。先修好再测。")
        print(out[-2500:])
        return 2
    print(f"  PASS  原版跑通（{dt:.0f}s）")
    baseline_tail = out.strip().splitlines()[-6:]
    for x in baseline_tail:
        print(f"        {x}")

    results = []
    print()
    print("=" * 70)
    print(f"变异反向验证（{len(MUTANTS)} 个）")
    print("=" * 70)

    for name, old, new in MUTANTS:
        src = TARGET.read_text(encoding="utf-8")
        n = src.count(old)
        if n != 1:
            # 锚点失效是「这个变异没测到」，不是「这个 bug 抓不住」——
            # 两者性质不同，别混进失败统计。
            results.append((name, None, f"锚点匹配 {n} 次（应为 1），本变异无效"))
            print(f"  SKIP  {name}   锚点匹配 {n} 次")
            TARGET.write_text(src, encoding="utf-8")
            continue

        try:
            mutated = src.replace(old, new, 1)
            ast.parse(mutated)          # 先确认不是语法坏掉
        except SyntaxError as e:
            results.append((name, None, f"变异本身语法错（测不到东西）: {e}"))
            print(f"  BAD   {name}   变异自身语法错")
            continue

        TARGET.write_text(mutated, encoding="utf-8")
        try:
            rc, out = run()
        finally:
            TARGET.write_text(src, encoding="utf-8")

        fake = [k for k in FAKE if k in out]
        real = rc != 0 and not fake

        if real:
            tail = [l for l in out.strip().splitlines() if l.strip()][-2:]
            why = " / ".join(t.strip() for t in tail)[:150]
            results.append((name, True, why))
            print(f"  PASS  {name}   rc={rc}  {why}")
        elif name in KNOWN_UNCATCHABLE:
            # 如实记录，不计入失败：那处代码本来就不是保险丝
            results.append((name, None, KNOWN_UNCATCHABLE[name]))
            print(f"  N/A   {name}   {KNOWN_UNCATCHABLE[name]}")
        else:
            detail = ("假失败（" + fake[0] + "）") if fake else \
                f"脚本照常跑完 rc={rc} —— 这个 bug 抓不住！"
            results.append((name, False, detail))
            print(f"  FAIL  {name}   {detail}")

    # 还原确认
    # ⚠ 用**内容**比，不依赖备份文件还在：
    #   上一轮实测踩过：中途手工清理把 .revbak 删了，
    #   结果还原校验自己先崩了，害得整轮结果作废。
    print()
    same = TARGET.read_text(encoding="utf-8") == base
    print(f"  还原校验：{'一致' if same else '❌ 不一致！'}")
    if bak.exists():
        bak.unlink()

    caught = [n for n, r, _ in results if r is True]
    failed = [n for n, r, _ in results if r is False]
    known = [n for n, r, _ in results if r is None]
    print()
    print("=" * 70)
    print(f"抓住 {len(caught)}/{len(results) - len(known)}"
          f"（另有 {len(known)} 个已知非保险丝，不计分）")
    if failed:
        print("没抓住：" + "、".join(failed))
    print("=" * 70)
    return 1 if failed or not same else 0


if __name__ == "__main__":
    sys.exit(main())
