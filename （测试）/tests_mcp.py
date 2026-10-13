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
import copy
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    "cacx_ai_status",
    "cacx_sync", "cacx_summarize",
    "cacx_summarize_batch", "cacx_ai_config",
    "cacx_doctor",
    # 删数据的工具排在最后：它不烧钱但**不可逆**，
    # 放在只读工具后面是为了让默认的探索路径永远不会碰到它。
    "cacx_purge",
    # 分析工作台：让 AI 一次会话走完 R1→R2
    "cacx_sample_options", "cacx_sample", "cacx_mine_orgs",
    "cacx_put_entities", "cacx_put_relations",
    "cacx_list_entities", "cacx_list_relations",
    "cacx_graph", "cacx_graph_stats",
    # 分词自学习（P5）。discover/list 是只读；review 只给建议不写盘；
    # adopt 会改词典，所以 confirm 默认 False —— 它必须留在
    # WRITE_TOOLS 里被「未确认不写盘」那条闸门盯住。
    "cacx_newwords_discover", "cacx_newwords_review",
    "cacx_newwords_adopt", "cacx_list_userdict",
]
WRITE_TOOLS = {"cacx_sync", "cacx_summarize",
               "cacx_summarize_batch", "cacx_ai_config",
               "cacx_purge", "cacx_put_entities", "cacx_put_relations",
               # ⚠ 采纳会改词典。词典是**累积资产**：写进去之后所有检索、
               #   词频、实体抽取都按它切分，误写不会报错，只会持续出偏差。
               #   所以它和 purge 一样必须走 confirm 闸门。
               #   （review 不算写操作 —— 它一个字都不落盘。）
               "cacx_newwords_adopt"}

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
    section("2. 全部工具注册")
    import cacx.mcp_server as m

    async def _names():
        tools = await m.mcp.list_tools()
        return [(t.name, t.description or "", t.input_schema or {}) for t in tools]

    tools = asyncio.run(_names())
    names = [t[0] for t in tools]
    # ⚠ 数量从 REQUIRED_TOOLS 推，别写死数字：写死的话每加一个工具
    #   都得记得改这里，忘了就变成一条跟功能无关的失败。
    check(f"工具总数 == {len(REQUIRED_TOOLS)}",
          len(names) == len(REQUIRED_TOOLS), f"实际 {len(names)}")
    for want in REQUIRED_TOOLS:
        check(f"已注册 {want}", want in names, f"现有 {names}")

    # 注册顺序：只读在前、写/花钱在后
    section("3. 注册顺序（只读在前，写/花钱在后）")
    first7 = names[:7]
    bad = [n for n in first7 if n in WRITE_TOOLS]
    check("前 7 个里没有写/花钱工具", not bad, f"前7={first7}")
    pos = {n: (names.index(n) if n in names else -1) for n in REQUIRED_TOOLS}
    # ⚠ 不断言绝对序号 —— 以后再加工具，序号必然整体位移，
    # 那种测试会逼着人改数字而不是改设计。改为断言**相对顺序**：
    # 不可逆的删除工具永远排最后，探索路径碰不到它。
    def _before(a, b):
        return pos[a] < pos[b]
    # ⚠ 不断言绝对序号 —— 以后再加工具，序号必然整体位移，
    # 那种测试会逼着人改数字而不是改设计。改为断言**相对顺序**：
    # purge 是唯一不可逆的删除工具，它必须排在「探索→写入」这条
    # 主路径的**后面**，不能夹在中间让人顺手点到。
    # （它不必是全局末位：cacx_graph 等只读工具排在它后面是安全的，
    #   探索路径的终点仍然碰不到删除。）
    for r in ("cacx_search", "cacx_get_article", "cacx_facets",
              "cacx_sample", "cacx_sample_options", "cacx_mine_orgs",
              "cacx_graph", "cacx_graph_stats",
              "cacx_put_entities", "cacx_put_relations"):
        check(f"{r} 在 purge 之前", _before(r, "cacx_purge"),
              f"pos={pos.get(r)} purge={pos['cacx_purge']}")
    # 只读工具应整体早于写工具
    # 只读工具应整体早于写工具。
    # ⚠ 注意：不能断言「cacx_graph 早于 cacx_put_relations」——
    #   注册顺序由源码里的装饰器位置决定，而 cacx_graph 依赖的
    #   db.graph() 写在实体写入方法**之后**，所以它必然注册在后。
    #   这不影响使用：AI 拿到的工具列表顺序不影响调用可行性，
    #   只影响默认展示次序。硬要它「按工作流排序」只能靠调换函数定义
    #   位置，代价是让读代码的人看到乱序的定义 —— 不值得。
    check("只读搜索早于写入实体", _before("cacx_search", "cacx_put_entities"))
    check("只读图谱早于 purge", _before("cacx_graph", "cacx_purge"))
    # 工作流顺序：选样本 → 挖机构 → 写关系
    check("选样本早于挖机构（工作流顺序）",
          _before("cacx_sample_options", "cacx_mine_orgs"))
    check("挖机构早于写关系（工作流顺序）",
          _before("cacx_mine_orgs", "cacx_put_relations"))
    # purge 之后不应再有其他「不可逆」工具
    after = names[names.index("cacx_purge") + 1:]
    IRREVERSIBLE = WRITE_TOOLS - {"cacx_put_entities", "cacx_put_relations"}
    bad = [n for n in after if n in IRREVERSIBLE]
    check("purge 之后没有不可逆工具", not bad, f"其后有 {bad}")

    # 每个工具都有 docstring（MCP 客户端靠它生成界面）
    section("4. 每个工具都有非空描述")
    descs = {t[0]: t[1] for t in tools}
    for n in REQUIRED_TOOLS:
        check(f"{n} 有描述", bool(descs.get(n) and len(descs[n].strip()) >= 10),
              f"desc={descs.get(n)!r}")

    # dry_run 默认值必须是 True（从 input_schema 读，等于客户端实际看到的默认）
    section("5. dry_run 默认 True（从 input_schema 读）")
    schemas = {t[0]: t[2] for t in tools}
    for tn in ("cacx_sync", "cacx_summarize",
               "cacx_summarize_batch", "cacx_ai_config", "cacx_purge"):
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
    settings_file = Path(__file__).resolve().parent.parent / "config" / "settings.json"
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


