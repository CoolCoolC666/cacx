# cacx · 网信办公开语料库工具

中国网信办（`www.cac.gov.cn`）与浙江网信网（`www.zjwx.gov.cn`）公开文章的
**采集 / 检索 / 分析**一体化工具。Python + FastAPI，本地起服务、浏览器访问；
同时保留完整命令行。

> 当前内置语料：**4507 篇 / 666 万字**（2024-01-01 ~ 2026-10-06）

---

## 快速开始

**Windows 双击即可**（不用记命令）：

```
start-cacx.bat        双击 → 自动装依赖 → 起服务 → 自动开浏览器
sync-latest.bat       双击 → 同步两站最新文章 → 打印统计
```

首次运行会自动 `pip install`，之后就直接开。关闭黑色命令行窗口即停止服务。

命令行用法：

```bash
git clone <repo>
cd <repo>
pip install -r requirements.txt

python -m cacx init          # 初始化配置与数据库
python -m cacx sync          # 抓取两站文章入库（首次约 20 分钟）
python -m cacx stats         # 看分布
python -m cacx serve         # 起检索界面 http://127.0.0.1:8770/
```

装成命令行（可选，装完直接用 `cacx`）：

```bash
pip install -e .
cacx stats
```

---

## 命令符

| 命令 | 作用 |
| --- | --- |
| `cacx init` | 初始化配置与数据库 |
| `cacx import` | 把既有的 json 语料灌入数据库 |
| `cacx stats` | 站点/栏目/年份分布 + 数据体检 |
| `cacx search "关键词"` | 检索，支持 `--site --column --year --date-from --date-to --min-chars --sort -n` |
| `cacx show <id>` | 看单篇全文 |
| `cacx config show\|set\|import\|export` | 配置管理（密钥自动掩码） |
| `cacx sync --site cac` | 增量同步（`--mode list-only` 只看增量条数） |
| `cacx export "关键词" -o out.csv` | 把检索结果导出本地 CSV / JSONL |
| `cacx ai status` | 看 AI 是否配好、配的哪家 |
| `cacx ai summarize ...` | 批量生成摘要（**默认只预览**，加 `--commit` 才真写库） |
| `cacx doctor` | 接口自检：站点是否改版、选择器是否失效 |
| `cacx serve` | 启动 Web 检索服务 |

例子：

```bash
cacx search "数据出境" -n 10
cacx search "清朗" --site zjwx --year 2026
cacx search "" --column 网信要闻 --sort chars_desc -n 20
cacx doctor --no-ai
```

### sync 的年份控制

```bash
cacx sync --year 2026                 # 只同步 2026 年
cacx sync --from-year 2025 --to-year 2026
cacx sync --year 2026 --mode list-only  # 只看条数，不写库
```

两站列表接口都是**按日期倒序**返回的，所以给了年份窗口时，采集器碰到早于
起始日的条目就**早停**，不再往下翻页。实测效果：CAC 同步 2026 年从翻 2.5 万条
存量降到 **6.7 秒**。

浙江站有个坑：同一年可能有跨年同日，判断早停用的是「**整页无一条落入窗口
且页面里存在更早的条目**」，只看单条会误切。

### export 导出

```bash
cacx export "清朗" -o 清朗.csv              # CSV
cacx export "清朗" -o 清朗.jsonl --format jsonl
cacx export "" --site cac --year 2026 -o 2026.csv --with-content
```

筛选条件跟 `search` 完全一致；`--with-content` 会把正文一并带出
（不加则只导元信息，文件小很多）。

### ai summarize 批量摘要

```bash
cacx ai status                       # 先看配没配
cacx ai summarize --recent 5          # 预览最近 5 篇要生成什么
cacx ai summarize --all-missing --limit 20 --commit   # 真的生成并落库
cacx ai summarize 关键词 -n 10 --commit              # 按关键词筛
```

范围由 `--id / --recent / --all-missing / 关键词` 指定，`--limit` 卡上限，
**不加 `--commit` 一律只打印预览**，一次误触不会烧配额。

---

## 中文检索是怎么做的

SQLite FTS5 的 `trigram` 分词器对中文有效，但**要求查询词 ≥ 3 个字符**——
实测 2 字词（如「清朗」）命中数为 0。所以这里做了两级降级：

| 查询 | 走法 | 实测 |
| --- | --- | --- |
| `数据出境负面清单` | FTS5 索引 | 18 条 / 1ms |
| `网信办` | FTS5 索引 | 1457 条 / 14ms |
| `清朗` | LIKE 扫描兜底 | 529 条 / 57ms |

