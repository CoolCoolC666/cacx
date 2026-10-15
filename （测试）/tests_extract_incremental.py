# -*- coding: utf-8 -*-
"""
抽取「全量 / 增量」两档 + 抽取源查询优化 测试
================================================================
背景：数据面板的「手动导入」= 样本配置器的一键抽取（POST /api/extract/run）。
用户把篇数拉到全量（28900 篇）后反馈卡顿，并要求「之后支持增量」。
用真库只读（mode=ro）把这条路径逐步拆开计时（2026-10-15）：
    ① 抽样 sample_articles      7.30s   ← 配额补足是「每补一篇发一条 SQL」
    ② 取正文 extract_sources    0.32s   ← 单条 `id IN (28900 个 ?)`
    ③ 抽取 extract_batch      175.52s   ← **真瓶颈**（6.07 ms/篇，与篇数线性）
    ④ 写入 upsert               另测
本文件钉住这次改动的三件事，都是「错了也不报错、只是数字悄悄变」的那类：

  ① **IN 分批**：一条 `id IN (N 个 ?)` 的变量数就是篇数。SQLite 的
     SQLITE_MAX_VARIABLE_NUMBER 是 32766（3.32 以前只有 999），
     而界面允许的篇数上限是 **39999** —— 配额一开到上限，抽取在取正文
     这一步就当场 `too many SQL variables`。分批到 900 一并留出老版本余量。
  ② **增量档**：只处理「尚无抽取结果」的文章。⚠ 它只过滤**清单**，
     不改抽样口径（命中范围/策略/配额一律照旧）。
  ③ **抽样配额分配**重写成「纯整数算 + 每年一条 SELECT」后，取回的 id
     必须与旧版**逐位相同** —— sample_articles 是报告里所有分母的来源，
     改快一点可以，改出不同的样本不行。（旧实现照抄在本文件里当参照物。）

跑法：py -3 "（测试）\tests_extract_incremental.py"
"""
import os
import shutil
import sys
import sys as _sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient          # noqa: E402

from cacx import db as D                            # noqa: E402
from cacx.web import app as A                       # noqa: E402

# ⚠ `from cacx.web import app as A` 拿到的是模块还是 FastAPI 实例要看导入顺序，
#   同名属性会遮住同名子模块 —— patch 必须走 sys.modules 取真模块。
MOD = _sys.modules["cacx.web.app"]

PAD = ("相关内容将依据有关法律法规逐步落实，各地区各部门应当加强协同联动，"
       "及时回应社会关切，持续完善相关工作机制，确保各项治理措施落到实处见到成效。")


def _body(i):
    return (f"中央网信办、公安部联合印发《关于加强网络数据安全治理的通知》第{i}号。"
            "通知要求各地网信部门会同公安机关开展网络安全专项检查，"
            "针对网络谣言、网络诈骗、未成年人沉迷等突出问题依法查处，"
            "各省级网信办要结合本地实际制定实施方案，"
            "市场监管总局、教育部将联合开展监督检查并对违规App下架处理。" + PAD)


ART_COLS = ("id,site,column_name,title,url,pub_day,year,chars,"
            "content_empty,content_text")


def _add_articles(d, n=6, year="2024"):
    """灌 n 篇正文足够长的文章（> extract.MIN_CHARS=200），返回 id 列表。

    ⚠ 正文必须真超过 MIN_CHARS，否则会被抽取引擎正确跳过 ——
      一旦被跳过就没有实体/关系，增量断言全都会因为「没东西可跳」而失真。
    """
    rows, ids = [], []
    for i in range(1, n + 1):
        b = _body(i)
        rows.append((i, "cac", "网信要闻",
                     f"中央网信办部署网络治理专项行动第{i}篇",
                     f"http://x/{i}", f"{year}-0{i % 9 + 1}-05", year,
                     len(b), 0, b))
        ids.append(i)
    ph = ",".join("?" * len(ART_COLS.split(",")))
    with d._lock:
        d.conn.executemany(
            f"INSERT INTO articles({ART_COLS}) VALUES({ph})", rows)
        d.conn.commit()
    return ids


