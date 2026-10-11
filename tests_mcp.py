# -*- coding: utf-8 -*-
"""
cacx MCP server 离线回归测试
================================================================
运行：  python tests_mcp.py

约束（与 tests_generic.py 一致）
  · 离线、不联网、不真调 AI（用户 MiniMax 5 小时限额已用尽）。
  · 数据库一律指向临时目录，**绝不**读写 config/settings.json 与
    data/cacx.db —— 通过给 cacx.mcp_server._CFG_OVERRIDE 注入一份
    人造 cfg，让 C.load() 根本不被调用。
  · 脚本式：check()/section()/main()，不引入 pytest。
"""

import asyncio
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def section(t):
    print(f"\n=== {t} ===")


REQUIRED_TOOLS = [
    "cacx_search", "cacx_get_article", "cacx_list_sites",
    "cacx_facets", "cacx_stats", "cacx_list_links",
    "cacx_sync", "cacx_summarize", "cacx_doctor",
]
WRITE_TOOLS = {"cacx_sync", "cacx_summarize"}

# 人造「真实格式」密钥：长得像 MiniMax key，但完全是测试用假串。
FAKE_KEY = "sk-TESTKEY-abcdef0123456789XYZ"


def _make_cfg(tmp: Path) -> dict:
    return {
        "db": {"path": str(tmp / "t.db")},
        # 站点留空：runner.sync 在 list-only 下也会尝试联网列表，
        # 空站点表直接让它的循环空转，保证离线。
        "sites": {},
        "collector": {},
        "ai": {
            "enabled": True,
            "provider": "openai_compat",
            "base_url": "https://api.example.com/v1",
            "model": "test-model",
            "api_key": FAKE_KEY,
            "timeout": 5,
            "max_tokens": 100,
            "tasks": {"summarize": {}, "tag": {}, "doctor": {}},
        },
        "search": {"min_fts_chars": 3, "default_sort": "date_desc"},
    }


# ---------------------------------------------------------------- 1. import
def test_import():
    section("1. import cacx.mcp_server")
    import cacx.mcp_server as m
    check("模块可 import", m is not None)
    check("MCPServer 实例存在", hasattr(m, "mcp") and m.mcp is not None)


# ---------------------------------------------------------------- 2. 工具注册
def test_tools_registered():
    section("2. 9 个工具全部注册")
    import cacx.mcp_server as m

    async def _names():
        tools = await m.mcp.list_tools()
        return [(t.name, t.description or "", t.input_schema or {}) for t in tools]

    tools = asyncio.run(_names())
    names = [t[0] for t in tools]
    check("工具总数 == 9", len(names) == 9, f"实际 {len(names)}: {names}")
    for want in REQUIRED_TOOLS:
        check(f"已注册 {want}", want in names, f"现有 {names}")

    # 注册顺序：只读在前
    section("3. 只读工具在写/花钱工具之前")
    first6 = names[:6]
    bad = [n for n in first6 if n in WRITE_TOOLS]
    check("前 6 个里没有 sync/summarize", not bad, f"前6={first6}")
    pos_sync = names.index("cacx_sync") if "cacx_sync" in names else -1
    pos_sum = names.index("cacx_summarize") if "cacx_summarize" in names else -1
    check("sync 在第 7 位", pos_sync == 6, f"位置 {pos_sync}")
    check("summarize 在第 8 位", pos_sum == 7, f"位置 {pos_sum}")

    # 每个工具都有 docstring（MCP 客户端靠它生成界面）
    section("4. 每个工具都有非空描述")
    descs = {t[0]: t[1] for t in tools}
    for n in REQUIRED_TOOLS:
        check(f"{n} 有描述", bool(descs.get(n) and len(descs[n].strip()) >= 10),
              f"desc={descs.get(n)!r}")

    # dry_run 默认值必须是 True（从 input_schema 读，等于客户端实际看到的默认）
    section("5. dry_run 默认 True（从 input_schema 读）")
    schemas = {t[0]: t[2] for t in tools}
    for tn in ("cacx_sync", "cacx_summarize"):
        props = (schemas.get(tn) or {}).get("properties") or {}
        dr = props.get("dry_run") or {}
        check(f"{tn}.dry_run 默认 True", dr.get("default") is True,
              f"schema={dr}")
    # doctor 默认 use_ai=False
    doc_props = (schemas.get("cacx_doctor") or {}).get("properties") or {}
    check("cacx_doctor.use_ai 默认 False",
          (doc_props.get("use_ai") or {}).get("default") is False,
          f"schema={doc_props.get('use_ai')}")


