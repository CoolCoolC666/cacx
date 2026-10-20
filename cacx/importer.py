# -*- coding: utf-8 -*-
"""
语料导入器
================================================================
把既有的三层产物（html + md + json）批量灌进 SQLite。
json 是主数据源（字段最全）；json 缺失时从同名 md 的 YAML frontmatter 回退解析。

同时导入：
    · external_links —— 站外链接条目（此前只躺在 errors.log / csv 里）
"""

import csv
import json
import time
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

from . import config as cfgmod

SITE_META = {
    "中国网信办": {"site": "cac", "site_name": "中国网信办"},
    "浙江网信网": {"site": "zjwx", "site_name": "浙江网信网"},
}

# 反查表：site 代码 -> 显示名（用于给 json 里缺失的 site_name 补值）
SITE_NAME = {v["site"]: v["site_name"] for v in SITE_META.values()}

FM_RE = None


def _parse_frontmatter(md_text: str) -> Dict[str, str]:
    """从 md 的 YAML frontmatter 里抠字段（json 缺失时的兜底）"""
    global FM_RE
    if FM_RE is None:
        import re
        FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.S)
    m = FM_RE.match(md_text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _iter_site_files(root: Path) -> Iterator[Tuple[str, Path]]:
    for col_dir in sorted(root.glob("[0-9][0-9]_*")):
        if not col_dir.is_dir():
            continue
        for jf in sorted(col_dir.glob("*.json")):
            yield col_dir.name, jf


def _norm_from_json(d: Dict, site_dir: str, site: str) -> Dict:
    col = d.get("column") or (site_dir.split("_", 1)[-1] if "_" in site_dir else site_dir)
    code = d.get("channel_code") or d.get("column_code") or d.get("column_id") or ""
    text = d.get("content_text") or ""
    return {
        "site": site or d.get("source_site", ""),
        # 语料 json 里没有 site_name 字段，按 site 代码补上
        "site_name": d.get("site_name") or SITE_NAME.get(site, ""),
        "column_code": str(code),
        "column_name": col,
        "title": d.get("title", ""),
        "pub_date": d.get("pub_date", ""),
        "url": d.get("url", ""),
        "source_text": d.get("source_text", ""),
        "chars": len(text.strip()),
        "content_text": text,
        "content_html": d.get("content_html", ""),
        "local_path": d.get("html_path", ""),
        "content_empty": bool(d.get("content_empty")),
    }


def _norm_from_md(fm: Dict, site_dir: str, site: str) -> Dict:
    col = fm.get("column") or (site_dir.split("_", 1)[-1] if "_" in site_dir else site_dir)
    return {
        "site": site,
        "site_name": SITE_NAME.get(site, ""),
        "column_code": str(fm.get("col_code") or fm.get("column_code") or ""),
        "column_name": col,
        "title": fm.get("title", ""),
        "pub_date": fm.get("pub_date", ""),
        "url": fm.get("url", ""),
        "source_text": fm.get("source", ""),
        "chars": 0,
        "content_text": "",
        "content_html": "",
        "local_path": "",
        "content_empty": False,
    }


def discover_corpus_roots(project_root: Path) -> Dict[str, Path]:
    """
    推断两个站点语料目录位置。
    优先项目根同级；找不到就让调用方传参。
    """
    found = {}
    for site_dir_name, meta in SITE_META.items():
        for cand in (project_root / site_dir_name,
                     project_root.parent / site_dir_name):
            if cand.is_dir():
                found[meta["site"]] = cand
                break
    return found


def import_corpus(db, roots: Dict[str, Path], verbose: bool = True
                  ) -> Dict[str, int]:
    """把两站语料灌进 db；返回统计"""
    stats = {"scanned": 0, "inserted": 0, "dup": 0, "empty": 0, "external": 0,
             "from_md": 0}
    t0 = time.time()
    conn = db.conn
    conn.execute("BEGIN")

    for site, root in roots.items():
        if not root.exists():
            if verbose:
                print(f"  [warn] 语料目录不存在，跳过: {root}")
            continue
        site_scanned = site_inserted = 0
        for col_dir, jf in _iter_site_files(root):
            stats["scanned"] += 1
            site_scanned += 1
            rec = None
            if jf.suffix == ".json":
                try:
                    d = json.loads(jf.read_text(encoding="utf-8"))
                    rec = _norm_from_json(d, col_dir, site)
                except Exception:
                    rec = None
            if rec is None:
                mf = jf.with_suffix(".md")
                if mf.exists():
                    rec = _norm_from_md(
                        _parse_frontmatter(mf.read_text(encoding="utf-8")), col_dir, site)
                    stats["from_md"] += 1
            if rec is None:
                continue
            if rec.get("content_empty"):
                stats["empty"] += 1
            if db.upsert_article(rec):
                stats["inserted"] += 1
                site_inserted += 1
            else:
                stats["dup"] += 1
        if verbose:
            print(f"  {site}: 扫描 {site_scanned} → 新增入库 {site_inserted}")

    conn.commit()

    # 站外链接（此前是独立 csv，不在语料目录内）
    ext_csv = project_root_of(roots) / "日志" / "_logs" / "站外链接条目清单.csv"
    if ext_csv.exists():
        with ext_csv.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                db.upsert_external({
                    "site": "cac",
                    "title": row.get("title", ""),
                    "pub_date": row.get("pub_date", ""),
                    "column_code": row.get("channel_code", ""),
                    "column_name": row.get("column", ""),
                    "list_url": row.get("list_url", ""),
                    "target_host": row.get("target_host", ""),
                    "is_own_site": row.get("is_own_site") == "是",
                    "fail_kind": row.get("fail_kind", ""),
                    "reason": row.get("reason", ""),
                })
                stats["external"] += 1
        db.conn.commit()

    db.rebuild_fts()
    db.set_meta("last_import_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def project_root_of(roots: Dict[str, Path]) -> Path:
    """从语料根反推任务根（用于定位日志目录）"""
    for r in roots.values():
        return r.parent
    return cfgmod.PROJECT_ROOT