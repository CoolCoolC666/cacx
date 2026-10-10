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

约定
    · 只读接口一律不写库；写操作（AI 落库）需显式 ?commit=1
    · 原文跳转走本服务中转，方便记录「哪篇被检索后跳走了」
"""

import json
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    sort: str = "date_desc",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    cfg = _cfg()
    page_size = cfg["app"].get("page_size", 20)
    r = _db().search(
        q=q, site=site, column_name=column, year=year,
        date_from=date_from, date_to=date_to, min_chars=min_chars,
        sort=sort, limit=size, offset=(page - 1) * size,
        min_fts_chars=cfg["search"]["min_fts_chars"])
    r["page"] = page
    r["size"] = size
    r["pages"] = (r["total"] + size - 1) // size if size else 0
    r["page_size_default"] = page_size
    return r


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
    return RedirectResponse(url, status_code=307)


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


@app.get("/api/ai/status")
def api_ai_status():
    return ai_status(_cfg())


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