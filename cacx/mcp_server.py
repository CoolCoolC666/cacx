# -*- coding: utf-8 -*-
"""
cacx MCP Server（stdio transport）
================================================================
把 cacx 的检索 / 采集 / AI 能力暴露成 MCP 工具，供任意 MCP 客户端
（Claude Desktop / Cursor / 任意 mcp-cli）直接调用。

设计要点
    · 只读工具在前、写/花钱工具在后（注册顺序即此约定）。
    · 写/花钱工具（sync / summarize）默认 dry_run=True，只预览不真跑，
      显式 dry_run=false 才真正联网 / 烧 AI 配额。
    · 数据库每次工具调用 open → 用完 close，不持有长连接 ——
      这样 Web 服务与 CLI 同时跑也不撞 SQLite 连接（详见 db.py 注释）。
    · **密钥零回显**：任何工具响应、异常文本里都不出现
      cfg["ai"]["api_key"] 明文；所有异常都过一遍 _sanitize。

MCP 客户端配置示例（对接文档 §5.4）：
    {
      "mcpServers": {
        "cacx": {
          "command": "python",
          "args": ["-m", "cacx.mcp_server"],
          "cwd": "E:\\\\Deskbox\\\\我的桌面\\\\...\\\\方向-爬取工具的更新"
        }
      }
    }
    （cwd 必须是项目根的绝对路径；Windows 反斜杠在 JSON 里要双写。）

运行：
    python -m cacx.mcp_server        # 直接以 stdio 模式启动
"""

from __future__ import annotations

import contextlib
import copy
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from . import config as C
from . import db as D
from . import sites as S

# ---------------------------------------------------------------- 服务器实例
mcp = MCPServer(
    "cacx",
    title="cacx 网信办语料库",
    description="中国网信办 / 浙江网信网文章采集、检索、分面统计与 AI 摘要",
)

# ---------------------------------------------------------------- 配置 / DB 辅助
# 测试可在模块层覆盖（不读写 settings.json）。生产路径走 C.load()。
_CFG_OVERRIDE: Optional[Dict[str, Any]] = None


def _get_cfg() -> Dict[str, Any]:
    """取配置。测试可赋 mcp_server._CFG_OVERRIDE 注入临时 cfg。"""
    return _CFG_OVERRIDE if _CFG_OVERRIDE is not None else C.load()


def _sanitize(text: Any) -> str:
    """
    把任意文本里可能出现的密钥明文剥掉。
    防御性：异常链、payload_preview、HTTP 错误体里都不该带 key。
    """
    s = str(text)
    try:
        key = (_get_cfg().get("ai") or {}).get("api_key") or ""
    except Exception:
        key = ""
    if key:
        s = s.replace(key, "***")
    # 再兜一道：Bearer <token> 形态
    import re
    s = re.sub(r"Bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***", s)
    return s


@contextlib.contextmanager
def _db():
    """每次调用独立开 / 关 DB，避免与 Web/CLI 长连接冲突。"""
    d = D.open_db(_get_cfg())
    try:
        yield d
    finally:
        with contextlib.suppress(Exception):
            d.close()


def _err(exc: BaseException) -> Dict[str, str]:
    """统一的错误返回：类型 + 已脱敏的消息。"""
    return {"error": f"{type(exc).__name__}: {_sanitize(exc)}"}


