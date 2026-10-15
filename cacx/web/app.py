# -*- coding: utf-8 -*-
"""
FastAPI 检索服务
================================================================
    GET  /                     检索界面
    GET  /api/search           检索（关键词 + 多条件筛选 + 排序 + 分页）
    GET  /api/article/{id}     单篇详情（含 AI 摘要）
    GET  /api/facets           分面：站点/栏目/年份分布
    GET  /api/stats            总览
    GET  /api/recent           最近入库
    GET  /api/health           存活探针
    GET  /open?url=...          原文跳转中转（记录点击，可后续做传播分析）
    POST /api/ai/summarize     对单篇生成摘要（默认预览，不落库）

    GET  /api/export           把当前检索结果导出为 CSV / JSONL 下载
    GET  /api/trend            按月/按年分布（柱状图用）
    GET  /api/events           行为埋点统计（检索/跳转/导出）
    GET  /api/ai/status        AI 配置状态

    POST /api/purge/preview    批量删除：影响面预览（只读，返 confirm_token）
    POST /api/purge/execute    批量删除：真删（需 yes=true + 正确的 token）

    GET  /api/ai/failures      跨重启的 AI 失败清单（**实时对账**，只列仍无摘要的）
    POST /api/ai/failures/clear 清空失败留痕（默认只清已补上的；all=true 清全部）
    POST /api/ai/annotations/manual 人工补录摘要（模型被内容审查拦下时用）
    POST /api/ai/retry-failed  只重跑「可重试」的失败项，不重跑整批

约定
    · 只读接口一律不写库；写操作（AI 落库）需显式 ?commit=1
    · 原文跳转走本服务中转，方便记录「哪篇被检索后跳走了」
"""

import csv
import io
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel, Field

from .. import config as C
from .. import db as D
from .. import extract
from .. import graph_analysis as GA
from .. import newwords as NW
from .. import textseg
from ..ai import advisory as advisory_run
from ..ai import diagnose as ai_diagnose
from ..ai import summarize as ai_summarize
from ..ai import status as ai_status
from ..ai import THEME_LABELS
from ..ai.base import AIError
from ..ai.tasks import review_new_words as ai_review

# 用 pathlib 解析，不能用 __file__.rsplit("/") —— Windows 上是反斜杠，切不开
STATIC_DIR = Path(__file__).resolve().parent / "static"
CFG: Dict[str, Any] = {}
DB: Optional[D.Database] = None

app = FastAPI(title="cacx 网信办语料检索", version="0.1.0")


@app.on_event("startup")
def _startup():
    global CFG, DB
    CFG = C.load()
    DB = D.open_db(CFG)


@app.on_event("shutdown")
def _shutdown():
    if DB is not None:
        DB.close()


def _cfg() -> Dict[str, Any]:
    return CFG or C.load()


def _db() -> D.Database:
    global DB
    if DB is None:
        DB = D.open_db(_cfg())
    return DB


# ---------------------------------------------------------------- 检索
@app.get("/api/search")
def api_search(
    q: str = "",
    site: str = "",
    column: str = "",
    year: str = "",
    date_from: str = "",
    date_to: str = "",
    min_chars: Optional[int] = None,
    skip_empty: bool = True,
    only_empty: bool = False,
    sort: str = "date_desc",
    tokenize: bool = False,
    op: str = "AND",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    cfg = _cfg()
    page_size = cfg["app"].get("page_size", 20)
    d = _db()
    r = d.search(
        q=q, site=site, column_name=column, year=year,
        date_from=date_from, date_to=date_to, min_chars=min_chars,
        # only_empty 与 skip_empty 是互斥的两种视图，
        # 同时传以 only_empty 为准（否则占位视图会被自己的过滤器清空）。
        skip_empty=(False if only_empty else skip_empty),
        only_empty=only_empty,
        sort=sort,
        tokenize=tokenize, op=op,
        limit=size, offset=(page - 1) * size,
        min_fts_chars=cfg["search"]["min_fts_chars"])
    r["page"] = page
    r["size"] = size
    r["pages"] = (r["total"] + size - 1) // size if size else 0
    r["page_size_default"] = page_size
    # 命中片段 + 命中词，供列表高亮。埋点只记检索词，不记完整筛选条件
    # ——详情和 IP 这类信息没必要为了统计而留在库里。
    # ⚠ 高亮必须用「实际用于匹配的词」，不是原查询串：
    #   分词后命中的是「清朗」「网络谣言」，拿整串去高亮会一个都标不出来。
    d.annotate_snippets(r["items"], q, tokenize=tokenize)
    d.record_event("search", detail=q or "(浏览)")
    return r


@app.get("/api/export")
def api_export(
    q: str = "",
    site: str = "",
    column: str = "",
    year: str = "",
    date_from: str = "",
    date_to: str = "",
    min_chars: Optional[int] = None,
    sort: str = "date_desc",
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
    limit: int = Query(200, ge=1, le=2000),
    with_content: bool = True,
    with_ai: bool = False,
):
    """
    按当前检索条件导出并直接下载。

    限量 2000 条是刻意的：正文动辄数千字，一次导出上万条会把内存打满。
    要全量导出走 CLI（`cacx export --limit`），那边可以慢慢流式写文件。

    with_ai=true 时优先导出「已生成摘要的文章」——这是摘要页的导出入口，
    走的是 ai_annotations 而不是正文检索，因为用户此刻想拿的是摘要。
    """
    import csv as _csv
    import io

    cfg = _cfg()
    d = _db()

    if with_ai:
        ann = d.list_annotations(task="summarize", q=q, site=site, limit=limit)
        ids = [it["id"] for it in ann["items"]]
        rows = d.fetch_for_export(ids)
        # 把摘要并进对应行
        by_id = {it["id"]: it for it in ann["items"]}
        for r in rows:
            p = (by_id.get(r["id"]) or {}).get("parsed") or {}
            r["ai_summary"] = p.get("summary", "")
            r["ai_tags"] = "|".join(p.get("tags") or [])
            r["ai_sentiment"] = p.get("sentiment", "")
            r["ai_entities"] = "|".join(p.get("entities") or [])
        cols = ["id", "site", "site_name", "column_name", "column_code", "title",
                "pub_date", "url", "source_text", "chars",
                "ai_summary", "ai_tags", "ai_sentiment", "ai_entities"]
        if with_content:
            cols.append("content_text")
    else:
        r = d.search(
            q=q, site=site, column_name=column, year=year,
            date_from=date_from, date_to=date_to, min_chars=min_chars,
            sort=sort, limit=limit,
            min_fts_chars=cfg["search"]["min_fts_chars"])
        rows = d.fetch_for_export([it["id"] for it in r["items"]])
        cols = ["id", "site", "site_name", "column_name", "column_code", "title",
                "pub_date", "url", "source_text", "chars"]
        if with_content:
            cols.append("content_text")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    if format == "csv":
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for a in rows:
            w.writerow(a)
        # utf-8-sig：Excel 打开中文 CSV 不乱码
        data = buf.getvalue().encode("utf-8-sig")
        fname = f"cacx_{stamp}.csv"
    else:
        buf = io.StringIO()
        for a in rows:
            buf.write(json.dumps(
                {k: a.get(k) for k in cols if with_content or k != "content_text"},
                ensure_ascii=False) + "\n")
        data = buf.getvalue().encode("utf-8")
        fname = f"cacx_{stamp}.jsonl"

    d.record_event("export", detail=q or "(全部)",
                   meta=f"format={format} n={len(rows)}")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8" if format == "csv"
        else "application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(fname)}",
                 "X-Export-Count": str(len(rows))})


@app.get("/api/trend")
def api_trend(site: str = "", granularity: str = "month"):
    """按年或按月的文章数/字数分布，给柱状图用"""
    d = _db()
    where, args = [], []
    if site:
        where.append("site=?"); args.append(site)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    if granularity == "month":
        sql = ("SELECT substr(pub_day,1,7) k, COUNT(*) n, SUM(chars) c "
               "FROM articles" + w + " GROUP BY k ORDER BY k")
    else:
        sql = ("SELECT year k, COUNT(*) n, SUM(chars) c "
               "FROM articles" + w + " AND year<>'' GROUP BY k ORDER BY k"
               ) if w else ("SELECT year k, COUNT(*) n, SUM(chars) c "
                            "FROM articles WHERE year<>'' GROUP BY k ORDER BY k")
    with d._lock:
        rows = [dict(r) for r in d.conn.execute(sql, args)]
    return {"granularity": granularity, "items": rows}


@app.get("/api/facets")
def api_facets(site: str = "", date_from: str = "", date_to: str = ""):
    """
    分面。site 非空时只返回该站栏目 —— 栏目下拉靠它跟随站点联动。
    date_from/date_to 非空时只过滤 columns（栏目 Top12 随日期变），
    sites/total 等字段保持全量。空则不传，走全区间。
    """
    kw = {}
    if date_from: kw["date_from"] = date_from
    if date_to:   kw["date_to"] = date_to
    return _db().facets_by_site(site, **kw)


@app.get("/api/stats")
def api_stats():
    return _db().facets()


@app.get("/api/recent")
def api_recent(n: int = Query(10, ge=1, le=100)):
    return {"items": _db().recent_articles(n)}


@app.get("/api/health")
def api_health():
    try:
        f = _db().facets()
        return {"ok": True, "articles": f["total"], "ts": time.strftime("%F %T")}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


# ---------------------------------------------------------------- 详情
@app.get("/api/article/{aid}")
def api_article(aid: int, with_html: bool = False):
    d = _db()
    a = d.get_article(aid, with_html=with_html)
    if not a:
        raise HTTPException(404, "文章不存在")
    out = {k: v for k, v in a.items() if k != "content_html"}
    ann = {}
    for task in ("summarize", "tag"):
        r = d.get_annotation(aid, task)
        if r:
            try:
                ann[task] = json.loads(r["result"])
            except Exception:
                ann[task] = r["result"]
    out["ai"] = ann
    out["ai_status"] = ai_status(_cfg())
    return out


# ---------------------------------------------------------------- 原文跳转
@app.get("/open")
def api_open(url: str = "", id: int = 0):
    """
    原文跳转中转。
    以后要做「检索→阅读→传播」行为分析时，在这里埋点即可。
    """
    if not url and id:
        a = _db().get_article(id)
        if not a:
            raise HTTPException(404, "文章不存在")
        url = a.get("url", "")
    if not url:
        raise HTTPException(400, "缺少 url 或 id")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "仅允许 http/https")
    # 只记本地 id 和目标域名，不记完整 URL 查询串——那可能带追踪参数
    if id:
        _db().record_event("open", article_id=id, detail=_host(url))
    return RedirectResponse(url, status_code=307)


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc


@app.get("/api/events")
def api_events(days: int = Query(30, ge=1, le=365), site: str = ""):
    """行为统计。site 可按站点过滤。

    ⚠ search / export 两类事件**天然没有站点归属**：
    检索是跨站的，导出也可能一次导了两站的事件。
    所以按站点过滤时，这两类会被排除，而不是给出「半个口径」的数字 ——
    混合着报会让人以为「浙江的检索次数是 0」，实际只是它不可归属。
    响应里用 site_applicable 明说这件事。
    """
    return _db().event_stats(days, site=site)


# --------------------------------------------- 面板：三张作业相关的图
# 这三张服务的是「网信办角色画像与权责边界」这个报告主题，
# 与旁边那张「检索行为」（我们自己的操作记录）是两回事。
_PANEL_D0 = "2024-01-01"
_PANEL_D1 = "2099-12-31"
# 时间轴主题图显示几类。**必须是一个共享常量**：网页、CSV 导出、
# 快照导出都从这里取。早先快照端用了自己的 limit 参数当 top，
# 于是同一份数据网页出 6 类、导出出 10 类 —— 两张图并排贴 PPT 时，
# 「为什么这里比那里多几层」没人答得上来。
_TL_TOP = 6


def _timeline_impl(site: str = "", top: int = _TL_TOP,
                   date_from: str = "", date_to: str = "",
                   include_ai: bool = True,
                   months_n: int = 0) -> Dict[str, Any]:
    """
    时间轴的**内部实现**（路由、网页端与快照导出都调它）。

    ⚠ 必须抽成独立函数，不能让快照导出直接调路由函数：
      路由参数的默认值是 `Query(6, ...)` **参数对象**，不是数字。
      走 HTTP 时框架会替换掉，走内部调用时不会 ——
      int() 直接炸（TypeError: '>' not supported between 'Query' and 'int'）。
      这个坑在 /api/graph/analysis 上已经踩过一次。

    ⚠ 两张图共用**同一个月份集合**（以发文量的区间为准）：
      发文量按「所有文章」数，主题按「有正文的文章」数，
      两个分母本来就不一样（正文为空的列表页占位也被算进发文量）。
      各自按自己的月份出图的话，横轴长度不同，并排看就像数据打架。

    ⚠ 末月通常**不完整**（当月还没过完），画趋势时最后一点偏低，
      界面上必须标出来，否则会被当成"发文量在跌"。

    ⚠ SVG 在**服务端**生成并随 payload 一起返回。网页和快照导出
      调的是同一个函数、同一份参数，所以「屏幕 = 导出」是构造出来的，
      不靠两边各写一遍图然后祈祷它们一致 ——
      之前就是各写一遍，前端那份算错 mx 时单根柱子高 2208px。
    """
    d = _db()
    dflt_f, dflt_t = _PANEL_D0, _PANEL_D1
    top = max(1, min(10, int(top)))
    vol = d.article_monthly_trend(site=site,
                                  date_from=date_from or dflt_f,
                                  date_to=date_to or dflt_t)
    themes = d.problem_monthly(site=site,
                               date_from=date_from or dflt_f,
                               date_to=date_to or dflt_t, top=top)
    months = [r["month"] for r in vol]
    # _sites 只挂在 article_monthly_trend 的第一行上，**必须先取出来**：
    # 下面按尾部 N 个月切片之后，行首已经不是原来那一行了。
    sites = (vol[0].get("_sites") if vol else []) or []
    tmap = {r["month"]: r for r in themes}
    kept = (themes[0].get("_kept") if themes else []) or []
    dropped = (themes[0].get("_dropped") if themes else []) or []
    trows = []
    for m in months:
        t = tmap.get(m)
        trows.append({"month": m, "total": (t or {}).get("total", 0),
                      "problems": (t or {}).get("problems",
                                                 {k: 0 for k in kept})})
    # ---- AI 口径（同一批月份，横轴对齐）----
    ai = None
    if include_ai:
        arows = d.ai_theme_monthly(months=months, site=site,
                                   date_from=date_from or dflt_f,
                                   date_to=date_to or dflt_t)
        totals = (arows[0].get("_totals") if arows else {}) or {}
        top_ai = [k for k, _ in sorted(totals.items(),
                                       key=lambda kv: -kv[1])[:top]]
        amap = {r["month"]: r for r in arows}
        ai = {
            "rows": [{"month": m,
                      "labeled": (amap.get(m) or {}).get("labeled", 0),
                      "themes": {k: (amap.get(m) or {}).get("themes", {}).get(k, 0)
                                 for k in top_ai}}
                     for m in months],
            "kept": top_ai,
            "coverage": d.theme_coverage(site=site,
                                         date_from=date_from or dflt_f,
                                         date_to=date_to or dflt_t),
        }
    last_month = months[-1] if months else ""
    # ---- 只截**尾部** N 个月，且**在服务端切** ----
    #   前端再自己 slice 一次的话，两边口径就对不上了
    #   （网页的月份下拉是面板全局筛选，时间轴必须跟着它走）。
    if months_n and len(months) > months_n:
        keep = set(months[-months_n:])
        vol = [r for r in vol if r["month"] in keep]
        trows = [r for r in trows if r["month"] in keep]
        if ai:
            ai["rows"] = [r for r in ai["rows"] if r["month"] in keep]
        months = [r["month"] for r in vol]
        last_month = months[-1] if months else ""
    # 末月到底过没过完：**只有末月就是当前自然月时才算没过完**。
    # ⚠ 不能写死 True —— 加了日期范围筛选后，date_to 能选到**历史区间**，
    #   那时末月是个完整的月（例：选 2025 全年，末月 2025-12 已过完）。
    #   这时候还标「末月未过完」是**反向误导**：读者会以为末月偏低是因为
    #   没采集完，其实它是完整的一个月 —— 恰好掩盖掉真实的跌幅。
    #   默认区间下末月恒为当月，写死恰好总是对的，所以这个坑此前露不出来。
    last_partial = bool(last_month) and last_month == date.today().strftime("%Y-%m")

    # ---- 出图（服务端）----
    # 发文量按站点堆叠；没有站点维度时退化成单序列「发文量」
    vkeys = list(sites) or ["发文量"]
    vrows = [{"month": r["month"],
              "problems": ({s: r.get(s, 0) for s in vkeys} if sites
                           else {"发文量": r["total"]})}
             for r in vol]
    vpal = _tl_palette(vkeys)
    tpal = _tl_palette(kept)
    apal = _tl_palette(ai["kept"] if ai else [])
    charts = {
        "volume": _svg_stack(vrows, vkeys, vpal, unit=" 篇",
                             partial_last=last_partial)
                   + _tl_legend(vkeys, vpal),
        "theme_kw": (_svg_stack(trows, kept, tpal, unit=" 次",
                                partial_last=last_partial) + _tl_legend(kept, tpal)
                     if kept else '<div class="empty">该筛选下无主题数据</div>'),
        "theme_ai": ((_svg_stack([{"month": r["month"], "problems": r["themes"]}
                                 for r in ai["rows"]],
                                ai["kept"], apal, unit=" 次",
                                partial_last=last_partial)
                      + _tl_legend(ai["kept"], apal))
                     if ai and ai["kept"]
                     else '<div class="empty">还没有 AI 主题标注。<br>'
                          '点「AI 标主题」生成 —— 会真的调用模型烧配额，'
                          '先看预览再决定。</div>'),
    }
    return {
        "site": site,
        "months": vol,
        "themes": trows,
        "ai": ai,
        "charts": charts,
        "sites": vkeys,
        "palette": {**vpal, **tpal, **apal},
        "kept": kept,
        "dropped": dropped,
        "last_month": last_month,
        # 口径回显（P7 立下的规矩：任何数字型选择都要说清分母）
        "scope": {
            "range": f"{date_from or dflt_f} ~ {date_to or dflt_t}",
            "months": len(months),
            "articles_total": sum(r["total"] for r in vol),
            "articles_with_body": sum(r["total"] for r in trows),
            "site": site or "两站合计",
            "last_month_partial": last_partial,
        },
        "note": ("发文量按所有文章计（含正文为空的列表页占位），"
                 "主题分布按有正文的文章计，两个分母不同；"
                 "关键词主题为粗筛，一篇可命中多类，各月相加会超过该月文章数；"
                 "AI 主题只统计**已被 AI 标注过**的文章，覆盖率见 ai.coverage；"
                 + (f"末月 {last_month} 尚未过完，不可与整月直接比较。"
                    if last_partial else
                    f"末月 {last_month} 已过完（选的是历史区间），可与整月直接比较。")),
    }


class ThemeReq(BaseModel):
    sites: List[str] = []        # 空 = 全部启用站点
    date_from: str = ""
    date_to: str = ""
    limit: int = 20               # 本次最多处理多少篇（分批调用）
    per_call: int = 12            # 每次 LLM 调用装几篇（MiroFish 式批量）
    dry_run: bool = True          # 铁律：默认只预览，真跑要显式改


_AI_THEME_LOCK = threading.Lock()
_AI_THEME_STATE: Dict[str, Any] = {"running": False, "done": 0,
                                    "failed": 0, "dropped": 0, "log": []}


