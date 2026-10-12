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
    tokenize: bool = False,
    op: str = "AND",
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
        tokenize:     中文分词检索。**默认 False**。
                      置 True 时用 jieba 把查询切成词组，配合 op="OR"。
                      实测「清朗网络谣言」精确检索 0 篇，分词 OR 命中 2807 篇
                      （FTS 用 trigram 分词器，整串匹配经常落空）。
                      但 OR 会放大结果集：查「中央网信办 专项行动」这类
                      精确组合时**不要**开，OR 会捞进无关文章。
        op:           AND（默认，所有词都要有）/ OR（任一词命中即可）。
                      只在 tokenize=True 时有意义。

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
                limit=limit, offset=offset, min_fts_chars=min_fts,
                tokenize=tokenize, op=op)
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


# ============================================== 3. 分析工作台（AI 调用的入口）
# 这一组是给「AI 一键生成报告/图谱」用的。顺序刻意如此：
#   选样本 → 挖机构 → 抽实体关系 → 写库 → 出图
# 这样 AI 一次会话就能走完 R1→R2，不需要人去中间搬数据。
@mcp.tool()
def cacx_sample_options(date_from: str = "2024-01-01",
                        date_to: str = "2099-12-31") -> List[Dict[str, Any]]:
    """
    列出可配样本的栏目及各有多少篇（**第一步总是先调这个**）。

    老师要「任选一个栏目，300-500 篇，2024 年以来」，但库里栏目从
    14 篇到 1600 多篇都有，光「选栏目」凑不出 300-500。所以规则是
    **时间窗给足 + 篇数用 limit 截**，任何一个栏目都能配出合规样本。

    返回：[{site, column, n(区间内篇数), years(跨几年), d0, d1}, ...]
    """
    try:
        with _db() as d:
            return d.sample_options(date_from, date_to)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_sample(
    site: str = "",
    column: str = "",
    date_from: str = "2024-01-01",
    date_to: str = "2099-12-31",
    limit: int = 500,
    strategy: str = "stratified",
    with_text: bool = False,
) -> Dict[str, Any]:
    """
    抽一个**分析样本**（只读）。

    参数：
        site / column: 都可空；column 传栏目名（如「网信要闻」）。
        date_from / date_to: 闭区间。默认 2024 至今 —— **不要**收窄到
            2025 或更晚，老师要「2024 年以来」的全貌。
        limit: 篇数上限（1~5000）。
        strategy: stratified（默认，按年份分层，保证每年都被抽到）
                  / recent（取最近 N 篇）
            ⚠ **除非明确只要最新动态，否则别用 recent**：
            取最近 500 篇会只覆盖 2025-03 之后，**2024 整年丢失**，
            「发文量与主题随时间的变化」这条要求直接作废。
        with_text: 是否带上正文全文。默认 False（500 篇全文很大），
            需要读正文抽实体时再置 True，或按需用 cacx_get_article 分批取。

    返回：{n, n_usable, total_pool, years_taken:{年:篇数}, date_min/max,
          items:[{id,title,pub_day,url,column_name}]}
    """
    try:
        with _db() as d:
            r = d.sample_articles(site=site, column=column,
                                  date_from=date_from, date_to=date_to,
                                  limit=limit, strategy=strategy)
            if with_text and r["items"]:
                ids = [x["id"] for x in r["items"]]
                q = ("SELECT id, content_text FROM articles "
                     "WHERE id IN (%s)" % ",".join("?" * len(ids)))
                txt = {int(i): t for i, t in d.conn.execute(q, ids)}
                for x in r["items"]:
                    x["content_text"] = txt.get(x["id"], "")
            return r
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_mine_orgs(
    site: str = "",
    column: str = "",
    date_from: str = "2024-01-01",
    date_to: str = "2099-12-31",
    limit: int = 3000,
    min_count: int = 3,
    merge: bool = True,
) -> Dict[str, Any]:
    """
    从标题里挖掘**机构候选词**（只读），可选归并同一家机构。

    为什么需要它：articles 表**没有发文机关字段**，联合发文只能从
    title + content 抽。这一步给出「哪些串像机构名、出现多少次、
    归并后归到谁名下」，是关系抽取的输入清单。

    ⚠ 这是**辅助**，不是结论。它只保证候选「像机构」，
    真正判断「这是不是发文机关」仍要人复核 —— 别把输出直接当定论写进报告。

    ⚠ 已知的边界（实测出来的，别指望它全自动）：
      · 「APEC 峰会」「二十国集团峰会」这类**会议/峰会**会以「会」结尾被收进来，
        它们不是发文机关，做联合发文分析时要自己剔掉。
      · 「全国人大常委会国务院」这种**两个真机构在标题里连写**会被当成一个实体，
        归并后仍需人工拆开。
      · 「时政要闻」栏目多是领导人活动报道，噪声比「网信要闻」「治理监管」高得多；
        做机构网络分析优先选后两个栏目。

    参数：
        limit:      扫描多少篇标题（按时间倒序取最近 N 篇）
        min_count:  出现几次才算候选，默认 3
        merge:      True 时把「中央网信办网络局 / 举报中心 / ……公安部」
                    这类同一机构的多种写法归并成一个主名（推荐 True）

    返回：{scanned, raw_n, merged_n, orgs:[{org,count,aliases,variants,
          example_title}]}
    """
    try:
        from . import textseg as TS
        with _db() as d:
            where, args = ["pub_day>=?", "pub_day<=?",
                           "COALESCE(content_empty,0)=0"], [date_from, date_to]
            if site:
                where.append("site=?"); args.append(site)
            if column:
                where.append("column_name=?"); args.append(column)
            titles = [r[0] for r in d.conn.execute(
                f"SELECT title FROM articles WHERE {' AND '.join(where)} "
                f"ORDER BY pub_day DESC, id DESC LIMIT ?",
                args + [int(limit)])]
            raw = TS.mine_orgs(titles, min_count=min_count, topn=400)
            out = TS.normalize_orgs(raw) if merge else [
                {"org": x["org"], "count": x["count"],
                 "example_title": x["example_title"], "aliases": [],
                 "variants": [x["org"]]} for x in raw]
        return {"scanned": len(titles), "raw_n": len(raw),
                "merged_n": len(out), "merged": bool(merge), "orgs": out}
    except Exception as e:
        return _err(e)