def _tmpdb(prefix="cacx_incr_"):
    """临时库。⚠ 用 mkdtemp 而不是 TemporaryDirectory：
    Windows 上后者退出时会因连接未关而 PermissionError。"""
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    d = D.open_db({"db": {"path": str(tmp / "t.db")}})
    real = (Path(__file__).resolve().parent.parent
            / "data" / "cacx.db").resolve()
    got = Path(d.conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    assert got != real, f"测试误开了真库：{got}"
    return d, tmp


class TestIncrementalFilter(unittest.TestCase):
    """增量差集：只留「尚无抽取结果」的文章。直接打 db 层。"""

    @classmethod
    def setUpClass(cls):
        cls.d, cls.tmp = _tmpdb()
        cls.ids = _add_articles(cls.d, 6)
        # 已知状态：1 有实体（无关系）、2 有关系（无实体）、3/4/5/6 两边都空
        with cls.d._lock:
            cls.d.conn.executemany(
                "INSERT INTO entities(name,type,article_id,evidence,extractor)"
                " VALUES(?,?,?,?,?)",
                [("中央网信办", "org", 1, "证据片段", "rule"),
                 ("公安部", "org", 1, "证据片段", "rule")])
            cls.d.conn.executemany(
                "INSERT INTO relations(src_name,dst_name,rel_type,article_id,"
                "evidence,weight,extractor) VALUES(?,?,?,?,?,?,?)",
                [("中央网信办", "公安部", "joint_issue", 2, "证据片段", 3.0,
                  "rule")])
            cls.d.conn.commit()
        cls.kw = dict(date_from="2000-01-01", date_to="2099-12-31", limit=100)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_全量档把命中全取出来(self):
        sc = self.d.extract_scope(**self.kw)
        self.assertEqual(sc["pool_hit"], 6)
        self.assertEqual(sc["skipped_existing"], 0, "全量档不该跳过任何篇")
        self.assertEqual([a["id"] for a in sc["articles"]], self.ids)

    def test_增量档只留没有抽取结果的(self):
        sc = self.d.extract_scope(only_unextracted=True, **self.kw)
        self.assertEqual(sc["pool_hit"], 6, "命中数不因档位变化 —— "
                                          "命中口径必须与全量一致")
        self.assertEqual([a["id"] for a in sc["articles"]], [3, 4, 5, 6])
        self.assertEqual(sc["skipped_existing"], 2)

    def test_只有实体或只有关系都算已有结果(self):
        """判据是「entities **或** relations 里出现过」。
        只查一张表就会漏掉另一张表里留下的那批 —— 实测真库里有
        relations 但没有 entities 的文章 0 篇，可 u 型只要出现一次就会错。"""
        sc = self.d.extract_scope(only_unextracted=True, **self.kw)
        got = {a["id"] for a in sc["articles"]}
        self.assertNotIn(1, got, "有实体没关系的篇也该跳过")
        self.assertNotIn(2, got, "有关系没实体的篇也该跳过")

    def test_抽取结果为空的文章增量下仍会重跑(self):
        """⚠ 这是增量的**固有边界**，不是缺陷：articles 表没有
        「抽过没有」的标记列，只能反查 entities/relations。
        一篇抽过但一个实体一个关系都没抽出来的文章两边都没记录，
        于是每次增量都会再抽一遍。所以增量省的是「已有结果」的那些篇，
        不等于「跑过一次以后就永不再跑」。"""
        sc = self.d.extract_scope(only_unextracted=True, **self.kw)
        self.assertIn(3, [a["id"] for a in sc["articles"]],
                      "无结果的文章必须能被重跑，否则永远补不上")

    def test_增量不改抽样口径(self):
        full = self.d.extract_scope(**self.kw)
        inc = self.d.extract_scope(only_unextracted=True, **self.kw)
        self.assertEqual(inc["pool_hit"], full["pool_hit"])
        self.assertTrue({a["id"] for a in inc["articles"]}
                        <= {a["id"] for a in full["articles"]},
                        "增量只能是全量的子集，不能改口径扩出别的文章")

    def test_extract_sources签名仍是清单(self):
        """extract_sources 在对接文档里是「返回原文清单」的公开签名，
        增量档不能把它改成 dict（计数另走 extract_scope）。"""
        out = self.d.extract_sources(only_unextracted=True, **self.kw)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 4)
        self.assertEqual(sorted(out[0]), ["content_text", "id", "title", "url"])


