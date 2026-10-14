# -*- coding: utf-8 -*-
"""
关联分析（networkx）—— 把关系表变成能写进报告的论据

为什么用 networkx 而不是自己写：度/介数/PageRank/Louvain 都是
有名字的经典算法，自己实现既慢又不可信，而且**固定随机种子才能复现**
（Louvain 默认带随机性，不锁 seed 的话同一份数据两次跑社区划分会不同，
报告里的数字就没法当场重跑给老师看）。

指标与报告的对应关系（这是设计的核心）：
    度中心性 ≠ 权责边界。
    「网信办度数第一」只能说明它出现最多，
    **介数**才能说明它在部门协作中居中协调 —— 而这才是权责边界的论据。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOUVAIN_SEED = 42  # 锁死随机性，否则同数据两次跑社区划分不一致


def _nx():
    """networkx 是必需依赖；若缺失给出可执行的修复提示而不是崩栈"""
    try:
        import networkx as nx
        return nx
    except ImportError:
        raise RuntimeError(
            "缺少 networkx（关联分析依赖）。\n"
            "  修复：pip install networkx\n"
            "  或在 requirements.txt 里补一行 networkx>=3.0 后重跑 start-cacx.bat")


def build_graph(relations: Iterable[Dict[str, Any]],
                min_weight: float = 1.0,
                undirected: bool = True,
                blocked_nodes: Optional[set] = None,
                rel_types: Optional[set] = None):
    """
    relations 每项须含 {src_name, dst_name, rel_type, weight}
    → (networkx.Graph, 边类型字典)

    ⚠ blocked_nodes：**低置信/脏节点黑名单**，边只要沾到其中一端就整条丢弃。
      实践上它承担的是「措施类」节点 —— 「三区一中心」「不仅规定」「了全社会」
      这类词蹭到了机构后缀，抽出来置信度低，若照样建边会把社区划分搅成
      一堆语义上不相干的碎片（实测社区 #1 里全是「一定的社会」「不仅会」）。
      对应方案的「低置信不进默认图谱」。

    ⚠ rel_types：只保留指定的关系类型。**在建图阶段就滤掉**，而不是
      建完图再挑边 —— 因为边的权重是共现强度，一对节点可能同时有
      joint_issue 与 issue 两种关系，混进图里的权重是两条之和。
      先筛后建才能让「只看 issue」时权重也是 issue 的强度。
      这一维以前界面上明明**显示着**各类型占比（joint_issue 71%）却**筛不了**。
    """
    nx = _nx()
    G = nx.Graph() if undirected else nx.DiGraph()
    types: Dict[Tuple[str, str], set] = {}
    blocked = blocked_nodes or set()
    want = {t for t in rel_types if t} if rel_types else None
    for r in relations:
        s = (r.get("src_name") or "").strip()
        d = (r.get("dst_name") or "").strip()
        if not s or not d or s == d:
            continue
        if s in blocked or d in blocked:
            continue
        rt = r.get("rel_type") or ""
        if want and rt not in want:
            continue
        try:
            w = float(r.get("weight") or 1.0)
        except (TypeError, ValueError):
            w = 1.0
        if w < min_weight:
            continue
        if G.has_edge(s, d):
            # 同一对可能有多条不同类型的边（既是联合发文又是发布）。
            # 无向图里只保留一条边，但**权重累加**（共现强度），
            # 边类型集合并起来（前端点边时能看到全部关系类型）。
            G[s][d]["weight"] += w
            types[(s, d)].add(rt)
        else:
            G.add_edge(s, d, weight=w)
            types[(s, d)] = {rt}
    return G, types


# 图例/配色用的四个实体类型。顺序即前端图例顺序，别随意改。
ENTITY_TYPES = ("org", "policy", "problem", "measure")


def node_type_map(entities: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """
    {实体名: 类型} —— 给图上的节点定「它是哪一类实体」。

    ⚠ 这张表以前**根本不存在**：建图只吃关系的两端名字，实体类型在建图
      时被整个丢掉，前端那句 `n.group || 'org'` 于是把所有节点都当成机构，
      画成一片蓝点 —— 图例上写着「政策■治理问题▲措施◆」，实际上一条都没生效。
      要按类型筛选、要颜色形状双编码，都得先有这个映射。

    ⚠ 同一名字可能既是机构又是政策（实测 16561 个去重名里 28 个跨类型，
      如「南方AI破谣局」既被抽成 org 又被抽成 policy）。图里一个节点只能有
      一个颜色，所以按**出现次数取众数**。多数实现请直接调
      node_type_map_from_counts —— 那个拿着真实条数；这里按每条 1 折算，
      逐条喂进来时权重是均等的。
    """
    acc: Dict[str, Dict[str, int]] = {}
    for e in entities:
        nm = (e.get("name") or "").strip()
        ty = (e.get("type") or "").strip()
        if not nm or not ty:
            continue
        acc.setdefault(nm, {})
        acc[nm][ty] = acc[nm].get(ty, 0) + 1
    return node_type_map_from_counts(acc)


def node_type_map_from_counts(counts: Dict[str, Dict[str, int]]
                              ) -> Dict[str, str]:
    """
    {实体名: {类型: 条数}} → {实体名: 类型}（按众数）

    ⚠ 并列时按 ENTITY_TYPES 的顺序靠前者胜出 —— 保证同一份数据每次跑出来
      的配色完全一致。颜色随机化的话，同一张图刷新两次长得不一样，
      演示时截图对不上，报告里的图例也没法当证据。

    ⚠ 名字在计数表里、但类型不在 ENTITY_TYPES 内的（脏数据）直接跳过，
      不归到 org —— 归 org 会让脏节点混进「机构」这一类，
      筛「只看机构」时它又冒出来，看着像筛选没生效。
    """
    rank = {t: i for i, t in enumerate(ENTITY_TYPES)}
    out: Dict[str, str] = {}
    for nm, c in (counts or {}).items():
        cand = {t: n for t, n in (c or {}).items() if t in rank and n > 0}
        if not cand:
            continue
        out[nm] = min(cand, key=lambda t: (-cand[t], rank[t]))
    return out


def crop_by_node_types(G, node_types: Optional[Dict[str, str]],
                       want_types: Optional[set]):
    """
    只保留选中类型的节点 → (sub, note)

    ⚠ 返回的是**诱导子图**：两端都被保留的边才留。所以筛成「仅政策」时
      政策与政策之间的联合发文会显示，而政策与机构的边会整条消失 ——
      这不是丢数据，是筛选的定义。note 要讲清楚，界面上得显示出来，
      否则用户以为图坏了。

    ⚠ want_types 为空/None = 不筛，返回原图。**别把「全不选」当成筛**：
      调用方要把「一个都不选」当成非法输入挡掉，否则会得到一张空图，
      而用户看到的是「图空了」而不是「你什么都没选」。
    """
    if not want_types or not node_types:
        return G, ""
    keep = [n for n in G.nodes() if node_types.get(n) in want_types]
    nx = _nx()
    sub = G.subgraph(keep).copy()
    names = {"org": "机构", "policy": "政策",
             "problem": "治理问题", "measure": "措施"}
    shown = "、".join(names.get(t, t) for t in ENTITY_TYPES if t in want_types)
    note = f"只看{shown}类实体"
    # ⚠ 判空要**同时看节点和边**。本项目的实际形态是 org→X 单向，
    #   筛成"只看政策"会得到 448 个政策节点、但**零条边** ——
    #   节点数非零，只判 `not number_of_nodes()` 就漏掉了，
    #   于是界面上出现"有节点却画不出任何连线、也没有提示"。
    #   实测就踩过：节点在、边没了、用户以为图坏了。
    if not sub.number_of_nodes() or not sub.number_of_edges():
        note += ("；这批实体之间没有边（关系全是「机构→政策/问题/措施」单向，"
                 "同类实体之间没有连线）。请同时勾选「机构」")
    return sub, note


def blocked_names(entities: Iterable[Dict[str, Any]],
                  types_wanted: Optional[set] = None) -> set:
    """
    从实体表算出「不进图」的节点名集合。
    判据：confidence <= 阈值 或 needs_review 为真。
    types_wanted 非空时只在该类实体里挑（默认只排措施类）。
    """
    from .extract import NEEDS_REVIEW_BELOW
    out = set()
    for e in entities:
        if types_wanted and e.get("type") not in types_wanted:
            continue
        if e.get("needs_review") or \
                float(e.get("confidence") or 0) <= NEEDS_REVIEW_BELOW:
            out.add((e.get("name") or "").strip())
    out.discard("")
    return out


def analyze(G, types: Optional[Dict] = None,
            topn: int = 20) -> Dict[str, Any]:
    """
    → {degree, strength, betweenness, pagerank, communities,
       components, summary, by_type}

    ⚠ topn 强制转 int：FastAPI 的 `Query(20, ge=1)` 传进来的是
    「参数对象」而不是数字，直接拿去切片会报
    slice indices must be integers。在模块边界就转掉，别指望调用方记得。
    """
    topn = max(1, int(topn))
    nx = _nx()
    if G.number_of_nodes() == 0:
        return {"nodes": 0, "edges": 0,
                "degree": [], "strength": [], "betweenness": [],
                "pagerank": [], "communities": [], "components": 0,
                "summary": "没有关系数据", "by_type": []}

    deg = dict(G.degree())
    strength = dict(G.degree(weight="weight"))
    btw = nx.betweenness_centrality(G, weight=None)
    pr = nx.pagerank(G)
    comps = sorted(nx.connected_components(G), key=len, reverse=True)

    # 社区划分：锁 seed 保证可复现
    try:
        comms = nx.community.louvain_communities(G, seed=LOUVAIN_SEED)
    except Exception:
        comms = list(nx.community.greedy_modularity_communities(G))

    # 给每个节点算社区编号，方便前端上色
    node_comm: Dict[str, int] = {}
    comm_list: List[Dict[str, Any]] = []
    for i, c in enumerate(sorted(comms, key=len, reverse=True)):
        members = sorted(c)
        for m in members:
            node_comm[m] = i
        comm_list.append({"id": i, "size": len(members), "members": members})

    # ⚠ 每个条目必须带**全部**指标，不能只带自己那一列 ——
    #   前端节点 tooltip 要同时显示「度/加权度/介数/社区」，
    #   报告表格也是一张表列全指标。缺一个 key 就会在渲染时 KeyError。
    def _top(d: Dict[str, float], key: str) -> List[Dict[str, Any]]:
        items = sorted(d.items(), key=lambda kv: -kv[1])[:topn]
        out = []
        for n, v in items:
            ts: set = set()
            if types:
                ts |= types.get((n, ""), set())
                ts |= types.get(("", n), set())
            out.append({"name": n, key: round(v, 4),
                        "degree": int(deg.get(n, 0)),
                        "strength": round(strength.get(n, 0), 2),
                        "betweenness": round(btw.get(n, 0.0), 4),
                        "pagerank": round(pr.get(n, 0.0), 4),
                        "community": node_comm.get(n, -1),
                        "types": sorted(ts)})
        return out

    # 按关系类型统计（报告里「联合发文占多少」的证据）
    by_type: Dict[str, int] = {}
    if types:
        for pair, ts in types.items():
            for t in ts:
                by_type[t] = by_type.get(t, 0) + 1

    total_edges = sum(by_type.values()) or G.number_of_edges()
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "degree": _top(deg, "degree"),
        "strength": _top(strength, "strength"),
        "betweenness": _top(btw, "betweenness"),
        "pagerank": _top(pr, "pagerank"),
        "communities": comm_list,
        "components": len(comps),
        "largest_component": len(comps[0]) if comps else 0,
        "by_type": sorted(
            [{"rel_type": t, "count": c,
              "pct": round(c * 100.0 / total_edges, 1)}
             for t, c in by_type.items()],
            key=lambda x: -x["count"]),
        "isolated": sum(1 for n in deg if deg[n] == 1),
        "summary": (f"{G.number_of_nodes()} 个实体 / {G.number_of_edges()} 条关系 / "
                    f"{len(comps)} 个连通块，最大块 {len(comps[0]) if comps else 0} 个节点"),
        "node_community": node_comm,
        # ⚠ 全量映射也带出去（和 node_community 一个道理）：介数在图上是
        #   最贵的一步（Brandes，1600 节点约 2.7 秒），而画图那边同样需要
        #   逐节点的介数/PageRank。不带出去，to_pyvis_nodes_edges 就会把
        #   同一张图的介数**再算一遍** —— 「同一张图算两次」不只是慢，
        #   更是口径容易漂移的地方。
        #   调用方在返回响应前 pop 掉这几个键（避免多传几十 KB）。
        "node_betweenness": btw,
        "node_pagerank": pr,
        # ⚠ 必须带上「我算的是哪张图」的指纹。否则画图那边无从判断
        #   传进来的 result 是不是**自己那张子图**的 —— 调用方完全可以
        #   把全图的 analyze 结果传进来 + focus 参数，
        #   于是「聚焦某机构」画出来的是子图、介数却是全图的。
        #   这不是假想：tests_graph_filter 里就有一条测试专门守它，
        #   复用功能上线当天就被它抓红了（值 0.05 vs 1.0）。
        "graph_fingerprint": _fingerprint(G),
    }


def _fingerprint(G) -> str:
    """一张图的「身份」：节点数 + 边数 + 节点名排序后的摘要。

    ⚠ 不能只比节点数/边数：两张不同的图完全可能同规模。
      但也不必算哈希 —— 排序后的节点名串起来取个长度 + 首尾片段就够，
      目的是「不是同一张图就别复用」，不是密码学意义上的判等。
    """
    names = sorted(G.nodes())
    joined = "\x00".join(names)
    return f"{G.number_of_nodes()}:{G.number_of_edges()}:{len(joined)}:" \
           f"{joined[:24]}:{joined[-24:]}"


def crop_scope(G, focus: str = "", keyword: str = "",
               hop: int = 1,
               node_types: Optional[Dict[str, str]] = None,
               want_types: Optional[set] = None):
    """
    按「实体类型 / 聚焦邻域 / 名称含」裁出子图 → (sub, scope_note)

    ⚠ 单独抽出来是因为**指标和画图必须用同一张图**：
      画图在 to_pyvis_nodes_edges 里裁子图，指标在 analyze 里算 ——
      两边各裁一次，就很容易出现「图是子图、数字是全图」的情况，
      而界面上写着「介数只在这张子图里算」，那就是骗用户。
      调用方裁一次，把 sub 同时喂给 analyze 和 to_pyvis。

    ⚠ 筛选顺序：**类型 → 聚焦 → 关键词**。
      类型放最前，因为它是「这张图里有哪些实体」的粗筛；若放在聚焦之后，
      「只看政策」就变成「只看聚焦邻域里的政策」——同一条筛选语句，
      换个顺序含义就变了，而用户以为自己选的是前者。
      反过来放在最前也不损失什么：聚焦/关键词都是在这批节点里再挑。

    ⚠ 焦点不在图里要**说清楚**并返回空图，不能静默回退成整张图 ——
      用户会以为筛生效了，其实什么都没筛。
    """
    nx = _nx()
    sub = G
    notes: List[str] = []
    focus = (focus or "").strip()
    kw = (keyword or "").strip()
    if want_types and node_types:
        sub, n1 = crop_by_node_types(sub, node_types, want_types)
        if n1:
            notes.append(n1)
    if focus:
        if focus in sub:
            sub = nx.ego_graph(sub, focus, radius=max(1, int(hop)))
            notes.append(f"以「{focus}」为中心 {max(1, int(hop))} 跳邻域")
        else:
            sub = nx.Graph()
            notes.append(f"「{focus}」不在当前图里"
                         f"（可能度数不够或已被前面的筛子滤掉）")
    if kw:
        sub = sub.subgraph([n for n in sub.nodes() if kw in n]).copy()
        notes.append(f"名称含「{kw}」")
    return sub, "；".join(notes)


def to_pyvis_nodes_edges(result: Dict[str, Any],
                         G, types: Optional[Dict] = None,
                         min_degree: int = 1,
                         limit: int = 300,
                         max_degree: int = 0,
                         keyword: str = "",
                         focus: str = "",
                         hop: int = 1,
                         node_types: Optional[Dict[str, str]] = None,
                         want_types: Optional[set] = None,
                         avail_by_group: Optional[Dict[str, int]] = None,
                         find: str = ""
                         ) -> Dict[str, Any]:
    """
    networkx 结果 → pyvis 前端要的 {nodes, edges}。

    ⚠ 只画「度 >= min_degree」的核心节点：全画出来 500+ 节点会糊成一团，
      也没人能看清。折叠掉的放折叠区，前端可展开。
    节点大小 ∝ 度，title 里带介数/社区，悬停就能看。

    ⚠ 筛选顺序有讲究：焦点邻域与关键词**先裁子图**，再按度数筛核心。
      反过来的话，「聚焦中央网信办 1 跳」会被 min_degree=2 砍掉一大半 ——
      而这个筛选器存在的意义恰恰是「看某机构的直接协作对象」，
      那批节点在整张图里度都不高。

    ⚠ focus 裁出的是**子图**，介数/PageRank 必须在子图上重算
      （沿用全图指标等于在两个不同口径的数字之间比较，没有意义）。
      scope_note 回传裁剪说明，界面要把它显示出来。

    ⚠ 「在子图上重算」这件事由 **analyze() 负责**，这里只做画。
      调用方按 crop_scope(sub) 裁一次，把同一份 result 同时喂给
      analyze 与这里 —— 指标不重复计算，才不会出现两处口径漂移。
    """
    nx = _nx()
    comm = result.get("node_community", {})

    # ---- 焦点邻域 / 关键词：先裁子图 ----
    # ⚠ 传进来的 result 应当已经是在同一张子图上算的（见 crop_scope 的说明），
    #   这里只负责画；指标不重复计算，避免两处口径漂移。
    sub, scope_note = crop_scope(G, focus=focus, keyword=keyword, hop=hop,
                                 node_types=node_types, want_types=want_types)

    # ⚠ 指标优先**复用** result 里已经算好的映射，但**必须先验明身份**。
    #   只有 result 确实是「对 sub 这张图」算出来的才复用；
    #   否则自己算 sub 的 —— 沿用全图介数去画子图，就是两套口径混用
    #   （tests_graph_filter.test_介数在子图上重算而非沿用全图 守这条）。
    #   实测：自己重算在 1600 节点的图上要 2.7 秒，和 analyze 里那次一样 ——
    #   所以 _graph_payload 一定会先把 sub 裁出来、喂给 analyze，再把同一份
    #   result 传进来，这条快路径在正常路径上必然命中。
    btw = result.get("node_betweenness")
    pr = result.get("node_pagerank")
    if (btw is None or pr is None
            or result.get("graph_fingerprint") != _fingerprint(sub)):
        btw = nx.betweenness_centrality(sub, weight=None)
        pr = nx.pagerank(sub) if sub.number_of_nodes() else {}
    d_of = dict(sub.degree())
    s_of = dict(sub.degree(weight="weight"))

    hi = int(max_degree or 0)
    core = [n for n, d in d_of.items()
            if d >= min_degree and (not hi or d <= hi)]
    # ⚠⚠ 排序**必须带第二判据（节点名）**。
    #   只按度数排时，度数并列的节点之间顺序 = 建图时的插入顺序，
    #   而插入顺序又取决于关系行的返回顺序 —— SQL 只要多一个 where
    #   （比如页面上勾了站点/时间筛选），查询计划就可能变、行序就变、
    #   被 limit 截掉的那批同度数节点就换人。
    #   实测（真实库 8419 条关系，min_degree=2 / limit=300）：
    #   同一批数据只换行序，修复前能画出 1448 / 1449 / 1450 / 1458 四种边数；
    #   加上第二判据后恒为 1449。
    #   报告里写死一个边数、页面刷新一次变成另一个 —— 两边都不报错，
    #   老师一对数字就露馅。**截断必须只由图的性质决定，不由行序决定。**
    core.sort(key=lambda n: (-d_of[n], n))
    core = core[:limit]

    keep = set(core)
    nodes = []
    for n in core:
        d = d_of[n]
        # ⚠ group 必须带上，且**只能有一份口径**：
        #   前端按它决定颜色+形状（机构●政策■治理问题▲措施◆），
        #   以前这个字段压根不存在，于是前端 `n.group || 'org'` 把
        #   所有节点当成机构，图上一片蓝点、图例形同虚设。
        #   取不到类型时归 org 而不是丢弃节点 —— 丢节点会静默改变
        #   度/介数，用户只会觉得「图怎么少了点」，不会想到是缺字段。
        grp = (node_types or {}).get(n) or "org"
        if grp not in ENTITY_TYPES:
            grp = "org"
        nodes.append({
            "id": n,
            "label": n,
            "group": grp,
            "size": 12 + min(d * 3, 40),
            "degree": d,
            "strength": round(s_of.get(n, 0), 1),
            "betweenness": round(float(btw.get(n, 0.0)), 4),
            "pagerank": round(float(pr.get(n, 0.0)), 4),
            "community": comm.get(n, -1),
            "title": (f"{n}\n度 {d}｜加权度 {s_of.get(n,0):.1f}｜"
                      f"介数 {btw.get(n,0):.4f}｜PageRank {pr.get(n,0):.4f}"
                      f"｜社区 {comm.get(n,-1)}"),
        })

    edges = []
    for s, d, dat in sub.edges(data=True):
        if s not in keep or d not in keep:
            continue
        ts = sorted(types.get((s, d), set())) if types else []
        edges.append({
            "from": s, "to": d,
            "weight": round(float(dat.get("weight", 1)), 1),
            "types": ts,
            "title": f"{s} → {d}\n权重 {dat.get('weight'):.1f}"
                      + (f"\n类型 {'、'.join(ts)}" if ts else ""),
        })
    # ⚠ 输出次序也要定死。sub.edges() 的遍历顺序跟建图插入顺序走，
    #   同一张图两次打开可能先画这条后画那条 —— 边是叠在一起的，
    #   谁在上面会变，看上去就是「图在闪」。
    #   ⚠⚠ 只排**列表次序**，绝不去改 from/to：
    #     types 是以 (s, d) 为键的，而且 undirected=False 时用的是
    #     DiGraph —— `target`（机构针对问题）这类关系**方向本身带语义**。
    #     把首尾对调「规范化」会让 types.get((s, d)) 查不到，类型整片丢失。
    edges.sort(key=lambda e: (min(e["from"], e["to"]),
                              max(e["from"], e["to"])))

    # ---- 检索高亮 ----
    # ⚠ 与 `keyword` 的区别是**只标不改图**：
    #   keyword 是裁剪（不匹配的节点直接消失，指标也跟着变），
    #   find 是高亮（节点照旧全在，只有命中项换个颜色）。
    #   两者混用会让用户以为「搜不到 = 库里没有」，而实际上它只是在裁。
    # ⚠ 匹配口径与 keyword 保持一致（子串、大小写敏感），否则
    #   「搜得到但不高亮」和「高亮了但搜不到」会同时出现。
    find = (find or "").strip()
    hits = set()
    for x in nodes:
        x["hit"] = bool(find) and find in str(x["id"])
        if x["hit"]:
            hits.add(x["id"])
    # 边命中 = **两端都命中**。只点亮命中一端的边的话，搜一个泛词
    # （比如「网信」）几乎等于把整张图点亮，等于没筛。
    for e in edges:
        e["hit"] = e["from"] in hits and e["to"] in hits
    hit_nodes = len(hits)
    hit_edges = sum(1 for e in edges if e["hit"])

    # 图例要显示「每类各多少个」，所以把**画出来的**这批节点按类型点一次数。
    # ⚠ 必须数 nodes 而不是全图：图例写在图下面，标的是屏幕上看得见的那些，
    #   标成全图 1633 节点里的 856 个机构，图上只有 300 个，数字对不上。
    by_group: Dict[str, int] = {}
    for x in nodes:
        by_group[x["group"]] = by_group.get(x["group"], 0) + 1
    return {"nodes": nodes, "edges": edges,
            "scope_note": scope_note,
            "find": find,
            "find_hits": hit_nodes,
            "find_edge_hits": hit_edges,
            "scope_nodes": sub.number_of_nodes(),
            "scope_edges": sub.number_of_edges(),
            "hidden_nodes": max(0, sub.number_of_nodes() - len(nodes)),
            "by_group": [{"group": t, "n": by_group.get(t, 0)}
                          for t in ENTITY_TYPES],
            # ⚠ avail_by_group = 该类型在**尚未按类型筛选**时的节点数。
            #   图例的禁用态必须看它，而不是 by_group：by_group 是筛完的结果，
            #   拿它判禁用会让「取消勾选 → 计数 0 → 判为不存在 → 禁用 →
            #   点不回来」形成死锁，用户把自己锁死在筛掉的���态里。
            "avail_by_group": [
                {"group": t, "n": int((avail_by_group or {}).get(t, 0))}
                for t in ENTITY_TYPES]}
