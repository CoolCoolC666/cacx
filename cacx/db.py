# -*- coding: utf-8 -*-
"""
SQLite 存储与检索
================================================================
检索方案（中文全文）
    SQLite FTS5 的 `trigram` 分词器对中文有效，但**要求查询词 >= 3 个字符**；
    实测 2 字词（如「清朗」）命中数为 0。因此：
        · 查询词 >= 3 字  → 走 FTS5 索引（快，支持短语与多词）
        · 查询词 1~2 字   → 退回 LIKE 扫描（慢一点，但结果正确）
    阈值由 settings['search']['min_fts_chars'] 控制。

表结构
    articles         文章主表（site, column, title, pub_date, content…）
    articles_fts     FTS5 外部内容索引（external content + 触发器）
    external_links   站外链接 / 异常条目（采不到正文的，显式登记而非静默丢弃）
    sync_runs        每次同步的批次记录
    ai_annotations   AI 产物（摘要 / 标签 / 自愈诊断），按 (article_id, task) 唯一
"""

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    site_name     TEXT,
    column_code   TEXT,
    column_name   TEXT,
    title         TEXT NOT NULL,
    pub_date      TEXT,
    pub_day       TEXT,
    year          TEXT,
    month         TEXT,
    url           TEXT,
    source_text   TEXT,
    chars         INTEGER DEFAULT 0,
    content_text  TEXT,
    content_html  TEXT,
    local_path    TEXT,
    content_empty INTEGER DEFAULT 0,
    first_seen    TEXT,
    synced_at     TEXT,
    UNIQUE (site, url)
);

CREATE INDEX IF NOT EXISTS idx_articles_site    ON articles(site);
CREATE INDEX IF NOT EXISTS idx_articles_col     ON articles(site, column_code);
CREATE INDEX IF NOT EXISTS idx_articles_day     ON articles(pub_day);
CREATE INDEX IF NOT EXISTS idx_articles_year    ON articles(year);

-- FTS5 外部内容索引
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    content_text,
    content='articles',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
    INSERT INTO articles_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END;