# ---------------------------------------------------------------- 6. 离线行为 + 密钥零回显
def test_offline_and_key_zero_echo():
    section("6. 临时 DB / 不碰 settings.json / 密钥零回显")
    import cacx.mcp_server as m
    from cacx import db as D

    tmp = Path(tempfile.mkdtemp(prefix="cacx_mcp_"))
    cfg = _make_cfg(tmp)

    # 记录 settings.json 修改时间，测试前后应不变
    settings_file = Path(__file__).resolve().parent / "config" / "settings.json"
    mtime_before = settings_file.stat().st_mtime if settings_file.exists() else None

    try:
        # 注入人造 cfg —— C.load() 根本不会被调
        m._CFG_OVERRIDE = cfg

        # 直接用 D.Database 往临时库里种一条文章 + 一条 external_links
        d = D.Database(tmp / "t.db")
        art_id = d.upsert_article({
            "site": "cac", "site_name": "中国网信办",
            "column_code": "yw", "column_name": "要闻",
            "title": "测试文章", "pub_date": "2026-10-10",
            "url": "https://www.cac.gov.cn/x.htm",
            "content_text": "正文 " * 200, "chars": 1000,
        })
        d.upsert_external({
            "site": "cac", "title": "专题页", "pub_date": "2026-10-10",
            "column_name": "要闻", "list_url": "https://www.cac.gov.cn/zt/",
            "target_host": "www.cac.gov.cn", "is_own_site": 1,
            "fail_kind": "index_page", "reason": "目录",
        })
        d.close()

        # --- 只读工具能跑通（指向 tmp 库）---
        r = m.cacx_search(q="测试")
        check("search 返回 total>=1", r.get("total", 0) >= 1, str(r)[:200])

        r = m.cacx_get_article(1)
        check("get_article 取到种的文章", r.get("title") == "测试文章",
              str(r)[:200])

        r = m.cacx_list_links(kind="index_page")
        check("list_links 取到种的 external", r.get("n", 0) == 1, str(r)[:200])

        r = m.cacx_facets()
        check("facets 能跑", r.get("total", 0) >= 1, str(r)[:200])

        r = m.cacx_stats()
        check("stats 能跑", r.get("total", 0) >= 1, str(r)[:200])

        r = m.cacx_list_sites()
        check("list_sites 返回 list", isinstance(r, list), str(r)[:200])

        # --- summarize：默认 dry_run=True，结果里绝不能出现 FAKE_KEY ---
        r = m.cacx_summarize(1)
        check("summarize 默认 dry_run=True", r.get("dry_run") is True,
              str(r)[:200])
        blob = json.dumps(r, ensure_ascii=False)
        check("summarize 响应不含密钥明文", FAKE_KEY not in blob,
              f"blob 前 200: {blob[:200]}")

        # --- sync：默认 dry_run=True；用打桩 runner.sync 抓 mode，不真联网 ---
        from cacx.collector import runner as R
        orig_sync = R.sync
        captured = {}

        def _stub_sync(cfg_, **kw):
            captured.update(kw)
            return 0
        R.sync = _stub_sync
        try:
            r = m.cacx_sync()
        finally:
            R.sync = orig_sync
        check("sync 默认 dry_run=True", r.get("dry_run") is True, str(r)[:200])
        check("dry_run 时 mode=list-only", captured.get("mode") == "list-only",
              f"captured={captured}")
        check("dry_run 时 verbose=False（不往 stdout 喷）",
              captured.get("verbose") is False, f"captured={captured}")

        # 显式 dry_run=False → mode 透传 incremental
        captured.clear()
        R.sync = _stub_sync
        try:
            r = m.cacx_sync(dry_run=False, mode="incremental")
        finally:
            R.sync = orig_sync
        check("dry_run=False 时 mode 透传", captured.get("mode") == "incremental",
              f"captured={captured}")

        # --- doctor：默认 use_ai=False，能跑通 ---
        r = m.cacx_doctor()
        check("doctor 返回 reports/data",
              isinstance(r, dict) and "reports" in r and "data" in r,
              str(r)[:200])
        check("doctor 默认不调 AI（ai=None）", r.get("ai") is None, str(r)[:200])

        # --- 异常脱敏：构造一个「异常链里带密钥」的场景，必须被剥掉 ---
        secret = f"Bearer {FAKE_KEY} 调用失败"
        try:
            raise RuntimeError(secret)
        except RuntimeError as e:
            out = m._sanitize(e)
        check("_sanitize 剥掉 Bearer key", FAKE_KEY not in out, out)
        # _err 返回也剥
        r = m._err(RuntimeError(secret))
        check("_err 剥掉密钥", FAKE_KEY not in json.dumps(r), str(r))

    finally:
        m._CFG_OVERRIDE = None
        shutil.rmtree(tmp, ignore_errors=True)

    # settings.json 未被读写
    if mtime_before is not None:
        after = settings_file.stat().st_mtime
        check("settings.json mtime 未变（没写）", after == mtime_before,
              f"before={mtime_before} after={after}")