# ---------------------------------------------------------------- 7. ai_status
def test_ai_status():
    section("7. cacx_ai_status（只读 / 密钥零回显）")
    import cacx.mcp_server as m

    tmp = Path(tempfile.mkdtemp(prefix="cacx_mcp_ais_"))
    m._CFG_OVERRIDE = _make_cfg(tmp)
    try:
        r = m.cacx_ai_status()
        check("ai_status 返回 dict", isinstance(r, dict), str(r)[:200])
        check("has_key=True", r.get("has_key") is True, str(r)[:200])
        km = r.get("key_masked") or ""
        check("key_masked 非空", bool(km), str(r)[:200])
        check("key_masked != 明文", km != FAKE_KEY, km)
        check("key_masked 保留末4位", km.endswith(FAKE_KEY[-4:]), km)
        for k in ("enabled", "provider", "model", "base_url", "ready",
                  "timeout", "max_tokens", "tasks"):
            check(f"ai_status 含 {k}", k in r, str(r)[:200])
        blob = json.dumps(r, ensure_ascii=False)
        check("ai_status 响应不含明文 key", FAKE_KEY not in blob,
              f"blob 前 200: {blob[:200]}")
    finally:
        m._CFG_OVERRIDE = None
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 8. ai_config
def test_ai_config():
    section("8. cacx_ai_config 双闸门 + 密钥三态")
    import cacx.mcp_server as m
    from cacx import config as C

    tmp = Path(tempfile.mkdtemp(prefix="cacx_mcp_cfg_"))
    cfg = _make_cfg(tmp)
    m._CFG_OVERRIDE = cfg

    saved_calls = []
    orig_save = C.save
    def fake_save(cfg_):
        saved_calls.append(copy.deepcopy(cfg_))
        return Path("fake-settings.json")
    C.save = fake_save
    try:
        before_model = cfg["ai"]["model"]

        # --- dry_run=True：不写盘 / would_change 对照 / 掩码 ---
        r = m.cacx_ai_config(model="new-model-xyz", dry_run=True)
        check("dry_run=True 返回 dry_run", r.get("dry_run") is True, str(r)[:200])
        check("dry_run=True changes=1", r.get("changes") == 1, str(r)[:200])
        wc = r.get("would_change") or []
        rec = [w for w in wc if w["field"] == "model"]
        check("would_change 含 model", len(rec) == 1, str(wc))
        if rec:
            check("model old/new 对照",
                  rec[0].get("old") == before_model
                  and rec[0].get("new") == "new-model-xyz", str(rec[0]))
        check("dry_run 未写盘", len(saved_calls) == 0, f"saved={len(saved_calls)}")
        check("dry_run 未改 override", cfg["ai"]["model"] == before_model,
              cfg["ai"]["model"])
        blob = json.dumps(r, ensure_ascii=False)
        check("dry_run 响应不含明文 key", FAKE_KEY not in blob, blob[:200])

        # --- provider 非法：报错且不写盘 ---
        r = m.cacx_ai_config(provider="evil", dry_run=True)
        check("provider 非法返回 error", "error" in r, str(r)[:200])

        # --- 闸门 2：dry_run=False + confirm=False 不写盘 ---
        saved_calls.clear()
        r = m.cacx_ai_config(model="should-not-save",
                             dry_run=False, confirm=False)
        check("confirm_required=True", r.get("confirm_required") is True,
              str(r)[:200])
        check("闸门2 未写盘", len(saved_calls) == 0, f"saved={len(saved_calls)}")
        check("闸门2 override 未变", cfg["ai"]["model"] == before_model,
              cfg["ai"]["model"])

        # --- 闸门 3：dry_run=False + confirm=True 才写盘 ---
        saved_calls.clear()
        r = m.cacx_ai_config(model="saved-model",
                             dry_run=False, confirm=True)
        check("saved=True", r.get("saved") is True, str(r)[:200])
        check("写盘 1 次", len(saved_calls) == 1, f"saved={len(saved_calls)}")
        check("changed 含 model", "model" in (r.get("changed") or []),
              str(r.get("changed")))
        check("写盘内容 model 已更新",
              saved_calls[0]["ai"]["model"] == "saved-model",
              saved_calls[0]["ai"]["model"])
        check("has_key 仍 True", r.get("has_key") is True, str(r)[:200])
        check("key_masked != 明文", r.get("key_masked") != FAKE_KEY,
              str(r.get("key_masked")))

        # --- 密钥三态 ---
        # (a) api_key 留空 = 不修改
        saved_calls.clear()
        m.cacx_ai_config(api_key="", model="m2",
                         dry_run=False, confirm=True)
        check("留空不改 key", saved_calls[0]["ai"]["api_key"] == FAKE_KEY,
              saved_calls[0]["ai"]["api_key"])

        # (b) api_key 非空 = 覆盖
        NEW_KEY = "sk-NEWKEY-999988887777"
        saved_calls.clear()
        r = m.cacx_ai_config(api_key=NEW_KEY, dry_run=False, confirm=True)
        check("非空覆盖 key", saved_calls[0]["ai"]["api_key"] == NEW_KEY,
              saved_calls[0]["ai"]["api_key"])
        blob = json.dumps(r, ensure_ascii=False)
        check("覆盖后响应不含新明文 key", NEW_KEY not in blob, blob[:200])

        # (c) clear_key=True = 清空
        saved_calls.clear()
        r = m.cacx_ai_config(clear_key=True, dry_run=False, confirm=True)
        check("clear_key 清空", saved_calls[0]["ai"]["api_key"] == "",
              saved_calls[0]["ai"]["api_key"])
        check("清空后 has_key=False", r.get("has_key") is False, str(r)[:200])
    finally:
        C.save = orig_save
        m._CFG_OVERRIDE = None
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 9. 新词采纳闸
def test_newwords_adopt_gate():
    """
    cacx_newwords_adopt 的 confirm 闸。

    ⚠ 这条要单独测，不能只靠「工具总数」：总数对了不代表闸门有效。
       词典是累积资产 —— 写进去之后所有检索、词频、实体抽取都按它切分，
       误写**不会报错**，只会持续出偏差，而且极难归因到「词典多了个词」。
    """
    section("9. cacx_newwords_adopt（confirm 闸门）")
    import cacx.mcp_server as m
    from cacx import newwords as NW

    tmp = Path(tempfile.mkdtemp(prefix="cacx_mcp_nw_"))
    try:
        # 把 userdict 指向临时目录：⚠ 绝不碰真实的 data/userdict.txt
        d = tmp / "data"
        d.mkdir(parents=True, exist_ok=True)
        ud = d / "userdict.txt"
        m._userdict_path = lambda: str(ud)

        # --- 闸门 1：默认 confirm=False，只预览 ---
        r = m.cacx_newwords_adopt(["清朗专项行动", "深度伪造"])
        check("默认 dry_run", r.get("dry_run") is True, str(r)[:200])
        check("dry_run 不建文件", not ud.exists(), str(ud))
        check("dry_run 列出将写入", r.get("will_write") == ["清朗专项行动", "深度伪造"],
              str(r.get("will_write")))
        check("dry_run 给出下一步", "confirm=true" in (r.get("next_step") or ""),
              str(r.get("next_step")))

        # --- 闸门 2：confirm=True 才落盘 ---
        r = m.cacx_newwords_adopt(["清朗专项行动", "深度伪造"], confirm=True)
        check("confirm=True 已写入", r.get("added") == 2, str(r)[:200])
        check("文件已生成", ud.exists(), str(ud))
        check("落盘后可读回",
              set(NW.read_userdict(str(ud))) == {"清朗专项行动", "深度伪造"},
              str(NW.read_userdict(str(ud))))

        # --- 闸门 3：重复采纳不重复写 ---
        r = m.cacx_newwords_adopt(["清朗专项行动", "深度伪造"], confirm=True)
        check("重复采纳 added=0", r.get("added") == 0, str(r)[:200])
        check("词典没被撑大", r.get("total") == 2, str(r)[:200])

        # --- 闸门 4：review 一个字都不能落盘 ---
        before = set(NW.read_userdict(str(ud)))
        import cacx.ai.tasks as AI_tasks
        prov = type("P", (), {"model": "fake", "chat": staticmethod(
            lambda s, u, dry_run=False: '{"decisions":[{"word":"清朗专项行动",'
                                        '"verdict":"keep","reason":"x"}]}')})()
        orig = AI_tasks.build
        AI_tasks.build = lambda cfg, task=None: prov
        try:
            res = AI_tasks.review_new_words({"ai": {"provider": "openai_compat"}},
                                            [{"word": "清朗专项行动"}],
                                            dry_run=False)
            check("review 返回建议", res["decisions"]["清朗专项行动"]["verdict"] == "keep",
                  str(res["decisions"]))
        finally:
            AI_tasks.build = orig
        check("review 后词典未变", set(NW.read_userdict(str(ud))) == before,
              str(NW.read_userdict(str(ud))))

        # --- 闸门 5：空词表不炸 ---
        r = m.cacx_newwords_adopt([])
        check("空词表返回 error", "error" in r, str(r)[:120])
    finally:
        m._CFG_OVERRIDE = None
        if hasattr(m, "_userdict_path"):
            del m._userdict_path
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 10. summarize_batch
def test_summarize_batch():
    section("10. cacx_summarize_batch（增量过滤 / dry_run 预览）")
    import cacx.mcp_server as m
    from cacx import db as D
    from cacx.ai import tasks as AI_tasks

    tmp = Path(tempfile.mkdtemp(prefix="cacx_mcp_batch_"))
    cfg = _make_cfg(tmp)
    cfg["sites"]["cac"] = {"enabled": True, "name": "中国网信办",
                           "base_url": "https://www.cac.gov.cn"}
    m._CFG_OVERRIDE = cfg

    # 种 3 篇文章
    d = D.Database(tmp / "t.db")
    ids = []
    for i, title in enumerate(["文章一", "文章二", "文章三"]):
        aid = d.upsert_article({
            "site": "cac", "site_name": "中国网信办",
            "column_code": "yw", "column_name": "要闻",
            "title": title, "pub_date": f"2026-10-1{i}",
            "url": f"https://www.cac.gov/{i}.htm",
            # ⚠ 正文必须每篇唯一：upsert_article 有「同站同正文指纹」去重
            #   守卫（MIN_DEDUP_CHARS=100），三篇正文逐字相同会被判成一文
            #   多栏目发，第 2、3 篇返回 False，ids 里就混进了假值。
            #   这个用例要的是「三篇不同文章」，不是「三篇长得一样的文章」。
            "content_text": "正文 " * 200 + f"第{i}篇独有段落", "chars": 1000,
        })
        assert aid, f"第 {i} 篇入库被拒：{title}"  # 入库被守卫拦下要当场炸，
        # 而不是把 False 一路带到后面变成看不懂的 total=0
        ids.append(aid)
    # 给第一篇打 summarize annotation（模拟已摘要过）
    d.save_annotation(ids[0], "summarize", {"summary": "已有摘要"})
    d.close()

    # 打桩 ai.tasks.summarize，计数调用次数
    calls = []
    orig_summarize = AI_tasks.summarize
    def fake_summarize(cfg_, art, dry_run=False):
        calls.append(art.get("id"))
        return {"summary": "stub", "tags": []}
    AI_tasks.summarize = fake_summarize

    try:
        # mode=incremental：过滤掉第一篇（已有摘要），剩 2 篇
        r = m.cacx_summarize_batch(mode="incremental", dry_run=True)
        check("incremental total=2", r.get("total") == 2, str(r)[:200])
        check("incremental skipped=1", r.get("skipped") == 1, str(r)[:200])
        check("incremental dry_run=True", r.get("dry_run") is True,
              str(r)[:200])
        # dry_run 只预览第一篇：fake_summarize 只被调 1 次
        check("dry_run 只调 1 次 AI", len(calls) == 1, f"calls={calls}")

        # mode=full：3 篇全选
        calls.clear()
        r = m.cacx_summarize_batch(mode="full", dry_run=True)
        check("full total=3", r.get("total") == 3, str(r)[:200])

        # limit 生效：limit=1 → 每站最多 1 篇
        r = m.cacx_summarize_batch(mode="full", limit=1, dry_run=True)
        check("limit=1 total<=1", r.get("total") <= 1, str(r)[:200])

        # 密钥零回显
        blob = json.dumps(r, ensure_ascii=False)
        check("batch dry_run 响应不含明文 key", FAKE_KEY not in blob,
              blob[:200])
    finally:
        AI_tasks.summarize = orig_summarize
        m._CFG_OVERRIDE = None
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 10. 源码静态扫描
def test_source_scan():
    section("10. 源码静态扫描：任何输出路径不回显 api_key")
    src_path = Path(__file__).resolve().parent.parent / "cacx" / "mcp_server.py"
    src = src_path.read_text(encoding="utf-8")

    # 1) 模块里不允许出现裸 print（MCP 走 stdout JSON-RPC，print 会污染通道）
    check("源码无 print(", "print(" not in src.replace("    # print(", ""),
          "存在 print( 调用")

    # 2) api_key 只允许在「授权函数」里被引用：
    #    - _sanitize：剥密钥（防御性）
    #    - cacx_ai_status：只读 key 算 has_key / 掩码
    #    - cacx_ai_config：签名入参 + 写盘（C.set_path 目标）
    #    其他函数里出现 api_key 即视为「可能把明文塞进响应」。
    #    docstring / 注释行一律放行。
    ALLOWED = {"_sanitize", "cacx_ai_status", "cacx_ai_config"}
    bad_lines = []
    cur_func = ""
    in_docstring = False
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        in_docstring = (in_docstring ^ (stripped.count('"""') % 2 == 1))
        # 只在顶格 def（外层函数）时更新 cur_func；嵌套 def（如 add_change）
        # 不改变外层函数上下文，否则函数体内的 api_key 调用会被误判。
        if line.startswith("def "):
            cur_func = stripped.split("(")[0].replace("def ", "").strip()
        if "api_key" in line and not in_docstring and not stripped.startswith("#"):
            if cur_func not in ALLOWED:
                bad_lines.append((i, stripped))
    check("api_key 仅在授权函数(_sanitize/ai_status/ai_config)内被引用",
          not bad_lines, str(bad_lines))

    # 3) _sanitize 在所有 except 路径上都被调用过（_err 用了它）
    check("_err 调用 _sanitize", "str(text)" in src and "_sanitize" in src)


# ---------------------------------------------------------------- main
def main():
    print("=" * 62)
    print("cacx MCP server 离线测试")
    print("=" * 62)
    for fn in (test_import, test_tools_registered,
               test_offline_and_key_zero_echo,
               test_ai_status, test_ai_config, test_newwords_adopt_gate,
               test_summarize_batch, test_source_scan):
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
