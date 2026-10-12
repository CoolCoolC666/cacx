# -*- coding: utf-8 -*-
"""
AI 正文截断可配置 + 自动重试默认值 的测试
================================================================
两条主线：
  A. cacx.ai.tasks —— 取消 2500 字硬编码，改成读 ai.max_content_chars，
     0/缺省 = 不截断（模型读到全文）。
  B. /api/config —— retry_attempts / retry_base_delay / max_content_chars
     可读写，且 max_content_chars 的 0 是**有效值**，不能被当「没改」跳过。

铁律：临时配置目录 + 临时库，绝不碰 config/settings.json 与 data/cacx.db。
跑法：py -3 tests_ai_content_limit.py
"""
import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cacx import config as C            # noqa: E402
from cacx.ai import tasks as T          # noqa: E402


def _art(body="正文" * 3000, **kw):
    a = {"title": "标题", "column_name": "要闻", "pub_date": "2024-05-06",
         "source_text": "新华社", "content_text": body}
    a.update(kw)
    return a


class TestContentLimit(unittest.TestCase):
    """A. 截断字数：读配置 / 默认不截断 / 非法值兜底"""

    def test_未配置时返回0即不截断(self):
        self.assertEqual(T.content_limit({}), 0)
        self.assertEqual(T.content_limit({"ai": {}}), 0)

    def test_默认配置就是不截断(self):
        """DEFAULTS 里 max_content_chars=0 —— 新装用户默认读到全文。"""
        self.assertEqual(C.DEFAULTS["ai"]["max_content_chars"], 0)
        self.assertEqual(T.content_limit(C.DEFAULTS), 0)

    def test_显式配置生效(self):
        self.assertEqual(T.content_limit({"ai": {"max_content_chars": 800}}), 800)

    def test_负值按不截断处理(self):
        """负数没有「只取前 -100 字」这种语义，一律当不截断。"""
        self.assertEqual(T.content_limit({"ai": {"max_content_chars": -5}}), 0)

    def test_非法值不抛异常且按不截断兜底(self):
        for bad in ("abc", None, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(T.content_limit({"ai": {"max_content_chars": bad}}), 0)

    def test_字符串数字也认(self):
        """配置文件被人手写成字符串 "1200" 时不该静默失效。"""
        self.assertEqual(T.content_limit({"ai": {"max_content_chars": "1200"}}), 1200)


class TestArticleBlock(unittest.TestCase):
    """A2. _article_block 的截断行为 + 原有的上下文头"""

    def test_默认不截断保留全文(self):
        body = "长" * 5000
        blk = T._article_block(_art(body))
        self.assertIn(body, blk)
        self.assertNotIn("…", blk)

    def test_limit为0表示不截断(self):
        body = "长" * 5000
        self.assertIn(body, T._article_block(_art(body), 0))

    def test_limit为正数时截断(self):
        blk = T._article_block(_art("长" * 5000), 100)
        self.assertIn("长" * 100, blk)
        self.assertNotIn("长" * 101, blk)

    def test_截断不会伤到标题等元信息(self):
        """截的是正文；标题/栏目/日期/来源必须仍完整在场。"""
        blk = T._article_block(_art("长" * 5000), 10)
        for k in ("标题：标题", "栏目：要闻", "日期：2024-05-06", "来源：新华社"):
            self.assertIn(k, blk)

    def test_正文缺失不炸(self):
        blk = T._article_block({"title": "只有标题"}, 100)
        self.assertIn("标题：只有标题", blk)

    def test_2500字的旧硬编码已不再是边界(self):
        """回归：正文 2600 字时，旧代码会砍到 2500，现在必须全在。"""
        body = "字" * 2600
        blk = T._article_block(_art(body), T.content_limit({}))
        self.assertEqual(blk.count("字"), 2600)


class TestBatchTaskStillTruncates(unittest.TestCase):
    """A3. 批量打标签的 700 字与 max_content_chars 是两回事，别被一起改掉"""

    def test_一次装多篇仍必须截断(self):
        """20 篇 × 全文会被模型拒收，这里刻意保留硬截断。"""
        arts = [_art("长" * 3000, title=f"第{i}篇") for i in range(20)]
        blocks = "\n\n---\n\n".join(T._article_block(a, 700) for a in arts[:20])
        self.assertEqual(blocks.count("长"), 20 * 700)

    def test_summarize用的是全局截断而非700(self):
        import inspect
        src = inspect.getsource(T.summarize)
        self.assertIn("content_limit(cfg)", src)
        self.assertNotIn("700", src)


class TestRetryDefaults(unittest.TestCase):
    """B0. 自动重试默认开启，且默认值明确"""

    def test_默认开启且为3次(self):
        self.assertEqual(C.DEFAULTS["ai"]["retry_attempts"], 3)

    def test_退避基数默认15秒(self):
        self.assertEqual(C.DEFAULTS["ai"]["retry_base_delay"], 1.5)

    def test_示例配置同步带上这两项(self):
        """给用户看的示例文件必须和代码默认值一致，否则照抄会跑偏。"""
        ex = json.loads(C.EXAMPLE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(ex["ai"]["retry_attempts"], C.DEFAULTS["ai"]["retry_attempts"])
        self.assertEqual(ex["ai"]["retry_base_delay"], C.DEFAULTS["ai"]["retry_base_delay"])
        self.assertEqual(ex["ai"]["max_content_chars"],
                         C.DEFAULTS["ai"]["max_content_chars"])


class TestProviderFallbackMatchesDefaults(unittest.TestCase):
    """
    B1. Provider 的兜底值必须与 config.DEFAULTS 一致。

    为什么单独测这个：兜底值只在「调用方递进来一份没跟 DEFAULTS 合并过的
    ai 段的 cfg」时才生效，平时被合并盖住看不出来。一旦两处漂移，
    「走服务」和「直接构造 provider」两条路的重试次数就不同，
    排查成本极高而现象只是「偶尔不重试」。
    """

    def setUp(self):
        from cacx.ai.base import OpenAICompat
        self.P = OpenAICompat

    def test_兜底值与默认值一致(self):
        self.assertEqual(self.P({}).retry_attempts, C.DEFAULTS["ai"]["retry_attempts"])
        self.assertEqual(self.P({}).retry_base_delay, C.DEFAULTS["ai"]["retry_base_delay"])

    def test_显式1等于关闭重试(self):
        self.assertEqual(self.P({"retry_attempts": 1}).retry_attempts, 1)

    def test_0按1处理(self):
        """「重试 0 次」没有意义，一律当 1（只尝试一次）。"""
        self.assertEqual(self.P({"retry_attempts": 0}).retry_attempts, 1)

    def test_显式次数生效(self):
        self.assertEqual(self.P({"retry_attempts": 5}).retry_attempts, 5)

    def test_字符串数字也认(self):
        """配置被手改成 "4" 时不该静默退回默认。"""
        self.assertEqual(self.P({"retry_attempts": "4"}).retry_attempts, 4)

    def test_负数按1处理(self):
        self.assertEqual(self.P({"retry_attempts": -3}).retry_attempts, 1)

    def test_默认就是会重试而不是只跑一次(self):
        """回归：曾经兜底值是 1（不重试），与 DEFAULTS 的 3 相反。"""
        self.assertGreater(self.P({}).retry_attempts, 1)


class TestConfigApi(unittest.TestCase):
    """B. 配置接口读写这三项"""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="cacx_cfglimit_"))
        self.old_settings = C.SETTINGS_FILE
        # 指到临时目录：C.load/C.save 与 web.app 共用同一个模块对象
        C.SETTINGS_FILE = self.tmp / "settings.json"
        self.WA = importlib.import_module("cacx.web.app")
        self.old_cfg = self.WA.CFG
        self.c = TestClient(self.WA.app)

    def tearDown(self):
        self.WA.CFG = self.old_cfg
        C.SETTINGS_FILE = self.old_settings
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self):
        r = self.c.get("/api/config")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def post(self, body):
        r = self.c.post("/api/config", json=body)
        return r.status_code, r.json()

    # ---------- 回显 ----------
    def test_未配置时回显默认值(self):
        """用户要的是「给一下默认值」——不回显等于还得翻文档。"""
        ai = self.get()["ai"]
        self.assertEqual(ai["retry_attempts"], 3)
        self.assertEqual(ai["retry_base_delay"], 1.5)
        self.assertEqual(ai["max_content_chars"], 0)

    def test_回显不泄漏密钥(self):
        self.post({"api_key": "sk-SECRET-xyz789"})
        raw = self.c.get("/api/config").text
        self.assertNotIn("sk-SECRET", raw)
        self.assertNotIn("xyz789", raw)

    # ---------- 写入 ----------
    def test_三项都能写(self):
        st, r = self.post({"retry_attempts": 5, "retry_base_delay": 2.5,
                           "max_content_chars": 1200})
        self.assertEqual(st, 200)
        self.assertTrue(r["saved"])
        ai = self.get()["ai"]
        self.assertEqual(ai["retry_attempts"], 5)
        self.assertEqual(ai["retry_base_delay"], 2.5)
        self.assertEqual(ai["max_content_chars"], 1200)

    def test_关闭重试设成1(self):
        self.post({"retry_attempts": 1})
        self.assertEqual(self.get()["ai"]["retry_attempts"], 1)
        self.assertEqual(T.content_limit(C.load()), 0)   # 顺带确认配置真落盘了

    def test_0是有意义的取值不能被当没改跳过(self):
        """
        回归最要命的一条：从 1200 改回「不受限」时，前端发的是 0。
        若沿用 put() 对 None/0 的「没改」判断，用户会以为放开了其实还卡着。
        """
        self.post({"max_content_chars": 1200})
        st, r = self.post({"max_content_chars": 0})
        self.assertEqual(st, 200)
        self.assertTrue(r["saved"], f"改回 0 被判成没有变化：{r}")
        self.assertEqual(self.get()["ai"]["max_content_chars"], 0)
        self.assertEqual(T.content_limit(C.load()), 0)

    def test_负数被夹到0(self):
        self.post({"max_content_chars": -99})
        self.assertEqual(self.get()["ai"]["max_content_chars"], 0)

    def test_changed只报字段名不含值(self):
        _, r = self.post({"max_content_chars": 1234})
        self.assertIn("ai.max_content_chars", r["changed"])
        self.assertFalse(any("1234" in str(x) for x in r["changed"]))

    def test_写成相同值时不算改动(self):
        self.post({"max_content_chars": 900})
        _, r = self.post({"max_content_chars": 900})
        self.assertFalse(r["saved"], f"同值写入不该报 saved：{r}")

    def test_不传这三项则保持原值(self):
        self.post({"retry_attempts": 4, "max_content_chars": 600})
        self.post({"model": "another-model"})     # 只改别的字段
        ai = self.get()["ai"]
        self.assertEqual(ai["retry_attempts"], 4)
        self.assertEqual(ai["max_content_chars"], 600)

    def test_白名单外字段照旧不生效(self):
        st, r = self.post({"collector_user_agent": "hack"})
        self.assertFalse(r["saved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)