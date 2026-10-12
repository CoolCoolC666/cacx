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

from . import textseg

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

-- AI 批处理的失败留痕。**独立于 ai_annotations** 是刻意的：
-- 失败绝不能写进 ai_annotations，否则 get_annotation() 就会返回非空，
-- 下一轮 resume 把它当成「已完成」永久跳过 —— 一个失败会被固化成永久缺口。
-- 这里另立一表记「谁失败了、为什么、该不该重试」，供：
--   · 界面列出失败清单
--   · 服务重启后一键续跑（内存里的 job 状态会随进程消失）
CREATE TABLE IF NOT EXISTS ai_failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    task       TEXT NOT NULL,
    error      TEXT,                   -- 人话错误
    retryable  INTEGER DEFAULT 0,      -- 1=值得重试（429/超时/5xx）
    attempts   INTEGER DEFAULT 1,      -- 试了几次（含自动重试）
    batch_id   TEXT,                   -- 哪一批产生的
    created_at TEXT,
    UNIQUE (article_id, task)
);
CREATE INDEX IF NOT EXISTS idx_ai_fail_retryable ON ai_failures(retryable, created_at);

-- ============================================================ 实体 / 关系
-- 这两张表是「实体关系抽取」（作业 R1）与「关系图谱」（R2）的落地，
-- 由 AI 通过 MCP 写入，不靠人工录入。
--
-- ⚠ 设计要点：**每条实体、每条关系都必须带 article_id（证据出处）**。
--   老师明确要求演示时「点关系能回溯到原文」，没有出处的关系
--   在图谱上就是无法验证的孤证，答辩时一问就塌。
--   复合主键 (name, article_id) 让同一机构在不同文章里各自留痕，
--   同时天然去重（AI 重复提交同一条不会产生重复行）。
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,          -- 机构 / 政策 / 治理问题 / 措施
    type        TEXT NOT NULL,          -- org / policy / problem / measure
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    aliases     TEXT,                   -- JSON 数组：归并前的其他写法
    evidence    TEXT,                   -- 原文片段（回溯用）
    extractor   TEXT,                   -- 谁抽的：human / gpt / doubao…
    confidence  REAL DEFAULT 0,
    created_at  TEXT,
    UNIQUE (name, type, article_id)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_article ON entities(article_id);

CREATE TABLE IF NOT EXISTS relations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    src_name     TEXT NOT NULL,
    dst_name     TEXT NOT NULL,
    rel_type     TEXT NOT NULL,         -- co_issue / regulate / cite / belong…
    article_id   INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    evidence     TEXT,                  -- 支撑这句话的原文片段
    weight       REAL DEFAULT 1.0,      -- 共现强度（同一篇出现多次可加权）
    extractor    TEXT,
    created_at   TEXT,
    -- 同一条关系在同一篇文章里只能有一条。⚠ 不能只对
    -- (src,dst,type) 建唯一约束：那会把「甲乙联合发文」在
    -- 10 篇文章里的 10 条证据压成 1 条，恰好抹掉最该保留的共现强度。
    UNIQUE (src_name, dst_name, rel_type, article_id)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(src_name);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst_name);