@app.post("/api/panel/ai-themes")
def api_panel_ai_themes(req: ThemeReq):
    """
    批量用 AI 给文章打主题标签（时间轴的 AI 口径数据源）。

    ⚠ dry_run 默认 True：误触按钮会真烧配额。
    ⚠ 只给**还没标过**的文章打（UNIQUE(article_id,task)），
      重复跑不会重复烧钱 —— 增量靠 SQL 保证，不靠记状态。
    ⚠ 批量策略参考 MiroFish：多个请求打包成一次 LLM 调用
      （成本约降一半、速度提升数倍），而不是逐篇调。
    """
    d = _db()
    dfrom = req.date_from or _PANEL_D0
    dto = req.date_to or _PANEL_D1
    one_site = req.sites[0] if len(req.sites) == 1 else ""
    cov = d.theme_coverage(site=one_site, date_from=dfrom, date_to=dto)
    todo = max(0, cov["total"] - cov["labeled"])
    if req.dry_run:
        take = min(int(req.limit), todo)
        per = max(1, int(req.per_call))
        return {"dry_run": True, "coverage": cov,
                "plan": f"将取 {take} 篇未标注文章，每批 {per} 篇，"
                        f"共约 {(take + per - 1) // per} 次调用",
                "labels": list(THEME_LABELS),
                "note": "这是预览，不会发任何请求。确认无误后把 dry_run 改为 false。"}

    if not _AI_THEME_LOCK.acquire(blocking=False):
        return {"error": "已有主题标注任务在跑", "state": dict(_AI_THEME_STATE)}
    try:
        cfg = _cfg()
        if AI.build(cfg, task="theme") is None:
            return {"error": "AI 未配置：请在设置里填好 API 后再试"}
        where, args = ["pub_day>=?", "pub_day<=?",
                       "COALESCE(content_empty,0)=0"], [dfrom, dto]
        if req.sites:
            where.append("site IN (%s)" % ",".join("?" * len(req.sites)))
            args += req.sites
        with d._lock:
            arts = [dict(r) for r in d.conn.execute(
                f"SELECT a.id, a.title, a.content_text, a.pub_day, a.site "
                f"FROM articles a WHERE {' AND '.join(where)} "
                f"AND a.id NOT IN (SELECT article_id FROM ai_annotations "
                f"WHERE task='theme') LIMIT ?", args + [int(req.limit)])]
        if not arts:
            return {"done": 0, "message": "没有待标注的文章（可能已全标完）",
                    "coverage": cov}
        _AI_THEME_STATE.update({"running": True, "done": 0, "failed": 0,
                                "dropped": 0, "log": []})
        done = failed = dropped = 0
        n = max(1, int(req.per_call))
        ids = {a["id"] for a in arts}
        for i in range(0, len(arts), n):
            chunk = arts[i:i + n]
            try:
                res = AI.theme_many(cfg, chunk, dry_run=False, per_call=n)
                rows = res.get("rows", []) if isinstance(res, dict) else []
                # ⚠ id 对齐必须验：模型可能回批内序号也可能回文章 id。
                #   对不上就**整批跳过并记日志**，绝不能按顺序硬塞 ——
                #   那样会把 A 篇的主题写到 B 篇头上，图看着完全正常。
                for k, row in enumerate(rows):
                    aid = row.get("id")
                    themes = [t for t in (row.get("themes") or []) if t]
                    if not themes:
                        continue
                    real = aid if aid in ids else None
                    if real is None and isinstance(aid, int) and 1 <= aid <= len(chunk):
                        real = chunk[aid - 1]["id"]     # 批内序号（1 起）
                    if real is None:
                        _AI_THEME_STATE["log"].append(
                            f"第{i//n+1}批 有 id 无法对齐，已跳过：{aid}")[:200]
                        continue
                    d.save_annotation(real, "theme",
                                      json.dumps(themes, ensure_ascii=False),
                                      model=str(cfg.get("model", "")))
                    done += 1
                dropped += res.get("dropped", 0) if isinstance(res, dict) else 0
            except Exception as e:      # 单批失败不拖垮整轮
                failed += len(chunk)
                _AI_THEME_STATE["log"].append(f"第{i//n+1}批失败: {e}"[:200])
            _AI_THEME_STATE.update({"done": done, "failed": failed,
                                    "dropped": dropped})
        return {"done": done, "failed": failed, "dropped": dropped,
                "asked": len(arts),
                "coverage": d.theme_coverage(site=one_site, date_from=dfrom,
                                              date_to=dto),
                "log": _AI_THEME_STATE["log"][:10]}
    finally:
        _AI_THEME_STATE["running"] = False
        if _AI_THEME_LOCK.locked():
            _AI_THEME_LOCK.release()


@app.get("/api/panel/timeline")
def api_panel_timeline(site: str = "", top: int = Query(_TL_TOP, ge=1, le=10),
                       date_from: str = "", date_to: str = "",
                       include_ai: bool = True,
                       months: int = Query(0, ge=0, le=120)):
    """时间轴 —— 对应老师要求的「发文量/主题随时间的变化」。

    months=0 表示不截断（给 CSV 导出用）；网页传面板的月份下拉值。
    """
    return _timeline_impl(site, int(top), date_from, date_to,
                          include_ai, int(months))


@app.get("/api/panel/problems")
def api_panel_problems(site: str = "", date_from: str = "", date_to: str = ""):
    """
    治理问题关键词分布 —— 直接对应老师要的「角色画像」。

    ⚠ 这是**关键词粗筛口径**，不是人工标注的分类。
    一个议题可能同时命中两类（既是网络暴力又涉及未成年人），
    所以各行相加会超过总数。报告里必须写明这一点。
    """
    kw = {}
    if date_from: kw["date_from"] = date_from
    if date_to:   kw["date_to"] = date_to
    return {"site": site, "items": _db().problem_distribution(site=site, **kw),
        "note": "关键词粗筛，一篇可命中多类，各行相加会超过总数；"
                "精确分类需按 R1 抽取结果人工复核"}


@app.get("/api/panel/orgs")
def api_panel_orgs(site: str = "", limit: int = Query(12, ge=1, le=50),
                   date_from: str = "", date_to: str = "",
                   mode: str = Query("coarse")):
    """
    高频机构 Top —— 直接对应「谁在发文中反复出现」。

    ⚠ 每条带 needs_review 标记：归并算法基于「前缀 / 包含 / 去噪骨架」，
    会把部分无关机构吸进同一组（尤其「XX会 / XX大会 / XX峰会」这类会议名，
    它们以机构后缀结尾但不是发文机关）。**带 ⚠ 的必须人工复核后再引用。**

    ⚠ T8 起两档口径（**切档只影响读取与统计展示，抽取库不动**）：
      · mode=coarse（默认）= 粗选：只走 org_like 粗筛，召回高，与历史行为一致；
      · mode=refine        = 精选：再过一道 `textseg.eventish_org` 通用形态闸门，
        活动/会议片段不进榜；响应**多一个 refine 字段**说明「滤掉了多少、
        哪些组因此缺省」。
    ⚠ coarse 响应**不加** refine 字段（前端用 `resp.refine` 是否存在判档），
      其余字段（items / review_count / note）两档同形，导出与快照零改动。
    """
    kw = {}
    if date_from: kw["date_from"] = date_from
    if date_to:   kw["date_to"] = date_to
    d = _db()
    if _refine_mode_on(mode):
        r = d.top_orgs_refine(site=site, limit=limit, **kw)
        items, rf = r["items"], r["refine"]
        miss = "、".join(f"{g['org']}（{g['count']}）"
                        for g in rf["filtered_groups"]) or "无"
        note = (f"精选模式：通用形态闸门已滤掉 {rf['filtered_total']} 个"
                f"活动/会议名候选（{rf['filtered_n']} 个候选串，"
                f"{len(rf['filtered_groups'])} 组因此缺省：{miss}）；"
                f"其余为机器挖掘的候选，非定论，带 ⚠ 的组需人工复核")
        rf = dict(rf)
        rf["note"] = note
        return {"site": site, "items": items,
                "review_count": sum(1 for i in items if i.get("needs_review")),
                "refine": rf, "note": note}
    items = d.top_orgs(site=site, limit=limit, **kw)
    return {"site": site, "items": items,
            "review_count": sum(1 for i in items if i.get("needs_review")),
            "note": "机器挖掘的候选，非定论；带 ⚠ 的组需人工复核"}


@app.get("/api/panel/joint")
def api_panel_joint(site: str = "", date_from: str = "", date_to: str = ""):
    """
    联合发文类文章月度趋势 —— 权责边界的直接证据。

    ⚠ 粗筛口径：只按标题出现「联合/印发/会同/联发/共同」判定，
    会有误召（正文提到但标题没有的会漏，标题顺带提及的会误召）。
    精确数字要看 R1 抽出来的「联合发文」关系。
    """
    kw = {}
    if date_from: kw["date_from"] = date_from
    if date_to:   kw["date_to"] = date_to
    return {"site": site, "items": _db().joint_issue_trend(site=site, **kw),
        "note": "按标题关键词粗筛，非精确统计；精确值以 R1 的联合发文关系为准"}


# ---------------------------------------------------------------- 图表数据导出
@app.get("/api/panel/export")
def api_panel_export(which: str, site: str = "",
                     granularity: str = "month", days: int = 30,
                     limit: int = Query(12, ge=1, le=200),
                     date_from: str = "", date_to: str = "",
                     mode: str = ""):
    """
    把面板上的图表数据导成 CSV。

    为什么单独开这个口而不是让前端拼：论文里要的是**能直接进表格/统计软件
    的数据**，不是截���图片。而「界面看到的数字」与「导出的数字」必须是
    同一份 —— 前端拼 CSV 就必然有「改了筛选条件忘了同步」的隐患。
    这里直接复用面板那几个接口的取数逻辑，口径天然一致。

    ⚠ date_from/date_to 是**屏幕时间口径的复刻**：前端 pn_from/pn_to 优先，
      都留空时由 pn_months 换算成 date_from（见 index.html 的 dlPanel）。
      不带日期参数的话，屏幕按「近 N 个月」筛、导出的却是全量数字 ——
      「屏幕=导出」这条铁律当场断掉。空则不传、走后端默认区间（T1 同款）。

    ⚠ T8：mode 与 /api/panel/orgs 同款（只有 which=orgs 吃它）。屏幕在精选档、
      导出却给粗选那份，「屏幕=导出」同样断 —— 所以导出必须接受这个参数。
    """
    which = (which or "").lower()
    d = _db()
    if which == "trend":
        src = api_trend(site=site, granularity=granularity)
        rows = [{"period": r["k"], "articles": r["n"],
                 "chars": r.get("c") or 0} for r in src["items"]]
        period_cn = "年份" if granularity == "year" else "月份"
        # 表头是中文、rows 的 key 是英文，两边对不上就会整列写空。
        # 所以这里存的是 (表头, key) 的显式映射，而不是 split 表头去猜。
        cols = [(period_cn, "period"), ("文章数", "articles"), ("字数", "chars")]
        title = f"{granularity}_trend"
    elif which == "columns":
        f = api_facets(site=site, date_from=date_from, date_to=date_to)
        # 筛选了站点就直接给满 limit —— 再除以站点数就成「单站只画 6 个」，
        # 那是「全部站点时两站平分名额」的逻辑，单站不该再分一次。
        per = limit if site else max(4, limit // max(1, len(f["sites"])))
        by_site: Dict[str, List[dict]] = {}
        for c in f["columns"]:
            by_site.setdefault(c["site"], []).append(c)
        rank = []
        for s in f["sites"]:
            tag = s["site_name"] or s["site"]
            for c in (by_site.get(s["site"]) or [])[:per]:
                rank.append({"site": s["site"], "site_name": tag,
                             "column": c["column_name"], "articles": c["n"]})
        rank.sort(key=lambda x: -x["articles"])
        rows = rank
        cols = [("站点", "site"), ("站点名", "site_name"),
                ("栏目", "column"), ("文章数", "articles")]
        title = "columns_top"
    elif which == "events":
        e = d.event_stats(days, site=site)
        rows = [{"kind": r["kind"], "count": r["n"]} for r in e["by_kind"]]
        cols = [("行为类型", "kind"), ("次数", "count")]
        title = f"events_{days}d"
    elif which == "problems":
        src = api_panel_problems(site=site, date_from=date_from,
                                 date_to=date_to)
        rows = [{"problem": r["problem"], "articles": r["articles"],
                 "pct": r["pct"]} for r in src["items"]]
        cols = [("治理问题", "problem"), ("文章数", "articles"),
                ("占比%", "pct")]
        title = "problem_distribution"
    elif which == "orgs":
        src = api_panel_orgs(site=site, limit=limit,
                             date_from=date_from, date_to=date_to,
                             mode=mode)
        rows = [{"org": r["org"], "count": r["count"],
                 "needs_review": "是" if r.get("needs_review") else "",
                 "aliases": "|".join(r.get("aliases") or []),
                 "basis": r.get("merge_basis", "")} for r in src["items"]]
        cols = [("机构", "org"), ("出现次数", "count"),
                ("需人工复核", "needs_review"), ("归并自", "aliases"),
                ("归并依据", "basis")]
        title = "top_orgs"
    elif which == "joint":
        src = api_panel_joint(site=site, date_from=date_from,
                              date_to=date_to)
        rows = [{"month": r["month"], "joint": r["joint"],
                 "total": r["total"], "pct": r["pct"]} for r in src["items"]]
        cols = [("月份", "month"), ("联合发文类", "joint"),
                ("当月总数", "total"), ("占比%", "pct")]
        title = "joint_issue_trend"
    else:
        raise HTTPException(400, {"message": f"不支持的图表: {which}",
                                  "refused": True})

    site_name = {"cac": "中国网信办", "zjwx": "浙江网信网"}.get(site, "全部站点")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    fname = f"cacx_{title}_{site_name}_{stamp}.csv"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for h, _ in cols])
    for r in rows:
        w.writerow([r.get(k, "") for _, k in cols])
    d.record_event("export", detail=f"panel:{which}",
                   meta=f"site={site} n={len(rows)}")
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(fname)}",
                 "X-Export-Count": str(len(rows))})


# ---------------------------------------------------------------- 面板快照 HTML
def _esc(s: Any) -> str:
    """HTML 转义。导出的文件要能在任何浏览器里打开，少一个转义都可能破版。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _nice_max(v: float) -> int:
    """
    把 Y 轴上限收成「好看的整数」，并且保证能被 4 整除。

    否则 140 篇的样本会画出 140 / 105 / 70 / 35 这种刻度 ——
    数学上没错，但一眼看上去像坏了，而且放大截图后更刺眼。
    导出物是要贴进 PPT 的，这种细节很显眼。
    """
    import math
    if v <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 4, 5, 8, 10):          # 全都能被 4 整除
        if v <= m * mag:
            return int(m * mag)
    return int(10 * mag)


def _svg_bars(rows: List[dict], unit: str = "", w: int = 900,
               h: int = 290, label_every: int = 1) -> str:
    """
    纯 SVG 柱状图。不引任何图表库 —— 导出的 HTML 必须双击即开、断网可用，
    一旦依赖外链 CDN，教室没网就只剩一个空白页。

    viewBox 宽度刻意贴近卡片实际渲染宽度（约 880px），这样 1:1 显示、
    字号不被缩小，投影时才看得清。
    """
    if not rows:
        return '<div class="empty">无数据</div>'
    pad_l, pad_b, pad_t, pad_r = 62, 54, 16, 18
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    mx = _nice_max(max((r["n"] for r in rows), default=0))
    step = plot_w / len(rows)
    bw = max(2.0, min(46.0, step * 0.66))
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" '
           f'preserveAspectRatio="xMidYMid meet">']
    for i in range(5):
        v = mx * i // 4
        y = pad_t + plot_h - plot_h * i / 4
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" '
                   f'class="grid"/>')
        out.append(f'<text x="{pad_l-10}" y="{y+4:.1f}" class="axis" '
                   f'text-anchor="end">{v:,}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{w-pad_r}" '
               f'y2="{pad_t+plot_h}" class="axisline"/>')
    for i, r in enumerate(rows):
        bh = max(2.0, r["n"] / mx * plot_h)
        x = pad_l + i * step + (step - bw) / 2
        y = pad_t + plot_h - bh
        out.append(
            f'<g class="bar"><title>{_esc(r["k"])}：{r["n"]:,}{_esc(unit)}</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
            f'rx="2"/></g>')
        if i % label_every == 0:
            cx = x + bw / 2
            out.append(f'<text x="{cx:.1f}" y="{pad_t+plot_h+16}" class="axis" '
                       f'text-anchor="end" transform="rotate(-42 {cx:.1f} '
                       f'{pad_t+plot_h+16})">{_esc(r["k"])}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_stack(rows: List[dict], keys: List[str],
               colors: Dict[str, str], w: int = 900, h: int = 320,
               unit: str = "", partial_last: bool = False) -> str:
    """
    纯 SVG 堆叠面积图 —— 时间轴两张图共用（发文量分站点 / 主题随时间）。

    ⚠ 堆的是**命中次数**不是互斥分类：一篇可命中多类，
      面积总和会超过发文量 —— 图例必须写明，否则
      「加起来怎么比发文量还多」答不上来。
    ⚠ 同样钳制：任一层高度不超过 plot_h。钳制在 mx 计算正确时是冗余的
      （mx ≥ 每个值），但它防的是「mx 被上游改错」这一类事故：
      SVG 越界不会报错，只会把图画到画布外，整页版面被撑坏。
      ⚠ 真正兜底的是下面 `_clip`：单独钳单层是**假的安全** ——
        两层各自顶满 plot_h 时，「base + 本层」会跑到 pad_t - plot_h
        （=-192px，画布外）。反向验证把 _nice_max 打桩成返回 1
        当场顶出这个洞。前两处 min() 只让降级后的形状还算可读，
        不承担正确性 —— 所以唯一的不变式测试挂在 _clip 上。
    """
    if not rows or not keys:
        return '<div class="empty">无数据</div>'
    pad_l, pad_b, pad_t, pad_r = 62, 92, 18, 18
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    n = len(rows)
    step = plot_w / max(1, n)
    mx = _nice_max(max((sum(r["problems"].get(k, 0) for k in keys)
                        for r in rows), default=0))
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" '
           f'preserveAspectRatio="xMidYMid meet">']
    for i in range(5):
        v = mx * i // 4
        y = pad_t + plot_h - plot_h * i / 4
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" '
                   f'y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-10}" y="{y+4:.1f}" class="axis" '
                   f'text-anchor="end">{v:,}{unit}</text>')
    base = [0.0] * n
    top_y = pad_t + plot_h          # 绘图区上沿：任何坐标都不许越过它

    def _clip(y: float) -> float:
        return min(top_y, max(pad_t, y))

    for sk in keys:
        col = colors.get(sk, "#8ab4f8")
        up, dn = [], []
        for i, r in enumerate(rows):
            # ⚠ 钳制要**钳三处**，只钳一层是假的：
            #   单层 ≤ plot_h、累计 base ≤ plot_h 都不代表
            #   「base + 本层高度」≤ plot_h —— 两层各自顶满时
            #   上沿会跑到 pad_t - plot_h（= -192px，画布外）。
            #   反向验证（把 _nice_max 打桩成返回 1）当场把它顶出来过。
            v = min(plot_h, r["problems"].get(sk, 0) / (mx or 1) * plot_h)
            x = pad_l + i * step + step / 2
            up.append((x, _clip(pad_t + plot_h - base[i] - v)))
            dn.append((x, _clip(pad_t + plot_h - base[i])))
            base[i] = min(plot_h, base[i] + v)
        up_s = " L".join(f"{x:.1f} {y:.1f}" for x, y in up)
        dn_s = " L".join(f"{x:.1f} {y:.1f}" for x, y in reversed(dn))
        out.append(f'<path d="M{up_s} L{dn_s} Z" fill="{col}" '
                   f'fill-opacity="0.72" stroke="{col}" stroke-width="1">'
                   f'<title>{_esc(sk)}</title></path>')
    for i in _tick_idx(n, max(1, n // 8)):
            x = pad_l + i * step + step / 2
            out.append(f'<text x="{x:.1f}" y="{pad_t+plot_h+18}" class="axis" '
                       f'text-anchor="middle">{_esc(rows[i]["month"])}</text>')
    if partial_last and n:
        # 末月还没过完 → 单独一条虚线竖标，否则最后一点天然偏低，
        # 会被读成「发文量在跌」（真跌和没过完长得一模一样）。
        x = pad_l + (n - 1) * step + step / 2
        out.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" '
                   f'y2="{pad_t+plot_h}" stroke="#8b93a3" stroke-width="1" '
                   f'stroke-dasharray="3 3"/>')
        out.append(f'<text x="{x-5:.1f}" y="{pad_t+11}" class="axis" '
                   f'text-anchor="end">末月未过完</text>')
    out.append("</svg>")
    return "".join(out)


# 时间轴配色：**按名字**固定，不按排名。
# 早期版本按 kept 顺序取色，结果换个筛选，主题的颜色就全变了 ——
# 同一张图里两次出现的「未成年人」颜色不同，读者会当成两个东西。
_TL_PALETTE = ("#5b8def", "#4caf7d", "#f2a33c", "#e0605f", "#9b7ede",
               "#39a0b8", "#d98cb3", "#8aa63c", "#c98a4b", "#6f8faf",
               "#b08050", "#5aa06a", "#a06a9b", "#7a8ba6", "#cf7b7b",
               "#7b9fd4", "#9cb85a", "#b08a7a", "#7fae8a", "#8e8ec4")


def _tl_color(key: str) -> str:
    """稳定取色：用 crc32 而不是内置 hash —— 后者按进程加盐，
    重启一次颜色全变，同一张导出图前后对不上。"""
    import zlib
    return _TL_PALETTE[zlib.crc32(key.encode("utf-8")) % len(_TL_PALETTE)]


def _tl_palette(keys: List[str]) -> Dict[str, str]:
    """**必须按名字取色，不能按排名取色**（enumerate）。

    按排名取色时，换个筛选让「未成年人」从第 3 名掉到第 1 名，
    它的颜色就变了 —— 同一份 PPT 里两次出现的同一主题两个颜色，
    读者会当成两个东西。
    """
    return {k: _tl_color(k) for k in keys}


def _tl_legend(keys: List[str], palette: Dict[str, str]) -> str:
    if not keys:
        return ""
    item = ('<span class="lg"><i style="background:%s"></i>%s</span>'
            % ("{c}", "{k}"))
    body = "".join(item.format(c=palette.get(k, "#9aa0a6"), k=_esc(k))
                   for k in keys)
    return ('<div class="legend">' + body + "</div>")


def _tick_idx(n: int, every: int) -> List[int]:
    """要标刻度的下标：等间隔 + **末尾**，但末尾离前一个太近就把前一个让掉。

    ⚠ 只按 `i % every == 0 or i == n-1` 画，末尾那个会跟前一个叠在一起 ——
      34 个月、每 4 个一格时，最后两个标签相距不到半个字宽，
      屏幕上就是「2026056-10」这种糊成一团的字符串。
    """
    if n <= 0:
        return []
    every = max(1, every)
    idxs = list(range(0, n, every))
    if idxs[-1] != n - 1:
        if (n - 1) - idxs[-1] < every / 2:
            idxs.pop()
        idxs.append(n - 1)
    return idxs


def _table(head: List[str], rows: List[List[Any]]) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
                 for r in rows)
    return (f'<details class="data"><summary>查看数据表（{len(rows)} 行）</summary>'
            f'<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'
            f"</details>")


_PANEL_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0e1117;color:#e6e9ef;
 font:15px/1.6 "Segoe UI","Microsoft YaHei","PingFang SC",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:34px 26px 60px}
h1{font-size:25px;margin:0 0 6px;letter-spacing:.5px}
.sub{color:#8b93a3;font-size:13.5px;margin-bottom:24px}
.sub b{color:#3b82f6;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));
 gap:18px}
.card{background:#151922;border:1px solid #232936;border-radius:12px;padding:18px 20px 14px}
.card h2{font-size:15.5px;margin:0 0 14px;color:#c2c8d4;font-weight:600}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:#1e2430;stroke-width:1}
.chart .axisline{stroke:#3a4150;stroke-width:1}
.chart .axis{fill:#8b93a3;font-size:12.5px;font-family:Consolas,monospace}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0 2px;
        font-size:11.5px;color:#8b93a3}
.legend .lg{display:inline-flex;align-items:center;gap:5px}
.legend .lg i{width:11px;height:11px;border-radius:2px;display:inline-block}
.chart .bar rect{fill:#3b82f6}
.chart .bar:hover rect{fill:#60a5fa}
.rank{display:flex;align-items:center;gap:10px;padding:7px 0;
 border-bottom:1px solid #1c222d}
.rank:last-child{border-bottom:none}
.rank .t{flex:1;font-size:13.5px}
.rank .v{font-family:Consolas,monospace;color:#3b82f6;font-size:14px}
.rank .bar{width:120px;height:6px;background:#1c222d;border-radius:3px;
 overflow:hidden;flex:none}
.rank .bar i{display:block;height:100%;background:#3b82f6}
.empty{color:#7d8698;padding:26px 0;text-align:center}
details.data{margin-top:12px}
details.data summary{cursor:pointer;color:#8b93a3;font-size:12.5px;user-select:none}
table{width:100%;border-collapse:collapse;margin-top:9px;font-size:12.5px}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid #1c222d}
th{color:#8b93a3;font-weight:600}
td{font-family:Consolas,monospace}
.note{margin-top:12px;color:#8b93a3;font-size:12.5px}
.foot{margin-top:30px;color:#6d7686;font-size:12px;
 line-height:1.9;border-top:1px solid #1c222d;padding-top:16px}
@media print{body{background:#fff;color:#111}.card{border-color:#ddd}
 details.data{display:none}}
"""


