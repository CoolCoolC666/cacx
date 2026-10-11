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

-- 用户行为埋点：检索 / 跳原文 / 导出。
-- 「检索到哪篇、点了哪篇」是舆情传播分析里最直接的一手信号，
-- 所以原文跳转走本服务中转而不是直链，就是为了能把这条记下来。
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- search / open / export / summarize
    article_id INTEGER,                -- 可空：search 不针对单篇
    detail     TEXT,                   -- 检索词等
    meta       TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind_time ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_article  ON events(article_id);
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
               min_fts_chars: int = 3,
               only_empty: bool = False) -> Dict[str, Any]:
        """
        组合检索。
        返回 {total, items, mode, took_ms}
        mode 会标明走的是 fts 还是 like，便于排查「为什么搜不到」。

        skip_empty / only_empty 是**三态**而不是布尔：
            skip_empty=True （默认）只返回有正文的
            only_empty=True 只返回空正文的（占位条目视图）
            两者都 False       全部返回（要在一个列表里混着看时用）
        ⚠ 不能用「skip_empty 取反」来实现 only_empty：那样会把
        「未打标的半成品」（chars=0 且 content_empty=0，抓取中途留下的）
        也当成空正文混进来。半成品是脏数据，占位不是。

        ⚠ 空正文条目**不在 FTS 索引里**（没正文可索引），所以带 q 时
        它们只能靠 title LIKE 命中 —— 检索「正文里的词」是找不到它们的，
        这是正确行为，不是 bug。

        ⚠ 读路径也必须持锁：共享连接下，写线程提交事务会让其他线程
          正在使用的游标失效（InterfaceError: bad parameter or other
          API misuse）。压测 6 读 + 3 写复现过，单加写锁挡不住。
          本工具是单机单用户，查询本身是毫秒级，串行化开销可接受。
        """
        with self._lock:
            return self._search_locked(
                q, site, column_code, column_name, year, date_from, date_to,
                min_chars, skip_empty, sort, limit, offset, min_fts_chars,
                only_empty)

    def _search_locked(self, q, site, column_code, column_name, year,
                       date_from, date_to, min_chars, skip_empty, sort,
                       limit, offset, min_fts_chars,
                       only_empty=False) -> Dict[str, Any]:
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
        if skip_empty and not only_empty:
            where.append("COALESCE(a.content_empty,0) = 0")
        if only_empty:
            # 只认「明确打了 content_empty 标」的行。
            # chars=0 但没打标的半成品（抓取中途留下的）不算占位 ——
            # 那是脏数据，integrity() 会单独报出来，不能混进占位视图。
            where.append("COALESCE(a.content_empty,0) = 1")

        q = (q or "").strip()
        mode = "none"
        join = ""
        use_fts = False

        if q:
            # 逐词判断长度：只要有任一词短于阈值，就整体退回 LIKE，
            # 避免 "清朗 专项行动" 这种组合词半边走索引半边走扫描导致漏结果
            toks = [t for t in re.split(r'[\s,，、;；]+', q) if t]
            use_fts = all(len(t) >= min_fts_chars for t in toks)
            if only_empty:
                # 占位条目没有正文 → 不在 FTS 索引里，走 FTS 必然 0 结果。
                # 强制退回标题 LIKE，否则用户在占位视图里搜什么都是空的，
                # 会误以为是功能坏了。
                use_fts = False
            if use_fts:
                join = ("JOIN articles_fts f ON f.rowid = a.id "
                        "AND articles_fts MATCH ?")
                args.insert(0, self._fts_quote(q))
                mode = "fts"
            else:
                mode = "like"
                for t in toks:
                    if only_empty:
                        where.append("a.title LIKE ?")
                        args.append(f"%{t}%")
                    else:
                        where.append("(a.title LIKE ? OR a.content_text LIKE ?)")
                        args += [f"%{t}%", f"%{t}%"]

        if sort == "date_asc":
            order = "a.pub_day ASC, a.id ASC"
        elif sort == "chars_desc":
            order = "a.chars DESC"
        elif sort == "chars_asc":
            order = "a.chars ASC"
        elif sort == "retry_first":
            # 占位视图专用：最早入库的排前面 —— 那批通常也是
            # 站点改版导致解析器失效、值得优先重抓的。
            order = "a.first_seen ASC, a.id ASC"
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
            "only_empty": bool(only_empty),
            # 占位视图里「有多少条能按标题搜到」是用户最常问的
            # 「为什么搜不到」，直接给数字，别让人自己猜。
            "title_searchable": not only_empty or mode == "like",
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

    # ---------------------------------------------------------- 命中片段
    @staticmethod
    def make_snippet(text: str, terms: List[str], width: int = 90) -> str:
        """
        截取第一个命中词周围的片段，供列表页高亮。

        必须在 Python 侧做而不是 SQL：中文没法用 substr 找多字节词的边界，
        且 SQLite 的 LIKE 不给位置。宁可多读一点文本，也别让「片段」变成猜测。
        """
        if not text:
            return ""
        terms = [t for t in terms if t]
        if not terms:
            return text[:width].replace("\n", " ")
        low = text.lower()
        pos = -1
        for t in terms:
            p = low.find(t.lower())
            if p >= 0 and (pos < 0 or p < pos):
                pos = p
        if pos < 0:
            return text[:width].replace("\n", " ")
        start = max(0, pos - width // 3)
        end = min(len(text), start + width)
        frag = text[start:end].replace("\n", " ").replace("\r", "")
        return ("…" if start > 0 else "") + frag + ("…" if end < len(text) else "")

    def annotate_snippets(self, items: List[dict], q: str,
                          width: int = 90) -> List[dict]:
        """给 search 结果就地补 snippet + highlights（不改 DB，不影响 CLI）"""
        terms = [t for t in re.split(r'[\s,，、;；]+', q or "") if t]
        if not terms:
            for it in items:
                it["snippet"] = ""
                it["highlights"] = []
            return items
        ids = [it["id"] for it in items]
        if not ids:
            return items
        ph = ",".join("?" * len(ids))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT id, content_text FROM articles WHERE id IN ({ph})",
                ids).fetchall()
        by_id = {r["id"]: r["content_text"] or "" for r in rows}
        for it in items:
            txt = by_id.get(it["id"], "")
            it["snippet"] = self.make_snippet(txt, terms, width)
            it["highlights"] = [t for t in terms if t.lower() in txt.lower()]
        return items

    # ---------------------------------------------------------- 埋点
    def record_event(self, kind: str, article_id: int = 0,
                     detail: str = "", meta: str = ""):
        """记录一条用户行为（检索/点击/导出），供传播分析用"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(kind, article_id, detail, meta, created_at) "
                "VALUES(?,?,?,?,?)",
                (kind, article_id or None, detail, meta,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def event_stats(self, since_days: int = 30) -> Dict[str, Any]:
        since = time.strftime("%Y-%m-%d",
                              time.localtime(time.time() - since_days * 86400))
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE created_at>=?",
                (since,)).fetchone()[0]
            by_kind = [dict(r) for r in self.conn.execute(
                "SELECT kind, COUNT(*) n FROM events WHERE created_at>=? "
                "GROUP BY kind ORDER BY n DESC", (since,))]
            top = [dict(r) for r in self.conn.execute(
                "SELECT e.article_id, a.title, a.site_name, COUNT(*) n "
                "FROM events e LEFT JOIN articles a ON a.id=e.article_id "
                "WHERE e.kind='open' AND e.created_at>=? "
                "GROUP BY e.article_id ORDER BY n DESC LIMIT 10", (since,))]
        return {"since": since, "total": total, "by_kind": by_kind, "top_open": top}

    # ---------------------------------------------------------- 批量导出
    def fetch_for_export(self, ids: List[int]) -> List[dict]:
        """按 id 列表取完整记录（含正文），保持传入顺序"""
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT id,site,site_name,column_name,column_code,title,"
                f"pub_date,url,source_text,chars,content_text "
                f"FROM articles WHERE id IN ({ph})", ids).fetchall()
        got = {r["id"]: dict(r) for r in rows}
        return [got[i] for i in ids if i in got]

    # ---------------------------------------------------------- AI 产物
    def list_annotations(self, task: str = "summarize", q: str = "",
                         site: str = "", limit: int = 50,
                         offset: int = 0) -> Dict[str, Any]:
        """
        列出已落库的 AI 产物（摘要/标签），用于「集中查看」。

        result 存的是 JSON 文本，筛选关键词得在 Python 侧做而不是 SQL：
        SQLite 没有内置 JSON 查询，且中文 LIKE 在长文本上会全表扫。
        先用 SQL 把候选集按时间倒序拉出来（带分页），再筛。
        """
        where, args = ["x.task = ?"], [task]
        if site:
            where.append("a.site = ?"); args.append(site)
        base = ("FROM ai_annotations x JOIN articles a ON a.id = x.article_id "
                f"WHERE {' AND '.join(where)}")
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
            rows = self.conn.execute(
                f"""SELECT x.id AS ann_id, x.provider, x.model, x.created_at,
                           a.id, a.title, a.site, a.site_name, a.column_name,
                           a.pub_date, a.chars, a.url, x.result
                    {base}
                    ORDER BY x.created_at DESC, x.id DESC
                    LIMIT ? OFFSET ?""",
                args + [int(limit), int(offset)]).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            try:
                d["parsed"] = json.loads(d.pop("result") or "{}")
            except Exception:
                d["parsed"] = {"summary": d.pop("result", "")}
            items.append(d)

        if q:
            ql = q.lower()
            items = [it for it in items
                     if ql in (it["parsed"].get("summary") or "").lower()
                     or ql in (it["title"] or "").lower()
                     or any(ql in str(t).lower()
                            for t in it["parsed"].get("tags") or [])]
        return {"total": total, "matched": len(items), "items": items}

    def annotation_stats(self) -> Dict[str, Any]:
        """AI 覆盖率：已生成 / 总数 / 按任务分布"""
        total = self.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        with self._lock:
            by_task = [dict(r) for r in self.conn.execute(
                "SELECT task, COUNT(*) n FROM ai_annotations GROUP BY task")]
            with_content = self.conn.execute(
                "SELECT COUNT(*) FROM articles "
                "WHERE COALESCE(content_empty,0)=0").fetchone()[0]
        done = 0
        for r in by_task:
            if r["task"] == "summarize":
                done = r["n"]
        # 分母用「有正文的文章」：源站即空的条目生成摘要没意义
        return {
            "total": total,
            "summarizable": with_content,
            "summarized": done,
            "coverage": round(done / with_content * 100, 2) if with_content else 0.0,
            "by_task": by_task,
        }

    def recent_sync_runs(self, n: int = 10) -> List[dict]:
        """
        最近的同步批次。失败批次必须看得见 —— 不然用户不知道
        「上次同步有没有把东西抓漏」。

        列名以表结构为准：sync_runs 里是 new_articles，不是 listed_new
        （SQL 的 INSERT 列名和 Python 参数名本来就允许不一致，
         这里曾照着参数名写查询，直接 500）。
        """
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT site, mode, started_at, finished_at, listed, "
                "new_articles, failed, note "
                "FROM sync_runs ORDER BY id DESC LIMIT ?", (int(n),))]

    # ---------------------------------------------------------- 统计
    def facets(self) -> Dict[str, Any]:
        return self.facets_by_site("")

    def facets_by_site(self, site: str = "") -> Dict[str, Any]:
        """
        分面统计。site 非空时**只返回该站的栏目**。

        这是栏目筛选能联动的关键：栏目下拉必须跟着站点走。
        早前端端一次拿全部 27 个栏目混在一起，切站点也不变 ——
        用户选了「浙江网信网」，栏目里还挂着国信的栏目，纯属噪音。
        站点列表本身始终返回全部（切站点要用），只过滤栏目。
        """
        with self._lock:
            sites = [dict(r) for r in self.conn.execute(
            "SELECT site, site_name, COUNT(*) n, SUM(chars) c "
            "FROM articles GROUP BY site, site_name ORDER BY n DESC")]
        if site:
            cols = [dict(r) for r in self.conn.execute(
                "SELECT site, column_code, column_name, COUNT(*) n "
                "FROM articles WHERE site=? "
                "GROUP BY site, column_code, column_name ORDER BY n DESC",
                (site,))]
        else:
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
                "sites": sites, "columns": cols, "years": years,
                "filtered_site": site}

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