# -*- coding: utf-8 -*-
"""T-temppool 反向验证：证明临时目录池的三条性质**不是**嘴上说说。

新增闸门必须先证明它会红，否则「0 残留」和「检测器压根没生效」
在输出里长得一模一样。这里逐条做正反两向对照。

四条性质：
  ① cleanup() 在句柄被占时必须返回 False（不能谎报成功）
  ② 句柄释放后 cleanup() 必须真的把目录删掉
  ③ sweep() 必须能清掉**没登记过**的遗留（atexit 兜不住的那种）
  ④ mkdtemp 必须落在项目内临时根、且保留前缀
     （部分用例断言 assertIn("cacx_sample_", db.path) 靠的就是前缀）

只动（测试）/.tmp 下的临时目录，不碰 config/settings.json 与 data/cacx.db。
"""
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _temppool  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(name)


_temppool.sweep()          # 清掉上一轮遗留，从干净状态开始

print("[1] 落点：必须在项目内，不许写 %TEMP%")
d = Path(_temppool.mkdtemp(prefix="cacx_mut_"))
check("落在 _temppool.ROOT 下", str(d).startswith(str(_temppool.ROOT)), str(d))
check("不在系统 %TEMP% 下", not str(d).startswith(str(Path(os.environ["TEMP"])).lower()))
check("保留 cacx_ 前缀（用例断言依赖）", d.name.startswith("cacx_mut_"), d.name)

print("\n[2] 反向：文件句柄被占时，cleanup() 必须返回 False")
(d / "t.db").write_bytes(b"x" * 4096)
handle = open(d / "t.db", "r+b")       # Windows 上打开即锁，删不掉
r = _temppool.cleanup(d)
check("返回 False（没谎报成功）", r is False, f"got {r!r}")
check("目录确实还在", d.exists(), "被删掉了说明这条检测无效")
handle.close()

print("\n[3] 正向：句柄释放后，cleanup() 必须真删")
r = _temppool.cleanup(d)
check("返回 True", r is True, f"got {r!r}")
check("目录已从磁盘消失", not d.exists())

print("\n[4] 反向：只读文件不能把 cleanup 卡死（onerror 补救）")
d2 = Path(_temppool.mkdtemp(prefix="cacx_mut_ro_"))
ro = d2 / "ro.txt"
ro.write_text("readonly", encoding="utf-8")
os.chmod(ro, 0o444)                    # 模拟 git 检出的只读文件
r = _temppool.cleanup(d2)
check("只读文件也删得掉", r is True and not d2.exists(), f"got {r!r}")

print("\n[5] sweep() 必须清掉没登记过的遗留")
stray = _temppool.ROOT / "cacx_mut_stray_notregistered"
stray.mkdir(parents=True, exist_ok=True)
(stray / "t.db").write_bytes(b"y" * 1024)
check("sweep 前它确实在", stray.exists())
n = _temppool.sweep()
check("sweep 报出了清理条数", n >= 1, f"n={n}")
check("sweep 后它没了", not stray.exists())

print("\n[6] 反向：故意留一个目录，两道检测各自都要看得见自己该管的")
d3 = _temppool.mkdtemp(prefix="cacx_mut_reg_")   # 登记过，故意不删
d4 = Path(_temppool.mkdtemp(prefix="cacx_mut_reg2_"))
stray2 = _temppool.ROOT / "cacx_mut_stray_unregistered"
stray2.mkdir(parents=True, exist_ok=True)
found = _temppool.leaks()
# leaks() 的职责边界：只管**登记过**的；没登记的归 sweep() 管。
# 反过来断言「leaks() 也该看到未登记的」是错的 —— 那是越界。
check("leaks() 抓到登记却没删的 d3", str(d3) in found, f"leaks={found}")
check("leaks() 抓到登记却没删的 d4", str(d4) in found)
check("leaks() 不越界管未登记的 stray2", str(stray2) not in found)
n = _temppool.sweep()
check("sweep 一次清掉三项（d3/d4/stray2）", n >= 3, f"n={n}")
check("sweep 后磁盘上全没了",
      not os.path.exists(d3) and not os.path.exists(d4)
      and not stray2.exists())
check("sweep 后 leaks() 为空", _temppool.leaks() == [], _temppool.leaks())

print("\n" + "=" * 60)
if fails:
    print(f"✗ {len(fails)} 条不成立: {fails}")
    sys.exit(1)
print(">>> _temppool 四条性质全部成立（含反向验证）")
