"""导入功能的 HTTP 端到端测试。
真走 /api/config/import，验预览不写盘、应用才写盘、非法 JSON 有提示。
测完无条件还原配置。
"""
import json
import sys
from pathlib import Path
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cacx import config as C

BASE = "http://127.0.0.1:8770"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def post(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


_orig = C.SETTINGS_FILE.read_text(encoding="utf-8")
MCP = json.dumps({"mcpServers": {"minimax": {"command": "npx", "env": {
    "MINIMAX_API_KEY": "sk-jK8mP2xQ9vL4nR7tY1uB3wZ5cF6hD8gA0sE2fG4iO6kQ",
    "MINIMAX_BASE_URL": "https://api.minimax.cn/v1"}}}})

try:
    print("=" * 64)
    print("导入端到端测试")
    print("=" * 64)

    # 1) 预览不得写盘
    before = C.SETTINGS_FILE.read_text(encoding="utf-8")
    st, r = post("/api/config/import", {"text": MCP, "apply": False})
    check("预览返回 200", st == 200, str(r.get("detail", "")))
    check("预览标记为未写入", r.get("applied") is False)
    got = {i["field"]: i for i in r["items"] if i["found"]}
    check("MCP env 结构能识别出密钥与地址",
          "ai.api_key" in got and "ai.base_url" in got,
          f"found={sorted(got)}")
    check("密钥只回显掩码不出明文",
          "sk-jK8mP2xQ9vL4nR7" not in json.dumps(r, ensure_ascii=False),
          f"masked={got.get('ai.api_key',{}).get('masked')}")
    check("预览后文件未被改动",
          C.SETTINGS_FILE.read_text(encoding="utf-8") == before)

    # 2) 非法 JSON 要给出可操作的提示
    st, r = post("/api/config/import", {"text": '{"mcpServers": {"a": '})
    check("非法 JSON 返回 400", st == 400, str(st))
    check("提示里说明了原因与行号",
          "不是合法 JSON" in r.get("detail", "")
          and ("行" in r.get("detail", "")),
          r.get("detail", "")[:60])

    # 3) 空内容
    st, r = post("/api/config/import", {"text": "   "})
    check("空内容返回 400", st == 400)

    # 4) 完全无关的配置：识别不到并给出提示
    st, r = post("/api/config/import",
                 {"text": '{"editor":{"fontSize":14,"theme":"dark"}}',
                  "apply": False})
    check("无关配置识别不到任何字段",
          not any(i["found"] for i in r["items"]))
    check("且给出「没识别出」的提示",
          any("识别出" in w for w in r.get("warnings", [])),
          str(r.get("warnings"))[:70])

    # 5) 真正应用 —— 会改配置，测完还原
    st, r = post("/api/config/import", {"text": MCP, "apply": True})
    check("应用返回 200 且标记已写入",
          st == 200 and r.get("applied") is True)
    check("只改该改的字段",
          set(r.get("changed", [])) <= {"ai.api_key", "ai.base_url", "ai.model"},
          str(r.get("changed")))
    now = get("/api/config")
    check("密钥已写入（has_key=True）", now["ai"]["has_key"] is True)
    check("接口地址已写入",
          now["ai"]["base_url"] == "https://api.minimax.cn/v1",
          now["ai"]["base_url"])

    # 6) 再导一次相同内容 → 无变化
    st, r = post("/api/config/import", {"text": MCP, "apply": True})
    check("重复导入相同内容 → 没有变化",
          r.get("changed") == [], str(r.get("message")))

    # 7) cacx 自身配置走整体合并
    st, r = post("/api/config/import",
                 {"text": json.dumps({"ai": {"model": "some-other-model"}}),
                  "apply": False})
    check("cacx 自身配置识别为整体合并",
          r.get("kind") == "cacx", str(r.get("kind")))

finally:
    C.SETTINGS_FILE.write_text(_orig, encoding="utf-8")
    after = get("/api/config")
    print(f"\n  已还原: provider={after['ai']['provider']} "
          f"has_key={after['ai']['has_key']} "
          f"base_url={after['ai']['base_url']}")

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)