# ------------------------------------------------ 实体 / 关系（写入）
@mcp.tool()
def cacx_put_entities(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    写入实体（**写操作**，作业 R1 的产物落库处）。

    rows 每项：
        name:       实体名，如「中央网信办」
        type:       org（机构）/ policy（政策）/ problem（治理问题）/ measure（措施）
        article_id: **出处文章的 id** —— 必填，不是可选项
        evidence:   支撑这一条的原文片段（推荐填）
        aliases:    其他写法，如 ["市场监管总局"]
        extractor:  谁抽的，默认 "ai"
        confidence: 0~1

    ⚠ 为什么 article_id 是硬要求：老师要求演示「点一条关系回溯原文」。
       没有出处的关系在图谱上就是无法验证的孤证，答辩时一问就塌。

    幂等：同一篇的同一实体重复提交只更新证据，不产生重复行，
          所以 AI 重跑一遍不会把图谱撑成双倍。

    返回：{written, skipped, errors[], error_count}
    """
    try:
        with _db() as d:
            return d.upsert_entities(rows)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_put_relations(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    写入关系（**写操作**，作业 R2 的产物落库处）。

    rows 每项：
        src_name:   起点实体名
        dst_name:   终点实体名
        rel_type:   co_issue（联合发文）/ regulate（监管）/ cite（引用）
                    / belong（隶属）/ implement（落实）……
        article_id: **出处文章 id** —— 必填
        evidence:   支撑这句话的原文片段
        weight:     权重，默认 1.0

    同名对 + 同类型 + 同一篇文章 = 同一行，重复提交 weight **累加**。
    但跨文章的证据**全部保留** —— 合作网络的强弱正是这么统计出来的。

    返回：{written, skipped, errors[], error_count}
    """
    try:
        with _db() as d:
            return d.upsert_relations(rows)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_list_entities(type: str = "", q: str = "",
                       article_id: Optional[int] = None,
                       limit: int = 200) -> List[Dict[str, Any]]:
    """
    实体清单（只读）。type 为 org/policy/problem/measure，空=全部。
    每条都带 article_title 与 url —— 供前端「点实体回溯原文」。
    """
    try:
        with _db() as d:
            return d.list_entities(type=type, q=q, article_id=article_id,
                                   limit=limit)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_list_relations(name: str = "", rel_type: str = "",
                        article_id: Optional[int] = None,
                        limit: int = 200) -> List[Dict[str, Any]]:
    """
    关系清单（只读）。name 非空时只看与该实体相连的关系（**含入边与出边**），
    即「在图谱上点开一个节点」的场景。
    """
    try:
        with _db() as d:
            return d.list_relations(name=name, rel_type=rel_type,
                                    article_id=article_id, limit=limit)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_graph(limit: int = 300) -> Dict[str, Any]:
    """
    关系图谱数据（只读）：{nodes:[{name,type,n}], edges:[{src,dst,rel_type,
    n(跨几篇), weight, article_ids[]}]}

    edges 里刻意带 article_ids —— 前端拿到后即可实现「点一条关系
    跳到支持它的原文」。这就是老师要的现场演示动作。
    """
    try:
        with _db() as d:
            return d.graph(limit=limit)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_graph_stats() -> Dict[str, Any]:
    """
    图谱概览（只读）：实体总数/机构数、关系条数/去重后边数、
    按类型分布、以及 **no_evidence**（没有原文证据的关系数）。

    no_evidence 是答辩风险指标 —— 别让它停在高位。
    """
    try:
        with _db() as d:
            return d.graph_stats()
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 分词实验室
def _userdict_path() -> str:
    from . import newwords as _NW
    return _NW.userdict_path(str(C.PROJECT_ROOT / "data"))


def _discover_nw(limit: int, max_chars: int, min_count: int,
                 topn: int) -> Dict[str, Any]:
    """discover 与 review 共用的取候选逻辑（只读）。

    ⚠ 已知词口径走 newwords.known_words()，与网页那条入口**同一份实现**。
      这里自己重写一遍的话，两边迟早漂移，而这种漂移用户看不见。
    """
    from . import newwords as NW
    with _db() as d:
        rows = d.conn.execute(
            "SELECT content_text FROM articles "
            "WHERE content_text IS NOT NULL AND length(content_text) > 200 "
            "LIMIT ?", (int(limit),)).fetchall()
    if not rows:
        return {"error": "语料里没有可用正文，先抓一些文章"}
    texts = [r[0][:int(max_chars)] for r in rows]
    known = NW.known_words(_userdict_path())
    cands = NW.discover_new_words(texts, min_count=min_count,
                                  topn=topn, known=known)
    for c in cands:
        c["hint"] = NW.domain_hint(c["word"]) or ""
    return {"articles": len(texts), "known_size": len(known), "items": cands}


@mcp.tool()
def cacx_newwords_discover(
    limit: int = 600,
    min_count: int = 8,
    topn: int = 60,
    max_chars: int = 5000,
) -> Dict[str, Any]:
    """
    从语料里挖新词候选（**只读**）。分词自学习的第一步。

    用无监督统计法：凝固度（该粘在一起的强度）+ 邻字熵（左右能接的字
    够不够杂）+ 子串可切分性（能切成两个已知词的就是短语，不是词），
    再按网信领域加权排序。

    ⚠ 返回的是**候选不是结论**：统计只保证「大概率是词」，
      保证不了「这个领域该收」。要写进词典必须人工确认
      （或先用 cacx_newwords_review 让 AI 过一遍，再自己复核）。

    参数：
        limit:     扫多少篇正文（默认 600，实测 800 篇时区分度最好）
        min_count: 候选至少出现几次，越大越保守
        topn:      返回多少个候选

    返回：{articles, known_size, items:[{word,count,cohesion,
          left_entropy,right_entropy,score,domain,hint}]}
    """
    try:
        return _discover_nw(limit, max_chars, min_count, topn)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_newwords_review(
    words: List[str],
    candidates: List[Dict[str, Any]] = [],
    run: bool = False,
    batch: int = 40,
) -> Dict[str, Any]:
    """
    让 AI 逐个判断候选词该不该收进词典（**只给建议，不写词典**）。

    ⚠⚠ 这个工具**一个字都不会写进词典**。采纳要用 cacx_newwords_adopt，
      而且那个还得再经你本人确认。理由：AI 只是概率，
      而用户词典会**持续污染**之后所有检索与统计，误判不可撤销。

    参数：
        words:      要评审的候选词（只传词名也能用，但带上 candidates
                    的统计指标更准 —— 模型能看到凝固度和邻字自由度）
        candidates: 可选，形如 discover 返回的 items（可直接整份传回）
        run:        False=只预览 prompt（不花钱），True=真调模型
        batch:      每次请求装多少个候选，省配额用

    返回：{dry_run, decisions:{词:{verdict:keep|drop|unsure, reason}},
          stats:{candidates,judged,keep,drop,unsure,missing,errors[]}}
    """
    from .ai.tasks import review_new_words
    try:
        cands = [c for c in (candidates or [])
                 if (c.get("word") or "").strip()]
        if not cands:
            cands = [{"word": w} for w in (words or []) if str(w).strip()]
        if not cands:
            return {"error": "没给候选词"}
        return review_new_words(_get_cfg(), cands, dry_run=not run,
                                batch=batch)
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_newwords_adopt(
    words: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """
    把词写进 jieba 用户词典（**写操作**，默认只预览）。

    ⚠ 为什么要 confirm 这道闸：词典是**累积资产**，写进去之后
      所有检索、词频统计、实体抽取都会按它切分。一次误写不会报错，
      但会持续污染结果，而且很难被发现是「词典里多了个词」导致的。
      所以先 dry_run 看清楚要写什么，confirm=true 才真的落盘。

    ⚠ 落盘是**追加**不覆盖：文件里可能有手工加过的词，不能被冲掉。
    ⚠ 写完立刻对当前进程生效，重启后仍在（启动时会自动加载）。

    参数：
        words:  要写入的词
        confirm: True 才真的写；False 只返回将要写入的内容

    返回（dry_run）：{dry_run:true, will_write:[…], current_total:N}
    返回（已执行）：{added:N, total:M, path:…}
    """
    try:
        from . import newwords as NW
        from . import textseg as TS
        ws = [str(w).strip() for w in (words or []) if str(w).strip()]
        if not ws:
            return {"error": "没给要采纳的词"}
        path = _userdict_path()
        cur = NW.read_userdict(path)
        new = [w for w in ws if w not in cur]
        if not confirm:
            return {"dry_run": True, "will_write": new,
                    "already_in_dict": [w for w in ws if w in cur],
                    "current_total": len(cur),
                    "next_step": "确认无误后把同样的 words 传回来并设 confirm=true。"}
        added = NW.write_userdict(path, new)
        for w in new:
            TS.add_word(w)
        return {"added": added, "total": len(NW.read_userdict(path)),
                "path": path, "applied_now": True}
    except Exception as e:
        return _err(e)


@mcp.tool()
def cacx_list_userdict() -> Dict[str, Any]:
    """
    列出已回流进分词词典的词（**只读**）。

    返回：{words:[…], count:N, path:…}
    """
    try:
        from . import newwords as NW
        p = _userdict_path()
        ws = NW.read_userdict(p)
        return {"words": ws, "count": len(ws), "path": p}
    except Exception as e:
        return _err(e)


# ============================================== 4. 删除（不可逆，排最后）
@mcp.tool()
def cacx_purge(
    target: str,
    site: str = "",
    site_like: str = "",
    column_name: str = "",
    year: str = "",
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    chars_below: int = 0,
    empty_content_only: bool = False,
    before_synced_at: str = "",
    ids: Optional[List[int]] = None,
    task: str = "",
    provider: str = "",
    result_like: str = "",
    created_before: str = "",
    fail_kind: str = "",
    is_own_site: Optional[bool] = None,
    confirm_token: str = "",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    批量删除本地数据（文章正文 / AI 摘要 / 失败条目登记）。

    ⚠⚠ 删除不可逆，**两道闸门都必须过**：

        闸门一 dry_run（默认 True）
            第一次调用保持默认，只返回影响面：命中多少条、按什么维度分布、
            样本长什么样、连带会删掉多少摘要，外加一个 confirm_token。
            此时**一个字节都没删**。

        闸门二 confirm_token
            真删必须把上一步的 confirm_token 原样传回来。
            token 由「条件 + 命中数」算出 —— 如果预览之后库变了
            （又同步了几篇、或者条件被改动），token 就对不上，执行会被拒绝。
            防止「我看的是 100 条的预览，实际删掉了 150 条」。

    正确用法（两次调用）：
        1. cacx_purge(target="annotations", site="zjwx", dry_run=True)
           → 读 matches / sample / warnings，把 confirm_token 念给用户听
        2. cacx_purge(target="annotations", site="zjwx",
                       confirm_token="<上一步的>", dry_run=False)

    参数：
        target: articles | annotations | external_links（**没有** meta /
                sync_runs —— 那是库的血缘，删了会破坏追溯）。
        其余均为筛选条件，至少给一个，否则拒绝。
        条件越精确允许删的量越大（只给 site 上限 200 条，
        给了年份/栏目/日期区间则到 2000 条）。

    返回：dry_run 时是影响面报告；真删时是
          {target, before, deleted:{articles,ai_annotations,events,...}, total}
    """
    try:
        from .purge import (PurgeRefused, _clean, execute_purge,
                           preview_purge)
        f = _clean({
            "site": site, "site_like": site_like, "column_name": column_name,
            "year": year, "date_from": date_from, "date_to": date_to, "q": q,
            "chars_below": chars_below, "empty_content_only": empty_content_only,
            "before_synced_at": before_synced_at, "ids": ids,
            "task": task, "provider": provider, "result_like": result_like,
            "created_before": created_before, "fail_kind": fail_kind,
            "is_own_site": is_own_site,
        })
        if not f:
            return {"refused": True,
                    "message": "没有给任何筛选条件。删除必须限定范围，"
                               "请至少给 site / date_from+date_to / task / "
                               "fail_kind / ids 之一。"}
        if not dry_run and not confirm_token:
            return {"refused": True,
                    "message": "缺少 confirm_token。请先用 dry_run=True 调一次"
                               "看影响面，把返回的 confirm_token 传回来再删。"
                               "（删除不可逆，这是设计上的二次确认闸门）"}
        with _db() as d:
            if dry_run:
                rep = preview_purge(d, target, **f)
                rep["dry_run"] = True
                rep["next_step"] = (
                    "确认无误后，把 confirm_token 与完全相同的条件原样传回，"
                    "并设 dry_run=false 执行删除。")
                return rep
            return execute_purge(d, target, confirm_token, **f)
    except PurgeRefused as e:
        return {"refused": True, "message": str(e)}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 入口
if __name__ == "__main__":
    mcp.run()
