# -*- coding: utf-8 -*-
"""分发版验收闸：把最新那个 zip 解压到临时目录，**独立跑起来**验它是完整可用的。

为什么要有这个闸（而不是"打包脚本自己报 PASS 就算完"）
  ================================================================
  _repack.py 的三道闸只能证明「我按计划拷了文件、排除了密钥」。
  它证明不了这两件事：
    ① zip 本身完整（历史上真踩过：漏文件、把上一轮 __pycache__ 打进去）
    ② 包内的库/说明/服务**互相自洽** —— 组员拿到后双击就能跑，
       而且 README 里念出的每个数字都跟他自己打开网页看到的对上
  所以这里测的是**最终那个 .zip**，不是散装目录：组员收到的是 zip。

跑法：py -3 （测试）\_accept_dist_pack.py [--keep]
  --keep  跑完不删临时解压目录（排查时用）

⚠ 端口固定 8791，刻意与开发用的 8770 分开 —— 这个闸会真的起一个服务，
  绝不能让它去读写开发库 data/cacx.db。
"""
import csv
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile

sys.stdout.reconfigure(encoding="utf-8")
# ⚠ 本文件在 （测试）/ 里，所以是 parent.parent。
#   写成 parent 会指到 （测试）/ 上去，找不到 网站程序/ ——
#   这个坑一天内踩过两次，别再踩第三次。
ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT.parent / "网站程序"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
KEEP = "--keep" in sys.argv

ok = bad = 0


