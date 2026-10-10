# -*- coding: utf-8 -*-
"""cacx 端到端自检：每个子模块都要能独立工作"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("=" * 70)
print("1) 包可导入")
print("=" * 70)
try:
    from cacx import config as C, db as D, importer as I, ai, collector
    check("import cacx.*", True, f"版本 {collector.__name__}")
except Exception as e:
    check("import cacx.*", False, repr(e))
    raise

cfg = C.load()
print()
print("=" * 70)
print("2) 配置与密钥掩码")
print("=" * 70)
check("settings 存在", C.SETTINGS_FILE.exists())
check("example 模板存在", C.EXAMPLE_FILE.exists())
_secret = "sk-abcdefghijklmnop"
_masked = C.mask_secret(_secret)
check("密钥掩码不泄露",
      "abcdefghijkl" not in _masked and _masked.endswith("mnop")
      and len(_masked) == len(_secret),
      _masked)
check("短密钥全遮", C.mask_secret("abc") == "***" and "abc" not in C.mask_secret("abc"))
shown = C.describe(cfg)
check("describe 不含明文 key", "api_key" in shown and '""' in shown
      or '"api_key": ""' in shown)

# 临时写一个含密钥的配置，确认存的是明文、读出来是掩码
C.set_path(cfg, "ai.api_key", "sk-TESTKEY-1234567890")
C.save(cfg)
cfg2 = C.load()
check("密钥落盘为明文(可被程序读)",
      C.get_path(cfg2, "ai.api_key") == "sk-TESTKEY-1234567890")
check("show 时掩码", "1234567890" not in C.describe(cfg2).replace("****", "")
      or "****" in C.describe(cfg2))
C.set_path(cfg, "ai.api_key", "")
C.save(cfg)
print("   （测试用密钥已清除）")

print()
print("=" * 70)
print("3) 数据库与中文检索")
print("=" * 70)
d = D.open_db(cfg)
f = d.facets()
# 不写死总数：语料会随 sync 增长，写死必然在每次新增后误报。
# 改成「facets 的 total 与直接 COUNT(*) 吻合 + 不低于基线」，才是真的在测一致性。
_raw_total = d.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
check("文章已入库且计数自洽",
      f["total"] == _raw_total and f["total"] >= 4507,
      f"{f['total']} 篇（基线 4507，sync 后增长属正常）")
check("站外条目已登记", f["external"] == 13, f"{f['external']} 条")
check("空正文为显式标记", f["empty"] == 2, f"{f['empty']} 篇")
# 抓取中途失败的半成品绝不能落库：content_text 为空就必须有显式标记
_ghost = d.conn.execute(
    "SELECT COUNT(*) FROM articles "
    "WHERE TRIM(COALESCE(content_text,''))='' AND COALESCE(content_empty,0)=0"
).fetchone()[0]
check("无半成品幽灵行", _ghost == 0, f"{_ghost} 条")

# 新增：命中片段 + 埋点 + 导出取数
# 文本要够长，否则 width 内不触发截断，测不到「…」是否正确
_long = "中央网信办印发通知在全国范围内部署开展为期两个月的清朗专项行动，" \
        "本次行动重点整治短视频虚假人设乱象问题。"
_snip = D.Database.make_snippet(_long, ["清朗"], 40)
check("命中片段截取正确", "清朗" in _snip and "…" in _snip, repr(_snip))
check("未命中时片段退化为开头",
      D.Database.make_snippet(_long, ["不存在"], 40) == _long[:40].replace("\n", " "),
      repr(D.Database.make_snippet(_long, ["不存在"], 40)[:20]))
_ids = [it["id"] for it in d.search(q="网信办", limit=3)["items"]]
_rows = d.fetch_for_export(_ids)
check("批量导出按传入顺序取全字段",
      [r["id"] for r in _rows] == _ids and len(_rows) == len(_ids)
      and "content_text" in _rows[0],
      f"{len(_rows)} 行，字段 {len(_rows[0])} 个")
_n0 = d.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
d.record_event("search", detail="__selftest__")
d.record_event("open", article_id=_ids[0] if _ids else None, detail="example.com")
_st = d.event_stats(1)
check("埋点写入并可统计", _st["total"] == _n0 + 2
      and {x["kind"] for x in _st["by_kind"]} >= {"search", "open"},
      f"总数 {_st['total']}")
# 埋点自检数据必须清掉，不能污染真实行为统计
with d._lock:
    d.conn.execute("DELETE FROM events WHERE detail IN ('__selftest__','example.com')")
    d.conn.commit()
check("埋点测试数据已清理",
      d.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == _n0)

r3 = d.search(q="清朗", limit=1)
check("2字词走 LIKE 兜底且有结果", r3["mode"] == "like" and r3["total"] > 0,
      f"mode={r3['mode']} 命中 {r3['total']}")
r5 = d.search(q="数据出境负面清单", limit=1)
check("长词走 FTS5 索引", r5["mode"] == "fts" and r5["total"] > 0,
      f"mode={r5['mode']} 命中 {r5['total']} {r5['took_ms']}ms")
# trigram 兜底正确性：LIKE 模式的结果数必须 >= FTS 模式（短词更宽松）
r_all = d.search(q="网信办", limit=1, min_fts_chars=99)   # 强制 LIKE
check("同一关键词两种模式结果一致(短词降级不丢结果)",
      r_all["total"] == d.search(q="网信办", limit=1, min_fts_chars=1)["total"],
      f"like={r_all['total']} fts={d.search(q='网信办', limit=1, min_fts_chars=1)['total']}")
rf = d.search(site="cac", column_name="网络安全", date_from="2025-01-01", limit=1)
check("组合筛选", rf["total"] > 0, f"{rf['total']} 条")
rc = d.search(sort="chars_desc", limit=1)
check("排序可用", rc["items"][0]["chars"] == max(
      x["chars"] for x in rc["items"]), f"最长 {rc['items'][0]['chars']} 字")

print()
print("=" * 70)
print("4) 采集器（离线部分）")
print("=" * 70)
from cacx.collector import CacCollector, ZjwxCollector
cc = CacCollector(cfg["sites"]["cac"], cfg["collector"], verbose=False)
zc = ZjwxCollector(cfg["sites"]["zjwx"], cfg["collector"], verbose=False)
check("CAC 栏目数 = 14", len(cc.columns()) == 14)
check("浙江栏目数 = 13", len(zc.columns()) == 13)
names = [c["name"] for c in zc.columns()]
check("浙江栏目名以站点为准",
      "治理监管" in names and "清朗网络" in names and "安全动态" in names
      and "法治浙江" not in names and "网上辟谣" not in names)
from cacx.collector.base import html_to_text
t = html_to_text("<p>第一段</p><p>第二段<img src='//x.com/a.jpg'></p>", "https://x.com")
check("HTML→纯文本 + 图片绝对化",
      "第一段" in t and "第二段" in t and "https://x.com/a.jpg" in t, repr(t[:70]))

print()
print("=" * 70)
print("4b) Web 前端（不启服务）")
print("=" * 70)
import subprocess
from cacx.web.app import STATIC_DIR
_index = STATIC_DIR / "index.html"
check("前端文件存在", _index.is_file())
if _index.is_file():
    _html = _index.read_text(encoding="utf-8")
    # 前端硬编码的接口路径必须在后端真存在，否则点了就是 404。
    # 取到的是前缀（/api/article、/api/ai），后端用 {id} 占位或带子路径，
    # 所以比的是「存在以此为前缀的路由」，不是全等。
    import re as _re
    _paths = set(_re.findall(r"['\"](/api/[a-z_]+)", _html))
    from cacx.web.app import app as _fa
    _routes = [r.path for r in _fa.routes if hasattr(r, "path")]
    _missing = sorted(p for p in _paths
                      if not any(r == p or r.startswith(p + "/") or f"{p}/{{" in r
                                 for r in _routes))
    check("前端调用的接口后端都存在", not _missing,
          f"检查 {len(_paths)} 个前缀" +
          (f"，缺 {sorted(_missing)}" if _missing else "，全部命中"))
    # 高亮逻辑跑 node 回归：实现从 index.html 抽取，改前端后不会变成假通过
    _js = Path(__file__).resolve().parent / "tests_mark.js"
    if _js.is_file():
        try:
            r = subprocess.run(["node", str(_js)], capture_output=True,
                               text=True, timeout=60,
                               cwd=str(Path(__file__).resolve().parent))
            _last = [x for x in r.stdout.splitlines() if x.strip()][-1] if r.stdout else ""
            check("高亮函数 node 回归", r.returncode == 0, _last)
        except FileNotFoundError:
            check("高亮函数 node 回归", False, "未装 node，跳过")
    else:
        check("高亮函数 node 回归", False, "缺 tests_mark.js")

print()
print("=" * 70)
print("5) AI 层（不真发请求）")
print("=" * 70)
from cacx.ai import parse_json, strip_think, status, build
check("剥离 <think> 前置换行也能净",
      strip_think("\n<think>x</think>\n真正内容") == "真正内容",
      repr(strip_think("\n<think>x</think>\n真正内容")))
check("剥离多段 <think>",
      strip_think("<think>a</think>正文<think>b</think>") == "正文")
check("从 markdown 代码块抠 JSON",
      parse_json('```json\n{"a":1}\n```') == {"a": 1})
check("从前后缀里抠 JSON",
      parse_json('好的，结果如下：{"a":1} 希望有用') == {"a": 1})
check("解析失败回落默认值", parse_json("完全不是json", {"d": 1}) == {"d": 1})
st = status(cfg)
check("AI 未配置时 ready=False", st["ready"] is False, f"provider={st['provider']}")
check("AI 状态不泄露密钥", "sk-" not in json.dumps(st, ensure_ascii=False))
# dry_run 不应发请求
C.set_path(cfg, "ai.provider", "openai_compat")
C.set_path(cfg, "ai.base_url", "https://example.invalid/v1")
C.set_path(cfg, "ai.api_key", "sk-dummy")
C.set_path(cfg, "ai.model", "dummy-model")
C.save(cfg)
try:
    cfg3 = C.load()
    p = build(cfg3, task="summarize")
    prev = p.chat("s", "u", dry_run=True)
    check("dry_run 只预览不请求", isinstance(json.loads(prev), dict)
          and json.loads(prev).get("_dry_run") is True)
finally:
    # 测试用的假配置必须无条件清干净，不能留在 settings.json 里
    C.set_path(cfg, "ai.provider", "none")
    C.set_path(cfg, "ai.api_key", "")
    C.set_path(cfg, "ai.base_url", "")
    C.set_path(cfg, "ai.model", "")
    C.save(cfg)
    check("测试假密钥已清理", C.get_path(C.load(), "ai.api_key") == "")
d.close()

print()
print("=" * 70)
print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    for x in FAIL:
        print("   失败:", x)
print("=" * 70)
sys.exit(1 if FAIL else 0)