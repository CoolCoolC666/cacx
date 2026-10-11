# -*- coding: utf-8 -*-
"""
后台命令符入口
================================================================
    cacx init                  初始化配置与数据库
    cacx import                把既有 json 语料灌入数据库
    cacx stats                 看语料分布
    cacx search "关键词"        命令符检索
    cacx show <id>             看单篇全文
    cacx config show|set|import|export
    cacx sync --site cac       增量同步
    cacx serve                 起 Web 检索服务
    cacx doctor                接口自检（AI 参与诊断）

约定
    · 只读类命令永不写库；写类命令一律打印将要发生的动作
    · 所有命令都从 settings.json 读配置，不硬编码路径
"""

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from .. import config as C
from .. import db as D
from .. import importer as I


def _fmt_article(it: dict, idx: int = 0) -> str:
    site = it.get("site_name") or it.get("site", "")
    return (f"{idx:>3}. [{site}·{it.get('column_name','')}] "
            f"{(it.get('pub_date') or '')[:10]}  {it.get('title','')}\n"
            f"       {it.get('chars',0):>6} 字  {it.get('url','')}")


def _parse_kv_pairs(pairs) -> dict:
    """
    把「k=v」形式的可重复参数解析成 dict。
    为什么容忍没有等号的条目而不是报错：诊断命令是给「随手粘个 URL 就想看一眼」
    的场景用的，用户敲错一个参数不该让整条诊断都跑不出来 —— 跳过坏条目、
    照常把接口诊断完，坏参数最多不出现在请求里。
    """
    out = {}
    for p in pairs or []:
        p = str(p).strip()
        if not p or "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


# ---------------------------------------------------------------- init
def cmd_init(args):
    C.ensure_dirs()
    cfg = C.load(create=True)
    d = D.open_db(cfg)
    print(f"配置: {C.SETTINGS_FILE}")
    print(f"模板: {C.EXAMPLE_FILE}")
    print(f"数据库: {d.path}")
    print(f"初始文章: {d.facets()['total']}")
    print("完成。下一步: cacx import")
    d.close()


# ---------------------------------------------------------------- import
def cmd_import(args):
    cfg = C.load(create=True)
    d = D.open_db(cfg)
    roots = I.discover_corpus_roots(C.PROJECT_ROOT)
    if not roots:
        print("未发现语料目录。请在 config/settings.json 设置 corpus_root，"
              "或把语料放在项目根同级的 中国网信办/ 浙江网信网/ 下。")
        d.close()
        return 1
    print(f"语料根: {roots}")
    st = I.import_corpus(d, roots)
    print(f"\n扫描 {st['scanned']} | 新增 {st['inserted']} | 刷新 {st['dup']} "
          f"| 空正文 {st['empty']} | 站外 {st['external']} | 耗时 {st['seconds']}s")
    if st["inserted"] == 0 and st["dup"]:
        print("（无新增：重复导入会幂等刷新既有行，用于修正字段）")
    d.close()
    return 0


# ---------------------------------------------------------------- stats
def cmd_stats(args):
    cfg = C.load()
    d = D.open_db(cfg)
    f = d.facets()
    print("=" * 66)
    print("语料总览")
    print("=" * 66)
    print(f"  文章总数   {f['total']:,}")
    print(f"  正文字数   {f['chars']:,}")
    print(f"  覆盖天数   {f['days']}")
    print(f"  空正文     {f['empty']}")
    print(f"  站外条目   {f['external']}")
    last = d.get_meta("last_import_at")
    if last:
        print(f"  最近入库   {last}")

    print("\n按站点")
    for s in f["sites"]:
        print(f"   {s['site']:6} {s.get('site_name') or '':8} {s['n']:>6} 篇  "
              f"{(s['c'] or 0):>10,} 字")

    print("\n按栏目")
    cur = None
    for c in f["columns"]:
        if c["site"] != cur:
            cur = c["site"]
            print(f"  -- {cur} --")
        print(f"     {(c.get('column_name') or '(未命名)'):16} {c['n']:>6}")

    print("\n按年份")
    for y in f["years"]:
        print(f"     {y['year']}  {y['n']:>6}")
    d.close()
    return 0


# ---------------------------------------------------------------- search
def cmd_search(args):
    cfg = C.load()
    d = D.open_db(cfg)
    r = d.search(
        q=args.query or "",
        site=args.site or "",
        column_name=args.column or "",
        year=args.year or "",
        date_from=args.date_from or "",
        date_to=args.date_to or "",
        min_chars=args.min_chars,
        sort=args.sort,
        limit=args.limit,
        min_fts_chars=cfg["search"]["min_fts_chars"],
    )
    print(f"命中 {r['total']:,} 条 | 检索方式 {r['mode']} | {r['took_ms']}ms")
    if r["mode"] == "like" and args.query:
        print("（提示: 查询含 1~2 字词，已退回 LIKE 扫描；改用 3 字以上会走 FTS5 索引更快）")
    print("-" * 66)
    for i, it in enumerate(r["items"], 1):
        print(_fmt_article(it, i))
    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(r, ensure_ascii=False, indent=2))
    d.close()
    return 0


