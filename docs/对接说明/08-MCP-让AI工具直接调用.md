# 08 · MCP：让 AI 工具直接调用本项目

> 一句话：把 cacx 包成 26 个 MCP 工具，
> 让 AI 助手（Claude / Cursor / Trae 等）能直接查语料、跑抽取、看图谱，
> 而不是在对话框里靠模型「回忆」网信办说过什么。

## 1 这部分要解决的问题

AI 助手回答「网信办对未成年人保护有哪些部署」这类问题时，
默认做法是**凭模型记忆作答**——训练数据里的内容可能过期、可能记错、
分不清哪句是原文哪句是模型自己的话。

有了 MCP，AI 工具可以直接：

- 调 `cacx_search` 拿到**真实语料**，回答里能引原文链接
- 调 `cacx_mine_orgs` 跑实体抽取，结果直接落到同一个库里
- 调 `cacx_graph` 拿图数据，回答里带得动结构

这就把「模型说的」和「库里查到的」分开了。

## 2 用户入口

在 MCP 客户端里配置本项目：

```json
{
  "mcpServers": {
    "cacx": {
      "command": "python",
      "args": ["-m", "cacx.mcp_server"],
      "cwd": "<项目根>"
    }
  }
}
```

配好后 AI 工具侧会出现 `cacx_*` 开头的工具。

## 3 与工具的接口面 —— 26 个工具

### 检索与浏览

| 工具 | 作用 |
|---|---|
| `cacx_search` | 全文检索（支持站点 / 栏目 / 年份 / 日期区间 / 分词 / 布尔） |
| `cacx_get_article` | 取单篇全文与元数据 |
| `cacx_facets` | 栏目 / 年份等分面 |
| `cacx_stats` | 库规模统计 |
| `cacx_list_sites` | 已注册站点 |
| `cacx_list_links` | 站外链接及失败原因 |

### 数据采集与同步

| 工具 | 作用 |
|---|---|
| `cacx_sync` | 触发一次同步 |
| `cacx_doctor` | 体检：数据库完整性、索引状态 |

### 实体关系（R1）

| 工具 | 作用 |
|---|---|
| `cacx_mine_orgs` | 对文章跑机构/实体抽取 |
| `cacx_put_entities` | 写入实体 |
| `cacx_put_relations` | 写入关系 |
| `cacx_list_entities` | 列实体 |
| `cacx_list_relations` | 列关系 |
| `cacx_purge` | 清图重抽 |

### 图谱（R2）

| 工具 | 作用 |
|---|---|
| `cacx_graph` | 图数据（节点 + 边） |
| `cacx_graph_stats` | 图规模概览 |

### 样本配置

| 工具 | 作用 |
|---|---|
| `cacx_sample_options` | 可选的站点 / 栏目 / 年份组合 |
| `cacx_sample` | 分层抽样取样本（用于人工标注配样） |

### AI 相关

| 工具 | 作用 |
|---|---|
| `cacx_ai_status` | AI 是否可用、用哪个模型 |
| `cacx_ai_config` | 读写 AI 配置 |
| `cacx_summarize` | 单篇摘要 |
| `cacx_summarize_batch` | 批量摘要（起后台任务） |

### 新词发现

| 工具 | 作用 |
|---|---|
| `cacx_newwords_discover` | 发现候选新词 |
| `cacx_newwords_review` | 标记候选「要 / 不要」 |
| `cacx_newwords_adopt` | 采纳入词典 |
| `cacx_list_userdict` | 查当前词典 |

### 实现

`cacx/mcp_server.py`（49 KB）。

**MCP 与网页共用同一套实现**——这是本项目的一条硬约束。
两边都调 `cacx/db.py` 与 `cacx/extract.py`，不各写一份业务逻辑。

代价：FastAPI 路由函数的默认值是 `Query(...)` **参数对象**，
走 HTTP 时框架会替换，走内部调用时不会。所以凡是要被两处调用的逻辑，
都抽成模块级 `_xxx_impl`，路由只做薄壳。
这个坑在 `/api/graph/analysis` 上踩过一次（`TypeError: '>' not supported
between 'Query' and 'int'`）。

## 4 数据流

```
 AI 客户端（Claude / Cursor / Trae …）
        │  JSON-RPC over stdio
        ▼
 cacx/mcp_server.py
        │  ① 解析工具名与参数
        │  ② 调 cacx/db.py 的查询函数 或 cacx/extract.py 的抽取函数
        ▼
 data/cacx.db          ← 与网页**同一个库**
        │
        ▼
 结构化结果回给客户端
        │
        ▼
 AI 生成回答（引用的链接就是库里 articles.url）
```

**关键性质**：MCP 写入（`cacx_put_entities` / `cacx_put_relations`）
与网页看到的是同一份数据。在 MCP 里抽完实体，
打开网页的关系图谱就能立刻看到——不需要同步、不需要导入导出。

## 5 口径与分母

- MCP 的搜索**默认不加日期下限**，返回全库结果。
  AI 客户端通常不知道作业要求是「2024 年以来」，
  所以这个限制要**在提示词里写死**，或者由调用方显式传 `date_from`。
  ⚠ 这是个容易出错的地方：报告里如果引用 MCP 的回答，
  要注明日期范围是不是限定过的。
- `cacx_graph` 返回的是**筛选后**的图，默认参数与网页一致。
- `cacx_sample` 默认 **stratified**（分层抽样），
  保证样本里各年份、各栏目的比例接近全库——不是随手取前 N 条。

## 6 已知局限（写进报告）

1. **MCP 客户端的信任边界**。谁能连上这个 server 谁就能读写数据库。
   目前没有鉴权，只适合本机使用。**不要把它暴露到网络上。**

2. **AI 客户端看不到「口径」提示**。网页上有大段口径回显文字，
   MCP 返回的是结构化数据，客户端 AI 通常不会主动复述口径。
   所以经 MCP 得到的回答**更容易丢掉分母说明**——
   这也是报告要人工写、不直接贴 AI 回答的原因。

3. **`cacx_purge` 是破坏性操作**。它清空图谱让 AI 重抽，
   如果 AI 客户端传错参数，图谱就没了。
   当前有确认参数，但**这属于「AI 能触发的不可逆操作」**，
   真要长期用应该加一层限制（比如只允许在小样本上跑）。

4. **配置里含 API Key**。`cacx_ai_config` 能读写配置。
   密钥全程掩码，`show` 与命令行预览都不回显完整 key，
   但工具本身具备改写能力——别把 MCP server 开给不可信的客户端。

5. **MCP 与网页共用实现，但共用的是「查询」，不是「页面状态」**。
   网页上的筛选（站点多选、日期区间、聚焦跳数）在 MCP 侧要重新传一遍参数，
   没有「继承当前页面状态」这回事。