CREATE INDEX IF NOT EXISTS idx_rel_article ON relations(article_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(rel_type);
"""


def _split_query(q: str) -> List[str]:
    """按显式分隔符切词（用户自己打了空格/逗号的场景）"""
    return [t for t in re.split(r'[\s,，、;；]+', (q or "").strip()) if t]


def _tokenize_for_search(q: str, tokenize: bool) -> List[str]:
    """
    查询切词：先按显式分隔符切，tokenize=True 时再把每个长串交给 jieba。

    ⚠ 为什么不做成「方法」：`_fts_quote` 是 @staticmethod，
    在里面写 `Self._xxx` / `DB._xxx` 都会在运行时炸（类名一改就崩），
    而且这里的逻辑本来就不依赖实例状态。
    """
    toks = _split_query(q)
    if not tokenize or not toks:
        return toks
    out: List[str] = []
    for t in toks:
        # 用户显式分隔出的词是「一个完整意图」，别再切碎
        if len(t) >= 2 and not _PUNCT_ONLY.search(t):
            sub = textseg.tokenize_query(t) or [t]
        else:
            sub = [t]
        out.extend(sub)
    # 去重但保序：重复词在 OR 里没意义，在 AND 里会把结果清空
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


_PUNCT_ONLY = re.compile(r'^[\s,，、;；]+$')


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
            # 写成功 = 这篇不再是失败项。放在这里而不是调用方，
            # 是为了「只要有摘要就绝不可能同时出现在失败清单里」——
            # 这条不变式比"谁负责清理"更可靠。
            self.conn.execute(
                "DELETE FROM ai_failures WHERE article_id=? AND task=?",
                (article_id, task))
            self.conn.commit()

    def get_annotation(self, article_id: int, task: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT * FROM ai_annotations WHERE article_id=? AND task=?",
            (article_id, task)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------- AI 失败留痕
    @staticmethod
    def classify_failure(error: str, retryable: bool) -> str:
        """
        把错误归成人能行动的类别。

        为什么必须做：原来的提示语是「因配置/请求问题失败（如 api_key、
        模型名），重试无用，请先改配置」—— 它把**所有**不可重试的错误
        一锅端说成配置问题。实测误伤：422 内容审查（供应商侧安全策略拦截）
        同样落在这一类，照着提示去改 api_key 纯属白折腾。

        类别与建议一一对应，不给猜测。
        """
        e = (error or "").lower()
        if "429" in e or "rate limit" in e or "速率限制" in error:
            return "rate_limit"          # 限流：等一会重试就行
        if "sensitive" in e or "422" in e or "unprocessable" in e:
            return "content_blocked"      # 内容审查：重试无用，只能人工补录
        if "401" in e or "403" in e or "unauthorized" in e or "invalid api key" in e:
            return "auth"                # 密钥/鉴权：去改配置
        if "timeout" in e or "timed out" in e or "connection" in e:
            return "network"             # 网络/超时：多半可重试
        if "model" in e and "not found" in e:
            return "bad_model"
        if "未配置" in error or "ai not configured" in e:
            return "not_configured"
        if "500" in e or "502" in e or "503" in e:
            return "server"
        return "retriable" if retryable else "other"

    #: 各类别在界面上给用户的说明（key 与 classify_failure 的返回值一一对应）
    FAILURE_HINTS = {
        "rate_limit": "供应商限流，等一会儿重试即可，不用改配置。",
        "content_blocked": "供应商内容安全策略拦截，重试无用。需要的话请人工补录摘要。",
        "auth": "密钥或鉴权有问题，请到设置页检查 api_key。",
        "network": "网络或超时问题，多半可直接重试。",
        "bad_model": "模型名不存在或无权限，请在设置页检查。",
        "not_configured": "AI 未配置，请先在设置页填好供应商、地址、模型与密钥。",
        "server": "供应商服务端错误，稍后重试。",
        "retriable": "临时故障，可重试。",
        "other": "其它错误，请查看下方原文。",
    }

    def record_ai_failure(self, article_id: int, task: str, error: str,
                          retryable: bool = False, attempts: int = 1,
                          batch_id: str = ""):
        """记一次失败。成功重试后应由 clear_ai_failure 抹掉。"""
        with self._lock:
            self.conn.execute(
                """INSERT INTO ai_failures
                   (article_id, task, error, retryable, attempts, batch_id,
                    created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(article_id, task) DO UPDATE SET
                     error=excluded.error, retryable=excluded.retryable,
                     attempts=excluded.attempts, batch_id=excluded.batch_id,
                     created_at=excluded.created_at""",
                (int(article_id), task, str(error)[:500],
                 1 if retryable else 0, int(attempts), batch_id,
                 time.strftime("%Y-%m-%d %H:%M:%S")))

    def clear_ai_failure(self, article_id: int, task: str):
        """这篇跑成功了 → 失败留痕作废（否则会一直显示在「失败清单」里，
        让人以为还没修好）。"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM ai_failures WHERE article_id=? AND task=?",
                (int(article_id), task))

    def list_ai_failures(self, task: str = "", retryable_only: bool = False,
                         limit: int = 200,
                         date_from: str = "", date_to: str = "",
                         only_orphans: bool = True) -> List[dict]:
        """
        失败清单。

        only_orphans=True（默认）时**实时对账**：只返回「仍需补跑」的项 ——
        即失败了、文章还在库、且该文章**至今没有**成功摘要。
        这么做的原因：表里的记录是**历史**，而摘要可能从任何路径补上
        （本程序重跑、批量导入、外部工具回填…）。逐次核对成本高、
        且一旦有路径绕过清理就永远显示「还没修好」，与事实相反。
        实测踩过：MiniMax 20:07 限流写了 65 条，之后豆包导入了这 65 条摘要，
        导入不走 save_annotation，失败记录就全成了僵尸（66 条里 65 条是）。
        以「有没有摘要」为准，是唯一与事实一致的判据。

        date_from/date_to：把清单口径与批量面板对齐（同一套选区），
        否则界面上的数字和「补跑」真正会跑到的篇数对不上。
        """
        sql = ("SELECT f.*, a.title, a.site, a.url, a.pub_date, a.chars,"
               "       COALESCE(a.content_empty,0) AS content_empty "
               "FROM ai_failures f "
               "JOIN articles a ON a.id = f.article_id WHERE 1=1")
        args: List[Any] = []
        if task:
            sql += " AND f.task = ?"; args.append(task)
        if only_orphans:
            sql += (" AND NOT EXISTS (SELECT 1 FROM ai_annotations n "
                    "WHERE n.article_id=f.article_id AND n.task=f.task)")
        if date_from:
            sql += " AND a.pub_date >= ?"; args.append(date_from)
        if date_to:
            sql += " AND a.pub_date <= ?"; args.append(date_to)
        sql += " AND COALESCE(a.content_empty,0)=0"
        if retryable_only:
            sql += " AND f.retryable = 1"
        sql += " ORDER BY f.created_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args)]

    def ai_failure_stats(self, task: str = "summarize",
                         date_from: str = "", date_to: str = "") -> dict:
        """
        失败清单的分类统计。口径与 list_ai_failures 对齐，
        界面上每个数字都能对上「点下去会发生什么」。
        """
        wheres = ["f.task=?"]
        args: List[Any] = [task]
        if date_from:
            wheres.append("a.pub_date >= ?"); args.append(date_from)
        if date_to:
            wheres.append("a.pub_date <= ?"); args.append(date_to)
        W = " AND ".join(wheres)
        JOIN = ("FROM ai_failures f JOIN articles a ON a.id=f.article_id "
                f"WHERE {W} AND COALESCE(a.content_empty,0)=0")
        ORPHAN = (f" AND NOT EXISTS (SELECT 1 FROM ai_annotations n "
                  f"WHERE n.article_id=f.article_id AND n.task=f.task)")

        def one(sql):
            return self.conn.execute(sql, list(args)).fetchone()[0]

        pending = one(f"SELECT COUNT(*) {JOIN}{ORPHAN}")
        retryable = one(f"SELECT COUNT(*) {JOIN}{ORPHAN} AND f.retryable=1")
        # 已过时 = 表里有、但摘要已存在。清空按钮的计数以它为准。
        stale = one(f"""SELECT COUNT(*) FROM ai_failures f
                    JOIN ai_annotations n ON n.article_id=f.article_id
                                         AND n.task=f.task
                    WHERE {W}""")
        total_rows = one(f"SELECT COUNT(*) {JOIN}")
        # 分类在 Python 侧算：不为此给表加列（SQLite 加列要迁移，
        # 而分类规则会随供应商变化而调整，改代码比改表便宜）。
        kinds: Dict[str, int] = {}
        for r in self.conn.execute(
                f"SELECT f.error, f.retryable {JOIN}{ORPHAN}", list(args)):
            k = self.classify_failure(r[0], bool(r[1]))
            kinds[k] = kinds.get(k, 0) + 1
        return {"pending": pending, "retryable": retryable,
                "stale": stale, "total_rows": total_rows, "kinds": kinds}

    def clear_ai_failures(self, task: str = "", only_stale: bool = True,
                          article_ids: Optional[List[int]] = None,
                          date_from: str = "", date_to: str = "") -> int:
        """
        清空失败清单。返回实际删除条数（逐项回报，不静默）。

        only_stale=True（默认）只清「已经有摘要」的那些 —— 安全的默认档。
        only_stale=False 清全部（含真正还没补跑的），需要调用方二次确认。
        article_ids 非空时只清这几篇（精确到篇）。
        """
        sql = ["DELETE FROM ai_failures WHERE 1=1"]
        args: List[Any] = []
        if task:
            sql.append("AND task=?"); args.append(task)
        if only_stale:
            sql.append("AND EXISTS (SELECT 1 FROM ai_annotations n "
                       "WHERE n.article_id=ai_failures.article_id "
                       "AND n.task=ai_failures.task)")
        if article_ids:
            ids = [int(i) for i in article_ids]
            sql.append(f"AND article_id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
        if date_from or date_to:
            sql.append("AND article_id IN (SELECT id FROM articles WHERE 1=1")
            if date_from:
                sql.append("AND pub_date >= ?"); args.append(date_from)
            if date_to:
                sql.append("AND pub_date <= ?"); args.append(date_to)
            sql.append(")")
        with self._lock:
            cur = self.conn.execute(" ".join(sql), args)
            self.conn.commit()
            return cur.rowcount

    def manual_annotation(self, article_id: int, task: str,
                          result: Dict[str, Any],
                          provider: str = "manual",
                          model: str = "") -> int:
        """
        人工补录 AI 产物（供应商内容审查拦下、或用户自己写了摘要的情形）。

        为什么不复用 save_annotation：那条路径会自动盖掉 provider/model 与
        时间戳，而这个入口必须**如实**记下「这是人工补的」，否则数据面板上
        分不清哪些摘要来自模型、哪些来自人。
        """
        with self._lock:
            self.conn.execute(
                """INSERT INTO ai_annotations
                   (article_id,task,provider,model,result,created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(article_id,task) DO UPDATE SET
                     result=excluded.result, provider=excluded.provider,
                     model=excluded.model, created_at=excluded.created_at""",
                (int(article_id), task, provider, model,
                 result if isinstance(result, str)
                 else json.dumps(result, ensure_ascii=False),
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.execute(
                "DELETE FROM ai_failures WHERE article_id=? AND task=?",
                (int(article_id), task))
            self.conn.commit()
            return 1

    # ---------------------------------------------------------- 检索
    @staticmethod
    def _fts_quote(q: str, tokenize: bool = False,
                   op: str = "AND") -> str:
        """
        把用户输入转成安全的 FTS5 查询串。

        tokenize=False（默认）：按显式分隔符切，词间 AND —— 保持既有行为。
        tokenize=True：中文按 jieba 切词后 OR。
            为什么需要这条：中文没有空格，FTS5 默认把整串当一个词，
            「清朗网络谣言」整串进去基本搜不到。切成 词组 后 OR 召回完全不同。
            但**不能默认开**：OR 会把结果集放大好几倍，
            精确查「中央网信办 专项行动」时反而捞出无关文章。
            所以做成显式开关，由调用方（AI / 前端「智能分词」勾选）决定。
        """
        toks = _tokenize_for_search(q, tokenize)
        if not toks:
            return ""
        joiner = " OR " if op.upper() == "OR" else " AND "
        return joiner.join('"' + t.replace('"', '""') + '"' for t in toks)

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
               only_empty: bool = False,
               tokenize: bool = False,
               op: str = "AND") -> Dict[str, Any]:
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
                only_empty, tokenize, op)

    def _search_locked(self, q, site, column_code, column_name, year,
                       date_from, date_to, min_chars, skip_empty, sort,
                       limit, offset, min_fts_chars,
                       only_empty=False, tokenize=False,
                       op="AND") -> Dict[str, Any]:
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
            toks = _tokenize_for_search(q, tokenize)
            if not toks:
                toks = [q]
            use_fts = all(len(t) >= min_fts_chars for t in toks)
            if only_empty:
                # 占位条目没有正文 → 不在 FTS 索引里，走 FTS 必然 0 结果。
                # 强制退回标题 LIKE，否则用户在占位视图里搜什么都是空的，
                # 会误以为是功能坏了。
                use_fts = False
            if use_fts:
                join = ("JOIN articles_fts f ON f.rowid = a.id "
                        "AND articles_fts MATCH ?")
                args.insert(0, self._fts_quote(q, tokenize, op))
                mode = "fts"
            else:
                mode = "like"
                clauses, cargs = [], []
                for t in toks:
                    like = f"%{t}%"
                    if only_empty:
                        clauses.append("a.title LIKE ?")
                        cargs.append(like)
                    else:
                        clauses.append("(a.title LIKE ? OR a.content_text LIKE ?)")
                        cargs += [like, like]
                if not clauses:
                    pass
                elif op.upper() == "OR":
                    # ⚠ 分词 OR 时**不能**每个词各加一条 WHERE ——
                    #    多条 WHERE 是 AND，OR 语义就没了，结果反而更少。
                    #    必须合成一条 (... OR ...)。
                    where.append("(" + " OR ".join(clauses) + ")")
                    args += cargs
                else:
                    where += clauses
                    args += cargs

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

    #: 只有绑定了 article 的行为才能按站点归属。
    #: search / export 不在其中 —— 检索天然跨站，导出也可能一次导了两站。
    SITE_SCOPED_EVENTS = ("open", "summarize")

    def event_stats(self, since_days: int = 30,
                    site: str = "") -> Dict[str, Any]:
        since = time.strftime("%Y-%m-%d",
                              time.localtime(time.time() - since_days * 86400))
        args: List[Any] = [since]
        if site:
            # 按站点过滤时，只看能归属到站点的那几类。
            # 不把 search/export 混进来报 —— 半口径的数字比没有更误导：
            # 界面上会出现「浙江的 search 是 0 次」，而事实是「不可归属」。
            ph = ",".join("?" * len(self.SITE_SCOPED_EVENTS))
            where = (f" AND e.kind IN ({ph}) "
                     "AND e.article_id IN (SELECT id FROM articles WHERE site=?)")
            args += list(self.SITE_SCOPED_EVENTS) + [site]
        else:
            where = ""
        with self._lock:
            base = "FROM events e WHERE e.created_at>=?" + where
            total = self.conn.execute(
                f"SELECT COUNT(*) {base}", list(args)).fetchone()[0]
            by_kind = [dict(r) for r in self.conn.execute(
                f"SELECT e.kind, COUNT(*) n {base} "
                "GROUP BY e.kind ORDER BY n DESC", list(args))]
            # 「跳原文最多」需要 join articles 拿标题，因此 JOIN 要写在 base 里。
            # 先把 base 拼成带 JOIN 的版本，再补 kind='open' 与 LIMIT。
            base_j = base.replace(
                "FROM events e", "FROM events e LEFT JOIN articles a ON a.id=e.article_id")
            top = [dict(r) for r in self.conn.execute(
                f"SELECT e.article_id, a.title, a.site, a.site_name, COUNT(*) n "
                f"{base_j} AND e.kind='open' GROUP BY e.article_id "
                "ORDER BY n DESC LIMIT 10", list(args))]
        return {"since": since, "total": total, "by_kind": by_kind,
                "top_open": top, "site": site,
                # 前端据此说明「哪些类别在按站点过滤下不计入」
                "site_applicable": list(self.SITE_SCOPED_EVENTS) if site
                else None,
                "note": ("按站点过滤时只统计 open / summarize；"
                         "search 与 export 跨站，不可归属。") if site else ""}

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

    # ---------------------------------------------------------- 分析样本
    #: 抽样策略。
    #:  ⚠ 别用「取最近 N 篇」—— 那是**按时间截断**，只覆盖末段。
    #:    实测 cac·网信要闻 共 741 篇，取最近 500 篇只覆盖到 2025-03，
    #:    2024 年整年丢失。而老师明确要求「发文量与主题随时间的变化」，
    #:    时间轴只剩一年，这条要求直接作废。
    #:  · stratified 按年份分层，每年等额 —— 保证每个年份都有样本，
    #:    时间轴才画得出趋势（默认）
    #:  · recent    取最近 N 篇 —— 只想看最新动态时用
    SAMPLE_STRATEGIES = ("stratified", "recent")

    def sample_articles(self, site: str = "", column: str = "",
                        date_from: str = "2024-01-01",
                        date_to: str = "2099-12-31",
                        limit: int = 500,
                        strategy: str = "stratified") -> Dict[str, Any]:
        """
        抽一个**分析样本**出来：给栏目、时间窗、篇数上限，其余自动。

        老师要「任选一个栏目，300-500 篇，2024 年以来」，
        但库里最多的栏目有 1600 多篇、最少的只有 14 篇——
        光靠「选栏目」凑不出 300-500。所以规则是：
          **时间窗给足（默认 2024 至今），篇数用 limit 截，抽样用分层。**
        这样任何一个栏目都能配出合规样本，选择权才真正在小组手里。
        """
        if strategy not in self.SAMPLE_STRATEGIES:
            raise ValueError(
                f"未知抽样策略 {strategy}，可选：{self.SAMPLE_STRATEGIES}")
        limit = max(1, min(int(limit), 5000))
        where, args = ["pub_day>=?", "pub_day<=?"], [date_from, date_to]
        if site:
            where.append("site=?"); args.append(site)
        if column:
            where.append("column_name=?"); args.append(column)
        w = " WHERE " + " AND ".join(where)

        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM articles{w}", args).fetchone()[0]
            years = [dict(r) for r in self.conn.execute(
                f"SELECT substr(pub_day,1,4) y, COUNT(*) n FROM articles{w} "
                f"GROUP BY y ORDER BY y", args)]
            if strategy == "recent":
                rows = [dict(r) for r in self.conn.execute(
                    f"SELECT id,site,column_name,title,pub_day,url,chars,"
                    f"COALESCE(content_empty,0) ce FROM articles{w} "
                    f"ORDER BY pub_day DESC, id DESC LIMIT ?", args + [limit])]
                picked_by_year: Dict[str, int] = {}
            else:
                # 按年份分层：每年先取 floor(limit/年数)，
                # 余额再按年份轮流补 —— 用 OFFSET 翻页，每一年各自推进，
                # 天然保证「每年都被覆盖到」，不会出现某年被跳过的坑。
                ny = max(1, len(years))
                per = max(1, limit // ny)
                rows, picked_by_year = [], {}
                COLS = ("id,site,column_name,title,pub_day,url,chars,"
                        "COALESCE(content_empty,0) ce")
                for y in years:
                    yr = str(y["y"])
                    offset = 0
                    want = min(per, y["n"])
                    while offset < want:
                        batch = self.conn.execute(
                            f"SELECT {COLS} FROM articles{w} "
                            f"AND substr(pub_day,1,4)=? "
                            f"ORDER BY pub_day DESC, id DESC LIMIT ? OFFSET ?",
                            args + [yr, want - offset, offset]).fetchall()
                        rows += [dict(r) for r in batch]
                        offset += len(batch)
                        if not batch:
                            break
                    picked_by_year[yr] = offset
                # 配额用不完就继续补，逐年轮流
                left = limit - len(rows)
                while left > 0:
                    grew = 0
                    for y in years:
                        if left <= 0:
                            break
                        yr = str(y["y"])
                        if picked_by_year.get(yr, 0) >= y["n"]:
                            continue
                        batch = self.conn.execute(
                            f"SELECT {COLS} FROM articles{w} "
                            f"AND substr(pub_day,1,4)=? "
                            f"ORDER BY pub_day DESC, id DESC LIMIT 1 OFFSET ?",
                            args + [yr, picked_by_year.get(yr, 0)]).fetchall()
                        if not batch:
                            continue
                        rows += [dict(r) for r in batch]
                        picked_by_year[yr] = picked_by_year.get(yr, 0) + 1
                        left -= len(batch)
                        grew += len(batch)
                    if not grew:
                        break

        rows.sort(key=lambda r: (r["pub_day"], r["id"]))
        usable = [r for r in rows if not r["ce"]]
        return {
            "n": len(rows), "n_usable": len(usable),
            "total_pool": total, "limit": limit, "strategy": strategy,
            "site": site, "column": column,
            "date_from": date_from, "date_to": date_to,
            "years_pool": years, "years_taken": picked_by_year,
            "date_min": min((r["pub_day"] for r in rows), default=""),
            "date_max": max((r["pub_day"] for r in rows), default=""),
            "items": rows,
        }

    def sample_options(self, date_from: str = "2024-01-01",
                       date_to: str = "2099-12-31") -> List[dict]:
        """有哪些栏目可以配样本，各有多少篇（给前端下拉 / AI 选参数用）"""
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT site, column_name, COUNT(*) n, "
                "COUNT(DISTINCT substr(pub_day,1,4)) years, "
                "MIN(pub_day) d0, MAX(pub_day) d1 "
                "FROM articles WHERE pub_day>=? AND pub_day<=? "
                "GROUP BY site, column_name ORDER BY n DESC",
                (date_from, date_to))]

    def column_years(self, site: str = "", column: str = "",
                     date_from: str = "2024-01-01",
                     date_to: str = "2099-12-31") -> List[dict]:
        """某栏目逐年篇数 —— 抽样的依据，也是时间轴的基础"""
        where, args = ["pub_day>=?", "pub_day<=?"], [date_from, date_to]
        if site:
            where.append("site=?"); args.append(site)
        if column:
            where.append("column_name=?"); args.append(column)
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                f"SELECT substr(pub_day,1,4) y, COUNT(*) n "
                f"FROM articles WHERE {' AND '.join(where)} "
                f"GROUP BY y ORDER BY y", args)]

    # ------------------------------------------------- 实体 / 关系（写入）
    ENTITY_TYPES = ("org", "policy", "problem", "measure")

    def upsert_entities(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        AI 批量写入实体。rows 每项：
            {name, type, article_id, evidence?, aliases?, extractor?, confidence?}

        返回 {written, skipped, errors:[...]}。
        重复提交同一篇的同一实体走 ON CONFLICT 更新证据，不产生重复行 ——
        AI 重跑一次不会把图谱撑成双倍。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        written = skipped = 0
        errors: List[str] = []
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                for r in rows:
                    name = (r.get("name") or "").strip()
                    etype = (r.get("type") or "").strip()
                    aid = r.get("article_id")
                    if not name or not aid:
                        skipped += 1
                        errors.append(f"缺 name 或 article_id：{r!r}")
                        continue
                    if etype not in self.ENTITY_TYPES:
                        skipped += 1
                        errors.append(
                            f"未知实体类型 {etype!r}，可选 {self.ENTITY_TYPES}")
                        continue
                    # article_id 必须真实存在，否则外键会炸；
                    # 先验一次，把报错变成可读提示
                    ok = self.conn.execute(
                        "SELECT 1 FROM articles WHERE id=?",
                        (int(aid),)).fetchone()
                    if not ok:
                        skipped += 1
                        errors.append(f"文章 {aid} 不存在")
                        continue
                    al = r.get("aliases") or []
                    if isinstance(al, str):
                        try:
                            al = json.loads(al)
                        except Exception:
                            al = [al]
                    self.conn.execute(
                        "INSERT INTO entities"
                        "(name,type,article_id,aliases,evidence,extractor,"
                        " confidence,created_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(name,type,article_id) DO UPDATE SET "
                        "evidence=excluded.evidence, aliases=excluded.aliases, "
                        "extractor=excluded.extractor, "
                        "confidence=excluded.confidence",
                        (name, etype, int(aid), json.dumps(al, ensure_ascii=False),
                         r.get("evidence") or "", r.get("extractor") or "ai",
                         float(r.get("confidence") or 0), now))
                    written += 1
                self.conn.commit()
            except Exception as e:
                self.conn.rollback()
                raise
        return {"written": written, "skipped": skipped,
                "errors": errors[:20], "error_count": len(errors)}

    def upsert_relations(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        AI 批量写入关系。rows 每项：
            {src_name, dst_name, rel_type, article_id, evidence?, weight?}

        ⚠ weight 累加而不是覆盖：同一对机构在 10 篇文章里联合发文，
        权重就该是 10 —— 合作网络的强弱正是这么来的。
        但只对**同一 article_id** 去重，跨文章的证据全部保留。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        written = skipped = 0
        errors: List[str] = []
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                for r in rows:
                    s = (r.get("src_name") or "").strip()
                    t = (r.get("dst_name") or "").strip()
                    rt = (r.get("rel_type") or "").strip()
                    aid = r.get("article_id")
                    if not s or not t or not rt or not aid:
                        skipped += 1
                        errors.append(f"缺字段：{r!r}")
                        continue
                    if s == t:
                        skipped += 1
                        errors.append(f"自环关系（{s}→{s}），跳过")
                        continue
                    ok = self.conn.execute(
                        "SELECT 1 FROM articles WHERE id=?",
                        (int(aid),)).fetchone()
                    if not ok:
                        skipped += 1
                        errors.append(f"文章 {aid} 不存在")
                        continue
                    self.conn.execute(
                        "INSERT INTO relations"
                        "(src_name,dst_name,rel_type,article_id,evidence,weight,"
                        " extractor,created_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(src_name,dst_name,rel_type,article_id) "
                        "DO UPDATE SET evidence=excluded.evidence, "
                        "weight=relations.weight+excluded.weight",
                        (s, t, rt, int(aid), r.get("evidence") or "",
                         float(r.get("weight") or 1.0),
                         r.get("extractor") or "ai", now))
                    written += 1
                self.conn.commit()
            except Exception as e:
                self.conn.rollback()
                raise
        return {"written": written, "skipped": skipped,
                "errors": errors[:20], "error_count": len(errors)}

    # ------------------------------------------------- 实体 / 关系（读取）
    def list_entities(self, type: str = "", q: str = "", article_id: Optional[int] = None,
                      limit: int = 200) -> List[dict]:
        """实体清单。type 为空=全部；q 对 name 做模糊匹配"""
        where, args = [], []
        if type:
            where.append("type=?"); args.append(type)
        if q:
            where.append("name LIKE ?"); args.append(f"%{q}%")
        if article_id:
            where.append("article_id=?"); args.append(int(article_id))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                f"SELECT e.id,e.name,e.type,e.article_id,e.aliases,e.evidence,"
                f"e.extractor,e.confidence,a.title AS article_title,"
                f"a.pub_day,a.url "
                f"FROM entities e LEFT JOIN articles a ON a.id=e.article_id"
                f"{w} ORDER BY e.type, e.name LIMIT ?",
                args + [int(limit)])]

    def list_relations(self, name: str = "", rel_type: str = "",
                       article_id: Optional[int] = None,
                       limit: int = 200) -> List[dict]:
        """关系清单。name 非空时只看与该实体相连的关系（图谱点开一个点用）"""
        where, args = [], []
        if name:
            where.append("(r.src_name=? OR r.dst_name=?)")
            args += [name, name]
        if rel_type:
            where.append("r.rel_type=?"); args.append(rel_type)
        if article_id:
            where.append("r.article_id=?"); args.append(int(article_id))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                f"SELECT r.*, a.title AS article_title, a.pub_day, a.url "
                f"FROM relations r LEFT JOIN articles a ON a.id=r.article_id"
                f"{w} ORDER BY r.weight DESC LIMIT ?",
                args + [int(limit)])]

    def graph(self, limit: int = 300) -> Dict[str, Any]:
        """
        关系图谱：节点 + 边。边按「跨多少篇文章」聚合 —— 这就是权重。

        ⚠ 刻意把 article_ids 全部带出来：答辩时要能「点一条关系
        回溯原文」，前端拿到的是文章 id 列表，直接跳原文即可。
        """
        with self._lock:
            nodes = [dict(r) for r in self.conn.execute(
                "SELECT type, name, COUNT(*) n FROM entities "
                "GROUP BY type, name ORDER BY n DESC LIMIT ?", (int(limit),))]
            edges = [dict(r) for r in self.conn.execute(
                "SELECT src_name, dst_name, rel_type, COUNT(*) n, "
                "GROUP_CONCAT(article_id) article_ids, "
                "SUM(weight) weight "
                "FROM relations GROUP BY src_name,dst_name,rel_type "
                "ORDER BY n DESC LIMIT ?", (int(limit),))]
        for e in edges:
            e["article_ids"] = [int(x) for x in (e.get("article_ids") or "").split(",") if x]
        return {"nodes": nodes, "edges": edges}

    def graph_stats(self) -> Dict[str, Any]:
        """图谱概览：节点数、边数、类型分布、证据覆盖率"""
        with self._lock:
            def one(sql, args=()):
                return self.conn.execute(sql, args).fetchone()[0]
            return {
                "entity_total": one("SELECT COUNT(*) FROM entities"),
                "entity_orgs": one("SELECT COUNT(DISTINCT name) FROM entities WHERE type='org'"),
                "relation_total": one("SELECT COUNT(*) FROM relations"),
                "rel_pairs": one("SELECT COUNT(*) FROM (SELECT 1 FROM relations "
                                 "GROUP BY src_name,dst_name,rel_type)"),
                "by_type": [dict(r) for r in self.conn.execute(
                    "SELECT type, COUNT(DISTINCT name) n FROM entities "
                    "GROUP BY type ORDER BY n DESC")],
                "by_rel": [dict(r) for r in self.conn.execute(
                    "SELECT rel_type, COUNT(*) n, COUNT(DISTINCT src_name||'→'||dst_name) pairs "
                    "FROM relations GROUP BY rel_type ORDER BY n DESC")],
                # 证据覆盖：没有 article_id 的关系等于无法验证，答辩时一问就塌
                "no_evidence": one("SELECT COUNT(*) FROM relations "
                                   "WHERE evidence IS NULL OR evidence=''"),
            }


def open_db(cfg: Dict[str, Any]) -> Database:
    """按配置打开数据库（相对路径按项目根解析）"""
    from . import config as cfgmod
    p = cfg.get("db", {}).get("path", "data/cacx.db")
    path = Path(p)
    if not path.is_absolute():
        path = cfgmod.PROJECT_ROOT / path
    return Database(path)