# ---------------------------------------------------------------- show
def cmd_show(args):
    cfg = C.load()
    d = D.open_db(cfg)
    a = d.get_article(args.id, with_html=False)
    if not a:
        print(f"没有 id={args.id}")
        d.close()
        return 1
    print("=" * 66)
    print(a["title"])
    print("=" * 66)
    print(f"站点   {a.get('site_name') or a['site']} · {a.get('column_name')}")
    print(f"时间   {a.get('pub_date')}")
    print(f"来源   {a.get('source_text') or '—'}")
    print(f"字数   {a.get('chars')}")
    print(f"原文   {a.get('url')}")
    if a.get("content_empty"):
        print("注意   本文正文在源站即为空（转载占位条目）")
    print("-" * 66)
    print(a.get("content_text") or "（无正文）")
    d.close()
    return 0


# ---------------------------------------------------------------- config
def cmd_config(args):
    cfg = C.load()
    if args.cfg_cmd == "show":
        print(C.describe(cfg))
    elif args.cfg_cmd == "set":
        val = args.value
        if val.lower() in ("true", "false"):
            val = val.lower() == "true"
        elif val.isdigit():
            val = int(val)
        C.set_path(cfg, args.key, val)
        p = C.save(cfg)
        shown = C.mask_secret(val) if any(
            s in args.key.lower() for s in C.SECRET_KEYS) else val
        print(f"已保存 {args.key} = {shown}  ->  {p}")
    elif args.cfg_cmd == "import":
        try:
            p, notes = C.import_from(Path(args.path))
            print(f"导入完成 -> {p}")
            for n in notes:
                print(f"   {n}")
        except Exception as e:
            print(f"导入失败: {e}")
            return 1
    elif args.cfg_cmd == "export":
        out = Path(args.path)
        out.write_text(C.describe(cfg), encoding="utf-8")
        print(f"已导出(密钥掩码) -> {out}")
    return 0


# ---------------------------------------------------------------- serve
def cmd_serve(args):
    from ..web.app import run
    cfg = C.load()
    host = args.host or cfg["app"]["host"]
    port = args.port or cfg["app"]["port"]
    run(cfg, host=host, port=port, open_browser=args.open_browser)


# ---------------------------------------------------------------- sync / doctor 转发
def cmd_sync(args):
    from ..collector.runner import sync
    cfg = C.load()
    # 年份 → 时间窗转换
    ds = de = None
    if args.year:
        ds, de = f"{args.year}-01-01", f"{args.year}-12-31"
    if args.from_year:
        ds = f"{args.from_year}-01-01"
    if args.to_year:
        de = f"{args.to_year}-12-31"
    return sync(cfg, sites=args.site, mode=args.mode, limit=args.limit,
                verbose=not args.quiet, date_start=ds, date_end=de)


def cmd_doctor(args):
    from ..collector.runner import doctor
    cfg = C.load()
    return doctor(cfg, site=args.site, use_ai=not args.no_ai)


