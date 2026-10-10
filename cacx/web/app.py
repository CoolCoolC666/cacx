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

约定
    · 只读接口一律不写库；写操作（AI 落库）需显式 ?commit=1
    · 原文跳转走本服务中转，方便记录「哪篇被检索后跳走了」
"""

import json
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel

from .. import config as C
from .. import db as D
from ..ai import diagnose as ai_diagnose
from ..ai import summarize as ai_summarize
from ..ai import status as ai_status

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
    sort: str = "date_desc",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    cfg = _cfg()
    page_size = cfg["app"].get("page_size", 20)
    d = _db()
    r = d.search(
        q=q, site=site, column_name=column, year=year,
        date_from=date_from, date_to=date_to, min_chars=min_chars,
        skip_empty=skip_empty, sort=sort,
        limit=size, offset=(page - 1) * size,
        min_fts_chars=cfg["search"]["min_fts_chars"])
    r["page"] = page
    r["size"] = size
    r["pages"] = (r["total"] + size - 1) // size if size else 0
    r["page_size_default"] = page_size
    # 命中片段 + 命中词，供列表高亮。埋点只记检索词，不记完整筛选条件
    # ——详情和 IP 这类信息没必要为了统计而留在库里。
    d.annotate_snippets(r["items"], q)
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
def api_facets():
    return _db().facets()


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
def api_events(days: int = Query(30, ge=1, le=365)):
    return _db().event_stats(days)


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


@app.get("/api/ai/status")
def api_ai_status():
    return ai_status(_cfg())


# ---------------------------------------------------------------- 同步
# 同步是长任务（首次全量 ~20 分钟），绝不能跑在请求线程里，
# 否则一个 POST /api/sync 会把整个服务卡死。
# 用单后台线程 + 共享状态，前端轮询 /api/sync/status。
SYNC_LOCK = threading.Lock()
SYNC_STATE: Dict[str, Any] = {
    "running": False, "started_at": "", "finished_at": "",
    "mode": "", "sites": [], "year": "",
    "logs": [], "totals": {"listed": 0, "new": 0, "failed": 0},
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
    elif k == "site_done":
        _sync_log(f"--- {ev.get('site')} 完成：列表 {ev.get('listed')}，"
                  f"新增 {ev.get('new')}，失败 {ev.get('failed')}",
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
        if site_list:
            bad = [s for s in site_list if s not in ("cac", "zjwx")]
            if bad:
                raise HTTPException(400, f"未知站点: {bad}")

        ds = de = None
        if year:
            ds, de = f"{int(year)}-01-01", f"{int(year)}-12-31"
        elif from_year or to_year:
            ds = f"{int(from_year)}-01-01" if from_year else None
            de = f"{int(to_year)}-12-31" if to_year else None

        SYNC_STATE.update({
            "running": True, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "", "mode": mode, "sites": site_list or ["全部"],
            "year": year or f"{from_year or ''}~{to_year or ''}",
            "logs": [], "error": "", "cancel_requested": False,
            "totals": {"listed": 0, "new": 0, "failed": 0},
        })
    _sync_log(f"开始同步：模式 {mode}"
              + (f"，年份 {year}" if year else "")
              + (f"，站点 {','.join(site_list)}" if site_list else "，全部站点"))

    # 传副本：后台线程不能引用服务里那份会被 /api/config 改写的 CFG
    cfg_snapshot = json.loads(json.dumps(_cfg()))
    t = threading.Thread(target=_sync_worker,
                         args=(cfg_snapshot, site_list, mode, ds, de, limit),
                         daemon=True)
    t.start()
    return {"started": True, "mode": mode, "year": year or None,
            "sites": site_list or "all"}


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
            "error": SYNC_STATE["error"],
            "log_total": len(SYNC_STATE["logs"]),
            "logs": logs,
        }


@app.get("/api/sync/runs")
def api_sync_runs(limit: int = Query(10, ge=1, le=100)):
    """历史同步批次"""
    return {"items": _db().recent_sync_runs(limit)}


@app.get("/api/doctor")
def api_doctor():
    """接口自检。放后台跑，因为要真打两站网络。"""
    import subprocess
    import sys
    # PROJECT_ROOT 定义在 config.py，不在 cacx/__init__.py —— 从错的地方导会 ImportError
    r = subprocess.run(
        [sys.executable, "-m", "cacx", "doctor"],
        cwd=str(C.PROJECT_ROOT), capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace")
    return {"rc": r.returncode, "out": (r.stdout or "") + (r.stderr or "")}


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