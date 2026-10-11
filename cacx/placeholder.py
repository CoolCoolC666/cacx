# -*- coding: utf-8 -*-
"""
占位条目：列表抓到、正文没入库的文章
================================================================
为什么需要它
----------------------------------------------------------------
采集是「先列列表 → 再逐篇抓正文」的两段式。于是存在第三种状态：

    列表里看到了（标题/日期/栏目/URL 都有）
    但正文抓取失败或从未尝试

早先的处理是 `search(skip_empty=True)` 把它们**整个藏起来**。
但「藏起来」是错的处置：这批数据不是垃圾，是**已知的缺口**。
藏起来的后果是 —— 检索数 28999、总览显示 28999，而真实语料只有 28929；
差额 70 条无声无息，谁也不知道它们在哪、能不能补。舆情分析里
「样本是否有偏」是要交代的事，缺口必须可见。

所以做法是给它们一个**占位身份**，而不是给它们空白：
  · 出现在检索结果里（可按标题搜到）
  · 详情页显示「正文未入库」+ 为什么 + 能点什么补
  · **不给正文**，也不假装有正文 —— 占位符不参与 FTS、不参与摘要
  · 保留跳回原文的链接，字段（标题/日期/栏目/来源）照常可用

⚠ 与「导航/站外链接」的区别（用户特别强调过）
----------------------------------------------------------------
导航页（index_page）、站外链接（external）、公众号（wechat）、脏数据（dirty）
被 `classify.py` 判成 SKIP_KINDS，**根本不进 articles 表**，
它们记在 `external_links` 里（当前 132 条）。
它们是「从来就不是文章」，占位是「是文章但正文没拿到」。
两者语义相反，混在一起会让「缺口」这个数字彻底失去意义 ——
所以本模块只认 `articles.content_empty=1`，绝不碰 external_links。

⚠ 也不包含「半成品」
----------------------------------------------------------------
`chars=0 且 content_empty=0` 的行是抓取中途崩掉的残骸（ghost），
属于脏数据而不是缺口。实测当前 0 条，但一旦有也不能混进占位视图 ——
那会让「70 条缺口」变成「71 条缺口」，其中一个是假信号。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .collector import runner as R
from .collector.base import html_to_text


def list_placeholders(d, site: str = "", column_name: str = "",
                      year: str = "", q: str = "",
                      limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """
    列出占位条目（只读，不动库）。

    复用 db.search(only_empty=True) 而不是自己写 SQL ——
    同一套筛选口径，避免两处实现漂移。
    """
    res = d.search(q=q, site=site, column_name=column_name, year=year,
                   skip_empty=False, only_empty=True, sort="retry_first",
                   limit=limit, offset=offset)
    items = []
    for it in res.get("items", []):
        items.append({
            "id": it["id"],
            "site": it.get("site"),
            "site_name": it.get("site_name"),
            "column_name": it.get("column_name"),
            "title": it.get("title"),
            "pub_date": it.get("pub_date"),
            "url": it.get("url"),
            "source_text": it.get("source_text"),
            "chars": it.get("chars") or 0,
            "placeholder": True,
        })
    by_site: Dict[str, int] = {}
    for (s, n) in d.conn.execute(
            "SELECT site, COUNT(*) FROM articles "
            "WHERE COALESCE(content_empty,0)=1 GROUP BY site ORDER BY 2 DESC"):
        by_site[s] = n
    return {"total": res.get("total", 0), "items": items,
            "by_site": by_site, "mode": res.get("mode"),
            # 明确的边界声明，前端要照着显示，别让用户误以为
            # 「导航页/站外链接」也能在这里被补抓
            "excludes": ["导航/栏目首页(index_page)", "站外链接(external)",
                         "微信公众号(wechat)", "脏数据(dirty)",
                         "抓取中断的半成品(chars=0 且未打标)"],
            }


def diagnose_one(art: Dict[str, Any]) -> str:
    """给占位条目一句人话解释「为什么正文没进来」。

    只做**基于已有字段的推断**，不额外发网络请求 ——
    诊断本身不该变成另一种副作用。
    """
    u = (art.get("url") or "").lower()
    title = art.get("title") or ""
    if u.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")):
        return "附件类链接，正文不在 HTML 里"
    if any(k in title for k in ("微视频", "微纪录", "短视频", "图解", "海报")):
        return "疑似视频/图集类稿件，正文靠媒体渲染，静态抓取拿不到"
    if u.startswith("http://") and "zjwx" in u:
        return "HTTP 明文链接，可能被源站重定向或已失效"
    return "正文未入库（可能抓取时解析器失效、或该链接当时已失效）"


def backfill(cfg: Dict[str, Any], d, article_ids: List[int],
             dry_run: bool = True, concurrency: int = 2,
             pause: float = 0.5,
             on_event: Optional[Callable[[str, Dict], None]] = None) -> Dict[str, Any]:
    """
    按需补抓指定几篇的正文。

    设计取舍：
    · **只补明确点名的几篇**，不做「一键补全站点全部空正文」。
      70 篇 × 网络往返，且源站可能早就改版了；批量补抓失败时会
      让人误以为整个站点坏了。默认 dry_run，先看会不会成功。
    · 走 `col.fetch_article(url, fallback)` —— 与正常 sync 同一套解析器。
      不另写一套抽取逻辑，否则补抓出来的正文和同步来的口径不一致，
      「同站同栏目两种格式」会成为以后排查噩梦。
    · 回填走 `upsert()`，它有「空值不覆盖好值」保护：
      补抓再失败也不会把这行的标题/日期/来源洗成空。
    """
    # 默认的 emit 必须能接受任意 kwargs —— 它下面所有调用都传 msg=，
    # 写成固定签名的话，没传 on_event 的调用方（测试、脚本）
    # 会在 emit(...) 上直接 TypeError。
    emit = on_event or (lambda kind, **kw: None)
    ids = [int(i) for i in (article_ids or [])]
    if not ids:
        return {"error": "没有指定 article_ids"}

    # 取待补条目，同时**校验确实是占位** —— 不给「已有正文的文章」
    # 开后门，否则这个接口就变成了绕过正常流程的万能重写入口。
    ph = ",".join("?" * len(ids))
    rows = d.conn.execute(
        f"SELECT id, site, column_name, title, url, chars, content_empty "
        f"FROM articles WHERE id IN ({ph})", ids).fetchall()
    arts = [dict(r) for r in rows]

    not_found = [i for i in ids if i not in {a["id"] for a in arts}]
    already = [a["id"] for a in arts
               if not a.get("content_empty")]
    todo = [a for a in arts if a.get("content_empty")]

    out: Dict[str, Any] = {
        "requested": len(ids), "todo": len(todo),
        "not_found": not_found,
        "skipped_has_content": already,
        "dry_run": bool(dry_run),
        "results": [],
    }
    if not_found:
        emit("warn", msg=f"库里没有这些 id：{not_found}")
    if already:
        emit("warn", msg=f"跳过 {len(already)} 篇已有正文的文章"
                        f"（本接口只补空正文，不做覆盖重写）")

    for a in todo:
        rec = {"id": a["id"], "title": a.get("title"),
               "url": a.get("url"), "site": a.get("site")}
        if dry_run:
            rec["would_fetch"] = a.get("url")
            rec["reason_if_failed"] = diagnose_one(a)
            out["results"].append(rec)
            continue

        site = a.get("site") or ""
        col = R.get_collector(cfg, site, verbose=False)
        if col is None:
            rec["status"] = "failed"
            rec["error"] = f"站点 {site} 未启用或配置损坏"
            out["results"].append(rec)
            continue
        try:
            art = col.fetch_article(a["url"], {"title": a.get("title", "")})
        except Exception as e:
            rec["status"] = "failed"
            rec["error"] = f"{type(e).__name__}: {e}"
            out["results"].append(rec)
            continue

        if art is None:
            rec["status"] = "failed"
            rec["error"] = "解析器没拿到正文（源站可能已改版或链接失效）"
            out["results"].append(rec)
            continue

        text = html_to_text(art.get("content_html", ""), col.base_url)
        if not text or len(text.strip()) < 10:
            rec["status"] = "failed"
            rec["error"] = "拿到 HTML 但正文为空"
            out["results"].append(rec)
            continue

        rec["status"] = "ok"
        rec["chars"] = len(text)
        out["results"].append(rec)
        out["_pending"] = out.get("_pending", []) + [
            (a, text, art, col)]

        if pause:
            time.sleep(pause)

    # 补抓成功的统一回填。upsert 的空值保护在这里生效：
    # 万一某字段抓空了，也不会动这行已有的标题/日期。
    pend = out.pop("_pending", [])
    if pend:
        for a, text, art, col in pend:
            rec = {
                "site": a["site"],
                "site_name": col.name,
                "column_name": a.get("column_name") or col.name,
                "title": a.get("title", ""),
                "pub_date": a.get("pub_date") or art.get("pub_date", ""),
                "url": a["url"],
                "source_text": art.get("source_text", ""),
                "chars": len(text),
                "content_text": text,
                "content_html": art.get("content_html", ""),
                "content_empty": False,
            }
            d.upsert(rec)
        d.conn.commit()
        d.rebuild_fts()
        out["filled"] = len(pend)
        emit("done", msg=f"补回 {len(pend)} 篇正文")

    ok = sum(1 for r in out["results"] if r.get("status") == "ok")
    bad = sum(1 for r in out["results"] if r.get("status") == "failed")
    out["ok"] = ok
    out["failed"] = bad
    if not dry_run:
        emit("done", msg=f"成功 {ok}，失败 {bad}")
    return out