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

    GET  /api/ai/failures      跨重启的 AI 失败清单（限流/超时/配置类）
    POST /api/ai/retry-failed  只重跑「可重试」的失败项，不重跑整批

约定
    · 只读接口一律不写库；写操作（AI 落库）需显式 ?commit=1
    · 原文跳转走本服务中转，方便记录「哪篇被检索后跳走了」
"""

import json
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel

from .. import config as C
from .. import db as D
from ..ai import advisory as advisory_run
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
    only_empty: bool = False,
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
        # only_empty 与 skip_empty 是互斥的两种视图，
        # 同时传以 only_empty 为准（否则占位视图会被自己的过滤器清空）。
        skip_empty=(False if only_empty else skip_empty),
        only_empty=only_empty,
        sort=sort,
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
def api_facets(site: str = ""):
    """
    分面。site 非空时只返回该站栏目 —— 栏目下拉靠它跟随站点联动。
    不带参数时行为与从前完全一致（向后兼容）。
    """
    return _db().facets_by_site(site)


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
                job["logs"].append({
                    "t": time.strftime("%H:%M:%S"),
                    "msg": f"⚠ {fatal} 篇因配置/请求问题失败"
                           f"（如 api_key、模型名），重试无用，请先改配置。"})
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
                    limit: int = Query(200, ge=1, le=1000)):
    """
    跨重启的失败清单。

    为什么要有这个接口：BATCH_JOBS 是**内存 dict**，进程一重启，
    刚才那批跑了多少、哪几篇失败、有没有中断，全都没了。
    失败留痕写在库里，所以重启后仍能回答「上次那批还欠哪几篇」，
    并据此只补跑它们 —— 而不是把整批重跑一遍再烧一次配额。
    """
    d = _db()
    rows = d.list_ai_failures(task=task, retryable_only=retryable_only,
                              limit=limit)
    return {"items": rows, "count": len(rows),
            "retryable": sum(1 for r in rows if r.get("retryable"))}


class RetryFailedIn(BaseModel):
    dry_run: bool = True
    concurrency: int = 3
    rate: float = 0.0
    retryable_only: bool = True
    limit: int = Query(100, ge=1, le=1000)


@app.post("/api/ai/retry-failed")
def api_ai_retry_failed(req: RetryFailedIn):
    """只重跑失败清单里的文章 —— 中断续跑的正确姿势。

    retryable_only=True 时只挑「值得重试」的（429/超时/5xx）。
    鉴权失败、模型名错误这类重试一万次也一样，直接跳过，
    否则每次点「重试」都是白烧一轮额度还依然失败。
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
    if not arts:
        raise HTTPException(400, {
            "message": "没有可重试的失败项"
                       + ("（可重试清单为空；若是配置类失败，"
                          "请先改 api_key / 模型名再试）" if req.retryable_only
                          else ""),
            "refused": True})
    return {"selected": len(arts),
            "article_ids": [a["id"] for a in arts],
            "preview": [{"id": a["id"], "title": a.get("title")}
                        for a in arts[:20]],
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
SYNC_LOCK = threading.Lock()
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
                for c, keep in sorted(drop.items()):
                    _sync_log(f"⚠ 已自动跳过同源重复站点 {c}"
                              f"（它与 {keep} 指向同一个网站，"
                              f"同时同步会把同一批稿件存成两行）")

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