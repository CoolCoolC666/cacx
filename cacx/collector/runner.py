# -*- coding: utf-8 -*-
"""
采集编排：sync 与 doctor
================================================================
sync  列表拉取 → 时间窗过滤 → 正文抓取 → 直接入 SQLite
      （不再写三层文件；文件产物已由既有语料提供，同步只补增量）
doctor 规则自检：接口是否还通、选择器是否失效；可选交给 AI 补充诊断
"""

import time
from typing import Dict, List, Optional

from .. import db as D
from .. import sites as S
from .base import SiteCollector, html_to_text
from .cac import CacCollector
from .generic import build as build_generic
from .zjwx import ZjwxCollector

# 内置站点：采集逻辑是代码，不是配置能表达的。
# 自定义站点走 GenericCollector，由 config/sites.d/*.json 驱动。
BUILTIN_COLLECTORS = {"cac": CacCollector, "zjwx": ZjwxCollector}
COLLECTORS = BUILTIN_COLLECTORS        # 兼容旧引用，实际请用 site_codes()


def site_codes(cfg: Dict, only_enabled: bool = True) -> List[str]:
    """当前配置下**所有**可采集的站点（内置 + 自定义）"""
    return S.enabled_codes(cfg) if only_enabled else \
        [c for c, v in S.all_sites(cfg).items()
         if not v.get("_broken") and not v.get("_shadowed")]


