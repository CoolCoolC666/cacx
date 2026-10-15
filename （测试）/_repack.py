# -*- coding: utf-8 -*-
"""
重建分发包（含新的 README / LICENSE / CONDUCT.md）

为什么不复用上次的脚本：`_pack.py` 已被回收站清理了（它本身也是
`_*` 本地脚本，不入库）。重新写一份，顺手修掉上一版的两处问题：
  ① 排除 `__init__.py`（子目录 __pycache__ 与本地脚本都漏了）
  ② 子目录的 __pycache__ 没被排除
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
SRC = Path(__file__).resolve().parent.parent
DEST = SRC.parent / "网站程序"
NAME = f"cacx-分发版-{datetime.now():%Y%m%d}"

# 排除「任何层级同名」——只写顶层规则会漏掉 cacx/ai/__pycache__
DIR_EXCLUDE = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
               ".git", ".venv", "venv", "node_modules", "data",
               "config/sites.d",
               # ⚠ 「（测试）」必须整目录排除 —— 里面的 tests_*.py
               #   不带下划线，走不到「单下划线排除」那条规则，
               #   于是 34 个测试文件 + 9 张截图会跟着进分发包。
               #   分发包是给组员用的，不该带我们的测试。
               "（测试）"}
FILE_EXCLUDE = {
    "config/settings.json", ".env",
    # 自动演示：**进 git 但不进分发包**（组员演示用的是手动版脚本，
    # 全自动脚本依赖本机 Playwright + chromium，跟着走只会让人装不上）
    "autodemo.py",
    "run-demo.bat",
}
PATTERNS = ("_*.py", "_*.js", "_*.txt", "_*.log", "_*.csv", "_*.jsonl", "*.err")

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def skip(name, rel=""):
    """
    ⚠ 两个参数缺一不可，且必须分开判：
      · `name`  —— 文件/目录的**本名**，用于按名字排除（__pycache__、_foo.py）
      · `rel`   —— 相对项目根的**路径**，用于按路径排除（config/sites.d）
    上一版把 `DIR_EXCLUDE` 里的 `config/sites.d` 当目录名去比 `name`，
    于是永远匹配不上，那个文件**真的被打进了分发包**。
    实测发现：包内出现了 config/sites.d/zjwx-api.json。
    （内容经查证不含密钥，只是选择器与分页参数，但按既定口径也不该带。）
    """
    if name in DIR_EXCLUDE:
        return True
    # 按路径前缀排除 —— 这是 DIR_EXCLUDE 里带斜杠那几项的判据
    if any(rel == d or rel.startswith(d + "/") for d in DIR_EXCLUDE if "/" in d):
        return True
    if rel in FILE_EXCLUDE or name in FILE_EXCLUDE:
        return True
    if name.endswith((".err", ".log", ".pyc")):
        return True
    # 单下划线 = 本地自用脚本；**双下划线是 Python 语法，必须保留**
    # （上次就因为没区分，把 5 个 __init__.py 全排掉了）
    if name.startswith("_") and not name.startswith("__"):
        return any(Path(name).match(p) for p in PATTERNS)
    return False


stage = DEST / NAME


def to_recycle(p: Path):
    """
    把目录送回收站，并**确认它真的消失了**。

    ⚠ 为什么不能直接调了就算：Shell.Application 的 InvokeVerb('delete')
    是**异步**的，命令立即返回、文件还在（535 MB 的目录尤其慢）。
    之前就是这么把上一轮残留的 __pycache__ 打进新包里的 ——
    脚本以为清干净了，其实往旧目录上继续拷。
    所以这里改用同步的 FileSystem.DeleteDirectory，并轮询确认。

    ⚠ DeleteFile 只能删文件，目录必须用 DeleteDirectory。
    """
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
          "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
          f"'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    import time as _t
    for _ in range(60):               # 最多等 30 秒
        if not p.exists():
            return True
        _t.sleep(0.5)
    return False


if stage.exists():
    if not to_recycle(stage):
        print(f"\n🔴 中止：旧的同名目录没能送进回收站（可能正被占用）。")
        print(f"   {stage}")
        print("   请关掉资源管理器里打开的窗口后重跑 —— ")
        print("   继续下去会把上一次的残留文件打进新包。")
        sys.exit(3)
    print(f"  旧的同名目录已送回收站：{stage.name}")
(stage / "data").mkdir(parents=True, exist_ok=True)


def to_recycle_file(p: Path):
    """旧 zip 也必须进回收站 —— 组员的删除口径是「一切可恢复」。"""
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
          "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
          f"'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    import time as _t
    for _ in range(40):
        if not p.exists():
            return True
        _t.sleep(0.5)
    return False

print("=" * 66)
print("1  数据库一致性快照（SQLite backup API，不做文件拷贝）")
print("=" * 66)
db_src = SRC / "data" / "cacx.db"
db_dst = stage / "data" / "cacx.db"
a = sqlite3.connect(f"file:{db_src.as_posix()}?mode=ro", uri=True)
b = sqlite3.connect(str(db_dst))
a.backup(b)          # WAL 里未 checkpoint 的数据也一并带走
b.close()
a.close()
check("快照已生成", db_dst.exists(), f"{db_dst.stat().st_size/1e6:.1f} MB")


def counts(p):
    c = sqlite3.connect(f"file:{Path(p).as_posix()}?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("articles", "ai_annotations", "ai_failures")}
    finally:
        c.close()


cs, cd = counts(db_src), counts(db_dst)
print(f"  源库 {cs}")
print(f"  快照 {cd}")
check("行数一致", cs == cd)
c = sqlite3.connect(str(db_dst))
ic = c.execute("PRAGMA integrity_check").fetchone()[0]
c.close()
check("快照通过 integrity_check", ic == "ok", ic)

print()
print("=" * 66)
print("2  复制源码")
print("=" * 66)
n = 0
for root, dirs, files in os.walk(SRC):
    for dn in list(dirs):
        if dn in DIR_EXCLUDE:
            dirs.remove(dn)
    for fn in files:
        rel = (Path(root) / fn).relative_to(SRC)
        if skip(fn) or skip(fn, rel.as_posix()):
            continue
        d = stage / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / fn, d)
        n += 1
print(f"  复制 {n} 个文件")

print()
print("=" * 66)
print("3  三道闸")
print("=" * 66)
inits = list(stage.rglob("__init__.py"))
check(f"__init__.py 齐全（{len(inits)} 个）", len(inits) == 5)
check("无 __pycache__", not list(stage.rglob("__pycache__")))
check("无 settings.json", not (stage / "config" / "settings.json").exists())
check("有 settings.example.json",
      (stage / "config" / "settings.example.json").exists())
for must in ("README.md", "LICENSE", "CONDUCT.md", "cacx/__main__.py",
             "cacx/web/static/index.html", "requirements.txt",
             "start-cacx.bat"):
    check(f"含 {must}", (stage / must).exists())
check("LICENSE 是纯 MIT",
      (stage / "LICENSE").read_text(encoding="utf-8")
      .rstrip().endswith("DEALINGS IN THE\nSOFTWARE."))
check("CONDUCT.md 有免责声明",
      "不随代码开源" in (stage / "CONDUCT.md").read_text(encoding="utf-8"))

# 残留守卫：上一轮就在分发目录里跑过服务验证，会生成 __pycache__，
# 而那个目录名是按日期的，重跑打包正好撞上 —— 于是旧残留被拷进新包。
# 所以这里不只查顶层，任何层级的运行产物都必须为零。
junk_py = [f for f in stage.rglob("*.pyc")]
junk_dir = [f for f in stage.rglob("__pycache__")]
junk_db = [f for f in stage.rglob("*.db-*")]
check("全包无 .pyc 运行产物", not junk_py, f"{len(junk_py)} 个：{junk_py[:3]}")
check("全包无 __pycache__ 目录", not junk_dir,
      f"{len(junk_dir)} 个：{[str(p.relative_to(stage)) for p in junk_dir[:3]]}")
check("全包无 SQLite 边车文件", not junk_db,
      f"{len(junk_db)} 个：{[p.name for p in junk_db[:3]]}")

# 排除项**逐项点名**核对 —— 上一版就是靠抽查漏掉了 sites.d。
# 断言写在包里「不该有的东西」清单上，而不是靠肉眼扫一遍排除表。
for bad_path, why in [
    ("config/settings.json", "含打包方密钥"),
    ("config/sites.d/zjwx-api.json", "站点配置，含选择器与分页参数"),
    (".git/config", "版本库"),
    ("data/cacx.db-wal", "WAL 由快照生成，不拷"),
    ("cacx/__init__.py", "❗这个必须有，排除规则误伤的典型"),
    # 自动演示：进了 git 就不该再进分发包（依赖本机 Playwright）
    ("autodemo.py", "全自动演示，进 git 不进分发包"),
    ("run-demo.bat", "全自动演示一键入口，同上"),
    ("（测试）/_rev_autodemo.py", "反向验证脚本，随（测试）整目录排除"),
    ("（测试）/演示产物", "截图与录像，派生物可重跑生成"),
]:
    exists = (stage / bad_path).exists()
    must_exist = bad_path.endswith("__init__.py")
    if must_exist:
        check(f"必须存在：{bad_path}（{why}）", exists)
    else:
        check(f"已排除：{bad_path}（{why}）", not exists)

# 密钥：唯一判据是「与真实 key 逐字节相同」
import json
import re
MY_KEY = json.loads((SRC / "config" / "settings.json")
                     .read_text(encoding="utf-8"))["ai"].get("api_key") or ""
RX = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
leak = []
for f in sorted(stage.rglob("*")):
    if not f.is_file() or f.suffix.lower() in (".db", ".png", ".zip"):
        continue
    try:
        t = f.read_text(encoding="utf-8")
    except Exception:
        continue
    if MY_KEY and MY_KEY in t:
        leak.append(f.relative_to(stage).as_posix())
check("全包无我的真实 key（逐字节比对）", not leak, str(leak))

print()
print("=" * 66)
print("4  组员说明")
print("=" * 66)
# ---- 说明里的每个数字都从**打包用的那份快照**现算，不再硬编码 ----
# 上一版就是硬编码写死的：清理「一文多栏目发」之后语料从 4519 变成 4416，
# 但 _repack.py 里的字还是 4519 —— 直接重打包就会把过期数字发给组员，
# 而且**没有任何检查会报错**（它只是段字符串）。
pd = sqlite3.connect(f"file:{db_dst.as_posix()}?mode=ro", uri=True)
P = {}
P["n24"] = pd.execute("SELECT COUNT(*) FROM articles WHERE year>=2024").fetchone()[0]
P["body"] = pd.execute(
    "SELECT COUNT(*) FROM articles WHERE year>=2024 "
    "AND content_text IS NOT NULL AND LENGTH(content_text)>0").fetchone()[0]
P["cols"] = pd.execute(
    "SELECT COUNT(DISTINCT site || '|' || column_code) FROM articles "
    "WHERE year>=2024").fetchone()[0]
P["ents"] = pd.execute("SELECT COUNT(DISTINCT name) FROM entities").fetchone()[0] \
    if pd.execute("SELECT name FROM sqlite_master WHERE type='table' "
                  "AND name='entities'").fetchone() else 0
P["rels"] = pd.execute("SELECT COUNT(*) FROM relations").fetchone()[0] \
    if pd.execute("SELECT name FROM sqlite_master WHERE type='table' "
                  "AND name='relations'").fetchone() else 0
pd.close()
print(f"  说明用数字现算：2024+ {P['n24']} / 有正文 {P['body']} / "
      f"栏目 {P['cols']} / 实体 {P['ents']} / 关系 {P['rels']}")

# 图谱口径要跑分析接口才有，打包时不假设服务在跑：
# 服务活着就现算，没活就退回**从快照直接算**的等价口径。
G = {}
BASE_GRAPH = "http://127.0.0.1:8770/api/graph/analysis"
# ⚠ 必须显式带 node_types，而且必须与前端 `gpDefaultTypes()` 一样是**全勾**。
#   不带的话后端按「不放行」算（measure 被 confidence 门槛挡着），
#   拿到的 1633/3921 是组员在网页上**根本看不到**的旧口径 —— README 会
#   印出跟页面对不上的数，组员照着念当场自相矛盾。
#   这里不写死关系类型名，只写实体类型（measure 是 4 类里唯一被门槛挡的）。
import urllib.parse as _upg
_BASE_T = _upg.urlencode({"min_degree": 2, "node_types":
                          "org,policy,problem,measure"})
try:
    import urllib.request as _u

    def _fetch(q, timeout):
        """带重试地取一次图谱分析。

        为什么重试要给足超时（这里 900s，不是 60s）：
        这个接口在**冷启动的服务**上要现算全图 + 介数/PageRank/社区。
        12162 节点、29069 边，介数是 O(V·E)≈3.5 亿次运算，实测冷服务
        要好几分钟。而客户端超时**不会让服务端停下来** —— 它还在算，
        后面几次重试全排在它后面，于是「重试」只是更快地把四次机会
        全部耗光，最后误判成「服务不可用」。
        实测：服务热的时候同一接口 0.3 秒，冷的时候能超过 300 秒。
        """
        import time as _t
        last = None
        for i, wait in enumerate((0, 5, 10, 20)):
            if wait:
                _t.sleep(wait)
            try:
                with _u.urlopen(BASE_GRAPH + "?" + q, timeout=timeout) as _r:
                    return json.loads(_r.read().decode("utf-8"))
            except Exception as exc:
                last = exc
                print(f"    … 第 {i + 1} 次取图谱失败（{type(exc).__name__}: {exc}），"
                      f"{'重试' if i < 3 else '放弃'}")
        raise last

    # 冷启动的服务可能要算好几分钟，别把「还在算」当成「挂了」。
    _a = _fetch(_BASE_T, 900)
    G["nodes"], G["edges"], G["comp"] = (_a["nodes"], _a["edges"],
                                         _a["components"])
    # 网页画布上那行「N 节点 / M 边」读的是 **an['graph'] 里的数组长度**，
    # 不是顶层计数 —— 顶层是分析口径，画布默认只画权重前 300 个点。
    # 这两个数差很多，组员照着念会当场自相矛盾，所以必须分别现算。
    _g = _a.get("graph") or {}
    G["rnodes"] = len(_g.get("nodes") or [])
    G["redges"] = len(_g.get("edges") or [])
    G["hidden"] = _g.get("hidden_nodes") or 0
    G["snodes"] = _g.get("scope_nodes")
    G["sedges"] = _g.get("scope_edges")
    _b = _a.get("betweenness") or []
    G["b0"] = _b[0]["betweenness"] if _b else None
    G["b1"] = _b[1]["betweenness"] if len(_b) > 1 else None
    G["n0"] = _b[0]["name"] if _b else None
    G["n1"] = _b[1]["name"] if len(_b) > 1 else None
    # 聚焦 1 跳：介数与子图规模都要单独取（报告的核心论点靠这组数）
    import urllib.parse as _up
    _fq = _up.urlencode({"min_degree": 2, "focus": "中央网信办", "hop": 1,
                         "node_types": "org,policy,problem,measure"})
    _f = _fetch(_fq, 90)
    _fb = _f.get("betweenness") or []
    G["fb0"] = _fb[0]["betweenness"] if _fb else None
    G["fb1"] = _fb[1]["betweenness"] if len(_fb) > 1 else None
    G["fn0"] = _fb[0]["name"] if _fb else None
    G["fn1"] = _fb[1]["name"] if len(_fb) > 1 else None
    _fg = _f.get("graph") or {}
    G["foc_nodes"] = _fg.get("scope_nodes")
    G["foc_edges"] = _fg.get("scope_edges")
    print(f"  图谱口径取自运行中的服务：{G['nodes']} 节点 / {G['edges']} 边 / "
          f"{G['comp']} 连通块；画布默认画 {G['rnodes']}/{G['redges']}"
          f"（折叠 {G['hidden']}）；聚焦 1 跳 {G['foc_nodes']}/{G['foc_edges']}")
except Exception as _e:
    # ⚠⚠ 这里**不能**再退回一组写死的数。
    #   实测过的教训：写死的那组 1759/4154/126 是「措施类默认勾选」**之前**的
    #   口径，而线上早已是 12162/29069/886。带着它出包，组员照着念的数字
    #   和页面上看到的不一样，而且**全程 31 项验收全过、没有任何报错** ——
    #   比直接失败危险得多。
    #   所以现在宁可停：先起服务，再重跑打包。
    print(f"\n✗ 取不到图谱口径（{type(_e).__name__}: {_e}）")
    print("  已重试 4 次仍失败，**中止打包**。")
    print("  请确认 http://127.0.0.1:8770 服务在跑（双击 启动cacx.bat），")
    print("  或等磁盘空闲后重跑。之所以不退回写死的数：")
    print("  那组数来自旧口径，发出去会让组员念的数字和页面对不上，")
    print("  而验收环节查不出来 —— 悄悄发错比发不出去糟糕得多。")
    raise SystemExit(2)

# MCP 工具数也现算：加一个工具就少写一个数字，是上一版「26」这种
# 硬编码最典型的失效方式（数字不会报错，只会悄悄变成假的）。
# ⚠ 别在这里写 `= cfg` —— 本文件没有 cfg 这个变量，NameError 会被下面的
#   except 吞掉、静默退回硬编码值，看起来"跑通了"其实是假的。
#   上一版就是这么差点把 26 又抄进新包，是加了打印才发现的。
try:
    import asyncio as _ai
    import tempfile as _tf
    _sf = SRC / "config" / "settings.json"
    _mtime0 = _sf.stat().st_mtime if _sf.exists() else None
    # ⚠ 本脚本住在 （测试）/ 里，sys.path[0] 是那个目录，`import cacx`
    #   直接 ModuleNotFoundError —— 而它会被下面的 except 吞成「退回 26」，
    #   看起来跑通了，其实这一版的工具数根本没验证过。必须显式把项目根加进来。
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import cacx.mcp_server as _m
    # list_tools 只枚举注册表，理论上不读配置；但仍注入一份指向临时库的
    # 人造 cfg —— 万一它内部要 load()，绝不会去碰 config/settings.json。
    _m._CFG_OVERRIDE = {"db": {"path": str(Path(_tf.mkdtemp()) / "t.db")},
                        "sites": {}, "ai": {}, "search": {}}
    N_TOOLS = len(_ai.run(_m.mcp.list_tools()))
    _mtime1 = _sf.stat().st_mtime if _sf.exists() else None
    check("取 MCP 工具数没动 settings.json", _mtime0 == _mtime1,
          f"{_mtime0} -> {_mtime1}")
    print(f"  MCP 工具数现算：{N_TOOLS}")
except Exception as _e:
    N_TOOLS = 26
    print(f"  🔴 MCP 工具数取不到（{type(_e).__name__}: {_e}），退回硬编码 26 —— "
          f"这一版的工具数**未经验证**")

(stage / "【组员请先看】README.txt").write_text(f"""cacx · 网信办公开语料库工具 —— 组员分发版
生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}
GitHub: https://github.com/CoolCoolC666/cacx  （已开源，MIT）