# ---------------------------------------------------------------- api-check
def cmd_api_check(args):
    """
    对任意接口 URL 做只读诊断。
    为什么单独有这个命令：site probe / doctor 都要先把 URL 包成站点配置，
    而对接新站时第一个问题恰恰是「这个地址到底能不能用、返回了什么」——
    diagnose.probe_api 就是为了绕开这个前置包装存在的。本命令只是它的
    人可读外壳，纯转发，绝不写库、不写配置。
    """
    from ..diagnose import probe_api
    res = probe_api(
        args.url,
        method=(args.method or "GET").upper(),
        headers=_parse_kv_pairs(args.header),
        params=_parse_kv_pairs(args.param),
        referer=args.referer or "",
    )
    if args.json:
        # --json 是给脚本消费的：原样吐完整 dict，不做任何裁剪/加工，
        # 否则下游拿不到 suggested 就没法粘配置了。
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print("=" * 66)
    print(f"API 接口诊断  {res['suggested']['method']}  {res['suggested']['api_url']}")
    print("=" * 66)
    verdict = "可用" if res["ok"] else "不可用 / 需人工确认"
    print(f"结论        {verdict}")
    print(f"HTTP 状态   {res['http_status'] or '(未连通)'}")
    print(f"耗时        {res['elapsed_ms']} ms")
    print(f"Content-Type {res['content_type'] or '(无)'}")
    if res["charset_declared"] or res["charset_used"]:
        print(f"编码        声明={res['charset_declared'] or '(无)'} "
              f"实际={res['charset_used'] or '(未知)'}")
    print(f"是 JSON     {'是' if res['is_json'] else '否'}")
    if res["json_top_keys"]:
        print(f"顶层键      {', '.join(res['json_top_keys'])}")
    if res["list_path_guess"]:
        print(f"列表路径    {res['list_path_guess']}  (count={res['count']})")
    if res["referer_required"]:
        print("Referer     必需 —— 去掉后响应变了，站点配置里必须带 referer")
    if res["sample"]:
        print("-" * 66)
        print("响应样本(前300字):")
        print(res["sample"])
    print("-" * 66)
    if res["issues"]:
        print("问题:")
        for i in res["issues"]:
            print(f"  ! {i}")
    else:
        print("问题: (无)")
    print("-" * 66)
    print("可直接贴进站点配置的 suggested:")
    # suggested 里只有 api_url/method/referer/list_path/params/选择器，
    # 不含任何鉴权头 —— 用户用 --header 传的 Cookie/Authorization 不会被回显出来
    print(json.dumps(res["suggested"], ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- 非文章条目查询
def cmd_links(args):
    """
    列出被判定为「非文章」而跳过的条目。

    这些不是失败，是**明确登记**：专题页、站外链接、公众号、脏数据。
    之前它们全被记成「正文抓不到」，同步日志里一片刺眼的失败，
    真问题（2015/2019 年下线的旧稿）反而被淹没。
    """
    from ..collector.classify import KIND_LABEL
    cfg = C.load()
    d = D.open_db(cfg)
    kind = args.kind or ""
    sql = ("SELECT site,title,pub_date,column_name,list_url,target_host,"
           "is_own_site,fail_kind,reason FROM external_links WHERE 1=1")
    args_ = []
    if kind:
        sql += " AND fail_kind=?"; args_.append(kind)
    if args.site:
        sql += " AND site=?"; args_.append(args.site)
    sql += " ORDER BY id DESC LIMIT ?"; args_.append(max(1, args.limit))
    with d._lock:
        rows = [dict(r) for r in d.conn.execute(sql, args_)]
    d.close()
    if not rows:
        print("没有符合条件的非文章条目（还没跑过同步，或已被筛掉）")
        return 0
    tally = {}
    for r in rows:
        tally[r["fail_kind"]] = tally.get(r["fail_kind"], 0) + 1
    print(f"共 {len(rows)} 条："
          + "，".join(f"{KIND_LABEL.get(k, k)} {v}" for k, v in tally.items()))
    print("-" * 78)
    for r in rows:
        own = "本站" if r["is_own_site"] else (r["target_host"] or "站外")
        print(f"[{KIND_LABEL.get(r['fail_kind'], r['fail_kind'])}] {own} "
              f"{r['pub_date']} {r['title'][:38]}")
        print(f"    {r['reason']}")
        print(f"    {r['list_url'][:96]}")
    return 0


# ---------------------------------------------------------------- 站点管理
def cmd_site(args):
    """自定义站点：推断 → 试抓 → 保存。配置写在 config/sites.d/<code>.json"""
    import json as _json
    from .. import sites as S
    from ..collector import probe as P
    cfg = C.load()
    act = args.action

    if act == "list":
        allspec = S.all_sites(cfg)
        if not allspec:
            print("还没有任何站点")
            return 0
        print(f"{'代码':<22}{'类型':<8}{'栏目':<6}{'名称'}")
        print("-" * 66)
        for code, s in allspec.items():
            kind = "内置" if s.get("builtin") else (s.get("kind") or "html")
            flag = ""
            if s.get("_broken"):
                flag = f"  [配置损坏: {s.get('_error')}]"
            elif not s.get("enabled", True):
                flag = "  [已停用]"
            print(f"{code:<22}{kind:<8}{len(s.get('columns') or []):<6}"
                  f"{s.get('name') or code}{flag}")
        return 0

    if act in ("add", "probe"):
        url = args.url
        if not url:
            print("请给网址：cacx site add https://www.example.gov.cn/")
            return 2
        print(f"[1/3] 正在打开 {url} 并推断采集规则 …")
        res = P.draft_from_url(cfg, url, name=args.name or "",
                               use_ai=not args.no_ai)
        for ev in res["evidence"]:
            print(f"      · {ev}")
        for iss in res["issues"]:
            print(f"      ! {iss}")
        draft = res["draft"]
        if not draft:
            print("推断失败，没有产出可用配置。")
            return 1
        code = args.code or S.unique_code(url, draft.get("name", ""))
        draft["code"] = code

        print(f"[2/3] 试抓验证（只读，不入库、不写配置）…")
        rep = P.probe_site(cfg, draft)
        for c in rep.get("columns", []):
            print(f"      · 栏目「{c.get('name')}」抓到 {c.get('listed', 0)} 条")
            for s in (c.get("samples") or [])[:3]:
                print(f"          {s.get('date',''):<12}{s.get('title','')[:40]}")
            if c.get("error"):
                print(f"          ! {c['error']}")
        art = rep.get("article")
        if art:
            print(f"      · 正文样本：{art['title'][:40]} "
                  f"[{art['pub_date'] or '无日期'}] {art['chars']} 字")
            print(f"          {art['preview'][:120]}")
        for iss in rep.get("issues", []):
            print(f"      ! {iss}")

        if act == "probe":
            print("\n（--probe 模式，未保存。要保存请去掉该参数）")
            print(_json.dumps(draft, ensure_ascii=False, indent=2))
            return 0

        if args.force or rep.get("ok"):
            path = S.save_custom(draft)
            print(f"[3/3] 已保存 → {path}")
            print(f"      网站点「数据同步」即可采集该站点。")
        else:
            print("\n[3/3] 试抓没通过，已**放弃保存**。")
            print("      修好上面的问题后重试，或加 --force 强行保存。")
            print(_json.dumps(draft, ensure_ascii=False, indent=2))
            return 1
        return 0

    if act == "remove":
        ok = S.delete_custom(args.code)
        print(("已删除站点配置 " if ok else "找不到站点配置 ") + args.code)
        print("提示：库里已入库的文章不会被删除，需要的话另行处理。")
        return 0 if ok else 1

    if act in ("enable", "disable"):
        ok = S.set_enabled(args.code, act == "enable")
        print(f"{'已启用' if ok else '操作失败'} {args.code}")
        return 0 if ok else 1

    print("未知子命令")
    return 2


# ---------------------------------------------------------------- export
def cmd_export(args):
    """把检索结果导出到本地文件（CSV / JSONL）"""
    import csv as _csv
    cfg = C.load()
    d = D.open_db(cfg)
    r = d.search(
        q=args.query or "", site=args.site or "", column_name=args.column or "",
        year=args.year or "", date_from=args.date_from or "",
        date_to=args.date_to or "", min_chars=args.min_chars,
        sort=args.sort, limit=args.limit, offset=args.offset,
        min_fts_chars=cfg["search"]["min_fts_chars"])

    full = None  # 占位：正文逐条取，不走批量 SQL

    # 逐条补全文（search 不返回正文）
    rows = []
    for it in r["items"]:
        a = d.get_article(it["id"])
        if a:
            rows.append(a)

    out = Path(args.out) if args.out else (
        C.PROJECT_ROOT / "data" /
        f"export_{args.format}_{time.strftime('%Y%m%d_%H%M%S')}.{args.format}")
    out.parent.mkdir(parents=True, exist_ok=True)

    cols = ["id", "site", "site_name", "column_name", "column_code", "title",
            "pub_date", "url", "source_text", "chars", "content_text"]
    if args.format == "csv":
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for a in rows:
                w.writerow(a)
    else:
        with out.open("w", encoding="utf-8") as f:
            for a in rows:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")

    size = out.stat().st_size
    print(f"已导出 {len(rows)} 篇 → {out}  ({size/1024:.1f} KB)")
    print(f"（命中总数 {r['total']}；本次按 limit={args.limit} 截取）")
    d.close()
    return 0


# ---------------------------------------------------------------- ai
def _resolve_sites(cfg, args) -> list:
    """
    解析 summarize 的 --site：从「单值」改成 action="append" 可重复，
    写法照 sync（build_parser 里 sp.add_argument("--site", action="append", ...)）。

    为什么单独抽一层：'--site all' 与显式站点混用的口径（以 all 为准并提示）
    要在选文章之前定死，不然选文章和落 record_sync 两处各算一遍就会漂移。
    返回空 list = 调用方旧行为（检索不按站过滤）。
    """
    raw = list(getattr(args, "site", None) or [])
    if "all" in raw:
        rest = [s for s in raw if s != "all"]
        if rest:
            print(f"提示：--site all 与 {rest} 混用，以 all 为准"
                  f"（全部启用站点：{len(rest)} 被忽略）")
        from .. import sites as S
        return list(S.enabled_codes(cfg))
    return raw


def _pick_articles(cfg, d, args, site_list):
    """
    按参数选出要处理的文章。

    limit 口径（多站点时的取舍，写在这里免得以后翻代码猜）：
      · --all-missing：每站各取 limit 条再合并 —— 缺失摘要的文章通常按时间
        倒序排，逐站各留 limit 篇比全局 limit 更公平（不会被单一大站占满）。
      · 条件检索：db.search 只支持单 site 参数，只能逐站各打 limit 条、
        合并去重后再按 pub_day 倒序截到 limit。即「每站候选上限 = limit」，
        最终总量仍被 limit 截断，不会因为多站点悄悄把 AI 配额翻 N 倍。
      · 单站 / 不指定站：行为与改造前完全一致。

    mode 语义（选文章阶段的开关，与 --resume 独立）：
      · incremental（默认）：--all-missing 走「x.id IS NULL」天然增量 SQL；
        条件检索分支在合并去重后再用 get_annotation 剔掉已有 summarize 摘要
        的文章。为什么在选文章这步就剔：run_batch 的 total 分母按选中篇数写，
        这里不剔会把「注定被跳过」的文章算进进度条分母。
      · full：--all-missing 摘掉 x.id IS NULL（全部有正文的都纳入）；条件
        检索分支不过滤 —— 本来就是要重生成已有摘要的文章。
      · --resume 是 run_batch 提交线程池前的兜底过滤，与本处是两道独立闸：
        mode 管「选文章时算不算它」，--resume 管「真跑时再跳一次」。full
        模式下想跳过已有的，就 --mode full --resume 组合用，双重过滤无害
        （resume 只会看到 skipped=0）。
    """
    if args.id:
        a = d.get_article(args.id)
        return [a] if a else []
    if args.recent:
        return d.recent_articles(args.recent)
    if args.all_missing:
        sql = ("SELECT a.id FROM articles a LEFT JOIN ai_annotations x "
               "ON x.article_id=a.id AND x.task='summarize' ")
        conds: list = []
        if args.mode != "full":
            # incremental：天然增量，SQL 里就只挑还没有摘要的
            conds.append("x.id IS NULL")
        params: list = []
        if site_list:
            # search 封装不支持多站，这里补一段裸 SQL 的 IN 过滤；
            # 站点代码来自 sites.enabled_codes / 用户手填，用占位符传值防注入
            conds.append("a.site IN ("
                        + ",".join("?" for _ in site_list) + ")")
            params.extend(site_list)
        if conds:
            sql += "WHERE " + " AND ".join(conds) + " "
        sql += "ORDER BY a.pub_day DESC LIMIT ?"
        params.append(args.limit)
        with d._lock:
            ids = [r["id"] for r in d.conn.execute(sql, params).fetchall()]
        return [d.get_article(i) for i in ids]
    # 按条件检索
    if not site_list:
        r = d.search(q=args.query or "", site="",
                     column_name=args.column or "", year=args.year or "",
                     date_from=args.date_from or "", date_to=args.date_to or "",
                     sort="date_desc", limit=args.limit,
                     min_fts_chars=cfg["search"]["min_fts_chars"])
        items = list(r["items"])
    else:
        seen, merged = set(), []
        for s in site_list:
            r = d.search(q=args.query or "", site=s,
                         column_name=args.column or "", year=args.year or "",
                         date_from=args.date_from or "", date_to=args.date_to or "",
                         sort="date_desc", limit=args.limit,
                         min_fts_chars=cfg["search"]["min_fts_chars"])
            for it in r["items"]:
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                merged.append(it)
        merged.sort(key=lambda it: it.get("pub_day") or "", reverse=True)
        items = merged[:args.limit]
    if args.mode != "full":
        # incremental：合并去重后剔掉已有摘要的文章。改造前这一支不过滤，是
        # 语义缺口（条件检索会把已有摘要的文章也卷进来烧配额），现在补上。
        items = [it for it in items
                 if d.get_annotation(it["id"], "summarize") is None]
    return [d.get_article(i["id"]) for i in items]


def cmd_ai(args):
    from .. import ai as AIM
    cfg = C.load()

    if args.ai_cmd == "status":
        st = AIM.status(cfg)
        print("=" * 60)
        print("AI 配置状态")
        print("=" * 60)
        print(f"  启用       {st['enabled']}")
        print(f"  供应商     {st['provider']}")
        print(f"  模型       {st['model'] or '(未设置)'}")
        print(f"  接口地址   {st['base_url'] or '(未设置)'}")
        print(f"  密钥       {st['api_key']}   ← 掩码显示")
        print(f"  就绪       {st['ready']}")
        if not st["ready"]:
            print("\n  配置方法（存本项目 config/settings.json，不动其他工具）:")
            print("    cacx config set ai.provider openai_compat")
            print("    cacx config set ai.base_url https://api.example.com/v1")
            print("    cacx config set ai.api_key  sk-xxxx")
            print("    cacx config set ai.model   your-model")
            print("  切换本地:  cacx config set ai.provider ollama")
            print("            cacx config set ai.base_url http://127.0.0.1:11434")
        d = D.open_db(cfg)
        n = d.conn.execute(
            "SELECT COUNT(*) FROM ai_annotations").fetchone()[0]
        print(f"\n  已落库 AI 结果 {n} 条")
        d.close()
        return 0

    # summarize
    # 选文章这步移到 run_batch 之外：选文章要试条件/多站点合并，跑批只是
    # 「拿一批文章跑完交汇总」——两件事分开，Web 端才能直接复用 run_batch。
    site_list = _resolve_sites(cfg, args)
    d = D.open_db(cfg)
    arts = _pick_articles(cfg, d, args, site_list)
    if not arts:
        print("没有选到文章。试试 --query / --recent / --year / --limit")
        d.close()
        return 1

    dry = not args.commit
    mode = "真生成并落库" if not dry else "预览（不外发请求，不写库）"
    print("=" * 60)
    print(f"AI 摘要 — {mode}")
    if site_list:
        print(f"站点       {', '.join(site_list)}")
    print(f"选中 {len(arts)} 篇"
          + (f"（并发 {args.concurrency}"
             + (f"，限速 {args.rate}/s" if args.rate > 0 else "")
             + (f"，resume" if args.resume else "")
             + f"，mode={args.mode}）"))
    print("=" * 60)

    # 真跑前先探一下 AI 是否就绪：build 返回 None（provider=none 等）时
    # run_batch 会把每篇都记成 failure，用户看到的是一片「失败 N 篇」却不知道
    # 根因是没配 AI。这里提前挡掉，给出可操作的配置指引。
    if not dry:
        from ..ai.base import build as _ai_build
        if _ai_build(cfg, task="summarize") is None:
            print("AI 未配置：请先配好 ai.provider / ai.base_url / ai.api_key / ai.model")
            print("  例: cacx config set ai.provider openai_compat")
            d.close()
            return 1

    started = time.strftime("%Y-%m-%d %H:%M:%S")

    def _on_progress(ev: dict):
        # 进度回调只是打屏；run_batch 内部已吞掉回调异常，这里不需要再包
        print(f"  [{ev.get('done',0)}/{ev.get('total',0)}] "
              f"ok={ev.get('ok',0)} fail={ev.get('fail',0)} "
              f"skip={ev.get('skipped',0)}  {str(ev.get('title',''))[:36]}")

    try:
        from ..ai.batch import run_batch
        summary = run_batch(
            cfg, d, arts, dry_run=dry,
            concurrency=args.concurrency, rate=args.rate,
            resume=args.resume, stop_on_error=args.stop_on_error,
            on_progress=_on_progress)
    except AIM.AIError as e:
        # 兜底：万一以后 run_batch 改成把 AIError 往外抛，这里也能给出
        # 和旧版一样清晰的提示而不是 traceback
        print(f"AI 调用失败: {e}")
        d.close()
        return 1

    if dry:
        # 预览模式：run_batch 只对第一篇取一次请求体，不并发不写库
        preview = summary.get("preview")
        if preview is None:
            err = (summary["failures"][0]["error"] if summary["failures"]
                   else "未知错误")
            print(f"预览失败: {err}")
            if "AIError" in err:
                print("（多半是 AI 没配置好；先 cacx ai status 看状态）")
            d.close()
            return 1
        print("预览请求体:")
        print(json.dumps(preview, ensure_ascii=False, indent=2)[:900])
        print("\n（以上为将要发送的内容。加 --commit 才会真调用并写库；"
              "dry-run 铁律：预览模式绝不外发请求）")
        # dry_run 预览不落 record_sync：那是同步批次的台账，预览没有产生任何
        # 实际工作，落一条「成功 0」的记录只会污染历史
        d.close()
        return 0

    # 收尾汇总
    print(f"\n成功 {summary['ok']}  失败 {summary['fail']}  "
          f"跳过 {summary['skipped']}  共 {summary['total']}")
    if args.mode == "full" and not args.resume:
        # 全量模式选文章阶段不过滤已有摘要，收尾再提一句，免得用户没意识到
        # 这次把旧摘要也重跑了一遍（想跳过已有的：--mode full --resume）
        print("提示：全量模式会重新生成已有摘要的文章")
    failures = summary.get("failures") or []
    if failures:
        if args.export_failures:
            out = Path(args.export_failures)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", encoding="utf-8") as f:
                for fl in failures:
                    f.write(json.dumps(
                        {k: fl.get(k) for k in ("id", "title", "url", "error")},
                        ensure_ascii=False) + "\n")
            print(f"失败清单 {len(failures)} 条已写出 → {out}")
        else:
            print("失败明细（前 10 条；要完整清单加 --export-failures）:")
            for fl in failures[:10]:
                print(f"  - [{fl.get('id')}] {str(fl.get('title'))[:40]}")
                print(f"      {fl.get('error')}")

    # 进度持久化：真跑（commit）成功收尾时落一条 sync_runs，mode='summarize'。
    # 与 collector 同步批次同一张表，doctor/历史页一处看全 —— 不新建表是因为
    # 这条记录的语义就是「一次批次跑了多少、成败几何」，和同步批次同构。
    site_tag = ",".join(site_list) if site_list else "all"
    d.record_sync(
        site=site_tag, mode="summarize", started=started,
        listed=summary["total"], new=summary["ok"], failed=summary["fail"],
        note=(f"skipped={summary['skipped']} concurrency={args.concurrency} "
              f"rate={args.rate} resume={args.resume}"))
    d.close()
    return 0 if summary["fail"] == 0 else 2


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(
        prog="cacx", description="网信办公开语料库工具（采集 / 检索 / 分析）")
    p.add_argument("-V", "--version", action="version", version="cacx 0.1.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化配置与数据库").set_defaults(func=cmd_init)

    sp = sub.add_parser("import", help="导入既有 json 语料")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("stats", help="语料统计")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("search", help="检索文章")
    sp.add_argument("query", nargs="?", default="", help="关键词，空则为浏览全部")
    sp.add_argument("--site", help="cac | zjwx")
    sp.add_argument("--column", help="栏目名")
    sp.add_argument("--year", help="年份")
    sp.add_argument("--date-from")
    sp.add_argument("--date-to")
    sp.add_argument("--min-chars", type=int)
    sp.add_argument("--sort", default="date_desc",
                    choices=["date_desc", "date_asc", "chars_desc", "chars_asc"])
    sp.add_argument("-n", "--limit", type=int, default=10)
    sp.add_argument("--json", action="store_true", help="附带 JSON 输出")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("show", help="查看单篇全文")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("config", help="配置管理")
    sp.add_argument("cfg_cmd", choices=["show", "set", "import", "export"])
    sp.add_argument("key", nargs="?", help="set 时的键，如 ai.api_key")
    sp.add_argument("value", nargs="?", help="set 时的值")
    sp.add_argument("--path", help="import/export 的文件路径")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("sync", help="增量同步文章（内置两站 + 你添加的站点）")
    sp.add_argument("--site", action="append",
                    help="可重复；不填则同步全部启用的站")
    sp.add_argument("--mode", default="incremental",
                    choices=["incremental", "full", "list-only"],
                    help="incremental=只补新的 / full=全量刷新 / list-only=只看增量条数")
    sp.add_argument("--year", help="只同步某一年，如 --year 2026")
    sp.add_argument("--from-year", help="起始年份，如 --from-year 2024")
    sp.add_argument("--to-year", help="结束年份，如 --to-year 2026")
    sp.add_argument("--limit", type=int, help="本次最多处理多少条")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("doctor", help="接口自检（站点是否改版）")
    # 站点代码是动态的（用户可以自己加站），不能用 choices 写死
    sp.add_argument("--site", action="append", help="只查指定站点，可重复")
    sp.add_argument("--no-ai", action="store_true", help="不用 AI，仅规则检查")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("api-check",
                        help="对任意接口 URL 做只读诊断（不写库不写配置）")
    sp.add_argument("url", help="接口地址，可省略 http(s):// 前缀")
    sp.add_argument("--method", default="GET", choices=["GET", "POST"])
    sp.add_argument("--referer", default="",
                    help="指定后会自动对比带/不带 Referer 两种响应")
    sp.add_argument("--header", action="append", default=[], metavar="k=v",
                    help="可重复，如 --header Cookie=abc（不会被回显进报告）")
    sp.add_argument("--param", action="append", default=[], metavar="k=v",
                    help="可重复，URL query / 表单参数")
    sp.add_argument("--json", action="store_true", help="输出完整 JSON")
    sp.set_defaults(func=cmd_api_check)

    sp = sub.add_parser("links", help="列出被跳过的非文章条目（专题页/站外/公众号/脏数据）")
    sp.add_argument("--kind", help="只看某一类: index_page/external/wechat/dirty")
    sp.add_argument("--site", help="限定站点")
    sp.add_argument("-n", "--limit", type=int, default=40)
    sp.set_defaults(func=cmd_links)

    sp = sub.add_parser("site", help="站点管理：添加 / 试抓 / 删除自定义站点")
    ssub = sp.add_subparsers(dest="action", required=True)
    ssub.add_parser("list", help="列出全部站点").set_defaults(func=cmd_site)

    spa = ssub.add_parser("add", help="输网址 → 推断规则 → 试抓验证 → 保存")
    spa.add_argument("url", help="站点或栏目列表页地址")
    spa.add_argument("--name", help="站点中文名（不给就从网页标题推断）")
    spa.add_argument("--code", help="站点代码（不给就按域名自动生成）")
    spa.add_argument("--no-ai", action="store_true", help="不用 AI，纯启发式推断")
    spa.add_argument("--force", action="store_true", help="试抓没过也强行保存")
    spa.set_defaults(func=cmd_site)

    spp = ssub.add_parser("probe", help="只推断+试抓并打印配置，不保存")
    spp.add_argument("url")
    spp.add_argument("--name", default="")
    spp.add_argument("--code", default="")
    spp.add_argument("--no-ai", action="store_true")
    spp.set_defaults(func=cmd_site)

    spr = ssub.add_parser("remove", help="删除自定义站点配置（不动已入库文章）")
    spr.add_argument("code")
    spr.set_defaults(func=cmd_site)

    spe = ssub.add_parser("enable", help="启用自定义站点")
    spe.add_argument("code")
    spe.set_defaults(func=cmd_site)
    spd = ssub.add_parser("disable", help="停用自定义站点（同步时跳过）")
    spd.add_argument("code")
    spd.set_defaults(func=cmd_site)

    sp = sub.add_parser("export", help="把检索结果导出到本地文件")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--site")
    sp.add_argument("--column")
    sp.add_argument("--year")
    sp.add_argument("--date-from")
    sp.add_argument("--date-to")
    sp.add_argument("--min-chars", type=int)
    sp.add_argument("--sort", default="date_desc",
                    choices=["date_desc", "date_asc", "chars_desc", "chars_asc"])
    sp.add_argument("-n", "--limit", type=int, default=500,
                    help="最多导出多少篇")
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--format", default="csv", choices=["csv", "jsonl"])
    sp.add_argument("--out", help="输出路径，缺省写到 data/export_*")
    sp.set_defaults(func=cmd_export)

    # ---- AI 子命令组 ----
    spai = sub.add_parser("ai", help="AI 功能（摘要 / 标签 / 自检诊断）")
    spai_sub = spai.add_subparsers(dest="ai_cmd", required=True)

    spai_sub.add_parser("status", help="查看 AI 配置状态（密钥掩码）"
                       ).set_defaults(func=cmd_ai)

    spsum = spai_sub.add_parser("summarize", help="生成摘要（默认只预览）")
    spsum.add_argument("--id", type=int, help="指定单篇 id")
    spsum.add_argument("--recent", type=int, help="最近入库的 N 篇")
    spsum.add_argument("--all-missing", action="store_true",
                       help="所有还没生成过摘要的文章")
    spsum.add_argument("query", nargs="?", default="", help="按关键词选")
    # action="append" 照 sync 的写法（见本文件 sync 子命令的 --site）：
    # 可重复传多个站点；特殊值 all = sites.enabled_codes(cfg) 全部启用站点；
    # 不填 = 检索不按站过滤（旧行为）
    spsum.add_argument("--site", action="append",
                       help="可重复；--site all = 全部启用站点；不填=不按站过滤")
    spsum.add_argument("--column")
    spsum.add_argument("--year")
    spsum.add_argument("--date-from")
    spsum.add_argument("--date-to")
    spsum.add_argument("-n", "--limit", type=int, default=10)
    spsum.add_argument("--commit", action="store_true",
                       help="真调用并写库（不加则只预览，不外发请求）")
    spsum.add_argument("--stop-on-error", action="store_true")
    spsum.add_argument("--resume", action="store_true",
                       help="跳过已有 summarize annotation 的文章（断点续跑）")
    spsum.add_argument("--mode", default="incremental",
                       choices=["incremental", "full"],
                       help="incremental=只处理尚无摘要的（默认）；"
                            "full=全部符合条件重新生成（含已有摘要）")
    spsum.add_argument("--rate", type=float, default=0.0,
                       help="每秒最多 AI 调用次数（0=不限速，避免触发供应商限流）")
    spsum.add_argument("--concurrency", type=int, default=3,
                       help="并发数（默认 3；落库仍串行）")
    spsum.add_argument("--export-failures", metavar="PATH",
                       help="收尾把失败清单写成 JSONL（每行 id,title,url,error）")
    spsum.set_defaults(func=cmd_ai)

    sp = sub.add_parser("serve", help="启动 Web 检索服务")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--open-browser", dest="open_browser",
                    action="store_true", default=None)
    sp.add_argument("--no-browser", dest="open_browser",
                    action="store_false", default=None)
    sp.set_defaults(func=cmd_serve)

    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n已中断")
        return 130