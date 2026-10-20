# -*- coding: utf-8 -*-
"""反向验证：把本轮的三处修复分别「改回去」，确认测试真的会红。

⚠ 会临时改写 cacx/ 下的源文件，然后**逐字节还原**并校验 sha256。
  还原失败会直接抛错，不留半改状态。
⚠ 按**行**替换而不是按多行串：cacx/graph_analysis.py 是 CRLF，
  用 "\n" 拼的多行串在那里永远匹配不上（第一版就栽在这，静默跳过）。
"""
import hashlib
import io
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "（测试）")

# (标签, 相对路径, 原行, 改成, 该红的测试全名)
MUTATIONS = [
    ("A 建图改回 LIMIT 5000", "cacx/web/app.py",
     "    rows = d.list_relations(limit=None, site=site,",
     "    rows = d.list_relations(limit=5000, site=site,",
     "tests_graph_limit.TestGraphPayloadUsesFullData."
     "test_建图取关系必须全量"),
    ("A2 底数那张图改回 5000", "cacx/web/app.py",
     "        rows_all = d.list_relations(limit=None)",
     "        rows_all = d.list_relations(limit=5000)",
     "tests_graph_limit.TestGraphPayloadUsesFullData."
     "test_带站点筛选时底数那张图也必须全量"),
    ("B 实体不再限定 measure", "cacx/web/app.py",
     "    ents = d.list_entities(type=\"measure\", limit=None)",
     "    ents = d.list_entities(limit=None)",
     "tests_graph_limit.TestGraphPayloadUsesFullData."
     "test_建图取实体必须全量且只要measure"),
    ("C 画图错喂**全图**的介数映射", "cacx/graph_analysis.py",
     "    btw = result.get(\"node_betweenness\")",
     "    btw = nx.betweenness_centrality(G, weight=None)",
     "tests_graph_limit.TestPyvisReuseIsFaithful."
     "test_焦点下复用与重算一致"),
    ("D 剥引用退回贪婪匹配（吃掉代码）", "cacx/web/app.py",
     "        if not rest.startswith(\"<\") or rest.startswith(\"</script>\"):",
     "        if True:",
     "tests_graph_limit.TestStripLocalRefs."
     "test_自闭合script绝不吃掉后面的代码"),
]

results = []


def _replace_line(path, old, new):
    """按**整行**精确替换一行。

    ⚠ 必须按行而不是多行串：cacx/graph_analysis.py 是 CRLF，
      用 "\n" 拼的多行串在那里永远匹配不上（第一版就栽在这，静默跳过）。
    ⚠ 每个变异都必须是**单行**，否则缩进回退的判断很容易连带吃掉
      周围的正常代码，最后报的是 SyntaxError 而不是断言失败 ——
      那不算「测试抓住了」（测试抓不住 bug 等于没测）。
    """
    lines = io.open(path, encoding="utf-8").read().splitlines(keepends=True)
    hit = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == old]
    if len(hit) != 1:
        raise RuntimeError(f"待改行命中 {len(hit)} 次（应为 1）：{old!r}")
    eol = "\r\n" if lines[hit[0]].endswith("\r\n") else "\n"
    lines[hit[0]] = new + eol
    return "".join(lines)


for label, rel, old, new, testcase in MUTATIONS:
    path = os.path.join(ROOT, rel)
    orig = io.open(path, "rb").read()
    orig_sha = hashlib.sha256(orig).hexdigest()
    try:
        patched = _replace_line(path, old, new)
        io.open(path, "w", encoding="utf-8", newline="").write(patched)
        p = subprocess.run(
            [sys.executable, "-m", "unittest", testcase, "-v"],
            cwd=TESTS, capture_output=True)
        out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
        # 只认「断言失败」：语法错 / 导入错都说明变异方式有问题，不算抓住
        caught = (p.returncode != 0) and ("AssertionError" in out)
        last = [x for x in out.strip().splitlines() if x.strip()][-1]
        results.append((label, "抓住 ✓" if caught
                        else f"没抓住 ✗(rc={p.returncode})", last[:96]))
    except RuntimeError as e:
        results.append((label, "跳过", str(e)[:110]))
    finally:
        io.open(path, "wb").write(orig)
        now = hashlib.sha256(io.open(path, "rb").read()).hexdigest()
        assert now == orig_sha, f"还原失败！{rel} 已损坏，请用 git checkout 恢复"

print("=" * 74)
print("反向验证（把修复改回去，测试必须变红）")
print("=" * 74)
ok = 0
for label, verdict, note in results:
    print(f"  {label:34} {verdict}")
    if note:
        print(f"      {note}")
    if verdict.startswith("抓住"):
        ok += 1
print(f"\n  {ok}/{len(results)} 个变异被测试抓住")

print("\n" + "=" * 74)
print("正向（修复在位时，同一批测试必须全过）")
print("=" * 74)
p = subprocess.run([sys.executable, "tests_graph_limit.py"], cwd=TESTS,
                   capture_output=True)
# unittest 把结果写 stderr，不是 stdout
forward = ((p.stderr or b"") + (p.stdout or b"")).decode("utf-8", "replace")
for line in forward.strip().splitlines()[-4:]:
    print("  " + line)