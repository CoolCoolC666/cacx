# -*- coding: utf-8 -*-
"""反向验证：本轮（缩放 + 首屏口径）新加的守卫，拆了真的会红吗？

项目铁律：新增测试必须**反向验证** —— 故意拆掉守卫，确认测试**会红**，
然后真还原（shutil.copy2 备份回去 + sha256 校验），不能只 unlink 备份了事。

变异点（全部作用在 tests_measure_gate.py 真读的 index.html 上）：
  M1  去掉首屏兜底：legendNotReady 三元退化成 lgOn
      → 首屏 lgSend 为空 → 节点_types 不传 → 回到旧口径 1633/3921
      → test_首屏图例未渲染时也要兜底 应红
  M2  去掉 fit：把「物理收敛后 fit」改成建图后立刻 fit
      → test_加载完要自动铺满 应红
  M3  把实体类型变量改回 types（与「关系类型」那节撞名）
      → 整页 ScriptError，图谱请求一个都不发
      → test_实体类型变量不得叫types 应红

任何一个变异没被抓到，说明测试在假绿，必须停下修正测试本身。
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "cacx" / "web" / "static" / "index.html"
TEST = ROOT / "（测试）" / "tests_measure_gate.py"
BAK = ROOT / "_tmp_mut_zoom_bak.html"

results = []


def run_test():
    r = subprocess.run([sys.executable, str(TEST)], capture_output=True,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = (r.stdout or b"").decode("utf-8", "replace")
    return r.returncode != 0 or "FAILED" in out or "OK" not in out


def mutate(old, new):
    src = HTML.read_text(encoding="utf-8")
    assert old in src, f"变异点没找到：{old!r}"
    HTML.write_text(src.replace(old, new, 1), encoding="utf-8")


def restore():
    shutil.copy2(BAK, HTML)
    assert hashlib.sha256(BAK.read_bytes()).hexdigest() == \
        hashlib.sha256(HTML.read_bytes()).hexdigest(), "还原后哈希不一致"


shutil.copy2(HTML, BAK)

# ---- M1：拆掉首屏兜底（这正是今天实测到的那个 bug）----
mutate("const lgSend = legendNotReady ? gpDefaultTypes() : lgOn;",
       "const lgSend = lgOn;   # 变异 M1：拆掉首屏兜底")
r1 = run_test()
restore()
results.append(("M1 首屏图例未渲染不兜底（退化成旧口径）", r1))

# ---- M2：fit 挪到物理收敛之前 ----
mutate("  GP_NET.once('stabilizationIterationsDone', _fitOnce);",
       "  GP_NET.fit({animation:false});   # 变异 M2：不等物理收敛就 fit")
r2 = run_test()
restore()
results.append(("M2 不等物理收敛就 fit", r2))

# ---- M3：变量名撞车 ----
mutate("const lgSend = legendNotReady ? gpDefaultTypes() : lgOn;",
       "const types = legendNotReady ? gpDefaultTypes() : lgOn;   # 变异 M3：撞名")
r3 = run_test()
restore()
results.append(("M3 实体类型变量撞名 types", r3))

BAK.unlink(missing_ok=True)

print("=" * 62)
for name, red in results:
    print(f"  {'✓ 会红' if red else '✗ 假绿！！'}  {name}")
if all(red for _, red in results):
    print("反向验证通过：所有变异都被测试抓住")
else:
    print("✗✗ 有变异没被抓住 —— 测试在假绿，必须修正测试本身")
    sys.exit(1)
