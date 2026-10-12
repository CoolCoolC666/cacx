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
                blocked_nodes: Optional[set] = None):
    """
    relations 每项须含 {src_name, dst_name, rel_type, weight}
    → (networkx.Graph, 边类型字典)

    ⚠ blocked_nodes：**低置信/脏节点黑名单**，边只要沾到其中一端就整条丢弃。
      实践上它承担的是「措施类」节点 —— 「三区一中心」「不仅规定」「了全社会」
      这类词蹭到了机构后缀，抽出来置信度低，若照样建边会把社区划分搅成
      一堆语义上不相干的碎片（实测社区 #1 里全是「一定的社会」「不仅会」）。
      对应方案的「低置信不进默认图谱」。
    """
    nx = _nx()
    G = nx.Graph() if undirected else nx.DiGraph()
    types: Dict[Tuple[str, str], set] = {}
    blocked = blocked_nodes or set()
    for r in relations:
        s = (r.get("src_name") or "").strip()
        d = (r.get("dst_name") or "").strip()
        if not s or not d or s == d:
            continue
        if s in blocked or d in blocked:
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
            types[(s, d)].add(r.get("rel_type") or "")
        else:
            G.add_edge(s, d, weight=w)
            types[(s, d)] = {r.get("rel_type") or ""}
    return G, types


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
    """
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
    }


def to_pyvis_nodes_edges(result: Dict[str, Any],
                         G, types: Optional[Dict] = None,
                         min_degree: int = 1,
                         limit: int = 300) -> Dict[str, Any]:
    """
    networkx 结果 → pyvis 前端要的 {nodes, edges}。

    ⚠ 只画「度 >= min_degree」的核心节点：全画出来 500+ 节点会糊成一团，
      也没人能看清。折叠掉的放折叠区，前端可展开。
    节点大小 ∝ 度，title 里带介数/社区，悬停就能看。
    """
    deg = {n["name"]: n for n in result.get("degree", [])}
    # ⚠ degree 列表只保留 topn。长尾节点会查不到介数/社区，
    #   所以这里**就地重算全量**，而不是从 top 列表里捞 ——
    #   否则核心节点之外的节点 tooltip 会缺指标。
    nx = _nx()
    full_deg = dict(G.degree())
    full_str = dict(G.degree(weight="weight"))
    btw = nx.betweenness_centrality(G, weight=None)
    pr = nx.pagerank(G)
    comm = result.get("node_community", {})

    core = [n for n, d in full_deg.items() if d >= min_degree]
    core.sort(key=lambda n: -full_deg[n])
    core = core[:limit]

    keep = set(core)
    nodes = []
    for n in core:
        d = full_deg[n]
        nodes.append({
            "id": n,
            "label": n,
            "size": 12 + min(d * 3, 40),
            "degree": d,
            "strength": round(full_str.get(n, 0), 1),
            "betweenness": round(float(btw.get(n, 0.0)), 4),
            "pagerank": round(float(pr.get(n, 0.0)), 4),
            "community": comm.get(n, -1),
            "title": (f"{n}\n度 {d}｜加权度 {full_str.get(n,0):.1f}｜"
                      f"介数 {btw.get(n,0):.4f}｜PageRank {pr.get(n,0):.4f}"
                      f"｜社区 {comm.get(n,-1)}"),
        })

    edges = []
    for s, d, dat in G.edges(data=True):
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
    return {"nodes": nodes, "edges": edges,
            "hidden_nodes": max(0, G.number_of_nodes() - len(nodes))}