@app.get("/api/panel/snapshot")
def api_panel_snapshot(site: str = "", months: int = Query(36, ge=6, le=120),
                       days: int = Query(30, ge=1, le=365),
                       top: int = Query(12, ge=1, le=50),
                       date_from: str = "", date_to: str = "",
                       mode: str = ""):
    """
    把整个数据面板导出成一个**独立的 HTML 文件**。

    为什么要有这个：老师要的「可运行的展示网站」我们已经有了，
    但报告和作业里还需要一个**能直接交上去、能贴进文档、离线能开**的东西。
    这个 HTML 双击就开、不断网、不需要 Python、不需要装任何东西。

    刻意不引任何图表库（同 _svg_bars 的理由），
    所以它不是一个「简化版」，而是一份**自包含的静态快照**。

    ⚠ date_from/date_to 必须与屏幕同一套口径（前端 pn_from/pn_to 优先，
      都留空时由 pn_months 换算）—— 快照里那几块要跟屏幕上的数字对得上，
      否则「屏幕=导出」就只剩下标题相同。空则不传、走后端默认区间。

    ⚠ T8：mode 与 /api/panel/orgs 同款（只作用于「高频机构」那一块）。
      屏幕在精选档、快照却给粗选榜，等于交上去的榜和看到的是两份数。
    """
    d = _db()
    trend_m = api_trend(site=site, granularity="month")["items"][-months:]
    trend_y = api_trend(site=site, granularity="year")["items"]
    f = api_facets(site=site, date_from=date_from, date_to=date_to)
    e = d.event_stats(days, site=site)

    # 栏目榜：单站给满 top 名额，多站平分（与前端同一口径）
    per = top if site else max(4, top // max(1, len(f["sites"])))
    by_site: Dict[str, List[dict]] = {}
    for c in f["columns"]:
        by_site.setdefault(c["site"], []).append(c)
    rank: List[dict] = []
    for sk in (site and [site] or [x["site"] for x in f["sites"]]):
        tag = {"cac": "国", "zjwx": "浙"}.get(sk, str(sk)[:2])
        for c in (by_site.get(sk) or [])[:per]:
            rank.append({"k": f"{tag}·{c['column_name']}", "n": c["n"]})
    rank.sort(key=lambda x: -x["n"])
    mx = max((x["n"] for x in rank), default=0) or 1

    ev = [{"k": r["kind"], "n": r["n"]} for r in e["by_kind"]]
    name = {"cac": "中国网信办", "zjwx": "浙江网信网"}.get(site, "全部站点")
    now = time.strftime("%Y-%m-%d %H:%M")
    # 副标题必须写**真正生效的那段区间**：给了日期就报到日，
    # 没给才说「近 N 个月」。写了假区间比不写更糟 —— 交上去的数据
    # 会被当成另一个口径去读。
    if date_from or date_to:
        period = ("日期 <b>" + _esc(date_from or "不限") + "</b> ~ <b>"
                  + _esc(date_to or "不限") + "</b>")
    else:
        period = f"近 {months} 个月"

    def rank_html(items):
        if not items:
            return '<div class="empty">无数据</div>'
        return "".join(
            f'<div class="rank"><span class="t">{_esc(x["k"])}</span>'
            f'<span class="bar"><i style="width:'
            f'{max(2, round(x["n"]/mx*100))}%"></i></span>'
            f'<span class="v">{x["n"]:,}</span></div>' for x in items)

    ev_html = "".join(
        f'<div class="rank"><span class="t">{_esc(x["k"])}</span>'
        f'<span class="bar"><i style="width:'
        f'{max(2, round(x["n"]/max((y["n"] for y in ev), default=1)*100))}%">'
        f'</i></span><span class="v">{x["n"]:,} 次</span></div>' for x in ev)

    note = ""
    if site and e.get("note"):
        note = f'<div class="note">{_esc(e["note"])}</div>'

    # ---- 三张作业相关的图（与网页端同源，保证「看到的 = 交出去的」）----
    prob = api_panel_problems(site=site, date_from=date_from, date_to=date_to)
    orgg = api_panel_orgs(site=site, limit=top,
                          date_from=date_from, date_to=date_to, mode=mode)
    joint = api_panel_joint(site=site, date_from=date_from, date_to=date_to)

    prob_rows = [{"k": r["problem"], "n": r["articles"]} for r in prob["items"]]
    pmx = max((x["n"] for x in prob_rows), default=0) or 1
    prob_html = "".join(
        f'<div class="rank"><span class="t">{_esc(x["k"])}</span>'
        f'<span class="bar"><i style="width:{max(2, round(x["n"]/pmx*100))}%">'
        f'</i></span><span class="v">{x["n"]:,}</span></div>' for x in prob_rows
    ) or '<div class="empty">无数据</div>'

    org_rows = [{"k": r["org"], "n": r["count"],
                 "warn": r.get("needs_review")} for r in orgg["items"]]
    omx = max((x["n"] for x in org_rows), default=0) or 1
    org_html = "".join(
        f'<div class="rank"><span class="t">'
        f'{"⚠ " if x["warn"] else ""}{_esc(x["k"])}</span>'
        f'<span class="bar"><i style="width:{max(2, round(x["n"]/omx*100))}%">'
        f'</i></span><span class="v">{x["n"]:,}</span></div>' for x in org_rows
    ) or '<div class="empty">无数据</div>'

    # 联合发文画占比而不是绝对篇数：发文多的月份会淹没趋势
    jit = joint["items"]
    jmx = max((r["pct"] for r in jit), default=0) or 1
    joint_bars = _svg_bars(
        [{"k": r["month"], "n": round(r["pct"], 1)} for r in jit],
        " %", label_every=max(1, len(jit)//12)) if jit else \
        '<div class="empty">无数据</div>'

    # ---- 时间轴（PPT 明确要的「时间轴」）----
    # ⚠ 调的是 _timeline_impl 而不是路由函数，且**参数与网页端一致**
    #   （同一个 top、同一段月份），所以下面这三段 SVG 与页面上看到的
    #   逐字符相同 —— 「屏幕 = 导出」靠的是同一个函数，不是两处人工同步。
    tl = _timeline_impl(site=site, top=_TL_TOP,
                        date_from=_PANEL_D0, date_to=_PANEL_D1,
                        include_ai=True, months_n=months)
    tl_m = tl["months"]
    tl_t = tl["themes"]
    ai_cov = (tl.get("ai") or {}).get("coverage") or {"labeled": 0,
                                                       "total": 0, "pct": 0}
    tl_kw_rows = [[r["month"], r["total"]] + [r["problems"].get(k, 0)
                                              for k in tl["kept"]]
                  for r in tl_t]
    tl_ai_rows = [[r["month"], r["labeled"]] + [r["themes"].get(k, 0)
                                                for k in
                                                ((tl.get("ai") or {})
                                                 .get("kept") or [])]
                  for r in ((tl.get("ai") or {}).get("rows") or [])]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网信办公开语料 · 数据面板 · {_esc(name)}</title>
<style>{_PANEL_CSS}</style></head><body><div class="wrap">
<h1>网信办公开语料 · 数据面板</h1>
<div class="sub">站点 <b>{_esc(name)}</b> · {period} ·
检索行为近 {days} 天 · 生成于 {now}</div>
<div class="grid">

<div class="card"><h2>按月分布</h2>
{_svg_bars(trend_m, " 篇", label_every=max(1, len(trend_m)//12))}
{_table(["月份", "文章数", "字数"],
        [[r["k"], f'{r["n"]:,}', f'{(r.get("c") or 0):,}'] for r in trend_m])}
</div>

<div class="card"><h2>按年分布</h2>
{_svg_bars(trend_y, " 篇")}
{_table(["年份", "文章数", "字数"],
        [[r["k"], f'{r["n"]:,}', f'{(r.get("c") or 0):,}'] for r in trend_y])}
</div>

<div class="card"><h2>栏目 Top {len(rank)}</h2>
{rank_html(rank)}
{_table(["栏目", "文章数"], [[x["k"], f'{x["n"]:,}'] for x in rank])}
</div>

<div class="card" style="grid-column:1/-1"><h2>时间轴 —— 发文量月度趋势（分站点）</h2>
{tl["charts"]["volume"]}
<div class="note">按所有文章计（含正文为空的列表页占位）；{_esc(
    f"末月虚线段尚未过完，不可与整月直接比较。"
    if tl["scope"].get("last_month_partial")
    else "所选区间末月已过完，可与整月直接比较。")}</div>
{_table(["月份"] + list(tl["sites"]),
        [[r["month"], f'{r["total"]:,}'] + [f'{r.get(s, 0):,}' for s in tl["sites"]]
         for r in tl_m])}
</div>

<div class="card" style="grid-column:1/-1"><h2>时间轴 —— 主题分布随时间（关键词粗筛）</h2>
{tl["charts"]["theme_kw"]}
<div class="note">堆的是「命中次数」不是互斥分类，一篇可命中多类，
<b>各月相加会超过该月发文量</b>。关键词主题只统计有正文的文章，
与上一张图的分母不同。</div>
{_table(["月份", "有正文", "合计命中"] + list(tl["kept"]),
        [[r["month"], f'{r["total"]:,}',
          f'{sum(r["problems"].values()):,}']
         + [f'{r["problems"].get(k, 0):,}' for k in tl["kept"]]
         for r in tl_t])}
</div>

<div class="card" style="grid-column:1/-1"><h2>时间轴 —— 主题分布随时间（AI 分类）</h2>
{tl["charts"]["theme_ai"]}
<div class="note">AI 主题已标注 {ai_cov["labeled"]}/{ai_cov["total"]} 篇
（{ai_cov["pct"]}%）—— 只统计**已被标注过**的文章，没标过的不在这个分母里。</div>
{_table(["月份", "已标注", "合计命中"]
         + list((tl.get("ai") or {}).get("kept") or []),
        tl_ai_rows) if tl_ai_rows else ""}
</div>

<div class="card"><h2>检索行为（近 {days} 天）</h2>
{ev_html or f'<div class="empty">该站点近 {days} 天没有行为记录</div>'}
{note}
{_table(["行为类型", "次数"], [[x["k"], f'{x["n"]:,}'] for x in ev])}
</div>

<div class="card"><h2>治理问题分布</h2>
{prob_html}
{_table(["治理问题", "文章数", "占比%"],
        [[r["problem"], f'{r["articles"]:,}', r["pct"]] for r in prob["items"]])}
<div class="note">{_esc(prob["note"])}</div>
</div>

<div class="card"><h2>高频机构 Top {len(org_rows)}</h2>
{org_html}
{_table(["机构", "出现次数", "需复核", "归并依据"],
        [[r["org"], f'{r["count"]:,}',
          "⚠ 是" if r.get("needs_review") else "",
          r.get("merge_basis", "")] for r in orgg["items"]])}
<div class="note">{_esc(orgg["note"])}</div>
</div>

<div class="card" style="grid-column:1/-1"><h2>联合发文类文章月度趋势（占比 %）</h2>
{joint_bars}
{_table(["月份", "联合发文类", "当月总数", "占比%"],
        [[r["month"], r["joint"], r["total"], r["pct"]] for r in jit])}
<div class="note">{_esc(joint["note"])}</div>
</div>

</div>
<div class="foot">
数据来源：中央网信办（cac.gov.cn）、浙江网信网（zjwx.gov.cn）公开发布页面。<br>
生成工具：cacx · 网络舆情分析与监测课程作业。仅供学习研究与课程作业用途，
内容著作权归原作者及原发布网站，不得商用。
</div>
</div></body></html>"""
    d.record_event("export", detail="panel:html",
                   meta=f"site={site} months={months}")
    fname = f"cacx_数据面板_{name}_{months}月_{time.strftime('%Y%m%d_%H%M%S')}.html"
    return Response(
        content=html.encode("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(fname)}"})


# ---------------------------------------------------------------- 分析样本
@app.get("/api/sample/options")
def api_sample_options(date_from: str = "2024-01-01",
                       date_to: str = "2099-12-31"):
    """可配样本的栏目清单（站点/栏目/总量/覆盖年份）"""
    return {"date_from": date_from, "date_to": date_to,
            "columns": _db().sample_options(date_from, date_to),
            "strategies": list(D.Database.SAMPLE_STRATEGIES)}


@app.get("/api/sample")
def api_sample(site: str = "", column: str = "",
               date_from: str = "2024-01-01", date_to: str = "2099-12-31",
               limit: int = Query(500, ge=1, le=200000),
               strategy: str = "stratified",
               with_items: bool = False):
    """
    抽一个分析样本。

    老师要「任选一个栏目，300-500 篇，2024 年以来」——
    但栏目规模从 14 篇到 1639 篇不等，光选栏目凑不出 300-500。
    所以规则是：**时间窗给足，篇数用 limit 截，抽样默认按年分层**。

    ⚠ 默认 stratified 而不是「取最近 N 篇」：
      后者只覆盖末段（实测 cac·网信要闻 取最近 500 篇，2024 年整年丢失），
      而老师明确要求「发文量与主题随时间的变化」，时间轴不能只剩一年。
    """
    d = _db()
    try:
        r = d.sample_articles(site, column, date_from, date_to,
                              limit, strategy)
    except ValueError as e:
        raise HTTPException(400, {"message": str(e), "refused": True})
    items = r.pop("items")
    if with_items:
        r["items"] = items
    else:
        r["items_sample"] = items[:20]
        r["items_total"] = len(items)
    return r


@app.get("/api/sample/years")
def api_sample_years(site: str = "", column: str = "",
                     date_from: str = "2024-01-01",
                     date_to: str = "2099-12-31"):
    """某栏目逐年篇数 —— 抽样依据 + 时间轴底稿"""
    return {"site": site, "column": column,
            "years": _db().column_years(site, column, date_from, date_to)}


@app.get("/api/sample/export")
def api_sample_export(site: str = "", column: str = "",
                      date_from: str = "2024-01-01",
                      date_to: str = "2099-12-31",
                      limit: int = Query(500, ge=1, le=200000),
                      strategy: str = "stratified"):
    """
    把样本导成 CSV（utf-8-sig，Excel 直接双击不乱码）。

    ⚠ 走服务端而不是前端拼：界面显示的篇数与导出的行数必须来自
      **同一次抽样**。前端拼的话，用户改一下篇数上限再下载，
      两边数字就对不上 —— 这种错最难发现。
    """
    r = _db().sample_articles(site=site, column=column,
                              date_from=date_from, date_to=date_to,
                              limit=limit, strategy=strategy)
    cols = ["article_id", "pub_day", "site", "column_name", "title", "url"]

    def _cell(v):
        return '"%s"' % str("" if v is None else v).replace('"', '""')

    lines = [",".join(cols)]
    for it in r["items"]:
        lines.append(",".join(_cell(it.get(c)) for c in cols))
    # 页脚注明抽样口径：CSV 脱离了界面后，参数必须写在文件里
    lines.append("")
    lines.append(_cell("# 样本口径：栏目=%s 区间=%s~%s 篇数上限=%d 策略=%s" % (
        r["column"] or "全部", r["date_from"], r["date_to"],
        r["limit"], r["strategy"])))
    lines.append(_cell("# 实际取到 %d 篇，有正文 %d 篇，逐年 %s" % (
        r["n"], r["n_usable"],
        " ".join("%s=%d" % kv for kv in sorted(r["years_taken"].items())))))
    body = "\r\n".join(lines).encode("utf-8-sig")
    fn = "cacx-样本_%s_%s_%s.csv" % (
        (column or "全部栏目").replace("/", "_"),
        r["date_min"] or date_from, r["date_max"] or date_to)
    # RFC5987：中文文件名要 URI 编码，否则部分浏览器会存成乱码名
    q = quote(fn, safe="")
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition":
            "attachment; filename=sample.csv; filename*=UTF-8''%s" % q})


# ---------------------------------------------------------------- 实体 / 关系
# 写入走 POST（AI 通过 MCP 直接写库时不需要这些，但网页端要能人工补录/纠错）。
class EntityIn(BaseModel):
    name: str
    type: str
    article_id: int
    evidence: str = ""
    aliases: List[str] = Field(default_factory=list)
    extractor: str = "human"
    confidence: float = 0.0


class RelationIn(BaseModel):
    src_name: str
    dst_name: str
    rel_type: str
    article_id: int
    evidence: str = ""
    weight: float = 1.0
    extractor: str = "human"


@app.get("/api/graph")
def api_graph(site: str = "", date_from: str = "", date_to: str = "",
              limit: int = Query(300, ge=1, le=2000),
              mode: str = Query("coarse")):
    """
    关系图谱：{nodes, edges}。
    edges 带 article_ids —— 前端「点一条关系回溯原文」直接用它。

    ⚠ site/date_from/date_to 与 /api/graph/analysis 同一套口径：
      画布筛了，边列表必须同步筛，否则两个数字打架。
      date_from/date_to 空串不传（SQLite 碰巧对、PostgreSQL 会错），
      走 db 默认区间（2024-01-01 ~ 2099-12-31）。

    ⚠ T8：mode=refine 时在**这里（app 层）**把活动形态的 org 节点剔掉，
      **不在 db.graph 内做** —— 一个开关只影响读取口径，库与 SQL 不动。
      剔法：`type=='org' 且 eventish_org(name)` 的 node 去掉；
      端点沾到这些名字的 edge 一起去掉（否则边指向一个画布上不存在的点）。
      响应加 `refined_count`（被剔掉的 org 节点条数）—— **refine 档下
      0 也带**，便于前端统一按 mode 渲染说明；coarse 档不加该字段。
      计数 = 本次响应里实际被剔的节点条数（与 refined_count 同源同口径，
      不是全库同名数），所以能直接和「少了几条」对上。
    """
    kw = {"site": site, "limit": limit}
    if date_from: kw["date_from"] = date_from
    if date_to:   kw["date_to"] = date_to
    data = _db().graph(**kw)
    if not _refine_mode_on(mode):
        return data
    ev = _eventish_org_names(_db())
    drop = {n.get("name") for n in data.get("nodes") or []
            if n.get("type") == "org" and (n.get("name") or "") in ev}
    nodes = [n for n in (data.get("nodes") or [])
             if not (n.get("type") == "org" and (n.get("name") or "") in ev)]
    edges = [e for e in (data.get("edges") or [])
             if (e.get("src_name") not in drop
                 and e.get("dst_name") not in drop)]
    return {"nodes": nodes, "edges": edges,
            "refined_count": len(data.get("nodes") or []) - len(nodes)}


@app.get("/api/graph/stats")
def api_graph_stats():
    """图谱概览。no_evidence 是答辩风险指标：没有原文证据的关系越多越危险"""
    return _db().graph_stats()


@app.get("/api/entities")
def api_entities(type: str = "", q: str = "",
                 article_id: Optional[int] = None,
                 limit: int = Query(200, ge=1, le=2000)):
    return {"items": _db().list_entities(type=type, q=q,
                                         article_id=article_id, limit=limit)}


@app.get("/api/relations")
def api_relations(name: str = "", rel_type: str = "",
                  article_id: Optional[int] = None,
                  limit: int = Query(200, ge=1, le=2000)):
    """name 非空时返回与该实体相连的关系（含入边出边）"""
    return {"items": _db().list_relations(name=name, rel_type=rel_type,
                                          article_id=article_id, limit=limit)}


@app.post("/api/entities")
def api_put_entities(rows: List[EntityIn]):
    """人工补录/纠错实体（AI 走 MCP 的 cacx_put_entities）"""
    return _db().upsert_entities([r.model_dump() for r in rows])


@app.post("/api/relations")
def api_put_relations(rows: List[RelationIn]):
    """人工补录/纠错关系"""
    return _db().upsert_relations([r.model_dump() for r in rows])


# ------------------------------------------------------- 内置抽取（规则）
# 四类实体的配色/形状。**不只靠颜色** —— 形状也区分，色盲可读。
_GRAPH_STYLE = {
    "org":     {"background": "#4c8dff", "border": "#7ab8ff", "shape": "dot"},
    "policy":  {"background": "#b48bff", "border": "#c9a8ff", "shape": "square"},
    "problem": {"background": "#ff6b6b", "border": "#ff9b9b", "shape": "triangle"},
    "measure": {"background": "#5ddc9a", "border": "#8ff0bb", "shape": "diamond"},
}
_GRAPH_STYLE_DEFAULT = {"background": "#9aa3b2", "border": "#c2c8d4",
                        "shape": "dot"}
# 检索高亮色。与页面 CSS 变量 --hl 同值 —— 导出件必须和屏幕上是同一个黄，
# 否则「导出后高亮去哪了」会变成一个查不出原因的问题。
_GRAPH_HIT_COLOR = "#ffd479"
_TYPE_CN = {"org": "机构", "policy": "政策", "problem": "治理问题",
            "measure": "措施"}


def _pyvis_lib_dir() -> Optional[str]:
    """定位 pyvis 自带的 vis-network 资源目录（内联用的源文件）"""
    try:
        import pyvis
    except ImportError:
        return None
    root = Path(pyvis.__file__).parent / "lib"
    if not root.is_dir():
        return None
    for v in sorted(root.glob("vis-*"), reverse=True):
        if (v / "vis-network.min.js").exists():
            return str(v)
    return None


# 资源引用标签：<script …> / <link …>，只取开标签（不吞闭合）
_RES_TAG_RE = re.compile(r"<(script|link)\b([^>]*)>", re.I)
_RES_ATTR_RE = re.compile(r"(src|href)\s*=\s*[\"']([^\"']*)[\"']", re.I)
# 内联资源不算「外部引用」：data: URI 与页内锚点都是自包含的
_RES_INLINE_PREFIX = ("data:", "#", "http://", "https://")


def _strip_local_refs(html: str) -> str:
    """
    删掉所有**相对路径**资源引用（`src="lib/…"` / `href="../node_modules/…"`），
    保留内联数据与绝对 http(s) 引用（后者由调用方另行处理）。

    ⚠⚠ 这里**绝对不能**用 `<script[^>]+src="…".*?</script>` 这种正则：
      pyvis 模板里有**自闭合**的 `<script src=…>`（后面没有配套的
      `</script>`），`.*?` 会一路跨到几百行之后的下一个 `</script>`，
      把中间的**真实代码**整段吃掉。实测就栽在这：导出件少了 699 字符，
      vis 还在、节点数据还在、控制台**一个错都不报**，但画布一片空白
      —— 静默失败，最难查的一种。

      所以逐个标签扫描，判据是 HTML 的真实语法 ——
      跳过开标签后的空白，看**下一个东西是什么**：
        · `</script>`  → 它有配对闭合，连闭合一起删（空壳或带内容都算）
        · 别的 `<…>`   → 这是**自闭合** script（pyvis 那些就是：
                        后面直接跟 `</head>`/`<body>`），
                        **只删开标签就停手**
        · 不是 `<`      → 它有内容，连内容与闭合一起删
                        （否则残留的裸 JS 会被浏览器当正文渲染）
    """
    out, pos = [], 0
    for m in _RES_TAG_RE.finditer(html):
        am = _RES_ATTR_RE.search(m.group(2) or "")
        if not am:
            continue
        ref = (am.group(2) or "").strip()
        if not ref or ref.startswith(_RES_INLINE_PREFIX):
            continue
        out.append(html[pos:m.start()])
        pos = m.end()
        if m.group(1).lower() != "script":
            continue
        rest = html[pos:].lstrip()
        # 自闭合的判据是「后面跟的不是本标签的闭合」。`</head>` 之类
        # 也以 '<' 开头，但它不是 `</script>`，所以那个分支要排除掉。
        if not rest.startswith("<") or rest.startswith("</script>"):
            close = html.find("</script>", pos)
            if close != -1:
                pos = close + len("</script>")
    out.append(html[pos:])
    return "".join(out)


def pyvis_html(nodes: List[dict], edges: List[dict],
               height: str = "620px") -> str:
    """
    nodes/edges → **零 CDN** 的单文件 HTML（pyvis + 内联 vis.js）。

    ⚠ 三件事必须都做，否则教室没网时只会白屏：
      ① vis-network.min.js / .css 换成内联（pyvis 默认走 CDN）
      ② bootstrap 的 css/js 引用必须**删掉** —— 我们不用它的布局，
         自己写样式，混进来只会让页面变形
      ③ re.sub 的替换串必须用 lambda：vis.js 源码里有 \\d，
         直接当替换模板会被当转义序列，报 bad escape \\d
    """
    lib = _pyvis_lib_dir()
    if not lib:
        raise RuntimeError(
            "找不到 vis-network 资源（pyvis 未安装或包内缺文件）。\n"
            "  修复：pip install pyvis")
    try:
        from pyvis.network import Network
    except ImportError:
        raise RuntimeError("缺少 pyvis。修复：pip install pyvis")

    g = Network(height=height, width="100%", directed=False,
                notebook=False, bgcolor="#0f1115", font_color="#e6e8ee")
    # pyvis 的 groups 是唯一能同时定「颜色+形状」的地方，
    # 因为 vis-network 的图例与配色都按 group 取
    g.options_groups = {k: dict(v) for k, v in _GRAPH_STYLE.items()}

    # 检索高亮：颜色走**单节点 color 参数**，不走 groups ——
    # groups 是全局一套，命中是逐节点的，两者混不到一起。
    # 高亮色沿用页面上的 --hl，导出件必须和屏幕上是同一个黄。
    for n in nodes:
        gid = n.get("group") or ""
        if gid not in _GRAPH_STYLE:
            gid = "org"          # 未知类型先归机构，形状颜色统一
        extra = {}
        # 检索高亮（hit）与点击聚焦（hl）在导出件里必须同色 ——
        # 「屏幕 = 导出」：屏幕上有聚焦，导出的 HTML 也得有，否则交付物对不上。
        if n.get("hit") or n.get("hl"):
            extra["color"] = {
                "background": _GRAPH_HIT_COLOR, "border": "#fff",
                "highlight": {"background": _GRAPH_HIT_COLOR, "border": "#fff"},
            }
            extra["borderWidth"] = 3
        g.add_node(n["id"], label=n.get("label") or n["id"], group=gid,
                   size=n.get("size", 16), title=n.get("title", ""), **extra)

    for e in edges:
        w = float(e.get("weight") or 1)
        hit = bool(e.get("hit") or e.get("hl"))
        g.add_edge(e["from"], e["to"], value=max(w, 0.1),
                   title=e.get("title", ""),
                   color=(_GRAPH_HIT_COLOR if hit else "#3a4a63"),
                   width=(3.0 if hit else max(w * 0.7, 0.6)))

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "g.html")
        g.write_html(p, notebook=False, local=True)
        html = Path(p).read_text(encoding="utf-8")

    js = Path(lib, "vis-network.min.js").read_text(encoding="utf-8")
    css = Path(lib, "vis-network.css").read_text(encoding="utf-8")
    html = re.sub(r'<script[^>]+src="[^"]*vis-network\.min\.js"[^>]*></script>',
                  lambda _m: "<script>" + js + "</script>", html)
    html = re.sub(r'<link[^>]+href="[^"]*vis-network\.min\.css"[^>]*>',
                  lambda _m: "<style>" + css + "</style>", html)
    # bootstrap 只删 <script>/<link> 标签，保留页面其余部分
    html = re.sub(r'<script[^>]+src="[^"]*bootstrap[^"]*"[^>]*></script>', "", html)
    html = re.sub(r'<link[^>]+href="[^"]*bootstrap[^"]*"[^>]*>', "", html)
    # ⚠ 再兜一层：任何漏网的外链 js/css 一律去掉，
    #   保证「零外链」是硬保证而不是「基本没有」
    html = re.sub(r'<script[^>]+src="https?://[^"]*"[^>]*></script>', "", html)
    html = re.sub(r'<link[^>]+href="https?://[^"]*"[^>]*>', "", html)
    # ⚠⚠ **相对路径**引用同样要去掉 —— 这是「零外链」判据的漏网之处。
    #
    #   「双击即开、断网可用」意味着用户只带走**这一个 .html**。
    #   相对路径（lib/…、../node_modules/…）看着「不是外链」，其实一样会 404：
    #   实测导出件里留着 3 个，控制台报 ERR_FILE_NOT_FOUND。
    #   图之所以还能画出来，是因为 vis.js 已经内联，这 3 个是**死引用**：
    #     · lib/bindings/utils.js      —— pyvis 的 notebook 辅助，静态页用不到
    #     · ../node_modules/vis/dist/… —— pyvis 模板里的源码引用，node_modules 不存在
    #   但「能用」不等于「对」：控制台红字在演示时很扎眼，而且哪天
    #   pyvis 升级把某个函数挪回 utils.js 就会静默崩掉。所以主动剥掉。
    html = _strip_local_refs(html)
    return html


class ExtractReq(BaseModel):
    site: str = ""
    column: str = ""
    date_from: str = "2024-01-01"
    date_to: str = "2099-12-31"
    limit: int = 500                 # 与报告分母一致（已拍板：默认样本口径）
    strategy: str = "stratified"     # ⚠ 别用 recent：会把 2024 整年丢掉
    with_measures: bool = True
    write: bool = False              # ⚠ 默认只预览不落库
    clear_first: bool = False        # ⚠ 清库不可逆，必须显式开
    confirm_clear: bool = False


@app.post("/api/extract/run")
def api_extract_run(req: ExtractReq):
    """
    规则抽取实体关系（老师追问「这个数怎么来的」时可当场重跑）。

    ⚠ 默认 write=false 只返回结果不落库 —— 抽取要跑几十秒，
      误触就写库会污染主库。
    ⚠ clear_first 会**删光**实体和关系，必须同时 confirm_clear=true。
    """
    d = _db()
    if req.clear_first:
        if not req.confirm_clear:
            return {"error": "clear_first 不可逆，必须同时传 confirm_clear=true"}
        d.clear_graph(only="auto")

    arts = d.extract_sources(site=req.site, column=req.column,
                             date_from=req.date_from, date_to=req.date_to,
                             limit=req.limit, strategy=req.strategy)
    if not arts:
        return {"error": "这个口径下没有文章，检查栏目名与时间区间"}

    t0 = time.time()
    res = extract.extract_batch(arts, with_measures=req.with_measures)
    res["elapsed"] = round(time.time() - t0, 1)
    res["scope"] = {"site": req.site, "column": req.column,
                    "date_from": req.date_from, "date_to": req.date_to,
                    "limit": req.limit, "strategy": req.strategy,
                    "articles_found": len(arts)}

    if req.write:
        # 只写高置信（不进图谱的那些低置信仍写库，便于复核与后续分析）
        e = d.upsert_entities(res["entities"])
        r = d.upsert_relations(res["relations"])
        res["written"] = {"entities": e, "relations": r}
    else:
        res["written"] = None
        res["hint"] = "这是预览结果，未写入数据库。确认无误后把 write 改成 true 再跑一次。"

    # 样本分布，供前端显示口径
    res["types"] = {}
    for x in res["entities"]:
        res["types"][x["type"]] = res["types"].get(x["type"], 0) + 1
    res["rel_types"] = {}
    for x in res["relations"]:
        res["rel_types"][x["rel_type"]] = res["rel_types"].get(x["rel_type"], 0) + 1
    # 别把上万条实体塞进 HTTP 响应
    res.pop("entities", None)
    res.pop("relations", None)
    return res


# 哪几类实体会被「低置信就踢出图」的规则扫到。集中一处，别在多个地方各写一遍。
_GRAPH_BLOCK_TYPES = {"measure"}


def _refine_mode_on(mode) -> bool:
    """
    是不是精选档。大小写/空白不敏感；其余任何值一律按粗选（默认口径）。

    ⚠ 为什么要容得下非字符串：这些路由的默认值是 FastAPI 的 `Query(...)`
      **实例**，只有在走 HTTP 时才被框架替换成真字符串。而本项目的测试与
      内部复用（`tests_panel_dates.py` 直接调 `api_panel_orgs(...)` 断言
      `d.top_orgs` 的 kwargs；`api_panel_export` / `api_panel_snapshot`
      直接调它）**都是直接调函数**—— 那时 mode 就是 Query 对象，
      `(mode or "").strip()` 当场 AttributeError。这里统一吃掉：
      Query 实例读它的 default，读不出就当粗选（默认档）。
    """
    if isinstance(mode, str):
        return mode.strip().lower() == "refine"
    dv = getattr(mode, "default", None)      # FastAPI Query 实例
    return isinstance(dv, str) and dv.strip().lower() == "refine"


def _eventish_org_names(d) -> set:
    """
    精选档要滤掉的 org 名集合 = {type=org 且 `textseg.eventish_org(name)`}。

    ⚠ 判据是**形态**（活动/会议片段），与抽取库内容无关 —— 这就是
      「切换只影响读取/统计展示、库数据不动」之所以成立的原因。
    ⚠ 走 `distinct_entity_names("org")` 而不是 list_entities：后者要 JOIN
      articles 再逐行造 11 列 dict（0.7 秒级），而图谱每次拖筛选都会调到；
      这里只要名字（实测 12075 个 org 去重名，一条 DISTINCT 查询）。
    """
    out = set()
    try:
        for n in d.distinct_entity_names("org"):
            n = (n or "").strip()
            if n and textseg.eventish_org(n):
                out.add(n)
    except Exception:
        # 形态闸门是**展示增强**：查不到名字不该让图谱整块挂掉，
        # 退化成「不过滤」（即粗选口径）是安全的，而不是 500。
        return set()
    return out


def _blocked_for_graph(d, allow_types: Optional[set] = None) -> set:
    """
    算出「不进图」的节点名集合。

    ⚠ 只取 `measure` 类实体，**不要为了保险去拉全量实体**：
      `blocked_names` 本来也只在 measure 里判（其余类型直接跳过），
      结果一模一样，但代价差 25 倍 —— 全量 45409 条要 0.75 秒、
      measure 只有 1249 条、约 0.03 秒。图谱每次拖筛选都会走这里。

    ⚠ `allow_types` = 用户在图例里**显式勾选**的类型，勾了就解除过滤。
      起因：`measure` 的 confidence 上限恰好等于低置信门槛（0.8），
      于是「<=0.8」的规则把这一类**100% 剔光**，图例上「措施」恒为 0、
      复选框还被判成 disabled —— 用户想看也点不开。
      默认不勾（默认图与报告口径完全不变），
      但勾了要真的能看见，否则「勾选」就是个假开关。
    """
    types = _GRAPH_BLOCK_TYPES - {t for t in (allow_types or set())}
    if not types:
        return set()
    ents = d.list_entities(type="measure", limit=None)
    return GA.blocked_names(ents, types)


def _node_types(d) -> Dict[str, str]:
    """
    {实体名: 类型}。图例筛选与颜色形状编码的**唯一**数据源。

    ⚠ 这条链路以前整个不存在 —— 建图只吃关系的两端名字，实体类型在建图时
      被丢掉，前端 `n.group || 'org'` 于是把所有节点都当成机构，画出一片
      蓝点，图例上的颜色+形状编码一条都没生效。

    ⚠ 按 (name,type) 两列取，不走 list_entities：后者要 JOIN articles 并逐行
      造 11 列 dict（0.7 秒），图谱每次拖筛选都调，得走窄列那条路。
    ⚠ 按 db 路径 + 行数缓存：抽取会改库，不按路径缓存就会在「刚抽完抽取」之后
      拿到旧类型映射，图例配色对不上新节点 —— 而且没有任何报错。
    """
    try:
        with d._lock:
            n_row = d.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    except Exception:
        return {}
    key = (str(getattr(d, "db_path", "") or id(d)), int(n_row or 0))
    if _NODE_TYPES_CACHE["key"] == key:
        return _NODE_TYPES_CACHE["map"]
    m = GA.node_type_map_from_counts(d.entity_type_counts())
    _NODE_TYPES_CACHE["key"] = key
    _NODE_TYPES_CACHE["map"] = m
    return m


# 节点类型映射的缓存。key 含 db 路径与实体行数，抽取后行数变，自动失效。
_NODE_TYPES_CACHE: Dict[str, Any] = {"key": None, "map": {}}


def _empty_graph_payload(min_degree, max_degree, rel_types, keyword,
                         focus, hop, site, date_from, date_to,
                         summary, node_types: str = "",
                         rel_rows_all: int = 0) -> Dict[str, Any]:
    """
    空结果的统一形状。

    ⚠ 抽出来是因为**形状必须只有一处**：以前「没有关系数据」那套内联字面量
      就写死了一份，现在多了一种空法（图例一个类型都没选），
      两份字面量迟早会漂 —— 漂了之后前端某个键读到 undefined，
      表现是「筛选后页面某块白掉」，极难定位。
    """
    return {"nodes": 0, "edges": 0, "degree": [], "strength": [],
            "betweenness": [], "pagerank": [], "communities": [],
            "summary": summary, "by_type": [],
            "graph": {"nodes": [], "edges": [], "by_group": [],
                      "avail_by_group": []},
            "filters": {"min_degree": min_degree,
                        "max_degree": int(max_degree or 0),
                        "rel_types": sorted(
                            {t.strip() for t in (rel_types or "").split(",")
                             if t.strip()}),
                        "keyword": keyword, "focus": focus, "hop": hop,
                        "site": [s.strip() for s in (site or "").split(",")
                                 if s.strip()],
                        "date_from": date_from, "date_to": date_to,
                        "node_types": [t.strip() for t in
                                       (node_types or "").split(",")
                                       if t.strip()],
                        "graph_nodes_all": 0, "graph_edges_all": 0,
                        "graph_nodes_typed": 0, "graph_edges_typed": 0,
                        "by_type_all": {}, "by_site_all": {},
                        "rel_rows_all": rel_rows_all,
                        "rel_rows": 0,
                        "scope_note": summary, "scope_nodes": 0,
                        "scope_edges": 0}}


def _graph_payload(min_degree: int, limit: int, min_weight: float,
                   topn: int, max_degree: int = 0, rel_types: str = "",
                   keyword: str = "", focus: str = "", hop: int = 1,
                   site: str = "", date_from: str = "", date_to: str = "",
                   node_types: str = "", find: str = "", hl: str = "",
                   mode: str = ""
                   ) -> Dict[str, Any]:
    """
    关联分析的**内部实现**（路由和 pyvis 产物都调它）。

    ⚠ 必须抽成独立函数，不能让 api_graph_vis 直接调 api_graph_analysis：
      路由函数的参数是 `Query(20, ge=1)` **参数对象**，不是数字。
      走 HTTP 时框架会替换掉，走内部调用时不会 —— int() 直接炸
      （TypeError: int() argument must be ... not 'Query'）。
      把 Query 对象的取值逻辑留在路由边界，函数内部只收纯数字。
    """
    d = _db()
    # ⚠ 站点/日期是**关系来源文章**的筛选，必须在建图之前做。
    #   事后挑边的话，边还在但权重已经是全时期的共现强度，
    #   「只看 2024 年」时那个权重并不是 2024 年的强度。
    # ⚠ limit=None = 全量。**建图绝不能带 LIMIT**：list_relations 是
    #   `ORDER BY weight DESC`，截断专挑低权重的关系丢，图上会静默少一大块，
    #   而界面没有任何提示。实测 8801 条关系取全量只要 0.08 秒。
    rows = d.list_relations(limit=None, site=site,
                            date_from=date_from, date_to=date_to)
    rows_all = None
    if (site or date_from or date_to):
        rows_all = d.list_relations(limit=None)
    if not rows:
        return _empty_graph_payload(min_degree, max_degree, rel_types,
                                    keyword, focus, hop, site,
                                    date_from, date_to,
                                    "没有关系数据", node_types=node_types)

    want = {t.strip() for t in (rel_types or "").split(",") if t.strip()}

    # ---- 实体类型筛选（图例那四个）----
    # ⚠ node_types 为空 = 不筛（默认全选）；**「全不选」当非法输入处理**，
    #   返回空图会让用户以为「这类一个都没有」，其实是筛选写法有问题。
    #   前端会挡住，但后端也得挡 —— MCP/直接调 API 绕不过前端。
    ntypes_map = _node_types(d)
    want_nt = {t.strip() for t in (node_types or "").split(",") if t.strip()}
    want_nt = {t for t in want_nt if t in GA.ENTITY_TYPES}
    empty_pick = bool((node_types or "").strip()) and not want_nt
    if empty_pick:
        return _empty_graph_payload(min_degree, max_degree, rel_types,
                                    keyword, focus, hop, site,
                                    date_from, date_to,
                                    "没有选中任何实体类型（至少选一类）")

    # 低置信实体不进图（与 build_graph 的口径一致）。
    # ⚠ 用户在图例里**显式勾选**的类型要解除这道过滤 —— 默认全拦（口径不变），
    #   勾了才放行。measure 是唯一会踩到的类型：它的 confidence 上限恰好
    #   等于门槛 0.8，默认口径下整类 100% 出局。
    # ⚠⚠ blocked_all 是**不过滤时**会被挡掉的量，用于给界面**说明**；
    #   建图用的是 allow 之后的 blocked。
    #   两者不能混：勾选放行后 blocked 变空，若拿它说明，
    #   界面上「已放行 210 个措施」这句话就一起消失了 —— 用户只看到
    #   措施从 0 变成 9，不知道中间发生了什么。
    blocked_all = _blocked_for_graph(d)
    blocked = _blocked_for_graph(d, allow_types=want_nt)
    # ---- T8 精选档：活动/会议形态的 org 名并入黑名单 --------------------
    # ⚠ 必须在 **build_graph 之前**并入。事后挑节点/边是错的：
    #   那样 graph_nodes_all / graph_edges_all / by_type / by_site 全是粗选口径，
    #   画布少一块而计数没少 —— 正是这个项目一直在防的「图与数字两套口径」。
    # ⚠ blocked_all 也要并：它是「不解除过滤时会被挡掉的量」，用于界面说明；
    #   只并 blocked 会让 blocked_kept_out = blocked_all - blocked 变成负数。
    # ⚠ 形态闸门不受图例勾选（allow_types）影响：它拦的是「活动名不是机构」，
    #   与「措施类低置信」是两回事，用户勾不勾都不该把它放回图上。
    refine_on = _refine_mode_on(mode)
    ev_orgs = _eventish_org_names(d) if refine_on else set()
    if ev_orgs:
        blocked_all = blocked_all | ev_orgs
        blocked = blocked | ev_orgs
    # 被剔的 org 名数 = **本次关系端点里**真正会变成节点的那些（不是全库同名数）——
    # 只有这样它才能和「少了几条」对上，不至于报一个用户永远验不了的数字。
    refined_count = 0
    if ev_orgs:
        ends = {(x.get("src_name") or "").strip() for x in rows}
        ends |= {(x.get("dst_name") or "").strip() for x in rows}
        refined_count = len(ends & ev_orgs)
    G, types = GA.build_graph(rows, min_weight=min_weight,
                              blocked_nodes=blocked,
                              rel_types=want or None)
    # ⚠ 只有真的筛过才需要再建一张未筛的图来报底数 ——
    #   build_graph 要把 5000 条关系全过一遍，无条件建两次纯属白干。
    if want or rows_all is not None:
        base_rows = rows if rows_all is None else rows_all
        G_all, _ = GA.build_graph(base_rows, min_weight=min_weight,
                                  blocked_nodes=blocked)
        n_all, e_all = G_all.number_of_nodes(), G_all.number_of_edges()
    else:
        n_all, e_all = G.number_of_nodes(), G.number_of_edges()

    # ⚠ 介数/PageRank/度数排名必须和画出来的图是**同一张图**。
    #   排名表来自 analyze()，画图来自 to_pyvis_nodes_edges() ——
    #   若 analyze 跑全图、画图裁子图，界面上就会同时出现两套口径的数字，
    #   而口径面板明明写着「介数只在这张子图里算」，那就是骗用户。
    #   所以这里裁一次子图，两边共用同一份分析结果。
    sub, scope_note = GA.crop_scope(G, focus=focus,
                                    keyword=keyword, hop=hop,
                                    node_types=ntypes_map,
                                    want_types=want_nt or None)
    if sub.number_of_nodes():
        r = GA.analyze(sub, types, topn=topn)
    else:
        # 空子图（焦点查无此机构）：analyze 在空图上会退化，
        # 宁可把排名清空，也不能拿全图数字冒充子图数字
        # ⚠ 兜底才需要那张**全图**分析结果，所以它放在这里现算。
        #   原来无条件在上面先算一次 —— 而介数在 1600 节点图上要 2.7 秒，
        #   等于每次打开图谱页（哪怕根本没填焦点）都白付一次钱。
        r = dict(GA.analyze(G, types, topn=topn))
        for k in ("degree", "strength", "betweenness", "pagerank"):
            r[k] = []
        # 映射与指纹也要清掉：这里是**全图**的分析结果，画的是空子图，
        # 不清的话 to_pyvis 会先看到指纹不匹配而自己重算（正确），
        # 但把全图的映射留在 r 里只会让下一个人误以为可以复用。
        r["node_betweenness"] = {}
        r["node_pagerank"] = {}
        r["graph_fingerprint"] = None
    # ⚠ 图例禁用态要依据的计数：**剔除实体类型筛选之前**的子图。
    #   判据必须是「这个类型在当前范围内存不存在」，而不是「筛完还剩几个」——
    #   用后者的话，取消勾选「政策」后计数变 0 → 判成 disabled →
    #   于是**再也点不回来**，用户把自己锁死了（实测真会发生）。
    #   只改前端判据是治不好的，因为信息在响应里就已经混了。
    pre, _pre_note = GA.crop_scope(G, focus=focus, keyword=keyword, hop=hop)
    avail_by_group = {t: sum(1 for x in pre.nodes()
                              if ntypes_map.get(x) == t)
                      for t in GA.ENTITY_TYPES}
    ne = GA.to_pyvis_nodes_edges(r, G, types, min_degree=min_degree,
                                 limit=limit, max_degree=max_degree,
                                 keyword=keyword, focus=focus, hop=hop,
                                 node_types=ntypes_map,
                                 want_types=want_nt or None,
                                 avail_by_group=avail_by_group,
                                 find=find, hl=hl)
    # ⚠⚠ **一张边都没有的图，等价于空图**，排名必须一起清掉。
    #   这不是假想：「只看政策」会得到 448 个政策节点、但**零条边** ——
    #   因为本库关系全是 org→X 单向，政策之间没有边。
    #   此时 sub.number_of_nodes() 是 448（非空），于是 analyze 照常跑，
    #   排出一份"介数 Top"名单，值全是 0.0 —— 而屏幕上一个点都没有。
    #   用户看到的是「图空白 + 一屏指标」，正是这个项目反复防的
    #   「图是一套口径、数字是另一套」。
    if ne and not ne.get("edges"):
        for k in ("degree", "strength", "betweenness", "pagerank",
                  "communities"):
            r[k] = []
        r["node_betweenness"] = {}
        r["node_pagerank"] = {}
        r["graph_fingerprint"] = None
        r["by_type"] = []
        # ⚠ 原因已经写在 crop_by_node_types 的 scope_note 里了（它同时判
        #   节点与边），这里只补一句"排名已清空"，别把原因重复一遍 ——
        #   两处各写一份说明，早晚会漂成两种说法。
        if "排名已清空" not in (ne.get("scope_note") or ""):
            ne["scope_note"] = ((ne.get("scope_note") or "") +
                                "；这批实体之间没有边（指标无法计算，排名已清空）"
                                ).lstrip("；")
    # 内部用的全量映射与指纹，去掉以免响应多大几十 KB
    r.pop("node_community", None)
    r.pop("node_betweenness", None)
    r.pop("node_pagerank", None)
    r.pop("graph_fingerprint", None)
    r["graph"] = ne
    # 说明用**不过滤**的量（勾选放行后也要说得清「放行了多少」），
    # 建图用的是 allow 之后的 blocked —— 两者刻意不共用。
    r["blocked_count"] = len(blocked_all)
    r["blocked_kept_out"] = len(blocked_all) - len(blocked)
    # ⚠⚠ blocked_count 只给总数是不够的：用户看到「低置信节点 210 个未进图」
    #   之后，**不会自动联想到「措施 210 个」**，于是图例上「措施 0」看起来
    #   像"这一类根本没抽出来"，而库里其实有 1168 个措施实体、489 条 adopt 关系。
    #   假口径比缺口径更伤：它让读者去找一个不存在的原因。
    #   典型现场：措施只被抽成 confidence<=0.8，而踢出门槛就是「<=0.8」，
    #   于是**整类 100% 出局** —— 图例恒为 0，且没有任何地方说明为什么。
    blocked_by_type: Dict[str, int] = {}
    for nm in blocked_all:
        t = ntypes_map.get(nm) or "其他"
        blocked_by_type[t] = blocked_by_type.get(t, 0) + 1
    r["blocked_by_type"] = blocked_by_type
    # ---- 口径回显：界面必须能说清「这些数字是在什么范围里算的」----
    #  ⚠ 不回显的话，用户会把聚焦子图里的介数和全图介数直接比大小，
    #    那是两个不同口径的数字，没有可比性。
    by_type_all: Dict[str, int] = {}
    for x in rows:
        by_type_all[x.get("rel_type") or ""] = \
            by_type_all.get(x.get("rel_type") or "", 0) + 1
    # 站点分布同样要回显：筛了站之后，用户得知道"全库本来有哪些站"
    by_site_all: Dict[str, int] = {}
    for x in (rows_all if rows_all is not None else rows):
        k = (x.get("site") or "") or "（站点未标注）"
        by_site_all[k] = by_site_all.get(k, 0) + 1
    r["filters"] = {
        "min_degree": min_degree,
        "max_degree": int(max_degree or 0),
        "rel_types": sorted(want),
        "keyword": keyword, "focus": focus, "hop": hop,
        "site": [s.strip() for s in (site or "").split(",") if s.strip()],
        "date_from": date_from, "date_to": date_to,
        # 图例筛了哪几类实体。⚠ 必须回显：筛完之后的度/介数是**子图口径**，
        #   和不筛时不是一个数，界面上得说清，否则用户会把两次数值直接比大小。
        "node_types": sorted(want_nt) or list(GA.ENTITY_TYPES),
        "node_types_active": bool(want_nt),
        "graph_nodes_all": n_all,
        "graph_edges_all": e_all,
        "graph_nodes_typed": G.number_of_nodes(),
        "graph_edges_typed": G.number_of_edges(),
        "by_type_all": by_type_all,
        "by_site_all": by_site_all,
        "rel_rows_all": len(rows_all if rows_all is not None else rows),
        "rel_rows": len(rows),
        "scope_note": ne.get("scope_note", ""),
        "scope_nodes": ne.get("scope_nodes", 0),
        "scope_edges": ne.get("scope_edges", 0),
    }
    # ⚠ 只有**精选档且真剔到了**才加 refined_count：粗选档不加（前端靠这个键
    #   的有无判档，加了会让粗选响应凭空多一句说明）。
    if refine_on and refined_count:
        r["filters"]["refined_count"] = refined_count
    return r


@app.get("/api/graph/analysis")
def api_graph_analysis(min_degree: int = Query(1, ge=1, le=50),
                       limit: int = Query(300, ge=1, le=1000),
                       min_weight: float = 1.0,
                       topn: int = Query(20, ge=1, le=100),
                       max_degree: int = Query(0, ge=0, le=500),
                       rel_types: str = "",
                       keyword: str = "",
                       focus: str = "",
                       hop: int = Query(1, ge=1, le=4),
                       site: str = "",
                       date_from: str = "",
                       date_to: str = "",
                       node_types: str = "", find: str = "", hl: str = "",
                       mode: str = ""):
    """
    关联指标（networkx）：度/加权度/介数/PageRank/Louvain 社区。

    这是「权责边界」的直接论据 —— 度中心性只能说明谁出现最多，
    **介数**才说明谁在协作网络里居中协调。

    筛选：
      · rel_types   逗号分隔的关系类型（joint_issue/issue/target/adopt）
      · node_types  逗号分隔的**实体类型**（org/policy/problem/measure），
                    就是图例那四个勾选项；空 = 全选
      · max_degree  度数上限（0 = 不限），用来压掉「一颗星把整张图盖住」
      · keyword     只保留名称含此词的节点
      · focus+hop   只看以某节点为中心 hop 跳的邻域（指标在子图上重算）
      · site        逗号分隔的来源站点（关系所在文章的站点）
      · date_from/date_to 关系来源文章的发表日期范围，支持 YYYY 或 YYYY-MM
      · find        高亮：含此串的所有节点 + 两端都命中的边（只标不改图）
      · hl          点击聚焦：节点名 → 该节点+一跳邻居+关联边；
                    a|b → 恰好 2 节点 + 1 边（同样只标不改图）
      · mode        口径档位：空/coarse = 粗选（默认，与历史逐字一致）；
                    refine = 精选，活动/会议形态的 org 节点并入黑名单，
                    画布/排名/各计数一起收紧，响应 filters 里多 refined_count

    ⚠ node_types 与 focus/keyword 的**先后顺序固定为 类型 → 聚焦 → 关键词**
      （见 graph_analysis.crop_scope）。所以「只看政策 + 聚焦某机构」=
      先砍到政策类、再在政策里找那个机构的邻域；换个顺序含义就变了。
    """
    return _graph_payload(int(min_degree), int(limit), float(min_weight),
                          int(topn), int(max_degree), rel_types,
                          keyword, focus, int(hop),
                          site, date_from, date_to,
                          node_types=node_types, find=find, hl=hl,
                          mode=mode)


@app.get("/api/graph/facets")
def api_graph_facets(limit: int = Query(20, ge=1, le=60),
                     mode: str = ""):
    """筛选器的候选项：有哪些关系类型、各有多少条；哪些节点适合当焦点。

    ⚠ 这些数字来自**未加任何筛选**的全图 —— 它们是筛子的选项说明，
      不是筛选后的结果。混了就会把「全库有多少条 issue」说成
      「筛完还有多少条 issue」。

    ⚠ T8：mode=refine 时 **focus_candidates 里也滤掉活动形态的 org 名** ——
      否则会出现「焦点下拉框里能选『协会召开第二届理事会』，选了却查出空图」
      （它已不在精选画布上）。其余计数（rel_types / graph_nodes / sites /
      years）是**筛子的候选说明**，保持全量口径不动。
    """
    d = _db()
    # ⚠ 这些计数是筛子的**候选说明**，必须是全量口径：截断了就会把
    #   「全库 8801 条关系」显示成「5000 条」，还按 weight 只留下高权重的
    #   那批，by_type / by_site / by_year 三个分布一起偏。
    rows = d.list_relations(limit=None)
    by_type: Dict[str, int] = {}
    for x in rows:
        t = x.get("rel_type") or ""
        by_type[t] = by_type.get(t, 0) + 1
    blocked = _blocked_for_graph(d)
    G, _ = GA.build_graph(rows, blocked_nodes=blocked)
    deg = dict(G.degree())
    # ⚠ 焦点候选必须按 name **去重**：同一个机构在多篇文章里会被反复抽成
    #   多条实体记录（每条一个 id），不去重时下拉框里会出现五个
    #   「中央网信办」，用户点哪个都一样。
    #   同名节点的度相同（无向图里度由邻接决定），取 max 即可。
    # 焦点候选只给机构类里度最高的那些 —— 随便点一个长尾节点
    # 邻域里可能只有 1 个点，图就空了。
    # ⚠ 机构名要用**全量去重名**：早先走 list_entities(limit=5000)，
    #   那按 name 排序截断，等于「只有笔画靠前的机构能当焦点」——
    #   下拉框里机构缺了一大半，却完全看不出来。
    refine_on = _refine_mode_on(mode)
    best: Dict[str, int] = {}
    for n in d.distinct_entity_names("org"):
        n = (n or "").strip()
        if not n or n in blocked:
            continue
        # ⚠ 精选档：活动名不进候选，否则「选了它 → 空图」。
        #   滤在 **[:limit] 截断之前** —— 截断后再滤会白占名额，
        #   下拉框凭空少几项却看不出来。
        if refine_on and textseg.eventish_org(n):
            continue
        best[n] = max(best.get(n, 0), deg.get(n, 0))
    cands = [{"name": n, "degree": dv} for n, dv in best.items()
             if dv >= 2]
    cands.sort(key=lambda x: -x["degree"])
    # 站点与年份：让「网站 / 日期范围」两个筛选器的候选项有据可查，
    # 而不是在前端硬编码。硬编码过一次，站点改名就静默筛空了。
    by_site: Dict[str, int] = {}
    by_year: Dict[str, int] = {}
    for x in rows:
        s = (x.get("site") or "") or ""
        if s:
            by_site[s] = by_site.get(s, 0) + 1
        y = str(x.get("pub_day") or "")[:4]
        if y:
            by_year[y] = by_year.get(y, 0) + 1
    return {
        "rel_types": [{"key": k, "n": v} for k, v in
                      sorted(by_type.items(), key=lambda kv: -kv[1])],
        "focus_candidates": cands[:limit],
        "graph_nodes": G.number_of_nodes(),
        "graph_edges": G.number_of_edges(),
        "degree_max": max(deg.values()) if deg else 0,
        "sites": [{"key": k, "n": v} for k, v in
                  sorted(by_site.items(), key=lambda kv: -kv[1])],
        "years": [{"key": k, "n": v} for k, v in
                  sorted(by_year.items())],
    }


@app.get("/api/graph/evidence")
def api_graph_evidence(src: str, dst: str, rel_type: str = "",
                       limit: int = Query(50, ge=1, le=200)):
    """点一条边 → 列出支撑它的原文句子与可点链接（老师要求的「点边看原文」）"""
    return {"items": _db().relation_evidence(src, dst, rel_type=rel_type,
                                             limit=limit)}


_VIS_CACHE: Dict[str, str] = {}


@app.get("/api/graph/vislib.js")
def api_graph_vislib_js():
    """
    把 vis-network 的 JS 内联到**主页面**里（零 CDN）。

    ⚠ 必须显式声明 Content-Type: application/javascript，不能用
      HTMLResponse —— 那会返回 text/html，浏览器按严格 MIME 检查
      **拒绝执行** <script src>，onload 不触发，ensureVis() 的 Promise
      永远悬空，loadGraph() 整条卡死（症状：图谱页一片空白，
      且只在**刷新后**出现，因为首屏 vis 可能已被别的路径注入）。
    ⚠ 缓存：469KB 不该每次进图谱页都重传。
    """
    lib = _pyvis_lib_dir()
    if not lib:
        raise HTTPException(500, "缺少 vis-network 资源，请 pip install pyvis")
    if "js" not in _VIS_CACHE:
        _VIS_CACHE["js"] = Path(lib, "vis-network.min.js").read_text(
            encoding="utf-8")
    return Response(_VIS_CACHE["js"],
                    media_type="application/javascript; charset=utf-8")


@app.get("/api/graph/vislib.css")
def api_graph_vislib_css():
    """vis-network 的 CSS（220KB），同样内联；MIME 必须是 text/css"""
    lib = _pyvis_lib_dir()
    if not lib:
        raise HTTPException(500, "缺少 vis-network 资源，请 pip install pyvis")
    if "css" not in _VIS_CACHE:
        _VIS_CACHE["css"] = Path(lib, "vis-network.css").read_text(
            encoding="utf-8")
    return Response(_VIS_CACHE["css"],
                    media_type="text/css; charset=utf-8")


@app.get("/api/graph/vis")
def api_graph_vis(min_degree: int = Query(1, ge=1, le=50),
                  limit: int = Query(300, ge=1, le=1000),
                  max_degree: int = Query(0, ge=0, le=500),
                  rel_types: str = "", keyword: str = "",
                  focus: str = "", hop: int = Query(1, ge=1, le=4),
                  site: str = "", date_from: str = "", date_to: str = "",
                  node_types: str = "", find: str = "", hl: str = "",
                  mode: str = ""):
    """
    产出 pyvis 用的单文件 HTML（vis.js **内联**，零 CDN）。

    为什么必须内联：教室没网，任何 <script src="https://cdn..."> 都只会
    白屏。vis-network 的 js/css 就在 pyvis 包里，直接读进来即可。

    ⚠ 参数必须与 /api/graph/analysis **完全对齐**：导出的是「屏幕上
      看到的那张图」。少接一个筛选参数，用户筛完了导出出来却是全图，
      交付物和演示当场对不上，而且这种错误极难察觉。
      T8 的 `mode` 同理：屏幕在精选档、导出的是粗选那张图，
      就是「屏幕=导出」断掉 —— 所以这里的 mode 直通 `_graph_payload`。
    """
    data = _graph_payload(int(min_degree), int(limit), 1.0, 20,
                          int(max_degree), rel_types, keyword, focus,
                          int(hop), site, date_from, date_to,
                          node_types=node_types, find=find, hl=hl,
                          mode=mode)
    g = data.get("graph") or {}
    if not g.get("nodes"):
        return {"error": "当前筛选下没有可显示的节点"}
    html = pyvis_html(g["nodes"], g["edges"])
    return {"html": html, "nodes": len(g["nodes"]),
            "edges": len(g["edges"]), "bytes": len(html),
            "scope_note": g.get("scope_note", "")}


# ---------------------------------------------------------------- AI
class SummarizeReq(BaseModel):
    id: int
    commit: bool = False      # 默认只预览不落库


@app.post("/api/ai/summarize")
def api_ai_summarize(req: SummarizeReq):
    cfg = _cfg()
    d = _db()
    a = d.get_article(req.id)
    if not a:
        raise HTTPException(404, "文章不存在")
    try:
        result = ai_summarize(cfg, a, dry_run=not req.commit)
    except Exception as e:
        raise HTTPException(503, f"AI 调用失败: {e}")
    # commit=True 时 dry_run=False，result 才是真实结果，此时才落库
    if req.commit:
        d.save_annotation(req.id, "summarize", result,
                          provider=cfg["ai"]["provider"], model=cfg["ai"]["model"])
    return {"committed": req.commit, "result": result}


@app.get("/api/annotations")
def api_annotations(
    task: str = "summarize",
    q: str = "",
    site: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """集中查看已落库的 AI 产物。摘要不再只藏在单篇详情里。"""
    return _db().list_annotations(task=task, q=q, site=site,
                                  limit=limit, offset=offset)


@app.get("/api/annotations/stats")
def api_annotations_stats():
    return _db().annotation_stats()


# ------------------------------------------------------- 分词实验室（新词自学习）
# 闭环：语料 → 新词候选（统计法） → 人工勾选 → 写进 jieba 用户词典 → 下次检索更准


def _userdict_path() -> str:
    """用户词典落点。与数据库同目录，整体备份/迁移时一起带走。"""
    return NW.userdict_path(str(C.PROJECT_ROOT / "data"))


def _known_words() -> set:
    """当前分词器已认识的词：内置领域词表 + jieba 词典 + 已回流词。

    ⚠ 实现在 newwords.known_words()，不在这里 —— 网页与 MCP 是两条入口，
      口径必须同一份实现，否则会出现「网页上不报旧词、MCP 里照报」
      这种用户完全无从察觉的漂移。"""
    return NW.known_words(_userdict_path())


class NewWordsReq(BaseModel):
    limit: int = Field(1200, ge=20, le=3000)   # 取多少篇文章来统计
    max_chars: int = Field(5000, ge=500, le=20000)  # 每篇取多少字
    min_count: int = Field(8, ge=2, le=200)
    topn: int = Field(60, ge=10, le=300)
    #: 抽样策略。⚠ 默认必须是 stratified —— 见 db.sample_content_text
    #: 里的说明：原来的「无 ORDER BY 的 LIMIT」实际只取到 cac + 单一年份。
    strategy: str = Field("stratified")
    site: str = ""                            # 空 = 两站都算


#: 语料底账的短缓存。
#: corpus_profile() 要对 3823 万字跑 SUM(length) 与两次 GROUP BY，
#: 本身就不便宜；而口径预览是「输入框每敲一下就发一次」的高频调用。
#: 语料不会秒级变化，缓存 30 秒既让预览变成毫秒级，
#: 又不至于让用户看到过期数据。
_CORPUS_TTL = 30.0
_CORPUS_CACHE: Dict[str, Any] = {"ts": 0.0, "key": None, "val": None}


def _corpus_profile_cached(site: str = ""):
    key = site
    now = time.time()
    if _CORPUS_CACHE["key"] == key and now - _CORPUS_CACHE["ts"] < _CORPUS_TTL:
        return _CORPUS_CACHE["val"]
    val = _db().corpus_profile(site=site)
    _CORPUS_CACHE.update(ts=now, key=key, val=val)
    return val


def _nw_corpus(limit: int = 1200, strategy: str = "stratified",
               site: str = ""):
    """取样 + 口径清单。预览与真挖掘共用这一个函数，
    避免「预览说覆盖 2 个站、挖出来只有 1 个站」这种对不上的情况。"""
    d = _db()
    s = d.sample_content_text(limit=limit, site=site, strategy=strategy)
    prof = _corpus_profile_cached(site)
    cover = {
        "pool": s["pool"], "pool_chars": s["pool_chars"],
        "n": s["n"], "chars": s["chars"],
        "pct_articles": round(100.0 * s["n"] / s["pool"], 2) if s["pool"] else 0,
        "pct_chars": round(100.0 * s["chars"] / s["pool_chars"], 2)
        if s["pool_chars"] else 0,
        "strategy": s["strategy"], "site": site,
        "cover_sites": s["cover_sites"], "cover_years": s["cover_years"],
        "picked": s["picked"],
        "pool_sites": len(prof["per_site"]),
        "pool_years": len(prof["per_year"]),
        "date_min": prof["date_min"], "date_max": prof["date_max"],
    }
    return s, cover


@app.get("/api/newwords/corpus")
def api_newwords_corpus(limit: int = Query(1200, ge=20, le=3000),
                        strategy: str = Query("stratified"),
                        site: str = Query("")):
    """**只报口径、不做统计** —— 让用户在点「挖掘」之前
    先看见「这个篇数会覆盖哪些站、哪几年、多少字」。"""
    try:
        _, cover = _nw_corpus(limit, strategy, site)
    except ValueError as e:
        return {"error": str(e)}
    return cover


@app.post("/api/newwords/discover")
def api_newwords_discover(req: NewWordsReq):
    """从语料里挖新词候选（只读，不改任何东西）"""
    try:
        s, cover = _nw_corpus(req.limit, req.strategy, req.site)
    except ValueError as e:
        return {"error": str(e)}
    if not s["texts"]:
        return {"error": "语料里没有可用正文，先抓一些文章"}
    texts = [t[:req.max_chars] for t in s["texts"]]
    known = _known_words()
    t0 = time.time()
    cands = NW.discover_new_words(texts, min_count=req.min_count,
                                  topn=req.topn, known=known)
    for c in cands:
        c["hint"] = NW.domain_hint(c["word"]) or ""
    return {"items": cands, "articles": len(texts),
            "known_size": len(known), "elapsed": round(time.time() - t0, 1),
            "coverage": cover}


class AdoptReq(BaseModel):
    words: List[str] = []


@app.get("/api/newwords/adopted")
def api_newwords_adopted():
    """已回流进词典的词"""
    return {"words": NW.read_userdict(_userdict_path()),
            "path": _userdict_path()}


@app.post("/api/newwords/adopt")
def api_newwords_adopt(req: AdoptReq):
    """
    把选中的新词写进 jieba 用户词典。

    ⚠ 落盘是**追加**，不覆盖 —— 文件里可能有手工加过的词。
    ⚠ 写完立刻 add_word 让**当前进程**也生效，
      否则用户点了采纳却发现分词没变化，会以为功能坏了。
    """
    words = [w.strip() for w in (req.words or []) if w and w.strip()]
    if not words:
        return {"error": "没有选中任何词"}
    path = _userdict_path()
    added = NW.write_userdict(path, words)
    for w in words:
        textseg.add_word(w)
    return {"added": added, "total": len(NW.read_userdict(path)),
            "path": path, "applied_now": True}


@app.get("/api/segment")
def api_segment(text: str, limit: int = Query(50, ge=1, le=200)):
    """试切分：看某个串在**当前词典**下被切成什么（采纳后应变化）"""
    if not text:
        return {"items": []}
    return {"text": text, "tokens": textseg.cut(text)[:limit],
            "jieba": textseg.available()}


class ReviewReq(BaseModel):
    """AI 预勾选。cands 由前端回传，不在这里重扫语料 ——
    界面上的候选表就是用户看到、正在复核的那一份，
    让后端自己重扫会出现「AI 评的是另一批词」这种最气人的错位。"""
    cands: List[Dict[str, Any]] = []
    run: bool = False          # 默认只预览：先看 prompt 和花多少 token
    batch: int = Query(40, ge=5, le=100)


@app.post("/api/newwords/review")
def api_newwords_review(req: ReviewReq):
    """
    让 AI 逐个判断候选词该不该收进词典，**只返回建议**。

    ⚠⚠ 这里**一个字都不会写进词典** —— 采纳必须由人点「✅ 采纳勾选」。
      AI 和统计都只是概率，而词典是**持续污染**后续所有检索与统计的东西。
      把「建议」与「生效」分成两步，是让误判可撤销的唯一办法。

    ⚠ 逐字一致的白名单：模型返回的 word 必须落在传入的候选里，
      交集之外一律丢弃（候选文本来自抓来的网页，防提示注入）。
    """
    cands = [c for c in (req.cands or []) if (c.get("word") or "").strip()]
    if not cands:
        return {"error": "没有候选可评审"}
    cfg = _cfg()
    try:
        res = ai_review(cfg, cands, dry_run=not req.run, batch=req.batch)
    except AIError as e:
        # AI 没配 / 调用失败都不是致命错误：统计结果照常可用，
        # 只是没有预勾选而已。返回可读原因，前端显示「跳过」而不是空白。
        return {"error": str(e), "retryable": bool(getattr(e, "retryable", False))}
    if not req.run:
        return {"dry_run": True, **res}
    return {"dry_run": False, **res}


@app.get("/api/ai/status")
def api_ai_status():
    return ai_status(_cfg())


# ---------------------------------------------------------------- 接口诊断
@app.get("/api/api-check")
def api_api_check(
    url: str = "",
    method: str = "GET",
    referer: str = "",
    header: List[str] = Query(default=[]),
    param: List[str] = Query(default=[]),
):
    """
    对任意接口 URL 做只读诊断（与 CLI `cacx api-check` 同一个后端
    cacx.diagnose.probe_api）。

    纯只读：不写库、不写配置。安全性说明：probe_api 不读 settings.json，
    返回的 suggested 只有 api_url/method/referer/list_path/选择器，
    调用方用 header= 传的鉴权头（Cookie/Authorization）不会被回显进响应。
    """
    if not url.strip():
        raise HTTPException(400, "缺少 url")

    def _kv(pairs):
        # k=v 形式的可重复 query 参数；坏条目跳过而不是 400 —— 诊断是
        # 「随手粘个地址看一眼」的场景，敲错一个参数不该挡住整次诊断
        out = {}
        for p in pairs or []:
            p = str(p).strip()
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
        return out

    from ..diagnose import probe_api
    try:
        return probe_api(url, method=method, referer=referer,
                         headers=_kv(header), params=_kv(param))
    except Exception as e:
        # 照 /api/sites/probe 的风格：接口本身返回失败由 probe_api 记进
        # issues；这里只有「诊断器自己炸了」才算 502
        raise HTTPException(502, f"诊断失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 批量摘要
# 与 /api/sync 同一个模式：批量 AI 是长任务（每篇一次网络往返），绝不能跑在
# 请求线程里，否则一个 POST 就把整个服务卡死。后台线程 + job 状态字典 + 前端
# 轮询。不复用 SYNC_STATE：摘要批次与同步批次互相独立，混用一份状态会让两个
# 页面的进度条互相串味。
BATCH_LOCK = threading.Lock()
BATCH_JOBS: Dict[str, dict] = {}


class BatchReq(BaseModel):
    """批量摘要请求体。dry_run 默认 True 是铁律：误触按钮也烧不掉配额。"""
    sites: List[str] = []           # 空 = 全部启用站点
    date_from: str = ""
    date_to: str = ""
    column: str = ""
    limit: int = 10
    concurrency: int = 3
    rate: float = 0.0
    resume: bool = False
    stop_on_error: bool = False
    dry_run: bool = True
    # incremental（默认）= 只处理尚无 summarize 摘要的文章；full = 全部符合条件
    # （含已有摘要，重新生成）。非法值交给 Pydantic Literal 拦成 422 —— 与
    # limit 传字符串这类 schema 错误同码，不另起一套手工 400，前端错误处理统一。
    mode: Literal["incremental", "full"] = "incremental"
    # 中断续跑专用：直接点名要跑哪几篇。
    # 与 sites/date/limit 互斥 —— 给了 ids 就只跑这些，
    # 「只重试失败项」靠它接上（见 /api/ai/retry-failed）。
    ids: Optional[List[int]] = None


def _same_source_groups(cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """按 base_url 把站点分组，返回 {base_url: [code, ...]}（只含 2+ 个的）。

    为什么需要它：可以给同一个网站配多个站点 code（内置的 zjwx +
    自定义的 zjwx-api 都是 www.zjwx.gov.cn）。它们抓到的是**同一批稿件**，
    同时勾选入库会产生：
      · 同一篇文章在库里出现两行（跨站同名标题已实测 10 条）
      · 批量摘要对同一篇跑两次 —— 重复烧 AI 配额
      · 计数口径失真（模拟：zjwx-api 同步后 28999 → 34716）
    所以批量摘要与同步都要能识别并提示这组。
    """
    from .. import sites as S
    groups: Dict[str, List[str]] = {}
    for code, src in (S.all_sites(cfg) or {}).items():
        if not (src or {}).get("enabled", True):
            continue
        base = str((src or {}).get("base_url", "") or "").rstrip("/")
        if base:
            groups.setdefault(base, []).append(code)
    return {b: c for b, c in groups.items() if len(c) > 1}


def _warn_same_source(cfg: Dict[str, Any], sites_list: List[str]) -> List[Dict]:
    """挑出本次涉及「同源多 code」的组，给界面一句人话提醒。

    ⚠ 只看**启用中**的站点。用户把 zjwx-api 停用之后，它根本不参与
    同步，此时再提示「同源重复」就是误报 —— 而误报会训练用户忽略这条
    提示，真出事时也就一起忽略了。
    """
    from .. import sites as S
    allsrc = S.all_sites(cfg) or {}
    enabled = {c for c, s in allsrc.items()
               if (s or {}).get("enabled", True)}
    groups = _same_source_groups(cfg)
    out = []
    for base, codes in groups.items():
        hit = [c for c in codes if c in sites_list and c in enabled]
        if len(hit) > 1:
            names = [f"{(allsrc.get(c) or {}).get('name') or c}({c})"
                     for c in hit]
            out.append({"base_url": base, "codes": hit, "names": names,
                        "note": f"这几个站点指向同一个站 {base}，"
                                "抓到的是同一批稿件；同时处理会重复入库、"
                                "重复消耗 AI 配额。建议只留一个。"})
    return out


def _select_batch_articles(d, cfg: Dict[str, Any], req: BatchReq):
    """
    按站点逐站 search 再合并（db.search 只支持单 site 参数）。

    limit 口径（2026-10-11 改）：
        原来是**每站各取 limit 条**，合并后不二次截断 ——
        界面上写着「最多条数 10」、勾了 2 个站，实际产出 20 条，
        用户看到的数字和控件含义对不上（实测踩到）。
        现在改为**全局上限**：各站按日期倒序取 limit 条，
        合并去重后按 (日期, id) 排序再截到 limit。
        每站仍然平等地拿到候选名额（不会被单一大站挤光），
        但**产出总数 = 用户填的数**，控件含义与行为一致。

    ⚠ 跨站去重按 article id 不够：同源的两个 site code 入库后
      article id 不同，必须再按 url / 标题去一次，否则同一篇稿件
      仍会被摘要两次。

    增量过滤为什么放在选文章这一步：
      run_batch 的 resume 是「提交线程池之前的兜底过滤」，发生在它内部；但前端
      进度条的分母 total 是 POST 时按 merged 长度写进 job 的。若这里不过滤，
      incremental 模式下 total 会把「已有摘要、注定被 resume 跳过」的文章也算进
      分母，进度条永远停在 (total-skipped)/total 上，看着像卡死。在选文章阶段
      就把已有 summarize annotation 的剔除掉，total 从一开始就是真实待办数，
      进度条分母不虚胖。full 模式本来就是要重生成已有摘要的文章，所以不过滤。
      本函数会被请求线程（预检）和 _batch_worker（后台自己再 open_db 选一遍）
      各调一次，两次过滤口径必须一致，否则预检 total 与后台 total 对不上。
    """
    from .. import sites as S
    # 中断续跑：点名 ids 时只跑这几篇，忽略站点/日期/limit ——
    # 「只重试失败项」必须精确到篇，否则 limit 截断会把失败项挤掉，
    # 变成「重试了但没轮到它」。
    if req.ids:
        arts = []
        for i in req.ids:
            it = d.get_article(int(i), with_html=False)
            if it:
                arts.append(it)
        if req.mode == "incremental":
            arts = [a for a in arts
                    if d.get_annotation(a["id"], "summarize") is None]
        return [], arts

    sites_list = req.sites or list(S.enabled_codes(cfg))
    seen, merged = set(), []
    for s in sites_list:
        r = d.search(q="", site=s, column_name=req.column,
                     date_from=req.date_from, date_to=req.date_to,
                     sort="date_desc", limit=req.limit,
                     min_fts_chars=cfg["search"]["min_fts_chars"])
        for it in r["items"]:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            merged.append(it)

    # 跨站去重：同一篇稿件可能被多个 site code 各存一行。
    # article id 不同，所以要按 url 去；url 也可能因参数差异不同，
    # 再用「标题+日期」兜一层。只留每组的第一条（按日期倒序时靠前的）。
    by_key, dedup = {}, []
    for it in merged:
        key = (str(it.get("url") or "").rstrip("/").lower()
               or str(it.get("title") or "").strip() + "|"
               + str(it.get("pub_date") or "")[:10])
        if key in by_key:
            continue
        by_key[key] = it["id"]
        dedup.append(it)
    merged = dedup

    if req.mode == "incremental":
        # 主线程单连接下 get_annotation 无锁直查是安全的（db 层已核对）
        merged = [it for it in merged
                  if d.get_annotation(it["id"], "summarize") is None]

    # 全局截断：按日期倒序，让「每站平权」与「总数可控」同时成立
    merged.sort(key=lambda it: (str(it.get("pub_date") or ""), it["id"]),
                reverse=True)
    if req.limit and len(merged) > req.limit:
        merged = merged[:req.limit]
    return sites_list, merged


def _batch_worker(job_id: str, cfg_snapshot: Dict[str, Any], req: BatchReq):
    """
    后台线程体。必须自己 open_db：SQLite 是单连接 + 锁，跨线程共用主连接
    并发写必炸（docs/工程留痕.md §3.3 压测结论），绝不复用服务主连接 DB。
    异常吞进 job['error']：线程里抛异常只会静默死掉，界面会永远转圈。
    """
    job = BATCH_JOBS[job_id]
    d = None
    try:
        d = D.open_db(cfg_snapshot)
        sites_list, merged = _select_batch_articles(d, cfg_snapshot, req)
        arts = [d.get_article(it["id"]) for it in merged]
        with BATCH_LOCK:
            job["sites"] = sites_list
            job["progress"]["total"] = len(arts)
            job["logs"].append({"t": time.strftime("%H:%M:%S"),
                                "msg": f"选定 {len(arts)} 篇，开始"})

        from ..ai.batch import run_batch as _run_batch

        def _on_ev(ev):
            with BATCH_LOCK:
                job["progress"].update({
                    "done": ev.get("done", 0), "ok": ev.get("ok", 0),
                    "fail": ev.get("fail", 0), "skipped": ev.get("skipped", 0),
                    "total": ev.get("total", len(arts))})
                job["logs"].append({
                    "t": time.strftime("%H:%M:%S"),
                    "msg": f"[{ev.get('done', 0)}/{ev.get('total', 0)}] "
                           f"{str(ev.get('title', ''))[:40]}"})
                # 与同步日志同款封顶，长跑把内存撑爆
                if len(job["logs"]) > 400:
                    del job["logs"][:len(job["logs"]) - 400]

        summary = _run_batch(cfg_snapshot, d, arts, dry_run=req.dry_run,
                             concurrency=req.concurrency, rate=req.rate,
                             resume=req.resume,
                             stop_on_error=req.stop_on_error,
                             batch_id=job_id, on_progress=_on_ev)
        with BATCH_LOCK:
            job["summary"] = summary
            # 中断/失败要把「该怎么办」直接写进 job，前端照着显示即可，
            # 不用在前端重新推导这些语义。
            if summary.get("not_run"):
                job["logs"].append({
                    "t": time.strftime("%H:%M:%S"),
                    "msg": f"⚠ 批次中断：{summary['not_run']} 篇尚未处理"
                           f"（未调 AI，没有烧配额）。重新点「开始生成」"
                           f"会跳过已完成的，只跑剩下的。"})
            if summary.get("retriable"):
                job["logs"].append({
                    "t": time.strftime("%H:%M:%S"),
                    "msg": f"⚠ {summary['retriable']} 篇因限流/超时失败，"
                           f"属于可重试 —— 用「仅重试失败项」即可补跑。"})
            fatal = summary["fail"] - summary["retriable"]
            if fatal > 0:
                # ⚠ 原来的提示语把所有不可重试错误都说成「如 api_key、模型名」，
                # 实测误导：422 内容审查（供应商安全策略拦截）也落在这一类，
                # 照着去改 api_key 纯属白折腾。改为只陈述事实 + 指路分类。
                job["logs"].append({
                    "t": time.strftime("%H:%M:%S"),
                    "msg": f"⚠ {fatal} 篇失败且重试无效（内容审查拦截 / 鉴权 / "
                           f"配置等）。失败清单里已按类别标注，每类都写明了"
                           f"该做什么 —— 请照类别处理，不要一律去改配置。"})
    except Exception as e:
        with BATCH_LOCK:
            job["error"] = f"{type(e).__name__}: {e}"
    finally:
        if d is not None:
            try:
                d.close()
            except Exception:
                pass
        with BATCH_LOCK:
            job["running"] = False
            job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


@app.get("/api/ai/failures")
def api_ai_failures(task: str = "summarize",
                    retryable_only: bool = False,
                    date_from: str = "", date_to: str = "",
                    include_resolved: bool = False,
                    limit: int = Query(200, ge=1, le=1000)):
    """
    跨重启的失败清单。

    为什么要有这个接口：BATCH_JOBS 是**内存 dict**，进程一重启，
    刚才那批跑了多少、哪几篇失败、有没有中断，全都没了。
    失败留痕写在库里，所以重启后仍能回答「上次那批还欠哪几篇」，
    并据此只补跑它们 —— 而不是把整批重跑一遍再烧一次配额。

    默认**实时对账**：只返回「至今仍无摘要」的那些。
    date_from/date_to 与批量面板同一套选区口径 ——
    两边口径不一致时，界面上的数字和「补跑」真会跑到的篇数对不上。
    """
    d = _db()
    rows = d.list_ai_failures(task=task, retryable_only=retryable_only,
                              limit=limit, date_from=date_from,
                              date_to=date_to,
                              only_orphans=not include_resolved)
    stats = d.ai_failure_stats(task=task, date_from=date_from, date_to=date_to)
    out = []
    for r in rows:
        k = D.Database.classify_failure(r.get("error", ""), bool(r.get("retryable")))
        r["kind"] = k
        r["hint"] = D.Database.FAILURE_HINTS.get(k, "")
        out.append(r)
    return {"items": out, "count": len(out), "stats": stats,
            # 保留这两个顶层字段：外部脚本/旧前端可能还在读它们。
            # retryable 是「当前返回的这批里，有几条属可自动重试」——
            # 与 stats.retryable（待补跑中的可重试数）同口径，保留不冲突。
            "retryable": sum(1 for r in out if r.get("retryable")),
            "hints": D.Database.FAILURE_HINTS}


class ClearFailuresIn(BaseModel):
    # 默认只清「已经补上摘要」的那些 —— 安全的默认档。
    # all=True 会连真正还没补跑的一起清掉，必须前端二次确认。
    all: bool = False
    task: str = "summarize"
    ids: Optional[List[int]] = None


@app.post("/api/ai/failures/clear")
def api_ai_failures_clear(req: ClearFailuresIn):
    """
    清空失败清单，返回实际删掉的条数（逐项回报，不静默）。

    只删 ai_failures 的留痕行，**不动 ai_annotations** ——
    失败清单纯粹是「历史记账」，删它不会丢任何摘要。
    """
    d = _db()
    before = d.ai_failure_stats(task=req.task)
    n = d.clear_ai_failures(task=req.task, only_stale=not req.all,
                            article_ids=req.ids)
    after = d.ai_failure_stats(task=req.task)
    return {"deleted": n, "before": before, "after": after,
            "scope": "all" if req.all else "resolved_only",
            "note": "只清理失败留痕，未删除任何已生成的摘要。"}


class ManualAnnIn(BaseModel):
    """人工补录：给供应商内容审查拦下、或用户自己写了摘要的篇目。"""
    article_id: int
    task: str = "summarize"
    summary: str
    tags: Optional[List[str]] = None
    sentiment: str = "neutral"
    entities: Optional[List[str]] = None
    note: str = ""


@app.post("/api/ai/annotations/manual")
def api_ai_annotation_manual(req: ManualAnnIn):
    """
    人工补录摘要。provider 记为 manual，与模型生成的可区分。

    适用场景：模型因内容安全策略拒绝（422 content_blocked），
    重试一万次也一样。**本接口不调用任何模型**，纯写入。
    """
    if not req.summary.strip():
        raise HTTPException(400, {"message": "摘要内容不能为空", "refused": True})
    d = _db()
    art = d.get_article(req.article_id, with_html=False)
    if not art:
        raise HTTPException(404, {"message": f"文章 {req.article_id} 不存在",
                                  "refused": True})
    if art.get("content_empty"):
        raise HTTPException(400, {
            "message": "该文章正文为空，补录摘要没有意义", "refused": True})
    d.manual_annotation(
        req.article_id, req.task,
        {"summary": req.summary.strip(),
         "tags": req.tags or [],
         "sentiment": req.sentiment or "neutral",
         "entities": req.entities or [],
         "manual_note": req.note},
        provider="manual", model="human")
    return {"saved": True, "article_id": req.article_id,
            "title": art.get("title", ""),
            "cleared_failure": True,
            "note": "已写入并清除该篇的失败留痕；provider 记为 manual。"}


class RetryFailedIn(BaseModel):
    dry_run: bool = True
    concurrency: int = 3
    rate: float = 0.0
    # 保留字段名与语义，但它现在只影响「挑哪些篇」，
    # 不再决定界面上那个按钮的数字 —— 那个数字由实时对账算。
    retryable_only: bool = True
    limit: int = Query(100, ge=1, le=1000)


@app.post("/api/ai/retry-failed")
def api_ai_retry_failed(req: RetryFailedIn):
    """只重跑失败清单里的文章 —— 中断续跑的正确姿势。

    retryable_only=True 时只挑「值得重试」的（429/超时/5xx）。
    内容审查拦截、鉴权失败、模型名错误这类重试一万次也一样，直接跳过，
    否则每次点「重试」都是白烧一轮额度还依然失败。

    ⚠ 这里不再按 retryable 标记来算「还欠几篇」—— 那个数字一律来自实时对账
    （没摘要才算欠）。原先按历史标记算，实测出现 58 篇 vs 实际 1 篇的离谱差异。
    """
    cfg = _cfg()
    d = _db()
    rows = d.list_ai_failures(task="summarize",
                              retryable_only=req.retryable_only,
                              limit=req.limit)
    arts = []
    for r in rows:
        a = d.get_article(r["article_id"], with_html=False)
        if a:
            arts.append(a)
    stats = d.ai_failure_stats(task="summarize")
    if not arts:
        raise HTTPException(400, {
            "message": (f"没有可重试的失败项（共 {stats['pending']} 篇待补跑，"
                        f"其中可自动重试 {stats['retryable']} 篇）。"
                        "若是内容审查拦截，请用人工补录；"
                        "若是鉴权/模型名问题，请先改配置。"),
            "refused": True, "stats": stats})
    return {"selected": len(arts),
            "article_ids": [a["id"] for a in arts],
            "preview": [{"id": a["id"], "title": a.get("title")}
                        for a in arts[:20]],
            "stats": stats,
            "dry_run": req.dry_run,
            "note": "把 article_ids 交给批量摘要接口的 ids 参数即可只跑这些"}


@app.post("/api/ai/summarize-batch")
def api_ai_summarize_batch(req: BatchReq):
    """启动一次批量摘要。立刻返回 job_id，实际工作在后台线程。"""
    cfg = _cfg()
    d = _db()
    # 预检放请求线程：要在响应里就给出 400（一篇都选不到时）。这里只用主
    # 连接做一次只读 search（读也持锁，和 /api/search 一样安全）；后台线程
    # 会用自己的连接再选一遍，不共享游标。
    try:
        sites_list, merged = _select_batch_articles(d, cfg, req)
    except Exception as e:
        raise HTTPException(500, f"选文章失败: {type(e).__name__}: {e}")
    if not merged:
        raise HTTPException(400, "没有可选到文章（换站点/时间范围，或调大 limit）")

    # 同源站点提示：zjwx 与 zjwx-api 指向同一个 base_url，
    # 同时勾上会让同一批稿件重复处理。前端要能显示出来。
    same_src = _warn_same_source(cfg, sites_list)

    job_id = uuid.uuid4().hex
    with BATCH_LOCK:
        BATCH_JOBS[job_id] = {
            "running": True,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "mode": req.mode,
            "progress": {"done": 0, "ok": 0, "fail": 0,
                         "skipped": 0, "total": len(merged)},
            "summary": None, "error": "", "logs": [],
            "dry_run": req.dry_run, "sites": sites_list,
            "same_source": same_src,
            # 把口径说清楚，省得用户对着「10」和「20」猜
            "limit": req.limit,
        }
    # 传副本：后台线程不能引用会被 /api/config 改写的 CFG（与 _sync_worker 同因）
    cfg_snapshot = json.loads(json.dumps(cfg))
    t = threading.Thread(target=_batch_worker,
                         args=(job_id, cfg_snapshot, req), daemon=True)
    t.start()
    return {"job_id": job_id, "started": True, "total": len(merged),
            "mode": req.mode, "dry_run": req.dry_run,
            "limit": req.limit, "same_source": same_src}


@app.get("/api/ai/summarize-batch/{job_id}")
def api_ai_summarize_batch_status(job_id: str):
    """轮询批次进度。job 不存在 404。"""
    with BATCH_LOCK:
        job = BATCH_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return dict(job)


# ---------------------------------------------------------------- 同步
# 同步是长任务（首次全量 ~20 分钟），绝不能跑在请求线程里，
# 否则一个 POST /api/sync 会把整个服务卡死。
# 用单后台线程 + 共享状态，前端轮询 /api/sync/status。
# ⚠ 必须是 RLock，不能用 Lock。
# Lock 不可重入：本线程再进一次 with 就永久阻塞，而且**只有本线程知道**——
# 服务不崩、别的接口照常响应，只有碰这把锁的端点悄悄死掉，
# 表现就是「点了开始同步一点反应都没有」。
# 这里必须可重入的原因：临界区里调了同样要加锁的 _sync_log()（登记日志）。
# 两层保险：①锁用 RLock，②别在临界区里调加锁的函数。
SYNC_LOCK = threading.RLock()
SYNC_STATE: Dict[str, Any] = {
    "running": False, "started_at": "", "finished_at": "",
    "mode": "", "sites": [], "year": "",
    "logs": [], "totals": {"listed": 0, "new": 0, "failed": 0},
    "columns": [], "skipped": 0, "skip_kinds": {},
    "error": "", "cancel_requested": False,
}
MAX_LOG = 400          # 防止长跑把内存撑爆，只留最近若干条


def _sync_log(msg: str, **kw):
    with SYNC_LOCK:
        SYNC_STATE["logs"].append(
            {"t": time.strftime("%H:%M:%S"), "msg": msg, **kw})
        if len(SYNC_STATE["logs"]) > MAX_LOG:
            del SYNC_STATE["logs"][:len(SYNC_STATE["logs"]) - MAX_LOG]


def _sync_worker(cfg, site_list, mode, date_start, date_end, limit):
    """后台线程体。异常必须吞进状态里 —— 线程里抛异常只会静默死掉，
    界面那边会永远转圈，看起来像「点了没反应」。"""
    from ..collector import runner as RUN
    try:
        RUN.sync(cfg, sites=site_list, mode=mode, limit=limit,
                 verbose=False, date_start=date_start, date_end=date_end,
                 on_progress=_on_sync_event)
    except Exception as e:
        with SYNC_LOCK:
            SYNC_STATE["error"] = f"{type(e).__name__}: {e}"
        _sync_log(f"!! 出错：{type(e).__name__}: {e}", level="error")
    finally:
        with SYNC_LOCK:
            SYNC_STATE["running"] = False
            SYNC_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync_log("同步结束。")


def _on_sync_event(ev: Dict[str, Any]):
    k = ev.get("kind")
    if k == "log":
        _sync_log(ev.get("msg", ""),
                  level="warn" if "！" in ev.get("msg", "") else "info")
    elif k == "progress":
        with SYNC_LOCK:
            SYNC_STATE["totals"] = {"listed": ev.get("listed", 0),
                                    "new": ev.get("new", 0),
                                    "failed": ev.get("failed", 0)}
    elif k == "site_start":
        with SYNC_LOCK:
            SYNC_STATE["columns"] = []
    elif k == "column":
        # 栏目级进度：同步时能看到「卡在哪个栏目上」，
        # 而不是只看到一个转圈的总数
        with SYNC_LOCK:
            SYNC_STATE["columns"].append({
                "site": ev.get("site", ""),
                "name": ev.get("column", ""),
                "listed": ev.get("n", 0),
                "state": "running",
            })
    elif k == "column_done":
        with SYNC_LOCK:
            for c in reversed(SYNC_STATE["columns"]):
                if (c["site"] == ev.get("site")
                        and c["name"] == ev.get("column")):
                    c["state"] = "done"
                    c["new"] = ev.get("new", 0)
                    c["failed"] = ev.get("failed", 0)
                    break
    elif k == "site_done":
        with SYNC_LOCK:
            for c in SYNC_STATE["columns"]:
                if c["site"] == ev.get("site") and c["state"] == "running":
                    c["state"] = "done"
            SYNC_STATE["skipped"] = ev.get("skipped", 0)
            SYNC_STATE["skip_kinds"] = ev.get("skip_kinds", {})
        _sync_log(f"--- {ev.get('site')} 完成：列表 {ev.get('listed')}，"
                  f"新增 {ev.get('new')}，失败 {ev.get('failed')}"
                  + (f"，跳过 {ev.get('skipped')}"
                     if ev.get("skipped") else ""),
                  level="ok")
    elif k == "done":
        with SYNC_LOCK:
            SYNC_STATE["totals"] = {"listed": ev.get("listed", 0),
                                    "new": ev.get("new", 0),
                                    "failed": ev.get("failed", 0)}


@app.post("/api/sync")
def api_sync_start(
    sites: Optional[str] = "",      # 逗号分隔，空=全部启用中的站
    mode: str = Query("incremental", pattern="^(incremental|full|list-only)$"),
    year: str = "",
    from_year: str = "",
    to_year: str = "",
    limit: Optional[int] = Query(None, ge=1, le=100000),
):
    """启动一次同步。立刻返回，实际工作在后台线程。"""
    with SYNC_LOCK:
        if SYNC_STATE["running"]:
            raise HTTPException(409, "已有同步在跑，先等它结束或点停止")
        site_list = [s.strip() for s in sites.split(",") if s.strip()] or None
        drop_notes = []      # 锁外统一写日志，锁内只攒字串
        if site_list:
            # 站点表是动态的（用户可以自己加站），不能写死 cac|zjwx，
            # 否则新站点点了同步直接被 400 挡回去
            from .. import sites as S
            known = set(S.all_sites(_cfg()).keys())
            bad = [s for s in site_list if s not in known]
            if bad:
                raise HTTPException(400, f"未知站点: {bad}")
            # 同源站点拦截：同一站配了多个 code（zjwx + zjwx-api）时，
            # 同时同步会把同一批稿件**各存一行**，之后再也无法靠
            # (site,url) 唯一约束合并掉 —— 因为 url 相同但 site 不同。
            # 这是重复入库的源头，比在摘要阶段拦更早、更干净。
            dup_src = _warn_same_source(_cfg(), site_list)
            if dup_src:
                names = "、".join(dup_src[0]["names"])
                raise HTTPException(400, {
                    "message": f"{names} 指向同一个站（{dup_src[0]['base_url']}），"
                               "同时同步会把同一批稿件重复入库。"
                               "请只勾选其中一个。",
                    "same_source": dup_src, "refused": True})
        else:
            # 走默认（全部启用站点）同样会撞上同源 —— 用户没勾选，
            # 但 zjwx-api 处于启用状态就会被一并同步。
            # 这里不硬拦（那会变成「一个都没勾就同步失败」的反直觉），
            # 改成自动剔除同源的后来者，并在日志里说明。
            from .. import sites as S
            cfg_now = _cfg()
            eff = list(S.enabled_codes(cfg_now))
            drop = {}
            for base, codes in _same_source_groups(cfg_now).items():
                if len(codes) > 1:
                    for c in codes[1:]:
                        drop[c] = codes[0]
            if drop:
                eff = [c for c in eff if c not in drop]
                # 只在临界区内攒字串，真正写日志挪到锁外（见下方）
                drop_notes = [
                    f"⚠ 已自动跳过同源重复站点 {c}"
                    f"（它与 {keep} 指向同一个网站，"
                    f"同时同步会把同一批稿件存成两行）"
                    for c, keep in sorted(drop.items())
                ]

        # 显式勾选时 site_list 原样传；默认路径用剔除同源后的 eff。
        # ⚠ 这一步必须真的把 eff 传下去 —— 上面算 eff 只是为了生成日志，
        # 若仍传 site_list（None），后台又会把 zjwx-api 一并同步，
        # 「自动跳过同源」就成了一句空话。
        run_sites = site_list or eff

        ds = de = None
        if year:
            ds, de = f"{int(year)}-01-01", f"{int(year)}-12-31"
        elif from_year or to_year:
            ds = f"{int(from_year)}-01-01" if from_year else None
            de = f"{int(to_year)}-12-31" if to_year else None

        SYNC_STATE.update({
            "running": True, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "", "mode": mode,
            "sites": run_sites if site_list else ["全部"],
            "year": year or f"{from_year or ''}~{to_year or ''}",
            "logs": [], "error": "", "cancel_requested": False,
            "totals": {"listed": 0, "new": 0, "failed": 0},
        })
    # 以下两处 _sync_log 都在临界区之外：临界区里调加锁的函数会自锁
    for note in drop_notes:
        _sync_log(note)
    _sync_log(f"开始同步：模式 {mode}"
              + (f"，年份 {year}" if year else "")
              + (f"，站点 {','.join(site_list)}" if site_list
                 else f"，全部站点（已剔除同源：{','.join(run_sites)}）"))

    # 传副本：后台线程不能引用服务里那份会被 /api/config 改写的 CFG
    cfg_snapshot = json.loads(json.dumps(_cfg()))
    t = threading.Thread(target=_sync_worker,
                         args=(cfg_snapshot, run_sites, mode, ds, de, limit),
                         daemon=True)
    t.start()
    return {"started": True, "mode": mode, "year": year or None,
            "sites": run_sites if site_list else "all",
            "effective_sites": run_sites}


@app.get("/api/sync/status")
def api_sync_status(since: int = Query(0, ge=0)):
    """轮询进度。since=已收到的日志条数，只回增量，前端就不用每次重拉全部。"""
    with SYNC_LOCK:
        logs = SYNC_STATE["logs"][since:]
        return {
            "running": SYNC_STATE["running"],
            "started_at": SYNC_STATE["started_at"],
            "finished_at": SYNC_STATE["finished_at"],
            "mode": SYNC_STATE["mode"],
            "sites": SYNC_STATE["sites"],
            "year": SYNC_STATE["year"],
            "totals": dict(SYNC_STATE["totals"]),
            "columns": list(SYNC_STATE["columns"]),
            "skipped": SYNC_STATE["skipped"],
            "skip_kinds": dict(SYNC_STATE["skip_kinds"]),
            "error": SYNC_STATE["error"],
            "log_total": len(SYNC_STATE["logs"]),
            "logs": logs,
        }


@app.get("/api/sync/runs")
def api_sync_runs(limit: int = Query(10, ge=1, le=100)):
    """历史同步批次"""
    return {"items": _db().recent_sync_runs(limit)}


@app.get("/api/doctor")
def api_doctor(ai: bool = False, advisory: bool = False):
    """
    接口自检，返回结构化结果（不是只吐一段文本，界面要分栏渲染）。

    ai=True        对「有问题的站点」做根因分析（原来只有 CLI 有，
                   界面点不到，等于没有）
    advisory=True  无论接口好坏都做一次数据健康评估 —— 全绿时也能问 AI
                   「这批数据能不能用于研究、下一步该采什么」

    两条 AI 链路都是**补充**：AI 没配置或调用失败都不影响规则层结论。
    """
    from ..collector import runner as RUN
    cfg = _cfg()
    d = _db()
    reports = []
    t_all = time.time()
    for s in RUN.COLLECTORS:
        col = RUN.get_collector(cfg, s, verbose=False)
        if col is None:
            continue
        t0 = time.time()
        try:
            rep = col.health_check()
        except Exception as e:
            rep = {"ok": False, "issues": [f"自检异常: {type(e).__name__}: {e}"],
                   "checked_columns": 0}
        rep["elapsed_ms"] = int((time.time() - t0) * 1000)
        rep["site"] = s
        rep["site_name"] = col.name
        reports.append(rep)

    f = d.facets()
    bad = [r for r in reports if not r.get("ok")]
    out = {
        "ok": not bad,
        "elapsed_ms": int((time.time() - t_all) * 1000),
        "reports": reports,
        "data": {"total": f["total"], "chars": f["chars"], "days": f["days"],
                 "empty": f["empty"], "external": f["external"]},
        "ai": None,
        "ai_note": "",
    }

    st = ai_status(cfg)
    if (ai or advisory) and not st.get("ready"):
        out["ai_note"] = "AI 未就绪，跳过 AI 辅助（规则层结论不受影响）"
    elif ai and bad:
        try:
            out["ai"] = ai_diagnose(cfg, bad, d)
            if out["ai"] is None:
                out["ai_note"] = "AI 未返回结果"
        except Exception as e:
            out["ai_note"] = f"AI 调用失败: {type(e).__name__}"
    elif advisory:
        try:
            out["ai"] = advisory_run(cfg, d, reports, d.recent_sync_runs(5))
            if out["ai"] is None:
                out["ai_note"] = "AI 未返回结果"
        except Exception as e:
            out["ai_note"] = f"AI 调用失败: {type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------- 站点管理
class ProbeIn(BaseModel):
    """输网址 → 推断规则（不落盘）"""
    url: str = ""
    name: str = ""
    use_ai: bool = True


class ProbeRunIn(BaseModel):
    """用候选配置真抓一次（不落盘、不入库）"""
    draft: Dict[str, Any] = {}
    max_columns: int = 5


class SiteSaveIn(BaseModel):
    """保存站点配置。只写 config/sites.d/<code>.json，不碰 settings.json。"""
    draft: Dict[str, Any] = {}
    force: bool = False


@app.get("/api/sites")
def api_sites():
    """站点清单：内置 + 自定义，附各自已入库篇数"""
    from .. import sites as S
    cfg = _cfg()
    d = _db()
    counts = {}
    with d._lock:
        for r in d.conn.execute(
                "SELECT site, COUNT(*) n, COALESCE(SUM(chars),0) c, "
                "COUNT(DISTINCT column_name) nc FROM articles "
                "GROUP BY site"):
            counts[r["site"]] = {"n": r["n"], "chars": r["c"],
                                 "n_columns": r["nc"]}
    return {"items": S.describe(cfg, counts),
            "dir": str(S.CUSTOM_DIR)}


@app.post("/api/sites/probe")
def api_sites_probe(req: ProbeIn):
    """
    推断规则，不保存。返回草稿 + 证据 + 问题清单。
    这是**第一段**：先看它推了什么，再决定要不要真抓。
    """
    from ..collector import probe as P
    if not req.url.strip():
        raise HTTPException(400, "请填写网址")
    try:
        return P.draft_from_url(_cfg(), req.url, name=req.name,
                                use_ai=req.use_ai)
    except Exception as e:
        raise HTTPException(502, f"推断失败：{type(e).__name__}: {e}")


@app.post("/api/sites/probe-run")
def api_sites_probe_run(req: ProbeRunIn):
    """
    用候选配置真抓一次。**这是第二段**：给用户看样本。
    全程只读：不写数据库、不写配置文件。
    """
    from ..collector import probe as P
    draft = dict(req.draft or {})
    if not draft:
        raise HTTPException(400, "没有待验证的配置草稿")
    try:
        return P.probe_site(_cfg(), draft, max_columns=max(1, req.max_columns))
    except Exception as e:
        raise HTTPException(502, f"试抓失败：{type(e).__name__}: {e}")


@app.post("/api/sites")
def api_sites_save(req: SiteSaveIn):
    """确认保存。只写 sites.d/<code>.json。"""
    from .. import sites as S
    draft = dict(req.draft or {})
    issues = S.validate(draft)
    if issues and not req.force:
        raise HTTPException(400, {"message": "配置校验没过", "issues": issues})
    try:
        path = S.save_custom(draft)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"saved": True, "file": str(path), "code": draft.get("code"),
            "forced": bool(req.force and issues),
            "issues": issues}


@app.delete("/api/sites/{code}")
def api_sites_delete(code: str):
    """删除自定义站点配置。**不动已入库的文章** —— 那是另一件事。"""
    from .. import sites as S
    if code in (C.load().get("sites") or {}):
        raise HTTPException(400, "内置站点不可删除，请到设置页停用或改采集范围")
    ok = S.delete_custom(code)
    return {"deleted": ok, "code": code,
            "note": "已入库的文章保留在库里，只是不会再同步"}


@app.post("/api/sites/{code}/enabled")
def api_sites_toggle(code: str, enabled: bool = True):
    from .. import sites as S
    return {"ok": S.set_enabled(code, enabled), "code": code, "enabled": enabled}


# ---------------------------------------------------------------- 占位条目
@app.get("/api/placeholders")
def api_placeholders(
    site: str = "", column_name: str = "", year: str = "", q: str = "",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0)):
    """
    占位条目：列表抓到、正文没入库的文章。

    ⚠ 这里**不含**导航页/站外链接/公众号/脏数据 —— 它们被 classify.py
    判成 SKIP_KINDS，根本不进 articles 表，记在 external_links 里。
    「从来不是文章」与「是文章但正文没拿到」是两回事，
    混在一起会让「缺口有多少」这个数字失去意义。
    """
    from ..placeholder import list_placeholders
    return list_placeholders(DB, site=site, column_name=column_name,
                             year=year, q=q, limit=limit, offset=offset)


class BackfillIn(BaseModel):
    article_ids: List[int]
    dry_run: bool = True


@app.post("/api/backfill")
def api_backfill(req: BackfillIn):
    """
    按需补抓指定几篇的正文。

    ⚠ 同 purge 的两阶段思路：dry_run 默认 True，先看这几篇能不能补上，
    再 dry_run=false 真抓。**只接受点名的 id**，不给「一键补全某站」——
    70 篇全量补抓若源站已改版，只会得到一片失败并让人误判站点坏了。
    """
    from ..placeholder import backfill
    if len(req.article_ids) > 50:
        raise HTTPException(400, "一次最多补抓 50 篇")
    events: List[Dict[str, Any]] = []
    try:
        return backfill(CFG, DB, req.article_ids, dry_run=req.dry_run,
                        on_event=lambda k, kw: events.append({"kind": k, **kw}))
    except Exception as e:
        raise HTTPException(502, f"补抓失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 批量删除
class PurgeIn(BaseModel):
    """删除条件。字段名与 purge.py 的 filters 一一对应。

    ⚠ 白名单刻意收窄：只接受能落到明确列上的条件，
    不接受任何形如 "sql" / "where" 的自由文本 ——
    那等于把 DELETE 的 WHERE 交给前端填。"""
    target: str
    site: Optional[str] = None
    site_like: Optional[str] = None
    column_name: Optional[str] = None
    year: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    q: Optional[str] = None
    chars_below: Optional[int] = None
    empty_content_only: Optional[bool] = None
    before_synced_at: Optional[str] = None
    ids: Optional[List[int]] = None
    # annotations
    task: Optional[str] = None
    provider: Optional[str] = None
    result_like: Optional[str] = None
    created_before: Optional[str] = None
    # external_links
    fail_kind: Optional[str] = None
    is_own_site: Optional[bool] = None
    # 执行闸门
    confirm_token: Optional[str] = None
    yes: bool = False

    def filters(self) -> Dict[str, Any]:
        f = self.model_dump()
        for k in ("target", "confirm_token", "yes"):
            f.pop(k, None)
        # 统一交给 purge._clean：False 与空串/空列表一样都是「没给条件」，
        # 两处各写一套过滤逻辑必然走偏（历史上已经错过一次）。
        from ..purge import _clean
        return _clean(f)


@app.post("/api/purge/preview")
def api_purge_preview(req: PurgeIn):
    """删除前的影响面预览。**只读，不动任何数据。**"""
    from ..purge import PurgeRefused, preview_purge
    try:
        return preview_purge(DB, req.target, **req.filters())
    except PurgeRefused as e:
        # 400 而不是 500：这是「按规则拒绝」，属于正常业务结果，
        # 前端要把它当可读提示展示，不是崩溃。
        raise HTTPException(400, {"message": str(e), "refused": True})


@app.post("/api/purge/execute")
def api_purge_execute(req: PurgeIn):
    """真删。必须同时满足：yes=true + 带 preview 返回的 confirm_token。"""
    from ..purge import PurgeRefused, execute_purge
    if not req.yes:
        raise HTTPException(400, {"message":
            "未确认：删除是不可逆操作，需显式传 yes=true",
            "refused": True})
    if not req.confirm_token:
        raise HTTPException(400, {"message":
            "缺少 confirm_token，请先调用 /api/purge/preview 看清影响面",
            "refused": True})
    try:
        return execute_purge(DB, req.target, req.confirm_token,
                             **req.filters())
    except PurgeRefused as e:
        raise HTTPException(400, {"message": str(e), "refused": True})


# ---------------------------------------------------------------- 配置
class ConfigIn(BaseModel):
    """只接受这些字段。白名单是刻意的：
    开放任意路径写入等于开放任意配置项被前端改写，
    包括 collector.user_agent 这种会影响采集行为的项。"""
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    clear_key: bool = False       # 显式清除密钥，与「留空=不修改」区分开
    enabled: Optional[bool] = None
    timeout: Optional[int] = None
    max_tokens: Optional[int] = None
    # 自动重试：默认开启。设 1 = 关闭（不重试）。
    retry_attempts: Optional[int] = None
    retry_base_delay: Optional[float] = None
    # 送进模型的正文截断字数；**0 或留空 = 不截断**。
    max_content_chars: Optional[int] = None
    page_size: Optional[int] = None
    host: Optional[str] = None
    port: Optional[int] = None
    date_start_cac: Optional[str] = None
    date_end_cac: Optional[str] = None
    date_start_zjwx: Optional[str] = None
    date_end_zjwx: Optional[str] = None


@app.get("/api/config")
def api_config_get():
    """读当前配置。密钥只给「有没有 / 长度」，绝不给明文或掩码原文。"""
    global CFG
    cfg = C.load()
    CFG = cfg
    ai = cfg.get("ai", {})
    key = ai.get("api_key", "") or ""
    return {
        "ai": {
            "enabled": bool(ai.get("enabled")),
            "provider": ai.get("provider", "none"),
            "base_url": ai.get("base_url", ""),
            "model": ai.get("model", ""),
            # 数值项回显**当前生效值**（未配置过时即默认值），
            # 这样用户不必翻文档就知道「不填时是多少」。
            "max_tokens": ai.get("max_tokens"),
            "timeout": ai.get("timeout"),
            "retry_attempts": ai.get("retry_attempts"),
            "retry_base_delay": ai.get("retry_base_delay"),
            "max_content_chars": ai.get("max_content_chars"),
            # 只有这两个字段，绝不回显 key 本身
            "has_key": bool(key),
            "key_len": len(key),
        },
        "app": {
            "host": cfg["app"].get("host"),
            "port": cfg["app"].get("port"),
            "page_size": cfg["app"].get("page_size"),
        },
        "sites": {
            k: {"date_start": v.get("date_start"), "date_end": v.get("date_end"),
                "enabled": v.get("enabled")}
            for k, v in cfg["sites"].items()
        },
        "search": cfg.get("search", {}),
        "settings_file": str(C.SETTINGS_FILE),
    }


@app.post("/api/config")
def api_config_set(req: ConfigIn):
    """写配置。空字符串的 api_key 表示「不修改」，避免前端回显为空就误清密钥。"""
    global CFG
    cfg = C.load()
    changed = []

    def put(path, val):
        if val is None:
            return
        if C.get_path(cfg, path) != val:
            changed.append(path)
        C.set_path(cfg, path, val)

    if req.provider is not None:
        if req.provider not in ("none", "openai_compat", "ollama"):
            raise HTTPException(400, "provider 只能是 none / openai_compat / ollama")
        put("ai.provider", req.provider)
    put("ai.base_url", req.base_url)
    put("ai.model", req.model)
    # 密钥的三态语义：
    #   非空且非清除标记 → 写入新 key
    #   空字符串          → 不修改（前端不回显，留空是常态）
    #   清除标记          → 显式清空
    # 用独立的 clear_key 字段承载「清除」，而不是拿哨兵值冒充 key ——
    # 哨兵法有个真实的坑：万一哪天有合法 key 恰好以该前缀开头，
    # 就会被无声清掉，且用户完全不知情。
    if getattr(req, "clear_key", False):
        put("ai.api_key", "")
    elif req.api_key:
        put("ai.api_key", req.api_key)
    if req.enabled is not None:
        put("ai.enabled", req.enabled)
    put("ai.timeout", req.timeout)
    put("ai.max_tokens", req.max_tokens)
    put("ai.retry_attempts", req.retry_attempts)
    put("ai.retry_base_delay", req.retry_base_delay)
    # max_content_chars 的 0 是有意义的取值（=不截断），
    # 不能像别的字段那样把 0/None 当「没改」跳过。
    if req.max_content_chars is not None:
        put("ai.max_content_chars", max(0, int(req.max_content_chars)))
    put("app.page_size", req.page_size)
    put("app.host", req.host)
    put("app.port", req.port)
    put("sites.cac.date_start", req.date_start_cac)
    put("sites.cac.date_end", req.date_end_cac)
    put("sites.zjwx.date_start", req.date_start_zjwx)
    put("sites.zjwx.date_end", req.date_end_zjwx)

    if not changed:
        return {"saved": False, "changed": [], "message": "没有变化"}
    C.save(cfg)
    CFG = cfg          # 让服务立即用新配置
    # 只报改了哪些字段名，不回显值 —— 值里可能含密钥
    return {"saved": True, "changed": changed, "message": "已保存"}


@app.get("/api/config/import/template")
def api_import_template():
    """空白模板，下载后填好再导入"""
    return {"template": C.IMPORT_TEMPLATE}


class ImportIn(BaseModel):
    text: str                       # 粘贴的 JSON 内容
    apply: bool = False             # False = 只预览，不写盘
    fields: Optional[str] = "ai.api_key,ai.base_url,ai.model"


def _scan_view(res: Dict[str, Any]) -> Dict[str, Any]:
    """把扫描结果整理成**可安全回传**的视图。
    绝不回传 value —— 只给掩码和来源路径。"""
    cur = _cfg()
    out = []
    for target in ("ai.api_key", "ai.base_url", "ai.model"):
        c = res["candidates"].get(target)
        old = C.get_path(cur, target) or ""
        if c:
            out.append({
                "field": target,
                "label": {"ai.api_key": "密钥", "ai.base_url": "接口地址",
                          "ai.model": "模型"}[target],
                "found": True,
                "masked": C.mask_secret(c["value"]) if target == "ai.api_key"
                          else c["value"],
                "from": c["from"],
                "why": c["why"],
                "changed": old != c["value"],
            })
        else:
            out.append({
                "field": target,
                "label": {"ai.api_key": "密钥", "ai.base_url": "接口地址",
                          "ai.model": "模型"}[target],
                "found": False,
                "masked": (C.mask_secret(old) if target == "ai.api_key" and old
                           else old) or "",
                "changed": False,
            })
    return {"items": out, "warnings": res["warnings"]}


@app.post("/api/config/import")
def api_import(req: ImportIn):
    """
    从 MCP / 其它 AI 工具的配置里导入 API 设置。

    两段式：**先预览（apply=false）看清会改什么，确认后才写盘**。
    导入配置天然容易误伤（字段名猜错、把图床 key 当 API key），
    不给预览就等于让人盲写 settings.json。
    """
    global CFG
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "内容为空")
    try:
        src = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(
            400, f"不是合法 JSON：第 {e.lineno} 行 {e.msg}。"
                 f"（常见原因：从配置文件里只复制了一部分，缺外层大括号）")

    # 情形 1：本身就是 cacx 配置 → 整体合并（不走字段扫描）
    if isinstance(src, dict) and "ai" in src and isinstance(src.get("ai"), dict):
        if req.apply:
            cfg = C.load()
            cfg = C._deep_merge(cfg, src)
            C.save(cfg)
            CFG = cfg
        ai = src.get("ai", {})
        return {"kind": "cacx", "applied": req.apply,
                "will_import": sorted(k for k, v in ai.items()
                                      if not isinstance(v, dict) and v),
                "message": "识别为 cacx 配置，将整体合并 ai 段"
                           + ("（已写入）" if req.apply else "（仅预览）")}

    res = C.scan_credentials(src)
    view = _scan_view(res)
    if not req.apply:
        return {"kind": "scan", "applied": False, **view}

    fields = [f.strip() for f in (req.fields or "").split(",") if f.strip()]
    changed, err = C.apply_candidates(res["candidates"], fields)
    if err:
        raise HTTPException(500, err)
    CFG = C.load()
    # 只报字段名，不报值 —— 值里含密钥
    return {"kind": "scan", "applied": True, "changed": changed,
            "message": ("已导入：" + ", ".join(changed)) if changed
                       else "没有变化（与当前配置相同）",
            **view}


@app.get("/api/config/status")
def api_config_status():
    """给设置页做「配齐了没」的自检。
    这里刻意把 ai_status() 里的掩码 key 换成布尔值：前端只需要知道
    「有没有 key」，不需要知道末四位，避免掩码原文经接口外流。"""
    cfg = _cfg()
    s = ai_status(cfg)
    s.pop("api_key", None)
    s["has_key"] = bool(cfg.get("ai", {}).get("api_key", ""))
    return s


# ---------------------------------------------------------------- 页面
@app.get("/", response_class=HTMLResponse)
def index():
    p = STATIC_DIR / "index.html"
    if p.is_file():
        return FileResponse(p, media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>cacx</h1><p>前端文件缺失。</p>", status_code=200)


def run(cfg: Dict, host: str = "127.0.0.1", port: int = 8770,
        open_browser: Optional[bool] = None):
    import uvicorn
    global CFG
    CFG = cfg
    if open_browser is None:
        open_browser = cfg.get("app", {}).get("open_browser", True)
    url = f"http://{host}:{port}/"
    print("=" * 60)
    print(f"  cacx 检索服务已启动: {url}")
    print(f"  文档: {url}docs")
    print("  Ctrl+C 停止")
    print("=" * 60)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host=host, port=port, log_level="warning")
