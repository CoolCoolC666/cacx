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
def _pick_articles(cfg, d, args):
    """按参数选出要处理的文章"""
    if args.id:
        a = d.get_article(args.id)
        return [a] if a else []
    if args.recent:
        return d.recent_articles(args.recent)
    if args.all_missing:
        ids = [r["id"] for r in d.conn.execute(
            "SELECT a.id FROM articles a LEFT JOIN ai_annotations x "
            "ON x.article_id=a.id AND x.task='summarize' "
            "WHERE x.id IS NULL ORDER BY a.pub_day DESC LIMIT ?",
            (args.limit,)).fetchall()]
        return [d.get_article(i) for i in ids]
    # 按条件检索
    r = d.search(q=args.query or "", site=args.site or "",
                 column_name=args.column or "", year=args.year or "",
                 date_from=args.date_from or "", date_to=args.date_to or "",
                 sort="date_desc", limit=args.limit,
                 min_fts_chars=cfg["search"]["min_fts_chars"])
    return [d.get_article(i["id"]) for i in r["items"]]


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
    d = D.open_db(cfg)
    arts = _pick_articles(cfg, d, args)
    if not arts:
        print("没有选到文章。试试 --query / --recent / --year / --limit")
        d.close()
        return 1

    mode = "真生成并落库" if args.commit else "预览（不外发请求，不写库）"
    print("=" * 60)
    print(f"AI 摘要 — {mode}")
    print(f"选中 {len(arts)} 篇")
    print("=" * 60)

    if not args.commit:
        try:
            preview = AIM.summarize(cfg, arts[0], dry_run=True)
            print("预览请求体:")
            print(json.dumps(preview, ensure_ascii=False, indent=2)[:900])
            print("\n（以上为将要发送的内容。加 --commit 才会真调用并写库）")
            d.close()
            return 0
        except Exception as e:
            print(f"预览失败: {e}")
            d.close()
            return 1

    ok = fail = 0
    for i, a in enumerate(arts, 1):
        try:
            res = AIM.summarize(cfg, a, dry_run=False)
            d.save_annotation(a["id"], "summarize", res,
                              provider=cfg["ai"]["provider"],
                              model=cfg["ai"]["model"])
            ok += 1
            tags = "、".join((res.get("tags") or [])[:5]) if isinstance(res, dict) else ""
            print(f"  [{i}/{len(arts)}] OK  {a['title'][:36]}  [{tags}]")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(arts)}] 失败 {type(e).__name__}: {str(e)[:80]}")
            if args.stop_on_error:
                break
    print(f"\n成功 {ok}  失败 {fail}")
    d.close()
    return 0 if fail == 0 else 2


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
    spsum.add_argument("--site")
    spsum.add_argument("--column")
    spsum.add_argument("--year")
    spsum.add_argument("--date-from")
    spsum.add_argument("--date-to")
    spsum.add_argument("-n", "--limit", type=int, default=10)
    spsum.add_argument("--commit", action="store_true",
                       help="真调用并写库（不加则只预览，不外发请求）")
    spsum.add_argument("--stop-on-error", action="store_true")
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