def chk(label, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {label}" + (f"   {detail}" if detail else ""))
    else:
        bad += 1
        print(f"  FAIL  {label}" + (f"   {detail}" if detail else ""))


def to_recycle(p: pathlib.Path) -> bool:
    """送回收站并确认它真消失了 —— 不许 rmtree 永久删。"""
    if not p.exists():
        return True
    verb = "DeleteDirectory" if p.is_dir() else "DeleteFile"
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
          f"[Microsoft.VisualBasic.FileIO.FileSystem]::{verb}("
          f"'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    for _ in range(120):
        if not p.exists():
            return True
        time.sleep(0.5)
    return False


def newest_zip() -> pathlib.Path:
    """按名字里的日期取最新那个分发包，别写死 20261013。"""
    cands = sorted(DEST.glob("cacx-分发版-*.zip"),
                   key=lambda p: re.findall(r"\d+", p.stem)[-1] if re.findall(r"\d+", p.stem) else "")
    return cands[-1] if cands else None


ZP = newest_zip()
if not ZP:
    print(f"🔴 {DEST} 下找不到 cacx-分发版-*.zip")
    sys.exit(2)
NAME = ZP.stem
TMP = DEST / "_unpack_test"
stage = TMP / NAME

print("=" * 70)
print(f"分发版验收   {NAME}.zip   {ZP.stat().st_size/1e6:.1f} MB")
print("=" * 70)

# ---------------------------------------------------------------- 1 解压
if TMP.exists():
    if not to_recycle(TMP):
        print(f"🔴 中止：上次留下的临时解压目录没能送进回收站：{TMP}")
        sys.exit(3)
TMP.mkdir(parents=True)
with zipfile.ZipFile(ZP) as z:
    names = [n.replace("\\", "/") for n in z.namelist()]
    z.extractall(TMP)
print(f"  解压 {len(names)} 个条目")


def in_zip(suffix):
    return any(n.endswith(suffix) for n in names)


for need in ("cacx/web/static/index.html", "data/cacx.db", "README.md",
             "start-cacx.bat", "【组员请先看】README.txt", "requirements.txt"):
    chk(f"zip 内含 {need}", in_zip(need))
for must_not in ("config/settings.json", "autodemo.py", "run-demo.bat",
                 "__pycache__/", ".git/config"):
    chk(f"zip 不含 {must_not}", not in_zip(must_not))
# 组员数据包与日志要真在包里（丢了组员就没法选栏目 / 看不到留痕）
chk("zip 内含 组员数据包/ 四个产物",
    all(in_zip(f"组员数据包/{x}") for x in
        ("cacx-作业数据集.db", "articles.csv", "栏目候选清单.csv", "联合发文线索.csv")))
# ⚠ 这条上一版写的是 in_zip("日志/")，而 in_zip 是 endswith ——
#   目录前缀永远匹配不上任何 .md 文件条目，于是闸门**对着有 13 份日志的包报了 FAIL**。
#   报错方向是"检查脚本自己写错"，不是包有问题：这类 FAIL 必须先怀疑判据。
chk("zip 内含 日志/（工程留痕）",
    any("/日志/" in n and n.endswith(".md") for n in names),
    f"{len([n for n in names if '/日志/' in n and n.endswith('.md')])} 份")
chk("zip 内含今天的开发日志",
    any("/日志/2026-10-13_" in n for n in names))

# ---------------------------------------------------------------- 2 .bat 编码
print("\n--- .bat 编码体检（cmd 按 GBK 解码，正文一个非 ASCII 字节就整份错位）---")
for b in sorted(stage.rglob("*.bat")):
    raw = b.read_bytes()
    non = [i for i, x in enumerate(raw) if x > 0x7F]
    firstline = raw[:non[0]].count(b"\n") + 1 if non else "-"
    chk(f"{b.name} 纯 ASCII", not non,
        f"{len(non)} 个非 ASCII 字节，首处 L{firstline}")

# ---------------------------------------------------------------- 3 包内库
print("\n--- 包内数据库 ---")
db = stage / "data" / "cacx.db"
chk("db 已解压", db.exists())
c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
for label, sql, want in [
    ("articles 总数", "SELECT COUNT(*) FROM articles", 28900),
    ("2024+", "SELECT COUNT(*) FROM articles WHERE year>=2024", 4416),
    ("relations", "SELECT COUNT(*) FROM relations", 8419),
    ("去重实体名", "SELECT COUNT(DISTINCT name) FROM entities", 16561),
    ("summarize 摘要", "SELECT COUNT(DISTINCT article_id) FROM ai_annotations "
                      "WHERE task='summarize'", 4411),
]:
    got = c.execute(sql).fetchone()[0]
    chk(f"{label} = {want}", got == want, f"实际 {got}")
chk("无跨站重复 URL", c.execute(
    "SELECT COUNT(*) FROM (SELECT url FROM articles GROUP BY url "
    "HAVING COUNT(*)>1)").fetchone()[0] == 0)
# ⚠ 口径必须与清理脚本一致（2024+ 且 同站+同标题+同 pub_day）。
#   上一版这里写成「全库按 site+body_hash」，报出 469 组像清理漏了一大片，
#   实际全在 2014-2022 —— **检查脚本口径不一致，它自己就是假警报源**。
chk("2024+ 里「同站+同标题+同日+同正文」的组已清完（应为 0）", c.execute(
    "SELECT COUNT(*) FROM (SELECT site, title, pub_day FROM articles "
    "WHERE pub_day>='2024-01-01' AND title IS NOT NULL AND title<>'' "
    "GROUP BY site, title, pub_day HAVING COUNT(*)>1 "
    "AND COUNT(DISTINCT body_hash)=1)").fetchone()[0] == 0)
chk("包内 db integrity_check ok",
    c.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
c.close()

# ---------------------------------------------------------------- 4 数据包
print("\n--- 组员数据包 ---")
pk = stage / "组员数据包"
d = (pk / "字段说明.md").read_text(encoding="utf-8") if (pk / "字段说明.md").exists() else ""
chk("字段说明用新口径 4416", "4416" in d)
for stale in ("4519 篇", "28999"):
    chk(f"字段说明无过期 {stale}", stale not in d)
cl = list(csv.DictReader((pk / "栏目候选清单.csv")
                         .read_text(encoding="utf-8-sig").splitlines()))
chk("栏目清单 25 行且篇数合计 = 4416",
    len(cl) == 25 and sum(int(r["2024+篇数"]) for r in cl) == 4416,
    f"{len(cl)} 行 / {sum(int(r['2024+篇数']) for r in cl)}")
# 窄口径重算：这一条守住「两级口径」，防止有人手滑统一成宽口径
NARROW = re.compile(r"(联合|印发|会同)")
c2 = sqlite3.connect(f"file:{(pk/'cacx-作业数据集.db').as_posix()}?mode=ro&immutable=1",
                     uri=True)
mis = 0
for r in cl:
    its = c2.execute("SELECT title, pub_day FROM articles WHERE site=? AND column_name=?",
                     (r["站点"], r["栏目"])).fetchall()
    if sum(1 for x in its if NARROW.search(x[0] or "")) != int(r["联合发文类(2024+)"]):
        mis += 1
c2.close()
chk("候选清单联合列 == 窄口径(联合|印发|会同)重算", mis == 0, f"{mis} 行不符")

# ---------------------------------------------------------------- 5 真跑
print("\n--- 启动包内服务（端口 8791，不碰开发用的 8770）---")
# ⚠ 必须基于 os.environ 复制：只传 PYTHONIOENCODING 会把 PATH 抹掉，
#   子进程起不来的症状会被误读成"分发包坏了"
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "cacx", "serve",
                         "--host", "127.0.0.1", "--port", str(PORT), "--no-browser"],
                        cwd=str(stage), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True,
                        encoding="utf-8", errors="replace", env=env)