def get_collector(cfg: Dict, site: str, verbose=True) -> Optional[SiteCollector]:
    """
    按站点代码造采集器。
    内置站点用专用类；自定义站点用 GenericCollector（配置驱动）。
    返回 None 表示「未启用 / 不存在 / 配置损坏」，调用方要显式登记，
    不能当作「这个站没东西」。
    """
    sc = cfg.get("sites", {}).get(site, {})
    spec = S.all_sites(cfg).get(site) or sc
    if not spec:
        return None
    if spec.get("_broken"):
        if verbose:
            print(f"[warn] 站点 {site} 配置损坏: {spec.get('_error')}")
        return None
    if not spec.get("enabled", True):
        return None
    cls = BUILTIN_COLLECTORS.get(site)
    if cls is not None:
        return cls(sc or spec, cfg.get("collector", {}), verbose=verbose)
    try:
        return build_generic(spec, cfg.get("collector", {}), verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[warn] 站点 {site} 配置无法使用: {type(e).__name__}: {e}")
        return None


def sync(cfg: Dict, sites: Optional[List[str]] = None, mode: str = "incremental",
         limit: Optional[int] = None, verbose: bool = True,
         date_start: Optional[str] = None, date_end: Optional[str] = None,
         on_progress=None,
         ) -> int:
    """
    mode:
      incremental  只处理库里没有的（默认）
      full         全量重抓并刷新
      list-only    只跑列表，不抓正文（用于快速看增量有多少）

    date_start / date_end:
      本次运行的时间窗，覆盖配置里的 sites.*.date_start/date_end。
      命令行的 --year / --from-year / --to-year 会转成这两个参数传进来。

    on_progress:
      可选回调 on_progress(event: dict)，用于把进度喂给 Web 界面。
      原来只有 print，网页端就只能「点了没反应，干等」。
      注意 runner 自己另开了 Database 连接，**不会**与 Web 服务共享连接，
      所以这里只回传消息，不碰服务里的 DB 对象。
    """
    def emit(kind: str, **kw):
        if on_progress is None:
            return
        try:
            on_progress({"kind": kind, **kw})
        except Exception:
            pass   # 回调坏了不能拖垮采集

    def say(msg: str, **kw):
        """原来只有 print；网页端要的是「同一条消息两个去处」。"""
        if verbose:
            print(msg)
        emit("log", msg=msg, **kw)

    # 逐站浅拷贝配置，避免改到调用方的 dict
    run_cfg = {**cfg, "sites": {k: dict(v) for k, v in cfg.get("sites", {}).items()}}
    for sc in run_cfg["sites"].values():
        if date_start:
            sc["date_start"] = date_start
        if date_end:
            sc["date_end"] = date_end

    d = D.open_db(run_cfg)
    # 默认站点表是**动态**的：用户在网页上加了新站点，
    # 不重启服务、不改这里，下一次 sync 就该带上它。
    # 早前这里写死 [s for s in COLLECTORS]，等于新站点永远采不到。
    sites = sites or site_codes(run_cfg)
    rc = 0
    totals = {"listed": 0, "new": 0, "failed": 0}
    for site in sites:
        col = get_collector(run_cfg, site, verbose)
        if col is None:
            say(f"[sync] 跳过 {site}（未启用或不支持）")
            continue
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        say(f"[sync] === {col.name} ({site}) ===")
        say(f"[sync] 时间窗 {col.date_start} ~ {col.date_end}  模式 {mode}")
        emit("site_start", site=site, site_name=col.name)

        listed = 0
        listed_new = 0
        failed = 0
        seen_urls = set()
        for c in col.columns():
            items = col.list_articles(c["code"])
            listed += len(items)
            say(f"  {c['name']:16} 列表 {len(items):5}")
            emit("column", site=site, column=c["name"], n=len(items),
                 listed=listed)
            for it in items:
                if it["url"] in seen_urls:
                    continue
                seen_urls.add(it["url"])

                # list-only 是**纯只读盘点**：只报增量，不碰数据库。
                if mode == "list-only":
                    if not d.get_by_url(site, it["url"]):
                        listed_new += 1
                        say(f"      + {it['date']} {it['title'][:46]}")
                    continue

                # 存在性只做**读**判断，不先写半成品。
                # 早期版本先 upsert 再抓正文，抓失败就留下
                # 「有标题无正文、content_empty 却是 0」的幽灵行 —— 已修正。
                exists = d.get_by_url(site, it["url"])
                if exists and mode != "full":
                    continue

                art = col.fetch_article(it["url"], it)
                if art is None:
                    failed += 1
                    say(f"      ! 正文抓不到（不写库）: {it['url']}")
                    emit("progress", site=site, listed=listed, new=listed_new,
                         failed=failed)
                    continue

                text = html_to_text(art["content_html"], col.base_url)
                rec = {
                    "site": site, "site_name": col.name,
                    "column_code": it["column_code"],
                    "column_name": it.get("column_name") or col.name,
                    "title": art.get("title") or it["title"],
                    "pub_date": art.get("pub_date") or it["date"],
                    "url": it["url"],
                    "source_text": art.get("source_text", ""),
                    "content_text": text,
                    "content_html": art["content_html"],
                    "chars": len(text.strip()),
                    "content_empty": not text.strip(),
                }
                if d.upsert_article(rec):
                    listed_new += 1
                    say(f"      + {it['date']} {it['title'][:46]}")
                emit("progress", site=site, listed=listed, new=listed_new,
                     failed=failed)
                if limit and listed >= limit:
                    break
            if limit and listed >= limit:
                break

        totals["listed"] += listed
        totals["new"] += listed_new
        totals["failed"] += failed
        if mode == "list-only":
            say(f"[sync] {col.name} 窗口内列表 {listed}，其中库里没有的 {listed_new}")
            emit("site_done", site=site, listed=listed, new=listed_new,
                 failed=failed, mode=mode)
            continue
        d.conn.commit()
        d.rebuild_fts()
        d.record_sync(site, mode, started, listed, listed_new, failed,
                      note=f"columns={len(col.columns())}")
        say(f"[sync] {col.name} 列表 {listed}，新增 {listed_new}，失败 {failed}")
        emit("site_done", site=site, listed=listed, new=listed_new,
             failed=failed, mode=mode)
    d.close()
    emit("done", **totals)
    return rc


# ---------------------------------------------------------------- doctor
def doctor(cfg: Dict, site: Optional[str] = None, use_ai: bool = True) -> int:
    """接口自检。规则层永远跑；use_ai=True 且 AI 已配置时再补一层 AI 诊断。"""
    d = D.open_db(cfg)
    sites = site or site_codes(cfg)
    reports = []
    print("=" * 68)
    print("接口自检 doctor")
    print("=" * 68)
    for s in sites:
        col = get_collector(cfg, s, verbose=False)
        if col is None:
            print(f"  {s:6} 未启用")
            continue
        t0 = time.time()
        rep = col.health_check()
        rep["elapsed_ms"] = int((time.time() - t0) * 1000)
        rep["site_name"] = col.name
        reports.append(rep)
        mark = "OK " if rep.get("ok") else "!! "
        print(f"  {mark}{s:6} {col.name:10} {rep.get('elapsed_ms',0):>5}ms  "
              f"栏目 {rep.get('checked_columns','?')}")
        for i in rep.get("issues", []):
            print(f"        - {i}")

    # 数据层体检
    f = d.facets()
    print("\n数据层")
    print(f"  文章 {f['total']} | 空正文 {f['empty']} | 站外条目 {f['external']}")
    if f["empty"]:
        print("  提示: 空正文条目应为显式标记(content_empty=1)的那种，"
              "若是未标记的空，说明回源时源站也取不到，需复查")

    # AI 补充诊断
    if use_ai:
        from ..ai import diagnose
        problems = [r for r in reports if not r.get("ok")]
        if problems:
            ai = diagnose(cfg, problems, d)
            if ai:
                print("\nAI 诊断")
                for k, v in ai.items():
                    print(f"  [{k}] {v}")
            else:
                print("\nAI 诊断: 未启用或调用失败（不影响规则层结论）")

    bad = [r for r in reports if not r.get("ok")]
    d.close()
    print()
    if bad:
        print(f"结论: {len(bad)} 个站点需要关注，详见上方 issues")
        return 2
    print("结论: 全部正常")
    return 0