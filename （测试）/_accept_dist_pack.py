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

跑法：py -3 （测试）\_accept_dist_pack.py [--keep] [--full-graph]
  --keep        跑完不删临时解压目录（排查时用）
  --full-graph  额外跑「README 数字 == 包内服务真值」慢检（十分钟量级，
                因为要在冷服务上算全图介数；默认不开）

⚠ 端口固定 8791，刻意与开发用的 8770 分开 —— 这个闸会真的起一个服务，
  绝不能让它去读写开发库 data/cacx.db。
"""
import csv
import json
import os
import pathlib
import re
import shutil
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
# --full-graph：额外跑「README 里的数 == 包内服务真值」的慢检。
#   默认不开，因为它要在**刚起的冷服务**上算全图介数（12162 节点，O(V·E)），
#   实测十分钟量级；日常验收没必要每次都付这个代价。
FULL_GRAPH = "--full-graph" in sys.argv

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
# ⚠⚠ 这些数**一律不写死**。原来这里写的是 28900/4416/8419/16561 ——
#   那是几轮抓取之前的旧值。写死的常量有两个坏处：口径一变它就静默过期，
#   验收恒红；更糟的是有人会去改常量让它变绿，于是数字对不上也没人知道。
#   现在改成「包里各处自述的数字 == 库里真值」，永远追得上。
CNT = {}
for label, sql in [
    ("articles 总数", "SELECT COUNT(*) FROM articles"),
    ("2024+", "SELECT COUNT(*) FROM articles WHERE year>=2024"),
    ("有正文", "SELECT COUNT(*) FROM articles WHERE content_text IS NOT NULL AND content_text<>''"),
    ("relations", "SELECT COUNT(*) FROM relations"),
    ("去重实体名", "SELECT COUNT(DISTINCT name) FROM entities"),
    ("summarize 摘要", "SELECT COUNT(DISTINCT article_id) FROM ai_annotations "
                      "WHERE task='summarize'"),
]:
    CNT[label] = c.execute(sql).fetchone()[0]
    chk(f"{label} > 0", CNT[label] > 0, f"实际 {CNT[label]}")
n24 = CNT["2024+"]
chk("无跨站重复 URL", c.execute(
    "SELECT COUNT(*) FROM (SELECT url FROM articles GROUP BY url "
    "HAVING COUNT(*)>1)").fetchone()[0] == 0)
# ⚠ 口径必须与清理脚本一致（2024+ 且 同站+同标题+同 pub_day）。
#   上一版这里写成「全库按 site+body_hash」，报出 469 组像清理漏了一大片，
#   实际全在 2014-2022 —— **检查脚本口径不一致，它自己就是假警报源**。
_dup = c.execute(
    "SELECT COUNT(*) FROM (SELECT site, title, pub_day FROM articles "
    "WHERE pub_day>='2024-01-01' AND title IS NOT NULL AND title<>'' "
    "GROUP BY site, title, pub_day HAVING COUNT(*)>1 "
    "AND COUNT(DISTINCT body_hash)=1)").fetchone()[0]
chk("2024+ 里「同站+同标题+同日+同正文」的组已清完（应为 0）", _dup == 0,
    f"实际 {_dup} 组 —— 真有重复数据，不是检查口径写错")
if _dup:
    print("    … 重复样例：")
    for _r in c.execute(
            "SELECT site, pub_day, COUNT(*), substr(title,1,36) FROM articles "
            "WHERE pub_day>='2024-01-01' AND title IS NOT NULL AND title<>'' "
            "GROUP BY site,title,pub_day HAVING COUNT(*)>1 "
            "AND COUNT(DISTINCT body_hash)=1 LIMIT 8"):
        print(f"      {_r}")
chk("包内 db integrity_check ok",
    c.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
c.close()

# ---------------------------------------------------------------- 4 数据包
print("\n--- 组员数据包 ---")
pk = stage / "组员数据包"
d = (pk / "字段说明.md").read_text(encoding="utf-8") if (pk / "字段说明.md").exists() else ""
# ⚠ 这里也**不写死** 4416：改成「字段说明自述的数 == 库真值」。
chk("字段说明.md 的 2024+ 数 == 库真值", str(n24) in d, f"库里 {n24}")
for stale in ("4519 篇", "28999", "4416"):
    # ⚠ 「口径变更记录」那一节**正当**写着旧值（"4416 → 4433"），
    #   那是这段历史的一部分，不是没更新。所以哨兵只查记录**之前**的正文。
    body = d.split("> **口径变更记录**")[0]
    chk(f"字段说明正文无过期 {stale}", stale not in body)
cl = list(csv.DictReader((pk / "栏目候选清单.csv")
                         .read_text(encoding="utf-8-sig").splitlines()))
chk("栏目清单 25 行且篇数合计 == 库里的 2024+",
    len(cl) == 25 and sum(int(r["2024+篇数"]) for r in cl) == n24,
    f"{len(cl)} 行 / 合计 {sum(int(r['2024+篇数']) for r in cl)} / 库 {n24}")
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
# ⚠⚠ 解释器必须选**包内那个**，不是 sys.executable。
#   上一版写死 sys.executable，于是验收跑的是开发机的 Python ——
#   而开发机碰巧装了 scipy，networkx 的 pagerank 能跑；
#   包里那份 requirements.txt 少了 scipy 这件事就永远测不出来，
#   48 项全绿、图谱面板在组员机器上却是坏的。
#   「在自己机器上绿」不等于「在收件人机器上绿」——
#   尤其这台机器的环境比交付目标环境**更完整**。
_pack_py = stage / "runtime" / "python.exe"
if _pack_py.exists():
    PY_EXE = str(_pack_py)
    print("用包内便携运行时: runtime\\python.exe")
else:
    PY_EXE = sys.executable
    print("⚠ 包内没有 runtime\\python.exe，退回开发机解释器 —— "
          "这次验收**不代表组员机器**的真实环境")
chk("包内自带便携运行时", _pack_py.exists(), f"{(stage / 'runtime').exists()}")

# ⚠ 小图冒烟：必须用**包内解释器 + 包内 cacx** 跑。
#   networkx>=3.6 的 pagerank() 无条件要 scipy，而 networkx 不声明它；
#   `import cacx` / `import networkx` 永远成功，只有真调 analyze() 才炸。
#   真跑全图要 278 秒，这张小图毫秒级。
#   这一道已经真抓出过两个 bug：scipy 漏声明，以及打包排除规则
#   把 numpy/_expired_attrs_2_0.py 当"本地脚本"排掉（包内服务直接起不来）。
#   不拷脚本进解压目录 —— _smoke_analyze.py 自己认 CACX_SMOKE_ROOT。
_s = subprocess.run([PY_EXE, str(pathlib.Path(__file__).resolve().parent
                                 / "_smoke_analyze.py")],
                    cwd=str(stage),
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    env={**os.environ, "CACX_SMOKE_ROOT": str(stage),
                         "PYTHONIOENCODING": "utf-8",
                         # 同样别让它在解压目录里撒 .pyc / __pycache__：
                         # 后面还有「全包无运行产物」那一类检查。
                         "PYTHONDONTWRITEBYTECODE": "1"})
_sout = (_s.stdout or "") + (_s.stderr or "")
_sline = [x for x in _sout.strip().splitlines() if "SMOKE-" in x]
chk("包内解释器小图 analyze() 出数（scipy 到位）",
    "SMOKE-OK" in (_s.stdout or ""), _sline[-1] if _sline else "无 SMOKE 行")
if "SMOKE-OK" not in (_s.stdout or ""):
    for x in _sout.strip().splitlines()[-8:]:
        print("        " + x)

# ⚠ 必须基于 os.environ 复制：只传 PYTHONIOENCODING 会把 PATH 抹掉，
#   子进程起不来的症状会被误读成"分发包坏了"
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
proc = subprocess.Popen([PY_EXE, "-X", "utf8", "-m", "cacx", "serve",
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
# ⚠⚠ 子服务必须**无条件**收掉，不能只在末尾正常路径 terminate。
#   踩过：脚本中途抛异常（冷启动图谱超时）直接退出，把 8791 的服务留在
#   解压目录 _unpack_test 里不放 —— 于是下次开跑第一步「送回收站」就失败，
#   报「上次留下的临时解压目录没能送进回收站」，而真凶是上上次的崩溃。
#   孤儿进程还会一直占着 380 MB 的库不释放，症状与根因隔得很远。
#   用 atexit 而不是 try/finally：解释器退出时（正常结束、未捕获异常）
#   都会跑，且不必把后面 50 行整体重新缩进。
import atexit as _atexit


def _kill_server():
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


_atexit.register(_kill_server)
if health:
    # ⚠⚠ 这里**不再写死任何数字**，全部改成「包内自洽」断言。
    #   原来写死的是 articles=28900、图谱 1759/4154/126、画布 300/1449 ——
    #   那是「措施类默认勾选放行」**之前**的口径。口径一变（2026-10-14 起
    #   子图 1633→12162、边 3921→29069、画布 1449→2995），这些常量就全废了，
    #   而写死的常量**不会自己报错**，只会让验收恒红或被绕过。
    #   真正的保证是：README 承诺的数 == 这个包自己起服务后报出的数。
    #   这条永远不会过期，也正是「组员照着念不会被打脸」的唯一依据。
    _pack_db = stage / "data" / "cacx.db"
    _con = sqlite3.connect(f"file:{_pack_db.as_posix()}?mode=ro", uri=True)
    try:
        _n_art = _con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        _con.close()
    chk(f"health.ok 且 articles={_n_art}（与包内库一致）",
        health.get("ok") is True and health.get("articles") == _n_art,
        f"health={health.get('articles')} 包内库={_n_art}")

    # ⚠ 必须带 node_types（全勾），与前端 gpDefaultTypes() 同口径。
    #   不带就是「不放行」，拿到组员在页面上看不到的另一套数。
    #
    #   ⚠⚠ 这一步默认**跳过**，要 --full-graph 才跑。
    #   原因不是图谱算不动，是它太贵：min_degree=2 下有 12162 个节点，
    #   networkx 的介数中心性是 O(V·E)≈3.5 亿次运算，纯 Python 冷启动实测
    #   8 分钟以上还没算完。而 README 承诺的「12162/29069/886」和画布
    #   「300/2995」**只有这个接口给得出来**（/api/graph 只回 300 条边）。
    #   把它塞进日常验收，结果就是每次验收都要等十分钟，且大概率中途超时。
    #   所以：默认跑快检，慢检显式开。
    rt = (stage / "【组员请先看】README.txt").read_text(encoding="utf-8")

    # 静态口径检查（不需要服务，任何时候都能跑）：
    # 上一版口径的数字一个都不许出现在 README 里。
    # 这条是有来由的 —— 打包脚本曾在服务超时时**静默退回**写死的旧数
    # （1759/4154/126/1449），而当时 31 项验收全过、没有任何报错。
    # 数字能悄悄过期，就得有个不依赖服务的哨兵盯着。
    for stale in ("1759", "4154", "1449", "28900", "1633", "3921"):
        chk(f"README.txt 无旧口径 {stale}", stale not in rt)

    # ⚠ 数字哨兵盯不住**说法**的过期。加派这一条，是因为本包的启动逻辑
    #   改过一次优先级：现在是「本机 Python 3.10+ 且有网就用本机并装依赖，
    #   否则用自带 runtime」。README 一旦还写着「需要 Python 3.10+，
    #   安装时务必勾选 Add Python to PATH」，就等于在教组员做一件
    #   **根本不需要做的事**，而且会让人以为不装 Python 就跑不起来 ——
    #   这正是当初随包携带运行时要消灭的那个印象。
    #   说法型哨兵只能用子串匹配，因为数字哨兵对它无能为力。
    for stale_claim in ("需要 Python 3.10+",
                        'Add Python to PATH"'):
        chk(f"README.txt 无过期说法「{stale_claim}」", stale_claim not in rt)
    chk("README.txt 说清了「不用装也能跑」",
        "什么都不用装" in rt and "runtime" in rt)

    if FULL_GRAPH:
        def _get_graph():
            last = None
            for i, wait in enumerate((0, 15, 30)):
                if wait:
                    time.sleep(wait)
                try:
                    with urllib.request.urlopen(
                            f"{BASE}/api/graph/analysis?min_degree=2"
                            f"&node_types=org,policy,problem,measure",
                            timeout=1800) as r:
                        return json.loads(r.read().decode("utf-8"))
                except Exception as exc:
                    last = exc
                    print(f"    … 图谱取数第 {i + 1} 次失败（{type(exc).__name__}）")
            raise last

        an = _get_graph()
        gn, ge, gc = an["nodes"], an["edges"], an["components"]
        g = an.get("graph") or {}
        cn, ce = len(g.get("nodes") or []), len(g.get("edges") or [])
        # README.txt 承诺的数必须与**服务真值**一致 —— 「组员照着念不会被
        # 打脸」的唯一保证。这里断言的是 README↔服务，不写死任何常量，
        # 所以口径再变也不会过期。
        for label, val in (("分析口径节点", gn), ("分析口径边", ge),
                           ("连通块", gc), ("画布节点", cn), ("画布边", ce)):
            chk(f"README.txt 里的{label} {val} == 服务真值", str(val) in rt)
    else:
        print("  （--full-graph 未开：跳过 README↔全量图谱一致性慢检，"
              "约需 10 分钟；静态旧口径哨兵已覆盖）")

    for stale in ("4519", "4517", "8801", "28999"):
        chk(f"README.txt 无过期 {stale}", stale not in rt)
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    chk("首页可取且是 cacx 页面", "cacx" in html and len(html) > 5000, f"{len(html)} 字")

_kill_server()          # 正常路径提前收掉；异常路径由上面的 atexit 兜

if not KEEP:
    if not to_recycle(TMP):
        print(f"\n⚠ 临时解压目录没能送进回收站，请手动处理：{TMP}")
else:
    print(f"\n--keep：解压目录留在 {TMP}")

print("\n" + "=" * 70)
print(f"通过 {ok} 项，失败 {bad} 项")
sys.exit(1 if bad else 0)