⚠ 本项目仅供学习研究与课程作业用途。采集内容均来自政府网站公开发布
   页面，著作权归原作者及原发布网站。详见 CONDUCT.md。

【本包内容】
  cacx/                源码
  config/              配置**模板**（不含任何人密钥）
  data/cacx.db         数据库快照：文章 {cs['articles']} 篇、摘要 {cs['ai_annotations']} 条
                       （2024 年以来共 {P['n24']} 篇 / 两站 {P['cols']} 个栏目；
                        其中 {P['body']} 篇有正文，
                        另 {P['n24'] - P['body']} 篇是空正文，抽取与检索都按 {P['body']} 口径。
                        ⚠ 同一篇文章发在多个栏目时 URL 不同、正文相同，
                        这类重复**在 2024 年以来范围内已按正文指纹去掉**；
                        2024 年以前的老文章没清理（不在作业分析范围内），
                        要按全站算篇数时记得这一点）
  README.md            完整说明（先看这个，里面有常见问题）
  CONDUCT.md           语料使用声明
  日志/ docs/          开发留痕与工程文档
  组员对接/            各角色的任务提示词（00~05 + 总方案）——照着干活
  组员数据包/          轻量数据集（小库+CSV，网站跑不动时的兜底）

【本包不含什么】
  X config/settings.json   打包方的密钥配置，**故意没放**
  X config/sites.d/        站点自定义配置，可能带鉴权 header
  X .git/                  版本库历史
  X data/userdict.txt      你自己的分词实验记录，各人从零开始