class TestBatchFetch(unittest.TestCase):
    """IN 分批：单条 SQL 的变量数就是篇数，不能随配额无限涨。"""

    @classmethod
    def setUpClass(cls):
        cls.d, cls.tmp = _tmpdb("cacx_incr_batch_")
        rows = [(i, "cac", "网信要闻", f"t{i}", f"http://x/{i}",
                 "2024-01-05", "2024", 10, 1, "短") for i in range(1, 2501)]
        ph = ",".join("?" * len(ART_COLS.split(",")))
        with cls.d._lock:
            cls.d.conn.executemany(
                f"INSERT INTO articles({ART_COLS}) VALUES({ph})", rows)
            cls.d.conn.commit()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_每批不超过900(self):
        chunks = self.d._id_batches(list(range(1, 2501)))
        self.assertTrue(chunks)
        self.assertTrue(all(len(c) <= 900 for c in chunks),
                        f"有批超过 900：{[len(c) for c in chunks]}")
        self.assertEqual([len(c) for c in chunks], [900, 900, 700])

    def test_分批阈值留了老SQLite的余量(self):
        """3.32 以前的 SQLite 上限是 999，取 900 是为了兼容。"""
        self.assertLessEqual(D.Database._IN_BATCH, 999)

    def test_分批取正文_全部取回且按id升序(self):
        got = self.d._fetch_sources(list(range(1, 2501)))
        self.assertEqual([g["id"] for g in got], list(range(1, 2501)),
                         "分批后必须仍然取全、且顺序与单条大 SQL 一致")
        self.assertEqual(got[0]["url"], "http://x/1")

    def test_空清单不炸(self):
        self.assertEqual(self.d._fetch_sources([]), [])

    def test_差集查询也不撞变量上限(self):
        """增量差集同样要分批：ids 有 2500 个，一条 SQL 会撞上限。"""
        self.assertEqual(self.d._drop_extracted(list(range(1, 2501))),
                         list(range(1, 2501)))


