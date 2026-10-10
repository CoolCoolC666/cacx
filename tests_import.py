"""扫描器的识别率与误伤测试。
每条用例都跑一遍 scan_credentials，验证：
  1) 该抓的抓到了
  2) 不该抓的**没有**被抓（误伤比漏抓更危险，会静默写坏配置）
"""
import sys

sys.path.insert(0, ".")
from cacx.config import scan_credentials

CASES = [
    # ---- 应识别 ----
    ("MCP env 大写变量名",
     {"mcpServers": {"minimax": {"command": "npx",
       "env": {"MINIMAX_API_KEY": "sk-jK8mP2xQ9vL4nR7tY1uB3wZ5cF6hD8gA0sE2fG4iO6kQ",
                "MINIMAX_BASE_URL": "https://api.minimax.cn/v1"}}}},
     {"ai.api_key", "ai.base_url"}),

    ("MCP headers Authorization",
     {"servers": {"x": {"url": "https://api.deepseek.com/v1",
                        "headers": {"Authorization": "Bearer sk-abcdefghij1234567890klmnopqrst"}}}},
     {"ai.api_key", "ai.base_url"}),

    ("扁平工具配置",
     {"api_key": "sk-1111111111111111aaaa", "base_url": "https://api.zhipuai.cn/v4",
      "model": "glm-4-plus"},
     {"ai.api_key", "ai.base_url", "ai.model"}),

    ("嵌套 provider",
     {"llm": {"default": {"provider": "openai", "apiKey": "sk-zzz9999888877776666yyy",
                          "api_base": "https://open.bigmodel.cn/api/paas/v4"}}},
     {"ai.api_key", "ai.base_url"}),

    ("命令行 args 带 --api-key",
     {"mcpServers": {"a": {"command": "python", "args": [
        "--api-key=sk-qqq111222333444555666777", "--base-url=https://api.moonshot.cn/v1"]}}},
     {"ai.api_key", "ai.base_url"}),

    ("args 数组分项",
     {"mcpServers": {"b": {"command": "node",
                          "args": ["server.js", "--port", "3000"]}}},
     set()),   # 不该误抓 --port 3000

    # ---- 不应误伤 ----
    ("图床 key（名字像但语义不对）",
     {"img": {"accessKeyId": "LTAI5txxxxxxxx", "accessKeySecret": "abcdef123456"},
      "proxy": "http://127.0.0.1:7890"},
     set()),

    ("静态资源 URL",
     {"cdn": {"img": "https://cdn.example.com/a.png",
              "css": "https://cdn.example.com/s.css",
              "favicon": "https://cdn.example.com/f.ico"}},
     set()),

    ("普通缓存 key（太短）",
     {"cache": {"key": "user:123", "token": "abc"}},
     set()),

    ("完全无关的配置",
     {"editor": {"fontSize": 14, "theme": "dark", "workspace": "C:/work"}},
     set()),

    # ---- 边界 ----
    ("只有站点首页 URL，不该当 API 端点",
     {"homepage": "https://www.cac.gov.cn"},
     set()),

    ("混合：既有真 key 也有干扰项",
     {"env": {"MY_API_KEY": "sk-real1234567890abcdefGHIJK",
              "PROXY_URL": "http://127.0.0.1:1080"},
      "cdn": "https://cdn.x.com/a.png"},
     {"ai.api_key"}),
]

ok = fail = 0
print("=" * 68)
print("导入扫描器测试")
print("=" * 68)
for name, src, expect in CASES:
    res = scan_credentials(src)
    got = set(res["candidates"])
    if got == expect:
        ok += 1
        mark = "PASS"
    else:
        fail += 1
        mark = "FAIL"
    print(f"  {mark}  {name}")
    if got != expect:
        print(f"        期望: {sorted(expect) or '（不识别任何）'}")
        print(f"        实际: {sorted(got) or '（不识别任何）'}")
        for f, c in res["candidates"].items():
            print(f"          {f} <- {c['from']}  ({c['why']})")

# 密钥绝不能出现在 masked 之外的任何地方
print()
print("=== 掩码检查 ===")
_leak = False
for name, src, _ in CASES:
    res = scan_credentials(src)
    for f, c in res["candidates"].items():
        if f == "ai.api_key":
            v = c["value"]
            if len(v) > 8 and v[-4:] in c["masked"]:
                pass
            else:
                _leak = True
                print(f"  FAIL {name}: 掩码不含末四位 → {c['masked']}")
            if v in str(c["masked"]):
                _leak = True
                print(f"  FAIL {name}: 掩码里出现了完整密钥")
if not _leak:
    ok += 1
    print("  PASS  所有密钥掩码正常，未泄露明文")

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)