【三步跑起来】
  1) 解压到**不含空格与中文括号**的路径，例如 D:\\cacx
     （路径含特殊字符会让 .bat 脚本出错，这是 Windows 的老毛病）

  2) 双击 启动cacx.bat（和 start-cacx.bat 效果相同）
     自动装依赖 → 起服务 → 自动开浏览器
     需要 Python 3.10+，安装时务必勾选 "Add Python to PATH"
     关闭黑色命令行窗口 = 关闭服务

  3) 浏览器打开 http://127.0.0.1:8770/

【网站你能直接干什么（不写代码）】
  · 检索：中文可切「整句精确 / 分词扩召回」两种模式，命中高亮，点标题看全文
  · 样本配置器：栏目 + 时间窗(2024-01-01~2099-12-31) + 篇数上限 + 按年分层，
    抽样预览能直接看到每年篇数，⬇CSV 导出（文件带抽样口径页脚）
  · 数据面板：**七张图两组**——检索行为 4 张（按年/按月/栏目Top12/行为）
    + 权责边界 3 张（治理问题分布/高频机构/联合发文月度趋势）。
    每张右上角 ⬇图(PNG·2倍分辨率) / ⬇数据(CSV)，顶部筛选栏联动；
    面板区右上「⬇ 面板 HTML」导出**断网双击即开**的作业版。
    ⚠ 权责边界三张是**关键词粗筛**（图上标了口径），做核对用，别当精确统计引用。
  · 关系图谱：点一条关系 → 列出支撑它的原文句子和可点链接（演示就演这两条）。
    ⚠ 本包已含图谱数据（去重实体 {P['ents']} / 关系 {P['rels']}），**开箱即用**，不用等谁合并。
    ⚠ 数字有三个口径，别混着念（念错当场被问住）：
      - 分析口径（min_degree=2）：{G['nodes']} 节点 / {G['edges']} 边 / {G['comp']} 个连通块
      - 网页画布默认实际绘制：{G['rnodes']} 节点 / {G['redges']} 边
        （权重截到前 {G['rnodes']} 个点，另有 {G['hidden']} 个低度数节点被折叠；
         画布标题那行小字就是当前真值，念之前先看它）
      - 聚焦某机构 1 跳：中央网信办 {G['foc_nodes']} 节点 / {G['foc_edges']} 边
        （在「聚焦」框输入别的机构名，数字会跟着变）
    ⚠ 报告里「居中协调 ≠ 度中心性」那个论点，数据是这样：
      **全图** {G['n0']}介数 {G['b0']}，第二名「{G['n1']}」{G['b1']} —— 只差 {G['b0']/G['b1']:.2f} 倍，
      看全图并不突出；**聚焦它自己 1 跳子图**后，它升到 {G['fb0']}，
      第二名 {G['fb1']} —— 差 {G['fb0']/G['fb1']:.0f} 倍。
      这说明它不是「跟谁都连」的广度型枢纽，而是**只在特定子网里当桥**，
      讲的时候要说清口径，否则等于自相矛盾。
  · 🧪 分词实验室：两件事，都能当场演示「真的生效了」
    - 挖掘新词：无监督统计法（凝固度 + 邻字熵 + 可切分性）从语料挖候选，
      **由你勾选**才写进 jieba 用户词典，写完立即生效、**重启后仍在**。
      候选是候选不是结论 —— 统计只保证「大概率是词」，收不收由人定。
    - 试切分：输入一句话，看它被切成什么样（回车即可）。
    - 🤖 AI 预勾选：一键让 AI 把「像词」的那批候选先勾上，你只管改剩下的。
      两步确认后才真调模型，**它只给建议、不写词典** —— 采纳仍然要你点按钮。
      ⚠ 会消耗你自己的 API 配额；默认只预览，不执行。
    ⚠ 演示建议：先试切「中央网信办深入开展清朗专项行动整治网络谣言」
      （被切碎）→ 采纳「清朗专项行动」「网络谣言」→ 再试一次（合并了）。
      「自己改自己的词典」这件事比任何截图都有说服力。
  · ⇅ 数据同步：只读两站公开列表接口，不登录、不碰授权内容。
    「只盘点」模式**只数条数不写库**，想先看有多少就选它；
    跑起来下面实时日志会逐列滚动，顶部显示 列表/新增/失败/跳过。
    ⚠ 第一次点同步要等一会儿才有动静（列表接口本身慢），
      **别连点两下** —— 同时只能跑一个，第二次会被直接挡回。
    ⚠ 本版修掉了一个会让你「点了完全没反应」的锁问题：
      之前只要站点保持「全部站点」就会卡死（现在好了）。
      如果你从旧包升级过来，遇到点不动就**重启一次服务**。
  · ⚠ 面板按站点筛选时，行为图只统计 open / summarize。
    检索与导出是跨站行为无法归属到某个站，所以不计入——界面上写明了，不是漏统计。

