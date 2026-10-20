# -*- coding: utf-8 -*-
"""反向验证：把残留闸的三处守卫生意弄坏，tests_reltype_rename 必须变红。

  M1 库里塞回一行旧值 —— 只在**副本**上做，并用 CACX_TEST_DB 指过去
  M2 从迁移表里删掉这次改名（使用方就不会自动改名了）
  M3 前端映射表漏一个键（漏的那处图例会显示英文原名）
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "（测试）" / "tests_reltype_rename.py"
DB = ROOT / "cacx" / "db.py"
HTML = ROOT / "cacx" / "web" / "static" / "index.html"


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(db=None):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if db is not None:
        env["CACX_TEST_DB"] = str(db)
    else:
        env.pop("CACX_TEST_DB", None)
    r = subprocess.run([sys.executable, "-X", "utf8", str(TEST)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT), env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


print("=== 反向验证：每个变异都必须让残留闸变红 ===")
rc, out = run()
print("基线（未变异）: returncode=%d %s\n" % (rc, "OK" if rc == 0 else "FAIL"))
if rc != 0:
    print(out[-2000:])
    sys.exit(2)

bad = []

# ---- M1 库里塞回一行旧值 -------------------------------------------------
# 只在副本上做：真库是这个项目的资产，往里插一行再删掉，
# 万一进程被杀在中间就留下脏数据。指向副本就完全不用碰它。
src_db = ROOT / "data" / "cacx.db"
if src_db.is_file():
    tmp = ROOT / "（测试）" / "_mut_reltype_copy.db"
    shutil.copy2(src_db, tmp)
    try:
        con = sqlite3.connect(str(tmp))
        con.execute("INSERT INTO relations (src_name, dst_name, rel_type, "
                    "article_id, evidence, weight, extractor) "
                    "SELECT 'A','B','co_issue', MIN(id), '联合印发', 1, 'mut' "
                    "FROM articles")
        con.commit()
        con.close()
        rc1, _ = run(db=tmp)
        print("  %s  M1 库里有旧值（打在副本上）"
              % ("RED " if rc1 != 0 else "!!! "))
        if rc1 == 0:
            bad.append("M1（库里塞旧值没能被抓住）")
    finally:
        tmp.unlink(missing_ok=True)
else:
    print("  SKIP  M1 —— 本机没有 data/cacx.db")

# ---- M2 迁移表里删掉这次改名 ---------------------------------------------
before = sha(DB)
bak = DB.with_name(DB.name + ".bak")
shutil.copy2(DB, bak)
try:
    DB.write_text(DB.read_text(encoding="utf-8").replace(
        'REL_TYPE_RENAMES = {"co_issue": "joint_issue"}',
        "REL_TYPE_RENAMES = {}"), encoding="utf-8")
    red = run()[0] != 0
    print("  %s  M2 迁移表被删空" % ("RED " if red else "!!! "))
    if not red:
        bad.append("M2（迁移表删了仍全绿 = 使用方不会自动改名）")
finally:
    shutil.copy2(bak, DB)
    bak.unlink()
if sha(DB) != before:
    bad.append("M2（未还原干净）")

# ---- M3 前端标签表被拆成两份副本 ----------------------------------------
before = sha(HTML)
bak = HTML.with_name(HTML.name + ".bak")
shutil.copy2(HTML, bak)
try:
    src = HTML.read_text(encoding="utf-8")
    # 模拟「顺手又写了一份字面量」—— 旧代码正是这么烂掉的
    HTML.write_text(
        src.replace("const label = GP_REL_CN;",
                    "const label = {issue:'发布', target:'针对', "
                    "adopt:'采取', joint_issue:'联合发文'};", 1),
        encoding="utf-8")
    red = run()[0] != 0
    print("  %s  M3 标签表被拆成两份副本" % ("RED " if red else "!!! "))
    if not red:
        bad.append("M3（标签表散成副本仍全绿 = 以后改名必漏一处）")
finally:
    shutil.copy2(bak, HTML)
    bak.unlink()
if sha(HTML) != before:
    bad.append("M3（未还原干净）")

print()
if bad:
    print("未达成：%d 项" % len(bad))
    for b in bad:
        print("  - " + b)
    sys.exit(1)
print("反向验证通过：3 个变异全部变红，文件已还原。")