class TestExtractRunApi(unittest.TestCase):
    """接口层：mode 两档、scope 报账字段、全量行为不变。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cacx_incr_api_")
        cls.orig_db = MOD._db
        cls.store = D.open_db({"db": {"path": os.path.join(cls.tmp, "t.db")}})
        real = (Path(__file__).resolve().parent.parent
                / "data" / "cacx.db").resolve()
        got = Path(cls.store.conn.execute(
            "PRAGMA database_list").fetchone()[2]).resolve()
        assert got != real, f"测试误开了真库：{got}"
        _add_articles(cls.store, 6)
        MOD._db = lambda: cls.store
        cls.client = TestClient(MOD.app)

    @classmethod
    def tearDownClass(cls):
        MOD._db = cls.orig_db
        try:
            cls.store.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # ⚠ 每个测试前清库：unittest 按字母序跑，上一个测试写的实体
        #   会让本测试的增量判定全部失真
        with self.store._lock:
            self.store.conn.execute("DELETE FROM relations")
            self.store.conn.execute("DELETE FROM entities")
            self.store.conn.commit()

    def _post(self, **kw):
        body = {"site": "cac", "column": "网信要闻", "limit": 10,
                "strategy": "stratified", "write": False}
        body.update(kw)
        r = self.client.post("/api/extract/run", json=body)
        self.assertEqual(r.status_code, 200, r.text[:400])
        return r.json()

    def _have_results(self):
        return self.store.conn.execute(
            "SELECT COUNT(DISTINCT article_id) FROM ("
            " SELECT article_id FROM entities UNION"
            " SELECT article_id FROM relations)").fetchone()[0]

    def test_默认档是全量_报账字段自洽(self):
        d = self._post()
        sc = d["scope"]
        self.assertEqual(sc["mode"], "full", "不传 mode 必须落在全量档")
        self.assertEqual(sc["skipped"], 0, "全量档 skipped 恒为 0")
        self.assertEqual(sc["articles_found"], 6, "articles_found = 口径命中篇数")
        self.assertEqual(sc["processed"], 6, "全量档 processed = 命中篇数")
        self.assertEqual(d["processed"], 6)
        self.assertEqual(d["skipped_existing"], 0)
        self.assertEqual(sc["processed"],
                         sc["articles_found"] - sc["skipped"],
                         "processed 必须等于 articles_found - skipped")

    def test_全量档行为与改前一致(self):
        """加 mode 不能悄悄改掉全量档的响应形状（前端与别的测试都在读）。"""
        d = self._post()
        self.assertNotIn("error", d)
        for k in ("articles", "elapsed", "scope", "written", "hint",
                  "types", "rel_types", "skipped", "skipped_reasons"):
            self.assertIn(k, d, f"全量档响应少了 {k}")
        self.assertNotIn("entities", d, "预览不该把实体塞进响应")
        self.assertNotIn("relations", d)
        self.assertIsNone(d["written"])
        self.assertEqual(d["articles"], 6)

    def test_顶层skipped仍是正文不足的意思(self):
        """⚠ 命名冲突：extract_batch 早就用 `skipped` 表示「正文不足被跳过」。
        增量跳过篇数**不能**占用顶层 `skipped` —— 覆盖会静默改掉旧含义。
        所以它落在 scope.skipped 与顶层 skipped_existing。"""
        self._post(write=True)
        d = self._post(mode="incremental")
        self.assertGreater(d["scope"]["skipped"], 0)
        self.assertEqual(d["skipped"], 0, "顶层 skipped 是「正文不足」，"
                                         "这里 6 篇正文都够长，应当是 0")
        self.assertEqual(d["skipped_existing"], d["scope"]["skipped"])

    def test_增量档在空库上等价于全量(self):
        d = self._post(mode="incremental")
        self.assertEqual(d["scope"]["mode"], "incremental")
        self.assertEqual(d["scope"]["skipped"], 0, "还没有任何抽取结果，没得跳")
        self.assertEqual(d["scope"]["processed"], 6)

    def test_增量档跳过已有结果的篇(self):
        self._post(write=True)                # 先落一轮结果
        have = self._have_results()
        self.assertGreater(have, 0, "全量写库后应当有抽取结果可跳")
        d = self._post(mode="incremental")
        self.assertNotIn("error", d)
        self.assertEqual(d["scope"]["skipped"], have)
        self.assertEqual(d["scope"]["processed"], 6 - have)
        self.assertEqual(d["scope"]["articles_found"], 6)

    def test_增量档全都抽过不算错误(self):
        """⚠ 这是接口层的坑：全量落库后再点增量，会有 0 篇要抽。
        那**不是失败**，不能 early-return 成 {"error":...} ——
        前端会把 error 渲染成红色报错，用户以为跑挂了。"""
        self._post(write=True)
        d = self._post(mode="incremental")
        self.assertNotIn("error", d)
        self.assertEqual(d["scope"]["processed"], 0)
        self.assertEqual(d["scope"]["skipped"], 6)
        self.assertEqual(d["scope"]["articles_found"], 6)

    def test_未知档位明确报错而不是静默当全量(self):
        d = self._post(mode="随便写")
        self.assertIn("error", d)
        self.assertIn("mode", d["error"])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM entities").fetchone()[0], 0,
            "档位不认识时必须什么都没跑")

    def test_档位大小写与空白不敏感(self):
        d = self._post(mode="  Incremental ")
        self.assertEqual(d["scope"]["mode"], "incremental")

    def test_空口径仍报错(self):
        d = self._post(column="不存在的栏目")
        self.assertIn("error", d, "真的没文章时，还是要说清楚是口径问题")


# ==================================================== 抽样配额分配的旧实现
# 以下是 git HEAD:cacx/db.py 里 sample_articles 的原算法**逐行照抄**
# （只去掉锁、把 self.conn 换成 d.conn），当参照物用。
# 别删：改配额分配就是改报告分母，必须随时能对回旧行为。
def _old_sample_ids(d, site="", column="", date_from="2024-01-01",
                    date_to="2099-12-31", limit=500, strategy="stratified"):
    COLS = ("id,site,column_name,title,pub_day,url,chars,"
            "COALESCE(content_empty,0) ce")
    limit = max(1, min(int(limit), 200000))
    where, args = ["pub_day>=?", "pub_day<=?"], [date_from, date_to]
    if site:
        where.append("site=?"); args.append(site)
    if column:
        where.append("column_name=?"); args.append(column)
    w = " WHERE " + " AND ".join(where)
    years = [dict(r) for r in d.conn.execute(
        f"SELECT substr(pub_day,1,4) y, COUNT(*) n FROM articles{w} "
        f"GROUP BY y ORDER BY y", args)]
    if strategy == "recent":
        rows = [dict(r) for r in d.conn.execute(
            f"SELECT {COLS} FROM articles{w} ORDER BY pub_day DESC, id DESC "
            f"LIMIT ?", args + [limit])]
        picked: dict = {}
    else:
        ny = max(1, len(years))
        per = max(1, limit // ny)
        rows, picked = [], {}
        for y in years:
            yr = str(y["y"]); offset = 0; want = min(per, y["n"])
            while offset < want:
                batch = d.conn.execute(
                    f"SELECT {COLS} FROM articles{w} "
                    f"AND substr(pub_day,1,4)=? "
                    f"ORDER BY pub_day DESC, id DESC LIMIT ? OFFSET ?",
                    args + [yr, want - offset, offset]).fetchall()
                rows += [dict(r) for r in batch]
                offset += len(batch)
                if not batch:
                    break
            picked[yr] = offset
        left = limit - len(rows)
        while left > 0:
            grew = 0
            for y in years:
                if left <= 0:
                    break
                yr = str(y["y"])
                if picked.get(yr, 0) >= y["n"]:
                    continue
                batch = d.conn.execute(
                    f"SELECT {COLS} FROM articles{w} "
                    f"AND substr(pub_day,1,4)=? "
                    f"ORDER BY pub_day DESC, id DESC LIMIT 1 OFFSET ?",
                    args + [yr, picked.get(yr, 0)]).fetchall()
                if not batch:
                    continue
                rows += [dict(r) for r in batch]
                picked[yr] = picked.get(yr, 0) + 1
                left -= len(batch); grew += len(batch)
            if not grew:
                break
    rows.sort(key=lambda r: (r["pub_day"], r["id"]))
    return [r["id"] for r in rows], picked


class TestSampleQuota(unittest.TestCase):
    """抽样配额分配：改实现可以，改结果不行。"""

    @classmethod
    def setUpClass(cls):
        cls.d, cls.tmp = _tmpdb("cacx_incr_quota_")
        # 4 个年份、池 5/3/8/2 —— 差额分布不均匀，「余额轮流补」才会真的跑起来
        rows, i = [], 0
        for year, n in [("2020", 5), ("2021", 3), ("2022", 8), ("2023", 2)]:
            for k in range(n):
                i += 1
                rows.append((i, "cac", "网信要闻", f"t{i}", f"http://x/{i}",
                             f"{year}-0{k % 9 + 1}-05", year, 10, 1, "短"))
        ph = ",".join("?" * len(ART_COLS.split(",")))
        with cls.d._lock:
            cls.d.conn.executemany(
                f"INSERT INTO articles({ART_COLS}) VALUES({ph})", rows)
            cls.d.conn.commit()
        cls.kw = dict(site="cac", column="网信要闻",
                      date_from="2000-01-01", date_to="2099-12-31")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.d.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_配额分配与旧实现逐位相同(self):
        """把 limit 扫一遍（含余额补足会跨 1~3 轮的那些值）。
        旧实现是「每补一篇发一条 LIMIT 1 OFFSET n」，新实现先算配额
        再每年发一条 —— 行为必须一模一样。"""
        for limit in (1, 2, 3, 4, 5, 8, 11, 12, 13, 14, 17, 18, 19, 40):
            old_ids, old_picked = _old_sample_ids(self.d, limit=limit,
                                                 **self.kw)
            s = self.d.sample_articles(limit=limit, **self.kw)
            self.assertEqual([r["id"] for r in s["items"]], old_ids,
                             f"limit={limit} 取回的文章与旧实现不同")
            self.assertEqual(s["years_taken"], old_picked,
                             f"limit={limit} 每年的配额与旧实现不同")
            self.assertEqual(s["n"], len(old_ids), f"limit={limit} 篇数不同")

    def test_余额先给年份表头的那些(self):
        """limit=12 → per=3：各年先取 3/3/3/2=11，余额 1 补 2020。
        顺序错了会让「哪一年多出样本」悄悄改变报告的年份分布。"""
        s = self.d.sample_articles(limit=12, **self.kw)
        self.assertEqual(s["years_taken"],
                         {"2020": 4, "2021": 3, "2022": 3, "2023": 2})
        self.assertEqual(s["n"], 12)

    def test_超过池子就取全部(self):
        s = self.d.sample_articles(limit=100, **self.kw)
        self.assertEqual(s["n"], 18, "库里只有 18 篇，不该凭空多出")
        self.assertEqual(s["years_taken"],
                         {"2020": 5, "2021": 3, "2022": 8, "2023": 2})

    def test_年份见底后不再硬补(self):
        """某一层见底时必须跳过它继续给别的层，不能死循环、也不能少取。"""
        s = self.d.sample_articles(limit=18, **self.kw)
        self.assertEqual(s["n"], 18)


if __name__ == "__main__":
    unittest.main(verbosity=2)