【MCP：给你的 AI 智能体装上手】
  支持 MCP 的智能体（ChatGPT/Claude/Cursor/豆包等）配置里加：
  {{"mcpServers": {{"cacx": {{"command": "python",
      "args": ["-m", "cacx.mcp_server"], "cwd": "你解压的目录"}}}}}}
  连上后共 {N_TOOLS} 个工具：取样/分词检索/机构候选挖掘归并/实体关系写入与查询/图谱统计，
  以及新词相关的 4 个（cacx_newwords_discover / cacx_newwords_review /
  cacx_newwords_adopt / cacx_list_userdict）。
  具体按「组员对接」里你那份提示词说的一步来。
  ⚠ cacx_newwords_adopt 会**往你的用户词典追加词**，默认 confirm=false，
    必须显式传 confirm=true 才真正写入 —— 别指望它自己动手。

【交作业回传（5 人小组的规矩）】
  你写入的是**你自己这份包的本地库**，协调人的主库看不到。
  所以成果一律以 **CSV 为准**：R1 用 cacx_list_entities()/cacx_list_relations()
  导出（或用站点接口导出），R3/R4 交各自 CSV+图+README，R5 交报告 md。
  打包命名 姓名-编号-任务名.zip，**钉钉发回**；**不要发 data/cacx.db**
  （太大、传不动，而且两份库没法自动对账）。

