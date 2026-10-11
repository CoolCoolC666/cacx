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