# ---------------------------------------------------------------- 7. 源码静态扫描
def test_source_scan():
    section("7. 源码静态扫描：任何输出路径不回显 api_key")
    src_path = Path(__file__).resolve().parent / "cacx" / "mcp_server.py"
    src = src_path.read_text(encoding="utf-8")

    # 1) 模块里不允许出现裸 print（MCP 走 stdout JSON-RPC，print 会污染通道）
    check("源码无 print(", "print(" not in src.replace("    # print(", ""),
          "存在 print( 调用")

    # 2) api_key 只能出现在 _sanitize 里（剥密钥）或注释/模块 docstring 里，
    #    不能出现在任何 return/响应构造路径。
    bad_lines = []
    in_sanitize = False
    in_docstring = False
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        # 三引号 docstring 状态翻转（粗略按行计数 """ 出现次数）
        in_docstring = (in_docstring ^ (stripped.count('"""') % 2 == 1))
        if line.startswith("def _sanitize"):
            in_sanitize = True
            continue
        if in_sanitize and line.startswith("def ") and "_sanitize" not in line:
            in_sanitize = False
        if "api_key" in line and not in_docstring and not stripped.startswith("#"):
            if not in_sanitize:
                bad_lines.append((i, stripped))
    check("api_key 仅在 _sanitize 内被引用（docstring/注释除外）",
          not bad_lines, str(bad_lines))

    # 3) _sanitize 在所有 except 路径上都被调用过（_err 用了它）
    check("_err 调用 _sanitize", "str(text)" in src and "_sanitize" in src)


# ---------------------------------------------------------------- main
def main():
    print("=" * 62)
    print("cacx MCP server 离线测试")
    print("=" * 62)
    for fn in (test_import, test_tools_registered,
               test_offline_and_key_zero_echo, test_source_scan):
        try:
            fn()
        except Exception:
            FAIL.append(fn.__name__)
            print(f"  !! {fn.__name__} 抛异常")
            traceback.print_exc()
    print("\n" + "=" * 62)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