【AI 功能】
  包内不含任何人的密钥。需要摘要/标签就用**你自己的** API 密钥：
  打开网页「设置」页填供应商/地址/模型/密钥，或把其它 AI 工具的配置
  粘贴到设置页顶部自动导入。
  ⚠ 生成摘要会消耗配额 —— 程序默认「只预览不执行」，先预览再生成。
  ⚠ 密钥别发群里、别贴进代码、别提交到仓库。

【给组员共享服务】
  推荐每人跑自己的一份包（互不依赖）。要共用一台的话：
  同一台机器：其他人用 http://127.0.0.1:8770/ 访问即可
  不同电脑：设置页把 app.host 改成 0.0.0.0，同组用那台机器的局域网 IP 访问。
  ⚠ 只在可信内网这么做，别直接暴露到公网。

【数据库说明】
  已含现有全部数据，**不需要**自己抓。
  · 想保留原始数据做对照 → 先复制一份再改
  · 想从零开始 → 删掉 data/cacx.db，然后 python -m cacx sync
""", encoding="utf-8")

print()
print("=" * 66)
print("5  压缩")
print("=" * 66)
zp = DEST / f"{NAME}.zip"
if zp.exists():
    if not to_recycle_file(zp):
        print("  🔴 旧 zip 没能送进回收站（可能被占用）。")
        print("     请关掉资源管理器里打开的 zip 后重跑；**不要**手动强删后继续。")
        sys.exit(2)
    print("  旧的同名 zip 已送回收站")
k = 0
with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in sorted(stage.rglob("*")):
        if f.is_file():
            z.write(f, Path(NAME) / f.relative_to(stage))
            k += 1
check(f"压缩包已生成（{k} 个文件）", zp.exists(),
      f"{zp.stat().st_size/1e6:.1f} MB")

h = hashlib.sha256()
with zp.open("rb") as fh:
    for blk in iter(lambda: fh.read(1 << 20), b""):
        h.update(blk)
print(f"  SHA256: {h.hexdigest()}")
print(f"  路径  : {zp}")
print(f"  散装  : {stage}   （保留，方便组员直接拷贝文件夹）")
print(f"\n通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)