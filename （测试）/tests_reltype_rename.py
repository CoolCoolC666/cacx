# -*- coding: utf-8 -*-
"""关系类型改名后的**残留闸**：co_issue 不许再出现在任何地方。

⚠ 为什么要专门立一道闸：
  改名（co_issue → joint_issue）本身只改了一行常量，真正会出事的是
  **漏改**——某处硬编码的旧名会让那一小块功能静默失效：
    · 库里的旧值还在 → 按类型筛选查不出来，且**不报任何错**
    · 前端映射表漏一个键 → 图例上显示成原始英文 `co_issue`
    · 文档里留着旧名 → 组员照着旧文档改代码，改出个新 bug
  这三种都不会让测试变红，只会让「高亮/筛选/对接」在某个角落悄悄失灵。

所以守三条：**库无旧值、源码无旧名、文档无旧名**。
（分发包与备份目录不在范围内：前者是打包产物会重打，后者是历史留档。）
"""

import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OLD = "co_issue"
NEW = "joint_issue"

# 这些目录不算「我们的代码/文档」
SKIP_DIRS = {"网站程序", "_备份_历史重写前", "node_modules", ".git",
             "data", "__pycache__"}
SCAN_SUFFIX = {".py", ".md", ".html", ".txt", ".bat", ".json"}

# ⚠ 白名单存在，是因为「解释这次改名的地方」**必然**要写出旧名：
#   db.py 的迁移表与注释、这份闸自己、报告闸里核对触发词的那句。
#   它们不是漏改，是「改名的证据」。
#   但白名单必须**窄到行**且逐条写理由 —— 写成「整个文件放行」，
#   等于给这个文件开了一个可以随便塞旧名的后门。
ALLOW = [
    ("cacx/db.py", '"co_issue": "joint_issue"'),      # 迁移登记表本体
    ("cacx/db.py", "co_issue"),                       # 同上文件里解释为何改名的注释
    ("（测试）/tests_reltype_rename.py", "co_issue"),  # 这份闸自己要用它当搜索词
    ("（测试）/_mutcheck_reltype.py", "co_issue"),     # 反向验证必须写出旧名才能做变异
    ("（测试）/_verify_report.py", "co_issue"),        # 核对报告措辞时引用旧名
]


def _allowed(rel, line):
    return any(rel == f and sub in line for f, sub in ALLOW)


def _files():
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIX:
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel


class TestNoLegacyRelType(unittest.TestCase):

    def test_源码与文档无旧名(self):
        bad = []
        for p, rel in _files():
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if OLD in line and not _allowed(rel.as_posix(), line):
                    bad.append("%s:%d" % (rel.as_posix(), i))
        self.assertEqual(bad, [], "仍含 %s：%s" % (OLD, bad[:20]))

    def test_数据库无旧值(self):
        """读真库用 mode=ro —— 这条测试绝不能改用户的库。

        ⚠ 库路径可用环境变量 CACX_TEST_DB 覆盖，是为了**反向验证**：
          验证「库里有旧值会被抓住」就得先造一个含旧值的库，
          而往真库里插一行再删掉，是在用户的资产上做写操作 ——
          万一进程被杀在中间，就留下一条脏数据。
          指向副本就完全不用碰真库。
        """
        db = Path(os.environ.get("CACX_TEST_DB")
                  or (ROOT / "data" / "cacx.db"))
        if not db.is_file():
            self.skipTest("找不到库：%s" % db)
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM relations WHERE rel_type=?",
                (OLD,)).fetchone()[0]
            new = con.execute(
                "SELECT COUNT(*) FROM relations WHERE rel_type=?",
                (NEW,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0, "库里还有 %d 行旧值 %s" % (n, OLD))
        self.assertGreater(new, 0, "新值 %s 一行都没有 —— 迁移可能被回滚了" % NEW)

    def test_迁移登记了这次改名(self):
        """改名表必须还在：否则组员那边升级时不会自动改名。"""
        from cacx.db import REL_TYPE_RENAMES
        self.assertEqual(REL_TYPE_RENAMES.get(OLD), NEW)
        # 只登记这一条；将来再加别的改名时，别忘了这里要跟着改
        self.assertEqual(set(REL_TYPE_RENAMES), {OLD})

    def test_抽取规则用的是新名(self):
        from cacx import extract
        src = (ROOT / "cacx" / "extract.py").read_text(encoding="utf-8")
        self.assertIn('add_rel(a, b, "%s"' % NEW, src)
        self.assertNotIn('add_rel(a, b, "%s"' % OLD, src)
        self.assertTrue(hasattr(extract, "extract_article"))

    def test_前端关系类型映射含新名(self):
        """前端 rel_type → 中文必须只有**一处**定义（GP_REL_CN）。

        以前指标面板、边列表各写了一份字面量，改名时漏掉的那份会
        **静默**退回显示英文原名 —— 不报错，只是界面悄悄变难读。
        所以这里断言「定义处只有一处，且三处都在用它」。
        """
        html = (ROOT / "cacx" / "web" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertNotIn(OLD, html)
        defines = len(re.findall(r"GP_REL_CN\s*=\s*\{", html))
        self.assertEqual(defines, 1,
                         "GP_REL_CN 被定义了 %d 处，中文名必须只有一个来源" % defines)
        for key in ("joint_issue", "issue", "target", "adopt"):
            self.assertIn("%s:'联合发文'" % NEW if key == NEW else key + ":",
                          html, "标签表缺 %s" % key)
        # 三处消费点都得引用同一个表，不能再有内联字面量
        self.assertEqual(html.count("'联合发文'"), 1,
                         "「联合发文」字面量出现多处，说明又散开成副本了")


if __name__ == "__main__":
    unittest.main(verbosity=2)