**混合查询时按最长的短板决定**：只要有一个词短于阈值（如「清朗 专项行动」里的
「清朗」），整条查询退回 LIKE，避免半边走索引半边走扫描导致漏结果。
自检里有一条断言专门验证「同一关键词两种模式结果数一致」。

---

## AI 部分

云端 API 优先，本地 Ollama 可切。三个任务：`summarize`（摘要+标签）、
`tag`（批量打标签）、`doctor`（接口自检的补充诊断）。

**默认只预览不执行**——生成类调用一律 `dry_run=True`，要真发请求必须显式
`dry_run=False`。防止一次误触烧掉配额。

```bash
# 配置（存到本项目 config/settings.json，不动任何第三方工具的原配置）
cacx config set ai.provider openai_compat
cacx config set ai.base_url https://api.example.com/v1
cacx config set ai.api_key sk-xxxxxxxx        # show 时自动掩码
cacx config set ai.model your-model

# 也支持从别处一次性导入，不用手抄
cacx config import ~/Downloads/some-tool-config.json
```

`doctor` 的 AI 层是**补充**而非依赖：规则层永远先跑并独立给出结论，AI 挂了
只是少一段建议，不影响判断。

---

## Web 界面

`cacx serve`（或双击 `start-cacx.bat`）后打开 http://127.0.0.1:8770/

**检索页**

- 关键词 + 站点/栏目/年份/日期区间/字数下限 组合筛选，四种排序
- **命中高亮**：标题与正文片段里的命中词标黄，长词优先不切碎
- 一键导出当前检索结果（CSV / JSONL，可指定条数）
- 结果点开是详情抽屉，全文 + 元信息 + **跳转原文**
- 原文跳转走 `/open?id=…` 中转并记录点击 —— 以后做「检索→阅读→传播」
  行为分析时，这张行为表就是最直接的一手数据

**数据面板**

- 按年 / 按月分布柱状图（可切站点）
- 栏目 Top 12
- 检索行为统计：各类操作次数、跳原文最多的文章

接口：`/api/search` `/api/article/{id}` `/api/facets` `/api/stats`
`/api/recent` `/api/health` `/api/export` `/api/trend` `/api/events`
`/api/ai/status` `/open`，交互式文档在 `/docs`。

---

## 项目结构

```
cacx/
├── config.py       配置持久化（密钥掩码、环境变量覆盖、可从外部导入）
├── db.py           SQLite schema + FTS5 检索 + 并发安全
├── importer.py     既有 json 语料批量入库
├── collector/      两站采集器 + sync/doctor 编排
│   ├── base.py     共用：限速重试、HTML→纯文本
│   ├── cac.py      中国网信办（/cms/JsonList JSON 端点）
│   └── zjwx.py     浙江网信网（jpaas HTML 端点）
├── ai/             适配层（OpenAI 兼容 / Ollama）+ 任务 + 自愈诊断
├── web/            FastAPI 服务 + 前端
└── cli/            命令符入口

config/    settings.json（gitignore）+ settings.example.json（提交）
data/      SQLite 数据库（gitignore）
selftest.py     29 项自检
stress_test.py  6 读 + 3 写并发压测
```

---

## 两个站点接口备忘

**中国网信办**

```
POST https://www.cac.gov.cn/cms/JsonList
body: channelCode / perPage / pageno / condition / fuhao / value
返回: {"totalRec": N, "list": [{topic,infourl,pubtime,filltime,coverurl,description}]}
```

- 返回是**扁平**结构，不是 `data.list`
- `perPage` 可开到 **200**（跑完 2.5 万条存量只需约 130 次请求）
- `totalRec` **有时是字符串**，要强转
- Referer 不校验，无 Cookie/验证码
- 正文在 `<DIV id=BodyLabel>`（**大写 DIV、属性无引号**），
  尾部混有 `<div id="网站群管理">` 需剥除

**浙江网信网**

```
GET /api-gateway/jpaas-publish-server/front/page/build/unit
    ?parseType=bulidstatic&webId=3238&pageId=<ColId>
    &paramJson={"pageNo":N,"pageSize":15}
```

- 总数从 `data.html` 里的 `count="N"` 取，别硬编码页数
- 栏目名**以站点自称为准**：治理监管 / 清朗网络 / 安全动态
  （不是「法治浙江 / 之江净网 / 网上辟谣」）
- 少数条目 `art_con` 源站即为空，属转载占位，不是 bug

---

## 数据说明

仓库**不含语料**，`data/` 与 `config/settings.json` 都在 `.gitignore` 里。
别人 clone 后跑 `cacx sync` 即可重建。

两站栏目清单已核对过完整性，详见 `docs/采集报告.md`。