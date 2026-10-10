import json
import urllib.request

BASE = "http://127.0.0.1:8770"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# 0) 备份原配置，测完还原
import pathlib
import sys
sys.path.insert(0, ".")
from cacx import config as C
orig = C.SETTINGS_FILE.read_text(encoding="utf-8")

try:
    # 1) 写入 provider + key
    st, r = post("/api/config", {"provider": "openai_compat",
                                 "base_url": "https://api.test.invalid/v1",
                                 "model": "test-model",
                                 "api_key": "sk-TESTKEY-abc123456",
                                 "enabled": True})
    check("写入配置返回 saved", st == 200 and r.get("saved") is True, str(r.get("changed")))
    check("changed 只报字段名不含值",
          all("sk-" not in c for c in r.get("changed", [])), str(r.get("changed")))

    # 2) 回读，确认 key 存进去了但没回显
    c = get("/api/config")
    check("key 已落盘（has_key=True, len>0）",
          c["ai"]["has_key"] is True and c["ai"]["key_len"] == len("sk-TESTKEY-abc123456"),
          f"len={c['ai']['key_len']}")
    raw = urllib.request.urlopen(BASE + "/api/config", timeout=20).read().decode("utf-8")
    check("回读响应不含 key 明文", "sk-TESTKEY" not in raw and "abc123456" not in raw)

    # 3) 关键：空 api_key 不应清掉已存的 key
    st, r = post("/api/config", {"model": "test-model-2"})
    c2 = get("/api/config")
    check("空 api_key 不会清掉已存密钥",
          c2["ai"]["has_key"] is True, f"has_key={c2['ai']['has_key']}")
    check("其它字段正常写入", c2["ai"]["model"] == "test-model-2")

    # 4) 显式清除（用独立字段，不用哨兵值冒充 key）
    st, r = post("/api/config", {"clear_key": True})
    c3 = get("/api/config")
    check("显式清除密钥生效", c3["ai"]["has_key"] is False, f"status={st}")

    # 4b) 合法 key 若恰好以 "__clear__" 开头，不能被误清
    post("/api/config", {"api_key": "__clear__not-really-a-key"})
    c3b = get("/api/config")
    check("哨兵样式前缀的合法 key 不被误清",
          c3b["ai"]["has_key"] is True and c3b["ai"]["key_len"] == len("__clear__not-really-a-key"),
          f"len={c3b['ai']['key_len']}")
    post("/api/config", {"clear_key": True})

    # 5) 非法 provider 要被拒
    st, r = post("/api/config", {"provider": "随便写的"})
    check("非法 provider 被拒 400", st == 400, f"status={st}")

    # 6) 不支持的字段不会写进去（白名单）
    st, r = post("/api/config", {"collector_user_agent": "hack"})
    check("白名单外的字段不生效", r.get("saved") is False, str(r.get("message")))

    # 7) 采集范围
    st, r = post("/api/config", {"date_start_cac": "2025-01-01",
                                 "date_end_cac": "2025-12-31"})
    c4 = get("/api/config")
    check("采集日期范围可写",
          c4["sites"]["cac"]["date_start"] == "2025-01-01"
          and c4["sites"]["cac"]["date_end"] == "2025-12-31",
          f"{c4['sites']['cac']['date_start']} ~ {c4['sites']['cac']['date_end']}")

finally:
    # 无条件还原，别把测试配置留给用户
    C.SETTINGS_FILE.write_text(orig, encoding="utf-8")
    c5 = get("/api/config")
    print(f"\n  已还原: provider={c5['ai']['provider']} "
          f"has_key={c5['ai']['has_key']} "
          f"cac={c5['sites']['cac']['date_start']}")

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)