# -*- coding: utf-8 -*-
"""
批量删除：摘要 / 文章 / 站外条目
================================================================
为什么是这个形状（设计取舍）
----------------------------------------------------------------
删本地语料是**不可逆**操作：文章删掉后要重新 sync 才能抓回来，
一�� 20 分钟起步。摘要删掉则要重新烧 AI 配额。所以本模块的核心不是
DELETE 语句，而是**让人在删之前看清要删掉什么**。

于是定成两阶段：

    preview_purge(...)  ->  {"matches": N, "by_site": [...], "by_task": [...],
                             "sample": [...], "confirm_token": "..."}
    execute_purge(...)  ->  必须带上 preview 返回的 confirm_token

`confirm_token` 是预览与执行之间的**一次性凭据**，由「条件 + 命中数 +
预览时刻」算出来。好处是：
  · 光看参数不够，还得先看过影响面才拿得到 token
  · 预览之后库若发生变化（有人又同步了几篇），token 失效、执行会拒绝
    —— 避免「我看的是 100 篇的预览，实际删掉了 150 篇」

⚠ 与项目其它铁律的一致性
  · 绝不静默删除：删了什么、删了多少，逐项回报
  · 空条件不给删：没有明确筛选条件时**拒绝**执行，防止手滑清库
  · 级联不遗漏：articles 被删时，ai_annotations / events / FTS 一并清，
    否则库里留孤儿行、搜索索引与正文表不一致
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

# 允许删除的目标白名单。刻意**不含** meta / sync_runs —— 那是库的血缘，
# 删了会破坏「这批数据什么时候抓的」这类追溯。
PURGE_TARGETS = ("articles", "annotations", "external_links")

# 「有筛选条件」与「条件足够精细」是两回事。
# 只给 --site 就删该站全部文章（如浙江 5717 篇）显然不该放行，
# 但一刀切限死 200 条又太僵 —— 明确限定年份/栏目/日期区间的批量清理
# 是正当需求。所以按**精细度**分级：越精确，允许删的量越大。
_FINE_FILTERS = ("ids", "q", "year", "column_name",
                 "date_from", "date_to", "chars_below",
                 "empty_content_only", "before_synced_at")
_FINE_LIMIT = 2000        # 有精确条件：最多 2000 条
_COARSE_LIMIT = 200       # 只有 site 之类的粗条件：最多 200 条


def _limit_for(f: Dict[str, Any]) -> int:
    return (_FINE_LIMIT if any(f.get(k) for k in _FINE_FILTERS)
            else _COARSE_LIMIT)


class PurgeRefused(Exception):
    """拒绝执行删除（条件不足 / token 失效 / 目标非法）"""


def _fingerprint(scope: Dict[str, Any], matches: int) -> str:
    """
    预览指纹：条件 + 命中数 -> 凭据。

    ⚠ 这里**绝不能掺时间戳**。早先掺了 time.time()，结果每次 preview
    算出来都不一样，execute 时拿上次 preview 的 token 必然判不中 ——
    二次确认闸门把自己锁死了，功能完全不可用（实测撞到）。
    正确做法：token 只标识「这些条件 + 这个命中数」这一份快照，
    数据是否变化由 execute 时**重新统计命中数**来发现（见下）。
    """
    payload = json.dumps(scope, ensure_ascii=False, sort_keys=True)
    raw = f"{payload}|{matches}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _scope_key(p: Dict[str, Any]) -> Dict[str, Any]:
    """参与指纹的条件项（去掉纯展示字段）"""
    return {k: v for k, v in sorted(p.items())
            if k not in ("confirm_token", "sample", "limit")}


def _clean(filters: Dict[str, Any]) -> Dict[str, Any]:
    """
    剔除「等于没给」的筛选值。

    ⚠ 陷阱：`False` 不是「没给」。早先写的是
    `v not in (None, "", [], {})` —— 而 `False not in (None, "", [], {})`
    为 True（False 与这四个值都不相等），于是 `empty_content_only=False`
    会被当成有效条件留下：
      · `if not f` 不成立 → 绕过「无条件拒绝删除」的总闸门
      · `_stats_articles` 里 `if f.get("empty_content_only")` 又是 False
        → 不拼任何 WHERE → **统计的就是全表**
    结果就是「传个 False 就能删掉整张表的前 200 条」。删除功能里
    这种漏洞是不可接受的，所以显式枚举假值，而不是靠不等比较。
    """
    out = {}
    for k, v in filters.items():
        if v is None or v is False:
            continue
        if isinstance(v, (str, list, dict, tuple)) and len(v) == 0:
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------- 影响面统计
def _stats_articles(d, f: Dict[str, Any]) -> Dict[str, Any]:
    where, args = [], []
    if f.get("site"):
        where.append("site = ?"); args.append(f["site"])
    if f.get("site_like"):
        where.append("site LIKE ?"); args.append(f"%{f['site_like']}%")
    if f.get("date_from"):
        where.append("pub_day >= ?"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("pub_day <= ?"); args.append(f["date_to"])
    if f.get("year"):
        where.append("year = ?"); args.append(str(f["year"]))
    if f.get("column_name"):
        where.append("column_name = ?"); args.append(f["column_name"])
    if f.get("ids"):
        ph = ",".join("?" * len(f["ids"]))
        where.append(f"id IN ({ph})")
        args.extend(f["ids"])
    if f.get("q"):
        # 用 search 走既有 FTS/LIKE 逻辑，避免另写一套筛选口径
        res = d.search(q=f["q"], site=f.get("site", "") or "",
                       column_name=f.get("column_name", "") or "",
                       year=str(f.get("year") or ""),
                       date_from=f.get("date_from", "") or "",
                       date_to=f.get("date_to", "") or "",
                       skip_empty=False, limit=100000)
        ids = [it["id"] for it in res.get("items", [])]
        ph = ",".join("?" * len(ids)) if ids else "NULL"
        where.append(f"id IN ({ph})"); args.extend(ids)
    if f.get("chars_below"):
        where.append("chars < ?"); args.append(int(f["chars_below"]))
    if f.get("empty_content_only"):
        where.append("content_empty = 1")
    if f.get("before_synced_at"):
        where.append("COALESCE(synced_at,'') < ?"); args.append(f["before_synced_at"])

    w = (" WHERE " + " AND ".join(where)) if where else ""

    total = d.conn.execute(f"SELECT COUNT(*) FROM articles{w}", args).fetchone()[0]
    by_site = [dict(r) for r in d.conn.execute(
        f"SELECT site, COUNT(*) n, MIN(pub_day) a, MAX(pub_day) b, "
        f"SUM(COALESCE(chars,0)) chars FROM articles{w} GROUP BY site", args)]
    sample = [dict(r) for r in d.conn.execute(
        f"SELECT id, site, pub_day, title, url FROM articles{w} "
        f"ORDER BY pub_day DESC LIMIT 8", args)]
    # 连带会一起没掉的 AI 摘要（这是用户最该知道的）
    ann = 0
    if total:
        ids_w = w.replace("WHERE ", "WHERE ") if w else "WHERE 1=1"
        ann = d.conn.execute(
            f"SELECT COUNT(*) FROM ai_annotations x "
            f"JOIN articles a ON a.id=x.article_id {ids_w}",
            args).fetchone()[0]
    return {"target": "articles", "matches": total,
            "by_site": by_site, "sample": sample,
            "cascade": {"ai_annotations": ann,
                        "note": "删文章会连带删掉它的 AI 摘要与检索事件"}}


def _stats_annotations(d, f: Dict[str, Any]) -> Dict[str, Any]:
    where, args = ["1=1"], []
    if f.get("task"):
        where.append("x.task = ?"); args.append(f["task"])
    if f.get("provider"):
        where.append("x.provider = ?"); args.append(f["provider"])
    if f.get("site"):
        where.append("a.site = ?"); args.append(f["site"])
    if f.get("date_from"):
        where.append("a.pub_day >= ?"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("a.pub_day <= ?"); args.append(f["date_to"])
    if f.get("created_before"):
        where.append("x.created_at < ?"); args.append(f["created_before"])
    if f.get("ids"):
        ph = ",".join("?" * len(f["ids"]))
        where.append(f"x.article_id IN ({ph})"); args.extend(f["ids"])
    if f.get("result_like"):
        where.append("x.result LIKE ?"); args.append(f"%{f['result_like']}%")
    w = " WHERE " + " AND ".join(where)

    total = d.conn.execute(
        f"SELECT COUNT(*) FROM ai_annotations x "
        f"JOIN articles a ON a.id=x.article_id {w}", args).fetchone()[0]
    by_task = [dict(r) for r in d.conn.execute(
        f"SELECT x.task, COUNT(*) n FROM ai_annotations x "
        f"JOIN articles a ON a.id=x.article_id {w} GROUP BY x.task", args)]
    sample = [dict(r) for r in d.conn.execute(
        f"SELECT x.article_id, a.title, x.task, x.provider, x.model, "
        f"x.created_at, substr(x.result,1,80) result FROM ai_annotations x "
        f"JOIN articles a ON a.id=x.article_id {w} "
        f"ORDER BY x.created_at DESC LIMIT 8", args)]
    return {"target": "annotations", "matches": total,
            "by_task": by_task, "sample": sample, "cascade": {}}


def _stats_external(d, f: Dict[str, Any]) -> Dict[str, Any]:
    where, args = ["1=1"], []
    if f.get("fail_kind"):
        where.append("fail_kind = ?"); args.append(f["fail_kind"])
    if f.get("site"):
        where.append("site = ?"); args.append(f["site"])
    if f.get("is_own_site") is not None:
        where.append("is_own_site = ?"); args.append(1 if f["is_own_site"] else 0)
    w = " WHERE " + " AND ".join(where)
    total = d.conn.execute(
        f"SELECT COUNT(*) FROM external_links{w}", args).fetchone()[0]
    by_kind = [dict(r) for r in d.conn.execute(
        f"SELECT fail_kind, COUNT(*) n FROM external_links{w} "
        f"GROUP BY fail_kind", args)]
    sample = [dict(r) for r in d.conn.execute(
        f"SELECT fail_kind, is_own_site, target_host, title, list_url "
        f"FROM external_links{w} LIMIT 8", args)]
    return {"target": "external_links", "matches": total,
            "by_kind": by_kind, "sample": sample, "cascade": {}}


_STATS = {"articles": _stats_articles,
          "annotations": _stats_annotations,
          "external_links": _stats_external}


# ---------------------------------------------------------------- 对外接口
def preview_purge(d, target: str, **filters) -> Dict[str, Any]:
    """
    算影响面，**不动任何数据**。
    返回里带 confirm_token，执行时必须原样带回。
    """
    if target not in PURGE_TARGETS:
        raise PurgeRefused(
            f"删除目标只支持 {PURGE_TARGETS}，收到 {target!r}"
            "（meta / sync_runs 是库的血缘，不提供删除）")

    f = _clean(filters)
    if not f:
        raise PurgeRefused(
            "没有给任何筛选条件 —— 拒绝预览/删除全量。"
            "请至少指定 site / date_from+date_to / task / fail_kind / ids 之一")

    with d._lock:
        st = _STATS[target](d, f)
        n = st["matches"]

    if n == 0:
        raise PurgeRefused("筛选条件下没有命中任何记录（可能条件写错了）")
    cap = _limit_for(f)
    if n > cap:
        fine = any(f.get(k) for k in _FINE_FILTERS)
        raise PurgeRefused(
            f"命中 {n} 条，超过当前条件下的删除上限 {cap}。"
            + ("你的条件里没有年份/栏目/日期区间/关键词等精确限定，"
               "只按站点删风险太大。" if not fine else
               "范围仍然偏大，请再收窄（例如加 --date-to 或按 --ids 分批）。")
            + "另：可先用 cacx purge ... --json 看清分布再决定。")

    st["filters"] = f
    st["confirm_token"] = _fingerprint(_scope_key(f) | {"target": target}, n)
    st["warnings"] = _warnings(target, n, f)
    return st


def _warnings(target: str, n: int, f: Dict[str, Any]) -> List[str]:
    w: List[str] = []
    if target == "articles":
        w.append(f"将永久删除 {n} 篇文章正文。删掉后要重新 sync 才能抓回"
                 "（全量约 20 分钟），这是**不可逆**操作。")
        if not f.get("date_to"):
            w.append("没有限定 date_to：会把该条件下**所有年份**的文章都删掉，"
                     "确认这是你要的范围吗？")
        if not (f.get("date_from") or f.get("year") or f.get("column_name")):
            w.append("没有限定站点之外的任何范围（年份/栏目），"
                     "当前条件相当宽。")
    elif target == "annotations":
        w.append(f"将删除 {n} 条 AI 摘要。文章正文保留，"
                 "但重新生成要再烧一次 AI 配额。")
    else:
        w.append(f"将删除 {n} 条失败/站外条目登记。"
                 "这些是当初分流留痕，删掉就查不到当初为什么跳过它们了。")
    return w


def execute_purge(d, target: str, confirm_token: str, **filters) -> Dict[str, Any]:
    """
    真删。必须带 preview_purge 返回的 confirm_token。

    返回 {"deleted": {...}, "total": N, "before": {...}}
    —— 逐项回报删了什么，绝不静默。
    """
    if not confirm_token:
        raise PurgeRefused(
            "缺少 confirm_token —— 必须先调 preview_purge 看影响面，"
            "再把返回的 token 传回来。这是删除前的二次确认闸门。")

    # 先复算一次：既能校验 token，也让「预览后库变了」暴露出来
    prev = preview_purge(d, target, **filters)
    if prev["confirm_token"] != confirm_token:
        raise PurgeRefused(
            "confirm_token 失效：筛选条件或命中数与你预览时不一致"
            f"（当前命中 {prev['matches']} 条，预览时 {prev['confirm_token']} "
            "对应的是另一组条件/条数）。请重新 preview 确认后再删。")

    f = _clean(filters)
    before = prev["matches"]
    deleted: Dict[str, int] = {}

    with d._lock:
        if target == "articles":
            ids = _article_ids(d, f)
            ph = ",".join("?" * len(ids))
            # ⚠ 顺序：**先删子表，再删父表**。
            # ai_articles 上声明了 ON DELETE CASCADE 且库开了 foreign_keys，
            # 若先删 articles，SQLite 会自动清掉摘要，事后自己再 DELETE 一遍
            # 只会得到 rowcount=0 —— 报告里就出现「删了文章却没删摘要」的
            # 假象（实测踩到）。先删子表既能拿到真实计数，也不依赖外键行为。
            deleted["ai_annotations"] = d.conn.execute(
                f"DELETE FROM ai_annotations WHERE article_id IN ({ph})",
                ids).rowcount
            # events 没有外键声明，只能手动清
            deleted["events"] = d.conn.execute(
                f"DELETE FROM events WHERE article_id IN ({ph})", ids).rowcount
            deleted["articles"] = d.conn.execute(
                f"DELETE FROM articles WHERE id IN ({ph})", ids).rowcount
            # FTS 由 articles_ad 触发器维护，这里不碰；
            # 只在体量大时优化一次索引
            deleted["fts_optimized"] = 0
            if deleted["articles"] > 500:
                d.conn.execute("INSERT INTO articles_fts(articles_fts) "
                               "VALUES('optimize')")
                deleted["fts_optimized"] = 1
        elif target == "annotations":
            where, args = _ann_where(f)
            deleted["annotations"] = d.conn.execute(
                f"DELETE FROM ai_annotations WHERE id IN ("
                f"SELECT x.id FROM ai_annotations x "
                f"JOIN articles a ON a.id=x.article_id WHERE {where})",
                args).rowcount
        else:
            where, args = ["1=1"], []
            if f.get("fail_kind"):
                where.append("fail_kind = ?"); args.append(f["fail_kind"])
            if f.get("site"):
                where.append("site = ?"); args.append(f["site"])
            if f.get("is_own_site") is not None:
                where.append("is_own_site = ?")
                args.append(1 if f["is_own_site"] else 0)
            deleted["external_links"] = d.conn.execute(
                "DELETE FROM external_links WHERE " + " AND ".join(where),
                args).rowcount
        d.conn.commit()

    return {"target": target, "before": before,
            "deleted": deleted,
            "total": sum(v for k, v in deleted.items()
                         if k in ("articles", "annotations", "external_links")),
            "filters": f}


def _article_ids(d, f) -> List[int]:
    where, args = [], []
    if f.get("site"):
        where.append("site = ?"); args.append(f["site"])
    if f.get("site_like"):
        where.append("site LIKE ?"); args.append(f"%{f['site_like']}%")
    if f.get("date_from"):
        where.append("pub_day >= ?"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("pub_day <= ?"); args.append(f["date_to"])
    if f.get("year"):
        where.append("year = ?"); args.append(str(f["year"]))
    if f.get("column_name"):
        where.append("column_name = ?"); args.append(f["column_name"])
    if f.get("chars_below"):
        where.append("chars < ?"); args.append(int(f["chars_below"]))
    if f.get("empty_content_only"):
        where.append("content_empty = 1")
    if f.get("before_synced_at"):
        where.append("COALESCE(synced_at,'') < ?"); args.append(f["before_synced_at"])
    if f.get("ids"):
        ph = ",".join("?" * len(f["ids"]))
        where.append(f"id IN ({ph})"); args.extend(f["ids"])
    if f.get("q"):
        res = d.search(q=f["q"], site=f.get("site", "") or "",
                       column_name=f.get("column_name", "") or "",
                       year=str(f.get("year") or ""),
                       date_from=f.get("date_from", "") or "",
                       date_to=f.get("date_to", "") or "",
                       skip_empty=False, limit=100000)
        ids = [it["id"] for it in res.get("items", [])]
        ph = ",".join("?" * len(ids)) if ids else "NULL"
        where.append(f"id IN ({ph})"); args.extend(ids)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    return [r["id"] for r in d.conn.execute(
        f"SELECT id FROM articles{w}", args)]


def _ann_where(f) -> tuple:
    where, args = ["1=1"], []
    if f.get("task"):
        where.append("x.task = ?"); args.append(f["task"])
    if f.get("provider"):
        where.append("x.provider = ?"); args.append(f["provider"])
    if f.get("site"):
        where.append("a.site = ?"); args.append(f["site"])
    if f.get("date_from"):
        where.append("a.pub_day >= ?"); args.append(f["date_from"])
    if f.get("date_to"):
        where.append("a.pub_day <= ?"); args.append(f["date_to"])
    if f.get("created_before"):
        where.append("x.created_at < ?"); args.append(f["created_before"])
    if f.get("ids"):
        ph = ",".join("?" * len(f["ids"]))
        where.append(f"x.article_id IN ({ph})"); args.extend(f["ids"])
    if f.get("result_like"):
        where.append("x.result LIKE ?"); args.append(f"%{f['result_like']}%")
    return " AND ".join(where), args