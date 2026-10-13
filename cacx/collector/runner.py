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
from .classify import KIND_LABEL, describe_line
from .generic import build as build_generic
from .zjwx import ZjwxCollector


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc or ""
    except Exception:
        return ""

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
        skipped: Dict[str, int] = {}      # 非文章条目，单独计数，不算失败
        skip_samples: Dict[str, List[str]] = {}
        seen_urls = set()
        for c in col.columns():
            items = col.list_articles(c["code"])
            listed += len(items)
            say(f"  {c['name']:16} 列表 {len(items):5}")
            emit("column", site=site, column=c["name"], n=len(items),
                 listed=listed)
            # 每列补发一次汇总进度。
            # 下面逐篇的 emit("progress") 只在**写库分支**里发，
            # list-only 分支直接 continue —— 于是整个盘点跑下来，
            # 状态行一直显示「列表 0 · 新增 0」，
            # 而这个模式的名字恰恰是「只盘点」，用户选它就是为了看条数。
            emit("progress", site=site, listed=listed, new=listed_new,
                 failed=failed)
            for it in items:
                if it["url"] in seen_urls:
                    continue
                seen_urls.add(it["url"])

                # ---- 非文章条目分流 ----
                # 专题页/站外链接/公众号/脏数据在列表阶段就能判定，
                # 让它们去跑正文只会白烧 3 次退避重试，还会把失败数撑爆，
                # 真正的失败（比如旧稿下线）反而被淹没。登记后跳过。
                kind = it.get("kind") or "article"
                if kind != "article":
                    skipped[kind] = skipped.get(kind, 0) + 1
                    samples = skip_samples.setdefault(kind, [])
                    if len(samples) < 3:
                        samples.append(f"{it.get('title','')[:26]} "
                                       f"{it.get('skip_reason','')}")
                    if kind == "dirty":
                        # 空 URL / javascript: 没有可登记的地址，
                        # 但仍然要留痕 —— 用合成键存下来，
                        # 否则源站的脏数据就彻底消失了，没人会知道它存在过。
                        synth = it.get("url") or (
                            f"empty:{site}:{it.get('column_code','')}:"
                            f"{it.get('title','')[:40]}")
                        d.upsert_external({
                            "site": site, "title": it.get("title", ""),
                            "pub_date": it.get("date", ""),
                            "column_code": it.get("column_code", ""),
                            "column_name": it.get("column_name", ""),
                            "list_url": synth, "target_host": "",
                            "is_own_site": 1, "fail_kind": "dirty",
                            "reason": it.get("skip_reason", ""),
                        })
                        continue
                    if kind in ("external", "wechat", "index_page"):
                        # 全部登记，不静默丢弃 —— 「被跳过」必须可查。
                        # is_own_site 用来看得清：专题页是本站的，站外不是。
                        d.upsert_external({
                            "site": site, "title": it.get("title", ""),
                            "pub_date": it.get("date", ""),
                            "column_code": it.get("column_code", ""),
                            "column_name": it.get("column_name", ""),
                            "list_url": it.get("url", ""),
                            "target_host": _host_of(it.get("url", "")),
                            "is_own_site": 1 if kind == "index_page" else 0,
                            "fail_kind": kind,
                            "reason": it.get("skip_reason", ""),
                        })
                    continue

                # list-only 是**纯只读盘点**：只报增量，不碰数据库。
                if mode == "list-only":
                    if d.get_url_owner(it["url"], exclude_site=site):
                        skipped["dup_other_site"] = skipped.get(
                            "dup_other_site", 0) + 1
                    elif not d.get_by_url(site, it["url"]):
                        listed_new += 1
                        say(f"      + {it['date']} {it['title'][:46]}")
                    continue

                # 存在性只做**读**判断，不先写半成品。
                # 早期版本先 upsert 再抓正文，抓失败就留下
                # 「有标题无正文、content_empty 却是 0」的幽灵行 —— 已修正。
                exists = d.get_by_url(site, it["url"])
                if exists and mode != "full":
                    continue

                # ---- 跨站去重（2026-10-13 加）----
                # 同一个 URL 若已被**别的站**收过，就到此为止：既不抓正文也不写库。
                # 为什么不能只看 (site, url)：articles 的唯一键就是 (site, url)，
                # 所以换站再采一遍同一篇文章，库里就会多一行。
                # 实测：通用采集器验证源 zjwx-api 把 zjwx 已有的 10 篇又收了一遍，
                # 凭空多出 62 条关系（8801 → 8863），报告里的核心数字全被带偏。
                # 放在抓正文**之前**，是因为正文抓取带 3 次退避重试，很贵 ——
                # 已知是重复就不该发出这个请求。db.upsert_article 里还有一道兜底，
                # 两层都留着：这里管"别浪费请求"，那里管"别写脏数据"。
                owner = d.get_url_owner(it["url"], exclude_site=site)
                if owner:
                    skipped["dup_other_site"] = skipped.get(
                        "dup_other_site", 0) + 1
                    samples = skip_samples.setdefault("dup_other_site", [])
                    if len(samples) < 3:
                        samples.append(f"已由 {owner['site']} 收录 {it['url'][:46]}")
                    continue

                art = col.fetch_article(it["url"], it)
                if art is None:
                    failed += 1
                    say(f"      ! 正文抓不到（不写库）: {it['url']}")
                    emit("progress", site=site, listed=listed, new=listed_new,
                         failed=failed)
                    continue

                text = html_to_text(art["content_html"], col.base_url)
                dup_body_before = d.dup_same_body
                rec = {
                    "site": site, "site_name": col.name,
                    # 全部用 .get()：写入路径上一个 KeyError 会让整次同步中断，
                    # 而这只是一个可选字段。缺了就退回栏目名，不丢条目。
                    "column_code": it.get("column_code", ""),
                    "column_name": it.get("column_name") or col.name,
                    "title": art.get("title") or it.get("title", ""),
                    "pub_date": art.get("pub_date") or it.get("date", ""),
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
                elif d.dup_same_body > dup_body_before:
                    # 一文多栏目发：同一篇稿子在这个栏目又出现了一次。
                    # upsert_article 内部按正文指纹拦掉的，URL 不重复，
                    # 所以站在列表层面完全看不出来 —— 必须单独记一笔，
                    # 否则统计上就是「这站这轮没新东西」。
                    skipped["dup_same_body"] = skipped.get("dup_same_body", 0) + 1
                    samples = skip_samples.setdefault("dup_same_body", [])
                    if len(samples) < 3:
                        samples.append(f"同正文已收录 {it['title'][:34]}")
                emit("progress", site=site, listed=listed, new=listed_new,
                     failed=failed)
                if limit and listed >= limit:
                    break
            if limit and listed >= limit:
                break
            # 栏目收尾：把该栏目本次的净增/失败回给界面，
            # 否则前端只知道「这个栏目跑完了」，不知道它到底出了多少
            emit("column_done", site=site, column=c["name"],
                 new=listed_new, failed=failed,
                 skipped=sum(skipped.values()))

        totals["listed"] += listed
        totals["new"] += listed_new
        totals["failed"] += failed
        totals["skipped"] = totals.get("skipped", 0) + sum(skipped.values())
        for k, v in skipped.items():
            totals["skip_kinds"] = totals.get("skip_kinds", {})
            totals["skip_kinds"][k] = totals["skip_kinds"].get(k, 0) + v
        if mode == "list-only":
            skip_txt = ("，跳过 " + describe_line(skipped)) if skipped else ""
            say(f"[sync] {col.name} 窗口内列表 {listed}，"
                f"其中库里没有的 {listed_new}{skip_txt}")
            emit("site_done", site=site, listed=listed, new=listed_new,
                 failed=failed, skipped=sum(skipped.values()),
                 skip_kinds=dict(skipped), mode=mode)
            continue
        d.conn.commit()
        d.rebuild_fts()
        d.record_sync(site, mode, started, listed, listed_new, failed,
                      note=f"columns={len(col.columns())}")
        skip_txt = ""
        if skipped:
            skip_txt = "，跳过 " + describe_line(skipped)
            for k, samples in skip_samples.items():
                for s in samples[:1]:
                    self_note = f"        · {KIND_LABEL.get(k, k)}示例: {s}"
                    say(self_note)
        say(f"[sync] {col.name} 列表 {listed}，新增 {listed_new}，"
            f"失败 {failed}{skip_txt}")
        emit("site_done", site=site, listed=listed, new=listed_new,
             failed=failed, skipped=sum(skipped.values()),
             skip_kinds=dict(skipped), mode=mode)
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