CREATE TABLE IF NOT EXISTS external_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site         TEXT,
    title        TEXT,
    pub_date     TEXT,
    column_code  TEXT,
    column_name  TEXT,
    list_url     TEXT UNIQUE,
    target_host  TEXT,
    is_own_site  INTEGER DEFAULT 0,
    fail_kind    TEXT,
    reason       TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    site           TEXT,
    mode           TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    listed         INTEGER DEFAULT 0,
    new_articles   INTEGER DEFAULT 0,
    failed         INTEGER DEFAULT 0,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS ai_annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    task       TEXT NOT NULL,
    provider   TEXT,
    model      TEXT,
    result     TEXT,
    created_at TEXT,
    UNIQUE (article_id, task)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 会把同步端点丢进线程池，
        # 同一个连接会被多个线程轮流用。配合 WAL 模式，这是 SQLite
        # 在「一个进程内并发读」下的标准做法。
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 写锁：共享连接下多线程同时写会撞 "database is locked"
        self._lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    # ---------------------------------------------------------- 基础
    def close(self):
        with self._lock:
            self.conn.commit()
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def set_meta(self, k, v):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))

    def get_meta(self, k, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r["value"] if r else default

    # ---------------------------------------------------------- 写入
    def upsert_article(self, rec: Dict[str, Any]) -> bool:
        """
        按 (site,url) 去重写入，返回是否**新增**。
        已存在的行做幂等刷新（修字段用），但保留 first_seen ——
        重复导入应当能修正既有数据，而不是静默跳过。
        """
        pub = (rec.get("pub_date") or "").strip()
        day = pub[:10] if len(pub) >= 10 else ""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            exists = self.conn.execute(
                "SELECT id FROM articles WHERE site=? AND url=?",
                (rec.get("site", ""), rec.get("url", ""))).fetchone()
            cur = self.conn.execute(
                """INSERT INTO articles
                   (site, site_name, column_code, column_name, title, pub_date,
                    pub_day, year, month, url, source_text, chars,
                    content_text, content_html, local_path, content_empty,
                    first_seen, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(site, url) DO UPDATE SET
                     -- 空值不得覆盖好值。
                     -- 教训：曾把 DO NOTHING 改成 DO UPDATE 后，某个 bug
                     -- 让 sync 拿着空的 pub_date/content 反复 upsert，
                     -- 把 1407 条完好记录刷成了空壳。刷新可以，但必须
                     -- 保证「拿到的东西更差」时不会降级已有数据。
                     site_name    = COALESCE(NULLIF(excluded.site_name,''),    articles.site_name),
                     column_code  = COALESCE(NULLIF(excluded.column_code,''),  articles.column_code),
                     column_name  = COALESCE(NULLIF(excluded.column_name,''),  articles.column_name),
                     title        = COALESCE(NULLIF(excluded.title,''),        articles.title),
                     pub_date     = COALESCE(NULLIF(excluded.pub_date,''),     articles.pub_date),
                     pub_day      = COALESCE(NULLIF(excluded.pub_day,''),      articles.pub_day),
                     year         = COALESCE(NULLIF(excluded.year,''),         articles.year),
                     month        = COALESCE(NULLIF(excluded.month,''),        articles.month),
                     source_text  = COALESCE(NULLIF(excluded.source_text,''),  articles.source_text),
                     chars        = CASE WHEN excluded.chars > 0 THEN excluded.chars ELSE articles.chars END,
                     content_text = COALESCE(NULLIF(excluded.content_text,''), articles.content_text),
                     content_html = COALESCE(NULLIF(excluded.content_html,''), articles.content_html),
                     local_path   = COALESCE(NULLIF(excluded.local_path,''),   articles.local_path),
                     content_empty= CASE WHEN excluded.chars > 0 THEN excluded.content_empty
                                         ELSE articles.content_empty END,
                     synced_at    = excluded.synced_at""",
                (rec.get("site", ""), rec.get("site_name", ""),
                 rec.get("column_code", ""), rec.get("column_name", ""),
                 rec.get("title", ""), pub, day, day[:4], day[:7],
                 rec.get("url", ""), rec.get("source_text", ""),
                 rec.get("chars", 0), rec.get("content_text", ""),
                 rec.get("content_html", ""), rec.get("local_path", ""),
                 1 if rec.get("content_empty") else 0, now, now))
            return exists is None

    def upsert_external(self, rec: Dict[str, Any]):
        with self._lock:
            self.conn.execute(
                """INSERT INTO external_links
                   (site,title,pub_date,column_code,column_name,list_url,
                    target_host,is_own_site,fail_kind,reason,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(list_url) DO NOTHING""",
                (rec.get("site", ""), rec.get("title", ""), rec.get("pub_date", ""),
                 rec.get("column_code", ""), rec.get("column_name", ""),
                 rec.get("list_url", ""), rec.get("target_host", ""),
                 1 if rec.get("is_own_site") else 0, rec.get("fail_kind", ""),
                 rec.get("reason", ""), time.strftime("%Y-%m-%d %H:%M:%S")))

    def record_sync(self, site: str, mode: str, started: str, listed: int,
                    new: int, failed: int, note: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO sync_runs
                   (site,mode,started_at,finished_at,listed,new_articles,failed,note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (site, mode, started, time.strftime("%Y-%m-%d %H:%M:%S"),
                 listed, new, failed, note))
            self.conn.commit()
            return cur.lastrowid

    def save_annotation(self, article_id: int, task: str, result,
                        provider: str = "", model: str = ""):
        with self._lock:
            self.conn.execute(
                """INSERT INTO ai_annotations
                   (article_id,task,provider,model,result,created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(article_id,task) DO UPDATE SET
                     result=excluded.result, provider=excluded.provider,
                     model=excluded.model, created_at=excluded.created_at""",
                (article_id, task, provider, model,
                 result if isinstance(result, str)
                 else json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def get_annotation(self, article_id: int, task: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT * FROM ai_annotations WHERE article_id=? AND task=?",
            (article_id, task)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------- 检索
    @staticmethod
    def _fts_quote(q: str) -> str:
        """把用户输入转成安全的 FTS5 查询串（多词 AND）"""
        toks = [t for t in re.split(r'[\s,，、;；]+', q.strip()) if t]
        return " AND ".join('"' + t.replace('"', '""') + '"' for t in toks)

    def search(self,
               q: str = "",
               site: str = "",
               column_code: str = "",
               column_name: str = "",
               year: str = "",
               date_from: str = "",
               date_to: str = "",
               min_chars: Optional[int] = None,
               skip_empty: bool = True,
               sort: str = "date_desc",
               limit: int = 20,
               offset: int = 0,
               min_fts_chars: int = 3) -> Dict[str, Any]:
        """
        组合检索。
        返回 {total, items, mode, took_ms}
        mode 会标明走的是 fts 还是 like，便于排查「为什么搜不到」。

        ⚠ 读路径也必须持锁：共享连接下，写线程提交事务会让其他线程
          正在使用的游标失效（InterfaceError: bad parameter or other
          API misuse）。压测 6 读 + 3 写复现过，单加写锁挡不住。
          本工具是单机单用户，查询本身是毫秒级，串行化开销可接受。
        """
        with self._lock:
            return self._search_locked(
                q, site, column_code, column_name, year, date_from, date_to,
                min_chars, skip_empty, sort, limit, offset, min_fts_chars)

    def _search_locked(self, q, site, column_code, column_name, year,
                       date_from, date_to, min_chars, skip_empty, sort,
                       limit, offset, min_fts_chars) -> Dict[str, Any]:
        t0 = time.time()
        where, args = [], []

        if site:
            where.append("a.site = ?"); args.append(site)
        if column_code:
            where.append("a.column_code = ?"); args.append(column_code)
        if column_name:
            where.append("a.column_name = ?"); args.append(column_name)
        if year:
            where.append("a.year = ?"); args.append(str(year))
        if date_from:
            where.append("a.pub_day >= ?"); args.append(date_from)
        if date_to:
            where.append("a.pub_day <= ?"); args.append(date_to)
        if min_chars is not None:
            where.append("a.chars >= ?"); args.append(int(min_chars))
        if skip_empty:
            where.append("COALESCE(a.content_empty,0) = 0")

        q = (q or "").strip()
        mode = "none"
        join = ""
        use_fts = False

        if q:
            # 逐词判断长度：只要有任一词短于阈值，就整体退回 LIKE，
            # 避免 "清朗 专项行动" 这种组合词半边走索引半边走扫描导致漏结果
            toks = [t for t in re.split(r'[\s,，、;；]+', q) if t]
            use_fts = all(len(t) >= min_fts_chars for t in toks)
            if use_fts:
                join = ("JOIN articles_fts f ON f.rowid = a.id "
                        "AND articles_fts MATCH ?")
                args.insert(0, self._fts_quote(q))
                mode = "fts"
            else:
                mode = "like"
                for t in toks:
                    where.append("(a.title LIKE ? OR a.content_text LIKE ?)")
                    args += [f"%{t}%", f"%{t}%"]

        if sort == "date_asc":
            order = "a.pub_day ASC, a.id ASC"
        elif sort == "chars_desc":
            order = "a.chars DESC"
        elif sort == "chars_asc":
            order = "a.chars ASC"
        else:
            order = "a.pub_day DESC, a.id DESC"

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        base = f"FROM articles a {join}{where_sql}"

        total = self.conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT a.id, a.site, a.site_name, a.column_name, a.column_code,
                       a.title, a.pub_date, a.url, a.source_text, a.chars,
                       a.content_empty
                {base}
                ORDER BY {order} LIMIT ? OFFSET ?""",
            args + [int(limit), int(offset)]).fetchall()

        return {
            "total": total,
            "mode": mode,
            "items": [dict(r) for r in rows],
            "took_ms": int((time.time() - t0) * 1000),
        }

    def get_article(self, article_id: int, with_html: bool = False) -> Optional[dict]:
        cols = ("a.*" if with_html else
                "a.id,a.site,a.site_name,a.column_name,a.column_code,a.title,"
                "a.pub_date,a.url,a.source_text,a.chars,a.content_text,"
                "a.content_empty,a.local_path")
        with self._lock:
            r = self.conn.execute(f"SELECT {cols} FROM articles a WHERE a.id=?",
                                  (article_id,)).fetchone()
            return dict(r) if r else None

    def get_by_url(self, site: str, url: str) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute(
                "SELECT id FROM articles WHERE site=? AND url=?",
                (site, url)).fetchone()
        return self.get_article(r["id"]) if r else None

    # ---------------------------------------------------------- 统计
    def facets(self) -> Dict[str, Any]:
      with self._lock:
        sites = [dict(r) for r in self.conn.execute(
            "SELECT site, site_name, COUNT(*) n, SUM(chars) c "
            "FROM articles GROUP BY site, site_name ORDER BY n DESC")]
        cols = [dict(r) for r in self.conn.execute(
            "SELECT site, column_code, column_name, COUNT(*) n "
            "FROM articles GROUP BY site, column_code, column_name "
            "ORDER BY site, n DESC")]
        years = [dict(r) for r in self.conn.execute(
            "SELECT year, COUNT(*) n FROM articles WHERE year<>'' "
            "GROUP BY year ORDER BY year")]
        empty = self.conn.execute(
            "SELECT COUNT(*) FROM articles WHERE COALESCE(content_empty,0)=1"
        ).fetchone()[0]
        ext = self.conn.execute(
            "SELECT COUNT(*) FROM external_links").fetchone()[0]
        total, chars, docs = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(chars),0), "
            "COUNT(DISTINCT pub_day) FROM articles").fetchone()
        return {"total": total, "chars": chars, "days": docs,
                "empty": empty, "external": ext,
                "sites": sites, "columns": cols, "years": years}

    def integrity(self) -> Dict[str, Any]:
        """
        数据体检：主动揪出「看起来采到了、实际不可用」的行。
        ghost = 有标题没正文，且 content_empty 没打标 —— 说明是抓取中途
        留下的半成品，而不是「源站本来就没有正文」。这类行必须清掉。
        """
        with self._lock:
            ghost = [dict(r) for r in self.conn.execute(
                "SELECT id,site,title,url,pub_date,chars FROM articles "
                "WHERE COALESCE(chars,0)=0 AND COALESCE(content_empty,0)=0")]
            no_date = self.conn.execute(
                "SELECT COUNT(*) FROM articles WHERE COALESCE(pub_day,'')=''"
            ).fetchone()[0]
            dup_title = self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT title FROM articles "
                "WHERE COALESCE(title,'')<>'' GROUP BY title HAVING COUNT(*)>1)"
            ).fetchone()[0]
            no_url = self.conn.execute(
                "SELECT COUNT(*) FROM articles WHERE COALESCE(url,'')=''").fetchone()[0]
        return {"ghost": ghost, "ghost_count": len(ghost),
                "no_date": no_date, "dup_title_groups": dup_title, "no_url": no_url}

    def rebuild_fts(self):
        """批量导入后重建索引（比逐条触发器快得多）"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
            self.conn.commit()

    def optimize(self):
        with self._lock:
            self.conn.execute(
                "INSERT INTO articles_fts(articles_fts) VALUES('optimize')")
            self.conn.execute("VACUUM")
            self.conn.commit()

    def recent_articles(self, n: int = 10) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id,site,column_name,title,pub_date,url,chars FROM articles "
                "ORDER BY synced_at DESC, id DESC LIMIT ?", (n,))]


def open_db(cfg: Dict[str, Any]) -> Database:
    """按配置打开数据库（相对路径按项目根解析）"""
    from . import config as cfgmod
    p = cfg.get("db", {}).get("path", "data/cacx.db")
    path = Path(p)
    if not path.is_absolute():
        path = cfgmod.PROJECT_ROOT / path
    return Database(path)