# ================================================================ 1. 检索（只读）
@mcp.tool()
def cacx_search(
    q: str = "",
    site: str = "",
    column_code: str = "",
    column_name: str = "",
    year: str = "",
    date_from: str = "",
    date_to: str = "",
    min_chars: Optional[int] = None,
    skip_empty: bool = True,
    sort: str = "date_desc",
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    组合检索文章（只读）。

    参数：
        q:            关键词；>=3 字走 FTS5，更短自动退回 LIKE。可多词空格分隔。
        site:         站点代码（如 cac / zjwx / 自定义 code），空=全部。
        column_code:  栏目代码精确过滤；column_name: 栏目名模糊匹配。
        year:         年份，如 "2025"。
        date_from / date_to:  pub_day 闭区间，YYYY-MM-DD。
        min_chars:    最小正文字数；skip_empty: 是否跳过空正文（默认 True）。
        sort:         date_desc / date_asc / chars_desc / chars_asc。
        limit / offset: 分页。

    返回：{total, mode(fts|like|none), took_ms, items:[{id,site,title,pub_date,url,chars,...}]}
    """
    try:
        cfg = _get_cfg()
        min_fts = int((cfg.get("search") or {}).get("min_fts_chars", 3))
        with _db() as d:
            return d.search(
                q=q, site=site, column_code=column_code, column_name=column_name,
                year=year, date_from=date_from, date_to=date_to,
                min_chars=min_chars, skip_empty=skip_empty, sort=sort,
                limit=limit, offset=offset, min_fts_chars=min_fts)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_get_article(article_id: int, with_html: bool = False) -> Dict[str, Any]:
    """
    按 id 取单篇文章全文（只读）。

    参数：
        article_id:  articles.id。
        with_html:   True 时连 content_html 一起返回（体量大，默认 False）。

    返回：{id,site,title,pub_date,url,content_text,chars,...}；不存在返回 {error}。
    """
    try:
        with _db() as d:
            art = d.get_article(int(article_id), with_html=with_html)
        if art is None:
            return {"error": f"article_id={article_id} 不存在"}
        return art
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_list_sites() -> List[Dict[str, Any]]:
    """
    列出全部站点（内置 + 自定义，只读）。

    返回：[{code, name, kind, enabled, builtin, broken, base_url}]
    """
    try:
        cfg = _get_cfg()
        out = []
        for code, s in S.all_sites(cfg).items():
            out.append({
                "code": code,
                "name": s.get("name") or code,
                "kind": s.get("kind", ""),
                "enabled": bool(s.get("enabled", True)),
                "builtin": bool(s.get("builtin")),
                "broken": bool(s.get("_broken")),
                "base_url": s.get("base_url", ""),
            })
        out.sort(key=lambda x: (not x["builtin"], x["name"]))
        return out
    except Exception as e:
        return [_err(e)]


@mcp.tool()
def cacx_facets(site: str = "") -> Dict[str, Any]:
    """
    分面统计（只读）：站点 / 栏目 / 年份分布。

    参数：
        site:  非空时只返回该站的栏目（下拉联动用）；空=全部。

    返回：{total, chars, days, empty, external, sites[], columns[], years[]}
    """
    try:
        with _db() as d:
            return d.facets_by_site(site=site)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_stats() -> Dict[str, Any]:
    """
    全库总体统计（只读）= cacx_facets(site="") 的别名，供客户端直观发现。
    """
    try:
        with _db() as d:
            return d.facets()
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_list_links(
    kind: str = "",
    site: str = "",
    limit: int = 40,
) -> Dict[str, Any]:
    """
    列出被判定为「非文章」而跳过的条目（专题页 / 站外链接 / 公众号 / 脏数据，只读）。

    参数：
        kind:  index_page / external / wechat / dirty；空=全部。
        site:  限定站点代码。
        limit: 最多返回行数。

    返回：{rows:[{site,title,pub_date,column_name,list_url,target_host,
                 is_own_site,fail_kind,reason}], n}
    """
    try:
        sql = ("SELECT site,title,pub_date,column_name,list_url,target_host,"
               "is_own_site,fail_kind,reason FROM external_links WHERE 1=1")
        args_: List[Any] = []
        if kind:
            sql += " AND fail_kind=?"; args_.append(kind)
        if site:
            sql += " AND site=?"; args_.append(site)
        sql += " ORDER BY id DESC LIMIT ?"; args_.append(max(1, int(limit)))
        with _db() as d:
            with d._lock:
                rows = [dict(r) for r in d.conn.execute(sql, args_)]
        return {"rows": rows, "n": len(rows)}
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_ai_status() -> Dict[str, Any]:
    """
    AI 配置概览（只读）。

    返回 AI 供应商 / 模型 / 就绪状态等摘要。密钥**绝不回显明文**：
    只给 has_key(bool) 与 key_masked（C.mask_secret 掩码，只露末 4 位）。
    不复用 ai.base.status 的 api_key 字段（其 _mask 对短 key 会返回
    「（未设置）」占位串，且字段名/口径与本工具约定不同），这里自己组装。

    返回：{enabled, provider, model, base_url, ready,
           has_key, key_masked, timeout, max_tokens, tasks}
    """
    try:
        from .ai import base as AIbase
        cfg = _get_cfg()
        ai = cfg.get("ai") or {}
        st = AIbase.status(cfg)
        # key 只用于算 has_key / 掩码，绝不把原值塞进返回
        key = ai.get("api_key") or ""
        return {
            "enabled": bool(ai.get("enabled", False)),
            "provider": ai.get("provider", "none"),
            "model": ai.get("model", ""),
            "base_url": ai.get("base_url", ""),
            "ready": bool(st.get("ready")),
            "has_key": bool(key),
            "key_masked": C.mask_secret(key),
            "timeout": int(ai.get("timeout", 60)),
            "max_tokens": int(ai.get("max_tokens", 2000)),
            "tasks": ai.get("tasks", {}),
        }
    except Exception as e:
        return _err(e)


# ================================================================ 2. 写 / 花钱（在后）
@mcp.tool()
def cacx_sync(
    dry_run: bool = True,
    sites: Optional[List[str]] = None,
    mode: str = "incremental",
    limit: Optional[int] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
) -> Dict[str, Any]:
    """
    跑一次采集同步（**默认只盘点不抓正文**）。

    ⚠ 安全：
        · dry_run=True（默认）→ 实际 mode 强制为 "list-only"，
          只列表盘点增量，不抓正文、不写库。
        · 只有显式 dry_run=false 才真正联网抓取；此时 mode 取 "incremental"
          （默认，只补新）或 "full"（全量重抓）。

    参数：
        dry_run:     True=只列出「库里没有哪些」；False=真抓。
        sites:       指定站点 code 列表；None=全部启用站点。
        mode:        incremental / full（仅 dry_run=False 时生效）。
        limit:       单次最多处理条数。
        date_start / date_end:  YYYY-MM-DD，覆盖配置里的时间窗。

    返回：{dry_run, mode, events:[...], totals:{listed,new,failed,skipped}}
    """
    try:
        from .collector import runner as R
        cfg = _get_cfg()
        events: List[Dict[str, Any]] = []

        if dry_run:
            run_mode = "list-only"
        else:
            run_mode = mode if mode in ("incremental", "full") else "incremental"

        R.sync(
            cfg,
            sites=sites,
            mode=run_mode,
            limit=limit,
            verbose=False,
            date_start=date_start,
            date_end=date_end,
            on_progress=lambda ev: events.append(ev or {}),
        )
        totals = {"listed": 0, "new": 0, "failed": 0, "skipped": 0}
        for ev in events:
            if ev.get("kind") == "done":
                for k in ("listed", "new", "failed", "skipped"):
                    totals[k] = ev.get(k, totals[k])
        return {"dry_run": bool(dry_run), "mode": run_mode,
                "totals": totals, "events": events}
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_summarize(article_id: int, dry_run: bool = True) -> Dict[str, Any]:
    """
    给一篇文章生成摘要 + 主题标签（**默认只预览不真调 AI**）。

    ⚠ 安全：
        · dry_run=True（默认）→ 不发任何网络请求，返回「将要发给模型的
          payload 预览」，可用来核对配置是否正确。
        · 只有显式 dry_run=false 才真的调用 AI（烧 MiniMax 配额）。

    参数：
        article_id:  articles.id。
        dry_run:     True=预览；False=真调 AI。

    返回：{article_id, title, dry_run, result:{summary,tags,sentiment,entities}
           或预览 payload}
    """
    try:
        from .ai import tasks as AI
        with _db() as d:
            art = d.get_article(int(article_id), with_html=False)
        if art is None:
            return {"error": f"article_id={article_id} 不存在"}
        cfg = _get_cfg()
        result = AI.summarize(cfg, art, dry_run=bool(dry_run))
        return {
            "article_id": art.get("id"),
            "title": art.get("title"),
            "dry_run": bool(dry_run),
            "result": result,
        }
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_summarize_batch(
    sites: Optional[List[str]] = None,
    date_from: str = "",
    date_to: str = "",
    column: str = "",
    mode: str = "incremental",
    limit: int = 10,
    concurrency: int = 3,
    rate: float = 0.0,
    resume: bool = False,
    stop_on_error: bool = False,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    批量给文章生成摘要（**默认只预览不真调 AI**，烧配额工具）。

    ⚠ mode 与 resume 的关系（与 web/CLI 语义一致）：
        · mode="incremental"（默认）：选文章阶段就在主线程过滤掉已有
          summarize annotation 的文章（d.get_annotation 判断）——这些文章
          根本不进批次，total 里就不含它们，skipped 计数含之。
        · mode="full"：选文章阶段不过滤，全部符合条件的文章都进批次；
          是否跳过已有摘要交给 run_batch 内部的 resume 参数决定。
        · resume=True 是 run_batch 自己的「断点续跑」开关（提交线程池前
          在主线程再筛一遍 annotation）。incremental 下它是双保险；
          full 下它才是主过滤器。

    ⚠ 线程模型（run_batch 自己管）：选文章 / 增量过滤都在本工具的主线程
      串行做（本工具是同步 def，天然主线程）；run_batch 内部才开线程池
      并发 AI 网络调用，落库仍回主线程串行。本工具不要再开线程。

    参数：
        sites:       指定站点 code 列表；None=全部启用站点。
        date_from/date_to: pub_day 闭区间 YYYY-MM-DD。
        column:      栏目名模糊匹配。
        mode:        incremental（默认）/ full。
        limit:       每站最多选多少条（合并去重后不二次截断）。
        concurrency: AI 并发数。
        rate:        每秒最多 AI 调用次数（0=不限速）。
        resume:      断点续跑（跳过已有摘要）。
        stop_on_error: 遇到首个错误就停。
        dry_run:     True（默认）只预览第一篇，不真调 AI、不写库。

    返回：{dry_run, mode, total, ok, fail, skipped, failures, 可选 preview}
    """
    try:
        from .ai.batch import run_batch
        cfg = _get_cfg()
        mode = mode if mode in ("incremental", "full") else "incremental"
        sites_list = list(sites) if sites else list(S.enabled_codes(cfg))

        with _db() as d:
            # 逐站 search 合并去重（db.search 只支持单 site）——照 web
            # _select_batch_articles：每站各取 limit 条，合并后不二次截断。
            seen, merged_ids = set(), []
            for s in sites_list:
                r = d.search(
                    q="", site=s, column_name=column,
                    date_from=date_from, date_to=date_to,
                    sort="date_desc", limit=int(limit),
                    min_fts_chars=int((cfg.get("search") or {}).get("min_fts_chars", 3)))
                for it in r.get("items") or []:
                    if it["id"] in seen:
                        continue
                    seen.add(it["id"])
                    merged_ids.append(it["id"])

            # incremental：主线程过滤掉已有摘要的文章（见 run_batch 线程模型注释）
            pre_filtered = 0
            picked_ids = []
            for aid in merged_ids:
                if mode == "incremental" and \
                        d.get_annotation(aid, "summarize") is not None:
                    pre_filtered += 1
                    continue
                picked_ids.append(aid)

            arts = [a for a in (d.get_article(aid) for aid in picked_ids)
                    if a is not None]

            summary = run_batch(
                cfg, d, arts,
                dry_run=bool(dry_run),
                concurrency=int(concurrency),
                rate=float(rate),
                resume=bool(resume),
                stop_on_error=bool(stop_on_error))

        out = dict(summary)
        out["mode"] = mode
        # 选择阶段过滤掉的并入 skipped，保证 total + skipped ≈ 候选数
        out["skipped"] = out.get("skipped", 0) + pre_filtered
        return out
    except Exception as e:
        return _err(e)


# 写盘前必须读磁盘原文的原因（docs/工程留痕.md §9 两次洗 key 事故）：
#   历史上有两次自检/批量任务把用户真实 MiniMax key 洗空——根因都是拿
#   「服务启动时的内存快照」改完就 save()，而快照是在用户后来通过 Web
#   填 key 之前拍的，save 一写就把 Web 刚填的 key 覆盖回了旧的空值。
#   所以任何写盘动作都必须当场重读磁盘原文，再在原文上逐字段改。
#   测试通过 monkeypatch _load_cfg_fresh 注入临时 cfg，绝不真碰 settings.json。
def _load_cfg_fresh() -> Dict[str, Any]:
    """写盘用：重读磁盘原文。生产走 C.load()；测试可覆盖本函数。"""
    if _CFG_OVERRIDE is not None:
        return copy.deepcopy(_CFG_OVERRIDE)
    return C.load()


@mcp.tool()
def cacx_ai_config(
    provider: str = "",
    base_url: str = "",
    model: str = "",
    api_key: str = "",
    timeout: Optional[int] = None,
    max_tokens: Optional[int] = None,
    enabled: Optional[bool] = None,
    clear_key: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
) -> Dict[str, Any]:
    """
    编辑 AI 配置（B 端写盘操作，**双闸门防误触**）。

    ⚠ 双闸门：
        · dry_run=True（默认）：只预览 would_change 对照，绝不写盘。
        · dry_run=False + confirm=False：仍不写盘，返回 confirm_required=True，
          提示「需要 confirm=true 才写盘」。
        · 只有 dry_run=False 且 confirm=True 才真写盘。

    ⚠ 密钥三态（照 web /api/config ConfigIn）：
        · api_key 留空（默认）= 不修改；
        · api_key 非空      = 覆盖；
        · clear_key=True    = 清空（先判 clear 再判非空——哨兵法有个真实的
          坑：万一哪天有合法 key 恰好以哨兵前缀开头，就会被无声清掉）。

    ⚠ 写盘前必做：C.load() 重读磁盘原文，再逐字段 set_path——见上面
      _load_cfg_fresh 的事故注释，绝不能拿内存快照直接 save。

    字段白名单：provider / base_url / model / api_key / timeout / max_tokens /
    enabled。provider 限 none / openai_compat / ollama。

    返回（预览）：{dry_run:true, would_change:[{field,old/new 或 old_masked/new_masked}], changes:n}
    返回（需确认）：{dry_run:false, confirm_required:true, ...}
    返回（已写盘）：{saved:true, changed:[字段名], has_key, key_masked}
    """
    try:
        P = "ai."
        # 字段 -> (dotted path, 是否密钥)
        changes: List[Dict[str, Any]] = []   # {field, path, old, new, secret}
        disk_cfg = _load_cfg_fresh()

        def add_change(field: str, path: str, new, secret: bool):
            old = C.get_path(disk_cfg, path)
            if old == new:
                return  # 没变，不入清单
            changes.append({"field": field, "path": path,
                            "old": old, "new": new, "secret": secret})

        if provider:
            if provider not in ("none", "openai_compat", "ollama"):
                return {"error": "provider 只能是 none / openai_compat / ollama"}
            add_change("provider", P + "provider", provider, False)
        if base_url:
            add_change("base_url", P + "base_url", base_url, False)
        if model:
            add_change("model", P + "model", model, False)
        # 密钥三态：先判 clear，再判非空（照 web ConfigIn 注释理由）
        if clear_key:
            add_change("api_key", P + "api_key", "", True)
        elif api_key:
            add_change("api_key", P + "api_key", api_key, True)
        if timeout is not None:
            add_change("timeout", P + "timeout", int(timeout), False)
        if max_tokens is not None:
            add_change("max_tokens", P + "max_tokens", int(max_tokens), False)
        if enabled is not None:
            add_change("enabled", P + "enabled", bool(enabled), False)

        # 构造 would_change 对照；密钥一律掩码，绝不回显明文
        would_change = []
        for ch in changes:
            if ch["secret"]:
                old_m = C.mask_secret(ch["old"] or "")
                new_m = C.mask_secret(ch["new"]) if ch["new"] else "(已清空)"
                would_change.append({"field": ch["field"],
                                     "old_masked": old_m,
                                     "new_masked": new_m})
            else:
                would_change.append({"field": ch["field"],
                                     "old": ch["old"], "new": ch["new"]})

        # 闸门 1：dry_run 默认开，只预览
        if dry_run:
            return {"dry_run": True, "confirm_required": False,
                    "would_change": would_change, "changes": len(changes)}

        # 闸门 2：confirm 默认关，仍不写盘
        if not confirm:
            return {"dry_run": False, "confirm_required": True,
                    "message": "需要 confirm=true 才写盘（双闸门防误触）",
                    "would_change": would_change, "changes": len(changes)}

        # 真写盘：在磁盘原文上逐字段改（_load_cfg_fresh 已重读）
        for ch in changes:
            C.set_path(disk_cfg, ch["path"], ch["new"])
        C.save(disk_cfg)

        ai = disk_cfg.get("ai") or {}
        key = ai.get("api_key") or ""
        return {"saved": True,
                "changed": [ch["field"] for ch in changes],
                "has_key": bool(key),
                "key_masked": C.mask_secret(key)}
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_doctor(
    site: Optional[List[str]] = None,
    use_ai: bool = False,
) -> Dict[str, Any]:
    """
    站点接口自检（规则层永远跑；AI 补充默认关闭）。

    参数：
        site:    指定站点 code 列表；None=全部启用站点。
        use_ai:  False（默认）只跑规则层（探接口、看选择器是否失效）；
                 True 且有失败站点时，再调一层 AI 诊断（烧配额）。

    返回：{reports:[{site,site_name,ok,checked_columns,issues,elapsed_ms}],
           data:{total,empty,external}, ai:{...}|null}
    """
    try:
        import time as _t
        from .collector import runner as R
        cfg = _get_cfg()
        codes = site or R.site_codes(cfg)
        reports: List[Dict[str, Any]] = []
        for s in codes:
            col = R.get_collector(cfg, s, verbose=False)
            if col is None:
                reports.append({"site": s, "ok": False,
                                "issues": ["未启用或配置损坏"]})
                continue
            t0 = _t.time()
            rep = col.health_check()
            rep["elapsed_ms"] = int((_t.time() - t0) * 1000)
            rep["site"] = s
            rep["site_name"] = col.name
            reports.append(rep)

        with _db() as d:
            f = d.facets()
            data = {"total": f.get("total", 0), "empty": f.get("empty", 0),
                    "external": f.get("external", 0)}
            ai_out = None
            if use_ai:
                from .ai import tasks as AI
                problems = [r for r in reports if not r.get("ok")]
                if problems:
                    ai_out = AI.diagnose(cfg, problems, d)
        return {"reports": reports, "data": data, "ai": ai_out}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 入口
if __name__ == "__main__":
    mcp.run()