health = None
out = ""
for _ in range(40):
    time.sleep(1.5)
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
        break
    except Exception:
        if proc.poll() is not None:          # 子进程秒退就别空等 60 秒
            break
chk("服务起来了", health is not None,
    f"rc={proc.returncode}" if proc.poll() is not None else "")
if health:
    chk("health.ok 且 articles=28900",
        health.get("ok") is True and health.get("articles") == 28900, str(health)[:120])
    with urllib.request.urlopen(f"{BASE}/api/graph/analysis?min_degree=2",
                                timeout=60) as r:
        an = json.loads(r.read().decode("utf-8"))
    chk("图谱口径 1633/3921/115",
        (an["nodes"], an["edges"], an["components"]) == (1633, 3921, 115),
        f"{an['nodes']}/{an['edges']}/{an['components']}")
    # ⚠ 1410 不是「某个时刻碰巧画出来的数」，是**修好截断确定性之后**的稳定值。
    #   原先这里写的是 1407 —— 那是 core.sort 只按度数排序时的产物，
    #   度数并列节点谁入选取决于 SQL 行序，同一张图能画出 1407/1416 等多个值。
    #   真实库实测（min_degree=1/limit=300、含低置信屏蔽、8419 条关系）：
    #   原序/倒序/4 个随机种子共 6 种行序，现在恒为 1410。
    #   改动这个数之前，先跑那条稳定性验证，别直接抄服务当前返回值 ——
    #   那正是当年把 1407 写进来的方式。
    g = an.get("graph") or {}
    chk("画布默认 300/1410",
        (len(g.get("nodes") or []), len(g.get("edges") or [])) == (300, 1410),
        f"{len(g.get('nodes') or [])}/{len(g.get('edges') or [])}")
    # README.txt 承诺的数必须与**服务真值**一致，这是"组员照着念不会被打脸"的唯一保证
    rt = (stage / "【组员请先看】README.txt").read_text(encoding="utf-8")
    for want in ("4416", "4415", "8419", "16561", "1633", "3921",
                 "300", "1410", "244", "1015", "0.194", "0.1173", "26 个工具"):
        chk(f"README.txt 含 {want}", want in rt)
    for stale in ("4519", "4517", "8801", "28999"):
        chk(f"README.txt 无过期 {stale}", stale not in rt)
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    chk("首页可取且是 cacx 页面", "cacx" in html and len(html) > 5000, f"{len(html)} 字")

proc.terminate()
try:
    proc.wait(timeout=20)
except Exception:
    proc.kill()

if not KEEP:
    if not to_recycle(TMP):
        print(f"\n⚠ 临时解压目录没能送进回收站，请手动处理：{TMP}")
else:
    print(f"\n--keep：解压目录留在 {TMP}")

print("\n" + "=" * 70)
print(f"通过 {ok} 项，失败 {bad} 项")
sys.exit(1 if bad else 0)
